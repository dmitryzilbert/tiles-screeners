from __future__ import annotations

from wallwatch.app.main import run_monitor_async


async def run_telegram_async(argv: list[str]) -> None:
    await run_monitor_async(argv)
