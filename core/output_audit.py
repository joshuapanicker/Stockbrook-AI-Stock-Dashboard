"""
Checks on what the model actually wrote, independent of whether its
verdict later proved right.

A verdict can be correct about the future and still be untrustworthy — if
it cites numbers that were never in its prompt, it got there by luck or
by recall of training data, not by reading the company's filing. That's a
separate axis of quality from the track record, and the one a user is
implicitly relying on when the app says "grounded in real SEC filings."

Shared by tests/eval_analysis_live.py (3 hand-picked scenarios, run on
demand) and scripts/backtest_worker.py (every historical call, at scale),
so the two can't drift into measuring subtly different things.
"""

from __future__ import annotations

import re

_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# Below this, a number is almost always structural rather than a claim —
# rule counts ("2 of 5"), bullet numbering, a P/E of 8. Flagging those
# buries the real fabrications in noise.
_TRIVIAL_MAX = 10.0


def numbers(text: str) -> set[str]:
    """Numeric tokens as written."""
    return {f"{float(n):g}" for n in _NUMBER.findall(text)}


def sourced(prompt: str) -> set[str]:
    """Every figure the model may legitimately state, given the prompt.

    Ratios count as their percentage too: the prompt carries
    `"profit_margin":0.63` and the model quite correctly writes "63%
    margin". Without that, every run reports invented numbers that were
    never invented. This stays a heuristic — a model that divides one
    provided figure by another is deriving, not fabricating — so unmatched
    values are worth a human glance, not an automatic failure.
    """
    out: set[str] = set()
    for raw in _NUMBER.findall(prompt):
        val = float(raw)
        out |= {f"{val:g}", f"{val * 100:g}", f"{round(val * 100):g}"}
    return out


def unsourced_numbers(output: str, prompt: str) -> list[str]:
    """Figures in `output` that don't trace back to anything in `prompt`.

    Deliberately not called "hallucinations": a model dividing one
    provided figure by another (price ÷ 52-week high, say) produces a
    number that never literally appears in the prompt and is still
    perfectly honest work. Treat a non-empty result as "worth looking
    at", and the RATE across many calls as the signal — a model that
    stays near zero is reading its inputs; one that drifts upward is
    reaching for something else.
    """
    extra = numbers(output) - sourced(prompt)
    return sorted(n for n in extra if float(n) > _TRIVIAL_MAX)
