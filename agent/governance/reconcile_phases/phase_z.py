"""Phase Z — Baseline Discovery (read-only).

Non-destructive discovery of what the codebase contains vs what the
acceptance graph tracks.  Reuses ``graph_generator.generate_graph`` in
read-only mode, produces three-way diffs, groups high-confidence
candidates into epic buckets, and writes artifacts to *scratch_dir*.

**Invariants**
- NEVER calls ``save_graph_atomic`` or ``state_service.init_node_states``.
- ``graph.json`` SHA-256 is unchanged after ``phase_z_run`` completes.
- Backlog rows are only filed when *apply_backlog* is explicitly ``True``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

import urllib.request

from .. import ai_cluster_processor
from ..llm_cache import LLMCache

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROMPT_VERSION = "2026-04-25"

EPIC_BUCKETS = (
    "governance",
    "api-server",
    "executor",
    "scripts",
    "tests",
    "docs",
    "uncategorized",
)

_EPIC_PATH_RULES: List[tuple] = [
    # (path substring/prefix, epic bucket)
    ("agent/governance/", "governance"),
    ("governance/", "governance"),
    ("agent/server", "api-server"),
    ("server/", "api-server"),
    ("api/", "api-server"),
    ("agent/executor", "executor"),
    ("executor/", "executor"),
    ("agent/service_manager", "executor"),
    ("scripts/", "scripts"),
    ("agent/tests/", "tests"),
    ("tests/", "tests"),
    ("test_", "tests"),
    ("docs/", "docs"),
    (".md", "docs"),
]

CONFIDENCE_HIGH_THRESHOLD = 0.85


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class Delta:
    """Single three-way diff finding."""
    delta_type: str          # graph_only_node | missing_node_high_conf | missing_node_low_conf | drift
    node_id: Optional[str]
    action: str              # report_only | candidate | review
    confidence: float
    detail: str
    sub_type: Optional[str] = None  # experiential | stale_candidate | policy_node | manual_review
    files: Optional[List[str]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EpicGroup:
    """Group of high-confidence candidates for a single epic bucket."""
    epic: str
    candidates: List[Delta] = field(default_factory=list)


# ---------------------------------------------------------------------------
# three_way_diff  (R2)
# ---------------------------------------------------------------------------

def _classify_graph_only_node(node_id: str, node_data: dict) -> str:
    """Sub-classify a graph-only node (R2, AC-Z11)."""
    created_by = node_data.get("created_by", "")
    primary = node_data.get("primary", [])
    if isinstance(primary, str):
        primary = [primary]

    # chain-rule → experiential
    if created_by == "chain-rule":
        return "experiential"

    # all primary are .md → policy_node (check BEFORE stale_candidate)
    if primary and all(f.endswith(".md") for f in primary):
        return "policy_node"

    # all primary files deleted (not on disk)
    if primary and all(not os.path.exists(f) for f in primary):
        return "stale_candidate"

    return "manual_review"


def three_way_diff(
    existing_graph: dict,
    candidate_graph: dict,
    workspace_path: str = "",
) -> List[Delta]:
    """Compare existing graph, candidate graph, and filesystem.

    Produces deterministic output — same inputs always yield identical deltas
    (AC-Z8).  Uses sorted iteration everywhere.

    Returns list of :class:`Delta` with three types:
    - ``graph_only_node``: in existing graph but not in candidate; action is
      always ``report_only`` (never suggests deletion).
    - ``missing_node_high_conf`` / ``missing_node_low_conf``: in candidate
      but not in existing graph.
    - ``drift``: present in both but with differing metadata.
    """
    deltas: List[Delta] = []

    existing_nodes: Dict[str, dict] = existing_graph.get("nodes", {})
    candidate_nodes: Dict[str, dict] = candidate_graph.get("nodes", {})

    existing_ids = set(existing_nodes.keys())
    candidate_ids = set(candidate_nodes.keys())

    # 1. graph_only_node — in existing but NOT in candidate
    for nid in sorted(existing_ids - candidate_ids):
        node_data = existing_nodes[nid]
        sub = _classify_graph_only_node(nid, node_data)
        deltas.append(Delta(
            delta_type="graph_only_node",
            node_id=nid,
            action="report_only",
            confidence=1.0,
            detail=f"Node {nid} exists in graph but not in candidate scan",
            sub_type=sub,
            files=node_data.get("primary", []) if isinstance(node_data.get("primary"), list) else [node_data.get("primary", "")],
            metadata={"created_by": node_data.get("created_by", "")},
        ))

    # 2. missing_node — in candidate but NOT in existing
    for nid in sorted(candidate_ids - existing_ids):
        cdata = candidate_nodes[nid]
        conf = _compute_candidate_confidence(cdata)
        is_high_conf = conf >= CONFIDENCE_HIGH_THRESHOLD
        dtype = "missing_node_high_conf" if is_high_conf else "missing_node_low_conf"
        deltas.append(Delta(
            delta_type=dtype,
            node_id=nid,
            action="candidate" if is_high_conf else "review",
            confidence=conf,
            detail=f"Candidate node {nid} discovered by scan (conf={conf:.2f})",
            files=cdata.get("primary", []) if isinstance(cdata.get("primary"), list) else [cdata.get("primary", "")],
            metadata=cdata,
        ))

    # 3. drift — in both but metadata differs
    for nid in sorted(existing_ids & candidate_ids):
        edata = existing_nodes[nid]
        cdata = candidate_nodes[nid]
        drift_fields = _detect_drift(edata, cdata)
        if drift_fields:
            deltas.append(Delta(
                delta_type="drift",
                node_id=nid,
                action="review",
                confidence=0.7,
                detail=f"Drift detected in fields: {', '.join(sorted(drift_fields))}",
                metadata={"drift_fields": sorted(drift_fields)},
            ))

    return deltas


def _compute_candidate_confidence(node_data: dict) -> float:
    """Deterministic confidence score for a candidate node (AC-Z8)."""
    score = 0.5
    primary = node_data.get("primary", [])
    if isinstance(primary, str):
        primary = [primary]
    # More primary files → higher confidence
    if len(primary) >= 3:
        score += 0.2
    elif len(primary) >= 1:
        score += 0.1
    # Has test files
    if node_data.get("test"):
        score += 0.15
    # Has secondary/docs
    if node_data.get("secondary"):
        score += 0.1
    # Clamp
    return min(score, 1.0)


def _detect_drift(existing: dict, candidate: dict) -> List[str]:
    """Return list of fields that differ between existing and candidate."""
    drift = []
    for key in ("primary", "secondary", "test", "deps"):
        ev = existing.get(key, [])
        cv = candidate.get(key, [])
        if isinstance(ev, str):
            ev = [ev]
        if isinstance(cv, str):
            cv = [cv]
        if sorted(ev) != sorted(cv):
            drift.append(key)
    return drift


# ---------------------------------------------------------------------------
# group_into_epics  (R3)
# ---------------------------------------------------------------------------

def _classify_epic(file_path: str) -> str:
    """Classify a file path into one of 7 epic buckets."""
    normalized = file_path.replace("\\", "/").lower()
    for pattern, epic in _EPIC_PATH_RULES:
        if pattern in normalized:
            return epic
    return "uncategorized"


def group_into_epics(deltas: List[Delta]) -> Dict[str, EpicGroup]:
    """Group high-conf missing-node deltas into epic buckets (R3).

    Returns at most 7 groups (one per epic bucket).  AC-Z9.
    """
    groups: Dict[str, EpicGroup] = {}
    for d in deltas:
        if d.delta_type != "missing_node_high_conf":
            continue
        files = d.files or []
        # Determine epic from first file (deterministic)
        epic = "uncategorized"
        for f in sorted(files):
            epic = _classify_epic(f)
            break
        if epic not in groups:
            groups[epic] = EpicGroup(epic=epic)
        groups[epic].candidates.append(d)
    return groups


def _repo_relpath(workspace: str, path: Any) -> str:
    """Return a stable repo-relative slash path when possible."""
    if not path:
        return ""
    raw = str(path)
    try:
        if os.path.isabs(raw):
            raw = os.path.relpath(raw, workspace)
    except Exception:
        pass
    return raw.replace("\\", "/")


def _repo_relpaths(workspace: str, paths: Any) -> List[str]:
    if not paths:
        return []
    if isinstance(paths, str):
        paths = [paths]
    try:
        return sorted({p for p in (_repo_relpath(workspace, item) for item in paths) if p})
    except TypeError:
        return []


def _feature_nodes_from_deltas(
    deltas: List[Delta],
    feature_node_cls: Any,
) -> List[Any]:
    feature_nodes = []
    for d in deltas:
        if d.delta_type != "missing_node_high_conf":
            continue
        files = list(d.files or [])
        module = ""
        if files:
            module = os.path.dirname(files[0].replace("\\", "/"))
        feature_nodes.append(feature_node_cls(
            qname=d.node_id or "",
            module=module,
            primary_files=files,
        ))
    return feature_nodes


def _feature_nodes_from_symbol_nodes(
    symbol_nodes: List[Dict[str, Any]],
    workspace: str,
    feature_node_cls: Any,
) -> List[Any]:
    feature_nodes = []
    for node in symbol_nodes or []:
        qname = str(node.get("node_id") or node.get("module") or "")
        if not qname:
            continue

        primary = _repo_relpath(workspace, node.get("primary_file", ""))
        test_files = _repo_relpaths(
            workspace,
            (node.get("test_coverage") or {}).get("test_files", []),
        )
        doc_files = _repo_relpaths(
            workspace,
            (node.get("doc_coverage") or {}).get("doc_files", []),
        )
        functions = {str(fn) for fn in (node.get("functions") or []) if fn}

        feature_nodes.append(feature_node_cls(
            qname=qname,
            module=str(node.get("module") or qname.rsplit(".", 1)[0]),
            primary_files=[primary] if primary else [],
            secondary_files=sorted(set(test_files + doc_files)),
            descendants=functions,
        ))
    return feature_nodes


def _feature_nodes_from_phase_z_v2(
    workspace: str,
    scratch: str,
    feature_node_cls: Any,
) -> List[Any]:
    from .phase_z_v2 import build_graph_v2_from_symbols

    result = build_graph_v2_from_symbols(
        workspace,
        dry_run=True,
        scratch_dir=scratch,
    )
    symbol_nodes = (
        list(result.get("nodes") or [])
        if isinstance(result, dict) and result.get("status") == "ok"
        else []
    )
    return _feature_nodes_from_symbol_nodes(symbol_nodes, workspace, feature_node_cls)


def _cluster_groups_from_synthesized_feature_clusters(
    feature_clusters: List[Dict[str, Any]],
    feature_node_cls: Any,
    cluster_group_cls: Any,
) -> List[Any]:
    cluster_groups = []
    for cluster in feature_clusters or []:
        primary_files = list(cluster.get("primary_files") or [])
        secondary_files = list(cluster.get("secondary_files") or [])
        functions = {str(fn) for fn in (cluster.get("functions") or []) if fn}
        modules = list(cluster.get("modules") or [])
        decorators = list(cluster.get("decorators") or [])
        entries = []
        for qname in cluster.get("entries") or []:
            qname = str(qname or "")
            if not qname:
                continue
            module = qname.split("::", 1)[0] if "::" in qname else ""
            entries.append(feature_node_cls(
                qname=qname,
                module=module,
                primary_files=primary_files,
                secondary_files=secondary_files,
                descendants=functions,
                decorators=decorators,
                deps=modules,
            ))
        if not entries:
            continue
        group = cluster_group_cls(
            entries=entries,
            primary_files=primary_files,
            secondary_files=secondary_files,
            cluster_fingerprint=str(cluster.get("cluster_fingerprint") or ""),
        )
        try:
            setattr(group, "synthesis", cluster.get("synthesis") or {})
        except Exception:
            pass
        cluster_groups.append(group)
    return cluster_groups


def _cluster_groups_from_phase_z_v2(
    workspace: str,
    scratch: str,
    feature_node_cls: Any,
    cluster_group_cls: Any,
    group_deltas_by_cluster: Any,
) -> List[Any]:
    from .phase_z_v2 import build_graph_v2_from_symbols

    result = build_graph_v2_from_symbols(
        workspace,
        dry_run=True,
        scratch_dir=scratch,
    )
    feature_clusters = (
        list(result.get("feature_clusters") or [])
        if isinstance(result, dict) and result.get("status") == "ok"
        else []
    )
    cluster_groups = _cluster_groups_from_synthesized_feature_clusters(
        feature_clusters,
        feature_node_cls,
        cluster_group_cls,
    )
    if cluster_groups:
        return cluster_groups

    symbol_nodes = (
        list(result.get("nodes") or [])
        if isinstance(result, dict) and result.get("status") == "ok"
        else []
    )
    feature_nodes = _feature_nodes_from_symbol_nodes(symbol_nodes, workspace, feature_node_cls)
    return group_deltas_by_cluster(feature_nodes)


# ---------------------------------------------------------------------------
# Artifact writers  (R8)
# ---------------------------------------------------------------------------

def write_candidate_artifact(scratch_dir: str, candidate_graph: dict) -> str:
    """Write candidate graph JSON to scratch_dir (never graph.json). R8."""
    os.makedirs(scratch_dir, exist_ok=True)
    path = os.path.join(scratch_dir, "phase_z_candidate_graph.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(candidate_graph, f, indent=2, sort_keys=True)
    log.info("phase_z: wrote candidate artifact → %s", path)
    return path


def write_diff_report(scratch_dir: str, deltas: List[Delta], epic_groups: Dict[str, EpicGroup]) -> str:
    """Write delta + epic summary to scratch_dir. R8."""
    os.makedirs(scratch_dir, exist_ok=True)
    path = os.path.join(scratch_dir, "phase_z_diff_report.json")
    report = {
        "generated": date.today().isoformat(),
        "delta_count": len(deltas),
        "deltas": [
            {
                "delta_type": d.delta_type,
                "node_id": d.node_id,
                "action": d.action,
                "confidence": d.confidence,
                "detail": d.detail,
                "sub_type": d.sub_type,
                "files": d.files,
            }
            for d in deltas
        ],
        "epic_summary": {
            epic: {
                "count": len(eg.candidates),
                "node_ids": [c.node_id for c in eg.candidates],
            }
            for epic, eg in sorted(epic_groups.items())
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    log.info("phase_z: wrote diff report → %s", path)
    return path


# ---------------------------------------------------------------------------
# Backlog filing  (R7)
# ---------------------------------------------------------------------------

def file_epic_backlog_row(
    project_id: str,
    epic: str,
    candidates: List[Delta],
    api_base: str = "http://localhost:40000",
) -> Optional[str]:
    """POST a backlog row for one epic bucket. R7."""
    today = date.today().isoformat()
    bug_id = f"OPT-BACKLOG-PHASE-Z-EPIC-{epic.upper()}-{today}"
    url = f"{api_base}/api/backlog/{project_id}/{bug_id}"

    node_ids = [c.node_id for c in candidates if c.node_id]
    files = []
    for c in candidates:
        files.extend(c.files or [])
    files = sorted(set(files))

    payload = {
        "title": f"Phase Z epic: {epic} ({len(candidates)} candidates)",
        "status": "open",
        "priority": "P2",
        "target_files": files,
        "test_files": [],
        "acceptance_criteria": [f"Review and integrate {len(candidates)} discovered candidate nodes"],
        "chain_task_id": "",
        "commit": "",
        "discovered_at": today,
        "fixed_at": "",
        "details_md": f"Auto-discovered by Phase Z baseline scan.\nEpic: {epic}\nCandidates: {', '.join(node_ids)}",
        "chain_trigger_json": json.dumps({"phase": "z", "epic": epic}),
        "required_docs": [],
        "provenance_paths": files,
        "metadata": {
            "operator_id": "reconcile-v3-phase-z",
            "epic": epic,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("phase_z: filed backlog %s → %s", bug_id, resp.status)
        return bug_id
    except Exception as e:
        log.warning("phase_z: failed to file backlog %s: %s", bug_id, e)
        return None


# ---------------------------------------------------------------------------
# Main entry point  (R1)
# ---------------------------------------------------------------------------

def phase_z_run(
    ctx,
    *,
    enable_llm_enrichment: bool = False,
    apply_backlog: bool = False,
    scope_kind: str = None,
    scope_value: str = None,
) -> Dict[str, Any]:
    """Run Phase Z baseline discovery (read-only).

    Parameters
    ----------
    ctx : ReconcileContext or similar
        Must provide ``.workspace_path``, ``.scratch_dir``, ``.project_id``,
        and ``.graph`` (the *existing* loaded graph as a dict).
    enable_llm_enrichment : bool
        When True, run cheap-first LLM enrichment via ``phase_z_llm``.
    apply_backlog : bool
        When True, POST backlog rows to governance API.  Default False (AC-Z1).
    scope_kind : str, optional
        When provided with scope_value, uses slice-aware baseline lookup (R9).
    scope_value : str, optional
        When provided with scope_kind, uses slice-aware baseline lookup (R9).

    Returns
    -------
    dict with keys: deltas, epic_groups, artifacts, backlog_rows
    """
    from ..graph_generator import generate_graph  # R1: read-only call

    # R9: Slice-aware baseline lookup for dedup
    if scope_kind and scope_value:
        try:
            from ..baseline_service import get_last_relevant_baseline
            conn = getattr(ctx, "conn", None)
            pid = getattr(ctx, "project_id", "aming-claw")
            if conn:
                _bl = get_last_relevant_baseline(conn, pid, scope_kind, scope_value)
                log.info("phase_z: using slice-aware baseline B%s for scope %s=%s",
                         _bl.get("baseline_id"), scope_kind, scope_value)
        except Exception as exc:
            log.warning("phase_z: slice-aware baseline lookup failed: %s", exc)

    workspace = getattr(ctx, "workspace_path", getattr(ctx, "workspace", "."))
    scratch = getattr(ctx, "scratch_dir", os.path.join(workspace, "docs", "dev", "scratch"))
    project_id = getattr(ctx, "project_id", "aming-claw")
    api_base = getattr(ctx, "api_base", "http://localhost:40000")
    prefer_symbol_clusters = bool(getattr(ctx, "prefer_symbol_clusters", True))

    # --- existing graph as dict ---
    existing_graph = getattr(ctx, "graph", {})
    if hasattr(existing_graph, "nodes"):
        # Convert from AcceptanceGraph / networkx to plain dict
        existing_nodes = {}
        if hasattr(existing_graph, "G"):
            import networkx as nx
            for nid, ndata in existing_graph.G.nodes(data=True):
                existing_nodes[nid] = dict(ndata)
        existing_graph = {"nodes": existing_nodes}
    elif not isinstance(existing_graph, dict):
        existing_graph = {"nodes": {}}

    # --- generate candidate graph (read-only, R1, AC-Z4) ---
    result = generate_graph(workspace)
    candidate_raw = result if isinstance(result, dict) else {}

    # Normalize candidate to {"nodes": {...}}
    candidate_nodes = {}
    graph_obj = candidate_raw.get("graph")
    if graph_obj and hasattr(graph_obj, "G"):
        import networkx as nx
        for nid, ndata in graph_obj.G.nodes(data=True):
            candidate_nodes[nid] = dict(ndata)
    elif isinstance(candidate_raw.get("nodes"), dict):
        candidate_nodes = candidate_raw["nodes"]
    candidate_graph = {"nodes": candidate_nodes}

    # --- three-way diff (R2) ---
    deltas = three_way_diff(existing_graph, candidate_graph, workspace)

    # --- LLM enrichment (R4, optional) ---
    if enable_llm_enrichment:
        try:
            from .phase_z_llm import enrich_deltas
            deltas = enrich_deltas(deltas, workspace)
        except Exception as e:
            log.warning("phase_z: LLM enrichment failed (non-blocking): %s", e)

    # --- group into epics (R3) ---
    epic_groups = group_into_epics(deltas)

    # --- CR1 R9: also produce 5-signal cluster_groups (additive, retains epic_groups) ---
    cluster_groups: list = []
    try:
        from .cluster_grouper import (
            ClusterGroup as _ClusterGroup,
            FeatureNode as _ClusterFeatureNode,
            group_deltas_by_cluster,
        )

        _feature_nodes = []
        _cluster_source = "none"

        if prefer_symbol_clusters:
            try:
                cluster_groups = _cluster_groups_from_phase_z_v2(
                    workspace,
                    scratch,
                    _ClusterFeatureNode,
                    _ClusterGroup,
                    group_deltas_by_cluster,
                )
                _feature_nodes = [
                    entry
                    for group in cluster_groups
                    for entry in (getattr(group, "entries", []) or [])
                ]
                _cluster_source = "phase-z-v2-symbols"
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("phase_z: phase_z_v2 cluster source failed (non-blocking): %s", exc)

        if not cluster_groups:
            _feature_nodes = _feature_nodes_from_deltas(deltas, _ClusterFeatureNode)
            cluster_groups = group_deltas_by_cluster(_feature_nodes)
            _cluster_source = "legacy-deltas"

        if not cluster_groups and not prefer_symbol_clusters:
            try:
                cluster_groups = _cluster_groups_from_phase_z_v2(
                    workspace,
                    scratch,
                    _ClusterFeatureNode,
                    _ClusterGroup,
                    group_deltas_by_cluster,
                )
                _feature_nodes = [
                    entry
                    for group in cluster_groups
                    for entry in (getattr(group, "entries", []) or [])
                ]
                _cluster_source = "phase-z-v2-symbols"
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("phase_z: phase_z_v2 cluster fallback failed (non-blocking): %s", exc)

        log.info(
            "phase_z: cluster_grouper produced %d clusters from %d feature nodes (source=%s)",
            len(cluster_groups),
            len(_feature_nodes),
            _cluster_source,
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("phase_z: cluster_grouper failed (non-blocking): %s", exc)
        cluster_groups = []

    # --- CR2: feed each cluster through ai_cluster_processor and attach reports ---
    # R4: build a single LLMCache shared across the whole cluster loop so that
    # cache hits short-circuit redundant LLM calls. Construction is wrapped in
    # try/except — failure must not break phase_z (cache=None is supported).
    _llm_cache = None
    try:
        _cache_root = scratch if scratch else workspace
        if _cache_root:
            _llm_cache = LLMCache(_cache_root)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("phase_z: LLMCache construction failed (non-blocking): %s", exc)
        _llm_cache = None

    cluster_payloads: list = []
    for _group in cluster_groups:
        _entries = list(getattr(_group, "entries", []) or [])
        _entry = _entries[0] if _entries else None
        # R6: graceful degradation around the per-cluster invocation.
        try:
            _report = ai_cluster_processor.process_cluster_with_ai(
                cluster=_entries,
                entry=_entry,
                workspace=workspace,
                use_ai=enable_llm_enrichment,
                cache=_llm_cache,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "phase_z: process_cluster_with_ai failed for fingerprint=%s: %s",
                getattr(_group, "cluster_fingerprint", None),
                exc,
            )
            _qname = getattr(_entry, "qname", "") if _entry is not None else ""
            _report = ai_cluster_processor.ClusterReport(
                feature_name=str(_qname or ""),
                purpose=None,
                expected_test_files=[],
                expected_doc_sections=[],
                dead_code_candidates=[],
                missing_tests=[],
                gap_explanation=None,
                doc_validation=None,
                enrichment_status="ai_unavailable",
            )

        # R3: stamp the cluster fingerprint so downstream consumers can join.
        try:
            _report.cluster_fingerprint = getattr(_group, "cluster_fingerprint", None)
        except Exception:  # pragma: no cover - defensive
            pass

        cluster_payloads.append({
            "cluster_fingerprint": getattr(_group, "cluster_fingerprint", None),
            "entries": [getattr(e, "qname", "") for e in _entries],
            "primary_files": list(getattr(_group, "primary_files", []) or []),
            "secondary_files": list(getattr(_group, "secondary_files", []) or []),
            "synthesis": getattr(_group, "synthesis", {}) or {},
            "report": _report.to_dict(),
        })

    # --- write artifacts (R8) ---
    candidate_path = write_candidate_artifact(scratch, candidate_graph)
    report_path = write_diff_report(scratch, deltas, epic_groups)

    # --- file backlog (R7, AC-Z1) ---
    backlog_rows: List[str] = []
    if apply_backlog:
        for epic, eg in sorted(epic_groups.items()):
            row_id = file_epic_backlog_row(project_id, epic, eg.candidates, api_base)
            if row_id:
                backlog_rows.append(row_id)

    return {
        "deltas": deltas,
        "epic_groups": epic_groups,
        # CR2 R5: cluster_groups is now a list of payload dicts (one per cluster)
        # carrying the AI-enriched ClusterReport (as dict) joined to its source
        # ClusterGroup via cluster_fingerprint.
        "cluster_groups": cluster_payloads,
        "artifacts": {
            "candidate_graph": candidate_path,
            "diff_report": report_path,
        },
        "backlog_rows": backlog_rows,
    }
