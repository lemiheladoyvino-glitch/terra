from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from server.game.loop import GameLoop
from server.game.persistence import load_world, serialize_world, write_snapshot
from server.game.simulation import Simulation
from server.game.world import World
from server.game.worldgen import CHUNK_SIZE
from server.net.connection import Connection, ConnectionRegistry

SEED = 42
SAVE_INTERVAL = 30
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db_path = Path(os.environ.get("TERRA_DB", "./terra.db")).resolve()
    snapshot = load_world(db_path)
    if snapshot is not None:
        app.state.world = World.from_snapshot(snapshot)
        logger.info("resumed world at tick %s with %s entities, %s villages",
                    app.state.world.tick_count, len(app.state.world.entities),
                    len(app.state.world.villages))
    else:
        app.state.world = World.new(SEED)
        logger.info("new world seed %s", SEED)
    save_lock = asyncio.Lock()
    stop_saving = asyncio.Event()

    async def save_current() -> None:
        async with save_lock:
            blob = serialize_world(app.state.world)
            write = asyncio.create_task(asyncio.to_thread(write_snapshot, db_path, blob))
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                # Cancellation cannot stop a worker thread: finish before releasing the lock.
                await write
                raise

    async def autosave() -> None:
        while not stop_saving.is_set():
            try:
                await asyncio.wait_for(stop_saving.wait(), timeout=SAVE_INTERVAL)
            except TimeoutError:
                try:
                    await save_current()
                except Exception:
                    logger.exception("World autosave failed; retaining the previous snapshot")

    app.state.save_world = save_current
    app.state.registry = ConnectionRegistry()
    app.state.simulation = Simulation(app.state.world, app.state.registry)
    app.state.loop = GameLoop(
        on_movement_tick=app.state.simulation.movement_tick,
        on_sim_tick=app.state.simulation.sim_tick,
    )
    app.state.loop.start()
    save_task = asyncio.create_task(autosave(), name="terra-autosave")
    try:
        yield
    finally:
        try:
            await app.state.loop.stop()
        finally:
            stop_saving.set()
            await save_task  # Wait for an in-flight write; never overlap the final save.
            await save_current()


app = FastAPI(title="Terra", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    world = app.state.world
    connection = Connection(websocket, app.state.registry)
    await websocket.accept()

    def on_welcome() -> None:
        app.state.simulation.send_inventory(connection)
        app.state.simulation.send_vitals(connection)

    try:
        app.state.simulation.add_player(connection)
        await connection.run(
            world.tick_count,
            world_size=world.terrain.size,
            chunk_size=CHUNK_SIZE,
            seed=world.terrain.seed,
            on_welcome=on_welcome,
        )
    finally:
        app.state.simulation.remove_player(connection)



app.mount("/", StaticFiles(directory=Path(__file__).resolve().parents[1] / "client", html=True))
