"""Tests for task implementation timeline evidence."""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path


def _fake_sha(label: str) -> str:
    """Valid-format sha256:<64hex> digest derived from a label.

    The content-hash format floor (AC-ROUTE-CONTEXT-CONTENT-HASH-VERIFY-GATE)
    rejects non-digest hash strings, so test fixtures must use real digests.
    These tokens carry only the hash (no embedded object), so any valid-format
    digest satisfies the format-floor-only path; the label keeps them unique.
    """
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _conn(tmp_dir):
    os.environ["SHARED_VOLUME_PATH"] = tmp_dir
    os.makedirs(
        os.path.join(tmp_dir, "codex-tasks", "state", "governance", "proj"),
        exist_ok=True,
    )
    from agent.governance.db import get_connection

    return get_connection("proj")


def _ctx(query=None, *, path_params=None, body=None, method="GET"):
    from agent.governance import server

    params = {"project_id": "proj"}
    if path_params:
        params.update(path_params)
    return server.RequestContext(
        None,
        method,
        params,
        query or {},
        body or {},
        "req-test",
        "",
        "",
    )


ROUTE_IDENTITY = {
    "route_context_hash": "sha256:4920bc6ece43e5166504c5c91d8e657eb4bf7490eb85df81d668b6ea60f6a927",
    "prompt_contract_id": "rprompt-ac-service-route-context-gate-20260531",
    "prompt_contract_hash": "sha256:e96ff2d045d64d1578145c9ec1457ff0a3b220b6dfdfe35c331dff620a3e0e3a",
}

STRICT_GOVERNANCE_POLICY = {
    "schema_version": "governance_policy.v1",
    "profile": "aming-claw",
    "source": "pytest",
    "public_safe": True,
    "requirements": {
        "graph_first_evidence": True,
        "worker_graph_trace": True,
        "independent_qa": True,
        "single_active_task": True,
        "close_timeline": True,
    },
}


def _route_context_consumption_events():
    return [
        {
            "event_kind": "route_context",
            "phase": "dispatch",
            "status": "passed",
            "event_id": "tl-route-context",
            "payload": {
                "route_context": {
                    **ROUTE_IDENTITY,
                    "caller_role": "observer",
                    "allowed_actions": ["dispatch_worker"],
                    "blocked_actions": ["apply_patch"],
                    "required_lanes": ["bounded_implementation_worker"],
                },
                "visible_injection_manifest_hash": "sha256:test-visible-manifest",
            },
        },
        {
            "event_kind": "route_action_precheck",
            "phase": "pre_mutation",
            "status": "allowed",
            "event_id": "tl-route-action",
            "verification": {
                **ROUTE_IDENTITY,
                "allowed_action": "dispatch_worker",
                "caller_role": "observer",
            },
        },
        {
            "event_kind": "mf_subagent_dispatch",
            "phase": "dispatch",
            "status": "passed",
            "event_id": "tl-dispatch",
            "payload": {
                "mf_subagent_dispatch_gate": {
                    **ROUTE_IDENTITY,
                    "worker_id": "mf-sub-test",
                    "bounded": True,
                }
            },
        },
        {
            "event_kind": "mf_subagent_startup",
            "phase": "startup_gate",
            "status": "passed",
            "event_id": "tl-startup",
            "payload": {
                "mf_subagent_startup_gate": {
                    **ROUTE_IDENTITY,
                    "worker_id": "mf-sub-test",
                    "fence_token": "fence-test",
                    "actual_cwd": "/repo/.worktrees/mf-sub-test",
                    "actual_git_root": "/repo/.worktrees/mf-sub-test",
                    "branch": "refs/heads/codex/mf-sub-test",
                    "head_commit": "head-test",
                }
            },
        },
    ]


def _route_context_qa_verification_event():
    return {
        "event_kind": "qa_verification",
        "phase": "verification",
        "status": "passed",
        "event_id": "tl-qa-verification",
        "verification": {
            **ROUTE_IDENTITY,
            "contract_evidence": [
                {
                    "requirement_id": "independent_verification_lane",
                    "status": "passed",
                    "reviewer_role": "qa",
                }
            ],
        },
    }


def _route_owned_source_event(identity=None, *, source_event_id="route-source-1"):
    route_identity = dict(identity or ROUTE_IDENTITY)
    return {
        "event_type": "route.action.requested",
        "event_kind": "route_action_source_event",
        "phase": "pre_mutation",
        "status": "requested",
        "correlation_id": source_event_id,
        "payload": {
            "route_source_event": {
                **route_identity,
                "source_event_id": source_event_id,
                "action": "dispatch_bounded_worker",
                "raw_prompt_persisted": False,
            }
        },
        "verification": {
            **route_identity,
            "source_event_id": source_event_id,
        },
    }


def _route_owned_source_service_event(
    identity=None,
    *,
    parent_event_id=0,
    source_event_id="route-source-1",
):
    route_identity = dict(identity or ROUTE_IDENTITY)
    return {
        "event_type": "service.route.completed",
        "event_kind": "service_route",
        "phase": "route_service",
        "status": "accepted",
        "parent_event_id": parent_event_id,
        "payload": {
            "service_id": "route.action_precheck",
            "decision": "allow",
            "source_event_id": source_event_id,
            "route_evidence": {
                **route_identity,
                "source_event_id": source_event_id,
            },
        },
        "verification": {
            **route_identity,
            "source_event_id": source_event_id,
            "decision": "allow",
        },
    }


def _mf_subagent_read_receipt_event(event_id=0, contract_hash="", identity=None):
    payload = {"read_receipt_hash": "sha256:test-read-receipt"}
    if contract_hash:
        payload["canonical_visible_contract_text_hash"] = contract_hash
    if identity:
        payload.update(identity)
    return {
        "id": event_id,
        "event_type": "mf_subagent.read_receipt",
        "event_kind": "mf_subagent_read_receipt",
        "phase": "startup",
        "status": "accepted",
        "payload": payload,
    }


def _add_attempt_lineage(
    event,
    *,
    runtime_context_id,
    task_id,
    parent_task_id,
    worker_slot_id="mf-sub-test",
    fence_token="fence-test",
):
    event["task_id"] = task_id
    payload = event.setdefault("payload", {})
    payload.update({
        "runtime_context_id": runtime_context_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "worker_slot_id": worker_slot_id,
        "fence_token": fence_token,
    })
    return event


def _route_context_architecture_review_event():
    return {
        "event_kind": "architecture_review",
        "phase": "architecture_review",
        "status": "passed",
        "event_id": "tl-architecture-review",
        "verification": {
            **ROUTE_IDENTITY,
            "contract_evidence": [
                {
                    "requirement_id": "architecture_review_lane",
                    "status": "passed",
                    "reviewer_role": "architecture",
                }
            ],
        },
    }


def _route_context_worker_dispatch_event(identity=None):
    route_identity = dict(identity or ROUTE_IDENTITY)
    return {
        "event_type": "mf_subagent.dispatch",
        "event_kind": "mf_subagent_dispatch",
        "phase": "dispatch",
        "status": "passed",
        "payload": {
            "mf_subagent_dispatch_gate": {
                **route_identity,
                "worker_id": "mf-sub-test",
                "bounded": True,
            }
        },
    }


def _route_context_worker_startup_event(identity=None):
    route_identity = dict(identity or ROUTE_IDENTITY)
    return {
        "event_type": "mf_subagent.startup",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "payload": {
            "mf_subagent_startup_gate": {
                **route_identity,
                "worker_id": "mf-sub-test",
                "fence_token": "fence-test",
                "actual_cwd": "/repo/.worktrees/mf-sub-test",
                "actual_git_root": "/repo/.worktrees/mf-sub-test",
                "branch": "refs/heads/codex/mf-sub-test",
                "head_commit": "head-test",
            }
        },
    }


def test_read_receipt_gate_reports_projection_fields_and_ordering_failure():
    from agent.governance import task_timeline

    implementation_event = _add_attempt_lineage(
        {
            "id": 1,
            "event_type": "mf.implementation",
            "event_kind": "implementation",
            "phase": "implementation",
            "status": "passed",
            "payload": {**ROUTE_IDENTITY, "changed_files": ["agent/foo.py"]},
        },
        runtime_context_id="mfrctx-ordering",
        task_id="worker-task",
        parent_task_id="parent-task",
    )
    read_receipt_event = _add_attempt_lineage(
        _mf_subagent_read_receipt_event(event_id=2, identity=ROUTE_IDENTITY),
        runtime_context_id="mfrctx-ordering",
        task_id="worker-task",
        parent_task_id="parent-task",
    )

    gate = task_timeline.mf_subagent_read_receipt_gate_verification(
        [implementation_event, read_receipt_event],
        route_identity_filter={
            **ROUTE_IDENTITY,
            "runtime_context_id": "mfrctx-ordering",
            "task_id": "worker-task",
            "parent_task_id": "parent-task",
        },
    )

    assert gate["required"] is True
    assert gate["passed"] is False
    assert gate["status"] == "out_of_order"
    assert gate["failure_reason"] == "worker_read_receipt_recorded_after_counted_evidence"
    fields = gate["runtime_context_projection_evidence_fields"]
    assert fields["schema_version"] == "runtime_context.timeline_evidence_fields.v1"
    assert "read_receipt_hash" in fields["read_receipt"]
    assert "runtime_context_id" in fields["attempt_lineage_filter"]
    assert "read_receipt_order" in fields["ordering"]


def test_route_startup_gate_reports_runtime_context_projection_evidence_fields():
    from agent.governance import task_timeline

    events = [
        _route_context_worker_dispatch_event(),
        _route_context_worker_startup_event(),
    ]
    gate = task_timeline.mf_route_context_gate_verification(events)

    fields = gate["runtime_context_projection_evidence_fields"]
    assert fields["schema_version"] == "runtime_context.timeline_evidence_fields.v1"
    assert "actual_git_root" in fields["startup"]
    assert "prompt_contract_hash" in fields["route_identity"]
    assert "mf_subagent_startup" in fields["required_evidence_ids"]


def test_precheck_startup_projection_requirements_name_missing_field():
    from agent.governance import precheck_service

    projection = {
        "schema_version": "runtime_context.current.v1",
        "worker_view": {
            "runtime_context_id": "mfrctx-precheck",
            "task_id": "worker-task",
            "parent_task_id": "parent-task",
            "worker_role": "mf_sub",
            "fence_token": "fence-precheck",
            "actual_fence_token": "fence-precheck",
            "branch_ref": "refs/heads/codex/precheck",
            "worktree_path": "/repo/.worktrees/precheck",
            "actual_git_root": "/repo/.worktrees/precheck",
            "base_commit": "base-precheck",
            "target_head_commit": "target-precheck",
            "merge_queue_id": "mq-precheck",
            "route_context_hash": _fake_sha("route"),
            "prompt_contract_id": "rprompt-precheck",
            "graph_query_identity": {
                "query_source": "mf_subagent",
                "parent_task_id": "parent-task",
            },
        },
    }

    result = precheck_service.run_precheck(
        "mf_subagent.startup",
        "contract-precheck",
        "startup_gate",
        {
            "runtime_context_projection": projection,
            "fence_token": "fence-precheck",
            "actual_fence_token": "fence-precheck",
        },
        "worker-b1",
    )

    evidence = result["evidence"]
    requirements = evidence["runtime_context_projection_requirements"]
    assert requirements["explicit_projection"] is True
    assert "prompt_contract_hash" in requirements["missing_fields"]
    missing = [
        item
        for item in requirements["missing"]
        if item["field"] == "prompt_contract_hash"
    ]
    assert missing
    assert missing[0]["expected_source"] == (
        "runtime_context.gate_inputs.v1.prompt_contract_hash"
    )
    assert missing[0]["producer"] == "runtime_context_service"
    assert missing[0]["consumer"] == "precheck_service"
    assert "missing_runtime_context_projection:prompt_contract_hash" in evidence["errors"]


def test_graph_query_identity_fields_are_projection_ready():
    from agent.governance import graph_query_trace

    identity = graph_query_trace.graph_query_identity(
        {
            "trace_id": "gqt-test",
            "project_id": "aming-claw",
            "snapshot_id": "scope-test",
            "query_source": "mf_subagent",
            "query_purpose": "subagent_context_build",
            "parent_task_id": "parent-task",
            "runtime_context_id": "mfrctx-graph",
            "task_id": "worker-task",
            "worker_role": "mf_sub",
            "fence_token": "fence-graph",
            "actor": "subagent-runtime-context-consumers-b1",
        }
    )

    assert identity["schema_version"] == "runtime_context.graph_query_identity.v1"
    assert identity["trace_id"] == "gqt-test"
    assert identity["runtime_context_id"] == "mfrctx-graph"
    assert "query_source" in identity["identity_fields"]
    assert "fence_token" in identity["identity_fields"]


def _runtime_text_generated_startup_intent_event(identity=None):
    route_identity = dict(identity or ROUTE_IDENTITY)
    launch_text_hash = "sha256:runtime-text-launch"
    return {
        "schema_version": "mf_subagent_startup_intent_event.v1",
        "event_type": "mf_subagent.startup_intent",
        "event_kind": "mf_subagent_startup_intent",
        "phase": "startup_intent",
        "status": "planned",
        "actor": "observer_runtime_text",
        "task_id": "BUG-RUNTIME-TEXT-STARTUP-impl-1",
        "backlog_id": "BUG-RUNTIME-TEXT-STARTUP",
        "close_satisfying": False,
        "actual_startup_required": True,
        "payload": {
            "mf_subagent_startup_intent": {
                "schema_version": "mf_subagent_startup_intent.v1",
                "intent_kind": "mf_subagent.startup_intent",
                "status": "planned",
                "close_satisfying": False,
                "actual_startup_required": True,
                **route_identity,
                "runtime_context_id": "orctx-runtime-text",
                "launch_text_hash": launch_text_hash,
                "raw_launch_text_persisted": False,
                "project_id": "proj",
                "task_id": "BUG-RUNTIME-TEXT-STARTUP-impl-1",
                "parent_task_id": "BUG-RUNTIME-TEXT-STARTUP",
                "worker_role": "mf_sub",
                "role": "mf_sub",
                "fence_token": "fence-runtime-text",
                "assigned_worktree": "/repo/.worktrees/runtime-text",
                "worktree_path": "/repo/.worktrees/runtime-text",
                "branch": "refs/heads/runtime-text/BUG-RUNTIME-TEXT-STARTUP-impl-1",
                "head_commit": "target123",
                "base_commit": "base123",
                "target_head_commit": "target123",
            }
        },
        "artifact_refs": {
            "runtime_context_id": "orctx-runtime-text",
            "launch_text_hash": launch_text_hash,
        },
    }


def _runtime_text_actual_startup_event(identity=None):
    route_identity = dict(identity or ROUTE_IDENTITY)
    return {
        "schema_version": "task_timeline_event_packet.v1",
        "event_type": "mf_subagent.startup",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "actor": "mf_sub",
        "task_id": "BUG-RUNTIME-TEXT-STARTUP-impl-1",
        "backlog_id": "BUG-RUNTIME-TEXT-STARTUP",
        "payload": {
            "mf_subagent_startup_gate": {
                "schema_version": "mf_subagent_startup_gate.v1",
                "gate_kind": "mf_subagent.startup",
                "status": "passed",
                "bounded": True,
                "same_as_expected_worker": True,
                "fence_token_matches": True,
                **route_identity,
                "runtime_context_id": "orctx-runtime-text",
                "launch_text_hash": "sha256:runtime-text-launch",
                "raw_launch_text_persisted": False,
                "project_id": "proj",
                "task_id": "BUG-RUNTIME-TEXT-STARTUP-impl-1",
                "parent_task_id": "BUG-RUNTIME-TEXT-STARTUP",
                "worker_role": "mf_sub",
                "role": "mf_sub",
                "fence_token": "fence-runtime-text",
                "assigned_worktree": "/repo/.worktrees/runtime-text",
                "worktree_path": "/repo/.worktrees/runtime-text",
                "actual_cwd": "/repo/.worktrees/runtime-text",
                "actual_git_root": "/repo/.worktrees/runtime-text",
                "branch": "refs/heads/runtime-text/BUG-RUNTIME-TEXT-STARTUP-impl-1",
                "head_commit": "target123",
                "base_commit": "base123",
                "target_head_commit": "target123",
            }
        },
    }


def _runtime_contract_read_receipt_event(
    *,
    runtime_context_id="mfrctx-dogfood",
    task_id="worker-task-dogfood",
    parent_task_id="BUG-DOGFOOD",
    fence_token="fence-dogfood",
    launch_text_hash="sha256:dogfood-launch",
):
    return {
        "event_type": "mf_subagent.read_receipt",
        "event_kind": "mf_subagent_read_receipt",
        "phase": "startup",
        "status": "accepted",
        "payload": {
            "schema_version": "mf_subagent_read_receipt.v1",
            "runtime_context_id": runtime_context_id,
            "launch_text_hash": launch_text_hash,
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "worker_role": "mf_sub",
            "fence_token": fence_token,
            "raw_launch_text_persisted": False,
        },
    }


def _runtime_contract_startup_event(
    identity=None,
    *,
    runtime_context_id="mfrctx-dogfood",
    task_id="worker-task-dogfood",
    parent_task_id="BUG-DOGFOOD",
    fence_token="fence-dogfood",
):
    route_identity = dict(identity or ROUTE_IDENTITY)
    return {
        "event_type": "mf_subagent.startup",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "actor": "mf_sub",
        "payload": {
            "mf_subagent_startup_gate": {
                "schema_version": "mf_subagent_startup_gate.v1",
                "status": "passed",
                "bounded": True,
                **route_identity,
                "route_id": "route-dogfood",
                "visible_injection_manifest_hash": "sha256:dogfood-visible",
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
                "parent_task_id": parent_task_id,
                "worker_role": "mf_sub",
                "worker_slot_id": "mfsub-dogfood",
                "worker_id": "mfsub-dogfood",
                "fence_token": fence_token,
                "actual_cwd": "/repo/.worktrees/dogfood",
                "actual_git_root": "/repo/.worktrees/dogfood",
                "worktree_path": "/repo/.worktrees/dogfood",
                "branch": "refs/heads/codex/dogfood",
                "head_commit": "head-dogfood",
                "base_commit": "base-dogfood",
                "target_head_commit": "target-dogfood",
                "merge_queue_id": "mq-dogfood",
                "owned_files": ["agent/governance/task_timeline.py"],
            }
        },
    }


def _runtime_contract_bounded_dispatch_event(
    identity=None,
    *,
    runtime_context_id="mfrctx-dogfood",
    task_id="worker-task-dogfood",
    parent_task_id="BUG-DOGFOOD",
    fence_token="fence-dogfood",
    read_receipt_event_id=0,
    startup_event_id=0,
):
    route_identity = dict(identity or ROUTE_IDENTITY)
    return {
        "event_type": "mf_subagent.dispatch",
        "event_kind": "bounded_implementation_worker_dispatch",
        "phase": "dispatch",
        "status": "accepted",
        "payload": {
            "bounded_implementation_worker_dispatch": {
                "schema_version": "bounded_implementation_worker_dispatch.v1",
                **route_identity,
                "route_id": "route-dogfood",
                "visible_injection_manifest_hash": "sha256:dogfood-visible",
                "runtime_context_id": runtime_context_id,
                "task_id": task_id,
                "parent_task_id": parent_task_id,
                "worker_role": "mf_sub",
                "worker_slot_id": "mfsub-dogfood",
                "fence_token": fence_token,
                "worktree_path": "/repo/.worktrees/dogfood",
                "branch": "refs/heads/codex/dogfood",
                "base_commit": "base-dogfood",
                "target_head_commit": "target-dogfood",
                "merge_queue_id": "mq-dogfood",
                "owned_files": ["agent/governance/task_timeline.py"],
                "read_receipt_event_id": read_receipt_event_id,
                "startup_event_id": startup_event_id,
                "raw_private_context_exposed": False,
            }
        },
    }


def _runtime_text_legacy_weak_startup_event(identity=None):
    route_identity = dict(identity or ROUTE_IDENTITY)
    return {
        "event_type": "mf_subagent.startup",
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "actor": "observer_runtime_text",
        "payload": {
            "mf_subagent_startup_gate": {
                **route_identity,
                "runtime_context_id": "orctx-runtime-text",
                "launch_text_hash": "sha256:runtime-text-launch",
                "project_id": "proj",
                "task_id": "BUG-RUNTIME-TEXT-STARTUP-impl-1",
                "parent_task_id": "BUG-RUNTIME-TEXT-STARTUP",
                "worker_role": "mf_sub",
                "fence_token": "fence-runtime-text",
                "branch": "refs/heads/runtime-text/BUG-RUNTIME-TEXT-STARTUP-impl-1",
                "head_commit": "target123",
            }
        },
    }


def _without_prompt_contract_hash(value):
    if isinstance(value, dict):
        return {
            key: _without_prompt_contract_hash(item)
            for key, item in value.items()
            if key != "prompt_contract_hash"
        }
    if isinstance(value, list):
        return [_without_prompt_contract_hash(item) for item in value]
    return value


def _replace_route_identity(value, identity):
    if isinstance(value, dict):
        replaced = {
            key: _replace_route_identity(item, identity)
            for key, item in value.items()
        }
        for key in ROUTE_IDENTITY:
            if key in replaced:
                replaced[key] = identity[key]
        return replaced
    if isinstance(value, list):
        return [_replace_route_identity(item, identity) for item in value]
    return value


def _route_token(action="task_timeline_append", bug_id="BUG-ROUTE", task_id="", project_id="proj"):
    scope = {"project_id": project_id, "backlog_id": bug_id}
    if task_id:
        scope["task_id"] = task_id
    return {
        "route_context_hash": _fake_sha(f"test-route-context-{action}"),
        "prompt_contract_id": f"prompt-contract-{action}",
        "prompt_contract_hash": _fake_sha(f"test-prompt-contract-{action}"),
        "caller_role": "observer",
        "allowed_action": action,
        "scope": scope,
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": ["timeline:test-route-token"],
    }


def _bound_route_token_gate(action="service_route", bug_id="BUG-ROUTE", task_id="", project_id="proj"):
    token = _route_token(action, bug_id=bug_id, task_id=task_id, project_id=project_id)
    return {
        "schema_version": "route_token_mutation_gate.v1",
        "allowed": True,
        "status": "accepted",
        "action": action,
        "decision": "route_token",
        "route_token_ref": f"rtok-test-{bug_id}",
        "server_issued_binding": True,
        "binding_source": "observer_route_token_refs",
        "route_context_hash": token["route_context_hash"],
        "prompt_contract_id": token["prompt_contract_id"],
        "prompt_contract_hash": token["prompt_contract_hash"],
        "caller_role": token["caller_role"],
        "route_token_hash": _fake_sha(f"test-route-token-body-{bug_id}-{task_id}"),
        "scope": token["scope"],
    }


def _route_token_gate_event(
    event_id=0,
    action="task_timeline_append",
    bug_id="BUG-ROUTE",
    task_id="",
):
    gate = _route_token(action, bug_id, task_id=task_id)
    gate.update({"decision": "route_token", "status": "accepted"})
    return {
        "id": event_id,
        "event_type": f"route_token_gate.{action}",
        "event_kind": "verification",
        "phase": "route_gate",
        "actor": "observer",
        "status": "accepted",
        "payload": {"route_token_gate": gate},
        "verification": gate,
    }


def _route_waiver(action="task_timeline_append", bug_id="BUG-ROUTE", task_id="", project_id="proj"):
    scope = {"project_id": project_id, "backlog_id": bug_id}
    if task_id:
        scope["task_id"] = task_id
    return {
        "accepted": True,
        "waiver_type": "manual_fix",
        "route_context_hash": _fake_sha(f"test-route-context-{action}"),
        "prompt_contract_id": f"prompt-contract-{action}",
        "caller_role": "observer",
        "allowed_action": action,
        "scope": scope,
        "reason": "Unit test supplies explicit route gate waiver evidence.",
        "timeline_evidence": {"event_id": "test-route-gate"},
    }


class TestTaskTimeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def _issue_route_token(
        self,
        bug_id,
        *,
        task_id="",
        allowed_actions=None,
    ):
        from agent.governance import observer_route_context

        token = observer_route_context.build_observer_write_route_token(
            project_id="proj",
            backlog_id=bug_id,
            task_id=task_id or bug_id,
            target_files=["agent/governance/task_timeline.py"],
            allowed_actions=allowed_actions or ["task_timeline_append"],
            now=datetime(2099, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
        )
        ref = observer_route_context.derive_route_token_ref(token)
        observer_route_context.persist_route_token_ref(
            self.conn,
            project_id="proj",
            route_token_ref=ref,
            token=token,
        )
        return {"route_token": token, "route_token_ref": ref}

    def _insert_router_backlog(self, bug_id="BUG-SERVICE-ROUTER", contract=None):
        contract = contract or {
            "parallel_contract": {
                "template_id": "mf_parallel.v1",
                "contract_instance_id": bug_id,
            }
        }
        self.conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, chain_trigger_json, created_at, updated_at)
               VALUES (?, ?, 'OPEN', 'P1', ?, '2026-05-29T00:00:00Z', '2026-05-29T00:00:00Z')""",
            (bug_id, "Service router test", json.dumps(contract)),
        )
        self.conn.commit()
        return contract

    def _record_route_owned_source_lineage(self, bug_id, *, task_id=""):
        from agent.governance import task_timeline

        source_event_id = f"source-{bug_id}-{task_id or 'backlog'}"
        source = _route_owned_source_event(source_event_id=source_event_id)
        recorded_source = task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id=bug_id,
            task_id=task_id,
            event_type=source["event_type"],
            phase=source["phase"],
            event_kind=source["event_kind"],
            status=source["status"],
            payload=source["payload"],
            verification=source["verification"],
            correlation_id=source["correlation_id"],
        )
        service_event = _route_owned_source_service_event(
            parent_event_id=recorded_source["id"],
            source_event_id=source_event_id,
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id=bug_id,
            task_id=task_id,
            event_type=service_event["event_type"],
            phase=service_event["phase"],
            event_kind=service_event["event_kind"],
            status=service_event["status"],
            parent_event_id=service_event["parent_event_id"],
            payload=service_event["payload"],
            verification=service_event["verification"],
        )
        self.conn.commit()

    def _record_route_context_consumption(
        self,
        bug_id,
        *,
        task_id="",
        include_source_lineage=True,
    ):
        from agent.governance import task_timeline

        if include_source_lineage:
            self._record_route_owned_source_lineage(bug_id, task_id=task_id)
        for event in [
            *_route_context_consumption_events(),
            _route_context_qa_verification_event(),
        ]:
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id=bug_id,
                task_id=task_id,
                event_type=event.get("event_type") or event.get("event_kind"),
                phase=event.get("phase", ""),
                event_kind=event.get("event_kind", ""),
                status=event.get("status", ""),
                payload=event.get("payload") or {},
                verification=event.get("verification") or {},
                artifact_refs=event.get("artifact_refs") or {},
            )
        self.conn.commit()

    def _record_route_service_context(self, bug_id, *, task_id=""):
        from agent.governance import task_timeline

        self._record_route_owned_source_lineage(bug_id, task_id=task_id)
        for event in _route_context_consumption_events()[:2]:
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id=bug_id,
                task_id=task_id,
                event_type=event.get("event_type") or event.get("event_kind"),
                phase=event.get("phase", ""),
                event_kind=event.get("event_kind", ""),
                status=event.get("status", ""),
                payload=event.get("payload") or {},
                verification=event.get("verification") or {},
                artifact_refs=event.get("artifact_refs") or {},
            )
        self.conn.commit()

    def _route_waiver_for_existing_identity(self, bug_id, *, task_id=""):
        waiver = _route_waiver("task_timeline_append", bug_id, task_id=task_id)
        waiver.update(ROUTE_IDENTITY)
        return waiver

    def test_record_event_accepts_observer_work_mode_transition(self):
        from agent.governance import task_timeline

        recorded = task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-WORK-MODE-TRANSITION",
            event_type="observer.work_mode_transition",
            event_kind="observer_work_mode_transition",
            phase="routing",
            actor="observer",
            status="accepted",
            payload={
                "from_work_mode": "observer_look_before_act",
                "to_work_mode": "observer_execution_supervisor",
                "route_identity": {
                    "route_id": "route-1",
                    "route_context_hash": "sha256:ctx",
                    "prompt_contract_id": "rprompt-1",
                },
                "route_action_precheck_event_id": "timeline:4366",
            },
        )

        gate = recorded["payload"]["meta_contract_gate"]
        self.assertTrue(gate["allowed"])
        self.assertEqual(gate["role"], "observer")
        self.assertEqual(gate["action"], "observer_work_mode_transition")

    def test_timeline_append_accepts_observer_work_mode_transition(self):
        from agent.governance import server

        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": "BUG-WORK-MODE-TRANSITION-APPEND",
                    "event_type": "observer.work_mode_transition",
                    "event_kind": "observer_work_mode_transition",
                    "phase": "routing",
                    "actor": "observer",
                    "status": "accepted",
                    "payload": {
                        "from_work_mode": "observer_look_before_act",
                        "to_work_mode": "observer_execution_supervisor",
                        "route_identity": {
                            "route_id": "route-1",
                            "route_context_hash": "sha256:ctx",
                            "prompt_contract_id": "rprompt-1",
                        },
                        "route_action_precheck_event_id": "timeline:4366",
                    },
                },
                method="POST",
            )
        )

        gate = result["payload"]["meta_contract_gate"]
        self.assertTrue(gate["allowed"])
        self.assertEqual(gate["action"], "observer_work_mode_transition")
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
            ("BUG-WORK-MODE-TRANSITION-APPEND",),
        ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_record_event_rejects_observer_work_mode_transition_phase_bypass(self):
        from agent.governance import task_timeline
        from agent.governance.mf_subagent_contract import MfSubagentContractError

        with self.assertRaisesRegex(MfSubagentContractError, "unknown timeline action"):
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id="BUG-WORK-MODE-TRANSITION-BYPASS",
                event_type="observer.work_mode_transition.unbound",
                event_kind="",
                phase="route_context",
                actor="observer",
                status="accepted",
                payload={
                    "route_identity": {
                        "route_id": "route-1",
                        "route_context_hash": "sha256:ctx",
                        "prompt_contract_id": "rprompt-1",
                    },
                    "route_action_precheck_event_id": "timeline:4366",
                },
            )
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
            ("BUG-WORK-MODE-TRANSITION-BYPASS",),
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_record_event_publishes_timeline_and_current_task_events(self):
        from agent.governance import event_bus, task_timeline

        bus = event_bus.get_event_bus()
        published = []

        def on_event(name, payload):
            if name in {"task_timeline.appended", "current_task.changed"}:
                published.append((name, payload))

        bus.subscribe_all(on_event)
        try:
            inserted = task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id="BUG-SSE",
                task_id="task-sse",
                event_type="mf.subagent.startup",
                phase="startup",
                event_kind="mf_subagent.startup",
                actor="mf-sub",
                status="running",
            )
        finally:
            bus.unsubscribe_all(on_event)

        timeline = next(
            payload for name, payload in published if name == "task_timeline.appended"
        )
        current = next(
            payload for name, payload in published if name == "current_task.changed"
        )
        self.assertEqual(timeline["project_id"], "proj")
        self.assertEqual(timeline["backlog_id"], "BUG-SSE")
        self.assertEqual(timeline["task_id"], "task-sse")
        self.assertEqual(timeline["event_id"], inserted["id"])
        self.assertEqual(timeline["status"], "running")
        self.assertEqual(current["source"], "task_timeline.record_event")
        self.assertEqual(current["runtime_state"], "running")

    def test_queued_event_publishes_timeline_and_current_task_events(self):
        from agent.governance import event_bus, task_timeline

        bus = event_bus.get_event_bus()
        published = []

        def on_event(name, payload):
            if name in {"task_timeline.appended", "current_task.changed"}:
                published.append((name, payload))

        bus.subscribe_all(on_event)
        try:
            inserted = task_timeline.enqueue_event(
                "proj",
                backlog_id="BUG-SSE-QUEUE",
                task_id="task-sse-queue",
                event_type="mf.subagent.progress",
                phase="implementation",
                event_kind="implementation",
                actor="mf-sub",
                status="running",
                wait=True,
            )
        finally:
            bus.unsubscribe_all(on_event)

        timeline = next(
            payload for name, payload in published if name == "task_timeline.appended"
        )
        current = next(
            payload for name, payload in published if name == "current_task.changed"
        )
        self.assertEqual(timeline["backlog_id"], "BUG-SSE-QUEUE")
        self.assertEqual(timeline["task_id"], "task-sse-queue")
        self.assertEqual(timeline["event_id"], inserted["id"])
        self.assertEqual(current["source"], "task_timeline.record_event")
        self.assertEqual(current["runtime_state"], "running")

    def test_concurrent_timeline_writes_use_serialized_queue(self):
        from agent.governance import task_timeline

        errors = []

        def write(i):
            try:
                task_timeline.enqueue_event(
                    "proj",
                    task_id="task-concurrent",
                    backlog_id="BUG-TL",
                    attempt_num=1,
                    event_type="ai.implementation_evidence.proposed",
                    actor=f"worker-{i}",
                    status="proposed",
                    payload={"i": i},
                    wait=True,
                )
            except Exception as exc:  # pragma: no cover - failure surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        events = task_timeline.list_events(self.conn, "proj", task_id="task-concurrent")
        self.assertEqual(len(events), 20)
        self.assertEqual(
            {event["payload"]["i"] for event in events},
            set(range(20)),
        )

    def test_timeline_append_protected_close_evidence_requires_route_token_before_mutation(self):
        from agent.governance import server
        from agent.governance.errors import GovernanceError

        for event_kind in ("implementation", "qa_verification", "independent_verification"):
            with self.subTest(event_kind=event_kind):
                with self.assertRaises(GovernanceError) as raised:
                    server.handle_task_timeline_append(
                        _ctx(
                            body={
                                "backlog_id": "BUG-TL-PROTECTED",
                                "event_type": f"mf.{event_kind}",
                                "event_kind": event_kind,
                                "status": "accepted",
                            },
                            method="POST",
                        )
                    )

                self.assertEqual(raised.exception.code, "route_token_required")
                self.assertEqual(
                    raised.exception.details["fault_domain"],
                    "caller_missing_route_evidence",
                )
                self.assertTrue(raised.exception.details["expected_behavior"])
                self.assertTrue(raised.exception.details["do_not_file_system_bug"])
                self.assertFalse(raised.exception.details["is_system_bug"])
                self.assertIn("next_valid_actions", raised.exception.details)
                self.assertIn("system_bug_preconditions", raised.exception.details)
                count = self.conn.execute(
                    "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
                    ("BUG-TL-PROTECTED",),
                ).fetchone()["c"]
                self.assertEqual(count, 0)

    def test_timeline_append_rejects_generic_waiver_without_route_identity(self):
        from agent.governance import server
        from agent.governance.errors import GovernanceError

        with self.assertRaises(GovernanceError) as raised:
            server.handle_task_timeline_append(
                _ctx(
                    body={
                        "backlog_id": "BUG-TL-BAD-WAIVER",
                        "event_type": "mf.verification",
                        "event_kind": "verification",
                        "status": "passed",
                        "route_waiver": {
                            "accepted": True,
                            "waiver_type": "manual_fix",
                            "allowed_action": "task_timeline_append",
                            "scope": {"project_id": "proj", "backlog_id": "BUG-TL-BAD-WAIVER"},
                            "reason": "Unit test supplies explicit route gate waiver evidence.",
                            "timeline_evidence": {"event_id": "test-route-gate"},
                        },
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "route_token_required")
        self.assertIn("route identity", str(raised.exception))
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
            ("BUG-TL-BAD-WAIVER",),
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_timeline_append_accepts_valid_route_token(self):
        from agent.governance import server

        issued = self._issue_route_token("BUG-TL-TOKEN")
        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": "BUG-TL-TOKEN",
                    "event_type": "mf.close_ready",
                    "event_kind": "close_ready",
                    "status": "accepted",
                    "route_token": issued["route_token"],
                },
                method="POST",
            )
        )

        self.assertEqual(result["route_token_gate"]["decision"], "route_token")
        self.assertTrue(result["route_token_gate"]["server_issued_binding"])
        self.assertEqual(
            result["route_token_gate"]["route_token_ref"],
            issued["route_token_ref"],
        )
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
            ("BUG-TL-TOKEN",),
        ).fetchone()["c"]
        self.assertEqual(count, 2)

    def test_timeline_append_rejects_full_route_token_with_allowed_actions_superset(self):
        from agent.governance import server
        from agent.governance.errors import GovernanceError

        issued = self._issue_route_token(
            "BUG-TL-TOKEN-SUPERSET",
            allowed_actions=["task_timeline_append"],
        )
        token = dict(issued["route_token"])
        token["allowed_actions"] = [
            *token["allowed_actions"],
            "backlog_close",
        ]

        with self.assertRaises(GovernanceError) as raised:
            server.handle_task_timeline_append(
                _ctx(
                    body={
                        "backlog_id": "BUG-TL-TOKEN-SUPERSET",
                        "event_type": "mf.close_ready",
                        "event_kind": "close_ready",
                        "status": "accepted",
                        "route_token": token,
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "route_token_required")
        self.assertIn("allowed_actions exceed", str(raised.exception))
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
            ("BUG-TL-TOKEN-SUPERSET",),
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_timeline_append_accepts_server_minted_ref_only_route_token(self):
        from agent.governance import server

        issued = self._issue_route_token("BUG-TL-REF-TOKEN")
        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": "BUG-TL-REF-TOKEN",
                    "event_type": "mf.close_ready",
                    "event_kind": "close_ready",
                    "status": "accepted",
                    "route_token_ref": issued["route_token_ref"],
                },
                method="POST",
            )
        )

        self.assertEqual(
            result["route_token_gate"]["decision"],
            "route_token_ref_resolved",
        )
        self.assertTrue(result["route_token_gate"]["server_issued_binding"])
        self.assertEqual(
            result["route_token_gate"]["route_token_ref"],
            issued["route_token_ref"],
        )

    def test_timeline_append_accepts_route_context_waiver(self):
        from agent.governance import server

        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": "BUG-TL-WAIVER",
                    "event_type": "mf.verification",
                    "event_kind": "verification",
                    "status": "passed",
                    "route_waiver": _route_waiver("task_timeline_append", "BUG-TL-WAIVER"),
                },
                method="POST",
            )
        )

        self.assertEqual(result["route_token_gate"]["decision"], "route_waiver")
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
            ("BUG-TL-WAIVER",),
        ).fetchone()["c"]
        self.assertEqual(count, 2)

    def test_mf_parallel_timeline_rejects_generic_waiver_for_protected_evidence(self):
        from agent.governance import server
        from agent.governance.errors import GovernanceError

        for event_kind in (
            "implementation",
            "verification",
            "close_ready",
            "checkpoint",
            "export",
        ):
            bug_id = f"BUG-TL-MF-PARALLEL-WAIVER-{event_kind.upper()}"
            self._insert_router_backlog(bug_id)
            self._record_route_service_context(bug_id)
            payload = {}
            if event_kind == "implementation":
                payload = _route_context_worker_dispatch_event()["payload"]

            with self.subTest(event_kind=event_kind):
                with self.assertRaises(GovernanceError) as raised:
                    server.handle_task_timeline_append(
                        _ctx(
                            body={
                                "backlog_id": bug_id,
                                "event_type": f"mf.{event_kind}",
                                "event_kind": event_kind,
                                "status": "accepted",
                                "payload": payload,
                                "route_waiver": self._route_waiver_for_existing_identity(bug_id),
                            },
                            method="POST",
                        )
                    )

                self.assertEqual(raised.exception.code, "route_token_required")
                self.assertEqual(
                    raised.exception.details["fault_domain"],
                    "caller_missing_route_evidence",
                )
                self.assertTrue(raised.exception.details["waiver_evidence_only"])
                self.assertIn(
                    "bounded_implementation_worker_dispatch",
                    raised.exception.details["required_before_protected_evidence"],
                )
                self.assertIn(
                    "mf_subagent_startup",
                    raised.exception.details["required_before_protected_evidence"],
                )
                count = self.conn.execute(
                    "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ? AND event_kind = ?",
                    (bug_id, event_kind),
                ).fetchone()["c"]
                self.assertEqual(count, 0)

    def test_mf_parallel_route_waiver_bootstraps_bounded_dispatch_evidence(self):
        from agent.governance import server, task_timeline

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-DISPATCH-BOOTSTRAP"
        contract = self._insert_router_backlog(bug_id)
        self._record_route_service_context(bug_id)
        event = _route_context_worker_dispatch_event()

        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    **event,
                    "route_waiver": self._route_waiver_for_existing_identity(bug_id),
                },
                method="POST",
            )
        )

        self.assertEqual(result["route_token_gate"]["decision"], "route_waiver")
        self.assertEqual(result["event_kind"], "mf_subagent_dispatch")
        events = task_timeline.list_events(self.conn, "proj", backlog_id=bug_id)
        gate = task_timeline.mf_route_context_gate_verification(
            events,
            contract=contract,
        )
        self.assertIn(
            "bounded_implementation_worker_dispatch",
            gate["present_requirement_ids"],
        )
        self.assertIn(
            "mf_subagent_startup",
            gate["missing_requirement_ids"],
        )

    def test_mf_parallel_route_waiver_bootstraps_startup_evidence(self):
        from agent.governance import server, task_timeline

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-STARTUP-BOOTSTRAP"
        contract = self._insert_router_backlog(bug_id)
        self._record_route_service_context(bug_id)
        event = _route_context_worker_startup_event()

        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    **event,
                    "route_waiver": self._route_waiver_for_existing_identity(bug_id),
                },
                method="POST",
            )
        )

        self.assertEqual(result["route_token_gate"]["decision"], "route_waiver")
        self.assertEqual(result["event_kind"], "mf_subagent_startup")
        events = task_timeline.list_events(self.conn, "proj", backlog_id=bug_id)
        gate = task_timeline.mf_route_context_gate_verification(
            events,
            contract=contract,
        )
        self.assertIn(
            "mf_subagent_startup",
            gate["present_requirement_ids"],
        )
        self.assertIn(
            "bounded_implementation_worker_dispatch",
            gate["missing_requirement_ids"],
        )

    def test_mf_parallel_task_scoped_route_waiver_bootstraps_dispatch_from_backlog_route_context(self):
        from agent.governance import server, task_timeline

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-TASK-DISPATCH-BOOTSTRAP"
        worker_task_id = "worker-task-dispatch-bootstrap"
        contract = self._insert_router_backlog(bug_id)
        self._record_route_service_context(bug_id)
        startup_event = _route_context_worker_startup_event()
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id=bug_id,
            task_id=worker_task_id,
            event_type=startup_event["event_type"],
            phase=startup_event["phase"],
            event_kind=startup_event["event_kind"],
            status=startup_event["status"],
            payload=startup_event["payload"],
        )
        self.conn.commit()

        dispatch_result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "task_id": worker_task_id,
                    **_route_context_worker_dispatch_event(),
                    "route_waiver": self._route_waiver_for_existing_identity(
                        bug_id,
                        task_id=worker_task_id,
                    ),
                },
                method="POST",
            )
        )

        self.assertEqual(dispatch_result["route_token_gate"]["decision"], "route_waiver")
        self.assertEqual(dispatch_result["event_kind"], "mf_subagent_dispatch")
        gate = task_timeline.mf_route_context_gate_verification(
            task_timeline.list_events(self.conn, "proj", backlog_id=bug_id),
            contract=contract,
        )
        self.assertIn(
            "bounded_implementation_worker_dispatch",
            gate["present_requirement_ids"],
        )
        self.assertIn("mf_subagent_startup", gate["present_requirement_ids"])
        self.assertEqual(gate["missing_requirement_ids"], ["independent_verification_lane"])

    def test_mf_parallel_route_waiver_bootstraps_dispatch_after_read_receipt_startup_lineage(self):
        from agent.governance import server, task_timeline

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-DISPATCH-READ-STARTUP"
        worker_task_id = "worker-task-read-startup"
        contract = self._insert_router_backlog(bug_id)
        self._record_route_owned_source_lineage(bug_id, task_id=worker_task_id)
        read_receipt = _mf_subagent_read_receipt_event(identity=ROUTE_IDENTITY)
        startup_event = _route_context_worker_startup_event()
        for event in (read_receipt, startup_event):
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id=bug_id,
                task_id=worker_task_id,
                event_type=event.get("event_type") or event.get("event_kind"),
                phase=event.get("phase", ""),
                event_kind=event.get("event_kind", ""),
                status=event.get("status", ""),
                payload=event.get("payload") or {},
                verification=event.get("verification") or {},
            )
        self.conn.commit()

        dispatch_result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "task_id": worker_task_id,
                    **_route_context_worker_dispatch_event(),
                    "route_waiver": self._route_waiver_for_existing_identity(
                        bug_id,
                        task_id=worker_task_id,
                    ),
                },
                method="POST",
            )
        )

        self.assertEqual(dispatch_result["route_token_gate"]["decision"], "route_waiver")
        self.assertEqual(dispatch_result["event_kind"], "mf_subagent_dispatch")
        gate = task_timeline.mf_route_context_gate_verification(
            task_timeline.list_events(
                self.conn,
                "proj",
                backlog_id=bug_id,
                task_id=worker_task_id,
            ),
            contract=contract,
        )
        self.assertIn(
            "bounded_implementation_worker_dispatch",
            gate["present_requirement_ids"],
        )
        self.assertIn("mf_subagent_startup", gate["present_requirement_ids"])

    def test_bounded_worker_dispatch_accepts_runtime_contract_lineage(self):
        from agent.governance import server, task_timeline

        bug_id = "BUG-TL-BOUNDED-DISPATCH-RUNTIME-LINEAGE"
        worker_task_id = "worker-task-dogfood"
        runtime_context_id = "mfrctx-dogfood"
        contract = self._insert_router_backlog(bug_id)
        self._record_route_service_context(bug_id)

        read_receipt = _runtime_contract_read_receipt_event(
            runtime_context_id=runtime_context_id,
            task_id=worker_task_id,
            parent_task_id=bug_id,
        )
        startup_event = _runtime_contract_startup_event(
            runtime_context_id=runtime_context_id,
            task_id=worker_task_id,
            parent_task_id=bug_id,
        )
        recorded_read_receipt = task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id=bug_id,
            task_id=worker_task_id,
            event_type=read_receipt["event_type"],
            phase=read_receipt["phase"],
            event_kind=read_receipt["event_kind"],
            status=read_receipt["status"],
            payload=read_receipt["payload"],
        )
        recorded_startup = task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id=bug_id,
            task_id=worker_task_id,
            event_type=startup_event["event_type"],
            phase=startup_event["phase"],
            event_kind=startup_event["event_kind"],
            status=startup_event["status"],
            actor=startup_event["actor"],
            payload=startup_event["payload"],
        )
        self.conn.commit()

        dispatch_result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "task_id": worker_task_id,
                    **_runtime_contract_bounded_dispatch_event(
                        runtime_context_id=runtime_context_id,
                        task_id=worker_task_id,
                        parent_task_id=bug_id,
                        read_receipt_event_id=recorded_read_receipt["id"],
                        startup_event_id=recorded_startup["id"],
                    ),
                    "route_waiver": self._route_waiver_for_existing_identity(
                        bug_id,
                        task_id=worker_task_id,
                    ),
                },
                method="POST",
            )
        )

        self.assertEqual(dispatch_result["route_token_gate"]["decision"], "route_waiver")
        self.assertEqual(
            dispatch_result["event_kind"],
            "bounded_implementation_worker_dispatch",
        )
        dispatch_payload = dispatch_result["payload"][
            "bounded_implementation_worker_dispatch"
        ]
        self.assertEqual(dispatch_payload["runtime_context_id"], runtime_context_id)
        self.assertEqual(
            dispatch_payload["read_receipt_event_id"],
            recorded_read_receipt["id"],
        )
        self.assertEqual(dispatch_payload["startup_event_id"], recorded_startup["id"])
        self.assertNotIn("raw_private_route_context", json.dumps(dispatch_payload))

        for event in [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
            _route_context_qa_verification_event(),
        ]:
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id=bug_id,
                task_id=event.get("task_id", ""),
                event_type=event.get("event_type") or event.get("event_kind"),
                phase=event.get("phase", ""),
                event_kind=event.get("event_kind", ""),
                status=event.get("status", ""),
                payload=event.get("payload") or {},
                verification=event.get("verification") or {},
            )
        self.conn.commit()

        events = task_timeline.list_events(self.conn, "proj", backlog_id=bug_id)
        ready = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertTrue(ready["route_context_gate"]["passed"], ready)
        self.assertNotIn(
            "bounded_implementation_worker_dispatch",
            ready["route_context_gate"]["missing_requirement_ids"],
        )
        self.assertEqual(
            ready["route_context_gate"]["attempt_lineage"]["lineage"][
                "runtime_context_id"
            ],
            runtime_context_id,
        )
        self.assertTrue(
            ready["contract_projection"]["read_receipt_gate"][
                "read_receipt_precedes_counted_evidence"
            ],
            ready,
        )

    def test_bounded_worker_dispatch_rejects_runtime_contract_lineage_mismatch(self):
        from agent.governance import server, task_timeline
        from agent.governance.errors import GovernanceError

        bug_id = "BUG-TL-BOUNDED-DISPATCH-RUNTIME-MISMATCH"
        worker_task_id = "worker-task-dogfood"
        runtime_context_id = "mfrctx-dogfood"
        self._insert_router_backlog(bug_id)
        self._record_route_service_context(bug_id)

        read_receipt = _runtime_contract_read_receipt_event(
            runtime_context_id=runtime_context_id,
            task_id=worker_task_id,
            parent_task_id=bug_id,
        )
        startup_event = _runtime_contract_startup_event(
            runtime_context_id=runtime_context_id,
            task_id=worker_task_id,
            parent_task_id=bug_id,
        )
        recorded_read_receipt = task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id=bug_id,
            task_id=worker_task_id,
            event_type=read_receipt["event_type"],
            phase=read_receipt["phase"],
            event_kind=read_receipt["event_kind"],
            status=read_receipt["status"],
            payload=read_receipt["payload"],
        )
        recorded_startup = task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id=bug_id,
            task_id=worker_task_id,
            event_type=startup_event["event_type"],
            phase=startup_event["phase"],
            event_kind=startup_event["event_kind"],
            status=startup_event["status"],
            payload=startup_event["payload"],
        )
        self.conn.commit()

        with self.assertRaises(GovernanceError) as raised:
            server.handle_task_timeline_append(
                _ctx(
                    body={
                        "backlog_id": bug_id,
                        "task_id": worker_task_id,
                        **_runtime_contract_bounded_dispatch_event(
                            runtime_context_id="mfrctx-wrong",
                            task_id=worker_task_id,
                            parent_task_id=bug_id,
                            read_receipt_event_id=recorded_read_receipt["id"],
                            startup_event_id=recorded_startup["id"],
                        ),
                        "route_waiver": self._route_waiver_for_existing_identity(
                            bug_id,
                            task_id=worker_task_id,
                        ),
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "route_token_required")
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ? AND event_kind = ?",
            (bug_id, "bounded_implementation_worker_dispatch"),
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_mf_parallel_route_waiver_rejects_dispatch_when_read_receipt_lineage_missing(self):
        from agent.governance import server, task_timeline
        from agent.governance.errors import GovernanceError

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-DISPATCH-NO-READ"
        worker_task_id = "worker-task-no-read"
        self._insert_router_backlog(bug_id)
        self._record_route_owned_source_lineage(bug_id, task_id=worker_task_id)
        startup_event = _route_context_worker_startup_event()
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id=bug_id,
            task_id=worker_task_id,
            event_type=startup_event["event_type"],
            phase=startup_event["phase"],
            event_kind=startup_event["event_kind"],
            status=startup_event["status"],
            payload=startup_event["payload"],
        )
        self.conn.commit()

        with self.assertRaises(GovernanceError) as raised:
            server.handle_task_timeline_append(
                _ctx(
                    body={
                        "backlog_id": bug_id,
                        "task_id": worker_task_id,
                        **_route_context_worker_dispatch_event(),
                        "route_waiver": self._route_waiver_for_existing_identity(
                            bug_id,
                            task_id=worker_task_id,
                        ),
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "route_token_required")
        self.assertIn("ordered read-receipt/startup lineage", str(raised.exception))
        self.assertIn(
            "mf_subagent_startup",
            raised.exception.details["present_requirement_ids"],
        )
        self.assertIn(
            "bounded_implementation_worker_dispatch",
            raised.exception.details["missing_requirement_ids"],
        )

    def test_mf_parallel_route_waiver_rejects_dispatch_when_waiver_identity_mismatches_lineage(self):
        from agent.governance import server, task_timeline
        from agent.governance.errors import GovernanceError

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-DISPATCH-WAIVER-MISMATCH"
        worker_task_id = "worker-task-waiver-mismatch"
        self._insert_router_backlog(bug_id)
        read_receipt = _mf_subagent_read_receipt_event(identity=ROUTE_IDENTITY)
        startup_event = _route_context_worker_startup_event()
        for event in (read_receipt, startup_event):
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id=bug_id,
                task_id=worker_task_id,
                event_type=event.get("event_type") or event.get("event_kind"),
                phase=event.get("phase", ""),
                event_kind=event.get("event_kind", ""),
                status=event.get("status", ""),
                payload=event.get("payload") or {},
                verification=event.get("verification") or {},
            )
        self.conn.commit()
        waiver = self._route_waiver_for_existing_identity(
            bug_id,
            task_id=worker_task_id,
        )
        waiver["route_context_hash"] = "sha256:mismatched-route-waiver"

        with self.assertRaises(GovernanceError) as raised:
            server.handle_task_timeline_append(
                _ctx(
                    body={
                        "backlog_id": bug_id,
                        "task_id": worker_task_id,
                        **_route_context_worker_dispatch_event(),
                        "route_waiver": waiver,
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "route_token_required")
        self.assertEqual(
            raised.exception.details["route_identity_mismatch_fields"],
            ["route_context_hash"],
        )
        self.assertIn("route identity does not match", str(raised.exception))
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ? AND event_kind = ?",
            (bug_id, "mf_subagent_dispatch"),
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_mf_parallel_task_scoped_route_waiver_bootstraps_qa_lane_from_backlog_route_context(self):
        from agent.governance import server, task_timeline

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-TASK-QA-BOOTSTRAP"
        worker_task_id = "worker-task-qa-bootstrap"
        contract = self._insert_router_backlog(bug_id)
        self._record_route_service_context(bug_id)
        for event in (
            _route_context_worker_dispatch_event(),
            _route_context_worker_startup_event(),
        ):
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id=bug_id,
                task_id=worker_task_id,
                event_type=event["event_type"],
                phase=event["phase"],
                event_kind=event["event_kind"],
                status=event["status"],
                payload=event["payload"],
            )
        self.conn.commit()

        qa_event = _route_context_qa_verification_event()
        qa_result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "task_id": worker_task_id,
                    "event_type": "independent_verification.completed",
                    "event_kind": qa_event["event_kind"],
                    "phase": qa_event["phase"],
                    "status": qa_event["status"],
                    "verification": qa_event["verification"],
                    "route_waiver": self._route_waiver_for_existing_identity(
                        bug_id,
                        task_id=worker_task_id,
                    ),
                },
                method="POST",
            )
        )

        self.assertEqual(qa_result["route_token_gate"]["decision"], "route_waiver")
        self.assertEqual(qa_result["event_kind"], "qa_verification")
        gate = task_timeline.mf_route_context_gate_verification(
            task_timeline.list_events(self.conn, "proj", backlog_id=bug_id),
            contract=contract,
        )
        self.assertTrue(gate["passed"], gate)
        self.assertIn("independent_verification_lane", gate["present_requirement_ids"])

    def test_mf_parallel_route_waiver_bootstrap_rejects_event_identity_mismatch(self):
        from agent.governance import server
        from agent.governance.errors import GovernanceError

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-DISPATCH-MISMATCH"
        self._insert_router_backlog(bug_id)
        self._record_route_service_context(bug_id)
        wrong_identity = dict(ROUTE_IDENTITY)
        wrong_identity["route_context_hash"] = "sha256:mismatched-route-context"
        event = _route_context_worker_dispatch_event(wrong_identity)

        with self.assertRaises(GovernanceError) as raised:
            server.handle_task_timeline_append(
                _ctx(
                    body={
                        "backlog_id": bug_id,
                        **event,
                        "route_waiver": self._route_waiver_for_existing_identity(bug_id),
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "route_token_required")
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ? AND event_kind = ?",
            (bug_id, "mf_subagent_dispatch"),
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_mf_parallel_waiver_evidence_does_not_satisfy_close_precheck(self):
        from agent.governance import server

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-EVIDENCE"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": bug_id,
        }
        self.conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, chain_trigger_json, mf_type, created_at, updated_at)
               VALUES (?, ?, 'OPEN', 'P0', ?, 'chain_rescue', '2026-05-29T00:00:00Z', '2026-05-29T00:00:00Z')""",
            (bug_id, "High-risk waiver evidence test", json.dumps(contract)),
        )
        self.conn.commit()

        waiver_result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "event_type": "route.waiver.recorded",
                    "event_kind": "route_waiver",
                    "phase": "route_gate",
                    "status": "accepted",
                    "route_waiver": _route_waiver(
                        "task_timeline_append",
                        bug_id,
                    ),
                },
                method="POST",
            )
        )
        self.assertEqual(waiver_result["event_kind"], "route_waiver")
        self.assertIn("route_waiver", waiver_result["payload"])

        precheck = server.handle_backlog_timeline_gate(
            _ctx(
                path_params={"project_id": "proj", "bug_id": bug_id},
                query={"include_events": "true"},
            )
        )

        self.assertFalse(precheck["can_close"], precheck)
        self.assertIn(
            "bounded_implementation_worker_dispatch",
            precheck["timeline_gate"]["route_context_gate"]["missing_requirement_ids"],
        )
        self.assertIn(
            "mf_subagent_startup",
            precheck["timeline_gate"]["route_context_gate"]["missing_requirement_ids"],
        )

    def test_mf_parallel_timeline_accepts_valid_route_token_for_protected_evidence(self):
        from agent.governance import server

        self._insert_router_backlog("BUG-TL-MF-PARALLEL-TOKEN")
        issued = self._issue_route_token("BUG-TL-MF-PARALLEL-TOKEN")
        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": "BUG-TL-MF-PARALLEL-TOKEN",
                    "event_type": "mf.implementation",
                    "event_kind": "implementation",
                    "status": "accepted",
                    "route_token": issued["route_token"],
                },
                method="POST",
            )
        )

        self.assertEqual(result["route_token_gate"]["decision"], "route_token")
        self.assertTrue(result["route_token_gate"]["server_issued_binding"])
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
            ("BUG-TL-MF-PARALLEL-TOKEN",),
        ).fetchone()["c"]
        self.assertEqual(count, 2)

    def test_mf_parallel_timeline_accepts_route_token_for_bounded_dispatch_lane(self):
        from agent.governance import server

        bug_id = "BUG-TL-MF-PARALLEL-TOKEN-DISPATCH"
        worker_task_id = "worker-token-dispatch"
        self._insert_router_backlog(bug_id)
        issued = self._issue_route_token(bug_id)

        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "task_id": worker_task_id,
                    **_runtime_contract_bounded_dispatch_event(
                        task_id=worker_task_id,
                        parent_task_id=bug_id,
                    ),
                    "route_token": issued["route_token"],
                },
                method="POST",
            )
        )

        self.assertEqual(result["route_token_gate"]["decision"], "route_token")
        self.assertTrue(result["route_token_gate"]["server_issued_binding"])
        self.assertEqual(result["route_token_gate"]["scope"]["task_id"], bug_id)
        self.assertEqual(
            result["event_kind"],
            "bounded_implementation_worker_dispatch",
        )
        self.assertNotIn("route_token", result["payload"])
        self.assertIn("route_token_gate", result["payload"])

    def test_mf_parallel_timeline_accepts_source_event_for_bounded_dispatch_lane_without_token(self):
        from agent.governance import server

        bug_id = "BUG-TL-MF-PARALLEL-SOURCE-DISPATCH"
        worker_task_id = "worker-source-dispatch"
        self._insert_router_backlog(bug_id)
        self._record_route_owned_source_lineage(bug_id, task_id=worker_task_id)

        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "task_id": worker_task_id,
                    **_runtime_contract_bounded_dispatch_event(
                        task_id=worker_task_id,
                        parent_task_id=bug_id,
                    ),
                },
                method="POST",
            )
        )

        self.assertEqual(
            result["route_token_gate"]["decision"],
            "route_owned_source_event",
        )
        self.assertEqual(
            result["route_token_gate"]["protected_lane"],
            "bounded_implementation_worker_dispatch",
        )
        self.assertTrue(result["route_token_gate"]["source_event_refs"])

    def test_mf_parallel_timeline_accepts_source_event_for_independent_verification_without_token(self):
        from agent.governance import server

        bug_id = "BUG-TL-MF-PARALLEL-SOURCE-QA"
        self._insert_router_backlog(bug_id)
        self._record_route_owned_source_lineage(bug_id)
        qa_event = _route_context_qa_verification_event()

        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "event_type": "independent_verification.completed",
                    "event_kind": qa_event["event_kind"],
                    "phase": qa_event["phase"],
                    "status": qa_event["status"],
                    "verification": qa_event["verification"],
                },
                method="POST",
            )
        )

        self.assertEqual(
            result["route_token_gate"]["decision"],
            "route_owned_source_event",
        )
        self.assertEqual(
            result["route_token_gate"]["protected_lane"],
            "independent_verification_lane",
        )

    def test_mf_parallel_timeline_rejects_generic_waiver_without_source_event_lineage(self):
        from agent.governance import server
        from agent.governance.errors import GovernanceError

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-NO-SOURCE-LINEAGE"
        self._insert_router_backlog(bug_id)
        self._record_route_context_consumption(
            bug_id,
            include_source_lineage=False,
        )

        with self.assertRaises(GovernanceError) as raised:
            server.handle_task_timeline_append(
                _ctx(
                    body={
                        "backlog_id": bug_id,
                        "event_type": "mf.implementation",
                        "event_kind": "implementation",
                        "status": "accepted",
                        "route_waiver": self._route_waiver_for_existing_identity(bug_id),
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "route_token_required")
        self.assertTrue(raised.exception.details["source_event_lineage_required"])
        self.assertEqual(
            raised.exception.details["legal_next_action"],
            "record_or_reuse_an_accepted_route_owned_source_event_for_the_claimed_route_identity",
        )
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ? AND event_kind = 'implementation'",
            (bug_id,),
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_mf_parallel_timeline_accepts_matching_waiver_after_bounded_evidence(self):
        from agent.governance import server

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-AFTER-EVIDENCE"
        self._insert_router_backlog(bug_id)
        self._record_route_context_consumption(bug_id)

        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "event_type": "mf.implementation",
                    "event_kind": "implementation",
                    "status": "accepted",
                    "route_waiver": self._route_waiver_for_existing_identity(bug_id),
                },
                method="POST",
            )
        )

        self.assertEqual(result["route_token_gate"]["decision"], "route_waiver")
        self.assertEqual(result["event_kind"], "implementation")
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ? AND event_kind = ?",
            (bug_id, "implementation"),
        ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_mf_parallel_timeline_accepts_task_scoped_waiver_after_backlog_evidence(self):
        from agent.governance import server

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-TASK-SCOPED"
        worker_task_id = "worker-task-scoped"
        self._insert_router_backlog(bug_id)
        self._record_route_context_consumption(bug_id)

        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "task_id": worker_task_id,
                    "event_type": "mf.implementation",
                    "event_kind": "implementation",
                    "status": "accepted",
                    "route_waiver": self._route_waiver_for_existing_identity(
                        bug_id,
                        task_id=worker_task_id,
                    ),
                },
                method="POST",
            )
        )

        self.assertEqual(result["route_token_gate"]["decision"], "route_waiver")
        self.assertEqual(result["event_kind"], "implementation")
        self.assertEqual(result["task_id"], worker_task_id)

    def test_mf_parallel_task_scoped_backlog_fallback_keeps_identity_mismatch_blocked(self):
        from agent.governance import server
        from agent.governance.errors import GovernanceError

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-TASK-MISMATCH"
        worker_task_id = "worker-task-mismatch"
        self._insert_router_backlog(bug_id)
        self._record_route_context_consumption(bug_id)
        waiver = self._route_waiver_for_existing_identity(
            bug_id,
            task_id=worker_task_id,
        )
        waiver["route_context_hash"] = "sha256:mismatched-route-context"

        with self.assertRaises(GovernanceError) as raised:
            server.handle_task_timeline_append(
                _ctx(
                    body={
                        "backlog_id": bug_id,
                        "task_id": worker_task_id,
                        "event_type": "mf.implementation",
                        "event_kind": "implementation",
                        "status": "accepted",
                        "route_waiver": waiver,
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "route_token_required")
        self.assertEqual(
            raised.exception.details["route_identity_mismatch_fields"],
            ["route_context_hash"],
        )

    def test_mf_parallel_timeline_allows_qa_waiver_to_break_independent_verification_cycle(self):
        from agent.governance import server, task_timeline
        from agent.governance.errors import GovernanceError

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-QA-CYCLE"
        self._insert_router_backlog(bug_id)
        self._record_route_owned_source_lineage(bug_id)
        for event in _route_context_consumption_events():
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id=bug_id,
                event_type=event.get("event_type") or event.get("event_kind"),
                phase=event.get("phase", ""),
                event_kind=event.get("event_kind", ""),
                status=event.get("status", ""),
                payload=event.get("payload") or {},
                verification=event.get("verification") or {},
            )
        self.conn.commit()
        waiver = self._route_waiver_for_existing_identity(bug_id)

        with self.assertRaises(GovernanceError) as blocked:
            server.handle_task_timeline_append(
                _ctx(
                    body={
                        "backlog_id": bug_id,
                        "event_type": "mf.implementation",
                        "event_kind": "implementation",
                        "status": "accepted",
                        "route_waiver": waiver,
                    },
                    method="POST",
                )
            )
        self.assertEqual(blocked.exception.code, "route_token_required")
        self.assertEqual(
            blocked.exception.details["missing_requirement_ids"],
            ["independent_verification_lane"],
        )

        qa_result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "event_type": "independent_verification.completed",
                    "event_kind": "qa_verification",
                    "phase": "verification",
                    "status": "passed",
                    "verification": {
                        **ROUTE_IDENTITY,
                        "contract_evidence": [
                            {
                                "requirement_id": "independent_verification_lane",
                                "status": "passed",
                                "reviewer_role": "qa",
                            }
                        ],
                    },
                    "route_waiver": waiver,
                },
                method="POST",
            )
        )

        self.assertEqual(qa_result["route_token_gate"]["decision"], "route_waiver")
        self.assertEqual(qa_result["event_kind"], "qa_verification")

        implementation_result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "event_type": "mf.implementation",
                    "event_kind": "implementation",
                    "status": "accepted",
                    "route_waiver": waiver,
                },
                method="POST",
            )
        )

        self.assertEqual(
            implementation_result["route_token_gate"]["decision"],
            "route_waiver",
        )
        self.assertEqual(implementation_result["event_kind"], "implementation")

    def test_mf_parallel_timeline_rejects_waiver_identity_mismatch_after_bounded_evidence(self):
        from agent.governance import server
        from agent.governance.errors import GovernanceError

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-MISMATCH"
        self._insert_router_backlog(bug_id)
        self._record_route_context_consumption(bug_id)
        waiver = self._route_waiver_for_existing_identity(bug_id)
        waiver["route_context_hash"] = "sha256:mismatched-route-context"

        with self.assertRaises(GovernanceError) as raised:
            server.handle_task_timeline_append(
                _ctx(
                    body={
                        "backlog_id": bug_id,
                        "event_type": "mf.implementation",
                        "event_kind": "implementation",
                        "status": "accepted",
                        "route_waiver": waiver,
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "route_token_required")
        self.assertEqual(
            raised.exception.details["route_identity_mismatch_fields"],
            ["route_context_hash"],
        )
        self.assertEqual(
            raised.exception.details["route_identity_recovery"]["classification"],
            "stale_or_mixed_route_evidence",
        )
        self.assertEqual(
            raised.exception.details["route_identity_recovery"]["cleanup_event_kind"],
            "route_identity_cleanup",
        )
        self.assertIn(
            "supersede",
            raised.exception.details["route_identity_recovery"]["guidance"],
        )
        self.assertIn(
            "do not delete audit history",
            raised.exception.details["route_identity_recovery"]["guidance"],
        )
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ? AND event_kind = ?",
            (bug_id, "implementation"),
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_task_claim_and_complete_write_verified_timeline(self):
        from agent.governance import task_timeline
        from agent.governance.task_registry import claim_task, complete_task, create_task

        task = create_task(
            self.conn,
            "proj",
            "implement evidence",
            task_type="dev",
            metadata={"bug_id": "BUG-TL", "mf_id": "MF-TL", "trace_id": "tr-tl"},
        )
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "worker-1", caller_pid=1234)
        self.conn.commit()
        self.assertEqual(claimed["task_id"], task["task_id"])

        result = {
            "changed_files": ["agent/example.py"],
            "implementation_evidence": [
                {
                    "file": "agent/example.py",
                    "symbols": ["do_work"],
                    "change_intent": "add observable evidence",
                }
            ],
            "self_check": {
                "ready_for_gate": True,
                "tests_run": ["pytest -q agent/tests/test_task_timeline.py"],
            },
            "_artifacts": {"output_path": "shared-volume/codex-tasks/logs/output.txt"},
        }

        with mock.patch("agent.governance.auto_chain.on_task_completed", return_value=None):
            complete_task(
                self.conn,
                task["task_id"],
                status="succeeded",
                result=result,
                project_id="proj",
                completed_by="worker-1",
                fence_token=fence,
            )
        self.conn.commit()

        events = task_timeline.list_events(self.conn, "proj", task_id=task["task_id"])
        event_types = [event["event_type"] for event in events]
        self.assertIn("task.claimed", event_types)
        self.assertIn("gate.evidence.verified", event_types)
        self.assertIn("task.completed", event_types)

        gate_event = next(event for event in events if event["event_type"] == "gate.evidence.verified")
        self.assertEqual(gate_event["status"], "passed")
        self.assertTrue(gate_event["verification"]["passed"])
        self.assertEqual(gate_event["backlog_id"], "BUG-TL")

    def test_list_events_filters_by_backlog_id_without_task_id(self):
        from agent.governance import task_timeline

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-a",
            backlog_id="BUG-A",
            event_type="task.started",
            actor="worker-a",
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-b",
            backlog_id="BUG-A",
            event_type="task.completed",
            actor="worker-b",
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-c",
            backlog_id="BUG-B",
            event_type="task.completed",
            actor="worker-c",
        )

        events = task_timeline.list_events(self.conn, "proj", backlog_id="BUG-A")

        self.assertEqual([event["task_id"] for event in events], ["task-a", "task-b"])
        self.assertEqual({event["backlog_id"] for event in events}, {"BUG-A"})

    def test_task_completed_timeline_event_without_route_token_records_blocked_services(self):
        from agent.governance import task_timeline

        self._insert_router_backlog()

        source = task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-router",
            backlog_id="BUG-SERVICE-ROUTER",
            event_type="task.completed",
            actor="worker",
            status="succeeded",
            payload={"task_type": "dev"},
        )
        self.conn.commit()

        routed = task_timeline.list_events(
            self.conn,
            "proj",
            parent_event_id=source["id"],
            event_kind="service_route",
        )

        self.assertGreaterEqual(len(routed), 2)
        self.assertEqual({event["parent_event_id"] for event in routed}, {source["id"]})
        self.assertTrue(all(event["correlation_id"].startswith("service-route:") for event in routed))
        self.assertIn(
            "test_governance.preview",
            {event["payload"]["service_id"] for event in routed},
        )
        self.assertIn(
            "review.recommendations",
            {event["payload"]["service_id"] for event in routed},
        )
        self.assertEqual({event["event_type"] for event in routed}, {"service.route.blocked"})
        self.assertEqual(
            {event["payload"]["status"] for event in routed},
            {"route_context_token_required"},
        )

    def test_task_completed_timeline_event_with_bound_route_token_records_allowed_service(self):
        from agent.governance import task_timeline

        bug_id = "BUG-SERVICE-ROUTER-BOUND-TOKEN"
        task_id = "task-router-bound-token"
        self._insert_router_backlog(
            bug_id=bug_id,
            contract={
                "parallel_contract": {
                    "contract_instance_id": bug_id,
                    "service_routes": [
                        {
                            "route_id": "service.test_governance.preview",
                            "service_id": "test_governance.preview",
                            "mode": "preview",
                            "side_effect_class": "read",
                        }
                    ],
                    "event_routes": [
                        {
                            "route_id": "event.task_completed.preview",
                            "event_kind": "task.completed",
                            "service_route_id": "service.test_governance.preview",
                            "enabled": True,
                        }
                    ],
                }
            },
        )
        issued = self._issue_route_token(
            bug_id,
            task_id=task_id,
            allowed_actions=["service_route"],
        )

        source = task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id=task_id,
            backlog_id=bug_id,
            event_type="task.completed",
            actor="worker",
            status="succeeded",
            payload={"route_token": issued["route_token"]},
        )
        self.conn.commit()

        routed = task_timeline.list_events(
            self.conn,
            "proj",
            parent_event_id=source["id"],
            event_kind="service_route",
        )

        self.assertEqual(len(routed), 1)
        self.assertEqual(routed[0]["event_type"], "service.route.completed")
        payload = routed[0]["payload"]
        gate = payload["result"]["route_context_gate"]
        self.assertEqual(payload["status"], "allowed")
        self.assertEqual(gate["decision"], "route_token")
        self.assertTrue(gate["server_issued_binding"])
        self.assertEqual(gate["route_token_ref"], issued["route_token_ref"])

    def test_timeline_append_redacts_top_level_route_token_payload(self):
        from agent.governance import server, task_timeline

        bug_id = "BUG-SERVICE-ROUTER-TOKEN"
        task_id = "task-router-token"
        self._insert_router_backlog(bug_id=bug_id)

        result = server.handle_task_timeline_append(
            _ctx(
                method="POST",
                body={
                    "task_id": task_id,
                    "backlog_id": bug_id,
                    "event_type": "task.completed",
                    "actor": "worker",
                    "status": "succeeded",
                    "payload": {"task_type": "dev"},
                    "route_token": _route_token(
                        "service_route",
                        bug_id=bug_id,
                        task_id=task_id,
                    ),
                },
            )
        )
        self.conn.commit()

        source = task_timeline.list_events(
            self.conn,
            "proj",
            task_id=task_id,
            backlog_id=bug_id,
        )[0]

        self.assertNotIn("route_token", source["payload"])
        self.assertIn("route_token_gate", source["payload"])
        gate = source["payload"]["route_token_gate"]
        self.assertEqual(gate["decision"], "route_token_input_redacted")
        self.assertEqual(
            gate["route_context_hash"],
            _fake_sha("test-route-context-service_route"),
        )
        self.assertTrue(gate["route_token_hash"].startswith("sha256:"))
        self.assertNotIn("expires_at", source["payload"].get("route_token_gate", {}))

    def test_project_bootstrap_with_route_handoff_requires_bound_route_token(self):
        from agent.governance import server
        from agent.governance.errors import GovernanceError

        workspace_path = os.path.join(self.tmp.name, "bootstrap-project")
        os.makedirs(workspace_path, exist_ok=True)
        route_identity = {
            **ROUTE_IDENTITY,
            "route_id": "route-bootstrap-test",
            "visible_injection_manifest_hash": "sha256:bootstrap-visible",
            "raw_prompt": "do-not-persist",
        }

        with mock.patch.object(
            server.project_service,
            "bootstrap_project",
            return_value={"project_id": "bootstrap-project", "graph_stats": {}},
        ) as bootstrap_project:
            with self.assertRaises(GovernanceError) as raised:
                server.handle_project_bootstrap(
                    _ctx(
                        method="POST",
                        body={
                            "workspace_path": workspace_path,
                            "project_id": "bootstrap-project",
                            "route_identity": route_identity,
                        },
                    )
                )

        self.assertEqual(raised.exception.code, "route_token_required")
        bootstrap_project.assert_not_called()

    def test_observer_repair_route_evidence_records_service_route_gate_inputs(self):
        from agent.governance import server, task_timeline

        bug_id = "BUG-OBSERVER-ROUTE-EVIDENCE"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": bug_id,
        }
        self.conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, target_files, test_files,
                acceptance_criteria, chain_trigger_json, mf_type, bypass_policy_json,
                created_at, updated_at)
               VALUES (?, ?, 'MF_IN_PROGRESS', 'P0', ?, ?, ?, ?, 'chain_rescue', ?,
                       '2026-06-02T00:00:00Z', '2026-06-02T00:00:00Z')""",
            (
                bug_id,
                "Observer repair route evidence",
                json.dumps(["agent/governance/observer_repair_run.py"]),
                json.dumps(["agent/tests/test_observer_repair_run.py"]),
                json.dumps(["record route service evidence only"]),
                json.dumps(contract),
                json.dumps({"mf_type": "chain_rescue"}),
            ),
        )
        self.conn.commit()

        dry_run = server.handle_observer_repair_run_route_evidence(
            _ctx(
                method="POST",
                body={
                    "root_backlog_ids": [bug_id],
                    "actor": "observer-test",
                    "version_check": {"ok": True, "dirty": False, "dirty_files": []},
                },
            )
        )

        self.assertFalse(dry_run["record"])
        self.assertTrue(dry_run["recordable"])
        self.assertEqual(
            [event["event_type"] for event in dry_run["source_events"]],
            ["route.prompt_context.requested", "route.action.requested"],
        )

        result = server.handle_observer_repair_run_route_evidence(
            _ctx(
                method="POST",
                body={
                    "root_backlog_ids": [bug_id],
                    "actor": "observer-test",
                    "record": True,
                    "version_check": {"ok": True, "dirty": False, "dirty_files": []},
                },
            )
        )

        self.assertTrue(result["recorded"])
        self.assertEqual(len(result["recorded_source_event_ids"]), 2)
        self.assertEqual(
            {event["payload"]["service_id"] for event in result["recorded_service_events"]},
            {"route.prompt_alert_bundle", "route.action_precheck"},
        )
        self.assertEqual(
            {event["event_type"] for event in result["recorded_service_events"]},
            {"service.route.completed"},
        )

        replay = server.handle_observer_repair_run_route_evidence(
            _ctx(
                method="POST",
                body={
                    "root_backlog_ids": [bug_id],
                    "actor": "observer-test",
                    "record": True,
                    "version_check": {"ok": True, "dirty": False, "dirty_files": []},
                },
            )
        )

        self.assertEqual(
            replay["reused_source_event_ids"],
            result["recorded_source_event_ids"],
        )
        self.assertEqual(
            replay["recorded_source_event_ids"],
            result["recorded_source_event_ids"],
        )

        events = task_timeline.list_events(self.conn, "proj", backlog_id=bug_id, limit=100)
        route_gate = task_timeline.mf_route_context_gate_verification(
            events,
            contract=contract,
        )

        self.assertEqual(
            route_gate["present_requirement_ids"],
            ["route_context", "route_action_precheck"],
        )
        self.assertEqual(
            route_gate["missing_requirement_ids"],
            [
                "bounded_implementation_worker_dispatch",
                "mf_subagent_startup",
                "independent_verification_lane",
            ],
        )

        close_gate = task_timeline.mf_close_gate_verification(events, contract=contract)
        self.assertFalse(close_gate["passed"], close_gate)
        self.assertEqual(
            close_gate["missing_event_kinds"],
            ["close_ready", "implementation", "verification"],
        )

    def test_observer_repair_route_evidence_records_external_action_precheck(self):
        from agent.governance import server, task_timeline

        bug_id = "BUG-OBSERVER-EXTERNAL-ROUTE-PRECHECK"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": bug_id,
        }
        identity = {
            **ROUTE_IDENTITY,
            "visible_injection_manifest_hash": "sha256:external-visible-manifest",
        }
        self.conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, target_files, test_files,
                acceptance_criteria, chain_trigger_json, mf_type, bypass_policy_json,
                created_at, updated_at)
               VALUES (?, ?, 'MF_IN_PROGRESS', 'P0', ?, ?, ?, ?, 'chain_rescue', ?,
                       '2026-06-03T00:00:00Z', '2026-06-03T00:00:00Z')""",
            (
                bug_id,
                "Observer external route action precheck",
                json.dumps(["agent/governance/observer_repair_run.py"]),
                json.dumps(["agent/tests/test_observer_repair_run.py"]),
                json.dumps(["record external route action precheck"]),
                json.dumps(contract),
                json.dumps({"mf_type": "chain_rescue"}),
            ),
        )
        self.conn.commit()

        dry_run = server.handle_observer_repair_run_route_evidence(
            _ctx(
                method="POST",
                body={
                    "root_backlog_ids": [bug_id],
                    "actor": "observer-test",
                    "action_precheck_id": "external-dispatch-precheck",
                    "route_identity": identity,
                    "action_precheck": {
                        **identity,
                        "caller_role": "observer",
                        "action": "dispatch_bounded_worker",
                        "allowed": True,
                        "private_provider_body": "do-not-leak-provider-body",
                        "raw_prompt": "do-not-leak-raw-prompt",
                    },
                    "version_check": {"ok": True, "dirty": False, "dirty_files": []},
                },
            )
        )

        self.assertFalse(dry_run["record"])
        self.assertTrue(dry_run["recordable"], dry_run)
        self.assertTrue(dry_run["route_action_precheck"]["present"])
        self.assertTrue(dry_run["route_action_precheck"]["valid"])
        self.assertTrue(dry_run["authorizes_protected_worker_dispatch_evidence"])
        self.assertFalse(dry_run["authorizes_protected_write"])
        dry_run_json = json.dumps(dry_run, sort_keys=True)
        self.assertNotIn("do-not-leak-provider-body", dry_run_json)
        self.assertNotIn("do-not-leak-raw-prompt", dry_run_json)

        result = server.handle_observer_repair_run_route_evidence(
            _ctx(
                method="POST",
                body={
                    "root_backlog_ids": [bug_id],
                    "actor": "observer-test",
                    "record": True,
                    "action_precheck_id": "external-dispatch-precheck",
                    "route_identity": identity,
                    "action_precheck": {
                        **identity,
                        "caller_role": "observer",
                        "action": "dispatch_bounded_worker",
                        "allowed": True,
                    },
                    "version_check": {"ok": True, "dirty": False, "dirty_files": []},
                },
            )
        )

        self.assertTrue(result["recorded"])
        self.assertEqual(len(result["recorded_source_event_ids"]), 1)
        self.assertEqual(
            {event["payload"]["service_id"] for event in result["recorded_service_events"]},
            {"route.action_precheck"},
        )

        events = task_timeline.list_events(self.conn, "proj", backlog_id=bug_id, limit=100)
        route_gate = task_timeline.mf_route_context_gate_verification(
            events,
            contract=contract,
        )

        self.assertEqual(
            route_gate["present_requirement_ids"],
            ["route_context", "route_action_precheck"],
        )
        self.assertNotIn("route_identity_mismatch", route_gate["missing_requirement_ids"])
        self.assertEqual(
            route_gate["route_identity"],
            {
                "route_context_hash": ROUTE_IDENTITY["route_context_hash"],
                "prompt_contract_id": ROUTE_IDENTITY["prompt_contract_id"],
                "prompt_contract_hash": ROUTE_IDENTITY["prompt_contract_hash"],
            },
        )

    def test_observer_repair_route_evidence_consumes_command_identity_from_seed(self):
        from agent.governance import server

        bug_id = "BUG-OBSERVER-COMMAND-ROUTE-CONSUMPTION"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": bug_id,
        }
        identity = {
            **ROUTE_IDENTITY,
            "route_id": "route-command-seed-consumed",
            "visible_injection_manifest_hash": "sha256:command-visible-manifest",
        }
        self.conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, target_files, test_files,
                acceptance_criteria, chain_trigger_json, mf_type, bypass_policy_json,
                created_at, updated_at)
               VALUES (?, ?, 'MF_IN_PROGRESS', 'P0', ?, ?, ?, ?, 'chain_rescue', ?,
                       '2026-06-03T00:00:00Z', '2026-06-03T00:00:00Z')""",
            (
                bug_id,
                "Observer command route consumption",
                json.dumps(["agent/governance/observer_repair_run.py"]),
                json.dumps(["agent/tests/test_observer_repair_run.py"]),
                json.dumps(["consume command route identity from seed"]),
                json.dumps(contract),
                json.dumps({"mf_type": "chain_rescue"}),
            ),
        )
        self.conn.commit()

        dry_run = server.handle_observer_repair_run_route_evidence(
            _ctx(
                method="POST",
                body={
                    "root_backlog_ids": [bug_id],
                    "actor": "observer-test",
                    "route_context_seed": {
                        "route_identity": identity,
                        "action_precheck": {
                            **identity,
                            "caller_role": "observer",
                            "action": "dispatch_bounded_worker",
                            "allowed": True,
                        },
                    },
                    "version_check": {"ok": True, "dirty": False, "dirty_files": []},
                },
            )
        )

        self.assertFalse(dry_run["record"])
        self.assertTrue(dry_run["recordable"], dry_run)
        self.assertEqual(
            dry_run["route_identity_consumption"]["consumed_route_identity"],
            identity,
        )
        self.assertTrue(dry_run["route_identity_consumption"]["consumed"])
        self.assertFalse(dry_run["route_identity_consumption"]["superseded"])
        self.assertEqual(dry_run["route_action_precheck"]["route_identity"], identity)
        self.assertNotIn("route_identity_supersession", dry_run)

    def test_ai_validated_timeline_route_persists_contract_evidence(self):
        from agent.governance import task_timeline

        bug_id = "BUG-AI-ROUTE-EVIDENCE"
        route_requirement = "ai_output_validated"
        self._insert_router_backlog(
            bug_id=bug_id,
            contract={
                "parallel_contract": {
                    "contract_instance_id": bug_id,
                    "service_routes": [
                        {
                            "route_id": "service.test_governance.preview",
                            "service_id": "test_governance.preview",
                            "mode": "preview",
                            "side_effect_class": "read",
                            "requirement_ids": ["service_route_checked"],
                        }
                    ],
                    "event_routes": [
                        {
                            "route_id": "event.ai_structured_output.validated",
                            "event_kind": "ai.structured_output.validated",
                            "service_route_id": "service.test_governance.preview",
                            "required_evidence_ids": [route_requirement],
                            "enabled": True,
                        }
                    ],
                }
            },
        )

        source = task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-ai-route",
            backlog_id=bug_id,
            event_type="ai.structured_output.validated",
            actor="ai-fixture",
            status="passed",
            payload={
                "producer": "fixture",
                "validated": True,
                "route_token_gate": _bound_route_token_gate(
                    "service_route",
                    bug_id=bug_id,
                    task_id="task-ai-route",
                ),
                "route_waiver": _route_waiver(
                    "service_route",
                    bug_id=bug_id,
                    task_id="task-ai-route",
                ),
            },
        )
        self.conn.commit()

        routed = task_timeline.list_events(
            self.conn,
            "proj",
            parent_event_id=source["id"],
            event_kind="service_route",
        )

        self.assertEqual(len(routed), 1)
        payload = routed[0]["payload"]
        self.assertEqual(payload["route_id"], "event.ai_structured_output.validated")
        self.assertEqual(payload["requirement_ids"], ["service_route_checked", route_requirement])
        self.assertEqual(
            [item["requirement_id"] for item in payload["contract_evidence"]],
            ["service_route_checked", route_requirement],
        )
        self.assertEqual(
            routed[0]["verification"]["contract_evidence"],
            payload["contract_evidence"],
        )

    def test_observer_reminder_echo_timeline_route_persists_safe_echo(self):
        from agent.governance import task_timeline

        bug_id = "BUG-REMINDER-ECHO"
        self._insert_router_backlog(
            bug_id=bug_id,
            contract={
                "parallel_contract": {
                    "template_id": "observer_reminder_echo_demo.v1",
                    "contract_instance_id": bug_id,
                }
            },
        )

        source = task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-reminder-echo",
            backlog_id=bug_id,
            event_type="observer.command.notified",
            actor="observer-fixture",
            status="notified",
            payload={
                "hook_reminder": {
                    "kind": "observer_command_pending",
                    "project_id": "proj",
                    "message": "pending observer commands exist; call observer_command_next",
                    "payload_included": False,
                    "next_action": {
                        "tool": "observer_command_next",
                        "description": "claim the next pending observer command",
                        "source": "nested-business-field",
                    },
                    "raw_id": "raw-1",
                    "source": "dashboard",
                    "command_type": "analyze_requirements",
                    "command_id": "cmd-1",
                },
                "route_token_gate": _bound_route_token_gate(
                    "service_route",
                    bug_id=bug_id,
                    task_id="task-reminder-echo",
                ),
                "route_waiver": _route_waiver(
                    "service_route",
                    bug_id=bug_id,
                    task_id="task-reminder-echo",
                ),
            },
        )
        self.conn.commit()

        routed = task_timeline.list_events(
            self.conn,
            "proj",
            parent_event_id=source["id"],
            event_kind="service_route",
        )

        self.assertEqual(len(routed), 1)
        payload = routed[0]["payload"]
        result = payload["result"]
        received_reminder = result["received_reminder"]
        echo = result["received_reminder_echo"]
        self.assertEqual(routed[0]["event_type"], "service.route.completed")
        self.assertEqual(payload["service_id"], "observer.reminder_echo")
        self.assertEqual(payload["route_id"], "event.observer_command_notified.reminder_echo")
        self.assertEqual(
            payload["requirement_ids"],
            [
                "observer_reminder_visible",
                "payload_boundary_preserved",
                "received_reminder_echo",
            ],
        )
        self.assertEqual(
            echo,
            {
                "kind": "observer_command_pending",
                "project_id": "proj",
                "message": "pending observer commands exist; call observer_command_next",
                "payload_included": False,
                "next_action": {
                    "tool": "observer_command_next",
                    "description": "claim the next pending observer command",
                },
            },
        )
        self.assertEqual(received_reminder, echo)
        result_json = json.dumps(result, sort_keys=True)
        self.assertNotIn("raw_id", result_json)
        self.assertNotIn("source", json.dumps(received_reminder, sort_keys=True))
        self.assertNotIn("source", json.dumps(echo, sort_keys=True))
        self.assertNotIn("nested-business-field", result_json)
        self.assertNotIn("command_type", result_json)
        self.assertNotIn("command_id", result_json)
        self.assertTrue(result["payload_boundary"]["business_payload_excluded"])

    def test_route_timeline_event_is_idempotent_for_same_source_event(self):
        from agent.governance import service_router, task_timeline

        self._insert_router_backlog()
        source = task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-router",
            backlog_id="BUG-SERVICE-ROUTER",
            event_type="task.completed",
            actor="worker",
            status="succeeded",
        )
        self.conn.commit()

        service_router.route_timeline_event(self.conn, source)
        service_router.route_timeline_event(self.conn, source)
        self.conn.commit()

        routed = task_timeline.list_events(
            self.conn,
            "proj",
            parent_event_id=source["id"],
            event_kind="service_route",
        )
        correlations = [event["correlation_id"] for event in routed]
        self.assertEqual(len(correlations), len(set(correlations)))
        self.assertEqual(len(routed), 2)

    def test_service_route_timeline_event_does_not_recurse(self):
        from agent.governance import task_timeline

        self._insert_router_backlog()
        source = task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-router",
            backlog_id="BUG-SERVICE-ROUTER",
            event_type="service.route.completed",
            phase="service_router",
            event_kind="service_route",
            actor="service-router",
            status="allowed",
            payload={"service_router_suppress": True},
            correlation_id="service-route:test",
        )
        self.conn.commit()

        children = task_timeline.list_events(
            self.conn,
            "proj",
            parent_event_id=source["id"],
        )

        self.assertEqual(children, [])

    def test_mf_process_timeline_records_queryable_test_scenario_decision(self):
        from agent.governance import task_timeline

        verification = task_timeline.mf_test_scenario_verification({
            "test_scenario_policy": "new_scenario_required",
            "test_scenario_spec": {
                "id": "scn-mf-timeline",
                "name": "MF timeline schema scenario",
                "steps": [
                    "record the observer scenario decision",
                    "record the implementation/gate result against the same scenario",
                ],
                "expected": [
                    "timeline rows are queryable by scenario and correlation",
                    "gate evidence keeps a parent pointer to the scenario decision",
                ],
            },
            "verification_notes": ["scenario was designed before implementation"],
        })
        self.assertTrue(verification["passed"], verification)

        scenario_event = task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF",
            mf_id="MF-20260523",
            task_id="task-mf",
            attempt_num=1,
            event_type="mf.test_scenario.decision",
            phase="plan",
            event_kind="scenario_spec",
            scenario_id="scn-mf-timeline",
            correlation_id="corr-mf-1",
            severity="info",
            decision="required",
            actor="observer",
            status="accepted",
            payload={
                "test_scenario_policy": "new_scenario_required",
                "test_scenario_spec": {
                    "id": "scn-mf-timeline",
                    "steps": ["record scenario", "record gate result"],
                    "expected": ["rows can be filtered"],
                },
            },
            verification=verification,
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF",
            mf_id="MF-20260523",
            task_id="task-mf",
            attempt_num=1,
            event_type="gate.mf_scenario.verified",
            phase="gate",
            event_kind="gate_result",
            scenario_id="scn-mf-timeline",
            parent_event_id=scenario_event["id"],
            correlation_id="corr-mf-1",
            severity="info",
            decision="approved",
            actor="gate",
            status="passed",
            verification={"passed": True, "checks": {"scenario_executed": True}},
        )
        self.conn.commit()

        events = task_timeline.list_events(
            self.conn,
            "proj",
            backlog_id="BUG-MF",
            scenario_id="scn-mf-timeline",
            correlation_id="corr-mf-1",
        )

        self.assertEqual([event["event_kind"] for event in events], ["scenario_spec", "gate_result"])
        self.assertEqual(events[0]["phase"], "plan")
        self.assertEqual(events[0]["decision"], "required")
        self.assertEqual(events[0]["schema_version"], 2)
        self.assertEqual(events[1]["parent_event_id"], scenario_event["id"])

        gate_events = task_timeline.list_events(
            self.conn,
            "proj",
            backlog_id="BUG-MF",
            scenario_id="scn-mf-timeline",
            event_kind="gate_result",
        )
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0]["event_type"], "gate.mf_scenario.verified")

    def test_mf_test_scenario_policy_verification(self):
        from agent.governance import task_timeline

        cases = [
            (
                "none with note",
                {"test_scenario_policy": "none", "verification_notes": ["copy-only README wording"]},
                True,
            ),
            (
                "none without note",
                {"test_scenario_policy": "none"},
                False,
            ),
            (
                "reuse existing with test command",
                {
                    "test_scenario_policy": "reuse_existing",
                    "tests_run": ["pytest -q agent/tests/test_task_timeline.py"],
                },
                True,
            ),
            (
                "reuse existing without evidence",
                {"test_scenario_policy": "reuse_existing"},
                False,
            ),
            (
                "new scenario missing spec",
                {"test_scenario_policy": "new_scenario_required", "verification_notes": ["high-risk path"]},
                False,
            ),
            (
                "new scenario with spec",
                {
                    "test_scenario_policy": "new_scenario_required",
                    "test_scenario_spec": {
                        "id": "scn-new",
                        "steps": ["seed fixture", "run MF command"],
                        "expected": ["gate sees scenario evidence"],
                    },
                },
                True,
            ),
            (
                "observer configured new scenario with deferred e2e",
                {
                    "test_scenario_policy": {
                        "mode": "observer_configured",
                        "decision": "new_scenario_required",
                        "allowed_decisions": [
                            "none",
                            "reuse_existing",
                            "new_scenario_required",
                        ],
                        "reason": "contract policy behavior needs focused coverage",
                        "required_evidence_ids": [
                            "observer_test_strategy",
                            "focused_tests",
                            "contract_gate_tests",
                            "docs_policy_update",
                            "e2e_deferred_followup",
                        ],
                        "e2e_decision": "e2e_deferred",
                        "followup_backlog_id": "E2E-OBSERVER-TEST-SCENARIO-POLICY-20260524",
                    },
                    "test_scenario_spec": {
                        "id": "scn-observer-policy",
                        "steps": ["instantiate contract", "run close gate"],
                        "expected": ["required evidence ids block close until referenced"],
                    },
                },
                True,
            ),
            (
                "observer configured deferred e2e missing followup",
                {
                    "test_scenario_policy": {
                        "mode": "observer_configured",
                        "decision": "none",
                        "allowed_decisions": [
                            "none",
                            "reuse_existing",
                            "new_scenario_required",
                        ],
                        "reason": "docs-only policy wording",
                        "required_evidence_ids": ["observer_test_strategy"],
                        "e2e_decision": "e2e_deferred",
                    },
                },
                False,
            ),
        ]
        for label, payload, expected in cases:
            with self.subTest(label=label):
                result = task_timeline.mf_test_scenario_verification(payload)
                self.assertEqual(result["passed"], expected, result)
                self.assertEqual(
                    result["effective_decision"],
                    (
                        payload["test_scenario_policy"]["decision"]
                        if isinstance(payload["test_scenario_policy"], dict)
                        else payload["test_scenario_policy"]
                    ),
                )

    def test_mf_close_gate_requires_observer_execution_evidence(self):
        from agent.governance import task_timeline

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-GATE",
            event_type="mf.implementation.completed",
            phase="implement",
            event_kind="implementation",
            actor="observer",
            status="passed",
            payload={"changed_files": ["agent/governance/server.py"]},
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-GATE",
            event_type="mf.verification.completed",
            phase="verify",
            event_kind="verification",
            actor="observer",
            status="passed",
            verification={"tests_run": ["pytest -q agent/tests/test_task_timeline.py"]},
        )
        self.conn.commit()

        events = task_timeline.list_events(self.conn, "proj", backlog_id="BUG-MF-GATE")
        blocked = task_timeline.mf_close_gate_verification(events)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(blocked["missing_event_kinds"], ["close_ready"])

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-GATE",
            event_type="mf.close_ready.accepted",
            phase="close",
            event_kind="close_ready",
            actor="observer",
            status="accepted",
            verification={"graph_reconciled": True, "preflight_ok": True},
        )
        self.conn.commit()

        ready_events = task_timeline.list_events(self.conn, "proj", backlog_id="BUG-MF-GATE")
        ready = task_timeline.mf_close_gate_verification(ready_events)

        self.assertTrue(ready["passed"], ready)
        self.assertEqual(
            ready["present_event_kinds"],
            ["close_ready", "implementation", "verification"],
        )

    def test_strict_governance_policy_requires_graph_trace_and_independent_qa(self):
        from agent.governance import task_timeline

        contract = {"governance_policy": STRICT_GOVERNANCE_POLICY}
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        blocked = task_timeline.mf_close_gate_verification(base_events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(blocked["governance_policy"]["profile"], "aming-claw")
        self.assertTrue(blocked["worker_graph_trace_gate"]["required"])
        self.assertTrue(blocked["independent_qa_gate"]["required"])
        self.assertEqual(
            blocked["worker_graph_trace_gate"]["missing_requirement_ids"],
            ["worker_graph_trace"],
        )
        self.assertEqual(
            blocked["independent_qa_gate"]["missing_requirement_ids"],
            ["independent_qa"],
        )
        self.assertIn(
            "worker_graph_trace",
            blocked["missing_evidence_groups"]["groups"],
        )
        self.assertIn("independent_qa", blocked["missing_evidence_groups"]["groups"])

        ready = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                {
                    "event_kind": "qa_verification",
                    "phase": "verification",
                    "actor": "qa",
                    "status": "passed",
                    "verification": {"graph_trace_ids": ["gqt-policy-close"]},
                },
            ],
            contract=contract,
        )

        self.assertTrue(ready["passed"], ready)
        self.assertEqual(ready["worker_graph_trace_gate"]["trace_ids"], ["gqt-policy-close"])
        self.assertTrue(ready["independent_qa_gate"]["passed"])

    # -----------------------------------------------------------------------
    # BUG-ROUTE-CONTEXT-CLOSE-GATE-QA-20260531: independence-by-identity tests
    # -----------------------------------------------------------------------

    def _parallel_contract_with_iv(self):
        """Return a contract that declares independent_verification_required."""
        return {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-IV-TEST",
            "route_topology_policy": {
                "selected_topology": "observer_led_parallel_lanes",
                "required_lanes": [
                    "observer_coordinator",
                    "bounded_implementation_worker",
                    "independent_verification_lane",
                    "observer_merge_close_gate",
                ],
                "independent_verification_required": True,
            },
        }

    def _startup_event(self, worker_slot_id: str) -> dict:
        """Return a minimal startup event that registers a worker identity."""
        return {
            "event_kind": "mf_subagent_startup",
            "phase": "startup_gate",
            "actor": worker_slot_id,
            "status": "passed",
            "payload": {
                "mf_subagent_startup_gate": {
                    "worker_slot_id": worker_slot_id,
                    "worker_id": worker_slot_id,
                    "agent_id": worker_slot_id,
                }
            },
        }

    def test_topology_policy_required_missing_iv_fails_independent_qa_gate(self):
        """Topology with independent_verification_required=True needs IV evidence."""
        from agent.governance import task_timeline

        contract = self._parallel_contract_with_iv()
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]
        result = task_timeline._independent_qa_gate(base_events, {}, contract=contract)
        self.assertTrue(result["required"], result)
        self.assertTrue(result["topology_required"], result)
        self.assertFalse(result["passed"], result)
        self.assertEqual(result["missing_requirement_ids"], ["independent_qa"])
        self.assertIn("independent_qa", result["reason"])

    def test_topology_policy_not_required_passes_without_iv(self):
        """Lightweight single-lane topology does NOT require IV evidence."""
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_workflow_runtime.v1",
            "contract_instance_id": "BUG-SINGLE-LANE",
            "route_topology_policy": {
                "selected_topology": "lightweight_single_lane",
                "required_lanes": ["single_bounded_worker"],
                "independent_verification_required": False,
            },
            "governance_policy": {
                "profile": "third-party-public",
                "requirements": {"independent_qa": False},
            },
        }
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
        ]
        result = task_timeline._independent_qa_gate(base_events, {}, contract=contract)
        self.assertFalse(result["required"], result)
        self.assertTrue(result["passed"], result)

    def test_worker_self_appended_iv_event_is_rejected(self):
        """IV evidence from the same worker (actor = worker_slot_id) must NOT count.
        Live incidents #3750/#3811.
        """
        from agent.governance import task_timeline

        worker_id = "codex-worker-impl-01"
        events = [
            self._startup_event(worker_id),
            {
                "event_kind": "qa_verification",
                "phase": "verification",
                "actor": worker_id,  # same as implementation worker — invalid
                "status": "passed",
            },
        ]
        policy = {"requirements": {"independent_qa": True}}
        result = task_timeline._independent_qa_gate(events, policy)
        self.assertTrue(result["required"], result)
        self.assertFalse(result["passed"], result)
        self.assertTrue(result["rejected_evidence_events"], result)
        self.assertEqual(
            result["rejected_evidence_events"][0]["reason"],
            "reviewer_is_known_worker",
        )
        self.assertIn(worker_id, result["known_worker_slot_ids"])

    def test_plain_observer_without_reviewer_identity_does_not_count(self):
        """A raw 'observer' verification event without reviewer identity does not count."""
        from agent.governance import task_timeline

        events = [
            {
                "event_kind": "qa_verification",
                "phase": "verification",
                "actor": "observer",  # plain observer, no on-behalf, no payload.reviewer
                "status": "passed",
            },
        ]
        policy = {"requirements": {"independent_qa": True}}
        result = task_timeline._independent_qa_gate(events, policy)
        self.assertTrue(result["required"], result)
        self.assertFalse(result["passed"], result)
        self.assertTrue(result["rejected_evidence_events"], result)
        self.assertEqual(
            result["rejected_evidence_events"][0]["reason"],
            "plain_observer_no_independent_reviewer",
        )

    def test_observer_on_behalf_with_independent_reviewer_counts(self):
        """Observer-on-behalf transport with independent reviewer DOES count.
        This is the established daily pattern — breaking it would brick every close.
        """
        from agent.governance import task_timeline

        worker_id = "codex-worker-impl-01"
        reviewer_id = "qa-reviewer-external-01"
        events = [
            self._startup_event(worker_id),
            {
                "event_kind": "qa_verification",
                "phase": "verification",
                "actor": f"observer-on-behalf-of:{reviewer_id}",
                "status": "passed",
                "payload": {
                    "qa_verdict_refs": ["evt-123"],
                },
            },
        ]
        policy = {"requirements": {"independent_qa": True}}
        result = task_timeline._independent_qa_gate(events, policy)
        self.assertTrue(result["required"], result)
        self.assertTrue(result["passed"], result)
        self.assertEqual(len(result["evidence_events"]), 1)
        self.assertEqual(result["evidence_events"][0]["reviewer_identity"], reviewer_id)

    def test_payload_reviewer_with_independent_identity_counts(self):
        """Observer transport with payload.reviewer set to independent reviewer counts."""
        from agent.governance import task_timeline

        worker_id = "codex-worker-impl-01"
        reviewer_id = "qa-external-reviewer-99"
        events = [
            self._startup_event(worker_id),
            {
                "event_kind": "qa_verification",
                "phase": "verification",
                "actor": "observer",  # plain observer transport
                "status": "passed",
                "payload": {
                    "reviewer": reviewer_id,
                    "qa_verdict_refs": ["evt-456"],
                },
            },
        ]
        policy = {"requirements": {"independent_qa": True}}
        result = task_timeline._independent_qa_gate(events, policy)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["evidence_events"][0]["reviewer_identity"], reviewer_id)

    def test_qa_review_direct_independent_actor_counts(self):
        """Direct qa_review event from a non-worker, non-observer actor counts."""
        from agent.governance import task_timeline

        worker_id = "codex-worker-impl-01"
        events = [
            self._startup_event(worker_id),
            {
                "event_kind": "qa_verification",
                "phase": "verification",
                "actor": "qa-lane-reviewer",
                "status": "passed",
            },
        ]
        policy = {"requirements": {"independent_qa": True}}
        result = task_timeline._independent_qa_gate(events, policy)
        self.assertTrue(result["passed"], result)
        self.assertEqual(len(result["evidence_events"]), 1)
        self.assertEqual(result["evidence_events"][0]["reviewer_identity"], "qa-lane-reviewer")

    def test_observer_with_verdict_refs_counts_as_independent(self):
        """Observer transport with qa_verdict_refs (even without on-behalf prefix) counts."""
        from agent.governance import task_timeline

        events = [
            {
                "event_kind": "qa_verification",
                "phase": "verification",
                "actor": "observer",
                "status": "passed",
                "payload": {"qa_verdict_refs": ["evt-789"]},
            },
        ]
        policy = {"requirements": {"independent_qa": True}}
        result = task_timeline._independent_qa_gate(events, policy)
        # observer with verdict_refs is treated as a legitimate relay
        self.assertTrue(result["passed"], result)

    def test_close_gate_passes_for_fixed_rows_without_iv_topology(self):
        """Rows without IV topology and without strict governance policy are unaffected.
        Backward-compat: already-FIXED rows with no independent_verification_required
        and third-party-public profile still close normally.
        """
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_workflow_runtime.v1",
            "governance_policy": {
                "profile": "third-party-public",
                "requirements": {
                    "graph_first_evidence": True,
                    "worker_graph_trace": False,
                    "independent_qa": False,
                    "single_active_task": False,
                    "close_timeline": True,
                },
            },
        }
        events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]
        result = task_timeline.mf_close_gate_verification(events, contract=contract)
        self.assertTrue(result["passed"], result)
        self.assertFalse(result["independent_qa_gate"]["required"])
        self.assertTrue(result["independent_qa_gate"]["passed"])

    def test_close_timeline_policy_can_disable_required_close_event_kinds(self):
        from agent.governance import task_timeline

        contract = {
            "governance_policy": {
                **STRICT_GOVERNANCE_POLICY,
                "profile": "third-party-public",
                "requirements": {
                    **STRICT_GOVERNANCE_POLICY["requirements"],
                    "worker_graph_trace": False,
                    "independent_qa": False,
                    "single_active_task": False,
                    "close_timeline": False,
                },
            },
        }

        result = task_timeline.mf_close_gate_verification([], contract=contract)

        self.assertTrue(result["passed"], result)
        self.assertFalse(result["close_timeline_required"])
        self.assertEqual(result["required_event_kinds"], [])
        self.assertEqual(result["missing_event_kinds"], [])
        self.assertFalse(result["checks"]["has_implementation"])
        self.assertFalse(result["checks"]["has_verification"])
        self.assertFalse(result["checks"]["has_close_ready"])

    def test_contract_projection_reports_read_receipt_before_counted_evidence(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-CONTRACT-PROJECTION",
            "canonical_visible_contract_text_hash": "sha256:contract-projection",
        }
        events = [
            {
                "id": 1,
                "event_type": "mf_subagent.read_receipt",
                "event_kind": "mf_subagent_read_receipt",
                "phase": "startup",
                "status": "accepted",
                "payload": {"read_receipt_hash": "sha256:read-receipt"},
            },
            {
                "id": 2,
                "event_type": "mf_subagent.startup",
                "event_kind": "mf_subagent_startup",
                "phase": "startup_gate",
                "status": "passed",
                "payload": {"runtime_context_id": "mfrctx-test"},
            },
        ]

        gate = task_timeline.mf_subagent_read_receipt_gate_verification(events)
        projection = task_timeline.mf_contract_projection(events, contract)

        self.assertTrue(gate["passed"], gate)
        self.assertEqual(gate["read_receipt_event_id"], 1)
        self.assertEqual(gate["first_counted_evidence_event_id"], 2)
        self.assertEqual(projection["schema_version"], "mf_contract_projection.v1")
        self.assertEqual(projection["source_of_truth"], "Contract/Revision/Event")
        self.assertEqual(projection["projection_watermark"], 2)
        self.assertEqual(projection["status"], "current")
        self.assertFalse(projection["stale"])
        self.assertFalse(projection["divergent"])
        self.assertEqual(
            projection["read_receipt_gate"]["read_receipt_hash"],
            "sha256:read-receipt",
        )

    def test_contract_projection_accepts_generated_contract_with_current_runtime_receipt(self):
        from agent.governance import task_timeline

        runtime_hash = "sha256:runtime-visible-contract"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-GENERATED-CONTRACT",
            "chain_trigger_json": {},
        }
        events = [
            _mf_subagent_read_receipt_event(event_id=1, contract_hash=runtime_hash),
            {
                "id": 2,
                "event_type": "mf_subagent.graph_query",
                "event_kind": "graph_query",
                "phase": "implementation",
                "status": "passed",
                "payload": {
                    "graph_trace_ids": ["gqt-runtime-contract"],
                    "canonical_visible_contract_text_hash": runtime_hash,
                },
            },
        ]

        projection = task_timeline.mf_contract_projection(events, contract)
        gate = task_timeline.mf_contract_projection_close_gate_verification(projection)

        self.assertEqual(projection["status"], "current")
        self.assertFalse(projection["stale"])
        self.assertFalse(projection["divergent"])
        self.assertFalse(projection["contract_hash_explicit"])
        self.assertEqual(projection["contract_hash_source"], "generated")
        self.assertNotEqual(projection["contract_hash"], runtime_hash)
        self.assertEqual(projection["observed_contract_hashes"], [runtime_hash])
        self.assertTrue(projection["read_receipt_gate"]["passed"], projection)
        self.assertTrue(gate["required"], gate)
        self.assertTrue(gate["passed"], gate)

    def test_contract_projection_rejects_explicit_hash_mismatch_with_runtime_receipt(self):
        from agent.governance import task_timeline

        contract_hash = "sha256:explicit-contract-current"
        runtime_hash = "sha256:runtime-visible-contract"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-EXPLICIT-CONTRACT-MISMATCH",
            "canonical_visible_contract_text_hash": contract_hash,
        }
        events = [
            _mf_subagent_read_receipt_event(event_id=1, contract_hash=runtime_hash),
            {
                "id": 2,
                "event_type": "mf_subagent.graph_query",
                "event_kind": "graph_query",
                "phase": "implementation",
                "status": "passed",
                "payload": {
                    "graph_trace_ids": ["gqt-runtime-contract"],
                    "canonical_visible_contract_text_hash": runtime_hash,
                },
            },
        ]

        projection = task_timeline.mf_contract_projection(events, contract)
        gate = task_timeline.mf_contract_projection_close_gate_verification(projection)

        self.assertEqual(projection["status"], "divergent")
        self.assertTrue(projection["stale"])
        self.assertTrue(projection["divergent"])
        self.assertTrue(projection["contract_hash_explicit"])
        self.assertEqual(projection["contract_hash_source"], "explicit")
        self.assertEqual(projection["contract_hash"], contract_hash)
        self.assertEqual(projection["observed_contract_hashes"], [runtime_hash])
        self.assertFalse(gate["passed"], gate)
        self.assertEqual(
            gate["missing_requirement_ids"],
            ["contract_projection_current", "contract_projection_not_divergent"],
        )

    def test_contract_projection_generated_contract_still_rejects_post_hoc_read_receipt(self):
        from agent.governance import task_timeline

        runtime_hash = "sha256:runtime-visible-contract"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-GENERATED-CONTRACT-LATE-RECEIPT",
            "chain_trigger_json": {},
        }
        events = [
            {
                "id": 10,
                "event_type": "mf_subagent.graph_query",
                "event_kind": "graph_query",
                "phase": "implementation",
                "status": "passed",
                "payload": {
                    "graph_trace_ids": ["gqt-runtime-contract"],
                    "canonical_visible_contract_text_hash": runtime_hash,
                },
            },
            _mf_subagent_read_receipt_event(event_id=11, contract_hash=runtime_hash),
        ]

        projection = task_timeline.mf_contract_projection(events, contract)
        gate = task_timeline.mf_contract_projection_close_gate_verification(projection)

        self.assertEqual(projection["status"], "stale")
        self.assertTrue(projection["stale"])
        self.assertFalse(projection["divergent"])
        self.assertEqual(projection["contract_hash_source"], "generated")
        self.assertEqual(projection["read_receipt_gate"]["status"], "out_of_order")
        self.assertFalse(gate["passed"], gate)
        self.assertEqual(
            gate["missing_requirement_ids"],
            ["contract_projection_current", "mf_subagent_read_receipt_gate"],
        )

    def test_close_precheck_read_receipt_gate_ignores_route_token_dispatch_before_startup(self):
        from agent.governance import task_timeline

        route_context, route_action, dispatch, startup = _route_context_consumption_events()
        route_context["id"] = 2005
        route_action["id"] = 2006
        dispatch["id"] = 2008
        startup["id"] = 2010
        events = [
            route_context,
            route_action,
            _route_token_gate_event(event_id=2007),
            dispatch,
            _mf_subagent_read_receipt_event(event_id=2009),
            startup,
        ]

        gate = task_timeline.mf_subagent_read_receipt_gate_verification(events)

        self.assertTrue(gate["passed"], gate)
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["read_receipt_event_id"], 2009)
        self.assertEqual(gate["first_counted_evidence_event_id"], 2010)
        self.assertEqual(gate["counted_evidence_event_ids"], [2010])
        self.assertNotIn(2007, gate["counted_evidence_event_ids"])
        self.assertNotIn(2008, gate["counted_evidence_event_ids"])

    def test_close_precheck_read_receipt_gate_rejects_post_hoc_after_real_worker_graph_startup(self):
        from agent.governance import task_timeline

        startup = _route_context_worker_startup_event()
        startup["id"] = 2008
        events = [
            {
                "id": 2007,
                "event_type": "mf_subagent.graph_query",
                "event_kind": "graph_query",
                "phase": "implementation",
                "actor": "mf_sub",
                "status": "passed",
                "payload": {"graph_trace_ids": ["gqt-real-worker-query"]},
            },
            startup,
            _mf_subagent_read_receipt_event(event_id=2009),
        ]

        gate = task_timeline.mf_subagent_read_receipt_gate_verification(events)

        self.assertFalse(gate["passed"], gate)
        self.assertEqual(gate["status"], "out_of_order")
        self.assertEqual(gate["failure_reason"], "worker_read_receipt_recorded_after_counted_evidence")
        self.assertEqual(gate["first_counted_evidence_event_id"], 2007)
        self.assertEqual(gate["read_receipt_event_id"], 2009)
        self.assertEqual(gate["counted_evidence_event_ids"], [2007, 2008])

    def test_close_precheck_accepts_startup_before_read_receipt_for_same_route_worktree_commit(self):
        from agent.governance import task_timeline

        contract_hash = "sha256:startup-before-read-receipt-contract"
        route_context, route_action, dispatch, startup = _route_context_consumption_events()
        route_context["id"] = 2101
        route_action["id"] = 2102
        dispatch["id"] = 2103
        startup["id"] = 2104
        worker_runtime = {
            "actual_cwd": "/repo/.worktrees/mf-sub-test",
            "actual_git_root": "/repo/.worktrees/mf-sub-test",
            "branch": "refs/heads/codex/mf-sub-test",
            "head_commit": "head-test",
        }
        lineage = {
            "runtime_context_id": "mfrctx-startup-before-read",
            "task_id": "mf-sub-startup-before-read",
            "parent_task_id": "BUG-STARTUP-BEFORE-READ-RECEIPT",
        }
        _add_attempt_lineage(startup, **lineage)
        read_receipt = _add_attempt_lineage(
            _mf_subagent_read_receipt_event(
                event_id=2105,
                contract_hash=contract_hash,
                identity={**ROUTE_IDENTITY, **worker_runtime},
            ),
            **lineage,
        )
        implementation = _add_attempt_lineage(
            {
                "id": 2106,
                "event_kind": "implementation",
                "phase": "implementation",
                "status": "accepted",
                "payload": {
                    **ROUTE_IDENTITY,
                    **worker_runtime,
                    "changed_files": ["agent/governance/task_timeline.py"],
                    "canonical_visible_contract_text_hash": contract_hash,
                },
            },
            **lineage,
        )
        qa = _add_attempt_lineage(
            {**_route_context_qa_verification_event(), "id": 2107},
            **lineage,
        )
        close_ready = _add_attempt_lineage(
            {
                "id": 2108,
                "event_kind": "close_ready",
                "phase": "close",
                "status": "accepted",
                "payload": {
                    **ROUTE_IDENTITY,
                    "canonical_visible_contract_text_hash": contract_hash,
                },
            },
            **lineage,
        )
        events = [
            route_context,
            route_action,
            dispatch,
            startup,
            read_receipt,
            implementation,
            qa,
            close_ready,
        ]
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-STARTUP-BEFORE-READ-RECEIPT",
            "canonical_visible_contract_text_hash": contract_hash,
        }

        ready = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertTrue(ready["passed"], ready)
        read_gate = ready["contract_projection"]["read_receipt_gate"]
        self.assertEqual(read_gate["status"], "passed")
        self.assertEqual(read_gate["read_receipt_event_id"], 2105)
        self.assertEqual(read_gate["first_counted_evidence_event_id"], 2106)
        self.assertEqual(
            read_gate["harmless_startup_before_read_receipt_event_ids"],
            [2104],
        )
        self.assertIn(2104, read_gate["counted_evidence_event_ids"])
        self.assertIn(2106, read_gate["counted_evidence_event_ids"])
        self.assertNotIn("raw_private_route_context", json.dumps(ready))

    def test_close_precheck_accepts_read_receipt_before_startup_for_same_route_worktree_commit(self):
        from agent.governance import task_timeline

        contract_hash = "sha256:read-receipt-before-startup-contract"
        route_context, route_action, dispatch, startup = _route_context_consumption_events()
        route_context["id"] = 2121
        route_action["id"] = 2122
        dispatch["id"] = 2123
        startup["id"] = 2125
        worker_runtime = {
            "actual_cwd": "/repo/.worktrees/mf-sub-test",
            "actual_git_root": "/repo/.worktrees/mf-sub-test",
            "branch": "refs/heads/codex/mf-sub-test",
            "head_commit": "head-test",
        }
        lineage = {
            "runtime_context_id": "mfrctx-read-before-startup",
            "task_id": "mf-sub-read-before-startup",
            "parent_task_id": "BUG-READ-RECEIPT-BEFORE-STARTUP",
        }
        _add_attempt_lineage(startup, **lineage)
        read_receipt = _add_attempt_lineage(
            _mf_subagent_read_receipt_event(
                event_id=2124,
                contract_hash=contract_hash,
                identity={**ROUTE_IDENTITY, **worker_runtime},
            ),
            **lineage,
        )
        implementation = _add_attempt_lineage(
            {
                "id": 2126,
                "event_kind": "implementation",
                "phase": "implementation",
                "status": "accepted",
                "payload": {
                    **ROUTE_IDENTITY,
                    **worker_runtime,
                    "changed_files": ["agent/governance/task_timeline.py"],
                    "canonical_visible_contract_text_hash": contract_hash,
                },
            },
            **lineage,
        )
        qa = _add_attempt_lineage(
            {**_route_context_qa_verification_event(), "id": 2127},
            **lineage,
        )
        close_ready = _add_attempt_lineage(
            {
                "id": 2128,
                "event_kind": "close_ready",
                "phase": "close",
                "status": "accepted",
                "payload": {
                    **ROUTE_IDENTITY,
                    "canonical_visible_contract_text_hash": contract_hash,
                },
            },
            **lineage,
        )
        events = [
            route_context,
            route_action,
            dispatch,
            read_receipt,
            startup,
            implementation,
            qa,
            close_ready,
        ]
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-READ-RECEIPT-BEFORE-STARTUP",
            "canonical_visible_contract_text_hash": contract_hash,
        }

        ready = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertTrue(ready["passed"], ready)
        read_gate = ready["contract_projection"]["read_receipt_gate"]
        self.assertEqual(read_gate["status"], "passed")
        self.assertEqual(read_gate["read_receipt_event_id"], 2124)
        self.assertEqual(read_gate["first_counted_evidence_event_id"], 2125)
        self.assertEqual(
            read_gate["harmless_startup_before_read_receipt_event_ids"],
            [],
        )
        self.assertIn(2125, read_gate["counted_evidence_event_ids"])
        self.assertIn(2126, read_gate["counted_evidence_event_ids"])

    def test_contract_projection_marks_missing_read_receipt_stale(self):
        from agent.governance import task_timeline

        events = [
            {
                "id": 10,
                "event_type": "mf_subagent.graph_query",
                "event_kind": "graph_query",
                "phase": "implementation",
                "status": "passed",
                "payload": {"graph_trace_ids": ["gqt-test"]},
            }
        ]

        projection = task_timeline.mf_contract_projection(events, contract=None)

        self.assertEqual(projection["status"], "no_contract")
        self.assertTrue(projection["stale"])
        self.assertFalse(projection["read_receipt_gate"]["passed"])
        self.assertEqual(
            projection["read_receipt_gate"]["missing_reason"],
            "worker_read_receipt_must_precede_graph_query_write_startup_evidence",
        )
        self.assertEqual(
            projection["read_receipt_gate"]["failure_reason"],
            "worker_read_receipt_missing_before_counted_evidence",
        )

    def test_contract_projection_rejects_post_hoc_read_receipt(self):
        from agent.governance import task_timeline

        contract_hash = "sha256:contract-projection-late-receipt"
        events = [
            {
                "id": 10,
                "event_type": "mf_subagent.graph_query",
                "event_kind": "graph_query",
                "phase": "implementation",
                "status": "passed",
                "payload": {
                    "graph_trace_ids": ["gqt-test"],
                    "canonical_visible_contract_text_hash": contract_hash,
                },
            },
            _mf_subagent_read_receipt_event(event_id=11, contract_hash=contract_hash),
            {"id": 12, "event_kind": "verification", "phase": "verification", "status": "passed"},
            {"id": 13, "event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-LATE-READ-RECEIPT",
            "canonical_visible_contract_text_hash": contract_hash,
        }

        gate = task_timeline.mf_subagent_read_receipt_gate_verification(events)
        blocked = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertFalse(gate["passed"], gate)
        self.assertEqual(gate["status"], "out_of_order")
        self.assertEqual(
            gate["failure_reason"],
            "worker_read_receipt_recorded_after_counted_evidence",
        )
        self.assertEqual(gate["first_counted_evidence_event_id"], 10)
        self.assertEqual(gate["read_receipt_event_id"], 11)
        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(
            blocked["contract_projection_gate"]["read_receipt_gate_status"],
            "out_of_order",
        )

    def test_route_identity_cleanup_scopes_read_receipt_gate_to_current_lineage(self):
        from agent.governance import task_timeline

        contract_hash = "sha256:contract-projection-clean-current-lineage"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-READ-RECEIPT-CLEANUP-LINEAGE",
            "canonical_visible_contract_text_hash": contract_hash,
        }
        stale_identity = {
            "route_context_hash": "sha256:stale-a3-route-context",
            "prompt_contract_id": "rprompt-stale-a3",
            "prompt_contract_hash": "sha256:stale-a3-prompt",
        }

        stale_route_events = _replace_route_identity(
            _route_context_consumption_events(),
            stale_identity,
        )
        for event_id, event in zip(range(2030, 2034), stale_route_events):
            event["id"] = event_id
        stale_read_receipt = _mf_subagent_read_receipt_event(
            event_id=2034,
            contract_hash=contract_hash,
            identity=stale_identity,
        )

        current_route_events = _route_context_consumption_events()
        for event_id, event in zip((2040, 2041, 2044, 2046), current_route_events):
            event["id"] = event_id
        current_read_receipt = _mf_subagent_read_receipt_event(
            event_id=2045,
            contract_hash=contract_hash,
            identity=ROUTE_IDENTITY,
        )
        current_qa = _route_context_qa_verification_event()
        current_qa["id"] = 2047
        cleanup = {
            "id": 2048,
            "event_kind": "route_identity_cleanup",
            "phase": "identity_recovery",
            "status": "accepted",
            "payload": {"route_identity_cleanup": ROUTE_IDENTITY},
        }
        close_events = [
            {
                "id": 2049,
                "event_kind": "implementation",
                "phase": "implementation",
                "status": "accepted",
                "payload": {
                    **ROUTE_IDENTITY,
                    "canonical_visible_contract_text_hash": contract_hash,
                },
            },
            {"id": 2050, "event_kind": "verification", "phase": "verification", "status": "passed"},
            {"id": 2051, "event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]
        events = [
            *stale_route_events,
            stale_read_receipt,
            *current_route_events[:3],
            current_read_receipt,
            current_route_events[3],
            current_qa,
            *close_events,
        ]

        poisoned = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertFalse(poisoned["passed"], poisoned)
        self.assertEqual(poisoned["checks"]["mf_subagent_read_receipt_gate"], "out_of_order")
        self.assertEqual(
            poisoned["contract_projection"]["read_receipt_gate"]["first_counted_evidence_event_id"],
            2033,
        )
        self.assertEqual(
            poisoned["contract_projection"]["read_receipt_gate"]["read_receipt_event_id"],
            2034,
        )

        ready = task_timeline.mf_close_gate_verification(
            [*events, cleanup],
            contract=contract,
        )

        self.assertTrue(ready["passed"], ready)
        self.assertEqual(ready["checks"]["mf_subagent_read_receipt_gate"], "passed")
        self.assertEqual(
            ready["contract_projection_gate"]["missing_requirement_ids"],
            [],
        )
        self.assertTrue(
            ready["route_context_gate"]["route_identity_cleanup"]["applied"]
        )
        read_gate = ready["contract_projection"]["read_receipt_gate"]
        self.assertTrue(read_gate["lineage_filter_applied"], read_gate)
        self.assertEqual(read_gate["lineage_route_identity"], ROUTE_IDENTITY)
        self.assertEqual(read_gate["read_receipt_event_id"], 2045)
        self.assertEqual(read_gate["first_counted_evidence_event_id"], 2046)
        self.assertEqual(read_gate["status"], "passed")
        self.assertIn(2033, read_gate["lineage_ignored_event_ids"])
        self.assertIn(2034, read_gate["lineage_ignored_event_ids"])
        self.assertIn(2046, read_gate["counted_evidence_event_ids"])
        self.assertIn(2049, read_gate["counted_evidence_event_ids"])
        self.assertNotIn(2033, read_gate["counted_evidence_event_ids"])

        missing_current_read = task_timeline.mf_close_gate_verification(
            [event for event in [*events, cleanup] if event.get("id") != 2045],
            contract=contract,
        )

        self.assertFalse(missing_current_read["passed"], missing_current_read)
        self.assertEqual(
            missing_current_read["contract_projection"]["read_receipt_gate"]["status"],
            "missing",
        )
        self.assertIn(
            "mf_subagent_read_receipt_gate",
            missing_current_read["contract_projection_gate"]["missing_requirement_ids"],
        )

    def test_same_route_fresh_attempt_scopes_read_receipt_by_runtime_context(self):
        from agent.governance import task_timeline

        contract_hash = "sha256:contract-projection-same-route-runtime-lineage"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-READ-RECEIPT-RUNTIME-LINEAGE",
            "canonical_visible_contract_text_hash": contract_hash,
        }
        parent_task_id = "BUG-READ-RECEIPT-RUNTIME-LINEAGE"
        stale_lineage = {
            "runtime_context_id": "mfrctx-stale-attempt",
            "task_id": "BUG-READ-RECEIPT-RUNTIME-LINEAGE-A0",
            "parent_task_id": parent_task_id,
            "worker_slot_id": "mf-sub-stale",
            "fence_token": "fence-stale",
        }
        current_lineage = {
            "runtime_context_id": "mfrctx-current-attempt",
            "task_id": "BUG-READ-RECEIPT-RUNTIME-LINEAGE-A1",
            "parent_task_id": parent_task_id,
            "worker_slot_id": "mf-sub-current",
            "fence_token": "fence-current",
        }

        stale_route_events = [
            _add_attempt_lineage(event, **stale_lineage)
            for event in _route_context_consumption_events()
        ]
        for event_id, event in zip((3000, 3001, 3003, 3005), stale_route_events):
            event["id"] = event_id
        stale_read_receipt = _mf_subagent_read_receipt_event(
            event_id=3006,
            contract_hash=contract_hash,
            identity={**ROUTE_IDENTITY, **stale_lineage},
        )

        current_route_events = [
            _add_attempt_lineage(event, **current_lineage)
            for event in _route_context_consumption_events()
        ]
        for event_id, event in zip((3010, 3011, 3012, 3014), current_route_events):
            event["id"] = event_id
        current_read_receipt = _mf_subagent_read_receipt_event(
            event_id=3013,
            contract_hash=contract_hash,
            identity={**ROUTE_IDENTITY, **current_lineage},
        )
        current_qa = _add_attempt_lineage(
            _route_context_qa_verification_event(),
            **current_lineage,
        )
        current_qa["id"] = 3016
        close_events = [
            _add_attempt_lineage(
                {
                    "id": 3015,
                    "event_kind": "implementation",
                    "phase": "implementation",
                    "status": "accepted",
                    "payload": {
                        **ROUTE_IDENTITY,
                        "canonical_visible_contract_text_hash": contract_hash,
                    },
                },
                **current_lineage,
            ),
            _add_attempt_lineage(
                {
                    "id": 3017,
                    "event_kind": "verification",
                    "phase": "verification",
                    "status": "passed",
                    "payload": {**ROUTE_IDENTITY},
                },
                **current_lineage,
            ),
            _add_attempt_lineage(
                {
                    "id": 3018,
                    "event_kind": "close_ready",
                    "phase": "close",
                    "status": "accepted",
                    "payload": {**ROUTE_IDENTITY},
                },
                **current_lineage,
            ),
        ]
        events = [
            *stale_route_events,
            stale_read_receipt,
            *current_route_events[:3],
            current_read_receipt,
            current_route_events[3],
            *close_events[:1],
            current_qa,
            *close_events[1:],
        ]

        poisoned_without_lineage = task_timeline.mf_subagent_read_receipt_gate_verification(
            events,
        )
        self.assertEqual(poisoned_without_lineage["status"], "out_of_order")
        self.assertEqual(poisoned_without_lineage["read_receipt_event_id"], 3006)

        ready = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertTrue(ready["passed"], ready)
        read_gate = ready["contract_projection"]["read_receipt_gate"]
        self.assertTrue(read_gate["lineage_filter_applied"], read_gate)
        self.assertEqual(
            read_gate["lineage_attempt_filter"]["runtime_context_id"],
            "mfrctx-current-attempt",
        )
        self.assertEqual(read_gate["read_receipt_event_id"], 3013)
        self.assertEqual(read_gate["first_counted_evidence_event_id"], 3014)
        self.assertEqual(read_gate["status"], "passed")
        self.assertIn(3005, read_gate["lineage_ignored_event_ids"])
        self.assertIn(3006, read_gate["lineage_ignored_event_ids"])
        self.assertNotIn(3005, read_gate["counted_evidence_event_ids"])
        self.assertIn(3014, read_gate["counted_evidence_event_ids"])
        self.assertEqual(
            ready["route_context_gate"]["attempt_lineage"]["lineage"][
                "runtime_context_id"
            ],
            "mfrctx-current-attempt",
        )

    def test_read_receipt_lineage_infers_parent_task_from_backlog_root(self):
        from agent.governance import task_timeline

        contract_hash = "sha256:contract-projection-a2-backlog-root-lineage"
        parent_task_id = "AC-MF-PROTECTED-EVIDENCE-LINEAGE-ROUTE-TOKEN-GAP-20260605"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": parent_task_id,
            "canonical_visible_contract_text_hash": contract_hash,
        }
        stale_lineage = {
            "runtime_context_id": "mfrctx-stale-a1",
            "task_id": f"{parent_task_id}-A1",
            "parent_task_id": parent_task_id,
            "worker_slot_id": "mfsub-stale-a1",
            "fence_token": "fence-stale-a1",
        }
        current_lineage = {
            "runtime_context_id": "mfrctx-18ba3d772941f4a2",
            "task_id": f"{parent_task_id}-A2",
            "parent_task_id": parent_task_id,
            "worker_slot_id": "mfsub-read-receipt-lineage-fallback-20260605-a2",
            "fence_token": "fence-ac-read-receipt-lineage-fallback-20260605-a2",
        }

        stale_route_events = [
            _add_attempt_lineage(event, **stale_lineage)
            for event in _route_context_consumption_events()
        ]
        for event_id, event in zip((4100, 4101, 4102, 4104), stale_route_events):
            event["id"] = event_id
            event["backlog_id"] = parent_task_id
        stale_read_receipt = _mf_subagent_read_receipt_event(
            event_id=4103,
            contract_hash=contract_hash,
            identity={
                **ROUTE_IDENTITY,
                "runtime_context_id": stale_lineage["runtime_context_id"],
                "task_id": stale_lineage["task_id"],
                "worker_slot_id": stale_lineage["worker_slot_id"],
                "fence_token": stale_lineage["fence_token"],
            },
        )
        stale_read_receipt["task_id"] = stale_lineage["task_id"]
        stale_read_receipt["backlog_id"] = parent_task_id

        current_route_events = [
            _add_attempt_lineage(event, **current_lineage)
            for event in _route_context_consumption_events()
        ]
        for event_id, event in zip((4110, 4111, 4112, 4114), current_route_events):
            event["id"] = event_id
            event["backlog_id"] = parent_task_id
        current_read_receipt = _mf_subagent_read_receipt_event(
            event_id=4113,
            contract_hash=contract_hash,
            identity={
                **ROUTE_IDENTITY,
                "runtime_context_id": current_lineage["runtime_context_id"],
                "task_id": current_lineage["task_id"],
                "worker_slot_id": current_lineage["worker_slot_id"],
                "fence_token": current_lineage["fence_token"],
            },
        )
        current_read_receipt["task_id"] = current_lineage["task_id"]
        current_read_receipt["backlog_id"] = parent_task_id
        self.assertNotIn("parent_task_id", current_read_receipt["payload"])

        cleanup = {
            "id": 4115,
            "event_kind": "route_identity_cleanup",
            "phase": "identity_recovery",
            "status": "accepted",
            "backlog_id": parent_task_id,
            "payload": {"route_identity_cleanup": ROUTE_IDENTITY},
        }
        current_qa = _add_attempt_lineage(
            _route_context_qa_verification_event(),
            **current_lineage,
        )
        current_qa["id"] = 4117
        current_qa["backlog_id"] = parent_task_id
        close_events = [
            _add_attempt_lineage(
                {
                    "id": 4116,
                    "event_kind": "implementation",
                    "phase": "implementation",
                    "status": "accepted",
                    "backlog_id": parent_task_id,
                    "payload": {
                        **ROUTE_IDENTITY,
                        "canonical_visible_contract_text_hash": contract_hash,
                    },
                },
                **current_lineage,
            ),
            _add_attempt_lineage(
                {
                    "id": 4118,
                    "event_kind": "verification",
                    "phase": "verification",
                    "status": "passed",
                    "backlog_id": parent_task_id,
                    "payload": {**ROUTE_IDENTITY},
                },
                **current_lineage,
            ),
            _add_attempt_lineage(
                {
                    "id": 4119,
                    "event_kind": "close_ready",
                    "phase": "close",
                    "status": "accepted",
                    "backlog_id": parent_task_id,
                    "payload": {**ROUTE_IDENTITY},
                },
                **current_lineage,
            ),
        ]
        events = [
            *stale_route_events,
            stale_read_receipt,
            *current_route_events[:3],
            current_read_receipt,
            current_route_events[3],
            cleanup,
            close_events[0],
            current_qa,
            *close_events[1:],
        ]

        ready = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertTrue(ready["passed"], ready)
        read_gate = ready["contract_projection"]["read_receipt_gate"]
        self.assertEqual(read_gate["status"], "passed")
        self.assertEqual(read_gate["read_receipt_event_id"], 4113)
        self.assertEqual(read_gate["first_counted_evidence_event_id"], 4114)
        self.assertEqual(
            read_gate["lineage_attempt_filter"]["parent_task_id"],
            parent_task_id,
        )
        self.assertIn(4103, read_gate["lineage_ignored_event_ids"])
        self.assertNotIn(4104, read_gate["counted_evidence_event_ids"])
        self.assertIn(4114, read_gate["counted_evidence_event_ids"])

    def test_mf_close_gate_requires_instantiated_contract_evidence(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-MF-CONTRACT",
            "evidence_requirements": [
                {
                    "id": "backend_tests",
                    "required": True,
                    "phase": "verification",
                    "command": "pytest -q agent/tests/test_task_timeline.py",
                },
                {
                    "id": "review_queue_category_e2e",
                    "required": True,
                    "phase": "integration",
                    "kind": "e2e",
                    "command": "cd frontend/dashboard && npm run e2e:semantic -- --project fixture --probe",
                },
            ],
        }
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {
                "event_kind": "verification",
                "phase": "verification",
                "status": "passed",
                "verification": {
                    "requirement_ids": ["backend_tests"],
                    "tests_run": ["pytest -q agent/tests/test_task_timeline.py"],
                },
            },
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        blocked = task_timeline.mf_close_gate_verification(base_events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(blocked["missing_event_kinds"], [])
        self.assertEqual(
            blocked["contract_gate"]["missing_requirement_ids"],
            ["review_queue_category_e2e"],
        )

        ready = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                *_route_context_consumption_events(),
                _route_context_qa_verification_event(),
                {
                    "event_kind": "verification",
                    "phase": "integration",
                    "status": "passed",
                    "verification": {
                        "contract_evidence": [
                            {
                                "requirement_id": "review_queue_category_e2e",
                                "status": "passed",
                                "command": (
                                    "cd frontend/dashboard && npm run e2e:semantic "
                                    "-- --project fixture --probe"
                                ),
                            }
                        ]
                    },
                },
            ],
            contract=contract,
        )

        self.assertTrue(ready["passed"], ready)
        self.assertTrue(ready["contract_gate"]["passed"])
        self.assertTrue(ready["route_context_gate"]["passed"])
        self.assertEqual(
            ready["contract_gate"]["present_requirement_ids"],
            ["backend_tests", "review_queue_category_e2e"],
        )

    def test_mf_parallel_close_gate_requires_route_context_consumption(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-ROUTE-CONTEXT",
        }
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        blocked = task_timeline.mf_close_gate_verification(base_events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(
            blocked["route_context_gate"]["missing_requirement_ids"],
            [
                "route_context",
                "route_action_precheck",
                "bounded_implementation_worker_dispatch",
                "mf_subagent_startup",
                "independent_verification_lane",
            ],
        )
        missing_groups = blocked["missing_evidence_groups"]["groups"]
        self.assertEqual(missing_groups["timeline"]["missing"], [])
        self.assertEqual(
            missing_groups["route_service"]["missing"],
            ["route_context", "route_action_precheck"],
        )
        self.assertEqual(
            missing_groups["bounded_worker"]["missing"],
            ["bounded_implementation_worker_dispatch", "mf_subagent_startup"],
        )
        self.assertEqual(
            missing_groups["independent_verification"]["missing"],
            ["independent_verification_lane"],
        )
        reminder = blocked["route_context_reminder"]
        self.assertTrue(reminder["blocked"])
        self.assertEqual(reminder["contract_template_id"], "mf_workflow_runtime.v1")
        self.assertEqual(
            reminder["allowed_stages"],
            ["dispatch", "startup_gate", "implementation_wait", "handoff_gate"],
        )
        self.assertIn("route.prompt_alert_bundle", [
            action["command"] for action in reminder["next_actions"]
        ])
        support_context = reminder["boundary"]["supporting_context_not_route_token"]
        self.assertIn("private_route_provider_context", support_context)
        self.assertNotIn("raw_private_route_context", support_context)
        self.assertNotIn("raw_private_route_context", repr(reminder))

        advisory_only = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                {
                    "event_kind": "route_context_advisory",
                    "status": "passed",
                    "payload": {"message": "observer should dispatch a worker"},
                },
            ],
            contract=contract,
        )
        self.assertFalse(advisory_only["passed"], advisory_only)

        route_consumption_only = task_timeline.mf_close_gate_verification(
            _route_context_consumption_events(),
            contract=contract,
        )

        self.assertFalse(route_consumption_only["passed"], route_consumption_only)
        self.assertEqual(
            route_consumption_only["missing_event_kinds"],
            ["close_ready", "implementation", "verification"],
        )
        self.assertEqual(
            route_consumption_only["route_context_gate"]["missing_requirement_ids"],
            ["independent_verification_lane"],
        )

        ordinary_verification_only = task_timeline.mf_close_gate_verification(
            [*base_events, *_route_context_consumption_events()],
            contract=contract,
        )

        self.assertFalse(ordinary_verification_only["passed"], ordinary_verification_only)
        self.assertEqual(
            ordinary_verification_only["route_context_gate"]["missing_requirement_ids"],
            ["independent_verification_lane"],
        )

        ready = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                *_route_context_consumption_events(),
                _route_context_qa_verification_event(),
            ],
            contract=contract,
        )

        self.assertTrue(ready["passed"], ready)
        self.assertEqual(
            ready["route_context_gate"]["present_requirement_ids"],
            [
                "route_context",
                "route_action_precheck",
                "bounded_implementation_worker_dispatch",
                "mf_subagent_startup",
                "independent_verification_lane",
            ],
        )

    def test_mf_parallel_close_gate_blocks_missing_read_receipt_for_contract_projection(self):
        from agent.governance import task_timeline

        contract_hash = "sha256:contract-projection-current"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-READ-RECEIPT-GATE",
            "canonical_visible_contract_text_hash": contract_hash,
        }
        events = [
            {
                "event_kind": "implementation",
                "phase": "implementation",
                "status": "accepted",
                "payload": {"canonical_visible_contract_text_hash": contract_hash},
            },
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
            *_route_context_consumption_events(),
            _route_context_qa_verification_event(),
        ]

        blocked = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(
            blocked["contract_projection_gate"]["missing_requirement_ids"],
            ["contract_projection_current", "mf_subagent_read_receipt_gate"],
        )
        self.assertEqual(
            blocked["contract_projection"]["read_receipt_gate"]["missing_reason"],
            "worker_read_receipt_must_precede_graph_query_write_startup_evidence",
        )
        self.assertEqual(
            blocked["missing_evidence_groups"]["groups"]["contract_projection"]["missing"],
            ["contract_projection_current", "mf_subagent_read_receipt_gate"],
        )

        ready = task_timeline.mf_close_gate_verification(
            [_mf_subagent_read_receipt_event(contract_hash=contract_hash), *events],
            contract=contract,
        )

        self.assertTrue(ready["passed"], ready)
        self.assertEqual(ready["contract_projection_gate"]["status"], "passed")
        self.assertEqual(ready["checks"]["mf_subagent_read_receipt_gate"], "passed")

    def test_20260604_dogfood_sequence_recognizes_read_receipt_and_qa_lane(self):
        from agent.governance import task_timeline

        contract_hash = "sha256:dogfood-read-receipt-contract"
        route_context, route_action, dispatch, startup = _route_context_consumption_events()
        route_context["id"] = 1
        route_action["id"] = 2
        dispatch["id"] = 3
        startup["id"] = 5
        qa_verification = _route_context_qa_verification_event()
        qa_verification["id"] = 7
        events = [
            route_context,
            route_action,
            dispatch,
            _mf_subagent_read_receipt_event(event_id=4, contract_hash=contract_hash),
            startup,
            {
                "id": 6,
                "event_kind": "implementation",
                "phase": "implementation",
                "status": "accepted",
                "payload": {
                    "changed_files": ["agent/governance/task_timeline.py"],
                    "canonical_visible_contract_text_hash": contract_hash,
                    "graph_trace_ids": ["gqt-20260604-dogfood"],
                },
            },
            qa_verification,
            {"id": 8, "event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-20260604-DOGFOOD",
            "canonical_visible_contract_text_hash": contract_hash,
        }

        ready = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertTrue(ready["passed"], ready)
        self.assertEqual(ready["missing_event_kinds"], [])
        self.assertEqual(ready["route_context_gate"]["missing_requirement_ids"], [])
        self.assertEqual(
            ready["route_context_gate"]["present_requirement_ids"],
            [
                "route_context",
                "route_action_precheck",
                "bounded_implementation_worker_dispatch",
                "mf_subagent_startup",
                "independent_verification_lane",
            ],
        )
        self.assertEqual(
            ready["contract_projection"]["read_receipt_gate"]["status"],
            "passed",
        )
        self.assertEqual(
            ready["contract_projection"]["read_receipt_gate"][
                "read_receipt_precedes_counted_evidence"
            ],
            True,
        )

    def test_mf_parallel_close_gate_blocks_divergent_contract_projection(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-DIVERGENT-PROJECTION",
            "canonical_visible_contract_text_hash": "sha256:contract-current",
        }
        events = [
            _mf_subagent_read_receipt_event(event_id=1),
            {
                "id": 2,
                "event_kind": "implementation",
                "phase": "implementation",
                "status": "accepted",
                "payload": {"canonical_visible_contract_text_hash": "sha256:stale"},
            },
            {"id": 3, "event_kind": "verification", "phase": "verification", "status": "passed"},
            {"id": 4, "event_kind": "close_ready", "phase": "close", "status": "accepted"},
            *_route_context_consumption_events(),
            _route_context_qa_verification_event(),
        ]

        blocked = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertTrue(blocked["contract_projection"]["divergent"])
        self.assertIn(
            "contract_projection_not_divergent",
            blocked["contract_projection_gate"]["missing_requirement_ids"],
        )

    def test_mf_close_gate_reports_missing_post_verification_actions(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_workflow_runtime.v1",
            "contract_instance_id": "BUG-POST-VERIFY-ACTIONS",
            "route_topology_policy": {"selected_topology": "lightweight_single_lane"},
            "verification_route_policy": {
                "post_verification_impact_actions": {
                    "required": True,
                    "actions": ["asset_inbox_binding"],
                    "requires_observer": True,
                }
            },
        }
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        blocked = task_timeline.mf_close_gate_verification(base_events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(
            blocked["post_verification_actions_gate"]["missing_actions"],
            ["asset_inbox_binding"],
        )
        self.assertTrue(
            blocked["post_verification_actions_gate"]["follow_up"]["required"]
        )
        self.assertEqual(
            blocked["missing_evidence_groups"]["groups"]["post_verification_actions"][
                "missing"
            ],
            ["asset_inbox_binding"],
        )

        ready = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                {
                    "event_kind": "close_ready",
                    "phase": "close",
                    "status": "accepted",
                    "verification": {
                        "post_verification_impact_actions": {
                            "status": "follow_up_filed",
                            "follow_up_filed": True,
                            "actions": ["asset_inbox_binding"],
                        }
                    },
                },
            ],
            contract=contract,
        )

        self.assertTrue(ready["passed"], ready)
        self.assertEqual(
            ready["post_verification_actions_gate"]["present_actions"],
            ["asset_inbox_binding"],
        )

    def test_mf_parallel_close_gate_accepts_child_startup_prompt_contract_lineage(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-CHILD-STARTUP-LINEAGE",
        }
        route_context, route_action, dispatch, _startup = _route_context_consumption_events()
        startup = _runtime_text_actual_startup_event(
            {
                **ROUTE_IDENTITY,
                "prompt_contract_id": (
                    ROUTE_IDENTITY["prompt_contract_id"] + ".child.worker-a"
                ),
                "prompt_contract_hash": "sha256:child-prompt-contract",
            }
        )
        startup["payload"]["mf_subagent_startup_gate"].update(
            {
                "parent_prompt_contract_id": ROUTE_IDENTITY["prompt_contract_id"],
                "parent_prompt_contract_hash": ROUTE_IDENTITY["prompt_contract_hash"],
                "child_prompt_contract_id": (
                    ROUTE_IDENTITY["prompt_contract_id"] + ".child.worker-a"
                ),
                "child_prompt_contract_hash": "sha256:child-prompt-contract",
                "parent_route_context_hash": ROUTE_IDENTITY["route_context_hash"],
                "child_route_context_hash": ROUTE_IDENTITY["route_context_hash"],
                "parent_visible_injection_manifest_hash": "sha256:test-visible-manifest",
                "child_visible_injection_manifest_hash": "sha256:test-visible-manifest",
                "visible_injection_manifest_hash": "sha256:test-visible-manifest",
                "parent_route_lineage": {
                    **ROUTE_IDENTITY,
                    "visible_injection_manifest_hash": "sha256:test-visible-manifest",
                },
                "child_route_lineage": {
                    "route_context_hash": ROUTE_IDENTITY["route_context_hash"],
                    "prompt_contract_id": (
                        ROUTE_IDENTITY["prompt_contract_id"] + ".child.worker-a"
                    ),
                    "prompt_contract_hash": "sha256:child-prompt-contract",
                    "visible_injection_manifest_hash": "sha256:test-visible-manifest",
                },
            }
        )
        events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
            route_context,
            route_action,
            dispatch,
            startup,
            _route_context_qa_verification_event(),
        ]

        ready = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertTrue(ready["passed"], ready)
        self.assertEqual(ready["route_context_gate"]["missing_requirement_ids"], [])
        self.assertEqual(
            ready["route_context_gate"]["route_identity"]["prompt_contract_id"],
            ROUTE_IDENTITY["prompt_contract_id"],
        )
        self.assertEqual(
            ready["route_context_gate"]["accepted_startup_lineages"][0][
                "child_prompt_contract_id"
            ],
            ROUTE_IDENTITY["prompt_contract_id"] + ".child.worker-a",
        )

    def test_mf_parallel_close_gate_rejects_generated_runtime_text_startup_intent(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-RUNTIME-TEXT-STARTUP",
        }
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]
        route_context, route_action, dispatch, _manual_startup = (
            _route_context_consumption_events()
        )

        ready = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                route_context,
                route_action,
                dispatch,
                _runtime_text_generated_startup_intent_event(),
                _route_context_qa_verification_event(),
            ],
            contract=contract,
        )

        self.assertFalse(ready["passed"], ready)
        self.assertEqual(
            ready["route_context_gate"]["missing_requirement_ids"],
            ["mf_subagent_startup"],
        )
        self.assertNotIn(
            "mf_subagent_startup",
            ready["route_context_gate"]["present_requirement_ids"],
        )

    def test_mf_parallel_close_gate_accepts_actual_runtime_text_startup_event(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-RUNTIME-TEXT-STARTUP",
        }
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]
        route_context, route_action, dispatch, _manual_startup = (
            _route_context_consumption_events()
        )

        ready = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                route_context,
                route_action,
                dispatch,
                _runtime_text_actual_startup_event(),
                _route_context_qa_verification_event(),
            ],
            contract=contract,
        )

        self.assertTrue(ready["passed"], ready)
        startup_events = ready["route_context_gate"]["evidence_events"][
            "mf_subagent_startup"
        ]
        self.assertEqual(startup_events[0]["event_kind"], "mf_subagent_startup")
        self.assertIn(
            "mf_subagent_startup",
            ready["route_context_gate"]["present_requirement_ids"],
        )

    def test_mf_parallel_close_gate_rejects_weak_startup_without_actual_identity(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-RUNTIME-TEXT-WEAK-STARTUP",
        }
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "finish", "phase": "handoff_gate", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]
        route_context, route_action, dispatch, _manual_startup = (
            _route_context_consumption_events()
        )

        blocked = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                route_context,
                route_action,
                dispatch,
                _runtime_text_legacy_weak_startup_event(),
                _route_context_qa_verification_event(),
            ],
            contract=contract,
        )

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(blocked["missing_event_kinds"], [])
        self.assertEqual(
            blocked["route_context_gate"]["missing_requirement_ids"],
            ["mf_subagent_startup"],
        )
        self.assertNotIn(
            "mf_subagent_startup",
            blocked["route_context_gate"]["present_requirement_ids"],
        )
        self.assertIn(
            {
                "id": None,
                "event_kind": "mf_subagent_startup",
                "status": "passed",
                "reason": "missing_actual_startup_identity",
                "categories": ["mf_subagent_startup"],
            },
            blocked["route_context_gate"]["ignored_route_events"],
        )
        self.assertTrue(blocked["checks"]["has_implementation"])
        self.assertTrue(blocked["checks"]["has_verification"])
        self.assertTrue(blocked["checks"]["has_close_ready"])

    def test_mf_parallel_close_gate_blocks_when_generated_startup_missing(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-RUNTIME-TEXT-MISSING-STARTUP",
        }
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "finish", "phase": "handoff_gate", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]
        route_context, route_action, dispatch, _manual_startup = (
            _route_context_consumption_events()
        )

        blocked = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                route_context,
                route_action,
                dispatch,
                _route_context_qa_verification_event(),
            ],
            contract=contract,
        )

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(blocked["missing_event_kinds"], [])
        self.assertEqual(
            blocked["route_context_gate"]["missing_requirement_ids"],
            ["mf_subagent_startup"],
        )
        self.assertTrue(blocked["checks"]["has_implementation"])
        self.assertTrue(blocked["checks"]["has_verification"])
        self.assertTrue(blocked["checks"]["has_close_ready"])

    def test_route_context_gate_accepts_visible_manifest_without_prompt_contract_hash(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-ROUTE-NO-PROMPT-HASH",
        }
        events = _without_prompt_contract_hash(
            [
                *_route_context_consumption_events(),
                _route_context_qa_verification_event(),
                {
                    "event_kind": "route_waiver",
                    "phase": "pre_mutation",
                    "status": "accepted",
                    "payload": {
                        "route_waiver": {
                            "accepted": True,
                            "route_context_hash": ROUTE_IDENTITY["route_context_hash"],
                            "prompt_contract_id": ROUTE_IDENTITY["prompt_contract_id"],
                            "allowed_action": "task_timeline_append",
                            "timeline_evidence": {"event_id": "tl-route-waiver"},
                        }
                    },
                },
            ]
        )

        result = task_timeline.mf_route_context_gate_verification(events, contract)

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["missing_requirement_ids"], [])
        self.assertEqual(
            result["route_identity"],
            {
                "route_context_hash": ROUTE_IDENTITY["route_context_hash"],
                "prompt_contract_id": ROUTE_IDENTITY["prompt_contract_id"],
            },
        )

    def test_route_context_gate_ignores_route_context_without_visible_manifest(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-ROUTE-NO-MANIFEST",
        }
        events = _without_prompt_contract_hash(_route_context_consumption_events())
        events[0]["payload"].pop("visible_injection_manifest_hash", None)

        result = task_timeline.mf_route_context_gate_verification(events, contract)

        self.assertFalse(result["passed"], result)
        self.assertIn("route_context", result["missing_requirement_ids"])
        self.assertEqual(
            result["ignored_route_events"][0]["reason"],
            "missing_visible_injection_manifest",
        )

    def test_mf_parallel_close_gate_requires_matching_qa_lane(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-ROUTE-QA-LANE",
            "route_topology_policy": {
                "selected_topology": "observer_led_parallel_lanes",
                "required_lanes": [
                    "observer_coordinator",
                    "bounded_implementation_worker",
                    "independent_verification_lane",
                    "observer_merge_close_gate",
                ],
                "independent_verification_required": True,
            },
        }
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
            *_route_context_consumption_events(),
        ]

        blocked = task_timeline.mf_close_gate_verification(base_events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(
            blocked["route_context_gate"]["missing_requirement_ids"],
            ["independent_verification_lane"],
        )

        wrong_identity = _route_context_qa_verification_event()
        wrong_identity["verification"]["prompt_contract_hash"] = "sha256:wrong"
        mismatch = task_timeline.mf_close_gate_verification(
            [*base_events, wrong_identity],
            contract=contract,
        )
        self.assertFalse(mismatch["passed"], mismatch)
        self.assertIn(
            "route_identity_mismatch",
            mismatch["route_context_gate"]["missing_requirement_ids"],
        )
        self.assertEqual(
            mismatch["missing_evidence_groups"]["groups"]["route_identity"]["missing"],
            ["route_identity_mismatch"],
        )

        ready = task_timeline.mf_close_gate_verification(
            [*base_events, _route_context_qa_verification_event()],
            contract=contract,
        )
        self.assertTrue(ready["passed"], ready)
        self.assertEqual(
            ready["route_context_gate"]["evidence_events"][
                "independent_verification_lane"
            ][0]["event_kind"],
            "qa_verification",
        )

    def test_optional_architecture_review_lane_does_not_block_lightweight_close(self):
        from agent.governance import task_timeline

        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]
        optional_contract = {
            "template_id": "mf_workflow_runtime.v1",
            "contract_instance_id": "BUG-OPTIONAL-ARCH-LANE",
            "route_topology_policy": {
                "selected_topology": "lightweight_single_lane",
                "recommended_topology": "single_lane.v1",
                "required_lanes": ["single_bounded_worker"],
            },
            "evidence_requirements": [
                {
                    "id": "architecture_review_lane",
                    "required": False,
                    "phase": "architecture_review",
                },
            ],
        }

        ready = task_timeline.mf_close_gate_verification(
            base_events,
            contract=optional_contract,
        )

        self.assertTrue(ready["passed"], ready)
        self.assertFalse(ready["route_context_gate"]["required"])
        self.assertNotIn(
            "architecture_review_lane",
            ready["route_context_gate"]["missing_requirement_ids"],
        )

        required_contract = {
            **optional_contract,
            "contract_instance_id": "BUG-REQUIRED-ARCH-LANE",
            "evidence_requirements": [
                {
                    "id": "architecture_review_lane",
                    "required": True,
                    "phase": "architecture_review",
                },
            ],
        }
        blocked = task_timeline.mf_close_gate_verification(
            [*base_events, *_route_context_consumption_events()],
            contract=required_contract,
        )

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(
            blocked["route_context_gate"]["missing_requirement_ids"],
            ["architecture_review_lane"],
        )

        with_arch_review = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                *_route_context_consumption_events(),
                _route_context_architecture_review_event(),
            ],
            contract=required_contract,
        )

        self.assertTrue(with_arch_review["passed"], with_arch_review)

    def test_mf_parallel_close_gate_requires_architecture_lane_only_when_named(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-ROUTE-ARCH-LANE",
            "route_topology_policy": {
                "selected_topology": "observer_led_parallel_lanes",
                "required_lanes": [
                    "observer_coordinator",
                    "bounded_implementation_worker",
                    "independent_verification_lane",
                    "architecture_review_lane",
                    "observer_merge_close_gate",
                ],
                "independent_verification_required": True,
            },
        }
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
            *_route_context_consumption_events(),
            _route_context_qa_verification_event(),
        ]

        blocked = task_timeline.mf_close_gate_verification(base_events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(
            blocked["route_context_gate"]["missing_requirement_ids"],
            ["architecture_review_lane"],
        )
        self.assertEqual(
            blocked["missing_evidence_groups"]["groups"]["architecture_review"]["missing"],
            ["architecture_review_lane"],
        )
        self.assertTrue(
            blocked["route_context_reminder"]["missing_evidence_groups"][
                "architecture_review"
            ]["missing"]
        )

        ready = task_timeline.mf_close_gate_verification(
            [*base_events, _route_context_architecture_review_event()],
            contract=contract,
        )

        self.assertTrue(ready["passed"], ready)
        self.assertIn(
            "architecture_review_lane",
            ready["route_context_gate"]["present_requirement_ids"],
        )
        self.assertEqual(
            ready["route_context_gate"]["evidence_events"]["architecture_review_lane"][
                0
            ]["event_kind"],
            "architecture_review",
        )

    def test_mf_parallel_close_gate_rejects_route_identity_mismatch(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-ROUTE-MISMATCH",
        }
        events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
            *_route_context_consumption_events(),
        ]
        events[-1]["payload"]["mf_subagent_startup_gate"]["prompt_contract_hash"] = (
            "sha256:different-prompt-contract"
        )

        blocked = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertIn(
            "route_identity_mismatch",
            blocked["route_context_gate"]["missing_requirement_ids"],
        )

    def test_mf_parallel_close_gate_accepts_route_identity_cleanup(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-ROUTE-CLEANUP",
        }
        stale_identity = {
            "route_context_hash": "sha256:stale-route-context",
            "prompt_contract_id": "rprompt-stale-route",
            "prompt_contract_hash": "sha256:stale-prompt-contract",
        }
        stale_events = _replace_route_identity(
            [*_route_context_consumption_events(), _route_context_qa_verification_event()],
            stale_identity,
        )
        current_events = [
            *_route_context_consumption_events(),
            _route_context_qa_verification_event(),
        ]
        close_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        blocked = task_timeline.mf_close_gate_verification(
            [*close_events, *stale_events, *current_events],
            contract=contract,
        )

        self.assertFalse(blocked["passed"], blocked)
        self.assertIn(
            "route_identity_mismatch",
            blocked["route_context_gate"]["missing_requirement_ids"],
        )

        cleanup = {
            "event_kind": "route_identity_cleanup",
            "phase": "identity_recovery",
            "status": "accepted",
            "payload": {
                "route_identity_cleanup": {
                    **ROUTE_IDENTITY,
                    "reason": "Supersede stale hand-written route evidence with the fresh service-generated route attempt.",
                }
            },
        }
        ready = task_timeline.mf_close_gate_verification(
            [*close_events, *stale_events, *current_events, cleanup],
            contract=contract,
        )

        self.assertTrue(ready["passed"], ready)
        self.assertEqual(ready["route_context_gate"]["missing_requirement_ids"], [])
        self.assertEqual(
            ready["route_context_gate"]["route_identity"],
            ROUTE_IDENTITY,
        )
        self.assertTrue(
            ready["route_context_gate"]["route_identity_cleanup"]["applied"]
        )
        self.assertGreater(
            ready["route_context_gate"]["route_identity_cleanup"][
                "superseded_event_count"
            ],
            0,
        )

    def test_observer_command_terminal_projection_uses_canonical_close_evidence(self):
        from agent.governance import task_timeline

        contract_hash = "sha256:observer-command-terminal-contract"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-COMMAND-TERMINAL-PROJECTION",
            "canonical_visible_contract_text_hash": contract_hash,
        }
        stale_identity = {
            "route_id": "route-repair-01c5a0404ba10777",
            "route_context_hash": "sha256:stale-route-context",
            "prompt_contract_id": "rprompt-stale-route",
            "prompt_contract_hash": "sha256:stale-prompt-contract",
            "visible_injection_manifest_hash": "sha256:stale-visible",
        }
        stale_events = _replace_route_identity(
            _route_context_consumption_events()[:3],
            stale_identity,
        )
        current_events = [
            *_route_context_consumption_events(),
            _route_context_qa_verification_event(),
        ]
        close_events = [
            _mf_subagent_read_receipt_event(
                event_id=0,
                contract_hash=contract_hash,
                identity=ROUTE_IDENTITY,
            ),
            {
                "id": 1811,
                "event_kind": "implementation",
                "phase": "implementation",
                "status": "accepted",
                "payload": {
                    **ROUTE_IDENTITY,
                    "canonical_visible_contract_text_hash": contract_hash,
                },
            },
            {"id": 1817, "event_kind": "verification", "phase": "verification", "status": "passed"},
            {"id": 1835, "event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]
        cleanup = {
            "id": 1824,
            "event_kind": "route_identity_cleanup",
            "phase": "identity_recovery",
            "status": "accepted",
            "payload": {"route_identity_cleanup": ROUTE_IDENTITY},
        }

        projection = task_timeline.observer_command_terminal_projection_from_close_evidence(
            {"backlog_id": "BUG-COMMAND-TERMINAL-PROJECTION", **stale_identity},
            {
                "canonical_close_evidence": {
                    "timeline_events": [*close_events, *stale_events, *current_events, cleanup],
                    "contract": contract,
                    "canonical_route_identity": {
                        "route_id": "route-repair-e97d980211e2dc1c",
                        **ROUTE_IDENTITY,
                    },
                    "backlog_close": {
                        "request_id": "req-97cd668efd14",
                        "backlog_status": "FIXED",
                    },
                }
            },
        )

        self.assertTrue(projection["passed"], projection)
        self.assertEqual(projection["command_projection_status"], "completed")
        self.assertEqual(
            projection["divergence_reason"],
            "superseded_route_identity_reconciled",
        )
        self.assertEqual(
            projection["canonical_route_identity"]["route_context_hash"],
            ROUTE_IDENTITY["route_context_hash"],
        )
        self.assertEqual(
            projection["superseded_route_identity"]["route_id"],
            "route-repair-01c5a0404ba10777",
        )

    def test_mf_close_gate_blocks_observer_direct_when_subagent_lane_required(self):
        from agent.governance import task_timeline

        contract = {
            "route_id": "route-lane-required",
            "route_context_hash": "sha256:lane-required",
            "required_lanes": [
                {"id": "bounded_implementation_subagent", "role": "implementation_worker"},
            ],
            "blocked_actions": [
                "edit_files_as_observer_or_independent_reviewer",
                "close_without_worker_or_subagent_evidence",
            ],
        }
        events = [
            {
                "event_kind": "implementation",
                "phase": "implementation",
                "actor": "codex-observer",
                "status": "accepted",
            },
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        blocked = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        lane_gate = blocked["lane_ownership_gate"]
        self.assertTrue(lane_gate["subagent_required"])
        self.assertEqual(
            lane_gate["missing_lane_ownership_ids"],
            [
                "bounded_implementation_subagent.dispatch",
                "bounded_implementation_subagent.review_ready",
            ],
        )
        self.assertFalse(blocked["checks"]["has_lane_ownership"])

    def test_mf_close_gate_dispatch_expectation_does_not_count_as_review_ready(self):
        from agent.governance import task_timeline

        contract = {
            "route_id": "route-lane-required",
            "route_context_hash": "sha256:lane-required",
            "required_lanes": [
                {"id": "bounded_implementation_subagent", "role": "implementation_worker"},
            ],
        }
        events = [
            {
                "event_type": "mf_subagent.dispatch",
                "phase": "bounded_subagent_dispatch",
                "actor": "codex-observer",
                "status": "accepted",
                "payload": {
                    "required_dispatch_key": "bounded_subagent_dispatch",
                    "bounded_implementation_subagent_id": "subagent-1",
                    "worker_role": "mf_sub",
                    "review_ready": True,
                    "stop_state": "review_ready",
                },
            },
            {
                "event_kind": "implementation",
                "phase": "implementation",
                "actor": "bounded-subagent-1",
                "status": "passed",
                "payload": {
                    "subagent_id": "subagent-1",
                    "worker_role": "mf_sub",
                    "changed_files": ["agent/governance/task_timeline.py"],
                },
            },
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        blocked = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(
            blocked["lane_ownership_gate"]["present_lane_ownership_ids"],
            ["bounded_implementation_subagent.dispatch"],
        )
        self.assertEqual(
            blocked["lane_ownership_gate"]["missing_lane_ownership_ids"],
            ["bounded_implementation_subagent.review_ready"],
        )

    def test_mf_close_gate_accepts_subagent_dispatch_and_review_ready(self):
        from agent.governance import task_timeline

        contract = {
            "route_id": "route-lane-required",
            "route_context_hash": "sha256:lane-required",
            "required_lanes": [
                {"id": "bounded_implementation_subagent", "role": "implementation_worker"},
            ],
        }
        events = [
            {
                "event_type": "mf_subagent.dispatch",
                "phase": "bounded_subagent_dispatch",
                "actor": "codex-observer",
                "status": "accepted",
                "payload": {
                    "required_dispatch_key": "bounded_subagent_dispatch",
                    "bounded_implementation_subagent_id": "subagent-1",
                    "worker_role": "mf_sub",
                },
            },
            {
                "event_kind": "implementation",
                "phase": "implementation",
                "actor": "bounded-subagent-1",
                "status": "passed",
                "payload": {
                    "subagent_id": "subagent-1",
                    "worker_role": "mf_sub",
                    "changed_files": ["agent/governance/task_timeline.py"],
                },
            },
            {
                "event_type": "mf_subagent.handoff",
                "phase": "review_ready",
                "actor": "bounded-subagent-1",
                "status": "accepted",
                "payload": {
                    "subagent_id": "subagent-1",
                    "worker_role": "mf_sub",
                    "review_ready": True,
                    "stop_state": "review_ready",
                },
            },
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        ready = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertTrue(ready["passed"], ready)
        self.assertEqual(
            ready["lane_ownership_gate"]["present_lane_ownership_ids"],
            [
                "bounded_implementation_subagent.dispatch",
                "bounded_implementation_subagent.review_ready",
            ],
        )

    def test_mf_close_gate_accepts_explicit_observer_direct_exception(self):
        from agent.governance import task_timeline

        contract = {
            "route_id": "route-lane-required",
            "route_context_hash": "sha256:lane-required",
            "required_evidence": ["bounded_implementation_subagent_id"],
            "blocked_actions": ["edit_files_as_observer_or_independent_reviewer"],
        }
        events = [
            {
                "event_kind": "implementation",
                "phase": "implementation",
                "actor": "codex-observer",
                "status": "accepted",
            },
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {
                "event_type": "mf.observer_direct_implementation_exception",
                "event_kind": "observer_direct_implementation_exception",
                "phase": "close",
                "actor": "operator",
                "status": "accepted",
                "payload": {
                    "route_id": "route-lane-required",
                    "reason": "operator approved a narrow same-worker correction",
                    "dirty_scope": {"files": ["agent/governance/task_timeline.py"]},
                    "approved_by": "operator-1",
                },
                "verification": {"operator_approved": True},
            },
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        ready = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertTrue(ready["passed"], ready)
        lane_gate = ready["lane_ownership_gate"]
        self.assertTrue(lane_gate["observer_direct_exception"]["accepted"])
        self.assertEqual(lane_gate["missing_lane_ownership_ids"], [])
        self.assertTrue(ready["contract_gate"]["passed"])
        self.assertEqual(
            ready["contract_gate"]["present_requirement_ids"],
            ["bounded_implementation_subagent_id"],
        )

    def test_observer_direct_exception_does_not_satisfy_unrelated_contract_evidence(self):
        from agent.governance import task_timeline

        contract = {
            "route_id": "route-lane-required",
            "route_context_hash": "sha256:lane-required",
            "required_evidence": [
                "bounded_implementation_subagent_id",
                "focused_tests",
            ],
            "blocked_actions": ["edit_files_as_observer_or_independent_reviewer"],
        }
        events = [
            {
                "event_kind": "implementation",
                "phase": "implementation",
                "actor": "codex-observer",
                "status": "accepted",
            },
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {
                "event_type": "mf.observer_direct_implementation_exception",
                "event_kind": "observer_direct_implementation_exception",
                "phase": "close",
                "actor": "operator",
                "status": "accepted",
                "payload": {
                    "route_id": "route-lane-required",
                    "reason": "operator approved a narrow same-worker correction",
                    "dirty_scope": {"files": ["agent/governance/task_timeline.py"]},
                    "approved_by": "operator-1",
                },
                "verification": {"operator_approved": True},
            },
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        blocked = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertTrue(blocked["lane_ownership_gate"]["passed"])
        self.assertEqual(
            blocked["contract_gate"]["present_requirement_ids"],
            ["bounded_implementation_subagent_id"],
        )
        self.assertEqual(
            blocked["contract_gate"]["missing_requirement_ids"],
            ["focused_tests"],
        )

    def test_mf_contract_gate_uses_observer_configured_required_evidence_ids(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-OBSERVER-POLICY",
            "evidence_requirements": [
                {
                    "id": "e2e_deferred_followup",
                    "required": False,
                    "phase": "integration",
                    "kind": "e2e_defer",
                },
            ],
            "test_scenario_policy": {
                "mode": "observer_configured",
                "decision": "new_scenario_required",
                "allowed_decisions": [
                    "none",
                    "reuse_existing",
                    "new_scenario_required",
                ],
                "reason": "observer requires contract-backed evidence",
                "required_evidence_ids": [
                    "observer_test_strategy",
                    "implementation_evidence",
                    "focused_tests",
                    "contract_gate_tests",
                    "docs_policy_update",
                    "e2e_deferred_followup",
                ],
                "e2e_decision": "e2e_deferred",
                "followup_backlog_id": "E2E-OBSERVER-TEST-SCENARIO-POLICY-20260524",
            },
        }
        base_events = [
            {
                "event_kind": "implementation",
                "phase": "implementation",
                "status": "accepted",
                "payload": {
                    "requirement_ids": [
                        "implementation_evidence",
                        "docs_policy_update",
                    ],
                },
            },
            {
                "event_kind": "verification",
                "phase": "verification",
                "status": "passed",
                "verification": {
                    "requirement_ids": [
                        "observer_test_strategy",
                        "focused_tests",
                        "contract_gate_tests",
                    ],
                },
            },
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        blocked = task_timeline.mf_close_gate_verification(base_events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(
            blocked["contract_gate"]["missing_requirement_ids"],
            ["e2e_deferred_followup"],
        )

        ready = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                *_route_context_consumption_events(),
                _route_context_qa_verification_event(),
                {
                    "event_kind": "verification",
                    "phase": "integration",
                    "status": "passed",
                    "verification": {
                        "contract_evidence": [
                            {
                                "requirement_id": "e2e_deferred_followup",
                                "status": "passed",
                                "followup_backlog_id": (
                                    "E2E-OBSERVER-TEST-SCENARIO-POLICY-20260524"
                                ),
                            }
                        ]
                    },
                },
            ],
            contract=contract,
        )

        self.assertTrue(ready["passed"], ready)
        self.assertTrue(ready["route_context_gate"]["passed"])
        self.assertEqual(
            ready["contract_gate"]["present_requirement_ids"],
            [
                "contract_gate_tests",
                "docs_policy_update",
                "e2e_deferred_followup",
                "focused_tests",
                "implementation_evidence",
                "observer_test_strategy",
            ],
        )

    def test_mf_parallel_template_exposes_optional_e2e_requirement(self):
        from agent.governance import task_timeline

        template_path = (
            Path(__file__).resolve().parents[1]
            / "governance"
            / "contract_templates"
            / "mf_parallel.v1.json"
        )
        template = json.loads(template_path.read_text(encoding="utf-8"))

        requirements = task_timeline.mf_contract_requirements(template)
        by_id = {item["id"]: item for item in requirements}
        policy = template["test_scenario_policy"]

        self.assertEqual(policy["mode"], "observer_configured")
        self.assertEqual(
            policy["allowed_decisions"],
            ["none", "reuse_existing", "new_scenario_required"],
        )
        self.assertIn("observer_test_strategy", policy["required_evidence_ids"])
        self.assertIn("focused_tests", by_id)
        self.assertIn("observer_test_strategy", by_id)
        self.assertIn("contract_gate_tests", by_id)
        self.assertIn("docs_policy_update", by_id)
        self.assertIn("e2e_deferred_followup", by_id)
        self.assertIn("integration_e2e", by_id)
        self.assertTrue(by_id["observer_test_strategy"]["required"])
        self.assertTrue(by_id["focused_tests"]["required"])
        self.assertFalse(by_id["contract_gate_tests"]["required"])
        self.assertFalse(by_id["docs_policy_update"]["required"])
        self.assertFalse(by_id["e2e_deferred_followup"]["required"])
        self.assertFalse(by_id["integration_e2e"]["required"])
        self.assertEqual(by_id["integration_e2e"]["kind"], "e2e")

    def test_db_migration_from_v41_adds_timeline_v2_columns_and_indexes(self):
        from agent.governance import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta (key, value) VALUES ('schema_version', '41');
            CREATE TABLE task_timeline_events (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id           TEXT NOT NULL,
                backlog_id           TEXT NOT NULL DEFAULT '',
                mf_id                TEXT NOT NULL DEFAULT '',
                task_id              TEXT NOT NULL DEFAULT '',
                attempt_num          INTEGER NOT NULL DEFAULT 0,
                event_type           TEXT NOT NULL,
                actor                TEXT NOT NULL DEFAULT '',
                status               TEXT NOT NULL DEFAULT '',
                payload_json         TEXT NOT NULL DEFAULT '{}',
                verification_json    TEXT NOT NULL DEFAULT '{}',
                artifact_refs_json   TEXT NOT NULL DEFAULT '{}',
                trace_id             TEXT NOT NULL DEFAULT '',
                commit_sha           TEXT NOT NULL DEFAULT '',
                created_at           TEXT NOT NULL
            );
        """)

        db._ensure_schema(conn)

        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(task_timeline_events)").fetchall()
        }
        self.assertIn("phase", columns)
        self.assertIn("event_kind", columns)
        self.assertIn("scenario_id", columns)
        self.assertIn("correlation_id", columns)
        self.assertIn("schema_version", columns)
        indexes = {
            str(row["name"])
            for row in conn.execute("PRAGMA index_list(task_timeline_events)").fetchall()
        }
        self.assertIn("idx_task_timeline_scenario", indexes)
        self.assertIn("idx_task_timeline_kind", indexes)
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        self.assertEqual(version, str(db.SCHEMA_VERSION))
        conn.close()

    def test_task_timeline_list_handler_filters_by_backlog_id_query(self):
        from agent.governance import server, task_timeline

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-a",
            backlog_id="BUG-A",
            event_type="task.started",
            actor="worker-a",
            trace_id="trace-a",
            phase="implement",
            event_kind="observation",
            scenario_id="scn-handler",
            correlation_id="corr-handler",
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-b",
            backlog_id="BUG-A",
            event_type="task.completed",
            actor="worker-b",
            trace_id="trace-b",
            phase="gate",
            event_kind="gate_result",
            scenario_id="scn-handler",
            correlation_id="corr-handler",
            decision="approved",
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-c",
            backlog_id="BUG-B",
            event_type="task.completed",
            actor="worker-c",
            trace_id="trace-c",
        )
        self.conn.commit()

        result = server.handle_task_timeline_list(_ctx({"backlog_id": "BUG-A"}))

        self.assertTrue(result["ok"])
        self.assertEqual(result["project_id"], "proj")
        self.assertEqual(result["task_id"], "")
        self.assertEqual(result["backlog_id"], "BUG-A")
        self.assertEqual(result["trace_id"], "")
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [event["task_id"] for event in result["events"]],
            ["task-a", "task-b"],
        )

        filtered = server.handle_task_timeline_list(
            _ctx({
                "backlog_id": "BUG-A",
                "task_id": "task-b",
                "trace_id": "trace-b",
                "phase": "gate",
                "event_kind": "gate_result",
                "scenario_id": "scn-handler",
                "correlation_id": "corr-handler",
                "decision": "approved",
                "limit": ["5"],
            })
        )
        self.assertEqual(filtered["task_id"], "task-b")
        self.assertEqual(filtered["trace_id"], "trace-b")
        self.assertEqual(filtered["phase"], "gate")
        self.assertEqual(filtered["event_kind"], "gate_result")
        self.assertEqual(filtered["scenario_id"], "scn-handler")
        self.assertEqual(filtered["correlation_id"], "corr-handler")
        self.assertEqual(filtered["decision"], "approved")
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["events"][0]["task_id"], "task-b")

    def test_backlog_current_task_endpoint_uses_timeline_fallback(self):
        from agent.governance import server, task_timeline

        self.conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, runtime_state, current_task_id,
                chain_trigger_json, created_at, updated_at)
               VALUES (?, ?, 'OPEN', 'P1', '', '', ?, '2026-06-07T00:00:00Z',
                       '2026-06-07T00:00:00Z')""",
            ("BUG-CURRENT", "Current task from timeline", "{}"),
        )
        event = task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-CURRENT",
            task_id="task-current",
            event_type="mf.subagent.startup",
            phase="startup",
            event_kind="mf_subagent.startup",
            actor="mf-sub",
            status="running",
        )
        self.conn.commit()

        result = server.handle_backlog_current_task(_ctx({"limit": "10"}))

        self.assertTrue(result["ok"])
        self.assertTrue(result["active"])
        self.assertEqual(result["source"], "task_timeline")
        self.assertEqual(result["backlog_id"], "BUG-CURRENT")
        self.assertEqual(result["task_id"], "task-current")
        self.assertEqual(result["latest_event"]["id"], event["id"])
        self.assertEqual(result["latest_event"]["status"], "running")
        self.assertEqual(result["active_count"], 1)
        self.assertEqual(result["single_active_task"]["active_count"], 1)

    def test_backlog_current_task_endpoint_prefers_newer_timeline_over_stale_runtime(self):
        from agent.governance import server, task_timeline

        self.conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, runtime_state, current_task_id,
                chain_trigger_json, created_at, updated_at)
               VALUES (?, ?, 'OPEN', 'P1', 'manual_fix_in_progress', 'task-stale',
                       ?, '2026-06-07T00:00:00Z', '2026-06-07T00:00:00Z')""",
            ("BUG-STALE", "Stale runtime task", "{}"),
        )
        self.conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, runtime_state, current_task_id,
                chain_trigger_json, created_at, updated_at)
               VALUES (?, ?, 'OPEN', 'P0', '', '',
                       ?, '2026-06-07T00:00:00Z', '2026-06-07T00:00:00Z')""",
            ("BUG-LIVE", "Live timeline task", "{}"),
        )
        event = task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-LIVE",
            task_id="task-live",
            event_type="mf.subagent.startup",
            phase="startup",
            event_kind="mf_subagent.startup",
            actor="mf-sub",
            status="running",
        )
        self.conn.commit()

        result = server.handle_backlog_current_task(_ctx({"limit": "10"}))

        self.assertTrue(result["active"])
        self.assertEqual(result["source"], "task_timeline")
        self.assertEqual(result["backlog_id"], "BUG-LIVE")
        self.assertEqual(result["task_id"], "task-live")
        self.assertEqual(result["latest_event"]["id"], event["id"])
        self.assertEqual(result["active_count"], 2)
        self.assertEqual(result["active_backlog"][0]["bug_id"], "BUG-LIVE")

    def test_backlog_timeline_gate_precheck_matches_close_gate_evidence(self):
        from agent.governance import server, task_timeline

        server.handle_backlog_upsert(
            _ctx(
                path_params={"bug_id": "BUG-MF-PRECHECK"},
                body={
                    "title": "MF timeline precheck",
                    "status": "OPEN",
                    "mf_type": "observer_hotfix",
                    "force_admit": True,
                },
                method="POST",
            )
        )

        for kind in ("implementation", "verification"):
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id="BUG-MF-PRECHECK",
                event_type=f"mf.{kind}",
                phase=kind,
                event_kind=kind,
                status="accepted",
            )
        self.conn.commit()

        blocked = server.handle_backlog_timeline_gate(
            _ctx({"include_events": "true"}, path_params={"bug_id": "BUG-MF-PRECHECK"})
        )

        self.assertTrue(blocked["ok"])
        self.assertTrue(blocked["applicable"])
        self.assertFalse(blocked["can_close"])
        self.assertEqual(blocked["timeline_gate"]["missing_event_kinds"], ["close_ready"])
        self.assertEqual(blocked["event_count"], 2)
        self.assertEqual(len(blocked["events"]), 2)

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-PRECHECK",
            event_type="mf.close_ready",
            phase="close",
            event_kind="close_ready",
            status="accepted",
        )
        self.conn.commit()

        ready = server.handle_backlog_timeline_gate(
            _ctx(path_params={"bug_id": "BUG-MF-PRECHECK"})
        )
        self.assertTrue(ready["can_close"])
        self.assertTrue(ready["timeline_gate"]["passed"])
        self.assertEqual(
            ready["timeline_gate"]["present_event_kinds"],
            ["close_ready", "implementation", "verification"],
        )

    def test_observer_hotfix_aliases_upsert_as_mf_applicable_rows(self):
        from agent.governance import server

        for index, alias in enumerate(("observer_hotfix", "observer-hotfix"), start=1):
            bug_id = f"BUG-MF-ALIAS-{index}"
            server.handle_backlog_upsert(
                _ctx(
                    path_params={"bug_id": bug_id},
                    body={
                        "title": "MF alias precheck",
                        "status": "OPEN",
                        "mf_type": alias,
                        "force_admit": True,
                    },
                    method="POST",
                )
            )

            row = self.conn.execute(
                "SELECT mf_type, bypass_policy_json FROM backlog_bugs WHERE bug_id = ?",
                (bug_id,),
            ).fetchone()
            self.assertEqual(row["mf_type"], "chain_rescue")
            self.assertIn("chain_rescue", row["bypass_policy_json"])

            precheck = server.handle_backlog_timeline_gate(
                _ctx(path_params={"bug_id": bug_id})
            )
            self.assertTrue(precheck["applicable"], precheck)
            self.assertFalse(precheck["can_close"])
            self.assertEqual(
                precheck["timeline_gate"]["missing_event_kinds"],
                ["close_ready", "implementation", "verification"],
            )

    def test_backlog_timeline_gate_precheck_uses_instantiated_contract(self):
        from agent.governance import server, task_timeline

        server.handle_backlog_upsert(
            _ctx(
                path_params={"bug_id": "BUG-MF-CONTRACT-PRECHECK"},
                body={
                    "title": "MF contract precheck",
                    "status": "OPEN",
                    "mf_type": "observer_hotfix",
                    "force_admit": True,
                    "chain_trigger_json": {
                        "parallel_contract": {
                            "template_id": "mf_parallel.v1",
                            "contract_instance_id": "BUG-MF-CONTRACT-PRECHECK",
                            "evidence_requirements": [
                                {"id": "unit_tests", "required": True, "phase": "verification"},
                                {"id": "dashboard_e2e", "required": True, "phase": "integration", "kind": "e2e"},
                            ],
                        }
                    },
                },
                method="POST",
            )
        )

        for kind in ("implementation", "close_ready"):
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id="BUG-MF-CONTRACT-PRECHECK",
                event_type=f"mf.{kind}",
                phase=kind,
                event_kind=kind,
                status="accepted",
            )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-CONTRACT-PRECHECK",
            event_type="mf.verification",
            phase="verification",
            event_kind="verification",
            status="passed",
            verification={"requirement_id": "unit_tests"},
        )
        self.conn.commit()

        blocked = server.handle_backlog_timeline_gate(
            _ctx(path_params={"bug_id": "BUG-MF-CONTRACT-PRECHECK"})
        )

        self.assertFalse(blocked["can_close"])
        self.assertEqual(
            blocked["timeline_gate"]["contract_gate"]["missing_requirement_ids"],
            ["dashboard_e2e"],
        )

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-CONTRACT-PRECHECK",
            event_type="mf.integration.e2e",
            phase="integration",
            event_kind="verification",
            status="passed",
            verification={
                "contract_evidence": [
                    {
                        "requirement_id": "dashboard_e2e",
                        "status": "passed",
                        "command": "npm run e2e:semantic -- --project fixture --probe",
                    }
                ]
            },
        )
        for event in [
            *_route_context_consumption_events(),
            _route_context_qa_verification_event(),
        ]:
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id="BUG-MF-CONTRACT-PRECHECK",
                event_type=f"mf.{event['event_kind']}",
                phase=event.get("phase", ""),
                event_kind=event.get("event_kind", ""),
                status=event.get("status", ""),
                payload=event.get("payload"),
                verification=event.get("verification"),
                artifact_refs=event.get("artifact_refs"),
            )
        self.conn.commit()

        ready = server.handle_backlog_timeline_gate(
            _ctx(path_params={"bug_id": "BUG-MF-CONTRACT-PRECHECK"})
        )
        self.assertTrue(ready["can_close"], ready)
        self.assertTrue(ready["timeline_gate"]["contract_gate"]["passed"])

    def test_backlog_close_handler_loads_instantiated_contract(self):
        from agent.governance import server, task_timeline
        from agent.governance.errors import GovernanceError

        server.handle_backlog_upsert(
            _ctx(
                path_params={"bug_id": "BUG-MF-CONTRACT-CLOSE"},
                body={
                    "title": "MF contract close",
                    "status": "OPEN",
                    "mf_type": "observer_hotfix",
                    "force_admit": True,
                    "chain_trigger_json": {
                        "parallel_contract": {
                            "template_id": "mf_parallel.v1",
                            "contract_instance_id": "BUG-MF-CONTRACT-CLOSE",
                            "evidence_requirements": [
                                {"id": "unit_tests", "required": True, "phase": "verification"},
                                {"id": "dashboard_e2e", "required": True, "phase": "integration", "kind": "e2e"},
                            ],
                        }
                    },
                },
                method="POST",
            )
        )

        for kind in ("implementation", "close_ready"):
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id="BUG-MF-CONTRACT-CLOSE",
                event_type=f"mf.{kind}",
                phase=kind,
                event_kind=kind,
                status="accepted",
            )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-CONTRACT-CLOSE",
            event_type="mf.verification",
            phase="verification",
            event_kind="verification",
            status="passed",
            verification={"requirement_id": "unit_tests"},
        )
        self.conn.commit()

        with self.assertRaises(GovernanceError) as raised:
            server.handle_backlog_close(
                _ctx(
                    path_params={"bug_id": "BUG-MF-CONTRACT-CLOSE"},
                    body={
                        "actor": "observer",
                        "route_waiver": _route_waiver("backlog_close", "BUG-MF-CONTRACT-CLOSE"),
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "mf_timeline_gate_failed")
        self.assertIn("dashboard_e2e", str(raised.exception))


# ---------------------------------------------------------------------------
# Observer cannot self-clear judge blockers (regression #3092)
# ---------------------------------------------------------------------------
def test_observer_self_cleared_judge_blocker_is_rejected():
    from agent.governance import task_timeline

    observer_accept = {
        "id": 1,
        "event_kind": "blocker_resolution",
        "actor": "observer",
        "status": "accepted",
        "payload": {"finding_kind": "judge_finding"},
    }
    gate = task_timeline.mf_blocker_resolution_gate_verification([observer_accept])
    assert gate["passed"] is False
    rejected = gate["rejected_observer_resolutions"]
    assert rejected and rejected[0]["reason"] == "observer_self_cleared_judge_blocker"
    assert rejected[0]["forced_status"] == "pending_judge_review"


def test_observer_judge_blocker_proposal_is_forced_pending_review():
    from agent.governance import task_timeline

    observer_propose = {
        "id": 1,
        "event_kind": "blocker_resolution",
        "actor": "observer",
        "status": "proposed",
        "payload": {"finding_kind": "judge_finding"},
    }
    gate = task_timeline.mf_blocker_resolution_gate_verification([observer_propose])
    # A proposal (not an accept) does not block the close gate, but is forced to
    # pending_judge_review.
    assert gate["passed"] is True
    forced = gate["forced_pending_judge_review"]
    assert forced and forced[0]["forced_status"] == "pending_judge_review"


def test_observer_p0_priority_downgrade_on_judge_finding_is_rejected():
    from agent.governance import task_timeline

    downgrade = {
        "id": 1,
        "event_kind": "blocker_resolution",
        "actor": "observer",
        "status": "proposed",
        "payload": {
            "finding_kind": "judge_finding",
            "from_priority": "P0",
            "to_priority": "P2",
        },
    }
    gate = task_timeline.mf_blocker_resolution_gate_verification([downgrade])
    assert gate["passed"] is False
    rejected = gate["rejected_observer_resolutions"][0]
    assert rejected["reason"] == "observer_safety_priority_downgrade_by_fiat"
    assert rejected["safety_priority_downgrade"] == {"from": "p0", "to": "p2"}


def test_judge_actor_may_accept_judge_blocker():
    from agent.governance import task_timeline

    judge_accept = {
        "id": 1,
        "event_kind": "blocker_resolution",
        "actor": "judge",
        "status": "accepted",
        "payload": {"finding_kind": "judge_finding"},
    }
    gate = task_timeline.mf_blocker_resolution_gate_verification([judge_accept])
    assert gate["passed"] is True
    assert gate["judge_accepted_resolutions"]


# ---------------------------------------------------------------------------
# Route repair/supersede invalidates stale evidence for close (#3093/#3094)
# ---------------------------------------------------------------------------
def test_route_repair_invalidates_stale_read_receipt_and_startup_for_close():
    from agent.governance import task_timeline

    canonical = dict(ROUTE_IDENTITY)
    stale = {
        "route_context_hash": "sha256:stale-route-context",
        "prompt_contract_id": "rprompt-stale-route",
        "prompt_contract_hash": "sha256:stale-prompt-contract",
    }
    cleanup = {
        "id": 1,
        "event_kind": "route_identity_cleanup",
        "phase": "identity_recovery",
        "status": "accepted",
        "payload": {"route_identity_cleanup": {**canonical}},
    }
    stale_read_receipt = {
        "id": 2,
        "event_kind": "mf_subagent_read_receipt",
        "status": "accepted",
        "payload": {**stale, "read_receipt_hash": "sha256:stale-rr"},
    }
    stale_startup = {
        "id": 3,
        "event_kind": "mf_subagent_startup",
        "status": "passed",
        "payload": {**stale, "fence_token": "fence-stale"},
    }
    gate = task_timeline.mf_stale_route_evidence_gate_verification(
        [cleanup, stale_read_receipt, stale_startup]
    )
    assert gate["route_identity_cleanup_applied"] is True
    superseded_kinds = {
        item["event_kind"] for item in gate["superseded_close_evidence"]
    }
    assert "mf_subagent_read_receipt" in superseded_kinds
    assert "mf_subagent_startup" in superseded_kinds
    assert gate["passed"] is False


def test_canonical_route_evidence_after_repair_is_not_superseded():
    from agent.governance import task_timeline

    canonical = dict(ROUTE_IDENTITY)
    cleanup = {
        "id": 1,
        "event_kind": "route_identity_cleanup",
        "phase": "identity_recovery",
        "status": "accepted",
        "payload": {"route_identity_cleanup": {**canonical}},
    }
    canonical_read_receipt = {
        "id": 2,
        "event_kind": "mf_subagent_read_receipt",
        "status": "accepted",
        "payload": {**canonical, "read_receipt_hash": "sha256:fresh-rr"},
    }
    gate = task_timeline.mf_stale_route_evidence_gate_verification(
        [cleanup, canonical_read_receipt]
    )
    assert gate["passed"] is True
    assert gate["superseded_close_evidence"] == []


# ---------------------------------------------------------------------------
# Rerecorded-under-canonical exemption (AC-STALE-ROUTE-GATE-RERECORD-EXEMPTION-20260610)
# ---------------------------------------------------------------------------
def test_all_stale_kinds_rerecorded_under_canonical_passes():
    """All stale-identity events have canonical counterparts → passed=True, rerecorded pairs listed, superseded empty."""
    from agent.governance import task_timeline

    canonical = dict(ROUTE_IDENTITY)
    stale = {
        "route_context_hash": "sha256:stale-route-context",
        "prompt_contract_id": "rprompt-stale-route",
        "prompt_contract_hash": "sha256:stale-prompt-contract",
    }
    cleanup = {
        "id": 1,
        "event_kind": "route_identity_cleanup",
        "phase": "identity_recovery",
        "status": "accepted",
        "payload": {"route_identity_cleanup": {**canonical}},
    }
    # Stale-identity events (old route)
    stale_read_receipt = {
        "id": 10,
        "event_kind": "mf_subagent_read_receipt",
        "status": "accepted",
        "payload": {**stale, "read_receipt_hash": "sha256:stale-rr"},
    }
    stale_startup = {
        "id": 11,
        "event_kind": "mf_subagent_startup",
        "status": "passed",
        "payload": {**stale, "fence_token": "fence-stale"},
    }
    stale_close_ready = {
        "id": 12,
        "event_kind": "close_ready",
        "status": "accepted",
        "payload": {**stale},
    }
    stale_dispatch = {
        "id": 13,
        "event_kind": "mf_subagent_dispatch",
        "status": "ok",
        "payload": {**stale},
    }
    # Canonical rerecords of all the above kinds
    canonical_read_receipt = {
        "id": 20,
        "event_kind": "mf_subagent_read_receipt",
        "status": "accepted",
        "payload": {**canonical, "read_receipt_hash": "sha256:canon-rr"},
    }
    canonical_startup = {
        "id": 21,
        "event_kind": "mf_subagent_startup",
        "status": "passed",
        "payload": {**canonical, "fence_token": "fence-canon"},
    }
    canonical_close_ready = {
        "id": 22,
        "event_kind": "close_ready",
        "status": "accepted",
        "payload": {**canonical},
    }
    canonical_dispatch = {
        "id": 23,
        "event_kind": "mf_subagent_dispatch",
        "status": "ok",
        "payload": {**canonical},
    }

    events = [
        cleanup,
        stale_read_receipt, stale_startup, stale_close_ready, stale_dispatch,
        canonical_read_receipt, canonical_startup, canonical_close_ready, canonical_dispatch,
    ]
    gate = task_timeline.mf_stale_route_evidence_gate_verification(events)

    assert gate["passed"] is True, f"Expected passed but got: {gate}"
    assert gate["superseded_close_evidence"] == [], gate["superseded_close_evidence"]
    rerecorded_ids = {item["superseded_id"] for item in gate["rerecorded_close_evidence"]}
    assert rerecorded_ids == {10, 11, 12, 13}, rerecorded_ids
    for item in gate["rerecorded_close_evidence"]:
        assert item["reason"] == "superseded_route_identity_evidence_rerecorded"
        assert item["canonical_event_ids"]


def test_partial_rerecord_blocks_missing_kind():
    """Canonical rerecord missing for one kind → passed=False, only that kind in superseded."""
    from agent.governance import task_timeline

    canonical = dict(ROUTE_IDENTITY)
    stale = {
        "route_context_hash": "sha256:stale-route-context",
        "prompt_contract_id": "rprompt-stale-route",
        "prompt_contract_hash": "sha256:stale-prompt-contract",
    }
    cleanup = {
        "id": 1,
        "event_kind": "route_identity_cleanup",
        "phase": "identity_recovery",
        "status": "accepted",
        "payload": {"route_identity_cleanup": {**canonical}},
    }
    stale_read_receipt = {
        "id": 10,
        "event_kind": "mf_subagent_read_receipt",
        "status": "accepted",
        "payload": {**stale, "read_receipt_hash": "sha256:stale-rr"},
    }
    stale_startup = {
        "id": 11,
        "event_kind": "mf_subagent_startup",
        "status": "passed",
        "payload": {**stale, "fence_token": "fence-stale"},
    }
    # Only canonical read_receipt is present; startup has no canonical counterpart.
    canonical_read_receipt = {
        "id": 20,
        "event_kind": "mf_subagent_read_receipt",
        "status": "accepted",
        "payload": {**canonical, "read_receipt_hash": "sha256:canon-rr"},
    }

    events = [cleanup, stale_read_receipt, stale_startup, canonical_read_receipt]
    gate = task_timeline.mf_stale_route_evidence_gate_verification(events)

    assert gate["passed"] is False, f"Expected blocked but got: {gate}"
    superseded_kinds = {item["event_kind"] for item in gate["superseded_close_evidence"]}
    assert "mf_subagent_startup" in superseded_kinds, superseded_kinds
    assert "mf_subagent_read_receipt" not in superseded_kinds, superseded_kinds
    rerecorded_ids = {item["superseded_id"] for item in gate["rerecorded_close_evidence"]}
    assert 10 in rerecorded_ids, rerecorded_ids
    assert 11 not in rerecorded_ids, rerecorded_ids


def test_no_cleanup_applied_both_lists_empty():
    """No route_identity_cleanup in events → gate passes and both lists are empty (unchanged behavior)."""
    from agent.governance import task_timeline

    canonical = dict(ROUTE_IDENTITY)
    read_receipt = {
        "id": 1,
        "event_kind": "mf_subagent_read_receipt",
        "status": "accepted",
        "payload": {**canonical, "read_receipt_hash": "sha256:rr"},
    }
    startup = {
        "id": 2,
        "event_kind": "mf_subagent_startup",
        "status": "passed",
        "payload": {**canonical, "fence_token": "fence-a"},
    }

    gate = task_timeline.mf_stale_route_evidence_gate_verification([read_receipt, startup])

    assert gate["passed"] is True, f"Expected passed but got: {gate}"
    assert gate["superseded_close_evidence"] == []
    assert gate["rerecorded_close_evidence"] == []


# ---------------------------------------------------------------------------
# Close-gate cross-ref rejection (regression #3090)
# ---------------------------------------------------------------------------
def test_close_gate_rejects_cross_backlog_evidence_ref():
    from agent.governance import task_timeline

    row_identity = {"backlog_id": "AC-A"}
    cross_backlog = {
        "id": 1,
        "event_kind": "close_ready",
        "status": "accepted",
        "payload": {"backlog_id": "AC-B"},
    }
    gate = task_timeline.mf_close_cross_ref_gate_verification(
        [cross_backlog], row_identity
    )
    assert gate["passed"] is False
    rejected = gate["rejected_cross_ref_evidence"][0]
    assert rejected["reason"] == "cross_ref_identity_mismatch"
    assert rejected["mismatches"]["backlog_id"] == {
        "expected": "AC-A",
        "actual": "AC-B",
    }


def test_close_gate_accepts_cross_ref_with_bridge_event():
    from agent.governance import task_timeline

    row_identity = {"backlog_id": "AC-A"}
    cross_backlog = {
        "id": 1,
        "event_kind": "close_ready",
        "status": "accepted",
        "payload": {"backlog_id": "AC-B"},
    }
    bridge = {
        "id": 2,
        "event_kind": "lineage_bridge",
        "status": "accepted",
        "payload": {"bridged_backlog_id": "AC-B"},
    }
    gate = task_timeline.mf_close_cross_ref_gate_verification(
        [cross_backlog, bridge], row_identity
    )
    assert gate["passed"] is True


def test_close_gate_accepts_same_backlog_scope_evidence():
    from agent.governance import task_timeline

    row_identity = {"backlog_id": "AC-A", "scope": "agent/governance"}
    same_row = {
        "id": 1,
        "event_kind": "close_ready",
        "status": "accepted",
        "payload": {"backlog_id": "AC-A", "scope": "agent/governance"},
    }
    gate = task_timeline.mf_close_cross_ref_gate_verification(
        [same_row], row_identity
    )
    assert gate["passed"] is True
    assert gate["rejected_cross_ref_evidence"] == []


# ---------------------------------------------------------------------------
# Multi-lane bridge: one row implemented by >=2 bounded mf_sub lanes under one
# observer command + route. [AC-CLOSE-CROSS-REF-MULTI-LANE-BRIDGE-20260609]
# ---------------------------------------------------------------------------
def _multi_lane_close_evidence():
    """Two bounded lanes (different task_ids) + a row-level aggregate, all under
    one backlog_id/project_id/route/observer-command."""

    common_route = {
        "route_id": "route-1",
        "route_context_hash": "rch-1",
        "prompt_contract_id": "pc-1",
        "observer_command_id": "cmd-1",
    }
    lane_canonical = {
        "id": 1,
        "event_kind": "mf_subagent_startup",
        "status": "accepted",
        "backlog_id": "AC-A",
        "project_id": "aming-claw",
        "task_id": "task-canonical",
        "payload": dict(common_route, scope="agent/governance"),
    }
    lane_sibling = {
        "id": 2,
        "event_kind": "mf_subagent_startup",
        "status": "accepted",
        "backlog_id": "AC-A",
        "project_id": "aming-claw",
        "task_id": "task-sibling",
        "payload": dict(common_route, scope="agent/tests"),
    }
    row_aggregate = {
        "id": 3,
        "event_kind": "close_ready",
        "status": "accepted",
        "backlog_id": "AC-A",
        "project_id": "aming-claw",
        "task_id": "",
        "payload": dict(common_route),
    }
    return common_route, lane_canonical, lane_sibling, row_aggregate


def test_close_gate_multi_lane_bridge_consumes_bridged_identities():
    """(a) One row + two lanes + accepted multi-lane bridge declaring both lanes
    -> cross_ref gate passes with no rejected evidence."""

    from agent.governance import task_timeline

    common_route, lane_canonical, lane_sibling, row_aggregate = (
        _multi_lane_close_evidence()
    )
    # The canonical lane scope is the row scope; the sibling lane scope differs.
    # Without the bridge the sibling lane would be rejected (see test (b)).
    row_identity = {
        "backlog_id": "AC-A",
        "route_id": "route-1",
        "prompt_contract_id": "pc-1",
        "scope": "agent/governance",
    }
    bridge = {
        "id": 4,
        "event_kind": "cross_ref_lineage_bridge",
        "status": "accepted",
        "backlog_id": "AC-A",
        "project_id": "aming-claw",
        "payload": dict(
            common_route,
            bridged_identities=[
                {
                    "backlog_id": "AC-A",
                    "project_id": "aming-claw",
                    "task_id": "task-canonical",
                },
                {
                    "backlog_id": "AC-A",
                    "project_id": "aming-claw",
                    "task_id": "task-sibling",
                },
            ],
        ),
    }
    gate = task_timeline.mf_close_cross_ref_gate_verification(
        [lane_canonical, lane_sibling, row_aggregate, bridge], row_identity
    )
    assert gate["passed"] is True
    assert gate["rejected_cross_ref_evidence"] == []
    # Both lanes + the row-level aggregate (task_id="") are admitted.
    assert "AC-A|aming-claw|task-canonical" in gate["bridged_lane_membership"]
    assert "AC-A|aming-claw|task-sibling" in gate["bridged_lane_membership"]
    assert "AC-A|aming-claw|" in gate["bridged_lane_membership"]


def test_close_gate_multi_lane_without_bridge_still_blocks():
    """(b) One row + two lanes but NO accepted bridge -> the non-canonical lane's
    evidence is still rejected as a cross_ref mismatch."""

    from agent.governance import task_timeline

    _, lane_canonical, lane_sibling, row_aggregate = _multi_lane_close_evidence()
    row_identity = {
        "backlog_id": "AC-A",
        "scope": "agent/governance",
    }
    gate = task_timeline.mf_close_cross_ref_gate_verification(
        [lane_canonical, lane_sibling, row_aggregate], row_identity
    )
    assert gate["passed"] is False
    rejected_scopes = {
        tuple(sorted(item["mismatches"]))
        for item in gate["rejected_cross_ref_evidence"]
    }
    # The sibling lane's scope differs from the row scope and is rejected.
    assert any("scope" in mismatch for mismatch in rejected_scopes)
    assert gate["bridged_lane_membership"] == []


def test_close_gate_foreign_row_evidence_still_rejected_3090():
    """(c) Foreign backlog_id/route with no in-scope bridge -> still rejected.
    Preserves the #3090 cross-ref identity protection: a bridge that declares a
    DIFFERENT backlog_id does not admit the foreign evidence."""

    from agent.governance import task_timeline

    row_identity = {"backlog_id": "AC-A", "project_id": "aming-claw"}
    foreign = {
        "id": 1,
        "event_kind": "close_ready",
        "status": "accepted",
        "backlog_id": "AC-FOREIGN",
        "project_id": "other-project",
        "task_id": "task-foreign",
        "payload": {"backlog_id": "AC-FOREIGN"},
    }
    # An accepted bridge exists, but it only declares a sibling for a DIFFERENT
    # backlog row, so the foreign evidence is NOT covered.
    bridge = {
        "id": 2,
        "event_kind": "cross_ref_lineage_bridge",
        "status": "accepted",
        "backlog_id": "AC-A",
        "project_id": "aming-claw",
        "payload": {
            "bridged_identities": [
                {
                    "backlog_id": "AC-A",
                    "project_id": "aming-claw",
                    "task_id": "task-sibling",
                }
            ]
        },
    }
    gate = task_timeline.mf_close_cross_ref_gate_verification(
        [foreign, bridge], row_identity
    )
    assert gate["passed"] is False
    rejected = gate["rejected_cross_ref_evidence"][0]
    assert rejected["reason"] == "cross_ref_identity_mismatch"
    assert rejected["mismatches"]["backlog_id"] == {
        "expected": "AC-A",
        "actual": "AC-FOREIGN",
    }


def test_close_gate_bridge_declaring_foreign_backlog_still_blocks_3090():
    """(d) ADVERSARIAL #3090: a bridge whose payload.bridged_identities[]
    EXPLICITLY declares a FOREIGN {backlog_id, project_id, task_id} PLUS matching
    foreign close evidence must STILL be blocked when the trusted row identity is
    AC-A. A bridge declaring a foreign backlog/project must never admit foreign
    evidence — neither via the membership consumer nor via the legacy bridged
    skip scraper. This is the regression whose absence let the hole through."""

    from agent.governance import task_timeline

    # Trusted row identity = AC-A / aming-claw.
    row_identity = {"backlog_id": "AC-A", "project_id": "aming-claw"}
    # Foreign close evidence whose backlog_id/project_id differ from the row.
    foreign_close = {
        "id": 1,
        "event_kind": "close_ready",
        "status": "accepted",
        "backlog_id": "AC-FOREIGN",
        "project_id": "other-project",
        "task_id": "task-foreign",
        "payload": {
            "backlog_id": "AC-FOREIGN",
            "project_id": "other-project",
        },
    }
    # An accepted bridge that EXPLICITLY declares the foreign lane as a sibling.
    bridge = {
        "id": 2,
        "event_kind": "cross_ref_lineage_bridge",
        "status": "accepted",
        "backlog_id": "AC-A",
        "project_id": "aming-claw",
        "payload": {
            "bridged_identities": [
                {
                    "backlog_id": "AC-FOREIGN",
                    "project_id": "other-project",
                    "task_id": "task-foreign",
                }
            ]
        },
    }
    gate = task_timeline.mf_close_cross_ref_gate_verification(
        [foreign_close, bridge], row_identity
    )
    # Floor holds: the foreign evidence is rejected despite the bridge.
    assert gate["passed"] is False
    rejected = gate["rejected_cross_ref_evidence"][0]
    assert rejected["reason"] == "cross_ref_identity_mismatch"
    assert rejected["mismatches"]["backlog_id"] == {
        "expected": "AC-A",
        "actual": "AC-FOREIGN",
    }
    # The foreign lane must NOT have leaked into the accepted membership set, and
    # the legacy bridged skip scraper must NOT have recorded the foreign backlog.
    assert gate["bridged_lane_membership"] == []
    assert "backlog_id=AC-FOREIGN" not in gate["bridged_identities"]


def test_close_gate_blocks_when_observer_self_clears_judge_blocker():
    from agent.governance import task_timeline

    contract = {
        "template_id": "mf_parallel.v1",
        "contract_instance_id": "BUG-CLOSE-JUDGE-SELFCLEAR",
    }
    events = [
        *_route_context_consumption_events(),
        _route_context_qa_verification_event(),
        {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
        {"event_kind": "verification", "phase": "verification", "status": "passed"},
        {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        {
            "event_kind": "blocker_resolution",
            "actor": "observer",
            "status": "accepted",
            "payload": {"finding_kind": "judge_finding"},
        },
    ]
    gate = task_timeline.mf_close_gate_verification(events, contract=contract)
    assert gate["passed"] is False
    assert gate["blocker_resolution_gate"]["passed"] is False
    assert "judge_blocker_resolution" in gate["missing_evidence_groups"]["groups"]


# ---------------------------------------------------------------------------
# Close-gate evidence integrity (AC-CLOSE-GATE-EVIDENCE-INTEGRITY-20260609)
# ---------------------------------------------------------------------------

_CLOSE_BASE_EVENTS = [
    {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
    {"event_kind": "verification", "phase": "verification", "status": "passed"},
    {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
]


def test_close_gate_rejects_when_cited_approval_excludes_backlog_close():
    """Criterion 1: a close whose own cited approval forbids close is rejected."""
    from agent.governance import task_timeline

    events = [
        {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
        {"event_kind": "verification", "phase": "verification", "status": "passed"},
        {
            "event_kind": "close_ready",
            "phase": "close",
            "status": "accepted",
            "payload": {
                "approval_text": "review_ready only / does not authorize backlog_close",
            },
        },
    ]
    gate = task_timeline.mf_close_gate_verification(events)
    assert gate["passed"] is False, gate
    scope = gate["approval_scope_gate"]
    assert scope["passed"] is False
    assert scope["status"] == "blocked"
    assert scope["approvals_excluding_close"], scope
    assert "approval_scope" in gate["missing_evidence_groups"]["groups"]


def test_close_gate_approval_exclusion_converted_to_waiver_state():
    """Criterion 1: an explicit recorded close-waiver turns the block into a
    visible waiver state instead of a silent success."""
    from agent.governance import task_timeline

    events = [
        {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
        {"event_kind": "verification", "phase": "verification", "status": "passed"},
        {
            "event_kind": "close_ready",
            "phase": "close",
            "status": "accepted",
            "payload": {"approval_text": "review_ready only / does not authorize backlog_close"},
        },
        {
            "event_kind": "backlog_close_waiver",
            "phase": "close",
            "status": "accepted",
            "payload": {"reason": "operator explicit override of review_ready scope"},
        },
    ]
    gate = task_timeline.mf_close_gate_verification(events)
    scope = gate["approval_scope_gate"]
    assert scope["passed"] is True
    assert scope["status"] == "waived"
    assert scope["has_close_waiver"] is True
    assert gate["passed"] is True, gate


def test_close_gate_allows_approval_that_authorizes_close():
    """Criterion 1: a normal approval that does not exclude close still passes."""
    from agent.governance import task_timeline

    events = [
        {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
        {"event_kind": "verification", "phase": "verification", "status": "passed"},
        {
            "event_kind": "close_ready",
            "phase": "close",
            "status": "accepted",
            "payload": {"approval_text": "approved for backlog_close after review"},
        },
    ]
    gate = task_timeline.mf_close_gate_verification(events)
    assert gate["approval_scope_gate"]["passed"] is True
    assert gate["passed"] is True, gate


def test_fixed_close_waiver_alert_flags_fixed_without_can_close_or_waiver():
    """Criterion 2: FIXED + can_close=false + no waiver is a governance alert."""
    from agent.governance import task_timeline

    alert = task_timeline.mf_fixed_close_waiver_alert("FIXED", False, _CLOSE_BASE_EVENTS)
    assert alert["alert"] is True
    assert alert["status"] == "alert"
    assert alert["reason"] == "fixed_row_without_can_close_or_close_waiver"


def test_fixed_close_waiver_alert_silenced_by_can_close_or_waiver():
    """Criterion 2: FIXED with can_close=true OR a visible waiver is not alerted."""
    from agent.governance import task_timeline

    assert (
        task_timeline.mf_fixed_close_waiver_alert("FIXED", True, _CLOSE_BASE_EVENTS)["alert"]
        is False
    )
    waiver_events = _CLOSE_BASE_EVENTS + [
        {
            "event_kind": "close_gate_waiver",
            "phase": "close",
            "status": "accepted",
            "payload": {"reason": "recorded waiver"},
        }
    ]
    assert (
        task_timeline.mf_fixed_close_waiver_alert("FIXED", False, waiver_events)["alert"]
        is False
    )
    # A non-FIXED row never alerts regardless of can_close.
    assert (
        task_timeline.mf_fixed_close_waiver_alert("OPEN", False, _CLOSE_BASE_EVENTS)["alert"]
        is False
    )


def test_close_gate_blocks_when_originating_command_still_claimed():
    """Criterion 3: a still-claimed originating observer command blocks close."""
    from agent.governance import task_timeline

    events = _CLOSE_BASE_EVENTS + [
        {
            "event_kind": "observer_command_claim",
            "status": "accepted",
            "payload": {"command_id": "cmd-close-1", "command_status": "claimed"},
        }
    ]
    gate = task_timeline.mf_close_gate_verification(events)
    assert gate["passed"] is False, gate
    cmd = gate["command_disposition_gate"]
    assert cmd["passed"] is False
    assert cmd["blocking_commands"][0]["command_id"] == "cmd-close-1"
    assert "command_disposition" in gate["missing_evidence_groups"]["groups"]


def test_close_gate_allows_when_originating_command_co_resolved():
    """Criterion 3: a terminal / co-resolved command does not block close."""
    from agent.governance import task_timeline

    events = _CLOSE_BASE_EVENTS + [
        {
            "event_kind": "observer_command_claim",
            "status": "accepted",
            "payload": {"command_id": "cmd-close-2", "command_status": "claimed"},
        },
        {
            "event_kind": "observer_command_complete",
            "status": "accepted",
            "payload": {"command_id": "cmd-close-2", "command_status": "completed"},
        },
    ]
    gate = task_timeline.mf_close_gate_verification(events)
    assert gate["command_disposition_gate"]["passed"] is True, gate
    assert gate["passed"] is True, gate

    co_resolved = _CLOSE_BASE_EVENTS + [
        {
            "event_kind": "observer_command_disposition",
            "status": "accepted",
            "payload": {
                "command_id": "cmd-close-3",
                "disposition": "co_resolved_with_close",
            },
        }
    ]
    gate2 = task_timeline.mf_close_gate_verification(co_resolved)
    assert gate2["command_disposition_gate"]["passed"] is True, gate2
    assert gate2["passed"] is True


# ---------------------------------------------------------------------------
# Tests for AC-READ-RECEIPT-APPEND-WRITE-VALIDATION-20260610:
# validate_and_normalize_mf_read_receipt_append
# ---------------------------------------------------------------------------

_RECEIPT_LINEAGE = {
    "runtime_context_id": "mfrctx-test-val",
    "task_id": "task-val-01",
    "parent_task_id": "task-val-01",
    "worker_slot_id": "claude-mfsub-val-01",
    "fence_token": "fence-val-abc",
    "read_receipt_hash": "sha256:test-receipt-hash-abc",
}


def _well_formed_receipt_payload(**overrides):
    """Return a well-formed mf_subagent read-receipt payload."""
    p = dict(_RECEIPT_LINEAGE)
    p.update(overrides)
    return p


def test_validate_receipt_rejects_event_3367_exact_shape():
    """Regression: event #3367 had event_type='mf_subagent_read_receipt',
    EMPTY event_kind, EMPTY status, and MISSING worker_slot_id / read_receipt_hash.
    The validator must reject it with an actionable error naming the missing fields."""
    from agent.governance import task_timeline

    # Reproduce #3367's exact incoming shape:
    # event_type marks a receipt but kind/status/hash/worker_slot_id are absent.
    with unittest.TestCase().assertRaises(ValueError) as ctx:
        task_timeline.validate_and_normalize_mf_read_receipt_append(
            event_type="mf_subagent_read_receipt",
            event_kind="",
            actor="mf_sub:worker-b4",
            status="",
            payload={
                "runtime_context_id": "mfrctx-b4",
                "task_id": "task-b4-bootstrap-handoff-20260610-01",
                "parent_task_id": "task-b4-bootstrap-handoff-20260610-01",
                # worker_slot_id intentionally absent (replicating #3367)
                "fence_token": "fence-b4-abc",
                # read_receipt_hash intentionally absent (replicating #3367)
                "launch_text_hash": "",  # also empty
            },
        )

    msg = str(ctx.exception)
    # Error must name the projection schema.
    assert "runtime_context.timeline_evidence_fields.v1" in msg, msg
    # Error must list specific missing fields.
    assert "status" in msg, msg
    assert "worker_slot_id" in msg or "read_receipt_hash" in msg, msg


def test_validate_receipt_rejects_missing_status():
    """A receipt with a good payload but no status is rejected."""
    from agent.governance import task_timeline

    with unittest.TestCase().assertRaises(ValueError) as ctx:
        task_timeline.validate_and_normalize_mf_read_receipt_append(
            event_type="mf_subagent.read_receipt",
            event_kind="mf_subagent_read_receipt",
            actor="mf_sub:worker",
            status="",
            payload=_well_formed_receipt_payload(),
        )

    msg = str(ctx.exception)
    assert "status" in msg, msg
    assert "runtime_context.timeline_evidence_fields.v1" in msg, msg


def test_validate_receipt_rejects_non_passing_status():
    """A receipt with a non-passing status (e.g. 'failed') is rejected."""
    from agent.governance import task_timeline

    with unittest.TestCase().assertRaises(ValueError) as ctx:
        task_timeline.validate_and_normalize_mf_read_receipt_append(
            event_type="mf_subagent.read_receipt",
            event_kind="mf_subagent_read_receipt",
            actor="mf_sub:worker",
            status="failed",
            payload=_well_formed_receipt_payload(),
        )

    msg = str(ctx.exception)
    assert "status" in msg, msg


def test_validate_receipt_rejects_missing_both_hashes():
    """A receipt missing both read_receipt_hash and launch_text_hash is rejected."""
    from agent.governance import task_timeline

    payload = _well_formed_receipt_payload()
    payload.pop("read_receipt_hash")
    # launch_text_hash also absent

    with unittest.TestCase().assertRaises(ValueError) as ctx:
        task_timeline.validate_and_normalize_mf_read_receipt_append(
            event_type="mf_subagent.read_receipt",
            event_kind="mf_subagent_read_receipt",
            actor="mf_sub:worker",
            status="ok",
            payload=payload,
        )

    msg = str(ctx.exception)
    assert "read_receipt_hash" in msg or "launch_text_hash" in msg, msg


def test_validate_receipt_accepts_launch_text_hash_without_read_receipt_hash():
    """launch_text_hash alone satisfies the hash requirement."""
    from agent.governance import task_timeline

    payload = _well_formed_receipt_payload()
    payload.pop("read_receipt_hash")
    payload["launch_text_hash"] = "sha256:launch-abc"

    kind, status, p = task_timeline.validate_and_normalize_mf_read_receipt_append(
        event_type="mf_subagent.read_receipt",
        event_kind="mf_subagent_read_receipt",
        actor="mf_sub:worker",
        status="ok",
        payload=payload,
    )
    assert kind == "mf_subagent_read_receipt"
    assert status == "ok"


def test_validate_receipt_rejects_missing_lineage_fields():
    """A receipt missing lineage fields (runtime_context_id, task_id,
    parent_task_id, fence_token) is rejected with those field names in the error."""
    from agent.governance import task_timeline

    payload = {"read_receipt_hash": "sha256:test", "worker_slot_id": "slot-1"}
    # All lineage fields absent.

    with unittest.TestCase().assertRaises(ValueError) as ctx:
        task_timeline.validate_and_normalize_mf_read_receipt_append(
            event_type="mf_subagent.read_receipt",
            event_kind="mf_subagent_read_receipt",
            actor="mf_sub:worker",
            status="ok",
            payload=payload,
        )

    msg = str(ctx.exception)
    assert "runtime_context_id" in msg, msg
    assert "task_id" in msg, msg
    assert "fence_token" in msg, msg


def test_validate_receipt_normalizes_empty_event_kind():
    """When event_type marks a receipt but event_kind is empty, it is normalized
    to 'mf_subagent_read_receipt'."""
    from agent.governance import task_timeline

    kind, status, p = task_timeline.validate_and_normalize_mf_read_receipt_append(
        event_type="mf_subagent.read_receipt",
        event_kind="",   # empty — must be normalized
        actor="mf_sub:worker",
        status="ok",
        payload=_well_formed_receipt_payload(),
    )
    assert kind == "mf_subagent_read_receipt"
    assert status == "ok"


def test_validate_receipt_normalizes_worker_slot_id_from_worker_id():
    """When worker_slot_id is absent but worker_id is present, worker_slot_id
    is normalized from worker_id."""
    from agent.governance import task_timeline

    payload = _well_formed_receipt_payload()
    payload.pop("worker_slot_id")
    payload["worker_id"] = "claude-mfsub-slot-norm"

    kind, status, p = task_timeline.validate_and_normalize_mf_read_receipt_append(
        event_type="mf_subagent.read_receipt",
        event_kind="mf_subagent_read_receipt",
        actor="mf_sub:worker",
        status="ok",
        payload=payload,
    )
    assert p["worker_slot_id"] == "claude-mfsub-slot-norm"


def test_validate_receipt_accepts_well_formed_shape_3366():
    """Shape #3366: event_kind + status + read_receipt_hash all present — passes unchanged."""
    from agent.governance import task_timeline

    kind, status, p = task_timeline.validate_and_normalize_mf_read_receipt_append(
        event_type="mf_subagent.read_receipt",
        event_kind="mf_subagent_read_receipt",
        actor="mf_sub:worker",
        status="ok",
        payload=_well_formed_receipt_payload(),
    )
    assert kind == "mf_subagent_read_receipt"
    assert status == "ok"
    assert "read_receipt_hash" in p


def test_validate_receipt_accepts_well_formed_shape_3369_hash_empty_launch_text_hash():
    """Shape #3369: event_kind + status present, read_receipt_hash empty but
    launch_text_hash provided — passes (and matched the gate on kind+status+hash)."""
    from agent.governance import task_timeline

    payload = _well_formed_receipt_payload()
    # Simulate #3369: read_receipt_hash empty string, launch_text_hash present
    payload["read_receipt_hash"] = ""
    payload["launch_text_hash"] = "sha256:launch-3369"

    kind, status, p = task_timeline.validate_and_normalize_mf_read_receipt_append(
        event_type="mf_subagent.read_receipt",
        event_kind="mf_subagent_read_receipt",
        actor="mf_sub:worker",
        status="ok",
        payload=payload,
    )
    assert kind == "mf_subagent_read_receipt"
    assert status == "ok"


def test_validate_receipt_passthrough_for_non_receipt_event():
    """Non-receipt events are passed through unchanged — no validation applied."""
    from agent.governance import task_timeline

    # A plain implementation event should pass through without any validation.
    kind, status, p = task_timeline.validate_and_normalize_mf_read_receipt_append(
        event_type="mf.implementation",
        event_kind="implementation",
        actor="mf_sub:worker",
        status="",  # empty status — OK for non-receipt
        payload={"changed_files": ["foo.py"]},
    )
    assert kind == "implementation"
    assert status == ""


def test_validate_receipt_error_message_names_projection_schema():
    """The error message must name the projection schema so the caller knows
    which gate definition requires the listed fields."""
    from agent.governance import task_timeline

    with unittest.TestCase().assertRaises(ValueError) as ctx:
        task_timeline.validate_and_normalize_mf_read_receipt_append(
            event_type="mf_subagent_read_receipt",
            event_kind="",
            actor="mf_sub",
            status="",
            payload={},
        )

    msg = str(ctx.exception)
    assert "runtime_context.timeline_evidence_fields.v1" in msg, msg
    assert "mf_subagent read-receipt append rejected" in msg, msg


if __name__ == "__main__":
    unittest.main()
