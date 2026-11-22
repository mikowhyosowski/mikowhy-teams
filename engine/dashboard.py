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
import threading
import time
import uuid
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

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Dark Factory — live</title>
<style>
 body{background:#0d1117;color:#c9d1d9;font-family:ui-monospace,Menlo,monospace;margin:16px}
 h1{font-size:18px} h2{font-size:14px;color:#8b949e;margin:18px 0 6px}
 table{border-collapse:collapse;width:100%} td,th{border-bottom:1px solid #21262d;padding:4px 8px;text-align:left;font-size:12px}
 tbody tr{cursor:pointer} tbody tr:hover{background:#161b22}
 tr.awaiting{background:#3d3000;animation:pulse 1.2s infinite}
 @keyframes pulse{0%,100%{background:#3d3000}50%{background:#5a4700}}
 tr.flash td{animation:flash 2.5s} .feed div.flash{animation:flash 2.5s}
 @keyframes flash{0%{background:#1f6feb88}100%{background:transparent}}
 /* żywe ikony / kolory statusów */
 .ic{display:inline-block}
 .ic-RUNNING{animation:spin 1s linear infinite;color:#58a6ff}
 .ic-AWAITING_APPROVAL{animation:pulseIco 1.2s infinite;color:#b08800}
 .ic-FAILED,.ic-ESCALATED{color:#f85149;animation:pulseIco .8s infinite}
 .ic-MERGED{color:#3fb950}
 .ic-READY{color:#8b949e}
 @keyframes spin{to{transform:rotate(360deg)}}
 @keyframes pulseIco{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.55;transform:scale(1.25)}}
 tr.running td{background:#0d2137}
 tr.running{box-shadow:inset 3px 0 0 #1f6feb}
 /* kolory wierszy wg statusu — status widać od razu */
 tr.st-AWAITING_APPROVAL td{background:#3d3000}
 tr.st-RUNNING td{background:#0d2137}
 tr.st-IN_REVIEW td{background:#241b38}
 tr.st-READY_TO_MERGE td{background:#0d3038}
 tr.st-MERGED td{background:#0d2b1a;color:#6e7681}
 tr.st-FAILED td,tr.st-ESCALATED td{background:#3d1215}
 tr.st-BLOCKED td{background:#2b1113;color:#6e7681}
 tr.st-READY td{background:#11151c}
 .chip{border-left:3px solid #30363d}
 .chip.st-RUNNING{border-left-color:#58a6ff}
 .chip.st-AWAITING_APPROVAL{border-left-color:#b08800}
 .chip.st-IN_REVIEW{border-left-color:#a371f7}
 .chip.st-READY_TO_MERGE{border-left-color:#39c5cf}
 .chip.st-MERGED{border-left-color:#3fb950}
 .chip.st-FAILED,.chip.st-ESCALATED{border-left-color:#f85149}
 .chip.st-BLOCKED{border-left-color:#8b949e}
 .chip.st-READY{border-left-color:#484f58}
 /* ikony priorytetu — kolejka realizacji */
 .pico{font-size:13px;margin-right:2px}
 .prio{font-weight:bold;border-radius:4px;padding:0 5px;font-size:10px}
 .prio-P0{background:#f85149;color:#fff;animation:pulseIco 1s infinite}
 .prio-P1{background:#d29922;color:#000}
 .prio-P2{background:#21262d;color:#8b949e}
 .prio-P3{background:#161b22;color:#484f58}
 .live-dot{color:#f85149;animation:pulseIco .7s infinite;font-size:12px}
 #now{display:flex;gap:8px;flex-wrap:wrap}
 .nowcard{background:#0d2137;border:1px solid #1f6feb;border-radius:8px;padding:8px 12px;min-width:180px;box-shadow:0 0 10px #1f6feb33}
 .nowcard b{color:#58a6ff}
 .nowcard .clock{font-size:16px;color:#3fb950;font-variant-numeric:tabular-nums}
 .nowcard .who{color:#8b949e;font-size:11px;display:block}
 .clock{font-variant-numeric:tabular-nums;color:#3fb950}
 /* zespół */
 #team{display:flex;gap:10px;flex-wrap:wrap}
 .pcard{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:10px;width:250px;transition:border-color .2s}
 .pcard.busy{border-color:#1f6feb;box-shadow:0 0 12px #1f6feb44;animation:cardPulse 2s infinite}
 @keyframes cardPulse{0%,100%{box-shadow:0 0 6px #1f6feb33}50%{box-shadow:0 0 14px #1f6feb88}}
 .pcard .ava{font-size:26px}
 .pcard .nm{font-weight:bold}
 .pcard .pos{color:#8b949e;font-size:11px}
 .pcard .story{font-size:11px;color:#8b949e;margin:6px 0;font-style:italic}
 .pcard .cur{font-size:11px;color:#58a6ff}
 .pcard .stats{font-size:10px;color:#484f58}
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
 #askbox{width:100%;height:56px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;font-family:inherit;border-radius:6px;padding:6px}
 #askBtn{margin:6px 0;padding:6px 14px;background:#1f6feb;color:#fff;border:0;border-radius:6px;cursor:pointer}
 .ask{border-bottom:1px solid #21262d;padding:4px 0;font-size:12px}
 .ask .q{color:#c9d1d9}
 .ask .a{color:#3fb950;white-space:pre-wrap}
 .ask .run{color:#b08800}
 /* statystyki kolejki + alerty sponsora */
 #stats{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0}
 .stat-badge{background:#21262d;border:1px solid #30363d;border-radius:14px;padding:3px 12px;font-size:12px}
 .stat-badge b{font-size:14px}
 .stat-badge.warn{border-color:#b08800;color:#d29922}
 .stat-badge.bad{border-color:#f85149;color:#f85149;animation:pulseIco 1s infinite}
 .stat-badge.ok{border-color:#3fb950;color:#3fb950}
 #alerts{display:flex;gap:8px;flex-wrap:wrap}
 .alert-card{background:#3d1215;border:1px solid #f85149;border-radius:8px;padding:8px 12px;max-width:340px;cursor:pointer;animation:pulseIco 1.4s infinite}
 .alert-card.ask{border-color:#b08800;background:#3d3000}
 .alert-card b{color:#f85149}
 .alert-card.ask b{color:#d29922}
 .alert-card .muted{font-size:11px}
 .kv{font-size:12px;margin:2px 0} .kv b{color:#8b949e}
 /* przepływ pracy (kanban statusów) */
 #flow{display:flex;gap:6px;overflow-x:auto;padding:4px 0;align-items:stretch}
 .stage{background:#161b22;border:1px solid #30363d;border-radius:8px;min-width:130px;padding:6px;flex:1}
 .stage h4{margin:0 0 4px;font-size:11px;color:#8b949e;display:flex;justify-content:space-between}
 .stage .cnt{background:#21262d;border-radius:10px;padding:0 7px;font-weight:bold}
 .stage.hot{border-color:#b08800;box-shadow:0 0 8px #b0880044}
 .stage.run{border-color:#1f6feb;box-shadow:0 0 8px #1f6feb44}
 .chip{display:block;background:#0d1117;border:1px solid #30363d;border-radius:6px;margin:3px 0;padding:3px 6px;font-size:11px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .chip:hover{border-color:#58a6ff}
 .chip b{color:#58a6ff}
 .chip .who{color:#8b949e;display:block;font-size:10px}
 .arrow{align-self:center;color:#8b949e;font-size:14px}
 /* mobile */
 @media (max-width:720px){
   body{margin:8px;font-size:13px}
   table{font-size:11px} td,th{padding:3px 4px}
   #flow{flex-direction:column}
   .stage{min-width:0}
   .arrow{transform:rotate(90deg)}
   #detail{width:100%;right:-100%}
   h1{font-size:15px} h2{font-size:12px}
   .feed{font-size:11px}
   #tasks{display:block;overflow-x:auto}
 }
</style></head><body>
<h1>🏭 Dark Factory <span class="live-dot">●</span> LIVE <span id="ts"></span></h1>
<div id="stats"></div>
<h2>🚨 Wymaga interwencji Sponsora</h2>
<div id="alerts"></div>
<h2>🔥 Teraz pracują <span class="muted">(zegarki tykają na żywo)</span></h2>
<div id="now"></div>
<h2>🔀 Przepływ pracy <span class="muted">(kliknij kafelek = szczegóły zadania)</span></h2>
<div id="flow"></div>
<h2>👥 Zespół <span class="muted">(kto aktualnie nad czym pracuje + krótka historia)</span></h2>
<div id="team"></div>
<h2>Projekty</h2><div id="projects"></div>
<h2>Zadania <span class="badge" id="awaitCount"></span> <span class="muted">(kliknij wiersz = szczegóły; żółte = czeka na Twoją akceptację)</span></h2>
<table id="tasks"><thead><tr><th></th><th>prio</th><th>id</th><th>tytuł</th><th>status</th><th>substep</th><th>retry</th><th>kto (rola)</th><th>projekt</th><th>pracuje</th><th>PR</th><th>updated</th></tr></thead><tbody></tbody></table>
<h2>⚡ Live — ostatnie wydarzenia (kto przejął / zmiana statusu, auto-refresh 2s)</h2><div class="feed" id="events"></div>
<h2>💬 Zapytaj Vibe bezpośrednio <span class="muted">(bez taska — jak `llm`, ale odpowiedź od razu tutaj)</span></h2>
<textarea id="askbox" placeholder="napisz polecenie / pytanie do Vibe…"></textarea>
<button id="askBtn" onclick="sendAsk()">Wyślij do Vibe ▶</button>
<div class="feed" id="asks"></div>
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
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function refresh(){ try{ await refreshInner(); }catch(e){ document.getElementById('ts').textContent = '⚠ błąd odświeżania: '+e; } }
async function refreshInner(){
  const r = await fetch('/api/state'); const d = await r.json();
  document.getElementById('ts').textContent = new Date().toLocaleTimeString();
  const st = d.stats||{};
  document.getElementById('stats').innerHTML =
    `<span class="stat-badge ${st.awaiting?'warn':''}">🕒 czeka na Ciebie: <b>${st.awaiting||0}</b></span>`+
    `<span class="stat-badge">⏳ w kolejce (READY): <b>${st.waiting||0}</b></span>`+
    `<span class="stat-badge ok">🔄 pracują: <b>${st.running||0}</b></span>`+
    `<span class="stat-badge ${st.overdue?'bad':'ok'}">⚠️ spóźnione/stare (&gt;1h w kolejce): <b>${st.overdue||0}</b></span>`+
    `<span class="stat-badge ${st.running_long?'warn':''}">🐌 długo biegną (&gt;1h): <b>${st.running_long||0}</b></span>`;
  const alerts = d.tasks.filter(t=>t.status==='AWAITING_APPROVAL');
  document.getElementById('alerts').innerHTML = alerts.length ? alerts.map(t =>
    `<div class="alert-card ${t.substep==='ASK_SPONSOR'?'ask':''}" onclick="openTask('${t.id}')">
       <b>${t.substep==='ASK_SPONSOR'?'❓ ODPOWIEDZ':'🚨 TWÓJ RUCH'}</b> <b>${esc(t.id)}</b>
       <div>${esc((t.title_sponsor||t.title||'').slice(0,80))}</div>
       <div class="muted">${esc((t.review_notes||'').slice(0,120))}</div>
       <div class="muted">czeka ${fmtDur(t.age||0)} · ${esc(t.persona||t.role||'')}</div>
     </div>`).join('') : '<span class="muted">nic nie czeka na Twoją decyzję ✅</span>';
  document.getElementById('projects').innerHTML = d.projects.map(p =>
    `<span class="muted">🗂</span> <b>${esc(p.project_id)}</b> policy=${esc(p.approval_policy||'manual')} read_only=${p.read_only?1:0} target=${esc(p.target)}`).join('<br>');
  const PIC = {'P0':'🔥','P1':'⚡','P2':'•','P3':'🐢'};
  // przepływ pracy: kanban statusów (kolejność procesu), kafelek = zadanie
  const FLOW = ['AWAITING_APPROVAL','READY','RUNNING','IN_REVIEW','READY_TO_MERGE','MERGED','FAILED','ESCALATED','BLOCKED'];
  const byStatus = {}; for(const t of d.tasks){ (byStatus[t.status] = byStatus[t.status]||[]).push(t); }
  document.getElementById('flow').innerHTML = FLOW.map((st,i) => {
    const items = byStatus[st]||[];
    const hot = st==='AWAITING_APPROVAL' && items.length, run = st==='RUNNING';
    const chips = items.slice(0,8).map(t =>
      `<span class="chip st-${st}" onclick="openTask('${t.id}')">${PIC[t.priority||'P2']||''}<b>${esc(t.id)}</b>${t.depth?' ↳':''} ${t.status==='RUNNING'&&t.started_at?(`<span class="clock" data-started="${t.started_at}">⏱ ${fmtDur(Date.now()/1000-t.started_at)}</span>`):''}
       <span class="who">${esc(t.persona||t.role||'')}</span></span>`).join('') + (items.length>8?`<span class="chip muted">+${items.length-8} więcej…</span>`:'');
    const arrow = i < FLOW.length-1 ? '<span class="arrow">→</span>' : '';
    return `<div class="stage ${hot?'hot':''} ${run?'run':''}"><h4>${d.icons[st]||''} ${st} <span class="cnt">${items.length}</span></h4>${chips}</div>${arrow}`;
  }).join('');
  const tb = document.querySelector('#tasks tbody'); tb.innerHTML = '';
  let awaiting = 0;
  const PRIO = {'P0':0,'P1':1,'P2':2,'P3':3};
  const sorted = [...d.tasks].sort((a,b)=>(PRIO[b.priority||'P2']-PRIO[a.priority||'P2']) || ((b.status==='RUNNING')-(a.status==='RUNNING')) || ((b.updated_at||0)-(a.updated_at||0)));
  const nowCards = [];
  for(const t of sorted){
    if(t.status==='AWAITING_APPROVAL') awaiting++;
    if(t.status==='RUNNING') nowCards.push(t);
    const tr = tb.insertRow();
    tr.className = 'st-' + t.status;
    if(t.status==='AWAITING_APPROVAL') tr.className+=' awaiting';
    if(t.status==='RUNNING') tr.className+=' running';
    if(taskStatus[t.id] && taskStatus[t.id] !== t.status){ tr.className += ' flash'; }
    taskStatus[t.id] = t.status;
    tr.onclick = () => openTask(t.id);
    const ago = Math.round((Date.now()/1000 - t.updated_at)) + 's';
    const indent = '&nbsp;'.repeat((t.depth||0)*4) + ((t.depth||0)?'↳ ':'')
    const PIC = {'P0':'🔥','P1':'⚡','P2':'•','P3':'🐢'};
    const prio = `<span class="pico" title="priorytet ${t.priority||'P2'}">${PIC[t.priority||'P2']||''}</span><span class="prio prio-${t.priority||'P2'}">${t.priority||'P2'}</span>`;
    const ic = `<span class="ic ic-${t.status}">${d.icons[t.status]||'?'}</span>`;
    const clock = t.status==='RUNNING' && t.started_at ? `<span class="clock" data-started="${t.started_at}">⏱ ${fmtDur(Date.now()/1000 - t.started_at)}</span>` : '';
    tr.innerHTML = `<td>${ic}</td><td>${prio}</td><td style="padding-left:${8+(t.depth||0)*16}px"><b>${indent}${t.id}</b></td><td title="tech: ${esc(t.title||'')}">${esc((t.title_sponsor||t.title||'').slice(0,70))}</td>
      <td class="st">${t.status==='AWAITING_APPROVAL'?(t.substep==='ASK_SPONSOR'?'<span class="badge">❓ ODPOWIEDZ</span> ':'<span class="badge">TWÓJ RUCH</span> ')+t.status:t.status}</td>
      <td class="muted">${esc(t.substep||'')}</td><td>${t.retry_count}</td>
      <td class="muted">${esc(t.persona||t.role||'')}</td><td class="muted">${esc(t.project_id||'')}</td>
      <td class="muted">${clock}</td>
      <td class="muted">${t.pr?('!'+t.pr):''}</td><td class="muted">${ago}</td>`;
  }
  document.getElementById('awaitCount').textContent = awaiting ? awaiting+' czeka na Ciebie' : '';
  document.getElementById('team').innerHTML = (d.team||[]).map(m => {
    const cur = m.current ? `<div class="cur">🔨 ${esc(m.current.id)} <span class="clock" data-started="${m.current.started_at||0}">⏱ ${m.current.started_at?fmtDur(Date.now()/1000-m.current.started_at):''}</span> <span class="prio prio-${m.current.priority||'P2'}">${m.current.priority||'P2'}</span></div>` : '<div class="cur muted">— wolny/a —</div>';
    return `<div class="pcard ${m.current?'busy':''}" title="rola: ${m.role}">
      <span class="ava">${m.avatar||'🧑‍💻'}</span> <span class="nm">${esc(m.name)}</span>
      <div class="pos">${esc(m.position)} · rola: ${esc(m.role)} · ${esc(m.gender)}</div>
      <div class="story">„${esc(m.backstory)}”</div>
      ${cur}
      <div class="stats">zadania: ${m.tasks} · zmergowane: ${m.merged}</div>
    </div>`;
  }).join('');
  document.getElementById('now').innerHTML = nowCards.length ? nowCards.map(t =>
    `<div class="nowcard" onclick="openTask('${t.id}')"><b>${d.icons.RUNNING} ${t.id}</b> <span class="pico">${PIC[t.priority||'P2']||''}</span><span class="prio prio-${t.priority||'P2'}">${t.priority||'P2'}</span>
     <div class="clock" data-started="${t.started_at||0}">⏱ ${t.started_at?fmtDur(Date.now()/1000-t.started_at):''}</div>
     <span class="who">${esc(t.persona||t.role||'')} · ${esc(t.project_id||'')}</span></div>`).join('')
    : '<span class="muted">nikt teraz nie pracuje</span>';
  document.getElementById('asks').innerHTML = (d.asks||[]).map(a =>
    `<div class="ask"><span class="muted">${new Date(a.ts*1000).toLocaleTimeString()}</span> <span class="q">❯ ${esc(a.q.slice(0,300))}</span>
     ${a.status==='running'?'<div class="run">⏳ Vibe myśli…</div>':''}
     ${a.status==='error'?'<div class="a" style="color:#f85149">'+esc(String(a.answer).slice(0,500))+'</div>':''}
     ${a.status==='done'?'<div class="a">'+esc(String(a.answer).slice(0,1200))+'</div>':''}</div>`).join('');
  const ev = document.getElementById('events');
  ev.innerHTML = (d.events||[]).map(e => {
    const isNew = e.ts > lastEventTs;
    const ch = e.changes ? Object.entries(e.changes).map(([k,v])=>`${esc(k)}: <b>${esc(String(v).slice(0,60))}</b>`).join(' · ')
                         : (e.event ? e.event : `${e.from||'?'} → ${e.to||'?'}`);
    return `<div class="${isNew?'flash':''}" data-ts="${e.ts}"><span class="muted">${new Date(e.ts*1000).toLocaleTimeString()}</span> <b>${esc(e.task_id||'')}</b> ${ch} <span class="muted">${e.by?('— '+esc(e.by)):''}</span></div>`;
  }).join('');
  if((d.events||[]).length) lastEventTs = Math.max(lastEventTs, ...d.events.map(e=>e.ts));
  document.getElementById('comments').innerHTML = d.comments.map(c =>
    `<div><span class="muted">${new Date(c.ts*1000).toLocaleTimeString()}</span> <b>${esc(c.author)}</b> ${c.task_id?('['+esc(c.task_id)+']'):''} ${esc(c.text.slice(0,200))}</div>`).join('');
  document.getElementById('metrics').innerHTML = d.metrics.map(m =>
    `<div><span class="muted">${new Date(m.ts*1000).toLocaleTimeString()}</span> ${esc(m.kind)} ${esc(m.task_id||'')} <span class="muted">${esc((m.payload||'').slice(0,100))}</span></div>`).join('');
  if(curTask) openTask(curTask, true);
  document.getElementById('runtime').innerHTML = (d.runtime||[]).map(f =>
    `<div><a href="#" onclick="view('/api/raw/'+encodeURIComponent(f.name));return false" style="color:#58a6ff">${esc(f.name)}</a> <span class="muted">${f.size}B, ${f.age}s temu</span></div>`).join('');
  document.getElementById('logs').innerHTML = (d.logs||[]).map(f =>
    `<div><a href="#" onclick="view('/api/log/'+encodeURIComponent(f.name)+'?n=60');return false" style="color:#58a6ff">${esc(f.name)}</a> <span class="muted">${(f.size/1024).toFixed(1)}KB, ${f.age}s temu</span></div>`).join('');
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
  // guard (review): nie przepisuj panelu przy każdym odświeżeniu — nie kasuje wpisanego komentarza
  const key = id+'|'+(d.status||'')+'|'+(d.substep||'')+'|'+(d.updated_at||0)+'|'+(t.comments||[]).length+'|'+(t.events||[]).length;
  if(keep && document.getElementById('detailBody').dataset.key === key) return;
  document.getElementById('detailBody').dataset.key = key;
  let html = `<h3>${t.icons[d.status]||''} ${esc(d.id)}</h3>
    <div class="kv"><b>tytuł:</b> ${esc(d.title_sponsor||d.title||'')}</div>
    <div class="kv muted" style="font-size:10px">tech: ${esc(d.title||'')}</div>
    <div class="kv"><b>status:</b> ${esc(d.status)} | <b>substep:</b> ${esc(d.substep||'-')} | <b>retry:</b> ${d.retry_count} | <b>read_only:</b> ${d.read_only?1:0}${d.mode==='llm'?' | <b>🤖 tryb LLM</b>':''} | <b>prio:</b> ${esc(d.priority||'P2')}</div>
    <div class="kv"><b>rola:</b> ${esc(d.role||'')} → <b>${esc(d.persona||'')}</b> | <b>projekt:</b> ${esc(d.project_id||'')} | <b>milestone:</b> ${esc(d.milestone||'')}</div>
    <div class="kv"><b>przejął:</b> ${esc(d.claimed_by||'-')}${d.started_at?` od ${new Date(d.started_at*1000).toLocaleTimeString()}`:''}${d.elapsed!=null?` (<b>${fmtDur(d.elapsed)}</b>)`:''}</div>
    <div class="kv"><b>rodzic:</b> ${d.parent_task?`<a href="#" onclick="openTask('${esc(d.parent_task)}');return false" style="color:#58a6ff">${esc(d.parent_task)}</a>`:'— (zadanie główne)'}</div>
    <h2>📜 Historia zmian</h2><div class="feed">${(t.events||[]).map(e=>`<div><span class="muted">${new Date(e.ts*1000).toLocaleTimeString()}</span> ${esc(e.event||((e.from||'?')+' → '+(e.to||'?')))}${(e.by||e.claimed_by||e.branch)?(' <span class="muted">'+esc(String(e.by||e.claimed_by||e.branch||'').slice(0,60))+'</span>'):''}</div>`).join('')||'brak'}</div>
    <div class="kv"><b>branch:</b> ${esc(d.branch||'-')} | <b>PR:</b> ${d.pr?('!'+d.pr):'-'}</div>
    <div class="kv"><b>scope:</b> ${esc((d.scope_paths||[]).join(', ')||'-')}</div>
    <div class="kv"><b>AC:</b> ${esc((d.acceptance_criteria||'').slice(0,300))}</div>
    <div class="kv"><b>uwagi:</b> ${esc((d.review_notes||'').slice(0,300))}</div>
    <h2>Komentarze</h2><div class="feed">${(t.comments||[]).map(c=>`<div><span class="muted">${new Date(c.ts*1000).toLocaleTimeString()}</span> <b>${esc(c.author)}</b> ${esc(c.text.slice(0,160))}</div>`).join('')||'brak'}</div>
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
async function sendAsk(){
  const t = document.getElementById('askbox').value.trim(); if(!t) return;
  document.getElementById('askbox').value = '';
  document.getElementById('askBtn').disabled = true;
  try{ await fetch('/api/ask', {method:'POST', body: JSON.stringify({text: t})}); } finally { document.getElementById('askBtn').disabled = false; }
  refresh();
}
async function approve(){ await fetch('/api/task/'+curTask+'/approve', {method:'POST', body:'{}'}); refresh(); }
async function decline(){ const why = prompt('Powód odrzucenia:'); if(why===null) return;
  await fetch('/api/task/'+curTask+'/decline', {method:'POST', body: JSON.stringify({reason: why})}); refresh(); }
refresh(); setInterval(refresh, 2000);
setInterval(() => { const v = document.getElementById('viewer'); if (liveOn && v.dataset.url) view(v.dataset.url); }, 2000);
// zegarki tykają co sekundę — widać że coś się dzieje
setInterval(() => {
  document.querySelectorAll('.clock[data-started]').forEach(el => {
    const s = parseFloat(el.dataset.started); if(s > 0) el.textContent = '⏱ ' + fmtDur(Date.now()/1000 - s);
  });
}, 1000);
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
                                             "mode", "parent_task", "started_at", "claimed_by", "priority",
                                             "review_notes", "created_at", "title_sponsor")})
        tasks[-1]["age"] = max(0, time.time() - (t.get("updated_at") or t.get("created_at") or time.time()))
        tasks[-1]["pr"] = github_bridge.pr_for_task(t["id"])
        p = personas.persona_for_role(t.get("role") or "")
        tasks[-1]["persona"] = f"{p['name']} · {p['position']}"
        tasks[-1]["depth"] = depth(t)
        if t.get("status") == "RUNNING" and t.get("started_at"):
            tasks[-1]["elapsed"] = max(0, time.time() - t["started_at"])
    tasks.sort(key=lambda t: (t["status"] != "AWAITING_APPROVAL", t["status"] != "RUNNING", -(t["updated_at"] or 0)))
    events = s.recent_events(30)
    # statystyki kolejki: ile czeka, ile już "spóźnionych" (READY/AWAITING starsze niż 1h), ile długo biegnie
    stats = {
        "waiting": sum(1 for t in tasks if t["status"] == "READY"),
        "awaiting": sum(1 for t in tasks if t["status"] == "AWAITING_APPROVAL"),
        "running": sum(1 for t in tasks if t["status"] == "RUNNING"),
        "overdue": sum(1 for t in tasks if t["status"] in ("READY", "AWAITING_APPROVAL") and t.get("age", 0) > 3600),
        "running_long": sum(1 for t in tasks if t["status"] == "RUNNING" and (t.get("elapsed") or 0) > 3600),
    }
    # zespół: jedna karta per rola (persona + backstory + aktualne zadanie + liczniki)
    from engine import personas as _pers
    roles: dict[str, dict] = {}
    for t in raw:
        roles.setdefault(t.get("role") or "worker", {"role": t.get("role") or "worker", "tasks": 0, "merged": 0, "current": None})
        roles[t.get("role") or "worker"]["tasks"] += 1
        if t.get("status") == "MERGED":
            roles[t.get("role") or "worker"]["merged"] += 1
        if t.get("status") == "RUNNING":
            roles[t.get("role") or "worker"]["current"] = t
    team = []
    for r in roles.values():
        p = _pers.persona_full(r["role"])
        cur = r["current"]
        team.append({**p, "tasks": r["tasks"], "merged": r["merged"],
                     "current": ({"id": cur["id"], "started_at": cur.get("started_at"), "priority": cur.get("priority")}
                                 if cur else None)})
    team.sort(key=lambda x: (x["current"] is None, x["role"]))
    # bezpośrednie zapytania do Vibe (formularz, bez taska) — ostatnie 10 + odpowiedzi z kv
    asks = []
    for r in s._conn().execute(
            "SELECT ts, payload FROM metrics WHERE kind='direct_ask' ORDER BY ts DESC LIMIT 10").fetchall():
        p = json.loads(r[1])
        qid = p.get("qid", "")
        asks.append({"ts": r[0], "qid": qid, "q": p.get("q", ""),
                     "status": s.get_kv(f"ask_status:{qid}", "running"),
                     "answer": s.get_kv(f"ask_ans:{qid}", "")})
    asks.reverse()
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
        return {"icons": ICON, "projects": projs, "tasks": tasks, "events": events, "team": team, "asks": asks,
                "stats": stats,
                "comments": s.recent_comments(40), "metrics": metrics,
                "runtime": runtime, "logs": logs}


def _run_ask(qid: str, text: str) -> None:
    """Bezpośrednia sesja vibe (bez taska): odpowiedź Sponsora ląduje w kv i na dashboardzie."""
    s = State()
    try:
        from engine.llm_gateway import get_gateway
        from engine.team_chat import build_state_summary  # noqa: F401
        system = ("Jesteś Vibe — osobisty asystent Sponsora w Dark Factory (repo mikowhy-teams). "
                  "Odpowiadasz PO POLSKU, zwięźle, konkretnie, z emoji gdzie pasuje. "
                  "Masz kontekst stanu fabryki poniżej. Nie tworzysz zadań — tylko odpowiadasz.\n\n"
                  "# Stan fabryki:\n" + build_state_summary(s))
        gw = get_gateway()
        ans = gw.complete(system=system, user=text, step=f"DIRECT-ASK-{qid}", model="strong-model")
        s.set_kv(f"ask_ans:{qid}", (ans or "")[:6000])
        s.set_kv(f"ask_status:{qid}", "done")
    except Exception as exc:
        s.set_kv(f"ask_ans:{qid}", f"BŁĄD: {exc!r}")
        s.set_kv(f"ask_status:{qid}", "error")


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
        if u.path == "/api/ask":
            # bezpośrednia sesja vibe z formularza — BEZ taska, odpowiedź w wątku na dashboardzie
            text = str(body.get("text", "")).strip()
            if not text:
                self._send(400, b'{"error":"pusty tekst"}', "application/json")
                return
            qid = uuid.uuid4().hex[:8]
            s = State()
            s.set_kv(f"ask_status:{qid}", "running")
            s.record_metric("direct_ask", {"qid": qid, "q": text[:500], "status": "running"}, None)
            threading.Thread(target=_run_ask, args=(qid, text), daemon=True).start()
            self._send(200, json.dumps({"ok": True, "qid": qid}).encode(), "application/json")
            return
        if len(parts) == 4 and parts[:2] == ["api", "task"]:
            _, _, tid, action = parts
            from engine.orchestrator import cmd_approve, cmd_decline, cmd_note
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
