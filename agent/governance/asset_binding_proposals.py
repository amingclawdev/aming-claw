"""Proposal/precheck helpers for doc/test/config asset bindings.

Weak asset evidence is review input, not trusted graph state.  This module is
used by deterministic scanners, AI output intake, and MF subagent contracts so
the same constraints are visible before the authoritative gate runs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any, Mapping


PROPOSAL_SCHEMA_VERSION = "asset_binding_proposal.v1"
PRECHECK_SCHEMA_VERSION = "asset_binding_precheck.v1"

ALLOWED_ASSET_KINDS = {"doc", "test", "config"}
ALLOWED_OPERATIONS = {"propose_binding", "materialize_binding"}
AI_PROPOSERS = {
    "ai",
    "ai_session",
    "codex",
    "claude",
    "codex_subagent",
    "claude_subagent",
    "subagent",
    "mf_subagent",
    "semantic_worker",
}
STRONG_EVIDENCE_KINDS = {
    "accepted_review_decision",
    "direct_symbol_import",
    "governance_hint",
    "registered_config_loader",
    "registered_config_rule",
    "same_language_test_name_match",
    "source_controlled_hint",
}
WEAK_EVIDENCE_KINDS = {
    "basename_reference",
    "filename_match",
    "import_only",
    "module_name_reference",
    "path_mention",
    "path_reference",
    "semantic_summary",
    "string_literal",
    "test_import_fanin",
    "weak_tests",
}


def proposal_fingerprint(payload: Mapping[str, Any]) -> str:
    """Return a stable hash for a proposal excluding precheck evidence."""

    normalized = _without_self_precheck(dict(payload))
    data = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def precheck_asset_binding_proposal(
    payload: Mapping[str, Any],
    *,
    mode: str = "self_precheck",
) -> dict[str, Any]:
    """Validate an asset-binding proposal using gate-compatible constraints."""

    errors: list[str] = []
    warnings: list[str] = []
    normalized = _normalize_payload(payload, errors=errors, warnings=warnings)
    strength = _binding_strength(normalized)
    operation = normalized.get("operation", "")
    decision = "can_materialize" if strength == "strong" else "review_required"

    if operation == "materialize_binding" and strength != "strong":
        errors.append("weak_evidence_cannot_materialize")
    if normalized.get("asset_kind") == "doc" and operation == "materialize_binding":
        if normalized.get("evidence_kind") not in {
            "accepted_review_decision",
            "governance_hint",
            "source_controlled_hint",
        }:
            errors.append("doc_materialization_requires_review_or_hint")
    if mode == "server_gate" and _requires_self_precheck(normalized):
        self_precheck = payload.get("self_precheck")
        if not isinstance(self_precheck, Mapping):
            errors.append("self_precheck_required")
        else:
            if self_precheck.get("schema_version") != PRECHECK_SCHEMA_VERSION:
                errors.append("self_precheck_schema_mismatch")
            if self_precheck.get("ok") is not True:
                errors.append("self_precheck_not_ok")
            expected_hash = proposal_fingerprint(payload)
            if str(self_precheck.get("proposal_hash") or "") != expected_hash:
                errors.append("self_precheck_proposal_hash_mismatch")

    result = {
        "schema_version": PRECHECK_SCHEMA_VERSION,
        "mode": mode,
        "ok": not errors,
        "decision": decision,
        "binding_strength": strength,
        "proposal_hash": proposal_fingerprint(payload),
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "normalized": normalized,
    }
    return result


def attach_asset_binding_self_precheck(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a proposal copy with compact self-precheck evidence attached."""

    out = copy.deepcopy(dict(payload))
    result = precheck_asset_binding_proposal(out, mode="self_precheck")
    out["self_precheck"] = compact_precheck(result)
    return out


def compact_precheck(result: Mapping[str, Any]) -> dict[str, Any]:
    """Drop normalized payload details before embedding precheck evidence."""

    return {
        "schema_version": str(result.get("schema_version") or PRECHECK_SCHEMA_VERSION),
        "ok": bool(result.get("ok")),
        "mode": str(result.get("mode") or ""),
        "decision": str(result.get("decision") or ""),
        "binding_strength": str(result.get("binding_strength") or ""),
        "proposal_hash": str(result.get("proposal_hash") or ""),
        "errors": list(result.get("errors") or []),
        "warnings": list(result.get("warnings") or []),
    }


def build_asset_binding_candidate(
    *,
    asset_kind: str,
    asset_path: str,
    target_node_id: str = "",
    target_module: str = "",
    target_title: str = "",
    evidence_kind: str,
    evidence: list[Any] | None = None,
    proposed_by: str = "deterministic_scanner",
    operation: str = "propose_binding",
    source: str = "reconcile",
) -> dict[str, Any]:
    """Build a reviewable binding proposal with self-precheck evidence."""

    payload = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "operation": operation,
        "asset_kind": asset_kind,
        "asset_path": asset_path,
        "target_node_id": target_node_id,
        "target_module": target_module,
        "target_title": target_title,
        "evidence_kind": evidence_kind,
        "evidence": list(evidence or []),
        "proposed_by": proposed_by,
        "source": source,
    }
    return attach_asset_binding_self_precheck(payload)


def trusted_doc_files(doc_coverage: Mapping[str, Any], project_root: str = "") -> list[str]:
    """Return doc paths that can be materialized without a review proposal."""

    trusted = doc_coverage.get("trusted_doc_files")
    if isinstance(trusted, list):
        return [_normalize_relpath(project_root, item) for item in trusted if _normalize_relpath(project_root, item)]
    return []


def doc_binding_candidates(
    doc_coverage: Mapping[str, Any],
    *,
    project_root: str,
    target_module: str,
    target_node_id: str = "",
    target_title: str = "",
) -> list[dict[str, Any]]:
    """Build proposal-first candidates from raw doc coverage matches."""

    trusted = set(trusted_doc_files(doc_coverage, project_root))
    candidates: list[dict[str, Any]] = []
    for raw_path in doc_coverage.get("doc_files") or []:
        path = _normalize_relpath(project_root, raw_path)
        if not path or path in trusted:
            continue
        candidates.append(build_asset_binding_candidate(
            asset_kind="doc",
            asset_path=path,
            target_node_id=target_node_id,
            target_module=target_module,
            target_title=target_title,
            evidence_kind="path_reference",
            evidence=[{
                "kind": "path_reference",
                "path": path,
                "note": "documentation mentions source path, basename, or module token",
            }],
            proposed_by="deterministic_scanner",
            source="doc_coverage",
        ))
    return candidates


def weak_test_binding_candidates(
    test_coverage: Mapping[str, Any],
    *,
    target_module: str,
    target_node_id: str = "",
    target_title: str = "",
) -> list[dict[str, Any]]:
    """Build proposal-first candidates from weak test fan-in evidence."""

    candidates: list[dict[str, Any]] = []
    for entry in test_coverage.get("fan_in_evidence") or []:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("evidence") or "") != "weak_tests":
            continue
        path = _normalize_relpath("", entry.get("path"))
        if not path:
            continue
        source_evidence = str(entry.get("source_evidence") or "test_import_fanin")
        candidates.append(build_asset_binding_candidate(
            asset_kind="test",
            asset_path=path,
            target_node_id=target_node_id,
            target_module=target_module,
            target_title=target_title,
            evidence_kind=source_evidence,
            evidence=[dict(entry)],
            proposed_by="deterministic_scanner",
            source="test_consumer_fanin",
        ))
    return candidates


def _normalize_payload(
    payload: Mapping[str, Any],
    *,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        errors.append("payload_must_be_object")
        return {}
    schema_version = str(payload.get("schema_version") or "").strip()
    if schema_version and schema_version != PROPOSAL_SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    operation = _token(payload.get("operation") or "propose_binding")
    if operation not in ALLOWED_OPERATIONS:
        errors.append("invalid_operation")
    asset_kind = _token(payload.get("asset_kind") or payload.get("kind"))
    if asset_kind not in ALLOWED_ASSET_KINDS:
        errors.append("invalid_asset_kind")
    asset_path = _normalize_relpath("", payload.get("asset_path") or payload.get("path"))
    if not asset_path:
        errors.append("asset_path_required")
    elif not _safe_relpath(asset_path):
        errors.append("unsafe_asset_path")

    target_node_id = str(payload.get("target_node_id") or payload.get("node_id") or "").strip()
    target_module = str(payload.get("target_module") or payload.get("module") or "").strip()
    target_title = str(payload.get("target_title") or payload.get("title") or "").strip()
    if not (target_node_id or target_module or target_title):
        errors.append("target_required")

    evidence_kind = _token(payload.get("evidence_kind") or payload.get("source_evidence"))
    evidence = payload.get("evidence")
    evidence_list = evidence if isinstance(evidence, list) else [evidence] if evidence else []
    if not evidence_kind:
        errors.append("evidence_kind_required")
    elif evidence_kind not in STRONG_EVIDENCE_KINDS | WEAK_EVIDENCE_KINDS:
        warnings.append("unknown_evidence_kind")
    if not evidence_list:
        errors.append("evidence_required")

    return {
        "schema_version": schema_version or PROPOSAL_SCHEMA_VERSION,
        "operation": operation,
        "asset_kind": asset_kind,
        "asset_path": asset_path,
        "target_node_id": target_node_id,
        "target_module": target_module,
        "target_title": target_title,
        "evidence_kind": evidence_kind,
        "evidence": evidence_list,
        "proposed_by": _token(payload.get("proposed_by") or payload.get("producer")),
        "source": str(payload.get("source") or "").strip(),
    }


def _binding_strength(normalized: Mapping[str, Any]) -> str:
    evidence_kind = str(normalized.get("evidence_kind") or "")
    if evidence_kind in STRONG_EVIDENCE_KINDS:
        return "strong"
    return "weak"


def _requires_self_precheck(normalized: Mapping[str, Any]) -> bool:
    return str(normalized.get("proposed_by") or "") in AI_PROPOSERS


def _without_self_precheck(payload: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(payload)
    out.pop("self_precheck", None)
    return out


def _safe_relpath(path: str) -> bool:
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:/", path):
        return False
    parts = PurePosixPath(path).parts
    return ".." not in parts


def _normalize_relpath(project_root: str, path: Any) -> str:
    raw = str(path or "").replace("\\", "/").strip()
    if not raw:
        return ""
    if project_root:
        try:
            root = PurePosixPath(str(project_root).replace("\\", "/"))
            candidate = PurePosixPath(raw)
            if candidate.is_absolute():
                raw = candidate.relative_to(root).as_posix()
        except (TypeError, ValueError):
            pass
    while raw.startswith("./"):
        raw = raw[2:]
    return raw.strip("/")


def _token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


__all__ = [
    "ALLOWED_ASSET_KINDS",
    "ALLOWED_OPERATIONS",
    "PRECHECK_SCHEMA_VERSION",
    "PROPOSAL_SCHEMA_VERSION",
    "attach_asset_binding_self_precheck",
    "build_asset_binding_candidate",
    "compact_precheck",
    "doc_binding_candidates",
    "precheck_asset_binding_proposal",
    "proposal_fingerprint",
    "trusted_doc_files",
    "weak_test_binding_candidates",
]
