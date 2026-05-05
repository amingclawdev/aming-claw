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
