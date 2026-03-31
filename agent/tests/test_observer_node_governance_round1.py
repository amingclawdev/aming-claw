import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import networkx  # noqa: F401
    _has_networkx = True
except ImportError:
    _has_networkx = False

from governance import project_service, role_service
from governance.db import DBContext, close_connection, get_connection
from governance.errors import PermissionDeniedError, ValidationError
from governance.redis_client import reset_redis
from governance.server import (
    RequestContext,
    handle_import_graph,
    handle_observer_sync_node_state,
    handle_summary,
    handle_node_update,
)
from role_permissions import check_permission, check_verify_permission


@unittest.skipUnless(_has_networkx, "networkx not installed")
class TestObserverNodeGovernanceRound1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        reset_redis()

        self.project_id = "observer-node-test"
        project_service.init_project(self.project_id)
        self.conn = get_connection(self.project_id)

        self.coord = role_service.register(self.conn, "coord-001", self.project_id, "coordinator")
        self.observer = role_service.register(self.conn, "observer-001", self.project_id, "observer")
        self.conn.commit()

        self.md_path = os.path.join(self.tmp.name, "graph.md")
        with open(self.md_path, "w", encoding="utf-8") as f:
            f.write(
                """# Test Graph

## L0

```
L0.1  Test Node  [impl:done] [verify:pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[agent/foo.py]
      secondary:[docs/foo.md]
      test:[agent/tests/test_foo.py]
```
"""
            )

    def tearDown(self):
        close_connection(self.conn)
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def _ctx(self, token: str, body: dict):
        return RequestContext(
            handler=None,
            method="POST",
            path_params={"project_id": self.project_id},
            query={},
            body=body,
            request_id="req-test",
            token=token,
            idem_key="",
        )

    def test_gatekeeper_permission_matrix_exists(self):
        allowed, reason = check_permission("gatekeeper", "query_governance")
        self.assertTrue(allowed, reason)

        denied, reason = check_permission("gatekeeper", "verify_update")
        self.assertFalse(denied)
        self.assertIn("cannot perform", reason)

        verify_allowed, reason = check_verify_permission("gatekeeper", "qa_pass")
        self.assertFalse(verify_allowed)

    def test_observer_import_requires_reason(self):
        with self.assertRaises(ValidationError):
            handle_import_graph(self._ctx(self.observer["token"], {"md_path": self.md_path}))

    def test_observer_import_and_sync_restore_node_state(self):
        result = handle_import_graph(
            self._ctx(
                self.observer["token"],
                {"md_path": self.md_path, "reason": "Recover graph after runtime state corruption"},
            )
        )
        self.assertEqual(result["node_states_initialized"], 1)

        with DBContext(self.project_id) as conn:
            conn.execute("DELETE FROM node_state WHERE project_id = ?", (self.project_id,))

        summary_before = handle_summary(
            RequestContext(None, "GET", {"project_id": self.project_id}, {}, {}, "req-summary", self.observer["token"], "")
        )
        self.assertEqual(summary_before["total_nodes"], 0)

        sync = handle_observer_sync_node_state(
            self._ctx(
                self.observer["token"],
                {"reason": "Rebuild node_state from graph.json after corruption"},
            )
        )
        self.assertEqual(sync["graph_nodes"], 1)
        self.assertEqual(sync["node_state_total"], 1)

        summary_after = handle_summary(
            RequestContext(None, "GET", {"project_id": self.project_id}, {}, {}, "req-summary-2", self.observer["token"], "")
        )
        self.assertEqual(summary_after["total_nodes"], 1)

    def test_observer_cannot_use_node_update(self):
        handle_import_graph(
            self._ctx(
                self.coord["token"],
                {"md_path": self.md_path},
            )
        )
        with self.assertRaises(PermissionDeniedError):
            handle_node_update(
                self._ctx(
                    self.observer["token"],
                    {"node_id": "L0.1", "attrs": {"description": "observer should not patch attrs directly"}},
                )
            )


if __name__ == "__main__":
    unittest.main()
