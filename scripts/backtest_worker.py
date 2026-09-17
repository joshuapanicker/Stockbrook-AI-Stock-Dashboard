"""
Backtest worker — generates AI verdicts on reconstructed historical dates
and scores them against real, already-elapsed price history.

This is the credibility system: `ai_calls` (core/track_record.py) only
grows as fast as real usage does, which is honest but currently produces
almost nothing. This generates a much larger sample by asking the same
question the app asks live, grounded in data reconstructed to look exactly
like it did on a real past date (core/point_in_time.py, core/filings.py's
`as_of` parameter) — and because that date has already passed, every
horizon (30/90/180 days out) can be scored immediately instead of waited
on.

Deliberately does NOT call analyze_stock() or log to the live `ai_calls`
table or `distill_log` — same reasoning scripts/backfill_distill.py
already documents for the live ledger (a synthetic run must never look
like real usage), extended here to distillation too: these prompts carry
reconstructed, occasionally-approximate fundamentals (see
core/point_in_time.py's documented gaps), and mixing that into training
data would reintroduce a subtler version of the v1 gain_pct units bug.
Results go to a separate table, `ai_calls_backtest`, surfaced separately.

Designed to be re-run indefinitely without duplicating work: every
(symbol, action, call_date) combination is checked against what's already
in the table before spending an API call, so this is meant to be invoked
on a schedule that just keeps extending the sample — see
docs/rag-and-model-roadmap.md for the current cadence and cost.

Usage:
    py -3 scripts/backtest_worker.py --limit 10 --dry-run   # no writes, no API calls
    py -3 scripts/backtest_worker.py --limit 50
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Same .env convention as api/server.py / tests/eval_analysis_live.py.
import os  # noqa: E402
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            if _v.strip():
                os.environ.setdefault(_k.strip(), _v.strip())

from core.analysis import _build_prompt, _build_retrieval_query, _parse_decision  # noqa: E402
from core.backtest_ledger import already_tried, log_backtest_call, schema_ready  # noqa: E402
from core.criteria import evaluate_criteria  # noqa: E402
from core.filings import extract_section, get_recent_filings, _strip_html, _throttled_get  # noqa: E402
from core.output_audit import unsourced_numbers  # noqa: E402
from core.point_in_time import (  # noqa: E402
    price_on_or_before, reconstruct_market_context, reconstruct_metrics,
)
from core.rag_index import _chunk_text, _section_label  # noqa: E402

MODEL = "claude-haiku-4-5-20251001"
SYSTEM = ("You are a disciplined stock analysis assistant. "
          "Use only the provided data. Do not fabricate missing values.")

# All five buy-criteria and sell-criteria fields, so a data gap is recorded
# even when it's a field the app's default criteria don't currently use —
# criteria are per-user and can reference any of these.
_ALL_FIELDS = ["trailing_pe", "forward_pe", "revenue_growth", "earnings_growth",
              "profit_margin", "operating_margin", "distance_to_low_pct",
              "distance_to_high_pct"]

# Both windows require call_date old enough that every horizon (30/90/180
# days out) has already elapsed, i.e. at least 185 days back — which
# creates a real tension for buy scenarios: a 25-call validation batch at
# this exact window found profit_margin missing in 25/25 calls and
# revenue_growth in 20/25 (core/point_in_time.py has the measured
# boundary — it's a few months, not the ~12 originally assumed). Sell
# only loses revenue_growth the same way and keeps the other four rules
# live, so it's the more complete signal.
_BUY_WINDOW_DAYS = (185, 360)
_SELL_WINDOW_DAYS = (185, 365 * 4)

# Buy needs 4 of its 5 rules to pass (core.criteria's default), and two of
# them are close to unconditionally False here: forward_pe is NEVER
# reconstructable (0/225 in the first real batches) and profit_margin is
# reconstructable in only ~2% of rows at this window. Reaching 4/5 needs
# that rare profit_margin case, a positive revenue_growth (available in
# ~19%), AND both always-available rules favorable, all at once — not
# literally impossible, but rare enough that a real 114-call batch of buy
# scenarios produced zero YES verdicts, confirmed by the math rather than
# assumed from the small sample alone. Every buy call at this rate is
# mostly testing whether reconstructed data clears a threshold it almost
# never can, not the AI's judgment — near-pure wasted spend. Weighted
# down hard rather than dropped to zero, so the rare qualifying case (and
# any future improvement in fundamentals depth) still gets sampled.
_ACTION_WEIGHTS = {"buy": 0.1, "sell": 0.9}

# Sampled from the app's own universe (data/universe.json, tracked in git so
# this runs anywhere), in two tiers drawn 50/50 so each gets a usable sample:
#
#   core   — the ~530 curated large caps the app fetches first
#   broad  — every other US-listed common stock, ~5,200 names
#
# The first ~800 backtest calls used a fixed list of 30 mega-caps. Those are
# large today BECAUSE they went up, which tilts every sell backtest toward
# looking wrong. Recording the tier per call lets the analysis check whether
# a finding holds outside that group instead of averaging the bias away.
#
# This narrows survivorship bias; it doesn't remove it. The listing is of
# stocks trading TODAY — names delisted since (bankruptcies, buyouts) are
# absent, and yfinance can't price most of them anyway. The broad tier also
# includes some non-operating listings (closed-end funds, shells) the symbol
# filter lets through; they tend to show up as heavy data_gaps.
_TIER_WEIGHTS = {"core": 0.5, "broad": 0.5}


def _load_universe() -> dict[str, list[str]]:
    path = Path(__file__).resolve().parent.parent / "data" / "universe.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    core = list(dict.fromkeys(data.get("core_symbols") or []))
    core_set = set(core)
    broad = [s for s in dict.fromkeys(data.get("symbols") or []) if s not in core_set]
    return {"core": core, "broad": broad}


def _random_call_dates(n: int, window: tuple[int, int]) -> list[date]:
    today = date.today()
    lo, hi = window
    return [today - timedelta(days=random.randint(lo, hi)) for _ in range(n)]


def _filing_context_asof(symbol: str, as_of: date, criteria_result: dict,
                         action: str) -> tuple[str, dict | None]:
    """Grounding block built fresh from the filing that existed on
    `as_of`, scored in-memory rather than through the persistent FTS
    index — this never serves a live request, so there's no reason to
    write historical filings into the same database real users' queries
    run against."""
    filings = get_recent_filings(symbol, as_of=as_of.isoformat())
    if not filings:
        return "", None

    query = _build_retrieval_query(symbol, action, criteria_result)
    query_words = {w.lower() for w in query.replace(",", " ").split() if len(w) > 3}

    best_chunk, best_score, best_meta = None, -1, None
    for filing in filings:
        try:
            text = _strip_html(_throttled_get(filing.url).text)
        except Exception:
            continue
        for section in ("risk_factors", "mda"):
            extracted = extract_section(text, filing.form, section)
            if not extracted:
                continue
            for chunk in _chunk_text(extracted)[:20]:
                words = {w.lower() for w in chunk.split()}
                score = len(query_words & words)
                if score > best_score:
                    best_chunk, best_score, best_meta = chunk, score, {
                        "form": filing.form, "date": filing.filing_date,
                        "section": _section_label(section),
                    }
    if not best_chunk:
        return "", None
    block = (f"Recent SEC filing excerpts (context — not exhaustive):\n"
            f"- [{best_meta['form']} filed {best_meta['date']}, {best_meta['section']}] {best_chunk}")
    return block, best_meta


def run_one(symbol: str, action: str, call_date: date, market: dict,
            tier: str) -> str:
    """Returns a short status string for logging; never raises — a bad
    ticker/date combination must not stop the batch."""
    if already_tried(symbol, action, call_date):
        return "skip (already tried)"

    metrics = reconstruct_metrics(symbol, call_date)
    if metrics.get("close_price") is None:
        return "skip (no price data)"

    gain_pct = None
    if action == "sell":
        # Grounded in a real historical price move, not a picked number:
        # what an investor's actual gain would be if they'd bought
        # ~180 days before this call date.
        buy_price = price_on_or_before(symbol, call_date - timedelta(days=180))
        if buy_price and metrics["close_price"]:
            gain_pct = round((metrics["close_price"] - buy_price) / buy_price, 4)

    criteria_result = evaluate_criteria(action, metrics, market, gain_pct=gain_pct)
    filing_ctx, filing_meta = _filing_context_asof(symbol, call_date, criteria_result, action)

    data_gaps = [f for f in _ALL_FIELDS if metrics.get(f) is None]

    prompt = _build_prompt(symbol, action, criteria_result, metrics, market,
                           gain_pct=gain_pct, filing_ctx=filing_ctx)

    import anthropic
    client = anthropic.Anthropic()
    message = client.messages.create(
        model=MODEL, max_tokens=300, system=SYSTEM,
        messages=[{"role": "user", "content": prompt}])
    text = message.content[0].text if message.content else ""
    decision = _parse_decision(text)
    if decision is None:
        return "skip (unparseable response)"

    unsourced = unsourced_numbers(text, prompt)

    ok = log_backtest_call(
        symbol, action, decision, metrics["close_price"], market.get("spy_latest"),
        criteria_result.get("rules_met"), criteria_result.get("rules_total"),
        call_date, data_gaps,
        filing_form=filing_meta["form"] if filing_meta else None,
        filing_date=filing_meta["date"] if filing_meta else None,
        # What the rules concluded on their own, before Claude saw anything —
        # the arm the AI has to beat to be worth its cost.
        criteria_passed=bool(criteria_result.get("passed")),
        unsourced=unsourced,
        response_text=text,
        universe_tier=tier,
    )
    flag = f" unsourced:{unsourced}" if unsourced else ""
    return (f"{'logged' if ok else 'LOG FAILED'}: {decision} "
           f"(rules:{'PASS' if criteria_result.get('passed') else 'fail'}, "
           f"gaps: {len(data_gaps)}){flag}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would run, skip the Claude call and the write")
    args = ap.parse_args()

    if not args.dry_run and not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    if not args.dry_run and not schema_ready():
        # Checked before any spend: a batch against a table missing a column
        # would pay for every Claude call and then fail every insert.
        print("ai_calls_backtest is missing columns this worker writes - run "
              "the latest migration in supabase_ai_calls_backtest.sql",
              file=sys.stderr)
        return 3

    universe = _load_universe()
    random.seed()  # varies call to call, by design — this extends over time
    plan = []
    for _ in range(args.limit):
        action = random.choices(list(_ACTION_WEIGHTS), weights=list(_ACTION_WEIGHTS.values()))[0]
        tier = random.choices(list(_TIER_WEIGHTS), weights=list(_TIER_WEIGHTS.values()))[0]
        symbol = random.choice(universe[tier])
        window = _BUY_WINDOW_DAYS if action == "buy" else _SELL_WINDOW_DAYS
        call_date = _random_call_dates(1, window)[0]
        plan.append((symbol, action, call_date, tier))

    print(f"{len(plan)} candidates (universe: {len(universe['core'])} core, "
          f"{len(universe['broad'])} broad)\n")
    if args.dry_run:
        for symbol, action, call_date, tier in plan:
            print(f"  {symbol:<6} {action:<4} {call_date}  {tier}")
        return 0

    market_cache: dict[date, dict] = {}
    logged = 0
    for i, (symbol, action, call_date, tier) in enumerate(plan, 1):
        if call_date not in market_cache:
            market_cache[call_date] = reconstruct_market_context(call_date)
        try:
            status = run_one(symbol, action, call_date, market_cache[call_date], tier)
        except Exception as exc:
            status = f"ERROR: {exc}"
        if status.startswith("logged"):
            logged += 1
        print(f"[{i}/{len(plan)}] {symbol:<6} {action:<4} {call_date} {tier:<5} {status}", flush=True)

    print(f"\n{logged}/{len(plan)} new calls logged "
         f"(~${logged * 0.0033:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
