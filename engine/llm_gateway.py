"""Jedyna bramka silnika do LLM. Sam silnik pozostaje deterministyczny (0 tokenów) —
wszystkie wywołania modeli dzieją się wyłącznie przez ten moduł, w mikro-etapach
workera/CR, nigdy w orchestratorze.

Provider wybierany zmienną środowiskową LLM_PROVIDER:
    vibe  — lokalny CLI Mistral Vibe w trybie headless (domyślny, gdy binary `vibe` jest w PATH)
    stub  — deterministyczny błąd "provider not configured"

Modele (zmienne środowiskowe, opcjonalnie):
    VIBE_FAST_MODEL   — szybki model do testów/boilerplate'u (default: zai-glm-5-3-flash, fast SWE)
    VIBE_STRONG_MODEL — mocny model do architektury/CR/debuggera (default: mistral-large-4-0)
Role z registry.json używają nazw semantycznych "fast-model"/"strong-model" —
gateway mapuje je na aliasy CLI.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".agents" / "runtime"
LLM_LOCK = ROOT / ".agents" / "llm.lock"


@__import__("contextlib").contextmanager
def _llm_process_lock(timeout_s: float = 900.0):
    """Cross-process lock na wywołania `vibe` CLI.

    Keyring/keys nie jest process-safe — współbieżne procesy vibe padają rc=3
    ("Reading API keys from the system keyring"). Lock serializuje WSZYSTKIE
    wywołania CLI (także agent-mode) na tej maszynie.
    """
    import contextlib, fcntl
    LLM_LOCK.parent.mkdir(parents=True, exist_ok=True)
    f = open(LLM_LOCK, "w")
    deadline = time.time() + timeout_s
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() > deadline:
                f.close()
                raise TimeoutError("llm lock timeout — za dużo równoległych wywołań LLM (kolejka powinna to serializować)")
            time.sleep(0.5)
    try:
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """Ładuje .env do os.environ — klucze API dla vibe.

    Opcja A′ (WORKER_OBSERVABILITY_PLAN.md §1.3):
    Klucze API (*_API_KEY, *_KEY) są zapisywane BEZWARUNKOWO do os.environ —
    vibe preferuje zmienne środowiskowe nad systemowym keyringiem (gnome-keyring/libsecret),
    co eliminuje błąd rc=3 "Reading API keys from the system keyring" przy równoległych
    wywołaniach. Pozostałe zmienne używają setdefault (nie nadpisują wartości z shella).
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip()  # obetnij komentarz inline
        key = key.strip()
        if key.startswith(("VIBE_", "TELEGRAM_", "HEALTH_")):
            os.environ[key] = value  # config z .env repo wygrywa (leczy trujące env z shella)
        elif key.endswith(("_API_KEY", "_KEY", "_SECRET", "_TOKEN")):
            # Opcja A′: klucze auth zawsze z .env → vibe nie pyta keyringa → koniec rc=3
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def _resolve_binary(binary: str) -> str:
    """Znajdź binary vibe: PATH, potem ~/.local/bin, /usr/local/bin (non-interactive shell może nie mieć ~/.local/bin w PATH)."""
    if shutil.which(binary):
        return binary
    for cand in (Path.home() / ".local" / "bin" / binary, Path("/usr/local/bin") / binary):
        if cand.exists():
            return str(cand)
    return binary  # niech subprocess rzuci czytelny błąd, jeśli naprawdę brak


def _autoclose_json(cand: str) -> str | None:
    """v2.7.29: naprawa UCIĘTEJ odpowiedzi (time limit / max-tokens): skan stanu
    napisów i nawiasów od pierwszego '{' i domknięcie napisu + nawiasów
    w odwrotnej kolejności. Zwraca zamkniętego kandydata albo None, gdy w tekście
    w ogóle nie ma '{'."""
    stack: list[str] = []
    in_str = False
    esc = False
    start = cand.find("{")
    if start == -1:
        return None
    end = len(cand)
    for i in range(start, len(cand)):
        c = cand[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[":
            stack.append("}" if c == "{" else "]")
        elif c in "}]" and stack:
            stack.pop()
            if not stack:
                end = i + 1  # kompletny obiekt — jednoznacznie zamknięty
                break
    body = cand[start:end]
    # obetnij zawieszoną przecinek/przed napisem (trailing comma przed domknięciem)
    body = re.sub(r",\s*$", "", body)
    closers = ''
    if in_str:
        # zawieszony napis (ucięty time-limit) — domknij z escapowanym końcem
        body = body.rstrip("\\")
        closers += '"'
    closers += "".join(reversed(stack))
    return body + closers

def extract_json(text: str) -> dict[str, Any]:
    """Wyciąga JSON z wyjścia modelu odpornie na:
    - bloki markdown ```json ... ``` (również bez znacznika języka),
    - prozę/stopki diagnostyczne przed i za JSON-em (np. 'Reading API keys...', użycie tokenów),
    - klamry wewnątrz stringów.
    Strategia: najpierw fenced block, potem skan z balansem nawiasów od pierwszego '{',
    fallback: pierwszy '{' do ostatniego '}'."""
    # sanityzacja klasycznych błędów LLM: \' nie jest legalnym escapem w JSON
    text = text.replace("\\'", "'")
    candidates: list[str] = []
    # zamknięte bloki fence
    for m in re.finditer(r"```(?:json|JSON)?\s*(.*?)```", text, re.DOTALL):
        candidates.append(m.group(1))
    # niezamknięty fence (ucięte przez time-limit) — bierz wszystko po otwierającym ```
    m_open = re.search(r"```(?:json|JSON)?\s*", text)
    if m_open and not re.search(r"```(?:json|JSON)?\s*.*?```", text, re.DOTALL):
        candidates.append(text[m_open.end():])
    candidates.append(text)
    for cand in candidates:
        start = cand.find("{")
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(cand)):
            c = cand[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cand[start : i + 1])
                    except json.JSONDecodeError:
                        break
        end = cand.rfind("}")
        if end > start:
            try:
                return json.loads(cand[start : end + 1])
            except json.JSONDecodeError:
                continue
    # v2.7.29 FALLBACK: odpowiedź ucięta (time limit) lub z napisem bez domknięcia —
    # autodomykannie napis/nawiasy z przeplaymi po liczby prób. Ratuje pełne pliki z 2/5_IMPL
    for cand in candidates:
        fixed = _autoclose_json(cand)
        if not fixed:
            continue
        for attempt in (fixed, fixed.replace(",}", "}").replace(",]", "]")):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Nie udało się wyciągnąć JSON z odpowiedzi ({len(text)} znaków): {text[:300]!r}")

MODEL_ALIASES = {
    "fast-model": os.environ.get("VIBE_FAST_MODEL", "zai-glm-5-3-flash"),
    "strong-model": os.environ.get("VIBE_STRONG_MODEL", "mistral-large-4-0"),
}


class LLMGateway:
    def complete(self, *, system: str, user: str, step: str, model: str) -> str:
        raise NotImplementedError

    def complete_json(self, *, system: str, user: str, step: str, model: str,
                      priority: str = "P2", timeout_s: int | None = None, **kw) -> dict[str, Any]:
        raw = self.complete(system=system, user=user, step=step, model=model,
                            priority=priority, timeout_s=timeout_s)
        try:
            return extract_json(raw)
        except ValueError:
            # retry: kompaktowy JSON bez fence'ów i komentarzy
            raw = self.complete(
                system=system,
                user=user + "\n\nYour previous answer was not parseable as JSON. "
                            "Return ONLY one compact JSON object — no markdown fences, no comments, no trailing text.",
                step=step, model=model, priority=priority, timeout_s=timeout_s)
            try:
                return extract_json(raw)
            except ValueError as exc:
                raise ValueError(f"LLM nie zwrócił czystego JSON w kroku {step} (po retry): {exc}") from exc


class StubGateway(LLMGateway):
    def complete(self, *, system: str, user: str, step: str, model: str, **_kw) -> str:
        raise RuntimeError(
            f"LLM provider nie skonfigurowany (LLM_PROVIDER={os.environ.get('LLM_PROVIDER', 'stub')!r}). "
            f"Podłącz provider w engine/llm_gateway.py, aby uruchomić mikro-etap {step}."
        )


class VibeCliGateway(LLMGateway):
    """Provider `vibe`: lokalny CLI Mistral Vibe w trybie headless.

    Wywołanie: vibe --trust --max-turns 1 --model <alias> --output text
               --enabled-tools __none__ --system-prompt-file <sys> --prompt-file <user>
    `--enabled-tools __none__` wyłącza wszystkie narzędzia — bot może tylko odpowiedzieć tekstem
    (żadnych komend, żadnej edycji plików; tym zajmuje się deterministyczny silnik).
    """

    def __init__(self, binary: str = "vibe", timeout: int = 300):
        _load_dotenv()  # klucze API (MISTRAL_API_KEY itp.) zanim odpalimy subprocess
        self.binary = _resolve_binary(binary)
        self.timeout = timeout

    # model może mimo wszystko spróbować wywołać tool (odmowa = kolejny turn) — stąd max-turns 3
    NO_TOOLS_NOTE = (
        "\n\n## Tryb headless — BRAK narzędzi\n"
        "NIE wywołuj żadnych tooli (bash, edycja plików, itp.). Całą resztą zajmuje się "
        "deterministyczny silnik. Odpowiadasz WYŁĄCZNIE tekstem (najlepiej JSON) — to Twój jedyny output."
    )

    def _run_once(self, system: str, user: str, alias: str, step: str) -> subprocess.CompletedProcess:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        safe_step = "".join(c if c.isalnum() or c in "-_" else "_" for c in step)
        # VIBE_ENABLED_TOOLS: domyślnie "__none__" (bot bez narzędzi — determinizm silnika).
        # Żeby bot mógł sam uruchamiać Pythona: VIBE_ENABLED_TOOLS=file_system.bash,file_system.read,file_system.write
        enabled_tools = os.environ.get("VIBE_ENABLED_TOOLS", "__none__")
        approval = os.environ.get("VIBE_APPROVAL_POLICY", "")  # np. smart-approve / auto-approve gdy narzędzia włączone
        with tempfile.TemporaryDirectory(dir=RUNTIME, prefix=f"gw-{safe_step}-") as tmp:
            sys_file = Path(tmp) / "system.md"
            user_file = Path(tmp) / "user.md"
            note = self.NO_TOOLS_NOTE if enabled_tools == "__none__" else (
                "\n\n## Narzędzia\nMasz dostęp do włączonych narzędzi (VIBE_ENABLED_TOOLS). "
                "Możesz uruchamiać kod, ALE ostateczny output musi być czystym JSON-em.")
            sys_file.write_text(system + note, encoding="utf-8")
            user_file.write_text(user, encoding="utf-8")
            max_turns = "1" if enabled_tools == "__none__" else "3"  # bez tooli: jedna strzałka JSON
            cmd = [
                self.binary, "--trust", "--max-turns", max_turns,
                "--model", alias, "--output", "text",
                "--enabled-tools", enabled_tools,
                "--system-prompt-file", str(sys_file),
                "--prompt-file", str(user_file),
                "--time-limit", str(self.timeout),
            ]
            if approval:
                cmd += ["--approval-policy", approval]
            # jawne env: pełne os.environ (po _load_dotenv) — klucze API, PATH z ~/.local/bin
            # VIBE_ENABLED_TOOLS usuwamy z env subprocessu (vibe ostrzega o nieparsowalnym formacie);
            # narzędzia włączamy wyłącznie flagą --enabled-tools powyżej
            clean_env = {k: v for k, v in os.environ.items() if not k.startswith("VIBE_ENABLED_TOOLS")}
            with _llm_process_lock():
                return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                      timeout=self.timeout + 60, env=clean_env)

    def complete(self, *, system: str, user: str, step: str, model: str, _direct: bool = False,
                 priority: str = "P2", timeout_s: int | None = None, **_kw) -> str:
        # KOLEJKA LLM: krótkie wywołania (complete) idą przez llm_jobs — wykonuje je seryjnie
        # dispatcher w OSOBNYM WĄTKU orchestratora (rate limit + retry/backoff + lease + priorytety).
        # Dzięki temu keyring/vibe CLI dotyka tylko jeden proces (koniec rc=3 keyring).
        # _direct=True (używa tego dispatch_once) = wykonaj OD RAZU, bez kolejki — inaczej deadlock.
        s = None
        queue_on = False
        try:
            from engine.state import State
            from engine import llm_queue
            s = State()
            queue_on = llm_queue.enabled(s)
        except Exception:
            queue_on = False
        if queue_on and s is not None and not _direct:
            from engine import llm_queue
            # v2: per-model timeout joba (fast szybciej failuje -> backoff) + priorytet zadania w kolejce
            job_timeout = int(timeout_s or llm_queue.model_timeout_s(model))
            jid = llm_queue.enqueue(s, "complete",
                                    {"system": system, "user": user, "step": step, "model": model,
                                     "timeout_s": job_timeout},
                                    requester="gateway", step=step, priority=priority)
            # czekamy aż job przejdzie próby w kolejce; v4: krótszy cap (max 2 próby + 120s zapasu) —
            # przy timeout worker od razu dostaje wyjątek i robi własny auto-retry zadania (nie wisi 19 min)
            # v2.7.25: floor 900s — job czeka w kolejce (serialny dispatcher, backlog 25+ jobów),
            # wait cap pod backlog NIE MOŻE być krótszy niż drain kolejki; lease-reclaim i tak
            # odzyskuje zawieszone joby, więc 1h hard-cap jest bezpieczny dla worker thread
            return llm_queue.wait_result(s, jid, timeout=3600)
        alias = MODEL_ALIASES.get(model, model)  # "fast-model"/"strong-model" -> alias CLI
        enabled = os.environ.get("VIBE_ENABLED_TOOLS", "__none__")
        attempts = 3 if enabled == "__none__" else 2  # bez tooli: model czasem mimo zakazu próbuje tooli — retry z mocniejszym przypomnieniem
        proc = self._run_once(system, user, alias, step)
        for i in range(1, attempts):
            if proc.returncode == 0:
                break
            reminder = ("\n\nREMINDER: You have NO tools. Do NOT emit any tool calls. "
                        "Reply with plain text JSON only, nothing else.")
            if i == attempts - 1 and enabled == "__none__":
                system = system[:4000]  # ostatnia próba: krótszy prompt = mniej okazji do halucynacji tooli
            proc = self._run_once(system, user + reminder * i, alias, step)
        if proc.returncode != 0:
            raise RuntimeError(
                f"vibe CLI rc={proc.returncode} (model={alias}, step={step}): {proc.stderr.strip()[-800:]}"
            )
        raw = proc.stdout.strip()
        # ślad do debugowania: surowa odpowiedź modelu (gitignored przez .agents/runtime/)
        try:
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in step)
            (RUNTIME / f"raw-{safe}-{int(time.time())}.txt").write_text(raw, encoding="utf-8")
        except OSError:
            pass
        return raw

    def agent(self, *, system: str, user: str, cwd: Path, step: str, model: str,
              max_turns: int = 40, time_limit: int = 1800,
              enabled_tools: str = "file_system.bash,file_system.read,file_system.write,"
                                   "file_system.edit,file_system.list,file_system.grep,process") -> subprocess.CompletedProcess:
        """Tryb agentowy: vibe Z narzędziami, w podanym katalogu (worktree zadania), wiele turnów.

        LLM sam wykonuje polecenia (bash, edycja plików). Używane przez zadania mode='llm'
        (komenda `llm <opis>` z Telegrama). Zwraca CompletedProcess — rc!=0 nie musi oznaczać
        porażki (model mógł skończyć pracę mimo niezerowego rc), decyzję podejmuje worker.
        """
        alias = MODEL_ALIASES.get(model, model)
        RUNTIME.mkdir(parents=True, exist_ok=True)
        safe_step = "".join(c if c.isalnum() or c in "-_" else "_" for c in step)
        with tempfile.TemporaryDirectory(dir=RUNTIME, prefix=f"gw-{safe_step}-") as tmp:
            sys_file = Path(tmp) / "system.md"
            user_file = Path(tmp) / "user.md"
            sys_file.write_text(system, encoding="utf-8")
            user_file.write_text(user, encoding="utf-8")
            cmd = [self.binary, "--trust", "--auto-approve", "--max-turns", str(max_turns),
                   "--model", alias, "--output", "text",
                   "--approval-policy", "auto-approve",
                   "--system-prompt-file", str(sys_file),
                   "--prompt-file", str(user_file),
                   "--time-limit", str(time_limit)]
            # UWAGA: --enabled-tools trzeba POWTARZAć per tool (jeden przecinkowy string
            # = jedna nieznana nazwa -> headless wyłącza wszystkie narzędzia)
            for t in enabled_tools.split(","):
                t = t.strip()
                if t:
                    cmd += ["--enabled-tools", t]
            clean_env = {k: v for k, v in os.environ.items() if not k.startswith("VIBE_ENABLED_TOOLS")}
            with _llm_process_lock(timeout_s=time_limit + 180):
                proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                                      timeout=time_limit + 120, env=clean_env)
        try:
            (RUNTIME / f"raw-{safe_step}-{int(time.time())}.txt").write_text(
                f"rc={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
                encoding="utf-8")
        except OSError:
            pass
        return proc


_REGISTRY: dict[str, type[LLMGateway]] = {"stub": StubGateway, "vibe": VibeCliGateway}


def register_gateway(name: str, cls: type[LLMGateway]) -> None:
    _REGISTRY[name] = cls


def get_gateway() -> LLMGateway:
    name = os.environ.get("LLM_PROVIDER")
    if not name:
        name = "vibe" if shutil.which("vibe") else "stub"  # vibe CLI domyślnie, gdy dostępny
    cls = _REGISTRY.get(name)
    if cls is None:
        raise RuntimeError(f"Nieznany LLM_PROVIDER={name!r}; dostępne: {sorted(_REGISTRY)}")
    return cls()


def make_sponsor_title(technical_title: str, context: str = "") -> str:
    """Tytuł zadania WYŚWIETLANY Sponsorowi — krótki (max ~70 znaków), prostymi słowami,
    bez żargonu technicznego. LLM (fast); fallback = techniczny tytuł."""
    try:
        gw = get_gateway()
        out = gw.complete_json(
            system=("Przepisz techniczny tytuł zadania na tytuł dla Sponsora (osoby nietechnicznej): "
                    "krótko (max 70 znaków), proste słowa, bez skrótów i żargonu, po polsku. "
                    "Odpowiedz WYŁĄCZNIE JSON: {\"title\": \"...\"}."),
            user=f"Tytuł techniczny: {technical_title}\nKontekst: {context[:300]}",
            step="SPONSOR_TITLE", model="fast-model")
        t = str(out.get("title", "")).strip()
        if 5 <= len(t) <= 90:
            return t
    except Exception:
        pass
    return technical_title[:70]
