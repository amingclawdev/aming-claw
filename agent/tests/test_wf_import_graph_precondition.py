from __future__ import annotations

from agent.governance import server
from agent.governance.errors import ValidationError


class _Ctx:
    query = {}

    def __init__(self, project_id: str = "no-wf-graph", query: dict | None = None):
        self._project_id = project_id
        self.query = query or {}

    def get_project_id(self) -> str:
        return self._project_id


def _missing_graph(project_id: str):
    raise ValidationError(f"No graph found for project {project_id!r}. Run import-graph first.")


def test_wf_summary_reports_import_graph_precondition(monkeypatch):
    monkeypatch.setattr(server.project_service, "load_project_graph", _missing_graph)

    result = server.handle_summary(_Ctx())

    assert result["ok"] is False
    assert result["error"] == "workflow_graph_missing"
    assert result["needs_import_graph"] is True
    assert result["total_nodes"] == 0
    assert result["next_action"] == "POST /api/wf/no-wf-graph/import-graph"


def test_wf_impact_reports_import_graph_precondition(monkeypatch):
    monkeypatch.setattr(server.project_service, "load_project_graph", _missing_graph)

    result = server.handle_impact(_Ctx(query={"files": "agent/governance/server.py"}))

    assert result["ok"] is False
    assert result["error"] == "workflow_graph_missing"
    assert result["needs_import_graph"] is True
    assert result["files"] == ["agent/governance/server.py"]
