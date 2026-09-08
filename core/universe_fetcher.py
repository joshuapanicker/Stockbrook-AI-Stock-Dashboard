"""
Background worker that fetches stock metrics for the full universe
and stores them in the SQLite universe cache.

Strategy (scaled for the ~6,000-symbol US-listed universe)
-----------------------------------------------------------
- Symbols are processed in priority order: the curated large-cap core first,
  then the full NASDAQ/NYSE/AMEX long tail — so the names users actually
  look at are populated within the first couple of minutes of a cold start.
- Work happens in chunks: one yf.download() batch price call per chunk
  (fast, threaded), then ticker.info fundamentals fetched CONCURRENTLY via a
  thread pool with a global rate throttle + 429-aware backoff.
- Everything user-facing reads from SQLite (WAL mode), so a running fetch
  never blocks or slows API requests.
- Each symbol is only re-fetched when its cached record is older than
  UNIVERSE_RECORD_TTL; errored symbols (delisted etc.) retry every 3 days.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import yfinance as yf

from core.universe_cache import (
    get_universe_symbols,
    get_stale_symbols,
    upsert_stock,
)
from core.universe_symbols import refresh_universe_symbols

# Tunables (env-overridable for the hosted deployment). Defaults are sized for
# a 512MB container: small batches keep each yf.download() DataFrame tiny and
# a small worker pool limits concurrent info-parse memory and thread-arena
# fragmentation. Bump these via env if running on a roomier host.
PRICE_BATCH_SIZE = int(os.getenv("UNIVERSE_PRICE_BATCH", "25"))    # symbols per yf.download()
INFO_WORKERS     = int(os.getenv("UNIVERSE_INFO_WORKERS", "3"))    # concurrent info fetches
REQ_INTERVAL     = float(os.getenv("UNIVERSE_REQ_INTERVAL", "0.12"))  # min secs between request starts
FULL_CYCLE_SLEEP = 3600     # re-check for stale data every hour


def _trim_memory() -> None:
    """Return freed heap to the OS after a chunk. pandas/yfinance churn leaves
    glibc holding freed pages; malloc_trim(0) hands them back so RSS actually
    falls between chunks instead of ratcheting up to the container limit.
    No-op off glibc (e.g. musl/macOS/Windows)."""
    gc.collect()
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        if hasattr(libc, "malloc_trim"):
            libc.malloc_trim(0)
    except Exception:
        pass


def _rss_mb() -> float | None:
    """Current resident set size in MB, read straight from the kernel
    (/proc/self/status) rather than resource.getrusage, whose ru_maxrss is a
    high-water mark that never falls — useless for seeing whether a cycle's
    malloc_trim actually gave memory back. Linux-only (Railway); returns
    None elsewhere so callers can skip logging quietly."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        return None
    return None


_stop_event = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()

# Global request pacing across worker threads
_throttle_lock = threading.Lock()
_last_request_start = 0.0

# When Yahoo rate-limits us, all workers back off until this timestamp
_backoff_until = 0.0

# Progress tracking (read by the API for status endpoint)
_progress = {"fetched": 0, "total": 0, "running": False, "last_run": None}


def _safe(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 4)
    except Exception:
        return None


def _throttle() -> None:
    """Pace request starts globally; honor any active rate-limit backoff."""
    global _last_request_start
    while True:
        with _throttle_lock:
            now = time.time()
            wait = max(_backoff_until - now, _last_request_start + REQ_INTERVAL - now)
            if wait <= 0:
                _last_request_start = now
                return
        if _stop_event.wait(min(wait, 2.0)):
            return


def _note_rate_limit(seconds: float = 30.0) -> None:
    global _backoff_until
    with _throttle_lock:
        _backoff_until = max(_backoff_until, time.time() + seconds)


# Yahoo periodically breaks yfinance's crumb/cookie handshake (an unresolved,
# recurring upstream issue — see ranaroussi/yfinance#2404). When it does,
# EVERY request 401s and yfinance retries internally with no backoff of its
# own, so without this check the fetcher hammers Yahoo at full speed for a
# whole cycle (~20 min observed), burning CPU/memory on retries that can
# never succeed. Treated like a rate limit, but with a much longer cooldown:
# this condition persists for a long time, unlike a brief 429 spike.
_AUTH_FAILURE_MARKERS = ("401", "unauthorized", "invalid crumb",
                         "unable to access this feature")


def _is_auth_or_rate_limited(msg: str) -> bool:
    low = msg.lower()
    return "429" in low or "too many requests" in low or any(m in low for m in _AUTH_FAILURE_MARKERS)


def _fetch_price_batch(symbols: list[str]) -> dict[str, dict]:
    """
    Use yf.download() to get 1-year OHLCV for a batch of symbols at once.
    Returns {symbol: {close, low_52w, high_52w}} — much faster than per-ticker calls.
    """
    _throttle()  # honor any active backoff before starting this batch
    if _stop_event.is_set():
        return {}
    results: dict[str, dict] = {}
    try:
        data = yf.download(
            symbols,
            period="1y",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )

        if data.empty:
            return results

        for symbol in symbols:
            try:
                if len(symbols) == 1:
                    # Single symbol: data IS the frame
                    df = data.dropna(subset=["Close"])
                else:
                    df = data[symbol].dropna(subset=["Close"])

                if df.empty:
                    continue

                close = _safe(float(df["Close"].iloc[-1]))
                low_52w = _safe(float(df["Low"].min()))
                high_52w = _safe(float(df["High"].max()))

                if close and low_52w:
                    dist_low = round((close - low_52w) / low_52w, 4)
                else:
                    dist_low = None

                if close and high_52w:
                    dist_high = round((high_52w - close) / high_52w, 4)
                else:
                    dist_high = None

                results[symbol] = {
                    "symbol": symbol,
                    "close_price": close,
                    "low_52_week": low_52w,
                    "high_52_week": high_52w,
                    "distance_to_low_pct": dist_low,
                    "distance_to_high_pct": dist_high,
                    "closer_to_52w_low": (
                        dist_low is not None
                        and dist_high is not None
                        and dist_low < dist_high
                    ),
                }
            except Exception:
                continue

    except Exception as e:
        if _is_auth_or_rate_limited(str(e)[:200]):
            _note_rate_limit(300.0)

    return results


def _fetch_one_info(symbol: str, price_data: dict[str, dict]) -> None:
    """Fetch fundamentals for one symbol (runs on a worker thread) and upsert."""
    if _stop_event.is_set():
        return
    _throttle()
    if _stop_event.is_set():
        return
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}

        base = price_data.get(symbol, {"symbol": symbol})

        # If price data was missing, try fast_info
        if not base.get("close_price"):
            fi = ticker.fast_info
            price = _safe(fi.get("lastPrice") or fi.get("regularMarketPrice"))
            if price:
                base["close_price"] = price

        metrics = {
            **base,
            "symbol": symbol,
            "trailing_pe":      _safe(info.get("trailingPE")),
            "forward_pe":       _safe(info.get("forwardPE")),
            "profit_margin":    _safe(info.get("profitMargins")),
            "operating_margin": _safe(info.get("operatingMargins")),
            "revenue_growth":   _safe(info.get("revenueGrowth")),
            "earnings_growth":  _safe(info.get("earningsGrowth")),
            "market_cap":       _safe(info.get("marketCap")),
            "sector":           info.get("sector"),
            "industry":         info.get("industry"),
        }

        upsert_stock(metrics, error=None)

    except Exception as e:
        msg = str(e)[:200]
        if _is_auth_or_rate_limited(msg):
            _note_rate_limit(300.0)
        # Mark symbol as errored but don't break the cycle
        upsert_stock({"symbol": symbol}, error=msg)

    with _lock:
        _progress["fetched"] += 1


def run_fetch_cycle(symbols: list[str] | None = None, force: bool = False) -> None:
    """
    Fetch data for stale (or all forced) symbols in the universe.
    Runs synchronously — call from a background thread.
    """
    # Keep the symbol universe itself fresh (weekly listing-directory refresh)
    try:
        refresh_universe_symbols()
    except Exception:
        pass

    if symbols is None:
        symbols = get_stale_symbols() if not force else get_universe_symbols()

    if not symbols:
        return

    with _lock:
        _progress["running"] = True
        _progress["total"] = len(symbols)
        _progress["fetched"] = 0

    start_rss = _rss_mb()
    print(f"[universe-fetcher] cycle start: {len(symbols)} symbols, RSS={start_rss} MB", flush=True)

    try:
        # Process chunk by chunk in priority order so high-interest symbols
        # land in the cache first: batch price download, then parallel info.
        with ThreadPoolExecutor(max_workers=INFO_WORKERS, thread_name_prefix="universe-info") as pool:
            for i in range(0, len(symbols), PRICE_BATCH_SIZE):
                if _stop_event.is_set():
                    break
                chunk = symbols[i: i + PRICE_BATCH_SIZE]
                price_data = _fetch_price_batch(chunk)

                futures = [pool.submit(_fetch_one_info, s, price_data) for s in chunk]
                for f in as_completed(futures):
                    if _stop_event.is_set():
                        break

                # Release this chunk's price DataFrame + info blobs and hand the
                # freed pages back to the OS before starting the next batch, so
                # sustained RSS stays flat instead of climbing to the OOM line.
                del price_data, futures
                _trim_memory()

    finally:
        with _lock:
            _progress["running"] = False
            _progress["last_run"] = time.time()
        end_rss = _rss_mb()
        delta_str = ""
        if start_rss is not None and end_rss is not None:
            delta_str = f" (delta={end_rss - start_rss:+.1f} MB)"
        print(f"[universe-fetcher] cycle end: RSS={end_rss} MB{delta_str}", flush=True)


def _worker() -> None:
    """Main background thread loop."""
    # Initial fetch on startup
    run_fetch_cycle()

    # Periodic refresh
    while not _stop_event.is_set():
        _stop_event.wait(FULL_CYCLE_SLEEP)
        if not _stop_event.is_set():
            run_fetch_cycle()


def start_background_fetcher() -> None:
    """Launch the background fetch thread (idempotent).

    Global kill switch: set UNIVERSE_DISABLED=1 to skip the background fetcher
    entirely (mirrors RAG_DISABLED). On a memory-starved container this is the
    immediate pressure-relief lever — the screener then serves whatever the
    universe cache already holds and fills lazily via normal per-symbol reads,
    at the cost of full-universe coverage until re-enabled.
    """
    if os.getenv("UNIVERSE_DISABLED") == "1":
        return
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_worker, daemon=True, name="universe-fetcher")
    _thread.start()


def stop_background_fetcher() -> None:
    _stop_event.set()


def get_progress() -> dict:
    with _lock:
        return dict(_progress)
