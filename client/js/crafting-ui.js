import { RECIPES, WORKBENCH_RANGE, affordableRecipes } from "./crafting.js";
const name = (id) => id.replaceAll("-", " ");

export class CraftingUI {
  constructor(sendCraft) {
    this.panel = document.querySelector("#crafting-panel");
    this.rows = document.querySelector("#crafting-rows");
    this.sendCraft = sendCraft;
    this.reset();
  }

  reset() {
    this.slots = [];
    this.nearWorkbench = false;
    this.panel.hidden = true;
    this.render();
  }

  toggle() { this.panel.hidden = !this.panel.hidden; }

  setInventory(slots) {
    this.slots = slots;
    this.render();
  }

  update(world) {
    const player = world.predicted;
    const near = Boolean(player && Array.from(world.entities.values()).some(({ record }) =>
      record.kind === "building" && record.building_type === "workbench" &&
      Math.hypot(record.position.x - player.x, record.position.y - player.y) <= WORKBENCH_RANGE));
    if (near !== this.nearWorkbench) {
      this.nearWorkbench = near;
      this.render();
    }
  }

  render() {
    this.rows.replaceChildren(...affordableRecipes(RECIPES, this.slots, this.nearWorkbench).map((recipe) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "recipe";
      row.disabled = !recipe.canCraft;
      const title = document.createElement("strong");
      title.textContent = `${name(recipe.output.id)} ×${recipe.output.quantity}`;
      const costs = document.createElement("span");
      costs.textContent = Object.entries(recipe.inputs).map(([item, qty]) => `${qty} ${name(item)}`).join(" · ");
      const reason = document.createElement("small");
      reason.textContent = recipe.reason ?? "Craft";
      row.append(title, costs, reason);
      row.addEventListener("click", () => this.sendCraft(recipe.id));
      return row;
    }));
  }
}
