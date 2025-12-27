from __future__ import annotations

import asyncio
import json
import logging
from t_tech.invest import schemas

from wallwatch.api.client import InstrumentInfo
from wallwatch.notify.telegram_notifier import (
    build_inline_keyboard,
    build_tinvest_url,
    build_tinvest_url_parts,
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
        "wall_key": "uid-sber|BUY|120.5",
        "distance_ticks_to_spread": 1,
        "distance_ticks": 2,
        "ratio_to_median": 12.3,
        "dwell_seconds": 3.2,
        "qty_change_last_interval": -50.0,
    }
    data.update(overrides)
    return WallEvent(**data)


def test_build_tinvest_url_parts() -> None:
    assert (
        build_tinvest_url_parts(
            schemas.InstrumentType.INSTRUMENT_TYPE_SHARE,
            ticker="SBER",
            isin=None,
        )
        == "https://www.tbank.ru/invest/stocks/SBER/"
    )
    assert (
        build_tinvest_url_parts(
            schemas.InstrumentType.INSTRUMENT_TYPE_BOND,
            ticker=None,
            isin="RU000A0JX0J2",
        )
        == "https://www.tbank.ru/invest/bonds/RU000A0JX0J2/"
    )
    assert (
        build_tinvest_url_parts(
            schemas.InstrumentType.INSTRUMENT_TYPE_ETF,
            ticker="TST@ETF",
            isin=None,
        )
        == "https://www.tbank.ru/invest/etfs/TST%40ETF/"
    )
    assert (
        build_tinvest_url_parts(
            schemas.InstrumentType.INSTRUMENT_TYPE_FUTURES,
            ticker="SiZ3",
            isin=None,
        )
        == "https://www.tbank.ru/invest/futures/SiZ3/"
    )
    assert (
        build_tinvest_url_parts(
            schemas.InstrumentType.INSTRUMENT_TYPE_CURRENCY,
            ticker="USD000UTSTOM",
            isin=None,
        )
        == "https://www.tbank.ru/invest/currencies/USD000UTSTOM/"
    )


def test_build_tinvest_url() -> None:
    assert (
        build_tinvest_url(
            "SBER",
            InstrumentInfo(
                instrument_id="uid-1",
                symbol="SBER",
                tick_size=0.01,
                instrument_type=schemas.InstrumentType.INSTRUMENT_TYPE_SHARE,
                ticker="SBER",
            )
        )
        == "https://www.tbank.ru/invest/stocks/SBER/"
    )
    assert (
        build_tinvest_url(
            "FXRL",
            InstrumentInfo(
                instrument_id="uid-2",
                symbol="FXRL",
                tick_size=0.01,
                instrument_type=schemas.InstrumentType.INSTRUMENT_TYPE_ETF,
                ticker="FXRL",
            )
        )
        == "https://www.tbank.ru/invest/etfs/FXRL/"
    )
    assert (
        build_tinvest_url(
            "RU000A107U81",
            InstrumentInfo(
                instrument_id="uid-3",
                symbol="RU000A107U81",
                tick_size=0.01,
                instrument_type=schemas.InstrumentType.INSTRUMENT_TYPE_BOND,
                isin="RU000A107U81",
            )
        )
        == "https://www.tbank.ru/invest/bonds/RU000A107U81/"
    )
    assert (
        build_tinvest_url(
            "USDRUB",
            InstrumentInfo(
                instrument_id="uid-4",
                symbol="USDRUB",
                tick_size=0.01,
                instrument_type=schemas.InstrumentType.INSTRUMENT_TYPE_CURRENCY,
                ticker="USDRUB",
            )
        )
        == "https://www.tbank.ru/invest/currencies/USDRUB/"
    )
    assert (
        build_tinvest_url(
            "USDRUBF",
            InstrumentInfo(
                instrument_id="uid-5",
                symbol="USDRUBF",
                tick_size=0.01,
                instrument_type=schemas.InstrumentType.INSTRUMENT_TYPE_FUTURES,
                ticker="USDRUBF",
            )
        )
        == "https://www.tbank.ru/invest/futures/USDRUBF/"
    )


def test_build_tinvest_url_fallback() -> None:
    assert (
        build_tinvest_url("VSEH", None) == "https://www.tbank.ru/invest/stocks/VSEH/"
    )
    assert (
        build_tinvest_url("RU0009029540", None)
        == "https://www.tbank.ru/invest/bonds/RU0009029540/"
    )


def test_format_event_message_contains_fields() -> None:
    message = format_event_message(_event())
    assert "WALL CONFIRMED" in message
    assert "Symbol" in message
    assert "Side" in message
    assert "Price" in message
    assert "Qty" in message
    assert "Ratio to median" in message
    assert "Distance to spread" in message
    assert "Dwell" in message
    assert "Qty change" in message


def test_build_inline_keyboard() -> None:
    url = "https://www.tbank.ru/invest/stocks/SBER/"
    keyboard = build_inline_keyboard(url, "Открыть в Т-Инвестициях")
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
        include_instrument_button=True,
        instrument_button_text="Открыть в Т-Инвестициях",
        append_security_share_utm=False,
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
        notifier = TelegramNotifier(
            token="token",
            chat_ids=[123],
            parse_mode="HTML",
            disable_web_preview=True,
            send_events=["wall_confirmed"],
            cooldown_seconds={"wall_confirmed": 0.0},
            instrument_by_symbol={},
            include_instrument_button=True,
            instrument_button_text="Открыть в Т-Инвестициях",
            append_security_share_utm=False,
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
    inline_keyboard = payload["reply_markup"]["inline_keyboard"]
    assert isinstance(inline_keyboard, list)
    assert all(isinstance(row, list) for row in inline_keyboard)
    assert inline_keyboard[0][0]["url"].endswith("/SBER/")


def test_lost_sent_once_per_confirm() -> None:
    notifier = TelegramNotifier(
        token="token",
        chat_ids=[1],
        parse_mode="HTML",
        disable_web_preview=True,
        send_events=["wall_confirmed", "wall_lost"],
        cooldown_seconds={},
        instrument_by_symbol={},
        include_instrument_button=False,
        instrument_button_text="",
        append_security_share_utm=False,
        logger=logging.getLogger("test"),
        start_worker=False,
        send_func=lambda *_: asyncio.sleep(0),
    )

    notifier.notify(_event(event="wall_confirmed"))
    notifier.notify(_event(event="wall_lost"))
    notifier.notify(_event(event="wall_lost"))
    assert notifier._queue.qsize() == 2

    notifier.notify(_event(event="wall_confirmed"))
    notifier.notify(_event(event="wall_lost"))
    assert notifier._queue.qsize() == 4

    asyncio.run(notifier.aclose())


def test_lost_dedup_is_per_instrument() -> None:
    notifier = TelegramNotifier(
        token="token",
        chat_ids=[1],
        parse_mode="HTML",
        disable_web_preview=True,
        send_events=["wall_lost"],
        cooldown_seconds={},
        instrument_by_symbol={},
        include_instrument_button=False,
        instrument_button_text="",
        append_security_share_utm=False,
        logger=logging.getLogger("test"),
        start_worker=False,
        send_func=lambda *_: asyncio.sleep(0),
    )

    notifier.notify(
        _event(
            event="wall_confirmed",
            symbol="SBER",
            wall_key="uid-sber|BUY|120.5",
            price=120.5,
        )
    )
    notifier.notify(
        _event(
            event="wall_lost",
            symbol="SBER",
            wall_key="uid-sber|BUY|120.5",
            price=120.5,
        )
    )
    notifier.notify(
        _event(
            event="wall_confirmed",
            symbol="GAZP",
            wall_key="uid-gazp|SELL|210.0",
            side=Side.SELL,
            price=210.0,
        )
    )
    notifier.notify(
        _event(
            event="wall_lost",
            symbol="GAZP",
            wall_key="uid-gazp|SELL|210.0",
            side=Side.SELL,
            price=210.0,
        )
    )
    assert notifier._queue.qsize() == 2

    asyncio.run(notifier.aclose())


def test_lost_dedup_is_per_wall() -> None:
    notifier = TelegramNotifier(
        token="token",
        chat_ids=[1],
        parse_mode="HTML",
        disable_web_preview=True,
        send_events=["wall_lost"],
        cooldown_seconds={},
        instrument_by_symbol={},
        include_instrument_button=False,
        instrument_button_text="",
        append_security_share_utm=False,
        logger=logging.getLogger("test"),
        start_worker=False,
        send_func=lambda *_: asyncio.sleep(0),
    )

    notifier.notify(
        _event(
            event="wall_confirmed",
            wall_key="uid-sber|BUY|120.5",
            price=120.5,
        )
    )
    notifier.notify(
        _event(
            event="wall_lost",
            wall_key="uid-sber|BUY|120.5",
            price=120.5,
        )
    )
    notifier.notify(
        _event(
            event="wall_confirmed",
            wall_key="uid-sber|BUY|121.0",
            price=121.0,
        )
    )
    notifier.notify(
        _event(
            event="wall_lost",
            wall_key="uid-sber|BUY|121.0",
            price=121.0,
        )
    )
    assert notifier._queue.qsize() == 2

    asyncio.run(notifier.aclose())


def test_repeat_lost_is_suppressed_per_wall() -> None:
    notifier = TelegramNotifier(
        token="token",
        chat_ids=[1],
        parse_mode="HTML",
        disable_web_preview=True,
        send_events=["wall_lost"],
        cooldown_seconds={},
        instrument_by_symbol={},
        include_instrument_button=False,
        instrument_button_text="",
        append_security_share_utm=False,
        logger=logging.getLogger("test"),
        start_worker=False,
        send_func=lambda *_: asyncio.sleep(0),
    )

    notifier.notify(
        _event(
            event="wall_confirmed",
            wall_key="uid-sber|BUY|120.5",
            price=120.5,
        )
    )
    notifier.notify(
        _event(
            event="wall_lost",
            wall_key="uid-sber|BUY|120.5",
            price=120.5,
        )
    )
    notifier.notify(
        _event(
            event="wall_lost",
            wall_key="uid-sber|BUY|120.5",
            price=120.5,
        )
    )
    assert notifier._queue.qsize() == 1

    asyncio.run(notifier.aclose())


def test_consuming_requires_confirm() -> None:
    notifier = TelegramNotifier(
        token="token",
        chat_ids=[1],
        parse_mode="HTML",
        disable_web_preview=True,
        send_events=["wall_confirmed", "wall_consuming"],
        cooldown_seconds={},
        instrument_by_symbol={},
        include_instrument_button=False,
        instrument_button_text="",
        append_security_share_utm=False,
        logger=logging.getLogger("test"),
        start_worker=False,
        send_func=lambda *_: asyncio.sleep(0),
    )

    notifier.notify(_event(event="wall_consuming"))
    assert notifier._queue.qsize() == 0

    notifier.notify(_event(event="wall_confirmed"))
    notifier.notify(_event(event="wall_consuming"))
    assert notifier._queue.qsize() == 2

    asyncio.run(notifier.aclose())
