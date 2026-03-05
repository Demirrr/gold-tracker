# Gold Tracker 📈

Automated trading signal system for **WisdomTree Physical Swiss Gold (SGBS.MI)** that sends Telegram alerts when it's a good time to buy or sell, and tracks whether those signals were accurate over time.

## How it works

```
[cron every 2min]         [cron every 2min]              [cron every 5min]
collect_price.py ──► SQLite (gold.db) ──► analyze.py ──► notify via Telegram
                                               │
                                               ▼
                                      track_outcomes.py
                                  (fills price_1h/4h/24h,
                                   marks GOOD/BAD/NEUTRAL)
```

1. **`collect_price.py`** — Fetches `SGBS.MI` price via yfinance every 2 minutes during market hours and stores OHLCV bars in SQLite.
2. **`analyze.py`** — Computes MA15 & MA60 moving averages, detects crossovers and ±1% threshold breaches from the buy price, generates BUY/SELL signals and sends Telegram alerts (1-hour cooldown per signal type).
3. **`track_outcomes.py`** — Runs every 5 minutes and retrospectively fills in the price 1h, 4h, and 24h after each signal was generated. It then automatically labels each signal as **GOOD**, **BAD**, or **NEUTRAL** so you can measure strategy accuracy over time.

## Setup

```bash
git clone https://github.com/Demirrr/gold-tracker.git
cd gold-tracker
python3 -m venv venv
venv/bin/pip install yfinance pandas
venv/bin/python scripts/init_db.py
```

Set your Telegram bot token:
```bash
export TELEGRAM_BOT_TOKEN=your_token_here
```

Seed historical data (recommended — provides enough bars for MA accuracy on first run):
```bash
venv/bin/python -c "
import yfinance as yf, sqlite3
from scripts.config import DB_PATH, TICKER
from datetime import timezone
df = yf.Ticker(TICKER).history(period='7d', interval='2m')
con = sqlite3.connect(DB_PATH)
for ts, row in df.iterrows():
    t = ts.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    con.execute('INSERT OR IGNORE INTO prices (ts,open,high,low,close,volume) VALUES (?,?,?,?,?,?)',
                (t, row.Open, row.High, row.Low, row.Close, row.Volume))
con.commit()
print(f'Seeded {len(df)} bars')
"
```

## Cron jobs

Add to your crontab (`crontab -e`):

```cron
TELEGRAM_BOT_TOKEN=your_token_here

# Collect price every 2 minutes on weekdays, market hours (07–15 UTC = 09–17 CET)
*/2 7-15 * * 1-5 /path/to/gold-tracker/venv/bin/python /path/to/gold-tracker/scripts/collect_price.py >> /path/to/gold-tracker/logs/cron.log 2>&1

# Analyse and notify (offset by 1 minute so data is fresh)
1-59/2 7-15 * * 1-5 /path/to/gold-tracker/venv/bin/python /path/to/gold-tracker/scripts/analyze.py >> /path/to/gold-tracker/logs/cron.log 2>&1

# Track outcomes every 5 minutes (all day, to catch 1h/4h/24h windows)
*/5 * * * * /path/to/gold-tracker/venv/bin/python /path/to/gold-tracker/scripts/track_outcomes.py >> /path/to/gold-tracker/logs/cron.log 2>&1
```

## Manual run

Run analysis immediately (bypasses the 1-hour cooldown and prints live stats to stdout):

```bash
venv/bin/python scripts/analyze.py --force
# or short form
venv/bin/python scripts/analyze.py -f
```

Example output:
```
=== Manual Analysis Run (cooldown bypassed) ===
  Price:   425.67 EUR  (-0.13% from buy at 426.23)
  MA15:    424.51
  MA60:    424.08
  P/L:     -0.13 EUR
  Bars:    173
  Crossed below: False | Crossed above: False
  Above threshold: False | Below threshold: False

  ℹ️  No signal. Cooldown: SELL=False, BUY=False
  Conditions not met for any signal at this time.
```

## Signal logic

| Signal | Condition |
|--------|-----------|
| 🔴 SELL | MA15 crosses below MA60 (bearish) AND/OR price ≥ buy price + threshold% |
| 🟢 BUY  | MA15 crosses above MA60 (bullish) AND/OR price ≤ buy price − threshold% |

Both conditions are evaluated independently — a signal fires if **either** triggers. The 1-hour cooldown prevents repeat alerts for the same signal type.

## Configuration

Edit `scripts/config.py`:

```python
TICKER = "SGBS.MI"          # WisdomTree Physical Swiss Gold, Borsa Italiana (EUR)
BUY_PRICE = 426.23          # Your entry price in EUR
SHARES_HELD = 0.234609
TOTAL_INVESTED = 101.0      # EUR incl. fees
SIGNAL_THRESHOLD_PCT = 1.0  # % from buy price to trigger price alert
MA_SHORT_MINUTES = 15
MA_LONG_MINUTES = 60
ALERT_COOLDOWN_MINUTES = 60
```

## Database schema

All data is stored in SQLite at `data/gold.db` (excluded from git).

### `prices` — raw market data
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| ts | TEXT | ISO UTC timestamp (unique) |
| open, high, low, close | REAL | OHLC prices in EUR |
| volume | REAL | Trade volume |

### `signals` — generated trading signals
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| ts | TEXT | When signal was generated (UTC) |
| signal_type | TEXT | `BUY` or `SELL` |
| price | REAL | Price at signal time |
| reason | TEXT | Human-readable explanation |
| ma15, ma60 | REAL | Moving average values at signal time |
| pct_from_buy | REAL | % change from your entry price |
| notified_at | TEXT | When Telegram alert was sent (NULL = not sent) |

### `outcomes` — was the signal accurate?
| Column | Type | Description |
|--------|------|-------------|
| signal_id | INTEGER | FK → signals.id |
| price_1h | REAL | Price 1 hour after signal |
| price_4h | REAL | Price 4 hours after signal |
| price_24h | REAL | Price 24 hours after signal |
| outcome | TEXT | `GOOD`, `BAD`, or `NEUTRAL` |
| filled_at | TEXT | When outcome was recorded |

**Outcome logic:**
- `SELL` signal → **GOOD** if `price_1h < signal_price` (price fell — good call to sell)
- `BUY` signal → **GOOD** if `price_1h > signal_price` (price rose — good call to buy)
- Within ±0.1% → **NEUTRAL**
- Otherwise → **BAD**

## Backtracking — measuring strategy accuracy

To review past signals and their outcomes at any time:

```bash
venv/bin/python -c "
import sqlite3
from scripts.config import DB_PATH

con = sqlite3.connect(DB_PATH)

print('=== ALL SIGNALS WITH OUTCOMES ===')
rows = con.execute('''
    SELECT s.ts, s.signal_type, s.price, s.pct_from_buy, s.reason,
           o.price_1h, o.price_24h, o.outcome
    FROM signals s
    LEFT JOIN outcomes o ON o.signal_id = s.id
    ORDER BY s.ts DESC
''').fetchall()

for r in rows:
    ts, sig, price, pct, reason, p1h, p24h, outcome = r
    print(f'  [{ts}] {sig:4s}  price={price:.2f}  pct={pct:+.2f}%')
    print(f'         reason:  {reason}')
    print(f'         1h later: {p1h:.2f if p1h else \"pending\":>7}  24h later: {p24h:.2f if p24h else \"pending\":>7}  outcome={outcome or \"pending\"}')
    print()

total = len(rows)
good  = sum(1 for r in rows if r[7] == 'GOOD')
bad   = sum(1 for r in rows if r[7] == 'BAD')
print(f'Summary: {total} signals total — {good} GOOD / {bad} BAD / {total-good-bad} pending or neutral')
"
```

You can also query specific windows:

```sql
-- All SELL signals that were good calls
SELECT s.ts, s.price, o.price_1h, o.outcome
FROM signals s JOIN outcomes o ON o.signal_id = s.id
WHERE s.signal_type = 'SELL' AND o.outcome = 'GOOD';

-- Overall accuracy rate
SELECT signal_type,
       COUNT(*) AS total,
       SUM(outcome = 'GOOD') AS good,
       ROUND(100.0 * SUM(outcome = 'GOOD') / COUNT(*), 1) AS accuracy_pct
FROM signals s JOIN outcomes o ON o.signal_id = s.id
GROUP BY signal_type;
```

## Example Telegram alert

```
🔴 GOLD SIGNAL: SELL
────────────────────────────
💡 What to do: Consider SELLING your position to lock in gains.
   Selling now at 430.50€ would give you ~430.50€/share.

📌 Why: Price +1.00% above buy price — take profit

📈 Price now:  430.50 EUR  (+1.00% from your buy at 426.23)
💰 Your P/L:  +1.00 EUR  (+1.00%)
📊 MA15 vs MA60: 429.80 vs 428.50  (short MA is above long MA)
📦 Data points: 250 price bars collected
🕐 2026-03-05 10:32 UTC
```

## Potential improvements

### Strategy enhancements
- **Multiple strategies in parallel** — run MA crossover, RSI, Bollinger Bands, and MACD as separate strategies simultaneously; only alert when 2+ strategies agree (confluence)
- **RSI (Relative Strength Index)** — alert when RSI > 70 (overbought → sell) or RSI < 30 (oversold → buy more)
- **Bollinger Bands** — flag when price touches the upper band (potential reversal) or lower band (potential bounce)
- **MACD** — detect momentum shifts earlier than simple MA crossovers
- **Volume confirmation** — only fire signals when volume is above average (stronger conviction)
- **Trailing stop-loss** — alert if price drops X% from a recent high after a profitable period

### Data & collection
- **Smaller intervals** — switch to 1-minute bars when more precision is needed (watch yfinance rate limits)
- **Multiple data sources** — cross-validate price with Alpha Vantage or a broker API to detect yfinance stale data
- **Pre/post-market awareness** — flag if a significant overnight price gap occurred before market open

### Signal quality
- **Weighted outcome scoring** — weight 1h outcomes more than 24h; gold can mean-revert over days
- **Signal confidence score** — combine how many conditions triggered (e.g. MA crossover + threshold breach = higher confidence than either alone)
- **Avoid choppy markets** — suppress signals when MA15 and MA60 are within 0.1% of each other (sideways market, false crossovers likely)

### Notifications
- **Telegram inline buttons** — add "I sold ✅" / "I ignored ❌" reply buttons to capture actual user decisions, not just signal outcomes
- **Daily summary** — send a morning Telegram message with overnight price change, current P/L, and today's MA outlook
- **Email fallback** — if Telegram fails, send via email

### Infrastructure
- **Web dashboard** — simple Flask/Streamlit app to visualise price history, signals, and accuracy chart
- **Alerts on data staleness** — notify if no new price bar has been collected for >10 minutes during market hours (collector may have crashed)
- **Docker container** — package the whole system for easy deployment on a VPS or Raspberry Pi
