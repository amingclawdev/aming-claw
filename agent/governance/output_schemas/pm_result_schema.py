"""PM-stage result preflight validator (PR1f — structural-only).

Validates a PM task's result payload BEFORE auto-chain advances to dev.
This validator is STRUCTURAL ONLY: it checks shape and types of the payload
and never inspects the natural-language content of acceptance criteria. The
prior PR1d substring scan for delete-keywords (DELETE / remove / replaces /
replaced_by) was removed because it over-fired on ACs that mention those
words as feature description rather than file-deletion intent (e.g.
"Rule J respects dev removes", "_file_deleted_in_worktree"). PM is a
proposer not an executor — graph deletes are enforced at QA/merge/swap,
not at PM. PM declarations remain advisory hints for the auto-inferrer.

Structural checks (S1–S7):
    S1  payload-not-dict                         → MALFORMED_JSON
    S2  acceptance_criteria absent / not list /   → MISSING_REQUIRED_FIELD
        empty
    S3  any AC element is not a string            → MISSING_REQUIRED_FIELD
    S4  empty work scope (target_files empty AND  → MISSING_REQUIRED_FIELD
        proposed_nodes empty)
    S5  any proposed_nodes element missing a      → MISSING_REQUIRED_FIELD
        non-empty 'primary'
    S6  removed_nodes / unmapped_files present    → MALFORMED_JSON
        but not a list
    S7  bypass_validations key present in payload → UNAUTHORIZED_SELF_WAIVER

The validator still reads the optional 'prd' sub-dict as a fallback when
top-level fields are absent or empty (legacy PM result shape).

Reuses ValidationError / ValidationResult / _apply_mode from
dev_result_schema so all call-sites consume a single shape.
"""
from __future__ import annotations

from typing import Any

from . import error_codes
from .dev_result_schema import (
    SCHEMA_VERSION,
    VALIDATOR_VERSION,
    ValidationError,
    ValidationResult,
    _apply_mode,
)


def _is_empty_list(value: Any) -> bool:
    """True for None, [], or non-list values that should be treated as empty."""
    if value is None:
        return True
    if isinstance(value, list):
        return len(value) == 0
    # Non-list types (e.g. dict shapes) — treat as missing/empty for safety.
    return False


def validate_pm_output(payload: dict, chain_context: Any = None,
                       mode: str = "warn") -> ValidationResult:
    """Validate a PM-stage result payload (structural-only).

    Modes: 'strict' (errors stay as errors), 'warn' (FATAL stay, non-FATAL
    demoted to warnings), 'disabled' (everything demoted; valid=True).

    See module docstring for the S1–S7 structural checks. The validator
    never inspects natural-language AC content; delete/remove/replaces
    intent is now handled downstream by dev's graph_delta.removes plus
    filesystem truth (PR1e) and the auto-inferrer's PRD-declaration hint
    (PR1c).
    """
    errors: list[ValidationError] = []

    # S1: payload must be a dict.
    if not isinstance(payload, dict):
        errors.append(ValidationError(
            error_codes.MALFORMED_JSON, "$",
            "payload is not a JSON object", "error"))
        return _apply_mode(errors, mode)

    # S7: bypass_validations is a self-waiver attempt — block immediately.
    if "bypass_validations" in payload:
        errors.append(ValidationError(
            error_codes.UNAUTHORIZED_SELF_WAIVER, "$.bypass_validations",
            "pm role cannot self-waive validation; use observer_emergency_bypass",
            "error",
            suggested_fix=(
                "remove bypass_validations; ask observer for emergency bypass"
            ),
        ))

    # PRD-shape support: PM result payloads sometimes wrap the declarations
    # under a 'prd' sub-dict (legacy shape). Read both top-level and
    # prd-nested forms; the first non-empty wins.
    prd = payload.get("prd") if isinstance(payload.get("prd"), dict) else {}

    # S2: acceptance_criteria must be present, a list, and non-empty.
    acceptance_criteria = payload.get("acceptance_criteria")
    if _is_empty_list(acceptance_criteria) and isinstance(prd, dict):
        prd_ac = prd.get("acceptance_criteria")
        if not _is_empty_list(prd_ac):
            acceptance_criteria = prd_ac

    if acceptance_criteria is None:
        errors.append(ValidationError(
            error_codes.MISSING_REQUIRED_FIELD, "$.acceptance_criteria",
            "missing required field 'acceptance_criteria'", "error"))
    elif not isinstance(acceptance_criteria, list):
        errors.append(ValidationError(
            error_codes.MISSING_REQUIRED_FIELD, "$.acceptance_criteria",
            "acceptance_criteria must be a list of strings", "error"))
    elif len(acceptance_criteria) == 0:
        errors.append(ValidationError(
            error_codes.MISSING_REQUIRED_FIELD, "$.acceptance_criteria",
            "acceptance_criteria must be non-empty", "error"))
    else:
        # S3: every AC element must be a string.
        for i, ac in enumerate(acceptance_criteria):
            if not isinstance(ac, str):
                errors.append(ValidationError(
                    error_codes.MISSING_REQUIRED_FIELD,
                    f"$.acceptance_criteria[{i}]",
                    "acceptance_criteria element is not a string",
                    "error"))

    # S4: work scope — at least one of target_files / proposed_nodes is
    # non-empty (read from prd as fallback).
    target_files = payload.get("target_files")
    if _is_empty_list(target_files) and isinstance(prd, dict):
        prd_tf = prd.get("target_files")
        if not _is_empty_list(prd_tf):
            target_files = prd_tf

    proposed_nodes = payload.get("proposed_nodes")
    if _is_empty_list(proposed_nodes) and isinstance(prd, dict):
        prd_pn = prd.get("proposed_nodes")
        if not _is_empty_list(prd_pn):
            proposed_nodes = prd_pn

    has_target_files = (
        isinstance(target_files, list) and len(target_files) > 0
    )
    has_proposed_nodes = (
        isinstance(proposed_nodes, list) and len(proposed_nodes) > 0
    )
    if not has_target_files and not has_proposed_nodes:
        errors.append(ValidationError(
            error_codes.MISSING_REQUIRED_FIELD, "$.target_files",
            (
                "PM work scope is empty: both target_files and "
                "proposed_nodes are absent or empty. PM must declare at "
                "least one target_file or proposed_node so the dev stage "
                "has a concrete scope."
            ),
            "error",
            suggested_fix=(
                "populate target_files (list of file paths the dev stage "
                "will modify) and/or proposed_nodes (list of new graph "
                "nodes) in the PM result payload."
            ),
        ))

    # S5: every proposed_nodes element must have a non-empty 'primary'.
    if isinstance(proposed_nodes, list):
        for i, n in enumerate(proposed_nodes):
            if not isinstance(n, dict):
                errors.append(ValidationError(
                    error_codes.MISSING_REQUIRED_FIELD,
                    f"$.proposed_nodes[{i}]",
                    "proposed_nodes element is not an object",
                    "error"))
                continue
            primary = n.get("primary")
            if not (isinstance(primary, str) and primary.strip()):
                errors.append(ValidationError(
                    error_codes.MISSING_REQUIRED_FIELD,
                    f"$.proposed_nodes[{i}].primary",
                    "proposed_nodes element missing non-empty 'primary'",
                    "error"))

    # S6: removed_nodes / unmapped_files MUST be lists when present.
    for fld in ("removed_nodes", "unmapped_files"):
        val = payload.get(fld)
        if val is None and isinstance(prd, dict):
            val = prd.get(fld)
        if val is not None and not isinstance(val, list):
            errors.append(ValidationError(
                error_codes.MALFORMED_JSON, f"$.{fld}",
                f"'{fld}' must be a list when present", "error"))

    return _apply_mode(errors, mode)


__all__ = [
    "SCHEMA_VERSION",
    "VALIDATOR_VERSION",
    "ValidationError",
    "ValidationResult",
    "validate_pm_output",
]
