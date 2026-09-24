# ---------------------------------------------------------------------------
# Watch — live stream tab. The source is an HLS (.m3u8) feed, which can't be
# dropped into an <iframe> like a normal web page, so it plays in a native
# <video> element instead: Safari/iOS plays HLS natively, everything else
# goes through hls.js (loaded from cdnjs). Both /watch and /portal/watch use
# _watch_body(), so both get this player.
#
# To add another stream later, just append to STREAM_SOURCES — a tab bar
# appears automatically once there is more than one.
# ---------------------------------------------------------------------------
STREAM_SOURCES = [
    {"id": "live", "label": "Live", "url": "https://skylivetab-new.akamaized.net/hls/live/2038780/sky1/index.m3u8"},
]

def _watch_body():
    multi = len(STREAM_SOURCES) > 1
    tabs_html = ""
    if multi:
        tabs_html = '<div class="tabs">' + "".join(
            f'<button class="tab{" active" if i==0 else ""}" data-tab="watch" '
            f'onclick="switchWatch(\'{s["id"]}\',this)">{s["label"]}</button>'
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
    return HTMLResponse(_cached_page("watch", lambda: _shell("watch", _watch_body(), _store)))

@app.get("/portal/watch", response_class=HTMLResponse)
async def portal_watch_page():
    return HTMLResponse(_cached_page("portal_watch", lambda: _shell("watch", _watch_body(), _store, friend=True)))
