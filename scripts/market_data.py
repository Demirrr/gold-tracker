"""Helpers for fetching market data from yfinance with retries/fallbacks."""
from __future__ import annotations

import logging
import time
from typing import Iterable

import yfinance as yf

_INTERVALS = ("2m", "5m", "15m", "1h")
_RETRY_DELAYS_SEC = (1.0, 2.0)


def _normalize_download_frame(df, ticker):
    if df is None or df.empty:
        return df
    columns = getattr(df, "columns", None)
    if getattr(columns, "nlevels", 1) == 1:
        return df
    level_values = columns.get_level_values(-1)
    if ticker in level_values:
        try:
            return df.xs(ticker, axis=1, level=-1)
        except Exception:
            pass
    # Fallback: use the first non-empty ticker symbol found in the last level
    for val in level_values.unique():
        if val and val is not None:
            try:
                result = df.xs(val, axis=1, level=-1)
                if not result.empty:
                    return result
            except Exception:
                continue
    return df


def _log_fetch_error(logger, instrument_id, method, interval, attempt, exc):
    logger.warning(
        "[%s] yfinance fetch failed (%s interval=%s attempt=%d): %s",
        instrument_id,
        method,
        interval,
        attempt,
        exc,
    )


def fetch_history(
    ticker: str,
    instrument_id: str,
    logger: logging.Logger,
    intervals: Iterable[str] = _INTERVALS,
    period: str = "5d",
):
    """
    Return the freshest non-empty OHLCV frame available.

    The primary path uses ``Ticker.history()``. When that raises or returns
    nothing, fall back to ``yf.download()`` and retry transient failures.
    Returns ``(df, interval, source)`` or ``(None, None, None)``.
    """
    retry_delays = (0.0, *_RETRY_DELAYS_SEC)
    for attempt, delay in enumerate(retry_delays, start=1):
        if delay:
            time.sleep(delay)
        for interval in intervals:
            try:
                df = yf.Ticker(ticker).history(period=period, interval=interval)
                if df is not None and not df.empty:
                    return df, interval, "ticker.history"
            except Exception as exc:
                _log_fetch_error(logger, instrument_id, "history", interval, attempt, exc)

            for auto_adj in (False, True):
                try:
                    df = yf.download(
                        ticker,
                        period=period,
                        interval=interval,
                        progress=False,
                        auto_adjust=auto_adj,
                        threads=False,
                    )
                    df = _normalize_download_frame(df, ticker)
                    if df is not None and not df.empty:
                        src = "download" if not auto_adj else "download(auto_adjust)"
                        return df, interval, src
                except Exception as exc:
                    _log_fetch_error(logger, instrument_id, f"download(auto_adjust={auto_adj})",
                                     interval, attempt, exc)

    return None, None, None


def latest_close(
    ticker: str,
    instrument_id: str,
    logger: logging.Logger,
    intervals: Iterable[str] = _INTERVALS,
    period: str = "5d",
):
    df, _, _ = fetch_history(
        ticker=ticker,
        instrument_id=instrument_id,
        logger=logger,
        intervals=intervals,
        period=period,
    )
    if df is None or df.empty:
        return None
    return float(df["Close"].iloc[-1])
