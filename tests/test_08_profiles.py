"""Device profiles: the board described as a file (device/profiles/*.toml).

The running stack is the reference board; the suite's other tests are what
prove that profile reproduces it. These check the profiles that ship, and
that a profile with a mistake is refused with a message naming the mistake.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "device"))

from vdev import profile as profiles  # noqa: E402

SHIPPED = sorted((ROOT / "device" / "profiles").glob("*.toml"))


def test_the_running_device_is_the_reference_board(device_url):
    board = requests.get(f"{device_url}/api/state", timeout=5).json()["system"]["profile"]
    assert board["name"] == "reference-gateway"
    assert {"WiFi", "Cellular", "Ethernet", "IP"} <= set(board["areas"])


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_shipped_profiles_load(path):
    board = profiles.load(str(path))
    assert board.name == path.stem
    assert board.objects and board.factory


def test_a_board_has_only_the_parts_its_profile_names():
    wired = profiles.load("wired-router")
    areas = {o.path.split(".")[1] for o in wired.objects}
    assert areas == {"Ethernet", "IP", "Hosts", "Firewall"}
    assert len(wired.factory["Device.Ethernet.Interface.{i}"]) == 5
    assert [c.path for c in wired.commands] == ["Device.IP.Diagnostics.IPPing()"]


def test_child_instances_are_numbered_under_their_parent():
    board = profiles.load("reference-gateway")
    addresses = dict(board.factory["Device.IP.Interface.{i}.IPv4Address.{i}"])
    assert addresses[(1, 1)]["IPAddress"] == "100.64.1.23"
    assert addresses[(2, 1)]["IPAddress"] == "192.168.1.1"


def _parse(text: str):
    return profiles.parse(tomllib.loads('[board]\nname = "t"\n' + text), source="t.toml")


@pytest.mark.parametrize("text, expected", [
    ('[[WiFi.Antenna]]\nName = "a"', "not an object the lab simulates"),
    ("[[Toaster.Slot]]\nCount = 2", "not an area the lab simulates"),
    ('[[WiFi.Radio]]\nColour = "blue"', "not a parameter of Device.WiFi.Radio"),
    ('[[WiFi.Radio]]\nStatus = "Up"', "computed by the device"),
    ('[[WiFi.Radio]]\nChannel = "six"', "is not a valid uint"),
    ("[[WiFi.Radio]]\nChannel = 999", "not a valid WiFi channel"),
    ('[[WiFi.SSID]]\nLowerLayers = "Device.WiFi.Radio.3"', "does not define"),
    ('[WiFi]\nRadio = 1', "outside any instance"),
])
def test_mistakes_are_named(text, expected):
    with pytest.raises(profiles.ProfileError, match=expected):
        _parse(text)


def test_board_roles_must_name_a_defined_interface():
    with pytest.raises(profiles.ProfileError, match="wan"):
        profiles.parse(tomllib.loads('[board]\nname = "t"\nwan = "Device.IP.Interface.1"\n'), source="t.toml")
    with pytest.raises(profiles.ProfileError, match="needs a \\[board\\]"):
        profiles.parse({}, source="t.toml")


def test_an_unknown_profile_lists_the_available_ones():
    with pytest.raises(profiles.ProfileError, match="reference-gateway"):
        profiles.load("no-such-board")


def test_the_example_in_the_vendor_guide_is_a_valid_profile():
    guide = (ROOT / "docs" / "vendor-integration.md").read_text(encoding="utf-8")
    example = guide.split("```toml\n", 1)[1].split("```", 1)[0]
    board = profiles.parse(tomllib.loads(example), source="vendor-integration.md")
    assert board.name == "my-gateway"
    access_point = board.factory["Device.WiFi.AccessPoint.{i}"][0][1]
    assert access_point["Security.ModeEnabled"] == "WPA3-Personal"
