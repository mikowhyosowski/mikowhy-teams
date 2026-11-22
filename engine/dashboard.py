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
 tr.flash td{animation:flash 2.5s} .feed div.flash{animation:flash 2.5s}
 @keyframes flash{0%{background:#1f6feb88}100%{background:transparent}}
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
<table id="tasks"><thead><tr><th></th><th>id</th><th>tytuł</th><th>status</th><th>substep</th><th>retry</th><th>kto (rola)</th><th>projekt</th><th>pracuje</th><th>PR</th><th>updated</th></tr></thead><tbody></tbody></table>
<h2>⚡ Live — ostatnie wydarzenia (kto przejął / zmiana statusu, auto-refresh 2s)</h2><div class="feed" id="events"></div>
<h2>💬 Komentarze agentów (feed)</h2><div class="feed" id="comments"></div>
<h2>📈 Ostatnie eventy</h2><div class="feed" id="metrics"></div>
<h2>🔴 Live — .agents/runtime (surowe odpowiedzi LLM) <button onclick="toggleLive()">▶/⏸</button></h2>
<div class="feed" id="runtime"></div>
<h2>📜 Logi procesów (.agents/logs) — klik = tail</h2>
<div class="feed" id="logs"></div>
<pre id="viewer" style="background:#0d1117;border:1px solid #30363d;padding:8px;max-height:300px;overflow:auto;font-size:11px"></pre>
<div id="detail"><button onclick="closeDetail()" style="float:right">✖</button><div id="detailBody">wybierz zadanie…</div></div>
<script>
let curTask = null;
let lastEventTs = 0;
const taskStatus = {};
function fmtDur(s){ s=Math.round(s); const m=Math.floor(s/60), h=Math.floor(m/60); return (h?h+'h ':'')+(m%60)+'m '+(s%60)+'s'; }
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
    if(taskStatus[t.id] && taskStatus[t.id] !== t.status){ tr.className = (tr.className?tr.className+' ':'')+'flash'; }
    taskStatus[t.id] = t.status;
    tr.onclick = () => openTask(t.id);
    const ago = Math.round((Date.now()/1000 - t.updated_at)) + 's';
    const indent = '&nbsp;'.repeat((t.depth||0)*4) + ((t.depth||0)?'↳ ':'')
    const elapsed = t.status==='RUNNING' && t.started_at ? fmtDur(Date.now()/1000 - t.started_at) : '';
    tr.innerHTML = `<td>${d.icons[t.status]||'?'}</td><td style="padding-left:${8+(t.depth||0)*16}px"><b>${indent}${t.id}</b></td><td>${(t.title||'').slice(0,60)}</td>
      <td class="st">${t.status==='AWAITING_APPROVAL'?'<span class="badge">TWÓJ RUCH</span> '+t.status:t.status}</td>
      <td class="muted">${t.substep||''}</td><td>${t.retry_count}</td>
      <td class="muted">${t.persona||t.role||''}</td><td class="muted">${t.project_id||''}</td>
      <td class="muted">${elapsed}</td>
      <td class="muted">${t.pr?('!'+t.pr):''}</td><td class="muted">${ago}</td>`;
  }
  document.getElementById('awaitCount').textContent = awaiting ? awaiting+' czeka na Ciebie' : '';
  const ev = document.getElementById('events');
  ev.innerHTML = (d.events||[]).map(e => {
    const isNew = e.ts > lastEventTs;
    const p = e; const txt = p.event ? p.event : `${p.from||'?'} → ${p.to||'?'}` + (p.claimed_by?(' przez '+p.claimed_by):'');
    return `<div class="${isNew?'flash':''}" data-ts="${e.ts}"><span class="muted">${new Date(e.ts*1000).toLocaleTimeString()}</span> <b>${e.task_id||''}</b> ${txt} <span class="muted">${(p.branch||p.review_notes||'')?String(p.branch||p.review_notes||'').slice(0,60):''}</span></div>`;
  }).join('');
  if((d.events||[]).length) lastEventTs = Math.max(lastEventTs, ...d.events.map(e=>e.ts));
  document.getElementById('comments').innerHTML = d.comments.map(c =>
    `<div><span class="muted">${new Date(c.ts*1000).toLocaleTimeString()}</span> <b>${c.author}</b> ${c.task_id?('['+c.task_id+']'):''} ${c.text.slice(0,200)}</div>`).join('');
  document.getElementById('metrics').innerHTML = d.metrics.map(m =>
    `<div><span class="muted">${new Date(m.ts*1000).toLocaleTimeString()}</span> ${m.kind} ${m.task_id||''} <span class="muted">${(m.payload||'').slice(0,100)}</span></div>`).join('');
  if(curTask) openTask(curTask, true);
  document.getElementById('runtime').innerHTML = (d.runtime||[]).map(f =>
    `<div><a href="#" onclick="view('/api/raw/'+encodeURIComponent(f.name));return false" style="color:#58a6ff">${f.name}</a> <span class="muted">${f.size}B, ${f.age}s temu</span></div>`).join('');
  document.getElementById('logs').innerHTML = (d.logs||[]).map(f =>
    `<div><a href="#" onclick="view('/api/log/'+encodeURIComponent(f.name)+'?n=60');return false" style="color:#58a6ff">${f.name}</a> <span class="muted">${(f.size/1024).toFixed(1)}KB, ${f.age}s temu</span></div>`).join('');
}
async function view(url){
  const r = await fetch(url); const t = await r.text();
  document.getElementById('viewer').textContent = t;
  document.getElementById('viewer').scrollTop = 1e9;
}
function toggleLive(){ liveOn = !liveOn; }
let liveOn = true;
async function openTask(id, keep){
  curTask = id;
  const r = await fetch('/api/task/'+id); const t = await r.json();
  document.getElementById('detail').classList.add('open');
  const d = t.task;
  let html = `<h3>${t.icons[d.status]||''} ${d.id}</h3>
    <div class="kv"><b>tytuł:</b> ${d.title||''}</div>
    <div class="kv"><b>status:</b> ${d.status} | <b>substep:</b> ${d.substep||'-'} | <b>retry:</b> ${d.retry_count} | <b>read_only:</b> ${d.read_only?1:0}${d.mode==='llm'?' | <b>🤖 tryb LLM</b>':''}</div>
    <div class="kv"><b>rola:</b> ${d.role||''} → <b>${d.persona||''}</b> | <b>projekt:</b> ${d.project_id||''} | <b>milestone:</b> ${d.milestone||''}</div>
    <div class="kv"><b>przejął:</b> ${d.claimed_by||'-'}${d.started_at?` od ${new Date(d.started_at*1000).toLocaleTimeString()}`:''}${d.elapsed!=null?` (<b>${fmtDur(d.elapsed)}</b>)`:''}</div>
    <div class="kv"><b>rodzic:</b> ${d.parent_task?`<a href="#" onclick="openTask('${d.parent_task}');return false" style="color:#58a6ff">${d.parent_task}</a>`:'— (zadanie główne)'}</div>
    <h2>📜 Historia zmian</h2><div class="feed">${(t.events||[]).map(e=>`<div><span class="muted">${new Date(e.ts*1000).toLocaleTimeString()}</span> ${e.event||((e.from||'?')+' → '+(e.to||'?'))}${(e.claimed_by||e.branch)?(' <span class="muted">'+String(e.claimed_by||e.branch||'').slice(0,60)+'</span>'):''}</div>`).join('')||'brak'}</div>
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
setInterval(() => { const v = document.getElementById('viewer'); if (liveOn && v.dataset.url) view(v.dataset.url); }, 2000);
const _view = view; view = function(u){ document.getElementById('viewer').dataset.url = u; return _view(u); };
</script></body></html>"""


def snapshot() -> dict:
    # State() jest tanie (współdzielone conn per proces) — nie zamykamy per request
    return _snapshot(State())


def _snapshot(s: State) -> dict:
    from engine import github_bridge, personas
    raw = s.list_tasks()
    by_id = {t["id"]: t for t in raw}

    def depth(t: dict, seen: set | None = None) -> int:
        seen = seen or set()
        p = t.get("parent_task")
        if not p or p in seen or p not in by_id:
            return 0
        return 1 + depth(by_id[p], seen | {t["id"]})

    tasks = []
    for t in raw:
        tasks.append({k: t.get(k) for k in ("id", "title", "status", "substep", "retry_count",
                                             "role", "project_id", "branch", "updated_at", "read_only",
                                             "mode", "parent_task", "started_at", "claimed_by")})
        tasks[-1]["pr"] = github_bridge.pr_for_task(t["id"])
        p = personas.persona_for_role(t.get("role") or "")
        tasks[-1]["persona"] = f"{p['name']} · {p['position']}"
        tasks[-1]["depth"] = depth(t)
        if t.get("status") == "RUNNING" and t.get("started_at"):
            tasks[-1]["elapsed"] = max(0, time.time() - t["started_at"])
    tasks.sort(key=lambda t: (t["status"] != "AWAITING_APPROVAL", t["status"] != "RUNNING", -(t["updated_at"] or 0)))
    events = s.recent_events(30)
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
    runtime_dir = ROOT / ".agents" / "runtime"
    runtime = []
    if runtime_dir.exists():
        for f in sorted(runtime_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:15]:
            st = f.stat()
            runtime.append({"name": f.name, "size": st.st_size, "age": int(time.time() - st.st_mtime)})
    logs_dir = ROOT / ".agents" / "logs"
    logs = []
    if logs_dir.exists():
        for f in sorted(logs_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:15]:
            st = f.stat()
            logs.append({"name": f.name, "size": st.st_size, "age": int(time.time() - st.st_mtime)})
        return {"icons": ICON, "projects": projs, "tasks": tasks, "events": events,
                "comments": s.recent_comments(40), "metrics": metrics,
                "runtime": runtime, "logs": logs}


def task_detail(task_id: str) -> dict:
    return _task_detail(State(), task_id)


def _task_detail(s: State, task_id: str) -> dict:
    from engine import github_bridge, personas
    t = s.get_task(task_id)
    if not t:
        return {"error": "nieznane zadanie", "icons": ICON}
    t["pr"] = github_bridge.pr_for_task(task_id)
    t["persona"] = personas.persona_label(t.get("role") or "")
    if t.get("status") == "RUNNING" and t.get("started_at"):
        t["elapsed"] = max(0, time.time() - t["started_at"])
    return {"icons": ICON, "task": t, "comments": s.recent_comments(20, task_id=task_id),
            "events": s.recent_events(50, task_id=task_id)}


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
        try:
            self._do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._send(500, json.dumps({"error": repr(e)}).encode(), "application/json")
            except Exception:
                pass

    def _do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path.startswith("/index"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif u.path == "/api/state":
            self._send(200, json.dumps(snapshot(), ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        elif u.path.startswith("/api/raw/"):
            name = u.path.rsplit("/", 1)[-1]
            f = (ROOT / ".agents" / "runtime" / name)
            if f.exists() and f.parent.resolve() == (ROOT / ".agents" / "runtime").resolve():
                self._send(200, f.read_text(encoding="utf-8", errors="replace")[-8000:].encode(), "text/plain; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
        elif u.path.startswith("/api/log/"):
            name = u.path.rsplit("/", 1)[-1]
            q = parse_qs(u.query)
            n = int(q.get("n", ["50"])[0])
            f = (ROOT / ".agents" / "logs" / name)
            if f.exists() and f.parent.resolve() == (ROOT / ".agents" / "logs").resolve():
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                self._send(200, ("\n".join(lines[-n:])).encode(), "text/plain; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
        elif u.path.startswith("/api/task/"):
            tid = u.path.rsplit("/", 1)[-1]
            self._send(200, json.dumps(task_detail(tid), ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        try:
            self._do_POST()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._send(500, json.dumps({"error": repr(e)}).encode(), "application/json")
            except Exception:
                pass

    def _do_POST(self):
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
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"🏭 Dashboard: http://{args.host}:{args.port}  (Ctrl+C stop)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
