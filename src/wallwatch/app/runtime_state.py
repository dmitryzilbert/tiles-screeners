from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class WallEventState:
    event_type: str
    ts: datetime
    symbol: str
    side: str
    price: float
    qty: float


@dataclass(frozen=True)
class RuntimeStateSnapshot:
    started_at: datetime
    pid: int
    stream_state: str
    since_last_message_seconds: float | None
    rx_total_orderbooks: int
    rx_total_trades: int
    current_symbols: list[str]
    depth: int
    last_wall_event: WallEventState | None
    last_error: str | None


@dataclass
class RuntimeState:
    started_at: datetime
    pid: int
    stream_state: str = "connecting"
    since_last_message_seconds: float | None = None
    rx_total_orderbooks: int = 0
    rx_total_trades: int = 0
    current_symbols: list[str] = field(default_factory=list)
    depth: int = 0
    last_wall_event: WallEventState | None = None
    last_error: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def update(self, **changes: Any) -> None:
        async with self._lock:
            for key, value in changes.items():
                if hasattr(self, key):
                    setattr(self, key, value)

    def update_sync(self, **changes: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.update(**changes))

    async def snapshot(self) -> RuntimeStateSnapshot:
        async with self._lock:
            return RuntimeStateSnapshot(
                started_at=self.started_at,
                pid=self.pid,
                stream_state=self.stream_state,
                since_last_message_seconds=self.since_last_message_seconds,
                rx_total_orderbooks=self.rx_total_orderbooks,
                rx_total_trades=self.rx_total_trades,
                current_symbols=list(self.current_symbols),
                depth=self.depth,
                last_wall_event=self.last_wall_event,
                last_error=self.last_error,
            )
