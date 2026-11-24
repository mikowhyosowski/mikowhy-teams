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
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Support DARK_FACTORY_ROOT env var for deployed instances, fallback to script location
_factory_root = os.environ.get('DARK_FACTORY_ROOT')
if _factory_root and os.path.isdir(_factory_root):
    ROOT = Path(_factory_root).resolve()
else:
    ROOT = Path(__file__).resolve().parent.parent

import sys
sys.path.insert(0, str(ROOT))

from engine import projects
from engine.state import State

ICON = {"READY": "⏳", "RUNNING": "🔄", "IN_REVIEW": "🔍", "CHANGES_REQUESTED": "🛠️", "AWAITING_OWNER": "🧑‍💼",
        "READY_TO_MERGE": "📦", "MERGED": "✅", "FAILED": "❌", "BLOCKED": "⛔", "ESCALATED": "🚨"}

VERSION = "2.6.4"


def _check_task_process_health(task_id: str, s: State) -> dict:
    """Weryfikacja na żywo stanu procesu OS dla zadania."""
    from engine.process_health import get_process_monitor
    monitor = get_process_monitor()
    task = s.get_task(task_id)
    claimed_by = task.get("claimed_by") if task else None
    check_result = monitor.check_task_process(task_id, claimed_by)
    
    result = {
        "task_id": task_id,
        "claimed_by": claimed_by,
        "current_pid": check_result.current_pid,
        "check_timestamp": check_result.check_timestamp,
        "orphan_pids": check_result.orphan_pids,
        "has_duplicate_workers": check_result.has_duplicate_workers,
        "zombie_pids": check_result.zombie_pids,
    }
    
    if check_result.process_status:
        ps = check_result.process_status
        result["process_status"] = {
            "pid": ps.pid, "alive": ps.alive, "status": ps.status,
            "state": ps.state, "cmdline": ps.cmdline[:500] if ps.cmdline else "",
            "cpu_pct": round(ps.cpu_pct, 2), "rss_mb": round(ps.rss_mb, 2),
            "uptime_s": round(ps.uptime_s, 2), "children": ps.children, "comm": ps.comm
        }
        result.update({
            "status": ps.status, "pid": ps.pid, "cmdline": ps.cmdline,
            "cpu_pct": round(ps.cpu_pct, 2), "rss_mb": round(ps.rss_mb, 2),
            "uptime_s": round(ps.uptime_s, 2), "children": ps.children
        })
    else:
        result.update({"status": "UNKNOWN", "pid": None, "cmdline": "",
                       "cpu_pct": 0.0, "rss_mb": 0.0, "uptime_s": 0.0, "children": []})
    try:
        from engine.worker_supervisor import inspect
        mine = [w for w in inspect(s) if w.get("task_id") == task_id]
        live = next((w for w in mine if w.get("alive")), None)
        result["supervisor"] = live or (mine[0] if mine else None)
        if result["supervisor"]:
            sup = result["supervisor"]
            result["status"] = "RUNNING" if sup.get("alive") else "DEAD"
            result["pid"] = sup.get("pid")
            result["phase"] = sup.get("phase")
            result["cmd"] = sup.get("cmd")
            result["rc"] = sup.get("rc")
            result["worker_state"] = sup.get("state")
    except Exception:
        result["supervisor"] = None
    return result


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
 .fav{cursor:pointer;margin-right:2px;font-size:12px;user-select:none}
 .fav.on{filter:drop-shadow(0 0 4px rgba(248,81,73,.85))}
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
 .feed div.inprogress{border-left:3px solid #1f6feb;background:#12233a}
 .feed div.done{border-left:3px solid #3fb950;background:#0e2a1a}
 .threadbox .tmsg.inprogress .tt{background:#12233a;border:1px solid #1f6feb;animation:pulseB 1.6s infinite}
 .threadbox .tmsg.done .tt{background:#0e2a1a;border-left:3px solid #3fb950}
 @keyframes pulseB{0%,100%{box-shadow:0 0 0 0 rgba(31,111,235,.35)}50%{box-shadow:0 0 10px 2px rgba(31,111,235,.35)}}
 .nowcard .teraz{font-size:11px;color:#58a6ff;margin-top:2px}
 #bellWrap{position:relative;margin-left:8px}
 #bell{background:#0d1117;border:1px solid #30363d;border-radius:10px;padding:1px 10px;font-size:14px;cursor:pointer;position:relative}
 #bellN{display:none;background:#da3633;color:#fff;border-radius:9px;font-size:9px;padding:0 4px;margin-left:2px}
 #bellPanel{display:none;position:absolute;right:0;top:26px;width:420px;max-height:60vh;overflow-y:auto;background:#0d1117;border:1px solid #30363d;border-radius:8px;z-index:50;box-shadow:0 8px 24px #000a;font-size:11px}
 #bellPanel.open{display:block}
 .bellHead{padding:6px 8px;border-bottom:1px solid #30363d;position:sticky;top:0;background:#0d1117}
 .notif{padding:5px 8px;border-bottom:1px solid #161b22}
 .notif.read{opacity:.55}
 .notif.nstatus{border-left:3px solid #1f6feb;background:#0d1a2e}  /* v2.7.22: zmiana statusu wyraźnie zaznaczona */
 .nbadge{background:#21262d;border:1px solid #30363d;border-radius:8px;padding:0 5px;font-size:9px;color:#a371f7}
 /* v2.7.23: rejestr operacji — spójny status kolejki każdego działania (llm/git/qg/tg) */
 .oprow{padding:4px 6px;border-bottom:1px solid #161b22;font-size:12px}
 .opst{display:inline-block;padding:0 6px;border-radius:8px;font-size:11px;margin-left:4px;white-space:nowrap}
 .qbox{border:1px solid #21262d;border-radius:8px;padding:6px;background:#0d1117}
 .legend{display:flex;flex-wrap:wrap;gap:4px 14px;border:1px solid #30363d;border-radius:8px;padding:8px 10px;background:#0d1117;font-size:11px;margin-bottom:8px}
 .legend .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px}
 #sysTabs{margin-bottom:8px}
 #notifList .notif{padding:5px 8px;border-bottom:1px solid #161b22;font-size:12px}
 .qrow{padding:4px 6px;border-bottom:1px solid #161b22;cursor:pointer;font-size:12px}
 .qrow:hover{background:#12233a}
 .qpos{color:#8b949e;font-size:10px;display:inline-block;width:34px}
 .logbad{background:#2d1215;border-left:3px solid #f85149}
 .ahelp{font-size:9px;color:#8b949e;max-width:190px;display:inline-block;vertical-align:middle;margin-left:2px}
 .spin{display:inline-block;animation:spin 1.2s linear infinite}
 @keyframes spin{to{transform:rotate(360deg)}}
 .subclk{color:#79c0ff;font-size:11px;font-variant-numeric:tabular-nums}  /* jaśniejszy od #58a6ff, ten sam rozmiar co „teraz:” */
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
.pbar{height:8px;background:#21262d;border-radius:6px;overflow:hidden;margin:6px 0;position:relative}
.pbar i{display:block;height:100%;background:linear-gradient(90deg,#238636,#3fb950)}
.pbar .pct{position:absolute;right:5px;top:50%;transform:translateY(-50%);font-size:8px;font-weight:bold;color:#e6ffec;text-shadow:0 1px 2px #000}
.pbar.sm{height:6px;margin:2px 0}
.pbar.sm .pct{font-size:7px;right:3px}
.pbarWrap{display:flex;align-items:center;gap:6px}
.pbarWrap .plabel{font-size:10px;color:#8b949e;white-space:nowrap}
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
<h1>🏭 Dark Factory <button id="liveTg" onclick="toggleAutoRefresh()" title="włącz/wyłącz auto-odświeżanie (LIVE)" style="background:#0d1117;border:1px solid #3fb950;color:#3fb950;border-radius:10px;padding:1px 10px;font-size:12px;cursor:pointer">● LIVE</button> <span id="ts"></span> <span id="host" class="muted" style="font-size:11px"></span> <span id="pause" class="muted" style="font-size:11px;color:#f0b429"></span> <span id="ver" class="muted" style="font-size:11px;cursor:pointer;border:1px solid #30363d;border-radius:10px;padding:1px 8px" onclick="openChangelog()" title="wersja dashboardu — klik = changelog"></span>
<span id="bellWrap"><button id="bell" onclick="toggleBell()" title="powiadomienia systemowe (kolejka, merge, decyzje, deploy…)">🔔<span id="bellN"></span></button><div id="bellPanel"></div></span></h1>
<div id="stats"></div>
<div id="newHires"></div>
<div id="projFilters" title="Filtry projektów — globalne (działają na wszystkich zakładkach i mapie Firma)"></div>
<div id="stFilters" title="Filtry statusów — globalne (MERGED domyślnie ukryty; klik = pokaż/ukryj)"></div>
<nav id="tabs">
  <button data-tab="overview" onclick="showTab('overview')">📊 Przegląd<span id="navAlert"></span></button>
  <button data-tab="sponsor" onclick="showTab('sponsor')">🎯 Tematy<span id="navDec"></span></button>
  <button data-tab="sys" onclick="showTab('sys')">🗂️ Kolejka &amp; Logi</button>
  <button data-tab="tasks" onclick="showTab('tasks')">📋 Zadania</button>
  <button data-tab="flow" onclick="showTab('flow')">🌊 Flow</button>
  <button data-tab="team" onclick="showTab('team')">👥 Zespół</button>
  <button data-tab="chat" onclick="showTab('chat')">💬 Czat Vibe</button>
  <button data-tab="live" onclick="showTab('live')">⚡ Live</button>
  <button data-tab="firma" onclick="showTab('firma')">🏢 Firma</button>
  <button data-tab="errors" onclick="showTab('errors')">🔴 Błędy & LLM</button>
</nav>
<section class="tabpage" id="tab-overview">
<h2>🚨 Wymaga interwencji Sponsora</h2>
<div id="alerts"></div>
<h2>🔥 Teraz pracują <span class="muted">(zegarki tykają na żywo)</span></h2>
<div id="now"></div>
<h2>🔀 Przepływ pracy <span class="muted">(kliknij kafelek = szczegóły zadania)</span></h2>
<div id="flow"></div>
<h2>Workery <span class="muted">(prawda z /proc przy odświeżeniu)</span></h2>
<div id="workers"></div>
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
<section class="tabpage" id="tab-sys">
<h2>🗂️ Kolejka &amp; Logi <span class="muted">— panel kontrolny prezesa</span></h2>
<div class="legend" id="legendBox">
  <b>🗺️ Jak czytać fabrykę (analogia świata rzeczywistego):</b>
  <span><i class="dot" style="background:#6e7681"></i>📥 <b>Kolejka (READY)</b> = sortownia paczek — bierzemy w kolejności: priorytet → postęp → wiek</span>
  <span><i class="dot" style="background:#1f6feb"></i>⚙️ <b>RUNNING</b> = stanowisko przy taśmie — agent pracuje, zegar tyka na żywo</span>
  <span><i class="dot" style="background:#d29922"></i>🛠️ <b>CHANGES_REQUESTED</b> = poprawki po kontroli jakości (pętla na taśmie)</span>
  <span><i class="dot" style="background:#a371f7"></i>🔍 <b>IN_REVIEW</b> = kontrola jakości — niezależny recenzent</span>
  <span><i class="dot" style="background:#238636"></i>📦 <b>READY_TO_MERGE</b> = pakowanie na wysyłkę (🚀 Wdróż = natychmiastowa wysyłka)</span>
  <span><i class="dot" style="background:#2ea043"></i>✅ <b>MERGED</b> = wysłane do magazynu (gotowe)</span>
  <span><i class="dot" style="background:#da3633"></i>🅿️ <b>FAILED</b> = remontownia — zadanie czeka na brygadę (cykliczny audyt FAILED, systems-auditor)</span>
  <span><i class="dot" style="background:#b08800"></i>🧑‍💼 <b>AWAITING_OWNER</b> = gabinet prezesa — tylko graniczne decyzje, nie rutyna</span>
  <span><i class="dot" style="background:#57606a"></i>⛔ <b>BLOCKED</b> = kwarantanna — odblokowanie wyłącznie przez READY</span>
  <span>🔔 <b>Powiadomienia</b> = poczta wewnętrzna prezesa — tylko to, co musisz wiedzieć (z czasem)</span>
</div>
<div class="ftabs" id="sysTabs">
  <button data-sview="queue" class="on" onclick="showSysView('queue')">📥 Kolejka (<span id="qCount"></span>)</button>
  <button data-sview="logs" onclick="showSysView('logs')">📜 Logi</button>
  <button data-sview="notifs" onclick="showSysView('notifs')">🔔 Powiadomienia (<span id="nCount"></span>)</button>
  <button data-sview="ops" onclick="showSysView('ops')">⚙️ Operacje (<span id="opCount"></span>)</button>
</div>
<div id="sysQueue">
<h3>📥 Kolejka zadań <span class="muted">(kolejność pobierania przez agentów: priorytet → kara za 3+ retry → wyższy postęp → najstarsze · klik wiersz = szczegóły)</span> <span id="queueHead" class="muted" style="color:#d29922"></span></h3>
<div id="queueBox" class="qbox"></div>
</div>
<div id="sysLogs" style="display:none">
<h3>📜 Logi aplikacji <label class="muted" style="font-size:11px"><input type="checkbox" id="onlyErr" onchange="renderSys()"> tylko błędy i ważne</label> <span class="muted">(grupowane — ten sam event pokazany raz z liczbą ×N i czasem ostatniego)</span></h3>
<div id="logsBox" class="feed"></div>
</div>
<div id="sysNotifs" style="display:none">
<h3>🔔 Powiadomienia systemowe <button class="tbtn" onclick="markNotifsRead()">✓ oznacz wszystkie przeczytane</button></h3>
<div id="notifList"></div>
</div>
<div id="sysOps" style="display:none">
<h3>⚙️ Rejestr operacji <span class="muted">— wszystko co przechodzi przez kolejki systemu: LLM 🤖 · git 🌿 · testy/bramki 🧪 · telegram ✈️ · status: ⏳ czeka na wysłanie → 📤 wysłano → ✅ zrobione / ❌ błąd (próba N)</span></h3>
<div id="opsBox" class="feed"></div>
</div>
</section>
<section class="tabpage" id="tab-tasks">
<h2>Zadania <span class="badge" id="awaitCount"></span> <span class="badge" id="favCount" title="Na górze tabeli"></span> <span class="muted">(kliknij wiersz = szczegóły; 🤍 klik = ❤️ śledź — ulubione zawsze na górze; filtry projektów są wyżej — globalne)</span></h2>
<table id="tasks"><thead><tr><th></th><th>prio</th><th>id</th><th>tytuł</th><th>status</th><th>substep</th><th>postęp</th><th>retry</th><th>kto (rola)</th><th>projekt</th><th>pracuje</th><th>PR</th><th>updated</th></tr></thead><tbody></tbody></table>
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
<section class="tabpage" id="tab-errors">
<h2>🔴 Błędy & Monitor LLM <button onclick="loadErrors()" style="background:#1f6feb;color:#fff;border:0;border-radius:6px;padding:4px 12px;cursor:pointer">🔄 Odśwież</button></h2>
<div id="errorsContainer" style="display:flex;gap:20px;margin-bottom:20px">
  <div style="flex:1">
    <h3>🔥 Ostatnie Błędy <span id="errorsCount"></span></h3>
    <div id="errorsList" style="max-height:400px;overflow-y:auto"></div>
  </div>
  <div style="flex:1">
    <h3>🤖 Wiadomości LLM <span id="llmCount"></span></h3>
    <div id="llmList" style="max-height:400px;overflow-y:auto"></div>
  </div>
</div>
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
let lastState = null;  // v2.7.18: hoist — startowy showTab/refresh wołają renderSys/renderBell PRZED końcem skryptu (TDZ fix)
let FAVS = new Set();  // v2.7.27: ❤️ ulubione zadania Sponsora (zustand z /api/state, wspólne między urządzeniami)
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
const ST_COLOR = {'READY':'#6e7681','RUNNING':'#1f6feb','IN_REVIEW':'#a371f7','CHANGES_REQUESTED':'#d29922','AWAITING_OWNER':'#b08800','READY_TO_MERGE':'#238636','MERGED':'#2ea043','FAILED':'#da3633','ESCALATED':'#f85149','BLOCKED':'#57606a','COOLING':'#54aeff'};
function ccls(text){ text = String(text||''); return text.startsWith('🔄') ? 'inprogress' : (text.includes('✅') ? 'done' : ''); }
function fmtSUBProg(st){const m={'1/5_PLAN':0.2,'2/5_IMPL':0.4,'3/5_TEST':0.6,'4/5_QG':0.8,'4.5_SIMPLIFY':0.85,'5/5_COMMIT':1.0,'LLM_EXEC':0.5,'done':1.0};return (st in m)?m[st]:0;}
const SUBCEL = {'1/5_PLAN':'planowanie zadania','2/5_IMPL':'implementacja kodu','3/5_TEST':'testy i weryfikacja','4/5_QG':'bramki jakości (lint/typy)','4.5_SIMPLIFY':'upraszczanie rozwiązania','5/5_COMMIT':'commit + push','LLM_EXEC':'wywołanie LLM','done':'zakończone'};
function sbadge(st){ const c = ST_COLOR[st]||'#6e7681'; return `<span class="sbadge ${st==='ESCALATED'?'esc':''}" style="background:${c}">${esc(st)}</span>`; }
// v2.7.23: rejestr operacji (ops) — każde działanie systemu ma tu status kolejki: ⏳ czeka na wysłanie → 📤 wysłano → ✅/❌
const OP_STYLE = {'QUEUED':['⏳ czeka na wysłanie','#d29922'],'RUNNING':['📤 wysłano · w trakcie','#1f6feb'],'DONE':['✅ zrobione','#3fb950'],'FAILED':['❌ błąd','#f85149']};
const OP_KIND_ICON = {llm:'🤖', git:'🌿', qg:'🧪', tg:'✈️'};
function opBadge(o){ const s = OP_STYLE[o.status]||['❔','#6e7681']; return `<span class="opst" style="color:${s[1]};border:1px solid ${s[1]}">${s[0]}${o.attempts>1?` (próba ${o.attempts})`:''}</span>`; }
function renderOps(ops){
  if(!ops || !ops.length) return '<span class="muted">brak operacji — wszystko co system „wysyła” (LLM 🤖, git 🌿, testy 🧪, telegram ✈️) pojawi się tutaj ze statusem kolejki</span>';
  return ops.slice().sort((a,b)=>((b.queued_at||0)-(a.queued_at||0))).map(o=>{
    const dur = o.finished_at && o.started_at ? ` · <span class="muted">${fmtDur(o.finished_at-o.started_at)}</span>`
      : (o.status==='RUNNING' ? ` · <span class="muted">trwa ${fmtDur(Date.now()/1000-(o.started_at||o.queued_at||0))}</span>` : '');
    return `<div class="oprow"><span class="muted">${new Date((o.queued_at||0)*1000).toLocaleTimeString()}</span> <span>${OP_KIND_ICON[o.kind]||'⚙️'}</span> <b>${esc(o.label)}</b> ${o.task_id?`<a href="#" onclick="openTask('${esc(o.task_id)}');return false" style="color:#58a6ff">${esc(o.task_id)}</a>`:''} ${opBadge(o)}${dur}${o.detail?`<div class="muted" style="font-size:11px">${esc(o.detail)}</div>`:''}</div>`;
  }).join('');
}
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
// postęp zadania z substepu (np. 2/5_IMPL -> 40%); progress przychodzi też z backendu (state.substep_progress)
function progPct(t){ const p = (typeof t.progress === 'number') ? t.progress : 0; return Math.max(0, Math.min(100, Math.round(p*100))); }
function pbarHtml(t, cls){
  const pct = progPct(t);
  if(!t.substep && pct === 0) return '—';
  return `<div class="pbar ${cls||''}" title="postęp: ${esc(t.substep||'')} — ${pct}%"><i style="width:${pct}%"></i><span class="pct">${pct}%</span></div>`;
}
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
  FAVS = new Set(d.favorites||[]);
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
  renderBell(d);  // v2.7.15: dzwonek powiadomień systemowych
  if(document.getElementById('tab-sys').classList.contains('on')) renderSys();
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
  const FLOW = ['READY','RUNNING','IN_REVIEW','CHANGES_REQUESTED','READY_TO_MERGE','MERGED','AWAITING_OWNER','BLOCKED'];
  const byStatus = {};
  function boardCol(t){
    if(t.lane==='resume') return 'RUNNING';
    if(t.lane==='fresh') return 'READY';
    if(t.lane==='review') return 'IN_REVIEW';
    if(t.lane==='changes') return 'CHANGES_REQUESTED';
    if(t.lane==='merge') return 'READY_TO_MERGE';
    return t.status;
  }
  for(const t of visTasks){ const col = boardCol(t); (byStatus[col] = byStatus[col]||[]).push(t); }
  document.getElementById('flow').innerHTML = FLOW.map((st,i) => {
    const items = byStatus[st]||[];
    const hot = st==='AWAITING_OWNER' && items.length, run = st==='RUNNING';
    const chips = items.slice(0,8).map(t =>
      `<span class="chip st-${st}" onclick="openTask('${t.id}')">${favMark(t.id)}${PIC[t.priority||'P2']||''}<b>${esc(t.id)}</b>${t.depth?' ↳':''} ${t.lane==='resume'?`<span class="who">${esc(t.substep||'w toku')}</span>`:''} ${t.status==='RUNNING'&&t.started_at?(`<span class="clock" data-started="${t.started_at}">⏱ ${fmtDur(Date.now()/1000-t.started_at)}</span>`):''}
       <span class="who">${esc(t.persona||t.role||'')}</span></span>`).join('') + (items.length>8?`<span class="chip muted">+${items.length-8} więcej…</span>`:'');
    const arrow = i < FLOW.length-1 ? '<span class="arrow">→</span>' : '';
    return `<div class="stage ${hot?'hot':''} ${run?'run':''}"><h4>${d.icons[st]||''} ${sbadge(st)} <span class="cnt">${items.length}</span></h4>${chips}</div>${arrow}`;
  }).join('');
  loadWorkers();
  const tb = document.querySelector('#tasks tbody'); tb.innerHTML = '';
  let awaiting = 0;
  const PRIO = {'P0':0,'P1':1,'P2':2,'P3':3};
  const sorted = [...visTasks].sort((a,b)=>((FAVS.has(b.id)-FAVS.has(a.id))) || (PRIO[b.priority||'P2']-PRIO[a.priority||'P2']) || ((b.status==='RUNNING')-(a.status==='RUNNING')) || ((b.updated_at||0)-(a.updated_at||0)));
  const nowCards = [];
  let staleNow = 0;
  const NOW_WIN = 300;  // v2.7.25: RUNNING bez aktywności >5 min schodzi z panelu „Teraz pracują”
  for(const t of sorted){
    if(t.status==='AWAITING_OWNER') awaiting++;
    if(t.status==='RUNNING'){
      const lastAct = Math.max(t.updated_at||0, t.substep_since||0, t.started_at||0);
      if(lastAct >= Date.now()/1000 - NOW_WIN) nowCards.push(t); else staleNow++;
    }
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
    const prio = `<span class="pico" title="priorytet ${t.priority||'P2'}">${PIC[t.priority||'P2']||''}</span><span class="prio prio-${t.priority||'P2'}">${t.priority||'P2'}</span><button class="tbtn" style="padding:0 4px;margin-left:4px" title="aktywuj agenta: zadanie → READY z priorytetem P0" onclick="event.stopPropagation();taskCtl('${t.id}','boost')">\u26a1</button>`;
    const ic = `<span class="ic ic-${t.status}">${d.icons[t.status]||'?'}</span>`;
    const res = t.res ? ` <span class="muted" title="zużycie workera (pid ${t.res.pid})">⚙️${t.res.cpu}% 🧠${t.res.rss_mb}MB</span>` : '';
    const clock = t.status==='RUNNING' && t.started_at ? `<span class="clock" data-started="${t.started_at}">⏱ ${fmtDur(Date.now()/1000 - t.started_at)}</span>${res}` : '';
    tr.innerHTML = `<td>${ic}</td><td>${prio}</td><td style="padding-left:${8+(t.depth||0)*16}px">${favMark(t.id)}<b>${indent}${t.id}</b></td><td title="tech: ${esc(t.title||'')}">${esc((t.title_sponsor||t.title||'').slice(0,70))}</td>
      <td class="st">${t.status==='AWAITING_OWNER'?(['ASK_OWNER','ASK_SPONSOR'].includes(t.substep)?'<span class="badge">❓ ODPOWIEDZ</span> ':'<span class="badge">TWÓJ RUCH</span> '):''}${sbadge(t.status)}</td>
      <td class="muted">${esc(t.substep||'')}</td><td style="min-width:90px">${pbarHtml(t,'sm')}</td><td>${t.retry_count}</td>
      <td class="muted">${esc(t.persona||t.role||'')}</td><td class="muted">${pbadge(t.project_id)}</td>
      <td class="muted">${clock}</td>
      <td class="muted">${t.pr?('!'+t.pr):''}</td><td class="muted">${ago}</td>`;
  }
  document.getElementById('awaitCount').textContent = awaiting ? awaiting+' czeka na Ciebie' : '';
  const favCount = document.getElementById('favCount');
  if(favCount) favCount.textContent = FAVS.size ? '❤️ ' + FAVS.size : '';
  const navA = document.getElementById('navAlert');
  if(navA) navA.textContent = awaiting ? ' 🚨'+awaiting : '';
  document.getElementById('team').innerHTML = (d.team||[]).map(m => {
    const col = m.color || '#8b949e';
    const curRes = m.current && m.current.res ? ` <span class="muted" title="pid ${m.current.res.pid}">⚙️${m.current.res.cpu}% 🧠${m.current.res.rss_mb}MB</span>` : '';
    const turn = m.current
      ? `<div class="cur">🔨 ${esc(m.current.id)} <span class="muted">${esc(m.current.substep||'tura')}</span> <span class="clock" data-started="${m.current.started_at||0}">⏱ ${m.current.started_at?fmtDur(Date.now()/1000-m.current.started_at):''}</span> <span class="prio prio-${m.current.priority||'P2'}">${m.current.priority||'P2'}</span>${curRes}${!m.current.res?" 💤":""}</div>`
      : (m.next ? `<div class="cur">następna tura: <b>${esc(m.next.id)}</b> <span class="muted">${esc(m.next.step)}</span></div>` : '<div class="cur muted">— wolny/a —</div>');
    // liczba zadań per status (kolorowe chipsy) zamiast „zadania: X zmergowane: Y”
    const chips = Object.entries(m.by_status||{}).sort((a,b)=>b[1]-a[1]).map(([st,n]) =>
      `<span class="sbadge" style="background:${ST_COLOR[st]||'#6e7681'};font-size:9px;padding:0 6px;margin:0 3px 2px 0" title="${esc(st)}: ${n}">${d.icons[st]||''} ${n}</span>`).join('');
    return `<div class="pcard ${m.current?'busy':''}" style="border-top:3px solid ${col}" title="rola: ${m.role} · kolor osoby: ${col}">
      <span class="ava" style="color:${col};text-shadow:0 0 8px ${col}66">${m.avatar||'🧑‍💻'}</span> <span class="nm">${esc(m.name)}</span>
      <button class="tbtn" style="float:right" onclick="event.stopPropagation();startThread('${esc(m.role)}')" title="rozmowa z ${esc(m.name)}">💬</button>
      <button class="tbtn" style="float:right;margin-right:4px" onclick="event.stopPropagation();openDuties('${esc(m.role)}')" title="obowiązki cykliczne ${esc(m.name)}">📋</button>
      <div class="pos" style="color:${col}">${esc(m.position)} · rola: ${esc(m.role)}</div>
      <div class="story">„${esc(m.backstory)}”</div>
      ${turn}
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
    `<div class="nowcard" onclick="openTask('${t.id}')">${favMark(t.id)}<b>${d.icons.RUNNING} ${t.alive===false?"💀":(t.alive?"🟢":"❔")} ${t.id}</b> <span class="pico">${PIC[t.priority||'P2']||''}</span><span class="prio prio-${t.priority||'P2'}">${t.priority||'P2'}</span>
     <div class="clock" data-started="${t.started_at||0}">⏱ ${t.started_at?fmtDur(Date.now()/1000-t.started_at):''}</div>
     <div class="pbarWrap" title="postęp z podetapów: ${esc(t.substep||'—')}">${pbarHtml(t)}<span class="plabel">${esc(t.substep||'start')}</span></div>
     <div class="teraz"><span class="spin">🌀</span> 🎯 teraz: ${SUBCEL[t.substep]||esc(t.substep||'start')}
       <span class="subclk" data-subsince="${t.substep_since||0}">${t.substep_since?fmtDur(Date.now()/1000 - t.substep_since):''}</span></div>
     ${t.idle_s!=null?`<div class="teraz" style="color:${t.stall?'#f85149':(t.llm_busy?'#d29922':'#3fb950')}">${t.stall?`🔴 BEZ OUTPUTU: ${fmtDur(t.idle_s)} (STALL)`:(t.llm_busy?`🟡 Czeka na model LLM (${fmtDur(t.idle_s)})`:`🟢 output: ${fmtDur(t.idle_s)} temu`)}</div>`:''}
     ${t.eta_s?`<div class="teraz" style="color:#8b949e">🔮 ETA ${fmtDur(t.eta_s)}${t.eta_left_s!=null?` · zostało ~${fmtDur(t.eta_left_s)} · będzie ~<b>${new Date((t.eta_at||0)*1000).toLocaleTimeString()}</b>`:''}</div>`:''}
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
    `<div class="${ccls(c.text)}"><span class="muted">${new Date(c.ts*1000).toLocaleTimeString()}</span> <b>${esc(c.author)}</b> ${c.task_id?('['+esc(c.task_id)+']'):''} ${esc(c.text.slice(0,200))}</div>`).join('');
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
  const key = 'task|'+id+'|'+(d.status||'')+'|'+(d.substep||'')+'|'+(d.updated_at||0)+'|'+(t.comments||[]).length+'|'+(t.events||[]).length+'|'+(t.attachments||[]).length+'|'+(t.ops||[]).length+'|'+detailTab;
  if(keep && document.getElementById('detailBody').dataset.key === key) return;
  document.getElementById('detailBody').dataset.key = key;
  const tabs = `<div class="dtabs">
    <button class="${detailTab==='overview'?'on':''}" onclick="detailTab='overview';openTask(curTask,true)">📋 Szczegóły</button>
    <button class="${detailTab==='console'?'on':''}" onclick="detailTab='console';openTask(curTask,true)">🖥️ Konsola</button>
    <button class="${detailTab==='history'?'on':''}" onclick="detailTab='history';openTask(curTask,true)">🕘 Historia (${(t.events||[]).length})</button>
    <button class="${detailTab==='comments'?'on':''}" onclick="detailTab='comments';openTask(curTask,true)">💬 Komentarze (${(t.comments||[]).length})</button>
    <button class="${detailTab==='atts'?'on':''}" onclick="detailTab='atts';openTask(curTask,true)">📎 Załączniki (${(t.attachments||[]).length})</button>
    <button class="${detailTab==='ops'?'on':''}" onclick="detailTab='ops';openTask(curTask,true)">⚙️ Operacje (${(t.ops||[]).length})</button>
    <button class="${detailTab==='proc'?'on':''}" onclick="detailTab='proc';openTask(curTask,true)">⚙️ Proces (${(t.proc&&t.proc.steps||[]).length})</button>
  </div>`;
  // blok podsumowania — ZAWSZE widoczny (tytuł, status, czas, rola, projekt, branch/PR)
  const summary = `<div class="kv"><b>tytuł:</b> ${esc(d.title_sponsor||d.title||'')}</div>
    <div class="kv muted" style="font-size:10px">tech: ${esc(d.title||'')}</div>
    <div class="kv"><b>status:</b> ${favMark(d.id)} ${sbadge(d.status)} | <b>substep:</b> ${esc(d.substep||'-')} | <b>retry:</b> ${d.retry_count} | <b>read_only:</b> ${d.read_only?1:0}${d.mode==='llm'?' | <b>🤖 tryb LLM</b>':''} | <b>prio:</b> <span class="prio prio-${d.priority||'P2'}">${esc(d.priority||'P2')}</span></div>
    <div class="kv"><b>rola:</b> ${esc(d.role||'')} → <b>${esc(d.persona||'')}</b> | <b>projekt:</b> ${pbadge(d.project_id)} | <b>milestone:</b> ${esc(d.milestone||'')}</div>
    <div class="kv"><b>łączny czas pracy:</b> ${t.proc&&t.proc.total_work_s!=null?fmtDur(t.proc.total_work_s):'—'} ${d.stop_requested?'· ⏹ stop zlecony':''} <button class="tbtn" onclick="taskCtl('${esc(d.id)}','boost')" title="aktywuj agenta: READY + P0">⚡ aktywuj</button></div>
    <div class="kv"><b>przejął:</b> ${esc(d.claimed_by||'-')}${d.started_at?` od ${new Date(d.started_at*1000).toLocaleTimeString()}`:''}${d.elapsed!=null?` (<b>${fmtDur(d.elapsed)}</b>)`:''} | <b>aktualizacja:</b> ${d.updated_at?new Date(d.updated_at*1000).toLocaleTimeString():'-'}</div>
    <div class="kv"><b>rodzic:</b> ${d.parent_task?`<a href="#" onclick="openTask('${esc(d.parent_task)}');return false" style="color:#58a6ff">${esc(d.parent_task)}</a>`:'— (zadanie główne)'} | <b>branch:</b> ${esc(d.branch||'-')} | <b>PR:</b> ${d.pr?('!'+d.pr):'-'}</div>
    ${(d.status!=='MERGED'&&d.status!=='AWAITING_OWNER')?`<div class="kv"><b>kontrola agenta:</b>
      ${d.stop_requested?`<span class="sbadge" style="background:#b08800">⏹ stop zlecony — worker zakończy na granicy etapu</span> `:''}
      ${d.status==='RUNNING'&&!d.stop_requested?`<button class="tbtn" style="border-color:#f85149;color:#f85149" onclick="taskCtl('${esc(d.id)}','stop')" title="worker dokończy bieżący etap i pauzuje (→ READY)">⏹️ zatrzymaj po etapie</button>`:''}
      ${d.stop_requested?`<button class="tbtn" style="border-color:#3fb950;color:#3fb950" onclick="taskCtl('${esc(d.id)}','activate')" title="usuń zlecenie stopu — agent pracuje dalej">▶️ wznów (anuluj stop)</button>`:''}
    </div>`:''}`;
  let body = '';
  if(detailTab === 'console'){
    body = `<h2>🖥️ Konsola agenta na żywo <button class="tbtn" onclick="loadTaskConsole('${esc(d.id)}')">🔄 Odśwież</button> <button class="tbtn" style="border-color:#da3633;color:#f85149" onclick="killAndRetry('${esc(d.id)}')">🔄 Zabij i ponów</button></h2>
      <div id="processStatusBar" style="margin-bottom:6px"></div>
      <div id="taskConsoleStatus" class="kv muted" style="margin-bottom:6px">Ładowanie konsoli...</div>
      <div style="margin-bottom:6px;display:flex;gap:6px;flex-wrap:wrap">
        <button class="tbtn" onclick="loadTaskConsole('${esc(d.id)}', null)">wszystko</button>
        <button class="tbtn" onclick="loadTaskConsole('${esc(d.id)}', 'cmd')">komendy</button>
        <button class="tbtn" onclick="loadTaskConsole('${esc(d.id)}', 'stdout')">stdout</button>
        <button class="tbtn" onclick="loadTaskConsole('${esc(d.id)}', 'stderr')">stderr</button>
        <button class="tbtn" onclick="loadTaskConsole('${esc(d.id)}', 'meta')">meta</button>
      </div>
      <div id="taskConsoleBox" style="background:#0b0f14;border:1px solid #30363d;border-radius:8px;padding:8px;max-height:360px;overflow-y:auto;font-family:ui-monospace,Menlo,monospace;font-size:11px;line-height:1.4"></div>`;
    setTimeout(()=>loadTaskConsole(d.id), 50);
  } else if(detailTab === 'history'){
    const nowS = Date.now()/1000;
    body = `<h2>🕘 Historia zmian <span class="muted">(najświeższe na górze — zielone = <60s, niebieskie = <1h)</span></h2>
      <div class="histbox">${(t.events||[]).slice().sort((a,b)=>((b.ts||0)-(a.ts||0))).map(e=>{
        const age = nowS - (e.ts||0);
        const cls = age<60 ? 'fresh2' : (age<3600 ? 'fresh1' : '');
        const ch = e.changes ? ' · ' + Object.entries(e.changes).map(([k,v])=>`${esc(k)}: ${esc(String(v).slice(0,50))}`).join(' · ') : '';
        return `<div class="${cls}"><span class="muted">${new Date((e.ts||0)*1000).toLocaleTimeString()}</span> ${esc(e.event||((e.from||'?')+' → '+(e.to||'?')))}${ch}${(e.by)?(' <span class="muted">— '+esc(String(e.by).slice(0,60))+'</span>'):''}</div>`;
      }).join('')||'<span class="muted">brak historii</span>'}</div>`;
  } else if(detailTab === 'comments'){
    body = `<h2>💬 Komentarze</h2><div class="threadbox">${(t.comments||[]).slice().sort((a,b)=>((b.ts||0)-(a.ts||0))).map(c=>`<div class="tmsg ${c.author==='sponsor'||c.author==='sponsor/dashboard'?'me':'agent'} ${ccls(c.text)}"><div class="ta"><b>${esc(c.author)}</b> <span class="muted">${new Date(c.ts*1000).toLocaleTimeString()}</span></div><div class="tt">${linkify(c.text, t.id)}</div></div>`).join('')||'<span class="muted">brak komentarzy</span>'}</div>
      <h2>✍️ Twój komentarz</h2>
      <textarea id="cbox" placeholder="uwaga do zadania (też leci na PR na GitHubie)…"></textarea>
      <button onclick="sendComment()">Dodaj komentarz</button>
      ${d.substep==='ASK_SPONSOR'?`<button style="background:#238636;border-color:#2ea043;color:#fff" onclick="sendQuickTaskYes()" title="Wyślij automatycznie: „${esc(quickYesText())}”">⚡ TAK — realizuj, bez pytań</button>`:''}`;
  } else if(detailTab === 'atts'){
    // v2.7.10 — rezultaty/załączniki zadania w jednym miejscu, klikalne + podgląd inline
    const TEXT_K = ['md','txt','json','log','csv','py','html','patch','diff'];
    body = `<h2>📎 Załączniki / rezultaty zadania</h2><div class="histbox">` + ((t.attachments||[]).map(a=>{
      const url = '/api/attachment/'+a.id+'/view';  // v2.7.12: otwórz = strona HTML (nie biała kartka)
      const rawUrl = '/api/attachment/'+a.id+'/raw';
      const isImg = ['png','jpg','jpeg','gif','svg'].includes(a.kind);
      const isTxt = TEXT_K.includes(a.kind);
      return `<div class="att" data-att="${a.id}">
        <span>📎 <a href="${url}" target="_blank" style="color:#58a6ff"><b>${esc(a.label||a.path)}</b></a></span>
        <span class="muted">${esc(a.path)} · ${(a.size/1024).toFixed(1)} KB · ${new Date(a.ts*1000).toLocaleString()}</span>
        ${isTxt?`<button class="tbtn" onclick="toggleAtt(${a.id})">👁 podgląd</button>`:''}
        <a class="tbtn" style="text-decoration:none" href="${url}" target="_blank">↗ otwórz</a>
        <a class="tbtn" style="text-decoration:none" href="${rawUrl}" target="_blank" title="surowy plik">⬇ raw</a>
        ${isImg?`<div class="attprev" id="attprev-${a.id}" style="display:none"><img src="${rawUrl}" style="max-width:100%;border-radius:6px;margin-top:4px"></div>`
              :`<pre class="attprev" id="attprev-${a.id}" style="display:none;white-space:pre-wrap;font-size:11px;background:#0d1117;padding:6px;border-radius:6px;margin-top:4px;max-height:300px;overflow:auto"></pre>`}
      </div>`;
    }).join('')||'<span class="muted">brak załączników — pliki wypracowane przez agenta (np. plan w .agents/runtime/) pojawiają się tu automatycznie</span>') + '</div>';
  } else if(detailTab === 'ops'){
    // v2.7.23: rejestr operacji zadania — każde działanie systemu (LLM/git/testy/telegram) ze statusem kolejki
    body = `<h2>⚙️ Operacje zadania <span class="muted">(⏳ czeka na wysłanie → 📤 wysłano · w trakcie → ✅ zrobione / ❌ błąd — próba N)</span></h2>
      <div class="histbox">${renderOps(t.ops||[])}</div>`;
  } else if(detailTab === 'proc'){
    // v2.7.23: diagram procesu — ścieżka zadań + do jdne banku historii zmian statusów i retry
    const pp = t.proc || {};
    const steps = ['1/5_PLAN','2/5_IMPL','3/5_TEST','4/5_QG','5/5_COMMIT'];
    const curSub = d.substep||'';
    const stepRow = steps.map(st => {
      const prog = fmtSUBProg(st), curProg = fmtSUBProg(curSub);
      const done = curProg > prog, on = Math.abs(curProg - prog) < 0.01;
      return `<span style="padding:2px 8px;border-radius:6px;margin:0 3px;font-size:11px;display:inline-block;border:1px solid ${on?'#58a6ff':'#30363d'};background:${on?'#12233a':(done?'#0d2b1a':'#161b22')};color:${done?'#3fb950':(on?'#58a6ff':'#8b949e')}">${done?'✅ ':''}${st}</span>`;
    }).join('→');
    body = `<h2>⚙️ Proces realizacji zadania</h2>
      <div class="histbox">
        <div style="padding:6px 2px">${stepRow}</div>
        <div style="padding:6px 2px"><b>kroki:</b> ${(pp.steps||[]).length} · <b>retry:</b> ${d.retry_count||0} · <b>modele LLM:</b> ${(pp.llm_models||[]).map(m=>`<span class="pbadge" style="background:#21262d">${esc(m.model||'—')} ×${m.n}</span>`).join(' ')}</div>
      </div>
      <h2>🕘 Historia zmian</h2>
      <div class="histbox">${(pp.steps||[]).map(se => {
        const age = Date.now()/1000 - (se.ts||0);
        const cls = age<60 ? 'fresh2' : (age<3600 ? 'fresh1' : '');
        return `<div class="${cls}"><span class="muted">${new Date((se.ts||0)*1000).toLocaleTimeString()}</span> ${esc(String((se.from||'?')+' → '+(se.to||'?')))}</div>`;
      }).join('')||'<span class="muted">brak</span>'}</div>`;
  } else {
    body = `<div class="kv"><b>scope:</b> ${esc((d.scope_paths||[]).join(', ')||'-')}</div>
    <div class="kv"><b>AC:</b> ${esc((d.acceptance_criteria||'').slice(0,300))}</div>
    <div class="kv"><b>uwagi:</b> ${esc((d.review_notes||'').slice(0,300))}</div>`;
    // v2.7.12+2.7.15: pasek akcji = tylko akcje dostępne dla TEGO statusu, każda z opisem prostymi słowami (co klik = jaki status/substep)
    lastActionsDef = t.actions_def || null;
    const acts = t.actions_def || {};
    const abtns = (t.available_actions||[]).map(a =>
      `<span style="display:inline-block;margin:2px 10px 2px 0"><button class="tbtn" title="${esc(ACTION_HELP[a]||(acts[a]||{}).label||a)}" onclick="flowAction('${esc(d.id)}','${esc(a)}')">${(acts[a]||{}).icon||''} ${esc((acts[a]||{}).label||a)}</button><br><span class="ahelp">${esc(ACTION_HELP[a]||'')}</span></span>`).join('');
    if(abtns) body += `<div style="margin-top:8px;border-top:1px solid #21262d;padding-top:6px"><b>⚡ Akcje dla statusu ${esc(d.status)} — klik = efekt:</b><br>${abtns}</div>`;
    if(d.status==='AWAITING_OWNER'){
      body += `<div style="margin-top:6px"><button class="ok" onclick="approve()">✅ Zaakceptuj → READY</button>
               <button class="no" onclick="decline()">🛑 Odrzuć → BLOCKED</button>
               <div class="ahelp" style="max-width:none">✅ = potwierdzam decyzję: zadanie → <b>READY</b>, agent kontynuuje od wypracowanego etapu (substep bez zmiany) · 🛑 = odrzucam: zadanie → <b>BLOCKED</b>, substep OWNER_PACKET (zamknięte do ręcznego odblokowania)</div></div>`;
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
    return `<div class="tmsg ${me?'me':'agent'} ${ccls(m.text)}"><div class="ta">${who} <span class="muted">${new Date(m.ts*1000).toLocaleTimeString()}</span></div><div class="tt">${esc(m.text)}</div></div>`;
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
async function approve(){
  try{
    const r = await fetch('/api/task/'+curTask+'/approve', {method:'POST', body:'{}'});
    const j = await r.json().catch(()=>({}));
    toast(j.ok ? ('✅ '+curTask+' → '+(j.to||'READY')) : ('⚠ '+(j.error||'nie zaakceptowano')));
  }catch(e){ toast('⚠ '+e); }
  refresh();
}
async function taskCtl(id, act){
  // act: 'stop' = łagodny stop na granicy etapu · 'activate' = anuluj stop / aktywuj agenta
  try{
    const r = await fetch('/api/task/'+id+'/'+act, {method:'POST', body:'{}'});
    const j = await r.json().catch(()=>({}));
    toast(j.ok ? (act==='stop' ? ('⏹ zlecono stop po etapie: '+id) : ('▶️ '+id+': '+(j.from||'?')+' → '+(j.to||'?')))
               : ('⚠ '+(j.error||'błąd')));
  }catch(e){ toast('⚠ '+e); }
  openTask(id, true); refresh();
}

// ===== ERRORS & LLM MESSAGES =====
async function loadErrors() {
  const errorsContainer = document.getElementById('errorsContainer');
  const errorsList = document.getElementById('errorsList');
  const llmList = document.getElementById('llmList');
  const errorsCount = document.getElementById('errorsCount');
  const llmCount = document.getElementById('llmCount');
  
  if(!errorsList || !llmList) return;
  
  try {
    // Pobierz błędy
    const errRes = await fetch('http://127.0.0.1:8767/api/errors');
    const errData = await errRes.json();
    
    // Pobierz wiadomości LLM
    const llmRes = await fetch('http://127.0.0.1:8767/api/llm-messages');
    const llmData = await llmRes.json();
    
    // Wyświetl błędy
    if(errData.errors && errData.errors.length > 0) {
      errorsCount.textContent = `(${errData.errors.length})`;
      errorsList.innerHTML = errData.errors.map(err => {
        const dt = new Date(err.timestamp * 1000).toLocaleString();
        const severityColor = {
          'ERROR': '#f85149',
          'CRITICAL': '#cb2431',
          'WARNING': '#d29922'
        }[err.severity] || '#f85149';
        
        return `
          <div style="border-left:4px solid ${severityColor};padding-left:12px;margin-bottom:8px;background:#0d1117;border-radius:4px;padding:8px">
            <div style="font-size:11px;color:#8b949e">[${err.source}] ${dt}</div>
            <div style="font-size:13px;font-weight:600">
              ${err.task_id ? `<a href="#" onclick="openTask('${esc(err.task_id)}')" style="color:#58a6ff">${esc(err.task_id)}</a>` : 'System'}
              ${err.step ? ` · <span style="color:#a371f7">${esc(err.step)}</span>` : ''}
            </div>
            <div style="color:#e6edf3;margin-top:4px">${esc(err.message)}</div>
          </div>
        `;
      }).join('');
    } else {
      errorsCount.textContent = '(0)';
      errorsList.innerHTML = '<div style="color:#8b949e;font-size:12px">Brak błędów do wyświetlenia</div>';
    }
    
    // Wyświetl wiadomości LLM
    if(llmData.messages && llmData.messages.length > 0) {
      llmCount.textContent = `(${llmData.messages.length})`;
      llmList.innerHTML = llmData.messages.map(msg => {
        const dt = new Date(msg.timestamp * 1000).toLocaleString();
        const statusColor = {
          'sent': '#3fb950',
          'received': '#58a6ff',
          'error': '#f85149',
          'timeout': '#d29922'
        }[msg.status] || '#8b949e';
        
        // Skróć prompt i response
        const shortPrompt = msg.prompt ? msg.prompt.slice(0, 100) + (msg.prompt.length > 100 ? '...' : '') : '';
        const shortResponse = msg.response ? msg.response.slice(0, 100) + (msg.response.length > 100 ? '...' : '') : '';
        
        return `
          <div style="border-left:4px solid ${statusColor};padding-left:12px;margin-bottom:8px;background:#0d1117;border-radius:4px;padding:8px">
            <div style="font-size:11px;color:#8b949e">[${msg.model}] ${dt} · ${msg.tokens} tokens</div>
            <div style="font-size:13px;font-weight:600">
              ${msg.task_id ? `<a href="#" onclick="openTask('${esc(msg.task_id)}')" style="color:#58a6ff">${esc(msg.task_id)}</a>` : 'System'}
              ${msg.status ? ` · <span style="color:${statusColor}">${msg.status}</span>` : ''}
            </div>
            ${msg.prompt ? `<div style="color:#8b949e;margin-top:4px;font-size:11px">📤 <b>Prompt:</b> ${esc(shortPrompt)}</div>` : ''}
            ${msg.response ? `<div style="color:#8b949e;margin-top:4px;font-size:11px">📥 <b>Response:</b> ${esc(shortResponse)}</div>` : ''}
            ${msg.error ? `<div style="color:#f85149;margin-top:4px;font-size:11px">❌ <b>Error:</b> ${esc(msg.error)}</div>` : ''}
          </div>
        `;
      }).join('');
    } else {
      llmCount.textContent = '(0)';
      llmList.innerHTML = '<div style="color:#8b949e;font-size:12px">Brak wiadomości LLM do wyświetlenia</div>';
    }
    
  } catch(e) {
    errorsList.innerHTML = `<div style="color:#f85149">⚠️ Błąd ładowania błędów: ${esc(e.message)}</div>`;
    llmList.innerHTML = `<div style="color:#f85149">⚠️ Błąd ładowania wiadomości LLM: ${esc(e.message)}</div>`;
  }
}

// Auto-refresh co 30s dla zakładki errors
setInterval(() => {
  if(document.getElementById('tab-errors').classList.contains('on')) {
    loadErrors();
  }
}, 30000);

async function loadWorkers(){
  const box = document.getElementById('workers');
  if(!box) return;
  try{
    const r = await fetch('/api/workers');
    const j = await r.json();
    const rows = j.workers || [];
    if(!rows.length){ box.innerHTML = '<span class="muted">brak procesów workerów</span>'; return; }
    box.innerHTML = rows.map(w => `<div class="kv">${w.alive?'🟢':'⚪'} <b>${esc(w.task_id||'')}</b> ${esc(w.module||'')} pid ${w.pid} · ${esc(w.state||'')} · ${esc(w.phase||'')} · ${esc(w.step||'')} <span class="muted">${esc((w.cmd||'').slice(0,120))}</span></div>`).join('');
  }catch(e){ box.textContent = 'nie udało się odczytać workerów'; }
}
async function loadTaskConsole(tid, streamFilter){
  const box = document.getElementById('taskConsoleBox');
  const st = document.getElementById('taskConsoleStatus');
  const bar = document.getElementById('processStatusBar');
  if(!box || !st) return;
  try{
    let url = '/api/task/'+encodeURIComponent(tid)+'/console?tail=250';
    if(streamFilter) url += '&stream=' + encodeURIComponent(streamFilter);
    const r = await fetch(url);
    const j = await r.json();
    st.innerHTML = `Plik: <code>${esc(j.file_path||'')}</code> · Linii: ${j.total_lines||0} · ` +
      (j.stall ? `<b style="color:#f85149">🔴 STALLED: ${fmtDur(j.idle_s||0)} bez outputu</b>` :
                 `<b style="color:#3fb950">🟢 Ostatni output: ${fmtDur(j.idle_s||0)} temu</b>`);
    if(j.process_check) renderProcessStatus(j.process_check, tid);
    const evs = j.events || [];
    if(!evs.length){
      box.innerHTML = '<span class="muted">Brak wpisów w konsoli dla tego zadania</span>';
      return;
    }
    box.innerHTML = evs.map(e => {
      const ts = e.ts ? new Date(e.ts*1000).toLocaleTimeString() : '';
      const step = e.step ? `[${esc(e.step)}] ` : '';
      if(e.stream === 'cmd'){
        return `<div style="color:#58a6ff"><span class="muted">${ts}</span> ❯ <b>${step}${esc(e.cmd||'')}</b></div>`;
      } else if(e.stream === 'stderr'){
        return `<div style="color:#f85149"><span class="muted">${ts}</span> ⚠ ${step}${esc(e.data||'')}</div>`;
      } else if(e.stream === 'meta'){
        const rcTxt = e.rc != null ? ` (rc=${e.rc})` : '';
        return `<div style="color:#a371f7"><span class="muted">${ts}</span> ℹ ${step}${esc(e.data||'')}${rcTxt}</div>`;
      } else {
        return `<div style="color:#c9d1d9"><span class="muted">${ts}</span> ${step}${esc(e.data||'')}</div>`;
      }
    }).join('');
    box.scrollTop = box.scrollHeight;
  }catch(e){
    st.textContent = 'Błąd ładowania konsoli: ' + e;
  }
}
async function killAndRetry(tid){
  if(!confirm('Czy na pewno zabić zadanie '+tid+' i wysłać je do ponowienia?')) return;
  try{
    const r = await fetch('/api/task/'+encodeURIComponent(tid)+'/kill_and_retry', {
      method: 'POST',
      body: JSON.stringify({reason: 'kliknięcie Zabij i ponów w dashboardzie'})
    });
    const j = await r.json();
    toast(j.ok ? ('🔄 Zadanie '+tid+' — USPIONE na '+Math.max(0, Math.round(((j.cooldown_until||0) - Date.now()/1000)))
                 +'s (status '+(j.to||'?')+' zostaje)') : ('⚠ '+(j.error||'Błąd')));
  }catch(e){ toast('⚠ '+e); }
  refresh();
}
async function decline(){ const why = prompt('Powód odrzucenia:'); if(why===null) return;
  try{
    const r = await fetch('/api/task/'+curTask+'/decline', {method:'POST', body: JSON.stringify({reason: why})});
    const j = await r.json().catch(()=>({}));
    toast(j.ok ? ('🛑 '+curTask+' → '+(j.to||'BLOCKED')) : ('⚠ '+(j.error||'nie odrzucono')));
  }catch(e){ toast('⚠ '+e); }
  refresh(); }
// v2.7.10 — podgląd załącznika inline (tekst) / obrazek
async function toggleAtt(id){
  const el = document.getElementById('attprev-'+id);
  if(!el) return;
  if(el.style.display !== 'none'){ el.style.display = 'none'; return; }
  if(!el.dataset.loaded){
    const r = await fetch('/api/attachment/'+id+'/raw');
    el.textContent = (await r.text()).slice(0, 20000);
    el.dataset.loaded = '1';
  }
  el.style.display = 'block';
}
// v2.7.10 — zgoda na DEPLOY_PROD z poziomu dashboardu (przycisk 🚀 Deploy → PROD)
async function approvalAction(id, action){
  const reason = action==='decline' ? (prompt('Powód odrzucenia deployu:')||'') : '';
  if(action==='decline' && !reason) return;
  try{
    const r = await fetch('/api/approval/'+encodeURIComponent(id)+'/'+action, {method:'POST', body: JSON.stringify({reason})});
    const j = await r.json().catch(()=>({}));
    toast(j.ok ? ('✅ '+id+' — '+(action==='approve'?'zgoda na deploy':'odrzucone')) : ('⚠ '+(j.error||'błąd zgody')));
  }catch(e){ toast('⚠ '+e); }
  refresh();
}
// zakładki: stan w URL hash (#tasks), przeżywa odświeżenie; nav jest sticky
const TABS = ['overview','sponsor','sys','tasks','flow','team','chat','live','firma','errors'];
function showTab(name, push){
  if(!TABS.includes(name)) name = 'overview';
  document.querySelectorAll('.tabpage').forEach(el => el.classList.toggle('on', el.id === 'tab-'+name));
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  if(push !== false) history.replaceState(null, '', '#'+name+filterParams()); // hash zachowuje filtry przy zmianie zakładki
  if(name === 'firma') renderFirma();
  if(name === 'flow') showFlowView(flowView, false);
  if(name === 'sponsor') renderSponsor();
  if(name === 'sys') renderSys();
  if(name === 'errors') loadErrors();
}
// ===== 🗂️ Kolejka & Logi + 🔔 powiadomienia (v2.7.15/17 — panel prezesa w sub-tabach) =====
let sysView = 'queue';
// v2.7.22: czytelny postęp w logach — task_event jako kolorowa strzałka „co → na co” (status + substep osobno)
function fmtLogPayload(kind, payload){
  if(kind === 'task_event'){
    try{
      const p = JSON.parse(payload);
      let h = '';
      if(p.from || p.to){
        const toC = ST_COLOR[p.to] || '#3fb950';
        h += ` <b style="color:#8b949e">${esc(p.from||'?')}</b> <span style="color:${toC};font-weight:bold">→ ${esc(p.to||'?')}</span>`;
      }
      const ch = p.changes || {};
      for(const [k,v] of Object.entries(ch)){
        const [o,n] = String(v).split(' → ');
        if(k === 'substep'){
          h += ` <span class="muted">etap:</span> <b style="color:#8b949e">${esc(o)}</b> <span style="color:#d29922;font-weight:bold">→ ${esc(n)}</span>`;
        } else if(k !== 'status'){
          h += ` <span class="muted">${esc(k)}:</span> <b style="color:#8b949e">${esc(o)}</b> → <b>${esc(n)}</b>`;
        }
      }
      if(p.by) h += ` <span class="muted">— ${esc(String(p.by).slice(0,40))}</span>`;
      return h || `<span class="muted">${esc(payload)}</span>`;
    }catch(e){ /* fallback niżej */ }
  }
  return `<span class="muted">${esc(payload)}</span>`;
}
// v2.7.22: powiadomienie statusowe — strzałka „→ STATUS” kolorowana na zielono/kolor statusu
function fmtNotifText(x){
  const t = esc(x.text);
  if(x.kind !== 'status') return t;
  return t.replace(/→\\s*([A-Z_]+)/, (m, st) => `→ <b style="color:${ST_COLOR[st]||'#3fb950'}">${st}</b>`);
}
function showSysView(v){
  sysView = v;
  document.querySelectorAll('#sysTabs button').forEach(b => b.classList.toggle('on', b.dataset.sview === v));
  document.getElementById('sysQueue').style.display = v==='queue' ? '' : 'none';
  document.getElementById('sysLogs').style.display = v==='logs' ? '' : 'none';
  document.getElementById('sysNotifs').style.display = v==='notifs' ? '' : 'none';
  document.getElementById('sysOps').style.display = v==='ops' ? '' : 'none';
}
function renderSys(){
  const d = lastState; if(!d) return;
  document.getElementById('qCount').textContent = (d.queue||[]).length;
  document.getElementById('nCount').textContent = d.notif_unread||0;
  document.getElementById('opCount').textContent = ((d.ops_stats&&(d.ops_stats.queued+d.ops_stats.running))||0);  // v2.7.23: ile operacji czeka/w trakcie
  document.getElementById('opsBox').innerHTML = renderOps(d.ops||[]);  // v2.7.23: rejestr operacji
  // v2.7.19: raport „za ile będzie” nad kolejką
  const etaRun = d.stats && d.stats.eta_running_at ? `🔮 bieżące RUNNING skończą się ~<b>${new Date(d.stats.eta_running_at*1000).toLocaleTimeString()}</b>` : '';
  const etaQ = d.stats && d.stats.eta_queue_s ? ` · kolejka razem ~${fmtDur(d.stats.eta_queue_s)}` : '';
  const qh = document.getElementById('queueHead'); if(qh) qh.innerHTML = (etaRun + etaQ) || '';
  document.getElementById('queueBox').innerHTML = (d.queue||[]).length ? (d.queue||[]).map(q =>
    `<div class="qrow" onclick="openTask('${esc(q.id)}')">
       <span class="qpos">#${q.pos}</span> ${favMark(q.id)} <span class="prio prio-${q.priority}">${q.priority}</span>
       <b>${esc(q.id)}</b> <span class="muted">${esc((q.title_sponsor||q.title||'').slice(0,55))}</span>
       <span class="pbar sm" title="postęp ${Math.round((q.progress||0)*100)}%"><i style="width:${Math.round((q.progress||0)*100)}%"></i></span>
       <span class="muted">${esc(q.substep||'—')} · retry ${q.retry_count}${q.retry_count>=3?' 🔚':''} · ${fmtDur(q.age)} temu · ${esc(q.persona||q.role)}${q.eta_s?` · 🔮 ETA ${fmtDur(q.eta_s)}`:''}${q.review_notes?` · 📝 ${esc(q.review_notes.slice(0,60))}`:''}</span>
     </div>`).join('') : '<span class="muted">kolejka pusta ✅ (wszystko w pracy lub zmergowane)</span>';
  // logi grupowane: (kind|task|payload) -> {count, last, first} — czytelne mimo spamu
  const BAD = {'auto_retry':1,'stuck_requeued':1,'merge':1,'owner_packet':1,'ask_owner':1,'llm_job_failed':1,'llm_batch_fail':1,'orchestrator_error':1,'merge_error':1,'circuit_breaker_escalation':1,'watchdog_escalation':1,'sponsor_decline':1,'failed_parked':1,'milestone_error':1,'health_check':1,'throttled':1};
  const onlyErr = document.getElementById('onlyErr') && document.getElementById('onlyErr').checked;
  const groups = {};
  for(const l of (d.applogs||[])){
    const k = l.kind+'|'+ (l.task_id||'') + '|' + l.payload;
    if(!groups[k]) groups[k] = {kind:l.kind, task_id:l.task_id, payload:l.payload, n:0, last:l.ts, first:l.ts, bad:!!BAD[l.kind]};
    const g = groups[k]; g.n++; if(l.ts>g.last) g.last=l.ts; if(l.ts<g.first) g.first=l.ts;
  }
  let rows = Object.values(groups).sort((a,b)=>b.last-a.last);
  if(onlyErr) rows = rows.filter(r=>r.bad);
  document.getElementById('logsBox').innerHTML = rows.length ? rows.map(g =>
    `<div class="${g.bad?'logbad':''}"><span class="muted">${new Date(g.last*1000).toLocaleTimeString()}</span>${g.n>1?` <b style="color:#d29922">×${g.n}</b> <span class="muted">(od ${new Date(g.first*1000).toLocaleTimeString()})</span>`:''} <b>${esc(g.kind)}</b> ${g.task_id?`<a href="#" onclick="openTask('${esc(g.task_id)}');return false" style="color:#58a6ff">${esc(g.task_id)}</a>`:''} ${fmtLogPayload(g.kind, g.payload)}</div>`).join('')
    : '<span class="muted">brak logów</span>';
  // powiadomienia (pełna lista w sub-tabie)
  document.getElementById('notifList').innerHTML = `<div class="histbox">` + ((d.notifications||[]).map(x =>
    `<div class="notif ${x.read?'read':''} ${x.kind==='status'?'nstatus':''}" ${x.task_id?`onclick="openTask('${esc(x.task_id)}')" style="cursor:pointer"`:''}>
       <span class="muted">${new Date(x.ts*1000).toLocaleString()}</span> <span class="nbadge">${esc(x.kind)}</span> ${fmtNotifText(x)}${x.task_id?` <b>${esc(x.task_id)}</b>`:''}
     </div>`).join('') || '<span class="muted">brak powiadomień</span>') + '</div>';
}
function renderBell(d){
  if(!d) return;  // v2.7.18: TDZ/guard — przed pierwszym refresh lastState może być null
  const n = d.notif_unread||0;
  const bn = document.getElementById('bellN');
  bn.textContent = n>0 ? (n>99?'99+':String(n)) : '';
  bn.style.display = n>0 ? 'inline' : 'none';
  document.getElementById('bellPanel').innerHTML =
    `<div class="bellHead"><b>🔔 Powiadomienia systemowe</b> <button class="tbtn" onclick="markNotifsRead()">✓ oznacz przeczytane</button></div>` +
    ((d.notifications||[]).map(x =>
      `<div class="notif ${x.read?'read':''} ${x.kind==='status'?'nstatus':''}" ${x.task_id?`onclick="openTask('${esc(x.task_id)}');toggleBell(false)" style="cursor:pointer"`:''}>
         <span class="muted">${new Date(x.ts*1000).toLocaleString()}</span> <span class="nbadge">${esc(x.kind)}</span> ${fmtNotifText(x)}${x.task_id?` <b>${esc(x.task_id)}</b>`:''}
       </div>`).join('') || '<div class="muted" style="padding:6px">brak powiadomień</div>');
}
function toggleBell(force){
  const p = document.getElementById('bellPanel');
  const open = force !== undefined ? force : !p.classList.contains('open');
  p.classList.toggle('open', open);
  if(open) renderBell(lastState);
}
async function markNotifsRead(){ try{ await fetch('/api/notifications/read',{method:'POST',body:'{}'}); }catch(e){} refresh(); }
// v2.7.15 — porządek w buttonach akcji: prostymi słowami co kliknięcie potwierdza i jaki będzie status/substep
const ACTION_HELP = {
  approve: 'Potwierdzam decyzję → status READY, substep: kontynuacja od wypracowanego etapu — agent sam wznawia (pełne testy + CR)',
  decline: 'Odrzucam zadanie → status BLOCKED, substep OWNER_PACKET — zamknięte; odblokowanie tylko ręcznie przez READY',
  retry: 'Wznawiam ręcznie → READY, retry=0 — na koniec kolejki wg sortowania (priorytet → postęp → wiek)',
  merge: 'Wdrażam teraz → orchestrator merguje gałąź (rebase + regression + push) → status MERGED',
  finish_stage: 'Dokończ i pauzuj: worker kończy bieżący etap → status READY (postęp zachowany, wznowienie z kolejki)',
  comment: 'Tylko komentarz — bez zmiany statusu; leci do zadania i na PR na GitHubie',
  set_priority: 'Tylko priorytet P0–P3 — bez zmiany statusu; ustawia pozycję w kolejce'
};
// v2.7.27 — ❤️ ulubione zadania: serduszko przy KAŻDYM zadaniu (aktywnym i nieaktywnym);
// ulubione lądują na szczycie tabeli zadań i są śledzone między urządzeniami (kv serwera)
function favMark(id){
  const on = FAVS.has(id);
  return `<span class="fav ${on?'on':''}" title="${on?'Ulubione — kliknij, aby przestać śledzić':'Kliknij, aby śledzić (❤️)'}" onclick="event.stopPropagation();toggleFav('${esc(id)}')">${on?'❤️':'🤍'}</span>`;
}
async function toggleFav(id){
  try{ const r = await fetch('/api/favorites', {method:'POST', body:JSON.stringify({task_id:id})}); const j = await r.json();
       toast(j.favorite ? ('❤️ śledzisz: '+id) : ('🤍 przestałeś śledzić: '+id)); }catch(e){ toast('⚠ '+e); }
  refresh();
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
      return `<button class="tbtn" title="${esc((typeof ACTION_HELP!=='undefined'&&ACTION_HELP[a])||ad.label||a)}" onclick="event.stopPropagation();flowAction('${esc(t.id)}','${esc(a)}')">${ad.icon||''} ${esc(ad.label||a)}</button>`; }).join('');
    return `<div class="dec-card ${t.status==='ESCALATED'?'esc':''}" onclick="openTask('${esc(t.id)}')">
      <div>${favMark(t.id)}<b>${esc(t.id)}</b> ${sbadge(t.status)} ${pbadge(t.project_id)} <span class="prio prio-${t.priority||'P2'}">${t.priority||'P2'}</span></div>
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
     <div style="font-size:11px">${esc(String(a.payload||'').slice(0,200))}</div>
     <div style="margin-top:4px">
       <button class="tbtn" style="background:#238636;border-color:#2ea043;color:#fff" onclick="approvalAction('${esc(a.id)}','approve')">🚀 Deploy → PROD</button>
       <button class="tbtn" style="background:#da3633;border-color:#f85149;color:#fff" onclick="approvalAction('${esc(a.id)}','decline')">🛑 Odrzuć</button>
     </div></div>`).join('')
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
    `<div class="brow" onclick="openTask('${esc(t.id)}')">${favMark(t.id)}<span class="prio prio-${t.priority||'P2'}">${t.priority||'P2'}</span>
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
      ${favMark(t.id)}<b>${esc(t.id)}</b> ${sbadge(t.status)} <span class="prio prio-${t.priority||'P2'}">${t.priority||'P2'}</span> ${esc((t.title_sponsor||t.title||'').slice(0,70))}<br>
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
let lastActionsDef = null;  // v2.7.12: actions_def z ostatnio otwartego zadania (dla flowAction poza flow)
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
      const din = st.delta_in || 0, dout = st.delta_out || 0;
      const dlt = (din || dout) ? ` <span style="color:${din?'#3fb950':'#8b949e'}">+${din}</span><span style="color:${dout?'#f85149':'#8b949e'}">−${dout}</span>` : '';
      return `<button class="pchip ${off?'off':''}" style="--c:${st.color}" title="klik = ukryj/pokaż etap · dwuklik = SOLO (tylko ten etap) · +/− = delta z ostatnich ${Math.round((st.delta_window_s||300)/60)} min" ondblclick="soloFlowStage('${esc(st.id)}')" onclick="toggleFlowStage('${esc(st.id)}')">${st.icon} ${esc(st.label)} <b>${st.count}</b>${dlt}</button>`;
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
      // delta z ostatnich 5 min (metryki task_event): +weszło −wyszło
      const din = st.delta_in || 0, dout = st.delta_out || 0, win = st.delta_window_s || 300;
      const delta = '<tspan fill="' + (din ? '#3fb950' : (T.muted||'#8b949e')) + '" font-weight="bold" title="przybyło zadań w ostatnich ' + Math.round(win/60) + ' min">+' + din + '</tspan>' +
        '<tspan fill="' + (dout ? '#f85149' : (T.muted||'#8b949e')) + '" font-weight="bold" title="ubyło zadań w ostatnich ' + Math.round(win/60) + ' min"> −' + dout + '</tspan>' +
        '<tspan fill="' + (T.muted||'#8b949e') + '"> (' + Math.round(win/60) + ' min)</tspan>';
      s += '<g class="stage ' + (st.hot && st.count ? 'hot' : '') + '">' +
        '<rect class="box" x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="12" stroke="' + st.color + '"/>' +
        '<text class="ttl" x="' + (x+10) + '" y="' + (y+22) + '" font-size="15" onclick="openStageInfo(\\'' + esc(st.id) + '\\')">' + st.icon + ' <tspan font-size="12" font-weight="bold" fill="' + (T.nodeTitle||'#c9d1d9') + '">' + esc(st.label) + '</tspan></text>' +
        '<circle cx="' + (x+w-18) + '" cy="' + (y+19) + '" r="15" fill="' + st.color + '"/><text class="cnt" x="' + (x+w-18) + '" y="' + (y+24) + '" font-size="13" font-weight="bold" text-anchor="middle" fill="#0b0f14">' + st.count + '</text>' +
        '<text x="' + (x+10) + '" y="' + (y+38) + '" font-size="9" fill="' + (T.muted||'#8b949e') + '">' + delta + ' · ' + esc((st.statuses||[]).join(' · ')) + over + '</text>';
      const chips = st.tasks.slice(0, maxChips);
      const PIC = {'P0':'🔥','P1':'⚡','P2':'•','P3':'🐢'};
      chips.forEach((t, ci) => {
        const cx = x+8, cy = y + 52 + ci*(chipH+5);
        const acts = t.available_actions || [];
        const clk = (t.status==='RUNNING' && t.started_at)
          ? '<tspan class="clk" data-started="' + t.started_at + '"> ⏱' + fmtDur(Date.now()/1000 - t.started_at) + '</tspan>'
          : (t.overdue ? ' <tspan fill="' + (T.overdue||'#f85149') + '">⚠' + fmtDur(t.age) + '</tspan>' : ' <tspan class="who">' + fmtDur(t.age) + '</tspan>');
        const cpct = (typeof t.progress === 'number') ? Math.round(t.progress*100) : 0;
        const cbar = (t.substep && cpct > 0) ? '<rect x="0" y="' + (chipH-4) + '" width="' + (w-16) + '" height="3" rx="1.5" fill="#21262d"/>' +
          '<rect x="0" y="' + (chipH-4) + '" width="' + Math.round((w-16)*cpct/100) + '" height="3" rx="1.5" fill="#3fb950"/>' +
          '<title>postęp ' + cpct + '% (' + esc(t.substep) + ')</title>' : '';
        s += '<g class="chip ' + (t.overdue?'overdue':'') + '" transform="translate(' + cx + ',' + cy + ')" onclick="openTask(\\'' + esc(t.id) + '\\')">' +
          '<rect width="' + (w-16) + '" height="' + chipH + '" rx="6"/>' + cbar +
          '<text x="6" y="13" font-size="10"><tspan>' + (PIC[t.priority]||'') + '</tspan> <tspan class="cid" fill="' + st.color + '">' + esc(t.id) + '</tspan>' + clk + '</text>' +
          '<text class="who" x="6" y="24" font-size="8">' + esc((t.persona||t.role||'').slice(0,16)) + (t.pr ? (' · !'+t.pr) : '') + (t.substep && cpct>0 ? (' · ' + cpct + '%') : '') + '</text>';
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
    '<div class="kv"><b>zadań:</b> <b style="font-size:18px;color:' + st.color + '">' + st.count + '</b>' +
    ' · delta ostatnie ' + Math.round((st.delta_window_s||300)/60) + ' min: <span style="color:#3fb950;font-weight:bold">+' + (st.delta_in||0) + '</span> / <span style="color:#f85149;font-weight:bold">−' + (st.delta_out||0) + '</span>' +
    (st.overdue ? ' · <span style="color:#f85149">⚠ ' + st.overdue + ' po SLA</span>' : '') + (st.sla_s ? ' · SLA: ' + fmtDur(st.sla_s) : '') + '</div>' +
    '<div class="kv muted">' + esc(st.description||'') + '</div>' +
    '<div class="kv"><b>akcje w etapie:</b> ' + (st.actions||[]).map(a => { const ad = (f.actions||{})[a]||{}; return (ad.icon||'') + ' ' + esc(ad.label||a); }).join(' · ') + '</div>' +
    '<h2>Zadania w etapie (' + st.count + ')</h2>' +
    '<div class="histbox">' + (st.tasks.map(t =>
      '<div onclick="openTask(\\'' + esc(t.id) + '\\')" style="cursor:pointer">' + favMark(t.id) +
      '<b style="color:' + st.color + '">' + esc(t.id) + '</b> ' + esc((t.title||'').slice(0,60)) + '<br>' +
      '<span class="muted">' + esc(t.status) + ' · ' + esc(t.priority||'P2') + ' · ' + esc(t.persona||t.role||'') + ' · ' + pbadge(t.project_id) + ' · ' + (t.overdue ? '<span style="color:#f85149">⚠</span> ' : '') + fmtDur(t.age) + '</span><br>' +
      (t.available_actions||[]).map(a => { const ad = (f.actions||{})[a]||{}; return '<button class="tbtn" onclick="event.stopPropagation();flowAction(\\'' + esc(t.id) + '\\',\\'' + esc(a) + '\\')">' + (ad.icon||'') + ' ' + esc(ad.label||a) + '</button>'; }).join('') +
      '</div>').join('') || '<span class="muted">pusto</span>') + '</div>';
}
async function flowAction(taskId, actionId){
  const ad = ((lastFlow && lastFlow.actions) || lastActionsDef || (lastSponsor && lastSponsor.actions_def) || {})[actionId] || {};
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
  const cur = n.current ? `<div class="cur" onclick="closeDetail();openTask('${esc(n.current.id)}')" style="cursor:pointer">🔨 <b>${esc(n.current.id)}</b> ${esc(n.current.substep||'tura')} <span class="prio prio-${n.current.priority||'P2'}">${n.current.priority||'P2'}</span> <span class="clock" data-started="${n.current.started_at||0}">⏱ ${fmtDur(n.current.elapsed||0)}</span></div>` : (n.next ? `<div class="cur">następna tura: <b>${esc(n.next.id)}</b> ${esc(n.next.step||'')}</div>` : '<div class="cur muted">— wolny/a (brak bieżącego zadania) —</div>');
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
      ${n.role ? `<button class="tbtn" onclick="startThread('${esc(n.role)}')">💬 rozmowa z ${esc(n.name||n.role)}</button> <button class="tbtn" onclick="openDuties('${esc(n.role)}')">📋 obowiązki</button> <button class="tbtn" style="border-color:#a371f7;color:#a371f7" onclick="openPersonCfg('${esc(n.role)}','${esc(n.name||n.role)}')">⚙️ ustawienia · 🎓 szkolenia</button>` : ''}
      <button class="tbtn" onclick="closeDetail()">✖ Zamknij</button>
    </div>`;
}
// ===== ⚙️ profil pracownika: ustawienia + skille + historia + szkolenie (v2.7.11) =====
async function openPersonCfg(role, name){
  curTask = null; curThread = null;
  document.getElementById('detail').classList.add('open');
  let d = {skills: []};
  try { const r = await fetch('/api/role/'+encodeURIComponent(role)+'/skills'); d = await r.json(); } catch(e) {}
  document.getElementById('detailBody').dataset.key = 'pcfg|' + role + '|' + (d.skills||[]).length;
  const skills = d.skills || [];
  const skillRows = skills.length ? skills.map(sk => `
    <div style="border-bottom:1px solid #21262d;padding:4px 0">
      <b>🎓 ${esc(sk.name)}</b> <span class="muted" style="font-size:10px">${new Date(sk.ts*1000).toLocaleString()} · źródło: ${esc(sk.source)}</span>
      ${sk.description ? `<div class="muted" style="font-size:11px">${esc(sk.description.slice(0,300))}</div>` : ''}
    </div>`).join('')
    : '<span class="muted">brak dodanych szkoleń — użyj formularza niżej</span>';
  document.getElementById('detailBody').innerHTML = `
    <h3>⚙️ Ustawienia: ${esc(name||role)} <span class="muted" style="font-size:12px">rola <code>${esc(role)}</code></span></h3>
    <div class="kv muted" style="font-size:11px">Plik skilli roli: <code>${esc(d.file||('.agents/skills/roles/'+role+'.md'))}</code> — trafia do promptu każdego zadania tej roli. Historia poniżej = kiedy który skill został nadany.</div>
    <h2>🎓 Szkolenia — nadane skille (${skills.length})</h2>
    <div class="histbox" style="max-height:200px">${skillRows}</div>
    <h2>➕ Dodaj skill (szkolenie)</h2>
    <div class="kv"><input id="skillName" placeholder="nazwa skilla, np. 'testy E2E w Playwright'" style="width:60%"></div>
    <div class="kv"><textarea id="skillDesc" placeholder="co dokładnie ma potrafić agent po tym szkoleniu…" style="width:100%;height:70px"></textarea></div>
    <button class="tbtn" style="background:#238636;border-color:#2ea043;color:#fff" onclick="addSkill('${esc(role)}')">➕ Dodaj skill (szkolenie)</button>
    <div class="muted" style="font-size:10px;margin-top:4px">Skill dopisuje się do pliku roli i od razu ląduje w promptcie agenta; wpisuje się też do historii (tabela role_skills).</div>
    <div style="margin-top:8px"><button class="tbtn" onclick="closeDetail()">✖ Zamknij</button></div>`;
}
async function addSkill(role){
  const name = (document.getElementById('skillName').value||'').trim();
  const description = (document.getElementById('skillDesc').value||'').trim();
  if(!name || !description){ alert('Podaj nazwę i opis szkolenia.'); return; }
  const r = await fetch('/api/role/'+encodeURIComponent(role)+'/skill', {method:'POST',
    body: JSON.stringify({name, description})});
  const res = await r.json();
  if(!r.ok){ alert('Błąd: ' + (res.error||r.status)); return; }
  if(res.findings && res.findings.length){ alert('Ostrzeżenia security scan:\\n' + res.findings.join('\\n')); }
  openPersonCfg(role, role);
  refresh();
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
function renderProcessStatus(pc, tid) {
  const bar = document.getElementById('processStatusBar');
  if(!bar) return;
  const sup = pc && pc.supervisor;
  if(sup){
    if(sup.alive){
      bar.innerHTML = `<div class="kv" style="color:#3fb950">⚙️ <b>🟢 ${esc(sup.state||'ZYJE')}</b> ${esc(sup.module||'')} pid ${sup.pid} · ${esc(sup.phase||'')} · ${esc(sup.step||'')} <span class="muted">${esc(sup.cmd||'')}</span></div>`;
    } else {
      bar.innerHTML = `<div class="kv" style="color:#f85149">⚙️ <b>🔴 ${esc(sup.state||'SKONCZYL')}</b> pid ${sup.pid} rc=${sup.rc==null?'?':sup.rc} · ${esc(sup.phase||'')} <span class="muted">${esc(sup.cmd||'')}</span></div>`;
    }
    return;
  }
  if(!pc || !pc.status || pc.pid === null || pc.pid === undefined){
    bar.innerHTML = '<div class="kv" style="color:#8b949e">⚙️ Proces: nieznany</div>';
    return;
  }
  const status = pc.status;
  const pid = pc.pid;
  const rss = pc.rss_mb ? pc.rss_mb.toFixed(0) : '?';
  const uptime = pc.uptime_s ? fmtDur(pc.uptime_s) : '?';
  const orphans = pc.orphan_pids || [];
  const has_duplicates = pc.has_duplicate_workers || false;
  let html = '';
  if(status === 'RUNNING'){
    html = `<div class="kv" style="color:#3fb950">⚙️ Proces: <b style="color:#3fb950">🟢</b> worker-${pid} działa (PID ${pid} · RAM: ${rss} MB · czas: ${uptime})</div>`;
  } else if(status === 'DEAD'){
    html = `<div class="kv" style="color:#f85149">⚙️ Proces: <b style="color:#f85149">🔴 ZATRZYMANY</b> — brak procesu PID ${pid} w systemie!</div>`;
    html += `<button class="tbtn" style="border-color:#f85149;" onclick="forceRetryTask('${esc(tid)}')">♻️ Odblokuj</button>`;
  } else if(status === 'ZOMBIE'){
    html = `<div class="kv" style="color:#d29922">⚙️ Proces: <b style="color:#d29922">💀 ZOMBIE</b> (PID ${pid} defunct)</div>`;
  }
  if(has_duplicates && orphans.length > 0){
    html += `<div class="kv" style="color:#d29922">⚠️ Stare procesy: PID ${orphans.join(', ')}</div>`;
    html += `<button class="tbtn" onclick="killOrphans('${esc(tid)}')">🧹 Zabij stare</button>`;
  }
  bar.innerHTML = html;
}
async function forceRetryTask(tid){
  if(!confirm(`Odblokować ${tid}?`)) return;
  try{
    const r = await fetch('/api/task/'+encodeURIComponent(tid)+'/force_retry', {method: 'POST'});
    const j = await r.json();
    toast(j.ok ? ('♻️ '+tid+': '+(j.from||'?')+' → '+(j.to||'?')) : ('⚠ '+(j.error||'Błąd'))); refresh();
  }catch(e){ toast('⚠ '+e); }
}
async function killOrphans(tid){
  if(!confirm(`Zabić osierocone procesy dla ${tid}?`)) return;
  try{
    const r = await fetch('/api/task/'+encodeURIComponent(tid)+'/kill-orphans', {method: 'POST'});
    const j = await r.json();
    toast(`🧹 Zabito ${j.killed.length} procesów`);
    setTimeout(() => loadTaskConsole(tid), 1000);
  }catch(e){ toast('⚠ '+e); }
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
  // v2.7.11: licznik bieżącego mikro-etapu („teraz: …”) tyka co sekundę
  document.querySelectorAll('.subclk[data-subsince]').forEach(el => {
    const s = parseFloat(el.dataset.subsince); if(s > 0) el.textContent = fmtDur(Date.now()/1000 - s);
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
        tasks.append({k: t.get(k) for k in ("id", "title", "status", "substep", "substep_since", "retry_count",
                                             "role", "project_id", "branch", "updated_at", "read_only",
                                             "mode", "parent_task", "started_at", "claimed_by", "priority",
                                             "review_notes", "created_at", "title_sponsor", "eta_s")})
        from engine.state import substep_progress
        tasks[-1]["progress"] = t.get("progress") if t.get("progress") is not None else substep_progress(t.get("substep"))
        from engine.state import dispatch_lane
        tasks[-1]["lane"] = dispatch_lane(t) or ""
        tasks[-1]["age"] = max(0, time.time() - (t.get("updated_at") or t.get("created_at") or time.time()))
        tasks[-1]["pr"] = github_bridge.pr_for_task(t["id"])
        p = personas.persona_for_role(t.get("role") or "")
        tasks[-1]["persona"] = f"{p['name']} · {p['position']}"
        tasks[-1]["depth"] = depth(t)
        if t.get("status") == "RUNNING" and t.get("started_at"):
            tasks[-1]["elapsed"] = max(0, time.time() - t["started_at"])
        # v2.7.19: ETA — ile zostało i kiedy skończy (dla RUNNING)
        eta_s = float(t.get("eta_s") or 0)
        if eta_s and t.get("status") == "RUNNING":
            left = max(0.0, eta_s - (tasks[-1].get("elapsed") or 0))
            tasks[-1]["eta_left_s"] = left
            tasks[-1]["eta_at"] = time.time() + left
        if t.get("status") == "RUNNING":
            tasks[-1]["res"] = _proc_stats(t.get("claimed_by"))
            from engine import console_streamer as _cs
            _la = _cs.last_activity_at(t["id"])
            if _la == 0.0:
                _log_p = ROOT / ".agents" / "logs" / f"worker_lifecycle.{t['id']}.log"
                try:
                    _la = _log_p.stat().st_mtime
                except FileNotFoundError:
                    _la = t.get("updated_at") or time.time()
            _idle = max(0.0, time.time() - _la)
            tasks[-1]["last_activity_at"] = _la
            tasks[-1]["idle_s"] = round(_idle, 1)
            _thresh = float(s.get_kv("stall_threshold_s", 600) or 600)
            _llm_busy = bool(s._conn().execute(
                "SELECT 1 FROM llm_jobs WHERE task_id=? AND status='RUNNING' LIMIT 1",
                (t["id"],)).fetchone())
            tasks[-1]["llm_busy"] = _llm_busy
            tasks[-1]["stall"] = (_idle >= _thresh) and not _llm_busy
    tasks.sort(key=lambda t: (t["status"] != "AWAITING_OWNER", t["status"] != "RUNNING", t["status"] != "CHANGES_REQUESTED", -(t["updated_at"] or 0)))
    events = s.recent_events(30)
    # statystyki kolejki: ile czeka, ile już "spóźnionych" (READY/AWAITING starsze niż 1h), ile długo biegnie
    stats = {
        "waiting": sum(1 for t in tasks if t["status"] == "READY"),
        "awaiting": sum(1 for t in tasks if t["status"] == "AWAITING_OWNER"),
        "running": sum(1 for t in tasks if t["status"] == "RUNNING"),
        "overdue": sum(1 for t in tasks if t["status"] in ("READY", "AWAITING_OWNER") and t.get("age", 0) > 3600),
        "running_long": sum(1 for t in tasks if t["status"] == "RUNNING" and (t.get("elapsed") or 0) > 3600),
        # v2.7.19: raport „za ile będzie” — ETA bieżących RUNNING i całej kolejki
        "eta_running_at": max((t.get("eta_at") for t in tasks if t.get("status") == "RUNNING" and t.get("eta_at")), default=None),
        "eta_queue_s": sum(float(t.get("eta_s") or 0) for t in tasks if t["status"] in ("READY", "CHANGES_REQUESTED")),
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
                                  "substep": cur.get("substep") or "", "title": cur.get("title") or "",
                                  "res": _proc_stats(cur.get("claimed_by"))}
                                 if cur else None),
                     "next": None})
    turns = s.next_work()
    taken = {m["current"]["id"] for m in team if m.get("current")}
    for m in team:
        if m.get("current"):
            continue
        nxt = next((t for t in turns if t["role"] == m["role"] and t["task_id"] not in taken), None)
        if nxt:
            m["next"] = {"id": nxt["task_id"], "step": nxt["step"], "lane": nxt["lane"],
                         "priority": nxt["priority"], "title": nxt["title"]}
            taken.add(nxt["task_id"])
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
    # v2.7.15 — boks Opiekuna „Kolejka & Logi”: kolejka w kolejności pobierania + logi aplikacji + powiadomienia
    queue = []
    for i, t in enumerate(s.claimable_tasks()):
        p = personas.persona_for_role(t.get("role") or "")
        queue.append({"pos": i + 1, "id": t["id"], "title": t.get("title") or "",
                      "title_sponsor": t.get("title_sponsor") or "", "priority": t.get("priority") or "P2",
                      "substep": t.get("substep") or "", "progress": t.get("progress") if t.get("progress") is not None else substep_progress(t.get("substep")),
                      "retry_count": t.get("retry_count") or 0, "role": t.get("role") or "",
                      "persona": p.get("name") or "", "project_id": t.get("project_id"),
                      "age": max(0, time.time() - (t.get("created_at") or time.time())),
                      "eta_s": float(t.get("eta_s") or 0),
                      "review_notes": (t.get("review_notes") or "")[:200]})
    applogs = []
    for r in s._conn().execute(
            "SELECT ts, kind, task_id, payload FROM metrics ORDER BY ts DESC LIMIT 150").fetchall():
        try:
            pl = json.loads(r[3])
        except Exception:
            pl = {"raw": str(r[3])[:200]}
        applogs.append({"ts": r[0], "kind": r[1], "task_id": r[2],
                        "payload": json.dumps(pl, ensure_ascii=False)[:220]})
    return {"icons": ICON, "projects": projs, "tasks": tasks, "events": events, "team": team, "asks": asks,
            "new_hires": new_hires,
            "stats": stats,
            "comments": s.recent_comments(40), "metrics": metrics,
            "runtime": runtime, "logs": logs,
            "queue": queue, "applogs": applogs,
            "notifications": s.list_notifications(60), "notif_unread": s.unread_notifications(),
            "ops": s.recent_ops(100), "ops_stats": s.ops_stats(),  # v2.7.23: rejestr operacji
            "favorites": s.list_favorites(),  # v2.7.27: ❤️ ulubione zadania Sponsora
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
        d = {"id": t["id"], "title": t.get("title") or "", "title_sponsor": t.get("title_sponsor") or "",
                "status": t["status"], "substep": t.get("substep") or "", "priority": t.get("priority") or "P2",
                "role": t.get("role") or "", "persona": personas.persona_label(t.get("role") or ""),
                "project_id": t.get("project_id"), "branch": t.get("branch"),
                "pr": github_bridge.pr_for_task(t["id"]),
                "review_notes": (t.get("review_notes") or "")[:300],
                "acceptance_criteria": (t.get("acceptance_criteria") or "")[:400],
                "scope_paths": t.get("scope_paths") or [], "parent_task": t.get("parent_task"),
                "age": max(0, now - (t.get("updated_at") or t.get("created_at") or now)),
                "started_at": t.get("started_at"), "milestone": t.get("milestone")}
        if t["status"] == "RUNNING":
            _ps = _proc_stats(t.get("claimed_by"))
            d["alive"] = _ps is not None
            d["res"] = _ps
        return d

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

    # actionable: wszystkie zadania, na które Sponsor może zmienić status jednym klikiem
    # (AWAITING_OWNER → approve/decline, READY_TO_MERGE → merge, BLOCKED → retry)
    actionable = []
    for t in tasks:
        if t["status"] in ("AWAITING_OWNER", "ESCALATED", "READY_TO_MERGE", "BLOCKED"):
            stage = flowmod.stage_of_status(cfg, t["status"])
            acts: list[str] = []
            for st in cfg.get("stages", []):
                if st.get("id") == stage:
                    acts = [a for a in (st.get("actions") or [])
                            if not flowmod.validate_action(cfg, t["status"], a)]
            actionable.append({**t_brief(t), "available_actions": acts})
    actionable.sort(key=lambda d: -d["age"])

    prio = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    backlog = sorted((t for t in tasks if t["status"] == "READY"),
                     key=lambda t: (prio.get(t.get("priority") or "P2", 2), -(t.get("created_at") or 0)))
    backlog = [t_brief(t) for t in backlog[:15]]

    return {"milestones": milestones, "decisions": decisions, "actionable": actionable,
            "approvals": s.pending_approvals(),
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
    # v2.7.12: akcje kontekstowe per status (tylko te, które mają sens dla tego zadania teraz)
    from engine.flow import flow as flowmod
    fcfg = flowmod.load_config()
    fstage = flowmod.stage_of_status(fcfg, t["status"])
    favail: list[str] = []
    for st in fcfg.get("stages", []):
        if st.get("id") == fstage:
            favail = [a for a in (st.get("actions") or []) if not flowmod.validate_action(fcfg, t["status"], a)]
    # v2.7.27+: łączny czas pracy nad zadaniem (suma okresów RUNNING; bieżący liczony live)
    try:
        _ev = s._conn().execute(
            "SELECT ts, payload FROM metrics WHERE kind='task_event' AND task_id=? ORDER BY ts ASC",
            (task_id,)).fetchall()
        _run_start, total_work_s = None, 0.0
        for _ts, _pl in _ev:
            try:
                _p = _json.loads(_pl)
            except Exception:
                continue
            _to = str((_p.get("changes") or {}).get("status") or _p.get("to") or "")
            if _to == "RUNNING" and _run_start is None:
                _run_start = float(_ts)
            elif _to != "RUNNING" and _run_start is not None:
                total_work_s += max(0.0, float(_ts) - _run_start)
                _run_start = None
        if t.get("status") == "RUNNING" and _run_start is not None:
            total_work_s += max(0.0, time.time() - _run_start)
    except Exception:
        total_work_s = 0.0

    # v2.7.23: panel „⚙️ Proces diagramu” — kroki, retry, modele LLM, czasy per krok
    try:
        import json as _json
        ev_rows = s._conn().execute(
            "SELECT ts, payload FROM metrics WHERE kind='task_event' AND task_id=? ORDER BY ts DESC LIMIT 30",
            (task_id,)).fetchall()
        steps_hist = []
        for r in ev_rows:
            try:
                pl = _json.loads(r[1])
            except Exception:
                pl = {"raw": str(r[1])[:150]}
            steps_hist.append({"ts": r[0], "from": pl.get("from") or "?", "to": pl.get("to") or "?",
                               "by": pl.get("by") or "", "substep": (pl.get("changes") or {}).get("substep")})
        op_rows = s._conn().execute(
            "SELECT kind, status, COUNT(*) n FROM ops WHERE task_id=? GROUP BY kind, status", (task_id,)).fetchall()
        op_summary = [dict(r) for r in op_rows]
        llm_models = s._conn().execute(
            "SELECT COALESCE(json_extract(payload,'$.model'),'—') m, COUNT(*) n FROM llm_jobs "
            "WHERE requester='gateway' AND created_at > ? GROUP BY m", (time.time() - 86400,)).fetchall()
    except Exception:
        steps_hist, op_summary = [], []

    return {"icons": ICON, "task": t, "total_work_s": total_work_s,
            "comments": s.recent_comments(20, task_id=task_id),
            "events": s.recent_events(50, task_id=task_id),
            "attachments": s.list_attachments(task_id),
            "ops": s.list_ops(task_id, 80),  # v2.7.23: operacje zadania (llm/git/qg/tg) ze statusem kolejki
            "proc": {"steps": steps_hist, "ops_summary": op_summary,
                     "llm_models": (llm_models if 'llm_models' in dir() else [])},
            "available_actions": favail, "actions_def": fcfg.get("actions", {}), "stage": fstage}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # CORS: centrum_dowodzenia (Expo Web) woła API prosto z przeglądarki
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_to_error_agent(self, path: str) -> None:
        """Proxy request do agenta monitorowania błędów (port 8767)."""
        import urllib.request
        try:
            # Usunięcie /api z początku path (np. /api/errors -> /errors)
            agent_path = path.replace("/api", "", 1) if path.startswith("/api") else path
            url = f"http://127.0.0.1:8767{agent_path}"
            
            with urllib.request.urlopen(url, timeout=5) as response:
                body = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
        except Exception as e:
            # Jeśli agent nie działa, zwróć pustą listę
            self._send(503, 
                      json.dumps({"error": f"Error agent not available: {str(e)}", "errors": [], "messages": []}).encode(),
                      "application/json; charset=utf-8")

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
        elif u.path.startswith("/api/errors"):
            # Proxy do agenta monitorowania błędów (port 8767)
            self._proxy_to_error_agent(u.path)
        elif u.path.startswith("/api/llm-messages"):
            # Proxy do agenta monitorowania błędów (port 8767)
            self._proxy_to_error_agent(u.path)
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
        elif u.path == "/api/workers":
            from engine.worker_supervisor import inspect
            s = State()
            rows = inspect(s)
            self._send(200, json.dumps({"workers": rows}, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        elif u.path == "/api/workers/health":
            from engine import console_streamer as cs
            s = State()
            now = time.time()
            try:
                threshold_s = float(s.get_kv("stall_threshold_s", 600) or 600)
            except Exception:
                threshold_s = 600.0
            workers_out = []
            stalled_out = []
            for t in s.list_tasks(status="RUNNING"):
                tid = t["id"]
                la = cs.last_activity_at(tid)
                if la == 0.0:
                    log_p = ROOT / ".agents" / "logs" / f"worker_lifecycle.{tid}.log"
                    try:
                        la = log_p.stat().st_mtime
                    except FileNotFoundError:
                        la = t.get("updated_at") or now
                idle_s = max(0.0, now - la)
                llm_busy = bool(s._conn().execute(
                    "SELECT 1 FROM llm_jobs WHERE task_id=? AND status='RUNNING' LIMIT 1",
                    (tid,)).fetchone())
                is_stalled = (idle_s >= threshold_s) and not llm_busy
                if is_stalled:
                    stalled_out.append(tid)
                workers_out.append({
                    "task_id": tid,
                    "status": t["status"],
                    "substep": t.get("substep"),
                    "claimed_by": t.get("claimed_by"),
                    "last_activity_at": la,
                    "idle_s": round(idle_s, 1),
                    "llm_job_running": llm_busy,
                    "stall": is_stalled,
                    "stall_threshold_s": threshold_s,
                    "console_lines": cs.count_lines(tid),
                })
            self._send(200, json.dumps({
                "workers": workers_out,
                "stalled": stalled_out,
                "stall_threshold_s": threshold_s,
            }, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
        elif u.path == "/api/pipeline/runs":
            # sponsor-shell: lista runów procesów z filtrami (task_id, agent_role, status, limit)
            q = parse_qs(u.query)
            s = State()
            runs = s.list_process_runs(
                task_id=q.get("task_id", [None])[0],
                agent_role=q.get("agent_role", [None])[0],
                status=q.get("status", [None])[0],
                limit=int(q.get("limit", ["100"])[0]))
            self._send(200, json.dumps({"runs": runs, "count": len(runs)},
                                       ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        elif u.path.startswith("/api/pipeline/runs/") and u.path.endswith("/log"):
            # pełny log runu (ANSI-safe: surowy tekst, klient renderuje w <pre>)
            run_id = u.path.split("/")[4]
            s = State()
            run = s.get_process_run(run_id)
            if not run:
                self._send(404, json.dumps({"error": "nieznany run"}).encode(), "application/json")
                return
            log_rel = run.get("log_path") or f".agents/logs/runs/{run_id}.log"
            f = (ROOT / log_rel).resolve()
            if not str(f).startswith(str(ROOT.resolve())) or not f.is_file():
                # awaryjnie: log może jeszcze nie istnieć — zwróć tail z bazy
                body = (run.get("stdout_tail") or "") + "\n--- stderr ---\n" + (run.get("stderr_tail") or "")
                self._send(200, body.encode(), "text/plain; charset=utf-8")
                return
            q = parse_qs(u.query)
            tail = int(q.get("tail", ["0"])[0])  # 0 = cały log
            data = f.read_text(encoding="utf-8", errors="replace")
            if tail > 0:
                data = "\n".join(data.splitlines()[-tail:])
            self._send(200, data.encode(), "text/plain; charset=utf-8")
        elif u.path.startswith("/api/pipeline/runs/"):
            # szczegóły pojedynczego runu
            run_id = u.path.rsplit("/", 1)[-1]
            run = State().get_process_run(run_id)
            self._send(200 if run else 404,
                       json.dumps(run or {"error": "nieznany run"}, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        elif u.path.startswith("/api/agents/") and u.path.endswith("/pipeline"):
            # Agent Execution Pipeline: pełna historia zadań + procesów danego agenta
            role = u.path.split("/")[3]
            s = State()
            self._send(200, json.dumps(s.agent_pipeline(role), ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        elif u.path.startswith("/api/task/") and u.path.endswith("/console"):
            from engine import console_streamer as cs
            parts = [p for p in u.path.split("/") if p]
            tid = parts[2]
            q = parse_qs(u.query)
            tail = int(q.get("tail", ["200"])[0])
            since_ts = float(q.get("since_ts", ["0.0"])[0])
            stream_f = q.get("stream", [None])[0]
            s = State()
            now = time.time()
            la = cs.last_activity_at(tid)
            if la == 0.0:
                log_p = ROOT / ".agents" / "logs" / f"worker_lifecycle.{tid}.log"
                try:
                    la = log_p.stat().st_mtime
                except FileNotFoundError:
                    la = now
            idle_s = max(0.0, now - la)
            try:
                threshold_s = float(s.get_kv("stall_threshold_s", 600) or 600)
            except Exception:
                threshold_s = 600.0
            events = cs.read_tail(tid, tail=tail, since_ts=since_ts, stream_filter=stream_f)
            try:
                fp = str(cs.console_path(tid).relative_to(ROOT))
            except ValueError:
                fp = str(cs.console_path(tid))
            self._send(200, json.dumps({
                "task_id": tid,
                "file_path": fp,
                "total_lines": cs.count_lines(tid),
                "last_activity_at": la,
                "idle_s": round(idle_s, 1),
                "stall": (idle_s >= threshold_s),
                "events": events,
                "process_check": _check_task_process_health(tid, s),
            }, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
        elif u.path.startswith("/api/task/") and u.path.endswith("/process-check"):
            parts = [p for p in u.path.split("/") if p]
            tid = parts[2]
            s = State()
            process_check = _check_task_process_health(tid, s)
            self._send(200, json.dumps({"task_id": tid, "process_check": process_check}, 
                       ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
        elif u.path.startswith("/api/task/") and u.path.endswith("/kill-orphans"):
            parts = [p for p in u.path.split("/") if p]
            tid = parts[2]
            from engine.process_health import get_process_monitor
            monitor = get_process_monitor()
            killed = monitor.kill_orphan_processes(tid)
            self._send(200, json.dumps({"task_id": tid, "killed": killed, "result": "OK"}, 
                       ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
        elif u.path.startswith("/api/task/") and u.path.endswith("/force_retry"):
            parts = [p for p in u.path.split("/") if p]
            tid = parts[2]
            from engine.state import READY
            s = State()
            task = s.get_task(tid)
            if task is None:
                self._send(404, json.dumps({"ok": False, "task_id": tid, "error": "nieznane zadanie"}).encode(),
                           "application/json")
                return
            status = task.get("status") or ""
            if status != "RUNNING":
                # requeue ma sens tylko dla zawieszonego RUNNING; z innych statusów są osobne akcje
                self._send(409, json.dumps({"ok": False, "task_id": tid, "from": status,
                                            "error": f"force_retry tylko z RUNNING (zadanie jest {status})"}).encode(),
                           "application/json")
                return
            s.transition(tid, READY, review_notes=f"{(task.get('review_notes') or '')} | ♻️ Odblokowano z konsoli (brak procesu)")
            self._send(200, json.dumps({"task_id": tid, "ok": True, "from": status, "to": READY,
                                        "action": "requeued"}, ensure_ascii=False).encode(), "application/json")
        elif u.path.startswith("/api/task/"):
            tid = u.path.rsplit("/", 1)[-1]
            self._send(200, json.dumps(task_detail(tid), ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        elif u.path.startswith("/api/role/") and u.path.endswith("/skills"):
            # v2.7.11: skille roli + historia (profil pracownika)
            role = u.path.split("/")[3]
            s = State()
            from engine import skill_compiler, personas
            f = skill_compiler.SKILLS / "roles" / f"{role}.md"
            return self._send(200, json.dumps({
                "role": role,
                "persona": personas.persona_label(role),
                "file": str(f.relative_to(skill_compiler.ROOT)) if f.exists() else None,
                "file_exists": f.exists(),
                "skills": s.list_role_skills(role),
            }, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")
        elif u.path.startswith("/api/attachment/") and u.path.endswith("/view"):
            # v2.7.12: podgląd załącznika jako strona HTML ( nigdy biała kartka — tekst w <pre>, obraz jako <img> )
            from engine.state import ROOT as _ROOT
            import html as _html
            try:
                att_id = int(u.path.split("/")[3])
                att = State().get_attachment(att_id)
                if not att:
                    page = "<h3>❌ nieznany załącznik</h3>"
                else:
                    full = (_ROOT / att["path"]).resolve()
                    ok = str(full).startswith(str(_ROOT.resolve())) and full.is_file()
                    ext = (att.get("kind") or "").lower()
                    raw_url = f"/api/attachment/{att_id}/raw"
                    if not ok:
                        page = f"<h3>❌ plik niedostępny</h3><p class='muted'>{_html.escape(att['path'])}</p>"
                    elif ext in ("png", "jpg", "jpeg", "gif", "svg"):
                        page = (f"<h3>🖼️ {_html.escape(att.get('label') or att['path'])}</h3>"
                                f"<p><a href='{raw_url}'>surowy plik ↗</a></p>"
                                f"<img src='{raw_url}' style='max-width:100%;border-radius:8px'>")
                    else:
                        data = full.read_text(encoding="utf-8", errors="replace")
                        if not data.strip():
                            data = "(plik jest aktualnie pusty — agent jeszcze go nie dokończył)"
                        page = (f"<h3>📎 {_html.escape(att.get('label') or att['path'])}</h3>"
                                f"<p class='muted'>{_html.escape(att['path'])} · {len(data)} znaków · "
                                f"<a href='{raw_url}'>pobierz / surowy ↗</a></p>"
                                f"<pre style='white-space:pre-wrap;background:#0d1117;padding:10px;border-radius:8px;"
                                f"max-height:75vh;overflow:auto;font-size:12px'>{_html.escape(data[:200000])}</pre>")
                doc = ("<!doctype html><html><head><meta charset='utf-8'><title>📎 "
                       + _html.escape(str(att.get('label') or 'załącznik') if att else 'załącznik')
                       + "</title><style>body{background:#0d1117;color:#c9d1d9;font-family:monospace;padding:16px}"
                       "a{color:#58a6ff}.muted{color:#8b949e;font-size:11px}</style></head><body>"
                       + page + "</body></html>")
                self._send(200, doc.encode("utf-8"), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, ("<h3>❌ błąd podglądu</h3><pre>" + _html.escape(repr(e)) + "</pre>").encode(),
                           "text/html; charset=utf-8")
        elif u.path.startswith("/api/attachment/") and u.path.endswith("/raw"):
            # v2.7.10: serwowanie załącznika zadania (bezpiecznie: tylko pliki wewnątrz ROOT repo)
            from engine.state import ROOT as _ROOT
            try:
                att_id = int(u.path.split("/")[3])
                att = State().get_attachment(att_id)
                if not att:
                    self._send(404, '{"error":"nieznany zalacznik"}'.encode(), "application/json")
                    return
                full = (_ROOT / att["path"]).resolve()
                if not str(full).startswith(str(_ROOT.resolve())) or not full.is_file():
                    self._send(404, '{"error":"plik niedostepny"}'.encode(), "application/json")
                    return
                ext = (att.get("kind") or full.suffix.lstrip(".")).lower()
                ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
                         "svg": "image/svg+xml", "json": "application/json", "md": "text/markdown; charset=utf-8",
                         "html": "text/html; charset=utf-8"}.get(ext, "text/plain; charset=utf-8")
                data = full.read_bytes()
                if len(data) > 2_000_000:
                    self._send(413, '{"error":"plik za duzy do podgladu (>2MB)"}'.encode(), "application/json")
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send(500, json.dumps({"error": repr(e)}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_OPTIONS(self):
        # CORS preflight (centrum_dowodzenia z przeglądarki)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        # przyciski UI wołają POST-em także endpointy diagnostyczne (force_retry, kill-orphans,
        # process-check, console) — obsługuje je GET; delegujemy, zamiast wracać 404 „unknown task action"
        if len(parts) == 4 and parts[:2] == ["api", "task"] and parts[3] in (
                "force_retry", "kill-orphans", "process-check", "console"):
            return self._do_GET()
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
        if len(parts) == 4 and parts[:2] == ["api", "role"] and parts[3] == "skill":
            # v2.7.11: szkolenie — dodaj skill do roli (plik roli + historia role_skills)
            import re as _re
            from engine import skill_compiler
            role = parts[2]
            name = str(body.get("name") or "").strip()[:120]
            description = str(body.get("description") or "").strip()[:4000]
            if not name or not description:
                self._send(400, '{"error":"name i description wymagane"}'.encode(), "application/json")
                return
            f = skill_compiler.SKILLS / "roles" / f"{role}.md"
            if not f.exists():
                self._send(400, json.dumps({"error": f"brak pliku roli {role}.md"}).encode(), "application/json")
                return
            findings = skill_compiler.security_scan_skill(description)
            section = (f"\n\n## 🎓 Szkolenie: {name} (dodane {time.strftime('%Y-%m-%d %H:%M')} przez Sponsora)\n"
                       f"{description}\n")
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(section)
            sk = State().add_role_skill(role, name, description, source="sponsor/dashboard")
            self._send(200, json.dumps({"ok": True, "skill": sk, "findings": findings},
                                        ensure_ascii=False, default=str).encode(), "application/json")
            return
        if u.path == "/api/favorites":
            # v2.7.27: ❤️ przełącz ulubione zadanie (body: {task_id}) — stan wspólny (kv), śledzenie przez Sponsora
            tid = str(body.get("task_id") or "").strip()
            if not tid:
                self._send(400, b'{"error":"task_id wymagane"}', "application/json")
                return
            on = State().toggle_favorite(tid)
            self._send(200, json.dumps({"ok": True, "task_id": tid, "favorite": on}).encode(), "application/json")
            return
        if u.path == "/api/notifications/read":
            # v2.7.15: oznacz wszystkie powiadomienia jako przeczytane (dzwonek)
            n = State().mark_notifications_read()
            self._send(200, json.dumps({"ok": True, "marked": n}).encode(), "application/json")
            return
        if len(parts) == 4 and parts[:2] == ["api", "approval"]:
            # v2.7.10: zgoda/odrzucenie DEPLOY_PROD (i innych approvals) z dashboardu
            aid, act = parts[2], parts[3]
            s = State()
            if act == "approve":
                s.resolve_approval(aid, "APPROVED")
                s.record_metric("deploy_approved", {"approval": aid, "via": "dashboard"}, None)
                s.add_comment(None, "sponsor/dashboard", f"🚀 Cel: wdrożyć na produkcję → ✅ Efekt: zgoda na `{aid}` z dashboardu (APPROVED)")
            elif act == "decline":
                s.resolve_approval(aid, "DECLINED")
                s.record_metric("deploy_declined", {"approval": aid, "reason": str(body.get("reason") or "")[:200], "via": "dashboard"}, None)
                s.add_comment(None, "sponsor/dashboard", f"🛑 Cel: odrzucić deploy → ✅ Efekt: `{aid}` odrzucone: {str(body.get('reason') or '')[:120]}")
            else:
                self._send(404, b'{"error":"unknown approval action"}', "application/json")
                return
            self._send(200, json.dumps({"ok": True, "id": aid, "action": act}).encode(), "application/json")
            return
        if len(parts) == 4 and parts[:2] == ["api", "task"]:
            from engine.orchestrator import cmd_approve, cmd_decline, cmd_note
            _, _, tid, action = parts
            s = State()
            task = s.get_task(tid)
            if task is None:
                self._send(404, json.dumps({"ok": False, "error": f"nieznane zadanie: {tid}"}).encode(),
                           "application/json")
                return
            status = task.get("status") or ""

            def _reply(code: int, payload: dict) -> None:
                self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json")

            try:
                if action == "comment":
                    cmd_note(tid, body.get("text", ""))
                    State().add_comment(tid, "sponsor/dashboard", body.get("text", ""))
                    self._send(200, b'{"ok":true}', "application/json")
                elif action in ("approve", "decline"):
                    # decyzja Opiekuna ma sens tylko z AWAITING_OWNER — z innego statusu nie ruszamy zadania
                    if status != "AWAITING_OWNER":
                        _reply(409, {"ok": False, "task_id": tid, "from": status,
                                     "error": f"{action} tylko z AWAITING_OWNER (zadanie jest {status})"})
                        return
                    if action == "approve":
                        cmd_approve(tid)
                    else:
                        cmd_decline(tid, body.get("reason", "odrzucone z dashboardu"))
                    _reply(200, {"ok": True, "task_id": tid, "from": status,
                                 "to": (s.get_task(tid) or {}).get("status")})
                elif action == "stop":
                    # ⏹️ łagodny stop: worker kończy bieżący etap, potem pauza (READY) — bez zabijania procesu
                    # status zostaje RUNNING do granicy etapu (zmienia go worker, nie dashboard)
                    s.request_stop(tid)
                    s.add_comment(tid, "sponsor/dashboard",
                                  "⏹️ STOP zlecony z dashboardu — agent dokończy bieżący etap i pauzuje (→ READY)")
                    _reply(200, {"ok": True, "task_id": tid, "from": status, "to": status,
                                 "note": "stop zlecony — status zmieni worker na granicy etapu"})
                elif action in ("activate", "boost"):
                    # ▶️ aktywacja / ⚡ boost: zdejmij stop i wstaw do kolejki — READY tylko gdy przejście legalne
                    s.clear_stop(tid)
                    if status == "MERGED":
                        _reply(409, {"ok": False, "task_id": tid, "from": status,
                                     "error": "MERGED — zadanie zamknięte, nie ma czego aktywować"})
                        return
                    if status == "READY":
                        _reply(200, {"ok": True, "task_id": tid, "from": status, "to": status,
                                     "note": "już w kolejce READY — status bez zmiany"})
                        return
                    try:
                        s.transition(tid, "READY", role="sponsor/dashboard",
                                     review_notes=("⚡ boost z dashboardu — READY z priorytetem P0"
                                                   if action == "boost" else
                                                   "▶️ aktywacja z dashboardu — agent wstaje i bierze zadanie"))
                    except Exception as exc:
                        _reply(409, {"ok": False, "task_id": tid, "from": status,
                                     "error": f"{action} niemożliwe: {exc}"})
                        return
                    if action == "boost":
                        s.set_priority(tid, "P0")
                    s.add_comment(tid, "sponsor/dashboard",
                                  "\u26a1 BOOST z dashboardu \u2014 zadanie z priorytetem P0 na szczycie kolejki roli"
                                  if action == "boost" else
                                  "▶️ AKTYWACJA z dashboardu — agent wstaje i podejmuje zadanie od wypracowanego etapu")
                    _reply(200, {"ok": True, "task_id": tid, "from": status, "to": "READY"})
                elif action == "kill_and_retry":
                    reason = str(body.get("reason") or "manual kill from dashboard")
                    if status == "MERGED":
                        _reply(409, {"ok": False, "task_id": tid, "from": status,
                                     "error": "MERGED — zadanie zamknięte"})
                        return
                    s.record_metric("kill_and_retry", {"task": tid, "reason": reason}, tid)
                    # COOLING = obecność USPIONE na cooldown (status i etap zostają, orchestrator wybudzi)
                    s.transition(tid, "COOLING", review_notes=f"kill_and_retry: {reason}")
                    s.add_comment(tid, "sponsor/dashboard", f"🔄 ZABIJ I PONÓW z dashboardu: {reason}")
                    fresh = s.get_task(tid) or {}
                    _reply(200, {"ok": True, "task_id": tid, "from": status, "to": fresh.get("status"),
                                 "presence": fresh.get("presence"),
                                 "cooldown_until": fresh.get("cooldown_until")})
                elif action == "dismiss_stall":
                    mins = float(body.get("minutes") or 30)
                    s.set_kv(f"stall_dismissed:{tid}", time.time() + mins * 60)
                    _reply(200, {"ok": True, "task_id": tid, "dismissed_for_mins": mins})
                else:
                    _reply(404, {"ok": False, "error": "unknown task action"})
            except SystemExit as e:
                # cmd_approve/cmd_decline sygnalizują odmowę przez SystemExit — w wątku HTTP
                # skończyłoby się to brakiem odpowiedzi (klik bez efektu i bez błędu)
                _reply(409, {"ok": False, "task_id": tid, "from": status,
                             "error": str(e) or "operacja odrzucona"})
            except Exception as e:
                _reply(400, {"ok": False, "task_id": tid, "from": status, "error": repr(e)})
            return
        if len(parts) == 5 and parts[:3] == ["api", "pipeline", "runs"] and parts[4] == "kill":
            # sponsor-shell: Sponsor zabija zawieszony proces (całą grupę) z poziomu UI
            from engine import sponsor_shell
            run_id = parts[3]
            result = sponsor_shell.kill_run(run_id)
            code = 200 if result.get("ok") else 404
            self._send(code, json.dumps(result, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
            return
        else:
            self._send(404, b"not found", "text/plain")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")  # 0.0.0.0 = dostęp też z LAN (http://<lan_ip>:8765)
    ap.add_argument("--ws-port", type=int, default=8767, help="port WebSocket gateway (live procesy/logi)")
    ap.add_argument("--no-ws", action="store_true", help="wyłącz WebSocket gateway")
    args = ap.parse_args()
    if not args.no_ws:
        from engine.ws_gateway import WsGateway
        WsGateway(args.host, args.ws_port).start()
        print(f"🔌 WebSocket: ws://{args.host}:{args.ws_port}  (live procesy/logi)", flush=True)
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"🏭 Dashboard: http://{args.host}:{args.port}  (Ctrl+C stop)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
