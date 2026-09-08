import {
  TILE_PX, CHUNK_SIZE, MOVE_SPEED, INTERP_DELAY_MS, SAMPLE_WINDOW_MS,
  SNAP_DISTANCE, CORRECTION_MS, COLLISION_EPSILON, TERRAIN_PALETTE,
} from "./constants.js";
import { decodeChunkRle } from "./rle.js";
import { INTERACTIONS, inInteractRange } from "./interact.js";

const PALETTE = TERRAIN_PALETTE.map((color) => Number.parseInt(color.slice(1), 16));

export class WorldRenderer {
  constructor(scene) {
    this.scene = scene;
    this.chunks = new Map();
    this.chunkVisuals = new Map();
    this.entities = new Map();
    this.reset();
  }

  reset() {
    for (const visual of this.chunkVisuals.values()) visual.destroy();
    for (const view of this.entities.values()) view.object.destroy();
    this.chunks.clear();
    this.chunkVisuals.clear();
    this.entities.clear();
    this.metadata = null;
    this.predicted = null;
    this.correction = { x: 0, y: 0 };
    this.tick = null;
    this.tickReceivedAt = null;
    this.bootstrapped = false;
  }

  welcome(metadata, tick, receivedAt) {
    this.metadata = metadata;
    this.clock(tick, receivedAt);
    this.scene.cameras.main.setBounds(0, 0, metadata.worldSize * TILE_PX, metadata.worldSize * TILE_PX);
    this.scene.cameras.main.roundPixels = true;
  }

  clock(tick, receivedAt) {
    if (this.tick === null || tick > this.tick) {
      this.tick = tick;
      this.tickReceivedAt = receivedAt;
    }
  }

  serverTime(now) {
    return this.tick * (1000 / this.metadata.config.movement_hz) + now - this.tickReceivedAt;
  }

  chunk(message) {
    if (!this.metadata || message.size !== CHUNK_SIZE) throw new Error("Invalid chunk size");
    const key = `${message.cx},${message.cy}`;
    if (this.chunks.has(key)) return;
    const tiles = decodeChunkRle(message.tiles);
    const graphic = this.scene.add.graphics({
      x: message.cx * this.metadata.chunkSize * TILE_PX,
      y: message.cy * this.metadata.chunkSize * TILE_PX,
    }).setDepth(0);
    for (let i = 0; i < tiles.length; i++) {
      graphic.fillStyle(PALETTE[tiles[i]], 1);
      graphic.fillRect((i % CHUNK_SIZE) * TILE_PX, Math.floor(i / CHUNK_SIZE) * TILE_PX, TILE_PX, TILE_PX);
    }
    this.chunks.set(key, tiles);
    this.chunkVisuals.set(key, graphic);
  }

  createEntity(record, time) {
    if (this.entities.has(record.id)) throw new Error("Duplicate entity");
    const own = record.id === this.metadata.entityId;
    const graphic = this.scene.add.graphics();
    switch (record.kind) {
      case "player":
        graphic.fillStyle(own ? 0xffd479 : 0xe0e0e0).fillCircle(0, 0, 8);
        graphic.lineStyle(2, 0x243025).strokeCircle(0, 0, 8);
        break;
      case "villager": graphic.fillStyle(0xcdb4db).fillCircle(0, 0, 6); break;
      case "tree":
        graphic.fillStyle(0x6b4931).fillRect(-2, 2, 4, 8);
        graphic.fillStyle(0x2f5d2f).fillTriangle(-10, 4, 0, -15, 10, 4);
        break;
      case "rock": graphic.fillStyle(0x6b6b6b).fillCircle(0, 0, 8); break;
      case "berry-bush": graphic.fillStyle(0xb04c93).fillCircle(0, 0, 5); break;
      case "building":
        graphic.fillStyle(0xac8661).fillRect(-11, -6, 22, 18);
        graphic.fillStyle(0x774e40).fillTriangle(-13, -6, 0, -16, 13, -6);
        break;
      case "grave": graphic.fillStyle(0xb1b3ba).fillRoundedRect(-5, -9, 10, 17, 3); break;
      case "merchant": graphic.fillStyle(0xe5ab61).fillTriangle(-8, 8, 0, -10, 8, 8); break;
      case "road": graphic.fillStyle(0xab997d).fillRect(-12, -5, 24, 10); break;
      default: throw new Error(`Unknown entity kind: ${record.kind}`);
    }
    const object = this.scene.add.container(record.position.x * TILE_PX, record.position.y * TILE_PX, [graphic]);
    object.setDepth(record.kind === "road" ? 1 : own ? 30 : record.kind === "player" ? 20 : 10);
    let label = null;
    if (record.kind === "player") {
      label = this.scene.add.text(0, -14, record.name, {
        fontFamily: "system-ui, sans-serif", fontSize: "10px", color: own ? "#ffe7b1" : "#ffffff",
        stroke: "#18231c", strokeThickness: 3,
      }).setOrigin(0.5, 1);
      object.add(label);
    }
    let highlight = null;
    if (Object.hasOwn(INTERACTIONS, record.kind)) {
      highlight = this.scene.add.graphics();
      highlight.lineStyle(1.5, 0xffe5a3, 0.7).strokeCircle(0, 0, 13);
      highlight.setVisible(false);
      object.addAt(highlight, 0);
    }
    const view = { record, object, label, highlight, samples: [] };
    this.entities.set(record.id, view);
    this.sample(view, time);
    if (own) this.reconcile(record.position);
  }

  sample(view, time) {
    view.samples.push({ time, ...view.record.position });
    // Retain one sample preceding the one-second window for bracketing.
    while (view.samples.length > 2 && view.samples[1].time < time - SAMPLE_WINDOW_MS) {
      view.samples.shift();
    }
  }

  snapshot(message, receivedAt) {
    for (const view of this.entities.values()) view.object.destroy();
    this.entities.clear();
    this.predicted = null;
    this.correction = { x: 0, y: 0 };
    this.clock(message.tick, receivedAt);
    const time = message.tick * (1000 / this.metadata.config.movement_hz);
    for (const record of message.entities) this.createEntity(record, time);
    this.bootstrapped = true;
  }

  delta(message, receivedAt) {
    if (!this.bootstrapped || message.tick <= this.tick) throw new Error("Out-of-order entity state");
    for (const id of message.left) {
      const view = this.entities.get(id);
      if (!view) throw new Error("Unknown left entity");
      view.object.destroy();
      this.entities.delete(id);
    }
    for (const record of message.changed) {
      const view = this.entities.get(record.id);
      if (!view) throw new Error("Unknown changed entity");
      view.record = record;
      view.label?.setText(record.name);
    }
    const time = message.tick * (1000 / this.metadata.config.movement_hz);
    // An unchanged entity is also an authoritative sample at this tick.
    for (const view of this.entities.values()) this.sample(view, time);
    for (const record of message.entered) this.createEntity(record, time);
    const own = this.entities.get(this.metadata.entityId);
    if (own) this.reconcile(own.record.position);
    this.clock(message.tick, receivedAt);
  }

  reconcile(position) {
    const previous = this.predicted;
    if (previous && Math.hypot(previous.x - position.x, previous.y - position.y) <= SNAP_DISTANCE) {
      // Reset physics immediately; preserve a temporary visual offset for easing.
      this.correction.x += previous.x - position.x;
      this.correction.y += previous.y - position.y;
    } else {
      this.correction = { x: 0, y: 0 };
    }
    this.predicted = { ...position };
  }

  walkable(x, y) {
    const size = this.metadata.worldSize;
    if (x < 0 || y < 0 || x >= size || y >= size) return false;
    const cx = Math.floor(x / CHUNK_SIZE), cy = Math.floor(y / CHUNK_SIZE);
    const tiles = this.chunks.get(`${cx},${cy}`);
    if (!tiles) return true;
    const tile = tiles[(y % CHUNK_SIZE) * CHUNK_SIZE + (x % CHUNK_SIZE)];
    return tile !== 0 && tile !== 1;
  }

  traverse(start, end) {
    // Same grid-crossing intervals, corner checks, and epsilon as Simulation._traverse.
    const dx = end.x - start.x, dy = end.y - start.y;
    const length = Math.hypot(dx, dy);
    if (length === 0) return start;
    const crossings = new Set([0, 1]);
    for (const axis of ["x", "y"]) {
      const change = end[axis] - start[axis];
      if (!change) continue;
      for (let boundary = Math.floor(Math.min(start[axis], end[axis])) + 1;
        boundary <= Math.floor(Math.max(start[axis], end[axis])); boundary++) {
        const t = (boundary - start[axis]) / change;
        if (t > 0 && t < 1) crossings.add(t);
      }
    }
    const times = [...crossings].sort((a, b) => a - b);
    const point = (t) => ({ x: start.x + dx * t, y: start.y + dy * t });
    const valid = (t) => {
      const p = point(t);
      return this.walkable(Math.floor(p.x), Math.floor(p.y));
    };
    for (let i = 0; i < times.length - 1; i++) {
      const left = times[i], right = times[i + 1];
      if (!valid(left) || !valid((left + right) / 2)) {
        return point(Math.max(0, left - COLLISION_EPSILON / length));
      }
    }
    if (!valid(1)) return point(Math.max(0, 1 - COLLISION_EPSILON / length));
    return end;
  }

  interpolate(samples, time) {
    for (let i = 1; i < samples.length; i++) {
      const a = samples[i - 1], b = samples[i];
      if (a.time <= time && time <= b.time) {
        const alpha = (time - a.time) / (b.time - a.time);
        return { x: a.x + (b.x - a.x) * alpha, y: a.y + (b.y - a.y) * alpha };
      }
    }
    return samples[samples.length - 1]; // Hold newest; never extrapolate.
  }

  update(now, dt, direction) {
    if (!this.metadata || !this.bootstrapped) return;
    const length = Math.hypot(direction.x, direction.y);
    if (this.predicted && length) {
      const distance = MOVE_SPEED * dt / 1000;
      this.predicted = this.traverse(this.predicted, {
        x: this.predicted.x + direction.x / length * distance,
        y: this.predicted.y + direction.y / length * distance,
      });
    }
    const decay = Math.exp(-dt / CORRECTION_MS);
    this.correction.x *= decay;
    this.correction.y *= decay;
    const renderTime = this.serverTime(now) - INTERP_DELAY_MS;
    for (const [id, view] of this.entities) {
      view.highlight?.setVisible(inInteractRange(view.record, this.predicted));
      const own = id === this.metadata.entityId;
      const position = own && this.predicted ? {
        x: this.predicted.x + this.correction.x,
        y: this.predicted.y + this.correction.y,
      } : this.interpolate(view.samples, renderTime);
      view.object.setPosition(position.x * TILE_PX, position.y * TILE_PX);
      if (own) {
        const camera = this.scene.cameras.main;
        camera.centerOn(view.object.x, view.object.y);
        camera.setScroll(Math.round(camera.scrollX), Math.round(camera.scrollY));
      }
    }
  }
}
