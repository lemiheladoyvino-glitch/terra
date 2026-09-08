from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

Position = tuple[float, float]
MOVE_SPEED = 4.0  # Tiles per second; integration is deferred to E1.2.


@dataclass(frozen=True)
class Entity:
    """Replace via World.move_entity to keep the spatial index synchronized.

    Fields hold the kind-specific record fields (all nine MVP kinds are supported).
    Callers supply valid kind fields; wire validation belongs to the protocol layer.
    """

    id: str
    kind: str
    position: Position
    fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.position) != 2 or not all(math.isfinite(v) for v in self.position):
            raise ValueError("position must have two finite coordinates")
        if {"id", "kind", "position"} & self.fields.keys():
            raise ValueError("kind fields cannot override common entity fields")

    def to_record(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind,
                "position": {"x": self.position[0], "y": self.position[1]}, **self.fields}
