"""
AI call tracking — builds an honest, public track record for the AI's
buy/sell verdicts.

Every time analyze_stock() gets a fresh YES/NO decision from Claude, it logs
one row here (log_call), once per symbol+action+day — repeated views of the
same stock the same day don't inflate or overwrite the recorded call.

When the scoreboard is requested, compute_track_record() looks back at
every logged call, and for each horizon (30/90/180 days) that has already
elapsed, fetches what the stock actually did afterward from historical
price history and compares it to SPY over the same window. Because that
comparison only uses data that already existed by the time each horizon
elapsed, it can't be quietly tuned after the fact — the whole point of a
track record is that it's unfakeable.

Scope: only calls where the AI actually recommended the action (decision
"YES") are scored — "when we said BUY, what happened" is the legible
question this answers. "Watch" analyses aren't logged; they aren't a
directional call in the same sense.

When Supabase isn't configured (local dev), calls fall back to a JSON file
so the flow is still testable end to end.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger("stockbrook")

HORIZONS: dict[str, int] = {"30d": 30, "90d": 90, "180d": 180}

# Postgres unique-violation code — the one insert failure log_call expects
# and silently accepts (same symbol+action already logged today). See
# postgrest.exceptions.APIError.code.
_DUPLICATE_KEY = "23505"

_LOCAL_PATH = Path(__file__).parent.parent / "data" / "ai_calls.json"
_local_lock = threading.Lock()


def _supabase():
    """Service-role client, or None when not configured (local dev)."""
    try:
        from core.db import get_client
        return get_client()
    except Exception:
        return None


def _local_read() -> dict:
    try:
        return json.loads(_LOCAL_PATH.read_text())
    except Exception:
        return {}


def _local_write(data: dict) -> None:
    _LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LOCAL_PATH.write_text(json.dumps(data))


# ── Logging ──────────────────────────────────────────────────────────────

def log_call(symbol: str, action: str, decision: str, price_at_call: float | None,
             spy_at_call: float | None, rules_met: int | None, rules_total: int | None) -> None:
    """Log one AI verdict. No-ops on a duplicate symbol+action+day (the
    Supabase table enforces this via a unique constraint; the local
    fallback checks explicitly)."""
    if price_at_call is None or action not in ("buy", "sell"):
        return
    today = datetime.now(timezone.utc).date().isoformat()
    row = {
        "symbol": symbol.upper(), "action": action, "decision": decision,
        "price_at_call": price_at_call, "spy_at_call": spy_at_call,
        "rules_met": rules_met, "rules_total": rules_total,
        "call_date": today,
    }
    sb = _supabase()
    if sb:
        try:
            sb.table("ai_calls").insert(row).execute()
        except Exception as exc:
            # Only a same-day duplicate for this symbol+action is expected
            # and silent (the unique constraint doing its job). Anything
            # else — most importantly the table not existing — must be
            # visible, or every future verdict disappears the same way this
            # one did for a month with nothing in the logs to show for it.
            if getattr(exc, "code", None) != _DUPLICATE_KEY:
                _log.exception("ai_calls insert failed for %s %s", symbol, action)
        return
    with _local_lock:
        data = _local_read()
        key = f"{row['symbol']}:{action}:{today}"
        if key not in data:
            data[key] = {**row, "created_at": datetime.now(timezone.utc).isoformat()}
            _local_write(data)


def _all_calls() -> list[dict]:
    sb = _supabase()
    if sb:
        try:
            res = (sb.table("ai_calls").select("*")
                   .order("created_at", desc=True).limit(3000).execute())
            return res.data or []
        except Exception:
            return []
    return sorted(_local_read().values(), key=lambda r: r.get("created_at", ""), reverse=True)


# ── Resolution ───────────────────────────────────────────────────────────

def _price_on_or_after(symbol: str, target: date) -> float | None:
    """Closing price on the first trading day on/after `target`, from
    cached history. None if that date is still in the future.

    The period ladder was written assuming `target` is always recent —
    true for a live call, whose horizons are at most 180 days past
    call_date. A backtest call_date can be years in the past, and the old
    ladder topped out at "2y": for a target older than that, EVERY point
    in the fetched history sits at or after it, so the loop below always
    returned the same earliest available price regardless of how much
    older the target actually was — silently making the 30d/90d/180d
    returns on an old backtest call identical when they should differ.
    Caught by that exact symptom on a real backtest row (AMZN sell
    2023-09-27: all three horizons resolved to 0.4844). "max" covers it.
    """
    today = datetime.now(timezone.utc).date()
    if target > today:
        return None
    from core.metrics import get_price_history
    days_out = (today - target).days
    period = ("max" if days_out > 700 else
             "2y" if days_out > 300 else "1y" if days_out > 25 else "3mo")
    try:
        hist = get_price_history(symbol, period)
    except Exception:
        return None
    if not hist:
        return None
    target_s = target.isoformat()
    for point in hist:
        if point["date"] >= target_s:
            return point["close"]
    return hist[-1]["close"]  # target predates available history — closest we have


def _resolve_row(call: dict) -> dict:
    """Attach return/alpha for every horizon that has already elapsed;
    horizons still in the future are simply left unresolved."""
    symbol = call["symbol"]
    call_date = date.fromisoformat(call["call_date"])
    price0 = call.get("price_at_call")
    spy0 = call.get("spy_at_call")
    resolved = dict(call)
    for label, days in HORIZONS.items():
        if price0 is None:
            continue
        price_n = _price_on_or_after(symbol, call_date + timedelta(days=days))
        if price_n is None:
            continue
        ret = (price_n - price0) / price0
        resolved[f"return_{label}"] = round(ret, 4)
        if spy0:
            spy_n = _price_on_or_after("SPY", call_date + timedelta(days=days))
            if spy_n:
                resolved[f"alpha_{label}"] = round(ret - (spy_n - spy0) / spy0, 4)
    return resolved


# ── Aggregation ──────────────────────────────────────────────────────────

def compute_track_record() -> dict:
    calls = [c for c in _all_calls() if c.get("decision") == "YES"]
    resolved = [_resolve_row(c) for c in calls]

    summary: dict[str, Any] = {}
    for label in HORIZONS:
        for action in ("buy", "sell"):
            rows = [r for r in resolved if r["action"] == action and f"return_{label}" in r]
            rets = [r[f"return_{label}"] for r in rows]
            alphas = [r[f"alpha_{label}"] for r in rows if f"alpha_{label}" in r]
            # A BUY call wins if price rose; a SELL call wins if price fell —
            # i.e. the recommendation would have helped, not hurt, if followed.
            wins = sum(1 for r in rets if (r > 0 if action == "buy" else r < 0))
            summary[f"{action}_{label}"] = {
                "count": len(rets),
                "avg_return": round(sum(rets) / len(rets), 4) if rets else None,
                "win_rate": round(wins / len(rets), 4) if rets else None,
                "avg_alpha_vs_spy": round(sum(alphas) / len(alphas), 4) if alphas else None,
            }

    return {
        "summary": summary,
        "total_calls_logged": len(calls),
        "recent_calls": resolved[:60],
    }
