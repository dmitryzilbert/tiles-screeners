from __future__ import annotations

import logging

from wallwatch.app.config import load_env_settings


def test_load_env_settings_lowercase(monkeypatch) -> None:
    monkeypatch.setenv("tinvest_token", "token")
    monkeypatch.setenv("wallwatch_retry_backoff_initial_seconds", "2.5")

    settings = load_env_settings()

    assert settings.token == "token"
    assert settings.retry_backoff_initial_seconds == 2.5


def test_load_env_settings_uppercase_warns(monkeypatch, caplog) -> None:
    monkeypatch.setenv("TINVEST_TOKEN", "token")

    with caplog.at_level(logging.WARNING):
        settings = load_env_settings()

    assert settings.token == "token"
    warnings = [record for record in caplog.records if record.getMessage() == "deprecated_uppercase_env"]
    assert warnings
    assert "TINVEST_TOKEN" in warnings[0].__dict__.get("variables", [])
