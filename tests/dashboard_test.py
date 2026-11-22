import json

import pytest

from dashboard import load_dashboard_data, render_dashboard


def test_load_dashboard_data_renders_null_sections_as_empty_lists(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "videos": None,
        "queue": None,
        "history": None,
        "metrics": {"runs": 3, "failures": 0},
    }), encoding="utf-8")

    data = load_dashboard_data(state)

    assert data["videos"] == []
    assert data["queue"] == []
    assert data["history"] == []
    assert data["metrics"] == {"runs": 3, "failures": 0}


def test_load_dashboard_data_keeps_populated_sections(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "videos": [{"id": "v1", "title": "News 1"}],
        "queue": [{"id": "q1"}],
        "history": [],
        "metrics": {"runs": 1, "failures": 0},
    }), encoding="utf-8")

    data = load_dashboard_data(state)

    assert data["videos"][0]["id"] == "v1"
    assert data["queue"][0]["id"] == "q1"
    assert data["history"] == []


def test_load_dashboard_data_missing_file_returns_empty_defaults(tmp_path):
    missing = tmp_path / "nope.json"

    data = load_dashboard_data(missing)

    assert data == {"videos": [], "queue": [], "history": [], "metrics": {}}


def test_render_dashboard_contains_key_elements_and_rows():
    data = {
        "videos": [{"id": "v1", "title": "News 1", "status": "done"}],
        "queue": [{"id": "q1", "stage": "tts"}],
        "history": [{"id": "h1", "outcome": "ok"}],
        "metrics": {"runs": 3, "failures": 0},
    }

    html = render_dashboard(data)

    assert isinstance(html, str) and len(html) > 0
    assert "News 1" in html
    assert "done" in html
    assert "tts" in html
    assert "3" in html


def test_render_dashboard_with_empty_data_does_not_leak_none_string():
    html = render_dashboard({"videos": [], "queue": [], "history": [], "metrics": {}})

    assert "None" not in html
    assert "null" not in html