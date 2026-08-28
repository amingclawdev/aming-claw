#!/bin/bash
# AC stable promotion. All evidence is projected from the live AC timeline
# before any Git/process write; legacy branch shortcuts are fail-closed.

set -euo pipefail
cd "$(dirname "$0")/.."

BOOTSTRAP_ANCHOR="a25838f15f949ac434cf78e03f20760e82ff81f0"
ROLLBACK_BASELINE="1012ec422160738356678f721ea470d583fe2be6"
ROLLBACK_BACKLOG="AC-PROMOTION-ACTIVATION-ROLLBACK-RESTART-P0-20260827"
ROLLBACK_CEX="cex-direct-main-60df8f3e0c9a1fbce338"
STABLE_BRANCH="codex/direct-no-pass-post-reconcile-r2"
DEV_BRANCH="codex/ac-dev"
STABLE_PORT="40000"
MANIFEST=""
DRY_RUN="false"
MODE="legacy"
ACTIVATION_PLAN=""

usage() {
    echo "Usage: $0 [--prepare|--activate|--recover] --promotion-manifest <manifest.json> --activation-plan <outside-repo-plan.json> [--dry-run]" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --promotion-manifest)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            MANIFEST="$2"; shift 2 ;;
        --activation-plan)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            ACTIVATION_PLAN="$2"; shift 2 ;;
        --prepare) MODE="prepare"; shift ;;
        --activate) MODE="activate"; shift ;;
        --recover) MODE="recover"; shift ;;
        --dry-run) DRY_RUN="true"; shift ;;
        *)
            echo "Refusing legacy merge-and-deploy argument: $1" >&2
            usage; exit 2 ;;
    esac
done

if [ "$MODE" = "activate" ] || [ "$MODE" = "recover" ]; then
    if [ -z "$ACTIVATION_PLAN" ] || [ ! -f "$ACTIVATION_PLAN" ]; then
        echo "Promotion blocked: --activation-plan must name an existing prepared plan." >&2
        exit 2
    fi
    if [ -z "${GOV_COORDINATOR_TOKEN:-}" ] && [ "$DRY_RUN" != "true" ]; then
        echo "Promotion blocked: GOV_COORDINATOR_TOKEN is required for activation." >&2
        exit 2
    fi
    python3 - "$MODE" "$ACTIVATION_PLAN" "$DRY_RUN" "$0" <<'PY'
import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


class PromotionFailure(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha(value):
    raw = value if isinstance(value, bytes) else canonical(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def fail(code, message):
    raise PromotionFailure(code, message)


def scrubbed_environment(extra=None):
    sensitive_fragments = (
        "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PRIVATE_KEY",
        "OPERATOR_APPROVAL", "SIGNOFF",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(fragment in key.upper() for fragment in sensitive_fragments)
    }
    environment.update(dict(extra or {}))
    return environment


def safe_json_file(path):
    raw = Path(path).read_bytes()
    try:
        return json.loads(raw), raw
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        fail("activation_plan_json_invalid", "activation plan is not canonical JSON")


def validate_plan(plan, raw, script_path):
    required = {
        "schema_version", "plan_id", "created_at", "project_id", "backlog_id",
        "contract_execution_id", "stable_branch", "dev_branch", "stable_worktree",
        "dev_worktree", "stable_anchor_commit", "candidate_commit",
        "candidate_tree_sha", "stable_runtime_source_sha256",
        "candidate_runtime_source_sha256", "implementation_delta", "promotion_delta",
        "qa_candidate_intent_sha256", "custody_authority",
        "manifest", "manifest_sha256", "promotion_intent_sha256",
        "promotion_manifest_sha256", "verifier_sha256", "precheck_receipt",
        "precheck_receipt_hash", "stable_database_path",
        "stable_database_identity", "stable_database_relative_path",
        "stable_database_path_sha256", "graph_identity_hash", "old_process",
        "old_launch_spec", "old_launch_environment", "candidate_launch_spec",
        "candidate_launch_environment", "runtime_process_executable",
        "stable_port", "bind_host",
        "lane_commands", "forward_patch_b64", "forward_patch_sha256",
        "reverse_apply_patch_sha256", "journal_path", "lock_path",
        "completion_body_template", "activation_policy", "plan_hash",
    }
    if set(plan) != required:
        fail("activation_plan_shape_invalid", "activation plan has missing or extra fields")
    core = {key: value for key, value in plan.items() if key != "plan_hash"}
    if plan.get("schema_version") != "ac_stable_activation_plan.v2" or sha(core) != plan.get("plan_hash"):
        fail("activation_plan_hash_mismatch", "activation plan digest mismatch")
    if raw != (canonical(plan) + "\n").encode("utf-8"):
        fail("activation_plan_not_canonical", "activation plan bytes are not canonical")
    if plan.get("project_id") != "aming-claw" or plan.get("stable_port") != 40000 or plan.get("bind_host") != "127.0.0.1":
        fail("activation_plan_scope_invalid", "activation plan is not exact AC stable scope")
    if plan.get("stable_branch") != "codex/direct-no-pass-post-reconcile-r2" or plan.get("dev_branch") != "codex/ac-dev":
        fail("activation_plan_branch_invalid", "activation plan branch scope mismatch")
    if plan.get("stable_anchor_commit") != "a25838f15f949ac434cf78e03f20760e82ff81f0":
        fail("activation_plan_anchor_invalid", "activation plan anchor mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", str(plan.get("candidate_commit") or "")):
        fail("activation_plan_candidate_invalid", "activation candidate is malformed")
    if plan.get("verifier_sha256") != sha(Path(script_path).read_bytes()):
        fail("activation_plan_verifier_drift", "activation verifier source changed")
    manifest = plan.get("manifest") if isinstance(plan.get("manifest"), dict) else {}
    precheck = plan.get("precheck_receipt") if isinstance(plan.get("precheck_receipt"), dict) else {}
    if not (
        manifest.get("schema_version") == "ac_stable_promotion_manifest.v2"
        and plan.get("manifest_sha256") == sha(manifest)
        and plan.get("promotion_intent_sha256") == manifest.get("promotion_intent_sha256")
        and plan.get("promotion_manifest_sha256") == manifest.get("promotion_manifest_sha256")
        and plan.get("implementation_delta") == manifest.get("implementation_delta")
        and plan.get("promotion_delta") == manifest.get("promotion_delta")
        and plan.get("qa_candidate_intent_sha256")
        == manifest.get("qa_candidate_intent_sha256")
        and plan.get("custody_authority")
        == manifest.get("custody_authority")
        and plan.get("candidate_tree_sha") == manifest.get("candidate_tree_sha")
        and plan.get("stable_runtime_source_sha256")
        == manifest.get("stable_runtime_source_sha256")
        and plan.get("candidate_runtime_source_sha256")
        == manifest.get("candidate_runtime_source_sha256")
        and plan.get("activation_policy") == manifest.get("activation_policy")
        and precheck.get("schema_version") == "ac_stable_promotion_precheck_receipt.v2"
        and plan.get("precheck_receipt_hash") == precheck.get("receipt_hash")
        and precheck.get("receipt_hash")
        == sha({key: value for key, value in precheck.items() if key != "receipt_hash"})
        and precheck.get("candidate_commit") == plan.get("candidate_commit")
        and precheck.get("candidate_tree_sha") == plan.get("candidate_tree_sha")
        and precheck.get("stable_runtime_source_sha256")
        == plan.get("stable_runtime_source_sha256")
        and precheck.get("candidate_runtime_source_sha256")
        == plan.get("candidate_runtime_source_sha256")
        and precheck.get("qa_candidate_intent_sha256")
        == plan.get("qa_candidate_intent_sha256")
        and precheck.get("custody_authority")
        == plan.get("custody_authority")
        and precheck.get("stable_anchor_commit") == plan.get("stable_anchor_commit")
        and precheck.get("stable_database_identity") == plan.get("stable_database_identity")
    ):
        fail("activation_plan_evidence_mismatch", "activation plan evidence is not exact v2 authority")
    patch = base64.b64decode(plan.get("forward_patch_b64") or "", validate=True)
    if not (
        sha(patch)
        == plan.get("forward_patch_sha256")
        == plan.get("reverse_apply_patch_sha256")
        == (plan.get("promotion_delta") or {}).get("diff_sha256")
        == (manifest.get("promotion_delta") or {}).get("diff_sha256")
    ):
        fail("activation_plan_patch_hash_mismatch", "prepared reverse patch digest mismatch")
    python_bin = str((plan.get("candidate_launch_spec") or [""])[0])
    expected_lanes = [
        [python_bin, "-m", "pytest", "-q", "agent/tests/test_graph_governance_api.py", "-k", "promotion_rollback"],
        [python_bin, "-m", "pytest", "-q", "agent/tests/test_graph_governance_api.py", "-k", "direct_main"],
        [python_bin, "-m", "pytest", "-q", "agent/tests/test_graph_governance_api.py", "-k", "mf_parallel"],
        [python_bin, "-m", "pytest", "-q", "agent/tests/test_graph_governance_api.py", "-k", "mf_batch_parallel"],
    ]
    if plan.get("lane_commands") != expected_lanes:
        fail("activation_plan_lane_commands_invalid", "activation lane commands are not hard-coded exact commands")
    expected_candidate_launch = [
        python_bin, "-m", "agent.cli", "start", "--runtime-plane", "stable",
        "--port", "40000", "--stable-anchor-commit", plan.get("candidate_commit"),
        "--workspace", plan.get("stable_worktree"), "--shared-volume-path",
        str(Path(plan.get("stable_worktree")) / "shared-volume"),
    ]
    expected_old_launch = [
        python_bin, "-m", "agent.cli", "start", "--workspace",
        plan.get("stable_worktree"), "--port", "40000",
    ]
    expected_environment = {
        "PYTHONPATH": plan.get("stable_worktree"),
        "SHARED_VOLUME_PATH": str(Path(plan.get("stable_worktree")) / "shared-volume"),
    }
    expected_relative_database = (
        "shared-volume/codex-tasks/state/governance/aming-claw/governance.db"
    )
    runtime_process_executable = str(plan.get("runtime_process_executable") or "")
    expected_old_process_command = " ".join(
        [runtime_process_executable, *expected_old_launch[1:]]
    )
    if not (
        Path(python_bin).is_absolute()
        and plan.get("candidate_launch_spec") == expected_candidate_launch
        and plan.get("old_launch_spec") == expected_old_launch
        and plan.get("candidate_launch_environment") == expected_environment
        and plan.get("old_launch_environment") == expected_environment
        and Path(runtime_process_executable).is_absolute()
        and (plan.get("old_process") or {}).get("command")
        == expected_old_process_command
        and plan.get("stable_database_relative_path") == expected_relative_database
        and plan.get("stable_database_path")
        == str(Path(plan.get("stable_worktree")) / expected_relative_database)
        and plan.get("stable_database_path_sha256")
        == sha(expected_relative_database.encode("utf-8"))
        and re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(plan.get("stable_runtime_source_sha256") or ""),
        )
        and re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(plan.get("candidate_runtime_source_sha256") or ""),
        )
    ):
        fail("activation_plan_launch_spec_invalid", "activation launch specs are not exact hard-coded argv")
    for field in ("stable_worktree", "dev_worktree", "journal_path", "lock_path"):
        if not Path(str(plan.get(field) or "")).is_absolute():
            fail("activation_plan_path_invalid", f"{field} must be absolute")
    plan_path = Path(sys.argv[2]).resolve(strict=True)
    for repo in (Path(plan["stable_worktree"]).resolve(), Path(plan["dev_worktree"]).resolve()):
        try:
            plan_path.relative_to(repo)
        except ValueError:
            pass
        else:
            fail("activation_plan_inside_repo", "activation plan must live outside every repository worktree")
    mode = stat.S_IMODE(plan_path.stat(follow_symlinks=False).st_mode)
    if plan_path.is_symlink() or mode != 0o600:
        fail("activation_plan_file_mode_invalid", "activation plan must be non-symlink mode 0600")
    return patch


class Journal:
    def __init__(self, path, plan_hash):
        self.path = Path(path)
        self.plan_hash = plan_hash
        self.rows = []
        if self.path.exists():
            if self.path.is_symlink() or stat.S_IMODE(self.path.stat(follow_symlinks=False).st_mode) != 0o600:
                fail("activation_journal_identity_invalid", "activation journal identity is invalid")
            previous = ""
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    fail("activation_journal_malformed", "activation journal contains malformed evidence")
                core = {key: value for key, value in row.items() if key != "entry_hash"}
                if row.get("plan_hash") != plan_hash or row.get("previous_entry_hash") != previous or sha(core) != row.get("entry_hash"):
                    fail("activation_journal_chain_invalid", "activation journal hash chain is invalid")
                previous = row["entry_hash"]
                self.rows.append(row)
            terminals = [row for row in self.rows if row.get("state") in {"COMPLETED", "ROLLED_BACK"}]
            if len(terminals) > 1:
                fail("activation_journal_terminal_ambiguous", "activation journal has ambiguous terminal receipts")
            if any(row.get("state") == "ROLLBACK_FATAL" for row in self.rows):
                fail("activation_journal_rollback_fatal", "activation journal records an unresolved rollback failure")
            if any(row.get("state") == "COMPLETION_AMBIGUOUS" for row in self.rows):
                fail("activation_journal_completion_ambiguous", "activation journal records an ambiguous completion receipt")

    def append(self, state, evidence=None):
        previous = self.rows[-1]["entry_hash"] if self.rows else ""
        core = {
            "schema_version": "ac_stable_activation_journal_entry.v2",
            "plan_hash": self.plan_hash,
            "sequence": len(self.rows) + 1,
            "state": state,
            "evidence": evidence or {},
            "previous_entry_hash": previous,
        }
        row = {**core, "entry_hash": sha(core)}
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                fail("activation_journal_identity_invalid", "activation journal must be a regular mode-0600 file")
            payload = (canonical(row) + "\n").encode("utf-8")
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    fail("activation_journal_write_failed", "activation journal write was incomplete")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        parent_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        self.rows.append(row)
        return row


class RealOps:
    def run(self, args, cwd, code, *, input_bytes=None):
        result = subprocess.run(args, cwd=cwd, input=input_bytes, capture_output=True, check=False)
        if result.returncode != 0:
            fail(code, f"command failed: {args[0]}")
        return result.stdout

    def git(self, root, *args, code="activation_git_failed"):
        return self.run(["git", *args], root, code).decode("utf-8", errors="strict").strip()

    def pid_identity(self, pid):
        birth = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, check=False).stdout.strip()
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False).stdout.strip()
        return {"pid": int(pid), "birth": birth, "command": command}

    def pid_alive(self, pid):
        state = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not state or state.startswith("Z"):
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False

    def port_pids(self, port):
        result = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True, check=False)
        return sorted({int(item) for item in result.stdout.split() if item.isdigit()})

    def health(self, port):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
            return json.load(response)

    def stop(self, identity, port, code, *, require_listener=True):
        current = self.pid_identity(identity["pid"])
        listeners = self.port_pids(port)
        listener_ok = (
            listeners == [identity["pid"]]
            if require_listener
            else listeners in ([], [identity["pid"]])
        )
        if current != identity or not listener_ok:
            fail(code + "_identity", "process/listener identity changed")
        os.kill(identity["pid"], signal.SIGTERM)
        for _ in range(100):
            if not self.pid_alive(identity["pid"]):
                break
            time.sleep(0.1)
        if self.pid_alive(identity["pid"]):
            fail(code + "_timeout", "exact process did not stop; no force kill attempted")
        if self.port_pids(port):
            fail(code + "_port_busy", "stable port did not become free")

    def start(self, launch_spec, cwd, log_path, environment):
        log = open(log_path, "ab", buffering=0)
        try:
            child_environment = scrubbed_environment(environment)
            process = subprocess.Popen(
                launch_spec,
                cwd=cwd,
                env=child_environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            return process.pid
        finally:
            log.close()

    def lane(self, command, cwd):
        self.run(command, cwd, "activation_lane_failed")

    def complete(self, body, token):
        request = urllib.request.Request(
            "http://127.0.0.1:40000/api/projects/aming-claw/ac-stable-promotion/complete",
            data=canonical(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Gov-Token": token},
            method="POST",
        )
        observed = None
        last = None
        for _ in range(2):
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    result = json.load(response)
                exact = bool(
                    result.get("ok") is True
                    and result.get("promoted_commit")
                    == body.get("candidate_commit")
                    and isinstance(result.get("timeline_event_id"), int)
                    and not isinstance(result.get("timeline_event_id"), bool)
                    and int(result.get("timeline_event_id") or 0) > 0
                    and re.fullmatch(
                        r"sha256:[0-9a-f]{64}",
                        str(result.get("promotion_receipt_hash") or ""),
                    )
                )
                if not exact:
                    fail(
                        "activation_completion_uncertain",
                        "completion returned bytes without an exact durable receipt",
                    )
                projected = {
                    key: result.get(key)
                    for key in (
                        "promotion_receipt_hash",
                        "promoted_commit",
                        "timeline_event_id",
                    )
                }
                if observed is None:
                    observed = projected
                    continue
                if projected != observed:
                    fail(
                        "activation_completion_uncertain",
                        "completion receipt readback conflicts with the first response",
                    )
                return result
            except urllib.error.HTTPError as exc:
                if 400 <= int(exc.code) < 500 and observed is None:
                    fail(
                        "activation_completion_durable_gate_rejected",
                        "completion durable gate deterministically rejected the request",
                    )
                last = exc
            except PromotionFailure:
                raise
            except (TimeoutError, urllib.error.URLError, OSError, ValueError) as exc:
                last = exc
        fail(
            "activation_completion_uncertain",
            "completion transport failed after request bytes may have been sent",
        )

    def reproject(self, plan, script_path):
        directory = Path(plan["journal_path"]).parent
        manifest_fd, manifest_raw = tempfile.mkstemp(
            prefix="ac-promotion-manifest-recheck-", suffix=".json", dir=directory
        )
        os.close(manifest_fd)
        manifest_path = Path(manifest_raw)
        output_path = manifest_path.with_suffix(".plan-never-written")
        try:
            manifest_path.write_text(
                canonical(plan["manifest"]) + "\n", encoding="utf-8"
            )
            manifest_path.chmod(0o600)
            environment = scrubbed_environment()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["SHARED_VOLUME_PATH"] = str(
                Path(plan["stable_worktree"]) / "shared-volume"
            )
            result = subprocess.run(
                [
                    script_path,
                    "--prepare",
                    "--promotion-manifest",
                    str(manifest_path),
                    "--activation-plan",
                    str(output_path),
                    "--dry-run",
                ],
                cwd=plan["dev_worktree"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                fail(
                    "activation_durable_reprojection_failed",
                    "durable v2 authority did not reproject immediately before mutation",
                )
            try:
                projected = json.loads(result.stdout.strip().splitlines()[-1])
            except (IndexError, TypeError, ValueError, json.JSONDecodeError):
                fail(
                    "activation_durable_reprojection_invalid",
                    "durable v2 reprojector returned a malformed receipt",
                )
            return projected.get("precheck_receipt")
        finally:
            if manifest_path.exists():
                manifest_path.unlink()
            if output_path.exists():
                output_path.unlink()

    def graph_hash(self, database):
        import sqlite3
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT project_id,snapshot_id,commit_sha,is_active FROM graph_snapshots WHERE project_id='aming-claw' ORDER BY snapshot_id"
            ).fetchall()
            return sha([list(row) for row in rows])
        finally:
            conn.close()


class ActivationMachine:
    def __init__(self, plan, patch, ops, journal, token, script_path=""):
        self.plan = plan
        self.patch = patch
        self.ops = ops
        self.journal = journal
        self.token = token
        self.script_path = script_path
        self.mutated = False
        self.mutation_intent = False
        self.completion_may_be_durable = False
        self.candidate_pid = 0
        self.candidate_identity = {}

    def expected_process_command(self, launch_spec):
        return " ".join(
            [self.plan["runtime_process_executable"], *launch_spec[1:]]
        )

    def exact_spawned_process(self, pid, launch_spec, code):
        deadline = time.monotonic() + 5.0
        expected_command = self.expected_process_command(launch_spec)
        first_identity = {}
        while True:
            identity = self.ops.pid_identity(pid)
            if (
                identity.get("pid") == pid
                and str(identity.get("birth") or "").strip()
                and identity.get("command") == expected_command
            ):
                if first_identity and identity != first_identity:
                    fail(code, "spawned process PID/birth/argv changed")
                return identity
            if not self.ops.pid_alive(pid) or time.monotonic() >= deadline:
                fail(code, "spawned process lacks exact PID/birth/argv")
            time.sleep(0.05)

    def journal_process_identity(self, *states):
        for row in reversed(self.journal.rows):
            if row.get("state") not in states:
                continue
            identity = (row.get("evidence") or {}).get("process_identity")
            if (
                isinstance(identity, dict)
                and int(identity.get("pid") or 0) > 0
                and str(identity.get("birth") or "").strip()
                and str(identity.get("command") or "").strip()
            ):
                return dict(identity)
        return {}

    def exact_started_process(self, pid, launch_spec, code, *, poll=True):
        deadline = time.monotonic() + (15.0 if poll else 0.0)
        expected_command = self.expected_process_command(launch_spec)
        first_identity = {}
        while True:
            identity = self.ops.pid_identity(pid)
            if (
                not first_identity
                and identity.get("pid") == pid
                and str(identity.get("birth") or "").strip()
                and str(identity.get("command") or "").strip()
            ):
                first_identity = dict(identity)
            if first_identity and identity != first_identity:
                fail(code, "started process PID/birth/argv changed while polling")
            if (
                identity.get("pid") == pid
                and str(identity.get("birth") or "").strip()
                and identity.get("command") == expected_command
                and self.ops.port_pids(self.plan["stable_port"]) == [pid]
            ):
                return identity
            if not poll or time.monotonic() >= deadline:
                fail(code, "started process did not bind with exact PID/birth/argv")
            if not self.ops.pid_alive(pid):
                fail(code, "started process exited before binding")
            time.sleep(0.1)

    def exact_database(self):
        stable = Path(self.plan["stable_worktree"])
        relative = Path(self.plan["stable_database_relative_path"])
        database = stable / relative
        if str(database) != self.plan["stable_database_path"]:
            fail("activation_database_path_drift", "stable database path is not canonical")
        current = stable
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                fail("activation_database_path_drift", "stable database parent is a symlink")
        try:
            metadata = database.stat(follow_symlinks=False)
        except OSError:
            fail("activation_database_identity_drift", "stable database is unavailable")
        expected = self.plan["stable_database_identity"]
        if not (
            stat.S_ISREG(metadata.st_mode)
            and database.resolve(strict=True) == database
            and (int(metadata.st_dev), int(metadata.st_ino))
            == (int(expected["device"]), int(expected["inode"]))
            and self.plan["stable_database_path_sha256"]
            == sha(str(relative).encode("utf-8"))
            == expected["stable_relative_path_sha256"]
        ):
            fail("activation_database_identity_drift", "stable database identity changed")
        return database

    def exact_stable_worktree(self):
        raw = self.ops.git(
            self.plan["stable_worktree"], "worktree", "list", "--porcelain"
        )
        expected_branch = "refs/heads/" + self.plan["stable_branch"]
        matches = []
        for block in raw.strip().split("\n\n"):
            values = dict(
                line.split(" ", 1) if " " in line else (line, "")
                for line in block.splitlines()
            )
            if values.get("branch") == expected_branch:
                matches.append(values.get("worktree", ""))
        if matches != [self.plan["stable_worktree"]]:
            fail(
                "activation_stable_worktree_ambiguous",
                "exactly one canonical stable branch worktree is required",
            )

    def exact_health(self, expected_commit, expected_pid=0, *, legacy=False):
        health = self.ops.health(self.plan["stable_port"])
        identity = health.get("runtime_plane_identity") or {}
        loaded = health.get("loaded_runtime_identity") or {}
        expected_source = (
            self.plan["stable_runtime_source_sha256"]
            if legacy
            else self.plan["candidate_runtime_source_sha256"]
        )
        common = bool(
            health.get("status") == "ok"
            and health.get("service") == "governance"
            and health.get("port") == self.plan["stable_port"]
            and health.get("runtime_loaded_version") == expected_commit
            and health.get("runtime_loaded_source_sha256") == expected_source
            and loaded.get("loaded_source_sha256") == expected_source
            and loaded.get("worktree_source_sha256") == expected_source
            and health.get("runtime_stale") is False
            and (not expected_pid or int(health.get("pid") or identity.get("pid") or 0) == expected_pid)
        )
        plane = bool(
            legacy
            and not identity
            or (
                identity.get("plane") == "stable"
                and identity.get("branch") == self.plan["stable_branch"]
                and identity.get("commit") == expected_commit
                and identity.get("stable_anchor_commit") == expected_commit
                and identity.get("stable_database_identity")
                == self.plan["stable_database_identity"]
            )
        )
        if not (common and plane):
            fail("activation_health_identity_mismatch", "stable health is not exact expected identity")
        self.exact_database()
        return health

    def poll_health(self, expected_commit, expected_pid, *, legacy=False):
        deadline = time.monotonic() + 15.0
        first = self.ops.pid_identity(expected_pid)
        while True:
            if self.ops.pid_identity(expected_pid) != first:
                fail(
                    "activation_process_identity_drift",
                    "PID/birth/argv changed while waiting for health",
                )
            try:
                return self.exact_health(
                    expected_commit, expected_pid, legacy=legacy
                )
            except PromotionFailure as exc:
                if time.monotonic() >= deadline:
                    raise exc
            time.sleep(0.1)

    def completion_body(self, health, previous_entry_hash):
        body = dict(self.plan["completion_body_template"])
        body["activation_plan_hash"] = self.plan["plan_hash"]
        body["activation_journal_previous_entry_hash"] = previous_entry_hash
        body["health_identity"] = {
            "runtime_loaded_version": health["runtime_loaded_version"],
            "runtime_plane_identity": health["runtime_plane_identity"],
            "runtime_stale": health["runtime_stale"],
        }
        return body

    def complete_candidate(self, health, *, resumed=False):
        if self.journal.rows and self.journal.rows[-1].get("state") == "COMPLETION_COMMITTED":
            committed = dict(self.journal.rows[-1].get("evidence") or {})
            completion = committed.get("completion")
            if not isinstance(completion, dict):
                fail(
                    "activation_completion_journal_uncertain",
                    "committed completion journal lacks the exact receipt",
                )
            row = self.journal.append(
                "COMPLETED", {"completion": completion, "resumed": True}
            )
            return {
                "ok": True,
                "idempotent": True,
                "completion": completion,
                "journal_entry_hash": row["entry_hash"],
            }
        prior_submission = bool(
            self.journal.rows
            and self.journal.rows[-1].get("state") == "COMPLETION_SUBMITTING"
        )
        if prior_submission:
            submitting = self.journal.rows[-1]
        else:
            submitting = self.journal.append(
                "COMPLETION_SUBMITTING",
                {"candidate_commit": self.plan["candidate_commit"]},
            )
        body = self.completion_body(health, submitting["entry_hash"])
        self.completion_may_be_durable = True
        try:
            completion = self.ops.complete(body, self.token)
        except PromotionFailure as exc:
            if (
                exc.code == "activation_completion_durable_gate_rejected"
                and not prior_submission
            ):
                self.completion_may_be_durable = False
            raise
        if not (
            isinstance(completion, dict)
            and completion.get("ok") is True
            and completion.get("promoted_commit")
            == self.plan["candidate_commit"]
            and isinstance(completion.get("timeline_event_id"), int)
            and not isinstance(completion.get("timeline_event_id"), bool)
            and int(completion.get("timeline_event_id") or 0) > 0
            and re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(completion.get("promotion_receipt_hash") or ""),
            )
        ):
            fail(
                "activation_completion_uncertain",
                "completion response lacks the exact durable receipt identity",
            )
        if not (
            self.candidate_pid > 0
            and self.ops.pid_identity(self.candidate_pid)
            == self.candidate_identity
            and self.ops.port_pids(self.plan["stable_port"])
            == [self.candidate_pid]
        ):
            fail(
                "activation_completion_process_identity_changed",
                "candidate PID/birth/argv/listener changed during completion",
            )
        self.exact_health(
            self.plan["candidate_commit"], self.candidate_pid
        )
        if (
            self.ops.graph_hash(self.plan["stable_database_path"])
            != self.plan["graph_identity_hash"]
        ):
            fail(
                "activation_completion_graph_identity_changed",
                "candidate graph identity changed during completion",
            )
        try:
            self.journal.append(
                "COMPLETION_COMMITTED",
                {
                    "completion": completion,
                    "request_previous_entry_hash": submitting["entry_hash"],
                },
            )
            row = self.journal.append(
                "COMPLETED", {"completion": completion, "resumed": resumed}
            )
        except PromotionFailure:
            fail(
                "activation_completion_journal_uncertain",
                "completion is durable but its terminal journal append failed",
            )
        return {
            "ok": True,
            "idempotent": resumed,
            "completion": completion,
            "journal_entry_hash": row["entry_hash"],
        }

    def validate_signoff_liveness(self):
        try:
            expires = dt.datetime.fromisoformat(
                str(self.plan["manifest"]["gates"]["operator_signoff"]["expires_at"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            fail("activation_plan_signoff_invalid", "activation plan operator signoff is malformed")
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.timezone.utc)
        if dt.datetime.now(dt.timezone.utc) >= expires:
            fail("activation_plan_signoff_expired", "activation plan operator signoff expired")

    def resume_exact_promoted_candidate(self):
        if not self.journal.rows or self.journal.rows[-1].get("state") not in {
            "CANDIDATE_HEALTHY",
            "COMPLETION_SUBMITTING",
            "COMPLETION_COMMITTED",
        }:
            fail("activation_already_promoted_ambiguous", "candidate HEAD lacks the exact resumable journal state")
        if self.journal.rows[-1].get("state") == "COMPLETION_COMMITTED":
            return self.complete_candidate({}, resumed=True)
        journal_identity = self.journal_process_identity(
            "CANDIDATE_SPAWNED", "CANDIDATE_HEALTHY"
        )
        if not journal_identity:
            fail(
                "activation_already_promoted_process_identity_mismatch",
                "candidate journal lacks exact PID/birth/argv custody",
            )
        self.candidate_pid = int(journal_identity["pid"])
        self.candidate_identity = journal_identity
        listeners = self.ops.port_pids(self.plan["stable_port"])
        if listeners != [self.candidate_pid]:
            fail("activation_already_promoted_listener_ambiguous", "candidate listener is absent or ambiguous")
        self.candidate_identity = self.exact_started_process(
            self.candidate_pid,
            self.plan["candidate_launch_spec"],
            "activation_already_promoted_process_identity_mismatch",
        )
        healthy_rows = [
            row for row in self.journal.rows
            if row.get("state") == "CANDIDATE_HEALTHY"
        ]
        journal_identity = (
            (healthy_rows[-1].get("evidence") or {}).get("process_identity")
            if healthy_rows
            else None
        )
        if journal_identity != self.candidate_identity:
            fail("activation_already_promoted_process_identity_mismatch", "candidate process differs from journal authority")
        health = self.poll_health(self.plan["candidate_commit"], self.candidate_pid)
        if not (
            self.ops.git(self.plan["stable_worktree"], "branch", "--show-current") == self.plan["stable_branch"]
            and self.ops.git(self.plan["stable_worktree"], "rev-parse", "HEAD^{tree}") == self.plan["candidate_tree_sha"]
            and self.ops.git(self.plan["stable_worktree"], "status", "--porcelain") == ""
            and self.ops.graph_hash(self.plan["stable_database_path"]) == self.plan["graph_identity_hash"]
        ):
            fail("activation_already_promoted_identity_mismatch", "resumable candidate identity changed")
        return self.complete_candidate(health, resumed=True)

    def validate_pre_mutation(self, *, run_lanes=True):
        stable = self.plan["stable_worktree"]
        dev = self.plan["dev_worktree"]
        self.exact_stable_worktree()
        if self.ops.git(stable, "rev-parse", "HEAD") != self.plan["stable_anchor_commit"]:
            fail("activation_cas_anchor_drift", "stable HEAD changed before mutation")
        if self.ops.git(stable, "branch", "--show-current") != self.plan["stable_branch"]:
            fail("activation_cas_branch_drift", "stable branch changed before mutation")
        if self.ops.git(stable, "status", "--porcelain"):
            fail("activation_cas_dirty_stable", "stable worktree is dirty")
        if self.ops.git(dev, "rev-parse", "HEAD") != self.plan["candidate_commit"]:
            fail("activation_candidate_drift", "dev candidate changed")
        if self.ops.git(dev, "branch", "--show-current") != self.plan["dev_branch"] or self.ops.git(dev, "status", "--porcelain"):
            fail("activation_candidate_identity_drift", "dev branch/worktree changed")
        if self.ops.pid_identity(self.plan["old_process"]["pid"]) != self.plan["old_process"]:
            fail("activation_old_pid_identity_drift", "old stable PID birth/command changed")
        if self.ops.port_pids(self.plan["stable_port"]) != [self.plan["old_process"]["pid"]]:
            fail("activation_old_listener_drift", "old stable listener changed")
        self.exact_health(
            self.plan["stable_anchor_commit"],
            self.plan["old_process"]["pid"],
            legacy=True,
        )
        if self.ops.git(dev, "rev-parse", f"{self.plan['candidate_commit']}^{{tree}}") != self.plan["candidate_tree_sha"]:
            fail("activation_candidate_tree_drift", "candidate tree changed")
        self.exact_database()
        if self.ops.graph_hash(self.plan["stable_database_path"]) != self.plan["graph_identity_hash"]:
            fail("activation_graph_identity_drift", "active graph changed before mutation")
        if run_lanes:
            for command in self.plan["lane_commands"]:
                self.ops.lane(command, dev)

    def validate_durable_reprojection(self):
        projected = self.ops.reproject(self.plan, self.script_path)
        if projected != self.plan["precheck_receipt"]:
            fail(
                "activation_durable_authority_changed",
                "durable QA/rollback/signoff authority changed before mutation",
            )

    def validate_non_process_after_stop(self):
        stable = self.plan["stable_worktree"]
        dev = self.plan["dev_worktree"]
        if not (
            self.ops.git(stable, "rev-parse", "HEAD")
            == self.plan["stable_anchor_commit"]
            and self.ops.git(stable, "branch", "--show-current")
            == self.plan["stable_branch"]
            and self.ops.git(stable, "status", "--porcelain") == ""
            and self.ops.git(dev, "rev-parse", "HEAD")
            == self.plan["candidate_commit"]
            and self.ops.git(dev, "branch", "--show-current")
            == self.plan["dev_branch"]
            and self.ops.git(dev, "status", "--porcelain") == ""
            and self.ops.git(
                dev, "rev-parse", f"{self.plan['candidate_commit']}^{{tree}}"
            )
            == self.plan["candidate_tree_sha"]
        ):
            fail(
                "activation_post_stop_source_drift",
                "source authority changed after old process stop",
            )
        self.exact_database()
        if (
            self.ops.graph_hash(self.plan["stable_database_path"])
            != self.plan["graph_identity_hash"]
        ):
            fail(
                "activation_post_stop_graph_drift",
                "graph identity changed after old process stop",
            )

    def rollback(self, cause):
        stable = self.plan["stable_worktree"]
        candidate = self.plan["candidate_commit"]
        anchor = self.plan["stable_anchor_commit"]
        try:
            head = self.ops.git(stable, "rev-parse", "HEAD")
            listeners = self.ops.port_pids(self.plan["stable_port"])
            restored_old_identity = self.journal_process_identity(
                "ROLLBACK_OLD_SPAWNED", "ROLLBACK_OLD_STARTED"
            )
            if self.candidate_pid and self.ops.pid_alive(self.candidate_pid):
                live_identity = self.ops.pid_identity(self.candidate_pid)
                expected_identity = self.candidate_identity or live_identity
                if (
                    live_identity != expected_identity
                    or live_identity.get("command")
                    != self.expected_process_command(
                        self.plan["candidate_launch_spec"]
                    )
                ):
                    fail(
                        "rollback_candidate_process_identity_drift",
                        "owned candidate PID identity changed",
                    )
                self.journal.append(
                    "ROLLBACK_STARTED",
                    {"cause": cause, "candidate_pid": self.candidate_pid},
                )
                self.ops.stop(
                    live_identity,
                    self.plan["stable_port"],
                    "rollback_candidate_stop_failed",
                    require_listener=False,
                )
                self.journal.append("ROLLBACK_CANDIDATE_STOPPED")
                listeners = self.ops.port_pids(self.plan["stable_port"])
            if head == anchor and not self.ops.git(stable, "status", "--porcelain") and listeners:
                expected_old_identity = (
                    restored_old_identity
                    if restored_old_identity
                    else self.plan["old_process"]
                )
                if listeners != [expected_old_identity["pid"]]:
                    fail("rollback_listener_ambiguous", "unexpected anchor listener")
                if (
                    self.ops.git(stable, "branch", "--show-current") != self.plan["stable_branch"]
                ):
                    fail("rollback_anchor_source_ambiguous", "anchor source is not exact and clean")
                if (
                    self.ops.pid_identity(expected_old_identity["pid"])
                    != expected_old_identity
                    or expected_old_identity.get("command")
                    != self.expected_process_command(self.plan["old_launch_spec"])
                ):
                    fail(
                        "rollback_old_process_identity_drift",
                        "restored old process identity is not exact",
                    )
                self.exact_health(anchor, listeners[0], legacy=True)
                row = self.journal.append(
                    "ROLLED_BACK",
                    {"cause": cause, "restored_commit": anchor, "already_restored": True},
                )
                return {"ok": False, "rolled_back": True, "idempotent": True, "journal_entry_hash": row["entry_hash"]}
            if listeners:
                fail("rollback_listener_ambiguous", "unexpected stable listener blocks rollback")
            if restored_old_identity and self.ops.pid_alive(
                restored_old_identity["pid"]
            ):
                if (
                    self.ops.pid_identity(restored_old_identity["pid"])
                    != restored_old_identity
                    or restored_old_identity.get("command")
                    != self.expected_process_command(self.plan["old_launch_spec"])
                ):
                    fail(
                        "rollback_old_process_identity_drift",
                        "journal-owned old process identity changed",
                    )
                old_identity = self.exact_started_process(
                    restored_old_identity["pid"],
                    self.plan["old_launch_spec"],
                    "rollback_old_process_identity_failed",
                )
                self.poll_health(anchor, old_identity["pid"], legacy=True)
                if (
                    self.ops.graph_hash(self.plan["stable_database_path"])
                    != self.plan["graph_identity_hash"]
                ):
                    fail(
                        "rollback_graph_identity_failed",
                        "rollback changed active graph identity",
                    )
                row = self.journal.append(
                    "ROLLED_BACK",
                    {
                        "cause": cause,
                        "restored_commit": anchor,
                        "old_process_recovered": True,
                    },
                )
                return {
                    "ok": False,
                    "rolled_back": True,
                    "idempotent": True,
                    "journal_entry_hash": row["entry_hash"],
                }
            old_pid = int(self.plan["old_process"]["pid"])
            if self.ops.pid_alive(old_pid):
                if self.ops.pid_identity(old_pid) != self.plan["old_process"]:
                    fail(
                        "rollback_old_process_identity_drift",
                        "old PID was reused while recovering stop failure",
                    )
                deadline = time.monotonic() + 10.0
                while self.ops.pid_alive(old_pid) and time.monotonic() < deadline:
                    if self.ops.port_pids(self.plan["stable_port"]) == [old_pid]:
                        self.exact_health(anchor, old_pid, legacy=True)
                        row = self.journal.append(
                            "ROLLED_BACK",
                            {
                                "cause": cause,
                                "restored_commit": anchor,
                                "old_process_recovered": True,
                            },
                        )
                        return {
                            "ok": False,
                            "rolled_back": True,
                            "idempotent": True,
                            "journal_entry_hash": row["entry_hash"],
                        }
                    time.sleep(0.1)
                if self.ops.pid_alive(old_pid):
                    fail(
                        "rollback_old_process_stop_ambiguous",
                        "old process neither rebound nor exited",
                    )
            status = self.ops.git(stable, "status", "--porcelain")
            if head == candidate:
                if status:
                    fail("rollback_candidate_source_ambiguous", "candidate source is dirty")
                self.journal.append("ROLLBACK_REF_RESTORE_INTENT")
                self.ops.run(
                    ["git", "update-ref", f"refs/heads/{self.plan['stable_branch']}", anchor, candidate],
                    stable,
                    "rollback_ref_cas_failed",
                )
                self.journal.append("ROLLBACK_REF_RESTORED")
                head = anchor
                status = self.ops.git(stable, "status", "--porcelain")
            if head == anchor and status:
                staged = self.ops.run(
                    [
                        "git", "diff", "--cached", "--no-ext-diff", "--no-textconv",
                        "--binary", "--full-index", "-M", "--", ".",
                    ],
                    stable,
                    "rollback_preimage_probe_failed",
                )
                unstaged = self.ops.run(
                    [
                        "git", "diff", "--no-ext-diff", "--no-textconv",
                        "--binary", "--full-index", "-M", "--", ".",
                    ],
                    stable,
                    "rollback_preimage_probe_failed",
                )
                if sha(staged) != self.plan["forward_patch_sha256"] or unstaged:
                    fail(
                        "rollback_preimage_ambiguous",
                        "anchor dirty state is not the exact candidate preimage",
                    )
                with tempfile.NamedTemporaryFile(prefix="ac-promotion-reverse-", suffix=".patch", delete=False) as patch_file:
                    patch_file.write(self.patch)
                    patch_file.flush()
                    os.fsync(patch_file.fileno())
                    patch_path = patch_file.name
                try:
                    self.ops.run(["git", "apply", "-R", "--index", patch_path], stable, "rollback_reverse_patch_failed")
                finally:
                    os.unlink(patch_path)
                self.journal.append("ROLLBACK_PATCH_REVERSED")
            elif head != anchor:
                fail("rollback_ref_ambiguous", "stable ref is neither exact candidate nor anchor")
            if self.ops.git(stable, "rev-parse", "HEAD") != anchor or self.ops.git(stable, "status", "--porcelain"):
                fail("rollback_anchor_verification_failed", "stable source did not return cleanly to anchor")
            self.exact_database()
            self.candidate_pid = self.ops.start(
                self.plan["old_launch_spec"],
                stable,
                str(Path(self.plan["journal_path"]).with_suffix(".old-runtime.log")),
                self.plan["old_launch_environment"],
            )
            self.candidate_identity = self.exact_spawned_process(
                self.candidate_pid,
                self.plan["old_launch_spec"],
                "rollback_old_process_spawn_identity_failed",
            )
            self.journal.append(
                "ROLLBACK_OLD_SPAWNED",
                {"process_identity": self.candidate_identity},
            )
            old_identity = self.exact_started_process(
                self.candidate_pid,
                self.plan["old_launch_spec"],
                "rollback_old_process_identity_failed",
            )
            self.journal.append(
                "ROLLBACK_OLD_STARTED", {"process_identity": old_identity}
            )
            self.poll_health(anchor, self.candidate_pid, legacy=True)
            if self.ops.graph_hash(self.plan["stable_database_path"]) != self.plan["graph_identity_hash"]:
                fail("rollback_graph_identity_failed", "rollback changed active graph identity")
            row = self.journal.append(
                "ROLLED_BACK",
                {"cause": cause, "restored_commit": anchor, "old_process": old_identity},
            )
            return {"ok": False, "rolled_back": True, "journal_entry_hash": row["entry_hash"]}
        except PromotionFailure as exc:
            self.journal.append(
                "ROLLBACK_FATAL", {"cause": cause, "failure_code": exc.code}
            )
            raise

    def activate(self, dry_run=False):
        terminals = [row for row in self.journal.rows if row.get("state") in {"COMPLETED", "ROLLED_BACK"}]
        if terminals:
            return {"ok": terminals[0]["state"] == "COMPLETED", "idempotent": True, "terminal": terminals[0]}
        current_head = self.ops.git(self.plan["stable_worktree"], "rev-parse", "HEAD")
        if current_head == self.plan["candidate_commit"]:
            last_state = self.journal.rows[-1].get("state") if self.journal.rows else ""
            if last_state not in {
                "CANDIDATE_HEALTHY",
                "COMPLETION_SUBMITTING",
                "COMPLETION_COMMITTED",
            }:
                fail(
                    "activation_already_promoted_ambiguous",
                    "candidate HEAD lacks the exact resumable journal state",
                )
            self.mutated = True
            self.mutation_intent = True
            try:
                return self.resume_exact_promoted_candidate()
            except PromotionFailure as exc:
                if self.completion_may_be_durable or last_state in {
                    "COMPLETION_SUBMITTING",
                    "COMPLETION_COMMITTED",
                }:
                    try:
                        self.journal.append(
                            "COMPLETION_AMBIGUOUS", {"cause": exc.code}
                        )
                    except PromotionFailure:
                        pass
                    raise
                return self.rollback(exc.code)
        if current_head != self.plan["stable_anchor_commit"]:
            fail("activation_cas_anchor_drift", "stable HEAD is neither exact anchor nor resumable candidate")
        self.validate_signoff_liveness()
        self.validate_pre_mutation(run_lanes=not dry_run)
        if dry_run:
            return {"ok": True, "dry_run": True, "writes_performed": False}
        self.validate_durable_reprojection()
        self.journal.append(
            "MUTATION_INTENT",
            {
                "stable_anchor_commit": self.plan["stable_anchor_commit"],
                "candidate_commit": self.plan["candidate_commit"],
                "precheck_receipt_hash": self.plan["precheck_receipt_hash"],
            },
        )
        self.mutation_intent = True
        try:
            # This full source/process/DB/graph CAS is intentionally the final
            # fallible operation before SIGTERM. Lanes and durable gate
            # projection already completed before the mutation intent.
            self.validate_pre_mutation(run_lanes=False)
            self.mutated = True
            self.ops.stop(self.plan["old_process"], self.plan["stable_port"], "activation_old_stop_failed")
            self.journal.append("OLD_STOPPED")
            self.validate_non_process_after_stop()
            self.ops.run(
                ["git", "merge", "--ff-only", self.plan["candidate_commit"]],
                self.plan["stable_worktree"],
                "activation_ff_failed",
            )
            self.journal.append("SOURCE_ADVANCED", {"candidate_commit": self.plan["candidate_commit"]})
            self.candidate_pid = self.ops.start(
                self.plan["candidate_launch_spec"],
                self.plan["stable_worktree"],
                str(Path(self.plan["journal_path"]).with_suffix(".candidate-runtime.log")),
                self.plan["candidate_launch_environment"],
            )
            self.candidate_identity = self.exact_spawned_process(
                self.candidate_pid,
                self.plan["candidate_launch_spec"],
                "activation_candidate_spawn_identity_mismatch",
            )
            self.journal.append(
                "CANDIDATE_SPAWNED",
                {"process_identity": self.candidate_identity},
            )
            self.candidate_identity = self.exact_started_process(
                self.candidate_pid,
                self.plan["candidate_launch_spec"],
                "activation_candidate_process_identity_mismatch",
            )
            health = self.poll_health(
                self.plan["candidate_commit"], self.candidate_pid
            )
            if not (
                self.ops.git(self.plan["stable_worktree"], "rev-parse", "HEAD")
                == self.plan["candidate_commit"]
                and self.ops.git(self.plan["stable_worktree"], "rev-parse", "HEAD^{tree}")
                == self.plan["candidate_tree_sha"]
                and self.ops.git(self.plan["stable_worktree"], "status", "--porcelain") == ""
            ):
                fail("activation_candidate_source_identity_mismatch", "activated source is not exact clean candidate")
            for command in self.plan["lane_commands"]:
                self.ops.lane(command, self.plan["stable_worktree"])
            self.journal.append(
                "CANDIDATE_HEALTHY",
                {"pid": self.candidate_pid, "process_identity": self.candidate_identity},
            )
            if self.ops.graph_hash(self.plan["stable_database_path"]) != self.plan["graph_identity_hash"]:
                fail("activation_graph_changed", "candidate activation changed graph identity")
            return self.complete_candidate(health)
        except PromotionFailure as exc:
            if self.completion_may_be_durable:
                try:
                    self.journal.append(
                        "COMPLETION_AMBIGUOUS", {"cause": exc.code}
                    )
                except PromotionFailure:
                    pass
                raise
            if self.mutation_intent:
                return self.rollback(exc.code)
            raise

    def recover(self, dry_run=False):
        terminals = [row for row in self.journal.rows if row.get("state") in {"COMPLETED", "ROLLED_BACK"}]
        if terminals:
            terminal = terminals[0]
            return {
                "ok": terminal["state"] == "COMPLETED",
                "rolled_back": terminal["state"] == "ROLLED_BACK",
                "idempotent": True,
                "terminal": terminal,
                "writes_performed": False,
            }
        if dry_run:
            return {"ok": True, "dry_run": True, "writes_performed": False}
        last_state = self.journal.rows[-1].get("state") if self.journal.rows else ""
        if last_state == "COMPLETION_COMMITTED":
            return self.complete_candidate({}, resumed=True)
        if last_state == "COMPLETION_SUBMITTING":
            self.mutated = True
            self.mutation_intent = True
            self.completion_may_be_durable = True
            try:
                return self.resume_exact_promoted_candidate()
            except PromotionFailure as exc:
                try:
                    self.journal.append(
                        "COMPLETION_AMBIGUOUS", {"cause": exc.code}
                    )
                except PromotionFailure:
                    pass
                raise
        candidate_identity = self.journal_process_identity(
            "CANDIDATE_SPAWNED", "CANDIDATE_HEALTHY"
        )
        if candidate_identity:
            self.candidate_pid = int(candidate_identity["pid"])
            self.candidate_identity = candidate_identity
        return self.rollback("operator_recover")

    def interrupt(self, signum):
        cause = f"operator_signal_{signal.Signals(signum).name.lower()}"
        if self.mutation_intent and not self.completion_may_be_durable:
            self.rollback(cause)
        raise SystemExit(128 + signum)


def main():
    mode, plan_path, dry_raw, script_path = sys.argv[1:]
    plan, raw = safe_json_file(plan_path)
    patch = validate_plan(plan, raw, script_path)
    if dry_raw == "true":
        os.environ["GIT_OPTIONAL_LOCKS"] = "0"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        journal = Journal(plan["journal_path"], plan["plan_hash"])
        machine = ActivationMachine(
            plan, patch, RealOps(), journal, "", script_path
        )
        result = (
            machine.activate(dry_run=True)
            if mode == "activate"
            else machine.recover(dry_run=True)
        )
        print(canonical(result))
        return
    lock_path = Path(plan["lock_path"])
    if lock_path.is_symlink():
        fail("activation_lock_identity_invalid", "activation lock cannot be a symlink")
    lock_fd = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode) or stat.S_IMODE(lock_stat.st_mode) != 0o600:
            fail("activation_lock_identity_invalid", "activation lock must be a regular mode-0600 file")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("activation_lock_busy", "another exact activation owns the lock")
        journal = Journal(plan["journal_path"], plan["plan_hash"])
        machine = ActivationMachine(
            plan,
            patch,
            RealOps(),
            journal,
            os.environ.get("GOV_COORDINATOR_TOKEN", ""),
            script_path,
        )
        def handle_signal(signum, _frame):
            machine.interrupt(signum)
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        if mode == "activate":
            result = machine.activate(dry_run=dry_raw == "true")
        else:
            result = machine.recover(dry_run=dry_raw == "true")
        print(canonical(result))
    except PromotionFailure as exc:
        print(f"Promotion blocked [{exc.code}]: {exc}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    main()
PY
    exit $?
fi

if [ -z "$MANIFEST" ] || [ ! -f "$MANIFEST" ]; then
    echo "Promotion blocked: --promotion-manifest must name an existing file." >&2
    exit 2
fi
if [ "$MODE" = "prepare" ]; then
    if [ -z "$ACTIVATION_PLAN" ]; then
        echo "Promotion blocked: --prepare requires --activation-plan." >&2
        exit 2
    fi
elif [ "$MODE" != "legacy" ]; then
    echo "Promotion blocked: unsupported promotion mode." >&2
    exit 2
fi
if [ -z "${SHARED_VOLUME_PATH:-}" ]; then
    echo "Promotion blocked: SHARED_VOLUME_PATH must identify the live AC volume." >&2
    exit 2
fi
if [ "$MODE" = "legacy" ] && [ "$DRY_RUN" != "true" ]; then
    echo "Promotion blocked: legacy one-shot mutation is retired; use --prepare then --activate." >&2
    exit 2
fi
if [ "$MODE" = "legacy" ] && [ -z "${GOV_COORDINATOR_TOKEN:-}" ] && [ "$DRY_RUN" != "true" ]; then
    echo "Promotion blocked: GOV_COORDINATOR_TOKEN is required to record completion." >&2
    exit 2
fi

manifest_value() {
    python3 - "$MANIFEST" "$1" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for part in sys.argv[2].split("."):
    value = value[part]
print("true" if value is True else "false" if value is False else value)
PY
}
manifest_optional_value() {
    python3 - "$MANIFEST" "$1" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for part in sys.argv[2].split("."):
    if not isinstance(value, dict) or part not in value:
        print("")
        raise SystemExit(0)
    value = value[part]
print("true" if value is True else "false" if value is False else value)
PY
}

STABLE_WORKTREE="$(python3 - "$STABLE_BRANCH" <<'PY'
import subprocess, sys
raw = subprocess.check_output(["git", "worktree", "list", "--porcelain"], text=True)
expected = "refs/heads/" + sys.argv[1]
matches = []
for block in raw.strip().split("\n\n"):
    values = dict(
        line.split(" ", 1) if " " in line else (line, "")
        for line in block.splitlines()
    )
    if values.get("branch") == expected:
        matches.append(values.get("worktree", ""))
if len(matches) == 1 and matches[0]:
    print(matches[0])
PY
)"
if [ -z "$STABLE_WORKTREE" ] || [ ! -d "$STABLE_WORKTREE" ]; then
    echo "Promotion blocked: exactly one stable branch worktree is required." >&2; exit 1
fi
CANONICAL_SHARED_VOLUME="$STABLE_WORKTREE/shared-volume"

CANDIDATE_COMMIT="$(manifest_value candidate_commit)"
MANIFEST_ANCHOR="$(manifest_value stable_anchor_commit)"
STABLE_RUNTIME_SOURCE_SHA256="$(manifest_optional_value stable_runtime_source_sha256)"
CANDIDATE_RUNTIME_SOURCE_SHA256="$(manifest_optional_value candidate_runtime_source_sha256)"
CURRENT_STABLE="$(git -C "$STABLE_WORKTREE" rev-parse HEAD)"

if [ "$(git branch --show-current)" != "$DEV_BRANCH" ]; then
    echo "Promotion blocked: only $DEV_BRANCH may be promoted." >&2; exit 1
fi
if [ "$(git rev-parse HEAD)" != "$CANDIDATE_COMMIT" ]; then
    echo "Promotion blocked: candidate_commit must equal the clean dev branch tip." >&2; exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "Promotion blocked: AC dev candidate worktree must be clean." >&2; exit 1
fi
if [ "$(git -C "$STABLE_WORKTREE" branch --show-current)" != "$STABLE_BRANCH" ]; then
    echo "Promotion blocked: stable worktree branch identity mismatch." >&2; exit 1
fi
if [ -n "$(git -C "$STABLE_WORKTREE" status --porcelain)" ]; then
    echo "Promotion blocked: stable branch worktree must be clean." >&2; exit 1
fi
if [ "$MANIFEST_ANCHOR" != "$CURRENT_STABLE" ]; then
    echo "Promotion blocked: manifest anchor is stale relative to current stable HEAD." >&2; exit 1
fi
if ! git merge-base --is-ancestor "$CURRENT_STABLE" "$CANDIDATE_COMMIT"; then
    echo "Promotion blocked: candidate is not a linear descendant of current stable." >&2; exit 1
fi

DIFF_SHA256="sha256:$(git diff --no-ext-diff --no-textconv --binary --full-index -M "$CURRENT_STABLE..$CANDIDATE_COMMIT" -- . | shasum -a 256 | awk '{print $1}')"
VERIFIER_SHA256="sha256:$(shasum -a 256 "$0" | awk '{print $1}')"
LIVE_DB="$(python3 - "$STABLE_WORKTREE" "${SHARED_VOLUME_PATH}" <<'PY'
from pathlib import Path
import sys
stable = Path(sys.argv[1]).expanduser().resolve(strict=True)
requested_input = Path(sys.argv[2]).expanduser().absolute()
if requested_input.is_symlink():
    raise SystemExit("Promotion blocked: alternate SHARED_VOLUME_PATH is forbidden")
requested = requested_input.resolve(strict=True)
root = (stable / "shared-volume").absolute()
if root.is_symlink() or root.resolve(strict=True) != root or requested != root:
    raise SystemExit("Promotion blocked: alternate SHARED_VOLUME_PATH is forbidden")
path = root / "codex-tasks" / "state" / "governance" / "aming-claw" / "governance.db"
cursor = root
for part in Path("codex-tasks/state/governance/aming-claw/governance.db").parts:
    cursor = cursor / part
    if cursor.is_symlink():
        raise SystemExit("Promotion blocked: live AC database path cannot contain a symlink")
resolved = path.resolve(strict=True)
if resolved != path.absolute() or not path.is_file():
    raise SystemExit("Promotion blocked: live AC database escaped its canonical path")
print(path.absolute())
PY
)"
STABLE_DATABASE_IDENTITY="$(python3 - "$LIVE_DB" <<'PY'
import hashlib, json, os, stat, sys
from pathlib import Path
path = Path(sys.argv[1]).absolute()
metadata = path.stat(follow_symlinks=False)
if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or path.resolve(strict=True) != path:
    raise SystemExit("Promotion blocked: canonical AC database identity is invalid")
relative = "shared-volume/codex-tasks/state/governance/aming-claw/governance.db"
print(json.dumps({
    "schema_version": "ac_stable_database_identity.v1",
    "device": int(metadata.st_dev),
    "inode": int(metadata.st_ino),
    "stable_relative_path_sha256": "sha256:" + hashlib.sha256(relative.encode()).hexdigest(),
}, sort_keys=True, separators=(",", ":")))
PY
)"

# Read-only projector: the manifest cannot self-declare PASS. One canonical,
# role-bound QA verdict owns every QA sub-result.  A separate append-only
# stable-plane operator audit event must already exist; this script never
# creates that signoff. No migration or DDL is executed.
promotion_precheck() {
python3 - "$MANIFEST" "$LIVE_DB" "$CURRENT_STABLE" "$CANDIDATE_COMMIT" "$DIFF_SHA256" "$VERIFIER_SHA256" "$BOOTSTRAP_ANCHOR" "$STABLE_DATABASE_IDENTITY" <<'PY'
import datetime as dt, hashlib, json, os, re, sqlite3, stat, subprocess, sys
from pathlib import Path

manifest_path, db_raw, stable, candidate, diff_hash, verifier_hash, bootstrap, database_identity_raw = sys.argv[1:]
manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
database_identity = json.loads(database_identity_raw)

def fail(message): raise SystemExit("Promotion blocked: " + message)
def sha(value):
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
def exact_sha(value): return bool(re.fullmatch(r"sha256:[0-9a-f]{64}", str(value or "")))
def operator_principal(value):
    return bool(
        isinstance(value, str) and value and value == value.strip()
        and len(value) <= 256 and not value.endswith(":route_ref")
    )
def deep_values(value, key):
    if isinstance(value, dict):
        for name, item in value.items():
            if name == key: yield item
            yield from deep_values(item, key)
    elif isinstance(value, list):
        for item in value: yield from deep_values(item, key)
def contains(value, key, expected): return any(item == expected for item in deep_values(value, key))
def safe_fence(value):
    return bool(
        isinstance(value, list) and value and len(set(value)) == len(value)
        and all(
            isinstance(item, str) and item.strip() == item and item
            and not Path(item).is_absolute() and ".." not in Path(item).parts
            for item in value
        )
    )

required_top = {
    "schema_version", "project_id", "backlog_id", "contract_execution_id",
    "stable_anchor_commit", "stable_branch", "branch", "candidate_commit",
    "file_fence", "diff_sha256", "promotion_intent_sha256", "gates", "deploy",
    "prior_promotion", "promotion_manifest_sha256", "stable_database_identity",
}
if manifest.get("schema_version") != "ac_stable_promotion_manifest.v1": fail("schema_version mismatch")
if set(manifest) != required_top: fail("manifest has missing or extra fields")
if manifest.get("project_id") != "aming-claw": fail("project_id must be aming-claw")
if manifest.get("stable_branch") != "codex/direct-no-pass-post-reconcile-r2": fail("stable_branch mismatch")
if manifest.get("branch") != "codex/ac-dev": fail("candidate branch mismatch")
if manifest.get("stable_anchor_commit") != stable: fail("stable anchor mismatch")
if manifest.get("candidate_commit") != candidate: fail("candidate commit mismatch")
if manifest.get("diff_sha256") != diff_hash or not exact_sha(diff_hash): fail("binary diff hash mismatch")
if manifest.get("stable_database_identity") != database_identity:
    fail("stable database identity mismatch")
fence = manifest.get("file_fence")
if not safe_fence(fence): fail("file_fence must be a non-empty exact safe relative list")
actual_files = subprocess.check_output(["git", "diff", "--name-only", stable, candidate], text=True).splitlines()
if set(actual_files) != set(fence) or len(actual_files) != len(fence): fail("actual files differ from file_fence")

intent = {key: manifest[key] for key in (
    "schema_version", "project_id", "backlog_id", "contract_execution_id",
    "stable_anchor_commit", "stable_branch", "branch", "candidate_commit",
    "file_fence", "diff_sha256", "deploy", "stable_database_identity",
)}
intent_hash = sha(intent)
if manifest.get("promotion_intent_sha256") != intent_hash: fail("promotion intent digest mismatch")
deploy = manifest.get("deploy")
if deploy != {"authorized": True, "mode": "host_supervisor", "stable_port": 40000}:
    fail("deploy identity mismatch")
gates = manifest.get("gates")
if not isinstance(gates, dict) or set(gates) != {"qa_verdict", "operator_signoff"}:
    fail("exact qa_verdict and operator_signoff gates required")
qa_gate = gates.get("qa_verdict")
operator_gate = gates.get("operator_signoff")
if not isinstance(qa_gate, dict) or set(qa_gate) != {"timeline_event_id", "status"} or qa_gate.get("status") != "passed":
    fail("qa_verdict gate shape/status mismatch")
if not isinstance(operator_gate, dict) or set(operator_gate) != {"status", "nonce", "operator_principal_id", "expires_at", "queue_event_id"} or operator_gate.get("status") != "approved":
    fail("operator_signoff gate shape/status mismatch")
signable_operator = {
    key: operator_gate.get(key)
    for key in ("status", "nonce", "operator_principal_id", "expires_at")
}
signable_manifest = {
    **intent,
    "promotion_intent_sha256": intent_hash,
    "prior_promotion": manifest.get("prior_promotion"),
    "gates": {
        "qa_verdict": dict(qa_gate),
        "operator_signoff": signable_operator,
    },
}
manifest_hash = sha(signable_manifest)
if manifest.get("promotion_manifest_sha256") != manifest_hash:
    fail("promotion manifest digest mismatch")

db_path = Path(db_raw).absolute()
before = db_path.stat(follow_symlinks=False)
if db_path.is_symlink() or not stat.S_ISREG(before.st_mode) or db_path.resolve(strict=True) != db_path:
    fail("live DB must be the canonical non-symlink AC database")
if (int(before.st_dev), int(before.st_ino)) != (
    int(database_identity["device"]), int(database_identity["inode"])
): fail("live DB physical identity differs from promotion manifest")
conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA query_only=ON")
try:
    opened = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve(strict=True)
    after = db_path.stat(follow_symlinks=False)
    if opened != db_path or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or db_path.is_symlink():
        fail("live DB identity changed during read-only open")
    schema_row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if schema_row is None or int(schema_row[0]) != 47: fail("live DB schema is not exact version 47")
    def event(event_id):
        if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 1:
            fail("every gate requires a positive timeline_event_id")
        row = conn.execute(
            "SELECT * FROM task_timeline_events WHERE id=? AND project_id='aming-claw'", (event_id,)
        ).fetchone()
        if row is None: fail(f"timeline event {event_id} is missing")
        result = dict(row)
        for key in ("payload_json", "verification_json", "artifact_refs_json"):
            try: result[key[:-5]] = json.loads(result.get(key) or "{}")
            except Exception: fail(f"timeline event {event_id} has invalid {key}")
        return result

    qa_row = event(qa_gate.get("timeline_event_id"))
    if qa_row.get("backlog_id") != manifest["backlog_id"]: fail("QA backlog mismatch")
    if qa_row.get("task_id") != manifest["contract_execution_id"]: fail("QA CEX mismatch")
    if qa_row.get("commit_sha") != candidate: fail("QA candidate mismatch")
    if qa_row.get("event_type") != "qa.independent_verification": fail("QA event_type mismatch")
    if qa_row.get("event_kind") != "independent_verification": fail("QA event_kind mismatch")
    if qa_row.get("phase") != "qa": fail("QA stage mismatch")
    if str(qa_row.get("status") or "").lower() not in {"passed", "pass"}: fail("QA verdict is not PASS")
    qa_evidence = {
        "payload": qa_row["payload"],
        "verification": qa_row["verification"],
        "artifact_refs": qa_row["artifact_refs"],
    }
    authority = next((value for value in deep_values(qa_evidence, "source_backed_contract_gate_authority") if isinstance(value, dict)), {})
    proof = authority.get("qa_session_proof") if isinstance(authority.get("qa_session_proof"), dict) else {}
    if not (
        authority.get("schema_version") == "source_backed_contract_gate_authority.v1"
        and authority.get("source") == "server_qa_session_verification"
        and authority.get("source_of_authority") == "qa_session_verification"
        and authority.get("authority_hash") == sha({key: value for key, value in authority.items() if key != "authority_hash"})
        and proof.get("schema_version") == "qa_session_scope_proof.v1"
        and proof.get("source") == "authenticated_qa_session"
        and proof.get("role") == "qa" and proof.get("verified") is True
        and proof.get("observer_impersonation") is False
        and str(proof.get("evidence_status") or "").lower() in {"accepted", "ok", "pass", "passed", "succeeded", "success"}
        and proof.get("close_satisfying") is True
        and proof.get("audit_only") is False
        and proof.get("passing_status_required_for_close") is True
        and proof.get("authority_scope") == "close_satisfying"
        and authority.get("authority_scope") == "close_satisfying"
        and authority.get("close_satisfying") is True
        and authority.get("audit_only") is False
        and proof.get("db_verified_graph_trace") is True
        and proof.get("query_source") == "qa"
        and proof.get("query_purpose") == "independent_verification"
        and proof.get("project_id") == "aming-claw"
        and proof.get("backlog_id") == manifest["backlog_id"]
        and proof.get("task_id") == manifest["contract_execution_id"]
        and proof.get("commit_sha") == candidate
        and proof.get("snapshot_commit_sha") == candidate
        and proof.get("principal_id") == qa_row.get("actor")
    ): fail("QA verdict lacks role-bound server authority")
    required_proof = ("principal_id", "qa_session_id", "qa_scope_binding_ref", "snapshot_id")
    if any(not isinstance(proof.get(key), str) or not proof.get(key) for key in required_proof):
        fail("QA verdict role-bound proof is incomplete")
    trace_ids = proof.get("graph_trace_ids")
    if not isinstance(trace_ids, list) or not trace_ids or len(trace_ids) != len(set(trace_ids)) or any(not isinstance(item, str) or not item for item in trace_ids):
        fail("QA verdict graph trace authority is incomplete")
    session = conn.execute(
        "SELECT principal_id, project_id, role, scope_json, status FROM sessions WHERE session_id=?",
        (proof["qa_session_id"],),
    ).fetchone()
    if session is None: fail("QA role-bound session is missing")
    try: session_scope = json.loads(session["scope_json"] or "[]")
    except Exception: fail("QA role-bound session scope is malformed")
    if not (
        session["principal_id"] == proof["principal_id"]
        and session["project_id"] == proof["project_id"]
        and session["role"] == "qa" and session["status"] == "active"
        and isinstance(session_scope, list)
        and proof["qa_scope_binding_ref"] in session_scope
    ): fail("QA role-bound session identity mismatch")
    placeholders = ",".join("?" for _ in trace_ids)
    trace_rows = conn.execute(
        f"""SELECT t.trace_id, t.project_id, t.snapshot_id, t.actor,
                   t.query_source, t.query_purpose, t.task_id, t.backlog_id,
                   t.commit_sha, t.qa_session_id, t.qa_scope_binding_ref,
                   t.status, s.commit_sha AS snapshot_commit_sha
            FROM graph_query_traces t
            JOIN graph_snapshots s
              ON s.project_id=t.project_id AND s.snapshot_id=t.snapshot_id
            WHERE t.project_id=? AND t.trace_id IN ({placeholders})""",
        (proof["project_id"], *trace_ids),
    ).fetchall()
    traces = {row["trace_id"]: row for row in trace_rows}
    trace_expected = {
        "project_id": proof["project_id"], "snapshot_id": proof["snapshot_id"],
        "actor": proof["principal_id"], "query_source": "qa",
        "query_purpose": "independent_verification", "task_id": proof["task_id"],
        "backlog_id": proof["backlog_id"], "commit_sha": proof["commit_sha"],
        "qa_session_id": proof["qa_session_id"],
        "qa_scope_binding_ref": proof["qa_scope_binding_ref"], "status": "complete",
        "snapshot_commit_sha": proof["snapshot_commit_sha"],
    }
    if not all(
        trace_id in traces
        and all(str(traces[trace_id][key] or "") == expected for key, expected in trace_expected.items())
        for trace_id in trace_ids
    ): fail("QA graph trace authority mismatch")
    canonical_line = next((value for value in deep_values(qa_evidence, "contract_runtime_canonical_line") if isinstance(value, dict)), {})
    if not (
        canonical_line.get("stage_id") == "qa"
        and canonical_line.get("line_id") == "qa_independent_verification"
        and canonical_line.get("contract_execution_id") == manifest["contract_execution_id"]
        and exact_sha(canonical_line.get("runtime_guide_hash"))
    ): fail("QA verdict lacks exact ContractRuntime line identity")
    review = proof.get("candidate_review_context") if isinstance(proof.get("candidate_review_context"), dict) else {}
    if not review:
        review = next((value for value in deep_values(qa_evidence, "candidate_review_context") if isinstance(value, dict)), {})
    if not (
        review.get("candidate_commit_sha") == candidate
        and review.get("comparison_base_commit_sha") == stable
        and review.get("comparison_authority_required") is True
        and review.get("candidate_diff_hash") == diff_hash
        and review.get("changed_files") == fence
    ): fail("QA exact candidate/parent/fence/diff authority mismatch")
    if not (
        contains(qa_evidence, "stable_anchor_commit", stable)
        and contains(qa_evidence, "promotion_intent_sha256", intent_hash)
        and any(item == fence for item in deep_values(qa_evidence, "file_fence"))
        and contains(qa_evidence, "stable_database_identity", database_identity)
    ): fail("QA verdict lacks exact stable/fence/intent binding")
    if any(item is True for item in deep_values(qa_evidence, "pass_synthesized")):
        fail("QA verdict synthesized PASS")
    promotion_results = next((value for value in deep_values(qa_evidence, "promotion_gate_results") if isinstance(value, dict)), {})
    branch = promotion_results.get("branch_service") if isinstance(promotion_results.get("branch_service"), dict) else {}
    if not (
        branch.get("test_id") and branch.get("status") == "passed"
        and exact_sha(branch.get("report_sha256"))
        and branch.get("runtime_plane") == "dev" and branch.get("port") == 40008
        and branch.get("bind_host") == "127.0.0.1"
    ): fail("branch-service QA subresult is incomplete")
    lanes = promotion_results.get("lanes") if isinstance(promotion_results.get("lanes"), dict) else {}
    if set(lanes) != {"direct_main", "mf_parallel", "mf_batch_parallel"}:
        fail("three-lane QA subresults are incomplete")
    for lane_name, lane in lanes.items():
        if not isinstance(lane, dict) or not (
            lane.get("test_id") and lane.get("status") == "passed"
            and exact_sha(lane.get("report_sha256"))
        ): fail(f"{lane_name} QA subresult is incomplete")
    same_authority = []
    for row in conn.execute(
        "SELECT * FROM task_timeline_events WHERE project_id=?",
        ("aming-claw",),
    ).fetchall():
        evidence = {}
        for key in ("payload_json", "verification_json", "artifact_refs_json"):
            try: evidence[key[:-5]] = json.loads(row[key] or "{}")
            except Exception: fail("QA replay candidate evidence is malformed")
        if contains(evidence, "authority_hash", authority["authority_hash"]):
            same_authority.append(dict(row))
    if len(same_authority) != 1 or int(same_authority[0]["id"]) != int(qa_row["id"]):
        fail("QA authority replay/ambiguity detected")

    queue_event_id = operator_gate.get("queue_event_id")
    if not isinstance(queue_event_id, int) or isinstance(queue_event_id, bool) or queue_event_id < 1:
        fail("operator signoff requires a positive queue_event_id")
    signoff_row = conn.execute(
        "SELECT * FROM release_operator_head_queue_events WHERE id=? AND project_id='aming-claw'",
        (queue_event_id,),
    ).fetchone()
    if signoff_row is None: fail("operator signoff event is missing")
    signoff_row = dict(signoff_row)
    try:
        before = json.loads(signoff_row.get("before_json") or "{}")
        after = json.loads(signoff_row.get("after_json") or "{}")
        signoff = json.loads(signoff_row.get("reason") or "")
    except Exception: fail("operator signoff event is not canonical JSON")
    if not (
        operator_principal(operator_gate.get("operator_principal_id"))
        and operator_principal(signoff_row.get("actor"))
        and signoff_row.get("action") == "reorder" and signoff_row.get("backlog_id") == ""
        and before == after and signoff_row.get("actor") == operator_gate.get("operator_principal_id")
    ): fail("operator signoff is not an authenticated stable no-op queue decision")
    expected_signoff = {
        "schema_version": "ac_stable_promotion_operator_signoff.v1",
        "nonce": operator_gate.get("nonce"),
        "operator_principal_id": operator_gate.get("operator_principal_id"),
        "expires_at": operator_gate.get("expires_at"),
        "project_id": "aming-claw",
        "backlog_id": manifest["backlog_id"],
        "contract_execution_id": manifest["contract_execution_id"],
        "stable_anchor_commit": stable,
        "candidate_commit": candidate,
        "promotion_intent_sha256": intent_hash,
        "promotion_manifest_sha256": manifest_hash,
        "verifier_sha256": verifier_hash,
        "diff_sha256": diff_hash,
        "file_fence": fence,
        "deploy": deploy,
        "stable_database_identity": database_identity,
    }
    if signoff != expected_signoff or signoff_row.get("reason") != json.dumps(signoff, sort_keys=True, separators=(",", ":")):
        fail("operator signoff canonical body mismatch")
    if not re.fullmatch(r"[0-9a-f]{32}", str(signoff.get("nonce") or "")):
        fail("operator signoff nonce is malformed")
    try:
        created = dt.datetime.fromisoformat(str(signoff_row["created_at"]).replace("Z", "+00:00"))
        expires = dt.datetime.fromisoformat(str(signoff["expires_at"]).replace("Z", "+00:00"))
    except ValueError: fail("operator signoff timestamps are invalid")
    now = dt.datetime.now(dt.timezone.utc)
    if created.tzinfo is None: created = created.replace(tzinfo=dt.timezone.utc)
    if expires.tzinfo is None: expires = expires.replace(tzinfo=dt.timezone.utc)
    if expires <= created or expires - created > dt.timedelta(hours=1) or now >= expires:
        fail("operator signoff is expired or exceeds the one-hour window")
    nonce_matches = []
    for row in conn.execute(
        "SELECT id, reason FROM release_operator_head_queue_events WHERE project_id='aming-claw' AND action='reorder'"
    ).fetchall():
        try: candidate_reason = json.loads(row["reason"] or "")
        except Exception: continue
        if candidate_reason.get("schema_version") == "ac_stable_promotion_operator_signoff.v1" and candidate_reason.get("nonce") == signoff["nonce"]:
            nonce_matches.append(int(row["id"]))
    if sorted(nonce_matches) != [queue_event_id]: fail("operator signoff nonce replay/ambiguity detected")
    approval_ref = f"release-operator-head-queue-event:{queue_event_id}"

    prior = manifest.get("prior_promotion"); previous_receipt = ""; prior_event_id = 0
    if stable == bootstrap:
        if prior != {"kind": "bootstrap", "stable_commit": bootstrap}: fail("exact bootstrap receipt required")
    else:
        if not isinstance(prior, dict) or prior.get("kind") != "timeline_receipt": fail("successor prior receipt required")
        previous_receipt = str(prior.get("receipt_hash") or "")
        if not exact_sha(previous_receipt): fail("prior receipt hash malformed")
        prior_event_id = prior.get("timeline_event_id")
        prior_row = event(prior_event_id)
        if prior_row.get("event_type") != "ac.stable_promotion_completed": fail("prior event type mismatch")
        if prior_row.get("commit_sha") != stable: fail("prior event not bound to current stable")
        if not contains(prior_row, "promoted_commit", stable): fail("prior promoted commit mismatch")
        if not contains(prior_row, "promotion_receipt_hash", previous_receipt): fail("prior receipt not durable")
        if not contains(prior_row, "stable_database_identity", database_identity):
            fail("prior receipt stable database identity mismatch")

    completion_rows = conn.execute(
        "SELECT * FROM task_timeline_events WHERE project_id='aming-claw' AND event_type='ac.stable_promotion_completed' ORDER BY id"
    ).fetchall()
    completion_events = []
    for row in completion_rows:
        item = dict(row)
        try: item["payload"] = json.loads(item.get("payload_json") or "{}")
        except Exception: fail("promotion completion chain contains malformed evidence")
        completion_events.append(item)
    if any(item.get("commit_sha") == candidate for item in completion_events):
        fail("candidate already has a promotion completion receipt")
    if any(
        isinstance(item.get("payload"), dict)
        and item["payload"].get("previous_stable_commit") == stable
        and item["payload"].get("promoted_commit") != candidate
        for item in completion_events
    ):
        fail("stable promotion chain already has a different successor")
    if stable != bootstrap:
        matching_prior = [
            item for item in completion_events
            if item.get("commit_sha") == stable
            and item.get("payload", {}).get("promoted_commit") == stable
            and item.get("payload", {}).get("promotion_receipt_hash") == previous_receipt
            and item.get("payload", {}).get("stable_database_identity") == database_identity
        ]
        if len(matching_prior) != 1 or int(matching_prior[0].get("id") or 0) != prior_event_id:
            fail("prior promotion receipt is missing or ambiguous")

    evidence_hashes = {
        "qa_verdict": sha({key: qa_row.get(key) for key in (
            "id", "project_id", "backlog_id", "task_id", "event_type", "phase",
            "event_kind", "actor", "status", "payload", "verification", "artifact_refs", "commit_sha",
        )}),
        "operator_signoff": sha({key: signoff_row.get(key) for key in (
            "id", "project_id", "action", "backlog_id", "actor", "reason",
            "before_json", "after_json", "created_at",
        )}),
    }
finally: conn.close()

body = {
    "schema_version": "ac_stable_promotion_precheck_receipt.v1",
    "verifier_version": "readonly_timeline_projector.v1",
    "verifier_sha256": verifier_hash,
    "promotion_intent_sha256": intent_hash,
    "promotion_manifest_sha256": manifest_hash,
    "stable_anchor_commit": stable,
    "candidate_commit": candidate,
    "diff_sha256": diff_hash,
    "stable_database_identity": database_identity,
    "previous_promotion_receipt_hash": previous_receipt,
    "prior_promotion_event_id": prior_event_id,
    "gate_event_ids": {
        "qa_verdict": qa_row["id"],
        "operator_signoff": queue_event_id,
    },
    "operator_approval_ref": approval_ref,
    "gate_evidence_hashes": evidence_hashes,
    "pass_synthesized": False,
    "writes_performed": False,
}
print(json.dumps({**body, "receipt_hash": sha(body)}, sort_keys=True))
PY
}
promotion_precheck_v2() {
python3 - "$MANIFEST" "$LIVE_DB" "$CURRENT_STABLE" "$CANDIDATE_COMMIT" "$VERIFIER_SHA256" "$BOOTSTRAP_ANCHOR" "$ROLLBACK_BASELINE" "$ROLLBACK_BACKLOG" "$ROLLBACK_CEX" "$STABLE_DATABASE_IDENTITY" <<'PY'
import datetime as dt
import hashlib
import json
import re
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

(
    manifest_path,
    db_raw,
    stable,
    candidate,
    verifier_hash,
    bootstrap,
    rollback_baseline,
    rollback_backlog,
    rollback_cex,
    database_identity_raw,
) = sys.argv[1:]
manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
database_identity = json.loads(database_identity_raw)


def fail(message):
    raise SystemExit("Promotion blocked: " + message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha(value):
    raw = value if isinstance(value, bytes) else canonical(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def exact_sha(value):
    return bool(re.fullmatch(r"sha256:[0-9a-f]{64}", str(value or "")))


def deep_values(value, key):
    if isinstance(value, dict):
        for name, item in value.items():
            if name == key:
                yield item
            yield from deep_values(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from deep_values(item, key)


def contains(value, key, expected):
    return any(item == expected for item in deep_values(value, key))


def git_bytes(*args):
    result = subprocess.run(
        ["git", *args], capture_output=True, check=False
    )
    if result.returncode:
        fail("candidate Git authority is unavailable")
    return result.stdout


required_top = {
    "schema_version", "project_id", "backlog_id", "contract_execution_id",
    "stable_anchor_commit", "stable_branch", "branch", "candidate_commit",
    "candidate_tree_sha", "stable_runtime_source_sha256",
    "candidate_runtime_source_sha256",
    "implementation_delta", "promotion_delta", "qa_candidate_intent_sha256",
    "rollback_authority_hash", "deploy", "stable_database_identity",
    "activation_policy", "custody_authority",
    "promotion_intent_sha256", "promotion_manifest_sha256", "gates",
    "prior_promotion",
}
if manifest.get("schema_version") != "ac_stable_promotion_manifest.v2":
    fail("v2 schema_version mismatch")
if set(manifest) != required_top:
    fail("v2 manifest has missing or extra fields")
if not (
    manifest.get("project_id") == "aming-claw"
    and manifest.get("backlog_id") == rollback_backlog
    and manifest.get("contract_execution_id") == rollback_cex
    and manifest.get("stable_anchor_commit") == stable == bootstrap
    and manifest.get("stable_branch") == "codex/direct-no-pass-post-reconcile-r2"
    and manifest.get("branch") == "codex/ac-dev"
    and manifest.get("candidate_commit") == candidate
    and re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("candidate_tree_sha") or ""))
    and exact_sha(manifest.get("stable_runtime_source_sha256"))
    and exact_sha(manifest.get("candidate_runtime_source_sha256"))
):
    fail("v2 manifest scope/lineage mismatch")
if manifest.get("stable_database_identity") != database_identity:
    fail("v2 stable database identity mismatch")
deploy = manifest.get("deploy")
if deploy != {
    "authorized": True,
    "mode": "host_supervisor_activation_plan_v2",
    "stable_port": 40000,
}:
    fail("v2 deploy identity mismatch")
activation_policy = manifest.get("activation_policy")
if activation_policy != {
    "schema_version": "ac_stable_activation_policy.v2",
    "prepare_required": True,
    "activation_plan_required": True,
    "automatic_activation": False,
    "rollback_required_after_first_mutation": True,
}:
    fail("v2 activation policy mismatch")
if not exact_sha(manifest.get("rollback_authority_hash")):
    fail("v2 rollback authority hash malformed")
if not exact_sha(manifest.get("qa_candidate_intent_sha256")):
    fail("v2 immutable QA candidate intent hash malformed")
if not isinstance(manifest.get("custody_authority"), dict):
    fail("v2 route/CEX custody authority missing")


def exact_delta(value, base):
    required = {
        "base_commit", "candidate_commit", "file_fence",
        "file_fence_sha256", "diff_sha256", "diff_byte_length",
        "source_sha256", "delta_hash",
    }
    if not isinstance(value, dict) or set(value) != required:
        fail("v2 delta shape mismatch")
    files = value.get("file_fence")
    sources = value.get("source_sha256")
    if not (
        value.get("base_commit") == base
        and value.get("candidate_commit") == candidate
        and isinstance(files, list)
        and files == sorted(set(files))
        and files
        and isinstance(sources, dict)
        and set(sources) == set(files)
        and all(exact_sha(item) for item in sources.values())
        and value.get("file_fence_sha256") == sha(files)
        and exact_sha(value.get("diff_sha256"))
        and isinstance(value.get("diff_byte_length"), int)
        and not isinstance(value.get("diff_byte_length"), bool)
        and value.get("diff_byte_length") > 0
        and value.get("delta_hash")
        == sha({key: item for key, item in value.items() if key != "delta_hash"})
    ):
        fail("v2 delta authority mismatch")
    actual_files = sorted(
        item.decode("utf-8", errors="surrogateescape")
        for item in git_bytes("diff", "--name-only", "-z", base, candidate, "--", ".").split(b"\0")
        if item
    )
    diff = git_bytes(
        "diff", "--no-ext-diff", "--no-textconv", "--binary",
        "--full-index", "-M", f"{base}..{candidate}", "--", ".",
    )
    actual_sources = {
        path: sha(git_bytes("show", f"{candidate}:{path}")) for path in actual_files
    }
    if not (
        actual_files == files
        and sha(diff) == value["diff_sha256"]
        and len(diff) == value["diff_byte_length"]
        and actual_sources == sources
    ):
        fail("v2 delta differs from exact Git authority")
    return value


parents = git_bytes("rev-list", "--parents", "-n", "1", candidate).decode().split()
if parents != [candidate, rollback_baseline]:
    fail("v2 candidate must be the direct single-parent D descendant")
implementation_delta = exact_delta(manifest.get("implementation_delta"), rollback_baseline)
promotion_delta = exact_delta(manifest.get("promotion_delta"), stable)
candidate_tree_sha = git_bytes("rev-parse", f"{candidate}^{{tree}}").decode().strip()
stable_runtime_source_sha256 = sha(
    git_bytes("show", f"{stable}:agent/governance/server.py")
)
candidate_runtime_source_sha256 = sha(
    git_bytes("show", f"{candidate}:agent/governance/server.py")
)
if not (
    manifest.get("candidate_tree_sha") == candidate_tree_sha
    and manifest.get("stable_runtime_source_sha256")
    == stable_runtime_source_sha256
    and manifest.get("candidate_runtime_source_sha256")
    == candidate_runtime_source_sha256
):
    fail("v2 candidate tree/runtime source authority mismatch")
if implementation_delta["file_fence"] != sorted(
    [
        "agent/governance/server.py",
        "agent/tests/test_deploy_chain.py",
        "agent/tests/test_graph_governance_api.py",
        "docs/governance/reconcile-workflow.md",
        "scripts/merge-and-deploy.sh",
    ]
):
    fail("v2 implementation fence is not the exact rollback row fence")

intent_keys = (
    "schema_version", "project_id", "backlog_id", "contract_execution_id",
    "stable_anchor_commit", "stable_branch", "branch", "candidate_commit",
    "candidate_tree_sha", "stable_runtime_source_sha256",
    "candidate_runtime_source_sha256",
    "implementation_delta", "promotion_delta", "qa_candidate_intent_sha256",
    "rollback_authority_hash", "deploy", "stable_database_identity",
    "activation_policy", "custody_authority",
)
intent = {key: manifest[key] for key in intent_keys}
intent_hash = sha(intent)
if manifest.get("promotion_intent_sha256") != intent_hash:
    fail("v2 promotion intent digest mismatch")
qa_candidate_intent = {
    "schema_version": "ac_stable_promotion_qa_candidate_intent.v2",
    "project_id": "aming-claw",
    "backlog_id": rollback_backlog,
    "contract_execution_id": rollback_cex,
    "stable_anchor_commit": stable,
    "stable_branch": "codex/direct-no-pass-post-reconcile-r2",
    "branch": "codex/ac-dev",
    "candidate_commit": candidate,
    "candidate_tree_sha": candidate_tree_sha,
    "stable_runtime_source_sha256": stable_runtime_source_sha256,
    "candidate_runtime_source_sha256": candidate_runtime_source_sha256,
    "implementation_delta": implementation_delta,
    "promotion_delta": promotion_delta,
    "deploy": deploy,
    "stable_database_identity": database_identity,
    "activation_policy": activation_policy,
    "custody_authority": manifest["custody_authority"],
}
qa_candidate_intent_hash = sha(qa_candidate_intent)
if manifest.get("qa_candidate_intent_sha256") != qa_candidate_intent_hash:
    fail("v2 immutable QA candidate intent digest mismatch")
gates = manifest.get("gates")
if not isinstance(gates, dict) or set(gates) != {"qa_verdict", "operator_signoff"}:
    fail("v2 exact QA and operator gates required")
qa_gate = gates.get("qa_verdict")
operator_gate = gates.get("operator_signoff")
if not (
    isinstance(qa_gate, dict)
    and set(qa_gate) == {"timeline_event_id", "status"}
    and qa_gate.get("status") == "passed"
):
    fail("v2 QA gate malformed")
if not (
    isinstance(operator_gate, dict)
    and set(operator_gate)
    == {"status", "nonce", "operator_principal_id", "expires_at", "queue_event_id"}
    and operator_gate.get("status") == "approved"
):
    fail("v2 operator gate malformed")
signable_operator = {
    key: operator_gate[key]
    for key in ("status", "nonce", "operator_principal_id", "expires_at")
}
signable = {
    **intent,
    "promotion_intent_sha256": intent_hash,
    "prior_promotion": manifest.get("prior_promotion"),
    "gates": {
        "qa_verdict": dict(qa_gate),
        "operator_signoff": signable_operator,
    },
}
manifest_hash = sha(signable)
if manifest.get("promotion_manifest_sha256") != manifest_hash:
    fail("v2 promotion manifest digest mismatch")
if manifest.get("prior_promotion") != {
    "kind": "bootstrap", "stable_commit": bootstrap
}:
    fail("v2 bootstrap predecessor mismatch")

db_path = Path(db_raw).absolute()
before = db_path.stat(follow_symlinks=False)
if db_path.is_symlink() or not stat.S_ISREG(before.st_mode) or db_path.resolve(strict=True) != db_path:
    fail("v2 live DB identity invalid")
if (int(before.st_dev), int(before.st_ino)) != (
    int(database_identity["device"]), int(database_identity["inode"])
):
    fail("v2 live DB inode mismatch")
conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA query_only=ON")
try:
    schema = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if schema is None or int(schema[0]) != 47:
        fail("v2 live DB schema mismatch")
    custody = manifest["custody_authority"]
    implementation_rows = conn.execute(
        "SELECT record_json FROM contract_runtime_executions "
        "WHERE contract_execution_id=?",
        (rollback_cex,),
    ).fetchall()
    if len(implementation_rows) != 1:
        fail("v2 Direct Main CEX custody missing or ambiguous")
    try:
        implementation_record = json.loads(implementation_rows[0][0])
    except Exception:
        fail("v2 Direct Main CEX record malformed")
    implementation_lines = [
        item
        for item in (implementation_record.get("completed_lines") or [])
        if isinstance(item, dict)
        and item.get("line_id") == "observer_implementation"
        and str(item.get("status") or "").lower() in {"pass", "passed"}
        and str(item.get("commit_sha") or "").lower() == candidate
    ]
    metadata = implementation_record.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    binding = metadata.get("operator_supervised_direct_main_runtime_binding")
    binding = binding if isinstance(binding, dict) else {}
    binding_unsigned = {
        key: item for key, item in binding.items() if key != "binding_hash"
    }
    binding_route = binding.get("route_identity")
    binding_route = binding_route if isinstance(binding_route, dict) else {}
    implementation_line = implementation_lines[0] if len(implementation_lines) == 1 else {}
    implementation_route_ref = str(
        implementation_line.get("route_token_ref")
        or (implementation_line.get("payload") or {}).get("route_token_ref")
        or ""
    )
    if not (
        implementation_record.get("contract_execution_id") == rollback_cex
        and implementation_record.get("contract_id")
        == "operator_supervised_direct_main"
        and int(implementation_record.get("execution_state_revision") or 0) > 0
        and implementation_record.get("execution_state_hash")
        == sha(
            {
                key: item
                for key, item in implementation_record.items()
                if key != "execution_state_hash"
            }
        )
        and len(implementation_lines) == 1
        and binding.get("binding_hash") == sha(binding_unsigned)
        and implementation_route_ref
        == str(binding_route.get("route_token_ref") or "")
    ):
        fail("v2 Direct Main implementation custody mismatch")
    implementation_route_rows = conn.execute(
        "SELECT status,scope_json,backlog_id,task_id FROM "
        "observer_route_token_refs WHERE route_token_ref=?",
        (implementation_route_ref,),
    ).fetchall()
    try:
        implementation_route_scope = json.loads(
            implementation_route_rows[0]["scope_json"] or "{}"
        ) if len(implementation_route_rows) == 1 else {}
    except Exception:
        implementation_route_scope = {}
    if not (
        len(implementation_route_rows) == 1
        and implementation_route_rows[0]["status"] == "active"
        and implementation_route_rows[0]["backlog_id"] == rollback_backlog
        and implementation_route_rows[0]["task_id"] == rollback_cex
        and implementation_route_scope.get("project_id") == "aming-claw"
        and implementation_route_scope.get("backlog_id") == rollback_backlog
        and implementation_route_scope.get("task_id") == rollback_cex
    ):
        fail("v2 Direct Main implementation route is not current")

    promotion_route_ref = str(custody.get("promotion_route_token_ref") or "")
    route_rows = conn.execute(
        "SELECT * FROM observer_route_token_refs "
        "WHERE project_id='aming-claw' AND route_token_ref=?",
        (promotion_route_ref,),
    ).fetchall()
    if len(route_rows) != 1:
        fail("v2 canonical promotion route missing or ambiguous")
    route_row = dict(route_rows[0])
    try:
        route_actions = json.loads(route_row.get("allowed_actions_json") or "[]")
        route_targets = json.loads(route_row.get("target_files_json") or "[]")
        route_owned = json.loads(route_row.get("owned_files_json") or "[]")
        route_evidence = json.loads(route_row.get("evidence_refs_json") or "[]")
        route_scope = json.loads(route_row.get("scope_json") or "{}")
        route_expires = dt.datetime.fromisoformat(
            str(route_row.get("expires_at") or "").replace("Z", "+00:00")
        )
    except Exception:
        fail("v2 canonical promotion route evidence malformed")
    if route_expires.tzinfo is None:
        route_expires = route_expires.replace(tzinfo=dt.timezone.utc)
    expected_route_evidence = sorted(
        {
            f"backlog:{rollback_backlog}",
            f"contract_runtime:{rollback_cex}",
            f"candidate_commit:{candidate}",
            f"candidate_tree:{candidate_tree_sha}",
            f"stable_anchor:{stable}",
            f"implementation_delta:{implementation_delta['delta_hash']}",
            f"promotion_delta:{promotion_delta['delta_hash']}",
            f"candidate_authority:{custody.get('candidate_authority_hash', '')}",
            "stable_database_identity:" + sha(database_identity),
        }
    )
    route_identity = {
        key: str(route_row.get(key) or "").strip()
        for key in (
            "route_id", "route_context_hash", "prompt_contract_id",
            "prompt_contract_hash", "visible_injection_manifest_hash",
            "route_token_ref",
        )
    }
    route_authority = {
        "schema_version": "ac_promotion_rollback_route_authority.v1",
        "accepted": True,
        "source": "canonical_stable_route_registry",
        "required_endpoint": "http://127.0.0.1:40000",
        "project_id": "aming-claw",
        "backlog_id": rollback_backlog,
        "contract_execution_id": rollback_cex,
        "candidate_commit": candidate,
        "route_token_ref": promotion_route_ref,
        "route_identity": route_identity,
        "evidence_refs_hash": sha(sorted(expected_route_evidence)),
        "writes_performed": False,
    }
    route_authority["authority_hash"] = sha(route_authority)
    if not (
        route_row.get("status") == "active"
        and route_row.get("backlog_id") == rollback_backlog
        and route_row.get("task_id") == rollback_cex
        and route_row.get("caller_role") == "observer"
        and route_actions == ["ac_stable_promotion_prepare"]
        and sorted(route_targets) == implementation_delta["file_fence"]
        and sorted(route_owned) == implementation_delta["file_fence"]
        and sorted(route_evidence) == sorted(expected_route_evidence)
        and route_scope.get("project_id") == "aming-claw"
        and route_scope.get("backlog_id") == rollback_backlog
        and route_scope.get("task_id") == rollback_cex
        and route_expires > dt.datetime.now(dt.timezone.utc)
        and all(route_identity.values())
    ):
        fail("v2 canonical promotion route authority mismatch")
    expected_custody = {
        "schema_version": "ac_promotion_rollback_custody_authority.v1",
        "contract_execution_id": rollback_cex,
        "contract_id": "operator_supervised_direct_main",
        "implementation_route_ref": implementation_route_ref,
        "implementation_binding_hash": binding["binding_hash"],
        "implementation_line_id": "observer_implementation",
        "implementation_line_hash": sha(implementation_line),
        "implementation_runtime_revision": int(
            implementation_record["execution_state_revision"]
        ),
        "implementation_runtime_state_hash": implementation_record[
            "execution_state_hash"
        ],
        "implementation_commit_sha": candidate,
        "candidate_authority_hash": custody.get("candidate_authority_hash"),
        "promotion_route_token_ref": promotion_route_ref,
        "promotion_route_authority_hash": route_authority["authority_hash"],
        "promotion_route_identity": route_identity,
        "writes_performed": False,
    }
    expected_custody["authority_hash"] = sha(expected_custody)
    if custody != expected_custody:
        fail("v2 route/CEX custody authority changed")
    if manifest.get("qa_candidate_intent_sha256") != qa_candidate_intent_hash:
        fail("v2 immutable QA candidate intent changed")
    qa_id = qa_gate.get("timeline_event_id")
    if not isinstance(qa_id, int) or isinstance(qa_id, bool) or qa_id < 1:
        fail("v2 QA timeline id malformed")
    qa_row = conn.execute(
        "SELECT * FROM task_timeline_events WHERE id=? AND project_id='aming-claw'",
        (qa_id,),
    ).fetchone()
    if qa_row is None:
        fail("v2 QA event missing")
    qa_row = dict(qa_row)
    try:
        evidence = {
            "payload": json.loads(qa_row.get("payload_json") or "{}"),
            "verification": json.loads(qa_row.get("verification_json") or "{}"),
            "artifact_refs": json.loads(qa_row.get("artifact_refs_json") or "{}"),
        }
    except Exception:
        fail("v2 QA evidence malformed")
    authority = next(
        (item for item in deep_values(evidence, "source_backed_contract_gate_authority") if isinstance(item, dict)),
        {},
    )
    proof = authority.get("qa_session_proof") if isinstance(authority.get("qa_session_proof"), dict) else {}
    review = proof.get("candidate_review_context") if isinstance(proof.get("candidate_review_context"), dict) else next(
        (item for item in deep_values(evidence, "candidate_review_context") if isinstance(item, dict)), {}
    )
    impl_review = next(
        (item for item in deep_values(evidence, "implementation_delta_review_context") if isinstance(item, dict)), {}
    )
    results = next(
        (item for item in deep_values(evidence, "promotion_gate_results") if isinstance(item, dict)), {}
    )
    branch_result = results.get("branch_service") if isinstance(results.get("branch_service"), dict) else {}
    lanes = results.get("lanes") if isinstance(results.get("lanes"), dict) else {}
    canonical_line = next(
        (item for item in deep_values(evidence, "contract_runtime_canonical_line") if isinstance(item, dict)), {}
    )
    if not (
        qa_row.get("backlog_id") == rollback_backlog
        and qa_row.get("task_id") == rollback_cex
        and qa_row.get("event_type") == "qa.independent_verification"
        and qa_row.get("phase") == "qa"
        and qa_row.get("event_kind") == "independent_verification"
        and str(qa_row.get("status") or "").lower() in {"pass", "passed"}
        and qa_row.get("commit_sha") == candidate
        and authority.get("authority_hash")
        == sha({key: item for key, item in authority.items() if key != "authority_hash"})
        and authority.get("schema_version") == "source_backed_contract_gate_authority.v1"
        and authority.get("source") == "server_qa_session_verification"
        and authority.get("source_of_authority") == "qa_session_verification"
        and authority.get("authority_scope") == "close_satisfying"
        and authority.get("close_satisfying") is True
        and authority.get("audit_only") is False
        and proof.get("role") == "qa"
        and proof.get("verified") is True
        and proof.get("observer_impersonation") is False
        and str(proof.get("evidence_status") or "").lower()
        in {"accepted", "ok", "pass", "passed", "succeeded", "success"}
        and proof.get("authority_scope") == "close_satisfying"
        and proof.get("close_satisfying") is True
        and proof.get("audit_only") is False
        and proof.get("passing_status_required_for_close") is True
        and proof.get("db_verified_graph_trace") is True
        and proof.get("query_source") == "qa"
        and proof.get("query_purpose") == "independent_verification"
        and proof.get("principal_id") == qa_row.get("actor")
        and proof.get("project_id") == "aming-claw"
        and proof.get("backlog_id") == rollback_backlog
        and proof.get("task_id") == rollback_cex
        and proof.get("commit_sha") == candidate
        and proof.get("snapshot_commit_sha") == candidate
        and canonical_line.get("stage_id") == "qa"
        and canonical_line.get("line_id") == "qa_independent_verification"
        and canonical_line.get("contract_execution_id") == rollback_cex
        and exact_sha(canonical_line.get("runtime_guide_hash"))
        and review.get("candidate_commit_sha") == candidate
        and review.get("comparison_base_commit_sha") == stable
        and review.get("comparison_authority_required") is True
        and review.get("candidate_diff_hash") == promotion_delta["diff_sha256"]
        and list(review.get("changed_files") or []) == promotion_delta["file_fence"]
        and impl_review.get("candidate_commit_sha") == candidate
        and impl_review.get("comparison_base_commit_sha") == rollback_baseline
        and impl_review.get("candidate_diff_hash") == implementation_delta["diff_sha256"]
        and list(impl_review.get("changed_files") or []) == implementation_delta["file_fence"]
        and contains(evidence, "implementation_delta", implementation_delta)
        and contains(evidence, "promotion_delta", promotion_delta)
        and contains(evidence, "candidate_tree_sha", candidate_tree_sha)
        and contains(
            evidence,
            "stable_runtime_source_sha256",
            stable_runtime_source_sha256,
        )
        and contains(
            evidence,
            "candidate_runtime_source_sha256",
            candidate_runtime_source_sha256,
        )
        and contains(
            evidence,
            "qa_candidate_intent_sha256",
            qa_candidate_intent_hash,
        )
        and contains(evidence, "stable_database_identity", database_identity)
        and not any(item is True for item in deep_values(evidence, "pass_synthesized"))
        and branch_result.get("status") == "passed"
        and branch_result.get("runtime_plane") == "dev"
        and branch_result.get("port") == 40008
        and branch_result.get("bind_host") == "127.0.0.1"
        and exact_sha(branch_result.get("report_sha256"))
        and set(lanes) == {"direct_main", "mf_parallel", "mf_batch_parallel"}
        and all(
            isinstance(item, dict)
            and item.get("status") == "passed"
            and item.get("test_id")
            and exact_sha(item.get("report_sha256"))
            for item in lanes.values()
        )
    ):
        fail("v2 QA authority mismatch")
    session = conn.execute(
        "SELECT principal_id,project_id,role,scope_json,status FROM sessions WHERE session_id=?",
        (str(proof.get("qa_session_id") or ""),),
    ).fetchone()
    try:
        session_scope = json.loads(session["scope_json"] or "[]") if session else []
    except Exception:
        fail("v2 QA session scope malformed")
    if not (
        session
        and session["principal_id"] == proof.get("principal_id")
        and session["project_id"] == "aming-claw"
        and session["role"] == "qa"
        and session["status"] == "active"
        and proof.get("qa_scope_binding_ref") in session_scope
    ):
        fail("v2 QA role-bound session mismatch")
    trace_ids = proof.get("graph_trace_ids")
    if not (
        isinstance(trace_ids, list)
        and trace_ids
        and len(trace_ids) == len(set(trace_ids))
        and all(isinstance(item, str) and item for item in trace_ids)
    ):
        fail("v2 QA graph trace authority malformed")
    placeholders = ",".join("?" for _ in trace_ids)
    trace_rows = conn.execute(
        f"""SELECT t.trace_id,t.project_id,t.snapshot_id,t.actor,
                   t.query_source,t.query_purpose,t.task_id,t.backlog_id,
                   t.commit_sha,t.qa_session_id,t.qa_scope_binding_ref,t.status,
                   s.commit_sha AS snapshot_commit_sha
              FROM graph_query_traces t
              JOIN graph_snapshots s
                ON s.project_id=t.project_id AND s.snapshot_id=t.snapshot_id
             WHERE t.project_id=? AND t.trace_id IN ({placeholders})""",
        ("aming-claw", *trace_ids),
    ).fetchall()
    trace_by_id = {row["trace_id"]: row for row in trace_rows}
    trace_expected = {
        "project_id": "aming-claw",
        "snapshot_id": proof.get("snapshot_id"),
        "actor": proof.get("principal_id"),
        "query_source": "qa",
        "query_purpose": "independent_verification",
        "task_id": rollback_cex,
        "backlog_id": rollback_backlog,
        "commit_sha": candidate,
        "qa_session_id": proof.get("qa_session_id"),
        "qa_scope_binding_ref": proof.get("qa_scope_binding_ref"),
        "status": "complete",
        "snapshot_commit_sha": candidate,
    }
    if not all(
        trace_id in trace_by_id
        and all(str(trace_by_id[trace_id][key] or "") == str(expected or "") for key, expected in trace_expected.items())
        for trace_id in trace_ids
    ):
        fail("v2 QA graph trace authority mismatch")
    same_authority = 0
    for raw in conn.execute(
        "SELECT payload_json,verification_json,artifact_refs_json FROM task_timeline_events WHERE project_id='aming-claw'"
    ).fetchall():
        try:
            projected = [json.loads(raw[index] or "{}") for index in range(3)]
        except Exception:
            fail("v2 QA replay evidence malformed")
        if contains(projected, "authority_hash", authority.get("authority_hash")):
            same_authority += 1
    if same_authority != 1:
        fail("v2 QA authority replay/ambiguity")

    rollback = conn.execute(
        'SELECT status,fixed_at,"commit" FROM backlog_bugs WHERE bug_id=?',
        (rollback_backlog,),
    ).fetchone()
    if not (
        rollback
        and str(rollback["status"] or "").upper() == "FIXED"
        and str(rollback["fixed_at"] or "").strip()
        and str(rollback["commit"] or "").strip().lower() == candidate
    ):
        fail("v2 rollback row is not fixed at exact D")
    rollback_authority = {
        "schema_version": "ac_promotion_rollback_row_authority.v2",
        "accepted": True,
        "status": str(rollback["status"] or "").upper(),
        "backlog_id": rollback_backlog,
        "fix_commit": str(rollback["commit"] or "").strip().lower(),
        "fixed_at": str(rollback["fixed_at"] or "").strip(),
        "candidate_commit": candidate,
        "candidate_parent_commit": rollback_baseline,
        "exact_descendant_d_required": True,
        "server_derived": True,
        "writes_performed": False,
    }
    rollback_authority["authority_hash"] = sha(rollback_authority)
    if manifest.get("rollback_authority_hash") != rollback_authority["authority_hash"]:
        fail("v2 rollback authority hash mismatch")

    queue_id = operator_gate.get("queue_event_id")
    if not isinstance(queue_id, int) or isinstance(queue_id, bool) or queue_id < 1:
        fail("v2 operator queue id malformed")
    signoff_row = conn.execute(
        "SELECT * FROM release_operator_head_queue_events WHERE id=? AND project_id='aming-claw'",
        (queue_id,),
    ).fetchone()
    if signoff_row is None:
        fail("v2 operator signoff missing")
    signoff_row = dict(signoff_row)
    try:
        reason = json.loads(signoff_row.get("reason") or "")
        before_json = json.loads(signoff_row.get("before_json") or "{}")
        after_json = json.loads(signoff_row.get("after_json") or "{}")
        created = dt.datetime.fromisoformat(str(signoff_row.get("created_at") or "").replace("Z", "+00:00"))
        expires = dt.datetime.fromisoformat(str(reason.get("expires_at") or "").replace("Z", "+00:00"))
    except Exception:
        fail("v2 operator signoff malformed")
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.timezone.utc)
    expected_reason = {
        "schema_version": "ac_stable_promotion_operator_signoff.v2",
        "nonce": operator_gate.get("nonce"),
        "operator_principal_id": operator_gate.get("operator_principal_id"),
        "expires_at": operator_gate.get("expires_at"),
        "project_id": "aming-claw",
        "backlog_id": rollback_backlog,
        "contract_execution_id": rollback_cex,
        "stable_anchor_commit": stable,
        "candidate_commit": candidate,
        "candidate_tree_sha": candidate_tree_sha,
        "stable_runtime_source_sha256": stable_runtime_source_sha256,
        "candidate_runtime_source_sha256": candidate_runtime_source_sha256,
        "promotion_intent_sha256": intent_hash,
        "qa_candidate_intent_sha256": qa_candidate_intent_hash,
        "promotion_manifest_sha256": manifest_hash,
        "verifier_sha256": verifier_hash,
        "implementation_delta_hash": implementation_delta["delta_hash"],
        "promotion_delta_hash": promotion_delta["delta_hash"],
        "rollback_authority_hash": manifest["rollback_authority_hash"],
        "custody_authority": custody,
        "stable_database_identity": database_identity,
    }
    actor = str(signoff_row.get("actor") or "")
    if not (
        reason == expected_reason
        and signoff_row.get("reason") == canonical(reason)
        and signoff_row.get("action") == "reorder"
        and signoff_row.get("backlog_id") == ""
        and before_json == after_json
        and actor == operator_gate.get("operator_principal_id")
        and actor and not actor.endswith(":route_ref")
        and re.fullmatch(r"[0-9a-f]{32}", str(reason.get("nonce") or ""))
        and expires > created
        and expires - created <= dt.timedelta(hours=1)
        and dt.datetime.now(dt.timezone.utc) < expires
    ):
        fail("v2 operator signoff authority mismatch")
    nonce_count = 0
    for row in conn.execute(
        "SELECT reason FROM release_operator_head_queue_events WHERE project_id='aming-claw' AND action='reorder'"
    ).fetchall():
        try:
            item = json.loads(row[0] or "")
        except Exception:
            continue
        if item.get("schema_version") == "ac_stable_promotion_operator_signoff.v2" and item.get("nonce") == reason["nonce"]:
            nonce_count += 1
    if nonce_count != 1:
        fail("v2 operator nonce replay/ambiguity")
    completion = conn.execute(
        "SELECT id FROM task_timeline_events WHERE project_id='aming-claw' AND event_type='ac.stable_promotion_completed' AND commit_sha=?",
        (candidate,),
    ).fetchall()
    if completion:
        fail("v2 candidate already has a completion receipt")
finally:
    conn.close()

evidence_hashes = {
    "qa_verdict": sha({
        "id": qa_row.get("id"),
        "project_id": qa_row.get("project_id"),
        "backlog_id": qa_row.get("backlog_id"),
        "task_id": qa_row.get("task_id"),
        "event_type": qa_row.get("event_type"),
        "phase": qa_row.get("phase"),
        "event_kind": qa_row.get("event_kind"),
        "actor": qa_row.get("actor"),
        "status": qa_row.get("status"),
        "payload": evidence["payload"],
        "verification": evidence["verification"],
        "artifact_refs": evidence["artifact_refs"],
        "commit_sha": qa_row.get("commit_sha"),
    }),
    "operator_signoff": sha({
        key: signoff_row.get(key)
        for key in (
            "id", "project_id", "action", "backlog_id", "actor", "reason",
            "before_json", "after_json", "created_at",
        )
    }),
}
body = {
    "schema_version": "ac_stable_promotion_precheck_receipt.v2",
    "verifier_version": "readonly_timeline_projector.v2",
    "verifier_sha256": verifier_hash,
    "promotion_intent_sha256": intent_hash,
    "qa_candidate_intent_sha256": qa_candidate_intent_hash,
    "promotion_manifest_sha256": manifest_hash,
    "stable_anchor_commit": stable,
    "candidate_commit": candidate,
    "candidate_tree_sha": candidate_tree_sha,
    "stable_runtime_source_sha256": stable_runtime_source_sha256,
    "candidate_runtime_source_sha256": candidate_runtime_source_sha256,
    "implementation_delta_hash": implementation_delta["delta_hash"],
    "promotion_delta_hash": promotion_delta["delta_hash"],
    "rollback_authority_hash": manifest["rollback_authority_hash"],
    "custody_authority": custody,
    "stable_database_identity": database_identity,
    "previous_promotion_receipt_hash": "",
    "prior_promotion_event_id": 0,
    "gate_event_ids": {
        "qa_verdict": qa_id,
        "operator_signoff": queue_id,
    },
    "operator_approval_ref": f"release-operator-head-queue-event:{queue_id}",
    "gate_evidence_hashes": evidence_hashes,
    "pass_synthesized": False,
    "writes_performed": False,
}
print(canonical({**body, "receipt_hash": sha(body)}))
PY
}
MANIFEST_SCHEMA="$(manifest_value schema_version)"
if [ "$MANIFEST_SCHEMA" = "ac_stable_promotion_manifest.v2" ]; then
    PRECHECK_RECEIPT="$(promotion_precheck_v2)"
elif [ "$MANIFEST_SCHEMA" = "ac_stable_promotion_manifest.v1" ]; then
    PRECHECK_RECEIPT="$(promotion_precheck)"
else
    echo "Promotion blocked: unsupported promotion manifest schema." >&2
    exit 1
fi
PRECHECK_RECEIPT_HASH="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["receipt_hash"])' <<<"$PRECHECK_RECEIPT")"

# Fallible source checks and stable-anchor health all happen before mutation.
python3 -m py_compile agent/cli.py agent/governance/db.py agent/governance/server.py
bash -n scripts/merge-and-deploy.sh
OLD_PID="$(python3 - "$CURRENT_STABLE" "$STABLE_PORT" "$STABLE_DATABASE_IDENTITY" "$STABLE_RUNTIME_SOURCE_SHA256" <<'PY'
import json, sys, urllib.request
anchor, port, database_identity_raw, source_sha256 = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
database_identity = json.loads(database_identity_raw)
with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
    health = json.load(response)
plane_identity = health.get("runtime_plane_identity") or {}
loaded_identity = health.get("loaded_runtime_identity") or {}
health_database_identity = plane_identity.get("stable_database_identity")
database_identity_ok = (
    health_database_identity == database_identity
    if health_database_identity is not None
    else anchor == "a25838f15f949ac434cf78e03f20760e82ff81f0"
)
plane_identity_ok = bool(
    not plane_identity
    and anchor == "a25838f15f949ac434cf78e03f20760e82ff81f0"
    or (
        plane_identity.get("plane") == "stable"
        and plane_identity.get("branch")
        == "codex/direct-no-pass-post-reconcile-r2"
        and plane_identity.get("commit") == anchor
        and plane_identity.get("stable_anchor_commit") == anchor
        and plane_identity.get("stable_database_identity") == database_identity
    )
)
source_identity_ok = bool(
    not source_sha256
    or (
        health.get("runtime_loaded_source_sha256") == source_sha256
        and loaded_identity.get("loaded_source_sha256") == source_sha256
        and loaded_identity.get("worktree_source_sha256") == source_sha256
    )
)
if not (
    health.get("status") == "ok" and health.get("service") == "governance"
    and health.get("port") == port and health.get("runtime_loaded_version") == anchor
    and source_identity_ok
    and health.get("runtime_stale") is False
    and database_identity_ok
    and plane_identity_ok
): raise SystemExit("Promotion blocked: stable health is not exact anchor")
print(int(health.get("pid") or 0))
PY
)"
COMMON_GIT_DIR="$(git -C "$STABLE_WORKTREE" rev-parse --git-common-dir)"
if [[ "$COMMON_GIT_DIR" = /* ]]; then
    REPO_ROOT="$(cd "$COMMON_GIT_DIR/.." && pwd)"
else
    REPO_ROOT="$(cd "$STABLE_WORKTREE/$COMMON_GIT_DIR/.." && pwd)"
fi
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "Promotion blocked: repository Python runtime unavailable at $PYTHON_BIN." >&2; exit 1
fi
if [ "$OLD_PID" -le 1 ]; then
    echo "Promotion blocked: stable process PID is not safely identifiable." >&2; exit 1
fi

if [ "$MODE" = "prepare" ]; then
    python3 - "$ACTIVATION_PLAN" "$DRY_RUN" "$MANIFEST" "$PRECHECK_RECEIPT" "$STABLE_WORKTREE" "$PWD" "$CURRENT_STABLE" "$CANDIDATE_COMMIT" "$STABLE_DATABASE_IDENTITY" "$LIVE_DB" "$OLD_PID" "$PYTHON_BIN" "$VERIFIER_SHA256" "$STABLE_PORT" <<'PY'
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

(
    plan_raw,
    dry_raw,
    manifest_raw,
    precheck_raw,
    stable_root_raw,
    dev_root_raw,
    stable,
    candidate,
    database_identity_raw,
    database_raw,
    old_pid_raw,
    python_raw,
    verifier_hash,
    port_raw,
) = sys.argv[1:]


def fail(code, message):
    raise SystemExit(f"Promotion blocked [{code}]: {message}")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha(value):
    raw = value if isinstance(value, bytes) else canonical(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def run(args, cwd, code, *, text=False):
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=text, check=False
    )
    if result.returncode:
        fail(code, "exact preparation command failed")
    return result.stdout


plan_input = Path(plan_raw).expanduser()
if not plan_input.is_absolute():
    fail("activation_plan_path_invalid", "activation plan path must be absolute")
if plan_input.is_symlink():
    fail("activation_plan_path_invalid", "activation plan path cannot be a symlink")
parent = plan_input.parent.resolve(strict=True)
plan_path = parent / plan_input.name
stable_root = Path(stable_root_raw).resolve(strict=True)
dev_root = Path(dev_root_raw).resolve(strict=True)
for repo in (stable_root, dev_root):
    try:
        plan_path.relative_to(repo)
    except ValueError:
        pass
    else:
        fail("activation_plan_inside_repo", "activation plan must be outside every worktree")
if plan_path.exists():
    fail("activation_plan_exists", "activation plan already exists; overwrite is forbidden")
manifest = json.loads(Path(manifest_raw).read_text(encoding="utf-8"))
precheck = json.loads(precheck_raw)
database_identity = json.loads(database_identity_raw)
if manifest.get("schema_version") != "ac_stable_promotion_manifest.v2":
    fail("activation_plan_manifest_invalid", "prepare requires an exact v2 manifest")
if precheck.get("schema_version") != "ac_stable_promotion_precheck_receipt.v2":
    fail("activation_plan_precheck_invalid", "prepare requires an exact v2 precheck receipt")
if precheck.get("receipt_hash") != sha(
    {key: value for key, value in precheck.items() if key != "receipt_hash"}
):
    fail("activation_plan_precheck_invalid", "precheck receipt digest mismatch")
port = int(port_raw)
old_pid = int(old_pid_raw)
if port != 40000 or old_pid <= 1:
    fail("activation_plan_process_invalid", "stable process identity is unsafe")
birth = run(
    ["ps", "-p", str(old_pid), "-o", "lstart="],
    dev_root,
    "activation_plan_process_invalid",
    text=True,
).strip()
command = run(
    ["ps", "-p", str(old_pid), "-o", "command="],
    dev_root,
    "activation_plan_process_invalid",
    text=True,
).strip()
listeners = run(
    ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
    dev_root,
    "activation_plan_listener_invalid",
    text=True,
).split()
if listeners != [str(old_pid)] or not birth or not command:
    fail("activation_plan_process_invalid", "stable PID/birth/command/listener is not exact")
old_process = {"pid": old_pid, "birth": birth, "command": command}
tree_sha = run(
    ["git", "rev-parse", f"{candidate}^{{tree}}"],
    dev_root,
    "activation_plan_tree_invalid",
    text=True,
).strip()
if not re.fullmatch(r"[0-9a-f]{40}", tree_sha):
    fail("activation_plan_tree_invalid", "candidate tree is malformed")
stable_runtime_source_sha256 = sha(
    run(
        ["git", "show", f"{stable}:agent/governance/server.py"],
        dev_root,
        "activation_plan_stable_source_invalid",
    )
)
candidate_runtime_source_sha256 = sha(
    run(
        ["git", "show", f"{candidate}:agent/governance/server.py"],
        dev_root,
        "activation_plan_candidate_source_invalid",
    )
)
if not (
    manifest.get("candidate_tree_sha") == tree_sha
    and manifest.get("stable_runtime_source_sha256")
    == stable_runtime_source_sha256
    and manifest.get("candidate_runtime_source_sha256")
    == candidate_runtime_source_sha256
):
    fail(
        "activation_plan_runtime_source_invalid",
        "manifest tree/runtime source authority differs from Git",
    )
patch = run(
    [
        "git", "diff", "--no-ext-diff", "--no-textconv", "--binary",
        "--full-index", "-M", f"{stable}..{candidate}", "--", ".",
    ],
    dev_root,
    "activation_plan_patch_invalid",
)
if not patch or sha(patch) != manifest["promotion_delta"]["diff_sha256"]:
    fail("activation_plan_patch_invalid", "prepared full-index patch differs from promotion delta")
database_relative_path = (
    "shared-volume/codex-tasks/state/governance/aming-claw/governance.db"
)
database = Path(database_raw).absolute()
if database != stable_root / database_relative_path:
    fail("activation_plan_database_invalid", "stable database path is not canonical")
cursor = stable_root
for part in Path(database_relative_path).parts:
    cursor = cursor / part
    if cursor.is_symlink():
        fail("activation_plan_database_invalid", "stable database parent is a symlink")
metadata = database.stat(follow_symlinks=False)
if (
    database.is_symlink()
    or not stat.S_ISREG(metadata.st_mode)
    or database.resolve(strict=True) != database
    or int(metadata.st_dev) != int(database_identity["device"])
    or int(metadata.st_ino) != int(database_identity["inode"])
):
    fail("activation_plan_database_invalid", "stable database identity changed")
database_uri = database.as_uri() + "?mode=ro"
import sqlite3
conn = sqlite3.connect(database_uri, uri=True)
try:
    rows = conn.execute(
        "SELECT project_id,snapshot_id,commit_sha,is_active FROM graph_snapshots "
        "WHERE project_id='aming-claw' ORDER BY snapshot_id"
    ).fetchall()
finally:
    conn.close()
graph_hash = sha([list(row) for row in rows])
python_bin = str(Path(python_raw).resolve(strict=True))
shared = str(stable_root / "shared-volume")
old_launch = [
    python_bin, "-m", "agent.cli", "start", "--workspace",
    str(stable_root), "--port", "40000",
]
candidate_launch = [
    python_bin, "-m", "agent.cli", "start", "--runtime-plane", "stable",
    "--port", "40000", "--stable-anchor-commit", candidate,
    "--workspace", str(stable_root), "--shared-volume-path", shared,
]
launch_environment = {
    "PYTHONPATH": str(stable_root),
    "SHARED_VOLUME_PATH": shared,
}
runtime_process_executable = command.split(" ", 1)[0]
expected_old_process_command = " ".join(
    [runtime_process_executable, *old_launch[1:]]
)
if (
    not Path(runtime_process_executable).is_absolute()
    or command != expected_old_process_command
):
    fail(
        "activation_plan_process_invalid",
        "stable process command differs from the hard-coded old launch argv",
    )
lane_commands = [
    [python_bin, "-m", "pytest", "-q", "agent/tests/test_graph_governance_api.py", "-k", selector]
    for selector in (
        "promotion_rollback", "direct_main", "mf_parallel", "mf_batch_parallel"
    )
]
journal_path = plan_path.with_suffix(plan_path.suffix + ".journal")
lock_path = plan_path.with_suffix(plan_path.suffix + ".lock")
completion = {
    "schema_version": "ac_stable_promotion_completion.v2",
    "project_id": "aming-claw",
    "backlog_id": manifest["backlog_id"],
    "contract_execution_id": manifest["contract_execution_id"],
    "candidate_commit": candidate,
    "candidate_tree_sha": tree_sha,
    "stable_runtime_source_sha256": stable_runtime_source_sha256,
    "candidate_runtime_source_sha256": candidate_runtime_source_sha256,
    "previous_stable_commit": stable,
    "promotion_intent_sha256": manifest["promotion_intent_sha256"],
    "qa_candidate_intent_sha256": manifest[
        "qa_candidate_intent_sha256"
    ],
    "promotion_manifest_sha256": manifest["promotion_manifest_sha256"],
    "precheck_receipt_hash": precheck["receipt_hash"],
    "verifier_sha256": verifier_hash,
    "precheck_receipt": precheck,
    "promotion_manifest": manifest,
    "previous_promotion_receipt_hash": precheck.get("previous_promotion_receipt_hash", ""),
    "implementation_delta": manifest["implementation_delta"],
    "promotion_delta": manifest["promotion_delta"],
    "diff_sha256": manifest["promotion_delta"]["diff_sha256"],
    "file_fence": manifest["promotion_delta"]["file_fence"],
    "rollback_authority_hash": manifest["rollback_authority_hash"],
    "custody_authority": manifest["custody_authority"],
    "deploy": manifest["deploy"],
    "stable_database_identity": database_identity,
    "operator_approval_ref": precheck["operator_approval_ref"],
}
core = {
    "schema_version": "ac_stable_activation_plan.v2",
    "plan_id": "ac-activation-" + candidate[:16],
    "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "project_id": "aming-claw",
    "backlog_id": manifest["backlog_id"],
    "contract_execution_id": manifest["contract_execution_id"],
    "stable_branch": "codex/direct-no-pass-post-reconcile-r2",
    "dev_branch": "codex/ac-dev",
    "stable_worktree": str(stable_root),
    "dev_worktree": str(dev_root),
    "stable_anchor_commit": stable,
    "candidate_commit": candidate,
    "candidate_tree_sha": tree_sha,
    "stable_runtime_source_sha256": stable_runtime_source_sha256,
    "candidate_runtime_source_sha256": candidate_runtime_source_sha256,
    "implementation_delta": manifest["implementation_delta"],
    "promotion_delta": manifest["promotion_delta"],
    "qa_candidate_intent_sha256": manifest[
        "qa_candidate_intent_sha256"
    ],
    "custody_authority": manifest["custody_authority"],
    "manifest": manifest,
    "manifest_sha256": sha(manifest),
    "promotion_intent_sha256": manifest["promotion_intent_sha256"],
    "promotion_manifest_sha256": manifest["promotion_manifest_sha256"],
    "verifier_sha256": verifier_hash,
    "precheck_receipt": precheck,
    "precheck_receipt_hash": precheck["receipt_hash"],
    "stable_database_path": str(database),
    "stable_database_identity": database_identity,
    "stable_database_relative_path": database_relative_path,
    "stable_database_path_sha256": sha(database_relative_path.encode("utf-8")),
    "graph_identity_hash": graph_hash,
    "old_process": old_process,
    "old_launch_spec": old_launch,
    "old_launch_environment": launch_environment,
    "candidate_launch_spec": candidate_launch,
    "candidate_launch_environment": launch_environment,
    "runtime_process_executable": runtime_process_executable,
    "stable_port": port,
    "bind_host": "127.0.0.1",
    "lane_commands": lane_commands,
    "forward_patch_b64": base64.b64encode(patch).decode("ascii"),
    "forward_patch_sha256": sha(patch),
    "reverse_apply_patch_sha256": sha(patch),
    "journal_path": str(journal_path),
    "lock_path": str(lock_path),
    "completion_body_template": completion,
    "activation_policy": manifest["activation_policy"],
}
plan = {**core, "plan_hash": sha(core)}
payload = (canonical(plan) + "\n").encode("utf-8")
if dry_raw == "true":
    print(canonical({
        "ok": True,
        "dry_run": True,
        "writes_performed": False,
        "plan_hash": plan["plan_hash"],
        "precheck_receipt": precheck,
    }))
    raise SystemExit(0)
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
fd = os.open(plan_path, flags, 0o600)
try:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            fail("activation_plan_write_failed", "activation plan write was incomplete")
        offset += written
    os.fsync(fd)
finally:
    os.close(fd)
os.chmod(plan_path, 0o600, follow_symlinks=False)
parent_fd = os.open(parent, os.O_RDONLY)
try:
    os.fsync(parent_fd)
finally:
    os.close(parent_fd)
print(canonical({
    "ok": True,
    "prepared": True,
    "writes_performed": True,
    "stable_mutation_performed": False,
    "plan_hash": plan["plan_hash"],
    "activation_plan": str(plan_path),
}))
PY
    exit $?
fi

if [ "$DRY_RUN" = "true" ]; then
    echo "Promotion preflight passed; receipt=$PRECHECK_RECEIPT_HASH; no mutation performed."
    exit 0
fi

# Re-project every durable gate at the last possible pre-mutation point.  This
# catches signoff expiry, nonce replay, DB replacement, QA replay, or promotion
# chain changes that occur while source/health checks are running.
FINAL_PRECHECK_RECEIPT="$(promotion_precheck)"
if [ "$FINAL_PRECHECK_RECEIPT" != "$PRECHECK_RECEIPT" ]; then
    echo "Promotion blocked: durable promotion evidence changed after precheck." >&2
    exit 1
fi

# Final compare-and-swap guard after every fallible precheck and immediately
# before the first stable Git mutation. A concurrent promotion must restart
# from a fresh manifest; it is never merged on top implicitly.
PRE_MERGE_STABLE="$(git -C "$STABLE_WORKTREE" rev-parse HEAD)"
if [ "$PRE_MERGE_STABLE" != "$CURRENT_STABLE" ] \
    || [ "$(git -C "$STABLE_WORKTREE" branch --show-current)" != "$STABLE_BRANCH" ] \
    || [ -n "$(git -C "$STABLE_WORKTREE" status --porcelain)" ]; then
    echo "Promotion blocked: stable HEAD/branch/worktree changed after precheck." >&2
    exit 1
fi

# No rebase/fallback merge/branch deletion/DB copy. The stable ref advances to
# the exact already-reviewed commit, never to a synthetic merge commit.
git -C "$STABLE_WORKTREE" merge --ff-only "$CANDIDATE_COMMIT"
PROMOTED_COMMIT="$(git -C "$STABLE_WORKTREE" rev-parse HEAD)"
if [ "$PROMOTED_COMMIT" != "$CANDIDATE_COMMIT" ]; then
    echo "Promotion failed: stable HEAD is not exact candidate." >&2; exit 1
fi

kill -TERM "$OLD_PID"
for _attempt in $(seq 1 50); do
    if ! kill -0 "$OLD_PID" 2>/dev/null; then break; fi
    sleep 0.2
done
if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Promotion failed: old stable process did not stop; no force-kill attempted." >&2; exit 1
fi

RUNTIME_LOG="${TMPDIR:-/tmp}/aming-claw-stable-${PROMOTED_COMMIT}.log"
(
    cd "$STABLE_WORKTREE"
    PYTHONPATH="$STABLE_WORKTREE" nohup "$PYTHON_BIN" -m agent.cli start \
        --runtime-plane stable --port "$STABLE_PORT" \
        --stable-anchor-commit "$PROMOTED_COMMIT" \
        --workspace "$STABLE_WORKTREE" \
        --shared-volume-path "$CANONICAL_SHARED_VOLUME" >"$RUNTIME_LOG" 2>&1 &
)

POST_HEALTH=""
for _attempt in $(seq 1 50); do
    POST_HEALTH="$(curl -sf "http://127.0.0.1:${STABLE_PORT}/api/health" 2>/dev/null || true)"
    if [ -n "$POST_HEALTH" ]; then break; fi
    sleep 0.2
done
python3 -c '
import json,sys
commit,branch,port,db_raw=sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4]; h=json.load(sys.stdin); i=h.get("runtime_plane_identity") or {}; db=json.loads(db_raw)
ok=(h.get("status")=="ok" and h.get("service")=="governance" and h.get("port")==port and h.get("runtime_plane")=="stable" and h.get("runtime_loaded_version")==commit and h.get("runtime_stale") is False and i.get("status")=="ready" and i.get("branch")==branch and i.get("commit")==commit and i.get("stable_anchor_commit")==commit and i.get("stable_database_identity")==db)
raise SystemExit(0 if ok else "Promotion failed: deployed identity is not exact candidate")
' "$PROMOTED_COMMIT" "$STABLE_BRANCH" "$STABLE_PORT" "$STABLE_DATABASE_IDENTITY" <<<"$POST_HEALTH"

COMPLETION="$(python3 - "$MANIFEST" "$PRECHECK_RECEIPT" "$POST_HEALTH" "$VERIFIER_SHA256" <<'PY'
import json, os, sys, urllib.request
m=json.load(open(sys.argv[1], encoding="utf-8")); pre=json.loads(sys.argv[2]); health=json.loads(sys.argv[3])
body={
 "project_id":"aming-claw", "backlog_id":m["backlog_id"],
 "contract_execution_id":m["contract_execution_id"], "candidate_commit":m["candidate_commit"],
 "previous_stable_commit":m["stable_anchor_commit"], "promotion_intent_sha256":m["promotion_intent_sha256"],
 "promotion_manifest_sha256":m["promotion_manifest_sha256"],
 "precheck_receipt_hash":pre["receipt_hash"], "verifier_sha256":sys.argv[4],
 "precheck_receipt":pre, "promotion_manifest":m,
 "previous_promotion_receipt_hash":pre.get("previous_promotion_receipt_hash", ""),
 "diff_sha256":m["diff_sha256"], "file_fence":m["file_fence"],
 "deploy":m["deploy"],
 "stable_database_identity":m["stable_database_identity"],
 "operator_approval_ref":pre["operator_approval_ref"],
 "health_identity":{"runtime_loaded_version":health["runtime_loaded_version"],"runtime_plane_identity":health["runtime_plane_identity"],"runtime_stale":health["runtime_stale"]},
}
req=urllib.request.Request("http://127.0.0.1:40000/api/projects/aming-claw/ac-stable-promotion/complete",data=json.dumps(body,sort_keys=True).encode(),headers={"Content-Type":"application/json","X-Gov-Token":os.environ["GOV_COORDINATOR_TOKEN"]},method="POST")
with urllib.request.urlopen(req,timeout=10) as response: result=json.load(response)
if not result.get("ok") or not result.get("promotion_receipt_hash"): raise SystemExit("completion receipt not persisted")
print(json.dumps(result,sort_keys=True))
PY
)"
echo "AC stable promotion complete: $COMPLETION"
