import os
import sys

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance import auto_chain


def _candidate_metadata():
    return {
        "operation_type": "reconcile-cluster",
        "cluster_payload": {
            "candidate_nodes": [
                {
                    "node_id": "L7.72",
                    "title": "agent.governance.reconcile_batch_memory",
                    "layer": "L7",
                    "parent": "L3.22",
                    "primary": ["agent/governance/reconcile_batch_memory.py"],
                }
            ]
        },
    }


def test_reconcile_cluster_pm_preflight_rejects_null_candidate_id():
    metadata = _candidate_metadata()
    prd = {
        "proposed_nodes": [
            {
                "node_id": None,
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": "L7",
                "primary": ["agent/governance/reconcile_batch_memory.py"],
            }
        ]
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert not passed
    assert "reconcile-cluster" in reason
    assert "candidate node_id" in reason


def test_reconcile_cluster_pm_preflight_accepts_exact_candidate_contract():
    metadata = _candidate_metadata()
    prd = {
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": 7,
                "deps": ["L3.22"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
            }
        ]
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert passed, reason


def test_reconcile_cluster_pm_preflight_rejects_missing_candidate_parent():
    metadata = _candidate_metadata()
    prd = {
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": 7,
                "primary": ["agent/governance/reconcile_batch_memory.py"],
            }
        ]
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert not passed
    assert "parent relation" in reason
    assert "L3.22" in reason


def test_reconcile_cluster_pm_preflight_rejects_wrong_node_layer():
    metadata = _candidate_metadata()
    prd = {
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": "L6",
                "deps": ["L3.22"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
            }
        ]
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert not passed
    assert "node layer" in reason


def test_reconcile_cluster_dev_prompt_uses_overlay_contract_not_generic_test_churn():
    metadata = {
        **_candidate_metadata(),
        "target_files": ["agent/governance/reconcile_batch_memory.py"],
        "test_files": ["agent/tests/test_reconcile_batch_memory.py"],
        "doc_impact": {"files": ["docs/dev/proposal-reconcile-cluster-driven-standard-chain.md"]},
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": 7,
                "deps": ["L3.22"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
            }
        ],
    }

    prompt = auto_chain._render_dev_contract_prompt("pm-task", metadata)

    assert "Test files to create/modify" not in prompt
    assert "Cluster test evidence files" in prompt
    assert "graph_delta.creates" in prompt
    assert "graph.rebase.overlay.json" in prompt
    assert "Do not mutate graph.json" in prompt
    assert "parent_layer as the node layer" in prompt


def test_reconcile_cluster_build_dev_prompt_does_not_pull_old_graph_docs(monkeypatch):
    def fail_graph_doc_lookup(*_args, **_kwargs):
        raise AssertionError("reconcile-cluster should not query old graph doc associations")

    monkeypatch.setattr(auto_chain, "_get_graph_doc_associations", fail_graph_doc_lookup)
    result = {
        "target_files": ["agent/governance/reconcile_batch_memory.py"],
        "requirements": ["R1"],
        "acceptance_criteria": ["AC1"],
        "verification": {"command": "python -m pytest agent/tests/test_reconcile_batch_memory.py"},
        "test_files": ["agent/tests/test_reconcile_batch_memory.py"],
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": 7,
                "deps": ["L3.22"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
            }
        ],
    }

    prompt, out_meta = auto_chain._build_dev_prompt("pm-task", result, _candidate_metadata())

    assert "graph_delta.creates" in prompt
    assert out_meta["doc_impact"] == {}


def test_reconcile_cluster_build_qa_prompt_does_not_pull_old_graph_docs(monkeypatch):
    def fail_graph_doc_lookup(*_args, **_kwargs):
        raise AssertionError("reconcile-cluster QA should not query old graph doc associations")

    monkeypatch.setattr(auto_chain, "_get_graph_doc_associations", fail_graph_doc_lookup)
    monkeypatch.setattr(auto_chain, "_query_graph_delta_proposed", lambda *_args, **_kwargs: None)
    result = {
        "test_report": {"passed": 1, "failed": 0, "tool": "pytest"},
        "changed_files": [],
    }
    metadata = {
        **_candidate_metadata(),
        "target_files": ["agent/governance/reconcile_batch_memory.py"],
        "acceptance_criteria": ["AC1: preserve candidate node"],
        "verification": {"command": "python -m pytest agent/tests/test_reconcile_batch_memory.py"},
    }

    prompt, out_meta = auto_chain._build_qa_prompt("test-task", result, metadata)

    assert "criteria_results" in prompt
    assert "Graph Consistency Check" not in prompt
    assert out_meta["target_files"] == ["agent/governance/reconcile_batch_memory.py"]


def test_reconcile_cluster_pm_prompt_uses_batch_memory_but_skips_old_graph_impact(monkeypatch, tmp_path):
    from executor_worker import ExecutorWorker

    target = tmp_path / "agent" / "governance" / "reconcile_batch_memory.py"
    target.parent.mkdir(parents=True)
    target.write_text("def create_or_get_batch():\n    return {}\n", encoding="utf-8")

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, *args, **kwargs):
        if "/batch-memory/" in url:
            return _Resp({
                "batch": {
                    "batch_id": "batch-qg",
                    "session_id": "run-qg",
                    "memory": {
                        "processed_clusters": {"old": {}},
                        "accepted_features": {},
                        "open_conflicts": [],
                    },
                }
            })
        if "/api/context/" in url:
            return _Resp({"exists": False})
        if "/api/task/" in url:
            return _Resp({"tasks": []})
        return _Resp({})

    def fake_post(url, *args, **kwargs):
        if "/api/impact/" in url:
            raise AssertionError("reconcile-cluster PM prompt must not query old graph impact")
        return _Resp({})

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(ExecutorWorker, "_fetch_memories", lambda self, query: [])

    worker = ExecutorWorker("aming-claw", governance_url="http://gov", workspace=str(tmp_path))
    metadata = {
        **_candidate_metadata(),
        "batch_id": "batch-qg",
        "cluster_fingerprint": "fp-qg",
        "reconcile_run_id": "run-qg",
        "target_files": ["agent/governance/reconcile_batch_memory.py"],
    }
    prompt = worker._build_prompt(
        "Reconcile cluster fp-qg — produce PRD",
        "pm",
        {
            "task_id": "pm-qg",
            "metadata": metadata,
            "operation_type": "reconcile-cluster",
            "cluster_payload": metadata["cluster_payload"],
            "cluster_report": metadata.get("cluster_report", {}),
            "target_files": metadata["target_files"],
            "test_files": ["agent/tests/test_reconcile_batch_memory.py"],
        },
    )

    assert "Reconcile Batch Memory" in prompt
    assert "Reconcile Cluster Source Of Truth" in prompt
    assert "Graph Impact Analysis" not in prompt
