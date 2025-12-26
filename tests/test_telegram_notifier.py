from __future__ import annotations

import asyncio
import json
import logging
from t_tech.invest import schemas

from wallwatch.api.client import InstrumentInfo
from wallwatch.notify.telegram_notifier import (
    build_inline_keyboard,
    build_instrument_url_parts,
    format_event_message,
    TelegramNotifier,
)
from wallwatch.state.models import Side, WallEvent


def _event(**overrides: object) -> WallEvent:
    data = {
        "event": "wall_confirmed",
        "symbol": "SBER",
        "side": Side.BUY,
        "price": 120.5,
        "qty": 1000.0,
        "wall_key": "SBER|BUY|120.5",
        "distance_ticks_to_spread": 1,
        "distance_ticks": 2,
        "ratio_to_median": 12.3,
        "dwell_seconds": 3.2,
        "qty_change_last_interval": -50.0,
    }
    data.update(overrides)
    return WallEvent(**data)


def test_build_instrument_url_parts() -> None:
    assert (
        build_instrument_url_parts(
            schemas.InstrumentType.INSTRUMENT_TYPE_SHARE,
            ticker="SBER",
            isin=None,
        )
        == "https://www.tbank.ru/invest/stocks/SBER/"
    )
    assert (
        build_instrument_url_parts(
            schemas.InstrumentType.INSTRUMENT_TYPE_BOND,
            ticker=None,
            isin="RU000A0JX0J2",
        )
        == "https://www.tbank.ru/invest/bonds/RU000A0JX0J2/"
    )
    assert (
        build_instrument_url_parts(
            schemas.InstrumentType.INSTRUMENT_TYPE_ETF,
            ticker="TST@ETF",
            isin=None,
        )
        == "https://www.tbank.ru/invest/etfs/TST%40ETF/"
    )
    assert (
        build_instrument_url_parts(
            schemas.InstrumentType.INSTRUMENT_TYPE_FUTURES,
            ticker="SiZ3",
            isin=None,
        )
        == "https://www.tbank.ru/invest/futures/SiZ3/"
    )
    assert (
        build_instrument_url_parts(
            schemas.InstrumentType.INSTRUMENT_TYPE_CURRENCY,
            ticker="USD000UTSTOM",
            isin=None,
        )
        == "https://www.tbank.ru/invest/currencies/USD000UTSTOM/"
    )


def test_format_event_message_contains_fields_and_link() -> None:
    url = "https://www.tbank.ru/invest/stocks/SBER/"
    message = format_event_message(_event(), url)
    assert "WALL CONFIRMED" in message
    assert "Symbol" in message
    assert "Side" in message
    assert "Price" in message
    assert "Qty" in message
    assert "Ratio to median" in message
    assert "Distance to spread" in message
    assert "Dwell" in message
    assert "Qty change" in message
    assert url in message


def test_build_inline_keyboard() -> None:
    url = "https://www.tbank.ru/invest/stocks/SBER/"
    keyboard = build_inline_keyboard(url)
    assert keyboard["inline_keyboard"] == [[{"text": "Открыть в Т-Инвестициях", "url": url}]]


def test_cooldown_prevents_duplicate_events() -> None:
    current_time = 0.0

    def _time() -> float:
        return current_time

    notifier = TelegramNotifier(
        token="token",
        chat_ids=[1],
        parse_mode="HTML",
        disable_web_preview=True,
        send_events=["wall_confirmed"],
        cooldown_seconds={"wall_confirmed": 60.0},
        instrument_by_symbol={},
        logger=logging.getLogger("test"),
        time_provider=_time,
        start_worker=False,
        send_func=lambda *_: asyncio.sleep(0),
    )

    notifier.notify(_event())
    notifier.notify(_event())
    assert notifier._queue.qsize() == 1

    current_time = 61.0
    notifier.notify(_event())
    assert notifier._queue.qsize() == 2

    asyncio.run(notifier.aclose())


def test_send_message_payload() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    async def _send(url: str, payload: dict[str, object]) -> None:
        requests.append((url, payload))

    async def _run() -> None:
        instrument = InstrumentInfo(
            instrument_id="uid-123",
            symbol="SBER",
            tick_size=0.01,
            instrument_type=schemas.InstrumentType.INSTRUMENT_TYPE_SHARE,
            ticker="SBER",
        )
        notifier = TelegramNotifier(
            token="token",
            chat_ids=[123],
            parse_mode="HTML",
            disable_web_preview=True,
            send_events=["wall_confirmed"],
            cooldown_seconds={"wall_confirmed": 0.0},
            instrument_by_symbol={"SBER": instrument},
            logger=logging.getLogger("test"),
            send_func=_send,
        )
        notifier.notify(_event())
        await notifier.flush()
        await notifier.aclose()

    asyncio.run(_run())

    assert len(requests) == 1
    payload = json.loads(json.dumps(requests[0][1]))
    assert payload["chat_id"] == 123
    assert payload["parse_mode"] == "HTML"
    assert payload["disable_web_page_preview"] is True
    assert "reply_markup" in payload
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("/SBER/")
