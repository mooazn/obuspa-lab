"""Other USP controllers, plugged into the agent at runtime.

A controller reached over its own MQTT connection needs three rows in the
agent: a Device.MQTT.Client (the connection to its broker), a
Device.LocalAgent.MTP (the agent listening on that connection) and a
Device.LocalAgent.Controller with one MTP (who the controller is and where it
listens). Plugging one in creates the three from a small definition and
enables the connection; unplugging deletes them. Every row carries the
definition's name as its Alias, which is how they are found again.

The rows are written over USP by the lab controller, like anything done from
the Browse view, and live in the agent's database: they survive reboots and
are removed by a factory reset.

A definition:
    {"name": "oktopus", "endpointId": "oktopusController",
     "broker": {"address": "mosquitto", "port": 1884},
     "controllerTopic": "oktopus/usp/v1/controller",
     "agentTopic": "",                     # optional; "" = assigned by the broker
     "role": "Device.LocalAgent.ControllerTrust.Role.1",   # optional
     "ui": "http://127.0.0.1:8090",        # optional; the lab UI links to it
     "description": "..."}                 # optional

The last two are the lab's, not the agent's: the device keeps them with its
other lab state.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .labctl import LabController, LabControllerError

log = logging.getLogger(__name__)

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Aliases of the rows the lab itself provisions
RESERVED = {"cpe-1", "lab"}
FULL_ACCESS = "Device.LocalAgent.ControllerTrust.Role.1"

CLIENTS = "Device.MQTT.Client."
MTPS = "Device.LocalAgent.MTP."
CONTROLLERS = "Device.LocalAgent.Controller."


def validate(definition: Any) -> dict:
    """Checks a definition and returns it normalised; raises ValueError."""
    if not isinstance(definition, dict):
        raise ValueError("definition must be an object")
    name = definition.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError("name must be 1-32 characters of a-z, 0-9 and '-'")
    if name in RESERVED:
        raise ValueError(f"{name!r} is used by the lab's own rows")
    endpoint = definition.get("endpointId")
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("endpointId is required")
    broker = definition.get("broker")
    if not isinstance(broker, dict) or not isinstance(broker.get("address"), str) \
            or not broker["address"] or type(broker.get("port")) is not int \
            or not 0 < broker["port"] < 65536:
        raise ValueError("broker needs an address and a port")
    topic = definition.get("controllerTopic")
    if not isinstance(topic, str) or not topic:
        raise ValueError("controllerTopic is required")
    agent_topic = definition.get("agentTopic", "")
    role = definition.get("role", FULL_ACCESS)
    if not isinstance(agent_topic, str) or not isinstance(role, str):
        raise ValueError("agentTopic and role must be strings")
    ui = definition.get("ui", "")
    if not isinstance(ui, str) or (ui and not ui.startswith(("http://", "https://"))):
        raise ValueError("ui must be an http(s) URL")
    description = definition.get("description", "")
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    return {"name": name, "endpointId": endpoint,
            "broker": {"address": broker["address"], "port": broker["port"]},
            "controllerTopic": topic, "agentTopic": agent_topic, "role": role,
            "ui": ui, "description": description}


# The controllers the lab itself provisions, by endpoint
KINDS = {"self::usp-controller": "tests", "self::vdev-lab": "lab"}


class ControllerPlugs:
    def __init__(self, lab: LabController, device=None):
        self.lab = lab
        self.device = device

    def _meta(self) -> dict:
        return self.device.controller_meta if self.device is not None else {}

    def _remember(self, name: str, meta: Optional[dict]) -> None:
        if self.device is None:
            return
        with self.device._lock:
            if meta:
                self.device.controller_meta[name] = meta
            else:
                self.device.controller_meta.pop(name, None)
            self.device.persist()

    def _with_alias(self, table: str, name: str) -> list[str]:
        """Instance paths ("Device.MQTT.Client.3.") in a table whose Alias is name."""
        values = self.lab.get(table + "*.Alias")
        return sorted(path[: -len("Alias")] for path, alias in values.items() if alias == name)

    def list(self) -> list[dict]:
        """Every controller the agent knows, with the state of its connection.

        kind is "tests" or "lab" for the lab's own, "plugged" for one plugged
        in here (its connection carries the same Alias), "other" otherwise.
        """
        aliases = self.lab.get(CONTROLLERS + "*.Alias")
        endpoints = self.lab.get(CONTROLLERS + "*.EndpointID")
        enabled = self.lab.get(CONTROLLERS + "*.Enable")
        references = self.lab.get(CONTROLLERS + "*.MTP.*.MQTT.Reference")
        clients = self.lab.get(CLIENTS, 2)
        meta = self._meta()
        listed = []
        for path, name in sorted(aliases.items(), key=lambda kv: _instance(kv[0])):
            controller = path[: -len("Alias")]
            endpoint = endpoints.get(controller + "EndpointID", "")
            reference = next((v for p, v in sorted(references.items()) if p.startswith(controller + "MTP.")), "")
            client = reference + "." if reference else ""
            plugged = bool(client) and clients.get(client + "Alias") == name and name not in RESERVED
            entry = {
                "name": name,
                "endpointId": endpoint,
                "kind": KINDS.get(endpoint, "plugged" if plugged else "other"),
                "controller": controller.rstrip("."),
                "controllerEnabled": enabled.get(controller + "Enable") == "true",
                "client": reference,
                "broker": f"{clients.get(client + 'BrokerAddress', '')}:{clients.get(client + 'BrokerPort', '')}"
                          if client else "",
                "enabled": clients.get(client + "Enable") == "true" if client else False,
                "status": clients.get(client + "Status", "") if client else "",
            }
            if plugged:
                entry.update(ui=meta.get(name, {}).get("ui", ""),
                             description=meta.get(name, {}).get("description", ""))
            listed.append(entry)
        return listed

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Connects or disconnects a plugged controller; False if none has that name."""
        clients = self._with_alias(CLIENTS, name)
        if name in RESERVED or not clients:
            return False
        self.lab.set({clients[0] + "Enable": "true" if enabled else "false"})
        return True

    def plug(self, definition: Any) -> dict:
        """Creates the rows for a controller, replacing any earlier ones of that name."""
        d = validate(definition)
        name = d["name"]
        self.unplug(name)
        try:
            client = self.lab.add(CLIENTS, {
                "Alias": name, "BrokerAddress": d["broker"]["address"],
                "BrokerPort": str(d["broker"]["port"]), "ProtocolVersion": "5.0",
                "TransportProtocol": "TCP/IP", "KeepAliveTime": "60", "Enable": "false",
            }).rstrip(".")
            mtp = self.lab.add(MTPS, {"Alias": name, "Protocol": "MQTT", "Enable": "true"})
            settings = {mtp + "MQTT.Reference": client}
            if d["agentTopic"]:
                settings[mtp + "MQTT.ResponseTopicConfigured"] = d["agentTopic"]
            self.lab.set(settings)
            controller = self.lab.add(CONTROLLERS, {
                "Alias": name, "EndpointID": d["endpointId"],
                "AssignedRole": d["role"], "Enable": "true",
            })
            cmtp = self.lab.add(controller + "MTP.", {"Alias": name, "Protocol": "MQTT", "Enable": "true"})
            self.lab.set({cmtp + "MQTT.Reference": client, cmtp + "MQTT.Topic": d["controllerTopic"]})
            self.lab.set({client + ".Enable": "true"})
        except LabControllerError:
            log.warning("plugging %s failed; removing what was created", name)
            try:
                self.unplug(name)
            except LabControllerError:
                pass
            raise
        self._remember(name, {"ui": d["ui"], "description": d["description"]})
        log.info("plugged controller %s (%s) via %s", name, d["endpointId"], client)
        return next((c for c in self.list() if c["name"] == name), {"name": name})

    def unplug(self, name: str) -> bool:
        """Deletes a controller's rows; False if there were none."""
        found = False
        # Controllers first: they reference the connection
        for table in (CONTROLLERS, MTPS, CLIENTS):
            for path in self._with_alias(table, name):
                self.lab.delete(path)
                found = True
        if found:
            log.info("unplugged controller %s", name)
        self._remember(name, None)
        return found


def _instance(path: str) -> int:
    """Instance number of "Device.LocalAgent.Controller.3.Alias", for ordering."""
    part = path[len(CONTROLLERS):].split(".", 1)[0]
    return int(part) if part.isdigit() else 0
