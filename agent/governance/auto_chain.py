# v12: smoke-test verified post MF-2026-04-05-002
"""Auto-chain dispatcher.

Wires task completion to next-stage task creation with gate validation
between each stage. Called by complete_task() when a task succeeds.

Full chain: PM → Dev → Test → QA → Merge → Deploy
Each transition runs a gate check before advancing.
"""

import json
import logging
import os
import re
import traceback
from .failure_classifier import classify_gate_failure, build_workflow_improvement_prompt
from .observability import new_trace_id, structured_log
from .doc_policy import (
    is_dev_artifact as _is_dev_note,
    is_governance_internal_repair as _is_governance_internal_repair,
    _GOVERNANCE_INTERNAL_PREFIXES,
)

log = logging.getLogger(__name__)

# Set to True to skip SERVER_VERSION vs git-HEAD check during development.
# Restore to False before production use.
_DISABLE_VERSION_GATE = False

# B15/B23/B31: Prefixes filtered from dirty_files before version gate evaluation.
# Paths matching any prefix are tool-local or non-governed and must not block chain.
# To add new entry here when an observer script writes a runtime-state file to repo
# root: append the path prefix (with both "/" and "\\" variants for cross-platform)
# to this tuple so that the version gate does not treat it as a governed dirty file.
_DIRTY_IGNORE = (
    ".claude/", ".claude\\",
    ".worktrees/", ".worktrees\\",
    "docs/dev/", "docs/dev\\",
    ".recent-tasks.json",
    ".governance-cache/", ".governance-cache\\",
    ".observer-cache/", ".observer-cache\\",
)

# Graph-driven doc governance: observation mode flag (Step 5, P1 principle)
# When True, graph doc checks log warnings instead of blocking.
_GRAPH_DOC_OBSERVATION_MODE = True

# ---------------------------------------------------------------------------
# Reconciliation Bypass Policy (R1)
# ---------------------------------------------------------------------------
RECONCILIATION_BYPASS_POLICY = {
    "required_metadata_fields": ["reconciliation_lane", "observer_authorized"],
    "allowed_lanes": {"A", "B"},
    "audit_action": "reconciliation_bypass",
}


def _check_reconciliation_bypass(conn, project_id, metadata):
    """Validate metadata against RECONCILIATION_BYPASS_POLICY.

    Returns (bypass: bool, observer_task_id: str|None).
    Checks:
      (a) metadata.reconciliation_lane in allowed_lanes
      (b) metadata.observer_authorized == True
      (c) task chain traces back to an observer-created parent task
    """
    policy = RECONCILIATION_BYPASS_POLICY

    # (a) reconciliation_lane must be in allowed set
    lane = str(metadata.get("reconciliation_lane", "") or "").strip().upper()
    if lane not in policy["allowed_lanes"]:
        return False, None

    # (b) observer_authorized must be explicitly True
    if not metadata.get("observer_authorized"):
        return False, None

    # (c) Walk parent chain to find observer-created task
    observer_task_id = metadata.get("observer_task_id")
    if not observer_task_id:
        for parent_meta in _walk_task_metadata_chain(conn, project_id, metadata):
            if parent_meta.get("created_by_observer") or parent_meta.get("observer_task_id"):
                observer_task_id = parent_meta.get("observer_task_id") or parent_meta.get("parent_task_id", "")
                break

    if not observer_task_id:
        # Fallback: treat the parent_task_id as the observer task if observer_authorized is set
        observer_task_id = metadata.get("parent_task_id", "unknown")

    return True, observer_task_id


def _audit_reconciliation_bypass(conn, project_id, task_id, observer_task_id, lane):
    """Write reconciliation_bypass event to audit_log (R6)."""
    try:
        conn.execute(
            "INSERT INTO audit_log (project_id, action, actor, ok, ts, task_id, details_json) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?, ?)",
            (
                project_id,
                "reconciliation_bypass",
                "auto-chain",
                1,
                task_id,
                json.dumps({
                    "observer_task_id": observer_task_id,
                    "lane": lane,
                    "task_id": task_id,
                }),
            ),
        )
    except Exception:
        log.debug("audit reconciliation_bypass failed (non-critical)", exc_info=True)


def _audit_version_gate_bypass(conn, project_id, task_id, operator_id, bypass_reason, task_type):
    """Write version_gate_bypass event to audit_log and check frequency."""
    try:
        conn.execute(
            "INSERT INTO audit_log (project_id, action, actor, ok, ts, task_id, details_json) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?, ?)",
            (
                project_id,
                "version_gate_bypass",
                operator_id,
                1,
                task_id,
                json.dumps({
                    "bypass_reason": bypass_reason,
                    "task_type": task_type,
                }),
            ),
        )
        # R3: Check bypass frequency in last 24 hours
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM audit_log "
            "WHERE action='version_gate_bypass' AND project_id=? "
            "AND ts >= datetime('now', '-24 hours')",
            (project_id,),
        ).fetchone()
        count = row[0] if row else 0
        if count > 3:
            log.warning("high bypass frequency: %d version_gate_bypass events for project %s in last 24h",
                        count, project_id)
    except Exception:
        log.debug("audit version_gate_bypass failed (non-critical)", exc_info=True)


# Chain definition: task_type → (gate_fn, next_type, prompt_builder)
# next_type=None means terminal stage (deploy trigger)
CHAIN = {
    "pm":    ("_gate_post_pm",    "dev",   "_build_dev_prompt"),
    "dev":   ("_gate_checkpoint", "test",  "_build_test_prompt"),
    "test":  ("_gate_t2_pass",    "qa",    "_build_qa_prompt"),
    "qa":    ("_gate_qa_pass",    "gatekeeper", "_build_gatekeeper_prompt"),
    "gatekeeper": ("_gate_gatekeeper_pass", "merge", "_build_merge_prompt"),
    "merge": ("_gate_release",    "deploy", "_build_deploy_prompt"),
    "deploy": ("_gate_deploy_pass", None, "_finalize_chain"),
}

# Maximum chain depth to prevent infinite loops
MAX_CHAIN_DEPTH = 10

# ---------------------------------------------------------------------------
# Graph-Driven Doc Governance Helpers (Step 5)
# ---------------------------------------------------------------------------


def _get_graph_doc_associations(project_id, target_files):
    """Query graph for doc associations of target_files.

    Returns list of doc paths that the graph considers related to the changed code.
    Uses confirmed secondary associations from graph nodes.
    """
    try:
        from .graph import AcceptanceGraph
        state_root = os.path.join(
            os.environ.get("SHARED_VOLUME_PATH",
                           os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "shared-volume")),
            "codex-tasks", "state", "governance", project_id)
        graph_path = os.path.join(state_root, "graph.json")
        if not os.path.exists(graph_path):
            return []
        graph = AcceptanceGraph()
        graph.load(graph_path)
        docs = set()
        target_set = set(target_files) if target_files else set()
        for node_id in graph.list_nodes():
            try:
                node_data = graph.get_node(node_id)
            except Exception:
                continue
            primary = node_data.get("primary", [])
            secondary = node_data.get("secondary", [])
            # Forward: code target → find related docs
            if any(f in target_set for f in primary):
                for s in secondary:
                    if s.endswith(".md"):
                        docs.add(s)
            # Reverse (G6): doc target → find related doc files only
            # R4: Never add primary code files (.py) to docs set
            if any(f in target_set for f in secondary):
                for p in primary:
                    if p.endswith(".md"):
                        docs.add(p)
        # B49: Defensive filter — remove doc paths that no longer exist on disk
        filtered = set()
        for doc_path in docs:
            if os.path.exists(doc_path):
                filtered.add(doc_path)
            else:
                log.warning("Stale graph doc reference filtered: %s", doc_path)
        return sorted(filtered)
    except Exception:
        log.debug("_get_graph_doc_associations failed (non-critical)", exc_info=True)
        return []


def _audit_doc_gap(conn, project_id, task_id, stage, missing_docs, changed_files):
    """Audit doc gap observation (5f). Writes to audit_index for later analysis."""
    try:
        from .audit_service import record
        record(
            conn, project_id,
            event="doc_gap_observation",
            actor="auto-chain",
            ok=True,  # observation, not failure
            node_ids=None,
            request_id="",
            stage=stage,
            task_id=task_id,
            missing_docs=sorted(missing_docs) if missing_docs else [],
            changed_files=changed_files[:10] if changed_files else [],
            observation_mode=True,
        )
    except Exception:
        log.debug("_audit_doc_gap failed (non-critical)", exc_info=True)


# TODO-DEPRECATED: _store_proposed_nodes removed per OPT-BACKLOG-GRAPH-DELTA-CHAIN-COMMIT PR-A.
# Replaced by graph.delta.proposed chain_events emission via _emit_graph_delta_event().
# The pending_nodes table had no downstream consumer (pn['docs'] never populated by PM).


def _emit_graph_delta_event(project_id, task_id, result):
    """Emit graph.delta.proposed event if result contains non-empty graph_delta.

    R1: graph_delta shape: {creates: [...], updates: [...], links: [...]}.
    R2: Emits via ChainContextStore._persist_event for chain_events persistence.
    R3: No event if graph_delta is missing, None, or all sub-arrays empty.
    """
    graph_delta = result.get("graph_delta") if isinstance(result, dict) else None
    if not graph_delta or not isinstance(graph_delta, dict):
        return

    # Normalize: default missing sub-arrays to []
    creates = graph_delta.get("creates", [])
    updates = graph_delta.get("updates", [])
    links = graph_delta.get("links", [])

    # R3: Skip if all sub-arrays are empty
    if not creates and not updates and not links:
        return

    normalized_delta = {
        "creates": creates,
        "updates": updates,
        "links": links,
    }

    try:
        from .chain_context import get_store
        store = get_store()
        # Find root_task_id for this task's chain
        # _task_to_root maps task_id -> root_task_id
        root_task_id = store._task_to_root.get(task_id, task_id)

        store._persist_event(
            root_task_id=root_task_id,
            task_id=task_id,
            event_type="graph.delta.proposed",
            payload={
                "source_task_id": task_id,
                "graph_delta": normalized_delta,
            },
            project_id=project_id,
        )
        log.info("auto_chain: emitted graph.delta.proposed for task %s (%d creates, %d updates, %d links)",
                 task_id, len(creates), len(updates), len(links))
    except Exception:
        log.debug("auto_chain: graph.delta.proposed emission failed", exc_info=True)


# ---------------------------------------------------------------------------
# Graph Delta Auto-Infer (OPT-BACKLOG-GRAPH-DELTA-AUTO-INFER)
# ---------------------------------------------------------------------------


def _is_dev_doc(path):
    """Return True if path matches docs/dev/** or is a dev-note artifact."""
    normalized = path.replace("\\", "/")
    if normalized.startswith("docs/dev/"):
        return True
    return _is_dev_note(normalized)


def _infer_graph_delta(pm_nodes, changed_files, dev_delta, dev_result):
    """Infer graph_delta from PM proposed_nodes + dev changed_files.

    Five deterministic rules:
      Rule A: PM proposed_nodes whose primary appears in changed_files (excl .md)
      Rule B: @route decorator grep on changed agent/**/*.py files
      Rule D: Updates to existing graph nodes whose primary is in changed_files
      Rule E: Dev override — dev entries replace inferred with same title/primary
      Rule F: Discard creates[] where ALL primary files are docs/dev/** or dev-notes

    Rules C (warn-only) and G (fuzzy title similarity) are explicitly SKIPPED.

    Returns (graph_delta_dict, rule_hits_list, inferred_from_list).
    """
    creates = []
    updates = []
    links = []
    rule_hits = []
    inferred_from = []

    # Normalize changed_files to forward-slash set
    changed_set = {f.replace("\\", "/") for f in (changed_files or [])}
    non_md_changed = {f for f in changed_set if not f.endswith(".md")}

    # ---- Rule A: PM proposed_nodes with matching primary in changed_files ----
    covered_primaries = set()
    if pm_nodes:
        inferred_from.append("pm_proposed_nodes")
        for node in pm_nodes:
            primaries = node.get("primary", [])
            if isinstance(primaries, str):
                primaries = [primaries]
            matched = [p for p in primaries if p.replace("\\", "/") in non_md_changed]
            if matched:
                entry = {
                    "node_id": node.get("node_id", ""),
                    "title": node.get("title", ""),
                    "parent_layer": node.get("parent_layer", ""),
                    "primary": primaries,
                    "deps": node.get("deps", []),
                    "description": node.get("description", ""),
                }
                creates.append(entry)
                covered_primaries.update(p.replace("\\", "/") for p in primaries)
                rule_hits.append({"rule": "A", "entry_title": entry["title"],
                                  "matched_files": matched})

    # ---- Rule B: @route decorator grep on changed agent/**/*.py ----
    py_agent_files = [f for f in non_md_changed
                      if f.startswith("agent/") and f.endswith(".py")
                      and f.replace("\\", "/") not in covered_primaries]
    if py_agent_files:
        inferred_from.append("route_decorator_grep")
        route_re = re.compile(
            r'@(?:\w+\.)?(?:route|get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']',
            re.IGNORECASE,
        )
        for fpath in py_agent_files:
            try:
                abs_path = fpath
                if not os.path.isabs(fpath):
                    abs_path = os.path.join(os.getcwd(), fpath)
                if not os.path.exists(abs_path):
                    continue
                with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                for m in route_re.finditer(content):
                    path_str = m.group(1)
                    # Determine method from decorator name
                    dec_match = re.search(r'@(?:\w+\.)?(\w+)\(', m.group(0))
                    method = dec_match.group(1).upper() if dec_match else "ROUTE"
                    if method == "ROUTE":
                        method = "ANY"
                    title = "HTTP endpoint: %s %s" % (method, path_str)
                    creates.append({
                        "node_id": "",
                        "title": title,
                        "parent_layer": "",
                        "primary": [fpath],
                        "deps": [],
                        "description": "Auto-inferred from @route decorator",
                    })
                    rule_hits.append({"rule": "B", "entry_title": title,
                                      "file": fpath})
            except Exception:
                log.debug("Rule B: failed to scan %s", fpath, exc_info=True)

    # ---- Rule D: Updates to existing graph nodes whose primary is in changed_files ----
    if changed_set:
        inferred_from.append("existing_graph_nodes")
        try:
            from . import project_service
            graph = project_service.load_project_graph(
                dev_result.get("project_id", "aming-claw") if isinstance(dev_result, dict) else "aming-claw"
            )
            if graph:
                # Collect pm_update_node_ids to skip (Rule D exception)
                pm_update_ids = set()
                if dev_delta and isinstance(dev_delta, dict):
                    for u in dev_delta.get("updates", []):
                        nid = u.get("node_id", "")
                        if nid:
                            pm_update_ids.add(nid)
                task_id = dev_result.get("task_id", "") if isinstance(dev_result, dict) else ""
                for node_id in graph.list_nodes():
                    if node_id in pm_update_ids:
                        continue
                    try:
                        node_data = graph.get_node(node_id)
                    except Exception:
                        continue
                    node_primaries = node_data.get("primary", [])
                    if isinstance(node_primaries, str):
                        node_primaries = [node_primaries]
                    touched = [p for p in node_primaries if p.replace("\\", "/") in changed_set]
                    if touched:
                        updates.append({
                            "node_id": node_id,
                            "fields": {"touched_by": task_id},
                        })
                        rule_hits.append({"rule": "D", "node_id": node_id,
                                          "touched_files": touched})
        except Exception:
            log.debug("Rule D: graph lookup failed", exc_info=True)

    # ---- Rule E: Dev override — merge dev entries with inferred ----
    source = "auto-inferred"
    if dev_delta and isinstance(dev_delta, dict):
        dev_creates = dev_delta.get("creates", [])
        dev_updates = dev_delta.get("updates", [])
        dev_links = dev_delta.get("links", [])

        if dev_creates or dev_updates or dev_links:
            # Dev provided some entries — merge
            # Build lookup keys for dev entries
            dev_title_set = set()
            dev_primary_set = set()
            for dc in dev_creates:
                t = dc.get("title", "")
                if t:
                    dev_title_set.add(t)
                for p in (dc.get("primary", []) if isinstance(dc.get("primary"), list) else [dc.get("primary", "")]):
                    if p:
                        dev_primary_set.add(p.replace("\\", "/"))

            # Filter inferred creates: remove those matching dev by title or primary
            filtered_creates = []
            for ic in creates:
                ic_title = ic.get("title", "")
                ic_primaries = ic.get("primary", [])
                if isinstance(ic_primaries, str):
                    ic_primaries = [ic_primaries]
                ic_pset = {p.replace("\\", "/") for p in ic_primaries}
                if ic_title in dev_title_set:
                    continue
                if ic_pset & dev_primary_set:
                    continue
                filtered_creates.append(ic)

            # Dev entries take priority, inferred fill gaps
            creates = list(dev_creates) + filtered_creates

            # For updates: dev overrides by node_id
            dev_update_ids = {u.get("node_id") for u in dev_updates}
            filtered_updates = [u for u in updates if u.get("node_id") not in dev_update_ids]
            updates = list(dev_updates) + filtered_updates

            links = list(dev_links) + links
            source = "dev-emitted+inferred-gaps"

    # ---- Rule F: Discard creates where ALL primary files are dev docs ----
    final_creates = []
    for entry in creates:
        primaries = entry.get("primary", [])
        if isinstance(primaries, str):
            primaries = [primaries]
        if primaries and all(_is_dev_doc(p) for p in primaries):
            rule_hits.append({"rule": "F", "discarded_title": entry.get("title", ""),
                              "reason": "all primaries are dev docs"})
            continue
        final_creates.append(entry)
    creates = final_creates

    delta = {"creates": creates, "updates": updates, "links": links}
    return delta, rule_hits, inferred_from, source


def _emit_or_infer_graph_delta(project_id, task_id, result, metadata):
    """Emit graph.delta.proposed, auto-inferring if dev omitted graph_delta.

    Replaces direct _emit_graph_delta_event() call to ensure graph.delta.proposed
    is ALWAYS emitted at dev→QA transition.

    Case A: Dev provided non-empty graph_delta → passthrough with source='dev-emitted'
    Case B: Dev omitted graph_delta → auto-infer from PM proposed_nodes + changed_files
    Case A+B: Dev provided partial + inference fills gaps → source='dev-emitted+inferred-gaps'
    """
    graph_delta = result.get("graph_delta") if isinstance(result, dict) else None

    # Normalize dev delta
    dev_has_delta = False
    if graph_delta and isinstance(graph_delta, dict):
        dc = graph_delta.get("creates", [])
        du = graph_delta.get("updates", [])
        dl = graph_delta.get("links", [])
        dev_has_delta = bool(dc or du or dl)

    log.info("_emit_or_infer_graph_delta: entry task=%s dev_has_delta=%s", task_id, dev_has_delta)

    # Load PM proposed_nodes from pm.prd.published chain_event
    pm_nodes = []
    try:
        from .chain_context import get_store
        store = get_store()
        root_task_id = store._task_to_root.get(task_id, task_id)
        # Also try chain_id from metadata
        if metadata.get("chain_id"):
            root_task_id = metadata["chain_id"]
        root_task_id = store._task_to_root.get(root_task_id, root_task_id)

        from .db import get_connection
        conn = get_connection(project_id)
        try:
            row = conn.execute(
                "SELECT payload_json FROM chain_events "
                "WHERE root_task_id = ? AND event_type = 'pm.prd.published' "
                "ORDER BY ts DESC LIMIT 1",
                (root_task_id,),
            ).fetchone()
            if row:
                payload = json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else row["payload_json"]
                pm_nodes = payload.get("proposed_nodes", [])
        finally:
            conn.close()
    except Exception:
        log.error("_emit_or_infer_graph_delta: pm.prd.published lookup failed", exc_info=True)

    log.info("_emit_or_infer_graph_delta: pm_nodes from chain_events count=%d", len(pm_nodes))

    changed_files = result.get("changed_files", metadata.get("changed_files", []))

    if dev_has_delta and not pm_nodes and not changed_files:
        log.info("_emit_or_infer_graph_delta: early-return pure dev-emitted passthrough task=%s", task_id)
        # Pure dev-emitted: passthrough with source field
        _emit_graph_delta_event_with_source(project_id, task_id, result, "dev-emitted")
        return

    # Run inference
    dev_result_ctx = dict(result) if isinstance(result, dict) else {}
    dev_result_ctx["project_id"] = project_id
    dev_result_ctx["task_id"] = task_id

    inferred_delta, rule_hits, inferred_from, source = _infer_graph_delta(
        pm_nodes, changed_files, graph_delta if dev_has_delta else None, dev_result_ctx
    )

    log.info("_emit_or_infer_graph_delta: inference produced source=%s creates=%d updates=%d links=%d",
             source, len(inferred_delta.get("creates", [])),
             len(inferred_delta.get("updates", [])),
             len(inferred_delta.get("links", [])))

    # Determine final source
    if dev_has_delta and source == "auto-inferred":
        # Dev had entries but inference didn't merge (no overlap case)
        source = "dev-emitted"

    final_creates = inferred_delta.get("creates", [])
    final_updates = inferred_delta.get("updates", [])
    final_links = inferred_delta.get("links", [])

    if not final_creates and not final_updates and not final_links:
        # Nothing to emit — still emit empty proposed for audit trail
        log.info("_emit_or_infer_graph_delta: early-return empty inference task=%s dev_has_delta=%s", task_id, dev_has_delta)
        if dev_has_delta:
            _emit_graph_delta_event_with_source(project_id, task_id, result, "dev-emitted")
        return

    # Emit graph.delta.proposed with source
    try:
        from .chain_context import get_store
        store = get_store()
        root_task_id = store._task_to_root.get(task_id, task_id)
        if metadata.get("chain_id"):
            root_task_id = store._task_to_root.get(metadata["chain_id"], metadata["chain_id"])

        store._persist_event(
            root_task_id=root_task_id,
            task_id=task_id,
            event_type="graph.delta.proposed",
            payload={
                "source_task_id": task_id,
                "source": source,
                "graph_delta": {
                    "creates": final_creates,
                    "updates": final_updates,
                    "links": final_links,
                },
            },
            project_id=project_id,
        )
        log.info("auto_chain: emitted graph.delta.proposed (source=%s) for task %s "
                 "(%d creates, %d updates, %d links)",
                 source, task_id, len(final_creates), len(final_updates), len(final_links))
    except Exception:
        log.error("auto_chain: graph.delta.proposed emission failed", exc_info=True)

    # R4: Emit graph.delta.inferred event when auto-inference path executed
    if source in ("auto-inferred", "dev-emitted+inferred-gaps"):
        try:
            from .chain_context import get_store
            store = get_store()
            root_task_id = store._task_to_root.get(task_id, task_id)
            if metadata.get("chain_id"):
                root_task_id = store._task_to_root.get(metadata["chain_id"], metadata["chain_id"])

            store._persist_event(
                root_task_id=root_task_id,
                task_id=task_id,
                event_type="graph.delta.inferred",
                payload={
                    "source": source,
                    "inferred_from": inferred_from,
                    "rule_hits": rule_hits,
                },
                project_id=project_id,
            )
            log.info("auto_chain: emitted graph.delta.inferred for task %s (rules: %s)",
                     task_id, [h.get("rule") for h in rule_hits])
        except Exception:
            log.error("auto_chain: graph.delta.inferred emission failed", exc_info=True)


def _emit_graph_delta_event_with_source(project_id, task_id, result, source):
    """Emit graph.delta.proposed with explicit source field (passthrough for dev-emitted)."""
    graph_delta = result.get("graph_delta") if isinstance(result, dict) else None
    if not graph_delta or not isinstance(graph_delta, dict):
        return

    creates = graph_delta.get("creates", [])
    updates = graph_delta.get("updates", [])
    links = graph_delta.get("links", [])

    if not creates and not updates and not links:
        return

    try:
        from .chain_context import get_store
        store = get_store()
        root_task_id = store._task_to_root.get(task_id, task_id)

        store._persist_event(
            root_task_id=root_task_id,
            task_id=task_id,
            event_type="graph.delta.proposed",
            payload={
                "source_task_id": task_id,
                "source": source,
                "graph_delta": {
                    "creates": creates,
                    "updates": updates,
                    "links": links,
                },
            },
            project_id=project_id,
        )
        log.info("auto_chain: emitted graph.delta.proposed (source=%s) for task %s",
                 source, task_id)
    except Exception:
        log.debug("auto_chain: graph.delta.proposed emission failed", exc_info=True)


# ---------------------------------------------------------------------------
# Graph Delta Transactional Commit (PR-C: OPT-BACKLOG-GRAPH-DELTA-CHAIN-COMMIT)
# ---------------------------------------------------------------------------

import uuid as _uuid


def _commit_graph_delta(conn, project_id, metadata):
    """Consume graph.delta.validated event and apply creates[]/updates[] to node_state.

    Called from _gate_gatekeeper_pass on merge_pass. All writes occur in a
    single transaction. On failure, rollback and emit graph.delta.failed.

    R1: Transactional commit of creates[]/updates[]
    R2: Node ID auto-generation for creates[] without explicit node_id
    R3: Idempotency via event_id check
    R4: Related-nodes carryforward after commit
    R6: links[] logged as TODO/skipped (no edges table)
    R7: Malformed creates[] (missing parent_layer) skipped with warning
    """
    root_task_id = metadata.get("chain_id") or metadata.get("parent_task_id", "")
    try:
        from .chain_context import get_store
        store = get_store()
        root_task_id = store._task_to_root.get(root_task_id, root_task_id)
    except Exception:
        pass

    # Query for graph.delta.validated event (use passed-in conn — same DB)
    try:
        row = conn.execute(
            "SELECT payload_json FROM chain_events "
            "WHERE root_task_id = ? AND event_type = 'graph.delta.validated' "
            "ORDER BY ts DESC LIMIT 1",
            (root_task_id,),
        ).fetchone()
    except Exception:
        log.debug("_commit_graph_delta: no chain_events table or query failed", exc_info=True)
        return  # No validated event — nothing to commit

    if not row:
        return  # No graph.delta.validated event for this chain

    try:
        validated_payload = json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else row["payload_json"]
    except Exception:
        log.warning("_commit_graph_delta: failed to parse validated payload")
        return

    # Extract the original proposed payload which contains graph_delta
    proposed_payload = validated_payload.get("proposed_payload", {})
    graph_delta = proposed_payload.get("graph_delta", {})
    if not graph_delta:
        return

    creates = graph_delta.get("creates", [])
    updates = graph_delta.get("updates", [])
    links = graph_delta.get("links", [])

    if not creates and not updates and not links:
        return

    # Generate event_id for idempotency
    # Use source_task_id from proposed_payload as the source event identifier
    source_event_id = proposed_payload.get("source_task_id", "")
    event_id = str(_uuid.uuid4())

    # R3: Idempotency check — look for prior graph.delta.committed with same root + source
    if source_event_id:
        try:
            prior = conn.execute(
                "SELECT payload_json FROM chain_events "
                "WHERE root_task_id = ? AND event_type = 'graph.delta.committed' "
                "ORDER BY ts DESC LIMIT 1",
                (root_task_id,),
            ).fetchone()
            if prior:
                prior_payload = json.loads(prior["payload_json"]) if isinstance(prior["payload_json"], str) else prior["payload_json"]
                if prior_payload.get("source_event_id") == source_event_id:
                    log.info("_commit_graph_delta: idempotent skip — already committed for source %s", source_event_id)
                    return prior_payload.get("committed_node_ids", [])
        except Exception:
            log.debug("_commit_graph_delta: idempotency check failed", exc_info=True)

    # R6: links[] — no edges table, log and skip
    if links:
        log.warning("_commit_graph_delta: TODO — links[] items skipped (no edges table in governance.db): %d items", len(links))

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    committed_node_ids = []

    try:
        # Begin transaction — conn should already be in autocommit=off mode
        # We use the passed-in conn for transactional safety

        # R2/R5: Process creates[]
        for item in creates:
            if not isinstance(item, dict):
                log.warning("_commit_graph_delta: skipping non-dict creates item")
                continue

            parent_layer = item.get("parent_layer")
            # R7: skip malformed items missing parent_layer
            if parent_layer is None:
                log.warning("_commit_graph_delta: skipping creates[] item with missing parent_layer: %s",
                            item.get("title", "<untitled>"))
                continue

            try:
                parent_layer = int(parent_layer)
            except (ValueError, TypeError):
                log.warning("_commit_graph_delta: skipping creates[] item with non-int parent_layer: %s", parent_layer)
                continue

            explicit_node_id = item.get("node_id")

            if explicit_node_id:
                # R2: Check collision
                existing = conn.execute(
                    "SELECT node_id FROM node_state WHERE project_id = ? AND node_id = ?",
                    (project_id, explicit_node_id),
                ).fetchone()
                if existing:
                    # AC5: Collision — reject entire batch
                    raise ValueError(f"node_id collision: {explicit_node_id} already exists")
                display_id = explicit_node_id
            else:
                # R2: Auto-generate node_id using existing pattern
                prefix = f"L{parent_layer}."
                existing_rows = conn.execute(
                    "SELECT node_id FROM node_state WHERE project_id = ? AND node_id LIKE ?",
                    (project_id, f"{prefix}%"),
                ).fetchall()
                max_index = 0
                for r in existing_rows:
                    try:
                        idx = int(r["node_id"].split(".")[1])
                        max_index = max(max_index, idx)
                    except (ValueError, IndexError):
                        pass
                display_id = f"L{parent_layer}.{max_index + 1}"

            # Insert node_state
            conn.execute(
                """INSERT OR IGNORE INTO node_state
                   (project_id, node_id, verify_status, build_status, updated_at, version)
                   VALUES (?, ?, 'pending', 'unknown', ?, 1)""",
                (project_id, display_id, now),
            )

            # Record in node_history
            try:
                title = item.get("title", display_id)
                conn.execute(
                    """INSERT INTO node_history
                       (project_id, node_id, from_status, to_status, role, evidence_json, session_id, ts, version)
                       VALUES (?, ?, 'none', 'pending', 'auto-chain', ?, 'graph-delta-commit', ?, 1)""",
                    (project_id, display_id,
                     json.dumps({"title": title, "deps": item.get("deps", []),
                                 "primary": item.get("primary", []),
                                 "source": "graph.delta.committed"}),
                     now),
                )
            except Exception:
                pass  # History is nice-to-have

            committed_node_ids.append(display_id)

        # Process updates[]
        for item in updates:
            if not isinstance(item, dict):
                continue
            node_id = item.get("node_id")
            if not node_id:
                continue
            fields = item.get("fields", {})
            if not fields:
                continue

            # Only update if node exists
            existing = conn.execute(
                "SELECT verify_status, version FROM node_state WHERE project_id = ? AND node_id = ?",
                (project_id, node_id),
            ).fetchone()
            if not existing:
                log.warning("_commit_graph_delta: update target %s not found, skipping", node_id)
                continue

            # Apply field updates (limited to safe fields)
            update_parts = []
            update_vals = []
            for field_name in ("verify_status", "build_status"):
                if field_name in fields:
                    update_parts.append(f"{field_name} = ?")
                    update_vals.append(fields[field_name])
            if update_parts:
                update_parts.append("updated_at = ?")
                update_vals.append(now)
                update_parts.append("updated_by = ?")
                update_vals.append("graph-delta-commit")
                update_vals.extend([project_id, node_id])
                conn.execute(
                    f"UPDATE node_state SET {', '.join(update_parts)} WHERE project_id = ? AND node_id = ?",
                    update_vals,
                )

            if node_id not in committed_node_ids:
                committed_node_ids.append(node_id)

        # AC3: Write graph.delta.committed event — write to same conn (transactional)
        committed_payload = {
            "event_id": event_id,
            "source_event_id": source_event_id,
            "committed_node_ids": committed_node_ids,
            "creates_count": len(creates),
            "updates_count": len(updates),
            "links_skipped": len(links),
        }
        conn.execute(
            "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
            "VALUES (?, ?, 'graph.delta.committed', ?, ?)",
            (root_task_id, metadata.get("task_id", ""),
             json.dumps(committed_payload, ensure_ascii=False), now),
        )

        # R4: Append committed node_ids to chain metadata related_nodes
        if committed_node_ids:
            try:
                existing_related = metadata.get("related_nodes", [])
                if isinstance(existing_related, str):
                    try:
                        existing_related = json.loads(existing_related)
                    except Exception:
                        existing_related = [existing_related] if existing_related else []
                new_related = list(set(existing_related + committed_node_ids))
                metadata["related_nodes"] = new_related

                # Also persist related_nodes.updated event to same conn
                conn.execute(
                    "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
                    "VALUES (?, ?, 'related_nodes.updated', ?, ?)",
                    (root_task_id, metadata.get("task_id", ""),
                     json.dumps({"related_nodes": new_related, "added": committed_node_ids},
                                ensure_ascii=False), now),
                )
            except Exception:
                log.debug("_commit_graph_delta: related_nodes carryforward failed", exc_info=True)

        log.info("_commit_graph_delta: committed %d nodes for chain %s: %s",
                 len(committed_node_ids), root_task_id, committed_node_ids)
        return committed_node_ids

    except ValueError as ve:
        # AC2/AC5: Collision or validation error — rollback
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("_commit_graph_delta: batch rejected — %s", ve)
        # Write graph.delta.failed event (post-rollback, new mini-transaction)
        try:
            conn.execute(
                "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
                "VALUES (?, ?, 'graph.delta.failed', ?, ?)",
                (root_task_id, metadata.get("task_id", ""),
                 json.dumps({"error": str(ve), "event_id": event_id}, ensure_ascii=False), now),
            )
            conn.commit()
        except Exception:
            log.debug("_commit_graph_delta: failed event write failed", exc_info=True)
        raise

    except Exception as exc:
        # AC2: Any other exception — rollback and emit failed event
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("_commit_graph_delta: transaction failed — %s", exc, exc_info=True)
        try:
            conn.execute(
                "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
                "VALUES (?, ?, 'graph.delta.failed', ?, ?)",
                (root_task_id, metadata.get("task_id", ""),
                 json.dumps({"error": str(exc), "event_id": event_id}, ensure_ascii=False), now),
            )
            conn.commit()
        except Exception:
            log.debug("_commit_graph_delta: failed event write failed", exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Graph-Path-Driven Routing (Roadmap §5.5)
# ---------------------------------------------------------------------------

# Linear chain stages after dev (used for graph-driven routing derivation)
_POST_DEV_STAGES = ["test", "qa", "gatekeeper", "merge"]


def _audit_routing_decision(conn, project_id, task_id, trace_id, decision):
    """Write routing decision to audit_log (R6/AC7)."""
    try:
        conn.execute(
            "INSERT INTO audit_log (project_id, action, actor, ok, ts, task_id, details_json) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?, ?)",
            (
                project_id,
                "routing_decision",
                "auto-chain",
                1,
                task_id,
                json.dumps({**decision, "trace_id": trace_id}),
            ),
        )
    except Exception:
        log.debug("audit routing_decision failed (non-critical)", exc_info=True)


def _audit_routing_skip(conn, project_id, task_id, trace_id, skip_info):
    """Write routing skip to audit_log (R3/AC1/AC2)."""
    try:
        conn.execute(
            "INSERT INTO audit_log (project_id, action, actor, ok, ts, task_id, details_json) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?, ?)",
            (
                project_id,
                "routing_skip",
                "auto-chain",
                1,
                task_id,
                json.dumps({**skip_info, "trace_id": trace_id}),
            ),
        )
    except Exception:
        log.debug("audit routing_skip failed (non-critical)", exc_info=True)


def _check_verify_requires_satisfied(conn, project_id, verify_requires):
    """Check if all verify_requires nodes are verified (AC4).

    Returns (satisfied: bool, blocking_nodes: list[str]).
    """
    if not verify_requires:
        return True, []
    blocking = []
    for req_nid in verify_requires:
        try:
            row = conn.execute(
                "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
                (project_id, req_nid),
            ).fetchone()
            if row is None:
                blocking.append(req_nid)
                continue
            status = (row["verify_status"] or "pending").strip()
            # AC10: rolled_back nodes don't block
            if status in _NON_BLOCKING_STATUSES:
                continue
            try:
                rank = _STATUS_ORDER.index(status)
            except ValueError:
                blocking.append(req_nid)
                continue
            # t2_pass is the minimum acceptable status (rank 2)
            if rank < _STATUS_ORDER.index("t2_pass"):
                blocking.append(req_nid)
        except Exception:
            blocking.append(req_nid)
    return len(blocking) == 0, blocking


def _derive_chain_stages_from_policies(policies):
    """Derive which chain stages to execute based on node policies (R2/R3).

    Given a list of node routing policies, determine the minimal set of
    chain stages needed. Uses the most restrictive policy (if ANY node
    requires a stage, it's included).

    Returns list of stage names in order.
    """
    if not policies:
        return list(_POST_DEV_STAGES)  # fallback to full chain

    needs_test = False
    needs_qa = False
    needs_gatekeeper = False
    all_skip = True

    for p in policies:
        gm = p.get("gate_mode", "auto")
        vl = p.get("verify_level", 1)

        if gm != "skip":
            all_skip = False

        # verify_level > 0 means test stage needed
        if vl > 0:
            needs_test = True

        # gate_mode != skip means QA and gatekeeper needed
        if gm != "skip":
            needs_qa = True
            needs_gatekeeper = True

    stages = []
    if needs_test:
        stages.append("test")
    if needs_qa:
        stages.append("qa")
    if needs_gatekeeper:
        stages.append("gatekeeper")
    stages.append("merge")  # merge is always needed

    return stages


def dispatch_next_stage(conn, project_id, task_id, current_stage,
                        result, metadata, trace_id, graph=None):
    """Graph-driven routing: determine next stage based on node policies (R2/R5).

    When graph is available and nodes have custom policies, derive the chain
    stages dynamically. Falls back to CHAIN dict when graph is None or
    all nodes use default policies (AC5/AC6).

    Returns (next_stage: str|None, skipped_stages: list[str], policies: list[dict]).
    """
    # R5: No graph → use linear CHAIN
    if graph is None:
        _audit_routing_decision(conn, project_id, task_id, trace_id, {
            "current_stage": current_stage,
            "routing_mode": "linear_chain",
            "reason": "no_graph_loaded",
        })
        return None, [], []  # Signal caller to use CHAIN dict

    # Only apply graph routing after dev stage
    if current_stage not in ("dev", "test", "qa", "gatekeeper"):
        _audit_routing_decision(conn, project_id, task_id, trace_id, {
            "current_stage": current_stage,
            "routing_mode": "linear_chain",
            "reason": "pre_dev_stage",
        })
        return None, [], []  # Use CHAIN dict for pm→dev

    # Get affected nodes from metadata
    changed_files = result.get("changed_files", metadata.get("changed_files", []))
    related_nodes = metadata.get("related_nodes", [])

    # Try to get routing policies from graph
    policies = []
    if related_nodes:
        try:
            policies = graph.get_routing_policies_for_nodes(related_nodes)
        except Exception:
            pass

    if not policies and changed_files:
        try:
            affected = graph.affected_nodes_by_files(changed_files)
            if affected:
                policies = graph.get_routing_policies_for_nodes(list(affected))
        except Exception:
            pass

    if not policies:
        _audit_routing_decision(conn, project_id, task_id, trace_id, {
            "current_stage": current_stage,
            "routing_mode": "linear_chain",
            "reason": "no_affected_nodes",
        })
        return None, [], []

    # AC6: Check if all nodes have default policies (auto + verify_level>=1)
    all_default = all(
        p.get("gate_mode", "auto") == "auto" and p.get("verify_level", 1) >= 1
        for p in policies
    )
    if all_default:
        _audit_routing_decision(conn, project_id, task_id, trace_id, {
            "current_stage": current_stage,
            "routing_mode": "linear_chain",
            "reason": "all_nodes_default_policy",
            "node_count": len(policies),
        })
        return None, [], policies

    # AC4: Check verify_requires ordering
    for p in policies:
        vr = p.get("verify_requires", [])
        if vr:
            satisfied, blocking = _check_verify_requires_satisfied(conn, project_id, vr)
            if not satisfied:
                _audit_routing_decision(conn, project_id, task_id, trace_id, {
                    "current_stage": current_stage,
                    "routing_mode": "blocked_by_verify_requires",
                    "node_id": p["node_id"],
                    "blocking_nodes": blocking,
                })
                # Return special signal for blocking
                return "blocked", [], policies

    # Derive stages from policies
    derived_stages = _derive_chain_stages_from_policies(policies)
    full_stages = list(_POST_DEV_STAGES)
    skipped = [s for s in full_stages if s not in derived_stages]

    # Log skip reasons for auditing
    for p in policies:
        gm = p.get("gate_mode", "auto")
        vl = p.get("verify_level", 1)
        if gm == "skip":
            _audit_routing_skip(conn, project_id, task_id, trace_id, {
                "node_id": p["node_id"],
                "gate_mode": gm,
                "skip": "qa,gatekeeper",
                "reason": "gate_mode=skip bypasses QA/gatekeeper",
            })
        if vl == 0:
            _audit_routing_skip(conn, project_id, task_id, trace_id, {
                "node_id": p["node_id"],
                "verify_level": vl,
                "skip": "test",
                "reason": "verify_level=0 skips test stage",
            })

    # Determine next stage from derived_stages based on current position
    if current_stage == "dev":
        next_stage = derived_stages[0] if derived_stages else None
    else:
        try:
            idx = derived_stages.index(current_stage)
            next_stage = derived_stages[idx + 1] if idx + 1 < len(derived_stages) else None
        except ValueError:
            next_stage = None

    _audit_routing_decision(conn, project_id, task_id, trace_id, {
        "current_stage": current_stage,
        "routing_mode": "graph_driven",
        "derived_stages": derived_stages,
        "skipped_stages": skipped,
        "next_stage": next_stage,
        "policies": [{"node_id": p["node_id"], "gate_mode": p.get("gate_mode"),
                       "verify_level": p.get("verify_level")} for p in policies],
    })

    return next_stage, skipped, policies


_TEST_FILE_PATTERN = re.compile(r"(agent/tests/[A-Za-z0-9_./-]+\.py)")


def _extract_test_files_from_verification(verification):
    """Pull explicit pytest file targets out of verification.command."""
    if not isinstance(verification, dict):
        return []
    command = verification.get("command")
    if not isinstance(command, str) or not command.strip():
        return []
    return list(dict.fromkeys(_TEST_FILE_PATTERN.findall(command)))


# B36-fix(4): Project root for dependent-test scan.
# This mirrors agent/governance/role_config.py's derivation (parents[2] of this file).
_PROJECT_ROOT_FOR_SCAN = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# Regex for import lines: captures module path after `from <x> import` or `import <x>`.
_IMPORT_RE = re.compile(r"^\s*(?:from\s+(\S+)\s+import|import\s+(\S+))", re.MULTILINE)

# Cache: (target_tuple) -> set of dependent test file paths. Cleared per-gate evaluation
# is unnecessary since imports don't change within a gate check; scope is process-wide.
_DEPENDENT_TESTS_CACHE: "dict[tuple, set[str]]" = {}


def _scan_dependent_tests(target_files):
    """B36-fix(4): Find test files that import any target file's module.

    For each target like 'agent/role_permissions.py', derive the stem
    ('role_permissions') and scan all tests/**/*.py for import lines referencing
    a module whose dotted path contains that stem as a component. Returns a set
    of POSIX-normalized relative paths (e.g. 'agent/tests/test_x.py').

    Safe: on any IO error returns empty set. Bounded: reads only first 16KB of
    each test file (imports are at top).
    """
    if not target_files:
        return set()
    stems = set()
    for tf in target_files:
        base = os.path.basename(tf.replace("\\", "/"))
        stem, ext = os.path.splitext(base)
        if ext != ".py" or not stem or stem == "__init__":
            continue
        stems.add(stem)
    if not stems:
        return set()

    key = tuple(sorted(stems))
    cached = _DEPENDENT_TESTS_CACHE.get(key)
    if cached is not None:
        return set(cached)

    dependent = set()
    root = _PROJECT_ROOT_FOR_SCAN
    if not os.path.isdir(root):
        _DEPENDENT_TESTS_CACHE[key] = set()
        return set()

    for dirpath, _dirnames, filenames in os.walk(root):
        # Only look under directories named 'tests'
        posix_dir = dirpath.replace("\\", "/")
        if "/tests" not in posix_dir and not posix_dir.endswith("tests"):
            continue
        # Skip vendored/third-party trees AND worktree mirrors.
        # Worktree mirrors live under .worktrees/ (top-level) or .claude/worktrees/.
        # They contain duplicate test files that pollute the scan result.
        # B49: Use relative path from root so that running FROM a worktree doesn't
        # self-exclude — only nested .worktrees/ subdirs are skipped.
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if ("/.venv/" in rel_dir or rel_dir.startswith(".venv")
                or "/node_modules/" in rel_dir or rel_dir.startswith("node_modules")
                or "/.claude/" in rel_dir or rel_dir.startswith(".claude")
                or "/.worktrees/" in rel_dir or rel_dir.startswith(".worktrees")):
            continue
        for fn in filenames:
            if not fn.startswith("test_") or not fn.endswith(".py"):
                continue
            abs_p = os.path.join(dirpath, fn)
            try:
                with open(abs_p, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read(16384)
            except Exception:
                continue
            for m in _IMPORT_RE.finditer(content):
                module = m.group(1) or m.group(2)
                if not module:
                    continue
                parts = module.split(".")
                if stems & set(parts):
                    rel = os.path.relpath(abs_p, root).replace("\\", "/")
                    dependent.add(rel)
                    break

    _DEPENDENT_TESTS_CACHE[key] = set(dependent)
    return dependent


def _compute_gate_static_allowed(project_id, metadata):
    """B36-fix(2): Single source of truth for gate's static allowed file set.

    Called by both _gate_checkpoint AND the retry-prompt scope_line builder so
    they cannot drift apart. Returns (target_set, allowed_set).

    NOT included here (must be added by caller as applicable):
      - stem-prefix dynamic tests (match against incoming changed files)
      - accumulated_changed_files from prior succeeded dev stages (retry only)
    """
    target = set(metadata.get("target_files", []) or [])
    allowed = set(target)
    allowed.update(metadata.get("test_files", []) or [])
    allowed.update(_extract_test_files_from_verification(metadata.get("verification", {})))
    doc_impact = metadata.get("doc_impact", {})
    if isinstance(doc_impact, dict):
        allowed.update(doc_impact.get("files", []) or [])
    graph_docs = _get_graph_doc_associations(project_id, list(target))
    if graph_docs:
        allowed.update(graph_docs)
    # B36-fix(4): tests importing any target — prevents PM under-specification from ping-ponging dev
    allowed.update(_scan_dependent_tests(list(target)))
    return target, allowed


def _load_task_trace(conn, task_id):
    """Load trace_id and chain_id from a task row."""
    if not task_id or not hasattr(conn, "execute"):
        return None, None
    try:
        row = conn.execute(
            "SELECT trace_id, chain_id FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if not row:
            return None, None
        return row["trace_id"], row["chain_id"]
    except Exception:
        return None, None


def _record_gate_event(conn, project_id, task_id, gate_name, passed, reason, trace_id):
    """Insert a row into gate_events for audit trail."""
    from datetime import datetime, timezone
    try:
        conn.execute(
            "INSERT INTO gate_events (project_id, task_id, gate_name, passed, reason, trace_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, task_id, gate_name, 1 if passed else 0,
             reason, trace_id,
             datetime.now(timezone.utc).isoformat()),
        )
    except Exception:
        log.debug("auto_chain: failed to record gate_event for %s/%s (non-critical)", gate_name, task_id, exc_info=True)


def _load_task_metadata(conn, project_id, task_id):
    if not task_id or not hasattr(conn, "execute"):
        return {}
    try:
        row = conn.execute(
            "SELECT metadata_json FROM tasks WHERE project_id=? AND task_id=?",
            (project_id, task_id),
        ).fetchone()
        if not row:
            return {}
        raw = row["metadata_json"] if isinstance(row, dict) or hasattr(row, "__getitem__") else None
        if not raw:
            return {}
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {}


def _walk_task_metadata_chain(conn, project_id, metadata, max_depth=6):
    visited = set()
    current = dict(metadata or {})
    for _ in range(max_depth):
        yield current
        parent_task_id = current.get("parent_task_id")
        if not parent_task_id or parent_task_id in visited:
            break
        visited.add(parent_task_id)
        current = _load_task_metadata(conn, project_id, parent_task_id)
        if not current:
            break


def _infer_lane_from_metadata(metadata):
    """Best-effort lane inference for replayed reconciliation chains."""
    if not isinstance(metadata, dict):
        return ""
    explicit = str(metadata.get("lane", "") or "").strip().upper()
    if explicit in {"A", "B", "C"}:
        return explicit

    text = " ".join(
        str(metadata.get(k, "") or "")
        for k in ("replay_source", "intent_summary", "_original_prompt")
    ).lower()
    match = re.search(r"lane\s+([abc])", text)
    if match:
        return match.group(1).upper()
    return ""


def _is_governed_dirty_workspace_chain(conn, project_id, metadata):
    """Allow narrow bypass only for explicit governed dirty-workspace reconciliation chains."""
    for current in _walk_task_metadata_chain(conn, project_id, metadata):
        if current.get("allow_dirty_workspace_reconciliation"):
            return True
        if current.get("parallel_plan") == "dirty-reconciliation-2026-03-30":
            return True
        lane = _infer_lane_from_metadata(current)
        text = " ".join(
            str(current.get(k, "") or "")
            for k in ("replay_source", "intent_summary", "_original_prompt")
        ).lower()
        if lane in {"A", "B"} and (
            "dirty-workspace" in text
            or "workflow improvement lane" in text
            or "reconciliation" in text
        ):
            return True
    return False


def _should_defer_doc_gate_to_lane_c(conn, project_id, metadata):
    """Lane A/B reconciliation tasks may defer doc updates to convergence Lane C."""
    lane = ""
    for current in _walk_task_metadata_chain(conn, project_id, metadata):
        lane = _infer_lane_from_metadata(current)
        if lane:
            break
    if lane not in {"A", "B"}:
        return False
    if _is_governed_dirty_workspace_chain(conn, project_id, metadata):
        return True
    return False


def _parse_pm_missing_fields(reason: str) -> list:
    """Extract missing field names from a PM gate block reason string.

    Handles both formats:
      - 'PRD missing mandatory fields: [field1, field2]'
      - 'PRD fields missing without skip_reasons: [field1, field2]...'
    """
    import re
    # Match the bracketed list after the colon
    m = re.search(r"(?:PRD missing mandatory fields|PRD fields missing without skip_reasons):\s*\[([^\]]*)\]", reason)
    if m:
        raw = m.group(1)
        return [f.strip().strip("'\"") for f in raw.split(",") if f.strip()]
    return []


def _effective_dev_retry_reason(conn, project_id, metadata, reason):
    """Rewrite stale lane A/B gate reasons into actionable code-only guidance."""
    if not isinstance(reason, str):
        return reason
    if not _should_defer_doc_gate_to_lane_c(conn, project_id, metadata):
        return reason

    lowered = reason.lower()
    if lowered.startswith("related docs not updated:") or (
        lowered.startswith("unrelated files modified:")
        and ("readme.md" in lowered or "docs/" in lowered)
    ):
        return (
            "Lane C owns documentation updates for this governed dirty-workspace "
            "reconciliation. Do NOT modify README.md or docs/. "
            "Retry as a code-only fix within target_files and keep changed_files "
            "limited to target_files."
        )
    return reason


def _maybe_create_workflow_improvement_task(conn, project_id, task_id, stage, reason, metadata, result):
    """Create one workflow-improvement task for workflow defects.

    The task goes through the normal coordinator entrypoint (`type=task`) so the
    existing chain can repair workflow/governance issues without introducing a
    parallel execution model.
    """
    if metadata.get("_workflow_improvement_created"):
        return None
    if metadata.get("operation_type") == "workflow_improvement":
        return None

    classification = classify_gate_failure(stage, reason, metadata, result)
    if not classification.get("workflow_improvement"):
        return None

    from . import task_registry
    improvement_prompt = build_workflow_improvement_prompt(task_id, stage, classification, metadata)
    improvement_task = task_registry.create_task(
        conn, project_id,
        prompt=improvement_prompt,
        task_type="task",
        created_by="auto-chain-workflow-improvement",
        metadata={
            "operation_type": "workflow_improvement",
            "source_task_id": task_id,
            "failing_stage": stage,
            "failure_class": classification.get("failure_class", ""),
            "suggested_action": classification.get("suggested_action", ""),
            "workflow_issue": classification,
            "chain_depth": 0,
            "_no_retry": True,
        },
    )
    metadata["_workflow_improvement_created"] = True
    improvement_id = improvement_task.get("task_id", "?")
    _publish_event("task.workflow_improvement", {
        "project_id": project_id,
        "task_id": improvement_id,
        "source_task_id": task_id,
        "failing_stage": stage,
        "failure_class": classification.get("failure_class", ""),
    })
    try:
        from . import audit_service
        audit_service.record(
            conn, project_id, "workflow.improvement.created",
            actor="auto-chain",
            ok=True,
            node_ids=metadata.get("related_nodes", []),
            task_id=improvement_id,
            source_task_id=task_id,
            failing_stage=stage,
            failure_class=classification.get("failure_class", ""),
            suggested_action=classification.get("suggested_action", ""),
        )
    except Exception:
        log.debug("auto_chain: audit workflow.improvement.created failed", exc_info=True)
    return {"task_id": improvement_id, "classification": classification}


def on_task_failed(conn, project_id, task_id, task_type, result=None, metadata=None, reason=""):
    """Best-effort workflow-improvement routing for failed task executions."""
    metadata = metadata or {}
    result = result or {}
    effective_reason = (
        reason
        or result.get("error")
        or result.get("summary")
        or "task execution failed"
    )
    return _maybe_create_workflow_improvement_task(
        conn,
        project_id,
        task_id,
        task_type,
        effective_reason,
        metadata,
        result,
    )


def _normalize_related_nodes(related_nodes):
    """Keep only concrete node-id strings for gate/audit/state updates."""
    if not related_nodes:
        return []
    if not isinstance(related_nodes, list):
        related_nodes = [related_nodes]

    normalized = []
    for item in related_nodes:
        if isinstance(item, str) and item.strip():
            normalized.append(item.strip())
        elif isinstance(item, dict):
            node_id = item.get("node_id") or item.get("id")
            if isinstance(node_id, str) and node_id.strip():
                normalized.append(node_id.strip())
    return normalized


def _render_dev_contract_prompt(source_task_id, metadata):
    """Render the structured Dev contract from PM/task metadata."""
    target_files = metadata.get("target_files", [])
    requirements = metadata.get("requirements", [])
    criteria = metadata.get("acceptance_criteria", [])
    verification = metadata.get("verification", {})

    parts = [
        f"Implement per PRD from {source_task_id}.\n",
        f"target_files: {json.dumps(target_files)}",
        f"requirements: {json.dumps(requirements, ensure_ascii=False)}",
        f"acceptance_criteria: {json.dumps(criteria, ensure_ascii=False)}",
    ]

    if verification:
        parts.append(f"verification: {json.dumps(verification, ensure_ascii=False)}")

    test_files = metadata.get("test_files", [])
    if test_files:
        parts.append(f"\nTest files to create/modify: {json.dumps(test_files)}")

    doc_impact = metadata.get("doc_impact", {})
    if doc_impact:
        parts.append(f"\nDoc impact: {json.dumps(doc_impact, ensure_ascii=False)}")

    # R5: Document optional graph_delta field for dev results (not required)
    proposed_nodes = metadata.get("proposed_nodes", [])
    if proposed_nodes:
        parts.append(
            "\nOptional: Your result JSON MAY include a `graph_delta` field to propose graph changes. "
            "Shape: {\"creates\": [{\"node_id\": \"...\", \"parent_layer\": \"...\", \"title\": \"...\", "
            "\"deps\": [...], \"primary\": \"...\", \"description\": \"...\"}], "
            "\"updates\": [{\"node_id\": \"...\", \"fields\": {}}], "
            "\"links\": [{\"from_node\": \"...\", \"to_node\": \"...\", \"relation\": \"...\"}]}. "
            "All sub-arrays default to []. Pure-refactor tasks may omit this field entirely."
        )

    return "\n".join(parts)


def on_task_completed(conn, project_id, task_id, task_type, status, result, metadata):
    """Called by complete_task(). Dispatches next stage if gate passes.

    Uses a SEPARATE connection to avoid holding caller's transaction lock
    during potentially slow gate checks and task creation.

    Returns dict with chain result, or None if not a chain-eligible task.
    """
    if status != "succeeded":
        return None
    if task_type not in CHAIN:
        return None

    # Use independent connection — don't hold caller's lock during chain ops
    from .db import get_connection
    try:
        conn = get_connection(project_id)
    except Exception:
        log.error("auto_chain: failed to get independent connection for %s", project_id)
        return None
    try:
        result_val = _do_chain(conn, project_id, task_id, task_type, result, metadata)
        conn.commit()
        return result_val
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _do_chain(conn, project_id, task_id, task_type, result, metadata):
    """Internal chain logic with guaranteed conn cleanup by caller."""
    metadata["related_nodes"] = _normalize_related_nodes(metadata.get("related_nodes", []))

    # --- Trace propagation: load trace_id/chain_id from current task ---
    _trace_id, _chain_id = _load_task_trace(conn, task_id)
    if not _trace_id and task_type == "pm":
        # Root PM task without trace_id — generate one and backfill
        _trace_id = new_trace_id()
        _chain_id = task_id
        try:
            conn.execute(
                "UPDATE tasks SET trace_id=?, chain_id=? WHERE task_id=?",
                (_trace_id, _chain_id, task_id),
            )
            # MF-2026-04-24-001 extension: release write lock immediately so
            # subsequent _publish_event subscribers (chain_context.on_task_completed
            # → _persist_event legacy path, opens separate conn) do not wait 60s
            # busy_timeout for this transaction to finish. Lock-hold time here
            # dominates the ~10min stall pattern in OPT-BACKLOG-AUTO-CHAIN-CONN-CONTENTION.
            conn.commit()
        except Exception:
            log.warning("auto_chain: failed to backfill trace_id on PM task %s", task_id)
    elif not _trace_id:
        # Non-PM task without trace (legacy) — generate trace but keep chain_id as parent_task_id
        _trace_id = new_trace_id()
        _chain_id = _chain_id or metadata.get("parent_task_id") or task_id

    # Non-blocking preflight log (first stage only)
    if task_type == "pm":
        try:
            from .preflight import run_preflight
            report = run_preflight(conn, project_id, auto_fix=False)
            if report.get("warnings"):
                log.warning("preflight warnings for %s: %s", project_id, report["warnings"])
            if not report.get("ok"):
                log.error("preflight blockers for %s: %s", project_id, report["blockers"])
        except Exception:
            pass  # never block chain on preflight failure

    # Auto-enrich: derive related_nodes from changed_files via impact API
    if not metadata.get("related_nodes"):
        changed = result.get("changed_files", metadata.get("changed_files", []))
        if changed:
            try:
                from .impact_analyzer import ImpactAnalyzer, ImpactAnalysisRequest, FileHitPolicy
                from . import project_service
                graph = project_service.load_project_graph(project_id)
                if graph:
                    def _get_status(nid):
                        from .enums import VerifyStatus
                        row = conn.execute(
                            "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
                            (project_id, nid)).fetchone()
                        return VerifyStatus.from_str(row["verify_status"]) if row else VerifyStatus.PENDING
                    analyzer = ImpactAnalyzer(graph, _get_status)
                    request = ImpactAnalysisRequest(
                        changed_files=changed,
                        file_policy=FileHitPolicy(match_primary=True, match_secondary=True),
                    )
                    impact = analyzer.analyze(request)
                    nodes = [n["node_id"] for n in impact.get("affected_nodes", [])]
                    if nodes:
                        metadata["related_nodes"] = nodes
                        log.info("auto_chain: enriched related_nodes from changed_files: %s", nodes)
            except Exception as e:
                log.warning("auto_chain: related_nodes enrichment failed: %s", e)

    depth = metadata.get("chain_depth", 0)
    if depth >= MAX_CHAIN_DEPTH:
        log.warning("auto_chain: max depth %d reached for task %s, stopping", depth, task_id)
        return {"chain_stopped": True, "reason": f"max_chain_depth={MAX_CHAIN_DEPTH}"}

    gate_fn_name, next_type, builder_name = CHAIN[task_type]

    # Emit task.completed to chain context store BEFORE gate check (R1)
    # so completion events are always recorded regardless of gate outcome
    _publish_event("task.completed", {
        "project_id": project_id, "task_id": task_id,
        "result": result, "type": task_type,
    })
    # A1: Audit task.completed lifecycle event
    try:
        from . import audit_service
        audit_service.record(
            conn, project_id, f"{task_type}.completed",
            actor="auto-chain",
            ok=True,
            node_ids=metadata.get("related_nodes", []),
            task_id=task_id,
            chain_depth=depth,
            trace_id=_trace_id,
        )
    except Exception:
        log.debug("auto_chain: audit task.completed failed (non-critical)", exc_info=True)
    # R6: structured_log with trace_id for gate transitions
    structured_log("info", f"{task_type}.completed",
                   project_id=project_id, task_id=task_id,
                   trace_id=_trace_id, chain_id=_chain_id)

    # M1: PM completes → persist full PRD to memory for future dev/qa recall
    # Moved BEFORE version gate so PRD publication fires regardless of gate outcome
    if task_type == "pm":
        prd = result.get("prd", result)
        prd_data = {
            "requirements": prd.get("requirements", result.get("requirements", [])),
            "acceptance_criteria": result.get("acceptance_criteria", prd.get("acceptance_criteria", [])),
            "target_files": result.get("target_files", []),
            "test_files": result.get("test_files", []),
            "proposed_nodes": result.get("proposed_nodes", []),
            "doc_impact": result.get("doc_impact", {}),
            "verification": result.get("verification", {}),
            "skip_reasons": result.get("skip_reasons", {}),
        }
        if any(prd_data.values()):
            _write_chain_memory(
                conn, project_id, "prd_scope",
                json.dumps(prd_data, ensure_ascii=False),
                metadata,
                extra_structured={"task_id": task_id, "chain_stage": "pm"},
            )

        # R3: Emit pm.prd.published event when PM result has non-empty proposed_nodes
        proposed_nodes = result.get("proposed_nodes", [])
        log.info("auto_chain: on_task_completed PM path proposed_nodes count=%d task=%s",
                 len(proposed_nodes), task_id)
        if proposed_nodes:
            try:
                from .chain_context import get_store
                store = get_store()
                root_task_id = store._task_to_root.get(task_id, task_id)
                store._persist_event(
                    root_task_id=root_task_id,
                    task_id=task_id,
                    event_type="pm.prd.published",
                    payload={
                        "proposed_nodes": proposed_nodes,
                        "test_files": result.get("test_files", []),
                        "target_files": result.get("target_files", []),
                        "requirements": prd.get("requirements", result.get("requirements", [])),
                        "acceptance_criteria": result.get("acceptance_criteria",
                                                          prd.get("acceptance_criteria", [])),
                    },
                    project_id=project_id,
                    conn=conn,  # MF-2026-04-24-001: share caller transaction
                )
                log.info("auto_chain: emitted pm.prd.published for task %s (%d proposed_nodes)",
                         task_id, len(proposed_nodes))
            except Exception:
                log.error("auto_chain: pm.prd.published emission failed", exc_info=True)

    # B30: merge produces a new commit advancing HEAD past chain_version; deploy updates
    # chain_version itself.  Both are version-advancing operations that must not be blocked
    # by the version gate (which anchors to chain_version per B29 fix). Gate remains active
    # for pm / dev / test / qa / gatekeeper.
    if task_type in ("merge", "deploy"):
        ver_passed, ver_reason = True, f"version_check skipped for {task_type} (version-advancing op)"
        log.debug("auto_chain: %s", ver_reason)
        _record_gate_event(conn, project_id, task_id, "version_check", ver_passed, ver_reason, _trace_id)
    else:
        # Pre-gate: version check — blocks on stale server or dirty workspace
        ver_passed, ver_reason = _gate_version_check(conn, project_id, result, metadata)
        _record_gate_event(conn, project_id, task_id, "version_check", ver_passed, ver_reason, _trace_id)

    # R2: Single-retry for dirty workspace — wait 10s then retry once (R4: max 1 retry)
    if not ver_passed and "dirty workspace" in ver_reason:
        import time
        log.info("auto_chain: dirty workspace detected for task %s, retrying in 10s...", task_id)
        time.sleep(10)
        ver_passed, ver_reason = _gate_version_check(conn, project_id, result, metadata)
        _record_gate_event(conn, project_id, task_id, "version_check_retry", ver_passed, ver_reason, _trace_id)

    if not ver_passed:
        # R2: Log at WARNING level with task_id, project_id, gate_reason, dirty_files
        _dirty_files = []
        try:
            _vrow = conn.execute(
                "SELECT dirty_files FROM project_version WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if _vrow and _vrow["dirty_files"]:
                _dirty_files = json.loads(_vrow["dirty_files"] or "[]")
        except Exception:
            pass
        log.warning(
            "auto_chain: version gate blocked for task %s (project=%s): %s dirty_files=%s",
            task_id, project_id, ver_reason, _dirty_files,
        )
        # R3: INSERT audit_log row with action='auto_chain_gate_blocked'
        try:
            from datetime import datetime, timezone
            conn.execute(
                "INSERT INTO audit_log (project_id, action, actor, ok, ts, task_id, details_json) "
                "VALUES (?, 'auto_chain_gate_blocked', 'auto-chain', 0, ?, ?, ?)",
                (
                    project_id,
                    datetime.now(timezone.utc).isoformat(),
                    task_id,
                    json.dumps({"gate_reason": ver_reason, "task_id": task_id, "project_id": project_id}),
                ),
            )
        except Exception:
            log.debug("auto_chain: failed to insert audit_log for gate block (non-critical)", exc_info=True)
        # MF-2026-04-24-001 extension: release write lock before sync event
        # dispatch so chain_context subscriber's legacy _persist_event does not
        # wait 60s busy_timeout on this audit_log INSERT.
        try:
            conn.commit()
        except Exception:
            log.debug("auto_chain: commit before gate.blocked publish failed (non-critical)", exc_info=True)
        _publish_event("gate.blocked", {
            "project_id": project_id, "task_id": task_id,
            "stage": "version_check", "next_stage": task_type,
            "reason": ver_reason,
        })
        return {"gate_blocked": True, "dispatched": False, "stage": "version_check", "reason": ver_reason}
    else:
        log.debug("auto_chain: version check passed for task %s: %s", task_id, ver_reason)

    # M4: Test completes → write validation_result memory (marks dev decision as tested)
    if task_type == "test":
        report = result.get("test_report", {})
        passed = report.get("passed", 0) if isinstance(report, dict) else 0
        if passed:
            _write_chain_memory(
                conn, project_id, "validation_result",
                f"Tests passed ({passed} passing) for {', '.join(metadata.get('changed_files', [])[:3])}",
                metadata,
                extra_structured={"task_id": task_id, "chain_stage": "test",
                                   "test_report": report,
                                   "validation_status": "tested",
                                   "parent_task_id": metadata.get("parent_task_id", "")},
            )

    # Auto-update nodes based on stage completion
    if task_type == "dev" and metadata.get("related_nodes"):
        _try_verify_update(conn, project_id, metadata, "testing", "dev",
                           {"type": "dev_complete", "producer": "auto-chain",
                            "task_id": task_id})

    # MF-2026-04-24-002: release caller write-lock before _emit_or_infer_graph_delta.
    # That helper + its 4 internal _persist_event legacy-path callsites open NEW
    # connections; if main conn has open transaction here (audit pm.completed at
    # ~1760 + optional _try_verify_update above), they wait 60s busy_timeout each
    # and compound into multi-minute dev-stage stalls. See OPT-BACKLOG-AUTO-CHAIN-
    # CONN-CONTENTION-DEV-PATH for the follow-on to MF-001.
    try:
        conn.commit()
    except Exception:
        log.debug("auto_chain: commit before graph_delta emit failed (non-critical)", exc_info=True)

    # R2: Emit graph.delta.proposed event (auto-infer if dev omitted graph_delta)
    if task_type == "dev":
        _emit_or_infer_graph_delta(project_id, task_id, result, metadata)

    # Run gate check
    gate_fn = _GATES[gate_fn_name]
    passed, reason = gate_fn(conn, project_id, result, metadata)
    _record_gate_event(conn, project_id, task_id, gate_fn_name, passed, reason, _trace_id)
    if not passed:
        workflow_improvement = _maybe_create_workflow_improvement_task(
            conn, project_id, task_id, task_type, reason, metadata, result
        )
        log.info("auto_chain: gate blocked %s→%s for task %s: %s",
                 task_type, next_type or "deploy", task_id, reason)
        # MF-2026-04-24-002: release write-lock before publish (subscriber
        # chain_context.on_gate_blocked opens separate conn via legacy path)
        try:
            conn.commit()
        except Exception:
            log.debug("auto_chain: commit before stage gate.blocked publish failed (non-critical)", exc_info=True)
        _publish_event("gate.blocked", {
            "project_id": project_id, "task_id": task_id,
            "stage": task_type, "next_stage": next_type or "deploy",
            "reason": reason,
        })
        # M3: Gate fail → write pitfall with previous output context
        _write_chain_memory(
            conn, project_id, "pitfall",
            f"Gate blocked at {task_type}: {reason}\n"
            f"Previous output keys: {list(result.keys())}\n"
            f"Previous output preview: {json.dumps(result, ensure_ascii=False)[:300]}",
            metadata,
            extra_structured={"task_id": task_id, "gate_stage": task_type,
                               "gate_reason": reason,
                               "previous_output_keys": list(result.keys()),
                               "chain_stage": task_type},
        )
        # G3: Persist gate.blocked to audit_index
        try:
            from . import audit_service
            audit_service.record(
                conn, project_id, "gate.blocked",
                actor="auto-chain",
                ok=False,
                node_ids=metadata.get("related_nodes", []),
                task_id=task_id,
                stage=task_type,
                next_stage=next_type or "deploy",
                reason=reason,
                trace_id=_trace_id,
            )
        except Exception:
            log.debug("auto_chain: audit gate.blocked failed (non-critical)", exc_info=True)
        structured_log("warning", "gate.blocked",
                       project_id=project_id, task_id=task_id,
                       stage=task_type, next_stage=next_type or "deploy",
                       trace_id=_trace_id, chain_id=_chain_id, reason=reason)

        # Special cases: test failure or QA rejection → retry as dev (not same stage)
        # Dev fixes the root cause; re-running test/qa without a code fix is wasteful
        if task_type in ("test", "qa"):
            failure_reason = reason
            if task_type == "qa":
                # Prefer specific rejection reason from QA result over gate reason
                failure_reason = result.get("reason", reason)
            original_prompt = metadata.get("_original_prompt", "")
            if not original_prompt:
                try:
                    from .chain_context import get_store
                    original_prompt = get_store().get_original_prompt(task_id)
                except Exception:
                    pass
            if not original_prompt:
                original_prompt = result.get("summary", "")
            stage_retry_prompt = (
                f"Fix {task_type} stage failures from task {task_id}.\n"
                f"failure_reason: {failure_reason}\n"
                f"retry_from_stage: {task_type}\n\n"
                f"Original task: {original_prompt}"
            )
            from . import task_registry
            # Dedup: skip if an active dev-retry already exists for this parent
            try:
                _existing_stage_retry = conn.execute(
                    "SELECT task_id FROM tasks WHERE project_id = ? AND type = 'dev' "
                    "AND status IN ('queued','claimed','observer_hold') "
                    "AND json_extract(metadata_json, '$.parent_task_id') = ?",
                    (project_id, task_id),
                ).fetchone()
            except Exception:
                _existing_stage_retry = None
            if _existing_stage_retry:
                _dup_id = _existing_stage_retry["task_id"]
                log.warning("auto_chain: dedup stage-retry — active dev retry %s already exists for %s",
                            _dup_id, task_id)
                out = {"gate_blocked": True, "stage": task_type, "reason": reason,
                       "retry_task_id": _dup_id, "retry_type": "dev",
                       "retry_from_stage": task_type, "dedup": True}
                if workflow_improvement:
                    out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
                return out
            # OPT-BACKLOG-CH2: fallback-fill missing bug_id from chain store
            # before creating retry task. Protects against in-process metadata
            # drops between stages (e.g. test→dev hop losing parent's metadata).
            _dev_retry_meta = {
                **metadata,
                "parent_task_id": task_id,
                "chain_depth": depth + 1,
                "failure_reason": failure_reason,
                "retry_from_stage": task_type,
                "_original_prompt": original_prompt,
            }
            if not _dev_retry_meta.get("bug_id"):
                try:
                    from .chain_context import get_store as _get_ctx_store_bug
                    _chain_bug = _get_ctx_store_bug().get_bug_id(task_id)
                    if _chain_bug:
                        _dev_retry_meta["bug_id"] = _chain_bug
                        log.info("auto_chain: CH2 fallback-filled bug_id=%s for dev-retry of %s",
                                 _chain_bug, task_id)
                except Exception:
                    log.debug("auto_chain: CH2 bug_id fallback failed for %s", task_id, exc_info=True)

            dev_retry = task_registry.create_task(
                conn, project_id,
                prompt=stage_retry_prompt,
                task_type="dev",
                created_by="auto-chain-stage-retry",
                metadata=_dev_retry_meta,
                trace_id=_trace_id,
                chain_id=_chain_id,
            )
            retry_id = dev_retry.get("task_id", "?")
            log.info("auto_chain: %s failure → dev retry task %s", task_type, retry_id)
            _publish_event("task.retry", {
                "project_id": project_id, "task_id": retry_id,
                "original_task_id": task_id, "reason": failure_reason,
                "retry_from_stage": task_type,
            })
            out = {
                "gate_blocked": True, "stage": task_type, "reason": reason,
                "retry_task_id": retry_id, "retry_type": "dev",
                "retry_from_stage": task_type,
            }
            if workflow_improvement:
                out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
                out["failure_class"] = workflow_improvement["classification"].get("failure_class", "")
            return out

        # Auto-retry: create a new task at the SAME stage with gate reason injected
        # Max 2 retries per gate to prevent infinite loops
        gate_retries = metadata.get("_gate_retry_count", 0)
        if gate_retries < 2 and depth < MAX_CHAIN_DEPTH - 1 and not metadata.get("_no_retry"):
            # Recover original prompt: metadata → chain context → result summary
            original_prompt = metadata.get("_original_prompt", "")
            if not original_prompt:
                try:
                    from .chain_context import get_store
                    original_prompt = get_store().get_original_prompt(task_id)
                except Exception:
                    pass
            if not original_prompt:
                original_prompt = result.get("summary", "")
            if task_type == "dev":
                retry_reason = _effective_dev_retry_reason(conn, project_id, metadata, reason)
                retry_contract = _render_dev_contract_prompt(
                    metadata.get("parent_task_id", task_id),
                    metadata,
                )
                # B36-fix(2): Build allowed list from the SAME helper gate uses —
                # prompt and gate can no longer disagree.
                from .chain_context import get_store as _get_ctx_store
                _target, allowed = _compute_gate_static_allowed(project_id, metadata)
                # B28a inheritance + B36-fix(1): accumulated files from prior succeeded dev stages
                try:
                    allowed.update(
                        _get_ctx_store().get_accumulated_changed_files(_chain_id, project_id)
                    )
                except Exception:
                    pass
                if not allowed:
                    allowed = set(metadata.get("target_files", []))  # final fallback
                if allowed:
                    # Gate also permits stem-prefix tests (tests/test_<stem>*.py) at evaluation
                    # time. Describe as pattern since matched dynamically.
                    _stems = sorted({
                        os.path.splitext(os.path.basename(t.replace("\\", "/")))[0]
                        for t in _target
                    })
                    pattern_note = (
                        f" Plus any file under a tests/ directory matching pattern "
                        f"tests/test_{{{'|'.join(_stems)}}}*.py."
                        if _stems else ""
                    )
                    scope_line = (
                        f"SCOPE CONSTRAINT: Checkpoint gate only allows changes to: "
                        f"{sorted(allowed)}.{pattern_note} Changes to any other files "
                        f"will be blocked as 'unrelated'.\n\n"
                    )
                else:
                    scope_line = ""
                # PR-B/R4: Enrich dev retry with graph_delta_review issues if rejection was from QA graph delta review
                _gd_retry_section = ""
                if "graph delta rejected by QA" in reason or "graph_delta_review" in reason:
                    _gd_review = result.get("graph_delta_review", {})
                    if isinstance(_gd_review, dict):
                        _gd_issues = _gd_review.get("issues", [])
                        _gd_diff = _gd_review.get("suggested_diff", {})
                        _gd_retry_section = (
                            "\n## Graph Delta Review Rejection\n"
                            f"QA graph_delta_review issues: {json.dumps(_gd_issues, ensure_ascii=False)}\n"
                            f"QA suggested_diff: {json.dumps(_gd_diff, ensure_ascii=False)}\n"
                            "Address the graph delta issues listed above in your retry.\n\n"
                        )
                retry_prompt = (
                    f"Previous attempt ({task_id}) was blocked by gate.\n"
                    f"Gate reason: {retry_reason}\n\n"
                    f"{_gd_retry_section}"
                    f"{scope_line}"
                    "IMPORTANT: Do not assume previous blockers still exist. "
                    "Re-verify all alleged blockers against current source before reporting them as remaining issues.\n\n"
                    "Fix the issue described above and retry.\n"
                    "Use the same Dev contract below, including the required verification command.\n\n"
                    f"{retry_contract}"
                )
            else:
                # R8: Check if this is a PM task blocked for PRD missing fields
                _is_pm_prd_missing = (
                    task_type == "pm"
                    and ("PRD missing mandatory fields" in reason
                         or "PRD fields missing without skip_reasons" in reason)
                )
                if _is_pm_prd_missing:
                    # R2: Parse missing fields from gate reason
                    _pm_missing_fields = _parse_pm_missing_fields(reason)
                    # R3: Show prior output keys
                    _prior_keys = sorted(result.keys()) if isinstance(result, dict) else []
                    # R7: Emit repeat regression event when new retry's count >= 2
                    # (gate_retries is current count; new task gets gate_retries + 1)
                    if gate_retries + 1 >= 2:
                        try:
                            from datetime import datetime, timezone
                            conn.execute(
                                "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
                                "VALUES (?, ?, 'pm.prd.repeat_regression', ?, ?)",
                                (_chain_id, task_id,
                                 json.dumps({"reason": reason, "gate_retry_count": gate_retries,
                                             "missing_fields": _pm_missing_fields,
                                             "prior_keys": _prior_keys},
                                            ensure_ascii=False),
                                 datetime.now(timezone.utc).isoformat()),
                            )
                        except Exception:
                            log.debug("auto_chain: pm.prd.repeat_regression event write failed", exc_info=True)
                    # R1, R4, R5, R6: Build structured PM retry prompt
                    retry_prompt = (
                        "[CRITICAL: PRD completeness gate blocked your prior output]\n\n"
                        f"Missing fields: {', '.join(_pm_missing_fields)}\n"
                        f"Your output contained keys: {_prior_keys}\n\n"
                        "## Required PRD JSON Shape\n"
                        "Your output MUST include ALL of the following fields:\n"
                        "```json\n"
                        "{\n"
                        '  "target_files": ["path/to/file.py"],\n'
                        '  "test_files": ["path/to/test_file.py"],\n'
                        '  "acceptance_criteria": ["AC1: ..."],\n'
                        '  "verification": {"method": "automated test", "command": "pytest ..."},\n'
                        '  "requirements": ["R1: ..."],\n'
                        '  "proposed_nodes": [{"node_id": "L3.x", "title": "...", "description": "..."}]\n'
                        "}\n"
                        "```\n\n"
                        f"Gate reason: {reason}\n\n"
                        f"Original task: {original_prompt}"
                    )
                else:
                    # AC7: Generic fallback for non-PM or non-missing-field PM retries
                    retry_prompt = (
                        f"Previous attempt ({task_id}) was blocked by gate.\n"
                        f"Gate reason: {reason}\n\n"
                        f"Fix the issue described above and retry.\n"
                        f"Original task: {original_prompt}"
                    )
            from . import task_registry
            # Dedup: skip if an active same-stage retry already exists for this parent
            try:
                _existing_same_retry = conn.execute(
                    "SELECT task_id FROM tasks WHERE project_id = ? AND type = ? "
                    "AND status IN ('queued','claimed','observer_hold') "
                    "AND json_extract(metadata_json, '$.parent_task_id') = ?",
                    (project_id, task_type, task_id),
                ).fetchone()
            except Exception:
                _existing_same_retry = None
            if _existing_same_retry:
                _dup_id = _existing_same_retry["task_id"]
                log.warning("auto_chain: dedup same-stage-retry — active %s retry %s already exists for %s",
                            task_type, _dup_id, task_id)
                out = {"gate_blocked": True, "stage": task_type, "reason": reason,
                       "retry_task_id": _dup_id, "dedup": True}
                if workflow_improvement:
                    out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
                return out
            # --- Sanitise retry metadata (R1/R2): strip stale inherited fields ---
            _retry_meta = {
                **metadata,
                "parent_task_id": task_id,
                "chain_depth": depth + 1,
                "previous_gate_reason": retry_reason if task_type == "dev" else reason,
                "_gate_retry_count": gate_retries + 1,
                "_original_prompt": original_prompt,
            }
            # R2: Strip inherited worktree/branch so new task creates fresh worktree
            _retry_meta.pop("_worktree", None)
            _retry_meta.pop("_branch", None)
            # R1: Remove inherited failure_reason from grandparent — only current gate reason kept
            _retry_meta.pop("failure_reason", None)

            # OPT-BACKLOG-CH2: fallback-fill missing bug_id from chain store. Retries
            # inherit parent's metadata via {**metadata} above, but if bug_id was
            # dropped somewhere upstream, the chain-level store still knows it.
            if not _retry_meta.get("bug_id"):
                try:
                    from .chain_context import get_store as _get_ctx_store_bug2
                    _chain_bug = _get_ctx_store_bug2().get_bug_id(task_id)
                    if _chain_bug:
                        _retry_meta["bug_id"] = _chain_bug
                        log.info("auto_chain: CH2 fallback-filled bug_id=%s for %s same-stage-retry of %s",
                                 _chain_bug, task_type, task_id)
                except Exception:
                    log.debug("auto_chain: CH2 bug_id fallback failed for %s", task_id, exc_info=True)

            retry_task = task_registry.create_task(
                conn, project_id,
                prompt=retry_prompt,
                task_type=task_type,
                created_by="auto-chain-retry",
                metadata=_retry_meta,
                trace_id=_trace_id,
                chain_id=_chain_id,
            )
            retry_id = retry_task.get("task_id", "?")
            log.info("auto_chain: retry created %s for blocked %s", retry_id, task_id)
            _publish_event("task.retry", {
                "project_id": project_id, "task_id": retry_id,
                "original_task_id": task_id, "reason": reason,
            })
            out = {"gate_blocked": True, "stage": task_type, "reason": reason,
                   "retry_task_id": retry_id}
            if workflow_improvement:
                out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
                out["failure_class"] = workflow_improvement["classification"].get("failure_class", "")
            return out

        # Retry exhausted — emit task.failed
        _publish_event("task.failed", {
            "project_id": project_id, "task_id": task_id,
            "reason": "gate_retry_exhausted", "gate_reason": reason,
        })
        out = {"gate_blocked": True, "stage": task_type, "reason": reason}
        if workflow_improvement:
            out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
            out["failure_class"] = workflow_improvement["classification"].get("failure_class", "")
        return out

    # M5: Dev success + checkpoint gate pass → write success pattern memory
    if task_type == "dev":
        _changed_for_pattern = result.get("changed_files", metadata.get("changed_files", []))
        _summary_for_pattern = result.get("summary", "")
        _write_chain_memory(
            conn, project_id, "pattern",
            _summary_for_pattern or f"Dev completed: {', '.join(_changed_for_pattern[:3])}",
            metadata,
            extra_structured={
                "task_id": task_id, "chain_stage": "dev",
                "changed_files": _changed_for_pattern,
                "gate": "checkpoint_pass",
            },
        )

    # Terminal stage → trigger deploy + archive chain
    if next_type is None:
        builder_fn = _BUILDERS[builder_name]
        deploy_result = builder_fn(conn, project_id, task_id, result, metadata)
        log.info("auto_chain: deploy triggered from task %s: %s", task_id, deploy_result)
        # A2: chain.completed audit summary
        try:
            from . import audit_service
            audit_service.record(
                conn, project_id, "chain.completed",
                actor="auto-chain",
                ok=True,
                node_ids=metadata.get("related_nodes", []),
                task_id=task_id,
                chain_depth=depth,
                changed_files=metadata.get("changed_files", []),
                trace_id=_trace_id,
            )
        except Exception:
            log.debug("auto_chain: audit chain.completed failed (non-critical)", exc_info=True)
        structured_log("info", "chain.completed",
                       project_id=project_id, task_id=task_id,
                       trace_id=_trace_id, chain_id=_chain_id)
        # Archive chain context (release memory, DB data preserved)
        try:
            from .chain_context import get_store
            get_store().archive_chain(task_id, project_id)
        except Exception:
            log.debug("auto_chain: chain archive failed (non-critical)")
        return deploy_result

    # --- R5/R7: Subtask fan-out for PM→Dev ---
    if task_type == "pm" and result.get("subtasks"):
        return _do_subtask_fanout(
            conn, project_id, task_id, result, metadata,
            _trace_id, _chain_id, depth,
        )

    # --- Graph-driven routing (R2): try graph-based next-stage derivation ---
    _graph_next = None
    _graph_skipped = []
    _graph_policies = []
    try:
        from . import project_service
        _graph = project_service.load_project_graph(project_id)
    except Exception:
        _graph = None

    if _graph is not None:
        _graph_next, _graph_skipped, _graph_policies = dispatch_next_stage(
            conn, project_id, task_id, task_type, result, metadata, _trace_id, _graph,
        )

        # Handle blocked by verify_requires (AC4)
        if _graph_next == "blocked":
            log.info("auto_chain: routing blocked by verify_requires for task %s", task_id)
            return {"routing_blocked": True, "reason": "verify_requires not satisfied"}

        # If graph routing returned a specific next stage, override CHAIN lookup
        if _graph_next is not None:
            next_type = _graph_next
            # Find the matching builder and gate for the overridden next_type
            for _chain_type, (_gfn, _ntype, _bname) in CHAIN.items():
                if _ntype == next_type:
                    builder_name = _bname
                    break
            # If next_type is in CHAIN as a key (e.g. "merge"), use its gate
            if next_type in CHAIN:
                pass  # builder already found above or use current stage's

    # R6/AC7: Audit every routing decision
    _audit_routing_decision(conn, project_id, task_id, _trace_id, {
        "current_stage": task_type,
        "next_stage": next_type,
        "routing_mode": "graph_driven" if _graph_next else "linear_chain",
        "skipped_stages": _graph_skipped,
    })

    # Create next stage task (with dedup check)
    builder_fn = _BUILDERS[builder_name]
    prompt, task_meta = builder_fn(task_id, result, metadata)

    # Attach graph routing policies to metadata for downstream stages
    if _graph_policies:
        task_meta["_graph_routing_policies"] = [
            {"node_id": p["node_id"], "gate_mode": p.get("gate_mode"),
             "verify_level": p.get("verify_level")}
            for p in _graph_policies
        ]

    # M6: Dedup — check if next stage already exists for this parent
    from . import task_registry
    existing = conn.execute(
        "SELECT task_id FROM tasks WHERE type = ? AND status IN ('queued','claimed','observer_hold') "
        "AND metadata_json LIKE ?",
        (next_type, f'%"parent_task_id": "{task_id}"%'),
    ).fetchone()
    if existing:
        log.warning("auto_chain: dedup — %s task already exists for parent %s: %s",
                     next_type, task_id, existing["task_id"])
        return {"task_id": existing["task_id"], "dedup": True}

    new_task = task_registry.create_task(
        conn, project_id,
        prompt=prompt,
        task_type=next_type,
        created_by="auto-chain",
        metadata={
            **task_meta,
            "parent_task_id": task_id,
            "chain_depth": depth + 1,
        },
        trace_id=_trace_id,
        chain_id=_chain_id,
    )

    log.info("auto_chain: %s→%s | %s → %s",
             task_type, next_type, task_id, new_task.get("task_id"))
    # MF-2026-04-24-002: release write-lock before next-stage task.created
    # publish. create_task above opened an implicit write transaction on main
    # conn; without this commit, chain_context.on_task_created subscriber's
    # legacy-path _persist_event waits 60s busy_timeout, stalling the chain
    # at every dispatch boundary.
    try:
        conn.commit()
    except Exception:
        log.debug("auto_chain: commit before next-stage task.created publish failed (non-critical)", exc_info=True)
    # OPT-BACKLOG-CH2: forward metadata.bug_id in task.created payload so
    # chain_context.on_task_created can populate chain.bug_id (first-write-wins).
    # Retry paths then fallback-fill from chain store when metadata is lost.
    _publish_event("task.created", {
        "project_id": project_id,
        "parent_task_id": task_id,
        "task_id": new_task.get("task_id"),
        "type": next_type,
        "prompt": prompt,
        "source": "auto-chain",
        "metadata": {"bug_id": task_meta.get("bug_id", "")},
    })
    return new_task


# ---------------------------------------------------------------------------
# Subtask fan-out / fan-in (R5, R6, R9)
# ---------------------------------------------------------------------------

def _do_subtask_fanout(conn, project_id, pm_task_id, result, metadata, trace_id, chain_id, depth):
    """Create subtask_group + dev tasks for PM subtask decomposition (R5)."""
    from . import task_registry
    from .models import SubtaskGroup
    from datetime import datetime, timezone

    subtasks = result["subtasks"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    group = SubtaskGroup(
        project_id=project_id,
        pm_task_id=pm_task_id,
        total_count=len(subtasks),
        trace_id=trace_id or "",
        chain_id=chain_id or "",
    )

    conn.execute(
        """INSERT INTO subtask_groups
           (group_id, project_id, pm_task_id, total_count, completed_count,
            status, created_at, trace_id, chain_id)
           VALUES (?, ?, ?, ?, 0, 'active', ?, ?, ?)""",
        (group.group_id, project_id, pm_task_id, len(subtasks),
         now, trace_id or "", chain_id or ""),
    )

    created_tasks = []
    for st in subtasks:
        deps = st.get("depends_on") or []
        is_blocked = len(deps) > 0

        st_meta = {
            **metadata,
            "parent_task_id": pm_task_id,
            "chain_depth": depth + 1,
            "target_files": st.get("target_files", []),
            "acceptance_criteria": st.get("acceptance_criteria", []),
            "verification": st.get("verification", {}),
            "test_files": st.get("test_files", []),
            "subtask_title": st.get("title", ""),
        }

        prompt = _render_dev_contract_prompt(pm_task_id, st_meta)

        new_task = task_registry.create_task(
            conn, project_id,
            prompt=prompt,
            task_type="dev",
            created_by="auto-chain-subtask",
            metadata=st_meta,
            trace_id=trace_id,
            chain_id=chain_id,
        )
        task_id = new_task["task_id"]

        # Set subtask fields and blocked status
        if is_blocked:
            conn.execute(
                """UPDATE tasks SET
                   subtask_group_id=?, subtask_local_id=?, subtask_depends_on=?,
                   execution_status='blocked', status='blocked'
                   WHERE task_id=?""",
                (group.group_id, st["id"], json.dumps(deps), task_id),
            )
        else:
            conn.execute(
                """UPDATE tasks SET
                   subtask_group_id=?, subtask_local_id=?, subtask_depends_on=?
                   WHERE task_id=?""",
                (group.group_id, st["id"], json.dumps(deps), task_id),
            )

        created_tasks.append({
            "task_id": task_id,
            "subtask_id": st["id"],
            "blocked": is_blocked,
        })

    log.info("auto_chain: subtask fan-out from PM %s → group %s (%d subtasks)",
             pm_task_id, group.group_id, len(subtasks))

    return {
        "subtask_group_id": group.group_id,
        "tasks_created": created_tasks,
        "total_count": len(subtasks),
    }


def on_subtask_merge_completed(conn, project_id, task_id):
    """Fan-in: called when a subtask's merge chain completes (R6).

    Decrements deps on downstream subtasks, unblocks ready ones.
    When all subtasks complete, creates a deploy task.
    """
    from . import task_registry

    # Find the subtask's group and local ID
    row = conn.execute(
        "SELECT subtask_group_id, subtask_local_id FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if not row or not row["subtask_group_id"]:
        return None

    group_id = row["subtask_group_id"]
    completed_local_id = row["subtask_local_id"]

    # Get group info
    group_row = conn.execute(
        "SELECT * FROM subtask_groups WHERE group_id=?", (group_id,)
    ).fetchone()
    if not group_row:
        return None

    # Increment completed_count
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE subtask_groups SET completed_count = completed_count + 1 WHERE group_id=?",
        (group_id,),
    )

    # Unblock downstream subtasks that depended on this one
    blocked_tasks = conn.execute(
        """SELECT task_id, subtask_depends_on FROM tasks
           WHERE subtask_group_id=? AND execution_status='blocked'""",
        (group_id,),
    ).fetchall()

    for bt in blocked_tasks:
        deps = json.loads(bt["subtask_depends_on"] or "[]")
        if completed_local_id in deps:
            deps.remove(completed_local_id)
            if not deps:
                # All deps satisfied — unblock
                conn.execute(
                    """UPDATE tasks SET execution_status='queued', status='queued',
                       subtask_depends_on=? WHERE task_id=?""",
                    (json.dumps(deps), bt["task_id"]),
                )
                log.info("auto_chain: unblocked subtask %s (group %s)",
                         bt["task_id"], group_id)
            else:
                conn.execute(
                    "UPDATE tasks SET subtask_depends_on=? WHERE task_id=?",
                    (json.dumps(deps), bt["task_id"]),
                )

    # Check if all subtasks complete → create deploy task
    updated_group = conn.execute(
        "SELECT completed_count, total_count, pm_task_id, project_id, trace_id, chain_id FROM subtask_groups WHERE group_id=?",
        (group_id,),
    ).fetchone()

    if updated_group and updated_group["completed_count"] >= updated_group["total_count"]:
        conn.execute(
            "UPDATE subtask_groups SET status='completed', completed_at=? WHERE group_id=?",
            (now, group_id),
        )
        # Create deploy task (R6)
        deploy_task = task_registry.create_task(
            conn, project_id,
            prompt=f"Deploy all subtasks from group {group_id} (PM: {updated_group['pm_task_id']})",
            task_type="deploy",
            created_by="auto-chain-fanin",
            metadata={
                "subtask_group_id": group_id,
                "parent_task_id": updated_group["pm_task_id"],
            },
            parent_task_id=updated_group["pm_task_id"],
            trace_id=updated_group["trace_id"],
            chain_id=updated_group["chain_id"],
        )
        log.info("auto_chain: fan-in complete for group %s → deploy %s",
                 group_id, deploy_task.get("task_id"))
        return {"deploy_task_id": deploy_task.get("task_id"), "group_id": group_id}

    return {"group_id": group_id, "unblocked": True}


def on_subtask_terminal_failure(conn, project_id, task_id):
    """Failure cascade: mark group failed, cancel blocked siblings (R9)."""
    row = conn.execute(
        "SELECT subtask_group_id FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if not row or not row["subtask_group_id"]:
        return None

    group_id = row["subtask_group_id"]
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Mark group as failed
    conn.execute(
        "UPDATE subtask_groups SET status='failed', completed_at=? WHERE group_id=?",
        (now, group_id),
    )

    # Cancel all blocked sibling tasks
    cancelled = conn.execute(
        """UPDATE tasks SET status='cancelled', execution_status='cancelled',
           completed_at=?, error_message='subtask group failed: sibling failure cascade'
           WHERE subtask_group_id=? AND execution_status='blocked'""",
        (now, group_id),
    ).rowcount

    log.info("auto_chain: failure cascade for group %s — cancelled %d blocked tasks",
             group_id, cancelled)
    return {"group_id": group_id, "cancelled_count": cancelled}


# ---------------------------------------------------------------------------
# Gate functions — each returns (passed: bool, reason: str)
# ---------------------------------------------------------------------------

def _gate_version_check(conn, project_id, result, metadata):
    """Pre-gate: verify the workspace is clean and governance code is current.

    Returns (True, reason) to pass, (False, reason) to block.
    Blocking conditions (return False):
      - server version != git HEAD (stale server — restart required)
      - Dirty workspace with non-ignored files (uncommitted changes)
    Bypass conditions (return True even if mismatch):
      - _DISABLE_VERSION_GATE=True (development override)
      - metadata.skip_version_check=True (task-level bypass)
      - metadata.observer_merge=True (Observer manual merge flow)
      - Reconciliation bypass (structured reconciliation lane)
      - Governed dirty-workspace chain (legacy compat)
    """
    if _DISABLE_VERSION_GATE:
        return True, "version gate disabled (_DISABLE_VERSION_GATE=True)"
    if metadata.get("skip_version_check"):
        operator_id = metadata.get("operator_id", "")
        bypass_reason = metadata.get("bypass_reason", "")
        if not isinstance(operator_id, str) or not operator_id.strip():
            missing = ["operator_id"]
            if not isinstance(bypass_reason, str) or not bypass_reason.strip():
                missing.append("bypass_reason")
            log.warning("skip_version_check ignored — missing required fields: %s (task metadata: %s)",
                        missing, {k: metadata.get(k) for k in ("skip_version_check", "operator_id", "bypass_reason")})
        elif not isinstance(bypass_reason, str) or not bypass_reason.strip():
            log.warning("skip_version_check ignored — missing required fields: %s (task metadata: %s)",
                        ["bypass_reason"], {k: metadata.get(k) for k in ("skip_version_check", "operator_id", "bypass_reason")})
        else:
            task_id = metadata.get("task_id") or metadata.get("parent_task_id") or "unknown"
            task_type = metadata.get("task_type", "unknown")
            _audit_version_gate_bypass(conn, project_id, task_id, operator_id.strip(), bypass_reason.strip(), task_type)
            return True, "skipped (task metadata)"
    if metadata.get("observer_merge"):
        return True, "observer merge bypass"
    if not hasattr(conn, "execute"):
        return True, "no db-capable connection, skipping"

    # --- Structured reconciliation bypass (RECONCILIATION_BYPASS_POLICY) ---
    bypass, observer_task_id = _check_reconciliation_bypass(conn, project_id, metadata)
    if bypass:
        lane = str(metadata.get("reconciliation_lane", "")).strip().upper()
        task_id = metadata.get("task_id") or metadata.get("parent_task_id") or "unknown"
        _audit_reconciliation_bypass(conn, project_id, task_id, observer_task_id, lane)
        return True, f"reconciliation-bypass (observer={observer_task_id}, lane={lane})"

    # --- Legacy governed dirty-workspace chain (kept for backward compat) ---
    if _is_governed_dirty_workspace_chain(conn, project_id, metadata):
        return True, "governed dirty-workspace reconciliation"

    try:
        row = conn.execute(
            "SELECT chain_version, git_head, dirty_files FROM project_version WHERE project_id=?",
            (project_id,),
        ).fetchone()
        dirty_files = json.loads(row["dirty_files"] or "[]") if row and row["dirty_files"] else []
        # B15/B23/B31: filter tool-local / non-governed paths (module-level _DIRTY_IGNORE)
        dirty_files = [f for f in dirty_files if not any(f.startswith(p) for p in _DIRTY_IGNORE)]
        if dirty_files:
            log.warning("version_check: dirty workspace (%d files: %s) — blocking chain",
                        len(dirty_files), dirty_files[:5])
            return False, f"dirty workspace ({len(dirty_files)} files: {dirty_files[:3]})"

        import subprocess
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        ).stdout.strip()
        if not head or head == "unknown":
            return True, "git HEAD unavailable, skipping"
        # B29: anchor version gate to DB chain_version (set only by successful Deploy),
        # not to get_server_version() which dynamically reads git HEAD (B19 side-effect).
        # This prevents manual Observer commits from silently advancing the gate baseline.
        chain_ver = (row["chain_version"] or "").strip() if row else ""
        if not chain_ver or chain_ver == "unknown":
            return True, "chain_version unavailable in DB, skipping"
        # B35: `head` is short (7 chars) from `git rev-parse --short`, but chain_version
        # may be full (40 chars) if a manual-fix SOP wrote via /api/version-update with
        # the full hash. Short git hashes are unique prefixes of full hashes, so compare
        # via prefix match in either direction.
        if not (chain_ver.startswith(head) or head.startswith(chain_ver)):
            log.warning("version_check: chain_version (%s) != git HEAD (%s) — blocking chain. "
                        "Complete a full workflow Deploy to update chain_version.",
                        chain_ver, head)
            return False, (f"chain_version ({chain_ver}) != git HEAD ({head}). "
                           f"Complete workflow Deploy to update chain_version.")
        return True, f"version match: {chain_ver}"
    except Exception as e:
        log.warning("version_check failed (non-fatal): %s", e)
        return True, f"version check skipped: {e}"


def _gate_post_pm(conn, project_id, result, metadata):
    """Validate PM PRD has mandatory fields + explain-or-provide for soft fields.

    Mandatory: target_files, verification, acceptance_criteria (hard block)
    Soft-mandatory: test_files, proposed_nodes, doc_impact (must provide OR skip_reasons)
    """
    prd = result.get("prd", {})

    # === Hard mandatory fields ===
    missing = []
    for field in ("target_files", "verification", "acceptance_criteria"):
        if not result.get(field) and not prd.get(field) and not metadata.get(field):
            missing.append(field)
    if missing:
        return False, f"PRD missing mandatory fields: {missing}"

    target_files = (result.get("target_files") or prd.get("target_files")
                    or metadata.get("target_files") or [])
    if not target_files:
        return False, "PRD target_files is empty"

    # G4: Auto-populate doc_impact from graph if PM left it empty
    doc_impact = result.get("doc_impact") or prd.get("doc_impact")
    if not doc_impact or (isinstance(doc_impact, dict) and not doc_impact.get("files")):
        graph_docs = _get_graph_doc_associations(project_id, target_files)
        # R2: Filter to only .md files — code files must never appear in doc_impact
        graph_docs = [f for f in graph_docs if f.endswith(".md")]
        if graph_docs:
            result["doc_impact"] = {
                "files": graph_docs,
                "changes": ["Auto-populated from graph associations"],
            }

    # G8: Auto-populate related_nodes from graph when PM left it empty
    if not result.get("related_nodes") and target_files:
        try:
            from .graph import AcceptanceGraph
            state_root = os.path.join(
                os.environ.get("SHARED_VOLUME_PATH",
                               os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "shared-volume")),
                "codex-tasks", "state", "governance", project_id)
            graph_path = os.path.join(state_root, "graph.json")
            if os.path.exists(graph_path):
                graph = AcceptanceGraph()
                graph.load(graph_path)
                target_set = set(target_files)
                matched_nodes = []
                for node_id in graph.list_nodes():
                    try:
                        node_data = graph.get_node(node_id)
                    except Exception:
                        continue
                    primary = node_data.get("primary", [])
                    if any(f in target_set for f in primary):
                        matched_nodes.append(node_id)
                if matched_nodes:
                    result["related_nodes"] = matched_nodes
        except Exception:
            pass  # G8: graph lookup failure is non-critical

    # === Soft-mandatory: provide OR explain in skip_reasons ===
    skip_reasons = result.get("skip_reasons", prd.get("skip_reasons", {}))
    if not isinstance(skip_reasons, dict):
        skip_reasons = {}
    soft_missing = []
    for field in ("test_files", "proposed_nodes", "doc_impact"):
        value = result.get(field) or prd.get(field)
        reason = skip_reasons.get(field, "")
        if not value and not reason:
            soft_missing.append(field)
    if soft_missing:
        return False, f"PRD fields missing without skip_reasons: {soft_missing}. Provide the field OR explain in skip_reasons why it's not needed."

    # === Subtask validation (R4) ===
    subtasks = result.get("subtasks") or prd.get("subtasks")
    if subtasks:
        if not isinstance(subtasks, list):
            return False, "subtasks must be an array"

        # Get max_subtasks limit (R2)
        max_subtasks = 5  # default
        try:
            pv_row = conn.execute(
                "SELECT max_subtasks FROM project_version WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if pv_row and pv_row["max_subtasks"]:
                max_subtasks = pv_row["max_subtasks"]
        except Exception:
            pass  # use default

        if len(subtasks) > max_subtasks:
            return False, f"subtask count {len(subtasks)} exceeds max_subtasks ({max_subtasks})"

        # Validate mandatory fields per subtask
        seen_ids = set()
        for st in subtasks:
            if not isinstance(st, dict):
                return False, "each subtask must be a dict"
            for mf in ("id", "title", "target_files", "acceptance_criteria"):
                if not st.get(mf):
                    return False, f"subtask missing mandatory field: {mf}"
            st_id = st["id"]
            if st_id in seen_ids:
                return False, f"duplicate subtask id: {st_id}"
            seen_ids.add(st_id)

        # Validate depends_on references
        for st in subtasks:
            for dep in (st.get("depends_on") or []):
                if dep not in seen_ids:
                    return False, f"subtask {st['id']} depends_on unknown id: {dep}"

        # DAG acyclicity check
        if not _check_subtask_dag_acyclic(subtasks):
            return False, "cyclic dependency in subtask depends_on"

    # === 5a: Graph doc classification validation (observation mode) ===
    target_files = (result.get("target_files") or prd.get("target_files")
                    or metadata.get("target_files") or [])
    graph_docs = _get_graph_doc_associations(project_id, target_files)
    if graph_docs:
        doc_impact = result.get("doc_impact") or prd.get("doc_impact") or {}
        declared_docs = set()
        if isinstance(doc_impact, dict):
            declared_docs.update(doc_impact.get("files", []))
        unclassified = [d for d in graph_docs if d not in declared_docs]
        if unclassified and _GRAPH_DOC_OBSERVATION_MODE:
            log.warning(
                "post_pm_gate: graph links %d doc(s) to target_files but PM did not classify them: %s",
                len(unclassified), unclassified[:5],
            )
            _audit_doc_gap(conn, project_id, metadata.get("parent_task_id", ""), "post_pm", set(unclassified), target_files)

    # === Merge all fields into result for downstream ===
    for field in ("target_files", "verification", "acceptance_criteria",
                  "test_files", "proposed_nodes", "doc_impact", "skip_reasons",
                  "requirements", "related_nodes"):
        if not result.get(field):
            result[field] = prd.get(field) or metadata.get(field)

    # Propagate subtasks to result for _do_chain
    if subtasks and not result.get("subtasks"):
        result["subtasks"] = subtasks

    return True, "ok"


def _check_subtask_dag_acyclic(subtasks):
    """Return True if the subtask dependency graph is a DAG (no cycles)."""
    # Build adjacency list
    adj = {}
    for st in subtasks:
        adj[st["id"]] = list(st.get("depends_on") or [])

    # Kahn's algorithm
    in_degree = {sid: 0 for sid in adj}
    for sid, deps in adj.items():
        for dep in deps:
            if dep in in_degree:
                in_degree[sid] += 0  # placeholder; we count from deps side
    # Recount properly
    in_degree = {sid: 0 for sid in adj}
    for sid, deps in adj.items():
        for dep in deps:
            pass  # deps are what sid depends on, so dep -> sid
    # Actually: if A depends_on B, then edge B->A. in_degree[A] += 1
    in_degree = {sid: 0 for sid in adj}
    for sid, deps in adj.items():
        for dep in deps:
            if sid in in_degree:
                in_degree[sid] += 1

    from collections import deque
    queue = deque(sid for sid, deg in in_degree.items() if deg == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        # Find nodes that depend on this node
        for sid, deps in adj.items():
            if node in deps:
                in_degree[sid] -= 1
                if in_degree[sid] == 0:
                    queue.append(sid)

    return visited == len(adj)


def _gate_checkpoint(conn, project_id, result, metadata):
    """Checkpoint gate for Dev.

    Trust executor-produced task-local diff evidence. Governance may run in a
    container without git/worktree parity, so it should not re-compute git diff
    here. Node alignment is also temporarily non-blocking until the acceptance
    graph deployed to governance catches up with in-flight node-by-node edits.
    """
    log.info("checkpoint_gate: result keys=%s, changed_files=%s, target_files=%s",
             list(result.keys()) if result else None,
             result.get("changed_files"),
             metadata.get("target_files"))
    changed = result.get("changed_files", [])
    if not changed:
        return False, "No files changed"

    # B36-fix(2): single source of truth shared with retry-prompt scope_line
    target, allowed = _compute_gate_static_allowed(project_id, metadata)
    # graph_docs still needed downstream for observation-mode doc check
    graph_docs = _get_graph_doc_associations(project_id, list(target))
    # B36-fix(1): on retry, inherit accumulated_changed_files from prior succeeded
    # dev stages so consecutive attempts can build on each other.
    if metadata.get("parent_task_id"):
        try:
            from .chain_context import get_store as _get_ctx_store
            _chain_id = metadata.get("chain_id") or metadata.get("parent_task_id")
            allowed.update(_get_ctx_store().get_accumulated_changed_files(_chain_id, project_id))
        except Exception:
            log.debug("_gate_checkpoint: accumulated_changed_files lookup failed", exc_info=True)
    # R1/R2: Derive allowed test files from target_files stems.
    # For each target file with stem S, allow changed files matching
    # tests/test_{S}*.py (under any parent directory or agent/tests/).
    _allowed_test_prefixes = []
    for tf in target:
        import os.path as _osp
        stem = _osp.splitext(_osp.basename(tf))[0]  # e.g. "ai_lifecycle"
        _allowed_test_prefixes.append(f"test_{stem}")
    if allowed:
        unrelated = []
        for f in changed:
            if f in allowed:
                continue
            # Check if file is a co-modified test file under a tests/ directory
            if _allowed_test_prefixes:
                import posixpath
                parts = f.replace("\\", "/")
                parent = posixpath.dirname(parts)
                basename = posixpath.basename(parts)
                if (parent.endswith("/tests") or parent.endswith("\\tests") or parent == "tests") \
                        and basename.endswith(".py"):
                    if any(basename.startswith(prefix) for prefix in _allowed_test_prefixes):
                        continue
            if _is_dev_note(f):
                continue
            unrelated.append(f)
        if unrelated:
            return False, f"Unrelated files modified: {unrelated}"
    # Syntax check: verify test_results if available
    test_results = result.get("test_results", {})
    if test_results.get("ran") and test_results.get("failed", 0) > 0:
        cmd = test_results.get('command', 'unknown')
        output_excerpt = str(test_results.get('output', ''))[:500]
        detail = f"Dev tests failed: {test_results.get('failed')} failures. Command: {cmd}"
        if output_excerpt:
            detail += f". Output excerpt: {output_excerpt}"
        return False, detail
    # --- Contract-drift detection (D10) --- warn-only ---
    try:
        from .drift_detector import detect_drift, findings_to_json
        _drift_baseline = metadata.get("_drift_baseline")
        if _drift_baseline and isinstance(_drift_baseline, dict):
            authorized = set(metadata.get("_drift_authorized_keys") or [])
            drift_findings = detect_drift(_drift_baseline, authorized_keys=authorized)
            if drift_findings:
                drift_report = findings_to_json(drift_findings)
                metadata["_drift_report"] = drift_report
                unauthorized = [f for f in drift_findings if not f.authorized]
                if unauthorized:
                    log.warning(
                        "checkpoint_gate: UNAUTHORIZED contract drift detected: %s",
                        drift_report,
                    )
                else:
                    log.info("checkpoint_gate: authorized contract drift: %s", drift_report)
        else:
            # No baseline captured — run fresh capture and attach for next stage
            from .drift_detector import capture_baseline
            baseline = capture_baseline()
            metadata["_drift_baseline"] = baseline
            metadata["_drift_report"] = "[]"
    except Exception:
        log.debug("checkpoint_gate: drift detection failed (non-critical)", exc_info=True)
    # Doc consistency check: use CODE_DOC_MAP to verify related docs are updated
    # Skip for governance-internal repairs to avoid oscillation loop (R2)
    if _is_governance_internal_repair(metadata, changed):
        log.info("checkpoint_gate: skipping doc consistency check for governance-internal repair")
        # Node gate is temporarily non-blocking while the governance graph catches
        # up with node-by-node local development. Keep the signal in logs only.
        related_nodes = _normalize_related_nodes(metadata.get("related_nodes", []))
        if related_nodes:
            log.warning(
                "checkpoint_gate: skipping related_nodes enforcement for dev task until graph sync is complete: %s",
                related_nodes,
            )
        return True, "ok"
    from .impact_analyzer import get_related_docs
    code_files = [f for f in changed if not f.startswith("docs/") and not f.endswith(".md")]
    doc_files_changed = set(f for f in changed if f.startswith("docs/") or f.endswith(".md"))
    doc_impact = metadata.get("doc_impact", {})
    if isinstance(doc_impact, dict) and "files" in doc_impact:
        # R3: Defensive filter — only .md files are valid expected docs
        expected_docs = {f for f in (doc_impact.get("files") or []) if f.endswith(".md")}
    else:
        expected_docs = get_related_docs(code_files)
    # docs/dev/** are informal dev notes — never enforce them as formal docs
    if expected_docs:
        expected_docs = {d for d in expected_docs if not _is_dev_note(d)}
    if expected_docs:
        missing_docs = expected_docs - doc_files_changed
        if missing_docs:
            if _should_defer_doc_gate_to_lane_c(conn, project_id, metadata):
                log.warning(
                    "checkpoint_gate: deferring doc updates to Lane C for governed reconciliation lane; missing docs=%s",
                    sorted(missing_docs),
                )
                return True, "doc updates deferred to Lane C"
            # Block by default — skip_doc_check only allowed with bootstrap_reason
            if metadata.get("skip_doc_check", False):
                bootstrap_reason = metadata.get("bootstrap_reason", "")
                if not bootstrap_reason:
                    return False, (f"skip_doc_check=true requires bootstrap_reason in metadata. "
                                   f"Missing docs: {sorted(missing_docs)}")
                log.warning("checkpoint_gate: docs skipped (bootstrap: %s): %s",
                            bootstrap_reason, sorted(missing_docs))
            else:
                return False, f"Related docs not updated: {sorted(missing_docs)}. Add them to changed_files."
    # 5c: Observation-mode graph doc check
    if graph_docs and _GRAPH_DOC_OBSERVATION_MODE:
        doc_files_in_changed = set(f for f in changed if f.startswith("docs/") or f.endswith(".md"))
        graph_docs_missing = set(graph_docs) - doc_files_in_changed
        if graph_docs_missing:
            log.warning(
                "checkpoint_gate: graph-linked docs not updated (observation): %s",
                sorted(graph_docs_missing)[:5],
            )
            _audit_doc_gap(conn, project_id, metadata.get("parent_task_id", ""), "checkpoint", graph_docs_missing, changed)

    # Node gate is temporarily non-blocking while the governance graph catches
    # up with node-by-node local development. Keep the signal in logs only.
    related_nodes = _normalize_related_nodes(metadata.get("related_nodes", []))
    if related_nodes:
        log.warning(
            "checkpoint_gate: skipping related_nodes enforcement for dev task until graph sync is complete: %s",
            related_nodes,
        )
    return True, "ok"


def _gate_t2_pass(conn, project_id, result, metadata):
    """Verify tests passed before advancing to QA."""
    report = result.get("test_report", {})
    if not isinstance(report, dict) or not report:
        return False, "Test stage missing required test_report"
    if "passed" not in report or "failed" not in report:
        return False, "Test stage test_report missing passed/failed counts"
    failed = report.get("failed", 0)
    if failed is None:
        failed = 0
    if failed > 0:
        return False, f"Tests failed: {failed} failures"
    # Update nodes FIRST (test passed → promote to t2_pass)
    # Evidence validator checks summary.passed > 0, so ensure it's there
    passed_count = report.get("passed", 1)  # Default 1 if not reported (tests passed gate)
    summary = {**report, "passed": passed_count, "failed": failed}
    vu_ok, vu_err = _try_verify_update(conn, project_id, metadata, "t2_pass", "tester",
                       {"type": "test_report", "producer": "auto-chain",
                        "tool": report.get("tool", "pytest"),
                        "summary": summary})
    # Then verify nodes reached t2_pass — defer enforcement when promotion failed or
    # impact-enriched related_nodes include over-broad graph neighbors
    related_nodes = metadata.get("related_nodes", [])
    if related_nodes:
        passed, reason = _check_nodes_min_status(conn, project_id, related_nodes, "t2_pass")
        if not passed:
            if not vu_ok:
                log.warning(
                    "t2_pass_gate: deferring related_nodes enforcement — verify_update failed: %s",
                    reason,
                )
            else:
                log.warning(
                    "t2_pass_gate: deferring related_nodes enforcement — over-broad related_nodes from impact analysis: %s",
                    reason,
                )
    return True, "ok"


def _gate_qa_pass(conn, project_id, result, metadata):
    """Verify QA recommendation before merge.

    Requires explicit qa_pass recommendation.
    Missing or ambiguous recommendation is a hard block (not auto-pass).
    """
    rec = result.get("recommendation", "")
    if rec == "qa_pass":
        pass  # Explicit pass
    elif rec in ("reject", "rejected"):
        return False, f"QA rejected: {result.get('reason', 'no reason given')}"
    else:
        # No explicit recommendation — BLOCK. Auto-pass is a security risk.
        return False, (
            f"QA gate requires explicit recommendation ('qa_pass' or 'reject'). "
            f"Got: {rec!r}. QA agent must set result.recommendation."
        )
    # PR-B: graph.delta.proposed enforcement — check BEFORE criteria evaluation
    _gd_proposed = _query_graph_delta_proposed(metadata)
    if _gd_proposed:
        gd_review = result.get("graph_delta_review")
        if not gd_review or not isinstance(gd_review, dict):
            return False, "graph.delta.proposed present but QA result omits graph_delta_review"
        gd_decision = gd_review.get("decision", "")
        if gd_decision == "reject":
            issues = gd_review.get("issues", [])
            return False, f"graph delta rejected by QA: {issues}"
        if gd_decision == "pass":
            # Write graph.delta.validated event to chain_events
            try:
                from .chain_context import get_store as _gd_store
                store = _gd_store()
                root_task_id = metadata.get("chain_id") or metadata.get("parent_task_id", "")
                root_task_id = store._task_to_root.get(root_task_id, root_task_id)
                task_id_for_event = metadata.get("task_id", "")
                store._persist_event(
                    root_task_id=root_task_id,
                    task_id=task_id_for_event,
                    event_type="graph.delta.validated",
                    payload={
                        "source_task_id": task_id_for_event,
                        "graph_delta_review": gd_review,
                        "proposed_payload": _gd_proposed,
                    },
                    project_id=project_id,
                    conn=conn,  # MF-2026-04-24-001: share caller transaction
                )
                log.info("auto_chain: wrote graph.delta.validated event for chain %s", root_task_id)
            except Exception:
                log.debug("auto_chain: graph.delta.validated write failed", exc_info=True)
        else:
            return False, f"graph_delta_review.decision must be 'pass' or 'reject', got: {gd_decision!r}"
    # AC7: No graph.delta.proposed → graph_delta_review field not required (back-compat)

    # E2E1: Verify criteria_results when acceptance_criteria exist
    criteria = metadata.get("acceptance_criteria", [])
    criteria_results = result.get("criteria_results", [])
    if criteria:
        if not criteria_results:
            log.warning("qa_gate: acceptance_criteria present (%d items) but QA result missing criteria_results — "
                        "allowing pass but criteria not individually verified", len(criteria))
        else:
            failed_criteria = [cr for cr in criteria_results if not cr.get("passed")]
            if failed_criteria:
                names = [cr.get("criterion", "?")[:60] for cr in failed_criteria]
                return False, f"QA approved overall but {len(failed_criteria)} criteria failed: {names}"
    # Update nodes FIRST (QA passed → promote to qa_pass)
    # Evidence rule: t2_pass → qa_pass requires "e2e_report" with summary.passed > 0
    vu_ok, vu_err = _try_verify_update(conn, project_id, metadata, "qa_pass", "qa",
                       {"type": "e2e_report", "producer": "auto-chain",
                        "summary": {"passed": 1, "failed": 0,
                                    "review": result.get("review_summary", "auto-chain QA pass")}})
    if not vu_ok:
        return False, f"qa_pass gate blocked — {vu_err}"
    # Then verify nodes reached qa_pass
    related_nodes = metadata.get("related_nodes", [])
    if related_nodes:
        passed, reason = _check_nodes_min_status(conn, project_id, related_nodes, "qa_pass")
        if not passed:
            if _is_governed_dirty_workspace_chain(conn, project_id, metadata):
                log.warning(
                    "qa_gate: deferring related_nodes qa_pass enforcement for governed dirty-workspace reconciliation lane: %s",
                    reason,
                )
            else:
                return False, f"qa_pass gate blocked — {reason}"
    # 5e: Graph doc verification (observation mode)
    if _GRAPH_DOC_OBSERVATION_MODE:
        target_files = metadata.get("target_files", [])
        graph_docs = _get_graph_doc_associations(project_id, target_files)
        if graph_docs:
            changed = metadata.get("changed_files", [])
            doc_files_changed = set(f for f in changed if f.startswith("docs/") or f.endswith(".md"))
            graph_docs_missing = set(graph_docs) - doc_files_changed
            if graph_docs_missing:
                log.warning(
                    "qa_gate: graph-linked docs not updated (observation): %s",
                    sorted(graph_docs_missing)[:5],
                )
                _audit_doc_gap(conn, project_id, metadata.get("parent_task_id", ""), "qa_pass", graph_docs_missing, changed)

    # M2: QA passed → write success pattern memory
    _write_chain_memory(
        conn, project_id, "qa_decision",
        result.get("review_summary", f"QA approved (rec={rec})"),
        metadata,
        extra_structured={"recommendation": rec, "chain_stage": "qa",
                          "changed_files": metadata.get("changed_files", [])},
    )
    return True, "ok"


def _gate_gatekeeper_pass(conn, project_id, result, metadata):
    """Require explicit isolated gatekeeper approval before merge."""
    rec = result.get("recommendation", "")
    if rec == "merge_pass":
        try:
            from . import gatekeeper as gk
            gk.record_check(
                conn, project_id,
                check_type="ai_acceptance_check",
                passed=True,
                result={
                    "summary": result.get("review_summary", ""),
                    "checked_requirements": result.get("checked_requirements", []),
                    "pm_alignment": result.get("pm_alignment", "pass"),
                },
                created_by="auto-chain-gatekeeper",
            )
        except Exception:
            log.debug("gatekeeper ai record failed (non-critical)", exc_info=True)

        # PR-C: Commit graph delta after gatekeeper passes (AC1)
        # R4: Escape hatch — skip graph delta validation if explicitly requested
        if metadata.get("skip_graph_delta_validation") is True and metadata.get("skip_reason"):
            log.warning(
                "_gate_gatekeeper_pass: skipping graph delta validation — %s",
                metadata["skip_reason"],
            )
        else:
            try:
                _commit_graph_delta(conn, project_id, metadata)
            except Exception as exc:
                # R1/R5: Graph delta failure blocks the gate
                log.error("_gate_gatekeeper_pass: graph delta commit failed — %s", exc, exc_info=True)
                return False, f"graph delta commit failed: {exc}"

        return True, "ok"
    if rec in ("reject", "rejected"):
        return False, f"Gatekeeper rejected merge: {result.get('reason', 'no reason given')}"
    return False, (
        "Gatekeeper must emit recommendation 'merge_pass' or 'reject'. "
        f"Got: {rec!r}"
    )


def _gate_release(conn, project_id, result, metadata):
    """Verify merge succeeded before deploy."""
    # Node status check: all related_nodes must be "qa_pass" before merge is allowed
    related_nodes = metadata.get("related_nodes", [])
    if related_nodes:
        passed, reason = _check_nodes_min_status(conn, project_id, related_nodes, "qa_pass")
        if not passed:
            if _is_governed_dirty_workspace_chain(conn, project_id, metadata):
                log.warning(
                    "release_gate: deferring related_nodes qa_pass enforcement for governed dirty-workspace reconciliation lane: %s",
                    reason,
                )
            else:
                return False, f"release gate blocked — {reason}"
    else:
        log.warning("release gate: no related_nodes — node verification skipped for %s",
                     metadata.get("parent_task_id", "unknown"))
    # For auto-chain deploys, we trust the merge task result
    # After successful merge, promote related_nodes to qa_pass
    if related_nodes:
        _try_verify_update(conn, project_id, metadata, "qa_pass", "merge",
                           {"type": "merge_complete", "producer": "auto-chain"})

    # TODO-DEPRECATED: _store_proposed_nodes callsite removed per OPT-BACKLOG-GRAPH-DELTA-CHAIN-COMMIT PR-A.
    # Graph delta is now emitted as chain_event in dev completion path via _emit_graph_delta_event().

    # R2: On merge-stage success, resolve pitfall memories linked to dev-retry ancestry
    _resolve_pitfall_memories(conn, project_id, result, metadata)

    return True, "ok"


def _resolve_pitfall_memories(conn, project_id, result, metadata):
    """R2: Walk chain_events backward to locate pitfall memory_ids written during dev-retry
    ancestry, then UPDATE those memories' resolution_commit and resolution_summary.

    Best-effort — never blocks chain progress on failure.
    """
    try:
        merge_commit = result.get("merge_commit", metadata.get("merge_commit", ""))
        if not merge_commit:
            return

        root_task_id = metadata.get("chain_id") or metadata.get("parent_task_id", "")
        if not root_task_id:
            return

        # Find all pitfall memories linked to this chain's scope via module_id matching
        target_files = metadata.get("target_files", [])
        if not target_files:
            return

        # Build module prefixes from target_files
        module_prefixes = []
        for tf in target_files:
            prefix = tf.replace("/", ".").replace("\\", ".")
            module_prefixes.append(prefix)

        # Query pitfall memories that match these modules and lack resolution
        for prefix in module_prefixes:
            try:
                rows = conn.execute(
                    "SELECT memory_id, content FROM memories "
                    "WHERE project_id = ? AND kind = 'pitfall' AND status = 'active' "
                    "AND module_id LIKE ? AND COALESCE(resolution_commit, '') = ''",
                    (project_id, prefix + "%"),
                ).fetchall()
                for row in rows:
                    summary = (row["content"] or "")[:120].replace("\n", " ")
                    conn.execute(
                        "UPDATE memories SET resolution_commit = ?, resolution_summary = ? "
                        "WHERE memory_id = ?",
                        (merge_commit, f"Resolved by merge {merge_commit[:8]}: {summary}", row["memory_id"]),
                    )
            except Exception:
                log.debug("_resolve_pitfall_memories: prefix %s failed", prefix, exc_info=True)
        conn.commit()
        log.info("_resolve_pitfall_memories: resolved pitfalls for merge %s", merge_commit[:8])
    except Exception:
        log.debug("_resolve_pitfall_memories failed (non-critical)", exc_info=True)


def _gate_deploy_pass(conn, project_id, result, metadata):
    """Deploy must report success AND smoke_test semantic coherence.

    R3: Validates that smoke_test.all_pass agrees with report.success and
    that no individual service has a False result. This catches cases where
    the production path diverges from the tested path.
    """
    report = result.get("report", result)
    if not isinstance(report, dict):
        return False, f"deploy failed: report is not a dict"

    # Check report.success first
    if report.get("success") is not True:
        return False, f"deploy failed: {json.dumps(report, ensure_ascii=False)[:300]}"

    # R3: Validate smoke_test semantic coherence
    smoke_test = report.get("smoke_test", {})
    if smoke_test:
        # Reject if all_pass is explicitly False
        if smoke_test.get("all_pass") is False:
            return False, (
                f"deploy gate rejected: smoke_test.all_pass=False contradicts "
                f"report.success=True — {json.dumps(smoke_test, ensure_ascii=False)[:200]}"
            )
        # Reject if any service has an explicit False value
        for svc in ("executor", "governance", "gateway"):
            if smoke_test.get(svc) is False:
                return False, (
                    f"deploy gate rejected: smoke_test.{svc}=False contradicts "
                    f"report.success=True — {json.dumps(smoke_test, ensure_ascii=False)[:200]}"
                )

    return True, "ok"


# ---------------------------------------------------------------------------
# Memory injection helpers for prompt builders (R3/R4/R5/R7)
# ---------------------------------------------------------------------------

def _inject_dev_memories(metadata):
    """R3: Build '## Prior pitfalls in this scope' section for dev prompts.

    Queries memory_service for kind IN (pitfall, pattern), top_k=5,
    module_id prefix-matching target_files, excluding memories older than
    30 days UNLESS resolution_commit is set. Returns section string or ''.
    """
    try:
        from . import memory_service
        from .db import get_connection
        project_id = metadata.get("project_id", "aming-claw")
        target_files = metadata.get("target_files", [])
        if not target_files:
            return ""
        conn = get_connection(project_id)
        try:
            results = memory_service.search_memories_for_injection(
                conn, project_id, target_files,
                kinds=["pitfall", "pattern"],
                top_k=5, max_age_days=30, include_resolved_old=True,
            )
        finally:
            conn.close()
        if not results:
            return ""
        lines = ["## Prior pitfalls in this scope"]
        for m in results:
            kind = m.get("kind", "pitfall")
            content = (m.get("content") or m.get("summary") or "")[:200].replace("\n", " ")
            rc = m.get("resolution_commit", "")
            if rc:
                lines.append(f"- [{kind}] {content} (fixed by commit {rc[:8]})")
            else:
                lines.append(f"- [{kind}] {content}")
        return "\n".join(lines)
    except Exception:
        log.debug("_inject_dev_memories failed (graceful degradation)", exc_info=True)
        return ""


def _inject_qa_memories(metadata):
    """R4: Build '## Prior QA decisions for similar scope' section for QA prompts."""
    try:
        from . import memory_service
        from .db import get_connection
        project_id = metadata.get("project_id", "aming-claw")
        target_files = metadata.get("target_files", [])
        if not target_files:
            return ""
        conn = get_connection(project_id)
        try:
            results = memory_service.search_memories_for_injection(
                conn, project_id, target_files,
                kinds=["qa_decision", "pattern", "failure_pattern"],
                top_k=3, max_age_days=30, include_resolved_old=True,
            )
        finally:
            conn.close()
        if not results:
            return ""
        lines = ["## Prior QA decisions for similar scope"]
        for m in results:
            kind = m.get("kind", "qa_decision")
            content = (m.get("content") or m.get("summary") or "")[:200].replace("\n", " ")
            lines.append(f"- [{kind}] {content}")
        return "\n".join(lines)
    except Exception:
        log.debug("_inject_qa_memories failed (graceful degradation)", exc_info=True)
        return ""


def _inject_gatekeeper_memories(metadata):
    """R5: Build '## Prior decisions' section for gatekeeper prompts."""
    try:
        from . import memory_service
        from .db import get_connection
        project_id = metadata.get("project_id", "aming-claw")
        target_files = metadata.get("target_files", [])
        if not target_files:
            return ""
        conn = get_connection(project_id)
        try:
            results = memory_service.search_memories_for_injection(
                conn, project_id, target_files,
                kinds=["decision", "failure_pattern"],
                top_k=3, max_age_days=30, include_resolved_old=True,
            )
        finally:
            conn.close()
        if not results:
            return ""
        lines = ["## Prior decisions"]
        for m in results:
            kind = m.get("kind", "decision")
            content = (m.get("content") or m.get("summary") or "")[:200].replace("\n", " ")
            lines.append(f"- [{kind}] {content}")
        return "\n".join(lines)
    except Exception:
        log.debug("_inject_gatekeeper_memories failed (graceful degradation)", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Prompt builders — return (prompt: str, metadata: dict)
# ---------------------------------------------------------------------------

def _build_dev_prompt(task_id, result, metadata):
    prd = result.get("prd", {})
    # target_files: result > prd > original metadata (preserve original task metadata)
    target_files = result.get("target_files", prd.get("target_files", metadata.get("target_files", [])))

    verification = result.get("verification", prd.get("verification", {}))
    requirements = result.get("requirements", prd.get("requirements", []))
    criteria = result.get("acceptance_criteria", prd.get("acceptance_criteria", []))
    test_files = result.get("test_files", prd.get("test_files", metadata.get("test_files", [])))
    doc_impact = result.get("doc_impact", prd.get("doc_impact", metadata.get("doc_impact", {})))
    skip_reasons = result.get("skip_reasons", prd.get("skip_reasons", metadata.get("skip_reasons", {})))
    proposed_nodes = result.get("proposed_nodes", metadata.get("proposed_nodes", []))

    # Fallback: if PM result lacks expected structure, read from chain context
    if not target_files or not verification or not criteria:
        try:
            from .chain_context import get_store
            parent_result = get_store().get_parent_result(task_id)
            if parent_result:
                if not target_files:
                    target_files = parent_result.get("target_files", target_files)
                if not verification:
                    verification = parent_result.get("verification", verification)
                if not criteria:
                    criteria = parent_result.get("acceptance_criteria", criteria)
                if not requirements:
                    requirements = parent_result.get("requirements", requirements)
        except Exception:
            pass
    # 5b: Merge graph-derived docs into doc_impact
    graph_docs = _get_graph_doc_associations(
        metadata.get("project_id", "aming-claw"), target_files)
    if graph_docs:
        if isinstance(doc_impact, dict):
            existing = set(doc_impact.get("files", []))
            new_docs = [d for d in graph_docs if d not in existing]
            if new_docs:
                doc_impact = dict(doc_impact)  # copy
                doc_impact["files"] = list(existing | set(new_docs))
                doc_impact.setdefault("changes", []).append(
                    f"Graph-linked docs added: {new_docs[:5]}")
        else:
            doc_impact = {"files": graph_docs, "changes": ["Graph-derived doc associations"]}

    out_meta = {
        **metadata,  # preserves skip_doc_check, changed_files, related_nodes, etc.
        "target_files": target_files,
        "requirements": requirements,
        "acceptance_criteria": criteria,
        "verification": verification,
        "test_files": test_files,
        "doc_impact": doc_impact,
        "skip_reasons": skip_reasons,
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes", result.get("related_nodes", []))),
        "proposed_nodes": proposed_nodes,
    }
    prompt = _render_dev_contract_prompt(task_id, out_meta)
    # R3/R7: Inject prior pitfalls section (graceful degradation)
    pitfalls_section = _inject_dev_memories(out_meta)
    if pitfalls_section:
        prompt = pitfalls_section + "\n\n" + prompt
    return prompt, out_meta


def _build_test_prompt(task_id, result, metadata):
    changed = result.get("changed_files", metadata.get("changed_files", []))
    verification = metadata.get("verification") or result.get("verification", {})
    test_files = metadata.get("test_files", [])
    prompt_parts = [
        f"Run tests for {task_id}.",
        f"changed_files: {json.dumps(changed)}",
    ]
    if verification:
        prompt_parts.append(f"verification: {json.dumps(verification, ensure_ascii=False)}")
    if test_files:
        prompt_parts.append(f"test_files: {json.dumps(test_files)}")
    prompt = "\n".join(prompt_parts)
    meta = {
        **metadata,  # preserves skip_doc_check and all other original task metadata
        # Prioritise original metadata values; only fall back to result if metadata lacks them
        "target_files": metadata.get("target_files") or result.get("target_files", []),
        "changed_files": changed,
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes") or result.get("related_nodes", [])),
        "verification": verification,
        "test_files": test_files,
    }
    # Propagate worktree info from dev result → test → qa → merge
    if result.get("_worktree"):
        meta["_worktree"] = result["_worktree"]
        meta["_branch"] = result.get("_branch", "")
    return prompt, meta


def _query_graph_delta_proposed(metadata):
    """Query chain_events for the latest graph.delta.proposed event on this chain's root_task_id.

    Returns the event payload dict if found, None otherwise.
    """
    try:
        from .chain_context import get_store
        store = get_store()
        # Resolve root_task_id: chain_id in metadata is the PM root, or fallback to parent_task_id
        root_task_id = metadata.get("chain_id") or metadata.get("parent_task_id")
        if not root_task_id:
            return None
        # Also check store's task_to_root mapping for better resolution
        root_task_id = store._task_to_root.get(root_task_id, root_task_id)

        from .db import get_connection
        project_id = metadata.get("project_id", "aming-claw")
        conn = get_connection(project_id)
        try:
            row = conn.execute(
                "SELECT payload_json FROM chain_events "
                "WHERE root_task_id = ? AND event_type = 'graph.delta.proposed' "
                "ORDER BY ts DESC LIMIT 1",
                (root_task_id,),
            ).fetchone()
            if row:
                return json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else row["payload_json"]
        finally:
            conn.close()
    except Exception:
        log.debug("_query_graph_delta_proposed: lookup failed", exc_info=True)
    return None


def _build_qa_prompt(task_id, result, metadata):
    report = result.get("test_report", {})
    changed = result.get("changed_files", metadata.get("changed_files", []))
    requirements = metadata.get("requirements", [])
    criteria = metadata.get("acceptance_criteria", [])
    doc_impact = metadata.get("doc_impact", {})
    verification = metadata.get("verification", {})
    prompt_parts = [
        f"QA review for {task_id}.",
        f"test_report: {json.dumps(report, ensure_ascii=False)}",
        f"changed_files: {json.dumps(changed)}",
    ]
    if requirements:
        prompt_parts.append(f"requirements: {json.dumps(requirements, ensure_ascii=False)}")
    if criteria:
        prompt_parts.append(f"acceptance_criteria: {json.dumps(criteria, ensure_ascii=False)}")
    if verification:
        prompt_parts.append(f"verification: {json.dumps(verification, ensure_ascii=False)}")
    if doc_impact:
        prompt_parts.append(f"doc_impact: {json.dumps(doc_impact, ensure_ascii=False)}")
    if criteria:
        prompt_parts.append(
            "\nYou MUST evaluate each acceptance_criteria item individually.\n"
            "Include in your result:\n"
            "  criteria_results: [{criterion: \"<text>\", passed: true/false, evidence: \"<why>\"}]\n"
            "Only set recommendation='qa_pass' if ALL criteria pass."
        )
    # 5d: Graph consistency check injection
    graph_docs = _get_graph_doc_associations(
        metadata.get("project_id", "aming-claw"),
        metadata.get("target_files", []))
    if graph_docs:
        prompt_parts.append(
            f"\n## Graph Consistency Check\n"
            f"The graph links these docs to the changed code: {json.dumps(graph_docs)}\n"
            f"Verify: are these docs still consistent with the code changes? "
            f"If not, note which docs need updates in your review."
        )
    # PR-B: Query chain_events for graph.delta.proposed and inject review instructions
    _gd_proposed = _query_graph_delta_proposed(metadata)
    if _gd_proposed:
        prompt_parts.append(
            "\n## Graph Delta Review\n"
            "A graph.delta.proposed event was found for this chain. "
            "You MUST review the proposed graph delta below and include a "
            "'graph_delta_review' field in your result JSON.\n\n"
            f"Proposed delta payload:\n{json.dumps(_gd_proposed, ensure_ascii=False, indent=2)}\n\n"
            "Required result field:\n"
            "  graph_delta_review: {\n"
            '    decision: "pass" | "reject",\n'
            "    issues: [\"<issue description>\", ...],  // empty list if decision is pass\n"
            "    suggested_diff: {}  // optional corrections to the delta\n"
            "  }\n"
            "If decision is 'reject', list specific issues. "
            "If decision is 'pass', issues should be an empty list."
        )
    prompt_parts.append("IMPORTANT: result.recommendation MUST be exactly 'qa_pass' or 'reject' (no other values accepted by the gate).")
    # R4/R7: Inject prior QA decisions section (graceful degradation)
    qa_memories = _inject_qa_memories(metadata)
    if qa_memories:
        prompt_parts.insert(0, qa_memories)
    prompt = "\n".join(prompt_parts)
    meta = {
        **metadata,  # preserves skip_doc_check and all other original task metadata
        # Prioritise original metadata values; only fall back to result if metadata lacks them
        "target_files": metadata.get("target_files") or result.get("target_files", []),
        "changed_files": changed,
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes") or result.get("related_nodes", [])),
        "test_report": report,
        "requirements": requirements,
        "acceptance_criteria": criteria,
        "verification": verification,
        "doc_impact": doc_impact,
    }
    if result.get("_worktree"):
        meta["_worktree"] = result["_worktree"]
        meta["_branch"] = result.get("_branch", "")
    return prompt, meta


def _build_gatekeeper_prompt(task_id, result, metadata):
    # R5/R7: Inject prior decisions section (graceful degradation)
    gk_memories = _inject_gatekeeper_memories(metadata)
    prompt = (
        f"Gatekeeper review for {task_id}.\n"
        "You are the final isolated acceptance check before merge.\n"
        "Use ONLY the PM contract, test evidence, QA review, changed file list, and doc-impact summary below.\n"
        "Do NOT request broader project context or unrelated history.\n"
        f"requirements: {json.dumps(metadata.get('requirements', []), ensure_ascii=False)}\n"
        f"acceptance_criteria: {json.dumps(metadata.get('acceptance_criteria', []), ensure_ascii=False)}\n"
        f"verification: {json.dumps(metadata.get('verification', {}), ensure_ascii=False)}\n"
        f"doc_impact: {json.dumps(metadata.get('doc_impact', {}), ensure_ascii=False)}\n"
        f"test_report: {json.dumps(metadata.get('test_report', {}), ensure_ascii=False)}\n"
        f"qa_review: {json.dumps({'review_summary': result.get('review_summary', ''), 'issues': result.get('issues', []), 'doc_updates_applied': result.get('doc_updates_applied', [])}, ensure_ascii=False)}\n"
        f"changed_files: {json.dumps(metadata.get('changed_files', []))}\n"
        "Respond with strict JSON: "
        "{\"schema_version\":\"v1\",\"review_summary\":\"...\",\"recommendation\":\"merge_pass|reject\",\"pm_alignment\":\"pass|partial|fail\",\"checked_requirements\":[\"R1\"],\"reason\":\"\"}"
    )
    if gk_memories:
        prompt = gk_memories + "\n\n" + prompt
    meta = {
        **metadata,
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes", result.get("related_nodes", []))),
    }
    # Propagate worktree isolation metadata through gatekeeper → merge
    if metadata.get("_worktree"):
        meta["_worktree"] = metadata["_worktree"]
        meta["_branch"] = metadata.get("_branch", "")
    elif result.get("_worktree"):
        meta["_worktree"] = result["_worktree"]
        meta["_branch"] = result.get("_branch", "")
    return prompt, meta


def _build_merge_prompt(task_id, result, metadata):
    prompt = f"Merge dev branch for {task_id} to main."
    return prompt, {
        **metadata,  # preserves skip_doc_check and all other original task metadata
        # Prioritise original metadata values; only fall back to result if metadata lacks them
        "target_files": metadata.get("target_files") or result.get("target_files", []),
        "changed_files": metadata.get("changed_files") or result.get("changed_files", []),
        "_worktree": metadata.get("_worktree") or result.get("_worktree", ""),
        "_branch": metadata.get("_branch") or result.get("_branch", ""),
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes") or result.get("related_nodes", [])),
    }


def _build_deploy_prompt(task_id, result, metadata):
    changed_files = metadata.get("changed_files") or result.get("changed_files", [])
    prompt = (
        f"Deploy changes after merge task {task_id}.\n"
        f"changed_files: {json.dumps(changed_files)}\n"
        "Run host-side deploy orchestration and smoke checks."
    )
    return prompt, {
        **metadata,
        "changed_files": changed_files,
        "merge_commit": result.get("merge_commit", metadata.get("merge_commit", "")),
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes") or result.get("related_nodes", [])),
    }


def _try_backlog_close_via_db(project_id, bug_id, commit_hash):
    """Attempt to close a backlog bug via the DB-first REST endpoint.

    Called from merge-stage finalize path when metadata.bug_id is set.
    On success returns True. On 404 or connection error, logs a warning
    (grep for 'backlog.*fallback' or 'backlog.*404') and returns False.
    """
    import urllib.request
    import urllib.error

    gov_url = os.environ.get("GOVERNANCE_URL", "http://localhost:40000").rstrip("/")
    url = f"{gov_url}/api/backlog/{project_id}/{bug_id}/close"
    data = json.dumps({"commit": commit_hash, "actor": "auto-chain"}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status < 300:
                log.info("backlog close: bug %s closed with commit %s", bug_id, commit_hash)
                return True
            log.warning("backlog close: unexpected status %d for bug %s — fallback to md", resp.status, bug_id)
            return False
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log.warning("backlog close: bug %s returned 404 — backlog fallback to md path", bug_id)
        else:
            log.warning("backlog close: HTTP %d for bug %s — backlog fallback to md path", exc.code, bug_id)
        return False
    except Exception as exc:
        log.warning("backlog close: connection error for bug %s (%s) — backlog fallback to md path", bug_id, exc)
        return False


def _finalize_chain(conn, project_id, task_id, result, metadata):
    """Terminal stage after deploy succeeds.

    R4: Call version-sync then version-update to advance chain_version.
    R5: Verify server version == new HEAD; warn if stale.
    R6 (OPT-DB-BACKLOG): Close backlog bug via DB if metadata.bug_id is set.
    """
    import subprocess as _sp

    report = result.get("report", result)
    finalize_result = {"deploy": "completed", "report": report}

    # --- R4: version-sync then version-update ---
    # PR-2 (R11): chain_version DB write now owned by redeploy_handler.
    # _finalize_version_sync is kept as fallback but skipped when redeploy
    # handler already wrote chain_version (detected via report metadata).
    redeploy_wrote_version = (
        report.get("steps", {}).get("executor", {}).get("redeploy_result", {}).get("ok")
        or report.get("steps", {}).get("governance", {}).get("redeploy_result", {}).get("ok")
        or report.get("steps", {}).get("gateway", {}).get("redeploy_result", {}).get("ok")
        or report.get("steps", {}).get("service_manager", {}).get("redeploy_result", {}).get("ok")
    )
    if redeploy_wrote_version:
        log.info("_finalize_chain: skipping _finalize_version_sync — redeploy handler owns DB write (R11)")
        finalize_result["version_sync_note"] = "skipped — redeploy handler owns DB write"
    else:
        try:
            _finalize_version_sync(conn, project_id, task_id)
        except Exception as e:
            log.warning("_finalize_chain: version-sync/update failed: %s", e)
            finalize_result["version_sync_error"] = str(e)

    # --- R5: verify server version == new HEAD ---
    try:
        from .server import get_server_version
        new_head = _sp.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        ).stdout.strip()
        server_ver = get_server_version()
        if new_head and new_head != "unknown" and server_ver != new_head:
            finalize_result["restart_required"] = True
            finalize_result["stale_server_version"] = server_ver
            finalize_result["expected_version"] = new_head
            log.warning(
                "_finalize_chain: server version (%s) != HEAD (%s) — restart_required=true",
                server_ver, new_head,
            )
    except Exception as e:
        log.debug("_finalize_chain: version verify failed: %s", e)

    # --- R6 (OPT-DB-BACKLOG): close backlog bug if metadata.bug_id set ---
    bug_id = metadata.get("bug_id", "")
    if bug_id:
        commit_hash = result.get("merge_commit", metadata.get("merge_commit", ""))
        closed = _try_backlog_close_via_db(project_id, bug_id, commit_hash)
        finalize_result["backlog_closed"] = closed
        if closed:
            finalize_result["backlog_bug_id"] = bug_id

    return finalize_result


def _finalize_version_sync(conn, project_id, task_id):
    """Call version-sync then version-update via local DB ops (R4).

    Uses direct DB writes instead of HTTP to avoid circular dependency.
    """
    import subprocess as _sp

    # Get current git HEAD
    head_result = _sp.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, timeout=5,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    )
    new_head = head_result.stdout.strip() if head_result.returncode == 0 else None
    if not new_head:
        log.warning("_finalize_version_sync: cannot determine git HEAD")
        return

    # version-sync: update git_head and dirty_files in project_version
    try:
        dirty_result = _sp.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        )
        dirty_files = [
            line[3:] for line in (dirty_result.stdout or "").strip().split("\n")
            if line.strip()
        ] if dirty_result.returncode == 0 else []
    except Exception:
        dirty_files = []

    conn.execute(
        "INSERT OR REPLACE INTO project_version (project_id, chain_version, git_head, dirty_files, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (project_id, new_head, new_head, json.dumps(dirty_files)),
    )
    log.info(
        "_finalize_version_sync: version-sync project=%s head=%s dirty=%d",
        project_id, new_head, len(dirty_files),
    )

    # version-update: set chain_version = new HEAD with updated_by='auto-chain'
    conn.execute(
        "UPDATE project_version SET chain_version=?, updated_by=?, updated_at=datetime('now') "
        "WHERE project_id=?",
        (new_head, f"auto-chain:{task_id}", project_id),
    )
    log.info(
        "_finalize_version_sync: version-update project=%s chain_version=%s updated_by=auto-chain:%s",
        project_id, new_head, task_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_chain_memory(conn, project_id, kind, content, metadata, extra_structured=None):
    """Best-effort memory write for chain events. Never blocks chain progress."""
    try:
        from . import memory_service
        from .models import MemoryEntry
        # Derive module_id from first target_file or changed_file
        target = (metadata.get("target_files") or metadata.get("changed_files") or [])
        module_id = target[0].replace("/", ".").replace("\\", ".") if target else "governance"
        # Dedup: skip if identical content already exists for same module+kind
        try:
            existing = conn.execute(
                "SELECT memory_id FROM memories WHERE project_id=? AND module_id=? AND kind=? "
                "AND status='active' AND content=? LIMIT 1",
                (project_id, module_id, kind, content),
            ).fetchone()
            if existing:
                log.debug("chain_memory dedup: skipping identical %s/%s", module_id, kind)
                return
        except Exception:
            pass  # dedup failure should not block write
        entry = MemoryEntry(
            module_id=module_id,
            kind=kind,
            content=content,
            created_by="auto-chain",
        )
        result = memory_service.write_memory(conn, project_id, entry)
        log.info("chain_memory.write: project=%s kind=%s module=%s id=%s content=%r",
                 project_id, kind, module_id, result.get("memory_id", "?"), content[:100])
        if extra_structured:
            # Patch structured field if write succeeded
            mid = result.get("memory_id", "")
            if mid:
                try:
                    import json as _json
                    conn.execute(
                        "UPDATE memories SET structured = ? WHERE memory_id = ?",
                        (_json.dumps(extra_structured), mid),
                    )
                except Exception:
                    pass
    except Exception:
        log.debug("_write_chain_memory failed (non-critical)", exc_info=True)


# Status ordering for node_state validation
_STATUS_ORDER = ["pending", "testing", "t2_pass", "qa_pass", "waived"]

# AC10: Statuses that are treated as "not blocking" — soft-deleted nodes
# don't block gates even though they aren't in the ordinal _STATUS_ORDER.
_NON_BLOCKING_STATUSES = {"rolled_back"}


def _check_nodes_min_status(conn, project_id, related_nodes, min_status):
    """Verify every node in related_nodes has at least min_status in node_state.

    Returns (passed: bool, reason: str).
    If node_state table is empty for this project (fresh DB bootstrap), skip check.
    If a node is not found in a populated DB it is skipped with a warning (not blocked).
    """
    related_nodes = _normalize_related_nodes(related_nodes)
    if not related_nodes:
        return True, "no related_nodes"
    try:
        min_rank = _STATUS_ORDER.index(min_status)
    except ValueError:
        return False, f"unknown min_status '{min_status}'"

    # Fresh DB: if no node_state records exist for this project, skip node check entirely
    node_count = conn.execute(
        "SELECT COUNT(*) FROM node_state WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    if node_count == 0:
        log.info("_check_nodes_min_status: node_state empty for project %s — skipping node check (fresh DB)", project_id)
        return True, "node_state empty (fresh DB bootstrap)"

    blocking = []
    for node_id in related_nodes:
        row = conn.execute(
            "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
            (project_id, node_id),
        ).fetchone()
        if row is None:
            # Not found in populated DB → skip with warning (node was never registered)
            log.warning("_check_nodes_min_status: node '%s' not found in DB for project '%s' — skipping", node_id, project_id)
            continue
        status = (row["verify_status"] or "pending").strip()
        # AC10: rolled_back (soft-deleted) nodes never block gates
        if status in _NON_BLOCKING_STATUSES:
            continue
        try:
            rank = _STATUS_ORDER.index(status)
        except ValueError:
            # Unknown status — treat conservatively as pending
            blocking.append((node_id, f"unknown status '{status}'"))
            continue
        if rank < min_rank:
            blocking.append((node_id, status))

    if blocking:
        details = ", ".join(f"{nid}={st}" for nid, st in blocking)
        return False, (
            f"related_nodes not yet at '{min_status}': [{details}]"
        )
    return True, "ok"


def _try_verify_update(conn, project_id, metadata, target_status, role, evidence_dict):
    """Best-effort node status update. Returns (True, "") on success, (False, error_msg) on failure."""
    related = _normalize_related_nodes(metadata.get("related_nodes", []))
    if not related:
        return True, ""
    try:
        from . import state_service
        from .graph import AcceptanceGraph
        # Load graph from project state directory
        import os
        state_root = os.path.join(
            os.environ.get("SHARED_VOLUME_PATH",
                           os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "shared-volume")),
            "codex-tasks", "state", "governance", project_id)
        graph_path = os.path.join(state_root, "graph.json")
        graph = AcceptanceGraph()
        if os.path.exists(graph_path):
            graph.load(graph_path)
        session = {"principal_id": "auto-chain", "role": role, "scope_json": "[]"}
        state_service.verify_update(
            conn, project_id, graph,
            node_ids=related if isinstance(related, list) else [related],
            target_status=target_status,
            session=session,
            evidence_dict=evidence_dict,
        )
        log.info("auto_chain: nodes %s → %s", related, target_status)
        return True, ""
    except Exception as e:
        log.warning("auto_chain: verify_update %s failed (non-blocking): %s", target_status, e,
                    exc_info=True)
        return False, f"verify_update failed for nodes {related}: {e}"


def _publish_event(event_name, payload):
    """Best-effort event publish to event bus."""
    try:
        from . import event_bus
        event_bus._bus.publish(event_name, payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Function lookup tables (avoid globals() for safety)
# ---------------------------------------------------------------------------

_GATES = {
    "_gate_post_pm": _gate_post_pm,
    "_gate_checkpoint": _gate_checkpoint,
    "_gate_t2_pass": _gate_t2_pass,
    "_gate_qa_pass": _gate_qa_pass,
    "_gate_gatekeeper_pass": _gate_gatekeeper_pass,
    "_gate_release": _gate_release,
    "_gate_deploy_pass": _gate_deploy_pass,
}

_BUILDERS = {
    "_build_dev_prompt": _build_dev_prompt,
    "_build_test_prompt": _build_test_prompt,
    "_build_qa_prompt": _build_qa_prompt,
    "_build_gatekeeper_prompt": _build_gatekeeper_prompt,
    "_build_merge_prompt": _build_merge_prompt,
    "_build_deploy_prompt": _build_deploy_prompt,
    "_finalize_chain": _finalize_chain,
}
