from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Deque, Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    quantity: float


@dataclass(frozen=True)
class OrderBookSnapshot:
    instrument_id: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    best_bid: Optional[float]
    best_ask: Optional[float]
    ts: datetime


@dataclass(frozen=True)
class Trade:
    instrument_id: str
    price: float
    quantity: float
    side: Optional[Side]
    ts: datetime


@dataclass
class WallCandidate:
    side: Side
    price: float
    size: float
    ratio: float
    v_ref: float
    distance_ticks: int


@dataclass
class Alert:
    instrument_id: str
    side: Side
    price: float
    event: str
    size: float
    ratio: float
    v_ref: float
    distance_ticks: int
    dwell_seconds: float
    executed_at_wall: float
    cancel_share: float
    reasons: list[str]
    ts: datetime


@dataclass
class ActiveWall:
    side: Side
    price: float
    first_seen: datetime
    last_seen: datetime
    last_size: float
    distance_ticks: int
    ratio_to_median: float
    reposition_count: int = 0
    confirmed_ts: Optional[datetime] = None
    consuming_ts: Optional[datetime] = None
    last_confirm_alert_ts: Optional[datetime] = None
    last_consuming_alert_ts: Optional[datetime] = None
    size_history: Deque[tuple[datetime, float]] = field(
        default_factory=lambda: deque(maxlen=200)
    )


@dataclass
class InstrumentState:
    instrument_id: str
    tick_size: float
    symbol: str
    last_snapshot: Optional[OrderBookSnapshot] = None
    trades: Deque[Trade] = field(default_factory=deque)
    active_wall: Optional[ActiveWall] = None
    last_debug_ts: Optional[datetime] = None
    last_debug_candidate_size: Optional[float] = None
    last_event_state: str = "NONE"
    last_event_wall_key: Optional[str] = None


@dataclass
class WallEvent:
    event: str
    symbol: str
    side: Side
    price: float
    qty: float
    wall_key: str
    distance_ticks_to_spread: Optional[int]
    distance_ticks: int
    ratio_to_median: float
    dwell_seconds: float
    qty_change_last_interval: float
    thresholds: Optional[dict[str, float | int]] = None
    timestamp: Optional[datetime] = None
    reason: Optional[str] = None

    def to_log_extra(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "symbol": self.symbol,
            "wall_key": self.wall_key,
            "side": self.side,
            "price": self.price,
            "qty": self.qty,
            "distance_ticks_to_spread": self.distance_ticks_to_spread,
            "distance_ticks": self.distance_ticks,
            "ratio_to_median": self.ratio_to_median,
            "dwell_seconds": self.dwell_seconds,
            "qty_change_last_interval": self.qty_change_last_interval,
        }
        if self.thresholds is not None:
            payload["thresholds"] = self.thresholds
        if self.timestamp is not None:
            payload["timestamp"] = self.timestamp.isoformat()
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload
