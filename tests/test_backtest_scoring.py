"""Scoring and output-audit logic for the backtest ledger.

Two things here are easy to get silently, expensively wrong:

  1. Direction. A sell call wins when the stock FALLS. The per-row
     `return_*`/`alpha_*` fields stay factual (what the stock did), so
     any aggregate that reports "how well did the calls do" has to flip
     the sign for sells. Get it backwards and a wall of failed sell calls
     reads as a strong track record — the live UI's colouring has exactly
     this bug today, which is what prompted these tests.

  2. Attribution. `ai_yes` vs `rules_passed` vs `all_calls` only mean
     something relative to each other; mixing up which rows land in which
     arm would make the AI look like it earned a result the free
     deterministic screener produced on its own.

    py -3 -m pytest tests/test_backtest_scoring.py
"""

from __future__ import annotations

import pytest

from core.backtest_ledger import _score
from core.output_audit import unsourced_numbers


def row(action: str, ret: float, alpha: float | None = None, **extra) -> dict:
    r = {"action": action, "return_30d": ret, **extra}
    if alpha is not None:
        r["alpha_30d"] = alpha
    return r


# ── direction ───────────────────────────────────────────────────────────

def test_buy_wins_when_the_stock_rises():
    rows = [row("buy", 0.10), row("buy", 0.05), row("buy", -0.20)]
    result = _score(rows, "buy", "30d")
    assert result["win_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert result["avg_return"] == pytest.approx((0.10 + 0.05 - 0.20) / 3, abs=1e-4)


def test_sell_wins_when_the_stock_falls():
    """Same raw numbers as the buy case, opposite verdict — so the win
    rate must invert, not repeat."""
    rows = [row("sell", 0.10), row("sell", 0.05), row("sell", -0.20)]
    result = _score(rows, "sell", "30d")
    assert result["win_rate"] == pytest.approx(1 / 3, abs=1e-4)


def test_sell_avg_return_is_reported_in_the_calls_favour():
    """A sell on a stock that then dropped 20% is a GOOD call, and must
    aggregate as positive — reporting the raw -0.20 would make the best
    possible sell call look like the worst."""
    result = _score([row("sell", -0.20)], "sell", "30d")
    assert result["avg_return"] == pytest.approx(0.20)


def test_sell_alpha_flips_too():
    """The bug that motivated this: a sell whose stock beat SPY by 14%
    is a failed call. Unflipped it reports +0.14 and reads as success."""
    result = _score([row("sell", 0.20, alpha=0.14)], "sell", "30d")
    assert result["avg_alpha_vs_spy"] == pytest.approx(-0.14)


def test_buy_alpha_is_not_flipped():
    result = _score([row("buy", 0.20, alpha=0.14)], "buy", "30d")
    assert result["avg_alpha_vs_spy"] == pytest.approx(0.14)


# ── empty / partial data ────────────────────────────────────────────────

def test_unresolved_rows_are_excluded_not_counted_as_zero():
    """A horizon that hasn't elapsed has no return key at all. Counting
    it as 0.0 would quietly drag every average toward nothing."""
    rows = [row("buy", 0.10), {"action": "buy"}]  # second has no return_30d
    result = _score(rows, "buy", "30d")
    assert result["count"] == 1
    assert result["avg_return"] == pytest.approx(0.10)


def test_no_rows_returns_nulls_not_a_crash():
    result = _score([], "buy", "30d")
    assert result == {"count": 0, "avg_return": None, "win_rate": None,
                      "avg_alpha_vs_spy": None}


def test_missing_alpha_does_not_block_return_scoring():
    result = _score([row("buy", 0.10)], "buy", "30d")
    assert result["avg_return"] == pytest.approx(0.10)
    assert result["avg_alpha_vs_spy"] is None


# ── output audit ────────────────────────────────────────────────────────

def test_number_present_in_prompt_is_not_flagged():
    assert unsourced_numbers("revenue grew to 57006 million", "revenue 57006") == []


def test_ratio_written_as_a_percentage_is_not_flagged():
    """The prompt carries 0.63; writing "63% margin" is correct, not
    invented — flagging it would bury real fabrications in noise."""
    assert unsourced_numbers("margin of 63%", '"profit_margin":0.63') == []


def test_number_absent_from_prompt_is_flagged():
    assert unsourced_numbers("revenue was 99999", "revenue 57006") == ["99999"]


def test_small_numbers_are_ignored_as_structural():
    """"2 of 5 criteria", bullet numbering, a P/E of 8 — not claims."""
    assert unsourced_numbers("2 of 5 rules, point 3", "nothing here") == []
