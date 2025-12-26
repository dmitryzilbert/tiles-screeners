from __future__ import annotations

import asyncio
import base64
import os
from types import SimpleNamespace
from pathlib import Path

import pytest

from wallwatch.app import main as app_main
from wallwatch.app.config import AppConfig
from wallwatch.api.client import InstrumentInfo


def test_cli_symbols_optional() -> None:
    parser = app_main._build_run_parser()
    args = parser.parse_args([])
    assert args.symbols is None


def test_doctor_parser_symbols_optional() -> None:
    parser = app_main._build_doctor_parser()
    args = parser.parse_args([])
    assert args.symbols is None


def test_build_doctor_report_uses_default_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("tinvest_token", "token")
    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    monkeypatch.setenv(
        "tinvest_ca_bundle_b64", base64.b64encode(pem.encode()).decode()
    )
    captured: dict[str, list[str]] = {}

    async def fake_resolve(
        self: app_main.MarketDataClient, symbols: list[str]
    ) -> tuple[list[InstrumentInfo], list[str]]:
        captured["symbols"] = list(symbols)
        return [InstrumentInfo(instrument_id="id", symbol="SBER", tick_size=0.01)], []

    monkeypatch.setattr(app_main.MarketDataClient, "resolve_instruments", fake_resolve)

    report, fatal = asyncio.run(app_main.build_doctor_report([], None))

    assert captured["symbols"] == app_main.DEFAULT_DOCTOR_SYMBOLS
    assert not fatal
    assert any(name == "ca_bundle" and ok for name, ok, _ in report)


def test_build_doctor_report_sets_grpc_env_var_for_ca_bundle_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("tinvest_token", "token")
    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    ca_path = tmp_path / "bundle.pem"
    ca_path.write_text(pem)
    monkeypatch.setenv("TINVEST_CA_BUNDLE_PATH", str(ca_path))
    monkeypatch.delenv("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", raising=False)

    async def fake_resolve(
        self: app_main.MarketDataClient, symbols: list[str]
    ) -> tuple[list[InstrumentInfo], list[str]]:
        return [InstrumentInfo(instrument_id="id", symbol="SBER", tick_size=0.01)], []

    monkeypatch.setattr(app_main.MarketDataClient, "resolve_instruments", fake_resolve)

    report, fatal = asyncio.run(app_main.build_doctor_report([], None))

    assert not fatal
    assert os.environ.get("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH") == str(ca_path)
    assert any(
        name == "ca_bundle"
        and ok
        and f"GRPC_DEFAULT_SSL_ROOTS_FILE_PATH={ca_path}" in message
        for name, ok, message in report
    )


def test_run_monitor_async_windows_skips_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_main.sys, "platform", "win32")

    settings = SimpleNamespace(
        token="token",
        log_level=20,
        retry_backoff_initial_seconds=0.0,
        retry_backoff_max_seconds=0.0,
        stream_idle_sleep_seconds=0.0,
        instrument_status=None,
    )
    monkeypatch.setattr(app_main, "load_env_settings", lambda: settings)
    monkeypatch.setattr(app_main, "ensure_required_env", lambda _: None)
    monkeypatch.setattr(app_main, "configure_grpc_root_certificates", lambda *_: None)
    monkeypatch.setattr(app_main, "load_app_config", lambda _: AppConfig())

    class DummyClient:
        def __init__(self, **_: object) -> None:
            return

        async def resolve_instruments(self, symbols: list[str]) -> tuple[list[InstrumentInfo], list[str]]:
            return [InstrumentInfo(instrument_id="id", symbol=symbols[0], tick_size=0.01)], []

        async def stream_market_data(self, *, stop_event: asyncio.Event, **_: object) -> None:
            stop_event.set()

    monkeypatch.setattr(app_main, "MarketDataClient", DummyClient)

    async def _run() -> None:
        loop = asyncio.get_running_loop()

        def _fail_signal_handler(*_: object) -> None:
            raise AssertionError("signal handler should not be registered on Windows")

        monkeypatch.setattr(loop, "add_signal_handler", _fail_signal_handler)
        await app_main.run_monitor_async(["--symbols", "SBER"])

    asyncio.run(_run())
