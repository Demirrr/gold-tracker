#!/usr/bin/env python3
"""
Analyse stored prices per instrument using two independent signal engines:

  1. CONFLUENCE — EMA Ribbon(5/8/21) + RSI(14) + MACD(12,26,9) + VWAP + ATR + Volume.
     Score-based day-trader engine. Fires at score >= 3. Confidence: LOW/MEDIUM/HIGH.
     Dynamic thresholds: take-profit and stop-loss targets scale with ATR (volatility).
     VWAP resets each session and acts as intraday fair-value anchor.
     Volume confirmation required for high-confidence signals.

  2. MACD-ONLY — MACD line crosses signal line (forward-looking, fast engine).

One combined Telegram message per instrument when either engine fires.

Usage:
  python analyze.py                       # all instruments, respects cooldown
  python analyze.py --instrument sgbs-as
  python analyze.py --force / -f          # bypass cooldown, fetch fresh data, print stats
"""
import argparse
import json
import logging
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
    """Exponential moving average; returns full series."""
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def compute_rsi(closes, period=14):
    """RSI(14) — returns latest value."""
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
    return 100 - (100 / (1 + avg_gain / avg_loss))


def compute_macd(closes, fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, prev_macd, prev_signal)."""
    if len(closes) < slow + signal:
        return None, None, None, None
    ema_fast   = ema(closes, fast)
    ema_slow   = ema(closes, slow)
    macd_line  = [f - s for f, s in zip(ema_fast, ema_slow)]
    sig_line   = ema(macd_line, signal)
    return (
        macd_line[-1], sig_line[-1],
        macd_line[-2] if len(macd_line) > 1 else None,
        sig_line[-2]  if len(sig_line)  > 1 else None,
    )


def compute_ema_ribbon(closes, fast=5, mid=8, slow=21):
    """
    EMA ribbon — returns (curr_fast, curr_mid, curr_slow, prev_fast, prev_mid, prev_slow).
    Returns all None if insufficient data.
    """
    if len(closes) < slow + 1:
        return None, None, None, None, None, None
    ef = ema(closes, fast)
    em = ema(closes, mid)
    es = ema(closes, slow)
    return ef[-1], em[-1], es[-1], ef[-2], em[-2], es[-2]


def compute_atr(highs, lows, closes, period=14):
    """Average True Range — Wilder's smoothing."""
    if len(closes) < period + 1:
        return None
    trs = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def compute_vwap_series(highs, lows, closes, volumes):
    """Cumulative VWAP series (resets per session). Returns list of VWAP values."""
    cum_tpv = cum_vol = 0.0
    result = []
    for h, l, c, v in zip(highs, lows, closes, volumes):
        tp = (h + l + c) / 3
        cum_tpv += tp * (v or 0)
        cum_vol  += (v or 0)
        result.append(cum_tpv / cum_vol if cum_vol > 0 else c)
    return result


def compute_volume_ratio(volumes, lookback=20):
    """Ratio of current bar volume to recent average. >2.0 = volume spike."""
    if len(volumes) < lookback + 1:
        return None
    avg = sum(volumes[-lookback - 1:-1]) / lookback
    return volumes[-1] / avg if avg > 0 else None


# ── DB helpers ────────────────────────────────────────────────────────────────

def fetch_ohlcv(con, instrument_id, n):
    """Fetch last n bars as arrays. Oldest bar first."""
    rows = con.execute(
        "SELECT ts, open, high, low, close, volume FROM prices "
        "WHERE instrument_id=? ORDER BY ts DESC LIMIT ?",
        (instrument_id, n),
    ).fetchall()
    rows = list(reversed(rows))
    return {
        "timestamps": [r[0] for r in rows],
        "opens":      [r[1] for r in rows],
        "highs":      [r[2] for r in rows],
        "lows":       [r[3] for r in rows],
        "closes":     [r[4] for r in rows],
        "volumes":    [r[5] for r in rows],
    }


def get_session_bars(con, instrument_id, session_open_ts):
    """Fetch today's session bars for VWAP (from market open UTC)."""
    rows = con.execute(
        "SELECT open, high, low, close, volume FROM prices "
        "WHERE instrument_id=? AND ts >= ? ORDER BY ts ASC",
        (instrument_id, session_open_ts),
    ).fetchall()
    if not rows:
        return None
    return {
        "opens":   [r[0] for r in rows],
        "highs":   [r[1] for r in rows],
        "lows":    [r[2] for r in rows],
        "closes":  [r[3] for r in rows],
        "volumes": [r[4] for r in rows],
    }


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
    logging.info("[%s][%s] Signal: %s price=%.4f pct=%s conf=%s",
                 instrument_id, engine, signal_type, price,
                 f"{pct:.2f}%" if pct is not None else "n/a", confidence)


# ── Signal engines ────────────────────────────────────────────────────────────

def engine_confluence(ohlcv, session_ohlcv, inst, pct):
    """
    Day-trader confluence engine.

    Scoring:
      EMA ribbon fully aligned   +2  (SELL or BUY)
      EMA fast/mid cross only    +1
      RSI overbought/oversold    +1 or +2
      MACD crossover             +1
      VWAP side (above/below)    +1
      VWAP crossover (fresh)     +1
      ATR take-profit target hit +1
      Volume spike confirmation  +1  (only if same direction signal already > 0)

    Fires when score >= 3.
    Safety: SELL suppressed if price < buy_price.
            BUY penalty -1 if EMA fully bearish.
    """
    closes  = ohlcv["closes"]
    highs   = ohlcv["highs"]
    lows    = ohlcv["lows"]
    opens   = ohlcv["opens"]
    volumes = ohlcv["volumes"]

    buy_price    = inst.get("buy_price")
    ema_fast_n   = inst.get("ema_fast_bars", 5)
    ema_mid_n    = inst.get("ema_mid_bars", 8)
    ema_slow_n   = inst.get("ema_slow_bars", 21)
    atr_period   = inst.get("atr_period_bars", 14)
    atr_tp_mult  = inst.get("atr_take_profit_mult", 1.5)
    vol_lookback = inst.get("volume_lookback_bars", 20)
    vol_spike    = inst.get("volume_spike_mult", 2.0)

    ef, em, es, ef_prev, em_prev, es_prev = compute_ema_ribbon(closes, ema_fast_n, ema_mid_n, ema_slow_n)
    if ef is None:
        return None, [], 0, None, None, None, None, None, None

    rsi_val = compute_rsi(closes)
    macd_l, macd_s, macd_l_prev, macd_s_prev = compute_macd(closes)
    atr_val   = compute_atr(highs, lows, closes, atr_period)
    vol_ratio = compute_volume_ratio(volumes, vol_lookback)

    price          = closes[-1]
    is_bearish_bar = price < opens[-1]
    is_bullish_bar = price >= opens[-1]

    # VWAP — requires session bars
    vwap = vwap_prev = prev_session_close = None
    if session_ohlcv and len(session_ohlcv["closes"]) >= 1:
        vwap_series = compute_vwap_series(
            session_ohlcv["highs"], session_ohlcv["lows"],
            session_ohlcv["closes"], session_ohlcv["volumes"],
        )
        vwap = vwap_series[-1]
        if len(session_ohlcv["closes"]) >= 2:
            vwap_prev          = vwap_series[-2]
            prev_session_close = session_ohlcv["closes"][-2]

    sell_score, sell_reasons = 0, []
    buy_score,  buy_reasons  = 0, []

    # ── EMA Ribbon ────────────────────────────────────────────────────────────
    fully_bearish = ef < em < es
    fully_bullish = ef > em > es
    fast_bearish  = ef < em
    fast_bullish  = ef > em

    if fully_bearish:
        sell_score += 2
        sell_reasons.append(
            f"EMA{ema_fast_n} ({ef:.3f}) < EMA{ema_mid_n} ({em:.3f}) < EMA{ema_slow_n} ({es:.3f}) — fully bearish")
    elif fast_bearish:
        sell_score += 1
        sell_reasons.append(f"EMA{ema_fast_n} ({ef:.3f}) < EMA{ema_mid_n} ({em:.3f}) — short-term bearish momentum")

    if fully_bullish:
        buy_score += 2
        buy_reasons.append(
            f"EMA{ema_fast_n} ({ef:.3f}) > EMA{ema_mid_n} ({em:.3f}) > EMA{ema_slow_n} ({es:.3f}) — fully bullish")
    elif fast_bullish:
        buy_score += 1
        buy_reasons.append(f"EMA{ema_fast_n} ({ef:.3f}) > EMA{ema_mid_n} ({em:.3f}) — short-term bullish momentum")

    # ── RSI ───────────────────────────────────────────────────────────────────
    if rsi_val is not None:
        if rsi_val > 75:
            sell_score += 2; sell_reasons.append(f"RSI {rsi_val:.1f} — strongly overbought")
        elif rsi_val > 65:
            sell_score += 1; sell_reasons.append(f"RSI {rsi_val:.1f} — overbought")
        elif rsi_val < 25:
            buy_score  += 2; buy_reasons.append(f"RSI {rsi_val:.1f} — strongly oversold")
        elif rsi_val < 35:
            buy_score  += 1; buy_reasons.append(f"RSI {rsi_val:.1f} — oversold")

    # ── MACD crossover ────────────────────────────────────────────────────────
    if macd_l is not None and macd_l_prev is not None and macd_s_prev is not None:
        if macd_l_prev >= macd_s_prev and macd_l < macd_s:
            sell_score += 1; sell_reasons.append(f"MACD ({macd_l:.4f}) crossed below signal ({macd_s:.4f})")
        elif macd_l_prev <= macd_s_prev and macd_l > macd_s:
            buy_score  += 1; buy_reasons.append(f"MACD ({macd_l:.4f}) crossed above signal ({macd_s:.4f})")

    # ── VWAP context and crossover ────────────────────────────────────────────
    if vwap is not None:
        vwap_pct = (price - vwap) / vwap * 100
        if price < vwap:
            sell_score += 1
            sell_reasons.append(f"Price {vwap_pct:.2f}% below VWAP ({vwap:.3f}) — bearish intraday context")
        else:
            buy_score += 1
            buy_reasons.append(f"Price +{vwap_pct:.2f}% above VWAP ({vwap:.3f}) — bullish intraday context")

        if vwap_prev is not None and prev_session_close is not None:
            if prev_session_close >= vwap_prev and price < vwap:
                sell_score += 1; sell_reasons.append(f"Price crossed below VWAP ({vwap:.3f}) — fresh bearish signal")
            elif prev_session_close <= vwap_prev and price > vwap:
                buy_score  += 1; buy_reasons.append(f"Price crossed above VWAP ({vwap:.3f}) — fresh bullish signal")

    # ── ATR take-profit target ────────────────────────────────────────────────
    if atr_val is not None and buy_price is not None:
        atr_tp = buy_price + atr_tp_mult * atr_val
        if price >= atr_tp and price > buy_price:
            sell_score += 1
            sell_reasons.append(f"Price ({price:.3f}) ≥ ATR take-profit target ({atr_tp:.3f} = entry + {atr_tp_mult}× ATR)")

    # ── Volume confirmation ───────────────────────────────────────────────────
    if vol_ratio is not None and vol_ratio >= vol_spike:
        if is_bearish_bar and sell_score > 0:
            sell_score += 1
            sell_reasons.append(f"Volume spike {vol_ratio:.1f}× avg on bearish bar — confirms sell pressure")
        elif is_bullish_bar and buy_score > 0:
            buy_score  += 1
            buy_reasons.append(f"Volume spike {vol_ratio:.1f}× avg on bullish bar — confirms buy pressure")

    # ── Safety rules ──────────────────────────────────────────────────────────
    if buy_price and price < buy_price:
        # Never recommend selling at a loss
        sell_score   = 0
        sell_reasons = []

    if fully_bearish and buy_score > 0:
        # Penalise buying when all EMAs point down
        buy_score = max(0, buy_score - 1)
        if buy_reasons:
            buy_reasons.append(f"⚠️  -1 pt: EMA fully bearish — buying into downtrend is risky")

    def conf_label(score):
        if score >= 5: return "HIGH"
        if score >= 4: return "MEDIUM"
        return "LOW"

    if sell_score >= 3 and sell_score >= buy_score:
        return "SELL", sell_reasons, sell_score, rsi_val, macd_l, macd_s, vwap, atr_val, vol_ratio
    if buy_score >= 3:
        return "BUY",  buy_reasons,  buy_score,  rsi_val, macd_l, macd_s, vwap, atr_val, vol_ratio
    return None, [], max(sell_score, buy_score), rsi_val, macd_l, macd_s, vwap, atr_val, vol_ratio


def engine_macd_only(closes, inst):
    """Pure MACD crossover — forward-looking, fast engine."""
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
        sig   = r["signal_type"]
        emoji = "🔴" if sig == "SELL" else "🟢"
        if r["engine"] == "confluence":
            conf_str = f" [{r['confidence']} confidence, score {r.get('score','')}/9]"
        else:
            conf_str = ""
        lines.append(f"{emoji} {engine_label}: {sig}{conf_str}")
        lines.append(f"   {r['reason']}")
        if r.get("rsi") is not None:
            lines.append(f"   RSI: {r['rsi']:.1f}  |  MACD: {r.get('macd_l', 0):.4f}  Signal: {r.get('macd_s', 0):.4f}")
        if r.get("vwap") is not None:
            vwap_pct = (price - r["vwap"]) / r["vwap"] * 100
            lines.append(f"   VWAP: {r['vwap']:.3f}  (price {'+' if vwap_pct >= 0 else ''}{vwap_pct:.2f}% from VWAP)")
        if r.get("atr") is not None and buy_price:
            atr_tp_mult = inst.get("atr_take_profit_mult", 1.5)
            tp = buy_price + atr_tp_mult * r["atr"]
            sl = buy_price - atr_tp_mult * r["atr"]
            lines.append(f"   ATR: {r['atr']:.3f}  |  TP: {tp:.3f}  |  SL: {sl:.3f}")
        if r.get("vol_ratio") is not None:
            lines.append(f"   Volume: {r['vol_ratio']:.1f}× avg")
        lines.append("")

    if buy_price:
        pl     = (price - buy_price) * shares
        pl_pct = (price / buy_price - 1) * 100
        pl_s   = "+" if pl >= 0 else ""
        lines.append(f"📈 Price: {price:.2f} {currency}  ({pct_sign}{pct:.2f}% from buy at {buy_price:.2f})")
        lines.append(f"💰 P/L:   {pl_s}{pl:.4f} {currency}  ({pl_s}{pl_pct:.2f}%)")
    else:
        lines.append(f"📈 Price: {price:.2f} {currency}  (watch-only)")

    lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")

    msg = "\n".join(lines)
    try:
        env = os.environ.copy()
        env["TELEGRAM_BOT_TOKEN"] = TELEGRAM_BOT_TOKEN
        subprocess.run(["bash", str(TELEGRAM_SCRIPT), msg], env=env, check=True, capture_output=True)
        logging.info("[%s] Telegram sent", inst["id"])
    except subprocess.CalledProcessError as exc:
        logging.error("[%s] Telegram failed: %s", inst["id"], exc.stderr)


# ── Staleness helper ──────────────────────────────────────────────────────────

def staleness_warning(last_ts_str, inst):
    if not last_ts_str:
        return "  ⚠️  No price data in DB\n"
    last_dt = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
    age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
    now_utc = datetime.now(timezone.utc)
    open_h,  open_m  = map(int, inst["market_open_utc"].split(":"))
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
    iid         = inst["id"]
    buy_price   = inst.get("buy_price")
    cooldown    = inst["alert_cooldown_minutes"]
    ema_slow_n  = inst.get("ema_slow_bars", 21)
    min_bars    = max(ema_slow_n + 1, 36)  # need ≥36 for MACD(12,26,9)

    total_rows = con.execute(
        "SELECT COUNT(*) FROM prices WHERE instrument_id=?", (iid,)
    ).fetchone()[0]
    last_ts_row = con.execute(
        "SELECT ts FROM prices WHERE instrument_id=? ORDER BY ts DESC LIMIT 1", (iid,)
    ).fetchone()
    last_ts_str = last_ts_row[0] if last_ts_row else None

    ohlcv = fetch_ohlcv(con, iid, min_bars + 1)
    closes = ohlcv["closes"]

    # Today's session bars for VWAP
    now_utc  = datetime.now(timezone.utc)
    open_h, open_m = map(int, inst["market_open_utc"].split(":"))
    session_open_dt = now_utc.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    session_open_ts = session_open_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    session_ohlcv   = get_session_bars(con, iid, session_open_ts)

    if force:
        print(f"\n{'═' * 46}")
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
        ema_fast_n  = inst.get("ema_fast_bars", 5)
        ema_mid_n   = inst.get("ema_mid_bars", 8)
        ef, em, es, *_ = compute_ema_ribbon(closes, ema_fast_n, ema_mid_n, ema_slow_n)
        rsi_val = compute_rsi(closes)
        macd_l, macd_s, _, _ = compute_macd(closes)
        atr_val   = compute_atr(ohlcv["highs"], ohlcv["lows"], closes)
        vol_ratio = compute_volume_ratio(ohlcv["volumes"])
        vwap = None
        if session_ohlcv:
            vs = compute_vwap_series(session_ohlcv["highs"], session_ohlcv["lows"],
                                     session_ohlcv["closes"], session_ohlcv["volumes"])
            vwap = vs[-1]

        pct_str = f"  ({pct:+.2f}% from buy at {buy_price})" if pct is not None else "  (watch-only)"
        print(f"  Last bar:  {last_ts_str}")
        print(f"  Price:     {price:.4f} {inst['currency']}{pct_str}")
        print(f"")
        print(f"  ── Trend ─────────────────────────────────────────")
        ribbon_dir = "BULLISH" if (ef and ef > em > es) else ("BEARISH" if (ef and ef < em < es) else "MIXED")
        print(f"  EMA{ema_fast_n}:     {ef:.4f}  EMA{ema_mid_n}: {em:.4f}  EMA{ema_slow_n}: {es:.4f}  → {ribbon_dir}")
        if vwap:
            vwap_pct = (price - vwap) / vwap * 100
            vwap_side = "above" if price >= vwap else "below"
            print(f"  VWAP:      {vwap:.4f}  (price {'+' if vwap_pct >= 0 else ''}{vwap_pct:.2f}% {vwap_side} VWAP)")
        else:
            print(f"  VWAP:      n/a (no session bars yet)")
        print(f"")
        print(f"  ── Momentum ───────────────────────────────────────")
        rsi_level = "overbought" if (rsi_val and rsi_val > 65) else ("oversold" if (rsi_val and rsi_val < 35) else "neutral")
        print(f"  RSI(14):   {rsi_val:.2f}  ({rsi_level})" if rsi_val else "  RSI(14):   n/a")
        macd_hist = (macd_l - macd_s) if (macd_l and macd_s) else None
        print(f"  MACD:      {macd_l:.4f}  Signal: {macd_s:.4f}  Histogram: {macd_hist:+.4f}" if macd_l else "  MACD:      n/a")
        print(f"")
        print(f"  ── Volatility / Volume ────────────────────────────")
        if atr_val and buy_price:
            atr_tp_mult = inst.get("atr_take_profit_mult", 1.5)
            tp = buy_price + atr_tp_mult * atr_val
            sl = buy_price - atr_tp_mult * atr_val
            print(f"  ATR(14):   {atr_val:.4f}  |  TP target: {tp:.4f}  |  SL level: {sl:.4f}")
        elif atr_val:
            print(f"  ATR(14):   {atr_val:.4f}")
        else:
            print(f"  ATR(14):   n/a")
        if vol_ratio:
            vol_str = f"  ⚡ SPIKE {vol_ratio:.1f}×" if vol_ratio >= inst.get("volume_spike_mult", 2.0) else f"  ({vol_ratio:.1f}× avg — normal)"
            print(f"  Volume:    {vol_str}")
        print(f"  Bars:      {total_rows}  |  Session bars: {len(session_ohlcv['closes']) if session_ohlcv else 0}")

    # Run both engines
    c_sig, c_reasons, c_score, rsi_v, macd_l, macd_s, vwap_v, atr_v, vol_v = \
        engine_confluence(ohlcv, session_ohlcv, inst, pct)
    m_sig, m_reason, m_macd_l, m_macd_s = engine_macd_only(closes, inst)

    def conf_label(score):
        if score >= 5: return "HIGH"
        if score >= 4: return "MEDIUM"
        return "LOW"

    telegram_results = []

    # Confluence engine
    if c_sig and (force or not in_cooldown(con, iid, "confluence", c_sig, cooldown)):
        reason_str = " + ".join(c_reasons)
        ef_curr, _, es_curr, *_ = compute_ema_ribbon(closes, inst.get("ema_fast_bars", 5),
                                                     inst.get("ema_mid_bars", 8), ema_slow_n)
        save_signal(con, iid, "confluence", c_sig, price, reason_str,
                    ef_curr, es_curr, rsi_v, macd_l, macd_s, pct, conf_label(c_score))
        telegram_results.append({
            "engine": "confluence", "signal_type": c_sig,
            "reason": reason_str, "confidence": conf_label(c_score), "score": c_score,
            "rsi": rsi_v, "macd_l": macd_l, "macd_s": macd_s,
            "vwap": vwap_v, "atr": atr_v, "vol_ratio": vol_v,
        })
        if force:
            print(f"\n  ✅ CONFLUENCE: {c_sig} [{conf_label(c_score)}, score {c_score}/9]")
            for r in c_reasons:
                print(f"     • {r}")
    elif force:
        print(f"\n  ℹ️  CONFLUENCE: no signal (score={c_score}/9, need ≥3)")

    # MACD engine
    if m_sig and (force or not in_cooldown(con, iid, "macd", m_sig, cooldown)):
        ef_curr, _, es_curr, *_ = compute_ema_ribbon(closes, inst.get("ema_fast_bars", 5),
                                                     inst.get("ema_mid_bars", 8), ema_slow_n)
        save_signal(con, iid, "macd", m_sig, price, m_reason,
                    ef_curr, es_curr, rsi_v, m_macd_l, m_macd_s, pct, None)
        telegram_results.append({
            "engine": "macd", "signal_type": m_sig, "reason": m_reason,
            "macd_l": m_macd_l, "macd_s": m_macd_s,
        })
        if force:
            print(f"  ✅ MACD ENGINE: {m_sig}")
            print(f"     • {m_reason}")
    elif force:
        ml, ms, _, _ = compute_macd(closes)
        print(f"  ℹ️  MACD ENGINE: no crossover"
              + (f" (MACD={ml:.4f}, Signal={ms:.4f})" if ml else " (insufficient data)"))

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
