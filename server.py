r · PY
#!/usr/bin/env python3
"""
The Post — Live Tips Server  v3
Tips + Analyzer + Live Odds. Schema-free payload so it never 422s.
"""
 
import os, json, datetime
from typing import List, Optional, Any, Dict
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
 
PUSH_API_KEY = os.environ.get("PUSH_API_KEY", "thepost2026")
STORE_FILE   = "/tmp/thepost_store.json"
 
app = FastAPI(title="The Post Tips", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
 
def _load():
    try:
        if os.path.exists(STORE_FILE):
            with open(STORE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"tips": [], "analyzer": [], "live": [], "last_push": None, "push_count": 0}
 
def _save(s):
    try:
        with open(STORE_FILE, "w") as f:
            json.dump(s, f)
    except Exception:
        pass
 
_store = _load()
 
# ── Schema-free push — accept any JSON, just require the top-level keys ──
@app.post("/push")
async def push(request: Request, x_api_key: str = Header(default="")):
    if x_api_key != PUSH_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
 
    _store["tips"]        = body.get("tips", [])
    _store["analyzer"]    = body.get("analyzer", [])
    _store["live"]        = body.get("live", [])
    _store["last_push"]   = body.get("generated_at") or datetime.datetime.now().isoformat()
    _store["push_count"] += 1
    _save(_store)
    return {
        "status":        "ok",
        "tips":          len(_store["tips"]),
        "analyzer_races":len(_store["analyzer"]),
        "live_races":    len(_store["live"]),
    }
 
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
 
# ── Shared shell ──────────────────────────────────────────────────────
def _pushed_str():
    try:
        return datetime.datetime.fromisoformat(_store["last_push"]).strftime("%d %b  %H:%M")
    except Exception:
        return "Never"
 
def _shell(page_id, body):
    pushed = _pushed_str()
    total  = len(_store["tips"])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0B0F14">
<title>The Post</title>
<style>
:root{{--bg:#0B0F14;--panel:#121821;--el:#1A222D;--bd:#232C38;
  --t1:#E6EDF3;--t2:#8B98A5;--green:#2ECC71;--red:#E74C3C;--acc:#3A82F7;--warn:#F0A500;}}
*{{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}}
body{{font-family:-apple-system,'Segoe UI',Arial,sans-serif;background:var(--bg);
  color:var(--t1);min-height:100vh;padding-bottom:72px;font-size:13px;}}
.header{{background:var(--panel);border-bottom:1px solid var(--bd);
  padding:14px 16px 10px;position:sticky;top:0;z-index:50;}}
.hrow{{display:flex;align-items:center;justify-content:space-between;}}
.appname{{font-size:18px;font-weight:700;}}
.dot{{width:7px;height:7px;background:var(--green);border-radius:50%;
  display:inline-block;margin-right:5px;animation:pulse 2s infinite;}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.status{{font-size:11px;color:var(--t2);margin-top:3px;}}
.rbtn{{background:var(--el);border:1px solid var(--bd);color:var(--t1);
  padding:5px 12px;border-radius:7px;font-size:12px;cursor:pointer;}}
.navbar{{position:fixed;bottom:0;left:0;right:0;background:var(--panel);
  border-top:1px solid var(--bd);display:flex;z-index:100;
  padding-bottom:env(safe-area-inset-bottom);}}
.nbtn{{flex:1;padding:12px 4px 10px;font-size:10px;color:var(--t2);background:none;
  border:none;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:3px;}}
.nbtn.active{{color:var(--acc);}}
.ni{{font-size:20px;line-height:1;}}
.tabs{{display:flex;background:var(--panel);border-bottom:1px solid var(--bd);
  overflow-x:auto;scrollbar-width:none;}}
.tabs::-webkit-scrollbar{{display:none;}}
.tab{{flex:1;min-width:72px;padding:11px 6px;font-size:12px;font-weight:600;
  color:var(--t2);background:none;border:none;border-bottom:2px solid transparent;
  cursor:pointer;white-space:nowrap;}}
.tab.active{{color:var(--t1);border-bottom-color:var(--acc);}}
.section{{display:none;}}.section.active{{display:block;}}
.content{{padding:12px 14px;}}
.card{{background:var(--panel);border:1px solid var(--bd);border-radius:10px;
  padding:13px 14px;margin-bottom:9px;}}
.ctop{{display:flex;align-items:center;justify-content:space-between;margin-bottom:3px;}}
.horse{{font-size:15px;font-weight:700;}}
.tag{{font-size:10px;font-weight:700;padding:3px 8px;border-radius:20px;
  letter-spacing:.4px;text-transform:uppercase;}}
.tag.top-play{{background:#1a3a1a;color:var(--green);}}
.tag.secondary{{background:#1a2a4a;color:var(--acc);}}
.tag.watch{{background:#2a2210;color:var(--warn);}}
.meta{{font-size:11px;color:var(--t2);margin-bottom:10px;}}
.stats{{display:flex;gap:6px;flex-wrap:wrap;}}
.stat{{background:var(--el);border-radius:6px;padding:7px 8px;flex:1;min-width:48px;text-align:center;}}
.sl{{display:block;font-size:9px;font-weight:700;color:var(--t2);letter-spacing:.5px;margin-bottom:3px;}}
.sv{{font-size:14px;font-weight:700;}}
.pos{{color:var(--green);}} .neg{{color:var(--red);}}
.summary{{display:flex;gap:8px;margin-bottom:14px;}}
.sc{{flex:1;background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:10px;text-align:center;}}
.sn{{font-size:22px;font-weight:700;}} .sl2{{font-size:10px;color:var(--t2);text-transform:uppercase;letter-spacing:.4px;}}
.rblock{{background:var(--panel);border:1px solid var(--bd);border-radius:10px;
  margin-bottom:9px;overflow:hidden;}}
.rhdr{{display:flex;align-items:center;justify-content:space-between;
  padding:11px 14px;cursor:pointer;}}
.rleft{{font-size:13px;font-weight:700;}}
.rmeta{{font-size:11px;color:var(--t2);margin-top:1px;}}
.rbody{{display:none;border-top:1px solid var(--bd);}}
.rbody.open{{display:block;}}
.tbl{{width:100%;border-collapse:collapse;font-size:11px;}}
.tbl th{{padding:7px 8px;text-align:left;color:var(--t2);font-size:10px;
  font-weight:700;text-transform:uppercase;letter-spacing:.4px;
  background:var(--el);border-bottom:1px solid var(--bd);}}
.tbl td{{padding:8px 8px;border-bottom:1px solid var(--bd);}}
.tbl tr:last-child td{{border-bottom:none;}}
.tbl .top td:first-child{{color:var(--warn);font-weight:700;}}
.vp{{color:var(--green);font-weight:600;}} .vn{{color:var(--red);}}
.ar{{text-align:right;font-variant-numeric:tabular-nums;}}
.rb{{font-size:11px;font-weight:700;padding:3px 9px;border-radius:20px;}}
.re{{background:#1a3a1a;color:var(--green);}}
.rs{{background:#1a2a4a;color:var(--acc);}}
.rm{{background:#2a2210;color:var(--warn);}}
.rl{{background:#2a1a1a;color:var(--red);}}
.ltag{{font-size:9px;font-weight:700;padding:2px 6px;border-radius:10px;
  background:#1a2e4a;color:var(--acc);margin-left:6px;}}
.dn{{color:var(--green);font-weight:700;}} .up{{color:var(--red);font-weight:700;}}
.empty{{color:var(--t2);text-align:center;padding:40px 0;font-size:13px;}}
</style>
</head>
<body>
<div class="header">
  <div class="hrow">
    <div class="appname"><span class="dot"></span>The Post</div>
    <button class="rbtn" onclick="location.reload()">↻</button>
  </div>
  <div class="status">Last push: {pushed} &nbsp;·&nbsp; {total} tip{"s" if total!=1 else ""}</div>
</div>
{body}
<nav class="navbar">
  <button class="nbtn {'active' if page_id=='tips' else ''}" onclick="go('/')">
    <span class="ni">🎯</span>Tips</button>
  <button class="nbtn {'active' if page_id=='analyzer' else ''}" onclick="go('/analyzer')">
    <span class="ni">🔍</span>Analyzer</button>
  <button class="nbtn {'active' if page_id=='live' else ''}" onclick="go('/live')">
    <span class="ni">📡</span>Live</button>
</nav>
<script>
function go(u){{location.href=u;}}
function showTab(id,btn,grp){{
  document.querySelectorAll('[data-grp="'+grp+'"]').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('[data-tab="'+grp+'"]').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}}
function tog(id){{document.getElementById(id).classList.toggle('open');}}
setTimeout(()=>location.reload(),90000);
</script>
</body></html>"""
 
# ── Tips page ─────────────────────────────────────────────────────────
def _cards(lst, label):
    if not lst:
        return f'<p class="empty">No {label} picks yet</p>'
    out = ""
    for t in lst:
        cls = (t.get("tag") or "").lower().replace(" ","-")
        tag = f'<span class="tag {cls}">{t["tag"]}</span>' if t.get("tag") else ""
        vc  = "pos" if t["value_pct"]>0 else "neg"
        out += f"""<div class="card">
<div class="ctop"><span class="horse">{t['horse']}</span>{tag}</div>
<div class="meta">{t['time']} · {t['track']} · {t['race']}</div>
<div class="stats">
  <div class="stat"><span class="sl">ODDS</span><span class="sv">${t['real_odds']:.2f}</span></div>
  <div class="stat"><span class="sl">UNITS</span><span class="sv">{int(t['units'])}u</span></div>
  <div class="stat"><span class="sl">VALUE</span><span class="sv {vc}">{t['value_pct']:+.1f}%</span></div>
  <div class="stat"><span class="sl">RSI</span><span class="sv">{int(t['rsi'])}</span></div>
  <div class="stat"><span class="sl">WIN%</span><span class="sv">{t['win_pct']:.1f}%</span></div>
</div></div>"""
    return out
 
@app.get("/", response_class=HTMLResponse)
async def tips_page():
    tips  = _store["tips"]
    back  = [t for t in tips if t["type"]=="BACK"]
    lay   = [t for t in tips if t["type"]=="LAY"]
    degen = [t for t in tips if t["type"]=="DEGEN"]
    body  = f"""
<div class="tabs">
  <button class="tab active" data-tab="tips" onclick="showTab('tb','this','tips')" onclick="showTab('tb',this,'tips')">Back ({len(back)})</button>
  <button class="tab"        data-tab="tips" onclick="showTab('td',this,'tips')">Degen ({len(degen)})</button>
  <button class="tab"        data-tab="tips" onclick="showTab('tl',this,'tips')">Lay ({len(lay)})</button>
</div>
<div class="content">
<div class="summary">
  <div class="sc"><div class="sn" style="color:var(--green)">{len(back)}</div><div class="sl2">Back</div></div>
  <div class="sc"><div class="sn" style="color:var(--warn)">{len(degen)}</div><div class="sl2">Degen</div></div>
  <div class="sc"><div class="sn" style="color:var(--red)">{len(lay)}</div><div class="sl2">Lay</div></div>
</div>
<div class="section active" id="tb" data-grp="tips">{_cards(back,'back')}</div>
<div class="section"        id="td" data-grp="tips">{_cards(degen,'degen')}</div>
<div class="section"        id="tl" data-grp="tips">{_cards(lay,'lay')}</div>
</div>"""
    # fix the first tab button onclick (f-string issue)
    body = body.replace(
        'onclick="showTab(\'tb\',\'this\',\'tips\')" onclick="showTab(\'tb\',this,\'tips\')"',
        'onclick="showTab(\'tb\',this,\'tips\')"'
    )
    return HTMLResponse(_shell("tips", body))
 
# ── Analyzer page ─────────────────────────────────────────────────────
@app.get("/analyzer", response_class=HTMLResponse)
async def analyzer_page():
    races = _store["analyzer"]
    if not races:
        body = '<div class="content"><p class="empty">No analyzer data yet — load CSVs in The Post desktop app</p></div>'
        return HTMLResponse(_shell("analyzer", body))
    blocks = ""
    for i, r in enumerate(races):
        rsi = float(r.get("rsi", 0))
        rc  = "re" if rsi>=80 else ("rs" if rsi>=70 else ("rm" if rsi>=60 else "rl"))
        rows = ""
        for j, h in enumerate(r.get("horses", [])):
            v   = float(h.get("value_pct", 0))
            vc  = "vp" if v>0 else ("vn" if v<0 else "")
            top = "top" if j<3 else ""
            rows += f"""<tr class="{top}">
  <td>{h.get('horse','')}</td>
  <td class="ar">{h.get('win_pct',0):.1f}%</td>
  <td class="ar">${h.get('real_odds',0):.2f}</td>
  <td class="ar">${h.get('fair_odds',0):.2f}</td>
  <td class="ar {vc}">{v:+.1f}%</td>
  <td>{str(h.get('jockey',''))[:12]}</td>
</tr>"""
        blocks += f"""<div class="rblock">
<div class="rhdr" onclick="tog('rb{i}')">
  <div><div class="rleft">{r.get('time','')} · {r.get('track','')}</div>
  <div class="rmeta">{r.get('race','')}</div></div>
  <span class="rb {rc}">RSI {int(rsi)}</span>
</div>
<div class="rbody" id="rb{i}">
  <table class="tbl"><thead><tr>
    <th>Horse</th><th>Win%</th><th>Odds</th><th>Fair</th><th>Val%</th><th>Jockey</th>
  </tr></thead><tbody>{rows}</tbody></table>
</div></div>"""
    return HTMLResponse(_shell("analyzer", f'<div class="content">{blocks}</div>'))
 
# ── Live page ─────────────────────────────────────────────────────────
@app.get("/live", response_class=HTMLResponse)
async def live_page():
    races = _store["live"]
    if not races:
        body = '<div class="content"><p class="empty">No live odds yet — load a date in The Post desktop app</p></div>'
        return HTMLResponse(_shell("live", body))
    blocks = ""
    for i, r in enumerate(races):
        rows = ""
        for h in r.get("runners", []):
            f   = float(h.get("flucs_pct", 0))
            fc  = "dn" if f>5 else ("up" if f<-5 else "")
            arr = "▼" if f>5 else ("▲" if f<-5 else "–")
            top = "top" if h.get("is_top3") else ""
            rows += f"""<tr class="{top}">
  <td>{h.get('horse','')}</td>
  <td class="ar">${h.get('open_odds',0):.2f}</td>
  <td class="ar">${h.get('now_odds',0):.2f}</td>
  <td class="ar">${h.get('place_odds',0):.2f}</td>
  <td class="ar {fc}">{arr} {abs(f):.1f}%</td>
</tr>"""
        blocks += f"""<div class="rblock">
<div class="rhdr" onclick="tog('lr{i}')">
  <div><div class="rleft">{r.get('time','')} · {r.get('track','')}<span class="ltag">LIVE</span></div>
  <div class="rmeta">{r.get('race','')} · {len(r.get('runners',[]))} runners</div></div>
  <span style="color:var(--t2);font-size:18px">›</span>
</div>
<div class="rbody" id="lr{i}">
  <table class="tbl"><thead><tr>
    <th>Horse</th><th>Open</th><th>Now</th><th>Place</th><th>Flucs</th>
  </tr></thead><tbody>{rows}</tbody></table>
</div></div>"""
    return HTMLResponse(_shell("live", f'<div class="content">{blocks}</div>'))
 
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
