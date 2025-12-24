from __future__ import annotations

import asyncio
import base64

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
