"""A minimal USP Controller speaking USP over MQTT.

Enough of a controller to drive tests and poke at the simulated device: Get and
Set, with request/response correlation by msg_id. It talks to obuspa exactly as
a real controller would - protobuf USP Records over MQTT 5 - so anything proved
here is proved against the real agent implementation, not a mock.

Deliberately not built on obuspa-test-controller: that replays messages from
files and prints responses, which is awkward to assert against.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from typing import Any, Callable, Iterable, Optional

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from .proto import usp_msg_pb2 as usp
from .proto import usp_record_pb2 as record

log = logging.getLogger(__name__)


# USP's NotifType names, keyed by the Notify message's oneof field, so that a
# notification's `type` matches what was passed to `subscribe`
NOTIF_TYPES = {
    "event": "Event",
    "value_change": "ValueChange",
    "oper_complete": "OperationComplete",
    "obj_creation": "ObjectCreation",
    "obj_deletion": "ObjectDeletion",
    "on_board_req": "OnBoardRequest",
}


class UspError(Exception):
    """A USP Error message came back instead of a response."""

    def __init__(self, code: int, message: str, param_errors: Optional[list] = None):
        super().__init__(f"USP error {code}: {message}")
        self.code = code
        self.message = message
        self.param_errors = param_errors or []


class UspTimeout(Exception):
    """No response arrived within the timeout."""


def to_usp_text(value: Any) -> str:
    """Python value -> the textual form USP puts on the wire."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class UspController:
    def __init__(
        self,
        broker_host: str = "localhost",
        broker_port: int = 1883,
        agent_endpoint_id: str = "os::vdev-001",
        controller_endpoint_id: str = "self::usp-controller",
        agent_topic: str = "/usp/agent",
        controller_topic: str = "/usp/controller",
        usp_version: str = "1.3",
        timeout: float = 15.0,
        max_notifications: Optional[int] = None,
    ):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.agent_endpoint_id = agent_endpoint_id
        self.controller_endpoint_id = controller_endpoint_id
        self.agent_topic = agent_topic
        self.controller_topic = controller_topic
        self.usp_version = usp_version
        self.timeout = timeout
        # Keeps only the most recent notifications when set, for a long-lived
        # controller; a test run keeps them all
        self.max_notifications = max_notifications

        self._pending: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()
        self._connected = threading.Event()

        # Notifications pushed by the agent (Boot!, OperationComplete, ...).
        # Kept as a list rather than a queue so several waiters can each scan
        # the history without consuming each other's events.
        self._notifications: list[dict] = []
        self._notification_event = threading.Condition()

        # Subscription instance paths created by this controller, so they can
        # be cleaned up rather than left in the agent's database
        self._subscriptions: list[str] = []

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"usp-controller-{uuid.uuid4().hex[:8]}",
            protocol=mqtt.MQTTv5,
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> "UspController":
        self._client.connect(self.broker_host, self.broker_port, keepalive=60)
        self._client.loop_start()
        if not self._connected.wait(timeout=self.timeout):
            raise UspTimeout(f"could not connect to broker at {self.broker_host}:{self.broker_port}")
        return self

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def __enter__(self) -> "UspController":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            log.error("MQTT connect failed: %s", reason_code)
            return
        client.subscribe(self.controller_topic)
        log.info("controller subscribed to %s", self.controller_topic)
        self._connected.set()

    def _on_message(self, client, userdata, message) -> None:
        try:
            rec = record.Record()
            rec.ParseFromString(message.payload)
        except Exception:
            log.warning("undecodable record on %s", message.topic)
            return

        # The agent also emits connect records; only session-less USP payloads
        # carry messages we care about.
        if not rec.HasField("no_session_context"):
            log.debug("ignoring record type %s", rec.WhichOneof("record_type"))
            return

        msg = usp.Msg()
        try:
            msg.ParseFromString(rec.no_session_context.payload)
        except Exception:
            log.warning("undecodable USP message from %s", rec.from_id)
            return

        # The agent sends notifications as requests, not responses
        if msg.body.HasField("request") and msg.body.request.HasField("notify"):
            self._handle_notify(msg, rec.from_id)
            return

        msg_id = msg.header.msg_id
        with self._lock:
            waiter = self._pending.get(msg_id)

        if waiter is None:
            log.debug("unsolicited message %s (%s)", msg_id, msg.header.msg_type)
            return

        waiter.put(msg)

    def _handle_notify(self, msg: usp.Msg, from_id: str) -> None:
        """Records an incoming notification and acknowledges it."""
        notify = msg.body.request.notify
        kind = notify.WhichOneof("notification")

        record_entry: dict = {
            "type": NOTIF_TYPES.get(kind, kind),
            "subscription_id": notify.subscription_id,
            "msg_id": msg.header.msg_id,
        }

        if kind == "event":
            record_entry["obj_path"] = notify.event.obj_path
            record_entry["event_name"] = notify.event.event_name
            record_entry["params"] = dict(notify.event.params)
        elif kind == "value_change":
            record_entry["param_path"] = notify.value_change.param_path
            record_entry["param_value"] = notify.value_change.param_value
        elif kind == "oper_complete":
            complete = notify.oper_complete
            record_entry["obj_path"] = complete.obj_path
            record_entry["command_name"] = complete.command_name
            record_entry["command_key"] = complete.command_key
            if complete.HasField("req_output_args"):
                record_entry["output_args"] = dict(complete.req_output_args.output_args)
            elif complete.HasField("cmd_failure"):
                record_entry["err_code"] = complete.cmd_failure.err_code
                record_entry["err_msg"] = complete.cmd_failure.err_msg
        elif kind == "obj_creation":
            record_entry["obj_path"] = notify.obj_creation.obj_path
        elif kind == "obj_deletion":
            record_entry["obj_path"] = notify.obj_deletion.obj_path

        log.info("notification: %s %s", kind, record_entry.get("event_name")
                 or record_entry.get("command_name") or record_entry.get("obj_path") or "")

        with self._notification_event:
            self._notifications.append(record_entry)
            if self.max_notifications and len(self._notifications) > self.max_notifications:
                del self._notifications[: -self.max_notifications]
            self._notification_event.notify_all()

        if notify.send_resp:
            response = usp.Msg()
            response.header.msg_id = msg.header.msg_id
            response.header.msg_type = usp.Header.NOTIFY_RESP
            response.body.response.notify_resp.subscription_id = notify.subscription_id
            self._publish(response)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def clear_notifications(self) -> None:
        with self._notification_event:
            self._notifications.clear()

    @property
    def notifications(self) -> list[dict]:
        with self._notification_event:
            return list(self._notifications)

    def wait_for_notification(
        self, match: Callable[[dict], bool], timeout: Optional[float] = None
    ) -> dict:
        """Blocks until a notification satisfying `match` has arrived.

        Each notification is a dict whose `type` is the NotifType it was
        subscribed with (Event, ValueChange, OperationComplete,
        ObjectCreation, ObjectDeletion), plus the fields of that type:
        `obj_path`/`event_name`/`params` for an Event, `command_key` and
        `output_args` or `err_code` for an OperationComplete, and so on.

        Notifications already received are considered, so there is no race
        between triggering something and starting to wait for it.
        """
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)

        with self._notification_event:
            while True:
                for entry in self._notifications:
                    if match(entry):
                        return entry

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UspTimeout(
                        f"no matching notification within {timeout or self.timeout}s "
                        f"(saw {[n['type'] for n in self._notifications]})"
                    )
                self._notification_event.wait(remaining)

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------

    def _publish(self, msg: usp.Msg) -> None:
        """Wraps a USP message in a Record and publishes it to the agent."""
        rec = record.Record()
        rec.version = self.usp_version
        rec.to_id = self.agent_endpoint_id
        rec.from_id = self.controller_endpoint_id
        rec.payload_security = record.Record.PLAINTEXT
        rec.no_session_context.payload = msg.SerializeToString()

        props = Properties(PacketTypes.PUBLISH)
        props.ResponseTopic = self.controller_topic
        props.ContentType = "application/vnd.bbf.usp.msg"

        self._client.publish(
            self.agent_topic, rec.SerializeToString(), qos=0, properties=props
        )

    def _send(self, msg: usp.Msg) -> usp.Msg:
        """Publishes a USP message and blocks for the matching response."""
        msg_id = msg.header.msg_id
        waiter: queue.Queue = queue.Queue(maxsize=1)

        with self._lock:
            self._pending[msg_id] = waiter

        try:
            self._publish(msg)

            try:
                response = waiter.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise UspTimeout(
                    f"no response to {usp.Header.MsgType.Name(msg.header.msg_type)} "
                    f"(msg_id={msg_id}) within {self.timeout}s"
                ) from exc
        finally:
            with self._lock:
                self._pending.pop(msg_id, None)

        if response.body.HasField("error"):
            err = response.body.error
            raise UspError(
                err.err_code,
                err.err_msg,
                [(p.param_path, p.err_code, p.err_msg) for p in err.param_errs],
            )

        return response

    @staticmethod
    def _new_msg(msg_type) -> usp.Msg:
        msg = usp.Msg()
        msg.header.msg_id = uuid.uuid4().hex[:16]
        msg.header.msg_type = msg_type
        return msg

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def get(self, paths: Iterable[str] | str, max_depth: int = 0) -> dict[str, str]:
        """Gets parameters, returning a flat {resolved_path: value} map.

        Raises UspError if the agent reports a per-path failure, so a typo in a
        path fails loudly rather than returning an empty dict.
        """
        if isinstance(paths, str):
            paths = [paths]
        paths = list(paths)

        msg = self._new_msg(usp.Header.GET)
        msg.body.request.get.param_paths.extend(paths)
        msg.body.request.get.max_depth = max_depth

        response = self._send(msg)
        get_resp = response.body.response.get_resp

        values: dict[str, str] = {}
        for req_result in get_resp.req_path_results:
            if req_result.err_code != 0:
                raise UspError(
                    req_result.err_code,
                    f"{req_result.requested_path}: {req_result.err_msg}",
                )
            for resolved in req_result.resolved_path_results:
                for name, value in resolved.result_params.items():
                    values[resolved.resolved_path + name] = value

        return values

    def get_one(self, path: str) -> str:
        """Gets exactly one parameter, returning its value."""
        values = self.get(path)
        if path not in values:
            if len(values) == 1:
                return next(iter(values.values()))
            raise KeyError(f"{path} not present in response: {sorted(values)}")
        return values[path]

    def set(self, updates: dict[str, Any], allow_partial: bool = False) -> None:
        """Sets one or more parameters given full instantiated paths."""
        msg = self._new_msg(usp.Header.SET)
        msg.body.request.set.allow_partial = allow_partial

        # USP groups parameter writes by the object that contains them
        by_object: dict[str, dict[str, Any]] = {}
        for path, value in updates.items():
            obj_path, _, param = path.rpartition(".")
            by_object.setdefault(obj_path + ".", {})[param] = value

        for obj_path, params in by_object.items():
            update_obj = msg.body.request.set.update_objs.add()
            update_obj.obj_path = obj_path
            for param, value in params.items():
                setting = update_obj.param_settings.add()
                setting.param = param
                setting.value = to_usp_text(value)
                setting.required = True

        response = self._send(msg)

        # A Set that fails on the device comes back as a per-object failure
        # rather than a top-level Error, so it has to be unpacked.
        for obj_result in response.body.response.set_resp.updated_obj_results:
            status = obj_result.oper_status
            if status.HasField("oper_failure"):
                failure = status.oper_failure
                param_errors = [
                    (pe.param, pe.err_code, pe.err_msg)
                    for inst in failure.updated_inst_failures
                    for pe in inst.param_errs
                ]
                detail = "; ".join(f"{p}: {m}" for p, _, m in param_errors)
                raise UspError(
                    failure.err_code,
                    f"{obj_result.requested_path}: {failure.err_msg}"
                    + (f" ({detail})" if detail else ""),
                    param_errors,
                )

    def add(self, obj_path: str, params: Optional[dict[str, Any]] = None) -> str:
        """Creates an object instance, returning the instantiated path."""
        msg = self._new_msg(usp.Header.ADD)
        msg.body.request.add.allow_partial = False

        create = msg.body.request.add.create_objs.add()
        create.obj_path = obj_path if obj_path.endswith(".") else obj_path + "."
        for param, value in (params or {}).items():
            setting = create.param_settings.add()
            setting.param = param
            setting.value = to_usp_text(value)
            setting.required = True

        response = self._send(msg)

        for result in response.body.response.add_resp.created_obj_results:
            status = result.oper_status
            if status.HasField("oper_failure"):
                raise UspError(
                    status.oper_failure.err_code,
                    f"{result.requested_path}: {status.oper_failure.err_msg}",
                )
            return status.oper_success.instantiated_path

        raise UspError(0, f"no result returned when adding {obj_path}")

    def delete(self, obj_paths: Iterable[str] | str, allow_partial: bool = False) -> None:
        """Deletes object instances."""
        if isinstance(obj_paths, str):
            obj_paths = [obj_paths]

        msg = self._new_msg(usp.Header.DELETE)
        msg.body.request.delete.allow_partial = allow_partial
        msg.body.request.delete.obj_paths.extend(
            path if path.endswith(".") else path + "." for path in obj_paths
        )

        response = self._send(msg)

        for result in response.body.response.delete_resp.deleted_obj_results:
            status = result.oper_status
            if status.HasField("oper_failure"):
                raise UspError(
                    status.oper_failure.err_code,
                    f"{result.requested_path}: {status.oper_failure.err_msg}",
                )

    def operate(
        self,
        command: str,
        inputs: Optional[dict[str, Any]] = None,
        command_key: str = "",
        send_resp: bool = True,
    ) -> dict[str, str]:
        """Invokes a USP command.

        For a synchronous command the output arguments are returned directly.
        An asynchronous command returns an empty dict here - its result arrives
        later as an OperationComplete notification, which requires a
        subscription (see `subscribe`).
        """
        msg = self._new_msg(usp.Header.OPERATE)
        msg.body.request.operate.command = command
        msg.body.request.operate.command_key = command_key or uuid.uuid4().hex[:8]
        msg.body.request.operate.send_resp = send_resp
        for name, value in (inputs or {}).items():
            msg.body.request.operate.input_args[name] = to_usp_text(value)

        response = self._send(msg)

        for result in response.body.response.operate_resp.operation_results:
            if result.HasField("cmd_failure"):
                raise UspError(
                    result.cmd_failure.err_code,
                    f"{result.executed_command}: {result.cmd_failure.err_msg}",
                )
            if result.HasField("req_output_args"):
                return dict(result.req_output_args.output_args)
            # req_obj_path means an async command was accepted and is running
            return {}

        return {}

    def subscribe(
        self,
        notif_type: str,
        reference_list: str,
        subscription_id: Optional[str] = None,
        persistent: bool = False,
    ) -> str:
        """Subscribes to notifications, returning the subscription ID.

        `notif_type` is one of Event, ValueChange, ObjectCreation,
        ObjectDeletion, OperationComplete.
        """
        subscription_id = subscription_id or f"sub-{uuid.uuid4().hex[:8]}"
        instance_path = self.add(
            "Device.LocalAgent.Subscription.",
            {
                "ID": subscription_id,
                "NotifType": notif_type,
                "ReferenceList": reference_list,
                "Persistent": persistent,
                "Enable": True,
            },
        )
        self._subscriptions.append(instance_path)
        return subscription_id

    def remove_subscriptions(self) -> None:
        """Deletes every subscription this controller created.

        Worth doing at the end of a test run: persistent subscriptions live in
        the agent's database, so without this they accumulate across runs and
        the agent keeps retrying notifications at a controller that has gone.
        """
        for path in list(self._subscriptions):
            try:
                self.delete(path)
            except Exception as exc:
                log.warning("could not delete subscription %s: %s", path, exc)
        self._subscriptions.clear()
