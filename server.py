#!/usr/bin/env python3
"""The Post — Live Tips Server  v5 (persistent storage via Upstash Redis)"""

import os, json, datetime, base64, hashlib
from urllib.parse import quote
import requests
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

PUSH_API_KEY = os.environ.get("PUSH_API_KEY", "thepost2026")

# --- Upstash Redis REST config ---
UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
STORE_KEY     = "thepost_store"

DEFAULT_STORE = {"tips": [], "analyzer": [], "live": [], "last_push": None, "push_count": 0}

app = FastAPI(title="The Post", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ICON_PATH = os.path.join(os.path.dirname(__file__), "thepost.png")

# ---------------------------------------------------------------------------
# Silk images — single shared component used by both the Tips cards and the
# Analyzer tables, so silks look and behave identically everywhere.
# ---------------------------------------------------------------------------
SILK_SIZE      = 22   # px — identical size used in every location, tuned for the slimline mobile cards
SILK_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "silk_cache")
os.makedirs(SILK_CACHE_DIR, exist_ok=True)
_silk_mem_cache = {}   # sha1(url) -> (content_type, bytes) — hot in-process cache

def _get_silk_url(d):
    """Pull whatever silk URL field is present on a tip/horse dict, trying
    every naming convention the desktop client might push (never generated,
    never substituted — only ever the real asset's own URL)."""
    return (
        d.get("silk_url") or d.get("SilkURL")
        or d.get("silk")     or d.get("Silk")
        or d.get("silk_image_url") or ""
    ).strip()

def _silk_html(url, size=SILK_SIZE):
    """Render the shared silk component. If no silk is available, render an
    identically-sized blank slot instead — never a placeholder icon, never a
    different image."""
    if not url:
        return f'<span class="silk-wrap" style="width:{size}px;height:{size}px;"></span>'
    # The desktop app now embeds the silk directly as a base64 data: URI
    # (it resolves the local silk file or CDN URL itself, so the browser
    # never has to fetch anything). Only route through the /silk network
    # proxy for a genuine remote URL — a data: URI can't be fetched by
    # requests.get() and should just be used as-is.
    src = url if url.startswith("data:") else f"/silk?u={quote(url, safe='')}"
    return (
        f'<span class="silk-wrap" style="width:{size}px;height:{size}px;">'
        f'<img class="silk-img" src="{src}" width="{size}" height="{size}" '
        f'loading="lazy" decoding="async" alt="" '
        f'onerror="this.style.visibility=\'hidden\'">'
        f'</span>'
    )

@app.get("/silk")
async def silk_proxy(u: str = ""):
    """Fetch-once, cache-forever proxy for a horse's real silk image. Serves
    from an in-memory cache first, then an on-disk cache, and only ever
    downloads a given silk URL once. Never generates or substitutes an image —
    if the fetch fails, the caller gets a 404 and the front-end leaves the
    slot blank."""
    if not u:
        raise HTTPException(status_code=404, detail="no silk")
    key = hashlib.sha1(u.encode("utf-8")).hexdigest()

    if key in _silk_mem_cache:
        ctype, data = _silk_mem_cache[key]
        return Response(data, media_type=ctype, headers={"Cache-Control": "public, max-age=604800, immutable"})

    ext = ".svg" if u.lower().split("?")[0].endswith(".svg") else ".img"
    disk_path = os.path.join(SILK_CACHE_DIR, key + ext)

    if os.path.exists(disk_path):
        with open(disk_path, "rb") as f:
            data = f.read()
        ctype = "image/svg+xml" if ext == ".svg" else "image/png"
        _silk_mem_cache[key] = (ctype, data)
        return Response(data, media_type=ctype, headers={"Cache-Control": "public, max-age=604800, immutable"})

    try:
        r = requests.get(u, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.content
        ctype = r.headers.get("content-type", "").split(";")[0].strip()
        if not ctype or "text/html" in ctype:
            ctype = "image/svg+xml" if ext == ".svg" else "image/png"
        with open(disk_path, "wb") as f:
            f.write(data)
        _silk_mem_cache[key] = (ctype, data)
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
    return JSONResponse({"name":"The Post","short_name":"The Post","description":"Racing Intelligence","start_url":"/","display":"standalone","background_color":"#0B0F14","theme_color":"#0B0F14","orientation":"portrait","icons":[{"src":"/icon.png","sizes":"512x512","type":"image/png"}]})

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
    """Write the store JSON to Upstash."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return
    try:
        payload = json.dumps(s)
        # Upstash REST SET: POST /set/<key> with raw value as body
        requests.post(f"{UPSTASH_URL}/set/{STORE_KEY}", headers=_headers(), data=payload, timeout=5)
    except Exception:
        pass

_store = _load()

@app.post("/push")
async def push(request: Request, x_api_key: str = Header(default="")):
    if x_api_key != PUSH_API_KEY: raise HTTPException(status_code=401,detail="Invalid API key")
    try: body = await request.json()
    except: raise HTTPException(status_code=400,detail="Invalid JSON")
    _store["tips"]        = body.get("tips",[])
    _store["analyzer"]    = body.get("analyzer",[])
    _store["live"]        = body.get("live",[])
    _store["last_push"]   = body.get("generated_at") or datetime.datetime.now().isoformat()
    _store["push_count"] += 1
    _save(_store)
    return {"status":"ok","tips":len(_store["tips"]),"analyzer_races":len(_store["analyzer"]),"live_races":len(_store["live"])}

@app.get("/api/tips")
async def api_tips():
    return JSONResponse(_load()["tips"])

@app.get("/api/analyzer")
async def api_analyzer():
    return JSONResponse(_load()["analyzer"])

@app.get("/api/status")
async def api_status():
    s = _load()
    return {"last_push":s["last_push"],"push_count":s["push_count"],"tips":len(s["tips"]),"analyzer_races":len(s["analyzer"])}

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
    """Full stat rundown for one horse — every field the desktop model tracks,
    laid out as a compact grid. Missing/zero fields are simply omitted."""
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
    """Stable id for a race, independent of list ordering/sort, so links can
    point at a specific race regardless of which sort is applied server-side."""
    raw = f'{r.get("track","")}|{r.get("race","")}|{r.get("time","")}'
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]

def _time_key(tstr):
    """Numeric sort key for race times.
    Assumes:
      - 11:xx and 12:xx are before 1 PM.
      - 1:xx–10:xx are afternoon races (13:xx–22:xx) unless AM/PM is specified.
    """
    try:
        t = str(tstr).strip().upper()

        if t.endswith("AM") or t.endswith("PM"):
            dt = datetime.datetime.strptime(t, "%I:%M %p")
            return dt.hour * 60 + dt.minute

        parts = t.split(":")
        h = int(parts[0])
        m = int(parts[1][:2]) if len(parts) > 1 else 0

        # Convert 1:00–10:59 to afternoon
        if 1 <= h <= 10:
            h += 12

        # Leave 11:xx and 12:xx unchanged
        return h * 60 + m

    except Exception:
        return 99999.0

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
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js" defer></script>
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
.sc{flex:1;background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:8px 4px;text-align:center;}
.sc-link{cursor:pointer;}
.sn{font-size:18px;font-weight:700;}.sl2{font-size:9px;color:var(--t2);text-transform:uppercase;letter-spacing:.4px;}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:10px;}
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
  <button class="nbtn """ + ("active" if page_id=="watch" else "") + """ " onclick="location.href='/portal/watch'"><span class="ni">&#x1F4FA;</span>Watch</button>
""" if friend else """  <button class="nbtn """ + ("active" if page_id=="dash" else "") + """ " onclick="location.href='/dash'"><span class="ni">&#x1F4CA;</span>Dashboard</button>
  <button class="nbtn """ + ("active" if page_id=="analyzer" else "") + """ " onclick="location.href='/analyzer'"><span class="ni">&#x1F50D;</span>Analyzer</button>
  <button class="nbtn """ + ("active" if page_id=="watch" else "") + """ " onclick="location.href='/watch'"><span class="ni">&#x1F4FA;</span>Watch</button>
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
  var containers=['cards-container','cards-container-d','cards-container-l','cards-container-p'];
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
function setContainer(id){var m={tb:'cards-container',td:'cards-container-d',tl:'cards-container-l',tp:'cards-container-p'};_activeContainer=m[id]||'cards-container';}
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
  blocks.sort(function(a,b){return (a.dataset[key]||'').localeCompare(b.dataset[key]||'');});
  blocks.forEach(function(b){c.appendChild(b);});
}

// ---------------------------------------------------------------------------
// Export Photo — renders the current tab's tips into an offscreen replica of
// the tips page (same cards, styling) but with the RSI and VALUE stats
// stripped out of each card and the header swapped for the app logo, then
// rasterizes it to a PNG the user can save or share.
// ---------------------------------------------------------------------------
var _logoCutoutCache=null;
function _isIOS(){
  return /iP(hone|od|ad)/.test(navigator.userAgent) ||
    (navigator.platform==='MacIntel' && navigator.maxTouchPoints>1);
}
function _isCanvasBlank(canvas){
  // Safari's foreignObject render path occasionally succeeds but paints
  // nothing (a known WebKit quirk) rather than throwing — sample a spread
  // of pixels' alpha channel to catch that case so we can fall back.
  try{
    var ctx=canvas.getContext('2d');
    var d=ctx.getImageData(0,0,canvas.width,canvas.height).data;
    for(var i=3;i<d.length;i+=811){ if(d[i]!==0) return false; }
    return true;
  }catch(e){ return false; }
}
function _seamlessLogo(){
  // The source /icon.png is a solid-black square with a white mark on it.
  // Rather than paste that black square onto the app's dark-navy (--bg)
  // background — which leaves a visible seam — key the black out into real
  // transparency (alpha = luminance) so the mark just floats on whatever's
  // behind it, seamlessly, regardless of the exact background colour.
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

function exportPhoto(){
  if(typeof html2canvas==='undefined'){ alert('Export library failed to load — check your connection.'); return; }
  var activeSection=document.querySelector('.section.active');
  if(!activeSection){ alert('Nothing to export yet.'); return; }

  var toast=document.getElementById('export-toast');
  toast.style.display='block';

  var clone=activeSection.cloneNode(true);
  clone.classList.add('active');

  // Strip RSI + VALUE stat tiles from every card, then rebalance the grid.
  // Silk images are kept — just forced to load eagerly so they're actually
  // painted in by the time html2canvas rasterizes the page.
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

    // Wait for every image (logo + silks) to actually finish loading —
    // cloned <img> tags don't carry over the "already loaded" state, so
    // capturing immediately would rasterize blank slots. Each image gets a
    // hard 3s cap so one stuck/broken image can't stall the whole export.
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
    // useCORS forces a fresh crossOrigin fetch even for images the browser
    // already has cached (silks are inline data: URIs now anyway, and the
    // logo/icon is same-origin) — dropping it avoids a redundant re-download.
    //
    // iOS Safari's canvas engine reconstructs every border-radius/box-shadow
    // by hand in html2canvas's default renderer, which is dramatically
    // slower than on Chrome/Android for card-heavy markup like this. Safari
    // supports rendering via an SVG <foreignObject> instead, which lets
    // WebKit's own (fast) native renderer do that work — so we try that
    // first on iOS, with an automatic fallback to the default renderer if
    // it comes back blank (a known occasional WebKit quirk).
    var bg=getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()||'#0B0F14';
    var iOS=_isIOS();
    var baseOpts={backgroundColor:bg,logging:false,imageTimeout:4000,scale:iOS?1.25:1.5};
    var foOpts=Object.assign({},baseOpts,{foreignObjectRendering:true});
    var render=iOS ? html2canvas(page,foOpts) : html2canvas(page,baseOpts);
    return render.then(function(canvas){
      if(iOS && _isCanvasBlank(canvas)) return html2canvas(page,baseOpts);
      return canvas;
    }).catch(function(){
      // foreignObjectRendering threw outright — standard renderer as fallback.
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

  // Hard ceiling on the whole process — if anything upstream (a stuck
  // image, an html2canvas edge case) never resolves, this fires anyway so
  // the user gets an error and their UI back instead of a frozen toast.
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
""" + ("" if page_id=="watch" else "setTimeout(function(){location.reload();},90000);") + """
</script>
</body></html>"""

def _cards_js(tips_list, label, container_id):
    if not tips_list:
        return f'<p class="empty">No {label} picks yet</p>'
    out = f'<div id="{container_id}">'
    for t in tips_list:
        cls = (t.get("tag") or "").lower().replace(" ","-")
        tag_html = f'<span class="tag {cls}">{t["tag"]}</span>' if t.get("tag") else ""
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
            f'<div class="stat"><span class="sl">ODDS</span><span class="sv">${t.get("real_odds",0):.2f}</span></div>'
            f'<div class="stat"><span class="sl">UNITS</span><span class="sv">{int(t.get("units",1))}u</span></div>'
            f'<div class="stat"><span class="sl">VALUE</span><span class="sv {vc}">{t.get("value_pct",0):+.1f}%</span></div>'
            f'<div class="stat"><span class="sl">RSI</span><span class="sv">{int(t.get("rsi",0))}</span></div>'
            f'<div class="stat"><span class="sl">WIN%</span><span class="sv">{t.get("win_pct",0):.1f}%</span></div>'
            f'</div></div>'
        )
    out += "</div>"
    return out

def _tips_body(store):
    tips  = store["tips"]
    back  = sorted([t for t in tips if t["type"]=="BACK"],  key=lambda t: t.get("track",""))
    degen = sorted([t for t in tips if t["type"]=="DEGEN"], key=lambda t: t.get("track",""))
    lay   = sorted([t for t in tips if t["type"]=="LAY"],   key=lambda t: t.get("track",""))
    place = sorted([t for t in tips if t["type"]=="PLACE"], key=lambda t: t.get("track",""))
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
        f'<button id="btn-td" class="tab" data-tab="tips" onclick="switchTab(\'td\',this,\'tips\');setContainer(\'td\')">Degen ({len(degen)})</button>'
        f'<button id="btn-tl" class="tab" data-tab="tips" onclick="switchTab(\'tl\',this,\'tips\');setContainer(\'tl\')">Lay ({len(lay)})</button>'
        f'<button id="btn-tp" class="tab" data-tab="tips" onclick="switchTab(\'tp\',this,\'tips\');setContainer(\'tp\')">Place ({len(place)})</button>'
        '</div>'
        + sort_bar +
        '<div class="content">'
        '<div class="summary">'
        f'<div class="sc"><div class="sn" style="color:var(--green)">{len(back)}</div><div class="sl2">Back</div></div>'
        f'<div class="sc"><div class="sn" style="color:var(--warn)">{len(degen)}</div><div class="sl2">Degen</div></div>'
        f'<div class="sc"><div class="sn" style="color:var(--red)">{len(lay)}</div><div class="sl2">Lay</div></div>'
        f'<div class="sc"><div class="sn" style="color:var(--acc)">{len(place)}</div><div class="sl2">Place</div></div>'
        '</div>'
        f'<div class="section active" id="tb" data-grp="tips">{_cards_js(back,"back","cards-container")}</div>'
        f'<div class="section" id="td" data-grp="tips">{_cards_js(degen,"degen","cards-container-d")}</div>'
        f'<div class="section" id="tl" data-grp="tips">{_cards_js(lay,"lay","cards-container-l")}</div>'
        f'<div class="section" id="tp" data-grp="tips">{_cards_js(place,"place","cards-container-p")}</div>'
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
async def tips_page():
    store = _load()
    return HTMLResponse(_shell("tips", _tips_body(store), store))

@app.get("/portal", response_class=HTMLResponse)
async def portal_tips_page():
    store = _load()
    return HTMLResponse(_shell("tips", _tips_body(store), store, friend=True))

def _dash_body(store, friend=False):
    tips     = store["tips"]
    analyzer = store["analyzer"]
    back  = [t for t in tips if t["type"]=="BACK"]
    degen = [t for t in tips if t["type"]=="DEGEN"]
    lay   = [t for t in tips if t["type"]=="LAY"]
    place = [t for t in tips if t["type"]=="PLACE"]
    all_t = back+degen+lay+place
    total_u  = sum(t.get("units",0) for t in all_t)
    avg_odds = (sum(t.get("real_odds",0) for t in all_t)/len(all_t)) if all_t else 0
    avg_win  = (sum(t.get("win_pct",0) for t in all_t)/len(all_t)) if all_t else 0
    tracks   = ", ".join(sorted({t.get("track","") for t in all_t if t.get("track")})) or "—"
    t_races  = len(analyzer)
    t_run    = sum(len(r.get("horses",[])) for r in analyzer)
    pushed   = _pushed_str(store)

    tips_base = "/portal" if friend else "/"

    # Bet-type boxes are now the only route into Tips — clicking one jumps
    # straight to that tab.
    def _sc(tid, count, color, label):
        return (
            f'<div class="sc sc-link" onclick="location.href=\'{tips_base}?tab={tid}\'">'
            f'<div class="sn" style="color:{color}">{count}</div><div class="sl2">{label}</div></div>'
        )

    # Next 5 races — every race is rendered as a hidden template row tagged
    # with its raw time string. Client-side JS reads the viewer's own local
    # clock (same approach as the Analyzer countdown), drops any race whose
    # start time has already passed, and shows only the soonest 5 that are
    # still upcoming. It re-checks every 30s so races roll off the list live
    # as the day goes on, without needing the page to reload.
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
        + _sc("td", len(degen), "var(--warn)",  "Degen")
        + _sc("tl", len(lay),   "var(--red)",   "Lay")
        + _sc("tp", len(place), "var(--acc)",   "Place")
        + '</div>'
        '<div class="stat-grid">'
        f'<div class="stat-card green"><div class="stat-label">Total Units</div><div class="stat-value">{total_u:.0f}u</div><div class="stat-sub">{len(all_t)} selections</div></div>'
        f'<div class="stat-card warn"><div class="stat-label">Avg Odds</div><div class="stat-value">${avg_odds:.2f}</div><div class="stat-sub">Avg win {avg_win:.1f}%</div></div>'
        f'<div class="stat-card green" style="grid-column:1/-1;"><div class="stat-label">Races Loaded</div><div class="stat-value">{t_races}</div><div class="stat-sub">{t_run} runners</div></div>'
        '</div>'
        + next_html +
        f'<div class="card" style="margin-bottom:9px;"><div class="stat-label" style="margin-bottom:8px;">Tracks Today</div><div style="font-size:13px;line-height:1.6;">{tracks}</div></div>'
        f'<div class="card"><div class="stat-label" style="margin-bottom:8px;">Last Push</div><div style="font-size:14px;font-weight:600;">{pushed}</div><div style="font-size:11px;color:var(--t2);margin-top:3px;">Push #{store["push_count"]}</div></div>'
        '</div>'
    )

@app.get("/dash", response_class=HTMLResponse)
async def dash_page():
    store = _load()
    return HTMLResponse(_shell("dash", _dash_body(store), store))

@app.get("/portal/dash", response_class=HTMLResponse)
async def portal_dash_page():
    store = _load()
    return HTMLResponse(_shell("dash", _dash_body(store, friend=True), store, friend=True))

@app.get("/analyzer", response_class=HTMLResponse)
async def analyzer_page():
    store = _load()
    races = sorted(store["analyzer"], key=lambda r: r.get("track",""))
    if not races:
        return HTMLResponse(_shell("analyzer",'<div class="content"><p class="empty">No analyzer data yet</p></div>', store))
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
            f'<div class="rblock" id="race-{rid}" data-time="{r.get("time","")}" data-track="{r.get("track","")}">'
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
        '<button class="asort-btn sort-btn" onclick="sortAnalyzer(\'time\',this)">&#x1F550; Time</button>'
        '<button class="asort-btn sort-btn active" onclick="sortAnalyzer(\'track\',this)">Track A-Z</button>'
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
    return HTMLResponse(_shell("analyzer", sort_bar + f'<div class="content"><div id="races-container">{blocks}</div></div>' + countdown_script, store))

# ---------------------------------------------------------------------------
# Watch — embedded live-stream tab. Each source is the broadcaster's own
# page loaded in an iframe so no video is ever extracted, re-hosted, or
# proxied — this just puts their official player inside the app shell.
# Update the URLs below if either broadcaster's live-vision widget moves to
# a different page than its homepage.
# ---------------------------------------------------------------------------
STREAM_SOURCES = [
    {"id": "racingcom", "label": "Racing.com", "url": "https://www.racing.com/"},
    {"id": "racingnsw", "label": "RacingNSW",   "url": "https://www.racingnsw.com.au/"},
]

def _watch_body():
    tabs_html = "".join(
        f'<button class="tab{" active" if i==0 else ""}" data-tab="watch" '
        f'onclick="switchWatch(\'{s["id"]}\',this)">{s["label"]}</button>'
        for i, s in enumerate(STREAM_SOURCES)
    )
    panes_html = "".join(
        f'''<div class="watch-pane{" active" if i==0 else ""}" id="watch-{s["id"]}">
  <div class="video-frame">
    <iframe src="{s["url"]}" loading="lazy" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen referrerpolicy="no-referrer-when-downgrade"></iframe>
  </div>
  <div class="watch-fallback">
    <span>Player not loading here? Some sites block embedding.</span>
    <a class="wbtn" href="{s["url"]}" target="_blank" rel="noopener">&#x2197; Open</a>
  </div>
</div>'''
        for i, s in enumerate(STREAM_SOURCES)
    )
    ids_json = json.dumps([s["id"] for s in STREAM_SOURCES])
    return f'''<div class="tabs">{tabs_html}</div>
<div class="content">
<div class="watch-note">Streams load the broadcaster's own site directly &mdash; if you're logged in / subscribed there in your regular browser, you may need to log in here too the first time.</div>
{panes_html}
</div>
<script>
var _watchIds={ids_json};
function switchWatch(id,btn){{
  document.querySelectorAll('[data-tab="watch"]').forEach(function(b){{b.classList.remove('active');}});
  btn.classList.add('active');
  document.querySelectorAll('.watch-pane').forEach(function(p){{p.classList.remove('active');}});
  var pane=document.getElementById('watch-'+id);
  if(pane) pane.classList.add('active');
  try{{localStorage.setItem('thepost_watch_tab',id);}}catch(e){{}}
}}
(function(){{
  var last=null;
  try{{ last=localStorage.getItem('thepost_watch_tab'); }}catch(e){{}}
  if(!last || _watchIds.indexOf(last)===-1) return;
  var idx=_watchIds.indexOf(last);
  var btns=document.querySelectorAll('[data-tab="watch"]');
  if(btns[idx]) switchWatch(last,btns[idx]);
}})();
</script>'''

@app.get("/watch", response_class=HTMLResponse)
async def watch_page():
    store = _load()
    return HTMLResponse(_shell("watch", _watch_body(), store))

@app.get("/portal/watch", response_class=HTMLResponse)
async def portal_watch_page():
    store = _load()
    return HTMLResponse(_shell("watch", _watch_body(), store, friend=True))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT",8000)))
