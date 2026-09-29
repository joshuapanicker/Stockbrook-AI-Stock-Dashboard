"""Replaying alternative rule sets against stored backtest history.

The trap this guards: a variant scored against only the rows that happen
to carry inputs, compared to a base computed over ALL rows, would look
better or worse purely from sample mismatch. Both arms must come from the
same scenario set.
"""

from __future__ import annotations

import pytest

from core.rule_lab import _passes, score_variant


def scenario(ret, *, pe=None, dist_high=None, trend="bullish", gain=None):
    return {
        "action": "sell",
        "return_180d": ret,
        "_inputs": {"metrics": {"trailing_pe": pe, "distance_to_high_pct": dist_high},
                    "market": {"market_trend": trend},
                    "gain_pct": gain},
    }


RULE_PE_OVER_50 = {"id": "pe", "field": "trailing_pe", "operator": "gt", "value": 50,
                   "description": "PE over 50"}
RULE_NEAR_HIGH = {"id": "hi", "field": "distance_to_high_pct", "operator": "lt",
                  "value": 0.10, "description": "within 10% of high"}


def test_a_rule_fires_only_on_matching_scenarios():
    passed, met = _passes(scenario(0.1, pe=60)["_inputs"], [RULE_PE_OVER_50], 1)
    assert passed and met == 1
    passed, met = _passes(scenario(0.1, pe=10)["_inputs"], [RULE_PE_OVER_50], 1)
    assert not passed and met == 0


def test_missing_field_does_not_pass():
    """A None metric must fail its rule, never be treated as satisfying
    it — the same convention the live criteria engine uses."""
    passed, _ = _passes(scenario(0.1, pe=None)["_inputs"], [RULE_PE_OVER_50], 1)
    assert not passed


def test_gain_pct_is_visible_to_rules():
    rule = {"id": "g", "field": "gain_pct", "operator": "gt", "value": 0.4,
            "description": "up 40%"}
    assert _passes(scenario(0.1, gain=0.5)["_inputs"], [rule], 1)[0]
    assert not _passes(scenario(0.1, gain=0.1)["_inputs"], [rule], 1)[0]


def test_variant_is_scored_against_the_same_pool_it_drew_from():
    """picked and base must come from one scenario set. A variant that
    looks good only because its base was computed over different rows has
    discovered nothing."""
    scenarios = [scenario(-0.30, pe=60),   # fired, fell -> a win for a sell
                 scenario(-0.20, pe=60),   # fired, fell -> win
                 scenario(0.40, pe=10),    # didn't fire, rose
                 scenario(0.50, pe=10)]    # didn't fire, rose
    result = score_variant(scenarios, [RULE_PE_OVER_50], 1, "sell", "180d")
    assert result["picked"]["count"] == 2
    assert result["picked"]["win_rate"] == 1.0      # both sells fell
    assert result["base"]["count"] == 4             # all four, not just picks
    assert result["base"]["win_rate"] == pytest.approx(0.5, abs=1e-4)
    assert result["fire_rate"] == pytest.approx(0.5)


def test_a_rule_that_fires_on_everything_matches_its_base():
    """The null result a variant search must be able to recognise."""
    always = {"id": "a", "field": "market_trend", "operator": "eq",
              "value": "bullish", "description": "always"}
    scenarios = [scenario(-0.1), scenario(0.2), scenario(-0.3)]
    result = score_variant(scenarios, [always], 1, "sell", "180d")
    assert result["fire_rate"] == 1.0
    assert result["picked"]["win_rate"] == result["base"]["win_rate"]


def test_unresolved_scenarios_do_not_count_as_losses():
    scenarios = [scenario(-0.30, pe=60), {"action": "sell", "_inputs":
                 {"metrics": {"trailing_pe": 60}, "market": {}, "gain_pct": None}}]
    result = score_variant(scenarios, [RULE_PE_OVER_50], 1, "sell", "180d")
    assert result["picked"]["count"] == 1
