"""The lab's own USP controller, behind the web UI's Browse view.

A second controller identity (`self::vdev-lab`, Controller.2 in the agent's
factory configuration), distinct from the test suite's, so the agent can tell
the two apart: objects the lab creates are owned by Controller.2. Like the
USP tap, it connects to the broker directly rather than through the WAN
relay - it stands for a controller out on the network, not device hardware -
so WAN faults affect it exactly as they affect any other controller.

It connects on first use and stays connected; paho reconnects on its own if
the broker restarts.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from uspctl import UspController, UspError, UspTimeout

log = logging.getLogger(__name__)

ENDPOINT_ID = "self::vdev-lab"
REPLY_TOPIC = "/usp/lab"

# What a request that goes unanswered most likely means, for the UI
NO_ANSWER = (
    "no response from the agent. It may be rebooting or cut off from the broker, "
    "or its database may lack the lab controller (Controller.2) - the console shows "
    "which at boot."
)


class LabControllerError(Exception):
    """A request the lab controller could not complete; `status` suits HTTP."""

    def __init__(self, status: int, message: str, code: Optional[int] = None,
                 param_errors: Optional[list] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.param_errors = param_errors or []


class LabController:
    def __init__(self, broker_host: str, broker_port: int, timeout: float = 10.0):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.timeout = timeout
        self._controller: Optional[UspController] = None
        self._lock = threading.Lock()

    def _connected(self) -> UspController:
        with self._lock:
            if self._controller is None:
                controller = UspController(
                    broker_host=self.broker_host,
                    broker_port=self.broker_port,
                    controller_endpoint_id=ENDPOINT_ID,
                    controller_topic=REPLY_TOPIC,
                    timeout=self.timeout,
                    max_notifications=200,
                )
                try:
                    controller.connect()
                except (UspTimeout, OSError) as exc:
                    raise LabControllerError(503, f"cannot reach the broker: {exc}") from exc
                log.info("lab controller %s connected to %s:%d",
                         ENDPOINT_ID, self.broker_host, self.broker_port)
                self._controller = controller
            return self._controller

    def _call(self, method: str, *args, **kwargs) -> Any:
        controller = self._connected()
        try:
            return getattr(controller, method)(*args, **kwargs)
        except UspError as exc:
            raise LabControllerError(400, exc.message, code=exc.code,
                                     param_errors=exc.param_errors) from exc
        except UspTimeout as exc:
            raise LabControllerError(504, NO_ANSWER) from exc

    # Blocking; the web layer runs these off the event loop

    def get(self, path: str, depth: int = 0) -> dict[str, str]:
        return self._call("get", path, max_depth=depth)

    def set(self, params: dict[str, Any]) -> None:
        self._call("set", params)

    def add(self, object_path: str, params: Optional[dict[str, Any]] = None) -> str:
        return self._call("add", object_path, params or {})

    def delete(self, path: str) -> None:
        self._call("delete", path)

    def operate(self, command: str, inputs: Optional[dict[str, Any]] = None) -> dict[str, str]:
        return self._call("operate", command, inputs or {})

    def stop(self) -> None:
        with self._lock:
            if self._controller is not None:
                self._controller.disconnect()
                self._controller = None
