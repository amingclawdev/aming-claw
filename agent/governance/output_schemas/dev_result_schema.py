"""Dev-stage result preflight validator (PR1).

Validates a dev task's result payload BEFORE auto-chain advances to test.
Catches phantom creates, mixed parent_layer types, missing required fields,
and unauthorized self-waivers — failure modes that have historically caused
gate retry loops late in the chain.

Reads PM declarations from chain_context.StageSnapshot.result_core (no new
dataclass introduced). When chain_context lacks a pm stage, structural
checks still run; PM-cross-checks are skipped silently.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from . import error_codes
from .error_codes import FATAL_CODES

SCHEMA_VERSION = "v1"
VALIDATOR_VERSION = "1.0.0"


@dataclass
class ValidationError:
    code: str
    field_path: str
    message: str
    severity: str
    suggested_fix: str | None = None
    context_ref: str | None = None


@dataclass
class ValidationResult:
    valid: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    validator_version: str = VALIDATOR_VERSION

    def to_machine_json(self) -> dict:
        return {
            "valid": self.valid,
            "schema_version": self.schema_version,
            "validator_version": self.validator_version,
            "errors": [asdict(e) for e in self.errors],
            "warnings": [asdict(w) for w in self.warnings],
        }

    def to_human_readable(self) -> str:
        lines = [f"ValidationResult(valid={self.valid}, "
                 f"errors={len(self.errors)}, warnings={len(self.warnings)})"]
        for e in self.errors:
            lines.append(f"  ERROR  [{e.code}] {e.field_path}: {e.message}")
        for w in self.warnings:
            lines.append(f"  WARN   [{w.code}] {w.field_path}: {w.message}")
        return "\n".join(lines)


def _pm_decl_from_context(chain_context: Any) -> dict:
    """Extract proposed_nodes / removed_nodes / unmapped_files from a chain
    context snapshot. Accepts dict-shaped (serialized) or object-shaped
    (raw ChainContext) inputs. Returns a dict of three lists, all []-defaulted.
    """
    out = {"proposed_nodes": [], "removed_nodes": [], "unmapped_files": []}
    if chain_context is None:
        return out
    pm_core = None
    if isinstance(chain_context, dict):
        stages = chain_context.get("stages")
        if isinstance(stages, list):
            for s in stages:
                if isinstance(s, dict) and s.get("type") == "pm":
                    pm_core = s.get("result_core") or {}
                    break
        elif isinstance(stages, dict):
            pm = stages.get("pm")
            if isinstance(pm, dict):
                pm_core = pm.get("result_core") or {}
    else:
        stages = getattr(chain_context, "stages", None)
        if isinstance(stages, dict):
            for s in stages.values():
                if getattr(s, "task_type", None) == "pm":
                    pm_core = getattr(s, "result_core", None) or {}
                    break
    if not pm_core:
        return out
    prd = pm_core.get("prd") if isinstance(pm_core.get("prd"), dict) else {}
    pn = pm_core.get("proposed_nodes") or []
    rn = pm_core.get("removed_nodes") or prd.get("removed_nodes") or []
    uf = pm_core.get("unmapped_files") or prd.get("unmapped_files") or []
    out["proposed_nodes"] = pn if isinstance(pn, list) else []
    out["removed_nodes"] = rn if isinstance(rn, list) else []
    out["unmapped_files"] = uf if isinstance(uf, list) else []
    return out


def _proposed_node_ids(proposed_nodes: list) -> set:
    ids = set()
    for n in proposed_nodes or []:
        if isinstance(n, dict) and n.get("node_id"):
            ids.add(n["node_id"])
        elif isinstance(n, str) and n:
            ids.add(n)
    return ids


def validate_dev_output(payload: dict, chain_context: Any = None,
                        mode: str = "warn") -> ValidationResult:
    """Validate a dev-stage result payload.

    Modes: 'strict' (errors stay as errors), 'warn' (fatal stay, non-fatal
    demoted to warnings), 'disabled' (everything demoted; valid=True).
    """
    errors: list[ValidationError] = []
    if not isinstance(payload, dict):
        errors.append(ValidationError(
            error_codes.MALFORMED_JSON, "$",
            "payload is not a JSON object", "error"))
        return _apply_mode(errors, mode)

    if "bypass_validations" in payload:
        errors.append(ValidationError(
            error_codes.UNAUTHORIZED_SELF_WAIVER, "$.bypass_validations",
            "dev role cannot self-waive validation; use observer_emergency_bypass",
            "error",
            suggested_fix="remove bypass_validations; ask observer for emergency bypass"))

    for fld in ("changed_files", "summary"):
        if fld not in payload:
            errors.append(ValidationError(
                error_codes.MISSING_REQUIRED_FIELD, f"$.{fld}",
                f"missing required field '{fld}'", "error"))

    gd = payload.get("graph_delta")
    if gd is not None and not isinstance(gd, dict):
        errors.append(ValidationError(
            error_codes.MALFORMED_JSON, "$.graph_delta",
            "graph_delta must be a JSON object", "error"))
        return _apply_mode(errors, mode)

    creates = (gd or {}).get("creates", []) if isinstance(gd, dict) else []
    if not isinstance(creates, list):
        creates = []

    parent_layer_types = set()
    for i, c in enumerate(creates):
        if not isinstance(c, dict):
            continue
        nid = c.get("node_id", "")
        if not (isinstance(nid, str) and nid.strip()):
            errors.append(ValidationError(
                error_codes.EMPTY_NODE_ID,
                f"$.graph_delta.creates[{i}].node_id",
                "creates[].node_id is empty", "error"))
        pl = c.get("parent_layer")
        if pl is not None:
            parent_layer_types.add(type(pl).__name__)

    if len(parent_layer_types) > 1:
        errors.append(ValidationError(
            error_codes.INVALID_PARENT_LAYER_TYPE,
            "$.graph_delta.creates[*].parent_layer",
            f"parent_layer types are mixed across creates entries: {sorted(parent_layer_types)}",
            "error",
            suggested_fix="use a single consistent type for parent_layer across all creates"))

    pm_decl = _pm_decl_from_context(chain_context)
    proposed_ids = _proposed_node_ids(pm_decl.get("proposed_nodes", []))
    removed = set(pm_decl.get("removed_nodes", []) or [])
    unmapped = set(pm_decl.get("unmapped_files", []) or [])
    if proposed_ids or removed or unmapped:
        for i, c in enumerate(creates):
            if not isinstance(c, dict):
                continue
            nid = c.get("node_id", "")
            primary = c.get("primary", "")
            if primary and primary in unmapped:
                errors.append(ValidationError(
                    error_codes.PHANTOM_CREATE_FOR_UNMAPPED_FILE,
                    f"$.graph_delta.creates[{i}].primary",
                    f"create binds to unmapped file '{primary}' declared by PM",
                    "error"))
            if nid and nid in removed:
                errors.append(ValidationError(
                    error_codes.PHANTOM_CREATE_FOR_DECLARED_REMOVED,
                    f"$.graph_delta.creates[{i}].node_id",
                    f"create node_id '{nid}' is in PM removed_nodes",
                    "error"))
            if nid and proposed_ids and nid not in proposed_ids:
                errors.append(ValidationError(
                    error_codes.CREATE_NOT_IN_PROPOSED_NODES,
                    f"$.graph_delta.creates[{i}].node_id",
                    f"create node_id '{nid}' is not declared in PM proposed_nodes",
                    "error"))

    return _apply_mode(errors, mode)


def _demote(err: ValidationError) -> ValidationError:
    return ValidationError(err.code, err.field_path, err.message, "warning",
                           err.suggested_fix, err.context_ref)


def _apply_mode(errors: list[ValidationError], mode: str) -> ValidationResult:
    if mode == "disabled":
        return ValidationResult(True, [], [_demote(e) for e in errors])
    if mode == "strict":
        return ValidationResult(not errors, list(errors), [])
    e_kept, w_kept = [], []
    for e in errors:
        (e_kept if e.code in FATAL_CODES else w_kept).append(
            e if e.code in FATAL_CODES else _demote(e))
    return ValidationResult(not e_kept, e_kept, w_kept)
