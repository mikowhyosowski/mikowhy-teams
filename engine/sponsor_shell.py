"""sponsor-shell — deterministyczny Command Execution Gateway fabryki.

JEDEN centralny punkt wejścia dla każdej komendy wykonywanej przez agentów i silnik:

    python3 -m engine.sponsor_shell --task <TASK_ID> --agent <ROLE> --step <SUBSTEP> -- <komenda...>

Gwarancje:
  * izolacja: każda komenda startuje w NOWEJ SESJI i GRUPIE PROCESÓW
    (subprocess start_new_session=True) — pid == pgid, więc os.killpg trafia
    w całe drzewo procesów potomnych, bez sierot/zombie;
  * rejestracja: started_at (ISO-8601 UTC), pid, pgid, cwd, command, timeout_s
    lądują w tabeli process_runs (.agents/state.db) PRZED startem procesu;
  * live capture: stdout/stderr czytane non-blocking w czasie rzeczywistym
    (selectors), pełny log w .agents/logs/runs/<run_id>.log, tail do bazy;
  * sprzątanie: timeout / SIGINT / kill z UI → killpg SIGTERM, eskalacja
    SIGKILL po grace period; status TIMEOUT / KILLED, reaping przez waitpid
    (zero zombie);
  * wynik: ustrukturyzowany słownik {run_id, status, exit_code, duration_ms,
    log_path, stdout_tail, stderr_tail, ...} — CLI drukuje go jako JSON.

Integracja: llm_gateway (vibe CLI), git_guard (mutujące git) i quality_gates
przechodzą przez run_command(), gdy kontekst Sponsora jest dostępny
(zmienne środowiskowe SPONSOR_TASK_ID / SPONSOR_AGENT_ROLE / SPONSOR_STEP
lub jawne argumenty) — każdy krok agenta w jednym spójnym modelu process_runs.
"""
from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
# ten sam root co warstwa stanu (DARK_FACTORY_ROOT / worktree → repo macierzysty)
from engine.state import ROOT as _STATE_ROOT  # noqa: E402

ROOT = _STATE_ROOT
RUNS_LOG_DIR = ROOT / ".agents" / "logs" / "runs"

# statusy runów — spójny model z process_runs.status
QUEUED = "QUEUED"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
TIMEOUT = "TIMEOUT"
KILLED = "KILLED"

TERMINAL_STATUSES = {SUCCEEDED, FAILED, TIMEOUT, KILLED}

DEFAULT_TIMEOUT_S = 600.0
KILL_GRACE_S = 3.0          # SIGTERM -> SIGKILL window
TAIL_CHARS = 4000           # ile znaków stdout/stderr trzymamy w kolumnach *_tail


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_run_id() -> str:
    """ULID-ish: czasowo sortowalny UUID (monotoniczny w praktyce dla UI)."""
    return uuid.uuid4().hex[:26]


def _state():
    """Lazy import — testy podstawiają własną bazę przez DARK_FACTORY_ROOT/State(db_path)."""
    from engine.state import State
    return State()


def _register_run(rec: dict[str, Any]) -> None:
    try:
        _state().add_process_run(rec)
    except Exception:
        pass  # logowanie runu nigdy nie może zabić samego wykonania komendy


def _update_run(run_id: str, **fields: Any) -> None:
    try:
        _state().update_process_run(run_id, **fields)
    except Exception:
        pass


def _finalize_run(run_id: str, fields: dict[str, Any]) -> None:
    """Finalny update runu odporny na wyścig ze Sponsorem zabijającym z zewnątrz:
    jeśli w bazie jest już KILLED/TIMEOUT, nie nadpisujemy go słabszym statusem."""
    try:
        s = _state()
        row = s.get_process_run(run_id)
        if row and row.get("status") in (KILLED, TIMEOUT) and fields.get("status") not in (KILLED, TIMEOUT):
            fields = dict(fields)
            fields["status"] = row["status"]
        s.update_process_run(run_id, **fields)
    except Exception:
        pass


def kill_process_group(pgid: int, grace_s: float = KILL_GRACE_S) -> str:
    """SIGTERM -> (grace) -> SIGKILL na CAŁĄ grupę procesów. Zwraca 'TERM' | 'KILL' | 'GONE'.

    Odporne na wyścigi: ProcessLookupError = grupa już nie żyje.
    """
    if pgid <= 0:
        return "GONE"
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "GONE"
    except PermissionError:
        return "GONE"
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)  # tylko sprawdź istnienie
        except ProcessLookupError:
            return "TERM"
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
        return "KILL"
    except ProcessLookupError:
        return "TERM"


def run_command(command: list[str] | str, *, task_id: str | None = None,
                agent_role: str | None = None, substep: str | None = None,
                cwd: Path | str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S,
                env: dict[str, str] | None = None, run_id: str | None = None,
                on_line: Callable[[str, str], None] | None = None,
                register: bool = True) -> dict[str, Any]:
    """Python API Sponsora: deterministyczne wykonanie komendy w izolowanej grupie procesów.

    Zwraca ustrukturyzowany wynik (taki sam jak CLI):
      {run_id, task_id, agent_role, substep, command, cwd, pid, pgid, status,
       exit_code, started_at, finished_at, duration_ms, timeout_s, log_path,
       stdout_tail, stderr_tail}
    """
    if isinstance(command, str):
        command = ["/bin/sh", "-c", command]
    command = [str(c) for c in command]
    if not command:
        raise ValueError("pusta komenda")
    run_id = run_id or _new_run_id()
    cwd_path = Path(cwd) if cwd else ROOT
    RUNS_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RUNS_LOG_DIR / f"{run_id}.log"
    try:
        log_rel = str(log_path.relative_to(ROOT))
    except ValueError:
        log_rel = str(log_path)

    started_at = _utcnow_iso()
    started_mono = time.monotonic()
    rec: dict[str, Any] = {
        "run_id": run_id, "task_id": task_id, "agent_role": agent_role,
        "substep": substep, "command": " ".join(command), "cwd": str(cwd_path),
        "status": QUEUED, "started_at": started_at, "timeout_s": float(timeout_s),
        "log_path": log_rel,
    }
    if register:
        _register_run(rec)

    proc = subprocess.Popen(
        command, cwd=str(cwd_path), env=env or os.environ.copy(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,  # nowa sesja + grupa: pid == pgid
    )
    pid = proc.pid
    pgid = os.getpgid(pid)  # == pid przy start_new_session
    if register:
        _update_run(run_id, status=RUNNING, pid=pid, pgid=pgid)

    status = SUCCEEDED
    exit_code: int | None = None
    sel = selectors.DefaultSelector()
    out_buf: list[str] = []
    err_buf: list[str] = []
    timed_out = False
    try:
        with log_path.open("a", encoding="utf-8") as log_f:
            log_f.write(f"# sponsor-shell run_id={run_id} task={task_id} agent={agent_role} "
                        f"step={substep}\n# started_at={started_at} pid={pid} pgid={pgid}\n"
                        f"# cmd: {rec['command']}\n# cwd: {cwd_path}\n")
            # pętla: non-blocking drain z krótkim selectem, żeby deadline (timeout) był responsywny
            deadline = started_mono + float(timeout_s)
            while _streams_open(proc):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                _drain_until(proc, sel, log_f, out_buf, err_buf, on_line, timeout=min(0.25, remaining))
    finally:
        if timed_out:
            kill_process_group(pgid)
            status = TIMEOUT
        # reap z grace: killujemy grupę TYLKO gdy proces naprawdę jeszcze żyje
        # (po EOF strumieni poll() może chwilowo zwracać None mimo kończącego się procesu)
        try:
            exit_code = proc.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            kill_process_group(pgid)
            status = KILLED
            exit_code = proc.wait(timeout=5)
        if not timed_out and status == SUCCEEDED:
            # jeśli Sponsor z zewnątrz już oznaczył run (KILLED/TIMEOUT) — nie nadpisuj
            if register:
                try:
                    row_status = _state().get_process_run(run_id)["status"]
                    if row_status in (KILLED, TIMEOUT):
                        status = row_status
                except Exception:
                    pass
            if status == SUCCEEDED:
                status = SUCCEEDED if exit_code == 0 else FAILED
        sel.close()

    finished_at = _utcnow_iso()
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    stdout_tail = "".join(out_buf)[-TAIL_CHARS:]
    stderr_tail = "".join(err_buf)[-TAIL_CHARS:]
    result = {
        "run_id": run_id,
        "task_id": task_id,
        "agent_role": agent_role,
        "substep": substep,
        "command": rec["command"],
        "cwd": str(cwd_path),
        "pid": pid,
        "pgid": pgid,
        "status": status,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "timeout_s": float(timeout_s),
        "log_path": rec["log_path"],
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }
    if register:
        _finalize_run(run_id, {"status": status, "exit_code": exit_code, "finished_at": finished_at,
                               "duration_ms": duration_ms, "stdout_tail": stdout_tail,
                               "stderr_tail": stderr_tail})
        # wynik zgodny z tym, co faktycznie jest w bazie (np. Sponsor ubił run w trakcie)
        try:
            row = _state().get_process_run(run_id)
            if row and row.get("status") in TERMINAL_STATUSES:
                result["status"] = row["status"]
        except Exception:
            pass
    return result


def _streams_open(proc: subprocess.Popen) -> bool:
    return (proc.stdout is not None and not proc.stdout.closed) or \
           (proc.stderr is not None and not proc.stderr.closed)


def _drain_until(proc: subprocess.Popen, sel: selectors.DefaultSelector,
                 log_f, out_buf: list[str], err_buf: list[str],
                 on_line: Callable[[str, str], None] | None, timeout: float) -> None:
    """Jak _drain, ale zamiast czekać do EOF — pojedynczy krok select(timeout)."""
    if not getattr(sel, "_registered", False):
        if proc.stdout is not None:
            sel.register(proc.stdout, selectors.EVENT_READ, ("stdout", proc.stdout, out_buf))
        if proc.stderr is not None:
            sel.register(proc.stderr, selectors.EVENT_READ, ("stderr", proc.stderr, err_buf))
        sel._registered = True  # type: ignore[attr-defined]
    for key, _ in sel.select(timeout=timeout):
        name, stream, buf = key.data
        try:
            chunk = os.read(stream.fileno(), 65536)
        except (BlockingIOError, OSError):
            continue
        if not chunk:
            try:
                sel.unregister(stream)
            except Exception:
                pass
            stream.close()
            continue
        text = chunk.decode("utf-8", errors="replace")
        log_f.write(text)
        log_f.flush()
        buf.append(text)
        if on_line:
            for ln in text.split("\n"):
                if ln:
                    on_line(name, ln)


def kill_run(run_id: str) -> dict[str, Any]:
    """Bezpieczne zabicie zawieszonego runu przez Sponsora (z UI / API).

    Czyta pid/pgid ze stanu, zabija grupę procesów, ustawia status KILLED.
    """
    s = _state()
    run = s.get_process_run(run_id)
    if not run:
        return {"run_id": run_id, "ok": False, "error": "nieznany run"}
    if run["status"] in TERMINAL_STATUSES:
        return {"run_id": run_id, "ok": True, "status": run["status"], "already_terminal": True}
    pgid = run.get("pgid") or run.get("pid")
    # NAJPIERW status KILLED w bazie (zamyka wyścig z wątkiem runu czytającym stan),
    # POTEM killpg — proces umiera, a run już wie, że został zabity przez Sponsora
    s.update_process_run(run_id, status=KILLED)
    how = kill_process_group(int(pgid)) if pgid else "GONE"
    finished_at = _utcnow_iso()
    s.update_process_run(run_id, finished_at=finished_at,
                         exit_code=-signal.SIGKILL, stderr_tail=(run.get("stderr_tail") or "") +
                         f"\n[sponsor-shell] zabito grupę pgid={pgid} ({how}) o {finished_at}")
    return {"run_id": run_id, "ok": True, "status": KILLED, "pgid": pgid, "kill": how}


def context_from_env() -> dict[str, str | None]:
    """Kontekst Sponsora ze zmiennych środowiskowych (ustawiany przez workera per krok)."""
    return {
        "task_id": os.environ.get("SPONSOR_TASK_ID") or None,
        "agent_role": os.environ.get("SPONSOR_AGENT_ROLE") or None,
        "substep": os.environ.get("SPONSOR_STEP") or None,
    }


def capture_enabled() -> bool:
    """Czy przechwytywać komendy przez sponsor-shell (integracja llm/git/qg)."""
    return os.environ.get("SPONSOR_SHELL_CAPTURE", "") in ("1", "true", "yes", "on")


def maybe_run(command: list[str], *, task_id: str | None = None, agent_role: str | None = None,
              substep: str | None = None, cwd: Path | str | None = None,
              timeout_s: float = DEFAULT_TIMEOUT_S) -> subprocess.CompletedProcess | None:
    """Jeśli capture włączone (lub jest kontekst sponsora) — run przez sponsor_shell i
    zwróć wynik jako CompletedProcess (kompatybilność z wywołującymi subprocess.run).
    W przeciwnym razie None = wywołujący robi zwykłe subprocess.run (stare zachowanie).
    """
    ctx = context_from_env()
    task_id = task_id or ctx["task_id"]
    agent_role = agent_role or ctx["agent_role"]
    substep = substep or ctx["substep"]
    if not (capture_enabled() or task_id):
        return None
    res = run_command(command, task_id=task_id, agent_role=agent_role, substep=substep,
                      cwd=cwd, timeout_s=timeout_s)
    return subprocess.CompletedProcess(
        command, res["exit_code"] if res["exit_code"] is not None else -1,
        res["stdout_tail"], res["stderr_tail"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sponsor-shell",
        description="Deterministyczny gateway wykonywania komend (izolowana grupa procesów + pełny log).")
    ap.add_argument("--task", dest="task_id", default=os.environ.get("SPONSOR_TASK_ID"))
    ap.add_argument("--agent", dest="agent_role", default=os.environ.get("SPONSOR_AGENT_ROLE"))
    ap.add_argument("--step", dest="substep", default=os.environ.get("SPONSOR_STEP"))
    ap.add_argument("--timeout", dest="timeout_s", type=float, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--no-register", action="store_true", help="nie zapisuj do state.db (tylko log + stdout)")
    ap.add_argument("--kill", dest="kill_run_id", default=None, help="zabij istniejący run i zakończ")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="komenda po '--', np. -- echo hello")
    args = ap.parse_args(argv)

    if args.kill_run_id:
        print(json.dumps(kill_run(args.kill_run_id), ensure_ascii=False))
        return 0

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        ap.error("brak komendy — użyj: -- <komenda...>")

    def _live(stream: str, line: str) -> None:
        sys.stdout.write(line + "\n") if stream == "stdout" else sys.stderr.write(line + "\n")
        sys.stdout.flush() if stream == "stdout" else sys.stderr.flush()

    result = run_command(cmd, task_id=args.task_id, agent_role=args.agent_role,
                         substep=args.substep, cwd=args.cwd, timeout_s=args.timeout_s,
                         on_line=_live, register=not args.no_register)
    print(json.dumps(result, ensure_ascii=False))
    return result["exit_code"] if result["exit_code"] is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
