from __future__ import annotations

import math
from time import perf_counter
from typing import Any

import pytest

from server.game.entities import Entity
from server.game.simulation import Simulation
from server.game.village import (
    BUILD_PLAN,
    BUILDING_COSTS,
    MAX_VILLAGERS,
    MIN_VILLAGE_SPACING,
    SPAWN_INTERVAL,
    State,
    Village,
)
from server.game.world import World
from server.game.worldgen import Terrain, TerrainKind
from server.net.connection import ConnectionRegistry
from server.net.protocol import decode, encode


def setup() -> tuple[Simulation, Village]:
    world = World(Terrain(bytes([TerrainKind.GRASS]) * 96 * 96, 96, 42))
    village = Village(0, (30.5, 30.5))
    world.villages.append(village)
    village.spawn(world)
    return Simulation(world, ConnectionRegistry()), village


def run(sim: Simulation, count: int) -> None:
    for _ in range(count):
        sim._village_tick()


def records(world: World) -> list[dict[str, Any]]:
    return [world.entities[id].to_record() for id in sorted(world.entities)]


@pytest.mark.parametrize('seed', [0, 1, 42, -123, 2026])
def test_seed(seed: int) -> None:
    first, second = World.new(seed), World.new(seed)
    assert len(first.villages) == 2
    assert [v.center for v in first.villages] == [v.center for v in second.villages]
    assert records(first) == records(second)
    assert math.dist(*[v.center for v in first.villages]) >= MIN_VILLAGE_SPACING
    for village in first.villages:
        assert len(village.villagers) == 3
        assert village.stockpile == {'wood': 0, 'stone': 0}
        assert village.build_queue == list(BUILD_PLAN)
        assert first.terrain.walkable(*map(int, village.center))
        assert first.terrain.at(*map(int, village.center)) != TerrainKind.ROCK
        assert all(first.entities[id].position == village.center for id in village.villagers)


def test_gather_return_depletion_and_index() -> None:
    sim, village = setup()
    tree = Entity('tree', 'tree', (36.5, 30.5), {'resource_remaining': 2})
    sim.world.add_entity(tree)
    worker = next(iter(village.workers.values()))
    sim._village_tick()
    assert worker.state == State.GATHER and worker.target == tree.id
    sim._village_tick()
    assert sim.world.entities[worker.id].position == (32.0, 30.5)
    run(sim, 3)
    assert worker.state == State.RETURN and worker.cargo == 'wood'
    assert sim.world.entities['tree'].fields['resource_remaining'] == 1
    assert tree.fields['resource_remaining'] == 2  # Frozen replacement, no alias mutation.
    run(sim, 20)
    assert village.stockpile['wood'] == 2
    assert 'tree' not in sim.world.entities
    assert not sim.world.query_radius(tree.position, 0)
    assert worker.cargo is None


def test_build_roads_cost_and_wire() -> None:
    sim, village = setup()
    village.stockpile = BUILDING_COSTS['house'].copy()
    sim._village_tick()
    assert next(iter(village.workers.values())).state == State.BUILD
    run(sim, 5)
    assert len(village.buildings) == 1
    assert village.buildings[0].fields == {'building_type': 'house', 'hp': 100}
    assert village.stockpile == {'wood': 0, 'stone': 0}
    assert village.build_queue == list(BUILD_PLAN[1:])
    roads = [e for e in sim.world.entities.values() if e.kind == 'road']
    assert roads
    assert len({e.position for e in roads + village.buildings}) == len(roads) + 1
    assert all(sim.world.terrain.walkable(*map(int, e.position)) for e in roads)
    message = {'t': 'snapshot', 'v': 2, 'tick': 0, 'entities': records(sim.world)}
    assert decode(encode(message)) == message


def test_competing_builders() -> None:
    sim, village = setup()
    village.spawn(sim.world)
    village.spawn(sim.world)
    village.stockpile = {'wood': 40, 'stone': 10}
    run(sim, 15)
    assert len(village.buildings) == 2
    assert len({b.position for b in village.buildings}) == 2
    assert village.stockpile == {'wood': 0, 'stone': 0}


def add_house(sim: Simulation, village: Village) -> None:
    house = Entity(f'house:{village.index}', 'building',
                   (village.center[0] + 4, village.center[1]),
                   {'building_type': 'house', 'hp': 100})
    sim.world.add_entity(house)
    village.buildings.append(house)


def test_growth_and_global_cap() -> None:
    sim, village = setup()
    run(sim, SPAWN_INTERVAL * 2)
    assert len(village.villagers) == 1
    add_house(sim, village)
    run(sim, SPAWN_INTERVAL * 10)
    assert len(village.villagers) == village.population_cap == 6
    # Populate other villages, including a second town eligible to spawn this tick.
    for index in range(1, 28):
        other = Village(index, (60.5, 60.5))
        sim.world.villages.append(other)
        add_house(sim, other)
        for _ in range(min(6, MAX_VILLAGERS - 1 - sum(
                len(v.villagers) for v in sim.world.villages))):
            other.spawn(sim.world)
    assert sum(len(v.villagers) for v in sim.world.villages) == MAX_VILLAGERS - 1
    run(sim, SPAWN_INTERVAL * 2)
    assert sum(len(v.villagers) for v in sim.world.villages) == MAX_VILLAGERS


def test_expansion() -> None:
    world = World.new(42)
    sim = Simulation(world, ConnectionRegistry())
    parent = world.villages[0]
    parent.build_queue.clear()
    while len(parent.villagers) < parent.population_cap:
        parent.spawn(world)
    before = set(parent.villagers)
    for _ in range(400):
        sim._village_tick()
        if len(world.villages) == 3:
            break
    assert len(world.villages) == 3
    new = world.villages[2]
    assert len(new.villagers) == 3
    assert new.villagers <= before
    assert not new.villagers & parent.villagers
    assert new.build_queue == list(BUILD_PLAN)
    assert all(math.dist(new.center, v.center) >= MIN_VILLAGE_SPACING
               for v in world.villages[:2])
    assert all(new.workers[id].state == State.IDLE for id in new.villagers)


def test_deterministic_ticks() -> None:
    a, b = World.new(42), World.new(42)
    # Change insertion order to expose accidental ordering dependencies.
    for id in reversed(list(b.entities)):
        b.add_entity(b.remove_entity(id))
    for world in (a, b):
        run(Simulation(world, ConnectionRegistry()), 200)
    assert records(a) == records(b)
    assert [(v.stockpile, v.build_queue, v.workers) for v in a.villages] == [
        (v.stockpile, v.build_queue, v.workers) for v in b.villages]


def test_water_blocks_gather_and_build_slots() -> None:
    sim, village = setup()
    grid = bytearray(sim.world.terrain.grid)
    for y in range(96):
        grid[y * 96 + 32] = TerrainKind.SHALLOW_WATER
    sim.world.terrain = Terrain(bytes(grid), 96, 42)
    sim.world.add_entity(Entity('unreachable', 'tree', (33.5, 30.5),
                                {'resource_remaining': 1}))
    run(sim, 10)
    worker = next(iter(village.workers.values()))
    assert worker.state == State.IDLE
    assert sim.world.entities[worker.id].position == village.center
    village.stockpile = BUILDING_COSTS['house'].copy()
    run(sim, 10)
    assert len(village.buildings) == 1
    assert village.buildings[0].position[0] < 32


def test_tick_budget() -> None:
    world = World.new(42)
    while sum(len(v.villagers) for v in world.villages) < MAX_VILLAGERS:
        for village in world.villages:
            village.spawn(world)
    sim = Simulation(world, ConnectionRegistry())
    start = perf_counter()
    sim._village_tick()  # All 160 choose jobs against the real resource density.
    elapsed = perf_counter() - start
    assert elapsed < .1, f'160 villagers took {elapsed * 1000:.2f} ms'


def test_expansion_at_global_cap_and_reserved_sites() -> None:
    sim, parent = setup()
    parent.build_queue.clear()
    for _ in range(parent.population_cap - 1):
        parent.spawn(sim.world)
    second = Village(1, (30.5, 75.5))
    sim.world.villages.append(second)
    second.build_queue.clear()
    for _ in range(second.population_cap):
        second.spawn(sim.world)
    crowded = Village(2, (10.5, 10.5))
    sim.world.villages.append(crowded)
    while sum(len(v.villagers) for v in sim.world.villages) < MAX_VILLAGERS:
        crowded.spawn(sim.world)
    run(sim, 40)
    assert len(sim.world.villages) >= 4
    assert sum(len(v.villagers) for v in sim.world.villages) == MAX_VILLAGERS
    for i, a in enumerate(sim.world.villages):
        for b in sim.world.villages[i + 1:]:
            if a.index >= 3 or b.index >= 3:
                assert math.dist(a.center, b.center) >= MIN_VILLAGE_SPACING


def test_lost_resource_reassigns_without_negative_counts() -> None:
    sim, village = setup()
    village.spawn(sim.world)
    sim.world.add_entity(Entity('tree', 'tree', village.center, {'resource_remaining': 1}))
    run(sim, 2)
    assert 'tree' not in sim.world.entities
    assert sorted(w.state for w in village.workers.values()) == [State.IDLE, State.RETURN]
    run(sim, 1)
    assert village.stockpile['wood'] == 1
