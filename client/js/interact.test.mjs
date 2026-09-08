import test from "node:test";
import assert from "node:assert/strict";
import { pickInteractable, inInteractRange } from "./interact.js";
const entity = (id, kind, x, y) => ({ id, kind, position: { x, y } });
const player = { x: 0, y: 0 };

test("picker filters by player range before measuring click distance", () => {
  const records = [entity("far", "tree", 2, 0), entity("near", "rock", 1, 0), entity("v", "villager", 1.5, 0)];
  assert.deepEqual(pickInteractable(records, { x: 2, y: 0 }, player), { entityId: "near", action: "mine" });
  assert.equal(pickInteractable([records[0], records[2]], player, player), null);
  assert.equal(pickInteractable(records, player, null), null);
});

test("picker maps all survival actions, includes range boundary, and breaks ties by ID", () => {
  for (const [kind, action] of [["tree", "chop"], ["rock", "mine"], ["berry-bush", "eat"], ["grave", "pickup"]]) {
    const record = entity("a", kind, 1.6, 0);
    assert.equal(inInteractRange(record, player), true);
    assert.deepEqual(pickInteractable([record], player, player), { entityId: "a", action });
  }
  const a = entity("a", "tree", -1, 0), b = entity("b", "rock", 1, 0);
  assert.equal(pickInteractable([b, a], player, player).entityId, "a");
  assert.equal(pickInteractable([a, b], { x: 0.8, y: 0 }, player).entityId, "b");
});
