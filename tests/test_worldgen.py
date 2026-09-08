from __future__ import annotations

import hashlib
import random

import pytest

from server.game.entities import Entity
from server.game.world import World
from server.game.worldgen import Terrain, TerrainKind, _ValueNoise, generate_island
from server.net.protocol import KINDS, MAX_MESSAGE_BYTES, SCHEMA_VERSION, decode, encode


def test_determinism() -> None:
    state = random.getstate()
    first, second = generate_island(42), generate_island(42)
    assert first.grid == second.grid
    assert isinstance(first.grid, bytes)
    assert first.grid != generate_island(43).grid
    assert len(generate_island(42, 48).grid) == 48 * 48
    a, b = World.new(42), World.new(42)
    assert {id: e.to_record() for id, e in a.entities.items()} == {
        id: e.to_record() for id, e in b.entities.items()
    }
    assert random.getstate() == state


@pytest.mark.parametrize("seed", [0, 1, 42, -123, 2026])
def test_island_sanity(seed: int) -> None:
    terrain = generate_island(seed)
    land = sum(tile not in (0, 1) for tile in terrain.grid)
    assert 0.25 <= land / len(terrain.grid) <= 0.75
    assert terrain.walkable(terrain.size // 2, terrain.size // 2)
    for n in range(terrain.size):
        for x, y in [(n, 0), (n, terrain.size - 1), (0, n), (terrain.size - 1, n)]:
            assert terrain.at(x, y) == TerrainKind.DEEP_WATER


def test_resources() -> None:
    world = World.new(42)
    occupied: set[tuple[float, float]] = set()
    ranges = {"tree": (5, 12), "rock": (4, 10), "berry-bush": (3, 6)}
    allowed = {
        "tree": {TerrainKind.FOREST_FLOOR, TerrainKind.GRASS},
        "rock": {TerrainKind.ROCK, TerrainKind.SAND, TerrainKind.GRASS},
        "berry-bush": {TerrainKind.GRASS, TerrainKind.FOREST_FLOOR},
    }
    assert {e.kind for e in world.entities.values()} == set(ranges) | {"villager"}
    for entity in world.entities.values():
        if entity.kind == "villager":
            continue
        x, y = entity.position
        tx, ty = int(x), int(y)
        assert (x, y) == (tx + 0.5, ty + 0.5)
        assert entity.id == f"{entity.kind}:{tx}:{ty}"
        assert entity.position not in occupied
        occupied.add(entity.position)
        assert world.terrain.walkable(tx, ty)
        assert world.terrain.at(tx, ty) in allowed[entity.kind]
        low, high = ranges[entity.kind]
        assert low <= entity.fields["resource_remaining"] <= high
        assert entity in world.query_radius(entity.position, 0)


def test_bounds_and_walkability() -> None:
    terrain = Terrain(bytes([0, 1, 2, 3, 4, 5, 0, 0, 0]), 3, 42)
    for tile in TerrainKind:
        x, y = int(tile) % 3, int(tile) // 3
        assert terrain.at(x, y) == tile
        assert terrain.walkable(x, y) == (tile not in (0, 1))
    for x, y in [(-1, 0), (0, -1), (3, 0), (0, 3)]:
        assert not terrain.in_bounds(x, y)
        assert not terrain.walkable(x, y)
        with pytest.raises(IndexError):
            terrain.at(x, y)
    chunk = terrain.chunk_tiles(0, 0)
    assert len(chunk) == 1024
    assert chunk[0:3] == terrain.grid[0:3]
    assert chunk[3:32] == bytes(29)
    assert chunk[96:] == bytes(928)
    with pytest.raises(IndexError):
        terrain.chunk_tiles(-1, 0)
    with pytest.raises(IndexError):
        terrain.chunk_tiles(1, 0)


@pytest.mark.parametrize("size", [0, 1, -1, True, 3.5])
def test_invalid_size(size: int) -> None:
    with pytest.raises(ValueError):
        generate_island(42, size)


def test_spatial_index() -> None:
    world = World(Terrain(bytes(64 * 64), 64, 0))
    points = [(-16.0, 0.0), (-0.1, 16.0), (0.0, 0.0), (3.0, 4.0),
              (15.9, 16.0), (16.0, 16.0), (32.0, 32.0), (80.0, 80.0)]
    for index, position in enumerate(points):
        world.add_entity(Entity(str(index), "tree", position, {"resource_remaining": 5}))

    def compare() -> None:
        for center in [(0.0, 0.0), (16.0, 16.0), (-20.0, -20.0), (80.0, 80.0)]:
            for radius in [0.0, 5.0, 16.0, 32.0, 150.0]:
                expected = sorted(e.id for e in world.entities.values()
                                  if sum((a - b) ** 2 for a, b in zip(e.position, center))
                                  <= radius ** 2)
                assert [e.id for e in world.query_radius(center, radius)] == expected

    compare()
    world.move_entity("2", (0.5, 0.5))  # Same bucket.
    world.move_entity("3", (48.0, 48.0))  # Across buckets.
    world.move_entity("4", (-32.0, -32.0))
    assert world.remove_entity("0").id == "0"
    compare()
    with pytest.raises(ValueError):
        world.add_entity(world.entities["1"])
    with pytest.raises(KeyError):
        world.remove_entity("missing")
    with pytest.raises(KeyError):
        world.move_entity("missing", (0.0, 0.0))
    with pytest.raises(ValueError):
        world.move_entity("1", (float("nan"), 0.0))
    compare()
    for id in list(world.entities):
        world.remove_entity(id)
    assert not world._buckets


@pytest.mark.parametrize("radius", [-1.0, float("inf"), float("nan")])
def test_invalid_radius(radius: float) -> None:
    world = World(Terrain(bytes(4), 2, 0))
    with pytest.raises(ValueError):
        world.query_radius((0.0, 0.0), radius)


@pytest.mark.parametrize("kind", KINDS)
def test_all_entity_records(kind: str) -> None:
    values = {"text": "Ada", "uint": 1, "id": "example"}
    entity = Entity("e1", kind, (1.5, 2.5),
                    {key: values[schema] for key, schema in KINDS[kind].items()})
    message = {"t": "snapshot", "v": 3, "tick": 0, "entities": [entity.to_record()]}
    assert decode(encode(message)) == message


def test_seed_42_terrain_golden() -> None:
    terrain = generate_island(42)
    assert terrain.grid[:16] == (
        b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    )
    # The prefix is all ocean; pin the entire grid to catch interior changes too.
    assert hashlib.sha256(terrain.grid).hexdigest() == (
        "cbaae36bdcd3fd32fed1b196478c007cc2873535839b12b4914aa3f5c924deb8"
    )


@pytest.mark.parametrize("x,y", [(32.5, 32.75), (33.0, 33.0), (1000.25, 2000.5),
                                 (-0.25, -33.5)])
def test_noise_wraps_outside_table(x: float, y: float) -> None:
    noise = _ValueNoise(random.Random(42))
    value = noise.sample(x, y)
    assert 0 <= value <= 1
    assert value == noise.sample(x % 33, y % 33)


def test_densest_window_snapshot_size() -> None:
    world = World.new(42)
    size, width = world.terrain.size, 40
    # Summed-area table counts every tile-aligned window without repeatedly
    # scanning the entity dictionary. A square conservatively covers circular AOI.
    counts = [[0] * (size + 1) for _ in range(size + 1)]
    for entity in world.entities.values():
        x, y = map(int, entity.position)
        counts[y + 1][x + 1] += 1
    for y in range(1, size + 1):
        for x in range(1, size + 1):
            counts[y][x] += counts[y - 1][x] + counts[y][x - 1] - counts[y - 1][x - 1]
    count, left, top = max(
        (counts[y + width][x + width] - counts[y][x + width]
         - counts[y + width][x] + counts[y][x], x, y)
        for y in range(size - width + 1) for x in range(size - width + 1)
    )
    records = [entity.to_record() for entity in world.entities.values()
               if left <= entity.position[0] < left + width
               and top <= entity.position[1] < top + width]
    assert len(records) == count > 0
    snapshot = encode({"t": "snapshot", "v": SCHEMA_VERSION, "tick": world.tick_count,
                       "entities": records})
    assert len(snapshot.encode("utf-8")) < 48 * 1024 < MAX_MESSAGE_BYTES
