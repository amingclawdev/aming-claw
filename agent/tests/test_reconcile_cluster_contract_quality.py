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
                    "_deps": ["L7.11"],
                    "primary": ["agent/governance/reconcile_batch_memory.py"],
                    "secondary": ["docs/governance/reconcile-workflow.md"],
                    "test": ["agent/tests/test_reconcile_batch_memory.py"],
                    "test_coverage": "direct",
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
                "parent_id": "L3.22",
                "deps": ["L7.11"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
                "secondary": ["docs/governance/reconcile-workflow.md"],
                "test": ["agent/tests/test_reconcile_batch_memory.py"],
                "test_coverage": "direct",
            }
        ],
        "verification": {
            "method": "automated test",
            "command": "pytest agent/tests/test_reconcile_batch_memory.py -v",
        },
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert passed, reason


def test_reconcile_cluster_pm_preflight_rejects_path_only_verification_for_candidate_tests():
    metadata = _candidate_metadata()
    prd = {
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": 7,
                "parent_id": "L3.22",
                "deps": ["L7.11"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
                "secondary": ["docs/governance/reconcile-workflow.md"],
                "test": ["agent/tests/test_reconcile_batch_memory.py"],
                "test_coverage": "direct",
            }
        ],
        "verification": {
            "method": "automated test",
            "command": (
                "python -c \"import pathlib; "
                "assert pathlib.Path('agent/tests/test_reconcile_batch_memory.py').exists()\""
            ),
        },
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert not passed
    assert "verification.command must run pytest" in reason
    assert "test_reconcile_batch_memory.py" in reason


def test_reconcile_cluster_pm_preflight_rejects_pytest_command_missing_candidate_test():
    metadata = _candidate_metadata()
    prd = {
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": 7,
                "parent_id": "L3.22",
                "deps": ["L7.11"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
                "secondary": ["docs/governance/reconcile-workflow.md"],
                "test": ["agent/tests/test_reconcile_batch_memory.py"],
                "test_coverage": "direct",
            }
        ],
        "verification": {
            "method": "automated test",
            "command": "pytest agent/tests/test_other.py -v",
        },
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert not passed
    assert "verification.command must include all candidate Python test consumers" in reason
    assert "test_reconcile_batch_memory.py" in reason


def test_reconcile_cluster_pm_preflight_rejects_dropped_candidate_test_consumer():
    metadata = _candidate_metadata()
    prd = {
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": 7,
                "parent_id": "L3.22",
                "deps": ["L7.11"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
                "secondary": ["docs/governance/reconcile-workflow.md"],
                "test": [],
                "test_coverage": "direct",
            }
        ]
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert not passed
    assert "proposed_nodes test" in reason
    assert "test_reconcile_batch_memory.py" in reason


def test_reconcile_cluster_pm_preflight_rejects_dropped_candidate_doc_consumer():
    metadata = _candidate_metadata()
    prd = {
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": 7,
                "parent_id": "L3.22",
                "deps": ["L7.11"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
                "secondary": [],
                "test": ["agent/tests/test_reconcile_batch_memory.py"],
                "test_coverage": "direct",
            }
        ]
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert not passed
    assert "proposed_nodes secondary" in reason
    assert "reconcile-workflow.md" in reason


def test_reconcile_orphan_doc_test_review_pm_allows_supplied_doc_test_superset():
    metadata = _candidate_metadata()
    metadata["cluster_report"] = {
        "allow_doc_test_augmentation": True,
        "purpose": "Final orphan doc/test review",
        "expected_doc_files": ["docs/governance/reconcile-doc-index.md"],
        "expected_test_files": ["agent/tests/test_reconcile_doc_index.py"],
    }
    prd = {
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": 7,
                "parent_id": "L3.22",
                "deps": ["L7.11"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
                "secondary": [
                    "docs/governance/reconcile-workflow.md",
                    "docs/governance/reconcile-doc-index.md",
                ],
                "test": [
                    "agent/tests/test_reconcile_batch_memory.py",
                    "agent/tests/test_reconcile_doc_index.py",
                ],
                "test_coverage": "direct",
            }
        ],
        "verification": {
            "method": "automated test",
            "command": (
                "pytest agent/tests/test_reconcile_batch_memory.py "
                "agent/tests/test_reconcile_doc_index.py -v"
            ),
        },
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
        metadata=metadata,
    )

    assert passed, reason


def test_reconcile_normal_cluster_pm_rejects_doc_test_superset():
    metadata = _candidate_metadata()
    prd = {
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": 7,
                "parent_id": "L3.22",
                "deps": ["L7.11"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
                "secondary": [
                    "docs/governance/reconcile-workflow.md",
                    "docs/governance/reconcile-doc-index.md",
                ],
                "test": ["agent/tests/test_reconcile_batch_memory.py"],
                "test_coverage": "direct",
            }
        ],
        "verification": {
            "method": "automated test",
            "command": "pytest agent/tests/test_reconcile_batch_memory.py -v",
        },
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
        metadata=metadata,
    )

    assert not passed
    assert "must match candidate exactly" in reason
    assert "reconcile-doc-index.md" in reason


def test_reconcile_cluster_pm_preflight_rejects_candidate_test_coverage_drift():
    metadata = _candidate_metadata()
    prd = {
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": 7,
                "parent_id": "L3.22",
                "deps": ["L7.11"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
                "secondary": ["docs/governance/reconcile-workflow.md"],
                "test": ["agent/tests/test_reconcile_batch_memory.py"],
                "test_coverage": "none",
            }
        ]
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert not passed
    assert "test_coverage" in reason
    assert "direct" in reason


def test_reconcile_cluster_pm_preflight_rejects_parent_in_deps():
    metadata = _candidate_metadata()
    prd = {
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": 7,
                "parent_id": "L3.22",
                "deps": ["L7.11", "L3.22"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
            }
        ]
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert not passed
    assert "deps" in reason
    assert "Do not put hierarchy parent in deps" in reason


def test_reconcile_cluster_pm_preflight_rejects_missing_candidate_parent():
    metadata = _candidate_metadata()
    prd = {
        "proposed_nodes": [
            {
                "node_id": "L7.72",
                "title": "agent.governance.reconcile_batch_memory",
                "parent_layer": 7,
                "deps": ["L7.11"],
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
                "parent_id": "L3.22",
                "deps": ["L7.11"],
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
                "parent_id": "L3.22",
                "deps": ["L7.11"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
            }
        ],
    }

    prompt = auto_chain._render_dev_contract_prompt("pm-task", metadata)

    assert "Test files to create/modify" not in prompt
    assert "Cluster test evidence files" in prompt
    assert "graph_delta.creates" in prompt
    assert "graph.rebase.overlay.json" in prompt
    assert "Do not mutate the active graph artifact" in prompt
    assert "parent_layer as the node layer" in prompt
    assert "do not put hierarchy parents in deps" in prompt


def test_reconcile_cluster_dev_preflight_rejects_candidate_deps_drift():
    metadata = _candidate_metadata()
    pm_nodes = [
        {
            "node_id": "L7.72",
            "title": "agent.governance.reconcile_batch_memory",
            "parent_layer": 7,
            "parent_id": "L3.22",
            "deps": ["L7.11"],
            "primary": ["agent/governance/reconcile_batch_memory.py"],
            "secondary": ["docs/governance/reconcile-workflow.md"],
            "test": ["agent/tests/test_reconcile_batch_memory.py"],
            "test_coverage": "direct",
        }
    ]
    dev_creates = [
        {
            "node_id": "L7.72",
            "title": "agent.governance.reconcile_batch_memory",
            "parent_layer": 7,
            "parent_id": "L3.22",
            "deps": ["L7.11", "L3.22"],
            "primary": ["agent/governance/reconcile_batch_memory.py"],
            "secondary": ["docs/governance/reconcile-workflow.md"],
            "test": ["agent/tests/test_reconcile_batch_memory.py"],
            "test_coverage": "direct",
        }
    ]

    passed, reason = auto_chain.preflight_reconcile_cluster_dev(
        pm_nodes,
        dev_creates,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert not passed
    assert "graph_delta.creates deps" in reason
    assert "L3.22" in reason


def test_reconcile_cluster_dev_preflight_rejects_dropped_candidate_test_consumer():
    metadata = _candidate_metadata()
    pm_nodes = [
        {
            "node_id": "L7.72",
            "title": "agent.governance.reconcile_batch_memory",
            "parent_layer": 7,
            "parent_id": "L3.22",
            "deps": ["L7.11"],
            "primary": ["agent/governance/reconcile_batch_memory.py"],
            "secondary": ["docs/governance/reconcile-workflow.md"],
            "test": ["agent/tests/test_reconcile_batch_memory.py"],
            "test_coverage": "direct",
        }
    ]
    dev_creates = [
        {
            "node_id": "L7.72",
            "title": "agent.governance.reconcile_batch_memory",
            "parent_layer": 7,
            "parent_id": "L3.22",
            "deps": ["L7.11"],
            "primary": ["agent/governance/reconcile_batch_memory.py"],
            "secondary": ["docs/governance/reconcile-workflow.md"],
            "test": [],
            "test_coverage": "direct",
        }
    ]

    passed, reason = auto_chain.preflight_reconcile_cluster_dev(
        pm_nodes,
        dev_creates,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert not passed
    assert "graph_delta.creates test" in reason
    assert "Candidate doc/test consumers" in reason


def test_reconcile_orphan_doc_test_review_dev_allows_supplied_doc_test_superset():
    metadata = _candidate_metadata()
    metadata["cluster_report"] = {
        "allow_doc_test_augmentation": True,
        "purpose": "Final orphan doc/test review",
        "expected_doc_files": ["docs/governance/reconcile-doc-index.md"],
        "expected_test_files": ["agent/tests/test_reconcile_doc_index.py"],
    }
    pm_nodes = [
        {
            "node_id": "L7.72",
            "title": "agent.governance.reconcile_batch_memory",
            "parent_layer": 7,
            "parent_id": "L3.22",
            "deps": ["L7.11"],
            "primary": ["agent/governance/reconcile_batch_memory.py"],
            "secondary": [
                "docs/governance/reconcile-workflow.md",
                "docs/governance/reconcile-doc-index.md",
            ],
            "test": [
                "agent/tests/test_reconcile_batch_memory.py",
                "agent/tests/test_reconcile_doc_index.py",
            ],
            "test_coverage": "direct",
        }
    ]
    dev_creates = [dict(pm_nodes[0])]

    passed, reason = auto_chain.preflight_reconcile_cluster_dev(
        pm_nodes,
        dev_creates,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
        metadata=metadata,
    )

    assert passed, reason


def test_reconcile_cluster_preflight_blocks_phase_z_test_superset_loss():
    """Regression for 82d80141: L7.92 must keep the full candidate test list."""
    candidate_tests = [
        "agent/tests/test_phase_z.py",
        "agent/tests/test_phase_z_ai_cluster_wiring.py",
        "agent/tests/test_phase_z_cluster_groups_smoke.py",
        "agent/tests/test_phase_z_v2_architecture_relations.py",
        "agent/tests/test_phase_z_v2_calibrate_script.py",
        "agent/tests/test_phase_z_v2_feature_clusters.py",
        "agent/tests/test_phase_z_v2_pr1.py",
        "agent/tests/test_phase_z_v2_pr2.py",
        "agent/tests/test_phase_z_v2_pr3.py",
        "agent/tests/test_phase_z_v2_project_profile.py",
        "agent/tests/test_phase_z_v2_traversal.py",
    ]
    metadata = {
        "operation_type": "reconcile-cluster",
        "cluster_payload": {
            "candidate_nodes": [
                {
                    "node_id": "L7.92",
                    "title": "agent.governance.reconcile_phases.phase_z",
                    "layer": "L7",
                    "parent": "L3.34",
                    "_deps": [],
                    "primary": ["agent/governance/reconcile_phases/phase_z.py"],
                    "test": candidate_tests,
                    "test_coverage": "direct",
                }
            ]
        },
    }
    prd = {
        "proposed_nodes": [
            {
                "node_id": "L7.92",
                "title": "agent.governance.reconcile_phases.phase_z",
                "parent_layer": "L7",
                "parent_id": "L3.34",
                "deps": [],
                "primary": ["agent/governance/reconcile_phases/phase_z.py"],
                "test": candidate_tests[:3],
                "test_coverage": "direct",
            }
        ]
    }

    passed, reason = auto_chain.preflight_reconcile_cluster_pm(
        prd,
        candidate_nodes=auto_chain._cluster_payload_candidate_nodes(metadata),
    )

    assert not passed
    assert "test_phase_z_v2_feature_clusters.py" in reason


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
                "parent_id": "L3.22",
                "deps": ["L7.11"],
                "primary": ["agent/governance/reconcile_batch_memory.py"],
            }
        ],
    }

    prompt, out_meta = auto_chain._build_dev_prompt("pm-task", result, _candidate_metadata())

    assert "graph_delta.creates" in prompt
    assert out_meta["doc_impact"]["files"] == ["docs/governance/reconcile-workflow.md"]


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
    assert "Graph Consistency Check" in prompt
    assert "docs/governance/reconcile-workflow.md" in prompt
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
