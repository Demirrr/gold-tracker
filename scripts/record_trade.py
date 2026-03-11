#!/usr/bin/env python3
"""
Record a manual (or algorithm-confirmed) trade and update instrument position state.

Usage:
  python record_trade.py --instrument itky-as --action buy --shares 2.615609 --price 19.12
  python record_trade.py -i itky-as -a sell --shares 1.0 --price 20.50 --fee 1.0
  python record_trade.py -i itky-as -a buy --shares 2.615609 --price 19.12 --total 51.0
  python record_trade.py --list itky-as   # show trade history

Options:
  --instrument / -i   Instrument ID (e.g. itky-as)
  --action     / -a   buy or sell
  --shares            Number of shares transacted
  --price             Price per share in EUR
  --fee               Transaction fee in EUR (default: from instruments.json, usually 1.0)
  --total             Total EUR paid/received (overrides fee calculation if provided)
  --ts                Trade timestamp ISO UTC (default: now)
  --source            MANUAL or AUTO (default: MANUAL)
  --notes             Optional note
  --list / -l         List trade history for an instrument
  --dry-run           Show what would change without writing anything
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import DB_PATH
from init_db import init_db

INSTRUMENTS_PATH = Path(__file__).parent / "instruments.json"


def load_instruments():
    with open(INSTRUMENTS_PATH) as f:
        return json.load(f)


def save_instruments(instruments):
    with open(INSTRUMENTS_PATH, "w") as f:
        json.dump(instruments, f, indent=2)
        f.write("\n")


def find_instrument(instruments, inst_id):
    for inst in instruments:
        if inst["id"].lower() == inst_id.lower():
            return inst
    return None


def ensure_trades_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id  TEXT    NOT NULL,
            ts             TEXT    NOT NULL,
            action         TEXT    NOT NULL,
            shares         REAL    NOT NULL,
            price          REAL    NOT NULL,
            fee            REAL    NOT NULL DEFAULT 1.0,
            total_eur      REAL    NOT NULL,
            source         TEXT    NOT NULL DEFAULT 'MANUAL',
            avg_cost_after REAL,
            shares_after   REAL,
            notes          TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_trades_inst_ts ON trades(instrument_id, ts)")
    con.commit()


def record_trade(args):
    instruments = load_instruments()
    inst = find_instrument(instruments, args.instrument)
    if inst is None:
        print(f"❌ Instrument '{args.instrument}' not found in instruments.json")
        print(f"   Available: {[i['id'] for i in instruments]}")
        sys.exit(1)

    action = args.action.upper()
    if action not in ("BUY", "SELL"):
        print(f"❌ Action must be 'buy' or 'sell', got: {args.action}")
        sys.exit(1)

    shares = args.shares
    price  = args.price
    fee    = args.fee if args.fee is not None else inst.get("transaction_fee_eur", 1.0)
    ts     = args.ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source = args.source.upper()
    notes  = args.notes or ""

    # Determine total EUR
    if args.total is not None:
        total_eur = args.total
        if action == "BUY":
            # Infer fee from total: total = shares*price + fee  →  fee = total - shares*price
            implied_fee = round(total_eur - shares * price, 4)
            if abs(implied_fee - fee) > 0.01:
                print(f"ℹ️  Inferred fee from --total: {implied_fee:.2f}€ (overrides default {fee:.2f}€)")
                fee = implied_fee
    else:
        if action == "BUY":
            total_eur = round(shares * price + fee, 6)
        else:
            total_eur = round(shares * price - fee, 6)

    # Current position
    prev_shares   = inst.get("shares_held", 0.0)
    prev_buy_price = inst.get("buy_price", price)
    prev_invested  = inst.get("total_invested", 0.0)

    # Compute new position state
    if action == "BUY":
        new_shares = prev_shares + shares
        # Weighted average cost basis (on share value, excluding fees)
        new_avg_price = ((prev_shares * prev_buy_price) + (shares * price)) / new_shares
        new_invested   = prev_invested + total_eur
    else:  # SELL
        if shares > prev_shares + 1e-9:
            print(f"❌ Cannot sell {shares} shares — only {prev_shares} held for {inst['id']}")
            sys.exit(1)
        new_shares    = prev_shares - shares
        new_avg_price = prev_buy_price  # cost basis unchanged on partial sell
        new_invested  = prev_invested - (prev_invested * (shares / prev_shares)) if prev_shares > 0 else 0.0

    new_shares    = round(new_shares, 6)
    new_avg_price = round(new_avg_price, 6)
    new_invested  = round(new_invested, 4)

    # P/L for sell
    pl_eur = round((price - prev_buy_price) * shares - fee, 4) if action == "SELL" else None

    # ── Print summary ────────────────────────────────────────────────────────
    print()
    print(f"  {'DRY RUN — ' if args.dry_run else ''}Recording trade for {inst['name']} ({inst['id']})")
    print(f"  ─────────────────────────────────────────────────")
    print(f"  Action:       {action}")
    print(f"  Shares:       {shares}")
    print(f"  Price:        {price:.4f} EUR/share")
    print(f"  Fee:          {fee:.2f} EUR")
    print(f"  Total:        {total_eur:.4f} EUR  ({'paid' if action == 'BUY' else 'received'})")
    print(f"  Timestamp:    {ts}")
    if pl_eur is not None:
        sign = "+" if pl_eur >= 0 else ""
        print(f"  P/L:          {sign}{pl_eur:.4f} EUR")
    print()
    print(f"  Position BEFORE → AFTER:")
    print(f"    Shares held:  {prev_shares:.6f} → {new_shares:.6f}")
    print(f"    Avg buy price: {prev_buy_price:.4f} → {new_avg_price:.4f} EUR")
    print(f"    Total invested: {prev_invested:.4f} → {new_invested:.4f} EUR")
    print()

    if args.dry_run:
        print("  ⚠️  Dry run — nothing written.")
        return

    # ── Apply changes ────────────────────────────────────────────────────────
    # 1. Update instruments.json
    inst["shares_held"]    = new_shares
    inst["buy_price"]      = new_avg_price
    inst["total_invested"] = new_invested
    save_instruments(instruments)
    print(f"  ✅ instruments.json updated")

    # 2. Insert into DB
    init_db()  # ensure schema exists
    con = sqlite3.connect(DB_PATH)
    ensure_trades_table(con)
    con.execute(
        """INSERT INTO trades
              (instrument_id, ts, action, shares, price, fee, total_eur, source,
               avg_cost_after, shares_after, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (inst["id"], ts, action, shares, price, fee, total_eur, source,
         new_avg_price, new_shares, notes)
    )
    con.commit()
    trade_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.close()
    print(f"  ✅ Trade recorded in DB (id={trade_id})")
    print()


def list_trades(args):
    if not DB_PATH.exists():
        print("No database found.")
        return

    con = sqlite3.connect(DB_PATH)
    ensure_trades_table(con)

    if args.list != "all":
        rows = con.execute(
            """SELECT id, instrument_id, ts, action, shares, price, fee, total_eur,
                      source, avg_cost_after, shares_after, notes
                 FROM trades WHERE instrument_id = ?
             ORDER BY ts ASC""",
            (args.list,),
        ).fetchall()
    else:
        rows = con.execute(
            """SELECT id, instrument_id, ts, action, shares, price, fee, total_eur,
                      source, avg_cost_after, shares_after, notes
                 FROM trades
             ORDER BY ts ASC"""
        ).fetchall()
    con.close()

    if not rows:
        print(f"No trades recorded yet for '{args.list}'.")
        return

    print(f"\n  {'ID':>4}  {'Instrument':<12}  {'Timestamp':>22}  {'Action':>4}  "
          f"{'Shares':>12}  {'Price':>8}  {'Fee':>5}  {'Total EUR':>10}  {'Source':>7}  Notes")
    print("  " + "─" * 105)
    for r in rows:
        id_, inst_id, ts, action, shares, price, fee, total, src, avg, saft, notes = r
        notes_str = (notes or "")[:20]
        print(f"  {id_:>4}  {inst_id:<12}  {ts:>22}  {action:>4}  "
              f"{shares:>12.6f}  {price:>8.4f}  {fee:>5.2f}  {total:>10.4f}  {src:>7}  {notes_str}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Record a manual trade and update instrument position state.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-i", "--instrument", help="Instrument ID (e.g. itky-as)")
    parser.add_argument("-a", "--action",     help="buy or sell")
    parser.add_argument("--shares",  type=float, help="Number of shares transacted")
    parser.add_argument("--price",   type=float, help="Price per share in EUR")
    parser.add_argument("--fee",     type=float, default=None,
                        help="Transaction fee in EUR (default: from instruments.json)")
    parser.add_argument("--total",   type=float, default=None,
                        help="Total EUR paid/received (overrides fee calc if given)")
    parser.add_argument("--ts",      type=str,   default=None,
                        help="Trade timestamp ISO UTC (default: now)")
    parser.add_argument("--source",  type=str,   default="MANUAL",
                        help="MANUAL or AUTO (default: MANUAL)")
    parser.add_argument("--notes",   type=str,   default=None,
                        help="Optional note")
    parser.add_argument("-l", "--list", type=str, metavar="INSTRUMENT_ID",
                        nargs="?", const="all",
                        help="List trade history (instrument ID or 'all')")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing anything")

    args = parser.parse_args()

    if args.list is not None:
        list_trades(args)
        return

    missing = [f for f, v in [
        ("--instrument", args.instrument),
        ("--action",     args.action),
        ("--shares",     args.shares),
        ("--price",      args.price),
    ] if v is None]
    if missing:
        parser.error(f"Missing required arguments: {', '.join(missing)}")

    record_trade(args)


if __name__ == "__main__":
    main()
