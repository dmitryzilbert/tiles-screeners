from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv
from wallwatch.api.client import MarketDataClient, _resolve_grpc_endpoint
from wallwatch.app.config import (
    CABundleError,
    ConfigError,
    configure_grpc_root_certificates,
    ensure_required_env,
    load_detector_config,
    load_env_settings,
    missing_required_env,
    parse_log_level,
)
from wallwatch.detector.wall_detector import DetectorConfig, WallDetector
from wallwatch.notify.notifier import ConsoleNotifier


def _configure_logger(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("wallwatch")
    logger.setLevel(level)
    if not logger.handlers:
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


DEFAULT_DOCTOR_SYMBOLS = ["SBER"]


def _resolve_log_level(cli_value: str | None, env_level: int) -> int:
    if cli_value is None:
        return env_level
    return parse_log_level(cli_value, name="log_level")


def _load_dotenv() -> None:
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=False)


def _parse_symbols(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Order book wall monitor")
    parser.add_argument("--symbols", required=True, help="Comma separated symbols/ISINs")
    parser.add_argument("--depth", type=int, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--log-level",
        default=None,
        help="Log level (default: INFO, env: log_level)",
    )
    return parser


def _build_doctor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WallWatch preflight checks")
    parser.add_argument(
        "--symbols",
        required=False,
        help="Optional comma separated symbols/ISINs (default: SBER)",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--log-level",
        default=None,
        help="Log level (default: INFO, env: log_level)",
    )
    return parser


async def run_async(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    _load_dotenv()
    if argv[:1] == ["run"]:
        await run_monitor_async(argv[1:])
        return
    if argv[:1] == ["doctor"]:
        await run_doctor_async(argv[1:])
        return
    if argv[:1] in (["telegram"], ["tg"]):
        from wallwatch.app.telegram import run_telegram_async

        await run_telegram_async(argv[1:])
        return
    await run_monitor_async(argv)


async def run_monitor_async(argv: list[str]) -> None:
    parser = _build_run_parser()
    args = parser.parse_args(argv)
    logger = _configure_logger(logging.INFO)

    try:
        settings = load_env_settings()
    except ConfigError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)

    try:
        log_level = _resolve_log_level(args.log_level, settings.log_level)
    except ConfigError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)
    logger.setLevel(log_level)

    try:
        ensure_required_env(settings)
    except ConfigError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)

    try:
        config = load_detector_config(args.config)
    except ConfigError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)
    if args.depth is not None:
        config = DetectorConfig(**{**asdict(config), "depth": args.depth})

    symbols = _parse_symbols(args.symbols)
    if len(symbols) > config.max_symbols:
        symbols = symbols[: config.max_symbols]

    try:
        configure_grpc_root_certificates(settings, logger)
    except CABundleError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)

    detector = WallDetector(config)
    notifier = ConsoleNotifier(logger)
    client = MarketDataClient(
        token=settings.token or "",
        logger=logger,
        root_certificates=None,
        stream_idle_sleep_seconds=settings.stream_idle_sleep_seconds,
        instrument_status=settings.instrument_status,
    )

    resolved, failures = await client.resolve_instruments(symbols)
    for item in failures:
        logger.warning("instrument_not_found", extra={"symbol": item})

    if not resolved:
        logger.error("no_instruments_resolved")
        sys.exit(1)

    for instrument in resolved:
        detector.upsert_instrument(
            instrument_id=instrument.instrument_id,
            tick_size=instrument.tick_size,
            symbol=instrument.symbol,
        )

    stop_event = asyncio.Event()
    last_message_ts: float | None = None
    connection_state: dict[str, object] = {"state": "idle", "backoff_seconds": None}
    connected_logged = False

    logger.info(
        "startup",
        extra={
            "pid": os.getpid(),
            "symbols": symbols,
            "depth": config.depth,
            "endpoint": _resolve_grpc_endpoint(),
        },
    )
    logger.info(
        "instrument_resolve",
        extra={
            "resolved": len(resolved),
            "failed": len(failures),
        },
    )

    def _handle_signal() -> None:
        logger.info("shutdown_requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal)

    async def _heartbeat() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(15.0)
            now = time.monotonic()
            since_last = None if last_message_ts is None else round(now - last_message_ts, 3)
            extra = {
                "alive": True,
                "since_last_message_seconds": since_last,
                "state": connection_state.get("state"),
            }
            if connection_state.get("state") == "backoff":
                extra["backoff_seconds"] = connection_state.get("backoff_seconds")
            logger.info("heartbeat", extra=extra)

    heartbeat_task = asyncio.create_task(_heartbeat())

    backoff = settings.retry_backoff_initial_seconds
    try:
        while not stop_event.is_set():
            try:
                connection_state["state"] = "connecting"
                connection_state["backoff_seconds"] = None
                connected_logged = False
                logger.info("connecting")

                def _mark_connected() -> None:
                    nonlocal last_message_ts, connected_logged
                    last_message_ts = time.monotonic()
                    if not connected_logged:
                        connected_logged = True
                        connection_state["state"] = "connected"
                        logger.info("connected")

                await client.stream_market_data(
                    instruments=resolved,
                    depth=config.depth,
                    on_order_book=lambda snapshot: (
                        _mark_connected() or detector.on_order_book(snapshot)
                    ),
                    on_trade=lambda trade: _mark_connected() or detector.on_trade(trade),
                    on_alerts=lambda alerts: [notifier.notify(alert) for alert in alerts],
                    stop_event=stop_event,
                )
                backoff = settings.retry_backoff_initial_seconds
            except Exception as exc:  # noqa: BLE001
                logger.error("stream_failed", extra={"error": str(exc)})
                connection_state["state"] = "backoff"
                connection_state["backoff_seconds"] = backoff
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, settings.retry_backoff_max_seconds)
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
    finally:
        stop_event.set()
        heartbeat_task.cancel()


async def run_doctor_async(argv: list[str]) -> None:
    parser = _build_doctor_parser()
    args = parser.parse_args(argv)
    logger = _configure_logger(logging.INFO)

    try:
        settings = load_env_settings()
    except ConfigError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)

    try:
        log_level = _resolve_log_level(args.log_level, settings.log_level)
    except ConfigError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)
    logger.setLevel(log_level)

    symbols = _parse_symbols(args.symbols) if args.symbols else DEFAULT_DOCTOR_SYMBOLS
    report, fatal = await build_doctor_report(
        symbols,
        args.config,
        log_level=log_level,
    )
    _log_report(report, logger)
    if fatal:
        sys.exit(1)


async def build_doctor_report(
    symbols: list[str],
    config_path: Path | None,
    log_level: int | None = None,
) -> tuple[list[tuple[str, bool, str]], bool]:
    logger = _configure_logger(log_level or logging.INFO)
    report: list[tuple[str, bool, str]] = []
    fatal = False

    try:
        settings = load_env_settings()
    except ConfigError as exc:
        report.append(("env", False, str(exc)))
        settings = None
        fatal = True

    if settings is not None:
        missing = missing_required_env(settings)
        if missing:
            report.append(("env", False, f"Missing required: {', '.join(missing)}"))
            fatal = True
        else:
            report.append(("env", True, "Required environment variables set"))

    try:
        _ = load_detector_config(config_path)
        report.append(("config", True, "Config loaded"))
    except ConfigError as exc:
        report.append(("config", False, str(exc)))
        fatal = True

    grpc_ca_path = None
    if settings is not None:
        try:
            grpc_ca_path = configure_grpc_root_certificates(settings, logger)
            if grpc_ca_path:
                report.append(
                    (
                        "ca_bundle",
                        True,
                        f"Using GRPC_DEFAULT_SSL_ROOTS_FILE_PATH={grpc_ca_path}",
                    )
                )
            else:
                report.append(("ca_bundle", True, "Using system/available CA bundle"))
        except CABundleError as exc:
            report.append(("ca_bundle", False, str(exc)))
            fatal = True

    if not fatal and settings is not None:
        grpc_symbols = symbols or DEFAULT_DOCTOR_SYMBOLS
        client = MarketDataClient(
            token=settings.token or "",
            logger=logger,
            root_certificates=None,
            stream_idle_sleep_seconds=settings.stream_idle_sleep_seconds,
            instrument_status=settings.instrument_status,
        )
        try:
            resolved, failures = await client.resolve_instruments(grpc_symbols)
        except Exception as exc:  # noqa: BLE001
            report.append(("grpc", False, f"gRPC request failed: {exc}"))
            fatal = True
        else:
            if resolved:
                report.append(("grpc", True, f"Resolved {len(resolved)} instrument(s)"))
                if failures:
                    logger.warning("instrument_resolve_failed", extra={"symbols": failures})
            else:
                report.append(("grpc", False, "No instruments resolved"))
                fatal = True

    return report, fatal


def _log_report(report: list[tuple[str, bool, str]], logger: logging.Logger) -> None:
    for name, ok, message in report:
        status = "OK" if ok else "FAIL"
        logger.info(
            "doctor_report",
            extra={"status": status, "check": name, "message": message},
        )


def main() -> None:
    asyncio.run(run_async())


if __name__ == "__main__":
    main()
