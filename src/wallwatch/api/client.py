from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Callable, Iterable

from t_tech.invest import AsyncClient, MarketDataRequest, SubscriptionAction
from t_tech.invest.services import InstrumentsService
from t_tech.invest.utils import quotation_to_decimal
from t_tech.invest import (
    OrderBookInstrument,
    SubscribeOrderBookRequest,
    SubscribeTradesRequest,
    TradeInstrument,
)

from wallwatch.state.models import OrderBookLevel, OrderBookSnapshot, Side, Trade


@dataclass(frozen=True)
class InstrumentInfo:
    instrument_id: str
    symbol: str
    tick_size: float


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _quotation_to_float(value) -> float:
    return float(quotation_to_decimal(value))


def _timestamp_to_datetime(value) -> datetime:
    return value.ToDatetime().astimezone(timezone.utc) if value else _now_utc()


class MarketDataClient:
    def __init__(self, token: str, logger: logging.Logger) -> None:
        self._token = token
        self._logger = logger

    async def resolve_instruments(self, symbols: Iterable[str]) -> tuple[list[InstrumentInfo], list[str]]:
        resolved: list[InstrumentInfo] = []
        failures: list[str] = []
        async with AsyncClient(self._token) as client:
            service = client.instruments
            for symbol in symbols:
                try:
                    info = await self._resolve_symbol(service, symbol)
                except Exception as exc:  # noqa: BLE001
                    self._logger.warning(
                        "instrument_resolve_failed",
                        extra={"symbol": symbol, "error": str(exc)},
                    )
                    failures.append(symbol)
                    continue
                if info is None:
                    failures.append(symbol)
                    continue
                resolved.append(info)
        return resolved, failures

    async def _resolve_symbol(
        self, service: InstrumentsService, symbol: str
    ) -> InstrumentInfo | None:
        response = await service.find_instrument(query=symbol)
        if not response.instruments:
            return None
        instrument = response.instruments[0]
        instrument_id = instrument.uid
        tick_size = _quotation_to_float(instrument.min_price_increment)
        return InstrumentInfo(instrument_id=instrument_id, symbol=symbol, tick_size=tick_size)

    async def stream_market_data(
        self,
        instruments: list[InstrumentInfo],
        depth: int,
        on_order_book: Callable[[OrderBookSnapshot], list],
        on_trade: Callable[[Trade], list],
        on_alerts: Callable[[list], None],
        stop_event: asyncio.Event,
    ) -> None:
        async with AsyncClient(self._token) as client:
            async for response in client.market_data_stream.market_data_stream(
                self._subscription_requests(instruments, depth)
            ):
                if stop_event.is_set():
                    break
                if response.orderbook:
                    snapshot = self._map_order_book(response.orderbook)
                    alerts = on_order_book(snapshot)
                    if alerts:
                        on_alerts(alerts)
                if response.trade:
                    trade = self._map_trade(response.trade)
                    alerts = on_trade(trade)
                    if alerts:
                        on_alerts(alerts)

    def _subscription_requests(
        self, instruments: list[InstrumentInfo], depth: int
    ) -> AsyncIterator[MarketDataRequest]:
        order_books = [
            OrderBookInstrument(instrument_id=info.instrument_id, depth=depth)
            for info in instruments
        ]
        trades = [
            TradeInstrument(instrument_id=info.instrument_id) for info in instruments
        ]

        async def _iter() -> AsyncIterator[MarketDataRequest]:
            yield MarketDataRequest(
                subscribe_order_book_request=SubscribeOrderBookRequest(
                    subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
                    instruments=order_books,
                )
            )
            yield MarketDataRequest(
                subscribe_trades_request=SubscribeTradesRequest(
                    subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
                    instruments=trades,
                )
            )
            while True:
                await asyncio.sleep(3600)

        return _iter()

    def _map_order_book(self, orderbook) -> OrderBookSnapshot:
        bids = [
            OrderBookLevel(
                price=_quotation_to_float(level.price),
                quantity=float(level.quantity),
            )
            for level in orderbook.bids
        ]
        asks = [
            OrderBookLevel(
                price=_quotation_to_float(level.price),
                quantity=float(level.quantity),
            )
            for level in orderbook.asks
        ]
        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None
        return OrderBookSnapshot(
            instrument_id=orderbook.instrument_id,
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
            ts=_timestamp_to_datetime(orderbook.time),
        )

    def _map_trade(self, trade) -> Trade:
        side = Side.BUY if trade.direction == 1 else Side.SELL if trade.direction == 2 else None
        return Trade(
            instrument_id=trade.instrument_id,
            price=_quotation_to_float(trade.price),
            quantity=float(trade.quantity),
            side=side,
            ts=_timestamp_to_datetime(trade.time),
        )
