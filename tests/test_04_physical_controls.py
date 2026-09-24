"""The physical device: SIM, reset button, factory reset, SD card.

Ordering is deliberate. A factory reset wipes the agent's database, taking every
subscription with it (a real controller would have to re-onboard), so it runs
after the tests that rely on subscriptions. Booting from the SD card swaps the
agent binary for a different obuspa release, so that goes last.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import requests

CELL = "Device.Cellular.Interface.1"
SDCARD = Path(__file__).parent.parent / "sdcard"


from conftest import post as _post, system_state as _system  # noqa: E402


# ----------------------------------------------------------------------
# SIM
# ----------------------------------------------------------------------


@pytest.fixture
def no_sim(device_url):
    """Starts and ends with the SIM out, whatever ran before or during."""
    def eject_if_present() -> None:
        if _system(device_url)["sim"]["inserted"]:
            _post(device_url, "/api/sim", {"action": "eject"})

    eject_if_present()
    yield
    eject_if_present()


def test_modem_without_sim_is_down(controller, device_url, no_sim):
    assert controller.get_one(f"{CELL}.USIM.Status") == "None"
    assert controller.get_one(f"{CELL}.Status") == "Down"
    assert controller.get("Device.Cellular.AccessPoint.") == {}


def test_inserting_sim_brings_modem_up(controller, device_url, no_sim):
    """Seating a SIM: USIM valid, modem registered, APN provisioned."""
    _post(device_url, "/api/sim", {"action": "insert", "iccid": "8944500000000000042",
                                   "carrier": "TestCarrier"})

    assert controller.get_one(f"{CELL}.USIM.Status") == "Valid"
    assert controller.get_one(f"{CELL}.USIM.ICCID") == "8944500000000000042"
    assert controller.get_one(f"{CELL}.NetworkInUse") == "TestCarrier"
    assert controller.get_one(f"{CELL}.Status") == "Up"
    assert controller.get_one("Device.Cellular.AccessPoint.1.APN") == "internet"


def test_sim_insertion_is_pushed_as_object_creation(controller, device_url, no_sim):
    """The APN appearing reaches a subscribed controller without polling."""
    controller.subscribe("ObjectCreation", "Device.Cellular.AccessPoint.")
    controller.clear_notifications()

    _post(device_url, "/api/sim", {"action": "insert"})

    controller.wait_for_notification(
        lambda n: n["type"] == "obj_creation"
        and n.get("obj_path", "").startswith("Device.Cellular.AccessPoint."),
        timeout=20,
    )


def test_ejecting_sim_takes_modem_down(controller, device_url, no_sim):
    _post(device_url, "/api/sim", {"action": "insert"})
    assert controller.get_one(f"{CELL}.Status") == "Up"

    _post(device_url, "/api/sim", {"action": "eject"})

    assert controller.get_one(f"{CELL}.Status") == "Down"
    assert controller.get("Device.Cellular.AccessPoint.") == {}


def test_sim_cannot_be_inserted_twice(device_url, no_sim):
    _post(device_url, "/api/sim", {"action": "insert"})
    response = requests.post(f"{device_url}/api/sim", json={"action": "insert"}, timeout=5)
    assert response.status_code == 409


# ----------------------------------------------------------------------
# The reset button
# ----------------------------------------------------------------------


def test_reset_button_really_restarts_the_agent(controller, device_url, boot_subscription,
                                                wait_for_agent, boot_marker):
    """A device-side reboot must take the agent down with it.

    The UI button does not go through USP at all: the device drops its socket,
    the plug-in notices its hardware has gone, and the agent exits. If this
    produces a Boot!, the physical reset button is doing what a real one does.
    """
    controller.clear_notifications()
    before = _system(device_url)["bootCount"]
    marker = boot_marker()

    _post(device_url, "/api/reboot", {"cause": "LocalReboot"})

    controller.wait_for_notification(
        lambda n: n["type"] == "event" and n.get("event_name") == "Boot!",
        timeout=90,
    )
    wait_for_agent(timeout=90, previous_boot=marker)

    after = _system(device_url)
    assert after["bootCount"] == before + 1
    assert after["rebootCause"] == "LocalReboot"


# ----------------------------------------------------------------------
# Factory reset - wipes the agent database, so no subscriptions after this
# ----------------------------------------------------------------------


def test_factory_reset_wipes_configuration(controller, device_url, wait_for_agent,
                                           boot_marker):
    """Hold the reset button: configuration gone on both the device and the agent."""
    controller.set({"Device.WiFi.SSID.1.SSID": "WillBeWiped"})
    _post(device_url, "/api/sim", {"action": "insert"})
    assert controller.get_one("Device.WiFi.SSID.1.SSID") == "WillBeWiped"

    marker = boot_marker()
    _post(device_url, "/api/factory-reset", {"cause": "FactoryReset"})
    wait_for_agent(timeout=120, previous_boot=marker)

    system = _system(device_url)
    assert system["rebootCause"] == "FactoryReset"
    assert system["bootCount"] == 1, "a factory reset starts the boot count over"

    # Device configuration is back to factory
    assert controller.get_one("Device.WiFi.SSID.1.SSID") == "VirtualGateway-001"

    # The SIM is hardware, not configuration: it stays in the slot
    assert controller.get_one(f"{CELL}.USIM.Status") == "Valid"
    _post(device_url, "/api/sim", {"action": "eject"})

    # The agent's database was recreated from its factory file too
    assert controller.get_one("Device.LocalAgent.EndpointID") == "os::vdev-001"


# ----------------------------------------------------------------------
# SD card - boots a different obuspa build, so this goes last
# ----------------------------------------------------------------------


@pytest.fixture
def flashed_card():
    manifest = SDCARD / "manifest.json"
    if not manifest.exists() or not (SDCARD / "obuspa").exists():
        pytest.skip("no image on the SD card - run `make flash REF=v10.0.0-master`")
    return json.loads(manifest.read_text())


def test_seating_card_without_image_is_rejected(device_url):
    if (SDCARD / "manifest.json").exists():
        pytest.skip("card has an image; the empty-slot case is not testable now")
    response = requests.post(f"{device_url}/api/sdcard", json={"action": "insert"}, timeout=5)
    assert response.status_code == 409


def test_card_is_visible_to_the_device(device_url, flashed_card):
    sdcard = _system(device_url)["sdcard"]
    assert sdcard["present"]
    assert sdcard["manifest"]["label"] == flashed_card["label"]


def test_booting_from_card_runs_the_flashed_agent(controller, device_url, flashed_card,
                                                  card_ejected, wait_for_agent, boot_marker):
    """The loop that used to need a card reader and a screwdriver.

    Seat the card, press reset, and the agent that comes back is the build on
    the card: the bootloader ran the binary it copied off the card, and the
    firmware reports the card's image as its software version. The obuspa
    release may be the same as the built-in one, so it proves nothing here.
    """
    builtin_image = controller.get_one("Device.DeviceInfo.SoftwareVersion")

    # Its own persistent Boot! subscription: the session-wide one does not
    # survive the factory reset test earlier in this module.
    boot_sub = controller.subscribe("Event", "Device.Boot!", persistent=True)

    _post(device_url, "/api/sdcard", {"action": "insert"})
    assert _system(device_url)["sdcard"]["inserted"]

    marker = boot_marker()
    _post(device_url, "/api/reboot", {"cause": "LocalReboot"})
    wait_for_agent(timeout=150, previous_boot=marker)

    booted = _system(device_url)["bootedFrom"]
    assert booted["from"] == "sdcard", booted
    assert booted["binary"] == "/tmp/sdboot/obuspa", booted
    assert booted["manifest"]["label"] == flashed_card["label"]

    card_image = controller.get_one("Device.DeviceInfo.SoftwareVersion")
    assert card_image == booted["softwareVersion"]
    assert card_image.startswith(flashed_card["label"])
    assert card_image != builtin_image

    # A different image than the last boot is a firmware update to obuspa
    boot = controller.wait_for_notification(
        lambda n: n["subscription_id"] == boot_sub and n.get("event_name") == "Boot!",
        timeout=30,
    )
    assert boot["params"]["FirmwareUpdated"] == "true", boot["params"]

    # The flashed build serves the same data model through the same plug-in
    assert controller.get_one("Device.WiFi.SSID.1.SSID") == "VirtualGateway-001"

    # Eject and reboot: the built-in image is back
    _post(device_url, "/api/sdcard", {"action": "eject"})
    marker = boot_marker()
    _post(device_url, "/api/reboot", {"cause": "LocalReboot"})
    wait_for_agent(timeout=120, previous_boot=marker)

    assert _system(device_url)["bootedFrom"]["from"] == "internal"
    assert controller.get_one("Device.DeviceInfo.SoftwareVersion") == builtin_image
