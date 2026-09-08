const ITEMS = {
  wood: { name: "Wood", color: "#946442", glyph: "▰" },
  stone: { name: "Stone", color: "#92969b", glyph: "◆" },
  berries: { name: "Berries", color: "#c4549e", glyph: "●" },
  "wooden-axe": { name: "Wooden axe", color: "#c59d66", glyph: "🪓", tool: true },
  "wooden-pickaxe": { name: "Wooden pickaxe", color: "#c59d66", glyph: "⛏", tool: true },
};

export class SurvivalUI {
  constructor() {
    this.inventory = document.querySelector("#inventory");
    this.toasts = document.querySelector("#toasts");
    this.timers = new Set();
    this.reset();
  }

  reset() {
    for (const timer of this.timers) clearTimeout(timer);
    this.timers.clear();
    this.toasts.replaceChildren();
    this.setInventory([]);
    for (const key of ["hp", "hunger"]) {
      document.querySelector(`#${key}-bar`).value = 0;
      document.querySelector(`#${key}-value`).textContent = "—";
    }
  }

  setVitals({ hp, hunger }) {
    for (const [key, value] of Object.entries({ hp, hunger })) {
      document.querySelector(`#${key}-bar`).value = value;
      document.querySelector(`#${key}-value`).textContent = `${value}/100`;
    }
  }

  setInventory(slots) {
    const occupied = new Map(slots.map((slot) => [slot.slot, slot]));
    const children = [];
    for (let index = 0; index < 16; index++) {
      const cell = document.createElement("div");
      cell.className = "inventory-slot";
      cell.setAttribute("role", "listitem");
      const stack = occupied.get(index);
      const label = document.createElement("span");
      label.className = "slot-index";
      label.textContent = String(index + 1);
      cell.append(label);
      if (stack) {
        const item = ITEMS[stack.item_id] ?? { name: stack.item_id, color: "#aaaaaa", glyph: "?" };
        const description = `${item.name} ×${stack.quantity}${item.tool ? `, ${Math.round(stack.durability * 100)}% durability` : ""}`;
        cell.title = description;
        cell.setAttribute("aria-label", `Slot ${index + 1}: ${description}`);
        const icon = document.createElement("span");
        icon.className = "item-icon";
        icon.style.backgroundColor = item.color;
        icon.textContent = item.glyph;
        icon.setAttribute("aria-hidden", "true");
        const quantity = document.createElement("span");
        quantity.className = "item-quantity";
        quantity.textContent = String(stack.quantity);
        cell.append(icon, quantity);
        if (item.tool) {
          const bar = document.createElement("progress");
          bar.className = "durability";
          bar.max = 1;
          bar.value = stack.durability;
          bar.setAttribute("aria-label", `${item.name} durability`);
          cell.append(bar);
        }
      } else {
        cell.setAttribute("aria-label", `Slot ${index + 1}: empty`);
      }
      children.push(cell);
    }
    this.inventory.replaceChildren(...children);
  }

  toast(message, dim = false) {
    const node = document.createElement("div");
    node.className = dim ? "toast dim" : "toast";
    node.textContent = message;
    this.toasts.append(node);
    const timer = setTimeout(() => {
      node.remove();
      this.timers.delete(timer);
    }, 3500);
    this.timers.add(timer);
  }

  event(message) {
    if (message.event === "tool_broke") {
      this.toast(`Your ${(ITEMS[message.item_id]?.name ?? message.item_id).toLowerCase()} broke`);
    } else if (message.event === "you_died") {
      this.toast("You died — grave left behind");
    } else if (message.event === "trade_result") {
      this.toast(message.reason);
    }
  }
}
