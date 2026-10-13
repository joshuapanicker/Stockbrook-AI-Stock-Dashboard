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


# --- stratification and the paired test -------------------------------
#
# These exist because of a real misreading. The effect of grounding was
# first assessed with an unpaired two-proportion test, which reported
# 1.8 sigma and was written up as "suggestive, not established". The data
# is paired — the same scenarios asked twice — and the paired test on the
# very same rows cleared significance comfortably. The wrong test did not
# just lose a little power; it changed the conclusion.


def met(grounded, ungrounded, **extra):
    return pair(grounded, ungrounded, criteria_passed=True, **extra)


def not_met(grounded, ungrounded, **extra):
    return pair(grounded, ungrounded, criteria_passed=False, **extra)


def test_strata_are_reported_separately():
    """The two strata behave nothing alike, and the pooled rate moves with
    the sampling mix rather than with grounding."""
    rows = [met("YES", "NO")] + [not_met("NO", "NO") for _ in range(9)]
    out = run(rows)
    assert out["strata"]["threshold_met"]["n"] == 1
    assert out["strata"]["threshold_not_met"]["n"] == 9
    assert out["strata"]["threshold_met"]["agreement_rate"] == 0.0
    assert out["strata"]["threshold_not_met"]["agreement_rate"] == 1.0
    # Pooled agreement here is 90%, which describes the mix, not the effect.
    assert out["agreement"]["rate"] == 0.9
    assert out["agreement"]["pooled_rate_depends_on_sampling"] is True


def test_paired_test_uses_only_discordant_pairs():
    """Agreement carries no information about the difference; adding more
    of it must not move the test statistic's basis."""
    base = [met("YES", "NO") for _ in range(9)] + [met("NO", "YES")]
    out = run(base)["strata"]["threshold_met"]
    assert (out["discordant_to_yes"], out["discordant_to_no"]) == (9, 1)
    padded = run(base + [met("YES", "YES") for _ in range(50)])
    padded = padded["strata"]["threshold_met"]
    assert (padded["discordant_to_yes"], padded["discordant_to_no"]) == (9, 1)


def test_paired_test_beats_the_unpaired_one_it_replaced():
    """Mirrors the real 53-scenario stratum: 42 vs 30 YES, discordant 13/1.

    An unpaired two-proportion test on these counts lands near 2.5 sigma
    and, on the smaller sample this was first read at, under 2. The paired
    test is the one that matches the design.
    """
    rows = ([met("YES", "YES") for _ in range(29)]
            + [met("YES", "NO") for _ in range(13)]
            + [met("NO", "YES")]
            + [met("NO", "NO") for _ in range(10)])
    out = run(rows)["strata"]["threshold_met"]
    assert out["n"] == 53
    assert (out["grounded_yes"], out["ungrounded_yes"]) == (42, 30)
    assert out["discordant_to_yes"] == 13 and out["discordant_to_no"] == 1
    assert out["z"] > 3.0


def test_no_discordant_pairs_reports_no_z_rather_than_dividing_by_zero():
    out = run([met("NO", "NO") for _ in range(20)])["strata"]["threshold_met"]
    assert out["effect_pp"] == 0.0
    assert "z" not in out
