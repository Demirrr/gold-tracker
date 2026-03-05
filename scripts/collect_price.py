#!/usr/bin/env python3
"""Fetch latest price for WGLD.MI and store in SQLite."""
import logging
import sqlite3
import sys
from datetime import datetime, timezone

import yfinance as yf

sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from config import DB_PATH, LOG_DIR, TICKER
from init_db import init_db

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_DIR / "collector.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def collect():
    init_db()
    # Fetch last 5 minutes of 1m candles to get the most recent complete bar
    df = yf.Ticker(TICKER).history(period="1d", interval="2m")
    if df.empty:
        logging.warning("No data returned from yfinance for %s", TICKER)
        return

    # Take the latest complete bar
    row = df.iloc[-1]
    ts = df.index[-1].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            """INSERT OR IGNORE INTO prices (ts, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ts, row.Open, row.High, row.Low, row.Close, row.Volume),
        )
        con.commit()
        logging.info("Stored %s close=%.4f", ts, row.Close)
    except Exception as exc:
        logging.error("DB insert failed: %s", exc)
    finally:
        con.close()


if __name__ == "__main__":
    collect()
