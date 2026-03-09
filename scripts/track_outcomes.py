#!/usr/bin/env python3
"""
Track outcomes of past signals.
For each signal older than 1h/4h/24h without a recorded outcome price,
fetch the current price and fill it in. Then compute GOOD/BAD/NEUTRAL.

  SELL signal → GOOD if price_Nh < signal price  (price fell after we said sell)
  BUY  signal → GOOD if price_Nh > signal price  (price rose after we said buy)
"""
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import yfinance as yf

sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from config import DB_PATH, LOG_DIR, TICKER
from init_db import init_db

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_DIR / "outcomes.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

OUTCOME_WINDOWS = [("price_1h", 1), ("price_4h", 4), ("price_24h", 24)]


def current_price():
    df = yf.Ticker(TICKER).history(period="1d", interval="2m")
    if df.empty:
        return None
    return float(df.Close.iloc[-1])


def compute_outcome(signal_type, signal_price, price_1h, price_4h, price_24h):
    """Use 1h outcome as primary measure of signal quality."""
    ref = price_1h or price_4h or price_24h
    if ref is None:
        return None
    if signal_type == "SELL":
        return "GOOD" if ref < signal_price else ("NEUTRAL" if abs(ref - signal_price) / signal_price < 0.001 else "BAD")
    if signal_type == "BUY":
        return "GOOD" if ref > signal_price else ("NEUTRAL" if abs(ref - signal_price) / signal_price < 0.001 else "BAD")
    return "NEUTRAL"


def track():
    init_db()
    now = datetime.now(timezone.utc)
    price = current_price()
    if price is None:
        logging.warning("Could not fetch current price for outcome tracking")
        return

    con = sqlite3.connect(DB_PATH)
    try:
        # Find signals that need outcome rows created or updated
        signals = con.execute(
            """SELECT s.id, s.ts, s.signal_type, s.price,
                      o.id, o.price_1h, o.price_4h, o.price_24h
               FROM signals s
               LEFT JOIN outcomes o ON o.signal_id = s.id
               WHERE s.signal_type IN ('BUY','SELL')"""
        ).fetchall()

        for sig_id, ts, sig_type, sig_price, out_id, p1h, p4h, p24h in signals:
            sig_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age = now - sig_dt
            updated = False

            p1h_new  = p1h  if p1h  is not None else (price if age >= timedelta(hours=1)  else None)
            p4h_new  = p4h  if p4h  is not None else (price if age >= timedelta(hours=4)  else None)
            p24h_new = p24h if p24h is not None else (price if age >= timedelta(hours=24) else None)

            if p1h_new != p1h or p4h_new != p4h or p24h_new != p24h:
                updated = True

            if not updated:
                continue

            outcome = compute_outcome(sig_type, sig_price, p1h_new, p4h_new, p24h_new)
            filled_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

            if out_id is None:
                con.execute(
                    """INSERT INTO outcomes (signal_id, price_1h, price_4h, price_24h, outcome, filled_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (sig_id, p1h_new, p4h_new, p24h_new, outcome, filled_at),
                )
            else:
                con.execute(
                    """UPDATE outcomes SET price_1h=?, price_4h=?, price_24h=?, outcome=?, filled_at=?
                       WHERE id=?""",
                    (p1h_new, p4h_new, p24h_new, outcome, filled_at, out_id),
                )

            logging.info(
                "Outcome updated for signal %d (%s at %.4f): 1h=%.4f outcome=%s",
                sig_id, sig_type, sig_price, p1h_new or 0, outcome,
            )

        con.commit()
    finally:
        con.close()


if __name__ == "__main__":
    track()
