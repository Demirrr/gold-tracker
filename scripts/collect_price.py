#!/usr/bin/env python3
"""Fetch latest prices for SGBS.AS and store any new bars in SQLite."""
import logging
import sqlite3
import sys
from datetime import timezone

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
    # Use period="2d" so we never miss bars around midnight/open boundaries.
    # Insert ALL returned bars — INSERT OR IGNORE skips duplicates safely.
    df = yf.Ticker(TICKER).history(period="2d", interval="2m")
    if df.empty:
        logging.warning("No data returned from yfinance for %s", TICKER)
        return

    con = sqlite3.connect(DB_PATH)
    inserted = 0
    try:
        for ts, row in df.iterrows():
            t = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            cur = con.execute(
                "INSERT OR IGNORE INTO prices (ts, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?)",
                (t, row.Open, row.High, row.Low, row.Close, row.Volume),
            )
            inserted += cur.rowcount
        con.commit()
        latest_ts = df.index[-1].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        logging.info("Inserted %d new bars. Latest: %s close=%.4f", inserted, latest_ts, df.Close.iloc[-1])
    except Exception as exc:
        logging.error("DB insert failed: %s", exc)
    finally:
        con.close()

    return inserted


if __name__ == "__main__":
    n = collect()
    print(f"Inserted {n} new bar(s)")
