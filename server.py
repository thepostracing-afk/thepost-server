#!/usr/bin/env python3
"""
The Post — Live Tips Server
============================
Deployed on Render (free tier).  The Post desktop app pushes tips here
after every reload/recalc.  Open the web UI on your phone to view them.

Endpoints:
  POST /push          — desktop app pushes a batch of tips (requires API key)
  GET  /              — mobile-friendly HTML dashboard
  GET  /api/tips      — raw JSON (for debugging)
  GET  /api/status    — last push time + counts
"""

import os
import json
import datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────
# Set PUSH_API_KEY in Render environment variables.
# The desktop app must send this in the X-API-Key header.
PUSH_API_KEY = os.environ.get("PUSH_API_KEY", "thepost2026")

app = FastAPI(title="The Post — Tips Server", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory store (persists until server restarts) ──────────────────
_store = {
    "tips":        [],   # list of tip dicts
    "last_push":   None, # ISO datetime string
    "push_count":  0,
}


# ── Models ────────────────────────────────────────────────────────────
class Tip(BaseModel):
    type:      str            # "BACK" | "LAY" | "DEGEN"
    horse:     str
    race:      str
    time:      str            # e.g. "13:25"
    track:     str
    units:     float
    real_odds: float
    fair_odds: float
    win_pct:   float          # e.g. 38.2
    value_pct: float          # e.g. 24.1
    rsi:       float          # displayed RSI (internal - 10)
    tag:       Optional[str] = ""   # "TOP PLAY" | "SECONDARY" | "WATCH" | ""

class PushPayload(BaseModel):
    tips: List[Tip]
    generated_at: Optional[str] = ""


# ── Routes ────────────────────────────────────────────────────────────

@app.post("/push")
async def push_tips(payload: PushPayload, x_api_key: str = Header(default="")):
    if x_api_key != PUSH_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    _store["tips"]       = [t.dict() for t in payload.tips]
    _store["last_push"]  = payload.generated_at or datetime.datetime.now().isoformat()
    _store["push_count"] += 1
    return {"status": "ok", "count": len(payload.tips)}


@app.get("/api/tips")
async def api_tips():
    return JSONResponse(_store["tips"])


@app.get("/api/status")
async def api_status():
    return {
        "last_push":  _store["last_push"],
        "tip_count":  len(_store["tips"]),
        "push_count": _store["push_count"],
    }


@app.get("/", response_class=HTMLResponse)
async def mobile_ui():
    tips   = _store["tips"]
    pushed = _store["last_push"] or "Never"

    # Shorten ISO string for display
    try:
        dt = datetime.datetime.fromisoformat(pushed)
        pushed_str = dt.strftime("%d %b %Y  %H:%M:%S")
    except Exception:
        pushed_str = pushed

    # Group tips by type
    back  = [t for t in tips if t["type"] == "BACK"]
    lay   = [t for t in tips if t["type"] == "LAY"]
    degen = [t for t in tips if t["type"] == "DEGEN"]

    def tip_cards(tip_list, accent):
        if not tip_list:
            return f'<p class="empty">No {accent} picks right now</p>'
        html = ""
        for t in tip_list:
            tag_html = ""
            if t.get("tag"):
                tag_cls = t["tag"].lower().replace(" ", "-")
                tag_html = f'<span class="tag {tag_cls}">{t["tag"]}</span>'
            rsi_disp = int(t["rsi"]) if t["rsi"] else "—"
            html += f"""
            <div class="card">
              <div class="card-top">
                <span class="horse">{t['horse']}</span>
                {tag_html}
              </div>
              <div class="card-meta">
                {t['time']}  ·  {t['track']}  ·  {t['race']}
              </div>
              <div class="card-stats">
                <div class="stat">
                  <span class="stat-label">ODDS</span>
                  <span class="stat-val">${t['real_odds']:.2f}</span>
                </div>
                <div class="stat">
                  <span class="stat-label">UNITS</span>
                  <span class="stat-val">{int(t['units'])}u</span>
                </div>
                <div class="stat">
                  <span class="stat-label">VALUE</span>
                  <span class="stat-val {'pos' if t['value_pct'] > 0 else 'neg'}">{t['value_pct']:+.1f}%</span>
                </div>
                <div class="stat">
                  <span class="stat-label">RSI</span>
                  <span class="stat-val">{rsi_disp}</span>
                </div>
                <div class="stat">
                  <span class="stat-label">WIN%</span>
                  <span class="stat-val">{t['win_pct']:.1f}%</span>
                </div>
              </div>
            </div>"""
        return html

    back_html  = tip_cards(back,  "back")
    lay_html   = tip_cards(lay,   "lay")
    degen_html = tip_cards(degen, "degen")

    total = len(tips)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="theme-color" content="#0B0F14">
  <title>The Post — Live Tips</title>
  <style>
    :root {{
      --bg:       #0B0F14;
      --panel:    #121821;
      --elevated: #1A222D;
      --border:   #232C38;
      --text1:    #E6EDF3;
      --text2:    #8B98A5;
      --green:    #2ECC71;
      --red:      #E74C3C;
      --accent:   #3A82F7;
      --warn:     #F0A500;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, 'SF Pro Display', 'Segoe UI', Arial, sans-serif;
      background: var(--bg);
      color: var(--text1);
      min-height: 100vh;
      padding-bottom: env(safe-area-inset-bottom);
    }}

    /* ── Header ── */
    .header {{
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      padding: 16px 20px 12px;
      position: sticky;
      top: 0;
      z-index: 10;
    }}
    .header-top {{
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .app-name {{
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.3px;
    }}
    .live-dot {{
      width: 8px; height: 8px;
      background: var(--green);
      border-radius: 50%;
      display: inline-block;
      margin-right: 6px;
      animation: pulse 2s infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50%       {{ opacity: 0.4; }}
    }}
    .status-line {{
      font-size: 11px;
      color: var(--text2);
      margin-top: 4px;
    }}
    .refresh-btn {{
      background: var(--elevated);
      border: 1px solid var(--border);
      color: var(--text1);
      padding: 6px 14px;
      border-radius: 8px;
      font-size: 13px;
      cursor: pointer;
      -webkit-tap-highlight-color: transparent;
    }}

    /* ── Tabs ── */
    .tabs {{
      display: flex;
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
    }}
    .tabs::-webkit-scrollbar {{ display: none; }}
    .tab-btn {{
      flex: 1;
      min-width: 80px;
      padding: 12px 8px;
      font-size: 13px;
      font-weight: 600;
      color: var(--text2);
      background: transparent;
      border: none;
      border-bottom: 2px solid transparent;
      cursor: pointer;
      white-space: nowrap;
      -webkit-tap-highlight-color: transparent;
      transition: color 0.15s, border-color 0.15s;
    }}
    .tab-btn.active {{
      color: var(--text1);
      border-bottom-color: var(--accent);
    }}

    /* ── Content ── */
    .content {{ padding: 16px; }}
    .section {{ display: none; }}
    .section.active {{ display: block; }}

    /* ── Cards ── */
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px 16px;
      margin-bottom: 10px;
    }}
    .card-top {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 4px;
    }}
    .horse {{
      font-size: 16px;
      font-weight: 700;
    }}
    .tag {{
      font-size: 10px;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 20px;
      letter-spacing: 0.5px;
      text-transform: uppercase;
    }}
    .tag.top-play  {{ background: #1a3a1a; color: var(--green); }}
    .tag.secondary {{ background: #1a2a4a; color: var(--accent); }}
    .tag.watch     {{ background: #2a2210; color: var(--warn); }}

    .card-meta {{
      font-size: 12px;
      color: var(--text2);
      margin-bottom: 12px;
    }}
    .card-stats {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .stat {{
      background: var(--elevated);
      border-radius: 6px;
      padding: 8px 10px;
      flex: 1;
      min-width: 52px;
      text-align: center;
    }}
    .stat-label {{
      display: block;
      font-size: 9px;
      font-weight: 700;
      color: var(--text2);
      letter-spacing: 0.6px;
      margin-bottom: 4px;
    }}
    .stat-val {{
      font-size: 15px;
      font-weight: 700;
    }}
    .stat-val.pos {{ color: var(--green); }}
    .stat-val.neg {{ color: var(--red); }}

    .empty {{
      color: var(--text2);
      font-size: 14px;
      text-align: center;
      padding: 40px 0;
    }}

    /* ── Summary bar ── */
    .summary {{
      display: flex;
      gap: 8px;
      margin-bottom: 16px;
    }}
    .summary-card {{
      flex: 1;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px;
      text-align: center;
    }}
    .summary-num {{
      font-size: 22px;
      font-weight: 700;
    }}
    .summary-lbl {{
      font-size: 10px;
      color: var(--text2);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
  </style>
</head>
<body>

<div class="header">
  <div class="header-top">
    <div class="app-name"><span class="live-dot"></span>The Post</div>
    <button class="refresh-btn" onclick="location.reload()">↻ Refresh</button>
  </div>
  <div class="status-line">Last push: {pushed_str}  ·  {total} tip{"s" if total != 1 else ""} loaded</div>
</div>

<div class="tabs">
  <button class="tab-btn active" onclick="showTab('back',this)">Back ({len(back)})</button>
  <button class="tab-btn"       onclick="showTab('degen',this)">Degen ({len(degen)})</button>
  <button class="tab-btn"       onclick="showTab('lay',this)">Lay ({len(lay)})</button>
</div>

<div class="content">

  <div class="section active" id="tab-back">
    <div class="summary">
      <div class="summary-card">
        <div class="summary-num" style="color:var(--green)">{len(back)}</div>
        <div class="summary-lbl">Back Bets</div>
      </div>
      <div class="summary-card">
        <div class="summary-num" style="color:var(--warn)">{len(degen)}</div>
        <div class="summary-lbl">Degen</div>
      </div>
      <div class="summary-card">
        <div class="summary-num" style="color:var(--red)">{len(lay)}</div>
        <div class="summary-lbl">Lay</div>
      </div>
    </div>
    {back_html}
  </div>

  <div class="section" id="tab-degen">
    {degen_html}
  </div>

  <div class="section" id="tab-lay">
    {lay_html}
  </div>

</div>

<script>
  function showTab(name, btn) {{
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('tab-' + name).classList.add('active');
    btn.classList.add('active');
  }}
  // Auto-refresh every 60 seconds
  setTimeout(() => location.reload(), 60000);
</script>

</body>
</html>"""
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
