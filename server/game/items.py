from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

AXE_DURABILITY = 40
PICKAXE_DURABILITY = 40


class ItemKind(StrEnum):
    WOOD = "wood"
    STONE = "stone"
    BERRIES = "berries"
    WOODEN_AXE = "wooden-axe"
    WOODEN_PICKAXE = "wooden-pickaxe"
    STONE_AXE = "stone-axe"
    STONE_PICKAXE = "stone-pickaxe"
    PADDED_ARMOR = "padded-armor"


@dataclass(frozen=True)
class ItemSpec:
    stackable: bool
    max_stack: int
    max_durability: int | None = None
    tool: str | None = None
    equip: str | None = None  # Cosmetic until combat exists; no equipment effects yet.


ITEMS: dict[str, ItemSpec] = {
    ItemKind.WOOD: ItemSpec(True, 99),
    ItemKind.STONE: ItemSpec(True, 99),
    ItemKind.BERRIES: ItemSpec(True, 99),
    ItemKind.WOODEN_AXE: ItemSpec(False, 1, AXE_DURABILITY, "axe"),
    ItemKind.WOODEN_PICKAXE: ItemSpec(False, 1, PICKAXE_DURABILITY, "pickaxe"),
    ItemKind.STONE_AXE: ItemSpec(False, 1, 90, "axe"),
    ItemKind.STONE_PICKAXE: ItemSpec(False, 1, 90, "pickaxe"),
    ItemKind.PADDED_ARMOR: ItemSpec(False, 1, equip="armor"),
}
