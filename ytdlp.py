"""
ytdlp.py
--------
Async-ready yt-dlp wrapper with:
  - In-memory TTL cache (8 min) — same URL won't hit YouTube twice
  - Async via ThreadPoolExecutor (non-blocking FastAPI)
  - Multi-client YouTube fallback (android → ios → web → tv_embedded → mweb)
  - Thread-safe proxy rotation
  - PO Token + cookie file support
  - Exponential backoff retry on IP blocks
"""

import os
import random
import time
import logging
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List, Dict, Any

import yt_dlp
from cachetools import TTLCache
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

# ── Logger ──────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ── Thread pool for blocking yt-dlp calls ───────────────────────────────────
_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(os.getenv("YTDLP_WORKERS", "4")),
    thread_name_prefix="ytdlp",
)

# ── In-memory cache (url+format → result, TTL = 8 min) ──────────────────────
_CACHE_TTL  = int(os.getenv("YTDLP_CACHE_TTL", "480"))   # seconds
_CACHE_SIZE = int(os.getenv("YTDLP_CACHE_SIZE", "256"))   # max entries
_cache      = TTLCache(maxsize=_CACHE_SIZE, ttl=_CACHE_TTL)
_cache_lock = threading.Lock()


def _cache_get(key: str):
    with _cache_lock:
        return _cache.get(key)


def _cache_set(key: str, value):
    with _cache_lock:
        _cache[key] = value


def cache_stats() -> dict:
    with _cache_lock:
        return {"size": len(_cache), "maxsize": _CACHE_SIZE, "ttl": _CACHE_TTL}


def cache_clear():
    with _cache_lock:
        _cache.clear()


# ── User-agent pool ──────────────────────────────────────────────────────────
try:
    from fake_useragent import UserAgent as _FUA
    _fua = _FUA()
    def _random_ua() -> str:
        try:
            return _fua.random
        except Exception:
            return _random_ua_static()
except Exception:
    _fua = None
    def _random_ua() -> str:
        return _random_ua_static()

_STATIC_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.122 Mobile Safari/537.36",
]

def _random_ua_static() -> str:
    return random.choice(_STATIC_UAS)


# ── Proxy pool (thread-safe round-robin) ────────────────────────────────────
_proxy_lock  = threading.Lock()
_proxy_index = 0


def _load_proxies() -> List[str]:
    raw = os.getenv("PROXY_LIST", "").strip()
    if not raw:
        return []
    proxies = [p.strip() for p in raw.split(",") if p.strip()]
    logger.info(f"Loaded {len(proxies)} proxies.")
    return proxies


_PROXY_POOL: List[str] = _load_proxies()


def reload_proxies() -> int:
    global _PROXY_POOL, _proxy_index
    with _proxy_lock:
        _PROXY_POOL = _load_proxies()
        _proxy_index = 0
    return len(_PROXY_POOL)


def get_proxy_pool() -> List[str]:
    with _proxy_lock:
        return list(_PROXY_POOL)


def _get_proxy() -> Optional[str]:
    global _proxy_index
    with _proxy_lock:
        if not _PROXY_POOL:
            return None
        proxy = _PROXY_POOL[_proxy_index % len(_PROXY_POOL)]
        _proxy_index += 1
    return proxy


# ── Sleep between calls ──────────────────────────────────────────────────────
_MIN_SLEEP = float(os.getenv("YTDLP_MIN_SLEEP", "0.5"))
_MAX_SLEEP = float(os.getenv("YTDLP_MAX_SLEEP", "2.0"))


def _sleep():
    time.sleep(random.uniform(_MIN_SLEEP, _MAX_SLEEP))


# ── Custom exceptions ────────────────────────────────────────────────────────
class IPBlockedError(Exception):
    """YouTube IP block / 429 / bot-check."""

class ExtractionError(Exception):
    """Genuine yt-dlp extraction failure."""


# ── Error classifiers ────────────────────────────────────────────────────────
def _is_ip_block(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in [
        "429", "too many requests", "ip block", "blocked",
        "bot", "captcha", "nsig extraction failed",
        "sign in to confirm", "confirm you're not a bot",
        "http error 403", "forbidden",
    ])


def _is_unavailable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in [
        "video unavailable", "private video",
        "has been removed", "no longer available",
        "does not exist", "account has been terminated",
        "copyright",
    ])


# ── yt-dlp options builder ───────────────────────────────────────────────────
def _build_opts(client: list, extra: Optional[Dict] = None) -> Dict[str, Any]:
    proxy = _get_proxy()
    ua    = _random_ua()

    opts: Dict[str, Any] = {
        "quiet":          True,
        "no_warnings":    True,
        "skip_download":  True,
        "noplaylist":     True,
        "socket_timeout": 20,
        "retries":        3,
        "extractor_retries": 2,
        "nocheckcertificate": True,
        "geo_bypass":     True,
        "geo_bypass_country": "US",
        "http_headers": {
            "User-Agent":                ua,
            "Accept-Language":           "en-US,en;q=0.9",
            "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding":           "gzip, deflate, br",
            "Referer":                   "https://www.google.com/",
            "DNT":                       "1",
            "Cache-Control":             "max-age=0",
        },
        "extractor_args": {
            "youtube": {
                "player_client": client,
                "skip":          ["hls"] if client[0] not in ("tv_embedded",) else ["dash"],
            }
        },
    }

    if proxy:
        opts["proxy"] = proxy

    cookie_file = os.getenv("YTDLP_COOKIE_FILE", "").strip()
    if cookie_file and os.path.isfile(cookie_file):
        opts["cookiefile"] = cookie_file

    po_token = os.getenv("YTDLP_PO_TOKEN", "").strip()
    if po_token:
        opts["extractor_args"]["youtube"]["po_token"] = [f"web+{po_token}"]

    if extra:
        opts.update(extra)

    return opts


# ── Multi-client fallback ────────────────────────────────────────────────────
_YT_CLIENTS = [
    ["android"],
    ["ios"],
    ["android_vr"],
    ["tv_embedded"],
    ["web"],
    ["mweb"],
]


def _try_extract(url: str, extra_opts: Optional[Dict] = None) -> Dict[str, Any]:
    last_exc: Optional[Exception] = None

    for client in _YT_CLIENTS:
        try:
            _sleep()
            opts = _build_opts(client, extra_opts)
            logger.debug(f"Client {client[0]} → {url[:60]}")
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info is None:
                raise ExtractionError("yt-dlp returned no data.")
            logger.debug(f"OK with client {client[0]}")
            return info
        except yt_dlp.utils.DownloadError as e:
            if _is_unavailable(e):
                raise ExtractionError(str(e)) from e
            if _is_ip_block(e):
                time.sleep(random.uniform(2.0, 4.0))
            last_exc = e
        except Exception as e:
            if _is_unavailable(e):
                raise ExtractionError(str(e)) from e
            last_exc = e

    if last_exc and _is_ip_block(last_exc):
        raise IPBlockedError(str(last_exc)) from last_exc
    raise ExtractionError(str(last_exc or "All clients failed."))


@retry(
    reraise=True,
    stop=stop_after_attempt(int(os.getenv("YTDLP_MAX_RETRIES", "2"))),
    wait=wait_exponential(multiplier=2, min=3, max=20),
    retry=retry_if_exception_type(IPBlockedError),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def _extract_sync(url: str, extra_opts: Optional[Dict] = None) -> Dict[str, Any]:
    return _try_extract(url, extra_opts)


# ── Async wrappers ───────────────────────────────────────────────────────────
async def _extract_async(url: str, extra_opts: Optional[Dict] = None) -> Dict[str, Any]:
    """Run sync extraction in threadpool — non-blocking for FastAPI."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_EXECUTOR, lambda: _extract_sync(url, extra_opts))


# ── Public API ───────────────────────────────────────────────────────────────

async def get_video_info(url: str) -> Dict[str, Any]:
    # 1. RAM cache check
    cache_key = f"info::{url}"
    cached = _cache_get(cache_key)
    if cached:
        logger.debug(f"RAM cache HIT info: {url[:50]}")
        return cached

    # 2. DB cache check (survives restarts)
    try:
        from database import get_cached_video_metadata, cache_video_metadata
        db_cached = get_cached_video_metadata(url)
        if db_cached:
            logger.debug(f"DB cache HIT info: {url[:50]}")
            _cache_set(cache_key, db_cached)   # warm RAM cache too
            return db_cached
    except Exception:
        pass

    # 3. Fetch from YouTube
    info = await _extract_async(url)
    result = {
        "id":               info.get("id"),
        "title":            info.get("title"),
        "uploader":         info.get("uploader"),
        "channel_url":      info.get("channel_url"),
        "duration_seconds": info.get("duration"),
        "view_count":       info.get("view_count"),
        "like_count":       info.get("like_count"),
        "upload_date":      info.get("upload_date"),
        "thumbnail":        info.get("thumbnail"),
        "description":      (info.get("description") or "")[:500],
        "webpage_url":      info.get("webpage_url"),
        "is_live":          info.get("is_live", False),
        "tags":             (info.get("tags") or [])[:10],
    }

    # 4. Save to both caches
    _cache_set(cache_key, result)
    try:
        cache_video_metadata(url, result)
    except Exception:
        pass

    return result


async def get_available_formats(url: str) -> List[Dict[str, Any]]:
    cache_key = f"formats::{url}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    info    = await _extract_async(url)
    formats = info.get("formats", [])
    result  = []
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") == "none":
            continue
        if (f.get("format_id") or "").startswith("sb"):
            continue
        result.append({
            "format_id":       f.get("format_id"),
            "ext":             f.get("ext"),
            "resolution":      f.get("resolution") or f.get("format_note", ""),
            "fps":             f.get("fps"),
            "vcodec":          f.get("vcodec"),
            "acodec":          f.get("acodec"),
            "filesize_approx": f.get("filesize") or f.get("filesize_approx"),
            "tbr":             f.get("tbr"),
        })
    _cache_set(cache_key, result)
    return result


async def get_direct_download_url(url: str, format_id: Optional[str] = None) -> Dict[str, Any]:
    fmt       = format_id or "best"
    cache_key = f"dl::{url}::{fmt}"
    cached    = _cache_get(cache_key)
    if cached:
        logger.debug(f"Cache HIT dl: {url[:50]} fmt={fmt}")
        return cached

    info       = await _extract_async(url, {"format": fmt})
    direct_url = info.get("url")

    if not direct_url:
        for rf in (info.get("requested_formats") or []):
            if rf.get("url"):
                direct_url = rf["url"]
                break

    if not direct_url:
        for f in info.get("formats", []):
            if f.get("url"):
                direct_url = f["url"]
                break

    if not direct_url:
        raise ExtractionError("Could not resolve a direct stream URL for this format.")

    result = {
        "title":      info.get("title"),
        "format_id":  info.get("format_id"),
        "ext":        info.get("ext"),
        "resolution": info.get("resolution"),
        "direct_url": direct_url,
        "is_live":    info.get("is_live", False),
    }
    _cache_set(cache_key, result)
    return result


async def search_youtube(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search YouTube and return simplified result list."""
    cache_key = f"search::{query}::{max_results}"
    cached    = _cache_get(cache_key)
    if cached:
        return cached

    opts = {
        "quiet":        True,
        "no_warnings":  True,
        "extract_flat": True,
        "noplaylist":   True,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }

    def _do_search():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
            return info.get("entries", []) if info else []

    loop    = asyncio.get_event_loop()
    entries = await loop.run_in_executor(_EXECUTOR, _do_search)

    results = []
    for e in entries:
        vid_id = e.get("id") or ""
        if not vid_id:
            continue
        results.append({
            "id":       vid_id,
            "title":    e.get("title", ""),
            "uploader": e.get("uploader") or e.get("channel", ""),
            "duration": e.get("duration"),
            "url":      f"https://www.youtube.com/watch?v={vid_id}",
            "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
        })

    _cache_set(cache_key, results)
    return results
