#!/usr/bin/env python3
"""
Fetch latest prices for all tracked instruments and store new bars in SQLite.
Usage:
  python collect_price.py                    # all instruments
  python collect_price.py --instrument sgbs-as
"""
import argparse
import json
import logging
import sqlite3
import sys
from datetime import timezone
from pathlib import Path

import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent))
from config import DB_PATH, LOG_DIR, SCRIPTS_DIR
from init_db import init_db

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_DIR / "collector.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def load_instruments(instrument_id=None):
    path = SCRIPTS_DIR / "instruments.json"
    instruments = json.loads(path.read_text())
    if instrument_id:
        instruments = [i for i in instruments if i["id"] == instrument_id]
        if not instruments:
            raise ValueError(f"Instrument '{instrument_id}' not found in instruments.json")
    return instruments


def collect_one(inst):
    iid    = inst["id"]
    ticker = inst["ticker"]

    df = yf.Ticker(ticker).history(period="2d", interval="2m")
    if df.empty:
        logging.warning("[%s] No data returned from yfinance", iid)
        return 0

    con = sqlite3.connect(DB_PATH)
    inserted = 0
    try:
        for ts, row in df.iterrows():
            t = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            cur = con.execute(
                "INSERT OR IGNORE INTO prices (instrument_id, ts, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (iid, t, row.Open, row.High, row.Low, row.Close, row.Volume),
            )
            inserted += cur.rowcount
        con.commit()
        latest_ts = df.index[-1].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        logging.info("[%s] Inserted %d new bars. Latest: %s close=%.4f",
                     iid, inserted, latest_ts, df.Close.iloc[-1])
    except Exception as exc:
        logging.error("[%s] DB insert failed: %s", iid, exc)
    finally:
        con.close()

    return inserted


def collect(instrument_id=None):
    init_db()
    instruments = load_instruments(instrument_id)
    for inst in instruments:
        n = collect_one(inst)
        print(f"[{inst['id']}] Inserted {n} new bar(s) — latest close available")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", "-i", help="Instrument ID (default: all)")
    args = parser.parse_args()
    collect(args.instrument)
