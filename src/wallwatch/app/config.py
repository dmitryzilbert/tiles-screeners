from __future__ import annotations

import base64
import binascii
import importlib.util
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


def load_env_settings() -> EnvSettings:
    token = os.getenv("TINVEST_TOKEN") or os.getenv("INVEST_TOKEN")
    ca_bundle_path = _clean_env_value(os.getenv("TINVEST_CA_BUNDLE_PATH"))
    ca_bundle_b64 = _clean_env_value(os.getenv("TINVEST_CA_BUNDLE_B64"))
    retry_backoff_initial_seconds = _parse_float_env(
        "WALLWATCH_RETRY_BACKOFF_INITIAL_SECONDS", 1.0
    )
    retry_backoff_max_seconds = _parse_float_env(
        "WALLWATCH_RETRY_BACKOFF_MAX_SECONDS", 30.0
    )
    stream_idle_sleep_seconds = _parse_float_env("WALLWATCH_STREAM_IDLE_SLEEP_SECONDS", 3600.0)
    return EnvSettings(
        token=token,
        ca_bundle_path=ca_bundle_path,
        ca_bundle_b64=ca_bundle_b64,
        retry_backoff_initial_seconds=retry_backoff_initial_seconds,
        retry_backoff_max_seconds=retry_backoff_max_seconds,
        stream_idle_sleep_seconds=stream_idle_sleep_seconds,
    )


def missing_required_env(settings: EnvSettings) -> list[str]:
    missing = []
    if not settings.token:
        missing.append("TINVEST_TOKEN")
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
        raise CABundleError("TINVEST_CA_BUNDLE_B64 is not valid base64") from exc
    if not data:
        raise CABundleError("TINVEST_CA_BUNDLE_B64 decoded to empty content")
    if not _looks_like_pem(data):
        raise CABundleError("TINVEST_CA_BUNDLE_B64 does not look like PEM data")
    return data


def _load_ca_bundle_path(value: str) -> bytes:
    path = Path(value)
    if not path.exists():
        raise CABundleError(f"TINVEST_CA_BUNDLE_PATH not found: {path}")
    if not path.is_file():
        raise CABundleError(f"TINVEST_CA_BUNDLE_PATH is not a file: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CABundleError(f"TINVEST_CA_BUNDLE_PATH is not readable: {path}") from exc
    if not data:
        raise CABundleError(f"TINVEST_CA_BUNDLE_PATH is empty: {path}")
    if not _looks_like_pem(data):
        raise CABundleError(f"TINVEST_CA_BUNDLE_PATH does not look like PEM: {path}")
    return data


def _looks_like_pem(data: bytes) -> bool:
    return b"-----BEGIN" in data and b"-----END" in data


def _parse_float_env(name: str, default: float) -> float:
    raw = _clean_env_value(os.getenv(name))
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a float, got {raw!r}") from exc


def _clean_env_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
