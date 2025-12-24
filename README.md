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
export TINVEST_TOKEN="..."
wallwatch --symbols SBER,GAZP --depth 20 --config config.yaml
```

Token is read from `TINVEST_TOKEN` (or `INVEST_TOKEN`).

## Environment variables

- `TINVEST_TOKEN` (REQUIRED): gRPC token for T-Invest.
- `INVEST_TOKEN` (OPTIONAL): legacy fallback token name (use only if `TINVEST_TOKEN` is unset).
- `TINVEST_CA_BUNDLE_PATH` (OPTIONAL): path to a PEM-encoded CA bundle for gRPC TLS.
- `TINVEST_CA_BUNDLE_B64` (OPTIONAL): base64-encoded PEM bundle for gRPC TLS.
- `WALLWATCH_RETRY_BACKOFF_INITIAL_SECONDS` (OPTIONAL, default `1.0`): initial retry backoff for reconnects.
- `WALLWATCH_RETRY_BACKOFF_MAX_SECONDS` (OPTIONAL, default `30.0`): maximum retry backoff for reconnects.
- `WALLWATCH_STREAM_IDLE_SLEEP_SECONDS` (OPTIONAL, default `3600.0`): idle sleep between stream keep-alives.

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
wallwatch doctor --symbols SBER,GAZP
```

Doctor validates required environment variables, CA bundle configuration, and resolves instruments.

## Deployment notes

For container images with minimal OS packages:

- Install system certificates (recommended):
  ```bash
  apt-get update && apt-get install -y ca-certificates
  ```
- Or provide a custom CA bundle:
  ```bash
  export TINVEST_CA_BUNDLE_PATH=/run/secrets/ca.pem
  # or
  export TINVEST_CA_BUNDLE_B64="$(base64 -w0 /run/secrets/ca.pem)"
  ```
