"""Behavioural eval for the analyze_stock prompt. Calls the real model.

Not a pytest test: it costs money and its results are statistical, so it
runs on demand rather than on every commit. Use it whenever the prompt in
`core/analysis._build_prompt` changes — the invariants in
`test_analysis_prompt.py` cover what the string must contain, this covers
what the model does with it.

It exists because a one-line prompt edit once destabilised a path that
already worked: a borderline buy went from a steady YES to 3 NO / 2 YES on
identical input, and a single sample would have shown nothing. Verdict
stability is therefore measured over repeated runs, not one.

    py -3 tests/eval_analysis_live.py            # ~9 calls, about $0.03
    py -3 tests/eval_analysis_live.py --runs 5   # tighter stability check

Needs ANTHROPIC_API_KEY. Exit status is non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Same .env convention as api/server.py, so this runs from a bare shell.
_env_file = _ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            if _v.strip():
                os.environ.setdefault(_k.strip(), _v.strip())

from core.analysis import _build_prompt, _parse_decision  # noqa: E402
from core.output_audit import unsourced_numbers  # noqa: E402

MODEL = "claude-haiku-4-5-20251001"
SYSTEM = ("You are a disciplined stock analysis assistant. "
          "Use only the provided data. Do not fabricate missing values.")

# Phrasings that deny the threshold. Only wrong when the threshold is met —
# that combination is what contradicted the checklist shown beside the
# verdict in the UI.
DENIAL = re.compile(
    r"insufficient\s+criteria|criteria\s+(?:are\s+|is\s+)?not\s+met|"
    r"criteria\s+(?:not|un)met|fails?\s+to\s+meet\s+the\s+criteria|"
    r"does\s+not\s+meet\s+the\s+(?:sell|buy)?\s*criteria|"
    r"only\s+\d+\s*(?:of|/)\s*\d+\s+criteria", re.IGNORECASE)


def rules(*passed: bool) -> list[dict]:
    names = ["near 52-week high", "trailing PE over 50", "revenue growth negative",
             "market bearish", "gain over 40%"]
    return [{"id": f"r{i}", "field": "revenue_growth", "description": names[i],
             "passed": p} for i, p in enumerate(passed)]


def criteria(details, min_required):
    met = sum(1 for d in details if d["passed"])
    return {"passed": met >= min_required, "rules_met": met,
            "rules_total": len(details), "min_required": min_required,
            "details": details}


NVDA = {"symbol": "NVDA", "date": "2026-08-06", "close_price": 182.4,
        "low_52_week": 86.6, "high_52_week": 195.0, "trailing_pe": 52.1,
        "forward_pe": 31.4, "revenue_growth": 0.85, "earnings_growth": 2.14,
        "profit_margin": 0.63, "operating_margin": 0.58,
        "distance_to_low_pct": 1.106, "distance_to_high_pct": 0.065}

AAPL = {"symbol": "AAPL", "date": "2026-08-06", "close_price": 214.3,
        "low_52_week": 169.2, "high_52_week": 260.1, "trailing_pe": 29.8,
        "forward_pe": 26.4, "revenue_growth": 0.09, "earnings_growth": 0.12,
        "profit_margin": 0.24, "operating_margin": 0.31,
        "distance_to_low_pct": 0.267, "distance_to_high_pct": 0.176}

BULL = {"market_trend": "bullish", "vix": 14.2}

# `expect_stable` marks a scenario whose verdict should be unanimous across
# runs. Not every scenario qualifies: a met sell threshold on a company with
# 85% revenue growth is a genuine disagreement between the mechanical
# screener and the model's judgment, and it splits legitimately. Demanding
# unanimity there would fail on every run and train the reader to ignore
# this report. Stability is asserted where a wobble would mean a prompt
# edit disturbed a path that was previously settled.
SCENARIOS = [
    # (name, symbol, action, metrics, criteria, gain_pct, met, expect_stable)
    ("sell/threshold-met, strong fundamentals", "NVDA", "sell", NVDA,
     criteria(rules(True, True, False, False, True), 2), 0.62, True, False),
    ("sell/threshold-not-met", "NVDA", "sell", NVDA,
     criteria(rules(True, False, False, False, False), 2), 0.05, False, True),
    ("buy/borderline at 4-of-5-need-4", "AAPL", "buy", AAPL,
     criteria(rules(True, True, True, True, False), 4), None, True, True),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3,
                    help="samples per scenario (stability needs >= 3)")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    import anthropic
    client = anthropic.Anthropic()

    failures: list[str] = []
    calls = 0

    for name, symbol, action, metrics, crit, gain, met, expect_stable in SCENARIOS:
        prompt = _build_prompt(symbol, action, crit, metrics, BULL,
                               gain_pct=gain)
        verdicts: Counter[str] = Counter()
        denials = 0
        malformed = 0
        invented: set[str] = set()

        for _ in range(args.runs):
            msg = client.messages.create(
                model=MODEL, max_tokens=300, system=SYSTEM,
                messages=[{"role": "user", "content": prompt}])
            calls += 1
            out = msg.content[0].text if msg.content else ""

            decision = _parse_decision(out)
            if decision is None or "Summary:" not in out:
                malformed += 1
            verdicts[decision or "UNPARSEABLE"] += 1
            if met and DENIAL.search(out):
                denials += 1
            invented |= set(unsourced_numbers(out, prompt))

        _, top_n = verdicts.most_common(1)[0]
        stable = top_n == args.runs
        note = ("stable" if stable else
                "UNSTABLE" if expect_stable else "split (expected)")
        print(f"\n{name}")
        print(f"  verdicts     {dict(verdicts)}  {note}")
        print(f"  format       {args.runs - malformed}/{args.runs} well-formed")
        if met:
            print(f"  false denial {denials}/{args.runs}")
        if invented:
            print(f"  unsourced numbers: {sorted(invented)[:8]}")

        if met and denials:
            failures.append(f"{name}: denied a met threshold "
                            f"{denials}/{args.runs} times")
        if malformed:
            failures.append(f"{name}: {malformed}/{args.runs} malformed")
        if expect_stable and not stable:
            failures.append(f"{name}: verdict unstable {dict(verdicts)} — a "
                            f"prompt change may have destabilised it")

    print(f"\n{'='*58}\n{calls} calls (~${calls * 0.0033:.2f})")
    for f in failures:
        print(f"  FAIL  {f}")
    if not failures:
        print("  all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
