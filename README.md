# Gold Tracker 📈

Automated trading signal system for **WisdomTree Physical Swiss Gold (SGBS.MI)** that sends Telegram alerts when it's a good time to buy or sell.

## How it works

```
[cron every 2min]         [cron every 2min]         [cron every 5min]
collect_price.py  ──► SQLite ──► analyze.py ──► signals ──► Telegram
                   (gold.db)                         track_outcomes.py
```

1. **`collect_price.py`** — Fetches `SGBS.MI` price via yfinance every 2 minutes during market hours and stores it in SQLite.
2. **`analyze.py`** — Computes MA15 & MA60 moving averages, detects crossovers and ±1% threshold breaches from the buy price, generates BUY/SELL signals and sends Telegram alerts (1-hour cooldown per signal type).
3. **`track_outcomes.py`** — Retrospectively fills in price at 1h/4h/24h after each signal and marks them GOOD/BAD/NEUTRAL to measure accuracy over time.

## Setup

```bash
git clone https://github.com/Demirrrr/gold-tracker.git
cd gold-tracker
python3 -m venv venv
venv/bin/pip install yfinance pandas
venv/bin/python scripts/init_db.py
```

Set your Telegram bot token:
```bash
export TELEGRAM_BOT_TOKEN=your_token_here
```

Seed historical data (optional but recommended for MA accuracy):
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
"
```

## Cron jobs

Add to your crontab (`crontab -e`):

```cron
TELEGRAM_BOT_TOKEN=your_token_here

# Collect price every 2 minutes on weekdays, market hours (07–15 UTC = 09–17 CET)
*/2 7-15 * * 1-5 /path/to/gold-tracker/venv/bin/python /path/to/gold-tracker/scripts/collect_price.py >> /path/to/gold-tracker/logs/cron.log 2>&1

# Analyse and notify
1-59/2 7-15 * * 1-5 /path/to/gold-tracker/venv/bin/python /path/to/gold-tracker/scripts/analyze.py >> /path/to/gold-tracker/logs/cron.log 2>&1

# Track outcomes every 5 minutes
*/5 * * * * /path/to/gold-tracker/venv/bin/python /path/to/gold-tracker/scripts/track_outcomes.py >> /path/to/gold-tracker/logs/cron.log 2>&1
```

## Manual run

Run analysis immediately (bypasses the 1-hour cooldown and prints live stats):

```bash
venv/bin/python scripts/analyze.py --force
# or
venv/bin/python scripts/analyze.py -f
```

## Signal logic

| Signal | Condition |
|--------|-----------|
| 🔴 SELL | MA15 crosses below MA60 (bearish) AND/OR price ≥ buy price +1% |
| 🟢 BUY  | MA15 crosses above MA60 (bullish) AND/OR price ≤ buy price −1% |

## Configuration

Edit `scripts/config.py`:

```python
TICKER = "SGBS.MI"          # WisdomTree Physical Swiss Gold, Borsa Italiana (EUR)
BUY_PRICE = 426.23          # Your buy price in EUR
SHARES_HELD = 0.234609
SIGNAL_THRESHOLD_PCT = 1.0  # % from buy price to trigger alert
MA_SHORT_MINUTES = 15
MA_LONG_MINUTES = 60
ALERT_COOLDOWN_MINUTES = 60
```

## Database schema

SQLite (`data/gold.db`):

- **`prices`** — OHLCV bars collected every 2 minutes
- **`signals`** — Generated BUY/SELL signals with MA values, % from buy price
- **`outcomes`** — Price at 1h/4h/24h after each signal; GOOD/BAD/NEUTRAL label

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
