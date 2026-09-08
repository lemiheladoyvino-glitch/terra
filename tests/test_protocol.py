from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.net.protocol import (
    KINDS,
    MAX_MESSAGE_BYTES,
    SCHEMA_VERSION,
    MessageType,
    ProtocolError,
    decode,
    encode,
)

EXAMPLES = [json.loads(raw) for raw in re.findall(
    r"```json\n(.*?)\n```", (Path(__file__).resolve().parents[1] / "PROTOCOL.md").read_text(), re.S
)]


@pytest.mark.parametrize("message", EXAMPLES)
def test_round_trip(message: dict[str, Any]) -> None:
    assert decode(encode(message)) == message
    assert message["v"] == SCHEMA_VERSION


def test_all_types_documented() -> None:
    assert {message["t"] for message in EXAMPLES} == {kind.value for kind in MessageType}


@pytest.mark.parametrize("raw", [
    "{", "[]", "null", '{}', '{"t":"unknown","v":1}',
    '{"t":"hello","v":2,"token":"x"}', '{"t":"hello","token":"x"}',
    '{"t":"hello","v":true,"token":"x"}', '{"t":"hello","v":1,"token":1}',
    '{"t":"hello","v":1,"token":"x","extra":0}',
    '{"t":"hello","v":1,"v":1,"token":"x"}',
    '{"t":"move","v":1,"direction":{"x":NaN,"y":0}}',
    '{"t":"move","v":1,"direction":{"x":2,"y":0}}',
    '{"t":"snapshot","v":1,"tick":0,"entities":[{}]}',
    ' ' * (MAX_MESSAGE_BYTES + 1), '\ud800', b'{}',
])
def test_malformed(raw: Any) -> None:
    with pytest.raises(ProtocolError):
        decode(raw)


@pytest.mark.parametrize("message", [
    {"t": "hello", "v": 1, "token": ""},
    {"t": "move", "v": 1, "target": {"x": float("inf"), "y": 0}},
    {"t": "snapshot", "v": 1, "tick": 0, "entities": [], "extra": True},
    {"t": "delta", "v": 1, "tick": 0, "entered": [], "changed": [], "left": ["a", "a"]},
    {"t": "inventory", "v": 1, "tick": -1, "slots": []},
])
def test_invalid_encode(message: dict[str, Any]) -> None:
    with pytest.raises(ProtocolError):
        encode(message)


def test_utf8_size_limit() -> None:
    message = {"t": "delta", "v": 1, "tick": 0, "entered": [], "changed": [],
               "left": [f"{i}" + "é" * 120 for i in range(300)]}
    with pytest.raises(ProtocolError):
        encode(message)
    with pytest.raises(ProtocolError):
        decode(json.dumps(message, ensure_ascii=False))
    raw = '{"t":"hello","v":1,"token":"x"}'
    assert decode(raw + " " * (MAX_MESSAGE_BYTES - len(raw))) == json.loads(raw)


@pytest.mark.parametrize("kind", KINDS)
def test_entity_kinds(kind: str) -> None:
    values = {"text": "Ada", "uint": 1, "id": "example"}
    entity = {"id": "e1", "kind": kind, "position": {"x": 0, "y": 0},
              **{key: values[schema] for key, schema in KINDS[kind].items()}}
    message = {"t": "snapshot", "v": 1, "tick": 0, "entities": [entity]}
    assert decode(encode(message)) == message


def test_app_transport_and_cleanup() -> None:
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert "phaser@3.90.0" in client.get("/").text
        assert client.get("/js/main.js").status_code == 200
        with client.websocket_connect("/ws") as socket:
            welcome = decode(socket.receive_text())
            assert welcome["t"] == "welcome"
            assert welcome["entity_id"] in app.state.registry.connections
            socket.send_json({"t": "hello", "v": 1, "token": "anything"})
            socket.send_json(welcome)
            assert decode(socket.receive_text())["code"] == "invalid_message"
            socket.send_bytes(b"binary")
            assert decode(socket.receive_text())["t"] == "error"
        # A following request yields to connection cleanup on the server event loop.
        client.get("/healthz")
        assert not app.state.registry.connections
    assert app.state.loop._task is None
