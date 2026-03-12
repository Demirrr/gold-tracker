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
import random
import sqlite3
import sys
import time
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import DB_PATH, LOG_DIR, SCRIPTS_DIR
from init_db import init_db
from market_data import fetch_history

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

    df, interval, source = fetch_history(
        ticker=ticker,
        instrument_id=iid,
        logger=logging,
    )

    if df is None or df.empty:
        logging.warning("[%s] No data returned from yfinance (market closed or rate-limited)", iid)
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
        fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (f"collector_heartbeat:{iid}", fetched_at),
        )
        con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (f"collector_last_source_ts:{iid}", latest_ts),
        )
        con.commit()
        if interval != "2m" or source != "ticker.history":
            logging.info("[%s] Used fallback source=%s interval=%s", iid, source, interval)
        logging.info("[%s] Inserted %d new bars. Latest: %s close=%.4f",
                     iid, inserted, latest_ts, df.Close.iloc[-1])
    except Exception as exc:
        logging.error("[%s] DB insert failed: %s", iid, exc)
    finally:
        con.close()

    return inserted


def collect(instrument_id=None):
    # Small random jitter (0–15 s) so cron bursts don't hit Yahoo Finance simultaneously
    time.sleep(random.uniform(0, 15))
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
