from __future__ import annotations

import json
from types import SimpleNamespace


def _fake_git_run(head: str):
    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(returncode=0, stdout=f"{head}\n", stderr="")
        if cmd[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout=" M agent/foo.py\n?? docs/new.md\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def test_finalize_version_sync_inserts_updated_by_for_new_project(isolated_gov_db, monkeypatch):
    from governance.auto_chain import _finalize_version_sync

    monkeypatch.setattr("subprocess.run", _fake_git_run("abc1234"))

    _finalize_version_sync(isolated_gov_db, "new-proj", "task-1")

    row = isolated_gov_db.execute(
        "SELECT chain_version, git_head, dirty_files, updated_by "
        "FROM project_version WHERE project_id=?",
        ("new-proj",),
    ).fetchone()
    assert row is not None
    assert row["chain_version"] == "abc1234"
    assert row["git_head"] == "abc1234"
    assert row["updated_by"] == "auto-chain:task-1"
    assert json.loads(row["dirty_files"]) == ["agent/foo.py", "docs/new.md"]


def test_finalize_version_sync_updates_existing_row_without_replace(isolated_gov_db, monkeypatch):
    from governance.auto_chain import _finalize_version_sync

    isolated_gov_db.execute(
        "INSERT INTO project_version "
        "(project_id, chain_version, updated_at, updated_by, git_head, dirty_files, observer_mode, max_subtasks) "
        "VALUES (?, ?, datetime('now'), ?, ?, ?, ?, ?)",
        ("aming-claw", "old-chain", "init", "old-head", "[]", 1, 9),
    )
    isolated_gov_db.commit()
    monkeypatch.setattr("subprocess.run", _fake_git_run("def5678"))

    _finalize_version_sync(isolated_gov_db, "aming-claw", "task-2")

    row = isolated_gov_db.execute(
        "SELECT chain_version, git_head, dirty_files, updated_by, observer_mode, max_subtasks "
        "FROM project_version WHERE project_id=?",
        ("aming-claw",),
    ).fetchone()
    assert row["chain_version"] == "def5678"
    assert row["git_head"] == "def5678"
    assert row["updated_by"] == "auto-chain:task-2"
    assert row["observer_mode"] == 1
    assert row["max_subtasks"] == 9
    assert json.loads(row["dirty_files"]) == ["agent/foo.py", "docs/new.md"]
