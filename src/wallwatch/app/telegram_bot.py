from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any, Callable, Iterable, Protocol
from urllib import request as urllib_request

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


def format_ping_response(snapshot: RuntimeStateSnapshot, now: datetime) -> str:
    timestamp = now.isoformat(timespec="seconds")
    uptime = format_uptime(snapshot.started_at, now)
    return f"pong {timestamp} uptime={uptime} state={snapshot.stream_state}"


def _format_last_wall_event(event: WallEventState | None) -> str:
    if event is None:
        return "none"
    ts = event.ts.isoformat(timespec="seconds")
    return f"{event.event_type} {event.symbol} {event.side} {event.price} {event.qty} @ {ts}"


def format_status_response(snapshot: RuntimeStateSnapshot) -> str:
    symbols_text = ", ".join(snapshot.current_symbols) if snapshot.current_symbols else "none"
    since_last = (
        "n/a"
        if snapshot.since_last_message_seconds is None
        else f"{snapshot.since_last_message_seconds:.3f}s"
    )
    lines = [
        f"state={snapshot.stream_state}",
        f"since_last_message={since_last}",
        f"rx_total_orderbooks={snapshot.rx_total_orderbooks}",
        f"rx_total_trades={snapshot.rx_total_trades}",
        f"symbols={symbols_text}",
        f"depth={snapshot.depth}",
        f"last_wall_event={_format_last_wall_event(snapshot.last_wall_event)}",
    ]
    return "\n".join(lines)


class TelegramHttpClient(Protocol):
    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class UrllibTelegramHttpClient:
    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        req = urllib_request.Request(url, data=data, headers=headers)

        def _do_request() -> dict[str, Any]:
            with urllib_request.urlopen(req, timeout=30) as response:
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}")
                payload_data = response.read()
                return json.loads(payload_data.decode("utf-8"))

        return await asyncio.to_thread(_do_request)


class TelegramApiClient:
    def __init__(self, token: str, http_client: TelegramHttpClient, logger: logging.Logger) -> None:
        self._token = token
        self._client = http_client
        self._logger = logger

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        payload = await self._client.post_json(self._url("/getUpdates"), params)
        if not payload.get("ok"):
            raise RuntimeError("telegram getUpdates failed")
        return payload.get("result", [])

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        disable_web_preview: bool = True,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        await self._client.post_json(self._url("/sendMessage"), payload)

    def _url(self, path: str) -> str:
        return f"https://api.telegram.org/bot{self._token}{path}"


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

    async def handle_command(
        self, text: str, *, chat_id: int, user_id: int | None
    ) -> str | None:
        parsed = parse_command(text)
        if parsed is None:
            return None
        if self._allowed_user_ids and (user_id is None or user_id not in self._allowed_user_ids):
            return "not allowed"
        if parsed.name in {"start", "help"}:
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
            return f"symbols={symbols_text}"
        if parsed.name == "watch":
            if not parsed.args:
                return "Usage: /watch <symbols>"
            symbols = parse_symbols(parsed.args)
            if not symbols:
                return "Usage: /watch <symbols>"
            if len(symbols) > self._max_symbols:
                return f"Too many symbols (max {self._max_symbols})."
            await self._manager.update_symbols(symbols)
            return f"watching: {', '.join(symbols)}"
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
            return f"removed: {', '.join(removed)}"
        self._logger.info("telegram_unknown_command", extra={"command": parsed.name, "chat_id": chat_id})
        return "Unknown command. Use /help."

    def _help_text(self) -> str:
        return (
            "WallWatch Telegram commands:\n"
            "/start - help\n"
            "/help - list commands\n"
            "/ping - health check\n"
            "/status - stream status\n"
            "/watch <symbols> - set symbols (up to 10)\n"
            "/unwatch <symbols> - remove symbols\n"
            "/list - list current symbols"
        )


class TelegramPolling:
    def __init__(
        self,
        *,
        api: TelegramApiClient,
        command_handler: TelegramCommandHandler,
        logger: logging.Logger,
        parse_mode: str | None,
        disable_web_preview: bool,
        poll_interval_seconds: float,
    ) -> None:
        self._api = api
        self._command_handler = command_handler
        self._logger = logger
        self._parse_mode = parse_mode
        self._disable_web_preview = disable_web_preview
        self._poll_interval_seconds = poll_interval_seconds
        self._offset: int | None = None
        self._last_command_chat_id: int | None = None

    @property
    def last_command_chat_id(self) -> int | None:
        return self._last_command_chat_id

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                updates = await self._api.get_updates(self._offset, timeout=30)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("telegram_poll_failed", extra={"error": str(exc)})
                await asyncio.sleep(self._poll_interval_seconds)
                continue

            for update in updates:
                update_id = update.get("update_id")
                if update_id is not None:
                    self._offset = update_id + 1
                message = update.get("message") or {}
                text = message.get("text")
                if not text:
                    continue
                chat = message.get("chat") or {}
                from_user = message.get("from") or {}
                chat_id = chat.get("id")
                if chat_id is None:
                    continue
                user_id = from_user.get("id")
                response = await self._command_handler.handle_command(
                    text, chat_id=chat_id, user_id=user_id
                )
                if response:
                    self._last_command_chat_id = chat_id
                    await self._send_response(chat_id, response)

            if not updates:
                await asyncio.sleep(self._poll_interval_seconds)

    async def send_startup_message(self, chat_ids: Iterable[int], text: str) -> None:
        for chat_id in chat_ids:
            await self._send_response(chat_id, text)

    async def _send_response(self, chat_id: int, text: str) -> None:
        try:
            await self._api.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=self._parse_mode,
                disable_web_preview=self._disable_web_preview,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("telegram_send_failed", extra={"error": str(exc)})
