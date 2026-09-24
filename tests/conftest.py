"""Fixtures for tests that run against the compose stack.

Tests talk to the stack from the host: MQTT on localhost:1883 (as a USP
controller) and the device's HTTP API on localhost:8080 (as the web UI does).
Bring the stack up with `make up` first.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "controller"))

from uspctl import UspController  # noqa: E402

BROKER_HOST = os.environ.get("VDEV_BROKER_HOST", "localhost")
BROKER_PORT = int(os.environ.get("VDEV_BROKER_PORT", "1883"))
DEVICE_URL = os.environ.get("VDEV_DEVICE_URL", "http://localhost:8080")

# The agent needs a moment after the broker is up before it answers USP
AGENT_READY_TIMEOUT = float(os.environ.get("VDEV_AGENT_TIMEOUT", "60"))


def system_state(device_url: str) -> dict:
    """The device's `system` block, fetched fresh."""
    return requests.get(f"{device_url}/api/state", timeout=5).json()["system"]


def post(device_url: str, path: str, body: dict | None = None) -> dict:
    response = requests.post(f"{device_url}{path}", json=body or {}, timeout=5)
    response.raise_for_status()
    return response.json()


@pytest.fixture(scope="session")
def device_url() -> str:
    """Waits for the device's web API and returns its base URL."""
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if requests.get(f"{DEVICE_URL}/api/state", timeout=2).ok:
                return DEVICE_URL
        except requests.RequestException:
            pass
        time.sleep(1)
    pytest.fail(f"device web API never came up at {DEVICE_URL} - is `make up` running?")


@pytest.fixture(scope="session")
def controller(device_url):
    """A USP controller connected to the agent, ready to take requests.

    Waits until the agent actually answers a Get, so individual tests do not
    each have to absorb agent startup time.
    """
    ctrl = UspController(broker_host=BROKER_HOST, broker_port=BROKER_PORT, timeout=10)
    ctrl.connect()

    deadline = time.time() + AGENT_READY_TIMEOUT
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            ctrl.get("Device.LocalAgent.EndpointID")
            break
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    else:
        ctrl.disconnect()
        pytest.fail(f"agent never answered USP within {AGENT_READY_TIMEOUT}s: {last_error}")

    yield ctrl

    # Persistent subscriptions outlive the process, so clean up or the agent
    # keeps retrying notifications at a controller that no longer exists.
    try:
        ctrl.remove_subscriptions()
    finally:
        ctrl.disconnect()


@pytest.fixture(scope="session")
def wait_for_agent(controller):
    """Returns a helper that blocks until the agent is answering USP again.

    Used after a reboot: the agent process exits, the container restarts it,
    and it has to reconnect to MQTT before anything else can be asserted.
    """

    def _wait(timeout: float = 90.0, previous_boot: str | None = None) -> None:
        deadline = time.time() + timeout
        last_error: Exception | None = None

        # First make sure a *new* boot has happened. A device-side reboot only
        # drops the socket a moment after the API answers, so polling a Get
        # straight away would succeed against the agent that is about to die.
        if previous_boot is not None:
            while time.time() < deadline:
                try:
                    booted = requests.get(f"{DEVICE_URL}/api/state", timeout=5).json()
                    if booted["system"]["bootedFrom"].get("bootedAt") != previous_boot:
                        break
                except requests.RequestException:
                    pass
                time.sleep(1)
            else:
                pytest.fail(f"agent never rebooted within {timeout}s")

        while time.time() < deadline:
            try:
                # Reading a *proxied* parameter, not an agent-internal one:
                # obuspa answers for itself as soon as it reconnects to MQTT,
                # well before the device has finished booting behind it.
                if controller.get("Device.WiFi.SSID.1.SSID"):
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(2)
        pytest.fail(f"device did not come back within {timeout}s: {last_error}")

    return _wait


@pytest.fixture
def boot_marker(device_url):
    """Returns the current boot's timestamp, to hand to wait_for_agent later."""
    def _marker() -> str:
        state = requests.get(f"{device_url}/api/state", timeout=5).json()
        return state["system"]["bootedFrom"].get("bootedAt", "")
    return _marker


@pytest.fixture(scope="session")
def oper_subscription(controller):
    """Subscribes to OperationComplete so async results reach the controller."""
    controller.subscribe(
        notif_type="OperationComplete",
        reference_list="Device.",
        persistent=False,
    )
    return controller


@pytest.fixture(scope="session")
def boot_subscription(controller):
    """Subscribes to Boot!, persistently so it survives the agent restart."""
    controller.subscribe(
        notif_type="Event",
        reference_list="Device.Boot!",
        persistent=True,
    )
    return controller


@pytest.fixture
def restore_ssid(controller):
    """Puts Device.WiFi.SSID.1 back how it was, so tests do not leak state."""
    path_ssid = "Device.WiFi.SSID.1.SSID"
    path_enable = "Device.WiFi.SSID.1.Enable"
    before = {
        path_ssid: controller.get_one(path_ssid),
        path_enable: controller.get_one(path_enable),
    }
    yield
    controller.set(before)


@pytest.fixture
def ui_row(device_url):
    """Fetches one object row from the device's own API, by instantiated path.

    The UI state is a list of objects each holding rows, because the model is a
    tree rather than a flat map.
    """

    def _row(path: str) -> dict:
        state = requests.get(f"{device_url}/api/state", timeout=5).json()
        for obj in state["objects"]:
            for row in obj["rows"]:
                if row["path"] == path:
                    return row["values"]
        raise KeyError(f"{path} not present in device state")

    return _row


@pytest.fixture
def attach_client_api(device_url):
    """Associates a WiFi client through the device's API, returning its instance."""
    created: list[tuple[int, int]] = []

    def _attach(access_point: int = 1, **kwargs) -> int:
        response = requests.post(
            f"{device_url}/api/clients/attach",
            json={"accessPoint": access_point, **kwargs},
            timeout=5,
        )
        response.raise_for_status()
        instance = response.json()["instance"]
        created.append((access_point, instance))
        return instance

    yield _attach

    for access_point, instance in created:
        requests.post(
            f"{device_url}/api/clients/detach",
            json={"accessPoint": access_point, "instance": instance},
            timeout=5,
        )


@pytest.fixture
def usp_visible(controller):
    """Waits until the agent serves a path the device just created.

    The device signals new instances to obuspa asynchronously (ObjectAdded
    over the plug-in's event stream), so a Get issued immediately after a
    device-side add can see the previous instance list. Real agents behave the
    same way; tests should wait rather than assume.
    """
    def _wait(path: str, timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        last: Exception | None = None
        while time.time() < deadline:
            try:
                values = controller.get(path)
                if values:
                    return values
            except Exception as exc:
                last = exc
            time.sleep(0.2)
        pytest.fail(f"{path} never became visible over USP within {timeout}s: {last}")

    def _host_for(mac: str, timeout: float = 5.0) -> str:
        """The Hosts.Host.{i} path whose PhysAddress is `mac`."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for key, value in controller.get("Device.Hosts.").items():
                if key.endswith(".PhysAddress") and value == mac:
                    return key.rsplit(".", 1)[0]
            time.sleep(0.2)
        pytest.fail(f"no Hosts.Host with PhysAddress {mac} within {timeout}s")

    _wait.host_for = _host_for
    return _wait


@pytest.fixture
def restore_radios(controller):
    """Puts both radios back on afterwards, so one test cannot strand another."""
    yield
    controller.set(
        {"Device.WiFi.Radio.1.Enable": True, "Device.WiFi.Radio.2.Enable": True}
    )


def boot_builtin(device_url: str, wait_for_agent) -> None:
    """Ejects the card and, if the running agent booted with it, reboots.

    Ejecting alone leaves the flashed image running until the next reset, so
    a later test run would silently exercise the card's build.
    """
    if system_state(device_url)["sdcard"]["inserted"]:
        post(device_url, "/api/sdcard", {"action": "eject"})
    booted = system_state(device_url)["bootedFrom"]
    if booted.get("cardSeated"):
        post(device_url, "/api/reboot", {"cause": "LocalReboot"})
        wait_for_agent(timeout=150, previous_boot=booted.get("bootedAt", ""))


@pytest.fixture
def card_ejected(device_url, wait_for_agent):
    """Whatever happens, boot the built-in image again afterwards."""
    yield
    boot_builtin(device_url, wait_for_agent)


@pytest.fixture
def console(device_url):
    """Returns a helper reading console lines: console(since=0, tail=None)."""
    def _read(since: int = 0, tail: int | None = None) -> list[dict]:
        # obuspa's protocol trace is verbose; ask for the whole ring by default
        params = {"since": since, "tail": tail if tail is not None else 20000}
        response = requests.get(f"{device_url}/api/console", params=params, timeout=5)
        response.raise_for_status()
        return response.json()["lines"]
    return _read


# ----------------------------------------------------------------------
# The lab around the firmware: faults, WAN, HAL, observation, flashing
# ----------------------------------------------------------------------


@pytest.fixture
def faults(device_url):
    """Fault helpers; clears every fault the test applied on the way out."""
    applied: set[str] = set()

    class Faults:
        def apply(self, kind: str, params: dict | None = None) -> dict:
            applied.add(kind)
            return post(device_url, "/api/faults", {"kind": kind, "params": params or {}})["fault"]

        def clear(self, kind: str) -> None:
            applied.discard(kind)
            requests.delete(f"{device_url}/api/faults/{kind}", timeout=5)

        def get(self, kind: str) -> dict | None:
            for fault in requests.get(f"{device_url}/api/faults", timeout=5).json()["faults"]:
                if fault["kind"] == kind:
                    return fault
            return None

        def wait_applied(self, kind: str, timeout: float = 20) -> dict:
            deadline = time.time() + timeout
            while time.time() < deadline:
                fault = self.get(kind)
                if fault and fault["applied"]:
                    return fault
                time.sleep(0.5)
            pytest.fail(f"fault {kind} was not applied within {timeout}s: {self.get(kind)}")

    yield Faults()
    for kind in list(applied):
        requests.delete(f"{device_url}/api/faults/{kind}", timeout=5)


@pytest.fixture
def lab_clock(device_url):
    """The lab clock; returns the firmware and device to real time on the way out."""

    class Clock:
        def get(self) -> dict:
            return requests.get(f"{device_url}/api/clock", timeout=5).json()

        def jump(self, seconds: float) -> dict:
            return post(device_url, "/api/clock", {"jump": seconds})

        def rate(self, rate: float) -> dict:
            return post(device_url, "/api/clock", {"rate": rate})

        def reset(self) -> dict:
            return requests.delete(f"{device_url}/api/clock", timeout=5).json()

    yield Clock()
    requests.delete(f"{device_url}/api/clock", timeout=5)


@pytest.fixture
def wan_state(device_url):
    return lambda: requests.get(f"{device_url}/api/wan", timeout=5).json()


@pytest.fixture
def hal(device_url):
    """HAL helpers; deletes every key the test set on the way out."""
    touched: set[str] = set()

    class Hal:
        def put(self, key: str, value: str) -> dict:
            touched.add(key)
            response = requests.put(f"{device_url}/api/hal/{key}", json={"value": value}, timeout=5)
            response.raise_for_status()
            return response.json()

        def get(self, key: str) -> str | None:
            response = requests.get(f"{device_url}/api/hal/{key}", timeout=5)
            return response.json()["value"] if response.ok else None

        def delete(self, key: str) -> int:
            touched.discard(key)
            return requests.delete(f"{device_url}/api/hal/{key}", timeout=5).status_code

    yield Hal()
    for key in list(touched):
        requests.delete(f"{device_url}/api/hal/{key}", timeout=5)


@pytest.fixture
def usp_timeline(device_url):
    def _read(since: int = 0) -> list[dict]:
        response = requests.get(f"{device_url}/api/usp/timeline",
                                params={"since": since, "limit": 1000}, timeout=5)
        response.raise_for_status()
        return response.json()["entries"]
    return _read


@pytest.fixture(scope="module")
def flashed_plugin_card(device_url, wait_for_agent):
    """Flashes the example plug-ins onto the card for the duration of a module.

    The developer's card is backed up first and restored afterwards, so the
    suite never eats a build someone was about to test. Skips when Docker is
    not available or VDEV_SKIP_FLASH is set.
    """
    import shutil
    import subprocess
    import tempfile

    if os.environ.get("VDEV_SKIP_FLASH") or shutil.which("docker") is None:
        pytest.skip("flashing needs docker (set VDEV_SKIP_FLASH to skip explicitly)")

    card = ROOT / "sdcard"
    backup = Path(tempfile.mkdtemp(prefix="sdcard-backup-"))
    for name in ("obuspa", "vdev_plugin.so", "manifest.json", "plugins"):
        source = card / name
        if source.is_dir():
            shutil.copytree(source, backup / name)
        elif source.exists():
            shutil.copy2(source, backup / name)

    result = subprocess.run(
        [str(ROOT / "scripts" / "flash.sh"),
         "--plugin", str(ROOT / "examples" / "disk-monitor"),
         "--plugin", str(ROOT / "examples" / "parental-controls"),
         "--label", "examples-test"],
        capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        pytest.fail(f"flash failed:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")

    import json
    manifest = json.loads((card / "manifest.json").read_text())

    # The device reads the card through a bind mount; wait until it reports
    # the card we just wrote before handing it to a test
    deadline = time.time() + 20
    while time.time() < deadline:
        seen = system_state(device_url)["sdcard"].get("manifest") or {}
        if seen.get("label") == manifest["label"] and seen.get("builtAt") == manifest["builtAt"]:
            break
        time.sleep(0.25)
    else:
        pytest.fail("device never saw the freshly flashed card")

    yield manifest

    # Back on the built-in image, then restore whatever was on the card
    boot_builtin(device_url, wait_for_agent)
    for name in ("obuspa", "vdev_plugin.so", "manifest.json", "plugins"):
        target = card / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        source = backup / name
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.exists():
            shutil.copy2(source, target)
    shutil.rmtree(backup, ignore_errors=True)


@pytest.fixture
def reboot_and_wait(device_url, wait_for_agent):
    """Reboots the device and blocks until the agent is serving again."""
    def _reboot(timeout: float = 150) -> None:
        marker = system_state(device_url)["bootedFrom"].get("bootedAt", "")
        post(device_url, "/api/reboot", {"cause": "LocalReboot"})
        wait_for_agent(timeout=timeout, previous_boot=marker)
    return _reboot
