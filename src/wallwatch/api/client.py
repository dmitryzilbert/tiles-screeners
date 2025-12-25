from __future__ import annotations

import asyncio
import inspect
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Callable, Iterable

import grpc
import t_tech.invest as tinvest
from t_tech.invest import AsyncClient, MarketDataRequest, SubscriptionAction
from t_tech.invest import schemas
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


class InstrumentResolveError(RuntimeError):
    pass


DEFAULT_TICK_SIZE = 0.01
_UID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{10}$")
_FIGI_RE = re.compile(r"^BBG[A-Z0-9]{9}$")
_KIND_RANKS = {
    schemas.InstrumentType.INSTRUMENT_TYPE_SHARE: 0,
    schemas.InstrumentType.INSTRUMENT_TYPE_ETF: 1,
    schemas.InstrumentType.INSTRUMENT_TYPE_BOND: 2,
    schemas.InstrumentType.INSTRUMENT_TYPE_CURRENCY: 3,
    schemas.InstrumentType.INSTRUMENT_TYPE_FUTURES: 4,
    schemas.InstrumentType.INSTRUMENT_TYPE_OPTION: 5,
    schemas.InstrumentType.INSTRUMENT_TYPE_SP: 6,
    schemas.InstrumentType.INSTRUMENT_TYPE_CLEARING_CERTIFICATE: 7,
    schemas.InstrumentType.INSTRUMENT_TYPE_INDEX: 8,
    schemas.InstrumentType.INSTRUMENT_TYPE_COMMODITY: 9,
    schemas.InstrumentType.INSTRUMENT_TYPE_UNSPECIFIED: 10,
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _quotation_to_float(value) -> float:
    return float(quotation_to_decimal(value))


def _timestamp_to_datetime(value) -> datetime:
    return value.ToDatetime().astimezone(timezone.utc) if value else _now_utc()


class MarketDataClient:
    def __init__(
        self,
        token: str,
        logger: logging.Logger,
        root_certificates: bytes | None = None,
        stream_idle_sleep_seconds: float = 3600.0,
        instrument_status: schemas.InstrumentStatus | None = schemas.InstrumentStatus.INSTRUMENT_STATUS_BASE,
    ) -> None:
        self._token = token
        self._logger = logger
        self._root_certificates = root_certificates
        self._stream_idle_sleep_seconds = stream_idle_sleep_seconds
        self._instrument_status = instrument_status

    async def resolve_instruments(self, symbols: Iterable[str]) -> tuple[list[InstrumentInfo], list[str]]:
        resolved: list[InstrumentInfo] = []
        failures: list[str] = []
        async with self._client() as client:
            service = client.instruments
            for symbol in symbols:
                try:
                    info = await self._resolve_symbol(service, symbol)
                except InstrumentResolveError as exc:
                    self._logger.warning(
                        "instrument_resolve_failed",
                        extra={"symbol": symbol, "error": str(exc)},
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    self._logger.warning(
                        "instrument_resolve_failed",
                        extra={"symbol": symbol, "error": str(exc)},
                    )
                    continue
                if info is None:
                    failures.append(symbol)
                    continue
                resolved.append(info)
        return resolved, failures

    async def _resolve_symbol(
        self, service: InstrumentsService, symbol: str
    ) -> InstrumentInfo | None:
        response = await self._find_instrument(service, symbol)
        if not response.instruments:
            return None
        instrument = self._select_best_match(symbol, response.instruments)
        if instrument is None:
            raise InstrumentResolveError("no_matching_instrument")
        instrument_id, id_type = self._resolve_lookup_id(instrument)
        if instrument_id is None or id_type is None:
            raise InstrumentResolveError("no_uid_or_figi")
        full_response = await service.get_instrument_by(id_type=id_type, id=instrument_id)
        full_instrument = full_response.instrument
        tick_size = self._resolve_tick_size(symbol, full_instrument)
        return InstrumentInfo(instrument_id=instrument_id, symbol=symbol, tick_size=tick_size)

    async def _find_instrument(
        self, service: InstrumentsService, symbol: str
    ) -> schemas.FindInstrumentResponse:
        kwargs: dict[str, object] = {"query": symbol}
        if self._instrument_status is not None:
            if "instrument_status" in inspect.signature(service.find_instrument).parameters:
                kwargs["instrument_status"] = self._instrument_status
        return await service.find_instrument(**kwargs)

    def _resolve_lookup_id(
        self, instrument: schemas.InstrumentShort
    ) -> tuple[str | None, schemas.InstrumentIdType | None]:
        uid = getattr(instrument, "uid", None)
        if uid:
            return uid, schemas.InstrumentIdType.INSTRUMENT_ID_TYPE_UID
        figi = getattr(instrument, "figi", None)
        if figi:
            return figi, schemas.InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI
        return None, None

    def _resolve_tick_size(self, symbol: str, instrument: schemas.Instrument) -> float:
        min_price_increment = getattr(instrument, "min_price_increment", None)
        tick_size = 0.0
        if min_price_increment is not None:
            try:
                tick_size = _quotation_to_float(min_price_increment)
            except Exception:  # noqa: BLE001
                tick_size = 0.0
        if tick_size <= 0.0:
            self._logger.warning(
                "instrument_tick_size_missing",
                extra={
                    "symbol": symbol,
                    "instrument_id": getattr(instrument, "uid", None)
                    or getattr(instrument, "figi", None),
                },
            )
            return DEFAULT_TICK_SIZE
        return tick_size

    def _select_best_match(
        self, symbol: str, instruments: Iterable[schemas.InstrumentShort]
    ) -> schemas.InstrumentShort | None:
        candidates = list(instruments)
        if not candidates:
            return None
        symbol_upper = symbol.upper()
        matches = self._filter_matches(symbol, symbol_upper, candidates)
        if matches is None:
            return None
        if matches:
            candidates = matches
        return min(candidates, key=self._candidate_rank)

    def _filter_matches(
        self,
        symbol: str,
        symbol_upper: str,
        instruments: list[schemas.InstrumentShort],
    ) -> list[schemas.InstrumentShort] | None:
        if _UID_RE.match(symbol):
            return [item for item in instruments if getattr(item, "uid", None) == symbol]
        if _FIGI_RE.match(symbol_upper):
            return [
                item
                for item in instruments
                if (getattr(item, "figi", "") or "").upper() == symbol_upper
            ]
        if _ISIN_RE.match(symbol_upper):
            isin_matches = [
                item
                for item in instruments
                if (getattr(item, "isin", "") or "").upper() == symbol_upper
            ]
            if isin_matches:
                return isin_matches
            fallback = [
                item
                for item in instruments
                if (getattr(item, "uid", None) == symbol)
                or (getattr(item, "figi", "") or "").upper() == symbol_upper
            ]
            return fallback or None
        ticker_matches = [
            item
            for item in instruments
            if (getattr(item, "ticker", "") or "").upper() == symbol_upper
        ]
        return ticker_matches or None

    def _candidate_rank(self, instrument: schemas.InstrumentShort) -> tuple[bool, int]:
        api_flag = bool(getattr(instrument, "api_trade_available_flag", False))
        instrument_kind = getattr(
            instrument,
            "instrument_kind",
            schemas.InstrumentType.INSTRUMENT_TYPE_UNSPECIFIED,
        )
        kind_rank = _KIND_RANKS.get(instrument_kind, len(_KIND_RANKS))
        return (not api_flag, kind_rank)

    async def stream_market_data(
        self,
        instruments: list[InstrumentInfo],
        depth: int,
        on_order_book: Callable[[OrderBookSnapshot], list],
        on_trade: Callable[[Trade], list],
        on_alerts: Callable[[list], None],
        stop_event: asyncio.Event,
    ) -> None:
        async with self._client() as client:
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
            self._logger.info(
                "subscribed",
                extra={
                    "order_books": len(order_books),
                    "trades": len(trades),
                },
            )
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
                await asyncio.sleep(self._stream_idle_sleep_seconds)

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

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[AsyncClient]:
        kwargs, channel = self._client_kwargs()
        async with AsyncClient(self._token, **kwargs) as client:
            yield client
        if channel is not None:
            await channel.close()

    def _client_kwargs(self) -> tuple[dict[str, object], grpc.aio.Channel | None]:
        return {}, None


def _resolve_grpc_endpoint() -> str | None:
    for name in (
        "API_URL",
        "API_ENDPOINT",
        "DEFAULT_API_URL",
        "DEFAULT_ENDPOINT",
        "DEFAULT_HOST",
        "DEFAULT_GRPC_ENDPOINT",
    ):
        value = getattr(tinvest, name, None)
        if isinstance(value, str) and value:
            return value
    return None
