"""Live Dashboard — podgląd zadań na żywo w przeglądarce (read-only):

  python -m engine.dashboard [--port 8765]   ->   http://localhost:8765

Pokazuje: projekty + approval_policy, tabelę zadań (status/substep/retry/branch/updated),
feed komentarzy agentów („zrobiłem, proszę o sprawdzenie”) i ostatnie metryki.
Odświeżanie: fetch /api/state co 2s. Zero zależności (stdlib http.server).
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import sys
sys.path.insert(0, str(ROOT))

from engine import projects
from engine.state import State

ICON = {"READY": "⏳", "RUNNING": "🔄", "IN_REVIEW": "🔍", "AWAITING_APPROVAL": "🕒",
        "READY_TO_MERGE": "📦", "MERGED": "✅", "FAILED": "❌", "BLOCKED": "⛔", "ESCALATED": "🚨"}

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Dark Factory — live</title>
<style>
 body{background:#0d1117;color:#c9d1d9;font-family:ui-monospace,Menlo,monospace;margin:16px}
 h1{font-size:18px} h2{font-size:14px;color:#8b949e;margin:18px 0 6px}
 table{border-collapse:collapse;width:100%} td,th{border-bottom:1px solid #21262d;padding:4px 8px;text-align:left;font-size:12px}
 .st{font-weight:bold} .muted{color:#8b949e} .feed{font-size:12px} .feed div{border-bottom:1px solid #21262d;padding:3px 0}
 #ts{color:#8b949e;font-size:11px}
</style></head><body>
<h1>🏭 Dark Factory — live <span id="ts"></span></h1>
<h2>Projekty</h2><div id="projects"></div>
<h2>Zadania</h2><table id="tasks"><thead><tr><th></th><th>id</th><th>tytuł</th><th>status</th><th>substep</th><th>retry</th><th>rola</th><th>projekt</th><th>branch</th><th>updated</th></tr></thead><tbody></tbody></table>
<h2>💬 Komentarze agentów</h2><div class="feed" id="comments"></div>
<h2>📈 Ostatnie eventy</h2><div class="feed" id="metrics"></div>
<script>
async function refresh(){
  const r = await fetch('/api/state'); const d = await r.json();
  document.getElementById('ts').textContent = new Date().toLocaleTimeString();
  document.getElementById('projects').innerHTML = d.projects.map(p =>
    `<span class="muted">🗂</span> <b>${p.project_id}</b> policy=${p.approval_policy||'manual'} read_only=${p.read_only?1:0} target=${p.target}`).join('<br>');
  const tb = document.querySelector('#tasks tbody'); tb.innerHTML = '';
  for(const t of d.tasks){
    const tr = tb.insertRow();
    const ago = Math.round((Date.now()/1000 - t.updated_at)) + 's temu';
    tr.innerHTML = `<td>${d.icons[t.status]||'?'}</td><td><b>${t.id}</b></td><td>${(t.title||'').slice(0,70)}</td>
      <td class="st">${t.status}</td><td class="muted">${t.substep||''}</td><td>${t.retry_count}</td>
      <td class="muted">${t.role||''}</td><td class="muted">${t.project_id||''}</td>
      <td class="muted">${t.branch||''}</td><td class="muted">${ago}</td>`;
  }
  document.getElementById('comments').innerHTML = d.comments.map(c =>
    `<div><span class="muted">${new Date(c.ts*1000).toLocaleTimeString()}</span> <b>${c.author}</b> ${c.task_id?('['+c.task_id+']'):''} ${c.text.slice(0,220)}</div>`).join('');
  document.getElementById('metrics').innerHTML = d.metrics.map(m =>
    `<div><span class="muted">${new Date(m.ts*1000).toLocaleTimeString()}</span> ${m.kind} ${m.task_id||''} <span class="muted">${(m.payload||'').slice(0,110)}</span></div>`).join('');
}
refresh(); setInterval(refresh, 2000);
</script></body></html>"""


def snapshot() -> dict:
    s = State()
    tasks = []
    for t in s.list_tasks():
        tasks.append({k: t.get(k) for k in ("id", "title", "status", "substep", "retry_count",
                                             "role", "project_id", "branch", "updated_at", "read_only")})
    tasks.sort(key=lambda t: (t["status"] != "RUNNING", -(t["updated_at"] or 0)))
    metrics = []
    for r in s._conn().execute(
            "SELECT ts, kind, task_id, payload FROM metrics WHERE kind != 'agent_comment' "
            "ORDER BY ts DESC LIMIT 40").fetchall():
        metrics.append({"ts": r[0], "kind": r[1], "task_id": r[2], "payload": (r[3] or "")[:160]})
    projs = []
    for p in projects.list_projects():
        projs.append({"project_id": p["project_id"], "target": p.get("target_repo_path"),
                      "read_only": bool(p.get("read_only")),
                      "approval_policy": p.get("approval_policy", "manual")})
    return {"icons": ICON, "projects": projs, "tasks": tasks,
            "comments": s.recent_comments(40), "metrics": metrics}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # cicho
        pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path.startswith("/api/state"):
            body = json.dumps(snapshot(), ensure_ascii=False, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        else:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"🏭 Dashboard: http://{args.host}:{args.port}  (Ctrl+C stop)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
