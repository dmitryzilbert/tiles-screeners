from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wallwatch.detector.wall_detector import DetectorConfig, WallDetector
from wallwatch.state.models import OrderBookLevel, OrderBookSnapshot, Side, Trade


def _ts(offset: float) -> datetime:
    return datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset)


def _snapshot(ts: datetime, size: float, price: float = 100.0) -> OrderBookSnapshot:
    bids = [
        OrderBookLevel(price=101.0, quantity=120.0),
        OrderBookLevel(price=price, quantity=size),
        OrderBookLevel(price=99.0, quantity=90.0),
    ]
    asks = [OrderBookLevel(price=102.0, quantity=80.0)]
    return OrderBookSnapshot(
        instrument_id="inst",
        bids=bids,
        asks=asks,
        best_bid=101.0,
        best_ask=102.0,
        ts=ts,
    )


def _trade(ts: datetime, price: float, qty: float) -> Trade:
    return Trade(
        instrument_id="inst",
        price=price,
        quantity=qty,
        side=Side.SELL,
        ts=ts,
    )


def _detector() -> WallDetector:
    config = DetectorConfig(
        depth=20,
        distance_ticks=2,
        k_ratio=5,
        abs_qty_threshold=500,
        dwell_seconds=2,
        reposition_window_seconds=2,
        reposition_ticks=1,
        reposition_similar_pct=0.2,
        reposition_max=0,
        trades_window_seconds=10,
        Emin=10,
        Amin=0.1,
        cancel_share_max=0.7,
        consuming_drop_pct=0.2,
        consuming_window_seconds=5,
        min_exec_confirm=5,
        cooldown_confirmed_seconds=5,
        cooldown_consuming_seconds=3,
        vref_levels=2,
    )
    detector = WallDetector(config)
    detector.upsert_instrument("inst", tick_size=1.0, symbol="TEST")
    return detector


def test_real_wall_triggers_confirm_and_consuming() -> None:
    detector = _detector()
    assert detector.on_order_book(_snapshot(_ts(0), 1000.0)) == []
    assert detector.on_order_book(_snapshot(_ts(1), 1000.0)) == []
    detector.on_trade(_trade(_ts(2), 100.0, 12.0))
    alerts = detector.on_order_book(_snapshot(_ts(2), 1000.0))
    assert any(alert.event == "ALERT_WALL_CONFIRMED" for alert in alerts)

    detector.on_trade(_trade(_ts(3), 100.0, 8.0))
    alerts = detector.on_order_book(_snapshot(_ts(3), 700.0))
    assert any(alert.event == "ALERT_WALL_CONSUMING" for alert in alerts)


def test_spoof_teleport_does_not_confirm() -> None:
    detector = _detector()
    detector.on_order_book(_snapshot(_ts(0), 1000.0, price=100.0))
    detector.on_order_book(_snapshot(_ts(1), 1000.0, price=101.0))
    detector.on_trade(_trade(_ts(2), 101.0, 20.0))
    alerts = detector.on_order_book(_snapshot(_ts(2), 1000.0, price=101.0))
    assert not any(alert.event == "ALERT_WALL_CONFIRMED" for alert in alerts)


def test_cancel_without_trades_does_not_confirm() -> None:
    detector = _detector()
    detector.on_order_book(_snapshot(_ts(0), 1000.0))
    detector.on_order_book(_snapshot(_ts(2), 600.0))
    alerts = detector.on_order_book(_snapshot(_ts(3), 600.0))
    assert not any(alert.event == "ALERT_WALL_CONFIRMED" for alert in alerts)
