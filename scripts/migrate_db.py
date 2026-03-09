#!/usr/bin/env python3
"""
Migrate existing single-instrument DB to multi-instrument + dual-engine schema.
Safe to run multiple times (idempotent).
"""
import sqlite3
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from config import DB_PATH


def column_exists(cur, table, column):
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def migrate():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    print("Starting migration...")

    # ── prices table ──────────────────────────────────────────────────────────
    if not column_exists(cur, "prices", "instrument_id"):
        print("  prices: adding instrument_id column...")
        cur.execute("ALTER TABLE prices ADD COLUMN instrument_id TEXT NOT NULL DEFAULT 'sgbs-as'")
        # Rebuild with correct unique constraint (instrument_id, ts) instead of just ts
        cur.executescript("""
            DROP INDEX IF EXISTS idx_prices_ts;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_prices_inst_ts ON prices(instrument_id, ts);
        """)
        print("  prices: back-filled instrument_id = 'sgbs-as' for all existing rows")
    else:
        print("  prices: instrument_id already present, skipping")

    # ── signals table ─────────────────────────────────────────────────────────
    new_signal_cols = [
        ("instrument_id",    "TEXT NOT NULL DEFAULT 'sgbs-as'"),
        ("engine",           "TEXT NOT NULL DEFAULT 'confluence'"),
        ("rsi",              "REAL"),
        ("macd_line",        "REAL"),
        ("macd_signal_line", "REAL"),
        ("confidence",       "TEXT"),
    ]
    # Rename old MA columns if needed (ma15→ma_short, ma60→ma_long)
    if column_exists(cur, "signals", "ma15") and not column_exists(cur, "signals", "ma_short"):
        print("  signals: renaming ma15→ma_short, ma60→ma_long via table rebuild...")
        cur.executescript("""
            CREATE TABLE signals_new (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_id    TEXT    NOT NULL DEFAULT 'sgbs-as',
                ts               TEXT    NOT NULL,
                signal_type      TEXT    NOT NULL,
                engine           TEXT    NOT NULL DEFAULT 'confluence',
                price            REAL    NOT NULL,
                reason           TEXT,
                ma_short         REAL,
                ma_long          REAL,
                rsi              REAL,
                macd_line        REAL,
                macd_signal_line REAL,
                pct_from_buy     REAL,
                confidence       TEXT,
                notified_at      TEXT
            );
            INSERT INTO signals_new
                (id, instrument_id, ts, signal_type, engine, price, reason,
                 ma_short, ma_long, pct_from_buy, notified_at)
            SELECT id, 'sgbs-as', ts, signal_type, 'confluence', price, reason,
                   ma15, ma60, pct_from_buy, notified_at
            FROM signals;
            DROP TABLE signals;
            ALTER TABLE signals_new RENAME TO signals;
            CREATE INDEX IF NOT EXISTS idx_signals_inst_ts ON signals(instrument_id, ts);
            CREATE INDEX IF NOT EXISTS idx_signals_engine  ON signals(engine);
        """)
        print("  signals: rebuilt with new schema")
    else:
        for col, col_def in new_signal_cols:
            if not column_exists(cur, "signals", col):
                print(f"  signals: adding {col}...")
                cur.execute(f"ALTER TABLE signals ADD COLUMN {col} {col_def}")

    # ── outcomes table ────────────────────────────────────────────────────────
    print("  outcomes: no structural changes needed")

    con.commit()
    con.close()
    print("Migration complete.")


if __name__ == "__main__":
    migrate()
