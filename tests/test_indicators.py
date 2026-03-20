"""
Unit tests for all technical indicator functions in analyze.py.
These tests use synthetic price series with known mathematical properties,
so they do NOT require a database or network access.
"""
import sys
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from analyze import (
    ema,
    compute_rsi,
    compute_macd,
    compute_ema_ribbon,
    compute_atr,
    compute_vwap_series,
    compute_volume_ratio,
    resample_ohlcv,
    get_mtf_trend,
    compute_position_size,
    compute_kelly_fraction,
    _collector_heartbeat_age_minutes,
    check_staleness,
    check_stop_loss,
)


# ── EMA ───────────────────────────────────────────────────────────────────────

class TestEMA:
    def test_single_value_returns_itself(self):
        assert ema([100.0], 5) == [100.0]

    def test_flat_series_stays_flat(self):
        result = ema([50.0] * 20, 5)
        assert all(abs(v - 50.0) < 1e-9 for v in result)

    def test_rising_series_ema_lags_price(self):
        # EMA of a rising series should always be below the current price
        prices = [float(i) for i in range(1, 51)]
        result = ema(prices, 10)
        # After warm-up, EMA should lag the rising price
        assert result[-1] < prices[-1]

    def test_ema_length_matches_input(self):
        prices = [100.0 + i * 0.5 for i in range(30)]
        result = ema(prices, 5)
        assert len(result) == len(prices)

    def test_ema_converges_to_constant(self):
        # 100 bars at 100.0 then 100 bars at 200.0 — EMA should approach 200
        prices = [100.0] * 100 + [200.0] * 100
        result = ema(prices, 10)
        assert abs(result[-1] - 200.0) < 0.01


# ── RSI ───────────────────────────────────────────────────────────────────────

class TestRSI:
    def test_returns_none_when_insufficient_data(self):
        assert compute_rsi([100.0] * 14) is None  # need period+1 = 15

    def test_all_gains_returns_100(self):
        # Strictly rising series → RSI = 100
        prices = [float(i) for i in range(1, 30)]
        assert compute_rsi(prices) == 100.0

    def test_all_losses_returns_zero(self):
        # Strictly falling series → avg_loss > 0, avg_gain = 0 → RSI = 0
        prices = [float(100 - i) for i in range(30)]
        assert compute_rsi(prices) == 0.0

    def test_flat_series_all_losses_zero(self):
        # Flat series → all differences are 0 → avg_gain = avg_loss = 0 → returns 100 (no loss)
        prices = [50.0] * 30
        assert compute_rsi(prices) == 100.0

    def test_rsi_within_0_100(self):
        import random
        random.seed(42)
        prices = [100.0 + random.gauss(0, 1) for _ in range(50)]
        rsi = compute_rsi(prices)
        assert rsi is not None
        assert 0.0 <= rsi <= 100.0

    def test_overbought_condition(self):
        # Strong uptrend should push RSI well above 65
        prices = [100.0 + i * 2 for i in range(50)]
        assert compute_rsi(prices) > 65

    def test_oversold_condition(self):
        # Strong downtrend should push RSI well below 35
        prices = [200.0 - i * 2 for i in range(50)]
        assert compute_rsi(prices) < 35


# ── MACD ─────────────────────────────────────────────────────────────────────

class TestMACD:
    def test_returns_none_when_insufficient_data(self):
        # Needs slow(26) + signal(9) = 35 bars
        closes = [100.0] * 34
        macd_l, sig, prev_l, prev_s = compute_macd(closes)
        assert macd_l is None

    def test_flat_series_macd_near_zero(self):
        closes = [100.0] * 50
        macd_l, sig, _, _ = compute_macd(closes)
        assert abs(macd_l) < 1e-6
        assert abs(sig) < 1e-6

    def test_bullish_crossover_detected(self):
        # Build a series where price rises sharply → MACD should cross above signal
        # Start flat then spike up
        closes = [100.0] * 40 + [110.0 + i * 0.5 for i in range(20)]
        macd_l, sig, prev_l, prev_s = compute_macd(closes)
        assert macd_l is not None
        # After a strong rise, fast EMA > slow EMA → positive MACD
        assert macd_l > 0

    def test_bearish_crossover_detected(self):
        closes = [100.0] * 40 + [90.0 - i * 0.5 for i in range(20)]
        macd_l, sig, prev_l, prev_s = compute_macd(closes)
        assert macd_l is not None
        assert macd_l < 0

    def test_returns_prev_values(self):
        closes = [100.0 + i * 0.1 for i in range(60)]
        macd_l, sig, prev_l, prev_s = compute_macd(closes)
        assert prev_l is not None
        assert prev_s is not None


# ── EMA Ribbon ────────────────────────────────────────────────────────────────

class TestEMARibbon:
    def test_returns_none_when_insufficient_data(self):
        closes = [100.0] * 20  # need slow(21) + 1 = 22
        ef, em, es, *_ = compute_ema_ribbon(closes, 5, 8, 21)
        assert ef is None

    def test_fully_bullish_uptrend(self):
        closes = [100.0 + i * 0.5 for i in range(60)]
        ef, em, es, *_ = compute_ema_ribbon(closes, 5, 8, 21)
        assert ef > em > es, f"Expected EMA5({ef:.3f}) > EMA8({em:.3f}) > EMA21({es:.3f})"

    def test_fully_bearish_downtrend(self):
        closes = [200.0 - i * 0.5 for i in range(60)]
        ef, em, es, *_ = compute_ema_ribbon(closes, 5, 8, 21)
        assert ef < em < es, f"Expected EMA5({ef:.3f}) < EMA8({em:.3f}) < EMA21({es:.3f})"

    def test_flat_ribbon_all_equal(self):
        closes = [100.0] * 60
        ef, em, es, *_ = compute_ema_ribbon(closes, 5, 8, 21)
        assert abs(ef - em) < 1e-6
        assert abs(em - es) < 1e-6

    def test_prev_values_returned(self):
        closes = [100.0 + i * 0.1 for i in range(60)]
        ef, em, es, ef_p, em_p, es_p = compute_ema_ribbon(closes, 5, 8, 21)
        assert ef_p is not None
        assert ef_p != ef  # previous should differ from current in trend


# ── ATR ───────────────────────────────────────────────────────────────────────

class TestATR:
    def _make_ohlcv(self, closes, spread=0.5):
        highs  = [c + spread for c in closes]
        lows   = [c - spread for c in closes]
        return highs, lows, closes

    def test_returns_none_when_insufficient_data(self):
        closes = [100.0] * 14  # need period+1 = 15
        highs, lows, _ = self._make_ohlcv(closes)
        assert compute_atr(highs, lows, closes, 14) is None

    def test_constant_spread_equals_spread(self):
        # When H-L is always 1.0 and no gaps, ATR should converge to 1.0
        closes = [100.0] * 50
        highs  = [101.0] * 50
        lows   = [99.0]  * 50
        atr = compute_atr(highs, lows, closes, 14)
        assert abs(atr - 2.0) < 0.01  # H-L range = 2.0

    def test_atr_positive(self):
        import random
        random.seed(0)
        closes = [100.0 + random.gauss(0, 1) for _ in range(50)]
        highs, lows, _ = self._make_ohlcv(closes, 0.5)
        atr = compute_atr(highs, lows, closes, 14)
        assert atr > 0

    def test_wider_spread_gives_higher_atr(self):
        closes = [100.0] * 50
        highs_narrow = [100.5] * 50
        lows_narrow  = [99.5]  * 50
        highs_wide   = [102.0] * 50
        lows_wide    = [98.0]  * 50
        atr_narrow = compute_atr(highs_narrow, lows_narrow, closes, 14)
        atr_wide   = compute_atr(highs_wide,   lows_wide,   closes, 14)
        assert atr_wide > atr_narrow


# ── VWAP ─────────────────────────────────────────────────────────────────────

class TestVWAP:
    def test_equal_prices_vwap_equals_price(self):
        # All bars same price → VWAP = that price
        price  = 100.0
        highs  = [price] * 10
        lows   = [price] * 10
        closes = [price] * 10
        vols   = [1000.0] * 10
        vwap   = compute_vwap_series(highs, lows, closes, vols)
        assert all(abs(v - price) < 1e-6 for v in vwap)

    def test_zero_volume_uses_close_as_fallback(self):
        highs  = [100.0]
        lows   = [100.0]
        closes = [100.0]
        vols   = [0.0]
        vwap   = compute_vwap_series(highs, lows, closes, vols)
        assert vwap[0] == 100.0

    def test_vwap_weighted_toward_high_volume_bars(self):
        # Two bars: one at 100 with tiny volume, one at 200 with huge volume
        # VWAP should be close to 200
        highs  = [100.0, 200.0]
        lows   = [100.0, 200.0]
        closes = [100.0, 200.0]
        vols   = [1.0, 10000.0]
        vwap   = compute_vwap_series(highs, lows, closes, vols)
        assert vwap[-1] > 190.0

    def test_vwap_series_length_matches_input(self):
        n = 20
        highs  = [100.0] * n
        lows   = [99.0]  * n
        closes = [99.5]  * n
        vols   = [500.0] * n
        vwap   = compute_vwap_series(highs, lows, closes, vols)
        assert len(vwap) == n

    def test_vwap_is_cumulative_not_rolling(self):
        # VWAP accumulates all day — early value differs from later
        highs  = [100.0, 110.0, 120.0]
        lows   = [100.0, 110.0, 120.0]
        closes = [100.0, 110.0, 120.0]
        vols   = [100.0, 100.0, 100.0]
        vwap   = compute_vwap_series(highs, lows, closes, vols)
        # VWAP[0]=100, VWAP[1]=(100+110)/2=105, VWAP[2]=(100+110+120)/3=110
        assert abs(vwap[0] - 100.0) < 1e-6
        assert abs(vwap[1] - 105.0) < 1e-6
        assert abs(vwap[2] - 110.0) < 1e-6


class TestStalenessHelpers:
    def test_collector_heartbeat_age_minutes_returns_none_when_missing(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        age = _collector_heartbeat_age_minutes(con, "sgbs-as", datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc))
        assert age is None

    def test_check_staleness_suppresses_alert_when_collector_heartbeat_is_fresh(self, monkeypatch):
        db_path = Path(__file__).parent / "test_staleness.db"
        if db_path.exists():
            db_path.unlink()
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("collector_heartbeat:sgbs-as", datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
        con.close()

        import analyze

        monkeypatch.setattr(analyze, "DB_PATH", db_path)
        monkeypatch.setattr(analyze, "send_telegram_text", lambda msg: (_ for _ in ()).throw(AssertionError(msg)))

        warning = check_staleness(
            (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat().replace("+00:00", "Z"),
            {
                "id": "sgbs-as",
                "name": "Test Instrument",
                "market_open_utc": "00:00",
                "market_close_utc": "23:59",
            },
        )
        assert "collector fetched successfully" in warning
        db_path.unlink()


# ── Volume ratio ──────────────────────────────────────────────────────────────

class TestVolumeRatio:
    def test_returns_none_when_insufficient_data(self):
        assert compute_volume_ratio([1000.0] * 20, lookback=20) is None

    def test_equal_volume_ratio_is_one(self):
        vols = [1000.0] * 25
        ratio = compute_volume_ratio(vols, lookback=20)
        assert abs(ratio - 1.0) < 1e-6

    def test_spike_detected(self):
        vols = [1000.0] * 24 + [5000.0]  # last bar is 5× average
        ratio = compute_volume_ratio(vols, lookback=20)
        assert ratio > 4.5

    def test_zero_avg_returns_none(self):
        vols = [0.0] * 25
        ratio = compute_volume_ratio(vols, lookback=20)
        assert ratio is None


# ── Resample / MTF ────────────────────────────────────────────────────────────

class TestResample:
    def _make_ohlcv(self, n, start=100.0, step=0.1):
        closes = [start + i * step for i in range(n)]
        return {
            "opens":   closes,
            "highs":   [c + 0.5 for c in closes],
            "lows":    [c - 0.5 for c in closes],
            "closes":  closes,
            "volumes": [500.0] * n,
        }

    def test_resampled_length(self):
        ohlcv = self._make_ohlcv(40)
        result = resample_ohlcv(ohlcv, bars_per_candle=8)
        # 40 bars / 8 = 5 candles
        assert len(result["closes"]) == 5

    def test_partial_last_candle_included(self):
        ohlcv = self._make_ohlcv(35)
        result = resample_ohlcv(ohlcv, bars_per_candle=8)
        # 35/8 = 4 full + 1 partial = 5 candles
        assert len(result["closes"]) == 5

    def test_high_is_max_of_bars(self):
        ohlcv = self._make_ohlcv(8)
        result = resample_ohlcv(ohlcv, bars_per_candle=8)
        # One candle — its high should be max of all 8 bar highs
        assert result["highs"][0] == max(ohlcv["highs"])

    def test_low_is_min_of_bars(self):
        ohlcv = self._make_ohlcv(8)
        result = resample_ohlcv(ohlcv, bars_per_candle=8)
        assert result["lows"][0] == min(ohlcv["lows"])

    def test_volume_is_sum(self):
        ohlcv = self._make_ohlcv(8)
        result = resample_ohlcv(ohlcv, bars_per_candle=8)
        assert result["volumes"][0] == sum(ohlcv["volumes"])


class TestMTFTrend:
    def _make_ohlcv(self, closes):
        return {
            "opens":   closes,
            "highs":   [c + 0.5 for c in closes],
            "lows":    [c - 0.5 for c in closes],
            "closes":  closes,
            "volumes": [500.0] * len(closes),
        }

    def test_bullish_trend_detected(self):
        closes = [100.0 + i * 0.3 for i in range(80)]
        ohlcv  = self._make_ohlcv(closes)
        mtf    = get_mtf_trend(ohlcv, bars_15m=8, bars_1h=30)
        assert mtf["trend_15m"] == "bullish"
        assert mtf["trend_1h"]  == "bullish"

    def test_bearish_trend_detected(self):
        closes = [200.0 - i * 0.3 for i in range(80)]
        ohlcv  = self._make_ohlcv(closes)
        mtf    = get_mtf_trend(ohlcv, bars_15m=8, bars_1h=30)
        assert mtf["trend_15m"] == "bearish"
        assert mtf["trend_1h"]  == "bearish"

    def test_neutral_when_insufficient_candles(self):
        closes = [100.0] * 10  # only 10 bars → 1-2 candles, not enough for EMA5 on 15m
        ohlcv  = self._make_ohlcv(closes)
        mtf    = get_mtf_trend(ohlcv, bars_15m=8, bars_1h=30)
        assert mtf["trend_15m"] == "neutral"

    def test_candle_counts_returned(self):
        closes = [100.0 + i * 0.1 for i in range(80)]
        ohlcv  = self._make_ohlcv(closes)
        mtf    = get_mtf_trend(ohlcv, bars_15m=8, bars_1h=30)
        assert mtf["candles_15m"] == 10   # 80 / 8 = 10 exact
        assert mtf["candles_1h"]  == 3    # 80 / 30 = 2 complete + 1 partial = 3


# ── Position sizing ───────────────────────────────────────────────────────────

class TestPositionSizing:
    """Tests for compute_position_size(signal, confidence_score, price, atr, inst)."""

    BASE_INST = {
        "buy_price":            426.23,
        "shares_held":          0.234609,
        "total_invested":       101.0,
        "max_position_eur":     300.0,
        "risk_per_trade_pct":   2.0,
        "transaction_fee_eur":  1.0,
    }

    def test_high_confidence_buy_larger_than_low(self):
        inst = dict(self.BASE_INST)
        eur_high, _, _ = compute_position_size("BUY", 5, 426.0, None, inst)
        eur_low,  _, _ = compute_position_size("BUY", 3, 426.0, None, inst)
        assert eur_high > eur_low, "HIGH confidence should suggest more EUR than LOW"

    def test_medium_confidence_buy_between_low_and_high(self):
        inst = dict(self.BASE_INST)
        eur_high, _, _ = compute_position_size("BUY", 5, 426.0, None, inst)
        eur_med,  _, _ = compute_position_size("BUY", 4, 426.0, None, inst)
        eur_low,  _, _ = compute_position_size("BUY", 3, 426.0, None, inst)
        assert eur_low < eur_med < eur_high

    def test_atr_cap_reduces_buy_size(self):
        inst = dict(self.BASE_INST)
        # Very large ATR (50% of price) — risk cap should kick in
        eur_no_atr,   _, _ = compute_position_size("BUY", 5, 426.0, None,  inst)
        eur_huge_atr, _, _ = compute_position_size("BUY", 5, 426.0, 213.0, inst)
        assert eur_huge_atr < eur_no_atr, "Large ATR must reduce suggested size"

    def test_small_atr_does_not_cap(self):
        inst = dict(self.BASE_INST)
        # Tiny ATR (0.1 EUR / 426 = 0.023%) — risk is negligible, cap should not fire
        eur_no_atr,   _, _ = compute_position_size("BUY", 5, 426.0, None,  inst)
        eur_small_atr,_, _ = compute_position_size("BUY", 5, 426.0, 0.1,   inst)
        assert eur_small_atr == eur_no_atr, "Tiny ATR should not reduce suggested size"

    def test_buy_does_not_exceed_available_budget(self):
        inst = dict(self.BASE_INST)
        price = 426.0
        current_value = inst["shares_held"] * price     # ~99.97 EUR
        available = inst["max_position_eur"] - current_value   # ~200 EUR
        eur, _, _ = compute_position_size("BUY", 9, price, None, inst)
        assert eur <= available + 0.01, "Suggested BUY must not exceed available budget"

    def test_buy_returns_correct_shares(self):
        # shares = (total_cost - fee) / price
        inst = dict(self.BASE_INST)
        price = 400.0
        fee   = inst["transaction_fee_eur"]
        total_cost, shares, _ = compute_position_size("BUY", 4, price, None, inst)
        expected_shares = round(max(0.0, total_cost - fee) / price, 6)
        assert shares == expected_shares

    def test_buy_eur_rounded_to_2dp(self):
        inst = dict(self.BASE_INST)
        eur, _, _ = compute_position_size("BUY", 4, 426.23, 1.23, inst)
        assert eur == round(eur, 2)

    def test_sell_high_confidence_sells_all(self):
        inst = dict(self.BASE_INST, shares_held=1.0)
        price = 430.0
        fee   = inst["transaction_fee_eur"]
        net_eur, shares, _ = compute_position_size("SELL", 5, price, None, inst)
        assert shares == 1.0, "HIGH confidence SELL should suggest selling all shares"
        # net proceeds = gross - fee
        assert net_eur == round(1.0 * price - fee, 2)

    def test_sell_low_confidence_sells_fraction(self):
        inst = dict(self.BASE_INST, shares_held=1.0)
        price = 430.0
        eur_high, shares_high, _ = compute_position_size("SELL", 5, price, None, inst)
        eur_low,  shares_low,  _ = compute_position_size("SELL", 3, price, None, inst)
        assert shares_low < shares_high, "LOW conf SELL should suggest fewer shares"

    def test_sell_zero_shares_returns_zero(self):
        inst = dict(self.BASE_INST, shares_held=0.0)
        eur, shares, rationale = compute_position_size("SELL", 5, 426.0, None, inst)
        assert eur == 0.0
        assert shares == 0.0
        assert "no shares" in rationale.lower()

    def test_buy_zero_price_returns_zero(self):
        inst = dict(self.BASE_INST)
        eur, shares, rationale = compute_position_size("BUY", 5, 0.0, None, inst)
        assert eur == 0.0
        assert shares == 0.0

    def test_already_at_max_position_returns_zero(self):
        # shares_held × price ≥ max_position_eur → available = 0
        inst = dict(self.BASE_INST, shares_held=1.0, max_position_eur=100.0)
        price = 200.0  # current value = 200 ≥ max_position_eur=100
        eur, shares, _ = compute_position_size("BUY", 5, price, None, inst)
        assert eur == 0.0, "Should not suggest buying when already at max position"

    def test_fee_deducted_from_buy_shares(self):
        """1€ fee must reduce shares acquired vs a fee-free buy."""
        inst_with_fee    = dict(self.BASE_INST, transaction_fee_eur=1.0)
        inst_without_fee = dict(self.BASE_INST, transaction_fee_eur=0.0)
        price = 426.0
        _, shares_with,    _ = compute_position_size("BUY", 4, price, None, inst_with_fee)
        _, shares_without, _ = compute_position_size("BUY", 4, price, None, inst_without_fee)
        assert shares_with < shares_without, "Fee should reduce shares acquired"

    def test_fee_deducted_from_sell_proceeds(self):
        """1€ fee must reduce net proceeds vs a fee-free sell."""
        inst_with_fee    = dict(self.BASE_INST, shares_held=1.0, transaction_fee_eur=1.0)
        inst_without_fee = dict(self.BASE_INST, shares_held=1.0, transaction_fee_eur=0.0)
        price = 430.0
        net_with,    _, _ = compute_position_size("SELL", 5, price, None, inst_with_fee)
        net_without, _, _ = compute_position_size("SELL", 5, price, None, inst_without_fee)
        assert net_with < net_without, "Fee should reduce net sell proceeds"
        assert net_without - net_with == pytest.approx(1.0)

    def test_itky_scale_sizing(self):
        """ITKY.AS is a low-priced ETF — check sizing scales correctly."""
        inst = {
            "buy_price":            15.76,
            "shares_held":          1.01437,
            "total_invested":       16.99,
            "max_position_eur":     100.0,
            "risk_per_trade_pct":   2.0,
            "transaction_fee_eur":  1.0,
        }
        price = 18.53
        eur, shares, _ = compute_position_size("BUY", 4, price, 0.07, inst)
        assert eur >= 0.0
        assert shares >= 0.0
        assert eur <= inst["max_position_eur"]

    def test_engine_parameter_accepted(self):
        """compute_position_size should accept engine kwarg without error."""
        inst = dict(self.BASE_INST)
        # con=None → Kelly skipped; engine kwarg must still be accepted
        eur_c, _, _ = compute_position_size("BUY", 4, 426.0, None, inst, con=None, engine="confluence")
        eur_m, _, _ = compute_position_size("BUY", 4, 426.0, None, inst, con=None, engine="macd")
        # Without a DB both fall back to confidence tiers → identical
        assert eur_c == eur_m

    def test_engine_none_fallback_matches_no_engine(self):
        """Passing engine=None should behave identically to omitting the parameter."""
        inst = dict(self.BASE_INST)
        eur_default, _, _ = compute_position_size("BUY", 5, 426.0, None, inst)
        eur_none,    _, _ = compute_position_size("BUY", 5, 426.0, None, inst, engine=None)
        assert eur_default == eur_none


# ── Kelly criterion ───────────────────────────────────────────────────────────

def _make_kelly_db(confluence_rows, macd_rows):
    """Build an in-memory DB with fake signals + outcomes for Kelly tests."""
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE signals (
        id INTEGER PRIMARY KEY, instrument_id TEXT, engine TEXT,
        signal_type TEXT, price REAL, ts TEXT)""")
    con.execute("""CREATE TABLE outcomes (
        id INTEGER PRIMARY KEY, signal_id INTEGER, price_24h REAL)""")
    sid = 1
    for engine, rows in (("confluence", confluence_rows), ("macd", macd_rows)):
        for sig_type, entry, exit_ in rows:
            con.execute("INSERT INTO signals VALUES (?,?,?,?,?,?)",
                        (sid, "inst-1", engine, sig_type, entry, "2024-01-01T00:00:00Z"))
            con.execute("INSERT INTO outcomes VALUES (?,?,?)", (sid, sid, exit_))
            sid += 1
    con.commit()
    return con


class TestKellyCriterion:
    # 35 winning BUY trades + 5 losing → realistic win-rate for confluence
    CONF_ROWS = [("BUY", 100.0, 110.0)] * 35 + [("BUY", 100.0, 90.0)] * 5
    # 35 losing BUY trades for macd (all losses → Kelly returns None for macd-only)
    MACD_ROWS = [("BUY", 100.0, 90.0)] * 35

    def test_returns_none_when_insufficient_data(self):
        con = _make_kelly_db([], [])
        assert compute_kelly_fraction(con, "inst-1") is None
        assert compute_kelly_fraction(con, "inst-1", engine="confluence") is None

    def test_returns_fraction_with_enough_data(self):
        con = _make_kelly_db(self.CONF_ROWS, self.MACD_ROWS)
        frac = compute_kelly_fraction(con, "inst-1", engine="confluence")
        assert frac is not None
        assert 0.05 <= frac <= 0.75

    def test_per_engine_fractions_differ(self):
        """Confluence (all wins) and MACD (all losses) must produce different fractions."""
        con = _make_kelly_db(self.CONF_ROWS, self.MACD_ROWS)
        frac_conf = compute_kelly_fraction(con, "inst-1", engine="confluence")
        # MACD has 35 outcomes but all losses → no wins list → returns None
        frac_macd = compute_kelly_fraction(con, "inst-1", engine="macd")
        assert frac_conf is not None
        assert frac_macd is None   # all-loss series → no wins → None

    def test_pooled_differs_from_per_engine(self):
        """Pooling all outcomes (engine=None) should give a different result than per-engine."""
        con = _make_kelly_db(self.CONF_ROWS, self.MACD_ROWS)
        frac_pooled = compute_kelly_fraction(con, "inst-1")
        frac_conf   = compute_kelly_fraction(con, "inst-1", engine="confluence")
        # Pooled mixes wins and losses; confluence-only is pure wins → fractions differ
        assert frac_pooled != frac_conf

    def test_fraction_clamped_to_bounds(self):
        """Result must always be in [0.05, 0.75] when returned."""
        # Mix of wins and losses so Kelly can be computed
        rows = [("BUY", 100.0, 110.0)] * 35 + [("BUY", 100.0, 90.0)] * 5
        con = _make_kelly_db(rows, [])
        frac = compute_kelly_fraction(con, "inst-1", engine="confluence")
        assert frac is not None
        assert 0.05 <= frac <= 0.75

    def test_unknown_instrument_returns_none(self):
        con = _make_kelly_db(self.CONF_ROWS, self.MACD_ROWS)
        assert compute_kelly_fraction(con, "does-not-exist", engine="confluence") is None

    def test_position_size_uses_kelly_when_available(self):
        """When DB has ≥30 outcomes, rationale string should mention Kelly."""
        con = _make_kelly_db(self.CONF_ROWS, [])
        inst = {
            "id": "inst-1",
            "buy_price": 100.0, "shares_held": 0.0, "total_invested": 100.0,
            "max_position_eur": 500.0, "risk_per_trade_pct": 2.0, "transaction_fee_eur": 1.0,
        }
        _, _, rationale = compute_position_size("BUY", 5, 100.0, None, inst, con=con, engine="confluence")
        assert "Kelly" in rationale


# ── Stop-loss monitor ─────────────────────────────────────────────────────────

def _make_sl_db():
    """In-memory SQLite DB with a minimal meta table for stop-loss cooldown tests."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    return con


def _base_inst(buy_price=100.0, shares=1.0, sl_pct=5.0, sl_cool=60):
    return {
        "id": "test-inst",
        "name": "Test Instrument",
        "currency": "EUR",
        "buy_price": buy_price,
        "shares_held": shares,
        "stop_loss_pct": sl_pct,
        "stop_loss_cooldown_minutes": sl_cool,
    }


class TestStopLoss:
    """Unit tests for check_stop_loss() — the stop-loss monitor function.

    All tests use an in-memory DB and a patched send_telegram_text so no real
    Telegram messages are ever sent.
    """

    def test_no_breach_returns_false(self, monkeypatch):
        """P/L above threshold: function returns (False, pl_pct, threshold)."""
        monkeypatch.setattr("analyze.send_telegram_text", lambda msg: None)
        inst = _base_inst(buy_price=100.0, shares=1.0, sl_pct=5.0)
        triggered, pl_pct, threshold = check_stop_loss(inst, 97.0, _make_sl_db())
        assert triggered is False
        assert abs(pl_pct - (-3.0)) < 0.01
        assert threshold == -5.0

    def test_breach_triggers_alert(self, monkeypatch):
        """P/L below threshold: function returns True and calls send_telegram_text."""
        sent = []
        monkeypatch.setattr("analyze.send_telegram_text", lambda msg: sent.append(msg))
        inst = _base_inst(buy_price=100.0, shares=1.0, sl_pct=5.0)
        triggered, pl_pct, threshold = check_stop_loss(inst, 94.0, _make_sl_db())
        assert triggered is True
        assert pl_pct < -5.0
        assert len(sent) == 1
        assert "STOP-LOSS" in sent[0]
        assert "test-inst" in sent[0] or "Test Instrument" in sent[0]

    def test_cooldown_suppresses_repeat_alert(self, monkeypatch):
        """Second breach within cooldown window must NOT send a second Telegram message."""
        sent = []
        monkeypatch.setattr("analyze.send_telegram_text", lambda msg: sent.append(msg))
        inst = _base_inst(buy_price=100.0, shares=1.0, sl_pct=5.0, sl_cool=60)
        con = _make_sl_db()
        # First breach — alert fires
        check_stop_loss(inst, 90.0, con)
        assert len(sent) == 1
        # Second breach within cooldown — alert suppressed
        check_stop_loss(inst, 89.0, con)
        assert len(sent) == 1          # still 1, no new message

    def test_recovery_clears_cooldown(self, monkeypatch):
        """After position recovers, cooldown is cleared so next breach re-alerts."""
        sent = []
        monkeypatch.setattr("analyze.send_telegram_text", lambda msg: sent.append(msg))
        inst = _base_inst(buy_price=100.0, shares=1.0, sl_pct=5.0, sl_cool=60)
        con = _make_sl_db()
        # First breach
        check_stop_loss(inst, 90.0, con)
        assert len(sent) == 1
        # Recovery — cooldown key should be cleared
        check_stop_loss(inst, 100.0, con)
        assert con.execute("SELECT value FROM meta WHERE key='stop_loss_alert:test-inst'").fetchone() is None
        # Second breach — should alert again
        check_stop_loss(inst, 90.0, con)
        assert len(sent) == 2

    def test_zero_shares_skips_check(self, monkeypatch):
        """No position held — check_stop_loss must return (False, None, None)."""
        monkeypatch.setattr("analyze.send_telegram_text", lambda msg: None)
        inst = _base_inst(buy_price=100.0, shares=0.0, sl_pct=5.0)
        triggered, pl_pct, threshold = check_stop_loss(inst, 80.0, _make_sl_db())
        assert triggered is False
        assert pl_pct is None
        assert threshold is None

    def test_no_buy_price_skips_check(self, monkeypatch):
        """No cost basis recorded — check_stop_loss must return (False, None, None)."""
        monkeypatch.setattr("analyze.send_telegram_text", lambda msg: None)
        inst = _base_inst(shares=1.0, sl_pct=5.0)
        inst["buy_price"] = None
        triggered, pl_pct, threshold = check_stop_loss(inst, 80.0, _make_sl_db())
        assert triggered is False
        assert pl_pct is None

    def test_no_stop_loss_pct_skips_check(self, monkeypatch):
        """stop_loss_pct not configured — silently skip."""
        monkeypatch.setattr("analyze.send_telegram_text", lambda msg: None)
        inst = _base_inst(shares=1.0)
        del inst["stop_loss_pct"]
        triggered, pl_pct, _ = check_stop_loss(inst, 80.0, _make_sl_db())
        assert triggered is False
        assert pl_pct is None

    def test_telegram_message_contains_key_info(self, monkeypatch):
        """Alert message must include price, avg cost, P/L %, and threshold."""
        sent = []
        monkeypatch.setattr("analyze.send_telegram_text", lambda msg: sent.append(msg))
        inst = _base_inst(buy_price=200.0, shares=2.0, sl_pct=10.0)
        check_stop_loss(inst, 170.0, _make_sl_db())   # −15%, well below −10%
        assert sent
        msg = sent[0]
        assert "170" in msg          # current price
        assert "200" in msg          # avg cost
        assert "-15" in msg or "−15" in msg or "-15.00" in msg  # P/L pct
        assert "10.0" in msg         # threshold
