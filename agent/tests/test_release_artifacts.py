from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "scripts" / "e2e-happy-path-smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("e2e_happy_path_smoke", SMOKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v2_release_versions_are_synchronized():
    smoke = _load_smoke_module()
    preflight = smoke.offline_preflight(ROOT)
    assert preflight["status"] == "passed"
    assert all(preflight["checks"].values())
    assert preflight["checks"]["managed_profile_plugin_version"] is True
    assert preflight["checks"]["compose_isolated_volume"] is True
    assert preflight["checks"]["docker_context_excludes_local_state"] is True


def test_happy_path_smoke_declares_two_zero_bypass_reference_worlds():
    smoke = _load_smoke_module()
    assert [world.lane for world in smoke.REFERENCE_WORLDS] == [
        "mf_parallel",
        "mf_batch_parallel",
    ]
    assert all(len(world.close_commit) == 40 for world in smoke.REFERENCE_WORLDS)
    assert len(smoke.REFERENCE_WORLDS[0].backlog_ids) == 1
    assert len(smoke.REFERENCE_WORLDS[1].backlog_ids) == 3


def test_no_pass_detector_uses_formal_fields_not_route_guidance_text():
    smoke = _load_smoke_module()
    harmless = {
        "id": 1,
        "event_type": "route_token_gate.task_timeline_append",
        "event_kind": "route_token_gate",
        "status": "accepted",
        "payload": {"allowed_actions": ["contract_runtime_bypass_line"]},
    }
    bypass = {
        "id": 2,
        "event_type": "contract_runtime_bypass_line",
        "event_kind": "contract_line_bypass",
        "status": "waived",
        "payload": {"no_pass_claim": True},
    }
    assert smoke._formal_no_pass_events([harmless]) == []
    assert smoke._formal_no_pass_events([harmless, bypass]) == [
        {
            "id": 2,
            "event_type": "contract_runtime_bypass_line",
            "event_kind": "contract_line_bypass",
            "status": "waived",
        }
    ]


def test_smoke_preflight_is_offline_and_machine_readable():
    completed = subprocess.run(
        [sys.executable, str(SMOKE), "--preflight", "--repo-root", str(ROOT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["passed"] is True
    assert payload["reference_worlds"] == []
    assert payload["raw_credentials_required"] is False
    assert payload["writes_performed_by_http_replay"] is False


def test_release_docs_name_replay_and_chain_trailer_boundaries():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "docs" / "dev" / "happy-path-runbook.md").read_text(
        encoding="utf-8"
    )
    assert "Aming Claw v2" in readme
    assert "e2e-happy-path-smoke.py" in readme
    assert "daily-planner-parallel-2184372f-20260806t203120z" in readme
    assert "daily-planner-batch-45824720-20260807t032549z" in readme
    assert "Chain-Source-Stage:" in runbook
    assert "event_kind=independent_verification" in runbook
    assert "runtime_loaded_version" in runbook
    assert "WIP=1" in runbook
