"""
Point-in-time reconstruction of stock metrics and market context, for
backtesting analyze_stock() against real historical dates instead of today.

Why this exists: core/metrics.py's fundamentals (trailing_pe, profit_margin,
etc.) come from yfinance's `.info` dict, which is a single CURRENT snapshot
with no "as of a past date" variant, free or otherwise. To honestly ask
"what would the AI have said on 2026-03-01" — not "what does the AI say
today about a stock that used to trade at that price" — every number fed
into the prompt has to come from data that carries its own timestamp:
daily price history (real trading dates), and earnings tied to the date
they were actually DISCLOSED, not the fiscal quarter they describe. A
company's Q2 results aren't public knowledge until its earnings call,
weeks after the quarter ends — using the period-end date instead would be
look-ahead bias, quietly giving the model information it couldn't have had.

What's reconstructable, and what genuinely isn't (measured 2026-09-13):

  - close_price, 52-week high/low, distance_to_low/high_pct — exact, for
    years back. Free daily OHLC history has real depth.
  - market_trend, vix — exact, same 20/50-day SPY moving-average logic
    core.metrics uses today, computed from SPY's own historical closes as
    of the target date.
  - trailing_pe — reconstructed from quarterly EPS matched to its real
    disclosure date (yfinance's `get_earnings_dates`), which goes back
    several years for large/mid caps. Good depth.
  - profit_margin, operating_margin — from `quarterly_income_stmt`'s
    trailing-twelve-month window, which only ever exposes ~5 quarters
    relative to *whenever this code runs* (not to the historical date
    being asked about) — a hard ceiling in yfinance's free data. For an
    `as_of` older than that window, these come back None.
  - revenue_growth, earnings_growth — single most-recent-quarter vs the
    same quarter a year earlier, matching what Yahoo's live `revenueGrowth`
    / `earningsGrowth` actually measure (verified: reconstructing this way
    for as_of=today landed within rounding of the live figures for AAPL,
    MSFT, NVDA, and KO). An earlier version of this module compared
    trailing-twelve-month sums, which needs two non-overlapping four-
    quarter windows — 8 quarters — and the free quarterly endpoint only
    ever exposes about 5; every value silently came back None. A full
    fiscal-year comparison was tried next and also rejected: it measures a
    materially different (much smoother, annual-cadence) quantity than
    Yahoo's own quarterly figure, so feeding it into criteria calibrated
    against the quarterly definition would misjudge the threshold. The
    single-quarter comparison is what's actually right, and it needs
    exactly the two quarterly columns furthest apart in the 5 available —
    no extra data source required.
    One disclosed exception: this uses `Total Revenue`, which for banks
    (JPM in testing) reads noticeably lower than Yahoo's own revenue
    figure — financials report revenue differently (net interest income
    plus noninterest income, not a single top-line sales figure) and nail-
    ing that down would need a sector-specific rule this module doesn't
    yet have. Earnings growth is unaffected and matched Yahoo's own figure
    even for JPM.
  - forward_pe — NEVER reconstructable. It's an analyst consensus
    estimate; no free source publishes historical point-in-time consensus
    data at all. Always None.

None of the above are ever guessed at when unavailable — they're left as
None, which core.criteria's existing convention (`_evaluate_rule` returns
False for a None field) already handles exactly like a live analysis with
genuinely missing data: the rule doesn't pass, it isn't fabricated.

Sanity-checked, not just trusted: `reconstruct_metrics(symbol, today)`
should land close to `core.metrics.get_stock_metrics(symbol)`'s live
numbers for the same ticker — there's no external ground truth for a past
date (that absence is exactly why this module has to exist), but agreement
at as_of=today is evidence the reconstruction method itself is sound. See
tests/test_point_in_time.py.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import yfinance as yf


def _to_date(d: date | str) -> date:
    return d if isinstance(d, date) else date.fromisoformat(d)


def _safe(x):
    try:
        if x is None:
            return None
        f = float(x)
        return f if f == f else None  # filters NaN
    except (TypeError, ValueError):
        return None


# ── Price-based reconstruction (deep history, no ceiling) ───────────────

def reconstruct_price_metrics(symbol: str, as_of: date | str) -> dict:
    """close_price, 52-week high/low, and distance_to_*, as they stood on
    `as_of` — computed from a real trailing-365-day window ending on that
    date, not today's window."""
    as_of = _to_date(as_of)
    hist = yf.Ticker(symbol.strip().upper()).history(
        start=(as_of - timedelta(days=380)).isoformat(),
        end=(as_of + timedelta(days=1)).isoformat(),
        interval="1d",
    ).dropna(subset=["Close"])
    if hist.empty:
        return {"symbol": symbol.upper(), "date": None, "close_price": None,
                "low_52_week": None, "high_52_week": None,
                "distance_to_low_pct": None, "distance_to_high_pct": None}

    hist = hist[hist.index.date <= as_of]
    if hist.empty:
        return {"symbol": symbol.upper(), "date": None, "close_price": None,
                "low_52_week": None, "high_52_week": None,
                "distance_to_low_pct": None, "distance_to_high_pct": None}

    latest_date = hist.index[-1].strftime("%Y-%m-%d")
    close_price = _safe(float(hist["Close"].iloc[-1]))

    # core.metrics' live path uses yfinance's period="1y", which in
    # practice returns 366 calendar days back, not 365 — confirmed by
    # comparing against it directly: AAPL's true 52-week low sat exactly
    # on that 366th day and a strict 365-day cutoff silently excluded it,
    # understating the window by one real trading day. Matching what's
    # actually used, not the nominal name.
    window = hist[hist.index.date >= as_of - timedelta(days=366)]
    low_52w = _safe(float(window["Low"].min())) if not window.empty else None
    high_52w = _safe(float(window["High"].max())) if not window.empty else None

    distance_to_low = (round((close_price - low_52w) / low_52w, 4)
                       if (close_price and low_52w) else None)
    distance_to_high = (round((high_52w - close_price) / high_52w, 4)
                        if (high_52w and close_price) else None)

    return {
        "symbol": symbol.upper(), "date": latest_date, "close_price": close_price,
        "low_52_week": low_52w, "high_52_week": high_52w,
        "distance_to_low_pct": distance_to_low, "distance_to_high_pct": distance_to_high,
    }


def price_on_or_before(symbol: str, target: date | str) -> float | None:
    """Closing price on the last trading day on/before `target`. Used both
    to anchor a synthetic buy price for backtest sell scenarios, and by
    callers needing a single historical price without the rest of the
    52-week reconstruction."""
    target = _to_date(target)
    hist = yf.Ticker(symbol.strip().upper()).history(
        start=(target - timedelta(days=10)).isoformat(),
        end=(target + timedelta(days=1)).isoformat(),
        interval="1d",
    ).dropna(subset=["Close"])
    if hist.empty:
        return None
    hist = hist[hist.index.date <= target]
    return _safe(float(hist["Close"].iloc[-1])) if not hist.empty else None


# ── Market context reconstruction (deep history, no ceiling) ────────────

def reconstruct_market_context(as_of: date | str) -> dict:
    """market_trend + vix, as they stood on `as_of` — same 20dma/50dma
    crossover rule core.metrics._fetch_market_context uses today, applied
    to SPY's real historical closes."""
    as_of = _to_date(as_of)
    spy = yf.Ticker("SPY").history(
        start=(as_of - timedelta(days=100)).isoformat(),
        end=(as_of + timedelta(days=1)).isoformat(),
        interval="1d",
    ).dropna(subset=["Close"])
    spy = spy[spy.index.date <= as_of]

    spy_latest = _safe(float(spy["Close"].iloc[-1])) if not spy.empty else None
    spy_20dma = _safe(float(spy["Close"].tail(20).mean())) if len(spy) >= 20 else None
    spy_50dma = _safe(float(spy["Close"].tail(50).mean())) if len(spy) >= 50 else None

    market_trend = "unknown"
    if spy_latest and spy_20dma and spy_50dma:
        if spy_latest > spy_20dma > spy_50dma:
            market_trend = "bullish"
        elif spy_latest < spy_20dma < spy_50dma:
            market_trend = "bearish"
        else:
            market_trend = "mixed"

    vix_hist = yf.Ticker("^VIX").history(
        start=(as_of - timedelta(days=10)).isoformat(),
        end=(as_of + timedelta(days=1)).isoformat(),
        interval="1d",
    ).dropna(subset=["Close"])
    vix_hist = vix_hist[vix_hist.index.date <= as_of]
    vix = _safe(float(vix_hist["Close"].iloc[-1])) if not vix_hist.empty else None

    return {"market_trend": market_trend, "vix": vix, "spy_latest": spy_latest}


# ── Fundamentals reconstruction (trailing_pe: deep; the rest: ~12mo) ────

def _trailing_eps_asof(symbol: str, as_of: date) -> float | None:
    """Sum of the four most recent quarterly *reported* EPS figures whose
    disclosure date falls on or before `as_of`. Requires all four — a
    partial sum would silently understate trailing EPS rather than admit
    it doesn't have enough history."""
    try:
        ed = yf.Ticker(symbol).get_earnings_dates(limit=20)
    except Exception:
        return None
    if ed is None or ed.empty or "Reported EPS" not in ed.columns:
        return None
    ed = ed.dropna(subset=["Reported EPS"])
    ed = ed[ed.index.date <= as_of].sort_index(ascending=False)
    if len(ed) < 4:
        return None
    return _safe(float(ed["Reported EPS"].iloc[:4].sum()))


_NI_ROW = "Net Income From Continuing Operation Net Minority Interest"


def _ttm_income_asof(symbol: str, as_of: date) -> dict | None:
    """Trailing-twelve-month revenue, net income, and operating income as
    known on `as_of` — the sum of the four most recent quarters whose
    period actually ended by then, each quarter's *disclosure* date (not
    period end) checked against `as_of` via the nearest later earnings
    date. No explicit day-count ceiling: `quarterly_income_stmt` only ever
    holds ~5 columns relative to today, so for an `as_of` old enough that
    none of those columns were disclosed by then, the loop below simply
    never reaches 4 and returns None on its own — a fixed cutoff here
    would just be redundant with, and could disagree with, that."""
    try:
        t = yf.Ticker(symbol)
        income = t.quarterly_income_stmt
        ed = t.get_earnings_dates(limit=20)
    except Exception:
        return None
    if income is None or income.empty or ed is None or ed.empty:
        return None

    disclosure_dates = sorted(ed.index.date)
    rows = []
    for period_end in sorted(income.columns, reverse=True):
        pe_date = period_end.date() if hasattr(period_end, "date") else period_end
        # The disclosure date is the first earnings date after this
        # quarter's period end — typically 2-8 weeks later.
        later = [d for d in disclosure_dates if d > pe_date]
        disclosed_on = min(later) if later else None
        if disclosed_on is None or disclosed_on > as_of:
            continue
        rows.append(period_end)
        if len(rows) == 4:
            break
    if len(rows) < 4:
        return None

    def _sum(row_name: str) -> float | None:
        if row_name not in income.index:
            return None
        vals = [income.loc[row_name, c] for c in rows]
        vals = [v for v in (_safe(v) for v in vals) if v is not None]
        return sum(vals) if len(vals) == 4 else None

    return {
        "revenue": _sum("Total Revenue"),
        "net_income": _sum(_NI_ROW),
        "operating_income": _sum("Total Operating Income As Reported"),
        "oldest_period_end": min(rows).date().isoformat(),
    }


def _quarterly_yoy_asof(symbol: str, as_of: date) -> dict:
    """Single most-recent-quarter revenue/earnings vs the same quarter a
    year earlier, both gated by disclosure date — the definition Yahoo's
    live revenueGrowth/earningsGrowth actually use (verified against
    live .info at as_of=today). Needs the newest quarter disclosed by
    `as_of` and whichever available quarter sits closest to 12 months
    before it; with only ~5 quarterly columns ever exposed, that's
    ordinarily the oldest one available, not a deliberately-chosen 4th."""
    try:
        t = yf.Ticker(symbol)
        q = t.quarterly_income_stmt
        ed = t.get_earnings_dates(limit=20)
    except Exception:
        return {"revenue_growth": None, "earnings_growth": None}
    if q is None or q.empty or ed is None or ed.empty:
        return {"revenue_growth": None, "earnings_growth": None}

    disclosure_dates = sorted(ed.index.date)

    def _disclosed_on(period_end) -> date | None:
        pe_date = period_end.date() if hasattr(period_end, "date") else period_end
        later = [d for d in disclosure_dates if d > pe_date]
        return min(later) if later else None

    known = [c for c in sorted(q.columns, reverse=True)
            if (d := _disclosed_on(c)) is not None and d <= as_of]
    if len(known) < 2:
        return {"revenue_growth": None, "earnings_growth": None}

    latest = known[0]
    # The comparison quarter should be ~1 year (~365 days) before `latest`;
    # pick whichever known quarter's period end is closest to that target.
    target = latest - timedelta(days=365)
    prior = min(known[1:], key=lambda c: abs((c - target).days))

    def _pair(row_name: str) -> tuple[float | None, float | None]:
        if row_name not in q.index:
            return None, None
        return _safe(q.loc[row_name, latest]), _safe(q.loc[row_name, prior])

    rev1, rev0 = _pair("Total Revenue")
    ni1, ni0 = _pair(_NI_ROW)
    return {
        "revenue_growth": (round((rev1 - rev0) / abs(rev0), 4)
                          if (rev1 is not None and rev0) else None),
        "earnings_growth": (round((ni1 - ni0) / abs(ni0), 4)
                            if (ni1 is not None and ni0) else None),
    }


def reconstruct_fundamentals(symbol: str, as_of: date | str) -> dict:
    """trailing_pe, revenue_growth, earnings_growth, profit_margin,
    operating_margin as known on `as_of`; forward_pe always None. Each
    field independently None when its own data isn't reconstructable —
    they don't all succeed or fail together."""
    symbol = symbol.strip().upper()
    as_of = _to_date(as_of)

    price = price_on_or_before(symbol, as_of)
    eps = _trailing_eps_asof(symbol, as_of)
    trailing_pe = (round(price / eps, 2) if (price and eps and eps > 0) else None)

    profit_margin = operating_margin = None
    ttm = _ttm_income_asof(symbol, as_of)
    if ttm and ttm["revenue"]:
        rev = ttm["revenue"]
        if ttm["net_income"] is not None:
            profit_margin = round(ttm["net_income"] / rev, 4)
        if ttm["operating_income"] is not None:
            operating_margin = round(ttm["operating_income"] / rev, 4)

    growth = _quarterly_yoy_asof(symbol, as_of)

    return {
        "trailing_pe": trailing_pe,
        "forward_pe": None,  # never reconstructable — see module docstring
        "revenue_growth": growth["revenue_growth"],
        "earnings_growth": growth["earnings_growth"],
        "profit_margin": profit_margin,
        "operating_margin": operating_margin,
    }


# ── Combined ──────────────────────────────────────────────────────────────

def reconstruct_metrics(symbol: str, as_of: date | str) -> dict:
    """Same key shape as core.metrics.get_stock_metrics(), built entirely
    from data timestamped on or before `as_of` — drops straight into
    evaluate_criteria() / _build_prompt() unchanged."""
    metrics = reconstruct_price_metrics(symbol, as_of)
    metrics.update(reconstruct_fundamentals(symbol, as_of))
    return metrics
