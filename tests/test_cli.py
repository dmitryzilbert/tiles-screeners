from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

import pytest

from wallwatch.app import main as app_main
from wallwatch.api.client import InstrumentInfo


def test_run_parser_requires_symbols() -> None:
    parser = app_main._build_run_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


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
