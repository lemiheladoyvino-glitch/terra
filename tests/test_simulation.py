from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest

from server.game.entities import Entity
from server.game.simulation import MAX_STEP, Simulation
from server.game.world import World
from server.game.worldgen import Terrain, TerrainKind
from server.net.connection import Connection, ConnectionRegistry
from server.net.protocol import decode


class FakeSocket:
    def __init__(self) -> None:
        self.closed: list[int] = []
        self.frames: list[str] = []

    async def close(self, code: int) -> None:
        self.closed.append(code)

    async def send_text(self, raw: str) -> None:
        self.frames.append(raw)

    async def receive_text(self) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def setup_world(size: int = 96) -> tuple[Simulation, ConnectionRegistry]:
    registry = ConnectionRegistry()
    terrain = Terrain(bytes([TerrainKind.GRASS]) * size * size, size, 42)
    return Simulation(World(terrain), registry), registry


def connect(sim: Simulation, id: str = "a") -> Connection:
    connection = Connection(FakeSocket(), sim.registry)
    connection.id = id
    sim.add_player(connection)
    sim.registry.register(connection)
    return connection


def drain(connection: Connection) -> list[dict[str, Any]]:
    messages = []
    while not connection.send_queue.empty():
        messages.append(decode(connection.send_queue.get_nowait()))
    return messages


def tick(sim: Simulation, connection: Connection) -> list[dict[str, Any]]:
    sim.movement_tick()
    return drain(connection)


def test_spawn_determinism() -> None:
    first = Simulation(World.new(42), ConnectionRegistry())
    second = Simulation(World.new(42), ConnectionRegistry())
    assert first.spawn_position == second.spawn_position
    x, y = map(int, first.spawn_position)
    assert first.world.terrain.walkable(x, y)
    assert first.world.terrain.at(x, y) in {TerrainKind.GRASS, TerrainKind.SAND}
    a, b = connect(first, "alpha"), connect(first, "beta")
    assert first.world.entities[a.id].position == first.world.entities[b.id].position
    assert first.world.entities[a.id].fields == {"name": "Wanderer-alph", "hp": 100}
    first.remove_player(a)
    assert a.id not in first.world.entities
    assert not a.known_records and not a.sent_chunks and not a.bootstrapped


def test_direction_and_diagonal() -> None:
    sim, _ = setup_world()
    connection = connect(sim)
    start = sim.world.entities[connection.id].position
    connection.move_intent = {"direction": {"x": 1, "y": 0}}
    for _ in range(10):
        tick(sim, connection)
    position = sim.world.entities[connection.id].position
    assert position == pytest.approx((start[0] + 4, start[1]))
    assert sim.world.tick_count == 10
    connection.move_intent = {"direction": {"x": 1, "y": 1}}
    tick(sim, connection)
    new_position = sim.world.entities[connection.id].position
    assert math.dist(position, new_position) == pytest.approx(MAX_STEP)
    assert new_position[0] - position[0] == pytest.approx(MAX_STEP / math.sqrt(2))
    connection.move_intent = {"direction": {"x": 0, "y": 0}}
    tick(sim, connection)
    assert sim.world.entities[connection.id].position == new_position
    assert connection.move_intent is None


@pytest.mark.parametrize("target", [(49.1, 48.5), (48.5, 48.5), (50.1, 50.2)])
def test_target_arrives_and_clears(target: tuple[float, float]) -> None:
    sim, _ = setup_world()
    connection = connect(sim)
    connection.move_intent = {"target": {"x": target[0], "y": target[1]}}
    for _ in range(15):
        tick(sim, connection)
    assert sim.world.entities[connection.id].position == target
    assert connection.move_intent is None


@pytest.mark.parametrize("direction", [(1, 0), (-1, 0), (0, 1), (0, -1)])
def test_world_bounds(direction: tuple[int, int]) -> None:
    sim, _ = setup_world(4)
    connection = connect(sim)
    connection.move_intent = {"direction": {"x": direction[0], "y": direction[1]}}
    for _ in range(20):
        tick(sim, connection)
        x, y = sim.world.entities[connection.id].position
        assert 0 <= x < 4 and 0 <= y < 4
        assert sim.world.terrain.walkable(math.floor(x), math.floor(y))
    coordinate = x if direction[0] else y
    assert min(coordinate, 4 - coordinate) < 1e-6


def test_water_and_corner_crossings() -> None:
    sim, _ = setup_world(8)
    tiles = bytearray(sim.world.terrain.grid)
    for y in range(8):
        tiles[y * 8 + 5] = TerrainKind.SHALLOW_WATER
    sim.world.terrain = Terrain(bytes(tiles), 8, 42)
    connection = connect(sim)
    connection.move_intent = {"target": {"x": 6, "y": 4.5}}
    for _ in range(10):
        tick(sim, connection)
        x, y = sim.world.entities[connection.id].position
        assert sim.world.terrain.walkable(math.floor(x), math.floor(y))
    assert 5 - 1e-6 < x < 5
    assert connection.move_intent is not None  # Blocked target is still the latest intent.

    # Enter water for less than 0.01 tile just before a diagonal corner: sampling
    # every 0.1 tile could miss it, but grid crossings must detect it.
    tiles = bytearray([TerrainKind.GRASS] * 64)
    tiles[3 * 8 + 4] = TerrainKind.DEEP_WATER
    sim.world.terrain = Terrain(bytes(tiles), 8, 42)
    sim.world.move_entity(connection.id, (3.99, 3.985))
    connection.move_intent = {"direction": {"x": 1, "y": 1}}
    tick(sim, connection)
    x, y = sim.world.entities[connection.id].position
    assert x < 4 and y < 4
    assert sim.world.terrain.walkable(math.floor(x), math.floor(y))


def test_aoi_lifecycle_and_tick_order() -> None:
    sim, _ = setup_world()
    a = connect(sim)
    sim.world.add_entity(Entity("tree", "tree", (49, 49), {"resource_remaining": 5}))
    sim.world.add_entity(Entity("far", "rock", (0.5, 0.5), {"resource_remaining": 4}))
    first = tick(sim, a)
    snapshot = first[-1]
    assert snapshot["t"] == "snapshot"
    assert {e["id"] for e in snapshot["entities"]} == {"a", "tree"}
    b = connect(sim, "b")
    second = tick(sim, a)
    assert second[-1]["t"] == "delta"
    assert [e["id"] for e in second[-1]["entered"]] == ["b"]
    a.move_intent = {"direction": {"x": 1, "y": 0}}
    third = tick(sim, a)
    assert [e["id"] for e in third[-1]["changed"]] == ["a"]
    sim.world.move_entity(a.id, (80.5, 48.5))
    a.move_intent = None
    fourth = tick(sim, a)
    assert set(fourth[-1]["left"]) == {"b", "tree"}
    fifth = tick(sim, a)
    assert fifth == [{"t": "delta", "v": 4, "tick": 5,
                      "entered": [], "left": [], "changed": []}]
    batches = [first, second, third, fourth, fifth]
    assert [[m["t"] for m in batch if m["t"] != "chunk"] for batch in batches] == [
        ["snapshot"], ["delta"], ["delta"], ["delta"], ["delta"]
    ]
    assert [batch[-1]["tick"] for batch in batches] == [1, 2, 3, 4, 5]
    sim.remove_player(b)
    assert not b.known_records and not b.sent_chunks and not b.bootstrapped


def test_chunks_once_order_and_new_territory() -> None:
    sim, _ = setup_world()
    a = connect(sim)
    first = tick(sim, a)
    chunks = {(m["cx"], m["cy"]) for m in first[:-1]}
    assert chunks == {(1, 0), (0, 1), (1, 1), (2, 1), (1, 2)}
    assert all(m["t"] == "chunk" for m in first[:-1])
    assert first[-1]["t"] == "snapshot"
    assert [m["t"] for m in tick(sim, a)] == ["delta"]
    sim.world.move_entity(a.id, (80.5, 80.5))
    fresh = tick(sim, a)
    assert fresh[-1]["t"] == "delta"
    new_chunks = {(m["cx"], m["cy"]) for m in fresh[:-1]}
    assert new_chunks == {(2, 2)}
    assert not chunks & new_chunks
    sim.world.move_entity(a.id, sim.spawn_position)
    assert [m["t"] for m in tick(sim, a)] == ["delta"]


def test_chunk_tangent_and_partial_edge() -> None:
    sim, _ = setup_world(70)
    a = connect(sim)
    sim.world.move_entity(a.id, (12.0, 12.0))
    messages = tick(sim, a)
    assert {(m["cx"], m["cy"]) for m in messages[:-1]} == {(0, 0), (1, 0), (0, 1)}
    sim.world.move_entity(a.id, (69.5, 69.5))
    messages = tick(sim, a)
    assert (2, 2) in {(m["cx"], m["cy"]) for m in messages[:-1]}


def test_backpressure_isolates_slow_connection() -> None:
    sim, _ = setup_world()
    slow, fast = connect(sim, "slow"), connect(sim, "fast")
    for _ in range(slow.send_queue.maxsize):
        slow.send_queue.put_nowait("full")
    sim.movement_tick()
    assert slow.closing and slow._close_code == 1011
    assert slow._close_requested.is_set()
    assert not slow.bootstrapped and not slow.sent_chunks
    assert drain(fast)[-1]["t"] == "snapshot"
    sim.movement_tick()
    assert sim.world.tick_count == 2
    assert drain(fast)[-1]["t"] == "delta"


def test_partial_bootstrap_backpressure() -> None:
    sim, _ = setup_world()
    a = connect(sim)
    for _ in range(a.send_queue.maxsize - 1):
        a.send_queue.put_nowait("full")
    sim.movement_tick()
    assert a.closing
    assert len(a.sent_chunks) == 1
    assert not a.bootstrapped and not a.known_records


def test_oversized_snapshot_closes_without_stopping_tick() -> None:
    sim, _ = setup_world()
    a = connect(sim)
    for n in range(1000):
        sim.world.add_entity(Entity(f"tree:{n}", "tree", sim.spawn_position,
                                    {"resource_remaining": 10}))
    sim.movement_tick()
    assert a.closing and a._close_code == 1009
    assert not a.bootstrapped
    assert sim.world.tick_count == 1


def test_scheduled_close_unwinds_connection() -> None:
    async def scenario() -> None:
        sim, _ = setup_world()
        a = connect(sim)
        task = asyncio.create_task(a.run(0, world_size=96, chunk_size=32, seed=42))
        await asyncio.sleep(0)
        a.close_soon(1011)
        await asyncio.wait_for(task, 1)
        assert a.websocket.closed == [1011]
        assert a.id not in sim.registry.connections
    asyncio.run(scenario())


def test_no_connections_world_keeps_ticking() -> None:
    sim, _ = setup_world()
    for _ in range(10):
        sim.movement_tick()
    sim.sim_tick()
    assert sim.world.tick_count == 10
