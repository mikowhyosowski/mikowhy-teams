import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dashboard import load_data, render_dashboard  # noqa: E402


def _sample_state(tmp_path: Path) -> Path:
    """Dashboard odczytuje stan z pliku JSON; błąd braku danych wynikał z
    złej ścieżki/klucza, więc test pigułkuje realny kształt danych."""
    state = {
        "generated_at": "2025-01-01T00:00:00+00:00",
        "jobs": [
            {
                "id": "CHAT-0001",
                "title": "Przykładowe zadanie",
                "status": "RUNNING",
                "progress": 42,
            }
        ],
        "history": [
            {"id": "CHAT-0001", "status": "RUNNING", "updated_at": "2025-01-01T00:00:00+00:00"}
        ],
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def test_load_data_returns_nonempty(tmp_path: Path) -> None:
    data = load_data(_sample_state(tmp_path))
    assert data, "load_data nie może zwracać pustego słownika"
    assert data.get("jobs"), "brak klucza jobs z danymi"


def test_render_includes_job_fields(tmp_path: Path) -> None:
    html = render_dashboard(load_data(_sample_state(tmp_path)))
    assert "CHAT-0001" in html, "dashboard nie renderuje identyfikatorów zadań"
    assert "RUNNING" in html, "dashboard nie renderuje statusów zadań"
    assert "42" in html, "dashboard nie renderuje postępu zadania"


def test_render_renders_all_key_sections(tmp_path: Path) -> None:
    html = render_dashboard(load_data(_sample_state(tmp_path)))
    for section in ("jobs", "history"):
        assert section in html.lower(), f"brak sekcji {section} w rendered HTML"


def test_regress_empty_state_renders_header_not_blank(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"jobs": [], "history": []}), encoding="utf-8")
    html = render_dashboard(load_data(empty))
    assert html.strip(), "dashboard nie może renderować pustej strony"
