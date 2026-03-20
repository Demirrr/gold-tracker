#!/usr/bin/env bash
set -euo pipefail

# Step 1 — Export data to CSV
python scripts/export_csv.py --interval 1h

# Step 2 — Run AI analysis
copilot --model gpt-4.1 --prompt \
"Analyse the exported CSVs in data/exports/all_1h/ for the two tracked instruments:
  • sgbs-as — WisdomTree Physical Swiss Gold
  • itky-as — iShares MSCI Turkey UCITS ETF

Position state (buy_price, shares_held, total_invested, stop_loss_pct) is in scripts/instruments.json.

For each instrument, work through these steps in order:

1. TREND
   Derive EMA5 and EMA21 from 1h closes in prices.csv.
   State: bullish (EMA5 > EMA21), bearish (EMA5 < EMA21), or mixed.
   Note whether the latest close is above or below the session VWAP.

2. MOMENTUM
   Compute RSI(14) from 1h closes.
   Flag overbought (RSI > 70) or oversold (RSI < 30) conditions.

3. POSITION & STOP-LOSS
   From instruments.json, read buy_price and shares_held.
   Calculate unrealised P/L % = (current_price - buy_price) / buy_price × 100.
   If P/L % < -stop_loss_pct, flag a stop-loss breach.
   If shares_held = 0, mark as no open position.

4. RECENT SIGNALS
   From signals.csv, show the last 3 signals (ts, signal_type, engine, confidence, reason).
   Note any pattern (e.g. repeated SELL, contradicting engines) or anomaly.

5. OUTCOME HIT RATE
   From outcomes.csv joined to signals.csv, compute GOOD/BAD/NEUTRAL counts per engine
   (confluence, macd) for this instrument.
   State which engine has the higher recent hit rate.

6. RECOMMENDATION
   Synthesise the above into a single BUY / SELL / HOLD verdict with one-sentence reasoning.
   Respect safety rules:
     - Do not recommend SELL if current price < buy_price (avoid realising a loss).
     - Do not recommend BUY if RSI > 70 (overbought).
     - Do not recommend SELL if RSI < 30 (oversold).
     - Prefer the engine with the higher hit rate when signals conflict.

Format the output as two clear sections, one per instrument, using the structure:
  [INSTRUMENT NAME]
  Trend: ...  |  RSI: ...  |  VWAP: above/below
  P/L: ...%  |  Stop-loss breach: yes/no
  Last signals: ...
  Hit rates — confluence: X% | macd: Y%
  → RECOMMENDATION: BUY / SELL / HOLD — <one-sentence reason>"
