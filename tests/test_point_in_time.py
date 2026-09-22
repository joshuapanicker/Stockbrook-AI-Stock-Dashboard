"""core/point_in_time.py — offline, mocked tests.

These lock in the two real bugs found while building this module (both
caught by comparing reconstruction at as_of=today against the live app's
own numbers, not by inspection):

  1. A 365-day 52-week window silently excluded the actual low when it
     fell on the 366th day back — which is what yfinance's own period="1y"
     actually returns. AAPL's real low sat exactly on that boundary.
  2. revenue_growth/earnings_growth compared trailing-twelve-month sums,
     which needs two non-overlapping four-quarter windows (8 quarters);
     the free quarterly endpoint only ever exposes ~5, so every value
     came back None regardless of `as_of`. The fix compares single
     quarters four-slots apart instead, which is also what Yahoo's own
     live figures actually measure.

Both are regression risks precisely because they don't raise — they
compute a plausible, wrong-or-empty number instead of erroring, so a
retest here has to check VALUES, not just "did it run."

    py -3 -m pytest tests/test_point_in_time.py
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.point_in_time import (
    _NI_ROW,
    _quarterly_yoy_asof,
    _trailing_eps_asof,
    _ttm_income_asof,
    reconstruct_fundamentals,
    reconstruct_market_context,
    reconstruct_price_metrics,
)


def daily_history(rows: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    """rows of (date, open, high, low, close) -> a frame shaped like
    yfinance's .history(), tz-aware index included (real yfinance is
    tz-aware; .date must still work correctly against it)."""
    idx = pd.DatetimeIndex([r[0] for r in rows], tz="America/New_York")
    return pd.DataFrame({
        "Open": [r[1] for r in rows], "High": [r[2] for r in rows],
        "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
    }, index=idx)


def earnings_dates(pairs: list[tuple[str, float]]) -> pd.DataFrame:
    """(date, reported_eps) -> a frame shaped like get_earnings_dates()."""
    idx = pd.DatetimeIndex([p[0] for p in pairs], tz="America/New_York")
    return pd.DataFrame({"Reported EPS": [p[1] for p in pairs]}, index=idx)


def quarterly_income(cols: dict[str, dict[str, float]]) -> pd.DataFrame:
    """{period_end_iso: {row_name: value}} -> a frame shaped like
    quarterly_income_stmt (rows = line items, columns = period ends)."""
    df = pd.DataFrame(cols)
    df.columns = pd.to_datetime(df.columns)
    return df


# yfinance USUALLY returns a tz-aware DatetimeIndex, so that's the default
# fixture — but "always" was wrong. In production an unresolvable symbol
# (a delisted ticker, a transient 404 on ^VIX) came back as an empty frame
# with a plain Index, `.index.date` raised, and four scheduled runs died.
# _PLAIN_INDEX_HISTORY below is that real shape.
_EMPTY_HISTORY = pd.DataFrame(columns=["Open", "High", "Low", "Close"],
                              index=pd.DatetimeIndex([], tz="America/New_York"))
_EMPTY_EARNINGS = pd.DataFrame(columns=["Reported EPS"],
                               index=pd.DatetimeIndex([], tz="America/New_York"))


def mock_ticker(history=None, quarterly_income_stmt=None, earnings_dates_df=None):
    t = MagicMock()
    t.history.return_value = history if history is not None else _EMPTY_HISTORY
    t.quarterly_income_stmt = (quarterly_income_stmt if quarterly_income_stmt is not None
                               else pd.DataFrame())
    t.get_earnings_dates.return_value = (earnings_dates_df if earnings_dates_df is not None
                                         else _EMPTY_EARNINGS)
    return t


# ── reconstruct_price_metrics: the 366-day boundary bug ─────────────────

def test_366_day_window_includes_the_boundary_low():
    """The actual defect: AAPL's true 52-week low sat exactly 366 days
    back and a stricter 365-day cutoff silently dropped it."""
    as_of = date(2026, 9, 13)
    boundary = (as_of - timedelta(days=366)).isoformat()  # the day that must count
    hist = daily_history([
        (boundary, 100, 101, 90.0, 100),   # the real low — must be included
        ("2026-06-01", 200, 205, 195.0, 200),
        (as_of.isoformat(), 210, 212, 208.0, 210),
    ])
    with patch("core.point_in_time.yf.Ticker", return_value=mock_ticker(history=hist)):
        result = reconstruct_price_metrics("AAPL", as_of)
    assert result["low_52_week"] == 90.0
    assert result["close_price"] == 210.0


def test_a_day_beyond_the_window_is_excluded():
    as_of = date(2026, 9, 13)
    too_old = (as_of - timedelta(days=367)).isoformat()
    hist = daily_history([
        (too_old, 100, 101, 5.0, 100),      # older than the window — must NOT count
        ("2026-06-01", 200, 205, 195.0, 200),
        (as_of.isoformat(), 210, 212, 208.0, 210),
    ])
    with patch("core.point_in_time.yf.Ticker", return_value=mock_ticker(history=hist)):
        result = reconstruct_price_metrics("AAPL", as_of)
    assert result["low_52_week"] == 195.0


def test_never_uses_a_bar_after_as_of():
    """A backtest date in the middle of fetched history must not see the
    future — the whole reason this module exists rather than just calling
    core.metrics with a modified clock."""
    as_of = date(2026, 3, 1)
    hist = daily_history([
        ("2026-02-01", 100, 101, 95.0, 100),
        (as_of.isoformat(), 100, 101, 98.0, 99),
        ("2026-06-01", 300, 305, 1.0, 300),   # after as_of — must be invisible
    ])
    with patch("core.point_in_time.yf.Ticker", return_value=mock_ticker(history=hist)):
        result = reconstruct_price_metrics("AAPL", as_of)
    assert result["close_price"] == 99.0
    assert result["low_52_week"] == 95.0  # not 1.0 from the future bar


def test_empty_history_returns_none_fields_not_a_crash():
    with patch("core.point_in_time.yf.Ticker", return_value=mock_ticker()):
        result = reconstruct_price_metrics("ZZZZ", date(2026, 1, 1))
    assert result["close_price"] is None
    assert result["low_52_week"] is None


# ── _trailing_eps_asof / _ttm_income_asof: disclosure-date gating ───────

def test_eps_requires_disclosure_not_just_period_end():
    """A quarter that ENDED before as_of but wasn't DISCLOSED until after
    it must not count — using it would be look-ahead bias, the exact
    failure mode this module exists to avoid."""
    as_of = date(2026, 1, 1)
    ed = earnings_dates([
        ("2025-01-30", 1.0), ("2025-04-30", 1.1), ("2025-07-30", 1.2),
        ("2026-01-15", 1.3),   # disclosed AFTER as_of — must be excluded
    ])
    with patch("core.point_in_time.yf.Ticker", return_value=mock_ticker(earnings_dates_df=ed)):
        result = _trailing_eps_asof("AAPL", as_of)
    assert result is None  # only 3 qualify, not the required 4


def test_eps_sums_exactly_four_disclosed_quarters():
    as_of = date(2026, 2, 1)
    ed = earnings_dates([
        ("2024-10-30", 0.5),  # a 5th, genuinely older quarter — must be excluded
        ("2025-01-30", 1.0), ("2025-04-30", 1.1),
        ("2025-07-30", 1.2), ("2025-10-30", 1.3),
    ])
    with patch("core.point_in_time.yf.Ticker", return_value=mock_ticker(earnings_dates_df=ed)):
        result = _trailing_eps_asof("AAPL", as_of)
    assert result == pytest.approx(1.0 + 1.1 + 1.2 + 1.3)  # the 4 most recent, not the 5th


def test_ttm_income_missing_row_returns_none_for_that_field_only():
    """A bank's income statement has no standard 'Operating Income' row —
    that must come back None, not raise, and not silently zero."""
    as_of = date(2026, 2, 1)
    ed = earnings_dates([
        ("2025-01-30", 1.0), ("2025-04-30", 1.0),
        ("2025-07-30", 1.0), ("2025-10-30", 1.0),
    ])
    income = quarterly_income({
        "2025-09-30": {"Total Revenue": 100.0, _NI_ROW: 10.0},
        "2025-06-30": {"Total Revenue": 90.0, _NI_ROW: 9.0},
        "2025-03-30": {"Total Revenue": 80.0, _NI_ROW: 8.0},
        "2024-12-30": {"Total Revenue": 70.0, _NI_ROW: 7.0},
        # no "Total Operating Income As Reported" row at all
    })
    t = mock_ticker(quarterly_income_stmt=income, earnings_dates_df=ed)
    with patch("core.point_in_time.yf.Ticker", return_value=t):
        result = _ttm_income_asof("AAPL", as_of)
    assert result["revenue"] == pytest.approx(340.0)
    assert result["operating_income"] is None


# ── _quarterly_yoy_asof: single-quarter comparison, not a TTM sum ───────

def test_growth_compares_two_single_quarters_not_ttm_sums():
    """The actual fix: growth is (latest quarter) vs (same quarter ~1yr
    earlier), each a single quarter — never a sum of several."""
    as_of = date(2026, 9, 13)
    ed = earnings_dates([
        ("2025-07-30", 1.0), ("2025-10-30", 1.0),
        ("2026-01-30", 1.0), ("2026-04-30", 1.0), ("2026-07-30", 1.0),
    ])
    income = quarterly_income({
        "2026-06-30": {"Total Revenue": 150.0, _NI_ROW: 15.0},  # latest
        "2026-03-31": {"Total Revenue": 140.0, _NI_ROW: 14.0},
        "2025-12-31": {"Total Revenue": 130.0, _NI_ROW: 13.0},
        "2025-09-30": {"Total Revenue": 120.0, _NI_ROW: 12.0},
        "2025-06-30": {"Total Revenue": 100.0, _NI_ROW: 10.0},  # ~1yr before latest
    })
    t = mock_ticker(quarterly_income_stmt=income, earnings_dates_df=ed)
    with patch("core.point_in_time.yf.Ticker", return_value=t):
        result = _quarterly_yoy_asof("AAPL", as_of)
    # 150 vs 100 (single quarters), NOT (150+140+130+120) vs anything
    assert result["revenue_growth"] == pytest.approx((150.0 - 100.0) / 100.0)
    assert result["earnings_growth"] == pytest.approx((15.0 - 10.0) / 10.0)


def test_growth_excludes_a_quarter_not_yet_disclosed():
    as_of = date(2026, 6, 1)
    ed = earnings_dates([
        ("2025-04-30", 1.0), ("2025-07-30", 1.0),
        ("2025-10-30", 1.0), ("2026-01-30", 1.0),
        # the quarter ending 2026-03-31 is NOT yet disclosed as of as_of
    ])
    income = quarterly_income({
        "2026-03-31": {"Total Revenue": 999.0, _NI_ROW: 999.0},  # undisclosed — must be ignored
        "2025-12-31": {"Total Revenue": 130.0, _NI_ROW: 13.0},
        "2025-09-30": {"Total Revenue": 120.0, _NI_ROW: 12.0},
        "2025-06-30": {"Total Revenue": 110.0, _NI_ROW: 11.0},
        "2025-03-31": {"Total Revenue": 100.0, _NI_ROW: 10.0},
    })
    t = mock_ticker(quarterly_income_stmt=income, earnings_dates_df=ed)
    with patch("core.point_in_time.yf.Ticker", return_value=t):
        result = _quarterly_yoy_asof("AAPL", as_of)
    assert result["revenue_growth"] == pytest.approx((130.0 - 100.0) / 100.0)


# ── reconstruct_fundamentals: forward_pe and independent-None fields ────

def test_forward_pe_is_always_none():
    with patch("core.point_in_time.yf.Ticker", return_value=mock_ticker()):
        result = reconstruct_fundamentals("AAPL", date(2026, 1, 1))
    assert result["forward_pe"] is None


def test_fundamentals_fields_fail_independently():
    """No earnings-date data at all -> trailing_pe/growth/margins all
    None, but the function still returns the full expected key set rather
    than raising."""
    with patch("core.point_in_time.yf.Ticker", return_value=mock_ticker()):
        result = reconstruct_fundamentals("ZZZZ", date(2026, 1, 1))
    for key in ("trailing_pe", "revenue_growth", "earnings_growth",
               "profit_margin", "operating_margin"):
        assert result[key] is None


# ── reconstruct_market_context ───────────────────────────────────────────

def test_market_trend_bullish_when_price_above_both_averages():
    as_of = date(2026, 3, 1)
    # 60 rising days: latest close > 20dma > 50dma
    rows = [((date(2026, 1, 1) + timedelta(days=i)).isoformat(), 100 + i, 100 + i,
            100 + i, 100 + i) for i in range(60)]
    spy_hist = daily_history(rows)
    vix_hist = daily_history([(as_of.isoformat(), 15, 15, 15, 15.0)])

    def side_effect(sym):
        return mock_ticker(history=vix_hist if sym == "^VIX" else spy_hist)

    with patch("core.point_in_time.yf.Ticker", side_effect=side_effect):
        result = reconstruct_market_context(as_of)
    assert result["market_trend"] == "bullish"
    assert result["vix"] == 15.0


def test_market_trend_bearish_when_price_below_both_averages():
    as_of = date(2026, 3, 1)
    rows = [((date(2026, 1, 1) + timedelta(days=i)).isoformat(), 160 - i, 160 - i,
            160 - i, 160 - i) for i in range(60)]
    spy_hist = daily_history(rows)

    def side_effect(sym):
        return mock_ticker(history=spy_hist if sym != "^VIX" else _EMPTY_HISTORY)

    with patch("core.point_in_time.yf.Ticker", side_effect=side_effect):
        result = reconstruct_market_context(as_of)
    assert result["market_trend"] == "bearish"


# ── indexes that aren't dated ───────────────────────────────────────────

_PLAIN_INDEX_HISTORY = pd.DataFrame(columns=["Open", "High", "Low", "Close"])


def test_price_metrics_survive_an_undated_index():
    """The production crash: an empty frame with a plain Index, not a
    DatetimeIndex. `.index.date` raises AttributeError on it."""
    with patch("core.point_in_time.yf.Ticker",
               return_value=mock_ticker(history=_PLAIN_INDEX_HISTORY)):
        result = reconstruct_price_metrics("ZZZZ", date(2026, 1, 1))
    assert result["close_price"] is None


def test_market_context_survives_an_undated_vix_index():
    """Exactly what took the runs down: SPY resolved fine, ^VIX came back
    with an undated index, and the whole batch aborted on it."""
    as_of = date(2026, 3, 1)
    rows = [((date(2026, 1, 1) + timedelta(days=i)).isoformat(), 100 + i, 100 + i,
            100 + i, 100 + i) for i in range(60)]
    spy_hist = daily_history(rows)

    def side_effect(sym):
        return mock_ticker(history=_PLAIN_INDEX_HISTORY if sym == "^VIX" else spy_hist)

    with patch("core.point_in_time.yf.Ticker", side_effect=side_effect):
        result = reconstruct_market_context(as_of)
    assert result["market_trend"] == "bullish"   # SPY still worked
    assert result["vix"] is None                 # VIX degraded, didn't raise


def test_trailing_eps_survives_an_undated_earnings_index():
    undated = pd.DataFrame({"Reported EPS": [1.0]})
    with patch("core.point_in_time.yf.Ticker",
               return_value=mock_ticker(earnings_dates_df=undated)):
        assert _trailing_eps_asof("ZZZZ", date(2026, 1, 1)) is None
