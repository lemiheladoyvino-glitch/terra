from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from server.net.protocol import SCHEMA_VERSION, ProtocolError, decode, encode

HELLO_TIMEOUT = 0.5

CLIENT_TYPES = {"hello", "move", "interact", "craft", "chat"}


class ConnectionRegistry:
    def __init__(self) -> None:
        self.connections: dict[str, Connection] = {}

    def register(self, connection: Connection) -> None:
        self.connections[connection.id] = connection

    def unregister(self, connection: Connection) -> None:
        if self.connections.get(connection.id) is connection:
            self.connections.pop(connection.id, None)


class Connection:
    def __init__(self, websocket: WebSocket, registry: ConnectionRegistry) -> None:
        self.id = str(uuid4())
        self.hello_token: str | None = None
        self.token = str(uuid4())
        self.websocket = websocket
        self.registry = registry
        self.send_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self.move_intent: dict[str, Any] | None = None
        self.interact_intent: dict[str, Any] | None = None
        self.craft_intent: dict[str, Any] | None = None
        self.aoi_radius = 20
        self.known_records: dict[str, dict[str, Any]] = {}
        self.sent_chunks: set[tuple[int, int]] = set()
        self.bootstrapped = False
        self.closing = False
        self._close_code = 1000
        self._close_reason = ""
        self._close_requested = asyncio.Event()

    def close_soon(self, code: int, reason: str = "") -> None:
        """Wake the TaskGroup-owned close watcher without blocking the tick."""
        if not self.closing:
            self.closing = True
            self._close_code = code
            self._close_reason = reason
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

    async def _read_message(self) -> dict[str, Any]:
        try:
            raw = await self.websocket.receive_text()
        except KeyError as exc:
            raise ProtocolError("expected text frame") from exc
        message = decode(raw)
        if message["t"] not in CLIENT_TYPES or "sender_id" in message:
            raise ProtocolError("message is not a client intent")
        return message

    def _handle_intent(self, message: dict[str, Any]) -> None:
        if message["t"] == "hello":
            raise ProtocolError("hello must be the first message")
        if message["t"] == "move":
            direction = message.get("direction")
            self.move_intent = None if direction == {"x": 0, "y": 0} else message
        elif message["t"] == "interact":
            self.interact_intent = message
        elif message["t"] == "craft":
            self.craft_intent = message

    async def _invalid_message(self, error: ProtocolError) -> None:
        await self.send(
            {
                "t": "error",
                "v": SCHEMA_VERSION,
                "code": "invalid_message",
                "message": str(error)[:2048],
            }
        )

    async def _recv_loop(self) -> None:
        while True:
            try:
                self._handle_intent(await self._read_message())
            except ProtocolError as exc:
                await self._invalid_message(exc)

    async def run(
        self,
        tick: int,
        *,
        world_size: int,
        chunk_size: int,
        seed: int,
        on_welcome: Callable[[], None] | None = None,
        on_connect: Callable[[], int] | None = None,
    ) -> None:
        # Endpoint accepts first; identity is established before registry/welcome.
        try:
            first_error = None
            try:
                first = await asyncio.wait_for(self._read_message(), timeout=HELLO_TIMEOUT)
                if first["t"] == "hello":
                    self.hello_token = first["token"]
                else:
                    self._handle_intent(first)
            except TimeoutError:
                pass
            except ProtocolError as exc:
                first_error = exc
            if on_connect is not None:
                tick = on_connect()
            self.registry.register(self)
            await self.send(
                {
                    "t": "welcome",
                    "v": SCHEMA_VERSION,
                    "entity_id": self.id,
                    "tick": tick,
                    "token": self.token,
                    "world_size": world_size,
                    "chunk_size": chunk_size,
                    "seed": seed,
                    "config": {"movement_hz": 10, "sim_hz": 1, "aoi_radius": self.aoi_radius},
                }
            )
            if on_welcome is not None:
                on_welcome()
            if first_error is not None:
                await self._invalid_message(first_error)
            async with asyncio.TaskGroup() as group:
                group.create_task(self._send_loop())
                group.create_task(self._watch_close())
                await self._recv_loop()
        except* WebSocketDisconnect:
            pass
        finally:
            try:
                if self.closing:
                    if self._close_reason:
                        await self.websocket.close(code=self._close_code, reason=self._close_reason)
                    else:
                        await self.websocket.close(code=self._close_code)
            finally:
                self.registry.unregister(self)
