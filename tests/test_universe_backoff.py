"""Backoff when the data provider refuses us.

These exist because of a production outage on 2026-11-11. Yahoo broke
yfinance's crumb handshake; every request came back 401. The fetcher had
backoff logic for exactly that case and it never fired, because
`yf.download()` does not raise on those 401s — it prints them and returns
an empty frame, and `ticker.info` returns {}. Every `except` clause stayed
cold, so the fetcher kept requesting at ~8/second for hours and starved
the API process until it stopped answering requests entirely.

The fix detects failure by outcome rather than by exception. The point of
these tests is that an empty "success" must trip the breaker.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from core import universe_fetcher as uf


@pytest.fixture(autouse=True)
def _reset_breaker():
    """Module-level counters leak between tests otherwise.

    `_throttle` is stubbed out throughout: it genuinely sleeps for the
    cooldown these tests are trying to trigger, so leaving it live makes
    the suite hang for minutes. That it hangs at all is the behaviour
    under test — it just belongs in assertions, not in wall-clock.
    """
    with uf._throttle_lock:
        uf._consecutive_failures = 0
        uf._backoff_until = 0.0
    with patch.object(uf, "_throttle", lambda: None):
        yield
    with uf._throttle_lock:
        uf._consecutive_failures = 0
        uf._backoff_until = 0.0


def _backoff_active() -> bool:
    with uf._throttle_lock:
        return uf._backoff_until > 0.0


def test_empty_download_trips_backoff_without_any_exception():
    """The regression. yfinance raises nothing; the breaker must still trip."""
    with patch.object(uf.yf, "download", return_value=pd.DataFrame()):
        for _ in range(uf._FAILURE_TRIP):
            assert uf._fetch_price_batch(["AAPL"]) == {}
    assert _backoff_active(), (
        "an empty frame is how the 401 failure actually arrives — if this "
        "does not back off, the fetcher spins against a dead endpoint")


def test_one_empty_batch_does_not_trip():
    """A single dead symbol is normal and must not stall the whole cycle."""
    with patch.object(uf.yf, "download", return_value=pd.DataFrame()):
        uf._fetch_price_batch(["DEADTICKER"])
    assert not _backoff_active()


def test_success_clears_the_streak():
    """A recovered provider must not stay one failure away from a cooldown."""
    with patch.object(uf.yf, "download", return_value=pd.DataFrame()):
        for _ in range(uf._FAILURE_TRIP - 1):
            uf._fetch_price_batch(["AAPL"])
    uf._note_success()
    with patch.object(uf.yf, "download", return_value=pd.DataFrame()):
        uf._fetch_price_batch(["AAPL"])
    assert not _backoff_active()


def test_cooldown_escalates_with_a_longer_streak():
    """A persistent outage should cost a few requests an hour, not thousands."""
    with patch.object(uf.yf, "download", return_value=pd.DataFrame()):
        for _ in range(uf._FAILURE_TRIP):
            uf._fetch_price_batch(["AAPL"])
    with uf._throttle_lock:
        first = uf._backoff_until
    with patch.object(uf.yf, "download", return_value=pd.DataFrame()):
        for _ in range(uf._FAILURE_TRIP * 3):
            uf._fetch_price_batch(["AAPL"])
    with uf._throttle_lock:
        later = uf._backoff_until
    assert later > first


class _EmptyTicker:
    """What yfinance hands back when the crumb handshake is refused."""
    info: dict = {}

    @property
    def fast_info(self):
        return {}


def test_empty_info_is_recorded_as_an_error_not_a_clean_row():
    """Writing all-None metrics with error=None both poisons the cache and
    hides the outage from the status endpoint."""
    written = {}

    def _capture(metrics, error=None):
        written["metrics"] = metrics
        written["error"] = error

    with patch.object(uf.yf, "Ticker", return_value=_EmptyTicker()), \
            patch.object(uf, "upsert_stock", _capture), \
            patch.object(uf, "_throttle", lambda: None):
        uf._fetch_one_info("AAPL", {})

    assert written["error"] is not None
    assert "trailing_pe" not in written["metrics"]


def test_empty_info_counts_toward_the_breaker():
    with patch.object(uf.yf, "Ticker", return_value=_EmptyTicker()), \
            patch.object(uf, "upsert_stock", lambda *a, **k: None), \
            patch.object(uf, "_throttle", lambda: None):
        for _ in range(uf._FAILURE_TRIP):
            uf._fetch_one_info("AAPL", {})
    assert _backoff_active()


# --- recovery from a poisoned crumb -----------------------------------
#
# Backing off is necessary but not sufficient. yfinance caches the crumb
# token and returns it forever once set, and it assigns the response body
# to that cache BEFORE checking whether the response was a 429 — so a
# rate-limited crumb fetch leaves the literal string "Too Many Requests"
# installed as the crumb, and every later request 401s for the life of the
# process. Present in 1.2.2 and 1.8.0 alike. Without the reset below, a
# cooldown just expires into the same failure.


def test_reset_clears_a_poisoned_crumb():
    from yfinance.data import YfData
    ydata = YfData()
    ydata._crumb = "Too Many Requests"
    ydata._cookie = "stale"

    assert "cleared" in uf._reset_provider_session()
    assert ydata._crumb is None, (
        "a cached crumb survives for the life of the process, so failing to "
        "clear it means the backoff expires straight back into 401s")
    assert ydata._cookie is None


def test_tripping_the_breaker_also_clears_the_crumb():
    """The two halves have to fire together: cooling down without clearing
    the crumb recovers nothing."""
    from yfinance.data import YfData
    ydata = YfData()
    ydata._crumb = "Too Many Requests"

    with patch.object(uf.yf, "download", return_value=pd.DataFrame()):
        for _ in range(uf._FAILURE_TRIP):
            uf._fetch_price_batch(["AAPL"])

    assert _backoff_active()
    assert ydata._crumb is None


def test_reset_survives_yfinance_internals_changing():
    """Recovery must never raise, however the library is shaped."""
    with patch.dict("sys.modules", {"yfinance.data": None}):
        assert isinstance(uf._reset_provider_session(), str)
