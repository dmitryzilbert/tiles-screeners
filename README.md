# WallWatch

Monitoring order-book walls via T-Invest gRPC (T-Bank / T-Investments). Reads market data only — no trading.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

```bash
cp .env.example .env
# edit .env
wallwatch run --symbols SBER,GAZP --depth 20 --config config.yaml
```

Example `.env`:

```dotenv
tinvest_token="..."
```

`.env` is loaded automatically from the current working directory (or its parent project root).
Token is read from `tinvest_token` (or legacy `invest_token`). Uppercase variants are deprecated but still supported; set
`warn_deprecated_env=1` to emit a one-time warning during startup.

## Environment variables

- `tinvest_token` (REQUIRED): gRPC token for T-Invest.
- `invest_token` (OPTIONAL): legacy fallback token name (use only if `tinvest_token` is unset).
- `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` (OPTIONAL, recommended for deployments): path to a PEM-encoded CA bundle for gRPC TLS.
- `tinvest_ca_bundle_path` (OPTIONAL): path to a PEM-encoded CA bundle for gRPC TLS.
- `tinvest_ca_bundle_b64` (OPTIONAL): base64-encoded PEM bundle for gRPC TLS.
- `wallwatch_retry_backoff_initial_seconds` (OPTIONAL, default `1.0`): initial retry backoff for reconnects.
- `wallwatch_retry_backoff_max_seconds` (OPTIONAL, default `30.0`): maximum retry backoff for reconnects.
- `wallwatch_stream_idle_sleep_seconds` (OPTIONAL, default `3600.0`): idle sleep between stream keep-alives.
- `wallwatch_instrument_status` (OPTIONAL, default `BASE`): instrument status for instrument lookup (`BASE` or `ALL`).
- `tg_bot_token` (REQUIRED for Telegram mode): Telegram bot token.
- `tg_chat_id` (REQUIRED for Telegram mode): chat id(s) for alerts (comma-separated for multiple).
- `tg_allowed_user_ids` (OPTIONAL): comma-separated user ids allowed to use commands.
- `tg_polling` (OPTIONAL, default `true`): enable polling mode.
- `tg_parse_mode` (OPTIONAL, default `HTML`): parse mode (`HTML` or `MarkdownV2`).
- `warn_deprecated_env` (OPTIONAL, default `false`): emit a one-time warning if uppercase env keys are used.

## Config

Use a YAML config to tune detector thresholds without code changes. Secrets stay in `.env`.

Example `config.yaml` (see `config.example.yaml`):

```yaml
logging:
  level: INFO

marketdata:
  depth: 20

walls:
  top_n_levels: 5
  candidate_ratio_to_median: 12.0
  candidate_max_distance_ticks: 2
  confirm_dwell_seconds: 3.0
  confirm_max_distance_ticks: 1
  consume_window_seconds: 3.0
  consume_drop_pct: 25.0
  teleport_reset: true

debug:
  walls_enabled: false
  walls_interval_seconds: 1.0
```

### Config priority

Priority order for overlapping settings:

1. CLI flags (explicitly passed)
2. YAML config (`--config`)
3. Environment variables / defaults

`marketdata.depth` is applied unless `--depth` is provided. `logging.level` is applied unless `--log-level` is provided.

## Calibration guidance

- `k_ratio`: start with 8–15. Higher values reduce false positives but may miss medium walls.
- `dwell_seconds`: 20–60 seconds. Longer dwell reduces spoofing but delays alerts.
- `Emin`: minimum executed volume at the wall price to confirm authenticity.
- `cancel_share_max`: set around 0.6–0.8. Lower values require more execution evidence.
- `consuming_drop_pct`: 15–30%. Increase to reduce frequent “consuming” alerts.
- `distance_ticks`: 1–10. Smaller values focus on near-touch walls.

## Architecture

- `api/client.py`: gRPC client, instrument resolution, subscriptions.
- `detector/wall_detector.py`: wall detection logic (pure, no I/O).
- `state/models.py`: dataclasses for events and state.
- `notify/notifier.py`: alert interface + console implementation.
- `app/main.py`: CLI + runtime orchestration.

## Testing

```bash
pytest
```

## Preflight checks

```bash
wallwatch doctor
wallwatch doctor --symbols SBER,GAZP
```

Doctor validates required environment variables, CA bundle configuration, and resolves instruments. In normal mode the
`--symbols` flag is required, while in doctor mode symbols are optional.

## Telegram interface

Examples:

```bash
# Install Telegram extra
pip install -e ".[telegram]"

# CLI monitoring only
wallwatch run --symbols SBER,GAZP --depth 20 --config config.yaml

# Telegram interface (commands + monitoring)
wallwatch telegram --symbols SBER,GAZP --config config.yaml

# Telegram interface + alerts (same mode; alerts go to tg_chat_id)
wallwatch telegram --symbols SBER,GAZP --config config.yaml
```

## Deployment notes

For container images with minimal OS packages:

- Install system certificates (recommended):
  ```bash
  apt-get update && apt-get install -y ca-certificates
  ```
- Or provide a custom CA bundle (recommended to use the gRPC env var directly):
  ```bash
  export GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/run/secrets/ca.pem
  # or use the wallwatch convenience wrapper:
  export tinvest_ca_bundle_path=/run/secrets/ca.pem
  # or
  export tinvest_ca_bundle_b64="$(base64 -w0 /run/secrets/ca.pem)"
  ```
