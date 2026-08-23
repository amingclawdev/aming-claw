#!/usr/bin/env python3
"""Deterministic v3 happy-path evidence replay.

The smoke has two complementary layers:

* HTTP replay reads three independent historical Direct Main, mf_parallel,
  and mf_batch_parallel worlds and proves that each world's close commit,
  immutable graph snapshot, and no-bypass timeline still agree. These are
  compatibility witnesses, not one simultaneous current-release warranty.
* ``--run-regressions`` creates isolated temporary governance worlds through
  the production route handlers and replays the merge/reconcile/close
  authority nodes that made those worlds possible.

No raw role, session, route, worker, or fence credential is accepted by this
script. The default run is read-only against governance. ``--preflight`` is
fully offline and is suitable for image build/install checks.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


RELEASE_VERSION = "0.2.1"
SCHEMA_VERSION = "aming_claw.happy_path_smoke.v3"


@dataclass(frozen=True)
class World:
    lane: str
    project_id: str
    close_commit: str
    snapshot_id: str
    graph_sha256: str
    backlog_ids: tuple[str, ...]


REFERENCE_WORLDS = (
    World(
        lane="direct_main",
        project_id="daily-planner-v2-direct-253d7f62-20260808t200221z",
        close_commit="8a6ef43d2454a8f02586f30380a41d6e16a37210",
        snapshot_id="full-8a6ef43-a390",
        graph_sha256="bd0d18ed960ed26a2e09ca5d7af3a2d09e8881126afcc7c971d4b8568992f0f5",
        backlog_ids=("DP-V2-DIRECT-253D7F62-R1-20260808",),
    ),
    World(
        lane="mf_parallel",
        project_id="daily-planner-v2-parallel-r3-20260809t050248z",
        close_commit="c18af8b3df4971a6beb2c8b07b793dbad1ae6e70",
        snapshot_id="full-c18af8b-2540",
        graph_sha256="bdb607afe83572f0db97516e1c0e961d7c77f0506c90f89d3bd79037e7ae6405",
        backlog_ids=("DP-V2-PARALLEL-R8-20260809",),
    ),
    World(
        lane="mf_batch_parallel",
        project_id="daily-planner-v2-batch-530a9121-20260809t043433z",
        close_commit="170425064c5e8cdb27e7c86c06b9f2ccaf1b72ee",
        snapshot_id="full-1704250-9d45",
        graph_sha256="0e3b24753ff1bd655d850c8da84b17417c311611651ffa42430a6dd2303dbc97",
        backlog_ids=(
            "DP-V2-BATCH-MODELS-530A9121-R2-20260809",
            "DP-V2-BATCH-PLANNER-530A9121-R2-20260809",
            "DP-V2-BATCH-COORD-530A9121-R2-20260809",
        ),
    ),
)


REGRESSION_NODES = (
    "agent/tests/test_graph_governance_api.py::"
    "test_mf_parallel_close_ready_bridges_durable_reconcile_to_descendant_head",
    "agent/tests/test_graph_governance_api.py::"
    "test_mf_batch_final_reconcile_is_shared_child_close_authority",
    "agent/tests/test_graph_governance_api.py::"
    "test_overlapping_fence_batch_children_merge_in_order_through_one_queue",
)


class SmokeFailure(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _base_version(value: object) -> str:
    return str(value or "").split("+", 1)[0]


def offline_preflight(repo_root: Path) -> dict[str, Any]:
    pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    codex = _read_json(repo_root / ".codex-plugin/plugin.json")
    claude = _read_json(repo_root / ".claude-plugin/plugin.json")
    marketplace = _read_json(repo_root / ".claude-plugin/marketplace.json")
    marketplace_version = str(marketplace["plugins"][0]["version"])
    dockerfile = (repo_root / "Dockerfile.governance").read_text(encoding="utf-8")
    dockerignore = (repo_root / ".dockerignore").read_text(encoding="utf-8")
    compose = (repo_root / "docker-compose.governance.yml").read_text(
        encoding="utf-8"
    )
    managed_profile_runtime = (
        repo_root / "agent" / "governance" / "contract_state_runtime.py"
    ).read_text(encoding="utf-8")
    gate_map = _read_json(
        repo_root / "docs/dev/contract-rule-gate-map.happy_path.v1.json"
    )
    map_worlds = gate_map.get("historical_reference_worlds") or []
    map_world_identities = [
        (
            item.get("lane"),
            item.get("project_id"),
            item.get("commit_sha"),
            item.get("snapshot_id"),
            str(item.get("snapshot_sha256") or "").removeprefix("sha256:"),
            tuple(item.get("backlog_ids") or []),
        )
        for item in map_worlds
    ]
    script_world_identities = [
        (
            world.lane,
            world.project_id,
            world.close_commit,
            world.snapshot_id,
            world.graph_sha256,
            world.backlog_ids,
        )
        for world in REFERENCE_WORLDS
    ]
    checks = {
        "pyproject_version": f'version = "{RELEASE_VERSION}"' in pyproject,
        "codex_manifest_version": _base_version(codex.get("version"))
        == RELEASE_VERSION,
        "codex_cachebuster_single": str(codex.get("version") or "").count("+codex.")
        == 1,
        "claude_manifest_version": claude.get("version") == RELEASE_VERSION,
        "claude_marketplace_version": marketplace_version == RELEASE_VERSION,
        "managed_profile_plugin_version": str(codex.get("version") or "")
        in managed_profile_runtime,
        "docker_port": "EXPOSE 40000" in dockerfile
        and "GOVERNANCE_PORT=40000" in dockerfile,
        "docker_healthcheck": "HEALTHCHECK" in dockerfile
        and "/api/health" in dockerfile
        and "runtime_loaded_version" in dockerfile
        and "AMING_CLAW_BUILD_COMMIT" in dockerfile,
        "docker_exact_build_identity": all(
            marker in dockerfile
            for marker in (
                "ARG AMING_CLAW_BUILD_COMMIT",
                "org.opencontainers.image.revision",
                "AMING_CLAW_BUILD_COMMIT=${AMING_CLAW_BUILD_COMMIT}",
            )
        ),
        "docker_redis_extra_installed": 'pip install --no-cache-dir ".[redis]"'
        in dockerfile,
        "docker_context_excludes_local_state": all(
            marker in dockerignore
            for marker in (".git", ".env", "shared-volume", ".venv")
        ),
        "compose_governance_profile": 'profiles: ["governance-demo"]' in compose,
        "compose_healthcheck": "/api/health" in compose,
        "compose_exact_build_identity": "AMING_CLAW_BUILD_COMMIT:" in compose
        and "runtime_loaded_version" in compose,
        "compose_non_conflicting_default_port": "${GOVERNANCE_PORT:-40001}:40000"
        in compose,
        "compose_isolated_volume": "governance-demo-data:/app/shared-volume"
        in compose,
        "gate_map_schema": gate_map.get("schema_version")
        == "aming_claw.contract_rule_gate_map.happy_path.v1",
        "gate_map_world_identity": map_world_identities == script_world_identities,
        "world_count": len(REFERENCE_WORLDS) == 3,
        "worlds_are_independent": all(
            item.get("simultaneous_environment") is False for item in map_worlds
        ),
        "combined_environment_not_claimed": (
            (gate_map.get("release_warranty_policy") or {}).get(
                "combined_environment_claimed"
            )
            is False
        ),
    }
    failures = [name for name, passed in checks.items() if not passed]
    if failures:
        raise SmokeFailure("offline preflight failed: " + ", ".join(failures))
    return {"status": "passed", "checks": checks}


def _http_json(base_url: str, path: str, timeout: float) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "aming-claw-v2-smoke"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SmokeFailure(f"GET {url} failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise SmokeFailure(f"GET {url} returned a non-object JSON value")
    if payload.get("ok") is False:
        raise SmokeFailure(f"GET {url} returned ok=false: {payload.get('error')}")
    return payload


def _formal_no_pass_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        envelope = " ".join(
            str(event.get(key) or "")
            for key in ("event_type", "event_kind", "status", "decision")
        ).lower()
        if (
            "bypass" in envelope
            or "waiv" in envelope
            or payload.get("no_pass_claim") is True
            or payload.get("no_pass_claimed") is True
        ):
            found.append(
                {
                    "id": event.get("id"),
                    "event_type": event.get("event_type"),
                    "event_kind": event.get("event_kind"),
                    "status": event.get("status"),
                }
            )
    return found


def replay_reference_world(
    base_url: str, world: World, timeout: float
) -> dict[str, Any]:
    backlog_evidence: list[dict[str, Any]] = []
    timeline_counts: dict[str, int] = {}
    for backlog_id in world.backlog_ids:
        backlog = _http_json(
            base_url,
            f"/api/backlog/{urllib.parse.quote(world.project_id)}/"
            f"{urllib.parse.quote(backlog_id)}",
            timeout,
        )
        if backlog.get("status") != "FIXED":
            raise SmokeFailure(
                f"{world.lane}:{backlog_id} status={backlog.get('status')!r}, expected FIXED"
            )
        if backlog.get("commit") != world.close_commit:
            raise SmokeFailure(
                f"{world.lane}:{backlog_id} commit={backlog.get('commit')!r}, "
                f"expected {world.close_commit}"
            )
        query = urllib.parse.urlencode({"backlog_id": backlog_id, "limit": 500})
        timeline = _http_json(
            base_url,
            f"/api/task/{urllib.parse.quote(world.project_id)}/timeline?{query}",
            timeout,
        )
        events = timeline.get("events")
        if not isinstance(events, list) or not events:
            raise SmokeFailure(f"{world.lane}:{backlog_id} has no timeline evidence")
        no_pass_events = _formal_no_pass_events(events)
        if no_pass_events:
            raise SmokeFailure(
                f"{world.lane}:{backlog_id} contains formal bypass/waive evidence: "
                f"{no_pass_events}"
            )
        timeline_counts[backlog_id] = len(events)
        backlog_evidence.append(
            {
                "backlog_id": backlog_id,
                "status": backlog.get("status"),
                "commit": backlog.get("commit"),
                "timeline_event_count": len(events),
                "formal_no_pass_event_count": 0,
            }
        )

    snapshot = _http_json(
        base_url,
        f"/api/graph-governance/{urllib.parse.quote(world.project_id)}/"
        f"snapshots/{urllib.parse.quote(world.snapshot_id)}/summary",
        timeout,
    )
    if snapshot.get("snapshot_id") != world.snapshot_id:
        raise SmokeFailure(
            f"{world.lane} snapshot id={snapshot.get('snapshot_id')!r}, "
            f"expected {world.snapshot_id}"
        )
    if snapshot.get("commit_sha") != world.close_commit:
        raise SmokeFailure(
            f"{world.lane} snapshot commit={snapshot.get('commit_sha')!r}, "
            f"expected {world.close_commit}"
        )
    if snapshot.get("graph_sha256") != world.graph_sha256:
        raise SmokeFailure(
            f"{world.lane} graph digest={snapshot.get('graph_sha256')!r}, "
            f"expected {world.graph_sha256}"
        )

    return {
        "lane": world.lane,
        "project_id": world.project_id,
        "close_commit": world.close_commit,
        "backlogs": backlog_evidence,
        "graph_snapshot_id": world.snapshot_id,
        "graph_commit": snapshot.get("commit_sha"),
        "graph_sha256": snapshot.get("graph_sha256"),
        "historical_snapshot_may_be_superseded": True,
        "simultaneous_environment": False,
        "current_release_warranty": False,
        "bypass": False,
    }


def run_route_regressions(repo_root: Path, timeout: int) -> dict[str, Any]:
    command: Sequence[str] = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        *REGRESSION_NODES,
    )
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure(f"route regression timeout after {timeout}s") from exc
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0:
        raise SmokeFailure(
            f"route regression failed with exit {completed.returncode}: {output[-4000:]}"
        )
    return {
        "status": "passed",
        "node_ids": list(REGRESSION_NODES),
        "command": list(command),
        "output": output[-4000:],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:40000",
        help="governance base URL for read-only durable evidence replay",
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="repository root used for offline checks and focused regressions",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="run only offline release artifact checks; make no network requests",
    )
    parser.add_argument(
        "--run-regressions",
        action="store_true",
        help="also replay isolated production-route merge/reconcile/close nodes",
    )
    parser.add_argument(
        "--regression-timeout",
        type=int,
        default=60,
        help="hard timeout for the isolated route replay",
    )
    parser.add_argument("--output", help="optional JSON result path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    try:
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "release_version": RELEASE_VERSION,
            "passed": False,
            "preflight": offline_preflight(repo_root),
            "reference_worlds": [],
            "reference_worlds_are_independent": True,
            "combined_environment_claimed": False,
            "current_release_warranty_claimed": False,
            "route_regressions": {"status": "not_requested"},
            "raw_credentials_required": False,
            "writes_performed_by_http_replay": False,
        }
        if not args.preflight:
            health = _http_json(args.base_url, "/api/health", args.timeout)
            if health.get("status") != "ok":
                raise SmokeFailure(
                    f"governance health status={health.get('status')!r}, expected ok"
                )
            if health.get("runtime_stale") is True:
                raise SmokeFailure("governance reports runtime_stale=true")
            result["health"] = {
                "status": health.get("status"),
                "runtime_loaded_version": health.get("runtime_loaded_version"),
                "runtime_stale": bool(health.get("runtime_stale", False)),
            }
            result["reference_worlds"] = [
                replay_reference_world(args.base_url, world, args.timeout)
                for world in REFERENCE_WORLDS
            ]
        if args.run_regressions:
            result["route_regressions"] = run_route_regressions(
                repo_root, args.regression_timeout
            )
        result["passed"] = True
        rendered = json.dumps(result, indent=2, sort_keys=True)
        if args.output:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0
    except SmokeFailure as exc:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "release_version": RELEASE_VERSION,
            "passed": False,
            "error": str(exc),
            "raw_credentials_exposed": False,
        }
        print(json.dumps(failure, indent=2, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
