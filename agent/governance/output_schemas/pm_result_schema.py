"""PM-stage result preflight validator (PR1d).

Validates a PM task's result payload BEFORE auto-chain advances to dev.
Catches the missing-declaration case where the acceptance_criteria imply a
file deletion (DELETE / remove / replaces / replaced_by keywords, case-
insensitive substring scan) AND target_files is non-empty, but the PM
result payload omits both removed_nodes and unmapped_files. This is the
PM-side analogue of the dev-side phantom-create check: without the PM
declarations, the dev-stage auto-inferrer cannot avoid emitting phantom
creates against deleted nodes/files, which leads to chain bouncing.

Reuses the ValidationError / ValidationResult dataclasses from the dev
result validator so call-sites (auto_chain._validate_pm_at_transition,
__init__.py public API) consume a single shape.
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

# Case-insensitive simple substring scan only — no LLM, no regex
# backreferences, no external service. Each acceptance_criteria entry is
# lowered and checked against this tuple of keywords. Verifiable by
# inspection.
_DELETE_KEYWORDS = ("delete", "remove", "replaces", "replaced_by")


def _has_delete_keyword(acceptance_criteria: Any) -> bool:
    """Return True iff any AC string contains a delete-keyword (case-insensitive)."""
    if not acceptance_criteria:
        return False
    if isinstance(acceptance_criteria, str):
        items: list[str] = [acceptance_criteria]
    elif isinstance(acceptance_criteria, list):
        items = [str(x) for x in acceptance_criteria if x is not None]
    else:
        return False
    for ac in items:
        lowered = ac.lower()
        for kw in _DELETE_KEYWORDS:
            if kw in lowered:
                return True
    return False


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
    """Validate a PM-stage result payload.

    Modes: 'strict' (errors stay as errors), 'warn' (FATAL stay, non-FATAL
    demoted to warnings), 'disabled' (everything demoted; valid=True).

    Currently the only check is MISSING_DECLARATION_FOR_DELETED_FILE — this
    is PM-side enforcement of the declarations contract described in
    docs/roles/pm.md. Other structural checks (required fields, JSON shape)
    are intentionally NOT duplicated here; they are enforced by the existing
    _gate_post_pm. This validator focuses on the declarations gap that has
    historically caused chain bouncing.
    """
    errors: list[ValidationError] = []
    if not isinstance(payload, dict):
        errors.append(ValidationError(
            error_codes.MALFORMED_JSON, "$",
            "payload is not a JSON object", "error"))
        return _apply_mode(errors, mode)

    # PRD-shape support: PM result payloads sometimes wrap the declarations
    # under a 'prd' sub-dict (legacy shape). Read both top-level and
    # prd-nested forms; the first non-empty wins.
    prd = payload.get("prd") if isinstance(payload.get("prd"), dict) else {}

    target_files = payload.get("target_files")
    if _is_empty_list(target_files) and isinstance(prd, dict):
        target_files = prd.get("target_files")

    acceptance_criteria = payload.get("acceptance_criteria")
    if _is_empty_list(acceptance_criteria) and isinstance(prd, dict):
        acceptance_criteria = prd.get("acceptance_criteria")

    removed_nodes = payload.get("removed_nodes")
    if _is_empty_list(removed_nodes) and isinstance(prd, dict):
        removed_nodes = prd.get("removed_nodes")

    unmapped_files = payload.get("unmapped_files")
    if _is_empty_list(unmapped_files) and isinstance(prd, dict):
        unmapped_files = prd.get("unmapped_files")

    has_target_files = (
        isinstance(target_files, list) and len(target_files) > 0
    )
    has_delete_kw = _has_delete_keyword(acceptance_criteria)
    declarations_empty = (
        _is_empty_list(removed_nodes) and _is_empty_list(unmapped_files)
    )

    if has_target_files and has_delete_kw and declarations_empty:
        errors.append(ValidationError(
            error_codes.MISSING_DECLARATION_FOR_DELETED_FILE,
            "$.removed_nodes",
            (
                "acceptance_criteria imply file deletion "
                "(DELETE/remove/replaces/replaced_by) and target_files is "
                "non-empty, but both removed_nodes and unmapped_files are "
                "empty. PM must declare which graph nodes will be removed "
                "and which files will be unmapped so the dev-stage auto-"
                "inferrer does not emit phantom creates."
            ),
            "error",
            suggested_fix=(
                "Populate removed_nodes (list of node_ids being deleted) "
                "and/or unmapped_files (list of file paths whose owning "
                "nodes should be unmapped). See "
                "docs/roles/pm.md '## Graph-delta declarations'."
            ),
        ))

    return _apply_mode(errors, mode)


__all__ = [
    "SCHEMA_VERSION",
    "VALIDATOR_VERSION",
    "ValidationError",
    "ValidationResult",
    "validate_pm_output",
]
