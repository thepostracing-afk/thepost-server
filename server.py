#!/usr/bin/env python3
"""The Post - Live Tips Server  v12 (phone widget feeds, only the newest Discord board shows, past-day tips hidden, alerts only on the tip's own race day, persistent storage via Upstash Redis, cached silk blobs)"""

import os, json, datetime, base64, hashlib, asyncio
from collections import OrderedDict
from urllib.parse import quote
import requests
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.concurrency import run_in_threadpool

try:
    from pywebpush import webpush, WebPushException
    _PUSH_LIB_AVAILABLE = True
except ImportError:
    # Lets the whole app boot even if `pywebpush` hasn't been deployed yet.
    # Add `pywebpush` to requirements.txt to enable jump-time notifications.
    _PUSH_LIB_AVAILABLE = False
    class WebPushException(Exception):
        pass
    def webpush(*args, **kwargs):
        raise WebPushException("pywebpush is not installed on the server")

try:
    from zoneinfo import ZoneInfo
    NOTIFY_TZ = ZoneInfo("Australia/Melbourne")
except Exception:
    # Falls back to fixed AEST (no daylight saving) if tzdata isn't available.
    # Add `tzdata` to requirements.txt to avoid this.
    NOTIFY_TZ = datetime.timezone(datetime.timedelta(hours=10))

# --- Web Push / VAPID config (jump-time notifications) ---
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "").strip() or "mailto:admin@example.com"
NOTIFY_TYPES = ("BACK", "PLACE", "MULTI")
# v10: days of the week alerts may go out (0=Mon ... 6=Sun). Default is every day,
# so a bet on a Tuesday or Thursday can't be missed. Set the env var
# NOTIFY_DAYS=5 on Render to go back to Saturday only.
# v11: on top of this, a tip only ever alerts on its OWN race day.
_nd = os.environ.get("NOTIFY_DAYS", "0,1,2,3,4,5,6")
NOTIFY_DAYS = {int(x) for x in _nd.split(",") if x.strip().isdigit() and 0 <= int(x) <= 6} or set(range(7))
# v10: an alert is still sent up to this many minutes AFTER the jump time if the
# server was briefly busy or restarting right when the window opened.
NOTIFY_GRACE_MIN = 2
# Single source of truth for "can we actually send a push right now".
_PUSH_READY = _PUSH_LIB_AVAILABLE and bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)

PUSH_API_KEY = os.environ.get("PUSH_API_KEY", "thepost2026")

# --- Upstash Redis REST config ---
UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
STORE_KEY     = "thepost_store"

DEFAULT_STORE = {"tips": [], "analyzer": [], "live": [], "last_push": None, "push_count": 0, "push_subs": {}, "pnl": None,
                 "discord_tips": [], "discord_updated": None, "tips_updated": None}

app = FastAPI(title="The Post", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=500)

ICON_PATH = os.path.join(os.path.dirname(__file__), "thepost.png")

# ---------------------------------------------------------------------------
# Silk images - single shared component used by the Tips cards and the
# Analyzer tables, so silks look and behave identically everywhere.
# ---------------------------------------------------------------------------
SILK_SIZE      = 22
SILK_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "silk_cache")
os.makedirs(SILK_CACHE_DIR, exist_ok=True)
SILK_MEM_CACHE_MAX = 500
_silk_mem_cache = OrderedDict()   # sha1(url) -> (content_type, bytes)

def _silk_mem_cache_put(key, value):
    _silk_mem_cache[key] = value
    _silk_mem_cache.move_to_end(key)
    if len(_silk_mem_cache) > SILK_MEM_CACHE_MAX:
        _silk_mem_cache.popitem(last=False)

# --- Inline (data: URI) silks ---
# Each distinct silk is decoded ONCE, stored under a short content hash, and
# served from /s/<hash> with a far-future immutable Cache-Control.
SILK_BLOB_MAX = 4000
_silk_blobs = OrderedDict()      # short hash -> (content_type, bytes)
_silk_uri_hash = {}              # sha1(data URI string) -> short hash

def _register_data_silk(data_uri):
    """Decode an inline data: URI once and return the short hash it's served
    under at /s/<hash>. Returns None if the URI can't be decoded."""
    uri_key = hashlib.sha1(data_uri.encode("utf-8")).hexdigest()
    h = _silk_uri_hash.get(uri_key)
    if h and h in _silk_blobs:
        return h
    try:
        header, b64 = data_uri.split(",", 1)
        ctype = header[5:].split(";")[0].strip() or "image/png"
        raw = base64.b64decode(b64)
    except Exception:
        return None
    h = hashlib.sha1(raw).hexdigest()[:16]
    _silk_blobs[h] = (ctype, raw)
    _silk_blobs.move_to_end(h)
    _silk_uri_hash[uri_key] = h
    while len(_silk_blobs) > SILK_BLOB_MAX:
        _silk_blobs.popitem(last=False)
    if len(_silk_uri_hash) > SILK_BLOB_MAX * 2:
        _silk_uri_hash.clear()
    return h

def _get_silk_url(d):
    """Pull whatever silk URL field is present on a tip/horse dict (never
    generated, never substituted - only ever the real asset's own URL)."""
    return (
        d.get("silk_url") or d.get("SilkURL")
        or d.get("silk")     or d.get("Silk")
        or d.get("silk_image_url") or ""
    ).strip()

def _get_horse_number(d):
    """Pull whatever saddlecloth/runner-number field is present on a tip
    dict. Returns '' (never a guess) if nothing is there."""
    for k in ("number", "horse_number", "saddlecloth", "saddlecloth_number",
              "runner_number", "tab_number", "program_number"):
        v = d.get(k)
        if v not in (None, ""):
            return v
    return ""

def _silk_html(url, size=SILK_SIZE):
    """Render the shared silk component. No silk = identically-sized blank slot."""
    blank = f'<span class="silk-wrap" style="width:{size}px;height:{size}px;"></span>'
    if not url:
        return blank
    if url.startswith("data:"):
        h = _register_data_silk(url)
        if not h:
            return blank
        src = f"/s/{h}"
    else:
        src = f"/silk?u={quote(url, safe='')}"
    return (
        f'<span class="silk-wrap" style="width:{size}px;height:{size}px;">'
        f'<img class="silk-img" src="{src}" width="{size}" height="{size}" '
        f'loading="lazy" decoding="async" alt="" '
        f'onerror="this.style.visibility=\'hidden\'">'
        f'</span>'
    )

def _fetch_silk_sync(u, disk_path, ext):
    """All the blocking work for a cache-miss silk. Always called via
    run_in_threadpool so a slow silk host never stalls the event loop."""
    if os.path.exists(disk_path):
        with open(disk_path, "rb") as f:
            data = f.read()
        ctype = "image/svg+xml" if ext == ".svg" else "image/png"
        return ctype, data
    r = requests.get(u, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    data = r.content
    ctype = r.headers.get("content-type", "").split(";")[0].strip()
    if not ctype or "text/html" in ctype:
        ctype = "image/svg+xml" if ext == ".svg" else "image/png"
    with open(disk_path, "wb") as f:
        f.write(data)
    return ctype, data

@app.get("/silk")
async def silk_proxy(u: str = ""):
    """Fetch-once, cache-forever proxy for a horse's real silk image."""
    if not u:
        raise HTTPException(status_code=404, detail="no silk")
    key = hashlib.sha1(u.encode("utf-8")).hexdigest()

    cached = _silk_mem_cache.get(key)
    if cached is not None:
        _silk_mem_cache.move_to_end(key)
        ctype, data = cached
        return Response(data, media_type=ctype, headers={"Cache-Control": "public, max-age=604800, immutable"})

    ext = ".svg" if u.lower().split("?")[0].endswith(".svg") else ".img"
    disk_path = os.path.join(SILK_CACHE_DIR, key + ext)

    try:
        ctype, data = await run_in_threadpool(_fetch_silk_sync, u, disk_path, ext)
        _silk_mem_cache_put(key, (ctype, data))
        return Response(data, media_type=ctype, headers={"Cache-Control": "public, max-age=604800, immutable"})
    except Exception:
        raise HTTPException(status_code=404, detail="silk unavailable")

@app.get("/s/{h}")
async def silk_blob(h: str):
    """Serves a decoded inline silk by its content hash."""
    hit = _silk_blobs.get(h)
    if not hit:
        raise HTTPException(status_code=404, detail="silk unavailable")
    ctype, data = hit
    return Response(data, media_type=ctype, headers={
        "Cache-Control": "public, max-age=31536000, immutable",
        "ETag": f'"{h}"',
    })

@app.get("/icon.png")
async def serve_icon():
    from fastapi.responses import FileResponse
    if os.path.exists(ICON_PATH):
        return FileResponse(ICON_PATH, media_type="image/png")
    return Response(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="), media_type="image/png")

@app.get("/manifest.json")
async def manifest():
    return JSONResponse({"name":"The Post","short_name":"The Post","description":"Racing Intelligence","start_url":"/dash","display":"standalone","background_color":"#0B0F14","theme_color":"#0B0F14","orientation":"portrait","icons":[{"src":"/icon.png","sizes":"512x512","type":"image/png"}]})

# ---------------------------------------------------------------------------
# Persistent storage via Upstash Redis REST API
# ---------------------------------------------------------------------------

def _headers():
    return {"Authorization": f"Bearer {UPSTASH_TOKEN}"}

def _load():
    """Fetch the store JSON from Upstash. Falls back to defaults on any error."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return dict(DEFAULT_STORE)
    try:
        r = requests.get(f"{UPSTASH_URL}/get/{STORE_KEY}", headers=_headers(), timeout=5)
        r.raise_for_status()
        result = r.json().get("result")
        if result is None:
            return dict(DEFAULT_STORE)
        return json.loads(result)
    except Exception:
        return dict(DEFAULT_STORE)

def _save(s):
    """Write the store JSON to Upstash (push subscriptions live in their own hash)."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return
    try:
        payload = json.dumps({k: v for k, v in s.items() if k != "push_subs"})
        requests.post(f"{UPSTASH_URL}/set/{STORE_KEY}", headers=_headers(), data=payload, timeout=5)
    except Exception:
        pass

# _store is the single in-memory source of truth for every GET route below.
_store = _load()
_store.setdefault("push_subs", {})
_store.setdefault("discord_tips", [])
_store.setdefault("discord_updated", None)
_store.setdefault("tips_updated", None)
_push_lock = asyncio.Lock()

def _live_tips(store):
    """The tips shown on the Tips page and Dashboard, returned by /api/tips
    and used for jump-time alerts are the tips posted to Discord, and nothing
    else.

    v11: a tip whose race day is in the past is never shown, so yesterday's
    tips disappear at midnight (Melbourne) even if no new board has been sent
    yet. Today's and future-dated tips (e.g. Saturday's board sent on Friday)
    stay. A tip with no date at all is kept (it can still never alert)."""
    today = _today_str()
    out = []
    for t in store.get("discord_tips") or []:
        day = _tip_day(t, store)
        if not day or day >= today:
            out.append(t)
    return out

# ---------------------------------------------------------------------------
# Durable push subscriptions. One Redis hash, one field per device.
# ---------------------------------------------------------------------------
SUBS_KEY = STORE_KEY + ":push_subs"
_subs = {}   # sub_key -> {"subscription":..., "prefs":..., "gone":bool, "fails":int}

def _redis_cmd(*cmd):
    r = requests.post(UPSTASH_URL.rstrip("/"), headers=_headers(), json=list(cmd), timeout=5)
    r.raise_for_status()
    return r.json().get("result")

def _subs_fetch():
    """All stored subscriptions, or None if Redis could not be read."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    try:
        flat = _redis_cmd("HGETALL", SUBS_KEY) or []
        out = {}
        for i in range(0, len(flat) - 1, 2):
            try: out[flat[i]] = json.loads(flat[i + 1])
            except Exception: pass
        return out
    except Exception as e:
        print(f"[subs] fetch failed: {e}")
        return None

def _subs_get(key):
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    try:
        raw = _redis_cmd("HGET", SUBS_KEY, key)
        return json.loads(raw) if raw else None
    except Exception:
        return None

def _subs_put(key, entry):
    """Persist one device. True on success (or when Redis isn't configured)."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return True
    try:
        _redis_cmd("HSET", SUBS_KEY, key, json.dumps(entry))
        return True
    except Exception as e:
        print(f"[subs] put failed: {e}")
        return False

def _subs_del(key):
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return True
    try:
        _redis_cmd("HDEL", SUBS_KEY, key)
        return True
    except Exception as e:
        print(f"[subs] delete failed: {e}")
        return False

def _subs_boot():
    """Load subscriptions at startup and migrate any that older versions kept
    inside the tips blob."""
    fresh = _subs_fetch()
    if fresh is not None:
        _subs.update(fresh)
    legacy = _store.get("push_subs") or {}
    for k, e in legacy.items():
        if isinstance(e, dict) and e.get("subscription"):
            _subs.setdefault(k, e)
    if legacy and fresh is not None:
        ok = True
        for k, e in legacy.items():
            if k not in fresh and isinstance(e, dict) and e.get("subscription"):
                ok = _subs_put(k, e) and ok
        if ok:
            _store["push_subs"] = {}
            _save(_store)

async def _sync_subs():
    """Adopt what Redis holds (another process may have added/changed devices)."""
    fresh = await run_in_threadpool(_subs_fetch)
    if fresh is None:
        return
    _subs.clear()
    _subs.update(fresh)

async def _sub_lookup(key):
    e = _subs.get(key)
    if e is None:
        e = await run_in_threadpool(_subs_get, key)
        if e:
            _subs[key] = e
    return e

def _push_status(exc):
    """HTTP status of a failed web push, if the library gave us one."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    if code:
        return int(code)
    txt = str(exc)
    for c in (410, 404):
        if f"Push failed: {c}" in txt or f"Response: <Response [{c}]>" in txt or f"[{c}]" in txt:
            return c
    return None

def _register_all_silks():
    """Decode every inline silk currently in the store so /s/<hash> URLs
    still resolve after a restart or after newer data is adopted."""
    try:
        for key in ("tips", "discord_tips"):
            for t in _store.get(key, []) or []:
                for d in [t] + list(t.get("legs") or []):
                    u = _get_silk_url(d)
                    if u.startswith("data:"):
                        _register_data_silk(u)
        for r in _store.get("analyzer", []):
            for h in r.get("horses", []):
                u = _get_silk_url(h)
                if u.startswith("data:"):
                    _register_data_silk(u)
    except Exception:
        pass

_register_all_silks()
_subs_boot()

# Rendered-HTML cache, keyed by page. Invalidated by push_count (and, because
# every page key carries today's date, again at midnight Melbourne time).
_page_cache = {}

def _cached_page(key, build_fn):
    pc = _store.get("push_count", 0)
    hit = _page_cache.get(key)
    if hit and hit[0] == pc:
        return hit[1]
    html = build_fn()
    _page_cache[key] = (pc, html)
    return html

# ---------------------------------------------------------------------------
# Freshness guarantee. Only adopts the Upstash copy if its push_count is
# strictly NEWER (>) than what this process already holds.
# ---------------------------------------------------------------------------
async def _refresh_store():
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return
    try:
        fresh = await run_in_threadpool(_load)
    except Exception:
        return
    if fresh.get("push_count", 0) > _store.get("push_count", 0):
        async with _push_lock:
            if fresh.get("push_count", 0) > _store.get("push_count", 0):
                _store.clear()
                _store.update(fresh)
                _store.setdefault("push_subs", {})
                _register_all_silks()

# ---------------------------------------------------------------------------
# Jump-time push notifications for the tips posted to Discord.
# ---------------------------------------------------------------------------
_notified = set()  # (sub_key, tip_id) already sent - in-memory fallback

def _sub_key(endpoint):
    return hashlib.sha1((endpoint or "").encode("utf-8")).hexdigest()[:16]

def _tip_id(t):
    day = _tip_day(t, _store)
    if t.get("type") == "MULTI":
        legs_key = "|".join(
            f'{l.get("track","")}-{l.get("race","")}-{l.get("horse","")}'
            for l in (t.get("legs") or [])
        )
        raw = f'MULTI|{day}|{t.get("time","")}|{legs_key}'
    else:
        raw = f'{day}|{t.get("type","")}|{t.get("track","")}|{t.get("race","")}|{t.get("time","")}|{t.get("horse","")}'
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

def _jump_dt_today(time_str, now):
    """Turn a race's raw time string into a concrete datetime for `now`'s
    date, using the same AM/PM inference as _time_key."""
    minutes = _time_key(time_str)
    if minutes is None or minutes >= 99999:
        return None
    h, m = divmod(int(minutes), 60)
    if h >= 24:
        return None
    return now.replace(hour=h % 24, minute=m, second=0, microsecond=0)

def _send_push_sync(subscription, payload):
    webpush(
        subscription_info=subscription,
        data=json.dumps(payload),
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims={"sub": VAPID_CLAIM_EMAIL},
    )

def _tip_day(t, store):
    """The date a tip is for (YYYY-MM-DD): the race day the desktop app sent
    with it. Falls back to the day it was posted to Discord (Melbourne date);
    '' if the server has no date at all."""
    rd = str(t.get("race_date") or "").strip()[:10]
    if not rd:
        rd = str(t.get("sent_at") or store.get("discord_updated") or "").strip()[:10]
    return rd

def _incoming_day(t, today):
    """Race day of a tip arriving in a new Discord push. A tip with no date at
    all is treated as being for today."""
    rd = str(t.get("race_date") or "").strip()[:10]
    if not rd:
        rd = str(t.get("sent_at") or "").strip()[:10]
    return rd or today

def _set_discord_board(new_discord, clear=False):
    """v11: replace the Discord board wholesale, never merge it with the old
    one. Tips dated before today are dropped from the incoming list. An empty
    result only clears the board when clear=True (so a glitchy empty push can
    never wipe good data). Returns True if the board changed."""
    if not isinstance(new_discord, list):
        return False
    today = _today_str()
    fresh = [t for t in new_discord if isinstance(t, dict) and _incoming_day(t, today) >= today]
    if not fresh and not clear:
        return False
    if fresh == (_store.get("discord_tips") or []):
        return False
    _store["discord_tips"] = fresh
    _store["discord_updated"] = datetime.datetime.now(NOTIFY_TZ).isoformat()
    return True

def _claim_send_sync(sub_key, tip_id):
    """True if this device has not been sent this tip yet. A Redis key (SET NX,
    24h expiry) means a restart or a second instance can't double-send; the
    in-memory set is the fallback if Redis can't be reached."""
    if (sub_key, tip_id) in _notified:
        return False
    if UPSTASH_URL and UPSTASH_TOKEN:
        try:
            if _redis_cmd("SET", f"{STORE_KEY}:sent:{sub_key}:{tip_id}", "1", "NX", "EX", 86400) is None:
                return False
        except Exception as e:
            print(f"[notify] dedupe check failed, using memory only: {e}")
    _notified.add((sub_key, tip_id))
    return True

def _release_send_sync(sub_key, tip_id):
    """Give the claim back after a failed send so the next tick retries."""
    _notified.discard((sub_key, tip_id))
    if UPSTASH_URL and UPSTASH_TOKEN:
        try:
            _redis_cmd("DEL", f"{STORE_KEY}:sent:{sub_key}:{tip_id}")
        except Exception:
            pass

async def _notify_tick():
    if not _PUSH_READY:
        return  # push not configured (or pywebpush not installed) - nothing to do
    now = datetime.datetime.now(NOTIFY_TZ)
    if now.weekday() not in NOTIFY_DAYS:
        return
    today = now.date().isoformat()

    # Work out which Discord tips are close to the jump BEFORE touching Redis,
    # so a quiet day costs no Upstash commands.
    due = []
    for t in _live_tips(_store):
        ttype = t.get("type")
        if ttype not in NOTIFY_TYPES:
            continue
        day = _tip_day(t, _store)
        if day != today:
            continue   # v11: alerts only on the tip's own race day (unknown day = no alert)
        jump = _jump_dt_today(t.get("time", ""), now)
        if not jump:
            continue
        mins_out = (jump - now).total_seconds() / 60.0
        if mins_out < -NOTIFY_GRACE_MIN or mins_out > 60:
            continue
        due.append((t, ttype, mins_out))
    if not due:
        return

    await _sync_subs()
    subs = [(k, e) for k, e in _subs.items() if not e.get("gone")]
    if not subs:
        return

    for t, ttype, mins_out in due:
        tid = _tip_id(t)
        if ttype == "MULTI":
            legs = t.get("legs", []) or []
            first_leg = legs[0] if legs else {}
            silk = _get_silk_url(first_leg)
            horse_label = f'{len(legs)}-Leg Multi'
            track_label = first_leg.get("track", t.get("track", ""))
            race_label  = first_leg.get("race", t.get("race", ""))
        else:
            silk = _get_silk_url(t)
            number = _get_horse_number(t)
            horse_label = f'{number}. {t.get("horse","")}' if number != "" else t.get("horse", "")
            track_label = t.get("track", "")
            race_label  = t.get("race", "")
        if silk and silk.startswith("data:"):
            _h = _register_data_silk(silk)
            icon = f"/s/{_h}" if _h else "/icon.png"
        elif silk:
            icon = f"/silk?u={quote(silk, safe='')}"
        else:
            icon = "/icon.png"
        when = "jumping now" if mins_out < 0.5 else f"{round(mins_out)} min to jump"
        price = t.get("combined_odds") if ttype == "MULTI" else t.get("real_odds")
        try:
            price_txt = f' \u00b7 ${float(price):.2f}' if price else ""
        except (TypeError, ValueError):
            price_txt = ""
        payload = {
            "title": f'{race_label if (track_label and str(race_label).startswith(str(track_label))) else (str(track_label) + " " + str(race_label)).strip()} \u2014 {when}',
            "body": f'{horse_label} \u00b7 {ttype.title()} \u00b7 {_dc_f(t.get("units"), 1):g}u{price_txt}',
            "icon": icon,
            "tag": tid,
            "url": "/tips",
        }
        for key, entry in subs:
            prefs = entry.get("prefs", {})
            if not prefs.get("enabled", True):
                continue
            if ttype == "PLACE" and not prefs.get("place", True):
                continue
            if ttype == "MULTI" and not prefs.get("multi", True):
                continue
            lead = prefs.get("minutes_before", 5)
            if mins_out > lead:
                continue
            if not await run_in_threadpool(_claim_send_sync, key, tid):
                continue
            try:
                await run_in_threadpool(_send_push_sync, entry["subscription"], payload)
                if entry.get("fails") or entry.get("gone"):
                    entry["fails"] = 0
                    entry.pop("gone", None)
                    await run_in_threadpool(_subs_put, key, entry)
            except Exception as e:
                # A failed send NEVER deletes a subscription. 404/410 marks it gone
                # (the device replaces it itself next time the app opens). Any other
                # failure gives the claim back so the next 20s tick retries while the
                # jump window is still open.
                code = _push_status(e)
                if code in (404, 410):
                    entry["gone"] = True
                else:
                    entry["fails"] = int(entry.get("fails", 0) or 0) + 1
                    await run_in_threadpool(_release_send_sync, key, tid)
                entry["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"
                print(f"[notify] send failed for {key}: status={code} {entry['last_error']}")
                await run_in_threadpool(_subs_put, key, entry)

@app.on_event("startup")
async def _start_background_sync():
    """Re-pull from Upstash every 45s so the in-memory store self-heals.
    Only ever adopts Upstash data that is strictly newer (>) than local."""
    async def _loop():
        while True:
            await asyncio.sleep(45)
            try:
                fresh = await run_in_threadpool(_load)
                if fresh.get("push_count", 0) > _store.get("push_count", 0):
                    async with _push_lock:
                        if fresh.get("push_count", 0) > _store.get("push_count", 0):
                            _store.clear()
                            _store.update(fresh)
                            _store.setdefault("push_subs", {})
                            _register_all_silks()
                await _sync_subs()
            except Exception:
                pass
    asyncio.create_task(_loop())

@app.on_event("startup")
async def _start_notify_loop():
    """Checks every 20s whether any tip has crossed into its notification window."""
    async def _loop():
        while True:
            await asyncio.sleep(20)
            try:
                await _notify_tick()
            except Exception:
                pass
    asyncio.create_task(_loop())

@app.on_event("startup")
async def _start_keepalive():
    """A free Render instance sleeps after ~15 min with no traffic, and a
    sleeping server cannot send alerts. Once it's awake this pings its own
    public URL every 10 min so it stays awake. (Render sets RENDER_EXTERNAL_URL
    itself.) It cannot wake itself, so also point a free uptime monitor at
    /healthz every 5 min as a backstop."""
    base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base:
        return
    def _ping():
        try:
            requests.get(base + "/healthz", timeout=10)
        except Exception:
            pass
    async def _loop():
        while True:
            await asyncio.sleep(600)
            await run_in_threadpool(_ping)
    asyncio.create_task(_loop())

@app.post("/push")
async def push(request: Request, x_api_key: str = Header(default="")):
    if x_api_key != PUSH_API_KEY: raise HTTPException(status_code=401,detail="Invalid API key")
    try: body = await request.json()
    except: raise HTTPException(status_code=400,detail="Invalid JSON")
    async with _push_lock:
        new_tips     = body.get("tips",[])
        new_analyzer = body.get("analyzer",[])
        new_live     = body.get("live",[])
        new_pnl      = body.get("pnl")
        tips_commit  = bool(body.get("tips_commit", True))
        new_discord  = body.get("discord_tips")   # None = not sent, list = the whole Discord board
        discord_clear = bool(body.get("discord_clear", False))   # v11: explicit wipe of the board
        # Refuse an entirely empty payload if we're already holding real data.
        is_suspicious_empty = (
            not new_analyzer and (not tips_commit or not new_tips)
            and (_store.get("tips") or _store.get("analyzer"))
        )
        if is_suspicious_empty:
            changed = False
            if isinstance(new_pnl, dict) and new_pnl != _store.get("pnl"):
                _store["pnl"] = new_pnl
                changed = True
            # v11: the board is replaced (never merged); an empty list only clears it with discord_clear
            if _set_discord_board(new_discord, discord_clear):
                changed = True
            if changed:
                _store["push_count"] += 1
                _register_all_silks()
                await run_in_threadpool(_save, _store)
            return {
                "status": "rejected", "reason": "empty payload ignored, kept previous data",
                "tips": len(_store["tips"]), "analyzer_races": len(_store["analyzer"]),
                "live_races": len(_store["live"]),
            }
        if tips_commit:
            _store["tips"]         = new_tips
            _store["tips_updated"] = body.get("generated_at") or datetime.datetime.now().isoformat()
        _set_discord_board(new_discord, discord_clear)   # v11: replaces the board, drops past-dated tips
        _store["analyzer"]    = new_analyzer
        _store["live"]        = new_live
        if isinstance(new_pnl, dict):
            _store["pnl"] = new_pnl
        _store["last_push"]   = body.get("generated_at") or datetime.datetime.now().isoformat()
        _store["push_count"] += 1
        _register_all_silks()
        await run_in_threadpool(_save, _store)
    return {"status":"ok","tips":len(_live_tips(_store)),"analyzer_races":len(_store["analyzer"]),"live_races":len(_store["live"]),
            "tips_committed":tips_commit,"discord_tips":len(_live_tips(_store))}

@app.get("/api/tips")
async def api_tips():
    await _refresh_store()
    return JSONResponse(_live_tips(_store))

@app.get("/api/analyzer")
async def api_analyzer():
    await _refresh_store()
    return JSONResponse(_store["analyzer"])

@app.get("/api/status")
async def api_status():
    await _refresh_store()
    return {"last_push":_store["last_push"],"push_count":_store["push_count"],"tips":len(_live_tips(_store)),"analyzer_races":len(_store["analyzer"])}

# ---------------------------------------------------------------------------
# v12: phone widget feeds (read by the Scriptable widgets on iPhone, or any
# widget app that can load JSON). Small, no-cache, same data as the app.
# ---------------------------------------------------------------------------
def _widget_tip_row(t, now):
    ttype = t.get("type", "")
    if ttype == "MULTI":
        legs = t.get("legs") or []
        first = legs[0] if legs else {}
        horse = f'{len(legs)}-Leg Multi' if legs else "Multi"
        track = ", ".join(dict.fromkeys(l.get("track", "") for l in legs if l.get("track")))
        time_s = first.get("time", t.get("time", ""))
        price = t.get("combined_odds")
        number = ""
    else:
        horse = t.get("horse", "")
        track = t.get("track", "")
        time_s = t.get("time", "")
        price = t.get("real_odds")
        number = _get_horse_number(t)
    jump = _jump_dt_today(time_s, now)
    return {
        "type": ttype,
        "number": number,
        "horse": horse,
        "track": track,
        "race": t.get("race", "") if ttype != "MULTI" else "Multi",
        "time": time_s,
        "odds": round(_dc_f(price), 2),
        "units": _dc_f(t.get("units"), 1),
        "minutes": round((jump - now).total_seconds() / 60.0) if jump else None,
    }

@app.get("/api/widget/tips")
async def api_widget_tips():
    """Today's tips only (race day = today, Melbourne), earliest jump first."""
    await _refresh_store()
    now = datetime.datetime.now(NOTIFY_TZ)
    today = now.date().isoformat()
    rows = []
    for t in _live_tips(_store):
        if t.get("type") not in NOTIFY_TYPES:
            continue
        if _tip_day(t, _store) != today:
            continue
        rows.append(_widget_tip_row(t, now))
    rows.sort(key=lambda r: _time_key(r["time"]))
    return JSONResponse({"date": today, "count": len(rows), "tips": rows},
                        headers={"Cache-Control": "no-store"})

@app.get("/api/widget/races")
async def api_widget_races(n: int = 5):
    """The next n races that have not started yet (default 5)."""
    await _refresh_store()
    now = datetime.datetime.now(NOTIFY_TZ)
    rows = []
    for r in _store.get("analyzer", []):
        jump = _jump_dt_today(r.get("time", ""), now)
        if not jump:
            continue
        mins = (jump - now).total_seconds() / 60.0
        if mins <= 0:
            continue
        rows.append({"time": r.get("time", ""), "track": r.get("track", ""),
                     "race": r.get("race", ""), "minutes": round(mins)})
    rows.sort(key=lambda r: r["minutes"])
    n = max(1, min(int(n), 10))
    return JSONResponse({"date": now.date().isoformat(), "races": rows[:n]},
                        headers={"Cache-Control": "no-store"})

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/api/push/status")
async def push_status_api():
    """Open this in a browser to check alerts are live: it shows whether push is
    configured, how many devices are registered, and which tips would alert."""
    await _refresh_store()
    await _sync_subs()
    now = datetime.datetime.now(NOTIFY_TZ)
    today = now.date().isoformat()
    active = [e for e in _subs.values() if not e.get("gone") and (e.get("prefs") or {}).get("enabled", True)]
    upcoming = []
    for t in _live_tips(_store):
        if t.get("type") not in NOTIFY_TYPES:
            continue
        day = _tip_day(t, _store)
        jump = _jump_dt_today(t.get("time", ""), now)
        upcoming.append({
            "horse": t.get("horse") or "Multi",
            "type": t.get("type"),
            "time": t.get("time", ""),
            "race_day": day or "unknown",
            "alerts_today": bool(day == today and jump is not None),
            "minutes_to_jump": round((jump - now).total_seconds() / 60.0) if jump else None,
        })
    return {
        "push_ready": _PUSH_READY,
        "pywebpush_installed": _PUSH_LIB_AVAILABLE,
        "vapid_keys_set": bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY),
        "devices_registered": len(_subs),
        "devices_active": len(active),
        "devices_dead": len([e for e in _subs.values() if e.get("gone")]),
        "alert_days": sorted(NOTIFY_DAYS),
        "today_is_alert_day": now.weekday() in NOTIFY_DAYS,
        "server_time": now.isoformat(timespec="seconds"),
        "tips_on_board": len(_live_tips(_store)),
        "tips": upcoming,
    }

# ---------------------------------------------------------------------------
# Push subscription management (Settings page)
# ---------------------------------------------------------------------------

def _default_prefs(overrides=None):
    p = {"enabled": True, "place": True, "multi": True, "minutes_before": 5}
    if overrides:
        p.update(overrides)
    return p

@app.get("/api/push/prefs")
async def push_prefs_get(endpoint: str = ""):
    if not endpoint:
        return {"subscribed": False}
    entry = await _sub_lookup(_sub_key(endpoint))
    if not entry:
        return {"subscribed": False}
    return {"subscribed": True, "gone": bool(entry.get("gone")),
            "prefs": entry.get("prefs", _default_prefs())}

def _clean_prefs(prefs_in, base=None):
    p = _default_prefs(base)
    prefs_in = prefs_in or {}
    if "enabled" in prefs_in: p["enabled"] = bool(prefs_in["enabled"])
    if "place"   in prefs_in: p["place"]   = bool(prefs_in["place"])
    if "multi"   in prefs_in: p["multi"]   = bool(prefs_in["multi"])
    if "minutes_before" in prefs_in:
        try: p["minutes_before"] = max(1, min(60, int(prefs_in["minutes_before"])))
        except (TypeError, ValueError): pass
    return p

@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    try: body = await request.json()
    except: raise HTTPException(status_code=400, detail="Invalid JSON")
    sub = body.get("subscription") or {}
    endpoint = sub.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=400, detail="Missing subscription")
    key = _sub_key(endpoint)
    existing = await _sub_lookup(key)
    old_ep = body.get("replaces")
    old_key = _sub_key(old_ep) if old_ep and old_ep != endpoint else None
    old_entry = await _sub_lookup(old_key) if old_key else None
    resync = bool(body.get("resync"))
    # An automatic re-registration keeps whatever settings the device already had.
    if resync and existing:
        base = existing.get("prefs")
    elif resync and old_entry:
        base = old_entry.get("prefs")
    else:
        base = None
    prefs = _clean_prefs(body.get("prefs"), base) if not (resync and base) else _default_prefs(base)
    entry = {"subscription": sub, "prefs": prefs, "fails": 0}
    if existing and existing.get("created"):
        entry["created"] = existing["created"]
    else:
        entry["created"] = datetime.datetime.now().isoformat()
    ok = await run_in_threadpool(_subs_put, key, entry)
    if not ok:
        raise HTTPException(status_code=503, detail="Could not save subscription, try again")
    _subs[key] = entry
    if old_entry is not None and old_key:
        _subs.pop(old_key, None)
        await run_in_threadpool(_subs_del, old_key)
    return {"status": "ok"}

@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request):
    # Only reached when the user switches notifications off on that device.
    try: body = await request.json()
    except: raise HTTPException(status_code=400, detail="Invalid JSON")
    endpoint = (body.get("subscription") or {}).get("endpoint") or body.get("endpoint")
    if endpoint:
        key = _sub_key(endpoint)
        ok = await run_in_threadpool(_subs_del, key)
        if not ok:
            raise HTTPException(status_code=503, detail="Could not remove subscription, try again")
        _subs.pop(key, None)
    return {"status": "ok"}

@app.post("/api/push/prefs")
async def push_prefs_post(request: Request):
    try: body = await request.json()
    except: raise HTTPException(status_code=400, detail="Invalid JSON")
    endpoint = (body.get("subscription") or {}).get("endpoint") or body.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=400, detail="Missing endpoint")
    key = _sub_key(endpoint)
    entry = await _sub_lookup(key)
    if not entry:
        raise HTTPException(status_code=404, detail="Not subscribed")
    entry["prefs"] = _clean_prefs(body.get("prefs"), entry.get("prefs"))
    ok = await run_in_threadpool(_subs_put, key, entry)
    if not ok:
        raise HTTPException(status_code=503, detail="Could not save settings, try again")
    _subs[key] = entry
    return {"status": "ok", "prefs": entry["prefs"]}

@app.post("/api/push/test")
async def push_test(request: Request):
    if not _PUSH_READY:
        raise HTTPException(status_code=503, detail="Push not configured on server")
    try: body = await request.json()
    except: raise HTTPException(status_code=400, detail="Invalid JSON")
    sub = body.get("subscription")
    if not sub or not sub.get("endpoint"):
        raise HTTPException(status_code=400, detail="Missing subscription")
    payload = {
        "title": "The Post",
        "body": "Test notification \u2014 jump-time alerts are set up on this device.",
        "icon": "/icon.png",
        "tag": "thepost-test",
        "url": "/tips",
    }
    try:
        await run_in_threadpool(_send_push_sync, sub, payload)
    except WebPushException as e:
        print(f"[push/test] WebPushException: {e}")
        raise HTTPException(status_code=502, detail=f"Push failed: {e}")
    except Exception as e:
        print(f"[push/test] {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail=f"Push failed: {type(e).__name__}: {e}")
    return {"status": "ok"}

@app.get("/sw.js")
async def service_worker():
    js = """
self.addEventListener('install', function(event){ self.skipWaiting(); });
self.addEventListener('activate', function(event){ event.waitUntil(self.clients.claim()); });
self.addEventListener('push', function(event){
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch(e) {}
  var title = data.title || 'The Post';
  var options = {
    body: data.body || '',
    icon: data.icon || '/icon.png',
    badge: '/icon.png',
    tag: data.tag || undefined,
    renotify: !!data.tag,
    requireInteraction: true,
    vibrate: [200, 100, 200],
    data: { url: data.url || '/' }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});
self.addEventListener('notificationclick', function(event){
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true}).then(function(list){
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      if ('focus' in c) {
        if ('navigate' in c) { try { c.navigate(url); } catch(e) {} }
        return c.focus();
      }
    }
    return clients.openWindow(url);
  }));
});
// The browser rotated or dropped this device's subscription: get a new one and
// tell the server, which carries the device's settings over to it.
function _b64ToUint8(b64){
  var pad = '='.repeat((4 - b64.length % 4) % 4);
  var base64 = (b64 + pad).replace(/-/g, '+').replace(/_/g, '/');
  var raw = atob(base64), arr = new Uint8Array(raw.length);
  for (var i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr;
}
self.addEventListener('pushsubscriptionchange', function(event){
  event.waitUntil((async function(){
    try {
      var old = event.oldSubscription || null;
      var sub = event.newSubscription || await self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: _b64ToUint8('__VAPID__')
      });
      await fetch('/api/push/subscribe', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({subscription: sub.toJSON(), resync: true, replaces: old ? old.endpoint : null})
      });
    } catch(e) {}
  })());
});
""".replace("__VAPID__", VAPID_PUBLIC_KEY)
    return Response(js, media_type="application/javascript", headers={"Cache-Control": "no-cache"})

_HEAL_JS = r"""(function(){
  var VAPID = "__VAPID__";
  var FLAG = 'thepost_push_on', PK = 'thepost_push_prefs';
  function mark(on){
    try { localStorage.setItem(FLAG, on ? '1' : '0'); } catch(e) {}
    try { document.cookie = 'thepost_push_on=' + (on ? '1' : '0') + '; max-age=31536000; path=/; SameSite=Lax'; } catch(e) {}
  }
  window.thePostPushMark = mark;
  function b64(b){
    var pad = '='.repeat((4 - b.length % 4) % 4);
    var base64 = (b + pad).replace(/-/g, '+').replace(/_/g, '/');
    var raw = atob(base64), arr = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
    return arr;
  }
  function post(path, body){
    return fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  }
  async function run(){
    try {
      if (!VAPID || !('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) return;
      if (Notification.permission !== 'granted') return;
      var want = false;
      try { want = localStorage.getItem(FLAG) === '1'; } catch(e) {}
      if (!want) { want = /(?:^|; )thepost_push_on=1/.test(document.cookie); }
      try { if (navigator.storage && navigator.storage.persist) navigator.storage.persist(); } catch(e) {}
      var reg = await navigator.serviceWorker.getRegistration('/sw.js');
      if (!reg) {
        if (!want) return;
        reg = await navigator.serviceWorker.register('/sw.js');
      }
      await navigator.serviceWorker.ready;
      var sub = await reg.pushManager.getSubscription();
      if (!sub) {
        if (!want) return;     // never turned on here, or turned off on purpose
        sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64(VAPID)});
      }
      var r = await fetch('/api/push/prefs?endpoint=' + encodeURIComponent(sub.endpoint));
      var s = await r.json();
      var prefs = s.prefs;
      if (!prefs) { try { prefs = JSON.parse(localStorage.getItem(PK) || 'null'); } catch(e) {} }
      if (!prefs) prefs = {enabled: true, place: true, multi: true, minutes_before: 5};
      var replaces = null;
      if (s.subscribed && s.gone) {
        // the push service says this subscription is dead: swap it for a fresh one
        replaces = sub.endpoint;
        try { await sub.unsubscribe(); } catch(e) {}
        sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64(VAPID)});
      } else if (s.subscribed) {
        mark(true);
        return;
      }
      var res = await post('/api/push/subscribe', {subscription: sub.toJSON(), prefs: prefs, resync: true, replaces: replaces});
      if (res.ok) { mark(true); }
    } catch(e) {}
  }
  var inflight = null;
  window.thePostPushHeal = function(){
    if (!inflight) inflight = run().then(function(){ inflight = null; }, function(){ inflight = null; });
    return inflight;
  };
  window.thePostPushHeal();
  document.addEventListener('visibilitychange', function(){
    if (document.visibilityState === 'visible') window.thePostPushHeal();
  });
  window.addEventListener('pageshow', function(){ window.thePostPushHeal(); });
  window.addEventListener('focus', function(){ window.thePostPushHeal(); });
})();
""".replace("__VAPID__", "%VAPID%")

@app.get("/push-heal.js")
async def push_heal_js():
    js = _HEAL_JS.replace("%VAPID%", VAPID_PUBLIC_KEY if _PUSH_READY else "")
    return Response(js, media_type="application/javascript", headers={"Cache-Control": "no-cache"})

def _stat_row(label, value, suffix=""):
    """One label/value chip for the horse stat rundown. Returns '' if there's
    nothing worth showing, so the grid only ever displays real data."""
    if value is None or value == "" :
        return ""
    try:
        if float(value) == 0:
            return ""
    except (TypeError, ValueError):
        pass
    return f'<div class="hstat"><span class="hsl">{label}</span><span class="hsv">{value}{suffix}</span></div>'

def _fmt_pct(v):
    try:
        return f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return ""

def _fmt_odds(v):
    try:
        f = float(v)
        return f"${f:.2f}" if f > 0 else ""
    except (TypeError, ValueError):
        return ""

def _horse_detail_html(h):
    """Full stat rundown for one horse. Missing/zero fields are simply omitted."""
    starts_line = ""
    cs, cw, cp = h.get("career_starts",""), h.get("career_wins",""), h.get("career_places","")
    if cs not in ("", 0, None):
        starts_line = f'<div class="hstat"><span class="hsl">CAREER</span><span class="hsv">{cw}-{cp} / {cs} starts</span></div>'

    dist_line = ""
    ds, dw, dp = h.get("distance_starts",""), h.get("distance_wins",""), h.get("distance_places","")
    if ds not in ("", 0, None):
        dist_line = f'<div class="hstat"><span class="hsl">DISTANCE</span><span class="hsv">{dw}-{dp} / {ds} starts</span></div>'

    trk_line = ""
    ts, tw, tp = h.get("track_starts",""), h.get("track_wins",""), h.get("track_places","")
    if ts not in ("", 0, None):
        trk_line = f'<div class="hstat"><span class="hsl">TRACK</span><span class="hsv">{tw}-{tp} / {ts} starts</span></div>'

    cond_line = ""
    cw2, cp2 = h.get("cond_wins",""), h.get("cond_places","")
    if cw2 not in ("", 0, None) or cp2 not in ("", 0, None):
        cond_line = f'<div class="hstat"><span class="hsl">TRACK COND</span><span class="hsv">{cw2}w-{cp2}p</span></div>'

    recent_line = ""
    rw, rp = h.get("recent_wins",""), h.get("recent_places","")
    if rw not in ("", 0, None) or rp not in ("", 0, None):
        recent_line = f'<div class="hstat"><span class="hsl">RECENT FORM</span><span class="hsv">{rw}w-{rp}p</span></div>'

    chips = "".join([
        _stat_row("TRAINER", h.get("trainer","")),
        _stat_row("WEIGHT", h.get("weight",""), "kg"),
        _stat_row("AGE", h.get("age","")),
        _stat_row("JOCKEY SR", _fmt_pct(h.get("jockey_sr",""))),
        _stat_row("TRAINER SR", _fmt_pct(h.get("trainer_sr",""))),
        starts_line, dist_line, trk_line, cond_line, recent_line,
        _stat_row("LAST 10", h.get("last10","")),
        _stat_row("DAYS SINCE RUN", h.get("days_since_run","")),
        _stat_row("SPELL", h.get("spell_days",""), " days"),
        _stat_row("RUNS THIS PREP", h.get("runs_this_prep","")),
        _stat_row("FIRST UP", "Yes" if h.get("first_up") else ""),
        _stat_row("SECOND UP", "Yes" if h.get("second_up") else ""),
        _stat_row("RUN STYLE", h.get("run_style","")),
        _stat_row("EARLY SPEED", h.get("early_speed","")),
        _stat_row("MAP SCORE", h.get("map_score","")),
        _stat_row("OPENING ODDS", _fmt_odds(h.get("opening_odds",""))),
        _stat_row("MID ODDS", _fmt_odds(h.get("mid_odds",""))),
        _stat_row("CLOSING ODDS", _fmt_odds(h.get("closing_odds",""))),
        _stat_row("MARKET DRIFT", _fmt_pct(h.get("market_drift",""))),
        _stat_row("BLINKERS ON", "Yes" if h.get("blinkers_on") else ""),
        _stat_row("BLINKERS OFF", "Yes" if h.get("blinkers_off") else ""),
        _stat_row("TONGUE TIE", "Yes" if h.get("tongue_tie") else ""),
        _stat_row("VISORS", "Yes" if h.get("visors") else ""),
        _stat_row("GEAR CHANGE", "Yes" if h.get("first_gear") else ""),
        _stat_row("LAST RATING", h.get("last_rating","")),
        _stat_row("AVG RATING", h.get("avg_rating","")),
        _stat_row("AVG MARGIN LOSS", h.get("avg_margin_loss","")),
        _stat_row("SIRE WIN % DRY", _fmt_pct(h.get("sire_win_dry",""))),
        _stat_row("SIRE WIN % WET", _fmt_pct(h.get("sire_win_wet",""))),
        _stat_row("TRACK CONDITION", h.get("track_condition","")),
    ])
    notes = (h.get("stewards_notes","") or "").strip()
    notes_html = f'<div class="hnotes"><span class="hsl">STEWARDS NOTES</span><p>{notes}</p></div>' if notes else ""
    if not chips and not notes_html:
        return '<div class="hstat-empty">No additional stats for this horse yet</div>'
    return f'<div class="hstat-grid">{chips}</div>{notes_html}'

def _pushed_str(store):
    try: return datetime.datetime.fromisoformat(store["last_push"]).strftime("%d %b  %H:%M")
    except: return "Never"

def _tips_sent_str(store):
    """When the Discord tips were last posted."""
    try: return datetime.datetime.fromisoformat(store.get("discord_updated") or store.get("tips_updated") or store["last_push"]).strftime("%d %b  %H:%M")
    except: return "Never"

def _dc_f(v, default=0.0):
    try: return float(v)
    except (TypeError, ValueError): return default

def _dc_day_prefix(t):
    """'SAT 07 NOV - ' in front of a tip's time when it is not for today."""
    try:
        d = datetime.date.fromisoformat(t.get("race_date", ""))
        if d != datetime.datetime.now(NOTIFY_TZ).date():
            return d.strftime("%a %d %b").upper() + " &middot; "
    except Exception:
        pass
    return ""

def _race_id(r):
    """Stable id for a race, independent of list ordering/sort."""
    raw = f'{r.get("track","")}|{r.get("race","")}|{r.get("time","")}'
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]

def _time_key(tstr):
    """Numeric sort key for race times.
    Assumes:
      - 11:xx and 12:xx are before 1 PM.
      - 1:xx-10:xx are afternoon races (13:xx-22:xx) unless AM/PM is specified.
    """
    try:
        t = str(tstr).strip().upper()

        if t.endswith("AM") or t.endswith("PM"):
            dt = datetime.datetime.strptime(t, "%I:%M %p")
            return dt.hour * 60 + dt.minute

        parts = t.split(":")
        h = int(parts[0])
        m = int(parts[1][:2]) if len(parts) > 1 else 0

        if 1 <= h <= 10:
            h += 12

        return h * 60 + m

    except Exception:
        return 99999.0

def _poll_script(push_count, page_id):
    """Cheap status poll every 20s; only reloads when push_count changed, and
    stashes scroll position / active tab / open rows first."""
    if page_id == "watch":
        return ""
    return """
(function(){
  var _initialPush=__PUSH__;
  function _saveUIState(){
    try{
      var state={scroll:window.scrollY,open:[]};
      document.querySelectorAll('.rbody.open,.hdetail-row.open').forEach(function(el){ if(el.id) state.open.push(el.id); });
      var activeTab=document.querySelector('.tab.active');
      if(activeTab && activeTab.id) state.tab=activeTab.id;
      sessionStorage.setItem('thepost_ui_state', JSON.stringify(state));
    }catch(e){}
  }
  function _restoreUIState(){
    try{
      var raw=sessionStorage.getItem('thepost_ui_state');
      if(!raw) return;
      sessionStorage.removeItem('thepost_ui_state');
      var state=JSON.parse(raw);
      if(state.tab){
        var btn=document.getElementById(state.tab);
        if(btn) btn.click();
      }
      (state.open||[]).forEach(function(id){
        var el=document.getElementById(id);
        if(el){
          el.classList.add('open');
          if(el.previousElementSibling) el.previousElementSibling.classList.add('open');
        }
      });
      if(typeof state.scroll==='number'){
        setTimeout(function(){ window.scrollTo(0,state.scroll); },60);
      }
    }catch(e){}
  }
  _restoreUIState();
  function _poll(){
    fetch('/api/status').then(function(r){return r.json();}).then(function(s){
      if(s && typeof s.push_count==='number' && s.push_count!==_initialPush){
        _saveUIState();
        location.reload();
      }
    }).catch(function(){});
  }
  setInterval(_poll, 20000);
})();
""".replace("__PUSH__", str(push_count))

def _shell(page_id, body, store, friend=False):
    pushed = _pushed_str(store)
    total  = len(_live_tips(store))
    share_btn = '<button class="sbtn" onclick="openShare()">&#x2197; Share</button>' if page_id=="tips" else ""
    export_btn = '<button class="sbtn ebtn" onclick="exportPhoto()">&#x1F4F7; Export</button>' if page_id=="tips" else ""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,minimum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="The Post">
<meta name="theme-color" content="#0B0F14">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.png">
<link rel="shortcut icon" href="/icon.png">
<title>The Post</title>
<style>
:root{--bg:#0B0F14;--panel:#121821;--el:#1A222D;--bd:#232C38;--t1:#E6EDF3;--t2:#8B98A5;--green:#2ECC71;--red:#E74C3C;--acc:#3A82F7;--warn:#F0A500;}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}
html{touch-action:pan-x pan-y;overscroll-behavior-y:contain;}
html,body{
  -webkit-user-select:none;-moz-user-select:none;-ms-user-select:none;user-select:none;
  -webkit-touch-callout:none;
}
body{font-family:-apple-system,'Segoe UI',Arial,sans-serif;background:var(--bg);color:var(--t1);min-height:100vh;padding-bottom:62px;font-size:12.5px;-webkit-text-size-adjust:100%;text-size-adjust:100%;}
img{-webkit-user-drag:none;user-drag:none;pointer-events:none;}
.header{background:var(--panel);border-bottom:1px solid var(--bd);padding:calc(env(safe-area-inset-top) + 12px) 16px 12px;position:sticky;top:0;z-index:50;box-shadow:0 1px 0 rgba(0,0,0,.35);}
.hrow{display:flex;align-items:center;justify-content:space-between;gap:10px;}
.appname{font-size:17px;font-weight:800;letter-spacing:-.2px;display:flex;align-items:center;}
.dot{width:6px;height:6px;background:var(--green);border-radius:50%;display:inline-block;margin-right:6px;animation:pulse 2s infinite;flex-shrink:0;}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.status{font-size:10.5px;color:var(--t2);margin-top:3px;}
.hbtns{display:flex;align-items:center;gap:6px;flex-shrink:0;}
.rbtn{background:var(--el);border:1px solid var(--bd);color:var(--t1);width:30px;height:30px;border-radius:8px;font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;line-height:1;}
.sbtn{background:#1a3a1a;border:1px solid #2ECC71;color:#2ECC71;height:30px;padding:0 12px;border-radius:8px;font-size:11.5px;font-weight:700;cursor:pointer;display:flex;align-items:center;justify-content:center;white-space:nowrap;}
.sbtn.ebtn{background:#1A2E4A;border:1px solid var(--acc);color:var(--acc);}
.navbar{position:fixed;bottom:0;left:0;right:0;background:var(--panel);border-top:1px solid var(--bd);display:flex;z-index:100;padding-bottom:env(safe-area-inset-bottom);}
.nbtn{flex:1;padding:9px 4px 8px;font-size:9.5px;color:var(--t2);background:none;border:none;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:2px;}
.nbtn.active{color:var(--acc);}
.ni{font-size:18px;line-height:1;}
.tabs{display:flex;background:var(--panel);border-bottom:1px solid var(--bd);overflow-x:auto;scrollbar-width:none;}
.tabs::-webkit-scrollbar{display:none;}
.tab{flex:1;min-width:64px;padding:9px 4px;font-size:11.5px;font-weight:600;color:var(--t2);background:none;border:none;border-bottom:2px solid transparent;cursor:pointer;white-space:nowrap;}
.tab.active{color:var(--t1);border-bottom-color:var(--acc);}
.section{display:none;}.section.active{display:block;}
.content{padding:10px 12px;}
.sortbar{display:flex;gap:5px;padding:8px 12px 4px;overflow-x:auto;scrollbar-width:none;flex-wrap:nowrap;background:var(--bg);}
.sortbar::-webkit-scrollbar{display:none;}
.sort-btn{background:var(--el);border:1px solid var(--bd);color:var(--t2);padding:4px 10px;border-radius:20px;font-size:10.5px;font-weight:600;cursor:pointer;white-space:nowrap;flex-shrink:0;}
.sort-btn.active{background:#1A2E4A;border-color:var(--acc);color:var(--acc);}
.card{background:var(--panel);border:1px solid var(--bd);border-radius:9px;padding:9px 10px;margin-bottom:7px;}
.ctop{display:flex;align-items:center;justify-content:space-between;gap:6px;margin-bottom:2px;}
.horse-row{display:inline-flex;align-items:center;gap:6px;min-width:0;}
.horse{font-size:13.5px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.silk-wrap{display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;border-radius:4px;overflow:hidden;background:transparent;vertical-align:middle;}
.silk-img{width:100%;height:100%;object-fit:contain;display:block;image-rendering:auto;}
.tag{font-size:9px;font-weight:700;padding:2px 7px;border-radius:20px;letter-spacing:.3px;text-transform:uppercase;flex-shrink:0;white-space:nowrap;}
.tag.top-play{background:#1a3a1a;color:var(--green);}
.tag.secondary{background:#1a2a4a;color:var(--acc);}
.tag.watch{background:#2a2210;color:var(--warn);}
.meta{font-size:10px;color:var(--t2);margin-bottom:7px;}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:5px;}
.stat{background:var(--el);border-radius:6px;padding:5px 3px;text-align:center;}
.sl{display:block;font-size:8px;font-weight:700;color:var(--t2);letter-spacing:.4px;margin-bottom:2px;}
.sv{font-size:12px;font-weight:700;white-space:nowrap;}
.pos{color:var(--green);}.neg{color:var(--red);}
.summary{display:flex;gap:6px;margin-bottom:10px;}
.sc{flex:1;background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:9px 4px 8px;text-align:center;transition:transform .1s;}
.sc-link{cursor:pointer;}
.sc-link:active{transform:scale(.97);}
.sn{font-size:20px;font-weight:800;line-height:1.1;}.sl2{font-size:9px;color:var(--t2);text-transform:uppercase;letter-spacing:.4px;margin-top:1px;}
.sc-pct{font-size:8.5px;color:var(--t2);margin-top:3px;opacity:.75;}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:10px;}
.spotlight-card{border-left:3px solid var(--green);background:linear-gradient(135deg,rgba(46,204,113,.08),var(--panel) 60%);}
.spot-top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:2px;}
.spot-horse{font-size:16px;font-weight:800;}
.spot-meta{font-size:10.5px;color:var(--t2);margin-bottom:9px;}
.spot-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;}
.spot-item{background:var(--el);border-radius:6px;padding:6px 3px;text-align:center;display:flex;flex-direction:column;gap:2px;}
.type-bar{display:flex;width:100%;height:9px;border-radius:5px;overflow:hidden;background:var(--el);margin-bottom:9px;}
.type-seg{height:100%;}
.type-legend{display:flex;gap:14px;flex-wrap:wrap;font-size:10.5px;color:var(--t2);}
.type-legend span{display:inline-flex;align-items:center;gap:5px;}
.type-legend i{width:8px;height:8px;border-radius:50%;display:inline-block;}
.stat-card{background:var(--panel);border:1px solid var(--bd);border-radius:9px;padding:10px 11px;border-left:3px solid var(--acc);}
.stat-card.green{border-left-color:var(--green);}
.stat-card.red{border-left-color:var(--red);}
.stat-card.warn{border-left-color:var(--warn);}
.stat-label{font-size:9px;color:var(--t2);text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px;}
.stat-value{font-size:18px;font-weight:700;}
.stat-sub{font-size:10px;color:var(--t2);margin-top:2px;}
.nr-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 2px;border-bottom:1px solid var(--bd);}
.nr-row:last-child{border-bottom:none;}
.nr-link{cursor:pointer;}
.nr-name{font-size:11.5px;color:var(--t1);line-height:1.4;}
.nr-arrow{color:var(--acc);font-size:14px;flex-shrink:0;}
.rblock{background:var(--panel);border:1px solid var(--bd);border-radius:9px;margin-bottom:7px;overflow:hidden;}
.rhdr{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:9px 11px;cursor:pointer;}
.rleft{font-size:12px;font-weight:700;}
.rmeta{font-size:10px;color:var(--t2);margin-top:1px;}
.rhdr-right{display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex-shrink:0;}
.cd{font-size:9.5px;font-weight:700;color:var(--t2);white-space:nowrap;}
.cd.cd-orange{color:var(--warn);}
.cd.cd-red{color:var(--red);}
.rbody{display:none;border-top:1px solid var(--bd);overflow-x:auto;}
.rbody.open{display:block;}
.tbl{width:100%;border-collapse:collapse;font-size:10.5px;}
.tbl th{padding:6px 6px;text-align:left;color:var(--t2);font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.3px;background:var(--el);border-bottom:1px solid var(--bd);white-space:nowrap;}
.tbl td{padding:5px 6px;border-bottom:1px solid var(--bd);vertical-align:middle;white-space:nowrap;}
.tbl td.silk-cell{width:30px;padding:5px 2px 5px 6px;}
.tbl tr:last-child td{border-bottom:none;}
.tbl .top td:first-child{color:var(--warn);font-weight:700;}
.vp{color:var(--green);font-weight:600;}.vn{color:var(--red);}
.ar{text-align:right;font-variant-numeric:tabular-nums;}
.rb{font-size:10px;font-weight:700;padding:3px 8px;border-radius:20px;flex-shrink:0;}
.re{background:#1a3a1a;color:var(--green);}
.rs{background:#1a2a4a;color:var(--acc);}
.rm{background:#2a2210;color:var(--warn);}
.rl{background:#2a1a1a;color:var(--red);}
.empty{color:var(--t2);text-align:center;padding:34px 0;font-size:12.5px;}
.horse-row-tr{cursor:pointer;}
.hcaret{color:var(--t2);font-size:9px;margin-left:3px;display:inline-block;transition:transform .15s;}
.horse-row-tr.open .hcaret{transform:rotate(180deg);color:var(--acc);}
.hdetail-row{display:none;background:var(--bg);}
.hdetail-row.open{display:table-row;}
.hdetail-row td{padding:10px 10px 12px;border-bottom:1px solid var(--bd);white-space:normal;}
.hstat-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px 10px;}
.hstat{background:var(--el);border-radius:6px;padding:5px 8px;display:flex;flex-direction:column;gap:1px;min-width:0;}
.hsl{font-size:8px;font-weight:700;color:var(--t2);letter-spacing:.4px;}
.hsv{font-size:11.5px;font-weight:600;color:var(--t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.hstat-empty{color:var(--t2);font-size:11px;padding:6px 2px;}
.hnotes{margin-top:8px;background:var(--el);border-radius:6px;padding:6px 8px;}
.hnotes p{font-size:11px;color:var(--t1);line-height:1.4;margin-top:3px;}
.multi-legs{display:flex;flex-direction:column;gap:6px;margin-bottom:8px;}
.multi-leg{display:flex;align-items:center;gap:8px;background:var(--el);border-radius:6px;padding:6px 8px;}
.multi-leg-info{flex:1;min-width:0;}
.multi-leg-horse{font-size:11.5px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.multi-leg-meta{font-size:9.5px;color:var(--t2);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.multi-leg-odds{font-size:12px;font-weight:700;color:var(--acc);flex-shrink:0;}
.multi-foot{display:flex;gap:6px;}
.mf-item{flex:1;background:var(--el);border-radius:6px;padding:6px 3px;text-align:center;display:flex;flex-direction:column;gap:2px;}
.multi-card .horse{font-size:13.5px;font-weight:700;}
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;align-items:flex-end;justify-content:center;}
.modal-bg.open{display:flex;}
.modal{background:var(--panel);border-radius:16px 16px 0 0;padding:20px 16px 32px;width:100%;max-width:480px;}
.modal-title{font-size:16px;font-weight:700;margin-bottom:16px;text-align:center;}
.share-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
.share-btn{background:var(--el);border:1px solid var(--bd);border-radius:10px;padding:14px 10px;text-align:center;cursor:pointer;color:var(--t1);font-size:12px;font-weight:600;text-decoration:none;display:block;}
.share-icon{font-size:24px;display:block;margin-bottom:6px;}
.modal-close{width:100%;margin-top:14px;padding:12px;background:var(--el);border:1px solid var(--bd);border-radius:10px;color:var(--t2);font-size:14px;cursor:pointer;}
#export-stage{position:fixed;top:0;left:0;width:390px;opacity:0;pointer-events:none;z-index:-1;background:var(--bg);}
#export-toast{position:fixed;left:50%;bottom:80px;transform:translateX(-50%);background:var(--panel);border:1px solid var(--bd);color:var(--t1);padding:10px 16px;border-radius:20px;font-size:12px;font-weight:600;z-index:300;display:none;box-shadow:0 4px 20px rgba(0,0,0,.4);}
.video-frame{position:relative;width:100%;padding-top:56.25%;background:#000;border-radius:9px;overflow:hidden;border:1px solid var(--bd);}
.video-frame iframe{position:absolute;top:0;left:0;width:100%;height:100%;border:0;}
.watch-pane{display:none;}
.watch-pane.active{display:block;}
.watch-fallback{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:8px;padding:9px 11px;background:var(--panel);border:1px solid var(--bd);border-radius:8px;}
.watch-fallback span{font-size:10.5px;color:var(--t2);}
.wbtn{background:#1A2E4A;border:1px solid var(--acc);color:var(--acc);padding:7px 13px;border-radius:8px;font-size:11px;font-weight:700;text-decoration:none;white-space:nowrap;flex-shrink:0;}
.watch-note{font-size:10.5px;color:var(--t2);line-height:1.5;padding:2px 2px 10px;}
.wtab-logo{height:18px;width:auto;vertical-align:middle;display:inline-block;}
.settings-row{display:flex;align-items:center;justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--bd);gap:10px;}
.settings-row:last-of-type{border-bottom:none;}
.settings-row>span{font-size:12.5px;}
.switch{position:relative;display:inline-block;width:42px;height:24px;flex-shrink:0;}
.switch input{opacity:0;width:0;height:0;}
.slider{position:absolute;cursor:pointer;inset:0;background:var(--el);border:1px solid var(--bd);border-radius:24px;transition:.15s;}
.slider:before{position:absolute;content:"";height:18px;width:18px;left:2px;top:2px;background:var(--t2);border-radius:50%;transition:.15s;}
input:checked + .slider{background:#1a3a1a;border-color:var(--green);}
input:checked + .slider:before{transform:translateX(18px);background:var(--green);}
.mins-input{width:56px;background:var(--el);border:1px solid var(--bd);color:var(--t1);border-radius:6px;padding:5px;text-align:center;font-size:12.5px;}
.push-status{font-size:10.5px;color:var(--t2);margin-top:8px;}
.pnl-big{font-size:30px;font-weight:800;line-height:1.1;}
.pnl-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 11px;cursor:pointer;}
.pnl-caret{color:var(--t2);font-size:14px;transition:transform .15s;}
.pnl-head.open .pnl-caret{transform:rotate(180deg);color:var(--acc);}
.pnl-row{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 2px;border-bottom:1px solid var(--bd);}
.pnl-row:last-child{border-bottom:none;}
.pnl-main{min-width:0;flex:1;}
.pnl-horse{font-size:12.5px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.pnl-sub{font-size:10px;color:var(--t2);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.pnl-val{font-size:13px;font-weight:700;flex-shrink:0;}
.pnl-tag{font-size:9px;font-weight:700;padding:2px 7px;border-radius:20px;flex-shrink:0;}
.pnl-tag.win{background:#1a3a1a;color:var(--green);}
.pnl-tag.loss{background:#2a1a1a;color:var(--red);}
</style>
</head>
<body>
<div class="header">
  <div class="hrow">
    <div class="appname"><span class="dot"></span>The Post</div>
    <div class="hbtns">""" + export_btn + share_btn + """
      <button class="rbtn" onclick="location.reload()">&#x21BB;</button>
    </div>
  </div>
  <div class="status">Tips sent: """ + _tips_sent_str(store) + """ &nbsp;&middot;&nbsp; """ + str(total) + """ tip""" + ("s" if total!=1 else "") + """</div>
</div>
""" + body + """
<div class="modal-bg" id="share-modal">
  <div class="modal">
    <div class="modal-title">Share Tips</div>
    <div class="share-grid">
      <a class="share-btn" onclick="shareVia('whatsapp')"><span class="share-icon">&#x1F4AC;</span>WhatsApp</a>
      <a class="share-btn" onclick="shareVia('sms')"><span class="share-icon">&#x1F4F1;</span>SMS</a>
      <a class="share-btn" onclick="shareVia('email')"><span class="share-icon">&#x2709;&#xFE0F;</span>Email</a>
      <a class="share-btn" onclick="shareVia('copy')"><span class="share-icon">&#x1F4CB;</span>Copy Text</a>
    </div>
    <button class="modal-close" onclick="closeShare()">Cancel</button>
  </div>
</div>
<div id="export-toast">Generating photo&hellip;</div>
<div id="export-stage"></div>
<nav class="navbar">
""" + ("""  <button class="nbtn """ + ("active" if page_id=="dash" else "") + """ " onclick="location.href='/portal/dash'"><span class="ni">&#x1F4CA;</span>Dashboard</button>
  <button class="nbtn """ + ("active" if page_id=="tips" else "") + """ " onclick="location.href='/portal/tips'"><span class="ni">&#x1F3C7;</span>Tips</button>
  <button class="nbtn """ + ("active" if page_id=="watch" else "") + """ " onclick="location.href='/portal/watch'"><span class="ni">&#x1F4FA;</span>Watch</button>
  <button class="nbtn """ + ("active" if page_id=="settings" else "") + """ " onclick="location.href='/settings'"><span class="ni">&#x2699;&#xFE0F;</span>Settings</button>
""" if friend else """  <button class="nbtn """ + ("active" if page_id=="dash" else "") + """ " onclick="location.href='/dash'"><span class="ni">&#x1F4CA;</span>Dashboard</button>
  <button class="nbtn """ + ("active" if page_id=="tips" else "") + """ " onclick="location.href='/tips'"><span class="ni">&#x1F3C7;</span>Tips</button>
  <button class="nbtn """ + ("active" if page_id=="analyzer" else "") + """ " onclick="location.href='/analyzer'"><span class="ni">&#x1F50D;</span>Analyzer</button>
  <button class="nbtn """ + ("active" if page_id=="watch" else "") + """ " onclick="location.href='/watch'"><span class="ni">&#x1F4FA;</span>Watch</button>
  <button class="nbtn """ + ("active" if page_id=="settings" else "") + """ " onclick="location.href='/settings'"><span class="ni">&#x2699;&#xFE0F;</span>Settings</button>
""") + """</nav>
<script>
var _sortKey='track',_sortDir=1,_activeContainer='cards-container';
function tog(id){document.getElementById(id).classList.toggle('open');}
function togHorse(id){
  var row=document.getElementById(id);
  if(!row) return;
  var open=row.classList.toggle('open');
  var trigger=row.previousElementSibling;
  if(trigger) trigger.classList.toggle('open', open);
}
function openShare(){document.getElementById('share-modal').classList.add('open');}
function closeShare(){document.getElementById('share-modal').classList.remove('open');}
function buildShareText(){
  var lines=['The Post - Tips\\n'],pushed=document.querySelector('.status');
  if(pushed) lines.push(pushed.textContent.trim()+'\\n');
  var containers=['cards-container','cards-container-p','cards-container-m'];
  containers.forEach(function(cid){
    var c=document.getElementById(cid);
    if(!c||c.closest('.section:not(.active)')) return;
    var cards=c.querySelectorAll('.sortable-card');
    cards.forEach(function(card){
      var h=card.dataset.horse,r=card.dataset.race,t=card.dataset.time;
      var o=parseFloat(card.dataset.real_odds).toFixed(2),u=card.dataset.units;
      lines.push(h+' @ $'+o+' ('+u+'u) - '+t+' '+r);
    });
  });
  lines.push('\\nthepost-server.onrender.com');
  return lines.join('\\n');
}
function shareVia(method){
  var txt=buildShareText(),enc=encodeURIComponent(txt);
  if(method==='whatsapp') window.open('https://wa.me/?text='+enc);
  else if(method==='sms') window.open('sms:?&body='+enc);
  else if(method==='email') window.open('mailto:?subject=The+Post+Tips&body='+enc);
  else if(method==='copy') navigator.clipboard.writeText(txt).then(function(){alert('Copied!');});
  closeShare();
}
function setContainer(id){var m={tb:'cards-container',tp:'cards-container-p',tm:'cards-container-m'};_activeContainer=m[id]||'cards-container';}
function switchTab(id,btn,grp){
  document.querySelectorAll('[data-grp="'+grp+'"]').forEach(function(s){s.classList.remove('active');});
  document.querySelectorAll('[data-tab="'+grp+'"]').forEach(function(b){b.classList.remove('active');});
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}
function sortBy(key,btn){
  if(_sortKey===key){_sortDir*=-1;}else{_sortKey=key;_sortDir=1;}
  document.querySelectorAll('.sort-btn').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  var c=document.getElementById(_activeContainer);
  if(!c) return;
  var cards=[].slice.call(c.querySelectorAll('.sortable-card'));
  cards.sort(function(a,b){
    var av=a.dataset[key]||'',bv=b.dataset[key]||'';
    var an=parseFloat(av),bn=parseFloat(bv);
    if(!isNaN(an)&&!isNaN(bn)) return (an-bn)*_sortDir;
    return av.localeCompare(bv)*_sortDir;
  });
  cards.forEach(function(c){document.getElementById(_activeContainer).appendChild(c);});
}
function sortAnalyzer(key,btn){
  document.querySelectorAll('.asort-btn').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  var c=document.getElementById('races-container');
  if(!c) return;
  var blocks=[].slice.call(c.querySelectorAll('.rblock'));
  blocks.sort(function(a,b){
    if(key==='time'){
      var at=parseFloat(a.dataset.timekey),bt=parseFloat(b.dataset.timekey);
      if(isNaN(at)) at=99999; if(isNaN(bt)) bt=99999;
      return at-bt;
    }
    return (a.dataset[key]||'').localeCompare(b.dataset[key]||'');
  });
  blocks.forEach(function(b){c.appendChild(b);});
}

// ---------------------------------------------------------------------------
// Export Photo - renders the current tab's tips into an offscreen replica of
// the tips page with the RSI and VALUE stats stripped out of each card and the
// header swapped for the app logo, then rasterizes it to a PNG.
// ---------------------------------------------------------------------------
var _logoCutoutCache=null;
function _isIOS(){
  return /iP(hone|od|ad)/.test(navigator.userAgent) ||
    (navigator.platform==='MacIntel' && navigator.maxTouchPoints>1);
}
function _isCanvasBlank(canvas){
  try{
    var ctx=canvas.getContext('2d');
    var d=ctx.getImageData(0,0,canvas.width,canvas.height).data;
    for(var i=3;i<d.length;i+=811){ if(d[i]!==0) return false; }
    return true;
  }catch(e){ return false; }
}
function _seamlessLogo(){
  // Keys the black square of /icon.png out into real transparency
  // (alpha = luminance) so the mark floats on the dark background.
  if(_logoCutoutCache) return Promise.resolve(_logoCutoutCache);
  return new Promise(function(resolve){
    var img=new Image();
    img.onload=function(){
      try{
        var c=document.createElement('canvas');
        c.width=img.naturalWidth; c.height=img.naturalHeight;
        var ctx=c.getContext('2d');
        ctx.drawImage(img,0,0);
        var frame=ctx.getImageData(0,0,c.width,c.height);
        var d=frame.data;
        for(var i=0;i<d.length;i+=4){
          var lum=0.299*d[i]+0.587*d[i+1]+0.114*d[i+2];
          d[i+3]=Math.round(lum*(d[i+3]/255));
        }
        ctx.putImageData(frame,0,0);
        _logoCutoutCache=c.toDataURL('image/png');
      }catch(e){
        _logoCutoutCache='/icon.png';
      }
      resolve(_logoCutoutCache);
    };
    img.onerror=function(){ resolve('/icon.png'); };
    img.src='/icon.png';
  });
}

function _loadHtml2Canvas(){
  // Loaded on demand (only when Export is actually tapped).
  if(typeof html2canvas!=='undefined') return Promise.resolve();
  if(window._h2cLoadPromise) return window._h2cLoadPromise;
  window._h2cLoadPromise=new Promise(function(resolve,reject){
    var s=document.createElement('script');
    s.src='https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js';
    s.onload=function(){resolve();};
    s.onerror=function(){ window._h2cLoadPromise=null; reject(new Error('load-failed')); };
    document.head.appendChild(s);
  });
  return window._h2cLoadPromise;
}
function exportPhoto(){
  _loadHtml2Canvas().then(_runExportPhoto).catch(function(){
    alert('Export library failed to load - check your connection.');
  });
}
function _runExportPhoto(){
  var activeSection=document.querySelector('.section.active');
  if(!activeSection){ alert('Nothing to export yet.'); return; }

  var toast=document.getElementById('export-toast');
  toast.style.display='block';

  var clone=activeSection.cloneNode(true);
  clone.classList.add('active');

  // Strip RSI + VALUE stat tiles from every card, then rebalance the grid.
  clone.querySelectorAll('.card').forEach(function(card){
    card.querySelectorAll('.stat').forEach(function(s){
      var sl=s.querySelector('.sl');
      if(sl && (sl.textContent==='RSI' || sl.textContent==='VALUE')) s.remove();
    });
    var grid=card.querySelector('.stats');
    if(grid) grid.style.gridTemplateColumns='repeat('+grid.children.length+',1fr)';
  });
  clone.querySelectorAll('img').forEach(function(img){
    img.removeAttribute('loading');
    img.loading='eager';
  });

  var stage=document.getElementById('export-stage');
  stage.innerHTML='';

  var page=document.createElement('div');
  page.style.width='390px';
  page.style.background='var(--bg)';
  page.style.fontFamily=getComputedStyle(document.body).fontFamily;
  page.style.color='var(--t1)';
  page.style.fontSize='12.5px';

  var logoWrap=document.createElement('div');
  logoWrap.style.textAlign='center';
  logoWrap.style.padding='26px 0 18px';
  var logoImg=document.createElement('img');
  logoImg.style.cssText='width:132px;height:auto;display:inline-block;';
  logoWrap.appendChild(logoImg);

  var divider=document.createElement('div');
  divider.style.cssText='height:1px;margin:0 20px 4px;background:linear-gradient(90deg,transparent,rgba(230,237,243,.16),transparent);';

  var content=document.createElement('div');
  content.className='content';
  content.appendChild(clone);

  var footer=document.createElement('div');
  footer.style.cssText='text-align:center;padding:6px 16px 24px;';
  footer.innerHTML=
    '<div style="height:1px;margin:4px 4px 12px;background:linear-gradient(90deg,transparent,rgba(230,237,243,.16),transparent);"></div>'+
    '<div style="font-size:10px;letter-spacing:.4px;color:var(--t2);">The Post &middot; Racing Intelligence</div>';

  page.appendChild(logoWrap);
  page.appendChild(divider);
  page.appendChild(content);
  page.appendChild(footer);
  stage.appendChild(page);

  var exportChain=_seamlessLogo().then(function(logoSrc){
    logoImg.src=logoSrc;

    // Wait for every image (logo + silks) to finish loading, with a 3s cap each.
    var imgs=[].slice.call(page.querySelectorAll('img'));
    return Promise.all(imgs.map(function(img){
      if(img.complete && img.naturalWidth>0) return Promise.resolve();
      return new Promise(function(resolve){
        var done=false;
        var finish=function(){ if(!done){ done=true; resolve(); } };
        img.onload=finish;
        img.onerror=finish;
        setTimeout(finish,3000);
      });
    }));
  }).then(function(){
    // iOS Safari: try the native foreignObject renderer first, falling back to
    // the default renderer if it comes back blank.
    var bg=getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()||'#0B0F14';
    var iOS=_isIOS();
    var baseOpts={backgroundColor:bg,logging:false,imageTimeout:4000,scale:iOS?1.25:1.5};
    var foOpts=Object.assign({},baseOpts,{foreignObjectRendering:true});
    var render=iOS ? html2canvas(page,foOpts) : html2canvas(page,baseOpts);
    return render.then(function(canvas){
      if(iOS && _isCanvasBlank(canvas)) return html2canvas(page,baseOpts);
      return canvas;
    }).catch(function(){
      return html2canvas(page,baseOpts);
    });
  }).then(function(canvas){
    return new Promise(function(resolve,reject){
      canvas.toBlob(function(blob){
        if(!blob){ reject(new Error('empty blob')); return; }
        resolve(blob);
      },'image/png');
    });
  });

  // Hard ceiling on the whole process so a stuck image can't freeze the toast.
  var watchdog=new Promise(function(_,reject){
    setTimeout(function(){ reject(new Error('export-timeout')); },22000);
  });

  Promise.race([exportChain,watchdog]).then(function(blob){
    stage.innerHTML='';
    toast.style.display='none';
    var fname='thepost-tips-'+Date.now()+'.png';
    var file=new File([blob],fname,{type:'image/png'});
    if(navigator.canShare && navigator.canShare({files:[file]})){
      navigator.share({files:[file],title:'The Post Tips'}).catch(function(){});
    } else {
      var url=URL.createObjectURL(blob);
      var link=document.createElement('a');
      link.href=url;
      link.download=fname;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setTimeout(function(){URL.revokeObjectURL(url);},4000);
    }
  }).catch(function(err){
    stage.innerHTML='';
    toast.style.display='none';
    if(err && err.message==='export-timeout'){
      alert('Export took too long and was cancelled - please try again.');
    } else {
      alert('Export failed - please try again.');
    }
  });
}
""" + _poll_script(store.get("push_count", 0), page_id) + """
</script>
<script src="/push-heal.js"></script>
</body></html>"""

def _cards_js(tips_list, label, container_id, odds_label="ODDS"):
    if not tips_list:
        return f'<p class="empty">No {label} picks yet</p>'
    out = f'<div id="{container_id}">'
    for t in tips_list:
        tag_val = t.get("tag") or ""
        cls = tag_val.lower().replace(" ","-")
        # SECONDARY and TOP PLAY badges are hidden - WATCH (and anything else) still shows.
        tag_html = f'<span class="tag {cls}">{tag_val}</span>' if tag_val and tag_val not in ("TOP PLAY", "SECONDARY") else ""
        vc  = "pos" if t.get("value_pct",0)>0 else "neg"
        number = _get_horse_number(t)
        horse_label = f'{number}. {t.get("horse","")}' if number != "" else t.get("horse","")
        out += (
            f'<div class="card sortable-card"'
            f' data-time="{t.get("time","")}"'
            f' data-units="{t.get("units",0)}"'
            f' data-track="{t.get("track","")}"'
            f' data-race="{t.get("race","")}"'
            f' data-win_pct="{t.get("win_pct",0)}"'
            f' data-rsi="{t.get("rsi",0)}"'
            f' data-real_odds="{t.get("real_odds",0)}"'
            f' data-horse="{t.get("horse","")}">'
            f'<div class="ctop"><span class="horse-row">{_silk_html(_get_silk_url(t))}<span class="horse">{horse_label}</span></span>{tag_html}</div>'
            f'<div class="meta">{_dc_day_prefix(t)}{t.get("time","")} &middot; {t.get("track","")} &middot; {t.get("race","")}</div>'
            f'<div class="stats">'
            f'<div class="stat"><span class="sl">{odds_label}</span><span class="sv">${_dc_f(t.get("real_odds")):.2f}</span></div>'
            f'<div class="stat"><span class="sl">UNITS</span><span class="sv">{_dc_f(t.get("units"), 1):g}u</span></div>'
            f'<div class="stat"><span class="sl">VALUE</span><span class="sv {vc}">{_dc_f(t.get("value_pct")):+.1f}%</span></div>'
            f'<div class="stat"><span class="sl">RSI</span><span class="sv">{int(_dc_f(t.get("rsi")))}</span></div>'
            f'<div class="stat"><span class="sl">WIN%</span><span class="sv">{_dc_f(t.get("win_pct")):.1f}%</span></div>'
            f'</div></div>'
        )
    out += "</div>"
    return out

# ---------------------------------------------------------------------------
# Multi cards - one compact row per leg and a two-tile footer.
# ---------------------------------------------------------------------------

def _multi_leg_html(leg):
    number = _get_horse_number(leg)
    horse_label = f'{number}. {leg.get("horse","")}' if number != "" else leg.get("horse","")
    odds = leg.get("odds", leg.get("real_odds", 0)) or 0
    return (
        '<div class="multi-leg">'
        f'{_silk_html(_get_silk_url(leg))}'
        '<div class="multi-leg-info">'
        f'<div class="multi-leg-horse">{horse_label}</div>'
        f'<div class="multi-leg-meta">{leg.get("time","")} &middot; {leg.get("track","")} &middot; {leg.get("race","")}</div>'
        '</div>'
        f'<div class="multi-leg-odds">${float(odds):.2f}</div>'
        '</div>'
    )

def _multi_card_html(t):
    legs = t.get("legs", []) or []
    tag_cls = (t.get("tag") or "").lower().replace(" ","-")
    tag_html = f'<span class="tag {tag_cls}">{t["tag"]}</span>' if t.get("tag") else ""
    legs_html = "".join(_multi_leg_html(l) for l in legs)
    combined = float(t.get("combined_odds", 0) or 0)
    units = _dc_f(t.get("units"), 1)
    tracks = ", ".join(dict.fromkeys(l.get("track","") for l in legs if l.get("track")))
    first_time = legs[0].get("time","") if legs else t.get("time","")
    title = f'{len(legs)}-Leg Multi' if legs else "Multi"
    return (
        '<div class="card multi-card sortable-card"'
        f' data-time="{first_time}"'
        f' data-units="{units}"'
        f' data-track="{tracks}"'
        f' data-race="Multi"'
        f' data-win_pct="0"'
        f' data-rsi="{t.get("avg_rsi",0)}"'
        f' data-real_odds="{combined}"'
        f' data-horse="{title}">'
        f'<div class="ctop"><span class="horse-row"><span class="horse">{title}</span></span>{tag_html}</div>'
        f'<div class="meta">{_dc_day_prefix(t)}{tracks}</div>'
        f'<div class="multi-legs">{legs_html}</div>'
        '<div class="multi-foot">'
        f'<div class="mf-item"><span class="sl">COMBINED ODDS</span><span class="sv">${combined:.2f}</span></div>'
        f'<div class="mf-item"><span class="sl">UNITS</span><span class="sv">{units:g}u</span></div>'
        '</div>'
        '</div>'
    )

def _cards_multi_js(tips_list):
    if not tips_list:
        return '<p class="empty">No multis yet</p>'
    out = '<div id="cards-container-m">'
    for t in tips_list:
        out += _multi_card_html(t)
    out += "</div>"
    return out

def _tips_body(store):
    tips  = _live_tips(store)
    back  = sorted([t for t in tips if t.get("type")=="BACK"],  key=lambda t: t.get("track",""))
    place = sorted([t for t in tips if t.get("type")=="PLACE"], key=lambda t: t.get("track",""))
    multi = sorted(
        [t for t in tips if t.get("type")=="MULTI"],
        key=lambda t: -(_dc_f(t.get("avg_rsi")))
    )
    sort_bar = (
        '<div class="sortbar">'
        '<button class="sort-btn" onclick="sortBy(\'time\',this)">&#x1F550; Time</button>'
        '<button class="sort-btn" onclick="sortBy(\'units\',this)">Units</button>'
        '<button class="sort-btn active" onclick="sortBy(\'track\',this)">Track</button>'
        '<button class="sort-btn" onclick="sortBy(\'win_pct\',this)">Win%</button>'
        '<button class="sort-btn" onclick="sortBy(\'rsi\',this)">RSI</button>'
        '<button class="sort-btn" onclick="sortBy(\'real_odds\',this)">Odds</button>'
        '</div>'
    )
    return (
        '<div class="tabs">'
        f'<button id="btn-tb" class="tab active" data-tab="tips" onclick="switchTab(\'tb\',this,\'tips\');setContainer(\'tb\')">Back ({len(back)})</button>'
        f'<button id="btn-tp" class="tab" data-tab="tips" onclick="switchTab(\'tp\',this,\'tips\');setContainer(\'tp\')">Place ({len(place)})</button>'
        f'<button id="btn-tm" class="tab" data-tab="tips" onclick="switchTab(\'tm\',this,\'tips\');setContainer(\'tm\')">Multi ({len(multi)})</button>'
        '</div>'
        + sort_bar +
        '<div class="content">'
        '<div class="summary">'
        f'<div class="sc"><div class="sn" style="color:var(--green)">{len(back)}</div><div class="sl2">Back</div></div>'
        f'<div class="sc"><div class="sn" style="color:var(--acc)">{len(place)}</div><div class="sl2">Place</div></div>'
        f'<div class="sc"><div class="sn" style="color:var(--warn)">{len(multi)}</div><div class="sl2">Multi</div></div>'
        '</div>'
        f'<div class="section active" id="tb" data-grp="tips">{_cards_js(back,"back","cards-container")}</div>'
        f'<div class="section" id="tp" data-grp="tips">{_cards_js(place,"place","cards-container-p",odds_label="PLACE ODDS")}</div>'
        f'<div class="section" id="tm" data-grp="tips">{_cards_multi_js(multi)}</div>'
        '</div>'
        '<script>'
        '(function(){'
        'var params=new URLSearchParams(window.location.search);'
        'var tab=params.get("tab");'
        'if(tab){ var btn=document.getElementById("btn-"+tab); if(btn) btn.click(); }'
        '})();'
        '</script>'
    )

# v10: the old Discord tab is now the Tips page itself, so these just redirect
# (old bookmarks and home-screen links keep working).
@app.get("/discord")
async def discord_page():
    return RedirectResponse(url="/tips")

@app.get("/portal/discord")
async def portal_discord_page():
    return RedirectResponse(url="/portal/tips")

@app.get("/api/discord")
async def api_discord():
    await _refresh_store()
    return JSONResponse({"updated": _store.get("discord_updated"), "tips": _live_tips(_store)})

@app.get("/", response_class=HTMLResponse)
async def home_redirect():
    return RedirectResponse(url="/dash")

@app.get("/portal", response_class=HTMLResponse)
async def portal_home_redirect():
    return RedirectResponse(url="/portal/dash")

@app.get("/tips", response_class=HTMLResponse)
async def tips_page():
    await _refresh_store()
    return HTMLResponse(_cached_page("tips:" + _today_str(), lambda: _shell("tips", _tips_body(_store), _store)))

@app.get("/portal/tips", response_class=HTMLResponse)
async def portal_tips_page():
    await _refresh_store()
    return HTMLResponse(_cached_page("portal_tips:" + _today_str(), lambda: _shell("tips", _tips_body(_store), _store, friend=True)))

def _dash_body(store, friend=False):
    tips     = _live_tips(store)
    analyzer = store["analyzer"]
    back  = [t for t in tips if t.get("type")=="BACK"]
    place = [t for t in tips if t.get("type")=="PLACE"]
    multi = [t for t in tips if t.get("type")=="MULTI"]
    all_t = back+place+multi
    back_u   = sum(_dc_f(t.get("units")) for t in back)
    place_u  = sum(_dc_f(t.get("units")) for t in place)
    multi_u  = sum(_dc_f(t.get("units")) for t in multi)
    total_u  = back_u+place_u+multi_u
    best     = max(all_t, key=lambda t: _dc_f(t.get("units")), default=None)

    track_set = set()
    for t in all_t:
        if t.get("type")=="MULTI":
            for l in (t.get("legs") or []):
                if l.get("track"): track_set.add(l.get("track"))
        elif t.get("track"):
            track_set.add(t.get("track"))
    tracks   = ", ".join(sorted(track_set)) or "&mdash;"
    t_races  = len(analyzer)
    t_run    = sum(len(r.get("horses",[])) for r in analyzer)
    pushed   = _pushed_str(store)

    tips_base = "/portal/tips" if friend else "/tips"

    # Bet-type boxes are the route into Tips - clicking one jumps straight to that tab.
    def _sc(tid, count, color, label):
        pct = round(100*count/len(all_t)) if all_t else 0
        return (
            f'<div class="sc sc-link" style="border-top:2px solid {color};" '
            f'onclick="location.href=\'{tips_base}?tab={tid}\'">'
            f'<div class="sn" style="color:{color}">{count}</div>'
            f'<div class="sl2">{label}</div>'
            f'<div class="sc-pct">{pct}% of tips</div></div>'
        )

    # Best Bet spotlight - whichever selection is carrying the most stake.
    spotlight_html = ""
    if best:
        b_type = best.get("type","")
        type_color = "var(--green)" if b_type=="BACK" else ("var(--acc)" if b_type=="PLACE" else "var(--warn)")
        if b_type == "MULTI":
            legs = best.get("legs") or []
            leg_tracks = ", ".join(dict.fromkeys(l.get("track","") for l in legs if l.get("track")))
            title_name = f'{len(legs)}-Leg Multi'
            meta = f'{best.get("time","")} &middot; {leg_tracks}' if leg_tracks else best.get("time","")
            stats_html = (
                f'<div class="spot-item"><span class="hsl">ODDS</span><span class="hsv">${_dc_f(best.get("combined_odds")):.2f}</span></div>'
                f'<div class="spot-item"><span class="hsl">LEGS</span><span class="hsv">{len(legs)}</span></div>'
                f'<div class="spot-item"><span class="hsl">AVG RSI</span><span class="hsv">{int(_dc_f(best.get("avg_rsi")))}</span></div>'
                f'<div class="spot-item"><span class="hsl">UNITS</span><span class="hsv">{_dc_f(best.get("units"), 1):g}u</span></div>'
            )
        else:
            title_name = best.get("horse","")
            meta = f'{best.get("time","")} &middot; {best.get("track","")} &middot; {best.get("race","")}'
            odds_lbl = "PLACE ODDS" if b_type == "PLACE" else "ODDS"
            stats_html = (
                f'<div class="spot-item"><span class="hsl">{odds_lbl}</span><span class="hsv">${_dc_f(best.get("real_odds")):.2f}</span></div>'
                f'<div class="spot-item"><span class="hsl">VALUE</span><span class="hsv pos">{_dc_f(best.get("value_pct")):+.1f}%</span></div>'
                f'<div class="spot-item"><span class="hsl">RSI</span><span class="hsv">{int(_dc_f(best.get("rsi")))}</span></div>'
                f'<div class="spot-item"><span class="hsl">UNITS</span><span class="hsv">{_dc_f(best.get("units"), 1):g}u</span></div>'
            )
        spotlight_html = (
            '<div class="card spotlight-card" style="margin-bottom:9px;">'
            '<div class="stat-label" style="margin-bottom:6px;">&#x2B50; Best Bet</div>'
            '<div class="spot-top">'
            f'<div class="spot-horse">{title_name}</div>'
            f'<span class="tag" style="background:transparent;border:1px solid {type_color};color:{type_color};">{b_type}</span>'
            '</div>'
            f'<div class="spot-meta">{meta}</div>'
            f'<div class="spot-stats">{stats_html}</div>'
            '</div>'
        )

    # Units-by-type bar
    type_bar_html = ""
    if total_u > 0:
        back_pct  = round(100*back_u/total_u)
        place_pct = round(100*place_u/total_u)
        multi_pct = max(0, 100-back_pct-place_pct)
        type_bar_html = (
            '<div class="card" style="margin-bottom:9px;">'
            '<div class="stat-label" style="margin-bottom:8px;">Units By Type</div>'
            '<div class="type-bar">'
            f'<div class="type-seg" style="width:{back_pct}%;background:var(--green);"></div>'
            f'<div class="type-seg" style="width:{place_pct}%;background:var(--acc);"></div>'
            f'<div class="type-seg" style="width:{multi_pct}%;background:var(--warn);"></div>'
            '</div>'
            '<div class="type-legend">'
            f'<span><i style="background:var(--green);"></i>Back {back_u:.0f}u</span>'
            f'<span><i style="background:var(--acc);"></i>Place {place_u:.0f}u</span>'
            f'<span><i style="background:var(--warn);"></i>Multi {multi_u:.0f}u</span>'
            '</div>'
            '</div>'
        )

    # Next 5 races - every race is rendered as a hidden template row; client-side
    # JS drops races already started and shows the soonest 5, re-checking every 30s.
    all_sorted = sorted(analyzer, key=lambda r: _time_key(r.get("time","")))
    tmpl_rows = ""
    for r in all_sorted:
        rid = _race_id(r)
        label = f'{r.get("time","")} &middot; {r.get("track","")} &middot; {r.get("race","")}'
        href = "" if friend else f"/analyzer#race-{rid}"
        tmpl_rows += (
            f'<div class="nr-tmpl" data-time="{r.get("time","")}" data-href="{href}">{label}</div>'
        )
    next_html = (
        '<div class="card" style="margin-bottom:9px;">'
        '<div class="stat-label" style="margin-bottom:8px;">Next 5 Races</div>'
        '<div id="next-races-visible"><p class="empty" style="padding:14px 0;">Loading&hellip;</p></div>'
        f'<div id="next-races-all" style="display:none;">{tmpl_rows}</div>'
        '</div>'
        '''<script>
(function(){
  function parseTime(str){
    if(!str) return null;
    var t=String(str).trim().toUpperCase();
    var m=t.match(/(\\d{1,2}):(\\d{2})\\s*([AP]M)?/);
    if(!m) return null;
    var h=parseInt(m[1],10), mins=parseInt(m[2],10);
    if(m[3]){
      if(m[3]==='PM' && h<12) h+=12;
      if(m[3]==='AM' && h===12) h=0;
    } else if(h>=1 && h<=10){
      h+=12;
    }
    var now=new Date();
    var d=new Date(now.getFullYear(),now.getMonth(),now.getDate(),h,mins,0,0);
    var diff=d.getTime()-now.getTime();
    if(diff < -6*3600*1000){ d.setDate(d.getDate()+1); diff=d.getTime()-now.getTime(); }
    return diff;
  }
  function refresh(){
    var all=[].slice.call(document.querySelectorAll('#next-races-all .nr-tmpl'));
    var upcoming=all.map(function(el){
      return {diff:parseTime(el.getAttribute('data-time')), href:el.getAttribute('data-href'), label:el.innerHTML};
    }).filter(function(r){ return r.diff!==null && r.diff>0; });
    upcoming.sort(function(a,b){ return a.diff-b.diff; });
    upcoming=upcoming.slice(0,5);
    var vis=document.getElementById('next-races-visible');
    if(!vis) return;
    if(!upcoming.length){
      vis.innerHTML='<p class="empty" style="padding:14px 0;">No more races today</p>';
      return;
    }
    vis.innerHTML=upcoming.map(function(r){
      if(r.href){
        return '<div class="nr-row nr-link" onclick="location.href=\\''+r.href+'\\'"><span class="nr-name">'+r.label+'</span><span class="nr-arrow">&#x2192;</span></div>';
      }
      return '<div class="nr-row"><span class="nr-name">'+r.label+'</span></div>';
    }).join('');
  }
  refresh();
  setInterval(refresh, 30000);
})();
</script>'''
    ) if all_sorted else ""

    return (
        '<div class="content">'
        '<div class="summary" style="margin-bottom:10px;">'
        + _sc("tb", len(back),  "var(--green)", "Back")
        + _sc("tp", len(place), "var(--acc)",   "Place")
        + _sc("tm", len(multi), "var(--warn)",  "Multi")
        + '</div>'
        + spotlight_html
        + ("" if friend else _pnl_card(store))
        + f'<div class="card stat-card green" style="margin-bottom:9px;"><div class="stat-label">Races Loaded</div><div class="stat-value">{t_races}</div><div class="stat-sub">{t_run} runners</div></div>'
        + type_bar_html
        + next_html +
        f'<div class="card" style="margin-bottom:9px;"><div class="stat-label" style="margin-bottom:8px;">Tracks</div><div style="font-size:13px;line-height:1.6;">{tracks}</div></div>'
        f'<div class="card"><div class="stat-label" style="margin-bottom:8px;">Last Push</div><div style="font-size:14px;font-weight:600;">{pushed}</div><div style="font-size:11px;color:var(--t2);margin-top:3px;">Push #{store["push_count"]}</div></div>'
        '</div>'
    )

@app.get("/dash", response_class=HTMLResponse)
async def dash_page():
    await _refresh_store()
    return HTMLResponse(_cached_page("dash:" + _today_str(), lambda: _shell("dash", _dash_body(_store), _store)))

@app.get("/portal/dash", response_class=HTMLResponse)
async def portal_dash_page():
    await _refresh_store()
    return HTMLResponse(_cached_page("portal_dash:" + _today_str(), lambda: _shell("dash", _dash_body(_store, friend=True), _store, friend=True)))

def _analyzer_body(store):
    races = sorted(store["analyzer"], key=lambda r: _time_key(r.get("time","")))
    if not races:
        return '<div class="content"><p class="empty">No analyzer data yet</p></div>'
    blocks = ""
    for r in races:
        rid = _race_id(r)
        rsi = float(r.get("rsi",0))
        rc  = "re" if rsi>=80 else ("rs" if rsi>=70 else ("rm" if rsi>=60 else "rl"))
        rows = ""
        for j, h in enumerate(r.get("horses",[])):
            v  = float(h.get("value_pct",0))
            vc = "vp" if v>0 else ("vn" if v<0 else "")
            top= "top" if j<3 else ""
            hid = f"h-{rid}-{j}"
            rows += (
                f'<tr class="{top} horse-row-tr" onclick="togHorse(\'{hid}\')">'
                f'<td class="silk-cell">{_silk_html(_get_silk_url(h))}</td>'
                f'<td>{h.get("horse","")} <span class="hcaret">&#x25BE;</span></td>'
                f'<td class="ar">{h.get("win_pct",0):.1f}%</td>'
                f'<td class="ar">${h.get("real_odds",0):.2f}</td>'
                f'<td class="ar">${h.get("fair_odds",0):.2f}</td>'
                f'<td class="ar {vc}">{v:+.1f}%</td>'
                f'<td>{str(h.get("jockey",""))[:12]}</td>'
                '</tr>'
                f'<tr class="hdetail-row" id="{hid}">'
                f'<td colspan="7">{_horse_detail_html(h)}</td>'
                '</tr>'
            )
        blocks += (
            f'<div class="rblock" id="race-{rid}" data-time="{r.get("time","")}" data-timekey="{_time_key(r.get("time",""))}" data-track="{r.get("track","")}">'
            f'<div class="rhdr" onclick="tog(\'rb-{rid}\')">'
            f'<div><div class="rleft">{r.get("time","")} &middot; {r.get("track","")}</div>'
            f'<div class="rmeta">{r.get("race","")}</div></div>'
            '<div class="rhdr-right">'
            f'<span class="rb {rc}">RSI {int(rsi)}</span>'
            f'<span class="cd" data-time="{r.get("time","")}">&nbsp;</span>'
            '</div>'
            '</div>'
            f'<div class="rbody" id="rb-{rid}">'
            '<table class="tbl"><thead><tr><th class="silk-cell"></th><th>Horse</th><th>Win%</th><th>Odds</th><th>Fair</th><th>Val%</th><th>Jockey</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></div>'
        )
    sort_bar = (
        '<div class="sortbar">'
        '<button class="asort-btn sort-btn active" onclick="sortAnalyzer(\'time\',this)">&#x1F550; Time</button>'
        '<button class="asort-btn sort-btn" onclick="sortAnalyzer(\'track\',this)">Track A-Z</button>'
        '</div>'
    )
    countdown_script = '''<script>
function _parseRaceTime(str){
  if(!str) return null;
  var m=String(str).match(/(\\d{1,2}):(\\d{2})\\s*([AaPp][Mm])?/);
  if(!m) return null;
  var h=parseInt(m[1],10), mins=parseInt(m[2],10);
    if(m[3]){
    var ap=m[3].toUpperCase();
    if(ap==='PM' && h<12) h+=12;
    if(ap==='AM' && h===12) h=0;
  } else {
    // Assume all race times before 1:00 are afternoon (e.g. 1:00 = 13:00+),
    // but anything before 1:00 (11:30, 12:20, etc.) is always morning.
    if(h >= 1 && h <= 10){
      h += 12;
    }
  }
  var now=new Date();
  var d=new Date(now.getFullYear(),now.getMonth(),now.getDate(),h,mins,0,0);
  var diff=d.getTime()-now.getTime();
  if(diff < -6*3600*1000){ d.setDate(d.getDate()+1); diff=d.getTime()-now.getTime(); }
  return diff;
}
function _fmtCountdown(ms){
  if(ms<=0) return {text:'Started',cls:'cd-red'};
  var mins=Math.round(ms/60000);
  var h=Math.floor(mins/60), m=mins%60;
  var text = h>0 ? (h+'h '+m+'m') : (m+'m');
  var cls = mins<=10 ? 'cd-red' : (mins<=60 ? 'cd-orange' : '');
  return {text:text,cls:cls};
}
function _tickCountdowns(){
  document.querySelectorAll('.cd').forEach(function(el){
    var t=el.getAttribute('data-time');
    var diff=_parseRaceTime(t);
    if(diff===null){ el.textContent=''; return; }
    var r=_fmtCountdown(diff);
    el.textContent=r.text;
    el.classList.remove('cd-orange','cd-red');
    if(r.cls) el.classList.add(r.cls);
  });
}
_tickCountdowns();
setInterval(_tickCountdowns, 15000);
(function(){
  var h=window.location.hash;
  if(h && h.indexOf('#race-')===0){
    var el=document.querySelector(h);
    if(el){
      var rid=h.slice(1).replace('race-','');
      var body=document.getElementById('rb-'+rid);
      if(body) body.classList.add('open');
      setTimeout(function(){ el.scrollIntoView({behavior:'smooth',block:'start'}); }, 150);
    }
  }
})();
</script>'''
    return sort_bar + f'<div class="content"><div id="races-container">{blocks}</div></div>' + countdown_script

@app.get("/analyzer", response_class=HTMLResponse)
async def analyzer_page():
    await _refresh_store()
    return HTMLResponse(_cached_page("analyzer:" + _today_str(), lambda: _shell("analyzer", _analyzer_body(_store), _store)))

# ---------------------------------------------------------------------------
# Watch - live stream tab. Both sources are HLS (.m3u8) feeds, played in a
# native <video> element: Safari/iOS plays HLS natively, everything else goes
# through hls.js. "logo" is optional: when empty the tab shows the text label.
# To bring back the Sky logo images, paste a data:image/png;base64,... URI
# into each "logo" field below.
# ---------------------------------------------------------------------------
STREAM_SOURCES = [
    {"id": "sky1", "label": "Sky 1", "logo": "",
     "url": "https://skylivetab-new.akamaized.net/hls/live/2038780/sky1/index.m3u8"},
    {"id": "sky2", "label": "Sky 2", "logo": "",
     "url": "https://skylivetab-new.akamaized.net/hls/live/2038781/sky2/index.m3u8"},
]

def _watch_body():
    multi = len(STREAM_SOURCES) > 1
    tabs_html = ""
    if multi:
        tabs_html = '<div class="tabs">' + "".join(
            f'<button class="tab{" active" if i==0 else ""}" data-tab="watch" '
            f'onclick="switchWatch(\'{s["id"]}\',this)">'
            + (f'<img class="wtab-logo" src="{s["logo"]}" alt="{s["label"]}">' if s.get("logo") else s["label"])
            + '</button>'
            for i, s in enumerate(STREAM_SOURCES)
        ) + '</div>'
    panes_html = "".join(
        f'''<div class="watch-pane{" active" if i==0 else ""}" id="watch-{s["id"]}">
  <div class="video-frame">
    <video id="vid-{s["id"]}" data-src="{s["url"]}" controls playsinline autoplay muted
           style="position:absolute;top:0;left:0;width:100%;height:100%;background:#000;"></video>
  </div>
  <div class="watch-fallback">
    <span id="vmsg-{s["id"]}">Tap the video to unmute.</span>
    <a class="wbtn" href="{s["url"]}" target="_blank" rel="noopener">&#x2197; Open</a>
  </div>
</div>'''
        for i, s in enumerate(STREAM_SOURCES)
    )
    ids_json = json.dumps([s["id"] for s in STREAM_SOURCES])
    return f'''{tabs_html}
<div class="content">
{panes_html}
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.8/hls.min.js"></script>
<script>
var _watchIds={ids_json};
var _hlsPlayers={{}};
function _watchMsg(id,msg){{ var el=document.getElementById('vmsg-'+id); if(el) el.textContent=msg; }}
function initStream(id){{
  var video=document.getElementById('vid-'+id);
  if(!video || _hlsPlayers[id]) return;
  var src=video.getAttribute('data-src');
  if(video.canPlayType('application/vnd.apple.mpegurl')){{
    // Safari / iOS: native HLS
    video.src=src;
    _hlsPlayers[id]='native';
    video.addEventListener('error',function(){{ _watchMsg(id,'Stream unavailable right now.'); }});
  }} else if(window.Hls && Hls.isSupported()){{
    var hls=new Hls({{lowLatencyMode:true}});
    hls.loadSource(src);
    hls.attachMedia(video);
    hls.on(Hls.Events.ERROR,function(ev,data){{
      if(!data.fatal) return;
      if(data.type===Hls.ErrorTypes.NETWORK_ERROR){{ _watchMsg(id,'Connection lost, retrying\u2026'); hls.startLoad(); }}
      else if(data.type===Hls.ErrorTypes.MEDIA_ERROR){{ hls.recoverMediaError(); }}
      else {{ _watchMsg(id,'Stream unavailable right now.'); hls.destroy(); delete _hlsPlayers[id]; }}
    }});
    _hlsPlayers[id]=hls;
  }} else {{
    _watchMsg(id,'This browser can\\'t play the stream.');
  }}
  var p=video.play(); if(p && p.catch) p.catch(function(){{}});
}}
function switchWatch(id,btn){{
  document.querySelectorAll('[data-tab="watch"]').forEach(function(b){{b.classList.remove('active');}});
  btn.classList.add('active');
  document.querySelectorAll('.watch-pane').forEach(function(p){{p.classList.remove('active');}});
  _watchIds.forEach(function(o){{ var v=document.getElementById('vid-'+o); if(v && o!==id) v.pause(); }});
  var pane=document.getElementById('watch-'+id);
  if(pane) pane.classList.add('active');
  initStream(id);
  try{{localStorage.setItem('thepost_watch_tab',id);}}catch(e){{}}
}}
(function(){{
  var first=_watchIds[0];
  var last=null;
  try{{ last=localStorage.getItem('thepost_watch_tab'); }}catch(e){{}}
  if(last && _watchIds.indexOf(last)>0){{
    var btns=document.querySelectorAll('[data-tab="watch"]');
    var idx=_watchIds.indexOf(last);
    if(btns[idx]){{ switchWatch(last,btns[idx]); return; }}
  }}
  initStream(first);
}})();
</script>'''

@app.get("/watch", response_class=HTMLResponse)
async def watch_page():
    await _refresh_store()
    return HTMLResponse(_cached_page("watch:" + _today_str(), lambda: _shell("watch", _watch_body(), _store)))

@app.get("/portal/watch", response_class=HTMLResponse)
async def portal_watch_page():
    await _refresh_store()
    return HTMLResponse(_cached_page("portal_watch:" + _today_str(), lambda: _shell("watch", _watch_body(), _store, friend=True)))

# ---------------------------------------------------------------------------
# Today's P&L card (Dashboard, owner pages only).
# ---------------------------------------------------------------------------

def _today_str():
    return datetime.datetime.now(NOTIFY_TZ).date().isoformat()

def _pnl_card(store):
    pnl = store.get("pnl") or {}
    recs = (pnl.get("records") or []) if pnl.get("date") == _today_str() else []
    if not recs:
        return (
            '<div class="card" style="margin-bottom:9px;">'
            '<div class="stat-label" style="margin-bottom:6px;">Today\'s P&amp;L</div>'
            '<div class="pnl-big" style="color:var(--t2);">0.00u</div>'
            '<div class="stat-sub">No bets marked today yet</div>'
            '</div>'
        )
    pls = [float(r.get("pnl_units", 0) or 0) for r in recs]
    net = sum(pls)
    won_u  = sum(p for p in pls if p > 0)
    lost_u = sum(p for p in pls if p < 0)
    n = len(recs)
    wins = sum(1 for r in recs if str(r.get("result", "")).upper() == "WIN")
    losses = n - wins
    col = "var(--green)" if net > 0 else ("var(--red)" if net < 0 else "var(--t2)")
    rows = ""
    for r, p in zip(recs, pls):
        pc = "var(--green)" if p > 0 else ("var(--red)" if p < 0 else "var(--t2)")
        res = str(r.get("result", "")).upper()
        tag_cls = "win" if res == "WIN" else "loss"
        sub = " &middot; ".join(x for x in (
            str(r.get("type", "")).title(),
            str(r.get("track", "")),
            f'{float(r.get("units", 0) or 0):g}u @ ${float(r.get("real_odds", 0) or 0):.2f}',
        ) if x)
        rows += (
            '<div class="pnl-row">'
            '<div class="pnl-main">'
            f'<div class="pnl-horse">{r.get("horse", "")}</div>'
            f'<div class="pnl-sub">{sub}</div>'
            '</div>'
            f'<span class="pnl-tag {tag_cls}">{res or "-"}</span>'
            f'<span class="pnl-val" style="color:{pc};">{p:+.2f}u</span>'
            '</div>'
        )
    return (
        '<div class="card pnl-card" style="margin-bottom:9px;padding:0;overflow:hidden;">'
        '<div class="pnl-head" onclick="this.classList.toggle(\'open\');document.getElementById(\'pnl-body\').classList.toggle(\'open\');">'
        '<div>'
        '<div class="stat-label" style="margin-bottom:4px;">Today\'s P&amp;L</div>'
        f'<div class="pnl-big" style="color:{col};">{net:+.2f}u</div>'
        f'<div class="stat-sub">{n} bet{"s" if n != 1 else ""} &middot; {wins} won &middot; {losses} lost &middot; tap for bets</div>'
        '</div>'
        '<span class="pnl-caret">&#x25BE;</span>'
        '</div>'
        '<div class="rbody" id="pnl-body">'
        '<div style="display:flex;gap:6px;padding:9px 11px 4px;">'
        f'<div class="sc"><div class="sn" style="color:var(--green);font-size:15px;">{won_u:+.2f}u</div><div class="sl2">Units won</div></div>'
        f'<div class="sc"><div class="sn" style="color:var(--red);font-size:15px;">{lost_u:+.2f}u</div><div class="sl2">Units lost</div></div>'
        f'<div class="sc"><div class="sn" style="color:{col};font-size:15px;">{net:+.2f}u</div><div class="sl2">Net</div></div>'
        '</div>'
        f'<div style="padding:2px 11px 6px;">{rows}</div>'
        '</div>'
        '</div>'
    )

# ---------------------------------------------------------------------------
# Settings - jump-time push notifications. Subscriptions are per-device.
# ---------------------------------------------------------------------------

def _settings_body():
    if not _PUSH_LIB_AVAILABLE:
        return ('<div class="content"><p class="empty">Push notifications need the <code>pywebpush</code> '
                'package on the server - add it to requirements.txt and redeploy.</p></div>')
    if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY):
        return ('<div class="content"><p class="empty">Push notifications aren\'t configured on '
                'the server yet (missing VAPID keys).</p></div>')
    return f'''<div class="content">
<div class="card" style="margin-bottom:9px;">
  <div class="stat-label" style="margin-bottom:2px;">Jump-Time Notifications</div>
  <div class="settings-row">
    <span>Enable on this device</span>
    <label class="switch"><input type="checkbox" id="pref-enabled" onchange="onMasterToggle()"><span class="slider"></span></label>
  </div>
  <div class="settings-row">
    <span>Place bets</span>
    <label class="switch"><input type="checkbox" id="pref-place" checked onchange="savePrefsOnly()"><span class="slider"></span></label>
  </div>
  <div class="settings-row">
    <span>Multi bets</span>
    <label class="switch"><input type="checkbox" id="pref-multi" checked onchange="savePrefsOnly()"><span class="slider"></span></label>
  </div>
  <div class="settings-row">
    <span>Minutes before jump</span>
    <input type="number" id="pref-minutes" class="mins-input" min="1" max="60" value="5" onchange="savePrefsOnly()">
  </div>
  <button class="sbtn" style="width:100%;justify-content:center;margin-top:10px;" onclick="sendTestNotification()">&#x1F514; Send Test Notification</button>
  <div id="push-status" class="push-status">Checking this device&hellip;</div>
</div>
<div class="card">
  <div style="font-size:11px;color:var(--t2);line-height:1.6;">
    Alerts cover the tips posted to Discord (Back always, Place and Multi if switched on) and only go out on the day of the race. On iPhone, add the app to your Home Screen first.
  </div>
</div>
</div>
<script>
var VAPID_PUBLIC_KEY="{VAPID_PUBLIC_KEY}";
function _b64ToUint8(b64){{
  var pad='='.repeat((4-b64.length%4)%4);
  var base64=(b64+pad).replace(/-/g,'+').replace(/_/g,'/');
  var raw=atob(base64), arr=new Uint8Array(raw.length);
  for(var i=0;i<raw.length;i++) arr[i]=raw.charCodeAt(i);
  return arr;
}}
function _setStatus(msg){{ var el=document.getElementById('push-status'); if(el) el.textContent=msg; }}
function _currentPrefs(){{
  return {{
    enabled: document.getElementById('pref-enabled').checked,
    place: document.getElementById('pref-place').checked,
    multi: document.getElementById('pref-multi').checked,
    minutes_before: parseInt(document.getElementById('pref-minutes').value,10) || 5
  }};
}}
async function _getSub(){{
  if(!('serviceWorker' in navigator)) return null;
  try{{
    var reg=await navigator.serviceWorker.getRegistration('/sw.js');
    if(!reg) return null;
    return await reg.pushManager.getSubscription();
  }}catch(e){{ return null; }}
}}
async function loadPushSettings(){{
  if(!('Notification' in window) || !('serviceWorker' in navigator) || !('PushManager' in window)){{
    _setStatus('Push notifications aren\\'t supported on this browser.');
    document.getElementById('pref-enabled').disabled=true;
    return;
  }}
  if(window.thePostPushHeal){{ try{{ await window.thePostPushHeal(); }}catch(e){{}} }}
  var sub=await _getSub();
  if(!sub){{ _setStatus('Not enabled on this device yet.'); return; }}
  try{{
    var r=await fetch('/api/push/prefs?endpoint='+encodeURIComponent(sub.endpoint));
    var s=await r.json();
    if(s.subscribed){{
      document.getElementById('pref-enabled').checked=!!s.prefs.enabled;
      document.getElementById('pref-place').checked=!!s.prefs.place;
      document.getElementById('pref-multi').checked=!!s.prefs.multi;
      document.getElementById('pref-minutes').value=s.prefs.minutes_before||5;
      _setStatus('Enabled on this device.');
    }} else {{
      // this device is still subscribed in the browser but the server has no record: re-register it
      var p=_currentPrefs(); p.enabled=true;
      var rr=await fetch('/api/push/subscribe',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{subscription:sub.toJSON(),prefs:p,resync:true}})}});
      if(rr.ok){{ document.getElementById('pref-enabled').checked=true; _setStatus('Enabled on this device.'); }}
      else {{ _setStatus('Not enabled on this device yet.'); }}
    }}
  }}catch(e){{ _setStatus('Could not load settings for this device.'); }}
}}
async function onMasterToggle(){{
  if(document.getElementById('pref-enabled').checked){{ await enablePush(); }} else {{ await disablePush(); }}
}}
async function enablePush(){{
  try{{
    var perm=await Notification.requestPermission();
    if(perm!=='granted'){{
      document.getElementById('pref-enabled').checked=false;
      _setStatus('Notification permission was not granted.');
      return;
    }}
    var reg=await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    var sub=await reg.pushManager.getSubscription();
    if(!sub){{
      sub=await reg.pushManager.subscribe({{userVisibleOnly:true, applicationServerKey:_b64ToUint8(VAPID_PUBLIC_KEY)}});
    }}
    var sr=await fetch('/api/push/subscribe',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{subscription:sub.toJSON(),prefs:_currentPrefs()}})}});
    if(!sr.ok) throw new Error('subscribe failed');
    try{{ if(window.thePostPushMark) window.thePostPushMark(true); localStorage.setItem('thepost_push_prefs',JSON.stringify(_currentPrefs())); }}catch(e){{}}
    _setStatus('Enabled on this device.');
  }}catch(e){{
    document.getElementById('pref-enabled').checked=false;
    _setStatus('Could not enable notifications on this device.');
  }}
}}
async function disablePush(){{
  try{{
    var sub=await _getSub();
    try{{ if(window.thePostPushMark) window.thePostPushMark(false); }}catch(e){{}}
    if(sub){{
      var ur=await fetch('/api/push/unsubscribe',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{endpoint:sub.endpoint}})}});
      if(!ur.ok) throw new Error('server unsubscribe failed');
      await sub.unsubscribe();
    }}
    _setStatus('Notifications disabled on this device.');
  }}catch(e){{ _setStatus('Could not fully disable \u2014 try again.'); }}
}}
async function savePrefsOnly(){{
  var sub=await _getSub();
  if(!sub) return;
  try{{ localStorage.setItem('thepost_push_prefs',JSON.stringify(_currentPrefs())); }}catch(e){{}}
  await fetch('/api/push/prefs',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{endpoint:sub.endpoint,prefs:_currentPrefs()}})}});
}}
async function sendTestNotification(){{
  var sub=await _getSub();
  if(!sub){{ _setStatus('Enable notifications on this device first.'); return; }}
  _setStatus('Sending test notification\u2026');
  try{{
    var r=await fetch('/api/push/test',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{subscription:sub.toJSON()}})}});
    if(r.ok){{ _setStatus('Test sent \u2014 check your notifications.'); return; }}
    var msg='Test failed to send.';
    try{{ var e=await r.json(); if(e && e.detail) msg='Failed: '+e.detail; }}catch(parseErr){{}}
    _setStatus(msg);
  }}catch(e){{ _setStatus('Test failed to send (network error reaching the server).'); }}
}}
window.addEventListener('load', function(){{ loadPushSettings(); }});
</script>'''

@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    await _refresh_store()
    return HTMLResponse(_cached_page("settings:" + _today_str(), lambda: _shell("settings", _settings_body(), _store)))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT",8000)))
