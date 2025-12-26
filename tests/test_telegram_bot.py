from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from urllib import request as urllib_request

from wallwatch.app.commands import (
    TelegramCommandHandler,
    format_ping_response,
    format_status_response,
)
from wallwatch.app.runtime_state import RuntimeState, RuntimeStateSnapshot, WallEventState
from wallwatch.app.telegram_client import TelegramApiClient, UrllibTelegramHttpClient


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
        await api.send_message(chat_id=1, text="hello", parse_mode="HTML", disable_web_preview=True)
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
        logger=logging.getLogger("test"),
        time_provider=time_provider,
    )

    async def _run() -> str | None:
        return await handler.handle_command("/watch SBER,GAZP", chat_id=1, user_id=2)

    response = asyncio.run(_run())

    assert response == "watching: SBER, GAZP"
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
        logger=logging.getLogger("test"),
        time_provider=datetime.now,
    )

    async def _run_start() -> str | None:
        return await handler.handle_command("/start", chat_id=1, user_id=2)

    async def _run_help() -> str | None:
        return await handler.handle_command("/help", chat_id=1, user_id=2)

    start_response = asyncio.run(_run_start())
    help_response = asyncio.run(_run_help())

    assert start_response is not None
    assert "Привет" in start_response
    assert "/help" in start_response
    assert help_response is not None
    assert "Привет" not in help_response
    for text in (start_response, help_response):
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
