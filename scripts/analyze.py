#!/usr/bin/env python3
"""
Analyse stored prices per instrument using two independent signal engines:

  1. CONFLUENCE — RSI(14) + MACD(12,26,9) + MA crossover + % threshold.
     Score-based: fires when ≥2 conditions agree. Confidence: LOW/MEDIUM/HIGH.

  2. MACD-ONLY  — MACD line crosses its signal line. More forward-looking,
     less lag than simple MA crossover.

One combined Telegram message is sent per instrument when either engine fires.

Usage:
  python analyze.py                    # all instruments, respects cooldown
  python analyze.py --instrument sgbs-as
  python analyze.py --force / -f       # bypass cooldown, print live stats
"""
import argparse
import json
import logging
import math
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import DB_PATH, LOG_DIR, SCRIPTS_DIR, TELEGRAM_BOT_TOKEN, TELEGRAM_SCRIPT
from init_db import init_db

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_DIR / "analyzer.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


# ── Instrument registry ───────────────────────────────────────────────────────

def load_instruments(instrument_id=None):
    instruments = json.loads((SCRIPTS_DIR / "instruments.json").read_text())
    if instrument_id:
        instruments = [i for i in instruments if i["id"] == instrument_id]
        if not instruments:
            raise ValueError(f"Instrument '{instrument_id}' not found")
    return instruments


# ── Technical indicators ──────────────────────────────────────────────────────

def ema(values, period):
    """Exponential moving average."""
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def compute_rsi(closes, period=14):
    """RSI(14) — returns latest RSI value."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_macd(closes, fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, prev_macd, prev_signal)."""
    if len(closes) < slow + signal:
        return None, None, None, None
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    return (
        macd_line[-1], signal_line[-1],
        macd_line[-2] if len(macd_line) > 1 else None,
        signal_line[-2] if len(signal_line) > 1 else None,
    )


def compute_ma(closes, period_bars):
    if len(closes) < period_bars + 1:
        return None, None
    current = sum(closes[-period_bars:]) / period_bars
    prev    = sum(closes[-period_bars - 1:-1]) / period_bars
    return current, prev


# ── DB helpers ────────────────────────────────────────────────────────────────

def fetch_closes(con, instrument_id, n):
    rows = con.execute(
        "SELECT close FROM prices WHERE instrument_id=? ORDER BY ts DESC LIMIT ?",
        (instrument_id, n),
    ).fetchall()
    return [r[0] for r in reversed(rows)]


def last_signal_ts(con, instrument_id, engine, signal_type):
    row = con.execute(
        "SELECT ts FROM signals WHERE instrument_id=? AND engine=? AND signal_type=? "
        "ORDER BY ts DESC LIMIT 1",
        (instrument_id, engine, signal_type),
    ).fetchone()
    return row[0] if row else None


def in_cooldown(con, instrument_id, engine, signal_type, cooldown_minutes):
    last = last_signal_ts(con, instrument_id, engine, signal_type)
    if not last:
        return False
    last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - last_dt < timedelta(minutes=cooldown_minutes)


def save_signal(con, instrument_id, engine, signal_type, price, reason,
                ma_short, ma_long, rsi, macd_line, macd_sig, pct, confidence):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute(
        """INSERT INTO signals
           (instrument_id, ts, signal_type, engine, price, reason,
            ma_short, ma_long, rsi, macd_line, macd_signal_line, pct_from_buy, confidence)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (instrument_id, ts, signal_type, engine, price, reason,
         ma_short, ma_long, rsi, macd_line, macd_sig, pct, confidence),
    )
    con.commit()
    logging.info("[%s][%s] Signal: %s price=%.4f pct=%.2f%% conf=%s reason=%s",
                 instrument_id, engine, signal_type, price, pct or 0, confidence, reason)


# ── Signal engines ────────────────────────────────────────────────────────────

def engine_confluence(closes, inst, pct):
    """
    Score-based engine. Returns (signal_type, reasons, score, rsi_val, macd_l, macd_s).
    Fires when score >= 2.
    """
    short_bars = inst["ma_short_minutes"] // 2
    long_bars  = inst["ma_long_minutes"]  // 2
    buy_price  = inst.get("buy_price")
    threshold  = inst["signal_threshold_pct"]

    ma_short, ma_short_prev = compute_ma(closes, short_bars)
    ma_long,  ma_long_prev  = compute_ma(closes, long_bars)
    rsi_val = compute_rsi(closes)
    macd_l, macd_s, macd_l_prev, macd_s_prev = compute_macd(closes)

    if ma_short is None or ma_long is None:
        return None, [], 0, None, None, None

    price = closes[-1]
    crossed_below = ma_short_prev >= ma_long_prev and ma_short < ma_long
    crossed_above = ma_short_prev <= ma_long_prev and ma_short > ma_long
    macd_crossed_below = (macd_l_prev is not None and macd_s_prev is not None
                          and macd_l_prev >= macd_s_prev and macd_l < macd_s)
    macd_crossed_above = (macd_l_prev is not None and macd_s_prev is not None
                          and macd_l_prev <= macd_s_prev and macd_l > macd_s)

    sell_score, sell_reasons = 0, []
    buy_score,  buy_reasons  = 0, []

    # MA trend
    if ma_short < ma_long:
        sell_score += 1; sell_reasons.append(f"MA{inst['ma_short_minutes']} below MA{inst['ma_long_minutes']} (bearish trend)")
    if ma_short > ma_long:
        buy_score  += 1; buy_reasons.append(f"MA{inst['ma_short_minutes']} above MA{inst['ma_long_minutes']} (bullish trend)")

    # RSI
    if rsi_val is not None:
        if rsi_val > 75:
            sell_score += 2; sell_reasons.append(f"RSI {rsi_val:.1f} — strongly overbought")
        elif rsi_val > 65:
            sell_score += 1; sell_reasons.append(f"RSI {rsi_val:.1f} — overbought")
        elif rsi_val < 25:
            buy_score  += 2; buy_reasons.append(f"RSI {rsi_val:.1f} — strongly oversold")
        elif rsi_val < 35:
            buy_score  += 1; buy_reasons.append(f"RSI {rsi_val:.1f} — oversold")

    # MACD crossover
    if macd_crossed_below:
        sell_score += 1; sell_reasons.append("MACD crossed below signal line (bearish)")
    if macd_crossed_above:
        buy_score  += 1; buy_reasons.append("MACD crossed above signal line (bullish)")

    # Price vs buy threshold (only if we have a position)
    if buy_price and pct is not None:
        if pct >= threshold and price > buy_price:
            sell_score += 1; sell_reasons.append(f"Price {pct:+.2f}% above entry — take profit")
        if pct <= -threshold:
            if ma_short >= ma_long:  # don't recommend buying into downtrend
                buy_score += 1; buy_reasons.append(f"Price {pct:+.2f}% below entry — dip opportunity")

    def confidence(score):
        if score >= 4: return "HIGH"
        if score >= 3: return "MEDIUM"
        return "LOW"

    if sell_score >= 2 and sell_score >= buy_score:
        return "SELL", sell_reasons, sell_score, rsi_val, macd_l, macd_s
    if buy_score >= 2:
        return "BUY", buy_reasons, buy_score, rsi_val, macd_l, macd_s
    return None, [], max(sell_score, buy_score), rsi_val, macd_l, macd_s


def engine_macd_only(closes, inst):
    """
    Pure MACD crossover engine.
    Returns (signal_type, reason, macd_l, macd_s).
    """
    macd_l, macd_s, macd_l_prev, macd_s_prev = compute_macd(closes)
    if macd_l is None or macd_l_prev is None:
        return None, None, None, None

    if macd_l_prev >= macd_s_prev and macd_l < macd_s:
        return "SELL", f"MACD ({macd_l:.4f}) crossed below signal ({macd_s:.4f})", macd_l, macd_s
    if macd_l_prev <= macd_s_prev and macd_l > macd_s:
        return "BUY",  f"MACD ({macd_l:.4f}) crossed above signal ({macd_s:.4f})", macd_l, macd_s
    return None, None, macd_l, macd_s


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(inst, price, pct, results):
    """
    results = list of dicts with engine result data.
    Sends one combined message per instrument.
    """
    if not TELEGRAM_BOT_TOKEN:
        logging.warning("TELEGRAM_BOT_TOKEN not set, skipping")
        return

    name      = inst["name"]
    buy_price = inst.get("buy_price")
    shares    = inst.get("shares_held", 0)
    currency  = inst.get("currency", "EUR")
    pct_sign  = "+" if (pct or 0) >= 0 else ""

    lines = [f"📊 SIGNAL REPORT — {name}", "─" * 36]

    for r in results:
        engine_label = "CONFLUENCE" if r["engine"] == "confluence" else "MACD ENGINE"
        sig  = r["signal_type"]
        emoji = "🔴" if sig == "SELL" else "🟢"
        conf_str = f" [{r['confidence']} confidence, score {r.get('score','')}/5]" if r["engine"] == "confluence" else ""
        lines.append(f"{emoji} {engine_label}: {sig}{conf_str}")
        lines.append(f"   {r['reason']}")
        if r.get("rsi") is not None:
            lines.append(f"   RSI: {r['rsi']:.1f}  |  MACD: {r.get('macd_l', 0):.4f}  Signal: {r.get('macd_s', 0):.4f}")
        lines.append("")

    if buy_price:
        pl = (price - buy_price) * shares
        pl_pct = (price / buy_price - 1) * 100
        pl_sign = "+" if pl >= 0 else ""
        lines.append(f"📈 Price: {price:.2f} {currency}  ({pct_sign}{pct:.2f}% from buy at {buy_price:.2f})")
        lines.append(f"💰 P/L:   {pl_sign}{pl:.4f} {currency}  ({pl_sign}{pl_pct:.2f}%)")
    else:
        lines.append(f"📈 Price: {price:.2f} {currency}  (watch-only — no position yet)")

    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")

    msg = "\n".join(lines)
    try:
        env = os.environ.copy()
        env["TELEGRAM_BOT_TOKEN"] = TELEGRAM_BOT_TOKEN
        subprocess.run(["bash", str(TELEGRAM_SCRIPT), msg], env=env, check=True, capture_output=True)
        logging.info("[%s] Telegram sent", inst["id"])
    except subprocess.CalledProcessError as exc:
        logging.error("[%s] Telegram failed: %s", inst["id"], exc.stderr)


# ── Data staleness helper ─────────────────────────────────────────────────────

def staleness_warning(last_ts_str, inst):
    if not last_ts_str:
        return "  ⚠️  No price data in DB\n"
    last_dt = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
    age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
    now_utc = datetime.now(timezone.utc)
    open_h, open_m   = map(int, inst["market_open_utc"].split(":"))
    close_h, close_m = map(int, inst["market_close_utc"].split(":"))
    market_open = (
        now_utc.weekday() < 5
        and (open_h, open_m) <= (now_utc.hour, now_utc.minute) <= (close_h, close_m)
    )
    if market_open and age_min > 30:
        return (f"  ⚠️  Last bar is {age_min:.0f} min old ({last_ts_str}) — "
                f"data may be stale (yfinance lags ~15–20 min for low-volume ETFs)\n")
    if not market_open:
        return f"  ℹ️  Market closed. Last bar: {last_ts_str} ({age_min:.0f} min ago)\n"
    return ""


# ── Main per-instrument analysis ──────────────────────────────────────────────

def analyse_instrument(inst, con, force=False):
    iid       = inst["id"]
    buy_price = inst.get("buy_price")
    cooldown  = inst["alert_cooldown_minutes"]
    short_bars = inst["ma_short_minutes"] // 2
    long_bars  = inst["ma_long_minutes"]  // 2
    # Need enough bars for MACD(12,26,9) + 1 prev: 26+9+1 = 36 bars minimum
    min_bars = max(long_bars + 1, 36)

    total_rows = con.execute(
        "SELECT COUNT(*) FROM prices WHERE instrument_id=?", (iid,)
    ).fetchone()[0]
    last_ts = con.execute(
        "SELECT ts FROM prices WHERE instrument_id=? ORDER BY ts DESC LIMIT 1", (iid,)
    ).fetchone()
    last_ts_str = last_ts[0] if last_ts else None

    closes = fetch_closes(con, iid, min_bars + 1)

    if force:
        print(f"\n{'═'*42}")
        print(f"  Instrument: {inst['name']} ({iid})")
        print(staleness_warning(last_ts_str, inst), end="")

    if len(closes) < min_bars:
        msg = f"  [{iid}] Not enough data ({len(closes)}/{min_bars} bars needed)"
        logging.info(msg)
        if force: print(msg)
        return

    price = closes[-1]
    pct   = ((price - buy_price) / buy_price * 100) if buy_price else None

    if force:
        print(f"  Last bar:  {last_ts_str}")
        print(f"  Price:     {price:.4f} {inst['currency']}"
              + (f"  ({pct:+.2f}% from buy at {buy_price})" if pct is not None else "  (watch-only)"))
        rsi_val = compute_rsi(closes)
        macd_l, macd_s, _, _ = compute_macd(closes)
        ma_s, _ = compute_ma(closes, short_bars)
        ma_l, _ = compute_ma(closes, long_bars)
        print(f"  MA{inst['ma_short_minutes']}:      {ma_s:.4f}  |  MA{inst['ma_long_minutes']}:  {ma_l:.4f}")
        print(f"  RSI(14):   {rsi_val:.2f}" if rsi_val else "  RSI(14):   n/a")
        print(f"  MACD:      {macd_l:.4f}  |  Signal: {macd_s:.4f}" if macd_l else "  MACD:      n/a")
        print(f"  Bars:      {total_rows}")

    # Run both engines
    c_sig, c_reasons, c_score, rsi_v, macd_l, macd_s = engine_confluence(closes, inst, pct)
    m_sig, m_reason, m_macd_l, m_macd_s = engine_macd_only(closes, inst)

    def conf_label(score):
        if score >= 4: return "HIGH"
        if score >= 3: return "MEDIUM"
        return "LOW"

    telegram_results = []

    # Confluence engine
    if c_sig and (force or not in_cooldown(con, iid, "confluence", c_sig, cooldown)):
        reason_str = " + ".join(c_reasons)
        save_signal(con, iid, "confluence", c_sig, price, reason_str,
                    compute_ma(closes, short_bars)[0], compute_ma(closes, long_bars)[0],
                    rsi_v, macd_l, macd_s, pct, conf_label(c_score))
        telegram_results.append({
            "engine": "confluence", "signal_type": c_sig,
            "reason": reason_str, "confidence": conf_label(c_score),
            "score": c_score, "rsi": rsi_v, "macd_l": macd_l, "macd_s": macd_s,
        })
        if force:
            print(f"\n  ✅ CONFLUENCE: {c_sig} [{conf_label(c_score)}, score {c_score}]")
            print(f"     {reason_str}")
    elif force:
        print(f"\n  ℹ️  CONFLUENCE: no signal (score={c_score}/5, "
              f"cooldown={in_cooldown(con, iid, 'confluence', 'SELL', cooldown) or in_cooldown(con, iid, 'confluence', 'BUY', cooldown)})")

    # MACD engine
    if m_sig and (force or not in_cooldown(con, iid, "macd", m_sig, cooldown)):
        save_signal(con, iid, "macd", m_sig, price, m_reason,
                    compute_ma(closes, short_bars)[0], compute_ma(closes, long_bars)[0],
                    rsi_v, m_macd_l, m_macd_s, pct, None)
        telegram_results.append({
            "engine": "macd", "signal_type": m_sig,
            "reason": m_reason, "macd_l": m_macd_l, "macd_s": m_macd_s,
        })
        if force:
            print(f"  ✅ MACD ENGINE: {m_sig}")
            print(f"     {m_reason}")
    elif force:
        macd_l_v, macd_s_v, _, _ = compute_macd(closes)
        print(f"  ℹ️  MACD ENGINE: no crossover "
              + (f"(MACD={macd_l_v:.4f}, Signal={macd_s_v:.4f})" if macd_l_v else "(insufficient data)"))

    if telegram_results:
        send_telegram(inst, price, pct or 0, telegram_results)


# ── Entry point ───────────────────────────────────────────────────────────────

def analyse(instrument_id=None, force=False):
    init_db()
    if force:
        import collect_price
        collect_price.collect(instrument_id)

    instruments = load_instruments(instrument_id)
    con = sqlite3.connect(DB_PATH)
    try:
        for inst in instruments:
            analyse_instrument(inst, con, force=force)
    finally:
        con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", "-i", help="Instrument ID (default: all)")
    parser.add_argument("--force", "-f", action="store_true",
                        help="Bypass cooldown, fetch fresh data, print live stats")
    args = parser.parse_args()
    if args.force:
        print("=== Manual Analysis Run (cooldown bypassed) ===")
    analyse(args.instrument, args.force)
