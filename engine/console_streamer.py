"""Console Streamer — przechwytywanie outputu workerów przez PTY do .jsonl.

Każde zdarzenie = jedna linia JSON z polami:
  ts     – Unix timestamp (float, precyzja ms)
  stream – "cmd" | "stdout" | "stderr" | "meta"
  step   – aktualny substep workera (np. "2/5_IMPL")
  cmd    – treść uruchamianej komendy (tylko gdy stream="cmd")
  data   – surowy output (gdy stream="stdout"/"stderr")
  rc     – exit code (gdy stream="meta" + data="exit")

Plik: .agents/logs/console/<TASK_ID>.jsonl
Rotacja: po przekroczeniu MAX_BYTES (50 MB) – truncate + nagłówek.

Użycie:
    streamer = ConsoleStreamer(task_id, state)
    proc = streamer.wrap_popen(["vibe", ...], cwd=worktree, step="2/5_IMPL",
                               cmd_label="vibe --model fast-model ...")
    proc.wait()
    streamer.log_meta("2/5_IMPL", "exit", rc=proc.returncode)
    streamer.close()
"""
from __future__ import annotations

import json
import os
import re
import select
import threading
import time
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.state import State

ROOT = Path(__file__).resolve().parent.parent
CONSOLE_DIR = ROOT / ".agents" / "logs" / "console"

MAX_BYTES = 50 * 1024 * 1024   # 50 MB – po przekroczeniu rotacja
DB_WRITE_INTERVAL = 30.0        # min odstęp zapisów last_activity_at do SQLite


def console_path(task_id: str) -> Path:
    """Ścieżka pliku .jsonl dla danego zadania."""
    CONSOLE_DIR.mkdir(parents=True, exist_ok=True)
    return CONSOLE_DIR / f"{task_id}.jsonl"


def _strip_ansi(text: str) -> str:
    """Usuwa sekwencje ANSI (kolor, kursor) z tekstu PTY."""
    return re.sub(r"\x1b\[[0-9;]*[mABCDEFGHJKLMSTfhilnqrsu]", "", text)


def _event(stream: str, step: str, *,
           cmd: str | None = None,
           data: str | None = None,
           rc: int | None = None) -> str:
    """Serializuje zdarzenie do JSON (jedna linia .jsonl)."""
    return json.dumps({
        "ts": time.time(),
        "stream": stream,
        "step": step,
        "cmd": cmd,
        "data": data,
        "rc": rc,
    }, ensure_ascii=False)


class ConsoleStreamer:
    """Otwiera plik .jsonl i udostępnia metody do logowania zdarzeń.

    Thread-safe: reader_thread pisze do pliku przez self._lock.
    Zapisuje last_activity_at do SQLite max co DB_WRITE_INTERVAL sekund
    (nie co bajt – brak lock contention na WAL).
    """

    def __init__(self, task_id: str, state: "State | None" = None) -> None:
        self.task_id = task_id
        self.state = state
        self._path = console_path(task_id)
        self._lock = threading.Lock()
        self._last_db_write = 0.0
        self._current_step = ""
        # Otwieramy w trybie append – przeżywa restart przy COOLING
        self._fh = self._path.open("a", encoding="utf-8", buffering=1)
        self._active_readers: list[threading.Thread] = []

    # ------------------------------------------------------------------
    # Publiczne API logowania
    # ------------------------------------------------------------------

    def log_cmd(self, step: str, cmd: str) -> None:
        """Loguje uruchomienie komendy."""
        self._current_step = step
        self._write(_event("cmd", step, cmd=cmd))

    def log_data(self, stream: str, step: str, data: str) -> None:
        """Loguje surowy output (stdout lub stderr)."""
        self._write(_event(stream, step, data=_strip_ansi(data)))

    def log_meta(self, step: str, event: str, rc: int | None = None) -> None:
        """Loguje zdarzenia meta (start/exit/substep change)."""
        self._write(_event("meta", step, data=event, rc=rc))

    def set_step(self, step: str) -> None:
        """Aktualizuje bieżący substep (bez zapisu zdarzenia)."""
        self._current_step = step

    # ------------------------------------------------------------------
    # Wrapper dla subprocess.Popen z PTY
    # ------------------------------------------------------------------

    def wrap_popen(
        self,
        args: list[str],
        *,
        cwd: Path | str | None = None,
        env: dict | None = None,
        step: str = "",
        cmd_label: str | None = None,
        capture_stderr: bool = True,
    ) -> "subprocess.Popen[bytes]":
        """Uruchamia proces przez PTY i strumieniuje output do .jsonl.

        Zwraca Popen – wywołujący powinien zrobić proc.wait().
        Reader thread kończy się automatycznie gdy proces zakończy się.
        """
        import pty

        label = cmd_label or " ".join(str(a) for a in args)
        self.log_cmd(step or self._current_step, label)

        master_fd, slave_fd = pty.openpty()

        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd if capture_stderr else None,
        )
        # slave_fd przekazany do procesu – zamykamy nasz egzemplarz
        os.close(slave_fd)

        t = threading.Thread(
            target=self._pty_reader,
            args=(master_fd, step or self._current_step, proc),
            daemon=True,
        )
        t.start()
        self._active_readers.append(t)
        return proc

    def _pty_reader(self, master_fd: int, step: str, proc: "subprocess.Popen") -> None:
        """Czyta bajty z PTY master i zapisuje do .jsonl."""
        buf = b""
        try:
            while True:
                try:
                    r, _, _ = select.select([master_fd], [], [], 0.5)
                except (ValueError, OSError):
                    break
                if r:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    # Flush przy każdym \n lub co 4 kB
                    while b"\n" in buf or len(buf) >= 4096:
                        nl = buf.find(b"\n")
                        if nl == -1:
                            line, buf = buf, b""
                        else:
                            line, buf = buf[: nl + 1], buf[nl + 1 :]
                        try:
                            text = line.decode("utf-8", errors="replace")
                        except Exception:
                            text = repr(line)
                        self.log_data("stdout", step, text)
                else:
                    # Brak danych – sprawdź czy proces skończył
                    if proc.poll() is not None:
                        break
        finally:
            # Flush resztek bufora
            if buf:
                try:
                    self.log_data("stdout", step, buf.decode("utf-8", errors="replace"))
                except Exception:
                    pass
            try:
                os.close(master_fd)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Wewnętrzne
    # ------------------------------------------------------------------

    def _write(self, line: str) -> None:
        """Thread-safe zapis do pliku + opcjonalny zapis DB."""
        with self._lock:
            try:
                # Rotacja po MAX_BYTES
                if self._fh.tell() >= MAX_BYTES:
                    self._fh.seek(0)
                    self._fh.truncate()
                    self._fh.write(json.dumps({
                        "ts": time.time(), "stream": "meta",
                        "step": self._current_step,
                        "data": "--- log rotated (50 MB limit) ---", "cmd": None, "rc": None,
                    }, ensure_ascii=False) + "\n")
                self._fh.write(line + "\n")
                self._fh.flush()
            except OSError:
                pass
        # Rzadki zapis DB (nie co bajt – brak WAL contention)
        now = time.time()
        if self.state is not None and now - self._last_db_write >= DB_WRITE_INTERVAL:
            self._last_db_write = now
            try:
                self.state._conn().execute(
                    "UPDATE tasks SET last_activity_at=? WHERE id=?",
                    (now, self.task_id),
                )
            except Exception:
                pass  # brak kolumny w starszej DB – ignorujemy (mtime wystarczy)

    def close(self) -> None:
        """Czeka na reader threads i zamyka plik."""
        for t in self._active_readers:
            t.join(timeout=5.0)
        with self._lock:
            try:
                self._fh.close()
            except OSError:
                pass

    def __enter__(self) -> "ConsoleStreamer":
        return self

    def __exit__(self, *_) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Funkcje pomocnicze używane przez dashboard i orchestrator
# ---------------------------------------------------------------------------

def last_activity_at(task_id: str) -> float:
    """Zwraca mtime pliku .jsonl lub 0.0 jeśli nie istnieje.

    Jeden syscall stat() – bezpieczne przy wielu klientach dashboard co 2s.
    """
    p = console_path(task_id)
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def is_stalled(task_id: str, threshold_s: float = 600.0) -> bool:
    """Czy task jest STALLED (mtime .jsonl starszy niż threshold_s sekund)."""
    la = last_activity_at(task_id)
    if la == 0.0:
        return False  # plik nie istnieje = task jeszcze nie wystartował
    return (time.time() - la) >= threshold_s


def read_tail(task_id: str, tail: int = 200,
              since_ts: float = 0.0,
              stream_filter: str | None = None) -> list[dict]:
    """Odczytuje ostatnie `tail` wpisów z .jsonl (opcjonalnie filtruje).

    Parametry:
        tail        – max liczba wierszy do zwrócenia
        since_ts    – tylko zdarzenia z ts > since_ts (do polling SSE)
        stream_filter – filtruje po polu stream (None = wszystko)
    """
    p = console_path(task_id)
    if not p.exists():
        return []
    events: list[dict] = []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    # Parsujemy od końca (tail) żeby uniknąć wczytywania 50 MB do RAM
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since_ts and ev.get("ts", 0) <= since_ts:
            continue
        if stream_filter and ev.get("stream") != stream_filter:
            continue
        events.append(ev)
        if len(events) >= tail:
            break
    events.reverse()
    return events


def count_lines(task_id: str) -> int:
    """Liczba wierszy w pliku .jsonl (do info w API)."""
    p = console_path(task_id)
    if not p.exists():
        return 0
    try:
        return sum(1 for _ in p.open(encoding="utf-8", errors="replace"))
    except OSError:
        return 0
