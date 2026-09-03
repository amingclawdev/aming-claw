from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import threading

import pytest

from agent.governance import audit_service, db as governance_db, redis_client
from agent.governance import project_service
from agent.governance.contracts.runtime import SQLiteContractExecutionStore
from agent.governance.errors import ValidationError


@pytest.fixture(autouse=True)
def isolated_project_storage(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "governance"

    def isolated_root():
        assert root.resolve().is_relative_to(tmp_path.resolve())
        assert not root.is_symlink()
        return root

    monkeypatch.setenv("AMING_CLAW_RUNTIME_PLANE", "stable")
    monkeypatch.setenv("SHARED_VOLUME_PATH", str(tmp_path / "shared"))
    for module in (governance_db, project_service, audit_service):
        monkeypatch.setattr(module, "_governance_root", isolated_root)

    def reject_external_redis(_self):
        raise AssertionError("project initialization tests forbid external Redis")

    monkeypatch.setattr(
        redis_client, "_instance",
        redis_client.RedisClient(url="redis://test-disabled.invalid:0/0"),
    )
    monkeypatch.setattr(redis_client.RedisClient, "connect", reject_external_redis)
    return root


def test_new_project_initializes_canonical_contract_store(isolated_project_storage):
    root = isolated_project_storage
    assert not (root / "new-project" / "governance.db").exists()

    result = project_service.init_project("NewProject", project_name="New project")

    assert result["project"]["project_id"] == "new-project"
    assert result["normalized_from"] == "NewProject"
    assert project_service._load_projects()["projects"]["new-project"]["initialized"]
    with closing(sqlite3.connect(root / "new-project" / "governance.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM contract_runtime_executions").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM worker_implementation_test_results_corrections"
        ).fetchone()[0] == 0


def test_repeat_project_init_preserves_warm_record_bytes_without_db_access(
    isolated_project_storage, monkeypatch,
):
    root = isolated_project_storage
    first = project_service.init_project("warm-project")
    db_path = root / "warm-project" / "governance.db"
    conn = governance_db.get_connection("warm-project")
    try:
        SQLiteContractExecutionStore(conn).create({
            "contract_execution_id": "cex-warm-project",
            "project_id": "warm-project",
            "backlog_id": "WARM-PROJECT",
            "contract_id": "operator_supervised_direct_main",
            "version": "1",
            "revision": "rev3",
            "execution_state_revision": 7,
        })
        conn.commit()
        before = tuple(conn.execute(
            "SELECT *, CAST(record_json AS BLOB) FROM contract_runtime_executions"
        ).fetchone())
    finally:
        conn.close()
    registry_before = (root / "projects.json").read_bytes()
    database_before = db_path.read_bytes()

    def reject_db_access(_project_id):
        raise AssertionError("initialized project must not open or repair its DB")

    monkeypatch.setattr(project_service, "get_connection", reject_db_access)
    replay = project_service.init_project("warm-project", project_name="Ignored rename")

    assert replay["project"] == first["project"]
    assert replay["message"] == "Project already initialized"
    assert (root / "projects.json").read_bytes() == registry_before
    assert db_path.read_bytes() == database_before
    with closing(sqlite3.connect(db_path)) as check:
        assert tuple(check.execute(
            "SELECT *, CAST(record_json AS BLOB) FROM contract_runtime_executions"
        ).fetchone()) == before


@pytest.mark.parametrize("initialized", [False, None])
def test_registered_uninitialized_project_is_not_implicitly_repaired(
    isolated_project_storage, monkeypatch, initialized,
):
    root = isolated_project_storage
    entry = {"project_id": "historical", "status": "active"}
    if initialized is not None:
        entry["initialized"] = initialized
    project_service._save_projects({"version": 1, "projects": {"historical": entry}})
    before = (root / "projects.json").read_bytes()

    def reject_db_access(_project_id):
        raise AssertionError("historical registration must not authorize store repair")

    monkeypatch.setattr(project_service, "get_connection", reject_db_access)
    with pytest.raises(ValidationError, match="explicit recovery is required"):
        project_service.init_project("historical")
    assert (root / "projects.json").read_bytes() == before
    assert not (root / "historical").exists()


@pytest.mark.parametrize("failure", ["owner", "commit", "registry"])
def test_project_init_failure_never_publishes_success_and_closes_connection(
    isolated_project_storage, monkeypatch, failure,
):
    root = isolated_project_storage
    opened = []
    real_get_connection = project_service.get_connection
    real_save = project_service._save_projects

    class TrackedConnection:
        def __init__(self, conn):
            self.conn = conn
            self.closed = False
            self.rolled_back = False

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, parameters=()):
            if failure == "owner" and (
                "CREATE TABLE IF NOT EXISTS worker_implementation_test_results_corrections"
                in sql
            ):
                assert self.conn.in_transaction
                assert self.conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE name='contract_runtime_executions'"
                ).fetchone()[0] == 1
                raise sqlite3.OperationalError("forced partial schema owner failure")
            return self.conn.execute(sql, parameters)

        def commit(self):
            if failure == "commit":
                raise sqlite3.OperationalError("forced initialization commit failure")
            self.conn.commit()

        def rollback(self):
            self.rolled_back = True
            self.conn.rollback()

        def close(self):
            self.closed = True
            self.conn.close()

    def tracked_connection(project_id):
        conn = TrackedConnection(real_get_connection(project_id))
        opened.append(conn)
        return conn

    def failing_save(_projects):
        raise OSError("forced registry save failure")

    monkeypatch.setattr(project_service, "get_connection", tracked_connection)
    if failure == "registry":
        monkeypatch.setattr(project_service, "_save_projects", failing_save)

    with pytest.raises((sqlite3.OperationalError, OSError), match="forced"):
        project_service.init_project("failed-project")

    assert len(opened) == 1
    assert opened[0].closed
    assert opened[0].rolled_back is (failure != "registry")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].conn.execute("SELECT 1")
    assert "failed-project" not in project_service._load_projects()["projects"]
    with closing(sqlite3.connect(root / "failed-project" / "governance.db")) as check:
        present = check.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='contract_runtime_executions'"
        ).fetchone()[0]
        assert present == (1 if failure == "registry" else 0)

    monkeypatch.setattr(project_service, "get_connection", real_get_connection)
    monkeypatch.setattr(project_service, "_save_projects", real_save)
    assert project_service.init_project("failed-project")["project"]["status"] == "active"
    assert project_service._load_projects()["projects"]["failed-project"]["initialized"]


def test_project_init_connection_failure_does_not_publish_success(monkeypatch):
    def failing_connection(_project_id):
        raise sqlite3.OperationalError("forced DB open failure")

    monkeypatch.setattr(project_service, "get_connection", failing_connection)
    with pytest.raises(sqlite3.OperationalError, match="forced DB open failure"):
        project_service.init_project("unopened-project")
    assert "unopened-project" not in project_service._load_projects()["projects"]


def test_project_registry_save_is_atomic_and_loadable(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    monkeypatch.setattr(project_service, "_governance_root", lambda: state_root)

    first = {
        "version": 1,
        "projects": {
            "demo": {
                "project_id": "demo",
                "workspace_path": str(tmp_path),
                "status": "active",
            }
        },
    }
    project_service._save_projects(first)

    loaded = project_service._load_projects()
    assert loaded["projects"]["demo"]["workspace_path"] == str(tmp_path)
    assert json.loads((state_root / "projects.json").read_text(encoding="utf-8"))["version"] == 1
    assert not list(state_root.glob(".projects.json.*.tmp"))


def test_project_registry_concurrent_readers_never_observe_partial_json(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    monkeypatch.setattr(project_service, "_governance_root", lambda: state_root)
    project_service._save_projects({"version": 1, "projects": {}})
    errors: list[Exception] = []

    def _reader() -> None:
        try:
            for _ in range(25):
                data = project_service._load_projects()
                assert isinstance(data.get("projects"), dict)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    readers = [threading.Thread(target=_reader) for _ in range(4)]
    for reader in readers:
        reader.start()
    for idx in range(25):
        project_service._save_projects(
            {
                "version": 1,
                "projects": {
                    "demo": {
                        "project_id": "demo",
                        "workspace_path": str(tmp_path),
                        "status": "active",
                        "counter": idx,
                    }
                },
            }
        )
    for reader in readers:
        reader.join()

    assert errors == []


def test_project_ai_routing_metadata_merges_partial_updates(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    monkeypatch.setattr(project_service, "_governance_root", lambda: state_root)
    project_service._save_projects(
        {
            "version": 1,
            "projects": {
                "demo": {
                    "project_id": "demo",
                    "workspace_path": str(tmp_path),
                    "status": "active",
                    "project_config": {
                        "project_id": "demo",
                        "ai": {
                            "routing": {
                                "pm": {"provider": "openai", "model": "gpt-5.5"},
                                "dev": {"provider": "openai", "model": "gpt-5.4"},
                                "semantic": {
                                    "provider": "anthropic",
                                    "model": "claude-opus-4-7",
                                },
                            }
                        },
                    },
                }
            },
        }
    )

    updated = project_service.update_project_ai_routing_metadata(
        "demo",
        {"semantic": {"provider": "openai", "model": "gpt-5.4-mini"}},
        actor="dashboard-test",
    )

    routing = updated["project_config"]["ai"]["routing"]
    assert routing["semantic"] == {"provider": "openai", "model": "gpt-5.4-mini"}
    assert routing["pm"] == {"provider": "openai", "model": "gpt-5.5"}
    assert routing["dev"] == {"provider": "openai", "model": "gpt-5.4"}
