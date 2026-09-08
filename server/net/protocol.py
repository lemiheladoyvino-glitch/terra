from __future__ import annotations

import json
import math
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = 2
CHUNK_SIZE = 32
MAX_MESSAGE_BYTES = 65_536


class MessageType(StrEnum):
    HELLO = "hello"
    MOVE = "move"
    INTERACT = "interact"
    CRAFT = "craft"
    CHAT = "chat"
    WELCOME = "welcome"
    CHUNK = "chunk"
    SNAPSHOT = "snapshot"
    DELTA = "delta"
    INVENTORY = "inventory"
    EVENT = "event"
    ERROR = "error"


class ProtocolError(ValueError):
    """Invalid, unsupported, or oversized wire message."""


# Schemas are closed objects. Tuples denote alternatives; lists denote arrays.
POSITION = {"x": "number", "y": "number"}
ITEM = {"item_id": "id", "quantity": "positive_int", "durability": "fraction"}
KINDS: dict[str, dict[str, Any]] = {
    "player": {"name": "text", "hp": "uint"},
    "villager": {"name": "text", "hp": "uint"},
    "tree": {"resource_remaining": "uint"},
    "rock": {"resource_remaining": "uint"},
    "berry-bush": {"resource_remaining": "uint"},
    "building": {"building_type": "id", "hp": "uint"},
    "grave": {"owner_id": "id", "expires_tick": "uint"},
    "merchant": {"name": "text", "shop_id": "id"},
    "road": {"road_type": "id"},
}
CONFIG = {"movement_hz": "ten", "sim_hz": "one", "aoi_radius": "positive_number"}
SCHEMAS: dict[str, Any] = {
    "hello": {"token": "text"},
    "move": ({"direction": POSITION}, {"target": POSITION}),
    "interact": {
        "target": ({"entity_id": "id"}, {"tile": {"x": "int", "y": "int"}}),
        "action": "action",
    },
    "craft": {"recipe_id": "id"},
    "chat": ({"text": "text"}, {"text": "text", "sender_id": "id", "tick": "uint"}),
    "welcome": {"entity_id": "id", "tick": "uint", "config": CONFIG,
                "world_size": "positive_int", "chunk_size": "chunk_size", "seed": "int"},
    "chunk": {"cx": "int", "cy": "int", "size": "chunk_size", "tiles": "rle"},
    "snapshot": {"tick": "uint", "entities": ["entity"]},
    "delta": {"tick": "uint", "entered": ["entity"], "left": ["id"], "changed": ["entity"]},
    "inventory": {"tick": "uint", "slots": [({"slot": "uint", **ITEM})]},
    "event": (
        {"tick": "uint", "event": "tool_broke", "item_id": "id"},
        {"tick": "uint", "event": "you_died", "grave_id": "id"},
        {"tick": "uint", "event": "trade_result", "success": "bool", "reason": "text"},
    ),
    "error": {"code": "id", "message": "text"},
}


def _validate(value: Any, schema: Any) -> None:
    if isinstance(schema, dict):
        if not isinstance(value, dict) or value.keys() != schema.keys():
            raise ProtocolError("object fields do not match schema")
        for key, child in schema.items():
            _validate(value[key], child)
        return
    if isinstance(schema, tuple):
        for option in schema:
            try:
                _validate(value, option)
                return
            except ProtocolError:
                pass
        raise ProtocolError("no matching message variant")
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise ProtocolError("expected array")
        for item in value:
            _validate(item, schema[0])
        return
    if schema == "rle":
        decode_chunk_rle(value)
        return
    if schema == "entity":
        if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
            raise ProtocolError("invalid entity")
        kind = value["kind"]
        if kind not in KINDS:
            raise ProtocolError("unknown entity kind")
        _validate(value, {"id": "id", "kind": kind, "position": POSITION, **KINDS[kind]})
        return
    number = type(value) in (int, float) and math.isfinite(value)
    checks = {
        "number": number,
        "positive_number": number and value > 0,
        "fraction": number and 0 <= value <= 1,
        "int": type(value) is int,
        "uint": type(value) is int and value >= 0,
        "positive_int": type(value) is int and value > 0,
        "id": isinstance(value, str) and 1 <= len(value) <= 128,
        "text": isinstance(value, str) and 1 <= len(value) <= 2048,
        "bool": type(value) is bool,
        "ten": type(value) is int and value == 10,
        "one": type(value) is int and value == 1,
        "chunk_size": type(value) is int and value == CHUNK_SIZE,
        "action": isinstance(value, str)
        and value in {"chop", "mine", "eat", "trade", "craft", "pickup"},
    }
    if not checks.get(schema, value == schema):
        raise ProtocolError(f"invalid {schema}")


def _message(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise ProtocolError("expected message object")
    if type(message.get("v")) is not int or message["v"] != SCHEMA_VERSION:
        raise ProtocolError("unsupported or missing schema version")
    kind = message.get("t")
    if not isinstance(kind, str) or kind not in SCHEMAS:
        raise ProtocolError("unknown message type")
    _validate({k: v for k, v in message.items() if k not in {"t", "v"}}, SCHEMAS[kind])
    if kind == "move" and "direction" in message:
        direction = message["direction"]
        if any(abs(component) > 1 for component in direction.values()):
            raise ProtocolError("direction components must be between -1 and 1")
    if kind in {"snapshot", "delta"}:
        groups = [message["entities"]] if kind == "snapshot" else [
            message["entered"], message["changed"], message["left"]
        ]
        ids = [item["id"] if isinstance(item, dict) else item for group in groups for item in group]
        if len(ids) != len(set(ids)):
            raise ProtocolError("entity IDs must be unique across message")
    if kind == "inventory":
        slots = [item["slot"] for item in message["slots"]]
        if len(slots) != len(set(slots)):
            raise ProtocolError("inventory slots must be unique")
    return message


def encode(message: dict[str, Any]) -> str:
    """Validate and serialize a complete message, including t and v."""
    try:
        _message(message)
        raw = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ProtocolError("message too large")
        return raw
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ProtocolError(str(exc)) from exc


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON key")
        result[key] = value
    return result


def decode(raw: str) -> dict[str, Any]:
    """Decode one UTF-8 JSON text frame. Binary frames are unsupported."""
    try:
        if not isinstance(raw, str):
            raise ProtocolError("expected text frame")
        if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ProtocolError("message too large")
        return _message(json.loads(raw, object_pairs_hook=_object))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ProtocolError(str(exc)) from exc


def decode_chunk_rle(runs: list[list[int]]) -> bytes:
    """Validate exactly 1024 tiles before expanding; IDs are terrain ordinals 0..5."""
    if not isinstance(runs, list) or not 1 <= len(runs) <= CHUNK_SIZE ** 2:
        raise ProtocolError("invalid chunk RLE")
    total = 0
    for run in runs:
        if (not isinstance(run, list) or len(run) != 2
                or type(run[0]) is not int or run[0] <= 0
                or type(run[1]) is not int or not 0 <= run[1] <= 5):
            raise ProtocolError("invalid chunk run")
        total += run[0]
        if total > CHUNK_SIZE ** 2:
            raise ProtocolError("chunk RLE exceeds 1024 tiles")
    if total != CHUNK_SIZE ** 2:
        raise ProtocolError("chunk RLE must contain 1024 tiles")
    return bytes(tile for length, tile in runs for _ in range(length))


def encode_chunk_rle(tiles: bytes) -> list[list[int]]:
    """Encode a 32x32 row-major byte grid into maximal [length, tile_id] runs."""
    if not isinstance(tiles, bytes) or len(tiles) != CHUNK_SIZE ** 2:
        raise ProtocolError("chunk must contain 1024 tile bytes")
    runs: list[list[int]] = []
    for tile in tiles:
        if tile > 5:
            raise ProtocolError("unknown terrain tile")
        if runs and runs[-1][1] == tile:
            runs[-1][0] += 1
        else:
            runs.append([1, tile])
    return runs
