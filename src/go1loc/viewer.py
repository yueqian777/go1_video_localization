"""Small localhost-only viewer for saved results; geometry remains in files."""

from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HTML = """<!doctype html>
<meta charset="utf-8"><title>go1_video_localization</title>
<style>body{font-family:system-ui;margin:2rem;max-width:1100px}pre{background:#f5f5f5;padding:1rem;overflow:auto} .warning{color:#a33}#canvas{max-width:100%;border:1px solid #777;cursor:crosshair}button{margin:.5rem 0;padding:.4rem}</style>
<h1>Go1 visual localization review</h1>
<p class="warning">Local review only. Coordinates are not navigation goals and may be arbitrary-scale.</p>
<h2>Manual query ROI</h2>
<input id="image" type="file" accept="image/jpeg,image/png,image/webp"><br>
<canvas id="canvas"></canvas><br>
<button id="download" disabled>Download ROI JSON</button>
<pre id="roi">No ROI selected.</pre>
<div id="app">Loading result…</div>
<script>
let imageName='',start=null,current=null; const input=document.getElementById('image'),canvas=document.getElementById('canvas'),ctx=canvas.getContext('2d');
input.onchange=()=>{const f=input.files[0];if(!f)return;imageName=f.name;const img=new Image();img.onload=()=>{canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;ctx.drawImage(img,0,0);canvas._img=img};img.src=URL.createObjectURL(f)};
canvas.onmousedown=e=>{if(!canvas._img)return;const r=canvas.getBoundingClientRect();start=[(e.clientX-r.left)*canvas.width/r.width,(e.clientY-r.top)*canvas.height/r.height];current=start};
canvas.onmousemove=e=>{if(!start)return;const r=canvas.getBoundingClientRect();current=[(e.clientX-r.left)*canvas.width/r.width,(e.clientY-r.top)*canvas.height/r.height];ctx.drawImage(canvas._img,0,0);ctx.strokeStyle='#e22';ctx.lineWidth=3;ctx.strokeRect(start[0],start[1],current[0]-start[0],current[1]-start[1])};
canvas.onmouseup=()=>{if(!start||!current)return;const roi=[Math.min(start[0],current[0]),Math.min(start[1],current[1]),Math.max(start[0],current[0]),Math.max(start[1],current[1])];document.getElementById('roi').textContent=JSON.stringify({query_id:imageName,roi},null,2);const blob=new Blob([JSON.stringify({query_id:imageName,roi},null,2)],{type:'application/json'});document.getElementById('download').disabled=false;document.getElementById('download').onclick=()=>{const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='roi.json';a.click()};start=null};
fetch('viewer_payload.json').then(r=>r.json()).then(data=>{
  const app=document.getElementById('app');
  app.innerHTML='<h2>Status: '+data.result.pose_status+'</h2><pre>'+JSON.stringify(data,null,2)+'</pre>';
}).catch(e=>document.getElementById('app').textContent='No viewer_payload.json: '+e);
</script>"""


def prepare_viewer(root: Path, result: dict | None = None, manifest: dict | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(HTML, encoding="utf-8")
    payload = {"result": result or {}, "map": manifest or {}}
    (root / "viewer_payload.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return root


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    root = root.resolve()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"viewer listening on http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
