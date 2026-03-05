#!/usr/bin/env python3
"""
Analyse stored prices, generate BUY/SELL signals, and trigger Telegram alerts.

Signal logic:
  SELL  → MA15 crosses below MA60  AND/OR  price >= buy_price * (1 + threshold%)
  BUY   → MA15 crosses above MA60  AND/OR  price <= buy_price * (1 - threshold%)
  1-hour cooldown per signal type to prevent spam.
"""
import logging
import sqlite3
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from config import (
    ALERT_COOLDOWN_MINUTES,
    BUY_PRICE,
    DB_PATH,
    LOG_DIR,
    MA_LONG_MINUTES,
    MA_SHORT_MINUTES,
    SHARES_HELD,
    SIGNAL_THRESHOLD_PCT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_SCRIPT,
)
from init_db import init_db

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_DIR / "analyzer.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# MA windows need at least this many data points (2-min bars)
MA_SHORT_BARS = MA_SHORT_MINUTES // 2   # 7 bars  ≈ 15 min
MA_LONG_BARS  = MA_LONG_MINUTES  // 2   # 30 bars ≈ 60 min
MIN_BARS_NEEDED = MA_LONG_BARS + 1      # need 1 extra for crossover detection


def fetch_recent_closes(con, n):
    rows = con.execute(
        "SELECT close FROM prices ORDER BY ts DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in reversed(rows)]  # oldest first


def last_signal_time(con, signal_type):
    row = con.execute(
        "SELECT ts FROM signals WHERE signal_type=? ORDER BY ts DESC LIMIT 1",
        (signal_type,),
    ).fetchone()
    return row[0] if row else None


def in_cooldown(con, signal_type):
    last = last_signal_time(con, signal_type)
    if not last:
        return False
    last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - last_dt < timedelta(minutes=ALERT_COOLDOWN_MINUTES)


def save_signal(con, signal_type, price, reason, ma15, ma60, pct):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute(
        """INSERT INTO signals (ts, signal_type, price, reason, ma15, ma60, pct_from_buy)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ts, signal_type, price, reason, ma15, ma60, pct),
    )
    con.commit()
    logging.info("Signal saved: %s price=%.4f pct=%.2f%% reason=%s", signal_type, price, pct, reason)
    return ts


def send_telegram(signal_type, price, pct, ma15, ma60, reason, total_rows):
    if not TELEGRAM_BOT_TOKEN:
        logging.warning("TELEGRAM_BOT_TOKEN not set, skipping notification")
        return

    pl        = (price - BUY_PRICE) * SHARES_HELD
    pl_pct    = (price / BUY_PRICE - 1) * 100
    pl_sign   = "+" if pl >= 0 else ""
    pct_sign  = "+" if pct >= 0 else ""
    trend     = "above" if ma15 > ma60 else "below"

    if signal_type == "SELL":
        action_line = (
            "Consider SELLING your position to lock in gains.\n"
            f"   Selling now at {price:.2f}€ would give you ~{BUY_PRICE + pl/SHARES_HELD:.2f}€/share."
        )
        emoji = "🔴"
    else:
        action_line = (
            "Consider BUYING more to average down your position.\n"
            f"   Current price is below your entry of {BUY_PRICE:.2f}€."
        )
        emoji = "🟢"

    msg = (
        f"{emoji} GOLD SIGNAL: {signal_type}\n"
        f"{'─' * 28}\n"
        f"💡 What to do: {action_line}\n\n"
        f"📌 Why: {reason}\n\n"
        f"📈 Price now:  {price:.2f} EUR  ({pct_sign}{pct:.2f}% from your buy at {BUY_PRICE:.2f})\n"
        f"💰 Your P/L:  {pl_sign}{pl:.4f} EUR  ({pl_sign}{pl_pct:.2f}%)\n"
        f"📊 MA{MA_SHORT_MINUTES} vs MA{MA_LONG_MINUTES}: {ma15:.2f} vs {ma60:.2f}  "
        f"(short MA is {trend} long MA)\n"
        f"📦 Data points: {total_rows} price bars collected\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
    )

    try:
        env = os.environ.copy()
        env["TELEGRAM_BOT_TOKEN"] = TELEGRAM_BOT_TOKEN
        subprocess.run(
            ["bash", str(TELEGRAM_SCRIPT), msg],
            env=env,
            check=True,
            capture_output=True,
        )
        logging.info("Telegram notification sent for %s", signal_type)
    except subprocess.CalledProcessError as exc:
        logging.error("Telegram send failed: %s", exc.stderr)


def analyse(force=False):
    init_db()
    con = sqlite3.connect(DB_PATH)

    try:
        total_rows = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        closes = fetch_recent_closes(con, MIN_BARS_NEEDED + 1)
        if len(closes) < MIN_BARS_NEEDED:
            msg = f"Not enough data yet ({len(closes)}/{MIN_BARS_NEEDED} bars)"
            logging.info(msg)
            if force:
                print(msg)
            return

        price    = closes[-1]
        ma15     = sum(closes[-MA_SHORT_BARS:]) / MA_SHORT_BARS
        ma60     = sum(closes[-MA_LONG_BARS:])  / MA_LONG_BARS
        ma15_prev = sum(closes[-MA_SHORT_BARS-1:-1]) / MA_SHORT_BARS
        ma60_prev = sum(closes[-MA_LONG_BARS-1:-1])  / MA_LONG_BARS

        pct = (price - BUY_PRICE) / BUY_PRICE * 100

        crossed_below = (ma15_prev >= ma60_prev) and (ma15 < ma60)  # bearish crossover
        crossed_above = (ma15_prev <= ma60_prev) and (ma15 > ma60)  # bullish crossover
        above_threshold = pct >= SIGNAL_THRESHOLD_PCT
        below_threshold = pct <= -SIGNAL_THRESHOLD_PCT

        logging.info(
            "price=%.4f ma15=%.4f ma60=%.4f pct=%.2f%% cross_below=%s cross_above=%s",
            price, ma15, ma60, pct, crossed_below, crossed_above,
        )

        if force:
            pl = (price - BUY_PRICE) * SHARES_HELD
            print(f"  Price:   {price:.4f} EUR  ({pct:+.2f}% from buy at {BUY_PRICE})")
            print(f"  MA{MA_SHORT_MINUTES}:    {ma15:.4f}")
            print(f"  MA{MA_LONG_MINUTES}:    {ma60:.4f}")
            print(f"  P/L:     {pl:+.4f} EUR")
            print(f"  Bars:    {total_rows}")
            print(f"  Crossed below: {crossed_below} | Crossed above: {crossed_above}")
            print(f"  Above threshold: {above_threshold} | Below threshold: {below_threshold}")

        signal_type = None
        reasons = []

        sell_reasons = []
        if crossed_below:
            sell_reasons.append(f"MA{MA_SHORT_MINUTES} crossed below MA{MA_LONG_MINUTES} (bearish)")
        if above_threshold:
            sell_reasons.append(f"Price {pct:+.2f}% above buy price — take profit")

        if sell_reasons and (force or not in_cooldown(con, "SELL")):
            signal_type = "SELL"
            reasons = sell_reasons

        if signal_type is None:
            buy_reasons = []
            if crossed_above:
                buy_reasons.append(f"MA{MA_SHORT_MINUTES} crossed above MA{MA_LONG_MINUTES} (bullish)")
            if below_threshold:
                buy_reasons.append(f"Price {pct:+.2f}% below buy price — potential dip buy")

            if buy_reasons and (force or not in_cooldown(con, "BUY")):
                signal_type = "BUY"
                reasons = buy_reasons

        if signal_type:
            reason_str = " + ".join(reasons)
            save_signal(con, signal_type, price, reason_str, ma15, ma60, pct)
            send_telegram(signal_type, price, pct, ma15, ma60, reason_str, total_rows)
            if force:
                print(f"\n  ✅ Signal fired: {signal_type} — Telegram sent.")
        else:
            if force:
                cooldown_sell = in_cooldown(con, "SELL")
                cooldown_buy  = in_cooldown(con, "BUY")
                print(f"\n  ℹ️  No signal. Cooldown: SELL={cooldown_sell}, BUY={cooldown_buy}")
                if not sell_reasons and not (crossed_above or below_threshold):
                    print("  Conditions not met for any signal at this time.")

    finally:
        con.close()


if __name__ == "__main__":
    force = "--force" in sys.argv or "-f" in sys.argv
    if force:
        print("=== Manual Analysis Run (cooldown bypassed) ===")
    analyse(force=force)
