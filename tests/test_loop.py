from __future__ import annotations

import asyncio

import pytest

from server.game.loop import GameLoop


class FakeClock:
    def __init__(self) -> None:
        self.now = 0
        self.wake = asyncio.Event()
        self.waiting = asyncio.Event()

    def clock(self) -> int:
        return self.now

    async def sleep(self, delay: float) -> None:
        assert 0 < delay <= 0.1
        self.waiting.set()
        await self.wake.wait()
        self.wake.clear()

    async def advance(self, nanoseconds: int) -> None:
        await self.waiting.wait()
        self.waiting.clear()
        self.now += nanoseconds
        self.wake.set()
        await self.waiting.wait()


@pytest.mark.parametrize("steps", [[100_000_000] * 100, [10_000_000_000], [37_000_000] * 1000])
def test_exact_ticks_and_no_drift(steps: list[int]) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        events: list[str] = []
        loop = GameLoop(lambda: events.append("m"), lambda: events.append("s"),
                        clock=clock.clock, sleep=clock.sleep)
        loop.start()
        loop.start()  # Idempotent; must not create a second task.
        elapsed = 0
        for step in steps:
            await clock.advance(step)
            elapsed += step
            assert events.count("m") == elapsed // 100_000_000
            assert events.count("s") == elapsed // 1_000_000_000
        assert events == (["m"] * 10 + ["s"]) * (elapsed // 1_000_000_000) + ["m"] * (
            elapsed // 100_000_000 % 10
        )
        await loop.stop()
        before = events.copy()
        clock.now += 100_000_000_000
        await asyncio.sleep(0)
        assert events == before
        await loop.stop()
        loop.start()
        await clock.advance(1_000_000_000)
        assert events[len(before):] == ["m"] * 10 + ["s"]
        await loop.stop()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_stop_before_task_runs() -> None:
    async def scenario() -> None:
        loop = GameLoop()
        await loop.stop()
        loop.start()
        await loop.stop()

    asyncio.run(scenario())
