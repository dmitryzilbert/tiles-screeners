from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from dataclasses import asdict
from typing import Iterable

from wallwatch.api.client import InstrumentInfo, MarketDataClient
from wallwatch.app.runtime_state import RuntimeState, WallEventState
from wallwatch.detector.wall_detector import DetectorConfig, WallDetector
from wallwatch.notify.notifier import Notifier
from wallwatch.notify.telegram_notifier import TelegramNotifier
from wallwatch.state.models import Alert, OrderBookSnapshot, Trade, WallEvent


class MarketDataManager:
    def __init__(
        self,
        *,
        detector_config: DetectorConfig,
        client: MarketDataClient,
        logger: logging.Logger,
        notifier: TelegramNotifier | None,
        alert_notifier: Notifier,
        runtime_state: RuntimeState,
        stop_event: asyncio.Event,
        debug_enabled: bool,
        debug_interval: float,
        retry_backoff_initial_seconds: float,
        retry_backoff_max_seconds: float,
    ) -> None:
        self._detector_config = detector_config
        self._client = client
        self._logger = logger
        self._notifier = notifier
        self._alert_notifier = alert_notifier
        self._runtime_state = runtime_state
        self._stop_event = stop_event
        self._debug_enabled = debug_enabled
        self._debug_interval = debug_interval
        self._retry_backoff_initial_seconds = retry_backoff_initial_seconds
        self._retry_backoff_max_seconds = retry_backoff_max_seconds
        self._symbols: list[str] = []
        self._lock = asyncio.Lock()
        self._restart_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._rx_orderbooks_interval = 0
        self._rx_trades_interval = 0
        self._rx_total_orderbooks = 0
        self._rx_total_trades = 0
        self._last_message_ts: float | None = None

    async def start(self, symbols: list[str]) -> None:
        await self.update_symbols(symbols)
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            return

    async def update_symbols(self, symbols: list[str]) -> None:
        normalized = list(dict.fromkeys(symbol.upper() for symbol in symbols if symbol))
        async with self._lock:
            self._symbols = normalized
        self._runtime_state.update_sync(current_symbols=list(normalized))
        self._restart_event.set()

    async def get_symbols(self) -> list[str]:
        async with self._lock:
            return list(self._symbols)

    def consume_interval_counts(self) -> tuple[int, int]:
        orderbooks = self._rx_orderbooks_interval
        trades = self._rx_trades_interval
        self._rx_orderbooks_interval = 0
        self._rx_trades_interval = 0
        return orderbooks, trades

    @property
    def last_message_ts(self) -> float | None:
        return self._last_message_ts

    async def _run(self) -> None:
        backoff = self._retry_backoff_initial_seconds
        while not self._stop_event.is_set():
            symbols = await self.get_symbols()
            if not symbols:
                self._runtime_state.update_sync(stream_state="connecting", last_error="no symbols")
                await asyncio.sleep(1.0)
                continue

            try:
                await self._stream_symbols(symbols)
                backoff = self._retry_backoff_initial_seconds
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                self._logger.error("stream_failed", extra={"error": str(exc)})
                self._runtime_state.update_sync(
                    stream_state="backoff",
                    last_error=str(exc),
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._retry_backoff_max_seconds)

            if self._restart_event.is_set():
                self._restart_event.clear()

    async def _stream_symbols(self, symbols: list[str]) -> None:
        self._runtime_state.update_sync(stream_state="connecting", last_error=None)
        resolved, failures = await self._client.resolve_instruments(symbols)
        for item in failures:
            self._logger.warning("instrument_not_found", extra={"symbol": item})
        if not resolved:
            raise RuntimeError("no_instruments_resolved")

        detector = WallDetector(
            DetectorConfig(**{**asdict(self._detector_config), "depth": self._detector_config.depth})
        )
        for instrument in resolved:
            detector.upsert_instrument(
                instrument_id=instrument.instrument_id,
                tick_size=instrument.tick_size,
                symbol=instrument.symbol,
            )
        instrument_by_symbol = {instrument.symbol: instrument for instrument in resolved}
        if self._notifier is not None:
            self._notifier.update_instruments(instrument_by_symbol)

        class _CompositeStopEvent:
            def __init__(self, stop_event: asyncio.Event, restart_event: asyncio.Event) -> None:
                self._stop_event = stop_event
                self._restart_event = restart_event

            def is_set(self) -> bool:
                return self._stop_event.is_set() or self._restart_event.is_set()

            def set(self) -> None:
                self._stop_event.set()

        stream_stop = _CompositeStopEvent(self._stop_event, self._restart_event)
        connected_logged = False

        def _mark_connected() -> None:
            nonlocal connected_logged
            self._last_message_ts = time.monotonic()
            if not connected_logged:
                connected_logged = True
                self._runtime_state.update_sync(stream_state="connected")
                self._logger.info("connected")

        def _handle_events(events: Iterable[WallEvent]) -> None:
            for event in events:
                self._logger.info(event.event, extra=event.to_log_extra())
                self._runtime_state.update_sync(
                    last_wall_event=WallEventState(
                        event_type=event.event,
                        ts=event.timestamp or datetime.now(timezone.utc),
                        symbol=event.symbol,
                        side=str(event.side),
                        price=event.price,
                        qty=event.qty,
                    )
                )
                if self._notifier is not None:
                    self._notifier.notify(event)

        def _on_order_book(snapshot: OrderBookSnapshot) -> list[Alert]:
            _mark_connected()
            self._rx_orderbooks_interval += 1
            self._rx_total_orderbooks += 1
            self._runtime_state.update_sync(rx_total_orderbooks=self._rx_total_orderbooks)
            if self._debug_enabled:
                alerts, debug_payload, events = detector.on_order_book_with_debug(
                    snapshot, self._debug_interval
                )
                _handle_events(events)
                if debug_payload is not None:
                    self._logger.info("wall_debug", extra=debug_payload)
                return alerts
            alerts, events = detector.on_order_book_with_events(snapshot)
            _handle_events(events)
            return alerts

        def _on_trade(trade: Trade) -> list[Alert]:
            _mark_connected()
            self._rx_trades_interval += 1
            self._rx_total_trades += 1
            self._runtime_state.update_sync(rx_total_trades=self._rx_total_trades)
            return detector.on_trade(trade)

        await self._client.stream_market_data(
            instruments=resolved,
            depth=self._detector_config.depth,
            on_order_book=_on_order_book,
            on_trade=_on_trade,
            on_alerts=lambda alerts: [self._alert_notifier.notify(alert) for alert in alerts],
            stop_event=stream_stop,
        )
