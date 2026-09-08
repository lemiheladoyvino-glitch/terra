from __future__ import annotations

from dataclasses import dataclass

from server.game.survival import PlayerState

WORKBENCH_RANGE = 2.5


@dataclass(frozen=True)
class Recipe:
    output_id: str
    output_qty: int
    inputs: dict[str, int]
    needs_workbench: bool = False
    places_entity: str | None = None


RECIPES: dict[str, Recipe] = {
    "workbench": Recipe("workbench", 1, {"wood": 10}, places_entity="workbench"),
    "wooden-axe": Recipe("wooden-axe", 1, {"wood": 5}),
    "wooden-pickaxe": Recipe("wooden-pickaxe", 1, {"wood": 5}),
    "stone-axe": Recipe("stone-axe", 1, {"wood": 3, "stone": 3}, True),
    "stone-pickaxe": Recipe("stone-pickaxe", 1, {"wood": 3, "stone": 3}, True),
    "padded-armor": Recipe("padded-armor", 1, {"wood": 8, "berries": 4}, True),
}


def can_craft(state: PlayerState, recipe: Recipe, near_workbench: bool) -> str | None:
    """Check station and materials without mutation; placement/capacity are server checks."""
    if recipe.needs_workbench and not near_workbench:
        return "need a workbench"
    totals: dict[str, int] = {}
    for stack in state.inventory:
        if stack is not None:
            totals[stack["item_id"]] = totals.get(stack["item_id"], 0) + stack["quantity"]
    missing = [f"{item} ({qty - totals.get(item, 0)})" for item, qty in recipe.inputs.items()
               if totals.get(item, 0) < qty]
    return f"missing materials: {', '.join(missing)}" if missing else None
