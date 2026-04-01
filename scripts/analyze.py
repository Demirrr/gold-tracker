#!/usr/bin/env python3
"""
Report EMA crossovers (all configured pairs) confirmed by RSI and volume.

A Telegram report is sent when any pair produces a crossover.  The message
shows the state of all pairs plus RSI(14) and current volume ratio.

Usage:
  python analyze.py                        # all instruments, respects cooldown
  python analyze.py --instrument sgbs-as
  python analyze.py --force / -f           # bypass cooldown, fetch fresh data
  python analyze.py --force --chart        # also send EMA ribbon chart image
"""
import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
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
    """RSI(period) — returns latest value or None if insufficient data."""
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


def compute_volume_ratio(volumes, lookback=20):
    """Ratio of current bar volume to recent average."""
    if len(volumes) < lookback + 1:
        return None
    avg = sum(volumes[-lookback - 1:-1]) / lookback
    return volumes[-1] / avg if avg > 0 else None


def ema_pair_state(closes, short_n, long_n):
    """
    Current and previous values for a short/long EMA pair.
    Returns (short_now, long_now, short_prev, long_prev) or (None,)*4.
    """
    if len(closes) < long_n + 1:
        return None, None, None, None
    short_series = ema(closes, short_n)
    long_series  = ema(closes, long_n)
    return short_series[-1], long_series[-1], short_series[-2], long_series[-2]


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


def last_signal_ts(con, instrument_id, engine, signal_type):
    row = con.execute(
        "SELECT ts FROM signals WHERE instrument_id=? AND engine=? AND signal_type=? "
        "ORDER BY ts DESC LIMIT 1",
        (instrument_id, engine, signal_type),
    ).fetchone()
    return row[0] if row else None


def has_new_data_since(con, instrument_id, engine):
    """True if the latest price bar is newer than the last signal from this engine."""
    row = con.execute(
        "SELECT ts FROM signals WHERE instrument_id=? AND engine=? ORDER BY ts DESC LIMIT 1",
        (instrument_id, engine),
    ).fetchone()
    last_sig = row[0] if row else None
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


def save_signal(con, instrument_id, signal_type, price, reason, ema_short, ema_long, rsi):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute(
        """INSERT INTO signals
           (instrument_id, ts, signal_type, engine, price, reason,
            ma_short, ma_long, rsi, macd_line, macd_signal_line,
            pct_from_buy, confidence, suggested_eur, suggested_shares)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (instrument_id, ts, signal_type, "ema", price, reason,
         ema_short, ema_long, rsi, None, None, None, None, None, None),
    )
    con.commit()
    logging.info("[%s][ema] %s  price=%.4f  %s", instrument_id, signal_type, price, reason)


# ── EMA pair evaluation ───────────────────────────────────────────────────────

def _ema_periods(inst):
    """Return sorted, deduplicated list of EMA periods for this instrument."""
    default = [
        inst.get("ema_fast_bars", 5),
        inst.get("ema_mid_bars",  8),
        inst.get("ema_slow_bars", 21),
    ]
    return sorted(set(inst.get("ema_periods", default)))


def evaluate_ema_pairs(closes, inst):
    """
    Evaluate consecutive EMA pairs for crossovers and current direction.

    Periods come from the 'ema_periods' config key (e.g. [5, 8, 21, 34, 50]).
    Consecutive pairs are formed: (5,8), (8,21), (21,34), (34,50).

    Returns a list of dicts — one per pair:
      { pair, short_n, long_n, signal, short_val, long_val, direction }
    signal is 'BUY' (golden cross), 'SELL' (death cross), or None.
    """
    periods = _ema_periods(inst)
    results = []
    for short_n, long_n in zip(periods, periods[1:]):
        sv, lv, sv_p, lv_p = ema_pair_state(closes, short_n, long_n)
        if sv is None:
            results.append({
                "pair": f"EMA{short_n}/EMA{long_n}",
                "short_n": short_n, "long_n": long_n,
                "signal": None, "short_val": None, "long_val": None,
                "direction": "neutral",
            })
            continue

        signal = None
        if sv_p <= lv_p and sv > lv:
            signal = "BUY"
        elif sv_p >= lv_p and sv < lv:
            signal = "SELL"

        direction = "bullish" if sv > lv else ("bearish" if sv < lv else "neutral")
        results.append({
            "pair": f"EMA{short_n}/EMA{long_n}",
            "short_n": short_n, "long_n": long_n,
            "signal": signal, "short_val": sv, "long_val": lv,
            "direction": direction,
        })
    return results


def ribbon_summary(pair_results):
    """
    Summarise overall EMA ribbon alignment across all computed pairs.
    Returns a short human-readable string.
    """
    available = [r for r in pair_results if r["short_val"] is not None]
    if not available:
        return "n/a"
    n      = len(available)
    n_bull = sum(1 for r in available if r["direction"] == "bullish")
    n_bear = sum(1 for r in available if r["direction"] == "bearish")
    if n_bull == n:
        return f"FULLY BULLISH  ({n}/{n} pairs aligned up) ✅"
    if n_bear == n:
        return f"FULLY BEARISH  ({n}/{n} pairs aligned down) ✅"
    return f"MIXED  ({n_bull}/{n} bullish, {n_bear}/{n} bearish)"


# ── Telegram ──────────────────────────────────────────────────────────────────

def _ema_guide(periods):
    """Return a short educational legend for the EMA periods in use."""
    # Approximate meaning by position in the sorted list
    labels = {
        0: ("fastest", "mirrors every price tick; first to catch a new move"),
        1: ("fast",    "short-term momentum — filters the smallest noise"),
        2: ("medium",  "standard short-term trend reference"),
        3: ("slower",  "broader trend view; far less whipsaw"),
        4: ("slowest", "medium-term anchor; often acts as dynamic support/resistance"),
    }
    lines = ["", "━━ EMA Guide ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for i, p in enumerate(sorted(set(periods))):
        role, desc = labels.get(i, ("extra", "additional trend reference"))
        lines.append(f"  EMA {p:<3} [{role}]  {desc}")
    lines += [
        "",
        "  Golden cross: short EMA rises above long EMA → bullish momentum",
        "  Death  cross: short EMA falls below long EMA → bearish momentum",
        "  All pairs aligned in the same direction → strongest signal.",
    ]
    return lines


# ── Chart generation ──────────────────────────────────────────────────────────

# EMA line colours in ribbon order (fast → slow)
_EMA_COLOURS = ["#f5a623", "#f8e71c", "#7ed321", "#4a90e2", "#9b59b6"]


def generate_ema_chart(inst, ohlcv, pair_results):
    """
    Render price + EMA ribbon and mark crossover points.

    Returns the path to a temporary PNG file.  The caller is responsible for
    deleting it after use.
    """
    import matplotlib
    matplotlib.use("Agg")           # headless — no display required
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    periods    = _ema_periods(inst)
    closes     = ohlcv["closes"]
    timestamps = ohlcv["timestamps"]

    # Parse timestamps
    dates = [datetime.fromisoformat(ts.replace("Z", "+00:00")) for ts in timestamps]

    # Compute one EMA series per period
    ema_series = {p: ema(closes, p) for p in periods}

    fig, (ax_price, ax_vol) = plt.subplots(
        2, 1, figsize=(12, 7),
        gridspec_kw={"height_ratios": [3, 1]},
        facecolor="#1a1a2e",
    )
    fig.subplots_adjust(hspace=0.08)

    for ax in (ax_price, ax_vol):
        ax.set_facecolor("#16213e")
        ax.tick_params(colors="#aaaaaa", labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#333355")

    # Price line
    ax_price.plot(dates, closes, color="#e0e0e0", linewidth=1.0,
                  alpha=0.85, label="Price", zorder=3)

    # EMA lines
    for i, period in enumerate(periods):
        colour = _EMA_COLOURS[i % len(_EMA_COLOURS)]
        ax_price.plot(dates, ema_series[period], color=colour,
                      linewidth=1.4, label=f"EMA {period}", zorder=4)

    # Mark crossover points
    for r in pair_results:
        if not r["signal"] or r["short_val"] is None:
            continue
        colour = "#00ff88" if r["signal"] == "BUY" else "#ff4455"
        marker = "^" if r["signal"] == "BUY" else "v"
        ax_price.scatter([dates[-1]], [r["short_val"]], color=colour,
                         marker=marker, s=90, zorder=6)

    # Volume bars
    volumes = ohlcv["volumes"]
    lookback = inst.get("volume_lookback_bars", 20)
    avg_vol  = sum(volumes[-lookback:]) / max(lookback, 1) if volumes else 1
    bar_colours = ["#3a7bd5" if v <= avg_vol * inst.get("volume_spike_mult", 2.0)
                   else "#f5a623" for v in volumes]
    ax_vol.bar(dates, volumes, color=bar_colours, width=0.001, alpha=0.7)
    if avg_vol:
        ax_vol.axhline(avg_vol, color="#888888", linewidth=0.8,
                       linestyle="--", label="Avg vol")

    # Titles and labels
    signal_tags = [r["signal"] for r in pair_results if r["signal"]]
    dominant    = "BUY" if signal_tags.count("BUY") >= signal_tags.count("SELL") else "SELL"
    title_colour = "#00ff88" if (not signal_tags or dominant == "BUY") else "#ff4455"
    ribbon_label = ribbon_summary(pair_results).replace("✅", "").replace("⚠️", "").strip()
    ax_price.set_title(
        f"{inst['name']}  |  {ribbon_label}",
        color=title_colour, fontsize=11, pad=8,
    )
    ax_price.set_ylabel("Price", color="#aaaaaa", fontsize=8)
    ax_vol.set_ylabel("Volume", color="#aaaaaa", fontsize=8)

    ax_price.xaxis.set_visible(False)
    ax_vol.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate(rotation=30, ha="right")

    legend = ax_price.legend(
        loc="upper left", fontsize=7,
        facecolor="#1a1a2e", edgecolor="#333355", labelcolor="#cccccc",
    )

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    fig.savefig(tmp.name, dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    logging.info("[%s] chart saved to %s", inst["id"], tmp.name)
    return tmp.name


def send_telegram_photo(image_path, caption):
    """Send an image to Telegram using sendPhoto multipart upload."""
    if not TELEGRAM_BOT_TOKEN:
        logging.warning("TELEGRAM_BOT_TOKEN not set, skipping photo")
        return
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        logging.warning("TELEGRAM_CHAT_ID not set, skipping photo")
        return
    try:
        result = subprocess.run(
            [
                "curl", "--silent", "--show-error",
                "-X", "POST",
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                "-F", f"chat_id={chat_id}",
                "-F", f"caption={caption}",
                "-F", f"photo=@{image_path}",
            ],
            capture_output=True, text=True, check=True,
        )
        logging.info("Telegram photo sent: %s", result.stdout[:80])
    except subprocess.CalledProcessError as exc:
        logging.error("Telegram photo failed: %s", exc.stderr)


def send_telegram(inst, price, pair_results, rsi_val, vol_ratio):
    if not TELEGRAM_BOT_TOKEN:
        logging.warning("TELEGRAM_BOT_TOKEN not set, skipping")
        return

    name      = inst["name"]
    currency  = inst.get("currency", "EUR")
    buy_price = inst.get("buy_price")
    vol_spike = inst.get("volume_spike_mult", 2.0)

    crossovers = [r for r in pair_results if r["signal"]]
    buys       = sum(1 for r in crossovers if r["signal"] == "BUY")
    sells      = sum(1 for r in crossovers if r["signal"] == "SELL")
    dominant   = "BUY" if buys >= sells else "SELL"
    hdr_emoji  = "🟢" if dominant == "BUY" else "🔴"

    lines = [f"{hdr_emoji} EMA REPORT — {name}", "─" * 42]

    # EMA pair table
    lines.append("EMA Pairs (fast → slow):")
    for r in pair_results:
        if r["short_val"] is None:
            lines.append(f"  {r['pair']:16}  n/a  (need more bars)")
            continue
        arrow = "↑" if r["direction"] == "bullish" else (
                "↓" if r["direction"] == "bearish" else "→")
        tag = ""
        if r["signal"] == "BUY":
            tag = "  ← 🟢 golden cross"
        elif r["signal"] == "SELL":
            tag = "  ← 🔴 death cross"
        lines.append(
            f"  {r['pair']:16}  {r['short_val']:.4f} / {r['long_val']:.4f}  {arrow}{tag}"
        )

    # Ribbon alignment summary
    ribbon = ribbon_summary(pair_results)
    lines += ["", f"Ribbon:  {ribbon}"]

    # RSI + Volume
    lines.append("")
    if rsi_val is not None:
        if rsi_val > 70:
            rsi_label = "overbought ⚠️"
        elif rsi_val < 30:
            rsi_label = "oversold ⚠️"
        else:
            rsi_label = "neutral"
        lines.append(f"RSI(14):  {rsi_val:.1f}  ({rsi_label})")

    if vol_ratio is not None:
        vol_label = (f"spike ⚡ {vol_ratio:.1f}x" if vol_ratio >= vol_spike
                     else f"{vol_ratio:.1f}x avg")
        lines.append(f"Volume:   {vol_label}")

    # Price
    lines.append("")
    if buy_price:
        pct      = (price / buy_price - 1) * 100
        pct_sign = "+" if pct >= 0 else ""
        lines.append(
            f"Price:  {price:.4f} {currency}  ({pct_sign}{pct:.2f}% from {buy_price:.4f})")
    else:
        lines.append(f"Price:  {price:.4f} {currency}  (watch-only)")

    lines.append(f"Time:   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")

    # Educational footer
    periods = _ema_periods(inst)
    lines += _ema_guide(periods)

    msg = "\n".join(lines)
    try:
        env = os.environ.copy()
        env["TELEGRAM_BOT_TOKEN"] = TELEGRAM_BOT_TOKEN
        subprocess.run(["bash", str(TELEGRAM_SCRIPT), msg], env=env,
                       check=True, capture_output=True)
        logging.info("[%s] Telegram sent", inst["id"])
    except subprocess.CalledProcessError as exc:
        logging.error("[%s] Telegram failed: %s", inst["id"], exc.stderr)


# ── Main per-instrument analysis ──────────────────────────────────────────────

def analyse_instrument(inst, con, force=False, chart=False):
    iid      = inst["id"]
    cooldown = inst["alert_cooldown_minutes"]
    periods  = _ema_periods(inst)
    min_bars = max(periods) + 2   # need slowest EMA series + one previous bar

    last_ts_row = con.execute(
        "SELECT ts FROM prices WHERE instrument_id=? ORDER BY ts DESC LIMIT 1", (iid,)
    ).fetchone()
    last_ts_str = last_ts_row[0] if last_ts_row else None

    ohlcv   = fetch_ohlcv(con, iid, min_bars + 1)
    closes  = ohlcv["closes"]
    volumes = ohlcv["volumes"]

    if force:
        print(f"\n{'=' * 46}")
        print(f"  Instrument: {inst['name']} ({iid})")
        print(f"  Last bar:   {last_ts_str or 'n/a'}")

    if len(closes) < min_bars:
        msg = f"  [{iid}] Not enough data ({len(closes)}/{min_bars} bars needed)"
        logging.info(msg)
        if force:
            print(msg)
        return

    price        = closes[-1]
    rsi_val      = compute_rsi(closes)
    vol_ratio    = compute_volume_ratio(volumes, inst.get("volume_lookback_bars", 20))
    pair_results = evaluate_ema_pairs(closes, inst)
    crossovers   = [r for r in pair_results if r["signal"]]

    if force:
        print(f"  Price:      {price:.4f} {inst['currency']}")
        rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "n/a"
        vol_str = f"{vol_ratio:.2f}x" if vol_ratio is not None else "n/a"
        print(f"  RSI(14):    {rsi_str}   Volume: {vol_str}")
        print()
        for r in pair_results:
            if r["short_val"] is None:
                print(f"  {r['pair']:16}  n/a")
            else:
                tag = f"  <- {r['signal']}" if r["signal"] else ""
                print(f"  {r['pair']:16}  {r['short_val']:.4f} / {r['long_val']:.4f}"
                      f"  [{r['direction'].upper()}]{tag}")

    if not crossovers:
        if force:
            print("\n  No EMA crossovers — nothing to send")
        return

    engine = "ema"
    if not force and not has_new_data_since(con, iid, engine):
        logging.info("[%s] EMA: no new data since last signal", iid)
        return

    buys     = [r for r in crossovers if r["signal"] == "BUY"]
    sells    = [r for r in crossovers if r["signal"] == "SELL"]
    dominant = "BUY" if len(buys) >= len(sells) else "SELL"

    if not force and in_cooldown(con, iid, engine, dominant, cooldown):
        logging.info("[%s] EMA signal in cooldown, skipping", iid)
        return

    reason_parts = []
    for r in crossovers:
        verb = "golden cross" if r["signal"] == "BUY" else "death cross"
        reason_parts.append(
            f"{r['pair']} {verb} ({r['short_val']:.4f} vs {r['long_val']:.4f})")
    reason_str = " | ".join(reason_parts)

    fn           = periods[0]
    sn           = periods[-1]
    sv, lv, _, _ = ema_pair_state(closes, fn, sn)
    save_signal(con, iid, dominant, price, reason_str, sv, lv, rsi_val)

    if force:
        emoji = "BUY  +" if dominant == "BUY" else "SELL -"
        print(f"\n  [{emoji}] EMA signal: {dominant}")
        for part in reason_parts:
            print(f"     * {part}")

    send_telegram(inst, price, pair_results, rsi_val, vol_ratio)

    if chart:
        caption = (
            f"{inst['name']} | {ribbon_summary(pair_results)} | "
            f"RSI {rsi_val:.1f}" if rsi_val else inst["name"]
        )
        image_path = generate_ema_chart(inst, ohlcv, pair_results)
        try:
            send_telegram_photo(image_path, caption)
            if force:
                print(f"  Chart sent: {image_path}")
        finally:
            Path(image_path).unlink(missing_ok=True)


# ── Entry point ───────────────────────────────────────────────────────────────

def analyse(instrument_id=None, force=False, chart=False):
    init_db()
    if force:
        import collect_price
        collect_price.collect(instrument_id)

    instruments = load_instruments(instrument_id)
    con = sqlite3.connect(DB_PATH)
    try:
        for inst in instruments:
            analyse_instrument(inst, con, force=force, chart=chart)
    finally:
        con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", "-i", help="Instrument ID (default: all)")
    parser.add_argument("--force", "-f", action="store_true",
                        help="Bypass cooldown, fetch fresh data, print live stats")
    parser.add_argument("--chart", "-c", action="store_true",
                        help="Generate EMA ribbon chart and send as Telegram photo")
    args = parser.parse_args()
    if args.force:
        print("=== Manual Analysis Run (cooldown bypassed) ===")
    analyse(args.instrument, args.force, args.chart)
