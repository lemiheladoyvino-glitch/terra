from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from typing import Any

from server.game.entities import Entity, Position
from server.game.village import Village, seed_villages
from server.game.worldgen import Terrain, TerrainKind, generate_island

BUCKET_SIZE = 16


@dataclass
class World:
    terrain: Terrain
    entities: dict[str, Entity] = field(default_factory=dict, init=False)
    tick_count: int = 0
    villages: list[Village] = field(default_factory=list)
    _buckets: dict[tuple[int, int], set[str]] = field(default_factory=dict, init=False, repr=False)

    @staticmethod
    def _bucket(position: Position) -> tuple[int, int]:
        return math.floor(position[0] / BUCKET_SIZE), math.floor(position[1] / BUCKET_SIZE)

    def add_entity(self, entity: Entity) -> None:
        """All entity membership mutations must go through these methods."""
        if entity.id in self.entities:
            raise ValueError(f"duplicate entity id: {entity.id}")
        self.entities[entity.id] = entity
        self._buckets.setdefault(self._bucket(entity.position), set()).add(entity.id)

    def remove_entity(self, id: str) -> Entity:
        """Remove and return an entity; unknown IDs raise KeyError."""
        entity = self.entities.pop(id)
        bucket = self._bucket(entity.position)
        self._buckets[bucket].remove(id)
        if not self._buckets[bucket]:
            del self._buckets[bucket]
        return entity

    def move_entity(self, id: str, new_pos: Position) -> None:
        """Update position/index only; movement/collision policy is not implemented."""
        entity = replace(self.entities[id], position=new_pos)
        self.remove_entity(id)
        self.add_entity(entity)

    def update_entity_fields(self, id: str, **fields: Any) -> None:
        """Replace kind fields without changing position or spatial membership."""
        entity = self.entities[id]
        self.entities[id] = replace(entity, fields={**entity.fields, **fields})

    def query_radius(self, center: Position, radius: float) -> list[Entity]:
        """Inclusive circular query, sorted by ID for deterministic results.

        Negative/nonfinite radii and nonfinite centers raise ValueError. Centers
        and entities outside terrain bounds are supported by this geometry index.
        """
        if radius < 0 or not math.isfinite(radius) or not all(math.isfinite(v) for v in center):
            raise ValueError("query must have a finite center and finite nonnegative radius")
        x, y = center
        lo = self._bucket((x - radius, y - radius))
        hi = self._bucket((x + radius, y + radius))
        ids: set[str] = set()
        for by in range(lo[1], hi[1] + 1):
            for bx in range(lo[0], hi[0] + 1):
                ids.update(self._buckets.get((bx, by), ()))
        return [self.entities[id] for id in sorted(ids)
                if (self.entities[id].position[0] - x) ** 2
                + (self.entities[id].position[1] - y) ** 2 <= radius * radius]

    @classmethod
    def new(cls, seed: int) -> World:
        world = cls(generate_island(seed))
        rng = random.Random(seed)
        # Mutually exclusive intervals ensure at most one resource per tile.
        densities = {
            TerrainKind.FOREST_FLOOR: (0.22, 0.0, 0.035),
            TerrainKind.GRASS: (0.04, 0.015, 0.025),
            TerrainKind.ROCK: (0.0, 0.30, 0.0),
            TerrainKind.SAND: (0.0, 0.025, 0.0),
        }
        for y in range(world.terrain.size):
            for x in range(world.terrain.size):
                terrain_kind = world.terrain.at(x, y)
                if terrain_kind not in densities:
                    continue
                tree, rock, berry = densities[terrain_kind]
                draw = rng.random()
                if draw < tree:
                    kind, remaining = "tree", rng.randint(5, 12)
                elif draw < tree + rock:
                    kind, remaining = "rock", rng.randint(4, 10)
                elif draw < tree + rock + berry:
                    kind, remaining = "berry-bush", rng.randint(3, 6)
                else:
                    continue
                world.add_entity(Entity(f"{kind}:{x}:{y}", kind, (x + 0.5, y + 0.5),
                                        {"resource_remaining": remaining}))
        seed_villages(world)
        return world
