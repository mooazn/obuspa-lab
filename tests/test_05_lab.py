"""The lab around the firmware: observation, faults, WAN, HAL.

Nothing here flashes a card; the vendor-plug-in scenarios that do are in
test_06_vendor_plugin.py so they run last.
"""

from __future__ import annotations

import time

import pytest
import requests

from conftest import post, system_state


# ----------------------------------------------------------------------
# Serial console
# ----------------------------------------------------------------------


def test_console_captures_agent_boot(controller, console):
    """The agent container's output reaches the device as a console.

    Bootloader lines and the proxy plug-in's registration line both come from
    inside the agent container; seeing them here proves the tee and the tail.
    """
    lines = [entry["text"] for entry in console()]

    boots = [i for i, text in enumerate(lines) if text.startswith("entrypoint: ==== boot")]
    assert boots, f"no boot marker in {len(lines)} console lines"
    this_boot = lines[boots[-1]:]

    assert any("starting" in text and "obuspa" in text for text in this_boot
               if text.startswith("entrypoint:")), this_boot[:40]
    assert any("registered" in text and "proxied data model entries" in text
               for text in this_boot), this_boot[:40]


def test_console_is_incremental(controller, console):
    """`since` returns only newer lines, so a UI can poll without duplicates.

    The console never goes quiet (obuspa's protocol trace logs keepalives), so
    the property is "everything returned is newer", not "nothing is returned".
    """
    latest = requests.get("http://localhost:8080/api/console", timeout=5).json()["latest"]
    newer = console(since=latest)
    assert all(entry["seq"] > latest for entry in newer)
    assert all(entry["seq"] > latest for entry in console(since=latest))


def test_agent_reported_connected(controller, device_url):
    """The device knows the firmware is up: the plug-in holds its event stream."""
    agent = system_state(device_url)["agent"]
    assert agent["eventsConnected"] is True
    assert agent["since"] is not None


# ----------------------------------------------------------------------
# USP timeline
# ----------------------------------------------------------------------


def test_usp_timeline_records_request_and_response(controller, usp_timeline):
    """The tap sees both halves of a transaction, correlated by msg_id."""
    latest = requests.get("http://localhost:8080/api/usp/timeline", timeout=5).json()["latest"]

    controller.get_one("Device.WiFi.SSID.1.SSID")
    time.sleep(0.5)

    entries = usp_timeline(since=latest)
    gets = [e for e in entries if e["msg_type"] == "GET"]
    resps = [e for e in entries if e["msg_type"] == "GET_RESP"]
    assert gets and resps, [e["summary"] for e in entries]
    assert gets[-1]["msg_id"] == resps[-1]["msg_id"]
    assert gets[-1]["direction"] == "controller->agent"
    assert resps[-1]["direction"] == "agent->controller"
    assert "Device.WiFi.SSID.1.SSID" in gets[-1]["summary"]


def test_usp_notifications_list_shows_events(controller, attach_client_api, device_url):
    """A notification the agent emits appears in the tap's notification view."""
    controller.subscribe("ObjectCreation", "Device.WiFi.AccessPoint.1.AssociatedDevice.")
    latest = requests.get(f"{device_url}/api/usp/timeline", timeout=5).json()["latest"]

    attach_client_api(1, mac="02:00:5e:aa:05:01", hostname="tapped")

    deadline = time.time() + 15
    while time.time() < deadline:
        notes = requests.get(f"{device_url}/api/usp/notifications",
                             params={"since": latest}, timeout=5).json()["notifications"]
        if any("ObjectCreation" in n["summary"] for n in notes):
            return
        time.sleep(0.5)
    pytest.fail("ObjectCreation notification never appeared in the tap")


# ----------------------------------------------------------------------
# Environment faults
# ----------------------------------------------------------------------


def test_unknown_fault_kind_is_rejected(device_url):
    response = requests.post(f"{device_url}/api/faults",
                             json={"kind": "meteor", "params": {}}, timeout=5)
    assert response.status_code == 400


def test_fault_params_are_validated(device_url):
    response = requests.post(f"{device_url}/api/faults",
                             json={"kind": "disk_fill", "params": {"percent": 250}}, timeout=5)
    assert response.status_code == 400


def test_disk_fill_is_applied_and_persists_across_reboot(faults, reboot_and_wait):
    """The fault daemon fills the tmpfs, and fills it again after a reboot.

    Real hardware does not empty its flash on reboot; the tmpfs does, so the
    device re-applies the fault as the agent comes up.
    """
    faults.apply("disk_fill", {"percent": 95})
    fault = faults.wait_applied("disk_fill")
    assert fault["error"] is None
    assert "9" in str(fault["detail"]), fault       # "95% used" or thereabouts
    first_applied_at = fault["appliedAt"]

    reboot_and_wait()

    fault = faults.wait_applied("disk_fill", timeout=40)
    assert fault["appliedAt"] != first_applied_at, "should have been re-applied on the new boot"

    faults.clear("disk_fill")
    time.sleep(2)
    assert faults.get("disk_fill") is None


# ----------------------------------------------------------------------
# WAN
# ----------------------------------------------------------------------


def test_wan_latency_slows_usp_round_trips(controller, faults):
    """Latency through the relay is felt by the controller, both directions."""
    controller.get_one("Device.WiFi.SSID.1.SSID")        # warm
    started = time.time()
    controller.get_one("Device.WiFi.SSID.1.SSID")
    baseline = time.time() - started

    faults.apply("wan_latency", {"ms": 400})
    time.sleep(0.3)

    started = time.time()
    controller.get_one("Device.WiFi.SSID.1.SSID")
    slowed = time.time() - started

    assert slowed >= baseline + 0.6, f"baseline {baseline:.3f}s, with latency {slowed:.3f}s"


def test_wan_down_drops_the_agent_and_it_reconnects(controller, faults, wan_state, wait_for_agent):
    """Cutting the WAN really disconnects the agent; it comes back on its own."""
    from uspctl import UspTimeout

    faults.apply("wan_down")

    deadline = time.time() + 15
    while time.time() < deadline and wan_state()["connections"] > 0:
        time.sleep(0.5)
    state = wan_state()
    assert state["linkUp"] is False and state["connections"] == 0, state

    with pytest.raises(UspTimeout):
        controller.get_one("Device.WiFi.SSID.1.SSID")

    faults.clear("wan_down")
    wait_for_agent(timeout=90)
    assert wan_state()["linkUp"] is True


def test_disabling_wan_interface_cuts_the_broker(controller, device_url, wan_state, wait_for_agent):
    """Taking Device.IP.Interface.1 down over USP is a real link loss.

    The Set itself succeeds - the relay waits a moment before cutting the link
    so the response gets out - and then the agent is gone until the interface
    comes back (over HTTP here, since the controller can no longer reach it).
    """
    controller.set({"Device.IP.Interface.1.Enable": False})

    deadline = time.time() + 8
    while time.time() < deadline and wan_state()["connections"] > 0:
        time.sleep(0.5)
    assert wan_state()["connections"] == 0, wan_state()

    post(device_url, "/api/set", {"path": "Device.IP.Interface.1.Enable", "value": True})
    wait_for_agent(timeout=90)


# ----------------------------------------------------------------------
# Virtual HAL
# ----------------------------------------------------------------------


def test_hal_roundtrip_via_http(hal, device_url):
    assert hal.get("thermal.cpu") is None

    hal.put("thermal.cpu", "47")
    assert hal.get("thermal.cpu") == "47"
    assert system_state(device_url)["hal"]["thermal.cpu"] == "47"

    hal.put("thermal.cpu", "95")
    assert hal.get("thermal.cpu") == "95"

    assert hal.delete("thermal.cpu") == 200
    assert hal.get("thermal.cpu") is None
    assert hal.delete("thermal.cpu") == 404


def test_hal_survives_a_reboot(hal, reboot_and_wait):
    """Hardware state outlives a firmware restart."""
    hal.put("board.revision", "B2")
    reboot_and_wait()
    assert hal.get("board.revision") == "B2"
