"""Live Dashboard — podgląd zadań na żywo w przeglądarce (read-only + akcje Sponsora):

  python -m engine.dashboard [--port 8765] [--host 0.0.0.0]  ->  http://<lan_ip>:8765 (z 127.0.0.1 auto-redirect)

Funkcje:
  - tabela zadań: klik w wiersz = szczegóły zadania (panel boczny)
  - zadania AWAITING_OWNER są PODŚWIETLONE (pulsują żółte) — to czeka na Ciebie
  - w szczegółach: komentarze agentów, pole do komentarza Sponsora,
    przyciski ✅ Zaakceptuj / 🛑 Odrzuć (dla AWAITING_OWNER), link PR na GitHubie
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

ICON = {"READY": "⏳", "RUNNING": "🔄", "IN_REVIEW": "🔍", "CHANGES_REQUESTED": "🛠️", "AWAITING_OWNER": "🧑‍💼",
        "READY_TO_MERGE": "📦", "MERGED": "✅", "FAILED": "❌", "BLOCKED": "⛔", "ESCALATED": "🚨"}

VERSION = "2.6.4"


def _version_info() -> dict:
    """Wersja dashboardu + ostatni commit + changelog (do badge'a w nagłówku)."""
    import subprocess
    last_hash, commit_ts = "", None
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%h|%ct|%s"], capture_output=True, text=True, cwd=ROOT)
        if out.returncode == 0:
            last_hash, ts, subj = out.stdout.strip().split("|", 2)
            commit_ts = float(ts)
    except Exception:
        pass
    changelog = ""
    try:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")[:4000]
    except OSError:
        changelog = "(brak CHANGELOG.md)"
    return {"version": VERSION, "last_commit": last_hash, "commit_ts": commit_ts,
            "commit_subject": subj if last_hash else "", "changelog": changelog,
            "age_s": (time.time() - commit_ts) if commit_ts else None}


def _git_merges(limit: int = 60) -> list[dict]:
    """Merge z git log (--merges) — źródło prawdy także dla direct merge Sponsora
    („sponsor: direct merge, no PR”), które NIE przechodzą przez metryki flow."""
    import subprocess
    out_list: list[dict] = []
    try:
        out = subprocess.run(["git", "log", "--merges", f"--format=%ct|%h|%s", f"-{limit}"],
                             capture_output=True, text=True, cwd=ROOT)
        if out.returncode == 0:
            for line in out.stdout.strip().splitlines():
                parts = line.split("|", 2)
                if len(parts) == 3:
                    ts, h, subj = float(parts[0]), parts[1], parts[2]
                    task_id = ""
                    m = re.search(r"([A-Z]+-\d+[a-z]?)", subj)
                    if m:
                        task_id = m.group(1)
                    out_list.append({"ts": ts, "hash": h, "subject": subj[:120],
                                     "task_id": task_id, "kind": "git_merge"})
    except Exception:
        pass
    return out_list


def _merges_info(s: State) -> dict:
    """Ostatnie merge'e: ile w ostatniej dobie, kiedy ostatni, lista ostatnich.
    Źródła: metryki flow (merge/auto_merge/MERGED) + git log --merges (direct merge)."""
    now = time.time()
    seen: set = set()
    all_m: list[dict] = []
    try:
        rows = s._conn().execute(
            "SELECT ts, task_id, kind, payload FROM metrics WHERE kind IN ('merge','auto_merge','task_event') "
            "ORDER BY ts DESC LIMIT 500").fetchall()
        for ts, task_id, kind, payload in rows:
            try:
                p = json.loads(payload)
            except Exception:
                p = {}
            st = str((p.get("changes") or {}).get("status", ""))
            if kind in ("merge", "auto_merge") or "MERGED" in st:
                key = (round(ts, 1), task_id or kind)
                if key in seen:
                    continue
                seen.add(key)
                all_m.append({"ts": ts, "task_id": task_id or (p.get("task") or ""),
                              "kind": kind, "source": "flow"})
    except Exception:
        pass
    for g in _git_merges():
        key = (round(g["ts"], 1), g.get("hash") or "")
        if key in seen:
            continue
        seen.add(key)
        all_m.append({**g, "source": "git"})
    all_m.sort(key=lambda m: -m["ts"])
    recent = all_m[:10]
    count_24h = sum(1 for m in all_m if now - m["ts"] < 86400)
    last_ts = all_m[0]["ts"] if all_m else None
    return {"last_ts": last_ts, "last_ago_s": (now - last_ts) if last_ts else None,
            "count_24h": count_24h, "recent": recent}

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
 .ic-AWAITING_OWNER{animation:pulseIco 1.2s infinite;color:#b08800}
.ic-CHANGES_REQUESTED{color:#d29922}
 .ic-FAILED,.ic-ESCALATED{color:#f85149;animation:pulseIco .8s infinite}
 .ic-MERGED{color:#3fb950}
 .ic-READY{color:#8b949e}
 @keyframes spin{to{transform:rotate(360deg)}}
 @keyframes pulseIco{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.55;transform:scale(1.25)}}
 tr.running td{background:#0d2137}
 tr.running{box-shadow:inset 3px 0 0 #1f6feb}
 /* kolory wierszy wg statusu — status widać od razu */
 tr.st-AWAITING_OWNER td{background:#3d3000}
tr.st-CHANGES_REQUESTED td{background:#2e2410}
 tr.st-RUNNING td{background:#0d2137}
 tr.st-IN_REVIEW td{background:#241b38}
 tr.st-READY_TO_MERGE td{background:#0d3038}
 tr.st-MERGED td{background:#0d2b1a;color:#6e7681}
 tr.st-FAILED td,tr.st-ESCALATED td{background:#3d1215}
 tr.st-BLOCKED td{background:#2b1113;color:#6e7681}
 tr.st-READY td{background:#11151c}
 .chip{border-left:3px solid #30363d}
 .chip.st-RUNNING{border-left-color:#58a6ff}
 .chip.st-AWAITING_OWNER{border-left-color:#b08800}
.chip.st-CHANGES_REQUESTED{border-left-color:#d29922}
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
#projFilters{display:flex;flex-wrap:wrap;gap:6px;margin:2px 0 6px;padding:5px 8px;background:#111823;border:1px solid #21262d;border-radius:10px;position:sticky;top:0;z-index:6}
#stFilters{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 6px;padding:5px 8px;background:#161b22;border:1px solid #21262d;border-radius:10px}
#flowStageFilters{display:flex;flex-wrap:wrap;gap:6px;width:100%;margin-top:4px}
.lchip{border-radius:12px;padding:2px 10px;font-size:11px;font-weight:bold;opacity:.55}
.lchip.on{opacity:1;outline:2px solid #fff}
#ver:hover{border-color:#58a6ff;color:#58a6ff}
.pchip{border:1px solid var(--c,#30363d);background:#161b22;color:#c9d1d9;border-radius:999px;padding:3px 10px;font-size:12px;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.pchip .dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.pchip.off{opacity:.35;text-decoration:line-through}
.pchip.reset{border-color:#30363d;color:#8b949e}
.pchip:active{transform:scale(.96)}
.sbadge,.pbadge{color:#fff!important;font-weight:bold;border-radius:6px;padding:1px 8px;font-size:10px;display:inline-block;letter-spacing:.3px;text-shadow:0 1px 1px #0008;white-space:nowrap}
.pbadge{box-shadow:0 1px 3px #0006}
.sbadge.esc{animation:pulseIco 1.2s infinite}
.pchip{background:var(--c,#30363d);color:#fff!important;border-color:transparent}
.pchip .muted{color:#ffffffcc!important}
.pchip.off{background:#21262d!important;color:#8b949e!important;text-decoration:line-through}
.pchip.off .muted{color:#8b949e!important}
.pchip.reset{background:#21262d!important;color:#8b949e!important}
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
 .stat-badge.bad{border-color:#f85149;color:#f85149;animation:pulseIco 1s 8} /* ograniczone miganie */
 .stat-badge.ok{border-color:#3fb950;color:#3fb950}
 #alerts{display:flex;gap:8px;flex-wrap:wrap}
 .alert-card{background:#3d1215;border:1px solid #f85149;border-radius:8px;padding:8px 12px;max-width:340px;cursor:pointer;animation:pulseIco 1.4s 7} /* miga tylko ~10s, potem spokojna ramka */
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
 /* 🎉 nowa osoba w firmie — banner + toast */
#newHires{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0}
.hire-card{background:#0d2b1a;border:1px solid #3fb950;border-radius:10px;padding:8px 14px;display:flex;gap:10px;align-items:center;cursor:pointer;animation:hirePop .5s ease-out}
.hire-card:hover{filter:brightness(1.2)}
@keyframes hirePop{from{transform:scale(.7);opacity:0}to{transform:scale(1);opacity:1}}
.hire-card .ava{font-size:26px}
#toast{position:fixed;bottom:18px;right:18px;background:#0d2b1a;border:1px solid #3fb950;color:#c9d1d9;padding:12px 18px;border-radius:12px;font-size:13px;z-index:99;opacity:0;transform:translateY(20px);transition:all .3s;pointer-events:none;max-width:340px;box-shadow:0 4px 16px #000a}
#toast.show{opacity:1;transform:none}
/* 🔄 banner "nowa wersja dashboardu — odśwież Ctrl+R" */
#updBanner{position:fixed;top:10px;left:50%;transform:translateX(-50%);background:#3d3000;border:1px solid #d29922;color:#f0b429;padding:10px 18px;border-radius:12px;z-index:100;font-size:13px;display:none;box-shadow:0 4px 16px #000a;animation:pulseIco 1.4s 8}
#updBanner.show{display:block}
#updBanner button{margin-left:10px;background:#d29922;color:#000;border:0;border-radius:8px;padding:5px 12px;font-weight:bold;cursor:pointer}
/* 🎯 Tematy (widok opiekuna) */
#spDecisions{display:flex;gap:10px;flex-wrap:wrap}
.dec-card{background:#3d3000;border:1px solid #b08800;border-radius:10px;padding:10px 14px;max-width:400px;cursor:pointer;animation:pulseIco 1.6s 6} /* „odczekaj chwilę”: miga ~10s i przestaje */
.dec-card.esc{border-color:#f85149;background:#3d1215}
.dec-card:hover{filter:brightness(1.15)}
.mscard{background:#161b22;border:1px solid #30363d;border-left:4px solid #1f6feb;border-radius:10px;padding:10px 14px;margin:8px 0;cursor:pointer}
.mscard:hover{border-color:#58a6ff;filter:brightness(1.1)}
.mscard.closed{opacity:.55}
.mscard .goal{font-size:12px;color:#c9d1d9;margin:4px 0}
.mscard .meta{font-size:11px;color:#8b949e}
.pbar{height:8px;background:#21262d;border-radius:6px;overflow:hidden;margin:6px 0}
.pbar i{display:block;height:100%;background:linear-gradient(90deg,#238636,#3fb950)}
.blk{font-size:11px;color:#f85149;margin-top:2px}
#spBacklog .brow{display:flex;gap:8px;align-items:center;border-bottom:1px solid #21262d;padding:4px 6px;font-size:12px;cursor:pointer;flex-wrap:wrap}
#spBacklog .brow:hover{background:#161b22}
/* 🌊 FLOW — diagram przepływu pracy (config-based: engine/flow/default_flow.json + override) */
#flowWrap{width:100%;overflow:auto;border:1px solid #21262d;border-radius:10px;background:#0b0f14}
#flowSvg{width:100%;min-width:960px;height:auto;display:block}
#flowSvg .laneBg{fill:#0d1117;stroke:#21262d}
#flowSvg .laneLabel{fill:#8b949e;font-size:11px;font-weight:bold}
#flowSvg .stage rect.box{fill:#161b22;stroke:#30363d;stroke-width:1.5}
#flowSvg .stage.hot rect.box{animation:flowHot 1.2s infinite}
@keyframes flowHot{0%,100%{stroke-width:2;filter:drop-shadow(0 0 4px #b0880066)}50%{stroke-width:3.5;filter:drop-shadow(0 0 14px #b08800cc)}}
#flowSvg .stage .ttl{cursor:pointer}
#flowSvg .stage .ttl:hover{fill:#58a6ff}
#flowSvg .chip{cursor:pointer}
#flowSvg .chip rect{fill:#0d1117;stroke:#30363d}
#flowSvg .chip:hover rect{stroke:#58a6ff}
#flowSvg .chip.overdue rect{stroke:#f85149;stroke-width:1.5}
#flowSvg .chip .cid{fill:#c9d1d9;font-weight:bold}
#flowSvg .chip .who{fill:#8b949e}
#flowSvg .chip .clk{fill:#3fb950;font-variant-numeric:tabular-nums}
#flowSvg .chip .act{cursor:pointer}
#flowSvg .chip .act:hover{filter:brightness(1.6)}
#flowSvg path.arrow{stroke:#484f58;stroke-width:2;fill:none}
#flowSvg path.arrow.hot{stroke:#b08800}
#flowSvg path.arrow.anim{stroke-dasharray:7 7;animation:flowDash 1.6s linear infinite}
@keyframes flowDash{to{stroke-dashoffset:-28}}
#flowSvg .arrowLbl{fill:#8b949e;font-size:9px;text-anchor:middle}
#flowSvg .arrowLbl.hot{fill:#d29922;font-weight:bold}
#flowToolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}
#flowToolbar button{background:#161b22;border:1px solid #30363d;color:#c9d1d9;border-radius:8px;padding:6px 12px;font-size:12px;cursor:pointer}
#flowToolbar button:hover{border-color:#58a6ff}
#flowToolbar button.cfg{background:#1f6feb;color:#fff;border-color:#1f6feb;font-weight:bold}
#flowMeta{font-size:11px}
#flowCfgBox{width:100%;height:52vh;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;font-family:inherit;font-size:11px;border-radius:8px;padding:8px}
.flowErr{color:#f85149;font-size:11px;white-space:pre-wrap;background:#3d1215;border:1px solid #f85149;border-radius:6px;padding:6px;margin:6px 0}
.flowOk{color:#3fb950;font-size:11px}

/* 🌊 FLOW — przełącznik pod-zakładek: ⚙️ flow pracy / 🏗️ flow stanowisk */
.ftabs{display:flex;gap:6px;margin:2px 0 10px;flex-wrap:wrap}
.ftabs button{background:#161b22;border:1px solid #30363d;color:#8b949e;border-radius:8px;padding:6px 14px;font-size:13px;cursor:pointer;white-space:nowrap}
.ftabs button.on{background:#1f6feb;color:#fff;border-color:#1f6feb;font-weight:bold}
.ftabs button:active{transform:scale(.96)}
#flowWorkView,#flowOrgView{animation:fadeIn .15s}
/* 🏗️ FLOW STANOWISK — org chart (config-based: engine/flow/default_org.json + override) */
#orgToolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}
#orgToolbar button{background:#161b22;border:1px solid #30363d;color:#c9d1d9;border-radius:8px;padding:6px 12px;font-size:12px;cursor:pointer}
#orgToolbar button:hover{border-color:#58a6ff}
#orgToolbar button.cfg{background:#1f6feb;color:#fff;border-color:#1f6feb;font-weight:bold}
#orgWrap{width:100%;overflow:auto;border:1px solid #21262d;border-radius:10px;background:#0b0f14}
#orgSvg{width:100%;min-width:960px;height:auto;display:block}
#orgSvg .edge{stroke:#484f58;stroke-width:2;fill:none}
#orgSvg .person{cursor:pointer}
#orgSvg .person rect.box{fill:#161b22;stroke:#30363d;stroke-width:1.5}
#orgSvg .person:hover rect.box{stroke:#58a6ff}
#orgSvg .person.root rect.box{fill:#1c2333;stroke:#b08800}
#orgSvg .person .av{font-size:20px}
#orgSvg .person .nm{fill:#c9d1d9;font-size:12px;font-weight:bold}
#orgSvg .person .rl{fill:#8b949e;font-size:10px}
#orgSvg .person .st{fill:#58a6ff;font-size:9px}
#orgSvg .person .dot{fill:#6e7681}
#orgSvg .person.busy .dot{fill:#3fb950}
#orgSvg .depthLbl{fill:#8b949e;font-size:10px;font-weight:bold}
/* widok FIRMA (osiedle z góry) */
 #estateWrap{width:100%;overflow:hidden;border:1px solid #d9c8a9;border-radius:10px}
 #estate{width:100%;height:72vh;display:block;cursor:grab;background:#f3ead8;touch-action:none}
 #estate:active{cursor:grabbing}
 #estate .desk{will-change:transform}
 #estate .desk rect{filter:drop-shadow(0 1px 2px #0009)}
 #estate .desk:hover rect{filter:brightness(1.5) drop-shadow(0 2px 4px #000a)}
 #estate g.office .plot{transition:filter .2s}
 #estate g.office:hover .plot{filter:brightness(1.3)}
 #estate g.room:hover rect{stroke:#58a6ff}
 #estate text{user-select:none}
 #estate .room.busy{animation:pulseIco 2s infinite}
 /* przy standardowej pracy agent tylko „miga” (blink avataru) */
 #estate .room.busy .ava{animation:avaBlink 1.1s infinite}
 @keyframes avaBlink{0%,100%{opacity:1}50%{opacity:.15}}
 /* alarm — mocne czerwone miganie strefy, gdy są zadania FAILED/ESCALATED */
 #estate .alarmzone rect{animation:alarmFlash .9s infinite}
 @keyframes alarmFlash{0%,100%{fill-opacity:.3;stroke:#f85149;stroke-width:1.5}50%{fill-opacity:.95;stroke:#ff7b72;stroke-width:3}}
 /* dymki rozmów live między agentami (kolor rozmówcy) */
 #estate .bubble{animation:bubblePop .25s ease-out}
 @keyframes bubblePop{from{opacity:0}to{opacity:1}}
 #estate .bubble rect{filter:drop-shadow(0 2px 5px #0009)}
 #estate .convLink{stroke-dasharray:5 6;animation:convDash 1.1s linear infinite;fill:none}
 @keyframes convDash{to{stroke-dashoffset:-22}}
 /* fullscreen widoku biura */
 #tab-firma.fs{position:fixed;inset:0;z-index:60;background:#0d1117;padding:12px;overflow:auto}
 #tab-firma.fs #estate{height:calc(100vh - 170px)}
 #tab-firma.fs #estateWrap{height:auto}
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
<h1>🏭 Dark Factory <button id="liveTg" onclick="toggleAutoRefresh()" title="włącz/wyłącz auto-odświeżanie (LIVE)" style="background:#0d1117;border:1px solid #3fb950;color:#3fb950;border-radius:10px;padding:1px 10px;font-size:12px;cursor:pointer">● LIVE</button> <span id="ts"></span> <span id="host" class="muted" style="font-size:11px"></span> <span id="pause" class="muted" style="font-size:11px;color:#f0b429"></span> <span id="ver" class="muted" style="font-size:11px;cursor:pointer;border:1px solid #30363d;border-radius:10px;padding:1px 8px" onclick="openChangelog()" title="wersja dashboardu — klik = changelog"></span></h1>
<div id="stats"></div>
<div id="newHires"></div>
<div id="projFilters" title="Filtry projektów — globalne (działają na wszystkich zakładkach i mapie Firma)"></div>
<div id="stFilters" title="Filtry statusów — globalne (MERGED domyślnie ukryty; klik = pokaż/ukryj)"></div>
<nav id="tabs">
  <button data-tab="overview" onclick="showTab('overview')">📊 Przegląd<span id="navAlert"></span></button>
  <button data-tab="sponsor" onclick="showTab('sponsor')">🎯 Tematy<span id="navDec"></span></button>
  <button data-tab="tasks" onclick="showTab('tasks')">📋 Zadania</button>
  <button data-tab="flow" onclick="showTab('flow')">🌊 Flow</button>
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
<section class="tabpage" id="tab-sponsor">
<h2>🚨 Wymaga Twojej decyzji <span class="muted">(klik kartę = szczegóły zadania · przyciski = akcje od razu przez flow)</span></h2>
<div id="spDecisions"></div>
<h2>⏳ Oczekujące zgody (approvals)</h2>
<div id="spApprovals" style="display:flex;gap:10px;flex-wrap:wrap"></div>
<h2>🎯 Tematy (milestone'y) <span class="muted">(klik temat = szczegóły: cel, postęp, zadania, bloki)</span> <button class="tbtn" onclick="openNewMilestone()">➕ nowy temat</button></h2>
<div id="spMilestones"></div>
<h2>📥 Backlog — zadania READY do pociągnięcia przez zespół <span class="muted">(klik = szczegóły)</span></h2>
<div id="spBacklog"></div>
</section>
<section class="tabpage" id="tab-tasks">
<h2>Zadania <span class="badge" id="awaitCount"></span> <span class="muted">(kliknij wiersz = szczegóły; żółte = czeka na Twoją akceptację; filtry projektów są wyżej — globalne)</span></h2>
<table id="tasks"><thead><tr><th></th><th>prio</th><th>id</th><th>tytuł</th><th>status</th><th>substep</th><th>retry</th><th>kto (rola)</th><th>projekt</th><th>pracuje</th><th>PR</th><th>updated</th></tr></thead><tbody></tbody></table>
</section>
<section class="tabpage" id="tab-flow">
<h2>🌊 Flow <span class="muted">(dwa widoki: przepływ pracy zespołu oraz hierarchia stanowisk — kto jest czyim szefem)</span></h2>
<div class="ftabs" id="flowTabs">
  <button data-fview="work" onclick="showFlowView('work')">⚙️ Flow pracy</button>
  <button data-fview="org" onclick="showFlowView('org')">🏗️ Flow stanowisk</button>
</div>
<div id="flowWorkView">
<h3>⚙️ Flow pracy <span class="muted">(diagram budowany z configa · klik kafelek = szczegóły zadania · klik ikonka akcji na kafelku = akcja Sponsora · klik nagłówek etapu = opis + wszystkie zadania)</span></h3>
<div id="flowToolbar">
  <button onclick="renderFlow()">🔄 odśwież diagram</button>
  <button class="cfg" onclick="openFlowCfg()">⚙️ zarządzaj flow (config JSON)</button>
  <span id="flowMeta" class="muted"></span>
  <div id="flowStageFilters" title="Filtry etapów flow — klik = ukryj/pokaż etap · dwuklik = SOLO (tylko ten etap)"></div>
</div>
<div id="flowWrap"><svg id="flowSvg" preserveAspectRatio="xMidYMin meet"></svg></div>
</div>
<div id="flowOrgView" style="display:none">
<h3>🏗️ Flow stanowisk <span class="muted">(hierarchia zespołu: kto komu podlega · klik osobę = karta pracownika · klik „szef”/„podwładny” = skok po drzewie · dane z configu org)</span></h3>
<div id="orgToolbar">
  <button onclick="renderOrg()">🔄 odśwież hierarchię</button>
  <button class="cfg" onclick="openOrgCfg()">⚙️ zarządzaj hierarchią (config JSON)</button>
  <span id="orgMeta" class="muted"></span>
</div>
<div id="orgWrap"><svg id="orgSvg" preserveAspectRatio="xMidYMin meet"></svg></div>
</div>
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
<h2>🤖 Kolejka LLM <span id="llmStats" class="muted" style="font-size:11px"></span> <span class="muted">(complete idą seryjnie przez kolejkę — dispatcher w orchestratorze, retry z backoff; koniec rc=3 keyring)</span></h2>
<div class="feed" id="llmJobs"></div>
<h2>🔴 Live — .agents/runtime (surowe odpowiedzi LLM) <button onclick="toggleLive()">▶/⏸</button></h2>
<div class="feed" id="runtime"></div>
<h2>📜 Logi procesów (.agents/logs) — klik = tail</h2>
<div class="feed" id="logs"></div>
<pre id="viewer" style="background:#0d1117;border:1px solid #30363d;padding:8px;max-height:300px;overflow:auto;font-size:11px"></pre>
</section>
<section class="tabpage" id="tab-firma">
<h2>🏢 Firma — mapka osiedla <span class="muted">(biuro=projekt · pokój=agent · biurko=zadanie · 🏆=milestone; klik biurko=szczegóły zadania, klik pokój=wątek z osobą, klik biuro=filtr projektu, klik legendę=podświetl status; kółko=zoom, drag=przesuwaj)</span>
<button onclick="renderFirma()" style="background:#1f6feb;color:#fff;border:0;border-radius:6px;padding:4px 12px;cursor:pointer">🔄 odśwież mapę</button>
<button id="fsBtn" onclick="toggleFirmaFs()" style="background:#161b22;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:4px 12px;cursor:pointer">⛶ pełny ekran</button>
<span class="muted" style="font-size:11px">na mapie auto-refresh rzadszy (co 10s) — nie wiesza widoku</span></h2>
<div id="firmaLegend"></div>
<div id="estateWrap"><svg id="estate" preserveAspectRatio="xMidYMid meet"></svg></div>
</section>
<div id="detail"><button onclick="closeDetail()" style="float:right">✖</button><div id="detailBody">wybierz zadanie…</div></div>
<script>
let curTask = null;
let lastEventTs = 0;
// kolorowe filtry projektów — hiddenProjs: localStorage + URL hash + serwer (kv), żeby przeżyły odświeżenie/zmianę adresu
const hiddenProjs = new Set(JSON.parse(localStorage.getItem('hiddenProjs')||'[]'));
// filtry STATUSÓW — globalne; MERGED domyślnie UKRYTY (odznaczony na starcie)
const hiddenSts = new Set(JSON.parse(localStorage.getItem('hiddenSts')||'["MERGED"]'));
function currentTabName(){ const b = document.querySelector('#tabs button.on'); return (b && b.dataset.tab) || 'overview'; }
function filterParams(){
  const p = [];
  if(hiddenProjs.size) p.push('hp=' + [...hiddenProjs].join(','));
  if(hiddenSts.size) p.push('hs=' + [...hiddenSts].join(','));
  if(typeof getFlowHiddenStages === 'function'){ const fh = getFlowHiddenStages(); if(fh.size) p.push('fs=' + [...fh].join(',')); }
  return p.length ? '?' + p.join('&') : '';
}
function persistFilters(){
  // źródła prawdy: localStorage (cache) + URL hash (przeżywa F5 i zmianę karty) + serwer kv (współdzielone między urządzeniami)
  localStorage.setItem('hiddenProjs', JSON.stringify([...hiddenProjs]));
  localStorage.setItem('hiddenSts', JSON.stringify([...hiddenSts]));
  if(typeof getFlowHiddenStages === 'function') localStorage.setItem('flowHiddenStages', JSON.stringify([...getFlowHiddenStages()]));
  history.replaceState(null, '', '#' + currentTabName() + filterParams());
  syncFiltersToServer();
}
async function syncFiltersToServer(){
  try{ await fetch('/api/filters', {method:'POST', body: JSON.stringify({hiddenProjs:[...hiddenProjs], hiddenSts:[...hiddenSts], flowHiddenStages:(typeof getFlowHiddenStages==='function'?[...getFlowHiddenStages()]:[])})}); }catch(e){}
}
function applyHashFilters(){
  const h = location.hash || '';
  if(h.indexOf('?') < 0) return false;
  const get = k => { const m = h.match(new RegExp('(?:^|[?&])' + k + '=([^&]*)')); return m ? m[1] : undefined; };
  let applied = false;
  const hp = get('hp'); if(hp !== undefined){ hiddenProjs.clear(); hp.split(',').filter(Boolean).forEach(x => hiddenProjs.add(x)); applied = true; }
  const hs = get('hs'); if(hs !== undefined){ hiddenSts.clear(); hs.split(',').filter(Boolean).forEach(x => hiddenSts.add(x)); applied = true; }
  const fs = get('fs'); if(fs !== undefined){ flowHiddenStages = new Set(fs.split(',').filter(Boolean)); applied = true; }
  if(applied){ persistFilters(); }
  return applied;
}
async function loadFiltersFromServer(){
  try{
    const r = await fetch('/api/filters'); const j = await r.json();
    if(!j) return false;
    if(j.hiddenProjs){ hiddenProjs.clear(); (j.hiddenProjs||[]).forEach(x => hiddenProjs.add(x)); }
    if(j.hiddenSts){ hiddenSts.clear(); (j.hiddenSts||[]).forEach(x => hiddenSts.add(x)); }
    if(j.flowHiddenStages && typeof getFlowHiddenStages === 'function') flowHiddenStages = new Set(j.flowHiddenStages);
    localStorage.setItem('hiddenProjs', JSON.stringify([...hiddenProjs]));
    localStorage.setItem('hiddenSts', JSON.stringify([...hiddenSts]));
    if(typeof getFlowHiddenStages === 'function') localStorage.setItem('flowHiddenStages', JSON.stringify([...getFlowHiddenStages()]));
    return true;
  }catch(e){ return false; }
}
function toggleSt(st){ if(hiddenSts.has(st)) hiddenSts.delete(st); else hiddenSts.add(st); persistFilters(); refresh(); /* refreshInner samo odświeży Firmę gdy aktywna */ }
function resetSts(){ hiddenSts.clear(); persistFilters(); refresh(); }
let _stFilterKey = '';
function renderStFilters(d){
  // cache: renderuj tylko gdy zmieni się zestaw statusów / liczby / stan ukrycia
  const allSt = [...new Set(d.tasks.map(t => t.status))];
  const key = JSON.stringify(allSt) + '|' + JSON.stringify(allSt.map(st => d.tasks.filter(t => t.status === st).length)) + '|' + JSON.stringify([...hiddenSts]);
  if(key === _stFilterKey) return;
  _stFilterKey = key;
  document.getElementById('stFilters').innerHTML = allSt.map(st => {
    const off = hiddenSts.has(st);
    const n = d.tasks.filter(t => t.status === st).length;
    return `<button class="pchip ${off?'off':''}" style="--c:${ST_COLOR[st]||'#6e7681'}" onclick="toggleSt('${st}')">${d.icons[st]||''} ${st} <span class="muted">${n}</span></button>`;
  }).join('') + `<button class="pchip reset" onclick="resetSts()">✖ wszystkie</button>`;
}
function projColor(s){ let h=0; for(const c of String(s)) h=(h*31+c.charCodeAt(0))>>>0; return 'hsl('+(h%360)+',70%,45%)'; }
const ST_COLOR = {'READY':'#6e7681','RUNNING':'#1f6feb','IN_REVIEW':'#a371f7','CHANGES_REQUESTED':'#d29922','AWAITING_OWNER':'#b08800','READY_TO_MERGE':'#238636','MERGED':'#2ea043','FAILED':'#da3633','ESCALATED':'#f85149','BLOCKED':'#57606a'};
function sbadge(st){ const c = ST_COLOR[st]||'#6e7681'; return `<span class="sbadge ${st==='ESCALATED'?'esc':''}" style="background:${c}">${esc(st)}</span>`; }
function pbadge(pid){ return `<span class="pbadge" style="background:${projColor(pid||'(brak)')}">${esc(pid||'(brak)')}</span>`; }
function toggleProj(pid){ if(hiddenProjs.has(pid)) hiddenProjs.delete(pid); else hiddenProjs.add(pid); persistFilters(); refresh(); /* refreshInner samo odświeży Firmę gdy aktywna — bez dubli renderu */ }
function resetProjs(){ hiddenProjs.clear(); persistFilters(); refresh(); }
let lastVersion = null;
let _projFilterKey = '';
function renderProjFilters(d, visTasks){
  // cache: renderuj pasek filtrów tylko gdy zmieni się lista projektów lub liczby zadań
  const allProjs = [...new Set(d.projects.map(p=>p.project_id).concat(d.tasks.map(t=>t.project_id||'(brak)')))];
  const key = JSON.stringify(allProjs) + '|' + JSON.stringify(allProjs.map(pid => d.tasks.filter(t=>(t.project_id||'(brak)')===pid).length)) + '|' + JSON.stringify([...hiddenProjs]);
  if(key === _projFilterKey) return;
  _projFilterKey = key;
  document.getElementById('projFilters').innerHTML = allProjs.map(pid => {
    const off = hiddenProjs.has(pid);
    const n = d.tasks.filter(t=>(t.project_id||'(brak)')===pid).length;
    return `<button class="pchip ${off?'off':''}" style="--c:${projColor(pid)}" onclick="toggleProj('${esc(pid)}')">${esc(pid)} <span class="muted">${n}</span></button>`;
  }).join('') + `<button class="pchip reset" onclick="resetProjs()">✖ wszystkie</button>`;
}
function openMerges(){
  curTask = null; curThread = null;
  const mg = (lastState && lastState.merges) || {recent: []};
  document.getElementById('detail').classList.add('open');
  document.getElementById('detailBody').dataset.key = 'merges';
  document.getElementById('detailBody').innerHTML = `<h3>🔀 Merge'e</h3>
    <div class="kv">w ostatnich 24h: <b>${mg.count_24h||0}</b>${mg.last_ago_s!=null?` · ostatni <b>${fmtDur(mg.last_ago_s)}</b> temu`:''}</div>
    <h2>Ostatnie (max 10)</h2><div class="histbox">${(mg.recent||[]).map(m=>`<div><span class="muted">${new Date(m.ts*1000).toLocaleString()}</span> ${m.task_id?`<a href="#" onclick="openTask('${esc(m.task_id)}');return false" style="color:#58a6ff">${esc(m.task_id)}</a>`:'—'} <span class="muted">${esc(m.kind)}</span> <span class="muted">(${fmtDur(Date.now()/1000-m.ts)} temu)</span></div>`).join('')||'<span class="muted">brak merge’ów</span>'}</div>`;
}
function openChangelog(){
  curTask = null; curThread = null;
  const v = lastVersion || {};
  document.getElementById('detail').classList.add('open');
  document.getElementById('detailBody').dataset.key = 'changelog';
  document.getElementById('detailBody').innerHTML = `<h3>🏷️ Wersja dashboardu: <b>v${esc(v.version||'?')}</b></h3>
    <div class="kv">ostatni commit: <b>${esc(v.last_commit||'?')}</b> ${v.commit_subject?`<span class="muted">— ${esc(v.commit_subject)}</span>`:''}</div>
    <div class="kv">aktualizacja kodu: <b>${v.age_s!=null?fmtDur(v.age_s)+' temu':'?'}</b> (${v.commit_ts?new Date(v.commit_ts*1000).toLocaleString():''})</div>
    <h2>📜 Changelog</h2><pre style="max-height:50vh;overflow:auto;background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:8px;font-size:11px;white-space:pre-wrap">${esc(v.changelog||'brak')}</pre>`;
}
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
// powrót do karty = od razu sprawdź stan (i ew. nową wersję)
document.addEventListener('visibilitychange', () => { if(!document.hidden && autoRefreshOn && !refreshPaused) refresh(); });
async function refresh(){ if(refreshPaused) return; try{ await refreshInner(); }catch(e){ document.getElementById('ts').textContent = '⚠ błąd odświeżania: '+e; } }
async function refreshInner(){
  const r = await fetch('/api/state'); const d = await r.json();
  lastState = d;
  document.getElementById('ts').textContent = new Date().toLocaleTimeString();
  if(d.host){
    document.getElementById('host').innerHTML = d.host.dashboard_url
      ? `📍 LAN: <a href="${esc(d.host.dashboard_url)}" style="color:#58a6ff" target="_blank">${esc(d.host.dashboard_url)}</a> (${esc(d.host.lan_ip)} · ${esc(d.host.hostname)})`
      : `📍 offline (brak IP LAN) · host: ${esc(d.host.hostname)}`;
  }
  const st = d.stats||{};
  const mg = d.merges||{};
  // 🎉 nowa osoba w firmie — banner (świeża < 1h) + toast przy pierwszym zobaczeniu
  const seenHires = new Set(JSON.parse(localStorage.getItem('seenHires')||'[]'));
  document.getElementById('newHires').innerHTML = (d.new_hires||[]).map(h => {
    if(!seenHires.has(h.role)){
      seenHires.add(h.role); localStorage.setItem('seenHires', JSON.stringify([...seenHires]));
      toast('🎉 Nowa osoba w firmie: ' + (h.name||h.role) + ' — ' + (h.position||'') + ' (rola ' + h.role + ')');
    }
    return `<div class="hire-card" onclick="showTab('team')">
      <span class="ava" style="color:${h.color||'#3fb950'}">${h.avatar||h.icon||'🧑'}</span>
      <span>🎉 <b>Nowa osoba w firmie:</b> <b>${esc(h.name||h.role)}</b> <span class="muted">${esc(h.position||'')} · rola ${esc(h.role)}</span></span>
      <span class="muted" style="font-size:11px">🏅 staż ${fmtDur(h.tenure_s||0)}</span>
    </div>`;
  }).join('');
  document.getElementById('stats').innerHTML =
    `<span class="stat-badge ${st.awaiting?'warn':''}">🕒 czeka na Ciebie: <b>${st.awaiting||0}</b></span>`+
    `<span class="stat-badge">⏳ w kolejce (READY): <b>${st.waiting||0}</b></span>`+
    `<span class="stat-badge ok">🔄 pracują: <b>${st.running||0}</b></span>`+
    `<span class="stat-badge ${st.overdue?'bad':'ok'}">⚠️ spóźnione/stare (&gt;1h w kolejce): <b>${st.overdue||0}</b></span>`+
    `<span class="stat-badge ${st.running_long?'warn':''}">🐌 długo biegną (&gt;1h): <b>${st.running_long||0}</b></span>`+
    (d.sys?`<span class="stat-badge" title="load / CPU / RAM laptopa">🖥️ load <b>${d.sys.load1}</b>/${d.sys.cpus} · 🧠 RAM <b>${d.sys.mem_used_pct}%</b> (wolne ${d.sys.mem_avail_gb}GB)</span>`:'')+
    `<span class="sbadge" style="background:#238636;cursor:pointer;font-size:11px;padding:3px 10px" onclick="openMerges()" title="klik = lista ostatnich merge’ów">🔀 mergi 24h: <b>${mg.count_24h||0}</b>${mg.last_ago_s!=null?` · ostatni <b>${fmtDur(mg.last_ago_s)}</b> temu`:''}</span>`;
  // wersja dashboardu w nagłówku (klik = changelog) + wykrywanie NOWEJ wersji po stronie serwera -> banner "odśwież Ctrl+R"
  if(d.version){
    const v = d.version;
    document.getElementById('ver').textContent = `v${v.version} · ${v.last_commit||''} · aktualizacja ${v.age_s!=null?fmtDur(v.age_s)+' temu':'?'}`;
    document.getElementById('ver').title = (v.commit_subject||'') + '\\nklik = changelog';
    lastVersion = v;
    const curVer = v.version + '|' + (v.last_commit || '');
    const seenVer = localStorage.getItem('dashVer');
    if(!seenVer){ localStorage.setItem('dashVer', curVer); }        // pierwszy load — zapamiętaj bez bannera
    else if(seenVer !== curVer){ showUpdateBanner(v); }               // serwer ma nowszy kod niż ta karta -> odśwież
  }
  // kolorowe filtry projektów — GLOBALNE (nad zakładkami), render tylko gdy zmieni się lista projektów (cache)
  const visTasks = d.tasks.filter(t => !hiddenProjs.has(t.project_id || '(brak)') && !hiddenSts.has(t.status));
  renderProjFilters(d, visTasks);
  renderStFilters(d);
  const alerts = visTasks.filter(t=>t.status==='AWAITING_OWNER');
  document.getElementById('alerts').innerHTML = alerts.length ? alerts.map(t =>
    `<div class="alert-card ${['ASK_OWNER','ASK_SPONSOR'].includes(t.substep)?'ask':''}" onclick="openTask('${t.id}')">
       <b>${['ASK_OWNER','ASK_SPONSOR'].includes(t.substep)?'❓ ODPOWIEDZ':'🚨 TWÓJ RUCH'}</b> <b>${esc(t.id)}</b> ${sbadge(t.status)} ${pbadge(t.project_id)}
       <div>${esc((t.title_sponsor||t.title||'').slice(0,80))}</div>
       <div class="muted">${esc((t.review_notes||'').slice(0,120))}</div>
       <div class="muted">czeka ${fmtDur(t.age||0)} · ${esc(t.persona||t.role||'')}</div>
     </div>`).join('') : '<span class="muted">nic nie czeka na Twoją decyzję ✅</span>';
  document.getElementById('projects').innerHTML = d.projects.map(p =>
    `<span class="muted">🗂</span> ${pbadge(p.project_id)} ${p.active===false?'🛑 <b style="color:#f85149">NIEAKTYWNY</b>':''} policy=${esc(p.approval_policy||'manual')} read_only=${p.read_only?1:0} target=${esc(p.target)}`).join('<br>');
  const PIC = {'P0':'🔥','P1':'⚡','P2':'•','P3':'🐢'};
  // przepływ pracy: kanban statusów (kolejność procesu), kafelek = zadanie
  const FLOW = ['AWAITING_OWNER','READY','RUNNING','CHANGES_REQUESTED','IN_REVIEW','READY_TO_MERGE','MERGED','FAILED','ESCALATED','BLOCKED'];
  const byStatus = {}; for(const t of visTasks){ (byStatus[t.status] = byStatus[t.status]||[]).push(t); }
  document.getElementById('flow').innerHTML = FLOW.map((st,i) => {
    const items = byStatus[st]||[];
    const hot = st==='AWAITING_OWNER' && items.length, run = st==='RUNNING';
    const chips = items.slice(0,8).map(t =>
      `<span class="chip st-${st}" onclick="openTask('${t.id}')">${PIC[t.priority||'P2']||''}<b>${esc(t.id)}</b>${t.depth?' ↳':''} ${t.status==='RUNNING'&&t.started_at?(`<span class="clock" data-started="${t.started_at}">⏱ ${fmtDur(Date.now()/1000-t.started_at)}</span>`):''}
       <span class="who">${esc(t.persona||t.role||'')}</span></span>`).join('') + (items.length>8?`<span class="chip muted">+${items.length-8} więcej…</span>`:'');
    const arrow = i < FLOW.length-1 ? '<span class="arrow">→</span>' : '';
    return `<div class="stage ${hot?'hot':''} ${run?'run':''}"><h4>${d.icons[st]||''} ${sbadge(st)} <span class="cnt">${items.length}</span></h4>${chips}</div>${arrow}`;
  }).join('');
  const tb = document.querySelector('#tasks tbody'); tb.innerHTML = '';
  let awaiting = 0;
  const PRIO = {'P0':0,'P1':1,'P2':2,'P3':3};
  const sorted = [...visTasks].sort((a,b)=>(PRIO[b.priority||'P2']-PRIO[a.priority||'P2']) || ((b.status==='RUNNING')-(a.status==='RUNNING')) || ((b.updated_at||0)-(a.updated_at||0)));
  const nowCards = [];
  for(const t of sorted){
    if(t.status==='AWAITING_OWNER') awaiting++;
    if(t.status==='RUNNING') nowCards.push(t);
    const tr = tb.insertRow();
    tr.className = 'st-' + t.status;
    if(t.status==='AWAITING_OWNER') tr.className+=' awaiting';
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
      <td class="st">${t.status==='AWAITING_OWNER'?(['ASK_OWNER','ASK_SPONSOR'].includes(t.substep)?'<span class="badge">❓ ODPOWIEDZ</span> ':'<span class="badge">TWÓJ RUCH</span> '):''}${sbadge(t.status)}</td>
      <td class="muted">${esc(t.substep||'')}</td><td>${t.retry_count}</td>
      <td class="muted">${esc(t.persona||t.role||'')}</td><td class="muted">${pbadge(t.project_id)}</td>
      <td class="muted">${clock}</td>
      <td class="muted">${t.pr?('!'+t.pr):''}</td><td class="muted">${ago}</td>`;
  }
  document.getElementById('awaitCount').textContent = awaiting ? awaiting+' czeka na Ciebie' : '';
  const navA = document.getElementById('navAlert');
  if(navA) navA.textContent = awaiting ? ' 🚨'+awaiting : '';
  document.getElementById('team').innerHTML = (d.team||[]).map(m => {
    const col = m.color || '#8b949e';
    const curRes = m.current && m.current.res ? ` <span class="muted" title="pid ${m.current.res.pid}">⚙️${m.current.res.cpu}% 🧠${m.current.res.rss_mb}MB</span>` : '';
    const cur = m.current ? `<div class="cur">🔨 ${esc(m.current.id)} <span class="clock" data-started="${m.current.started_at||0}">⏱ ${m.current.started_at?fmtDur(Date.now()/1000-m.current.started_at):''}</span> <span class="prio prio-${m.current.priority||'P2'}">${m.current.priority||'P2'}</span>${curRes}</div>` : '<div class="cur muted">— wolny/a —</div>';
    // liczba zadań per status (kolorowe chipsy) zamiast „zadania: X zmergowane: Y”
    const chips = Object.entries(m.by_status||{}).sort((a,b)=>b[1]-a[1]).map(([st,n]) =>
      `<span class="sbadge" style="background:${ST_COLOR[st]||'#6e7681'};font-size:9px;padding:0 6px;margin:0 3px 2px 0" title="${esc(st)}: ${n}">${d.icons[st]||''} ${n}</span>`).join('');
    return `<div class="pcard ${m.current?'busy':''}" style="border-top:3px solid ${col}" title="rola: ${m.role} · kolor osoby: ${col}">
      <span class="ava" style="color:${col};text-shadow:0 0 8px ${col}66">${m.avatar||'🧑‍💻'}</span> <span class="nm">${esc(m.name)}</span>
      <button class="tbtn" style="float:right" onclick="event.stopPropagation();startThread('${esc(m.role)}')" title="rozmowa z ${esc(m.name)}">💬</button>
      <button class="tbtn" style="float:right;margin-right:4px" onclick="event.stopPropagation();openDuties('${esc(m.role)}')" title="obowiązki cykliczne ${esc(m.name)}">📋</button>
      <div class="pos" style="color:${col}">${esc(m.position)} · rola: ${esc(m.role)}</div>
      <div class="story">„${esc(m.backstory)}”</div>
      ${cur}
      <div class="stats">${chips || '<span class="muted">brak zadań</span>'}</div>
      <div class="tenure" title="czas w firmie / przepracowany czas">🏅 staż ${fmtDur(m.tenure_s||0)} · ⏱ przepracowane ${fmtDur(m.work_s||0)}</div>
    </div>`;
  }).join('');
  document.getElementById('threads').innerHTML = (d.threads||[]).map(th =>
    `<div class="tcard" onclick="openThread('${esc(th.id)}')">
       <b>${th.persona.avatar||'💬'} ${esc(th.persona.name||th.role)}</b> ${pbadge(th.role)} — ${esc((th.title||'').slice(0,60))}
       <span class="muted">· ${th.count} wiad.</span>
       ${th.last?`<div class="muted" style="font-size:11px">${new Date(th.last.ts*1000).toLocaleTimeString()} <b>${esc(th.last.author)}</b>: ${esc((th.last.text||'').slice(0,90))}</div>`:''}
     </div>`).join('') || '<span class="muted">brak wątków — kliknij 💬 przy osobie lub napisz na Telegramie</span>';
  document.getElementById('now').innerHTML = nowCards.length ? nowCards.map(t =>
    `<div class="nowcard" onclick="openTask('${t.id}')"><b>${d.icons.RUNNING} ${t.id}</b> <span class="pico">${PIC[t.priority||'P2']||''}</span><span class="prio prio-${t.priority||'P2'}">${t.priority||'P2'}</span>
     <div class="clock" data-started="${t.started_at||0}">⏱ ${t.started_at?fmtDur(Date.now()/1000-t.started_at):''}</div>
     ${t.res?`<div class="muted" title="proces workera pid ${t.res.pid}">⚙️ CPU ${t.res.cpu}% · 🧠 RAM ${t.res.rss_mb}MB</div>`:''}
     <span class="who">${esc(t.persona||t.role||'')} · ${pbadge(t.project_id)}</span></div>`).join('')
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
  // kolejka LLM
  try{
    const lr = await fetch('/api/llm/jobs'); const lj = await lr.json();
    const stEl = document.getElementById('llmStats');
    if(stEl) stEl.textContent = Object.entries(lj.stats||{}).map(([k,v]) => k + ': ' + v.count + ' (prób ' + v.attempts + ')').join(' · ') || 'pusta';
    document.getElementById('llmJobs').innerHTML = (lj.jobs||[]).map(j =>
      `<div><span class="muted">${new Date(j.created_at*1000).toLocaleTimeString()}</span> <b style="color:${j.status==='DONE'?'#3fb950':(j.status==='FAILED'?'#f85149':(j.status==='RUNNING'?'#58a6ff':'#d29922'))}">${esc(j.status)}</b> ${esc(j.kind)} ${esc(j.step||'')} <span class="muted">${esc(j.requester||'')} · prób ${j.attempts} · ${esc(j.id)}</span>${j.error?`<span style="color:#f85149"> ${esc((j.error||'').slice(0,120))}</span>`:''}</div>`).join('') || '<span class="muted">pusta kolejka</span>';
  }catch(e){}
  if(curTask) openTask(curTask, true);
  if(curThread) openThread(curThread, true);
  if(document.getElementById('tab-firma').classList.contains('on')) renderFirma();
  if(document.getElementById('tab-flow').classList.contains('on')){ if(flowView === 'org') renderOrg(); else renderFlow(); }
  if(document.getElementById('tab-sponsor').classList.contains('on')) renderSponsor();
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
    <button class="${detailTab==='overview'?'on':''}" onclick="detailTab='overview';openTask(curTask,true)">📋 Szczegóły</button>
    <button class="${detailTab==='history'?'on':''}" onclick="detailTab='history';openTask(curTask,true)">🕘 Historia (${(t.events||[]).length})</button>
    <button class="${detailTab==='comments'?'on':''}" onclick="detailTab='comments';openTask(curTask,true)">💬 Komentarze (${(t.comments||[]).length})</button>
  </div>`;
  // blok podsumowania — ZAWSZE widoczny (tytuł, status, czas, rola, projekt, branch/PR)
  const summary = `<div class="kv"><b>tytuł:</b> ${esc(d.title_sponsor||d.title||'')}</div>
    <div class="kv muted" style="font-size:10px">tech: ${esc(d.title||'')}</div>
    <div class="kv"><b>status:</b> ${sbadge(d.status)} | <b>substep:</b> ${esc(d.substep||'-')} | <b>retry:</b> ${d.retry_count} | <b>read_only:</b> ${d.read_only?1:0}${d.mode==='llm'?' | <b>🤖 tryb LLM</b>':''} | <b>prio:</b> <span class="prio prio-${d.priority||'P2'}">${esc(d.priority||'P2')}</span></div>
    <div class="kv"><b>rola:</b> ${esc(d.role||'')} → <b>${esc(d.persona||'')}</b> | <b>projekt:</b> ${pbadge(d.project_id)} | <b>milestone:</b> ${esc(d.milestone||'')}</div>
    <div class="kv"><b>przejął:</b> ${esc(d.claimed_by||'-')}${d.started_at?` od ${new Date(d.started_at*1000).toLocaleTimeString()}`:''}${d.elapsed!=null?` (<b>${fmtDur(d.elapsed)}</b>)`:''} | <b>aktualizacja:</b> ${d.updated_at?new Date(d.updated_at*1000).toLocaleTimeString():'-'}</div>
    <div class="kv"><b>rodzic:</b> ${d.parent_task?`<a href="#" onclick="openTask('${esc(d.parent_task)}');return false" style="color:#58a6ff">${esc(d.parent_task)}</a>`:'— (zadanie główne)'} | <b>branch:</b> ${esc(d.branch||'-')} | <b>PR:</b> ${d.pr?('!'+d.pr):'-'}</div>`;
  let body = '';
  if(detailTab === 'history'){
    const nowS = Date.now()/1000;
    body = `<h2>🕘 Historia zmian <span class="muted">(najświeższe na górze — zielone = <60s, niebieskie = <1h)</span></h2>
      <div class="histbox">${(t.events||[]).slice().sort((a,b)=>((b.ts||0)-(a.ts||0))).map(e=>{
        const age = nowS - (e.ts||0);
        const cls = age<60 ? 'fresh2' : (age<3600 ? 'fresh1' : '');
        const ch = e.changes ? ' · ' + Object.entries(e.changes).map(([k,v])=>`${esc(k)}: ${esc(String(v).slice(0,50))}`).join(' · ') : '';
        return `<div class="${cls}"><span class="muted">${new Date((e.ts||0)*1000).toLocaleTimeString()}</span> ${esc(e.event||((e.from||'?')+' → '+(e.to||'?')))}${ch}${(e.by)?(' <span class="muted">— '+esc(String(e.by).slice(0,60))+'</span>'):''}</div>`;
      }).join('')||'<span class="muted">brak historii</span>'}</div>`;
  } else if(detailTab === 'comments'){
    body = `<h2>💬 Komentarze</h2><div class="threadbox">${(t.comments||[]).slice().sort((a,b)=>((b.ts||0)-(a.ts||0))).map(c=>`<div class="tmsg ${c.author==='sponsor'||c.author==='sponsor/dashboard'?'me':'agent'}"><div class="ta"><b>${esc(c.author)}</b> <span class="muted">${new Date(c.ts*1000).toLocaleTimeString()}</span></div><div class="tt">${esc(c.text.slice(0,400))}</div></div>`).join('')||'<span class="muted">brak komentarzy</span>'}</div>
      <h2>✍️ Twój komentarz</h2>
      <textarea id="cbox" placeholder="uwaga do zadania (też leci na PR na GitHubie)…"></textarea>
      <button onclick="sendComment()">Dodaj komentarz</button>
      ${d.substep==='ASK_SPONSOR'?`<button style="background:#238636;border-color:#2ea043;color:#fff" onclick="sendQuickTaskYes()" title="Wyślij automatycznie: „${esc(quickYesText())}”">⚡ TAK — realizuj, bez pytań</button>`:''}`;
  } else {
    body = `<div class="kv"><b>scope:</b> ${esc((d.scope_paths||[]).join(', ')||'-')}</div>
    <div class="kv"><b>AC:</b> ${esc((d.acceptance_criteria||'').slice(0,300))}</div>
    <div class="kv"><b>uwagi:</b> ${esc((d.review_notes||'').slice(0,300))}</div>`;
    if(d.status==='AWAITING_OWNER'){
      body += `<button class="ok" onclick="approve()">✅ Zaakceptuj → READY</button>
               <button class="no" onclick="decline()">🛑 Odrzuć → BLOCKED</button>`;
    }
  }
  document.getElementById('detailBody').innerHTML = `<h3>${t.icons[d.status]||''} ${esc(d.id)}</h3>` + summary + tabs + body;
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
    <div>
      <button onclick="sendThreadMsg()">Wyślij ✈</button>
      <button style="background:#238636;border-color:#2ea043;color:#fff" onclick="sendQuickYes(curThread)" title="Wyślij automatycznie: „${esc(quickYesText())}”">⚡ szybka odp: TAK</button>
    </div>`;
  const tb = document.getElementById('threadbox'); if(tb) tb.scrollTop = tb.scrollHeight;
}
function startThread(role){
  // od razu widok czatu (jak SMS) — bez dialogu prompt(); pierwsza wiadomość -> nowy wątek
  curThread = null; curTask = null;
  const m = (lastState && lastState.team || []).find(x => x.role === role);
  const p = m || {name: role, position: role, avatar: '🧑‍💻', role};
  document.getElementById('detail').classList.add('open');
  document.getElementById('detailBody').dataset.key = 'newthread|' + role;
  document.getElementById('detailBody').innerHTML = `<h3>💬 ${esc(p.avatar||'🧑‍💻')} ${esc(p.name)} <span class="muted">${esc(p.position||role)} · rola ${esc(role)}</span></h3>
    <div class="threadbox" id="threadbox" style="min-height:140px"><span class="muted">Nowa rozmowa — napisz pierwszą wiadomość poniżej.</span></div>
    <textarea id="tbox" placeholder="napisz do ${esc(p.name)}… (Enter = wyślij)"></textarea>
    <button onclick="sendNewThread('${esc(role)}')">Wyślij ✈</button>`;
  setTimeout(() => { const t = document.getElementById('tbox'); if(t) t.focus(); }, 60);
}
async function sendNewThread(role){
  const el = document.getElementById('tbox');
  const txt = el.value.trim(); if(!txt) return;
  el.value = '';
  const r = await fetch('/api/threads/start', {method:'POST', body: JSON.stringify({role, text: txt})});
  const j = await r.json();
  detailTab = 'overview';
  openThread(j.thread_id);
  refresh();
}
// Enter w polu wątku = wyślij (Shift+Enter = nowa linia)
document.addEventListener('keydown', (e) => {
  if(e.key === 'Enter' && !e.shiftKey && e.target && e.target.id === 'tbox'){
    e.preventDefault();
    if(curThread) sendThreadMsg(); else if(e.target.closest('#detailBody')) { const b = document.querySelector('#detailBody button'); }
  }
});
async function sendThreadMsg(){
  const txt = document.getElementById('tbox').value.trim(); if(!txt || !curThread) return;
  document.getElementById('tbox').value = '';
  await fetch('/api/thread/'+curThread+'/post', {method:'POST', body: JSON.stringify({text: txt})});
  refresh();
  setTimeout(() => openThread(curThread, true), 1200);
  setTimeout(() => openThread(curThread, true), 6000);
}
// ⚡ szybka odpowiedź „TAK” — tekst z configa flow (meta.quick_yes) lub domyślny
function quickYesText(){
  const cfg = (lastFlow && lastFlow.config) || (lastSponsor && lastSponsor.config) || null;
  return (cfg && cfg.meta && cfg.meta.quick_yes)
    || 'Zgadzam się — realizuj. O nic już nie pytaj i nic nie potwierdzaj (żadnych pytań ani potwierdzeń).';
}
async function sendQuickYes(tid){
  if(!tid) return;
  await fetch('/api/thread/'+tid+'/post', {method:'POST', body: JSON.stringify({text: quickYesText()})});
  refresh();
  setTimeout(() => openThread(tid, true), 1200);
  setTimeout(() => openThread(tid, true), 6000);
}
async function sendQuickTaskYes(){
  if(!curTask) return;
  await fetch('/api/task/'+curTask+'/comment', {method:'POST', body: JSON.stringify({text: quickYesText()})});
  refresh();
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
const TABS = ['overview','sponsor','tasks','flow','team','chat','live','firma'];
function showTab(name, push){
  if(!TABS.includes(name)) name = 'overview';
  document.querySelectorAll('.tabpage').forEach(el => el.classList.toggle('on', el.id === 'tab-'+name));
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  if(push !== false) history.replaceState(null, '', '#'+name+filterParams()); // hash zachowuje filtry przy zmianie zakładki
  if(name === 'firma') renderFirma();
  if(name === 'flow') showFlowView(flowView, false);
  if(name === 'sponsor') renderSponsor();
}
// ===== 🎯 TEMATY — widok opiekuna (milestone'y + decyzje + zgody + backlog) =====
let lastSponsor = null;
async function renderSponsor(){
  let d;
  try{ const r = await fetch('/api/sponsor'); d = await r.json(); }catch(e){ return; }
  lastSponsor = d;
  const acts = d.actions_def || {};
  document.getElementById('spDecisions').innerHTML = (d.decisions||[]).map(t => {
    const btns = (t.available_actions||[]).map(a => { const ad = acts[a]||{};
      return `<button class="tbtn" onclick="event.stopPropagation();flowAction('${esc(t.id)}','${esc(a)}')">${ad.icon||''} ${esc(ad.label||a)}</button>`; }).join('');
    return `<div class="dec-card ${t.status==='ESCALATED'?'esc':''}" onclick="openTask('${esc(t.id)}')">
      <div><b>${esc(t.id)}</b> ${sbadge(t.status)} ${pbadge(t.project_id)} <span class="prio prio-${t.priority||'P2'}">${t.priority||'P2'}</span></div>
      <div style="font-size:12px;margin:3px 0">${esc((t.title_sponsor||t.title||'').slice(0,110))}</div>
      ${t.substep?`<div class="muted" style="font-size:11px">substep: ${esc(t.substep)}</div>`:''}
      ${t.review_notes?`<div class="muted" style="font-size:11px">📝 ${esc(t.review_notes.slice(0,140))}</div>`:''}
      <div class="muted" style="font-size:11px">⏳ ${fmtDur(t.age)} · ${esc(t.persona||t.role||'')} · temat: ${esc(t.milestone||'')}${t.pr?(' · PR !'+t.pr):''}</div>
      <div>${btns}</div>
    </div>`;
  }).join('') || '<span class="muted">nic nie czeka na decyzję ✅</span>';
  document.getElementById('spApprovals').innerHTML = (d.approvals||[]).map(a =>
    `<div class="dec-card esc" style="cursor:default;animation:none"><b>${esc(a.id)}</b> <span class="sbadge" style="background:#b08800">${esc(a.kind)}</span>
     <div class="muted" style="font-size:11px">milestone: ${esc(a.milestone)} · ${a.created_at?new Date(a.created_at*1000).toLocaleString():''}</div>
     <div style="font-size:11px">${esc(String(a.payload||'').slice(0,200))}</div></div>`).join('')
    || '<span class="muted">brak oczekujących zgód</span>';
  document.getElementById('spMilestones').innerHTML = (d.milestones||[]).map(m => {
    const per = Object.entries(m.per_status||{}).sort((a,b)=>b[1]-a[1]).map(([st,n]) =>
      `<span class="sbadge" style="background:${ST_COLOR[st]||'#6e7681'};font-size:9px;margin:0 3px 2px 0">${esc(st)} ${n}</span>`).join('');
    const pct = Math.round((m.progress||0)*100);
    return `<div class="mscard ${m.status==='CLOSED'?'closed':''}" style="border-left-color:${projColor(m.project_id||'mikowhy-teams')}" onclick="openMilestone('${esc(m.id)}')">
      <div><b>🎯 ${esc(m.id)}</b> <span class="sbadge" style="background:${m.status==='OPEN'?'#1f6feb':(m.status==='CLOSED'?'#2ea043':'#b08800')}">${esc(m.status)}</span> ${pbadge(m.project_id)}
        <span class="meta">· gałąź <code>${esc(m.branch||'')}</code> · zadań ${m.tasks_total} · zmergowanych ${m.merged}</span></div>
      <div class="goal">${esc(((m.title||'') + ' — ' + (m.goal||''))).slice(0,180)}</div>
      <div class="pbar"><i style="width:${pct}%"></i></div>
      <div class="meta">${pct}% zamknięte · ${per}</div>
      ${m.blockers && m.blockers.length ? `<div class="blk">⚠ bloki: ${m.blockers.map(b=>esc(b.id)+' ['+esc(b.status)+']').join(', ')}</div>` : ''}
    </div>`;
  }).join('') || '<span class="muted">brak tematów — dodaj pierwszy przyciskiem ➕ nowy temat</span>';
  document.getElementById('spBacklog').innerHTML = (d.backlog||[]).map(t =>
    `<div class="brow" onclick="openTask('${esc(t.id)}')"><span class="prio prio-${t.priority||'P2'}">${t.priority||'P2'}</span>
     <b>${esc(t.id)}</b> <span>${esc((t.title_sponsor||t.title||'').slice(0,90))}</span>
     <span class="muted">${esc(t.persona||t.role||'')} · ${pbadge(t.project_id)} · temat ${esc(t.milestone||'')} · ${fmtDur(t.age)}</span></div>`).join('')
    || '<span class="muted">pusty backlog</span>';
  const nd = document.getElementById('navDec');
  if(nd) nd.textContent = (d.decisions||[]).length ? ' 🚨' + (d.decisions||[]).length : '';
}
function openMilestone(mid){
  const d = lastSponsor; if(!d) return;
  const m = (d.milestones||[]).find(x => x.id === mid); if(!m) return;
  curTask = null; curThread = null;
  document.getElementById('detail').classList.add('open');
  document.getElementById('detailBody').dataset.key = 'ms|' + mid + '|' + (m.tasks||[]).length;
  document.getElementById('detailBody').innerHTML = `<h3>🎯 ${esc(m.id)} <span class="sbadge" style="background:${m.status==='OPEN'?'#1f6feb':(m.status==='CLOSED'?'#2ea043':'#b08800')}">${esc(m.status)}</span></h3>
    <div class="kv"><b>tytuł:</b> ${esc(m.title||'')}</div>
    <div class="kv"><b>cel (o co chodzi):</b> ${esc(m.goal||'')}</div>
    <div class="kv"><b>gałąź:</b> <code>${esc(m.branch||'')}</code> · projekt: ${pbadge(m.project_id)} · utworzony: ${m.created_at?new Date(m.created_at*1000).toLocaleString():''}</div>
    <div class="kv"><b>postęp:</b> ${m.merged}/${m.tasks_total} zmergowane (${Math.round((m.progress||0)*100)}%) · otwartych: ${m.open_tasks}</div>
    <h2>Zadania tematu (${(m.tasks||[]).length})</h2>
    <div class="histbox">${(m.tasks||[]).map(t=>`<div onclick="openTask('${esc(t.id)}')" style="cursor:pointer">
      <b>${esc(t.id)}</b> ${sbadge(t.status)} <span class="prio prio-${t.priority||'P2'}">${t.priority||'P2'}</span> ${esc((t.title_sponsor||t.title||'').slice(0,70))}<br>
      <span class="muted">${esc(t.persona||t.role||'')} · ${esc(t.substep||'')}${t.review_notes?' · 📝 '+esc(t.review_notes.slice(0,80)):''} · ${fmtDur(t.age)}</span>
      ${t.acceptance_criteria?`<div class="muted" style="font-size:10px">AC: ${esc(t.acceptance_criteria.slice(0,150))}</div>`:''}
    </div>`).join('')||'<span class="muted">brak zadań</span>'}</div>
    <button class="tbtn" onclick="showTab('flow')">🌊 zobacz w Flow</button>`;
}
function openNewMilestone(){
  curTask = null; curThread = null;
  document.getElementById('detail').classList.add('open');
  document.getElementById('detailBody').dataset.key = 'newms';
  document.getElementById('detailBody').innerHTML = `<h3>➕ Nowy temat (milestone)</h3>
    <div class="kv muted" style="font-size:11px">Temat = kontener na zadania. Nazwij tak, żeby było wiadomo o co chodzi (np. FIRMA-011).</div>
    <div class="kv"><b>ID:</b> <input id="msId" style="background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:4px;width:180px" placeholder="FIRMA-011"></div>
    <div class="kv"><b>tytuł:</b> <input id="msTitle" style="background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:4px;width:100%" placeholder="krótko, po ludzku — o co chodzi"></div>
    <div class="kv"><b>cel (goal):</b><textarea id="msGoal" style="width:100%;height:80px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;font-family:inherit;border-radius:6px;padding:6px" placeholder="co ma być osiągnięte — jak poznamy, że temat jest domknięty"></textarea></div>
    <div class="kv"><b>gałąź:</b> <input id="msBranch" style="background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:4px;width:240px" placeholder="feat/firma-011 (puste = auto)"></div>
    <button class="ok" onclick="saveMilestone()">➕ Utwórz temat</button>
    <div id="msMsg"></div>`;
}
async function saveMilestone(){
  const body = {id: document.getElementById('msId').value.trim(), title: document.getElementById('msTitle').value.trim(),
                goal: document.getElementById('msGoal').value.trim(), branch: document.getElementById('msBranch').value.trim()};
  const msg = document.getElementById('msMsg');
  try{
    const r = await fetch('/api/milestone', {method:'POST', body: JSON.stringify(body)});
    const j = await r.json();
    if(r.ok && j.ok){ msg.innerHTML = '<div class="flowOk">✅ temat utworzony: ' + esc(j.id) + ' (gałąź ' + esc(j.branch) + ')</div>'; renderSponsor(); refresh(); }
    else { msg.innerHTML = '<div class="flowErr">❌ ' + esc(j.error||'błąd') + '</div>'; }
  }catch(e){ msg.innerHTML = '<div class="flowErr">❌ ' + esc(String(e)) + '</div>'; }
}
// ===== 🌊 FLOW: diagram przepływu pracy budowany z configa (engine/flow) =====
let lastFlow = null;
// filtr etapów flow (localStorage); defaultowo ukryte = etapy w pełni pokryte ukrytymi statusami (np. done przy MERGED off)
let flowHiddenStages = null;
function getFlowHiddenStages(){
  if(flowHiddenStages === null){
    const raw = localStorage.getItem('flowHiddenStages');
    flowHiddenStages = new Set(raw !== null ? JSON.parse(raw) : []);
  }
  return flowHiddenStages;
}
function toggleFlowStage(sid){ const h = getFlowHiddenStages(); if(h.has(sid)) h.delete(sid); else h.add(sid); persistFilters(); renderFlow(); }
function soloFlowStage(sid){ const all = (lastFlow ? lastFlow.stages.map(s => s.id) : []); flowHiddenStages = new Set(all.filter(x => x !== sid)); persistFilters(); renderFlow(); }
function resetFlowStages(){ flowHiddenStages = new Set(); persistFilters(); renderFlow(); }
async function renderFlow(){
  const svg = document.getElementById('flowSvg');
  let f;
  try{ const r = await fetch('/api/flow'); f = await r.json(); }catch(e){ return; }
  lastFlow = f;
  const cfg = f.config, L = cfg.layout || {}, T = cfg.theme || {};
  const lanes = L.lanes || {};
  const laneKeys = Object.keys(lanes).sort((a,b)=>(lanes[a].y||0)-(lanes[b].y||0));
  const nodeW = L.nodeW||210, gap = L.arrowGap||34, laneH = L.laneHeight||250, laneGap = L.laneGap||74;
  const maxChips = L.maxChips||7, chipH = L.chipH||30;
  const stages = f.stages || [];
  // filtr etapów: chipsy nad diagramem; przy pierwszym użyciu ukryj etapy w pełni pokryte ukrytymi statusami
  if(localStorage.getItem('flowHiddenStages') === null && stages.length){
    const h0 = getFlowHiddenStages();
    for(const st of stages){ if((st.statuses||[]).length && st.statuses.every(s => hiddenSts.has(s))) h0.add(st.id); }
    localStorage.setItem('flowHiddenStages', JSON.stringify([...h0]));
  }
  const fh = getFlowHiddenStages();
  const stageBar = document.getElementById('flowStageFilters');
  if(stageBar){
    stageBar.innerHTML = stages.map(st => {
      const off = fh.has(st.id);
      return `<button class="pchip ${off?'off':''}" style="--c:${st.color}" title="klik = ukryj/pokaż etap · dwuklik = SOLO (tylko ten etap)" ondblclick="soloFlowStage('${esc(st.id)}')" onclick="toggleFlowStage('${esc(st.id)}')">${st.icon} ${esc(st.label)} <span class="muted">${st.count}</span></button>`;
    }).join('') + `<button class="pchip reset" onclick="resetFlowStages()">✖ wszystkie etapy</button>`;
  }
  const visStages = stages.filter(s => !fh.has(s.id));
  const byLane = {};
  for(const st of visStages){ (byLane[st.lane] = byLane[st.lane]||[]).push(st); }
  for(const k in byLane) byLane[k].sort((a,b)=>a.order-b.order);
  const maxN = Math.max(1, ...laneKeys.map(k=>(byLane[k]||[]).length));
  const W = 16 + maxN*(nodeW+gap) + 8;
  const laneY = {}; let yAcc = 10;
  for(const k of laneKeys){ laneY[k] = yAcc; yAcc += laneH + laneGap; }
  const H = yAcc - laneGap + 10;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  const pos = {};
  let s = '<defs>' +
    '<marker id="flowArrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="' + (T.arrow||'#484f58') + '"/></marker>' +
    '<marker id="flowArrowHot" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="' + (T.arrowHot||'#b08800') + '"/></marker>' +
    '</defs>';
  for(const k of laneKeys){
    const y = laneY[k];
    s += '<g class="lane"><rect class="laneBg" x="6" y="' + y + '" width="' + (W-12) + '" height="' + laneH + '" rx="12"/>' +
         '<text class="laneLabel" x="14" y="' + (y+16) + '">' + esc(lanes[k].label||k) + '</text></g>';
  }
  for(const k of laneKeys){
    (byLane[k]||[]).forEach((st, i) => {
      const x = 16 + i*(nodeW+gap), y = laneY[k] + 26, w = nodeW, h = laneH - 36;
      pos[st.id] = {x:x, y:y, w:w, h:h};
      const over = st.overdue ? ' · <tspan fill="' + (T.overdue||'#f85149') + '">⚠ ' + st.overdue + ' po SLA</tspan>' : '';
      s += '<g class="stage ' + (st.hot && st.count ? 'hot' : '') + '">' +
        '<rect class="box" x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="12" stroke="' + st.color + '"/>' +
        '<text class="ttl" x="' + (x+10) + '" y="' + (y+22) + '" font-size="15" onclick="openStageInfo(\\'' + esc(st.id) + '\\')">' + st.icon + ' <tspan font-size="12" font-weight="bold" fill="' + (T.nodeTitle||'#c9d1d9') + '">' + esc(st.label) + '</tspan></text>' +
        '<circle cx="' + (x+w-16) + '" cy="' + (y+18) + '" r="10" fill="' + st.color + '"/><text class="cnt" x="' + (x+w-16) + '" y="' + (y+22) + '" font-size="10" text-anchor="middle">' + st.count + '</text>' +
        '<text x="' + (x+10) + '" y="' + (y+36) + '" font-size="9" fill="' + (T.muted||'#8b949e') + '">' + esc((st.statuses||[]).join(' · ')) + over + '</text>';
      const chips = st.tasks.slice(0, maxChips);
      const PIC = {'P0':'🔥','P1':'⚡','P2':'•','P3':'🐢'};
      chips.forEach((t, ci) => {
        const cx = x+8, cy = y + 44 + ci*(chipH+5);
        const acts = t.available_actions || [];
        const clk = (t.status==='RUNNING' && t.started_at)
          ? '<tspan class="clk" data-started="' + t.started_at + '"> ⏱' + fmtDur(Date.now()/1000 - t.started_at) + '</tspan>'
          : (t.overdue ? ' <tspan fill="' + (T.overdue||'#f85149') + '">⚠' + fmtDur(t.age) + '</tspan>' : ' <tspan class="who">' + fmtDur(t.age) + '</tspan>');
        s += '<g class="chip ' + (t.overdue?'overdue':'') + '" transform="translate(' + cx + ',' + cy + ')" onclick="openTask(\\'' + esc(t.id) + '\\')">' +
          '<rect width="' + (w-16) + '" height="' + chipH + '" rx="6"/>' +
          '<text x="6" y="13" font-size="10"><tspan>' + (PIC[t.priority]||'') + '</tspan> <tspan class="cid" fill="' + st.color + '">' + esc(t.id) + '</tspan>' + clk + '</text>' +
          '<text class="who" x="6" y="24" font-size="8">' + esc((t.persona||t.role||'').slice(0,16)) + (t.pr ? (' · !'+t.pr) : '') + '</text>';
        acts.forEach((a, ai) => {
          const ad = (f.actions||{})[a] || {};
          s += '<text class="act" x="' + (w-16-8-(acts.length-1-ai)*17) + '" y="17" font-size="12" onclick="event.stopPropagation();flowAction(\\'' + esc(t.id) + '\\',\\'' + esc(a) + '\\')"><title>' + esc(ad.label||a) + '</title>' + (ad.icon||'▶') + '</text>';
        });
        s += '</g>';
      });
      if(st.tasks.length > chips.length){
        s += '<text class="who" x="' + (x+8) + '" y="' + (y+44+chips.length*(chipH+5)+12) + '" font-size="9" fill="' + (T.muted||'#8b949e') + '" style="cursor:pointer" onclick="openStageInfo(\\'' + esc(st.id) + '\\')">+' + (st.tasks.length-chips.length) + ' więcej… (klik nagłówek)</text>';
      }
      s += '</g>';
    });
  }
  for(const tr of (f.transitions||[])){
    const a = pos[tr.from], b = pos[tr.to];
    if(!a || !b) continue;
    const x1 = a.x + a.w, y1 = a.y + a.h/2, x2 = b.x, y2 = b.y + b.h/2;
    const hot = tr.kind === 'sponsor';
    let d;
    if(Math.abs(y1-y2) < 4 && x2 > x1) d = 'M ' + x1 + ' ' + y1 + ' L ' + (x2-8) + ' ' + y2;
    else d = 'M ' + x1 + ' ' + y1 + ' C ' + (x1+46) + ' ' + y1 + ', ' + Math.max(x2-46, x1+10) + ' ' + y2 + ', ' + (x2-8) + ' ' + y2;
    s += '<path class="arrow ' + (hot?'hot':'') + ' ' + (tr.kind==='auto'?'anim':'') + '" d="' + d + '" marker-end="url(#' + (hot?'flowArrowHot':'flowArrow') + ')"/>' +
         '<text class="arrowLbl ' + (hot?'hot':'') + '" x="' + ((x1+x2)/2) + '" y="' + ((y1+y2)/2 - 6) + '">' + (tr.icon||'') + ' ' + esc(tr.label||'') + '</text>';
  }
  svg.innerHTML = s;
  const hot = (f.stats && f.stats.hot) || 0, over = (f.stats && f.stats.overdue) || 0;
  document.getElementById('flowMeta').innerHTML =
    'etapy: <b>' + f.stages.length + '</b>' + (fh.size ? ' (ukryte: <b style="color:#f85149">' + fh.size + '</b>)' : '') + ' · zadania: <b>' + f.stats.total + '</b> · hot: <b style="color:' + (hot?'#d29922':'#3fb950') + '">' + hot + '</b> · po SLA: <b style="color:' + (over?'#f85149':'#3fb950') + '">' + over + '</b>' +
    ' · override configu: <b>' + (f.override_active ? '✅ aktywny (.agents/flow/flow.config.json)' : 'nie (default)') + '</b>' +
    (f.errors && f.errors.length ? ' · <b style="color:#f85149">⚠ błędy configu: ' + f.errors.length + '</b>' : '');
}
function openStageInfo(sid){
  const f = lastFlow; if(!f) return;
  const st = f.stages.find(x => x.id === sid); if(!st) return;
  curTask = null; curThread = null;
  document.getElementById('detail').classList.add('open');
  document.getElementById('detailBody').dataset.key = 'flowstage|' + sid + '|' + st.count;
  document.getElementById('detailBody').innerHTML = '<h3>' + st.icon + ' ' + esc(st.label) + '</h3>' +
    '<div class="kv"><b>etap:</b> ' + esc(st.id) + ' · tor: ' + esc(st.lane) + ' · statusy: ' + esc((st.statuses||[]).join(', ')) + '</div>' +
    '<div class="kv"><b>zadań:</b> ' + st.count + (st.overdue ? ' · <span style="color:#f85149">⚠ ' + st.overdue + ' po SLA</span>' : '') + (st.sla_s ? ' · SLA: ' + fmtDur(st.sla_s) : '') + '</div>' +
    '<div class="kv muted">' + esc(st.description||'') + '</div>' +
    '<div class="kv"><b>akcje w etapie:</b> ' + (st.actions||[]).map(a => { const ad = (f.actions||{})[a]||{}; return (ad.icon||'') + ' ' + esc(ad.label||a); }).join(' · ') + '</div>' +
    '<h2>Zadania w etapie (' + st.count + ')</h2>' +
    '<div class="histbox">' + (st.tasks.map(t =>
      '<div onclick="openTask(\\'' + esc(t.id) + '\\')" style="cursor:pointer">' +
      '<b style="color:' + st.color + '">' + esc(t.id) + '</b> ' + esc((t.title||'').slice(0,60)) + '<br>' +
      '<span class="muted">' + esc(t.status) + ' · ' + esc(t.priority||'P2') + ' · ' + esc(t.persona||t.role||'') + ' · ' + pbadge(t.project_id) + ' · ' + (t.overdue ? '<span style="color:#f85149">⚠</span> ' : '') + fmtDur(t.age) + '</span><br>' +
      (t.available_actions||[]).map(a => { const ad = (f.actions||{})[a]||{}; return '<button class="tbtn" onclick="event.stopPropagation();flowAction(\\'' + esc(t.id) + '\\',\\'' + esc(a) + '\\')">' + (ad.icon||'') + ' ' + esc(ad.label||a) + '</button>'; }).join('') +
      '</div>').join('') || '<span class="muted">pusto</span>') + '</div>';
}
async function flowAction(taskId, actionId){
  const ad = ((lastFlow && lastFlow.actions) || (lastSponsor && lastSponsor.actions_def) || {})[actionId] || {};
  const payload = {};
  if(ad.requires_reason){
    const txt = prompt(ad.text_label || (ad.label + ':'), '');
    if(txt === null) return;
    payload.text = txt; payload.reason = txt;
  }
  if(ad.type === 'set_priority'){
    const cur = ((lastState && lastState.tasks.find(t => t.id === taskId)) || {}).priority || 'P2';
    const p = prompt('Priorytet (' + (ad.choices || ['P0','P1','P2','P3']).join(', ') + '):', cur);
    if(p === null) return;
    payload.priority = p.trim();
  }
  if(ad.confirm && !confirm(ad.confirm)) return;
  try{
    const r = await fetch('/api/flow/action', {method:'POST', body: JSON.stringify(Object.assign({task_id: taskId, action: actionId}, payload))});
    const j = await r.json();
    if(!r.ok || !j.ok){ alert('❌ ' + (j.error || 'błąd akcji')); return; }
    refresh(); renderFlow();
  }catch(e){ alert('❌ ' + e); }
}
// ===== ⚙️ zarządzanie flow configem (override .agents/flow/flow.config.json) =====
async function openFlowCfg(){
  curTask = null; curThread = null;
  let c;
  try{ const r = await fetch('/api/flow/config'); c = await r.json(); }catch(e){ alert('❌ ' + e); return; }
  document.getElementById('detail').classList.add('open');
  document.getElementById('detailBody').dataset.key = 'flowcfg';
  const src = (c.override_raw != null) ? c.override_raw : c.default_raw;
  const errs = c.errors || [];
  document.getElementById('detailBody').innerHTML = '<h3>⚙️ Config flow (JSON)</h3>' +
    '<div class="kv muted" style="font-size:11px">Edytuj definicję przepływu: etapy <b>stages</b> (id/lane/statuses/kolor/SLA/akcje), przejścia <b>transitions</b>, akcje Sponsora <b>actions</b> (type/allowed_from/target_status), wygląd <b>layout</b> i <b>theme</b>. Zapis tworzy override <b>.agents/flow/flow.config.json</b> (deep-merge z default; listy zastępują). Błędy walidacji (np. przejście niedozwolone przez VALID_TRANSITIONS) blokują zapis.</div>' +
    '<div class="kv">stan: ' + (c.override_active ? 'override <b>AKTYWNY</b>' : 'default (engine/flow/default_flow.json)') + ' · błędy bieżącego configu: <b style="color:' + (errs.length?'#f85149':'#3fb950') + '">' + errs.length + '</b></div>' +
    (errs.length ? '<div class="flowErr">' + esc(errs.join('\\n')) + '</div>' : '') +
    '<textarea id="flowCfgBox" spellcheck="false">' + esc(src) + '</textarea>' +
    '<div><button class="ok" onclick="saveFlowCfg()">💾 Zapisz override</button> ' +
    '<button onclick="resetFlowCfg()">🗑️ Reset do defaultu</button> ' +
    '<button onclick="closeDetail()">✖ Zamknij</button></div>' +
    '<div id="flowCfgMsg"></div>';
}
async function saveFlowCfg(){
  const el = document.getElementById('flowCfgBox'); if(!el) return;
  const msg = document.getElementById('flowCfgMsg');
  try{
    const r = await fetch('/api/flow/config', {method:'POST', body: JSON.stringify({config: el.value})});
    const j = await r.json();
    if(r.ok && j.ok){ msg.innerHTML = '<div class="flowOk">✅ zapisano override — diagram przeładowany</div>'; renderFlow(); refresh(); }
    else { msg.innerHTML = '<div class="flowErr">❌ odrzucono:\\n' + esc(((j.errors||[j.error||'błąd']).join('\\n'))) + '</div>'; }
  }catch(e){ msg.innerHTML = '<div class="flowErr">❌ ' + esc(String(e)) + '</div>'; }
}
async function resetFlowCfg(){
  if(!confirm('Usunąć override .agents/flow/flow.config.json i wrócić do default_flow.json?')) return;
  await fetch('/api/flow/config/reset', {method:'POST', body:'{}'});
  openFlowCfg(); renderFlow(); refresh();
}
// ===== 🌊 FLOW — pod-zakładki: ⚙️ flow pracy / 🏗️ flow stanowisk =====
let flowView = (function(){ const h = (location.hash||'').match(/(?:^|[?&])fv=([^&]*)/); const v = h ? h[1] : (localStorage.getItem('flowView') || 'work'); return (v === 'org') ? 'org' : 'work'; })();
function showFlowView(v, push){
  flowView = (v === 'org') ? 'org' : 'work';
  localStorage.setItem('flowView', flowView);
  document.querySelectorAll('#flowTabs button').forEach(b => b.classList.toggle('on', b.dataset.fview === flowView));
  document.getElementById('flowWorkView').style.display = flowView === 'work' ? '' : 'none';
  document.getElementById('flowOrgView').style.display = flowView === 'org' ? '' : 'none';
  if(push !== false) history.replaceState(null, '', '#flow' + filterParams() + (flowView === 'org' ? (filterParams() ? '&' : '?') + 'fv=org' : ''));
  if(flowView === 'org') renderOrg(); else renderFlow();
}
// ===== 🏗️ FLOW STANOWISK: org chart z configa (engine/flow/default_org.json + override) =====
let lastOrg = null;
async function renderOrg(){
  const svg = document.getElementById('orgSvg');
  let o;
  try{ const r = await fetch('/api/org'); o = await r.json(); }catch(e){ return; }
  lastOrg = o;
  const meta = document.getElementById('orgMeta');
  if(meta) meta.textContent = `👥 ${o.stats.agents} agentów + ${o.stats.humans} ludzi · max głębokość ${o.stats.depth_max} · pracuje teraz ${o.stats.busy} · ${o.override_active ? 'override AKTYWNY (.agents/flow/org.config.json)' : 'default (engine/flow/default_org.json)'}` + (o.errors && o.errors.length ? ` · ⚠ błędy configu: ${o.errors.length}` : '');
  const cfg = o.config || {}, L = cfg.layout || {}, T = cfg.theme || {};
  const nodes = o.nodes || [];
  const nodeW = L.nodeW || 196, nodeH = L.nodeH || 92, hGap = L.hGap || 20, vGap = L.vGap || 70, pad = L.pad || 18;
  // poziomy = depth; węzły centrowane per poziom
  const byDepth = {};
  for(const n of nodes){ (byDepth[n.depth] = byDepth[n.depth] || []).push(n); }
  const depths = Object.keys(byDepth).map(Number).sort((a,b)=>a-b);
  const maxN = Math.max(1, ...depths.map(d => byDepth[d].length));
  const W = pad*2 + maxN*nodeW + (maxN-1)*hGap;
  const H = pad*2 + depths.length*nodeH + (depths.length-1)*vGap;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  const pos = {};
  for(const d of depths){
    const row = byDepth[d];
    const rowW = row.length*nodeW + (row.length-1)*hGap;
    let x = pad + Math.max(0, (maxN*nodeW + (maxN-1)*hGap - rowW)/2);
    const y = pad + d*(nodeH + vGap);
    for(const n of row){ pos[n.id] = {x, y, cx: x + nodeW/2, cy: y + nodeH/2}; x += nodeW + hGap; }
  }
  const byId = {}; for(const n of nodes) byId[n.id] = n;
  let s = '';
  // etykiety poziomów (głębokości)
  for(const d of depths){
    const lbl = d === 0 ? '👑 zarząd' : 'poziom ' + d;
    s += `<text class="depthLbl" x="4" y="${pos[byDepth[d][0].id].y + 12}">${lbl}</text>`;
  }
  // krawędzie szef -> podwładny (łamana: dół szefa -> środek -> góra dziecka)
  for(const n of nodes){
    if(!n.reports_to || !pos[n.reports_to]) continue;
    const p = pos[n.reports_to], c = pos[n.id];
    const my = (p.y + nodeH + c.y) / 2;
    s += `<path class="edge" d="M ${p.cx} ${p.y + nodeH} L ${p.cx} ${my} L ${c.cx} ${my} L ${c.cx} ${c.y}"/>`;
  }
  // węzły (osoby)
  for(const n of nodes){
    const p = pos[n.id]; if(!p) continue;
    const col = (n.persona && n.persona.color) || (n.kind === 'human' ? '#b08800' : '#8b949e');
    const root = n.depth === 0;
    const busy = n.busy ? ' busy' : '';
    const nm = esc(n.name || n.label);
    const rl = esc(n.position || n.title || n.role || '');
    const stLine = n.current
      ? `🔨 ${esc(n.current.id)} ⏱ ${fmtDur(n.current.elapsed||0)}`
      : (n.kind === 'agent' ? `${n.tasks} zad · ${n.merged} zmerg.` : (n.title || ''));
    const boss = n.reports_to && byId[n.reports_to] ? `szef: ${esc(byId[n.reports_to].name || byId[n.reports_to].label)}` : 'root (nikt wyżej)';
    const kids = (n.reports || []).map(rid => byId[rid] ? byId[rid].name || byId[rid].label : rid);
    s += `<g class="person${root?' root':''}${busy}" onclick="openPerson('${esc(n.id)}')" transform="translate(${p.x},${p.y})">
      <rect class="box" width="${nodeW}" height="${nodeH}" rx="10"/>
      <text class="av" x="12" y="26">${n.icon || '🧑'}</text>
      <circle class="dot" cx="${nodeW-14}" cy="14" r="5" title="${n.busy ? 'pracuje teraz' : 'wolny/a'}"/>
      <text class="nm" x="40" y="24" fill="${col}">${nm}</text>
      <text class="rl" x="12" y="44">${rl.length > 26 ? rl.slice(0,25)+'…' : rl}</text>
      <text class="st" x="12" y="${nodeH-24}">${esc(stLine).slice(0, 30)}</text>
      <text class="rl" x="12" y="${nodeH-10}" fill="${T.muted||'#8b949e'}">${esc(boss).slice(0, 34)}${kids.length ? ' · podwładni: ' + esc(kids.join(', ')).slice(0, 40) : ''}</text>
    </g>`;
  }
  svg.innerHTML = s;
}
function openPerson(nodeId){
  const o = lastOrg; if(!o) return;
  const n = (o.nodes||[]).find(x => x.id === nodeId); if(!n) return;
  const byId = {}; for(const x of (o.nodes||[])) byId[x.id] = x;
  curTask = null; curThread = null;
  document.getElementById('detail').classList.add('open');
  document.getElementById('detailBody').dataset.key = 'person|' + nodeId + '|' + (o.ts || 0);
  const col = (n.persona && n.persona.color) || '#b08800';
  const boss = n.reports_to && byId[n.reports_to];
  const kids = (n.reports || []).map(rid => byId[rid]).filter(Boolean);
  const chips = Object.entries(n.by_status||{}).sort((a,b)=>b[1]-a[1]).map(([st,c]) =>
    `<span class="sbadge" style="background:${ST_COLOR[st]||'#6e7681'};font-size:9px;margin:0 3px 2px 0">${esc(st)} ${c}</span>`).join('');
  const cur = n.current ? `<div class="cur" onclick="closeDetail();openTask('${esc(n.current.id)}')" style="cursor:pointer">🔨 <b>${esc(n.current.id)}</b> ${esc((n.current.title||'').slice(0,80))} <span class="prio prio-${n.current.priority||'P2'}">${n.current.priority||'P2'}</span> <span class="clock" data-started="${n.current.started_at||0}">⏱ ${fmtDur(n.current.elapsed||0)}</span> ${pbadge(n.current.project_id)}</div>` : '<div class="cur muted">— wolny/a (brak bieżącego zadania) —</div>';
  document.getElementById('detailBody').innerHTML = `
    <h3>${n.icon||'🧑'} <span style="color:${col}">${esc(n.name||n.label)}</span> <span class="muted" style="font-size:12px">${esc(n.title||'')} · rola: <code>${esc(n.role||'—')}</code></span></h3>
    ${n.persona && n.persona.backstory ? `<div class="story">„${esc(n.persona.backstory)}”</div>` : ''}
    <div class="kv"><b>stanowisko:</b> ${esc(n.position||'—')} ${n.persona?('· płeć: ' + esc(n.persona.gender)):''}</div>
    <div class="kv"><b>szef:</b> ${boss ? `<a href="#" onclick="openPerson('${esc(boss.id)}');return false">${boss.icon||''} ${esc(boss.name||boss.label)}</a> <span class="muted">(${esc(boss.title||boss.role||'')})</span>` : '👑 root — nikt wyżej'}</div>
    <div class="kv"><b>podwładni (${kids.length}):</b> ${kids.length ? kids.map(k => `<a href="#" onclick="openPerson('${esc(k.id)}');return false">${k.icon||''} ${esc(k.name||k.label)}</a>`).join(' · ') : '<span class="muted">brak (liść drzewa)</span>'}</div>
    <div class="kv"><b>bieżące:</b></div>${cur}
    <div class="kv"><b>zadania:</b> ${n.tasks} razem · ${n.merged} zmergowanych ${chips ? ' · ' + chips : ''}</div>
    ${n.description ? `<div class="kv muted" style="font-size:11px">${esc(n.description)}</div>` : ''}
    <div style="margin-top:8px">
      ${n.role ? `<button class="tbtn" onclick="startThread('${esc(n.role)}')">💬 rozmowa z ${esc(n.name||n.role)}</button> <button class="tbtn" onclick="openDuties('${esc(n.role)}')">📋 obowiązki</button>` : ''}
      <button class="tbtn" onclick="closeDetail()">✖ Zamknij</button>
    </div>`;
}
// ===== ⚙️ zarządzanie configem hierarchii (override .agents/flow/org.config.json) =====
async function openOrgCfg(){
  curTask = null; curThread = null;
  let c;
  try{ const r = await fetch('/api/org/config'); c = await r.json(); }catch(e){ alert('❌ ' + e); return; }
  document.getElementById('detail').classList.add('open');
  document.getElementById('detailBody').dataset.key = 'orgcfg';
  const src = (c.override_raw != null) ? c.override_raw : c.default_raw;
  const errs = c.errors || [];
  document.getElementById('detailBody').innerHTML = '<h3>⚙️ Config hierarchii stanowisk (JSON)</h3>' +
    '<div class="kv muted" style="font-size:11px">Edytuj <b>nodes</b>: każdy węzeł ma <b>id</b>, <b>kind</b> (agent|human), dla agenta <b>role</b> (musi istnieć w rejestrze), <b>reports_to</b> = id szefa (null tylko dla jednego root). Walidacja pilnuje: jeden root, brak cykli, reports_to istnieje, węzły dosięgalne z root. Zapis tworzy override <b>.agents/flow/org.config.json</b> (deep-merge z default; lista nodes zastępuje).</div>' +
    '<div class="kv">stan: ' + (c.override_active ? 'override <b>AKTYWNY</b>' : 'default (engine/flow/default_org.json)') + ' · błędy bieżącego configu: <b style="color:' + (errs.length?'#f85149':'#3fb950') + '">' + errs.length + '</b></div>' +
    (errs.length ? '<div class="flowErr">' + esc(errs.join('\\n')) + '</div>' : '') +
    '<textarea id="flowCfgBox" spellcheck="false">' + esc(src) + '</textarea>' +
    '<div><button class="ok" onclick="saveOrgCfg()">💾 Zapisz override</button> ' +
    '<button onclick="resetOrgCfg()">🗑️ Reset do defaultu</button> ' +
    '<button onclick="closeDetail()">✖ Zamknij</button></div>' +
    '<div id="flowCfgMsg"></div>';
}
async function saveOrgCfg(){
  const el = document.getElementById('flowCfgBox'); if(!el) return;
  const msg = document.getElementById('flowCfgMsg');
  try{
    const r = await fetch('/api/org/config', {method:'POST', body: JSON.stringify({config: el.value})});
    const j = await r.json();
    if(r.ok && j.ok){ msg.innerHTML = '<div class="flowOk">✅ zapisano override — hierarchia przeładowana</div>'; renderOrg(); refresh(); }
    else { msg.innerHTML = '<div class="flowErr">❌ odrzucono:\\n' + esc(((j.errors||[j.error||'błąd']).join('\\n'))) + '</div>'; }
  }catch(e){ msg.innerHTML = '<div class="flowErr">❌ ' + esc(String(e)) + '</div>'; }
}
async function resetOrgCfg(){
  if(!confirm('Usunąć override .agents/flow/org.config.json i wrócić do default_org.json?')) return;
  await fetch('/api/org/config/reset', {method:'POST', body:'{}'});
  openOrgCfg(); renderOrg(); refresh();
}
// ===== 📋 Obowiązki cykliczne (recurring) per pracownik — priorytet P3 (niżej niż bieżące) =====
async function openDuties(role){
  curTask = null; curThread = null;
  let list = [];
  try{ const r = await fetch('/api/recurring?role=' + encodeURIComponent(role)); list = await r.json(); }catch(e){}
  const m = (lastState && lastState.team || []).find(x => x.role === role) || {name: role, icon: '🧑', color: '#8b949e'};
  document.getElementById('detail').classList.add('open');
  document.getElementById('detailBody').dataset.key = 'duties|' + role + '|' + list.length + '|' + (lastState ? lastState.ts || 0 : 0);
  const rows = list.map(x => `<div class="ask">
    <b>${x.active ? '✅' : '⏸️'}</b> <span class="prio prio-${x.priority||'P3'}">${x.priority||'P3'}</span> <b>${esc(x.title)}</b>
    <div class="muted">${esc(x.schedule)} · ${esc(x.id)}${x.last_run ? ' · ostatnio ' + new Date(x.last_run*1000).toLocaleString() : ' · jeszcze nie wykonywane'}</div>
    <button class="tbtn" onclick="toggleRecurring('${esc(x.id)}', ${x.active ? 0 : 1})">${x.active ? '⏸ wyłącz' : '▶ włącz'}</button>
  </div>`).join('') || '<span class="muted">brak obowiązków cyklicznych</span>';
  document.getElementById('detailBody').innerHTML = `<h3>📋 Obowiązki ${m.icon || ''} ${esc(m.name)} <span class="muted" style="color:${m.color||'#8b949e'}">(${esc(role)})</span></h3>
    <div class="kv muted" style="font-size:11px">Zadania cykliczne mają priorytet <b>P3</b> — zawsze po bieżących sprawach (P0–P2) w kolejce.</div>
    ${rows}
    <h2>➕ Dodaj obowiązek</h2>
    <textarea id="recTitle" style="width:100%;height:44px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;font-family:inherit;border-radius:6px;padding:6px" placeholder="np. cotygodniowy przegląd health-reportu…"></textarea>
    <div style="margin:6px 0">
      <select id="recSched" style="background:#161b22;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:4px">
        <option value="daily">codziennie</option><option value="weekly" selected>co tydzień</option><option value="hourly">co godzinę</option>
      </select>
      <button onclick="addRecurring('${esc(role)}')">➕ Dodaj (P3)</button>
    </div>`;
}
async function addRecurring(role){
  const t = (document.getElementById('recTitle')||{}).value || '';
  const txt = t.trim(); if(!txt) return;
  const sch = (document.getElementById('recSched')||{}).value || 'weekly';
  await fetch('/api/recurring', {method:'POST', body: JSON.stringify({role: role, title: txt, schedule: sch, priority: 'P3'})});
  openDuties(role); refresh();
}
async function toggleRecurring(id, active){
  await fetch('/api/recurring/' + id + '/toggle', {method:'POST', body: JSON.stringify({active: !!active})});
  refresh();
  const key = (document.getElementById('detailBody').dataset.key || '');
  const m = key.match(/^duties\\|([^|]+)\\|/);
  if(m) openDuties(m[1]);
}
// ===== FIRMA: widok świata (osiedle biur, SVG top-down, animacje przepływu) =====
const SVGNS = 'http://www.w3.org/2000/svg';
let firmaWorld = null;       // ostatni /api/firma
let firmaView = null;        // {x,y,w,h} viewBox (pan/zoom)
let deskEls = {};            // taskId -> <g> (persistent — żeby CSS transition animował przejazd)
let firmaGate = null;        // {x,y} brama recepcji (= Sponsor) — do dymków rozmów
let firmaRenderSeq = 0;      // token renderu — ostatni wygrywa (ochrona przed wyścigiem klik + auto-refresh)
let roomByRole = {};         // role -> room (kolor osoby + pozycja pokoju)
const expandedZones = new Set();  // klucze office:zone rozwinięte po kliknięciu zbiorczego [+N]
function toggleFirmaFs(){
  const sec = document.getElementById('tab-firma');
  const on = sec.classList.toggle('fs');
  const b = document.getElementById('fsBtn');
  if(b) b.textContent = on ? '🗗 wyjdź z pełnego ekranu' : '⛶ pełny ekran';
  if(on && document.documentElement.requestFullscreen){ try{ document.documentElement.requestFullscreen().catch(()=>{}); }catch(e){} }
  if(!on && document.fullscreenElement){ try{ document.exitFullscreen().catch(()=>{}); }catch(e){} }
  setTimeout(() => { const svg = document.getElementById('estate'); if(firmaView && svg) svg.setAttribute('viewBox', firmaView.x + ' ' + firmaView.y + ' ' + firmaView.w + ' ' + firmaView.h); }, 60);
}
function svgEl(name, attrs, parent){
  const e = document.createElementNS(SVGNS, name);
  for(const k in attrs) e.setAttribute(k, attrs[k]);
  if(parent) parent.appendChild(e);
  return e;
}
function escXml(s){ return esc(s); }
let dimStatus = null;         // klik legendy -> podświetl tylko ten status (reszta przyciemniona)
let staticKey = null;         // cache warstwy statycznej mapy
async function renderFirma(){
  const mySeq = ++firmaRenderSeq;   // przestarzałe rendery (starszy fetch) są odrzucane niżej
  try{
    const r = await fetch('/api/firma'); firmaWorld = await r.json();
  }catch(e){ return; }
  if(mySeq !== firmaRenderSeq) return;  // wyścig: ktoś już renderuje nowszy stan — ten porzucamy
  const W = firmaWorld, cfg = W.config, C = cfg.colors, s2 = cfg.stage2 || {};
  roomByRole = {};
  const g = cfg.estate.grid, off = cfg.office, R = cfg.rooms, D = cfg.desks;
  const cols = Math.max(1, g.cols|0), gap = g.gap, margin = g.margin + 26; // +26 na ogrodzenie
  const ow = off.w;
  const visOffices = W.offices.filter(o => !hiddenProjs.has(o.project_id));
  const rows = Math.max(1, Math.ceil(visOffices.length / cols));
  // wysokość biura rośnie z liczbą pokoi (żeby WSZYSCY agenci projektu byli w środku)
  const zonesH = off.h - cfg.zones.alarm.rect[1];  // wysokość pasa stref od góry (alarm=150)
  const ohArr = visOffices.map(o => Math.max(off.h, R.y0 + 46 + Math.ceil(o.rooms.length / R.perRow) * (R.h + R.gap) + 12 + zonesH));
  const rowH = [], rowY = []; let yAcc = margin;
  for(let i = 0; i < rows; i++){ rowH[i] = Math.max(off.h, ...ohArr.slice(i*cols, (i+1)*cols)); rowY[i] = yAcc; yAcc += rowH[i] + gap; }
  const Wsvg = margin*2 + cols*ow + (cols-1)*gap;
  const Hsvg = yAcc - gap + margin + 110;
  if(!firmaView) firmaView = {x:-10, y:-10, w:Wsvg+20, h:Hsvg+20};
  const svg = document.getElementById('estate');
  svg.setAttribute('viewBox', `${firmaView.x} ${firmaView.y} ${firmaView.w} ${firmaView.h}`);
  // pozycje biur i pokojów — ZAWSZE ustawiane (nowy fetch = nowe obiekty; bez tego _ox byłoby undefined -> NaN)
  visOffices.forEach((o, oi) => {
    const col = oi % cols, row = Math.floor(oi / cols);
    o._ox = margin + col*(ow+gap); o._oy = rowY[row]; o._zoneOff = ohArr[oi] - off.h;
    o.rooms.forEach((room, ri) => {
      room._x = o._ox + R.x0 + (ri % R.perRow) * (R.w + R.gap);
      room._y = o._oy + R.y0 + 40 + Math.floor(ri / R.perRow) * (R.h + R.gap);
      roomByRole[room.role] = room;   // też przy cache hit (inaczej dymki rozmów by znikały)
    });
  });
  // wszystkie projekty ukryte filtrem — czytelny komunikat zamiast pustej mapy
  if(visOffices.length === 0){
    let st0 = document.getElementById('estateStatic');
    if(!st0){ st0 = svgEl('g', {id:'estateStatic'}, svg); }
    st0.innerHTML = `<text x="${Wsvg/2}" y="${Hsvg/2}" text-anchor="middle" font-size="16" fill="${C.muted}">🫥 wszystkie projekty ukryte filtrem — klik „✖ wszystkie” nad zakładkami, aby biura wróciły</text>`;
    staticKey = 'empty';
    const dl0 = document.getElementById('deskLayer'); if(dl0) dl0.innerHTML = '';
    const cl0 = document.getElementById('convLayer'); if(cl0) cl0.innerHTML = '';
    return;
  }
  // ===== warstwa statyczna (cache'owana — przebuduj tylko gdy zmieni się struktura świata/config) =====
  const key = JSON.stringify([visOffices.map(o=>[o.project_id,o.read_only,o.milestones.length,o.rooms.length]), cfg.version, s2]);
  let stLayer = document.getElementById('estateStatic');
  if(!stLayer){ stLayer = svgEl('g', {id:'estateStatic'}, svg); }
  if(key !== staticKey){
    staticKey = key;
    let s = '';
    // tło osiedla (trawa) + droga wokół
    s += `<rect x="-26" y="-26" width="${Wsvg+52}" height="${Hsvg+52}" rx="18" fill="#dccdab"/>`;
    s += `<rect x="0" y="0" width="${Wsvg}" height="${Hsvg}" fill="${C.bg}"/>`;
    // OGRODZENIE osiedla (zamknięte) — słupki + 2 poziome belki
    const fence = (x1,y1,x2,y2) => {
      let f = `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="#8b6b3d" stroke-width="4"/>`;
      f += `<line x1="${x1}" y1="${y1-5}" x2="${x2}" y2="${y2-5}" stroke="#a8845c" stroke-width="3"/>`;
      const len = Math.hypot(x2-x1, y2-y1), n = Math.max(2, Math.floor(len/34));
      for(let i=0;i<=n;i++){ const px = x1+(x2-x1)*i/n, py = y1+(y2-y1)*i/n;
        f += `<line x1="${px}" y1="${py-9}" x2="${px}" y2="${py+9}" stroke="#6e5433" stroke-width="5" stroke-linecap="round"/>`; }
      return f;
    };
    s += fence(-14,-14, Wsvg+14,-14) + fence(-14,Hsvg+14, Wsvg+14,Hsvg+14) + fence(-14,-14,-14,Hsvg+14) + fence(Wsvg+14,-14,Wsvg+14,Hsvg+14);
    // brama (przerwa w ogrodzeniu na dole) + szyld osiedla
    s += `<rect x="${Wsvg/2-90}" y="${Hsvg+6}" width="180" height="14" fill="${C.bg}"/>`;
    const gate = cfg.estate.gate, gx = Wsvg/2 - gate.w/2, gy = Hsvg + 18;
    firmaGate = {x: gx + gate.w/2, y: gy + gate.h/2};
    s += `<g><rect x="${gx-8}" y="${gy-10}" width="${gate.w+16}" height="${gate.h+20}" rx="10" fill="#3a2f1d" stroke="#8b6b3d" stroke-width="3"/>
      <rect x="${gx}" y="${gy}" width="${gate.w}" height="${gate.h}" rx="8" fill="${C.gate}" opacity="0.95"/>
      <text x="${gx+gate.w/2}" y="${gy+17}" text-anchor="middle" fill="#fff" font-size="12" font-weight="bold">🚪 ${esc(cfg.meta.name||'Osiedle')}</text>
      <text x="${gx+gate.w/2}" y="${gy+36}" text-anchor="middle" fill="#fff" font-size="11">${W.estate.host&&W.estate.host.dashboard_url?esc(W.estate.host.dashboard_url):'offline'}</text>
      <text x="${gx+gate.w/2}" y="${gy+53}" text-anchor="middle" fill="#cfe3ff" font-size="10">${W.estate.sys?('load '+W.estate.sys.load1+'/'+W.estate.sys.cpus+' · 🧠'+W.estate.sys.mem_used_pct+'%'):''}</text></g>`;
    // drogi między działkami + linie środkowe
    for(let i=1;i<cols;i++){ const x = margin + i*ow + (i-0.5)*gap - cfg.estate.road/2;
      s += `<rect x="${x}" y="0" width="${cfg.estate.road}" height="${Hsvg}" fill="${C.road}"/>`;
      if(s2.roadDashes){ const cx = x + cfg.estate.road/2;
        for(let y=10; y<Hsvg; y+=28) s += `<line x1="${cx}" y1="${y}" x2="${cx}" y2="${y+14}" stroke="#8b949e" stroke-width="2" opacity="0.5"/>`; }
    }
    // rośliny: drzewa + kwiaty TYLKO poza biurami (pas góra/dół osiedla + wzdłuż dróg)
    const flora = s2.flora || {};
    for(let i=0;i<(s2.treeCount||6);i++){
      const tx = 30 + ((i*97) % Math.max(1, Wsvg-60));
      const ty = (i % 2 === 0) ? (14 + ((i*31) % 22)) : (Hsvg - 16 - ((i*29) % 42));
      s += `<text x="${tx}" y="${ty}" font-size="20">${i%3===0?'🌲':'🌳'}</text>`;
    }
    for(let i=1;i<cols;i++){  // drzewka przy drogach pionowych między działkami
      const x = margin + i*ow + (i-0.5)*gap + cfg.estate.road/2;
      const y = 60 + ((i*53) % Math.max(1, Hsvg-160));
      s += `<text x="${x-8}" y="${y}" font-size="18">🌳</text>`;
    }
    for(let i=0;i<(flora.flowers||8);i++){
      const fx = 24 + ((i*53) % Math.max(1, Wsvg-48));
      const fy = (i % 2 === 0) ? (34 + ((i*29) % 26)) : (Hsvg - 10 - ((i*37) % 46));
      s += `<text x="${fx}" y="${fy}" font-size="12">${['🌷','🌼','🌻'][i%3]}</text>`;
    }
    // biura — kreskówkowe budynki: korpus z gradientem, dach (trójkąt), komin, drzwi, okna, szyld
    visOffices.forEach((o, oi) => {
      const col = oi % cols, row = Math.floor(oi / cols);
      const ox = margin + col*(ow+gap), oy = rowY[row];
      const oh = ohArr[oi];                       // wysokość TEGO biura
      const zoneOff = oh - off.h;                // strefy „przyklejone” do dołu biura
      const gid = 'grad-' + String(o.project_id).replace(/[^a-z0-9_-]/gi, '_');
      if(s2.gradients) s += `<defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${o.color}" stop-opacity="0.65"/><stop offset="100%" stop-color="${C.plot}" stop-opacity="1"/></linearGradient></defs>`;
      s += `<g class="office" data-pid="${esc(o.project_id)}" style="cursor:pointer">`;
      // cień + korpus (flat design: cień/gradient tylko gdy włączone w stage2)
      if(s2.shadows) s += `<rect x="${ox+4}" y="${oy+6}" width="${ow}" height="${oh}" rx="12" fill="#000" opacity="0.35"/>`;
      s += `<rect class="plot" x="${ox}" y="${oy}" width="${ow}" height="${oh}" rx="12" fill="${s2.gradients ? 'url(#' + gid + ')' : C.office}" stroke="${o.color}" stroke-width="3"/>`;
      // dach (trójkąt) + komin
      s += `<polygon points="${ox-8},${oy+4} ${ox+ow/2},${oy-26} ${ox+ow+8},${oy+4}" fill="${o.color}" opacity="0.9" stroke="#0d1117" stroke-width="2"/>`;
      s += `<rect x="${ox+ow-46}" y="${oy-24}" width="16" height="22" fill="#6e7681" stroke="#30363d"/>`;
      // szyld z nazwą projektu
      s += `<rect x="${ox+10}" y="${oy+8}" width="${Math.min(ow-20, 14*o.project_id.length+70)}" height="22" rx="6" fill="#0d1117" opacity="0.85" stroke="${o.color}"/>`;
      s += `<text x="${ox+18}" y="${oy+24}" font-size="12" font-weight="bold" fill="#fff">🏢 ${esc(o.project_id)}</text>`;
      s += `<text x="${ox+ow-12}" y="${oy+24}" text-anchor="end" font-size="10" fill="${C.muted}">${o.read_only?'🚧 read-only':'🔓 '+esc(o.policy||'')}</text>`;
      if(o.read_only) for(let t=0;t<8;t++)
        s += `<line x1="${ox+8+t*28}" y1="${oy+34}" x2="${ox+26+t*28}" y2="${oy+50}" stroke="${C.tape}" stroke-width="3" opacity="0.75"/>`;
      // okna + drzwi (front budynku)
      for(let wi=0; wi<3; wi++)
        s += `<rect x="${ox+off.w-64+wi*18}" y="${oy+34}" width="12" height="14" rx="2" fill="#bfe3ff" opacity="0.9" stroke="#0d1117"/>`;
      s += `<rect x="${ox+ow/2-12}" y="${oy+oh-16}" width="24" height="16" rx="3" fill="${o.color}" stroke="#0d1117"/>`;
      // gabinet kierownika + trofea (milestony)
      const mz = cfg.zones.manager, mx = ox+mz.rect[0], my = oy+mz.rect[1]+8+zoneOff;
      s += `<g class="manager"><rect x="${mx}" y="${my}" width="${mz.rect[2]}" height="${mz.rect[3]}" rx="6" fill="${C.room}" stroke="${C.roomStroke}" stroke-width="1.5"/>
        <text x="${mx+mz.rect[2]/2}" y="${my+12}" text-anchor="middle" font-size="9" fill="${C.muted}">🏆 kierownik</text>`;
      o.milestones.slice(0,4).forEach((m, mi) => {
        const tx = mx+6+mi*(cfg.trophy.w+cfg.trophy.gap), ty = my+20;
        s += `<g class="trophy"><title>${esc(m.id+' — '+m.status)}</title>
          <rect x="${tx}" y="${ty}" width="${cfg.trophy.w}" height="${cfg.trophy.h}" rx="4" fill="${m.status==='CLOSED'?'#d29922':'#21262d'}" stroke="${C.roomStroke}"/>
          <text x="${tx+cfg.trophy.w/2}" y="${ty+19}" text-anchor="middle" font-size="13">${m.status==='CLOSED'?'🏆':'🔨'}</text></g>`;
      });
      s += `</g>`;
      // pokoje agentów (klik = wątek rozmowy z osobą)
      o.rooms.forEach((room, ri) => {
        const rx = ox + R.x0 + (ri % R.perRow) * (R.w + R.gap);
        const ry = oy + R.y0 + 40 + Math.floor(ri / R.perRow) * (R.h + R.gap);
        // wszystkie pokoje mieszczą się — wysokość biura liczona wyżej
        const busy = !!room.current;
        const rcol = room.color || '#1f6feb';
        roomByRole[room.role] = room;
        s += `<g class="room ${busy?'busy':''}" data-role="${esc(room.role)}" style="cursor:pointer">
          <rect x="${rx}" y="${ry}" width="${R.w}" height="${R.h}" rx="8" fill="${C.room}" stroke="${busy?rcol:(room.color_soft||C.roomStroke)}" stroke-width="${busy?2.5:1.5}"/>
          <text class="ava" x="${rx+8}" y="${ry+17}" font-size="15">${room.avatar||'🧑'}</text>
          <text x="${rx+27}" y="${ry+15}" font-size="10" font-weight="bold" fill="${rcol}">${esc(room.name)}</text>
          <text x="${rx+8}" y="${ry+26}" font-size="8" fill="${C.muted}">${esc(room.position)}</text>
          ${room.res?`<text x="${rx+R.w-8}" y="${ry+15}" text-anchor="end" font-size="8" fill="${C.muted}">⚙️${room.res.cpu}% 🧠${room.res.rss_mb}MB</text>`:''}
          <text x="${rx+8}" y="${ry+R.h-8}" font-size="9" fill="${busy?rcol:C.muted}">${busy?('🔨 '+esc(room.current.id)):('wolny/a · zadań: '+room.taskCount+(room.threadCount?' · 💬'+room.threadCount:''))}</text>
          <title>klik = rozmowa z ${esc(room.name)} (${esc(room.role)}) · zadania: ${esc(JSON.stringify(room.by_status||{}))} · staż ${fmtDur(room.tenure_s||0)}</title></g>`;
        room._x = rx; room._y = ry;
      });
      // strefy biura (półki na teczki)
      for(const [zk, z] of Object.entries(cfg.zones)){
        if(zk === 'manager' || zk === 'desk') continue;
        const zx = ox+z.rect[0], zy = oy+z.rect[1]+zoneOff;
        const hasAlarm = zk==='alarm' && o.desks.some(d=>d.zone==='alarm');
        s += `<g class="zone ${hasAlarm?'alarmzone':''}"><rect x="${zx}" y="${zy}" width="${z.rect[2]}" height="${z.rect[3]}" rx="6" fill="${C.room}" fill-opacity="0.45" stroke="${C.roomStroke}" stroke-dasharray="4 3"/>
          <text x="${zx+4}" y="${zy+11}" font-size="8" fill="${C.muted}">${esc(z.label)}</text></g>`;
      }
      s += `</g>`;
      o._ox = ox; o._oy = oy; o._zoneOff = zoneOff;
    });
    if(mySeq !== firmaRenderSeq) return;  // wyścig — przestarzały render nie może nadpisać nowszego
    stLayer.innerHTML = s;
    // klik: biuro=filtr projektu, pokój=wątek z osobą
    stLayer.querySelectorAll('g.office').forEach(og => {
      og.addEventListener('click', (e) => {
        if(e.target.closest('g.room')) return;
        toggleProj(og.dataset.pid);
      });
    });
    stLayer.querySelectorAll('g.room').forEach(rg => {
      rg.addEventListener('click', (e) => { e.stopPropagation(); startThread(rg.dataset.role); });
    });
  }
  // ===== legenda (klik = podświetl status, dimStatus) =====
  document.getElementById('firmaLegend').innerHTML = Object.entries(cfg.statusStyle)
    .map(([k,v]) => `<button class="lchip ${dimStatus===k?'on':''}" style="background:${v.color};color:#fff;border:0;cursor:pointer" onclick="dimStatus=${dimStatus===k?'null':JSON.stringify(k)};renderFirma()">${v.icon} ${k}</button>`).join('')
    + `<span class="muted" style="font-size:11px;align-self:center">klik w legendę = pokaż tylko ten status · klik biurko = zadanie</span>`;
  if(mySeq !== firmaRenderSeq) return;  // wyścig — ochrona przed nadpisaniem przez starszy render
  // ===== warstwa biurek (persistent; BEZ animacji przejazdu — od razu na miejscu) =====
  let deskLayer = document.getElementById('deskLayer');
  if(!deskLayer){ deskLayer = svgEl('g', {id:'deskLayer'}, svg); }
  const stCfg = cfg.statusStyle, ps = cfg.prioScale;
  const moveMs = cfg.anim.moveMs || 0;
  const seen = new Set();
  for(const o of visOffices){
    const ox = o._ox, oy = o._oy;
    const byZone = {};
    for(const d of o.desks.filter(x => !hiddenSts.has(x.status))){ (byZone[d.zone] = byZone[d.zone]||[]).push(d); }
    for(const [zone, list] of Object.entries(byZone)){
      // agregacja artefaktów: gdy w strefie jest więcej biurek niż miejsc — pokaż capacity-1 + zbiorczy [+N]
      const z = cfg.zones[zone];
      let capacity = 999;
      if(zone === 'desk'){
        capacity = Math.max(1, Math.floor((R.w-12)/(D.w+D.gap))) * Math.max(1, Math.floor((R.h - R.headerH - 8)/(D.h+D.gap)));
      } else if(z){
        capacity = Math.max(1, Math.floor((z.rect[2]-10)/(D.w+D.gap))) * Math.max(1, Math.floor((z.rect[3]-16)/(D.h+D.gap)));
      }
      const zkey = o.project_id + ':' + zone;
      const expanded = expandedZones.has(zkey);
      let show = list, hiddenN = 0;
      if(list.length > capacity && !expanded){ show = list.slice(0, Math.max(1, capacity-1)); hiddenN = list.length - show.length; }
      const deskPos = (d, i) => {
        const scale = ps[d.priority] || 1;
        const dw = D.w*scale, dh = D.h*scale;
        let x, y;
        if(zone === 'desk'){
          const room = o.rooms.find(r => r.role === d.role) || o.rooms[0];
          if(room && room._x !== undefined){
            const perRow = Math.max(1, Math.floor((R.w-12)/(D.w+D.gap)));
            x = room._x + 6 + (i % perRow)*(D.w+D.gap);
            y = room._y + R.headerH + 4 + Math.floor(i/perRow)*(D.h+D.gap);
          } else { x = ox+20; y = oy+70; }
        } else {
          if(!z) return null;
          const zx = ox+z.rect[0], zy = oy+z.rect[1]+(o._zoneOff||0);
          const perRow = Math.max(1, Math.floor((z.rect[2]-10)/(D.w+D.gap)));
          x = zx + 5 + (i % perRow)*(D.w+D.gap);
          y = zy + 16 + Math.floor(i/perRow)*(D.h+D.gap);
        }
        x += (D.w-dw)/2; y += (D.h-dh)/2;
        return {x:x, y:y, dw:dw, dh:dh};
      };
      show.forEach((d, i) => {
        seen.add(d.id);
        const p = deskPos(d, i); if(!p) return;
        const sty = stCfg[d.status] || stCfg.READY;
        const scale = ps[d.priority] || 1;
        const x = p.x, y = p.y, dw = p.dw, dh = p.dh;
        let gEl = deskEls[d.id];
        const dim = dimStatus && d.status !== dimStatus;
        if(!gEl){
          gEl = svgEl('g', {class:'desk', cursor:'pointer'}, deskLayer);
          const title = svgEl('title', {}, gEl);
          svgEl('rect', {width:dw, height:dh, rx:4, fill:sty.color, opacity:0.92, stroke:'#0d1117'}, gEl);
          const ic = svgEl('text', {x:dw/2, y:dh/2+4, 'text-anchor':'middle', 'font-size':Math.round(10*scale), 'pointer-events':'none'}, gEl);
          ic.textContent = sty.icon;
          const lb = svgEl('text', {x:dw/2, y:dh+9, 'text-anchor':'middle', 'font-size':8, fill:C.text, 'pointer-events':'none'}, gEl);
          lb.textContent = d.id;
          gEl.addEventListener('click', (ev) => { ev.stopPropagation(); openTask(d.id); });
          deskEls[d.id] = gEl;
          gEl.style.transform = `translate(${x}px,${y}px)`; // bez transition — od razu na miejscu (szybko)
        } else {
          gEl.style.transform = `translate(${x}px,${y}px)`;
        }
        gEl.style.opacity = dim ? '0.12' : '1';
      });
      if(hiddenN > 0){
        // zbiorczy kafelek [+N] — klik rozwija wszystkie zadania strefy
        const d0 = list[show.length] || list[0];
        const p = deskPos(d0, show.length) || {x: ox+20, y: oy+120, dw: D.w, dh: D.h};
        const g = svgEl('g', {class:'desk more', cursor:'pointer', transform:'translate(' + p.x + 'px,' + p.y + 'px)'}, deskLayer);
        svgEl('rect', {width:p.dw, height:p.dh, rx:4, fill:'#161b22', stroke:'#d29922', 'stroke-width':2, 'stroke-dasharray':'4 3'}, g);
        const t = svgEl('text', {x:p.dw/2, y:p.dh/2+4, 'text-anchor':'middle', 'font-size':11, fill:'#d29922', 'font-weight':'bold', 'pointer-events':'none'}, g);
        t.textContent = '[+' + hiddenN + ']';
        const ti = svgEl('title', {}, g);
        ti.textContent = 'klik = rozwiń wszystkie zadania tej strefy (' + list.length + ')';
        g.addEventListener('click', (ev) => { ev.stopPropagation(); expandedZones.add(zkey); renderFirma(); });
      }
    }
  }
  for(const id of Object.keys(deskEls)){
    if(!seen.has(id)){ deskEls[id].remove(); delete deskEls[id]; }
  }
  // ===== dymki rozmów live (wątki sponsor↔agent świeższe niż 150s) — kolor rozmówcy =====
  let convLayer = document.getElementById('convLayer');
  if(!convLayer){ convLayer = svgEl('g', {id:'convLayer'}, svg); }
  let cs = '';
  const roomPos = {};
  for(const o of visOffices) for(const r of o.rooms) if(r._x !== undefined) roomPos[r.role] = {x: r._x + R.w/2, y: r._y + R.h/2};
  const gatePos = firmaGate || {x: Wsvg/2, y: Hsvg + 40};
  for(const c of (W.conversations || [])){
    const a = roomPos[c.a_role];
    if(!a) continue;
    const rm = roomByRole[c.a_role] || {};
    const col = rm.color || '#58a6ff';
    const b = (c.b_role === 'sponsor') ? gatePos : (roomPos[c.b_role] || gatePos);
    cs += '<path class="convLink" d="M ' + a.x + ' ' + (a.y-12) + ' C ' + a.x + ' ' + (a.y-70) + ', ' + b.x + ' ' + (b.y-70) + ', ' + b.x + ' ' + (b.y-8) + '" stroke="' + col + '" stroke-width="2" opacity="0.85"/>';
    const txt = (c.text || '…');
    const bw = Math.min(200, 34 + txt.length*5.4), bh = 36;
    const bx = Math.max(4, Math.min(Wsvg-4-bw, a.x - bw/2)), by = Math.max(4, a.y - 84);
    cs += '<g class="bubble"><rect x="' + bx + '" y="' + by + '" width="' + bw + '" height="' + bh + '" rx="10" fill="' + col + '"/>' +
      '<polygon points="' + (a.x-6) + ',' + (by+bh) + ' ' + (a.x+6) + ',' + (by+bh) + ' ' + a.x + ',' + (by+bh+8) + '" fill="' + col + '"/>' +
      '<text x="' + (bx+8) + '" y="' + (by+15) + '" font-size="10" fill="#fff" font-weight="bold">' + (c.author === 'sponsor' ? '🧑‍💼→ ' : (rm.avatar||'') + ' ') + esc(txt.slice(0, 26)) + '</text>' +
      '<text x="' + (bx+8) + '" y="' + (by+29) + '" font-size="8" fill="#ffffffd9">💬 live · ' + esc(c.a_role) + (c.b_role === 'sponsor' ? ' ↔ Sponsor' : ' ↔ ' + esc(c.b_role)) + '</text></g>';
  }
  convLayer.innerHTML = cs;
}
// pan/zoom mapy osiedla (wheel = zoom wokół kursora, drag = przesuwaj)
function initFirmaPanZoom(){
  const svg = document.getElementById('estate');
  if(!svg || svg.dataset.pz) return; svg.dataset.pz = '1';
  let drag = null;
  svg.addEventListener('wheel', (e) => {
    if(!firmaView) return;
    e.preventDefault();
    const f = e.deltaY > 0 ? 1.15 : 1/1.15;
    const r = svg.getBoundingClientRect();
    const mx = firmaView.x + (e.clientX - r.left) / r.width * firmaView.w;
    const my = firmaView.y + (e.clientY - r.top) / r.height * firmaView.h;
    const nw = Math.min(5000, Math.max(200, firmaView.w * f));
    firmaView.h = nw * (r.height / r.width);
    firmaView.w = nw;
    firmaView.x = mx - (e.clientX - r.left) / r.width * firmaView.w;
    firmaView.y = my - (e.clientY - r.top) / r.height * firmaView.h;
    svg.setAttribute('viewBox', `${firmaView.x} ${firmaView.y} ${firmaView.w} ${firmaView.h}`);
  }, {passive:false});
  svg.addEventListener('mousedown', (e) => { drag = {x:e.clientX, y:e.clientY, vx:firmaView?firmaView.x:0, vy:firmaView?firmaView.y:0}; });
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
window.addEventListener('hashchange', () => { applyHashFilters(); showTab((location.hash.slice(1).split('?')[0])||'overview', false); });
showTab((location.hash.slice(1).split('?')[0])||'overview', false);
// inicjalny stan przełącznika pod-zakładek Flow (⚙️ praca / 🏗️ stanowiska) — bez renderowania, samo UI
document.querySelectorAll('#flowTabs button').forEach(b => b.classList.toggle('on', b.dataset.fview === flowView));
document.getElementById('flowWorkView').style.display = flowView === 'work' ? '' : 'none';
document.getElementById('flowOrgView').style.display = flowView === 'org' ? '' : 'none';
let lastState = null;
let firmaSlowTick = 0;
function firmaActive(){ return document.getElementById('tab-firma').classList.contains('on'); }
let autoRefreshOn = true;
function showUpdateBanner(v){
  let b = document.getElementById('updBanner');
  if(!b){ b = document.createElement('div'); b.id = 'updBanner'; document.body.appendChild(b); }
  b.innerHTML = `🔄 <b>Nowa wersja dashboardu na serwerze:</b> v${esc(v.version)} <span class="muted">(${esc(v.last_commit||'')}${v.commit_subject?' — '+esc(v.commit_subject.slice(0,60)):''})</span> — ta karta ma starą wersję, odśwież <b>Ctrl+R</b> <button onclick="localStorage.setItem('dashVer', '${esc(v.version)}|${esc(v.last_commit||'')}');location.reload()">🔄 Odśwież teraz</button>`;
  b.classList.add('show');
}
function toast(msg){
  let t = document.getElementById('toast');
  if(!t){ t = document.createElement('div'); t.id = 'toast'; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 12000);
}
function toggleAutoRefresh(){
  autoRefreshOn = !autoRefreshOn;
  const b = document.getElementById('liveTg');
  if(b){ b.textContent = autoRefreshOn ? '● LIVE' : '⏸ PAUZA';
         b.style.borderColor = b.style.color = autoRefreshOn ? '#3fb950' : '#f85149'; }
  document.getElementById('ts').textContent = autoRefreshOn ? new Date().toLocaleTimeString() : '⏸ zatrzymano';
  if(autoRefreshOn) refresh();
}
(async function initFilters(){
  // kolejność: URL hash -> localStorage -> serwer (kv, współdzielone między urządzeniami) -> default
  if(!applyHashFilters()){
    if(!localStorage.getItem('hiddenProjs') && !localStorage.getItem('hiddenSts') && !localStorage.getItem('flowHiddenStages')){
      await loadFiltersFromServer();
    }
  }
  refresh();
})();
setInterval(() => {
  if(refreshPaused || !autoRefreshOn) return;
  if(firmaActive()){
    // na mapie Firma nie odświeżamy co 2s (wieszałoby widok) — tylko co 10s lekkie /api/firma + zegary osobno
    firmaSlowTick++;
    if(firmaSlowTick % 5 === 0) renderFirma();
    return;
  }
  refresh();
}, 2000);
setInterval(() => { if (refreshPaused || !autoRefreshOn) return; const v = document.getElementById('viewer'); if (liveOn && v.dataset.url) view(v.dataset.url); }, 2000);
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
    tasks.sort(key=lambda t: (t["status"] != "AWAITING_OWNER", t["status"] != "RUNNING", t["status"] != "CHANGES_REQUESTED", -(t["updated_at"] or 0)))
    events = s.recent_events(30)
    # statystyki kolejki: ile czeka, ile już "spóźnionych" (READY/AWAITING starsze niż 1h), ile długo biegnie
    stats = {
        "waiting": sum(1 for t in tasks if t["status"] == "READY"),
        "awaiting": sum(1 for t in tasks if t["status"] == "AWAITING_OWNER"),
        "running": sum(1 for t in tasks if t["status"] == "RUNNING"),
        "overdue": sum(1 for t in tasks if t["status"] in ("READY", "AWAITING_OWNER") and t.get("age", 0) > 3600),
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
    now_t = time.time()
    for r in roles.values():
        p = _pers.persona_full(r["role"])
        cur = r["current"]
        rtasks = [t for t in raw if (t.get("role") or "worker") == r["role"]]
        by_status: dict[str, int] = {}
        for t in rtasks:
            by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        first_seen = min((t.get("created_at") or now_t) for t in rtasks) if rtasks else now_t
        work_s = float(s.get_kv(f"work_s:{r['role']}", 0) or 0)
        if cur and cur.get("started_at"):
            work_s += max(0.0, now_t - float(cur["started_at"]))
        team.append({**p, "tasks": r["tasks"], "merged": r["merged"], "by_status": by_status,
                     "tenure_s": max(0.0, now_t - first_seen), "work_s": work_s,
                     "current": ({"id": cur["id"], "started_at": cur.get("started_at"), "priority": cur.get("priority"),
                                  "res": _proc_stats(cur.get("claimed_by"))}
                                 if cur else None)})
    # kolejność wg STAŻU pracy (najstarsi pracownicy pierwsi); pracujący mają osobny badge
    team.sort(key=lambda x: (-(x.get("tenure_s") or 0), x["role"]))
    # --- nowa osoba w firmie: rola widziana pierwszy raz i ŚWIEŻA (tenure < 10 min) -> banner/toast ---
    seen_roles = set(json.loads(s.get_kv("seen_roles", "[]") or "[]"))
    new_hires: list[dict] = []
    for m in team:
        role = m.get("role") or ""
        if role and role not in seen_roles:
            seen_roles.add(role)
            if (m.get("tenure_s") or 0) < 600:  # naprawdę nowa (stare role przy pierwszym run nie są "new")
                ts = time.time()
                s.set_kv(f"new_hire_ts:{role}", ts)
                s.record_metric("new_hire", {"role": role, "name": m.get("name"), "position": m.get("position")}, None)
                new_hires.append({"role": role, "name": m.get("name"), "position": m.get("position"),
                                  "avatar": m.get("avatar"), "icon": m.get("icon"), "color": m.get("color"),
                                  "tenure_s": m.get("tenure_s") or 0, "ts": ts})
    s.set_kv("seen_roles", json.dumps(sorted(seen_roles)))
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
                      "approval_policy": p.get("approval_policy", "manual"),
                      "active": projects.is_active(p["project_id"])})
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
        # (staż tenure_s / przepracowany work_s są już policzone przy budowaniu team — sort wg stażu)
        from engine import team_threads
        return {"icons": ICON, "projects": projs, "tasks": tasks, "events": events, "team": team, "asks": asks,
                "new_hires": new_hires,
                "stats": stats,
                "comments": s.recent_comments(40), "metrics": metrics,
                "runtime": runtime, "logs": logs,
                "milestones": s.list_milestones(),
                "threads": team_threads.list_threads(s, limit=15),
                "version": _version_info(),
                "merges": _merges_info(s)}


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


def _sponsor_view(s: State) -> dict:
    """Widok opiekuna (Sponsor): tematy (milestone'y) z postępem per status + blokerami,
    zadania wymagające decyzji (AWAITING_OWNER/ESCALATED) z dostępnymi akcjami z flow configa,
    oczekujące zgody (approvals) i backlog READY. Czytelny podgląd „o co chodzi”."""
    from engine.flow import flow as flowmod
    from engine import github_bridge, personas
    cfg = flowmod.load_config()
    now = time.time()
    tasks = s.list_tasks()
    by_ms: dict[str, list[dict]] = {}
    for t in tasks:
        by_ms.setdefault(t.get("milestone") or "?", []).append(t)

    def t_brief(t: dict) -> dict:
        return {"id": t["id"], "title": t.get("title") or "", "title_sponsor": t.get("title_sponsor") or "",
                "status": t["status"], "substep": t.get("substep") or "", "priority": t.get("priority") or "P2",
                "role": t.get("role") or "", "persona": personas.persona_label(t.get("role") or ""),
                "project_id": t.get("project_id"), "branch": t.get("branch"),
                "pr": github_bridge.pr_for_task(t["id"]),
                "review_notes": (t.get("review_notes") or "")[:300],
                "acceptance_criteria": (t.get("acceptance_criteria") or "")[:400],
                "scope_paths": t.get("scope_paths") or [], "parent_task": t.get("parent_task"),
                "age": max(0, now - (t.get("updated_at") or t.get("created_at") or now)),
                "started_at": t.get("started_at"), "milestone": t.get("milestone")}

    milestones = []
    for m in s.list_milestones():
        ts = by_ms.get(m["id"], [])
        per: dict[str, int] = {}
        for t in ts:
            per[t["status"]] = per.get(t["status"], 0) + 1
        total = len(ts)
        merged = per.get("MERGED", 0)
        blockers = [t_brief(t) for t in ts if t["status"] in ("BLOCKED", "ESCALATED", "FAILED", "AWAITING_OWNER")]
        blockers.sort(key=lambda b: -b["age"])
        milestones.append({**m, "tasks_total": total, "per_status": per, "merged": merged,
                           "progress": (merged / total) if total else 0.0, "open_tasks": total - merged,
                           "blockers": blockers,
                           "tasks": [t_brief(t) for t in sorted(ts, key=lambda t: -(t.get("updated_at") or 0))][:50]})
    milestones.sort(key=lambda m: (m.get("status") != "OPEN", -(m.get("created_at") or 0)))

    decisions = []
    for t in tasks:
        if t["status"] in ("AWAITING_OWNER", "ESCALATED"):
            stage = flowmod.stage_of_status(cfg, t["status"])
            acts: list[str] = []
            for st in cfg.get("stages", []):
                if st.get("id") == stage:
                    acts = [a for a in (st.get("actions") or [])
                            if not flowmod.validate_action(cfg, t["status"], a)]
            decisions.append({**t_brief(t), "available_actions": acts})
    decisions.sort(key=lambda d: -d["age"])

    prio = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    backlog = sorted((t for t in tasks if t["status"] == "READY"),
                     key=lambda t: (prio.get(t.get("priority") or "P2", 2), -(t.get("created_at") or 0)))
    backlog = [t_brief(t) for t in backlog[:15]]

    return {"milestones": milestones, "decisions": decisions, "approvals": s.pending_approvals(),
            "backlog": backlog, "actions_def": cfg.get("actions", {}), "ts": now}


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
        elif u.path == "/api/flow":
            # FLOW: diagram przepływu pracy z configa (engine/flow) + snapshot zadań
            from engine.flow import flow as flowmod
            self._send(200, json.dumps(flowmod.build_flow(snapshot(), State()),
                                       ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        elif u.path == "/api/flow/config":
            # FLOW: surowy config (merged) + override + default + błędy walidacji (do edytora w UI)
            from engine.flow import flow as flowmod
            cfg = flowmod.load_config()
            self._send(200, json.dumps({
                "config": cfg,
                "default_raw": flowmod.DEFAULT_CFG.read_text(encoding="utf-8"),
                "override_raw": (flowmod.OVERRIDE_CFG.read_text(encoding="utf-8")
                                 if flowmod.OVERRIDE_CFG.exists() else None),
                "override_active": flowmod.OVERRIDE_CFG.exists(),
                "errors": flowmod.validate_config(cfg),
            }, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
        elif u.path == "/api/org":
            # ORG: flow stanowisk — hierarchia zespołu (kto jest czyim szefem) z configa + persony + żywe zadania
            from engine.flow import org as orgmod
            self._send(200, json.dumps(orgmod.build_org(snapshot(), State()),
                                       ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        elif u.path == "/api/org/config":
            # ORG: surowy config hierarchii (merged) + override + default + błędy walidacji (do edytora w UI)
            from engine.flow import org as orgmod
            cfg = orgmod.load_config()
            self._send(200, json.dumps({
                "config": cfg,
                "default_raw": orgmod.DEFAULT_CFG.read_text(encoding="utf-8"),
                "override_raw": (orgmod.OVERRIDE_CFG.read_text(encoding="utf-8")
                                 if orgmod.OVERRIDE_CFG.exists() else None),
                "override_active": orgmod.OVERRIDE_CFG.exists(),
                "errors": orgmod.validate_config(cfg),
            }, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
        elif u.path == "/api/llm/jobs":
            # kolejka wywołań LLM (engine/llm_queue) — stats + ostatnie joby
            from engine import llm_queue
            s = State()
            self._send(200, json.dumps({"stats": llm_queue.stats(s), "jobs": llm_queue.recent_jobs(s, 30)},
                                       ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
        elif u.path == "/api/filters":
            # filtry dashboardu współdzielone między przeglądarkami/urządzeniami (kv w state.db)
            s = State()
            self._send(200, json.dumps({
                "hiddenProjs": json.loads(s.get_kv("filters:hiddenProjs", "[]") or "[]"),
                "hiddenSts": json.loads(s.get_kv("filters:hiddenSts", '["MERGED"]') or '["MERGED"]'),
                "flowHiddenStages": json.loads(s.get_kv("filters:flowHiddenStages", "[]") or "[]"),
            }).encode(), "application/json; charset=utf-8")
        elif u.path == "/api/sponsor":
            # widok opiekuna: tematy (milestone'y) + decyzje + zgody + backlog
            self._send(200, json.dumps(_sponsor_view(State()), ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        elif u.path == "/api/recurring":
            # obowiązki cykliczne per rola (zakładka „Obowiązki” na karcie pracownika)
            role = parse_qs(u.query).get("role", [None])[0]
            self._send(200, json.dumps(State().list_recurring(role=role), ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
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
        if u.path == "/api/filters":
            # zapisz filtry (współdzielone) — POST body: {hiddenProjs, hiddenSts, flowHiddenStages}
            s = State()
            s.set_kv("filters:hiddenProjs", json.dumps(body.get("hiddenProjs") or []))
            s.set_kv("filters:hiddenSts", json.dumps(body.get("hiddenSts") or ["MERGED"]))
            s.set_kv("filters:flowHiddenStages", json.dumps(body.get("flowHiddenStages") or []))
            self._send(200, b'{"ok":true}', "application/json")
            return
        if u.path == "/api/milestone":
            # opiekun zakłada nowy temat (milestone) — read-only dopiero po review, jak zadania AWAITING_OWNER
            mid = str(body.get("id") or "").strip()
            goal = str(body.get("goal") or "").strip()
            branch = str(body.get("branch") or "").strip() or ("feat/" + (mid.lower().replace(" ", "-") if mid else " temat"))
            title = str(body.get("title") or mid).strip()
            pid = str(body.get("project_id") or "mikowhy-teams")
            if not mid or not goal:
                self._send(400, b'{"error":"id i goal wymagane"}', "application/json")
                return
            s = State()
            s.add_milestone(mid, goal, branch, title=title, project_id=pid)
            s.record_metric("milestone_add", {"id": mid, "goal": goal[:200], "by": "sponsor/dashboard"}, None)
            self._send(200, json.dumps({"ok": True, "id": mid, "branch": branch}).encode(), "application/json")
            return
        if u.path == "/api/recurring":
            # dodaj obowiązek cykliczny (domyślnie priorytet P3 — niżej niż bieżące sprawy)
            role = str(body.get("role") or "")
            title = str(body.get("title") or "").strip()
            if not role or not title:
                self._send(400, b'{"error":"role i title wymagane"}', "application/json")
                return
            rid = "REC-" + uuid.uuid4().hex[:6].upper()
            State().add_recurring(rid, role, title, schedule=str(body.get("schedule") or "daily"),
                                  priority=str(body.get("priority") or "P3"))
            self._send(200, json.dumps({"ok": True, "id": rid}).encode(), "application/json")
            return
        if len(parts) == 4 and parts[:2] == ["api", "recurring"] and parts[3] == "toggle":
            State().set_recurring_active(parts[2], bool(body.get("active", True)))
            self._send(200, b'{"ok":true}', "application/json")
            return
        if u.path == "/api/flow/action":
            # FLOW: akcja Sponsora zdefiniowana w configu (approve/decline/retry/comment/set_priority)
            from engine.flow import flow as flowmod
            try:
                res = flowmod.apply_action(str(body.get("task_id") or ""), str(body.get("action") or ""), body)
                self._send(200, json.dumps(res, ensure_ascii=False, default=str).encode(), "application/json")
            except ValueError as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")
            return
        if u.path == "/api/flow/config":
            # FLOW: zapis override configu (walidacja; błędy blokują zapis)
            from engine.flow import flow as flowmod
            errs = flowmod.save_override(body.get("config"))
            if errs:
                self._send(400, json.dumps({"ok": False, "errors": errs}).encode(), "application/json")
            else:
                self._send(200, json.dumps({"ok": True}).encode(), "application/json")
            return
        if u.path == "/api/flow/config/reset":
            # FLOW: usuń override -> powrót do default_flow.json
            from engine.flow import flow as flowmod
            removed = flowmod.reset_override()
            self._send(200, json.dumps({"ok": True, "removed": removed}).encode(), "application/json")
            return
        if u.path == "/api/org/config":
            # ORG: zapis override configu hierarchii (walidacja; błędy blokują zapis)
            from engine.flow import org as orgmod
            errs = orgmod.save_override(body.get("config"))
            if errs:
                self._send(400, json.dumps({"ok": False, "errors": errs}).encode(), "application/json")
            else:
                self._send(200, json.dumps({"ok": True}).encode(), "application/json")
            return
        if u.path == "/api/org/config/reset":
            # ORG: usuń override -> powrót do default_org.json
            from engine.flow import org as orgmod
            removed = orgmod.reset_override()
            self._send(200, json.dumps({"ok": True, "removed": removed}).encode(), "application/json")
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
