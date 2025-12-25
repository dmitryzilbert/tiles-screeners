from __future__ import annotations

import base64
import binascii
import importlib.util
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from t_tech.invest import schemas

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
    instrument_status: schemas.InstrumentStatus = schemas.InstrumentStatus.INSTRUMENT_STATUS_BASE


GRPC_ROOTS_ENV_VAR = "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"
_DEPRECATED_UPPERCASE_WARNED = False


def load_env_settings(warn_deprecated_env: bool | None = None) -> EnvSettings:
    warn_deprecated_env = _resolve_warn_deprecated_env(warn_deprecated_env)
    token = _get_env_value(
        "tinvest_token",
        legacy_names=["invest_token"],
        warn_deprecated_env=warn_deprecated_env,
    )
    ca_bundle_path = _get_env_value(
        "tinvest_ca_bundle_path",
        warn_deprecated_env=warn_deprecated_env,
    )
    ca_bundle_b64 = _get_env_value(
        "tinvest_ca_bundle_b64",
        warn_deprecated_env=warn_deprecated_env,
    )
    retry_backoff_initial_seconds = _parse_float_env(
        "wallwatch_retry_backoff_initial_seconds",
        1.0,
        warn_deprecated_env=warn_deprecated_env,
    )
    retry_backoff_max_seconds = _parse_float_env(
        "wallwatch_retry_backoff_max_seconds",
        30.0,
        warn_deprecated_env=warn_deprecated_env,
    )
    stream_idle_sleep_seconds = _parse_float_env(
        "wallwatch_stream_idle_sleep_seconds",
        3600.0,
        warn_deprecated_env=warn_deprecated_env,
    )
    instrument_status = _parse_instrument_status_env(
        "wallwatch_instrument_status",
        schemas.InstrumentStatus.INSTRUMENT_STATUS_BASE,
        warn_deprecated_env=warn_deprecated_env,
    )
    tg_bot_token = _get_env_value("tg_bot_token", warn_deprecated_env=warn_deprecated_env)
    tg_chat_ids = _parse_int_list_env("tg_chat_id", warn_deprecated_env=warn_deprecated_env)
    tg_allowed_user_ids = _parse_int_list_env(
        "tg_allowed_user_ids",
        warn_deprecated_env=warn_deprecated_env,
    )
    tg_polling = _parse_bool_env("tg_polling", True, warn_deprecated_env=warn_deprecated_env)
    tg_parse_mode = _parse_parse_mode_env(
        "tg_parse_mode",
        "HTML",
        warn_deprecated_env=warn_deprecated_env,
    )
    return EnvSettings(
        token=token,
        ca_bundle_path=ca_bundle_path,
        ca_bundle_b64=ca_bundle_b64,
        retry_backoff_initial_seconds=retry_backoff_initial_seconds,
        retry_backoff_max_seconds=retry_backoff_max_seconds,
        stream_idle_sleep_seconds=stream_idle_sleep_seconds,
        instrument_status=instrument_status,
        tg_bot_token=tg_bot_token,
        tg_chat_ids=tg_chat_ids,
        tg_allowed_user_ids=set(tg_allowed_user_ids),
        tg_polling=tg_polling,
        tg_parse_mode=tg_parse_mode,
    )


def configure_grpc_root_certificates(settings: EnvSettings, logger: logging.Logger) -> str | None:
    if settings.ca_bundle_b64:
        data = _load_ca_bundle_b64(settings.ca_bundle_b64)
        path = _write_temp_pem(data)
    elif settings.ca_bundle_path:
        _load_ca_bundle_path(settings.ca_bundle_path)
        path = settings.ca_bundle_path
    else:
        return None
    os.environ[GRPC_ROOTS_ENV_VAR] = path
    logger.info(
        "custom_ca_bundle_enabled",
        extra={"env_var": GRPC_ROOTS_ENV_VAR, "path": path},
    )
    return path


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


def _write_temp_pem(data: bytes) -> str:
    with tempfile.NamedTemporaryFile(prefix="wallwatch-ca-", suffix=".pem", delete=False) as handle:
        handle.write(data)
        return handle.name


def _looks_like_pem(data: bytes) -> bool:
    return b"-----BEGIN" in data and b"-----END" in data


def has_exact_env_key(name: str) -> bool:
    return any(key == name for key in os.environ.keys())


def get_env_with_deprecated_uppercase(
    lower: str,
    upper: str,
    logger: logging.Logger,
    warn_code: str,
    warn_deprecated_env: bool,
) -> str | None:
    global _DEPRECATED_UPPERCASE_WARNED
    if (
        warn_deprecated_env
        and not _DEPRECATED_UPPERCASE_WARNED
        and has_exact_env_key(upper)
        and not has_exact_env_key(lower)
    ):
        _DEPRECATED_UPPERCASE_WARNED = True
        logger.warning(warn_code, extra={"variables": [upper]})
    return os.getenv(lower) or os.getenv(upper)


def _get_env_value(
    name: str,
    legacy_names: list[str] | None = None,
    logger: logging.Logger | None = None,
    warn_code: str = "deprecated_uppercase_env",
    warn_deprecated_env: bool = False,
) -> str | None:
    legacy_names = legacy_names or []
    logger = logger or logging.getLogger("wallwatch")
    raw = _clean_env_value(
        get_env_with_deprecated_uppercase(
            name,
            name.upper(),
            logger,
            warn_code,
            warn_deprecated_env,
        )
    )
    if raw is not None:
        return raw
    for legacy in legacy_names:
        raw = _clean_env_value(
            get_env_with_deprecated_uppercase(
                legacy,
                legacy.upper(),
                logger,
                warn_code,
                warn_deprecated_env,
            )
        )
        if raw is not None:
            return raw
    return None


def _parse_float_env(name: str, default: float, warn_deprecated_env: bool = False) -> float:
    raw = _get_env_value(name, warn_deprecated_env=warn_deprecated_env)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a float, got {raw!r}") from exc


def _parse_bool_env(name: str, default: bool, warn_deprecated_env: bool = False) -> bool:
    raw = _get_env_value(name, warn_deprecated_env=warn_deprecated_env)
    if raw is None:
        return default
    return _parse_bool_value(name, raw)


def _parse_int_list_env(name: str, warn_deprecated_env: bool = False) -> list[int]:
    raw = _get_env_value(name, warn_deprecated_env=warn_deprecated_env)
    if raw is None:
        return []
    values: list[int] = []
    for item in raw.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        try:
            values.append(int(cleaned))
        except ValueError as exc:
            raise ConfigError(f"{name} must be a comma-separated list of integers") from exc
    return values


def _parse_parse_mode_env(name: str, default: str, warn_deprecated_env: bool = False) -> str:
    raw = _get_env_value(name, warn_deprecated_env=warn_deprecated_env)
    if raw is None:
        return default
    if raw not in {"HTML", "MarkdownV2"}:
        raise ConfigError(f"{name} must be HTML or MarkdownV2, got {raw!r}")
    return raw


def _parse_instrument_status_env(
    name: str,
    default: schemas.InstrumentStatus,
    warn_deprecated_env: bool = False,
) -> schemas.InstrumentStatus:
    raw = _get_env_value(name, warn_deprecated_env=warn_deprecated_env)
    if raw is None:
        return default
    value = raw.strip().upper()
    if value == "BASE":
        return schemas.InstrumentStatus.INSTRUMENT_STATUS_BASE
    if value == "ALL":
        return schemas.InstrumentStatus.INSTRUMENT_STATUS_ALL
    raise ConfigError(f"{name} must be BASE or ALL, got {raw!r}")


def _clean_env_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_bool_value(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _resolve_warn_deprecated_env(warn_deprecated_env: bool | None) -> bool:
    if warn_deprecated_env is not None:
        return warn_deprecated_env
    raw = os.getenv("warn_deprecated_env") or os.getenv("WARN_DEPRECATED_ENV")
    if raw is None:
        return False
    return _parse_bool_value("warn_deprecated_env", raw)
