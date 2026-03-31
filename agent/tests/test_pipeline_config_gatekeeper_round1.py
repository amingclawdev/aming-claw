import unittest


class TestPipelineConfigGatekeeperRound1(unittest.TestCase):
    def test_validate_pipeline_config_accepts_gatekeeper_role(self):
        from pipeline_config import validate_pipeline_config

        config = {
            "default": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            "roles": {
                "gatekeeper": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            },
        }

        self.assertEqual(validate_pipeline_config(config), [])

    def test_resolve_role_config_reads_gatekeeper_override(self):
        from pipeline_config import resolve_role_config

        config = {
            "default": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            "roles": {
                "gatekeeper": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            },
        }

        resolved = resolve_role_config("gatekeeper", config)
        self.assertEqual(resolved["provider"], "anthropic")
        self.assertEqual(resolved["model"], "claude-sonnet-4-6")


if __name__ == "__main__":
    unittest.main()
