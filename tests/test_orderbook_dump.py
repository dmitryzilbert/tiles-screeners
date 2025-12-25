from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from wallwatch.api.client import InstrumentInfo
from wallwatch.app import main as app_main
from wallwatch.state.models import OrderBookLevel, OrderBookSnapshot


def test_orderbook_dump_throttles_requests() -> None:
    call_times: list[float] = []
    stop_event = asyncio.Event()

    class FakeClient:
        async def get_order_book(self, instrument_id: str, depth: int) -> OrderBookSnapshot:
            call_times.append(time.monotonic())
            if len(call_times) >= 3:
                stop_event.set()
            return OrderBookSnapshot(
                instrument_id=instrument_id,
                bids=[OrderBookLevel(price=100.0, quantity=1.0)],
                asks=[OrderBookLevel(price=101.0, quantity=1.0)],
                best_bid=100.0,
                best_ask=101.0,
                ts=datetime.now(timezone.utc),
            )

    async def run() -> None:
        await app_main._run_orderbook_dump(
            client=FakeClient(),
            instruments=[InstrumentInfo(instrument_id="uid-1", symbol="SBER", tick_size=0.01)],
            depth=1,
            interval=0.05,
            logger=logging.getLogger("test"),
            stop_event=stop_event,
        )

    asyncio.run(run())

    assert len(call_times) == 3
    intervals = [b - a for a, b in zip(call_times, call_times[1:])]
    assert all(interval >= 0.04 for interval in intervals)
