"""Tests that handle_version_check uses trailer-priority chain_ver.

When trailer_state['source'] == 'trailer', chain_ver MUST come from
trailer_state (version/chain_sha), not the project_version DB row.
runtime_match must compare gov/sm runtime against the same trailer-priority
chain_ver. When trailer is absent or source != 'trailer', the DB row is the
source of truth (regression guard for prior behavior).
"""

import json
import sqlite3
import sys
from unittest import mock

# Pre-mock governance submodules using Python 3.10+ syntax for 3.9 compat
for _m in ("evidence", "state_service", "gate_policy", "project_service",
           "memory_service", "idempotency", "impact_analyzer", "role_service",
           "session_context", "session_persistence", "doc_generator",
           "gatekeeper", "task_registry", "failure_classifier", "reconcile",
           "chain_context", "graph"):
    sys.modules.setdefault(f"agent.governance.{_m}", mock.MagicMock())

from agent.governance.server import handle_version_check  # noqa: E402


def _make_mock_conn(chain_ver="dbsha00", git_head="dbsha00"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE project_version (
            project_id TEXT PRIMARY KEY,
            chain_version TEXT,
            updated_at TEXT,
            git_head TEXT,
            dirty_files TEXT,
            git_synced_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO project_version VALUES (?, ?, ?, ?, ?, ?)",
        ("test-proj", chain_ver, "2026-01-01T00:00:00Z", git_head, "[]", "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    return conn


class _Ctx:
    body = {}
    query = {}
    def __init__(self, pid="test-proj"):
        self._pid = pid
    def get_project_id(self):
        return self._pid


def _fake_urlopen(sm_runtime=""):
    fake_resp = mock.MagicMock()
    fake_resp.read.return_value = json.dumps({"runtime_version": sm_runtime}).encode()
    fake_resp.__enter__ = lambda s: s
    fake_resp.__exit__ = mock.Mock(return_value=False)
    return fake_resp


def test_trailer_source_overrides_db_chain_version():
    """When source='trailer', returned chain_version MUST equal trailer version, not DB row."""
    trailer = {"source": "trailer", "version": "trail77", "chain_sha": "trail77",
               "dirty": False, "dirty_files": []}
    with mock.patch("agent.governance.server.get_connection", return_value=_make_mock_conn(chain_ver="dbsha00", git_head="trail77")), \
         mock.patch("agent.governance.server._utc_now", return_value="2026-01-01T00:00:00Z"), \
         mock.patch("agent.governance.chain_trailer.get_chain_state", return_value=trailer), \
         mock.patch("agent.governance.chain_trailer.get_runtime_version", return_value=""), \
         mock.patch("urllib.request.urlopen", return_value=_fake_urlopen("")):
        result = handle_version_check(_Ctx())
    assert result["chain_version"] == "trail77"
    assert result["project_version"] == "trail77"
    assert result["source"] == "trailer"


def test_trailer_source_runtime_match_uses_trailer_chain():
    """runtime_match=True when gov/sm runtime equal trailer chain even though DB row differs."""
    trailer = {"source": "trailer", "version": "trail77", "chain_sha": "trail77",
               "dirty": False, "dirty_files": []}
    with mock.patch("agent.governance.server.get_connection", return_value=_make_mock_conn(chain_ver="dbsha00", git_head="trail77")), \
         mock.patch("agent.governance.server._utc_now", return_value="2026-01-01T00:00:00Z"), \
         mock.patch("agent.governance.chain_trailer.get_chain_state", return_value=trailer), \
         mock.patch("agent.governance.chain_trailer.get_runtime_version", return_value="trail77"), \
         mock.patch("urllib.request.urlopen", return_value=_fake_urlopen("trail77")):
        result = handle_version_check(_Ctx())
    assert result["runtime_match"] is True
    assert result["gov_runtime_version"] == "trail77"
    assert result["sm_runtime_version"] == "trail77"


def test_no_trailer_falls_back_to_db_chain_version():
    """Trailer absent -> chain_ver comes from DB row (regression guard)."""
    with mock.patch("agent.governance.server.get_connection", return_value=_make_mock_conn(chain_ver="dbsha00", git_head="dbsha00")), \
         mock.patch("agent.governance.server._utc_now", return_value="2026-01-01T00:00:00Z"), \
         mock.patch("agent.governance.chain_trailer.get_chain_state", side_effect=Exception("no trailer")), \
         mock.patch("agent.governance.chain_trailer.get_runtime_version", return_value=""), \
         mock.patch("urllib.request.urlopen", return_value=_fake_urlopen("")):
        result = handle_version_check(_Ctx())
    assert result["chain_version"] == "dbsha00"
    assert result["project_version"] == "dbsha00"
    assert result["source"] == "db"


def test_trailer_source_head_falls_back_to_db_chain_version():
    """source='head' (no Chain-Source-Stage trailer found) -> chain_ver from DB row."""
    trailer = {"source": "head", "version": "headsha", "chain_sha": "headsha",
               "dirty": False, "dirty_files": []}
    with mock.patch("agent.governance.server.get_connection", return_value=_make_mock_conn(chain_ver="dbsha00", git_head="dbsha00")), \
         mock.patch("agent.governance.server._utc_now", return_value="2026-01-01T00:00:00Z"), \
         mock.patch("agent.governance.chain_trailer.get_chain_state", return_value=trailer), \
         mock.patch("agent.governance.chain_trailer.get_runtime_version", return_value=""), \
         mock.patch("urllib.request.urlopen", return_value=_fake_urlopen("")):
        result = handle_version_check(_Ctx())
    assert result["chain_version"] == "dbsha00"
    assert result["source"] == "head"
