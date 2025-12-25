from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Iterable

from wallwatch.state.models import (
    ActiveWall,
    Alert,
    InstrumentState,
    OrderBookLevel,
    OrderBookSnapshot,
    Side,
    Trade,
    WallCandidate,
    WallEvent,
)


@dataclass(frozen=True)
class DetectorConfig:
    max_symbols: int = 10
    depth: int = 20
    distance_ticks: int = 10
    k_ratio: float = 10.0
    abs_qty_threshold: float = 0.0
    dwell_seconds: float = 30.0
    reposition_window_seconds: float = 3.0
    reposition_ticks: int = 1
    reposition_similar_pct: float = 0.2
    reposition_max: int = 1
    trades_window_seconds: float = 20.0
    Emin: float = 200.0
    Amin: float = 0.2
    cancel_share_max: float = 0.7
    consuming_drop_pct: float = 0.2
    consuming_window_seconds: float = 8.0
    min_exec_confirm: float = 50.0
    cooldown_confirmed_seconds: float = 120.0
    cooldown_consuming_seconds: float = 45.0
    vref_levels: int = 10
    teleport_reset: bool = False


class WallDetector:
    def __init__(self, config: DetectorConfig) -> None:
        self._config = config
        self._states: dict[str, InstrumentState] = {}

    def upsert_instrument(self, instrument_id: str, tick_size: float, symbol: str) -> None:
        if instrument_id in self._states:
            return
        self._states[instrument_id] = InstrumentState(
            instrument_id=instrument_id, tick_size=tick_size, symbol=symbol
        )

    def remove_instrument(self, instrument_id: str) -> None:
        self._states.pop(instrument_id, None)

    def instrument_ids(self) -> set[str]:
        return set(self._states.keys())

    def list_states(self) -> list[InstrumentState]:
        return list(self._states.values())

    def on_trade(self, trade: Trade) -> list[Alert]:
        state = self._states.get(trade.instrument_id)
        if state is None:
            return []
        state.trades.append(trade)
        self._cleanup_trades(state, trade.ts)
        return []

    def on_order_book(self, snapshot: OrderBookSnapshot) -> list[Alert]:
        alerts, _, _ = self._process_order_book(snapshot, debug_interval=None)
        return alerts

    def on_order_book_with_debug(
        self, snapshot: OrderBookSnapshot, debug_interval: float
    ) -> tuple[list[Alert], dict[str, object] | None, list[WallEvent]]:
        return self._process_order_book(snapshot, debug_interval=debug_interval)

    def on_order_book_with_events(
        self, snapshot: OrderBookSnapshot
    ) -> tuple[list[Alert], list[WallEvent]]:
        alerts, _, events = self._process_order_book(snapshot, debug_interval=None)
        return alerts, events

    def _process_order_book(
        self, snapshot: OrderBookSnapshot, debug_interval: float | None
    ) -> tuple[list[Alert], dict[str, object] | None, list[WallEvent]]:
        state = self._states.get(snapshot.instrument_id)
        if state is None:
            return [], None, []
        state.last_snapshot = snapshot
        self._cleanup_trades(state, snapshot.ts)
        alerts: list[Alert] = []
        candidate = self._find_candidate(snapshot, state.tick_size)
        debug_payload: dict[str, object] | None = None
        events: list[WallEvent] = []

        if candidate is None:
            if state.active_wall is not None:
                wall = state.active_wall
                reason = self._resolve_lost_reason(snapshot, wall, teleport_detected=False)
                events.append(
                    self._build_wall_event(
                        state=state,
                        wall=wall,
                        qty=wall.last_size,
                        dwell_seconds=(snapshot.ts - wall.first_seen).total_seconds(),
                        event="wall_lost",
                        reason=reason,
                    )
                )
                state.active_wall = None
            debug_payload = self._build_debug_payload(
                state=state,
                snapshot=snapshot,
                candidate=None,
                wall=None,
                teleport_detected=False,
                dwell_seconds=0.0,
                debug_interval=debug_interval,
            )
            return alerts, debug_payload, events

        previous_wall = state.active_wall
        wall, teleport_detected = self._update_active_wall(state, candidate, snapshot.ts)
        wall.distance_ticks = candidate.distance_ticks
        wall.ratio_to_median = candidate.ratio
        if previous_wall is None or previous_wall is not wall:
            if previous_wall is not None:
                reason = self._resolve_lost_reason(snapshot, previous_wall, teleport_detected)
                events.append(
                    self._build_wall_event(
                        state=state,
                        wall=previous_wall,
                        qty=previous_wall.last_size,
                        dwell_seconds=(snapshot.ts - previous_wall.first_seen).total_seconds(),
                        event="wall_lost",
                        reason=reason,
                    )
                )
            events.append(
                self._build_wall_event(
                    state=state,
                    wall=wall,
                    qty=candidate.size,
                    dwell_seconds=(snapshot.ts - wall.first_seen).total_seconds(),
                    event="wall_candidate",
                )
            )
        previous_size = wall.last_size
        wall.size_history.append((snapshot.ts, candidate.size))
        wall.last_size = candidate.size
        wall.last_seen = snapshot.ts

        dwell_seconds = (snapshot.ts - wall.first_seen).total_seconds()
        executed_at_wall = self._executed_volume_at_price(state, candidate.price)
        size_drop = max(previous_size - candidate.size, 0.0)
        cancel_share = self._cancel_share(executed_at_wall, size_drop)
        absorption_score = executed_at_wall / max(candidate.size, 1e-9)

        should_confirm = self._should_confirm(
            wall,
            dwell_seconds,
            executed_at_wall,
            cancel_share,
            absorption_score,
            size_drop,
            snapshot.ts,
        )
        if should_confirm:
            if wall.confirmed_ts is None:
                events.append(
                    self._build_wall_event(
                        state=state,
                        wall=wall,
                        qty=candidate.size,
                        dwell_seconds=dwell_seconds,
                        event="wall_confirmed",
                    )
                )
            alert = self._build_alert(
                snapshot,
                candidate,
                "ALERT_WALL_CONFIRMED",
                dwell_seconds,
                executed_at_wall,
                cancel_share,
                [
                    f"dwell>={self._config.dwell_seconds}",
                    f"ratio>={self._config.k_ratio} or abs>={self._config.abs_qty_threshold}",
                ],
            )
            wall.confirmed_ts = snapshot.ts
            wall.last_confirm_alert_ts = snapshot.ts
            alerts.append(alert)

        should_consuming = self._should_consuming(
            wall, snapshot.ts, executed_at_wall, cancel_share
        )
        if should_consuming:
            if wall.consuming_ts is None:
                events.append(
                    self._build_wall_event(
                        state=state,
                        wall=wall,
                        qty=candidate.size,
                        dwell_seconds=dwell_seconds,
                        event="wall_consuming",
                    )
                )
                wall.consuming_ts = snapshot.ts
            reasons = [
                f"drop>={self._config.consuming_drop_pct:.2f}",
                f"exec>={self._config.min_exec_confirm}",
            ]
            alert = self._build_alert(
                snapshot,
                candidate,
                "ALERT_WALL_CONSUMING",
                dwell_seconds,
                executed_at_wall,
                cancel_share,
                reasons,
            )
            wall.last_consuming_alert_ts = snapshot.ts
            alerts.append(alert)

        debug_payload = self._build_debug_payload(
            state=state,
            snapshot=snapshot,
            candidate=candidate,
            wall=wall,
            teleport_detected=teleport_detected,
            dwell_seconds=dwell_seconds,
            debug_interval=debug_interval,
        )
        return alerts, debug_payload, events

    def _find_candidate(
        self, snapshot: OrderBookSnapshot, tick_size: float
    ) -> WallCandidate | None:
        candidates: list[WallCandidate] = []
        if snapshot.best_bid is not None:
            candidates += self._find_side_candidate(
                Side.BUY, snapshot.bids, snapshot.best_bid, tick_size
            )
        if snapshot.best_ask is not None:
            candidates += self._find_side_candidate(
                Side.SELL, snapshot.asks, snapshot.best_ask, tick_size
            )
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.ratio)

    def _find_side_candidate(
        self,
        side: Side,
        levels: list[OrderBookLevel],
        best_price: float,
        tick_size: float,
    ) -> list[WallCandidate]:
        if not levels:
            return []
        top_levels = levels[: self._config.vref_levels]
        v_ref = self._median_volume(top_levels)
        candidates: list[WallCandidate] = []
        for level in levels:
            dist_ticks = int(round(abs(level.price - best_price) / tick_size))
            if dist_ticks == 0 or dist_ticks > self._config.distance_ticks:
                continue
            ratio = level.quantity / max(v_ref, 1e-9)
            if ratio >= self._config.k_ratio or level.quantity >= self._config.abs_qty_threshold:
                candidates.append(
                    WallCandidate(
                        side=side,
                        price=level.price,
                        size=level.quantity,
                        ratio=ratio,
                        v_ref=v_ref,
                        distance_ticks=dist_ticks,
                    )
                )
        return candidates

    def _median_volume(self, levels: Iterable[OrderBookLevel]) -> float:
        values = [level.quantity for level in levels if level.quantity > 0]
        if not values:
            return 0.0
        return float(median(values))

    def _update_active_wall(
        self, state: InstrumentState, candidate: WallCandidate, ts: datetime
    ) -> tuple[ActiveWall, bool]:
        wall = state.active_wall
        if wall and wall.side == candidate.side and wall.price == candidate.price:
            return wall, False
        reposition_count = 0
        teleport_detected = False
        if wall is not None:
            within_window = (ts - wall.last_seen).total_seconds() <= self._config.reposition_window_seconds
            if within_window:
                price_delta = abs(candidate.price - wall.price)
                max_delta = self._config.reposition_ticks * state.tick_size
                size_similarity = abs(candidate.size - wall.last_size) / max(wall.last_size, 1e-9)
                if price_delta <= max_delta and size_similarity <= self._config.reposition_similar_pct:
                    reposition_count = wall.reposition_count + 1
                    teleport_detected = True
                    if self._config.teleport_reset:
                        reposition_count = 0
        new_wall = ActiveWall(
            side=candidate.side,
            price=candidate.price,
            first_seen=ts,
            last_seen=ts,
            last_size=candidate.size,
            distance_ticks=candidate.distance_ticks,
            ratio_to_median=candidate.ratio,
            reposition_count=reposition_count,
        )
        state.active_wall = new_wall
        return new_wall, teleport_detected

    def _resolve_lost_reason(
        self, snapshot: OrderBookSnapshot, wall: ActiveWall, teleport_detected: bool
    ) -> str:
        if teleport_detected:
            return "teleport"
        level_qty = self._find_level_quantity(snapshot, wall.side, wall.price)
        return "disappear" if level_qty is None else "cancel"

    def _find_level_quantity(
        self, snapshot: OrderBookSnapshot, side: Side, price: float
    ) -> float | None:
        levels = snapshot.bids if side == Side.BUY else snapshot.asks
        for level in levels:
            if level.price == price:
                return level.quantity
        return None

    def _build_wall_event(
        self,
        *,
        state: InstrumentState,
        wall: ActiveWall,
        qty: float,
        dwell_seconds: float,
        event: str,
        reason: str | None = None,
    ) -> WallEvent:
        return WallEvent(
            event=event,
            symbol=state.symbol,
            side=wall.side,
            price=wall.price,
            qty=qty,
            distance_ticks=wall.distance_ticks,
            ratio_to_median=wall.ratio_to_median,
            dwell_seconds=dwell_seconds,
            reason=reason,
        )

    def _build_debug_payload(
        self,
        *,
        state: InstrumentState,
        snapshot: OrderBookSnapshot,
        candidate: WallCandidate | None,
        wall: ActiveWall | None,
        teleport_detected: bool,
        dwell_seconds: float,
        debug_interval: float | None,
    ) -> dict[str, object] | None:
        if debug_interval is None:
            return None
        if debug_interval <= 0:
            debug_interval = 0.0
        if state.last_debug_ts is not None:
            elapsed = (snapshot.ts - state.last_debug_ts).total_seconds()
            if elapsed < debug_interval:
                return None
        state.last_debug_ts = snapshot.ts

        candidate_side = candidate.side.value if candidate else None
        candidate_price = candidate.price if candidate else None
        candidate_qty = candidate.size if candidate else None
        qty_ratio_to_median = candidate.ratio if candidate else None
        candidate_distance_ticks_to_spread = None
        spread = None
        if snapshot.best_bid is not None and snapshot.best_ask is not None:
            spread = snapshot.best_ask - snapshot.best_bid
            if candidate is not None:
                if candidate.side == Side.BUY:
                    candidate_distance_ticks_to_spread = int(
                        round(abs(snapshot.best_ask - candidate.price) / state.tick_size)
                    )
                else:
                    candidate_distance_ticks_to_spread = int(
                        round(abs(candidate.price - snapshot.best_bid) / state.tick_size)
                    )

        qty_change_last_interval = 0.0
        if candidate is not None:
            if state.last_debug_candidate_size is not None:
                qty_change_last_interval = candidate.size - state.last_debug_candidate_size
            state.last_debug_candidate_size = candidate.size
        else:
            state.last_debug_candidate_size = None

        debug_state = "NONE"
        if candidate is not None and wall is not None:
            if wall.confirmed_ts is not None:
                drop_pct = self._consuming_drop_pct(wall, snapshot.ts)
                debug_state = (
                    "CONSUMING"
                    if drop_pct >= self._config.consuming_drop_pct
                    else "CONFIRMED"
                )
            else:
                debug_state = "CANDIDATE"

        return {
            "symbol": state.symbol,
            "best_bid": snapshot.best_bid,
            "best_ask": snapshot.best_ask,
            "spread": spread,
            "candidate_side": candidate_side,
            "candidate_price": candidate_price,
            "candidate_qty": candidate_qty,
            "candidate_distance_ticks_to_spread": candidate_distance_ticks_to_spread,
            "qty_ratio_to_median": qty_ratio_to_median,
            "dwell_seconds": round(dwell_seconds, 3),
            "qty_change_last_interval": qty_change_last_interval,
            "teleport_detected": teleport_detected,
            "state": debug_state,
        }

    def _cleanup_trades(self, state: InstrumentState, ts: datetime) -> None:
        window = timedelta(seconds=self._config.trades_window_seconds)
        while state.trades and (ts - state.trades[0].ts) > window:
            state.trades.popleft()

    def _executed_volume_at_price(self, state: InstrumentState, price: float) -> float:
        return sum(trade.quantity for trade in state.trades if trade.price == price)

    def _cancel_share(self, executed_at_wall: float, size_drop: float) -> float:
        if size_drop <= 0:
            return 0.0
        return 1.0 - min(executed_at_wall, size_drop) / max(size_drop, 1e-9)

    def _should_confirm(
        self,
        wall: ActiveWall,
        dwell_seconds: float,
        executed_at_wall: float,
        cancel_share: float,
        absorption_score: float,
        size_drop: float,
        ts: datetime,
    ) -> bool:
        if wall.reposition_count > self._config.reposition_max:
            return False
        if dwell_seconds < self._config.dwell_seconds:
            return False
        has_cancel_signal = size_drop > 0 and cancel_share <= self._config.cancel_share_max
        if not (
            executed_at_wall >= self._config.Emin
            or has_cancel_signal
            or absorption_score >= self._config.Amin
        ):
            return False
        if wall.last_confirm_alert_ts is None:
            return True
        cooldown = timedelta(seconds=self._config.cooldown_confirmed_seconds)
        return (ts - wall.last_confirm_alert_ts) >= cooldown

    def _should_consuming(
        self,
        wall: ActiveWall,
        ts: datetime,
        executed_at_wall: float,
        cancel_share: float,
    ) -> bool:
        if wall.confirmed_ts is None:
            return False
        if executed_at_wall < self._config.min_exec_confirm and cancel_share > self._config.cancel_share_max:
            return False
        drop_pct = self._consuming_drop_pct(wall, ts)
        if drop_pct < self._config.consuming_drop_pct:
            return False
        if wall.last_consuming_alert_ts is None:
            return True
        cooldown = timedelta(seconds=self._config.cooldown_consuming_seconds)
        return (ts - wall.last_consuming_alert_ts) >= cooldown

    def _consuming_drop_pct(self, wall: ActiveWall, ts: datetime) -> float:
        if not wall.size_history:
            return 0.0
        window = timedelta(seconds=self._config.consuming_window_seconds)
        baseline = None
        for point_ts, size in wall.size_history:
            if (ts - point_ts) <= window:
                baseline = size
                break
        if baseline is None or baseline <= 0:
            return 0.0
        return max((baseline - wall.last_size) / baseline, 0.0)

    def _build_alert(
        self,
        snapshot: OrderBookSnapshot,
        candidate: WallCandidate,
        event: str,
        dwell_seconds: float,
        executed_at_wall: float,
        cancel_share: float,
        reasons: list[str],
    ) -> Alert:
        return Alert(
            instrument_id=snapshot.instrument_id,
            side=candidate.side,
            price=candidate.price,
            event=event,
            size=candidate.size,
            ratio=candidate.ratio,
            v_ref=candidate.v_ref,
            distance_ticks=candidate.distance_ticks,
            dwell_seconds=dwell_seconds,
            executed_at_wall=executed_at_wall,
            cancel_share=cancel_share,
            reasons=reasons,
            ts=snapshot.ts,
        )
