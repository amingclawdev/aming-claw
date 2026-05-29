"""E2E evidence ledger and impact planning.

The ledger is intentionally file-backed JSON next to the governance state. It
keeps E2E proof independent from graph schema migrations while still binding a
passing run to snapshot file hashes and L7 feature hashes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import sqlite3

from . import graph_snapshot_store as store
from .db import _governance_root


LEDGER_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_hash(payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def project_e2e_dir(project_id: str) -> Path:
    return _governance_root() / project_id / "e2e"


def project_ledger_path(project_id: str) -> Path:
    return project_e2e_dir(project_id) / "evidence-ledger.json"


def snapshot_ledger_path(project_id: str, snapshot_id: str) -> Path:
    return store.snapshot_companion_dir(project_id, snapshot_id) / "e2e" / "evidence-ledger.json"


def _normalize_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip().strip("/")


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values: Iterable[Any] = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        value = _normalize_path(item)
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _graph_nodes(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    if isinstance(deps, dict) and isinstance(deps.get("nodes"), list):
        return [dict(node) for node in deps["nodes"] if isinstance(node, dict)]
    nodes = graph_json.get("nodes") if isinstance(graph_json, dict) else []
    if isinstance(nodes, list):
        return [dict(node) for node in nodes if isinstance(node, dict)]
    if isinstance(nodes, dict):
        result = []
        for node_id, node in nodes.items():
            item = dict(node) if isinstance(node, dict) else {}
            item.setdefault("id", str(node_id))
            result.append(item)
        return result
    return []


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("node_id") or node.get("id") or "").strip()


def _node_paths(node: dict[str, Any]) -> list[str]:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    values: list[Any] = []
    for key in ("primary_files", "primary", "secondary_files", "secondary", "test_files", "test", "config"):
        raw = node.get(key)
        if isinstance(raw, list):
            values.extend(raw)
    config_files = metadata.get("config_files") if isinstance(metadata, dict) else []
    if isinstance(config_files, list):
        values.extend(config_files)
    return _string_list(values)


def _file_hash(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    for key in ("file_hash", "sha256", "hash", "content_hash"):
        value = str(row.get(key) or "").strip()
        if value:
            return value if value.startswith("sha256:") else f"sha256:{value}"
    return ""


def _snapshot_state(project_id: str, snapshot_id: str) -> dict[str, Any]:
    graph_json = _read_json(store.snapshot_graph_path(project_id, snapshot_id), {})
    inventory_rows = _read_json(store.snapshot_companion_dir(project_id, snapshot_id) / "file_inventory.json", [])
    if not isinstance(inventory_rows, list):
        inventory_rows = []
    inventory = {
        _normalize_path(row.get("path")): dict(row)
        for row in inventory_rows
        if isinstance(row, dict) and _normalize_path(row.get("path"))
    }
    nodes = {
        _node_id(node): node
        for node in _graph_nodes(graph_json if isinstance(graph_json, dict) else {})
        if _node_id(node)
    }
    return {"graph_json": graph_json, "inventory": inventory, "nodes": nodes}


def _feature_hash(node: dict[str, Any] | None, inventory: dict[str, dict[str, Any]]) -> str:
    if not node:
        return ""
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    direct = str(node.get("feature_hash") or metadata.get("feature_hash") or "").strip()
    if direct:
        return direct if direct.startswith("sha256:") else f"sha256:{direct}"
    files = [
        {"path": path, "hash": _file_hash(inventory.get(path))}
        for path in _node_paths(node)
    ]
    return _json_hash({"node_id": _node_id(node), "files": sorted(files, key=lambda row: row["path"])})


def _file_evidence(path_value: str, inventory: dict[str, dict[str, Any]]) -> dict[str, Any]:
    path = _normalize_path(path_value)
    row = inventory.get(path)
    return {
        "path": path,
        "present": bool(row),
        "file_hash": _file_hash(row),
        "file_kind": str(row.get("file_kind") or "") if row else "",
        "scan_status": str(row.get("scan_status") or "") if row else "",
    }


def _node_evidence(node_id: str, nodes: dict[str, dict[str, Any]], inventory: dict[str, dict[str, Any]]) -> dict[str, Any]:
    node = nodes.get(str(node_id))
    return {
        "node_id": str(node_id),
        "present": bool(node),
        "title": str((node or {}).get("title") or ""),
        "layer": str((node or {}).get("layer") or ""),
        "feature_hash": _feature_hash(node, inventory),
        "paths": _node_paths(node or {}),
    }


def _load_project_ledger(project_id: str) -> dict[str, Any]:
    payload = _read_json(project_ledger_path(project_id), {"version": LEDGER_VERSION, "entries": []})
    if not isinstance(payload, dict):
        payload = {"version": LEDGER_VERSION, "entries": []}
    entries = payload.get("entries")
    if not isinstance(entries, list):
        payload["entries"] = []
    payload["version"] = LEDGER_VERSION
    return payload


def _write_project_ledger(project_id: str, payload: dict[str, Any]) -> None:
    _write_json(project_ledger_path(project_id), payload)


def _append_snapshot_ledger(project_id: str, snapshot_id: str, entry: dict[str, Any]) -> None:
    path = snapshot_ledger_path(project_id, snapshot_id)
    payload = _read_json(path, {"version": LEDGER_VERSION, "entries": []})
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        payload = {"version": LEDGER_VERSION, "entries": []}
    payload["entries"] = [row for row in payload["entries"] if row.get("evidence_id") != entry["evidence_id"]]
    payload["entries"].append(entry)
    _write_json(path, payload)


def record_e2e_evidence(
    conn: sqlite3.Connection | None,
    project_id: str,
    snapshot_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Persist one E2E run proof bound to the current snapshot hashes."""
    if conn is not None and not store.get_graph_snapshot(conn, project_id, snapshot_id):
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    state = _snapshot_state(project_id, snapshot_id)
    coverage = body.get("coverage") if isinstance(body.get("coverage"), dict) else {}
    covered_node_ids = _string_list(body.get("covered_node_ids") or body.get("node_ids") or coverage.get("node_ids"))
    covered_files = _string_list(body.get("covered_files") or body.get("files") or coverage.get("files"))
    for node_id in covered_node_ids:
        covered_files.extend(_node_paths(state["nodes"].get(node_id, {})))
    covered_files = _string_list(covered_files)

    created_at = str(body.get("created_at") or _utc_now())
    suite_id = str(body.get("suite_id") or "dashboard.trunk").strip()
    run_id = str(body.get("run_id") or "").strip()
    evidence_id = str(body.get("evidence_id") or "").strip()
    if not evidence_id:
        evidence_id = "e2e-" + hashlib.sha256(
            json.dumps([project_id, snapshot_id, suite_id, run_id, created_at], sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]

    entry = {
        "evidence_id": evidence_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "suite_id": suite_id,
        "status": str(body.get("status") or "passed").strip().lower(),
        "command": str(body.get("command") or "").strip(),
        "run_id": run_id,
        "artifact_path": str(body.get("artifact_path") or body.get("report_path") or "").strip(),
        "actor": str(body.get("actor") or "dashboard_e2e").strip(),
        "created_at": created_at,
        "covered_files": {
            path: _file_evidence(path, state["inventory"])
            for path in sorted(covered_files)
        },
        "covered_nodes": {
            node_id: _node_evidence(node_id, state["nodes"], state["inventory"])
            for node_id in sorted(covered_node_ids)
        },
        "metadata": body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
    }
    entry["evidence_hash"] = _json_hash(entry)

    ledger = _load_project_ledger(project_id)
    entries = [row for row in ledger.get("entries", []) if isinstance(row, dict) and row.get("evidence_id") != evidence_id]
    entries.append(entry)
    ledger["entries"] = entries[-1000:]
    ledger["updated_at"] = created_at
    _write_project_ledger(project_id, ledger)
    _append_snapshot_ledger(project_id, snapshot_id, entry)
    return {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "evidence_id": evidence_id,
        "suite_id": suite_id,
        "status": entry["status"],
        "covered_file_count": len(entry["covered_files"]),
        "covered_node_count": len(entry["covered_nodes"]),
        "ledger_path": str(project_ledger_path(project_id)),
    }


def _latest_entries(project_id: str) -> dict[str, dict[str, Any]]:
    entries = [
        row for row in _load_project_ledger(project_id).get("entries", [])
        if isinstance(row, dict) and row.get("suite_id")
    ]
    entries.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("evidence_id") or "")))
    latest: dict[str, dict[str, Any]] = {}
    for entry in entries:
        latest[str(entry["suite_id"])] = entry
    return latest


def _suite_trigger(suite: dict[str, Any]) -> dict[str, Any]:
    trigger = suite.get("trigger") if isinstance(suite.get("trigger"), dict) else {}
    return {
        "paths": _string_list(trigger.get("paths")),
        "nodes": _string_list(trigger.get("nodes")),
        "tags": _string_list(trigger.get("tags")),
    }


def _match_path(path: str, patterns: list[str]) -> bool:
    norm = _normalize_path(path)
    for pattern in patterns:
        pat = _normalize_path(pattern)
        if not pat:
            continue
        if fnmatch.fnmatch(norm, pat) or norm == pat or norm.startswith(pat.rstrip("/") + "/"):
            return True
    return False


def _trigger_matches(
    suite: dict[str, Any],
    *,
    changed_files: list[str],
    changed_node_ids: list[str],
) -> bool:
    trigger = _suite_trigger(suite)
    if changed_files and any(_match_path(path, trigger["paths"]) for path in changed_files):
        return True
    if changed_node_ids and set(changed_node_ids).intersection(trigger["nodes"]):
        return True
    return False


def _bool_from_suite(suite: dict[str, Any], key: str, default: bool = False) -> bool:
    value = suite.get(key)
    if value is None:
        return default
    return bool(value)


def _dict_from_suite(suite: dict[str, Any], key: str) -> dict[str, Any]:
    value = suite.get(key)
    return value if isinstance(value, dict) else {}


def _suite_command_text(suite: dict[str, Any]) -> str:
    command = suite.get("command")
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command or "")


def _suite_live_ai_requested(suite: dict[str, Any]) -> bool:
    if _bool_from_suite(suite, "live_ai"):
        return True
    command_lower = _suite_command_text(suite).lower()
    if (
        "--allow-live-ai" in command_lower
        or "--allow_live_ai" in command_lower
        or "--semantic-live" in command_lower
        or "live-ai-environment-probe" in command_lower
    ):
        return True
    return any(
        isinstance(suite.get(key), dict)
        for key in ("live_ai_environment", "live_ai_environment_probe", "ai_environment_probe")
    )


def _expected_ai_route(suite: dict[str, Any]) -> dict[str, str]:
    env = (
        _dict_from_suite(suite, "live_ai_environment")
        or _dict_from_suite(suite, "live_ai_environment_probe")
        or _dict_from_suite(suite, "ai_environment_probe")
    )
    expected = env.get("expected") if isinstance(env.get("expected"), dict) else {}
    route = env.get("route") if isinstance(env.get("route"), dict) else {}
    return {
        "provider": str(
            expected.get("provider")
            or route.get("provider")
            or env.get("expected_provider")
            or env.get("provider")
            or suite.get("expected_provider")
            or suite.get("provider")
            or ""
        ).strip(),
        "model": str(
            expected.get("model")
            or route.get("model")
            or env.get("expected_model")
            or env.get("model")
            or suite.get("expected_model")
            or suite.get("model")
            or ""
        ).strip(),
        "role": str(
            expected.get("role")
            or route.get("role")
            or env.get("expected_role")
            or env.get("role")
            or suite.get("expected_role")
            or suite.get("role")
            or ""
        ).strip(),
    }


def _suite_route_policy(
    suite: dict[str, Any],
    *,
    project_id: str,
    can_autorun: bool,
) -> dict[str, Any]:
    trigger = _suite_trigger(suite)
    tags = {tag.lower().replace("_", "-") for tag in trigger["tags"]}
    command = _suite_command_text(suite)
    command_lower = command.lower()
    isolation_project = str(suite.get("isolation_project") or "").strip()
    isolation_lower = isolation_project.lower()
    project_lower = str(project_id or "").strip().lower()
    live_ai = _suite_live_ai_requested(suite)
    requires_approval = _bool_from_suite(suite, "requires_human_approval")
    manual_approval_required = live_ai or requires_approval
    mutates_db = _bool_from_suite(suite, "mutates_db", True)
    safety = _dict_from_suite(suite, "safety")
    execution_policy = _dict_from_suite(suite, "execution_policy")
    fixture_only = bool(safety.get("fixture_only"))
    calls_models = safety.get("calls_models", suite.get("calls_models"))
    model_calls_forbidden = (
        str(execution_policy.get("model_calls") or "").lower() == "forbidden"
        or calls_models is False
    )
    execution_lane = str(execution_policy.get("lane") or suite.get("lane") or "").strip().lower()
    structured_output = (
        bool(tags.intersection({"ai-structured-output", "structured-output", "structured-output-fixture"}))
        or "structured-output" in command_lower
        or "structured_output" in command_lower
        or execution_lane == "ai_structured_output_fixture"
    )
    live_ai_env = (
        _dict_from_suite(suite, "live_ai_environment")
        or _dict_from_suite(suite, "live_ai_environment_probe")
        or _dict_from_suite(suite, "ai_environment_probe")
    )
    environment_check = (
        live_ai
        and (
            bool(live_ai_env)
            or bool(tags.intersection({
                "ai-environment",
                "ai-runtime",
                "environment",
                "environment-check",
                "live-ai-environment",
                "probe",
                "readiness",
                "readiness-check",
            }))
            or "live-ai-environment-probe" in command_lower
        )
    )
    allow_live_ai_flag_present = "--allow-live-ai" in command_lower or "--allow_live_ai" in command_lower
    requires_allow_live_ai = environment_check or allow_live_ai_flag_present
    expected_route = _expected_ai_route(suite)

    classes: list[str] = []

    def add(name: str) -> None:
        if name not in classes:
            classes.append(name)

    if "fixture" in tags or "fixture" in isolation_lower or "fixture" in command_lower:
        add("fixture")
    if (
        "docker" in tags
        or isinstance(suite.get("docker_ai_e2e"), dict)
        or command_lower.startswith("docker ")
        or " docker " in f" {command_lower} "
        or "docker/" in command_lower
        or "docker-compose" in command_lower
    ):
        add("docker")
    if live_ai:
        add("live_ai")
    if structured_output:
        add("ai_structured_output")
    if structured_output and (fixture_only or "fixture" in tags or "fixture" in command_lower) and not live_ai:
        add("structured_output_fixture")
    if model_calls_forbidden:
        add("model_calls_forbidden")
    if environment_check:
        add("environment-check")
        add("live_ai_environment")
    if requires_allow_live_ai:
        add("requires_allow_live_ai")
    if allow_live_ai_flag_present:
        add("explicit_allow_live_ai")
    if manual_approval_required:
        add("manual_approval")
    if mutates_db:
        add("mutating_governance")
    static_route = (
        bool(tags.intersection({"static", "static-route"}))
        or "--static-route" in command_lower
        or "--build-dashboard" in command_lower
    )
    if static_route:
        add("static")
    if "production" in tags or (static_route and project_lower and isolation_lower == project_lower):
        add("production")
    if "cleanup" in tags or "cleanup" in command_lower:
        add("cleanup")
    if tags.intersection({"focus", "focused"}) or "--focus" in command_lower or "--focused" in command_lower:
        add("focused")
    if (
        "source-only" in tags
        or "--source-only" in command_lower
        or "--source_only" in command_lower
        or "--skip-dashboard" in command_lower
    ):
        add("source_only")

    if manual_approval_required:
        execution_mode = "manual_approval"
    elif can_autorun:
        execution_mode = "autorun"
    else:
        execution_mode = "manual"
    if execution_mode != "autorun":
        add("manual")

    if environment_check:
        lane = "live_ai_environment"
    elif structured_output and "structured_output_fixture" in classes:
        lane = "structured_output_fixture"
    elif structured_output:
        lane = "ai_structured_output"
    else:
        lane = ""

    return {
        "suite_classes": classes,
        "execution_mode": execution_mode,
        "manual_approval_required": manual_approval_required,
        "ai_evidence_policy": {
            "lane": lane,
            "readiness_check": bool(environment_check),
            "invocation_evidence_required": bool(environment_check and allow_live_ai_flag_present),
            "model_calls_forbidden": bool(model_calls_forbidden),
            "requires_allow_live_ai": bool(requires_allow_live_ai),
            "allow_live_ai_flag_present": bool(allow_live_ai_flag_present),
            "sanitized_evidence_required": bool(environment_check),
            "expected_provider": expected_route["provider"],
            "expected_model": expected_route["model"],
            "expected_role": expected_route["role"],
        },
    }


def _compare_entry_to_snapshot(
    entry: dict[str, Any] | None,
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    if not entry:
        return [{"kind": "missing_evidence"}]
    reasons: list[dict[str, Any]] = []
    for path, expected in (entry.get("covered_files") or {}).items():
        current = _file_evidence(path, state["inventory"])
        if not current["present"]:
            reasons.append({"kind": "file_missing", "path": path})
        elif current["file_hash"] != expected.get("file_hash"):
            reasons.append({
                "kind": "file_hash_changed",
                "path": path,
                "expected": expected.get("file_hash") or "",
                "actual": current["file_hash"],
            })
    for node_id, expected in (entry.get("covered_nodes") or {}).items():
        current = _node_evidence(node_id, state["nodes"], state["inventory"])
        if not current["present"]:
            reasons.append({"kind": "node_missing", "node_id": node_id})
        elif current["feature_hash"] != expected.get("feature_hash"):
            reasons.append({
                "kind": "node_feature_hash_changed",
                "node_id": node_id,
                "expected": expected.get("feature_hash") or "",
                "actual": current["feature_hash"],
            })
    return reasons


def plan_e2e_impact(
    conn: sqlite3.Connection | None,
    project_id: str,
    snapshot_id: str,
    e2e_config: dict[str, Any],
    *,
    changed_files: list[str] | None = None,
    changed_node_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Return stale/missing/current E2E suites for a snapshot."""
    if conn is not None and not store.get_graph_snapshot(conn, project_id, snapshot_id):
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    suites = e2e_config.get("suites") if isinstance(e2e_config.get("suites"), dict) else {}
    latest = _latest_entries(project_id)
    suite_ids = sorted(set(suites) | set(latest))
    state = _snapshot_state(project_id, snapshot_id)
    changed_files = _string_list(changed_files)
    changed_node_ids = _string_list(changed_node_ids)

    rows: list[dict[str, Any]] = []
    counts = {"current": 0, "stale": 0, "missing": 0, "failed": 0, "blocked": 0}
    global_auto = bool(e2e_config.get("auto_run"))
    for suite_id in suite_ids:
        suite = suites.get(suite_id, {})
        if not isinstance(suite, dict):
            suite = {}
        entry = latest.get(suite_id)
        reasons = _compare_entry_to_snapshot(entry, state)
        latest_status = str((entry or {}).get("status") or "").lower()
        if not entry:
            status = "missing"
        elif latest_status and latest_status != "passed":
            status = "failed"
        elif reasons:
            status = "stale"
        else:
            status = "current"
        if status not in counts:
            counts[status] = 0
        counts[status] += 1
        trigger_match = _trigger_matches(suite, changed_files=changed_files, changed_node_ids=changed_node_ids)
        live_ai = _suite_live_ai_requested(suite)
        approval = bool(suite.get("requires_human_approval"))
        auto_run = bool(suite.get("auto_run")) and global_auto and not live_ai and not approval
        route_policy = _suite_route_policy(suite, project_id=project_id, can_autorun=auto_run)
        rows.append({
            "suite_id": suite_id,
            "label": str(suite.get("label") or suite_id),
            "status": status,
            "required": status in {"missing", "stale", "failed"} or trigger_match,
            "trigger_matched": trigger_match,
            "live_ai": live_ai,
            "requires_human_approval": approval,
            "can_autorun": auto_run,
            "blocked_reason": "live_ai_requires_manual_approval" if live_ai or approval else "",
            **route_policy,
            "command": str(suite.get("command") or ""),
            "timeout_sec": int(suite.get("timeout_sec") or e2e_config.get("default_timeout_sec") or 900),
            "latest_evidence": {
                "evidence_id": (entry or {}).get("evidence_id", ""),
                "snapshot_id": (entry or {}).get("snapshot_id", ""),
                "status": (entry or {}).get("status", ""),
                "created_at": (entry or {}).get("created_at", ""),
                "artifact_path": (entry or {}).get("artifact_path", ""),
            },
            "stale_reasons": reasons,
            "trigger": _suite_trigger(suite),
        })

    counts["total"] = len(rows)
    counts["required"] = sum(1 for row in rows if row["required"])
    return {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "summary": counts,
        "suites": rows,
        "ledger_path": str(project_ledger_path(project_id)),
    }
