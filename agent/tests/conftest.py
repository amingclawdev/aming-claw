"""Shared pytest configuration, markers, and fixtures."""
import sqlite3
import sys
import os

import pytest

# Ensure agent package is importable
_agent_dir = os.path.join(os.path.dirname(__file__), "..")
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: end-to-end tests requiring live governance container + executor")


# ---------------------------------------------------------------------------
# Legacy audit_log DDL — not in governance SCHEMA_SQL but used by auto_chain
# ---------------------------------------------------------------------------
_AUDIT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT, action TEXT, actor TEXT, ok INTEGER,
    ts TEXT, task_id TEXT, details_json TEXT
)
"""


@pytest.fixture()
def isolated_gov_db(monkeypatch):
    """Function-scoped in-memory SQLite DB with full governance schema.

    * Creates ``sqlite3.connect(':memory:')``.
    * Initialises schema via ``governance.db._ensure_schema`` (all production
      tables: project_version, audit_index, node_state, tasks, task_attempts,
      gate_events, chain_events, …).
    * Adds the legacy ``audit_log`` table used by ``auto_chain.py`` helpers.
    * Monkeypatches ``governance.db.get_connection`` so any code that calls
      ``get_connection(project_id)`` during the test receives this connection.
    * Yields the connection, then closes it on teardown.
    """
    from governance.db import _ensure_schema, _configure_connection

    conn = sqlite3.connect(":memory:")
    _configure_connection(conn, busy_timeout=0)
    _ensure_schema(conn)

    # Legacy audit_log table (auto_chain writes to it directly)
    conn.executescript(_AUDIT_LOG_DDL)
    conn.commit()

    # Monkeypatch get_connection to return the in-memory connection
    monkeypatch.setattr("governance.db.get_connection", lambda *_args, **_kw: conn)

    yield conn

    conn.close()
