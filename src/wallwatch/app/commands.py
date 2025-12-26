from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import html
import logging
from typing import Callable, Iterable

from wallwatch.app.market_data_manager import MarketDataManager
from wallwatch.app.runtime_state import RuntimeState, RuntimeStateSnapshot, WallEventState


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    args: list[str]


def parse_command(text: str) -> ParsedCommand | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    if not parts:
        return None
    command = parts[0][1:]
    if "@" in command:
        command = command.split("@", 1)[0]
    if not command:
        return None
    return ParsedCommand(name=command.lower(), args=parts[1:])


def parse_symbols(args: Iterable[str]) -> list[str]:
    symbols: list[str] = []
    for arg in args:
        for item in arg.split(","):
            cleaned = item.strip().upper()
            if cleaned:
                symbols.append(cleaned)
    return list(dict.fromkeys(symbols))


def format_uptime(started_at: datetime, now: datetime) -> str:
    delta = now - started_at
    minutes, _ = divmod(int(delta.total_seconds()), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes}m"


def _format_since_last(snapshot: RuntimeStateSnapshot) -> str:
    if snapshot.since_last_message_seconds is None:
        return "n/a"
    return f"{snapshot.since_last_message_seconds:.3f}s"


def format_ping_response(snapshot: RuntimeStateSnapshot, now: datetime) -> str:
    timestamp = html.escape(now.isoformat(timespec="seconds"))
    uptime = html.escape(format_uptime(snapshot.started_at, now))
    since_last = html.escape(_format_since_last(snapshot))
    return (
        f"pong {timestamp} uptime={uptime} stream_state={html.escape(str(snapshot.stream_state))} "
        f"rx_total_orderbooks={html.escape(str(snapshot.rx_total_orderbooks))} "
        f"rx_total_trades={html.escape(str(snapshot.rx_total_trades))} "
        f"since_last_message_seconds={since_last}"
    )


def _format_last_wall_event(event: WallEventState | None) -> str:
    if event is None:
        return "none"
    ts = html.escape(event.ts.isoformat(timespec="seconds"))
    return (
        f"{html.escape(str(event.event_type))} {html.escape(str(event.symbol))} "
        f"{html.escape(str(event.side))} {html.escape(str(event.price))} "
        f"{html.escape(str(event.qty))} @ {ts}"
    )


def format_status_response(snapshot: RuntimeStateSnapshot) -> str:
    symbols_text = ", ".join(snapshot.current_symbols) if snapshot.current_symbols else "none"
    symbols_text = html.escape(symbols_text)
    since_last = html.escape(_format_since_last(snapshot))
    lines = [
        f"state={html.escape(str(snapshot.stream_state))}",
        f"since_last_message={since_last}",
        f"rx_total_orderbooks={html.escape(str(snapshot.rx_total_orderbooks))}",
        f"rx_total_trades={html.escape(str(snapshot.rx_total_trades))}",
        f"symbols={symbols_text}",
        f"depth={html.escape(str(snapshot.depth))}",
        f"last_wall_event={_format_last_wall_event(snapshot.last_wall_event)}",
    ]
    return "\n".join(lines)


class TelegramCommandHandler:
    def __init__(
        self,
        *,
        runtime_state: RuntimeState,
        manager: MarketDataManager,
        max_symbols: int,
        allowed_user_ids: set[int],
        logger: logging.Logger,
        time_provider: Callable[[timezone], datetime] = datetime.now,
    ) -> None:
        self._runtime_state = runtime_state
        self._manager = manager
        self._max_symbols = max_symbols
        self._allowed_user_ids = allowed_user_ids
        self._logger = logger
        self._time_provider = time_provider

    async def handle_command(self, text: str, *, chat_id: int, user_id: int | None) -> str | None:
        parsed = parse_command(text)
        if parsed is None:
            return None
        if self._allowed_user_ids and (user_id is None or user_id not in self._allowed_user_ids):
            self._logger.info(
                "telegram_not_allowed",
                extra={"chat_id": chat_id, "user_id": user_id, "command": parsed.name},
            )
            return "not allowed"
        response = await self._handle_allowed_command(parsed)
        self._logger.info(
            "telegram_command_handled",
            extra={"chat_id": chat_id, "user_id": user_id, "command": parsed.name},
        )
        return response

    async def _handle_allowed_command(self, parsed: ParsedCommand) -> str:
        if parsed.name == "start":
            return self._start_text()
        if parsed.name == "help":
            return self._help_text()
        if parsed.name == "ping":
            snapshot = await self._runtime_state.snapshot()
            now = self._time_provider(timezone.utc)
            return format_ping_response(snapshot, now)
        if parsed.name == "status":
            snapshot = await self._runtime_state.snapshot()
            return format_status_response(snapshot)
        if parsed.name == "list":
            symbols = await self._manager.get_symbols()
            symbols_text = ", ".join(symbols) if symbols else "none"
            return f"symbols={html.escape(symbols_text)}"
        if parsed.name == "watch":
            if not parsed.args:
                return "Usage: /watch <symbols>"
            symbols = parse_symbols(parsed.args)
            if not symbols:
                return "Usage: /watch <symbols>"
            if len(symbols) > self._max_symbols:
                return f"Too many symbols (max {html.escape(str(self._max_symbols))})."
            await self._manager.update_symbols(symbols)
            return f"watching: {html.escape(', '.join(symbols))}"
        if parsed.name == "unwatch":
            if not parsed.args:
                return "Usage: /unwatch <symbols>"
            symbols = parse_symbols(parsed.args)
            if not symbols:
                return "Usage: /unwatch <symbols>"
            current = await self._manager.get_symbols()
            remaining = [symbol for symbol in current if symbol not in symbols]
            await self._manager.update_symbols(remaining)
            removed = [symbol for symbol in symbols if symbol in current]
            if not removed:
                return "no matching symbols to remove"
            if not remaining:
                return f"removed: {html.escape(', '.join(removed))} (idle)"
            return f"removed: {html.escape(', '.join(removed))}"
        return "Unknown command. Use /help."

    def _start_text(self) -> str:
        return (
            "Привет! Я WallWatch бот.\n"
            "Я слежу за стенками в стакане и состоянием стрима.\n\n"
            + self._help_text()
        )

    def _help_text(self) -> str:
        return (
            "Доступные команды:\n"
            "/start - приветствие и помощь\n"
            "/help - список команд\n"
            "/ping - health check\n"
            "/status - текущий статус стрима\n"
            "/watch <symbols> - установить список (до 10)\n"
            "/unwatch <symbols> - убрать символы\n"
            "/list - показать текущие symbols"
        )
