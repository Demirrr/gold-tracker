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


def compute_adx(highs, lows, closes, period=14):
    """
    Average Directional Index (ADX) with +DI and -DI — Wilder smoothing.
    Returns (adx, plus_di, minus_di) or (None, None, None) if insufficient data.
    ADX > 25 → trending market.  ADX < 20 → ranging/choppy market.
    """
    if len(closes) < period * 2 + 1:
        return None, None, None
    trs, plus_dms, minus_dms = [], [], []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i]  - closes[i - 1]))
        up   = highs[i]  - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dms.append(up   if up > down and up > 0   else 0.0)
        minus_dms.append(down if down > up and down > 0 else 0.0)
        trs.append(tr)
    # Wilder smoothing (period-bar initial average, then rolling)
    def _wilder(series, p):
        result = [sum(series[:p])]
        for v in series[p:]:
            result.append(result[-1] - result[-1] / p + v)
        return result
    atr_s   = _wilder(trs,       period)
    pdm_s   = _wilder(plus_dms,  period)
    mdm_s   = _wilder(minus_dms, period)
    pdi = [100 * p / a if a > 0 else 0.0 for p, a in zip(pdm_s, atr_s)]
    mdi = [100 * m / a if a > 0 else 0.0 for m, a in zip(mdm_s, atr_s)]
    dx  = [abs(p - m) / (p + m) * 100 if (p + m) > 0 else 0.0 for p, m in zip(pdi, mdi)]
    adx_s = _wilder(dx, period)
    return adx_s[-1], pdi[-1], mdi[-1]


def compute_rsi_series(closes, period=14):
    """Full RSI series (needed for divergence detection)."""
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    result = []
    for i in range(period - 1, len(gains)):
        ag = sum(gains[i - period + 1:i + 1]) / period
        al = sum(losses[i - period + 1:i + 1]) / period
        result.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return result


def compute_rsi_divergence(closes, lookback=20):
    """
    Detect RSI divergence over the most recent `lookback` bars.

    Bearish divergence: price made a HIGHER high but RSI made a LOWER high
                        at those swing peaks  →  likely reversal down  (+2 SELL)
    Bullish divergence: price made a LOWER low  but RSI made a HIGHER low
                        at those swing troughs →  likely reversal up   (+2 BUY)

    A swing high is a bar where close > both immediate neighbours.
    A swing low  is a bar where close < both immediate neighbours.

    Returns ("bearish", strength) | ("bullish", strength) | (None, 0).
    strength = 1 (weak) or 2 (strong, price Δ > 0.5%).
    """
    if len(closes) < lookback + 14 + 2:
        return None, 0
    window   = closes[-(lookback + 14 + 2):]
    rsi_full = compute_rsi_series(window)
    # Align: rsi_full[i] corresponds to window[i + 14]
    offset = 14
    price_window = window[offset:]   # same length as rsi_full
    if len(price_window) < lookback or len(rsi_full) < lookback:
        return None, 0
    price_w = price_window[-lookback:]
    rsi_w   = rsi_full[-lookback:]
    # Find swing highs and swing lows (index within the lookback window)
    swing_highs = [i for i in range(1, len(price_w) - 1)
                   if price_w[i] > price_w[i - 1] and price_w[i] > price_w[i + 1]]
    swing_lows  = [i for i in range(1, len(price_w) - 1)
                   if price_w[i] < price_w[i - 1] and price_w[i] < price_w[i + 1]]
    # Need at least 2 swing highs/lows to compare
    if len(swing_highs) >= 2:
        i1, i2 = swing_highs[-2], swing_highs[-1]
        p_delta = (price_w[i2] - price_w[i1]) / price_w[i1]
        r_delta =  rsi_w[i2]   -  rsi_w[i1]
        if p_delta > 0 and r_delta < 0:          # price higher high, RSI lower high
            strength = 2 if abs(p_delta) > 0.005 else 1
            return "bearish", strength
    if len(swing_lows) >= 2:
        i1, i2 = swing_lows[-2], swing_lows[-1]
        p_delta = (price_w[i2] - price_w[i1]) / price_w[i1]
        r_delta =  rsi_w[i2]   -  rsi_w[i1]
        if p_delta < 0 and r_delta > 0:          # price lower low, RSI higher low
            strength = 2 if abs(p_delta) > 0.005 else 1
            return "bullish", strength
    return None, 0


def resample_ohlcv(ohlcv, bars_per_candle):
    """
    Resample 2-min OHLCV bars into higher-timeframe candles.
    bars_per_candle: 8 → ~16-min, 30 → 60-min.
    Includes partial last candle so the most recent price is always represented.
    """
    n = len(ohlcv["closes"])
    if n < 2:
        return {"opens": [], "highs": [], "lows": [], "closes": [], "volumes": []}
    result = {"opens": [], "highs": [], "lows": [], "closes": [], "volumes": []}
    for start in range(0, n, bars_per_candle):
        end = min(start + bars_per_candle, n)
        result["opens"].append(ohlcv["opens"][start])
        result["highs"].append(max(ohlcv["highs"][start:end]))
        result["lows"].append(min(ohlcv["lows"][start:end]))
        result["closes"].append(ohlcv["closes"][end - 1])
        result["volumes"].append(sum(ohlcv["volumes"][start:end]))
    return result


def get_mtf_trend(ohlcv, bars_15m=8, bars_1h=30):
    """
    Compute higher-timeframe trends from stored 2-min bars.

    - 15-min: EMA(5) vs EMA(8) on ~16-min candles (needs 9 candles = 72 bars)
    - 1-hour: last 60-min close vs previous close (needs 2 candles = 60 bars)

    Returns dict with trend_15m, trend_1h ('bullish'/'bearish'/'neutral'),
    and candle counts.
    """
    tf15 = resample_ohlcv(ohlcv, bars_15m)
    tf1h = resample_ohlcv(ohlcv, bars_1h)

    trend_15m = "neutral"
    if len(tf15["closes"]) >= 9:
        e5 = ema(tf15["closes"], 5)[-1]
        e8 = ema(tf15["closes"], 8)[-1]
        trend_15m = "bullish" if e5 > e8 else "bearish"

    trend_1h = "neutral"
    if len(tf1h["closes"]) >= 2:
        trend_1h = "bullish" if tf1h["closes"][-1] >= tf1h["closes"][-2] else "bearish"

    return {
        "trend_15m":    trend_15m,
        "trend_1h":     trend_1h,
        "candles_15m":  len(tf15["closes"]),
        "candles_1h":   len(tf1h["closes"]),
    }


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


def last_any_signal_ts(con, instrument_id, engine):
    """Return the most recent signal timestamp for any signal type from this engine."""
    row = con.execute(
        "SELECT ts FROM signals WHERE instrument_id=? AND engine=? "
        "ORDER BY ts DESC LIMIT 1",
        (instrument_id, engine),
    ).fetchone()
    return row[0] if row else None


def has_new_data_since(con, instrument_id, engine):
    """
    True if the latest price bar is newer than the last signal from this engine.
    Prevents re-firing the same stale crossover after cooldown expires.
    If no previous signal exists, always returns True.
    """
    last_sig = last_any_signal_ts(con, instrument_id, engine)
    if not last_sig:
        return True
    latest_bar = con.execute(
        "SELECT ts FROM prices WHERE instrument_id=? ORDER BY ts DESC LIMIT 1",
        (instrument_id,),
    ).fetchone()
    if not latest_bar:
        return False
    return latest_bar[0] > last_sig


def in_cooldown(con, instrument_id, engine, signal_type, cooldown_minutes):
    last = last_signal_ts(con, instrument_id, engine, signal_type)
    if not last:
        return False
    last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - last_dt < timedelta(minutes=cooldown_minutes)


def save_signal(con, instrument_id, engine, signal_type, price, reason,
                ma_short, ma_long, rsi, macd_line, macd_sig, pct, confidence,
                suggested_eur=None, suggested_shares=None):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute(
        """INSERT INTO signals
           (instrument_id, ts, signal_type, engine, price, reason,
            ma_short, ma_long, rsi, macd_line, macd_signal_line, pct_from_buy, confidence,
            suggested_eur, suggested_shares)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (instrument_id, ts, signal_type, engine, price, reason,
         ma_short, ma_long, rsi, macd_line, macd_sig, pct, confidence,
         suggested_eur, suggested_shares),
    )
    con.commit()
    logging.info("[%s][%s] Signal: %s price=%.4f pct=%s conf=%s size=%.2f€",
                 instrument_id, engine, signal_type, price,
                 f"{pct:.2f}%" if pct is not None else "n/a", confidence,
                 suggested_eur or 0)


# ── Position sizing ───────────────────────────────────────────────────────────

def compute_kelly_fraction(con, instrument_id, engine=None, min_trades=30):
    """
    Compute half-Kelly position fraction from graded signal outcomes.

    When `engine` is supplied (e.g. "confluence" or "macd") only that engine's
    outcomes are used, giving independent fractions that reflect each engine's
    actual win-rate and payoff ratio.

    Requires at least `min_trades` graded outcomes.  Returns a fraction in
    [0.05, 0.75] or None if insufficient data.

    Kelly formula:  f = W - (1 - W) / (avg_win / avg_loss)
    We use half-Kelly (f/2) to reduce volatility from parameter estimation error.
    """
    try:
        if engine:
            rows = con.execute(
                """
                SELECT s.signal_type, s.price, o.price_24h
                FROM signals s
                JOIN outcomes o ON o.signal_id = s.id
                WHERE s.instrument_id = ?
                  AND s.engine = ?
                  AND o.price_24h IS NOT NULL
                  AND s.price > 0
                ORDER BY s.ts DESC
                LIMIT 200
                """,
                (instrument_id, engine),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT s.signal_type, s.price, o.price_24h
                FROM signals s
                JOIN outcomes o ON o.signal_id = s.id
                WHERE s.instrument_id = ?
                  AND o.price_24h IS NOT NULL
                  AND s.price > 0
                ORDER BY s.ts DESC
                LIMIT 200
                """,
                (instrument_id,),
            ).fetchall()
    except Exception:
        return None

    if len(rows) < min_trades:
        return None

    wins, losses = [], []
    for sig_type, entry_price, exit_price in rows:
        pct_return = (exit_price - entry_price) / entry_price
        if sig_type == "SELL":
            pct_return = -pct_return  # SELL profits when price falls
        if pct_return > 0:
            wins.append(pct_return)
        else:
            losses.append(abs(pct_return))

    if not wins or not losses:
        return None

    win_rate = len(wins) / len(rows)
    avg_win  = sum(wins)  / len(wins)
    avg_loss = sum(losses) / len(losses)

    if avg_loss == 0:
        return None

    kelly = win_rate - (1 - win_rate) / (avg_win / avg_loss)
    half_kelly = kelly / 2.0
    # Clamp to sensible range
    return round(min(0.75, max(0.05, half_kelly)), 4)


def compute_position_size(signal, confidence_score, price, atr, inst, con=None, engine=None):
    """
    ATR-adjusted fractional position sizing with transaction fee deduction.
    When ≥30 graded outcomes are available for the given engine, applies
    half-Kelly sizing instead of the fixed confidence tiers.  Passing
    engine="confluence" or engine="macd" uses that engine's own win-rate
    history; passing engine=None (or omitting it) pools all outcomes.

    BUY:
      1. Base fraction from Kelly (if ≥30 trades) or confidence tiers
         (LOW=25%, MEDIUM=50%, HIGH=75%) of max_position_eur.
      2. ATR risk cap — reduce if (shares × ATR) would exceed risk_per_trade_pct of total_invested.
      3. Already-held value is subtracted from max_position_eur so we never over-allocate.
      4. Transaction fee is deducted from the investable amount before computing shares.
         suggested_eur = total cash to spend (shares + fee).
         suggested_shares = (suggested_eur - fee) / price.

    SELL:
      Suggest selling a fraction of current holdings (LOW=33%, MEDIUM=50%, HIGH=100%).
      suggested_eur = gross proceeds from the sale MINUS the fee (net in pocket).

    Returns (suggested_eur, suggested_shares, rationale_str).
    """
    max_pos_eur   = float(inst.get("max_position_eur",    inst.get("total_invested", 100) * 3))
    risk_pct      = float(inst.get("risk_per_trade_pct",  2.0)) / 100.0
    total_inv     = float(inst.get("total_invested",      100.0))
    shares_held   = float(inst.get("shares_held",         0.0))
    fee           = float(inst.get("transaction_fee_eur", 1.0))

    if price <= 0:
        return 0.0, 0.0, "price unavailable"

    # Try Kelly Criterion first (requires sufficient trade history per engine)
    kelly_frac = None
    sizing_method = "confidence"
    if con is not None:
        kelly_frac = compute_kelly_fraction(con, inst.get("id", ""), engine=engine)

    # Confidence → base fraction (fallback or when Kelly unavailable)
    if confidence_score >= 5:
        frac, conf_label = 0.75, "HIGH"
    elif confidence_score >= 4:
        frac, conf_label = 0.50, "MEDIUM"
    else:
        frac, conf_label = 0.25, "LOW"

    if kelly_frac is not None:
        frac = kelly_frac
        engine_tag = f":{engine}" if engine else ""
        sizing_method = f"Kelly{engine_tag}({kelly_frac:.2%})"

    if signal == "BUY":
        # How much room is left before hitting max_position_eur?
        current_value  = shares_held * price
        available_eur  = max(0.0, max_pos_eur - current_value)
        base_eur       = frac * available_eur

        method_str = f"{sizing_method} → {frac*100:.1f}% of {available_eur:.2f}€ available"
        if kelly_frac is None:
            method_str = f"{conf_label} conf → {frac*100:.0f}% of {available_eur:.2f}€ available"
        rationale_parts = [method_str]

        if atr and atr > 0:
            # ATR risk cap: if price drops 1 ATR after buying, loss ≤ risk budget
            risk_budget_eur = risk_pct * total_inv           # e.g. 2% of 101€ = 2.02€
            risk_fraction   = atr / price                    # fraction of price per ATR move
            max_eur_by_risk = risk_budget_eur / risk_fraction if risk_fraction > 0 else base_eur
            if max_eur_by_risk < base_eur:
                rationale_parts.append(
                    f"ATR-capped from {base_eur:.2f}€ → {max_eur_by_risk:.2f}€ "
                    f"(risk {risk_budget_eur:.2f}€ / ATR {atr:.3f})"
                )
                base_eur = max_eur_by_risk

        total_cost       = round(max(0.0, base_eur), 2)          # total cash out (shares + fee)
        net_investable   = max(0.0, total_cost - fee)             # cash going into shares
        suggested_shares = round(net_investable / price, 6) if price > 0 else 0.0
        rationale        = ", ".join(rationale_parts)
        return total_cost, suggested_shares, rationale

    elif signal == "SELL":
        if shares_held <= 0:
            return 0.0, 0.0, "no shares held"
        sell_fracs  = {0.25: 0.33, 0.50: 0.50, 0.75: 1.00}
        sell_frac   = sell_fracs.get(frac, frac)
        if kelly_frac is not None:
            # For sells, Kelly fraction maps linearly to fraction of holdings to sell
            sell_frac = min(1.0, kelly_frac * 2)
        sell_shares = round(shares_held * sell_frac, 6)
        gross_eur   = round(sell_shares * price, 2)
        net_eur     = round(max(0.0, gross_eur - fee), 2)         # proceeds after fee
        sizing_label = sizing_method if kelly_frac else conf_label
        rationale   = (f"{sizing_label} → sell {sell_frac*100:.0f}% of {shares_held:.6f} shares "
                       f"(gross {gross_eur:.2f}€ − {fee:.2f}€ fee = {net_eur:.2f}€ net)")
        return net_eur, sell_shares, rationale

    return 0.0, 0.0, "unknown signal"


# ── Signal engines ────────────────────────────────────────────────────────────

def engine_confluence(ohlcv, session_ohlcv, inst, pct, mtf=None):
    """
    Day-trader confluence engine.

    Scoring:
      EMA ribbon fully aligned     +2  (SELL or BUY)
      EMA fast/mid cross only      +1
      RSI overbought/oversold      +1 or +2
      MACD crossover               +1
      VWAP side (above/below)      +1
      VWAP crossover (fresh)       +1
      ATR take-profit target hit   +1
      Volume spike confirmation    +1  (only if same direction already > 0)
      RSI divergence               +1 or +2
      ADX trend bonus              +1  (EMA/MACD if ADX > 25; suppressed if ADX < 20)

    Fires when score >= 3.

    Hard gates (suppress signal entirely):
      - MTF gate: SELL suppressed if both 15-min AND 1-hour trend are bullish.
                  BUY  suppressed if both 15-min AND 1-hour trend are bearish.
      - Volume gate: signal suppressed if volume < volume_min_mult (default 0.8×).

    Safety: SELL suppressed if price < buy_price.
            BUY penalty -1 if EMA fully bearish.

    Returns 10-tuple: (signal, reasons, score, rsi, macd_l, macd_s, vwap, atr, vol_ratio, adx)
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
        return None, [], 0, None, None, None, None, None, None, None

    rsi_val = compute_rsi(closes)
    macd_l, macd_s, macd_l_prev, macd_s_prev = compute_macd(closes)
    atr_val   = compute_atr(highs, lows, closes, atr_period)
    vol_ratio = compute_volume_ratio(volumes, vol_lookback)
    adx_val, plus_di, minus_di = compute_adx(highs, lows, closes)
    div_dir, div_strength = compute_rsi_divergence(closes)

    price          = closes[-1]
    is_bearish_bar = price < opens[-1]
    is_bullish_bar = price >= opens[-1]

    # ADX regime flags
    is_trending = adx_val is not None and adx_val > 25
    is_ranging  = adx_val is not None and adx_val < 20

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

    # ── ADX regime adjustment ─────────────────────────────────────────────────
    # In ranging markets (ADX < 20), trend-following signals (EMA/MACD) are unreliable.
    # In trending markets (ADX > 25), they get a bonus.
    if is_ranging and (sell_score > 0 or buy_score > 0):
        # Strip trend-following scores (EMA-based, MACD) — keep RSI/VWAP mean-reversion
        # We achieve this by reducing EMA contribution by 1 in ranging market
        if sell_score > 0:
            sell_score = max(0, sell_score - 1)
            sell_reasons.append(f"⚠️  ADX {adx_val:.1f} — ranging market, EMA trend score reduced by 1")
        if buy_score > 0:
            buy_score = max(0, buy_score - 1)
            buy_reasons.append(f"⚠️  ADX {adx_val:.1f} — ranging market, EMA trend score reduced by 1")
    elif is_trending:
        # Trending market: EMA ribbon signals are more reliable — add bonus if already pointing
        if (fully_bearish or fast_bearish) and sell_score > 0:
            sell_score += 1
            sell_reasons.append(f"📈 ADX {adx_val:.1f} — strong trend confirms bearish EMA alignment")
        if (fully_bullish or fast_bullish) and buy_score > 0:
            buy_score += 1
            buy_reasons.append(f"📈 ADX {adx_val:.1f} — strong trend confirms bullish EMA alignment")

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

    # ── RSI Divergence ────────────────────────────────────────────────────────
    if div_dir == "bearish":
        sell_score += div_strength
        sell_reasons.append(f"RSI bearish divergence — price higher high, RSI lower high (strength {div_strength})")
    elif div_dir == "bullish":
        buy_score += div_strength
        buy_reasons.append(f"RSI bullish divergence — price lower low, RSI higher low (strength {div_strength})")

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

    # Determine candidate signal before applying gates
    if sell_score >= 3 and sell_score >= buy_score:
        candidate_sig     = "SELL"
        candidate_reasons = sell_reasons
        candidate_score   = sell_score
    elif buy_score >= 3:
        candidate_sig     = "BUY"
        candidate_reasons = buy_reasons
        candidate_score   = buy_score
    else:
        return None, [], max(sell_score, buy_score), rsi_val, macd_l, macd_s, vwap, atr_val, vol_ratio, adx_val

    # ── Hard gate 1: MTF alignment ────────────────────────────────────────────
    # Suppress if signal trades against BOTH higher timeframes (worst false signals)
    if mtf is not None:
        t15 = mtf.get("trend_15m", "neutral")
        t1h = mtf.get("trend_1h",  "neutral")
        if candidate_sig == "SELL" and t15 == "bullish" and t1h == "bullish":
            logging.info("SELL suppressed by MTF gate: both 15m and 1h bullish")
            return None, [f"⛔ SELL suppressed: counter-trend (15m {t15}, 1h {t1h})"], candidate_score, \
                   rsi_val, macd_l, macd_s, vwap, atr_val, vol_ratio, adx_val
        if candidate_sig == "BUY" and t15 == "bearish" and t1h == "bearish":
            logging.info("BUY suppressed by MTF gate: both 15m and 1h bearish")
            return None, [f"⛔ BUY suppressed: counter-trend (15m {t15}, 1h {t1h})"], candidate_score, \
                   rsi_val, macd_l, macd_s, vwap, atr_val, vol_ratio, adx_val

    # ── Hard gate 2: minimum volume ───────────────────────────────────────────
    # Signals on near-zero volume are unreliable (thin market, no conviction)
    vol_min = inst.get("volume_min_mult", 0.8)
    if vol_ratio is not None and vol_ratio < vol_min:
        logging.info("%s suppressed by volume gate: %.2f× < %.2f× minimum", candidate_sig, vol_ratio, vol_min)
        return None, [f"⛔ {candidate_sig} suppressed: low volume ({vol_ratio:.2f}× avg < {vol_min}× minimum)"], \
               candidate_score, rsi_val, macd_l, macd_s, vwap, atr_val, vol_ratio, adx_val

    return candidate_sig, candidate_reasons, candidate_score, rsi_val, macd_l, macd_s, vwap, atr_val, vol_ratio, adx_val


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

def send_telegram_text(text):
    """Send a plain text Telegram message (used for alerts such as stale data warnings)."""
    if not TELEGRAM_BOT_TOKEN:
        logging.warning("TELEGRAM_BOT_TOKEN not set, skipping alert")
        return
    try:
        env = os.environ.copy()
        env["TELEGRAM_BOT_TOKEN"] = TELEGRAM_BOT_TOKEN
        subprocess.run(["bash", str(TELEGRAM_SCRIPT), text], env=env, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        logging.error("Telegram alert failed: %s", exc.stderr)


def send_telegram(inst, price, pct, results):
    if not TELEGRAM_BOT_TOKEN:
        logging.warning("TELEGRAM_BOT_TOKEN not set, skipping")
        return

    name      = inst["name"]
    buy_price = inst.get("buy_price")
    shares    = inst.get("shares_held", 0)
    currency  = inst.get("currency", "EUR")
    pct_sign  = "+" if (pct or 0) >= 0 else ""
    paper     = inst.get("paper_trading", False)

    header = f"📊 {'[PAPER] ' if paper else ''}SIGNAL REPORT — {name}"
    lines = [header, "─" * 36]

    for r in results:
        engine_label = "CONFLUENCE" if r["engine"] == "confluence" else "MACD ENGINE"
        sig   = r["signal_type"]
        emoji = "🔴" if sig == "SELL" else "🟢"
        if r["engine"] == "confluence":
            conf_str = f" [{r['confidence']} confidence, score {r.get('score','')}/9]"
        else:
            conf_str = ""
        paper_tag = " ⚠️ PAPER — do not execute" if paper else ""
        lines.append(f"{emoji} {engine_label}: {sig}{conf_str}{paper_tag}")
        lines.append(f"   {r['reason']}")
        if r.get("rsi") is not None:
            lines.append(f"   RSI: {r['rsi']:.1f}  |  MACD: {r.get('macd_l', 0):.4f}  Signal: {r.get('macd_s', 0):.4f}")
        if r.get("vwap") is not None:
            vwap_pct = (price - r["vwap"]) / r["vwap"] * 100
            lines.append(f"   VWAP: {r['vwap']:.3f}  (price {'+' if vwap_pct >= 0 else ''}{vwap_pct:.2f}% from VWAP)")
        if r.get("atr") is not None:
            atr_tp_mult = inst.get("atr_take_profit_mult", 1.5)
            sl_price = price - atr_tp_mult * r["atr"]
            tp_price = price + atr_tp_mult * r["atr"]
            if sig == "BUY":
                lines.append(f"   ATR: {r['atr']:.3f}  |  🎯 TP: {tp_price:.3f}  |  🛑 SL: {sl_price:.3f}  (±{atr_tp_mult}× ATR)")
            else:
                lines.append(f"   ATR: {r['atr']:.3f}")
        if r.get("adx") is not None:
            adx = r["adx"]
            regime = "TRENDING" if adx > 25 else ("RANGING" if adx < 20 else "NEUTRAL")
            lines.append(f"   ADX: {adx:.1f} — {regime}")
        if r.get("vol_ratio") is not None:
            lines.append(f"   Volume: {r['vol_ratio']:.1f}× avg")
        if r.get("mtf"):
            t15 = r["mtf"].get("trend_15m", "neutral")
            t1h = r["mtf"].get("trend_1h",  "neutral")
            lines.append(f"   MTF:   15m {t15.upper()}  |  1h {t1h.upper()}")
        # Position sizing recommendation
        sug_eur    = r.get("suggested_eur")
        sug_shares = r.get("suggested_shares")
        sug_reason = r.get("sizing_rationale", "")
        fee        = float(inst.get("transaction_fee_eur", 1.0))
        if sug_eur is not None and sug_eur > 0:
            if sig == "BUY":
                net_investable = max(0.0, sug_eur - fee)
                lines.append(
                    f"   💶 Suggested BUY:  {sug_eur:.2f}€ total  "
                    f"({net_investable:.2f}€ into shares + {fee:.2f}€ fee)"
                )
                lines.append(f"      → ~{sug_shares:.6f} shares @ {price:.4f}")
            else:
                gross_eur = round(sug_shares * price, 2)
                lines.append(
                    f"   💶 Suggested SELL: {sug_shares:.6f} shares  "
                    f"(gross {gross_eur:.2f}€ − {fee:.2f}€ fee = {sug_eur:.2f}€ net)"
                )
            if sug_reason:
                lines.append(f"      ({sug_reason})")
        elif sug_eur == 0:
            lines.append(f"   💶 Suggested: no action ({sug_reason})")
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

# Track which instruments have already received a staleness alert this run
# (avoids spamming Telegram every 2 minutes if data is stuck)
_STALE_ALERT_COOLDOWN_MIN = 30
_COLLECTOR_HEARTBEAT_GRACE_MIN = 10


def _meta_value(con, key):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _collector_heartbeat_age_minutes(con, instrument_id, now_utc):
    last_fetch = _meta_value(con, f"collector_heartbeat:{instrument_id}")
    if not last_fetch:
        return None
    return (now_utc - datetime.fromisoformat(last_fetch.replace("Z", "+00:00"))).total_seconds() / 60


def check_staleness(last_ts_str, inst):
    """Return a warning string; also fire a one-shot Telegram alert during market hours."""
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
        iid = inst["id"]
        con = sqlite3.connect(DB_PATH)
        msg = (f"  ⚠️  Last bar is {age_min:.0f} min old ({last_ts_str}) — "
               f"data may be stale (yfinance lags ~15–20 min for low-volume ETFs)\n")
        meta_key = f"stale_alert:{iid}"
        try:
            heartbeat_age_min = _collector_heartbeat_age_minutes(con, iid, now_utc)
            if heartbeat_age_min is not None and heartbeat_age_min <= _COLLECTOR_HEARTBEAT_GRACE_MIN:
                logging.info(
                    "[%s] Latest bar is %d min old but collector heartbeat is fresh (%.1f min) — suppressing stale alert",
                    iid,
                    int(age_min),
                    heartbeat_age_min,
                )
                return (
                    f"  ℹ️  Last bar is {age_min:.0f} min old ({last_ts_str}), "
                    f"but collector fetched successfully {heartbeat_age_min:.0f} min ago\n"
                )
            last_sent_raw = _meta_value(con, meta_key)
            last_sent = datetime.fromisoformat(last_sent_raw) if last_sent_raw else None
            if last_sent is None or (now_utc - last_sent).total_seconds() / 60 >= _STALE_ALERT_COOLDOWN_MIN:
                alert = (
                    f"⚠️ STALE DATA — {inst['name']} ({iid})\n"
                    f"Last price bar is {age_min:.0f} min old ({last_ts_str}).\n"
                    f"Collector heartbeat is missing or outdated — check collector.log / cron.log."
                )
                logging.warning("[%s] Stale data alert fired (%d min old)", iid, int(age_min))
                send_telegram_text(alert)
                con.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (meta_key, now_utc.isoformat()),
                )
                con.commit()
        finally:
            con.close()
        return msg
    # Reset the alert cooldown once data is fresh again
    iid = inst["id"]
    meta_key = f"stale_alert:{iid}"
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("DELETE FROM meta WHERE key=?", (meta_key,))
        con.commit()
    finally:
        con.close()
    if not market_open:
        return f"  ℹ️  Market closed. Last bar: {last_ts_str} ({age_min:.0f} min ago)\n"
    return ""


# ── Main per-instrument analysis ──────────────────────────────────────────────

def analyse_instrument(inst, con, force=False):
    iid         = inst["id"]
    buy_price   = inst.get("buy_price")
    cooldown    = inst["alert_cooldown_minutes"]
    paper       = inst.get("paper_trading", False)
    ema_slow_n  = inst.get("ema_slow_bars", 21)
    bars_15m    = inst.get("mtf_15m_bars", 8)
    bars_1h     = inst.get("mtf_1h_bars", 30)
    # Minimum bars: need ≥36 for MACD(12,26,9).
    # MTF gracefully returns "neutral" when not enough candles — don't inflate min_bars for it.
    min_bars    = max(ema_slow_n + 1, 36)

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

    # Multi-timeframe trend (uses the full ohlcv window, not just session)
    mtf = get_mtf_trend(ohlcv, bars_15m, bars_1h)

    if force:
        print(f"\n{'═' * 46}")
        print(f"  Instrument: {inst['name']} ({iid})" + ("  [PAPER TRADING]" if paper else ""))
        print(check_staleness(last_ts_str, inst), end="")
    else:
        # Always run staleness check (sends Telegram alert if data is stale during market hours)
        check_staleness(last_ts_str, inst)

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
        print(f"")
        print(f"  ── Multi-timeframe & Regime ────────────────────────")
        t15 = mtf["trend_15m"]
        t1h = mtf["trend_1h"]
        c15 = mtf["candles_15m"]
        c1h = mtf["candles_1h"]
        t15_icon = "📈" if t15 == "bullish" else ("📉" if t15 == "bearish" else "➡️")
        t1h_icon = "📈" if t1h == "bullish" else ("📉" if t1h == "bearish" else "➡️")
        print(f"  15-min:    {t15_icon} {t15.upper():8}  ({c15} candles, EMA5 vs EMA8)")
        print(f"  1-hour:    {t1h_icon} {t1h.upper():8}  ({c1h} candles, last close vs prev)")
        adx_disp, _, _ = compute_adx(ohlcv["highs"], ohlcv["lows"], closes)
        if adx_disp is not None:
            regime = "TRENDING 📊" if adx_disp > 25 else ("RANGING ↔️" if adx_disp < 20 else "NEUTRAL ➡️")
            print(f"  ADX(14):   {adx_disp:.1f}  ({regime})")
        div_d, div_s = compute_rsi_divergence(closes)
        if div_d:
            div_icon = "📉" if div_d == "bearish" else "📈"
            print(f"  RSI Div:   {div_icon} {div_d.upper()} divergence (strength {div_s})")
        vol_min = inst.get("volume_min_mult", 0.8)
        if vol_ratio is not None and vol_ratio < vol_min:
            print(f"  ⚠️  Volume gate: {vol_ratio:.2f}× < {vol_min}× minimum — signals would be suppressed")
        print(f"")
        print(f"  ── Position sizing ─────────────────────────────────")
        for eng in ("confluence", "macd"):
            kf = compute_kelly_fraction(con, iid, engine=eng)
            n_outcomes = con.execute(
                """SELECT COUNT(*) FROM signals s JOIN outcomes o ON o.signal_id = s.id
                   WHERE s.instrument_id = ? AND s.engine = ? AND o.price_24h IS NOT NULL""",
                (iid, eng),
            ).fetchone()[0]
            if kf is not None:
                print(f"  Kelly ({eng:<11}): {kf:.2%}  ({n_outcomes} graded outcomes)")
            else:
                need = 30 - n_outcomes
                print(f"  Kelly ({eng:<11}): not yet ({n_outcomes}/30 graded outcomes, need {need} more)")

    # Run both engines
    c_sig, c_reasons, c_score, rsi_v, macd_l, macd_s, vwap_v, atr_v, vol_v, adx_v = \
        engine_confluence(ohlcv, session_ohlcv, inst, pct, mtf=mtf)
    m_sig, m_reason, m_macd_l, m_macd_s = engine_macd_only(closes, inst)

    def conf_label(score):
        if score >= 5: return "HIGH"
        if score >= 4: return "MEDIUM"
        return "LOW"

    telegram_results = []

    # Confluence engine — skip entirely if no new price data since last signal
    confluence_has_new = force or has_new_data_since(con, iid, "confluence")
    if c_sig and confluence_has_new and (force or not in_cooldown(con, iid, "confluence", c_sig, cooldown)):
        reason_str = " + ".join(c_reasons)
        if paper:
            reason_str = "[PAPER] " + reason_str
        ef_curr, _, es_curr, *_ = compute_ema_ribbon(closes, inst.get("ema_fast_bars", 5),
                                                     inst.get("ema_mid_bars", 8), ema_slow_n)
        sug_eur, sug_shares, sug_reason = compute_position_size(c_sig, c_score, price, atr_v, inst, con, engine="confluence")
        save_signal(con, iid, "confluence", c_sig, price, reason_str,
                    ef_curr, es_curr, rsi_v, macd_l, macd_s, pct, conf_label(c_score),
                    sug_eur, sug_shares)
        telegram_results.append({
            "engine": "confluence", "signal_type": c_sig,
            "reason": reason_str, "confidence": conf_label(c_score), "score": c_score,
            "rsi": rsi_v, "macd_l": macd_l, "macd_s": macd_s,
            "vwap": vwap_v, "atr": atr_v, "vol_ratio": vol_v, "mtf": mtf, "adx": adx_v,
            "suggested_eur": sug_eur, "suggested_shares": sug_shares, "sizing_rationale": sug_reason,
        })
        if force:
            print(f"\n  ✅ CONFLUENCE: {c_sig} [{conf_label(c_score)}, score {c_score}/9]")
            for r in c_reasons:
                print(f"     • {r}")
            if c_sig == "BUY" and atr_v:
                atr_tp_mult = inst.get("atr_take_profit_mult", 1.5)
                print(f"     🎯 TP: {price + atr_tp_mult * atr_v:.4f}  |  🛑 SL: {price - atr_tp_mult * atr_v:.4f}  (±{atr_tp_mult}× ATR)")
            fee = inst.get("transaction_fee_eur", 1.0)
            if c_sig == "BUY":
                net_inv = max(0.0, sug_eur - fee)
                print(f"     💶 Suggested BUY:  {sug_eur:.2f}€ total  ({net_inv:.2f}€ into shares + {fee:.2f}€ fee)")
                print(f"        → ~{sug_shares:.6f} shares @ {price:.4f}")
            else:
                gross = round(sug_shares * price, 2)
                print(f"     💶 Suggested SELL: {sug_shares:.6f} shares  (gross {gross:.2f}€ − {fee:.2f}€ fee = {sug_eur:.2f}€ net)")
            print(f"        ({sug_reason})")
    elif force:
        if not confluence_has_new:
            print(f"\n  ℹ️  CONFLUENCE: skipped — no new price data since last signal")
        elif c_score >= 3 and c_reasons:
            # Signal scored but was suppressed by a gate
            print(f"\n  ⛔ CONFLUENCE: signal suppressed  (score was {c_score}/9)")
            print(f"     {c_reasons[0]}")
        else:
            print(f"\n  ℹ️  CONFLUENCE: no signal (score={c_score}/9, need ≥3)")

    # MACD engine — skip entirely if no new price data since last signal
    macd_has_new = force or has_new_data_since(con, iid, "macd")
    if m_sig and macd_has_new and (force or not in_cooldown(con, iid, "macd", m_sig, cooldown)):
        ef_curr, _, es_curr, *_ = compute_ema_ribbon(closes, inst.get("ema_fast_bars", 5),
                                                     inst.get("ema_mid_bars", 8), ema_slow_n)
        # MACD-only engine doesn't have a confluence score; use a default score of 3 (LOW)
        sug_eur, sug_shares, sug_reason = compute_position_size(m_sig, 3, price, None, inst, con, engine="macd")
        macd_reason_str = ("[PAPER] " + m_reason) if paper else m_reason
        save_signal(con, iid, "macd", m_sig, price, macd_reason_str,
                    ef_curr, es_curr, rsi_v, m_macd_l, m_macd_s, pct, None,
                    sug_eur, sug_shares)
        telegram_results.append({
            "engine": "macd", "signal_type": m_sig, "reason": m_reason,
            "macd_l": m_macd_l, "macd_s": m_macd_s,
            "suggested_eur": sug_eur, "suggested_shares": sug_shares, "sizing_rationale": sug_reason,
        })
        if force:
            print(f"  ✅ MACD ENGINE: {m_sig}")
            print(f"     • {m_reason}")
            if m_sig == "BUY" and atr_v:
                atr_tp_mult = inst.get("atr_take_profit_mult", 1.5)
                print(f"     🎯 TP: {price + atr_tp_mult * atr_v:.4f}  |  🛑 SL: {price - atr_tp_mult * atr_v:.4f}  (±{atr_tp_mult}× ATR)")
            fee = inst.get("transaction_fee_eur", 1.0)
            if m_sig == "BUY":
                net_inv = max(0.0, sug_eur - fee)
                print(f"     💶 Suggested BUY:  {sug_eur:.2f}€ total  ({net_inv:.2f}€ into shares + {fee:.2f}€ fee)")
                print(f"        → ~{sug_shares:.6f} shares @ {price:.4f}")
            else:
                gross = round(sug_shares * price, 2)
                print(f"     💶 Suggested SELL: {sug_shares:.6f} shares  (gross {gross:.2f}€ − {fee:.2f}€ fee = {sug_eur:.2f}€ net)")
            print(f"        ({sug_reason})")
    elif force:
        if not macd_has_new:
            print(f"  ℹ️  MACD ENGINE: skipped — no new price data since last signal")
        else:
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
