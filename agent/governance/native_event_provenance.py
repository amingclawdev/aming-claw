"""Read one sealed current-full reconcile event, never infer formal authority."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from collections.abc import Mapping

from .contracts.hash import stable_sha256

SCHEMA = "contract_runtime.native_event.v1"
MAX_BYTES = 16384
FAMILY = "graph.reconcile.current_full"
ENTRYPOINT = "POST /api/graph-governance/{project_id}/reconcile/current-full"
SCOPE_FIELDS = ("project_id", "backlog_id", "task_id", "parent_task_id", "runtime_context_id", "merge_queue_id")
BINDING_FIELDS = ("project_id", "backlog_id", "contract_execution_id", "contract_id",
    "contract_revision_id", "contract_hash", "execution_state_revision",
    "execution_state_hash", "runtime_guide_hash")


def refusal(reason):
    return {"schema_version": SCHEMA, "response_view": "native_event", "ok": False,
        "error": reason, "observation_only": True, "native_event": None,
        "formal_world": "UNKNOWN", "formal_position": "UNKNOWN",
        "formal_world_authority": False, "formal_position_authority": False,
        "direction_authority": False, "effect_authority": False}


def wire_response(response):
    if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        return refusal("native_event_wire_unbounded")
    return response


def _mapping(raw):
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _hashed_authority(value):
    return bool(value and value.get("authority_hash") == stable_sha256(
        {key: item for key, item in value.items() if key != "authority_hash"}))


def _public_scope(value):
    return all(isinstance(item, str) and item and
        len(item.encode("utf-8")) <= 256 and
        re.fullmatch(r"[A-Za-z0-9_.:-]+", item) for item in value.values())


def _read_shared_batch_event(conn, *, source_record, recorded, claim, authority,
                             reader, event, seal, marker, payload, expected_hash):
    """Bind an immutable child admission to the original sealed parent event.

    All lifecycle acceptance belongs to the existing server authority helper.
    This projection retains original admission flags and complete hashes; it
    cannot upgrade a recorded admission because later QA/close lines exist.
    """
    shared = _mapping(authority.get("shared_batch_reconcile_authority"))
    if any(len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_BYTES
            for value in (event, seal, claim)):
        return refusal("native_event_association_invalid")
    if not shared or reader is None:
        return refusal("native_event_association_missing")
    if shared.get("schema_version") != "contract_runtime.shared_batch_final_reconcile_authority.v1":
        return refusal("native_event_association_unsupported")
    if not (_hashed_authority(claim) and _hashed_authority(authority)
            and _hashed_authority(shared)):
        return refusal("native_event_association_invalid")
    try:
        binding = reader(authority)
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        return refusal("native_event_association_invalid")
    fresh = _mapping(_mapping(binding).get("authority"))
    dispatch = _mapping(_mapping(binding).get("dispatch"))
    if not fresh or fresh != authority or not dispatch:
        return refusal("native_event_association_invalid")
    if len(json.dumps(dispatch, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        return refusal("native_event_association_invalid")
    if ("terminal_current_full_reconcile_authority" in claim
            and _mapping(_mapping(binding).get("receipt")) != claim):
        return refusal("native_event_association_invalid")
    child_scope = {key: authority.get(key) for key in (*SCOPE_FIELDS, "contract_execution_id")}
    selected_line = source_record["completed_lines"][
        recorded["recorded_line"]["source_completed_line_index"]]
    # A coherent line digest does not prove that its own scope belongs to the
    # child whose original receipt the durable authority just verified.
    if any(selected_line.get(key) != child_scope[key]
            for key in ("task_id", "runtime_context_id", "parent_task_id")):
        return refusal("native_event_association_invalid")
    source_scope = {"project_id": event.get("project_id"),
        "backlog_id": event.get("backlog_id"), "task_id": event.get("task_id")}
    if not (_public_scope(child_scope) and _public_scope(source_scope)
            and all(child_scope[key] == recorded[key]
                for key in ("project_id", "backlog_id", "contract_execution_id"))
            and source_scope["project_id"] == recorded["project_id"]
            and source_scope["backlog_id"] == shared.get("coordination_backlog_id")
            and source_scope["task_id"] == shared.get("coordination_reconcile_task_id")
            and source_scope["task_id"] == authority.get("reconcile_task_id")
            and not _mapping(marker.get("runtime_context_scope"))
            and not _mapping(payload.get("runtime_context_scope"))):
        return refusal("native_event_association_invalid")
    event_id, occurred_at = event["id"], event.get("created_at")
    if not (type(marker.get("reconcile_event_id")) is int
            and marker["reconcile_event_id"] == seal.get("reconcile_event_id") == event_id
            and marker.get("reconcile_event_created_at") == seal.get("reconcile_event_created_at")
                == authority.get("reconcile_event_created_at") == occurred_at
            and _timestamp(occurred_at)
            and shared.get("final_reconcile_event_ref") == f"timeline:{event_id}"
            and marker.get("snapshot_id") == payload.get("snapshot_id") == seal.get("snapshot_id")
                == shared.get("active_snapshot_id")
            and marker.get("target_commit_sha") == seal.get("target_commit_sha")
                == event.get("commit_sha") == payload.get("target_commit_sha")
                == shared.get("final_head_commit") == authority.get("canonical_head_commit")
            and isinstance(event.get("commit_sha"), str)
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", event["commit_sha"])
            and payload.get("current_full_reconcile") is True
            and payload.get("reconcile_mode") == "current_full"
            and all(payload.get(key) is True for key in
                ("canonical_head_verified", "active_snapshot_verified", "graph_reconciled"))):
        return refusal("native_event_association_invalid")
    index = dispatch.get("line_index")
    lines = source_record.get("completed_lines", [])
    dispatch_ref = authority.get("contract_runtime_dispatch_source_ref")
    if not (type(index) is int and 0 <= index <= 2**63 - 1 and index < len(lines)
            and dispatch.get("line") == lines[index]
            and dispatch.get("source_ref") == dispatch_ref
            and dispatch_ref == f"contract_runtime:{recorded['contract_execution_id']}:completed_lines:{index}"
            and dispatch_ref in shared.get("child_contract_dispatch_refs", [])):
        return refusal("native_event_association_invalid")
    try:
        epochs = conn.execute("SELECT * FROM parallel_branch_integration_epochs WHERE project_id=? AND batch_id=? LIMIT 2",
            (recorded["project_id"], shared.get("batch_id"))).fetchall()
        queue = [dict(row) for row in conn.execute(
            "SELECT * FROM parallel_branch_merge_queue_items WHERE project_id=? AND merge_queue_id=? ORDER BY queue_index ASC,queue_item_id ASC",
            (recorded["project_id"], child_scope["merge_queue_id"])).fetchall()]
        snapshots = conn.execute("SELECT notes,commit_sha FROM graph_snapshots WHERE project_id=? AND snapshot_id=? LIMIT 2",
            (recorded["project_id"], seal.get("snapshot_id"))).fetchall()
    except sqlite3.Error:
        return refusal("native_event_store_unavailable")
    if not (len(epochs) == 1 and len(snapshots) == 1
            and snapshots[0]["commit_sha"] == event["commit_sha"]
            and _mapping(_mapping(snapshots[0]["notes"]).get("current_full_reconcile")) == marker):
        return refusal("native_event_association_invalid")
    epoch = dict(epochs[0])
    if any(len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_BYTES
            for value in (epoch, queue)):
        return refusal("native_event_association_invalid")
    selected_queue = [item for item in queue if item.get("task_id") == child_scope["task_id"]
        and item.get("backlog_id") == child_scope["backlog_id"]]
    if not (epoch.get("epoch_id") == shared.get("epoch_id")
            and epoch.get("status") == "closed" and epoch.get("reconcile_state") == "reconciled"
            and [item.get("queue_item_id") for item in queue] == shared.get("ordered_queue_item_ids")
            and len(selected_queue) == 1
            and selected_queue[0].get("queue_item_id") == authority.get("queue_item_id")
            and selected_queue[0].get("merge_commit") == authority.get("merged_commit_sha")
            and shared.get("merge_queue_id") == child_scope["merge_queue_id"]):
        return refusal("native_event_association_invalid")
    identifiers = {key: shared.get(key) for key in ("batch_id", "epoch_id", "active_snapshot_id")}
    identifiers["queue_item_id"] = authority.get("queue_item_id")
    if not _public_scope(identifiers):
        return refusal("native_event_association_invalid")
    flags = {key: shared.get(key) for key in ("all_children_contract_qa_verified",
        "current_child_contract_qa_verified", "final_qa_required_for_current_child")}
    flags["close_satisfying"] = authority.get("close_satisfying")
    if not (shared.get("authority_purpose") == "postmerge_qa_admission"
            and flags == {"all_children_contract_qa_verified": False,
                "current_child_contract_qa_verified": False,
                "final_qa_required_for_current_child": True, "close_satisfying": False}
            and all(type(value) is bool for value in flags.values())):
        return refusal("native_event_association_unsupported")
    digest = stable_sha256(event)
    if expected_hash is not None and digest != expected_hash:
        return refusal("native_event_digest_mismatch")
    association = {"schema_version": "shared_batch_association.v1",
        "source_of_authority": "server._contract_runtime_shared_batch_reconcile_authority",
        "source_scope": source_scope, "queried_child_scope": child_scope,
        "queried_binding_hash": stable_sha256({key: recorded[key] for key in BINDING_FIELDS}),
        "recorded_line_hash": stable_sha256(recorded["recorded_line"]),
        "batch_id": shared["batch_id"], "epoch_id": epoch["epoch_id"], "epoch_hash": stable_sha256(epoch),
        "merge_queue_id": child_scope["merge_queue_id"], "queue_item_id": authority["queue_item_id"],
        "ordered_queue_hash": stable_sha256(queue), "child_dispatch_ref": dispatch_ref,
        "child_dispatch_hash": stable_sha256(lines[index]), "child_dispatch_scope": dict(child_scope),
        "source_event_id": str(event_id), "source_event_hash": digest, "source_event_time": occurred_at,
        "provenance_id": seal["provenance_id"], "provenance_hash": seal["provenance_hash"],
        "snapshot_id": seal["snapshot_id"], "target_commit": event["commit_sha"],
        "shared_authority_hash": shared["authority_hash"], "terminal_authority_hash": authority["authority_hash"],
        "authority_purpose": shared["authority_purpose"], **flags}
    association["association_hash"] = stable_sha256(association)
    return wire_response({
        **{key: recorded[key] for key in ("ok", "error", "absence", "observation_only", "actor_role", "request_id",
            *BINDING_FIELDS, "recorded_line")},
        "schema_version": "contract_runtime.native_event.shared_batch_association.v1", "response_view": "native_event",
        "source_of_authority": "protected_current_full_reconcile_native_store_binding",
        "proof_variant": "shared_batch_association.v1", "association": association,
        "native_event": {"event_family": FAMILY, "event_type": "graph.reconcile", "source_event_id": str(event_id),
            "occurred_at": occurred_at, "timestamp_provenance": "native_writer_utc_timestamp_sealed_in_current_full_provenance",
            "source_event_hash": digest, "digest_convention": "stable_sha256_complete_task_timeline_events_row_v1",
            "source_locator": {"store": "task_timeline_events", "project_id": recorded["project_id"], "event_id": str(event_id)},
            "binding_locator": {"store": "graph_current_full_reconcile_provenance", "provenance_id": seal["provenance_id"]},
            "binding_hash": seal["provenance_hash"], "runtime_scope": {}, "source_scope": source_scope},
        "formal_world": "UNKNOWN", "formal_position": "UNKNOWN", "generation": "UNKNOWN", "execution": "UNKNOWN",
        "formal_world_authority": False, "formal_position_authority": False, "direction_authority": False, "effect_authority": False,
    })


def read_native_event(conn, *, source_record, recorded, selector, actor_role,
                      shared_batch_reader=None):
    """The timeline row alone never qualifies; require its separate native seal.

    The original completed line is only accepted recorded evidence. Its exact
    reconcile pointer must match the sealed event and durable runtime context.
    No close/merge/Position eligibility is inherited from either digest.
    """
    if actor_role not in ("observer", "coordinator"):
        return refusal("native_event_role_unsupported")
    for field in ("source_event_id", "contract_revision_id"):
        if field not in selector or selector[field] == "":
            return refusal("native_event_selector_missing")
        if not isinstance(selector[field], str) or len(selector[field].encode("utf-8")) > 256:
            return refusal("native_event_selector_invalid")
    if not re.fullmatch(r"[1-9][0-9]{0,18}", selector["source_event_id"]):
        return refusal("native_event_selector_invalid")
    event_id = int(selector["source_event_id"])
    if event_id > 2**63 - 1:
        return refusal("native_event_selector_invalid")
    if selector.get("event_family", FAMILY) != FAMILY:
        return refusal("native_event_family_unsupported")
    expected_hash = selector.get("source_event_hash")
    if expected_hash is not None and (not isinstance(expected_hash, str) or
            not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_hash)):
        return refusal("native_event_selector_invalid")
    if recorded.get("ok") is not True:
        return refusal(recorded.get("error", "native_event_source_invalid"))
    if selector["contract_revision_id"] != recorded["contract_revision_id"]:
        return refusal("native_event_scope_mismatch")
    project, backlog, execution = (recorded[key] for key in ("project_id", "backlog_id", "contract_execution_id"))
    line = source_record["completed_lines"][recorded["recorded_line"]["source_completed_line_index"]]
    claim = _mapping(_mapping(line.get("payload")).get("reconcile_authority"))
    authority = _mapping(claim.get("terminal_current_full_reconcile_authority")) or claim
    shared_claim = "shared_batch_reconcile_authority" in authority
    if (line.get("line_id"), line.get("evidence_kind"), line.get("actor_role")) != (
            "observer_reconcile", "reconcile", "observer"):
        return refusal("native_event_line_family_unsupported")
    if (type(authority.get("reconcile_event_id")) is not int or
            authority["reconcile_event_id"] != event_id or
            authority.get("reconcile_source_ref") != f"timeline:{event_id}"):
        return refusal("native_event_line_binding_mismatch")
    try:
        events = conn.execute("SELECT * FROM task_timeline_events WHERE project_id=? AND id=? LIMIT 2",
            (project, event_id)).fetchall()
        seals = conn.execute("SELECT * FROM graph_current_full_reconcile_provenance WHERE project_id=? AND reconcile_event_id=? LIMIT 2",
            (project, event_id)).fetchall()
    except sqlite3.Error:
        return refusal("native_event_store_unavailable")
    if not events:
        return refusal("native_event_missing")
    if len(events) != 1 or len(seals) > 1:
        return refusal("native_event_binding_ambiguous")
    if not seals:
        return refusal("native_event_native_binding_missing")
    event, seal = dict(events[0]), dict(seals[0])
    if (event.get("event_type"), event.get("event_kind"), event.get("phase"), event.get("status")) != (
            "graph.reconcile", "reconcile", "reconcile", "passed"):
        return refusal("native_event_family_unsupported")
    marker, route = _mapping(seal.get("marker_json")), _mapping(seal.get("route_evidence_json"))
    scope = _mapping(marker.get("runtime_context_scope"))
    payload = _mapping(event.get("payload_json"))
    core = {key: value for key, value in marker.items() if key != "provenance_hash"}
    if not (marker.get("schema_version") == "current_full_reconcile.provenance.v2"
            and marker.get("source") == "graph_governance_api"
            and marker.get("protected_action") == seal.get("protected_action") == "graph_current_full_reconcile"
            and marker.get("protected_entrypoint") == seal.get("protected_entrypoint") == ENTRYPOINT
            and marker.get("normal_update_path") is True and marker.get("dev_force_graph_only") is False
            and marker.get("activate") is True and not marker.get("operator_event_id")
            and marker.get("provenance_id") == seal.get("provenance_id")
            and isinstance(seal.get("provenance_id"), str)
            and re.fullmatch(r"cfrp-[a-f0-9]{24}", seal["provenance_id"])
            and all(marker.get(key) == seal.get(key) for key in ("request_id", "request_started_at", "marker_created_at"))
            and marker.get("provenance_hash") == seal.get("provenance_hash") == stable_sha256(core)
            and marker.get("route_evidence") == route
            and route.get("schema_version") == "graph_current_full_reconcile.route_evidence.v1"
            and route.get("protected_action") == "graph_current_full_reconcile"
            and route.get("authenticated_role") in ("observer", "coordinator")
            and route.get("authentication_source") in ("operator_capability", "observer_session_route_token_ref")
            and route.get("raw_route_token_persisted") is False):
        return refusal("native_event_native_binding_invalid")
    if shared_claim:
        return _read_shared_batch_event(conn, source_record=source_record,
            recorded=recorded, claim=claim, authority=authority,
            reader=shared_batch_reader, event=event, seal=seal,
            marker=marker, payload=payload, expected_hash=expected_hash)
    identity = {key: scope.get(key, "") for key in SCOPE_FIELDS}
    if not (identity["project_id"] == project and identity["backlog_id"] == backlog
            and all(identity[key] for key in ("task_id", "runtime_context_id"))
            and all(isinstance(value, str) and (not value or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value))
                for value in identity.values())
            and scope.get("source") == "parallel_branch_runtime_context" and scope.get("server_derived") is True
            and scope.get("contract_execution_id", execution) == execution
            and execution in {scope.get("contract_execution_id"), identity["task_id"], identity["parent_task_id"]}
            and all(authority.get(key, "") == value for key, value in identity.items())
            and authority.get("contract_execution_id") == execution):
        return refusal("native_event_scope_mismatch")
    route_scope, event_scope = _mapping(route.get("runtime_context_scope")), _mapping(payload.get("runtime_context_scope"))
    if not all(all(item.get(key, "") == value for key, value in identity.items())
               and item.get("source") == "parallel_branch_runtime_context" and item.get("server_derived") is True
               and item.get("contract_execution_id", execution) == execution for item in (route_scope, event_scope)):
        return refusal("native_event_scope_mismatch")
    try:
        contexts = conn.execute("SELECT project_id,backlog_id,task_id,parent_task_id,runtime_context_id,merge_queue_id FROM parallel_branch_runtime_contexts WHERE project_id=? AND runtime_context_id=? LIMIT 2",
            (project, identity["runtime_context_id"])).fetchall()
        snapshots = conn.execute("SELECT notes,commit_sha FROM graph_snapshots WHERE project_id=? AND snapshot_id=? LIMIT 2",
            (project, seal.get("snapshot_id"))).fetchall()
    except sqlite3.Error:
        return refusal("native_event_store_unavailable")
    if len(contexts) != 1 or dict(contexts[0]) != identity:
        return refusal("native_event_runtime_context_mismatch")
    if len(snapshots) != 1 or _mapping(_mapping(snapshots[0]["notes"]).get("current_full_reconcile")) != marker:
        return refusal("native_event_snapshot_binding_mismatch")
    occurred_at = event.get("created_at")
    if not (event.get("backlog_id") == backlog and event.get("task_id") == identity["task_id"]
            and type(marker.get("reconcile_event_id")) is int
            and marker.get("reconcile_event_id") == event_id
            and seal.get("reconcile_event_id") == event_id
            and marker.get("reconcile_event_created_at") == seal.get("reconcile_event_created_at")
                == authority.get("reconcile_event_created_at") == occurred_at and _timestamp(occurred_at)
            and marker.get("snapshot_id") == payload.get("snapshot_id") == seal.get("snapshot_id")
            and marker.get("target_commit_sha") == seal.get("target_commit_sha") == event.get("commit_sha")
                == snapshots[0]["commit_sha"] == payload.get("target_commit_sha")
            and payload.get("current_full_reconcile") is True and payload.get("reconcile_mode") == "current_full"
            and all(payload.get(key) is True for key in ("canonical_head_verified", "active_snapshot_verified", "graph_reconciled"))):
        return refusal("native_event_event_binding_mismatch")
    digest = stable_sha256(event)  # Entire persisted row, including complete JSON columns.
    if expected_hash is not None and expected_hash != digest:
        return refusal("native_event_digest_mismatch")
    return wire_response({
        **{key: recorded[key] for key in ("ok", "error", "absence", "observation_only", "actor_role", "request_id",
            "project_id", "backlog_id", "contract_execution_id", "contract_id", "contract_revision_id", "contract_hash",
            "execution_state_revision", "execution_state_hash", "runtime_guide_hash", "recorded_line")},
        "schema_version": SCHEMA, "response_view": "native_event",
        "source_of_authority": "protected_current_full_reconcile_native_store_binding",
        "native_event": {"event_family": FAMILY, "event_type": "graph.reconcile", "source_event_id": str(event_id),
            "occurred_at": occurred_at, "timestamp_provenance": "native_writer_utc_timestamp_sealed_in_current_full_provenance",
            "source_event_hash": digest, "digest_convention": "stable_sha256_complete_task_timeline_events_row_v1",
            "source_locator": {"store": "task_timeline_events", "project_id": project, "event_id": str(event_id)},
            "binding_locator": {"store": "graph_current_full_reconcile_provenance", "provenance_id": seal["provenance_id"]},
            "binding_hash": seal["provenance_hash"], "runtime_scope": identity},
        "formal_world": "UNKNOWN", "formal_position": "UNKNOWN",
        "formal_world_authority": False, "formal_position_authority": False,
        "generation": "UNKNOWN", "execution": "UNKNOWN",
        "direction_authority": False, "effect_authority": False,
    })
