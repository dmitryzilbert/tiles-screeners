from __future__ import annotations

import os
from pathlib import Path

import pytest

from wallwatch.app.config import CABundleError, ConfigError, EnvSettings, ensure_required_env, load_ca_bundle


def _settings(**overrides: object) -> EnvSettings:
    data = {
        "token": "token",
        "ca_bundle_path": None,
        "ca_bundle_b64": None,
        "log_level": 20,
        "retry_backoff_initial_seconds": 1.0,
        "retry_backoff_max_seconds": 30.0,
        "stream_idle_sleep_seconds": 3600.0,
        "tg_bot_token": None,
        "tg_chat_ids": [],
        "tg_allowed_user_ids": set(),
        "tg_polling": True,
        "tg_parse_mode": "HTML",
    }
    data.update(overrides)
    return EnvSettings(**data)


def test_missing_token_raises() -> None:
    settings = _settings(token=None)
    with pytest.raises(ConfigError, match="tinvest_token"):
        ensure_required_env(settings)


def test_ca_bundle_b64_invalid() -> None:
    settings = _settings(ca_bundle_b64="not-base64@@@")
    with pytest.raises(CABundleError, match="base64"):
        load_ca_bundle(settings)


def test_ca_bundle_path_missing(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.pem"
    settings = _settings(ca_bundle_path=str(missing_path))
    with pytest.raises(CABundleError, match="not found"):
        load_ca_bundle(settings)


def test_ca_bundle_path_empty(tmp_path: Path) -> None:
    empty_path = tmp_path / "empty.pem"
    empty_path.write_text("")
    settings = _settings(ca_bundle_path=str(empty_path))
    with pytest.raises(CABundleError, match="empty"):
        load_ca_bundle(settings)


def test_ca_bundle_path_not_readable(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("chmod permissions are not reliable on Windows")
    ca_path = tmp_path / "ca.pem"
    ca_path.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    ca_path.chmod(0)
    if os.access(ca_path, os.R_OK):
        pytest.skip("chmod permissions are not enforced in this environment")
    settings = _settings(ca_bundle_path=str(ca_path))
    with pytest.raises(CABundleError, match="not readable"):
        load_ca_bundle(settings)
    ca_path.chmod(0o600)
