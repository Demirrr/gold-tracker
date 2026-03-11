# Gold Tracker 📊

Automated trading signal system that tracks ETF/ETP prices via yfinance, stores data in SQLite, generates BUY/SELL signals using dual signal engines, and sends Telegram alerts — all running locally on a Raspberry Pi with cron.

---

## Instruments tracked

| # | Name | Ticker | ISIN | Avg buy price | Shares held | Total invested |
|---|------|--------|------|---------------|-------------|----------------|
| 1 | WisdomTree Physical Swiss Gold | SGBS.AS | JE00B588CD74 | 426.23 EUR | 0.234609 | 101.00 EUR |
| 2 | iShares MSCI Turkey UCITS ETF | ITKY.AS | IE00B1FZS574 | 18.18 EUR | 3.629979 | 67.99 EUR |

> **Avg buy price** and **Shares held** are automatically maintained by `record_trade.py` — do not edit them by hand.

Both on Euronext Amsterdam (EUR). To add a new instrument, edit `scripts/instruments.json` (see [Adding a new instrument](#adding-a-new-instrument) below).

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
│   ├── instruments.json         ← instrument registry (config + live position state)
│   ├── config.py                ← system-level config (paths, env vars)
│   ├── init_db.py               ← creates DB schema
│   ├── migrate_db.py            ← idempotent DB migration (safe to re-run)
│   ├── collect_price.py         ← fetch 2-min bars from yfinance → DB
│   ├── analyze.py               ← dual engine signal analysis + Telegram
│   ├── track_outcomes.py        ← grade past signals (GOOD/BAD/NEUTRAL)
│   └── record_trade.py          ← record manual/executed trades, update position state
└── venv/                        ← Python virtual environment
```

---

## Signal Engines

Two independent strategies run in parallel per instrument. One combined Telegram message is sent when either engine fires.

### Engine 1 — Confluence (Day-trader, score-based)

A multi-indicator day-trader engine. Fires when **score ≥ 3** (max 9). Each condition contributes points:

| Condition | Points | Direction |
|-----------|--------|-----------|
| EMA5 < EMA8 < EMA21 — fully bearish | +2 | SELL |
| EMA5 < EMA8 — short-term bearish | +1 | SELL |
| EMA5 > EMA8 > EMA21 — fully bullish | +2 | BUY |
| EMA5 > EMA8 — short-term bullish | +1 | BUY |
| RSI > 75 — strongly overbought | +2 | SELL |
| RSI > 65 — overbought | +1 | SELL |
| RSI < 25 — strongly oversold | +2 | BUY |
| RSI < 35 — oversold | +1 | BUY |
| MACD crossed below signal | +1 | SELL |
| MACD crossed above signal | +1 | BUY |
| Price below session VWAP (bearish context) | +1 | SELL |
| Price above session VWAP (bullish context) | +1 | BUY |
| Price crossed below VWAP (fresh cross) | +1 | SELL |
| Price crossed above VWAP (fresh cross) | +1 | BUY |
| Price ≥ entry + 1.5× ATR (take-profit target hit) | +1 | SELL |
| Volume spike (≥ 2× avg) on bearish bar | +1 | SELL (confirms) |
| Volume spike (≥ 2× avg) on bullish bar | +1 | BUY (confirms) |

**Confidence:** LOW (score 3) / MEDIUM (score 4) / HIGH (score 5+)

**Safety rules:**
- SELL suppressed if `price < buy_price` — never suggest selling at a loss
- BUY penalised −1 if EMA fully bearish — don't buy into a downtrend

**New day-trading indicators vs old version:**

| Old | New | Why it's better |
|-----|-----|-----------------|
| MA15 / MA60 simple crossover | EMA5 / EMA8 / EMA21 ribbon | EMAs react faster; 3-level ribbon shows trend strength, not just direction |
| Fixed 1% threshold | ATR-based take-profit | Scales with current volatility — 1% means very different things in calm vs volatile markets |
| No volume awareness | Volume spike detection | High-volume confirmation separates genuine moves from noise |
| No intraday anchor | VWAP (session reset) | Institutional benchmark — VWAP side tells you if smart money is buying or selling |

### Engine 2 — MACD-only

EMA(12) / EMA(26) → MACD line + 9-period signal line.  
Fires on MACD crossover of signal line. More forward-looking than EMA ribbon.

### `instruments.json` config keys

| Key | Default | Description |
|-----|---------|-------------|
| `ema_fast_bars` | 5 | EMA fast period (bars) |
| `ema_mid_bars` | 8 | EMA mid period (bars) |
| `ema_slow_bars` | 21 | EMA slow period (bars) |
| `atr_period_bars` | 14 | ATR period (bars) |
| `atr_take_profit_mult` | 1.5 | Take-profit = entry + mult × ATR |
| `volume_lookback_bars` | 20 | Bars used to compute average volume |
| `volume_spike_mult` | 2.0 | Volume ratio threshold for spike detection |
| `alert_cooldown_minutes` | 60 | Min gap between same-type signals |

### Telegram message format

```
📊 SIGNAL REPORT — WisdomTree Physical Swiss Gold
────────────────────────────────────────
🔴 CONFLUENCE: SELL [HIGH confidence, score 6/9]
   EMA5 < EMA8 < EMA21 (fully bearish) + RSI 72.3 — overbought + Price crossed below VWAP
   RSI: 72.3  |  MACD: -0.4200  Signal: -0.1800
   VWAP: 425.123  (price -0.28% from VWAP)
   ATR: 1.267  |  TP: 428.131  |  SL: 424.329
   Volume: 3.2× avg

🔴 MACD ENGINE: SELL
   MACD (-0.4200) crossed below signal (-0.1800)

📈 Price: 424.81 EUR (-0.33% from buy at 426.23)
💰 P/L:   -0.3340 EUR (-0.34%)
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
export TELEGRAM_SCRIPT="/home/youruser/send_telegram_message.sh"  # optional: defaults to /home/cdemir/send_telegram_message.sh
```

> ⚠️ Never commit these values. They are read from environment only.

The `send_telegram_message.sh` script uses `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from the environment.
`TELEGRAM_SCRIPT` overrides the path to the Telegram helper script (useful when deploying on a different machine).

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
TELEGRAM_SCRIPT=/home/youruser/send_telegram_message.sh

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

## Recording trades

Whenever you actually execute a buy or sell (whether triggered by a signal or your own decision), record it with `record_trade.py`. This keeps position state in sync so that P/L, average cost, and position-sizing calculations remain accurate.

### Record a buy

```bash
venv/bin/python scripts/record_trade.py \
  --instrument itky-as \
  --action buy \
  --shares 2.615609 \
  --price 19.12 \
  --total 51.0              # total EUR paid including fee (optional — infers fee automatically)
```

Or specify the fee explicitly:

```bash
venv/bin/python scripts/record_trade.py \
  --instrument itky-as \
  --action buy \
  --shares 2.615609 \
  --price 19.12 \
  --fee 1.0
```

### Record a sell

```bash
venv/bin/python scripts/record_trade.py \
  --instrument itky-as \
  --action sell \
  --shares 1.5 \
  --price 20.50 \
  --fee 1.0
```

### Dry run (preview without writing)

```bash
venv/bin/python scripts/record_trade.py \
  --instrument itky-as \
  --action buy \
  --shares 2.0 \
  --price 19.50 \
  --dry-run
```

### View trade history

```bash
# For a specific instrument
venv/bin/python scripts/record_trade.py --list itky-as

# All instruments
venv/bin/python scripts/record_trade.py --list
```

### What it does under the hood

| Step | What changes |
|------|--------------|
| **BUY** | `shares_held` increases; `buy_price` recalculated as weighted average; `total_invested` increases by total EUR paid |
| **SELL** | `shares_held` decreases; `buy_price` unchanged (cost basis stays); `total_invested` decreases proportionally |
| **DB** | A row is inserted into the `trades` table with full trade details, timestamp, and post-trade position snapshot |
| **P/L** | `analyze.py` reads the updated `buy_price` from `instruments.json` — all future P/L % is automatically relative to your true average cost |

> **Tip:** Always record trades before running `analyze.py --force` so the P/L display reflects your actual position.

### All options

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--instrument` | `-i` | required | Instrument ID (e.g. `itky-as`) |
| `--action` | `-a` | required | `buy` or `sell` |
| `--shares` | | required | Number of shares transacted |
| `--price` | | required | Price per share in EUR |
| `--fee` | | from `instruments.json` | Transaction fee in EUR |
| `--total` | | computed | Total EUR paid/received — if given, fee is inferred |
| `--ts` | | now (UTC) | Trade timestamp in ISO UTC format |
| `--source` | | `MANUAL` | `MANUAL` or `AUTO` |
| `--notes` | | — | Free-text note stored with the trade |
| `--list` | `-l` | — | Print trade history (instrument ID or omit for all) |
| `--dry-run` | | — | Preview changes without writing anything |

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

### `trades`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| instrument_id | TEXT | e.g. `itky-as` |
| ts | TEXT | UTC ISO timestamp of trade execution |
| action | TEXT | BUY \| SELL |
| shares | REAL | shares transacted |
| price | REAL | price per share in EUR |
| fee | REAL | broker fee in EUR |
| total_eur | REAL | total EUR paid (BUY) or received (SELL) |
| source | TEXT | MANUAL \| AUTO |
| avg_cost_after | REAL | weighted avg buy price after this trade |
| shares_after | REAL | shares held after this trade |
| notes | TEXT | optional free-text note |

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

### Already implemented ✅
- **SQL injection hardening** — `record_trade.py --list` uses parameterized queries
- **`paper_trading` enforcement** — Paper signals are tagged `[PAPER]` in the DB and Telegram messages include a `⚠️ PAPER — do not execute` warning per signal line
- **Configurable Telegram script path** — `TELEGRAM_SCRIPT` env var overrides the hardcoded path, enabling deployment on any machine
- **Stale data Telegram alert** — `analyze.py` fires a one-shot Telegram alert during market hours if price data is >30 min old (e.g. when `collect_price.py` crashes silently)
- **Per-engine Kelly criterion** — `compute_kelly_fraction` accepts an `engine` parameter so Confluence and MACD each learn from their own graded outcomes independently; `--force` output shows each engine's progress toward the 30-outcome threshold; rationale in Telegram messages now shows `Kelly:confluence(...)` or `Kelly:macd(...)`

### Pending

#### 🔴 High priority — risk & operational reliability
- **Stop-loss alerts** — Alert when unrealized P/L drops below a configurable threshold (e.g. −5%) regardless of other signals; the most important missing safety net when away from the market
- **Daily loss circuit-breaker** — Suppress further signals once a configurable daily loss limit is hit; prevents runaway signal-chasing in volatile sessions
- **Data retention policy** — Archive or delete 2-min bars older than N days; at 2-min resolution the `prices` table grows ~700 rows/day per instrument and will bloat the DB over months

#### 🟡 Medium priority — signal quality & analysis
- **Backtesting framework** — Replay historical bars from the `prices` table against the current signal logic to tune parameters (EMA periods, score thresholds, cooldowns) without waiting for live forward-tests
- **Strategy comparison report** — Weekly cron Telegram message comparing hit rate, avg P/L, and Kelly fraction of both engines; makes it easy to spot if one engine is consistently underperforming
- **More signal engines** — Bollinger Bands mean-reversion, ATR-based volatility stop, Stochastic oscillator
- **CSV / JSON export** — `--export` flag on `record_trade.py` or a standalone script to dump signals + outcomes for external analysis in Excel or Jupyter

#### 🟢 Low priority — automation & infrastructure
- **Broker integration** — Auto-execute trades via broker API (e.g. IBKR, DEGIRO) and call `record_trade.py --source AUTO` on fill; high value but high operational risk
- **Machine learning** — Train a binary classifier on labelled outcomes (GOOD/BAD) to dynamically weight signal conditions; requires a few hundred graded outcomes before it's meaningful
- **Web dashboard** — Flask/FastAPI endpoint serving a live chart of price + signals + outcomes with per-instrument P/L history
- **Multi-channel alerts** — Push to Slack, Discord, or email in addition to Telegram
