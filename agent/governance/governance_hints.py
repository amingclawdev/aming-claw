"""Parse governance hints embedded in project files.

MVP scope: attach currently-unbound doc/test/config files to existing graph
nodes. Hints are intentionally source-controlled evidence; they are applied
during reconcile instead of mutating graph DB state directly.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .language_policy import DEFAULT_LANGUAGE_POLICY


_HINT_RE = re.compile(r"<!--\s*governance-hint\s*([\s\S]*?)\s*-->", re.IGNORECASE)
_LINE_HINT_RE = re.compile(
    r"(?m)^\s*(?:#|//)\s*governance-hint\s+({.*})\s*$",
    re.IGNORECASE,
)

GOVERNANCE_HINTS_ROOT_KEY = "governance_hints"
GOVERNANCE_HINTS_SCHEMA_VERSION = "governance_hints.v1"
ASSET_BINDING_EVENT_SCHEMA_VERSION = "asset_binding_event.v1"

_ROLE_TO_FIELD = {
    "doc": "secondary",
    "docs": "secondary",
    "document": "secondary",
    "secondary": "secondary",
    "secondary_files": "secondary",
    "test": "test",
    "tests": "test",
    "test_files": "test",
    "config": "config",
    "configs": "config",
    "config_files": "config",
}
_FIELD_TO_ASSET_KIND = {
    "secondary": "doc",
    "test": "test",
    "config": "config",
}


@dataclass(frozen=True)
class BindingHint:
    source_path: str
    path: str
    field: str
    operation: str = "bind"
    target_node_id: str = ""
    target_module: str = ""
    target_title: str = ""
    target_area_key: str = ""
    target_subsystem_key: str = ""
    target_asset_key: str = ""


@dataclass(frozen=True)
class _HintCommentMatch:
    start: int
    end: int
    raw_json: str
    style: str


class GovernanceHintMutationError(ValueError):
    """Raised when a source-backed governance-hint mutation is invalid."""


class GovernanceHintCASMismatch(GovernanceHintMutationError):
    """Raised when source or envelope compare-and-swap evidence is stale."""


def binding_hint_to_dict(hint: BindingHint) -> dict[str, str]:
    return {
        "source_path": hint.source_path,
        "path": hint.path,
        "field": hint.field,
        "operation": hint.operation,
        "target_node_id": hint.target_node_id,
        "target_module": hint.target_module,
        "target_title": hint.target_title,
        "target_area_key": hint.target_area_key,
        "target_subsystem_key": hint.target_subsystem_key,
        "target_asset_key": hint.target_asset_key,
    }


def diff_governance_hint_bindings(
    previous: Iterable[BindingHint],
    current: Iterable[BindingHint],
    *,
    rollback_epoch: str = "",
    source_commit: str = "",
    target_commit: str = "",
) -> dict[str, Any]:
    """Return invertible add/change/remove deltas for incremental reconcile."""
    previous_by_key = _binding_hints_by_key(previous)
    current_by_key = _binding_hints_by_key(current)
    deltas: list[dict[str, Any]] = []

    for key in sorted(set(previous_by_key) | set(current_by_key)):
        old = previous_by_key.get(key)
        new = current_by_key.get(key)
        if old and new and _binding_hint_signature(old) == _binding_hint_signature(new):
            continue
        if old is None and new is not None:
            delta_type = "hint_rollback_restored" if rollback_epoch else "hint_added"
            inverse_action = "remove_restored_binding" if rollback_epoch else "remove_binding"
        elif old is not None and new is None:
            delta_type = "hint_removed"
            inverse_action = "restore_binding"
        elif rollback_epoch:
            delta_type = "hint_rollback_restored"
            inverse_action = "restore_previous_binding"
        else:
            delta_type = "hint_changed"
            inverse_action = "restore_previous_binding"
        delta = {
            "delta_type": delta_type,
            "path": key[1],
            "source_path": key[0],
            "field": (new or old).field if (new or old) else "",
            "previous": binding_hint_to_dict(old) if old else None,
            "current": binding_hint_to_dict(new) if new else None,
            "inverse_action": inverse_action,
            "rollback_epoch": rollback_epoch,
            "source_commit": source_commit,
            "target_commit": target_commit,
        }
        deltas.append(delta)

    by_type: dict[str, int] = {}
    for delta in deltas:
        dtype = str(delta.get("delta_type") or "")
        by_type[dtype] = by_type.get(dtype, 0) + 1
    return {
        "delta_count": len(deltas),
        "by_type": by_type,
        "deltas": deltas,
        "rollback_epoch": rollback_epoch,
        "source_commit": source_commit,
        "target_commit": target_commit,
    }


def normalize_relpath(project_root: str | Path, path: str) -> str:
    raw = str(path or "").replace("\\", "/").strip()
    if not raw:
        return ""
    try:
        root = Path(project_root).resolve()
        candidate = Path(raw)
        if candidate.is_absolute():
            raw = candidate.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        pass
    while raw.startswith("./"):
        raw = raw[2:]
    return raw.strip("/")


def parse_governance_hint_bindings(markdown: str, *, source_path: str = "") -> list[BindingHint]:
    """Return binding hints from a reserved JSON envelope or legacy comments."""
    source_suffix = Path(str(source_path or "")).suffix.lower()
    if source_suffix == ".json":
        try:
            payload = json.loads(markdown or "")
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(payload, dict):
            return []
        envelope = payload.get(GOVERNANCE_HINTS_ROOT_KEY)
        return _bindings_from_governance_hints_envelope(
            envelope,
            source_path=source_path,
        )

    hints: list[BindingHint] = []
    for match in _HINT_RE.finditer(markdown or ""):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        hints.extend(_bindings_from_payload(payload, source_path=source_path))
    for match in _LINE_HINT_RE.finditer(markdown or ""):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        hints.extend(_bindings_from_payload(payload, source_path=source_path))
    return hints


def render_governance_hint_comment(path: str, payload: dict[str, Any]) -> str:
    """Render a governance hint using a comment style safe for the file type."""
    rel = normalize_relpath("", path)
    suffix = Path(rel).suffix.lower()
    name = Path(rel).name.lower()
    body = f"governance-hint {json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    if suffix in {".md", ".mdx", ".html", ".htm"}:
        return f"<!-- {body} -->"
    if suffix in {
        ".py",
        ".pyw",
        ".sh",
        ".bash",
        ".ps1",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".txt",
        ".rst",
        ".adoc",
    } or name in {"dockerfile", "makefile"}:
        return f"# {body}"
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return f"// {body}"
    return ""


def normalize_governance_hints_envelope(value: Any) -> dict[str, Any]:
    """Validate and copy the reserved versioned JSON source-metadata envelope."""
    if not isinstance(value, Mapping):
        raise GovernanceHintMutationError("governance_hints must be an object")
    schema_version = str(value.get("schema_version") or "")
    if schema_version != GOVERNANCE_HINTS_SCHEMA_VERSION:
        raise GovernanceHintMutationError(
            f"governance_hints.schema_version must be {GOVERNANCE_HINTS_SCHEMA_VERSION!r}"
        )
    events = value.get("asset_binding_events")
    if not isinstance(events, list):
        raise GovernanceHintMutationError(
            "governance_hints.asset_binding_events must be an ordered list"
        )
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise GovernanceHintMutationError(
                f"governance_hints.asset_binding_events[{index}] must be an object"
            )
        event_schema = str(event.get("schema_version") or "")
        if event_schema != ASSET_BINDING_EVENT_SCHEMA_VERSION:
            raise GovernanceHintMutationError(
                "governance_hints.asset_binding_events"
                f"[{index}].schema_version must be {ASSET_BINDING_EVENT_SCHEMA_VERSION!r}"
            )
    return deepcopy(dict(value))


def governance_hints_envelope_sha256(value: Any) -> str:
    """Hash only the reserved root envelope for source-metadata CAS/audit."""
    envelope = value if isinstance(value, Mapping) else {}
    raw = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def governance_hint_source_sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def mutate_governance_hint_text(
    text: str,
    *,
    source_path: str,
    action: str,
    event: Mapping[str, Any] | None = None,
    nodes: Iterable[dict[str, Any]] = (),
    path: str = "",
    role: str = "",
    target_node_id: str = "",
    target_module: str = "",
    expected_source_sha256: str = "",
    expected_envelope_sha256: str = "",
    rollback_envelope: Mapping[str, Any] | None = None,
    rollback_text: str | None = None,
) -> dict[str, Any]:
    """Plan one deterministic governance-hint source mutation.

    JSON sources are changed only through the reserved top-level envelope.
    Every operation preserves all non-reserved root values and emits stable
    sorted/indented JSON with one trailing newline.
    """
    source = normalize_relpath("", source_path)
    op = str(action or "").strip().lower()
    if op == "repair":
        op = "stabilize"
    if op not in {"attach", "unbind", "stabilize", "withdraw", "rollback"}:
        raise GovernanceHintMutationError(
            "action must be attach, unbind, stabilize, withdraw, or rollback"
        )

    before = text or ""
    source_hash_before = governance_hint_source_sha256(before)
    _assert_expected_hash(
        "expected_source_sha256",
        expected_source_sha256,
        source_hash_before,
    )
    if Path(source).suffix.lower() == ".json":
        result = _mutate_json_governance_hint_text(
            before,
            source_path=source,
            action=op,
            event=event,
            nodes=nodes,
            path=path,
            role=role,
            target_node_id=target_node_id,
            target_module=target_module,
            expected_envelope_sha256=expected_envelope_sha256,
            rollback_envelope=rollback_envelope,
            rollback_text=rollback_text,
        )
    else:
        if expected_envelope_sha256:
            _assert_expected_hash(
                "expected_envelope_sha256",
                expected_envelope_sha256,
                governance_hints_envelope_sha256({}),
            )
        result = _mutate_legacy_governance_hint_text(
            before,
            source_path=source,
            action=op,
            event=event,
            nodes=nodes,
            path=path,
            role=role,
            target_node_id=target_node_id,
            target_module=target_module,
            rollback_text=rollback_text,
        )

    after = str(result.get("text") or "")
    result.update(
        {
            "schema_version": "governance_hint_mutation.v1",
            "action": op,
            "source_path": source,
            "changed": after != before,
            "source_sha256_before": source_hash_before,
            "source_sha256_after": governance_hint_source_sha256(after),
        }
    )
    return result


def mutate_governance_hint_file(
    source_file: str | Path,
    *,
    source_path: str,
    action: str,
    dry_run: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Apply the shared mutation plan with one same-directory atomic replace."""
    file_path = Path(source_file)
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise GovernanceHintMutationError(
            f"file is not utf-8 text: {source_path}"
        ) from exc
    result = mutate_governance_hint_text(
        text,
        source_path=source_path,
        action=action,
        **kwargs,
    )
    result["dry_run"] = bool(dry_run)
    result["written"] = bool(result["changed"] and not dry_run)
    if result["written"]:
        _atomic_write_text(file_path, str(result["text"]))
    return result


def _mutate_json_governance_hint_text(
    text: str,
    *,
    source_path: str,
    action: str,
    event: Mapping[str, Any] | None,
    nodes: Iterable[dict[str, Any]],
    path: str,
    role: str,
    target_node_id: str,
    target_module: str,
    expected_envelope_sha256: str,
    rollback_envelope: Mapping[str, Any] | None,
    rollback_text: str | None,
) -> dict[str, Any]:
    try:
        root = json.loads(text or "")
    except (json.JSONDecodeError, TypeError) as exc:
        raise GovernanceHintMutationError("invalid JSON source; no hints were mutated") from exc
    if not isinstance(root, dict):
        raise GovernanceHintMutationError("JSON source root must be an object")

    original_envelope = root.get(GOVERNANCE_HINTS_ROOT_KEY)
    envelope_hash_before = governance_hints_envelope_sha256(original_envelope)
    _assert_expected_hash(
        "expected_envelope_sha256",
        expected_envelope_sha256,
        envelope_hash_before,
    )

    repaired_count = 0
    withdrawn_count = 0
    unchanged_count = 0
    errors: list[dict[str, str]] = []
    mutated = False
    out = deepcopy(root)
    if action in {"attach", "unbind"}:
        if original_envelope is None:
            envelope: dict[str, Any] = {
                "schema_version": GOVERNANCE_HINTS_SCHEMA_VERSION,
                "asset_binding_events": [],
            }
        else:
            envelope = normalize_governance_hints_envelope(original_envelope)
        proposed = _canonical_binding_event(
            event or {},
            source_path=source_path,
            operation="bind" if action == "attach" else "unbind",
        )
        events = list(envelope.get("asset_binding_events") or [])
        if _last_matching_event_is_equivalent(
            events,
            proposed,
            source_path=source_path,
        ):
            unchanged_count = 1
        else:
            events.append(proposed)
            envelope["asset_binding_events"] = events
            mutated = True
        out[GOVERNANCE_HINTS_ROOT_KEY] = envelope
    elif action in {"stabilize", "withdraw"}:
        if original_envelope is None:
            unchanged_count = 1
        else:
            envelope = normalize_governance_hints_envelope(original_envelope)
            by_id, by_module, by_title, all_nodes = _node_indexes(nodes)
            matcher = {
                "path": _filter_path(source_path, path),
                "field": _ROLE_TO_FIELD.get(str(role or "").strip().lower(), ""),
                "target_node_id": str(target_node_id or "").strip(),
                "target_module": str(target_module or "").strip(),
            }
            rewritten, stats = _rewrite_hint_payload(
                envelope,
                source_path=source_path,
                by_id=by_id,
                by_module=by_module,
                by_title=by_title,
                nodes=all_nodes,
                action=action,
                matcher=matcher,
            )
            repaired_count = int(stats.get("repaired") or 0)
            withdrawn_count = int(stats.get("withdrawn") or 0)
            unchanged_count = int(stats.get("unchanged") or 0)
            errors = list(stats.get("errors") or [])
            mutated = bool(repaired_count or withdrawn_count)
            next_envelope = rewritten or {
                "schema_version": GOVERNANCE_HINTS_SCHEMA_VERSION,
            }
            next_envelope.setdefault("asset_binding_events", [])
            out[GOVERNANCE_HINTS_ROOT_KEY] = next_envelope
    else:
        restored = rollback_envelope
        if rollback_text is not None:
            try:
                rollback_root = json.loads(rollback_text)
            except (json.JSONDecodeError, TypeError) as exc:
                raise GovernanceHintMutationError("rollback_text must be valid JSON") from exc
            if not isinstance(rollback_root, dict):
                raise GovernanceHintMutationError("rollback_text root must be an object")
            candidate = rollback_root.get(GOVERNANCE_HINTS_ROOT_KEY)
            restored = candidate if isinstance(candidate, Mapping) else None
        previous = out.get(GOVERNANCE_HINTS_ROOT_KEY)
        if restored is None:
            out.pop(GOVERNANCE_HINTS_ROOT_KEY, None)
        else:
            out[GOVERNANCE_HINTS_ROOT_KEY] = normalize_governance_hints_envelope(restored)
        mutated = previous != out.get(GOVERNANCE_HINTS_ROOT_KEY)

    rendered = (
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if mutated
        else text
    )
    envelope_after = out.get(GOVERNANCE_HINTS_ROOT_KEY)
    return {
        "text": rendered,
        "repaired_count": repaired_count,
        "withdrawn_count": withdrawn_count,
        "unchanged_count": unchanged_count,
        "error_count": len(errors),
        "errors": errors,
        "envelope_sha256_before": envelope_hash_before,
        "envelope_sha256_after": governance_hints_envelope_sha256(envelope_after),
    }


def _mutate_legacy_governance_hint_text(
    text: str,
    *,
    source_path: str,
    action: str,
    event: Mapping[str, Any] | None,
    nodes: Iterable[dict[str, Any]],
    path: str,
    role: str,
    target_node_id: str,
    target_module: str,
    rollback_text: str | None,
) -> dict[str, Any]:
    if action in {"stabilize", "withdraw"}:
        return rewrite_governance_hint_text(
            text,
            source_path=source_path,
            nodes=nodes,
            action=action,
            path=path,
            role=role,
            target_node_id=target_node_id,
            target_module=target_module,
        )
    if action == "rollback":
        if rollback_text is None:
            raise GovernanceHintMutationError("rollback_text is required for legacy sources")
        return _rewrite_result(
            rollback_text,
            action=action,
            changed=rollback_text != text,
        )

    proposed = _canonical_binding_event(
        event or {},
        source_path=source_path,
        operation="bind" if action == "attach" else "unbind",
    )
    if proposed.get("path") == ".":
        proposed["path"] = source_path
    existing = parse_governance_hint_bindings(text, source_path=source_path)
    proposed_hint = _binding_hint_from_item(proposed, source_path=source_path)
    if proposed_hint:
        for existing_hint in reversed(existing):
            if _effective_binding_key(existing_hint) != _effective_binding_key(proposed_hint):
                continue
            if _same_effective_binding(existing_hint, proposed_hint):
                return _rewrite_result(text, action=action, unchanged_count=1)
            break
    comment = render_governance_hint_comment(
        source_path,
        {"asset_binding_event": proposed},
    )
    if not comment:
        raise GovernanceHintMutationError(
            f"file type does not support direct governance-hint comments: {source_path}"
        )
    if action == "attach":
        rendered = comment.rstrip() + "\n\n" + text
    else:
        prefix = text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        rendered = prefix + comment.rstrip() + "\n"
    result = _rewrite_result(rendered, action=action, changed=rendered != text)
    result["rendered_hint"] = comment
    return result


def _canonical_binding_event(
    event: Mapping[str, Any],
    *,
    source_path: str,
    operation: str,
) -> dict[str, Any]:
    out = deepcopy(dict(event))
    out["schema_version"] = ASSET_BINDING_EVENT_SCHEMA_VERSION
    out["operation"] = operation
    raw_path = str(out.get("path") or out.get("file") or out.get("file_path") or "").strip()
    resolved = _resolve_binding_path(raw_path, source_path=source_path)
    out.pop("file", None)
    out.pop("file_path", None)
    out["path"] = "." if resolved == normalize_relpath("", source_path) else resolved
    return out


def _last_matching_event_is_equivalent(
    events: Iterable[Any],
    proposed: Mapping[str, Any],
    *,
    source_path: str,
) -> bool:
    proposed_hint = _binding_hint_from_item(dict(proposed), source_path=source_path)
    if proposed_hint is None:
        raise GovernanceHintMutationError("asset binding event is incomplete")
    for item in reversed(list(events)):
        if not isinstance(item, Mapping):
            continue
        hint = _binding_hint_from_item(dict(item), source_path=source_path)
        if hint is None or _effective_binding_key(hint) != _effective_binding_key(proposed_hint):
            continue
        return _same_effective_binding(hint, proposed_hint)
    return False


def _same_effective_binding(left: BindingHint, right: BindingHint) -> bool:
    return _binding_hint_signature(left) == _binding_hint_signature(right)


def _effective_binding_key(hint: BindingHint) -> tuple[str, str]:
    return (normalize_relpath("", hint.path or hint.source_path), hint.field)


def _resolve_binding_path(raw_path: str, *, source_path: str) -> str:
    token = str(raw_path or "").replace("\\", "/").strip()
    if token in {"", ".", "./", "self", "$self"}:
        return normalize_relpath("", source_path)
    return normalize_relpath("", token)


def _filter_path(source_path: str, path: str) -> str:
    if not str(path or "").strip():
        return ""
    return _resolve_binding_path(str(path), source_path=source_path)


def _assert_expected_hash(field: str, expected: str, actual: str) -> None:
    token = str(expected or "").strip().lower()
    if not token:
        return
    if not token.startswith("sha256:"):
        token = "sha256:" + token
    if token != str(actual or "").strip().lower():
        raise GovernanceHintCASMismatch(f"{field} mismatch")


def _atomic_write_text(path: Path, text: str) -> None:
    mode = path.stat().st_mode
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def audit_governance_hint_bindings(
    hints: Iterable[BindingHint],
    nodes: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Classify hint target stability against a graph node set.

    `target_node_id` is useful immediate evidence, but it is not durable across
    full graph rebuilds.  Stable targets (`target_module`/`target_title`) win
    when present; node-id-only or conflicting hints are repair candidates.
    """

    by_id, by_module, by_title, all_nodes = _node_indexes(nodes)
    items: list[dict[str, Any]] = []
    by_status: dict[str, int] = {}
    for hint in hints:
        node_target = by_id.get(hint.target_node_id) if hint.target_node_id else None
        stable_target, stable_state = _resolve_stable_target(
            hint,
            by_module=by_module,
            by_title=by_title,
            nodes=all_nodes,
        )
        resolved = _resolve_target(
            hint,
            by_id=by_id,
            by_module=by_module,
            by_title=by_title,
            nodes=all_nodes,
        )
        if stable_target is not None and node_target is not None and stable_target is not node_target:
            status = "target_conflict"
        elif stable_target is not None:
            status = "stable"
        elif stable_state == "ambiguous":
            status = "target_ambiguous"
        elif node_target is not None:
            status = "node_id_only"
        else:
            status = "target_missing"
        needs_repair = status in {"node_id_only", "target_conflict", "target_missing", "target_ambiguous"}
        by_status[status] = by_status.get(status, 0) + 1
        items.append({
            "source_path": hint.source_path,
            "path": hint.path,
            "field": hint.field,
            "target_node_id": hint.target_node_id,
            "target_module": hint.target_module,
            "target_title": hint.target_title,
            "target_area_key": hint.target_area_key,
            "target_subsystem_key": hint.target_subsystem_key,
            "target_asset_key": hint.target_asset_key,
            "status": status,
            "needs_repair": needs_repair,
            "resolved_target": _node_identity(resolved),
            "node_id_target": _node_identity(node_target),
            "stable_target": _node_identity(stable_target),
        })
    return {
        "hint_count": len(items),
        "needs_repair_count": sum(1 for item in items if item["needs_repair"]),
        "by_status": dict(sorted(by_status.items())),
        "items": items,
    }


def rewrite_governance_hint_text(
    text: str,
    *,
    source_path: str,
    nodes: Iterable[dict[str, Any]],
    action: str,
    path: str = "",
    role: str = "",
    target_node_id: str = "",
    target_module: str = "",
) -> dict[str, Any]:
    """Repair or withdraw source-controlled governance hint comments.

    The function only rewrites text.  Callers are responsible for writing the
    file, committing the source change, and running reconcile so the graph is
    updated by projection.
    """

    op = str(action or "").strip().lower()
    if op not in {"stabilize", "withdraw"}:
        raise ValueError("action must be stabilize or withdraw")
    by_id, by_module, by_title, all_nodes = _node_indexes(nodes)
    matcher = {
        "path": normalize_relpath("", path),
        "field": _ROLE_TO_FIELD.get(str(role or "").strip().lower(), ""),
        "target_node_id": str(target_node_id or "").strip(),
        "target_module": str(target_module or "").strip(),
    }
    source = normalize_relpath("", source_path)
    matches = _hint_comment_matches(text or "")
    if not matches:
        return _rewrite_result(text or "", action=op)

    chunks: list[str] = []
    cursor = 0
    repaired = 0
    withdrawn = 0
    unchanged = 0
    errors: list[dict[str, str]] = []
    for match in matches:
        chunks.append((text or "")[cursor:match.start])
        cursor = match.end
        try:
            payload = json.loads(match.raw_json.strip())
        except json.JSONDecodeError as exc:
            chunks.append((text or "")[match.start:match.end])
            errors.append({"source_path": source, "reason": "invalid_json", "error": str(exc)})
            continue
        rewritten, stats = _rewrite_hint_payload(
            payload,
            source_path=source,
            by_id=by_id,
            by_module=by_module,
            by_title=by_title,
            nodes=all_nodes,
            action=op,
            matcher=matcher,
        )
        repaired += stats["repaired"]
        withdrawn += stats["withdrawn"]
        unchanged += stats["unchanged"]
        errors.extend(stats["errors"])
        if rewritten is None:
            # Remove the complete hint comment and one following blank line.
            while cursor < len(text or "") and (text or "")[cursor] in {" ", "\t"}:
                cursor += 1
            if (text or "")[cursor:cursor + 2] == "\n\n":
                cursor += 2
            elif cursor < len(text or "") and (text or "")[cursor] == "\n":
                cursor += 1
            continue
        chunks.append(_render_hint_payload_for_style(rewritten, style=match.style))
    chunks.append((text or "")[cursor:])
    new_text = "".join(chunks)
    return _rewrite_result(
        new_text,
        action=op,
        changed=new_text != (text or ""),
        repaired_count=repaired,
        withdrawn_count=withdrawn,
        unchanged_count=unchanged,
        errors=errors,
    )


def load_governance_hint_bindings(project_root: str | Path) -> list[BindingHint]:
    """Scan non-excluded text-like project files for binding hints."""
    root = Path(project_root).resolve()
    hints: list[BindingHint] = []
    if not root.exists():
        return hints
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if DEFAULT_LANGUAGE_POLICY.is_excluded_path(rel):
            continue
        if path.suffix.lower() not in {
            ".md",
            ".mdx",
            ".txt",
            ".rst",
            ".adoc",
            ".yaml",
            ".yml",
            ".json",
            ".py",
            ".pyw",
            ".sh",
            ".bash",
            ".ps1",
            ".toml",
            ".ini",
            ".cfg",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".mjs",
            ".cjs",
        }:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hints.extend(parse_governance_hint_bindings(text, source_path=rel))
    return hints


def apply_binding_hints_to_graph_nodes(
    project_root: str | Path,
    nodes: list[dict[str, Any]],
    *,
    hints: Iterable[BindingHint] | None = None,
) -> dict[str, Any]:
    """Mutate nodes by applying orphan-only binding hints.

    Bind/unbind hints are replayed in source order. Bind remains conservative:
    a path is eligible only when it exists and is not already present in any
    node's primary/secondary/test/config fields. Unbind is a tombstone command:
    it removes only the targeted materialized binding from the replay result
    and leaves the source-controlled command as durable evidence.
    """
    root = Path(project_root).resolve()
    binding_hints = list(hints) if hints is not None else load_governance_hint_bindings(root)
    effective_hints = _last_event_wins(binding_hints)
    by_id, by_module, by_title, all_nodes = _node_indexes(nodes)
    already_bound = _bound_paths(nodes)
    applied: list[dict[str, str]] = []
    removed: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    for hint in effective_hints:
        rel = normalize_relpath(root, hint.path or hint.source_path)
        if not rel:
            skipped.append({"path": rel, "reason": "missing_path", "source_path": hint.source_path})
            continue
        if hint.field not in {"secondary", "test", "config"}:
            skipped.append({"path": rel, "reason": "unsupported_role", "source_path": hint.source_path})
            continue
        target = _resolve_target(
            hint,
            by_id=by_id,
            by_module=by_module,
            by_title=by_title,
            nodes=all_nodes,
        )
        if target is None:
            skipped.append({"path": rel, "reason": "target_missing", "source_path": hint.source_path})
            continue
        if hint.operation == "unbind":
            target_node_id = str(target.get("id") or target.get("node_id") or "")
            if _remove_node_binding_path(target, rel, hint.field):
                already_bound = _bound_paths(nodes)
                metadata = target.setdefault("metadata", {})
                if isinstance(metadata, dict):
                    entries = metadata.setdefault("governance_hint_bindings", [])
                    if isinstance(entries, list):
                        entries.append({
                            "operation": "unbind",
                            "path": rel,
                            "field": hint.field,
                            "source_path": hint.source_path,
                        })
                removed.append({
                    "path": rel,
                    "field": hint.field,
                    "target_node_id": target_node_id,
                    "source_path": hint.source_path,
                })
            else:
                skipped.append({
                    "path": rel,
                    "field": hint.field,
                    "target_node_id": target_node_id,
                    "reason": "binding_not_present",
                    "source_path": hint.source_path,
                })
            continue
        if hint.operation != "bind":
            skipped.append({
                "path": rel,
                "field": hint.field,
                "reason": "unsupported_operation",
                "source_path": hint.source_path,
            })
            continue
        if rel in already_bound:
            skipped.append({"path": rel, "reason": "already_bound", "source_path": hint.source_path})
            continue
        if DEFAULT_LANGUAGE_POLICY.is_index_doc_path(rel):
            skipped.append({"path": rel, "reason": "index_doc_deferred", "source_path": hint.source_path})
            continue
        if not (root / rel).exists():
            skipped.append({"path": rel, "reason": "file_missing", "source_path": hint.source_path})
            continue
        values = _path_list(target.get(hint.field))
        if rel not in values:
            values.append(rel)
            target[hint.field] = sorted(set(values))
        metadata = target.setdefault("metadata", {})
        if isinstance(metadata, dict):
            entries = metadata.setdefault("governance_hint_bindings", [])
            if isinstance(entries, list):
                entries.append({
                    "operation": "bind",
                    "path": rel,
                    "field": hint.field,
                    "source_path": hint.source_path,
                })
            _prune_asset_binding_candidate(metadata, rel, hint.field)
        already_bound.add(rel)
        applied.append({
            "path": rel,
            "field": hint.field,
            "target_node_id": str(target.get("id") or target.get("node_id") or ""),
            "source_path": hint.source_path,
        })

    return {
        "hint_count": len(binding_hints),
        "effective_hint_count": len(effective_hints),
        "applied_count": len(applied),
        "removed_count": len(removed),
        "skipped_count": len(skipped),
        "applied": applied,
        "removed": removed,
        "skipped": skipped[:50],
    }


def _binding_hint_key(hint: BindingHint) -> tuple[str, str]:
    return (
        normalize_relpath("", hint.source_path or hint.path),
        normalize_relpath("", hint.path or hint.source_path),
    )


def _binding_hint_signature(hint: BindingHint) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        hint.operation,
        hint.field,
        hint.target_node_id,
        hint.target_module,
        hint.target_title,
        hint.target_area_key,
        hint.target_subsystem_key,
        hint.target_asset_key,
    )


def _binding_hints_by_key(hints: Iterable[BindingHint]) -> dict[tuple[str, str], BindingHint]:
    return {_binding_hint_key(hint): hint for hint in hints}


def _last_event_wins(hints: Iterable[BindingHint]) -> list[BindingHint]:
    """Return effective binding events while preserving each last-event position."""
    ordered = list(hints)
    last_indexes: dict[tuple[str, str], int] = {}
    for index, hint in enumerate(ordered):
        last_indexes[_effective_binding_key(hint)] = index
    return [
        hint
        for index, hint in enumerate(ordered)
        if last_indexes[_effective_binding_key(hint)] == index
    ]


def normalized_governance_binding_hints(
    project_root: str | Path,
) -> list[dict[str, str]]:
    """Return deterministic normalized source hints for graph fingerprinting."""
    return [binding_hint_to_dict(hint) for hint in load_governance_hint_bindings(project_root)]


def _bindings_from_governance_hints_envelope(
    envelope: Any,
    *,
    source_path: str,
) -> list[BindingHint]:
    try:
        normalized = normalize_governance_hints_envelope(envelope)
    except GovernanceHintMutationError:
        return []
    hints: list[BindingHint] = []
    for item in normalized.get("asset_binding_events") or []:
        if not isinstance(item, dict):
            continue
        hint = _binding_hint_from_item(item, source_path=source_path)
        if hint:
            hints.append(hint)
    return hints


def _bindings_from_payload(payload: Any, *, source_path: str) -> list[BindingHint]:
    if isinstance(payload, list):
        out: list[BindingHint] = []
        for item in payload:
            out.extend(_bindings_from_payload(item, source_path=source_path))
        return out
    if not isinstance(payload, dict):
        return []

    candidates: list[Any] = []
    if isinstance(payload.get("bindings"), list):
        candidates.extend(payload.get("bindings") or [])
    if isinstance(payload.get("asset_binding_events"), list):
        candidates.extend(payload.get("asset_binding_events") or [])
    if isinstance(payload.get("asset_binding_event"), dict):
        candidates.append(payload.get("asset_binding_event"))
    if isinstance(payload.get("attach_to_node"), dict):
        item = dict(payload.get("attach_to_node") or {})
        item.setdefault("operation", "bind")
        candidates.append(item)
    if isinstance(payload.get("binding"), dict):
        candidates.append(payload.get("binding"))
    if _looks_like_binding(payload):
        candidates.append(payload)

    hints: list[BindingHint] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        hint = _binding_hint_from_item(item, source_path=source_path)
        if hint:
            hints.append(hint)
    return hints


def _hint_comment_matches(text: str) -> list[_HintCommentMatch]:
    matches: list[_HintCommentMatch] = []
    for match in _HINT_RE.finditer(text or ""):
        matches.append(_HintCommentMatch(
            start=match.start(),
            end=match.end(),
            raw_json=match.group(1).strip(),
            style="html",
        ))
    for match in _LINE_HINT_RE.finditer(text or ""):
        matches.append(_HintCommentMatch(
            start=match.start(),
            end=match.end(),
            raw_json=match.group(1).strip(),
            style=_line_comment_style(match.group(0)),
        ))
    return sorted(matches, key=lambda item: item.start)


def _line_comment_style(raw: str) -> str:
    stripped = raw.lstrip()
    if stripped.startswith("//"):
        return "slash"
    return "hash"


def _render_hint_payload_for_style(payload: Any, *, style: str) -> str:
    body = f"governance-hint {json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    if style == "slash":
        return f"// {body}"
    if style == "hash":
        return f"# {body}"
    return f"<!-- {body} -->"


def _rewrite_result(
    text: str,
    *,
    action: str,
    changed: bool = False,
    repaired_count: int = 0,
    withdrawn_count: int = 0,
    unchanged_count: int = 0,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "changed": changed,
        "repaired_count": repaired_count,
        "withdrawn_count": withdrawn_count,
        "unchanged_count": unchanged_count,
        "error_count": len(errors or []),
        "errors": errors or [],
        "text": text,
    }


def _rewrite_hint_payload(
    payload: Any,
    *,
    source_path: str,
    by_id: dict[str, dict[str, Any]],
    by_module: dict[str, dict[str, Any]],
    by_title: dict[str, dict[str, Any]],
    nodes: list[dict[str, Any]],
    action: str,
    matcher: dict[str, str],
) -> tuple[Any | None, dict[str, Any]]:
    stats = {"repaired": 0, "withdrawn": 0, "unchanged": 0, "errors": []}
    rewritten = _rewrite_payload_value(
        payload,
        source_path=source_path,
        by_id=by_id,
        by_module=by_module,
        by_title=by_title,
        nodes=nodes,
        action=action,
        matcher=matcher,
        stats=stats,
    )
    if _payload_is_empty(rewritten):
        rewritten = None
    return rewritten, stats


def _rewrite_payload_value(
    value: Any,
    *,
    source_path: str,
    by_id: dict[str, dict[str, Any]],
    by_module: dict[str, dict[str, Any]],
    by_title: dict[str, dict[str, Any]],
    nodes: list[dict[str, Any]],
    action: str,
    matcher: dict[str, str],
    stats: dict[str, Any],
) -> Any | None:
    if isinstance(value, list):
        items = [
            _rewrite_payload_value(
                item,
                source_path=source_path,
                by_id=by_id,
                by_module=by_module,
                by_title=by_title,
                nodes=nodes,
                action=action,
                matcher=matcher,
                stats=stats,
            )
            for item in value
        ]
        kept = [item for item in items if not _payload_is_empty(item)]
        return kept
    if not isinstance(value, dict):
        stats["unchanged"] += 1
        return value

    if _looks_like_binding(value):
        return _rewrite_binding_dict(
            value,
            source_path=source_path,
            by_id=by_id,
            by_module=by_module,
            by_title=by_title,
            nodes=nodes,
            action=action,
            matcher=matcher,
            stats=stats,
        )

    changed = False
    out = dict(value)
    for key in ("attach_to_node", "binding"):
        if isinstance(out.get(key), dict):
            rewritten = _rewrite_binding_dict(
                out[key],
                source_path=source_path,
                by_id=by_id,
                by_module=by_module,
                by_title=by_title,
                nodes=nodes,
                action=action,
                matcher=matcher,
                stats=stats,
            )
            if rewritten is None:
                out.pop(key, None)
                changed = True
            elif rewritten != out[key]:
                out[key] = rewritten
                changed = True
    if isinstance(out.get("asset_binding_event"), dict):
        rewritten = _rewrite_binding_dict(
            out["asset_binding_event"],
            source_path=source_path,
            by_id=by_id,
            by_module=by_module,
            by_title=by_title,
            nodes=nodes,
            action=action,
            matcher=matcher,
            stats=stats,
        )
        if rewritten is None:
            out.pop("asset_binding_event", None)
            changed = True
        elif rewritten != out["asset_binding_event"]:
            out["asset_binding_event"] = rewritten
            changed = True
    if isinstance(out.get("bindings"), list):
        original = list(out["bindings"])
        rewritten_items = [
            _rewrite_payload_value(
                item,
                source_path=source_path,
                by_id=by_id,
                by_module=by_module,
                by_title=by_title,
                nodes=nodes,
                action=action,
                matcher=matcher,
                stats=stats,
            )
            for item in original
        ]
        kept = [item for item in rewritten_items if not _payload_is_empty(item)]
        if kept:
            out["bindings"] = kept
        else:
            out.pop("bindings", None)
        changed = changed or kept != original
    if isinstance(out.get("asset_binding_events"), list):
        original = list(out["asset_binding_events"])
        rewritten_items = [
            _rewrite_payload_value(
                item,
                source_path=source_path,
                by_id=by_id,
                by_module=by_module,
                by_title=by_title,
                nodes=nodes,
                action=action,
                matcher=matcher,
                stats=stats,
            )
            for item in original
        ]
        kept = [item for item in rewritten_items if not _payload_is_empty(item)]
        if kept:
            out["asset_binding_events"] = kept
        else:
            out.pop("asset_binding_events", None)
        changed = changed or kept != original
    if not changed and not any(
        key in value
        for key in ("attach_to_node", "binding", "bindings", "asset_binding_event", "asset_binding_events")
    ):
        stats["unchanged"] += 1
    return None if _payload_is_empty(out) else out


def _rewrite_binding_dict(
    item: dict[str, Any],
    *,
    source_path: str,
    by_id: dict[str, dict[str, Any]],
    by_module: dict[str, dict[str, Any]],
    by_title: dict[str, dict[str, Any]],
    nodes: list[dict[str, Any]],
    action: str,
    matcher: dict[str, str],
    stats: dict[str, Any],
) -> dict[str, Any] | None:
    hint = _binding_hint_from_item(item, source_path=source_path)
    if hint is None or not _hint_matches_filter(hint, matcher):
        stats["unchanged"] += 1
        return dict(item)
    if action == "withdraw":
        stats["withdrawn"] += 1
        return None

    target = _resolve_target(
        hint,
        by_id=by_id,
        by_module=by_module,
        by_title=by_title,
        nodes=nodes,
    )
    if target is None:
        stats["errors"].append({
            "source_path": source_path,
            "path": hint.path,
            "reason": "target_missing",
        })
        return dict(item)
    module = _node_module(target)
    title = str(target.get("title") or "")
    node_id = str(target.get("id") or target.get("node_id") or "")
    metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
    area_key = str(target.get("area_key") or metadata.get("area_key") or "")
    subsystem_key = str(target.get("subsystem_key") or metadata.get("subsystem_key") or "")
    asset_key = str(target.get("asset_key") or metadata.get("asset_key") or "")
    out = dict(item)
    changed = False
    if module and out.get("target_module") != module:
        out["target_module"] = module
        changed = True
    if area_key and out.get("target_area_key") != area_key:
        out["target_area_key"] = area_key
        changed = True
    if subsystem_key and out.get("target_subsystem_key") != subsystem_key:
        out["target_subsystem_key"] = subsystem_key
        changed = True
    if asset_key and out.get("target_asset_key") != asset_key:
        out["target_asset_key"] = asset_key
        changed = True
    if title and out.get("target_title") != title:
        out["target_title"] = title
        changed = True
    if node_id and out.get("target_node_id") != node_id:
        out["target_node_id"] = node_id
        changed = True
    if changed:
        stats["repaired"] += 1
    else:
        stats["unchanged"] += 1
    return out


def _binding_hint_from_item(item: dict[str, Any], *, source_path: str) -> BindingHint | None:
    operation = str(item.get("operation") or item.get("action") or "bind").strip().lower()
    if operation in {"attach", "add", "bind_to_node"}:
        operation = "bind"
    if operation in {"remove", "detach", "withdraw"}:
        operation = "unbind"
    role = str(
        item.get("role") or item.get("binding") or item.get("attachment_role") or "doc"
    ).strip().lower()
    field = _ROLE_TO_FIELD.get(role, "")
    path = _resolve_binding_path(
        str(item.get("path") or item.get("file") or item.get("file_path") or ""),
        source_path=source_path,
    )
    target_node_id = str(
        item.get("node_id") or item.get("target_node_id") or item.get("target") or ""
    ).strip()
    target_module = str(item.get("module") or item.get("target_module") or "").strip()
    target_title = str(item.get("title") or item.get("target_title") or "").strip()
    target_area_key = str(item.get("area_key") or item.get("target_area_key") or "").strip()
    target_subsystem_key = str(
        item.get("subsystem_key") or item.get("target_subsystem_key") or ""
    ).strip()
    target_asset_key = str(item.get("asset_key") or item.get("target_asset_key") or "").strip()
    if field and path and (
        target_node_id
        or target_module
        or target_title
        or target_area_key
        or target_subsystem_key
        or target_asset_key
    ):
        return BindingHint(
            source_path=source_path,
            path=path,
            field=field,
            operation=operation,
            target_node_id=target_node_id,
            target_module=target_module,
            target_title=target_title,
            target_area_key=target_area_key,
            target_subsystem_key=target_subsystem_key,
            target_asset_key=target_asset_key,
        )
    return None


def _hint_matches_filter(hint: BindingHint, matcher: dict[str, str]) -> bool:
    if matcher.get("path") and normalize_relpath("", hint.path) != matcher["path"]:
        return False
    if matcher.get("field") and hint.field != matcher["field"]:
        return False
    if matcher.get("target_node_id") and hint.target_node_id != matcher["target_node_id"]:
        return False
    if matcher.get("target_module") and hint.target_module != matcher["target_module"]:
        return False
    return True


def _payload_is_empty(value: Any) -> bool:
    if value is None:
        return True
    if value == {} or value == []:
        return True
    return False


def _looks_like_binding(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("node_id")
        or payload.get("target_node_id")
        or payload.get("module")
        or payload.get("target_module")
        or payload.get("area_key")
        or payload.get("target_area_key")
        or payload.get("subsystem_key")
        or payload.get("target_subsystem_key")
        or payload.get("asset_key")
        or payload.get("target_asset_key")
    ) and bool(payload.get("path") or payload.get("file") or payload.get("file_path"))


def _node_indexes(
    nodes: Iterable[dict[str, Any]],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
]:
    node_list = list(nodes)
    by_id: dict[str, dict[str, Any]] = {}
    by_module: dict[str, dict[str, Any]] = {}
    by_title: dict[str, dict[str, Any]] = {}
    for node in node_list:
        node_id = str(node.get("id") or node.get("node_id") or "")
        if node_id:
            by_id[node_id] = node
        title = str(node.get("title") or "")
        if title:
            by_title.setdefault(title, node)
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        module = str(node.get("module") or metadata.get("module") or "")
        if module:
            by_module[module] = node
    return by_id, by_module, by_title, node_list


def _resolve_target(
    hint: BindingHint,
    *,
    by_id: dict[str, dict[str, Any]],
    by_module: dict[str, dict[str, Any]],
    by_title: dict[str, dict[str, Any]],
    nodes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    stable_target, _stable_state = _resolve_stable_target(
        hint,
        by_module=by_module,
        by_title=by_title,
        nodes=nodes,
    )
    if stable_target is not None:
        return stable_target
    if hint.target_node_id and hint.target_node_id in by_id:
        return by_id[hint.target_node_id]
    if hint.target_node_id:
        module_target, _module_state = _unique_node_by_module(nodes, hint.target_node_id)
        if module_target is not None:
            return module_target
    if hint.target_node_id and hint.target_node_id in by_title:
        return by_title[hint.target_node_id]
    return None


def _resolve_stable_target(
    hint: BindingHint,
    *,
    by_module: dict[str, dict[str, Any]],
    by_title: dict[str, dict[str, Any]],
    nodes: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    stable_fields = {
        "module": hint.target_module,
        "title": hint.target_title,
        "area_key": hint.target_area_key,
        "subsystem_key": hint.target_subsystem_key,
        "asset_key": hint.target_asset_key,
    }
    if not any(stable_fields.values()):
        return None, "none"
    if hint.target_module:
        module_target, module_state = _unique_node_by_module(nodes, hint.target_module)
        if module_target is not None:
            return module_target, "matched"
        if module_state == "ambiguous":
            candidates = [
                node for node in nodes
                if _node_matches_stable_fields(node, stable_fields)
            ]
            if len(candidates) == 1:
                return candidates[0], "matched"
            return None, "ambiguous"

    candidates = [
        node for node in nodes
        if _node_matches_stable_fields(node, stable_fields)
    ]
    if len(candidates) == 1:
        return candidates[0], "matched"
    if len(candidates) > 1:
        return None, "ambiguous"
    if hint.target_title and hint.target_title in by_title:
        # Title-only matches are trusted only when the title is globally unique.
        title_candidates = [
            node for node in nodes
            if str(node.get("title") or "") == hint.target_title
        ]
        if len(title_candidates) == 1:
            return title_candidates[0], "matched"
        if len(title_candidates) > 1:
            return None, "ambiguous"
    return None, "missing"


def _unique_node_by_module(
    nodes: Iterable[dict[str, Any]],
    module: str,
) -> tuple[dict[str, Any] | None, str]:
    if not module:
        return None, "none"
    candidates = [
        node for node in nodes
        if _node_module(node) == module
    ]
    if len(candidates) == 1:
        return candidates[0], "matched"
    if len(candidates) > 1:
        return None, "ambiguous"
    return None, "missing"


def _node_matches_stable_fields(node: dict[str, Any], fields: dict[str, str]) -> bool:
    if fields.get("module") and _node_module(node) != fields["module"]:
        return False
    if fields.get("title") and str(node.get("title") or "") != fields["title"]:
        return False
    if fields.get("area_key") and _node_metadata_value(node, "area_key") != fields["area_key"]:
        return False
    if (
        fields.get("subsystem_key")
        and _node_metadata_value(node, "subsystem_key") != fields["subsystem_key"]
    ):
        return False
    if fields.get("asset_key") and _node_metadata_value(node, "asset_key") != fields["asset_key"]:
        return False
    return True


def _node_metadata_value(node: dict[str, Any] | None, key: str) -> str:
    if not node:
        return ""
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    return str(node.get(key) or metadata.get(key) or "")


def _node_module(node: dict[str, Any] | None) -> str:
    if not node:
        return ""
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    return str(node.get("module") or metadata.get("module") or "")


def _node_identity(node: dict[str, Any] | None) -> dict[str, Any]:
    if not node:
        return {}
    return {
        "node_id": str(node.get("id") or node.get("node_id") or ""),
        "title": str(node.get("title") or ""),
        "module": _node_module(node),
        "area_key": _node_metadata_value(node, "area_key"),
        "subsystem_key": _node_metadata_value(node, "subsystem_key"),
        "asset_key": _node_metadata_value(node, "asset_key"),
        "primary_files": _path_list(node.get("primary") or node.get("primary_files")),
    }


def _prune_asset_binding_candidate(metadata: dict[str, Any], path: str, field: str) -> None:
    asset_kind = _FIELD_TO_ASSET_KIND.get(field, "")
    if not asset_kind:
        return
    rel = normalize_relpath("", path)
    candidates = metadata.get("asset_binding_candidates")
    if isinstance(candidates, list):
        kept = [
            item for item in candidates
            if not (
                isinstance(item, dict)
                and normalize_relpath("", str(item.get("asset_path") or "")) == rel
                and str(item.get("asset_kind") or "") == asset_kind
            )
        ]
        metadata["asset_binding_candidates"] = kept
    if asset_kind == "doc":
        docs = [item for item in _path_list(metadata.get("candidate_doc_files")) if item != rel]
        metadata["candidate_doc_files"] = docs
    elif asset_kind == "test":
        tests = [item for item in _path_list(metadata.get("weak_test_files")) if item != rel]
        metadata["weak_test_files"] = tests


def _remove_node_binding_path(node: dict[str, Any], path: str, field: str) -> bool:
    rel = normalize_relpath("", path)
    if not rel:
        return False
    changed = False
    aliases = {
        "secondary": ("secondary", "secondary_files"),
        "test": ("test", "tests", "test_files"),
        "config": ("config", "config_files"),
    }.get(field, (field,))
    for alias in aliases:
        values = _path_list(node.get(alias))
        if rel in values:
            node[alias] = [item for item in values if item != rel]
            changed = True
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    if field == "config" and isinstance(metadata, dict):
        values = _path_list(metadata.get("config_files"))
        if rel in values:
            metadata["config_files"] = [item for item in values if item != rel]
            changed = True
    return changed


def _path_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return [normalize_relpath("", str(item)) for item in values if normalize_relpath("", str(item))]


def _bound_paths(nodes: Iterable[dict[str, Any]]) -> set[str]:
    bound: set[str] = set()
    for node in nodes:
        for key in (
            "primary",
            "primary_files",
            "primary_file",
            "secondary",
            "secondary_files",
            "test",
            "tests",
            "test_files",
            "config",
            "config_files",
        ):
            bound.update(_path_list(node.get(key)))
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        bound.update(_path_list(metadata.get("config_files")))
    return bound
