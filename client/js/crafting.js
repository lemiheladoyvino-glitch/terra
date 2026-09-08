// Keep in sync with server/game/recipes.py (schema remains 3).
export const WORKBENCH_RANGE = 2.5;
export const RECIPES = {
  workbench: { output_id: "workbench", output_qty: 1, inputs: { wood: 10 }, needs_workbench: false, places_entity: "workbench" },
  "wooden-axe": { output_id: "wooden-axe", output_qty: 1, inputs: { wood: 5 }, needs_workbench: false },
  "wooden-pickaxe": { output_id: "wooden-pickaxe", output_qty: 1, inputs: { wood: 5 }, needs_workbench: false },
  "stone-axe": { output_id: "stone-axe", output_qty: 1, inputs: { wood: 3, stone: 3 }, needs_workbench: true },
  "stone-pickaxe": { output_id: "stone-pickaxe", output_qty: 1, inputs: { wood: 3, stone: 3 }, needs_workbench: true },
  "padded-armor": { output_id: "padded-armor", output_qty: 1, inputs: { wood: 8, berries: 4 }, needs_workbench: true },
};

export function affordableRecipes(recipes, inventorySlots, nearWorkbench) {
  const totals = new Map();
  for (const slot of inventorySlots) {
    if (slot) totals.set(slot.item_id, (totals.get(slot.item_id) ?? 0) + slot.quantity);
  }
  return Object.entries(recipes).map(([id, recipe]) => {
    const missing = Object.entries(recipe.inputs).filter(([item, qty]) => (totals.get(item) ?? 0) < qty);
    const reason = recipe.needs_workbench && !nearWorkbench ? "need workbench" :
      missing.length ? `missing materials: ${missing.map(([item]) => item).join(", ")}` : null;
    return { id, inputs: recipe.inputs, output: { id: recipe.output_id, quantity: recipe.output_qty }, canCraft: reason === null, reason };
  });
}
