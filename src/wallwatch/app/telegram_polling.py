from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from wallwatch.app.commands import TelegramCommandHandler
from wallwatch.app.telegram_client import TelegramApiClient, TelegramApiError


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
        self._last_chat_id: int | None = None

    @property
    def last_chat_id(self) -> int | None:
        return self._last_chat_id

    async def run(self, stop_event: asyncio.Event) -> None:
        self._logger.info("telegram_polling_started")
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
                chat = message.get("chat") or {}
                from_user = message.get("from") or {}
                chat_id = chat.get("id")
                if update_id is not None or chat_id is not None:
                    self._logger.info(
                        "telegram_update_received",
                        extra={
                            "update_id": update_id,
                            "chat_id": chat_id,
                            "has_text": bool(text),
                        },
                    )
                if not text or chat_id is None:
                    continue
                user_id = from_user.get("id")
                self._last_chat_id = chat_id
                response = await self._command_handler.handle_command(
                    text, chat_id=chat_id, user_id=user_id
                )
                if response:
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
            description = exc.description if isinstance(exc, TelegramApiError) else None
            extra = {"error": str(exc)}
            if description:
                extra["telegram_description"] = description
            self._logger.warning("telegram_send_failed", extra=extra)
