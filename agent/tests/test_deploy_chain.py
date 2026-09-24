"""Tests for deploy_chain.restart_local_governance (B7 fix).

AC8: Tests cover:
  (a) process crashes immediately after start
  (b) health check retries succeed on 3rd attempt
  (c) port not released in time
  (d) stderr content included in failure summary
"""
import json
import copy
import hashlib
import ast
import base64
import logging
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_netstat_no_listener():
    """netstat output with no LISTENING on port 40000."""
    r = MagicMock()
    r.stdout = "  TCP    0.0.0.0:445    0.0.0.0:0    LISTENING    4\n"
    r.returncode = 0
    return r


def _mock_netstat_with_listener(pid=12345):
    """netstat output with governance LISTENING on port 40000."""
    r = MagicMock()
    r.stdout = f"  TCP    0.0.0.0:40000    0.0.0.0:0    LISTENING    {pid}\n"
    r.returncode = 0
    return r


class _FakeProc:
    """Fake subprocess.Popen result with controllable poll() behavior."""

    def __init__(self, pid=99999, crash_after=None, exit_code=1):
        self.pid = pid
        self.returncode = None
        self._crash_after = crash_after  # number of poll() calls before crash
        self._poll_count = 0
        self._exit_code = exit_code

    def poll(self):
        self._poll_count += 1
        if self._crash_after is not None and self._poll_count >= self._crash_after:
            self.returncode = self._exit_code
            return self._exit_code
        return None


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Tests: _is_port_free
# ---------------------------------------------------------------------------

class TestIsPortFree:
    def test_free_port(self):
        from agent.deploy_chain import _is_port_free
        # Use a high ephemeral port that should be free
        assert _is_port_free(59123) is True

    def test_occupied_port(self):
        from agent.deploy_chain import _is_port_free
        # Bind a port, then check it's not free
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 59124))
            assert _is_port_free(59124) is False


# ---------------------------------------------------------------------------
# Tests: _read_stderr_log
# ---------------------------------------------------------------------------

class TestReadStderrLog:
    def test_reads_existing_file(self, tmp_path):
        from agent.deploy_chain import _read_stderr_log
        f = tmp_path / "test.log"
        f.write_text("ImportError: no module named 'missing'\n")
        result = _read_stderr_log(str(f))
        assert "ImportError" in result

    def test_truncates_large_file(self, tmp_path):
        from agent.deploy_chain import _read_stderr_log
        f = tmp_path / "big.log"
        f.write_text("X" * 5000)
        result = _read_stderr_log(str(f), max_bytes=100)
        assert "truncated" in result
        assert len(result) < 200

    def test_missing_file_returns_empty(self):
        from agent.deploy_chain import _read_stderr_log
        assert _read_stderr_log("/nonexistent/path.log") == ""


# ---------------------------------------------------------------------------
# Tests: restart_local_governance (AC8)
# ---------------------------------------------------------------------------

class TestRestartLocalGovernance:
    """B7 fix: restart_local_governance with stderr capture, retry, port check."""

    @patch("time.sleep")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("agent.deploy_chain._is_port_free", return_value=True)
    @patch("agent.deploy_chain._read_stderr_log", return_value="RuntimeError: port in use")
    def test_ac8a_immediate_crash_reports_stderr(
        self, mock_read, mock_port, mock_run, mock_popen, mock_sleep
    ):
        """(a) Process crashes immediately — stderr content in summary."""
        from agent.deploy_chain import restart_local_governance

        mock_run.return_value = _mock_netstat_no_listener()
        crash_proc = _FakeProc(pid=55555, crash_after=1, exit_code=1)
        mock_popen.return_value = crash_proc

        ok, summary = restart_local_governance(port=40000)

        assert ok is False
        assert "crashed immediately" in summary
        assert "RuntimeError: port in use" in summary

    @patch("time.sleep")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("agent.deploy_chain._is_port_free", return_value=True)
    def test_ac8b_health_retry_succeeds_on_third(
        self, mock_port, mock_run, mock_popen, mock_sleep
    ):
        """(b) Health check fails twice, succeeds on 3rd attempt."""
        from agent.deploy_chain import restart_local_governance

        mock_run.return_value = _mock_netstat_no_listener()
        mock_popen.return_value = _FakeProc(pid=55556)  # never crashes

        # Mock requests.get: first 2 calls raise, 3rd succeeds
        call_count = [0]
        import requests as _req

        def fake_get(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise _req.ConnectionError("refused")
            return _FakeResponse(200)

        with patch("requests.get", side_effect=fake_get):
            ok, summary = restart_local_governance(port=40000)

        assert ok is True
        assert "governance OK" in summary
        assert "attempt 3" in summary

    @patch("time.sleep")
    @patch("subprocess.run")
    @patch("agent.deploy_chain._is_port_free", return_value=False)
    def test_ac8c_port_not_released(self, mock_port, mock_run, mock_sleep):
        """(c) Port not released — warning in summary but proceeds."""
        from agent.deploy_chain import restart_local_governance

        mock_run.return_value = _mock_netstat_with_listener(pid=11111)

        with patch("subprocess.Popen") as mock_popen, \
             patch("requests.get") as mock_get:
            mock_popen.return_value = _FakeProc(pid=22222)
            mock_get.return_value = _FakeResponse(200)

            ok, summary = restart_local_governance(port=40000)

        assert "still held after 5s" in summary

    @patch("time.sleep")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("agent.deploy_chain._is_port_free", return_value=True)
    @patch("agent.deploy_chain._read_stderr_log", return_value="Address already in use")
    def test_ac8d_stderr_in_failure_summary(
        self, mock_read, mock_port, mock_run, mock_popen, mock_sleep
    ):
        """(d) All health checks fail — stderr content included in summary."""
        from agent.deploy_chain import restart_local_governance
        import requests as _req

        mock_run.return_value = _mock_netstat_no_listener()
        mock_popen.return_value = _FakeProc(pid=33333)  # stays alive

        with patch("requests.get", side_effect=_req.ConnectionError("refused")):
            ok, summary = restart_local_governance(port=40000)

        assert ok is False
        assert "Address already in use" in summary
        assert "unreachable after 4 attempts" in summary


# ---------------------------------------------------------------------------
# Tests: restart_executor signal write + logging
# ---------------------------------------------------------------------------

class TestRestartExecutorWritesSignal:
    """R2: restart_executor writes valid signal file and logs."""

    def test_restart_executor_writes_signal(self, tmp_path, caplog, monkeypatch):
        """AC3+AC4: signal file has correct keys and log line emitted."""
        from agent.deploy_chain import restart_executor

        from agent import manager_http_server
        (tmp_path / "shared-volume").mkdir()
        monkeypatch.setattr(manager_http_server, "_project_root", lambda: tmp_path)

        with caplog.at_level(logging.INFO, logger="agent.deploy_chain"):
            result = restart_executor("proj")

        assert result is True

        signal_file = tmp_path / "shared-volume" / "codex-tasks" / "state" / "manager_signal.json"
        assert signal_file.exists()

        data = json.loads(signal_file.read_text())
        assert data["action"] == "restart"
        assert "requested_at" in data

        assert any("wrote restart signal" in rec.message for rec in caplog.records)


class TestRestartLocalGovernanceLogging:
    """AC7: Verify log.warning is called on failure."""

    @patch("time.sleep")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("agent.deploy_chain._is_port_free", return_value=True)
    @patch("agent.deploy_chain._read_stderr_log", return_value="crash!")
    @patch("agent.deploy_chain.log")
    def test_ac7_log_warning_on_crash(
        self, mock_log, mock_read, mock_port, mock_run, mock_popen, mock_sleep
    ):
        from agent.deploy_chain import restart_local_governance

        mock_run.return_value = _mock_netstat_no_listener()
        mock_popen.return_value = _FakeProc(pid=44444, crash_after=1)

        restart_local_governance(port=40000)

        warning_calls = [c for c in mock_log.warning.call_args_list
                         if "crashed immediately" in str(c)]
        assert len(warning_calls) >= 1

    @patch("time.sleep")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("agent.deploy_chain._is_port_free", return_value=True)
    @patch("agent.deploy_chain._read_stderr_log", return_value="")
    @patch("agent.deploy_chain.log")
    def test_ac7_log_warning_on_health_failure(
        self, mock_log, mock_read, mock_port, mock_run, mock_popen, mock_sleep
    ):
        from agent.deploy_chain import restart_local_governance
        import requests as _req

        mock_run.return_value = _mock_netstat_no_listener()
        mock_popen.return_value = _FakeProc(pid=55555)

        with patch("requests.get", side_effect=_req.ConnectionError("refused")):
            restart_local_governance(port=40000)

        warning_calls = [c for c in mock_log.warning.call_args_list
                         if "health check failed" in str(c)]
        assert len(warning_calls) >= 1


class TestExplicitACPromotionScript:
    @staticmethod
    def _script() -> Path:
        return Path(__file__).resolve().parents[2] / "scripts" / "merge-and-deploy.sh"

    def test_script_is_valid_shell_and_fails_closed_without_manifest(self):
        script = self._script()
        syntax = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert syntax.returncode == 0, syntax.stderr

        result = subprocess.run(
            [str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "--promotion-manifest must name an existing file" in result.stderr

    def test_script_rejects_legacy_branch_shortcut_before_git_mutation(self):
        result = subprocess.run(
            [str(self._script()), "dev/legacy-shortcut", "--dry-run"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "Refusing legacy merge-and-deploy argument" in result.stderr

    def test_script_requires_all_explicit_promotion_gates(self):
        text = self._script().read_text(encoding="utf-8")
        assert "ac_stable_promotion_manifest.v1" in text
        assert "qa_verdict" in text
        assert "promotion_gate_results" in text
        assert "branch_service" in text
        assert "mf_batch_parallel" in text
        assert "operator_signoff" in text
        assert "release_operator_head_queue_events" in text
        assert "nonce replay/ambiguity" in text
        assert 'STABLE_BRANCH="main"' in text
        assert 'manifest.get("stable_branch")' in text
        assert 'branch") == expected' in text
        assert "contract_execution_id" in text
        assert "promotion_intent_sha256" in text
        assert "promotion_manifest_sha256" in text
        assert "timeline_event_id" in text
        assert "PRAGMA query_only=ON" in text
        assert '"?mode=ro"' in text
        assert "pass_synthesized" in text
        assert "previous_promotion_receipt_hash" in text
        assert "ac.stable_promotion_completed" in text
        assert "merge --ff-only" in text
        assert "runtime_loaded_version" in text
        assert "runtime_plane_identity" in text
        assert "runtime_stale" in text
        assert "/ac-stable-promotion/complete" in text
        assert 'refs/heads/main' in text
        assert 'MAIN_WORKTREE' not in text
        assert "git merge-base --is-ancestor" in text
        assert "PRE_MERGE_STABLE" in text
        assert "changed after precheck" in text
        assert "merge --no-ff" not in text
        assert "git rebase" not in text
        assert "--skip-deploy" not in text
        assert "git branch -d" not in text
        assert "governance-dev" not in text
        assert "docker compose" not in text

    def test_script_enforces_source_only_promotion_delta(self):
        text = self._script().read_text(encoding="utf-8")

        assert "SOURCE_ONLY_PROMOTION_FENCE" in text
        assert "source_only_git_delta_verified" in text
        assert "database/runtime bytes are outside the source-only Git fence" in text
        for forbidden in (
            '".db"',
            '".sqlite"',
            '"-wal"',
            '"-shm"',
            '"shared-volume/"',
            '"graph-snapshots/"',
            '"graph-index/"',
        ):
            assert forbidden in text
        assert "git diff --name-only -z" in text

    def test_every_quoted_python_heredoc_parses(self):
        lines = self._script().read_text(encoding="utf-8").splitlines()
        snippets = []
        index = 0
        while index < len(lines):
            if "<<'PY'" not in lines[index]:
                index += 1
                continue
            start = index + 1
            end = start
            while end < len(lines) and lines[end] != "PY":
                end += 1
            assert end < len(lines), (
                f"unterminated Python heredoc at line {index + 1}"
            )
            snippets.append((index + 2, "\n".join(lines[start:end]) + "\n"))
            index = end + 1

        assert snippets
        for line_number, source in snippets:
            ast.parse(
                source,
                filename=(
                    "merge-and-deploy.sh:python-heredoc:"
                    f"{line_number}"
                ),
            )

    @classmethod
    def _activation_runtime(cls) -> dict:
        lines = cls._script().read_text(encoding="utf-8").splitlines()
        start = next(
            index + 1
            for index, line in enumerate(lines)
            if "python3 - \"$MODE\" \"$ACTIVATION_PLAN\"" in line
        )
        end = next(index for index in range(start, len(lines)) if lines[index] == "PY")
        namespace = {
            "__name__": "ac_activation_isolated_test",
            "__file__": str(cls._script()),
        }
        exec(compile("\n".join(lines[start:end]) + "\n", str(cls._script()), "exec"), namespace)
        return namespace

    @staticmethod
    def _activation_plan_fixture(tmp_path: Path, runtime: dict) -> tuple[dict, Path, bytes]:
        stable = tmp_path / "stable-worktree"
        dev = tmp_path / "dev-worktree"
        stable.mkdir()
        dev.mkdir()
        database_relative_path = (
            "shared-volume/codex-tasks/state/governance/aming-claw/governance.db"
        )
        database = stable / database_relative_path
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE historical_control_fixture(value)")
        metadata = database.stat()
        python_bin = str(Path(sys.executable).resolve())
        anchor = "a25838f15f949ac434cf78e03f20760e82ff81f0"
        candidate = "d" * 40
        patch_bytes = b"exact-full-index-binary-patch"
        implementation_delta = {"delta_hash": "sha256:" + "1" * 64}
        promotion_delta = {
            "delta_hash": "sha256:" + "2" * 64,
            "diff_sha256": runtime["sha"](patch_bytes),
        }
        database_identity = {
            "schema_version": "ac_stable_database_identity.v1",
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "stable_relative_path_sha256": runtime["sha"](
                database_relative_path.encode()
            ),
        }
        activation_policy = {
            "schema_version": "ac_stable_activation_policy.v2",
            "prepare_required": True,
            "activation_plan_required": True,
            "automatic_activation": False,
            "rollback_required_after_first_mutation": True,
        }
        custody_core = {
            "schema_version": "ac_promotion_rollback_custody_authority.v1",
            "contract_execution_id": "cex-direct-main-60df8f3e0c9a1fbce338",
            "contract_id": "operator_supervised_direct_main",
            "implementation_route_ref": "rtok-implementation",
            "implementation_binding_hash": "sha256:" + "a" * 64,
            "implementation_line_id": "observer_implementation",
            "implementation_line_hash": "sha256:" + "b" * 64,
            "implementation_runtime_revision": 4,
            "implementation_runtime_state_hash": "sha256:" + "c" * 64,
            "implementation_commit_sha": candidate,
            "candidate_authority_hash": "sha256:" + "2" * 64,
            "promotion_route_token_ref": "rtok-promotion-prepare",
            "promotion_route_authority_hash": "sha256:" + "d" * 64,
            "promotion_route_identity": {
                "route_id": "route-promotion",
                "route_context_hash": "sha256:" + "e" * 64,
                "prompt_contract_id": "prompt-promotion",
                "prompt_contract_hash": "sha256:" + "f" * 64,
                "visible_injection_manifest_hash": "sha256:" + "1" * 64,
                "route_token_ref": "rtok-promotion-prepare",
            },
            "writes_performed": False,
        }
        custody = {
            **custody_core,
            "authority_hash": runtime["sha"](custody_core),
        }
        qa_candidate_intent_sha256 = "sha256:" + "3" * 64
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        manifest = {
            "schema_version": "ac_stable_promotion_manifest.v2",
            "promotion_intent_sha256": "sha256:" + "4" * 64,
            "promotion_manifest_sha256": "sha256:" + "5" * 64,
            "implementation_delta": implementation_delta,
            "promotion_delta": promotion_delta,
            "qa_candidate_intent_sha256": qa_candidate_intent_sha256,
            "candidate_tree_sha": "e" * 40,
            "stable_runtime_source_sha256": "sha256:" + "7" * 64,
            "candidate_runtime_source_sha256": "sha256:" + "8" * 64,
            "activation_policy": activation_policy,
            "custody_authority": custody,
            "gates": {"operator_signoff": {"expires_at": expires}},
        }
        precheck_core = {
            "schema_version": "ac_stable_promotion_precheck_receipt.v2",
            "candidate_commit": candidate,
            "candidate_tree_sha": "e" * 40,
            "stable_runtime_source_sha256": "sha256:" + "7" * 64,
            "candidate_runtime_source_sha256": "sha256:" + "8" * 64,
            "qa_candidate_intent_sha256": qa_candidate_intent_sha256,
            "custody_authority": custody,
            "stable_anchor_commit": anchor,
            "stable_database_identity": database_identity,
        }
        precheck = {
            **precheck_core,
            "receipt_hash": runtime["sha"](precheck_core),
        }
        shared = str(stable / "shared-volume")
        old_launch = [
            python_bin, "-m", "agent.cli", "start", "--workspace",
            str(stable), "--port", "40000",
        ]
        candidate_launch = [
            python_bin, "-m", "agent.cli", "start", "--runtime-plane", "stable",
            "--port", "40000", "--stable-anchor-commit", candidate,
            "--workspace", str(stable), "--shared-volume-path", shared,
        ]
        launch_environment = {
            "PYTHONPATH": str(stable),
            "SHARED_VOLUME_PATH": shared,
        }
        lane_commands = [
            [
                python_bin, "-m", "pytest", "-q",
                "agent/tests/test_graph_governance_api.py", "-k", selector,
            ]
            for selector in (
                "promotion_rollback", "direct_main", "mf_parallel",
                "mf_batch_parallel",
            )
        ]
        plan_path = tmp_path / "activation.json"
        core = {
            "schema_version": "ac_stable_activation_plan.v2",
            "plan_id": "ac-activation-fixture",
            "created_at": "2026-08-28T00:00:00Z",
            "project_id": "aming-claw",
            "backlog_id": "AC-PROMOTION-ACTIVATION-ROLLBACK-RESTART-P0-20260827",
            "contract_execution_id": "cex-direct-main-60df8f3e0c9a1fbce338",
            "stable_branch": "codex/direct-no-pass-post-reconcile-r2",
            "dev_branch": "codex/ac-dev",
            "stable_worktree": str(stable),
            "dev_worktree": str(dev),
            "stable_anchor_commit": anchor,
            "candidate_commit": candidate,
            "candidate_tree_sha": "e" * 40,
            "stable_runtime_source_sha256": "sha256:" + "7" * 64,
            "candidate_runtime_source_sha256": "sha256:" + "8" * 64,
            "qa_candidate_intent_sha256": qa_candidate_intent_sha256,
            "custody_authority": custody,
            "implementation_delta": implementation_delta,
            "promotion_delta": promotion_delta,
            "manifest": manifest,
            "manifest_sha256": runtime["sha"](manifest),
            "promotion_intent_sha256": manifest["promotion_intent_sha256"],
            "promotion_manifest_sha256": manifest["promotion_manifest_sha256"],
            "verifier_sha256": runtime["sha"](TestExplicitACPromotionScript._script().read_bytes()),
            "precheck_receipt": precheck,
            "precheck_receipt_hash": precheck["receipt_hash"],
            "stable_database_path": str(database),
            "stable_database_identity": database_identity,
            "stable_database_relative_path": database_relative_path,
            "stable_database_path_sha256": runtime["sha"](
                database_relative_path.encode()
            ),
            "graph_identity_hash": "sha256:" + "6" * 64,
            "old_process": {
                "pid": 41001,
                "birth": "birth-old",
                "command": " ".join(old_launch),
            },
            "old_launch_spec": old_launch,
            "old_launch_environment": launch_environment,
            "candidate_launch_spec": candidate_launch,
            "candidate_launch_environment": launch_environment,
            "runtime_process_executable": python_bin,
            "stable_port": 40000,
            "bind_host": "127.0.0.1",
            "lane_commands": lane_commands,
            "forward_patch_b64": base64.b64encode(patch_bytes).decode("ascii"),
            "forward_patch_sha256": runtime["sha"](patch_bytes),
            "reverse_apply_patch_sha256": runtime["sha"](patch_bytes),
            "journal_path": str(tmp_path / "activation.journal"),
            "lock_path": str(tmp_path / "activation.lock"),
            "completion_body_template": {},
            "activation_policy": activation_policy,
        }
        plan = {**core, "plan_hash": runtime["sha"](core)}
        plan_path.write_text(runtime["canonical"](plan) + "\n", encoding="utf-8")
        plan_path.chmod(0o600)
        return plan, plan_path, patch_bytes

    def test_activation_plan_validator_rejects_tamper_and_noncanonical_bytes(
        self, tmp_path, monkeypatch
    ):
        runtime = self._activation_runtime()
        plan, plan_path, patch_bytes = self._activation_plan_fixture(tmp_path, runtime)
        monkeypatch.setattr(sys, "argv", ["script", "activate", str(plan_path), "false", str(self._script())])

        assert runtime["validate_plan"](
            plan, plan_path.read_bytes(), str(self._script())
        ) == patch_bytes
        tampered = json.loads(json.dumps(plan))
        tampered["candidate_commit"] = "0" * 40
        with pytest.raises(runtime["PromotionFailure"]) as rejected:
            runtime["validate_plan"](
                tampered,
                (runtime["canonical"](tampered) + "\n").encode(),
                str(self._script()),
            )
        assert rejected.value.code == "activation_plan_hash_mismatch"
        forged_patch = json.loads(json.dumps(plan))
        replacement_patch = b"forged full-index patch\n"
        forged_patch["forward_patch_b64"] = base64.b64encode(
            replacement_patch
        ).decode("ascii")
        forged_patch["forward_patch_sha256"] = runtime["sha"](
            replacement_patch
        )
        forged_patch["reverse_apply_patch_sha256"] = runtime["sha"](
            replacement_patch
        )
        forged_core = {
            key: value
            for key, value in forged_patch.items()
            if key != "plan_hash"
        }
        forged_patch["plan_hash"] = runtime["sha"](forged_core)
        with pytest.raises(runtime["PromotionFailure"]) as patch_substitution:
            runtime["validate_plan"](
                forged_patch,
                (runtime["canonical"](forged_patch) + "\n").encode(),
                str(self._script()),
            )
        assert patch_substitution.value.code == "activation_plan_patch_hash_mismatch"
        with pytest.raises(runtime["PromotionFailure"]) as noncanonical:
            runtime["validate_plan"](
                plan,
                (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode(),
                str(self._script()),
            )
        assert noncanonical.value.code == "activation_plan_not_canonical"

    def test_prepare_and_activation_source_require_exclusive_fsync_plan_and_no_legacy_mutation(self):
        text = self._script().read_text(encoding="utf-8")
        assert "os.O_EXCL" in text
        assert "os.fsync(fd)" in text
        assert "os.fsync(parent_fd)" in text
        assert "legacy one-shot mutation is retired" in text
        assert 'if [ "$MODE" = "prepare" ]' in text
        assert 'if [ "$MODE" = "activate" ] || [ "$MODE" = "recover" ]' in text
        assert "git reset" not in text
        assert "git checkout" not in text
        assert "--force" not in text

    def test_activation_journal_hash_chain_rejects_ambiguity_fatal_and_symlink(
        self, tmp_path
    ):
        runtime = self._activation_runtime()
        plan_hash = "sha256:" + "a" * 64
        path = tmp_path / "activation.journal"
        journal = runtime["Journal"](path, plan_hash)
        journal.append("COMPLETED", {"receipt": "sha256:" + "b" * 64})
        loaded = runtime["Journal"](path, plan_hash)
        assert loaded.rows[-1]["state"] == "COMPLETED"

        loaded.append("ROLLED_BACK", {"cause": "ambiguous"})
        with pytest.raises(runtime["PromotionFailure"]) as ambiguous:
            runtime["Journal"](path, plan_hash)
        assert ambiguous.value.code == "activation_journal_terminal_ambiguous"

        fatal_path = tmp_path / "fatal.journal"
        fatal = runtime["Journal"](fatal_path, plan_hash)
        fatal.append("ROLLBACK_FATAL", {"cause": "reverse_patch"})
        with pytest.raises(runtime["PromotionFailure"]) as unresolved:
            runtime["Journal"](fatal_path, plan_hash)
        assert unresolved.value.code == "activation_journal_rollback_fatal"

        completion_path = tmp_path / "completion-ambiguous.journal"
        completion = runtime["Journal"](completion_path, plan_hash)
        completion.append(
            "COMPLETION_AMBIGUOUS", {"cause": "response_lost"}
        )
        with pytest.raises(runtime["PromotionFailure"]) as lost_receipt:
            runtime["Journal"](completion_path, plan_hash)
        assert lost_receipt.value.code == (
            "activation_journal_completion_ambiguous"
        )

        target = tmp_path / "journal-target"
        target.write_text("", encoding="utf-8")
        target.chmod(0o600)
        link = tmp_path / "journal-link"
        link.symlink_to(target)
        with pytest.raises(runtime["PromotionFailure"]) as symlinked:
            runtime["Journal"](link, plan_hash)
        assert symlinked.value.code == "activation_journal_identity_invalid"

    def test_completion_transport_requires_exact_idempotent_receipt_readback(
        self, monkeypatch
    ):
        runtime = self._activation_runtime()
        candidate = "d" * 40
        body = {"candidate_commit": candidate}
        receipt = {
            "ok": True,
            "promotion_receipt_hash": "sha256:" + "9" * 64,
            "promoted_commit": candidate,
            "timeline_event_id": 9001,
        }

        class Response:
            def __init__(self, value):
                self.payload = json.dumps(value).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, *_args):
                return self.payload

        replies = iter((Response(receipt), Response(receipt)))
        monkeypatch.setattr(
            runtime["urllib"].request,
            "urlopen",
            lambda *_args, **_kwargs: next(replies),
        )
        assert runtime["RealOps"]().complete(body, "PRIVATE") == receipt

        replies = iter(
            (
                Response(receipt),
                runtime["urllib"].error.URLError("lost-response"),
            )
        )

        def response_then_loss(*_args, **_kwargs):
            value = next(replies)
            if isinstance(value, BaseException):
                raise value
            return value

        monkeypatch.setattr(
            runtime["urllib"].request, "urlopen", response_then_loss
        )
        with pytest.raises(runtime["PromotionFailure"]) as lost:
            runtime["RealOps"]().complete(body, "PRIVATE")
        assert lost.value.code == "activation_completion_uncertain"

        conflict = {**receipt, "timeline_event_id": 9002}
        replies = iter((Response(receipt), Response(conflict)))
        monkeypatch.setattr(
            runtime["urllib"].request,
            "urlopen",
            lambda *_args, **_kwargs: next(replies),
        )
        with pytest.raises(runtime["PromotionFailure"]) as conflicting:
            runtime["RealOps"]().complete(body, "PRIVATE")
        assert conflicting.value.code == "activation_completion_uncertain"

        def deterministic_rejection(*_args, **_kwargs):
            raise runtime["urllib"].error.HTTPError(
                "http://127.0.0.1:40000/complete",
                409,
                "gate rejected",
                {},
                None,
            )

        monkeypatch.setattr(
            runtime["urllib"].request,
            "urlopen",
            deterministic_rejection,
        )
        with pytest.raises(runtime["PromotionFailure"]) as rejected:
            runtime["RealOps"]().complete(body, "PRIVATE")
        assert rejected.value.code == (
            "activation_completion_durable_gate_rejected"
        )

    def test_activation_machine_success_and_failure_rolls_back_without_forbidden_git(
        self, tmp_path, monkeypatch
    ):
        runtime = self._activation_runtime()
        plan, _plan_path, patch_bytes = self._activation_plan_fixture(tmp_path, runtime)
        live_network_touches = []
        monotonic_ticks = iter(range(0, 100000, 100))
        monkeypatch.setattr(runtime["time"], "monotonic", lambda: next(monotonic_ticks))
        monkeypatch.setattr(runtime["time"], "sleep", lambda _seconds: None)

        def forbid_live_network(*args, **kwargs):
            live_network_touches.append((args, kwargs))
            pytest.fail("isolated activation harness must never touch live 40000")

        monkeypatch.setattr(runtime["urllib"].request, "urlopen", forbid_live_network)

        class FakeJournal:
            def __init__(self):
                self.rows = []
                self.fail_state = ""

            def append(self, state, evidence=None):
                if state == self.fail_state:
                    runtime["fail"](
                        "activation_journal_write_failed", "injected journal failure"
                    )
                row = {
                    "state": state,
                    "evidence": evidence or {},
                    "entry_hash": "sha256:" + f"{len(self.rows) + 1:064x}",
                }
                self.rows.append(row)
                return row

        class FakeOps:
            def __init__(self, fail_at=""):
                self.head = plan["stable_anchor_commit"]
                self.dev_head = plan["candidate_commit"]
                self.stable_branch = plan["stable_branch"]
                self.dev_branch = plan["dev_branch"]
                self.stable_dirty = ""
                self.dev_dirty = ""
                self.candidate_tree = plan["candidate_tree_sha"]
                self.listener = plan["old_process"]["pid"]
                self.identities = {
                    plan["old_process"]["pid"]: dict(plan["old_process"])
                }
                self.fail_at = fail_at
                self.failed_once = False
                self.commands = []
                self.start_count = 0
                self.lane_count = 0
                self.stable_worktree_count = 1

            def git(self, root, *args, code="activation_git_failed"):
                self.commands.append(("git", *args))
                if args == ("worktree", "list", "--porcelain"):
                    block = (
                        f"worktree {plan['stable_worktree']}\n"
                        f"HEAD {plan['stable_anchor_commit']}\n"
                        f"branch refs/heads/{plan['stable_branch']}\n"
                    )
                    if self.stable_worktree_count == 1:
                        return block
                    return block + (
                        f"\nworktree {plan['stable_worktree']}-duplicate\n"
                        f"HEAD {plan['stable_anchor_commit']}\n"
                        f"branch refs/heads/{plan['stable_branch']}\n"
                    )
                if args == ("rev-parse", "HEAD"):
                    if (
                        self.fail_at == "activation_final_cas_drift"
                        and self.lane_count == 4
                        and self.listener == plan["old_process"]["pid"]
                        and not self.failed_once
                    ):
                        self.failed_once = True
                        return "0" * 40
                    if (
                        self.fail_at == "activation_post_stop_cas_drift"
                        and not self.listener
                        and not self.failed_once
                    ):
                        self.failed_once = True
                        return "0" * 40
                    return self.head if str(root) == plan["stable_worktree"] else self.dev_head
                if args == ("rev-parse", "HEAD^{tree}") or args == (
                    "rev-parse", f"{plan['candidate_commit']}^{{tree}}"
                ):
                    return self.candidate_tree
                if args == ("branch", "--show-current"):
                    return self.stable_branch if str(root) == plan["stable_worktree"] else self.dev_branch
                if args == ("status", "--porcelain"):
                    if (
                        self.fail_at == "rollback_anchor_verification_failed"
                        and self.head == plan["stable_anchor_commit"]
                    ):
                        return " M rollback"
                    return self.stable_dirty if str(root) == plan["stable_worktree"] else self.dev_dirty
                raise AssertionError((root, args))

            def run(self, args, cwd, code, *, input_bytes=None):
                self.commands.append(tuple(args))
                if code == self.fail_at:
                    runtime["fail"](code, "injected")
                if args[:2] == ["git", "merge"]:
                    self.head = plan["candidate_commit"]
                elif args[:2] == ["git", "update-ref"]:
                    self.head = plan["stable_anchor_commit"]
                    self.stable_dirty = "M  candidate-preimage"
                elif args[:3] == ["git", "apply", "-R"]:
                    self.stable_dirty = ""
                elif args[:3] == ["git", "diff", "--cached"]:
                    return patch_bytes
                return b""

            def pid_identity(self, pid):
                return dict(self.identities[int(pid)])

            def port_pids(self, _port):
                return [self.listener] if self.listener else []

            def pid_alive(self, pid):
                return int(pid) in self.identities and int(pid) == self.listener

            def health(self, _port):
                commit = self.head
                if (
                    self.fail_at == "activation_health_identity_mismatch"
                    and commit == plan["candidate_commit"]
                ):
                    commit = "0" * 40
                if (
                    self.fail_at == "rollback_old_health_failed"
                    and commit == plan["stable_anchor_commit"]
                    and self.start_count
                ):
                    commit = "0" * 40
                source = (
                    plan["stable_runtime_source_sha256"]
                    if commit == plan["stable_anchor_commit"]
                    else plan["candidate_runtime_source_sha256"]
                )
                if (
                    self.fail_at == "activation_stable_source_identity_mismatch"
                    and commit == plan["stable_anchor_commit"]
                ) or (
                    self.fail_at == "activation_candidate_source_identity_mismatch"
                    and commit == plan["candidate_commit"]
                ):
                    source = "sha256:" + "0" * 64
                return {
                    "status": "ok",
                    "service": "governance",
                    "port": 40000,
                    "runtime_loaded_version": commit,
                    "runtime_loaded_source_sha256": source,
                    "runtime_stale": False,
                    "pid": self.listener,
                    "runtime_plane_identity": {
                        "plane": "stable",
                        "branch": plan["stable_branch"],
                        "commit": commit,
                        "stable_anchor_commit": commit,
                        "stable_database_identity": plan["stable_database_identity"],
                        "pid": self.listener,
                    },
                    "loaded_runtime_identity": {
                        "loaded_source_sha256": source,
                        "worktree_source_sha256": source,
                    },
                }

            def stop(self, identity, _port, code, *, require_listener=True):
                if code == self.fail_at:
                    runtime["fail"](code, "injected")
                assert identity["pid"] == self.listener
                self.listener = 0

            def start(self, launch_spec, _cwd, _log, _environment):
                if self.fail_at == "rollback_old_start_failed":
                    runtime["fail"]("rollback_old_start_failed", "injected")
                if self.fail_at == "start" and not self.failed_once:
                    self.failed_once = True
                    runtime["fail"]("activation_start_failed", "injected")
                self.start_count += 1
                self.listener = 42000 + self.start_count
                self.identities[self.listener] = {
                    "pid": self.listener,
                    "birth": f"birth-{self.listener}",
                    "command": " ".join(launch_spec),
                }
                return self.listener

            def graph_hash(self, _database):
                if self.fail_at == "activation_graph_changed" and self.head == plan["candidate_commit"]:
                    return "sha256:" + "f" * 64
                if self.fail_at == "rollback_graph_identity_failed" and self.head == plan["stable_anchor_commit"]:
                    return "sha256:" + "e" * 64
                return plan["graph_identity_hash"]

            def lane(self, _command, cwd):
                self.lane_count += 1
                if self.fail_at == "activation_lane_failed" and str(cwd) == plan["dev_worktree"]:
                    runtime["fail"]("activation_lane_failed", "injected")
                if (
                    self.fail_at == "activation_post_start_lane_failed"
                    and str(cwd) == plan["stable_worktree"]
                ):
                    runtime["fail"](
                        "activation_post_start_lane_failed", "injected"
                    )

            def reproject(self, supplied_plan, _script_path):
                if self.fail_at == "activation_durable_reprojection_failed":
                    runtime["fail"](
                        "activation_durable_reprojection_failed", "injected"
                    )
                projected = json.loads(json.dumps(supplied_plan["precheck_receipt"]))
                if self.fail_at == "activation_durable_authority_changed":
                    projected["receipt_hash"] = "sha256:" + "0" * 64
                elif self.fail_at == "activation_promotion_route_superseded":
                    projected["custody_authority"][
                        "promotion_route_authority_hash"
                    ] = "sha256:" + "0" * 64
                elif self.fail_at == "activation_cex_revision_drift":
                    projected["custody_authority"][
                        "implementation_runtime_revision"
                    ] += 1
                return projected

            def complete(self, _body, _token):
                if self.fail_at in {
                    "activation_completion_uncertain",
                    "activation_completion_durable_gate_rejected",
                }:
                    runtime["fail"](self.fail_at, "injected")
                if self.fail_at == "activation_completion_process_exit":
                    self.listener = 0
                if self.fail_at == "activation_completion_port_rebind":
                    self.listener = 49999
                    self.identities[49999] = {
                        "pid": 49999,
                        "birth": "foreign-rebind",
                        "command": "foreign-listener",
                    }
                return {
                    "ok": True,
                    "promotion_receipt_hash": "sha256:" + "9" * 64,
                    "promoted_commit": plan["candidate_commit"],
                    "timeline_event_id": 9001,
                }

        success_ops = FakeOps()
        success_journal = FakeJournal()
        success = runtime["ActivationMachine"](
            plan, patch_bytes, success_ops, success_journal, "token"
        ).activate()
        assert success["ok"] is True
        assert success_ops.head == plan["candidate_commit"]
        assert success_journal.rows[-1]["state"] == "COMPLETED"

        for injected in (
            "activation_post_stop_cas_drift",
            "activation_final_cas_drift",
            "activation_ff_failed",
            "start",
            "activation_health_identity_mismatch",
            "activation_candidate_source_identity_mismatch",
            "activation_post_start_lane_failed",
            "activation_graph_changed",
        ):
            ops = FakeOps(injected)
            journal = FakeJournal()
            result = runtime["ActivationMachine"](
                plan, patch_bytes, ops, journal, "token"
            ).activate()
            assert result["rolled_back"] is True, injected
            assert ops.head == plan["stable_anchor_commit"], injected
            assert journal.rows[-1]["state"] == "ROLLED_BACK", injected
            flattened = " ".join(
                str(part) for command in ops.commands for part in command
            )
            assert " reset " not in f" {flattened} "
            assert " checkout " not in f" {flattened} "
            assert " --force " not in f" {flattened} "

        lane_ops = FakeOps("activation_lane_failed")
        lane_journal = FakeJournal()
        with pytest.raises(runtime["PromotionFailure"]) as lane_failure:
            runtime["ActivationMachine"](
                plan, patch_bytes, lane_ops, lane_journal, "token"
            ).activate()
        assert lane_failure.value.code == "activation_lane_failed"
        assert lane_ops.listener == plan["old_process"]["pid"]
        assert lane_ops.head == plan["stable_anchor_commit"]
        assert lane_journal.rows == []

        changed_authority_ops = FakeOps("activation_durable_authority_changed")
        changed_authority_journal = FakeJournal()
        with pytest.raises(runtime["PromotionFailure"]) as authority_changed:
            runtime["ActivationMachine"](
                plan,
                patch_bytes,
                changed_authority_ops,
                changed_authority_journal,
                "token",
            ).activate()
        assert authority_changed.value.code == "activation_durable_authority_changed"
        assert changed_authority_ops.listener == plan["old_process"]["pid"]
        assert changed_authority_journal.rows == []

        for custody_drift in (
            "activation_promotion_route_superseded",
            "activation_cex_revision_drift",
        ):
            ops = FakeOps(custody_drift)
            journal = FakeJournal()
            with pytest.raises(runtime["PromotionFailure"]) as rejected:
                runtime["ActivationMachine"](
                    plan, patch_bytes, ops, journal, "token"
                ).activate()
            assert rejected.value.code == (
                "activation_durable_authority_changed"
            )
            assert ops.listener == plan["old_process"]["pid"]
            assert ops.head == plan["stable_anchor_commit"]
            assert journal.rows == []

        for completion_failure in ("activation_completion_uncertain",):
            ops = FakeOps(completion_failure)
            journal = FakeJournal()
            with pytest.raises(runtime["PromotionFailure"]) as ambiguous_completion:
                runtime["ActivationMachine"](
                    plan, patch_bytes, ops, journal, "token"
                ).activate()
            assert ambiguous_completion.value.code == completion_failure
            assert ops.head == plan["candidate_commit"]
            assert ops.listener != 0
            assert journal.rows[-1]["state"] == "COMPLETION_AMBIGUOUS"

        for completion_continuity_failure in (
            "activation_completion_process_exit",
            "activation_completion_port_rebind",
        ):
            ops = FakeOps(completion_continuity_failure)
            journal = FakeJournal()
            with pytest.raises(runtime["PromotionFailure"]) as continuity:
                runtime["ActivationMachine"](
                    plan, patch_bytes, ops, journal, "token"
                ).activate()
            assert continuity.value.code.startswith("activation_completion_")
            assert ops.head == plan["candidate_commit"]
            assert journal.rows[-1]["state"] == "COMPLETION_AMBIGUOUS"

        rejected_ops = FakeOps("activation_completion_durable_gate_rejected")
        rejected_journal = FakeJournal()
        rejected = runtime["ActivationMachine"](
            plan, patch_bytes, rejected_ops, rejected_journal, "token"
        ).activate()
        assert rejected["rolled_back"] is True
        assert rejected_ops.head == plan["stable_anchor_commit"]
        assert rejected_journal.rows[-1]["state"] == "ROLLED_BACK"

        committed_journal_failure_ops = FakeOps()
        committed_journal_failure = FakeJournal()
        committed_journal_failure.fail_state = "COMPLETION_COMMITTED"
        with pytest.raises(runtime["PromotionFailure"]) as journal_uncertain:
            runtime["ActivationMachine"](
                plan,
                patch_bytes,
                committed_journal_failure_ops,
                committed_journal_failure,
                "token",
            ).activate()
        assert journal_uncertain.value.code == "activation_completion_journal_uncertain"
        assert committed_journal_failure_ops.head == plan["candidate_commit"]
        assert committed_journal_failure_ops.listener != 0
        assert committed_journal_failure.rows[-1]["state"] == "COMPLETION_AMBIGUOUS"

        pre_mutation_ops = FakeOps("activation_old_stop_failed")
        pre_mutation_journal = FakeJournal()
        old_stop = runtime["ActivationMachine"](
            plan,
            patch_bytes,
            pre_mutation_ops,
            pre_mutation_journal,
            "token",
        ).activate()
        assert old_stop["rolled_back"] is True
        assert pre_mutation_ops.head == plan["stable_anchor_commit"]
        assert pre_mutation_ops.listener == plan["old_process"]["pid"]
        assert pre_mutation_journal.rows[-1]["state"] == "ROLLED_BACK"

        rollback_fatal_ops = FakeOps("rollback_ref_cas_failed")
        rollback_fatal_ops.fail_at = "activation_graph_changed"
        original_run = rollback_fatal_ops.run

        def fail_rollback_ref(args, cwd, code, *, input_bytes=None):
            if code == "rollback_ref_cas_failed":
                runtime["fail"](code, "injected")
            return original_run(args, cwd, code, input_bytes=input_bytes)

        rollback_fatal_ops.run = fail_rollback_ref
        rollback_fatal_journal = FakeJournal()
        with pytest.raises(runtime["PromotionFailure"]) as fatal:
            runtime["ActivationMachine"](
                plan,
                patch_bytes,
                rollback_fatal_ops,
                rollback_fatal_journal,
                "token",
            ).activate()
        assert fatal.value.code == "rollback_ref_cas_failed"
        assert rollback_fatal_journal.rows[-1]["state"] == "ROLLBACK_FATAL"

        replay_journal = FakeJournal()
        replay_journal.append("COMPLETED", {"receipt": "existing"})
        replay_ops = FakeOps()
        replay = runtime["ActivationMachine"](
            plan, patch_bytes, replay_ops, replay_journal, "token"
        ).activate()
        assert replay["idempotent"] is True
        assert replay_ops.commands == []

        expired_plan = json.loads(json.dumps(plan))
        expired_plan["manifest"]["gates"]["operator_signoff"][
            "expires_at"
        ] = "2026-08-27T00:00:00Z"
        with pytest.raises(runtime["PromotionFailure"]) as expired:
            runtime["ActivationMachine"](
                expired_plan, patch_bytes, FakeOps(), FakeJournal(), "token"
            ).activate()
        assert expired.value.code == "activation_plan_signoff_expired"
        expired_terminal = FakeJournal()
        expired_terminal.append("COMPLETED", {"receipt": "existing"})
        expired_replay_ops = FakeOps()
        replay_after_expiry = runtime["ActivationMachine"](
            expired_plan,
            patch_bytes,
            expired_replay_ops,
            expired_terminal,
            "token",
        ).activate()
        assert replay_after_expiry["idempotent"] is True
        assert expired_replay_ops.commands == []

        resumed_ops = FakeOps()
        resumed_ops.head = plan["candidate_commit"]
        resumed_ops.listener = 45001
        resumed_ops.identities[45001] = {
            "pid": 45001,
            "birth": "resumed-candidate",
            "command": " ".join(plan["candidate_launch_spec"]),
        }
        resumed_journal = FakeJournal()
        resumed_journal.append(
            "CANDIDATE_HEALTHY",
            {
                "pid": 45001,
                "process_identity": resumed_ops.identities[45001],
            },
        )
        resumed = runtime["ActivationMachine"](
            plan, patch_bytes, resumed_ops, resumed_journal, "token"
        ).activate()
        assert resumed["ok"] is True
        assert resumed["idempotent"] is True
        assert resumed_ops.head == plan["candidate_commit"]
        assert resumed_journal.rows[-1]["state"] == "COMPLETED"

        def promoted_ops(fail_at=""):
            ops = FakeOps(fail_at)
            ops.head = plan["candidate_commit"]
            ops.listener = 45011
            ops.identities[45011] = {
                "pid": 45011,
                "birth": "recovery-candidate",
                "command": " ".join(plan["candidate_launch_spec"]),
            }
            return ops

        def submitted_journal():
            journal = FakeJournal()
            journal.append(
                "CANDIDATE_HEALTHY",
                {
                    "pid": 45011,
                    "process_identity": promoted_ops().identities[45011],
                },
            )
            journal.append(
                "COMPLETION_SUBMITTING",
                {"candidate_commit": plan["candidate_commit"]},
            )
            return journal

        lost_response_activate_ops = promoted_ops()
        lost_response_activate_journal = submitted_journal()
        lost_response_activate = runtime["ActivationMachine"](
            plan,
            patch_bytes,
            lost_response_activate_ops,
            lost_response_activate_journal,
            "token",
        ).activate()
        assert lost_response_activate["ok"] is True
        assert lost_response_activate_journal.rows[-1]["state"] == "COMPLETED"

        lost_response_recover_ops = promoted_ops()
        lost_response_recover_journal = submitted_journal()
        lost_response_recover = runtime["ActivationMachine"](
            plan,
            patch_bytes,
            lost_response_recover_ops,
            lost_response_recover_journal,
            "token",
        ).recover()
        assert lost_response_recover["ok"] is True
        assert lost_response_recover_journal.rows[-1]["state"] == "COMPLETED"

        committed_ops = promoted_ops()
        committed_journal = submitted_journal()
        committed_journal.append(
            "COMPLETION_COMMITTED",
            {
                "completion": {
                    "ok": True,
                    "promotion_receipt_hash": "sha256:" + "9" * 64,
                    "promoted_commit": plan["candidate_commit"],
                    "timeline_event_id": 9001,
                }
            },
        )
        committed_finalize = runtime["ActivationMachine"](
            plan, patch_bytes, committed_ops, committed_journal, "token"
        ).recover()
        assert committed_finalize["ok"] is True
        assert committed_journal.rows[-1]["state"] == "COMPLETED"

        rejected_resume_ops = promoted_ops(
            "activation_completion_durable_gate_rejected"
        )
        rejected_resume_journal = FakeJournal()
        rejected_resume_journal.append(
            "CANDIDATE_HEALTHY",
            {
                "pid": 45011,
                "process_identity": rejected_resume_ops.identities[45011],
            },
        )
        rejected_resume = runtime["ActivationMachine"](
            plan,
            patch_bytes,
            rejected_resume_ops,
            rejected_resume_journal,
            "token",
        ).activate()
        assert rejected_resume["rolled_back"] is True
        assert rejected_resume_ops.head == plan["stable_anchor_commit"]

        source_drift_resume_ops = promoted_ops(
            "activation_candidate_source_identity_mismatch"
        )
        source_drift_resume_journal = FakeJournal()
        source_drift_resume_journal.append(
            "CANDIDATE_HEALTHY",
            {
                "pid": 45011,
                "process_identity": source_drift_resume_ops.identities[45011],
            },
        )
        source_drift_resume = runtime["ActivationMachine"](
            plan,
            patch_bytes,
            source_drift_resume_ops,
            source_drift_resume_journal,
            "token",
        ).activate()
        assert source_drift_resume["rolled_back"] is True
        assert source_drift_resume_ops.head == plan["stable_anchor_commit"]

        for recovery_mode in ("activate", "recover"):
            uncertain_ops = promoted_ops(
                "activation_completion_durable_gate_rejected"
            )
            uncertain_journal = submitted_journal()
            machine = runtime["ActivationMachine"](
                plan, patch_bytes, uncertain_ops, uncertain_journal, "token"
            )
            with pytest.raises(runtime["PromotionFailure"]) as uncertain:
                getattr(machine, recovery_mode)()
            assert uncertain.value.code == (
                "activation_completion_durable_gate_rejected"
            )
            assert uncertain_ops.head == plan["candidate_commit"]
            assert uncertain_journal.rows[-1]["state"] == (
                "COMPLETION_AMBIGUOUS"
            )

        crash_candidate_ops = FakeOps()
        crash_candidate_ops.head = plan["candidate_commit"]
        crash_candidate_ops.listener = 45002
        crash_candidate_ops.identities[45002] = {
            "pid": 45002,
            "birth": "crash-candidate",
            "command": " ".join(plan["candidate_launch_spec"]),
        }
        crash_candidate_journal = FakeJournal()
        crash_candidate_journal.append(
            "CANDIDATE_SPAWNED",
            {"process_identity": crash_candidate_ops.identities[45002]},
        )
        recovered_candidate = runtime["ActivationMachine"](
            plan,
            patch_bytes,
            crash_candidate_ops,
            crash_candidate_journal,
            "token",
        ).recover()
        assert recovered_candidate["rolled_back"] is True
        assert crash_candidate_ops.head == plan["stable_anchor_commit"]
        assert crash_candidate_journal.rows[-1]["state"] == "ROLLED_BACK"

        crash_old_ops = FakeOps()
        crash_old_ops.head = plan["stable_anchor_commit"]
        crash_old_ops.listener = 45003
        crash_old_ops.identities[45003] = {
            "pid": 45003,
            "birth": "crash-restored-old",
            "command": " ".join(plan["old_launch_spec"]),
        }
        crash_old_journal = FakeJournal()
        crash_old_journal.append(
            "ROLLBACK_OLD_SPAWNED",
            {"process_identity": crash_old_ops.identities[45003]},
        )
        recovered_old = runtime["ActivationMachine"](
            plan,
            patch_bytes,
            crash_old_ops,
            crash_old_journal,
            "token",
        ).recover()
        assert recovered_old["rolled_back"] is True
        assert crash_old_ops.head == plan["stable_anchor_commit"]
        assert crash_old_journal.rows[-1]["state"] == "ROLLED_BACK"

        precheck_cases = [
            (
                lambda ops: setattr(ops, "stable_worktree_count", 2),
                "activation_stable_worktree_ambiguous",
            ),
            (lambda ops: setattr(ops, "head", "0" * 40), "activation_cas_anchor_drift"),
            (lambda ops: setattr(ops, "stable_branch", "other"), "activation_cas_branch_drift"),
            (lambda ops: setattr(ops, "stable_dirty", " M source"), "activation_cas_dirty_stable"),
            (lambda ops: setattr(ops, "dev_head", "0" * 40), "activation_candidate_drift"),
            (lambda ops: setattr(ops, "dev_branch", "other"), "activation_candidate_identity_drift"),
            (lambda ops: setattr(ops, "dev_dirty", " M source"), "activation_candidate_identity_drift"),
            (lambda ops: ops.identities.__setitem__(plan["old_process"]["pid"], {**plan["old_process"], "birth": "other"}), "activation_old_pid_identity_drift"),
            (lambda ops: setattr(ops, "listener", 41002), "activation_old_listener_drift"),
            (lambda ops: setattr(ops, "candidate_tree", "0" * 40), "activation_candidate_tree_drift"),
            (
                lambda ops: setattr(
                    ops, "fail_at", "activation_stable_source_identity_mismatch"
                ),
                "activation_health_identity_mismatch",
            ),
            (lambda ops: setattr(ops, "fail_at", "rollback_graph_identity_failed"), "activation_graph_identity_drift"),
        ]
        for mutate, expected_code in precheck_cases:
            ops = FakeOps()
            mutate(ops)
            journal = FakeJournal()
            with pytest.raises((runtime["PromotionFailure"], AssertionError)) as rejected:
                runtime["ActivationMachine"](
                    plan, patch_bytes, ops, journal, "token"
                ).validate_pre_mutation()
            if isinstance(rejected.value, runtime["PromotionFailure"]):
                assert rejected.value.code == expected_code
            assert journal.rows == []

        database = Path(plan["stable_database_path"])
        original_database = database.with_suffix(".original")
        database.replace(original_database)
        database.write_bytes(b"replacement")
        try:
            with pytest.raises(runtime["PromotionFailure"]) as db_drift:
                runtime["ActivationMachine"](
                    plan, patch_bytes, FakeOps(), FakeJournal(), "token"
                ).validate_pre_mutation()
            assert db_drift.value.code == "activation_database_identity_drift"
        finally:
            database.unlink()
            original_database.replace(database)

        for injected in (
            "rollback_candidate_stop_failed",
            "rollback_ref_cas_failed",
            "rollback_reverse_patch_failed",
            "rollback_anchor_verification_failed",
            "rollback_old_start_failed",
            "rollback_old_health_failed",
            "rollback_graph_identity_failed",
        ):
            ops = FakeOps(injected)
            ops.head = plan["candidate_commit"]
            ops.listener = 43001
            ops.identities[43001] = {
                "pid": 43001,
                "birth": "candidate-birth",
                "command": " ".join(plan["candidate_launch_spec"]),
            }
            journal = FakeJournal()
            machine = runtime["ActivationMachine"](
                plan, patch_bytes, ops, journal, "token"
            )
            machine.mutated = True
            machine.candidate_pid = 43001
            machine.candidate_identity = dict(ops.identities[43001])
            with pytest.raises(runtime["PromotionFailure"]) as rollback_failure:
                machine.rollback("injected_failure")
            expected_rollback_code = {
                "rollback_old_health_failed": "activation_health_identity_mismatch",
            }.get(injected, injected)
            assert rollback_failure.value.code.startswith(expected_rollback_code)
            assert journal.rows[-1]["state"] == "ROLLBACK_FATAL"

        for signum in (__import__("signal").SIGINT, __import__("signal").SIGTERM):
            ops = FakeOps()
            ops.head = plan["candidate_commit"]
            ops.listener = 44000 + int(signum)
            ops.identities[ops.listener] = {
                "pid": ops.listener,
                "birth": "signal-candidate",
                "command": " ".join(plan["candidate_launch_spec"]),
            }
            journal = FakeJournal()
            machine = runtime["ActivationMachine"](
                plan, patch_bytes, ops, journal, "token"
            )
            machine.mutated = True
            machine.mutation_intent = True
            machine.candidate_pid = ops.listener
            machine.candidate_identity = dict(ops.identities[ops.listener])
            with pytest.raises(SystemExit) as interrupted:
                machine.interrupt(signum)
            assert interrupted.value.code == 128 + int(signum)
            assert ops.head == plan["stable_anchor_commit"]
            assert journal.rows[-1]["state"] == "ROLLED_BACK"

        for unexpected_head in (plan["candidate_commit"], "f" * 40):
            ops = FakeOps()
            ops.head = unexpected_head
            journal = FakeJournal()
            with pytest.raises(runtime["PromotionFailure"]) as already:
                runtime["ActivationMachine"](
                    plan, patch_bytes, ops, journal, "token"
                ).activate()
            expected = (
                "activation_already_promoted_ambiguous"
                if unexpected_head == plan["candidate_commit"]
                else "activation_cas_anchor_drift"
            )
            assert already.value.code == expected
            assert journal.rows == []
        assert live_network_touches == []

    def test_real_git_reverse_apply_and_crash_recovery_restore_exact_anchor(
        self, tmp_path
    ):
        runtime = self._activation_runtime()
        root = tmp_path / "stable-real-git"
        root.mkdir()

        def git(*args, text=True):
            result = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=text, check=False
            )
            assert result.returncode == 0, result.stderr
            return result.stdout

        git("init", "-q", "-b", "codex/direct-no-pass-post-reconcile-r2")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        source = root / "agent/governance/server.py"
        source.parent.mkdir(parents=True)
        source.write_text("anchor\n", encoding="utf-8")
        (root / "state.txt").write_text("anchor\n", encoding="utf-8")
        database_relative = (
            "shared-volume/codex-tasks/state/governance/aming-claw/governance.db"
        )
        database = root / database_relative
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE historical_control_fixture(value)")
        git("add", ".")
        git("commit", "-qm", "anchor")
        anchor = git("rev-parse", "HEAD").strip()
        source.write_text("candidate\n", encoding="utf-8")
        (root / "state.txt").write_text("candidate\n", encoding="utf-8")
        git("add", ".")
        git("commit", "-qm", "candidate")
        candidate = git("rev-parse", "HEAD").strip()
        tree = git("rev-parse", "HEAD^{tree}").strip()
        patch_bytes = subprocess.check_output(
            [
                "git", "diff", "--no-ext-diff", "--no-textconv", "--binary",
                "--full-index", "-M", f"{anchor}..{candidate}", "--", ".",
            ],
            cwd=root,
        )
        metadata = database.stat()
        python_bin = str(Path(sys.executable).resolve())
        shared = str(root / "shared-volume")
        old_launch = [
            python_bin, "-m", "agent.cli", "start", "--workspace",
            str(root), "--port", "40000",
        ]
        candidate_launch = [
            python_bin, "-m", "agent.cli", "start", "--runtime-plane", "stable",
            "--port", "40000", "--stable-anchor-commit", candidate,
            "--workspace", str(root), "--shared-volume-path", shared,
        ]
        database_identity = {
            "schema_version": "ac_stable_database_identity.v1",
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "stable_relative_path_sha256": runtime["sha"](
                database_relative.encode()
            ),
        }
        plan = {
            "stable_worktree": str(root),
            "stable_branch": "codex/direct-no-pass-post-reconcile-r2",
            "stable_anchor_commit": anchor,
            "candidate_commit": candidate,
            "candidate_tree_sha": tree,
            "stable_port": 40000,
            "stable_database_path": str(database),
            "stable_database_relative_path": database_relative,
            "stable_database_path_sha256": runtime["sha"](
                database_relative.encode()
            ),
            "stable_database_identity": database_identity,
            "stable_runtime_source_sha256": runtime["sha"](
                subprocess.check_output(
                    ["git", "show", f"{anchor}:agent/governance/server.py"],
                    cwd=root,
                )
            ),
            "candidate_runtime_source_sha256": runtime["sha"](
                subprocess.check_output(
                    ["git", "show", f"{candidate}:agent/governance/server.py"],
                    cwd=root,
                )
            ),
            "old_process": {
                "pid": 51001,
                "birth": "old-birth",
                "command": " ".join(old_launch),
            },
            "old_launch_spec": old_launch,
            "old_launch_environment": {
                "PYTHONPATH": str(root), "SHARED_VOLUME_PATH": shared,
            },
            "candidate_launch_spec": candidate_launch,
            "candidate_launch_environment": {
                "PYTHONPATH": str(root), "SHARED_VOLUME_PATH": shared,
            },
            "runtime_process_executable": python_bin,
            "graph_identity_hash": "sha256:" + "a" * 64,
            "forward_patch_sha256": runtime["sha"](patch_bytes),
            "journal_path": str(tmp_path / "real-git.journal"),
        }

        class Journal:
            def __init__(self):
                self.rows = []

            def append(self, state, evidence=None):
                row = {
                    "state": state,
                    "evidence": evidence or {},
                    "entry_hash": "sha256:" + f"{len(self.rows)+1:064x}",
                }
                self.rows.append(row)
                return row

        class HybridOps(runtime["RealOps"]):
            def __init__(self):
                self.listener = 0
                self.identities = {}
                self.commands = []

            def run(self, args, cwd, code, *, input_bytes=None):
                self.commands.append(tuple(args))
                return super().run(
                    args, cwd, code, input_bytes=input_bytes
                )

            def port_pids(self, _port):
                return [self.listener] if self.listener else []

            def pid_alive(self, pid):
                return int(pid) == self.listener and bool(self.listener)

            def pid_identity(self, pid):
                return dict(self.identities.get(int(pid), {}))

            def start(self, launch_spec, _cwd, _log, _environment):
                self.listener = 52001
                self.identities[self.listener] = {
                    "pid": self.listener,
                    "birth": "restored-old-birth",
                    "command": " ".join(
                        [plan["runtime_process_executable"], *launch_spec[1:]]
                    ),
                }
                return self.listener

            def health(self, _port):
                return {
                    "status": "ok", "service": "governance", "port": 40000,
                    "pid": self.listener, "runtime_loaded_version": anchor,
                    "runtime_loaded_source_sha256": plan["stable_runtime_source_sha256"],
                    "runtime_stale": False, "runtime_plane_identity": {},
                    "loaded_runtime_identity": {
                        "loaded_source_sha256": plan["stable_runtime_source_sha256"],
                        "worktree_source_sha256": plan["stable_runtime_source_sha256"],
                    },
                }

            def graph_hash(self, _database):
                return plan["graph_identity_hash"]

        first_ops = HybridOps()
        first_journal = Journal()
        first = runtime["ActivationMachine"](
            plan, patch_bytes, first_ops, first_journal, "token"
        ).rollback("candidate_failure")
        assert first["rolled_back"] is True
        assert git("rev-parse", "HEAD").strip() == anchor
        assert git("status", "--porcelain") == ""
        assert "ROLLBACK_REF_RESTORED" in {
            row["state"] for row in first_journal.rows
        }
        assert "ROLLBACK_PATCH_REVERSED" in {
            row["state"] for row in first_journal.rows
        }

        # Recreate the exact crash window: ref was restored by CAS but index
        # and worktree still contain the candidate preimage.
        git("merge", "--ff-only", candidate)
        git(
            "update-ref",
            "refs/heads/codex/direct-no-pass-post-reconcile-r2",
            anchor,
            candidate,
        )
        assert git("rev-parse", "HEAD").strip() == anchor
        assert git("status", "--porcelain")
        recovered_ops = HybridOps()
        recovered_journal = Journal()
        recovered = runtime["ActivationMachine"](
            plan, patch_bytes, recovered_ops, recovered_journal, "token"
        ).recover()
        assert recovered["rolled_back"] is True
        assert git("rev-parse", "HEAD").strip() == anchor
        assert git("status", "--porcelain") == ""
        flattened = " ".join(
            part
            for row in recovered_ops.commands
            for part in row
        )
        assert "reset" not in flattened
        assert "checkout" not in flattened
        assert "--force" not in flattened

    def test_real_subprocess_delayed_bind_preserves_pid_argv_and_health(
        self, tmp_path, monkeypatch
    ):
        runtime = self._activation_runtime()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        stable = tmp_path / "stable-runtime"
        database_relative = (
            "shared-volume/codex-tasks/state/governance/aming-claw/governance.db"
        )
        database = stable / database_relative
        database.parent.mkdir(parents=True)
        database.write_bytes(b"db")
        metadata = database.stat()
        database_identity = {
            "schema_version": "ac_stable_database_identity.v1",
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "stable_relative_path_sha256": runtime["sha"](
                database_relative.encode()
            ),
        }
        commit = "d" * 40
        source_sha = "sha256:" + "a" * 64
        server_script = tmp_path / "delayed-health.py"
        server_script.write_text(
            """
import http.server, json, os, sys, time
port=int(sys.argv[1]); commit=sys.argv[2]; source=sys.argv[3]
database=json.loads(sys.argv[4]); time.sleep(0.35)
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        body={"status":"ok","service":"governance","port":port,"pid":os.getpid(),
              "runtime_loaded_version":commit,"runtime_loaded_source_sha256":source,
              "runtime_stale":False,
              "secret_present":any(os.environ.get(k) for k in ("GOV_COORDINATOR_TOKEN","AC_OPERATOR_SECRET")),
              "loaded_runtime_identity":{"loaded_source_sha256":source,"worktree_source_sha256":source},
              "runtime_plane_identity":{"plane":"stable","branch":"codex/direct-no-pass-post-reconcile-r2",
              "commit":commit,"stable_anchor_commit":commit,"stable_database_identity":database,"pid":os.getpid()}}
        raw=json.dumps(body).encode(); self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
http.server.HTTPServer(("127.0.0.1",port),Handler).serve_forever()
""".strip()
            + "\n",
            encoding="utf-8",
        )
        launch = [
            str(Path(sys.executable).resolve()),
            str(server_script),
            str(port),
            commit,
            source_sha,
            json.dumps(database_identity, sort_keys=True, separators=(",", ":")),
        ]
        plan = {
            "stable_port": port,
            "stable_branch": "codex/direct-no-pass-post-reconcile-r2",
            "stable_worktree": str(stable),
            "stable_database_relative_path": database_relative,
            "stable_database_path": str(database),
            "stable_database_path_sha256": runtime["sha"](
                database_relative.encode()
            ),
            "stable_database_identity": database_identity,
            "stable_runtime_source_sha256": "sha256:" + "b" * 64,
            "candidate_runtime_source_sha256": source_sha,
        }
        ops = runtime["RealOps"]()
        machine = runtime["ActivationMachine"](
            plan, b"", ops, type("J", (), {"rows": []})(), "token"
        )
        monkeypatch.setenv("GOV_COORDINATOR_TOKEN", "PRIVATE-COORDINATOR")
        monkeypatch.setenv("AC_OPERATOR_SECRET", "PRIVATE-OPERATOR")
        pid = ops.start(
            launch,
            str(stable),
            str(tmp_path / "delayed-health.log"),
            {"PYTHONPATH": str(stable)},
        )
        identity = {}
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                observed = ops.pid_identity(pid)
                if observed.get("birth") and observed.get("command"):
                    plan["runtime_process_executable"] = observed["command"].split(
                        " ", 1
                    )[0]
                    break
                time.sleep(0.05)
            assert plan.get("runtime_process_executable")
            identity = machine.exact_started_process(
                pid, launch, "delayed_candidate_identity"
            )
            health = machine.poll_health(commit, pid)
            assert identity["pid"] == pid
            assert identity["command"] == " ".join(
                [plan["runtime_process_executable"], *launch[1:]]
            )
            assert health["runtime_loaded_version"] == commit
            assert health["secret_present"] is False
        finally:
            if ops.pid_alive(pid):
                current = identity or ops.pid_identity(pid)
                ops.stop(
                    current,
                    port,
                    "delayed_candidate_cleanup",
                    require_listener=False,
                )

        # A candidate that spawned but never bound is still owned by exact
        # PID/birth/argv and must be stoppable during rollback.
        sleeper = [
            str(Path(sys.executable).resolve()),
            "-c",
            "import time; time.sleep(30)",
        ]
        sleeper_pid = ops.start(
            sleeper,
            str(stable),
            str(tmp_path / "spawn-before-bind.log"),
            {},
        )
        sleeper_identity = {}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            sleeper_identity = ops.pid_identity(sleeper_pid)
            if sleeper_identity.get("birth") and sleeper_identity.get("command"):
                break
            time.sleep(0.05)
        sleeper_executable = sleeper_identity["command"].split(" ", 1)[0]
        assert sleeper_identity["command"] == " ".join(
            [sleeper_executable, *sleeper[1:]]
        )
        assert ops.port_pids(port) == []
        ops.stop(
            sleeper_identity,
            port,
            "spawn_before_bind_cleanup",
            require_listener=False,
        )
        assert not ops.pid_alive(sleeper_pid)

    def test_activation_lock_is_exclusive_across_real_processes(self, tmp_path):
        import fcntl

        runtime = self._activation_runtime()
        plan, plan_path, _patch = self._activation_plan_fixture(tmp_path, runtime)
        lock_path = Path(plan["lock_path"])
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(
                [
                    str(self._script()),
                    "--activate",
                    "--activation-plan",
                    str(plan_path),
                ],
                cwd=self._script().parents[1],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "GOV_COORDINATOR_TOKEN": "isolated-test"},
            )
        finally:
            os.close(fd)
        assert result.returncode != 0
        assert "activation_lock_busy" in (result.stdout + result.stderr)

    def test_activation_dry_run_is_physically_zero_write(
        self, tmp_path, monkeypatch, capsys
    ):
        runtime = self._activation_runtime()
        plan, plan_path, _patch = self._activation_plan_fixture(tmp_path, runtime)

        class DryOps:
            def git(self, root, *args, code="activation_git_failed"):
                if args == ("worktree", "list", "--porcelain"):
                    return (
                        f"worktree {plan['stable_worktree']}\n"
                        f"HEAD {plan['stable_anchor_commit']}\n"
                        f"branch refs/heads/{plan['stable_branch']}\n"
                    )
                if args == ("rev-parse", "HEAD"):
                    return (
                        plan["stable_anchor_commit"]
                        if str(root) == plan["stable_worktree"]
                        else plan["candidate_commit"]
                    )
                if args == ("branch", "--show-current"):
                    return (
                        plan["stable_branch"]
                        if str(root) == plan["stable_worktree"]
                        else plan["dev_branch"]
                    )
                if args == ("status", "--porcelain"):
                    return ""
                if args == (
                    "rev-parse",
                    f"{plan['candidate_commit']}^{{tree}}",
                ):
                    return plan["candidate_tree_sha"]
                raise AssertionError((root, args, code))

            def pid_identity(self, pid):
                assert pid == plan["old_process"]["pid"]
                return dict(plan["old_process"])

            def port_pids(self, _port):
                return [plan["old_process"]["pid"]]

            def health(self, _port):
                source = plan["stable_runtime_source_sha256"]
                return {
                    "status": "ok",
                    "service": "governance",
                    "port": 40000,
                    "pid": plan["old_process"]["pid"],
                    "runtime_loaded_version": plan["stable_anchor_commit"],
                    "runtime_loaded_source_sha256": source,
                    "runtime_stale": False,
                    "runtime_plane_identity": {},
                    "loaded_runtime_identity": {
                        "loaded_source_sha256": source,
                        "worktree_source_sha256": source,
                    },
                }

            def graph_hash(self, _database):
                return plan["graph_identity_hash"]

        before_paths = sorted(
            str(path.relative_to(tmp_path))
            for path in tmp_path.rglob("*")
        )
        before_bytes = {
            str(path.relative_to(tmp_path)): path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file()
        }
        monkeypatch.setitem(runtime, "RealOps", DryOps)
        monkeypatch.setattr(
            sys,
            "argv",
            ["script", "activate", str(plan_path), "true", str(self._script())],
        )

        runtime["main"]()

        output = json.loads(capsys.readouterr().out.strip())
        assert output["dry_run"] is True
        assert output["writes_performed"] is False
        assert not Path(plan["lock_path"]).exists()
        assert not Path(plan["journal_path"]).exists()
        assert sorted(
            str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")
        ) == before_paths
        assert {
            str(path.relative_to(tmp_path)): path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file()
        } == before_bytes

    @staticmethod
    def _current_main_state(fixture):
        """Capture physical release state immediately before a read-only guard."""
        def database_state(path):
            return {
                suffix: (path.parent / (path.name + suffix)).read_bytes()
                for suffix in ("", "-wal", "-shm")
                if (path.parent / (path.name + suffix)).is_file()
            }

        with sqlite3.connect(f"file:{fixture['stable_db']}?mode=ro", uri=True) as conn:
            queue = tuple(conn.execute(
                "SELECT * FROM release_operator_head_queue_events ORDER BY id"
            ))
            refs = tuple(conn.execute(
                "SELECT * FROM graph_snapshot_refs ORDER BY ref_name"
            ))
        return {
            "stable_db": database_state(fixture["stable_db"]),
            "dev_db": database_state(fixture["dev_db"]),
            "queue": queue,
            "refs": refs,
            "stable_head": fixture["git"]("rev-parse", "HEAD", root=fixture["stable"]),
            "dev_head": fixture["git"]("rev-parse", "HEAD", root=fixture["dev"]),
        }

    @pytest.mark.parametrize("case", [
        "stale_anchor", "extra_deploy_key", "missing_qa", "forged_qa",
        "failed_qa_authority", "cross_actor_qa_replay", "expired_signoff",
        "replayed_signoff", "route_ref_signoff", "broken_prior", "symlink_db",
    ])
    def test_current_main_precheck_fault_preserves_release_state(
        self, monkeypatch, tmp_path, case
    ):
        from agent.governance import server
        from agent.tests.test_graph_governance_api import (
            _main_binding_release_fixture, _main_binding_rebind_signoff,
        )

        fixture = _main_binding_release_fixture(
            monkeypatch, tmp_path, main_preimage=True
        )
        manifest = fixture["manifest"]
        baseline = server._ac_main_binding_precheck(manifest)
        assert baseline["writes_performed"] is False
        assert baseline["candidate_commit"] == fixture["candidate"]
        assert baseline["gate_event_ids"]["qa_verdict"] == fixture["qa_id"]

        if case == "stale_anchor":
            manifest["stable_anchor_commit"] = "e" * 40
        elif case == "extra_deploy_key":
            manifest["deploy"]["caller_claimed_pass"] = True
        elif case in {"missing_qa", "forged_qa", "failed_qa_authority", "cross_actor_qa_replay"}:
            with sqlite3.connect(fixture["dev_db"]) as conn:
                if case == "missing_qa":
                    conn.execute("DELETE FROM task_timeline_events WHERE id=?", (fixture["qa_id"],))
                elif case == "forged_qa":
                    conn.execute("UPDATE task_timeline_events SET event_type='qa.forged' WHERE id=?", (fixture["qa_id"],))
                elif case == "failed_qa_authority":
                    conn.execute("UPDATE sessions SET status='revoked' WHERE role='qa'")
                else:
                    row = conn.execute("SELECT * FROM task_timeline_events WHERE id=?", (fixture["qa_id"],)).fetchone()
                    columns = [item[1] for item in conn.execute("PRAGMA table_info(task_timeline_events)") if item[1] != "id"]
                    values = dict(zip([item[1] for item in conn.execute("PRAGMA table_info(task_timeline_events)")], row))
                    values.update(backlog_id="AC-OTHER", task_id="cex-other", actor="observer-replay", event_type="observer.copied_qa")
                    conn.execute(
                        f"INSERT INTO task_timeline_events({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        [values[key] for key in columns],
                    )
        elif case == "expired_signoff":
            now = datetime.now(timezone.utc).replace(microsecond=0)
            _main_binding_rebind_signoff(
                fixture,
                expires_at=(now - timedelta(minutes=1)).isoformat(),
                created_at=(now - timedelta(minutes=30)).isoformat(),
            )
        elif case == "replayed_signoff":
            with sqlite3.connect(fixture["stable_db"]) as conn:
                row = conn.execute("SELECT * FROM release_operator_head_queue_events WHERE id=?", (fixture["operator"]["queue_event_id"],)).fetchone()
                columns = [item[1] for item in conn.execute("PRAGMA table_info(release_operator_head_queue_events)") if item[1] != "id"]
                values = dict(zip([item[1] for item in conn.execute("PRAGMA table_info(release_operator_head_queue_events)")], row))
                conn.execute(
                    f"INSERT INTO release_operator_head_queue_events({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    [values[key] for key in columns],
                )
        elif case == "route_ref_signoff":
            _main_binding_rebind_signoff(
                fixture, operator_principal_id="observer:route_ref", actor="observer:route_ref"
            )
        elif case == "broken_prior":
            with sqlite3.connect(fixture["stable_db"]) as conn:
                conn.execute("DELETE FROM task_timeline_events WHERE event_type='ac.stable_promotion_completed'")
        else:
            escaped = tmp_path / "escaped-governance.db"
            fixture["stable_db"].replace(escaped)
            fixture["stable_db"].symlink_to(escaped)

        before = self._current_main_state(fixture)
        expected = {
            "extra_deploy_key": "promotion manifest does not match deployed request",
            "missing_qa": "release canonical QA line differs from its unique accepted event",
            "forged_qa": "release canonical QA line differs from its unique accepted event",
            "failed_qa_authority": "durable QA verdict is not role-bound",
            "cross_actor_qa_replay": "durable QA authority replay/ambiguity detected",
            "expired_signoff": "durable operator signoff is expired",
            "replayed_signoff": "durable operator signoff nonce replay detected",
            "route_ref_signoff": "durable operator signoff identity mismatch",
            "broken_prior": "ordinary main successor requires its predecessor receipt",
            "symlink_db": "release stable DB path changed",
        }
        if case == "stale_anchor":
            lines = self._script().read_text(encoding="utf-8").splitlines()
            start = next(i for i, line in enumerate(lines) if line == 'if [ "$MANIFEST_ANCHOR" != "$CURRENT_STABLE" ]; then')
            clause = "\n".join(lines[start:start + 3]) + "\n"
            result = subprocess.run(
                ["bash", "-c", clause],
                env={**os.environ, "MANIFEST_ANCHOR": manifest["stable_anchor_commit"],
                     "CURRENT_STABLE": fixture["instance"]["stable_anchor_commit"]},
                capture_output=True, text=True, check=False,
            )
            assert result.returncode != 0
            assert "manifest anchor is stale relative to current stable HEAD" in result.stderr
        else:
            with pytest.raises(server.ValidationError, match=expected[case]):
                server._ac_main_binding_precheck(manifest)
        assert self._current_main_state(fixture) == before

    def test_promotion_rejects_alternate_ac_shaped_shared_volume_before_mutation(
        self, monkeypatch, tmp_path
    ):
        from agent.governance import server
        from agent.tests.test_graph_governance_api import _main_binding_release_fixture

        fixture = _main_binding_release_fixture(monkeypatch, tmp_path, main_preimage=True)
        assert server._ac_main_binding_precheck(fixture["manifest"])["writes_performed"] is False
        stable = fixture["stable"]
        alternate = tmp_path / "alternate-shared"
        alternate.mkdir()
        script = self._script()
        lines = script.read_text(encoding="utf-8").splitlines()
        marker = 'LIVE_DB="$(python3 - "$STABLE_WORKTREE" "${SHARED_VOLUME_PATH}" <<\'PY\''
        start = next(i + 1 for i, line in enumerate(lines) if line == marker)
        end = next(i for i in range(start, len(lines)) if lines[i] == "PY")
        before = self._current_main_state(fixture)
        result = subprocess.run(
            [sys.executable, "-", str(stable), str(alternate)],
            input="\n".join(lines[start:end]) + "\n",
            capture_output=True, text=True, check=False,
        )
        assert result.returncode != 0
        assert "alternate SHARED_VOLUME_PATH is forbidden" in result.stderr
        assert self._current_main_state(fixture) == before


@pytest.mark.parametrize("listener_delay,health_delay,expected_failure,health_error", [
    (449, 100, False, "http"), (449, 100, False, "url"),
    (449, 152, True, "http"), (449, 152, True, "url")])
def test_activation_cold_start_uses_one_600_second_budget(monkeypatch, listener_delay, health_delay, expected_failure, health_error):
    runtime = TestExplicitACPromotionScript._activation_runtime()
    clock = {"now": 0.0}
    from types import SimpleNamespace
    runtime["time"] = SimpleNamespace(monotonic=lambda: clock["now"],
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    launch = ["/python", "-m", "agent.cli"]
    identity = {"pid": 1234, "birth": "same-birth", "command": "/python -m agent.cli"}
    class Ops:
        def pid_identity(self, _pid): return dict(identity)
        def pid_alive(self, _pid): return True
        def port_pids(self, _port): return [1234] if clock["now"] >= listener_delay else []
    machine = object.__new__(runtime["ActivationMachine"])
    machine.plan = {"runtime_process_executable": "/python", "stable_port": 40000}
    machine.ops = Ops()
    healthy_at = listener_delay + health_delay
    def health(_commit, _pid, *, legacy=False):
        if clock["now"] < healthy_at:
            if health_error == "http":
                from urllib.error import HTTPError
                raise HTTPError("http://127.0.0.1:40000/api/health", 503, "warming", {}, None)
            if health_error == "url":
                from urllib.error import URLError
                raise URLError("warming")
            runtime["fail"]("activation_health_identity_mismatch", "wrong exact health")
        return {"status": "ok"}
    machine.exact_health = health
    assert machine.exact_started_process(1234, launch, "listener_pending") == identity
    if expected_failure:
        with pytest.raises(runtime["PromotionFailure"], match="did not become available"):
            machine.poll_health("candidate", 1234)
        assert clock["now"] < 601
    else:
        assert machine.poll_health("candidate", 1234) == {"status": "ok"}
        assert clock["now"] >= healthy_at
        machine.ops.port_pids = lambda _port: []
        with pytest.raises(runtime["PromotionFailure"]) as drift:
            machine.poll_health("candidate", 1234)
        assert drift.value.code == "activation_listener_identity_drift"
        machine.ops.port_pids = lambda _port: [1234]
        machine.exact_health = lambda *_args, **_kwargs: runtime["fail"](
            "activation_health_identity_mismatch", "wrong loaded source")
        before = clock["now"]
        with pytest.raises(runtime["PromotionFailure"]) as mismatch:
            machine.poll_health("candidate", 1234)
        assert mismatch.value.code == "activation_health_identity_mismatch"
        assert clock["now"] == before
        # Rollback starts a fresh bounded wait for its distinct old process.
        identity.update(pid=1235, birth="rollback-birth")
        machine.ops.port_pids = lambda _port: [1235]
        assert machine.exact_started_process(1235, launch, "rollback_pending") == identity
        assert machine._cold_start_deadline == pytest.approx(clock["now"] + 600)


@pytest.mark.parametrize("first_main", [False, True])
def test_main_binding_runs_exact_shell_precheck_before_legacy_timeline_branch(monkeypatch, tmp_path, first_main):
    """Execute the actual promotion_precheck heredoc; the v1/main path exits early."""
    from contextlib import redirect_stdout
    from io import StringIO
    from agent.tests.test_graph_governance_api import _main_binding_release_fixture
    from agent.governance import server
    fixture = _main_binding_release_fixture(monkeypatch, tmp_path, main_preimage=True, first_main=first_main)
    manifest = fixture["manifest"]
    script = TestExplicitACPromotionScript._script()
    lines = script.read_text().splitlines()
    start = next(i + 1 for i, line in enumerate(lines) if line.startswith('python3 - "$MANIFEST" "$LIVE_DB"'))
    end = next(i for i in range(start, len(lines)) if lines[i] == "PY")
    manifest_path = tmp_path / "promotion-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    verifier = "sha256:" + hashlib.sha256((fixture["dev"] / "scripts/merge-and-deploy.sh").read_bytes()).hexdigest()
    args = ["promotion_precheck", str(manifest_path), str(fixture["stable_db"]),
            manifest["stable_anchor_commit"], manifest["candidate_commit"],
            manifest["diff_sha256"], verifier, server.AC_STABLE_ANCHOR_COMMIT,
            json.dumps(manifest["stable_database_identity"])]
    output = StringIO()
    with monkeypatch.context() as boundary, redirect_stdout(output):
        boundary.setattr(sys, "argv", args)
        with pytest.raises(SystemExit) as completed:
            exec(compile("\n".join(lines[start:end]) + "\n", str(script), "exec"),
                 {"__name__": "main_promotion_precheck_fixture"})
    assert completed.value.code == 0
    receipt = json.loads(output.getvalue())
    assert receipt == server._ac_main_binding_precheck(manifest)
    if first_main:
        assert manifest["prior_promotion"]["continuity"] == "legacy_first_main"
        assert receipt["previous_promotion_receipt_hash"] is None


def test_main_binding_prepare_preserves_real_venv_launcher_for_candidate_and_rollback(monkeypatch, tmp_path):
    from agent.tests.test_graph_governance_api import _main_binding_release_fixture
    from agent.governance import server

    fixture = _main_binding_release_fixture(monkeypatch, tmp_path, main_preimage=True)
    stable, dev = fixture["stable"], fixture["dev"]
    manifest = fixture["manifest"]
    venv_python = tmp_path / "repo-runtime" / ".venv" / "bin" / "python"
    created = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_python.parent.parent)],
        capture_output=True, text=True, check=False,
    )
    assert created.returncode == 0, created.stderr
    assert venv_python.is_symlink()
    site_packages = subprocess.check_output(
        [str(venv_python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        text=True,
    ).strip()
    (Path(site_packages) / "ac_venv_launcher_sentinel.py").write_text("VALUE = 'venv-only'\n")

    script = TestExplicitACPromotionScript._script()
    runtime = TestExplicitACPromotionScript._activation_runtime()
    launch_env = runtime["scrubbed_environment"]({
        "PYTHONPATH": str(stable), "SHARED_VOLUME_PATH": str(stable / "shared-volume"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    bare_import = subprocess.run(
        [str(venv_python.resolve()), "-c", "import ac_venv_launcher_sentinel"],
        cwd=stable, env=launch_env, capture_output=True, text=True, check=False,
    )
    assert bare_import.returncode != 0
    assert "No module named 'ac_venv_launcher_sentinel'" in bare_import.stderr
    observed = subprocess.check_output(
        [str(venv_python), "-c", "import os, subprocess; "
         "print(subprocess.check_output(['ps', '-p', str(os.getpid()), '-o', 'command='], "
         "text=True).split()[0])"],
        cwd=stable, env=launch_env, text=True,
    ).strip()
    assert observed.startswith("/") and observed != str(venv_python)
    fixture["old_process"]["command"] = " ".join(
        [observed, *fixture["old_launch"][1:]]
    )

    precheck = server._ac_main_binding_precheck(manifest)
    manifest_path = tmp_path / "venv-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    plan_path = tmp_path / "venv-activation.json"
    lines = script.read_text().splitlines()
    start = next(i + 1 for i, line in enumerate(lines)
                 if 'python3 - "$ACTIVATION_PLAN" "$DRY_RUN"' in line)
    end = next(i for i in range(start, len(lines)) if lines[i] == "PY")
    prepare_source = "\n".join(lines[start:end]) + "\n"
    real_run = subprocess.run

    def observed_process_boundary(args, **kwargs):
        if args[0] == "ps":
            field = "birth" if args[-1] == "lstart=" else "command"
            return subprocess.CompletedProcess(args, 0,
                stdout=fixture["old_process"][field] + "\n", stderr="")
        if args[0] == "lsof":
            return subprocess.CompletedProcess(args, 0,
                stdout=str(fixture["old_process"]["pid"]) + "\n", stderr="")
        return real_run(args, **kwargs)

    prepare_argv = ["prepare", str(plan_path), "false", str(manifest_path),
        json.dumps(precheck), str(stable), str(dev), manifest["stable_anchor_commit"],
        fixture["candidate"], json.dumps(manifest["stable_database_identity"]),
        str(fixture["stable_db"]), str(fixture["old_process"]["pid"]),
        str(venv_python), precheck["verifier_sha256"], "40000"]
    with monkeypatch.context() as boundary:
        boundary.setattr(subprocess, "run", observed_process_boundary)
        boundary.setattr(sys, "argv", prepare_argv)
        exec(compile(prepare_source, str(script), "exec"), {"__name__": "venv_prepare_fixture"})
    plan = json.loads(plan_path.read_bytes())
    assert plan["candidate_launch_spec"][0] == str(venv_python)
    assert plan["old_launch_spec"][0] == str(venv_python)
    assert plan["runtime_process_executable"] == observed
    assert plan["old_process"]["command"].split()[0] == observed
    with monkeypatch.context() as boundary:
        boundary.setattr(sys, "argv", ["script", "activate", str(plan_path), "false", str(script)])
        runtime["validate_plan"](plan, plan_path.read_bytes(), str(script))
        wrong_argv = copy.deepcopy(plan)
        wrong_argv["old_process"]["command"] += " --unexpected"
        wrong_argv["plan_hash"] = runtime["sha"]({
            key: value for key, value in wrong_argv.items() if key != "plan_hash"
        })
        wrong_bytes = (runtime["canonical"](wrong_argv) + "\n").encode()
        with pytest.raises(runtime["PromotionFailure"]) as rejected:
            runtime["validate_plan"](wrong_argv, wrong_bytes, str(script))
        assert rejected.value.code == "activation_plan_launch_spec_invalid"

    # The fixture CLI exercises the plan's interpreter and sanitized environment
    # without starting a governance listener on the stable port.
    (stable / "ac_venv_launch_probe.py").write_text(
        "import ac_venv_launcher_sentinel, json, os, subprocess, sys\n"
        "physical = subprocess.check_output(['ps', '-p', str(os.getpid()), '-o', 'command='], "
        "text=True).split()[0]\n"
        "print(json.dumps({'sentinel': ac_venv_launcher_sentinel.VALUE, "
        "'physical': physical, 'argv': sys.argv[1:]}))\n"
    )
    for spec_key, env_key in (("candidate_launch_spec", "candidate_launch_environment"),
                              ("old_launch_spec", "old_launch_environment")):
        spec = plan[spec_key]
        env = runtime["scrubbed_environment"](plan[env_key])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        launched = subprocess.run(
            [spec[0], "-m", "ac_venv_launch_probe", *spec[4:]],
            cwd=stable, env=env, capture_output=True, text=True, check=False,
        )
        assert launched.returncode == 0, (spec_key, launched.stderr)
        result = json.loads(launched.stdout)
        assert result["sentinel"] == "venv-only"
        assert result["physical"] == plan["runtime_process_executable"]
        assert "--runtime-plane" in result["argv"]
        assert "stable" in result["argv"]


@pytest.mark.parametrize("main_preimage,fault,first_main", [(False, "none", False), (False, "candidate_start", False), (False, "after_branch", False), (False, "after_source", False), (True, "none", False), (True, "after_source", False), (True, "none", True), (True, "after_source", True)])
def test_main_binding_create_prepare_activate_and_exact_rollback(monkeypatch, tmp_path, main_preimage, fault, first_main):
    from agent.tests.test_graph_governance_api import _main_binding_release_fixture
    from agent.governance import server, db
    fixture = _main_binding_release_fixture(monkeypatch, tmp_path, main_preimage=main_preimage, first_main=first_main)
    # Real authentication in the physical fixture control DB, never a cached role.
    with sqlite3.connect(fixture["stable_db"]) as conn:
        conn.row_factory = sqlite3.Row
        wrong_role = server.role_service.register(conn, "observer-fixture", "aming-claw", "observer")
        wrong_project = server.role_service.register(conn, "external-coordinator", "external-project", "coordinator")
    manifest = fixture["manifest"]
    stable, dev = fixture["stable"], fixture["dev"]
    anchor, candidate = manifest["stable_anchor_commit"], fixture["candidate"]
    old_branch = fixture["instance"]["stable_branch_preimage"]
    script = TestExplicitACPromotionScript._script()
    runtime = TestExplicitACPromotionScript._activation_runtime()
    precheck = server._ac_main_binding_precheck(manifest)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    plan_path = tmp_path / "main-activation.json"
    source_lines = script.read_text().splitlines()
    start = next(i + 1 for i, line in enumerate(source_lines) if 'python3 - "$ACTIVATION_PLAN" "$DRY_RUN"' in line)
    end = next(i for i in range(start, len(source_lines)) if source_lines[i] == "PY")
    prepare_source = "\n".join(source_lines[start:end]) + "\n"
    run = subprocess.run
    def prepare_process_boundary(args, **kwargs):
        if args[0] == "ps":
            output = fixture["old_process"]["birth" if args[-1] == "lstart=" else "command"] + "\n"
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        if args[0] == "lsof":
            return subprocess.CompletedProcess(args, 0, stdout=str(fixture["old_process"]["pid"]) + "\n", stderr="")
        return run(args, **kwargs)
    args = ["prepare", str(plan_path), "false", str(manifest_path), json.dumps(precheck), str(stable), str(dev), anchor, candidate,
            json.dumps(manifest["stable_database_identity"]), str(fixture["stable_db"]), str(fixture["old_process"]["pid"]),
            str(Path(sys.executable).resolve()), precheck["verifier_sha256"], "40000"]
    with monkeypatch.context() as boundary:
        boundary.setattr(subprocess, "run", prepare_process_boundary)
        boundary.setattr(sys, "argv", args)
        exec(compile(prepare_source, str(script), "exec"), {"__name__": "main_prepare_fixture"})
    plan = json.loads(plan_path.read_bytes())
    assert plan["manifest"] == manifest
    assert plan["stable_branch"] == "main"
    assert plan["schema_version"] == "ac_stable_activation_plan.v2"
    assert plan["completion_body_template"]["schema_version"] == "ac_stable_promotion_completion.v1"
    assert fixture["git"]("branch", "--show-current") == old_branch
    with monkeypatch.context() as boundary:
        boundary.setattr(sys, "argv", ["script", "activate", str(plan_path), "false", str(script)])
        patch = runtime["validate_plan"](plan, plan_path.read_bytes(), str(script))
    before_db = fixture["stable_db"].read_bytes()
    preimage_health = copy.deepcopy(fixture["health"])
    class ProcessBoundary(runtime["RealOps"]):
        def __init__(self):
            self.identity = dict(plan["old_process"])
            self.loaded = anchor
            self.calls = []
            self.failed = False
            self.completed = False
        def run(self, args, cwd, code, *, input_bytes=None):
            self.calls.append(tuple(args))
            result = super().run(args, cwd, code, input_bytes=input_bytes)
            if not self.failed and ((fault == "after_branch" and args[:3] == ["git", "switch", "main"])
                    or (fault == "after_source" and args[:3] == ["git", "merge", "--ff-only"])):
                self.failed = True
                runtime["fail"]("fixture_crash_after_mutation", fault)
            return result
        def pid_identity(self, pid):
            return dict(self.identity) if self.identity.get("pid") == pid else {}
        def pid_alive(self, pid):
            return self.identity.get("pid") == pid
        def port_pids(self, port):
            assert port == 40000
            return [self.identity["pid"]] if self.identity else []
        def health(self, port):
            assert port == 40000
            health = copy.deepcopy(preimage_health)
            source_hash = plan["stable_runtime_source_sha256"] if self.loaded == anchor else plan["candidate_runtime_source_sha256"]
            branch = old_branch if self.loaded == anchor else "main"
            health.update({"pid": self.identity["pid"], "runtime_loaded_version": self.loaded, "runtime_loaded_source_sha256": source_hash})
            health["runtime_plane_identity"].update({"branch": branch, "expected_branch": branch, "commit": self.loaded, "stable_anchor_commit": self.loaded})
            if self.loaded == candidate:
                with monkeypatch.context() as boundary:
                    self.stable_runtime(boundary)
                    health["runtime_plane_identity"] = server._runtime_plane_identity()
                assert health["runtime_plane_identity"]["status"] == "ready"
            health["loaded_runtime_identity"].update({"loaded_commit": self.loaded, "loaded_source_sha256": source_hash, "worktree_source_sha256": source_hash})
            return health
        def stable_runtime(self, boundary):
            boundary.setenv("AMING_CLAW_RUNTIME_PLANE", "stable")
            boundary.setenv("AMING_CLAW_HOME", str(stable))
            boundary.setenv("SHARED_VOLUME_PATH", str(stable / "shared-volume"))
            boundary.setenv("AMING_CLAW_STABLE_ANCHOR_COMMIT", candidate)
            boundary.delenv(server.BUILD_COMMIT_ENV, raising=False)
            boundary.setattr(server, "__file__", str(stable / "agent/governance/server.py"))
            boundary.setattr(server, "PORT", 40000)
            boundary.setattr(server, "SERVER_PID", self.identity["pid"])
        def stop(self, identity, port, code, **kwargs):
            assert self.identity == identity and port == 40000
            self.identity = {}
        def start(self, launch, cwd, log, environment):
            assert cwd == str(stable)
            if fault == "candidate_start" and launch == plan["candidate_launch_spec"]:
                runtime["fail"]("fixture_candidate_start_failed", "isolated process boundary")
            self.loaded = candidate if launch == plan["candidate_launch_spec"] else anchor
            self.identity = {"pid": 52002 if self.loaded == candidate else 52003, "birth": "fixture-new-birth", "command": " ".join([plan["runtime_process_executable"], *launch[1:]])}
            fixture["health"].clear(); fixture["health"].update(self.health(40000))
            return self.identity["pid"]
        def lane(self, command, cwd):
            assert command in plan["lane_commands"] and cwd in {str(stable), str(dev)}
        def reproject(self, selected, script_path):
            return server._ac_main_binding_precheck(selected["manifest"])
        def complete(self, body, token):
            previous_receipt = manifest["prior_promotion"].get("receipt_hash")
            assert body["previous_promotion_receipt_hash"] == previous_receipt
            assert server._ac_promotion_request_previous_receipt(body, previous_stable=anchor) == previous_receipt
            def request(credential):
                ctx = server.RequestContext(None, "POST", {"project_id": "aming-claw"}, {}, body, "isolated-release", credential, "")
                server._guard_runtime_world_request(method="POST", path="/api/projects/aming-claw/ac-stable-promotion/complete",
                    path_params=ctx.path_params, body=body, query={}, token=credential)
                return server.handle_ac_stable_promotion_complete(ctx)
            with monkeypatch.context() as boundary:
                self.stable_runtime(boundary)
                for rejected in (wrong_role["token"], wrong_project["token"]):
                    with pytest.raises(server.PermissionDeniedError):
                        request(rejected)
                with pytest.raises(ValueError, match="stable runtime rejects the AC project domain"):
                    db.get_connection("aming-claw")
                receipt = request(token)
                replay = request(token)
            assert receipt["idempotent"] is False
            assert replay["idempotent"] is True
            assert receipt["timeline_event_id"] == replay["timeline_event_id"]
            assert receipt["promotion_receipt_hash"] == replay["promotion_receipt_hash"]
            self.completed = True
            return receipt
    ops = ProcessBoundary()
    for module in (fixture["db"], db):
        monkeypatch.setattr(module, "_stable_process_identity", lambda pid: (ops.pid_identity(pid).get("birth", ""), ops.pid_identity(pid).get("command", ""), str(stable)))
    journal = runtime["Journal"](plan["journal_path"], plan["plan_hash"])
    result = runtime["ActivationMachine"](plan, patch, ops, journal, fixture["operator_token"], str(script)).activate()
    assert result["ok"] is (fault == "none")
    assert ops.completed is (fault == "none")
    if not main_preimage:
        assert fixture["git"]("rev-parse", "refs/heads/" + old_branch) == anchor
    assert fixture["git"]("status", "--porcelain") == ""
    assert fixture["git"]("rev-parse", "HEAD", root=dev) == candidate
    assert fixture["git"]("status", "--porcelain", root=dev) == ""
    if fault == "none":
        assert fixture["git"]("branch", "--show-current") == "main"
        assert fixture["git"]("rev-parse", "refs/heads/main") == candidate
        assert journal.rows[-1]["state"] == "COMPLETED"
        with sqlite3.connect(fixture["stable_db"]) as conn:
            rows = conn.execute("SELECT payload_json FROM task_timeline_events WHERE event_type='ac.stable_promotion_completed'").fetchall()
        assert len(rows) == (2 if main_preimage and not first_main else 1)
        receipt = json.loads(rows[-1][0])
        assert receipt["prior_promotion"] == manifest["prior_promotion"]
        assert receipt["stable_branch"] == "main"
        assert receipt["previous_promotion_receipt_hash"] == manifest["prior_promotion"].get("receipt_hash")
        if first_main:
            # The emitted receipt, rather than a seeded fixture row, is the
            # exact predecessor selected by the next normal main release.
            with sqlite3.connect(fixture["stable_db"]) as conn:
                conn.row_factory = sqlite3.Row
                emitted = server._ac_promotion_completion_events(conn, "aming-claw")
            next_prior = server._ac_main_binding_receipt_predecessor(
                [row for row in emitted if row["commit_sha"] == candidate],
                candidate, manifest["stable_database_identity"])
            assert next_prior == {"kind": "timeline_receipt",
                "timeline_event_id": emitted[0]["id"],
                "receipt_hash": receipt["promotion_receipt_hash"]}
            next_body = {"promotion_manifest": {"prior_promotion": next_prior},
                "precheck_receipt": {"previous_promotion_receipt_hash": next_prior["receipt_hash"],
                    "prior_promotion_event_id": next_prior["timeline_event_id"]},
                "previous_promotion_receipt_hash": next_prior["receipt_hash"]}
            assert server._ac_promotion_request_previous_receipt(
                next_body, previous_stable=candidate) == next_prior["receipt_hash"]
            with pytest.raises(server.ValidationError, match="ambiguous"):
                server._ac_main_binding_receipt_predecessor(
                    [emitted[0], emitted[0]], candidate, manifest["stable_database_identity"])
    else:
        assert fixture["stable_db"].read_bytes() == before_db
        assert result["rolled_back"] is True
        assert fixture["git"]("branch", "--show-current") == old_branch
        assert fixture["git"]("rev-parse", "refs/heads/main") == anchor
        assert journal.rows[-1]["state"] == "ROLLED_BACK"
    assert not any("reset" in call or "clean" in call for call in ops.calls)
