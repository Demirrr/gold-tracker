# Gold Tracker 📊

Automated EMA crossover signal system that tracks ETF/ETP prices via yfinance, stores data in SQLite, evaluates multiple EMA pairs confirmed by RSI and volume, and sends Telegram reports — all running locally on a Raspberry Pi with cron.

---

## Instruments tracked

| # | Name | Ticker | ISIN | Buy price | Shares held |
|---|------|--------|------|-----------|-------------|
| 1 | WisdomTree Physical Swiss Gold | SGBS.AS | JE00B588CD74 | 413.13 EUR | 1.035996 |
| 2 | iShares MSCI Turkey UCITS ETF | ITKY.AS | IE00B1FZS574 | 18.18 EUR | 0.0 |

Both on Euronext Amsterdam (EUR). To add a new instrument see [Adding a new instrument](#adding-a-new-instrument).

---

## Architecture

```
gold-tracker/
├── data/
│   └── gold.db                  ← SQLite database (prices, signals, outcomes, trades)
├── logs/
│   ├── cron.log                 ← cron stdout/stderr
│   ├── analyzer.log             ← signal engine log
│   ├── collector.log            ← price collection log
│   └── outcomes.log             ← outcome tracking log
├── scripts/
│   ├── instruments.json         ← instrument registry (tickers, EMA config, position state)
│   ├── config.py                ← system-level config (paths, env vars)
│   ├── init_db.py               ← creates DB schema
│   ├── migrate_db.py            ← idempotent DB migration (safe to re-run)
│   ├── collect_price.py         ← fetch 2-min bars from yfinance → DB
│   ├── market_data.py           ← yfinance fetch helpers with retry / fallback logic
│   ├── analyze.py               ← EMA crossover engine + Telegram report
│   ├── track_outcomes.py        ← grade past signals (GOOD / BAD / NEUTRAL)
│   └── record_trade.py          ← record manual/executed trades, update position state
└── venv/                        ← Python virtual environment
```

---

## How it works

### Price collection

`collect_price.py` fetches OHLCV bars from Yahoo Finance via `yfinance` and inserts new rows into the `prices` table.  It tries intervals in order `2m → 5m → 15m → 1h`, with two automatic retries and an `auto_adjust` fallback, so transient Yahoo Finance issues rarely cause a missed bar.

### Signal engine — EMA ribbon + RSI + Volume

`analyze.py` evaluates **consecutive EMA pairs** derived from the `ema_periods` config key.  With the default `[5, 8, 21, 34, 50]` four pairs are evaluated:

| Pair | Character |
|------|-----------|
| EMA 5 / EMA 8 | Ultra-fast — reacts within a few bars; first to catch a new move |
| EMA 8 / EMA 21 | Fast — classic short-term day-trade trigger |
| EMA 21 / EMA 34 | Medium-short — confirms the developing trend |
| EMA 34 / EMA 50 | Medium — institutional timeframe, low false-signal rate |

A **golden cross** (short EMA rises above long EMA) triggers a **BUY** report.  
A **death cross** (short EMA falls below long EMA) triggers a **SELL** report.

The message also reports:

- **Ribbon alignment** — whether all pairs point in the same direction.  The more pairs aligned, the stronger and more reliable the signal.
- **RSI(14)** — overbought (> 70) or oversold (< 30) context flags.
- **Volume ratio** — current bar volume vs. 20-bar average; spikes (≥ 2×) indicate conviction.

### Why multiple EMA periods?

A single EMA pair can produce false crossovers in choppy, sideways markets.  Stacking multiple periods creates a **ribbon**:

- When the ribbon is **fully aligned** (EMA 5 > EMA 8 > EMA 21 > EMA 34 > EMA 50 for a BUY), the trend is strong and consistent across all timeframes — a high-conviction signal.
- When only the fastest pair (EMA 5/8) crosses but slower pairs remain counter-trend, it is likely a short-lived pullback, not a trend change — the report makes this visible at a glance.
- Adding EMA 34 and EMA 50 extends coverage to the **medium-term institutional timeframe**, which acts as dynamic support/resistance and is watched by larger participants.

### EMA — mathematical definition

An Exponential Moving Average gives more weight to recent prices than a Simple Moving Average.  For a period *n*:

```
multiplier  k  =  2 / (n + 1)

EMA(today)  =  price(today) × k  +  EMA(yesterday) × (1 − k)
```

The first EMA value is seeded with a Simple Moving Average over the first *n* bars.  Because *k* is larger for smaller *n*, a short-period EMA reacts faster to price changes than a long-period one.

### EMA period reference

| Period | Role | What it tells you |
|--------|------|-------------------|
| EMA 5 | Fastest | Mirrors almost every tick; first indicator to turn in a new direction |
| EMA 8 | Fast | Short-term momentum filter; removes the smallest noise |
| EMA 21 | Medium | The standard short-term trend reference; reliable across many instruments |
| EMA 34 | Slower | Broader trend direction; far less susceptible to whipsaw |
| EMA 50 | Slowest | Medium-term anchor; a cross here signals a genuine trend change |

> **Fibonacci connection** — 5, 8, 21, 34 are all Fibonacci numbers, a sequence widely used in technical analysis.  Many institutional participants watch these levels, which is part of why they tend to be self-fulfilling.

### EMA periods are in **bars**, not minutes

The numbers (5, 8, 21…) count **price bars**, not wall-clock time.  The real time span of each EMA depends on how densely yfinance returns data for that instrument:

| EMA | Bars | Dense trading (2 min bars) | Sparse trading (10 min bars) |
|-----|------|----------------------------|------------------------------|
| EMA 5  | 5  | ~10 min  | ~50 min  |
| EMA 8  | 8  | ~16 min  | ~1.5 h   |
| EMA 21 | 21 | ~42 min  | ~3–4 h   |
| EMA 34 | 34 | ~1.1 h   | ~5–6 h   |
| EMA 50 | 50 | ~1.7 h   | ~8 h     |

`collect_price.py` requests 2-minute bars and falls back to 5 m → 15 m → 1 h when Yahoo Finance does not return finer data (common for thinly traded ETFs such as SGBS).  On sparse instruments each bar represents a genuine traded price, which tends to make the EMA values more meaningful — the signal spans a longer window but contains less noise.

---

## Telegram message format

```
🟢 EMA REPORT — WisdomTree Physical Swiss Gold
──────────────────────────────────────────
EMA Pairs (fast → slow):
  EMA5/EMA8         384.8500 / 383.1200  ↑  ← 🟢 golden cross
  EMA8/EMA21        383.1200 / 381.0500  ↑  ← 🟢 golden cross
  EMA21/EMA34       381.0500 / 379.2000  ↑
  EMA34/EMA50       379.2000 / 377.4000  ↑

Ribbon:  FULLY BULLISH  (4/4 pairs aligned up) ✅

RSI(14):  55.3  (neutral)
Volume:   spike ⚡ 2.4x avg

Price:  389.8800 EUR  (-5.63% from 413.1300)
Time:   2026-04-01 07:25 UTC

━━ EMA Guide ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  EMA 5   [fastest]  mirrors every price tick; first to catch a new move
  EMA 8   [fast]  short-term momentum — filters the smallest noise
  EMA 21  [medium]  standard short-term trend reference
  EMA 34  [slower]  broader trend view; far less whipsaw
  EMA 50  [slowest]  medium-term anchor; often acts as dynamic support/resistance

  Golden cross: short EMA rises above long EMA → bullish momentum
  Death  cross: short EMA falls below long EMA → bearish momentum
  All pairs aligned in the same direction → strongest signal.
```

---

## `instruments.json` config keys

| Key | Description |
|-----|-------------|
| `id` | Unique instrument identifier used in CLI and DB |
| `name` | Human-readable name (used in Telegram messages) |
| `ticker` | Yahoo Finance ticker symbol |
| `isin` | ISIN for reference |
| `currency` | Display currency (e.g. `EUR`) |
| `buy_price` | Average cost basis per share — updated by `record_trade.py` |
| `shares_held` | Current position size — updated by `record_trade.py` |
| `ema_periods` | List of EMA periods to evaluate (default `[5, 8, 21, 34, 50]`); consecutive pairs are formed automatically |
| `volume_lookback_bars` | Bars used to compute the average volume baseline (default `20`) |
| `volume_spike_mult` | Volume ratio above which a spike is flagged (default `2.0`) |
| `alert_cooldown_minutes` | Minimum gap between same-direction signals per instrument (default `60`) |
| `market_open_utc` | Market open time in UTC — used for display context |
| `market_close_utc` | Market close time in UTC — used for display context |

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/Demirrr/gold-tracker.git
cd gold-tracker
python3 -m venv venv
venv/bin/pip install yfinance pandas
```

### 2. Environment variables

```bash
export TELEGRAM_BOT_TOKEN="<your-bot-token>"
export TELEGRAM_CHAT_ID="<your-chat-id>"
export TELEGRAM_SCRIPT="/home/youruser/send_telegram_message.sh"
```

> ⚠️ Never commit these values — they are read from the environment only.

### 3. Initialise the database

```bash
venv/bin/python scripts/init_db.py
```

### 4. Seed historical data

```bash
venv/bin/python scripts/collect_price.py
```

### 5. Set up cron

```cron
TELEGRAM_BOT_TOKEN=<your-token>
TELEGRAM_CHAT_ID=<your-chat-id>

# Collect price every 2 min on weekdays during Euronext hours (07:00–16:30 UTC)
*/2 7-16 * * 1-5 /path/to/venv/bin/python /path/to/scripts/collect_price.py >> /path/to/logs/cron.log 2>&1

# Analyse 1 min after collect so fresh data is always available
1-59/2 7-16 * * 1-5 /path/to/venv/bin/python /path/to/scripts/analyze.py >> /path/to/logs/cron.log 2>&1

# Track signal outcomes every 5 min (runs all day)
*/5 * * * * /path/to/venv/bin/python /path/to/scripts/track_outcomes.py >> /path/to/logs/cron.log 2>&1
```

> **Why `7-16`?**  Euronext Amsterdam closes at **17:30 local time**.  In summer (CEST, UTC+2) that is 15:30 UTC; in winter (CET, UTC+1) it is 16:30 UTC.  Hour range `7-16` covers both seasons.

---

## Manual analysis

```bash
# Both instruments — bypasses cooldown, fetches fresh data, prints live stats
venv/bin/python scripts/analyze.py --force

# Single instrument
venv/bin/python scripts/analyze.py --force --instrument sgbs-as
```

Example output:

```
==============================================
  Instrument: WisdomTree Physical Swiss Gold (sgbs-as)
  Last bar:   2026-04-01T07:04:00Z
  Price:      389.8800 EUR
  RSI(14):    78.4   Volume: 0.00x

  EMA5/EMA8         384.3628 / 383.2120  [BULLISH]
  EMA8/EMA21        383.2120 / 380.5976  [BULLISH]
  EMA21/EMA34       380.5976 / 378.5832  [BULLISH]
  EMA34/EMA50       378.5832 / 376.7326  [BULLISH]

  No EMA crossovers — nothing to send
```

---

## Recording trades

Whenever you execute a buy or sell, record it with `record_trade.py` so `buy_price` and `shares_held` stay accurate.

```bash
# Buy
venv/bin/python scripts/record_trade.py \
  --instrument itky-as --action buy \
  --shares 2.615609 --price 19.12 --fee 1.0

# Sell
venv/bin/python scripts/record_trade.py \
  --instrument itky-as --action sell \
  --shares 1.5 --price 20.50 --fee 1.0

# Dry run (preview without writing)
venv/bin/python scripts/record_trade.py \
  --instrument itky-as --action buy --shares 2.0 --price 19.50 --dry-run

# View history
venv/bin/python scripts/record_trade.py --list itky-as
```

---

## Database schema

### `prices`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| instrument_id | TEXT | e.g. `sgbs-as` |
| ts | TEXT | UTC ISO timestamp |
| open / high / low / close | REAL | OHLC |
| volume | REAL | |

Unique constraint on `(instrument_id, ts)`.

### `signals`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| instrument_id | TEXT | |
| ts | TEXT | UTC ISO timestamp |
| signal_type | TEXT | `BUY` \| `SELL` |
| engine | TEXT | `ema` |
| price | REAL | price at signal time |
| reason | TEXT | which pairs crossed and in which direction |
| ma_short | REAL | fastest EMA value at signal time |
| ma_long | REAL | slowest EMA value at signal time |
| rsi | REAL | RSI(14) at signal time |

### `outcomes`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| signal_id | INTEGER FK | references `signals.id` |
| price_1h | REAL | price 1 h after signal |
| price_4h | REAL | price 4 h after signal |
| price_24h | REAL | price 24 h after signal |
| outcome | TEXT | `GOOD` \| `BAD` \| `NEUTRAL` |

---

## Outcome tracking

`track_outcomes.py` fills `price_1h`, `price_4h`, `price_24h` and grades every signal:

- **GOOD** — price moved in the direction the signal predicted
- **BAD** — price moved against it
- **NEUTRAL** — change < 0.1%

Query signal quality:

```sql
SELECT signal_type,
       COUNT(*) AS total,
       SUM(CASE WHEN outcome='GOOD' THEN 1 ELSE 0 END) AS good,
       ROUND(100.0 * SUM(CASE WHEN outcome='GOOD' THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_good
FROM signals s
JOIN outcomes o ON o.signal_id = s.id
GROUP BY signal_type;
```

---

## Adding a new instrument

1. Look up the yfinance ticker (Euronext Amsterdam instruments use the `.AS` suffix).
2. Add an entry to `scripts/instruments.json`:

```json
{
  "id": "my-etf",
  "name": "My ETF Name",
  "ticker": "XYZ.AS",
  "isin": "...",
  "currency": "EUR",
  "buy_price": 100.0,
  "shares_held": 0.0,
  "ema_periods": [5, 8, 21, 34, 50],
  "volume_lookback_bars": 20,
  "volume_spike_mult": 2.0,
  "alert_cooldown_minutes": 60,
  "market_open_utc": "08:00",
  "market_close_utc": "16:30"
}
```

3. Seed historical data: `venv/bin/python scripts/collect_price.py --instrument my-etf`
4. No cron changes needed — all scripts loop over instruments automatically.

