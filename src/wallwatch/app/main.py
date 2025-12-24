from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from wallwatch.api.client import MarketDataClient
from wallwatch.detector.wall_detector import DetectorConfig, WallDetector
from wallwatch.notify.notifier import ConsoleNotifier


def _configure_logger() -> logging.Logger:
    logger = logging.getLogger("wallwatch")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    return logger


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "message": record.getMessage()}
        standard = {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in standard}
        payload.update(extras)
        return json.dumps(payload, ensure_ascii=False)


def load_config(path: Path | None) -> DetectorConfig:
    if path is None:
        return DetectorConfig()
    content = yaml.safe_load(path.read_text()) or {}
    return DetectorConfig(**content)


async def run_async() -> None:
    parser = argparse.ArgumentParser(description="Order book wall monitor")
    parser.add_argument("--symbols", required=True, help="Comma separated symbols/ISINs")
    parser.add_argument("--depth", type=int, default=None)
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    if args.depth is not None:
        config = DetectorConfig(**{**asdict(config), "depth": args.depth})

    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    if len(symbols) > config.max_symbols:
        symbols = symbols[: config.max_symbols]

    token = os.getenv("TINVEST_TOKEN") or os.getenv("INVEST_TOKEN")
    if not token:
        raise SystemExit("TINVEST_TOKEN is required")

    logger = _configure_logger()
    detector = WallDetector(config)
    notifier = ConsoleNotifier()
    client = MarketDataClient(token=token, logger=logger)

    resolved, failures = await client.resolve_instruments(symbols)
    for item in failures:
        logger.warning("instrument_not_found", extra={"symbol": item})

    if not resolved:
        raise SystemExit("No instruments resolved")

    for instrument in resolved:
        detector.upsert_instrument(
            instrument_id=instrument.instrument_id,
            tick_size=instrument.tick_size,
            symbol=instrument.symbol,
        )

    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("shutdown_requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    backoff = 1.0
    while not stop_event.is_set():
        try:
            await client.stream_market_data(
                instruments=resolved,
                depth=config.depth,
                on_order_book=detector.on_order_book,
                on_trade=detector.on_trade,
                on_alerts=lambda alerts: [notifier.notify(alert) for alert in alerts],
                stop_event=stop_event,
            )
            backoff = 1.0
        except Exception as exc:  # noqa: BLE001
            logger.error("stream_failed", extra={"error": str(exc)})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def main() -> None:
    asyncio.run(run_async())


if __name__ == "__main__":
    main()
