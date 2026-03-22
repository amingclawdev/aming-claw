"""Tests for governance init + role assignment flow."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from governance.redis_client import reset_redis
from governance.db import get_connection, close_connection
from governance.errors import AuthError, PermissionDeniedError


class TestInitProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        reset_redis()

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_init_returns_coordinator_token(self):
        from governance.project_service import init_project
        result = init_project("test-proj", "mypassword123", "Test Project")
        self.assertIn("coordinator", result)
        self.assertTrue(result["coordinator"]["token"].startswith("gov-"))
        self.assertIn("session_id", result["coordinator"])
        self.assertEqual(result["project"]["project_id"], "test-proj")

    def test_init_same_project_without_password_rejected(self):
        from governance.project_service import init_project
        init_project("test-proj", "mypassword123")
        with self.assertRaises(AuthError) as ctx:
            init_project("test-proj", "wrongpassword")
        self.assertIn("already initialized", ctx.exception.message)

    def test_init_same_project_with_correct_password_resets(self):
        from governance.project_service import init_project
        r1 = init_project("test-proj", "mypassword123")
        r2 = init_project("test-proj", "mypassword123")
        # New token issued
        self.assertIn("coordinator", r2)
        self.assertTrue(r2["coordinator"]["token"].startswith("gov-"))
        self.assertIn("reset", r2.get("message", "").lower())

    def test_init_short_password_rejected(self):
        from governance.project_service import init_project
        from governance.errors import ValidationError
        with self.assertRaises(ValidationError):
            init_project("test-proj", "abc")

    def test_init_invalid_project_id_rejected(self):
        from governance.project_service import init_project
        from governance.errors import ValidationError
        with self.assertRaises(ValidationError):
            init_project("bad project!", "password123")

    def test_token_not_saved_to_disk(self):
        from governance.project_service import init_project
        from governance.db import _governance_root
        init_project("no-persist", "password123")
        project_dir = _governance_root() / "no-persist"
        credential_files = list(project_dir.glob("session_*.json"))
        self.assertEqual(len(credential_files), 0)

    def test_password_hash_not_in_list(self):
        from governance.project_service import init_project, list_projects
        init_project("test-proj", "mypassword123")
        projects = list_projects()
        for p in projects:
            self.assertNotIn("password_hash", p)


class TestRoleAssignment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        reset_redis()

        from governance.project_service import init_project
        result = init_project("test-proj", "password123")
        self.coord_token = result["coordinator"]["token"]
        self.project_id = "test-proj"

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def _coord_session(self):
        from governance import role_service
        conn = get_connection(self.project_id)
        session = role_service.authenticate(conn, self.coord_token)
        return conn, session

    def test_coordinator_assigns_tester(self):
        from governance.project_service import assign_role
        conn, session = self._coord_session()
        try:
            result = assign_role(conn, self.project_id, session,
                                 "tester-001", "tester", scope=["L0.*", "L1.*"])
            conn.commit()
        finally:
            close_connection(conn)
        self.assertEqual(result["role"], "tester")
        self.assertTrue(result["token"].startswith("gov-"))
        self.assertEqual(result["principal_id"], "tester-001")

    def test_coordinator_assigns_dev(self):
        from governance.project_service import assign_role
        conn, session = self._coord_session()
        try:
            result = assign_role(conn, self.project_id, session,
                                 "dev-001", "dev")
            conn.commit()
        finally:
            close_connection(conn)
        self.assertEqual(result["role"], "dev")

    def test_non_coordinator_cannot_assign(self):
        from governance.project_service import assign_role
        from governance import role_service
        # First assign a tester
        conn, coord_session = self._coord_session()
        tester_result = assign_role(conn, self.project_id, coord_session,
                                    "tester-001", "tester")
        conn.commit()
        close_connection(conn)

        # Now try to assign as tester
        conn = get_connection(self.project_id)
        tester_session = role_service.authenticate(conn, tester_result["token"])
        with self.assertRaises(PermissionDeniedError):
            assign_role(conn, self.project_id, tester_session,
                        "dev-001", "dev")
        close_connection(conn)

    def test_cannot_assign_coordinator_role(self):
        from governance.project_service import assign_role
        conn, session = self._coord_session()
        with self.assertRaises(PermissionDeniedError):
            assign_role(conn, self.project_id, session,
                        "coord-002", "coordinator")
        close_connection(conn)

    def test_coordinator_revokes_session(self):
        from governance.project_service import assign_role, revoke_role
        from governance import role_service
        conn, session = self._coord_session()
        tester = assign_role(conn, self.project_id, session, "tester-001", "tester")
        conn.commit()
        close_connection(conn)

        conn, session = self._coord_session()
        revoke_role(conn, self.project_id, session, tester["session_id"])
        conn.commit()

        # Tester token should no longer work
        from governance.errors import TokenExpiredError, TokenInvalidError
        with self.assertRaises((TokenExpiredError, TokenInvalidError)):
            role_service.authenticate(conn, tester["token"])
        close_connection(conn)

    def test_assigned_token_works_for_auth(self):
        from governance.project_service import assign_role
        from governance import role_service
        conn, session = self._coord_session()
        tester = assign_role(conn, self.project_id, session, "tester-001", "tester")
        conn.commit()
        close_connection(conn)

        # Verify tester can authenticate
        conn = get_connection(self.project_id)
        tester_session = role_service.authenticate(conn, tester["token"])
        self.assertEqual(tester_session["role"], "tester")
        self.assertEqual(tester_session["principal_id"], "tester-001")
        close_connection(conn)


if __name__ == "__main__":
    unittest.main()
