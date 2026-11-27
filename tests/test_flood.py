import asyncio

from tgdl.downloader import flood
from tgdl.downloader.flood import FloodGate
from tgdl.events import EventBus


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_gate_status_and_extension(monkeypatch) -> None:
    monkeypatch.setattr(flood, "SAFETY_MARGIN", 1.0)
    clock = Clock()
    g = FloodGate("download", clock=clock, wall=lambda: 5000.0)
    assert not g.active and g.status()["until"] is None
    g.trigger(30)
    assert g.active and g.remaining == 31 and g.status()["until"] == 5031.0
    g.trigger(5)  # a shorter wait never shortens an existing pause
    assert g.remaining == 31
    clock.t += 31
    assert not g.active and g.status()["remaining"] == 0


async def test_gate_publishes_and_blocks(monkeypatch) -> None:
    monkeypatch.setattr(flood, "SAFETY_MARGIN", 0.0)
    bus = EventBus()
    g = FloodGate("download", bus)
    async with bus.subscribe() as q:
        g.trigger(0.2)
        ev = q.get_nowait()
        assert ev.type == "flood" and ev.data["active"] and ev.data["scope"] == "download"
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await g.wait()
        assert loop.time() - t0 >= 0.15
        ev = q.get_nowait()
        assert ev.type == "flood" and not ev.data["active"]


async def test_bus_drops_when_full() -> None:
    bus = EventBus(queue_size=2)
    async with bus.subscribe() as q:
        for i in range(5):
            bus.publish("x", i=i)
        assert q.qsize() == 2
    assert bus.subscriber_count == 0
