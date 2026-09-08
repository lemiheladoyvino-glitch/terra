from __future__ import annotations

import math
from typing import Any, TypedDict

from server.game.items import ITEMS
from server.game.survival import INVENTORY_SLOTS, ItemStack


class StoredPlayer(TypedDict):
    player_id: str
    token: str
    name: str
    hp: int
    hunger: int
    inventory: list[ItemStack | None]
    position: list[float]


class PlayerStore:
    def __init__(self) -> None:
        self.players: dict[str, StoredPlayer] = {}


def validate_player(data: Any) -> StoredPlayer:
    """Validate persisted state without putting credentials into error messages."""
    if not isinstance(data, dict) or set(data) != {
        "player_id",
        "token",
        "name",
        "hp",
        "hunger",
        "inventory",
        "position",
    }:
        raise ValueError("invalid player record")
    for key in ("player_id", "token", "name"):
        if not isinstance(data[key], str) or not 1 <= len(data[key]) <= 128:
            raise ValueError("invalid player identity")
    for key in ("hp", "hunger"):
        if type(data[key]) is not int or not 0 <= data[key] <= 100:
            raise ValueError("invalid player vitals")
    position = data["position"]
    if (
        not isinstance(position, list)
        or len(position) != 2
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in position)
    ):
        raise ValueError("invalid player position")
    slots = data["inventory"]
    if not isinstance(slots, list) or len(slots) != INVENTORY_SLOTS:
        raise ValueError("invalid player inventory")
    for stack in slots:
        if stack is None:
            continue
        if not isinstance(stack, dict) or set(stack) != {"item_id", "quantity", "durability"}:
            raise ValueError("invalid inventory slot")
        if not isinstance(stack["item_id"], str) or stack["item_id"] not in ITEMS:
            raise ValueError("invalid inventory item")
        spec = ITEMS[stack["item_id"]]
        if type(stack["quantity"]) is not int or not 1 <= stack["quantity"] <= spec.max_stack:
            raise ValueError("invalid inventory quantity")
        durability = stack["durability"]
        if spec.max_durability is None:
            if durability is not None:
                raise ValueError("invalid non-tool durability")
        elif type(durability) is not int or not 1 <= durability <= spec.max_durability:
            raise ValueError("invalid tool durability")
    return data
