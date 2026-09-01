"""CLI entry point for aming-claw.

Usage:
    aming-claw init            - create .aming-claw.yaml in current directory
    aming-claw bootstrap       - bootstrap an external project
    aming-claw status          - show governance status
    aming-claw plugin install  - install/update plugin assets from Git
    aming-claw plugin update   - check/apply plugin updates from Git
    aming-claw backlog export  - export portable backlog JSON
    aming-claw backlog import  - import portable backlog JSON
    aming-claw start           - start governance in the foreground
    aming-claw open            - open the dashboard URL
    aming-claw launcher        - write a local launcher HTML artifact
    aming-claw run-executor    - start executor worker
    aming-claw branch-service validate - validate isolated branch governance
    aming-claw observer run    - build or execute route-bound observer invocation
    aming-claw observer poll   - claim observer command and plan route-bound work
    aming-claw observer dogfood - plan controlled dogfood observer/subagent run
    aming-claw runtime-context current - inspect Runtime Context Service current-state
    aming-claw mf precommit-check - run MF pre-commit guards
    aming-claw mf dispatch-gate - validate MF subagent dispatch evidence
"""

import os
import sys
import logging
import json
import hashlib
import re
import stat
import time
import webbrowser
import socket
import sqlite3
import subprocess
import tempfile
import shutil
import stat
import shlex
import signal
import ctypes
import secrets
import http.client
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional

try:
    import click
except ImportError:
    # Provide a helpful error when click isn't installed
    print("Error: 'click' package is required. Install with: pip install click", file=sys.stderr)
    sys.exit(1)

log = logging.getLogger(__name__)

DEFAULT_GOVERNANCE_URL = "http://localhost:40000"
AC_STABLE_SERVICE_PORT = 40000
AC_DEV_SERVICE_PORT = 40008
AC_DEV_BRANCH = "codex/ac-dev"
AC_STABLE_BRANCH = "codex/direct-no-pass-post-reconcile-r2"
AC_STABLE_ANCHOR_COMMIT = "a25838f15f949ac434cf78e03f20760e82ff81f0"
AC_DATABASE_STABLE_RELATIVE_PATH = (
    "shared-volume/codex-tasks/state/governance/aming-claw/governance.db"
)
AC_DEV_STORAGE_ROOT_ENV = "AMING_CLAW_DEV_STORAGE_ROOT"
CANONICAL_REF_ADOPTION_ACTION = "canonical_ref_adoption"
CANONICAL_REF_ADOPTION_SCHEMA = "canonical_ref_adoption_route_bound.v1"
_GOVERNANCE_PROBE_HEALTH_BYTES = 64 * 1024
_GOVERNANCE_PROBE_GRAPH_BYTES = 256 * 1024

# Governance keeps bounded SQLite state open for each registered project. 4096
# leaves release-scale descriptor headroom while remaining below ordinary POSIX
# hard limits. This changes only the current foreground process; it never edits
# launchd, systemd, shell, or host-wide resource configuration.
_GOVERNANCE_MIN_NOFILE = 4096

_YAML_TEMPLATE = """\
# aming-claw project configuration
project_id: ""
workspace_path: "."
governance_port: 40000
notification_backend: "telegram"
redis_url: "redis://localhost:6379/0"
max_workers: 4
db_path: ""
"""


def _source_git_identity() -> dict[str, str]:
    """Return the exact source checkout identity used by the CLI process."""

    root = Path(__file__).resolve().parents[1]

    def run(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    try:
        source_sha256 = "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        source_sha256 = ""
    return {
        "root": run("rev-parse", "--show-toplevel") or str(root),
        "branch": run("branch", "--show-current"),
        "commit": run("rev-parse", "HEAD").lower(),
        "tree": run("rev-parse", "HEAD^{tree}").lower(),
        "source_sha256": source_sha256,
        "dirty": run("status", "--porcelain"),
    }


def _stable_start_identity_precheck(
    *,
    port: int,
    requested_anchor: str,
) -> dict[str, str]:
    """Prevent a dev/dirty checkout from impersonating the stable plane."""

    identity = _source_git_identity()
    if port != AC_STABLE_SERVICE_PORT:
        raise click.ClickException(
            f"AC stable runtime is reserved to port {AC_STABLE_SERVICE_PORT}; got {port}."
        )
    if identity["branch"] != AC_STABLE_BRANCH:
        raise click.ClickException(
            f"AC stable runtime requires branch {AC_STABLE_BRANCH}; "
            f"got {identity['branch'] or 'unknown'}."
        )
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", identity["commit"]):
        raise click.ClickException("AC stable runtime requires an exact Git HEAD commit.")
    if identity["dirty"]:
        raise click.ClickException("AC stable runtime requires a clean frozen worktree.")
    anchor = str(requested_anchor or identity["commit"]).strip().lower()
    if anchor != identity["commit"]:
        raise click.ClickException(
            "AC stable runtime anchor must equal the exact frozen branch HEAD."
        )
    return {**identity, "stable_anchor_commit": anchor}


def _stable_running_identity_matches(
    health: Mapping[str, Any],
    expected: Mapping[str, str],
) -> bool:
    identity = health.get("runtime_plane_identity")
    if not isinstance(identity, Mapping):
        return False
    commit = str(expected.get("commit") or "")
    return bool(
        health.get("runtime_plane") == "stable"
        and health.get("port") == AC_STABLE_SERVICE_PORT
        and health.get("runtime_loaded_version") == commit
        and health.get("runtime_stale") is False
        and identity.get("status") == "ready"
        and identity.get("branch") == AC_STABLE_BRANCH
        and identity.get("commit") == commit
        and identity.get("stable_anchor_commit") == commit
    )


def _dev_source_identity_precheck() -> dict[str, str]:
    """Bind a dev start/reuse decision to this exact clean checkout."""

    identity = _source_git_identity()
    try:
        root = str(Path(identity.get("root") or "").resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(
            "AC dev runtime requires an exact physical source worktree."
        ) from exc
    commit = str(identity.get("commit") or "").strip().lower()
    if identity.get("branch") != AC_DEV_BRANCH:
        raise click.ClickException(
            f"AC dev runtime requires branch {AC_DEV_BRANCH}; "
            f"got {identity.get('branch') or 'unknown'}."
        )
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise click.ClickException("AC dev runtime requires an exact Git HEAD commit.")
    if identity.get("dirty"):
        raise click.ClickException("AC dev runtime requires a clean immutable candidate.")
    return {**identity, "root": root, "commit": commit}


def _canonical_ac_stable_shared_volume() -> Path:
    """Locate the one persistent shared volume belonging to the frozen stable branch."""
    source_root = Path(str(_source_git_identity().get("root") or "")).resolve(strict=True)
    result = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=source_root,
        capture_output=True, text=True, timeout=5, check=False)
    roots: list[Path] = []
    if result.returncode == 0:
        for block in result.stdout.strip().split("\n\n"):
            fields = dict(line.split(" ", 1) if " " in line else (line, "") for line in block.splitlines())
            if fields.get("branch") == "refs/heads/" + AC_STABLE_BRANCH and fields.get("worktree"):
                roots.append(Path(fields["worktree"]).resolve(strict=True))
    if len(roots) != 1:
        raise click.ClickException("Exact stable shared-volume identity is unavailable.")
    shared = roots[0] / "shared-volume"
    if not shared.is_dir() or shared.is_symlink() or shared.resolve(strict=True) != shared:
        raise click.ClickException("Canonical stable shared-volume identity is unavailable.")
    return shared


def _dev_running_identity_matches(
    health: Mapping[str, Any],
    expected: Mapping[str, str],
    *,
    stable_anchor_commit: str,
    dev_database_identity: Mapping[str, Any],
) -> bool:
    identity = health.get("runtime_plane_identity")
    loaded_identity = health.get("loaded_runtime_identity")
    if not isinstance(identity, Mapping) or not isinstance(loaded_identity, Mapping):
        return False
    runtime_source_sha256 = str(expected.get("source_sha256") or "")
    try:
        runtime_source = Path(str(expected.get("root") or "")) / "agent" / "governance" / "server.py"
        if runtime_source.is_file():
            runtime_source_sha256 = "sha256:" + hashlib.sha256(runtime_source.read_bytes()).hexdigest()
    except OSError:
        return False
    return bool(
        health.get("runtime_plane") == "dev"
        and health.get("port") == AC_DEV_SERVICE_PORT
        and health.get("bind_host") == "127.0.0.1"
        and type(health.get("pid")) is int
        and int(health.get("pid") or 0) > 0
        and health.get("runtime_loaded_version") == expected.get("commit")
        and health.get("runtime_stale") is False
        and identity.get("schema_version") == "ac_runtime_plane_identity.v1"
        and identity.get("status") == "ready"
        and identity.get("plane") == "dev"
        and identity.get("bind_host") == "127.0.0.1"
        and identity.get("port") == AC_DEV_SERVICE_PORT
        and identity.get("expected_port") == AC_DEV_SERVICE_PORT
        and identity.get("pid") == health.get("pid")
        and identity.get("worktree_root") == expected.get("root")
        and identity.get("branch") == AC_DEV_BRANCH
        and identity.get("expected_branch") == AC_DEV_BRANCH
        and identity.get("commit") == expected.get("commit")
        and identity.get("worktree_dirty") is False
        and identity.get("worktree_dirty_files") == []
        and identity.get("stable_anchor_commit") == stable_anchor_commit
        and dev_database_identity.get("schema_version")
        == "ac_governance_database_identity.v2"
        and dev_database_identity.get("world_id") == "ac-dev"
        and dev_database_identity.get("project_id") == "aming-claw"
        and type(dev_database_identity.get("device")) is int
        and int(dev_database_identity.get("device") or 0) > 0
        and type(dev_database_identity.get("inode")) is int
        and int(dev_database_identity.get("inode") or 0) > 0
        and _exact_sha256(dev_database_identity.get("relative_path_sha256"))
        and _exact_sha256(dev_database_identity.get("genesis_sha256"))
        and identity.get("database_identity") == dict(dev_database_identity)
        and identity.get("world_id") == "ac-dev"
        and loaded_identity.get("schema_version")
        == "governance_loaded_runtime_identity.v1"
        and loaded_identity.get("loaded_commit") == expected.get("commit")
        and loaded_identity.get("loaded_pid") == health.get("pid")
        and _git_commit_identity_matches(
            loaded_identity.get("worktree_head_version"), expected.get("commit")
        )
        and loaded_identity.get("runtime_stale") is False
        and loaded_identity.get("runtime_stale_reasons") == []
        and loaded_identity.get("loaded_source_sha256")
        == runtime_source_sha256
        and loaded_identity.get("worktree_source_sha256")
        == runtime_source_sha256
    )


def _canonical_dev_database_binding(
    storage_root: str,
    *,
    source_identity: Mapping[str, Any],
    linked_v3_receipt: Path | None = None,
) -> dict[str, Any]:
    """Bootstrap or verify one source-only, physically separate dev world."""

    if not str(storage_root or "").strip():
        raise click.ClickException("AC dev runtime requires --dev-storage-root")
    root_input = Path(storage_root).expanduser().absolute()
    if root_input.is_symlink():
        raise click.ClickException("AC dev storage root cannot be a symlink")
    stable_shared = os.environ.get("SHARED_VOLUME_PATH", "").strip()
    if stable_shared:
        try:
            if root_input.resolve() == Path(stable_shared).expanduser().resolve():
                raise click.ClickException(
                    "AC dev storage root must be physically separate from stable"
                )
        except OSError as exc:
            raise click.ClickException("AC dev storage identity is unavailable") from exc
    from agent.governance.db import bootstrap_dev_governance_store

    try:
        receipt = bootstrap_dev_governance_store(
            root_input,
            source_identity=source_identity,
            process_identity={
                "pid": os.getpid(),
                "start_identity": f"pid:{os.getpid()}:cli-bootstrap",
            },
            linked_v3_receipt=linked_v3_receipt,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    return {
        "dev_storage_root": str(root_input),
        "database_path": str(receipt["database_path"]),
        "dev_database_identity": dict(receipt["database_identity"]),
        "genesis_sha256": str(receipt["genesis_sha256"]),
        "source_only": receipt.get("source_only") is True,
        "rows_copied": int(receipt.get("rows_copied") or 0),
    }


def _require_dev_cutover_activation(
    storage_root: str,
    *,
    source_identity: Mapping[str, Any],
    database_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Require the operator's atomic cutover marker before port 40008 starts."""

    from agent.governance.db import validate_dev_world_cutover_activation

    try:
        return validate_dev_world_cutover_activation(
            storage_root=storage_root,
            expected_dev_database_identity=database_identity,
            source_identity=source_identity,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise click.ClickException(
            "AC dev runtime requires an active verified dev-cutover checkpoint: "
            + str(exc)
        ) from exc


def _canonical_stable_database_binding(
    requested_shared_volume: str,
    *,
    stable_anchor_commit: str,
) -> dict[str, Any]:
    """CLI error adapter for the sole DB-layer stable binding predicate."""
    from agent.governance.db import verified_stable_database_binding
    try:
        binding = verified_stable_database_binding(
            requested_shared_volume, stable_anchor_commit=stable_anchor_commit
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(
            "AC dev runtime canonical stable database identity is unavailable."
        ) from exc
    return {
        "shared_volume_path": str(binding["shared_volume_path"]),
        "stable_database_identity": dict(binding["stable_database_identity"]),
    }


def _exact_sha256(value: Any) -> bool:
    return bool(re.fullmatch(r"sha256:[0-9a-f]{64}", str(value or "")))


def _git_commit_identity_matches(left: Any, right: Any) -> bool:
    left_value = str(left or "").strip().lower()
    right_value = str(right or "").strip().lower()
    return bool(
        re.fullmatch(r"[0-9a-f]{7,64}", left_value)
        and re.fullmatch(r"[0-9a-f]{7,64}", right_value)
        and (
            left_value == right_value
            or left_value.startswith(right_value)
            or right_value.startswith(left_value)
        )
    )


def _verified_generic_health_identity(
    health: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the immutable part of the temporary generic stable authority."""

    loaded = str(health.get("runtime_loaded_version") or "").strip().lower()
    identity = health.get("runtime_plane_identity")
    loaded_identity = health.get("loaded_runtime_identity")
    if not (
        health.get("status") == "ok"
        and health.get("service") == "governance"
        and health.get("port") == AC_STABLE_SERVICE_PORT
        and health.get("runtime_plane") == "generic"
        and health.get("runtime_stale") is False
        and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", loaded)
        and type(health.get("pid")) is int
        and int(health.get("pid") or 0) > 0
        and isinstance(identity, Mapping)
        and identity.get("schema_version") == "ac_runtime_plane_identity.v1"
        and identity.get("status") == "ready"
        and identity.get("plane") == "generic"
        and identity.get("bind_host") == "0.0.0.0"
        and identity.get("port") == AC_STABLE_SERVICE_PORT
        and identity.get("expected_port") == AC_STABLE_SERVICE_PORT
        and identity.get("pid") == health.get("pid")
        and identity.get("branch") == AC_STABLE_BRANCH
        and identity.get("expected_branch") == AC_STABLE_BRANCH
        and identity.get("commit") == loaded
        and identity.get("worktree_dirty") is False
        and identity.get("worktree_dirty_files") == []
        and isinstance(loaded_identity, Mapping)
        and loaded_identity.get("schema_version")
        == "governance_loaded_runtime_identity.v1"
        and loaded_identity.get("loaded_commit") == loaded
        and loaded_identity.get("loaded_pid") == health.get("pid")
        and _git_commit_identity_matches(
            loaded_identity.get("worktree_head_version"), loaded
        )
        and loaded_identity.get("runtime_stale") is False
        and loaded_identity.get("runtime_stale_reasons") == []
        and _exact_sha256(loaded_identity.get("loaded_source_sha256"))
        and loaded_identity.get("loaded_source_sha256")
        == loaded_identity.get("worktree_source_sha256")
    ):
        return {}
    return {
        "commit": loaded,
        "pid": int(health["pid"]),
        "worktree_root": str(identity.get("worktree_root") or ""),
        "database_identity": (
            dict(identity.get("stable_database_identity"))
            if isinstance(identity.get("stable_database_identity"), Mapping)
            else {}
        ),
    }


def _verified_generic_graph_identity(
    graph_status: Mapping[str, Any],
    *,
    loaded_commit: str,
) -> dict[str, Any]:
    current_state = (
        graph_status.get("current_state")
        if isinstance(graph_status.get("current_state"), Mapping)
        else {}
    )
    graph_stale = (
        current_state.get("graph_stale")
        if isinstance(current_state.get("graph_stale"), Mapping)
        else {}
    )
    snapshot_id = str(graph_status.get("active_snapshot_id") or "").strip()
    graph_commit = str(graph_status.get("graph_snapshot_commit") or "").strip().lower()
    materialized_commit = str(
        graph_status.get("materialized_graph_baseline_commit") or ""
    ).strip().lower()
    if not (
        graph_status.get("ok") is True
        and graph_status.get("project_id") == "aming-claw"
        and snapshot_id
        and graph_commit == loaded_commit
        and materialized_commit == loaded_commit
        and graph_stale.get("is_stale") is False
        and graph_stale.get("head_commit") == loaded_commit
        and graph_stale.get("active_graph_commit") == loaded_commit
    ):
        return {}
    return {
        "active_snapshot_id": snapshot_id,
        "graph_snapshot_commit": graph_commit,
        "materialized_graph_baseline_commit": materialized_commit,
    }


def _verified_generic_stable_authority(
    health: Mapping[str, Any],
) -> dict[str, Any]:
    """Accept generic 40000 only when source, Git, graph, and port agree."""

    base = _verified_generic_health_identity(health)
    if not base or _port_is_open(AC_DEV_SERVICE_PORT):
        return {}
    try:
        expected_root = Path(base["worktree_root"]).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return {}
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=expected_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    roots: list[Path] = []
    if proc.returncode == 0:
        for block in proc.stdout.strip().split("\n\n"):
            values = dict(
                line.split(" ", 1) if " " in line else (line, "")
                for line in block.splitlines()
            )
            if (
                values.get("branch") == "refs/heads/" + AC_STABLE_BRANCH
                and values.get("worktree")
            ):
                try:
                    roots.append(Path(values["worktree"]).resolve(strict=True))
                except (OSError, RuntimeError, ValueError):
                    return {}
    if len(roots) != 1 or roots[0] != expected_root:
        return {}

    def git_output(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=expected_root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    if (
        git_output("branch", "--show-current") != AC_STABLE_BRANCH
        or git_output("rev-parse", "HEAD").lower() != base["commit"]
        or git_output("status", "--porcelain")
    ):
        return {}
    graph_status = _probe_governance_path(
        AC_STABLE_SERVICE_PORT,
        "/api/graph-governance/aming-claw/status",
    )
    graph = _verified_generic_graph_identity(
        graph_status or {},
        loaded_commit=base["commit"],
    )
    if not graph:
        return {}
    return {
        **base,
        "mode": "verified_generic",
        "health": dict(health),
        "graph": graph,
        "stable_branch_root": str(expected_root),
    }


def _current_stable_runtime_authority() -> dict[str, Any]:
    health = _probe_governance(AC_STABLE_SERVICE_PORT) or {}
    loaded = str(health.get("runtime_loaded_version") or "").strip().lower()
    if not (
        health.get("status") == "ok"
        and health.get("service") == "governance"
        and health.get("port") == AC_STABLE_SERVICE_PORT
        and health.get("runtime_stale") is False
        and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", loaded)
    ):
        return {}
    identity = health.get("runtime_plane_identity")
    if isinstance(identity, Mapping) and identity:
        if health.get("runtime_plane") == "generic":
            return _verified_generic_stable_authority(health)
        if not (
            health.get("runtime_plane") == "stable"
            and identity.get("status") == "ready"
            and identity.get("branch") == AC_STABLE_BRANCH
            and identity.get("commit") == loaded
            and identity.get("stable_anchor_commit") == loaded
        ):
            return {}
        return {"commit": loaded, "mode": "explicit_stable", "health": dict(health)}
    if loaded == AC_STABLE_ANCHOR_COMMIT:
        return {"commit": loaded, "mode": "legacy_a258", "health": dict(health)}
    return {}


def _current_stable_anchor_commit() -> str:
    """Resolve the exact currently loaded stable commit, never a stale default."""

    authority = _current_stable_runtime_authority()
    if not authority:
        raise click.ClickException(
            "AC dev runtime requires exact stable or verified-generic authority "
            "on port 40000."
        )
    return str(authority["commit"])


def _local_stable_source_anchor() -> str:
    """Resolve the frozen stable source ref without contacting port 40000."""

    identity = _source_git_identity()
    root = Path(str(identity.get("root") or "")).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{AC_STABLE_BRANCH}"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise click.ClickException("Local stable source anchor is unavailable.") from exc
    commit = result.stdout.strip().lower() if result.returncode == 0 else ""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise click.ClickException("Local stable source anchor is unavailable.")
    return commit


def _run_dev_governance() -> None:
    """Enter the guarded server module without the legacy startup wrapper."""

    from agent.governance.server import main as governance_main

    governance_main()


def _git_checked(root: Path, *args: str) -> str:
    """Run a bounded Git read/write operation, returning stripped stdout."""

    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True,
            timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise click.ClickException("canonical dev adoption Git invocation failed") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise click.ClickException(detail or "canonical dev adoption Git operation failed")
    return result.stdout.strip()


def _sha256_json(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _worktree_adoption_snapshot(root: Path, ref: str) -> dict[str, str]:
    """Produce a non-mutating, content-addressed Git state snapshot."""

    status = _git_checked(root, "status", "--porcelain=v1", "-z")
    head = _git_checked(root, "rev-parse", "HEAD").lower()
    tree = _git_checked(root, "rev-parse", "HEAD^{tree}").lower()
    index = _git_checked(root, "ls-files", "--stage", "-z")
    ref_value = _git_checked(root, "rev-parse", "--verify", ref).lower()
    return {
        "head": head,
        "tree": tree,
        "ref": ref_value,
        "ref_sha256": _sha256_json({"ref": ref, "value": ref_value}),
        "index_sha256": "sha256:" + hashlib.sha256(index.encode("utf-8")).hexdigest(),
        "worktree_sha256": "sha256:" + hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "worktree_clean": str(not status).lower(),
    }


def _adoption_worktrees(repo_root: Path, branch: str) -> list[Path]:
    output = _git_checked(repo_root, "worktree", "list", "--porcelain")
    roots: list[Path] = []
    for block in output.split("\n\n"):
        values = dict(
            line.split(" ", 1) if " " in line else (line, "")
            for line in block.splitlines()
        )
        if values.get("branch") == "refs/heads/" + branch and values.get("worktree"):
            try:
                roots.append(Path(values["worktree"]).resolve(strict=True))
            except (OSError, RuntimeError, ValueError) as exc:
                raise click.ClickException("canonical dev adoption worktree identity is unavailable") from exc
    return roots


def _require_dead_dev_pid(pid_path: Path | None) -> None:
    """Reject a live persisted dev PID; stale records are evidence, not holders."""

    if pid_path is None or not pid_path.is_file():
        return
    try:
        payload = json.loads(pid_path.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise click.ClickException("canonical dev adoption PID record is unreadable") from exc
    if pid <= 0:
        raise click.ClickException("canonical dev adoption PID record is invalid")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise click.ClickException("canonical dev adoption cannot verify dev PID ownership") from exc
    raise click.ClickException("canonical dev adoption refuses a live dev runtime holder")


def _validate_resume_receipt(
    receipt_path: Path,
    *,
    expected_commit: str,
    target_commit: str,
    route_bound_intent: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException("canonical dev adoption resume receipt is unreadable") from exc
    if not isinstance(receipt, dict):
        raise click.ClickException("canonical dev adoption resume receipt is invalid")
    receipt_hash = receipt.pop("receipt_sha256", "")
    if receipt_hash != _sha256_json(receipt):
        raise click.ClickException("canonical dev adoption resume receipt hash mismatch")
    if not (
        receipt.get("schema_version") == "ac_dev_canonical_ref_adoption.v2"
        and receipt.get("expected_commit") == expected_commit
        and receipt.get("target_commit") == target_commit
        and receipt.get("stage") in {"detached", "cas", "attached"}
        and isinstance(receipt.get("post"), Mapping)
        and isinstance(receipt.get("stable"), Mapping)
    ):
        raise click.ClickException("canonical dev adoption resume receipt does not prove an exact stage")
    if route_bound_intent is not None and receipt.get("route_bound_intent") != dict(route_bound_intent):
        raise click.ClickException("canonical dev adoption resume receipt intent mismatch")
    return {**receipt, "receipt_sha256": receipt_hash}


def _read_regular_json(path: Path, *, label: str) -> dict[str, Any]:
    """Read an operator-supplied evidence file without accepting links."""

    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise OSError("not a regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"canonical dev adoption {label} is unreadable") from exc
    if not isinstance(value, dict):
        raise click.ClickException(f"canonical dev adoption {label} is invalid")
    return value


def _route_bound_adoption_intent(
    *, route_token_path: Path, route_intent_path: Path, dev_database: Path
) -> dict[str, str]:
    """Resolve an adoption intent only from a server-issued route-registry row.

    The file presented by an operator is merely a locator.  Its fields are
    compared byte-for-byte with the registry lineage after the full token has
    passed the salted-digest verifier; it can therefore not become a
    self-authored authorization manifest.
    """

    from agent.governance import observer_route_context as route_context

    token = _read_regular_json(route_token_path, label="route token")
    supplied = _read_regular_json(route_intent_path, label="route intent")
    if supplied.get("schema_version") != CANONICAL_REF_ADOPTION_SCHEMA:
        raise click.ClickException("canonical dev adoption intent schema is invalid")
    route_token_ref = str(supplied.get("route_token_ref") or "").strip()
    payload = supplied.get("canonical_ref_adoption")
    if not route_token_ref or not isinstance(payload, Mapping):
        raise click.ClickException("canonical dev adoption intent is incomplete")
    try:
        if not stat.S_ISREG(dev_database.lstat().st_mode):
            raise OSError("not a regular database")
        conn = sqlite3.connect(f"file:{dev_database}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error) as exc:
        raise click.ClickException("canonical dev adoption route registry is unavailable") from exc
    try:
        binding = route_context.verify_route_token_binding(
            conn, project_id="aming-claw", token=token,
            route_token_ref=route_token_ref,
            backlog_id=str(payload.get("backlog_id") or ""),
            task_id=str(payload.get("contract_execution_id") or ""),
        )
        if CANONICAL_REF_ADOPTION_ACTION not in set(binding.get("allowed_actions") or []):
            raise click.ClickException("canonical dev adoption route action is not authorized")
        row = conn.execute(
            "SELECT route_lineage_json FROM observer_route_token_refs "
            "WHERE project_id=? AND route_token_ref=? AND status='active'",
            ("aming-claw", route_token_ref),
        ).fetchone()
    except route_context.RouteTokenRefError as exc:
        raise click.ClickException("canonical dev adoption route binding rejected") from exc
    except sqlite3.Error as exc:
        raise click.ClickException("canonical dev adoption route registry is unavailable") from exc
    finally:
        conn.close()
    if row is None:
        raise click.ClickException("canonical dev adoption route intent is not server-issued")
    try:
        lineage = json.loads(str(row["route_lineage_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException("canonical dev adoption route lineage is invalid") from exc
    stored = lineage.get("canonical_ref_adoption") if isinstance(lineage, Mapping) else None
    if not isinstance(stored, Mapping) or dict(stored) != dict(payload):
        raise click.ClickException("canonical dev adoption rejects forged or self-authored intent")
    required = (
        "project_id", "backlog_id", "action", "contract_execution_id", "generation",
        "custody", "canonical_ref", "expected_commit", "target_commit", "target_tree",
        "source_content_sha256", "qa_content_sha256", "issued_at", "expires_at",
        "replay_identity",
    )
    if any(not str(payload.get(key) or "").strip() for key in required):
        raise click.ClickException("canonical dev adoption route intent is incomplete")
    if (
        payload.get("project_id") != "aming-claw"
        or payload.get("action") != CANONICAL_REF_ADOPTION_ACTION
        or payload.get("canonical_ref") != "refs/heads/codex/ac-dev"
        or any(not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(payload.get(key) or ""))
               for key in ("expected_commit", "target_commit", "target_tree"))
        or any(not _exact_sha256(str(payload.get(key) or ""))
               for key in ("source_content_sha256", "qa_content_sha256"))
    ):
        raise click.ClickException("canonical dev adoption route intent does not bind an exact target")
    return {
        **{key: str(payload[key]) for key in required},
        "route_token_ref": route_token_ref,
    }


def _canonical_ref_adoption_lifecycle(
    *, dev_database: Path, route_token_path: Path, intent: Mapping[str, str],
    prior_receipt: Mapping[str, Any] | None, phase: str, result_receipt_sha256: str = "",
) -> dict[str, Any]:
    """Delegate adoption lifecycle mutation/verification to its route helper.

    This CLI intentionally owns no SQL and never interprets the nested state.
    It only supplies the phase receipt hashes produced by the Git operation.
    """
    from agent.governance import observer_route_context as route_context

    token = _read_regular_json(route_token_path, label="route token")
    route_token_ref = str(intent["route_token_ref"])
    previous = str(
        (prior_receipt or {}).get("receipt_sha256")
        or route_context._sha256(dict(intent))
    )
    try:
        conn = sqlite3.connect(str(dev_database))
        conn.row_factory = sqlite3.Row
        if result_receipt_sha256:
            return route_context.canonical_ref_adoption_advance(
                conn,
                project_id="aming-claw",
                route_token_ref=route_token_ref,
                token=token,
                replay_identity=intent["replay_identity"],
                previous_receipt_sha256=previous,
                next_phase={"detach": "detached", "cas": "cas", "attach": "attached"}[phase],
                receipt_sha256=result_receipt_sha256,
            )
        if phase == "detach":
            state = route_context.canonical_ref_adoption_reserve(
                conn,
                project_id="aming-claw",
                route_token_ref=route_token_ref,
                token=token,
                replay_identity=intent["replay_identity"],
                initial_receipt_sha256=previous,
            )
            return route_context.canonical_ref_adoption_resume(
                conn,
                project_id="aming-claw",
                route_token_ref=route_token_ref,
                token=token,
                replay_identity=intent["replay_identity"],
                previous_receipt_sha256=previous,
                expected_phase=str(state["phase"]),
            )
        expected = {"cas": "detached", "attach": "cas"}[phase]
        return route_context.canonical_ref_adoption_resume(
            conn,
            project_id="aming-claw",
            route_token_ref=route_token_ref,
            token=token,
            replay_identity=intent["replay_identity"],
            previous_receipt_sha256=previous,
            expected_phase=expected,
        )
    except (OSError, sqlite3.Error, route_context.RouteTokenRefError) as exc:
        raise click.ClickException("canonical dev adoption lifecycle rejected: " + str(exc)) from exc
    finally:
        try:
            conn.close()
        except UnboundLocalError:
            pass


def _require_regular_resume_receipt(
    _ctx: click.Context, param: click.Parameter, value: Path | None
) -> Path | None:
    """Keep an explicitly supplied resume receipt a real, non-link file."""

    if value is None:
        return None
    try:
        mode = value.lstat().st_mode
    except OSError as exc:
        raise click.BadParameter("must be an existing regular receipt file", param=param) from exc
    if not stat.S_ISREG(mode):
        raise click.BadParameter("must be an existing regular receipt file", param=param)
    return value


def _stable_process_identity(pid: int) -> dict[str, str]:
    """Read the stable process start/command/cwd tuple without mutating it."""

    try:
        process = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
            capture_output=True, text=True, timeout=5, check=False,
        )
        cwd = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise click.ClickException("canonical dev adoption stable process identity is unavailable") from exc
    line = process.stdout.strip()
    cwd_line = next((entry[1:] for entry in cwd.stdout.splitlines() if entry.startswith("n")), "")
    if process.returncode or not line or not cwd_line:
        raise click.ClickException("canonical dev adoption stable process identity is unavailable")
    # ps emits a fixed-width date followed by the command.  The exact string is
    # retained in the receipt rather than inferred from a mutable process name.
    return {"start": line[:24].strip(), "command": line[24:].strip(), "cwd": cwd_line}


def _stable_adoption_identity(stable_root: Path) -> dict[str, Any]:
    """Bind adoption to stable Git, loaded runtime, process, and DB identity."""

    health = _probe_governance(AC_STABLE_SERVICE_PORT) or {}
    identity = health.get("runtime_plane_identity")
    loaded = health.get("loaded_runtime_identity")
    stable = _worktree_adoption_snapshot(stable_root, "HEAD")
    pid = health.get("pid")
    if not (
        health.get("status") == "ok"
        and health.get("service") == "governance"
        and health.get("port") == AC_STABLE_SERVICE_PORT
        and health.get("runtime_plane") == "stable"
        and health.get("runtime_loaded_version") == stable["head"]
        and health.get("runtime_stale") is False
        and type(pid) is int and pid > 0
        and isinstance(identity, Mapping)
        and identity.get("worktree_root") == str(stable_root)
        and identity.get("branch") == AC_STABLE_BRANCH
        and identity.get("commit") == stable["head"]
        and identity.get("worktree_dirty") is False
        and isinstance(identity.get("stable_database_identity"), Mapping)
        and isinstance(loaded, Mapping)
        and loaded.get("loaded_commit") == stable["head"]
        and loaded.get("loaded_pid") == pid
        and loaded.get("runtime_stale") is False
        and loaded.get("loaded_source_sha256") == loaded.get("worktree_source_sha256")
        and _exact_sha256(loaded.get("loaded_source_sha256"))
    ):
        raise click.ClickException("canonical dev adoption stable runtime identity mismatch")
    process = _stable_process_identity(pid)
    if not all(process.values()):
        raise click.ClickException("canonical dev adoption stable process identity mismatch")
    return {
        "commit": stable["head"], "tree": stable["tree"], "port": AC_STABLE_SERVICE_PORT,
        "pid": pid, "loaded_source_sha256": loaded["loaded_source_sha256"],
        "process_start": process["start"], "process_command": process["command"],
        "process_cwd": process["cwd"],
        "database_identity": dict(identity["stable_database_identity"]),
        "git": stable,
    }


def _adopt_canonical_dev_ref(
    *,
    candidate_worktree: Path,
    canonical_worktree: Path,
    expected_commit: str,
    target_commit: str,
    target_tree: str,
    pid_path: Path | None = None,
    resume_receipt: Mapping[str, Any] | None = None,
    phase: str = "run",
    stable_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one interruption-safe detach/CAS/attach adoption stage."""

    candidate = candidate_worktree.resolve(strict=True)
    canonical = canonical_worktree.resolve(strict=True)
    if candidate == canonical:
        raise click.ClickException("candidate and canonical dev worktrees must differ")
    candidate_snapshot = _worktree_adoption_snapshot(candidate, "HEAD")
    if (
        candidate_snapshot["head"] != target_commit
        or candidate_snapshot["tree"] != target_tree
        or candidate_snapshot["worktree_clean"] != "true"
    ):
        raise click.ClickException("canonical dev adoption candidate does not match exact target/tree/clean state")
    if _git_checked(candidate, "branch", "--show-current") == AC_DEV_BRANCH:
        raise click.ClickException("canonical dev adoption candidate must not already hold codex/ac-dev")
    # ``git merge-base --is-ancestor`` communicates success solely by status;
    # _git_checked turns its non-zero status into the required fail-closed error.
    _git_checked(candidate, "merge-base", "--is-ancestor", expected_commit, target_commit)
    dev_roots = _adoption_worktrees(candidate, AC_DEV_BRANCH)
    allowed_dev_roots = (
        ([canonical], []) if phase == "detach" else
        ([],) if phase == "cas" else
        ([], [canonical])
    )
    if dev_roots not in allowed_dev_roots:
        raise click.ClickException("canonical dev adoption requires one exact codex/ac-dev worktree")
    stable_roots = _adoption_worktrees(candidate, AC_STABLE_BRANCH)
    if len(stable_roots) != 1 or stable_roots[0] in {candidate, canonical}:
        raise click.ClickException("canonical dev adoption detects a stable worktree conflict")
    stable_snapshot = _worktree_adoption_snapshot(stable_roots[0], "HEAD")
    if stable_snapshot["worktree_clean"] != "true":
        raise click.ClickException("canonical dev adoption refuses a dirty stable worktree")
    current_stable = dict(stable_identity or _stable_adoption_identity(stable_roots[0]))
    if not current_stable:
        raise click.ClickException("canonical dev adoption stable identity is unavailable")
    if resume_receipt is not None and resume_receipt.get("stable") != current_stable:
        raise click.ClickException("canonical dev adoption stable identity changed during resume")
    canonical_snapshot = _worktree_adoption_snapshot(canonical, "refs/heads/" + AC_DEV_BRANCH)
    if _port_is_open(AC_DEV_SERVICE_PORT):
        raise click.ClickException("canonical dev adoption refuses an occupied dev service port")
    _require_dead_dev_pid(pid_path)
    branch = _git_checked(canonical, "branch", "--show-current")
    if phase not in {"detach", "cas", "attach"}:
        raise click.ClickException("canonical dev adoption phase must be detach, cas, or attach")
    required_stages = {"detach": set(), "cas": {"detached", "cas"}, "attach": {"cas", "attached"}}[phase]
    if required_stages:
        if resume_receipt is None or resume_receipt.get("stage") not in required_stages:
            raise click.ClickException("canonical dev adoption requires the prior exact stage receipt")
    if phase == "detach":
        if branch == "" and canonical_snapshot["head"] == expected_commit and canonical_snapshot["ref"] == expected_commit:
            if resume_receipt is None or resume_receipt.get("stage") != "detached":
                raise click.ClickException("canonical dev adoption detached state requires its exact receipt")
            post = canonical_snapshot
            status = "idempotent_detached"
        elif (
            branch == AC_DEV_BRANCH and canonical_snapshot["head"] == expected_commit
            and canonical_snapshot["ref"] == expected_commit and canonical_snapshot["worktree_clean"] == "true"
        ):
            _git_checked(canonical, "checkout", "--detach", expected_commit)
            post = _worktree_adoption_snapshot(canonical, "refs/heads/" + AC_DEV_BRANCH)
            status = "detached"
        else:
            raise click.ClickException("canonical dev adoption detach precondition failed before Git effects")
    elif phase == "cas":
        if branch != "" or canonical_snapshot["head"] != expected_commit or canonical_snapshot["worktree_clean"] != "true":
            raise click.ClickException("canonical dev adoption CAS requires clean detached expected worktree")
        if canonical_snapshot["ref"] == target_commit:
            post = canonical_snapshot
            status = "idempotent_cas"
        elif canonical_snapshot["ref"] == expected_commit:
            _git_checked(canonical, "update-ref", "refs/heads/" + AC_DEV_BRANCH, target_commit, expected_commit)
            post = _worktree_adoption_snapshot(canonical, "refs/heads/" + AC_DEV_BRANCH)
            if post["ref"] != target_commit or post["head"] != expected_commit:
                raise click.ClickException("canonical dev adoption CAS postcondition failed; stop for audit")
            status = "cas"
        else:
            raise click.ClickException("canonical dev adoption CAS ref drift before Git effects")
    else:
        if branch == AC_DEV_BRANCH and canonical_snapshot["head"] == target_commit and canonical_snapshot["ref"] == target_commit:
            post = canonical_snapshot
            status = "idempotent_attached"
        elif (
            branch == "" and canonical_snapshot["head"] == expected_commit
            and canonical_snapshot["ref"] == target_commit and canonical_snapshot["worktree_clean"] == "true"
        ):
            _git_checked(canonical, "checkout", AC_DEV_BRANCH)
            post = _worktree_adoption_snapshot(canonical, "refs/heads/" + AC_DEV_BRANCH)
            if post["head"] != target_commit or post["tree"] != target_tree or post["worktree_clean"] != "true":
                raise click.ClickException("canonical dev adoption attach postcondition failed; stop for audit")
            status = "attached"
        else:
            raise click.ClickException("canonical dev adoption attach precondition failed before Git effects")
    receipt = {
        "schema_version": "ac_dev_canonical_ref_adoption.v2",
        "stage": {"detach": "detached", "cas": "cas", "attach": "attached"}[phase],
        "status": status,
        "expected_commit": expected_commit,
        "target_commit": target_commit,
        "target_tree": target_tree,
        "candidate": candidate_snapshot,
        "stable": current_stable,
        "pre": canonical_snapshot,
        "post": post,
    }
    return {**receipt, "receipt_sha256": _sha256_json(receipt)}


@click.group()
@click.version_option(package_name="aming-claw")
def main():
    """aming-claw - governance-driven workflow platform."""
    pass


@main.command("dev-adopt-canonical")
@click.option(
    "--candidate-worktree", required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Clean QA target worktree at the fixed adoption commit.",
)
@click.option(
    "--canonical-worktree", required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="The sole clean worktree currently holding codex/ac-dev.",
)
@click.option(
    "--dev-pid-file", required=True, type=click.Path(path_type=Path),
    help="Persisted dev PID record; a live PID rejects adoption.",
)
@click.option(
    "--route-token", required=True,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, readable=True, path_type=Path),
    help="Full server-issued route token; verified locally against the dev DB.",
)
@click.option(
    "--route-intent", required=True,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, readable=True, path_type=Path),
    help="Server response intent locator, never a standalone authorization.",
)
@click.option(
    "--dev-database", required=True,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, readable=True, path_type=Path),
    help="Canonical local AC-dev SQLite database containing the route registry.",
)
@click.option(
    "--resume-receipt", default=None,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, readable=True, path_type=Path),
    callback=_require_regular_resume_receipt,
    help="Prior content-addressed receipt, required only for an exact resume.",
)
@click.option(
    "--phase", type=click.Choice(["detach", "cas", "attach"]), default="detach",
    show_default=True, help="One interruption-safe adoption phase per invocation.",
)
def dev_adopt_canonical(
    candidate_worktree: Path,
    canonical_worktree: Path,
    dev_pid_file: Path,
    route_token: Path,
    route_intent: Path,
    dev_database: Path,
    resume_receipt: Path | None,
    phase: str,
):
    """Adopt one server-bound canonical dev ref in receipt-bound phases."""

    intent = _route_bound_adoption_intent(
        route_token_path=route_token,
        route_intent_path=route_intent,
        dev_database=dev_database,
    )

    prior = (
        _validate_resume_receipt(
            resume_receipt,
            expected_commit=intent["expected_commit"],
            target_commit=intent["target_commit"],
            route_bound_intent={key: value for key, value in intent.items() if key != "route_token_ref"},
        )
        if resume_receipt else None
    )
    _canonical_ref_adoption_lifecycle(
        dev_database=dev_database,
        route_token_path=route_token,
        intent=intent,
        prior_receipt=prior,
        phase=phase,
    )

    result = _adopt_canonical_dev_ref(
        candidate_worktree=candidate_worktree,
        canonical_worktree=canonical_worktree,
        expected_commit=intent["expected_commit"],
        target_commit=intent["target_commit"],
        target_tree=intent["target_tree"],
        pid_path=dev_pid_file,
        resume_receipt=prior,
        phase=phase,
    )
    result["route_bound_intent"] = {
        key: value for key, value in intent.items() if key != "route_token_ref"
    }
    result["receipt_sha256"] = _sha256_json({key: value for key, value in result.items() if key != "receipt_sha256"})
    _canonical_ref_adoption_lifecycle(
        dev_database=dev_database,
        route_token_path=route_token,
        intent=intent,
        prior_receipt=prior,
        phase=phase,
        result_receipt_sha256=str(result["receipt_sha256"]),
    )
    click.echo(json.dumps(result, indent=2, sort_keys=True))


@main.command()
def init():
    """Initialize project: create .aming-claw.yaml in the current directory."""
    target = os.path.join(os.getcwd(), ".aming-claw.yaml")
    if os.path.exists(target):
        click.echo(f".aming-claw.yaml already exists at {target}")
        return
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(_YAML_TEMPLATE)
    click.echo(f"Created {target}")


@main.command()
@click.option("--path", default=".", help="Workspace path to bootstrap")
@click.option("--name", default="", help="Project name")
@click.option("--project-id", default="", help="Explicit governance project id")
@click.option("--language", default="", help="Project language override")
@click.option(
    "--exclude-path",
    "exclude_paths",
    multiple=True,
    help="Graph exclude path prefix. May be repeated.",
)
@click.option(
    "--ignore-glob",
    "ignore_globs",
    multiple=True,
    help="Graph ignore glob. May be repeated.",
)
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance base URL")
def bootstrap(path, name, project_id, language, exclude_paths, ignore_globs, governance_url):
    """Bootstrap an external project through the governance API."""
    workspace_path = str(Path(path).expanduser().resolve())
    graph_override: dict[str, Any] = {}
    if exclude_paths:
        graph_override["exclude_paths"] = list(exclude_paths)
    if ignore_globs:
        graph_override["ignore_globs"] = list(ignore_globs)

    config_override: dict[str, Any] = {}
    if project_id:
        config_override["project_id"] = project_id
    if language:
        config_override["language"] = language
    if graph_override:
        config_override["graph"] = graph_override

    payload: dict[str, Any] = {
        "workspace_path": workspace_path,
        "project_name": name,
    }
    if project_id:
        payload["project_id"] = project_id
    if language:
        payload["language"] = language
    if exclude_paths:
        payload["exclude_patterns"] = list(exclude_paths)
    if config_override:
        payload["config_override"] = config_override

    url = governance_url.rstrip("/") + "/api/project/bootstrap"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise click.ClickException(f"bootstrap failed ({exc.code}): {body}") from exc
    except urllib.error.URLError as exc:
        raise click.ClickException(
            f"bootstrap failed: governance API is unavailable at {governance_url}: {exc}"
        ) from exc

    click.echo(json.dumps(result, indent=2, sort_keys=True))


@main.command("scan")
@click.option("--path", default=".", help="External project path to scan")
@click.option("--project-id", default="", help="Governance project id")
@click.option("--session-id", default="", help="Optional deterministic scan session id")
def scan(path, project_id, session_id):
    """Scan an external project into a local .aming-claw candidate workspace."""
    from agent.governance.external_project_governance import scan_external_project

    result = scan_external_project(
        path,
        project_id=project_id or None,
        session_id=session_id or None,
    )
    click.echo(json.dumps(result, indent=2, sort_keys=True))


@main.command()
def status():
    """Show governance service status."""
    from agent.config import AmingConfig
    import requests as _requests
    cfg = AmingConfig.load()
    url = f"http://localhost:{cfg.governance_port}/api/health"
    try:
        resp = _requests.get(url, timeout=5)
        click.echo(resp.json())
    except Exception as exc:
        click.echo(f"Governance unreachable: {exc}", err=True)
        sys.exit(1)


_AC_DEV_SCHEMA_ADMISSION_RECEIPT_VERSION = "ac_dev_offline_schema_admission.v2"
_AC_DEV_SCHEMA_RECERTIFICATION_RECEIPT_VERSION = "ac_dev_offline_schema_admission.v3"


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _admission_identity(path: Path) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    details = resolved.stat()
    return {
        "path": str(resolved),
        "device": int(details.st_dev),
        "inode": int(details.st_ino),
    }


def _canonical_dev_database_identity_projection(database: Path) -> dict[str, object]:
    """Derive the full v2 identity carried by durable launch receipts."""
    from agent.governance import db as _db
    physical = _admission_identity(database)
    uri = "file:" + urllib.parse.quote(str(database.absolute())) + "?mode=ro&immutable=1"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0)
        meta = dict(connection.execute(
            "SELECT key, value FROM schema_meta WHERE key IN "
            "('governance_world_id', 'governance_world_genesis_sha256')"
        ))
    except sqlite3.Error as exc:
        raise click.ClickException("AC dev durable database identity is unreadable") from exc
    finally:
        if connection is not None:
            connection.close()
    genesis = str(meta.get("governance_world_genesis_sha256") or "")
    expected = {
        "schema_version": "ac_governance_database_identity.v2",
        "world_id": "ac-dev", "project_id": "aming-claw",
        "device": physical["device"], "inode": physical["inode"],
        "relative_path_sha256": "sha256:" + hashlib.sha256(
            _db.AC_DATABASE_DEV_RELATIVE_PATH.encode("utf-8")
        ).hexdigest(),
        "genesis_sha256": genesis,
    }
    if meta.get("governance_world_id") != "ac-dev" or not _exact_sha256(genesis):
        raise click.ClickException("AC dev durable database identity is invalid")
    return expected


def _matches_canonical_dev_database_identity(database: Path, claimed: object) -> bool:
    """Require every full-v2 field, never compare it to a reduced identity."""
    return isinstance(claimed, Mapping) and dict(claimed) == _canonical_dev_database_identity_projection(database)


def _admission_regular_file(path: Path, *, archive: Path | None = None) -> None:
    try:
        details = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise click.ClickException("AC dev schema admission receipt/file is unreadable") from exc
    if not stat.S_ISREG(details.st_mode) or path.is_symlink() or resolved != path.absolute():
        raise click.ClickException("AC dev schema admission requires a regular nonlink file")
    if archive is not None:
        try:
            resolved.relative_to(archive.resolve(strict=True))
        except ValueError as exc:
            raise click.ClickException("AC dev schema admission receipt is outside its canonical archive") from exc


def _admission_database_sha256(
    database: Path, *, expected_identity: Mapping[str, object],
) -> str:
    """Hash the canonical database through one immutable, bounded descriptor."""
    _admission_regular_file(database)
    if _admission_identity(database) != expected_identity:
        raise click.ClickException("AC dev schema admission database identity changed")
    try:
        descriptor = os.open(database, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise click.ClickException("AC dev schema admission database is unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            int(before.st_dev) != expected_identity.get("device")
            or int(before.st_ino) != expected_identity.get("inode")
        ):
            raise click.ClickException("AC dev schema admission database identity changed")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            int(after.st_dev) != int(before.st_dev)
            or int(after.st_ino) != int(before.st_ino)
            or int(after.st_size) != int(before.st_size)
            or int(after.st_mtime_ns) != int(before.st_mtime_ns)
            or int(after.st_ctime_ns) != int(before.st_ctime_ns)
            or _admission_identity(database) != expected_identity
        ):
            raise click.ClickException("AC dev schema admission database changed during digest")
        return "sha256:" + digest.hexdigest()
    except OSError as exc:
        raise click.ClickException("AC dev schema admission database is unreadable") from exc
    finally:
        os.close(descriptor)


def _admission_sqlite_artifact_identities(database: Path) -> dict[str, object | None]:
    """Capture non-following SQLite identities without accepting replacement."""
    identities: dict[str, object | None] = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(str(database) + suffix)
        try:
            details = path.lstat()
        except FileNotFoundError:
            identities[suffix] = None
            continue
        if not stat.S_ISREG(details.st_mode) or path.is_symlink() or path.resolve(strict=True) != path.absolute():
            raise click.ClickException("AC dev schema admission SQLite artifact identity is invalid")
        identities[suffix] = {
            "device": int(details.st_dev), "inode": int(details.st_ino),
        }
    return identities


def _assert_admission_sqlite_artifacts(
    database: Path, before: Mapping[str, object | None], *, after_close: bool = False,
) -> None:
    after = _admission_sqlite_artifact_identities(database)
    if after.get("") != before.get("") or after.get("-journal") is not None:
        raise click.ClickException("AC dev schema admission SQLite identity changed")
    for suffix in ("-wal", "-shm"):
        old, new = before.get(suffix), after.get(suffix)
        if old == new or (after_close and old is not None and new is None):
            continue
        raise click.ClickException("AC dev schema admission SQLite sidecar identity changed")


def _admission_file_state(path: Path) -> tuple[int, int, int, int, int]:
    details = path.stat(follow_symlinks=False)
    return (
        int(details.st_dev), int(details.st_ino), int(details.st_size),
        int(details.st_mtime_ns), int(details.st_ctime_ns),
    )


def _admission_source_identity(source_tip_raw: str) -> dict[str, object]:
    try:
        source_tip = json.loads(source_tip_raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException("offline AC dev schema admission source-tip is invalid") from exc
    if not isinstance(source_tip, dict):
        raise click.ClickException("offline AC dev schema admission source-tip is invalid")
    cli_source = _source_git_identity()
    return {
        "db_source_tip": source_tip,
        "db_source_tip_sha256": "sha256:" + hashlib.sha256(
            _canonical_json_bytes(source_tip)
        ).hexdigest(),
        "cli_source": cli_source,
        "cli_source_sha256": "sha256:" + hashlib.sha256(
            _canonical_json_bytes(cli_source)
        ).hexdigest(),
    }


def _validated_historical_admission_source_identity(value: object) -> dict[str, object]:
    """Validate a receipt's immutable source binding without rebinding its CLI."""
    if not isinstance(value, dict) or not isinstance(value.get("db_source_tip"), dict):
        raise click.ClickException(
            "AC dev schema admission recertification historical source mismatch"
        )
    db_tip = value["db_source_tip"]
    cli_source = value.get("cli_source")
    db_tip_sha256 = "sha256:" + hashlib.sha256(_canonical_json_bytes(db_tip)).hexdigest()
    cli_sha256 = (
        "sha256:" + hashlib.sha256(_canonical_json_bytes(cli_source)).hexdigest()
        if isinstance(cli_source, dict) else ""
    )
    if (
        value.get("db_source_tip_sha256") != db_tip_sha256
        or value.get("cli_source_sha256") != cli_sha256
        or not isinstance(cli_source, dict)
        or cli_source.get("branch") != AC_DEV_BRANCH
        or cli_source.get("dirty") != ""
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(cli_source.get("commit") or ""))
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(cli_source.get("tree") or ""))
    ):
        raise click.ClickException(
            "AC dev schema admission recertification historical source mismatch"
        )
    return dict(value)


def _validated_authority_receipt_inventory(value: object, *, db_module) -> dict[str, object]:
    """Validate the exact 308-object registry without opening SQLite."""
    if not isinstance(value, dict) or set(value) != {"inventory", "sha256"}:
        raise click.ClickException(
            "AC dev schema admission recertification historical inventory mismatch"
        )
    inventory = value.get("inventory")
    if not isinstance(inventory, list) or len(inventory) != db_module.AC_AUTHORITY_SCHEMA_INVENTORY_COUNT:
        raise click.ClickException(
            "AC dev schema admission recertification historical inventory mismatch"
        )
    encoded = json.dumps(inventory, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    if (
        value.get("sha256") != digest
        or digest != db_module.AC_AUTHORITY_SCHEMA_INVENTORY_SHA256
    ):
        raise click.ClickException(
            "AC dev schema admission recertification historical inventory mismatch"
        )
    return dict(value)


def _write_admission_receipt(archive: Path, payload: dict[str, Any]) -> tuple[Path, str]:
    """Persist a byte-addressed receipt without placing its own hash in it."""
    archive.mkdir(parents=True, exist_ok=True)
    payload_bytes = _canonical_json_bytes(payload)
    digest = hashlib.sha256(payload_bytes).hexdigest()
    receipt_path = archive / f"{digest}.json"
    sidecar = archive / f"{digest}.sha256"
    if receipt_path.exists() or sidecar.exists():
        _admission_regular_file(receipt_path, archive=archive)
        _admission_regular_file(sidecar, archive=archive)
        if receipt_path.read_bytes() != payload_bytes or sidecar.read_text(encoding="utf-8") != f"sha256:{digest}  {receipt_path.name}\n":
            raise click.ClickException("AC dev schema admission receipt address collision")
        return receipt_path, "sha256:" + digest
    temporary = archive / f".{digest}.tmp"
    temporary.write_bytes(payload_bytes)
    os.replace(temporary, receipt_path)
    temporary_sidecar = archive / f".{digest}.sha256.tmp"
    temporary_sidecar.write_text(f"sha256:{digest}  {receipt_path.name}\n", encoding="utf-8")
    os.replace(temporary_sidecar, sidecar)
    return receipt_path, "sha256:" + digest


def _read_admission_receipt(path: Path, *, archive: Path) -> tuple[dict[str, Any], str]:
    _admission_regular_file(path, archive=archive)
    match = re.fullmatch(r"([0-9a-f]{64})\.json", path.name)
    if match is None:
        raise click.ClickException("AC dev schema admission receipt has a noncanonical name")
    digest = match.group(1)
    sidecar = archive / f"{digest}.sha256"
    _admission_regular_file(sidecar, archive=archive)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise click.ClickException("AC dev schema admission receipt content digest mismatch")
    if sidecar.read_text(encoding="utf-8") != f"sha256:{digest}  {path.name}\n":
        raise click.ClickException("AC dev schema admission receipt sidecar mismatch")
    try:
        value = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException("AC dev schema admission receipt is not JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") not in {
        _AC_DEV_SCHEMA_ADMISSION_RECEIPT_VERSION,
        _AC_DEV_SCHEMA_RECERTIFICATION_RECEIPT_VERSION,
    }:
        raise click.ClickException("AC dev schema admission receipt schema mismatch")
    return value, "sha256:" + digest


def _validate_admission_receipt_chain(
    receipt: dict[str, Any], receipt_sha256: str, *, archive: Path,
    project_id: str, port: int, root_identity: dict[str, object],
    database_identity: dict[str, object], source_identity: dict[str, object],
    plan_sha256: str,
) -> None:
    """Validate every byte-addressed predecessor before any DDL is possible."""
    required = {
        "schema_version", "stage", "project_id", "port", "root_identity",
        "database_identity", "source_identity", "plan_sha256",
        "schema_inventory_before", "schema_inventory_after", "backup",
        "database_sha256_before", "database_sha256_after",
        "previous_receipt_sha256", "changed", "missing",
    }
    current = receipt
    current_digest = receipt_sha256
    descendant_before: dict[str, object] | None = None
    descendant_database_before = ""
    descendant: dict[str, Any] | None = None
    while True:
        current_required = set(required)
        if current.get("schema_version") == _AC_DEV_SCHEMA_RECERTIFICATION_RECEIPT_VERSION:
            current_required.add("recertification")
        if set(current) != current_required or current.get("schema_version") not in {
            _AC_DEV_SCHEMA_ADMISSION_RECEIPT_VERSION,
            _AC_DEV_SCHEMA_RECERTIFICATION_RECEIPT_VERSION,
        }:
            raise click.ClickException("AC dev schema admission receipt fields mismatch")
        if (
            current.get("stage") not in {"completed", "rolled_back"}
            or current.get("project_id") != project_id or current.get("port") != port
            or current.get("root_identity") != root_identity
            or current.get("database_identity") != database_identity
            or current.get("source_identity") != source_identity
            or current.get("plan_sha256") != plan_sha256
        ):
            raise click.ClickException("AC dev schema admission receipt binding mismatch")
        backup = current.get("backup")
        if not isinstance(backup, dict) or set(backup) != {"identity", "sha256"}:
            raise click.ClickException("AC dev schema admission receipt backup binding mismatch")
        backup_identity = backup.get("identity")
        if not isinstance(backup_identity, dict) or not isinstance(backup.get("sha256"), str):
            raise click.ClickException("AC dev schema admission receipt backup binding mismatch")
        backup_path = Path(str(backup_identity.get("path") or ""))
        _admission_regular_file(backup_path, archive=archive)
        if _admission_identity(backup_path) != backup_identity or _file_sha256(backup_path) != backup["sha256"]:
            raise click.ClickException("AC dev schema admission receipt backup changed")
        if descendant_before is not None:
            inventory_continuous = current.get("schema_inventory_after") == descendant_before
            database_continuous = current.get("database_sha256_after") == descendant_database_before
            recertification = dict(descendant.get("recertification") or {}) if descendant else {}
            stale_bridge = (
                inventory_continuous and not database_continuous and descendant is not None
                and descendant.get("schema_version") == _AC_DEV_SCHEMA_RECERTIFICATION_RECEIPT_VERSION
                and descendant.get("stage") == "completed" and descendant.get("changed") is False
                and descendant.get("missing") == []
                and descendant.get("schema_inventory_before") == descendant.get("schema_inventory_after")
                and descendant.get("database_sha256_before") == descendant.get("database_sha256_after")
                and recertification == {
                    "mode": "existing_bytes_from_stale_completed_receipt",
                    "historical_receipt_sha256": current_digest,
                    "historical_database_sha256_after": current.get("database_sha256_after"),
                    "current_database_sha256": descendant.get("database_sha256_after"),
                }
            )
            if not (inventory_continuous and database_continuous) and not stale_bridge:
                raise click.ClickException("AC dev schema admission receipt chain is discontinuous")
        previous = current.get("previous_receipt_sha256")
        if not isinstance(previous, str):
            raise click.ClickException("AC dev schema admission receipt predecessor mismatch")
        if not previous:
            return
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", previous) or previous == current_digest:
            raise click.ClickException("AC dev schema admission receipt predecessor mismatch")
        previous_path = archive / (previous.removeprefix("sha256:") + ".json")
        descendant = current
        descendant_before = current["schema_inventory_before"]
        descendant_database_before = str(current["database_sha256_before"])
        current, current_digest = _read_admission_receipt(previous_path, archive=archive)


def _offline_dev_schema_admission(
    storage_root: Path, *, project_id: str, port: int, resume_receipt: Path | None,
    authority_projection: bool = False,
    recertify_existing_bytes: Path | None = None,
    _after_checkpoint_for_test=None,
) -> dict[str, Any]:
    """One-shot, offline-only repair for the bounded dev backlog-read plan.

    This command intentionally has no HTTP, graph, or service-manager path.
    Its target is a stopped external AC dev world and it never opens the stable
    database selected by the normal runtime.
    """
    if project_id != "aming-claw" or port != AC_DEV_SERVICE_PORT:
        raise click.ClickException("offline AC dev schema admission requires exact project and port 40008")
    root = storage_root.expanduser().absolute()
    if root.is_symlink() or not root.is_dir() or "shared-volume" in root.parts:
        raise click.ClickException("offline AC dev schema admission rejects non-external/stable root")
    database = root / "governance" / "aming-claw" / "governance.db"
    if database.is_symlink() or not database.is_file() or database.resolve(strict=True) != database:
        raise click.ClickException("offline AC dev schema admission database identity is invalid")
    if _port_is_open(AC_DEV_SERVICE_PORT):
        raise click.ClickException("offline AC dev schema admission requires stopped port 40008")
    pid_file = root / "runtime" / "state" / "dev-governance" / "governance.pid"
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except (OSError, ValueError):
            pass
        else:
            raise click.ClickException("offline AC dev schema admission requires no live dev PID")
    holders = subprocess.run(["lsof", "-t", "--", str(database)], capture_output=True, text=True, check=False)
    if holders.stdout.strip():
        raise click.ClickException("offline AC dev schema admission requires no database holders")
    launch = root / "launch-receipt.json"
    try:
        launch_data = json.loads(launch.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException("offline AC dev schema admission requires a readable launch receipt") from exc
    if not isinstance(launch_data, dict) or launch_data.get("world_id") != "ac-dev" or launch_data.get("project_id") != project_id or launch_data.get("port") != port:
        raise click.ClickException("offline AC dev schema admission launch receipt mismatch")
    from agent.governance import db as _db
    if authority_projection:
        admit = _db.admit_missing_authority_projection_schema
        drift = _db.authority_projection_schema_drift
        plan_fn = _db.authority_projection_schema_plan
        # Both receipts bind the complete sqlite_master inventory; retain a
        # plan-specific selector so the authority path cannot accidentally
        # depend on an unimported backlog-only symbol.
        inventory_fn = _db.backlog_read_schema_inventory
    else:
        admit = _db.admit_missing_backlog_read_schema
        drift = _db.backlog_read_schema_drift
        plan_fn = _db.backlog_read_schema_plan
        inventory_fn = _db.backlog_read_schema_inventory
    if recertify_existing_bytes is not None and (not authority_projection or resume_receipt is not None):
        raise click.ClickException(
            "AC dev schema admission recertification requires authority schema and no resume receipt"
        )
    if recertify_existing_bytes is not None:
        root_identity = _admission_identity(root)
        database_identity = _admission_identity(database)
        archive = root / "archive" / "schema-admission"
        historical, historical_sha256 = _read_admission_receipt(
            recertify_existing_bytes.absolute(), archive=archive,
        )
        plan_sha256 = "sha256:" + hashlib.sha256(
            _canonical_json_bytes(plan_fn())
        ).hexdigest()
        historical_source = _validated_historical_admission_source_identity(
            historical.get("source_identity")
        )
        historical_db_tip = historical_source["db_source_tip"]
        expected_source = historical_source
        if (
            historical.get("schema_version") != _AC_DEV_SCHEMA_ADMISSION_RECEIPT_VERSION
            or historical.get("stage") != "completed"
            or historical.get("changed") is not True
            or historical.get("project_id") != project_id
            or historical.get("port") != port
            or historical.get("root_identity") != root_identity
            or historical.get("database_identity") != database_identity
            or historical.get("source_identity") != expected_source
            or historical.get("plan_sha256") != plan_sha256
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(historical.get("database_sha256_after") or ""),
            )
        ):
            raise click.ClickException(
                "AC dev schema admission recertification historical receipt mismatch"
            )
        expected_inventory = _validated_authority_receipt_inventory(
            historical.get("schema_inventory_after"), db_module=_db,
        )
        _validate_admission_receipt_chain(
            historical, historical_sha256, archive=archive,
            project_id=project_id, port=port, root_identity=root_identity,
            database_identity=database_identity, source_identity=expected_source,
            plan_sha256=plan_sha256,
        )
        artifacts_before = _admission_sqlite_artifact_identities(database)
        if any(artifacts_before.get(suffix) is not None for suffix in ("-wal", "-shm", "-journal")):
            raise click.ClickException(
                "AC dev schema admission recertification requires durable sidecar-free bytes"
            )
        durable_before = _admission_database_sha256(
            database, expected_identity=database_identity,
        )
        if historical["database_sha256_after"] == durable_before:
            raise click.ClickException(
                "AC dev schema admission recertification requires a stale completed receipt"
            )
        readonly = sqlite3.connect(
            "file:" + urllib.parse.quote(str(database)) + "?mode=ro&immutable=1", uri=True,
            timeout=0, isolation_level=None,
        )
        try:
            readonly.execute("PRAGMA query_only=ON")
            checks = [str(row[0]).lower() for row in readonly.execute("PRAGMA quick_check")]
            if checks != ["ok"]:
                raise click.ClickException("AC dev schema admission recertification quick_check failed")
            meta = dict(readonly.execute(
                "SELECT key, value FROM schema_meta WHERE key IN "
                "('governance_world_id','governance_world_genesis_json',"
                "'governance_world_source_tip_json')"
            ))
            if (
                meta.get("governance_world_id") != "ac-dev"
                or not meta.get("governance_world_genesis_json")
                or not meta.get("governance_world_source_tip_json")
            ):
                raise click.ClickException("offline AC dev schema admission genesis/source-tip mismatch")
            state = drift(readonly)
            if state["missing"] or state["invalid"]:
                raise click.ClickException("AC dev schema admission recertification requires complete exact schema")
            inventory = inventory_fn(readonly)
            current_db_tip = json.loads(str(meta["governance_world_source_tip_json"]))
            if (
                current_db_tip != historical_db_tip
                or "sha256:" + hashlib.sha256(
                    _canonical_json_bytes(current_db_tip)
                ).hexdigest() != historical_source["db_source_tip_sha256"]
            ):
                raise click.ClickException(
                    "AC dev schema admission recertification current source-tip mismatch"
                )
            source_identity = historical_source
        finally:
            readonly.close()
        durable_after = _admission_database_sha256(
            database, expected_identity=database_identity,
        )
        if durable_after != durable_before:
            raise click.ClickException("AC dev schema admission recertification changed database bytes")
        if inventory != expected_inventory or source_identity != expected_source:
            raise click.ClickException(
                "AC dev schema admission recertification current database binding mismatch"
            )
        if _admission_sqlite_artifact_identities(database) != artifacts_before:
            raise click.ClickException(
                "AC dev schema admission recertification changed SQLite artifacts"
            )
        backup = archive / (durable_after.removeprefix("sha256:") + ".pre.sqlite")
        if backup.exists():
            _admission_regular_file(backup, archive=archive)
        else:
            shutil.copy2(database, backup)
        if _file_sha256(backup) != durable_after:
            raise click.ClickException("AC dev schema admission backup digest mismatch")
        payload = {
            "schema_version": _AC_DEV_SCHEMA_RECERTIFICATION_RECEIPT_VERSION,
            "stage": "completed", "project_id": project_id, "port": port,
            "root_identity": root_identity, "database_identity": database_identity,
            "source_identity": source_identity, "plan_sha256": plan_sha256,
            "schema_inventory_before": inventory, "schema_inventory_after": inventory,
            "backup": {"identity": _admission_identity(backup), "sha256": durable_after},
            "database_sha256_before": durable_before,
            "database_sha256_after": durable_after,
            "previous_receipt_sha256": historical_sha256,
            "changed": False, "missing": [],
            "recertification": {
                "mode": "existing_bytes_from_stale_completed_receipt",
                "historical_receipt_sha256": historical_sha256,
                "historical_database_sha256_after": historical["database_sha256_after"],
                "current_database_sha256": durable_after,
            },
        }
        receipt_path, receipt_digest = _write_admission_receipt(archive, payload)
        return {
            "status": "recertified_existing_bytes", "receipt_path": str(receipt_path),
            "receipt_sha256": receipt_digest, "changed": False, "missing": [],
            "post_sha256": durable_after,
        }
    conn = sqlite3.connect(str(database), timeout=5)
    try:
        meta = dict(conn.execute("SELECT key, value FROM schema_meta WHERE key IN ('governance_world_id','governance_world_genesis_json','governance_world_source_tip_json')"))
        if meta.get("governance_world_id") != "ac-dev" or not meta.get("governance_world_genesis_json") or not meta.get("governance_world_source_tip_json"):
            raise click.ClickException("offline AC dev schema admission genesis/source-tip mismatch")
        root_identity = _admission_identity(root)
        database_identity = _admission_identity(database)
        source_identity = _admission_source_identity(str(meta["governance_world_source_tip_json"]))
        plan = plan_fn()
        plan_sha256 = "sha256:" + hashlib.sha256(_canonical_json_bytes(plan)).hexdigest()
        archive = root / "archive" / "schema-admission"
        before = drift(conn)
        if before["invalid"]:
            raise click.ClickException("offline AC dev schema admission rejects unexpected schema drift")
        inventory_before = inventory_fn(conn)
        previous_sha256 = ""
        if resume_receipt is not None:
            prior, previous_sha256 = _read_admission_receipt(resume_receipt.absolute(), archive=archive)
            if prior.get("schema_version") == _AC_DEV_SCHEMA_RECERTIFICATION_RECEIPT_VERSION:
                source_identity = _validated_historical_admission_source_identity(
                    prior.get("source_identity")
                )
                try:
                    current_tip = json.loads(str(meta["governance_world_source_tip_json"]))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise click.ClickException(
                        "AC dev schema admission recertification current source-tip mismatch"
                    ) from exc
                if current_tip != source_identity["db_source_tip"]:
                    raise click.ClickException(
                        "AC dev schema admission recertification current source-tip mismatch"
                    )
            _validate_admission_receipt_chain(
                prior, previous_sha256, archive=archive, project_id=project_id,
                port=port, root_identity=root_identity,
                database_identity=database_identity, source_identity=source_identity,
                plan_sha256=plan_sha256,
            )
            current_database_sha256 = _admission_database_sha256(
                database, expected_identity=database_identity,
            )
            if prior["stage"] == "completed":
                if (prior["schema_inventory_after"] != inventory_before or before["missing"]
                    or prior["database_sha256_after"] != current_database_sha256):
                    raise click.ClickException("AC dev schema admission completed receipt does not match current database")
                return {"status": "already_admitted", "receipt_path": str(resume_receipt), "receipt_sha256": previous_sha256, "changed": False, "missing": []}
            if (
                prior["stage"] != "rolled_back"
                or prior["schema_inventory_after"] != inventory_before
                or prior["database_sha256_after"] != current_database_sha256
            ):
                raise click.ClickException("AC dev schema admission rollback receipt does not match current database")
        pre_digest = _admission_database_sha256(database, expected_identity=database_identity)
        archive.mkdir(parents=True, exist_ok=True)
        backup = archive / (pre_digest.removeprefix("sha256:") + ".pre.sqlite")
        if backup.exists():
            _admission_regular_file(backup, archive=archive)
        else:
            shutil.copy2(database, backup)
        if _file_sha256(backup) != pre_digest:
            raise click.ClickException("AC dev schema admission backup digest mismatch")
        backup_identity = _admission_identity(backup)
        try:
            result = admit(conn)
        except BaseException as exc:
            conn.rollback()
            after_meta = dict(conn.execute("SELECT key, value FROM schema_meta WHERE key='governance_world_source_tip_json'"))
            inventory_after = inventory_fn(conn)
            rollback_payload = {
                "schema_version": _AC_DEV_SCHEMA_ADMISSION_RECEIPT_VERSION,
                "stage": "rolled_back", "project_id": project_id, "port": port,
                "root_identity": root_identity, "database_identity": database_identity,
                "source_identity": source_identity, "plan_sha256": plan_sha256,
                "schema_inventory_before": inventory_before, "schema_inventory_after": inventory_after,
                "backup": {"identity": backup_identity, "sha256": pre_digest},
                "database_sha256_before": pre_digest, "database_sha256_after": "",
                "previous_receipt_sha256": previous_sha256, "changed": False,
                "missing": before["missing"],
            }
            if inventory_after != inventory_before or after_meta.get("governance_world_source_tip_json") != meta["governance_world_source_tip_json"]:
                raise click.ClickException("AC dev schema admission rollback proof failed") from exc
            conn.close()
            conn = None
            rollback_payload["database_sha256_after"] = _admission_database_sha256(
                database, expected_identity=database_identity,
            )
            failed_path, failed_digest = _write_admission_receipt(archive, rollback_payload)
            raise click.ClickException(
                f"AC dev schema admission rolled back; receipt={failed_path} sha256={failed_digest}"
            ) from exc
        inventory_after = inventory_fn(conn)
        post_meta = dict(conn.execute("SELECT key, value FROM schema_meta WHERE key='governance_world_source_tip_json'"))
        if post_meta.get("governance_world_source_tip_json") != meta["governance_world_source_tip_json"]:
            raise click.ClickException("AC dev schema admission source-tip advanced unexpectedly")
        if drift(conn)["missing"] or inventory_after == inventory_before and before["missing"]:
            raise click.ClickException("AC dev schema admission postcondition failed")
        sqlite_identities = _admission_sqlite_artifact_identities(database)
        if authority_projection:
            checkpoint = tuple(int(value) for value in conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone())
            if checkpoint != (0, 0, 0):
                raise click.ClickException("AC dev schema admission checkpoint did not truncate")
        durable_state = _admission_file_state(database) if authority_projection else None
        if _after_checkpoint_for_test is not None:
            _after_checkpoint_for_test()
        # Verify the committed projection and immutable source binding once
        # more after checkpoint, then close the writer before hashing bytes.
        inventory_after = inventory_fn(conn)
        post_meta = dict(conn.execute(
            "SELECT key, value FROM schema_meta WHERE key='governance_world_source_tip_json'"
        ))
        if (
            drift(conn)["missing"] or drift(conn)["invalid"]
            or post_meta.get("governance_world_source_tip_json")
            != meta["governance_world_source_tip_json"]
        ):
            raise click.ClickException("AC dev schema admission durable postcondition failed")
        _assert_admission_sqlite_artifacts(database, sqlite_identities)
        if durable_state is not None and _admission_file_state(database) != durable_state:
            raise click.ClickException("AC dev schema admission database drifted after checkpoint")
        conn.close()
        conn = None
        _assert_admission_sqlite_artifacts(
            database, sqlite_identities, after_close=True,
        )
        if durable_state is not None and _admission_file_state(database) != durable_state:
            raise click.ClickException("AC dev schema admission database drifted while closing writer")
        durable_post_digest = _admission_database_sha256(
            database, expected_identity=database_identity,
        )
        receipt_payload = {
            "schema_version": _AC_DEV_SCHEMA_ADMISSION_RECEIPT_VERSION,
            "stage": "completed", "project_id": project_id, "port": port,
            "root_identity": root_identity, "database_identity": database_identity,
            "source_identity": source_identity, "plan_sha256": plan_sha256,
            "schema_inventory_before": inventory_before, "schema_inventory_after": inventory_after,
            "backup": {"identity": backup_identity, "sha256": pre_digest},
            "database_sha256_before": pre_digest, "database_sha256_after": durable_post_digest,
            "previous_receipt_sha256": previous_sha256, "changed": bool(result["changed"]),
            "missing": result["missing"],
        }
        receipt_path, receipt_digest = _write_admission_receipt(archive, receipt_payload)
        return {"status": "admitted", "receipt_path": str(receipt_path), "receipt_sha256": receipt_digest, "changed": result["changed"], "missing": result["missing"], "post_sha256": durable_post_digest}
    finally:
        if conn is not None:
            conn.close()


@main.command("dev-admit-schema")
@click.option("--dev-storage-root", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--project-id", required=True)
@click.option("--port", required=True, type=int)
@click.option("--resume-receipt", default=None, type=click.Path(exists=True, dir_okay=False, path_type=Path))
def dev_admit_schema(dev_storage_root: Path, project_id: str, port: int, resume_receipt: Path | None) -> None:
    """Admit only the known missing backlog-read objects into a stopped dev world."""
    try:
        click.echo(json.dumps(_offline_dev_schema_admission(dev_storage_root, project_id=project_id, port=port, resume_receipt=resume_receipt), sort_keys=True))
    except click.ClickException:
        raise
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("dev-admit-authority-schema")
@click.option("--dev-storage-root", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--project-id", required=True)
@click.option("--port", required=True, type=int)
@click.option("--resume-receipt", default=None, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--recertify-existing-bytes", default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Historical immutable completed receipt whose stale DB hash is recertified.",
)
def dev_admit_authority_schema(dev_storage_root: Path, project_id: str, port: int, resume_receipt: Path | None, recertify_existing_bytes: Path | None) -> None:
    """Offline-only admission of the complete six-table AC authority plan."""
    try:
        click.echo(json.dumps(_offline_dev_schema_admission(
            dev_storage_root, project_id=project_id, port=port,
            resume_receipt=resume_receipt, authority_projection=True,
            recertify_existing_bytes=recertify_existing_bytes,
        ), sort_keys=True))
    except click.ClickException:
        raise
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        raise click.ClickException(str(exc)) from exc


def _dashboard_url(governance_url: str) -> str:
    return governance_url.rstrip("/") + "/dashboard"


def _default_runtime_workspace() -> Path:
    """Return the plugin/runtime root used for local governance state."""
    return Path(__file__).resolve().parents[1]


def _aming_claw_source_checkout(start: Path) -> Optional[Path]:
    candidate = start.resolve()
    for root in (candidate, *candidate.parents):
        if all(
            (root / rel).exists()
            for rel in (".git", "agent/cli.py", "start_governance.py", "pyproject.toml")
        ):
            return root
    return None


def _file_sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return ""


def _require_source_checkout_matches_loaded_package(workspace: str = "") -> None:
    package_root = _default_runtime_workspace().resolve()
    candidates = [Path.cwd()]
    if workspace:
        candidates.insert(0, Path(workspace).expanduser())
    source_roots = list(
        dict.fromkeys(
            root
            for candidate in candidates
            if (root := _aming_claw_source_checkout(candidate)) is not None
        )
    )
    source_root = next((root for root in source_roots if root != package_root), None)
    if source_root is None:
        return
    diagnostic = {
        "schema_version": "governance_startup_source_identity.v1",
        "source_checkout": str(source_root),
        "source_cli_sha256": _file_sha256(source_root / "agent" / "cli.py"),
        "loaded_package_root": str(package_root),
        "loaded_cli_sha256": _file_sha256(Path(__file__).resolve()),
        "zero_write_rejection": True,
    }
    raise click.ClickException(
        "Aming Claw source checkout does not match the loaded package root; "
        "refusing startup before health or runtime mutation. "
        + json.dumps(diagnostic, sort_keys=True)
        + ". Run the source checkout interpreter with `python -m agent.cli start`, "
        "or repair that checkout with `python -m pip install -e .`."
    )


class _GovernanceProbeNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _strict_local_governance_json_probe(
    port: int,
    path: str,
    *,
    timeout: float,
    max_bytes: int,
) -> Optional[dict]:
    if (
        type(port) is not int
        or not 1 <= port <= 65535
        or not path.startswith("/api/")
        or "//" in path
        or "#" in path
        or any(ord(char) < 0x20 for char in path)
        or max_bytes < 1
    ):
        return None
    url = f"http://127.0.0.1:{port}{path}"
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json"},
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _GovernanceProbeNoRedirect(),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            if int(response.getcode()) != 200 or str(response.geturl() or "") != url:
                return None
            content_type = str(response.headers.get("Content-Type") or "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                return None
            content_length = str(response.headers.get("Content-Length") or "").strip()
            if content_length:
                declared_length = int(content_length)
                if declared_length < 0 or declared_length > max_bytes:
                    return None
            payload_bytes = response.read(max_bytes + 1)
    except (
        OSError,
        ValueError,
        UnicodeError,
        urllib.error.URLError,
        http.client.HTTPException,
    ):
        return None
    if not payload_bytes or len(payload_bytes) > max_bytes:
        return None
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _probe_governance(port: int, *, timeout: float = 2.0) -> Optional[dict]:
    return _strict_local_governance_json_probe(
        port,
        "/api/health",
        timeout=timeout,
        max_bytes=_GOVERNANCE_PROBE_HEALTH_BYTES,
    )


def _probe_governance_path(
    port: int,
    path: str,
    *,
    timeout: float = 2.0,
) -> Optional[dict]:
    return _strict_local_governance_json_probe(
        port,
        path,
        timeout=timeout,
        max_bytes=_GOVERNANCE_PROBE_GRAPH_BYTES,
    )


def _http_json(method: str, url: str, payload: dict | None = None, *, timeout: float = 30.0) -> tuple[int, dict]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local governance URL by default
            body = resp.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            return resp.status, parsed if isinstance(parsed, dict) else {"ok": False, "error": "non_object_response"}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"ok": False, "error": "http_error", "message": body}
        if not isinstance(parsed, dict):
            parsed = {"ok": False, "error": "http_error", "message": body}
        return exc.code, parsed


def _port_is_open(port: int, *, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _port_owner_hint(port: int) -> str:
    if sys.platform.startswith("win"):
        try:
            proc = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except Exception:
            return ""
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                if parts[1].endswith(f":{port}"):
                    return f" PID={parts[-1]}"
        return ""
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return ""
    pid = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    return f" PID={pid}" if pid else ""


def _governance_start_resource_preflight() -> dict[str, Any]:
    """Ensure this process has release-safe file-descriptor headroom.

    Platforms without the POSIX ``resource`` API continue with an explicit
    unavailable diagnostic because there is no portable per-process limit to
    inspect or change there. When the API is present, unreadable limits,
    mutation failures, or a finite hard limit below the release minimum fail
    closed before governance imports and opens project databases.
    """
    diagnostic: dict[str, Any] = {
        "schema_version": "governance_startup_resource_preflight.v1",
        "resource": "RLIMIT_NOFILE",
        "required_soft_limit": _GOVERNANCE_MIN_NOFILE,
        "process_scope_only": True,
        "global_host_mutation": False,
    }
    try:
        import resource as resource_module
    except ImportError:
        return {
            **diagnostic,
            "supported": False,
            "status": "unavailable",
            "policy": "continue_when_resource_api_unavailable",
        }

    required_api = ("RLIMIT_NOFILE", "RLIM_INFINITY", "getrlimit", "setrlimit")
    if any(not hasattr(resource_module, name) for name in required_api):
        return {
            **diagnostic,
            "supported": False,
            "status": "unavailable",
            "policy": "continue_when_resource_api_unavailable",
        }

    try:
        soft_limit, hard_limit = resource_module.getrlimit(resource_module.RLIMIT_NOFILE)
    except (OSError, ValueError) as exc:
        raise click.ClickException(
            "Governance startup resource preflight could not read RLIMIT_NOFILE; "
            "refusing to start without verified descriptor headroom."
        ) from exc

    infinity = resource_module.RLIM_INFINITY
    hard_is_infinite = hard_limit == infinity
    limit_display = lambda value: "infinity" if value == infinity else int(value)
    diagnostic.update(
        {
            "supported": True,
            "original_soft_limit": limit_display(soft_limit),
            "hard_limit": limit_display(hard_limit),
            "hard_limit_sufficient": hard_is_infinite
            or hard_limit >= _GOVERNANCE_MIN_NOFILE,
        }
    )

    if not hard_is_infinite and hard_limit < _GOVERNANCE_MIN_NOFILE:
        raise click.ClickException(
            "Governance startup requires RLIMIT_NOFILE soft headroom of at least "
            f"{_GOVERNANCE_MIN_NOFILE}, but the finite hard limit is {hard_limit}. "
            "Raise the launcher/service hard limit before starting governance."
        )

    if soft_limit >= _GOVERNANCE_MIN_NOFILE or soft_limit == infinity:
        return {
            **diagnostic,
            "status": "already_sufficient",
            "policy": "preserve_higher_existing_limit",
            "effective_soft_limit": limit_display(soft_limit),
        }

    try:
        resource_module.setrlimit(
            resource_module.RLIMIT_NOFILE,
            (_GOVERNANCE_MIN_NOFILE, hard_limit),
        )
        effective_soft, effective_hard = resource_module.getrlimit(
            resource_module.RLIMIT_NOFILE
        )
    except (OSError, ValueError) as exc:
        raise click.ClickException(
            "Governance startup could not raise the process RLIMIT_NOFILE soft "
            f"limit to {_GOVERNANCE_MIN_NOFILE}; refusing to start."
        ) from exc
    if effective_soft < _GOVERNANCE_MIN_NOFILE:
        raise click.ClickException(
            "Governance startup RLIMIT_NOFILE verification failed after the "
            f"raise attempt (effective soft limit {effective_soft})."
        )

    return {
        **diagnostic,
        "status": "raised",
        "policy": "raise_process_soft_limit_to_release_minimum",
        "effective_soft_limit": limit_display(effective_soft),
        "effective_hard_limit": limit_display(effective_hard),
    }


def _launcher_html(governance_url: str) -> str:
    dashboard_url = _dashboard_url(governance_url)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Aming Claw Launcher</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #172033; }}
    main {{ max-width: 760px; }}
    a.button {{ display: inline-block; padding: 10px 14px; border: 1px solid #b6c7e6; border-radius: 6px; text-decoration: none; color: #0f3d7a; background: #f6f9ff; }}
    code {{ background: #f2f5fa; padding: 2px 5px; border-radius: 4px; }}
    pre {{ background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 6px; overflow: auto; }}
  </style>
</head>
<body>
  <main>
    <h1>Aming Claw Launcher</h1>
    <p>This local launcher never starts governance automatically. Start services explicitly, then open the dashboard.</p>
    <p><a class="button" href="{dashboard_url}">Open dashboard</a></p>
    <h2>Start locally</h2>
    <pre>aming-claw start</pre>
    <p>If the console script is not on PATH yet, use:</p>
    <pre>python -m agent.cli start</pre>
    <h2>Install/update plugin from Git</h2>
    <pre>aming-claw plugin install https://github.com/amingclawdev/aming-claw</pre>
    <h2>Check status</h2>
    <pre>aming-claw status</pre>
    <p>Codex and Claude Code should connect through the project <code>.mcp.json</code> after governance is available at <code>{governance_url}</code>.</p>
  </main>
</body>
</html>
"""


_AC_DEV_DURABLE_LAUNCH_VERSION = "ac_dev_durable_launch.v1"
_AC_DEV_CANONICAL_LEGACY_POSTIMAGE_ADOPTION_VERSION = (
    "ac_dev_canonical_legacy_postimage_adoption.v1"
)
_DURABLE_START_LEGACY_ADOPTION = "LEGACY_ADOPTION"
_DURABLE_START_COMPLETED_BOOTSTRAP = "COMPLETED_BOOTSTRAP"


def _immutable_sqlite_projection(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return source-independent logical hashes without ever opening a writer."""
    from agent.governance import db as _db
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise click.ClickException("AC dev canonical adoption quick-check failed")
        projection = _db._sqlite_logical_projection(
            connection, exclude_tables=frozenset({"schema_meta"}),
        )
        meta = dict(connection.execute("SELECT key,value FROM schema_meta ORDER BY key"))
        return projection, {str(key): str(value) for key, value in meta.items()}
    finally:
        connection.close()


def _canonical_adoption_receipts(root: Path) -> list[Path]:
    directory = root / "archive" / "canonical-legacy-postimage-adoption"
    return sorted(directory.glob("adoption.*.json")) if directory.is_dir() else []


def _read_canonical_adoption_receipt(path: Path) -> tuple[dict[str, Any], str]:
    return _read_durable_content_receipt(path, "adoption")


def _canonical_legacy_postimage_adoption(
    dev_storage: Path, *, linked_v3_receipt: Path,
    source_identity: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Seal the one audited canonical foreground postimage without DB writes."""
    from agent.governance import db as _db
    from agent.runtime_plane import resolve_ac_dev_storage_root

    source = dict(source_identity or _dev_source_identity_precheck())
    if (source.get("branch") != AC_DEV_BRANCH
            or source.get("root") != str(Path(__file__).resolve().parents[1])
            or source.get("dirty")):
        raise click.ClickException("AC dev canonical adoption candidate source mismatch")
    stable = _db.verified_stable_database_binding()
    stable_root = Path(str(stable["shared_volume_path"])).resolve(strict=True)
    root = dev_storage.expanduser().absolute()
    if root.is_symlink() or root.resolve(strict=True) != root or root != resolve_ac_dev_storage_root(stable_root):
        raise click.ClickException("AC dev canonical adoption requires the canonical dev root")
    database = root / _db.AC_DATABASE_DEV_RELATIVE_PATH
    identity = _admission_identity(database)
    stable_db = Path(str(stable["database_path"])).resolve(strict=True)
    stable_identity = _admission_identity(stable_db)
    if (identity["device"], identity["inode"]) == (stable_identity["device"], stable_identity["inode"]):
        raise click.ClickException("AC dev canonical adoption overlaps stable database")
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(database) + suffix).exists() or Path(str(database) + suffix).is_symlink():
            raise click.ClickException("AC dev canonical adoption requires sidecar-free bytes")
    if _port_is_open(AC_DEV_SERVICE_PORT) or _durable_listener_pid(AC_DEV_SERVICE_PORT):
        raise click.ClickException("AC dev canonical adoption requires a free port 40008")
    holder = subprocess.run(
        ["lsof", "-t", str(database)], capture_output=True, text=True, check=False, timeout=3,
    ).stdout.strip()
    if holder:
        raise click.ClickException("AC dev canonical adoption rejects database holders")

    archive = root / "archive" / "schema-admission"
    linked, linked_digest = _read_admission_receipt(linked_v3_receipt.absolute(), archive=archive)
    linked_source = _validated_historical_admission_source_identity(linked.get("source_identity"))
    plan_sha = "sha256:" + hashlib.sha256(
        _canonical_json_bytes(_db.authority_projection_schema_plan())
    ).hexdigest()
    root_identity = _admission_identity(root)
    if (linked.get("schema_version") != _AC_DEV_SCHEMA_RECERTIFICATION_RECEIPT_VERSION
            or linked.get("stage") != "completed" or linked.get("changed") is not False
            or linked.get("project_id") != "aming-claw" or linked.get("port") != 40008
            or linked.get("root_identity") != root_identity
            or linked.get("database_identity") != identity or linked.get("plan_sha256") != plan_sha):
        raise click.ClickException("AC dev canonical adoption linked-v3 binding mismatch")
    _validated_authority_receipt_inventory(linked.get("schema_inventory_after"), db_module=_db)
    _validate_admission_receipt_chain(
        linked, linked_digest, archive=archive, project_id="aming-claw", port=40008,
        root_identity=root_identity, database_identity=identity,
        source_identity=linked_source, plan_sha256=plan_sha,
    )
    backup = Path(str(dict(linked.get("backup") or {}).get("identity", {}).get("path") or ""))
    if not backup.is_file() or backup.is_symlink() or _file_sha256(backup) != linked.get("database_sha256_after"):
        raise click.ClickException("AC dev canonical adoption preimage backup mismatch")
    if (_admission_identity(backup).get("device") != dict(linked.get("backup") or {}).get("identity", {}).get("device")
            or _admission_identity(backup).get("inode") != dict(linked.get("backup") or {}).get("identity", {}).get("inode")):
        raise click.ClickException("AC dev canonical adoption preimage identity mismatch")
    historical_sha = str(linked.get("previous_receipt_sha256") or "")
    quarantine_root = root / "quarantine" / "schema-admission-sidecars" / str(
        linked.get("database_sha256_after") or ""
    ).removeprefix("sha256:")
    manifests = sorted(quarantine_root.glob("manifest.*.json"))
    completed_manifests = [item for item in manifests if item.name != "manifest.pending.json"]
    if len(completed_manifests) != 1:
        raise click.ClickException("AC dev canonical adoption quarantine manifest is missing")
    quarantine_manifest = completed_manifests[0]
    manifest_match = re.fullmatch(r"manifest\.([0-9a-f]{64})\.json", quarantine_manifest.name)
    manifest_raw = quarantine_manifest.read_bytes()
    try:
        manifest = json.loads(manifest_raw)
    except (OSError, ValueError, TypeError) as exc:
        raise click.ClickException("AC dev canonical adoption quarantine manifest is invalid") from exc
    sidecars = list(manifest.get("sidecars") or []) if isinstance(manifest, Mapping) else []
    kinds = {str(item.get("kind") or "") for item in sidecars if isinstance(item, Mapping)}
    historical_readback = dict(dict(manifest.get("readback") or {}).get("historical_receipt") or {})
    for item in sidecars:
        target = Path(str(item.get("target") or ""))
        target_identity = _admission_identity(target)
        target_stat = target.stat(follow_symlinks=False)
        source_claim = dict(item.get("source") or {})
        if (target.is_symlink() or not target.is_file()
                or target_identity.get("device") != source_claim.get("st_dev")
                or target_identity.get("inode") != source_claim.get("st_ino")
                or int(target_stat.st_size) != source_claim.get("size")
                or _file_sha256(target).removeprefix("sha256:") != source_claim.get("sha256")):
            raise click.ClickException("AC dev canonical adoption quarantine target mismatch")
    if (manifest_match is None
            or hashlib.sha256(manifest_raw).hexdigest() != manifest_match.group(1)
            or manifest.get("schema_version") != "aming-claw.schema-admission-sidecar-quarantine.v1"
            or manifest.get("stage") != "completed" or kinds != {"wal", "shm"}
            or dict(manifest.get("database") or {}).get("sha256")
                != str(linked.get("database_sha256_after") or "").removeprefix("sha256:")
            or historical_readback.get("sha256") != historical_sha.removeprefix("sha256:")):
        raise click.ClickException("AC dev canonical adoption quarantine chain mismatch")

    before_projection, before_meta = _immutable_sqlite_projection(backup)
    after_projection, after_meta = _immutable_sqlite_projection(database)
    if before_projection != after_projection:
        raise click.ClickException("AC dev canonical adoption rejects non-custody database drift")
    changed = {key: {"before": before_meta.get(key), "after": after_meta.get(key)}
               for key in sorted(set(before_meta) | set(after_meta))
               if before_meta.get(key) != after_meta.get(key)}
    exact_fields = {
        "governance_world_current_process_json", "governance_world_source_tip_json",
        "governance_world_source_tip_revision", "governance_world_source_tip_sha256",
    }
    if set(changed) != exact_fields:
        raise click.ClickException("AC dev canonical adoption custody delta mismatch")
    try:
        old_tip = json.loads(before_meta["governance_world_source_tip_json"])
        new_tip = json.loads(after_meta["governance_world_source_tip_json"])
        old_process = json.loads(after_meta["governance_world_current_process_json"])
    except (KeyError, TypeError, ValueError) as exc:
        raise click.ClickException("AC dev canonical adoption custody metadata is invalid") from exc
    receipt_cli = dict(linked_source.get("cli_source") or {})
    receipt_db_tip = dict(linked_source.get("db_source_tip") or {})
    if old_tip != receipt_db_tip:
        raise click.ClickException("AC dev canonical adoption admitted preimage source-tip mismatch")
    legacy_source = dict(new_tip)
    legacy_pid = int(old_process.get("pid") or 0)
    stable_health = dict(stable.get("health") or {})
    stable_pid = int(stable_health.get("pid") or 0)
    stable_runtime = dict(stable_health.get("runtime_plane_identity") or {})
    if (stable_pid <= 0 or stable_pid == legacy_pid
            or stable_runtime.get("world_id") != "ac-stable"
            or stable_runtime.get("worktree_root") in {str(root), str(source.get("root") or "")}):
        raise click.ClickException("AC dev canonical adoption stable custody overlaps legacy world")
    try:
        if legacy_pid > 0:
            os.kill(legacy_pid, 0)
            raise click.ClickException("AC dev canonical adoption legacy PID remains live")
    except ProcessLookupError:
        pass
    old_commit = str(legacy_source.get("commit") or "")
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", old_commit, str(source.get("commit") or "")],
        cwd=source["root"], check=False, capture_output=True,
    )
    if ancestry.returncode != 0:
        raise click.ClickException("AC dev canonical adoption source lineage mismatch")
    old_cli = subprocess.run(
        ["git", "show", f"{old_commit}:agent/cli.py"], cwd=source["root"],
        check=False, capture_output=True,
    )
    if (old_cli.returncode != 0 or "sha256:" + hashlib.sha256(old_cli.stdout).hexdigest()
            != legacy_source.get("source_sha256")
            or _file_sha256(Path(source["root"]) / "agent" / "cli.py") != source.get("source_sha256")):
        raise click.ClickException("AC dev canonical adoption source hash mismatch")
    current_sha = _admission_database_sha256(database, expected_identity=identity)
    pidfile = root / "runtime" / "state" / "dev-governance" / "governance.pid"
    pidfile_binding: dict[str, Any] = {"path": str(pidfile), "exists": pidfile.exists()}
    if pidfile.exists():
        if pidfile.is_symlink() or not pidfile.is_file():
            raise click.ClickException("AC dev canonical adoption stale pidfile is invalid")
        pidfile_binding.update(_admission_identity(pidfile))
        pidfile_binding["sha256"] = _file_sha256(pidfile)
        try:
            stale_record = json.loads(pidfile.read_text(encoding="utf-8"))
            stale_pid = int(stale_record.get("pid") or 0)
        except (OSError, TypeError, ValueError) as exc:
            raise click.ClickException("AC dev canonical adoption stale pidfile is unreadable") from exc
        pidfile_binding["record"] = stale_record
        try:
            if stale_pid > 0:
                os.kill(stale_pid, 0)
                raise click.ClickException("AC dev canonical adoption stale pidfile PID remains live")
        except ProcessLookupError:
            pass
    # ``target`` above is deliberately scoped to quarantine sidecar validation;
    # inventory authority is always the canonical admitted database.
    inventory_connection = sqlite3.connect(
        "file:" + urllib.parse.quote(str(database)) + "?mode=ro&immutable=1", uri=True,
    )
    try:
        target_inventory_after = _db.backlog_read_schema_inventory(inventory_connection)
    finally:
        inventory_connection.close()
    payload = {
        "schema_version": _AC_DEV_CANONICAL_LEGACY_POSTIMAGE_ADOPTION_VERSION,
        "stage": "completed", "project_id": "aming-claw", "port": 40008,
        "root_identity": root_identity, "database_identity": identity,
        "database_sha256_preimage": linked.get("database_sha256_after"),
        "database_sha256_postimage": current_sha,
        "linked_v3_receipt": str(linked_v3_receipt.absolute()),
        "linked_v3_receipt_sha256": linked_digest,
        "receipt_source_identity": receipt_cli,
        "preimage_source_tip_identity": receipt_db_tip,
        "legacy_source_identity": legacy_source, "candidate_source_identity": source,
        "custody_delta": changed, "non_schema_meta_projection": after_projection,
        "schema_meta_postimage": after_meta,
        "legacy_process_identity": old_process, "stale_pidfile_identity": pidfile_binding,
        "quarantine_manifest": {"path": str(quarantine_manifest),
                                "sha256": "sha256:" + manifest_match.group(1)},
        "readbacks": {"port_free": True, "database_holders": [], "sidecars_absent": True,
                      "stable_database_identity": stable_identity,
                      "stable_pid": stable_pid,
                      "stable_runtime_commit": stable_runtime.get("commit"),
                      "stable_worktree_root": stable_runtime.get("worktree_root")},
    }
    destination_dir = root / "archive" / "canonical-legacy-postimage-adoption"
    existing = _canonical_adoption_receipts(root)
    if existing:
        if len(existing) != 1:
            raise click.ClickException("AC dev canonical adoption has ambiguous prior receipts")
        prior, prior_sha = _read_canonical_adoption_receipt(existing[0])
        if prior != payload:
            raise click.ClickException("AC dev canonical adoption replay drift")
        return {"status": "already_adopted", "receipt_path": str(existing[0]), "receipt_sha256": prior_sha}
    destination_dir.mkdir(parents=True, exist_ok=True)
    receipt_path, receipt_sha = _durable_content_receipt(destination_dir, "adoption", payload)
    return {"status": "adopted", "receipt_path": str(receipt_path), "receipt_sha256": receipt_sha}


@main.command("dev-adopt-canonical-legacy-postimage")
@click.option("--dev-storage-root", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--project-id", required=True)
@click.option("--port", required=True, type=int)
@click.option("--linked-v3-receipt", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
def dev_adopt_canonical_legacy_postimage(dev_storage_root: Path, project_id: str, port: int, linked_v3_receipt: Path) -> None:
    if project_id != "aming-claw" or port != 40008:
        raise click.ClickException("AC dev canonical adoption requires aming-claw on port 40008")
    click.echo(json.dumps(_canonical_legacy_postimage_adoption(
        dev_storage_root, linked_v3_receipt=linked_v3_receipt,
    ), sort_keys=True))


def _dashboard_backlog_table_projection(
    connection: sqlite3.Connection,
) -> tuple[dict[str, Any], list[tuple[object, ...]]]:
    """Return the exact backlog table ABI and a lossless ordered row digest."""
    from agent.governance import db as _db
    table_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='backlog_bugs'"
    ).fetchone()
    if table_sql_row is None or not table_sql_row[0]:
        raise click.ClickException("dashboard backlog bootstrap source table is missing")
    columns = [tuple(row) for row in connection.execute("PRAGMA table_info(\"backlog_bugs\")")]
    if not columns or str(columns[0][1]) != "bug_id":
        raise click.ClickException("dashboard backlog bootstrap table schema is invalid")
    names = [str(row[1]) for row in columns]
    quoted = ",".join(_db._sqlite_quote_identifier(name) for name in names)
    rows = []
    for row in connection.execute(
        f"SELECT {quoted} FROM \"backlog_bugs\" ORDER BY \"bug_id\""
    ):
        rows.append(tuple(row))
    encoded_rows = [[_db._sqlite_projection_value(value) for value in row] for row in rows]
    status_index = names.index("status")
    statuses: dict[str, int] = {}
    for row in rows:
        status = str(row[status_index])
        statuses[status] = statuses.get(status, 0) + 1
    schema_payload = {
        "table": "backlog_bugs", "sql": _db._backlog_read_normalized_sql(table_sql_row[0]),
        "columns": [list(row) for row in columns], "order": ["bug_id", "ASC"],
    }
    row_payload = {"schema": schema_payload, "rows": encoded_rows}
    return {
        "schema": schema_payload,
        "schema_sha256": "sha256:" + hashlib.sha256(
            _canonical_json_bytes(schema_payload)
        ).hexdigest(),
        "rows_sha256": "sha256:" + hashlib.sha256(
            _canonical_json_bytes(row_payload)
        ).hexdigest(),
        "row_count": len(rows), "status_counts": dict(sorted(statuses.items())),
        "columns": names,
    }, rows


def _bootstrap_regular_database(path: Path, *, label: str) -> dict[str, Any]:
    absolute = path.expanduser().absolute()
    if absolute.is_symlink() or not absolute.is_file() or absolute.resolve(strict=True) != absolute:
        raise click.ClickException(f"dashboard backlog bootstrap {label} database is invalid")
    details = absolute.stat(follow_symlinks=False)
    if details.st_nlink != 1:
        raise click.ClickException(f"dashboard backlog bootstrap {label} database link count is invalid")
    for suffix in ("-wal", "-shm", "-journal"):
        companion = Path(str(absolute) + suffix)
        if companion.exists() or companion.is_symlink():
            raise click.ClickException(f"dashboard backlog bootstrap {label} requires sidecar-free bytes")
    holders = subprocess.run(
        ["lsof", "-t", "--", str(absolute)], capture_output=True, text=True,
        check=False, timeout=3,
    ).stdout.strip()
    if holders:
        raise click.ClickException(f"dashboard backlog bootstrap {label} rejects database holders")
    return {
        **_admission_identity(absolute), "size": int(details.st_size),
        "mtime_ns": int(details.st_mtime_ns), "nlink": int(details.st_nlink),
        "sha256": _file_sha256(absolute),
    }


def _historical_dashboard_bootstrap_adoption(root: Path) -> tuple[dict[str, Any], str, Path]:
    """Authenticate sealed historical adoption bytes without reading current DB logic.

    This deliberately proves receipt content/address/path relationships only.
    Callers select a separate current-database validator for first import versus
    a postcommit/replay receipt; a historical adoption must never authorize the
    current logical postimage by itself.
    """
    from agent.governance import db as _db
    adoptions = _canonical_adoption_receipts(root)
    if len(adoptions) != 1:
        raise click.ClickException("dashboard backlog bootstrap adoption is missing or ambiguous")
    adoption, adoption_sha = _read_canonical_adoption_receipt(adoptions[0])
    linked = Path(str(adoption.get("linked_v3_receipt") or ""))
    source = _source_git_identity()
    try:
        linked_value, linked_sha = _read_admission_receipt(
            linked.absolute(), archive=root / "archive" / "schema-admission",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException("dashboard backlog bootstrap historical linked receipt mismatch") from exc
    database = root / "governance" / "aming-claw" / "governance.db"
    root_stat = root.stat(follow_symlinks=False)
    if (adoption.get("schema_version") != _AC_DEV_CANONICAL_LEGACY_POSTIMAGE_ADOPTION_VERSION
            or adoption.get("stage") != "completed"
            or adoption.get("project_id") != "aming-claw" or adoption.get("port") != AC_DEV_SERVICE_PORT
            or adoption.get("root_identity") != {"path": str(root), "device": int(root_stat.st_dev),
                                                   "inode": int(root_stat.st_ino)}
            or adoption.get("database_identity", {}).get("path") != str(database)
            or adoption.get("linked_v3_receipt") != str(linked.absolute())
            or adoption.get("linked_v3_receipt_sha256") != linked_sha
            or adoption.get("candidate_source_identity") != source
            or linked_value.get("database_sha256_after") != adoption.get("database_sha256_preimage")):
        raise click.ClickException("dashboard backlog bootstrap historical adoption mismatch")
    return adoption, adoption_sha, adoptions[0]


def _stopped_dashboard_bootstrap_ancestry(
    root: Path, *, expected_database_sha256: str | None = None, phase: str,
) -> dict[str, Any]:
    """Prove historical chain then apply the phase-specific current DB gate."""
    from agent.governance import db as _db
    adoption, adoption_sha, adoption_path = _historical_dashboard_bootstrap_adoption(root)
    if phase not in {"first_import", "pending_finalize", "completed_replay"}:
        raise click.ClickException("dashboard backlog bootstrap phase is invalid")
    # Only first mutation is allowed through the legacy helper: it demands the
    # stopped current preimage.  Postcommit finalize/replay instead bind current
    # bytes through their own immutable bootstrap receipt below.
    if phase == "first_import":
        linked = Path(str(adoption.get("linked_v3_receipt") or ""))
        try:
            _db._validated_canonical_legacy_postimage_adoption(
                root, linked, _source_git_identity(), _db.verified_stable_database_binding(),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise click.ClickException("dashboard backlog bootstrap preimage adoption mismatch") from exc
    runtime = root / "runtime" / "durable-launch"
    database = root / "governance" / "aming-claw" / "governance.db"
    database_before = _bootstrap_regular_database(database, label="target")
    current_sha = str(expected_database_sha256 or database_before["sha256"])
    if not _exact_sha256(current_sha):
        raise click.ClickException("dashboard backlog bootstrap ancestry database hash is invalid")
    candidates: list[tuple[str, dict[str, Any], str, str]] = []
    for launch_path in runtime.glob("launch.*.json") if runtime.is_dir() else ():
        launch, launch_sha = _read_durable_content_receipt(launch_path, "launch")
        # Cached receipt names never select authority: independently reread
        # every content-addressed member and bind it to today's stopped DB.
        if (launch.get("schema_version") != _AC_DEV_DURABLE_LAUNCH_VERSION
                or launch.get("stage") != "completed"
                or launch.get("project_id") != "aming-claw"
                or launch.get("port") != AC_DEV_SERVICE_PORT
                or launch.get("dev_storage_root") != str(root)
                or launch.get("database_path") != str(database)
                or launch.get("database_sha256_after") != current_sha
                or not _matches_canonical_dev_database_identity(
                    database, launch.get("database_identity")
                )
                or launch.get("policy") != {"runtime_plane": "dev", "migration": "verify-only",
                    "stable_deployment": "deny", "graph_activation": "deny",
                    "background_workers": "deny"}):
            continue
        stops = []
        for stop_path in runtime.glob("stop.*.json"):
            stop, stop_sha = _read_durable_content_receipt(stop_path, "stop")
            if stop.get("launch_sha256") == launch_sha:
                stops.append((stop, stop_sha))
        if len(stops) != 1:
            continue
        stop, stop_sha = stops[0]
        exit_sha = str(stop.get("exit_sha256") or "")
        if not _exact_sha256(exit_sha):
            continue
        exit_value, read_exit_sha = _read_durable_content_receipt(
            runtime / f"exit.{exit_sha[7:]}.json", "exit",
        )
        if (read_exit_sha == exit_sha and exit_value.get("launch_sha256") == launch_sha
                and exit_value.get("binding") == _durable_exit_binding(launch, launch_sha)
                and exit_value.get("challenge_sha256") == stop.get("challenge_sha256")):
            candidates.append((launch_sha, launch, stop_sha, exit_sha))
    if len(candidates) != 1:
        raise click.ClickException(
            "dashboard backlog bootstrap latest stopped generation is missing or ambiguous"
        )
    launch_sha, launch, stop_sha, exit_sha = candidates[0]
    if _durable_listener_pid(AC_DEV_SERVICE_PORT):
        raise click.ClickException("dashboard backlog bootstrap requires a free port 40008")
    try:
        _posix_process_identity(int(launch.get("pid") or 0))
    except click.ClickException:
        pass
    else:
        raise click.ClickException("dashboard backlog bootstrap generation is still live")
    database_after = _bootstrap_regular_database(database, label="target")
    if database_after != database_before:
        raise click.ClickException("dashboard backlog bootstrap database drifted during ancestry read")
    ancestry = {
        "adoption_receipt": str(adoption_path), "adoption_sha256": adoption_sha,
        "launch_sha256": launch_sha, "stop_sha256": stop_sha, "exit_sha256": exit_sha,
        "database_sha256": current_sha, "database_identity": launch.get("database_identity"),
    }
    ancestry["ancestry_sha256"] = "sha256:" + hashlib.sha256(
        _canonical_json_bytes(ancestry)
    ).hexdigest()
    return ancestry


def _offline_dashboard_backlog_bootstrap(
    root: Path, *, project_id: str, port: int, source_database: Path,
) -> dict[str, Any]:
    """Copy only backlog_bugs from one immutable historical DB into stopped canonical dev."""
    from agent.governance import db as _db
    from agent.runtime_plane import resolve_ac_dev_storage_root
    if project_id != "aming-claw" or port != AC_DEV_SERVICE_PORT:
        raise click.ClickException("dashboard backlog bootstrap requires aming-claw on port 40008")
    stable = _db.verified_stable_database_binding()
    expected = resolve_ac_dev_storage_root(Path(str(stable["shared_volume_path"])).resolve(strict=True))
    canonical = root.expanduser().absolute()
    if canonical.is_symlink() or canonical.resolve(strict=True) != canonical or canonical != expected:
        raise click.ClickException("dashboard backlog bootstrap requires the canonical dev root")
    if _port_is_open(AC_DEV_SERVICE_PORT) or _durable_listener_pid(AC_DEV_SERVICE_PORT):
        raise click.ClickException("dashboard backlog bootstrap requires stopped port 40008")
    target = canonical / "governance" / "aming-claw" / "governance.db"
    target_before = _bootstrap_regular_database(target, label="target")
    source_file_before = _bootstrap_regular_database(source_database, label="source")
    source_uri = "file:" + urllib.parse.quote(str(source_database.absolute())) + "?mode=ro&immutable=1"
    source_connection = sqlite3.connect(source_uri, uri=True, timeout=0, isolation_level=None)
    try:
        source_connection.execute("PRAGMA query_only=ON")
        if source_connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise click.ClickException("dashboard backlog bootstrap source quick-check failed")
        if source_connection.execute(
            "SELECT 1 FROM projects WHERE project_id='aming-claw'"
        ).fetchone() is None:
            raise click.ClickException("dashboard backlog bootstrap source project mismatch")
        source_projection, rows = _dashboard_backlog_table_projection(source_connection)
    finally:
        source_connection.close()
    if _bootstrap_regular_database(source_database, label="source") != source_file_before:
        raise click.ClickException("dashboard backlog bootstrap source drifted during projection")
    source_before = {
        **source_file_before,
        "backlog_schema_sha256": source_projection["schema_sha256"],
    }
    receipts_dir = canonical / "archive" / "dashboard-backlog-bootstrap"
    existing = sorted(receipts_dir.glob("bootstrap.*.json")) if receipts_dir.is_dir() else []
    pendings = sorted(receipts_dir.glob("bootstrap-pending.*.json")) if receipts_dir.is_dir() else []
    if existing:
        if len(existing) != 1:
            raise click.ClickException("dashboard backlog bootstrap receipt is ambiguous")
        prior, prior_sha = _read_durable_content_receipt(existing[0], "bootstrap")
        prior_pending_sha = str(prior.get("pending_sha256") or "")
        prior_pending_path = Path(str(prior.get("pending_receipt") or ""))
        try:
            pending_ok = (_exact_sha256(prior_pending_sha)
                          and _read_durable_content_receipt(
                              prior_pending_path, "bootstrap-pending",
                          )[1] == prior_pending_sha)
        except click.ClickException:
            pending_ok = False
        recorded_ancestry = dict(prior.get("ancestry") or {})
        try:
            current_ancestry = _stopped_dashboard_bootstrap_ancestry(
                canonical,
                expected_database_sha256=str(recorded_ancestry.get("database_sha256") or ""),
                phase="completed_replay",
            )
        except click.ClickException:
            current_ancestry = {}
        runtime = canonical / "runtime" / "durable-launch"
        immutable_chain = (
            (Path(str(recorded_ancestry.get("adoption_receipt") or "")), "adoption",
             recorded_ancestry.get("adoption_sha256")),
            (runtime / f"launch.{str(recorded_ancestry.get('launch_sha256') or '')[7:]}.json",
             "launch", recorded_ancestry.get("launch_sha256")),
            (runtime / f"stop.{str(recorded_ancestry.get('stop_sha256') or '')[7:]}.json",
             "stop", recorded_ancestry.get("stop_sha256")),
            (runtime / f"exit.{str(recorded_ancestry.get('exit_sha256') or '')[7:]}.json",
             "exit", recorded_ancestry.get("exit_sha256")),
        )
        try:
            chain_ok = all(
                _read_durable_content_receipt(path, prefix)[1] == digest
                for path, prefix, digest in immutable_chain if _exact_sha256(str(digest or ""))
            ) and all(_exact_sha256(str(digest or "")) for _path, _prefix, digest in immutable_chain)
        except click.ClickException:
            chain_ok = False
        if (not pending_ok or not chain_ok or current_ancestry != recorded_ancestry
                or prior.get("source_identity") != source_before
                or prior.get("source_projection") != source_projection
                or prior.get("target_database_sha256_after") != target_before["sha256"]
                or not _matches_canonical_dev_database_identity(
                    target, recorded_ancestry.get("database_identity")
                )):
            raise click.ClickException("dashboard backlog bootstrap replay drift")
        # v1 receipts predate the split inventory bindings.  New receipts bind
        # the five managed objects and every protected object independently;
        # never reinterpret legacy receipt bytes as the newer schema.
        replay_connection = sqlite3.connect(
            "file:" + urllib.parse.quote(str(target)) + "?mode=ro&immutable=1", uri=True,
        )
        try:
            if ("managed_inventory_after" in prior
                    and _db.backlog_read_schema_managed_inventory(replay_connection)
                    != prior.get("managed_inventory_after")):
                raise click.ClickException("dashboard backlog bootstrap replay managed schema drift")
            if ("protected_inventory_after" in prior
                    and _db.backlog_read_schema_protected_inventory(replay_connection)
                    != prior.get("protected_inventory_after")):
                raise click.ClickException("dashboard backlog bootstrap replay protected schema drift")
            if ("target_database_identity_v2_after" in prior
                    and not _matches_canonical_dev_database_identity(
                        target, prior.get("target_database_identity_v2_after"))):
                raise click.ClickException("dashboard backlog bootstrap replay v2 identity drift")
        finally:
            replay_connection.close()
        return {"status": "already_bootstrapped", "receipt": str(existing[0]),
                "receipt_sha256": prior_sha, "row_count": source_projection["row_count"]}
    if len(pendings) > 1:
        raise click.ClickException("dashboard backlog bootstrap pending receipt is ambiguous")
    pending: dict[str, Any] | None = None
    pending_path: Path | None = None
    pending_sha = ""
    if pendings:
        pending_path = pendings[0]
        pending, pending_sha = _read_durable_content_receipt(
            pending_path, "bootstrap-pending",
        )
        if (pending.get("schema_version") != "ac_dev_dashboard_backlog_bootstrap_pending.v1"
                or pending.get("stage") != "pending"
                or pending.get("source_identity") != source_before
                or pending.get("source_projection") != source_projection
                or pending.get("target_database_identity") != _admission_identity(target)):
            raise click.ClickException("dashboard backlog bootstrap pending receipt mismatch")
        if ("target_database_identity_v2_before" in pending
                and not _matches_canonical_dev_database_identity(
                    target, pending.get("target_database_identity_v2_before"))):
            raise click.ClickException("dashboard backlog bootstrap pending v2 identity mismatch")
        pending_backup = dict(pending.get("backup") or {})
        pending_backup_path = Path(str(pending_backup.get("path") or ""))
        if (pending_backup_path.is_symlink() or not pending_backup_path.is_file()
                or _admission_identity(pending_backup_path) != pending_backup.get("identity")
                or _file_sha256(pending_backup_path) != pending_backup.get("sha256")):
            raise click.ClickException("dashboard backlog bootstrap pending backup mismatch")
    if pending:
        ancestry = dict(pending.get("ancestry") or {})
        try:
            if _stopped_dashboard_bootstrap_ancestry(
                canonical,
                expected_database_sha256=str(ancestry.get("database_sha256") or ""),
                phase="pending_finalize",
            ) != ancestry:
                raise click.ClickException("dashboard backlog bootstrap pending ancestry HOLD")
        except click.ClickException:
            raise
    else:
        ancestry = _stopped_dashboard_bootstrap_ancestry(canonical, phase="first_import")
    connection = sqlite3.connect(str(target), timeout=5, isolation_level=None)
    preimage_sha = str(pending.get("target_database_sha256_before") or "") if pending else target_before["sha256"]
    backup = receipts_dir / f"{preimage_sha[7:]}.pre.sqlite"
    try:
        target_projection, target_rows = _dashboard_backlog_table_projection(connection)
        observed_inventory = _db.backlog_read_schema_inventory(connection)
        managed_inventory = _db.backlog_read_schema_managed_inventory(connection)
        protected_inventory = _db.backlog_read_schema_protected_inventory(connection)
        target_identity_v2 = _canonical_dev_database_identity_projection(target)
        protected_projection = _db._sqlite_logical_projection(
            connection,
            exclude_tables=frozenset({"backlog_bugs", "dashboard_backlog_cache_generation"}),
        )
        if pending and target_before["sha256"] != preimage_sha:
            expected = dict(pending.get("expected_postimage") or {})
            expected_managed = expected.get("managed_inventory_after")
            expected_protected = expected.get("protected_inventory_after")
            expected_v2 = expected.get("target_database_identity_v2_after")
            if (target_projection != source_projection
                    or (expected_managed is not None and managed_inventory != expected_managed)
                    or (expected_managed is None and observed_inventory != expected.get("target_inventory_after"))
                    or (expected_protected is not None and protected_inventory != expected_protected)
                    or (expected_v2 is not None and target_identity_v2 != expected_v2)
                    or protected_projection != pending.get("protected_projection_before")):
                raise click.ClickException("dashboard backlog bootstrap pending recovery HOLD")
            target_after = target_before
            payload = {
                "schema_version": "ac_dev_dashboard_backlog_bootstrap.v1", "stage": "completed",
                "project_id": project_id, "port": port, "pending_receipt": str(pending_path),
                "pending_sha256": pending_sha, "source_identity": source_before,
                "source_projection": source_projection,
                "target_identity": {"before": pending["target_identity_before"], "after": target_after},
                "target_database_sha256_before": preimage_sha,
                "target_database_sha256_after": target_after["sha256"],
                "target_database_identity_v2_after": target_identity_v2,
                "target_inventory_before": pending["target_inventory_before"],
                "target_inventory_after": observed_inventory,
                "managed_inventory_after": managed_inventory,
                "protected_inventory_after": protected_inventory,
                "admission": pending["expected_admission"], "ancestry": ancestry,
                "backup": pending["backup"], "row_count": source_projection["row_count"],
                "status_counts": source_projection["status_counts"],
                "recovery_mode": "finalized_exact_postimage",
            }
            connection.close(); connection = None
            receipt, receipt_sha = _durable_content_receipt(receipts_dir, "bootstrap", payload)
            return {"status": "finalized", "receipt": str(receipt),
                    "receipt_sha256": receipt_sha, "row_count": source_projection["row_count"]}
        if target_rows or target_projection["schema"] != source_projection["schema"]:
            raise click.ClickException("dashboard backlog bootstrap target is not pristine")
        target_inventory_before = observed_inventory
        protected_inventory_before = protected_inventory
        target_identity_v2_before = target_identity_v2
        drift = _db.backlog_read_schema_drift(connection)
        if drift["invalid"]:
            raise click.ClickException("dashboard backlog bootstrap target schema mismatch")
        archive_root = receipts_dir.parent
        if archive_root.is_symlink() or not archive_root.is_dir():
            raise click.ClickException("dashboard backlog bootstrap archive is invalid")
        receipts_dir.mkdir(parents=True, exist_ok=True)
        if receipts_dir.is_symlink() or not receipts_dir.is_dir():
            raise click.ClickException("dashboard backlog bootstrap receipt directory is invalid")
        if not pending:
            if backup.exists() or backup.is_symlink():
                raise click.ClickException("dashboard backlog bootstrap backup collision")
            shutil.copy2(target, backup)
        if _file_sha256(backup) != target_before["sha256"]:
            raise click.ClickException("dashboard backlog bootstrap backup mismatch")
        if not pending:
            expected_managed_after = _db.canonical_backlog_read_schema_managed_inventory()
            pending_payload = {
                "schema_version": "ac_dev_dashboard_backlog_bootstrap_pending.v1",
                "stage": "pending", "project_id": project_id, "port": port,
                "source_identity": source_before, "source_projection": source_projection,
                "target_database_identity": _admission_identity(target),
                "target_database_identity_v2_before": target_identity_v2_before,
                "target_identity_before": target_before,
                "target_database_sha256_before": target_before["sha256"],
                "target_inventory_before": target_inventory_before,
                "protected_projection_before": protected_projection,
                "protected_inventory_before": protected_inventory_before,
                "backup": {"path": str(backup), "identity": _admission_identity(backup),
                           "sha256": _file_sha256(backup)},
                "ancestry": ancestry, "schema_plan": _db.backlog_read_schema_plan(),
                "expected_admission": {"changed": bool(drift["missing"]),
                                       "missing": drift["missing"]},
                "expected_postimage": {"source_projection": source_projection,
                                       "target_inventory_after": None,
                                       "managed_inventory_after": expected_managed_after,
                                       "protected_inventory_after": protected_inventory_before,
                                       "target_database_identity_v2_after": target_identity_v2_before,
                                       "recovery_modes": ["retry_exact_preimage", "finalize_exact_postimage"]},
            }
            pending_path, pending_sha = _durable_content_receipt(
                receipts_dir, "bootstrap-pending", pending_payload,
            )
            pending = pending_payload
        connection.execute("BEGIN IMMEDIATE")
        admission = _db.admit_missing_backlog_read_schema(connection, commit=False)
        columns = list(source_projection["columns"])
        quoted = ",".join(_db._sqlite_quote_identifier(name) for name in columns)
        placeholders = ",".join("?" for _ in columns)
        connection.executemany(
            f"INSERT INTO \"backlog_bugs\" ({quoted}) VALUES ({placeholders})", rows,
        )
        after_projection, _ = _dashboard_backlog_table_projection(connection)
        protected_after = _db._sqlite_logical_projection(
            connection,
            exclude_tables=frozenset({"backlog_bugs", "dashboard_backlog_cache_generation"}),
        )
        managed_after = _db.backlog_read_schema_managed_inventory(connection)
        protected_inventory_after = _db.backlog_read_schema_protected_inventory(connection)
        expected_managed_after = pending["expected_postimage"].get("managed_inventory_after")
        if (after_projection != source_projection or protected_after != protected_projection
                or (expected_managed_after is not None and managed_after != expected_managed_after)
                or protected_inventory_after != protected_inventory_before):
            raise click.ClickException("dashboard backlog bootstrap transaction postcondition failed")
        connection.commit()
        checkpoint = tuple(int(value) for value in connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone())
        if checkpoint != (0, 0, 0):
            raise click.ClickException("dashboard backlog bootstrap checkpoint failed")
    except BaseException:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()
    target_after = _bootstrap_regular_database(target, label="target")
    target_identity_v2_after = _canonical_dev_database_identity_projection(target)
    inventory_connection = sqlite3.connect(
        "file:" + urllib.parse.quote(str(target)) + "?mode=ro&immutable=1", uri=True,
    )
    try:
        target_inventory_after = _db.backlog_read_schema_inventory(inventory_connection)
        managed_inventory_after = _db.backlog_read_schema_managed_inventory(inventory_connection)
        protected_inventory_after = _db.backlog_read_schema_protected_inventory(inventory_connection)
    finally:
        inventory_connection.close()
    payload = {
        "schema_version": "ac_dev_dashboard_backlog_bootstrap.v1", "stage": "completed",
        "project_id": project_id, "port": port, "pending_receipt": str(pending_path),
        "pending_sha256": pending_sha, "source_identity": source_before,
        "source_projection": source_projection, "target_identity": {
            "before": target_before, "after": target_after,
        }, "target_database_sha256_before": target_before["sha256"],
        "target_database_sha256_after": target_after["sha256"],
        "target_database_identity_v2_after": target_identity_v2_after,
        "target_inventory_before": target_inventory_before,
        "target_inventory_after": target_inventory_after,
        "managed_inventory_after": managed_inventory_after,
        "protected_inventory_after": protected_inventory_after,
        "admission": admission, "ancestry": ancestry,
        "backup": pending["backup"],
        "row_count": source_projection["row_count"],
        "status_counts": source_projection["status_counts"],
    }
    receipt, receipt_sha = _durable_content_receipt(receipts_dir, "bootstrap", payload)
    return {"status": "bootstrapped", "receipt": str(receipt),
            "receipt_sha256": receipt_sha, "row_count": source_projection["row_count"]}


def _completed_dashboard_bootstrap_binding(root: Path) -> dict[str, Any] | None:
    """Return a receipt-bound postimage for durable start, or None if absent."""
    from agent.governance import db as _db
    receipts_dir = root / "archive" / "dashboard-backlog-bootstrap"
    if not receipts_dir.exists():
        return None
    if receipts_dir.is_symlink() or not receipts_dir.is_dir():
        raise click.ClickException("dashboard backlog bootstrap archive is invalid")
    completed = sorted(receipts_dir.glob("bootstrap.*.json"))
    pendings = sorted(receipts_dir.glob("bootstrap-pending.*.json"))
    if len(completed) != 1 or len(pendings) != 1:
        raise click.ClickException("dashboard backlog bootstrap start receipts are missing or ambiguous")
    receipt, receipt_sha = _read_durable_content_receipt(completed[0], "bootstrap")
    pending_path = Path(str(receipt.get("pending_receipt") or ""))
    pending, pending_sha = _read_durable_content_receipt(pending_path, "bootstrap-pending")
    if (pending_path != pendings[0] or receipt.get("pending_sha256") != pending_sha
            or receipt.get("stage") != "completed" or pending.get("stage") != "pending"):
        raise click.ClickException("dashboard backlog bootstrap start pending binding mismatch")
    ancestry = dict(receipt.get("ancestry") or {})
    if _stopped_dashboard_bootstrap_ancestry(
        root, expected_database_sha256=str(ancestry.get("database_sha256") or ""),
        phase="completed_replay",
    ) != ancestry:
        raise click.ClickException("dashboard backlog bootstrap start ancestry mismatch")
    database = root / "governance" / "aming-claw" / "governance.db"
    current = _bootstrap_regular_database(database, label="target")
    if (receipt.get("target_database_sha256_after") != current["sha256"]
            or not _matches_canonical_dev_database_identity(
                database, receipt.get("target_database_identity_v2_after"))):
        raise click.ClickException("dashboard backlog bootstrap start postimage mismatch")
    connection = sqlite3.connect(
        "file:" + urllib.parse.quote(str(database)) + "?mode=ro&immutable=1", uri=True,
    )
    try:
        projection, _rows = _dashboard_backlog_table_projection(connection)
        if (projection != receipt.get("source_projection")
                or _db.backlog_read_schema_managed_inventory(connection)
                != receipt.get("managed_inventory_after")
                or _db.backlog_read_schema_protected_inventory(connection)
                != receipt.get("protected_inventory_after")):
            raise click.ClickException("dashboard backlog bootstrap start projection mismatch")
    finally:
        connection.close()
    return {"database_path": str(database), "database_identity": _admission_identity(database),
            "database_sha256": current["sha256"], "bootstrap_receipt": str(completed[0]),
            "bootstrap_receipt_sha256": receipt_sha, "bootstrap_pending": str(pending_path),
            "bootstrap_pending_sha256": pending_sha,
            "historical_launch_sha256": ancestry.get("launch_sha256"),
            "historical_stop_sha256": ancestry.get("stop_sha256"),
            "historical_exit_sha256": ancestry.get("exit_sha256"),
            "historical_database_sha256": ancestry.get("database_sha256"),
            "historical_database_postimage": pending.get("target_database_sha256_before"),
            "historical_launch_id": None}


def _durable_start_phase(root: Path) -> tuple[str, dict[str, Any] | None]:
    """Closed durable-start phase selection; artifacts may never fall back."""
    archive = root / "archive" / "dashboard-backlog-bootstrap"
    if archive.exists() or archive.is_symlink():
        return _DURABLE_START_COMPLETED_BOOTSTRAP, _completed_dashboard_bootstrap_binding(root)
    return _DURABLE_START_LEGACY_ADOPTION, None


@main.command("dev-bootstrap-dashboard-backlog")
@click.option("--dev-storage-root", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--project-id", required=True)
@click.option("--port", required=True, type=int)
@click.option("--source-database", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
def dev_bootstrap_dashboard_backlog(
    dev_storage_root: Path, project_id: str, port: int, source_database: Path,
) -> None:
    click.echo(json.dumps(_offline_dashboard_backlog_bootstrap(
        dev_storage_root, project_id=project_id, port=port,
        source_database=source_database,
    ), sort_keys=True))


@main.command("dev-create-cow-successor-receipt")
@click.option("--dev-storage-root", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--operator-receipt", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--predecessor-backup", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--linked-v3-receipt", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
def dev_create_cow_successor_receipt(
    dev_storage_root: Path, operator_receipt: Path, predecessor_backup: Path,
    linked_v3_receipt: Path,
) -> None:
    """Seal one source-validated COW physical successor for the AC dev DB."""
    from agent.governance import db as _db
    try:
        result = _db.create_dev_cow_successor_receipt(
            dev_storage_root, operator_receipt=operator_receipt,
            predecessor_backup=predecessor_backup,
            linked_v3_receipt=linked_v3_receipt,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, sort_keys=True))


def _posix_exclusive_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise click.ClickException("AC dev durable launch path is not canonical")
    raw = _canonical_json_bytes(payload)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise click.ClickException("AC dev durable launch receipt collision") from exc
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _noreplace_promote(source: Path, destination: Path) -> None:
    """Promote one fsynced inode without permitting destination replacement."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        result = libc.renamex_np(source_bytes, destination_bytes, 0x00000004)  # RENAME_EXCL
    elif hasattr(libc, "renameat2"):
        result = libc.renameat2(-100, source_bytes, -100, destination_bytes, 1)  # RENAME_NOREPLACE
    else:
        raise click.ClickException("AC dev durable no-replace rename is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        raise click.ClickException("AC dev durable launch receipt collision") from OSError(
            error, os.strerror(error), destination,
        )
    _fsync_parent(destination)


def _posix_process_identity(pid: int) -> dict[str, str]:
    start = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
        text=True, timeout=3, check=False,
    ).stdout.strip()
    argv = subprocess.run(
        ["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True,
        text=True, timeout=3, check=False,
    ).stdout.strip()
    cwd_result = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        capture_output=True, text=True, timeout=3, check=False,
    )
    cwd = next((line[1:] for line in cwd_result.stdout.splitlines() if line.startswith("n")), "")
    if not start or not argv or not cwd:
        raise click.ClickException("AC dev durable process identity is unavailable")
    return {
        "start_identity": "sha256:" + hashlib.sha256(start.encode("utf-8")).hexdigest(),
        "argv": argv,
        "cwd": str(Path(cwd).resolve(strict=True)),
    }


def _durable_listener_pid(port: int) -> int:
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True, text=True, timeout=3, check=False,
    )
    pids = {int(line) for line in result.stdout.splitlines() if line.isdigit()}
    return next(iter(pids)) if len(pids) == 1 else 0


def _require_first_cow_runtime_pristine(dev_storage: Path) -> None:
    """Admit only an untouched first-start runtime; archives are out of scope."""
    runtime = (dev_storage / "runtime" / "durable-launch").absolute()
    if not runtime.exists():
        return
    try:
        metadata = runtime.stat(follow_symlinks=False)
    except OSError as exc:
        raise click.ClickException("AC dev first-start runtime is invalid") from exc
    if (runtime.is_symlink() or not stat.S_ISDIR(metadata.st_mode)
            or runtime.resolve(strict=True) != runtime
            or runtime.parent.is_symlink() or any(runtime.iterdir())):
        raise click.ClickException("AC dev first-start runtime must be pristine")


def _validated_linked_v3_receipt(
    receipt_path: Path, *, dev_storage: Path, database: Path,
    database_identity: Mapping[str, object], source_identity: Mapping[str, object],
    allow_postimage: bool = False, durable_start_phase: str = _DURABLE_START_LEGACY_ADOPTION,
) -> tuple[str, dict[str, Any]]:
    if durable_start_phase not in {_DURABLE_START_LEGACY_ADOPTION, _DURABLE_START_COMPLETED_BOOTSTRAP}:
        raise click.ClickException("AC dev durable launch phase is invalid")
    archive = dev_storage / "archive" / "schema-admission"
    receipt, digest = _read_admission_receipt(receipt_path.absolute(), archive=archive)
    from agent.governance import db as _db
    plan_sha256 = "sha256:" + hashlib.sha256(
        _canonical_json_bytes(_db.authority_projection_schema_plan())
    ).hexdigest()
    receipt_source = _validated_historical_admission_source_identity(
        receipt.get("source_identity")
    )
    historical_source = dict(receipt_source.get("cli_source") or {})
    canonical_database_identity = _admission_identity(database)
    linked_database_identity = dict(receipt.get("database_identity") or {})
    if (linked_database_identity.get("device"), linked_database_identity.get("inode")) != (
            canonical_database_identity.get("device"), canonical_database_identity.get("inode")):
        if durable_start_phase == _DURABLE_START_COMPLETED_BOOTSTRAP:
            _historical_dashboard_bootstrap_adoption(dev_storage)
            _db.validate_dev_cow_completed_generation_projection(
                dev_storage, linked_v3_receipt=receipt_path,
                source_identity=source_identity,
                stable_binding=_db.verified_stable_database_binding(),
            )
        else:
            _require_first_cow_runtime_pristine(dev_storage)
            _db.validate_dev_cow_successor_preimage(
                dev_storage, linked_v3_receipt=receipt_path,
                source_identity=source_identity,
                stable_binding=_db.verified_stable_database_binding(),
            )
        return digest, receipt
    if durable_start_phase == _DURABLE_START_COMPLETED_BOOTSTRAP:
        # Bootstrap owns the current postimage; this re-authenticates only the
        # sealed historical bridge and deliberately never asks legacy code to
        # compare today's logical database with adoption-era bytes.
        _historical_dashboard_bootstrap_adoption(dev_storage)
    if historical_source != dict(source_identity):
        adoptions = _canonical_adoption_receipts(dev_storage)
        runtime = dev_storage / "runtime" / "durable-launch"
        if durable_start_phase == _DURABLE_START_COMPLETED_BOOTSTRAP:
            _historical_dashboard_bootstrap_adoption(dev_storage)
        elif adoptions:
            if len(adoptions) != 1:
                raise click.ClickException(
                    "AC dev durable launch canonical adoption is missing or ambiguous"
                )
            try:
                _db._validated_canonical_legacy_postimage_adoption(
                    dev_storage, receipt_path.absolute(), source_identity,
                    _db.verified_stable_database_binding(),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                raise click.ClickException(
                    "AC dev durable launch canonical adoption mismatch"
                ) from exc
        else:
            # Before the first completed generation, replay remains byte-exact
            # against the legacy postimage and retains the free-port/no-holder
            # admission gates.
            _canonical_legacy_postimage_adoption(
                dev_storage, linked_v3_receipt=receipt_path,
                source_identity=source_identity,
            )
        if len(adoptions) != 1:
            adoptions = _canonical_adoption_receipts(dev_storage)
        if len(adoptions) != 1:
            raise click.ClickException("AC dev durable launch linked-v3 source mismatch")
        adoption, _adoption_digest = _read_canonical_adoption_receipt(adoptions[0])
        if (adoption.get("schema_version")
                != _AC_DEV_CANONICAL_LEGACY_POSTIMAGE_ADOPTION_VERSION
                or adoption.get("stage") != "completed"
                or adoption.get("receipt_source_identity") != historical_source
                or adoption.get("candidate_source_identity") != dict(source_identity)
                or adoption.get("linked_v3_receipt") != str(receipt_path.absolute())
                or adoption.get("linked_v3_receipt_sha256") != digest):
            raise click.ClickException("AC dev durable launch canonical adoption mismatch")
    _validated_authority_receipt_inventory(
        receipt.get("schema_inventory_after"), db_module=_db,
    )
    root_identity = _admission_identity(dev_storage)
    if (
        receipt.get("schema_version") != _AC_DEV_SCHEMA_RECERTIFICATION_RECEIPT_VERSION
        or receipt.get("stage") != "completed" or receipt.get("changed") is not False
        or receipt.get("project_id") != "aming-claw" or receipt.get("port") != AC_DEV_SERVICE_PORT
        or receipt.get("root_identity") != root_identity
        or receipt.get("database_identity") != canonical_database_identity
        or database_identity.get("device") != canonical_database_identity["device"]
        or database_identity.get("inode") != canonical_database_identity["inode"]
        or receipt.get("plan_sha256") != plan_sha256
        or (not allow_postimage and receipt.get("database_sha256_after")
            != _admission_database_sha256(
                database, expected_identity=canonical_database_identity,
            ))
    ):
        raise click.ClickException("AC dev durable launch linked-v3 receipt mismatch")
    _validate_admission_receipt_chain(
        receipt, digest, archive=archive, project_id="aming-claw", port=AC_DEV_SERVICE_PORT,
        root_identity=root_identity, database_identity=canonical_database_identity,
        source_identity=receipt_source, plan_sha256=plan_sha256,
    )
    return digest, receipt


def _durable_child_exit(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name("." + path.name + f".{os.getpid()}.tmp")
    _posix_exclusive_json(temporary, payload)
    if path.exists() or path.is_symlink():
        raise click.ClickException("AC dev durable exit receipt collision")
    _noreplace_promote(temporary, path)


def _durable_receipt_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _durable_content_receipt(directory: Path, prefix: str, payload: Mapping[str, Any]) -> tuple[Path, str]:
    raw = _canonical_json_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    destination = directory / f"{prefix}.{digest}.json"
    temporary = directory / f".{prefix}.{secrets.token_hex(16)}.pending"
    _posix_exclusive_json(temporary, payload)
    _noreplace_promote(temporary, destination)
    return destination, "sha256:" + digest


def _read_durable_content_receipt(path: Path, prefix: str) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise click.ClickException(f"AC dev durable {prefix} receipt is not canonical")
    match = re.fullmatch(rf"{re.escape(prefix)}\.([0-9a-f]{{64}})\.json", path.name)
    if match is None:
        raise click.ClickException(f"AC dev durable {prefix} receipt has noncanonical name")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != match.group(1):
        raise click.ClickException(f"AC dev durable {prefix} receipt digest mismatch")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise click.ClickException(f"AC dev durable {prefix} receipt is malformed") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"AC dev durable {prefix} receipt is malformed")
    return payload, "sha256:" + digest


def _validated_durable_runtime_dir(requested: Path, dev_storage: Path) -> Path:
    requested_absolute = requested.absolute()
    expected_absolute = (dev_storage / "runtime" / "durable-launch").absolute()
    try:
        resolved = requested_absolute.resolve(strict=True)
        expected = expected_absolute.resolve(strict=True)
    except OSError as exc:
        raise click.ClickException("AC dev durable child runtime is unavailable") from exc
    if (requested_absolute.is_symlink() or resolved != expected
            or resolved.parent.is_symlink() or dev_storage.absolute().is_symlink()):
        raise click.ClickException("AC dev durable child runtime is outside its bound dev root")
    return resolved


def _durable_exit_binding(receipt: Mapping[str, Any], receipt_sha256: str) -> dict[str, Any]:
    process = receipt["process"]
    return {
        "launch_id": receipt["launch_id"], "completed_receipt_sha256": receipt_sha256,
        "pid": receipt["pid"], "start_identity": process["start_identity"],
        "argv_sha256": "sha256:" + hashlib.sha256(
            _canonical_json_bytes(receipt["argv"])
        ).hexdigest(),
        "cwd": receipt["cwd"], "python": receipt["python"],
        "source_commit": receipt["source_commit"], "source_tree": receipt["source_tree"],
        "server_sha256": receipt["server_sha256"], "source_root": receipt["source_root"],
        "database_path": receipt["database_path"], "database_identity": receipt["database_identity"],
        "dev_storage_root": receipt["dev_storage_root"], "project_id": receipt["project_id"],
        "port": receipt["port"], "policy": receipt["policy"],
        "linked_v3_receipt_sha256": receipt["linked_v3_receipt_sha256"],
        "log_path": receipt["log_path"], "log_identity": receipt["log_identity"],
        "readiness_sha256": receipt["readiness_sha256"],
        "database_sha256_before": receipt["database_sha256_before"],
        "database_sha256_after": receipt["database_sha256_after"],
    }


def _posix_detached_popen(
    argv: list[str], *, cwd: Path, log_fd: int, pass_fds: tuple[int, ...] = (),
) -> subprocess.Popen:
    """The sole no-shell/session-detached child creation primitive."""
    return subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
        start_new_session=True, shell=False, close_fds=True, pass_fds=pass_fds,
    )


def _durable_dev_launch(
    *, dev_storage: Path, database: Path, database_identity: Mapping[str, object],
    source_identity: Mapping[str, object], stable_anchor_commit: str,
    linked_receipt: Path,
) -> None:
    if os.name != "posix":
        raise click.ClickException("AC dev durable launch requires POSIX")
    runtime = dev_storage / "runtime" / "durable-launch"
    lock = runtime / "launch.lock"
    durable_phase, bootstrap_binding = _durable_start_phase(dev_storage)
    if durable_phase == _DURABLE_START_COMPLETED_BOOTSTRAP and bootstrap_binding is None:
        raise click.ClickException("dashboard backlog bootstrap durable phase is incomplete")
    linked_digest, _linked = _validated_linked_v3_receipt(
        linked_receipt, dev_storage=dev_storage, database=database,
        database_identity=database_identity, source_identity=source_identity,
        allow_postimage=True, durable_start_phase=durable_phase,
    )
    runtime.mkdir(parents=True, exist_ok=True)
    if bootstrap_binding is None:
        from agent.governance.db import validate_dev_preimage_only
        preimage = validate_dev_preimage_only(
            dev_storage, source_identity=source_identity,
            linked_v3_receipt=linked_receipt,
        )
    else:
        preimage = bootstrap_binding
    source_root = Path(str(source_identity["root"])).resolve(strict=True)
    server = source_root / "agent" / "governance" / "server.py"
    server_sha = "sha256:" + hashlib.sha256(server.read_bytes()).hexdigest()

    def current_database_sha256() -> str:
        before = _admission_identity(database)
        digest = _file_sha256(database)
        if _admission_identity(database) != before:
            raise click.ClickException("AC dev durable database identity changed during hash")
        return digest

    def validate_live_generation(
        path: Path, value: Mapping[str, Any], digest: str,
        process: Mapping[str, str], health: Mapping[str, Any],
    ) -> None:
        pid = int(value.get("pid") or 0)
        pending_sha = str(value.get("pending_sha256") or "")
        readiness_sha = str(value.get("readiness_sha256") or "")
        if not (_exact_sha256(pending_sha) and _exact_sha256(readiness_sha)):
            raise click.ClickException("AC dev durable live receipt chain mismatch")
        pending_path = runtime / f"pending.{pending_sha[7:]}.json"
        readiness_path = runtime / f"readiness.{readiness_sha[7:]}.json"
        pending, pending_read_sha = _read_durable_content_receipt(pending_path, "pending")
        readiness, readiness_read_sha = _read_durable_content_receipt(readiness_path, "readiness")
        stopped = []
        for stop_path in runtime.glob("stop.*.json"):
            stop, _ = _read_durable_content_receipt(stop_path, "stop")
            if stop.get("launch_sha256") == digest:
                stopped.append(stop_path)
        exited = []
        for exit_path in runtime.glob("exit.*.json"):
            exit_value, _ = _read_durable_content_receipt(exit_path, "exit")
            if exit_value.get("launch_sha256") == digest:
                exited.append(exit_path)
        expected_policy = {"runtime_plane": "dev", "migration": "verify-only",
                           "stable_deployment": "deny", "graph_activation": "deny",
                           "background_workers": "deny"}
        custody = dict(preimage.get("custody_projection") or {})
        if (
            value.get("schema_version") != _AC_DEV_DURABLE_LAUNCH_VERSION
            or value.get("stage") != "completed" or value.get("pid") != pid
            or value.get("process") != dict(process)
            or value.get("argv") is None or value.get("cwd") != str(source_root)
            or process.get("cwd") != str(source_root)
            or value.get("source_root") != str(source_root)
            or value.get("source_commit") != source_identity.get("commit")
            or value.get("source_tree") != source_identity.get("tree")
            or value.get("server_sha256") != server_sha
            or value.get("dev_storage_root") != str(dev_storage)
            or value.get("database_path") != str(database)
            or dict(value.get("database_identity") or {}) != health.get("runtime_plane_identity", {}).get("database_identity")
            or value.get("database_sha256_after") != current_database_sha256()
            or value.get("project_id") != "aming-claw" or value.get("port") != AC_DEV_SERVICE_PORT
            or value.get("policy") != expected_policy
            or value.get("linked_v3_receipt_sha256") != linked_digest
            or pending_read_sha != pending_sha or readiness_read_sha != readiness_sha
            or pending.get("launch_id") != value.get("launch_id")
            or pending.get("linked_v3_receipt_sha256") != linked_digest
            or pending.get("database_sha256_before") != value.get("database_sha256_before")
            or readiness.get("pending_sha256") != pending_sha
            or readiness.get("launch_id") != value.get("launch_id")
            or readiness.get("pid") != pid
            or readiness.get("database_sha256_before") != value.get("database_sha256_before")
            or readiness.get("database_sha256_after") != value.get("database_sha256_after")
            or readiness.get("database_identity") != value.get("database_identity")
            or readiness.get("custody_projection") != custody
            or custody.get("pid") != pid or custody.get("launch_id") != value.get("launch_id")
            or value.get("launch_id") not in str(process.get("argv") or "")
            or stopped or exited
            or not _dev_running_identity_matches(
                health, source_identity, stable_anchor_commit=stable_anchor_commit,
                dev_database_identity=dict(value.get("database_identity") or {}),
            )
        ):
            raise click.ClickException("AC dev durable live completed receipt mismatch")

    suspicious = list(runtime.glob(".launch*.json"))
    if suspicious:
        raise click.ClickException("AC dev durable hidden launch receipt state")
    existing_launches = []
    for path in runtime.glob("launch.*.json"):
        value, digest = _read_durable_content_receipt(path, "launch")
        existing_launches.append((path, value, digest))
    expected_policy = {"runtime_plane": "dev", "migration": "verify-only",
                       "stable_deployment": "deny", "graph_activation": "deny",
                       "background_workers": "deny"}

    def completed_chain_is_valid(value: Mapping[str, Any]) -> bool:
        pending_sha = str(value.get("pending_sha256") or "")
        readiness_sha = str(value.get("readiness_sha256") or "")
        if not (_exact_sha256(pending_sha) and _exact_sha256(readiness_sha)):
            return False
        pending, pending_read = _read_durable_content_receipt(
            runtime / f"pending.{pending_sha[7:]}.json", "pending",
        )
        readiness, readiness_read = _read_durable_content_receipt(
            runtime / f"readiness.{readiness_sha[7:]}.json", "readiness",
        )
        return bool(
            value.get("schema_version") == _AC_DEV_DURABLE_LAUNCH_VERSION
            and value.get("stage") == "completed"
            and value.get("source_root") == str(source_root)
            and value.get("source_commit") == source_identity.get("commit")
            and value.get("source_tree") == source_identity.get("tree")
            and value.get("server_sha256") == server_sha
            and value.get("dev_storage_root") == str(dev_storage)
            and value.get("database_path") == str(database)
            and isinstance(value.get("database_identity"), dict)
            and value.get("database_identity") == readiness.get("database_identity")
            and value.get("database_identity", {}).get("device") == _admission_identity(database)["device"]
            and value.get("database_identity", {}).get("inode") == _admission_identity(database)["inode"]
            and value.get("project_id") == "aming-claw"
            and value.get("port") == AC_DEV_SERVICE_PORT
            and value.get("policy") == expected_policy
            and value.get("linked_v3_receipt_sha256") == linked_digest
            and pending_read == pending_sha and readiness_read == readiness_sha
            and pending.get("launch_id") == value.get("launch_id")
            and pending.get("linked_v3_receipt_sha256") == linked_digest
            and readiness.get("pending_sha256") == pending_sha
            and readiness.get("launch_id") == value.get("launch_id")
            and readiness.get("pid") == value.get("pid")
            and readiness.get("database_sha256_after") == value.get("database_sha256_after")
        )

    stop_values = []
    for path in runtime.glob("stop.*.json"):
        value, digest = _read_durable_content_receipt(path, "stop")
        stop_values.append((value, digest))
    exit_values = {}
    for path in runtime.glob("exit.*.json"):
        value, digest = _read_durable_content_receipt(path, "exit")
        exit_values[digest] = value
    abnormal_values = []
    for path in runtime.glob("abnormal.*.json"):
        value, digest = _read_durable_content_receipt(path, "abnormal")
        abnormal_values.append((value, digest))

    invalid_generations = []
    dead_unsealed = []
    for path, value, digest in existing_launches:
        try:
            valid_chain = completed_chain_is_valid(value)
        except click.ClickException:
            valid_chain = False
        if not valid_chain:
            invalid_generations.append(path)
            continue
        process = None
        try:
            process = _posix_process_identity(int(value.get("pid") or 0))
        except click.ClickException:
            pass
        if process is not None:
            continue
        stops = [stop for stop, _ in stop_values if stop.get("launch_sha256") == digest]
        stopped = False
        if len(stops) == 1:
            exit_value = exit_values.get(str(stops[0].get("exit_sha256") or ""))
            stopped = bool(
                exit_value
                and exit_value.get("launch_sha256") == digest
                and exit_value.get("binding") == _durable_exit_binding(value, digest)
                and exit_value.get("challenge_sha256") == stops[0].get("challenge_sha256")
            )
        seals = [seal for seal, _ in abnormal_values
                 if seal.get("launch_sha256") == digest]
        sealed = len(seals) == 1 and seals[0].get("stage") == "completed_child_absent"
        if len(stops) > 1 or len(seals) > 1 or (stops and not stopped) or (seals and not sealed):
            invalid_generations.append(path)
        elif not stopped and not sealed:
            dead_unsealed.append((path, value, digest))
    if invalid_generations:
        raise click.ClickException("AC dev durable completed generation is unclassifiable")
    if durable_phase == _DURABLE_START_COMPLETED_BOOTSTRAP:
        assert bootstrap_binding is not None
        historical_sha = str(bootstrap_binding.get("historical_launch_sha256") or "")
        selected = [item for item in existing_launches if item[2] == historical_sha]
        if (not _exact_sha256(historical_sha) or len(selected) != 1
                or len(existing_launches) != 1
                or selected[0][1].get("database_sha256_after")
                != bootstrap_binding.get("historical_database_postimage")):
            raise click.ClickException("AC dev durable bootstrap historical generation mismatch")
        bootstrap_binding["historical_launch_id"] = selected[0][1].get("launch_id")
    elif existing_launches:
        current_postimage = current_database_sha256()
        matching_postimages = [
            digest for _path, value, digest in existing_launches
            if value.get("database_sha256_after") == current_postimage
        ]
        if len(matching_postimages) != 1:
            raise click.ClickException(
                "AC dev durable completed generation postimage is missing or ambiguous"
            )
    if len(dead_unsealed) > 1:
        raise click.ClickException("AC dev durable multiple dead unsealed generations")
    live_generations = []
    for _path, value, digest in existing_launches:
        pid = int(value.get("pid") or 0)
        try:
            process = _posix_process_identity(pid)
        except click.ClickException:
            process = None
        if process is not None:
            live_generations.append((_path, value, digest, process))
    if len(live_generations) > 1:
        raise click.ClickException("AC dev durable multiple live generations")
    if live_generations:
        _path, value, digest, process = live_generations[0]
        pid = int(value.get("pid") or 0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            listener = _durable_listener_pid(AC_DEV_SERVICE_PORT)
            if listener == pid:
                health = _probe_governance(AC_DEV_SERVICE_PORT, timeout=0.5)
                if not health or health.get("pid") != pid:
                    raise click.ClickException("AC dev durable live health mismatch")
                validate_live_generation(_path, value, digest, process, health)
                click.echo(json.dumps({"status": "already_running", "pid": pid,
                                       "receipt": str(_path), "receipt_sha256": digest}, sort_keys=True))
                return
            if listener:
                raise click.ClickException("AC dev durable unknown listener owns port 40008")
            try:
                process = _posix_process_identity(pid)
            except click.ClickException:
                break
            time.sleep(0.1)
        else:
            raise click.ClickException("AC dev durable live child remained unbound")
    listener_before_recovery = _durable_listener_pid(AC_DEV_SERVICE_PORT)
    if listener_before_recovery:
        raise click.ClickException("AC dev durable unknown listener owns port 40008")
    if dead_unsealed:
        _path, dead_value, dead_digest = dead_unsealed[0]
        # The PID lookup above proved the exact Darwin start identity absent;
        # the listener lookup proved the reserved port free.  Seal that readback
        # before any lock claim or successor child creation.
        seal_payload = {
            "schema_version": "ac_dev_durable_abnormal_seal.v1",
            "stage": "completed_child_absent", "launch_sha256": dead_digest,
            "pid": dead_value.get("pid"), "process": dead_value.get("process"),
            "database_sha256": current_database_sha256(),
            "listener_pid": None,
        }
        existing_seals = [(value, digest) for value, digest in abnormal_values
                          if value.get("launch_sha256") == dead_digest]
        if existing_seals:
            if len(existing_seals) != 1 or existing_seals[0][0] != seal_payload:
                raise click.ClickException("AC dev durable abnormal seal mismatch")
        else:
            seal_path, seal_digest = _durable_content_receipt(
                runtime, "abnormal", seal_payload,
            )
            replay, replay_digest = _read_durable_content_receipt(seal_path, "abnormal")
            if replay != seal_payload or replay_digest != seal_digest:
                raise click.ClickException("AC dev durable abnormal seal readback mismatch")
    if lock.is_symlink():
        raise click.ClickException("AC dev durable launch lock is not canonical")
    if lock.exists():
        try:
            lock_value = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise click.ClickException("AC dev durable recovery lock is unreadable") from exc
        pending_paths = list(runtime.glob("pending.*.json"))
        readiness_paths = list(runtime.glob("readiness.*.json"))
        if not pending_paths:
            raise click.ClickException("AC dev durable recovery pending receipt is missing")
        pending_candidates = []
        for path in pending_paths:
            value, digest = _read_durable_content_receipt(path, "pending")
            pending_candidates.append((path, value, digest))
        selected = [item for item in pending_candidates
                    if item[1].get("launch_id") == lock_value.get("launch_id")]
        if len(selected) != 1:
            raise click.ClickException("AC dev durable recovery pending generation mismatch")
        pending_path, pending_value, pending_digest = selected[0]
        # No recovery/seal is legal until the old child is OS-proven absent and
        # the reserved listener is free.
        pending_pid = 0
        readiness_matches = []
        for path in readiness_paths:
            value, digest = _read_durable_content_receipt(path, "readiness")
            if value.get("pending_sha256") == pending_digest:
                readiness_matches.append((path, value, digest))
                pending_pid = int(value.get("pid") or 0)
        if pending_pid:
            try:
                _posix_process_identity(pending_pid)
            except click.ClickException:
                pass
            else:
                raise click.ClickException("AC dev durable recovery child is still live")
        if _durable_listener_pid(AC_DEV_SERVICE_PORT):
            raise click.ClickException("AC dev durable recovery requires free port 40008")
        database_now = current_database_sha256()
        recovery_stage = ""
        recovery_readiness = ""
        if database_now == pending_value.get("database_sha256_before") and not readiness_matches:
            recovery_stage = "preimage_retryable"
        elif len(readiness_matches) == 1 and database_now == readiness_matches[0][1].get("database_sha256_after"):
            recovery_stage = "postimage_child_absent"
            recovery_readiness = readiness_matches[0][2]
        else:
            raise click.ClickException("AC dev durable recovery database projection mismatch")
        _durable_content_receipt(runtime, "abnormal", {
            "schema_version": "ac_dev_durable_abnormal_seal.v1", "stage": recovery_stage,
            "pending_sha256": pending_digest, "readiness_sha256": recovery_readiness,
            "database_sha256": database_now,
        })
        lock.unlink()
        _fsync_parent(lock)
    if _durable_listener_pid(AC_DEV_SERVICE_PORT):
        raise click.ClickException("AC dev durable launch requires free port 40008")
    launch_id = hashlib.sha256(
        f"{time.time_ns()}\0{os.getpid()}\0{source_identity['commit']}".encode()
    ).hexdigest()[:24]
    log_path = runtime / f"governance-{launch_id}.log"
    _posix_exclusive_json(lock, {"schema_version": _AC_DEV_DURABLE_LAUNCH_VERSION,
                                 "stage": "locked", "launch_id": launch_id})
    log_fd = os.open(
        log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    parent_sock.settimeout(15)
    pending_identity = (dict(preimage["database_identity"])
                        if durable_phase == _DURABLE_START_COMPLETED_BOOTSTRAP
                        else {key: dict(preimage["database_identity"])[key] for key in ("device", "inode")})
    pending_payload = {
        "schema_version": "ac_dev_durable_pending.v1", "stage": "pending",
        "durable_start_phase": durable_phase,
        "launch_id": launch_id, "parent_pid": os.getpid(),
        "source_identity": dict(source_identity), "dev_storage_root": str(dev_storage),
        "database_path": str(database), "database_identity": pending_identity,
        "database_sha256_before": preimage["database_sha256"],
        "linked_v3_receipt": str(linked_receipt.absolute()),
        "linked_v3_receipt_sha256": linked_digest,
    }
    if bootstrap_binding is not None:
        pending_payload["dashboard_bootstrap"] = {
            key: bootstrap_binding[key] for key in (
                "bootstrap_receipt", "bootstrap_receipt_sha256",
                "bootstrap_pending", "bootstrap_pending_sha256",
                "historical_launch_sha256", "historical_stop_sha256",
                "historical_exit_sha256", "historical_launch_id",
            )
        }
    pending, pending_sha256 = _durable_content_receipt(runtime, "pending", pending_payload)
    argv = [
        sys.executable, "-m", "agent.cli", "start", "--runtime-plane", "dev",
        "--port", str(AC_DEV_SERVICE_PORT), "--dev-storage-root", str(dev_storage),
        "--stable-anchor-commit", stable_anchor_commit,
        "--durable-child-runtime-dir", str(runtime),
        "--durable-child-launch-id", launch_id,
        "--durable-child-control-fd", str(child_sock.fileno()),
        "--durable-child-pending-receipt", str(pending),
        "--durable-child-linked-v3-receipt", str(linked_receipt.absolute()),
    ]
    try:
        child = _posix_detached_popen(
            argv, cwd=source_root, log_fd=log_fd, pass_fds=(child_sock.fileno(),),
        )
    except BaseException:
        os.close(log_fd)
        parent_sock.close(); child_sock.close()
        raise
    child_sock.close()
    os.close(log_fd)
    try:
        process = _posix_process_identity(child.pid)
        # macOS ps renders argv without recoverable quoting, so exact list
        # equality is proven by the child-owned readiness projection below.
        # The OS readback here binds start/cwd and the unguessable launch id.
        if process["cwd"] != str(source_root):
            raise click.ClickException("AC dev durable child process identity mismatch")
        if launch_id not in process["argv"]:
            raise click.ClickException("AC dev durable child process identity mismatch")
        raw = b""
        while b"\n" not in raw and len(raw) < 65536:
            chunk = parent_sock.recv(65536 - len(raw))
            if not chunk:
                raise click.ClickException("AC dev durable child readiness pipe EOF")
            raw += chunk
        if b"\n" not in raw:
            raise click.ClickException("AC dev durable child readiness is oversized")
        message = json.loads(raw.split(b"\n", 1)[0])
        readiness_path = Path(str(message.get("readiness_path") or ""))
        readiness, readiness_sha256 = _read_durable_content_receipt(readiness_path, "readiness")
        if (message.get("readiness_sha256") != readiness_sha256
                or readiness.get("pending_sha256") != pending_sha256
                or readiness.get("launch_id") != launch_id
                or readiness.get("pid") != child.pid
                or readiness.get("database_sha256_before") != preimage["database_sha256"]
                or _durable_listener_pid(AC_DEV_SERVICE_PORT) != 0):
            raise click.ClickException("AC dev durable child readiness mismatch")
        database_identity = dict(readiness.get("database_identity") or {})
        if durable_phase == _DURABLE_START_COMPLETED_BOOTSTRAP:
            actual_post_custody = _canonical_dev_database_identity_projection(database)
            if (readiness.get("durable_start_phase") != _DURABLE_START_COMPLETED_BOOTSTRAP
                    or database_identity != actual_post_custody
                    or readiness.get("database_sha256_after") != _file_sha256(database)
                    or not isinstance(readiness.get("custody_delta"), Mapping)):
                raise click.ClickException("AC dev durable bootstrap readiness post-custody mismatch")
            database_identity = actual_post_custody
        base = {
            "schema_version": _AC_DEV_DURABLE_LAUNCH_VERSION, "stage": "completed",
            "launch_id": launch_id, "pid": child.pid, "process": process,
            "argv": argv, "cwd": str(source_root), "python": str(Path(sys.executable).resolve()),
            "source_commit": source_identity["commit"], "source_tree": source_identity["tree"],
            "server_sha256": server_sha, "source_root": str(source_root),
            "database_path": str(database), "database_identity": dict(database_identity),
            "dev_storage_root": str(dev_storage), "project_id": "aming-claw",
            "port": AC_DEV_SERVICE_PORT, "linked_v3_receipt_sha256": linked_digest,
            "log_path": str(log_path), "log_identity": _admission_identity(log_path),
            "policy": {"runtime_plane": "dev", "migration": "verify-only",
                       "stable_deployment": "deny", "graph_activation": "deny",
                       "background_workers": "deny"},
            "pending_sha256": pending_sha256,
            "readiness_sha256": readiness_sha256,
            "database_sha256_before": readiness["database_sha256_before"],
            "database_sha256_after": readiness["database_sha256_after"],
        }
        if bootstrap_binding is not None:
            base["dashboard_bootstrap"] = pending_payload["dashboard_bootstrap"]
        completed = base
        active, active_sha256 = _durable_content_receipt(runtime, "launch", completed)
        parent_sock.sendall(_canonical_json_bytes({
            "completed_path": str(active), "completed_sha256": active_sha256,
        }) + b"\n")
        parent_sock.close()
        deadline = time.monotonic() + 15
        health = None
        while time.monotonic() < deadline and child.poll() is None:
            health = _probe_governance(AC_DEV_SERVICE_PORT, timeout=0.5)
            if health and health.get("pid") == child.pid and _durable_listener_pid(AC_DEV_SERVICE_PORT) == child.pid:
                break
            time.sleep(0.1)
        else:
            raise click.ClickException("AC dev durable child did not become exact healthy listener")
        if not health or not _dev_running_identity_matches(
            health, source_identity, stable_anchor_commit=stable_anchor_commit,
            dev_database_identity=database_identity,
        ):
            raise click.ClickException(
                "AC dev durable child health identity mismatch: "
                + json.dumps({"health": health, "source": dict(source_identity),
                              "database_identity": dict(database_identity),
                              "stable_anchor_commit": stable_anchor_commit}, sort_keys=True)
            )
        lock.unlink()
        _fsync_parent(lock)
        click.echo(json.dumps({"status": "started", "pid": child.pid, "receipt": str(active),
                               "receipt_sha256": active_sha256}, sort_keys=True))
    except BaseException:
        parent_sock.close()
        if child.poll() is None:
            os.kill(child.pid, signal.SIGTERM)
        failure = runtime / f"launch-{launch_id}.failed.json"
        if not failure.exists():
            _posix_exclusive_json(failure, {
                "schema_version": _AC_DEV_DURABLE_LAUNCH_VERSION, "stage": "failed",
                "launch_id": launch_id, "pid": child.pid,
            })
        raise


def _verified_durable_stopped_chain(dev_storage: Path) -> tuple[Path, str]:
    """Read-only proof for an already terminal durable generation."""
    runtime = dev_storage / "runtime" / "durable-launch"
    def read(prefix: str) -> dict[str, tuple[Path, dict[str, Any]]]:
        return {digest: (path, value) for path in runtime.glob(f"{prefix}.*.json")
                for value, digest in (_read_durable_content_receipt(path, prefix),)}
    launches, pendings, readinesses = read("launch"), read("pending"), read("readiness")
    challenges, exits, stops = read("stop-challenge"), read("exit"), read("stop")
    if not launches or list(runtime.glob("abnormal.*.json")):
        raise click.ClickException("AC dev durable stopped chain is missing or ambiguous")
    terminals: dict[str, tuple[Path, str]] = {}
    predecessors: dict[str, str] = {}
    used: set[str] = set()
    for launch_sha, (_launch_path, launch) in launches.items():
        pending_sha, readiness_sha = launch.get("pending_sha256"), launch.get("readiness_sha256")
        matching_challenges = [(sha, value) for sha, (_path, value) in challenges.items()
                               if value.get("launch_sha256") == launch_sha]
        matching_stops = [(sha, path, value) for sha, (path, value) in stops.items()
                          if value.get("launch_sha256") == launch_sha]
        if (not isinstance(pending_sha, str) or pending_sha not in pendings
                or not isinstance(readiness_sha, str) or readiness_sha not in readinesses
                or len(matching_challenges) != 1 or len(matching_stops) != 1):
            raise click.ClickException("AC dev durable stopped chain is incomplete")
        challenge_sha, challenge = matching_challenges[0]
        stop_sha, stop_path, stop = matching_stops[0]
        exit_sha = stop.get("exit_sha256")
        if (exit_sha not in exits or stop.get("challenge_sha256") != challenge_sha
                or exits[exit_sha][1].get("launch_sha256") != launch_sha
                or exits[exit_sha][1].get("challenge_sha256") != challenge_sha
                or launch.get("stage") != "completed"
                or launch.get("dev_storage_root") != str(dev_storage)):
            raise click.ClickException("AC dev durable stopped chain binding mismatch")
        used.update({launch_sha, pending_sha, readiness_sha, challenge_sha, exit_sha, stop_sha})
        binding = launch.get("dashboard_bootstrap")
        if isinstance(binding, Mapping):
            prior = str(binding.get("historical_launch_sha256") or "")
            if prior not in launches or prior == launch_sha:
                raise click.ClickException("AC dev durable stopped bootstrap predecessor mismatch")
            predecessors[launch_sha] = prior
        terminals[launch_sha] = (stop_path, stop_sha)
    if any(predecessors.get(node) == node for node in predecessors):
        raise click.ClickException("AC dev durable stopped chain cycle")
    children = set(predecessors.values())
    maximal = [node for node in terminals if node not in children]
    if len(maximal) != 1 or len(used) != sum(len(group) for group in (launches, pendings, readinesses, challenges, exits, stops)):
        raise click.ClickException("AC dev durable stopped chain is ambiguous")
    launch_sha = maximal[0]
    stop_path, stop_sha = terminals[launch_sha]
    launch = launches[launch_sha][1]
    if launch.get("database_sha256_after") != _file_sha256(dev_storage / "governance" / "aming-claw" / "governance.db"):
        raise click.ClickException("AC dev durable stopped terminal DB mismatch")
    try:
        _posix_process_identity(int(launch.get("pid") or 0))
    except click.ClickException:
        pass
    else:
        raise click.ClickException("AC dev durable stopped chain PID is live")
    if _durable_listener_pid(AC_DEV_SERVICE_PORT) or _port_is_open(AC_DEV_SERVICE_PORT):
        raise click.ClickException("AC dev durable stopped chain port is not free")
    return stop_path, stop_sha


def _durable_dev_stop(dev_storage: Path) -> None:
    runtime = dev_storage / "runtime" / "durable-launch"
    launch_paths = list(runtime.glob("launch.*.json"))
    live_launches = []
    for path in launch_paths:
        value, digest = _read_durable_content_receipt(path, "launch")
        pid_value = int(value.get("pid") or 0)
        if pid_value > 0 and _durable_listener_pid(AC_DEV_SERVICE_PORT) == pid_value:
            live_launches.append((path, value, digest))
    if not live_launches and not _durable_listener_pid(AC_DEV_SERVICE_PORT):
        stopped_path, stopped_sha = _verified_durable_stopped_chain(dev_storage)
        click.echo(json.dumps({"status": "already_stopped", "receipt": str(stopped_path),
                               "receipt_sha256": stopped_sha}, sort_keys=True))
        return
    if len(launch_paths) == 1:
        receipt_path = launch_paths[0]
        receipt, launch_sha256 = _read_durable_content_receipt(receipt_path, "launch")
    elif len(live_launches) == 1:
        receipt_path, receipt, launch_sha256 = live_launches[0]
    else:
        raise click.ClickException("AC dev durable stop requires completed launch receipt")
    pid = int(receipt.get("pid") or 0) if isinstance(receipt, dict) else 0
    if (
        not isinstance(receipt, dict) or receipt.get("schema_version") != _AC_DEV_DURABLE_LAUNCH_VERSION
        or receipt.get("stage") != "completed" or receipt.get("project_id") != "aming-claw"
        or receipt.get("port") != AC_DEV_SERVICE_PORT or pid <= 0
    ):
        raise click.ClickException("AC dev durable launch receipt is malformed")
    try:
        receipt_root = Path(str(receipt.get("dev_storage_root") or "")).resolve(strict=True)
        source_root = Path(str(receipt.get("source_root") or "")).resolve(strict=True)
        database = Path(str(receipt.get("database_path") or "")).resolve(strict=True)
        log_path = Path(str(receipt.get("log_path") or "")).resolve(strict=True)
        current_source = _source_git_identity()
        database_identity = _admission_identity(database)
        server_sha = "sha256:" + hashlib.sha256(
            (source_root / "agent" / "governance" / "server.py").read_bytes()
        ).hexdigest()
    except (OSError, ValueError) as exc:
        raise click.ClickException("AC dev durable stop bound identity is unavailable") from exc
    if (
        receipt_root != dev_storage.resolve(strict=True)
        or not isinstance(receipt.get("database_identity"), dict)
        or receipt["database_identity"].get("device") != database_identity["device"]
        or receipt["database_identity"].get("inode") != database_identity["inode"]
        or receipt.get("log_identity") != _admission_identity(log_path)
        or log_path.parent != runtime.resolve(strict=True)
        or current_source.get("root") != str(source_root)
        or current_source.get("commit") != receipt.get("source_commit")
        or current_source.get("tree") != receipt.get("source_tree")
        or current_source.get("dirty") != ""
        or server_sha != receipt.get("server_sha256")
        or receipt.get("python") != str(Path(sys.executable).resolve())
        or receipt.get("cwd") != str(source_root)
        or receipt.get("policy") != {"runtime_plane": "dev", "migration": "verify-only",
            "stable_deployment": "deny", "graph_activation": "deny",
            "background_workers": "deny"}
        or receipt.get("database_sha256_after") != _file_sha256(database)
    ):
        raise click.ClickException("AC dev durable stop bound identity mismatch")
    linked_digest = str(receipt.get("linked_v3_receipt_sha256") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", linked_digest):
        raise click.ClickException("AC dev durable stop linked-v3 identity mismatch")
    linked_path = dev_storage / "archive" / "schema-admission" / f"{linked_digest[7:]}.json"
    # A live stop must not call the bootstrap start selector: that selector
    # deliberately proves a stopped/free listener.  Phase is bound by this
    # immutable completed launch receipt instead.
    bootstrap_binding = receipt.get("dashboard_bootstrap")
    if bootstrap_binding is None:
        durable_phase = _DURABLE_START_LEGACY_ADOPTION
    elif isinstance(bootstrap_binding, Mapping):
        durable_phase = _DURABLE_START_COMPLETED_BOOTSTRAP
    else:
        raise click.ClickException("AC dev durable stop launch phase is malformed")
    validated_digest, _ = _validated_linked_v3_receipt(
        linked_path, dev_storage=dev_storage, database=database,
        database_identity=database_identity, source_identity=current_source,
        allow_postimage=True, durable_start_phase=durable_phase,
    )
    if validated_digest != linked_digest:
        raise click.ClickException("AC dev durable stop linked-v3 identity mismatch")
    process = _posix_process_identity(pid)
    current_health = _probe_governance(AC_DEV_SERVICE_PORT, timeout=0.5)
    if (
        process != receipt.get("process") or process["cwd"] != receipt.get("cwd")
        or not isinstance(receipt.get("argv"), list)
        or receipt.get("launch_id") not in process["argv"]
        or _durable_listener_pid(AC_DEV_SERVICE_PORT) != pid
        or not current_health or current_health.get("pid") != pid
    ):
        raise click.ClickException("AC dev durable stop process identity mismatch")
    before_exits = {path.name for path in runtime.glob("exit.*.json")}
    challenge_payload = {
        "schema_version": "ac_dev_durable_stop_challenge.v1", "stage": "term_requested",
        "nonce": secrets.token_hex(32), "launch_sha256": launch_sha256,
        "binding": _durable_exit_binding(receipt, launch_sha256),
    }
    challenge_path, challenge_sha256 = _durable_content_receipt(
        runtime, "stop-challenge", challenge_payload,
    )
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        raise click.ClickException("AC dev durable stop TERM timeout; SIGKILL not authorized")
    new_exits = [path for path in runtime.glob("exit.*.json") if path.name not in before_exits]
    if len(new_exits) != 1:
        raise click.ClickException("AC dev durable child exit receipt is missing")
    exit_path = new_exits[0]
    exit_receipt, exit_sha256 = _read_durable_content_receipt(exit_path, "exit")
    if (
        not isinstance(exit_receipt, dict)
        or exit_receipt.get("schema_version") != "ac_dev_durable_exit.v1"
        or exit_receipt.get("pid") != pid
        or exit_receipt.get("status") not in {"terminated", "normal", "python_exception"}
        or not isinstance(exit_receipt.get("exit_code"), int)
        or exit_receipt.get("binding") != challenge_payload["binding"]
        or exit_receipt.get("challenge_sha256") != challenge_sha256
        or exit_receipt.get("launch_sha256") != launch_sha256
        or (exit_receipt.get("status"), exit_receipt.get("exit_code")) not in {
            ("terminated", 143), ("normal", 0), ("python_exception", 1),
        }
    ):
        raise click.ClickException("AC dev durable child exit receipt is malformed")
    stopped, stopped_sha256 = _durable_content_receipt(runtime, "stop", {
        "schema_version": "ac_dev_durable_stop.v1", "stage": "completed",
        "launch_sha256": launch_sha256, "challenge_sha256": challenge_sha256,
        "exit_sha256": exit_sha256,
    })
    click.echo(json.dumps({"status": "stopped", "pid": pid, "receipt": str(stopped)}, sort_keys=True))


@main.command()
@click.option(
    "--workspace",
    default="",
    help="Runtime workspace root for shared-volume/project state. Defaults to the plugin runtime root, not the current project.",
)
@click.option("--port", default=40000, type=int, help="Governance HTTP port.")
@click.option(
    "--runtime-plane",
    type=click.Choice(["generic", "stable", "dev"]),
    default="generic",
    help=(
        "Runtime plane. Generic preserves the public start contract; AC stable/dev "
        "are explicit self-hosting modes."
    ),
)
@click.option(
    "--runtime-workspace",
    default="",
    help="Process-local runtime state root for the dev plane; must be outside the source worktree.",
)
@click.option(
    "--shared-volume-path",
    default="",
    help="Stable/generic shared-volume root. Forbidden for the dev plane.",
)
@click.option(
    "--dev-storage-root",
    default="",
    help="Dedicated non-symlink AC dev-world storage root.",
)
@click.option(
    "--stable-anchor-commit",
    default="",
    help=(
        "Exact stable commit. Dev defaults to the currently loaded stable; stable "
        "defaults to and must equal the checked-out stable branch HEAD."
    ),
)
@click.option("--durable-launch", is_flag=True, help="Launch the validated AC dev foreground server as a durable POSIX child.")
@click.option("--durable-stop", is_flag=True, help="Stop only the exact receipt-bound durable AC dev child with bounded TERM.")
@click.option("--linked-v3-receipt", default=None, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--durable-child-runtime-dir", default=None, type=click.Path(file_okay=False, dir_okay=True, path_type=Path), hidden=True)
@click.option("--durable-child-launch-id", default="", hidden=True)
@click.option("--durable-child-control-fd", default=-1, type=int, hidden=True)
@click.option("--durable-child-pending-receipt", default=None, type=click.Path(exists=True, dir_okay=False, path_type=Path), hidden=True)
@click.option("--durable-child-linked-v3-receipt", default=None, type=click.Path(exists=True, dir_okay=False, path_type=Path), hidden=True)
def start(
    workspace,
    port,
    runtime_plane,
    runtime_workspace,
    shared_volume_path,
    dev_storage_root,
    stable_anchor_commit,
    durable_launch,
    durable_stop,
    linked_v3_receipt,
    durable_child_runtime_dir,
    durable_child_launch_id,
    durable_child_control_fd,
    durable_child_pending_receipt,
    durable_child_linked_v3_receipt,
):
    """Start governance in the foreground without spawning plugin-owned workers."""
    from agent.runtime_plane import graph_activation_policy

    if sum(bool(value) for value in (durable_launch, durable_stop, durable_child_runtime_dir)) > 1:
        raise click.ClickException("AC dev durable lifecycle modes are mutually exclusive")
    if (durable_launch or durable_stop or durable_child_runtime_dir) and runtime_plane != "dev":
        raise click.ClickException("AC dev durable lifecycle is dev-only")
    if durable_launch and linked_v3_receipt is None:
        raise click.ClickException("AC dev durable launch requires --linked-v3-receipt")
    if not durable_launch and linked_v3_receipt is not None:
        raise click.ClickException("--linked-v3-receipt is valid only with --durable-launch")
    child_binding_args = all((bool(durable_child_launch_id), durable_child_runtime_dir is not None,
                              durable_child_control_fd >= 0, durable_child_pending_receipt is not None,
                              durable_child_linked_v3_receipt is not None))
    if bool(durable_child_runtime_dir) != child_binding_args:
        raise click.ClickException("AC dev durable child requires its complete launch binding")

    if runtime_plane == "dev" and graph_activation_policy("dev")[
        "active_graph_activation_allowed"
    ]:
        raise click.ClickException(
            "AC dev runtime policy cannot authorize active graph activation."
        )
    _require_source_checkout_matches_loaded_package(workspace)
    health = None
    database_binding = None
    dev_identity = None
    if runtime_plane == "dev":
        if port != AC_DEV_SERVICE_PORT:
            raise click.ClickException(
                f"AC dev runtime is reserved to port {AC_DEV_SERVICE_PORT}; got {port}."
            )
        dev_identity = _dev_source_identity_precheck()
        if shared_volume_path:
            raise click.ClickException(
                "AC dev runtime cannot bind --shared-volume-path; use --dev-storage-root."
            )
        stable_anchor_commit = stable_anchor_commit or _local_stable_source_anchor()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", stable_anchor_commit):
            raise click.ClickException("AC dev runtime requires an exact local stable source anchor.")
        from agent.runtime_plane import resolve_ac_dev_storage_root
        from agent.governance.db import verified_stable_database_binding
        stable_shared = Path(str(verified_stable_database_binding(
            stable_anchor_commit=stable_anchor_commit
        )["shared_volume_path"]))
        canonical_dev_storage = resolve_ac_dev_storage_root(stable_shared)
        selected_dev_storage = canonical_dev_storage
        if (durable_launch or durable_stop or durable_child_runtime_dir is not None) and dev_storage_root:
            requested = Path(dev_storage_root).expanduser().absolute()
            try:
                requested_resolved = requested.resolve(strict=True)
                source_resolved = Path(dev_identity["root"]).resolve(strict=True)
                stable_resolved = stable_shared.resolve(strict=True)
            except OSError as exc:
                raise click.ClickException("AC dev durable isolated root is unavailable") from exc
            if (requested.is_symlink() or requested_resolved in {source_resolved, stable_resolved}
                    or source_resolved in requested_resolved.parents
                    or stable_resolved in requested_resolved.parents):
                raise click.ClickException("AC dev durable root is not physically isolated")
            selected_dev_storage = requested_resolved
        elif dev_storage_root and Path(dev_storage_root).expanduser().absolute() != selected_dev_storage:
            raise click.ClickException("AC dev storage root must equal the canonical stable-volume sibling.")
        if durable_stop:
            _durable_dev_stop(selected_dev_storage)
            return
        if durable_launch:
            isolated_database = selected_dev_storage / "governance" / "aming-claw" / "governance.db"
            if not isolated_database.is_file():
                raise click.ClickException("AC dev durable launch requires its existing receipt-bound database")
            durable_phase, _bootstrap = _durable_start_phase(selected_dev_storage)
            _linked_digest, _linked = _validated_linked_v3_receipt(
                linked_v3_receipt, dev_storage=selected_dev_storage,
                database=isolated_database,
                database_identity=_admission_identity(isolated_database),
                source_identity=dev_identity,
                allow_postimage=True, durable_start_phase=durable_phase,
            )
        # Listener ownership is the first dev-world admission decision.  A
        # running or foreign process must be rejected before bootstrap, source
        # CAS, activation validation, or any dedicated-root filesystem write.
        health = _probe_governance(port)
        if (not durable_launch and health and health.get("status") == "ok"
                and health.get("service") == "governance"):
            runtime_identity = health.get("runtime_plane_identity")
            reported_database_identity = (
                dict(runtime_identity.get("database_identity"))
                if isinstance(runtime_identity, Mapping)
                and isinstance(runtime_identity.get("database_identity"), Mapping)
                else {}
            )
            if not _dev_running_identity_matches(
                health,
                dev_identity,
                stable_anchor_commit=stable_anchor_commit,
                dev_database_identity=reported_database_identity,
            ):
                raise click.ClickException(
                    "Port 40008 is occupied by governance with a mismatched AC dev runtime identity."
                )
            dashboard = _dashboard_url(f"http://localhost:{port}")
            version = health.get("version") or health.get("runtime_version") or "unknown"
            click.echo(f"Governance already running on port {port} (version {version}).")
            click.echo(f"Dashboard: {dashboard}")
            return
        if not durable_launch and _port_is_open(port):
            owner = _port_owner_hint(port)
            raise click.ClickException(
                f"Port {port} is already in use{owner}, but /api/health is not Aming Claw governance. "
                "Stop that process or choose a different --port."
            )
        # DB ingress independently re-derives this stable-volume sibling and
        # treats the dev-root env only as an equality assertion.
        os.environ["AMING_CLAW_SHARED_VOLUME"] = str(stable_shared)
        os.environ[AC_DEV_STORAGE_ROOT_ENV] = str(selected_dev_storage)
        if durable_launch or durable_child_runtime_dir is not None:
            from agent.governance.db import validate_dev_preimage_only
            lifecycle_receipt = (
                linked_v3_receipt if durable_launch else durable_child_linked_v3_receipt
            )
            try:
                bootstrap_preimage = _completed_dashboard_bootstrap_binding(selected_dev_storage)
                if bootstrap_preimage:
                    preimage_binding = bootstrap_preimage
                else:
                    preimage_binding = validate_dev_preimage_only(
                        selected_dev_storage, source_identity=dev_identity,
                        linked_v3_receipt=lifecycle_receipt,
                    )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise click.ClickException(str(exc)) from exc
            database_binding = {
                "dev_storage_root": str(selected_dev_storage),
                "database_path": preimage_binding["database_path"],
                "dev_database_identity": preimage_binding["database_identity"],
                "preimage": preimage_binding,
            }
        else:
            database_binding = _canonical_dev_database_binding(
                str(selected_dev_storage), source_identity=dev_identity,
            )
        dev_storage_root = str(database_binding["dev_storage_root"])
        if durable_launch:
            _durable_dev_launch(
                dev_storage=Path(dev_storage_root),
                database=Path(str(database_binding["database_path"])),
                database_identity=dict(database_binding["dev_database_identity"]),
                source_identity=dev_identity,
                stable_anchor_commit=stable_anchor_commit,
                linked_receipt=linked_v3_receipt,
            )
            return
        runtime_root = Path(dev_storage_root) / "runtime"
        stable_identity = None
    elif runtime_plane == "stable":
        stable_identity = _stable_start_identity_precheck(
            port=port,
            requested_anchor=stable_anchor_commit,
        )
        stable_anchor_commit = stable_identity["commit"]
    else:
        stable_identity = None
    if runtime_plane != "dev":
        health = _probe_governance(port)
    if health and health.get("status") == "ok" and health.get("service") == "governance":
        if runtime_plane == "stable" and not _stable_running_identity_matches(
            health, stable_identity or {}
        ):
            raise click.ClickException(
                "Port 40000 is occupied by governance whose loaded stable "
                "branch/commit identity does not match this clean frozen checkout."
            )
        dashboard = _dashboard_url(f"http://localhost:{port}")
        version = health.get("version") or health.get("runtime_version") or "unknown"
        click.echo(f"Governance already running on port {port} (version {version}).")
        click.echo(f"Dashboard: {dashboard}")
        return
    if runtime_plane != "dev" and _port_is_open(port):
        owner = _port_owner_hint(port)
        raise click.ClickException(
            f"Port {port} is already in use{owner}, but /api/health is not Aming Claw governance. "
            "Stop that process or choose a different --port."
        )
    os.environ["GOVERNANCE_PORT"] = str(port)
    if runtime_plane == "dev":
        if port != AC_DEV_SERVICE_PORT:
            raise click.ClickException(
                f"AC dev runtime is reserved to port {AC_DEV_SERVICE_PORT}; got {port}."
            )
        source_root = Path(str((dev_identity or {}).get("root") or "")).resolve()
        if not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", stable_anchor_commit
        ):
            raise click.ClickException("AC dev runtime requires an exact stable anchor commit.")
        if not dev_storage_root:
            raise click.ClickException("AC dev runtime requires a dedicated dev storage root.")
        dev_storage = Path(dev_storage_root).expanduser().resolve()
        existing_db = dev_storage / "governance" / "aming-claw" / "governance.db"
        if not existing_db.is_file():
            raise click.ClickException(
                "AC dev runtime requires its bootstrapped governance.db; "
                f"not found at {existing_db}."
            )
        runtime_root = (Path(dev_storage_root) / "runtime").absolute()
        if runtime_root == source_root or source_root in runtime_root.parents:
            raise click.ClickException(
                "AC dev runtime workspace must be outside the source worktree."
            )
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "dev"
        os.environ["AMING_CLAW_STABLE_ANCHOR_COMMIT"] = stable_anchor_commit
        os.environ["AMING_CLAW_ALLOWED_PROJECT_IDS"] = "aming-claw"
        os.environ["AMING_CLAW_DB_MIGRATION_POLICY"] = "verify-only"
        os.environ["AMING_CLAW_STABLE_DEPLOYMENT"] = "deny"
        os.environ[AC_DEV_STORAGE_ROOT_ENV] = str(dev_storage)
        os.environ.pop("SHARED_VOLUME_PATH", None)
        if durable_child_runtime_dir is not None:
            durable_runtime = _validated_durable_runtime_dir(
                durable_child_runtime_dir, dev_storage,
            )
            pending, pending_sha256 = _read_durable_content_receipt(
                durable_child_pending_receipt, "pending",
            )
            if (pending.get("launch_id") != durable_child_launch_id
                    or pending.get("dev_storage_root") != str(dev_storage)
                    or pending.get("source_identity") != dict(dev_identity or {})):
                raise click.ClickException("AC dev durable child pending binding mismatch")
            phase = pending.get("durable_start_phase")
            bootstrap_fields = {"dashboard_bootstrap"}
            if phase is None:
                if (pending.get("schema_version") != "ac_dev_durable_pending.v1"
                        or any(field in pending for field in bootstrap_fields)):
                    raise click.ClickException("AC dev durable child pending phase is missing")
                phase = _DURABLE_START_LEGACY_ADOPTION
            if phase not in {_DURABLE_START_LEGACY_ADOPTION, _DURABLE_START_COMPLETED_BOOTSTRAP}:
                raise click.ClickException("AC dev durable child phase is invalid")
            pending_database = dict(pending.get("database_identity") or {})
            canonical_database = dev_storage / "governance" / "aming-claw" / "governance.db"
            if phase == _DURABLE_START_LEGACY_ADOPTION:
                actual_physical = _admission_identity(canonical_database)
                if ("dashboard_bootstrap" in pending
                        or set(pending_database) != {"device", "inode"}
                        or any(type(pending_database[key]) is not int for key in pending_database)
                        or pending_database != {key: actual_physical[key] for key in ("device", "inode")}):
                    raise click.ClickException("AC dev durable child legacy database identity mismatch")
            elif (set(pending_database) != {"path", "device", "inode"}
                    or pending_database.get("path") != str(canonical_database)
                    or type(pending_database.get("device")) is not int
                    or type(pending_database.get("inode")) is not int
                    or _admission_identity(canonical_database) != pending_database
                    or not isinstance(pending.get("dashboard_bootstrap"), Mapping)):
                raise click.ClickException("AC dev durable child bootstrap database identity mismatch")
            control = socket.socket(fileno=durable_child_control_fd)
            control.settimeout(15)
            child_process = _posix_process_identity(os.getpid())
            custody = {
                "pid": os.getpid(), "start_identity": child_process["start_identity"],
                "argv": sys.argv, "cwd": child_process["cwd"],
                "source_root": str(source_root), "source_commit": (dev_identity or {})["commit"],
                "source_tree": (dev_identity or {})["tree"], "dev_storage_root": str(dev_storage),
                "project_id": "aming-claw", "port": AC_DEV_SERVICE_PORT,
                "launch_id": durable_child_launch_id,
                "policy": {"runtime_plane": "dev", "migration": "verify-only",
                           "stable_deployment": "deny", "graph_activation": "deny",
                           "background_workers": "deny"},
            }
            from agent.governance.db import commit_dev_child_custody
            try:
                if phase == _DURABLE_START_LEGACY_ADOPTION:
                    committed = commit_dev_child_custody(
                        dev_storage, source_identity=dev_identity or {},
                        process_identity=custody,
                        linked_v3_receipt=durable_child_linked_v3_receipt,
                    )
                elif phase == _DURABLE_START_COMPLETED_BOOTSTRAP:
                    committed = commit_dev_child_custody(
                        dev_storage, source_identity=dev_identity or {},
                        process_identity=custody,
                        linked_v3_receipt=durable_child_linked_v3_receipt,
                    )
                else:
                    raise click.ClickException("AC dev durable child phase is invalid")
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise click.ClickException(str(exc)) from exc
            readiness, readiness_sha256 = _durable_content_receipt(
                durable_runtime, "readiness", {
                    "schema_version": "ac_dev_durable_readiness.v1", "stage": "ready_unbound",
                    "launch_id": durable_child_launch_id, "pid": os.getpid(),
                    "pending_sha256": pending_sha256,
                    "database_sha256_before": committed["database_sha256_before"],
                    "database_sha256_after": committed["database_sha256_after"],
                    "database_identity": committed["database_identity"],
                    "custody_projection": committed["custody_projection"],
                    "durable_start_phase": phase,
                    "custody_delta": committed.get("custody_delta"),
                },
            )
            control.sendall(_canonical_json_bytes({
                "readiness_path": str(readiness), "readiness_sha256": readiness_sha256,
            }) + b"\n")
            raw = b""
            while b"\n" not in raw and len(raw) < 65536:
                chunk = control.recv(65536 - len(raw))
                if not chunk:
                    raise click.ClickException("AC dev durable parent release pipe EOF")
                raw += chunk
            release = json.loads(raw.split(b"\n", 1)[0])
            completed_path = Path(str(release.get("completed_path") or ""))
            completed, completed_sha256 = _read_durable_content_receipt(completed_path, "launch")
            if (release.get("completed_sha256") != completed_sha256
                    or completed.get("launch_id") != durable_child_launch_id
                    or completed.get("readiness_sha256") != readiness_sha256
                    or completed.get("pid") != os.getpid()):
                raise click.ClickException("AC dev durable completed release mismatch")
            control.close()
            database_binding["dev_database_identity"] = committed["database_identity"]
        else:
            from agent.governance.db import write_dev_launch_receipt
            server_source = Path(__file__).resolve().parent / "governance" / "server.py"
            source_hash = "sha256:" + hashlib.sha256(server_source.read_bytes()).hexdigest()
            write_dev_launch_receipt(dev_storage, stable_shared_volume=stable_shared,
                source_sha256=source_hash, port=AC_DEV_SERVICE_PORT)
    elif runtime_plane == "stable":
        runtime_root = Path(workspace).resolve() if workspace else _default_runtime_workspace()
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "stable"
        os.environ["AMING_CLAW_STABLE_ANCHOR_COMMIT"] = stable_anchor_commit
        if shared_volume_path:
            os.environ["SHARED_VOLUME_PATH"] = str(
                Path(shared_volume_path).expanduser().resolve()
            )
        else:
            os.environ.setdefault("SHARED_VOLUME_PATH", str(runtime_root / "shared-volume"))
    else:
        runtime_root = Path(workspace).resolve() if workspace else _default_runtime_workspace()
        os.environ["AMING_CLAW_RUNTIME_PLANE"] = "generic"
        os.environ.pop("AMING_CLAW_STABLE_ANCHOR_COMMIT", None)
        if shared_volume_path:
            os.environ["SHARED_VOLUME_PATH"] = str(
                Path(shared_volume_path).expanduser().resolve()
            )
        else:
            os.environ.setdefault("SHARED_VOLUME_PATH", str(runtime_root / "shared-volume"))
    os.environ["AMING_CLAW_HOME"] = str(runtime_root)
    resource_preflight = _governance_start_resource_preflight()
    click.echo(
        "Governance startup resource preflight: "
        + json.dumps(resource_preflight, sort_keys=True),
        err=True,
    )
    if runtime_plane == "dev":
        # Do not enter start_governance.py: its legacy host bootstrap performs
        # a chain-history backfill before the server can enforce the dev plane.
        if durable_child_runtime_dir is None:
            _run_dev_governance()
        else:
            durable_runtime = _validated_durable_runtime_dir(
                durable_child_runtime_dir, dev_storage,
            )
            completed_runtime = durable_runtime
            previous_term = signal.getsignal(signal.SIGTERM)
            def _term_handler(_signum, _frame):
                raise SystemExit(143)
            def _write_bound_exit(status: str, exit_code: int, exception_type: str) -> None:
                launches = []
                for candidate in completed_runtime.glob("launch.*.json"):
                    value, digest = _read_durable_content_receipt(candidate, "launch")
                    if value.get("launch_id") == durable_child_launch_id:
                        launches.append((value, digest))
                if len(launches) != 1:
                    return
                launch, launch_sha256 = launches[0]
                challenges = []
                for candidate in durable_runtime.glob("stop-challenge.*.json"):
                    value, digest = _read_durable_content_receipt(candidate, "stop-challenge")
                    if (value.get("launch_sha256") == launch_sha256
                            and value.get("binding") == _durable_exit_binding(launch, launch_sha256)):
                        challenges.append((value, digest))
                challenge_sha256 = challenges[0][1] if len(challenges) == 1 else None
                _durable_content_receipt(durable_runtime, "exit", {
                    "schema_version": "ac_dev_durable_exit.v1", "pid": os.getpid(),
                    "status": status, "exit_code": exit_code, "exception_type": exception_type,
                    "launch_sha256": launch_sha256, "challenge_sha256": challenge_sha256,
                    "binding": _durable_exit_binding(launch, launch_sha256),
                })
            signal.signal(signal.SIGTERM, _term_handler)
            try:
                _run_dev_governance()
            except BaseException as exc:
                _write_bound_exit(
                    "terminated" if isinstance(exc, SystemExit) else "python_exception",
                    int(exc.code or 0) if isinstance(exc, SystemExit) else 1,
                    type(exc).__name__,
                )
                raise
            else:
                _write_bound_exit("normal", 0, "")
            finally:
                signal.signal(signal.SIGTERM, previous_term)
    else:
        import start_governance

        start_governance.main(workspace_root=runtime_root)


@main.group("dev-cutover")
def dev_cutover():
    """Preflight, activate, or roll back the isolated AC dev world."""


@dev_cutover.command("preflight")
@click.option("--dev-storage-root", required=True, type=click.Path(path_type=str))
@click.option(
    "--legacy-database",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=str),
)
@click.option("--stable-anchor-commit", required=True)
def dev_cutover_preflight(dev_storage_root, legacy_database, stable_anchor_commit):
    """Write a read-only, restart-safe cutover checkpoint; start nothing."""

    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", stable_anchor_commit):
        raise click.ClickException("Cutover requires an exact stable source commit.")
    source = _dev_source_identity_precheck()
    binding = _canonical_dev_database_binding(
        dev_storage_root, source_identity=source
    )
    from agent.governance.db import preflight_dev_world_cutover

    os.environ["AMING_CLAW_STABLE_ANCHOR_COMMIT"] = stable_anchor_commit.lower()
    try:
        receipt = preflight_dev_world_cutover(
            legacy_database_path=legacy_database,
            storage_root=binding["dev_storage_root"],
            source_identity=source,
            process_identity={
                "pid": os.getpid(),
                "start_identity": f"pid:{os.getpid()}:dev-cutover-preflight",
            },
            expected_dev_database_identity=binding["dev_database_identity"],
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(receipt, indent=2, sort_keys=True))


@dev_cutover.command("activate")
@click.option("--dev-storage-root", required=True, type=click.Path(path_type=str))
@click.option("--preflight-hash", required=True)
@click.option("--stable-anchor-commit", required=True)
def dev_cutover_activate(dev_storage_root, preflight_hash, stable_anchor_commit):
    """Atomically activate an unchanged checkpoint; start no listener."""

    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", stable_anchor_commit):
        raise click.ClickException("Cutover requires an exact stable source commit.")
    from agent.governance.db import activate_dev_world_cutover

    os.environ["AMING_CLAW_STABLE_ANCHOR_COMMIT"] = stable_anchor_commit.lower()
    try:
        receipt = activate_dev_world_cutover(
            storage_root=dev_storage_root,
            preflight_hash=preflight_hash,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(receipt, indent=2, sort_keys=True))


@dev_cutover.command("rollback")
@click.option("--dev-storage-root", required=True, type=click.Path(path_type=str))
@click.option("--preflight-hash", required=True)
def dev_cutover_rollback(dev_storage_root, preflight_hash):
    """Atomically retire only the matching inactive-world activation marker."""

    from agent.governance.db import rollback_dev_world_cutover

    try:
        receipt = rollback_dev_world_cutover(
            storage_root=dev_storage_root,
            preflight_hash=preflight_hash,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(receipt, indent=2, sort_keys=True))


@main.group("branch-service")
def branch_service():
    """Validate isolated branch governance services."""
    pass


def _local_branch_service_validate(payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Fail closed: shared-DB branch supervision was superseded by dev start."""

    _ = payload
    return 410, {
        "ok": False,
        "error": "shared_database_branch_service_retired",
        "replacement": "aming-claw start --runtime-plane dev --dev-storage-root <root>",
        "zero_write_rejection": True,
        "writes_performed": False,
        "process_started": False,
    }


def _local_branch_service_adopt_stop_handoff(
    payload: Mapping[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Fail closed: cross-world orphan handoff is not a storage authority."""

    _ = payload
    return 410, {
        "ok": False,
        "error": "shared_database_orphan_handoff_retired",
        "replacement": "stop the exact old process, then run source-only dev bootstrap",
        "zero_write_rejection": True,
        "writes_performed": False,
        "signals_sent": False,
        "process_started": False,
    }


@branch_service.command("validate")
@click.option(
    "--worktree",
    "worktree_path",
    required=True,
    type=click.Path(file_okay=False, dir_okay=True, path_type=str),
    help="Branch/worker checkout root to start as the service cwd.",
)
@click.option(
    "--port",
    default=AC_DEV_SERVICE_PORT,
    show_default=True,
    type=int,
    help="Reserved AC dev governance port.",
)
@click.option(
    "--governance-url",
    default=DEFAULT_GOVERNANCE_URL,
    help="Compatibility-only stable URL; never used to launch the dev process.",
)
@click.option("--runtime-workspace", default="", help="Isolated AMING_CLAW_HOME for the branch service.")
@click.option(
    "--shared-volume-path",
    required=True,
    help="Existing SHARED_VOLUME_PATH containing aming-claw/governance.db.",
)
@click.option(
    "--stable-anchor-commit",
    default="",
    help="Exact current stable release commit; defaults from stable port 40000.",
)
@click.option("--python", "python_bin", default="", help="Python executable for the branch service. Defaults to current Python.")
@click.option("--timeout-sec", default=30.0, type=float, help="Seconds to wait for branch /api/health.")
@click.option("--keep-running", is_flag=True, help="Leave the validated branch service running.")
@click.option("--json-output", is_flag=True, help="Print full structured validation evidence.")
def branch_service_validate(
    worktree_path,
    port,
    governance_url,
    runtime_workspace,
    shared_volume_path,
    stable_anchor_commit,
    python_bin,
    timeout_sec,
    keep_running,
    json_output,
):
    """Reject the retired shared-database branch-service bootstrap."""
    _ = (
        worktree_path,
        port,
        governance_url,
        runtime_workspace,
        shared_volume_path,
        stable_anchor_commit,
        python_bin,
        timeout_sec,
        keep_running,
        json_output,
    )
    raise click.ClickException(
        "Shared-database branch-service validation is retired. Use `aming-claw "
        "start --runtime-plane dev --dev-storage-root <dedicated-root>`; the "
        "dev world must never bind or proxy the stable governance database."
    )


@branch_service.command("adopt-stop-handoff")
@click.option(
    "--orphan-worktree",
    "orphan_worktree_path",
    required=True,
    type=click.Path(file_okay=False, dir_okay=True, path_type=str),
    help="Dirty AC dev worktree whose exact process is being adopted.",
)
@click.option(
    "--successor-worktree",
    "successor_worktree_path",
    required=True,
    type=click.Path(file_okay=False, dir_okay=True, path_type=str),
    help="Clean descendant worktree that will receive codex/ac-dev.",
)
@click.option("--orphan-pid", required=True, type=int, help="Exact live orphan PID.")
@click.option(
    "--inspection-receipt",
    default=None,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=str),
    help="Prior JSON inspection receipt; supplying it selects execute mode.",
)
@click.option(
    "--runtime-workspace",
    default="",
    type=click.Path(file_okay=False, dir_okay=True, path_type=str),
    help="Isolated runtime root for the clean same-port replacement.",
)
@click.option(
    "--allow-kill",
    is_flag=True,
    help="Explicitly authorize SIGKILL only after exact post-TERM revalidation.",
)
@click.option("--term-timeout-sec", default=5.0, type=float, show_default=True)
@click.option("--kill-timeout-sec", default=5.0, type=float, show_default=True)
@click.option("--replacement-timeout-sec", default=30.0, type=float, show_default=True)
def branch_service_adopt_stop_handoff(
    orphan_worktree_path,
    successor_worktree_path,
    orphan_pid,
    inspection_receipt,
    runtime_workspace,
    allow_kill,
    term_timeout_sec,
    kill_timeout_sec,
    replacement_timeout_sec,
):
    """Reject the retired cross-world orphan handoff path."""

    _ = (
        orphan_worktree_path,
        successor_worktree_path,
        orphan_pid,
        inspection_receipt,
        runtime_workspace,
        allow_kill,
        term_timeout_sec,
        kill_timeout_sec,
        replacement_timeout_sec,
    )
    raise click.ClickException(
        "Shared-database orphan handoff is retired. Stop the exact old process, "
        "preserve its bytes as an archive, and start the dedicated dev world "
        "from source with a fresh genesis."
    )


@main.command("open")
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service base URL.")
def open_dashboard(governance_url):
    """Open the dashboard in the default browser."""
    url = _dashboard_url(governance_url)
    webbrowser.open(url)
    click.echo(url)


@main.command()
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service base URL.")
@click.option("--output", default="", help="Output HTML path. Defaults to .aming-claw/aming-claw-launcher.html.")
@click.option("--open-browser", is_flag=True, help="Open the generated launcher in the default browser.")
def launcher(governance_url, output, open_browser):
    """Write a local launcher HTML artifact with dashboard links and start commands."""
    target = Path(output) if output else Path.cwd() / ".aming-claw" / "aming-claw-launcher.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_launcher_html(governance_url), encoding="utf-8")
    if open_browser:
        webbrowser.open(target.resolve().as_uri())
    click.echo(str(target))


@main.command("run-executor")
def run_executor():
    """Start the executor worker."""
    from agent.executor_worker import main as worker_main
    worker_main()


@main.group()
def backlog():
    """Export and import portable backlog data."""
    pass


@backlog.command("export")
@click.option("--project-id", default="aming-claw", help="Governance project id.")
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service base URL.")
@click.option("--output", default="", help="Output JSON path. Prints JSON to stdout when omitted.")
@click.option("--status", default="", help="Optional backlog status filter, e.g. OPEN or FIXED.")
@click.option("--priority", default="", help="Optional priority filter, e.g. P1.")
@click.option("--bug-id", "bug_ids", multiple=True, help="Optional bug id to export. Can be repeated.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON even when --output is used.")
def backlog_export(project_id, governance_url, output, status, priority, bug_ids, json_output):
    """Export backlog rows as portable JSON."""
    query = {
        key: value
        for key, value in {
            "status": status,
            "priority": priority,
            "bug_id": ",".join(bug_ids),
        }.items()
        if value
    }
    qs = f"?{urllib.parse.urlencode(query)}" if query else ""
    url = f"{governance_url.rstrip('/')}/api/backlog/{urllib.parse.quote(project_id, safe='')}/portable/export{qs}"
    code, payload = _http_json("GET", url)
    if code >= 400 or payload.get("ok") is False:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        raise click.exceptions.Exit(1)

    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if json_output or not output:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo(f"Exported {payload.get('row_count', 0)} backlog row(s) to {output}")


@backlog.command("import")
@click.option("--project-id", default="aming-claw", help="Governance project id.")
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service base URL.")
@click.option("--input", "input_path", required=True, help="Input JSON path, or '-' for stdin.")
@click.option("--on-conflict", default="skip", type=click.Choice(["skip", "overwrite", "fail"]), help="How to handle existing bug ids.")
@click.option("--dry-run", is_flag=True, help="Validate and report planned changes without writing rows.")
@click.option("--actor", default="cli", help="Actor recorded in the import result.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def backlog_import_cmd(project_id, governance_url, input_path, on_conflict, dry_run, actor, json_output):
    """Import portable backlog JSON into a governance project."""
    try:
        raw = sys.stdin.read() if input_path == "-" else Path(input_path).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Cannot read backlog import JSON: {exc}") from exc

    url = f"{governance_url.rstrip('/')}/api/backlog/{urllib.parse.quote(project_id, safe='')}/portable/import"
    body = {
        "payload": payload,
        "on_conflict": on_conflict,
        "dry_run": dry_run,
        "actor": actor,
    }
    code, result = _http_json("POST", url, body)
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(
            "Backlog import "
            f"{'dry-run ' if dry_run else ''}"
            f"inserted={result.get('inserted_count', 0)} "
            f"updated={result.get('updated_count', 0)} "
            f"skipped={result.get('skipped_count', 0)} "
            f"errors={result.get('error_count', 0)}"
        )
    if code >= 400 or not result.get("ok", False):
        raise click.exceptions.Exit(1)


@main.group("runtime-context")
def runtime_context():
    """Runtime Context Service views."""
    pass


@runtime_context.command("current")
@click.option("--project-id", default="aming-claw", help="Governance project id.")
@click.option("--runtime-context-id", required=True, help="Runtime context id, e.g. mfrctx-...")
@click.option("--fence-token", default="", help="Fence token required for mf_sub worker view.")
@click.option("--parent-task-id", default="", help="Parent observer/MF task id for fence validation.")
@click.option(
    "--view",
    default="auto",
    type=click.Choice(["auto", "current", "gate_inputs", "worker_view", "close_gate_view", "all"]),
    help="Observer view selector. mf_sub callers always receive worker_view.",
)
@click.option("--graph-trace-id", default="", help="Optional graph trace id fallback.")
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service base URL.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def runtime_context_current(
    project_id,
    runtime_context_id,
    fence_token,
    parent_task_id,
    view,
    graph_trace_id,
    governance_url,
    json_output,
):
    """Read the canonical current-state projection for a worker runtime context."""
    query = {
        key: value
        for key, value in {
            "fence_token": fence_token,
            "parent_task_id": parent_task_id,
            "view": view,
            "graph_trace_id": graph_trace_id,
        }.items()
        if value
    }
    qs = f"?{urllib.parse.urlencode(query)}" if query else ""
    url = (
        f"{governance_url.rstrip('/')}/api/graph-governance/"
        f"{urllib.parse.quote(project_id, safe='')}/parallel-branches/"
        f"runtime-contexts/{urllib.parse.quote(runtime_context_id, safe='')}"
        f"/current-state{qs}"
    )
    code, payload = _http_json("GET", url)
    if json_output:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo(
            "runtime context: "
            f"{payload.get('view', 'unknown')} "
            f"project={project_id} runtime_context_id={runtime_context_id}"
        )
        service = payload.get("runtime_context_service") or {}
        views = service.get("views") if isinstance(service, dict) else {}
        if isinstance(views, dict):
            click.echo("views: " + ", ".join(sorted(views)))
        if code >= 400 or payload.get("ok") is False:
            click.echo(
                f"error: {payload.get('error') or payload.get('message') or 'runtime context lookup failed'}",
                err=True,
            )
    if code >= 400 or payload.get("ok") is False:
        raise click.exceptions.Exit(1)


@main.group()
def plugin():
    """Install and validate local Aming Claw plugin assets."""
    pass


@plugin.command("install")
@click.argument("repo_url", required=False)
@click.option("--install-root", default="", help="User-local plugin cache root.")
@click.option("--ref", default="", help="Optional branch, tag, or commit to checkout.")
@click.option("--python", "python_executable", default=sys.executable, help="Python executable for pip/start commands.")
@click.option("--no-pip", is_flag=True, help="Clone and validate only; do not pip install.")
@click.option("--no-codex-install", is_flag=True, help="Do not install Codex plugin cache/config.")
@click.option("--codex-home", default="", help="Override Codex home for plugin cache/config.")
@click.option("--codex-config", default="", help="Override Codex config.toml path.")
@click.option("--codex-marketplace-root", default="", help="Override generated Codex marketplace root.")
@click.option("--start", is_flag=True, help="Run the start command after install.")
@click.option("--dry-run", is_flag=True, help="Print planned commands without changing state.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
@click.option("--validate-only", is_flag=True, help="Validate the computed checkout path without cloning or fetching.")
def plugin_install(repo_url, install_root, ref, python_executable, no_pip, no_codex_install, codex_home, codex_config, codex_marketplace_root, start, dry_run, json_output, validate_only):
    """Clone/update the plugin from a Git URL and print next steps."""
    from agent.plugin_installer import (
        DEFAULT_REPO_URL,
        PluginInstallError,
        format_result,
        install_from_git,
    )

    try:
        result = install_from_git(
            repo_url or DEFAULT_REPO_URL,
            install_root=install_root or None,
            ref=ref,
            python_executable=python_executable,
            install_package=not no_pip,
            install_codex_plugin=not no_codex_install,
            codex_home=codex_home or None,
            codex_config=codex_config or None,
            codex_marketplace_root=codex_marketplace_root or None,
            start=start,
            dry_run=dry_run,
            validate_only=validate_only,
            suppress_command_output=json_output,
        )
    except PluginInstallError as exc:
        raise click.ClickException(str(exc)) from exc
    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(format_result(result))


@plugin.command("update")
@click.argument("repo_url", required=False)
@click.option("--check", "check_only", is_flag=True, help="Check for updates and refresh local state without applying.")
@click.option("--apply", "apply_update", is_flag=True, help="Apply a fast-forward update to the local plugin checkout.")
@click.option("--install-root", default="", help="User-local plugin cache root.")
@click.option("--ref", default="", help="Optional branch, tag, or commit to compare/apply.")
@click.option("--python", "python_executable", default=sys.executable, help="Python executable for pip/cache commands.")
@click.option("--no-pip", is_flag=True, help="Do not pip install after applying.")
@click.option("--no-codex-install", is_flag=True, help="Do not refresh Codex plugin cache/config after applying.")
@click.option("--codex-home", default="", help="Override Codex home for plugin cache checks.")
@click.option("--codex-config", default="", help="Override Codex config.toml path.")
@click.option("--codex-marketplace-root", default="", help="Override generated Codex marketplace root.")
@click.option("--plugin-state", default="", help="Optional plugin update state JSON path.")
@click.option("--dry-run", is_flag=True, help="Print planned update commands without changing state.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def plugin_update(repo_url, check_only, apply_update, install_root, ref, python_executable, no_pip, no_codex_install, codex_home, codex_config, codex_marketplace_root, plugin_state, dry_run, json_output):
    """Check or apply updates for a Git-backed local plugin checkout."""
    if check_only and apply_update:
        raise click.ClickException("Use either --check or --apply, not both.")
    from agent.plugin_installer import (
        DEFAULT_REPO_URL,
        format_plugin_update_result,
        update_plugin_from_git,
    )

    result = update_plugin_from_git(
        repo_url or DEFAULT_REPO_URL,
        install_root=install_root or None,
        ref=ref,
        apply_update=apply_update,
        python_executable=python_executable,
        install_package=not no_pip,
        install_codex_plugin=not no_codex_install,
        codex_home=codex_home or None,
        codex_config=codex_config or None,
        codex_marketplace_root=codex_marketplace_root or None,
        state_path=plugin_state or None,
        suppress_command_output=json_output,
        dry_run=dry_run,
    )
    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(format_plugin_update_result(result))
    if not result.ok:
        raise click.exceptions.Exit(1)


@plugin.command("doctor")
@click.option("--plugin-root", default="", help="Local Aming Claw plugin checkout root.")
@click.option("--governance-url", default="http://localhost:40000", help="Governance service URL.")
@click.option("--codex-config", default="", help="Optional Codex config.toml path.")
@click.option("--codex-home", default="", help="Optional Codex home for plugin cache checks.")
@click.option("--python", "python_executable", default=sys.executable, help="Python executable to validate for local runtime.")
@click.option("--skip-governance", is_flag=True, help="Skip governance health probe.")
@click.option("--check-service-manager", is_flag=True, help="Also check advanced chain/executor ServiceManager health.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def plugin_doctor(plugin_root, governance_url, codex_config, codex_home, python_executable, skip_governance, check_service_manager, json_output):
    """Run read-only aftercare checks for a local plugin install."""
    from agent.plugin_installer import doctor_plugin, format_doctor_result

    result = doctor_plugin(
        plugin_root=plugin_root or None,
        governance_url=governance_url,
        codex_config=codex_config or None,
        codex_home=codex_home or None,
        python_executable=python_executable,
        check_governance=not skip_governance,
        check_service_manager=check_service_manager,
    )
    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(format_doctor_result(result))
    if not result.ok:
        raise click.exceptions.Exit(1)


@main.group()
def observer():
    """Observer runtime launcher."""
    pass


def _observer_poll_session_registration_payload(
    *,
    observer_kind: str,
    session_label: str,
    cwd: str,
) -> dict:
    return {
        "observer_kind": observer_kind or "codex",
        "session_label": session_label,
        "pid": os.getpid(),
        "cwd": cwd,
        "capabilities": {
            "actions": [
                "observer_session_heartbeat",
                "observer_session_close",
                "observer_command_claim",
                "observer_command_complete",
                "observer_command_fail",
            ],
            "command_types": ["execute_backlog_row"],
        },
    }


def _observer_poll_public_session(
    payload: dict,
    *,
    print_session_token: bool,
) -> dict:
    if not isinstance(payload, dict):
        return {}
    public = dict(payload)
    if not print_session_token:
        public.pop("session_token", None)
    return public


def _observer_poll_invocation_fields(plan: dict) -> dict:
    """Keep request and result contracts distinct; retain result-only legacy alias."""
    request = (
        plan.get("invocation_request")
        if isinstance(plan.get("invocation_request"), dict)
        else {}
    )
    result = (
        plan.get("invocation_result")
        if isinstance(plan.get("invocation_result"), dict)
        else {}
    )
    legacy = plan.get("invocation") if isinstance(plan.get("invocation"), dict) else {}
    if legacy.get("schema_version") == "ai_invocation_request.v1":
        request = request or legacy
    elif legacy:
        result = result or legacy

    if result:
        import hashlib

        result = dict(result)
        had_error = "error" in result
        raw_error = str(result.pop("error", "") or "")
        for field in (
            "authorization",
            "command",
            "credential",
            "credentials",
            "env",
            "output_text",
            "password",
            "prompt",
            "raw_output",
            "result",
            "secret",
            "stderr",
            "stdout",
            "system_prompt",
        ):
            result.pop(field, None)
        if had_error:
            result["error"] = ""
            result["error_present"] = bool(raw_error)
            result["error_sha256"] = (
                "sha256:" + hashlib.sha256(raw_error.encode("utf-8")).hexdigest()
                if raw_error
                else ""
            )
            result["raw_error_stored"] = False
        if "evidence_refs" in result:
            from agent.ai_lifecycle import sanitize_evidence_refs

            result["evidence_refs"] = sanitize_evidence_refs(result["evidence_refs"])

    fields = {}
    if request:
        fields["invocation_request"] = request
    if result:
        fields["invocation_result"] = result
        fields["invocation"] = result
    return fields


def _validate_cli_invocation_routing(provider: str, model: str, backend_mode: str) -> None:
    from agent.pipeline_config import BACKEND_AUTH_MODE, validate_invocation_routing

    backend = str(backend_mode or "").strip().lower()
    effective_provider = "fixture" if backend == "fixture" else provider
    effective_model = "" if backend == "fixture" else model
    errors = validate_invocation_routing(
        provider=effective_provider,
        model=effective_model,
        backend_mode=backend,
        auth_mode=BACKEND_AUTH_MODE.get(backend, ""),
    )
    if errors:
        raise click.ClickException("invalid AI invocation routing: " + "; ".join(errors))


def _observer_poll_completion_result(plan: dict) -> dict:
    route_identity = plan.get("route_identity") if isinstance(plan.get("route_identity"), dict) else {}
    return {
        "ok": bool(plan.get("ok")),
        "status": str(plan.get("status") or ""),
        "schema_version": str(plan.get("schema_version") or ""),
        "observer_command_id": str(plan.get("observer_command_id") or ""),
        "backlog_id": str(plan.get("backlog_id") or ""),
        "route_id": str(route_identity.get("route_id") or ""),
        "route_context_hash": str(route_identity.get("route_context_hash") or ""),
        "prompt_contract_id": str(route_identity.get("prompt_contract_id") or ""),
        "prompt_contract_hash": str(route_identity.get("prompt_contract_hash") or ""),
        "route_token_ref": str(route_identity.get("route_token_ref") or ""),
        "visible_injection_manifest_hash": str(
            route_identity.get("visible_injection_manifest_hash") or ""
        ),
        "calls_models": bool(plan.get("calls_models")),
        **_observer_poll_invocation_fields(plan),
        "execute": bool(plan.get("execute")),
        "service_manager_required": False,
        "executor_worker_required": False,
        "uses_task_create": False,
        "payload_free_reminder": True,
        "reminder_payload_required": False,
    }


def _observer_poll_failure_result(plan: dict) -> dict:
    route_identity = plan.get("route_identity") if isinstance(plan.get("route_identity"), dict) else {}
    failure = (
        plan.get("failure_evidence")
        if isinstance(plan.get("failure_evidence"), dict)
        else {}
    )
    projection = (
        plan.get("terminal_contract_projection")
        if isinstance(plan.get("terminal_contract_projection"), dict)
        else {}
    )
    return {
        "ok": False,
        "status": str(plan.get("status") or "blocked"),
        "schema_version": str(plan.get("schema_version") or ""),
        "observer_command_id": str(plan.get("observer_command_id") or ""),
        "backlog_id": str(plan.get("backlog_id") or ""),
        "route_id": str(route_identity.get("route_id") or failure.get("route_id") or ""),
        "route_context_hash": str(
            route_identity.get("route_context_hash")
            or failure.get("route_context_hash")
            or ""
        ),
        "prompt_contract_id": str(
            route_identity.get("prompt_contract_id")
            or failure.get("prompt_contract_id")
            or ""
        ),
        "prompt_contract_hash": str(
            route_identity.get("prompt_contract_hash")
            or failure.get("prompt_contract_hash")
            or ""
        ),
        "route_token_ref": str(
            route_identity.get("route_token_ref")
            or failure.get("route_token_ref")
            or ""
        ),
        "visible_injection_manifest_hash": str(
            route_identity.get("visible_injection_manifest_hash")
            or failure.get("visible_injection_manifest_hash")
            or ""
        ),
        "terminal_dispatch_blocker": bool(plan.get("terminal_dispatch_blocker")),
        "blocker_id": str(failure.get("blocker_id") or projection.get("divergence_reason") or ""),
        "command_projection_status": str(
            projection.get("command_projection_status")
            or plan.get("command_projection_status")
            or "failed"
        ),
        "canonical_contract_state": str(
            projection.get("canonical_contract_state")
            or plan.get("canonical_contract_state")
            or "blocked"
        ),
        "calls_models": bool(plan.get("calls_models")),
        **_observer_poll_invocation_fields(plan),
        "execute": bool(plan.get("execute")),
        "service_manager_required": False,
        "executor_worker_required": False,
        "uses_task_create": False,
        "payload_free_reminder": True,
        "reminder_payload_required": False,
    }


def _observer_poll_append_timeline(
    *,
    base_url: str,
    project_id: str,
    observer_command_id: str,
    event_type: str,
    phase: str,
    status: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not observer_command_id:
        return {
            "ok": False,
            "skipped": True,
            "event_type": event_type,
            "phase": phase,
            "error": "missing_observer_command_id",
        }
    encoded_project = urllib.parse.quote(project_id, safe="")
    body = {
        "task_id": observer_command_id,
        "backlog_id": str(payload.get("backlog_id") or ""),
        "event_type": event_type,
        "phase": phase,
        "event_kind": "observer_poll",
        "status": status,
        "actor": "observer_poll_cli",
        "payload": payload,
    }
    try:
        code, response = _http_json(
            "POST",
            f"{base_url}/api/task/{encoded_project}/timeline",
            body,
        )
    except Exception as exc:  # pragma: no cover - defensive fail-soft CLI guard
        return {
            "ok": False,
            "event_type": event_type,
            "phase": phase,
            "http_status": 0,
            "error": str(exc),
        }
    ok = code < 400 and response.get("ok", True) is not False
    result = {
        "ok": ok,
        "event_type": event_type,
        "phase": phase,
        "http_status": code,
    }
    if not ok:
        result["response"] = response
    return result


def _observer_poll_heartbeat(
    *,
    base_url: str,
    project_id: str,
    session_id: str,
    session_token: str,
) -> dict[str, Any]:
    encoded_project = urllib.parse.quote(project_id, safe="")
    encoded_session = urllib.parse.quote(session_id, safe="")
    try:
        code, response = _http_json(
            "POST",
            (
                f"{base_url}/api/projects/{encoded_project}/observer-sessions/"
                f"{encoded_session}/heartbeat"
            ),
            {"session_id": session_id, "session_token": session_token},
        )
    except Exception as exc:  # pragma: no cover - defensive fail-soft CLI guard
        return {"ok": False, "http_status": 0, "error": str(exc)}
    return {
        "ok": code < 400 and response.get("ok", True) is not False,
        "http_status": code,
        "observer_session_id": str(
            response.get("observer_session_id") or response.get("session_id") or session_id
        ),
        "heartbeat_interval_sec": response.get("heartbeat_interval_sec"),
        "response": response if code >= 400 or response.get("ok", True) is False else {},
    }


def _observer_poll_normalize_claim_response(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {"ok": False, "error": "non_object_claim_response"}
    if isinstance(payload.get("command"), dict) or payload.get("empty") is True:
        return payload
    if payload.get("command_id") and payload.get("command_type"):
        return {
            "ok": True,
            "project_id": str(payload.get("project_id") or ""),
            "observer_session_id": str(payload.get("claimed_by_session_id") or ""),
            "command": payload,
            "empty": False,
            "normalized_from": "raw_command",
        }
    return payload


@observer.command("poll")
@click.option("--project-id", required=True, help="Governance project id.")
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service URL.")
@click.option("--session-id", default="", help="Existing observer session id. Omit to register one.")
@click.option(
    "--session-token",
    default="",
    envvar="AMING_CLAW_OBSERVER_SESSION_TOKEN",
    help="Existing observer session token. Can also use AMING_CLAW_OBSERVER_SESSION_TOKEN.",
)
@click.option(
    "--command-id",
    default="",
    help="Specific observer command id to claim. Defaults to next command.",
)
@click.option("--observer-kind", default="codex", help="Observer kind used when registering a session.")
@click.option("--session-label", default="", help="Observer session label used when registering a session.")
@click.option(
    "--print-session-token",
    is_flag=True,
    help="Include a newly registered session token in JSON output.",
)
@click.option("--provider", default="openai", help="Provider name, e.g. openai or anthropic.")
@click.option("--model", default="", help="Optional provider model override.")
@click.option(
    "--backend-mode",
    default="codex_cli",
    help="Invocation backend, e.g. codex_cli, claude_cli, openai_api, anthropic_api.",
)
@click.option("--workspace", default="", help="Observer workspace. Defaults to current working directory.")
@click.option(
    "--prompt-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Optional observer prompt file.",
)
@click.option(
    "--dispatch-gate-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="MF subagent dispatch gate evidence JSON required for live code-mutating backends.",
)
@click.option(
    "--main-worktree",
    default="",
    help="Target/main worktree path blocked by one-hop dispatch policy.",
)
@click.option(
    "--timeout-sec",
    default=120,
    type=int,
    help="Observer invocation timeout if --execute is used.",
)
@click.option(
    "--early-progress-timeout-sec",
    default=20.0,
    type=float,
    help="Fail codex_cli workers that produce no output or worktree changes before this timeout.",
)
@click.option(
    "--execute",
    is_flag=True,
    help="Actually invoke the configured provider after one-hop gate validation.",
)
@click.option(
    "--watch/--once",
    default=False,
    help="Keep polling until --max-commands or --idle-timeout-sec is reached. Defaults to --once.",
)
@click.option(
    "--max-commands",
    default=0,
    type=int,
    help="Maximum commands to process in --watch mode. Use 0 for no command limit.",
)
@click.option(
    "--idle-timeout-sec",
    default=None,
    type=float,
    help="Exit --watch after this many idle seconds. Defaults to 60; use 0 to exit on first empty poll.",
)
@click.option(
    "--poll-interval-sec",
    default=5.0,
    type=float,
    help="Seconds between empty --watch polls.",
)
@click.option(
    "--complete-planned",
    is_flag=True,
    help="Complete the claimed command with the poll/plan result.",
)
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def observer_poll(
    project_id,
    governance_url,
    session_id,
    session_token,
    command_id,
    observer_kind,
    session_label,
    print_session_token,
    provider,
    model,
    backend_mode,
    workspace,
    prompt_file,
    dispatch_gate_file,
    main_worktree,
    timeout_sec,
    early_progress_timeout_sec,
    execute,
    watch,
    max_commands,
    idle_timeout_sec,
    poll_interval_sec,
    complete_planned,
    json_output,
):
    """Claim an observer command and build a standalone route-bound plan."""
    from agent.observer_runtime import (
        ObserverPollLoopConfig,
        ObserverPollRequest,
        build_observer_poll_loop_metadata,
        build_observer_poll_plan,
        observer_poll_timeline_payload,
    )

    _validate_cli_invocation_routing(provider, model, backend_mode)
    base_url = governance_url.rstrip("/")
    encoded_project = urllib.parse.quote(project_id, safe="")
    cwd = workspace or os.getcwd()
    effective_idle_timeout_sec = 60.0 if idle_timeout_sec is None and watch else (idle_timeout_sec or 0.0)
    loop = build_observer_poll_loop_metadata(
        ObserverPollLoopConfig(
            watch=bool(watch),
            max_commands=max_commands,
            idle_timeout_sec=effective_idle_timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
    )
    result: dict = {
        "ok": False,
        "schema_version": "observer_poll_cli.v1",
        "project_id": project_id,
        "governance_url": base_url,
        "execute": execute,
        "watch": bool(watch),
        "complete_planned": complete_planned,
        "service_manager_required": False,
        "executor_worker_required": False,
        "uses_task_create": False,
        "payload_free_reminder": True,
        "reminder_payload_required": False,
        "loop": loop,
        "heartbeats": [],
        "observer_polls": [],
        "completions": [],
        "timeline": [],
        "failures": [],
    }
    active_session_id = session_id
    active_session_token = session_token
    registered_session: dict = {}

    if watch and command_id:
        result.update(
            {
                "status": "rejected",
                "error": "command-id cannot be combined with --watch",
            }
        )
        click.echo(json.dumps(result, indent=2, sort_keys=True) if json_output else result["error"])
        raise click.exceptions.Exit(1)

    if bool(active_session_id) != bool(active_session_token):
        result.update(
            {
                "status": "rejected",
                "error": "session-id and session-token must be supplied together",
            }
        )
        click.echo(json.dumps(result, indent=2, sort_keys=True) if json_output else result["error"])
        raise click.exceptions.Exit(1)

    if not active_session_id:
        register_payload = _observer_poll_session_registration_payload(
            observer_kind=observer_kind,
            session_label=session_label,
            cwd=cwd,
        )
        code, registered = _http_json(
            "POST",
            f"{base_url}/api/projects/{encoded_project}/observer-sessions/register",
            register_payload,
        )
        registered_session = _observer_poll_public_session(
            registered,
            print_session_token=print_session_token,
        )
        result["registered_session"] = registered_session
        if code >= 400 or not registered.get("ok"):
            result.update(
                {
                    "status": "rejected",
                    "error": "observer session registration failed",
                    "http_status": code,
                    "response": registered_session,
                }
            )
            click.echo(json.dumps(result, indent=2, sort_keys=True) if json_output else result["error"])
            raise click.exceptions.Exit(1)
        active_session_id = str(registered.get("observer_session_id") or registered.get("session_id") or "")
        active_session_token = str(registered.get("session_token") or "")

    result["observer_session_id"] = active_session_id
    prompt = Path(prompt_file).read_text(encoding="utf-8") if prompt_file else ""
    dispatch_gate = {}
    if dispatch_gate_file:
        try:
            parsed_gate = json.loads(Path(dispatch_gate_file).read_text(encoding="utf-8"))
        except Exception as exc:
            raise click.ClickException(f"invalid dispatch gate file: {exc}") from exc
        if not isinstance(parsed_gate, dict):
            raise click.ClickException("dispatch gate file must contain a JSON object")
        dispatch_gate = parsed_gate

    last_activity = time.monotonic()
    next_command_id = command_id
    stop_reason = ""
    while True:
        heartbeat = _observer_poll_heartbeat(
            base_url=base_url,
            project_id=project_id,
            session_id=active_session_id,
            session_token=active_session_token,
        )
        result["heartbeats"].append(heartbeat)
        loop["heartbeat_count"] = len(result["heartbeats"])
        if not heartbeat.get("ok"):
            result.update(
                {
                    "ok": False,
                    "status": "rejected",
                    "error": "observer session heartbeat failed",
                    "heartbeat": heartbeat,
                }
            )
            break

        claim_payload = {
            "session_id": active_session_id,
            "session_token": active_session_token,
        }
        claim_endpoint = "claim" if next_command_id else "next"
        if next_command_id:
            claim_payload["command_id"] = next_command_id
        next_command_id = ""
        loop["claim_attempts"] += 1
        claim_code, raw_claim_response = _http_json(
            "POST",
            f"{base_url}/api/projects/{encoded_project}/observer-commands/{claim_endpoint}",
            claim_payload,
        )
        claim_response = _observer_poll_normalize_claim_response(raw_claim_response)
        if claim_code >= 400 or not claim_response.get("ok"):
            result.update(
                {
                    "ok": False,
                    "status": "rejected",
                    "error": "observer command claim failed",
                    "http_status": claim_code,
                    "response": raw_claim_response,
                }
            )
            stop_reason = "claim_failed"
            break

        command = (
            claim_response.get("command")
            if isinstance(claim_response.get("command"), dict)
            else None
        )
        if command:
            observer_command_id = str(command.get("command_id") or "")
            result["timeline"].append(
                _observer_poll_append_timeline(
                    base_url=base_url,
                    project_id=project_id,
                    observer_command_id=observer_command_id,
                    event_type="observer_poll_claimed",
                    phase="claim",
                    status="claimed",
                    payload=observer_poll_timeline_payload(
                        observer_command_id=observer_command_id,
                        command=command,
                        event="claim",
                    ),
                )
            )

        child_heartbeat_interval_sec = 0.0
        heartbeat_interval = heartbeat.get("heartbeat_interval_sec")
        try:
            if heartbeat_interval:
                child_heartbeat_interval_sec = max(1.0, min(10.0, float(heartbeat_interval) / 2.0))
        except (TypeError, ValueError):
            child_heartbeat_interval_sec = 10.0
        if not child_heartbeat_interval_sec:
            child_heartbeat_interval_sec = 10.0

        def child_heartbeat() -> dict[str, Any]:
            child_result = _observer_poll_heartbeat(
                base_url=base_url,
                project_id=project_id,
                session_id=active_session_id,
                session_token=active_session_token,
            )
            child_result["phase"] = "execute_child"
            result["heartbeats"].append(child_result)
            loop["heartbeat_count"] = len(result["heartbeats"])
            return child_result

        plan = build_observer_poll_plan(
            ObserverPollRequest(
                project_id=project_id,
                observer_session_id=active_session_id,
                command=command,
                provider=provider,
                model=model,
                backend_mode=backend_mode,
                workspace=cwd,
                prompt=prompt,
                timeout_sec=timeout_sec,
                early_progress_timeout_sec=early_progress_timeout_sec,
                dispatch_gate=dispatch_gate,
                main_worktree=main_worktree or cwd,
                heartbeat_callback=child_heartbeat if execute else None,
                heartbeat_interval_sec=child_heartbeat_interval_sec,
            ),
            execute=execute,
        )
        result["observer_polls"].append(plan)
        result.update(
            {
                "ok": bool(plan.get("ok")),
                "status": plan.get("status") or "planned",
                "empty": bool(plan.get("empty")),
                "claim": {
                    "http_status": claim_code,
                    "empty": bool(claim_response.get("empty")),
                    "observer_command_id": str((command or {}).get("command_id") or ""),
                },
                "observer_poll": plan,
            }
        )
        if command:
            observer_command_id = str(command.get("command_id") or "")
            result["timeline"].append(
                _observer_poll_append_timeline(
                    base_url=base_url,
                    project_id=project_id,
                    observer_command_id=observer_command_id,
                    event_type="observer_poll_planned",
                    phase="plan",
                    status=str(plan.get("status") or "planned"),
                    payload=observer_poll_timeline_payload(
                        observer_command_id=observer_command_id,
                        command=command,
                        plan=plan,
                        event="plan",
                    ),
                )
            )

        if not plan.get("ok"):
            if command and plan.get("terminal_dispatch_blocker"):
                failure_result = _observer_poll_failure_result(plan)
                fail_payload = {
                    "session_id": active_session_id,
                    "session_token": active_session_token,
                    "error": failure_result.get("blocker_id")
                    or plan.get("error")
                    or "observer command terminal blocker",
                    "result": failure_result,
                }
                fail_code, fail_response = _http_json(
                    "POST",
                    (
                        f"{base_url}/api/projects/{encoded_project}/observer-commands/"
                        f"{urllib.parse.quote(str(command.get('command_id') or ''), safe='')}/fail"
                    ),
                    fail_payload,
                )
                failure = {
                    "http_status": fail_code,
                    "ok": bool(fail_response.get("ok")),
                    "observer_command_id": str(
                        (fail_response.get("command") or {}).get("command_id")
                        or command.get("command_id")
                        or ""
                    ),
                    "blocker_id": failure_result.get("blocker_id"),
                }
                result["failure"] = failure
                result["failures"].append(failure)
                if fail_code >= 400 or not fail_response.get("ok"):
                    failure["response"] = fail_response
                    result["error"] = "observer command failure projection failed"
                    stop_reason = "command_fail_failed"
                else:
                    stop_reason = "command_failed"
                observer_command_id = str(command.get("command_id") or "")
                result["timeline"].append(
                    _observer_poll_append_timeline(
                        base_url=base_url,
                        project_id=project_id,
                        observer_command_id=observer_command_id,
                        event_type="observer_poll_failed",
                        phase="fail",
                        status=(
                            "failed"
                            if fail_code < 400 and fail_response.get("ok")
                            else "fail_projection_failed"
                        ),
                        payload=observer_poll_timeline_payload(
                            observer_command_id=observer_command_id,
                            command=command,
                            plan=plan,
                            result=failure_result,
                            event="fail",
                        ),
                    )
                )
            else:
                stop_reason = "plan_rejected"
            break

        if command:
            loop["processed_count"] += 1
            last_activity = time.monotonic()
            if complete_planned:
                completion_result = _observer_poll_completion_result(plan)
                complete_payload = {
                    "session_id": active_session_id,
                    "session_token": active_session_token,
                    "result": completion_result,
                }
                complete_code, complete_response = _http_json(
                    "POST",
                    (
                        f"{base_url}/api/projects/{encoded_project}/observer-commands/"
                        f"{urllib.parse.quote(str(command.get('command_id') or ''), safe='')}/complete"
                    ),
                    complete_payload,
                )
                completion = {
                    "http_status": complete_code,
                    "ok": bool(complete_response.get("ok")),
                    "observer_command_id": str(
                        (complete_response.get("command") or {}).get("command_id") or ""
                    ),
                }
                result["completion"] = completion
                result["completions"].append(completion)
                if complete_code >= 400 or not complete_response.get("ok"):
                    result["ok"] = False
                    result["status"] = "rejected"
                    result["error"] = "observer command completion failed"
                    completion["response"] = complete_response
                    stop_reason = "completion_failed"
                observer_command_id = str(command.get("command_id") or "")
                result["timeline"].append(
                    _observer_poll_append_timeline(
                        base_url=base_url,
                        project_id=project_id,
                        observer_command_id=observer_command_id,
                        event_type="observer_poll_completed",
                        phase="complete",
                        status=(
                            "completed"
                            if complete_code < 400 and complete_response.get("ok")
                            else "completion_failed"
                        ),
                        payload=observer_poll_timeline_payload(
                            observer_command_id=observer_command_id,
                            command=command,
                            plan=plan,
                            result=completion_result,
                            event="complete",
                        ),
                    )
                )
                if stop_reason:
                    break
            elif watch:
                stop_reason = "claimed_command_left_open"
                break

            if not watch:
                stop_reason = "once"
                break
            if loop["effective_max_commands"] and loop["processed_count"] >= loop["effective_max_commands"]:
                stop_reason = "max_commands"
                break
            continue

        loop["empty_polls"] += 1
        if not watch:
            stop_reason = "empty"
            break
        idle_elapsed_sec = max(0.0, time.monotonic() - last_activity)
        loop["idle_elapsed_sec"] = idle_elapsed_sec
        if loop["idle_timeout_sec"] <= 0 or idle_elapsed_sec >= loop["idle_timeout_sec"]:
            stop_reason = "idle_timeout"
            break
        sleep_for = min(loop["poll_interval_sec"], loop["idle_timeout_sec"] - idle_elapsed_sec)
        if sleep_for > 0:
            time.sleep(sleep_for)

    loop["stop_reason"] = stop_reason or result.get("status") or ""

    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(
            f"observer poll: {result.get('status')} project={project_id} "
            f"session={active_session_id}"
        )
        poll = result.get("observer_poll") or {}
        if poll.get("observer_command_id"):
            click.echo(f"command: {poll.get('observer_command_id')} backlog={poll.get('backlog_id')}")
        click.echo(f"execute={execute} calls_models={poll.get('calls_models', False)}")
        if not result.get("ok"):
            click.echo(
                f"error: {result.get('error') or poll.get('error') or 'observer poll rejected'}",
                err=True,
            )
    if not result.get("ok"):
        raise click.exceptions.Exit(1)


@observer.command("run")
@click.option("--project-id", required=True, help="Governance project id.")
@click.option("--backlog-id", required=True, help="Backlog id the observer will supervise.")
@click.option("--route-context-hash", required=True, help="Route context hash for this observer run.")
@click.option("--prompt-contract-id", required=True, help="Prompt contract id for this observer run.")
@click.option("--prompt-contract-hash", default="", help="Optional prompt contract hash.")
@click.option("--route-token-ref", default="", help="Optional route token id/ref.")
@click.option("--provider", default="openai", help="Provider name, e.g. openai or anthropic.")
@click.option("--model", default="", help="Optional provider model override.")
@click.option("--backend-mode", default="codex_cli", help="Invocation backend, e.g. codex_cli, claude_cli, openai_api, anthropic_api.")
@click.option("--workspace", default="", help="Observer workspace. Defaults to current working directory.")
@click.option("--prompt-file", default=None, type=click.Path(exists=True, dir_okay=False, readable=True), help="Optional observer prompt file.")
@click.option(
    "--dispatch-gate-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="MF subagent dispatch gate evidence JSON required for live code-mutating backends.",
)
@click.option("--main-worktree", default="", help="Target/main worktree path blocked by one-hop dispatch policy.")
@click.option("--timeout-sec", default=120, type=int, help="Observer invocation timeout if --execute is used.")
@click.option(
    "--early-progress-timeout-sec",
    default=20.0,
    type=float,
    help="Fail codex_cli workers that produce no output or worktree changes before this timeout.",
)
@click.option("--execute", is_flag=True, help="Actually invoke the configured provider. Default is dry-run evidence only.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def observer_run(
    project_id,
    backlog_id,
    route_context_hash,
    prompt_contract_id,
    prompt_contract_hash,
    route_token_ref,
    provider,
    model,
    backend_mode,
    workspace,
    prompt_file,
    dispatch_gate_file,
    main_worktree,
    timeout_sec,
    early_progress_timeout_sec,
    execute,
    json_output,
):
    """Build or execute a route-bound observer invocation."""
    from agent.observer_runtime import ObserverRunRequest, run_observer
    from agent.ai_invocation import RoutePromptContract

    _validate_cli_invocation_routing(provider, model, backend_mode)
    prompt = Path(prompt_file).read_text(encoding="utf-8") if prompt_file else ""
    dispatch_gate = {}
    if dispatch_gate_file:
        try:
            parsed_gate = json.loads(Path(dispatch_gate_file).read_text(encoding="utf-8"))
        except Exception as exc:
            raise click.ClickException(f"invalid dispatch gate file: {exc}") from exc
        if not isinstance(parsed_gate, dict):
            raise click.ClickException("dispatch gate file must contain a JSON object")
        dispatch_gate = parsed_gate
    request = ObserverRunRequest(
        project_id=project_id,
        backlog_id=backlog_id,
        route=RoutePromptContract(
            route_context_hash=route_context_hash,
            prompt_contract_id=prompt_contract_id,
            prompt_contract_hash=prompt_contract_hash,
            route_token_ref=route_token_ref,
        ),
        provider=provider,
        model=model,
        backend_mode=backend_mode,
        workspace=workspace or os.getcwd(),
        prompt=prompt,
        timeout_sec=timeout_sec,
        early_progress_timeout_sec=early_progress_timeout_sec,
        dispatch_gate=dispatch_gate,
        main_worktree=main_worktree or os.getcwd(),
    )
    result = run_observer(request, execute=execute)
    result.update(_observer_poll_invocation_fields(result))
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(f"observer run: {result.get('status')} project={project_id} backlog={backlog_id}")
        invocation = (
            result.get("invocation_result")
            or result.get("invocation")
            or result.get("invocation_request")
            or {}
        )
        click.echo(f"backend: {invocation.get('backend_mode', backend_mode)} execute={execute}")
        click.echo(f"route: {route_context_hash}")
        if not result.get("ok"):
            click.echo("missing: " + ", ".join(result.get("missing") or []), err=True)
    if not result.get("ok"):
        raise click.exceptions.Exit(1)


@observer.command("dogfood")
@click.option("--project-id", required=True, help="Governance project id.")
@click.option("--backlog-id", required=True, help="Backlog id the observer will supervise.")
@click.option("--route-context-hash", required=True, help="Route context hash for this observer run.")
@click.option("--prompt-contract-id", required=True, help="Prompt contract id for this observer run.")
@click.option("--prompt-contract-hash", default="", help="Optional prompt contract hash.")
@click.option("--route-token-ref", default="", help="Optional route token id/ref.")
@click.option("--route-id", default="", help="Route id for route-owned dogfood evidence.")
@click.option("--precheck-run-id", default="", help="Optional judgment topology precheck id for evidence.")
@click.option("--visible-injection-manifest-hash", default="", help="Visible injection manifest hash for route-owned dogfood evidence.")
@click.option("--provider", default="openai", help="Provider name, e.g. openai or anthropic.")
@click.option("--model", default="", help="Optional provider model override.")
@click.option("--backend-mode", default="codex_cli", help="Invocation backend, e.g. codex_cli, claude_cli, openai_api, anthropic_api.")
@click.option("--main-worktree", default="", help="Target/main worktree path blocked by dispatch policy. Defaults to cwd.")
@click.option("--workspace-root", default="", help="Parent workspace root for generated worker worktrees. Defaults to main worktree parent.")
@click.option("--owned-file", "owned_files", multiple=True, required=True, help="Owned file fence for the worker. Repeatable.")
@click.option("--task-id", default="", help="Worker task id. Defaults to backlog id.")
@click.option("--worker-id", default="", help="Worker id used in deterministic worktree planning.")
@click.option("--attempt", default=1, type=int, help="Worker attempt number.")
@click.option("--worktree-root", default=".worktrees", help="Worktree root under workspace-root.")
@click.option("--branch-prefix", default="dogfood", help="Generated branch prefix.")
@click.option("--merge-queue-id", default="", help="Merge queue id. Defaults to a deterministic dogfood id.")
@click.option("--fence-token", default="", help="Fence token. Defaults to a deterministic dogfood token.")
@click.option(
    "--branch-runtime-registration-ref",
    default="",
    help="Allocation source/API/CLI reference; not the worker runtime_context_id.",
)
@click.option(
    "--runtime-context-id",
    default="",
    help="Worker runtime_context_id returned by branch allocation.",
)
@click.option(
    "--branch-runtime-evidence-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help=(
        "Optional JSON allocation evidence object, including source_ref, "
        "runtime_context_id, and persisted branch context."
    ),
)
@click.option("--graph-trace-id", "graph_trace_ids", multiple=True, required=True, help="Graph query trace id proving graph-first evidence. Repeatable.")
@click.option("--base-commit", default="", help="Optional base commit. Defaults to main worktree HEAD.")
@click.option("--target-head-commit", default="", help="Optional target HEAD commit. Defaults to base commit.")
@click.option("--timeout-sec", default=120, type=int, help="Observer invocation timeout if --execute is used.")
@click.option(
    "--early-progress-timeout-sec",
    default=20.0,
    type=float,
    help="Fail codex_cli workers that produce no output or worktree changes before this timeout.",
)
@click.option("--gate-output", "--gate-output-path", "gate_output", default="", type=click.Path(dir_okay=False), help="Optional path to write generated dispatch gate JSON.")
@click.option("--materialize-worktree", is_flag=True, help="Create the gated worker worktree before planning/execution.")
@click.option("--execute", is_flag=True, help="Invoke the configured provider after gate and worktree preflight. Default is dry-run evidence only.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def observer_dogfood(
    project_id,
    backlog_id,
    route_context_hash,
    prompt_contract_id,
    prompt_contract_hash,
    route_token_ref,
    route_id,
    precheck_run_id,
    visible_injection_manifest_hash,
    provider,
    model,
    backend_mode,
    main_worktree,
    workspace_root,
    owned_files,
    task_id,
    worker_id,
    attempt,
    worktree_root,
    branch_prefix,
    merge_queue_id,
    fence_token,
    branch_runtime_registration_ref,
    runtime_context_id,
    branch_runtime_evidence_file,
    graph_trace_ids,
    base_commit,
    target_head_commit,
    timeout_sec,
    early_progress_timeout_sec,
    gate_output,
    materialize_worktree,
    execute,
    json_output,
):
    """Plan or execute a controlled source-backed dogfood observer run."""
    from agent.ai_invocation import RoutePromptContract
    from agent.observer_runtime import (
        DogfoodObserverPlanRequest,
        build_dogfood_observer_run_plan,
    )

    _validate_cli_invocation_routing(provider, model, backend_mode)
    branch_runtime_evidence: dict[str, Any] = {}
    if branch_runtime_evidence_file:
        try:
            parsed_evidence = json.loads(Path(branch_runtime_evidence_file).read_text(encoding="utf-8"))
        except Exception as exc:
            raise click.ClickException(f"invalid branch runtime evidence file: {exc}") from exc
        if not isinstance(parsed_evidence, dict):
            raise click.ClickException("branch runtime evidence file must contain a JSON object")
        branch_runtime_evidence = parsed_evidence
    request = DogfoodObserverPlanRequest(
        project_id=project_id,
        backlog_id=backlog_id,
        route=RoutePromptContract(
            route_context_hash=route_context_hash,
            prompt_contract_id=prompt_contract_id,
            prompt_contract_hash=prompt_contract_hash,
            route_token_ref=route_token_ref,
        ),
        provider=provider,
        model=model,
        backend_mode=backend_mode,
        main_worktree=main_worktree or os.getcwd(),
        workspace_root=workspace_root,
        owned_files=tuple(owned_files),
        task_id=task_id,
        worker_id=worker_id,
        attempt=attempt,
        worktree_root=worktree_root,
        branch_prefix=branch_prefix,
        merge_queue_id=merge_queue_id,
        fence_token=fence_token,
        graph_trace_ids=tuple(graph_trace_ids),
        branch_runtime_registration_ref=branch_runtime_registration_ref,
        branch_runtime_evidence=branch_runtime_evidence,
        runtime_context_id=runtime_context_id,
        base_commit=base_commit,
        target_head_commit=target_head_commit,
        timeout_sec=timeout_sec,
        early_progress_timeout_sec=early_progress_timeout_sec,
        route_id=route_id,
        precheck_run_id=precheck_run_id,
        visible_injection_manifest_hash=visible_injection_manifest_hash,
    )
    result = build_dogfood_observer_run_plan(
        request,
        execute=execute,
        materialize_worktree=materialize_worktree,
    )
    if gate_output:
        route_allowed = bool((result.get("route_identity_validation") or {}).get("allowed", True))
        gate_allowed = bool((result.get("dispatch_gate_validation") or {}).get("allowed", False))
        if route_allowed and gate_allowed:
            gate_path = Path(gate_output)
            gate_path.parent.mkdir(parents=True, exist_ok=True)
            gate_path.write_text(
                json.dumps(result.get("dispatch_gate") or {}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            result["gate_output"] = str(gate_path)
        else:
            result["gate_output_skipped"] = {
                "path": gate_output,
                "reason": "route_identity_or_dispatch_gate_validation_failed",
                "route_identity_allowed": route_allowed,
                "dispatch_gate_allowed": gate_allowed,
            }
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(f"observer dogfood: {result.get('status')} project={project_id} backlog={backlog_id}")
        click.echo(f"execute={execute} calls_models={result.get('calls_models')}")
        runtime_context = result.get("runtime_context") or {}
        if runtime_context.get("worktree_path"):
            click.echo(f"worktree: {runtime_context.get('worktree_path')}")
        executable_launch = result.get("executable_worker_launch") or {}
        if executable_launch.get("command_display"):
            click.echo(f"worker launch command: {executable_launch.get('command_display')}")
        missing_launch = executable_launch.get("missing_fields") or []
        if missing_launch:
            click.echo(f"missing launch fields: {', '.join(missing_launch)}", err=True)
        if gate_output:
            click.echo(f"gate: {gate_output}")
        if not result.get("ok"):
            validation = (
                result.get("route_identity_validation")
                or result.get("dispatch_gate_validation")
                or result.get("materialization_preflight")
                or result.get("execute_preflight")
                or {}
            )
            click.echo(f"error: {validation.get('error', 'observer dogfood rejected')}", err=True)
    if not result.get("ok"):
        raise click.exceptions.Exit(1)


@observer.group("runtime-text")
def observer_runtime_text():
    """Observer runtime text preparation."""
    pass


@observer_runtime_text.command("prepare")
@click.option("--project-id", required=True, help="Governance project id.")
@click.option("--backlog-id", required=True, help="Backlog id for the bounded worker.")
@click.option("--route-context-hash", required=True, help="Route context hash for this worker launch.")
@click.option("--prompt-contract-id", required=True, help="Prompt contract id for this worker launch.")
@click.option("--prompt-contract-hash", default="", help="Optional prompt contract hash.")
@click.option("--route-token-ref", default="", help="Optional route token id/ref.")
@click.option("--route-id", default="", help="Parent route id for route-owned evidence.")
@click.option("--precheck-run-id", default="", help="Optional route/topology precheck id.")
@click.option("--visible-injection-manifest-hash", default="", help="Public-safe visible injection manifest hash.")
@click.option("--main-worktree", default="", help="Target/main worktree path blocked by dispatch policy. Defaults to cwd.")
@click.option("--workspace-root", default="", help="Parent workspace root for generated worker worktrees. Defaults to main worktree parent.")
@click.option("--owned-file", "owned_files", multiple=True, help="Owned file fence for the worker. Repeatable.")
@click.option(
    "--observer-command-id",
    default="",
    help=(
        "Claimed backlog-specific execute_backlog_row command id required for "
        "startup/read-receipt lineage."
    ),
)
@click.option("--task-id", default="", help="Worker task id. Defaults to backlog id.")
@click.option("--parent-task-id", default="", help="Parent observer/MF task id. Defaults to backlog id.")
@click.option("--worker-id", default="", help="Worker id used in deterministic worktree planning.")
@click.option("--attempt", default=1, type=int, help="Worker attempt number.")
@click.option("--worktree-root", default=".worktrees", help="Worktree root under workspace-root.")
@click.option("--branch-prefix", default="runtime-text", help="Generated branch prefix.")
@click.option("--merge-queue-id", default="", help="Merge queue id. Defaults to a deterministic runtime-text id.")
@click.option("--fence-token", default="", help="Fence token. Defaults to a deterministic runtime-text token.")
@click.option(
    "--branch-runtime-registration-ref",
    default="",
    help="Allocation source/API/CLI reference; not the worker runtime_context_id.",
)
@click.option(
    "--runtime-context-id",
    default="",
    help="Worker contract runtime_context_id from persisted allocation evidence.",
)
@click.option(
    "--branch-runtime-evidence-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help=(
        "Optional JSON allocation evidence object, including source_ref, "
        "runtime_context_id, and persisted branch context."
    ),
)
@click.option(
    "--graph-trace-id",
    "graph_trace_ids",
    multiple=True,
    help=(
        "Optional prelaunch graph context trace id. Repeatable; does not satisfy "
        "worker-owned finish graph_trace_evidence."
    ),
)
@click.option("--base-commit", default="", help="Optional base commit. Defaults to main worktree HEAD.")
@click.option("--target-head-commit", default="", help="Optional target HEAD commit. Defaults to base commit.")
@click.option("--acceptance-criterion", "acceptance_criteria", multiple=True, help="Acceptance criterion for the worker contract. Repeatable.")
@click.option("--test-command", "test_commands", multiple=True, help="Focused test command for the worker contract. Repeatable.")
@click.option(
    "--prompt-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Optional worker prompt file.",
)
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def observer_runtime_text_prepare(
    project_id,
    backlog_id,
    route_context_hash,
    prompt_contract_id,
    prompt_contract_hash,
    route_token_ref,
    route_id,
    precheck_run_id,
    visible_injection_manifest_hash,
    main_worktree,
    workspace_root,
    owned_files,
    observer_command_id,
    task_id,
    parent_task_id,
    worker_id,
    attempt,
    worktree_root,
    branch_prefix,
    merge_queue_id,
    fence_token,
    branch_runtime_registration_ref,
    runtime_context_id,
    branch_runtime_evidence_file,
    graph_trace_ids,
    base_commit,
    target_head_commit,
    acceptance_criteria,
    test_commands,
    prompt_file,
    json_output,
):
    """Prepare runtime launch text for a host-created mf_sub worker."""
    from agent.ai_invocation import RoutePromptContract
    from agent.observer_runtime import (
        ObserverRuntimeTextPrepareRequest,
        build_observer_runtime_text_context,
    )

    prompt = Path(prompt_file).read_text(encoding="utf-8") if prompt_file else ""
    branch_runtime_evidence: dict[str, Any] = {}
    if branch_runtime_evidence_file:
        try:
            parsed_evidence = json.loads(Path(branch_runtime_evidence_file).read_text(encoding="utf-8"))
        except Exception as exc:
            raise click.ClickException(f"invalid branch runtime evidence file: {exc}") from exc
        if not isinstance(parsed_evidence, dict):
            raise click.ClickException("branch runtime evidence file must contain a JSON object")
        branch_runtime_evidence = parsed_evidence
    request = ObserverRuntimeTextPrepareRequest(
        project_id=project_id,
        backlog_id=backlog_id,
        route=RoutePromptContract(
            route_context_hash=route_context_hash,
            prompt_contract_id=prompt_contract_id,
            prompt_contract_hash=prompt_contract_hash,
            route_token_ref=route_token_ref,
        ),
        main_worktree=main_worktree or os.getcwd(),
        workspace_root=workspace_root,
        owned_files=tuple(owned_files),
        observer_command_id=observer_command_id,
        task_id=task_id,
        parent_task_id=parent_task_id,
        worker_id=worker_id,
        attempt=attempt,
        worktree_root=worktree_root,
        branch_prefix=branch_prefix,
        merge_queue_id=merge_queue_id,
        fence_token=fence_token,
        graph_trace_ids=tuple(graph_trace_ids),
        branch_runtime_registration_ref=branch_runtime_registration_ref,
        branch_runtime_evidence=branch_runtime_evidence,
        runtime_context_id=runtime_context_id,
        base_commit=base_commit,
        target_head_commit=target_head_commit,
        prompt=prompt,
        acceptance_criteria=tuple(acceptance_criteria),
        test_commands=tuple(test_commands),
        route_id=route_id,
        precheck_run_id=precheck_run_id,
        visible_injection_manifest_hash=visible_injection_manifest_hash,
    )
    result = build_observer_runtime_text_context(request)
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(
            f"observer runtime-text prepare: {result.get('status')} "
            f"project={project_id} backlog={backlog_id}"
        )
        click.echo(f"runtime_context_id: {result.get('runtime_context_id')}")
        click.echo(f"launch_text_hash: {result.get('launch_text_hash')}")
        executable_launch = result.get("executable_worker_launch") or {}
        if executable_launch.get("command_display"):
            click.echo(f"worker launch command: {executable_launch.get('command_display')}")
        missing_launch = executable_launch.get("missing_fields") or []
        if missing_launch:
            click.echo(f"missing launch fields: {', '.join(missing_launch)}", err=True)
        if not result.get("ok"):
            validation = result.get("dispatch_gate_validation") or {}
            click.echo(
                f"error: {result.get('input_error') or validation.get('error') or 'runtime text rejected'}",
                err=True,
            )
    if not result.get("ok"):
        raise click.exceptions.Exit(1)


@main.group()
def mf():
    """Manual-fix workflow checks."""
    pass


@mf.command("precommit-check")
@click.option("--plugin-state", default="", help="Optional plugin update state JSON path.")
@click.option(
    "--route-consumption-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Optional route-context consumption evidence JSON path.",
)
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def mf_precommit_check(plugin_state, route_consumption_file, json_output):
    """Run local MF pre-commit guards that do not mutate governance state."""
    from agent.plugin_installer import (
        format_plugin_update_state_status,
        plugin_update_state_status,
    )

    plugin_status = plugin_update_state_status(state_path=plugin_state or None)
    route_status = _mf_route_consumption_file_status(route_consumption_file)
    result = {
        "ok": bool(plugin_status.get("ok")) and bool(route_status.get("ok")),
        "checks": {
            "plugin_update_state": plugin_status,
            "route_context_consumption": route_status,
        },
    }
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo("Aming Claw MF precommit check")
        click.echo("")
        click.echo(format_plugin_update_state_status(plugin_status))
        if route_consumption_file:
            status = "pass" if route_status.get("ok") else "fail"
            click.echo(f"route context consumption: {status}")
            missing = route_status.get("missing_requirement_ids") or []
            if missing:
                click.echo(f"missing: {', '.join(missing)}")
    if not result["ok"]:
        raise click.exceptions.Exit(1)


def _mf_route_consumption_file_status(path: str) -> dict:
    if not path:
        return {"status": "skipped", "ok": True}
    from agent.governance.task_timeline import mf_route_context_gate_verification

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "fail", "ok": False, "error": f"invalid route consumption file: {exc}"}
    if not isinstance(payload, dict):
        return {"status": "fail", "ok": False, "error": "route consumption file must be a JSON object"}
    raw_events = payload.get("timeline_evidence") or payload.get("events") or payload.get("route_events")
    if isinstance(raw_events, dict):
        events = [raw_events]
    elif isinstance(raw_events, list):
        events = [item for item in raw_events if isinstance(item, dict)]
    else:
        events = [payload] if any(key in payload for key in ("route_context_hash", "route_identity")) else []
    contract = payload.get("contract") if isinstance(payload.get("contract"), dict) else payload
    gate = mf_route_context_gate_verification(events, contract=contract)
    return {
        "status": "pass" if gate.get("passed") else "fail",
        "ok": bool(gate.get("passed")),
        "required": bool(gate.get("required")),
        "missing_requirement_ids": gate.get("missing_requirement_ids") or [],
        "present_requirement_ids": gate.get("present_requirement_ids") or [],
        "topology_policy": gate.get("topology_policy") or {},
    }


@mf.command("dispatch-gate")
@click.option(
    "--contract-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Existing MF subagent dispatch contract JSON path.",
)
@click.option("--target-worktree", default="", help="Target worktree path to block same-worktree dispatch.")
@click.option("--main-worktree", default="", help="Main worktree path to block same-worktree dispatch.")
def mf_dispatch_gate(contract_file, target_worktree, main_worktree):
    """Validate MF subagent dispatch evidence before worker handoff."""
    from agent.governance.mf_subagent_contract import validate_mf_subagent_dispatch_gate

    try:
        payload = json.loads(Path(contract_file).read_text(encoding="utf-8"))
        result = validate_mf_subagent_dispatch_gate(
            payload,
            target_worktree_path=target_worktree,
            main_worktree_path=main_worktree,
        )
    except Exception as exc:
        click.echo(f"REJECT: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc

    click.echo(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
