#!/usr/bin/env python3
"""
Patches The Post's server.py (v7 or v8) into v9: notifications stay on.

What was killing subscriptions:
  1. Subscriptions lived inside the big tips blob. Any /push from a process that
     had an older or empty copy (restart while Upstash was slow, an overlapping
     Render deploy, a failed read) wrote a blob WITHOUT them and wiped every device.
  2. subscribe never bumped push_count, so other processes never picked new
     subscriptions up.
  3. The send loop deleted a subscription the moment any error text contained
     "404" or "410".
  4. Nothing re-registered a device if the browser rotated or dropped its
     subscription, and the Settings toggle showed OFF whenever the server had
     lost the entry, even though the phone was still subscribed.

What v9 does:
  - Subscriptions live in their own Upstash hash (one field per device), so
    /push can never overwrite them. Existing subscriptions are migrated.
  - The server NEVER deletes a subscription because a send failed. A 404/410
    only marks it "gone"; the device replaces it itself next time the app opens.
    Subscriptions are only removed when the user turns notifications off.
  - Every page now loads /push-heal.js: if notification permission is granted it
    re-registers a lost subscription (keeping the saved settings) on its own.
  - sw.js handles pushsubscriptionchange and updates itself immediately.
  - Settings page no longer shows OFF for a device that is still subscribed.

Safe to run again. Usage:  python patch_server_v9.py [path/to/server.py]
Makes server_v8_backup.py next to it, then rewrites server.py. Nothing is
written if any anchor is missing.
"""
import os, sys, shutil

path = sys.argv[1] if len(sys.argv) > 1 else "server.py"
raw = open(path, "rb").read().decode("utf-8")
crlf = "\r\n" in raw
s = raw.replace("\r\n", "\n")

if "def _subs_boot(" in s:
    print("This server.py already has v9. Nothing to do.")
    sys.exit(0)

def fail(label, n):
    print(f"STOPPED: could not patch '{label}' (found {n} matches, expected 1). Nothing was written.")
    sys.exit(1)

def rep(old, new, label):
    global s
    n = s.count(old)
    if n != 1:
        fail(label, n)
    s = s.replace(old, new)

def swap_between(start_marker, end_marker, new_block, label):
    """Replace everything from start_marker up to (not including) end_marker."""
    global s
    a, b = s.count(start_marker), s.count(end_marker)
    if a != 1: fail(label + " (start)", a)
    if b != 1: fail(label + " (end)", b)
    i, j = s.index(start_marker), s.index(end_marker)
    if j < i:
        print(f"STOPPED: '{label}' markers are in the wrong order. Nothing was written.")
        sys.exit(1)
    s = s[:i] + new_block + s[j:]

# ---------------------------------------------------------------------------
# 1. docstring label (only if present)
# ---------------------------------------------------------------------------
if "v8 (held tips + Discord page," in s:
    s = s.replace("v8 (held tips + Discord page,", "v9 (durable push subs, held tips + Discord page,", 1)

# ---------------------------------------------------------------------------
# 2. _save never writes subscriptions into the big blob again
# ---------------------------------------------------------------------------
rep('        payload = json.dumps(s)',
    '        # v9: push subscriptions live in their own Redis hash, never in this blob,\n'
    '        # so a /push (or a stale process) can no longer wipe them.\n'
    '        payload = json.dumps({k: v for k, v in s.items() if k != "push_subs"})',
    "save: strip push_subs")

# ---------------------------------------------------------------------------
# 3. subscription storage
# ---------------------------------------------------------------------------
SUBS_BLOCK = '''_push_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# v9: durable push subscriptions. One Redis hash, one field per device, so
# adding/removing a device is a single-field write that can never clobber the
# others, and the tips blob (_save) never contains them.
# ---------------------------------------------------------------------------
SUBS_KEY = STORE_KEY + ":push_subs"
_subs = {}   # sub_key -> {"subscription":..., "prefs":..., "gone":bool, "fails":int}

def _redis_cmd(*cmd):
    r = requests.post(UPSTASH_URL.rstrip("/"), headers=_headers(), json=list(cmd), timeout=5)
    r.raise_for_status()
    return r.json().get("result")

def _subs_fetch():
    """All stored subscriptions, or None if Redis could not be read (never
    confuse 'could not read' with 'there are none')."""
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
    inside the tips blob (only when Redis was readable, so a failed read can
    never cause a wipe)."""
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
            _save(_store)      # rewrites the blob without push_subs

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
'''
rep('_push_lock = asyncio.Lock()', SUBS_BLOCK.rstrip("\n"), "subs infrastructure")

rep('_register_all_silks()\n\n# Rendered-HTML cache',
    '_register_all_silks()\n_subs_boot()\n\n# Rendered-HTML cache',
    "boot hook")

# ---------------------------------------------------------------------------
# 4. keep subscriptions in sync between processes
# ---------------------------------------------------------------------------
rep('''    subs = list(_store.get("push_subs", {}).items())
    if not subs:
        return''',
    '''    await _sync_subs()
    subs = [(k, e) for k, e in _subs.items() if not e.get("gone")]
    if not subs:
        return''',
    "notify: subs source")

rep('''            try:
                await run_in_threadpool(_send_push_sync, entry["subscription"], payload)
                _notified.add(dedupe)
            except WebPushException as e:
                if "410" in str(e) or "404" in str(e):
                    # Subscription expired/revoked on the browser's end — drop it.
                    _store.get("push_subs", {}).pop(key, None)
                    await run_in_threadpool(_save, _store)
            except Exception:
                pass''',
    '''            try:
                await run_in_threadpool(_send_push_sync, entry["subscription"], payload)
                _notified.add(dedupe)
                if entry.get("fails") or entry.get("gone"):
                    entry["fails"] = 0
                    entry.pop("gone", None)
                    await run_in_threadpool(_subs_put, key, entry)
            except Exception as e:
                # v9: a failed send NEVER deletes a subscription. 404/410 just marks
                # it gone (the device replaces it itself next time the app opens).
                code = _push_status(e)
                if code in (404, 410):
                    entry["gone"] = True
                else:
                    entry["fails"] = int(entry.get("fails", 0) or 0) + 1
                entry["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"
                print(f"[notify] send failed for {key}: status={code} {entry['last_error']}")
                await run_in_threadpool(_subs_put, key, entry)''',
    "notify: failure handling")

rep('''                            _store.clear()
                            _store.update(fresh)
                            _store.setdefault("push_subs", {})
                            _register_all_silks()
            except Exception:
                pass
    asyncio.create_task(_loop())''',
    '''                            _store.clear()
                            _store.update(fresh)
                            _store.setdefault("push_subs", {})
                            _register_all_silks()
                await _sync_subs()
            except Exception:
                pass
    asyncio.create_task(_loop())''',
    "background sync: subs")

# ---------------------------------------------------------------------------
# 5. subscribe / unsubscribe / prefs endpoints
# ---------------------------------------------------------------------------
ENDPOINTS = '''@app.get("/api/push/prefs")
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
        # The device explicitly replaced this subscription with a new one.
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

'''
swap_between('@app.get("/api/push/prefs")', '@app.post("/api/push/test")', ENDPOINTS, "push endpoints")

# ---------------------------------------------------------------------------
# 6. service worker + heal script
# ---------------------------------------------------------------------------
SW_BLOCK = r'''@app.get("/sw.js")
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
    data: { url: data.url || '/' }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});
self.addEventListener('notificationclick', function(event){
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.openWindow(url));
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
        try { localStorage.setItem(FLAG, '1'); } catch(e) {}
        return;
      }
      var res = await post('/api/push/subscribe', {subscription: sub.toJSON(), prefs: prefs, resync: true, replaces: replaces});
      if (res.ok) { try { localStorage.setItem(FLAG, '1'); } catch(e) {} }
    } catch(e) {}
  }
  var inflight = null;
  window.thePostPushHeal = function(){
    if (!inflight) inflight = run().then(function(){ inflight = null; }, function(){ inflight = null; });
    return inflight;
  };
  window.thePostPushHeal();
  var last = Date.now();
  document.addEventListener('visibilitychange', function(){
    if (document.visibilityState === 'visible' && Date.now() - last > 600000) {
      last = Date.now();
      window.thePostPushHeal();
    }
  });
})();
""".replace("__VAPID__", "%VAPID%")

@app.get("/push-heal.js")
async def push_heal_js():
    js = _HEAL_JS.replace("%VAPID%", VAPID_PUBLIC_KEY if _PUSH_READY else "")
    return Response(js, media_type="application/javascript", headers={"Cache-Control": "no-cache"})

'''
swap_between('@app.get("/sw.js")', 'def _stat_row(', SW_BLOCK, "service worker")

rep('</script>\n</body></html>"""',
    '</script>\n<script src="/push-heal.js"></script>\n</body></html>"""',
    "shell: heal script")

# ---------------------------------------------------------------------------
# 7. Settings page JS (these strings sit inside an f-string, so braces are doubled)
# ---------------------------------------------------------------------------
rep("""  var sub=await _getSub();
  if(!sub){{ _setStatus('Not enabled on this device yet.'); return; }}
  try{{
    var r=await fetch('/api/push/prefs?endpoint='+encodeURIComponent(sub.endpoint));""",
    """  if(window.thePostPushHeal){{ try{{ await window.thePostPushHeal(); }}catch(e){{}} }}
  var sub=await _getSub();
  if(!sub){{ _setStatus('Not enabled on this device yet.'); return; }}
  try{{
    var r=await fetch('/api/push/prefs?endpoint='+encodeURIComponent(sub.endpoint));""",
    "settings: heal first")

rep("""    }} else {{
      _setStatus('Not enabled on this device yet.');
    }}
  }}catch(e){{ _setStatus('Could not load settings for this device.'); }}""",
    """    }} else {{
      // this device is still subscribed in the browser but the server has no record: re-register it
      var p=_currentPrefs(); p.enabled=true;
      var rr=await fetch('/api/push/subscribe',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{subscription:sub.toJSON(),prefs:p,resync:true}})}});
      if(rr.ok){{ document.getElementById('pref-enabled').checked=true; _setStatus('Enabled on this device.'); }}
      else {{ _setStatus('Not enabled on this device yet.'); }}
    }}
  }}catch(e){{ _setStatus('Could not load settings for this device.'); }}""",
    "settings: no server record")

rep("""    await fetch('/api/push/subscribe',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{subscription:sub.toJSON(),prefs:_currentPrefs()}})}});
    _setStatus('Enabled on this device.');""",
    """    var sr=await fetch('/api/push/subscribe',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{subscription:sub.toJSON(),prefs:_currentPrefs()}})}});
    if(!sr.ok) throw new Error('subscribe failed');
    try{{ localStorage.setItem('thepost_push_on','1'); localStorage.setItem('thepost_push_prefs',JSON.stringify(_currentPrefs())); }}catch(e){{}}
    _setStatus('Enabled on this device.');""",
    "settings: enable")

rep("""    if(sub){{
      await fetch('/api/push/unsubscribe',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{endpoint:sub.endpoint}})}});
      await sub.unsubscribe();
    }}
    _setStatus('Notifications disabled on this device.');""",
    """    try{{ localStorage.setItem('thepost_push_on','0'); }}catch(e){{}}
    if(sub){{
      var ur=await fetch('/api/push/unsubscribe',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{endpoint:sub.endpoint}})}});
      if(!ur.ok) throw new Error('server unsubscribe failed');
      await sub.unsubscribe();
    }}
    _setStatus('Notifications disabled on this device.');""",
    "settings: disable")

rep("""  var sub=await _getSub();
  if(!sub) return;
  await fetch('/api/push/prefs',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{endpoint:sub.endpoint,prefs:_currentPrefs()}})}});""",
    """  var sub=await _getSub();
  if(!sub) return;
  try{{ localStorage.setItem('thepost_push_prefs',JSON.stringify(_currentPrefs())); }}catch(e){{}}
  await fetch('/api/push/prefs',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{endpoint:sub.endpoint,prefs:_currentPrefs()}})}});""",
    "settings: remember prefs")

rep("loadPushSettings();\n</script>'''",
    "window.addEventListener('load', function(){{ loadPushSettings(); }});\n</script>'''",
    "settings: run after load")

# ---------------------------------------------------------------------------
# write (backup first)
# ---------------------------------------------------------------------------
backup = os.path.join(os.path.dirname(os.path.abspath(path)), "server_v8_backup.py")
if not os.path.exists(backup):
    shutil.copyfile(path, backup)
out = s.replace("\n", "\r\n") if crlf else s
open(path, "wb").write(out.encode("utf-8"))
print(f"Patched {path} to v9. Backup of the old file: {backup}")
