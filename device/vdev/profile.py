"""Device profiles: a board described as a file.

A profile is a TOML file listing the instances a board has at factory reset,
in TR-181's own names:

    [board]
    name = "reference-gateway"
    wan = "Device.IP.Interface.1"

    [[WiFi.Radio]]                      # Device.WiFi.Radio.1
    Name = "wl0"
    Channel = 6

    [[IP.Interface]]                    # Device.IP.Interface.1
    Name = "wan"
      [[IP.Interface.IPv4Address]]      # Device.IP.Interface.1.IPv4Address.1
      IPAddress = "100.64.1.23"

Each array-of-tables entry is one instance, numbered in order from 1; an
array of tables inside an instance is a child object. A table inside an
instance is a group of dotted parameter names (`Security.ModeEnabled`). An
empty table such as `[Hosts]` declares an area with no factory instances.

The areas a profile mentions (`WiFi`, `Cellular`, ...) are the parts the
board has: every object and command the lab simulates in those areas is
present, with its behaviour, and nothing from the others. Parameters a
profile leaves out take the object's defaults.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any, Optional

from .model import (
    COMMANDS,
    EVENTS,
    MODEL,
    TYPE_BOOL,
    TYPE_INT,
    TYPE_UINT,
    TYPE_ULONG,
    CommandDef,
    EventDef,
    ObjectDef,
)

DEFAULT_PROFILE = "reference-gateway"
PROFILE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profiles")

# An instance reference such as "Device.WiFi.Radio.2"
_REFERENCE = re.compile(r"^Device\.(?:[A-Za-z][\w]*\.)+\d+$")


class ProfileError(ValueError):
    """A profile that cannot describe a board; the message names where."""


@dataclass
class Profile:
    name: str
    description: str
    source: str
    wan: Optional[str]
    lan: Optional[str]
    objects: list[ObjectDef]
    commands: list[CommandDef]
    events: list[EventDef]
    #: {schema_path: [(instance_tuple, {param: value})]}
    factory: dict[str, list[tuple[tuple[int, ...], dict[str, Any]]]] = field(default_factory=dict)

    def factory_row(self, schema_path: str, instances: tuple[int, ...]) -> Optional[dict[str, Any]]:
        for row_instances, values in self.factory.get(schema_path, []):
            if row_instances == tuple(instances):
                return dict(values)
        return None

    def summary(self) -> dict:
        return {"name": self.name, "description": self.description,
                "areas": sorted({_area(o.path) for o in self.objects})}


def _area(path: str) -> str:
    """"Device.WiFi.Radio.{i}" -> "WiFi"."""
    return path.split(".")[1]


def resolve(name_or_path: Optional[str] = None, directory: str = PROFILE_DIR) -> str:
    """A profile file from a name in the profiles directory, or a path."""
    value = name_or_path or DEFAULT_PROFILE
    if value.endswith(".toml") or os.sep in value:
        path = value
    else:
        path = os.path.join(directory, f"{value}.toml")
    if not os.path.isfile(path):
        known = sorted(f[:-5] for f in os.listdir(directory) if f.endswith(".toml")) if os.path.isdir(directory) else []
        raise ProfileError(f"no profile {value!r} (looked for {path}; available: {', '.join(known) or 'none'})")
    return path


def load(name_or_path: Optional[str] = None, directory: str = PROFILE_DIR) -> Profile:
    path = resolve(name_or_path, directory)
    with open(path, "rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ProfileError(f"{path}: not valid TOML: {exc}") from exc
    return parse(data, source=path)


def parse(data: dict, source: str = "<profile>") -> Profile:
    """Builds a profile from parsed TOML; raises ProfileError on any problem."""
    data = dict(data)
    board = data.pop("board", None)
    if not isinstance(board, dict) or not isinstance(board.get("name"), str) or not board["name"]:
        raise ProfileError(f"{source}: needs a [board] table with a name")

    objects = {obj.path: obj for obj in MODEL}
    known_areas = {_area(path) for path in objects}
    factory: dict[str, list[tuple[tuple[int, ...], dict[str, Any]]]] = {}
    areas: set[str] = set()

    def where(schema: str, instances: tuple[int, ...] = ()) -> str:
        concrete = schema
        for n in instances:
            concrete = concrete.replace("{i}", str(n), 1)
        return f"{source}: {concrete}"

    def namespace(prefix: str, table: dict) -> None:
        for key, value in table.items():
            if _is_instances(value):
                instances(f"{prefix}.{key}.{{i}}", value, ())
            elif isinstance(value, dict):
                namespace(f"{prefix}.{key}", value)
            else:
                raise ProfileError(f"{source}: {prefix}.{key} is a parameter outside any instance; "
                                   f"parameters go inside [[...]] instance tables")

    def instances(schema: str, rows: list, parent: tuple[int, ...]) -> None:
        obj = objects.get(schema)
        if obj is None:
            same_area = [p for p in objects if _area(p) == _area(schema)]
            raise ProfileError(f"{source}: {schema} is not an object the lab simulates "
                               f"(in this area: {', '.join(same_area)})")
        for number, row in enumerate(rows, start=1):
            key = parent + (number,)
            values: dict[str, Any] = {}
            for name, value in row.items():
                if _is_instances(value):
                    instances(f"{schema}.{name}.{{i}}", value, key)
                elif isinstance(value, dict):
                    for sub, sub_value in _flatten(value, name):
                        values[sub] = sub_value
                else:
                    values[name] = value
            for name, value in values.items():
                _check_param(obj, name, value, where(schema, key))
            factory.setdefault(schema, []).append((key, values))

    for area, table in data.items():
        if area not in known_areas:
            raise ProfileError(f"{source}: [{area}] is not an area the lab simulates "
                               f"(known: {', '.join(sorted(known_areas))})")
        if not isinstance(table, dict):
            raise ProfileError(f"{source}: {area} must be a table of objects")
        areas.add(area)
        namespace(f"Device.{area}", table)

    present = [obj for obj in MODEL if _area(obj.path) in areas]
    commands = [cmd for cmd in COMMANDS if _area(cmd.path) in areas]
    events = [ev for ev in EVENTS if _area(ev.path) in areas]

    # Every instance reference must point at an instance the profile defines
    defined = {_concrete(schema, key) for schema, rows in factory.items() for key, _ in rows}
    for schema, rows in factory.items():
        for key, values in rows:
            for name, value in values.items():
                if isinstance(value, str) and _REFERENCE.match(value) and value not in defined:
                    raise ProfileError(f"{where(schema, key)}.{name} refers to {value}, "
                                       f"which this profile does not define")
    for role in ("wan", "lan"):
        target = board.get(role)
        if target is not None and target not in defined:
            raise ProfileError(f"{source}: [board] {role} = {target!r} is not an instance this profile defines")

    return Profile(
        name=board["name"],
        description=str(board.get("description", "")),
        source=source,
        wan=board.get("wan"),
        lan=board.get("lan"),
        objects=present,
        commands=commands,
        events=events,
        factory=factory,
    )


def _is_instances(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, dict) for v in value) and bool(value)


def _flatten(table: dict, prefix: str):
    for key, value in table.items():
        if isinstance(value, dict):
            yield from _flatten(value, f"{prefix}.{key}")
        else:
            yield f"{prefix}.{key}", value


def _concrete(schema: str, instances: tuple[int, ...]) -> str:
    path = schema
    for n in instances:
        path = path.replace("{i}", str(n), 1)
    return path


def _check_param(obj: ObjectDef, name: str, value: Any, where: str) -> None:
    param = obj.param(name)
    if param is None:
        names = ", ".join(p.name for p in obj.params if p.derived is None)
        raise ProfileError(f"{where}.{name} is not a parameter of {obj.path} (settable: {names})")
    if param.derived is not None:
        raise ProfileError(f"{where}.{name} is computed by the device and cannot be set in a profile")
    if param.type == TYPE_BOOL:
        ok = isinstance(value, bool)
    elif param.type in (TYPE_INT, TYPE_UINT, TYPE_ULONG):
        ok = isinstance(value, int) and not isinstance(value, bool) and (param.type == TYPE_INT or value >= 0)
    else:
        ok = isinstance(value, str)
    if not ok:
        raise ProfileError(f"{where}.{name} = {value!r} is not a valid {param.type}")
    if param.validate is not None:
        try:
            param.validate(value)
        except ValueError as exc:
            raise ProfileError(f"{where}.{name} = {value!r}: {exc}") from exc
