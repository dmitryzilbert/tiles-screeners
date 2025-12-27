from __future__ import annotations

import os
from pathlib import Path

import pytest

from wallwatch.app.config import (
    CABundleError,
    ConfigError,
    EnvSettings,
    ensure_required_env,
    load_app_config,
    load_ca_bundle,
    resolve_depth,
    resolve_log_level,
)


def _settings(**overrides: object) -> EnvSettings:
    data = {
        "token": "token",
        "ca_bundle_path": None,
        "ca_bundle_b64": None,
        "log_level": 20,
        "retry_backoff_initial_seconds": 1.0,
        "retry_backoff_max_seconds": 30.0,
        "stream_idle_sleep_seconds": 3600.0,
        "grpc_endpoint": None,
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


def test_load_app_config_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
logging:
  level: DEBUG
walls:
  candidate_max_distance_ticks: 2
  confirm_dwell_seconds: 3.0
""".lstrip()
    )

    config = load_app_config(config_path)
    detector_config = config.detector_config()

    assert config.logging.level == 10
    assert detector_config.distance_ticks == 2
    assert detector_config.dwell_seconds == 3.0


def test_cli_priority_overrides_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
logging:
  level: DEBUG
marketdata:
  depth: 20
""".lstrip()
    )

    config = load_app_config(config_path)

    depth = resolve_depth(50, config.marketdata.depth)
    log_level = resolve_log_level("INFO", config.logging.level, 20)

    assert depth == 50
    assert log_level == 20


def test_walls_config_from_yaml_overrides_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
walls:
  candidate_ratio_to_median: 123
""".lstrip()
    )

    config = load_app_config(config_path)

    assert config.walls.candidate_ratio_to_median == 123.0


def test_telegram_config_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
telegram:
  enabled: true
  polling: false
  poll_interval_seconds: 2.0
  startup_message: true
  send_events: [wall_candidate]
  cooldown_seconds:
    wall_candidate: 10
  disable_web_preview: false
  commands_enabled: false
""".lstrip()
    )

    config = load_app_config(config_path)

    assert config.telegram.enabled is True
    assert config.telegram.polling is False
    assert config.telegram.poll_interval_seconds == 2.0
    assert config.telegram.startup_message is True
    assert config.telegram.send_events == ("wall_candidate",)
    assert config.telegram.cooldown_seconds["wall_candidate"] == 10.0
    assert config.telegram.disable_web_preview is False
    assert config.telegram.commands_enabled is False


def test_unknown_config_keys_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
unknown_root: 1
walls:
  candidate_ratio_to_median: 5
  weird: 2
""".lstrip()
    )

    with caplog.at_level("WARNING"):
        _ = load_app_config(config_path)

    matches = [record for record in caplog.records if record.message == "unknown_config_keys"]
    assert matches
    keys = matches[-1].__dict__.get("keys")
    assert "unknown_root" in keys
    assert "walls.weird" in keys
