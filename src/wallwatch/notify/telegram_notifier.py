from __future__ import annotations

import asyncio
import contextlib
import json
import html
import logging
import time
from typing import Any, Awaitable, Callable, Iterable
from urllib import error as urllib_error
from urllib.parse import quote

from t_tech.invest import schemas
from urllib import request as urllib_request

from wallwatch.api.client import InstrumentInfo
from wallwatch.app.telegram_client import TelegramApiError, _extract_description
from wallwatch.state.models import Side, WallEvent

_EVENT_TITLES = {
    "wall_candidate": "🟨 WALL CANDIDATE",
    "wall_confirmed": "✅ WALL CONFIRMED",
    "wall_consuming": "🚨 WALL CONSUMING",
    "wall_lost": "⛔ WALL LOST",
}

_TINVEST_BASE_URL = "https://www.tbank.ru"
_SECURITY_SHARE_UTM = "utm_source=security_share"


def build_inline_keyboard(url: str, button_text: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": button_text, "url": url}]]}


def build_tinvest_url(
    instrument: InstrumentInfo | None,
    *,
    append_security_share_utm: bool = False,
) -> str | None:
    if instrument is None:
        return None
    return build_tinvest_url_parts(
        instrument.instrument_type,
        ticker=instrument.ticker,
        isin=instrument.isin,
        append_security_share_utm=append_security_share_utm,
    )


def build_tinvest_url_parts(
    instrument_type: schemas.InstrumentType | None,
    *,
    ticker: str | None,
    isin: str | None,
    append_security_share_utm: bool = False,
) -> str | None:
    path: str | None = None
    identifier = None
    if instrument_type == schemas.InstrumentType.INSTRUMENT_TYPE_SHARE:
        identifier = ticker
        path = "/invest/stocks/{identifier}/"
    elif instrument_type == schemas.InstrumentType.INSTRUMENT_TYPE_ETF:
        identifier = ticker
        path = "/invest/etfs/{identifier}/"
    elif instrument_type == schemas.InstrumentType.INSTRUMENT_TYPE_BOND:
        identifier = isin or ticker
        path = "/invest/bonds/{identifier}/"
    elif instrument_type == schemas.InstrumentType.INSTRUMENT_TYPE_CURRENCY:
        identifier = ticker
        path = "/invest/currencies/{identifier}/"
    elif instrument_type == schemas.InstrumentType.INSTRUMENT_TYPE_FUTURES:
        identifier = ticker
        path = "/invest/futures/{identifier}/"
    elif instrument_type == schemas.InstrumentType.INSTRUMENT_TYPE_INDEX:
        identifier = ticker
        path = "/invest/indexes/{identifier}/"
    if not identifier or not path:
        return None
    encoded = quote(identifier, safe="")
    url = f"{_TINVEST_BASE_URL}{path.format(identifier=encoded)}"
    if append_security_share_utm:
        return f"{url}?{_SECURITY_SHARE_UTM}"
    return url


def format_event_message(event: WallEvent) -> str:
    title = _EVENT_TITLES.get(event.event, event.event.upper())
    distance_ticks = (
        str(event.distance_ticks_to_spread)
        if event.distance_ticks_to_spread is not None
        else "n/a"
    )
    lines = [
        f"<b>{html.escape(title)}</b>",
        f"<b>Symbol:</b> {html.escape(event.symbol)}",
        f"<b>Side:</b> {html.escape(_format_side(event.side))}",
        f"<b>Price:</b> {_format_decimal(event.price)}",
        f"<b>Qty:</b> {_format_decimal(event.qty)}",
        f"<b>Ratio to median:</b> {_format_decimal(event.ratio_to_median, digits=2)}",
        f"<b>Distance to spread:</b> {html.escape(distance_ticks)}",
        f"<b>Dwell:</b> {_format_decimal(event.dwell_seconds, digits=1)}s",
        f"<b>Qty change:</b> {_format_signed(event.qty_change_last_interval)}",
    ]
    return "\n".join(lines)


def _format_decimal(value: float, digits: int = 6) -> str:
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _format_signed(value: float, digits: int = 2) -> str:
    formatted = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    if not formatted.startswith("-"):
        return f"+{formatted}"
    return formatted


def _format_side(side: Side) -> str:
    return side.value if isinstance(side, Side) else str(side)


class TelegramNotifier:
    def __init__(
        self,
        *,
        token: str,
        chat_ids: Iterable[int],
        parse_mode: str,
        disable_web_preview: bool,
        send_events: Iterable[str],
        cooldown_seconds: dict[str, float],
        instrument_by_symbol: dict[str, InstrumentInfo],
        include_instrument_button: bool,
        instrument_button_text: str,
        append_security_share_utm: bool,
        logger: logging.Logger,
        time_provider: Callable[[], float] = time.monotonic,
        queue_maxsize: int = 1000,
        start_worker: bool = True,
        send_func: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._token = token
        self._chat_ids = list(chat_ids)
        self._parse_mode = parse_mode
        self._disable_web_preview = disable_web_preview
        self._send_events = set(send_events)
        self._cooldown_seconds = dict(cooldown_seconds)
        self._instrument_by_symbol = instrument_by_symbol
        self._include_instrument_button = include_instrument_button
        self._instrument_button_text = instrument_button_text
        self._append_security_share_utm = append_security_share_utm
        self._logger = logger
        self._time_provider = time_provider
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_maxsize)
        self._send_func = send_func or self._send_via_http
        self._task = asyncio.create_task(self._worker()) if start_worker else None
        self._last_sent: dict[tuple[str, str], float] = {}
        self._session_state: dict[tuple[str, str], str] = {}

    def update_instruments(self, instrument_by_symbol: dict[str, InstrumentInfo]) -> None:
        self._instrument_by_symbol = instrument_by_symbol

    def notify(self, event: WallEvent) -> None:
        session_key = (event.symbol, event.wall_key)
        if event.event == "wall_confirmed":
            self._session_state[session_key] = "CONFIRMED"
        elif event.event == "wall_lost":
            if self._session_state.get(session_key) != "CONFIRMED":
                self._logger.debug("lost_suppressed_no_confirm", extra=event.to_log_extra())
                return
        elif event.event == "wall_consuming":
            if self._session_state.get(session_key) != "CONFIRMED":
                self._logger.debug(
                    "consuming_suppressed_no_confirm", extra=event.to_log_extra()
                )
                return
        if event.event not in self._send_events:
            return
        if not self._cooldown_allows(event):
            return
        instrument = self._instrument_by_symbol.get(event.symbol)
        instrument_url = build_tinvest_url(
            instrument,
            append_security_share_utm=self._append_security_share_utm,
        )
        text = format_event_message(event)
        payload: dict[str, Any] = {
            "text": text,
            "parse_mode": self._parse_mode,
            "disable_web_page_preview": self._disable_web_preview,
        }
        if instrument_url and self._include_instrument_button:
            payload["reply_markup"] = build_inline_keyboard(
                instrument_url,
                self._instrument_button_text,
            )
        self._enqueue(payload)
        if event.event == "wall_lost":
            self._session_state[session_key] = "LOST"

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def flush(self) -> None:
        await self._queue.join()

    def _enqueue(self, payload: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._logger.warning("telegram_queue_full")

    async def _worker(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                await self._send_payload(payload)
            finally:
                self._queue.task_done()

    async def _send_payload(self, payload: dict[str, Any]) -> None:
        for chat_id in self._chat_ids:
            data = dict(payload)
            data["chat_id"] = chat_id
            try:
                await self._send_func(
                    f"https://api.telegram.org/bot{self._token}/sendMessage", data
                )
            except Exception as exc:  # noqa: BLE001
                description = exc.description if isinstance(exc, TelegramApiError) else None
                extra = {"error": self._redact_token(str(exc))}
                if description:
                    extra["telegram_description"] = description
                self._logger.warning(
                    "telegram_send_failed",
                    extra=extra,
                )

    def _cooldown_allows(self, event: WallEvent) -> bool:
        cooldown = self._cooldown_seconds.get(event.event, 0.0)
        if cooldown <= 0:
            return True
        key = (event.symbol, event.event)
        now = self._time_provider()
        last = self._last_sent.get(key)
        if last is not None and (now - last) < cooldown:
            return False
        self._last_sent[key] = now
        return True

    def _redact_token(self, message: str) -> str:
        return message.replace(self._token, "***") if self._token else message

    async def _send_via_http(self, url: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        req = urllib_request.Request(url, data=data, headers=headers)

        def _do_request() -> None:
            try:
                with urllib_request.urlopen(req, timeout=10) as response:
                    response.read()
            except urllib_error.HTTPError as exc:
                description = _extract_description(exc)
                raise TelegramApiError(f"HTTP {exc.code}", description=description) from exc

        await asyncio.to_thread(_do_request)
