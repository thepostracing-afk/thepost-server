#!/usr/bin/env python3
"""
The Post — Live Tips Server  v2
=================================
Tips persist until a new push arrives (survives page refreshes).
Pages:
  /              — Tips (Back / Degen / Lay)
  /analyzer      — Full race analyzer (all CSV races + horses)
  /live          — Live odds snapshot
  /api/tips      — raw JSON
  /api/analyzer  — raw JSON
  /api/live      — raw JSON
  POST /push     — desktop app pushes everything at once
"""

import os, json, datetime
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

PUSH_API_KEY = os.environ.get("PUSH_API_KEY", "thepost2026")
STORE_FILE   = "/tmp/thepost_store.json"   # persists across requests, lost on restart

app = FastAPI(title="The Post Tips", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Persistent store ──────────────────────────────────────────────────
def _load_store():
    try:
        if os.path.exists(STORE_FILE):
            with open(STORE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"tips": [], "analyzer": [], "live": [], "last_push": None, "push_count": 0}

def _save_store(s):
    try:
        with open(STORE_FILE, "w") as f:
            json.dump(s, f)
    except Exception:
        pass

_store = _load_store()

# ── Models ────────────────────────────────────────────────────────────
class Tip(BaseModel):
    type:      str
    horse:     str
    race:      str
    time:      str
    track:     str
    units:     float
    real_odds: float
    fair_odds: float
    win_pct:   float
    value_pct: float
    rsi:       float
    tag:       Optional[str] = ""

class HorseRow(BaseModel):
    horse:     str
    barrier:   Any = ""
    jockey:    str = ""
    win_pct:   float = 0
    real_odds: float = 0
    fair_odds: float = 0
    value_pct: float = 0
    rsi:       float = 0

class RaceBlock(BaseModel):
    race:      str
    track:     str
    time:      str
    rsi:       float
    horses:    List[HorseRow]

class LiveRunner(BaseModel):
    horse:    str
    barrier:  Any = ""
    jockey:   str = ""
    open_odds: float = 0
    now_odds:  float = 0
    place_odds: float = 0
    peak_odds:  float = 0
    flucs_pct:  float = 0
    is_top3:    bool = False

class LiveRace(BaseModel):
    race:      str
    track:     str
    time:      str
    runners:   List[LiveRunner]

class PushPayload(BaseModel):
    tips:         List[Tip]
    analyzer:     Optional[List[RaceBlock]] = []
    live:         Optional[List[LiveRace]]  = []
    generated_at: Optional[str] = ""

# ── Push endpoint ─────────────────────────────────────────────────────
@app.post("/push")
async def push(payload: PushPayload, x_api_key: str = Header(default="")):
    if x_api_key != PUSH_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    _store["tips"]       = [t.dict() for t in payload.tips]
    _store["analyzer"]   = [r.dict() for r in (payload.analyzer or [])]
    _store["live"]       = [r.dict() for r in (payload.live or [])]
    _store["last_push"]  = payload.generated_at or datetime.datetime.now().isoformat()
    _store["push_count"] += 1
    _save_store(_store)
    return {"status": "ok", "tips": len(_store["tips"]),
            "analyzer_races": len(_store["analyzer"]),
            "live_races": len(_store["live"])}

@app.get("/api/tips")
async def api_tips(): return JSONResponse(_store["tips"])

@app.get("/api/analyzer")
async def api_analyzer(): return JSONResponse(_store["analyzer"])

@app.get("/api/live")
async def api_live(): return JSONResponse(_store["live"])

@app.get("/api/status")
async def api_status():
    return {"last_push": _store["last_push"], "push_count": _store["push_count"],
            "tips": len(_store["tips"]), "analyzer_races": len(_store["analyzer"]),
            "live_races": len(_store["live"])}

# ── Shared HTML shell ─────────────────────────────────────────────────
def _shell(page_id, body, extra_script=""):
    try:
        dt = datetime.datetime.fromisoformat(_store["last_push"] or "")
        pushed_str = dt.strftime("%d %b  %H:%M")
    except Exception:
        pushed_str = "Never"

    total_tips = len(_store["tips"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0B0F14">
<title>The Post</title>
<style>
:root{{
  --bg:#0B0F14;--panel:#121821;--elevated:#1A222D;--border:#232C38;
  --t1:#E6EDF3;--t2:#8B98A5;--green:#2ECC71;--red:#E74C3C;
  --accent:#3A82F7;--warn:#F0A500;
}}
*{{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}}
body{{font-family:-apple-system,'Segoe UI',Arial,sans-serif;background:var(--bg);color:var(--t1);min-height:100vh;padding-bottom:80px;}}

/* NAV BAR */
.navbar{{position:fixed;bottom:0;left:0;right:0;background:var(--panel);border-top:1px solid var(--border);
  display:flex;z-index:100;padding-bottom:env(safe-area-inset-bottom);}}
.nav-btn{{flex:1;padding:12px 4px 10px;font-size:10px;color:var(--t2);background:none;border:none;
  cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:3px;transition:color .15s;}}
.nav-btn.active{{color:var(--accent);}}
.nav-icon{{font-size:20px;line-height:1;}}

/* HEADER */
.header{{background:var(--panel);border-bottom:1px solid var(--border);
  padding:14px 16px 10px;position:sticky;top:0;z-index:50;}}
.header-row{{display:flex;align-items:center;justify-content:space-between;}}
.app-name{{font-size:18px;font-weight:700;}}
.dot{{width:7px;height:7px;background:var(--green);border-radius:50%;display:inline-block;
  margin-right:5px;animation:pulse 2s infinite;}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
.status{{font-size:11px;color:var(--t2);margin-top:3px;}}
.refresh{{background:var(--elevated);border:1px solid var(--border);color:var(--t1);
  padding:5px 12px;border-radius:7px;font-size:12px;cursor:pointer;}}

/* TABS (within a page) */
.tabs{{display:flex;background:var(--panel);border-bottom:1px solid var(--border);overflow-x:auto;scrollbar-width:none;}}
.tabs::-webkit-scrollbar{{display:none;}}
.tab{{flex:1;min-width:72px;padding:11px 6px;font-size:12px;font-weight:600;color:var(--t2);
  background:none;border:none;border-bottom:2px solid transparent;cursor:pointer;white-space:nowrap;}}
.tab.active{{color:var(--t1);border-bottom-color:var(--accent);}}

/* CONTENT */
.page{{display:none;}}.page.active{{display:block;}}
.section{{display:none;}}.section.active{{display:block;}}
.content{{padding:12px 14px;}}

/* CARDS */
.card{{background:var(--panel);border:1px solid var(--border);border-radius:10px;
  padding:13px 14px;margin-bottom:9px;}}
.card-top{{display:flex;align-items:center;justify-content:space-between;margin-bottom:3px;}}
.horse{{font-size:15px;font-weight:700;}}
.tag{{font-size:10px;font-weight:700;padding:3px 8px;border-radius:20px;letter-spacing:.4px;text-transform:uppercase;}}
.tag.top-play{{background:#1a3a1a;color:var(--green);}}
.tag.secondary{{background:#1a2a4a;color:var(--accent);}}
.tag.watch{{background:#2a2210;color:var(--warn);}}
.meta{{font-size:11px;color:var(--t2);margin-bottom:10px;}}
.stats{{display:flex;gap:6px;flex-wrap:wrap;}}
.stat{{background:var(--elevated);border-radius:6px;padding:7px 8px;flex:1;min-width:48px;text-align:center;}}
.sl{{display:block;font-size:9px;font-weight:700;color:var(--t2);letter-spacing:.5px;margin-bottom:3px;}}
.sv{{font-size:14px;font-weight:700;}}
.sv.pos{{color:var(--green);}} .sv.neg{{color:var(--red);}}

/* SUMMARY ROW */
.summary{{display:flex;gap:8px;margin-bottom:14px;}}
.sum-card{{flex:1;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px;text-align:center;}}
.sum-n{{font-size:22px;font-weight:700;}} .sum-l{{font-size:10px;color:var(--t2);text-transform:uppercase;letter-spacing:.4px;}}

/* RACE BLOCK (analyzer) */
.race-block{{background:var(--panel);border:1px solid var(--border);border-radius:10px;margin-bottom:10px;overflow:hidden;}}
.race-hdr{{display:flex;align-items:center;justify-content:space-between;padding:11px 14px;
  cursor:pointer;border-bottom:1px solid var(--border);}}
.race-hdr-left{{font-size:13px;font-weight:700;}}
.race-hdr-right{{display:flex;align-items:center;gap:10px;}}
.rsi-badge{{font-size:11px;font-weight:700;padding:3px 9px;border-radius:20px;}}
.rsi-elite{{background:#1a3a1a;color:var(--green);}}
.rsi-safe{{background:#1a2a4a;color:var(--accent);}}
.rsi-med{{background:#2a2210;color:var(--warn);}}
.rsi-low{{background:#2a1a1a;color:var(--red);}}
.race-meta{{font-size:11px;color:var(--t2);}}
.race-body{{display:none;}}
.race-body.open{{display:block;}}

/* TABLE */
.tbl{{width:100%;border-collapse:collapse;font-size:11px;}}
.tbl th{{padding:7px 8px;text-align:left;color:var(--t2);font-size:10px;font-weight:700;
  text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid var(--border);background:var(--elevated);}}
.tbl td{{padding:8px 8px;border-bottom:1px solid var(--border);vertical-align:middle;}}
.tbl tr:last-child td{{border-bottom:none;}}
.tbl tr.top3 td:first-child{{color:var(--warn);font-weight:700;}}
.tbl tr:hover td{{background:var(--elevated);}}
.val-pos{{color:var(--green);font-weight:600;}}
.val-neg{{color:var(--red);}}
.odds-col{{text-align:right;font-variant-numeric:tabular-nums;}}
.empty{{color:var(--t2);text-align:center;padding:40px 0;font-size:13px;}}
.arrow-dn{{color:var(--green);font-weight:700;}} .arrow-up{{color:var(--red);font-weight:700;}}

/* LIVE badge */
.live-tag{{font-size:9px;font-weight:700;padding:2px 6px;border-radius:10px;background:#1a2e4a;color:var(--accent);margin-left:6px;}}
</style>
</head>
<body>

<div class="header">
  <div class="header-row">
    <div class="app-name"><span class="dot"></span>The Post</div>
    <button class="refresh" onclick="location.reload()">↻</button>
  </div>
  <div class="status">Last push: {pushed_str} &nbsp;·&nbsp; {total_tips} tip{"s" if total_tips!=1 else ""}</div>
</div>

{body}

<!-- bottom nav -->
<nav class="navbar">
  <button class="nav-btn {'active' if page_id=='tips' else ''}" onclick="location.href='/'">
    <span class="nav-icon">🎯</span>Tips
  </button>
  <button class="nav-btn {'active' if page_id=='analyzer' else ''}" onclick="location.href='/analyzer'">
    <span class="nav-icon">🔍</span>Analyzer
  </button>
  <button class="nav-btn {'active' if page_id=='live' else ''}" onclick="location.href='/live'">
    <span class="nav-icon">📡</span>Live
  </button>
</nav>

<script>
function showTab(name,btn,grp){{
  document.querySelectorAll('.section[data-grp="'+grp+'"]').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('.tab[data-grp="'+grp+'"]').forEach(b=>b.classList.remove('active'));
  document.getElementById('sec-'+name).classList.add('active');
  btn.classList.add('active');
}}
function toggleRace(id){{
  const b=document.getElementById('rb-'+id);
  b.classList.toggle('open');
}}
setTimeout(()=>location.reload(), 90000);
{extra_script}
</script>
</body></html>"""

# ── Tips page ─────────────────────────────────────────────────────────
def _tip_cards(tip_list, type_label):
    if not tip_list:
        return f'<p class="empty">No {type_label} picks yet</p>'
    html = ""
    for t in tip_list:
        tag_html = ""
        if t.get("tag"):
            cls = t["tag"].lower().replace(" ","-")
            tag_html = f'<span class="tag {cls}">{t["tag"]}</span>'
        val_cls = "pos" if t["value_pct"] > 0 else "neg"
        html += f"""<div class="card">
  <div class="card-top"><span class="horse">{t['horse']}</span>{tag_html}</div>
  <div class="meta">{t['time']} · {t['track']} · {t['race']}</div>
  <div class="stats">
    <div class="stat"><span class="sl">ODDS</span><span class="sv">${t['real_odds']:.2f}</span></div>
    <div class="stat"><span class="sl">UNITS</span><span class="sv">{int(t['units'])}u</span></div>
    <div class="stat"><span class="sl">VALUE</span><span class="sv {val_cls}">{t['value_pct']:+.1f}%</span></div>
    <div class="stat"><span class="sl">RSI</span><span class="sv">{int(t['rsi'])}</span></div>
    <div class="stat"><span class="sl">WIN%</span><span class="sv">{t['win_pct']:.1f}%</span></div>
  </div>
</div>"""
    return html

@app.get("/", response_class=HTMLResponse)
async def tips_page():
    tips  = _store["tips"]
    back  = [t for t in tips if t["type"]=="BACK"]
    lay   = [t for t in tips if t["type"]=="LAY"]
    degen = [t for t in tips if t["type"]=="DEGEN"]

    body = f"""
<div class="tabs">
  <button class="tab active" data-grp="tips" onclick="showTab('back',this,'tips')">Back ({len(back)})</button>
  <button class="tab"        data-grp="tips" onclick="showTab('degen',this,'tips')">Degen ({len(degen)})</button>
  <button class="tab"        data-grp="tips" onclick="showTab('lay',this,'tips')">Lay ({len(lay)})</button>
</div>
<div class="content">
  <div class="summary">
    <div class="sum-card"><div class="sum-n" style="color:var(--green)">{len(back)}</div><div class="sum-l">Back</div></div>
    <div class="sum-card"><div class="sum-n" style="color:var(--warn)">{len(degen)}</div><div class="sum-l">Degen</div></div>
    <div class="sum-card"><div class="sum-n" style="color:var(--red)">{len(lay)}</div><div class="sum-l">Lay</div></div>
  </div>
  <div class="section active" id="sec-back" data-grp="tips">{_tip_cards(back,'back')}</div>
  <div class="section"        id="sec-degen" data-grp="tips">{_tip_cards(degen,'degen')}</div>
  <div class="section"        id="sec-lay"   data-grp="tips">{_tip_cards(lay,'lay')}</div>
</div>"""
    return HTMLResponse(_shell("tips", body))

# ── Analyzer page ─────────────────────────────────────────────────────
@app.get("/analyzer", response_class=HTMLResponse)
async def analyzer_page():
    races = _store["analyzer"]
    if not races:
        body = '<div class="content"><p class="empty">No analyzer data yet — load CSVs in The Post desktop app</p></div>'
        return HTMLResponse(_shell("analyzer", body))

    blocks = ""
    for idx, r in enumerate(races):
        rsi = r.get("rsi", 0)
        if rsi >= 80:   rsi_cls, rsi_lbl = "rsi-elite", f"RSI {int(rsi)}"
        elif rsi >= 70: rsi_cls, rsi_lbl = "rsi-safe",  f"RSI {int(rsi)}"
        elif rsi >= 60: rsi_cls, rsi_lbl = "rsi-med",   f"RSI {int(rsi)}"
        else:           rsi_cls, rsi_lbl = "rsi-low",   f"RSI {int(rsi)}"

        rows = ""
        for i, h in enumerate(r.get("horses", [])):
            val = h.get("value_pct", 0)
            val_cls = "val-pos" if val > 0 else ("val-neg" if val < 0 else "")
            top3_cls = "top3" if i < 3 else ""
            rows += f"""<tr class="{top3_cls}">
  <td>{h['horse']}</td>
  <td class="odds-col">{h['win_pct']:.1f}%</td>
  <td class="odds-col">${h['real_odds']:.2f}</td>
  <td class="odds-col">${h['fair_odds']:.2f}</td>
  <td class="odds-col {val_cls}">{val:+.1f}%</td>
  <td>{h.get('jockey','')[:12]}</td>
</tr>"""

        blocks += f"""<div class="race-block">
  <div class="race-hdr" onclick="toggleRace({idx})">
    <div>
      <div class="race-hdr-left">{r['time']} · {r['track']}</div>
      <div class="race-meta">{r['race']}</div>
    </div>
    <div class="race-hdr-right">
      <span class="rsi-badge {rsi_cls}">{rsi_lbl}</span>
    </div>
  </div>
  <div class="race-body" id="rb-{idx}">
    <table class="tbl">
      <thead><tr><th>Horse</th><th>Win%</th><th>Odds</th><th>Fair</th><th>Val%</th><th>Jockey</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div>"""

    body = f'<div class="content">{blocks}</div>'
    return HTMLResponse(_shell("analyzer", body))

# ── Live Odds page ────────────────────────────────────────────────────
@app.get("/live", response_class=HTMLResponse)
async def live_page():
    races = _store["live"]
    if not races:
        body = '<div class="content"><p class="empty">No live odds yet — load a date in The Post desktop app</p></div>'
        return HTMLResponse(_shell("live", body))

    blocks = ""
    for idx, r in enumerate(races):
        runners = r.get("runners", [])
        rows = ""
        for h in runners:
            fluc = h.get("flucs_pct", 0)
            if fluc > 5:    fluc_cls, arrow = "val-pos", "▼"
            elif fluc < -5: fluc_cls, arrow = "val-neg", "▲"
            else:           fluc_cls, arrow = "",        "–"

            top3_cls = "top3" if h.get("is_top3") else ""
            rows += f"""<tr class="{top3_cls}">
  <td>{h['horse']}</td>
  <td class="odds-col">${h.get('open_odds',0):.2f}</td>
  <td class="odds-col">${h.get('now_odds',0):.2f}</td>
  <td class="odds-col">${h.get('place_odds',0):.2f}</td>
  <td class="odds-col {fluc_cls}">{arrow} {abs(fluc):.1f}%</td>
</tr>"""

        blocks += f"""<div class="race-block">
  <div class="race-hdr" onclick="toggleRace('L{idx}')">
    <div>
      <div class="race-hdr-left">{r['time']} · {r['track']}<span class="live-tag">LIVE</span></div>
      <div class="race-meta">{r['race']} · {len(runners)} runners</div>
    </div>
    <span style="color:var(--t2);font-size:18px;">›</span>
  </div>
  <div class="race-body" id="rb-L{idx}">
    <table class="tbl">
      <thead><tr><th>Horse</th><th>Open</th><th>Now</th><th>Place</th><th>Flucs</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div>"""

    body = f'<div class="content">{blocks}</div>'
    return HTMLResponse(_shell("live", body))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
