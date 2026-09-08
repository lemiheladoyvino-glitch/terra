from __future__ import annotations

import math
import random
from dataclasses import dataclass
from enum import IntEnum

CHUNK_SIZE = 32


class TerrainKind(IntEnum):
    DEEP_WATER = 0
    SHALLOW_WATER = 1
    SAND = 2
    GRASS = 3
    ROCK = 4
    FOREST_FLOOR = 5


@dataclass(frozen=True)
class Terrain:
    """Immutable row-major bytes: grid[y * size + x] is a TerrainKind ordinal."""

    grid: bytes
    size: int
    seed: int

    def __post_init__(self) -> None:
        if self.size < 2 or len(self.grid) != self.size * self.size:
            raise ValueError("terrain must be a square of size >= 2")
        if any(tile > 5 for tile in self.grid):
            raise ValueError("unknown terrain kind")

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.size and 0 <= y < self.size

    def at(self, x: int, y: int) -> TerrainKind:
        if not self.in_bounds(x, y):
            raise IndexError((x, y))
        return TerrainKind(self.grid[y * self.size + x])

    def walkable(self, x: int, y: int) -> bool:
        return self.in_bounds(x, y) and self.at(x, y) not in {
            TerrainKind.DEEP_WATER, TerrainKind.SHALLOW_WATER,
        }

    def chunk_tiles(self, cx: int, cy: int) -> bytes:
        """Return 32x32 row-major tiles; partial edge chunks pad with deep water."""
        if not (0 <= cx < math.ceil(self.size / CHUNK_SIZE)
                and 0 <= cy < math.ceil(self.size / CHUNK_SIZE)):
            raise IndexError((cx, cy))
        return bytes(
            self.at(x, y) if self.in_bounds(x, y) else TerrainKind.DEEP_WATER
            for y in range(cy * CHUNK_SIZE, (cy + 1) * CHUNK_SIZE)
            for x in range(cx * CHUNK_SIZE, (cx + 1) * CHUNK_SIZE)
        )


class _ValueNoise:
    def __init__(self, rng: random.Random) -> None:
        self.values = [[rng.random() for _ in range(33)] for _ in range(33)]

    def sample(self, x: float, y: float) -> float:
        ix, iy = math.floor(x), math.floor(y)
        tx, ty = x - ix, y - iy
        tx, ty = tx * tx * (3 - 2 * tx), ty * ty * (3 - 2 * ty)
        # Periodic wrapping supports any finite coordinate without changing the
        # existing table or PRNG draws (and therefore existing island output).
        size = len(self.values)
        x0, x1 = ix % size, (ix + 1) % size
        y0, y1 = iy % size, (iy + 1) % size
        a, b = self.values[y0][x0], self.values[y0][x1]
        c, d = self.values[y1][x0], self.values[y1][x1]
        return (a + (b - a) * tx) * (1 - ty) + (c + (d - c) * tx) * ty

    def fractal(self, x: float, y: float) -> float:
        return sum(self.sample(x * frequency, y * frequency) * weight
                   for frequency, weight in ((3, 8), (6, 4), (12, 2), (24, 1))) / 15


def generate_island(seed: int, size: int = 192) -> Terrain:
    """Generate deterministic four-octave value noise with radial elevation falloff.

    Supports integer sizes >= 2; all edge tiles are deep water. Tiny grids may
    contain no land. PRNG state is local and never changes the global generator.
    """
    if type(seed) is not int or type(size) is not int or size < 2:
        raise ValueError("seed and size must be integers; size must be >= 2")
    rng = random.Random(seed)
    elevation_noise, moisture_noise = _ValueNoise(rng), _ValueNoise(rng)
    tiles = bytearray(size * size)
    for y in range(1, size - 1):
        for x in range(1, size - 1):
            nx, ny = x / (size - 1), y / (size - 1)
            radius = math.hypot(2 * nx - 1, 2 * ny - 1)
            elevation = 1 - radius + 0.30 * (elevation_noise.fractal(nx, ny) - 0.5)
            if elevation < 0.12:
                tile = TerrainKind.DEEP_WATER
            elif elevation < 0.20:
                tile = TerrainKind.SHALLOW_WATER
            elif elevation < 0.27:
                tile = TerrainKind.SAND
            elif elevation > 0.72:
                tile = TerrainKind.ROCK
            elif moisture_noise.fractal(nx, ny) > 0.50:
                tile = TerrainKind.FOREST_FLOOR
            else:
                tile = TerrainKind.GRASS
            tiles[y * size + x] = tile
    return Terrain(bytes(tiles), size, seed)
