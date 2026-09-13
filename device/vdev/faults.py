"""Device-owned fault state; the shared directory is only a firmware mailbox."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import time
import uuid
from pathlib import Path


class FaultRegistry:
    SCOPES = {"disk_fill": "agent", "wan_down": "device", "wan_latency": "device"}

    def __init__(self, device):
        self.device = device
        self.directory = Path(device.run_dir) / "faults" if device.run_dir else None
        self.acks = {}
        self.task = None
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)
            for path in self.directory.iterdir():
                if path.is_file():
                    path.unlink()
            for fault in device.fault_state.values():
                self._write(fault)

    @classmethod
    def validate(cls, kind, params):
        if kind not in cls.SCOPES:
            raise ValueError("unknown fault kind")
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        expected = {"disk_fill": {"percent"}, "wan_latency": {"ms"}, "wan_down": set()}[kind]
        if set(params) != expected:
            raise ValueError(f"{kind} requires exactly {sorted(expected)}")
        for key in expected:
            value = params[key]
            low, high = (1, 100) if key == "percent" else (0, 2000)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key} must be an integer from {low} to {high}")

    def _write(self, fault):
        if self.directory and fault["scope"] == "agent":
            path = self.directory / (fault["kind"] + ".json")
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(fault))
            os.replace(temp, path)

    def set_fault(self, kind, params):
        self.validate(kind, params)
        with self.device._lock:
            fault = {"kind": kind, "params": dict(params), "scope": self.SCOPES[kind],
                     "createdAt": time.time(), "revision": uuid.uuid4().hex}
            self.device.fault_state[kind] = fault
            self.acks.pop(kind, None)
            self.device.persist()
            self._write(fault)
        self.device._notify()
        return next(f for f in self.list_faults() if f["kind"] == kind)

    def clear_fault(self, kind):
        with self.device._lock:
            if kind not in self.device.fault_state:
                raise KeyError(kind)
            del self.device.fault_state[kind]
            self.acks.pop(kind, None)
            self.device.persist()
            if self.directory:
                for suffix in (".json", ".applied"):
                    (self.directory / (kind + suffix)).unlink(missing_ok=True)
        self.device._notify()

    def list_faults(self):
        with self.device._lock:
            boot = self.device.booted_from().get("bootedAt")
            result = []
            for fault in self.device.fault_state.values():
                ack = self.acks.get(fault["kind"], {})
                if ack.get("revision") != fault["revision"] or ack.get("boot") != boot:
                    ack = {}
                local = fault["scope"] == "device"
                result.append({**copy.deepcopy(fault), "applied": local or bool(ack.get("ok")),
                               "detail": ack.get("detail"), "error": ack.get("error"),
                               "appliedAt": ack.get("appliedAt")})
            return result

    async def watch(self):
        while True:
            updated = {}
            if self.directory:
                for path in self.directory.glob("*.applied"):
                    try:
                        updated[path.stem] = json.loads(path.read_text())
                    except (OSError, ValueError):
                        continue
            if updated != self.acks:
                with self.device._lock:
                    self.acks = updated
                self.device._notify()
            await asyncio.sleep(1)
