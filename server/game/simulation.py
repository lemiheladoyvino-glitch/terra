from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Any

from server.game.entities import MOVE_SPEED, Entity, Position
from server.game.world import World
from server.game.worldgen import CHUNK_SIZE, TerrainKind
from server.net.protocol import SCHEMA_VERSION, ProtocolError, encode, encode_chunk_rle

if TYPE_CHECKING:
    from server.net.connection import Connection, ConnectionRegistry

MOVEMENT_HZ = 10
MAX_STEP = MOVE_SPEED / MOVEMENT_HZ
COLLISION_EPSILON = 1e-8


class Simulation:
    """Synchronous authoritative work on the connections' asyncio event loop."""

    def __init__(self, world: World, registry: ConnectionRegistry) -> None:
        self.world = world
        self.registry = registry
        self.spawn_position = self._find_spawn()

    def _find_spawn(self) -> Position:
        terrain = self.world.terrain
        center = terrain.size // 2
        # Chebyshev rings, each in row-major order, including the center first.
        for radius in range(terrain.size):
            for y in range(center - radius, center + radius + 1):
                for x in range(center - radius, center + radius + 1):
                    if max(abs(x - center), abs(y - center)) != radius:
                        continue
                    if terrain.in_bounds(x, y) and terrain.at(x, y) in {
                        TerrainKind.GRASS, TerrainKind.SAND,
                    }:
                        return x + 0.5, y + 0.5
        raise ValueError("world has no grass or sand spawn tile")

    def add_player(self, connection: Connection) -> None:
        self.world.add_entity(Entity(
            connection.id, "player", self.spawn_position,
            {"name": f"Wanderer-{connection.id[:4]}", "hp": 100},
        ))

    def remove_player(self, connection: Connection) -> None:
        if connection.id in self.world.entities:
            self.world.remove_entity(connection.id)
        connection.move_intent = None
        connection.known_records.clear()
        connection.sent_chunks.clear()
        connection.bootstrapped = False

    def movement_tick(self) -> None:
        connections = tuple(self.registry.connections.values())
        for connection in connections:
            if not connection.closing and connection.id in self.world.entities:
                self._move(connection)
        self.world.tick_count += 1
        for connection in connections:
            if not connection.closing and connection.id in self.world.entities:
                self._stream(connection)

    def sim_tick(self) -> None:
        """Reserved for 1 Hz village/world simulation in E4."""

    def _move(self, connection: Connection) -> None:
        intent = connection.move_intent
        if intent is None:
            return
        start = self.world.entities[connection.id].position
        target = intent.get("target")
        if target is not None:
            dx, dy = target["x"] - start[0], target["y"] - start[1]
        else:
            dx, dy = intent["direction"]["x"], intent["direction"]["y"]
        distance = math.hypot(dx, dy)
        if distance == 0:
            connection.move_intent = None
            return
        # Scaling first avoids overflowing hypot on otherwise finite wire coordinates.
        scale = max(abs(dx), abs(dy))
        ux, uy = dx / scale, dy / scale
        magnitude = math.hypot(ux, uy)
        step = min(MAX_STEP, distance) if target is not None else MAX_STEP
        end = (start[0] + ux / magnitude * step, start[1] + uy / magnitude * step)
        if target is not None and distance <= MAX_STEP:
            end = (target["x"], target["y"])
        position = self._traverse(start, end)
        if position != start:
            self.world.move_entity(connection.id, position)
        if target is not None and position == (target["x"], target["y"]):
            connection.move_intent = None

    def _traverse(self, start: Position, end: Position) -> Position:
        """Visit every segment interval between grid crossings; never skip a tile.

        Test crossing points as well as interval interiors, including corner ties.
        Back off along the segment by an epsilon before a blocked crossing.
        """
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length == 0:
            return start
        crossings = {0.0, 1.0}
        for origin, destination in zip(start, end):
            change = destination - origin
            if change:
                for boundary in range(math.floor(min(origin, destination)) + 1,
                                      math.floor(max(origin, destination)) + 1):
                    t = (boundary - origin) / change
                    if 0 < t < 1:
                        crossings.add(t)
        times = sorted(crossings)

        def point(t: float) -> Position:
            return start[0] + dx * t, start[1] + dy * t

        def walkable(t: float) -> bool:
            x, y = point(t)
            return self.world.terrain.walkable(math.floor(x), math.floor(y))

        for left, right in zip(times, times[1:]):
            if not walkable(left) or not walkable((left + right) / 2):
                return point(max(0.0, left - COLLISION_EPSILON / length))
        if not walkable(1.0):
            return point(max(0.0, 1.0 - COLLISION_EPSILON / length))
        return end

    def _enqueue(self, connection: Connection, message: dict[str, Any]) -> bool:
        try:
            raw = encode(message)
            connection.send_queue.put_nowait(raw)
        except asyncio.QueueFull:
            connection.close_soon(1011)
            return False
        except ProtocolError as exc:
            if str(exc) != "message too large":
                raise
            connection.close_soon(1009)
            return False
        return True

    def _stream(self, connection: Connection) -> None:
        player = self.world.entities[connection.id]
        terrain = self.world.terrain
        x, y = player.position
        radius = connection.aoi_radius
        chunk_count = (terrain.size + CHUNK_SIZE - 1) // CHUNK_SIZE
        for cy in range(chunk_count):
            for cx in range(chunk_count):
                if (cx, cy) in connection.sent_chunks:
                    continue
                closest_x = min(max(x, cx * CHUNK_SIZE), min((cx + 1) * CHUNK_SIZE, terrain.size))
                closest_y = min(max(y, cy * CHUNK_SIZE), min((cy + 1) * CHUNK_SIZE, terrain.size))
                if (x - closest_x) ** 2 + (y - closest_y) ** 2 > radius ** 2:
                    continue
                if not self._enqueue(connection, {
                    "t": "chunk", "v": SCHEMA_VERSION, "cx": cx, "cy": cy, "size": CHUNK_SIZE,
                    "tiles": encode_chunk_rle(terrain.chunk_tiles(cx, cy)),
                }):
                    return
                connection.sent_chunks.add((cx, cy))
        records = {entity.id: entity.to_record()
                   for entity in self.world.query_radius(player.position, radius)}
        if not connection.bootstrapped:
            message = {"t": "snapshot", "v": SCHEMA_VERSION, "tick": self.world.tick_count,
                       "entities": list(records.values())}
        else:
            known = connection.known_records
            message = {
                "t": "delta", "v": SCHEMA_VERSION, "tick": self.world.tick_count,
                "entered": [record for id, record in records.items() if id not in known],
                "left": sorted(known.keys() - records.keys()),
                "changed": [record for id, record in records.items()
                            if id in known and record != known[id]],
            }
        if self._enqueue(connection, message):
            connection.known_records = records
            connection.bootstrapped = True
