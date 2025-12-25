from __future__ import annotations

import base64
import binascii
import importlib.util
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    log_level: int
    retry_backoff_initial_seconds: float
    retry_backoff_max_seconds: float
    stream_idle_sleep_seconds: float
    tg_bot_token: str | None
    tg_chat_ids: list[int]
    tg_allowed_user_ids: set[int]
    tg_polling: bool
    tg_parse_mode: str
    instrument_status: schemas.InstrumentStatus = schemas.InstrumentStatus.INSTRUMENT_STATUS_BASE


@dataclass(frozen=True)
class LoggingConfig:
    level: int | None = None


@dataclass(frozen=True)
class MarketDataConfig:
    depth: int = DetectorConfig().depth


@dataclass(frozen=True)
class WallsConfig:
    top_n_levels: int = DetectorConfig().vref_levels
    candidate_ratio_to_median: float = DetectorConfig().k_ratio
    candidate_max_distance_ticks: int = DetectorConfig().distance_ticks
    confirm_dwell_seconds: float = DetectorConfig().dwell_seconds
    confirm_max_distance_ticks: int = DetectorConfig().reposition_ticks
    consume_window_seconds: float = DetectorConfig().consuming_window_seconds
    consume_drop_pct: float = DetectorConfig().consuming_drop_pct * 100.0
    teleport_reset: bool = False


@dataclass(frozen=True)
class DebugConfig:
    walls_enabled: bool = False
    walls_interval_seconds: float = 1.0


@dataclass(frozen=True)
class AppConfig:
    logging: LoggingConfig = LoggingConfig()
    marketdata: MarketDataConfig = MarketDataConfig()
    walls: WallsConfig = WallsConfig()
    debug: DebugConfig = DebugConfig()
    detector_defaults: DetectorConfig = DetectorConfig()

    def detector_config(self) -> DetectorConfig:
        base = self.detector_defaults
        walls = self.walls
        return DetectorConfig(
            **{
                **base.__dict__,
                "depth": self.marketdata.depth,
                "vref_levels": walls.top_n_levels,
                "k_ratio": walls.candidate_ratio_to_median,
                "distance_ticks": walls.candidate_max_distance_ticks,
                "dwell_seconds": walls.confirm_dwell_seconds,
                "reposition_ticks": walls.confirm_max_distance_ticks,
                "consuming_window_seconds": walls.consume_window_seconds,
                "consuming_drop_pct": walls.consume_drop_pct / 100.0,
                "teleport_reset": walls.teleport_reset,
            }
        )


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
    log_level = _parse_log_level_env(
        "log_level",
        logging.INFO,
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
        log_level=log_level,
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
    return load_app_config(path).detector_config()


def load_app_config(path: Path | None) -> AppConfig:
    if path is None:
        return AppConfig()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        content = yaml.safe_load(path.read_text()) or {}
    except OSError as exc:
        raise ConfigError(f"Unable to read config file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in config file: {path}") from exc
    if not isinstance(content, dict):
        raise ConfigError("Config root must be a mapping")
    return _parse_app_config(content)


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


def parse_log_level(value: str, name: str = "log_level") -> int:
    cleaned = value.strip()
    if not cleaned:
        raise ConfigError(f"{name} must be a valid log level, got {value!r}")
    upper = cleaned.upper()
    if upper.isdigit():
        level = int(upper)
    else:
        level = logging._nameToLevel.get(upper, -1)
    if level < 0:
        raise ConfigError(f"{name} must be a valid log level, got {value!r}")
    return level


def resolve_log_level(
    cli_value: str | None, config_level: int | None, env_level: int
) -> int:
    if cli_value is not None:
        return parse_log_level(cli_value, name="log_level")
    if config_level is not None:
        return config_level
    return env_level


def _parse_log_level_env(
    name: str,
    default: int,
    warn_deprecated_env: bool = False,
) -> int:
    raw = _get_env_value(name, warn_deprecated_env=warn_deprecated_env)
    if raw is None:
        return default
    return parse_log_level(raw, name=name)


def resolve_depth(cli_value: int | None, config_value: int) -> int:
    return config_value if cli_value is None else cli_value


def _parse_app_config(raw: dict[str, Any]) -> AppConfig:
    if not _has_config_sections(raw):
        return _parse_legacy_detector_config(raw)

    logging_section = _get_section(raw, "logging")
    marketdata_section = _get_section(raw, "marketdata")
    walls_section = _get_section(raw, "walls")
    debug_section = _get_section(raw, "debug")

    logging_config = _parse_logging_config(logging_section)
    marketdata_config = _parse_marketdata_config(marketdata_section)
    walls_config = _parse_walls_config(walls_section)
    debug_config = _parse_debug_config(debug_section)
    return AppConfig(
        logging=logging_config,
        marketdata=marketdata_config,
        walls=walls_config,
        debug=debug_config,
    )


def _has_config_sections(raw: dict[str, Any]) -> bool:
    return any(key in raw for key in ("logging", "marketdata", "walls", "debug"))


def _parse_legacy_detector_config(raw: dict[str, Any]) -> AppConfig:
    if not raw:
        return AppConfig()
    try:
        legacy = DetectorConfig(**raw)
    except TypeError as exc:
        raise ConfigError(f"Invalid config keys: {', '.join(raw.keys())}") from exc
    return _app_config_from_detector(legacy)


def _app_config_from_detector(legacy: DetectorConfig) -> AppConfig:
    return AppConfig(
        marketdata=MarketDataConfig(depth=legacy.depth),
        walls=WallsConfig(
            top_n_levels=legacy.vref_levels,
            candidate_ratio_to_median=legacy.k_ratio,
            candidate_max_distance_ticks=legacy.distance_ticks,
            confirm_dwell_seconds=legacy.dwell_seconds,
            confirm_max_distance_ticks=legacy.reposition_ticks,
            consume_window_seconds=legacy.consuming_window_seconds,
            consume_drop_pct=legacy.consuming_drop_pct * 100.0,
            teleport_reset=legacy.teleport_reset,
        ),
        detector_defaults=legacy,
    )


def _get_section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in raw or raw[key] is None:
        return {}
    section = raw[key]
    if not isinstance(section, dict):
        raise ConfigError(f"{key} section must be a mapping")
    return section


def _parse_logging_config(raw: dict[str, Any]) -> LoggingConfig:
    if not raw:
        return LoggingConfig()
    level = raw.get("level")
    if level is None:
        return LoggingConfig()
    return LoggingConfig(level=_parse_log_level_value(level, "logging.level"))


def _parse_marketdata_config(raw: dict[str, Any]) -> MarketDataConfig:
    base = MarketDataConfig()
    if not raw:
        return base
    depth = raw.get("depth", base.depth)
    return MarketDataConfig(depth=_parse_int_value(depth, "marketdata.depth"))


def _parse_walls_config(raw: dict[str, Any]) -> WallsConfig:
    base = WallsConfig()
    if not raw:
        return base
    return WallsConfig(
        top_n_levels=_parse_int_value(
            raw.get("top_n_levels", base.top_n_levels), "walls.top_n_levels"
        ),
        candidate_ratio_to_median=_parse_float_value(
            raw.get("candidate_ratio_to_median", base.candidate_ratio_to_median),
            "walls.candidate_ratio_to_median",
        ),
        candidate_max_distance_ticks=_parse_int_value(
            raw.get("candidate_max_distance_ticks", base.candidate_max_distance_ticks),
            "walls.candidate_max_distance_ticks",
        ),
        confirm_dwell_seconds=_parse_float_value(
            raw.get("confirm_dwell_seconds", base.confirm_dwell_seconds),
            "walls.confirm_dwell_seconds",
        ),
        confirm_max_distance_ticks=_parse_int_value(
            raw.get("confirm_max_distance_ticks", base.confirm_max_distance_ticks),
            "walls.confirm_max_distance_ticks",
        ),
        consume_window_seconds=_parse_float_value(
            raw.get("consume_window_seconds", base.consume_window_seconds),
            "walls.consume_window_seconds",
        ),
        consume_drop_pct=_parse_float_value(
            raw.get("consume_drop_pct", base.consume_drop_pct),
            "walls.consume_drop_pct",
        ),
        teleport_reset=_parse_bool_value_yaml(
            raw.get("teleport_reset", base.teleport_reset),
            "walls.teleport_reset",
        ),
    )


def _parse_debug_config(raw: dict[str, Any]) -> DebugConfig:
    base = DebugConfig()
    if not raw:
        return base
    return DebugConfig(
        walls_enabled=_parse_bool_value_yaml(
            raw.get("walls_enabled", base.walls_enabled),
            "debug.walls_enabled",
        ),
        walls_interval_seconds=_parse_float_value(
            raw.get("walls_interval_seconds", base.walls_interval_seconds),
            "debug.walls_interval_seconds",
        ),
    )


def _parse_log_level_value(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a valid log level")
    if isinstance(value, int):
        if value < 0:
            raise ConfigError(f"{name} must be a valid log level")
        return value
    if isinstance(value, str):
        return parse_log_level(value, name=name)
    raise ConfigError(f"{name} must be a string or integer")


def _parse_int_value(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer") from exc
    raise ConfigError(f"{name} must be an integer")


def _parse_float_value(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a float")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError as exc:
            raise ConfigError(f"{name} must be a float") from exc
    raise ConfigError(f"{name} must be a float")


def _parse_bool_value_yaml(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _parse_bool_value(name, value)
    raise ConfigError(f"{name} must be a boolean")
