"""FastAPI backend — bridges the React UI to core/ logic."""

from __future__ import annotations

from pathlib import Path
import sys
import os

# Load .env if present
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.screener import run_screen, ScreenedStock
from core.metrics import get_stock_metrics, get_market_context, get_price_history
from core.criteria import evaluate_criteria
from core.portfolio import compute_gain, get_portfolio_price_history, _lookup_price_on_date
from core.analysis import analyze_stock
from core.chat import chat as stock_chat
from core.auth import get_current_user, get_optional_user

# Local-dev escape hatch: the pre-auth file-based portfolio store is only
# reachable when this env var is set. In production every portfolio
# operation requires a valid Supabase token.
ALLOW_ANON_PORTFOLIO = os.getenv("ALLOW_ANON_PORTFOLIO") == "1"


def _require_user_or_dev(user_id: str | None) -> None:
    if user_id is None and not ALLOW_ANON_PORTFOLIO:
        raise HTTPException(status_code=401, detail="Not authenticated")

import math
from fastapi.responses import JSONResponse

app = FastAPI(title="Stockbrook API")

# ── CORS ────────────────────────────────────────────────────────────────────
# Restricted to our own frontends instead of "*". Auth is via Bearer tokens
# (not cookies), so this is defense-in-depth: it stops arbitrary origins from
# scripting the API and keeps browser errors readable only for our own app.
# Override/extend via env without a code change.
import re
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
# Matches the production Vercel domain (including preview deploys) + localhost.
# Both the pre-rebrand (stockwiz) and renamed (stockbrook) project slugs are
# allowed — Vercel 307s the old one to the new, but keep both to be safe.
_ORIGIN_REGEX = os.getenv(
    "ALLOWED_ORIGIN_REGEX",
    r"^https://stock(wiz|brook)-ai-stock-dashboard[\w-]*\.vercel\.app$|^http://localhost:\d+$",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _cors_origin(request) -> str | None:
    """The request's Origin if it's one we allow — so error/limit responses
    (which bypass the CORS middleware) stay readable by our own frontend."""
    origin = request.headers.get("origin", "")
    if not origin:
        return None
    if origin in ALLOWED_ORIGINS:
        return origin
    if _ORIGIN_REGEX and re.match(_ORIGIN_REGEX, origin):
        return origin
    return None


def _cors_headers(request) -> dict:
    o = _cors_origin(request)
    return {"Access-Control-Allow-Origin": o} if o else {}


# Unhandled errors: log the real cause server-side, return a generic message.
# Never echo str(exc) to the client — it can leak internal paths, DB errors,
# and stack context that help an attacker map the system.
import logging as _logging
_log = _logging.getLogger("stockbrook")

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    _log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers=_cors_headers(request),
    )


from core.credits import CreditsExhausted

@app.exception_handler(CreditsExhausted)
async def credits_exhausted_handler(request, exc):
    return JSONResponse(
        status_code=402,
        content={"detail": str(exc), "code": "credits_exhausted"},
        headers=_cors_headers(request),
    )


@app.on_event("startup")
async def prewarm():
    """Pre-fetch data for top watchlist stocks and start universe background fetcher."""
    import asyncio
    from core.criteria import get_watchlist
    from core.metrics import get_price_history, get_stock_metrics, get_market_context
    from core.universe_fetcher import start_background_fetcher

    def warm(symbol: str):
        try:
            get_stock_metrics(symbol)
            get_price_history(symbol, "1y")
            get_price_history(symbol, "6mo")
        except Exception:
            pass

    async def run():
        await asyncio.sleep(1)  # let server finish starting
        # Run all warm-up fetches on worker threads so the event loop stays
        # free to serve requests while the cache fills.
        await asyncio.to_thread(get_market_context)
        symbols = get_watchlist()[:8]  # top 8 only
        await asyncio.gather(*[asyncio.to_thread(warm, s) for s in symbols])
        # Start universe background fetcher in a thread
        start_background_fetcher()
        # (RAG needs no warm-up: FTS5 retrieval has no model to load, and
        # ticker indexing happens lazily in the background on first use.)

    asyncio.create_task(run())


def sanitize(obj):
    """Recursively replace nan/inf with None for JSON safety."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


@app.middleware("http")
async def sanitize_response(request, call_next):
    import json
    response = await call_next(request)
    # Only sanitize non-streaming JSON responses
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("application/json"):
        return response
    # Don't buffer SSE or large streams
    body = b""
    async for chunk in response.body_iterator:
        body += chunk
        if len(body) > 10 * 1024 * 1024:  # 10MB safety cap
            break
    try:
        data = json.loads(body)
        clean = sanitize(data)
        return JSONResponse(content=clean, status_code=response.status_code,
                            headers=dict(response.headers))
    except Exception:
        from starlette.responses import Response
        return Response(content=body, status_code=response.status_code,
                        headers=dict(response.headers), media_type="application/json")


# ── Rate limiting ─────────────────────────────────────────────────────────
# Lightweight in-memory sliding window per client IP. No new dependency (the
# earlier torch stack OOM'd this container, so we keep additions minimal) and
# no shared store needed on a single-worker deploy. AI/LLM paths get a tighter
# budget than ordinary reads. Generous by default so normal use never trips;
# tune via env. Skips CORS preflight (OPTIONS).
import time as _time
from collections import defaultdict, deque

_RL_WINDOW = 60.0
_RL_MAX = int(os.getenv("RATE_LIMIT_PER_MIN", "200"))
_RL_AI_MAX = int(os.getenv("RATE_LIMIT_AI_PER_MIN", "60"))
_RL_AI_PREFIXES = ("/api/analyze", "/api/predict", "/api/chat",
                   "/api/general-chat", "/api/universe/agent")
_RL_HITS: dict[str, deque] = defaultdict(deque)


@app.middleware("http")
async def rate_limit(request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    fwd = request.headers.get("x-forwarded-for", "")
    # Take the right-most hop: it's appended by our own trusted proxy (Railway's
    # edge), while every hop before it is client-supplied and can be spoofed to
    # mint a fresh IP (and thus a fresh rate-limit bucket) on every request.
    ip = fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "unknown")
    path = request.url.path
    is_ai = path.startswith(_RL_AI_PREFIXES)
    limit = _RL_AI_MAX if is_ai else _RL_MAX
    bucket = _RL_HITS[f"{ip}:{'ai' if is_ai else 'gen'}"]
    now = _time.monotonic()
    while bucket and now - bucket[0] > _RL_WINDOW:
        bucket.popleft()
    if len(bucket) >= limit:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Please slow down and try again shortly."},
            headers=_cors_headers(request),
        )
    bucket.append(now)
    # Opportunistic cleanup so the map can't grow unbounded across many IPs.
    if len(_RL_HITS) > 10_000:
        for k in [k for k, v in list(_RL_HITS.items()) if not v]:
            _RL_HITS.pop(k, None)
    return await call_next(request)


# ── Pydantic models ───────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatMessage]


# ── Screener ──────────────────────────────────────────────────────────────

@app.get("/api/screen")
def screen():
    results = run_screen()
    return [{"symbol": r.symbol, "metrics": r.metrics, "market": r.market,
             "buy_result": r.buy_result, "watch_result": r.watch_result,
             "classification": r.classification, "error": r.error} for r in results]


# ── Market ────────────────────────────────────────────────────────────────

@app.get("/api/market")
def market():
    return get_market_context()


# ── Price history ─────────────────────────────────────────────────────────

@app.get("/api/history/{symbol}")
def history(symbol: str, period: str = "1y"):
    try:
        return get_price_history(symbol, period)
    except Exception:
        _log.exception("history failed for %s", symbol)
        raise HTTPException(status_code=400, detail=f"Could not fetch price history for {symbol}.")


# ── News & Earnings ───────────────────────────────────────────────────────

@app.get("/api/news/{symbol}")
def stock_news(symbol: str):
    from concurrent.futures import ThreadPoolExecutor
    from core.news import get_recent_news, get_recent_earnings
    # Headlines and earnings are independent yfinance calls — fetch concurrently
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_news = pool.submit(get_recent_news, symbol)
        f_earnings = pool.submit(get_recent_earnings, symbol)
        return {
            "headlines": f_news.result(),
            "earnings": f_earnings.result(),
        }


# ── Metrics (fast, no LLM) ────────────────────────────────────────────────

@app.get("/api/metrics/{symbol}")
def stock_metrics(symbol: str):
    """Lightweight metrics endpoint so clients can render price/fundamentals
    immediately without waiting for the AI analysis to complete."""
    try:
        return get_stock_metrics(symbol)
    except Exception:
        _log.exception("metrics failed for %s", symbol)
        raise HTTPException(status_code=400, detail=f"Could not fetch metrics for {symbol}.")


# ── Analysis ──────────────────────────────────────────────────────────────

# NOTE: sync `def` endpoints run on FastAPI's worker threadpool, so slow
# yfinance/LLM work here no longer blocks the event loop (and every other
# in-flight request) the way an `async def` with blocking calls did.
@app.get("/api/analyze/{symbol}")
def analyze(symbol: str, action: str = "buy", gain_pct: float | None = None,
            user_id: str = Depends(get_current_user)):
    try:
        return analyze_stock(symbol, action, gain_pct=gain_pct, user_id=user_id)
    except CreditsExhausted:
        raise
    except Exception:
        _log.exception("analyze failed for %s", symbol)
        raise HTTPException(status_code=500, detail=f"Analysis failed for {symbol}. Please try again.")


# ── Prediction ────────────────────────────────────────────────────────────

@app.get("/api/predict/{symbol}")
def predict(symbol: str, user_id: str = Depends(get_current_user)):
    try:
        from core.prediction import predict_stock
        return predict_stock(symbol, user_id=user_id)
    except CreditsExhausted:
        raise
    except Exception:
        _log.exception("predict failed for %s", symbol)
        raise HTTPException(status_code=500, detail=f"Prediction failed for {symbol}. Please try again.")


# ── AI track record ───────────────────────────────────────────────────────

@app.get("/api/track-record")
def track_record():
    """
    Public, unauthenticated scoreboard of how the AI's buy/sell verdicts
    have actually performed against real price history vs SPY. No auth —
    this is meant to be an honest, checkable record, not a per-user stat.
    """
    from core.cache import fetch_through
    from core.track_record import compute_track_record
    # Resolution touches historical price data for every logged call, so
    # it's cached for an hour rather than recomputed on every page load.
    return fetch_through("track_record", 3600, compute_track_record, stale_ttl=21600)


# ── Chat — specific routes MUST come before wildcard /api/chat/{symbol} ──

from fastapi.responses import StreamingResponse

def _sse(text: str) -> str:
    import json
    return f"data: {json.dumps({'token': text})}\n\n"

def _stream_anthropic(system: str, messages: list[dict], max_tokens: int = 400,
                      user_id: str | None = None,
                      key_info: tuple[str, bool] | None = None):
    """Generator that yields SSE tokens from Anthropic streaming API.

    Endpoints must call resolve_api_key(user_id) BEFORE returning the
    StreamingResponse (and pass it as key_info) so an exhausted user gets a
    clean 402 instead of a broken stream.
    """
    import anthropic
    from core.credits import resolve_api_key, add_tokens_used
    api_key, metered = key_info if key_info else resolve_api_key(user_id)
    client = anthropic.Anthropic(api_key=api_key)
    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            yield _sse(text)
        if metered and user_id:
            try:
                usage = stream.get_final_message().usage
                add_tokens_used(user_id, usage.input_tokens + usage.output_tokens)
            except Exception:
                pass
    yield "data: [DONE]\n\n"


@app.post("/api/chat/general")
async def general_chat_endpoint(req: ChatRequest, user_id: str = Depends(get_current_user)):
    import asyncio
    from core.general_chat import build_system
    from core.credits import resolve_api_key
    key_info = resolve_api_key(user_id)  # raises 402 before the stream starts
    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    # build_system does blocking network I/O — keep it off the event loop
    system = await asyncio.to_thread(build_system, messages)
    return StreamingResponse(
        _stream_anthropic(system, messages, max_tokens=400,
                          user_id=user_id, key_info=key_info),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat/title")
async def generate_title(req: ChatRequest, user_id: str = Depends(get_current_user)):
    try:
        from core.credits import metered_create
        convo = "\n".join(f"{m.role}: {m.content[:200]}" for m in req.messages[:4])
        response = metered_create(
            user_id,
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{"role": "user", "content": f"Give a 4-word max title for this conversation. No quotes, no punctuation:\n{convo}"}],
        )
        return {"title": response.content[0].text.strip() if response.content else "Market Discussion"}
    except Exception:
        return {"title": "Market Discussion"}


@app.post("/api/chat/{symbol}")
async def chat_endpoint(symbol: str, req: ChatRequest,
                        user_id: str = Depends(get_current_user)):
    import asyncio
    from core.chat import build_system as build_stock_system
    from core.credits import resolve_api_key
    key_info = resolve_api_key(user_id)  # raises 402 before the stream starts
    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    # build_system fetches metrics/news over the network — keep it off the event loop
    system = await asyncio.to_thread(build_stock_system, symbol, messages, user_id=user_id)
    return StreamingResponse(
        _stream_anthropic(system, messages, max_tokens=400,
                          user_id=user_id, key_info=key_info),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Portfolio ─────────────────────────────────────────────────────────────

@app.get("/api/portfolio")
def portfolio(user_id: str | None = Depends(get_optional_user)):
    # Supabase when authenticated; the file store only exists for local dev
    _require_user_or_dev(user_id)
    from concurrent.futures import ThreadPoolExecutor
    from core.db import get_holdings as db_get_holdings
    from core.portfolio import get_holdings as file_get_holdings
    if user_id:
        holdings = db_get_holdings(user_id)
    else:
        holdings = file_get_holdings()
    market = get_market_context()
    def enrich_holding(h):
        symbol = h["symbol"]
        try:
            metrics = get_stock_metrics(symbol)
            current = metrics["close_price"]
            gain = compute_gain(h, current)
            sell_result = evaluate_criteria("sell", metrics, market,
                                            gain_pct=gain.get("gain_pct"),
                                            user_id=user_id)
            hist = get_portfolio_price_history(symbol, h["buy_date"])
        except Exception:
            metrics = None; current = None
            gain = {"gain_pct": None, "gain_abs": None, "total_value": None}
            sell_result = None; hist = []
        return {**h, "current_price": current,
                "gain_pct": gain.get("gain_pct"),
                "gain_abs": gain.get("gain_abs"),
                "total_value": gain.get("total_value"),
                "sell_result": sell_result,
                "metrics": metrics, "history": hist}

    # Network-bound market lookups used to run one holding at a time. Fetching
    # them concurrently makes first portfolio load scale with the slowest
    # symbol rather than the sum of every symbol's latency.
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(holdings)))) as pool:
        return list(pool.map(enrich_holding, holdings))


class AddHoldingRequest(BaseModel):
    symbol: str
    buy_date: str
    buy_price: float | None = None
    shares: float = 1.0
    notes: str = ""


@app.post("/api/portfolio")
def add_to_portfolio(req: AddHoldingRequest, user_id: str | None = Depends(get_optional_user)):
    _require_user_or_dev(user_id)
    from core.db import upsert_holding
    from core.portfolio import add_holding as file_add_holding
    buy_price = req.buy_price
    if buy_price is None:
        buy_price = _lookup_price_on_date(req.symbol, req.buy_date)
    if user_id:
        return upsert_holding(user_id, req.symbol, req.buy_date, buy_price, req.notes, req.shares)
    # Local dev only (ALLOW_ANON_PORTFOLIO=1): file-backed store
    return file_add_holding(req.symbol, req.buy_date, buy_price, req.notes, req.shares)


@app.delete("/api/portfolio/{symbol:path}")
def remove_from_portfolio(symbol: str, user_id: str | None = Depends(get_optional_user)):
    _require_user_or_dev(user_id)
    from core.db import delete_holding as db_delete
    from core.portfolio import remove_holding as file_remove
    # URL-decode in case the symbol contains encoded special chars
    from urllib.parse import unquote
    symbol = unquote(symbol)
    if user_id:
        if not db_delete(user_id, symbol):
            raise HTTPException(status_code=404, detail=f"{symbol} not found")
    else:
        if not file_remove(symbol):
            raise HTTPException(status_code=404, detail=f"{symbol} not found")
    return {"removed": symbol}


class SellHoldingRequest(BaseModel):
    sell_price: float
    sell_date: str | None = None  # defaults to today


# NOTE: /portfolio/sold must be defined BEFORE /portfolio/{symbol}/sell
# so FastAPI doesn't match the literal "sold" as a symbol path param.
@app.get("/api/portfolio/sold")
def get_sold_positions(user_id: str | None = Depends(get_optional_user)):
    """Return the user's completed trade history."""
    if not user_id:
        return []  # no history for unauthenticated users
    from core.db import get_sold_positions as db_get_sold
    return db_get_sold(user_id)


@app.post("/api/portfolio/{symbol}/sell")
def sell_holding(
    symbol: str,
    req: SellHoldingRequest,
    user_id: str | None = Depends(get_optional_user),
):
    """Record a sale, capture realized P&L, and remove the holding."""
    _require_user_or_dev(user_id)
    from urllib.parse import unquote
    from datetime import date
    from core.db import record_sale, get_holdings as db_get_holdings
    from core.portfolio import get_holding as file_get_holding, remove_holding as file_remove

    symbol = unquote(symbol).upper()
    sell_date = req.sell_date or date.today().isoformat()

    if user_id:
        holdings = db_get_holdings(user_id)
        holding = next((h for h in holdings if h["symbol"] == symbol), None)
        if not holding:
            # Already sold — check if a recent sale exists and return it
            from core.db import get_sold_positions as db_get_sold
            sold = db_get_sold(user_id)
            recent = next((s for s in sold if s["symbol"] == symbol), None)
            if recent:
                return recent  # idempotent: return the existing sale record
            raise HTTPException(status_code=404, detail=f"{symbol} not found in portfolio")
        return record_sale(
            user_id=user_id,
            symbol=symbol,
            sell_date=sell_date,
            sell_price=req.sell_price,
            shares=float(holding.get("shares") or 1),
            buy_price=holding.get("buy_price"),
            buy_date=holding.get("buy_date", ""),
        )
    else:
        from core.portfolio import get_holding as file_get_holding, remove_holding as file_remove
        holding = file_get_holding(symbol)
        if not holding:
            raise HTTPException(status_code=404, detail=f"{symbol} not found")
        file_remove(symbol)
        return {"symbol": symbol, "sell_price": req.sell_price, "sell_date": sell_date}


# ── Plaid (brokerage sync) ────────────────────────────────────────────────

@app.post("/api/plaid/link-token")
def plaid_link_token(user_id: str | None = Depends(get_optional_user)):
    """Create a Plaid Link token to initialize the Link flow."""
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from core.plaid_client import create_link_token
    try:
        token = create_link_token(user_id)
        return {"link_token": token}
    except Exception:
        _log.exception("plaid link-token creation failed for user %s", user_id)
        raise HTTPException(status_code=500, detail="Could not start the brokerage connection. Please try again.")


class PlaidExchangeRequest(BaseModel):
    public_token: str
    institution_name: str = ""


@app.post("/api/plaid/exchange")
def plaid_exchange(req: PlaidExchangeRequest, user_id: str | None = Depends(get_optional_user)):
    """Exchange public token for access token and store it."""
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from core.plaid_client import exchange_public_token, save_plaid_token
    try:
        result = exchange_public_token(req.public_token)
        access_token = result["access_token"]
        item_id = result["item_id"]
        save_plaid_token(user_id, access_token, req.institution_name, item_id)
        return {"connected": True, "institution": req.institution_name}
    except Exception:
        _log.exception("plaid token exchange failed for user %s", user_id)
        raise HTTPException(status_code=500, detail="Could not connect the brokerage account. Please try again.")


@app.get("/api/plaid/holdings")
def plaid_holdings(user_id: str | None = Depends(get_optional_user)):
    """Fetch real brokerage holdings from ALL connected Plaid accounts, merged."""
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from core.plaid_client import get_plaid_tokens, get_holdings
    connections = get_plaid_tokens(user_id)
    if not connections:
        return {"connected": False, "holdings": []}
    all_holdings: list[dict] = []
    errors: list[str] = []
    for conn in connections:
        try:
            h = get_holdings(conn["access_token"])
            # Tag each holding with the institution name; fall back to a short
            # unique identifier so multiple anonymous connections are distinguishable
            label = (conn.get("institution_name") or "").strip()
            if not label:
                # Use last 6 chars of item_id or connection id as a readable fallback
                fallback_id = (conn.get("item_id") or conn.get("id") or "")[-6:]
                label = f"Account …{fallback_id}" if fallback_id else "Unknown Account"
            for item in h:
                item["institution"] = label
            all_holdings.extend(h)
        except Exception as e:
            errors.append(f"{conn.get('institution_name', conn['id'])}: {e}")
    return {
        "connected": True,
        "holdings": all_holdings,
        "errors": errors if errors else None,
    }


@app.get("/api/plaid/status")
def plaid_status(user_id: str | None = Depends(get_optional_user)):
    """Return list of all connected Plaid accounts for the user."""
    try:
        if not user_id:
            return {"connected": False, "connections": []}
        from core.plaid_client import get_plaid_tokens
        connections = get_plaid_tokens(user_id)
        if connections:
            return {
                "connected": True,
                "connections": [
                    {
                        "id": c["id"],
                        "institution": c.get("institution_name", ""),
                        "updated_at": c.get("updated_at"),
                    }
                    for c in connections
                ],
            }
        return {"connected": False, "connections": []}
    except Exception:
        _log.exception("plaid status lookup failed for user %s", user_id)
        return {"connected": False, "connections": [], "error": "Could not fetch brokerage connection status."}


@app.delete("/api/plaid/disconnect/{connection_id}")
def plaid_disconnect(connection_id: str, remove_holdings: bool = False,
                     user_id: str | None = Depends(get_optional_user)):
    """Remove a specific Plaid connection by its UUID.
    If remove_holdings=true, also delete all portfolio holdings synced from that institution.
    """
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from core.plaid_client import delete_plaid_connection, get_plaid_tokens
    # Grab institution name before deleting so we can match holdings
    removed_count = 0
    if remove_holdings:
        connections = get_plaid_tokens(user_id)
        conn = next((c for c in connections if c["id"] == connection_id), None)
        if conn and conn.get("institution_name"):
            from core.db import delete_holdings_by_source
            removed_count = delete_holdings_by_source(user_id, conn["institution_name"])
    delete_plaid_connection(user_id, connection_id)
    return {"disconnected": connection_id, "holdings_removed": removed_count}


@app.delete("/api/plaid/disconnect")
def plaid_disconnect_all(user_id: str | None = Depends(get_optional_user)):
    """Remove ALL Plaid connections for user (legacy fallback)."""
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from core.plaid_client import delete_plaid_connection
    delete_plaid_connection(user_id)
    return {"disconnected": True}


# ── Portfolio source cleanup ──────────────────────────────────────────────

@app.delete("/api/portfolio/source/{source}")
def remove_holdings_by_source(source: str, user_id: str | None = Depends(get_optional_user)):
    """Delete all portfolio holdings synced from a given source (institution name).
    Used when a brokerage has already been disconnected but holdings remain.
    """
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from core.db import delete_holdings_by_source
    removed = delete_holdings_by_source(user_id, source)
    return {"source": source, "removed": removed}


# ── Symbol search (typeahead) ─────────────────────────────────────────────

@app.get("/api/search")
def search_symbols(q: str = ""):
    """
    Fast symbol/company search against the universe cache.
    Returns up to 10 matches ordered by market cap.
    """
    from core.universe_cache import _connect
    if not q or len(q.strip()) < 1:
        return []
    q = q.strip().upper()
    with _connect() as conn:
        rows = conn.execute("""
            SELECT symbol, sector, close_price, market_cap
            FROM universe_stocks
            WHERE fetch_error IS NULL
              AND close_price IS NOT NULL
              AND (
                UPPER(symbol) LIKE ? OR
                UPPER(symbol) = ?
              )
            ORDER BY market_cap DESC NULLS LAST
            LIMIT 10
        """, (f"{q}%", q)).fetchall()
    return [{"symbol": r["symbol"], "sector": r["sector"],
             "close_price": r["close_price"], "market_cap": r["market_cap"]}
            for r in rows]


# ── Financials ────────────────────────────────────────────────────────────

@app.get("/api/financials/{symbol}")
def financials(symbol: str):
    """Return quarterly revenue and net income for charting."""
    from core.cache import get as cache_get, set as cache_set
    import math
    key = f"financials:{symbol.upper()}"
    cached = cache_get(key, 3600)  # cache 1hr
    if cached:
        return cached
    try:
        import yfinance as yf
        from concurrent.futures import ThreadPoolExecutor
        t = yf.Ticker(symbol.strip().upper())
        # quarterly_financials and stock metrics are independent network
        # fetches — start both at once
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_fin = pool.submit(lambda: t.quarterly_financials)
            f_metrics = pool.submit(get_stock_metrics, symbol)
            fin = f_fin.result()
            try:
                metrics = f_metrics.result()
            except Exception:
                metrics = {}
        if fin is None or fin.empty:
            return {"quarters": [], "revenue_growth_yoy": None, "profit_margin": None}

        def safe_val(v):
            try:
                f = float(v)
                return None if (math.isnan(f) or math.isinf(f)) else round(f, 0)
            except Exception:
                return None

        quarters = []
        cols = list(fin.columns[:8])  # last 8 quarters max
        for col in reversed(cols):
            q_label = col.strftime("%Y Q%q") if hasattr(col, 'strftime') else str(col)[:7]
            rev = safe_val(fin.loc["Total Revenue", col]) if "Total Revenue" in fin.index else None
            ni  = safe_val(fin.loc["Net Income", col])    if "Net Income"    in fin.index else None
            quarters.append({"quarter": q_label, "revenue": rev, "net_income": ni})

        # YoY revenue growth (latest vs 4 quarters ago)
        rev_growth = None
        rev_vals = [q["revenue"] for q in quarters if q["revenue"] is not None]
        if len(rev_vals) >= 5:
            latest, year_ago = rev_vals[-1], rev_vals[-5]
            if year_ago and year_ago != 0:
                rev_growth = round((latest - year_ago) / abs(year_ago), 4)

        result = {
            "quarters": quarters,
            "revenue_growth_yoy": rev_growth,
            "profit_margin": metrics.get("profit_margin"),
        }
        cache_set(key, result)
        return result
    except Exception as e:
        return {"quarters": [], "revenue_growth_yoy": None, "profit_margin": None}


# ── Alerts ────────────────────────────────────────────────────────────────

@app.get("/api/alerts")
def list_alerts(user_id: str | None = Depends(get_optional_user)):
    if not user_id:
        return []
    from core.alerts import get_alerts
    return get_alerts(user_id)


class CreateAlertRequest(BaseModel):
    symbol: str
    alert_type: str
    threshold: float | None = None


@app.post("/api/alerts")
def create_alert_endpoint(req: CreateAlertRequest, user_id: str | None = Depends(get_optional_user)):
    if not user_id:
        raise HTTPException(status_code=401, detail="Must be logged in to create alerts")
    from core.alerts import create_alert
    return create_alert(user_id, req.symbol, req.alert_type, req.threshold)


@app.delete("/api/alerts/{alert_id}")
def delete_alert_endpoint(alert_id: str, user_id: str | None = Depends(get_optional_user)):
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from core.alerts import delete_alert
    if not delete_alert(user_id, alert_id):
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"deleted": alert_id}


class ToggleAlertRequest(BaseModel):
    enabled: bool


@app.patch("/api/alerts/{alert_id}")
def toggle_alert_endpoint(alert_id: str, req: ToggleAlertRequest,
                           user_id: str | None = Depends(get_optional_user)):
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from core.alerts import toggle_alert
    return toggle_alert(user_id, alert_id, req.enabled)


@app.post("/api/alerts/check")
def check_alerts_endpoint(user_id: str | None = Depends(get_optional_user)):
    """Evaluate all alerts and return which ones are currently firing."""
    if not user_id:
        return []
    from core.alerts import check_alerts
    return check_alerts(user_id)


# ── Criteria ──────────────────────────────────────────────────────────────

@app.get("/api/criteria")
def get_criteria(user_id: str | None = Depends(get_optional_user)):
    from core.criteria import load_criteria
    c = load_criteria(user_id)
    return {k: v for k, v in c.items() if k != "watchlist"}


@app.put("/api/criteria")
def update_criteria(body: dict, user_id: str | None = Depends(get_optional_user)):
    from core.db import save_user_criteria
    from core.criteria import _load_defaults
    if not user_id:
        raise HTTPException(status_code=401, detail="Must be logged in to save criteria")
    defaults = _load_defaults()
    current = {k: v for k, v in defaults.items() if k != "watchlist"}
    for mode in ("buy", "watch", "sell"):
        if mode in body:
            current[mode] = body[mode]
    save_user_criteria(user_id, current)
    return {"saved": True}


# ── User Profile ──────────────────────────────────────────────────────────

@app.get("/api/profile")
def get_profile(user_id: str | None = Depends(get_optional_user)):
    from core.db import get_user_profile
    from core.db import DEFAULT_PROFILE
    if not user_id:
        return dict(DEFAULT_PROFILE)
    return get_user_profile(user_id)


@app.put("/api/profile")
def update_profile(body: dict, user_id: str | None = Depends(get_optional_user)):
    from core.db import save_user_profile
    if not user_id:
        raise HTTPException(status_code=401, detail="Must be logged in to save profile")
    return save_user_profile(user_id, body)


# ── Universe ──────────────────────────────────────────────────────────────

@app.get("/api/universe/signals")
def universe_signals(limit: int = 60):
    """
    Evaluate buy/watch criteria against the top cached universe stocks.
    Returns classified results sorted buy → watch → none, limited to top candidates.
    """
    from core.universe_cache import query_universe
    from core.criteria import evaluate_criteria
    from core.metrics import get_market_context

    # Pull top stocks by market cap from the cache
    stocks = query_universe(order_by="market_cap DESC", limit=limit)
    market = get_market_context()

    results = []
    for s in stocks:
        buy_result = evaluate_criteria("buy", s, market)
        watch_result = evaluate_criteria("watch", s, market)

        if buy_result["passed"]:
            classification = "buy"
        elif watch_result["passed"]:
            classification = "watch"
        else:
            classification = "none"

        results.append({
            "symbol": s["symbol"],
            "classification": classification,
            "metrics": s,
            "buy_result": buy_result,
            "watch_result": watch_result,
        })

    # Sort: buy first, then watch, then none
    order = {"buy": 0, "watch": 1, "none": 2}
    results.sort(key=lambda x: (order[x["classification"]], -(x["metrics"].get("market_cap") or 0)))

    # Only return buy + watch to keep the panel clean
    return [r for r in results if r["classification"] != "none"]


@app.get("/api/universe/status")
def universe_status():
    """Return background fetch progress and cache stats."""
    from core.universe_fetcher import get_progress
    from core.universe_cache import get_cached_count, get_total_universe_size
    from core.universe_symbols import _read_universe_file
    progress = get_progress()
    universe = _read_universe_file()
    return {
        "cached": get_cached_count(),
        "total": get_total_universe_size(),
        "fetching": progress["running"],
        "fetched_this_cycle": progress["fetched"],
        "cycle_total": progress["total"],
        "last_run": progress["last_run"],
        # Where the symbol universe comes from — surfaced in the UI
        "universe_source": "NASDAQ · NYSE · AMEX listings" if universe.get("source") == "nasdaqtrader" else "curated list",
        "universe_updated": universe.get("updated"),
    }


@app.get("/api/universe/sectors")
def universe_sectors():
    """Return distinct sectors available in the cached universe."""
    from core.universe_cache import get_sectors
    return get_sectors()


class UniverseQueryRequest(BaseModel):
    sector: str | None = None
    max_forward_pe: float | None = None
    max_trailing_pe: float | None = None
    min_revenue_growth: float | None = None
    min_profit_margin: float | None = None
    min_earnings_growth: float | None = None
    near_52w_low_pct: float | None = None
    min_market_cap: float | None = None
    max_price: float | None = None
    min_price: float | None = None
    symbols: list[str] | None = None
    limit: int = 50
    order_by: str = "market_cap DESC"


@app.post("/api/universe/query")
def universe_query(req: UniverseQueryRequest):
    """Structured query against the cached universe."""
    from core.universe_cache import query_universe
    results = query_universe(
        sector=req.sector,
        max_forward_pe=req.max_forward_pe,
        max_trailing_pe=req.max_trailing_pe,
        min_revenue_growth=req.min_revenue_growth,
        min_profit_margin=req.min_profit_margin,
        near_52w_low_pct=req.near_52w_low_pct,
        min_earnings_growth=req.min_earnings_growth,
        min_market_cap=req.min_market_cap,
        max_price=req.max_price,
        min_price=req.min_price,
        symbols=req.symbols,
        limit=min(req.limit, 500),
        order_by=req.order_by,
    )
    return results


# ── AI credits ────────────────────────────────────────────────────────────

class ApiKeyRequest(BaseModel):
    api_key: str


@app.get("/api/credits")
def get_credits(user_id: str = Depends(get_current_user)):
    from core.credits import credits_status
    return credits_status(user_id)


@app.post("/api/credits/key")
def set_credits_key(req: ApiKeyRequest, user_id: str = Depends(get_current_user)):
    from core.credits import validate_api_key, set_user_api_key, credits_status
    key = req.api_key.strip()
    if not validate_api_key(key):
        raise HTTPException(status_code=400,
                            detail="That key doesn't look valid. It should start with sk-ant- "
                                   "and be active at console.anthropic.com.")
    set_user_api_key(user_id, key)
    return credits_status(user_id)


@app.delete("/api/credits/key")
def delete_credits_key(user_id: str = Depends(get_current_user)):
    from core.credits import delete_user_api_key, credits_status
    delete_user_api_key(user_id)
    return credits_status(user_id)


# ── Account ───────────────────────────────────────────────────────────────

@app.delete("/api/account")
def delete_account(user_id: str = Depends(get_current_user)):
    """Permanently delete the authenticated user and all their data."""
    from core.db import delete_user_account
    try:
        delete_user_account(user_id)
    except Exception:
        _log.exception("account deletion failed for user %s", user_id)
        raise HTTPException(status_code=500, detail="Could not delete the account. Please try again.")
    return {"deleted": True}


# ── Signup notification webhook (called by Supabase, not the app) ─────────
# Configure in Supabase → Database → Webhooks: fire on INSERT to auth.users,
# POST to {RAILWAY_URL}/api/internal/hooks/new-user, with a custom header
# "X-Webhook-Secret: <SIGNUP_WEBHOOK_SECRET>" matching the Railway env var.

class ValidateEmailRequest(BaseModel):
    email: str


@app.post("/api/auth/validate-email")
def validate_email(req: ValidateEmailRequest):
    """
    Pre-signup check: does this email's domain look real? Catches typos and
    fabricated domains without sending anything. Public endpoint — runs
    before the user has a session.
    """
    from core.email_validation import has_valid_mx
    valid, reason = has_valid_mx(req.email)
    return {"valid": valid, "reason": reason}


@app.post("/api/internal/hooks/new-user")
async def new_user_hook(request: Request):
    secret = os.environ.get("SIGNUP_WEBHOOK_SECRET", "").strip()
    if not secret or request.headers.get("x-webhook-secret") != secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    body = await request.json()
    record = body.get("record", {})
    email = record.get("email", "unknown")
    created_at = record.get("created_at")

    from core.notify import send_signup_notification
    send_signup_notification(email, created_at)
    return {"ok": True}


@app.post("/api/internal/notify-signin")
def notify_signin(user_id: str = Depends(get_current_user)):
    """Called by the client right after a successful sign-in."""
    from core.db import get_user_email
    from core.notify import send_signin_notification, NOTIFY_EMAIL
    email = get_user_email(user_id)
    if email and email.lower() != NOTIFY_EMAIL.lower():
        send_signin_notification(email)
    return {"ok": True}


class AgentFilterRequest(BaseModel):
    query: str
    messages: list[ChatMessage] = []


@app.post("/api/universe/agent")
async def universe_agent(req: AgentFilterRequest, user_id: str = Depends(get_current_user)):
    """
    Natural language → structured filter → ranked results + streamed AI summary.
    Returns SSE stream. First event contains the structured filter + raw results,
    then tokens stream for the summary.
    """
    import asyncio
    from core.universe_agent import run_agent_filter, build_summary_system
    from core.metrics import get_market_context
    from core.credits import resolve_api_key

    key_info = resolve_api_key(user_id)  # raises 402 before any AI work

    try:
        # Blocking LLM + DB work — run on a worker thread
        filters, results = await asyncio.to_thread(run_agent_filter, req.query, user_id)
    except CreditsExhausted:
        raise
    except Exception:
        _log.exception("universe agent filter failed for user %s", user_id)
        raise HTTPException(status_code=500, detail="Could not process that request. Please try again.")

    market = await asyncio.to_thread(get_market_context)

    # First SSE event: structured payload (filters + results list)
    import json as _json
    first_event = _json.dumps({
        "type": "results",
        "filters": filters,
        "results": sanitize(results[:50]),
        "total_matched": len(results),
    })

    def generate():
        yield f"data: {first_event}\n\n"
        # Stream the AI summary
        system = build_summary_system(filters, results, market)
        messages = [{"role": "user", "content": req.query}]
        yield from _stream_anthropic(system, messages, max_tokens=300,
                                     user_id=user_id, key_info=key_info)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.server:app", host="0.0.0.0", port=8000, reload=True)
