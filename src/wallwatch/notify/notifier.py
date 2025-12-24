from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from wallwatch.state.models import Alert


class Notifier:
    def notify(self, alert: Alert) -> None:
        raise NotImplementedError


@dataclass
class ConsoleNotifier(Notifier):
    def notify(self, alert: Alert) -> None:
        parts = [
            f"event={alert.event}",
            f"instrument={alert.instrument_id}",
            f"side={alert.side}",
            f"price={alert.price}",
            f"size={alert.size}",
            f"ratio={alert.ratio:.2f}",
            f"v_ref={alert.v_ref:.2f}",
            f"distance_ticks={alert.distance_ticks}",
            f"dwell={alert.dwell_seconds:.1f}s",
            f"executed_at_wall={alert.executed_at_wall:.2f}",
            f"cancel_share={alert.cancel_share:.2f}",
            f"reasons={','.join(alert.reasons)}",
        ]
        print(" ".join(parts))


class TelegramNotifier(Notifier):
    def __init__(
        self,
        bot: Any,
        chat_ids: list[int],
        parse_mode: str,
        logger: logging.Logger,
    ) -> None:
        self._bot = bot
        self._chat_ids = chat_ids
        self._parse_mode = parse_mode
        self._logger = logger
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task = asyncio.create_task(self._worker())

    def notify(self, alert: Alert) -> None:
        self._enqueue(self._format_alert(alert))

    async def send_alert(self, text: str) -> None:
        self._enqueue(text)

    def close(self) -> None:
        self._task.cancel()

    def _enqueue(self, text: str) -> None:
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            self._logger.warning("telegram_queue_full")

    async def _worker(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await self._send_with_retry(text)
            finally:
                self._queue.task_done()

    async def _send_with_retry(self, text: str) -> None:
        backoff = 1.0
        for attempt in range(3):
            try:
                for chat_id in self._chat_ids:
                    await self._bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=self._parse_mode,
                    )
                return
            except Exception as exc:  # noqa: BLE001
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is not None:
                    await asyncio.sleep(float(retry_after))
                    continue
                if attempt < 2:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                self._logger.warning("telegram_send_failed", extra={"error": str(exc)})
                return

    def _format_alert(self, alert: Alert) -> str:
        return (
            f"{alert.event}\n"
            f"instrument={alert.instrument_id}\n"
            f"side={alert.side}\n"
            f"price={alert.price}\n"
            f"size={alert.size}\n"
            f"ratio={alert.ratio:.2f}\n"
            f"v_ref={alert.v_ref:.2f}\n"
            f"distance_ticks={alert.distance_ticks}\n"
            f"dwell={alert.dwell_seconds:.1f}s\n"
            f"executed_at_wall={alert.executed_at_wall:.2f}\n"
            f"cancel_share={alert.cancel_share:.2f}\n"
            f"reasons={','.join(alert.reasons)}"
        )


class SlackNotifier(Notifier):
    def notify(self, alert: Alert) -> None:
        raise NotImplementedError("Slack notifier is not implemented yet.")
