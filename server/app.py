from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from server.game.loop import GameLoop
from server.game.world import World
from server.game.worldgen import CHUNK_SIZE
from server.net.connection import Connection, ConnectionRegistry

SEED = 42


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.world = World.new(SEED)
    app.state.registry = ConnectionRegistry()
    app.state.loop = GameLoop()
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
    await Connection(websocket, app.state.registry).run(
        world.tick_count,
        world_size=world.terrain.size,
        chunk_size=CHUNK_SIZE,
        seed=world.terrain.seed,
    )


app.mount("/", StaticFiles(directory=Path(__file__).resolve().parents[1] / "client", html=True))
