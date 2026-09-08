import { CHUNK_SIZE, TERRAIN_PALETTE } from "./constants.js";

export function decodeChunkRle(runs) {
  const length = CHUNK_SIZE * CHUNK_SIZE;
  if (!Array.isArray(runs) || runs.length === 0 || runs.length > length) {
    throw new Error("Invalid chunk RLE");
  }
  let total = 0;
  for (const run of runs) {
    if (!Array.isArray(run) || run.length !== 2 ||
        !Number.isSafeInteger(run[0]) || run[0] <= 0 ||
        !Number.isInteger(run[1]) || run[1] < 0 || run[1] >= TERRAIN_PALETTE.length) {
      throw new Error("Invalid chunk run");
    }
    total += run[0];
    if (total > length) throw new Error("Chunk exceeds 1024 tiles");
  }
  if (total !== length) throw new Error("Chunk must contain 1024 tiles");
  const tiles = new Uint8Array(length);
  let offset = 0;
  for (const [count, tile] of runs) {
    tiles.fill(tile, offset, offset + count);
    offset += count;
  }
  return tiles;
}
