"""Bounded native COW fixtures; real clone syscalls use only kilobytes."""
from __future__ import annotations

import json
import ctypes
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from agent.governance import db, graph_query_trace, graph_snapshot_store as snapshots
from agent.governance import project_service, server, stale_artifact_cleanup as cleanup
from agent.governance import snapshot_cow_cleanup as cow
from agent.governance.contracts.runtime import SQLiteContractExecutionStore


@pytest.fixture
def cow_fixture(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    root = tmp_path / "repo"
    root.mkdir()
    world = tmp_path / "world"
    project = world / "proj"
    project.mkdir(parents=True)
    monkeypatch.setattr(db, "_governance_root", lambda: world)
    config = {"governance": {"snapshot_retention": {"keep_last_n": 1},
              "snapshot_cow_cleanup": {"max_snapshots": 4, "max_pairs": 8,
                                       "max_hash_bytes": 1024 * 1024}}}
    monkeypatch.setattr(project_service, "get_project_config_metadata", lambda _pid: config)
    monkeypatch.setattr(project_service, "resolve_project_root", lambda _pid, **_kw: root)
    conn = sqlite3.connect(project / "governance.db")
    conn.row_factory = sqlite3.Row
    db._ensure_schema(conn)
    snapshots.ensure_schema(conn)
    from agent.governance.reconcile_semantic_enrichment import _ensure_semantic_state_schema
    _ensure_semantic_state_schema(conn)
    graph_query_trace.ensure_schema(conn)
    conn.executescript(SQLiteContractExecutionStore.SCHEMA_SQL)
    server._ensure_release_operator_head_queue_schema(conn)
    for sid, status, created in (("full-old", "superseded", "2020-01-01"),
                                 ("full-active", "active", "2021-01-01")):
        conn.execute("INSERT INTO graph_snapshots(project_id,snapshot_id,snapshot_kind,status,created_at,"
                     "commit_sha) VALUES(?,?,?,?,?,?)",
                     ("proj", sid, "full", status, created, "fixture"))
    conn.execute("INSERT INTO graph_snapshot_refs(project_id,ref_name,snapshot_id,updated_at,commit_sha) "
                 "VALUES('proj','active','full-active','2021','fixture')")
    base = snapshots._snapshot_root("proj", "full-old")
    for index, (source, target) in enumerate(cow.PAIRS):
        (base / source).parent.mkdir(parents=True, exist_ok=True)
        content = (b"isolated real APFS fixture " + bytes([index])) * 1024
        (base / source).write_bytes(content)
        (base / target).write_bytes(content)
        os.utime(base / source, ns=(10_000_000_000, 10_000_000_000))
        os.utime(base / target, ns=(20_000_000_000, 20_000_000_000))
        os.chmod(base / target, 0o640)
        if sys.platform == "darwin":
            cow._call("setxattr", ctypes.c_int, [ctypes.c_char_p, ctypes.c_char_p,
                ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int],
                os.fsencode(base / target), b"org.aming-claw.isolated-test",
                ctypes.create_string_buffer(b"target-only"), 11, 0, 0x41)
    conn.commit()
    archive = tmp_path / "fixture-archive"
    archive.mkdir()
    # This fixture isolates byte/ACL/restore behavior. External volume custody
    # is tested separately; clonefile and atomic replacement remain real.
    descriptor = {"path": str(archive), "identity": archive.stat().st_ino,
                  "mount_identity": archive.stat().st_ino, "uuid": "isolated-fixture",
                  "mount": str(archive), "device": archive.stat().st_dev + 1}
    monkeypatch.setattr(cow, "_archive", lambda *_args, **_kw: descriptor)
    monkeypatch.setattr(cow, "_volume", lambda path: {"uuid": "fixture-apfs", "mount": str(path),
                                                    "device": path.stat().st_dev})
    yield conn, root, base, config, archive
    conn.close()


def preview(fixture):
    conn, root, *_ = fixture
    return cleanup.build_stale_artifact_cleanup_projection(conn, "proj", repo_root_path=root,
                                                          dimension=cow.DIMENSION)


def apply(fixture, plan):
    conn, root, *_ = fixture
    return cleanup.apply_stale_artifact_cleanup(conn, "proj", repo_root_path=root,
        dimension=cow.DIMENSION, plan_hash=plan["plan_hash"], plan_revision=plan["plan_revision"],
        operation_id=plan["operation_id"], candidate_ids=[r["candidate_id"] for r in plan["candidates"]])


def recover(fixture, plan, action="inspect"):
    conn, root, *_ = fixture
    return cleanup.recover_snapshot_cow_cleanup(conn, "proj", repo_root_path=root,
                                                operation_id=plan["operation_id"], action=action)


def test_preview_exact_bounded_plan_and_target_specific_metadata(cow_fixture):
    plan = preview(cow_fixture)
    assert plan["writes_performed"] is False
    assert plan["apply_plan_available"] is True
    assert len(plan["candidates"]) == 2
    assert plan["summary"]["physical_reclaim"] == "UNKNOWN"
    assert plan["summary"]["unique_inode_allocated_bytes"] >= plan["summary"]["duplicate_byte_potential"]
    for pair in plan["candidates"]:
        assert pair["source_metadata"]["mtime_ns"] != pair["target_metadata"]["mtime_ns"]
        assert pair["target_metadata"]["mode"] == 0o640
    assert not cow._state_root("proj").exists()


@pytest.mark.parametrize("key,value", [("max_hash_bytes", 1), ("max_pairs", 1), ("max_snapshots", 1)])
def test_budget_exhaustion_has_no_applyable_omissions(cow_fixture, key, value):
    conn, _, _, config, _ = cow_fixture
    config["governance"]["snapshot_cow_cleanup"][key] = value
    if key == "max_snapshots":
        conn.execute("INSERT INTO graph_snapshots(project_id,snapshot_id,snapshot_kind,status,created_at,"
                     "commit_sha) VALUES('proj','full-another','full','superseded','2019','')")
        conn.commit()
    plan = preview(cow_fixture)
    assert plan["summary"]["truncated"] is True
    assert plan["apply_plan_available"] is False
    assert all(not p["safe_to_apply"] for p in plan["candidates"])


def test_missing_and_malformed_live_inventory_refuse(cow_fixture):
    conn = cow_fixture[0]
    conn.execute("DROP TABLE sessions")
    conn.commit()
    with pytest.raises(cleanup.StaleArtifactCleanupError, match="preview_refused"):
        preview(cow_fixture)


def test_completed_audit_reference_is_history_but_live_ref_protects(cow_fixture):
    conn, _, _, _, _ = cow_fixture
    conn.execute("INSERT INTO graph_query_traces(trace_id,project_id,snapshot_id,query_source,query_purpose,"
                 "status,created_at,updated_at) VALUES('history','proj','full-old','test','test','complete','2020','2020')")
    conn.commit()
    assert len(preview(cow_fixture)["candidates"]) == 2
    conn.execute("UPDATE graph_query_traces SET status='running'")
    conn.commit()
    assert preview(cow_fixture)["candidates"] == []


def test_source_symlink_hardlink_and_hash_difference_refuse(cow_fixture):
    _, _, base, *_ = cow_fixture
    source, target = (base / rel for rel in cow.PAIRS[0])
    target.unlink()
    target.symlink_to(source)
    assert not any(r["target"] == cow.PAIRS[0][1] for r in preview(cow_fixture)["candidates"])
    target.unlink()
    os.link(source, target)
    assert not any(r["target"] == cow.PAIRS[0][1] for r in preview(cow_fixture)["candidates"])
    target.unlink()
    target.write_bytes(b"different")
    assert not any(r["target"] == cow.PAIRS[0][1] for r in preview(cow_fixture)["candidates"])


def test_hash_drift_and_wrong_scope_refuse(cow_fixture, monkeypatch):
    conn, root, base, *_ = cow_fixture
    pair = cow._metadata(base / cow.PAIRS[0][0])
    changed = dict(pair, ino=pair["ino"] + 1)
    with pytest.raises(cow.CowRefusal):
        cow._hash(base / cow.PAIRS[0][0], changed)
    with pytest.raises(cow.CowRefusal, match="root_mismatch"):
        cow.preview(conn, "proj", root.parent)
    monkeypatch.setattr(db, "_governance_root", lambda: root)
    with pytest.raises(cow.CowRefusal, match="world_mismatch|path_missing"):
        cow.preview(conn, "proj", root)


def test_unsupported_platform_and_stale_exact_plan_are_zero_write(cow_fixture, monkeypatch):
    plan = preview(cow_fixture)
    monkeypatch.setattr(cow.sys, "platform", "linux")
    with pytest.raises(cleanup.StaleArtifactCleanupError) as caught:
        apply(cow_fixture, plan)
    assert caught.value.payload["writes_performed"] is False
    assert not cow._state_root("proj").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="real APFS clone fixture requires macOS")
@pytest.mark.parametrize("delta", [0, -1000, 1000])
def test_real_clone_metadata_restore_replay_and_truthful_df(cow_fixture, monkeypatch, delta):
    plan = preview(cow_fixture)
    real_usage = shutil.disk_usage
    count = [0]
    def usage(path):
        count[0] += 1
        return real_usage(path)._replace(free=1000000 + (delta if count[0] > 1 else 0))
    monkeypatch.setattr(cow.shutil, "disk_usage", usage)
    result = apply(cow_fixture, plan)
    assert result["ok"] is True, result
    assert result["filesystem"]["observed_net_delta"] == delta
    assert result["applied_count"] == 2
    replay = apply(cow_fixture, plan)
    assert replay["replay"] is True and replay["writes_performed"] is False
    fresh = preview(cow_fixture)
    assert fresh["candidates"] == []
    assert any(r["reason"] == "matching_native_completion" for r in fresh["refusals"])
    inspected = recover(cow_fixture, plan)
    assert inspected["mode"] == "inspect" and not inspected["writes_performed"]
    restored = recover(cow_fixture, plan, "restore")
    assert restored["ok"] and restored["restored_count"] == 2
    base = cow_fixture[2]
    for row in plan["candidates"]:
        actual = cow._metadata(base / row["target"])
        assert all(actual[k] == row["target_metadata"][k] for k in cow.PRESERVED)
        assert cow._metadata(base / row["source"]) == row["source_metadata"]
    assert cow._journal_path("proj", plan["operation_id"]).exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="real APFS clone fixture requires macOS")
def test_real_clone_write_isolation_and_replay_drift(cow_fixture):
    plan = preview(cow_fixture)
    assert apply(cow_fixture, plan)["ok"]
    row = plan["candidates"][0]
    base = cow_fixture[2]
    (base / row["target"]).write_bytes(b"independent change")
    assert cow._hash(base / row["source"], row["source_metadata"]) == row["sha256"]
    with pytest.raises(cleanup.StaleArtifactCleanupError):
        apply(cow_fixture, plan)
    with pytest.raises(cleanup.StaleArtifactCleanupError, match="cow_refused"):
        recover(cow_fixture, plan, "restore")


@pytest.mark.skipif(sys.platform != "darwin", reason="real APFS clone fixture requires macOS")
@pytest.mark.parametrize("race", ["pin", "writer", "source", "target"])
def test_live_pin_writer_source_target_race_after_preview(cow_fixture, monkeypatch, race):
    plan = preview(cow_fixture)
    conn, _, base, *_ = cow_fixture
    if race == "pin":
        conn.execute("INSERT INTO graph_snapshot_refs(project_id,ref_name,snapshot_id,updated_at,commit_sha) "
                     "VALUES('proj','candidate','full-old','now','fixture')")
        conn.commit()
    elif race == "writer":
        monkeypatch.setattr(cow, "_quiet", lambda *_: (_ for _ in ()).throw(cow.CowRefusal("writer")))
    else:
        (base / plan["candidates"][0][race]).write_bytes(b"drift")
    with pytest.raises(cleanup.StaleArtifactCleanupError) as caught:
        apply(cow_fixture, plan)
    assert caught.value.payload["writes_performed"] is False
    assert not cow._state_root("proj").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="real APFS clone fixture requires macOS")
def test_partial_two_pairs_stop_restart_inspect_restore(cow_fixture, monkeypatch):
    plan = preview(cow_fixture)
    real = cow._clone
    calls = []
    def fail_second(source, stage):
        calls.append(source)
        if len(calls) == 2:
            raise OSError("injected second clone failure")
        real(source, stage)
    monkeypatch.setattr(cow, "_clone", fail_second)
    result = apply(cow_fixture, plan)
    assert not result["ok"] and result["applied_count"] == 1
    assert len(calls) == 2
    assert recover(cow_fixture, plan)["state"] == "partial_or_ambiguous"
    # A new service connection sees the same durable journal; never clone again.
    connection = sqlite3.connect(cow_fixture[0].execute("PRAGMA database_list").fetchone()[2])
    connection.row_factory = sqlite3.Row
    try:
        replay = cow.apply(connection, "proj", cow_fixture[1],
            candidate_ids=[r["candidate_id"] for r in plan["candidates"]], plan_hash=plan["plan_hash"],
            plan_revision=1, operation_id=plan["operation_id"])
        assert replay["error"] == "cow_operation_inspect_required"
        assert len(calls) == 2
    finally:
        connection.close()
    restored = recover(cow_fixture, plan, "restore")
    assert restored["ok"] and restored["restored_count"] == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="real APFS clone fixture requires macOS")
@pytest.mark.parametrize("timing", ["before", "after"])
def test_rename_failure_retains_known_phase_and_refuses_retry(cow_fixture, monkeypatch, timing):
    plan = preview(cow_fixture)
    real = os.replace
    def failure(source, target):
        if str(Path(source).name).startswith(".snapshot-cow-"):
            if timing == "after":
                real(source, target)
            raise OSError("injected rename failure")
        real(source, target)
    monkeypatch.setattr(cow.os, "replace", failure)
    result = apply(cow_fixture, plan)
    assert not result["ok"] and result["write_disposition"] == "ambiguous"
    inspected = recover(cow_fixture, plan)
    assert inspected["pair_observations"][0]["phase"] == "before_replace"
    assert apply(cow_fixture, plan)["error"] == "cow_operation_inspect_required"


@pytest.mark.skipif(sys.platform != "darwin", reason="real APFS clone fixture requires macOS")
def test_journal_fsync_failure_is_inspectable_never_blind_retry(cow_fixture, monkeypatch):
    plan = preview(cow_fixture)
    real = cow._fsync
    def failure(path, **kw):
        if path == cow._state_root("proj"):
            raise OSError("injected journal directory fsync")
        real(path, **kw)
    monkeypatch.setattr(cow, "_fsync", failure)
    result = apply(cow_fixture, plan)
    assert result["ok"] is False and result["journal_write_failed"]
    assert recover(cow_fixture, plan)["mode"] == "inspect"


def test_external_archive_requires_configured_mount_uuid_and_distinct_device(cow_fixture, monkeypatch):
    monkeypatch.undo()
    with pytest.raises((cow.CowRefusal, OSError)):
        cow._archive("unconfigured", cow_fixture[2])


def test_opaque_recovery_identity_refuses_arbitrary_path(cow_fixture):
    with pytest.raises(cleanup.StaleArtifactCleanupError):
        cleanup.recover_snapshot_cow_cleanup(cow_fixture[0], "proj", repo_root_path=cow_fixture[1],
                                              operation_id="../../arbitrary", action="restore")


def test_selected_one_snapshot_stays_usable_in_large_history(cow_fixture):
    conn, _, _, config, _ = cow_fixture
    for index in range(50):
        conn.execute("INSERT INTO graph_snapshots(project_id,snapshot_id,snapshot_kind,status,created_at,commit_sha) "
                     "VALUES('proj',?,'full','superseded','2019','fixture')", (f"full-other-{index}",))
    conn.commit()
    config["governance"]["snapshot_cow_cleanup"].update(max_snapshots=1, snapshot_ids=["full-old"])
    plan = preview(cow_fixture)
    assert plan["apply_plan_available"] and len(plan["candidates"]) == 2
    assert {r["snapshot_id"] for r in plan["candidates"]} == {"full-old"}


def test_semantic_terminal_history_and_live_or_leased_use(cow_fixture):
    conn = cow_fixture[0]
    conn.execute("INSERT INTO graph_semantic_jobs(project_id,snapshot_id,node_id,status) "
                 "VALUES('proj','full-old','node','ai_complete')")
    conn.commit()
    assert len(preview(cow_fixture)["candidates"]) == 2
    conn.execute("UPDATE graph_semantic_jobs SET lease_expires_at='2999-01-01T00:00:00+00:00'")
    conn.commit()
    assert preview(cow_fixture)["candidates"] == []
    conn.execute("UPDATE graph_semantic_jobs SET status='running',lease_expires_at=''")
    conn.commit()
    assert preview(cow_fixture)["candidates"] == []


def test_malformed_retention_config_refuses_instead_of_guessing(cow_fixture):
    cow_fixture[3]["governance"]["snapshot_retention"]["keep_last_n"] = "unknown"
    with pytest.raises(cleanup.StaleArtifactCleanupError):
        preview(cow_fixture)


@pytest.mark.skipif(sys.platform != "darwin", reason="small real APFS fixture")
def test_fresh_completion_lookup_hashes_only_budgeted_pair_bytes(cow_fixture, monkeypatch):
    plan = preview(cow_fixture)
    assert apply(cow_fixture, plan)["ok"]
    real = cow._hash
    observed = []
    def digest(path, expected):
        observed.append(expected["size"])
        return real(path, expected)
    monkeypatch.setattr(cow, "_hash", digest)
    fresh = preview(cow_fixture)
    assert fresh["candidates"] == []
    assert sum(observed) == fresh["summary"]["hash_bytes"]
    assert sum(observed) <= fresh["budgets"]["max_hash_bytes"]


@pytest.mark.skipif(sys.platform != "darwin", reason="small real APFS fixture")
def test_failed_backup_or_restore_proof_prevents_any_clone(cow_fixture, monkeypatch):
    plan = preview(cow_fixture)
    monkeypatch.setattr(cow, "_clone", lambda *_a: pytest.fail("backup failed: clone forbidden"))
    monkeypatch.setattr(cow, "_copy", lambda *_a: (_ for _ in ()).throw(cow.CowRefusal("restore proof failed")))
    result = apply(cow_fixture, plan)
    assert not result["ok"] and result["graph_files_replaced"] == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="small real APFS fixture")
def test_recovery_journal_failure_visible_to_supported_inspect(cow_fixture, monkeypatch):
    plan = preview(cow_fixture)
    assert apply(cow_fixture, plan)["ok"]
    real = cow._write
    def failure(path, record):
        if path.name.endswith(".recovery.json"):
            temporary = path.with_name(path.name + ".pending")
            temporary.write_text(json.dumps(record))
            raise OSError("injected recovery persistence failure")
        real(path, record)
    monkeypatch.setattr(cow, "_write", failure)
    result = recover(cow_fixture, plan, "restore")
    assert not result["ok"] and result["write_disposition"] == "ambiguous"
    inspect = recover(cow_fixture, plan)
    assert inspect["recovery_pending"]["operation_id"] == plan["operation_id"]
