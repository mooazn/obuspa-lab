"""The virtual device itself: state and behaviour, with no protocol awareness.

Nothing in this module knows what USP is. It deals in native Python values and
instantiated data model paths. Management protocols attach to it as adapters:
today the USP proxy in `shim.py`, tomorrow (if wanted) a CWMP/TR-069 adapter,
without any change here.

Instance representation
-----------------------
Objects may nest, e.g. Device.WiFi.AccessPoint.{i}.AssociatedDevice.{i}. Each
row is keyed by the *full tuple* of instance numbers in a single flat dict per
schema path:

    _state["Device.WiFi.AccessPoint.{i}.AssociatedDevice.{i}"] = {
        (1, 1): {...},      # access point 1, client 1
        (1, 4): {...},      # access point 1, client 4
    }                       # access point 2 simply has no keys

The instance space is jagged - one access point can have three clients while
another has none - so it is deliberately not stored as nested dicts or as
anything rectangular. A parent that exists with zero children is just a prefix
with no matching keys, which is a different thing from a parent that does not
exist (that is a missing row in the parent's own table).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional

from .labclock import LabClock
from .model import (
    COMMANDS,
    EVENTS,
    FACTORY_INSTANCES,
    MODEL,
    CommandDef,
    ObjectDef,
    ParamDef,
)

log = logging.getLogger(__name__)


class DeviceError(Exception):
    """Raised when an operation is rejected by the device."""


class UnknownPathError(DeviceError):
    """Raised when a path does not exist in the data model."""


class ReadOnlyError(DeviceError):
    """Raised when a write targets a read-only parameter."""


class RebootInterrupted(Exception):
    """Raised inside a running job when the device reboots under it."""


class Resolved:
    """A path resolved against the data model."""

    __slots__ = ("obj", "instances", "param")

    def __init__(self, obj: ObjectDef, instances: tuple[int, ...],
                 param: Optional[ParamDef]):
        self.obj = obj
        self.instances = instances
        self.param = param


class Job:
    """A running asynchronous command."""

    def __init__(self, device: "VirtualDevice", request_id: int, path: str):
        self.device = device
        self.request_id = request_id
        self.path = path
        self._cancelled = threading.Event()

    def status(self, text: str) -> None:
        """Reports progress, which reaches the controller as an operation status."""
        self.device.emit(
            {"event": "operation_status", "request_id": self.request_id, "status": text}
        )

    def sleep(self, seconds: float) -> None:
        """Sleeps, but wakes immediately and aborts if the device reboots."""
        if self._cancelled.wait(timeout=seconds):
            raise RebootInterrupted("device rebooted while the operation was running")

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()


def _schema_regex(schema_path: str) -> re.Pattern:
    """Turns Device.WiFi.SSID.{i} into a regex capturing each instance number."""
    parts = [re.escape(part) for part in schema_path.split("{i}")]
    return re.compile("^" + r"(\d+)".join(parts) + r"(?:\.(.+))?$")


class VirtualDevice:
    """A simulated CPE.

    Thread-safe: the USP proxy, the web UI and running jobs all touch this
    concurrently. The lock is reentrant because derived parameters read other
    parts of the device while a read is already in progress.
    """

    def __init__(
        self,
        model: Optional[list[ObjectDef]] = None,
        state_file: Optional[str] = None,
        reboot_seconds: float = 5.0,
        sdcard_dir: Optional[str] = None,
    ):
        self._model = model if model is not None else MODEL
        self._commands = COMMANDS
        self._events = EVENTS
        self._lock = threading.RLock()
        self._listeners: list[Callable[[], None]] = []
        self._event_listeners: list[Callable[[dict], None]] = []

        self.state_file = state_file
        self.reboot_seconds = reboot_seconds

        # Removable media. The SD card directory is a read-only mount shared
        # with the agent; whether the card is *seated* is device state.
        self.run_dir = os.path.dirname(state_file) if state_file else None
        self.sdcard_dir = sdcard_dir
        self.sdcard_inserted = False

        # {schema_path: {instance_tuple: {param_name: native_value}}}
        self._state: dict[str, dict[tuple[int, ...], dict[str, Any]]] = {
            obj.path: {} for obj in self._model
        }
        self._regex = {obj.path: _schema_regex(obj.path) for obj in self._model}
        # Deepest and longest schemas first, so the most specific object claims
        # a path before a shorter ancestor can.
        self._resolution_order = sorted(
            self._model, key=lambda o: (o.depth, len(o.path)), reverse=True
        )
        self._add_regex = {
            obj.path: re.compile(
                "^" + r"(\d+)".join(re.escape(p) for p in obj.add_path.split("{i}")) + "$"
            )
            for obj in self._model
        }

        # The lab clock: what time the device and the firmware believe it is.
        # A lab instrument like the faults - it persists, and survives a
        # factory reset the way a hardware RTC does.
        self.clock = LabClock(os.path.join(self.run_dir, "faketime") if self.run_dir else None)

        self.boot_count = 0
        self.reboot_cause = "FactoryReset"
        self.booted_at = self.clock.now()
        self.rebooting = False

        self._jobs: dict[int, Job] = {}
        self._reboot_hook: Optional[Callable[[], None]] = None

        # Whether the agent's plug-in currently holds its event subscription -
        # the surest sign the firmware is up. A crash-looping agent (say, a
        # vendor plug-in with a missing symbol) never gets this far.
        self.agent_connected = False
        self.agent_connected_since: Optional[float] = None
        # Returns the number of agent boots in the last five minutes (longer than
        # the container restart backoff); set by main
        self._recent_boots: Callable[[], int] = lambda: 0

        self.fault_state: dict[str, dict] = {}
        self.hal: dict[str, str] = {}
        self._hal_listeners = []
        self.wan = None
        self._load_factory_defaults()
        self._load_persisted()
        from .faults import FaultRegistry
        self.faults = FaultRegistry(self)
        # The control file is rewritten now so that an agent booting against a
        # stale volume never runs on a clock the device no longer holds.
        self.clock.write_file()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    @property
    def model(self) -> list[ObjectDef]:
        return self._model

    @property
    def commands(self) -> list[CommandDef]:
        return self._commands

    @property
    def events(self) -> list:
        return self._events

    def _object_by_schema(self, schema_path: str) -> Optional[ObjectDef]:
        for obj in self._model:
            if obj.path == schema_path:
                return obj
        return None

    def _defaults_for(self, obj: ObjectDef) -> dict[str, Any]:
        return {p.name: p.default for p in obj.params if p.derived is None}

    def _load_factory_defaults(self) -> None:
        for schema_path, rows in FACTORY_INSTANCES.items():
            obj = self._object_by_schema(schema_path)
            if obj is None:
                continue
            for instances, row in rows:
                values = self._defaults_for(obj)
                values.update({k: v for k, v in row.items() if k in values})
                self._state[obj.path][tuple(instances)] = values

    # ------------------------------------------------------------------
    # Persistence - configuration survives a reboot, runtime state does not
    # ------------------------------------------------------------------

    def _load_persisted(self) -> None:
        if not self.state_file or not os.path.exists(self.state_file):
            return

        try:
            with open(self.state_file) as handle:
                saved = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read %s, starting from factory defaults: %s",
                        self.state_file, exc)
            return

        for schema_path, rows in saved.get("objects", {}).items():
            obj = self._object_by_schema(schema_path)
            if obj is None:
                continue
            restored: dict[tuple[int, ...], dict[str, Any]] = {}
            for key, values in rows.items():
                instances = tuple(int(n) for n in key.split(","))
                merged = self._defaults_for(obj)
                merged.update(
                    {
                        name: value
                        for name, value in values.items()
                        if (param := obj.param(name)) is not None and param.persistent
                    }
                )
                restored[instances] = merged
            self._state[obj.path] = restored

        self.fault_state = saved.get("faults", {})
        self.clock.load(saved.get("clock"))
        self.boot_count = saved.get("boot_count", 0)
        self.reboot_cause = saved.get("reboot_cause", "FactoryReset")
        self.sdcard_inserted = bool(saved.get("sdcard_inserted", False))
        log.info("restored configuration from %s (boot count %d, cause %s)",
                 self.state_file, self.boot_count, self.reboot_cause)

    def persist(self) -> None:
        """Writes configuration to disk. Called on every change and on reboot."""
        if not self.state_file:
            return

        with self._lock:
            payload = {
                "objects": {
                    obj.path: {
                        ",".join(str(n) for n in instances): {
                            name: value
                            for name, value in values.items()
                            if (p := obj.param(name)) is not None and p.persistent
                        }
                        for instances, values in self._state[obj.path].items()
                    }
                    for obj in self._model
                },
                "boot_count": self.boot_count,
                "reboot_cause": self.reboot_cause,
                "sdcard_inserted": self.sdcard_inserted,
                "faults": self.fault_state,
                "clock": self.clock.to_json(),
            }

        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            temporary = f"{self.state_file}.tmp"
            with open(temporary, "w") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(temporary, self.state_file)      # atomic
        except OSError as exc:
            log.warning("could not persist state to %s: %s", self.state_file, exc)

    # ------------------------------------------------------------------
    # Change notification and event push
    # ------------------------------------------------------------------

    def add_listener(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # a broken UI client must not break the device
                pass

    def add_event_listener(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            self._event_listeners.append(callback)

    def remove_event_listener(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            if callback in self._event_listeners:
                self._event_listeners.remove(callback)

    def emit(self, event: dict) -> None:
        """Pushes an event towards the agent (and anything else listening)."""
        with self._lock:
            listeners = list(self._event_listeners)
        for callback in listeners:
            try:
                callback(event)
            except Exception:
                log.exception("event listener failed for %s", event.get("event"))

    def set_boot_probe(self, probe: Callable[[], int]) -> None:
        self._recent_boots = probe

    # ------------------------------------------------------------------
    # Lab clock
    # ------------------------------------------------------------------

    def adjust_clock(self, jump: Optional[float] = None, rate: Optional[float] = None) -> dict:
        """Jumps the clock and/or changes its rate, for the device and the firmware."""
        with self._lock:
            snapshot = self.clock.adjust(jump=jump, rate=rate)
            self.persist()
        self._clock_changed()
        return snapshot

    def reset_clock(self) -> dict:
        with self._lock:
            snapshot = self.clock.reset()
            self.persist()
        self._clock_changed()
        return snapshot

    def sync_clock(self) -> str:
        """Rewrites the firmware's control file to the clock as it is now.

        Called by the agent's plug-in on its way up, so that a fresh obuspa
        process anchors to the current lab time rather than to the time of
        the last change.
        """
        with self._lock:
            return self.clock.write_file()

    def _clock_changed(self) -> None:
        log.info("lab clock: %s", self.clock.control_string())
        # The firmware's timers block until their next deadline in real time;
        # the plug-in turns this into a wake-up of the data-model thread so
        # that anything now due fires at once.
        self.emit({"event": "clock", **self.clock.snapshot()})
        self._notify()

    def set_agent_connected(self, connected: bool) -> None:
        with self._lock:
            self.agent_connected = connected
            self.agent_connected_since = time.time() if connected else None
        self._notify()

    def hal_get(self, key):
        with self._lock:
            return self.hal.get(key)

    def hal_set(self, key, value):
        if not isinstance(key, str) or not key or len(key.encode()) > 255:
            raise DeviceError("key must be 1..255 UTF-8 bytes")
        if value is not None and (not isinstance(value, str) or len(value.encode()) > 4096):
            raise DeviceError("value must be a string of at most 4096 UTF-8 bytes or null")
        with self._lock:
            if value is None:
                self.hal.pop(key, None)
            else:
                self.hal[key] = value
            for callback in list(self._hal_listeners):
                callback({"key": key, "value": value})
        self._notify()

    def add_hal_listener(self, callback):
        with self._lock:
            self._hal_listeners.append(callback)
            return dict(self.hal)

    def remove_hal_listener(self, callback):
        with self._lock:
            self._hal_listeners.remove(callback)

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def resolve(self, path: str) -> Resolved:
        """Resolves an instantiated path, with or without a trailing parameter.

        Candidates are tried deepest-first. A parent schema will otherwise
        swallow a child path - Device.WiFi.AccessPoint.{i} happily matches
        Device.WiFi.AccessPoint.1.AssociatedDevice.1.MACAddress with a
        "parameter" of AssociatedDevice.1.MACAddress - so the most specific
        object has to win, and a near-miss has to fall through rather than
        abort the search.
        """
        path = path.rstrip(".")
        matched_object = False

        for obj in self._resolution_order:
            match = self._regex[obj.path].match(path)
            if match is None:
                continue

            groups = match.groups()
            instances = tuple(int(n) for n in groups[: obj.depth])
            param_name = groups[obj.depth]

            if param_name is None:
                return Resolved(obj, instances, None)

            param = obj.param(param_name)
            if param is not None:
                return Resolved(obj, instances, param)

            # The object part matched but the remainder is not one of its
            # parameters; a deeper object may still claim this path.
            matched_object = True

        if matched_object:
            raise UnknownPathError(f"unknown parameter: {path}")
        raise UnknownPathError(f"unknown path: {path}")

    def resolve_command(self, path: str) -> tuple[CommandDef, tuple[int, ...]]:
        """Resolves an invoked command path to its definition and instances."""
        for command in self._commands:
            pattern = "^" + r"(\d+)".join(
                re.escape(part) for part in command.path.split("{i}")
            ) + "$"
            match = re.match(pattern, path)
            if match is not None:
                return command, tuple(int(n) for n in match.groups())

        raise UnknownPathError(f"unknown command: {path}")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def child_instances(self, schema_path: str,
                        parent: tuple[int, ...] = ()) -> list[int]:
        """Instance numbers of `schema_path` directly under `parent`.

        Returns an empty list for a parent with no children, which is a normal
        state and not an error - the instance space is jagged.
        """
        with self._lock:
            rows = self._state.get(schema_path)
            if rows is None:
                return []
            parent = tuple(parent)
            depth = len(parent)
            return sorted(
                key[depth]
                for key in rows
                if len(key) == depth + 1 and key[:depth] == parent
            )

    def get(self, path: str) -> Any:
        """Returns the native value at an instantiated parameter path."""
        with self._lock:
            resolved = self.resolve(path)
            if resolved.param is None:
                raise UnknownPathError(f"not a parameter: {path}")

            values = self._state[resolved.obj.path].get(resolved.instances)
            if values is None:
                raise UnknownPathError(f"no such instance: {path}")

            if resolved.param.derived is not None:
                return resolved.param.derived(values, self, resolved.instances)

            return values[resolved.param.name]

    def safe_get(self, path: str) -> Any:
        """As get(), but returns None instead of raising.

        Used by derived parameters, which routinely point at references that
        may not resolve (an SSID whose radio was deleted, say).
        """
        try:
            return self.get(path)
        except (DeviceError, KeyError):
            return None

    def instances(self) -> list[str]:
        """Every existing object instance path, parents before children.

        obuspa needs the parent registered before the child, so the ordering
        here is load-bearing.
        """
        with self._lock:
            out: list[str] = []
            for obj in sorted(self._model, key=lambda o: o.depth):
                for key in sorted(self._state[obj.path]):
                    out.append(obj.instantiate(key))
            return out

    def _row_values(self, obj: ObjectDef, instances: tuple[int, ...],
                    values: dict) -> dict:
        row = {}
        for param in obj.params:
            if param.derived is not None:
                row[param.name] = param.derived(values, self, instances)
            else:
                row[param.name] = values[param.name]
        return row

    def snapshot(self) -> dict:
        """Returns the full device state, for the UI."""
        with self._lock:
            objects = []
            for obj in self._model:
                rows = []
                for key in sorted(self._state[obj.path]):
                    values = self._state[obj.path][key]
                    rows.append(
                        {
                            "path": obj.instantiate(key),
                            "instances": list(key),
                            "values": self._row_values(obj, key, values),
                        }
                    )
                objects.append(
                    {
                        "schema": obj.path,
                        "writable": obj.writable,
                        # Parameter metadata so the UI can render any object
                        # generically instead of knowing them by name
                        "params": [
                            {
                                "name": p.name,
                                "type": p.type,
                                "writable": p.is_writable,
                            }
                            for p in obj.params
                        ],
                        "rows": rows,
                    }
                )

            return {
                "objects": objects,
                "system": {
                    "rebooting": self.rebooting,
                    "bootCount": self.boot_count,
                    "rebootCause": self.reboot_cause,
                    "upTime": int(self.clock.now() - self.booted_at),
                    "runningJobs": [
                        {"requestId": job.request_id, "path": job.path}
                        for job in self._jobs.values()
                    ],
                    "sim": {
                        "inserted": self.safe_get(
                            "Device.Cellular.Interface.1.USIM.Status") == "Valid",
                        "iccid": self.safe_get("Device.Cellular.Interface.1.USIM.ICCID") or "",
                        "carrier": self.safe_get("Device.Cellular.Interface.1.NetworkInUse") or "",
                    },
                    "sdcard": {
                        "present": self.sdcard_manifest() is not None,
                        "inserted": self.sdcard_inserted,
                        "manifest": self.sdcard_manifest(),
                    },
                    "bootedFrom": self.booted_from(),
                    "faults": self.faults.list_faults(),
                    "clock": self.clock.snapshot(),
                    "hal": dict(self.hal),
                    "wan": self.wan.snapshot() if self.wan else None,
                    "agent": {
                        "eventsConnected": self.agent_connected,
                        "since": self.agent_connected_since,
                        "recentBoots": (recent := self._recent_boots()),
                        # Restarting repeatedly without ever connecting: a
                        # plug-in that fails to load, or a data model collision
                        "crashLooping": recent >= 3 and not self.agent_connected,
                    },
                },
            }

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def set(self, path: str, value: Any, internal: bool = False) -> None:
        """Writes a native value to an instantiated parameter path."""
        self.set_many({path: value}, internal=internal)

    def set_many(self, updates: dict[str, Any], internal: bool = False) -> None:
        """Applies several writes atomically.

        Either all succeed or none are applied, which is what a USP Set of
        multiple parameters expects. The raised DeviceError carries the path
        that failed so the caller can report a failure index.

        `internal` writes come from the device itself rather than a controller,
        so they may touch read-only parameters.
        """
        with self._lock:
            snapshot = {
                schema: {k: dict(v) for k, v in rows.items()}
                for schema, rows in self._state.items()
            }

            side_effects: list[tuple[ParamDef, tuple[int, ...], Any]] = []
            for path, value in updates.items():
                try:
                    param, instances = self._set_locked(path, value, internal)
                except DeviceError as exc:
                    self._state = snapshot
                    exc.path = path     # lets the caller report a failure index
                    raise
                except Exception:
                    self._state = snapshot
                    raise
                if param.on_change is not None:
                    side_effects.append((param, instances, value))

            # Ripple effects run only once every write has been accepted, so a
            # rejected Set never leaves half a cascade behind.
            for param, instances, value in side_effects:
                param.on_change(self, instances, value)

            self._notify()
        self.persist()

    def _set_locked(self, path: str, value: Any,
                    internal: bool) -> tuple[ParamDef, tuple[int, ...]]:
        resolved = self.resolve(path)
        if resolved.param is None:
            raise UnknownPathError(f"not a parameter: {path}")

        param = resolved.param
        if param.derived is not None:
            raise ReadOnlyError(f"parameter is read-only: {path}")
        if not internal and not param.is_writable:
            raise ReadOnlyError(f"parameter is read-only: {path}")

        values = self._state[resolved.obj.path].get(resolved.instances)
        if values is None:
            raise UnknownPathError(f"no such instance: {path}")

        if not internal and param.validate is not None:
            try:
                param.validate(value)
            except ValueError as exc:
                raise DeviceError(str(exc)) from exc

        values[param.name] = value
        return param, resolved.instances

    # ------------------------------------------------------------------
    # Object lifecycle
    # ------------------------------------------------------------------

    def _resolve_add(self, add_path: str) -> tuple[ObjectDef, tuple[int, ...]]:
        """Resolves Device.WiFi.AccessPoint.1.AssociatedDevice to its schema."""
        add_path = add_path.rstrip(".")
        for obj in self._model:
            match = self._add_regex[obj.path].match(add_path)
            if match is not None:
                return obj, tuple(int(n) for n in match.groups())
        raise UnknownPathError(f"unknown object: {add_path}")

    def add(self, add_path: str, internal: bool = False) -> int:
        """Creates an instance, returning its number.

        The parent must already exist: a child of a missing parent is a
        different failure from a parent that merely has no children yet.
        """
        with self._lock:
            obj, parent = self._resolve_add(add_path)
            if not internal and not obj.writable:
                raise DeviceError(f"object does not support Add: {add_path}")

            if parent:
                parent_obj = None
                for candidate in self._model:
                    if candidate.depth == len(parent) and obj.path.startswith(
                        candidate.path
                    ):
                        parent_obj = candidate
                        break
                if parent_obj is not None and parent not in self._state[parent_obj.path]:
                    raise UnknownPathError(f"no such parent instance: {add_path}")

            existing = self.child_instances(obj.path, parent)
            instance = (max(existing) + 1) if existing else 1
            key = parent + (instance,)
            self._state[obj.path][key] = self._defaults_for(obj)

            created = obj.instantiate(key)
            self._notify()

        self.emit({"event": "object_added", "path": created})
        self.persist()
        return instance

    def delete(self, path: str, internal: bool = False) -> None:
        """Deletes an object instance and everything underneath it."""
        with self._lock:
            resolved = self.resolve(path)
            if resolved.param is not None:
                raise DeviceError(f"not an object instance: {path}")
            if not internal and not resolved.obj.writable:
                raise DeviceError(f"object does not support Delete: {path}")

            rows = self._state[resolved.obj.path]
            if resolved.instances not in rows:
                raise UnknownPathError(f"no such instance: {path}")

            removed = [resolved.obj.instantiate(resolved.instances)]
            del rows[resolved.instances]

            # Children go with the parent, deepest first
            for child in sorted(self._model, key=lambda o: -o.depth):
                if child is resolved.obj or not child.path.startswith(resolved.obj.path):
                    continue
                prefix = resolved.instances
                for key in [k for k in self._state[child.path]
                            if k[: len(prefix)] == prefix]:
                    removed.append(child.instantiate(key))
                    del self._state[child.path][key]

            self._notify()

        for gone in removed:
            self.emit({"event": "object_deleted", "path": gone})
        self.persist()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def operate_sync(self, path: str, inputs: dict) -> dict:
        """Runs a synchronous command and returns its output arguments."""
        command, instances = self.resolve_command(path)
        if command.is_async:
            raise DeviceError(f"{path} is an asynchronous command")

        arguments = dict(inputs)
        arguments["__instances__"] = instances

        try:
            return command.handler(self, arguments) or {}
        except ValueError as exc:
            raise DeviceError(str(exc)) from exc

    def operate_async(self, path: str, request_id: int, inputs: dict) -> None:
        """Starts an asynchronous command on its own thread and returns at once."""
        command, instances = self.resolve_command(path)
        if not command.is_async:
            raise DeviceError(f"{path} is a synchronous command")

        with self._lock:
            running = sum(1 for job in self._jobs.values() if job.path == path)
            if running >= command.max_concurrency:
                raise DeviceError(
                    f"{path} already has {running} operations in progress "
                    f"(limit {command.max_concurrency})"
                )

            job = Job(self, request_id, path)
            self._jobs[request_id] = job

        arguments = dict(inputs)
        arguments["__instances__"] = instances

        threading.Thread(
            target=self._run_job,
            args=(command, job, arguments),
            name=f"job-{request_id}",
            daemon=True,
        ).start()
        self._notify()

    def _run_job(self, command: CommandDef, job: Job, inputs: dict) -> None:
        err_code = 0
        err_msg = ""
        outputs: dict = {}

        try:
            outputs = command.handler(self, inputs, job) or {}
        except RebootInterrupted as exc:
            err_code = 7022     # USP_ERR_COMMAND_FAILURE
            err_msg = str(exc)
        except (ValueError, DeviceError) as exc:
            err_code = 7022
            err_msg = str(exc)
        except Exception as exc:
            log.exception("command %s failed", job.path)
            err_code = 7022
            err_msg = f"internal error: {exc}"

        with self._lock:
            self._jobs.pop(job.request_id, None)

        # A job killed by a reboot reports nothing: the agent is gone anyway,
        # and obuspa fails the operation itself on restart.
        if not job.cancelled:
            self.emit(
                {
                    "event": "operation_complete",
                    "request_id": job.request_id,
                    "err_code": err_code,
                    "err_msg": err_msg,
                    "output": {k: str(v) for k, v in outputs.items()},
                }
            )
        self._notify()

    # ------------------------------------------------------------------
    # Reboot
    # ------------------------------------------------------------------

    def set_reboot_hook(self, hook: Callable[[], None]) -> None:
        """Registers what actually performs the simulated downtime."""
        self._reboot_hook = hook

    def reboot(self, cause: str = "RemoteReboot") -> None:
        """Simulates a device reboot.

        Configuration is persisted and restored; volatile parameters and any
        in-flight operations are lost, the way they are on real hardware.
        """
        with self._lock:
            if self.rebooting:
                # Pressing reset during a reboot does nothing on real hardware
                # either; a second downtime racing the first would only make
                # the boot record lie.
                log.info("reboot requested (cause=%s) while already rebooting - ignored", cause)
                return
            log.info("reboot requested (cause=%s)", cause)
            self.rebooting = True
            self.reboot_cause = cause
            self.boot_count += 1
            jobs = list(self._jobs.values())

        for job in jobs:
            job.cancel()

        self.persist()
        self._write_boot_config()
        self._notify()

        if self._reboot_hook is not None:
            self._reboot_hook()

    def factory_reset(self, cause: str = "FactoryReset") -> None:
        """Wipes everything back to factory state and reboots.

        The device forgets its configuration; the agent must forget its
        database too, which the entrypoint does when it finds the marker file.
        A physical card left in the slot stays in the slot.
        """
        log.info("factory reset requested (cause=%s)", cause)
        card_seated = self.sdcard_inserted

        # Cards in slots are hardware, not configuration: they stay put, and
        # the modem re-reads the SIM on the way back up.
        sim = None
        if self.safe_get("Device.Cellular.Interface.1.USIM.Status") == "Valid":
            sim = {
                "iccid": self.safe_get("Device.Cellular.Interface.1.USIM.ICCID"),
                "imsi": self.safe_get("Device.Cellular.Interface.1.USIM.IMSI"),
                "carrier": self.safe_get("Device.Cellular.Interface.1.NetworkInUse"),
            }

        with self._lock:
            self._state = {obj.path: {} for obj in self._model}
            self._load_factory_defaults()
            self.boot_count = 0
            self.sdcard_inserted = card_seated

        if sim is not None:
            from .model import insert_sim      # avoids a cycle at import time
            insert_sim(self, iccid=sim["iccid"], imsi=sim["imsi"], carrier=sim["carrier"])

        if self.state_file and os.path.exists(self.state_file):
            try:
                os.unlink(self.state_file)
            except OSError as exc:
                log.warning("could not remove %s: %s", self.state_file, exc)

        if self.run_dir:
            try:
                with open(os.path.join(self.run_dir, "factory-reset"), "w") as handle:
                    handle.write(cause)
            except OSError as exc:
                log.warning("could not write factory reset marker: %s", exc)

        self.reboot(cause)

    # ------------------------------------------------------------------
    # Removable media: SIM lives in the data model; the SD card is the
    # image the agent boots from
    # ------------------------------------------------------------------

    def sdcard_manifest(self) -> Optional[dict]:
        """What is on the card in the slot, or None if the slot is empty."""
        if not self.sdcard_dir:
            return None
        path = os.path.join(self.sdcard_dir, "manifest.json")
        try:
            with open(path) as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None

    def insert_sdcard(self) -> None:
        # A card written a moment ago can be mid-flight through the bind
        # mount; give it a beat before deciding the slot is empty.
        for _ in range(20):
            if self.sdcard_manifest() is not None:
                break
            time.sleep(0.1)
        else:
            raise DeviceError("there is no image on the card - run `make flash` first")
        with self._lock:
            self.sdcard_inserted = True
        self._write_boot_config()
        self.persist()
        self._notify()

    def eject_sdcard(self) -> None:
        with self._lock:
            self.sdcard_inserted = False
        self._write_boot_config()
        self.persist()
        self._notify()

    def _write_boot_config(self) -> None:
        """Tells the agent's entrypoint where to boot from next time."""
        if not self.run_dir:
            return
        try:
            with open(os.path.join(self.run_dir, "boot.json"), "w") as handle:
                json.dump(
                    {"bootFrom": "sdcard" if self.sdcard_inserted else "internal"},
                    handle,
                )
        except OSError as exc:
            log.warning("could not write boot config: %s", exc)

    def booted_from(self) -> dict:
        """What the agent actually booted, as it reported on its way up."""
        if not self.run_dir:
            return {"from": "unknown"}
        try:
            with open(os.path.join(self.run_dir, "booted-from.json")) as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"from": "unknown"}

    def complete_boot(self) -> None:
        """Called once the device is serving again after a reboot."""
        with self._lock:
            self.rebooting = False
            self.booted_at = self.clock.now()

            # Volatile parameters come up at their defaults, and rows made
            # entirely of volatile state (associated clients) do not come back
            # at all - nothing reassociates until it decides to.
            for obj in self._model:
                volatile_row = all(
                    not p.persistent for p in obj.params if p.derived is None
                )
                if volatile_row:
                    # Rows made entirely of runtime state do not survive: WiFi
                    # clients have to reassociate. Anything the factory defines
                    # is rediscovered though, the way a gateway repopulates its
                    # host table from the wired devices still plugged into it.
                    self._state[obj.path].clear()
                    for instances, row in FACTORY_INSTANCES.get(obj.path, []):
                        values = self._defaults_for(obj)
                        values.update({k: v for k, v in row.items() if k in values})
                        self._state[obj.path][tuple(instances)] = values
                    continue
                for values in self._state[obj.path].values():
                    for param in obj.params:
                        if param.derived is None and not param.persistent:
                            values[param.name] = param.default

            self._notify()

        log.info("device booted (boot count %d, cause %s)",
                 self.boot_count, self.reboot_cause)
