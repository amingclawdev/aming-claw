"""Bounded native COW fixtures; real clone syscalls use only kilobytes."""
from __future__ import annotations

import json
import ctypes
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

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
