import { INTERACT_RANGE } from "./constants.js";

export const INTERACTIONS = Object.freeze({
  tree: "chop", rock: "mine", "berry-bush": "eat", grave: "pickup",
});

export function inInteractRange(record, player) {
  return Boolean(player && Object.hasOwn(INTERACTIONS, record.kind) &&
    Math.hypot(record.position.x - player.x, record.position.y - player.y) <= INTERACT_RANGE);
}

// Both points are world tile coordinates. Filter by player range first, then
// choose nearest to the click; equal distances are broken by stable entity ID.
export function pickInteractable(records, click, player) {
  let nearest = null, distance = Infinity;
  for (const record of records) {
    if (!inInteractRange(record, player)) continue;
    const d = Math.hypot(record.position.x - click.x, record.position.y - click.y);
    if (d < distance || (d === distance && record.id < nearest.id)) {
      nearest = record;
      distance = d;
    }
  }
  return nearest ? { entityId: nearest.id, action: INTERACTIONS[nearest.kind] } : null;
}
