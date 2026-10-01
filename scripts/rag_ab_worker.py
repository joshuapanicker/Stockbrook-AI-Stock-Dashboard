"""
Paired A/B test of RAG grounding.

For each historical scenario: reconstruct it exactly as the backtest
worker does, build the SEC filing excerpt, then ask the model TWICE —
once with that excerpt in the prompt, once with it removed and nothing
else changed. Same company, same date, same metrics, same criteria text.

This is the experiment the main backtest can't run. There, the only
available comparison was grounded calls against ungrounded ones, and
grounding isn't randomly assigned: a call is ungrounded when no filing
existed for that date and company, which skews it older and toward
smaller and foreign firms. Pairing removes every one of those confounds,
because each scenario is its own control.

Scenarios with no filing are SKIPPED, not run: there is nothing to
withhold from arm B, so they carry no information about grounding and
would only cost money.

Everything shared with the main worker (universe tiers, call-date
windows, filing-excerpt construction) is imported from it rather than
copied, so the two can't drift into testing subtly different things.

    py -3 scripts/rag_ab_worker.py --limit 5 --dry-run
    py -3 scripts/rag_ab_worker.py --limit 100     # ~$0.66 (2 calls each)
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import os  # noqa: E402
_env_file = _ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            if _v.strip():
                os.environ.setdefault(_k.strip(), _v.strip())

from core.analysis import _build_prompt, _parse_decision  # noqa: E402
from core.criteria import evaluate_criteria  # noqa: E402
from core.output_audit import unsourced_numbers  # noqa: E402
from core.point_in_time import (  # noqa: E402
    price_on_or_before, reconstruct_market_context, reconstruct_metrics,
)
from core.rag_ab import (  # noqa: E402
    already_tried, log_pair, resolve_unresolved, schema_ready,
)

# Imported, never re-implemented: a second copy of the scenario or
# excerpt logic would mean the A/B tests something the backtest doesn't.
from backtest_worker import (  # noqa: E402
    MODEL, SYSTEM, _SELL_WINDOW_DAYS, _TIER_WEIGHTS, _filing_context_asof,
    _jsonable, _load_universe, _random_call_dates,
)

# Sell only. Buy produces no YES verdicts at this window (0 in 206 real
# calls — two of its five rules are near-permanently unreconstructable),
# so a buy arm would compare two identical streams of NO and learn
# nothing for twice the price.
_ACTION = "sell"


def run_pair(symbol: str, call_date: date, tier: str, market: dict) -> str:
    """Returns a short status line; never raises."""
    if already_tried(symbol, _ACTION, call_date):
        return "skip (already tried)"

    metrics = reconstruct_metrics(symbol, call_date)
    if metrics.get("close_price") is None:
        return "skip (no price data)"

    buy_price = price_on_or_before(symbol, call_date - timedelta(days=180))
    gain_pct = (round((metrics["close_price"] - buy_price) / buy_price, 4)
                if buy_price else None)

    criteria_result = evaluate_criteria(_ACTION, metrics, market, gain_pct=gain_pct)
    filing_ctx, filing_meta = _filing_context_asof(
        symbol, call_date, criteria_result, _ACTION)
    if not filing_ctx:
        # Nothing to withhold — this scenario can say nothing about
        # grounding, so don't pay for it.
        return "skip (no filing to withhold)"

    import anthropic
    client = anthropic.Anthropic()

    def ask(ctx: str) -> tuple[str | None, str, list[str]]:
        prompt = _build_prompt(symbol, _ACTION, criteria_result, metrics, market,
                               gain_pct=gain_pct, filing_ctx=ctx)
        msg = client.messages.create(
            model=MODEL, max_tokens=300, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}])
        text = msg.content[0].text if msg.content else ""
        return _parse_decision(text), text, unsourced_numbers(text, prompt)

    grounded, g_text, g_unsourced = ask(filing_ctx)
    ungrounded, u_text, u_unsourced = ask("")
    if grounded is None or ungrounded is None:
        return "skip (unparseable response)"

    ok = log_pair({
        "symbol": symbol.upper(), "action": _ACTION,
        "call_date": call_date.isoformat(), "universe_tier": tier,
        "rules_met": criteria_result.get("rules_met"),
        "rules_total": criteria_result.get("rules_total"),
        "criteria_passed": bool(criteria_result.get("passed")),
        "price_at_call": metrics["close_price"],
        "spy_at_call": market.get("spy_latest"),
        "criteria_inputs": _jsonable({"metrics": metrics, "market": market,
                                      "gain_pct": gain_pct}),
        "filing_form": filing_meta["form"] if filing_meta else None,
        "filing_date": filing_meta["date"] if filing_meta else None,
        "decision_grounded": grounded, "response_grounded": g_text,
        "unsourced_grounded": g_unsourced,
        "decision_ungrounded": ungrounded, "response_ungrounded": u_text,
        "unsourced_ungrounded": u_unsourced,
    })
    mark = "SAME" if grounded == ungrounded else f"FLIP {ungrounded}->{grounded}"
    return f"{'logged' if ok else 'LOG FAILED'}: {mark}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20,
                    help="scenarios (each costs two model calls)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run:
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY not set", file=sys.stderr)
            return 2
        if not schema_ready():
            print("ai_rag_ab is missing columns this worker writes — run "
                  "supabase_ai_rag_ab.sql", file=sys.stderr)
            return 3

    universe = _load_universe()
    random.seed()
    plan = []
    for _ in range(args.limit):
        tier = random.choices(list(_TIER_WEIGHTS),
                              weights=list(_TIER_WEIGHTS.values()))[0]
        plan.append((random.choice(universe[tier]),
                     _random_call_dates(1, _SELL_WINDOW_DAYS)[0], tier))

    print(f"{len(plan)} scenarios, 2 calls each "
          f"(~${len(plan) * 2 * 0.0033:.2f} if none skipped)\n")
    if args.dry_run:
        for symbol, call_date, tier in plan:
            print(f"  {symbol:<6} {call_date}  {tier}")
        return 0

    market_cache: dict[date, dict] = {}
    logged = flips = 0
    for i, (symbol, call_date, tier) in enumerate(plan, 1):
        try:
            if call_date not in market_cache:
                market_cache[call_date] = reconstruct_market_context(call_date)
            status = run_pair(symbol, call_date, tier, market_cache[call_date])
        except Exception as exc:
            status = f"ERROR: {exc}"
        if status.startswith("logged"):
            logged += 1
            if "FLIP" in status:
                flips += 1
        print(f"[{i}/{len(plan)}] {symbol:<6} {call_date} {tier:<5} {status}",
              flush=True)

    print(f"\n{logged}/{len(plan)} pairs logged, {flips} verdict flips")
    done, tried = resolve_unresolved(limit=400)
    if tried:
        print(f"resolved {done}/{tried} pending outcomes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
