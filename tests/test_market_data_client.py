from __future__ import annotations

import logging
from dataclasses import dataclass

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
    )

    mapped_trade = client._map_trade(trade)

    assert mapped_trade is not None
    assert mapped_trade.instrument_id == "uid-456"
