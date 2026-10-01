"""Paired RAG A/B analysis.

The headline figure is the agreement rate between the two arms, and the
accounting has to be exact: a pair miscounted as agreeing hides the only
scenarios where outcome quality is answerable at all. Disagreements are
the scarce resource in this design, so losing one to an off-by-one
matters more than it would anywhere else.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.rag_ab import compute_rag_ab


def pair(grounded, ungrounded, ret=None, **extra):
    row = {"symbol": "X", "action": "sell", "resolved_at": "2026-09-30T00:00:00Z",
           "decision_grounded": grounded, "decision_ungrounded": ungrounded,
           "unsourced_grounded": [], "unsourced_ungrounded": [], **extra}
    if ret is not None:
        row["return_180d"] = ret
        row["return_30d"] = ret
        row["return_90d"] = ret
    return row


def run(rows):
    with patch("core.rag_ab._all_pairs", return_value=rows):
        return compute_rag_ab()


def test_no_pairs_is_not_a_crash():
    assert run([])["pairs"] == 0


def test_agreement_counts_both_directions_of_sameness():
    """YES/YES and NO/NO are both agreement — only a differing verdict
    counts as the filing having changed the decision."""
    rows = [pair("YES", "YES"), pair("NO", "NO"), pair("YES", "NO")]
    result = run(rows)["agreement"]
    assert result["same_verdict"] == 2
    assert result["disagreed"] == 1
    assert result["rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_flip_direction_is_attributed_correctly():
    """Which way the filing moved the verdict is the interesting part:
    did grounding create a sell call, or suppress one?"""
    rows = [pair("YES", "NO"), pair("YES", "NO"), pair("NO", "YES")]
    result = run(rows)["agreement"]
    assert result["filing_flipped_to_yes"] == 2   # grounding created these
    assert result["filing_flipped_to_no"] == 1    # grounding suppressed this


def test_each_arm_is_scored_on_its_own_yes_calls():
    """The arms disagree about which scenarios are sells, so each must be
    scored over the set IT picked — not over a shared set."""
    rows = [pair("YES", "NO", ret=-0.20),   # only grounded picked; it fell -> win
            pair("NO", "YES", ret=0.30)]    # only ungrounded picked; it rose -> loss
    s = run(rows)["summary"]["sell_180d"]
    assert s["grounded_yes"]["count"] == 1
    assert s["grounded_yes"]["win_rate"] == 1.0
    assert s["ungrounded_yes"]["count"] == 1
    assert s["ungrounded_yes"]["win_rate"] == 0.0
    assert s["all_scenarios"]["count"] == 2      # the shared denominator


def test_pairs_missing_an_arm_are_excluded():
    """A row where one arm failed to parse isn't a pair and can't be
    compared — counting it would inflate agreement."""
    rows = [pair("YES", "YES"), pair("YES", None), pair(None, "NO")]
    assert run(rows)["pairs"] == 1


def test_unsourced_rate_is_reported_per_arm():
    rows = [pair("NO", "NO", unsourced_grounded=["99999"], unsourced_ungrounded=[]),
            pair("NO", "NO", unsourced_grounded=[], unsourced_ungrounded=[])]
    rates = run(rows)["unsourced_rate"]
    assert rates["grounded"] == pytest.approx(0.5)
    assert rates["ungrounded"] == pytest.approx(0.0)
