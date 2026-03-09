#!/usr/bin/env python3
"""
Track outcomes of past signals for all instruments.
Fills price_1h/4h/24h after each signal and marks GOOD/BAD/NEUTRAL.
Runs for all instruments; use --instrument to target one.
"""
import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent))
from config import DB_PATH, LOG_DIR, SCRIPTS_DIR
from init_db import init_db

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_DIR / "outcomes.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def load_instruments(instrument_id=None):
    instruments = json.loads((SCRIPTS_DIR / "instruments.json").read_text())
    if instrument_id:
        instruments = [i for i in instruments if i["id"] == instrument_id]
    return instruments


def current_price(ticker):
    df = yf.Ticker(ticker).history(period="2d", interval="2m")
    return float(df.Close.iloc[-1]) if not df.empty else None


def compute_outcome(signal_type, signal_price, ref_price):
    if ref_price is None:
        return None
    diff_pct = (ref_price - signal_price) / signal_price
    if abs(diff_pct) < 0.001:
        return "NEUTRAL"
    if signal_type == "SELL":
        return "GOOD" if ref_price < signal_price else "BAD"
    if signal_type == "BUY":
        return "GOOD" if ref_price > signal_price else "BAD"
    return "NEUTRAL"


def track_instrument(inst, con, price):
    iid = inst["id"]
    now = datetime.now(timezone.utc)

    signals = con.execute(
        """SELECT s.id, s.ts, s.signal_type, s.price,
                  o.id, o.price_1h, o.price_4h, o.price_24h
           FROM signals s
           LEFT JOIN outcomes o ON o.signal_id = s.id
           WHERE s.instrument_id = ? AND s.signal_type IN ('BUY','SELL')""",
        (iid,),
    ).fetchall()

    updated = 0
    for sig_id, ts, sig_type, sig_price, out_id, p1h, p4h, p24h in signals:
        sig_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        age = now - sig_dt

        p1h_new  = p1h  if p1h  is not None else (price if age >= timedelta(hours=1)  else None)
        p4h_new  = p4h  if p4h  is not None else (price if age >= timedelta(hours=4)  else None)
        p24h_new = p24h if p24h is not None else (price if age >= timedelta(hours=24) else None)

        if p1h_new == p1h and p4h_new == p4h and p24h_new == p24h:
            continue

        outcome   = compute_outcome(sig_type, sig_price, p1h_new or p4h_new or p24h_new)
        filled_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        if out_id is None:
            con.execute(
                "INSERT INTO outcomes (signal_id, price_1h, price_4h, price_24h, outcome, filled_at) "
                "VALUES (?,?,?,?,?,?)",
                (sig_id, p1h_new, p4h_new, p24h_new, outcome, filled_at),
            )
        else:
            con.execute(
                "UPDATE outcomes SET price_1h=?, price_4h=?, price_24h=?, outcome=?, filled_at=? "
                "WHERE id=?",
                (p1h_new, p4h_new, p24h_new, outcome, filled_at, out_id),
            )
        logging.info("[%s] Outcome signal=%d %s@%.4f → 1h=%.4f outcome=%s",
                     iid, sig_id, sig_type, sig_price, p1h_new or 0, outcome)
        updated += 1

    con.commit()
    return updated


def track(instrument_id=None):
    init_db()
    instruments = load_instruments(instrument_id)
    con = sqlite3.connect(DB_PATH)
    try:
        for inst in instruments:
            price = current_price(inst["ticker"])
            if price is None:
                logging.warning("[%s] Could not fetch price for outcome tracking", inst["id"])
                continue
            n = track_instrument(inst, con, price)
            logging.info("[%s] Updated %d outcome row(s)", inst["id"], n)
    finally:
        con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", "-i", help="Instrument ID (default: all)")
    args = parser.parse_args()
    track(args.instrument)
