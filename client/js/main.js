import { TERRAIN_PALETTE, TILE_PX } from "./constants.js";
import { Network } from "./net.js";
import { WorldRenderer } from "./render.js";
import { SurvivalUI } from "./survival-ui.js";
import { CraftingUI } from "./crafting-ui.js";
import { pickInteractable } from "./interact.js";

const statusElement = document.querySelector("#status");
const positionElement = document.querySelector("#position");
const countElement = document.querySelector("#entity-count");
const tickElement = document.querySelector("#server-tick");
const overlay = document.querySelector("#connection-overlay");
const overlayMessage = document.querySelector("#connection-message");
const MOVEMENT_KEYS = new Set(["KeyW", "KeyA", "KeyS", "KeyD", "ArrowUp", "ArrowLeft", "ArrowDown", "ArrowRight"]);

function boot() {
  class IslandScene extends Phaser.Scene {
    create() {
      window.terra.scene = this;
      this.world = new WorldRenderer(this);
      this.survivalUI = new SurvivalUI();
      this.craftingUI = new CraftingUI((id) => this.net.sendCraft(id));
      this.heldKeys = new Set();
      this.connected = false;
      this.net = new Network({
        welcome: (message, receivedAt) => {
          this.world.welcome(this.net.welcome, message.tick, receivedAt);
          console.log("Terra welcome", this.net.welcome);
        },
        chunk: (message) => this.world.chunk(message),
        snapshot: (message, receivedAt) => this.world.snapshot(message, receivedAt),
        delta: (message, receivedAt) => this.world.delta(message, receivedAt),
        inventory: (message) => {
          this.survivalUI.setInventory(message.slots);
          this.craftingUI.setInventory(message.slots);
        },
        vitals: (message) => this.survivalUI.setVitals(message),
        event: (message) => this.survivalUI.event(message),
        error: (message) => {
          if (message.code === "invalid_intent") this.survivalUI.toast(message.message, true);
          else console.warn("Terra server error", message.code, message.message);
        },
      }, (status) => {
        this.connected = status === "connected";
        statusElement.textContent = status === "connected" ? "Connected" : status === "connecting" ? "Connecting…" : "Disconnected";
        overlay.hidden = this.connected;
        // Keep the reconnect wording visible throughout subsequent attempts.
        if (status === "disconnected") overlayMessage.textContent = "Disconnected — reconnecting";
      }, () => {
        this.world.reset();
        this.survivalUI.reset();
        this.craftingUI.reset();
        this.heldKeys.clear();
        this.updateHud();
      });
      const interact = (pointer) => {
        if (!this.connected || document.hidden || !pointer.leftButtonDown()) return;
        const point = this.cameras.main.getWorldPoint(pointer.x, pointer.y);
        const result = pickInteractable(
          Array.from(this.world.entities.values(), (view) => view.record),
          { x: point.x / TILE_PX, y: point.y / TILE_PX }, this.world.predicted,
        );
        if (result) this.net.sendInteract(result.entityId, result.action);
      };
      this.input.on("pointerdown", interact);
      const keyDown = (event) => {
        if (event.code === "KeyC") {
          event.preventDefault();
          if (!event.repeat && !document.hidden && this.connected) this.craftingUI.toggle();
          return;
        }
        if (!MOVEMENT_KEYS.has(event.code)) return;
        event.preventDefault();
        if (!document.hidden && this.connected) this.heldKeys.add(event.code);
        this.net.sendMove(this.direction());
      };
      const keyUp = (event) => {
        if (!MOVEMENT_KEYS.has(event.code)) return;
        event.preventDefault();
        this.heldKeys.delete(event.code);
        this.net.sendMove(this.direction());
      };
      const release = () => {
        this.heldKeys.clear();
        this.net.sendMove({ x: 0, y: 0 });
      };
      const visibility = () => {
        if (document.hidden) {
          release();
        } else {
          const own = this.world.entities.get(this.world.metadata?.entityId);
          const latest = own?.samples.at(-1);
          if (latest) {
            // Resume from confirmed state even when no own-player changed record arrives.
            this.world.predicted = { x: latest.x, y: latest.y };
            this.world.correction = { x: 0, y: 0 };
          }
        }
      };
      window.addEventListener("keydown", keyDown);
      window.addEventListener("keyup", keyUp);
      window.addEventListener("blur", release);
      document.addEventListener("visibilitychange", visibility);
      this.events.once("shutdown", () => {
        window.removeEventListener("keydown", keyDown);
        window.removeEventListener("keyup", keyUp);
        window.removeEventListener("blur", release);
        document.removeEventListener("visibilitychange", visibility);
        this.input.off("pointerdown", interact);
        this.net.stop();
        this.world.reset();
        this.survivalUI.reset();
        this.craftingUI.reset();
      });
      this.net.connect();
    }

    direction() {
      const held = (...keys) => keys.some((key) => this.heldKeys.has(key));
      return {
        x: Number(held("KeyD", "ArrowRight")) - Number(held("KeyA", "ArrowLeft")),
        y: Number(held("KeyS", "ArrowDown")) - Number(held("KeyW", "ArrowUp")),
      };
    }

    updateHud() {
      const position = this.world.predicted;
      positionElement.textContent = position ? `${Math.floor(position.x)}, ${Math.floor(position.y)}` : "—, —";
      countElement.textContent = String(this.world.entities.size);
      tickElement.textContent = this.world.tick === null ? "—" : String(this.world.tick);
    }

    update(_time, delta) {
      const direction = this.connected ? this.direction() : { x: 0, y: 0 };
      this.net.sendMove(direction); // Network deduplicates; no per-frame wire traffic.
      // Avoid a huge local step after tab suspension; authoritative updates resume it.
      this.world.update(performance.now(), Math.min(delta, 100), direction);
      this.craftingUI.update(this.world);
      this.updateHud();
    }
  }

  const game = new Phaser.Game({
    type: Phaser.AUTO,
    parent: "game",
    backgroundColor: TERRAIN_PALETTE[0],
    pixelArt: true,
    roundPixels: true,
    scale: { mode: Phaser.Scale.RESIZE, width: window.innerWidth, height: window.innerHeight },
    scene: IslandScene,
  });
  window.terra = { game };
}

if (window.Phaser) {
  boot();
} else {
  statusElement.textContent = "Unable to load renderer";
  overlayMessage.textContent = "Phaser could not load. Check your connection and reload.";
}
