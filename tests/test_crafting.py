from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

from server.game.entities import Entity
from server.game.items import ITEMS
from server.game.recipes import RECIPES, WORKBENCH_RANGE, can_craft
from server.game.simulation import Simulation
from server.game.survival import PlayerState, add_item
from server.game.world import World
from server.game.worldgen import Terrain, TerrainKind
from server.net.connection import Connection, ConnectionRegistry
from server.net.protocol import decode, encode


@pytest.fixture
def setup() -> tuple[Simulation, Connection, PlayerState]:
    registry = ConnectionRegistry()
    world = World(Terrain(bytes([TerrainKind.GRASS]) * 64 * 64, 64, 42))
    sim = Simulation(world, registry)
    connection = Connection(None, registry)
    connection.id = "crafter"
    sim.add_player(connection)
    registry.register(connection)
    return sim, connection, sim.players[connection.id]


def drain(connection: Connection) -> list[dict[str, Any]]:
    messages = []
    while not connection.send_queue.empty():
        messages.append(decode(connection.send_queue.get_nowait()))
    return messages


def craft(sim: Simulation, connection: Connection, recipe: str) -> list[dict[str, Any]]:
    connection.craft_intent = {"recipe_id": recipe}
    sim.movement_tick()
    assert connection.craft_intent is None
    return drain(connection)


def bench(sim: Simulation, distance: float = 1.0) -> None:
    x, y = sim.spawn_position
    sim.world.add_entity(
        Entity("bench", "building", (x + distance, y), {"building_type": "workbench", "hp": 100})
    )


def quantity(state: PlayerState, item: str) -> int:
    return sum(s["quantity"] for s in state.inventory if s and s["item_id"] == item)


@pytest.mark.parametrize("id", RECIPES)
def test_recipe_happy_path(setup: tuple[Simulation, Connection, PlayerState], id: str) -> None:
    sim, connection, state = setup
    recipe = RECIPES[id]
    if recipe.needs_workbench:
        bench(sim)
    for item, qty in recipe.inputs.items():
        add_item(state, item, qty)
    before_output = quantity(state, recipe.output_id)
    messages = craft(sim, connection, id)
    assert not any(m["t"] == "error" for m in messages)
    assert all(quantity(state, item) == 0 for item in recipe.inputs)
    assert any(m["t"] == "inventory" for m in messages)
    if recipe.places_entity:
        placed = sim.world.entities[f"workbench:{connection.id}:0"]
        assert placed.position == sim.spawn_position
        assert placed.fields == {"building_type": "workbench", "hp": 100}
        assert placed.to_record() in next(m for m in messages if m["t"] == "snapshot")["entities"]
    else:
        assert quantity(state, recipe.output_id) == before_output + recipe.output_qty
        output = next(
            s for s in reversed(state.inventory) if s and s["item_id"] == recipe.output_id
        )
        assert output["durability"] == ITEMS[recipe.output_id].max_durability
    assert ITEMS["padded-armor"].equip == "armor"


@pytest.mark.parametrize(
    "distance,allowed", [(None, False), (WORKBENCH_RANGE, True), (WORKBENCH_RANGE + 0.001, False)]
)
def test_workbench_range(
    setup: tuple[Simulation, Connection, PlayerState], distance: float | None, allowed: bool
) -> None:
    sim, connection, state = setup
    if distance is not None:
        bench(sim, distance)
    add_item(state, "wood", 3)
    add_item(state, "stone", 3)
    before = deepcopy(state.inventory)
    messages = craft(sim, connection, "stone-axe")
    if allowed:
        assert quantity(state, "stone-axe") == 1
    else:
        assert state.inventory == before
        assert next(m for m in messages if m["t"] == "error")["message"] == "need a workbench"


@pytest.mark.parametrize("failure", ["unknown", "missing", "full"])
def test_rejection_is_atomic(
    setup: tuple[Simulation, Connection, PlayerState], failure: str
) -> None:
    sim, connection, state = setup
    recipe = "wooden-axe"
    if failure == "unknown":
        recipe = "no-such-recipe"
    elif failure == "full":
        add_item(state, "wood", 5)
        add_item(state, "stone", 99 * 13)
        assert all(state.inventory)
    before = deepcopy(state.inventory)
    entities = dict(sim.world.entities)
    messages = craft(sim, connection, recipe)
    error = next(m for m in messages if m["t"] == "error")
    assert error["code"] == "invalid_intent"
    expected = {
        "unknown": "unknown recipe",
        "missing": "missing materials:",
        "full": "inventory full",
    }
    assert error["message"].startswith(expected[failure])
    assert state.inventory == before
    assert sim.world.entities == entities


@pytest.mark.parametrize("kind", ["building", "road"])
def test_occupied_tile_rejects_placement(
    setup: tuple[Simulation, Connection, PlayerState], kind: str
) -> None:
    sim, connection, state = setup
    add_item(state, "wood", 10)
    x, y = sim.spawn_position
    # Opposite corners of the same tile: occupancy queries must use the tile center.
    sim.world.move_entity(connection.id, (x - 0.49, y - 0.49))
    fields = {"building_type": "house", "hp": 100} if kind == "building" else {"road_type": "dirt"}
    sim.world.add_entity(Entity("occupied", kind, (x + 0.49, y + 0.49), fields))
    before = deepcopy(state.inventory)
    messages = craft(sim, connection, "workbench")
    assert next(m for m in messages if m["t"] == "error")["message"] == "tile occupied"
    assert state.inventory == before


def test_placed_bench_enables_stone_axe(setup: tuple[Simulation, Connection, PlayerState]) -> None:
    sim, connection, state = setup
    add_item(state, "wood", 13)
    add_item(state, "stone", 3)
    craft(sim, connection, "workbench")
    craft(sim, connection, "stone-axe")
    assert quantity(state, "stone-axe") == 1
    assert quantity(state, "wood") == quantity(state, "stone") == 0


def test_interact_craft_move_order(setup: tuple[Simulation, Connection, PlayerState]) -> None:
    sim, connection, state = setup
    add_item(state, "wood", 9)
    x, y = sim.spawn_position
    sim.world.add_entity(Entity("tree", "tree", (x + 1, y), {"resource_remaining": 5}))
    connection.interact_intent = {"action": "chop", "target": {"entity_id": "tree"}}
    connection.move_intent = {"direction": {"x": 1, "y": 0}}
    craft(sim, connection, "workbench")
    assert sim.world.entities["workbench:crafter:0"].position == (x, y)
    assert sim.world.entities[connection.id].position == pytest.approx((x + 0.4, y))
    assert quantity(state, "wood") == 0


def test_can_craft_aggregates_and_does_not_mutate() -> None:
    state = PlayerState()
    add_item(state, "wood", 102)
    before = deepcopy(state)
    assert can_craft(state, RECIPES["workbench"], False) is None
    assert can_craft(state, RECIPES["stone-axe"], False) == "need a workbench"
    assert can_craft(state, RECIPES["stone-axe"], True) == "missing materials: stone (3)"
    assert state == before


def test_latest_craft_is_queued_and_removed(
    setup: tuple[Simulation, Connection, PlayerState],
) -> None:
    sim, connection, _ = setup

    class Socket:
        def __init__(self) -> None:
            self.recipes = iter(["wooden-axe", "workbench"])

        async def receive_text(self) -> str:
            recipe = next(self.recipes, None)
            if recipe is None:
                raise WebSocketDisconnect()
            return encode({"t": "craft", "v": 4, "recipe_id": recipe})

    connection.websocket = Socket()

    async def scenario() -> None:
        with pytest.raises(WebSocketDisconnect):
            await connection._recv_loop()

    asyncio.run(scenario())
    assert connection.craft_intent["recipe_id"] == "workbench"
    sim.remove_player(connection)
    assert connection.craft_intent is None
