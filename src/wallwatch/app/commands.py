from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import html
import logging
from typing import Any, Callable, Iterable

from wallwatch.app.market_data_manager import MarketDataManager
from wallwatch.app.runtime_state import RuntimeState, RuntimeStateSnapshot, WallEventState
from wallwatch.notify.telegram_notifier import (
    build_inline_keyboard,
    build_tinvest_url,
    format_event_message,
)
from wallwatch.state.models import Side, WallEvent


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    args: list[str]


@dataclass(frozen=True)
class TelegramMessage:
    text: str
    reply_markup: dict[str, Any] | None = None


@dataclass(frozen=True)
class CommandResponse:
    text: str | None
    messages: list[TelegramMessage] = field(default_factory=list)


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


def html_escape(value: object) -> str:
    return html.escape(str(value))


def format_code(text: str) -> str:
    return f"<code>{html_escape(text)}</code>"


def format_ping_response(snapshot: RuntimeStateSnapshot, now: datetime) -> str:
    timestamp = html_escape(now.isoformat(timespec="seconds"))
    uptime = html_escape(format_uptime(snapshot.started_at, now))
    since_last = html_escape(_format_since_last(snapshot))
    return (
        f"pong {timestamp} uptime={uptime} stream_state={html_escape(snapshot.stream_state)} "
        f"rx_total_orderbooks={html_escape(snapshot.rx_total_orderbooks)} "
        f"rx_total_trades={html_escape(snapshot.rx_total_trades)} "
        f"since_last_message_seconds={since_last}"
    )


def _format_last_wall_event(event: WallEventState | None) -> str:
    if event is None:
        return "none"
    ts = html_escape(event.ts.isoformat(timespec="seconds"))
    return (
        f"{html_escape(event.event_type)} {html_escape(event.symbol)} "
        f"{html_escape(event.side)} {html_escape(event.price)} "
        f"{html_escape(event.qty)} @ {ts}"
    )


def format_status_response(snapshot: RuntimeStateSnapshot) -> str:
    symbols_text = ", ".join(snapshot.current_symbols) if snapshot.current_symbols else "none"
    symbols_text = html_escape(symbols_text)
    since_last = html_escape(_format_since_last(snapshot))
    lines = [
        f"state={html_escape(snapshot.stream_state)}",
        f"since_last_message={since_last}",
        f"rx_total_orderbooks={html_escape(snapshot.rx_total_orderbooks)}",
        f"rx_total_trades={html_escape(snapshot.rx_total_trades)}",
        f"symbols={symbols_text}",
        f"depth={html_escape(snapshot.depth)}",
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
        include_instrument_button: bool,
        instrument_button_text: str,
        append_security_share_utm: bool,
        logger: logging.Logger,
        time_provider: Callable[[timezone], datetime] = datetime.now,
    ) -> None:
        self._runtime_state = runtime_state
        self._manager = manager
        self._max_symbols = max_symbols
        self._allowed_user_ids = allowed_user_ids
        self._include_instrument_button = include_instrument_button
        self._instrument_button_text = instrument_button_text
        self._append_security_share_utm = append_security_share_utm
        self._logger = logger
        self._time_provider = time_provider

    async def handle_command(
        self, text: str, *, chat_id: int, user_id: int | None
    ) -> CommandResponse | None:
        parsed = parse_command(text)
        if parsed is None:
            return None
        if self._allowed_user_ids and (user_id is None or user_id not in self._allowed_user_ids):
            self._logger.info(
                "telegram_not_allowed",
                extra={"chat_id": chat_id, "user_id": user_id, "command": parsed.name},
            )
            return CommandResponse(text="not allowed")
        response = await self._handle_allowed_command(parsed)
        self._logger.info(
            "telegram_command_handled",
            extra={"chat_id": chat_id, "user_id": user_id, "command": parsed.name},
        )
        return response

    async def _handle_allowed_command(self, parsed: ParsedCommand) -> CommandResponse:
        if parsed.name == "start":
            return CommandResponse(text=self._start_text())
        if parsed.name == "help":
            return CommandResponse(text=self._help_text())
        if parsed.name == "ping":
            snapshot = await self._runtime_state.snapshot()
            now = self._time_provider(timezone.utc)
            return CommandResponse(text=format_ping_response(snapshot, now))
        if parsed.name == "status":
            snapshot = await self._runtime_state.snapshot()
            return CommandResponse(text=format_status_response(snapshot))
        if parsed.name == "list":
            symbols = await self._manager.get_symbols()
            symbols_text = ", ".join(symbols) if symbols else "none"
            return CommandResponse(text=f"symbols={html_escape(symbols_text)}")
        if parsed.name == "watch":
            if not parsed.args:
                return CommandResponse(text=f"Usage: {format_code('/watch <symbols>')}")
            symbols = parse_symbols(parsed.args)
            if not symbols:
                return CommandResponse(text=f"Usage: {format_code('/watch <symbols>')}")
            if len(symbols) > self._max_symbols:
                return CommandResponse(
                    text=f"Too many symbols (max {html_escape(self._max_symbols)})."
                )
            await self._manager.update_symbols(symbols)
            return CommandResponse(text=f"watching: {html_escape(', '.join(symbols))}")
        if parsed.name == "unwatch":
            if not parsed.args:
                return CommandResponse(text=f"Usage: {format_code('/unwatch <symbols>')}")
            symbols = parse_symbols(parsed.args)
            if not symbols:
                return CommandResponse(text=f"Usage: {format_code('/unwatch <symbols>')}")
            current = await self._manager.get_symbols()
            remaining = [symbol for symbol in current if symbol not in symbols]
            await self._manager.update_symbols(remaining)
            removed = [symbol for symbol in symbols if symbol in current]
            if not removed:
                return CommandResponse(text="no matching symbols to remove")
            if not remaining:
                return CommandResponse(text=f"removed: {html_escape(', '.join(removed))} (idle)")
            return CommandResponse(text=f"removed: {html_escape(', '.join(removed))}")
        if parsed.name == "smoke":
            smoke = self._build_smoke_message()
            return CommandResponse(text=None, messages=[smoke])
        return CommandResponse(text="Unknown command. Use /help.")

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
            f"/watch - установить список (до 10), например {format_code('/watch SBER GAZP')}\n"
            f"/unwatch - убрать символы, например {format_code('/unwatch SBER GAZP')}\n"
            "/list - показать текущие symbols\n"
            "/smoke - тестовое прод-уведомление"
        )

    def _build_smoke_message(self) -> TelegramMessage:
        event = WallEvent(
            event="wall_confirmed",
            symbol="VSEH",
            side=Side.BUY,
            price=123.45,
            qty=6789.0,
            wall_key="smoke|BUY|123.45",
            distance_ticks_to_spread=1,
            distance_ticks=2,
            ratio_to_median=7.5,
            dwell_seconds=3.4,
            qty_change_last_interval=120.0,
        )
        text = format_event_message(event)
        reply_markup = None
        if self._include_instrument_button:
            instrument_url = build_tinvest_url(
                event.symbol,
                None,
                append_security_share_utm=self._append_security_share_utm,
            )
            if instrument_url:
                reply_markup = build_inline_keyboard(
                    instrument_url,
                    self._instrument_button_text,
                )
        return TelegramMessage(text=text, reply_markup=reply_markup)
