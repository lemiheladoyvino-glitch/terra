from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

import pytest
from fastapi import WebSocketDisconnect

from server.game.entities import Entity
from server.game.items import AXE_DURABILITY, ITEMS, PICKAXE_DURABILITY
from server.game.simulation import Simulation
from server.game.survival import (
    GRAVE_TTL,
    INVENTORY_SLOTS,
    PlayerState,
    add_item,
    consume_item,
    damage_tool,
    find_tool,
    serialize_inventory,
)
from server.game.world import World
from server.game.worldgen import Terrain, TerrainKind
from server.net.connection import Connection, ConnectionRegistry
from server.net.protocol import decode, encode


@pytest.fixture
def setup() -> tuple[Simulation, Connection]:
    registry = ConnectionRegistry()
    sim = Simulation(World(Terrain(bytes([TerrainKind.GRASS]) * 64 * 64, 64, 42)), registry)
    connection = Connection(None, registry)
    connection.id = "test-player"
    sim.add_player(connection)
    registry.register(connection)
    return sim, connection


def drain(connection: Connection) -> list[dict[str, Any]]:
    result = []
    while not connection.send_queue.empty():
        result.append(decode(connection.send_queue.get_nowait()))
    return result


def resource(sim: Simulation, kind: str, remaining: int = 2) -> Entity:
    x, y = sim.spawn_position
    entity = Entity("resource", kind, (x + 1, y), {"resource_remaining": remaining})
    sim.world.add_entity(entity)
    return entity


def interact(sim: Simulation, connection: Connection, action: str,
             target: str = "resource") -> list[dict[str, Any]]:
    connection.interact_intent = {"action": action, "target": {"entity_id": target}}
    sim.movement_tick()
    assert connection.interact_intent is None
    return drain(connection)


def count(state: PlayerState, item: str) -> int:
    return sum(s["quantity"] for s in state.inventory if s and s["item_id"] == item)


def test_starter_kit(setup: tuple[Simulation, Connection]) -> None:
    sim, connection = setup
    state = sim.players[connection.id]
    assert state.hp == state.hunger == 100
    assert len(state.inventory) == INVENTORY_SLOTS
    assert count(state, "wooden-axe") == count(state, "wooden-pickaxe") == 1
    assert count(state, "berries") == 0
    sim.send_inventory(connection)
    message, = drain(connection)
    assert decode(encode(message)) == message
    assert message["slots"] == [
        {"slot": 0, "item_id": "wooden-axe", "quantity": 1, "durability": 1.0},
        {"slot": 1, "item_id": "wooden-pickaxe", "quantity": 1, "durability": 1.0},
    ]
    sim.remove_player(connection)
    assert connection.id not in sim.players


@pytest.mark.parametrize("action,kind,tool,item,uses", [
    ("chop", "tree", "axe", "wood", AXE_DURABILITY),
    ("mine", "rock", "pickaxe", "stone", PICKAXE_DURABILITY),
])
def test_harvest_and_break(setup: tuple[Simulation, Connection], action: str, kind: str,
                           tool: str, item: str, uses: int) -> None:
    sim, connection = setup
    entity = resource(sim, kind, uses)
    state = sim.players[connection.id]
    slot = find_tool(state, tool)
    assert slot is not None
    sim.movement_tick()
    drain(connection)
    original = entity.to_record()
    messages = interact(sim, connection, action)
    assert count(state, item) == 1
    assert state.inventory[slot]["durability"] == uses - 1
    assert sim.world.entities[entity.id].fields["resource_remaining"] == uses - 1
    assert entity.to_record() == original  # Replacement never mutates old records.
    assert sim.world.entities[entity.id] in sim.world.query_radius(entity.position, 0)
    assert any(m["t"] == "inventory" for m in messages)
    for _ in range(uses - 1):
        messages = interact(sim, connection, action)
    assert count(state, item) == uses
    assert state.inventory[slot] is None
    assert entity.id not in sim.world.entities
    assert any(m["t"] == "event" and m["event"] == "tool_broke" for m in messages)
    assert entity.id in next(m for m in messages if m["t"] == "delta")["left"]


@pytest.mark.parametrize("failure", ["range", "tool", "full", "wrong-kind"])
def test_rejected_chop_is_atomic(setup: tuple[Simulation, Connection], failure: str) -> None:
    sim, connection = setup
    entity = resource(sim, "rock" if failure == "wrong-kind" else "tree")
    state = sim.players[connection.id]
    if failure == "range":
        sim.world.move_entity(entity.id, (0.5, 0.5))
    elif failure == "tool":
        consume_item(state, "wooden-axe")
    elif failure == "full":
        add_item(state, "stone", 99 * 14)
    before = deepcopy(state.inventory)
    messages = interact(sim, connection, "chop")
    error = next(m for m in messages if m["t"] == "error")
    assert error["code"] == "invalid_intent"
    if failure == "tool":
        assert error["message"] == "no axe"
    assert state.inventory == before
    assert sim.world.entities[entity.id].fields["resource_remaining"] == 2


def test_interaction_precedes_movement(setup: tuple[Simulation, Connection]) -> None:
    sim, connection = setup
    entity = resource(sim, "tree")
    x, y = sim.spawn_position
    sim.world.move_entity(entity.id, (x + 1.5, y))
    connection.move_intent = {"direction": {"x": -1, "y": 0}}
    interact(sim, connection, "chop")
    assert count(sim.players[connection.id], "wood") == 1
    assert sim.world.entities[connection.id].position == pytest.approx((x - 0.4, y))


def test_eat(setup: tuple[Simulation, Connection]) -> None:
    sim, connection = setup
    resource(sim, "berry-bush")
    state = sim.players[connection.id]
    state.hunger = 90
    interact(sim, connection, "eat")
    assert state.hunger == 98
    assert sim.world.entities["resource"].fields["resource_remaining"] == 1
    interact(sim, connection, "eat")
    assert state.hunger == 100
    assert "resource" not in sim.world.entities


def test_survival_decay_damage_regen_and_dispatch(setup: tuple[Simulation, Connection]) -> None:
    sim, connection = setup
    state = sim.players[connection.id]
    village = Entity("village", "building", (1.5, 1.5), {"building_type": "hall", "hp": 100})
    sim.world.add_entity(village)
    for _ in range(99):
        sim.sim_tick()
        drain(connection)  # Emulate a live client consuming its private vitals.
    assert state.hunger == 1 and state.hp == 100
    sim.sim_tick()
    assert state.hunger == 0 and state.hp == 98
    assert sim.world.entities[connection.id].fields["hp"] == 98
    sim.sim_tick()
    assert state.hp == 96
    state.hunger = 52
    sim.sim_tick()
    assert state.hp == 97 and state.hunger == 51
    sim.sim_tick()
    assert state.hp == 97 and state.hunger == 50
    assert sim.world.entities[village.id] is village
    assert sim._village_tick() is None


def die(sim: Simulation, connection: Connection) -> str:
    state = sim.players[connection.id]
    state.hp, state.hunger = 2, 0
    sim._survival_tick()
    return f"grave:{connection.id}:{sim.world.tick_count}"


def test_death_and_pickup(setup: tuple[Simulation, Connection]) -> None:
    sim, connection = setup
    state = sim.players[connection.id]
    add_item(state, "wood", 12)
    damage_tool(state, 0)
    before = deepcopy([s for s in state.inventory if s is not None])
    death_position = (40.5, 40.5)
    sim.world.move_entity(connection.id, death_position)
    sim.world.tick_count = 50
    grave_id = die(sim, connection)
    grave = sim.world.entities[grave_id]
    assert grave.position == death_position
    assert grave.fields == {"owner_id": connection.id, "expires_tick": 50 + GRAVE_TTL}
    assert sim.graves[grave_id] == before
    assert state.inventory == [None] * INVENTORY_SLOTS
    assert state.hp == state.hunger == 100
    assert sim.world.entities[connection.id].fields["hp"] == 100
    assert sim.world.entities[connection.id].position == sim.spawn_position
    messages = drain(connection)
    assert [m["t"] for m in messages] == ["event", "inventory", "vitals"]
    assert messages[0]["event"] == "you_died" and messages[0]["grave_id"] == grave_id
    assert messages[1]["slots"] == []
    sim.world.move_entity(connection.id, death_position)
    interact(sim, connection, "pickup", grave_id)
    assert [s for s in state.inventory if s is not None] == before
    assert grave_id not in sim.world.entities and grave_id not in sim.graves
    assert serialize_inventory(state)[0]["durability"] == 39 / 40


def test_partial_grave_pickup_and_expiry(setup: tuple[Simulation, Connection]) -> None:
    sim, connection = setup
    state = sim.players[connection.id]
    add_item(state, "wood", 30)
    grave_id = die(sim, connection)
    drain(connection)
    add_item(state, "stone", 99 * 15)
    add_item(state, "wood", 98)
    interact(sim, connection, "pickup", grave_id)
    assert count(state, "wood") == 99
    assert next(s for s in sim.graves[grave_id] if s["item_id"] == "wood")["quantity"] == 29
    assert grave_id in sim.world.entities
    sim.world.tick_count = GRAVE_TTL - 1
    sim._survival_tick()
    assert grave_id in sim.graves
    sim.world.tick_count += 1
    sim._survival_tick()
    assert grave_id not in sim.graves and grave_id not in sim.world.entities


def test_inventory_helpers() -> None:
    state = PlayerState()
    assert add_item(state, "wood", 100) == 0
    assert [s["quantity"] for s in state.inventory if s] == [99, 1]
    before = deepcopy(state.inventory)
    assert not consume_item(state, "wood", 101)
    assert state.inventory == before
    assert consume_item(state, "wood", 100)
    assert state.inventory == [None] * INVENTORY_SLOTS
    assert add_item(state, "wooden-axe", 17) == 1
    assert all(s["quantity"] == 1 for s in state.inventory if s)
    assert ITEMS["wooden-axe"].tool == "axe"
    assert ITEMS["wood"].max_durability is None
    assert find_tool(state, "pickaxe") is None


def test_latest_interact_is_queued() -> None:
    class Socket:
        def __init__(self) -> None:
            self.messages = iter([
                {"t": "interact", "v": 3, "action": "chop", "target": {"entity_id": "a"}},
                {"t": "interact", "v": 3, "action": "mine", "target": {"entity_id": "b"}},
            ])

        async def receive_text(self) -> str:
            message = next(self.messages, None)
            if message is None:
                raise WebSocketDisconnect()
            return encode(message)

    connection = Connection(Socket(), ConnectionRegistry())

    async def scenario() -> None:
        with pytest.raises(WebSocketDisconnect):
            await connection._recv_loop()

    asyncio.run(scenario())
    assert connection.interact_intent["action"] == "mine"
    assert connection.interact_intent["target"] == {"entity_id": "b"}


def test_zero_hp_dies_instead_of_regenerating(setup: tuple[Simulation, Connection]) -> None:
    sim, connection = setup
    state = sim.players[connection.id]
    state.hp = 0
    state.hunger = 100
    sim._survival_tick()
    assert state.hp == state.hunger == 100
    assert len(sim.graves) == 1
    assert drain(connection)[0]["event"] == "you_died"


def test_vitals_owner_only_and_send_on_change(setup: tuple[Simulation, Connection]) -> None:
    sim, connection = setup
    other = Connection(None, sim.registry)
    other.id = "other"
    sim.add_player(other)
    sim.registry.register(other)
    sim.send_vitals(connection)
    assert drain(connection) == [{"t": "vitals", "v": 3, "hp": 100, "hunger": 100}]
    assert drain(other) == []
    sim.send_vitals(connection)
    assert drain(connection) == []
    sim._survival_tick()
    assert drain(connection) == [{"t": "vitals", "v": 3, "hp": 100, "hunger": 99}]
    assert drain(other) == [{"t": "vitals", "v": 3, "hp": 100, "hunger": 99}]
    sim.send_vitals(connection)
    assert drain(connection) == []
    resource(sim, "berry-bush")
    sim.players[connection.id].hunger = 80
    messages = interact(sim, connection, "eat")
    assert next(m for m in messages if m["t"] == "vitals")["hunger"] == 88
    drain(other)
    sim.players[connection.id].hp = 0
    sim.players[connection.id].hunger = 0
    sim._survival_tick()
    assert drain(connection)[-1] == {"t": "vitals", "v": 3, "hp": 100, "hunger": 100}
    sim.remove_player(connection)
    assert connection.id not in sim.last_vitals


def test_unchanged_survival_vitals_and_failed_enqueue(setup: tuple[Simulation, Connection]) -> None:
    sim, connection = setup
    # At 0 hunger/2 HP, this tick dies and resets to the already-sent full values.
    sim.send_vitals(connection)
    drain(connection)
    sim.players[connection.id].hp = 2
    sim.players[connection.id].hunger = 0
    sim._survival_tick()
    assert all(m["t"] != "vitals" for m in drain(connection))
    sim.players[connection.id].hunger = 90
    for _ in range(connection.send_queue.maxsize):
        connection.send_queue.put_nowait("full")
    sim.send_vitals(connection)
    assert connection.closing
    assert sim.last_vitals[connection.id] == (100, 100)
