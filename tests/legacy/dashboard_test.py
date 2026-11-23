import json

import pytest

from engine.dashboard import create_app


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """Izolowane srodowisko dashboardu: .agents/runtime i .agents/logs w tmp_path."""
    runtime = tmp_path / ".agents" / "runtime"
    logs = tmp_path / ".agents" / "logs"
    runtime.mkdir(parents=True)
    logs.mkdir(parents=True)
    (runtime / "worker-1.response.json").write_text(
        '{"task": "CHAT-0005", "status": "RUNNING"}', encoding="utf-8"
    )
    (logs / "orchestrator.log").write_text(
        "line-1\nline-2\nline-3\nline-4\nline-5\n", encoding="utf-8"
    )
    monkeypatch.setenv("AGENTS_DIR", str(tmp_path / ".agents"))
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def test_health_endpoint_returns_200(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_index_renders_runtime_and_logs_sections(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert ".agents/runtime" in body
    assert ".agents/logs" in body


def test_api_raw_serves_runtime_file_content(client):
    resp = client.get("/api/raw/worker-1.response.json")
    assert resp.status_code == 200
    payload = json.loads(resp.get_data(as_text=True))
    assert payload["task"] == "CHAT-0005"


def test_api_raw_rejects_path_traversal(client):
    resp = client.get("/api/raw/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)


def test_api_raw_returns_404_for_missing_file(client):
    resp = client.get("/api/raw/missing.response.json")
    assert resp.status_code == 404


def test_api_log_tail_returns_last_n_lines(client):
    resp = client.get("/api/log/orchestrator.log?n=2")
    assert resp.status_code == 200
    lines = resp.get_json()["lines"]
    assert lines == ["line-4", "line-5"]


def test_api_log_tail_defaults_without_n(client):
    resp = client.get("/api/log/orchestrator.log")
    assert resp.status_code == 200
    lines = resp.get_json()["lines"]
    assert lines[-1] == "line-5"
    assert len(lines) <= 200


def test_api_log_rejects_path_traversal(client):
    resp = client.get("/api/log/../../etc/passwd")
    assert resp.status_code in (400, 404)


def test_dashboard_lists_file_clickable_content(client):
    """Klik na plik w .agents/runtime pokazuje jego tresc (naprawa CHAT-0005)."""
    index = client.get("/").get_data(as_text=True)
    assert "worker-1.response.json" in index
    assert "/api/raw/worker-1.response.json" in index
