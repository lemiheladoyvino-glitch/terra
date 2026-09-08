import { SCHEMA_VERSION } from "./constants.js";

const INBOUND = new Set(["welcome", "chunk", "snapshot", "delta", "inventory", "event", "chat", "error"]);

export class Network {
  constructor(handlers, onStatus, onReset) {
    this.handlers = handlers;
    this.onStatus = onStatus;
    this.onReset = onReset;
    this.socket = null;
    this.timer = null;
    this.welcome = null;
    this.lastDirection = null;
    this.stopped = false;
  }

  connect() {
    if (this.stopped) return;
    this.onStatus("connecting");
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    let socket;
    try {
      socket = new WebSocket(`${protocol}//${location.host}/ws`);
    } catch (error) {
      console.error("Terra connection failed", error);
      this.retry();
      return;
    }
    this.socket = socket;
    socket.addEventListener("message", ({ data }) => {
      if (socket !== this.socket || this.stopped) return;
      let message;
      try {
        message = JSON.parse(data);
      } catch (error) {
        console.warn("Terra malformed frame", error);
        return;
      }
      if (!message || message.v !== SCHEMA_VERSION || !INBOUND.has(message.t)) return;
      try {
        if (message.t === "welcome") {
          this.welcome = {
            entityId: message.entity_id,
            worldSize: message.world_size,
            chunkSize: message.chunk_size,
            seed: message.seed,
            config: message.config,
          };
          this.onStatus("connected");
        }
        this.handlers[message.t]?.(message, performance.now());
      } catch (error) {
        // Bad state requires a fresh snapshot, not a partially applied world.
        console.error("Terra invalid state; reconnecting", error);
        socket.close(1000, "state reset");
      }
    });
    socket.addEventListener("error", () => {
      if (socket === this.socket) socket.close();
    });
    socket.addEventListener("close", () => {
      if (socket !== this.socket) return;
      this.socket = null;
      this.retry();
    });
  }

  retry() {
    this.welcome = null;
    this.lastDirection = null;
    this.onReset();
    if (this.stopped) return;
    this.onStatus("disconnected");
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.connect(), 1000);
  }

  sendMove(direction) {
    if (!this.welcome || this.socket?.readyState !== WebSocket.OPEN) return;
    if (this.lastDirection?.x === direction.x && this.lastDirection?.y === direction.y) return;
    this.socket.send(JSON.stringify({ t: "move", v: SCHEMA_VERSION, direction }));
    this.lastDirection = { ...direction };
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.timer);
    this.socket?.close();
  }
}
