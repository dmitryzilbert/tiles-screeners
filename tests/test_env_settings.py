from __future__ import annotations

import logging

from wallwatch.app import main as app_main
from wallwatch.app.config import load_env_settings


def test_load_env_settings_lowercase(monkeypatch) -> None:
    monkeypatch.setenv("tinvest_token", "token")
    monkeypatch.setenv("wallwatch_retry_backoff_initial_seconds", "2.5")

    settings = load_env_settings()

    assert settings.token == "token"
    assert settings.retry_backoff_initial_seconds == 2.5


def test_load_env_settings_uppercase_no_warn_by_default(monkeypatch, caplog) -> None:
    monkeypatch.setenv("TINVEST_TOKEN", "token")

    with caplog.at_level(logging.WARNING):
        settings = load_env_settings()

    assert settings.token == "token"
    warnings = [record for record in caplog.records if record.getMessage() == "deprecated_uppercase_env"]
    assert not warnings


def test_load_env_settings_uppercase_warns(monkeypatch, caplog) -> None:
    monkeypatch.setenv("TINVEST_TOKEN", "token")

    with caplog.at_level(logging.WARNING):
        settings = load_env_settings(warn_deprecated_env=True)

    assert settings.token == "token"
    warnings = [record for record in caplog.records if record.getMessage() == "deprecated_uppercase_env"]
    assert len(warnings) == 1
    assert "TINVEST_TOKEN" in warnings[0].__dict__.get("variables", [])


def test_load_env_settings_from_dotenv(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    dotenv_path = project_root / ".env"
    dotenv_path.write_text('tinvest_token="dotenv-token"\n', encoding="utf-8")
    workdir = project_root / "nested"
    workdir.mkdir()

    monkeypatch.delenv("tinvest_token", raising=False)
    monkeypatch.chdir(workdir)

    app_main._load_dotenv()
    settings = load_env_settings()

    assert settings.token == "dotenv-token"
