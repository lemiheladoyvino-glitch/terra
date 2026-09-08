from __future__ import annotations

import math

from server.game.entities import Position
from server.game.worldgen import Terrain

COLLISION_EPSILON = 1e-8


def traverse(terrain: Terrain, start: Position, end: Position) -> Position:
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
        return terrain.walkable(math.floor(x), math.floor(y))

    for left, right in zip(times, times[1:]):
        if not walkable(left) or not walkable((left + right) / 2):
            return point(max(0.0, left - COLLISION_EPSILON / length))
    if not walkable(1.0):
        return point(max(0.0, 1.0 - COLLISION_EPSILON / length))
    return end

