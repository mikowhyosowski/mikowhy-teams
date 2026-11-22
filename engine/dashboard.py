"""Live Dashboard — podgląd zadań na żywo w przeglądarce (read-only + akcje Sponsora):

  python -m engine.dashboard [--port 8765]   ->   http://localhost:8765

Funkcje:
  - tabela zadań: klik w wiersz = szczegóły zadania (panel boczny)
  - zadania AWAITING_APPROVAL są PODŚWIETLONE (pulsują żółte) — to czeka na Ciebie
  - w szczegółach: komentarze agentów, pole do komentarza Sponsora,
    przyciski ✅ Zaakceptuj / 🛑 Odrzuć (dla AWAITING_APPROVAL), link PR na GitHubie
  - API: GET /api/state, GET /api/task/<id>, POST /api/task/<id>/{comment,approve,decline}
Odświeżanie: fetch /api/state co 2s. Zero zależności (stdlib http.server).
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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
 tbody tr{cursor:pointer} tbody tr:hover{background:#161b22}
 tr.awaiting{background:#3d3000;animation:pulse 1.2s infinite}
 @keyframes pulse{0%,100%{background:#3d3000}50%{background:#5a4700}}
 .badge{background:#b08800;color:#000;border-radius:4px;padding:0 6px;font-size:11px;font-weight:bold}
 .st{font-weight:bold} .muted{color:#8b949e} .feed{font-size:12px} .feed div{border-bottom:1px solid #21262d;padding:3px 0}
 #ts{color:#8b949e;font-size:11px}
 #detail{position:fixed;top:0;right:-460px;width:440px;height:100%;background:#161b22;border-left:1px solid #30363d;
   padding:14px;overflow-y:auto;transition:right .2s;z-index:10}
 #detail.open{right:0}
 #detail h3{font-size:14px;margin:4px 0}
 #detail textarea{width:100%;height:70px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;font-family:inherit}
 #detail button{margin:4px 4px 0 0;padding:5px 10px;background:#21262d;color:#c9d1d9;border:1px solid #30363d;cursor:pointer}
 #detail button.ok{background:#1f6feb} #detail button.no{background:#da3633}
 .kv{font-size:12px;margin:2px 0} .kv b{color:#8b949e}
</style></head><body>
<h1>🏭 Dark Factory — live <span id="ts"></span></h1>
<h2>Projekty</h2><div id="projects"></div>
<h2>Zadania <span class="badge" id="awaitCount"></span> <span class="muted">(kliknij wiersz = szczegóły; żółte = czeka na Twoją akceptację)</span></h2>
<table id="tasks"><thead><tr><th></th><th>id</th><th>tytuł</th><th>status</th><th>substep</th><th>retry</th><th>rola</th><th>projekt</th><th>PR</th><th>updated</th></tr></thead><tbody></tbody></table>
<h2>💬 Komentarze agentów (feed)</h2><div class="feed" id="comments"></div>
<h2>📈 Ostatnie eventy</h2><div class="feed" id="metrics"></div>
<div id="detail"><button onclick="closeDetail()" style="float:right">✖</button><div id="detailBody">wybierz zadanie…</div></div>
<script>
let curTask = null;
async function refresh(){
  const r = await fetch('/api/state'); const d = await r.json();
  document.getElementById('ts').textContent = new Date().toLocaleTimeString();
  document.getElementById('projects').innerHTML = d.projects.map(p =>
    `<span class="muted">🗂</span> <b>${p.project_id}</b> policy=${p.approval_policy||'manual'} read_only=${p.read_only?1:0} target=${p.target}`).join('<br>');
  const tb = document.querySelector('#tasks tbody'); tb.innerHTML = '';
  let awaiting = 0;
  for(const t of d.tasks){
    if(t.status==='AWAITING_APPROVAL') awaiting++;
    const tr = tb.insertRow();
    if(t.status==='AWAITING_APPROVAL') tr.className='awaiting';
    tr.onclick = () => openTask(t.id);
    const ago = Math.round((Date.now()/1000 - t.updated_at)) + 's';
    tr.innerHTML = `<td>${d.icons[t.status]||'?'}</td><td><b>${t.id}</b></td><td>${(t.title||'').slice(0,60)}</td>
      <td class="st">${t.status==='AWAITING_APPROVAL'?'<span class="badge">TWÓJ RUCH</span> '+t.status:t.status}</td>
      <td class="muted">${t.substep||''}</td><td>${t.retry_count}</td>
      <td class="muted">${t.role||''}</td><td class="muted">${t.project_id||''}</td>
      <td class="muted">${t.pr?('!'+t.pr):''}</td><td class="muted">${ago}</td>`;
  }
  document.getElementById('awaitCount').textContent = awaiting ? awaiting+' czeka na Ciebie' : '';
  document.getElementById('comments').innerHTML = d.comments.map(c =>
    `<div><span class="muted">${new Date(c.ts*1000).toLocaleTimeString()}</span> <b>${c.author}</b> ${c.task_id?('['+c.task_id+']'):''} ${c.text.slice(0,200)}</div>`).join('');
  document.getElementById('metrics').innerHTML = d.metrics.map(m =>
    `<div><span class="muted">${new Date(m.ts*1000).toLocaleTimeString()}</span> ${m.kind} ${m.task_id||''} <span class="muted">${(m.payload||'').slice(0,100)}</span></div>`).join('');
  if(curTask) openTask(curTask, true);
}
async function openTask(id, keep){
  curTask = id;
  const r = await fetch('/api/task/'+id); const t = await r.json();
  document.getElementById('detail').classList.add('open');
  const d = t.task;
  let html = `<h3>${t.icons[d.status]||''} ${d.id}</h3>
    <div class="kv"><b>tytuł:</b> ${d.title||''}</div>
    <div class="kv"><b>status:</b> ${d.status} | <b>substep:</b> ${d.substep||'-'} | <b>retry:</b> ${d.retry_count} | <b>read_only:</b> ${d.read_only?1:0}</div>
    <div class="kv"><b>rola:</b> ${d.role||''} | <b>projekt:</b> ${d.project_id||''} | <b>milestone:</b> ${d.milestone||''}</div>
    <div class="kv"><b>branch:</b> ${d.branch||'-'} | <b>PR:</b> ${d.pr?('!'+d.pr):'-'}</div>
    <div class="kv"><b>scope:</b> ${(d.scope_paths||[]).join(', ')||'-'}</div>
    <div class="kv"><b>AC:</b> ${(d.acceptance_criteria||'').slice(0,300)}</div>
    <div class="kv"><b>uwagi:</b> ${(d.review_notes||'').slice(0,300)}</div>
    <h2>Komentarze</h2><div class="feed">${(t.comments||[]).map(c=>`<div><span class="muted">${new Date(c.ts*1000).toLocaleTimeString()}</span> <b>${c.author}</b> ${c.text.slice(0,160)}</div>`).join('')||'brak'}</div>
    <h2>💬 Twój komentarz</h2>
    <textarea id="cbox" placeholder="uwaga do zadania (też leci na PR na GitHubie)…"></textarea>
    <button onclick="sendComment()">Dodaj komentarz</button>`;
  if(d.status==='AWAITING_APPROVAL'){
    html += `<button class="ok" onclick="approve()">✅ Zaakceptuj → READY</button>
             <button class="no" onclick="decline()">🛑 Odrzuć → BLOCKED</button>`;
  }
  document.getElementById('detailBody').innerHTML = html;
}
function closeDetail(){ document.getElementById('detail').classList.remove('open'); curTask=null; }
async function sendComment(){
  const txt = document.getElementById('cbox').value.trim(); if(!txt||!curTask) return;
  await fetch('/api/task/'+curTask+'/comment', {method:'POST', body: JSON.stringify({text: txt})});
  document.getElementById('cbox').value=''; refresh();
}
async function approve(){ await fetch('/api/task/'+curTask+'/approve', {method:'POST', body:'{}'}); refresh(); }
async function decline(){ const why = prompt('Powód odrzucenia:'); if(why===null) return;
  await fetch('/api/task/'+curTask+'/decline', {method:'POST', body: JSON.stringify({reason: why})}); refresh(); }
refresh(); setInterval(refresh, 2000);
</script></body></html>"""


def snapshot() -> dict:
    s = State()
    from engine import github_bridge
    tasks = []
    for t in s.list_tasks():
        tasks.append({k: t.get(k) for k in ("id", "title", "status", "substep", "retry_count",
                                             "role", "project_id", "branch", "updated_at", "read_only")})
        tasks[-1]["pr"] = github_bridge.pr_for_task(t["id"])
    tasks.sort(key=lambda t: (t["status"] != "AWAITING_APPROVAL", t["status"] != "RUNNING", -(t["updated_at"] or 0)))
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


def task_detail(task_id: str) -> dict:
    s = State()
    from engine import github_bridge
    t = s.get_task(task_id)
    if not t:
        return {"error": "nieznane zadanie", "icons": ICON}
    t["pr"] = github_bridge.pr_for_task(task_id)
    return {"icons": ICON, "task": t, "comments": s.recent_comments(20, task_id=task_id)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path.startswith("/index"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif u.path == "/api/state":
            self._send(200, json.dumps(snapshot(), ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        elif u.path.startswith("/api/task/"):
            tid = u.path.rsplit("/", 1)[-1]
            self._send(200, json.dumps(task_detail(tid), ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]  # ['api','task','<id>','<action>']
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if len(parts) == 4 and parts[:2] == ["api", "task"]:
            _, _, tid, action = parts
            from engine.orchestrator import cmd_approve, cmd_decline, cmd_note
            from engine.state import State
            try:
                if action == "comment":
                    cmd_note(tid, body.get("text", ""))
                    State().add_comment(tid, "sponsor/dashboard", body.get("text", ""))
                    self._send(200, b'{"ok":true}', "application/json")
                elif action == "approve":
                    cmd_approve(tid)
                    self._send(200, b'{"ok":true}', "application/json")
                elif action == "decline":
                    cmd_decline(tid, body.get("reason", "odrzucone z dashboardu"))
                    self._send(200, b'{"ok":true}', "application/json")
                else:
                    self._send(404, b'{"error":"unknown action"}', "application/json")
            except SystemExit as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": repr(e)}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


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
