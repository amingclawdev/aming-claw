"""Bounded native COW fixtures; real clone syscalls use only kilobytes."""
from __future__ import annotations

import json
import ctypes
import hashlib
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from contextlib import nullcontext

import pytest

from agent.governance import db, graph_query_trace, graph_snapshot_store as snapshots
from agent.governance import project_service, server, stale_artifact_cleanup as cleanup
from agent.governance import snapshot_cow_cleanup as cow
from agent.governance.contracts.runtime import SQLiteContractExecutionStore

_REAL_VOLUME = cow._volume


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


@pytest.mark.parametrize("nested_snapshot", [False, True])
def test_owned_connection_cow_preview_initializes_shared_census(cow_fixture, nested_snapshot):
    fixture_conn, root, *_ = cow_fixture
    database = Path(fixture_conn.execute("PRAGMA database_list").fetchone()[2])

    def factory():
        reader = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        reader.row_factory = sqlite3.Row
        reader.execute("PRAGMA query_only=ON")
        return reader

    with snapshots._owned_reference_connection(factory) as reader:
        budget = snapshots._reference_budget(reader)
        assert budget.deadline is None
        changes = reader.total_changes
        busy = reader.execute("PRAGMA busy_timeout").fetchone()[0]

        def check_preview():
            plan = cleanup.build_stale_artifact_cleanup_projection(
                reader, "proj", repo_root_path=root, dimension=cow.DIMENSION)
            assert len(plan["candidates"]) == 2 and plan["writes_performed"] is False
            assert budget.deadline is not None and not budget.active
            deadline = budget.deadline
            pins = cow._live_pins(reader, "proj")
            assert "full-active" in pins and "full-old" not in pins
            assert budget.deadline == deadline and not budget.active
            assert reader.total_changes == changes
            assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == busy

        if nested_snapshot:
            with snapshots._reference_read_snapshot(reader):
                check_preview()
                assert reader.in_transaction
        else:
            check_preview()
        assert not reader.in_transaction
    with pytest.raises(sqlite3.ProgrammingError):
        reader.execute("SELECT 1")


@pytest.mark.parametrize("refusal", ["deadline", "row_limit"])
def test_owned_connection_cow_census_retains_refusals(cow_fixture, refusal):
    fixture_conn, _root, *_ = cow_fixture
    if refusal == "row_limit":
        fixture_conn.executemany(
            "INSERT INTO graph_snapshot_refs(project_id,ref_name,snapshot_id,updated_at,commit_sha) "
            "VALUES('proj',?,'full-old','2020','fixture')",
            [(f"bounded-{n}",) for n in range(cow.MAX_LIVE_ROWS)])
        fixture_conn.commit()
    database = Path(fixture_conn.execute("PRAGMA database_list").fetchone()[2])

    def factory():
        reader = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        reader.row_factory = sqlite3.Row
        reader.execute("PRAGMA query_only=ON")
        return reader

    with snapshots._owned_reference_connection(factory) as reader:
        budget = snapshots._reference_budget(reader)
        if refusal == "deadline":
            budget.deadline = snapshots.time.monotonic() - 1
        changes = reader.total_changes
        busy = reader.execute("PRAGMA busy_timeout").fetchone()[0]
        expected = "cow_reference_census_refused" if refusal == "deadline" else "cow_ref_window_unbounded"
        with pytest.raises(cow.CowRefusal, match=expected) as caught:
            cow._live_pins(reader, "proj")
        if refusal == "deadline":
            assert caught.value.metadata["cause"] == "reference_census_budget_exhausted"
            assert budget.exhausted
        assert not budget.active and not reader.in_transaction
        assert reader.total_changes == changes
        assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == busy


@pytest.mark.parametrize("owned", [False, True])
def test_legacy_pin_inventory_discards_only_unrelated_aggregate_text(cow_fixture, owned):
    conn, root, _base, _config, _archive = cow_fixture
    original_pins = cow._live_pins(conn, "proj")
    text = "unrelated operational prose " * 350
    assert len(text.encode()) * 128 > cow.MAX_JSON_BYTES
    conn.executemany(
        "INSERT INTO backlog_bugs(bug_id,status,details_md,created_at,updated_at) "
        "VALUES(?,'OPEN',?,'2026','2026')",
        [(f"unrelated-{n}", text) for n in range(128)])
    conn.commit()
    before = hashlib.sha256(conn.serialize()).hexdigest()
    database = Path(conn.execute("PRAGMA database_list").fetchone()[2])

    def factory():
        reader = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        reader.row_factory = sqlite3.Row
        reader.execute("PRAGMA query_only=ON")
        return reader

    context = snapshots._owned_reference_connection(factory) if owned else nullcontext(conn)
    with context as reader:
        changes = reader.total_changes
        pins = cow._live_pins(reader, "proj")
        assert pins == original_pins
        cow._eligible(reader, "proj", "full-old", pins)
        assert reader.total_changes == changes
        assert len(cleanup.build_stale_artifact_cleanup_projection(
            reader, "proj", repo_root_path=root, dimension=cow.DIMENSION)["candidates"]) == 2
    assert before == hashlib.sha256(conn.serialize()).hexdigest()


@pytest.mark.parametrize("form", ["scalar", "prose", "path", "json", "escaped_path", "nested_json", "future_column"])
def test_legacy_pin_inventory_preserves_complete_row_and_decoded_matches(cow_fixture, form):
    conn, _root, base, *_ = cow_fixture
    field = "details_md"
    value = "full-old"
    if form == "prose":
        value = "prefix/full-old/suffix with surrounding prose"
    elif form == "path":
        value = str(base / "graph.json")
    elif form in {"json", "escaped_path", "nested_json"}:
        field = "takeover_json"
        value = json.dumps({"snapshot_path": str(base)})
        if form == "escaped_path":
            value = value.replace("/", "\\/")
        elif form == "nested_json":
            value = json.dumps({"serialized": value})
    elif form == "future_column":
        field = "future_snapshot_note"
        conn.execute("ALTER TABLE backlog_bugs ADD COLUMN future_snapshot_note TEXT")
    conn.execute(
        f"INSERT INTO backlog_bugs(bug_id,status,{field},created_at,updated_at) "
        "VALUES('legacy-positive','unknown',?,'2026','2026')", (value,))
    conn.commit()
    changes = conn.total_changes
    pins = cow._live_pins(conn, "proj")
    assert "full-old" in pins
    with pytest.raises(cow.CowRefusal, match="cow_snapshot_live_or_retained"):
        cow._eligible(conn, "proj", "full-old", pins)
    assert conn.total_changes == changes


@pytest.mark.parametrize("payload,reason", [
    ("{broken", "cow_live_payload_malformed:backlog_bugs"),
    (sqlite3.Binary(b'{}'), "cow_live_payload_unbounded:backlog_bugs"),
    (json.dumps({"large": "x" * (1024 * 1024)}), "cow_live_payload_unbounded:backlog_bugs"),
])
def test_legacy_pin_inventory_keeps_perfield_refusals(cow_fixture, payload, reason):
    conn = cow_fixture[0]
    conn.execute("INSERT INTO backlog_bugs(bug_id,status,takeover_json,created_at,updated_at) "
                 "VALUES('invalid-live','OPEN',?,'2026','2026')", (payload,))
    conn.commit()
    changes = conn.total_changes
    with pytest.raises(cow.CowRefusal, match=reason):
        cow._live_pins(conn, "proj")
    assert conn.total_changes == changes


def test_legacy_pin_inventory_keeps_individual_row_and_real_pin_byte_caps(cow_fixture):
    conn = cow_fixture[0]
    conn.execute("INSERT INTO backlog_bugs(bug_id,status,details_md,created_at,updated_at) "
                 "VALUES('oversized-row','OPEN',?,'2026','2026')", ("x" * (cow.MAX_JSON_BYTES + 1),))
    conn.commit()
    with pytest.raises(cow.CowRefusal, match="cow_reference_census_refused"):
        cow._live_pins(conn, "proj")
    # SQLite page transfer is below1MiB, but legacy JSON escaping expands this
    # individual row beyond1MiB. Preserve that original row-body refusal too.
    conn.execute("UPDATE backlog_bugs SET details_md=? WHERE bug_id='oversized-row'", ("雪" * 175000,))
    conn.commit()
    with pytest.raises(cow.CowRefusal, match="cow_live_census_unbounded"):
        cow._live_pins(conn, "proj")
    conn.execute("UPDATE backlog_bugs SET details_md='' WHERE bug_id='oversized-row'")
    # These are actual reference IDs, not unrelated serialized prose. Keep the
    # original aggregate pin-byte cap even when their count is below2000.
    conn.executemany(
        "INSERT INTO graph_snapshot_refs(project_id,ref_name,snapshot_id,updated_at,commit_sha) "
        "VALUES('proj',?,?,'2020','fixture')",
        [(f"large-pin-{n}", f"actual-ref-{n}-" + "z" * 1050) for n in range(1000)])
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM graph_snapshot_refs").fetchone()[0] < cow.MAX_LIVE_ROWS
    with pytest.raises(cow.CowRefusal, match="cow_live_census_unbounded"):
        cow._live_pins(conn, "proj")


def test_legacy_pin_inventory_keeps_real_live_row_cap(cow_fixture):
    conn = cow_fixture[0]
    conn.executemany(
        "INSERT INTO backlog_bugs(bug_id,status,created_at,updated_at) "
        "VALUES(?,'OPEN','2026','2026')", [(f"row-{n}",) for n in range(cow.MAX_LIVE_ROWS + 1)])
    conn.commit()
    with pytest.raises(cow.CowRefusal, match="cow_live_window_unbounded:backlog_bugs"):
        cow._live_pins(conn, "proj")


@pytest.mark.parametrize("declared_type,payload", [
    ("BLOB", sqlite3.Binary(b'{"snapshot_id":"scope-opaque-reference"}')),
    ("BLOB", sqlite3.Binary(b'\x00\xffopaque-owner')),
    ("TEXT", sqlite3.Binary(b'{"snapshot_id":"scope-opaque-reference"}')),
    ("INTEGER", 7),
])
def test_typed_rework_unprojected_owner_storage_protects_selector_and_public(
        cow_fixture, declared_type, payload):
    conn, root, base, *_ = cow_fixture
    sid = "scope-opaque-reference"
    conn.execute("INSERT INTO graph_snapshots(project_id,snapshot_id,commit_sha,snapshot_kind,status,created_at) "
                 "VALUES ('proj',?,'fixture','scope','superseded','2020')", (sid,))
    path = snapshots._snapshot_root("proj", sid)
    path.mkdir(parents=True)
    (path / "graph.json").write_text('{}')
    conn.execute(f"CREATE TABLE ordinary_audit(payload {declared_type})")
    conn.execute("INSERT INTO ordinary_audit VALUES (?)", (payload,))
    conn.commit()
    before = hashlib.sha256(conn.serialize()).hexdigest()
    changes = conn.total_changes
    files = {str(p): p.read_bytes() for p in base.parent.rglob('*') if p.is_file()}
    fetched = []

    def row_factory(cursor, row):
        fetched.extend(value for col, value in zip(cursor.description, row)
                       if col[0] == "payload" or isinstance(value, bytes))
        return sqlite3.Row(cursor, row)

    conn.row_factory = row_factory
    state = snapshots.snapshot_retention_reference_state(conn, "proj")
    assert not state["complete"]
    assert any(reason.startswith("ordinary_audit_payload_") for reason in state["refusal_reasons"])
    metadata = state["refusal_metadata"][0]
    assert metadata["table"] == "ordinary_audit" and metadata["field"] == "payload"
    assert metadata["body_fetched"] is False and metadata["complete"] is False
    assert metadata["sqlite_storage_type"] == ("blob" if isinstance(payload, (bytes, memoryview))
                                                else "integer" if isinstance(payload, int) else "text")
    selected = snapshots.select_snapshot_retention_candidates(
        conn, "proj", keep_last_n=0, extra_bundle_snapshot_ids=set())
    assert not selected["reference_authority_complete"] and selected["candidates"] == []
    assert "reference_authority_incomplete" in next(
        r["reasons"] for r in selected["protected"] if r["snapshot_id"] == sid)
    public = cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=root, dimension="graph_snapshots")
    assert public["summary"]["safe_apply_count"] == 0
    assert any(reason.startswith("ordinary_audit_payload_") for reason in public["global_refusal_reasons"])
    own_rows = [r for r in public["candidates"] if r["snapshot_id"] == sid]
    assert own_rows and all(r["safe_to_apply"] is False for r in own_rows)
    assert fetched == [] and conn.total_changes == changes
    assert before == hashlib.sha256(conn.serialize()).hexdigest()
    assert files == {str(p): p.read_bytes() for p in base.parent.rglob('*') if p.is_file()}
    assert not cow._state_root("proj").exists()


@pytest.mark.parametrize("declared_type", ["TEXT", "", "BLOB"])
def test_typed_rework_supported_actual_text_owner_is_censusable(cow_fixture, declared_type):
    conn, root, *_ = cow_fixture
    conn.execute(f"CREATE TABLE ordinary_audit(payload {declared_type}, optional_note TEXT)")
    conn.execute("INSERT INTO ordinary_audit VALUES (?, NULL)",
                 (json.dumps({"nested": {"snapshot_id": "full-old"}}),))
    conn.commit()
    before = hashlib.sha256(conn.serialize()).hexdigest()
    state = snapshots.snapshot_retention_reference_state(conn, "proj")
    assert state["complete"] and state["refusal_reasons"] == []
    assert "full-old" in state["current_use"] and "full-old" in state["durable_references"]
    public = cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=root, dimension="graph_snapshots")
    assert public["global_refusal_reasons"] == []
    own_rows = [r for r in public["candidates"] if r["snapshot_id"] == "full-old"]
    assert own_rows and all(not r["safe_to_apply"] for r in own_rows)
    assert before == hashlib.sha256(conn.serialize()).hexdigest()


def test_typed_rework_fts_logical_owner_carries_pin_but_shadow_name_is_not_authority(cow_fixture):
    conn, *_ = cow_fixture
    conn.execute("CREATE VIRTUAL TABLE ordinary_audit_fts USING fts5 (payload)")
    conn.execute("INSERT INTO ordinary_audit_fts VALUES (?)", ('{"snapshot_id":"full-old"}',))
    conn.commit()
    before = hashlib.sha256(conn.serialize()).hexdigest()
    state = snapshots.snapshot_retention_reference_state(conn, "proj")
    assert state["complete"] and "full-old" in state["protected"]
    assert before == hashlib.sha256(conn.serialize()).hexdigest()
    # This is an ordinary table, not a SQLite-confirmed FTS5 shadow.
    conn.execute("CREATE TABLE fabricated_fts_data(payload BLOB)")
    conn.execute("INSERT INTO fabricated_fts_data VALUES (?)", (sqlite3.Binary(b'opaque-owner'),))
    conn.commit()
    state = snapshots.snapshot_retention_reference_state(conn, "proj")
    assert not state["complete"] and "fabricated_fts_data_payload_unreadable" in state["refusal_reasons"]


def test_typed_rework_contentless_fts_never_proves_absence(cow_fixture):
    conn, root, *_ = cow_fixture
    conn.execute("CREATE VIRTUAL TABLE opaque_audit_fts USING fts5(payload, content='')")
    conn.execute("INSERT INTO opaque_audit_fts VALUES (?)", ('{"snapshot_id":"full-old"}',))
    conn.commit()
    assert conn.execute("SELECT payload FROM opaque_audit_fts").fetchone()[0] is None
    state = snapshots.snapshot_retention_reference_state(conn, "proj")
    assert not state["complete"] and state["refusal_reasons"]
    selected = snapshots.select_snapshot_retention_candidates(
        conn, "proj", keep_last_n=0, extra_bundle_snapshot_ids=set())
    assert not selected["reference_authority_complete"] and not selected["candidates"]
    public = cleanup.build_stale_artifact_cleanup_projection(
        conn, "proj", repo_root_path=root, dimension="graph_snapshots")
    assert public["global_refusal_reasons"] and public["summary"]["safe_apply_count"] == 0


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


def _insert_census_contract(conn, payload):
    conn.execute(
        "INSERT INTO contract_runtime_executions "
        "(contract_execution_id,project_id,backlog_id,contract_id,version,"
        "revision,execution_state_revision,record_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("private-census-cex", "proj", "backlog", "contract", "1", "1", 1,
         payload, "now", "now"),
    )
    conn.commit()


def _observe_census_record_fetches(conn):
    fetched_lengths = []
    def row_factory(cursor, row):
        for column, value in zip(cursor.description, row):
            if column[0] == "record_json" and value is not None:
                fetched_lengths.append(len(value))
        return sqlite3.Row(cursor, row)
    conn.row_factory = row_factory
    return fetched_lengths


@pytest.mark.parametrize("entrypoint", ["preview", "apply"])
def test_census_oversized_multibyte_late_pin_without_fetch(cow_fixture, entrypoint):
    if entrypoint == "apply" and sys.platform != "darwin":
        pytest.skip("native apply preflight requires macOS")
    conn, _, base, _, _ = cow_fixture
    approved = preview(cow_fixture)
    before = {p: p.read_bytes() for p in base.rglob("*.json")}
    payload = json.dumps({"runtime_guide": {"next_legal_action": 0},
                          "private_payload": "雪" * 350_000,
                          "late_pin": {"snapshot_id": "full-old"}}, ensure_ascii=False)
    assert len(payload) < cow.MAX_JSON_BYTES < len(payload.encode("utf-8"))
    _insert_census_contract(conn, payload)
    fetched_lengths = _observe_census_record_fetches(conn)
    plan = preview(cow_fixture)
    assert plan["candidates"] == [] and plan["writes_performed"] is False
    assert any(r["reason"] == "cow_snapshot_live_or_retained" for r in plan["refusals"])
    if entrypoint == "apply":
        with pytest.raises(cleanup.StaleArtifactCleanupError) as caught:
            apply(cow_fixture, approved)
        assert caught.value.payload["writes_performed"] is False
    assert fetched_lengths == []
    assert "private-census-cex" not in json.dumps(plan) and "雪" not in json.dumps(plan, ensure_ascii=False)
    assert not cow._state_root("proj").exists()
    assert {p: p.read_bytes() for p in before} == before


def test_census_large_completed_contract_is_skipped_without_body_fetch(cow_fixture):
    conn = cow_fixture[0]
    payload = json.dumps({"runtime_guide": {"next_legal_action": None},
                          "snapshot_id": "full-old", "history": "雪" * 350_000}, ensure_ascii=False)
    assert len(payload.encode("utf-8")) > cow.MAX_JSON_BYTES
    _insert_census_contract(conn, payload)
    fetched_lengths = _observe_census_record_fetches(conn)
    plan = preview(cow_fixture)
    assert len(plan["candidates"]) == 2
    assert plan["writes_performed"] is False
    assert fetched_lengths == []


@pytest.mark.parametrize("duplicate_level", ["runtime_guide", "next_legal_action"])
@pytest.mark.parametrize("last_completed", [False, True])
def test_census_large_duplicate_keys_match_python_last_member(cow_fixture, duplicate_level, last_completed):
    conn = cow_fixture[0]
    first, last = ('{"snapshot_id":"full-old"}', 'null') if last_completed else (
        'null', '{"snapshot_id":"full-old"}')
    if duplicate_level == "runtime_guide":
        members = ('"runtime_guide":{"next_legal_action":' + first + '},'
                   '"runtime_guide":{"next_legal_action":' + last + '}')
    else:
        members = ('"runtime_guide":{"next_legal_action":' + first + ','
                   '"next_legal_action":' + last + '}')
    payload = '{' + members + ',"history":' + json.dumps("雪" * 350_000, ensure_ascii=False) + '}'
    assert (json.loads(payload)["runtime_guide"]["next_legal_action"] is None) == last_completed
    assert len(payload.encode("utf-8")) > cow.MAX_JSON_BYTES
    _insert_census_contract(conn, payload)
    fetched_lengths = _observe_census_record_fetches(conn)
    if last_completed:
        assert len(preview(cow_fixture)["candidates"]) == 2
    else:
        plan = preview(cow_fixture)
        assert plan["candidates"] == [] and plan["writes_performed"] is False
    assert fetched_lengths == []
    assert not cow._state_root("proj").exists()


@pytest.mark.parametrize("action", [0, True, False, ""])
def test_census_large_nonnull_scalar_action_remains_live(cow_fixture, action):
    conn = cow_fixture[0]
    payload = json.dumps({"runtime_guide": {"next_legal_action": action},
                          "history": "雪" * 350_000}, ensure_ascii=False)
    _insert_census_contract(conn, payload)
    fetched_lengths = _observe_census_record_fetches(conn)
    assert len(preview(cow_fixture)["candidates"]) == 2
    assert fetched_lengths == []


@pytest.mark.parametrize("payload,cause,storage_type", [
    (sqlite3.Binary(b'{"runtime_guide":{"next_legal_action":null}}'), "nontext", "blob"),
    ('{"runtime_guide":', "malformed_json", "text"),
    ('{}', "missing_runtime_guide", "text"),
    ('{"runtime_guide":null}', "missing_runtime_guide", "text"),
    ('{"runtime_guide":{}}', "missing_next_legal_action", "text"),
    ('[]', "missing_runtime_guide", "text"),
])
def test_census_contract_storage_and_guide_states_are_distinct(cow_fixture, payload, cause, storage_type):
    conn = cow_fixture[0]
    _insert_census_contract(conn, payload)
    assert conn.execute("SELECT typeof(record_json) FROM contract_runtime_executions").fetchone()[0] == storage_type
    with pytest.raises(cleanup.StaleArtifactCleanupError) as caught:
        preview(cow_fixture)
    details = caught.value.payload
    facts = details["refusal_metadata"]
    assert facts["cause"] == cause and facts["sqlite_storage_type"] == storage_type
    assert facts["storage_byte_length"] == len(payload.encode("utf-8") if isinstance(payload, str) else payload)
    assert facts["utf8_byte_length"] == (len(payload.encode("utf-8")) if storage_type == "text" else None)
    assert details["apply_plan_available"] is False and details["writes_performed"] is False
    assert not cow._state_root("proj").exists()


def test_census_null_required_contract_payload_is_not_optional_absence(cow_fixture):
    conn = cow_fixture[0]
    # TEXT affinity is a schema name/type check, not a NOT NULL guarantee.
    conn.execute("DROP TABLE contract_runtime_executions")
    conn.execute("CREATE TABLE contract_runtime_executions "
                 "(contract_execution_id TEXT PRIMARY KEY, project_id TEXT, backlog_id TEXT, record_json TEXT)")
    conn.execute("INSERT INTO contract_runtime_executions VALUES('private-census-cex','proj','backlog',NULL)")
    conn.commit()
    with pytest.raises(cleanup.StaleArtifactCleanupError) as caught:
        preview(cow_fixture)
    details = caught.value.payload
    facts = details["refusal_metadata"]
    assert details["refusal_reason"] == "cow_contract_live_state_unknown"
    assert facts["cause"] == "null" and facts["sqlite_storage_type"] == "null"
    assert facts["utf8_byte_length"] is None and facts["storage_byte_length"] is None
    assert details["apply_plan_available"] is False


@pytest.mark.parametrize("reference_kind", ["escaped_snapshot", "serialized_audit", "path", "run"])
def test_typed_census_late_page_and_decoded_late_pin(cow_fixture, reference_kind):
    conn, _, base, _, _ = cow_fixture
    if reference_kind == "run":
        _insert_claim(conn, run_id="unique-current-run")
    if reference_kind == "escaped_snapshot":
        late = '{"snapshot_id":"full\\u002dold"}'
    elif reference_kind == "serialized_audit":
        late = json.dumps('{"snapshot_id":"full\\u002dold"}')
    elif reference_kind == "path":
        late = json.dumps(str(base / "graph.json"))
    else:
        late = json.dumps("unique-current-run")
    # More than one internal page; late identity follows >1MiB of multibyte data.
    for index in range(70):
        payload = '{"runtime_guide":{"next_legal_action":0},"history":' + json.dumps(
            "雪" * 350_000 if index == 69 else "audit", ensure_ascii=False) + ',"late":' + (
            late if index == 69 else 'null') + '}'
        conn.execute("INSERT INTO contract_runtime_executions "
            "(contract_execution_id,project_id,backlog_id,contract_id,version,revision,execution_state_revision,record_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", (f"cex-{index:03d}", "proj", "backlog", "contract", "1", "1", 1, payload, "now", "now"))
    conn.commit()
    fetched = _observe_census_record_fetches(conn)
    statements = []
    conn.set_trace_callback(statements.append)
    plan = preview(cow_fixture)
    conn.set_trace_callback(None)
    assert plan["candidates"] == [] and plan["writes_performed"] is False
    assert fetched == []
    pages = [q for q in statements if 'FROM "contract_runtime_executions"' in q and 'ORDER BY _rowid_' in q]
    assert len(pages) >= 3 and any('_rowid_>64' in q for q in pages)


def test_typed_census_native_complex_shape_over_20k_nodes_still_projects_late_pin(cow_fixture):
    conn = cow_fixture[0]
    # Actual measured native shape exceeds 20,001 tree nodes and has four
    # valid serialized JSON strings. Traversal is complete inside SQLite.
    payload = json.dumps({"runtime_guide": {"next_legal_action": 0},
        "history": [{"step": index, "notes": "雪" * 48, "details": ["audit", index]} for index in range(6000)],
        "serialized": [json.dumps({"audit": ["record", {"late_snapshot_id": "full-old"}]}) for _ in range(4)],
        "late_snapshot_id": "full-old"}, ensure_ascii=False)
    assert len(payload.encode()) > cow.MAX_JSON_BYTES
    assert conn.execute("SELECT count(*) FROM json_tree(?)", (payload,)).fetchone()[0] > 20_001
    _insert_census_contract(conn, payload)
    fetched = _observe_census_record_fetches(conn)
    plan = preview(cow_fixture)
    assert plan["reference_census"]["complete"] and plan["candidates"] == []
    assert fetched == [] and plan["writes_performed"] is False


def test_typed_census_unfinished_projection_keeps_causal_metadata(cow_fixture):
    conn = cow_fixture[0]
    nested = '{"snapshot_id":"full-old"}'
    for _ in range(10):
        nested = json.dumps({"serialized_audit": nested})
    payload = json.dumps({"runtime_guide": {"next_legal_action": 0}, "history": "雪" * 350_000,
                          "nested": nested}, ensure_ascii=False)
    _insert_census_contract(conn, payload)
    fetched = _observe_census_record_fetches(conn)
    with pytest.raises(cleanup.StaleArtifactCleanupError) as caught:
        preview(cow_fixture)
    facts = caught.value.payload["refusal_metadata"]
    assert facts["cause"] == "typed_projection_incomplete" and facts["complete"] is False
    assert facts["sqlite_storage_type"] == "text" and facts["utf8_byte_length"] == len(payload.encode())
    assert facts["body_fetched"] is False and facts["execution_identity_hash"].startswith("sha256:")
    assert caught.value.payload["writes_performed"] is False and fetched == []
    assert not cow._state_root("proj").exists()


@pytest.mark.parametrize("payload,cause", [
    ('{"runtime_guide":{"next_legal_action":null}}' + chr(0) + '{"late":"full-old"}', "malformed_json"),
    ('{"runtime_guide":{"next_legal_action":0},"late":"prefix\\u0000full-old"}', "typed_projection_incomplete"),
    ('{"runtime_guide\\u0000suffix":{"next_legal_action":null},"late":"full-old"}', "missing_runtime_guide"),
])
def test_typed_census_nul_or_nul_key_never_proves_pin_absence(cow_fixture, payload, cause):
    conn = cow_fixture[0]
    _insert_census_contract(conn, payload)
    fetched = _observe_census_record_fetches(conn)
    with pytest.raises(cleanup.StaleArtifactCleanupError) as caught:
        preview(cow_fixture)
    assert caught.value.payload["refusal_metadata"]["cause"] == cause
    assert caught.value.payload["refusal_metadata"]["utf8_byte_length"] == len(payload.encode())
    assert caught.value.payload["writes_performed"] is False and fetched == []


def test_typed_census_unknown_owner_and_column_stay_protective(cow_fixture):
    conn = cow_fixture[0]
    conn.execute("CREATE TABLE future_qa_session (project_id TEXT, payload_json TEXT)")
    conn.execute("INSERT INTO future_qa_session VALUES ('proj','{}')")
    conn.commit()
    with pytest.raises(cleanup.StaleArtifactCleanupError) as unknown:
        preview(cow_fixture)
    assert unknown.value.payload["refusal_metadata"]["cause"] == "owner_schema_unknown"
    assert unknown.value.payload["refusal_metadata"]["body_fetched"] is False
    conn.execute("ALTER TABLE contract_runtime_executions ADD COLUMN future_pins TEXT")
    conn.commit()
    with pytest.raises(cleanup.StaleArtifactCleanupError) as caught:
        preview(cow_fixture)
    assert caught.value.payload["refusal_metadata"]["cause"] == "contract_runtime_executions_owner_schema_unknown"
    assert caught.value.payload["writes_performed"] is False


def test_typed_census_live_pin_arriving_after_approval_refuses_apply(cow_fixture):
    if sys.platform != "darwin":
        pytest.skip("native apply preflight requires macOS")
    conn = cow_fixture[0]
    approved = preview(cow_fixture)
    _insert_census_contract(conn, json.dumps({"runtime_guide": {"next_legal_action": 0},
                                            "late_snapshot_id": "full-old"}))
    with pytest.raises(cleanup.StaleArtifactCleanupError) as caught:
        apply(cow_fixture, approved)
    assert caught.value.payload["writes_performed"] is False
    assert not cow._state_root("proj").exists()


# Read-only native row from reviewer-candidate-claim-readonly.json,
# SHA256 faa9d07fe4ce1311601dab31739a49e0f2ed4a2bce1a032d8ff0b957add56bf3.
# Only the project is remapped into the isolated fixture; no live DB is opened.
_OBSERVED_RELEASED_CLAIM = {
    "acquired_at": "2026-09-06T04:54:06Z",
    "claim_id": "gcfclaim-37d2e8ac3b6542c6a042",
    "commit_sha": "63472f630faad83701d69a799a81a9452208b7e9",
    "manager_epoch": "govgen-f49aa54ffa6d4b84ac97da0223b4208a",
    "manager_pid": 78113,
    "manager_start_identity": "sha256:71fab283b80412c3c4216ee5ec2562f0c221b09a6fc832f4061e51a6dc7838d9",
    "manager_started_at": "2026-09-06T04:42:17Z",
    "project_id": "aming-claw",
    "released_at": "2026-09-06T04:58:21Z",
    "run_id": "current-full-mcp-1755abf70c3bd306",
    "snapshot_id": "full-63472f630faa-44e17fae3823",
    "status": "released",
    "terminal_status": "candidate_ready",
}


def _insert_claim(conn, **overrides):
    claim = {**_OBSERVED_RELEASED_CLAIM, "project_id": "proj", "snapshot_id": "full-old", **overrides}
    conn.execute("INSERT INTO graph_current_full_build_claim_history (" + ",".join(claim) + ") "
                 "VALUES (" + ",".join("?" for _ in claim) + ")", tuple(claim.values()))
    conn.commit()
    return claim


@pytest.mark.parametrize("outcome", ["candidate_ready", "failed"])
def test_observed_canonical_released_claim_admits_small_native_preview(cow_fixture, outcome):
    conn, _, base, config, _ = cow_fixture
    sid = _OBSERVED_RELEASED_CLAIM["snapshot_id"]
    base.rename(base.with_name(sid))
    conn.execute("UPDATE graph_snapshots SET snapshot_id=?,commit_sha=?,created_at=? "
                 "WHERE snapshot_id='full-old'", (sid, _OBSERVED_RELEASED_CLAIM["commit_sha"],
                                                "2026-09-06T04:57:19Z"))
    conn.execute("UPDATE graph_snapshots SET created_at='2026-09-29T00:00:00Z' "
                 "WHERE snapshot_id='full-active'")
    claim = _insert_claim(conn, snapshot_id=sid, terminal_status=outcome)
    before_changes, before_config = conn.total_changes, json.dumps(config, sort_keys=True)
    plan = preview(cow_fixture)
    assert plan["apply_plan_available"] and len(plan["candidates"]) == 2
    assert {r["snapshot_id"] for r in plan["candidates"]} == {sid}
    assert plan["writes_performed"] is False
    assert conn.total_changes == before_changes
    assert dict(conn.execute("SELECT * FROM graph_current_full_build_claim_history").fetchone()) == claim
    assert json.dumps(config, sort_keys=True) == before_config
    assert not cow._state_root("proj").exists()


def test_native_failed_claim_terminalization_is_history(cow_fixture):
    conn = cow_fixture[0]
    claim = _insert_claim(conn, status="active", released_at="", terminal_status="")
    released = snapshots.terminalize_current_full_build_claim(
        conn, "proj", claim_id=claim["claim_id"], run_id=claim["run_id"], snapshot_id="full-old",
        commit_sha=claim["commit_sha"], manager_start_identity=claim["manager_start_identity"],
        terminal_status="failed")
    assert released["status"] == "released" and released["terminal_status"] == "failed"
    assert len(preview(cow_fixture)["candidates"]) == 2
    assert dict(conn.execute("SELECT * FROM graph_current_full_build_claim_history").fetchone()) == released


@pytest.mark.parametrize("overrides", [
    {"status": None}, {"status": ""}, {"status": "unknown"}, {"status": "RELEASED"},
    {"terminal_status": None}, {"terminal_status": ""}, {"terminal_status": "unknown"},
    {"terminal_status": "complete"}, {"terminal_status": "CANDIDATE_READY"},
    {"released_at": None}, {"released_at": ""}, {"released_at": "not-a-date"},
    {"released_at": "2026-02-30T04:58:21Z"}, {"released_at": "2026-09-06T24:00:00Z"},
    {"released_at": "2026-09-06T04:58:60Z"}, {"released_at": "0000-01-01T00:00:00Z"},
    {"released_at": "2026-09-06T04:58:21"}, {"released_at": "2026-09-06T04:58:21+00:00"},
    {"released_at": "2026-09-06T04:58:21.000Z"}, {"released_at": "2026-09-06T04:58:21z"},
    {"released_at": " 2026-09-06T04:58:21Z"},
    *({field: value} for field in ("claim_id", "run_id", "commit_sha", "manager_epoch",
                                   "manager_start_identity", "manager_started_at", "acquired_at")
      for value in (None, "", " ")),
    {"claim_id": "\x00broken"}, {"run_id": "run\n"}, {"manager_epoch": "\tepoch"},
    {"commit_sha": "63472f6"}, {"commit_sha": "z" * 40}, {"commit_sha": "a" * 41},
    {"manager_start_identity": "sha256:invalid"}, {"manager_start_identity": "sha256:" + "z" * 64},
    {"manager_pid": None}, {"manager_pid": 0}, {"manager_pid": -1}, {"manager_pid": "unknown"},
    {"manager_started_at": "2026-02-30T04:42:17Z"}, {"acquired_at": "2026-09-06T04:54:06"},
])
def test_incomplete_or_noncanonical_released_claim_remains_protective(cow_fixture, overrides):
    conn = cow_fixture[0]
    # A damaged/legacy store can contain NULL even though the native schema
    # forbids it. Preserve every native field while loosening fixture constraints.
    conn.executescript("CREATE TABLE claim_fixture AS SELECT * FROM graph_current_full_build_claim_history; "
                       "DROP TABLE graph_current_full_build_claim_history; "
                       "ALTER TABLE claim_fixture RENAME TO graph_current_full_build_claim_history;")
    _insert_claim(conn, **overrides)
    assert preview(cow_fixture)["candidates"] == []


@pytest.mark.parametrize("field,value", [
    ("commit_sha", b"a" * 40), ("commit_sha", b"a" * 64),
    ("commit_sha", "a" * 40 + "\x00malformed"), ("commit_sha", "a" * 64 + "\x00malformed"),
    ("manager_start_identity", "sha256:" + "a" * 64 + "\x00malformed"),
    *((field, ("full-old" if field == "snapshot_id" else _OBSERVED_RELEASED_CLAIM[field]).encode()) for field in
      ("claim_id", "snapshot_id", "run_id", "manager_epoch", "manager_start_identity",
       "released_at", "manager_started_at", "acquired_at", "status", "terminal_status")),
    *((field, _OBSERVED_RELEASED_CLAIM[field] + "\x00malformed") for field in
      ("released_at", "manager_started_at", "acquired_at", "status", "terminal_status")),
])
def test_native_text_affinity_cannot_make_blob_or_nul_proof_canonical(cow_fixture, field, value):
    conn = cow_fixture[0]
    # Keep native schema: TEXT affinity can still store BLOB. SQLite string
    # length/GLOB/substr can also ignore a NUL suffix in a stored TEXT value.
    claim = _insert_claim(conn, **{field: value})
    stored_type = conn.execute("SELECT typeof(" + field + ") FROM graph_current_full_build_claim_history").fetchone()[0]
    assert stored_type == ("blob" if isinstance(value, bytes) else "text")
    before_changes = conn.total_changes
    assert preview(cow_fixture)["candidates"] == []
    assert conn.total_changes == before_changes
    assert dict(conn.execute("SELECT * FROM graph_current_full_build_claim_history").fetchone()) == claim


@pytest.mark.parametrize("field", ["claim_id", "run_id", "manager_epoch"])
def test_owner_identity_boundaries_match_complete_native_strip_set(cow_fixture, field):
    conn = cow_fixture[0]
    claim = _insert_claim(conn)
    # Behavioral oracle is the producer's actual Python normalization, including
    # control separators and Unicode whitespace; interior characters stay valid.
    whitespace = [chr(point) for point in range(sys.maxunicode + 1) if chr(point).isspace()]
    for space in whitespace:
        for raw in (space, space + "owner", "owner" + space, "owner" + space + "identity"):
            conn.execute("UPDATE graph_current_full_build_claim_history SET " + field + "=?", (raw,))
            conn.commit()
            claim[field] = raw
            before_changes = conn.total_changes
            pins = cow._live_pins(conn, "proj")
            if not raw.strip() or raw.strip() != raw:
                with pytest.raises(cow.CowRefusal, match="cow_snapshot_live_or_retained"):
                    cow._eligible(conn, "proj", "full-old", pins)
            else:
                cow._eligible(conn, "proj", "full-old", pins)
            assert conn.total_changes == before_changes
            assert dict(conn.execute("SELECT * FROM graph_current_full_build_claim_history").fetchone()) == claim


def test_native_acquire_normalizes_boundaries_and_preserves_internal_unicode(cow_fixture):
    conn, _, base, _, _ = cow_fixture
    sid = "full-native-normalized"
    base.rename(base.with_name(sid))
    conn.execute("DELETE FROM graph_snapshots WHERE snapshot_id='full-old'")
    conn.commit()
    claim = snapshots.acquire_current_full_build_claim(
        conn, "\u00a0proj\u3000", run_id="\u00a0run\u00a0identity\u3000",
        snapshot_id="\u2003" + sid + "\x1c", commit_sha=_OBSERVED_RELEASED_CLAIM["commit_sha"],
        manager_epoch="\u2003epoch\u2003identity\x1c", manager_pid=_OBSERVED_RELEASED_CLAIM["manager_pid"],
        manager_started_at=_OBSERVED_RELEASED_CLAIM["manager_started_at"],
        manager_start_identity=_OBSERVED_RELEASED_CLAIM["manager_start_identity"])
    assert claim["project_id"] == "proj" and claim["snapshot_id"] == sid
    assert claim["run_id"] == "run\u00a0identity" and claim["manager_epoch"] == "epoch\u2003identity"
    released = snapshots.terminalize_current_full_build_claim(
        conn, "proj", claim_id=claim["claim_id"], run_id=claim["run_id"], snapshot_id=sid,
        commit_sha=claim["commit_sha"], manager_start_identity=claim["manager_start_identity"],
        terminal_status="failed")
    conn.execute("INSERT INTO graph_snapshots(project_id,snapshot_id,snapshot_kind,status,created_at,commit_sha) "
                 "VALUES('proj',?,'full','superseded','2020-01-01',?)", (sid, claim["commit_sha"]))
    conn.commit()
    assert len(preview(cow_fixture)["candidates"]) == 2
    assert dict(conn.execute("SELECT * FROM graph_current_full_build_claim_history").fetchone()) == released


@pytest.mark.parametrize("column", ["released_at", "terminal_status", "claim_id", "run_id", "commit_sha",
                                    "manager_epoch", "manager_pid", "manager_started_at",
                                    "manager_start_identity", "acquired_at"])
def test_released_claim_schema_missing_proof_refuses(cow_fixture, column):
    conn = cow_fixture[0]
    _insert_claim(conn)
    # Rebuild a partial legacy fixture schema, including omissions of indexed
    # identity columns; never use a schema bypass or the original governance DB.
    fields = [r[1] for r in conn.execute("PRAGMA table_info(graph_current_full_build_claim_history)")
              if r[1] != column]
    conn.executescript("CREATE TABLE claim_fixture AS SELECT " + ",".join(fields) +
                       " FROM graph_current_full_build_claim_history; "
                       "DROP TABLE graph_current_full_build_claim_history; "
                       "ALTER TABLE claim_fixture RENAME TO graph_current_full_build_claim_history;")
    conn.commit()
    with pytest.raises(cow.CowRefusal, match="cow_live_schema_incomplete:graph_current_full_build_claim_history"):
        cow._live_pins(conn, "proj")


@pytest.mark.parametrize("fence", ["same_sid_active", "process"])
def test_released_history_never_overrides_current_full_writer(cow_fixture, monkeypatch, fence):
    conn = cow_fixture[0]
    _insert_claim(conn)
    if fence == "same_sid_active":
        _insert_claim(conn, claim_id="active-same-sid", run_id="active-run", status="active",
                      released_at="", terminal_status="")
    else:
        monkeypatch.setattr(server, "_CURRENT_FULL_BUILD_KEYS", {("proj", "fixture-current-full")})
    with pytest.raises(cow.CowRefusal, match="cow_current_full_writer_active"):
        cow._live_pins(conn, "proj")


@pytest.mark.parametrize("reference", ["active_ref", "newest", "retention", "pending", "backlog", "qa", "lease"])
def test_independent_reference_wins_over_released_claim(cow_fixture, reference):
    conn, _, _, config, _ = cow_fixture
    _insert_claim(conn)
    if reference == "active_ref":
        conn.execute("UPDATE graph_snapshot_refs SET snapshot_id='full-old'")
    elif reference == "newest":
        conn.execute("UPDATE graph_snapshots SET created_at='2999' WHERE snapshot_id='full-old'")
    elif reference == "retention":
        config["governance"]["snapshot_retention"]["keep_last_n"] = 2
    elif reference == "pending":
        conn.execute("INSERT INTO pending_scope_reconcile(project_id,commit_sha,queued_at,status,snapshot_id) "
                     "VALUES('proj','fixture','2026','pending','full-old')")
    elif reference == "backlog":
        conn.execute("INSERT INTO backlog_bugs(bug_id,status,takeover_json,created_at,updated_at) "
                     "VALUES('fixture','OPEN','{\"snapshot_id\":\"full-old\"}','2026','2026')")
    elif reference == "qa":
        conn.execute("CREATE TABLE fixture_qa_sessions(project_id,snapshot_id,status)")
        conn.execute("INSERT INTO fixture_qa_sessions VALUES('proj','full-old','active')")
    else:
        conn.execute("INSERT INTO graph_semantic_jobs(project_id,snapshot_id,node_id,status,lease_expires_at) "
                     "VALUES('proj','full-old','fixture','ai_complete','2999-01-01T00:00:00+00:00')")
    conn.commit()
    assert preview(cow_fixture)["candidates"] == []


def test_released_claim_with_present_future_lease_stays_protective(cow_fixture):
    conn = cow_fixture[0]
    conn.execute("ALTER TABLE graph_current_full_build_claim_history ADD COLUMN lease_expires_at TEXT")
    _insert_claim(conn, lease_expires_at="2999-01-01T00:00:00+00:00")
    assert preview(cow_fixture)["candidates"] == []


def test_released_label_in_other_store_stays_protective(cow_fixture):
    conn = cow_fixture[0]
    _insert_claim(conn)
    conn.execute("INSERT INTO graph_query_traces(trace_id,project_id,snapshot_id,query_source,query_purpose,"
                 "status,created_at,updated_at) VALUES('fixture','proj','full-old','test','test','released','2026','2026')")
    conn.commit()
    assert preview(cow_fixture)["candidates"] == []
    assert "released" not in cow.TERMINAL


def test_released_histories_do_not_consume_bounded_live_row_budget(cow_fixture, monkeypatch):
    conn = cow_fixture[0]
    monkeypatch.setattr(cow, "MAX_LIVE_ROWS", 2)
    for index in range(7):
        _insert_claim(conn, claim_id=f"history-{index}", run_id=f"run-{index}",
                      terminal_status="candidate_ready" if index % 2 else "failed")
    assert len(preview(cow_fixture)["candidates"]) == 2
    assert conn.execute("SELECT COUNT(*) FROM graph_current_full_build_claim_history").fetchone()[0] == 7
    # Valid histories must not hide a later live row or dilute its own budget.
    _insert_claim(conn, claim_id="unknown", run_id="unknown-run", status="unknown")
    assert preview(cow_fixture)["candidates"] == []
    for index in range(2):
        _insert_claim(conn, claim_id=f"unknown-{index}", run_id=f"unknown-run-{index}", status="unknown")
    with pytest.raises(cow.CowRefusal, match="cow_live_window_unbounded:graph_current_full_build_claim_history"):
        cow._live_pins(conn, "proj")


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


@pytest.mark.skipif(sys.platform != "darwin", reason="real mounted APFS resolver requires macOS")
@pytest.mark.parametrize("location", ["internal", "external"])
def test_volume_resolves_real_nested_mounted_paths(tmp_path, location):
    if location == "external":
        configured = os.environ.get("AMING_CLAW_COW_TEST_ARCHIVE_ROOT")
        if not configured:
            pytest.skip("isolated external fixture root was not supplied")
        root = Path(configured)
    else:
        root = tmp_path.resolve()
    with tempfile.TemporaryDirectory(prefix="cow-volume-regression-", dir=root) as temporary:
        nested = Path(temporary) / "nested path" / "child"
        nested.mkdir(parents=True)
        before = nested.stat()
        volume = cow._volume(nested)
        # Independent diskutil query of the resolved mounted volume, unmocked.
        raw = subprocess.run(["diskutil", "info", "-plist", volume["mount"]],
                             capture_output=True, check=True, timeout=10)
        info = plistlib.loads(raw.stdout)
        assert volume == {"uuid": info["VolumeUUID"], "mount": info["MountPoint"],
                          "device": before.st_dev}
        assert info["FilesystemType"].lower() == "apfs"
        assert Path(volume["mount"]).stat().st_dev == before.st_dev
        assert (nested.stat().st_dev, nested.stat().st_ino) == (before.st_dev, before.st_ino)
        link = Path(temporary) / "link"
        link.symlink_to(nested, target_is_directory=True)
        with pytest.raises(cow.CowRefusal, match="symlink_component"):
            cow._volume(link)


@pytest.mark.skipif(sys.platform != "darwin", reason="mounted-device resolver requires macOS")
@pytest.mark.parametrize("command", ["df", "diskutil"])
@pytest.mark.parametrize("failure", ["missing", "exit", "timeout", "malformed", "malformed_xml"])
def test_volume_command_failures_are_typed_before_writes(tmp_path, monkeypatch, command, failure):
    real_run = subprocess.run
    def fail(args, **kwargs):
        if args[0] != command:
            return real_run(args, **kwargs)
        if failure == "missing":
            raise FileNotFoundError("unavailable volume utility")
        if failure == "exit":
            raise subprocess.CalledProcessError(1, args)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 10)
        if failure == "malformed_xml":
            return subprocess.CompletedProcess(args, 0, stdout=b'<?xml version="1.0"?><plist><dict>')
        return subprocess.CompletedProcess(args, 0, stdout=b"unreadable volume inventory")
    monkeypatch.setattr(cow.subprocess, "run", fail)
    with pytest.raises(cow.CowRefusal, match="cow_volume_(unreadable|device_unverified)"):
        cow._volume(tmp_path.resolve())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="mounted-device resolver requires macOS")
def test_unreadable_volume_apply_has_zero_write_envelope(cow_fixture, monkeypatch):
    plan = preview(cow_fixture)
    real_run = subprocess.run
    def fail(args, **kwargs):
        if args[0] == "diskutil":
            raise subprocess.CalledProcessError(1, args)
        return real_run(args, **kwargs)
    monkeypatch.setattr(cow, "_volume", _REAL_VOLUME)
    monkeypatch.setattr(cow.subprocess, "run", fail)
    with pytest.raises(cleanup.StaleArtifactCleanupError) as caught:
        apply(cow_fixture, plan)
    result = caught.value.payload
    assert result["ok"] is False and result["refusal_reason"] == "cow_volume_unreadable"
    assert result["writes_performed"] is False and result["write_disposition"] == "not_written"
    assert not cow._state_root("proj").exists()
    assert list(cow_fixture[4].iterdir()) == []


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


def test_internal_selection_narrows_without_engine_hash_or_manual_plan_change(cow_fixture):
    conn, root, _, config, _ = cow_fixture
    manual = preview(cow_fixture)
    selection = cow.RunSelection(('full-old',), 1, 2, 1024 * 1024)
    internal = cow.preview(conn, 'proj', root, _run_selection=selection)
    assert internal['config_hash'] == manual['config_hash'] == cow._digest(config['governance']['snapshot_cow_cleanup'])
    assert internal['plan_hash'] != manual['plan_hash']
    assert preview(cow_fixture)['plan_hash'] == manual['plan_hash']
    with pytest.raises(cow.CowRefusal, match='selection_invalid'):
        cow.preview(conn, 'proj', root, _run_selection=cow.RunSelection(('full-old',), 1, 1, 1024))
    with pytest.raises(cow.CowRefusal, match='selection_invalid'):
        cow.preview(conn, 'proj', root, _run_selection=cow.RunSelection(('full-old',), 5, 2, 1024))


def test_digest_meter_exact_scope_all_proof_hashes_and_deadline(cow_fixture):
    _, _, base, *_ = cow_fixture
    path = base / cow.PAIRS[0][0]
    expected = cow._metadata(path)
    meter = cow.DigestMeter(expected['size'] * 2)
    with cow.digest_budget(meter):
        cow._hash(path, expected)
        cow._hash(path, expected)
        assert meter.consumed == expected['size'] * 2
        with pytest.raises(cow.CowRefusal, match='budget_exhausted'):
            cow._hash(path, expected)
        assert meter.consumed == meter.limit
    assert cow._DIGEST_METER.get() is None
    assert cow._hash(path, expected)
    with cow.digest_budget(cow.DigestMeter(1024 * 1024, deadline=0)):
        with pytest.raises(cow.CowRefusal, match='deadline_exhausted'):
            cow._hash(path, expected)


@pytest.mark.skipif(sys.platform != 'darwin', reason='isolated native APFS fixture')
def test_timer_policy_changes_preserve_legacy_receipt_recovery_but_engine_drift_refuses(cow_fixture, monkeypatch):
    conn, root, _, config, _ = cow_fixture
    monkeypatch.setattr(cow, '_quiet', lambda *_args: None)
    plan = preview(cow_fixture)
    assert apply(cow_fixture, plan)['ok']
    record = cow._read(cow._journal_path('proj', plan['operation_id']))
    assert 'run_selection' not in record
    config['governance']['snapshot_cow_cleanup_periodic'] = {'enabled': False, 'revision': 200}
    assert apply(cow_fixture, plan)['replay']
    assert recover(cow_fixture, plan, 'restore')['ok']
    config['governance']['snapshot_cow_cleanup']['max_pairs'] = 6
    with pytest.raises(cleanup.StaleArtifactCleanupError):
        apply(cow_fixture, plan)


def test_internal_plan_fresh_pin_and_engine_policy_drift_are_not_bypassed(cow_fixture, monkeypatch):
    conn, root, _, config, _ = cow_fixture
    selection = cow.RunSelection(('full-old',), 1, 2, 1024 * 1024)
    plan = cow.preview(conn, 'proj', root, _run_selection=selection)
    conn.execute("INSERT INTO graph_snapshot_refs(project_id,ref_name,snapshot_id,updated_at,commit_sha) "
                 "VALUES('proj','new-pin','full-old','now','fixture')")
    conn.commit()
    with pytest.raises(cleanup.StaleArtifactCleanupError):
        cleanup.apply_stale_artifact_cleanup(conn, 'proj', repo_root_path=root, dimension=cow.DIMENSION,
            candidate_ids=[r['candidate_id'] for r in plan['candidates']], plan_hash=plan['plan_hash'],
            plan_revision=plan['plan_revision'], operation_id=plan['operation_id'], _run_selection=selection)
    assert not cow._state_root('proj').exists()
    conn.execute("DELETE FROM graph_snapshot_refs WHERE ref_name='new-pin'")
    conn.commit()
    config['governance']['snapshot_cow_cleanup']['max_pairs'] = 7
    with pytest.raises(cow.CowRefusal, match='config_drift'):
        cow._fresh(conn, 'proj', root, plan, plan['candidates'][0])


def test_current_contract_completed_audits_do_not_spend_prejournal_census_budget(cow_fixture, monkeypatch):
    """Real apply shape: preview then selected-row _fresh, same native writer lock."""
    if sys.platform != 'darwin':
        pytest.skip('native apply preflight requires macOS')
    conn, root, base, *_ = cow_fixture
    records = [(f'phase-{n}', 'proj', 'backlog', 'contract', '1', '1', 1,
                json.dumps({'runtime_guide': {'next_legal_action': False if n<19 else None},
                            'history': {'note': f'live-audit-{n:03d} full-active' if n<19 else f'completed-audit-{n:03d} full-old',
                                        'nested': json.dumps({'values': [{'audit': 'large independent completed metadata ' * 128}] * 8})}}), 'now', 'now')
               for n in range(110)]
    conn.executemany('INSERT INTO contract_runtime_executions '
                     '(contract_execution_id,project_id,backlog_id,contract_id,version,revision,'
                     'execution_state_revision,record_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)', records)
    conn.commit()
    approved = preview(cow_fixture)
    row = approved['candidates'][0]
    files_before = {p: p.read_bytes() for p in base.rglob('*.json')}
    database = Path(conn.execute('PRAGMA database_list').fetchone()[2])
    clock = [0.0]
    completed_deep_searches = [0]
    builtin = sqlite3.connect(':memory:')

    def factory():
        reader = sqlite3.connect(database)
        reader.row_factory = sqlite3.Row
        def instr(haystack, needle):
            # Charge only the real SQL completed-audit decoded-pin search.
            # State validation, live projection and all unrelated SQL stay real.
            if (needle == 'full-old' and isinstance(haystack, str)
                    and '\x00' in haystack and 'completed-audit-' in haystack):
                completed_deep_searches[0] += 1
                clock[0] += 13.0 / 91
            return builtin.execute('SELECT instr(?,?)', (haystack, needle)).fetchone()[0]
        reader.create_function('instr', 2, instr)
        return reader

    phases = []
    original_rows = snapshots._contract_reference_rows
    def contract_rows(*args, **kwargs):
        phases.append((args[0].in_transaction, snapshots._reference_budget(args[0]).deadline))
        yield from original_rows(*args, **kwargs)
    monkeypatch.setattr(snapshots, '_contract_reference_rows', contract_rows)
    monkeypatch.setattr(snapshots.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(snapshots._REFERENCE_LOG, 'isEnabledFor', lambda *_: False)
    try:
        with snapshots._owned_reference_connection(factory) as reader:
            budget = snapshots._reference_budget(reader)
            result = cleanup._cow_locked(reader, 'proj', root, 'apply',
                    candidate_ids=[row['candidate_id']], plan_hash=approved['plan_hash'],
                    plan_revision=approved['plan_revision'], operation_id=approved['operation_id'])
            assert result['ok'] and result['writes_performed']
            assert len(phases)==5 and all(phase==(True,25.0) for phase in phases)
            assert completed_deep_searches == [0] and budget.deadline == 25.0
            assert not reader.in_transaction and not budget.active and not budget.exhausted
            assert cow._state_root('proj').exists()
        assert {p: p.read_bytes() for p in files_before} == files_before
        assert len(approved['candidates'])==2  # Both original equal graph/index pairs, one selected.
    finally:
        builtin.close()


@pytest.mark.parametrize('body', [
    {'runtime_guide': {'next_legal_action': None}, 'snapshot_id': 'full-old'},
    {'runtime_guide': {'next_legal_action': False}, 'snapshot_id': 'full-old'},
    {'runtime_guide': {'next_legal_action': 0}, 'serialized': json.dumps({'snapshot_id': 'full-old'})},
    {}, {'runtime_guide': {}}, '{broken', sqlite3.Binary(b'{}'), None,
    '{"runtime_guide":{"next_legal_action":false},"runtime_guide":{"next_legal_action":null},"snapshot_id":"full-old"}',
    '{"runtime_guide":{"next_legal_action":null},"runtime_guide":{"next_legal_action":false},"snapshot_id":"full-old"}',
    '{"runtime_guide":{"next_legal_action":false,"next_legal_action":null},"snapshot_id":"full-old"}',
    '{"runtime_guide":{"next_legal_action":null,"next_legal_action":false},"snapshot_id":"full-old"}',
])
def test_current_contract_projection_only_skips_exact_completed_state(cow_fixture, body):
    conn = cow_fixture[0]
    payload = json.dumps(body) if isinstance(body, dict) else body
    if payload is None:
        conn.execute('DROP TABLE contract_runtime_executions')
        conn.execute('CREATE TABLE contract_runtime_executions(contract_execution_id TEXT, '
                     'project_id TEXT,backlog_id TEXT,record_json TEXT)')
    _insert_census_contract(conn, payload) if payload is not None else conn.execute(
        "INSERT INTO contract_runtime_executions VALUES('null','proj','backlog',NULL)")
    durable = list(snapshots._contract_reference_rows(conn, 'proj', {'full-old'}))
    current = list(snapshots._contract_reference_rows(conn, 'proj', {'full-old'}, current_only=True))
    assert len(current) == len(durable) == 1
    assert current[0]['state'] == durable[0]['state']
    assert current[0]['metadata'] == durable[0]['metadata']
    if durable[0]['state'] == 'completed':
        assert durable[0]['pins'] == ['full-old']  # Historical durable protection remains.
        assert current[0]['pins'] == [] and current[0]['complete']
    else:
        assert current == durable


@pytest.mark.parametrize('change', ['live_data', 'schema', 'token', 'unknown_store', 'external_manifest', 'current_fence'])
def test_current_contract_rechecks_late_protections_without_memo(cow_fixture, monkeypatch, change):
    conn, root, _, config, _ = cow_fixture
    _insert_census_contract(conn, json.dumps({'runtime_guide': {'next_legal_action': None},
                                            'snapshot_id': 'full-old'}))
    manifest = root / 'current-manifest.json'
    if change == 'external_manifest':
        manifest.write_text(json.dumps({'snapshot_id': 'full-active'}))
        config['governance']['snapshot_cow_cleanup']['bundle_manifests'] = [str(manifest)]
    approved = preview(cow_fixture)
    row = approved['candidates'][0]
    cow._fresh(conn, 'proj', root, approved, row)
    if change == 'live_data':
        conn.execute("UPDATE contract_runtime_executions SET record_json=?",
                     (json.dumps({'runtime_guide': {'next_legal_action': False}, 'late_pin': 'full-old'}),))
        conn.commit()  # End the earlier view; the next census must freshly read.
    elif change == 'schema':
        conn.execute('ALTER TABLE contract_runtime_executions ADD COLUMN unowned TEXT')
    elif change == 'token':
        conn.execute("INSERT INTO graph_snapshots(project_id,snapshot_id,snapshot_kind,status,created_at,commit_sha) VALUES('proj',?,'full','superseded','2019','fixture')",('x'*1025,))
    elif change == 'unknown_store':
        conn.execute('CREATE TABLE late_qa_session(snapshot_id TEXT,status TEXT)')
        conn.execute("INSERT INTO late_qa_session VALUES('full-old','completed')")
    elif change == 'external_manifest':
        manifest.write_text(json.dumps({'snapshot_id': 'full-old'}))
    else:
        monkeypatch.setattr(server, '_graph_release_build_fence_state', lambda *_: {'clear': False})
    with pytest.raises(cow.CowRefusal) as caught:
        cow._fresh(conn, 'proj', root, approved, row)
    if change == 'schema':
        assert str(caught.value)=='cow_reference_census_refused'
        assert caught.value.metadata['cause']=='contract_runtime_executions_owner_schema_unknown'
    elif change == 'token':
        assert str(caught.value)=='cow_reference_census_refused'
        assert caught.value.metadata['cause']=='reference_identity_inventory_unbounded'
    elif change == 'current_fence':
        assert str(caught.value)=='cow_current_full_writer_active'
    else:
        assert str(caught.value)=='cow_snapshot_live_or_retained'
    assert not cow._state_root('proj').exists()


@pytest.mark.parametrize('phase', ['prejournal', 'before_replace'])
def test_current_contract_shortcut_retains_absolute_budget_in_later_apply_phases(cow_fixture, monkeypatch, phase):
    if sys.platform != 'darwin':
        pytest.skip('native apply preflight requires macOS')
    conn, root, base, *_ = cow_fixture
    _insert_census_contract(conn, json.dumps({'runtime_guide': {'next_legal_action': None},
                                            'snapshot_id': 'full-old'}))
    approved=preview(cow_fixture);row=approved['candidates'][0]
    database=Path(conn.execute('PRAGMA database_list').fetchone()[2])
    before={p:p.read_bytes() for p in base.rglob('*.json')}
    clock=[0.0];fresh_calls=[0];fresh=cow._fresh
    def timed_fresh(*args, **kwargs):
        fresh_calls[0]+=1
        if fresh_calls[0]==(1 if phase=='prejournal' else 4):
            clock[0]=26.0
        return fresh(*args,**kwargs)
    monkeypatch.setattr(cow,'_fresh',timed_fresh)
    monkeypatch.setattr(snapshots.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(snapshots._REFERENCE_LOG,'isEnabledFor',lambda *_:False)
    def factory():
        reader=sqlite3.connect(database);reader.row_factory=sqlite3.Row;return reader
    with snapshots._owned_reference_connection(factory) as reader:
        budget=snapshots._reference_budget(reader)
        arguments=dict(candidate_ids=[row['candidate_id']],plan_hash=approved['plan_hash'],
                       plan_revision=approved['plan_revision'],operation_id=approved['operation_id'])
        if phase=='prejournal':
            with pytest.raises(cleanup.StaleArtifactCleanupError) as caught:
                cleanup._cow_locked(reader,'proj',root,'apply',**arguments)
            assert caught.value.payload['native_prejournal_refusal']
            assert not cow._state_root('proj').exists()
        else:
            result=cleanup._cow_locked(reader,'proj',root,'apply',**arguments)
            assert not result['ok'] and result['applied_count']==0
            assert result['error']=='CowRefusal:cow_reference_census_refused'
        assert budget.deadline==25.0 and budget.exhausted and not budget.active
        assert not reader.in_transaction
    assert {p:p.read_bytes() for p in before}==before
