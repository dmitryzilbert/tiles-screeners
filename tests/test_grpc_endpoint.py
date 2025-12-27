from __future__ import annotations

import logging

from wallwatch.app.config import DEFAULT_GRPC_ENDPOINT, load_env_settings, resolve_grpc_endpoint


def test_default_grpc_endpoint_uses_tbank(monkeypatch) -> None:
    monkeypatch.delenv("tinvest_grpc_endpoint", raising=False)
    monkeypatch.delenv("invest_grpc_endpoint", raising=False)

    settings = load_env_settings()
    endpoint = resolve_grpc_endpoint(settings, logging.getLogger("test"))

    assert "tbank.ru" in endpoint
    assert "tinkoff.ru" not in endpoint
    assert endpoint == DEFAULT_GRPC_ENDPOINT


def test_deprecated_grpc_endpoint_warns(monkeypatch, caplog) -> None:
    monkeypatch.setenv("tinvest_grpc_endpoint", "invest-public-api.tinkoff.ru:443")

    settings = load_env_settings()
    with caplog.at_level(logging.WARNING):
        endpoint = resolve_grpc_endpoint(settings, logging.getLogger("test"))

    assert endpoint == "invest-public-api.tinkoff.ru:443"
    assert any(
        record.getMessage() == "deprecated_endpoint_domain" for record in caplog.records
    )
