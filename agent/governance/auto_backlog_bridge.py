"""Phase Z v2 PR4 — Auto-backlog filing bridge (§6 Phase-4).

Converts an approved remediation plan into ``type='reconcile'`` tasks via
the governance HTTP API. Reconcile-type tasks are 4-gate exempt per
MF-2026-04-21-005. Public surface: :func:`compose_bug_id`,
:func:`file_remediation_plan`.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

log = logging.getLogger(__name__)

__all__ = [
    "compose_bug_id", "file_remediation_plan",
    "compose_cluster_bug_id", "file_cluster_as_backlog",
    "enrich_feature_cluster_payload",
    "DEFAULT_CREATOR_ALLOWLIST", "MAX_DUP_SUFFIX",
]

DEFAULT_CREATOR_ALLOWLIST: frozenset[str] = frozenset(
    {"reconcile-bridge", "coordinator", "auto-approval-bot"}
)
MAX_DUP_SUFFIX: int = 10
_DEFAULT_BASE_URL = "http://localhost:40000"
_ACTIVE_STATUSES = ("claimed", "queued", "pending", "running")


def _slug(target_node: str) -> str:
    s = (target_node or "").lower().replace(".", "-")
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "unknown"


def compose_bug_id(run_id: str, action_type: str, target_node: str) -> str:
    """``OPT-BACKLOG-RECONCILE-{run_id[:8]}-{action_type}-{slug}``."""
    return (
        f"OPT-BACKLOG-RECONCILE-{(run_id or '')[:8]}-"
        f"{action_type or 'unknown'}-{_slug(target_node)}"
    )


def _is_allowed_creator(creator: str) -> bool:
    if not isinstance(creator, str) or not creator:
        return False
    return creator in DEFAULT_CREATOR_ALLOWLIST or creator.startswith("observer-")


class _DefaultHttpClient:
    """urllib-backed JSON HTTP client used when caller passes none."""

    def __init__(self, base_url: str = _DEFAULT_BASE_URL, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _full(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}{path}"

    def get(self, url: str) -> dict:
        req = urllib.request.Request(self._full(url), method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8") or "{}")

    def post(self, url: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self._full(url), data=json.dumps(payload).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8") or "{}")


def _exception_message(exc: Exception) -> str:
    """Return an exception message with HTTP response bodies when available."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        if body:
            return f"{exc!s}: {body}"
    return str(exc)


def _count_active_tasks(http_client: Any, project_id: str) -> int:
    try:
        resp = http_client.get(f"/api/task/{project_id}/list?limit=200")
    except Exception:
        log.warning("auto_backlog_bridge: queue-size probe failed", exc_info=True)
        return 0
    tasks = resp.get("tasks") if isinstance(resp, dict) else None
    if not isinstance(tasks, list):
        return 0
    return sum(
        1 for t in tasks
        if isinstance(t, dict) and str(t.get("status", "")).lower() in _ACTIVE_STATUSES
    )


def _bug_exists(http_client: Any, project_id: str, bug_id: str) -> bool:
    if not bug_id:
        return False
    try:
        resp = http_client.get(
            f"/api/backlog/{project_id}/exists?bug_id={urllib.parse.quote(bug_id)}"
        )
    except Exception:
        return False
    return isinstance(resp, dict) and bool(resp.get("exists", False))


def file_remediation_plan(
    plan: dict, run_id: str, project_id: str,
    creator: str = "reconcile-bridge", dry_run: bool = False,
    queue_threshold: int = 50, http_client: Any = None,
) -> dict:
    """File each plan action as a reconcile task; see module docstring."""
    result: dict[str, Any] = {
        "filed": 0, "skipped": 0, "errors": [], "task_ids": [],
        "planned": [] if dry_run else None,
    }
    if not _is_allowed_creator(creator):
        result["errors"].append({"reason": "unauthorized_creator", "creator": creator})
        log.warning("auto_backlog_bridge: unauthorized_creator=%r — no tasks filed", creator)
        return result

    actions: Iterable[dict] = (plan or {}).get("actions") or []
    if not isinstance(actions, list):
        actions = []
    plan_id = (plan or {}).get("plan_id") or ""
    if http_client is None and not dry_run:
        http_client = _DefaultHttpClient()

    for idx, action in enumerate(actions):
        if not isinstance(action, dict):
            result["errors"].append({"action_index": idx, "reason": "invalid_action"})
            continue
        action_type = action.get("action") or action.get("action_type") or "unknown"
        target_node = action.get("target_node") or action.get("node") or ""
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        target_files = params.get("files") or []
        if not isinstance(target_files, list):
            target_files = [str(target_files)]
        drift_type = params.get("drift_type") or "unknown"
        base_bug_id = compose_bug_id(run_id, action_type, target_node)

        # R7 — queue capacity check (applies even in dry-run when http_client supplied).
        if http_client is not None:
            try:
                active = _count_active_tasks(http_client, project_id)
            except Exception:
                active = 0
            if active >= queue_threshold:
                result["skipped"] += 1
                log.warning("auto_backlog_bridge: queue_full active=%d threshold=%d action_index=%d",
                            active, queue_threshold, idx)
                continue

        # R5 — duplicate-suffix walk -2..-10; beyond cap, record error.
        chosen_bug_id: str | None = base_bug_id
        if http_client is not None:
            suffix = 1
            while _bug_exists(http_client, project_id, chosen_bug_id):
                suffix += 1
                if suffix > MAX_DUP_SUFFIX:
                    chosen_bug_id = None
                    break
                chosen_bug_id = f"{base_bug_id}-{suffix}"
        if chosen_bug_id is None:
            result["errors"].append({"action_index": idx, "bug_id": base_bug_id,
                                     "reason": "duplicate_collision_exhausted"})
            continue

        metadata = {"bug_id": chosen_bug_id, "reconcile_run_id": run_id,
                    "drift_type": drift_type, "plan_id": plan_id, "action_index": idx}
        payload = {"type": "reconcile",
                   "prompt": action.get("prompt") or f"reconcile {action_type} {target_node}",
                   "target_files": target_files, "metadata": metadata, "created_by": creator}

        if dry_run:
            result["planned"].append({"bug_id": chosen_bug_id, "action_index": idx,
                                      "target_node": target_node,
                                      "target_files": list(target_files),
                                      "action_type": action_type, "drift_type": drift_type})
            continue

        try:
            resp = http_client.post(f"/api/task/{project_id}/create", payload)
        except Exception as exc:
            result["errors"].append({"action_index": idx, "bug_id": chosen_bug_id,
                                     "reason": f"create_failed: {exc!s}"})
            continue

        task_id = str(resp.get("task_id") or "") if isinstance(resp, dict) else ""
        if task_id:
            result["task_ids"].append(task_id)
            result["filed"] += 1
        else:
            result["errors"].append({"action_index": idx, "bug_id": chosen_bug_id,
                                     "reason": "create_no_task_id"})

    return result


# ---------------------------------------------------------------------------
# CR3 — Reconcile-cluster backlog filing (R4)
# ---------------------------------------------------------------------------


def compose_cluster_bug_id(
    run_id: str, cluster_fingerprint: str, slug_hint: str = "cluster",
) -> str:
    """Compose the canonical cluster bug_id.

    Format: ``OPT-BACKLOG-RECONCILE-{run_id[:8]}-CLUSTER-{cluster_fingerprint[:8]}-{slug}``
    """
    rid = (run_id or "")[:8]
    fp = (cluster_fingerprint or "")[:8]
    return (
        f"OPT-BACKLOG-RECONCILE-{rid}-CLUSTER-{fp}-{_slug(slug_hint)}"
    )


def _normalize_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if text.lower() in {"none", "null", "n/a", "na", "-"}:
        return ""
    return text


def _path_list(*values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            candidates = value
        else:
            candidates = [value]
        for item in candidates:
            text = _normalize_path(item)
            if text and text not in out:
                out.append(text)
    return out


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or node.get("node_id") or node.get("candidate_node_id") or "").strip()


def _candidate_nodes(candidate_graph: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("deps_graph", "hierarchy_graph", "evidence_graph"):
        section = candidate_graph.get(key)
        if not isinstance(section, dict):
            continue
        raw = section.get("nodes")
        if isinstance(raw, dict):
            nodes = [dict(v, id=str(k)) for k, v in raw.items() if isinstance(v, dict)]
        elif isinstance(raw, list):
            nodes = [dict(n) for n in raw if isinstance(n, dict)]
        else:
            nodes = []
        leafs = [n for n in nodes if _path_list(n.get("primary"), n.get("primary_files"))]
        if leafs:
            return leafs
    return []


def _node_test_files(node: dict[str, Any]) -> list[str]:
    coverage = node.get("test_coverage")
    coverage_files = coverage.get("test_files") if isinstance(coverage, dict) else []
    return _path_list(
        node.get("test"),
        node.get("tests"),
        node.get("test_files"),
        coverage_files,
    )


def _node_doc_files(node: dict[str, Any]) -> list[str]:
    return [
        p for p in _path_list(node.get("secondary"), node.get("secondary_files"))
        if p.lower().endswith((".md", ".mdx", ".rst", ".txt")) or p.startswith("docs/")
    ]


def _cluster_slug_hint(cluster: dict[str, Any], matched_nodes: list[dict[str, Any]]) -> str:
    if cluster.get("slug"):
        return str(cluster["slug"])
    modules = cluster.get("modules") if isinstance(cluster.get("modules"), list) else []
    if modules:
        return " ".join(str(m).split(".")[-1] for m in modules[:2])
    if matched_nodes:
        return str(matched_nodes[0].get("title") or _node_id(matched_nodes[0]) or "cluster")
    return str(cluster.get("cluster_fingerprint") or "cluster")


def _controlled_reconcile_prompt() -> str:
    return (
        "Controlled graph rebase rollout cluster. PM must propose exactly the "
        "candidate node_id set from cluster_payload.candidate_nodes, preserving "
        "node_id, parent, title, primary, layer, deps, secondary, test, and "
        "test_coverage exactly. Candidate doc/test consumers are graph identity; "
        "do not drop, move, dedupe across sibling nodes, or invent them in PM. "
        "Dev must emit graph_delta.creates one-for-one with PM proposed_nodes "
        "and must preserve the same identity fields before writing overlay. "
        "Do not mutate the active graph or candidate graph artifacts. "
        "Default outcome is overlay-only changed_files=[]; modify source/docs/tests "
        "only if verification proves a real defect. If candidate nodes declare "
        "Python test consumers, PM verification.command must run pytest over "
        "those exact test files; path-existence verification is allowed only "
        "when the candidate has no Python tests. Do not cite ignored docs/dev "
        "proposal files as required evidence."
    )


def enrich_feature_cluster_payload(
    cluster: dict[str, Any],
    *,
    candidate_graph: dict[str, Any] | None = None,
    candidate_graph_path: str = "",
    overlay_path: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    """Attach candidate graph context before a FeatureCluster enters the chain."""
    payload = dict(cluster or {})
    primaries = set(_path_list(payload.get("primary_files")))
    matched_nodes: list[dict[str, Any]] = []
    for node in _candidate_nodes(candidate_graph or {}):
        node_primaries = set(_path_list(node.get("primary"), node.get("primary_files")))
        if primaries and primaries.intersection(node_primaries):
            normalized = dict(node)
            nid = _node_id(normalized)
            if nid:
                normalized["node_id"] = nid
            normalized["primary"] = _path_list(
                normalized.get("primary"),
                normalized.get("primary_files"),
            )
            normalized["secondary"] = _path_list(
                normalized.get("secondary"),
                normalized.get("secondary_files"),
            )
            normalized["test"] = _node_test_files(normalized)
            matched_nodes.append(normalized)
    matched_nodes.sort(key=lambda n: _node_id(n) or ",".join(_path_list(n.get("primary"))))

    secondary_files = _path_list(payload.get("secondary_files"))
    expected_tests = _path_list(
        *[_node_test_files(n) for n in matched_nodes],
        [p for p in secondary_files if "/test" in f"/{p.lower()}" or p.lower().endswith(("_test.py", ".test.js", ".spec.js"))],
    )
    expected_docs = _path_list(
        *[_node_doc_files(n) for n in matched_nodes],
        [p for p in secondary_files if p.lower().endswith((".md", ".mdx", ".rst", ".txt")) or p.startswith("docs/")],
    )

    if candidate_graph_path:
        payload["candidate_graph_path"] = candidate_graph_path
    if overlay_path:
        payload["overlay_path"] = overlay_path
        payload["reconcile_overlay_path"] = overlay_path
    if run_id:
        payload.setdefault("batch_id", run_id)
    if matched_nodes:
        payload["candidate_nodes"] = matched_nodes

    report = dict(payload.get("cluster_report") or {})
    report.setdefault(
        "purpose",
        (
            "Controlled graph rebase rollout: validate this FeatureCluster, "
            "preserve candidate node identity, attach tracked doc/test consumers "
            "when evidence exists, and write overlay-only graph_delta."
        ),
    )
    report.setdefault("title", _cluster_slug_hint(payload, matched_nodes))
    report["expected_test_files"] = _path_list(report.get("expected_test_files"), expected_tests)
    report["expected_doc_files"] = _path_list(report.get("expected_doc_files"), expected_docs)
    report["expected_doc_sections"] = _path_list(report.get("expected_doc_sections"), expected_docs)
    report.setdefault(
        "coverage_audit",
        (
            "PM must inspect candidate_nodes plus expected_doc_files/"
            "expected_test_files; if an expected test/doc is a consumer, add it "
            "to graph_delta node.test/secondary without editing files unless a "
            "real defect is proven."
        ),
    )
    if candidate_graph_path:
        report.setdefault("candidate_graph_path", candidate_graph_path)
    if overlay_path:
        report.setdefault("overlay_path", overlay_path)
    payload["cluster_report"] = report
    payload.setdefault("prompt", _controlled_reconcile_prompt())
    payload.setdefault("slug", _cluster_slug_hint(payload, matched_nodes))
    return payload


def file_cluster_as_backlog(
    cluster_group: dict,
    cluster_report: dict,
    run_id: str,
    project_id: str,
    *,
    creator: str = "reconcile-bridge",
    http_client: Any = None,
    operation_type: str = "reconcile-cluster",
    batch_id: str = "",
) -> dict:
    """File one ``type='pm'`` task per cluster group (R4).

    POSTs ``/api/task/{project_id}/create`` with::

        {
          "type": "pm",                       # 4-gate exempt path
          "metadata": {
            "operation_type": "reconcile-cluster",
            "cluster_fingerprint": "...",
            "cluster_payload": {...},
            "cluster_report": {...},
            "bug_id": "OPT-BACKLOG-RECONCILE-{run_id[:8]}-CLUSTER-{fp[:8]}-{slug}"
          },
          ...
        }

    Returns a dict::

        {"backlog_id": <bug_id>, "task_id": <task_id|"">,
         "skipped": <bool>, "reason": <str>, "filed": <bool>}
    """
    cluster_group = cluster_group or {}
    cluster_report = cluster_report or {}
    cluster_fp = (
        cluster_group.get("cluster_fingerprint")
        or cluster_report.get("cluster_fingerprint")
        or ""
    )
    slug_hint = (
        cluster_group.get("slug")
        or cluster_report.get("title")
        or cluster_report.get("purpose")
        or "cluster"
    )
    backlog_id = compose_cluster_bug_id(run_id, cluster_fp, slug_hint)

    out: dict[str, Any] = {
        "backlog_id": backlog_id,
        "task_id": "",
        "filed": False,
        "skipped": False,
        "reason": "",
    }

    if not _is_allowed_creator(creator):
        out["skipped"] = True
        out["reason"] = "unauthorized_creator"
        log.warning(
            "auto_backlog_bridge: cluster_filing rejected unauthorized_creator=%r",
            creator,
        )
        return out

    if http_client is None:
        http_client = _DefaultHttpClient()

    batch_id = (
        batch_id
        or str(cluster_group.get("batch_id") or "")
        or str(cluster_report.get("batch_id") or "")
        or str(run_id or "")
    )
    active_session: dict[str, Any] = {}
    if operation_type == "reconcile-cluster":
        try:
            session_resp = http_client.get(f"/api/reconcile/{project_id}/sessions/active")
            if isinstance(session_resp, dict) and isinstance(session_resp.get("session"), dict):
                active_session = session_resp["session"]
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "auto_backlog_bridge: active reconcile session lookup failed run=%s err=%s",
                run_id, exc,
            )
    batch_ref: dict[str, Any] = {}
    if operation_type == "reconcile-cluster" and batch_id:
        try:
            batch_resp = http_client.post(
                f"/api/reconcile/{project_id}/batch-memory",
                {
                    "batch_id": batch_id,
                    "session_id": active_session.get("session_id") or run_id,
                    "created_by": creator,
                    "initial_memory": {
                        "candidate_graph_path": (
                            cluster_group.get("candidate_graph_path")
                            or cluster_report.get("candidate_graph_path")
                            or ""
                        ),
                        "known_clusters": {
                            cluster_fp: {
                                "primary_files": list(cluster_group.get("primary_files") or []),
                                "cluster_fingerprint": cluster_fp,
                                "purpose": cluster_report.get("purpose") or "",
                            }
                        } if cluster_fp else {},
                    },
                },
            )
            if isinstance(batch_resp, dict):
                batch = batch_resp.get("batch") if isinstance(batch_resp.get("batch"), dict) else {}
                batch_ref = {
                    "batch_id": str(batch.get("batch_id") or batch_id),
                    "session_id": str(batch.get("session_id") or run_id or ""),
                }
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "auto_backlog_bridge: batch-memory create/get failed batch=%s err=%s",
                batch_id, exc,
            )

    metadata = {
        "bug_id": backlog_id,
        "operation_type": operation_type,
        "cluster_fingerprint": cluster_fp,
        "cluster_payload": cluster_group,
        "cluster_report": cluster_report,
        "reconcile_run_id": run_id,
    }
    if operation_type == "reconcile-cluster":
        metadata.update({
            "skip_version_check": True,
            "operator_id": creator,
            "bypass_reason": (
                "reconcile-cluster branch-local graph rebase; observer preflight "
                "validated runtime before filing"
            ),
        })
        for key in ("candidate_graph_path", "overlay_path", "reconcile_overlay_path"):
            value = cluster_group.get(key) or cluster_report.get(key)
            if value:
                metadata[key] = str(value)
    if active_session:
        metadata.update({
            "session_id": active_session.get("session_id", ""),
            "reconcile_session_id": active_session.get("session_id", ""),
            "reconcile_target_branch": active_session.get("target_branch", ""),
            "reconcile_target_base_commit": active_session.get("base_commit_sha", ""),
            "reconcile_target_head": active_session.get("target_head_sha", ""),
        })
    if batch_id:
        metadata["batch_id"] = batch_ref.get("batch_id") or batch_id
        metadata["reconcile_batch_id"] = metadata["batch_id"]
        metadata["batch_memory_ref"] = batch_ref or {
            "batch_id": batch_id,
            "session_id": active_session.get("session_id") or run_id,
        }
    primary_files = list(
        cluster_group.get("primary_files")
        or cluster_report.get("expected_doc_sections")
        or []
    )
    target_files = [str(f) for f in primary_files]
    expected_test_files = [
        str(f) for f in (cluster_report.get("expected_test_files") or [])
    ]
    metadata["target_files"] = target_files
    metadata["test_files"] = expected_test_files
    backlog_payload = {
        "title": (
            f"Reconcile FeatureCluster {cluster_fp[:8]}: "
            f"{slug_hint or 'cluster'}"
        ),
        "status": "OPEN",
        "priority": "P2",
        "target_files": target_files,
        "test_files": list(cluster_group.get("secondary_files") or []),
        "acceptance_criteria": [
            "PM produces candidate-only graph proposal for this FeatureCluster",
            "Dev/Test/QA validate docs/tests/coverage gaps for the cluster",
            "Gatekeeper applies candidate nodes to overlay only",
        ],
        "details_md": (
            "Auto-filed by reconcile cluster bridge before PM task creation.\n\n"
            f"run_id: {run_id}\n"
            f"cluster_fingerprint: {cluster_fp}\n"
            f"primary_files: {', '.join(target_files)}"
        ),
        "chain_trigger_json": {
            "operation_type": operation_type,
            "reconcile_run_id": run_id,
            "cluster_fingerprint": cluster_fp,
            "reconcile_session_id": active_session.get("session_id", ""),
            "reconcile_target_branch": active_session.get("target_branch", ""),
            "reconcile_target_base_commit": active_session.get("base_commit_sha", ""),
        },
        "force_admit": True,
        "actor": creator,
    }

    payload = {
        "type": "pm",
        "prompt": (
            cluster_group.get("prompt")
            or f"Reconcile cluster {cluster_fp[:8]} — produce PRD"
        ),
        "target_files": target_files,
        "metadata": metadata,
        "created_by": creator,
    }

    try:
        http_client.post(
            f"/api/backlog/{project_id}/{urllib.parse.quote(backlog_id)}",
            backlog_payload,
        )
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"backlog_upsert_failed: {_exception_message(exc)}"
        log.warning(
            "auto_backlog_bridge: cluster backlog upsert failed bug=%s err=%s",
            backlog_id, exc,
        )
        return out

    try:
        resp = http_client.post(f"/api/task/{project_id}/create", payload)
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"create_failed: {_exception_message(exc)}"
        log.warning(
            "auto_backlog_bridge: cluster_filing POST failed bug=%s err=%s",
            backlog_id, exc,
        )
        return out

    task_id = str(resp.get("task_id") or "") if isinstance(resp, dict) else ""
    if task_id:
        out["task_id"] = task_id
        out["filed"] = True
        return out
    out["reason"] = "create_no_task_id"
    return out
