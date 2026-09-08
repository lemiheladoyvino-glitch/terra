from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from server.net.protocol import SCHEMA_VERSION, ProtocolError, decode, encode

CLIENT_TYPES = {"hello", "move", "interact", "craft", "chat"}


class ConnectionRegistry:
    def __init__(self) -> None:
        self.connections: dict[str, Connection] = {}

    def register(self, connection: Connection) -> None:
        self.connections[connection.id] = connection

    def unregister(self, connection: Connection) -> None:
        self.connections.pop(connection.id, None)


class Connection:
    def __init__(self, websocket: WebSocket, registry: ConnectionRegistry) -> None:
        self.id = str(uuid4())
        self.websocket = websocket
        self.registry = registry
        self.send_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)

    async def send(self, message: dict[str, Any]) -> None:
        await self.send_queue.put(encode(message))

    async def _send_loop(self) -> None:
        while True:
            await self.websocket.send_text(await self.send_queue.get())

    async def _recv_loop(self) -> None:
        while True:
            try:
                try:
                    raw = await self.websocket.receive_text()
                except KeyError as exc:
                    # Starlette's receive_text accesses a missing text key on binary frames.
                    raise ProtocolError("expected text frame") from exc
                message = decode(raw)
                if message["t"] not in CLIENT_TYPES or "sender_id" in message:
                    raise ProtocolError("message is not a client intent")
            except ProtocolError as exc:
                await self.send({
                    "t": "error", "v": SCHEMA_VERSION,
                    "code": "invalid_message", "message": str(exc)[:2048],
                })
                continue
            # Intents are validated but intentionally have no gameplay effects.

    async def run(self, tick: int, *, world_size: int, chunk_size: int, seed: int) -> None:
        await self.websocket.accept()
        self.registry.register(self)
        try:
            await self.send({
                "t": "welcome", "v": SCHEMA_VERSION, "entity_id": self.id, "tick": tick,
                "world_size": world_size, "chunk_size": chunk_size, "seed": seed,
                "config": {"movement_hz": 10, "sim_hz": 1, "aoi_radius": 20},
            })
            async with asyncio.TaskGroup() as group:
                group.create_task(self._send_loop())
                await self._recv_loop()
        except* WebSocketDisconnect:
            pass
        finally:
            self.registry.unregister(self)
