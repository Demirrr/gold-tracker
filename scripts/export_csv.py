#!/usr/bin/env python3
"""
export_csv.py — Export all gold-tracker data to a single directory as CSV.

Prices are resampled to the requested interval; signals, trades, and outcomes
are filtered to match the same instrument/date range and written alongside.

Output directory: data/exports/<instrument>_<interval>/
  prices.csv   — OHLCV bars resampled to --interval
  signals.csv  — all signals with indicator snapshot
  trades.csv   — all recorded trades
  outcomes.csv — signal outcome grades (1h / 4h / 24h)

Examples:
  python scripts/export_csv.py --interval 1h
  python scripts/export_csv.py --interval 10min --instrument sgbs-as
  python scripts/export_csv.py --interval 1d --start 2026-03-10 --end 2026-03-20
  python scripts/export_csv.py --interval 30min --output-dir /tmp/exports
"""

import argparse
import logging
import sqlite3
from pathlib import Path

import pandas as pd

from config import BASE_DIR, DB_PATH, LOG_DIR

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_DIR / "export.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
EXPORT_BASE_DIR = BASE_DIR / "data" / "exports"

# Maps CLI label → pandas resample frequency string
VALID_INTERVALS: dict[str, str] = {
    "10min": "10min",
    "30min": "30min",
    "1h":    "1h",
    "2h":    "2h",
    "5h":    "5h",
    "1d":    "1D",
}

# Tables to always export, and which column is their timestamp
_TABLES: dict[str, str] = {
    "prices":   "ts",
    "signals":  "ts",
    "trades":   "ts",
    "outcomes": "filled_at",
}

# Tables that carry an instrument_id column
_INSTRUMENT_TABLES = {"prices", "signals", "trades"}


# ── Data Loading ─────────────────────────────────────────────────────────────

def load_table(
    conn: sqlite3.Connection,
    table: str,
    instrument: str | None,
    start: str | None,
    end: str | None,
) -> pd.DataFrame:
    ts_col = _TABLES[table]
    clauses = ["1=1"]
    params: list = []

    if instrument and table in _INSTRUMENT_TABLES:
        clauses.append("instrument_id = ?")
        params.append(instrument)

    if start:
        clauses.append(f"{ts_col} >= ?")
        params.append(start)

    if end:
        clauses.append(f"{ts_col} <= ?")
        params.append(end)

    where = " AND ".join(clauses)
    order = f"ORDER BY instrument_id, {ts_col}" if table in _INSTRUMENT_TABLES else f"ORDER BY {ts_col}"
    query = f"SELECT * FROM {table} WHERE {where} {order}"
    return pd.read_sql_query(query, conn, params=params)


# ── Resampling ───────────────────────────────────────────────────────────────

def resample_prices(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Resample 2-min OHLCV bars to *freq* per instrument.

    Uses standard OHLCV aggregation: open=first, high=max, low=min,
    close=last, volume=sum. Empty periods (market closed) are dropped.
    """
    df["ts"] = pd.to_datetime(df["ts"], utc=True)

    parts: list[pd.DataFrame] = []
    for instrument_id, group in df.groupby("instrument_id"):
        group = group.set_index("ts").sort_index()

        agg = (
            group[["open", "high", "low", "close", "volume"]]
            .resample(freq)
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
            )
            .dropna(subset=["close"])
        )

        agg.insert(0, "instrument_id", instrument_id)
        agg = agg.reset_index()
        agg["ts"] = agg["ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        parts.append(agg)

    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


# ── Export helpers ────────────────────────────────────────────────────────────

def _write(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False)
    log.info("Exported %d rows → %s", len(df), path)
    print(f"  ✅  {path.name:<16} {len(df):>5,} rows")


# ── Main export bundle ────────────────────────────────────────────────────────

def export_bundle(
    interval: str,
    instrument: str | None,
    start: str | None,
    end: str | None,
    output_dir: Path,
) -> None:
    freq = VALID_INTERVALS[interval]
    instrument_label = instrument or "all"

    bundle_dir = output_dir / f"{instrument_label}_{interval}"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n📦  Bundle: {bundle_dir}")

    conn = sqlite3.connect(DB_PATH)
    try:
        for table, _ in _TABLES.items():
            df = load_table(conn, table, instrument, start, end)

            if df.empty:
                log.warning("No data in '%s' (instrument=%s start=%s end=%s)",
                            table, instrument, start, end)
                print(f"  ⚠️   {table:<16} no data — skipped")
                continue

            if table == "prices":
                df = resample_prices(df, freq)
                if df.empty:
                    print(f"  ⚠️   prices           resampling produced no data — skipped")
                    continue

            _write(df, bundle_dir / f"{table}.csv")

    except Exception as exc:
        log.exception("Export failed: %s", exc)
        print(f"\n❌  Export failed: {exc}")
        raise
    finally:
        conn.close()

    print(f"\n✔   All files written to {bundle_dir}\n")


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export all gold-tracker data (prices, signals, trades, outcomes) to a CSV bundle.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--interval",
        choices=list(VALID_INTERVALS.keys()),
        required=True,
        metavar="INTERVAL",
        help="Resample interval for prices: 10min | 30min | 1h | 2h | 5h | 1d",
    )
    parser.add_argument(
        "--instrument",
        default=None,
        metavar="ID",
        help="Filter by instrument_id (e.g. sgbs-as). Default: all instruments.",
    )
    parser.add_argument(
        "--start",
        default=None,
        metavar="YYYY-MM-DD",
        help="Start date filter (inclusive).",
    )
    parser.add_argument(
        "--end",
        default=None,
        metavar="YYYY-MM-DD",
        help="End date filter (inclusive).",
    )
    parser.add_argument(
        "--output-dir",
        default=str(EXPORT_BASE_DIR),
        metavar="DIR",
        help=f"Parent output directory (default: {EXPORT_BASE_DIR})",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    export_bundle(
        interval=args.interval,
        instrument=args.instrument,
        start=args.start,
        end=args.end,
        output_dir=Path(args.output_dir),
    )


if __name__ == "__main__":
    main()
