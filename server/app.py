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
SHUTDOWN_SAVE_TIMEOUT = 10
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configured_path = os.environ.get("TERRA_DB", "./terra.db")
    db_path = Path(configured_path).resolve()
    is_default = db_path == Path("./terra.db").resolve()
    logger.info("TERRA_DB=%s (default=%s)", db_path, is_default)
    if is_default:
        logger.warning("Default TERRA_DB is non-durable on most hosts; "
                       "set TERRA_DB to a persistent path")
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
    app.state.simulation = Simulation(app.state.world, app.state.registry, db_path)
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
            async def finish_saving() -> None:
                await save_task
                await app.state.simulation.flush_player_saves()
                await save_current()

            # Keep transaction ordering: cancelling a thread await cannot stop disk I/O.
            final_save = asyncio.create_task(finish_saving(), name="terra-final-save")
            app.state.shutdown_save_task = final_save
            try:
                await asyncio.wait_for(asyncio.shield(final_save), SHUTDOWN_SAVE_TIMEOUT)
            except TimeoutError:
                logger.error(
                    "Shutdown save did not finish within %s seconds", SHUTDOWN_SAVE_TIMEOUT,
                )

                def report_completion(task: asyncio.Task[None]) -> None:
                    if not task.cancelled() and task.exception() is not None:
                        logger.error("Deferred shutdown save failed", exc_info=task.exception())

                final_save.add_done_callback(report_completion)
            except Exception:
                logger.exception("Shutdown save failed; retaining previous durable snapshots")


app = FastAPI(title="Terra", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str | int]:
    world = app.state.world
    return {"status": "ok", "tick": world.tick_count,
            "entities": len(world.entities), "villages": len(world.villages)}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    world = app.state.world
    connection = Connection(websocket, app.state.registry)
    await websocket.accept()

    def on_welcome() -> None:
        app.state.simulation.send_inventory(connection)
        app.state.simulation.send_vitals(connection)

    def on_connect() -> int:
        app.state.simulation.add_player(connection)
        return world.tick_count

    try:
        await connection.run(
            world.tick_count,
            world_size=world.terrain.size,
            chunk_size=CHUNK_SIZE,
            seed=world.terrain.seed,
            on_welcome=on_welcome,
            on_connect=on_connect,
        )
    finally:
        app.state.simulation.remove_player(connection)
        await app.state.simulation.flush_player_saves()



app.mount("/", StaticFiles(directory=Path(__file__).resolve().parents[1] / "client", html=True))
