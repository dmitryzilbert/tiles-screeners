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
from wallwatch.api.client import InstrumentInfo, MarketDataClient, _resolve_grpc_endpoint
from wallwatch.app.config import (
    CABundleError,
    ConfigError,
    configure_grpc_root_certificates,
    ensure_required_env,
    load_app_config,
    load_env_settings,
    missing_required_env,
    resolve_depth,
    resolve_log_level,
)
from wallwatch.detector.wall_detector import DetectorConfig, WallDetector
from wallwatch.notify.notifier import ConsoleNotifier
from wallwatch.notify.telegram_notifier import TelegramNotifier
from wallwatch.state.models import Alert, OrderBookSnapshot, Trade, WallEvent


def _configure_logger(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("wallwatch")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
    return logger


def _log_wall_events(logger: logging.Logger, events: list[WallEvent]) -> None:
    for event in events:
        logger.info(event.event, extra=event.to_log_extra())


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

    if config.telegram.enabled:
        missing = []
        if not settings.tg_bot_token:
            missing.append("tg_bot_token")
        if not settings.tg_chat_ids:
            missing.append("tg_chat_id")
        if missing:
            logger.error(
                "config_error",
                extra={
                    "error": "Missing required Telegram environment variables: "
                    + ", ".join(missing)
                },
            )
            sys.exit(1)

    detector_config = config.detector_config()
    depth = resolve_depth(args.depth, detector_config.depth)
    detector_config = DetectorConfig(**{**asdict(detector_config), "depth": depth})

    symbols = _parse_symbols(args.symbols)
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

    logger.info(
        "effective_config",
        extra={
            "config_path": str(args.config) if args.config else None,
            "logging.level": log_level,
            "marketdata.depth": detector_config.depth,
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
            "telegram.send_events": list(config.telegram.send_events),
            "telegram.cooldown_seconds": config.telegram.cooldown_seconds,
            "telegram.disable_web_preview": config.telegram.disable_web_preview,
            "dump.book_enabled": args.dump_book,
            "dump.book_interval_seconds": args.dump_book_interval,
            "dump.book_levels": args.dump_book_levels,
        },
    )

    detector = WallDetector(detector_config)
    notifier = ConsoleNotifier(logger)
    client = MarketDataClient(
        token=settings.token or "",
        logger=logger,
        root_certificates=None,
        stream_idle_sleep_seconds=settings.stream_idle_sleep_seconds,
        instrument_status=settings.instrument_status,
    )
    telegram_notifier: TelegramNotifier | None = None

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

    instrument_by_symbol = {instrument.symbol: instrument for instrument in resolved}
    if config.telegram.enabled:
        telegram_notifier = TelegramNotifier(
            token=settings.tg_bot_token or "",
            chat_ids=settings.tg_chat_ids,
            parse_mode=settings.tg_parse_mode,
            disable_web_preview=config.telegram.disable_web_preview,
            send_events=config.telegram.send_events,
            cooldown_seconds=config.telegram.cooldown_seconds,
            instrument_by_symbol=instrument_by_symbol,
            logger=logger,
        )

    stop_event = asyncio.Event()
    last_message_ts: float | None = None
    connection_state: dict[str, object] = {"state": "idle", "backoff_seconds": None}
    connected_logged = False
    rx_orderbooks_last_interval = 0
    rx_trades_last_interval = 0
    rx_total_orderbooks = 0
    rx_total_trades = 0

    logger.info(
        "startup",
        extra={
            "pid": os.getpid(),
            "symbols": symbols,
            "depth": detector_config.depth,
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
        nonlocal rx_orderbooks_last_interval, rx_trades_last_interval
        while not stop_event.is_set():
            await asyncio.sleep(15.0)
            now = time.monotonic()
            since_last = None if last_message_ts is None else round(now - last_message_ts, 3)
            extra = {
                "alive": True,
                "since_last_message_seconds": since_last,
                "state": connection_state.get("state"),
                "rx_orderbooks_last_interval": rx_orderbooks_last_interval,
                "rx_trades_last_interval": rx_trades_last_interval,
                "rx_total_orderbooks": rx_total_orderbooks,
                "rx_total_trades": rx_total_trades,
            }
            if connection_state.get("state") == "backoff":
                extra["backoff_seconds"] = connection_state.get("backoff_seconds")
            logger.info("heartbeat", extra=extra)
            rx_orderbooks_last_interval = 0
            rx_trades_last_interval = 0

    heartbeat_task = asyncio.create_task(_heartbeat())
    dump_task = None
    if args.dump_book:
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

                def _on_order_book(snapshot: OrderBookSnapshot) -> list[Alert]:
                    nonlocal rx_orderbooks_last_interval, rx_total_orderbooks
                    _mark_connected()
                    rx_orderbooks_last_interval += 1
                    rx_total_orderbooks += 1
                    if debug_enabled:
                        debug_interval = args.debug_walls_interval
                        if debug_interval is None:
                            debug_interval = config.debug.walls_interval_seconds
                        alerts, debug_payload, events = detector.on_order_book_with_debug(
                            snapshot, debug_interval
                        )
                        _log_wall_events(logger, events)
                        if telegram_notifier is not None:
                            for event in events:
                                telegram_notifier.notify(event)
                        if debug_payload is not None:
                            logger.info("wall_debug", extra=debug_payload)
                        return alerts
                    alerts, events = detector.on_order_book_with_events(snapshot)
                    _log_wall_events(logger, events)
                    if telegram_notifier is not None:
                        for event in events:
                            telegram_notifier.notify(event)
                    return alerts

                def _on_trade(trade: Trade) -> list[Alert]:
                    nonlocal rx_trades_last_interval, rx_total_trades
                    _mark_connected()
                    rx_trades_last_interval += 1
                    rx_total_trades += 1
                    return detector.on_trade(trade)

                await client.stream_market_data(
                    instruments=resolved,
                    depth=detector_config.depth,
                    on_order_book=_on_order_book,
                    on_trade=_on_trade,
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
        if dump_task is not None:
            dump_task.cancel()
        if telegram_notifier is not None:
            await telegram_notifier.aclose()


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
