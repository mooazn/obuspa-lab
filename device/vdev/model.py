"""Declarative description of the simulated device's data model.

This module is the single source of truth for what the device exposes and how
it behaves. The C plug-in asks for it at startup and registers whatever it
finds here, so growing the data model is a pure-Python change - no recompiling,
no touching obuspa.

Object paths may contain several instance placeholders, e.g.
Device.WiFi.AccessPoint.{i}.AssociatedDevice.{i}. Objects must be listed
parent-first, because that is the order they get registered in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# Types are the names the C plug-in maps onto obuspa's DM_ flags.
TYPE_STRING = "string"
TYPE_BOOL = "bool"
TYPE_UINT = "uint"
TYPE_INT = "int"
TYPE_ULONG = "ulong"
TYPE_DATETIME = "datetime"


@dataclass
class ParamDef:
    """One parameter within an object."""

    name: str
    type: str = TYPE_STRING
    writable: bool = False
    default: Any = ""

    # If set, the value is computed rather than stored. Called as
    # derived(values, device, instances) so it can look at the rest of the
    # device, which is what makes cross-object behaviour possible.
    derived: Optional[Callable[[dict, Any, tuple], Any]] = None

    # If set, called with the candidate native value before a write is
    # accepted. Raise ValueError to reject it; the message is returned to the
    # controller as the USP error.
    validate: Optional[Callable[[Any], None]] = None

    # If set, called as on_change(device, instances, value) after a successful
    # write. This is where a change to one object ripples out to others.
    on_change: Optional[Callable[[Any, tuple, Any], None]] = None

    # Volatile parameters are reset to their default by a reboot, the way
    # runtime state on real hardware is.
    persistent: bool = True

    @property
    def is_writable(self) -> bool:
        return self.writable and self.derived is None


@dataclass
class ObjectDef:
    """A multi-instance object, possibly nested inside another."""

    # Schema path, e.g. "Device.WiFi.AccessPoint.{i}.AssociatedDevice.{i}"
    path: str

    # Whether a controller may Add/Delete instances
    writable: bool = False

    params: list[ParamDef] = field(default_factory=list)

    @property
    def depth(self) -> int:
        """How many instance numbers identify one row of this object."""
        return self.path.count("{i}")

    @property
    def add_path(self) -> str:
        """The path an Add targets: everything before the final placeholder."""
        return self.path[: -len(".{i}")]

    def instantiate(self, instances: tuple[int, ...]) -> str:
        """Fills the placeholders in, giving Device.WiFi.SSID.1 and the like."""
        out = self.path
        for number in instances:
            out = out.replace("{i}", str(number), 1)
        return out

    def param(self, name: str) -> Optional[ParamDef]:
        for p in self.params:
            if p.name == name:
                return p
        return None


@dataclass
class CommandDef:
    """A USP command.

    Async commands return to the controller immediately and report completion
    later, which is how anything with a duration must behave.
    """

    path: str
    handler: Callable[..., dict]
    is_async: bool = False
    input_args: list[str] = field(default_factory=list)
    output_args: list[str] = field(default_factory=list)
    max_concurrency: int = 1


@dataclass
class EventDef:
    """A USP event the device can emit, e.g. Device.WiFi.SSID.1.Something!"""

    path: str
    args: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


def _validate_ssid(value: str) -> None:
    """SSIDs are 1-32 octets. Real radios reject anything else, so we do too."""
    if not 1 <= len(value.encode("utf-8")) <= 32:
        raise ValueError("SSID must be between 1 and 32 octets")


def _validate_channel(value: int) -> None:
    """Only channels that exist in one of the two bands."""
    two_ghz = set(range(1, 15))
    five_ghz = {36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112,
                116, 120, 124, 128, 132, 136, 140, 149, 153, 157, 161, 165}
    if value not in two_ghz | five_ghz:
        raise ValueError(f"{value} is not a valid WiFi channel")


def _validate_passphrase(value: str) -> None:
    if not 8 <= len(value) <= 63:
        raise ValueError("passphrase must be between 8 and 63 characters")


# ----------------------------------------------------------------------
# Derived values - where the device stops being a dictionary
# ----------------------------------------------------------------------


def _radio_status(values, device, instances) -> str:
    return "Up" if values.get("Enable") else "Down"


def _ssid_status(values, device, instances) -> str:
    """An SSID is only up if its radio is up underneath it."""
    if not values.get("Enable"):
        return "Down"
    lower = values.get("LowerLayers") or ""
    if lower and device.safe_get(f"{lower}.Enable") is True:
        return "Up"
    return "Down"


def _accesspoint_status(values, device, instances) -> str:
    """An access point follows the SSID it is bound to."""
    if not values.get("Enable"):
        return "Disabled"
    reference = values.get("SSIDReference") or ""
    if reference and device.safe_get(f"{reference}.Status") == "Up":
        return "Enabled"
    return "Disabled"


def _associated_device_count(values, device, instances) -> int:
    return len(device.child_instances("Device.WiFi.AccessPoint.{i}.AssociatedDevice.{i}",
                                      instances))


def _cellular_status(values, device, instances) -> str:
    """No SIM, no service - regardless of Enable."""
    if not values.get("Enable"):
        return "Down"
    return "Up" if values.get("USIM.Status") == "Valid" else "Down"


def _interface_status(values, device, instances) -> str:
    return "Up" if values.get("Enable") else "Down"


FIREWALL_TARGETS = ("Accept", "Drop", "Reject")


def _validate_target(value: str) -> None:
    if value not in FIREWALL_TARGETS:
        raise ValueError(f"Target must be one of {', '.join(FIREWALL_TARGETS)}")


def _validate_port(value: int) -> None:
    if not -1 <= value <= 65535:
        raise ValueError("port must be -1 (any) or 0..65535")


def _blocked_by_firewall(device, mac: str = "", ip: str = "") -> bool:
    """Whether an enabled Drop/Reject rule in an enabled chain matches a host.

    A rule matches on whichever of SourceMAC / SourceIP it specifies; an
    empty field is a wildcard. This is the cause-to-effect link firmware
    relies on: it writes the rule, the device derives the consequence.
    """
    mac = (mac or "").lower()
    for chain in device.child_instances("Device.Firewall.Chain.{i}", ()):
        if not device.safe_get(f"Device.Firewall.Chain.{chain}.Enable"):
            continue
        for rule in device.child_instances("Device.Firewall.Chain.{i}.Rule.{i}", (chain,)):
            prefix = f"Device.Firewall.Chain.{chain}.Rule.{rule}"
            if not device.safe_get(f"{prefix}.Enable"):
                continue
            if device.safe_get(f"{prefix}.Target") not in ("Drop", "Reject"):
                continue
            rule_mac = (device.safe_get(f"{prefix}.SourceMAC") or "").lower()
            rule_ip = device.safe_get(f"{prefix}.SourceIP") or ""
            if not rule_mac and not rule_ip:
                continue                    # a rule that names no source matches nothing here
            if rule_mac and rule_mac != mac:
                continue
            if rule_ip and rule_ip != ip:
                continue
            return True
    return False


def _host_active(values, device, instances) -> bool:
    """A host is active unless its interface is down or the firewall drops it."""
    layer1 = values.get("Layer1Interface") or ""
    if layer1 and device.safe_get(f"{layer1}.Status") not in ("Up", "Enabled"):
        return False
    return not _blocked_by_firewall(device, values.get("PhysAddress", ""),
                                    values.get("IPAddress", ""))


def _client_active(values, device, instances) -> bool:
    """An associated client is active unless the firewall drops its MAC."""
    return not _blocked_by_firewall(device, values.get("MACAddress", ""))


def _rule_status(values, device, instances) -> str:
    return "Enabled" if values.get("Enable") else "Disabled"


def _rule_count(values, device, instances) -> int:
    return len(device.child_instances("Device.Firewall.Chain.{i}.Rule.{i}", instances))


# ----------------------------------------------------------------------
# Behaviour - what a change to one object does to the rest
# ----------------------------------------------------------------------


def _radio_enable_changed(device, instances, value) -> None:
    """Disabling a radio kicks every client associated through it.

    This is the behaviour that makes the simulation worth having: a controller
    that disables a radio should see the client count fall to zero, exactly as
    it would against real hardware.
    """
    if value:
        return

    radio_path = f"Device.WiFi.Radio.{instances[0]}"

    for ap in device.child_instances("Device.WiFi.AccessPoint.{i}", ()):
        reference = device.safe_get(f"Device.WiFi.AccessPoint.{ap}.SSIDReference") or ""
        if not reference:
            continue
        if device.safe_get(f"{reference}.LowerLayers") != radio_path:
            continue
        for client in device.child_instances(
            "Device.WiFi.AccessPoint.{i}.AssociatedDevice.{i}", (ap,)
        ):
            detach_client(device, ap, client)


# ----------------------------------------------------------------------
# The data model
# ----------------------------------------------------------------------

MODEL: list[ObjectDef] = [
    # -- Ethernet -----------------------------------------------------
    ObjectDef(
        path="Device.Ethernet.Interface.{i}",
        params=[
            ParamDef("Enable", TYPE_BOOL, writable=True, default=True),
            ParamDef("Status", derived=_interface_status),
            ParamDef("Name", default="eth0"),
            ParamDef("MACAddress", default="02:00:5e:00:00:01"),
            ParamDef("MaxBitRate", TYPE_INT, writable=True, default=1000),
            ParamDef("DuplexMode", writable=True, default="Full"),
        ],
    ),

    # -- IP -----------------------------------------------------------
    ObjectDef(
        path="Device.IP.Interface.{i}",
        params=[
            ParamDef("Enable", TYPE_BOOL, writable=True, default=True),
            ParamDef("Status", derived=_interface_status),
            ParamDef("Name", default="wan"),
            ParamDef("Type", default="Normal"),
            ParamDef("LowerLayers", writable=True, default=""),
        ],
    ),
    ObjectDef(
        path="Device.IP.Interface.{i}.IPv4Address.{i}",
        writable=True,
        params=[
            ParamDef("Enable", TYPE_BOOL, writable=True, default=True),
            ParamDef("Status", default="Enabled"),
            ParamDef("IPAddress", writable=True, default="0.0.0.0"),
            ParamDef("SubnetMask", writable=True, default="255.255.255.0"),
            ParamDef("AddressingType", default="DHCP"),
        ],
    ),

    # -- WiFi ---------------------------------------------------------
    ObjectDef(
        path="Device.WiFi.Radio.{i}",
        params=[
            ParamDef("Enable", TYPE_BOOL, writable=True, default=True,
                     on_change=_radio_enable_changed),
            ParamDef("Status", derived=_radio_status),
            ParamDef("Name", default="wl0"),
            ParamDef("OperatingFrequencyBand", default="2.4GHz"),
            ParamDef("SupportedFrequencyBands", default="2.4GHz"),
            ParamDef("Channel", TYPE_UINT, writable=True, default=6,
                     validate=_validate_channel),
            ParamDef("AutoChannelEnable", TYPE_BOOL, writable=True, default=False),
            ParamDef("OperatingChannelBandwidth", writable=True, default="20MHz"),
            ParamDef("TransmitPower", TYPE_INT, writable=True, default=100),
            # Noise is runtime state: a reboot forgets it
            ParamDef("Noise", TYPE_INT, default=-92, persistent=False),
        ],
    ),
    ObjectDef(
        path="Device.WiFi.SSID.{i}",
        writable=True,
        params=[
            ParamDef("Enable", TYPE_BOOL, writable=True, default=True),
            ParamDef("Status", derived=_ssid_status),
            ParamDef("Name", default="wl0"),
            ParamDef("SSID", writable=True, default="VirtualGateway-001",
                     validate=_validate_ssid),
            ParamDef("BSSID", default="02:00:5e:00:00:10"),
            ParamDef("LowerLayers", writable=True, default="Device.WiFi.Radio.1"),
        ],
    ),
    ObjectDef(
        path="Device.WiFi.AccessPoint.{i}",
        writable=True,
        params=[
            ParamDef("Enable", TYPE_BOOL, writable=True, default=True),
            ParamDef("Status", derived=_accesspoint_status),
            ParamDef("SSIDReference", writable=True, default="Device.WiFi.SSID.1"),
            ParamDef("SSIDAdvertisementEnabled", TYPE_BOOL, writable=True, default=True),
            ParamDef("AssociatedDeviceNumberOfEntries", TYPE_UINT,
                     derived=_associated_device_count),
            ParamDef("Security.ModeEnabled", writable=True, default="WPA2-Personal"),
            ParamDef("Security.KeyPassphrase", writable=True, default="changeme123",
                     validate=_validate_passphrase),
        ],
    ),
    ObjectDef(
        path="Device.WiFi.AccessPoint.{i}.AssociatedDevice.{i}",
        params=[
            # Clients are runtime state - they do not survive a reboot
            ParamDef("MACAddress", default="", persistent=False),
            ParamDef("OperatingStandard", default="ax", persistent=False),
            ParamDef("SignalStrength", TYPE_INT, default=-50, persistent=False),
            ParamDef("LastDataDownlinkRate", TYPE_UINT, default=780000,
                     persistent=False),
            ParamDef("LastDataUplinkRate", TYPE_UINT, default=650000,
                     persistent=False),
            ParamDef("Active", TYPE_BOOL, derived=_client_active),
        ],
    ),

    # -- Hosts --------------------------------------------------------
    ObjectDef(
        path="Device.Hosts.Host.{i}",
        params=[
            ParamDef("PhysAddress", default="", persistent=False),
            ParamDef("IPAddress", default="", persistent=False),
            ParamDef("HostName", default="", persistent=False),
            ParamDef("InterfaceType", default="802.11", persistent=False),
            ParamDef("Layer1Interface", default="", persistent=False),
            ParamDef("Active", TYPE_BOOL, default=True, derived=_host_active),
        ],
    ),

    # -- Firewall -----------------------------------------------------
    # The subset of TR-181 Device.Firewall that gives a rule an effect: a
    # Drop/Reject rule naming a client's MAC or IP makes that client inactive
    # in Hosts and AssociatedDevice. Vendor logic acts on the hardware by
    # writing rules here through obuspa's data model API.
    ObjectDef(
        path="Device.Firewall.Chain.{i}",
        params=[
            ParamDef("Enable", TYPE_BOOL, writable=True, default=True),
            ParamDef("Name", default="LAN"),
            ParamDef("RuleNumberOfEntries", TYPE_UINT, derived=_rule_count),
        ],
    ),
    ObjectDef(
        path="Device.Firewall.Chain.{i}.Rule.{i}",
        writable=True,
        params=[
            ParamDef("Enable", TYPE_BOOL, writable=True, default=True),
            ParamDef("Status", derived=_rule_status),
            ParamDef("Order", TYPE_UINT, writable=True, default=1),
            ParamDef("Description", writable=True, default=""),
            ParamDef("Target", writable=True, default="Drop", validate=_validate_target),
            ParamDef("Protocol", TYPE_INT, writable=True, default=-1),
            ParamDef("SourceMAC", writable=True, default=""),
            ParamDef("SourceIP", writable=True, default=""),
            ParamDef("DestIP", writable=True, default=""),
            ParamDef("DestPort", TYPE_INT, writable=True, default=-1,
                     validate=_validate_port),
        ],
    ),

    # -- Cellular -----------------------------------------------------
    # The modem is always present; the SIM is the removable part. Seating a
    # SIM makes the USIM valid and provisions an APN profile, which is what
    # the carrier does - and which a subscribed controller sees appear.
    ObjectDef(
        path="Device.Cellular.Interface.{i}",
        params=[
            ParamDef("Enable", TYPE_BOOL, writable=True, default=True),
            ParamDef("Status", derived=_cellular_status),
            ParamDef("Name", default="wwan0"),
            ParamDef("IMEI", default="356938035643809"),
            ParamDef("CurrentAccessTechnology", default="", persistent=False),
            ParamDef("NetworkInUse", default="", persistent=False),
            ParamDef("RSSI", TYPE_INT, default=-113, persistent=False),
            ParamDef("USIM.Status", default="None"),
            ParamDef("USIM.IMSI", default=""),
            ParamDef("USIM.ICCID", default=""),
            ParamDef("USIM.MSISDN", default=""),
        ],
    ),
    ObjectDef(
        path="Device.Cellular.AccessPoint.{i}",
        params=[
            ParamDef("Enable", TYPE_BOOL, writable=True, default=True),
            ParamDef("Alias", default="cpe-apn-1"),
            ParamDef("APN", writable=True, default=""),
            ParamDef("Interface", default="Device.Cellular.Interface.1"),
        ],
    ),
]


#: Instances present at factory reset, keyed by schema path. Nested objects use
#: a tuple of the parent instance numbers.
FACTORY_INSTANCES: dict[str, list[tuple[tuple[int, ...], dict[str, Any]]]] = {
    "Device.Ethernet.Interface.{i}": [
        ((1,), {"Name": "eth0", "MACAddress": "02:00:5e:00:00:01"}),
        ((2,), {"Name": "eth1", "MACAddress": "02:00:5e:00:00:02"}),
    ],
    "Device.IP.Interface.{i}": [
        ((1,), {"Name": "wan", "LowerLayers": "Device.Ethernet.Interface.1"}),
        ((2,), {"Name": "lan", "LowerLayers": "Device.Ethernet.Interface.2"}),
    ],
    "Device.IP.Interface.{i}.IPv4Address.{i}": [
        ((1, 1), {"IPAddress": "100.64.1.23", "SubnetMask": "255.255.255.0",
                  "AddressingType": "DHCP"}),
        ((2, 1), {"IPAddress": "192.168.1.1", "SubnetMask": "255.255.255.0",
                  "AddressingType": "Static"}),
    ],
    "Device.WiFi.Radio.{i}": [
        ((1,), {"Name": "wl0", "OperatingFrequencyBand": "2.4GHz",
                "SupportedFrequencyBands": "2.4GHz", "Channel": 6,
                "OperatingChannelBandwidth": "20MHz", "Noise": -92}),
        ((2,), {"Name": "wl1", "OperatingFrequencyBand": "5GHz",
                "SupportedFrequencyBands": "5GHz", "Channel": 36,
                "OperatingChannelBandwidth": "80MHz", "Noise": -96}),
    ],
    "Device.WiFi.SSID.{i}": [
        ((1,), {"Name": "wl0", "SSID": "VirtualGateway-001",
                "BSSID": "02:00:5e:00:00:10",
                "LowerLayers": "Device.WiFi.Radio.1"}),
        ((2,), {"Name": "wl1", "SSID": "VirtualGateway-001-5G",
                "BSSID": "02:00:5e:00:00:11",
                "LowerLayers": "Device.WiFi.Radio.2"}),
    ],
    "Device.WiFi.AccessPoint.{i}": [
        ((1,), {"SSIDReference": "Device.WiFi.SSID.1"}),
        ((2,), {"SSIDReference": "Device.WiFi.SSID.2"}),
    ],
    "Device.Firewall.Chain.{i}": [
        ((1,), {"Name": "LAN", "Enable": True}),
    ],
    "Device.Cellular.Interface.{i}": [
        ((1,), {"Name": "wwan0", "IMEI": "356938035643809"}),
    ],
    "Device.Hosts.Host.{i}": [
        ((1,), {"PhysAddress": "02:00:5e:00:aa:01", "IPAddress": "192.168.1.10",
                "HostName": "wired-desktop", "InterfaceType": "Ethernet",
                "Layer1Interface": "Device.Ethernet.Interface.2"}),
    ],
}


# ----------------------------------------------------------------------
# Clients coming and going
# ----------------------------------------------------------------------


def attach_client(device, access_point: int, mac: str, hostname: str,
                  ip: str = "", signal: int = -50) -> int:
    """Associates a client with an access point.

    Creates both the AssociatedDevice row and the matching Hosts entry, which
    is what a real gateway does, and signals both to the agent so a subscribed
    controller sees the ObjectCreation.
    """
    ap_path = f"Device.WiFi.AccessPoint.{access_point}"
    reference = device.safe_get(f"{ap_path}.SSIDReference") or ""

    instance = device.add(f"{ap_path}.AssociatedDevice", internal=True)
    device.set_many(
        {
            f"{ap_path}.AssociatedDevice.{instance}.MACAddress": mac,
            f"{ap_path}.AssociatedDevice.{instance}.SignalStrength": signal,
        },
        internal=True,
    )

    host = device.add("Device.Hosts.Host", internal=True)
    device.set_many(
        {
            f"Device.Hosts.Host.{host}.PhysAddress": mac,
            f"Device.Hosts.Host.{host}.HostName": hostname,
            f"Device.Hosts.Host.{host}.IPAddress": ip or _next_lan_address(device),
            f"Device.Hosts.Host.{host}.InterfaceType": "802.11",
            f"Device.Hosts.Host.{host}.Layer1Interface": reference,
        },
        internal=True,
    )

    return instance


def detach_client(device, access_point: int, instance: int) -> None:
    """Disassociates a client, removing its Hosts entry too."""
    ap_path = f"Device.WiFi.AccessPoint.{access_point}"
    mac = device.safe_get(f"{ap_path}.AssociatedDevice.{instance}.MACAddress")

    device.delete(f"{ap_path}.AssociatedDevice.{instance}", internal=True)

    if mac:
        for host in device.child_instances("Device.Hosts.Host.{i}", ()):
            if device.safe_get(f"Device.Hosts.Host.{host}.PhysAddress") == mac:
                device.delete(f"Device.Hosts.Host.{host}", internal=True)
                break


def insert_sim(device, iccid: str = "8944500000000000001",
               imsi: str = "234500000000001", carrier: str = "VirtualCell",
               apn: str = "internet") -> None:
    """Seats a SIM: the USIM becomes valid, the modem registers, an APN appears."""
    device.set_many(
        {
            "Device.Cellular.Interface.1.USIM.Status": "Valid",
            "Device.Cellular.Interface.1.USIM.ICCID": iccid,
            "Device.Cellular.Interface.1.USIM.IMSI": imsi,
            "Device.Cellular.Interface.1.USIM.MSISDN": "",
            "Device.Cellular.Interface.1.CurrentAccessTechnology": "LTE",
            "Device.Cellular.Interface.1.NetworkInUse": carrier,
            "Device.Cellular.Interface.1.RSSI": -67,
        },
        internal=True,
    )
    if not device.child_instances("Device.Cellular.AccessPoint.{i}", ()):
        instance = device.add("Device.Cellular.AccessPoint", internal=True)
        device.set_many(
            {
                f"Device.Cellular.AccessPoint.{instance}.APN": apn,
                f"Device.Cellular.AccessPoint.{instance}.Alias": f"cpe-apn-{instance}",
            },
            internal=True,
        )


def eject_sim(device) -> None:
    """Pulls the SIM: the modem drops off the network and the APN goes with it."""
    device.set_many(
        {
            "Device.Cellular.Interface.1.USIM.Status": "None",
            "Device.Cellular.Interface.1.USIM.ICCID": "",
            "Device.Cellular.Interface.1.USIM.IMSI": "",
            "Device.Cellular.Interface.1.USIM.MSISDN": "",
            "Device.Cellular.Interface.1.CurrentAccessTechnology": "",
            "Device.Cellular.Interface.1.NetworkInUse": "",
            "Device.Cellular.Interface.1.RSSI": -113,
        },
        internal=True,
    )
    for instance in device.child_instances("Device.Cellular.AccessPoint.{i}", ()):
        device.delete(f"Device.Cellular.AccessPoint.{instance}", internal=True)


def sim_inserted(device) -> bool:
    return device.safe_get("Device.Cellular.Interface.1.USIM.Status") == "Valid"


def _next_lan_address(device) -> str:
    """Picks the next free address in the LAN pool."""
    taken = set()
    for host in device.child_instances("Device.Hosts.Host.{i}", ()):
        address = device.safe_get(f"Device.Hosts.Host.{host}.IPAddress") or ""
        if address.startswith("192.168.1."):
            try:
                taken.add(int(address.rsplit(".", 1)[1]))
            except ValueError:
                pass

    for candidate in range(20, 255):
        if candidate not in taken:
            return f"192.168.1.{candidate}"
    return "192.168.1.254"


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------


def _cmd_ssid_reset(device, inputs: dict) -> dict:
    """Device.WiFi.SSID.{i}.Reset() - back to the factory SSID."""
    instance = inputs["__instances__"][0]
    factory = dict(FACTORY_INSTANCES["Device.WiFi.SSID.{i}"][instance - 1][1])
    device.set_many(
        {
            f"Device.WiFi.SSID.{instance}.SSID": factory["SSID"],
            f"Device.WiFi.SSID.{instance}.Enable": True,
        }
    )
    return {"Status": "Reset"}


def _cmd_ap_disassociate(device, inputs: dict) -> dict:
    """Device.WiFi.AccessPoint.{i}.DisassociateAll() - kick every client."""
    access_point = inputs["__instances__"][0]
    clients = device.child_instances(
        "Device.WiFi.AccessPoint.{i}.AssociatedDevice.{i}", (access_point,)
    )
    for client in clients:
        detach_client(device, access_point, client)
    return {"Disassociated": str(len(clients))}


def _cmd_ip_ping(device, inputs: dict, job) -> dict:
    """Device.IP.Diagnostics.IPPing() - a real TR-181 asynchronous command."""
    host = inputs.get("Host", "")
    if not host:
        raise ValueError("Host is required")

    repetitions = int(inputs.get("NumberOfRepetitions", 3))
    interval_ms = int(inputs.get("Timeout", 300))

    if not 1 <= repetitions <= 100:
        raise ValueError("NumberOfRepetitions must be between 1 and 100")

    job.status("Requested")

    success = 0
    total_ms = 0
    for attempt in range(repetitions):
        job.sleep(interval_ms / 1000.0)
        # A simulated network: reachable hosts answer, and one canned address
        # never does, so failure paths are testable.
        if host != "203.0.113.1":
            success += 1
            total_ms += 12 + attempt
        job.status(f"Sent {attempt + 1}/{repetitions}")

    return {
        "SuccessCount": str(success),
        "FailureCount": str(repetitions - success),
        "AverageResponseTime": str(total_ms // success if success else 0),
    }


COMMANDS: list[CommandDef] = [
    CommandDef(
        path="Device.WiFi.SSID.{i}.Reset()",
        handler=_cmd_ssid_reset,
        output_args=["Status"],
    ),
    CommandDef(
        path="Device.WiFi.AccessPoint.{i}.DisassociateAll()",
        handler=_cmd_ap_disassociate,
        output_args=["Disassociated"],
    ),
    CommandDef(
        path="Device.IP.Diagnostics.IPPing()",
        handler=_cmd_ip_ping,
        is_async=True,
        input_args=["Host", "NumberOfRepetitions", "Timeout"],
        output_args=["SuccessCount", "FailureCount", "AverageResponseTime"],
        max_concurrency=4,
    ),
]


#: Events the device emits. Device.Boot! is not here - obuspa implements and
#: emits that itself when the agent starts.
EVENTS: list[EventDef] = []
