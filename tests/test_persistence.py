from __future__ import annotations

import gzip
import json
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.app as app_module
from server.game.persistence import SNAPSHOT_VERSION, load_world, save_world, world_to_dict
from server.game.simulation import Simulation
from server.game.village import State
from server.game.world import World
from server.net.connection import ConnectionRegistry


def test_round_trip_and_fsm_continuation() -> None:
    world = World.new(42)
    sim = Simulation(world, ConnectionRegistry())
    for _ in range(300):
        sim._village_tick()
        world.tick_count += 10
    resources = [e for e in world.entities.values() if e.kind == "tree"]
    for e in resources[:5]:
        world.update_entity_fields(e.id, resource_remaining=1)
    village = world.villages[0]
    worker = next(iter(village.workers.values()))
    worker.state = State.GATHER
    worker.target = resources[-1].id
    worker.cargo = "wood"
    worker.slot = (village.center[0] + 3, village.center[1])
    world.move_entity(worker.id, (village.center[0] + 0.25, village.center[1]))
    data = world_to_dict(world)
    restored = World.from_snapshot(data)
    assert world_to_dict(restored) == data
    assert restored.terrain.grid == world.terrain.grid
    assert all(v._sites is None for v in restored.villages)
    for v in restored.villages:
        assert all(e is restored.entities[e.id] for e in v.buildings)
        for id in v.villagers:
            e = restored.entities[id]
            assert e in restored.query_radius(e.position, 0)
    other = Simulation(restored, ConnectionRegistry())
    for _ in range(50):
        sim._village_tick()
        other._village_tick()
        world.tick_count += 10
        restored.tick_count += 10
        assert world_to_dict(world) == world_to_dict(restored)


def test_migration_and_detached_snapshot() -> None:
    world = World.new(42)
    v = world.villages[0]
    id = next(iter(v.workers))
    v.founders = {id}
    v.destination = (100.5, 100.5)
    v.workers[id].state = State.MIGRATE
    v._sites = iter([(1, 2)])
    data = world_to_dict(world)
    restored = World.from_snapshot(data)
    assert world_to_dict(restored) == data
    assert restored.villages[0]._sites is None
    v.stockpile["wood"] = 999
    v.build_queue.clear()
    assert world_to_dict(restored) == data
    assert "_sites" not in data["villages"][0]


def test_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "terra.db"
    world = World.new(42)
    world.tick_count = 123
    save_world(path, world)
    data = load_world(path)
    assert data == world_to_dict(world)
    assert world_to_dict(World.from_snapshot(data)) == data
    world.tick_count = 124
    save_world(path, world)
    assert load_world(path)["tick_count"] == 124
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM kv").fetchone()[0] == 1
        stamp = db.execute("SELECT updated_at FROM kv").fetchone()[0]
        assert stamp.endswith("+00:00")


def test_missing_database_and_row(tmp_path: Path) -> None:
    path = tmp_path / "terra.db"
    assert load_world(path) is None
    assert not path.exists()
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE kv(key TEXT PRIMARY KEY, value BLOB NOT NULL, updated_at TEXT NOT NULL)"
        )
    assert load_world(path) is None


@pytest.mark.parametrize(
    "blob",
    [
        b"garbage",
        gzip.compress(b"not json"),
        gzip.compress(json.dumps({"version": SNAPSHOT_VERSION + 1}).encode()),
        gzip.compress(json.dumps({"version": SNAPSHOT_VERSION, "seed": 42}).encode()),
    ],
)
def test_corruption_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, blob: bytes
) -> None:
    path = tmp_path / "terra.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE kv(key TEXT PRIMARY KEY, value BLOB NOT NULL, updated_at TEXT NOT NULL)"
        )
        db.execute("INSERT INTO kv VALUES ('world', ?, 'now')", (blob,))
    assert load_world(path) is None
    assert not path.exists()
    (quarantined,) = tmp_path.glob("terra.db.corrupt-*")
    with sqlite3.connect(quarantined) as db:
        assert db.execute("SELECT value FROM kv").fetchone()[0] == blob
    assert "Corrupt world database preserved" in caplog.text


def test_invalid_sqlite_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "terra.db"
    path.write_bytes(b"not sqlite")
    assert load_world(path) is None
    assert list(tmp_path.glob("*.corrupt-*"))


def test_no_reseeding(tmp_path: Path) -> None:
    world = World.new(42)
    trees = [e.id for e in world.entities.values() if e.kind == "tree"]
    for id in trees:
        world.update_entity_fields(id, resource_remaining=1)
    world.remove_entity(trees[0])
    restored = World.from_snapshot(world_to_dict(world))
    assert {
        e.id: e.fields["resource_remaining"] for e in restored.entities.values() if e.kind == "tree"
    } == {id: 1 for id in trees[1:]}


def test_lifespan_resumes_and_final_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "lifespan.db"
    monkeypatch.setenv("TERRA_DB", str(path))
    with TestClient(app_module.app) as client:
        client.portal.call(app_module.app.state.loop.stop)
        app_module.app.state.world.tick_count = 456
        client.portal.call(app_module.app.state.save_world)
        assert load_world(path)["tick_count"] == 456
        app_module.app.state.world.tick_count = 457
    assert load_world(path)["tick_count"] == 457
    with TestClient(app_module.app) as client:
        client.portal.call(app_module.app.state.loop.stop)
        assert app_module.app.state.world.tick_count == 457
    assert load_world(path)["tick_count"] == 457


def test_periodic_save_uses_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERRA_DB", str(tmp_path / "periodic.db"))
    monkeypatch.setattr(app_module, "SAVE_INTERVAL", 0.01)
    saved = threading.Event()
    serializer_threads: list[int] = []
    writer_threads: list[int] = []
    serialize, write = app_module.serialize_world, app_module.write_snapshot

    def capture(world: World) -> bytes:
        serializer_threads.append(threading.get_ident())
        return serialize(world)

    def record(path: Path, blob: bytes) -> None:
        writer_threads.append(threading.get_ident())
        write(path, blob)
        saved.set()

    monkeypatch.setattr(app_module, "serialize_world", capture)
    monkeypatch.setattr(app_module, "write_snapshot", record)
    with TestClient(app_module.app):
        assert saved.wait(3)
    assert serializer_threads and writer_threads
    assert set(serializer_threads).isdisjoint(writer_threads)
