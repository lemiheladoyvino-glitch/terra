from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class World:
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    tick_count: int = 0
