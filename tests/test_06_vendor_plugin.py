"""A developer's own plug-in on the platform.

These flash the disk-monitor example onto the card, so they run last and need
Docker. The scenario is the one the platform exists for: custom C logic with a
background thread, stimulated by an environment fault, observed over USP.
"""

from __future__ import annotations

import time

import pytest

from conftest import post, system_state

MON = "Device.X_VDEV_DiskMonitor."
EVENT = MON + "SpaceLow!"


@pytest.fixture(scope="module")
def plugin_booted(flashed_plugin_card, device_url, wait_for_agent):
    """Seats the flashed card and reboots into it once for the module.

    Waits for the agent to be fully up, so a test that reboots again straight
    away is not doing so while the previous boot is still in its bootloader.
    """
    if not system_state(device_url)["sdcard"]["inserted"]:
        post(device_url, "/api/sdcard", {"action": "insert"})
    marker = system_state(device_url)["bootedFrom"].get("bootedAt", "")
    post(device_url, "/api/reboot", {"cause": "LocalReboot"})
    wait_for_agent(timeout=150, previous_boot=marker)
    yield flashed_plugin_card


def test_plugin_only_card_boots_builtin_with_plugin(controller, device_url, plugin_booted,
                                                    wait_for_agent):
    """A card carrying only plug-ins boots the built-in obuspa plus the plug-ins."""
    wait_for_agent(timeout=120)

    booted = system_state(device_url)["bootedFrom"]
    assert booted["from"] == "internal", booted
    assert booted["cardSeated"] is True
    assert any(p.endswith("disk-monitor.so") for p in booted["plugins"]), booted
    assert booted["manifest"]["label"] == plugin_booted["label"]

    assert controller.get_one("Device.LocalAgent.SoftwareVersion") == "11.0.0"
    assert controller.get_one(MON + "Threshold") == "90"
    assert controller.get_one(MON + "Path") == "/data"


def test_plugin_registration_is_visible_in_console(plugin_booted, console):
    lines = [e["text"] for e in console()]
    assert any("loading vendor plug-in disk-monitor.so" in t for t in lines)
    assert any(t.startswith("disk-monitor: watching") for t in lines)


def test_disk_monitor_raises_space_low(controller, plugin_booted, faults, wait_for_agent):
    """Fill the disk from the lab; the vendor's thread alarms over USP."""
    wait_for_agent(timeout=60)
    controller.subscribe("Event", EVENT)
    controller.clear_notifications()

    faults.apply("disk_fill", {"percent": 95})
    faults.wait_applied("disk_fill")

    notification = controller.wait_for_notification(
        lambda n: n["type"] == "event" and n.get("event_name") == "SpaceLow!", timeout=30)
    assert int(notification["params"]["UsedPercent"]) >= 90
    assert notification["params"]["Threshold"] == "90"
    assert notification["params"]["Path"] == "/data"

    faults.clear("disk_fill")
    deadline = time.time() + 15
    while time.time() < deadline:
        if int(controller.get_one(MON + "UsedPercent")) < 90:
            return
        time.sleep(1)
    pytest.fail("UsedPercent never dropped after the fault was cleared")


def test_threshold_is_a_persisted_setting(controller, plugin_booted, wait_for_agent):
    wait_for_agent(timeout=60)
    controller.set({MON + "Threshold": 70})
    assert controller.get_one(MON + "Threshold") == "70"
    controller.set({MON + "Threshold": 90})


def test_disk_monitor_threshold_from_hal(controller, plugin_booted, faults, hal, wait_for_agent):
    """A value that would come from hardware, driven from the lab instead."""
    wait_for_agent(timeout=60)
    controller.subscribe("Event", EVENT)
    controller.clear_notifications()

    hal.put("diskmon.threshold", "50")
    faults.apply("disk_fill", {"percent": 60})
    faults.wait_applied("disk_fill")

    notification = controller.wait_for_notification(
        lambda n: n["type"] == "event" and n.get("event_name") == "SpaceLow!", timeout=30)
    assert notification["params"]["Threshold"] == "50"
    assert 55 <= int(notification["params"]["UsedPercent"]) <= 70


def test_disk_monitor_survives_reboot_and_alarms_again(controller, plugin_booted, faults,
                                                       reboot_and_wait):
    """The thread starts on every boot; a still-full disk alarms again."""
    controller.subscribe("Event", EVENT, persistent=True)
    faults.apply("disk_fill", {"percent": 95})
    faults.wait_applied("disk_fill")
    controller.clear_notifications()

    reboot_and_wait()

    controller.wait_for_notification(
        lambda n: n["type"] == "event" and n.get("event_name") == "SpaceLow!", timeout=60)


def test_vendor_logic_is_absent_without_the_card(controller, plugin_booted, device_url,
                                                  wait_for_agent):
    """Reboot with the card ejected: the built-in image runs, your code does not.

    This is the mistake of updating a plug-in and forgetting the card. The
    platform must come up healthy (it is stock firmware) *and* make the absence
    unmistakable: no plug-ins in the boot report, and the vendor object gone
    from the data model. Runs last, since it leaves the card ejected.
    """
    from uspctl import UspError

    post(device_url, "/api/sdcard", {"action": "eject"})
    marker = system_state(device_url)["bootedFrom"].get("bootedAt", "")
    post(device_url, "/api/reboot", {"cause": "LocalReboot"})
    wait_for_agent(timeout=150, previous_boot=marker)

    booted = system_state(device_url)["bootedFrom"]
    assert booted["from"] == "internal"
    assert booted["cardSeated"] is False
    assert booted["plugins"] == []

    # Stock firmware is healthy...
    assert controller.get_one("Device.WiFi.SSID.1.SSID")
    # ...and the vendor object simply does not exist
    with pytest.raises(UspError):
        controller.get(MON)
