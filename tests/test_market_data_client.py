from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from t_tech.invest import schemas

from wallwatch.api.client import MarketDataClient


@dataclass
class FakeOrderLevel:
    price: schemas.Quotation
    quantity: float


@dataclass
class FakeOrderBook:
    instrument_uid: str
    bids: list[FakeOrderLevel]
    asks: list[FakeOrderLevel]
    time: object | None = None


@dataclass
class FakeTrade:
    instrument_uid: str
    price: schemas.Quotation
    quantity: float
    direction: int
    time: object | None = None


def test_map_order_book_uses_instrument_uid() -> None:
    client = MarketDataClient(token="token", logger=logging.getLogger("test"))
    orderbook = FakeOrderBook(
        instrument_uid="uid-123",
        bids=[FakeOrderLevel(price=schemas.Quotation(units=1, nano=0), quantity=10.0)],
        asks=[FakeOrderLevel(price=schemas.Quotation(units=2, nano=0), quantity=20.0)],
        time=datetime.now(timezone.utc),
    )

    snapshot = client._map_order_book(orderbook)

    assert snapshot is not None
    assert snapshot.instrument_id == "uid-123"


def test_map_trade_uses_instrument_uid() -> None:
    client = MarketDataClient(token="token", logger=logging.getLogger("test"))
    trade = FakeTrade(
        instrument_uid="uid-456",
        price=schemas.Quotation(units=3, nano=0),
        quantity=5.0,
        direction=1,
        time=datetime.now(timezone.utc),
    )

    mapped_trade = client._map_trade(trade)

    assert mapped_trade is not None
    assert mapped_trade.instrument_id == "uid-456"


def test_stream_market_data_handles_cancelled_error(caplog) -> None:
    async def run() -> None:
        client = MarketDataClient(token="token", logger=logging.getLogger("test"))

        class FakeMarketDataStream:
            def market_data_stream(self, *_: object, **__: object):
                async def _iter():
                    raise asyncio.CancelledError
                    yield  # pragma: no cover

                return _iter()

        class FakeClient:
            market_data_stream = FakeMarketDataStream()

        @asynccontextmanager
        async def fake_client():
            yield FakeClient()

        client._client = fake_client  # type: ignore[assignment]
        stop_event = asyncio.Event()
        await client.stream_market_data(
            instruments=[],
            depth=1,
            on_order_book=lambda _: [],
            on_trade=lambda _: [],
            on_alerts=lambda _: None,
            stop_event=stop_event,
        )

    caplog.set_level(logging.INFO)
    asyncio.run(run())
    assert any(
        record.msg == {"message": "shutdown", "reason": "cancelled"}
        for record in caplog.records
    )
