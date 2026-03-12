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
        suggested_eur    REAL,               -- position sizing: recommended EUR amount
        suggested_shares REAL,               -- position sizing: recommended share count
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

    CREATE TABLE IF NOT EXISTS trades (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        instrument_id TEXT    NOT NULL,
        ts            TEXT    NOT NULL,       -- ISO UTC timestamp of trade
        action        TEXT    NOT NULL,       -- BUY | SELL
        shares        REAL    NOT NULL,       -- shares transacted
        price         REAL    NOT NULL,       -- price per share (EUR)
        fee           REAL    NOT NULL DEFAULT 1.0,
        total_eur     REAL    NOT NULL,       -- total EUR paid (BUY) or received (SELL)
        source        TEXT    NOT NULL DEFAULT 'MANUAL',  -- MANUAL | AUTO
        avg_cost_after REAL,                  -- weighted avg buy price after this trade
        shares_after  REAL,                   -- shares_held after this trade
        notes         TEXT
    );

    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_prices_inst_ts   ON prices(instrument_id, ts);
    CREATE INDEX IF NOT EXISTS idx_signals_inst_ts  ON signals(instrument_id, ts);
    CREATE INDEX IF NOT EXISTS idx_signals_engine   ON signals(engine);
    CREATE INDEX IF NOT EXISTS idx_trades_inst_ts   ON trades(instrument_id, ts);
    """)

    con.commit()
    con.close()

if __name__ == "__main__":
    init_db()
    print(f"Database initialised at {DB_PATH}")
