# CSV Export — Usage Guide

`scripts/export_csv.py` exports all gold-tracker data (prices, signals, trades,
outcomes) into a single directory as CSV files ready for external analysis.

---

## Quick start

```bash
# Activate the virtual environment first
source venv/bin/activate

# Export everything at 1-hour candles
python scripts/export_csv.py --interval 1h
```

Output:
```
📦  Bundle: data/exports/all_1h
  ✅  prices.csv         156 rows
  ✅  signals.csv         30 rows
  ✅  trades.csv           4 rows
  ✅  outcomes.csv        30 rows

✔   All files written to data/exports/all_1h
```

---

## Arguments

| Argument | Required | Description |
|---|---|---|
| `--interval` | ✅ Yes | Resample interval for prices: `10min` `30min` `1h` `2h` `5h` `1d` |
| `--instrument` | No | Filter by instrument ID (e.g. `sgbs-as`). Default: all instruments. |
| `--start` | No | Start date filter, inclusive (`YYYY-MM-DD`). |
| `--end` | No | End date filter, inclusive (`YYYY-MM-DD`). |
| `--output-dir` | No | Parent directory for bundles. Default: `data/exports/`. |

---

## Examples

### All instruments, different intervals

```bash
# 10-minute candles
python scripts/export_csv.py --interval 10min

# 30-minute candles
python scripts/export_csv.py --interval 30min

# Hourly candles
python scripts/export_csv.py --interval 1h

# 2-hour candles
python scripts/export_csv.py --interval 2h

# 5-hour candles
python scripts/export_csv.py --interval 5h

# Daily candles
python scripts/export_csv.py --interval 1d
```

Each command creates its own bundle directory:
```
data/exports/
├── all_10min/
├── all_30min/
├── all_1h/
├── all_2h/
├── all_5h/
└── all_1d/
```

---

### Single instrument

```bash
python scripts/export_csv.py --interval 1h --instrument sgbs-as
python scripts/export_csv.py --interval 30min --instrument itky-as
```

Output goes to `data/exports/sgbs-as_1h/`, `data/exports/itky-as_30min/`, etc.

---

### Date range filter

```bash
# Export only the last 10 days at daily candles
python scripts/export_csv.py --interval 1d --start 2026-03-10 --end 2026-03-20

# Single instrument with date range
python scripts/export_csv.py --interval 1h --instrument sgbs-as --start 2026-03-15
```

---

### Custom output directory

```bash
python scripts/export_csv.py --interval 1h --output-dir /tmp/my-exports
# → /tmp/my-exports/all_1h/prices.csv  …
```

---

## Output files

Every bundle directory contains the same four files:

### `prices.csv`
OHLCV bars resampled to the requested interval.
Aggregation: `open=first`, `high=max`, `low=min`, `close=last`, `volume=sum`.
Empty periods (market closed) are dropped.

```
ts,instrument_id,open,high,low,close,volume
2026-03-04T08:00:00Z,sgbs-as,425.08,425.08,425.08,425.08,0.0
2026-03-04T09:00:00Z,sgbs-as,424.94,425.17,424.86,425.17,52.0
```

### `signals.csv`
All buy/sell signals with the full indicator snapshot at signal time.

```
id,instrument_id,ts,signal_type,engine,price,reason,
ma_short,ma_long,rsi,macd_line,macd_signal_line,
pct_from_buy,confidence,suggested_eur,suggested_shares
35,sgbs-as,2026-03-10T11:19:14Z,BUY,confluence,425.07,...,HIGH,60.49,3.11
```

Key columns:

| Column | Description |
|---|---|
| `signal_type` | `BUY` or `SELL` |
| `engine` | `confluence` (score-based) or `macd` (crossover) |
| `confidence` | `LOW` / `MEDIUM` / `HIGH` |
| `rsi` | RSI(14) at signal time |
| `ma_short` / `ma_long` | EMA5 / EMA21 at signal time |
| `suggested_eur` | Recommended position size in EUR |

### `trades.csv`
All manually recorded trades with post-trade position state.

```
id,instrument_id,ts,action,shares,price,fee,total_eur,
source,avg_cost_after,shares_after,notes
2,sgbs-as,2026-03-11T18:37:27Z,BUY,0.233154,433.2,-0.0023,101.0,
MANUAL,429.70,0.467763,Second buy to match broker position
```

### `outcomes.csv`
Signal outcome grades measured at 1 h, 4 h, and 24 h after the signal.

```
id,signal_id,price_1h,price_4h,price_24h,outcome,filled_at
23,31,425.28,425.55,427.59,GOOD,2026-03-11T08:45:03Z
```

| `outcome` | Meaning |
|---|---|
| `GOOD` | Signal direction was correct |
| `BAD` | Signal direction was wrong |
| `NEUTRAL` | Price moved less than 0.1% |

---

## Logs

All export runs are appended to `logs/export.log`.

```
2026-03-20T14:05:12 INFO Exported 156 rows → data/exports/all_1h/prices.csv
2026-03-20T14:05:12 INFO Exported  30 rows → data/exports/all_1h/signals.csv
```

---

## Automating with cron

To export a fresh daily bundle every night at midnight:

```cron
0 0 * * * cd /home/cdemir/gold-tracker && source venv/bin/activate && \
  python scripts/export_csv.py --interval 1d >> logs/cron.log 2>&1
```
