from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from server.game.entities import Entity, Position
from server.game.movement import traverse
from server.game.worldgen import TerrainKind

if TYPE_CHECKING:
    from server.game.world import World

BUILD_PLAN = ('house', 'house', 'house', 'town_hall', 'forge', 'house', 'house', 'cathedral')
BUILDING_COSTS = {
    'house': {'wood': 20, 'stone': 5},
    'town_hall': {'wood': 40, 'stone': 20},
    'forge': {'wood': 30, 'stone': 25},
    'cathedral': {'wood': 120, 'stone': 80},
}
VILLAGER_STEP = 1.5
ARRIVAL_RADIUS = 1.2
RESOURCE_RADIUS = 32.0
SPAWN_INTERVAL = 20
FOUNDER_COUNT = 3
MIN_VILLAGE_SPACING = 40
MAX_VILLAGERS = 160
SITE_SCAN_BUDGET = 64


class State(StrEnum):
    IDLE = 'IDLE'
    GATHER = 'GATHER'
    RETURN = 'RETURN'
    BUILD = 'BUILD'
    MIGRATE = 'MIGRATE'


@dataclass
class Villager:
    id: str
    state: State = State.IDLE
    target: str | None = None
    cargo: str | None = None
    slot: Position | None = None


@dataclass
class Village:
    index: int
    center: Position
    stockpile: dict[str, int] = field(default_factory=lambda: {'wood': 0, 'stone': 0})
    buildings: list[Entity] = field(default_factory=list)
    build_queue: list[str] = field(default_factory=lambda: list(BUILD_PLAN))
    villagers: set[str] = field(default_factory=set)
    workers: dict[str, Villager] = field(default_factory=dict)
    age: int = 0
    next_id: int = 0
    founders: set[str] = field(default_factory=set)
    destination: Position | None = None
    _sites: Iterator[tuple[int, int]] | None = field(default=None, repr=False)

    @property
    def population_cap(self) -> int:
        return 4 + 2 * sum(b.fields['building_type'] == 'house' for b in self.buildings)

    def spawn(self, world: World) -> None:
        """Create a deterministic worker; caller enforces population limits."""
        id = f'villager:{self.index}:{self.next_id}'
        self.next_id += 1
        world.add_entity(Entity(id, 'villager', self.center, {'name': id, 'hp': 100}))
        self.villagers.add(id)
        self.workers[id] = Villager(id)


def spiral(center: Position, radius: int) -> Iterator[tuple[int, int]]:
    """Square rings in deterministic clockwise order, O(number of yielded tiles)."""
    cx, cy = map(math.floor, center)
    yield cx, cy
    for r in range(1, radius + 1):
        for x in range(cx - r, cx + r):
            yield x, cy - r
        for y in range(cy - r, cy + r):
            yield cx + r, y
        for x in range(cx + r, cx - r, -1):
            yield x, cy + r
        for y in range(cy + r, cy - r, -1):
            yield cx - r, y


def _land_area(world: World, x: int, y: int) -> bool:
    return all(world.terrain.walkable(x + dx, y + dy)
               and world.terrain.at(x + dx, y + dy) != TerrainKind.ROCK
               for dx in range(-2, 3) for dy in range(-2, 3))


def seed_villages(world: World) -> None:
    size = world.terrain.size
    for anchor in ((size * .28, size * .5), (size * .72, size * .5)):
        for x, y in spiral(anchor, size):
            center = (x + .5, y + .5)
            if (_land_area(world, x, y)
                    and all(math.dist(center, v.center) >= MIN_VILLAGE_SPACING
                            for v in world.villages)):
                village = Village(len(world.villages), center)
                world.villages.append(village)
                for _ in range(FOUNDER_COUNT):
                    village.spawn(world)
                break
        else:
            raise ValueError('world has insufficient land for starting villages')


def _affordable(village: Village) -> bool:
    return bool(village.build_queue) and all(
        village.stockpile.get(k, 0) >= n
        for k, n in BUILDING_COSTS[village.build_queue[0]].items())


def _occupied(world: World, position: Position) -> bool:
    tile = tuple(map(math.floor, position))
    return any(e.kind in {'building', 'road'}
               and tuple(map(math.floor, e.position)) == tile
               for e in world.query_radius(position, 1))


def _slot(world: World, village: Village, start: Position) -> Position | None:
    for x, y in spiral(village.center, 8):
        pos = (x + .5, y + .5)
        if (math.dist(pos, village.center) >= 3 and world.terrain.walkable(x, y)
                and not _occupied(world, pos)
                and traverse(world.terrain, village.center, pos) == pos
                and traverse(world.terrain, start, pos) == pos):
            return pos
    return None


def _move(
    world: World, worker: Villager, target: Position, home: Position | None = None,
) -> bool:
    start = world.entities[worker.id].position
    distance = math.dist(start, target)
    # Near a shore, finish the approach if turning home early would cross water.
    can_turn = home is None or traverse(world.terrain, start, home) == home
    if distance > ARRIVAL_RADIUS or not can_turn:
        step = min(VILLAGER_STEP, distance)
        end = tuple(a + (b - a) * step / distance for a, b in zip(start, target))
        position = traverse(world.terrain, start, (end[0], end[1]))
        world.move_entity(worker.id, position)
        return (math.dist(position, target) <= ARRIVAL_RADIUS
                and (home is None or traverse(world.terrain, position, home) == home))
    return True


def road_tiles(start: Position, end: Position) -> Iterator[tuple[int, int]]:
    """Inclusive integer Bresenham line."""
    x, y = map(math.floor, start)
    tx, ty = map(math.floor, end)
    dx, dy = abs(tx - x), -abs(ty - y)
    sx, sy = (1 if x < tx else -1), (1 if y < ty else -1)
    error = dx + dy
    while True:
        yield x, y
        if (x, y) == (tx, ty):
            break
        twice = 2 * error
        if twice >= dy:
            error += dy
            x += sx
        if twice <= dx:
            error += dx
            y += sy


def _build(world: World, village: Village, slot: Position) -> None:
    if not _affordable(village) or _occupied(world, slot):
        return
    kind = village.build_queue.pop(0)
    for resource, cost in BUILDING_COSTS[kind].items():
        village.stockpile[resource] -= cost
    entity = Entity(f'building:{village.index}:{len(village.buildings)}', 'building', slot,
                    {'building_type': kind, 'hp': 100})
    world.add_entity(entity)
    village.buildings.append(entity)
    for x, y in road_tiles(slot, village.center):
        position = (x + .5, y + .5)
        if world.terrain.walkable(x, y) and not _occupied(world, position):
            world.add_entity(Entity(f'road:{x}:{y}', 'road', position, {'road_type': 'dirt'}))


def _step(world: World, village: Village, worker: Villager) -> None:
    if worker.state == State.MIGRATE:
        if village.destination is not None:
            _move(world, worker, village.destination)
    elif worker.state == State.IDLE:
        if _affordable(village):
            worker.slot = _slot(world, village, world.entities[worker.id].position)
            if worker.slot is not None:
                worker.state = State.BUILD
            return
        if not village.build_queue:
            return
        costs = BUILDING_COSTS[village.build_queue[0]]
        needs = {k: max(0, n - village.stockpile.get(k, 0)) / n for k, n in costs.items()}
        position = world.entities[worker.id].position
        candidates = []
        for entity in world.query_radius(position, RESOURCE_RADIUS):
            resource = {'tree': 'wood', 'rock': 'stone'}.get(entity.kind)
            if resource and needs[resource] > 0 and entity.fields['resource_remaining'] > 0:
                candidates.append((math.dist(position, entity.position) / needs[resource],
                                   entity.id, entity))
        for _, _, entity in sorted(candidates):
            # Both legs must be directly reachable; no worker gets stranded returning.
            if (traverse(world.terrain, position, entity.position) == entity.position
                    and traverse(world.terrain, entity.position, village.center) == village.center):
                worker.target = entity.id
                worker.state = State.GATHER
                break
    elif worker.state == State.GATHER:
        resource = world.entities.get(worker.target or '')
        if resource is None or resource.fields.get('resource_remaining', 0) <= 0:
            worker.state = State.IDLE
        elif _move(world, worker, resource.position, village.center):
            remaining = resource.fields['resource_remaining'] - 1
            if remaining:
                world.update_entity_fields(resource.id, resource_remaining=remaining)
            else:
                world.remove_entity(resource.id)
            worker.cargo = 'wood' if resource.kind == 'tree' else 'stone'
            worker.target = None
            worker.state = State.RETURN
    elif worker.state == State.RETURN:
        if _move(world, worker, village.center):
            if worker.cargo:
                village.stockpile[worker.cargo] = village.stockpile.get(worker.cargo, 0) + 1
            worker.cargo = None
            worker.state = State.IDLE
    elif worker.state == State.BUILD:
        if worker.slot is not None and _move(world, worker, worker.slot):
            _build(world, village, worker.slot)
            worker.slot = None
            worker.state = State.IDLE


def _site_bucket(position: Position) -> tuple[int, int]:
    return (math.floor(position[0] / MIN_VILLAGE_SPACING),
            math.floor(position[1] / MIN_VILLAGE_SPACING))


def _separated(center: Position, sites: dict[tuple[int, int], list[Position]]) -> bool:
    bx, by = _site_bucket(center)
    return all(math.dist(center, site) >= MIN_VILLAGE_SPACING
               for y in range(by - 1, by + 2) for x in range(bx - 1, bx + 2)
               for site in sites.get((x, y), ()))


def _expand(
    world: World, village: Village, sites: dict[tuple[int, int], list[Position]],
) -> None:
    if village.founders:
        assert village.destination is not None
        if all(math.dist(world.entities[id].position, village.destination) <= ARRIVAL_RADIUS
               for id in village.founders):
            new = Village(len(world.villages), village.destination)
            for id in sorted(village.founders):
                village.villagers.remove(id)
                worker = village.workers.pop(id)
                worker.state = State.IDLE
                new.villagers.add(id)
                new.workers[id] = worker
            world.villages.append(new)
            village.founders.clear()
            village.destination = None
        return
    if village.build_queue or len(village.villagers) < village.population_cap:
        return
    if village._sites is None:
        village._sites = spiral((village.center[0] + MIN_VILLAGE_SPACING,
                                 village.center[1]), world.terrain.size)
    # Amortize unsuccessful site searches, including worlds with no remaining space.
    for _ in range(SITE_SCAN_BUDGET):
        tile = next(village._sites, None)
        if tile is None:
            return
        x, y = tile
        center = (x + .5, y + .5)
        if (not _land_area(world, x, y)
                or not _separated(center, sites)
                or _occupied(world, center)
                or traverse(world.terrain, village.center, center) != center):
            continue
        # Dispatch from home so the checked migration segment applies to every founder.
        ready = [id for id in sorted(village.villagers)
                 if village.workers[id].state == State.IDLE
                 and traverse(world.terrain, world.entities[id].position, center) == center]
        if len(ready) < FOUNDER_COUNT:
            return
        village.destination = center
        sites.setdefault(_site_bucket(center), []).append(center)
        village.founders = set(ready[:FOUNDER_COUNT])
        for id in village.founders:
            village.workers[id].state = State.MIGRATE
        return


def tick_villages(world: World) -> None:
    """One deterministic 1 Hz step; newborn towns start working on the next tick."""
    for village in world.villages:
        for id in sorted(id for id in village.villagers if id not in world.entities):
            village.villagers.remove(id)
            village.workers.pop(id, None)
            village.founders.discard(id)
    sites: dict[tuple[int, int], list[Position]] = {}
    for village in world.villages:
        for site in (village.center, village.destination):
            if site is not None:
                sites.setdefault(_site_bucket(site), []).append(site)
    population = sum(len(v.villagers) for v in world.villages)
    for village in tuple(world.villages):
        village.age += 1
        for id in sorted(village.villagers):
            _step(world, village, village.workers[id])
        if (village.age % SPAWN_INTERVAL == 0 and population < MAX_VILLAGERS
                and len(village.villagers) < village.population_cap
                and any(b.fields['building_type'] == 'house' for b in village.buildings)):
            village.spawn(world)
            population += 1
        _expand(world, village, sites)
