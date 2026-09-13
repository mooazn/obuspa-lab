"""The basic loop: a parameter served by the device, read and written over USP.

Controller -> MQTT -> obuspa -> proxy plug-in -> Python device, and back.

Everything here goes over real USP: protobuf records on MQTT to a real
OB-USP-AGENT. If these pass, the architecture works and everything after this
is adding data model and behaviour rather than plumbing.
"""

from __future__ import annotations

import pytest
import requests

from uspctl import UspError

SSID_PATH = "Device.WiFi.SSID.1.SSID"
ENABLE_PATH = "Device.WiFi.SSID.1.Enable"
STATUS_PATH = "Device.WiFi.SSID.1.Status"
NAME_PATH = "Device.WiFi.SSID.1.Name"


def test_agent_is_reachable(controller):
    """The agent answers USP at all, from its own built-in data model."""
    endpoint_id = controller.get_one("Device.LocalAgent.EndpointID")
    assert endpoint_id == "os::vdev-001"


def test_proxied_parameter_is_readable(controller):
    """A parameter served by the Python device reaches the controller."""
    value = controller.get_one(SSID_PATH)
    assert value  # whatever it is, the plug-in resolved it through to the device


def test_set_then_get_round_trip(controller, restore_ssid):
    """The loop: Set a proxied parameter, read it back."""
    controller.set({SSID_PATH: "RoundTrip"})
    assert controller.get_one(SSID_PATH) == "RoundTrip"


def test_derived_parameter_follows_stored_one(controller, restore_ssid):
    """Status is computed by the device, not stored - the device has behaviour."""
    controller.set({ENABLE_PATH: True})
    assert controller.get_one(STATUS_PATH) == "Up"

    controller.set({ENABLE_PATH: False})
    assert controller.get_one(STATUS_PATH) == "Down"


def test_read_only_parameter_is_rejected(controller):
    """The agent enforces the access the device declared."""
    with pytest.raises(UspError):
        controller.set({STATUS_PATH: "Up"})


def test_device_validation_surfaces_as_usp_error(controller, restore_ssid):
    """A device-side rule failure comes back as a USP error, not a silent no-op."""
    too_long = "x" * 33
    with pytest.raises(UspError):
        controller.set({SSID_PATH: too_long})

    # and the rejected write did not take effect
    assert controller.get_one(SSID_PATH) != too_long


def test_get_multiple_parameters(controller):
    """A single Get spanning the object returns every parameter in it."""
    values = controller.get("Device.WiFi.SSID.1.")
    assert {NAME_PATH, SSID_PATH, ENABLE_PATH, STATUS_PATH} <= set(values)


def test_ui_and_controller_see_one_device(controller, device_url, ui_row, restore_ssid):
    """The web UI and the controller are views onto the same state.

    This is the whole point of the architecture: the browser is not a separate
    mock, it is the device the controller is managing.
    """
    controller.set({SSID_PATH: "SetByController"})

    assert ui_row("Device.WiFi.SSID.1")["SSID"] == "SetByController"

    # and the other direction: a UI write is what the next Get returns
    requests.post(
        f"{device_url}/api/set",
        json={"path": SSID_PATH, "value": "SetByUI"},
        timeout=5,
    ).raise_for_status()

    assert controller.get_one(SSID_PATH) == "SetByUI"
