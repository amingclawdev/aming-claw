"""Parse governance hints embedded in project files.

MVP scope: attach currently-unbound doc/test/config files to existing graph
nodes. Hints are intentionally source-controlled evidence; they are applied
during reconcile instead of mutating graph DB state directly.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .language_policy import DEFAULT_LANGUAGE_POLICY


_HINT_RE = re.compile(r"<!--\s*governance-hint\s*([\s\S]*?)\s*-->", re.IGNORECASE)

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


@dataclass(frozen=True)
class BindingHint:
    source_path: str
    path: str
    field: str
    target_node_id: str = ""
    target_module: str = ""
    target_title: str = ""


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
    """Return binding hints from governance-hint HTML comments."""
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
    return hints


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

    A path is eligible only when it exists and is not already present in any
    node's primary/secondary/test/config fields. This keeps the MVP from
    moving or overwriting existing bindings.
    """
    root = Path(project_root).resolve()
    binding_hints = list(hints) if hints is not None else load_governance_hint_bindings(root)
    by_id, by_module, by_title = _node_indexes(nodes)
    already_bound = _bound_paths(nodes)
    applied: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    for hint in binding_hints:
        rel = normalize_relpath(root, hint.path or hint.source_path)
        if not rel:
            skipped.append({"path": rel, "reason": "missing_path", "source_path": hint.source_path})
            continue
        if hint.field not in {"secondary", "test", "config"}:
            skipped.append({"path": rel, "reason": "unsupported_role", "source_path": hint.source_path})
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
        target = _resolve_target(hint, by_id=by_id, by_module=by_module, by_title=by_title)
        if target is None:
            skipped.append({"path": rel, "reason": "target_missing", "source_path": hint.source_path})
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
                    "path": rel,
                    "field": hint.field,
                    "source_path": hint.source_path,
                })
        already_bound.add(rel)
        applied.append({
            "path": rel,
            "field": hint.field,
            "target_node_id": str(target.get("id") or target.get("node_id") or ""),
            "source_path": hint.source_path,
        })

    return {
        "hint_count": len(binding_hints),
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped[:50],
    }


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
    if isinstance(payload.get("attach_to_node"), dict):
        candidates.append(payload.get("attach_to_node"))
    if isinstance(payload.get("binding"), dict):
        candidates.append(payload.get("binding"))
    if _looks_like_binding(payload):
        candidates.append(payload)

    hints: list[BindingHint] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        role = str(
            item.get("role") or item.get("binding") or item.get("attachment_role") or "doc"
        ).strip().lower()
        field = _ROLE_TO_FIELD.get(role, "")
        path = normalize_relpath(
            "",
            str(item.get("path") or item.get("file") or item.get("file_path") or source_path),
        )
        target_node_id = str(
            item.get("node_id") or item.get("target_node_id") or item.get("target") or ""
        ).strip()
        target_module = str(item.get("module") or item.get("target_module") or "").strip()
        target_title = str(item.get("title") or item.get("target_title") or "").strip()
        if field and path and (target_node_id or target_module or target_title):
            hints.append(BindingHint(
                source_path=source_path,
                path=path,
                field=field,
                target_node_id=target_node_id,
                target_module=target_module,
                target_title=target_title,
            ))
    return hints


def _looks_like_binding(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("node_id")
        or payload.get("target_node_id")
        or payload.get("module")
        or payload.get("target_module")
    ) and bool(payload.get("path") or payload.get("file") or payload.get("file_path"))


def _node_indexes(
    nodes: Iterable[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_module: dict[str, dict[str, Any]] = {}
    by_title: dict[str, dict[str, Any]] = {}
    for node in nodes:
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
    return by_id, by_module, by_title


def _resolve_target(
    hint: BindingHint,
    *,
    by_id: dict[str, dict[str, Any]],
    by_module: dict[str, dict[str, Any]],
    by_title: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if hint.target_node_id and hint.target_node_id in by_id:
        return by_id[hint.target_node_id]
    if hint.target_module and hint.target_module in by_module:
        return by_module[hint.target_module]
    if hint.target_title and hint.target_title in by_title:
        return by_title[hint.target_title]
    if hint.target_node_id and hint.target_node_id in by_module:
        return by_module[hint.target_node_id]
    if hint.target_node_id and hint.target_node_id in by_title:
        return by_title[hint.target_node_id]
    return None


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
