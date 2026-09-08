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
    decode_chunk_rle,
    encode,
    encode_chunk_rle,
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
    "{", "[]", "null", '{}', '{"t":"unknown","v":2}',
    '{"t":"hello","v":1,"token":"x"}', '{"t":"hello","token":"x"}',
    '{"t":"hello","v":true,"token":"x"}', '{"t":"hello","v":2,"token":1}',
    '{"t":"hello","v":2,"token":"x","extra":0}',
    '{"t":"hello","v":2,"v":2,"token":"x"}',
    '{"t":"move","v":2,"direction":{"x":NaN,"y":0}}',
    '{"t":"move","v":2,"direction":{"x":2,"y":0}}',
    '{"t":"snapshot","v":2,"tick":0,"entities":[{}]}',
    ' ' * (MAX_MESSAGE_BYTES + 1), '\ud800', b'{}',
])
def test_malformed(raw: Any) -> None:
    with pytest.raises(ProtocolError):
        decode(raw)


@pytest.mark.parametrize("message", [
    {"t": "hello", "v": 2, "token": ""},
    {"t": "move", "v": 2, "target": {"x": float("inf"), "y": 0}},
    {"t": "snapshot", "v": 2, "tick": 0, "entities": [], "extra": True},
    {"t": "delta", "v": 2, "tick": 0, "entered": [], "changed": [], "left": ["a", "a"]},
    {"t": "inventory", "v": 2, "tick": -1, "slots": []},
])
def test_invalid_encode(message: dict[str, Any]) -> None:
    with pytest.raises(ProtocolError):
        encode(message)


def test_utf8_size_limit() -> None:
    message = {"t": "delta", "v": 2, "tick": 0, "entered": [], "changed": [],
               "left": [f"{i}" + "é" * 120 for i in range(300)]}
    with pytest.raises(ProtocolError):
        encode(message)
    with pytest.raises(ProtocolError):
        decode(json.dumps(message, ensure_ascii=False))
    raw = '{"t":"hello","v":2,"token":"x"}'
    assert decode(raw + " " * (MAX_MESSAGE_BYTES - len(raw))) == json.loads(raw)


@pytest.mark.parametrize("kind", KINDS)
def test_entity_kinds(kind: str) -> None:
    values = {"text": "Ada", "uint": 1, "id": "example"}
    entity = {"id": "e1", "kind": kind, "position": {"x": 0, "y": 0},
              **{key: values[schema] for key, schema in KINDS[kind].items()}}
    message = {"t": "snapshot", "v": 2, "tick": 0, "entities": [entity]}
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
            player_id = welcome["entity_id"]
            assert player_id in app.state.world.entities

            def receive_until(kind: str) -> dict[str, Any]:
                for _ in range(100):
                    message = decode(socket.receive_text())
                    if message["t"] == kind:
                        return message
                raise AssertionError(f"did not receive {kind}")

            first_chunk = decode(socket.receive_text())
            assert first_chunk["t"] == "chunk"
            snapshot = receive_until("snapshot")
            own = next(e for e in snapshot["entities"] if e["id"] == player_id)
            assert own["kind"] == "player"
            socket.send_json({"t": "move", "v": 2, "direction": {"x": 1, "y": 0}})
            for _ in range(10):
                delta = receive_until("delta")
                assert delta["tick"] > snapshot["tick"]
                if any(e["id"] == player_id for e in delta["changed"]):
                    break
            else:
                raise AssertionError("player movement was not streamed")
            socket.send_json({"t": "move", "v": 2, "direction": {"x": 0, "y": 0}})
            socket.send_json({"t": "hello", "v": 2, "token": "anything"})
            socket.send_json(welcome)
            assert receive_until("error")["code"] == "invalid_message"
            socket.send_bytes(b"binary")
            assert receive_until("error")["code"] == "invalid_message"
        # A following request yields to connection cleanup on the server event loop.
        client.get("/healthz")
        assert not app.state.registry.connections
        assert player_id not in app.state.world.entities
    assert app.state.loop._task is None


def test_schema_v2_rejects_v1() -> None:
    assert SCHEMA_VERSION == 2
    with pytest.raises(ProtocolError):
        decode('{"t":"hello","v":1,"token":"x"}')


@pytest.mark.parametrize("runs", [
    [], [[0, 0]], [[-1, 0]], [[1023, 0]], [[1025, 0]], [[1024, 6]],
    [[1024, -1]], [[True, 0]], [[1024, True]], [[1024.0, 0]], [[1024, 0, 0]],
    [[1024]], "invalid", [[10**100, 0]], [[512, 0], [513, 1]],
])
def test_invalid_chunk_rle(runs: Any) -> None:
    message = {"t": "chunk", "v": 2, "cx": 0, "cy": 0, "size": 32, "tiles": runs}
    with pytest.raises(ProtocolError):
        encode(message)
    with pytest.raises(ProtocolError):
        decode(json.dumps(message))


@pytest.mark.parametrize("field,value", [("size", 16), ("size", True), ("cx", 0.5),
                                          ("cy", False)])
def test_invalid_chunk_fields(field: str, value: Any) -> None:
    message = {"t": "chunk", "v": 2, "cx": 0, "cy": 0, "size": 32, "tiles": [[1024, 0]]}
    message[field] = value
    with pytest.raises(ProtocolError):
        encode(message)


def test_chunk_real_terrain_round_trip() -> None:
    from server.game.worldgen import generate_island

    terrain = generate_island(42)
    for cx, cy in [(0, 0), (2, 2), (3, 1), (5, 5)]:
        tiles = terrain.chunk_tiles(cx, cy)
        message = {"t": "chunk", "v": 2, "cx": cx, "cy": cy, "size": 32,
                   "tiles": encode_chunk_rle(tiles)}
        decoded = decode_chunk_rle(decode(encode(message))["tiles"])
        assert len(decoded) == 1024
        assert decoded == tiles
        assert all(decoded[y * 32 + x] == terrain.at(cx * 32 + x, cy * 32 + y)
                   for y in range(32) for x in range(32))
    assert encode_chunk_rle(bytes(1024)) == [[1024, 0]]
    assert decode_chunk_rle([[512, 0], [512, 0]]) == bytes(1024)


@pytest.mark.parametrize("tiles", [bytes(1023), bytes(1025), bytes([6]) * 1024])
def test_invalid_chunk_encode(tiles: bytes) -> None:
    with pytest.raises(ProtocolError):
        encode_chunk_rle(tiles)


@pytest.mark.parametrize("field,value", [("world_size", 0), ("chunk_size", 16),
                                          ("seed", True)])
def test_invalid_welcome_v2(field: str, value: Any) -> None:
    welcome = next(m.copy() for m in EXAMPLES if m["t"] == "welcome")
    welcome[field] = value
    with pytest.raises(ProtocolError):
        encode(welcome)
    del welcome[field]
    with pytest.raises(ProtocolError):
        encode(welcome)
