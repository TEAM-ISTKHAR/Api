"""
main.py
--------
HellAPI v5.0 — Commercial YouTube Stream API

Plans:
  free   →   50 req/day,   5 req/min  — Free
  basic  →  500 req/day,  20 req/min  — ₹99/month
  pro    → 5000 req/day,  60 req/min  — ₹299/month
  ultra  →  Unlimited,   200 req/min  — ₹699/month

Auth:
  Header:      x-api-key: HellAPIxxxxxxxx
  Query param: ?api_key=HellAPIxxxxxxxx
"""

import os
import logging
from typing import Optional

from dotenv import load_dotenv

# Load configuration before importing modules that read environment variables
# during import (database path, rate-limit settings, yt-dlp worker settings).
load_dotenv()

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ytdlp import (
    get_video_info,
    get_available_formats,
    get_direct_download_url,
    get_proxy_pool,
    reload_proxies,
    search_youtube,
    cache_stats,
    cache_clear,
    IPBlockedError,
    ExtractionError,
)
import database as db
from database import (
    is_ip_blocked,
    record_invalid_attempt,
    reset_attempts,
    log_key_audit,
    get_bf_stats,
    get_video_cache_stats,
    clear_video_cache,
)

load_dotenv()

logger   = logging.getLogger(__name__)
APP_NAME = os.getenv("APP_NAME", "HellAPI")
APP_URL  = os.getenv("APP_URL", "")           # e.g. https://hellapi.onrender.com
BOT_URL  = os.getenv("BOT_URL", "")           # e.g. https://t.me/HellAPIBot
ADMIN_KEY = os.getenv("ADMIN_KEY", "")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    logger.info(f"{APP_NAME} v5.0 started.")
    yield

app = FastAPI(
    title=f"{APP_NAME} — YouTube Stream API",
    description=(
        "Commercial-grade YouTube audio/video stream API.\n\n"
        f"Get your free API key from our Telegram bot: {BOT_URL or 'Contact admin'}\n\n"
        "**Authentication:** Pass your key via `x-api-key` header or `?api_key=` query param.\n\n"
        "**Plans:** Free → Basic → Pro → Ultra (see `/plans`)"
    ),
    version="5.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # Credentials cannot be used with a wildcard origin. The API authenticates
    # with an API key, so browser credentials are intentionally disabled.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_client_ip(request: Request) -> str:
    """Extract real client IP, respecting reverse-proxy headers."""
    # Render / Railway / Heroku set X-Forwarded-For
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


def _extract_key(request: Request) -> str:
    key = request.headers.get("x-api-key") or request.query_params.get("api_key")
    if not key:
        raise HTTPException(
            status_code=401,
            detail={
                "error":   "missing_key",
                "message": (
                    "API key required. Pass via header 'x-api-key' or '?api_key=' param. "
                    f"Get your free key at {BOT_URL or 'our Telegram bot'}."
                ),
            },
        )
    return key


def require_valid_key(request: Request) -> dict:
    key = _extract_key(request)
    ip  = _get_client_ip(request)

    # ── Brute-force / IP block check ────────────────────────────────────
    if is_ip_blocked(ip):
        raise HTTPException(
            status_code=429,
            detail={
                "error":   "ip_blocked",
                "message": "Too many invalid attempts. Your IP is temporarily blocked.",
                "retry_after": "5 minutes",
            },
        )

    # ── Key validation ───────────────────────────────────────────────────
    key_row = db.validate_key(key)
    if not key_row:
        result = record_invalid_attempt(ip)
        # Log failed attempt
        log_key_audit(0, key[:39], "invalid_attempt", ip=ip,
                      note=f"attempt {result['attempts']}/{db.BF_MAX_ATTEMPTS}")
        if result["blocked"]:
            logger.warning(f"IP blocked after {result['attempts']} invalid attempts: {ip}")
            raise HTTPException(
                status_code=429,
                detail={
                    "error":   "ip_blocked",
                    "message": (
                        f"Too many invalid key attempts. "
                        f"IP blocked for {db.BF_BLOCK_SEC // 60} minutes."
                    ),
                },
            )
        raise HTTPException(
            status_code=401,
            detail={
                "error":   "invalid_key",
                "message": (
                    "Invalid or expired API key. "
                    f"Get a new key at {BOT_URL or 'our Telegram bot'}."
                ),
            },
        )

    # Valid key — clear any failed attempt counter for this IP
    reset_attempts(ip)

    # ── Rate limit check ─────────────────────────────────────────────────
    rate = db.check_rate_limit(key)
    if not rate["allowed"]:
        plan     = rate.get("plan", "free")
        plan_cfg = db.PLANS.get(plan, db.PLANS["free"])
        reason   = rate.get("reason", "")

        if reason == "rpm_exceeded":
            msg = (
                f"Too many requests per minute. "
                f"Your plan '{plan}' allows {plan_cfg['rpm']} req/min. "
                "Upgrade for higher limits."
            )
        else:
            lim = "Unlimited" if plan_cfg["rpd"] == -1 else str(plan_cfg["rpd"])
            msg = (
                f"Daily limit reached ({rate['used']}/{lim}). "
                f"Plan: {plan}. Resets at midnight UTC. "
                f"Upgrade at {BOT_URL or 'our Telegram bot'}."
            )

        raise HTTPException(
            status_code=429,
            detail={
                "error":       "rate_limit_exceeded",
                "message":     msg,
                "plan":        plan,
                "used":        rate.get("used", 0),
                "limit":       rate.get("limit", 0),
                "upgrade_url": BOT_URL,
            },
        )

    return {"key": key, "plan": rate["plan"], "used": rate["used"],
            "limit": rate["limit"], "row": key_row}


def require_admin(request: Request):
    if not ADMIN_KEY:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "admin_not_configured",
                "message": "ADMIN_KEY is not configured on this server.",
            },
        )
    key = request.headers.get("x-api-key") or request.query_params.get("api_key")
    if key != ADMIN_KEY:
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "Admin access required."},
        )


# ---------------------------------------------------------------------------
# Error mapper
# ---------------------------------------------------------------------------

def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, IPBlockedError):
        return HTTPException(
            status_code=503,
            detail={
                "error":   "ip_blocked",
                "message": "Server IP temporarily blocked by YouTube. Retry in a few minutes.",
                "tip":     "Configure PROXY_LIST in .env to avoid this.",
            },
        )
    if isinstance(exc, ExtractionError):
        msg = str(exc).lower()
        if "private" in msg:
            friendly = "This video is private."
        elif "unavailable" in msg or "removed" in msg:
            friendly = "This video is unavailable or has been removed."
        elif "copyright" in msg:
            friendly = "This video is blocked due to copyright."
        elif "age" in msg:
            friendly = "This video requires age verification. Set YTDLP_COOKIE_FILE to bypass."
        else:
            friendly = f"Could not extract stream URL. ({str(exc)[:150]})"
        return HTTPException(
            status_code=400,
            detail={"error": "extraction_failed", "message": friendly},
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status_code=400,
            detail={"error": "bad_request", "message": str(exc)},
        )
    logger.error(f"Unhandled error: {exc}", exc_info=True)
    return HTTPException(
        status_code=500,
        detail={"error": "internal_error", "message": str(exc)},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_url(q: str) -> str:
    """If not a URL, search YouTube and return the top result URL."""
    if q.startswith("http://") or q.startswith("https://"):
        return q
    results = await search_youtube(q, max_results=1)
    if not results:
        raise ValueError(f"No YouTube results found for: {q}")
    return results[0]["url"]


def _log(key_info: dict, endpoint: str, query: str,
         success: bool = True, media_type: str = "audio"):
    try:
        row = key_info.get("row") or db.validate_key(key_info["key"])
        if row:
            db.log_usage(row["user_id"], key_info["key"],
                         endpoint, query, success, media_type)
    except Exception as e:
        logger.warning(f"log_usage: {e}")


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------

@app.get("/youtube.py", tags=["Files"], include_in_schema=False)
async def serve_youtube_py(request: Request):
    """Serve youtube.py — protected, only accessible with admin key."""
    require_admin(request)
    import pathlib
    from fastapi.responses import PlainTextResponse
    src = pathlib.Path(__file__).parent / "youtube.py"
    if not src.exists():
        raise HTTPException(status_code=404, detail="youtube.py not found")
    return PlainTextResponse(content=src.read_text(encoding="utf-8"),
                             media_type="text/plain")


@app.get("/", tags=["Info"], summary="API info & endpoint list")
def root():
    return {
        "api":       APP_NAME,
        "version":   "5.0.0",
        "status":    "online",
        "bot_url":   BOT_URL,
        "docs_url":  f"{APP_URL}/docs" if APP_URL else "/docs",
        "plans_url": f"{APP_URL}/plans" if APP_URL else "/plans",
        "auth":      "x-api-key header  OR  ?api_key= param",
        "endpoints": {
            "GET /stream":     "?url= or ?query=  →  universal stream",
            "GET /song":       "?url= or ?query= [&video=true]  →  AnonXMusic/Yukki style",
            "GET /api/stream": "?url= or ?query= &type=audio|video",
            "GET /ytdl":       "?url= &format=bestaudio  →  raw yt-dlp style",
            "GET /search":     "?q= [&limit=5]  →  YouTube search",
            "GET /info":       "?url=  →  video metadata",
            "GET /formats":    "?url=  →  all available formats",
            "GET /my/stats":   "your usage stats",
            "GET /plans":      "all plans & pricing",
        },
    }


@app.get("/healthz", tags=["Info"], include_in_schema=False)
def healthz():
    """Lightweight unauthenticated health check for hosting platforms."""
    return {"status": "ok", "service": APP_NAME}


@app.get("/plans", tags=["Info"], summary="All plans & pricing")
def plans():
    result = {}
    for plan_id, plan in db.PLANS.items():
        result[plan_id] = {
            **plan,
            "rpd_display": "Unlimited" if plan["rpd"] == -1 else f"{plan['rpd']:,}",
            "get_key":     BOT_URL,
        }
    return {"plans": result, "get_key_bot": BOT_URL}


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------

@app.get("/search", tags=["Search"], summary="Search YouTube")
async def yt_search(
    request: Request,
    q:     str = Query(..., description="Search query"),
    limit: int = Query(5, ge=1, le=10, description="Number of results (1-10)"),
):
    key_info = require_valid_key(request)
    try:
        results = await search_youtube(q, max_results=limit)
        _log(key_info, "/search", q, media_type="search")
        return {"ok": True, "query": q, "count": len(results), "results": results}
    except Exception as e:
        _log(key_info, "/search", q, success=False)
        raise _map_error(e)


# ---------------------------------------------------------------------------
# /stream  — universal
# ---------------------------------------------------------------------------

@app.get("/stream", tags=["Stream"], summary="Universal stream URL")
async def universal_stream(
    request: Request,
    url:     Optional[str] = Query(None, description="YouTube URL"),
    query:   Optional[str] = Query(None, description="Search query"),
):
    key_info = require_valid_key(request)
    q = url or query
    if not q:
        raise HTTPException(400, {"error": "missing_param",
                                  "message": "Provide 'url' or 'query'."})
    try:
        resolved = await _resolve_url(q)
        info, result = await _fetch_info_and_dl(resolved, "bestaudio/best")
        _log(key_info, "/stream", q)
        return {
            "status":     "ok",
            "cached":     False,
            "plan":       key_info["plan"],
            "title":      info.get("title") or "",
            "duration":   info.get("duration_seconds") or 0,
            "thumbnail":  info.get("thumbnail") or "",
            "uploader":   info.get("uploader") or "",
            "view_count": info.get("view_count") or 0,
            "stream_url": result["direct_url"],
            "source_url": resolved,
            "ext":        result.get("ext") or "",
            "format_id":  result.get("format_id") or "",
            "is_live":    result.get("is_live", False),
        }
    except Exception as e:
        _log(key_info, "/stream", q, success=False)
        raise _map_error(e)


# ---------------------------------------------------------------------------
# /song  — AnonXMusic / YukkiMusic / MusicBot style
# ---------------------------------------------------------------------------

@app.get("/song", tags=["Stream"], summary="Song stream (AnonX/Yukki style)")
async def song_endpoint(
    request: Request,
    url:     Optional[str] = Query(None),
    query:   Optional[str] = Query(None),
    video:   bool          = Query(False, description="True for video stream"),
):
    key_info = require_valid_key(request)
    q = url or query
    if not q:
        raise HTTPException(400, {"error": "missing_param",
                                  "message": "Provide 'url' or 'query'."})
    try:
        fmt      = "bestvideo+bestaudio/best" if video else "bestaudio/best"
        resolved = await _resolve_url(q)
        info, result = await _fetch_info_and_dl(resolved, fmt)

        dur  = info.get("duration_seconds") or 0
        mins, secs = divmod(int(dur), 60)
        mtype = "video" if video else "audio"
        _log(key_info, "/song", q, media_type=mtype)

        return {
            "results": [{
                "title":        info.get("title") or "",
                "link":         resolved,
                "duration":     f"{mins}:{secs:02d}",
                "duration_sec": dur,
                "thumbnail":    info.get("thumbnail") or "",
                "uploader":     info.get("uploader") or "",
                "streamLink":   result["direct_url"],
                "stream_url":   result["direct_url"],
                "ext":          result.get("ext") or "",
                "isLive":       result.get("is_live", False),
                "view_count":   info.get("view_count") or 0,
            }]
        }
    except Exception as e:
        _log(key_info, "/song", q, success=False)
        raise _map_error(e)


# ---------------------------------------------------------------------------
# /api/stream  — audio / video toggle
# ---------------------------------------------------------------------------

@app.get("/api/stream", tags=["Stream"], summary="Audio or video stream")
async def api_stream(
    request: Request,
    url:     Optional[str] = Query(None),
    query:   Optional[str] = Query(None),
    type:    str           = Query("audio", description="'audio' or 'video'"),
):
    key_info = require_valid_key(request)
    q = url or query
    if not q:
        raise HTTPException(400, {"error": "missing_param",
                                  "message": "Provide 'url' or 'query'."})
    if type not in ("audio", "video"):
        raise HTTPException(400, {"error": "bad_param",
                                  "message": "type must be 'audio' or 'video'."})
    try:
        fmt      = "bestaudio/best" if type == "audio" else "bestvideo+bestaudio/best"
        resolved = await _resolve_url(q)
        info, result = await _fetch_info_and_dl(resolved, fmt)
        _log(key_info, "/api/stream", q, media_type=type)

        return {
            "ok":         True,
            "type":       type,
            "plan":       key_info["plan"],
            "url":        resolved,
            "title":      info.get("title") or "",
            "duration":   info.get("duration_seconds") or 0,
            "thumbnail":  info.get("thumbnail") or "",
            "uploader":   info.get("uploader") or "",
            "stream_url": result["direct_url"],
            "direct_url": result["direct_url"],
            "format_id":  result.get("format_id") or "",
            "ext":        result.get("ext") or "",
            "is_live":    result.get("is_live", False),
        }
    except Exception as e:
        _log(key_info, "/api/stream", q, success=False)
        raise _map_error(e)


# ---------------------------------------------------------------------------
# /ytdl  — raw yt-dlp style
# ---------------------------------------------------------------------------

@app.get("/ytdl", tags=["Stream"], summary="Raw yt-dlp style response")
async def ytdl_endpoint(
    request: Request,
    url:    str = Query(...),
    format: str = Query("bestaudio"),
):
    key_info = require_valid_key(request)
    try:
        info, result = await _fetch_info_and_dl(url, format)
        _log(key_info, "/ytdl", url)
        return {
            "id":          info.get("id"),
            "title":       info.get("title"),
            "uploader":    info.get("uploader"),
            "duration":    info.get("duration_seconds"),
            "thumbnail":   info.get("thumbnail"),
            "webpage_url": url,
            "url":         result["direct_url"],
            "ext":         result.get("ext"),
            "format_id":   result.get("format_id"),
            "acodec":      "opus",
            "vcodec":      "none" if "audio" in format else "h264",
        }
    except Exception as e:
        _log(key_info, "/ytdl", url, success=False)
        raise _map_error(e)


# ---------------------------------------------------------------------------
# /info  and  /formats
# ---------------------------------------------------------------------------

@app.get("/info", tags=["Video"], summary="Video metadata")
async def video_info(request: Request, url: str = Query(...)):
    key_info = require_valid_key(request)
    try:
        data = await get_video_info(url)
        _log(key_info, "/info", url)
        return {"success": True, "plan": key_info["plan"], "data": data}
    except Exception as e:
        _log(key_info, "/info", url, success=False)
        raise _map_error(e)


@app.get("/formats", tags=["Video"], summary="All available formats")
async def video_formats(request: Request, url: str = Query(...)):
    key_info = require_valid_key(request)
    try:
        formats = await get_available_formats(url)
        _log(key_info, "/formats", url)
        return {"success": True, "count": len(formats), "formats": formats}
    except Exception as e:
        _log(key_info, "/formats", url, success=False)
        raise _map_error(e)


# ---------------------------------------------------------------------------
# /my/stats
# ---------------------------------------------------------------------------

@app.get("/my/stats", tags=["Account"], summary="Your usage stats")
def my_stats(request: Request):
    key_info = require_valid_key(request)
    row      = key_info.get("row") or db.validate_key(key_info["key"])
    stats    = db.get_usage_stats(row["user_id"])
    plan_cfg = db.PLANS.get(key_info["plan"], db.PLANS["free"])
    rpd      = plan_cfg["rpd"]
    days     = db.days_remaining_by_key(key_info["key"])
    return {
        "plan":            key_info["plan"],
        "plan_price":      plan_cfg["price"],
        "daily_limit":     rpd if rpd != -1 else "unlimited",
        "rpm_limit":       plan_cfg["rpm"],
        "used_today":      stats["today"],
        "remaining_today": max(0, rpd - stats["today"]) if rpd != -1 else "unlimited",
        "total_requests":  stats["total"],
        "total_audio":     stats["total_audio"],
        "total_video":     stats["total_video"],
        "last_used":       stats["last_used"],
        "key_expires_in":  f"{days} days",
        "upgrade_url":     BOT_URL,
    }


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@app.get("/admin/stats", tags=["Admin"], summary="Global stats")
def admin_stats(request: Request):
    require_admin(request)
    return db.get_admin_stats()


@app.get("/admin/proxy-status", tags=["Admin"])
def proxy_status(request: Request):
    require_admin(request)
    pool   = get_proxy_pool()
    masked = [p.split("@")[-1][:40] for p in pool]
    return {"proxy_count": len(pool), "proxies": masked}


@app.post("/admin/proxy-reload", tags=["Admin"])
def proxy_reload(request: Request):
    require_admin(request)
    return {"success": True, "proxy_count": reload_proxies()}


@app.get("/admin/cache-stats", tags=["Admin"])
def admin_cache_stats(request: Request):
    require_admin(request)
    ram  = cache_stats()
    db_c = get_video_cache_stats()
    return {
        "ram_cache":       ram,
        "db_video_cache":  db_c,
    }


@app.post("/admin/cache-clear", tags=["Admin"])
def admin_cache_clear(
    request: Request,
    target: str = Query("all", description="'ram' | 'db' | 'all'"),
    older_than_days: int = Query(None, description="Only clear DB entries older than N days"),
):
    require_admin(request)
    cleared = []
    if target in ("ram", "all"):
        cache_clear()
        cleared.append("ram")
    if target in ("db", "all"):
        clear_video_cache(older_than_days)
        cleared.append("db")
    return {"success": True, "cleared": cleared}


@app.get("/admin/security", tags=["Admin"], summary="Brute-force & IP block status")
def admin_security(request: Request):
    require_admin(request)
    stats = get_bf_stats()
    return {
        "active_ip_blocks":   stats["active_blocks"],
        "pending_ips":        stats["pending_attempts"],
        "config": {
            "max_attempts":   db.BF_MAX_ATTEMPTS,
            "window_seconds": db.BF_WINDOW_SEC,
            "block_seconds":  db.BF_BLOCK_SEC,
        },
    }


@app.post("/admin/unblock-ip", tags=["Admin"], summary="Manually unblock an IP")
def admin_unblock_ip(request: Request, ip: str = Query(...)):
    require_admin(request)
    reset_attempts(ip)
    return {"success": True, "message": f"IP {ip} unblocked."}


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def _global_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "internal_error", "message": str(exc)},
    )


# ---------------------------------------------------------------------------
# Internal helper — fetch info + download url in parallel using cache
# ---------------------------------------------------------------------------

async def _fetch_info_and_dl(url: str, fmt: str):
    """
    Fetch info and direct URL. Both are cached independently.
    If the cache already has info, we save one yt-dlp call.
    """
    import asyncio as _asyncio
    info, result = await _asyncio.gather(
        get_video_info(url),
        get_direct_download_url(url, format_id=fmt),
    )
    return info, result


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False,
                workers=int(os.getenv("WORKERS", "1")))
