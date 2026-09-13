"""A tap on the broker: every USP record between agent and controller, decoded.

This is the lab's protocol analyser, not part of the device. It subscribes to
the broker directly (not through the WAN relay) so that it keeps seeing the
controller's side even while the device's link is down.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
    from google.protobuf.json_format import MessageToDict
    from .usp_proto import usp_msg_pb2 as usp
    from .usp_proto import usp_record_pb2 as record
    AVAILABLE = True
except ImportError as exc:      # bindings missing outside the image
    log.warning("USP tap unavailable: %s", exc)
    AVAILABLE = False


class UspTap:
    def __init__(self, host: str, port: int, topics: tuple[str, ...] = ("/usp/agent", "/usp/controller"),
                 history: int = 1000):
        self.host, self.port, self.topics = host, port, topics
        self._history: collections.deque = collections.deque(maxlen=history)
        self._seq = 0
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[dict], None]] = []
        self.connected = False
        self._client = None

    # ------------------------------------------------------------------

    def start(self) -> None:
        if not AVAILABLE:
            return
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id="vdev-observer", protocol=mqtt.MQTTv5)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = lambda *a, **k: setattr(self, "connected", False)
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(1, 10)
        try:
            self._client.connect_async(self.host, self.port, keepalive=30)
            self._client.loop_start()
        except Exception as exc:
            log.warning("USP tap could not start: %s", exc)

    def stop(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def entries(self, since: int = 0, limit: Optional[int] = None) -> list[dict]:
        with self._lock:
            items = [e for e in self._history if e["seq"] > since]
        return items[-limit:] if limit else items

    def notifications(self, since: int = 0) -> list[dict]:
        return [e for e in self.entries(since) if e.get("msg_type") == "NOTIFY"]

    @property
    def latest_seq(self) -> int:
        return self._seq

    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self.connected = reason_code == 0
        if self.connected:
            for topic in self.topics:
                client.subscribe(topic)
            log.info("USP tap observing %s on %s:%d", ", ".join(self.topics), self.host, self.port)

    def _on_message(self, client, userdata, message) -> None:
        entry = self._decode(message.topic, message.payload)
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._history.append(entry)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(entry)
            except Exception:
                pass

    def _decode(self, topic: str, payload: bytes) -> dict:
        entry: dict = {
            "ts": time.time(), "topic": topic, "bytes": len(payload),
            "direction": "controller->agent" if topic.endswith("/agent") else "agent->controller",
            "from_id": None, "to_id": None, "record_type": None,
            "msg_id": None, "msg_type": None, "summary": "", "body": None,
        }
        try:
            rec = record.Record()
            rec.ParseFromString(payload)
        except Exception as exc:
            entry["summary"] = f"undecodable record ({exc})"
            return entry

        entry.update(from_id=rec.from_id, to_id=rec.to_id, record_type=rec.WhichOneof("record_type"))

        if not rec.HasField("no_session_context"):
            entry["summary"] = f"{entry['record_type']} record"
            if rec.HasField("mqtt_connect"):
                entry["summary"] = f"MQTT connect record (subscribed to {rec.mqtt_connect.subscribed_topic})"
            elif rec.HasField("disconnect"):
                entry["summary"] = f"disconnect: {rec.disconnect.reason}"
            return entry

        try:
            msg = usp.Msg()
            msg.ParseFromString(rec.no_session_context.payload)
        except Exception as exc:
            entry["summary"] = f"undecodable USP message ({exc})"
            return entry

        entry["msg_id"] = msg.header.msg_id
        entry["msg_type"] = usp.Header.MsgType.Name(msg.header.msg_type)
        try:
            entry["body"] = MessageToDict(msg.body, preserving_proto_field_name=True)
        except Exception:
            entry["body"] = None
        entry["summary"] = self._summarise(msg)
        return entry

    @staticmethod
    def _summarise(msg) -> str:
        body = msg.body
        if body.HasField("error"):
            return f"Error {body.error.err_code}: {body.error.err_msg}"
        if body.HasField("request"):
            req = body.request
            kind = req.WhichOneof("req_type")
            if kind == "get":
                return "Get " + ", ".join(req.get.param_paths)
            if kind == "set":
                return "Set " + ", ".join(
                    f"{o.obj_path}{p.param}={p.value}" for o in req.set.update_objs for p in o.param_settings)
            if kind == "add":
                return "Add " + ", ".join(o.obj_path for o in req.add.create_objs)
            if kind == "delete":
                return "Delete " + ", ".join(req.delete.obj_paths)
            if kind == "operate":
                return f"Operate {req.operate.command}"
            if kind == "notify":
                n = req.notify
                which = n.WhichOneof("notification")
                if which == "event":
                    return f"Notify Event {n.event.obj_path}{n.event.event_name} {dict(n.event.params)}"
                if which == "oper_complete":
                    return f"Notify OperationComplete {n.oper_complete.obj_path}{n.oper_complete.command_name}"
                if which == "value_change":
                    return f"Notify ValueChange {n.value_change.param_path}={n.value_change.param_value}"
                if which == "obj_creation":
                    return f"Notify ObjectCreation {n.obj_creation.obj_path}"
                if which == "obj_deletion":
                    return f"Notify ObjectDeletion {n.obj_deletion.obj_path}"
                return f"Notify {which}"
            return kind or "request"
        if body.HasField("response"):
            resp = body.response
            kind = resp.WhichOneof("resp_type")
            if kind == "get_resp":
                count = sum(len(r.result_params) for q in resp.get_resp.req_path_results
                            for r in q.resolved_path_results)
                errors = [q for q in resp.get_resp.req_path_results if q.err_code]
                return f"GetResp {count} value(s)" + (f", {len(errors)} error(s)" if errors else "")
            return kind.replace("_resp", "Resp") if kind else "response"
        return ""


def asyncio_bridge(tap: UspTap, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()

    def on_entry(entry: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, entry)
        except RuntimeError:
            pass

    queue.callback = on_entry       # type: ignore[attr-defined]
    tap.subscribe(on_entry)
    return queue
