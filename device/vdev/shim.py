"""USP-facing adapter: serves the data model to the obuspa C plug-in.

This is the only module that knows about USP's conventions - notably that every
value crosses the wire as text, with booleans spelled "true"/"false". The
device core deals in native Python values; the conversion happens here.

Protocol: newline-delimited JSON over a Unix domain socket, one request object
and one response object per line. See ../../plugin/vdev_plugin.c for the
matching end.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from .core import DeviceError, UnknownPathError, VirtualDevice
from .model import ObjectDef, ParamDef, TYPE_BOOL, TYPE_INT, TYPE_UINT

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# USP text codec
# ----------------------------------------------------------------------


def encode(param: ParamDef, value: Any) -> str:
    """Native value -> USP textual representation."""
    if param.type == TYPE_BOOL:
        return "true" if value else "false"
    return str(value)


def decode(param: ParamDef, text: str) -> Any:
    """USP textual representation -> native value."""
    if param.type == TYPE_BOOL:
        lowered = text.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        raise DeviceError(f"invalid boolean value: {text!r}")

    if param.type in (TYPE_UINT, TYPE_INT):
        try:
            number = int(text)
        except ValueError as exc:
            raise DeviceError(f"invalid integer: {text!r}") from exc
        if param.type == TYPE_UINT and number < 0:
            raise DeviceError(f"value must not be negative: {text!r}")
        return number

    return text


# ----------------------------------------------------------------------
# Request handling
# ----------------------------------------------------------------------


class ShimServer:
    """Serves the device's data model to the obuspa plug-in."""

    def __init__(self, device: VirtualDevice, socket_path: str):
        self.device = device
        self.socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # Event-stream handlers never return on their own, so they have to be
        # cancelled explicitly or stop() would wait for them forever.
        self._event_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()

        # A socket left behind by an unclean shutdown would block bind()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self.socket_path
        )
        # The agent runs as a different user in some setups
        os.chmod(self.socket_path, 0o666)
        log.info("data model proxy listening on %s", self.socket_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()

            # The agent's event subscription blocks forever by design; without
            # cancelling it, wait_closed() would never return.
            for task in list(self._event_tasks):
                task.cancel()

            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                log.warning("timed out waiting for connections to close")

            self._server = None

        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    response = {"ok": False, "error": f"malformed request: {exc}"}
                else:
                    # The event stream takes the connection over for good
                    if request.get("op") == "events":
                        await self._stream_events(reader, writer)
                        return
                    if request.get("op") == "hal_watch":
                        await self._stream_hal(reader, writer, request.get("prefix", ""))
                        return
                    response = self.dispatch(request)

                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    async def _stream_events(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Holds the connection open, pushing device events to the agent.

        The device emits from whatever thread the work happened on, so events
        are marshalled back onto the event loop before being written.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict] = asyncio.Queue()

        task = asyncio.current_task()
        if task is not None:
            self._event_tasks.add(task)

        def on_event(event: dict) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass    # loop is shutting down

        closed = asyncio.create_task(reader.read())
        self.device.add_event_listener(on_event)
        self.device.set_agent_connected(True)
        log.info("agent subscribed to device events")

        try:
            while True:
                pending = asyncio.create_task(queue.get())
                try:
                    done, _ = await asyncio.wait([pending, closed], return_when=asyncio.FIRST_COMPLETED)
                    if closed in done:
                        break
                    event = pending.result()
                finally:
                    pending.cancel()
                writer.write(json.dumps(event).encode() + b"\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            closed.cancel()
            self.device.remove_event_listener(on_event)
            self.device.set_agent_connected(False)
            if task is not None:
                self._event_tasks.discard(task)
            writer.close()
            log.info("agent event subscription closed")

    def _op_hal_dm_add(self, request: dict) -> dict:
        path = request.get("path") or ""
        instance = self.device.add(path, internal=True)
        log.info("hal: added %s%d", path if path.endswith(".") else path + ".", instance)
        return {"ok": True, "instance": instance}

    def _op_hal_dm_delete(self, request: dict) -> dict:
        path = request.get("path") or ""
        self.device.delete(path, internal=True)
        log.info("hal: deleted %s", path)
        return {"ok": True}

    def _op_hal_set(self, request):
        self.device.hal_set(request["key"], request["value"])
        return {"ok": True}

    async def _stream_hal(self, reader, writer, prefix):
        if not isinstance(prefix, str):
            return
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        task = asyncio.current_task()
        self._event_tasks.add(task)
        def changed(entry):
            if entry["key"].startswith(prefix):
                loop.call_soon_threadsafe(queue.put_nowait, entry)
        initial = self.device.add_hal_listener(changed)
        closed = asyncio.create_task(reader.read())
        try:
            for key, value in initial.items():
                if key.startswith(prefix):
                    writer.write(json.dumps({"key": key, "value": value}).encode() + b"\n")
            await writer.drain()
            while True:
                pending = asyncio.create_task(queue.get())
                try:
                    done, _ = await asyncio.wait([pending, closed], return_when=asyncio.FIRST_COMPLETED)
                    if closed in done:
                        break
                    writer.write(json.dumps(pending.result()).encode() + b"\n")
                    await writer.drain()
                finally:
                    pending.cancel()
        finally:
            closed.cancel()
            self.device.remove_hal_listener(changed)
            self._event_tasks.discard(task)

    # ------------------------------------------------------------------
    # Reboot
    # ------------------------------------------------------------------

    def request_reboot(self, cause: str = "RemoteReboot") -> None:
        """Starts a reboot. Returns immediately so the caller can be answered."""
        if self._loop is None:
            raise DeviceError("device is not serving yet")
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._delayed_reboot(cause))
        )

    async def _delayed_reboot(self, cause: str) -> None:
        # Let the RPC response reach the agent before anything goes away
        await asyncio.sleep(0.2)
        self.device.reboot(cause)

    def request_factory_reset(self, cause: str = "FactoryReset") -> None:
        """Starts a factory reset. Returns immediately, like request_reboot."""
        if self._loop is None:
            raise DeviceError("device is not serving yet")
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._delayed_factory_reset(cause))
        )

    async def _delayed_factory_reset(self, cause: str) -> None:
        await asyncio.sleep(0.2)
        self.device.factory_reset(cause)

    def begin_downtime(self) -> None:
        """The device's reboot hook: takes the socket away for a while.

        Dropping the socket is what makes this realistic. obuspa loses its data
        model provider and exits, the container restarts it, and the controller
        sees a genuine disconnect, reconnect and Boot! event produced by real
        agent code rather than a simulated one.
        """
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._downtime())
        )

    async def _downtime(self) -> None:
        await self.stop()
        log.info("device is down for %.1fs", self.device.reboot_seconds)
        await asyncio.sleep(self.device.reboot_seconds)
        await self.start()
        self.device.complete_boot()

    # ------------------------------------------------------------------

    def dispatch(self, request: dict) -> dict:
        """Routes one request. Never raises - errors come back as JSON."""
        op = request.get("op")
        handler = {
            "hal_get": lambda r: {"ok": True, "value": self.device.hal_get(r["key"])},
            "hal_set": self._op_hal_set,
            # Data model operations from the firmware's HAL backend. These act
            # as the hardware layer: they may create, set and delete what a
            # controller may not, and they bypass controller-facing validation
            # the way a driver reporting state does.
            "hal_dm_add": self._op_hal_dm_add,
            "hal_dm_set": lambda r: self._op_set(r, internal=True),
            "hal_dm_delete": self._op_hal_dm_delete,
            "model": self._op_model,
            "instances": self._op_instances,
            "get": self._op_get,
            "set": self._op_set,
            "add": self._op_add,
            "delete": self._op_delete,
            "operate": self._op_operate,
            "reboot": self._op_reboot,
            "factory_reset": self._op_factory_reset,
        }.get(op)

        if handler is None:
            return {"ok": False, "error": f"unknown op: {op!r}"}

        try:
            return handler(request)
        except DeviceError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # never let the agent hang on our bug
            log.exception("unhandled error serving op=%s", op)
            return {"ok": False, "error": f"internal error: {exc}"}

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def _op_model(self, request: dict) -> dict:
        """Describes the data model so the plug-in can register it."""
        objects = []
        params = []

        for obj in self.device.model:
            objects.append({"path": obj.path, "writable": obj.writable})
            for param in obj.params:
                params.append(
                    {
                        "path": f"{obj.path}.{param.name}",
                        "type": param.type,
                        "writable": param.is_writable,
                    }
                )

        commands = [
            {
                "path": command.path,
                "async": command.is_async,
                "input": command.input_args,
                "output": command.output_args,
                "max_concurrency": command.max_concurrency,
            }
            for command in self.device.commands
        ]

        events = [
            {"path": event.path, "args": event.args} for event in self.device.events
        ]

        return {
            "ok": True,
            "objects": objects,
            "params": params,
            "commands": commands,
            "events": events,
        }

    def _op_instances(self, request: dict) -> dict:
        return {"ok": True, "instances": self.device.instances()}

    def _op_get(self, request: dict) -> dict:
        paths = request.get("paths") or []
        values: dict[str, str] = {}

        for path in paths:
            try:
                resolved = self.device.resolve(path)
                if resolved.param is None:
                    continue
                values[path] = encode(resolved.param, self.device.get(path))
            except UnknownPathError:
                # Per obuspa's contract, unreadable parameters are omitted
                # rather than failing the whole batch.
                log.warning("get: unknown path %s", path)

        return {"ok": True, "values": values}

    def _op_set(self, request: dict, internal: bool = False) -> dict:
        raw = request.get("params") or {}
        ordered_paths = list(raw.keys())

        updates: dict[str, Any] = {}
        for path in ordered_paths:
            resolved = self.device.resolve(path)
            if resolved.param is None:
                raise DeviceError(f"not a parameter: {path}")
            try:
                updates[path] = decode(resolved.param, raw[path])
            except DeviceError as exc:
                return {
                    "ok": False,
                    "error": str(exc),
                    "failure_index": ordered_paths.index(path),
                }

        try:
            self.device.set_many(updates, internal=internal)
        except DeviceError as exc:
            failed_path = getattr(exc, "path", None)
            index = (
                ordered_paths.index(failed_path)
                if failed_path in ordered_paths
                else -1
            )
            return {"ok": False, "error": str(exc), "failure_index": index}

        log.info("set %s", ", ".join(f"{k}={v!r}" for k, v in updates.items()))
        return {"ok": True}

    def _op_add(self, request: dict) -> dict:
        path = request.get("path") or ""
        instance = self.device.add(path)
        log.info("added %s%d", path if path.endswith(".") else path + ".", instance)
        return {"ok": True, "instance": instance}

    def _op_delete(self, request: dict) -> dict:
        path = request.get("path") or ""
        self.device.delete(path)
        log.info("deleted %s", path)
        return {"ok": True}

    def _op_operate(self, request: dict) -> dict:
        """Runs a USP command.

        Async commands return as soon as they have started; their result
        arrives later on the event stream.
        """
        path = request.get("path") or ""
        inputs = request.get("input") or {}

        if request.get("async"):
            request_id = request.get("request_id")
            if not isinstance(request_id, int):
                raise DeviceError("request_id is required for asynchronous commands")
            self.device.operate_async(path, request_id, inputs)
            log.info("started %s (request %d)", path, request_id)
            return {"ok": True}

        outputs = self.device.operate_sync(path, inputs)
        log.info("ran %s", path)
        return {"ok": True, "output": {k: str(v) for k, v in outputs.items()}}

    def _op_reboot(self, request: dict) -> dict:
        cause = request.get("cause") or "RemoteReboot"
        self.request_reboot(cause)
        return {"ok": True}

    def _op_factory_reset(self, request: dict) -> dict:
        """A controller called Device.FactoryReset(); obuspa has already wiped
        its own database and is about to exit. The device wipes its side."""
        self.request_factory_reset(request.get("cause") or "FactoryReset")
        return {"ok": True}
