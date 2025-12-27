from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv
from wallwatch.api.client import InstrumentInfo, MarketDataClient
from wallwatch.app.config import (
    CABundleError,
    ConfigError,
    configure_grpc_root_certificates,
    ensure_required_env,
    DEFAULT_GRPC_ENDPOINT,
    load_app_config,
    load_env_settings,
    missing_required_env,
    resolve_grpc_endpoint,
    resolve_depth,
    resolve_log_level,
)
from wallwatch.detector.wall_detector import DetectorConfig
from wallwatch.app.market_data_manager import MarketDataManager
from wallwatch.app.runtime_state import RuntimeState
from wallwatch.app.commands import TelegramCommandHandler
from wallwatch.app.telegram_client import TelegramApiClient, UrllibTelegramHttpClient
from wallwatch.app.telegram_polling import TelegramPolling
from wallwatch.notify.notifier import ConsoleNotifier
from wallwatch.notify.telegram_notifier import TelegramNotifier


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


def _load_dotenv() -> None:
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=False)


def _parse_symbols(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Order book wall monitor")
    parser.add_argument("--symbols", required=False, help="Comma separated symbols/ISINs")
    parser.add_argument("--depth", type=int, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--dump-book",
        action="store_true",
        default=False,
        help="Periodically dump order books via unary requests",
    )
    parser.add_argument(
        "--dump-book-interval",
        type=float,
        default=2.0,
        help="Order book dump interval in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--dump-book-levels",
        type=int,
        default=10,
        help="Order book dump depth levels (default: 10)",
    )
    parser.add_argument(
        "--debug-walls",
        action="store_true",
        default=None,
        help="Enable wall detector debug logs",
    )
    parser.add_argument(
        "--debug-walls-interval",
        type=float,
        default=None,
        help="Wall debug log interval in seconds (default: 1.0)",
    )
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
        config = load_app_config(args.config)
    except ConfigError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)

    try:
        log_level = resolve_log_level(args.log_level, config.logging.level, settings.log_level)
    except ConfigError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)
    logger.setLevel(log_level)

    try:
        ensure_required_env(settings)
    except ConfigError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)

    if config.telegram.enabled and not settings.tg_bot_token:
        logger.error(
            "config_error",
            extra={"error": "Missing required Telegram environment variable: tg_bot_token"},
        )
        sys.exit(1)

    detector_config = config.detector_config()
    depth = resolve_depth(args.depth, detector_config.depth)
    detector_config = DetectorConfig(**{**asdict(detector_config), "depth": depth})

    symbols = _parse_symbols(args.symbols) if args.symbols else []
    if len(symbols) > detector_config.max_symbols:
        symbols = symbols[: detector_config.max_symbols]

    debug_enabled = config.debug.walls_enabled if args.debug_walls is None else args.debug_walls
    debug_interval = (
        config.debug.walls_interval_seconds
        if args.debug_walls_interval is None
        else args.debug_walls_interval
    )

    try:
        configure_grpc_root_certificates(settings, logger)
    except CABundleError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)

    grpc_endpoint = resolve_grpc_endpoint(settings, logger)
    logger.info(
        "effective_config",
        extra={
            "config_path": str(args.config) if args.config else None,
            "logging.level": log_level,
            "marketdata.depth": detector_config.depth,
            "grpc.endpoint": grpc_endpoint,
            "debug.walls_enabled": debug_enabled,
            "debug.walls_interval_seconds": debug_interval,
            "walls.top_n_levels": config.walls.top_n_levels,
            "walls.candidate_ratio_to_median": config.walls.candidate_ratio_to_median,
            "walls.candidate_max_distance_ticks": config.walls.candidate_max_distance_ticks,
            "walls.confirm_dwell_seconds": config.walls.confirm_dwell_seconds,
            "walls.confirm_max_distance_ticks": config.walls.confirm_max_distance_ticks,
            "walls.consume_window_seconds": config.walls.consume_window_seconds,
            "walls.consume_drop_pct": config.walls.consume_drop_pct,
            "walls.teleport_reset": config.walls.teleport_reset,
            "telegram.enabled": config.telegram.enabled,
            "telegram.polling": config.telegram.polling,
            "telegram.poll_interval_seconds": config.telegram.poll_interval_seconds,
            "telegram.startup_message": config.telegram.startup_message,
            "telegram.send_events": list(config.telegram.send_events),
            "telegram.cooldown_seconds": config.telegram.cooldown_seconds,
            "telegram.disable_web_preview": config.telegram.disable_web_preview,
            "telegram.commands_enabled": config.telegram.commands_enabled,
            "telegram.include_instrument_button": config.telegram.include_instrument_button,
            "telegram.button_text": config.telegram.button_text,
            "telegram.append_security_share_utm": config.telegram.append_security_share_utm,
            "dump.book_enabled": args.dump_book,
            "dump.book_interval_seconds": args.dump_book_interval,
            "dump.book_levels": args.dump_book_levels,
        },
    )

    notifier = ConsoleNotifier(logger)
    client = MarketDataClient(
        token=settings.token or "",
        logger=logger,
        root_certificates=None,
        stream_idle_sleep_seconds=settings.stream_idle_sleep_seconds,
        instrument_status=settings.instrument_status,
        endpoint=grpc_endpoint,
    )
    telegram_notifier: TelegramNotifier | None = None

    if config.telegram.enabled:
        telegram_notifier = TelegramNotifier(
            token=settings.tg_bot_token or "",
            chat_ids=settings.tg_chat_ids,
            parse_mode=settings.tg_parse_mode,
            disable_web_preview=config.telegram.disable_web_preview,
            send_events=config.telegram.send_events,
            cooldown_seconds=config.telegram.cooldown_seconds,
            instrument_by_symbol={},
            include_instrument_button=config.telegram.include_instrument_button,
            instrument_button_text=config.telegram.button_text,
            append_security_share_utm=config.telegram.append_security_share_utm,
            logger=logger,
        )

    stop_event = asyncio.Event()
    runtime_state = RuntimeState(
        started_at=datetime.now(timezone.utc),
        pid=os.getpid(),
        current_symbols=list(symbols),
        depth=detector_config.depth,
    )
    runtime_state.update_sync(stream_state="connecting" if symbols else "idle")
    manager = MarketDataManager(
        detector_config=detector_config,
        client=client,
        logger=logger,
        notifier=telegram_notifier,
        alert_notifier=notifier,
        runtime_state=runtime_state,
        stop_event=stop_event,
        debug_enabled=debug_enabled,
        debug_interval=debug_interval,
        retry_backoff_initial_seconds=settings.retry_backoff_initial_seconds,
        retry_backoff_max_seconds=settings.retry_backoff_max_seconds,
    )
    await manager.start(symbols)

    logger.info(
        "startup",
        extra={
            "pid": os.getpid(),
            "symbols": symbols,
            "depth": detector_config.depth,
            "endpoint": grpc_endpoint,
        },
    )
    resolved: list[InstrumentInfo] = []
    if args.dump_book:
        resolved, failures = await client.resolve_instruments(symbols)
        for item in failures:
            logger.warning("instrument_not_found", extra={"symbol": item})
        if resolved:
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
            since_last = (
                None
                if manager.last_message_ts is None
                else round(now - manager.last_message_ts, 3)
            )
            await runtime_state.update(since_last_message_seconds=since_last)
            interval_orderbooks, interval_trades = manager.consume_interval_counts()
            snapshot = await runtime_state.snapshot()
            extra = {
                "alive": True,
                "since_last_message_seconds": since_last,
                "state": snapshot.stream_state,
                "rx_orderbooks_last_interval": interval_orderbooks,
                "rx_trades_last_interval": interval_trades,
                "rx_total_orderbooks": snapshot.rx_total_orderbooks,
                "rx_total_trades": snapshot.rx_total_trades,
            }
            logger.info("heartbeat", extra=extra)

    heartbeat_task = asyncio.create_task(_heartbeat())
    dump_task = None
    if args.dump_book and resolved:
        dump_task = asyncio.create_task(
            _run_orderbook_dump(
                client=client,
                instruments=resolved,
                depth=args.dump_book_levels,
                interval=args.dump_book_interval,
                logger=logger,
                stop_event=stop_event,
            )
        )

    telegram_http_client: UrllibTelegramHttpClient | None = None
    telegram_api: TelegramApiClient | None = None
    telegram_polling: TelegramPolling | None = None
    telegram_polling_task: asyncio.Task[None] | None = None
    if config.telegram.enabled and settings.tg_bot_token:
        telegram_http_client = UrllibTelegramHttpClient()
        telegram_api = TelegramApiClient(settings.tg_bot_token, telegram_http_client, logger)
        if (
            config.telegram.commands_enabled
            and config.telegram.polling
            and settings.tg_polling
        ):
            command_handler = TelegramCommandHandler(
                runtime_state=runtime_state,
                manager=manager,
                max_symbols=detector_config.max_symbols,
                allowed_user_ids=settings.tg_allowed_user_ids,
                logger=logger,
                time_provider=datetime.now,
            )
            telegram_polling = TelegramPolling(
                api=telegram_api,
                command_handler=command_handler,
                logger=logger,
                parse_mode=settings.tg_parse_mode,
                disable_web_preview=config.telegram.disable_web_preview,
                poll_interval_seconds=config.telegram.poll_interval_seconds,
            )
            telegram_polling_task = asyncio.create_task(telegram_polling.run(stop_event))

    if config.telegram.enabled and config.telegram.startup_message and telegram_api is not None:
        recipients = list(settings.tg_chat_ids)
        if not recipients and telegram_polling is not None:
            if telegram_polling.last_chat_id is not None:
                recipients = [telegram_polling.last_chat_id]
        if recipients:
            startup_text = (
                f"✅ wallwatch started (pid={runtime_state.pid}, "
                f"symbols={', '.join(symbols) if symbols else 'none'}, "
                f"depth={detector_config.depth})\n"
                "/help"
            )
            if telegram_polling is not None:
                await telegram_polling.send_startup_message(recipients, startup_text)
            else:
                for chat_id in recipients:
                    await telegram_api.send_message(
                        chat_id=chat_id,
                        text=startup_text,
                        parse_mode=settings.tg_parse_mode,
                        disable_web_preview=config.telegram.disable_web_preview,
                    )

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
    finally:
        stop_event.set()
        heartbeat_task.cancel()
        if dump_task is not None:
            dump_task.cancel()
        if telegram_polling_task is not None:
            telegram_polling_task.cancel()
        if telegram_notifier is not None:
            await telegram_notifier.aclose()
        telegram_http_client = None
        await manager.stop()


async def _run_orderbook_dump(
    client: MarketDataClient,
    instruments: list[InstrumentInfo],
    depth: int,
    interval: float,
    logger: logging.Logger,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        cycle_started = time.monotonic()
        for instrument in instruments:
            if stop_event.is_set():
                break
            try:
                snapshot = await client.get_order_book(
                    instrument_id=instrument.instrument_id,
                    depth=depth,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "orderbook_dump_failed",
                    extra={"symbol": instrument.symbol, "error": str(exc)},
                )
                continue
            if snapshot is None:
                continue
            best_bid = snapshot.best_bid
            best_ask = snapshot.best_ask
            spread = None
            if best_bid is not None and best_ask is not None:
                spread = best_ask - best_bid
            logger.info(
                "orderbook_dump",
                extra={
                    "symbol": instrument.symbol,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": spread,
                    "bids": [
                        {"price": level.price, "qty": level.quantity}
                        for level in snapshot.bids[:depth]
                    ],
                    "asks": [
                        {"price": level.price, "qty": level.quantity}
                        for level in snapshot.asks[:depth]
                    ],
                },
            )
        elapsed = time.monotonic() - cycle_started
        remaining = interval - elapsed
        if remaining > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                continue


async def run_doctor_async(argv: list[str]) -> None:
    parser = _build_doctor_parser()
    args = parser.parse_args(argv)
    logger = _configure_logger(logging.INFO)

    try:
        settings = load_env_settings()
    except ConfigError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)

    config = None
    if args.config is not None:
        try:
            config = load_app_config(args.config)
        except ConfigError:
            config = None

    try:
        config_level = None if config is None else config.logging.level
        log_level = resolve_log_level(args.log_level, config_level, settings.log_level)
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

    grpc_endpoint = DEFAULT_GRPC_ENDPOINT
    if settings is not None:
        grpc_endpoint = resolve_grpc_endpoint(settings, logger)
    report.append(("grpc_endpoint", True, f"Endpoint: {grpc_endpoint}"))

    try:
        _ = load_app_config(config_path)
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
            endpoint=grpc_endpoint,
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
    try:
        asyncio.run(run_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    main()
