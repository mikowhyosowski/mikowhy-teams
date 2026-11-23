"""Regression test for the news-video-generator dashboard (CHAT-0004).

Root cause pinned by this test: empty collections were serialized as null,
so the dashboard rendered blank. Contract: empty data is [] (never null),
dates are ISO-8601 with timezone (UTC), legacy visual_history stays readable.
"""
from datetime import datetime, timezone
from typing import Any

from app.dashboard import build_dashboard_payload, render_dashboard

FIXED_NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)


def _legacy_visual_history_entry() -> dict[str, Any]:
    return {
        "id": "vh-legacy-001",
        "video_id": "vid-001",
        "created_at": "2026-11-20T08:30:00+00:00",
        "frames": [{"index": 0, "url": "https://cdn.example.com/f0.png"}],
    }


class _EmptyStore:
    def list_videos(self) -> list[dict[str, Any]]:
        return []

    def list_visual_history(self) -> list[dict[str, Any]]:
        return [_legacy_visual_history_entry()]


def _payload() -> dict[str, Any]:
    return build_dashboard_payload(_EmptyStore(), now=FIXED_NOW)


def test_empty_collections_serialize_as_list_not_null() -> None:
    data = _payload()
    assert data["recent_videos"] == []
    assert isinstance(data["visual_history"], list)


def test_payload_contains_all_key_sections() -> None:
    assert {"stats", "recent_videos", "visual_history"} <= set(_payload())


def test_dates_are_iso8601_with_timezone() -> None:
    data = _payload()
    assert data["generated_at"] == "2026-11-22T12:00:00+00:00"
    for entry in data["visual_history"]:
        assert datetime.fromisoformat(entry["created_at"]).tzinfo is not None


def test_legacy_visual_history_remains_compatible() -> None:
    entry = next(e for e in _payload()["visual_history"] if e["id"] == "vh-legacy-001")
    assert entry["frames"][0]["url"] == "https://cdn.example.com/f0.png"


def test_render_dashboard_emits_all_sections_even_when_empty() -> None:
    html = render_dashboard(_payload())
    for marker in ("stats", "recent_videos", "visual_history", "vh-legacy-001"):
        assert marker in html
