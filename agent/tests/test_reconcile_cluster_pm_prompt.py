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
                    "node_id": "L7.1",
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
    assert '"node_id": "L7.1"' in prompt
    assert "copy that node_id exactly" in prompt
    assert "parent_layer as the candidate node's own layer" in prompt
    assert "via parent/parent_id only" in prompt
    assert "do not put hierarchy parents in deps" in prompt
    assert '"purpose": "Audit governance server surface"' in prompt
    assert "Do not widen scope beyond these files" in prompt
    assert "### agent/governance/server.py" in prompt


def test_reconcile_cluster_pm_prompt_keeps_candidate_manifest_untruncated(tmp_path, monkeypatch):
    import requests
    from executor_worker import ExecutorWorker, _derive_target_files, _derive_test_files

    source = tmp_path / "agent" / "governance" / "orchestrator.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run_orchestrated():\n    return True\n", encoding="utf-8")

    noisy_nodes = []
    for idx, node_id in enumerate(["L7.107", "L7.110", "L7.159", "L7.163", "L7.168", "L7.82"]):
        primary = "agent/governance/reconcile_phases/orchestrator.py" if node_id == "L7.82" else f"scripts/file_{idx}.py"
        noisy_nodes.append(
            {
                "node_id": node_id,
                "title": f"node {node_id}",
                "primary": [primary],
                "layer": "L7",
                "metadata": {
                    "hierarchy_parent": "L3.18",
                    "large_evidence": "x" * 3000,
                },
            }
        )
    metadata = {
        "operation_type": "reconcile-cluster",
        "cluster_fingerprint": "fp-truncate",
        "cluster_payload": {
            "primary_files": ["agent/governance/orchestrator.py"],
            "candidate_nodes": noisy_nodes,
        },
        "cluster_report": {},
    }

    class _Resp:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _Resp({"exists": False, "tasks": []}))
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: _Resp({"affected_nodes": []}))

    worker = ExecutorWorker.__new__(ExecutorWorker)
    worker.project_id = "aming-claw"
    worker.base_url = "http://localhost:40000"
    worker.workspace = str(tmp_path)
    worker._fetch_memories = lambda query: []

    prompt = worker._build_prompt(
        "Reconcile cluster fp-truncate - produce PRD",
        "pm",
        {
            "task_id": "task-truncate",
            "metadata": metadata,
            "operation_type": "reconcile-cluster",
            "cluster_payload": metadata["cluster_payload"],
            "cluster_report": metadata["cluster_report"],
            "target_files": _derive_target_files(metadata),
            "test_files": _derive_test_files(metadata),
        },
    )

    manifest_start = prompt.index("## Reconcile Candidate Node Manifest")
    source_start = prompt.index("## Reconcile Cluster Source Of Truth")
    manifest = prompt[manifest_start:source_start]
    assert "candidate_node_count: 6" in manifest
    assert '"node_id": "L7.82"' in manifest
    assert '"primary": [\n      "agent/governance/reconcile_phases/orchestrator.py"' in manifest
    assert '"hierarchy_parent": "L3.18"' in manifest
    assert "...<truncated>" in prompt
