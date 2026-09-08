from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict

from server.game.items import ITEMS

INTERACT_RANGE = 1.6
HUNGER_DECAY = 1
STARVE_DAMAGE = 2
GRAVE_TTL = 9000
INVENTORY_SLOTS = 16
BERRY_HUNGER = 8


class ItemStack(TypedDict):
    item_id: str
    quantity: int
    durability: int | None  # Remaining uses; None for non-tools.


class InventoryRecord(TypedDict):
    slot: int
    item_id: str
    quantity: int
    durability: float


@dataclass
class PlayerState:
    inventory: list[ItemStack | None] = field(default_factory=lambda: [None] * INVENTORY_SLOTS)
    hp: int = 100
    hunger: int = 100


def add_item(state: PlayerState, item_id: str, qty: int, *, durability: int | None = None) -> int:
    """Fill existing stacks then empty slots; return leftovers. Tools retain supplied uses."""
    spec = ITEMS[item_id]
    if type(qty) is not int or qty < 0:
        raise ValueError("quantity must be a nonnegative integer")
    remaining = spec.max_durability if durability is None else durability
    if spec.max_durability is not None and (
        type(remaining) is not int or not 1 <= remaining <= spec.max_durability
    ):
        raise ValueError("invalid tool durability")
    if spec.max_durability is None and durability is not None:
        raise ValueError("non-tools have no durability")
    if spec.stackable:
        for stack in state.inventory:
            if stack is not None and stack["item_id"] == item_id:
                count = min(qty, spec.max_stack - stack["quantity"])
                stack["quantity"] += count
                qty -= count
    for slot, stack in enumerate(state.inventory):
        if qty == 0:
            break
        if stack is None:
            count = min(qty, spec.max_stack)
            state.inventory[slot] = {"item_id": item_id, "quantity": count, "durability": remaining}
            qty -= count
    return qty


def consume_item(state: PlayerState, item_id: str, qty: int = 1) -> bool:
    """Consume across slots atomically; insufficient quantity leaves inventory unchanged."""
    if type(qty) is not int or qty < 0:
        raise ValueError("quantity must be a nonnegative integer")
    if sum(s["quantity"] for s in state.inventory if s and s["item_id"] == item_id) < qty:
        return False
    for slot, stack in enumerate(state.inventory):
        if stack is not None and stack["item_id"] == item_id:
            count = min(qty, stack["quantity"])
            stack["quantity"] -= count
            qty -= count
            if stack["quantity"] == 0:
                state.inventory[slot] = None
        if qty == 0:
            break
    return True


def find_tool(state: PlayerState, tool_type: str) -> int | None:
    return next((slot for slot, stack in enumerate(state.inventory)
                 if stack is not None and ITEMS[stack["item_id"]].tool == tool_type
                 and stack["durability"] is not None and stack["durability"] > 0), None)


def damage_tool(state: PlayerState, slot: int) -> bool:
    stack = state.inventory[slot]
    if stack is None or stack["durability"] is None:
        raise ValueError("slot does not contain a tool")
    stack["durability"] -= 1
    if stack["durability"] == 0:
        state.inventory[slot] = None
        return True
    return False


def serialize_inventory(state: PlayerState) -> list[InventoryRecord]:
    records: list[InventoryRecord] = []
    for slot, stack in enumerate(state.inventory):
        if stack is None:
            continue
        maximum = ITEMS[stack["item_id"]].max_durability
        durability = stack["durability"]
        records.append({"slot": slot, "item_id": stack["item_id"], "quantity": stack["quantity"],
                        "durability": durability / maximum if durability is not None and maximum
                        else 1.0})
    return records
