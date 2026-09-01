import os
import sys
import tempfile
import unittest
import subprocess
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, agent_dir)

from executor_worker import ExecutorWorker


def _install_fixed_stable_boundary(root):
    """Use real temporary Git worktree state with fixed private boundaries."""
    from agent.governance import db
    stable_root = (Path(root) / "stable-runtime").resolve()
    source = stable_root / "agent" / "governance"
    source.mkdir(parents=True, exist_ok=True)
    (source / "server.py").write_text("# stable fixture\n", encoding="utf-8")
    (source / "db.py").write_text("# fixture module origin\n", encoding="utf-8")
    shared = stable_root / "shared-volume"
    shared.mkdir(exist_ok=True)
    database = shared / "codex-tasks" / "state" / "governance" / "aming-claw" / "governance.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    database.touch()
    subprocess.run(["git", "init", "-b", "codex/direct-no-pass-post-reconcile-r2"], cwd=stable_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=stable_root, check=True)
    subprocess.run(["git", "config", "user.name", "AC Test"], cwd=stable_root, check=True)
    subprocess.run(["git", "add", "."], cwd=stable_root, check=True)
    if subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=stable_root, capture_output=True).returncode:
        subprocess.run(["git", "commit", "-m", "stable fixture"], cwd=stable_root, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=stable_root, check=True, capture_output=True, text=True).stdout.strip()
    digest = "sha256:" + hashlib.sha256((source / "server.py").read_bytes()).hexdigest()
    identity = {"schema_version": "ac_stable_database_identity.v1", "device": database.stat().st_dev, "inode": database.stat().st_ino, "stable_relative_path_sha256": "sha256:" + hashlib.sha256(b"shared-volume/codex-tasks/state/governance/aming-claw/governance.db").hexdigest()}
    db.__file__ = str(source / "db.py")
    db._stable_health_request = lambda: {"status": "ok", "service": "governance", "port": 40000, "runtime_plane": "stable", "runtime_stale": False, "pid": 4242, "runtime_loaded_version": head, "runtime_plane_identity": {"worktree_root": str(stable_root), "branch": "codex/direct-no-pass-post-reconcile-r2", "commit": head, "stable_anchor_commit": head, "database_identity": identity, "stable_database_identity": identity, "project_allowlist": []}, "loaded_runtime_identity": {"loaded_commit": head, "loaded_source_path": str(source / "server.py"), "loaded_source_sha256": digest, "worktree_source_sha256": digest}}
    db._stable_process_identity = lambda pid: ("fixture-start", "python -m agent.governance.server", str(stable_root))
    return shared


def _ac_worker(workspace):
    workspace = os.path.realpath(workspace)
    from agent.runtime_plane import resolve_ac_dev_storage_root
    from agent.governance.db import write_dev_launch_receipt

    # The worker's Git worktree is never its data world.  Bind a persistent
    # temp stable volume and its resolver-derived sibling instead.
    persistent_root = Path(workspace).resolve().parent
    stable = _install_fixed_stable_boundary(persistent_root)
    storage = resolve_ac_dev_storage_root(stable)
    storage.mkdir(parents=True, exist_ok=True)
    server_source = Path(agent_dir) / "governance" / "server.py"
    source_hash = "sha256:" + hashlib.sha256(server_source.read_bytes()).hexdigest()
    os.environ["AMING_CLAW_SHARED_VOLUME"] = str(stable)
    os.environ["AMING_CLAW_DEV_STORAGE_ROOT"] = str(storage)
    write_dev_launch_receipt(storage, stable_shared_volume=stable, source_sha256=source_hash, port=40008)
    return ExecutorWorker("aming-claw", governance_url="http://127.0.0.1:40008", workspace=workspace)


def _git(cmd, cwd):
    return subprocess.run(["git", *cmd], cwd=cwd, check=True, capture_output=True, text=True)


def _repo_with_worktree(tmpdir, name="worker"):
    repo = os.path.join(tmpdir, "repo")
    os.makedirs(repo)
    _git(["init"], repo)
    _git(["config", "user.email", "test@example.invalid"], repo)
    _git(["config", "user.name", "Test"], repo)
    with open(os.path.join(repo, "README"), "w", encoding="utf-8") as handle:
        handle.write("base\n")
    _git(["add", "README"], repo)
    _git(["commit", "-m", "base"], repo)
    worktree = os.path.join(repo, ".worktrees", name)
    os.makedirs(os.path.dirname(worktree), exist_ok=True)
    _git(["worktree", "add", "-b", f"test/{name}", worktree, "HEAD"], repo)
    return repo, os.path.realpath(worktree)


class TestDevWorktreeRound3(unittest.TestCase):
    def setUp(self):
        from agent.governance import db
        self._db_boundary = (db.__file__, db._stable_health_request, db._stable_process_identity)

    def tearDown(self):
        # The fixed boundary is private to each worker test; never leak a
        # synthetic storage claim into deploy/CLI tests that run afterward.
        os.environ.pop("AMING_CLAW_SHARED_VOLUME", None)
        os.environ.pop("AMING_CLAW_DEV_STORAGE_ROOT", None)
        from agent.governance import db
        db.__file__, db._stable_health_request, db._stable_process_identity = self._db_boundary

    def test_dev_session_uses_worktree_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, worktree = _repo_with_worktree(tmpdir)
            worker = _ac_worker(repo)
            worker._register_task_worktree("task-dev-1", worktree)
            fake_session = MagicMock(pid=123, status="completed", stderr="", session_id="sess-1")
            fake_session.stdout = '{"schema_version":"v1","summary":"ok","changed_files":[]}'
            fake_lifecycle = MagicMock()
            fake_lifecycle.create_session.return_value = fake_session
            fake_lifecycle.wait_for_output.return_value = {"status": "completed", "elapsed_sec": 1.0}
            fake_lifecycle.extend_deadline = MagicMock()
            worker._lifecycle = fake_lifecycle
            task = {
                "task_id": "task-dev-1", "type": "dev", "prompt": "Implement change",
                "metadata": {"target_files": ["agent/executor_worker.py"]},
            }
            real_run = subprocess.run

            def _run(cmd, **kwargs):
                # Identity revalidation is intentionally real; only the
                # following staging effect is a spy in this unit test.
                if cmd[:2] == ["git", "rev-parse"]:
                    return real_run(cmd, **kwargs)
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch.object(worker, "_create_worktree", return_value=(worktree, "dev/task-dev-1")), \
                 patch.object(worker, "_build_prompt", return_value="prompt"), \
                 patch.object(worker, "_get_git_changed_files", return_value=["agent/executor_worker.py"]), \
                 patch.object(worker, "_write_memory"), patch("subprocess.run", side_effect=_run) as mock_run:
                result = worker._execute_task(task)

            self.assertEqual(result["status"], "succeeded")
            fake_lifecycle.create_session.assert_called_once()
            self.assertEqual(fake_lifecycle.create_session.call_args.kwargs["workspace"], worktree)
            self.assertEqual(result["result"]["_worktree"], worktree)
            self.assertEqual(result["result"]["_branch"], "dev/task-dev-1")
            mock_run.assert_called()

    def test_git_changed_files_uses_supplied_cwd(self):
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory(dir=repo) as worktree:
            worker = _ac_worker(repo)
            proc1 = MagicMock(returncode=0, stdout="agent/foo.py\n")
            proc2 = MagicMock(returncode=0, stdout="")
            proc3 = MagicMock(returncode=0, stdout="")
            with patch("subprocess.run", side_effect=[proc1, proc2, proc3]) as mock_run:
                files = worker._get_git_changed_files(cwd=worktree)

            self.assertEqual(files, ["agent/foo.py"])
            self.assertEqual(mock_run.call_args_list[0].kwargs["cwd"], worktree)

    def test_git_changed_files_includes_untracked_new_files(self):
        """B27: untracked new files (git ls-files --others) must appear in changed_files."""
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory(dir=repo) as worktree:
            worker = _ac_worker(repo)

            proc1 = MagicMock(returncode=0, stdout="agent/existing.py\n")  # modified tracked
            proc2 = MagicMock(returncode=0, stdout="agent/staged_new.py\n")  # staged new
            proc3 = MagicMock(returncode=0, stdout="agent/untracked_new.py\n")  # untracked new

            with patch("subprocess.run", side_effect=[proc1, proc2, proc3]):
                files = worker._get_git_changed_files(cwd=worktree)

            self.assertIn("agent/existing.py", files)
            self.assertIn("agent/staged_new.py", files)
            self.assertIn("agent/untracked_new.py", files)
            self.assertEqual(len(files), 3)

    def test_create_worktree_uses_attempt_scoped_path_for_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _ = _repo_with_worktree(tmpdir)
            worker = _ac_worker(repo)
            worktree_path, branch_name = worker._create_worktree("task-abc", attempt_num=2)

            self.assertEqual(branch_name, "dev/task-abc-attempt-2")
            self.assertEqual(
                worktree_path,
                os.path.realpath(os.path.join(repo, ".worktrees", "dev-task-abc-attempt-2")),
            )
            self.assertTrue(os.path.isdir(worktree_path))
            self.assertEqual(_git(["rev-parse", "--show-toplevel"], worktree_path).stdout.strip(), worktree_path)

    def test_create_worktree_keeps_first_attempt_names(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _ = _repo_with_worktree(tmpdir)
            worker = _ac_worker(repo)
            worktree_path, branch_name = worker._create_worktree("task-abc", attempt_num=1)

            self.assertEqual(branch_name, "dev/task-abc")
            self.assertEqual(worktree_path, os.path.realpath(os.path.join(repo, ".worktrees", "dev-task-abc")))

    def test_child_handoff_requires_registered_parent_and_exact_git_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, worktree = _repo_with_worktree(tmpdir)
            worker = _ac_worker(repo)
            worker._register_task_worktree("dev-root", worktree)

            self.assertEqual(
                worker._handoff_task_worktree("dev-root", "qa-child", worktree), worktree
            )
            self.assertEqual(worker._validated_task_worktree("qa-child", worktree), worktree)
            with self.assertRaisesRegex(ValueError, "parent task"):
                worker._handoff_task_worktree("missing", "test-child", worktree)
            with self.assertRaisesRegex(ValueError, "claim"):
                worker._handoff_task_worktree("dev-root", "wrong-child", repo)

    def test_registration_rejects_non_git_directory(self):
        with tempfile.TemporaryDirectory() as repo:
            worker = _ac_worker(repo)
            with self.assertRaisesRegex(ValueError, "Git"):
                worker._register_task_worktree("task-no-git", repo)

    def test_cleanup_rejects_unregistered_sibling_without_effect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _ = _repo_with_worktree(tmpdir)
            worker = _ac_worker(repo)
            sibling = os.path.join(repo, ".worktrees", "unregistered")
            os.makedirs(sibling)
            marker = os.path.join(sibling, "keep")
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("must remain\n")

            with patch("subprocess.run") as run:
                self.assertFalse(worker._remove_worktree(sibling, "test/unregistered"))

            self.assertTrue(os.path.isfile(marker))
            run.assert_not_called()

    def test_cleanup_rejects_symlink_claim_without_effect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, worktree = _repo_with_worktree(tmpdir)
            worker = _ac_worker(repo)
            worker._register_task_worktree("dev-root", worktree)
            claim = os.path.join(repo, ".worktrees", "symlink-claim")
            os.symlink(worktree, claim)

            with patch("subprocess.run") as run:
                self.assertFalse(worker._remove_worktree(claim, "test/worker"))

            self.assertTrue(os.path.isdir(worktree))
            run.assert_not_called()

    def test_cleanup_rejects_identity_drift_without_deletion_or_branch_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, worktree = _repo_with_worktree(tmpdir)
            worker = _ac_worker(repo)
            worker._register_task_worktree("dev-root", worktree)
            # Materially replace the registered Git worktree at its old path.
            _git(["worktree", "remove", "--force", worktree], repo)
            os.makedirs(worktree)
            marker = os.path.join(worktree, "replacement")
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("do not delete\n")
            ref_before = _git(["rev-parse", "test/worker"], repo).stdout.strip()

            self.assertFalse(worker._remove_worktree(worktree, "test/worker"))

            self.assertTrue(os.path.isfile(marker))
            self.assertEqual(_git(["rev-parse", "test/worker"], repo).stdout.strip(), ref_before)
            self.assertIn("dev-root", worker._task_worktrees)

    def test_cleanup_unregisters_shared_handoffs_only_after_real_git_removal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, worktree = _repo_with_worktree(tmpdir)
            worker = _ac_worker(repo)
            worker._register_task_worktree("dev-root", worktree)
            worker._handoff_task_worktree("dev-root", "qa-child", worktree)

            self.assertTrue(worker._remove_worktree(worktree, "test/worker"))

            self.assertFalse(os.path.exists(worktree))
            self.assertNotIn("dev-root", worker._task_worktrees)
            self.assertNotIn("qa-child", worker._task_worktrees)
            self.assertNotEqual(
                _git(["branch", "--list", "test/worker"], repo).stdout.strip(), "test/worker"
            )

    def test_version_write_rejects_identity_drift_after_read_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _ = _repo_with_worktree(tmpdir)
            worker = _ac_worker(repo)
            repo = worker.workspace
            version = os.path.join(repo, "VERSION")
            with open(version, "w", encoding="utf-8") as handle:
                handle.write("CHAIN_VERSION=old\n")
            # This models the earlier read-side validation in merge handling.
            self.assertEqual(worker._revalidate_effect_workspace("merge", repo), repo)
            observed = open(version, encoding="utf-8").read()

            moved = os.path.join(os.path.dirname(repo), "replaced-repo")
            os.rename(repo, moved)
            os.makedirs(repo)
            _git(["init"], repo)
            replacement = os.path.join(repo, "VERSION")
            with open(replacement, "w", encoding="utf-8") as handle:
                handle.write("CHAIN_VERSION=replacement\n")

            with self.assertRaisesRegex(ValueError, "workspace identity changed"):
                worker._write_version_file("merge", replacement, observed.replace("old", "new"))

            self.assertEqual(
                open(replacement, encoding="utf-8").read(), "CHAIN_VERSION=replacement\n"
            )


if __name__ == "__main__":
    unittest.main()
