# Gold Tracker 📊

Automated trading signal system that tracks ETF/ETP prices via yfinance, stores data in SQLite, generates BUY/SELL signals using dual signal engines, and sends Telegram alerts — all running locally on a Raspberry Pi with cron.

---

## Instruments tracked

| # | Name | Ticker | ISIN | Buy price | Shares | Total invested |
|---|------|--------|------|-----------|--------|----------------|
| 1 | WisdomTree Physical Swiss Gold | SGBS.AS | JE00B588CD74 | 426.23 EUR | 0.234609 | 101.00 EUR |
| 2 | iShares MSCI Turkey UCITS ETF | ITKY.AS | IE00B1FZS574 | 15.76 EUR | 1.01437 | 16.75 EUR |

Both on Euronext Amsterdam (EUR). To add a new instrument, edit `scripts/instruments.json` (see [Adding a new instrument](#adding-a-new-instrument) below).

---

## Architecture

```
gold-tracker/
├── data/
│   └── gold.db                  ← SQLite database (prices, signals, outcomes)
├── logs/
│   ├── cron.log                 ← cron stdout/stderr
│   ├── analyzer.log             ← signal engine log
│   ├── collector.log            ← price collection log
│   └── outcomes.log             ← outcome tracking log
├── scripts/
│   ├── instruments.json         ← instrument registry (config per asset)
│   ├── config.py                ← system-level config (paths, env vars)
│   ├── init_db.py               ← creates DB schema
│   ├── migrate_db.py            ← idempotent DB migration (safe to re-run)
│   ├── collect_price.py         ← fetch 2-min bars from yfinance → DB
│   ├── analyze.py               ← dual engine signal analysis + Telegram
│   └── track_outcomes.py        ← grade past signals (GOOD/BAD/NEUTRAL)
└── venv/                        ← Python virtual environment
```

---

## Signal Engines

Two independent strategies run in parallel per instrument. A single combined Telegram message is sent when either engine fires.

### Engine 1 — Confluence (RSI + MACD + MA)

Score-based: each condition contributes points; alert fires when score ≥ 2.

| Condition | Points | Direction |
|-----------|--------|-----------|
| MA15 below MA60 (bearish) | +1 | SELL |
| MA15 above MA60 (bullish) | +1 | BUY |
| RSI > 65 (overbought) | +1 | SELL |
| RSI > 75 (strongly overbought) | +2 | SELL |
| RSI < 35 (oversold) | +1 | BUY |
| RSI < 25 (strongly oversold) | +2 | BUY |
| MACD crossed below signal | +1 | SELL |
| MACD crossed above signal | +1 | BUY |
| Price ≥ threshold% above entry (and > buy price) | +1 | SELL |
| Price ≤ threshold% below entry (and MA bullish) | +1 | BUY |

**Confidence:** LOW (2 pts) / MEDIUM (3 pts) / HIGH (4+ pts)

Safety rules:
- SELL is suppressed if `current_price < buy_price` — never sell at a loss
- BUY dip is suppressed when MA15 < MA60 — never buy into a downtrend

### Engine 2 — MACD-only

EMA(12) / EMA(26) → MACD line + 9-period signal line.
Fires on MACD crossover of signal line. More forward-looking than simple MA crossover.

### Telegram message format

```
📊 SIGNAL REPORT — WisdomTree Physical Swiss Gold
────────────────────────────────────────
🔴 CONFLUENCE: SELL [HIGH confidence, score 4/5]
   MA15 below MA60 (bearish trend) + RSI 72.0 — overbought + price +1.20% above entry — take profit

🔴 MACD ENGINE: SELL
   MACD (-0.4200) crossed below signal (-0.1800)

📈 Price: 430.50 EUR (+1.00% from buy at 426.23)
💰 P/L:   +1.0152 EUR (+0.99%)
🕐 2026-03-09 09:15 UTC
```

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

Add to `~/.bashrc` (or export in shell):

```bash
export TELEGRAM_BOT_TOKEN="<your-bot-token>"
export TELEGRAM_CHAT_ID="<your-chat-id>"
```

> ⚠️ Never commit these values. They are read from environment only.

The `send_telegram_message.sh` script uses `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from the environment.

### 3. Initialise the database

```bash
venv/bin/python scripts/init_db.py
```

Or run the migration if upgrading from an older single-instrument schema:

```bash
venv/bin/python scripts/migrate_db.py
```

### 4. Seed historical data (first run)

```bash
venv/bin/python scripts/collect_price.py
```

This fetches up to 7 days of 2-minute bars for all instruments.

### 5. Set up cron

```
crontab -e
```

Add these lines (with your real token/chat ID):

```cron
TELEGRAM_BOT_TOKEN=<your-token>
TELEGRAM_CHAT_ID=<your-chat-id>

# Collect price every 2 min on weekdays during Euronext hours (08:00–16:30 UTC → 07–15 safe window)
*/2 7-15 * * 1-5 /path/to/venv/bin/python /path/to/scripts/collect_price.py >> /path/to/logs/cron.log 2>&1

# Analyse 1 min after collect
1-59/2 7-15 * * 1-5 /path/to/venv/bin/python /path/to/scripts/analyze.py >> /path/to/logs/cron.log 2>&1

# Track outcomes every 5 min
*/5 * * * * /path/to/venv/bin/python /path/to/scripts/track_outcomes.py >> /path/to/logs/cron.log 2>&1
```

---

## Manual analysis

Run at any time to fetch fresh data and see live stats (bypasses cooldown):

```bash
venv/bin/python scripts/analyze.py --force
# or for a single instrument:
venv/bin/python scripts/analyze.py --force --instrument sgbs-as
```

---

## Database schema

### `prices`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| instrument_id | TEXT | e.g. `sgbs-as` |
| ts | TEXT | UTC ISO timestamp |
| open/high/low/close | REAL | OHLC |
| volume | REAL | |

Unique constraint on `(instrument_id, ts)`.

### `signals`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| instrument_id | TEXT | |
| ts | TEXT | UTC ISO timestamp |
| signal_type | TEXT | BUY \| SELL |
| engine | TEXT | confluence \| macd |
| price | REAL | price at signal time |
| reason | TEXT | human-readable explanation |
| ma_short | REAL | MA15 value |
| ma_long | REAL | MA60 value |
| rsi | REAL | RSI(14) value |
| macd_line | REAL | MACD line value |
| macd_signal_line | REAL | MACD signal line value |
| pct_from_buy | REAL | % change from buy price |
| confidence | TEXT | LOW \| MEDIUM \| HIGH (confluence only) |
| notified_at | TEXT | when Telegram was sent |

### `outcomes`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| signal_id | INTEGER FK | references signals.id |
| price_1h | REAL | price 1h after signal |
| price_4h | REAL | price 4h after signal |
| price_24h | REAL | price 24h after signal |
| outcome | TEXT | GOOD \| BAD \| NEUTRAL |
| filled_at | TEXT | when outcome was recorded |

---

## Backtesting and outcome tracking

Every signal is stored in the `signals` table. The `track_outcomes.py` script fills in `price_1h`, `price_4h`, `price_24h` and marks each signal as:

- **GOOD** — price moved in the direction the signal predicted
- **BAD** — price moved against the signal
- **NEUTRAL** — price change < 0.1%

To review signal quality:

```sql
-- Hit rate per engine
SELECT engine, signal_type,
       COUNT(*) AS total,
       SUM(CASE WHEN outcome='GOOD' THEN 1 ELSE 0 END) AS good,
       ROUND(100.0 * SUM(CASE WHEN outcome='GOOD' THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_good
FROM signals s
JOIN outcomes o ON o.signal_id = s.id
GROUP BY engine, signal_type;

-- All signals with outcome
SELECT s.instrument_id, s.ts, s.signal_type, s.engine,
       s.price, s.pct_from_buy, s.confidence,
       o.price_1h, o.outcome
FROM signals s
LEFT JOIN outcomes o ON o.signal_id = s.id
ORDER BY s.ts DESC;
```

This data lets you evaluate which engine performs better over time and tune thresholds accordingly.

---

## Adding a new instrument

1. Look up the yfinance ticker (Euronext Amsterdam suffix `.AS` recommended for EUR instruments).
2. Add an entry to `scripts/instruments.json`:

```json
{
  "id": "my-etf",
  "name": "My ETF Name",
  "ticker": "XYZ.AS",
  "isin": "...",
  "currency": "EUR",
  "buy_price": 100.0,
  "shares_held": 1.0,
  "total_invested": 101.0,
  "signal_threshold_pct": 1.0,
  "ma_short_minutes": 15,
  "ma_long_minutes": 60,
  "alert_cooldown_minutes": 60,
  "market_open_utc": "08:00",
  "market_close_utc": "16:30"
}
```

3. Seed historical data: `venv/bin/python scripts/collect_price.py --instrument my-etf`
4. No cron changes needed — scripts loop all instruments automatically.

---

## Potential improvements

- **More signal engines** — Bollinger Bands mean-reversion, ATR-based volatility stop, Stochastic oscillator
- **Machine learning** — Train a classifier on labelled outcomes (GOOD/BAD) to weight signal conditions dynamically
- **Multiple timeframes** — Run daily bars alongside 2-min bars for macro trend confirmation (avoid buying a short-term dip in a long-term downtrend)
- **Volume analysis** — Filter signals by volume spike confirmation (high volume on crossover = more reliable signal)
- **Paper trading mode** — Simulate trades with virtual portfolio, compare virtual P/L across strategies before using real money
- **Web dashboard** — Flask/FastAPI endpoint serving a simple chart of price + signals + outcomes
- **Push to multiple channels** — Slack, Discord, email in addition to Telegram
- **Dynamic thresholds** — Auto-tune `signal_threshold_pct` based on recent ATR (Average True Range) to adapt to volatility
- **Stop-loss alerts** — Alert when P/L drops below a configurable threshold (e.g. -5%) regardless of other signals
- **Strategy comparison report** — Weekly summary message comparing hit rate of both engines, sent every Monday
