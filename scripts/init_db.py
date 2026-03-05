#!/usr/bin/env python3
"""Initialize the SQLite database schema."""
import sqlite3
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from config import DB_PATH

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS prices (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      TEXT NOT NULL UNIQUE,   -- ISO UTC timestamp
        open    REAL,
        high    REAL,
        low     REAL,
        close   REAL NOT NULL,
        volume  REAL
    );

    CREATE TABLE IF NOT EXISTS signals (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT NOT NULL,
        signal_type   TEXT NOT NULL,   -- BUY | SELL | HOLD
        price         REAL NOT NULL,
        reason        TEXT,
        ma15          REAL,
        ma60          REAL,
        pct_from_buy  REAL,
        notified_at   TEXT            -- NULL = not yet sent
    );

    CREATE TABLE IF NOT EXISTS outcomes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id   INTEGER NOT NULL REFERENCES signals(id),
        price_1h    REAL,
        price_4h    REAL,
        price_24h   REAL,
        outcome     TEXT,             -- GOOD | BAD | NEUTRAL
        filled_at   TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_prices_ts   ON prices(ts);
    CREATE INDEX IF NOT EXISTS idx_signals_ts  ON signals(ts);
    CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(signal_type);
    """)

    con.commit()
    con.close()
    print(f"Database initialised at {DB_PATH}")

if __name__ == "__main__":
    init_db()
