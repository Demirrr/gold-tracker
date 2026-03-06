# Gold Tracker — shared constants
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent

# Asset
TICKER = "SGBS.AS"          # WisdomTree Physical Swiss Gold, Euronext Amsterdam (EUR) ISIN JE00B588CD74
BUY_PRICE = 426.23           # EUR per share
SHARES_HELD = 0.234609
TOTAL_INVESTED = 101.0       # EUR incl. 1€ fee

# Signal thresholds
SIGNAL_THRESHOLD_PCT = 1.0   # % from buy price to trigger price alert
MA_SHORT_MINUTES = 15
MA_LONG_MINUTES = 60
ALERT_COOLDOWN_MINUTES = 60  # min gap between same signal type alerts

# Paths
DB_PATH = BASE_DIR / "data" / "gold.db"
LOG_DIR = BASE_DIR / "logs"

# Telegram (read from environment)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = "7943206744"
TELEGRAM_SCRIPT = Path("/home/cdemir/send_telegram_message.sh")

# Market hours (CET/CEST) — Borsa Italiana
MARKET_OPEN_HOUR = 9
MARKET_CLOSE_HOUR = 17
MARKET_CLOSE_MINUTE = 30
