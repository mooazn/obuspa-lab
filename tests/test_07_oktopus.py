"""The agent under a real open source controller: Oktopus, run by `make oktopus`.

Opt-in. Runs only when Oktopus answers at VDEV_OKTOPUS_URL (default
http://127.0.0.1:8090), is plugged into the agent, and VDEV_OKTOPUS_EMAIL and
VDEV_OKTOPUS_PASSWORD name an account on it (the admin created on first visit
will do):

    make oktopus
    VDEV_OKTOPUS_EMAIL=... VDEV_OKTOPUS_PASSWORD=... make test ARGS="-k oktopus"
"""
from __future__ import annotations

import os
import time
import urllib.parse

import pytest
import requests

from conftest import system_state

URL = os.environ.get("VDEV_OKTOPUS_URL", "http://127.0.0.1:8090")
EMAIL = os.environ.get("VDEV_OKTOPUS_EMAIL")
PASSWORD = os.environ.get("VDEV_OKTOPUS_PASSWORD")
SN = "os::vdev-001"


@pytest.fixture(scope="module")
def oktopus(device_url):
    try:
        requests.get(f"{URL}/api/auth/admin/exists", timeout=3).raise_for_status()
    except requests.RequestException:
        pytest.skip(f"Oktopus is not running at {URL} (make oktopus)")
    if not (EMAIL and PASSWORD):
        pytest.skip("set VDEV_OKTOPUS_EMAIL and VDEV_OKTOPUS_PASSWORD to an Oktopus account")
    plugged = requests.get(f"{device_url}/api/controllers", timeout=10).json()["controllers"]
    if not any(c["name"] == "oktopus" for c in plugged):
        pytest.skip("Oktopus is not plugged into the agent (make oktopus)")

    login = requests.put(f"{URL}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=10)
    assert login.status_code == 200, f"cannot log in to Oktopus as {EMAIL}"
    token = login.json()

    class Oktopus:
        headers = {"Authorization": token}
        device = f"{URL}/api/device/{urllib.parse.quote(SN, safe='')}/mqtt"

        def listing(self) -> dict | None:
            devices = requests.get(f"{URL}/api/device", headers=self.headers, timeout=10).json()["devices"]
            return next((d for d in devices if d["SN"] == SN), None)

        def online(self) -> bool:
            entry = self.listing()
            return bool(entry) and entry["Status"] == 2

        def wait(self, online: bool, timeout: float) -> None:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self.online() == online:
                    return
                time.sleep(1)
            pytest.fail(f"Oktopus never saw the device {'online' if online else 'offline'} within {timeout}s")

        def get(self, path: str) -> str:
            body = {"param_paths": [path], "max_depth": 1}
            result = requests.put(f"{self.device}/get", json=body, headers=self.headers, timeout=20).json()
            resolved = result["req_path_results"][0]["resolved_path_results"][0]
            return next(iter(resolved["result_params"].values()))

        def set(self, obj: str, param: str, value: str) -> None:
            body = {"allow_partial": False, "update_objs": [{
                "obj_path": obj, "param_settings": [{"param": param, "value": value, "required": True}]}]}
            response = requests.put(f"{self.device}/set", json=body, headers=self.headers, timeout=20)
            assert response.status_code == 200 and "OperSuccess" in response.text, response.text

    return Oktopus()


def test_oktopus_lists_the_device(oktopus):
    oktopus.wait(online=True, timeout=30)
    entry = oktopus.listing()
    assert entry["Vendor"] == "obuspa-lab"
    assert entry["Model"] == "Virtual Gateway"


def test_oktopus_reads_and_writes_the_hardware(oktopus, controller, device_url, restore_ssid):
    """A Set from Oktopus goes through the agent to the simulated device."""
    assert oktopus.get("Device.WiFi.SSID.1.SSID") == controller.get_one("Device.WiFi.SSID.1.SSID")
    oktopus.set("Device.WiFi.SSID.1.", "SSID", "SetByOktopus")
    ssids = [row["values"]["SSID"] for obj in requests.get(f"{device_url}/api/state", timeout=5).json()["objects"]
             if obj["schema"] == "Device.WiFi.SSID.{i}" for row in obj["rows"]]
    assert "SetByOktopus" in ssids


def test_wan_faults_reach_oktopus_too(oktopus, faults, wait_for_agent, device_url):
    """Oktopus's connection runs through the device's WAN port like the lab's own."""
    oktopus.wait(online=True, timeout=30)
    faults.apply("wan_down")
    oktopus.wait(online=False, timeout=20)
    faults.clear("wan_down")
    oktopus.wait(online=True, timeout=120)
    wait_for_agent(timeout=120)
    assert system_state(device_url)["wan"]["linkUp"] is True
