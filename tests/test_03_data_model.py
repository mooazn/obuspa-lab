"""The TR-181 data model: nested objects and cross-object behaviour.

The interesting assertions here are the ones that would need real hardware:
disabling a radio and watching clients fall off it, and a client appearing as
two objects in two different subtrees at once.
"""

from __future__ import annotations

import pytest

from uspctl import UspError

RADIO1 = "Device.WiFi.Radio.1"
RADIO2 = "Device.WiFi.Radio.2"
SSID1 = "Device.WiFi.SSID.1"
SSID2 = "Device.WiFi.SSID.2"
AP1 = "Device.WiFi.AccessPoint.1"
AP2 = "Device.WiFi.AccessPoint.2"
CLIENTS1 = f"{AP1}.AssociatedDeviceNumberOfEntries"


# ----------------------------------------------------------------------
# Breadth
# ----------------------------------------------------------------------


def test_whole_wifi_subtree_is_readable(controller):
    """A single Get spanning Device.WiFi. returns the whole tree."""
    values = controller.get("Device.WiFi.")

    assert f"{RADIO1}.Channel" in values
    assert f"{SSID1}.SSID" in values
    assert f"{AP1}.SSIDReference" in values


def test_two_radios_with_different_bands(controller):
    assert controller.get_one(f"{RADIO1}.OperatingFrequencyBand") == "2.4GHz"
    assert controller.get_one(f"{RADIO2}.OperatingFrequencyBand") == "5GHz"
    assert controller.get_one(f"{RADIO1}.Channel") == "6"
    assert controller.get_one(f"{RADIO2}.Channel") == "36"


def test_nested_object_is_readable(controller):
    """Device.IP.Interface.{i}.IPv4Address.{i} - two levels of instance."""
    assert controller.get_one("Device.IP.Interface.1.IPv4Address.1.IPAddress") == "100.64.1.23"
    assert controller.get_one("Device.IP.Interface.2.IPv4Address.1.IPAddress") == "192.168.1.1"


def test_nested_singleton_parameter(controller):
    """A parameter under a non-instance sub-object, e.g. AccessPoint.1.Security."""
    assert controller.get_one(f"{AP1}.Security.ModeEnabled") == "WPA2-Personal"


def test_channel_validation_is_enforced_by_the_device(controller):
    with pytest.raises(UspError):
        controller.set({f"{RADIO2}.Channel": 7000})

    assert controller.get_one(f"{RADIO2}.Channel") == "36"


def test_passphrase_validation(controller):
    with pytest.raises(UspError):
        controller.set({f"{AP1}.Security.KeyPassphrase": "short"})


# ----------------------------------------------------------------------
# Jagged instances
# ----------------------------------------------------------------------


def test_access_point_with_no_clients_reports_zero(controller):
    """An object with no children is a normal state, not a missing object."""
    assert controller.get_one(f"{AP2}.AssociatedDeviceNumberOfEntries") == "0"


def test_clients_are_counted_per_access_point(controller, attach_client_api):
    """One access point can have clients while its sibling has none."""
    attach_client_api(1, mac="02:00:5e:aa:00:01", hostname="pixel")
    attach_client_api(1, mac="02:00:5e:aa:00:02", hostname="laptop")

    assert controller.get_one(CLIENTS1) == "2"
    assert controller.get_one(f"{AP2}.AssociatedDeviceNumberOfEntries") == "0"


def test_client_is_visible_in_both_subtrees(controller, attach_client_api):
    """A client shows up as an AssociatedDevice and as a Host, like real kit."""
    mac = "02:00:5e:aa:00:07"
    instance = attach_client_api(1, mac=mac, hostname="tablet")

    assert controller.get_one(f"{AP1}.AssociatedDevice.{instance}.MACAddress") == mac

    hosts = controller.get("Device.Hosts.")
    assert mac in hosts.values(), "the client should also appear in Device.Hosts"


# ----------------------------------------------------------------------
# Cross-object behaviour
# ----------------------------------------------------------------------


def test_disabling_a_radio_takes_its_ssid_down(controller, restore_radios):
    assert controller.get_one(f"{SSID1}.Status") == "Up"

    controller.set({f"{RADIO1}.Enable": False})

    assert controller.get_one(f"{SSID1}.Status") == "Down"
    assert controller.get_one(f"{AP1}.Status") == "Disabled"


def test_disabling_a_radio_drops_its_clients(controller, attach_client_api,
                                             restore_radios):
    """The scenario that is genuinely annoying to arrange on real hardware."""
    attach_client_api(1, mac="02:00:5e:aa:00:11", hostname="phone")
    attach_client_api(1, mac="02:00:5e:aa:00:12", hostname="watch")
    assert controller.get_one(CLIENTS1) == "2"

    controller.set({f"{RADIO1}.Enable": False})

    assert controller.get_one(CLIENTS1) == "0"


def test_the_other_radio_is_unaffected(controller, restore_radios):
    """A cascade must stop at the boundary of what it actually affects."""
    controller.set({f"{RADIO1}.Enable": False})

    assert controller.get_one(f"{SSID2}.Status") == "Up"
    assert controller.get_one(f"{AP2}.Status") == "Enabled"


def test_disassociate_all_command(controller, attach_client_api):
    """A command that changes the shape of the tree, not just a value."""
    attach_client_api(1, mac="02:00:5e:aa:00:21", hostname="one")
    attach_client_api(1, mac="02:00:5e:aa:00:22", hostname="two")

    outputs = controller.operate(f"{AP1}.DisassociateAll()")

    assert outputs["Disassociated"] == "2"
    assert controller.get_one(CLIENTS1) == "0"


# ----------------------------------------------------------------------
# Object lifecycle over USP
# ----------------------------------------------------------------------


def test_controller_can_add_and_delete_an_ssid(controller):
    """Objects marked writable accept Add and Delete from a controller."""
    created = controller.add("Device.WiFi.SSID.", {"SSID": "GuestNetwork"})

    assert created.startswith("Device.WiFi.SSID.")
    assert controller.get_one(f"{created}SSID") == "GuestNetwork"

    controller.delete(created)

    with pytest.raises(UspError):
        controller.get(f"{created}SSID")


def test_controller_cannot_fabricate_a_client(controller):
    """Clients are runtime state: only the device may create them."""
    with pytest.raises(UspError):
        controller.add(f"{AP1}.AssociatedDevice.")


def test_client_arrival_is_pushed_as_object_creation(controller, attach_client_api):
    """A client joining reaches a subscribed controller without polling.

    This is the plug-in's event thread turning a device-side change into
    USP_SIGNAL_ObjectAdded, which is what makes the simulation useful for
    testing controller behaviour rather than just device state.
    """
    controller.subscribe(
        notif_type="ObjectCreation",
        reference_list=f"{AP1}.AssociatedDevice.",
    )
    controller.clear_notifications()

    attach_client_api(1, mac="02:00:5e:aa:00:31", hostname="arriving")

    notification = controller.wait_for_notification(
        lambda n: n["type"] == "obj_creation"
        and n.get("obj_path", "").startswith(f"{AP1}.AssociatedDevice."),
        timeout=20,
    )
    assert notification["obj_path"].startswith(f"{AP1}.AssociatedDevice.")
