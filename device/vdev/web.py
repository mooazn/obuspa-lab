"""Web UI and HTTP API for the simulated device.

The UI is a view onto the same in-process device object the USP proxy serves,
so a change made by a controller shows up in the browser immediately, and a
change made in the browser is what the next controller Get returns.

Note the asymmetry: browser -> controller propagation is pull
based, because obuspa queries the device on demand. Pushing device-originated
changes to the controller needs USP notifications (Subscription / ValueChange)
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .console import asyncio_bridge
from .usptap import asyncio_bridge as tap_bridge
from .core import DeviceError, UnknownPathError, VirtualDevice
from .model import attach_client, detach_client, eject_sim, insert_sim, sim_inserted

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"


def _random_mac() -> str:
    """A locally-administered MAC, so it cannot collide with anything real."""
    return "02:00:5e:" + ":".join(f"{random.randint(0, 255):02x}" for _ in range(3))


def create_app(device: VirtualDevice, shim=None, console=None, tap=None) -> FastAPI:
    app = FastAPI(title="Virtual CPE", docs_url="/api/docs")

    clients: set[WebSocket] = set()
    changes: asyncio.Queue[None] = asyncio.Queue()

    # The device notifies from whichever thread made the change, so hop back
    # onto the event loop before touching asyncio state.
    loop = asyncio.get_event_loop()

    def on_change() -> None:
        try:
            loop.call_soon_threadsafe(changes.put_nowait, None)
        except RuntimeError:
            pass    # loop is shutting down

    device.add_listener(on_change)

    async def broadcaster() -> None:
        while True:
            await changes.get()
            if not clients:
                continue
            payload = json.dumps(device.snapshot())
            for ws in list(clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    clients.discard(ws)

    @app.on_event("startup")
    async def _start_broadcaster() -> None:
        app.state.broadcaster = asyncio.create_task(broadcaster())

    @app.on_event("shutdown")
    async def _stop_broadcaster() -> None:
        app.state.broadcaster.cancel()

    # ------------------------------------------------------------------

    # three.js, the scene module and anything else the page loads
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    async def get_state() -> dict:
        return device.snapshot()

    @app.post("/api/set")
    async def set_param(body: dict) -> dict:
        """Sets a parameter from the UI, using native JSON types."""
        path = body.get("path")
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        if "value" not in body:
            raise HTTPException(status_code=400, detail="value is required")

        try:
            device.set(path, body["value"])
        except UnknownPathError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DeviceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {"ok": True, "state": device.snapshot()}

    @app.post("/api/clients/attach")
    async def attach(body: dict) -> dict:
        """Associates a new client, as if something had just joined the WiFi.

        Both the AssociatedDevice row and the Hosts entry are created, and both
        are signalled to the agent - so a controller subscribed to
        ObjectCreation sees them appear without polling.
        """
        try:
            access_point = int(body.get("accessPoint", 1))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="accessPoint must be a number") from exc

        mac = body.get("mac") or _random_mac()
        hostname = body.get("hostname") or f"client-{mac[-5:].replace(':', '')}"

        try:
            instance = attach_client(
                device,
                access_point,
                mac=mac,
                hostname=hostname,
                ip=body.get("ip", ""),
                signal=int(body.get("signal", -50)),
            )
        except UnknownPathError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DeviceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {"ok": True, "instance": instance, "state": device.snapshot()}

    @app.post("/api/clients/detach")
    async def detach(body: dict) -> dict:
        """Disassociates a client, removing its Hosts entry too."""
        try:
            access_point = int(body["accessPoint"])
            instance = int(body["instance"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="accessPoint and instance are required"
            ) from exc

        try:
            detach_client(device, access_point, instance)
        except UnknownPathError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DeviceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {"ok": True, "state": device.snapshot()}

    # ------------------------------------------------------------------
    # Physical controls, driven by the 3D view
    # ------------------------------------------------------------------

    @app.post("/api/factory-reset")
    async def factory_reset(body: dict | None = None) -> dict:
        """The reset button held down: everything back to factory, then reboot."""
        if shim is None:
            raise HTTPException(status_code=503, detail="factory reset is not available")
        cause = (body or {}).get("cause") or "FactoryReset"
        try:
            shim.request_factory_reset(cause)
        except DeviceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "cause": cause}

    @app.post("/api/sim")
    async def sim(body: dict) -> dict:
        """Seats or pulls the SIM card."""
        action = body.get("action")
        try:
            if action == "insert":
                if sim_inserted(device):
                    raise HTTPException(status_code=409, detail="a SIM is already inserted")
                insert_sim(
                    device,
                    iccid=body.get("iccid") or "8944500000000000001",
                    imsi=body.get("imsi") or "234500000000001",
                    carrier=body.get("carrier") or "VirtualCell",
                    apn=body.get("apn") or "internet",
                )
            elif action == "eject":
                if not sim_inserted(device):
                    raise HTTPException(status_code=409, detail="no SIM to eject")
                eject_sim(device)
            else:
                raise HTTPException(status_code=400, detail="action must be insert or eject")
        except DeviceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "state": device.snapshot()}

    @app.post("/api/sdcard")
    async def sdcard(body: dict) -> dict:
        """Seats or ejects the SD card. Takes effect on the next boot."""
        action = body.get("action")
        try:
            if action == "insert":
                device.insert_sdcard()
            elif action == "eject":
                device.eject_sdcard()
            else:
                raise HTTPException(status_code=400, detail="action must be insert or eject")
        except DeviceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "state": device.snapshot()}

    @app.post("/api/reboot")
    async def reboot(body: dict | None = None) -> dict:
        """Reboots the device from the UI, as a controller's Device.Reboot() would."""
        if shim is None:
            raise HTTPException(status_code=503, detail="reboot is not available")

        cause = (body or {}).get("cause") or "LocalReboot"
        try:
            shim.request_reboot(cause)
        except DeviceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        return {"ok": True, "cause": cause}

    # ------------------------------------------------------------------
    # Faults: the lab perturbing the environment the firmware runs in
    # ------------------------------------------------------------------

    @app.get("/api/faults")
    async def list_faults() -> dict:
        return {"faults": device.faults.list_faults(), "kinds": device.faults.SCOPES}

    @app.post("/api/faults")
    async def set_fault(body: dict) -> dict:
        """Applies (or updates) a fault. One instance per kind."""
        try:
            fault = device.faults.set_fault(body.get("kind"), body.get("params") or {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "fault": fault}

    @app.delete("/api/faults/{kind}")
    async def clear_fault(kind: str) -> dict:
        try:
            device.faults.clear_fault(kind)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no such fault: {kind}") from exc
        return {"ok": True}

    @app.get("/api/wan")
    async def wan_state() -> dict:
        if device.wan is None:
            raise HTTPException(status_code=503, detail="WAN relay is not running")
        return device.wan.snapshot()

    # ------------------------------------------------------------------
    # Virtual HAL: a free key/value namespace vendor code may read
    # ------------------------------------------------------------------

    @app.get("/api/hal")
    async def hal_all() -> dict:
        return {"hal": dict(device.hal)}

    @app.get("/api/hal/{key:path}")
    async def hal_get(key: str) -> dict:
        value = device.hal_get(key)
        if value is None:
            raise HTTPException(status_code=404, detail=f"{key} is not set")
        return {"key": key, "value": value}

    @app.put("/api/hal/{key:path}")
    async def hal_put(key: str, body: dict) -> dict:
        if "value" not in body:
            raise HTTPException(status_code=400, detail="value is required")
        try:
            device.hal_set(key, None if body["value"] is None else str(body["value"]))
        except DeviceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "key": key, "value": device.hal_get(key)}

    @app.delete("/api/hal/{key:path}")
    async def hal_delete(key: str) -> dict:
        if device.hal_get(key) is None:
            raise HTTPException(status_code=404, detail=f"{key} is not set")
        device.hal_set(key, None)
        return {"ok": True}

    # ------------------------------------------------------------------
    # Serial console: what the agent container has printed
    # ------------------------------------------------------------------

    @app.get("/api/console")
    async def get_console(since: int = 0, tail: int | None = 200) -> dict:
        """Console lines with seq > since (most recent `tail` of them)."""
        if console is None:
            return {"lines": [], "latest": 0}
        return {"lines": console.entries(since=since, tail=tail), "latest": console.latest_seq}

    @app.websocket("/ws/console")
    async def console_ws(ws: WebSocket) -> None:
        """Streams console lines as they arrive, after a backlog."""
        await ws.accept()
        if console is None:
            await ws.close()
            return
        queue = asyncio_bridge(console, asyncio.get_running_loop())
        try:
            for entry in console.entries(tail=300):
                await ws.send_text(json.dumps(entry))
            while True:
                entry = await queue.get()
                await ws.send_text(json.dumps(entry))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            console.unsubscribe(queue.callback)

    # ------------------------------------------------------------------
    # USP timeline: every record seen on the broker, decoded
    # ------------------------------------------------------------------

    @app.get("/api/usp/timeline")
    async def usp_timeline(since: int = 0, limit: int | None = 200) -> dict:
        if tap is None:
            return {"entries": [], "latest": 0, "connected": False}
        return {"entries": tap.entries(since=since, limit=limit),
                "latest": tap.latest_seq, "connected": tap.connected}

    @app.get("/api/usp/notifications")
    async def usp_notifications(since: int = 0) -> dict:
        if tap is None:
            return {"notifications": [], "latest": 0}
        return {"notifications": tap.notifications(since=since), "latest": tap.latest_seq}

    @app.websocket("/ws/usp")
    async def usp_ws(ws: WebSocket) -> None:
        await ws.accept()
        if tap is None:
            await ws.close()
            return
        queue = tap_bridge(tap, asyncio.get_running_loop())
        try:
            for entry in tap.entries(limit=200):
                await ws.send_text(json.dumps(entry))
            while True:
                await ws.send_text(json.dumps(await queue.get()))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            tap.unsubscribe(queue.callback)

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        await ws.accept()
        clients.add(ws)
        try:
            await ws.send_text(json.dumps(device.snapshot()))
            while True:
                # We only push; reading keeps the connection alive and lets us
                # notice the client going away.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(ws)

    return app
