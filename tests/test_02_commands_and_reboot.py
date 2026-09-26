"""Commands, asynchronous operations, and reboot.

These exercise the parts that a real device makes painful: things that take
time and complete later, and reboots.
"""

from __future__ import annotations

import time

import pytest
import requests

from uspctl import UspError

SSID_PATH = "Device.WiFi.SSID.1.SSID"
RESET_CMD = "Device.WiFi.SSID.1.Reset()"
PING_CMD = "Device.IP.Diagnostics.IPPing()"


# ----------------------------------------------------------------------
# Synchronous commands
# ----------------------------------------------------------------------


def test_sync_command_runs_in_the_device(controller, restore_ssid):
    """A USP command reaches the Python device and takes effect."""
    controller.set({SSID_PATH: "SomethingElse"})
    assert controller.get_one(SSID_PATH) == "SomethingElse"

    outputs = controller.operate(RESET_CMD)

    assert outputs.get("Status") == "Reset"
    assert controller.get_one(SSID_PATH) == "VirtualGateway-001"


def test_unknown_command_is_rejected(controller):
    with pytest.raises(UspError):
        controller.operate("Device.WiFi.SSID.1.NoSuchCommand()")


# ----------------------------------------------------------------------
# Asynchronous commands
# ----------------------------------------------------------------------


def test_async_command_completes_via_notification(controller, oper_subscription):
    """An async command returns immediately, then reports completion later.

    This is the shape every long-running device operation takes, firmware
    updates included.
    """
    command_key = "ping-success"
    started = controller.operate(PING_CMD, {"Host": "1.1.1.1", "NumberOfRepetitions": 3},
                                 command_key=command_key)

    # Async commands carry no output in the Operate response
    assert started == {}

    notification = controller.wait_for_notification(
        lambda n: n["type"] == "OperationComplete" and n.get("command_key") == command_key,
        timeout=30,
    )

    assert notification["command_name"] == "IPPing()"
    assert "err_code" not in notification, notification.get("err_msg")

    outputs = notification["output_args"]
    assert outputs["SuccessCount"] == "3"
    assert outputs["FailureCount"] == "0"
    assert int(outputs["AverageResponseTime"]) > 0


def test_async_command_reports_device_level_failure(controller, oper_subscription):
    """An unreachable host produces failures in the output, not an error."""
    command_key = "ping-unreachable"
    controller.operate(PING_CMD, {"Host": "203.0.113.1", "NumberOfRepetitions": 2},
                       command_key=command_key)

    notification = controller.wait_for_notification(
        lambda n: n["type"] == "OperationComplete" and n.get("command_key") == command_key,
        timeout=30,
    )

    outputs = notification["output_args"]
    assert outputs["SuccessCount"] == "0"
    assert outputs["FailureCount"] == "2"


def test_async_command_argument_validation(controller, oper_subscription):
    """A bad argument fails the command, and the failure reaches the controller."""
    command_key = "ping-no-host"

    try:
        controller.operate(PING_CMD, {"NumberOfRepetitions": 1}, command_key=command_key)
    except UspError:
        return      # rejected up front, which is also correct

    notification = controller.wait_for_notification(
        lambda n: n["type"] == "OperationComplete" and n.get("command_key") == command_key,
        timeout=30,
    )
    assert notification.get("err_code"), "expected the command to fail"


def test_async_command_is_visible_while_running(controller, device_url, oper_subscription):
    """The UI shows work in flight - the device is doing something, not blocked."""
    controller.operate(PING_CMD, {"Host": "1.1.1.1", "NumberOfRepetitions": 8,
                                  "Timeout": 400}, command_key="ping-slow")

    deadline = time.time() + 10
    seen = False
    while time.time() < deadline:
        state = requests.get(f"{device_url}/api/state", timeout=5).json()
        if state["system"]["runningJobs"]:
            seen = True
            break
        time.sleep(0.3)

    assert seen, "the running operation never showed up in the device state"


# ----------------------------------------------------------------------
# Reboot
# ----------------------------------------------------------------------


def test_reboot_emits_boot_event_and_keeps_config(controller, boot_subscription,
                                                  wait_for_agent):
    """Device.Reboot() produces a real disconnect, restart and Boot! event.

    The agent process genuinely exits and is restarted, so the Boot! comes from
    obuspa itself rather than from anything simulated.
    """
    controller.set({SSID_PATH: "SurvivesReboot"})
    controller.clear_notifications()

    controller.operate("Device.Reboot()")

    controller.wait_for_notification(
        lambda n: n["type"] == "Event" and n.get("event_name") == "Boot!",
        timeout=90,
    )

    wait_for_agent(timeout=90)

    # Configuration is persisted across the reboot, as on real hardware
    assert controller.get_one(SSID_PATH) == "SurvivesReboot"


def test_reboot_is_counted_by_the_device(controller, device_url, wait_for_agent):
    """The device tracks boots, so a test can tell a reboot really happened."""
    before = requests.get(f"{device_url}/api/state", timeout=5).json()["system"]

    controller.operate("Device.Reboot()")
    wait_for_agent(timeout=90)

    after = requests.get(f"{device_url}/api/state", timeout=5).json()["system"]

    assert after["bootCount"] == before["bootCount"] + 1
    assert after["rebootCause"] == "RemoteReboot"
    assert after["upTime"] <= before["upTime"] + 90
