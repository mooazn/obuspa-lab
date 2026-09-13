"""The device's WAN port: an ordered TCP relay with link and latency faults."""
import asyncio
import os


class WanRelay:
    def __init__(self, device):
        self.device = device
        device.wan = self
        self.host = os.getenv("VDEV_BROKER_HOST", "broker")
        self.port = int(os.getenv("VDEV_BROKER_PORT", "1883"))
        self.listen = int(os.getenv("VDEV_WAN_PORT", "1883"))
        self.delay = float(os.getenv("VDEV_WAN_LINKDOWN_DELAY", "1"))
        self.connections = set()
        self.tasks = set()
        self.link_up = True
        self.reasons = []
        self.latency = 0
        self.server = None
        self.monitor = None

    def snapshot(self):
        return {"linkUp": self.link_up, "reasons": list(self.reasons),
                "connections": len(self.connections), "latencyMs": self.latency,
                "upstream": f"{self.host}:{self.port}", "listen": f"0.0.0.0:{self.listen}"}

    def desired(self):
        reasons = []
        with self.device._lock:
            faults = self.device.fault_state
            if "wan_down" in faults:
                reasons.append("wan_down")
            if self.device.safe_get("Device.IP.Interface.1.Status") != "Up":
                reasons.append("IP interface down")
            if not self.device.safe_get("Device.Ethernet.Interface.1.Enable"):
                reasons.append("Ethernet interface disabled")
            latency = faults.get("wan_latency", {}).get("params", {}).get("ms", 0)
        return reasons, latency

    async def start(self):
        self.reasons, self.latency = self.desired()
        self.link_up = not self.reasons
        self.server = await asyncio.start_server(self.handle, "0.0.0.0", self.listen)
        self.monitor = asyncio.create_task(self.watch())

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        pending = list(self.tasks)
        if self.monitor:
            pending.append(self.monitor)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def watch(self):
        down_at = None
        loop = asyncio.get_running_loop()
        while True:
            reasons, latency = self.desired()
            before = self.snapshot()
            self.reasons, self.latency = reasons, latency
            if reasons:
                if down_at is None:
                    down_at = loop.time() + self.delay
                if self.link_up and loop.time() >= down_at:
                    self.link_up = False
                    for pair in list(self.connections):
                        for writer in pair:
                            writer.close()
            else:
                down_at = None
                self.link_up = True
            if self.snapshot() != before:
                self.device._notify()
            await asyncio.sleep(0.05)

    async def pump(self, reader, writer):
        while data := await reader.read(65536):
            if self.latency:
                await asyncio.sleep(self.latency / 1000)
            writer.write(data)
            await writer.drain()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        pair = None
        pumps = []
        try:
            if not self.link_up:
                return
            remote, upstream = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 5)
            if not self.link_up:
                return
            pair = (writer, upstream)
            self.connections.add(pair)
            self.device._notify()
            pumps = [asyncio.create_task(self.pump(reader, upstream)),
                     asyncio.create_task(self.pump(remote, writer))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            writer.close()
            if upstream:
                upstream.close()
            if pair:
                self.connections.discard(pair)
                self.device._notify()
            self.tasks.discard(task)
