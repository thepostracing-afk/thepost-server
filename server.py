#!/usr/bin/env python3
"""
Patches The Post's server.py (v7) into v8:
  - Tips are HELD: the server only replaces its tips when the desktop app says
    tips_commit = true (a Discord send, or "Push tips to server now").
  - New Discord page (/discord, /portal/discord, /api/discord) showing the tips
    exactly as they were posted to Discord (price, units, day/time, P&L status).
  - Header shows when the tips were last sent. Jump-time notifications skip tips
    for another day.
Usage:  python patch_server_v8.py [path/to/server.py]
Makes server_v7_backup.py next to it, then rewrites server.py. Nothing is written
if any anchor is missing.
"""
import os, sys, shutil

path = sys.argv[1] if len(sys.argv) > 1 else "server.py"
raw = open(path, "rb").read().decode("utf-8")
crlf = "\r\n" in raw
s = raw.replace("\r\n", "\n")

def rep(old, new, label):
    global s
    n = s.count(old)
    if n != 1:
        print(f"STOPPED: could not patch '{label}' (found {n} matches, expected 1). Nothing was written.")
        sys.exit(1)
    s = s.replace(old, new)

if "def _discord_body(" in s:
    print("This server.py already has the Discord page (v8). Nothing to do.")
    sys.exit(0)

rep('"""The Post — Live Tips Server  v7 (persistent storage via Upstash Redis, cached silk blobs)"""',
    '"""The Post — Live Tips Server  v8 (held tips + Discord page, persistent storage via Upstash Redis, cached silk blobs)"""',
    "docstring")

rep('DEFAULT_STORE = {"tips": [], "analyzer": [], "live": [], "last_push": None, "push_count": 0, "push_subs": {}, "pnl": None}',
    'DEFAULT_STORE = {"tips": [], "analyzer": [], "live": [], "last_push": None, "push_count": 0, "push_subs": {}, "pnl": None,\n'
    '                 "discord_tips": [], "discord_updated": None, "tips_updated": None}',
    "DEFAULT_STORE")

rep('_store.setdefault("push_subs", {})  # older persisted stores may predate this key',
    '_store.setdefault("push_subs", {})  # older persisted stores may predate this key\n'
    '_store.setdefault("discord_tips", [])      # v8\n'
    '_store.setdefault("discord_updated", None)\n'
    '_store.setdefault("tips_updated", None)',
    "store defaults")

# ---- /push: hold tips unless the desktop commits them
rep('        new_pnl      = body.get("pnl")  # today\'s P&L from the desktop app (None if not sent)',
    '        new_pnl      = body.get("pnl")  # today\'s P&L from the desktop app (None if not sent)\n'
    '        # v8: tips only change when the desktop app commits them (a Discord send or\n'
    '        # "Push tips to server now"). Builds that don\'t send the flag always commit.\n'
    '        tips_commit  = bool(body.get("tips_commit", True))\n'
    '        new_discord  = body.get("discord_tips")   # None = not sent, list = the whole Discord board',
    "push: read flags")

rep('''        is_suspicious_empty = (
            not new_tips and not new_analyzer
            and (_store.get("tips") or _store.get("analyzer"))
        )''',
    '''        is_suspicious_empty = (
            not new_analyzer and (not tips_commit or not new_tips)
            and (_store.get("tips") or _store.get("analyzer"))
        )''', "push: empty guard")

rep('''            if isinstance(new_pnl, dict) and new_pnl != _store.get("pnl"):
                _store["pnl"] = new_pnl
                _store["push_count"] += 1
                await run_in_threadpool(_save, _store)''',
    '''            changed = False
            if isinstance(new_pnl, dict) and new_pnl != _store.get("pnl"):
                _store["pnl"] = new_pnl
                changed = True
            if isinstance(new_discord, list) and new_discord != _store.get("discord_tips"):
                _store["discord_tips"] = new_discord
                _store["discord_updated"] = datetime.datetime.now().isoformat()
                changed = True
            if changed:
                _store["push_count"] += 1
                await run_in_threadpool(_save, _store)''', "push: suspicious branch")

rep('''        _store["tips"]        = new_tips
        _store["analyzer"]    = new_analyzer''',
    '''        if tips_commit:
            _store["tips"]         = new_tips
            _store["tips_updated"] = body.get("generated_at") or datetime.datetime.now().isoformat()
        if isinstance(new_discord, list):
            if new_discord != _store.get("discord_tips"):
                _store["discord_updated"] = datetime.datetime.now().isoformat()
            _store["discord_tips"] = new_discord
        _store["analyzer"]    = new_analyzer''', "push: store tips")

rep('    return {"status":"ok","tips":len(_store["tips"]),"analyzer_races":len(_store["analyzer"]),"live_races":len(_store["live"])}',
    '    return {"status":"ok","tips":len(_store["tips"]),"analyzer_races":len(_store["analyzer"]),"live_races":len(_store["live"]),\n'
    '            "tips_committed":tips_commit,"discord_tips":len(_store.get("discord_tips") or [])}',
    "push: response")

# ---- notifications: only tips for today
rep('''        if ttype not in NOTIFY_TYPES:
            continue
        jump = _jump_dt_today(t.get("time", ""), now)''',
    '''        if ttype not in NOTIFY_TYPES:
            continue
        rd = t.get("race_date")
        if rd and rd != now.date().isoformat():
            continue   # v8: a tip moved to another day must not alert today
        jump = _jump_dt_today(t.get("time", ""), now)''', "notify: today only")

# ---- header: when the tips were last sent
rep('def _race_id(r):',
    '''def _tips_sent_str(store):
    """When the tips were last sent (tips are held between Discord sends)."""
    try: return datetime.datetime.fromisoformat(store.get("tips_updated") or store["last_push"]).strftime("%d %b  %H:%M")
    except: return "Never"

def _race_id(r):''', "tips sent helper")
rep('Last push: """ + pushed + """', 'Tips sent: """ + _tips_sent_str(store) + """', "header text")
rep('    total  = len(store["tips"])',
    '    total  = len(store.get("discord_tips") or []) if page_id == "discord" else len(store["tips"])',
    "header count")

# ---- nav buttons
rep('''onclick="location.href='/tips'"><span class="ni">&#x1F3C7;</span>Tips</button>''',
    '''onclick="location.href='/tips'"><span class="ni">&#x1F3C7;</span>Tips</button>
  <button class="nbtn """ + ("active" if page_id=="discord" else "") + """ " onclick="location.href='/discord'"><span class="ni">&#x1F4E8;</span>Discord</button>''',
    "owner nav")
rep('''onclick="location.href='/portal/tips'"><span class="ni">&#x1F3C7;</span>Tips</button>''',
    '''onclick="location.href='/portal/tips'"><span class="ni">&#x1F3C7;</span>Tips</button>
  <button class="nbtn """ + ("active" if page_id=="discord" else "") + """ " onclick="location.href='/portal/discord'"><span class="ni">&#x1F4E8;</span>Discord</button>''',
    "friend nav")

# ---- the Discord page itself
NEW_PAGE = r'''# ---------------------------------------------------------------------------
# Discord tips (v8) — exactly what the desktop app posted to Discord: the price,
# units, race day / time as sent, plus whether it has been saved to the P&L.
# The desktop app owns the board and sends the whole list on every push, so this
# page just shows it. Tips stay until the next Discord send.
# ---------------------------------------------------------------------------
import html as _html

_DC_CSS = """<style>
.dc-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:7px;font-size:10px;color:var(--t2);}
.dc-ch{background:#1A2E4A;color:var(--acc);border-radius:20px;padding:2px 8px;font-weight:700;font-size:9px;letter-spacing:.3px;white-space:nowrap;}
.dc-pnl{font-weight:700;white-space:nowrap;}
.dc-note{font-size:10.5px;color:var(--t2);padding:2px 2px 10px;line-height:1.5;}
</style>"""

def _dc_sent_str(iso):
    try: return datetime.datetime.fromisoformat(iso).strftime("%a %I:%M %p").replace(" 0", " ")
    except Exception: return ""

def _dc_day_prefix(t):
    try:
        d = datetime.date.fromisoformat(t.get("race_date", ""))
        if d != datetime.datetime.now(NOTIFY_TZ).date():
            return d.strftime("%a %d %b").upper() + " &middot; "
    except Exception:
        pass
    return ""

def _dc_foot(t):
    res = str(t.get("result", "") or "").upper()
    if res:
        try: p = float(t.get("pnl_units") or 0)
        except (TypeError, ValueError): p = 0.0
        col = "var(--green)" if res == "WIN" else "var(--red)"
        right = f'<span class="dc-pnl" style="color:{col};">{res} {p:+.2f}u</span>'
    else:
        right = '<span>Not in P&amp;L yet</span>'
    ch = _html.escape(str(t.get("channel", "") or "Discord"))
    return (f'<div class="dc-foot"><span><span class="dc-ch">{ch}</span>&nbsp; Sent {_dc_sent_str(t.get("sent_at", ""))}</span>'
            f'{right}</div>')

def _dc_f(v, default=0.0):
    try: return float(v)
    except (TypeError, ValueError): return default

def _dc_single_card(t):
    esc = _html.escape
    typ = t.get("type", "BACK")
    number = _get_horse_number(t)
    horse = esc(f'{number}. {t.get("horse","")}' if number != "" else str(t.get("horse", "")))
    tag_cls = "top-play" if typ == "BACK" else "secondary"
    vp = _dc_f(t.get("value_pct"))
    vc = "pos" if vp > 0 else "neg"
    odds_label = "PLACE ODDS" if typ == "PLACE" else "ODDS"
    return (
        '<div class="card">'
        f'<div class="ctop"><span class="horse-row">{_silk_html(_get_silk_url(t))}<span class="horse">{horse}</span></span>'
        f'<span class="tag {tag_cls}">{esc(typ)}</span></div>'
        f'<div class="meta">{_dc_day_prefix(t)}{esc(str(t.get("time","")))} &middot; {esc(str(t.get("track","")))} &middot; {esc(str(t.get("race","")))}</div>'
        '<div class="stats">'
        f'<div class="stat"><span class="sl">{odds_label}</span><span class="sv">${_dc_f(t.get("real_odds")):.2f}</span></div>'
        f'<div class="stat"><span class="sl">UNITS</span><span class="sv">{_dc_f(t.get("units"), 1):g}u</span></div>'
        f'<div class="stat"><span class="sl">VALUE</span><span class="sv {vc}">{vp:+.1f}%</span></div>'
        f'<div class="stat"><span class="sl">RSI</span><span class="sv">{int(_dc_f(t.get("rsi")))}</span></div>'
        f'<div class="stat"><span class="sl">WIN%</span><span class="sv">{_dc_f(t.get("win_pct")):.1f}%</span></div>'
        '</div>'
        f'{_dc_foot(t)}</div>'
    )

def _dc_multi_card(t):
    legs = t.get("legs", []) or []
    tracks = ", ".join(dict.fromkeys(l.get("track", "") for l in legs if l.get("track")))
    title = f'{len(legs)}-Leg Multi' if legs else "Multi"
    return (
        '<div class="card multi-card">'
        f'<div class="ctop"><span class="horse-row"><span class="horse">{title}</span></span><span class="tag watch">MULTI</span></div>'
        f'<div class="meta">{_dc_day_prefix(t)}{_html.escape(tracks)}</div>'
        f'<div class="multi-legs">{"".join(_multi_leg_html(l) for l in legs)}</div>'
        '<div class="multi-foot">'
        f'<div class="mf-item"><span class="sl">COMBINED ODDS</span><span class="sv">${_dc_f(t.get("combined_odds")):.2f}</span></div>'
        f'<div class="mf-item"><span class="sl">UNITS</span><span class="sv">{_dc_f(t.get("units"), 1):g}u</span></div>'
        '</div>'
        f'{_dc_foot(t)}</div>'
    )

def _discord_body(store):
    tips  = store.get("discord_tips") or []
    back  = [t for t in tips if t.get("type") == "BACK"]
    place = [t for t in tips if t.get("type") == "PLACE"]
    multi = [t for t in tips if t.get("type") == "MULTI"]
    if not tips:
        return (_DC_CSS + '<div class="content"><p class="empty">Nothing sent to Discord yet.<br><br>'
                'Tips you post from the app show up here exactly as posted (price, units, time) '
                'and stay until you post new ones.</p></div>')
    def section(items, label, sid, active):
        inner = "".join(_dc_multi_card(t) if t.get("type") == "MULTI" else _dc_single_card(t) for t in items) \
                or f'<p class="empty">No {label} tips sent</p>'
        return f'<div class="section{" active" if active else ""}" id="{sid}" data-grp="dtips">{inner}</div>'
    try:
        upd = datetime.datetime.fromisoformat(store.get("discord_updated")).strftime("%a %d %b %H:%M")
    except Exception:
        upd = ""
    return (
        _DC_CSS +
        '<div class="tabs">'
        f'<button id="btn-db" class="tab active" data-tab="dtips" onclick="switchTab(\'db\',this,\'dtips\')">Back ({len(back)})</button>'
        f'<button id="btn-dp" class="tab" data-tab="dtips" onclick="switchTab(\'dp\',this,\'dtips\')">Place ({len(place)})</button>'
        f'<button id="btn-dm" class="tab" data-tab="dtips" onclick="switchTab(\'dm\',this,\'dtips\')">Multi ({len(multi)})</button>'
        '</div>'
        '<div class="content">'
        f'<div class="dc-note">Exactly as posted to Discord{(" &middot; updated " + upd) if upd else ""}. These stay until new tips are posted.</div>'
        + section(back, "back", "db", True) + section(place, "place", "dp", False) + section(multi, "multi", "dm", False) +
        '</div>'
        '<script>(function(){var p=new URLSearchParams(window.location.search).get("tab");'
        'if(p){var b=document.getElementById("btn-"+p);if(b)b.click();}})();</script>'
    )

@app.get("/discord", response_class=HTMLResponse)
async def discord_page():
    await _refresh_store()
    return HTMLResponse(_cached_page("discord:" + _today_str(), lambda: _shell("discord", _discord_body(_store), _store)))

@app.get("/portal/discord", response_class=HTMLResponse)
async def portal_discord_page():
    await _refresh_store()
    return HTMLResponse(_cached_page("portal_discord:" + _today_str(), lambda: _shell("discord", _discord_body(_store), _store, friend=True)))

@app.get("/api/discord")
async def api_discord():
    await _refresh_store()
    return JSONResponse({"updated": _store.get("discord_updated"), "tips": _store.get("discord_tips") or []})

'''
rep('@app.get("/", response_class=HTMLResponse)\nasync def home_redirect():',
    NEW_PAGE + '@app.get("/", response_class=HTMLResponse)\nasync def home_redirect():', "discord page")

# ---- write (backup first)
backup = os.path.join(os.path.dirname(os.path.abspath(path)), "server_v7_backup.py")
if not os.path.exists(backup):
    shutil.copyfile(path, backup)
out = s.replace("\n", "\r\n") if crlf else s
open(path, "wb").write(out.encode("utf-8"))
print(f"Patched {path} to v8. Backup of the old file: {backup}")
