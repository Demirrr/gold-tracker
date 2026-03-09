# Gold Tracker — system-level config only
# Per-instrument settings live in instruments.json
import os
from pathlib import Path

BASE_DIR    = Path(__file__).parent.parent
SCRIPTS_DIR = Path(__file__).parent

# Paths
DB_PATH = BASE_DIR / "data" / "gold.db"
LOG_DIR = BASE_DIR / "logs"

# Telegram (read from environment — never hardcode)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_SCRIPT    = Path("/home/cdemir/send_telegram_message.sh")
