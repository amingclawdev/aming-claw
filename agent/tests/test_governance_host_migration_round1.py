import os
import sys
import unittest
from pathlib import Path
from unittest import mock

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))


class TestGovernanceHostMigrationRound1(unittest.TestCase):
    def test_configure_host_env_sets_host_defaults(self):
        import start_governance

        env = {}
        with mock.patch.dict(os.environ, env, clear=True):
            configured = start_governance.configure_host_env(repo_root)

        self.assertEqual(configured["GOVERNANCE_PORT"], "40000")
        self.assertEqual(configured["DBSERVICE_URL"], "http://localhost:40002")
        self.assertEqual(configured["REDIS_URL"], "redis://localhost:40079/0")
        self.assertEqual(configured["MEMORY_BACKEND"], "docker")
        self.assertTrue(configured["SHARED_VOLUME_PATH"].endswith("shared-volume"))

    def test_configure_host_env_preserves_existing_values(self):
        import start_governance

        with mock.patch.dict(os.environ, {
            "GOVERNANCE_PORT": "45555",
            "DBSERVICE_URL": "http://custom:1234",
        }, clear=True):
            configured = start_governance.configure_host_env(repo_root)

        self.assertEqual(configured["GOVERNANCE_PORT"], "45555")
        self.assertEqual(configured["DBSERVICE_URL"], "http://custom:1234")


if __name__ == "__main__":
    unittest.main()
