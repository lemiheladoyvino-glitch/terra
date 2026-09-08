from __future__ import annotations

import asyncio
import logging
import math
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from server.game.entities import MOVE_SPEED, Entity, Position
from server.game.movement import traverse
from server.game.persistence import load_player, save_player
from server.game.players import PlayerStore, StoredPlayer
from server.game.recipes import RECIPES, WORKBENCH_RANGE, can_craft
from server.game.survival import (
    BERRY_HUNGER,
    GRAVE_TTL,
    HUNGER_DECAY,
    INTERACT_RANGE,
    STARVE_DAMAGE,
    ItemStack,
    PlayerState,
    add_item,
    consume_item,
    damage_tool,
    find_tool,
    serialize_inventory,
)
from server.game.village import tick_villages
from server.game.world import World
from server.game.worldgen import CHUNK_SIZE, TerrainKind
from server.net.protocol import SCHEMA_VERSION, ProtocolError, encode, encode_chunk_rle

if TYPE_CHECKING:
    from server.net.connection import Connection, ConnectionRegistry

MOVEMENT_HZ = 10
MAX_STEP = MOVE_SPEED / MOVEMENT_HZ


class Simulation:
    """Synchronous authoritative work on the connections' asyncio event loop."""

    def __init__(
        self, world: World, registry: ConnectionRegistry, db_path: str | Path | None = None
    ) -> None:
        self.world = world
        self.registry = registry
        # Crash snapshots may contain connected players; only token login makes
        # them live again. Their durable state comes from player rows.
        for entity in tuple(world.entities.values()):
            if entity.kind == "player":
                world.remove_entity(entity.id)
        self.spawn_position = self._find_spawn()
        self.players: dict[str, PlayerState] = {}
        self.graves = world.grave_contents
        self.db_path = db_path
        self.player_store = PlayerStore()
        self.player_tokens = self.player_store.players
        self._owners: dict[str, Connection] = {}
        self._persisted: dict[str, StoredPlayer] = {}
        self._save_tasks: set[asyncio.Task[None]] = set()
        self._player_save_lock = asyncio.Lock()
        self._save_ticks = 0
        self.last_vitals: dict[str, tuple[int, int]] = {}

    def _find_spawn(self) -> Position:
        terrain = self.world.terrain
        center = terrain.size // 2
        # Chebyshev rings, each in row-major order, including the center first.
        for radius in range(terrain.size):
            for y in range(center - radius, center + radius + 1):
                for x in range(center - radius, center + radius + 1):
                    if max(abs(x - center), abs(y - center)) != radius:
                        continue
                    if terrain.in_bounds(x, y) and terrain.at(x, y) in {
                        TerrainKind.GRASS,
                        TerrainKind.SAND,
                    }:
                        return x + 0.5, y + 0.5
        raise ValueError("world has no grass or sand spawn tile")

    def add_player(self, connection: Connection) -> None:
        token = connection.hello_token
        stored = self.player_tokens.get(token) if token else None
        if stored is None and token and self.db_path is not None:
            stored = load_player(self.db_path, token)
            if stored is not None:
                self._persisted[token] = deepcopy(stored)
        if stored is not None:
            old = self._owners.get(stored["player_id"])
            if old is not None:
                self._capture_player(old)
                stored = self.player_tokens[stored["token"]]
                old.close_soon(1000, "session replaced")
            connection.id = stored["player_id"]
            connection.token = stored["token"]
            state = PlayerState(deepcopy(stored["inventory"]), stored["hp"], stored["hunger"])
            position = tuple(stored["position"])
            if not self.world.terrain.walkable(math.floor(position[0]), math.floor(position[1])):
                position = self.spawn_position
            name = stored["name"]
        else:
            # Connection.id is already a freshly minted UUID, distinct from the new token.
            connection.token = str(uuid4())
            state = PlayerState()
            add_item(state, "wooden-axe", 1)
            add_item(state, "wooden-pickaxe", 1)
            position = self.spawn_position
            name = f"Wanderer-{connection.id[:4]}"
        if connection.id in self.world.entities:
            self.world.remove_entity(connection.id)
        self.world.add_entity(
            Entity(connection.id, "player", position, {"name": name, "hp": state.hp})
        )
        self.players[connection.id] = state
        self._owners[connection.id] = connection
        self.last_vitals.pop(connection.id, None)
        self._capture_player(connection)

    def _capture_player(self, connection: Connection) -> StoredPlayer:
        state = self.players[connection.id]
        entity = self.world.entities[connection.id]
        stored: StoredPlayer = {
            "player_id": connection.id,
            "token": connection.token,
            "name": entity.fields["name"],
            "hp": state.hp,
            "hunger": state.hunger,
            "inventory": deepcopy(state.inventory),
            "position": list(entity.position),
        }
        self.player_tokens[connection.token] = stored
        return stored

    def _schedule_player_save(self, stored: StoredPlayer) -> None:
        if self.db_path is None:
            return

        async def write() -> None:
            async with self._player_save_lock:
                # Read the newest detached state after taking the lock, so stale
                # disconnect/autosave requests never overwrite a takeover's state.
                current = deepcopy(self.player_tokens[stored["token"]])
                if self._persisted.get(current["token"]) == current:
                    return
                try:
                    await asyncio.to_thread(save_player, self.db_path, current)
                    self._persisted[current["token"]] = current
                except Exception:
                    logging.getLogger(__name__).error(
                        "Player save failed; will retry", exc_info=False
                    )

        task = asyncio.create_task(write(), name="terra-player-save")
        self._save_tasks.add(task)
        task.add_done_callback(self._save_tasks.discard)

    async def flush_player_saves(self) -> None:
        for connection in tuple(self._owners.values()):
            self._capture_player(connection)
        for stored in tuple(self.player_tokens.values()):
            self._schedule_player_save(stored)
        if self._save_tasks:
            await asyncio.shield(asyncio.gather(*tuple(self._save_tasks)))

    def send_inventory(self, connection: Connection) -> None:
        """Send the initial inventory after welcome, or a changed inventory thereafter."""
        self._enqueue(
            connection,
            {
                "t": "inventory",
                "v": SCHEMA_VERSION,
                "tick": self.world.tick_count,
                "slots": serialize_inventory(self.players[connection.id]),
            },
        )

    def send_vitals(self, connection: Connection) -> None:
        """Private owner state; cache only a successfully enqueued update."""
        state = self.players[connection.id]
        vitals = (state.hp, state.hunger)
        if self.last_vitals.get(connection.id) == vitals:
            return
        if self._enqueue(
            connection, {"t": "vitals", "v": SCHEMA_VERSION, "hp": state.hp, "hunger": state.hunger}
        ):
            self.last_vitals[connection.id] = vitals

    def remove_player(self, connection: Connection) -> None:
        if self._owners.get(connection.id) is connection:
            stored = self._capture_player(connection)
            self._schedule_player_save(stored)
            self._owners.pop(connection.id)
            self.world.remove_entity(connection.id)
            self.players.pop(connection.id, None)
            self.last_vitals.pop(connection.id, None)
        connection.interact_intent = None
        connection.craft_intent = None
        connection.move_intent = None
        connection.known_records.clear()
        connection.sent_chunks.clear()
        connection.bootstrapped = False

    def movement_tick(self) -> None:
        connections = tuple(self.registry.connections.values())
        for connection in connections:
            if not connection.closing and connection.id in self.world.entities:
                intent = connection.interact_intent
                connection.interact_intent = None
                if intent is not None:
                    self._interact(connection, intent)
                craft = connection.craft_intent
                connection.craft_intent = None
                if craft is not None and not connection.closing:
                    self._craft(connection, craft)
                if not connection.closing:
                    self._move(connection)
        self.world.tick_count += 1
        for connection in connections:
            if not connection.closing and connection.id in self.world.entities:
                self._stream(connection)

    def sim_tick(self) -> None:
        self._survival_tick()
        self._village_tick()

    def _village_tick(self) -> None:
        tick_villages(self.world)

    def _invalid_interact(self, connection: Connection, message: str) -> None:
        self._enqueue(
            connection,
            {"t": "error", "v": SCHEMA_VERSION, "code": "invalid_intent", "message": message},
        )

    def _interact(self, connection: Connection, intent: dict[str, Any]) -> None:
        action = intent["action"]
        if action in {"trade", "craft"}:
            return
        target = self.world.entities.get(intent["target"].get("entity_id"))
        player = self.world.entities[connection.id]
        if target is None or math.dist(player.position, target.position) > INTERACT_RANGE:
            self._invalid_interact(connection, "target missing or out of range")
            return
        state = self.players[connection.id]
        if action in {"chop", "mine"}:
            kind, tool, item = (
                ("tree", "axe", "wood") if action == "chop" else ("rock", "pickaxe", "stone")
            )
            if target.kind != kind or target.fields["resource_remaining"] <= 0:
                self._invalid_interact(connection, "invalid resource target")
                return
            slot = find_tool(state, tool)
            if slot is None:
                self._invalid_interact(connection, f"no {tool}")
                return
            # Reject a full inventory before consuming a resource or tool use.
            if add_item(state, item, 1):
                self._invalid_interact(connection, "inventory full")
                return
            stack = state.inventory[slot]
            assert stack is not None
            tool_id = stack["item_id"]
            self._deplete(target)
            if damage_tool(state, slot):
                self._enqueue(
                    connection,
                    {
                        "t": "event",
                        "v": SCHEMA_VERSION,
                        "tick": self.world.tick_count,
                        "event": "tool_broke",
                        "item_id": tool_id,
                    },
                )
            self.send_inventory(connection)
        elif action == "eat" and target.kind == "berry-bush":
            if target.fields["resource_remaining"] <= 0:
                self._invalid_interact(connection, "empty berry bush")
                return
            self._deplete(target)
            state.hunger = min(100, state.hunger + BERRY_HUNGER)
            self.send_vitals(connection)
        elif action == "pickup" and target.kind == "grave" and target.id in self.graves:
            leftovers: list[ItemStack] = []
            for stack in self.graves[target.id]:
                qty = add_item(
                    state, stack["item_id"], stack["quantity"], durability=stack["durability"]
                )
                if qty:
                    leftovers.append({**stack, "quantity": qty})
            if leftovers:
                self.graves[target.id] = leftovers
            else:
                self.graves.pop(target.id)
                self.world.remove_entity(target.id)
            self.send_inventory(connection)
        else:
            self._invalid_interact(connection, "invalid interaction target")

    def _tile_occupied(self, position: Position) -> bool:
        tile = tuple(map(math.floor, position))
        center = (tile[0] + 0.5, tile[1] + 0.5)
        return any(
            entity.kind in {"building", "road", "workbench"}
            and tuple(map(math.floor, entity.position)) == tile
            for entity in self.world.query_radius(center, 1)
        )

    def _craft(self, connection: Connection, intent: dict[str, Any]) -> None:
        recipe = RECIPES.get(intent["recipe_id"])
        if recipe is None:
            self._invalid_interact(connection, "unknown recipe")
            return
        position = self.world.entities[connection.id].position
        near_workbench = any(
            entity.kind == "building" and entity.fields.get("building_type") == "workbench"
            for entity in self.world.query_radius(position, WORKBENCH_RANGE)
        )
        state = self.players[connection.id]
        reason = can_craft(state, recipe, near_workbench)
        if reason:
            self._invalid_interact(connection, reason)
            return
        if recipe.places_entity:
            if self._tile_occupied(position):
                self._invalid_interact(connection, "tile occupied")
                return
        else:
            # Fit is checked against inventory BEFORE consuming inputs, as specified.
            trial = deepcopy(state)
            if add_item(trial, recipe.output_id, recipe.output_qty):
                self._invalid_interact(connection, "inventory full")
                return
        for item_id, qty in recipe.inputs.items():
            consume_item(state, item_id, qty)
        if recipe.places_entity:
            center = (math.floor(position[0]) + 0.5, math.floor(position[1]) + 0.5)
            self.world.add_entity(
                Entity(
                    f"workbench:{connection.id}:{self.world.tick_count}",
                    "building",
                    center,
                    {"building_type": recipe.places_entity, "hp": 100},
                )
            )
        else:
            add_item(state, recipe.output_id, recipe.output_qty)
        self.send_inventory(connection)

    def _deplete(self, entity: Entity) -> None:
        remaining = entity.fields["resource_remaining"] - 1
        if remaining <= 0:
            self.world.remove_entity(entity.id)
        else:
            self.world.update_entity_fields(entity.id, resource_remaining=remaining)

    def _survival_tick(self) -> None:
        for connection in tuple(self.registry.connections.values()):
            state = self.players.get(connection.id)
            if state is None or connection.closing:
                continue
            state.hunger = max(0, state.hunger - HUNGER_DECAY)
            if state.hunger == 0:
                state.hp = max(0, state.hp - STARVE_DAMAGE)
            elif state.hunger > 50 and 0 < state.hp < 100:
                state.hp = min(100, state.hp + 1)
            if state.hp == 0:
                self._die(connection, state)
            if self.world.entities[connection.id].fields["hp"] != state.hp:
                self.world.update_entity_fields(connection.id, hp=state.hp)
            self.send_vitals(connection)
        self._save_ticks += 1
        if self._save_ticks >= 10:
            self._save_ticks = 0
            for connection in tuple(self._owners.values()):
                self._capture_player(connection)
            for stored in tuple(self.player_tokens.values()):
                if self._persisted.get(stored["token"]) != stored:
                    self._schedule_player_save(stored)
        for grave_id in list(self.graves):
            grave = self.world.entities.get(grave_id)
            if grave is None or grave.fields["expires_tick"] <= self.world.tick_count:
                self.graves.pop(grave_id)
                if grave is not None:
                    self.world.remove_entity(grave_id)

    def _die(self, connection: Connection, state: PlayerState) -> None:
        player = self.world.entities[connection.id]
        grave_id = f"grave:{connection.id}:{self.world.tick_count}"
        self.world.add_entity(
            Entity(
                grave_id,
                "grave",
                player.position,
                {"owner_id": connection.id, "expires_tick": self.world.tick_count + GRAVE_TTL},
            )
        )
        self.graves[grave_id] = [stack for stack in state.inventory if stack is not None]
        state.inventory = [None] * len(state.inventory)
        state.hp = state.hunger = 100
        connection.move_intent = None
        connection.interact_intent = None
        connection.craft_intent = None
        self.world.move_entity(connection.id, self.spawn_position)
        self._enqueue(
            connection,
            {
                "t": "event",
                "v": SCHEMA_VERSION,
                "tick": self.world.tick_count,
                "event": "you_died",
                "grave_id": grave_id,
            },
        )
        self.send_inventory(connection)

    def _move(self, connection: Connection) -> None:
        intent = connection.move_intent
        if intent is None:
            return
        start = self.world.entities[connection.id].position
        target = intent.get("target")
        if target is not None:
            dx, dy = target["x"] - start[0], target["y"] - start[1]
        else:
            dx, dy = intent["direction"]["x"], intent["direction"]["y"]
        distance = math.hypot(dx, dy)
        if distance == 0:
            connection.move_intent = None
            return
        # Scaling first avoids overflowing hypot on otherwise finite wire coordinates.
        scale = max(abs(dx), abs(dy))
        ux, uy = dx / scale, dy / scale
        magnitude = math.hypot(ux, uy)
        step = min(MAX_STEP, distance) if target is not None else MAX_STEP
        end = (start[0] + ux / magnitude * step, start[1] + uy / magnitude * step)
        if target is not None and distance <= MAX_STEP:
            end = (target["x"], target["y"])
        position = self._traverse(start, end)
        if position != start:
            self.world.move_entity(connection.id, position)
        if target is not None and position == (target["x"], target["y"]):
            connection.move_intent = None

    def _traverse(self, start: Position, end: Position) -> Position:
        return traverse(self.world.terrain, start, end)

    def _enqueue(self, connection: Connection, message: dict[str, Any]) -> bool:
        if connection.closing:
            return False
        try:
            raw = encode(message)
            connection.send_queue.put_nowait(raw)
        except asyncio.QueueFull:
            connection.close_soon(1011)
            return False
        except ProtocolError as exc:
            if str(exc) != "message too large":
                raise
            connection.close_soon(1009)
            return False
        return True

    def _stream(self, connection: Connection) -> None:
        player = self.world.entities[connection.id]
        terrain = self.world.terrain
        x, y = player.position
        radius = connection.aoi_radius
        chunk_count = (terrain.size + CHUNK_SIZE - 1) // CHUNK_SIZE
        for cy in range(chunk_count):
            for cx in range(chunk_count):
                if (cx, cy) in connection.sent_chunks:
                    continue
                closest_x = min(max(x, cx * CHUNK_SIZE), min((cx + 1) * CHUNK_SIZE, terrain.size))
                closest_y = min(max(y, cy * CHUNK_SIZE), min((cy + 1) * CHUNK_SIZE, terrain.size))
                if (x - closest_x) ** 2 + (y - closest_y) ** 2 > radius**2:
                    continue
                if not self._enqueue(
                    connection,
                    {
                        "t": "chunk",
                        "v": SCHEMA_VERSION,
                        "cx": cx,
                        "cy": cy,
                        "size": CHUNK_SIZE,
                        "tiles": encode_chunk_rle(terrain.chunk_tiles(cx, cy)),
                    },
                ):
                    return
                connection.sent_chunks.add((cx, cy))
        records = {
            entity.id: entity.to_record()
            for entity in self.world.query_radius(player.position, radius)
        }
        if not connection.bootstrapped:
            message = {
                "t": "snapshot",
                "v": SCHEMA_VERSION,
                "tick": self.world.tick_count,
                "entities": list(records.values()),
            }
        else:
            known = connection.known_records
            message = {
                "t": "delta",
                "v": SCHEMA_VERSION,
                "tick": self.world.tick_count,
                "entered": [record for id, record in records.items() if id not in known],
                "left": sorted(known.keys() - records.keys()),
                "changed": [
                    record for id, record in records.items() if id in known and record != known[id]
                ],
            }
        if self._enqueue(connection, message):
            connection.known_records = records
            connection.bootstrapped = True
