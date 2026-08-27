"""Tests for deploy_chain.restart_local_governance (B7 fix).

AC8: Tests cover:
  (a) process crashes immediately after start
  (b) health check retries succeed on 3rd attempt
  (c) port not released in time
  (d) stderr content included in failure summary
"""
import json
import hashlib
import ast
import logging
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
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

    @patch("agent.deploy_chain._state_dir")
    def test_restart_executor_writes_signal(self, mock_state_dir, tmp_path, caplog):
        """AC3+AC4: signal file has correct keys and log line emitted."""
        from agent.deploy_chain import restart_executor

        mock_state_dir.return_value = tmp_path

        with caplog.at_level(logging.INFO, logger="agent.deploy_chain"):
            result = restart_executor()

        assert result is True

        signal_file = tmp_path / "manager_signal.json"
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
        assert 'STABLE_BRANCH="codex/direct-no-pass-post-reconcile-r2"' in text
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
        assert 'refs/heads/main' not in text
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

    @staticmethod
    def _negative_promotion_fixture(tmp_path: Path, case: str) -> tuple[Path, Path, str]:
        source_script = TestExplicitACPromotionScript._script()
        stable_root = tmp_path / "stable"
        dev_root = tmp_path / "dev"
        stable_root.mkdir()

        def run(args, cwd, *, text=True):
            result = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=text,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            return result.stdout

        run(["git", "init", "-b", "codex/direct-no-pass-post-reconcile-r2"], stable_root)
        run(["git", "config", "user.email", "test@example.com"], stable_root)
        run(["git", "config", "user.name", "Test"], stable_root)
        for path in (
            stable_root / "agent" / "cli.py",
            stable_root / "agent" / "governance" / "db.py",
            stable_root / "agent" / "governance" / "server.py",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture\n", encoding="utf-8")
        script = stable_root / "scripts" / "merge-and-deploy.sh"
        script.parent.mkdir(parents=True)
        script.write_bytes(source_script.read_bytes())
        script.chmod(0o755)
        (stable_root / "change.txt").write_text("base\n", encoding="utf-8")
        (stable_root / ".gitignore").write_text(
            "shared-volume/\n", encoding="utf-8"
        )
        run(["git", "add", "."], stable_root)
        run(["git", "commit", "-m", "base"], stable_root)
        stable = run(["git", "rev-parse", "HEAD"], stable_root).strip()
        run(
            ["git", "worktree", "add", "-b", "codex/ac-dev", str(dev_root), stable],
            stable_root,
        )
        (dev_root / "change.txt").write_text("candidate\n", encoding="utf-8")
        run(["git", "add", "change.txt"], dev_root)
        run(["git", "commit", "-m", "candidate"], dev_root)
        candidate = run(["git", "rev-parse", "HEAD"], dev_root).strip()
        diff = subprocess.run(
            [
                "git", "diff", "--no-ext-diff", "--no-textconv", "--binary",
                "--full-index", "-M", f"{stable}..{candidate}", "--", ".",
            ],
            cwd=dev_root,
            capture_output=True,
            check=True,
        ).stdout
        diff_hash = "sha256:" + hashlib.sha256(diff).hexdigest()
        verifier_hash = "sha256:" + hashlib.sha256(
            (dev_root / "scripts" / "merge-and-deploy.sh").read_bytes()
        ).hexdigest()
        fence = ["change.txt"]
        deploy = {"authorized": True, "mode": "host_supervisor", "stable_port": 40000}
        if case == "extra_deploy_key":
            deploy["caller_claimed_pass"] = True
        backlog_id = "AC-SCRIPT-E2E"
        cex = "cex-script-e2e"
        intent = {
            "schema_version": "ac_stable_promotion_manifest.v1",
            "project_id": "aming-claw",
            "backlog_id": backlog_id,
            "contract_execution_id": cex,
            "stable_anchor_commit": stable,
            "stable_branch": "codex/direct-no-pass-post-reconcile-r2",
            "branch": "codex/ac-dev",
            "candidate_commit": candidate,
            "file_fence": fence,
            "diff_sha256": diff_hash,
            "deploy": deploy,
        }

        def sha(value):
            return "sha256:" + hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()

        shared = stable_root / "shared-volume"
        db_path = (
            shared / "codex-tasks" / "state" / "governance" / "aming-claw" / "governance.db"
        )
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(db_path)
        metadata = db_path.stat()
        database_identity = {
            "schema_version": "ac_stable_database_identity.v1",
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "stable_relative_path_sha256": "sha256:"
            + hashlib.sha256(
                b"shared-volume/codex-tasks/state/governance/aming-claw/governance.db"
            ).hexdigest(),
        }
        intent["stable_database_identity"] = database_identity
        intent_hash = sha(intent)
        conn.executescript(
            """
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES('schema_version', '47');
            CREATE TABLE task_timeline_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, backlog_id TEXT,
                task_id TEXT, event_type TEXT, phase TEXT, event_kind TEXT, actor TEXT,
                status TEXT, payload_json TEXT, verification_json TEXT,
                artifact_refs_json TEXT, commit_sha TEXT, created_at TEXT
            );
            CREATE TABLE release_operator_head_queue_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, action TEXT,
                backlog_id TEXT, actor TEXT, reason TEXT, before_json TEXT,
                after_json TEXT, created_at TEXT
            );
            CREATE TABLE sessions(
                session_id TEXT PRIMARY KEY, principal_id TEXT, project_id TEXT,
                role TEXT, scope_json TEXT, status TEXT
            );
            CREATE TABLE graph_snapshots(
                project_id TEXT, snapshot_id TEXT, commit_sha TEXT,
                PRIMARY KEY(project_id, snapshot_id)
            );
            CREATE TABLE graph_query_traces(
                trace_id TEXT PRIMARY KEY, project_id TEXT, snapshot_id TEXT,
                actor TEXT, query_source TEXT, query_purpose TEXT, task_id TEXT,
                backlog_id TEXT, commit_sha TEXT, qa_session_id TEXT,
                qa_scope_binding_ref TEXT, status TEXT
            );
            """
        )
        previous_receipt = "sha256:" + "7" * 64
        prior_event_id = 888
        if case != "broken_prior":
            conn.execute(
                """INSERT INTO task_timeline_events(
                       id, project_id, backlog_id, task_id, event_type, phase,
                       event_kind, actor, status, payload_json, verification_json,
                       artifact_refs_json, commit_sha, created_at
                   ) VALUES (?, 'aming-claw', 'AC-PRIOR', 'cex-prior',
                       'ac.stable_promotion_completed', 'release', 'stable_promotion',
                       'operator', 'accepted', ?, '{}', '{}', ?, ?)""",
                (
                    prior_event_id,
                    json.dumps(
                        {
                            "promoted_commit": stable,
                            "promotion_receipt_hash": previous_receipt,
                            "stable_database_identity": database_identity,
                        },
                        sort_keys=True,
                    ),
                    stable,
                    "2026-08-27T13:00:00Z",
                ),
            )
        qa_event_id = 999
        if case != "missing_qa":
            qa_principal = "qa-e2e"
            qa_session_id = "qa-session-e2e"
            qa_scope_binding_ref = "qa-scope-e2e"
            qa_snapshot_id = "full-e2e-candidate"
            qa_trace_id = "gqt-e2e-independent-verification"
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, 'aming-claw', 'qa', ?, 'active')",
                (
                    qa_session_id,
                    qa_principal,
                    json.dumps([qa_scope_binding_ref]),
                ),
            )
            conn.execute(
                "INSERT INTO graph_snapshots VALUES ('aming-claw', ?, ?)",
                (qa_snapshot_id, candidate),
            )
            conn.execute(
                """INSERT INTO graph_query_traces VALUES(
                       ?, 'aming-claw', ?, ?, 'qa', 'independent_verification',
                       ?, ?, ?, ?, ?, 'complete')""",
                (
                    qa_trace_id,
                    qa_snapshot_id,
                    qa_principal,
                    cex,
                    backlog_id,
                    candidate,
                    qa_session_id,
                    qa_scope_binding_ref,
                ),
            )
            qa_proof = {
                "schema_version": "qa_session_scope_proof.v1",
                "source": "authenticated_qa_session",
                "role": "qa",
                "verified": True,
                "observer_impersonation": False,
                "evidence_status": (
                    "failed" if case == "failed_qa_authority" else "passed"
                ),
                "authority_scope": "close_satisfying",
                "close_satisfying": True,
                "audit_only": False,
                "passing_status_required_for_close": True,
                "db_verified_graph_trace": True,
                "query_source": "qa",
                "query_purpose": "independent_verification",
                "project_id": "aming-claw",
                "backlog_id": backlog_id,
                "task_id": cex,
                "commit_sha": candidate,
                "principal_id": qa_principal,
                "qa_session_id": qa_session_id,
                "qa_scope_binding_ref": qa_scope_binding_ref,
                "snapshot_id": qa_snapshot_id,
                "snapshot_commit_sha": candidate,
                "graph_trace_ids": [qa_trace_id],
                "candidate_review_context": {
                    "candidate_commit_sha": candidate,
                    "comparison_base_commit_sha": stable,
                    "comparison_authority_required": True,
                    "candidate_diff_hash": diff_hash,
                    "changed_files": fence,
                },
            }
            authority = {
                "schema_version": "source_backed_contract_gate_authority.v1",
                "source": "server_qa_session_verification",
                "source_of_authority": "qa_session_verification",
                "authority_scope": "close_satisfying",
                "close_satisfying": True,
                "audit_only": False,
                "qa_session_proof": qa_proof,
            }
            authority["authority_hash"] = sha(authority)
            qa_payload = {
                "source_backed_contract_gate_authority": authority,
                "contract_runtime_canonical_line": {
                    "stage_id": "qa",
                    "line_id": "qa_independent_verification",
                    "contract_execution_id": cex,
                    "runtime_guide_hash": "sha256:" + "5" * 64,
                },
                "stable_anchor_commit": stable,
                "promotion_intent_sha256": intent_hash,
                "file_fence": fence,
                "stable_database_identity": database_identity,
            }
            report_hash = "sha256:" + "4" * 64
            qa_verification = {
                "pass_synthesized": False,
                "promotion_gate_results": {
                    "branch_service": {
                        "test_id": "branch-loopback", "status": "passed",
                        "report_sha256": report_hash, "runtime_plane": "dev",
                        "port": 40008, "bind_host": "127.0.0.1",
                    },
                    "lanes": {
                        lane: {
                            "test_id": f"lane-{lane}", "status": "passed",
                            "report_sha256": report_hash,
                        }
                        for lane in ("direct_main", "mf_parallel", "mf_batch_parallel")
                    },
                },
            }
            conn.execute(
                """INSERT INTO task_timeline_events(
                       id, project_id, backlog_id, task_id, event_type, phase,
                       event_kind, actor, status, payload_json, verification_json,
                       artifact_refs_json, commit_sha, created_at
                   ) VALUES (?, 'aming-claw', ?, ?, ?, 'qa',
                       'independent_verification', ?, 'passed', ?, ?, '{}', ?, ?)""",
                (
                    qa_event_id,
                    backlog_id,
                    cex,
                    "qa.forged" if case == "forged_qa" else "qa.independent_verification",
                    qa_principal,
                    json.dumps(qa_payload, sort_keys=True),
                    json.dumps(qa_verification, sort_keys=True),
                    candidate,
                    "2026-08-27T14:00:00Z",
                ),
            )
            if case == "cross_actor_qa_replay":
                conn.execute(
                    """INSERT INTO task_timeline_events(
                           project_id, backlog_id, task_id, event_type, phase,
                           event_kind, actor, status, payload_json,
                           verification_json, artifact_refs_json, commit_sha,
                           created_at
                       ) VALUES ('aming-claw', 'AC-OTHER', 'cex-other',
                           'observer.copied_qa', 'observer', 'copied_qa',
                           'observer-principal', 'accepted', '{}', ?, '{}', ?, ?)""",
                    (
                        json.dumps(
                            {"source_backed_contract_gate_authority": authority},
                            sort_keys=True,
                        ),
                        candidate,
                        "2026-08-27T14:01:00Z",
                    ),
                )
        qa_gate = {"timeline_event_id": qa_event_id, "status": "passed"}
        now = datetime.now(timezone.utc)
        created = now - timedelta(hours=2) if case == "expired_signoff" else now
        expires = created + timedelta(minutes=30)
        operator_base = {
            "status": "approved",
            "nonce": "6" * 32,
            "operator_principal_id": (
                "observer:route_ref"
                if case == "route_ref_signoff"
                else "operator-e2e"
            ),
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        prior = {
            "kind": "timeline_receipt",
            "timeline_event_id": prior_event_id,
            "receipt_hash": previous_receipt,
        }
        manifest_hash = sha(
            {
                **intent,
                "promotion_intent_sha256": intent_hash,
                "prior_promotion": prior,
                "gates": {
                    "qa_verdict": qa_gate,
                    "operator_signoff": operator_base,
                },
            }
        )
        signoff = {
            "schema_version": "ac_stable_promotion_operator_signoff.v1",
            "nonce": operator_base["nonce"],
            "operator_principal_id": operator_base["operator_principal_id"],
            "expires_at": operator_base["expires_at"],
            "project_id": "aming-claw",
            "backlog_id": backlog_id,
            "contract_execution_id": cex,
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
        reason = json.dumps(signoff, sort_keys=True, separators=(",", ":"))
        before_after = json.dumps({"backlog_ids": [backlog_id]}, sort_keys=True)
        signoff_cursor = conn.execute(
            """INSERT INTO release_operator_head_queue_events(
                   project_id, action, backlog_id, actor, reason,
                   before_json, after_json, created_at
               ) VALUES ('aming-claw', 'reorder', '', ?, ?, ?, ?, ?)""",
            (
                operator_base["operator_principal_id"],
                reason,
                before_after,
                before_after,
                created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )
        signoff_event_id = int(signoff_cursor.lastrowid)
        if case == "replayed_signoff":
            conn.execute(
                """INSERT INTO release_operator_head_queue_events(
                       project_id, action, backlog_id, actor, reason,
                       before_json, after_json, created_at
                   ) VALUES ('aming-claw', 'reorder', '', ?, ?, ?, ?, ?)""",
                (
                    operator_base["operator_principal_id"], reason,
                    before_after, before_after,
                    created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
        conn.commit()
        conn.close()
        if case == "symlink_db":
            escaped_db = tmp_path / "escaped-governance.db"
            db_path.replace(escaped_db)
            db_path.symlink_to(escaped_db)
        manifest = {
            **intent,
            "promotion_intent_sha256": intent_hash,
            "promotion_manifest_sha256": manifest_hash,
            "gates": {
                "qa_verdict": qa_gate,
                "operator_signoff": {
                    **operator_base,
                    "queue_event_id": signoff_event_id,
                },
            },
            "prior_promotion": prior,
        }
        if case == "stale_anchor":
            manifest["stable_anchor_commit"] = "e" * 40
        manifest_path = tmp_path / f"manifest-{case}.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        return dev_root / "scripts" / "merge-and-deploy.sh", manifest_path, stable

    @pytest.mark.parametrize(
        ("case", "expected"),
        [
            ("stale_anchor", "manifest anchor is stale"),
            ("extra_deploy_key", "deploy identity mismatch"),
            ("missing_qa", "timeline event 999 is missing"),
            ("forged_qa", "QA event_type mismatch"),
            (
                "failed_qa_authority",
                "QA verdict lacks role-bound server authority",
            ),
            ("cross_actor_qa_replay", "QA authority replay/ambiguity"),
            ("expired_signoff", "operator signoff is expired"),
            ("replayed_signoff", "nonce replay/ambiguity"),
            (
                "route_ref_signoff",
                "operator signoff is not an authenticated stable no-op queue decision",
            ),
            ("broken_prior", "timeline event 888 is missing"),
            ("symlink_db", "live AC database cannot be a symlink"),
        ],
    )
    def test_negative_promotion_e2e_never_mutates_stable(
        self, tmp_path, case, expected
    ):
        script, manifest, stable = self._negative_promotion_fixture(tmp_path, case)
        result = subprocess.run(
            [str(script), "--promotion-manifest", str(manifest), "--dry-run"],
            cwd=script.parents[1],
            env={
                **os.environ,
                "SHARED_VOLUME_PATH": str(
                    tmp_path / "stable" / "shared-volume"
                ),
            },
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert expected in (result.stderr + result.stdout)
        stable_root = tmp_path / "stable"
        assert subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=stable_root, text=True
        ).strip() == stable

    def test_promotion_rejects_alternate_ac_shaped_shared_volume_before_mutation(
        self, tmp_path
    ):
        script, manifest, stable = self._negative_promotion_fixture(
            tmp_path, "broken_prior"
        )
        alternate = tmp_path / "alternate-shared"
        alternate.mkdir()

        result = subprocess.run(
            [str(script), "--promotion-manifest", str(manifest), "--dry-run"],
            cwd=script.parents[1],
            env={**os.environ, "SHARED_VOLUME_PATH": str(alternate)},
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert "alternate SHARED_VOLUME_PATH is forbidden" in (
            result.stderr + result.stdout
        )
        assert subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path / "stable",
            text=True,
        ).strip() == stable
