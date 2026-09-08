from __future__ import annotations

import asyncio
import gzip
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from server.app import app
from server.game.persistence import load_player, save_player, world_to_dict
from server.game.simulation import Simulation
from server.game.survival import add_item
from server.game.world import World
from server.net.connection import Connection, ConnectionRegistry
from server.net.protocol import decode


def connect(sim: Simulation, token: str | None = None) -> Connection:
    connection = Connection(None, sim.registry)
    connection.hello_token = token
    sim.add_player(connection)
    sim.registry.register(connection)
    return connection


def welcome(socket: Any) -> dict[str, Any]:
    message = decode(socket.receive_text())
    assert message["t"] == "welcome"
    assert decode(socket.receive_text())["t"] == "inventory"
    assert decode(socket.receive_text())["t"] == "vitals"
    return message


def test_wire_identity_and_first_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERRA_DB", str(tmp_path / "identity.db"))
    monkeypatch.setattr("server.net.connection.HELLO_TIMEOUT", 0.02)
    with TestClient(app) as client:
        client.portal.call(app.state.loop.stop)
        with client.websocket_connect("/ws") as socket:
            first = welcome(socket)
            assert first["token"] != first["entity_id"]
            state = app.state.simulation.players[first["entity_id"]]
            state.hp, state.hunger = 70, 60
            add_item(state, "wood", 7)
            position = app.state.simulation.spawn_position
            socket.send_json({"t": "hello", "v": 4, "token": first["token"]})
            assert decode(socket.receive_text())["code"] == "invalid_message"
        with client.websocket_connect("/ws") as socket:
            socket.send_json({"t": "hello", "v": 4, "token": first["token"]})
            resumed = welcome(socket)
            assert resumed["entity_id"] == first["entity_id"]
            assert resumed["token"] == first["token"]
            state = app.state.simulation.players[resumed["entity_id"]]
            assert (state.hp, state.hunger) == (70, 60)
            assert any(s and s["item_id"] == "wood" and s["quantity"] == 7 for s in state.inventory)
            assert app.state.world.entities[resumed["entity_id"]].position == position
        with client.websocket_connect("/ws") as socket:
            second = welcome(socket)
            assert second["token"] != first["token"]
        with client.websocket_connect("/ws") as socket:
            socket.send_json({"t": "hello", "v": 4, "token": "unknown"})
            assert welcome(socket)["token"] != "unknown"
        with client.websocket_connect("/ws") as socket:
            socket.send_json({"t": "move", "v": 4, "direction": {"x": 1, "y": 0}})
            moved = welcome(socket)
            connection = app.state.registry.connections[moved["entity_id"]]
            assert connection.move_intent["direction"] == {"x": 1, "y": 0}


def test_live_takeover_does_not_remove_new_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TERRA_DB", str(tmp_path / "takeover.db"))
    monkeypatch.setattr("server.net.connection.HELLO_TIMEOUT", 0.02)
    with TestClient(app) as client:
        client.portal.call(app.state.loop.stop)
        with client.websocket_connect("/ws") as old_socket:
            first = welcome(old_socket)
            old = app.state.registry.connections[first["entity_id"]]
            app.state.simulation.players[old.id].hunger = 42
            with client.websocket_connect("/ws") as new_socket:
                new_socket.send_json({"t": "hello", "v": 4, "token": first["token"]})
                resumed = welcome(new_socket)
                assert resumed["entity_id"] == old.id
                with pytest.raises(WebSocketDisconnect) as closed:
                    old_socket.receive_text()
                assert closed.value.code == 1000
                client.get("/healthz")
                new = app.state.registry.connections[old.id]
                assert new is not old
                assert app.state.simulation.players[new.id].hunger == 42
                assert new.id in app.state.world.entities


def test_db_resume_grave_and_safe_position(tmp_path: Path) -> None:
    async def scenario() -> None:
        db = tmp_path / "players.db"
        sim = Simulation(World.new(42), ConnectionRegistry(), db)
        first = connect(sim)
        state = sim.players[first.id]
        add_item(state, "stone", 12)
        state.hp, state.hunger = 65, 25
        position = sim.spawn_position
        before = deepcopy(state.inventory)
        sim.remove_player(first)
        sim.registry.unregister(first)
        await sim.flush_player_saves()
        stored = load_player(db, first.token)
        assert stored["position"] == list(position)
        other = Simulation(World.from_snapshot(world_to_dict(sim.world)), ConnectionRegistry(), db)
        resumed = connect(other, first.token)
        assert resumed.id == first.id
        assert other.players[resumed.id].inventory == before
        assert (other.players[resumed.id].hp, other.players[resumed.id].hunger) == (65, 25)
        other.players[resumed.id].hp = 2
        other.players[resumed.id].hunger = 0
        other._survival_tick()
        grave_id = next(iter(other.graves))
        assert other.world.entities[grave_id].fields["owner_id"] == first.id
        other.remove_player(resumed)
        other.registry.unregister(resumed)
        await other.flush_player_saves()
        restarted = Simulation(
            World.from_snapshot(world_to_dict(other.world)), ConnectionRegistry(), db
        )
        returned = connect(restarted, first.token)
        returned.interact_intent = {"action": "pickup", "target": {"entity_id": grave_id}}
        restarted.movement_tick()
        assert restarted.players[returned.id].inventory == before
        assert grave_id not in restarted.world.entities
        await restarted.flush_player_saves()
        invalid = load_player(db, first.token)
        invalid["position"] = [-100, 1]
        save_player(db, invalid)
        final = Simulation(World.new(42), ConnectionRegistry(), db)
        restored = connect(final, first.token)
        assert final.world.entities[restored.id].position == final.spawn_position

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "blob",
    [b"garbage", gzip.compress(b"not json"), gzip.compress(json.dumps({"version": 99}).encode())],
)
def test_bad_player_isolated(tmp_path: Path, caplog: pytest.LogCaptureFixture, blob: bytes) -> None:
    db_path = tmp_path / "world.db"
    secret = "secret-bearer-token"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE kv(key TEXT PRIMARY KEY, value BLOB NOT NULL, updated_at TEXT NOT NULL)"
        )
        db.execute("INSERT INTO kv VALUES ('world', ?, 'now')", (b"world-unchanged",))
        db.execute("INSERT INTO kv VALUES (?, ?, 'now')", (f"player:{secret}", blob))
    assert load_player(db_path, secret) is None
    assert secret not in caplog.text
    assert "Corrupt player row" in caplog.text
    with sqlite3.connect(db_path) as db:
        assert (
            db.execute("SELECT value FROM kv WHERE key='world'").fetchone()[0] == b"world-unchanged"
        )
        assert (
            db.execute("SELECT count(*) FROM kv WHERE key LIKE 'corrupt-player:%'").fetchone()[0]
            == 1
        )
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_debounced_save_only_writes_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[dict[str, Any]] = []

    def save(_path: Path, stored: dict[str, Any]) -> None:
        writes.append(deepcopy(stored))

    monkeypatch.setattr("server.game.simulation.save_player", save)

    async def scenario() -> None:
        sim = Simulation(World.new(42), ConnectionRegistry(), tmp_path / "dirty.db")
        connection = connect(sim)
        for _ in range(10):
            sim._survival_tick()
        await sim.flush_player_saves()
        assert len(writes) == 1 and writes[0]["hunger"] == 90
        await sim.flush_player_saves()
        assert len(writes) == 1
        sim.players[connection.id].hunger = 80
        sim.remove_player(connection)
        await sim.flush_player_saves()
        assert len(writes) == 2 and writes[-1]["hunger"] == 80

    asyncio.run(scenario())


def test_crash_player_entities_only_return_on_login() -> None:
    sim = Simulation(World.new(42), ConnectionRegistry())
    connection = connect(sim)
    snapshot = world_to_dict(sim.world)
    restarted = Simulation(World.from_snapshot(snapshot), ConnectionRegistry())
    assert connection.id not in restarted.world.entities
    restarted.player_tokens[connection.token] = deepcopy(sim.player_tokens[connection.token])
    restored = connect(restarted, connection.token)
    assert restored.id == connection.id
    assert restored.id in restarted.world.entities
