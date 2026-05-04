from __future__ import annotations

import os
import sys
from pathlib import Path

agent_dir = os.path.join(os.path.dirname(__file__), "..")
if agent_dir not in sys.path:
    sys.path.insert(0, agent_dir)


def test_reconcile_cluster_context_derives_files_from_payload():
    from executor_worker import _derive_target_files, _derive_test_files

    metadata = {
        "operation_type": "reconcile-cluster",
        "cluster_payload": {
            "primary_files": ["agent/a.py", "agent/b.py", "agent/a.py"],
        },
        "cluster_report": {
            "expected_test_files": ["agent/tests/test_a.py"],
        },
    }

    assert _derive_target_files(metadata) == ["agent/a.py", "agent/b.py"]
    assert _derive_test_files(metadata) == ["agent/tests/test_a.py"]


def test_reconcile_cluster_pm_prompt_embeds_cluster_metadata(tmp_path, monkeypatch):
    import requests
    from executor_worker import ExecutorWorker, _derive_target_files, _derive_test_files

    source = tmp_path / "agent" / "governance" / "server.py"
    source.parent.mkdir(parents=True)
    source.write_text("def handle_health():\n    return {'ok': True}\n", encoding="utf-8")

    metadata = {
        "operation_type": "reconcile-cluster",
        "cluster_fingerprint": "fp-prompt1",
        "reconcile_run_id": "run-prompt1",
        "cluster_payload": {
            "primary_files": ["agent/governance/server.py"],
            "candidate_nodes": [
                {
                    "node_id": None,
                    "title": "Governance server",
                    "primary": "agent/governance/server.py",
                }
            ],
        },
        "cluster_report": {
            "purpose": "Audit governance server surface",
            "expected_test_files": ["agent/tests/test_server.py"],
            "expected_doc_sections": ["docs/governance/server.md"],
        },
    }

    class _Resp:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: _Resp({"exists": False, "tasks": []}),
    )
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: _Resp({"affected_nodes": []}),
    )

    worker = ExecutorWorker.__new__(ExecutorWorker)
    worker.project_id = "aming-claw"
    worker.base_url = "http://localhost:40000"
    worker.workspace = str(tmp_path)
    worker._fetch_memories = lambda query: []

    prompt = worker._build_prompt(
        "Reconcile cluster fp-prompt1 - produce PRD",
        "pm",
        {
            "task_id": "task-prompt1",
            "metadata": metadata,
            "operation_type": "reconcile-cluster",
            "cluster_payload": metadata["cluster_payload"],
            "cluster_report": metadata["cluster_report"],
            "target_files": _derive_target_files(metadata),
            "test_files": _derive_test_files(metadata),
        },
    )

    assert "## Reconcile Cluster Source Of Truth" in prompt
    assert '"cluster_fingerprint": "fp-prompt1"' in prompt
    assert '"purpose": "Audit governance server surface"' in prompt
    assert "Do not widen scope beyond these files" in prompt
    assert "### agent/governance/server.py" in prompt
