"""Scoring and output-audit logic for the backtest ledger.

Two things here are easy to get silently, expensively wrong:

  1. Direction. A sell call wins when the stock FALLS. The per-row
     `return_*`/`alpha_*` fields stay factual (what the stock did), so
     any aggregate that reports "how well did the calls do" has to flip
     the sign for sells. Get it backwards and a wall of failed sell calls
     reads as a strong track record — the live UI's colouring has exactly
     this bug today, which is what prompted these tests.

  2. Attribution. `ai_yes` vs `rules_passed` vs `all_calls` only mean
     something relative to each other; mixing up which rows land in which
     arm would make the AI look like it earned a result the free
     deterministic screener produced on its own.

    py -3 -m pytest tests/test_backtest_scoring.py
"""

from __future__ import annotations

import pytest

from core.backtest_ledger import _score
from core.output_audit import unsourced_numbers


def row(action: str, ret: float, alpha: float | None = None, **extra) -> dict:
    r = {"action": action, "return_30d": ret, **extra}
    if alpha is not None:
        r["alpha_30d"] = alpha
    return r


# ── direction ───────────────────────────────────────────────────────────

def test_buy_wins_when_the_stock_rises():
    rows = [row("buy", 0.10), row("buy", 0.05), row("buy", -0.20)]
    result = _score(rows, "buy", "30d")
    assert result["win_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert result["avg_return"] == pytest.approx((0.10 + 0.05 - 0.20) / 3, abs=1e-4)


def test_sell_wins_when_the_stock_falls():
    """Same raw numbers as the buy case, opposite verdict — so the win
    rate must invert, not repeat."""
    rows = [row("sell", 0.10), row("sell", 0.05), row("sell", -0.20)]
    result = _score(rows, "sell", "30d")
    assert result["win_rate"] == pytest.approx(1 / 3, abs=1e-4)


def test_sell_avg_return_is_reported_in_the_calls_favour():
    """A sell on a stock that then dropped 20% is a GOOD call, and must
    aggregate as positive — reporting the raw -0.20 would make the best
    possible sell call look like the worst."""
    result = _score([row("sell", -0.20)], "sell", "30d")
    assert result["avg_return"] == pytest.approx(0.20)


def test_sell_alpha_flips_too():
    """The bug that motivated this: a sell whose stock beat SPY by 14%
    is a failed call. Unflipped it reports +0.14 and reads as success."""
    result = _score([row("sell", 0.20, alpha=0.14)], "sell", "30d")
    assert result["avg_alpha_vs_spy"] == pytest.approx(-0.14)


def test_buy_alpha_is_not_flipped():
    result = _score([row("buy", 0.20, alpha=0.14)], "buy", "30d")
    assert result["avg_alpha_vs_spy"] == pytest.approx(0.14)


# ── empty / partial data ────────────────────────────────────────────────

def test_unresolved_rows_are_excluded_not_counted_as_zero():
    """A horizon that hasn't elapsed has no return key at all. Counting
    it as 0.0 would quietly drag every average toward nothing."""
    rows = [row("buy", 0.10), {"action": "buy"}]  # second has no return_30d
    result = _score(rows, "buy", "30d")
    assert result["count"] == 1
    assert result["avg_return"] == pytest.approx(0.10)


def test_no_rows_returns_nulls_not_a_crash():
    result = _score([], "buy", "30d")
    assert result == {"count": 0, "avg_return": None, "win_rate": None,
                      "avg_alpha_vs_spy": None}


def test_missing_alpha_does_not_block_return_scoring():
    result = _score([row("buy", 0.10)], "buy", "30d")
    assert result["avg_return"] == pytest.approx(0.10)
    assert result["avg_alpha_vs_spy"] is None


# ── output audit ────────────────────────────────────────────────────────

def test_number_present_in_prompt_is_not_flagged():
    assert unsourced_numbers("revenue grew to 57006 million", "revenue 57006") == []


def test_ratio_written_as_a_percentage_is_not_flagged():
    """The prompt carries 0.63; writing "63% margin" is correct, not
    invented — flagging it would bury real fabrications in noise."""
    assert unsourced_numbers("margin of 63%", '"profit_margin":0.63') == []


def test_number_absent_from_prompt_is_flagged():
    assert unsourced_numbers("revenue was 99999", "revenue 57006") == ["99999"]


def test_small_numbers_are_ignored_as_structural():
    """"2 of 5 criteria", bullet numbering, a P/E of 8 — not claims."""
    assert unsourced_numbers("2 of 5 rules, point 3", "nothing here") == []


def test_price_rounded_to_cents_is_not_flagged():
    """The real false positive this check produced: the prompt carries
    276.957352118569 and the model writes the price to the cent. That's
    correct, readable behaviour, not invention."""
    prompt = '"high_52_week":276.957352118569'
    assert unsourced_numbers("52-week high of 276.96", prompt) == []


def test_percentage_rounded_to_one_decimal_is_not_flagged():
    """Same failure in the ratio case: 0.42413 rendered as "42.4%"."""
    assert unsourced_numbers("42.4% below the high", '"distance":0.42413') == []


def test_rounding_tolerance_does_not_swallow_a_real_invention():
    """The tolerance must follow the precision the model wrote, not wave
    through anything nearby — 280 is not a rounding of 276.96."""
    prompt = '"high_52_week":276.957352118569'
    assert unsourced_numbers("high of 280", prompt) == ["280"]


# ── universe tiers ──────────────────────────────────────────────────────

def test_universe_tiers_are_disjoint_and_cover_everything():
    """A symbol counted in both tiers would put the same stock in both
    arms of the comparison; one in neither would never be sampled."""
    import json
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "scripts"))
    from backtest_worker import _load_universe

    tiers = _load_universe()
    core, broad = set(tiers["core"]), set(tiers["broad"])
    assert core and broad
    assert not core & broad
    listed = set(json.loads((root / "data" / "universe.json").read_text())["symbols"])
    assert (core | broad) >= listed


# ── stored resolution ───────────────────────────────────────────────────

def test_stored_resolution_promotes_saved_returns():
    """Reads must use the stored outcome, not refetch prices. Only
    non-null horizons get keys, matching what live resolution produced —
    _score treats a missing key as "not resolved yet"."""
    from core.backtest_ledger import _stored_resolution
    row = {"action": "sell", "symbol": "X", "return_30d": "0.12",
           "alpha_30d": None, "return_90d": "-0.05"}
    out = _stored_resolution(row)
    assert out["return_30d"] == pytest.approx(0.12)   # numeric, not str
    assert out["return_90d"] == pytest.approx(-0.05)
    assert "alpha_30d" not in out                      # null stays absent
    assert out["symbol"] == "X"


def test_stored_resolution_feeds_score_unchanged():
    """The whole point: stored values must score identically to
    freshly-computed ones."""
    from core.backtest_ledger import _stored_resolution
    stored = [_stored_resolution({"action": "sell", "return_30d": "-0.20"}),
              _stored_resolution({"action": "sell", "return_30d": "0.10"})]
    result = _score(stored, "sell", "30d")
    assert result["count"] == 2
    assert result["win_rate"] == pytest.approx(0.5, abs=1e-4)
    assert result["avg_return"] == pytest.approx(0.05, abs=1e-4)


def test_unresolved_row_is_not_counted_as_a_zero_return():
    """A row awaiting resolution has no return key at all. Treating it as
    0.0 would drag every average toward nothing."""
    from core.backtest_ledger import _stored_resolution
    rows = [_stored_resolution({"action": "sell", "return_30d": "-0.20"}),
            _stored_resolution({"action": "sell"})]
    assert _score(rows, "sell", "30d")["count"] == 1


# ── default summary excludes the biased slice ───────────────────────────

def test_default_summary_excludes_legacy30():
    """legacy30 is survivorship-biased and drags the headline figure.
    A reader reaching for the obvious key must not land on it."""
    from core.backtest_ledger import _EXCLUDED_FROM_DEFAULT, _summarize
    rows = ([{"action": "sell", "decision": "YES", "universe_tier": "core",
              "return_30d": -0.10}] * 2
            + [{"action": "sell", "decision": "YES", "universe_tier": "legacy30",
                "return_30d": 0.50}] * 8)
    kept = [r for r in rows
            if (r.get("universe_tier") or "unknown") not in _EXCLUDED_FROM_DEFAULT]
    result = _summarize(kept)["sell_30d"]["ai_yes"]
    assert result["count"] == 2          # the 8 legacy rows are gone
    assert result["win_rate"] == 1.0     # not dragged to 0.2 by them


def test_a_new_tier_counts_toward_the_default_automatically():
    """Denylist, not allowlist: a tier added later must appear in the
    headline rather than silently vanishing from it."""
    from core.backtest_ledger import _EXCLUDED_FROM_DEFAULT
    assert "smallcap_2027" not in _EXCLUDED_FROM_DEFAULT
    assert "legacy30" in _EXCLUDED_FROM_DEFAULT
