"""Tests for backlog insert AI triage gate."""
import os, sys, types, importlib, importlib.abc, re as _re
_agent_dir = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _agent_dir)

class _Py39Fix(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    _GOV = os.path.join(_agent_dir, "governance")
    _PAT = _re.compile(r"->\s*\w+\s*\|")
    _paths = {}
    def find_module(self, name, path=None):
        if name.startswith("governance.") and name not in sys.modules:
            fp = os.path.join(self._GOV, name.split(".")[-1] + ".py")
            if os.path.isfile(fp):
                with open(fp) as f: c = f.read()
                if "from __future__ import annotations" not in c and self._PAT.search(c):
                    self._paths[name] = fp; return self
    def load_module(self, name):
        if name in sys.modules: return sys.modules[name]
        with open(self._paths[name]) as f: src = "from __future__ import annotations\n" + f.read()
        m = types.ModuleType(name); m.__file__ = self._paths[name]; m.__package__ = "governance"; m.__path__ = []
        sys.modules[name] = m; exec(compile(src, self._paths[name], "exec"), m.__dict__); return m
if sys.version_info < (3, 10): sys.meta_path.insert(0, _Py39Fix())

from unittest.mock import MagicMock, patch
import pytest

def _ctx(bug_id="NEW-1", pid="test-proj", **b):
    c = MagicMock(); c.path_params = {"project_id": pid, "bug_id": bug_id}; c.body = b; return c

def _conn(rows):
    c = MagicMock()
    def _ex(sql, params=None):
        r = MagicMock()
        if "SELECT" in str(sql) and "status='OPEN'" in str(sql): r.fetchall.return_value = rows
        else: r.fetchone.return_value = None; r.fetchall.return_value = []
        return r
    c.execute.side_effect = _ex; return c

@pytest.fixture(autouse=True)
def _aud():
    with patch("governance.server.audit_service"): yield

def test_admit_when_no_open_rows():
    with patch("governance.server.get_connection", return_value=_conn([])):
        from governance.server import handle_backlog_upsert
        assert handle_backlog_upsert(_ctx(title="New bug"))["ok"] is True

def test_admit_when_no_overlap():
    with patch("governance.server.get_connection", return_value=_conn([{"bug_id": "X", "title": "Other", "target_files": '["z.py"]'}])):
        from governance.server import handle_backlog_upsert
        assert handle_backlog_upsert(_ctx(title="Different"))["ok"] is True

def test_supersede_closes_old_row():
    with patch("governance.server.get_connection", return_value=_conn([{"bug_id": "OLD-2", "title": "Diff", "target_files": '["a.py"]'}])):
        from governance.server import handle_backlog_upsert
        r = handle_backlog_upsert(_ctx(title="New", target_files=["a.py"]))
        assert r["action"] == "superseded" and "OLD-2" in r["closed_bugs"]

def test_reject_dup_returns_409():
    with patch("governance.server.get_connection", return_value=_conn([{"bug_id": "OLD-1", "title": "Dup Bug", "target_files": "[]"}])):
        from governance.server import handle_backlog_upsert
        r = handle_backlog_upsert(_ctx(title="Dup Bug"))
        assert isinstance(r, tuple) and r[0] == 409 and "duplicate_of" in r[1]

def test_merge_into_appends_details():
    with patch("governance.server.get_connection", return_value=_conn([{"bug_id": "OLD-3", "title": "O", "target_files": '["a.py","b.py","c.py"]'}])):
        from governance.server import handle_backlog_upsert
        r = handle_backlog_upsert(_ctx(title="X", target_files=["a.py", "b.py"], details_md="e"))
        assert r["action"] == "merge_into" and r["bug_id"] == "OLD-3"

def test_force_admit_bypasses_gate():
    with patch("governance.server.get_connection", return_value=_conn([{"bug_id": "OLD-1", "title": "Dup Bug", "target_files": "[]"}])):
        from governance.server import handle_backlog_upsert
        r = handle_backlog_upsert(_ctx(title="Dup Bug", force_admit=True))
        assert r["ok"] is True and r["action"] == "upserted"

def test_agent_failure_falls_back_to_admit():
    c = MagicMock()
    def _ex(sql, params=None):
        if "SELECT" in str(sql) and "status='OPEN'" in str(sql): raise RuntimeError("boom")
        r = MagicMock(); r.fetchone.return_value = None; return r
    c.execute.side_effect = _ex
    with patch("governance.server.get_connection", return_value=c):
        from governance.server import handle_backlog_upsert
        assert handle_backlog_upsert(_ctx(title="Bug"))["ok"] is True
