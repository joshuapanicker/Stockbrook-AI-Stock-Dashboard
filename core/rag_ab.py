"""
Storage and analysis for the paired RAG A/B test (`ai_rag_ab`).

One row = one historical scenario asked twice: once with the SEC filing
excerpt in the prompt, once with it removed and nothing else changed.

This exists because the main backtest cannot answer whether RAG helps.
There, the only available comparison was grounded calls against
ungrounded ones — and grounding isn't randomly assigned. A call is
ungrounded when no filing existed for that date and company, which skews
it older and toward smaller and foreign firms, so the comparison is
tangled up with date and company type. Pairing removes that entirely:
each scenario is its own control.

The primary question is well-powered and cheap: does the filing change
the verdict at all? If the two arms agree on nearly everything, RAG is
inert for decisions regardless of how good retrieval looks in isolation.
Only where they disagree does outcome quality become answerable, and
those disagreements are the scarce resource — hence pairing rather than
two independent samples, which would need far more calls for the same
power.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from core.backtest_ledger import _score, _stored_resolution
from core.track_record import HORIZONS, _resolve_row

_log = logging.getLogger("stockbrook")

_DUPLICATE_KEY = "23505"
_MAX_RESOLVE_ATTEMPTS = 3
_RESOLVED_FIELDS = tuple(f"{k}_{h}" for h in HORIZONS for k in ("return", "alpha"))

_REQUIRED_COLUMNS = ("decision_grounded", "decision_ungrounded",
                     "response_grounded", "response_ungrounded",
                     "unsourced_grounded", "unsourced_ungrounded",
                     "criteria_inputs", "resolved_at", "resolve_attempts")


def _supabase():
    try:
        from core.db import get_client
        return get_client()
    except Exception:
        return None


def schema_ready() -> bool:
    """Checked before a batch spends anything: a missing column fails only
    at insert, after both Claude calls are already paid for."""
    sb = _supabase()
    if not sb:
        return False
    try:
        sb.table("ai_rag_ab").select(",".join(_REQUIRED_COLUMNS)).limit(1).execute()
        return True
    except Exception:
        _log.exception("ai_rag_ab schema check failed")
        return False


def already_tried(symbol: str, action: str, call_date: date) -> bool:
    sb = _supabase()
    if not sb:
        return False
    try:
        res = (sb.table("ai_rag_ab").select("id")
               .eq("symbol", symbol.upper()).eq("action", action)
               .eq("call_date", call_date.isoformat()).limit(1).execute())
        return bool(res.data)
    except Exception:
        _log.exception("ai_rag_ab existence check failed for %s %s %s",
                       symbol, action, call_date)
        return False


def log_pair(row: dict) -> bool:
    """Insert one scenario with both arms' verdicts."""
    sb = _supabase()
    if not sb:
        _log.error("ai_rag_ab insert skipped: no Supabase client")
        return False
    try:
        sb.table("ai_rag_ab").insert(row).execute()
        return True
    except Exception as exc:
        if getattr(exc, "code", None) == _DUPLICATE_KEY:
            return True
        _log.exception("ai_rag_ab insert failed for %s", row.get("symbol"))
        return False


def _all_pairs() -> list[dict]:
    sb = _supabase()
    if not sb:
        return []
    rows: list[dict] = []
    page = 1000
    try:
        while True:
            res = (sb.table("ai_rag_ab").select("*").order("id", desc=True)
                   .range(len(rows), len(rows) + page - 1).execute())
            batch = res.data or []
            rows.extend(batch)
            if len(batch) < page:
                return rows
    except Exception:
        _log.exception("ai_rag_ab read failed")
        return []


def resolve_unresolved(limit: int = 200) -> tuple[int, int]:
    """Score each scenario's realised outcome once. The outcome belongs to
    the scenario, not the arm, so one set of returns serves both."""
    sb = _supabase()
    if not sb:
        return (0, 0)
    try:
        rows = (sb.table("ai_rag_ab").select("*")
                .is_("resolved_at", "null")
                .lt("resolve_attempts", _MAX_RESOLVE_ATTEMPTS)
                .order("id").limit(limit).execute()).data or []
    except Exception:
        _log.exception("ai_rag_ab unresolved fetch failed")
        return (0, 0)

    resolved = 0
    for row in rows:
        try:
            filled = _resolve_row(row)
        except Exception:
            _log.exception("resolution failed for id=%s", row.get("id"))
            filled = {}
        patch: dict[str, Any] = {f: filled.get(f) for f in _RESOLVED_FIELDS}
        patch["resolve_attempts"] = (row.get("resolve_attempts") or 0) + 1
        if any(patch[f] is not None for f in _RESOLVED_FIELDS):
            patch["resolved_at"] = datetime.now(timezone.utc).isoformat()
            resolved += 1
        try:
            sb.table("ai_rag_ab").update(patch).eq("id", row["id"]).execute()
        except Exception:
            _log.exception("resolution write failed for id=%s", row.get("id"))
    return (resolved, len(rows))


def compute_rag_ab() -> dict:
    """What the filing excerpt actually changed.

    `agreement` is the headline and the best-powered figure: if both arms
    reach the same verdict on nearly every scenario, the filing is not
    influencing decisions, whatever retrieval quality looks like measured
    on its own.

    `disagreements` is where outcome quality becomes answerable, and only
    there — on identical scenarios, so the comparison is clean. Expect it
    to be the scarce number; read it with its own n, not the total.
    """
    rows = [r for r in _all_pairs()
            if r.get("decision_grounded") and r.get("decision_ungrounded")]
    if not rows:
        return {"pairs": 0, "is_ab": True}

    agree = [r for r in rows
             if r["decision_grounded"] == r["decision_ungrounded"]]
    disagree = [r for r in rows if r not in agree]

    def arm(subset: list[dict], field: str, action: str, horizon: str) -> dict:
        yes = [_stored_resolution(r) for r in subset
               if r[field] == "YES" and r["action"] == action]
        return _score(yes, action, horizon)

    summary: dict[str, Any] = {}
    for horizon in HORIZONS:
        summary[f"sell_{horizon}"] = {
            "grounded_yes": arm(rows, "decision_grounded", "sell", horizon),
            "ungrounded_yes": arm(rows, "decision_ungrounded", "sell", horizon),
            "all_scenarios": _score(
                [_stored_resolution(r) for r in rows if r["action"] == "sell"],
                "sell", horizon),
        }

    flipped_to_yes = sum(1 for r in disagree
                         if r["decision_grounded"] == "YES")
    def flag_rate(field: str) -> float | None:
        seen = [r for r in rows if r.get(field) is not None]
        return round(sum(1 for r in seen if r[field]) / len(seen), 4) if seen else None

    return {
        "pairs": len(rows),
        "agreement": {
            "same_verdict": len(agree),
            "rate": round(len(agree) / len(rows), 4),
            "disagreed": len(disagree),
            "filing_flipped_to_yes": flipped_to_yes,
            "filing_flipped_to_no": len(disagree) - flipped_to_yes,
        },
        "summary": summary,
        "unsourced_rate": {
            "grounded": flag_rate("unsourced_grounded"),
            "ungrounded": flag_rate("unsourced_ungrounded"),
        },
        "unresolved_pairs": sum(1 for r in rows if r.get("resolved_at") is None),
        "is_ab": True,
    }
