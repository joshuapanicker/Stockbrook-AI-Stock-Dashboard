"""log_call's error handling.

The ai_calls Supabase table did not exist in production for roughly a
month — the migration was written but never run — and log_call's insert
failure was caught by a bare `except Exception: pass` commented as if it
only handled a same-day duplicate. It didn't distinguish that from "table
missing," so the public track record silently logged nothing the whole
time, with nothing in the logs to say why.

    py -3 -m pytest tests/test_track_record.py
"""

from __future__ import annotations

import logging
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
