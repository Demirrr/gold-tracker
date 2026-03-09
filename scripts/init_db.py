#!/usr/bin/env python3
"""Initialize the SQLite database schema (multi-instrument, dual-engine)."""
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
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        instrument_id TEXT    NOT NULL DEFAULT 'sgbs-as',
        ts            TEXT    NOT NULL,
        open          REAL,
        high          REAL,
        low           REAL,
        close         REAL    NOT NULL,
        volume        REAL,
        UNIQUE(instrument_id, ts)
    );

    CREATE TABLE IF NOT EXISTS signals (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        instrument_id    TEXT    NOT NULL DEFAULT 'sgbs-as',
        ts               TEXT    NOT NULL,
        signal_type      TEXT    NOT NULL,   -- BUY | SELL
        engine           TEXT    NOT NULL,   -- confluence | macd
        price            REAL    NOT NULL,
        reason           TEXT,
        ma_short         REAL,
        ma_long          REAL,
        rsi              REAL,
        macd_line        REAL,
        macd_signal_line REAL,
        pct_from_buy     REAL,
        confidence       TEXT,               -- LOW | MEDIUM | HIGH (confluence only)
        notified_at      TEXT
    );

    CREATE TABLE IF NOT EXISTS outcomes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id   INTEGER NOT NULL REFERENCES signals(id),
        price_1h    REAL,
        price_4h    REAL,
        price_24h   REAL,
        outcome     TEXT,
        filled_at   TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_prices_inst_ts   ON prices(instrument_id, ts);
    CREATE INDEX IF NOT EXISTS idx_signals_inst_ts  ON signals(instrument_id, ts);
    CREATE INDEX IF NOT EXISTS idx_signals_engine   ON signals(engine);
    """)

    con.commit()
    con.close()
    print(f"Database initialised at {DB_PATH}")

if __name__ == "__main__":
    init_db()
