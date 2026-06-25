"""Adapters that let legacy gates feed ContractGateKernel decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .write_gate import validate_contract_write


def submit_line_write_gate_adapter(
    definition: Mapping[str, Any],
    execution_state: Mapping[str, Any],
    write: Mapping[str, Any],
    *,
    runtime_guide: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the existing line write gate and return adapter evidence."""

    decision = validate_contract_write(
        definition,
        execution_state,
        write,
        runtime_guide=runtime_guide,
    )
    return {
        "schema_version": "contract_gate_legacy_adapter_result.v1",
        "adapter_id": "contract_write_gate.v1",
        "decision": "allow" if decision.ok else "block",
        "ok": decision.ok,
        "errors": list(decision.errors),
        "warnings": [],
        "legacy_authoritative": True,
    }


def warning_adapter_result(
    adapter_id: str,
    warning: str,
    *,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "contract_gate_legacy_adapter_result.v1",
        "adapter_id": str(adapter_id or ""),
        "decision": "warn",
        "ok": True,
        "errors": [],
        "warnings": [str(warning or "")],
        "evidence": dict(evidence or {}),
        "legacy_authoritative": False,
    }


def adapter_errors(results: Sequence[Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    for result in results:
        errors.extend(str(item) for item in result.get("errors") or [] if str(item or ""))
    return errors


def adapter_warnings(results: Sequence[Mapping[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for result in results:
        warnings.extend(str(item) for item in result.get("warnings") or [] if str(item or ""))
    return warnings
