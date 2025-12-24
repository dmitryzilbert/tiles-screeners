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
export tinvest_token="..."
wallwatch run --symbols SBER,GAZP --depth 20 --config config.yaml
```

Token is read from `tinvest_token` (or legacy `invest_token`). Uppercase variants are deprecated but still supported.

## Environment variables

- `tinvest_token` (REQUIRED): gRPC token for T-Invest.
- `invest_token` (OPTIONAL): legacy fallback token name (use only if `tinvest_token` is unset).
- `tinvest_ca_bundle_path` (OPTIONAL): path to a PEM-encoded CA bundle for gRPC TLS.
- `tinvest_ca_bundle_b64` (OPTIONAL): base64-encoded PEM bundle for gRPC TLS.
- `wallwatch_retry_backoff_initial_seconds` (OPTIONAL, default `1.0`): initial retry backoff for reconnects.
- `wallwatch_retry_backoff_max_seconds` (OPTIONAL, default `30.0`): maximum retry backoff for reconnects.
- `wallwatch_stream_idle_sleep_seconds` (OPTIONAL, default `3600.0`): idle sleep between stream keep-alives.
- `tg_bot_token` (REQUIRED for Telegram mode): Telegram bot token.
- `tg_chat_id` (REQUIRED for Telegram mode): chat id(s) for alerts (comma-separated for multiple).
- `tg_allowed_user_ids` (OPTIONAL): comma-separated user ids allowed to use commands.
- `tg_polling` (OPTIONAL, default `true`): enable polling mode.
- `tg_parse_mode` (OPTIONAL, default `HTML`): parse mode (`HTML` or `MarkdownV2`).

## Config

Example `config.yaml`:

```yaml
max_symbols: 10
depth: 20
distance_ticks: 10
k_ratio: 10
abs_qty_threshold: 0

dwell_seconds: 30
reposition_window_seconds: 3
reposition_ticks: 1
reposition_similar_pct: 0.2
reposition_max: 1

trades_window_seconds: 20
Emin: 200
Amin: 0.2
cancel_share_max: 0.7

consuming_drop_pct: 0.2
consuming_window_seconds: 8
min_exec_confirm: 50

cooldown_confirmed_seconds: 120
cooldown_consuming_seconds: 45
```

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
- Or provide a custom CA bundle:
  ```bash
  export tinvest_ca_bundle_path=/run/secrets/ca.pem
  # or
  export tinvest_ca_bundle_b64="$(base64 -w0 /run/secrets/ca.pem)"
  ```
