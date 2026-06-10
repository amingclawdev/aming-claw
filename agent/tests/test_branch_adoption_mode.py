"""Existing-worktree/branch adoption mode for close lineage.

Covers AC-CLOSE-LINEAGE-EXISTING-WORKTREE-ADOPTION-MODE-20260610: adoption
acceptance happy paths, false-startup rejection, unchanged fresh-dispatch
semantics, and the close route_context_gate adoption equivalence.
"""

from __future__ import annotations

from pathlib import Path
import sys


_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from agent.governance import task_timeline
from agent.governance.mf_workflow_runtime import load_workflow_contract
from agent.governance.precheck_service import run_precheck
from agent.tests.fixtures.mf_workflow_runtime import (
    CONTRACT_ID,
    advance_target_head,
    commit_worker_candidate,
    create_runtime_fixture,
)


ROUTE_IDENTITY = {
    "route_context_hash": "sha256:test-adoption-route-context",
    "prompt_contract_id": "rprompt-adoption-test",
    "prompt_contract_hash": "sha256:test-adoption-prompt-contract",
}

CLOSE_GATE_CONTRACT = {
    "template_id": "mf_parallel.v1",
    "contract_instance_id": "BUG-ADOPTION-CLOSE-GATE",
}


def _adoption_evidence(fixture, head_commit: str, **overrides: object) -> dict[str, object]:
    evidence: dict[str, object] = {
        "schema_version": "branch_adoption_evidence.v1",
        "status": "passed",
        "adopted_branch_ref": fixture.branch,
        "adopted_base_commit": fixture.base_commit,
        "adopted_head_commit": head_commit,
        "attestation_source": f"git -C {fixture.worker_worktree} rev-parse HEAD",
    }
    evidence.update(overrides)
    return evidence


# --- startup gate: adoption acceptance -------------------------------------


def test_startup_adoption_happy_path_accepts_adopted_head(tmp_path: Path) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    adopted_head = commit_worker_candidate(fixture)
    subject = fixture.startup_subject(contract)
    subject["branch_adoption_mode"] = "existing_branch"
    subject["branch_adoption_evidence"] = _adoption_evidence(fixture, adopted_head)

    result = run_precheck(
        "mf_subagent.startup",
        CONTRACT_ID,
        "startup_gate",
        subject,
        "pytest",
    )

    assert result["decision"] == "allow"
    adoption = result["evidence"]["branch_adoption"]
    assert adoption["accepted"] is True
    assert adoption["enforced"] is True
    assert adoption["adopted_head_commit"] == adopted_head
    assert adoption["head_matches_adopted_head"] is True
    assert adoption["ancestry_check"] == "git_merge_base_is_ancestor"
    assert adoption["ancestry_verified"] is True
    assert "worker_head_mismatch" not in result["evidence"]["errors"]
    assert "actual_head_mismatch" not in result["evidence"]["errors"]


def test_startup_adoption_accepts_adopt_existing_branch_alias(tmp_path: Path) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    adopted_head = commit_worker_candidate(fixture)
    subject = fixture.startup_subject(contract)
    subject["branch_adoption_mode"] = "adopt_existing_branch"
    subject["branch_adoption_evidence"] = _adoption_evidence(fixture, adopted_head)

    result = run_precheck(
        "mf_subagent.startup",
        CONTRACT_ID,
        "startup_gate",
        subject,
        "pytest",
    )

    assert result["decision"] == "allow"
    assert result["evidence"]["branch_adoption_mode"] == "adopt_existing_branch"
    assert result["evidence"]["branch_adoption"]["accepted"] is True


# --- startup gate: false startup still rejected -----------------------------


def test_startup_rejects_attested_head_that_is_not_actual_head(tmp_path: Path) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    commit_worker_candidate(fixture)
    subject = fixture.startup_subject(contract)
    subject["branch_adoption_mode"] = "existing_branch"
    # Claims the worker sits at base while the worktree really moved ahead.
    subject["branch_adoption_evidence"] = _adoption_evidence(
        fixture,
        fixture.base_commit,
    )

    result = run_precheck(
        "mf_subagent.startup",
        CONTRACT_ID,
        "startup_gate",
        subject,
        "pytest",
    )

    assert result["decision"] == "block"
    assert "adoption_attested_head_mismatch" in result["evidence"]["errors"]
    assert "worker_head_mismatch" in result["evidence"]["errors"]
    assert "actual_head_mismatch" in result["evidence"]["errors"]
    assert result["evidence"]["branch_adoption"]["accepted"] is False


def test_startup_rejects_missing_adoption_evidence_at_non_base_head(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    commit_worker_candidate(fixture)
    subject = fixture.startup_subject(contract)
    subject["branch_adoption_mode"] = "existing_branch"

    result = run_precheck(
        "mf_subagent.startup",
        CONTRACT_ID,
        "startup_gate",
        subject,
        "pytest",
    )

    assert result["decision"] == "block"
    assert "missing_existing_branch_adoption_evidence" in result["evidence"]["errors"]
    assert "worker_head_mismatch" in result["evidence"]["errors"]
    assert "actual_head_mismatch" in result["evidence"]["errors"]


def test_startup_rejects_fabricated_base_head_lineage(tmp_path: Path) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    moved_head = advance_target_head(fixture)
    adopted_head = commit_worker_candidate(fixture)
    subject = fixture.startup_subject(contract)
    # The attested base is a real commit that is NOT an ancestor of the
    # adopted head: the claimed base..head lineage is fabricated.
    subject["base_commit"] = moved_head
    subject["target_head_commit"] = moved_head
    subject["branch_adoption_mode"] = "existing_branch"
    subject["branch_adoption_evidence"] = _adoption_evidence(
        fixture,
        adopted_head,
        adopted_base_commit=moved_head,
    )

    result = run_precheck(
        "mf_subagent.startup",
        CONTRACT_ID,
        "startup_gate",
        subject,
        "pytest",
    )

    assert result["decision"] == "block"
    assert "adoption_base_not_ancestor_of_head" in result["evidence"]["errors"]
    assert result["evidence"]["branch_adoption"]["accepted"] is False


def test_startup_rejects_unknown_attested_base_object(tmp_path: Path) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    adopted_head = commit_worker_candidate(fixture)
    bogus_base = "f" * 40
    subject = fixture.startup_subject(contract)
    subject["base_commit"] = bogus_base
    subject["branch_adoption_mode"] = "existing_branch"
    subject["branch_adoption_evidence"] = _adoption_evidence(
        fixture,
        adopted_head,
        adopted_base_commit=bogus_base,
    )

    result = run_precheck(
        "mf_subagent.startup",
        CONTRACT_ID,
        "startup_gate",
        subject,
        "pytest",
    )

    assert result["decision"] == "block"
    assert "adoption_ancestry_unverifiable" in result["evidence"]["errors"]


# --- dispatch gate: adoption mode and unchanged fresh semantics --------------


def test_dispatch_adoption_does_not_require_worker_head_at_base(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    adopted_head = commit_worker_candidate(fixture)
    subject = fixture.dispatch_subject(contract)
    subject["branch_adoption_mode"] = "adopt_existing_branch"
    subject["branch_adoption_evidence"] = _adoption_evidence(fixture, adopted_head)

    result = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        subject,
        "pytest",
    )

    assert result["decision"] == "allow"
    assert "worker_head_mismatch" not in result["evidence"]["errors"]
    adoption = result["evidence"]["branch_adoption"]
    assert adoption["accepted"] is True
    assert adoption["enforced"] is True
    assert adoption["ancestry_check"] == "git_merge_base_is_ancestor"


def test_dispatch_adoption_without_evidence_still_blocks_off_base_head(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    commit_worker_candidate(fixture)
    subject = fixture.dispatch_subject(contract)
    subject["branch_adoption_mode"] = "existing_branch"

    result = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        subject,
        "pytest",
    )

    assert result["decision"] == "block"
    assert "missing_existing_branch_adoption_evidence" in result["evidence"]["errors"]
    assert "worker_head_mismatch" in result["evidence"]["errors"]


def test_fresh_dispatch_semantics_unchanged_without_adoption_mode(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)

    at_base = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        fixture.dispatch_subject(contract),
        "pytest",
    )
    assert at_base["decision"] == "allow"
    assert at_base["evidence"]["branch_adoption"] == {}

    commit_worker_candidate(fixture)
    off_base = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        fixture.dispatch_subject(contract),
        "pytest",
    )
    assert off_base["decision"] == "block"
    assert "worker_head_mismatch" in off_base["evidence"]["errors"]


def test_fresh_startup_semantics_unchanged_without_adoption_mode(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)

    at_base = run_precheck(
        "mf_subagent.startup",
        CONTRACT_ID,
        "startup_gate",
        fixture.startup_subject(contract),
        "pytest",
    )
    assert at_base["decision"] == "allow"
    assert at_base["evidence"]["branch_adoption"] == {}

    commit_worker_candidate(fixture)
    off_base = run_precheck(
        "mf_subagent.startup",
        CONTRACT_ID,
        "startup_gate",
        fixture.startup_subject(contract),
        "pytest",
    )
    assert off_base["decision"] == "block"
    assert "worker_head_mismatch" in off_base["evidence"]["errors"]
    assert "actual_head_mismatch" in off_base["evidence"]["errors"]


# --- close route_context_gate: adoption equivalence --------------------------


def _route_context_events() -> list[dict[str, object]]:
    return [
        {
            "event_kind": "route_context",
            "phase": "dispatch",
            "status": "passed",
            "event_id": "tl-route-context",
            "payload": {
                "route_context": {
                    **ROUTE_IDENTITY,
                    "caller_role": "observer",
                    "required_lanes": ["bounded_implementation_worker"],
                },
                "visible_injection_manifest_hash": "sha256:test-visible-manifest",
            },
        },
        {
            "event_kind": "route_action_precheck",
            "phase": "pre_mutation",
            "status": "allowed",
            "event_id": "tl-route-action",
            "verification": {**ROUTE_IDENTITY, "allowed_action": "dispatch_worker"},
        },
        {
            "event_kind": "mf_subagent_dispatch",
            "phase": "dispatch",
            "status": "passed",
            "event_id": "tl-dispatch",
            "payload": {
                "mf_subagent_dispatch_gate": {
                    **ROUTE_IDENTITY,
                    "worker_id": "mf-sub-adopt",
                    "bounded": True,
                }
            },
        },
    ]


def _adoption_startup_event(
    *,
    adopted_head: str = "adopted-head",
    actual_head: str = "adopted-head",
    with_evidence: bool = True,
) -> dict[str, object]:
    gate: dict[str, object] = {
        **ROUTE_IDENTITY,
        "worker_id": "mf-sub-adopt",
        "fence_token": "fence-test",
        "actual_cwd": "/repo/.worktrees/mf-sub-adopt",
        "actual_git_root": "/repo/.worktrees/mf-sub-adopt",
        "branch": "refs/heads/codex/mf-sub-adopt",
        "head_commit": actual_head,
        "branch_adoption_mode": "existing_branch",
    }
    if with_evidence:
        gate["branch_adoption_evidence"] = {
            "schema_version": "branch_adoption_evidence.v1",
            "adopted_branch_ref": "refs/heads/codex/mf-sub-adopt",
            "adopted_base_commit": "adopted-base",
            "adopted_head_commit": adopted_head,
            "attestation_source": "git rev-parse HEAD",
        }
    return {
        "event_type": "mf_subagent.startup_adoption",
        "event_kind": "mf_subagent_startup_adoption",
        "phase": "startup_gate",
        "status": "passed",
        "event_id": "tl-startup-adoption",
        "payload": {"mf_subagent_startup_adoption_gate": gate},
    }


def _qa_verification_event() -> dict[str, object]:
    return {
        "event_kind": "qa_verification",
        "phase": "verification",
        "status": "passed",
        "event_id": "tl-qa-verification",
        "verification": {
            **ROUTE_IDENTITY,
            "contract_evidence": [
                {
                    "requirement_id": "independent_verification_lane",
                    "status": "passed",
                    "reviewer_role": "qa",
                }
            ],
        },
    }


def _fresh_startup_event() -> dict[str, object]:
    return {
        "event_kind": "mf_subagent_startup",
        "phase": "startup_gate",
        "status": "passed",
        "event_id": "tl-startup",
        "payload": {
            "mf_subagent_startup_gate": {
                **ROUTE_IDENTITY,
                "worker_id": "mf-sub-adopt",
                "fence_token": "fence-test",
                "actual_cwd": "/repo/.worktrees/mf-sub-adopt",
                "actual_git_root": "/repo/.worktrees/mf-sub-adopt",
                "branch": "refs/heads/codex/mf-sub-adopt",
                "head_commit": "head-test",
            }
        },
    }


def test_close_route_gate_accepts_adoption_startup_equivalent() -> None:
    events = [
        *_route_context_events(),
        _adoption_startup_event(),
        _qa_verification_event(),
    ]

    gate = task_timeline.mf_route_context_gate_verification(events, CLOSE_GATE_CONTRACT)

    assert gate["passed"] is True, gate
    assert "mf_subagent_startup" in gate["present_requirement_ids"]
    assert gate["checks"]["mf_subagent_startup_present"] is True
    startup_refs = gate["evidence_events"]["mf_subagent_startup"]
    assert startup_refs[0]["event_kind"] == "mf_subagent_startup_adoption"


def test_close_route_gate_rejects_adoption_with_mismatched_attested_head() -> None:
    events = [
        *_route_context_events(),
        _adoption_startup_event(adopted_head="other-head", actual_head="real-head"),
    ]

    gate = task_timeline.mf_route_context_gate_verification(events, CLOSE_GATE_CONTRACT)

    assert gate["passed"] is False, gate
    assert "mf_subagent_startup" in gate["missing_requirement_ids"]
    reasons = {item["reason"] for item in gate["ignored_route_events"]}
    assert "invalid_branch_adoption_evidence" in reasons


def test_close_route_gate_rejects_adoption_without_evidence() -> None:
    events = [*_route_context_events(), _adoption_startup_event(with_evidence=False)]

    gate = task_timeline.mf_route_context_gate_verification(events, CLOSE_GATE_CONTRACT)

    assert gate["passed"] is False, gate
    assert "mf_subagent_startup" in gate["missing_requirement_ids"]
    reasons = {item["reason"] for item in gate["ignored_route_events"]}
    assert "invalid_branch_adoption_evidence" in reasons


def test_close_route_gate_fresh_startup_unchanged() -> None:
    events = [
        *_route_context_events(),
        _fresh_startup_event(),
        _qa_verification_event(),
    ]

    gate = task_timeline.mf_route_context_gate_verification(events, CLOSE_GATE_CONTRACT)

    assert gate["passed"] is True, gate
    assert "mf_subagent_startup" in gate["present_requirement_ids"]


def test_adoption_startup_event_kind_is_protected_close_evidence() -> None:
    # Parity with fresh mf_subagent_startup: protection keys off event_kind.
    assert task_timeline.is_protected_close_evidence(
        {"event_kind": "mf_subagent_startup_adoption"}
    )
