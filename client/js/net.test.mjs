import test from "node:test";
import assert from "node:assert/strict";
import { Network } from "./net.js";
import { SCHEMA_VERSION } from "./constants.js";

class Socket {
  static OPEN = 1;
  constructor() { this.handlers = {}; this.sent = []; this.readyState = 1; }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  send(raw) { this.sent.push(JSON.parse(raw)); }
  close() {}
}

function environment(t, storage) {
  const previous = [globalThis.WebSocket, globalThis.location, globalThis.localStorage];
  globalThis.WebSocket = Socket;
  globalThis.location = { protocol: "http:", host: "localhost" };
  globalThis.localStorage = storage;
  t.after(() => {
    [globalThis.WebSocket, globalThis.location, globalThis.localStorage] = previous;
  });
}

const welcome = (token) => ({ data: JSON.stringify({ t: "welcome", v: SCHEMA_VERSION,
  token, entity_id: "player", world_size: 192, chunk_size: 32, seed: 42,
  config: { movement_hz: 10, sim_hz: 1, aoi_radius: 20 } }) });

test("stored token is the first frame; welcome persists replacement for reconnect", (t) => {
  const values = new Map([["terra.token", "old-token"]]);
  environment(t, { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) });
  const net = new Network({}, () => {}, () => {});
  net.connect();
  net.sendMove({ x: 1, y: 0 });
  assert.equal(net.socket.sent.length, 0);
  net.socket.handlers.open();
  assert.deepEqual(net.socket.sent, [{ t: "hello", v: 4, token: "old-token" }]);
  net.socket.handlers.message(welcome("new-token"));
  assert.equal(values.get("terra.token"), "new-token");
  net.socket.handlers.close();
  clearTimeout(net.timer);
  net.connect();
  net.socket.handlers.open();
  assert.equal(net.socket.sent[0].token, "new-token");
  net.stop();
});

test("storage failure remains tokenless initially and uses in-memory token on retry", (t) => {
  environment(t, { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } });
  const net = new Network({}, () => {}, () => {});
  net.connect();
  net.socket.handlers.open();
  assert.equal(net.socket.sent.length, 0);
  net.socket.handlers.message(welcome("session-token"));
  net.socket.handlers.close();
  clearTimeout(net.timer);
  net.connect();
  net.socket.handlers.open();
  assert.equal(net.socket.sent[0].token, "session-token");
  net.stop();
});

test("replacement closes do not auto-reconnect and take the session back", (t) => {
  environment(t, { getItem: () => null, setItem() {} });
  const statuses = [];
  const net = new Network({}, (status) => statuses.push(status), () => {});
  net.connect();
  net.socket.handlers.message(welcome("token"));
  net.socket.handlers.close({ code: 1000, reason: "session replaced" });
  assert.equal(net.stopped, true);
  assert.equal(net.timer, null);
  assert.equal(statuses.at(-1), "replaced");
});
