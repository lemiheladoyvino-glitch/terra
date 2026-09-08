from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from server.game.loop import GameLoop
from server.game.simulation import Simulation
from server.game.world import World
from server.game.worldgen import CHUNK_SIZE
from server.net.connection import Connection, ConnectionRegistry

SEED = 42


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.world = World.new(SEED)
    app.state.registry = ConnectionRegistry()
    app.state.simulation = Simulation(app.state.world, app.state.registry)
    app.state.loop = GameLoop(
        on_movement_tick=app.state.simulation.movement_tick,
        on_sim_tick=app.state.simulation.sim_tick,
    )
    app.state.loop.start()
    try:
        yield
    finally:
        await app.state.loop.stop()


app = FastAPI(title="Terra", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    world = app.state.world
    connection = Connection(websocket, app.state.registry)
    await websocket.accept()
    try:
        app.state.simulation.add_player(connection)
        await connection.run(
            world.tick_count,
            world_size=world.terrain.size,
            chunk_size=CHUNK_SIZE,
            seed=world.terrain.seed,
            on_welcome=lambda: app.state.simulation.send_inventory(connection),
        )
    finally:
        app.state.simulation.remove_player(connection)



app.mount("/", StaticFiles(directory=Path(__file__).resolve().parents[1] / "client", html=True))
