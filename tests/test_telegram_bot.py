from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from urllib import request as urllib_request

from wallwatch.app.commands import (
    CommandResponse,
    TelegramCommandHandler,
    format_ping_response,
    format_status_response,
)
from wallwatch.app.runtime_state import RuntimeState, RuntimeStateSnapshot, WallEventState
from wallwatch.app.telegram_client import TelegramApiClient, UrllibTelegramHttpClient
from wallwatch.app.telegram_polling import TelegramPolling
from wallwatch.notify.telegram_notifier import TelegramNotifier


class FakeManager:
    def __init__(self, symbols: list[str] | None = None) -> None:
        self.symbols = symbols or []
        self.updated: list[str] | None = None

    async def update_symbols(self, symbols: list[str]) -> None:
        self.updated = list(symbols)
        self.symbols = list(symbols)

    async def get_symbols(self) -> list[str]:
        return list(self.symbols)


class FakeHttpClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        if url.endswith("/getUpdates"):
            self.requests.append({"type": "getUpdates", "payload": payload})
            return {"ok": True, "result": [{"update_id": 10, "message": {"text": "/ping"}}]}
        if url.endswith("/sendMessage"):
            self.requests.append({"type": "sendMessage", "payload": payload})
            return {"ok": True, "result": {"message_id": 1}}
        return {"ok": False}


def test_telegram_api_client_mock_transport() -> None:
    async def _run() -> None:
        client = FakeHttpClient()
        api = TelegramApiClient("token", client, logger=logging.getLogger("test"))
        updates = await api.get_updates(offset=1, timeout=30)
        assert updates[0]["update_id"] == 10
        await api.send_message(
            chat_id=1,
            text="hello",
            parse_mode="HTML",
            disable_web_preview=True,
            reply_markup=None,
        )
        assert any(req["type"] == "getUpdates" for req in client.requests)
        assert any(req["type"] == "sendMessage" for req in client.requests)

    asyncio.run(_run())


def test_ping_and_status_responses() -> None:
    started_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    snapshot = RuntimeStateSnapshot(
        started_at=started_at,
        pid=123,
        stream_state="connected",
        since_last_message_seconds=0.5,
        rx_total_orderbooks=10,
        rx_total_trades=5,
        current_symbols=["SBER"],
        depth=20,
        last_wall_event=WallEventState(
            event_type="wall_confirmed",
            ts=datetime(2024, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
            symbol="SBER",
            side="BUY",
            price=120.0,
            qty=100.0,
        ),
        last_error=None,
    )
    now = datetime(2024, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
    ping = format_ping_response(snapshot, now)
    status = format_status_response(snapshot)
    assert (
        ping
        == "pong 2024-01-01T00:00:10+00:00 uptime=0h0m stream_state=connected "
        "rx_total_orderbooks=10 rx_total_trades=5 since_last_message_seconds=0.500s"
    )
    assert "state=connected" in status
    assert "symbols=SBER" in status


def test_watch_updates_symbols() -> None:
    runtime_state = RuntimeState(
        started_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        pid=1,
        current_symbols=["SBER"],
        depth=20,
    )
    manager = FakeManager(symbols=["SBER"])

    def time_provider(_: timezone) -> datetime:
        return datetime(2024, 1, 1, 0, 0, 10, tzinfo=timezone.utc)

    handler = TelegramCommandHandler(
        runtime_state=runtime_state,
        manager=manager,
        max_symbols=10,
        allowed_user_ids=set(),
        include_instrument_button=True,
        instrument_button_text="Открыть в Т-Инвестициях",
        append_security_share_utm=False,
        emit_event=None,
        logger=logging.getLogger("test"),
        time_provider=time_provider,
    )

    async def _run() -> CommandResponse | None:
        return await handler.handle_command("/watch SBER,GAZP", chat_id=1, user_id=2)

    response = asyncio.run(_run())

    assert response is not None
    assert response.text == "watching: SBER, GAZP"
    assert manager.updated == ["SBER", "GAZP"]


def test_start_and_help_responses() -> None:
    runtime_state = RuntimeState(
        started_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        pid=1,
        current_symbols=["SBER"],
        depth=20,
    )
    manager = FakeManager(symbols=["SBER"])

    handler = TelegramCommandHandler(
        runtime_state=runtime_state,
        manager=manager,
        max_symbols=10,
        allowed_user_ids=set(),
        include_instrument_button=True,
        instrument_button_text="Открыть в Т-Инвестициях",
        append_security_share_utm=False,
        emit_event=None,
        logger=logging.getLogger("test"),
        time_provider=datetime.now,
    )

    async def _run_start() -> CommandResponse | None:
        return await handler.handle_command("/start", chat_id=1, user_id=2)

    async def _run_help() -> CommandResponse | None:
        return await handler.handle_command("/help", chat_id=1, user_id=2)

    start_response = asyncio.run(_run_start())
    help_response = asyncio.run(_run_help())

    assert start_response is not None
    assert start_response.text is not None
    assert "Привет" in start_response.text
    assert "/help" in start_response.text
    assert help_response is not None
    assert help_response.text is not None
    assert "Привет" not in help_response.text
    for text in (start_response.text, help_response.text):
        assert "<symbols>" not in text
        assert "</symbols>" not in text
        tags = re.findall(r"</?([a-zA-Z0-9]+)>", text)
        assert all(tag == "code" for tag in tags)


def test_telegram_http_client_sends_json(monkeypatch) -> None:
    seen: dict[str, object] = {}
    payload = {
        "chat_id": 1,
        "text": "hello",
        "reply_markup": {"inline_keyboard": [[{"text": "Open", "url": "https://x"}]]},
    }

    def fake_urlopen(req: urllib_request.Request, timeout: int = 0):
        seen["data"] = req.data
        seen["headers"] = {key.lower(): value for key, value in req.headers.items()}

        class Response:
            status = 200

            def read(self) -> bytes:
                return b'{"ok": true, "result": {"message_id": 1}}'

            def __enter__(self) -> Response:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        return Response()

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)

    async def _run() -> None:
        client = UrllibTelegramHttpClient()
        await client.post_json("https://api.telegram.org/bot123/sendMessage", payload)

    asyncio.run(_run())

    assert seen["headers"]["content-type"] == "application/json; charset=utf-8"
    assert isinstance(seen["data"], bytes)
    assert json.loads(seen["data"].decode("utf-8")) == payload


def test_telegram_poll_timeout_is_debug(caplog) -> None:
    class TimeoutApi:
        def __init__(self, stop_event: asyncio.Event) -> None:
            self._stop_event = stop_event

        async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, object]]:
            self._stop_event.set()
            raise TimeoutError()

        async def send_message(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError("send_message should not be called")

    class DummyHandler:
        async def handle_command(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            return None

    async def _run() -> list[logging.LogRecord]:
        stop_event = asyncio.Event()
        api = TimeoutApi(stop_event)
        polling = TelegramPolling(
            api=api,
            command_handler=DummyHandler(),
            logger=logging.getLogger("telegram_polling_test"),
            parse_mode=None,
            disable_web_preview=True,
            poll_interval_seconds=0,
        )
        caplog.set_level(logging.DEBUG, logger="telegram_polling_test")
        await polling.run(stop_event)
        return list(caplog.records)

    records = asyncio.run(_run())
    assert any(record.message == "telegram_poll_timeout" for record in records)
    assert not any(record.message == "telegram_poll_failed" for record in records)


def test_smoke_command_sends_message_with_button() -> None:
    stop_event = asyncio.Event()
    sent_payloads: list[dict[str, object]] = []

    class SmokeApi:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, object]]:
            stop_event.set()
            return [
                {
                    "update_id": 1,
                    "message": {
                        "text": "/smoke",
                        "chat": {"id": 123},
                        "from": {"id": 99},
                    },
                }
            ]

        async def send_message(
            self,
            *,
            chat_id: int,
            text: str,
            parse_mode: str | None = None,
            disable_web_preview: bool = True,
            reply_markup: dict[str, object] | None = None,
        ) -> None:
            self.sent.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_preview": disable_web_preview,
                    "reply_markup": reply_markup,
                }
            )

    runtime_state = RuntimeState(
        started_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        pid=1,
        current_symbols=["SBER"],
        depth=20,
    )
    manager = FakeManager(symbols=["SBER"])
    api = SmokeApi()

    async def _run() -> None:
        async def _send(url: str, payload: dict[str, object]) -> None:
            sent_payloads.append(payload)

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
        handler = TelegramCommandHandler(
            runtime_state=runtime_state,
            manager=manager,
            max_symbols=10,
            allowed_user_ids={99},
            include_instrument_button=True,
            instrument_button_text="Открыть в Т-Инвестициях",
            append_security_share_utm=False,
            emit_event=notifier.notify_event,
            logger=logging.getLogger("test"),
            time_provider=datetime.now,
        )
        polling = TelegramPolling(
            api=api,
            command_handler=handler,
            logger=logging.getLogger("telegram_polling_test"),
            parse_mode="HTML",
            disable_web_preview=True,
            poll_interval_seconds=0,
        )
        await polling.run(stop_event)
        await notifier.flush()
        await notifier.aclose()

    asyncio.run(_run())

    assert len(api.sent) == 0
    assert len(sent_payloads) == 1
    sent = json.loads(json.dumps(sent_payloads[0]))
    assert sent["chat_id"] == 123
    assert sent["parse_mode"] == "HTML"
    reply_markup = sent["reply_markup"]
    assert reply_markup is not None
    inline_keyboard = reply_markup["inline_keyboard"]
    assert isinstance(inline_keyboard, list)
    assert all(isinstance(row, list) for row in inline_keyboard)
    assert inline_keyboard[0][0]["url"].endswith("/invest/stocks/VSEH/")
