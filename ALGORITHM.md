# Signal Algorithm

This document describes the full pipeline that decides **when to buy or sell** a tracked instrument and **how much** to trade.

---

## Overview

Every 2 minutes during market hours, the system:

1. **Collects** the latest OHLCV bar from yfinance and stores it in SQLite.
2. **Analyses** the stored bars with two independent signal engines.
3. **Sends a Telegram message** when a signal fires, including a position sizing recommendation.
4. **Grades past signals** (GOOD / BAD / NEUTRAL) after 1 h, 4 h, and 24 h.

```
collect_price.py  →  analyze.py  →  Telegram
                                  ↓
                            track_outcomes.py
```

---

## Engine 1 — Confluence (day-trader)

The primary engine. It scores the current bar across five indicator categories and fires a BUY or SELL when the **total score ≥ 3**.

### Indicators and scores

| Category | Condition | Direction | Points |
|---|---|---|---|
| **EMA Ribbon** | EMA5 > EMA8 > EMA21 (full stack) | BUY | +2 |
| **EMA Ribbon** | EMA5 > EMA8 only (partial) | BUY | +1 |
| **EMA Ribbon** | EMA5 < EMA8 < EMA21 (full stack) | SELL | +2 |
| **EMA Ribbon** | EMA5 < EMA8 only (partial) | SELL | +1 |
| **RSI(14)** | RSI < 25 — strongly oversold | BUY | +2 |
| **RSI(14)** | RSI < 35 — oversold | BUY | +1 |
| **RSI(14)** | RSI > 75 — strongly overbought | SELL | +2 |
| **RSI(14)** | RSI > 65 — overbought | SELL | +1 |
| **MACD** | MACD line crossed **above** signal line | BUY | +1 |
| **MACD** | MACD line crossed **below** signal line | SELL | +1 |
| **VWAP** | Price above intraday VWAP | BUY | +1 |
| **VWAP** | Price below intraday VWAP | SELL | +1 |
| **VWAP crossover** | Price just crossed **above** VWAP (fresh) | BUY | +1 |
| **VWAP crossover** | Price just crossed **below** VWAP (fresh) | SELL | +1 |
| **ATR take-profit** | Price ≥ entry + 1.5 × ATR(14) | SELL | +1 |
| **Volume spike** | Volume ≥ 2× 20-bar average on a bearish bar | SELL | +1 |
| **Volume spike** | Volume ≥ 2× 20-bar average on a bullish bar | BUY | +1 |

> Volume spike only adds a point if a signal already exists in that direction (it confirms, never originates).

Maximum possible score: **9 points**.

### Confidence levels

| Score | Confidence |
|---|---|
| ≥ 5 | HIGH |
| 4 | MEDIUM |
| 3 | LOW |

### Safety rules (applied before gates)

- **SELL blocked if price < buy price** — the system will never suggest selling at a loss.
- **SELL blocked if RSI < 30 (oversold)** — selling into deeply oversold conditions is contradictory; the RSI gate suppresses the signal entirely.
- **BUY blocked if RSI > 70 (overbought)** — buying into overbought conditions is contradictory; the RSI gate suppresses the signal entirely.
- **BUY penalty −1 if EMA ribbon is fully bearish** — buying into a downtrend is penalised.
- **RSI scoring penalty** — in addition to the hard gate, an oversold RSI (< 25) also subtracts 2 points from `sell_score`; a mildly oversold RSI (25–35) subtracts 1 point. Mirrors the buy_score additions already in place.

### Hard gates (applied after scoring)

Even if the score ≥ 3, a signal can be **completely suppressed** by two hard gates:

#### Gate 1 — Multi-timeframe (MTF) filter

The 2-minute bars are resampled on-the-fly into **15-minute** and **1-hour** candles.

- **15-min trend** = EMA5 vs EMA8 on the 15-min candles.
- **1-hour trend** = last close vs previous close on the 1-hour candles.

Suppression rules:

| Signal | Suppressed if… |
|---|---|
| SELL | Both 15-min **and** 1-hour trend are **bullish** |
| BUY | Both 15-min **and** 1-hour trend are **bearish** |

> Only suppressed when *both* higher timeframes agree. One neutral or one disagreeing timeframe lets the signal through.

#### Gate 2 — Minimum volume gate

Signal suppressed if:

```
current_volume < 0.8 × 20-bar average volume
```

Signals on near-zero volume (thin/illiquid market) are considered unreliable.

### Anti-duplicate gate

An additional **freshness check** prevents the same stale signal from re-firing after the cooldown expires:

- The latest price bar timestamp is compared to the timestamp of the last signal from this engine.
- If no new bar has arrived since the last signal, the engine is skipped entirely.

### Cooldown

Once a signal fires, the engine will not fire **the same signal type again** for the instrument for **60 minutes** (configurable per instrument).

---

## Engine 2 — MACD-only (fast crossover)

A simpler, faster engine that runs in parallel with Confluence. It looks only at MACD line crossovers:

| Condition | Signal |
|---|---|
| MACD line crossed **above** signal line | BUY |
| MACD line crossed **below** signal line | SELL |

- Parameters: fast EMA = 12 bars, slow EMA = 26 bars, signal = 9 bars.
- No scoring — it fires on a crossover alone.
- **RSI context filter** — BUY crossovers are skipped when RSI > 70 (overbought); SELL crossovers are skipped when RSI < 30 (oversold). Prevents the fast engine from firing into RSI extremes where crossovers are usually noise.
- **Cross-engine whiplash gate** — before the MACD engine fires, it checks whether the Confluence engine recently fired the *opposite* signal within the cooldown window. If so, the MACD signal is suppressed to avoid contradictory alerts.
- Subject to the same cooldown, freshness check, and anti-duplicate rules as Confluence.
- Confidence is always treated as LOW (score = 3) for position sizing purposes.

---

## Position Sizing

When either engine fires, the system computes a **suggested trade size** (EUR and shares).

### For BUY signals

```
available_eur   = max_position_eur − (shares_held × current_price)
base_eur        = confidence_fraction × available_eur

confidence_fraction:
  HIGH   (score ≥ 5)  →  75%
  MEDIUM (score = 4)  →  50%
  LOW    (score = 3)  →  25%
```

**ATR risk cap** — the size is further reduced if buying `base_eur` worth of shares would risk more than `risk_per_trade_pct` (default 2%) of `total_invested` in a single ATR move:

```
risk_budget_eur = risk_per_trade_pct% × total_invested
max_by_risk     = risk_budget_eur / (ATR / price)

if max_by_risk < base_eur:
    base_eur = max_by_risk   ← ATR cap applied
```

**Transaction fee deduction** — the 1 € fee is subtracted from the investable amount:

```
total_cost       = base_eur              ← what you spend in total (cash out)
net_investable   = total_cost − 1.00€   ← what actually buys shares
suggested_shares = net_investable / price
```

### For SELL signals

```
sell_fraction:
  HIGH   → 100% of holdings
  MEDIUM →  50% of holdings
  LOW    →  33% of holdings

gross_proceeds = sell_shares × price
net_proceeds   = gross_proceeds − 1.00€ fee   ← what you receive after fee
```

### Position sizing config keys (per instrument in `instruments.json`)

| Key | Default | Meaning |
|---|---|---|
| `max_position_eur` | 300 | Maximum total EUR to hold in this instrument |
| `risk_per_trade_pct` | 2.0 | Max % of `total_invested` to risk per ATR move |
| `transaction_fee_eur` | 1.0 | Broker fee per trade |

---

## Indicator Parameters (per instrument in `instruments.json`)

| Key | Default | Used by |
|---|---|---|
| `ema_fast_bars` | 5 | EMA ribbon |
| `ema_mid_bars` | 8 | EMA ribbon |
| `ema_slow_bars` | 21 | EMA ribbon |
| `atr_period_bars` | 14 | ATR, take-profit level, risk cap |
| `atr_take_profit_mult` | 1.5 | Take-profit = entry + mult × ATR |
| `volume_lookback_bars` | 20 | Volume ratio baseline |
| `volume_spike_mult` | 2.0 | Volume confirmation threshold |
| `volume_min_mult` | 0.8 | Minimum volume gate |
| `mtf_15m_bars` | 8 | 2-min bars per 15-min candle |
| `mtf_1h_bars` | 30 | 2-min bars per 1-hour candle |
| `alert_cooldown_minutes` | 60 | Cooldown between same-direction signals |
| `stop_loss_pct` | 5.0 | Stop-loss threshold: alert fires when unrealised P/L < −N% |
| `stop_loss_cooldown_minutes` | 60 | Cooldown between repeat stop-loss alerts |

---

## Stop-loss Monitor

The stop-loss monitor runs on **every analysis cycle**, independently of the two signal engines. It is a pure risk-management check and does not produce BUY/SELL signals.

### Logic

```
pl_pct = (current_price − avg_buy_price) / avg_buy_price × 100

if pl_pct < −stop_loss_pct:
    fire 🚨 STOP-LOSS ALERT via Telegram
```

### Cooldown and recovery

| State | Behaviour |
|---|---|
| P/L below threshold, cooldown not active | Alert fires; cooldown timestamp written to `meta` table |
| P/L below threshold, within cooldown window | Alert suppressed; logged at WARNING level |
| P/L recovers above threshold | Cooldown key cleared from `meta`; next breach will alert again |

### Guard conditions (check skipped entirely)

- `shares_held = 0` — no open position
- `buy_price` not set — no cost basis recorded
- `stop_loss_pct` not configured in `instruments.json`

### ADX calculation note

ADX smoothing uses a **mean-init** Wilder average for the DX series (correct formula: `ADX[i] = (ADX[i-1] × (p−1) + DX[i]) / p`), ensuring the output is always in the 0–100 range. The TR/DM smoothing correctly uses the standard Wilder sum-init.

---

## Signal Quality Assessment

After a signal fires, `track_outcomes.py` grades it by comparing the signal price to prices at fixed future intervals:

| Checkpoint | GOOD if | BAD if |
|---|---|---|
| 1 hour later | BUY → price rose; SELL → price fell | Opposite |
| 4 hours later | BUY → price rose; SELL → price fell | Opposite |
| 24 hours later | BUY → price rose; SELL → price fell | Opposite |

Grades are stored in the `outcomes` table alongside each signal and can be reviewed with:

```sql
SELECT s.ts, s.instrument_id, s.signal_type, s.engine, s.confidence,
       s.price, o.outcome
FROM signals s
LEFT JOIN outcomes o ON o.signal_id = s.id
ORDER BY s.ts DESC;
```

---

## Flow Diagram

```
2-min bar arrives
        │
        ▼
 fetch_ohlcv() ──── last N bars from DB
        │
        ├──► Stop-loss monitor (check_stop_loss) ─────────────────────┐
        │      ├── shares_held > 0 and buy_price set?                  │
        │      ├── pl_pct < −stop_loss_pct? ─No──► clear cooldown     │
        │      ├── Within cooldown window? ─Yes──► suppress           │
        │      └── Alert fires → Telegram 🚨                          │
        │                                                              │
        ├──► Engine 1: Confluence ──────────────────────────────────┐  │
        │      │                                                     │  │
        │      ├── Score indicators (EMA + RSI + MACD + VWAP + ATR + Volume)
        │      ├── Apply RSI penalty (oversold → −pts from sell_score)│ │
        │      ├── Apply safety rules (no loss SELL, EMA penalty)   │  │
        │      ├── Score ≥ 3? ─No──► skip                          │  │
        │      ├── RSI gate (oversold→block SELL, overbought→block BUY)│ │
        │      ├── MTF gate ──blocked?──► suppress                  │  │
        │      ├── Volume gate ──blocked?──► suppress               │  │
        │      ├── Freshness check ──stale?──► skip                 │  │
        │      └── Cooldown active? ──yes──► skip                   │  │
        │                                                            │  │
        ├──► Engine 2: MACD-only ─────────────────────────────────┐ │  │
        │      ├── MACD crossover? ─No──► skip                    │ │  │
        │      ├── RSI filter (overbought BUY / oversold SELL → skip)│ │ │
        │      ├── Cross-engine gate (opposite confluence signal?) │ │  │
        │      ├── Freshness check                                 │ │  │
        │      └── Cooldown active?                                │ │  │
        │                                                          │ │  │
        ▼                                                          ▼ ▼  ▼
  compute_position_size()  ←──────────── signal + confidence + ATR + inst
        │
        ├── BUY:  available_eur × fraction − fee → shares
        └── SELL: holdings × fraction  → gross − fee → net
        │
        ▼
  save_signal()  →  SQLite
        │
        ▼
  send_telegram()  →  Telegram message with price, P/L, suggested trade
```
