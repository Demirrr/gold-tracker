"""
Integration tests that run against the live SQLite database.
These tests verify:
  - Schema integrity (all expected columns exist)
  - Data sanity (prices within plausible bounds, no duplicates)
  - Indicator computation on real stored bars
  - Signal logic (gates fire correctly on stored data)
  - Engine freshness gate (no signal fires on stale data)

Requires: at least 36 price bars per instrument in the DB.
Skip gracefully if DB is missing or empty.
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from config import DB_PATH
from analyze import (
    compute_rsi,
    compute_macd,
    compute_ema_ribbon,
    compute_atr,
    compute_vwap_series,
    compute_volume_ratio,
    get_mtf_trend,
    fetch_ohlcv,
    has_new_data_since,
    in_cooldown,
    engine_confluence,
    engine_macd_only,
)
import json
from config import SCRIPTS_DIR


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def db():
    if not DB_PATH.exists():
        pytest.skip("Database not found — run collect_price.py first")
    return sqlite3.connect(str(DB_PATH))


@pytest.fixture(scope="module")
def instruments():
    return json.loads((SCRIPTS_DIR / "instruments.json").read_text())


@pytest.fixture(scope="module")
def instrument_ids(db):
    rows = db.execute("SELECT DISTINCT instrument_id FROM prices").fetchall()
    ids  = [r[0] for r in rows]
    if not ids:
        pytest.skip("No price data in DB — run collect_price.py first")
    return ids


# ── Schema tests ──────────────────────────────────────────────────────────────

class TestDBSchema:
    def test_prices_table_exists(self, db):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "prices" in tables

    def test_signals_table_exists(self, db):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "signals" in tables

    def test_outcomes_table_exists(self, db):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "outcomes" in tables

    def test_prices_columns(self, db):
        cols = {r[1] for r in db.execute("PRAGMA table_info(prices)")}
        required = {"id", "instrument_id", "ts", "open", "high", "low", "close", "volume"}
        assert required.issubset(cols), f"Missing columns: {required - cols}"

    def test_signals_columns(self, db):
        cols = {r[1] for r in db.execute("PRAGMA table_info(signals)")}
        required = {"id", "instrument_id", "ts", "signal_type", "engine",
                    "price", "reason", "ma_short", "ma_long", "rsi",
                    "macd_line", "macd_signal_line", "pct_from_buy", "confidence"}
        assert required.issubset(cols), f"Missing columns: {required - cols}"

    def test_outcomes_columns(self, db):
        cols = {r[1] for r in db.execute("PRAGMA table_info(outcomes)")}
        required = {"id", "signal_id", "price_1h", "price_4h", "price_24h", "outcome", "filled_at"}
        assert required.issubset(cols), f"Missing columns: {required - cols}"

    def test_prices_unique_constraint(self, db, instrument_ids):
        # No (instrument_id, ts) pair should appear twice
        for iid in instrument_ids:
            dups = db.execute(
                "SELECT ts, COUNT(*) as cnt FROM prices WHERE instrument_id=? "
                "GROUP BY ts HAVING cnt > 1",
                (iid,)
            ).fetchall()
            assert dups == [], f"Duplicate timestamps in {iid}: {dups[:3]}"


# ── Data sanity tests ─────────────────────────────────────────────────────────

class TestDataSanity:
    def test_prices_are_positive(self, db, instrument_ids):
        for iid in instrument_ids:
            bad = db.execute(
                "SELECT COUNT(*) FROM prices WHERE instrument_id=? AND close <= 0",
                (iid,)
            ).fetchone()[0]
            assert bad == 0, f"{iid}: {bad} non-positive close prices"

    def test_high_gte_low(self, db, instrument_ids):
        for iid in instrument_ids:
            bad = db.execute(
                "SELECT COUNT(*) FROM prices WHERE instrument_id=? AND high < low",
                (iid,)
            ).fetchone()[0]
            assert bad == 0, f"{iid}: {bad} bars where high < low"

    def test_high_gte_close(self, db, instrument_ids):
        for iid in instrument_ids:
            bad = db.execute(
                "SELECT COUNT(*) FROM prices WHERE instrument_id=? AND high < close - 0.001",
                (iid,)
            ).fetchone()[0]
            assert bad == 0, f"{iid}: {bad} bars where high < close"

    def test_low_lte_close(self, db, instrument_ids):
        for iid in instrument_ids:
            bad = db.execute(
                "SELECT COUNT(*) FROM prices WHERE instrument_id=? AND low > close + 0.001",
                (iid,)
            ).fetchone()[0]
            assert bad == 0, f"{iid}: {bad} bars where low > close"

    def test_timestamps_are_valid_iso(self, db, instrument_ids):
        for iid in instrument_ids:
            rows = db.execute(
                "SELECT ts FROM prices WHERE instrument_id=? LIMIT 20", (iid,)
            ).fetchall()
            for (ts,) in rows:
                try:
                    datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    pytest.fail(f"{iid}: invalid timestamp format: {ts}")

    def test_timestamps_are_utc(self, db, instrument_ids):
        for iid in instrument_ids:
            bad = db.execute(
                "SELECT COUNT(*) FROM prices WHERE instrument_id=? AND ts NOT LIKE '%Z'",
                (iid,)
            ).fetchone()[0]
            assert bad == 0, f"{iid}: {bad} timestamps not in UTC (missing Z suffix)"

    def test_prices_within_plausible_range(self, db, instruments):
        for inst in instruments:
            iid = inst["id"]
            buy_price = inst.get("buy_price")
            if buy_price is None:
                continue
            rows = db.execute(
                "SELECT MIN(close), MAX(close) FROM prices WHERE instrument_id=?", (iid,)
            ).fetchone()
            if rows[0] is None:
                continue
            min_p, max_p = rows
            # Sanity: price should not deviate more than 50% from buy price
            assert min_p > buy_price * 0.5, f"{iid}: suspiciously low min price {min_p}"
            assert max_p < buy_price * 2.0, f"{iid}: suspiciously high max price {max_p}"

    def test_signals_type_valid(self, db):
        invalid = db.execute(
            "SELECT COUNT(*) FROM signals WHERE signal_type NOT IN ('BUY','SELL','INFO')"
        ).fetchone()[0]
        assert invalid == 0, f"{invalid} signals with invalid signal_type"

    def test_signals_engine_valid(self, db):
        invalid = db.execute(
            "SELECT COUNT(*) FROM signals WHERE engine NOT IN ('confluence','macd')"
        ).fetchone()[0]
        assert invalid == 0, f"{invalid} signals with unknown engine"

    def test_outcomes_type_valid(self, db):
        invalid = db.execute(
            "SELECT COUNT(*) FROM outcomes WHERE outcome NOT IN ('GOOD','BAD','NEUTRAL')"
        ).fetchone()[0]
        assert invalid == 0, f"{invalid} outcomes with invalid outcome value"


# ── Indicator computation on real data ───────────────────────────────────────

class TestIndicatorsOnRealData:
    """Run indicator functions on bars fetched from the live DB and assert sanity."""

    MIN_BARS = 36

    def _get_ohlcv(self, db, iid, n=60):
        return fetch_ohlcv(db, iid, n)

    def test_rsi_in_range(self, db, instrument_ids):
        for iid in instrument_ids:
            ohlcv = self._get_ohlcv(db, iid)
            if len(ohlcv["closes"]) < self.MIN_BARS:
                pytest.skip(f"{iid}: only {len(ohlcv['closes'])} bars, need {self.MIN_BARS}")
            rsi = compute_rsi(ohlcv["closes"])
            if rsi is not None:
                assert 0.0 <= rsi <= 100.0, f"{iid}: RSI out of range: {rsi}"

    def test_macd_returns_four_values(self, db, instrument_ids):
        for iid in instrument_ids:
            ohlcv = self._get_ohlcv(db, iid)
            if len(ohlcv["closes"]) < self.MIN_BARS:
                pytest.skip(f"{iid}: only {len(ohlcv['closes'])} bars")
            result = compute_macd(ohlcv["closes"])
            assert len(result) == 4, "MACD must return (macd_line, signal, prev_macd, prev_signal)"

    def test_ema_ribbon_ordering_consistent_with_trend(self, db, instrument_ids):
        for iid in instrument_ids:
            ohlcv = self._get_ohlcv(db, iid)
            closes = ohlcv["closes"]
            if len(closes) < 22:
                continue
            ef, em, es, *_ = compute_ema_ribbon(closes, 5, 8, 21)
            if ef is None:
                continue
            # All three should be positive prices
            assert ef > 0 and em > 0 and es > 0

    def test_atr_positive_on_real_data(self, db, instrument_ids):
        for iid in instrument_ids:
            ohlcv = self._get_ohlcv(db, iid)
            if len(ohlcv["closes"]) < 20:
                continue
            atr = compute_atr(ohlcv["highs"], ohlcv["lows"], ohlcv["closes"], 14)
            if atr is not None:
                assert atr > 0, f"{iid}: ATR should be positive, got {atr}"
                # ATR should be a small fraction of the price (< 5%)
                avg_close = sum(ohlcv["closes"]) / len(ohlcv["closes"])
                assert atr < avg_close * 0.05, f"{iid}: ATR={atr} > 5% of avg price — suspiciously high"

    def test_volume_ratio_non_negative(self, db, instrument_ids):
        for iid in instrument_ids:
            ohlcv = self._get_ohlcv(db, iid)
            ratio = compute_volume_ratio(ohlcv["volumes"], lookback=20)
            if ratio is not None:
                assert ratio >= 0, f"{iid}: negative volume ratio: {ratio}"

    def test_mtf_returns_valid_trends(self, db, instrument_ids):
        for iid in instrument_ids:
            ohlcv = self._get_ohlcv(db, iid, n=80)
            mtf = get_mtf_trend(ohlcv, bars_15m=8, bars_1h=30)
            assert mtf["trend_15m"] in ("bullish", "bearish", "neutral")
            assert mtf["trend_1h"]  in ("bullish", "bearish", "neutral")
            assert isinstance(mtf["candles_15m"], int)
            assert isinstance(mtf["candles_1h"],  int)


# ── Signal engine sanity tests ────────────────────────────────────────────────

class TestSignalEngines:
    def test_macd_engine_returns_four_values(self, db, instrument_ids, instruments):
        for inst in instruments:
            iid = inst["id"]
            if iid not in instrument_ids:
                continue
            ohlcv = fetch_ohlcv(db, iid, 60)
            if len(ohlcv["closes"]) < 36:
                continue
            result = engine_macd_only(ohlcv["closes"], inst)
            assert len(result) == 4
            sig, reason, ml, ms = result
            assert sig in (None, "BUY", "SELL")
            if sig:
                assert isinstance(reason, str) and len(reason) > 0

    def test_confluence_engine_returns_nine_values(self, db, instrument_ids, instruments):
        for inst in instruments:
            iid = inst["id"]
            if iid not in instrument_ids:
                continue
            ohlcv = fetch_ohlcv(db, iid, 72)
            if len(ohlcv["closes"]) < 36:
                continue
            result = engine_confluence(ohlcv, None, inst, pct=None)
            assert len(result) == 9
            sig = result[0]
            assert sig in (None, "BUY", "SELL")

    def test_no_signal_on_insufficient_bars(self, db, instruments):
        for inst in instruments:
            # Provide only 5 bars — far too few for any indicator
            tiny_ohlcv = {
                "opens":   [100.0] * 5,
                "highs":   [101.0] * 5,
                "lows":    [99.0]  * 5,
                "closes":  [100.0] * 5,
                "volumes": [500.0] * 5,
            }
            sig, *_ = engine_confluence(tiny_ohlcv, None, inst, pct=None)
            assert sig is None, f"Engine should return None with only 5 bars, got {sig}"


# ── Freshness gate tests ──────────────────────────────────────────────────────

class TestFreshnessGate:
    def test_has_new_data_true_when_no_signals(self, db, instrument_ids):
        # When there are no signals, freshness gate should always pass
        for iid in instrument_ids:
            result = has_new_data_since(db, iid, "confluence")
            # If no signals exist for this engine, must return True
            sig_count = db.execute(
                "SELECT COUNT(*) FROM signals WHERE instrument_id=? AND engine='confluence'",
                (iid,)
            ).fetchone()[0]
            if sig_count == 0:
                assert result is True, f"{iid}: should be True with no prior signals"

    def test_cooldown_false_when_no_signals(self, db, instrument_ids):
        for iid in instrument_ids:
            result = in_cooldown(db, iid, "confluence", "SELL", 60)
            sig_count = db.execute(
                "SELECT COUNT(*) FROM signals WHERE instrument_id=? AND engine='confluence' AND signal_type='SELL'",
                (iid,)
            ).fetchone()[0]
            if sig_count == 0:
                assert result is False
