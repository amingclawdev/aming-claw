"""PreToolUse onboarding guard for Aming Claw protected actions."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, TextIO


ONBOARD_COMMAND = "/aming-claw:onboard"
AUDIT_ENV = "AMING_CLAW_ONBOARDING_GUARD_AUDIT"
ONBOARDED_ENV = "AMING_CLAW_ONBOARDED"

PROTECTED_ACTIONS = frozenset(
    {
        "backlog_close",
        "bootstrap_project",
        "dispatch_bounded_implementation_lane",
        "dispatch_bounded_implementation_worker",
        "dispatch_bounded_mf_sub",
        "dispatch_bounded_worker",
        "execute_backlog_row",
        "observer_command_claim",
        "observer_command_complete",
        "observer_command_enqueue",
        "observer_command_fail",
        "observer_command_takeover",
        "observer_dispatch",
        "observer_dispatch_bounded_worker",
        "parallel_branch_startup",
        "project_bootstrap",
    }
)

PROTECTED_TEXT_PATTERNS = (
    (
        "project_bootstrap",
        re.compile(r"(?<![\w-])aming-claw\s+bootstrap(?![\w-])", re.IGNORECASE),
    ),
    ("project_bootstrap", re.compile(r"/api/project/bootstrap\b", re.IGNORECASE)),
    ("bootstrap_project", re.compile(r"\bbootstrap_project\b", re.IGNORECASE)),
    ("project_bootstrap", re.compile(r"\bproject_bootstrap\b", re.IGNORECASE)),
    ("parallel_branch_startup", re.compile(r"\bparallel_branch_startup\b", re.IGNORECASE)),
    ("execute_backlog_row", re.compile(r"\bexecute_backlog_row\b", re.IGNORECASE)),
    (
        "observer_dispatch",
        re.compile(r"\bobserver_dispatch(?:_bounded_worker)?\b", re.IGNORECASE),
    ),
    (
        "dispatch_bounded_worker",
        re.compile(
            r"\bdispatch_bounded_(?:worker|implementation_worker|mf_sub)\b",
            re.IGNORECASE,
        ),
    ),
)

ONBOARD_COMPLETE_VALUES = frozenset(
    {
        "1",
        "complete",
        "completed",
        "done",
        "onboarded",
        "true",
        "yes",
    }
)

TOOL_NAME_KEYS = ("tool_name", "toolName", "name")
ACTION_KEYS = (
    "action",
    "action_id",
    "command_type",
    "operation",
    "precheck_id",
    "requested_action",
    "route_action",
    "tool_name",
    "toolName",
)
TEXT_KEYS = ("command", "cmd", "url", "path", "endpoint")


def canonical_name(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return ""
    for separator in ("__", ".", "/", ":"):
        if separator in text:
            text = text.rsplit(separator, 1)[-1]
    return re.sub(r"[^a-z0-9_]+", "_", text).strip("_")


def _truthy_complete(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in ONBOARD_COMPLETE_VALUES
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, Mapping):
        for key in ("complete", "completed", "done", "onboarded", "is_complete"):
            if _truthy_complete(value.get(key)):
                return True
        for key in ("status", "state", "phase", "value"):
            if _truthy_complete(value.get(key)):
                return True
    return False


def _walk_mappings(value: Any, *, depth: int = 0) -> list[Mapping[str, Any]]:
    if depth > 5:
        return []
    if isinstance(value, Mapping):
        mappings: list[Mapping[str, Any]] = [value]
        for child in value.values():
            mappings.extend(_walk_mappings(child, depth=depth + 1))
        return mappings
    if isinstance(value, list):
        mappings = []
        for child in value:
            mappings.extend(_walk_mappings(child, depth=depth + 1))
        return mappings
    return []


def payload_onboarding_complete(payload: Mapping[str, Any]) -> bool:
    for mapping in _walk_mappings(payload):
        for key, value in mapping.items():
            normalized = canonical_name(key)
            if "onboard" in normalized and _truthy_complete(value):
                return True
            if normalized in {"aming_claw_onboarding_complete", "aming_claw_onboarded"}:
                return _truthy_complete(value)
    return False


def env_onboarding_complete(env: Mapping[str, str] | None = None) -> bool:
    effective_env = os.environ if env is None else env
    return _truthy_complete(effective_env.get(ONBOARDED_ENV))


def onboarding_complete(
    payload: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> bool:
    return env_onboarding_complete(env) or payload_onboarding_complete(payload)


def _candidate_action_values(payload: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in TOOL_NAME_KEYS:
        if key in payload:
            values.append(str(payload[key]))
    for mapping in _walk_mappings(payload):
        for key in ACTION_KEYS:
            value = mapping.get(key)
            if isinstance(value, (str, int, float)):
                values.append(str(value))
    return values


def _candidate_text_values(payload: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for mapping in _walk_mappings(payload):
        for key in TEXT_KEYS:
            value = mapping.get(key)
            if isinstance(value, str):
                values.append(value)
    return values


def protected_match(payload: Mapping[str, Any]) -> str:
    for value in _candidate_action_values(payload):
        name = canonical_name(value)
        if name in PROTECTED_ACTIONS:
            return name

    for text in _candidate_text_values(payload):
        for label, pattern in PROTECTED_TEXT_PATTERNS:
            match = pattern.search(text)
            if match:
                return label

    return ""


def deny_response(match: str) -> dict[str, str]:
    return {
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"Aming Claw onboarding is required before {match or 'this protected action'}. "
            f"Run {ONBOARD_COMMAND}, complete onboarding, then retry."
        ),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_audit_record(path: str, *, match: str, reason: str) -> None:
    if not path:
        return
    record = {
        "schema_version": "aming_claw_onboarding_guard_audit.v1",
        "event": "pretooluse_onboarding_guard",
        "decision": "deny",
        "tool_match": match,
        "reason": reason,
        "next_action": ONBOARD_COMMAND,
        "timestamp": _utc_now(),
    }
    audit_path = Path(path).expanduser()
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def evaluate(
    payload: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    match = protected_match(payload)
    if not match or onboarding_complete(payload, env):
        return {"allow": True, "match": match}
    response = deny_response(match)
    return {
        "allow": False,
        "match": match,
        "response": response,
        "reason": response["permissionDecisionReason"],
    }


def main(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    effective_env = os.environ if env is None else env

    try:
        payload = json.load(stdin)
    except json.JSONDecodeError as exc:
        response = deny_response("invalid_pretooluse_payload")
        stdout.write(json.dumps(response, sort_keys=True) + "\n")
        stderr.write(f"Invalid PreToolUse payload: {exc}\n")
        write_audit_record(
            str(effective_env.get(AUDIT_ENV, "")),
            match="invalid_pretooluse_payload",
            reason=response["permissionDecisionReason"],
        )
        return 2

    if not isinstance(payload, Mapping):
        response = deny_response("invalid_pretooluse_payload")
        stdout.write(json.dumps(response, sort_keys=True) + "\n")
        write_audit_record(
            str(effective_env.get(AUDIT_ENV, "")),
            match="invalid_pretooluse_payload",
            reason=response["permissionDecisionReason"],
        )
        return 2

    decision = evaluate(payload, effective_env)
    if decision["allow"]:
        return 0

    response = decision["response"]
    stdout.write(json.dumps(response, sort_keys=True) + "\n")
    write_audit_record(
        str(effective_env.get(AUDIT_ENV, "")),
        match=str(decision["match"]),
        reason=str(decision["reason"]),
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
