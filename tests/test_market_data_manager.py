from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from wallwatch.api.client import InstrumentInfo
from wallwatch.app.market_data_manager import MarketDataManager
from wallwatch.app.runtime_state import RuntimeState
from wallwatch.detector.wall_detector import DetectorConfig
from wallwatch.notify.notifier import ConsoleNotifier


def _build_manager(
    *,
    client: object,
    runtime_state: RuntimeState,
    stop_event: asyncio.Event,
) -> MarketDataManager:
    logger = logging.getLogger("test_market_data_manager")
    return MarketDataManager(
        detector_config=DetectorConfig(),
        client=client,  # type: ignore[arg-type]
        logger=logger,
        notifier=None,
        alert_notifier=ConsoleNotifier(logger),
        runtime_state=runtime_state,
        stop_event=stop_event,
        debug_enabled=False,
        debug_interval=1.0,
        retry_backoff_initial_seconds=0.0,
        retry_backoff_max_seconds=0.0,
    )


def test_idle_does_not_subscribe() -> None:
    async def _run() -> None:
        stop_event = asyncio.Event()
        runtime_state = RuntimeState(
            started_at=datetime.now(timezone.utc),
            pid=1,
            current_symbols=[],
            depth=1,
        )
        called = False

        class DummyClient:
            async def resolve_instruments(self, symbols: list[str]) -> tuple[list[InstrumentInfo], list[str]]:
                return [], []

            async def stream_market_data(self, **_: object) -> None:
                nonlocal called
                called = True

        manager = _build_manager(client=DummyClient(), runtime_state=runtime_state, stop_event=stop_event)
        await manager.start([])
        await asyncio.sleep(0.05)
        stop_event.set()
        await manager.stop()

        snapshot = await runtime_state.snapshot()
        assert snapshot.stream_state == "idle"
        assert not called

    asyncio.run(_run())


def test_symbols_present_unchanged() -> None:
    async def _run() -> None:
        stop_event = asyncio.Event()
        runtime_state = RuntimeState(
            started_at=datetime.now(timezone.utc),
            pid=1,
            current_symbols=[],
            depth=1,
        )
        called = False

        class DummyClient:
            async def resolve_instruments(self, symbols: list[str]) -> tuple[list[InstrumentInfo], list[str]]:
                return [InstrumentInfo(instrument_id="id", symbol=symbols[0], tick_size=0.01)], []

            async def stream_market_data(self, *, stop_event: asyncio.Event, **_: object) -> None:
                nonlocal called
                called = True
                stop_event.set()

        manager = _build_manager(client=DummyClient(), runtime_state=runtime_state, stop_event=stop_event)
        await manager.start(["SBER"])
        await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        await manager.stop()

        assert called

    asyncio.run(_run())
