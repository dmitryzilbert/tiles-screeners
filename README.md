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
export TINVEST_TOKEN="..."
wallwatch --symbols SBER,GAZP --depth 20 --config config.yaml
```

Token is read from `TINVEST_TOKEN` (or `INVEST_TOKEN`).

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
