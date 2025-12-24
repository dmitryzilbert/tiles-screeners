from __future__ import annotations

import base64
import binascii
import importlib.util
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from wallwatch.detector.wall_detector import DetectorConfig


class ConfigError(ValueError):
    pass


class CABundleError(ValueError):
    pass


@dataclass(frozen=True)
class EnvSettings:
    token: str | None
    ca_bundle_path: str | None
    ca_bundle_b64: str | None
    retry_backoff_initial_seconds: float
    retry_backoff_max_seconds: float
    stream_idle_sleep_seconds: float
    tg_bot_token: str | None
    tg_chat_ids: list[int]
    tg_allowed_user_ids: set[int]
    tg_polling: bool
    tg_parse_mode: str


def load_env_settings() -> EnvSettings:
    logger = logging.getLogger("wallwatch")
    used_uppercase: list[str] = []

    token, warnings = _get_env_value("tinvest_token", legacy_names=["invest_token"])
    used_uppercase.extend(warnings)
    ca_bundle_path, warnings = _get_env_value("tinvest_ca_bundle_path")
    used_uppercase.extend(warnings)
    ca_bundle_b64, warnings = _get_env_value("tinvest_ca_bundle_b64")
    used_uppercase.extend(warnings)
    retry_backoff_initial_seconds, warnings = _parse_float_env(
        "wallwatch_retry_backoff_initial_seconds", 1.0
    )
    used_uppercase.extend(warnings)
    retry_backoff_max_seconds, warnings = _parse_float_env(
        "wallwatch_retry_backoff_max_seconds", 30.0
    )
    used_uppercase.extend(warnings)
    stream_idle_sleep_seconds, warnings = _parse_float_env(
        "wallwatch_stream_idle_sleep_seconds", 3600.0
    )
    used_uppercase.extend(warnings)
    tg_bot_token, warnings = _get_env_value("tg_bot_token")
    used_uppercase.extend(warnings)
    tg_chat_ids, warnings = _parse_int_list_env("tg_chat_id")
    used_uppercase.extend(warnings)
    tg_allowed_user_ids, warnings = _parse_int_list_env("tg_allowed_user_ids")
    used_uppercase.extend(warnings)
    tg_polling, warnings = _parse_bool_env("tg_polling", True)
    used_uppercase.extend(warnings)
    tg_parse_mode, warnings = _parse_parse_mode_env("tg_parse_mode", "HTML")
    used_uppercase.extend(warnings)

    if used_uppercase:
        logger.warning(
            "deprecated_uppercase_env",
            extra={"variables": sorted(set(used_uppercase))},
        )
    return EnvSettings(
        token=token,
        ca_bundle_path=ca_bundle_path,
        ca_bundle_b64=ca_bundle_b64,
        retry_backoff_initial_seconds=retry_backoff_initial_seconds,
        retry_backoff_max_seconds=retry_backoff_max_seconds,
        stream_idle_sleep_seconds=stream_idle_sleep_seconds,
        tg_bot_token=tg_bot_token,
        tg_chat_ids=tg_chat_ids,
        tg_allowed_user_ids=set(tg_allowed_user_ids),
        tg_polling=tg_polling,
        tg_parse_mode=tg_parse_mode,
    )


def missing_required_env(settings: EnvSettings) -> list[str]:
    missing = []
    if not settings.token:
        missing.append("tinvest_token")
    return missing


def ensure_required_env(settings: EnvSettings) -> None:
    missing = missing_required_env(settings)
    if missing:
        raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")


def load_detector_config(path: Path | None) -> DetectorConfig:
    if path is None:
        return DetectorConfig()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        content = yaml.safe_load(path.read_text()) or {}
    except OSError as exc:
        raise ConfigError(f"Unable to read config file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in config file: {path}") from exc
    return DetectorConfig(**content)


def load_ca_bundle(settings: EnvSettings) -> bytes | None:
    if settings.ca_bundle_b64:
        return _load_ca_bundle_b64(settings.ca_bundle_b64)
    if settings.ca_bundle_path:
        return _load_ca_bundle_path(settings.ca_bundle_path)
    return None


def resolve_root_certificates(settings: EnvSettings) -> bytes | None:
    bundle = load_ca_bundle(settings)
    if bundle is not None:
        return bundle
    if importlib.util.find_spec("certifi") is None:
        return None
    import certifi

    ca_path = Path(certifi.where())
    if not ca_path.exists():
        return None
    try:
        data = ca_path.read_bytes()
    except OSError:
        return None
    return data if _looks_like_pem(data) else None


def _load_ca_bundle_b64(value: str) -> bytes:
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CABundleError("tinvest_ca_bundle_b64 is not valid base64") from exc
    if not data:
        raise CABundleError("tinvest_ca_bundle_b64 decoded to empty content")
    if not _looks_like_pem(data):
        raise CABundleError("tinvest_ca_bundle_b64 does not look like PEM data")
    return data


def _load_ca_bundle_path(value: str) -> bytes:
    path = Path(value)
    if not path.exists():
        raise CABundleError(f"tinvest_ca_bundle_path not found: {path}")
    if not path.is_file():
        raise CABundleError(f"tinvest_ca_bundle_path is not a file: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CABundleError(f"tinvest_ca_bundle_path is not readable: {path}") from exc
    if not data:
        raise CABundleError(f"tinvest_ca_bundle_path is empty: {path}")
    if not _looks_like_pem(data):
        raise CABundleError(f"tinvest_ca_bundle_path does not look like PEM: {path}")
    return data


def _looks_like_pem(data: bytes) -> bool:
    return b"-----BEGIN" in data and b"-----END" in data


def _get_env_value(name: str, legacy_names: list[str] | None = None) -> tuple[str | None, list[str]]:
    legacy_names = legacy_names or []
    candidates = [name, name.upper()]
    for legacy in legacy_names:
        candidates.append(legacy)
        candidates.append(legacy.upper())
    for env_name in candidates:
        raw = _clean_env_value(os.getenv(env_name))
        if raw is not None:
            warnings = [env_name] if env_name.isupper() else []
            return raw, warnings
    return None, []


def _parse_float_env(name: str, default: float) -> tuple[float, list[str]]:
    raw, warnings = _get_env_value(name)
    if raw is None:
        return default, []
    try:
        return float(raw), warnings
    except ValueError as exc:
        raise ConfigError(f"{name} must be a float, got {raw!r}") from exc


def _parse_bool_env(name: str, default: bool) -> tuple[bool, list[str]]:
    raw, warnings = _get_env_value(name)
    if raw is None:
        return default, []
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True, warnings
    if value in {"0", "false", "no", "n", "off"}:
        return False, warnings
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _parse_int_list_env(name: str) -> tuple[list[int], list[str]]:
    raw, warnings = _get_env_value(name)
    if raw is None:
        return [], []
    values: list[int] = []
    for item in raw.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        try:
            values.append(int(cleaned))
        except ValueError as exc:
            raise ConfigError(f"{name} must be a comma-separated list of integers") from exc
    return values, warnings


def _parse_parse_mode_env(name: str, default: str) -> tuple[str, list[str]]:
    raw, warnings = _get_env_value(name)
    if raw is None:
        return default, []
    if raw not in {"HTML", "MarkdownV2"}:
        raise ConfigError(f"{name} must be HTML or MarkdownV2, got {raw!r}")
    return raw, warnings


def _clean_env_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
