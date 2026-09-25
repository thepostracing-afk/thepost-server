#!/usr/bin/env python3
"""The Post — Live Tips Server  v6 (persistent storage via Upstash Redis)"""

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
    _PUSH_LIB_AVAILABLE = False
    class WebPushException(Exception):
        pass
    def webpush(*args, **kwargs):
        raise WebPushException("pywebpush is not installed on the server")

try:
    from zoneinfo import ZoneInfo
    NOTIFY_TZ = ZoneInfo("Australia/Melbourne")
except Exception:
    NOTIFY_TZ = datetime.timezone(datetime.timedelta(hours=10))

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "").strip() or "mailto:admin@example.com"
NOTIFY_TYPES = ("BACK", "PLACE", "MULTI")
_PUSH_READY = _PUSH_LIB_AVAILABLE and bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)

PUSH_API_KEY = os.environ.get("PUSH_API_KEY", "thepost2026")

UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
STORE_KEY     = "thepost_store"

DEFAULT_STORE = {"tips": [], "analyzer": [], "live": [], "pnl": [], "last_push": None, "push_count": 0, "push_subs": {}}

app = FastAPI(title="The Post", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=500)

ICON_PATH = os.path.join(os.path.dirname(__file__), "thepost.png")

SILK_SIZE      = 22
SILK_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "silk_cache")
os.makedirs(SILK_CACHE_DIR, exist_ok=True)
SILK_MEM_CACHE_MAX = 500
_silk_mem_cache = OrderedDict()

def _silk_mem_cache_put(key, value):
    _silk_mem_cache[key] = value
    _silk_mem_cache.move_to_end(key)
    if len(_silk_mem_cache) > SILK_MEM_CACHE_MAX:
        _silk_mem_cache.popitem(last=False)

def _get_silk_url(d):
    return (
        d.get("silk_url") or d.get("SilkURL")
        or d.get("silk")     or d.get("Silk")
        or d.get("silk_image_url") or ""
    ).strip()

def _get_horse_number(d):
    for k in ("number", "horse_number", "saddlecloth", "saddlecloth_number",
              "runner_number", "tab_number", "program_number"):
        v = d.get(k)
        if v not in (None, ""):
            return v
    return ""

def _silk_html(url, size=SILK_SIZE):
    if not url:
        return f'<span class="silk-wrap" style="width:{size}px;height:{size}px;"></span>'
    src = url if url.startswith("data:") else f"/silk?u={quote(url, safe='')}"
    return (
        f'<span class="silk-wrap" style="width:{size}px;height:{size}px;">'
        f'<img class="silk-img" src="{src}" width="{size}" height="{size}" '
        f'loading="lazy" decoding="async" alt="" '
        f'onerror="this.style.visibility=\'hidden\'">'
        f'</span>'
    )

def _fetch_silk_sync(u, disk_path, ext):
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

@app.get("/icon.png")
async def serve_icon():
    from fastapi.responses import FileResponse
    if os.path.exists(ICON_PATH):
        return FileResponse(ICON_PATH, media_type="image/png")
    return Response(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="), media_type="image/png")

@app.get("/manifest.json")
async def manifest():
    return JSONResponse({"name":"The Post","short_name":"The Post","description":"Racing Intelligence","start_url":"/dash","display":"standalone","background_color":"#0B0F14","theme_color":"#0B0F14","orientation":"portrait","icons":[{"src":"/icon.png","sizes":"512x512","type":"image/png"}]})

def _headers():
    return {"Authorization": f"Bearer {UPSTASH_TOKEN}"}

def _load():
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
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return
    try:
        payload = json.dumps(s)
        requests.post(f"{UPSTASH_URL}/set/{STORE_KEY}", headers=_headers(), data=payload, timeout=5)
    except Exception:
        pass

_store = _load()
_store.setdefault("push_subs", {})
_store.setdefault("pnl", [])
_push_lock = asyncio.Lock()

_page_cache = {}

def _cached_page(key, build_fn):
    pc = _store.get("push_count", 0)
    hit = _page_cache.get(key)
    if hit and hit[0] == pc:
        return hit[1]
    html = build_fn()
    _page_cache[key] = (pc, html)
    return html

_notified = set()

def _sub_key(endpoint):
    return hashlib.sha1((endpoint or "").encode("utf-8")).hexdigest()[:16]

def _tip_id(t):
    if t.get("type") == "MULTI":
        legs_key = "|".join(
            f'{l.get("track","")}-{l.get("race","")}-{l.get("horse","")}'
            for l in (t.get("legs") or [])
        )
        raw = f'MULTI|{t.get("time","")}|{legs_key}'
    else:
        raw = f'{t.get("type","")}|{t.get("track","")}|{t.get("race","")}|{t.get("time","")}|{t.get("horse","")}'
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

def _jump_dt_today(time_str, now):
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

async def _notify_tick():
    if not _PUSH_READY:
        return
    now = datetime.datetime.now(NOTIFY_TZ)
    if now.weekday() != 5:
        return
    subs = list(_store.get("push_subs", {}).items())
    if not subs:
        return
    for t in _store.get("tips", []):
        ttype = t.get("type")
        if ttype not in NOTIFY_TYPES:
            continue
        jump = _jump_dt_today(t.get("time", ""), now)
        if not jump:
            continue
        mins_out = (jump - now).total_seconds() / 60.0
        tid = _tip_id(t)
        if ttype == "MULTI":
            legs = t.get("legs", []) or []
            first_leg = legs[0] if legs else {}
            silk = _get_silk_url(first_leg)
            horse_label = f'{len(legs)}-Leg Multi'
            track_label = first_leg.get("track", t.get("track",""))
            race_label  = first_leg.get("race", t.get("race",""))
        else:
            silk = _get_silk_url(t)
            number = _get_horse_number(t)
            horse_label = f'{number}. {t.get("horse","")}' if number != "" else t.get("horse", "")
            track_label = t.get("track","")
            race_label  = t.get("race","")
        icon = f"/silk?u={quote(silk, safe='')}" if (silk and not silk.startswith("data:")) else "/icon.png"
        for key, entry in subs:
            prefs = entry.get("prefs", {})
            if not prefs.get("enabled", True):
                continue
            if ttype == "PLACE" and not prefs.get("place", True):
                continue
            if ttype == "MULTI" and not prefs.get("multi", True):
                continue
            lead = prefs.get("minutes_before", 5)
            if not (0 <= mins_out <= lead):
                continue
            dedupe = (key, tid)
            if dedupe in _notified:
                continue
            payload = {
                "title": f'{track_label} {race_label} \u2014 {max(0, round(mins_out))} min to jump',
                "body": f'{horse_label} \u00b7 {ttype.title()} \u00b7 {int(t.get("units",1))}u',
                "icon": icon,
                "tag": tid,
                "url": "/tips",
            }
            try:
                await run_in_threadpool(_send_push_sync, entry["subscription"], payload)
                _notified.add(dedupe)
            except WebPushException as e:
                if "410" in str(e) or "404" in str(e):
                    _store.get("push_subs", {}).pop(key, None)
                    await run_in_threadpool(_save, _store)
            except Exception:
                pass

@app.on_event("startup")
async def _start_background_sync():
    async def _loop():
        while True:
            await asyncio.sleep(45)
            try:
                fresh = await run_in_threadpool(_load)
                if fresh.get("push_count", 0) != _store.get("push_count", 0):
                    _store.clear()
                    _store.update(fresh)
                    _store.setdefault("push_subs", {})
            except Exception:
                pass
    asyncio.create_task(_loop())

@app.on_event("startup")
async def _start_notify_loop():
    async def _loop():
        while True:
            await asyncio.sleep(20)
            try:
                await _notify_tick()
            except Exception:
                pass
    asyncio.create_task(_loop())

@app.post("/push")
async def push(request: Request, x_api_key: str = Header(default="")):
    if x_api_key != PUSH_API_KEY: raise HTTPException(status_code=401,detail="Invalid API key")
    try: body = await request.json()
    except: raise HTTPException(status_code=400,detail="Invalid JSON")
    async with _push_lock:
        _store["tips"]        = body.get("tips",[])
        _store["analyzer"]    = body.get("analyzer",[])
        _store["live"]        = body.get("live",[])
        _store["last_push"]   = body.get("generated_at") or datetime.datetime.now().isoformat()
        _store["push_count"] += 1
        await run_in_threadpool(_save, _store)
    return {"status":"ok","tips":len(_store["tips"]),"analyzer_races":len(_store["analyzer"]),"live_races":len(_store["live"])}

@app.post("/push/pnl")
async def push_pnl(request: Request, x_api_key: str = Header(default="")):
    """Receives today's marked win/loss results from the desktop app. The
    desktop app always sends its full current list for today (not just the
    newest record), so this just replaces _store["pnl"] wholesale — same
    pattern as /push for tips, and it means the list naturally resets once
    the desktop app starts sending a new day's records."""
    if x_api_key != PUSH_API_KEY: raise HTTPException(status_code=401,detail="Invalid API key")
    try: body = await request.json()
    except: raise HTTPException(status_code=400,detail="Invalid JSON")
    async with _push_lock:
        _store["pnl"] = body.get("pnl", [])
        _store["push_count"] += 1
        await run_in_threadpool(_save, _store)
    return {"status":"ok","pnl_records":len(_store["pnl"])}

@app.get("/api/tips")
async def api_tips():
    return JSONResponse(_store["tips"])

@app.get("/api/analyzer")
async def api_analyzer():
    return JSONResponse(_store["analyzer"])

@app.get("/api/status")
async def api_status():
    return {"last_push":_store["last_push"],"push_count":_store["push_count"],"tips":len(_store["tips"]),"analyzer_races":len(_store["analyzer"])}

def _default_prefs(overrides=None):
    p = {"enabled": True, "place": True, "multi": True, "minutes_before": 5}
    if overrides:
        p.update(overrides)
    return p

@app.get("/api/push/prefs")
async def push_prefs_get(endpoint: str = ""):
    if not endpoint:
        return {"subscribed": False}
    entry = _store.get("push_subs", {}).get(_sub_key(endpoint))
    if not entry:
        return {"subscribed": False}
    return {"subscribed": True, "prefs": entry.get("prefs", _default_prefs())}

@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    try: body = await request.json()
    except: raise HTTPException(status_code=400, detail="Invalid JSON")
    sub = body.get("subscription") or {}
    if not sub.get("endpoint"):
        raise HTTPException(status_code=400, detail="Missing subscription")
    prefs_in = body.get("prefs") or {}
    key = _sub_key(sub["endpoint"])
    _store.setdefault("push_subs", {})[key] = {
        "subscription": sub,
        "prefs": _default_prefs({
            "enabled": bool(prefs_in.get("enabled", True)),
            "place": bool(prefs_in.get("place", True)),
            "multi": bool(prefs_in.get("multi", True)),
            "minutes_before": max(1, min(60, int(prefs_in.get("minutes_before", 5) or 5))),
        }),
    }
    await run_in_threadpool(_save, _store)
    return {"status": "ok"}

@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request):
    try: body = await request.json()
    except: raise HTTPException(status_code=400, detail="Invalid JSON")
    endpoint = (body.get("subscription") or {}).get("endpoint") or body.get("endpoint")
    if endpoint:
        _store.get("push_subs", {}).pop(_sub_key(endpoint), None)
        await run_in_threadpool(_save, _store)
    return {"status": "ok"}

@app.post("/api/push/prefs")
async def push_prefs_post(request: Request):
    try: body = await request.json()
    except: raise HTTPException(status_code=400, detail="Invalid JSON")
    endpoint = (body.get("subscription") or {}).get("endpoint") or body.get("endpoint")
    if not endpoint:
        raise HTTPException(status_code=400, detail="Missing endpoint")
    entry = _store.get("push_subs", {}).get(_sub_key(endpoint))
    if not entry:
        raise HTTPException(status_code=404, detail="Not subscribed")
    prefs_in = body.get("prefs") or {}
    if "enabled" in prefs_in: entry["prefs"]["enabled"] = bool(prefs_in["enabled"])
    if "place"   in prefs_in: entry["prefs"]["place"]   = bool(prefs_in["place"])
    if "multi"   in prefs_in: entry["prefs"]["multi"]   = bool(prefs_in["multi"])
    if "minutes_before" in prefs_in:
        try: entry["prefs"]["minutes_before"] = max(1, min(60, int(prefs_in["minutes_before"])))
        except (TypeError, ValueError): pass
    await run_in_threadpool(_save, _store)
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
self.addEventListener('push', function(event){
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch(e) {}
  var title = data.title || 'The Post';
  var options = {
    body: data.body || '',
    icon: data.icon || '/icon.png',
    badge: '/icon.png',
    tag: data.tag || undefined,
    data: { url: data.url || '/' }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});
self.addEventListener('notificationclick', function(event){
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.openWindow(url));
});
"""
    return Response(js, media_type="application/javascript")

def _stat_row(label, value, suffix=""):
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

def _race_id(r):
    raw = f'{r.get("track","")}|{r.get("race","")}|{r.get("time","")}'
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]

def _time_key(tstr):
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
    total  = len(store["tips"])
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
  <div class="status">Last push: """ + pushed + """ &nbsp;&middot;&nbsp; """ + str(total) + """ tip""" + ("s" if total!=1 else "") + """</div>
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
    alert('Export library failed to load — check your connection.');
  });
}
function _runExportPhoto(){
  var activeSection=document.querySelector('.section.active');
  if(!activeSection){ alert('Nothing to export yet.'); return; }

  var toast=document.getElementById('export-toast');
  toast.style.display='block';

  var clone=activeSection.cloneNode(true);
  clone.classList.add('active');

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
      alert('Export took too long and was cancelled — please try again.');
    } else {
      alert('Export failed — please try again.');
    }
  });
}
""" + _poll_script(store.get("push_count", 0), page_id) + """
</script>
</body></html>"""

def _cards_js(tips_list, label, container_id, odds_label="ODDS"):
    if not tips_list:
        return f'<p class="empty">No {label} picks yet</p>'
    out = f'<div id="{container_id}">'
    for t in tips_list:
        tag_val = t.get("tag") or ""
        cls = tag_val.lower().replace(" ","-")
        tag_html = f'<span class="tag {cls}">{tag_val}</span>' if tag_val and tag_val not in ("TOP PLAY", "SECONDARY") else ""
        vc  = "pos" if t.get("value_pct",0)>0 else "neg"
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
            f'<div class="ctop"><span class="horse-row">{_silk_html(_get_silk_url(t))}<span class="horse">{t.get("horse","")}</span></span>{tag_html}</div>'
            f'<div class="meta">{t.get("time","")} &middot; {t.get("track","")} &middot; {t.get("race","")}</div>'
            f'<div class="stats">'
            f'<div class="stat"><span class="sl">{odds_label}</span><span class="sv">${t.get("real_odds",0):.2f}</span></div>'
            f'<div class="stat"><span class="sl">UNITS</span><span class="sv">{int(t.get("units",1))}u</span></div>'
            f'<div class="stat"><span class="sl">VALUE</span><span class="sv {vc}">{t.get("value_pct",0):+.1f}%</span></div>'
            f'<div class="stat"><span class="sl">RSI</span><span class="sv">{int(t.get("rsi",0))}</span></div>'
            f'<div class="stat"><span class="sl">WIN%</span><span class="sv">{t.get("win_pct",0):.1f}%</span></div>'
            f'</div></div>'
        )
    out += "</div>"
    return out

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
    units = t.get("units", 1)
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
        f'<div class="meta">{tracks}</div>'
        f'<div class="multi-legs">{legs_html}</div>'
        '<div class="multi-foot">'
        f'<div class="mf-item"><span class="sl">COMBINED ODDS</span><span class="sv">${combined:.2f}</span></div>'
        f'<div class="mf-item"><span class="sl">UNITS</span><span class="sv">{int(units)}u</span></div>'
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
    tips  = store["tips"]
    back  = sorted([t for t in tips if t["type"]=="BACK"],  key=lambda t: t.get("track",""))
    place = sorted([t for t in tips if t["type"]=="PLACE"], key=lambda t: t.get("track",""))
    multi = sorted(
        [t for t in tips if t["type"]=="MULTI"],
        key=lambda t: -(t.get("avg_rsi",0) or 0)
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

@app.get("/", response_class=HTMLResponse)
async def home_redirect():
    return RedirectResponse(url="/dash")

@app.get("/portal", response_class=HTMLResponse)
async def portal_home_redirect():
    return RedirectResponse(url="/portal/dash")

@app.get("/tips", response_class=HTMLResponse)
async def tips_page():
    return HTMLResponse(_cached_page("tips", lambda: _shell("tips", _tips_body(_store), _store)))

@app.get("/portal/tips", response_class=HTMLResponse)
async def portal_tips_page():
    return HTMLResponse(_cached_page("portal_tips", lambda: _shell("tips", _tips_body(_store), _store, friend=True)))

def _pnl_card_html(store):
    """Today's marked results — wins, losses, and net units, plus a
    per-result list (horse, track, bet type, and the units won/lost on
    that one). Renders nothing if nothing's been marked yet today."""
    records = store.get("pnl", [])
    if not records:
        return ""
    wins   = sum(1 for r in records if str(r.get("result","")).upper() == "WIN")
    losses = sum(1 for r in records if str(r.get("result","")).upper() != "WIN")
    net    = sum(float(r.get("pnl_units", 0) or 0) for r in records)
    net_color = "var(--green)" if net > 0 else ("var(--red)" if net < 0 else "var(--t2)")

    rows = ""
    for r in reversed(records):  # most recent result first
        is_win = str(r.get("result","")).upper() == "WIN"
        rc = "var(--green)" if is_win else "var(--red)"
        pnl_val = float(r.get("pnl_units", 0) or 0)
        sub = " &middot; ".join(x for x in [r.get("track",""), r.get("type","")] if x)
        rows += (
            '<div class="nr-row">'
            f'<span class="nr-name">{r.get("horse","")}'
            + (f'<br><span style="font-size:9.5px;color:var(--t2);">{sub}</span>' if sub else '')
            + '</span>'
            f'<span style="font-weight:700;color:{rc};white-space:nowrap;">{pnl_val:+.2f}u</span>'
            '</div>'
        )

    return (
        '<div class="card" style="margin-bottom:9px;">'
        '<div class="stat-label" style="margin-bottom:8px;">Today&#8217;s P&amp;L</div>'
        '<div class="spot-stats" style="grid-template-columns:repeat(3,1fr);margin-bottom:6px;">'
        f'<div class="spot-item"><span class="hsl">WINS</span><span class="hsv" style="color:var(--green);">{wins}</span></div>'
        f'<div class="spot-item"><span class="hsl">LOSSES</span><span class="hsv" style="color:var(--red);">{losses}</span></div>'
        f'<div class="spot-item"><span class="hsl">NET UNITS</span><span class="hsv" style="color:{net_color};">{net:+.2f}u</span></div>'
        '</div>'
        f'{rows}'
        '</div>'
    )

def _dash_body(store, friend=False):
    tips     = store["tips"]
    analyzer = store["analyzer"]
    back  = [t for t in tips if t["type"]=="BACK"]
    place = [t for t in tips if t["type"]=="PLACE"]
    multi = [t for t in tips if t["type"]=="MULTI"]
    all_t = back+place+multi
    back_u   = sum(t.get("units",0) for t in back)
    place_u  = sum(t.get("units",0) for t in place)
    multi_u  = sum(t.get("units",0) for t in multi)
    total_u  = back_u+place_u+multi_u
    best     = max(all_t, key=lambda t: t.get("units",0), default=None)

    track_set = set()
    for t in all_t:
        if t.get("type")=="MULTI":
            for l in (t.get("legs") or []):
                if l.get("track"): track_set.add(l.get("track"))
        elif t.get("track"):
            track_set.add(t.get("track"))
    tracks   = ", ".join(sorted(track_set)) or "—"
    t_races  = len(analyzer)
    t_run    = sum(len(r.get("horses",[])) for r in analyzer)
    pushed   = _pushed_str(store)

    tips_base = "/portal/tips" if friend else "/tips"

    def _sc(tid, count, color, label):
        pct = round(100*count/len(all_t)) if all_t else 0
        return (
            f'<div class="sc sc-link" style="border-top:2px solid {color};" '
            f'onclick="location.href=\'{tips_base}?tab={tid}\'">'
            f'<div class="sn" style="color:{color}">{count}</div>'
            f'<div class="sl2">{label}</div>'
            f'<div class="sc-pct">{pct}% of tips</div></div>'
        )

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
                f'<div class="spot-item"><span class="hsl">ODDS</span><span class="hsv">${best.get("combined_odds",0):.2f}</span></div>'
                f'<div class="spot-item"><span class="hsl">LEGS</span><span class="hsv">{len(legs)}</span></div>'
                f'<div class="spot-item"><span class="hsl">AVG RSI</span><span class="hsv">{int(best.get("avg_rsi",0))}</span></div>'
                f'<div class="spot-item"><span class="hsl">UNITS</span><span class="hsv">{int(best.get("units",1))}u</span></div>'
            )
        else:
            title_name = best.get("horse","")
            meta = f'{best.get("time","")} &middot; {best.get("track","")} &middot; {best.get("race","")}'
            odds_lbl = "PLACE ODDS" if b_type == "PLACE" else "ODDS"
            stats_html = (
                f'<div class="spot-item"><span class="hsl">{odds_lbl}</span><span class="hsv">${best.get("real_odds",0):.2f}</span></div>'
                f'<div class="spot-item"><span class="hsl">VALUE</span><span class="hsv pos">{best.get("value_pct",0):+.1f}%</span></div>'
                f'<div class="spot-item"><span class="hsl">RSI</span><span class="hsv">{int(best.get("rsi",0))}</span></div>'
                f'<div class="spot-item"><span class="hsl">UNITS</span><span class="hsv">{int(best.get("units",1))}u</span></div>'
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
        + _pnl_card_html(store)
        + f'<div class="card stat-card green" style="margin-bottom:9px;"><div class="stat-label">Races Loaded</div><div class="stat-value">{t_races}</div><div class="stat-sub">{t_run} runners</div></div>'
        + type_bar_html
        + next_html +
        f'<div class="card" style="margin-bottom:9px;"><div class="stat-label" style="margin-bottom:8px;">Tracks Today</div><div style="font-size:13px;line-height:1.6;">{tracks}</div></div>'
        f'<div class="card"><div class="stat-label" style="margin-bottom:8px;">Last Push</div><div style="font-size:14px;font-weight:600;">{pushed}</div><div style="font-size:11px;color:var(--t2);margin-top:3px;">Push #{store["push_count"]}</div></div>'
        '</div>'
    )

@app.get("/dash", response_class=HTMLResponse)
async def dash_page():
    return HTMLResponse(_cached_page("dash", lambda: _shell("dash", _dash_body(_store), _store)))

@app.get("/portal/dash", response_class=HTMLResponse)
async def portal_dash_page():
    return HTMLResponse(_cached_page("portal_dash", lambda: _shell("dash", _dash_body(_store, friend=True), _store, friend=True)))

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
    return HTMLResponse(_cached_page("analyzer", lambda: _shell("analyzer", _analyzer_body(_store), _store)))

# ---------------------------------------------------------------------------
# Watch — live stream tab. Two sources now: Sky 1 and Sky 2, each shown as a
# tab with the broadcaster's own logo instead of a plain text label. Source
# is an HLS (.m3u8) feed, which can't be dropped into an <iframe> like a
# normal web page, so it plays in a native <video> element instead:
# Safari/iOS plays HLS natively, everything else goes through hls.js (loaded
# from cdnjs). Both /watch and /portal/watch use _watch_body(), so both get
# this player.
#
# To add another stream later, just append to STREAM_SOURCES with an "icon"
# data URI (or omit "icon" to fall back to plain text) — a tab bar appears
# automatically once there is more than one.
# ---------------------------------------------------------------------------
STREAM_SOURCES = [
    {"id": "sky1", "label": "Sky 1", "url": "https://skylivetab-new.akamaized.net/hls/live/2038780/sky1/index.m3u8",
     "icon": "data:image/png;base64,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"},
    {"id": "sky2", "label": "Sky 2", "url": "https://skylivetab-new.akamaized.net/hls/live/2038781/sky2/index.m3u8",
     "icon": "data:image/png;base64,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"},
]

def _watch_body():
    multi = len(STREAM_SOURCES) > 1
    tabs_html = ""
    if multi:
        tabs_html = '<div class="tabs" style="align-items:center;">' + "".join(
            f'<button class="tab{" active" if i==0 else ""}" data-tab="watch" '
            f'style="padding:10px 4px;display:flex;align-items:center;justify-content:center;" '
            f'onclick="switchWatch(\'{s["id"]}\',this)">'
            + (f'<img src="{s["icon"]}" alt="{s["label"]}" style="height:34px;max-width:100px;object-fit:contain;display:block;">' if s.get("icon") else s["label"])
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
    return HTMLResponse(_cached_page("watch", lambda: _shell("watch", _watch_body(), _store)))

@app.get("/portal/watch", response_class=HTMLResponse)
async def portal_watch_page():
    return HTMLResponse(_cached_page("portal_watch", lambda: _shell("watch", _watch_body(), _store, friend=True)))

def _settings_body():
    if not _PUSH_LIB_AVAILABLE:
        return ('<div class="content"><p class="empty">Push notifications need the <code>pywebpush</code> '
                'package on the server — add it to requirements.txt and redeploy.</p></div>')
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
    Back bets always notify when this is on. Notifications only ever go out on <b>Saturdays</b> &mdash; even if races happen to be loaded on another day.
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
      _setStatus('Not enabled on this device yet.');
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
    await fetch('/api/push/subscribe',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{subscription:sub.toJSON(),prefs:_currentPrefs()}})}});
    _setStatus('Enabled on this device.');
  }}catch(e){{
    document.getElementById('pref-enabled').checked=false;
    _setStatus('Could not enable notifications on this device.');
  }}
}}
async function disablePush(){{
  try{{
    var sub=await _getSub();
    if(sub){{
      await fetch('/api/push/unsubscribe',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{endpoint:sub.endpoint}})}});
      await sub.unsubscribe();
    }}
    _setStatus('Notifications disabled on this device.');
  }}catch(e){{ _setStatus('Could not fully disable \u2014 try again.'); }}
}}
async function savePrefsOnly(){{
  var sub=await _getSub();
  if(!sub) return;
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
loadPushSettings();
</script>'''

@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return HTMLResponse(_cached_page("settings", lambda: _shell("settings", _settings_body(), _store)))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT",8000)))
