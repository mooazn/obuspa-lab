"""Entry point: runs the device, its USP proxy socket, and the web UI together."""

from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from .console import ConsoleTail
from .core import VirtualDevice
from .shim import ShimServer
from .usptap import UspTap
from .wan import WanRelay
from .web import create_app

DEFAULT_SOCK = "/run/vdev/vdev.sock"
DEFAULT_STATE_FILE = "/run/vdev/device-state.json"


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("VDEV_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    socket_path = os.environ.get("VDEV_SOCK", DEFAULT_SOCK)
    state_file = os.environ.get("VDEV_STATE_FILE", DEFAULT_STATE_FILE)
    http_port = int(os.environ.get("VDEV_HTTP_PORT", "8080"))
    reboot_seconds = float(os.environ.get("VDEV_REBOOT_SECONDS", "5"))
    sdcard_dir = os.environ.get("VDEV_SDCARD_DIR", "/sdcard")

    device = VirtualDevice(
        state_file=state_file, reboot_seconds=reboot_seconds, sdcard_dir=sdcard_dir
    )

    shim = ShimServer(device, socket_path)
    await shim.start()

    # A reboot originating anywhere in the device (a controller calling
    # Device.Reboot(), the UI, or later a firmware activation) takes the
    # provider socket down through here.
    device.set_reboot_hook(shim.begin_downtime)

    # The agent container's console, written to the shared run volume
    console = ConsoleTail(os.environ.get("VDEV_AGENT_LOG", "/run/vdev/agent.log"))
    console.start()

    # The WAN port: the agent reaches its broker through this relay, so link
    # faults and latency are real to it. Fault acks from the agent container
    # are picked up by the registry's watcher.
    wan = WanRelay(device)
    await wan.start()
    fault_watch = asyncio.create_task(device.faults.watch())

    # The lab's protocol analyser, on the broker directly (not via the relay)
    tap = UspTap(os.environ.get("VDEV_BROKER_HOST", "broker"),
                 int(os.environ.get("VDEV_BROKER_PORT", "1883")))
    tap.start()

    app = create_app(device, shim, console=console, tap=tap)
    config = uvicorn.Config(
        app, host="0.0.0.0", port=http_port, log_level="info", access_log=False
    )
    server = uvicorn.Server(config)

    logging.getLogger(__name__).info("virtual CPE web UI on http://0.0.0.0:%d", http_port)

    try:
        await server.serve()
    finally:
        fault_watch.cancel()
        tap.stop()
        await wan.stop()
        await shim.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
