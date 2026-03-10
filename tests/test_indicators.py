"""
Unit tests for all technical indicator functions in analyze.py.
These tests use synthetic price series with known mathematical properties,
so they do NOT require a database or network access.
"""
import sys
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
