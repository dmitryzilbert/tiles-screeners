from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import importlib.util
import logging
import signal
import sys
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wallwatch.api.client import MarketDataClient
from wallwatch.app.config import (
    CABundleError,
    ConfigError,
    configure_grpc_root_certificates,
    ensure_required_env,
    load_app_config,
    load_env_settings,
    resolve_depth,
    resolve_log_level,
)
from wallwatch.app.main import _configure_logger, build_doctor_report
from wallwatch.detector.wall_detector import DetectorConfig, WallDetector
from wallwatch.notify.notifier import TelegramNotifier


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    args: list[str]


def parse_command(text: str) -> ParsedCommand | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    if not parts:
        return None
    command = parts[0][1:]
    if "@" in command:
        command = command.split("@", 1)[0]
    if not command:
        return None
    return ParsedCommand(name=command.lower(), args=parts[1:])


def _parse_symbols(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _ensure_telegram_dependency(logger: logging.Logger) -> None:
    if importlib.util.find_spec("telegram") is None:
        logger.error(
            "missing_dependency",
            extra={"error": "Telegram support not installed. Install with: pip install -e '.[telegram]'"},
        )
        sys.exit(1)


def _ensure_telegram_env(settings: Any) -> None:
    missing = []
    if not settings.tg_bot_token:
        missing.append("tg_bot_token")
    if not settings.tg_chat_ids:
        missing.append("tg_chat_id")
    if missing:
        raise ConfigError(
            "Missing required Telegram environment variables: " + ", ".join(missing)
        )


def _format_uptime(started_at: datetime) -> str:
    delta = datetime.now(timezone.utc) - started_at
    minutes, _ = divmod(int(delta.total_seconds()), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes}m"


def _resolve_version() -> str:
    try:
        return importlib.metadata.version("wallwatch")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


class TelegramMonitor:
    def __init__(
        self,
        *,
        config: DetectorConfig,
        client: MarketDataClient,
        notifier: TelegramNotifier,
        logger: logging.Logger,
        max_symbols: int,
        initial_symbols: list[str],
    ) -> None:
        self._config = config
        self._client = client
        self._notifier = notifier
        self._logger = logger
        self._max_symbols = max_symbols
        self._symbols = list(dict.fromkeys(initial_symbols))
        self._resolved: list[Any] = []
        self._detector = WallDetector(config)
        self._alert_history: deque[str] = deque(maxlen=10)
        self._stream_status = "idle"
        self._restart_event = asyncio.Event()
        self._started_at = datetime.now(timezone.utc)

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    def request_restart(self) -> None:
        self._restart_event.set()

    def add_symbol(self, symbol: str) -> tuple[bool, str]:
        normalized = symbol.strip().upper()
        if not normalized:
            return False, "Symbol is empty."
        if normalized in self._symbols:
            return False, f"{normalized} is already monitored."
        if len(self._symbols) >= self._max_symbols:
            return False, f"Max symbols reached ({self._max_symbols})."
        self._symbols.append(normalized)
        self.request_restart()
        return True, f"Added {normalized}."

    def remove_symbol(self, symbol: str) -> tuple[bool, str]:
        normalized = symbol.strip().upper()
        if normalized not in self._symbols:
            return False, f"{normalized} is not in the monitor list."
        self._symbols.remove(normalized)
        self.request_restart()
        return True, f"Removed {normalized}."

    def status_text(self) -> str:
        lines = [
            f"stream={self._stream_status}",
            f"symbols={', '.join(self._symbols) if self._symbols else 'none'}",
            f"resolved={', '.join(item.symbol for item in self._resolved) if self._resolved else 'none'}",
        ]
        confirmed = []
        for state in self._detector.list_states():
            wall = state.active_wall
            if wall and wall.confirmed_ts:
                confirmed.append(f"{state.symbol} {wall.side} {wall.price}")
        lines.append(f"confirmed_walls={'; '.join(confirmed) if confirmed else 'none'}")
        if self._alert_history:
            lines.append("recent_alerts:")
            for item in self._alert_history:
                lines.append(f"- {item}")
        return "\n".join(lines)

    def ping_text(self) -> str:
        version = _resolve_version()
        uptime = _format_uptime(self._started_at)
        return f"pong (version={version}, uptime={uptime})"

    async def run(self, stop_event: asyncio.Event) -> None:
        delay = 1.0
        while not stop_event.is_set():
            if not self._symbols:
                self._stream_status = "idle"
                await asyncio.sleep(1.0)
                continue

            try:
                resolved, failures = await self._client.resolve_instruments(self._symbols)
            except Exception as exc:  # noqa: BLE001
                self._stream_status = "resolve_failed"
                self._logger.warning("instrument_resolve_failed", extra={"error": str(exc)})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue

            for item in failures:
                self._logger.warning("instrument_not_found", extra={"symbol": item})
            if not resolved:
                self._stream_status = "no_instruments"
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue

            delay = 1.0
            self._resolved = resolved
            resolved_ids = {item.instrument_id for item in resolved}
            for instrument_id in self._detector.instrument_ids():
                if instrument_id not in resolved_ids:
                    self._detector.remove_instrument(instrument_id)
            for instrument in resolved:
                self._detector.upsert_instrument(
                    instrument_id=instrument.instrument_id,
                    tick_size=instrument.tick_size,
                    symbol=instrument.symbol,
                )

            stream_stop = asyncio.Event()

            async def _watch_stop() -> None:
                await asyncio.wait(
                    [stop_event.wait(), self._restart_event.wait()],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                stream_stop.set()

            watcher = asyncio.create_task(_watch_stop())
            self._stream_status = "running"
            try:
                def _on_order_book(snapshot: Any) -> list[Any]:
                    alerts, events = self._detector.on_order_book_with_events(snapshot)
                    for event in events:
                        self._logger.info(event.event, extra=event.to_log_extra())
                    return alerts

                await self._client.stream_market_data(
                    instruments=resolved,
                    depth=self._config.depth,
                    on_order_book=_on_order_book,
                    on_trade=self._detector.on_trade,
                    on_alerts=self._on_alerts,
                    stop_event=stream_stop,
                )
            except Exception as exc:  # noqa: BLE001
                self._stream_status = "stream_failed"
                self._logger.error("stream_failed", extra={"error": str(exc)})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
            finally:
                watcher.cancel()

            if self._restart_event.is_set():
                self._restart_event.clear()

    def _on_alerts(self, alerts: list[Any]) -> list[Any]:
        for alert in alerts:
            summary = f"{alert.instrument_id} {alert.event} {alert.price}"
            self._alert_history.append(summary)
            self._notifier.notify(alert)
        return alerts


async def run_telegram_async(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description="WallWatch Telegram interface")
    parser.add_argument("--symbols", default="", help="Comma separated symbols/ISINs")
    parser.add_argument("--depth", type=int, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--log-level",
        default=None,
        help="Log level (default: INFO, env: log_level)",
    )
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
        _ensure_telegram_env(settings)
    except ConfigError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)

    if not settings.tg_polling:
        logger.error(
            "config_error",
            extra={"error": "tg_polling=false is not supported in CLI mode"},
        )
        sys.exit(1)

    detector_config = config.detector_config()
    depth = resolve_depth(args.depth, detector_config.depth)
    detector_config = DetectorConfig(**{**asdict(detector_config), "depth": depth})

    debug_enabled = config.debug.walls_enabled

    logger.info(
        "effective_config",
        extra={
            "config_path": str(args.config) if args.config else None,
            "logging.level": log_level,
            "marketdata.depth": detector_config.depth,
            "debug.walls_enabled": debug_enabled,
            "debug.walls_interval_seconds": config.debug.walls_interval_seconds,
            "walls.top_n_levels": config.walls.top_n_levels,
            "walls.candidate_ratio_to_median": config.walls.candidate_ratio_to_median,
            "walls.candidate_max_distance_ticks": config.walls.candidate_max_distance_ticks,
            "walls.confirm_dwell_seconds": config.walls.confirm_dwell_seconds,
            "walls.confirm_max_distance_ticks": config.walls.confirm_max_distance_ticks,
            "walls.consume_window_seconds": config.walls.consume_window_seconds,
            "walls.consume_drop_pct": config.walls.consume_drop_pct,
            "walls.teleport_reset": config.walls.teleport_reset,
        },
    )

    _ensure_telegram_dependency(logger)
    from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
    from telegram import Update

    try:
        configure_grpc_root_certificates(settings, logger)
    except CABundleError as exc:
        logger.error("config_error", extra={"error": str(exc)})
        sys.exit(1)

    bot = ApplicationBuilder().token(settings.tg_bot_token or "").build()
    notifier = TelegramNotifier(
        bot.bot,
        settings.tg_chat_ids,
        settings.tg_parse_mode,
        logger,
    )

    initial_symbols = _parse_symbols(args.symbols)
    if len(initial_symbols) > detector_config.max_symbols:
        initial_symbols = initial_symbols[: detector_config.max_symbols]

    client = MarketDataClient(
        token=settings.token or "",
        logger=logger,
        root_certificates=None,
        stream_idle_sleep_seconds=settings.stream_idle_sleep_seconds,
        instrument_status=settings.instrument_status,
    )

    monitor = TelegramMonitor(
        config=detector_config,
        client=client,
        notifier=notifier,
        logger=logger,
        max_symbols=detector_config.max_symbols,
        initial_symbols=initial_symbols,
    )

    allowed_chat_ids = set(settings.tg_chat_ids)
    allowed_user_ids = set(settings.tg_allowed_user_ids)

    async def _send_message(update: Update, text: str) -> None:
        if update.message is None:
            return
        await update.message.reply_text(
            text,
            parse_mode=settings.tg_parse_mode,
        )

    def _is_authorized(update: Update) -> bool:
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_id = update.effective_user.id if update.effective_user else None
        if chat_id is None or chat_id not in allowed_chat_ids:
            return False
        if allowed_user_ids and (user_id is None or user_id not in allowed_user_ids):
            return False
        return True

    async def _handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.message.text is None:
            return
        if not _is_authorized(update):
            return
        parsed = parse_command(update.message.text)
        if parsed is None:
            return
        name = parsed.name
        cmd_args = parsed.args
        if name == "start":
            await _send_message(update, _help_text())
            return
        if name == "help":
            await _send_message(update, _help_text())
            return
        if name == "ping":
            await _send_message(update, monitor.ping_text())
            return
        if name == "symbols":
            symbols_text = ", ".join(monitor.symbols) if monitor.symbols else "none"
            await _send_message(update, f"symbols={symbols_text}")
            return
        if name == "add":
            if not cmd_args:
                await _send_message(update, "Usage: /add <symbol>")
                return
            ok, message = monitor.add_symbol(cmd_args[0])
            await _send_message(update, message)
            return
        if name == "remove":
            if not cmd_args:
                await _send_message(update, "Usage: /remove <symbol>")
                return
            ok, message = monitor.remove_symbol(cmd_args[0])
            await _send_message(update, message)
            return
        if name == "restart_stream":
            monitor.request_restart()
            await _send_message(update, "Stream restart requested.")
            return
        if name == "status":
            await _send_message(update, monitor.status_text())
            return
        if name == "doctor":
            if not monitor.symbols:
                await _send_message(update, "No symbols configured. Use /add <symbol> first.")
                return
            report, fatal = await build_doctor_report(monitor.symbols, args.config)
            await _send_message(update, _format_doctor_report(report, fatal))
            return
        await _send_message(update, "Unknown command. Use /help.")

    def _help_text() -> str:
        return (
            "WallWatch Telegram commands:\n"
            "/start - help\n"
            "/help - list commands\n"
            "/ping - health check\n"
            "/doctor - run preflight checks\n"
            "/status - stream status and alerts\n"
            "/symbols - list monitored symbols\n"
            "/add <symbol> - add symbol\n"
            "/remove <symbol> - remove symbol\n"
            "/restart_stream - reconnect market data stream"
        )

    def _format_doctor_report(report: list[tuple[str, bool, str]], fatal: bool) -> str:
        lines = ["Doctor report:"]
        for name, ok, message in report:
            status = "OK" if ok else "FAIL"
            lines.append(f"{status} {name}: {message}")
        lines.append("Result: FAIL" if fatal else "Result: OK")
        return "\n".join(lines)

    bot.add_handler(MessageHandler(filters.COMMAND, _handle_command))

    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("shutdown_requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling()
    try:
        await monitor.run(stop_event)
    finally:
        notifier.close()
        await bot.updater.stop()
        await bot.stop()
        await bot.shutdown()
