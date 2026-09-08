from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class GameLoop:
    """Integer-nanosecond accumulator; hooks are synchronous and must not block.

    At coincident deadlines movement runs before simulation. Catch-up never drops
    ticks. Inject a monotonic clock and sleeper for deterministic virtual time.
    """

    MOVEMENT_NS = 100_000_000
    SIM_EVERY = 10

    def __init__(
        self,
        on_movement_tick: Callable[[], None] | None = None,
        on_sim_tick: Callable[[], None] | None = None,
        *,
        clock: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.on_movement_tick = on_movement_tick or (lambda: None)
        self.on_sim_tick = on_sim_tick or (lambda: None)
        self._clock = clock
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._last_ns = 0
        self._accumulator_ns = 0
        self._movement_ticks = 0

    def start(self) -> None:
        if self._task is not None:
            return
        self._last_ns = self._clock()
        self._accumulator_ns = 0
        self._movement_ticks = 0
        self._task = asyncio.create_task(self._run(), name="terra-game-loop")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        while True:
            now = self._clock()
            elapsed = now - self._last_ns
            if elapsed < 0:
                raise ValueError("clock must be monotonic")
            self._last_ns = now
            self._accumulator_ns += elapsed
            while self._accumulator_ns >= self.MOVEMENT_NS:
                self._accumulator_ns -= self.MOVEMENT_NS
                self._movement_ticks += 1
                self.on_movement_tick()
                if self._movement_ticks % self.SIM_EVERY == 0:
                    self.on_sim_tick()
            await self._sleep((self.MOVEMENT_NS - self._accumulator_ns) / 1_000_000_000)
