"""Live Dashboard — podgląd zadań na żywo w przeglądarce (read-only + akcje Sponsora):

  python -m engine.dashboard [--port 8765] [--host 0.0.0.0]  ->  http://<lan_ip>:8765 (z 127.0.0.1 auto-redirect)

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
import os
import re
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
#projFilters{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.pchip{border:1px solid var(--c,#30363d);background:#161b22;color:#c9d1d9;border-radius:999px;padding:3px 10px;font-size:12px;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.pchip .dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.pchip.off{opacity:.35;text-decoration:line-through}
.pchip.reset{border-color:#30363d;color:#8b949e}
.pchip:active{transform:scale(.96)}
 #detail button{margin:4px 4px 0 0;padding:5px 10px;background:#21262d;color:#c9d1d9;border:1px solid #30363d;cursor:pointer}
 #detail button.ok{background:#1f6feb} #detail button.no{background:#da3633}
 #askbox{width:100%;height:56px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;font-family:inherit;border-radius:6px;padding:6px}
 #askBtn{margin:6px 0;padding:6px 14px;background:#1f6feb;color:#fff;border:0;border-radius:6px;cursor:pointer}
 .ask{border-bottom:1px solid #21262d;padding:4px 0;font-size:12px}
 .ask .q{color:#c9d1d9}
 .ask .a{color:#3fb950;white-space:pre-wrap}
 .ask .run{color:#b08800}
 /* zakładki (layout) */
 #tabs{position:sticky;top:0;background:#0d1117;z-index:5;display:flex;gap:6px;overflow-x:auto;padding:6px 0;border-bottom:1px solid #21262d}
 #tabs button{background:#161b22;border:1px solid #30363d;color:#8b949e;border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer;white-space:nowrap}
 #tabs button.on{background:#1f6feb;color:#fff;border-color:#1f6feb;font-weight:bold}
 #tabs button:active{transform:scale(.96)}
 .tabpage{display:none;animation:fadeIn .15s}
 .tabpage.on{display:block}
 @keyframes fadeIn{from{opacity:0}to{opacity:1}}
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
 /* widok FIRMA (osiedle z góry) */
 #estateWrap{width:100%;overflow:hidden;border:1px solid #21262d;border-radius:10px}
 #estate{width:100%;height:72vh;display:block;cursor:grab;background:#0b0f14;touch-action:none}
 #estate:active{cursor:grabbing}
 #estate .desk{will-change:transform}
 #estate .desk rect{filter:drop-shadow(0 1px 2px #0009)}
 #estate .desk:hover rect{filter:brightness(1.5) drop-shadow(0 2px 4px #000a)}
 #estate g.office .plot{transition:filter .2s}
 #estate g.office:hover .plot{filter:brightness(1.3)}
 #estate g.room:hover rect{stroke:#58a6ff}
 #estate text{user-select:none}
 #estate .room.busy{animation:pulseIco 2s infinite}
 #estate .alarmzone{animation:pulseIco 1.2s infinite}
 #firmaLegend{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
 @media (max-width:720px){ #estate{height:60vh} }
 /* zakładki w panelu szczegółów + świeżość + scroll-boksy */
 .dtabs{display:flex;gap:4px;margin:4px 0 8px;flex-wrap:wrap}
 .dtabs button{background:#161b22;border:1px solid #30363d;color:#8b949e;border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer}
 .dtabs button.on{background:#1f6feb;color:#fff;border-color:#1f6feb;font-weight:bold}
 .histbox,.threadbox{max-height:280px;overflow-y:auto;border:1px solid #21262d;border-radius:8px;padding:6px;background:#0d1117}
 .histbox div{padding:3px 6px;font-size:11px;border-bottom:1px solid #161b22}
 .histbox .fresh2{background:#0e2a1a;border-left:3px solid #3fb950}
 .histbox .fresh1{background:#12233a;border-left:3px solid #1f6feb}
 .threadbox .tmsg{margin:5px 0}
 .threadbox .ta{font-size:10px;color:#8b949e}
 .threadbox .tt{font-size:12px;padding:5px 9px;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere}
 .threadbox .tmsg.me .tt{background:#1f2d4d;margin-left:18%}
 .threadbox .tmsg.agent .tt{background:#0e2a1a;margin-right:18%}
 .tenure{font-size:10px;color:#d29922;margin-top:3px}
 .tbtn{margin-top:4px;background:#21262d;border:1px solid #30363d;color:#58a6ff;border-radius:6px;padding:3px 8px;font-size:11px;cursor:pointer}
 .tcard{border:1px solid #21262d;border-left:3px solid #1f6feb;border-radius:8px;padding:6px 10px;margin:4px 0;cursor:pointer;background:#111823}
 .tcard:hover{border-color:#58a6ff}
</style></head><body>
<h1>🏭 Dark Factory <span class="live-dot">●</span> LIVE <span id="ts"></span> <span id="host" class="muted" style="font-size:11px"></span> <span id="pause" class="muted" style="font-size:11px;color:#f0b429"></span></h1>
<div id="stats"></div>
<nav id="tabs">
  <button data-tab="overview" onclick="showTab('overview')">📊 Przegląd<span id="navAlert"></span></button>
  <button data-tab="tasks" onclick="showTab('tasks')">📋 Zadania</button>
  <button data-tab="team" onclick="showTab('team')">👥 Zespół</button>
  <button data-tab="chat" onclick="showTab('chat')">💬 Czat Vibe</button>
  <button data-tab="live" onclick="showTab('live')">⚡ Live</button>
  <button data-tab="firma" onclick="showTab('firma')">🏢 Firma</button>
</nav>
<section class="tabpage" id="tab-overview">
<h2>🚨 Wymaga interwencji Sponsora</h2>
<div id="alerts"></div>
<h2>🔥 Teraz pracują <span class="muted">(zegarki tykają na żywo)</span></h2>
<div id="now"></div>
<h2>🔀 Przepływ pracy <span class="muted">(kliknij kafelek = szczegóły zadania)</span></h2>
<div id="flow"></div>
<h2>Projekty</h2><div id="projects"></div>
</section>
<section class="tabpage" id="tab-tasks">
<h2>Zadania <span class="badge" id="awaitCount"></span> <span class="muted">(kliknij wiersz = szczegóły; żółte = czeka na Twoją akceptację)</span></h2>
<div id="projFilters"></div>
<table id="tasks"><thead><tr><th></th><th>prio</th><th>id</th><th>tytuł</th><th>status</th><th>substep</th><th>retry</th><th>kto (rola)</th><th>projekt</th><th>pracuje</th><th>PR</th><th>updated</th></tr></thead><tbody></tbody></table>
</section>
<section class="tabpage" id="tab-team">
<h2>👥 Zespół <span class="muted">(kto aktualnie nad czym pracuje + krótka historia + staż; 💬 = rozmowa z osobą)</span></h2>
<div id="team"></div>
<h2>💬 Wątki rozmów z zespołem <span class="muted">(pytasz na Telegramie lub tutaj — odpowiada kompetentna osoba; kliknij wątek, żeby kontynuować)</span></h2>
<div id="threads"></div>
</section>
<section class="tabpage" id="tab-chat">
<h2>💬 Zapytaj Vibe bezpośrednio <span class="muted">(bez taska — jak `llm`, ale odpowiedź od razu tutaj)</span></h2>
<textarea id="askbox" placeholder="napisz polecenie / pytanie do Vibe…"></textarea>
<button id="askBtn" onclick="sendAsk()">Wyślij do Vibe ▶</button>
<div class="feed" id="asks"></div>
<h2>💬 Komentarze agentów (feed)</h2><div class="feed" id="comments"></div>
</section>
<section class="tabpage" id="tab-live">
<h2>⚡ Live — ostatnie wydarzenia (kto przejął / zmiana statusu, auto-refresh 2s)</h2><div class="feed" id="events"></div>
<h2>📈 Ostatnie eventy</h2><div class="feed" id="metrics"></div>
<h2>🔴 Live — .agents/runtime (surowe odpowiedzi LLM) <button onclick="toggleLive()">▶/⏸</button></h2>
<div class="feed" id="runtime"></div>
<h2>📜 Logi procesów (.agents/logs) — klik = tail</h2>
<div class="feed" id="logs"></div>
<pre id="viewer" style="background:#0d1117;border:1px solid #30363d;padding:8px;max-height:300px;overflow:auto;font-size:11px"></pre>
</section>
<section class="tabpage" id="tab-firma">
<h2>🏢 Firma — osiedle biur <span class="muted">(z góry: biuro=projekt · pokój=agent · biurko=zadanie; kółko=zoom, drag=przesuwaj, klik biurko=szczegóły; klik biuro=filtr projektu)</span></h2>
<div id="firmaLegend"></div>
<div id="estateWrap"><svg id="estate" preserveAspectRatio="xMidYMid meet"></svg></div>
</section>
<div id="detail"><button onclick="closeDetail()" style="float:right">✖</button><div id="detailBody">wybierz zadanie…</div></div>
<script>
let curTask = null;
let lastEventTs = 0;
// kolorowe filtry projektów — hiddenProjs przechowywane w localStorage
const hiddenProjs = new Set(JSON.parse(localStorage.getItem('hiddenProjs')||'[]'));
function projColor(s){ let h=0; for(const c of String(s)) h=(h*31+c.charCodeAt(0))>>>0; return 'hsl('+(h%360)+',70%,45%)'; }
function toggleProj(pid){ if(hiddenProjs.has(pid)) hiddenProjs.delete(pid); else hiddenProjs.add(pid); localStorage.setItem('hiddenProjs', JSON.stringify([...hiddenProjs])); refresh(); }
function resetProjs(){ hiddenProjs.clear(); localStorage.setItem('hiddenProjs','[]'); refresh(); }
// fallback: gdy strona otwarta przez 127.0.0.1/localhost — przekieruj przeglądarkę na adres LAN
(async function redirectToLan(){
  try{
    if(!['127.0.0.1','localhost','::1'].includes(location.hostname)) return;
    const r = await fetch('/api/state'); const d = await r.json();
    if(d.host && d.host.dashboard_url && location.origin !== d.host.dashboard_url){
      location.replace(d.host.dashboard_url + '/');
    }
  }catch(e){}
})();
const taskStatus = {};
function fmtDur(s){ s=Math.round(s); const m=Math.floor(s/60), h=Math.floor(m/60); return (h?h+'h ':'')+(m%60)+'m '+(s%60)+'s'; }
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
let refreshPaused = false;
function setPaused(p){ refreshPaused = p; document.getElementById('pause').textContent = p ? '⏸ odświeżanie wstrzymane — piszesz…' : ''; }
document.addEventListener('focusin', e => { if(e.target && e.target.matches && e.target.matches('textarea,input')) setPaused(true); });
document.addEventListener('focusout', e => { if(e.target && e.target.matches && e.target.matches('textarea,input')) { setPaused(false); refresh(); } });
async function refresh(){ if(refreshPaused) return; try{ await refreshInner(); }catch(e){ document.getElementById('ts').textContent = '⚠ błąd odświeżania: '+e; } }
async function refreshInner(){
  const r = await fetch('/api/state'); const d = await r.json();
  document.getElementById('ts').textContent = new Date().toLocaleTimeString();
  if(d.host){
    document.getElementById('host').innerHTML = d.host.dashboard_url
      ? `📍 LAN: <a href="${esc(d.host.dashboard_url)}" style="color:#58a6ff" target="_blank">${esc(d.host.dashboard_url)}</a> (${esc(d.host.lan_ip)} · ${esc(d.host.hostname)})`
      : `📍 offline (brak IP LAN) · host: ${esc(d.host.hostname)}`;
  }
  const st = d.stats||{};
  document.getElementById('stats').innerHTML =
    `<span class="stat-badge ${st.awaiting?'warn':''}">🕒 czeka na Ciebie: <b>${st.awaiting||0}</b></span>`+
    `<span class="stat-badge">⏳ w kolejce (READY): <b>${st.waiting||0}</b></span>`+
    `<span class="stat-badge ok">🔄 pracują: <b>${st.running||0}</b></span>`+
    `<span class="stat-badge ${st.overdue?'bad':'ok'}">⚠️ spóźnione/stare (&gt;1h w kolejce): <b>${st.overdue||0}</b></span>`+
    `<span class="stat-badge ${st.running_long?'warn':''}">🐌 długo biegną (&gt;1h): <b>${st.running_long||0}</b></span>`+
    (d.sys?`<span class="stat-badge" title="load / CPU / RAM laptopa">🖥️ load <b>${d.sys.load1}</b>/${d.sys.cpus} · 🧠 RAM <b>${d.sys.mem_used_pct}%</b> (wolne ${d.sys.mem_avail_gb}GB)</span>`:'');
  // kolorowe filtry projektów (stan w localStorage — przeżywa odświeżenia); visTasks MUSI być przed użyciem (TDZ!)
  const visTasks = d.tasks.filter(t => !hiddenProjs.has(t.project_id || '(brak)'));
  const allProjs = [...new Set(d.projects.map(p=>p.project_id).concat(d.tasks.map(t=>t.project_id||'(brak)')))];
  document.getElementById('projFilters').innerHTML = allProjs.map(pid => {
    const off = hiddenProjs.has(pid);
    const n = d.tasks.filter(t=>(t.project_id||'(brak)')===pid).length;
    return `<button class="pchip ${off?'off':''}" style="--c:${projColor(pid)}" onclick="toggleProj('${esc(pid)}')"><span class="dot" style="background:${projColor(pid)}"></span>${esc(pid)} <span class="muted">${n}</span></button>`;
  }).join('') + `<button class="pchip reset" onclick="resetProjs()">✖ wszystkie</button>`;
  const alerts = visTasks.filter(t=>t.status==='AWAITING_APPROVAL');
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
  const byStatus = {}; for(const t of visTasks){ (byStatus[t.status] = byStatus[t.status]||[]).push(t); }
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
  const sorted = [...visTasks].sort((a,b)=>(PRIO[b.priority||'P2']-PRIO[a.priority||'P2']) || ((b.status==='RUNNING')-(a.status==='RUNNING')) || ((b.updated_at||0)-(a.updated_at||0)));
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
    const res = t.res ? ` <span class="muted" title="zużycie workera (pid ${t.res.pid})">⚙️${t.res.cpu}% 🧠${t.res.rss_mb}MB</span>` : '';
    const clock = t.status==='RUNNING' && t.started_at ? `<span class="clock" data-started="${t.started_at}">⏱ ${fmtDur(Date.now()/1000 - t.started_at)}</span>${res}` : '';
    tr.innerHTML = `<td>${ic}</td><td>${prio}</td><td style="padding-left:${8+(t.depth||0)*16}px"><b>${indent}${t.id}</b></td><td title="tech: ${esc(t.title||'')}">${esc((t.title_sponsor||t.title||'').slice(0,70))}</td>
      <td class="st">${t.status==='AWAITING_APPROVAL'?(t.substep==='ASK_SPONSOR'?'<span class="badge">❓ ODPOWIEDZ</span> ':'<span class="badge">TWÓJ RUCH</span> ')+t.status:t.status}</td>
      <td class="muted">${esc(t.substep||'')}</td><td>${t.retry_count}</td>
      <td class="muted">${esc(t.persona||t.role||'')}</td><td class="muted"><span class="dot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${projColor(t.project_id||'')}"></span> ${esc(t.project_id||'')}</td>
      <td class="muted">${clock}</td>
      <td class="muted">${t.pr?('!'+t.pr):''}</td><td class="muted">${ago}</td>`;
  }
  document.getElementById('awaitCount').textContent = awaiting ? awaiting+' czeka na Ciebie' : '';
  const navA = document.getElementById('navAlert');
  if(navA) navA.textContent = awaiting ? ' 🚨'+awaiting : '';
  document.getElementById('team').innerHTML = (d.team||[]).map(m => {
    const curRes = m.current && m.current.res ? ` <span class="muted" title="pid ${m.current.res.pid}">⚙️${m.current.res.cpu}% 🧠${m.current.res.rss_mb}MB</span>` : '';
    const cur = m.current ? `<div class="cur">🔨 ${esc(m.current.id)} <span class="clock" data-started="${m.current.started_at||0}">⏱ ${m.current.started_at?fmtDur(Date.now()/1000-m.current.started_at):''}</span> <span class="prio prio-${m.current.priority||'P2'}">${m.current.priority||'P2'}</span>${curRes}</div>` : '<div class="cur muted">— wolny/a —</div>';
    return `<div class="pcard ${m.current?'busy':''}" title="rola: ${m.role}">
      <span class="ava">${m.avatar||'🧑‍💻'}</span> <span class="nm">${esc(m.name)}</span>
      <button class="tbtn" style="float:right" onclick="event.stopPropagation();startThread('${esc(m.role)}')" title="nowy wątek z ${esc(m.name)}">💬</button>
      <div class="pos">${esc(m.position)} · rola: ${esc(m.role)} · ${esc(m.gender)}</div>
      <div class="story">„${esc(m.backstory)}”</div>
      ${cur}
      <div class="stats">zadania: ${m.tasks} · zmergowane: ${m.merged}</div>
      <div class="tenure" title="czas w firmie / przepracowany czas">🏅 w firmie ${fmtDur(m.tenure_s||0)} · ⏱ przepracowane ${fmtDur(m.work_s||0)}</div>
    </div>`;
  }).join('');
  document.getElementById('threads').innerHTML = (d.threads||[]).map(th =>
    `<div class="tcard" onclick="openThread('${esc(th.id)}')">
       <b>${th.persona.avatar||'💬'} ${esc(th.persona.name||th.role)}</b> <span class="muted">${esc(th.role)}</span> — ${esc((th.title||'').slice(0,60))}
       <span class="muted">· ${th.count} wiad.</span>
       ${th.last?`<div class="muted" style="font-size:11px">${new Date(th.last.ts*1000).toLocaleTimeString()} <b>${esc(th.last.author)}</b>: ${esc((th.last.text||'').slice(0,90))}</div>`:''}
     </div>`).join('') || '<span class="muted">brak wątków — kliknij 💬 przy osobie lub napisz na Telegramie</span>';
  document.getElementById('now').innerHTML = nowCards.length ? nowCards.map(t =>
    `<div class="nowcard" onclick="openTask('${t.id}')"><b>${d.icons.RUNNING} ${t.id}</b> <span class="pico">${PIC[t.priority||'P2']||''}</span><span class="prio prio-${t.priority||'P2'}">${t.priority||'P2'}</span>
     <div class="clock" data-started="${t.started_at||0}">⏱ ${t.started_at?fmtDur(Date.now()/1000-t.started_at):''}</div>
     ${t.res?`<div class="muted" title="proces workera pid ${t.res.pid}">⚙️ CPU ${t.res.cpu}% · 🧠 RAM ${t.res.rss_mb}MB</div>`:''}
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
  if(curThread) openThread(curThread, true);
  if(document.getElementById('tab-firma').classList.contains('on')) renderFirma();
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
let detailTab = 'overview';
let curThread = null;
async function openTask(id, keep){
  curTask = id; curThread = null;
  const r = await fetch('/api/task/'+id); const t = await r.json();
  document.getElementById('detail').classList.add('open');
  const d = t.task;
  // guard (review): nie przepisuj panelu przy każdym odświeżeniu — nie kasuje wpisanego komentarza
  const key = 'task|'+id+'|'+(d.status||'')+'|'+(d.substep||'')+'|'+(d.updated_at||0)+'|'+(t.comments||[]).length+'|'+(t.events||[]).length+'|'+detailTab;
  if(keep && document.getElementById('detailBody').dataset.key === key) return;
  document.getElementById('detailBody').dataset.key = key;
  const tabs = `<div class="dtabs">
    <button class="${detailTab==='overview'?'on':''}" onclick="detailTab='overview';openTask(curTask,true)">📋 Przegląd</button>
    <button class="${detailTab==='history'?'on':''}" onclick="detailTab='history';openTask(curTask,true)">🕘 Historia (${(t.events||[]).length})</button>
    <button class="${detailTab==='comments'?'on':''}" onclick="detailTab='comments';openTask(curTask,true)">💬 Komentarze (${(t.comments||[]).length})</button>
  </div>`;
  let body = '';
  if(detailTab === 'history'){
    const nowS = Date.now()/1000;
    body = `<h2>🕘 Historia zmian <span class="muted">(najświeższe na górze — zielone = <60s, niebieskie = <1h)</span></h2>
      <div class="histbox">${(t.events||[]).slice().reverse().map(e=>{
        const age = nowS - (e.ts||0);
        const cls = age<60 ? 'fresh2' : (age<3600 ? 'fresh1' : '');
        const ch = e.changes ? ' · ' + Object.entries(e.changes).map(([k,v])=>`${esc(k)}: ${esc(String(v).slice(0,50))}`).join(' · ') : '';
        return `<div class="${cls}"><span class="muted">${new Date((e.ts||0)*1000).toLocaleTimeString()}</span> ${esc(e.event||((e.from||'?')+' → '+(e.to||'?')))}${ch}${(e.by)?(' <span class="muted">— '+esc(String(e.by).slice(0,60))+'</span>'):''}</div>`;
      }).join('')||'<span class="muted">brak historii</span>'}</div>`;
  } else if(detailTab === 'comments'){
    body = `<h2>💬 Komentarze</h2><div class="threadbox">${(t.comments||[]).map(c=>`<div class="tmsg ${c.author==='sponsor'||c.author==='sponsor/dashboard'?'me':'agent'}"><div class="ta"><b>${esc(c.author)}</b> <span class="muted">${new Date(c.ts*1000).toLocaleTimeString()}</span></div><div class="tt">${esc(c.text.slice(0,400))}</div></div>`).join('')||'<span class="muted">brak komentarzy</span>'}</div>
      <h2>✍️ Twój komentarz</h2>
      <textarea id="cbox" placeholder="uwaga do zadania (też leci na PR na GitHubie)…"></textarea>
      <button onclick="sendComment()">Dodaj komentarz</button>`;
  } else {
    body = `
    <div class="kv"><b>tytuł:</b> ${esc(d.title_sponsor||d.title||'')}</div>
    <div class="kv muted" style="font-size:10px">tech: ${esc(d.title||'')}</div>
    <div class="kv"><b>status:</b> <span class="st st-${d.status}">${esc(d.status)}</span> | <b>substep:</b> ${esc(d.substep||'-')} | <b>retry:</b> ${d.retry_count} | <b>read_only:</b> ${d.read_only?1:0}${d.mode==='llm'?' | <b>🤖 tryb LLM</b>':''} | <b>prio:</b> <span class="prio prio-${d.priority||'P2'}">${esc(d.priority||'P2')}</span></div>
    <div class="kv"><b>rola:</b> ${esc(d.role||'')} → <b>${esc(d.persona||'')}</b> | <b>projekt:</b> ${esc(d.project_id||'')} | <b>milestone:</b> ${esc(d.milestone||'')}</div>
    <div class="kv"><b>przejął:</b> ${esc(d.claimed_by||'-')}${d.started_at?` od ${new Date(d.started_at*1000).toLocaleTimeString()}`:''}${d.elapsed!=null?` (<b>${fmtDur(d.elapsed)}</b>)`:''}</div>
    <div class="kv"><b>rodzic:</b> ${d.parent_task?`<a href="#" onclick="openTask('${esc(d.parent_task)}');return false" style="color:#58a6ff">${esc(d.parent_task)}</a>`:'— (zadanie główne)'}</div>
    <div class="kv"><b>branch:</b> ${esc(d.branch||'-')} | <b>PR:</b> ${d.pr?('!'+d.pr):'-'}</div>
    <div class="kv"><b>scope:</b> ${esc((d.scope_paths||[]).join(', ')||'-')}</div>
    <div class="kv"><b>AC:</b> ${esc((d.acceptance_criteria||'').slice(0,300))}</div>
    <div class="kv"><b>uwagi:</b> ${esc((d.review_notes||'').slice(0,300))}</div>`;
    if(d.status==='AWAITING_APPROVAL'){
      body += `<button class="ok" onclick="approve()">✅ Zaakceptuj → READY</button>
               <button class="no" onclick="decline()">🛑 Odrzuć → BLOCKED</button>`;
    }
  }
  document.getElementById('detailBody').innerHTML = `<h3>${t.icons[d.status]||''} ${esc(d.id)}</h3>` + tabs + body;
}
// ===== wątki rozmów z agentami =====
async function openThread(tid, keep){
  curThread = tid; curTask = null;
  const r = await fetch('/api/thread/'+tid); const t = await r.json();
  if(t.error){ return; }
  document.getElementById('detail').classList.add('open');
  const key = 'thread|'+tid+'|'+(t.updated_at||0)+'|'+(t.messages||[]).length;
  if(keep && document.getElementById('detailBody').dataset.key === key) return;
  document.getElementById('detailBody').dataset.key = key;
  const p = t.persona || {};
  const msgs = (t.messages||[]).map(m=>{
    const me = m.author==='sponsor';
    const who = me ? '🧑‍💼 Sponsor' : (m.author==='system' ? '⚙️ system' : `${p.avatar||'🧑‍💻'} ${esc(p.name||m.author)} (${esc(t.role)})`);
    return `<div class="tmsg ${me?'me':'agent'}"><div class="ta">${who} <span class="muted">${new Date(m.ts*1000).toLocaleTimeString()}</span></div><div class="tt">${esc(m.text)}</div></div>`;
  }).join('');
  const lastSponsor = (t.messages||[]).length && t.messages[t.messages.length-1].author==='sponsor'
    && (Date.now()/1000 - t.messages[t.messages.length-1].ts) < 120;
  document.getElementById('detailBody').innerHTML = `<h3>💬 ${esc(p.avatar||'')} ${esc(p.name||t.role)} <span class="muted">${esc(p.position||'')} · rola ${esc(t.role)}</span></h3>
    <div class="kv muted">${esc(t.title||'')}</div>
    <div class="threadbox" id="threadbox">${msgs||'<span class="muted">początek wątku — napisz pierwsza wiadomość</span>'}</div>
    ${lastSponsor?'<div class="muted" style="font-size:11px">🤖 '+esc(p.name||t.role)+' pisze odpowiedź…</div>':''}
    <textarea id="tbox" placeholder="napisz do ${esc(p.name||t.role)}…"></textarea>
    <button onclick="sendThreadMsg()">Wyślij ✈</button>`;
  const tb = document.getElementById('threadbox'); if(tb) tb.scrollTop = tb.scrollHeight;
}
async function startThread(role){
  const p = prompt('O co zapytasz osobę z roli: ' + role + ' ?');
  if(!p) return;
  const r = await fetch('/api/threads/start', {method:'POST', body: JSON.stringify({role, text: p})});
  const j = await r.json();
  detailTab = 'overview';
  openThread(j.thread_id);
  refresh();
}
async function sendThreadMsg(){
  const txt = document.getElementById('tbox').value.trim(); if(!txt || !curThread) return;
  document.getElementById('tbox').value = '';
  await fetch('/api/thread/'+curThread+'/post', {method:'POST', body: JSON.stringify({text: txt})});
  refresh();
  setTimeout(() => openThread(curThread, true), 1200);
  setTimeout(() => openThread(curThread, true), 6000);
}
function closeDetail(){ document.getElementById('detail').classList.remove('open'); curTask=null; curThread=null; }
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
// zakładki: stan w URL hash (#tasks), przeżywa odświeżenie; nav jest sticky
const TABS = ['overview','tasks','team','chat','live','firma'];
function showTab(name, push){
  if(!TABS.includes(name)) name = 'overview';
  document.querySelectorAll('.tabpage').forEach(el => el.classList.toggle('on', el.id === 'tab-'+name));
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  if(push !== false) history.replaceState(null, '', '#'+name);
  if(name === 'firma') renderFirma();
}
// ===== FIRMA: widok świata (osiedle biur, SVG top-down, animacje przepływu) =====
const SVGNS = 'http://www.w3.org/2000/svg';
let firmaWorld = null;       // ostatni /api/firma
let firmaView = null;        // {x,y,w,h} viewBox (pan/zoom)
let deskEls = {};            // taskId -> <g> (persistent — żeby CSS transition animował przejazd)
function svgEl(name, attrs, parent){
  const e = document.createElementNS(SVGNS, name);
  for(const k in attrs) e.setAttribute(k, attrs[k]);
  if(parent) parent.appendChild(e);
  return e;
}
function escXml(s){ return esc(s); }
async function renderFirma(){
  try{
    const r = await fetch('/api/firma'); firmaWorld = await r.json();
  }catch(e){ return; }
  const W = firmaWorld, cfg = W.config, C = cfg.colors;
  const g = cfg.estate.grid, off = cfg.office, R = cfg.rooms, D = cfg.desks;
  const cols = Math.max(1, g.cols|0), gap = g.gap, margin = g.margin;
  const ow = off.w, oh = off.h;
  const visOffices = W.offices.filter(o => !hiddenProjs.has(o.project_id));
  const rows = Math.max(1, Math.ceil(visOffices.length / cols));
  const Wsvg = margin*2 + cols*ow + (cols-1)*gap;
  const Hsvg = margin*2 + rows*oh + (rows-1)*gap + 90;
  if(!firmaView) firmaView = {x:0, y:0, w:Wsvg, h:Hsvg};
  const svg = document.getElementById('estate');
  svg.setAttribute('viewBox', `${firmaView.x} ${firmaView.y} ${firmaView.w} ${firmaView.h}`);
  // warstwa statyczna (tło, działki, biura, pokoje, strefy, recepcja)
  const stLayer = svgEl('g', {}, svg); stLayer.id = 'estateStatic';
  svgEl('rect', {x:0, y:0, width:Wsvg, height:Hsvg, fill:C.bg}, stLayer);
  // etap 2: defs — gradienty per biuro + cień
  const s2 = cfg.stage2 || {};
  const defs = svgEl('defs', {}, stLayer);
  if(s2.gradients){
    for(const o of visOffices){
      const gid = 'grad-' + String(o.project_id).replace(/[^a-z0-9_-]/gi, '_');
      const lg = svgEl('linearGradient', {id: gid, x1: '0', y1: '0', x2: '0', y2: '1'}, defs);
      svgEl('stop', {offset: '0%', 'stop-color': o.color, 'stop-opacity': 0.55}, lg);
      svgEl('stop', {offset: '100%', 'stop-color': C.plot, 'stop-opacity': 1}, lg);
    }
  }
  if(s2.shadows){
    const f = svgEl('filter', {id: 'fshadow', x: '-20%', y: '-20%', width: '140%', height: '140%'}, defs);
    svgEl('feDropShadow', {dx: 0, dy: 3, stdDeviation: 4, 'flood-color': '#000', 'flood-opacity': 0.5}, f);
  }
  // drogi z przerywaną linią środkową (etap 2)
  for(let i=1;i<cols;i++){ const x = margin + i*ow + (i-0.5)*gap - cfg.estate.road/2;
    svgEl('rect', {x, y:0, width:cfg.estate.road, height:Hsvg, fill:C.road}, stLayer);
    if(s2.roadDashes){
      const cx = x + cfg.estate.road/2;
      for(let y=10; y<Hsvg; y+=28) svgEl('line', {x1:cx, y1:y, x2:cx, y2:y+14, stroke:'#8b949e', 'stroke-width':2, opacity:0.5}, stLayer);
    }
  }
  // drzewa na osiedlu (etap 2) — deterministyczne pozycje
  if(s2.trees){
    for(let i=0;i<(s2.treeCount||6);i++){
      const tx = margin/2 + ((i*97) % Math.max(1, Wsvg - margin));
      const ty = margin/2 + ((i*61) % Math.max(1, Hsvg - margin));
      const t = svgEl('text', {x:tx, y:ty, 'font-size':18, opacity:0.8}, stLayer);
      t.textContent = s2.treeEmoji || '🌳';
    }
  }
  // recepcja (brama osiedla) = host/LAN
  const gate = cfg.estate.gate, gx = Wsvg/2 - gate.w/2, gy = Hsvg - margin - gate.h + 16;
  const gg = svgEl('g', {}, stLayer);
  svgEl('rect', {x:gx, y:gy, width:gate.w, height:gate.h, rx:8, fill:C.gate, opacity:0.92}, gg);
  const t1 = svgEl('text', {x:gx+gate.w/2, y:gy+17, 'text-anchor':'middle', fill:'#fff', 'font-size':12}, gg);
  t1.textContent = '🚪 ' + gate.label;
  const t2 = svgEl('text', {x:gx+gate.w/2, y:gy+36, 'text-anchor':'middle', fill:'#fff', 'font-size':11}, gg);
  t2.textContent = (W.estate.host && W.estate.host.dashboard_url) || 'offline';
  const t3 = svgEl('text', {x:gx+gate.w/2, y:gy+53, 'text-anchor':'middle', fill:'#cfe3ff', 'font-size':10}, gg);
  t3.textContent = W.estate.sys ? `load ${W.estate.sys.load1}/${W.estate.sys.cpus} · 🧠 ${W.estate.sys.mem_used_pct}%` : '';
  // pogoda nad osiedlem = sys
  const wth = svgEl('text', {x:cfg.estate.weather.x, y:cfg.estate.weather.y, 'font-size':12, fill:C.muted}, stLayer);
  wth.textContent = `🌥️ ${cfg.estate.weather.label}: load ${W.estate.sys?W.estate.sys.load1:'?'} · RAM ${W.estate.sys?W.estate.sys.mem_used_pct+'%':'?'}`;
  // biura
  visOffices.forEach((o, oi) => {
    const col = oi % cols, row = Math.floor(oi / cols);
    const ox = margin + col*(ow+gap), oy = margin + row*(oh+gap);
    const og = svgEl('g', {cursor:'pointer', class:'office'}, stLayer);
    og.addEventListener('click', () => toggleProj(o.project_id));
    const gid = 'grad-' + String(o.project_id).replace(/[^a-z0-9_-]/gi, '_');
    svgEl('rect', {x:ox, y:oy, width:ow, height:oh, rx:10, class:'plot',
                   fill: (s2.gradients ? `url(#${gid})` : C.plot),
                   stroke:o.color, 'stroke-width':2.5,
                   filter: s2.shadows ? 'url(#fshadow)' : null}, og);
    svgEl('rect', {x:ox, y:oy, width:ow, height:off.roofH, rx:10, fill:C.roof, stroke:o.color}, og);
    // etap 2: okna w dachu + drzwi od frontu
    if(s2.windows){
      for(let wi=0; wi<3; wi++)
        svgEl('rect', {x:ox+off.w-70+wi*18, y:oy+10, width:12, height:14, rx:2, fill:'#0d1117', stroke:o.color, 'stroke-width':1, opacity:0.9}, og);
    }
    if(s2.door)
      svgEl('rect', {x:ox+ow/2-12, y:oy+oh-14, width:24, height:14, rx:3, fill:o.color, opacity:0.85}, og);
    svgEl('rect', {x:ox, y:oy+off.roofH-6, width:ow, height:6, fill:o.color}, og);
    const rt = svgEl('text', {x:ox+10, y:oy+23, 'font-size':13, 'font-weight':'bold', fill:C.text}, og);
    rt.textContent = '🏢 ' + o.project_id;
    const rt2 = svgEl('text', {x:ox+ow-10, y:oy+23, 'text-anchor':'end', 'font-size':10, fill:C.muted}, og);
    rt2.textContent = o.read_only ? '🚧 read-only' : '🔓 ' + (o.policy||'');
    if(o.read_only) for(let t=0;t<8;t++)
      svgEl('line', {x1:ox+8+t*28, y1:oy+off.roofH, x2:ox+26+t*28, y2:oy+off.roofH+16, stroke:C.tape, 'stroke-width':3, opacity:0.7}, og);
    // gabinet kierownika + trofea (milestony)
    const mz = cfg.zones.manager, mx = ox+mz.rect[0], my = oy+mz.rect[1];
    const mg = svgEl('g', {}, og);
    svgEl('rect', {x:mx, y:my, width:mz.rect[2], height:mz.rect[3], rx:6, fill:C.room, stroke:C.roomStroke}, mg);
    const mt = svgEl('text', {x:mx+mz.rect[2]/2, y:my+12, 'text-anchor':'middle', 'font-size':9, fill:C.muted}, mg);
    mt.textContent = '🏆 kierownik';
    o.milestones.slice(0,4).forEach((m, mi) => {
      const tx = mx+6+mi*(cfg.trophy.w+cfg.trophy.gap), ty = my+20;
      const tg = svgEl('g', {}, mg);
      const tt = svgEl('title', {}, tg); tt.textContent = `${m.id} — ${m.status}`;
      svgEl('rect', {x:tx, y:ty, width:cfg.trophy.w, height:cfg.trophy.h, rx:4,
                     fill: m.status==='CLOSED' ? '#d29922' : '#21262d', stroke:C.roomStroke}, tg);
      const ti = svgEl('text', {x:tx+cfg.trophy.w/2, y:ty+19, 'text-anchor':'middle', 'font-size':13}, tg);
      ti.textContent = m.status==='CLOSED' ? '🏆' : '🔨';
    });
    // pokoje agentów
    o.rooms.forEach((room, ri) => {
      const rx = ox + R.x0 + (ri % R.perRow) * (R.w + R.gap);
      const ry = oy + R.y0 + Math.floor(ri / R.perRow) * (R.h + R.gap);
      if(ry + R.h > oy + cfg.zones.alarm.rect[1] - 8) return; // nie wchodzimy w strefy
      const busy = !!room.current;
      const rg = svgEl('g', {}, og);
      svgEl('rect', {x:rx, y:ry, width:R.w, height:R.h, rx:8, fill:C.room,
                     stroke: busy ? '#1f6feb' : C.roomStroke, 'stroke-width': busy?2:1}, rg);
      const a = svgEl('text', {x:rx+8, y:ry+16, 'font-size':14}, rg); a.textContent = room.avatar;
      const n = svgEl('text', {x:rx+26, y:ry+15, 'font-size':10, 'font-weight':'bold', fill:C.text}, rg); n.textContent = room.name;
      const p = svgEl('text', {x:rx+8, y:ry+26, 'font-size':8, fill:C.muted}, rg); p.textContent = room.position;
      if(room.res){ const rr = svgEl('text', {x:rx+R.w-8, y:ry+15, 'text-anchor':'end', 'font-size':8, fill:C.muted}, rg);
        rr.textContent = `⚙️${room.res.cpu}% 🧠${room.res.rss_mb}MB`; }
      const b = svgEl('text', {x:rx+8, y:ry+R.h-8, 'font-size':9, fill: busy ? '#58a6ff' : C.muted}, rg);
      b.textContent = busy ? `🔨 ${room.current.id} ⏱` : `wolny/a · zadań: ${room.taskCount}`;
      room._x = rx; room._y = ry;
    });
    // strefy biura
    for(const [zk, z] of Object.entries(cfg.zones)){
      if(zk === 'manager' || zk === 'desk') continue;
      const zx = ox+z.rect[0], zy = oy+z.rect[1];
      const zg = svgEl('g', {class: zk==='alarm' && o.desks.some(d=>d.zone==='alarm') ? 'alarmzone' : ''}, og);
      svgEl('rect', {x:zx, y:zy, width:z.rect[2], height:z.rect[3], rx:6, fill:'none', stroke:C.roomStroke, 'stroke-dasharray':'4 3'}, zg);
      const zt = svgEl('text', {x:zx+4, y:zy+11, 'font-size':8, fill:C.muted}, zg); zt.textContent = z.label;
    }
    o._ox = ox; o._oy = oy;
  });
  // legenda statusów
  document.getElementById('firmaLegend').innerHTML = Object.entries(cfg.statusStyle)
    .map(([k,v]) => `<span class="stat-badge" style="border-color:${v.color}">${v.icon} ${k}</span>`).join('');
  // warstwa biurek (persistent — animacje przejazdu)
  let deskLayer = document.getElementById('deskLayer');
  if(!deskLayer){ deskLayer = svgEl('g', {id:'deskLayer'}, svg); }
  const stCfg = cfg.statusStyle, ps = cfg.prioScale;
  const seen = new Set();
  for(const o of visOffices){
    const ox = o._ox, oy = o._oy;
    const byZone = {};
    for(const d of o.desks){ (byZone[d.zone] = byZone[d.zone]||[]).push(d); }
    for(const [zone, list] of Object.entries(byZone)){
      list.forEach((d, i) => {
        seen.add(d.id);
        const sty = stCfg[d.status] || stCfg.READY;
        const scale = ps[d.priority] || 1;
        const dw = D.w*scale, dh = D.h*scale;
        let x, y;
        if(zone === 'desk'){
          const room = o.rooms.find(r => r.role === d.role) || o.rooms[0];
          if(room && room._x !== undefined){
            const perRow = Math.max(1, Math.floor((R.w-12)/(D.w+D.gap)));
            x = room._x + 6 + (i % perRow)*(D.w+D.gap);
            y = room._y + R.headerH + 4 + Math.floor(i/perRow)*(D.h+D.gap);
          } else { x = ox+20; y = oy+60; }
        } else {
          const z = cfg.zones[zone]; if(!z) return;
          const zx = ox+z.rect[0], zy = oy+z.rect[1];
          const perRow = Math.max(1, Math.floor((z.rect[2]-10)/(D.w+D.gap)));
          x = zx + 5 + (i % perRow)*(D.w+D.gap);
          y = zy + 16 + Math.floor(i/perRow)*(D.h+D.gap);
        }
        x += (D.w-dw)/2; y += (D.h-dh)/2;
        let gEl = deskEls[d.id];
        if(!gEl){
          gEl = svgEl('g', {class:'desk', cursor:'pointer'}, deskLayer);
          const title = svgEl('title', {}, gEl);
          svgEl('rect', {width:dw, height:dh, rx:4, fill:sty.color, opacity:0.92, stroke:'#0d1117'}, gEl);
          const ic = svgEl('text', {x:dw/2, y:dh/2+4, 'text-anchor':'middle', 'font-size':Math.round(10*scale)}, gEl);
          ic.textContent = sty.icon;
          const lb = svgEl('text', {x:dw/2, y:dh+9, 'text-anchor':'middle', 'font-size':8, fill:C.text}, gEl);
          lb.textContent = d.id;
          gEl.addEventListener('click', (ev) => { ev.stopPropagation(); openTask(d.id); });
          deskEls[d.id] = gEl;
          // fade-in: start bez transition, pozycja początkowa, potem włącz transition
          gEl.style.transition = 'none';
          gEl.style.transform = `translate(${x}px,${y}px)`;
          gEl.style.opacity = '0';
          requestAnimationFrame(() => requestAnimationFrame(() => {
            gEl.style.transition = `transform ${cfg.anim.moveMs}ms ease, opacity ${cfg.anim.fadeMs}ms`;
            gEl.style.opacity = '1';
          }));
        } else {
          title.textContent = `${d.id} · ${d.status} · ${d.title_sponsor||d.title||''}`;
          gEl.style.transition = `transform ${cfg.anim.moveMs}ms ease, opacity ${cfg.anim.fadeMs}ms`;
          gEl.style.transform = `translate(${x}px,${y}px)`;  // zmiana strefy -> płynny przejazd
        }
      });
    }
  }
  // usuń biurka znikniętych zadań (fade-out)
  for(const id of Object.keys(deskEls)){
    if(!seen.has(id)){
      const gEl = deskEls[id];
      gEl.style.transition = `opacity ${cfg.anim.fadeMs}ms`;
      gEl.style.opacity = '0';
      setTimeout(() => { gEl.remove(); }, cfg.anim.fadeMs + 50);
      delete deskEls[id];
    }
  }
}
// pan/zoom mapy osiedla
function initFirmaPanZoom(){
  const svg = document.getElementById('estate');
  if(svg.dataset.pz) return; svg.dataset.pz = '1';
  let drag = null;
  svg.addEventListener('wheel', (e) => {
    if(!firmaView) return;
    e.preventDefault();
    const f = e.deltaY > 0 ? 1.15 : 1/1.15;
    const r = svg.getBoundingClientRect();
    const mx = firmaView.x + (e.clientX - r.left) / r.width * firmaView.w;
    const my = firmaView.y + (e.clientY - r.top) / r.height * firmaView.h;
    firmaView.w = Math.min(4000, Math.max(200, firmaView.w * f));
    firmaView.h = firmaView.w * (r.height / r.width) * (firmaView.h/firmaView.w) * (firmaView.w/(firmaView.w*f)); // zachowaj proporcje
    firmaView.h = firmaView.w * (r.height / r.width);
    firmaView.x = mx - (e.clientX - r.left) / r.width * firmaView.w;
    firmaView.y = my - (e.clientY - r.top) / r.height * firmaView.h;
    svg.setAttribute('viewBox', `${firmaView.x} ${firmaView.y} ${firmaView.w} ${firmaView.h}`);
  }, {passive:false});
  svg.addEventListener('mousedown', (e) => { drag = {x:e.clientX, y:e.clientY, vx:firmaView.x, vy:firmaView.y}; });
  window.addEventListener('mousemove', (e) => {
    if(!drag || !firmaView) return;
    const r = svg.getBoundingClientRect();
    firmaView.x = drag.vx - (e.clientX - drag.x) / r.width * firmaView.w;
    firmaView.y = drag.vy - (e.clientY - drag.y) / r.height * firmaView.h;
    svg.setAttribute('viewBox', `${firmaView.x} ${firmaView.y} ${firmaView.w} ${firmaView.h}`);
  });
  window.addEventListener('mouseup', () => { drag = null; });
}
initFirmaPanZoom();
window.addEventListener('hashchange', () => showTab(location.hash.slice(1)||'overview', false));
showTab(location.hash.slice(1)||'overview', false);
refresh(); setInterval(() => { if(!refreshPaused) refresh(); }, 2000);
setInterval(() => { if (refreshPaused) return; const v = document.getElementById('viewer'); if (liveOn && v.dataset.url) view(v.dataset.url); }, 2000);
// zegarki tykają co sekundę — widać że coś się dzieje
setInterval(() => {
  document.querySelectorAll('.clock[data-started]').forEach(el => {
    const s = parseFloat(el.dataset.started); if(s > 0) el.textContent = '⏱ ' + fmtDur(Date.now()/1000 - s);
  });
}, 1000);
const _view = view; view = function(u){ document.getElementById('viewer').dataset.url = u; return _view(u); };
</script></body></html>"""


def host_info(port: int = 8765) -> dict:
    """Adres IP hosta w LAN (UDP-socket trick — bez ruchu sieciowego, bez hardcoded IP)
    + URL dashboardu. Offline -> lan_ip=None (explicit null)."""
    import socket
    ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            ip = None
    return {"lan_ip": ip, "port": port,
            "dashboard_url": f"http://{ip}:{port}" if ip else None,
            "hostname": socket.gethostname()}


def _proc_stats(claimed_by) -> dict | None:
    """Zużycie zasobów workera z /proc: średni CPU% od startu procesu + RSS MB.
    Lekki odczyt (bez ps), None gdy proces już nie żyje."""
    m = re.search(r"(\d+)$", str(claimed_by or ""))
    if not m:
        return None
    pid = int(m.group(1))
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().rsplit(")", 1)[1].split()
        utime, stime, starttime = int(parts[11]), int(parts[12]), int(parts[19])
        clk = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        elapsed = max(1.0, uptime - starttime / clk)
        cpu = round(100.0 * (utime + stime) / clk / elapsed, 1)
        rss_mb = 0.0
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    rss_mb = round(int(line.split()[1]) / 1024, 0)
                    break
        return {"pid": pid, "cpu": cpu, "rss_mb": rss_mb}
    except (OSError, IndexError, ValueError):
        return None


def _sys_stats() -> dict:
    """Wskaźniki zasobów całego laptopa: load1, liczba CPU, użycie RAM."""
    load1 = os.getloadavg()[0]
    mem = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    mem[k] = int(v.split()[0])
    except OSError:
        pass
    total = mem.get("MemTotal", 1)
    avail = mem.get("MemAvailable", 0)
    return {"load1": round(load1, 2), "cpus": os.cpu_count() or 0,
            "mem_used_pct": round(100.0 * (total - avail) / total, 1),
            "mem_avail_gb": round(avail / 1024 / 1024, 2)}


def snapshot() -> dict:
    # State() jest tanie (współdzielone conn per proces) — nie zamykamy per request
    d = _snapshot(State())
    d["host"] = host_info()
    d["sys"] = _sys_stats()
    return d


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
        if t.get("status") == "RUNNING":
            tasks[-1]["res"] = _proc_stats(t.get("claimed_by"))
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
                     "current": ({"id": cur["id"], "started_at": cur.get("started_at"), "priority": cur.get("priority"),
                                  "res": _proc_stats(cur.get("claimed_by"))}
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
        # badge „czas w firmie / przepracowane” per rola
        now = time.time()
        for m in team:
            role = m["role"]
            rtasks = [t for t in raw if (t.get("role") or "worker") == role]
            first_seen = min((t.get("created_at") or now) for t in rtasks) if rtasks else now
            work_s = float(s.get_kv(f"work_s:{role}", 0) or 0)
            if m.get("current") and m["current"].get("started_at"):
                work_s += max(0.0, now - float(m["current"]["started_at"]))
            m["tenure_s"] = max(0.0, now - first_seen)
            m["work_s"] = work_s
        from engine import team_threads
        return {"icons": ICON, "projects": projs, "tasks": tasks, "events": events, "team": team, "asks": asks,
                "stats": stats,
                "comments": s.recent_comments(40), "metrics": metrics,
                "runtime": runtime, "logs": logs,
                "milestones": s.list_milestones(),
                "threads": team_threads.list_threads(s, limit=15)}


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
            # redirect 127.0.0.1/localhost -> adres LAN (żeby przeglądarka sama przeszła na http://<lan_ip>:8765)
            host_hdr = (self.headers.get("Host") or "").split(":")[0].strip("[]")
            port = self.server.server_address[1]
            lan = host_info(port).get("lan_ip")
            if lan and host_hdr in ("127.0.0.1", "localhost", "::1"):
                self.send_response(302)
                self.send_header("Location", f"http://{lan}:{port}/")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif u.path == "/api/state":
            self._send(200, json.dumps(snapshot(), ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        elif u.path == "/api/firma":
            # FIRMA-002: świat „firma” (osiedle→biura→pokoje→biurka) + config widoku
            from engine.firma.view import build_world
            self._send(200, json.dumps(build_world(snapshot(), State()), ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        elif u.path == "/api/threads":
            from engine import team_threads
            role = parse_qs(u.query).get("role", [None])[0]
            self._send(200, json.dumps(team_threads.list_threads(State(), role=role), ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        elif u.path.startswith("/api/thread/"):
            from engine import team_threads
            tid = u.path.rsplit("/", 1)[-1]
            th = team_threads.get_thread(State(), tid)
            self._send(200 if th else 404,
                       json.dumps(th or {"error": "nie ma takiego wątku"}, ensure_ascii=False, default=str).encode(),
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
        if u.path == "/api/threads/start":
            # nowy wątek z konkretnym agentem (rola) + pierwsza wiadomość Sponsora + asynchroniczna odpowiedź
            from engine import team_threads
            role = str(body.get("role") or "principal-debugger")
            text = str(body.get("text", "")).strip()
            if not text:
                self._send(400, b'{"error":"pusty tekst"}', "application/json")
                return
            s = State()
            th = team_threads.ensure_thread(s, role, text[:80])
            team_threads.add_message(s, th["id"], "sponsor", text, kind="msg")
            team_threads.sponsor_post_async(State, th["id"], text)  # agent odpowie w tle
            self._send(200, json.dumps({"ok": True, "thread_id": th["id"], "role": role}).encode(),
                       "application/json")
            return
        if len(parts) == 4 and parts[:2] == ["api", "thread"] and parts[3] == "post":
            # kontynuacja wątku z dashboardu
            from engine import team_threads
            tid = parts[2]
            text = str(body.get("text", "")).strip()
            if not text:
                self._send(400, b'{"error":"pusty tekst"}', "application/json")
                return
            s = State()
            team_threads.add_message(s, tid, "sponsor", text, kind="msg")
            team_threads.sponsor_post_async(State, tid, text)
            self._send(200, b'{"ok":true}', "application/json")
            return
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
    ap.add_argument("--host", default="0.0.0.0")  # 0.0.0.0 = dostęp też z LAN (http://<lan_ip>:8765)
    args = ap.parse_args()
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"🏭 Dashboard: http://{args.host}:{args.port}  (Ctrl+C stop)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
