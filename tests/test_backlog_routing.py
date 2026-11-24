from engine.task_tagger import tag_for
from engine.backlog_rebuilder import regroup
from engine.priority_assigner import assign
from engine.routing_rules import route

def test_tag_and_route():
    assert tag_for("Napraw dashboard") == "dashboard"
    assert route("Analiza wizerunku") == "mikowhy-brand"
    assert assign({"status": "BLOCKED", "created_at": 0}, 10) == "P1"

def test_regroup_skips_merged():
    groups = regroup([
        {"id": "A", "status": "READY", "project_id": "mikowhy-teams"},
        {"id": "B", "status": "MERGED", "project_id": "mikowhy-teams"},
    ])
    assert groups == {"mikowhy-teams": ["A"]}
