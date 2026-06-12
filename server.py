#!/usr/bin/env python3
"""The Post — Live Tips Server  v5 (persistent storage via Upstash Redis)"""

import os, json, datetime, base64
import requests
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

PUSH_API_KEY = os.environ.get("PUSH_API_KEY", "thepost2026")

# --- Upstash Redis REST config ---
UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
STORE_KEY     = "thepost_store"

DEFAULT_STORE = {"tips": [], "analyzer": [], "live": [], "last_push": None, "push_count": 0}

app = FastAPI(title="The Post", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ICON_PATH = os.path.join(os.path.dirname(__file__), "icon.png")

@app.get("/icon.png")
async def serve_icon():
    from fastapi.responses import FileResponse, Response
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

def _pushed_str(store):
    try: return datetime.datetime.fromisoformat(store["last_push"]).strftime("%d %b  %H:%M")
    except: return "Never"

def _shell(page_id, body, store):
    pushed = _pushed_str(store)
    total  = len(store["tips"])
    share_btn = '<button class="sbtn" onclick="openShare()">&#x2197; Share</button>' if page_id=="tips" else ""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
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
body{font-family:-apple-system,'Segoe UI',Arial,sans-serif;background:var(--bg);color:var(--t1);min-height:100vh;padding-bottom:80px;font-size:13px;}
.header{background:var(--panel);border-bottom:1px solid var(--bd);padding:14px 16px 10px;position:sticky;top:0;z-index:50;}
.hrow{display:flex;align-items:center;justify-content:space-between;}
.appname{font-size:18px;font-weight:700;}
.dot{width:7px;height:7px;background:var(--green);border-radius:50%;display:inline-block;margin-right:5px;animation:pulse 2s infinite;}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.status{font-size:11px;color:var(--t2);margin-top:3px;}
.hbtns{display:flex;gap:8px;}
.rbtn{background:var(--el);border:1px solid var(--bd);color:var(--t1);padding:5px 12px;border-radius:7px;font-size:12px;cursor:pointer;}
.sbtn{background:#1a3a1a;border:1px solid #2ECC71;color:#2ECC71;padding:5px 12px;border-radius:7px;font-size:12px;cursor:pointer;}
.navbar{position:fixed;bottom:0;left:0;right:0;background:var(--panel);border-top:1px solid var(--bd);display:flex;z-index:100;padding-bottom:env(safe-area-inset-bottom);}
.nbtn{flex:1;padding:12px 4px 10px;font-size:10px;color:var(--t2);background:none;border:none;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:3px;}
.nbtn.active{color:var(--acc);}
.ni{font-size:20px;line-height:1;}
.tabs{display:flex;background:var(--panel);border-bottom:1px solid var(--bd);overflow-x:auto;scrollbar-width:none;}
.tabs::-webkit-scrollbar{display:none;}
.tab{flex:1;min-width:72px;padding:11px 6px;font-size:12px;font-weight:600;color:var(--t2);background:none;border:none;border-bottom:2px solid transparent;cursor:pointer;white-space:nowrap;}
.tab.active{color:var(--t1);border-bottom-color:var(--acc);}
.section{display:none;}.section.active{display:block;}
.content{padding:12px 14px;}
.sortbar{display:flex;gap:6px;padding:10px 14px 4px;overflow-x:auto;scrollbar-width:none;flex-wrap:nowrap;background:var(--bg);}
.sortbar::-webkit-scrollbar{display:none;}
.sort-btn{background:var(--el);border:1px solid var(--bd);color:var(--t2);padding:5px 11px;border-radius:20px;font-size:11px;font-weight:600;cursor:pointer;white-space:nowrap;flex-shrink:0;}
.sort-btn.active{background:#1A2E4A;border-color:var(--acc);color:var(--acc);}
.card{background:var(--panel);border:1px solid var(--bd);border-radius:10px;padding:13px 14px;margin-bottom:9px;}
.ctop{display:flex;align-items:center;justify-content:space-between;margin-bottom:3px;}
.horse{font-size:15px;font-weight:700;}
.tag{font-size:10px;font-weight:700;padding:3px 8px;border-radius:20px;letter-spacing:.4px;text-transform:uppercase;}
.tag.top-play{background:#1a3a1a;color:var(--green);}
.tag.secondary{background:#1a2a4a;color:var(--acc);}
.tag.watch{background:#2a2210;color:var(--warn);}
.meta{font-size:11px;color:var(--t2);margin-bottom:10px;}
.stats{display:flex;gap:6px;flex-wrap:wrap;}
.stat{background:var(--el);border-radius:6px;padding:7px 8px;flex:1;min-width:48px;text-align:center;}
.sl{display:block;font-size:9px;font-weight:700;color:var(--t2);letter-spacing:.5px;margin-bottom:3px;}
.sv{font-size:14px;font-weight:700;}
.pos{color:var(--green);}.neg{color:var(--red);}
.summary{display:flex;gap:8px;margin-bottom:14px;}
.sc{flex:1;background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:10px;text-align:center;}
.sn{font-size:22px;font-weight:700;}.sl2{font-size:10px;color:var(--t2);text-transform:uppercase;letter-spacing:.4px;}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px;}
.stat-card{background:var(--panel);border:1px solid var(--bd);border-radius:10px;padding:14px;border-left:3px solid var(--acc);}
.stat-card.green{border-left-color:var(--green);}
.stat-card.red{border-left-color:var(--red);}
.stat-card.warn{border-left-color:var(--warn);}
.stat-label{font-size:10px;color:var(--t2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;}
.stat-value{font-size:22px;font-weight:700;}
.stat-sub{font-size:11px;color:var(--t2);margin-top:3px;}
.rblock{background:var(--panel);border:1px solid var(--bd);border-radius:10px;margin-bottom:9px;overflow:hidden;}
.rhdr{display:flex;align-items:center;justify-content:space-between;padding:11px 14px;cursor:pointer;}
.rleft{font-size:13px;font-weight:700;}
.rmeta{font-size:11px;color:var(--t2);margin-top:1px;}
.rbody{display:none;border-top:1px solid var(--bd);}
.rbody.open{display:block;}
.tbl{width:100%;border-collapse:collapse;font-size:11px;}
.tbl th{padding:7px 8px;text-align:left;color:var(--t2);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;background:var(--el);border-bottom:1px solid var(--bd);}
.tbl td{padding:8px 8px;border-bottom:1px solid var(--bd);}
.tbl tr:last-child td{border-bottom:none;}
.tbl .top td:first-child{color:var(--warn);font-weight:700;}
.vp{color:var(--green);font-weight:600;}.vn{color:var(--red);}
.ar{text-align:right;font-variant-numeric:tabular-nums;}
.rb{font-size:11px;font-weight:700;padding:3px 9px;border-radius:20px;}
.re{background:#1a3a1a;color:var(--green);}
.rs{background:#1a2a4a;color:var(--acc);}
.rm{background:#2a2210;color:var(--warn);}
.rl{background:#2a1a1a;color:var(--red);}
.empty{color:var(--t2);text-align:center;padding:40px 0;font-size:13px;}
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;align-items:flex-end;justify-content:center;}
.modal-bg.open{display:flex;}
.modal{background:var(--panel);border-radius:16px 16px 0 0;padding:20px 16px 32px;width:100%;max-width:480px;}
.modal-title{font-size:16px;font-weight:700;margin-bottom:16px;text-align:center;}
.share-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
.share-btn{background:var(--el);border:1px solid var(--bd);border-radius:10px;padding:14px 10px;text-align:center;cursor:pointer;color:var(--t1);font-size:12px;font-weight:600;text-decoration:none;display:block;}
.share-icon{font-size:24px;display:block;margin-bottom:6px;}
.modal-close{width:100%;margin-top:14px;padding:12px;background:var(--el);border:1px solid var(--bd);border-radius:10px;color:var(--t2);font-size:14px;cursor:pointer;}
</style>
</head>
<body>
<div class="header">
  <div class="hrow">
    <div class="appname"><span class="dot"></span>The Post</div>
    <div class="hbtns">""" + share_btn + """
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
<nav class="navbar">
  <button class="nbtn """ + ("active" if page_id=="dash" else "") + """ " onclick="location.href='/dash'"><span class="ni">&#x1F4CA;</span>Dashboard</button>
  <button class="nbtn """ + ("active" if page_id=="tips" else "") + """ " onclick="location.href='/'"><span class="ni">&#x1F3AF;</span>Tips</button>
  <button class="nbtn """ + ("active" if page_id=="analyzer" else "") + """ " onclick="location.href='/analyzer'"><span class="ni">&#x1F50D;</span>Analyzer</button>
</nav>
<script>
var _sortKey='time',_sortDir=1,_activeContainer='cards-container';
function tog(id){document.getElementById(id).classList.toggle('open');}
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
setTimeout(function(){location.reload();},90000);
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
            f'<div class="ctop"><span class="horse">{t.get("horse","")}</span>{tag_html}</div>'
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

@app.get("/", response_class=HTMLResponse)
async def tips_page():
    store = _load()
    tips  = store["tips"]
    back  = [t for t in tips if t["type"]=="BACK"]
    degen = [t for t in tips if t["type"]=="DEGEN"]
    lay   = [t for t in tips if t["type"]=="LAY"]
    place = [t for t in tips if t["type"]=="PLACE"]
    sort_bar = (
        '<div class="sortbar">'
        '<button class="sort-btn active" onclick="sortBy(\'time\',this)">&#x1F550; Time</button>'
        '<button class="sort-btn" onclick="sortBy(\'units\',this)">Units</button>'
        '<button class="sort-btn" onclick="sortBy(\'track\',this)">Track</button>'
        '<button class="sort-btn" onclick="sortBy(\'win_pct\',this)">Win%</button>'
        '<button class="sort-btn" onclick="sortBy(\'rsi\',this)">RSI</button>'
        '<button class="sort-btn" onclick="sortBy(\'real_odds\',this)">Odds</button>'
        '</div>'
    )
    body = (
        '<div class="tabs">'
        f'<button class="tab active" data-tab="tips" onclick="switchTab(\'tb\',this,\'tips\');setContainer(\'tb\')">Back ({len(back)})</button>'
        f'<button class="tab" data-tab="tips" onclick="switchTab(\'td\',this,\'tips\');setContainer(\'td\')">Degen ({len(degen)})</button>'
        f'<button class="tab" data-tab="tips" onclick="switchTab(\'tl\',this,\'tips\');setContainer(\'tl\')">Lay ({len(lay)})</button>'
        f'<button class="tab" data-tab="tips" onclick="switchTab(\'tp\',this,\'tips\');setContainer(\'tp\')">Place ({len(place)})</button>'
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
    )
    return HTMLResponse(_shell("tips", body, store))

@app.get("/dash", response_class=HTMLResponse)
async def dash_page():
    store    = _load()
    tips     = store["tips"]
    analyzer = store["analyzer"]
    back  = [t for t in tips if t["type"]=="BACK"]
    degen = [t for t in tips if t["type"]=="DEGEN"]
    lay   = [t for t in tips if t["type"]=="LAY"]
    place = [t for t in tips if t["type"]=="PLACE"]
    all_t = back+degen+lay+place
    total_u  = sum(t.get("units",0) for t in all_t)
    avg_odds = (sum(t.get("real_odds",0) for t in all_t)/len(all_t)) if all_t else 0
    avg_rsi  = (sum(t.get("rsi",0) for t in all_t)/len(all_t)) if all_t else 0
    avg_val  = (sum(t.get("value_pct",0) for t in all_t)/len(all_t)) if all_t else 0
    avg_win  = (sum(t.get("win_pct",0) for t in all_t)/len(all_t)) if all_t else 0
    pos_val  = len([t for t in all_t if t.get("value_pct",0)>0])
    neg_val  = len([t for t in all_t if t.get("value_pct",0)<0])
    top_p    = len([t for t in all_t if t.get("tag")=="TOP PLAY"])
    sec_p    = len([t for t in all_t if t.get("tag")=="SECONDARY"])
    tracks   = ", ".join(sorted({t.get("track","") for t in all_t if t.get("track")})) or "—"
    hi_rsi   = len([r for r in analyzer if float(r.get("rsi",0))>=80])
    med_rsi  = len([r for r in analyzer if 70<=float(r.get("rsi",0))<80])
    t_races  = len(analyzer)
    t_run    = sum(len(r.get("horses",[])) for r in analyzer)
    vc = "var(--green)" if avg_val>0 else "var(--red)"
    pushed   = _pushed_str(store)
    body = (
        '<div class="content">'
        '<div class="summary" style="margin-bottom:10px;">'
        f'<div class="sc"><div class="sn" style="color:var(--green)">{len(back)}</div><div class="sl2">Back</div></div>'
        f'<div class="sc"><div class="sn" style="color:var(--warn)">{len(degen)}</div><div class="sl2">Degen</div></div>'
        f'<div class="sc"><div class="sn" style="color:var(--red)">{len(lay)}</div><div class="sl2">Lay</div></div>'
        f'<div class="sc"><div class="sn" style="color:var(--acc)">{len(place)}</div><div class="sl2">Place</div></div>'
        '</div>'
        '<div class="stat-grid">'
        f'<div class="stat-card green"><div class="stat-label">Total Units</div><div class="stat-value">{total_u:.0f}u</div><div class="stat-sub">{len(all_t)} selections</div></div>'
        f'<div class="stat-card" style="border-left-color:{vc}"><div class="stat-label">Avg Value</div><div class="stat-value" style="color:{vc}">{avg_val:+.1f}%</div><div class="stat-sub">{pos_val} pos &middot; {neg_val} neg</div></div>'
        f'<div class="stat-card warn"><div class="stat-label">Avg Odds</div><div class="stat-value">${avg_odds:.2f}</div><div class="stat-sub">Avg win {avg_win:.1f}%</div></div>'
        f'<div class="stat-card"><div class="stat-label">Avg RSI</div><div class="stat-value">{avg_rsi:.0f}</div><div class="stat-sub">{top_p} TOP &middot; {sec_p} SEC</div></div>'
        '</div>'
        '<div class="stat-grid">'
        f'<div class="stat-card green"><div class="stat-label">Races Loaded</div><div class="stat-value">{t_races}</div><div class="stat-sub">{t_run} runners</div></div>'
        f'<div class="stat-card"><div class="stat-label">High RSI</div><div class="stat-value">{hi_rsi}</div><div class="stat-sub">{med_rsi} medium RSI</div></div>'
        '</div>'
        f'<div class="card" style="margin-bottom:9px;"><div class="stat-label" style="margin-bottom:8px;">Tracks Today</div><div style="font-size:13px;line-height:1.6;">{tracks}</div></div>'
        f'<div class="card"><div class="stat-label" style="margin-bottom:8px;">Last Push</div><div style="font-size:14px;font-weight:600;">{pushed}</div><div style="font-size:11px;color:var(--t2);margin-top:3px;">Push #{store["push_count"]}</div></div>'
        '</div>'
    )
    return HTMLResponse(_shell("dash", body, store))

@app.get("/analyzer", response_class=HTMLResponse)
async def analyzer_page():
    store = _load()
    races = store["analyzer"]
    if not races:
        return HTMLResponse(_shell("analyzer",'<div class="content"><p class="empty">No analyzer data yet</p></div>', store))
    blocks = ""
    for i, r in enumerate(races):
        rsi = float(r.get("rsi",0))
        rc  = "re" if rsi>=80 else ("rs" if rsi>=70 else ("rm" if rsi>=60 else "rl"))
        rows = ""
        for j, h in enumerate(r.get("horses",[])):
            v  = float(h.get("value_pct",0))
            vc = "vp" if v>0 else ("vn" if v<0 else "")
            top= "top" if j<3 else ""
            rows += (
                f'<tr class="{top}">'
                f'<td>{h.get("horse","")}</td>'
                f'<td class="ar">{h.get("win_pct",0):.1f}%</td>'
                f'<td class="ar">${h.get("real_odds",0):.2f}</td>'
                f'<td class="ar">${h.get("fair_odds",0):.2f}</td>'
                f'<td class="ar {vc}">{v:+.1f}%</td>'
                f'<td>{str(h.get("jockey",""))[:12]}</td>'
                '</tr>'
            )
        blocks += (
            f'<div class="rblock" data-time="{r.get("time","")}" data-track="{r.get("track","")}">'
            f'<div class="rhdr" onclick="tog(\'rb{i}\')">'
            f'<div><div class="rleft">{r.get("time","")} &middot; {r.get("track","")}</div>'
            f'<div class="rmeta">{r.get("race","")}</div></div>'
            f'<span class="rb {rc}">RSI {int(rsi)}</span>'
            '</div>'
            f'<div class="rbody" id="rb{i}">'
            '<table class="tbl"><thead><tr><th>Horse</th><th>Win%</th><th>Odds</th><th>Fair</th><th>Val%</th><th>Jockey</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></div>'
        )
    sort_bar = (
        '<div class="sortbar">'
        '<button class="asort-btn sort-btn active" onclick="sortAnalyzer(\'time\',this)">&#x1F550; Time</button>'
        '<button class="asort-btn sort-btn" onclick="sortAnalyzer(\'track\',this)">Track A-Z</button>'
        '</div>'
    )
    return HTMLResponse(_shell("analyzer", sort_bar + f'<div class="content"><div id="races-container">{blocks}</div></div>', store))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT",8000)))
