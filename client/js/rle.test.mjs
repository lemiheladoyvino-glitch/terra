import test from "node:test";
import assert from "node:assert/strict";
import { decodeChunkRle } from "./rle.js";

// Test-side encoder for canonical round trips; not shipped as client wire logic.
function encode(tiles) {
  const runs = [];
  for (const tile of tiles) {
    const last = runs.at(-1);
    if (last?.[1] === tile) last[0]++;
    else runs.push([1, tile]);
  }
  return runs;
}

for (const [name, runs] of [
  ["all water", [[1024, 0]]],
  ["mixed terrain", [[128, 0], [64, 1], [64, 2], [256, 3], [256, 4], [256, 5]]],
  ["alternating tiles", Array.from({ length: 1024 }, (_, i) => [1, i % 6])],
]) {
  test(`round trip: ${name}`, () => {
    const tiles = decodeChunkRle(runs);
    assert.ok(tiles instanceof Uint8Array);
    assert.equal(tiles.length, 1024);
    assert.deepEqual([...tiles], runs.flatMap(([length, tile]) => Array(length).fill(tile)));
    assert.deepEqual(encode(tiles), runs);
  });
}

test("adjacent same-id runs are allowed", () => {
  const tiles = decodeChunkRle([[512, 3], [512, 3]]);
  assert.deepEqual(tiles, new Uint8Array(1024).fill(3));
  assert.deepEqual(decodeChunkRle(encode(tiles)), tiles);
});

test("reject wrong totals and run containers", () => {
  for (const runs of [null, {}, "runs", [], [[1023, 0]], [[1025, 0]], [[512, 0], [513, 1]],
    [[1024]], [[1024, 0, 1]], [null], Array(1025).fill([1, 0])]) {
    assert.throws(() => decodeChunkRle(runs));
  }
});

test("reject invalid tile IDs", () => {
  for (const id of [-1, 6, 3.5, NaN, Infinity, true, "3", null]) {
    assert.throws(() => decodeChunkRle([[1024, id]]));
  }
});

test("reject non-positive, fractional, and unsafe lengths", () => {
  for (const length of [0, -1, 0.5, NaN, Infinity, true, "1024", null, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => decodeChunkRle([[length, 0]]));
  }
});
