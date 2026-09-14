"""core/track_record.py: log_call's error handling, and _price_on_or_after's
period selection.

log_call: the ai_calls Supabase table did not exist in production for
roughly a month — the migration was written but never run — and the
insert failure was caught by a bare `except Exception: pass` commented as
if it only handled a same-day duplicate. It didn't distinguish that from
"table missing," so the public track record silently logged nothing the
whole time, with nothing in the logs to say why.

_price_on_or_after: written assuming `target` is always recent, which
holds for a live call (whose horizons are at most 180 days past
call_date) but not for a backtest call, whose call_date can be years old.
The period ladder topped out at "2y" — for an older target, every point
in the fetched history sits at or after it, so every horizon resolved to
the same earliest available price regardless of how much further back the
oldest one actually was. Caught on a real row: an AMZN backtest sell from
2023 resolved identical 30d/90d/180d returns.

    py -3 -m pytest tests/test_track_record.py
"""

from __future__ import annotations

import logging
from datetime import date, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from postgrest.exceptions import APIError

from core import track_record as tr


def make_sb(*, code: str, message: str = "error") -> MagicMock:
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.side_effect = (
        APIError({"code": code, "message": message}))
    return sb


def test_duplicate_key_is_silent(caplog):
    """The one failure this is allowed to swallow: the same symbol+action
    already logged today, enforced by the table's unique constraint."""
    sb = make_sb(code="23505", message="duplicate key value violates unique constraint")
    with patch.object(tr, "_supabase", return_value=sb):
        with caplog.at_level(logging.ERROR, logger="stockbrook"):
            tr.log_call("AAPL", "buy", "YES", 200.0, 500.0, 3, 5)
    assert caplog.records == []


def test_missing_table_is_logged(caplog):
    """The exact failure that hid silently for a month in production."""
    sb = make_sb(code="PGRST205", message="Could not find the table 'public.ai_calls'")
    with patch.object(tr, "_supabase", return_value=sb):
        with caplog.at_level(logging.ERROR, logger="stockbrook"):
            tr.log_call("AAPL", "buy", "YES", 200.0, 500.0, 3, 5)
    assert len(caplog.records) == 1
    assert "AAPL" in caplog.text


@pytest.mark.parametrize("code", ["42501", "08006", None])
def test_other_failures_are_also_logged(caplog, code):
    """Anything that isn't the specific expected duplicate must be visible
    — permission errors, connection failures, a typo'd column, all of it."""
    sb = make_sb(code=code, message="something else went wrong")
    with patch.object(tr, "_supabase", return_value=sb):
        with caplog.at_level(logging.ERROR, logger="stockbrook"):
            tr.log_call("AAPL", "buy", "YES", 200.0, 500.0, 3, 5)
    assert len(caplog.records) == 1


def test_successful_insert_logs_nothing(caplog):
    sb = MagicMock()
    with patch.object(tr, "_supabase", return_value=sb):
        with caplog.at_level(logging.ERROR, logger="stockbrook"):
            tr.log_call("AAPL", "buy", "YES", 200.0, 500.0, 3, 5)
    assert caplog.records == []
    sb.table.return_value.insert.assert_called_once()


def test_no_price_never_calls_supabase():
    """A verdict with no price can't be resolved against future price
    history, so it's dropped before ever reaching the insert."""
    sb = MagicMock()
    with patch.object(tr, "_supabase", return_value=sb):
        tr.log_call("AAPL", "buy", "YES", None, 500.0, 3, 5)
    sb.table.assert_not_called()


def test_watch_action_never_calls_supabase():
    """Only buy/sell are directional calls worth scoring."""
    sb = MagicMock()
    with patch.object(tr, "_supabase", return_value=sb):
        tr.log_call("AAPL", "watch", "YES", 200.0, 500.0, 3, 5)
    sb.table.assert_not_called()


# ── _price_on_or_after: period selection for old targets ────────────────

def _hist_point(d: date, close: float) -> dict:
    return {"date": d.isoformat(), "close": close}


def test_old_target_uses_a_period_wide_enough_to_contain_it():
    """The actual defect: for a target ~1000 days back, the old "2y"
    ceiling fetched history that didn't reach the target at all, so the
    loop matched the first (most recent-available) point regardless of
    how much further back the true target was."""
    old_target = date.today() - timedelta(days=1000)

    # "2y" history that does NOT reach back to old_target — every point in
    # it is >= old_target, so the old code would return this one.
    wrong_answer = _hist_point(date.today() - timedelta(days=700), 999.0)
    # "max" history that DOES reach back far enough to contain the real
    # trading day at/after old_target.
    correct_answer = _hist_point(old_target + timedelta(days=1), 42.0)

    def fake_get_price_history(symbol, period):
        if period == "max":
            return [correct_answer, wrong_answer]
        return [wrong_answer]  # any shorter period: too narrow, as before

    with patch("core.metrics.get_price_history", side_effect=fake_get_price_history):
        result = tr._price_on_or_after("AAPL", old_target)
    assert result == 42.0, "used a period too narrow to contain the target"


def test_three_horizons_on_an_old_call_date_resolve_to_different_prices():
    """The observed symptom: a real backtest row had identical
    30d/90d/180d returns because all three targets fell outside the "2y"
    window and collapsed onto the same fallback price."""
    call_date = date.today() - timedelta(days=1000)
    targets = [call_date + timedelta(days=n) for n in (30, 90, 180)]
    # A "max"-only series with a genuinely different close near each target.
    series = [_hist_point(t + timedelta(days=1), 100.0 + i * 50)
             for i, t in enumerate(targets)]

    def fake_get_price_history(symbol, period):
        return series if period == "max" else []

    with patch("core.metrics.get_price_history", side_effect=fake_get_price_history):
        prices = [tr._price_on_or_after("AAPL", t) for t in targets]
    assert len(set(prices)) == 3, f"horizons collapsed onto the same price: {prices}"


def test_recent_target_still_uses_a_short_period():
    """No behavior change for the case this was already correct for —
    live calls, whose targets are always within a year or so."""
    recent_target = date.today() - timedelta(days=10)
    with patch("core.metrics.get_price_history") as mock_hist:
        mock_hist.return_value = [_hist_point(recent_target, 10.0)]
        tr._price_on_or_after("AAPL", recent_target)
    mock_hist.assert_called_once_with("AAPL", "3mo")
