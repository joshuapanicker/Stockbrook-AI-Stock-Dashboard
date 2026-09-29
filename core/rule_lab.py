"""
Score alternative screening rules against the backtest history, offline.

Why this exists: the backtest measured two things and found the same
answer for both. The AI's verdicts don't beat the deterministic screener,
and the screener's own picks only match the base rate of the stocks it
sampled from. So the rules are what's worth experimenting on — they
decide what a user is shown, and nothing has ever tested them against an
alternative.

Rule evaluation is deterministic, so a variant costs nothing: no model
call, no network. Each stored call carries `criteria_inputs` (exactly
what evaluate_criteria saw) and its realised outcome, which makes
"what would THESE rules have picked, and how did those picks do" a pure
function of data already in the table.

Only rows written after criteria_inputs existed can be replayed — older
ones are skipped rather than silently counted as non-matches, which would
understate every variant equally and invisibly.

    from core.rule_lab import score_variant, load_scenarios
    scenarios = load_scenarios("sell")
    score_variant(scenarios, rules, min_required=2, horizon="180d")
"""

from __future__ import annotations

from typing import Any

from core.backtest_ledger import _all_backtest_calls, _score, _stored_resolution
from core.criteria import _evaluate_rule

# The slice kept out of headline reporting is kept out here too: tuning
# rules against a survivorship-biased sample would fit them to the bias.
_EXCLUDED_TIERS = ("legacy30",)


def load_scenarios(action: str, include_excluded_tiers: bool = False) -> list[dict]:
    """Replayable historical scenarios for one action.

    Each is the stored inputs plus the stored outcome, so a variant can be
    scored without refetching anything.
    """
    out: list[dict] = []
    for row in _all_backtest_calls():
        if row.get("action") != action:
            continue
        if not include_excluded_tiers and \
                (row.get("universe_tier") or "unknown") in _EXCLUDED_TIERS:
            continue
        inputs = row.get("criteria_inputs")
        if not inputs:
            continue  # predates the column — skipped, never counted as a miss
        resolved = _stored_resolution(row)
        resolved["_inputs"] = inputs
        out.append(resolved)
    return out


def _passes(inputs: dict, rules: list[dict], min_required: int) -> tuple[bool, int]:
    """Would this rule set have fired on this scenario?

    Mirrors evaluate_criteria's own combination of metrics, market and
    gain_pct, and reuses its rule evaluator rather than reimplementing
    the operators — a second implementation would drift from the one
    that actually runs in the app.
    """
    combined: dict[str, Any] = {**(inputs.get("metrics") or {}),
                                **(inputs.get("market") or {})}
    if inputs.get("gain_pct") is not None:
        combined["gain_pct"] = inputs["gain_pct"]
    met = sum(1 for rule in rules if _evaluate_rule(rule, combined))
    return met >= min_required, met


def score_variant(scenarios: list[dict], rules: list[dict], min_required: int,
                  action: str, horizon: str = "180d") -> dict:
    """How the picks this rule set would have made actually turned out.

    `base` is every scenario considered, not just the ones that fired —
    a variant is only interesting insofar as its picks beat the pool it
    drew them from. A rule that fires on everything matches the base rate
    by construction and has discovered nothing.
    """
    picked = [s for s in scenarios if _passes(s["_inputs"], rules, min_required)[0]]
    return {
        "picked": _score(picked, action, horizon),
        "base": _score(scenarios, action, horizon),
        "fire_rate": round(len(picked) / len(scenarios), 4) if scenarios else None,
        "replayable_scenarios": len(scenarios),
    }
