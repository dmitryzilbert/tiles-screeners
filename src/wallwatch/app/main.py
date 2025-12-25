from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv
from wallwatch.api.client import MarketDataClient
from wallwatch.app.config import (
    CABundleError,
    ConfigError,
    configure_grpc_root_certificates,
    ensure_required_env,
    load_detector_config,
    load_env_settings,
    missing_required_env,
)
from wallwatch.detector.wall_detector import DetectorConfig, WallDetector
from wallwatch.notify.notifier import ConsoleNotifier


def _configure_logger() -> logging.Logger:
    logger = logging.getLogger("wallwatch")
    logger.setLevel(logging.INFO)
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
    return parser


def _build_doctor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WallWatch preflight checks")
    parser.add_argument(
        "--symbols",
        required=False,
        help="Optional comma separated symbols/ISINs (default: SBER)",
    )
    parser.add_argument("--config", type=Path, default=None)
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

    try:
        settings = load_env_settings()
        ensure_required_env(settings)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    try:
        config = load_detector_config(args.config)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    if args.depth is not None:
        config = DetectorConfig(**{**asdict(config), "depth": args.depth})

    symbols = _parse_symbols(args.symbols)
    if len(symbols) > config.max_symbols:
        symbols = symbols[: config.max_symbols]

    logger = _configure_logger()
    try:
        configure_grpc_root_certificates(settings, logger)
    except CABundleError as exc:
        raise SystemExit(str(exc)) from exc

    detector = WallDetector(config)
    notifier = ConsoleNotifier()
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
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal)

    backoff = settings.retry_backoff_initial_seconds
    try:
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
                backoff = settings.retry_backoff_initial_seconds
            except Exception as exc:  # noqa: BLE001
                logger.error("stream_failed", extra={"error": str(exc)})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, settings.retry_backoff_max_seconds)
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
    finally:
        stop_event.set()


async def run_doctor_async(argv: list[str]) -> None:
    parser = _build_doctor_parser()
    args = parser.parse_args(argv)
    symbols = _parse_symbols(args.symbols) if args.symbols else DEFAULT_DOCTOR_SYMBOLS
    report, fatal = await build_doctor_report(symbols, args.config)
    _print_report(report)
    if fatal:
        raise SystemExit(1)


async def build_doctor_report(
    symbols: list[str],
    config_path: Path | None,
) -> tuple[list[tuple[str, bool, str]], bool]:
    logger = _configure_logger()
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


def _print_report(report: list[tuple[str, bool, str]]) -> None:
    for name, ok, message in report:
        status = "OK" if ok else "FAIL"
        print(f"{status}\t{name}\t{message}")


def main() -> None:
    asyncio.run(run_async())


if __name__ == "__main__":
    main()
