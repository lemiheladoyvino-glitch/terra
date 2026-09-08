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
        self.move_intent: dict[str, Any] | None = None
        self.aoi_radius = 20
        self.known_records: dict[str, dict[str, Any]] = {}
        self.sent_chunks: set[tuple[int, int]] = set()
        self.bootstrapped = False
        self.closing = False
        self._close_code = 1000
        self._close_requested = asyncio.Event()

    def close_soon(self, code: int) -> None:
        """Wake the TaskGroup-owned close watcher without blocking the tick."""
        if not self.closing:
            self.closing = True
            self._close_code = code
            self._close_requested.set()

    async def _watch_close(self) -> None:
        await self._close_requested.wait()
        # Unwind both I/O loops before sending the close frame in run's finally.
        raise WebSocketDisconnect(self._close_code)

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
            if message["t"] == "move":
                direction = message.get("direction")
                self.move_intent = None if direction == {"x": 0, "y": 0} else message
            # Other valid intents have no gameplay effects yet.

    async def run(self, tick: int, *, world_size: int, chunk_size: int, seed: int) -> None:
        # The endpoint accepts and adds the player before calling run.
        self.registry.register(self)
        try:
            await self.send({
                "t": "welcome", "v": SCHEMA_VERSION, "entity_id": self.id, "tick": tick,
                "world_size": world_size, "chunk_size": chunk_size, "seed": seed,
                "config": {"movement_hz": 10, "sim_hz": 1, "aoi_radius": self.aoi_radius},
            })
            async with asyncio.TaskGroup() as group:
                group.create_task(self._send_loop())
                group.create_task(self._watch_close())
                await self._recv_loop()
        except* WebSocketDisconnect:
            pass
        finally:
            try:
                if self.closing:
                    await self.websocket.close(code=self._close_code)
            finally:
                self.registry.unregister(self)
