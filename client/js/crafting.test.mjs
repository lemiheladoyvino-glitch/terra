import test from "node:test";
import assert from "node:assert/strict";
import { RECIPES, affordableRecipes } from "./crafting.js";
const slot = (item_id, quantity) => ({ item_id, quantity });

test("crafting lists all recipes and material reasons", () => {
  const rows = affordableRecipes(RECIPES, [], false);
  assert.equal(rows.length, 6);
  assert.equal(rows.find((r) => r.id === "wooden-axe").canCraft, false);
  assert.match(rows.find((r) => r.id === "wooden-axe").reason, /missing materials/);
  assert.equal(rows.find((r) => r.id === "stone-axe").reason, "need workbench");
});

test("materials aggregate across slots without mutation", () => {
  const slots = [slot("wood", 2), null, slot("wood", 3)];
  const before = structuredClone(slots);
  const rows = affordableRecipes(RECIPES, slots, false);
  assert.equal(rows.find((r) => r.id === "wooden-axe").canCraft, true);
  assert.equal(rows.find((r) => r.id === "workbench").canCraft, false);
  assert.deepEqual(slots, before);
});

test("workbench toggle gates advanced recipes and every recipe can be afforded", () => {
  const slots = [slot("wood", 99), slot("stone", 99), slot("berries", 99)];
  const away = affordableRecipes(RECIPES, slots, false);
  const near = affordableRecipes(RECIPES, slots, true);
  assert.equal(away.filter((r) => r.canCraft).length, 3);
  assert.ok(near.every((r) => r.canCraft && r.reason === null));
  assert.deepEqual(near.find((r) => r.id === "padded-armor").output, { id: "padded-armor", quantity: 1 });
  assert.equal(affordableRecipes(RECIPES, slots, false).find((r) => r.id === "stone-axe").canCraft, false);
});
