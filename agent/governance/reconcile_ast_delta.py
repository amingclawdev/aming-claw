from __future__ import annotations

import ast
import hashlib
import json
from typing import Any


FUNCTION_SIGNATURE_SCHEMA_VERSION = "reconcile_ast_delta.function_signature.v1"
FUNCTION_DELTA_SCHEMA_VERSION = "reconcile_ast_delta.function_delta.v1"
SOURCE_FILE_DELTA_SCHEMA_VERSION = "reconcile_ast_delta.source_file_delta.v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _ast_dump(node: ast.AST | None) -> str:
    if node is None:
        return ""
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _arg_record(arg: ast.arg, default: ast.AST | None) -> dict[str, Any]:
    return {
        "name": str(arg.arg or ""),
        "annotation": _ast_dump(arg.annotation),
        "default": _ast_dump(default) if default is not None else None,
    }


def _optional_arg_record(arg: ast.arg | None) -> dict[str, Any] | None:
    if arg is None:
        return None
    return {
        "name": str(arg.arg or ""),
        "annotation": _ast_dump(arg.annotation),
    }


def _callable_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    args = node.args
    positional = list(args.posonlyargs) + list(args.args)
    defaults: list[ast.AST | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    posonly_count = len(args.posonlyargs)
    posonly_defaults = defaults[:posonly_count]
    regular_defaults = defaults[posonly_count:]

    kw_defaults = list(args.kw_defaults)
    return {
        "kind": "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
        "posonlyargs": [
            _arg_record(arg, default)
            for arg, default in zip(args.posonlyargs, posonly_defaults)
        ],
        "args": [
            _arg_record(arg, default)
            for arg, default in zip(args.args, regular_defaults)
        ],
        "vararg": _optional_arg_record(args.vararg),
        "kwonlyargs": [
            _arg_record(arg, default)
            for arg, default in zip(args.kwonlyargs, kw_defaults)
        ],
        "kwarg": _optional_arg_record(args.kwarg),
        "returns": _ast_dump(node.returns),
    }


def _decorator_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [_ast_dump(item) for item in getattr(node, "decorator_list", []) or []]


class _FunctionSignatureCollector(ast.NodeVisitor):
    def __init__(self, module_name: str) -> None:
        self.module_name = str(module_name or "")
        self._scope: list[str] = []
        self.functions: list[dict[str, Any]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(str(node.name or ""))
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        local_parts = [part for part in [*self._scope, str(node.name or "")] if part]
        local_qualname = ".".join(local_parts)
        qualified_name = f"{self.module_name}::{local_qualname}" if self.module_name else local_qualname
        signature = _callable_signature(node)
        item = {
            "schema_version": FUNCTION_SIGNATURE_SCHEMA_VERSION,
            "name": str(node.name or ""),
            "qualified_name": qualified_name,
            "local_qualname": local_qualname,
            "parent_qualname": ".".join(self._scope),
            "kind": signature["kind"],
            "lineno": int(getattr(node, "lineno", 0) or 0),
            "end_lineno": int(getattr(node, "end_lineno", getattr(node, "lineno", 0)) or 0),
            "signature": signature,
            "signature_hash": _hash_json(signature),
            "decorators": _decorator_signature(node),
        }
        self.functions.append(item)
        self._scope.append(str(node.name or ""))
        self.generic_visit(node)
        self._scope.pop()


def python_source_function_signatures(
    source: str,
    *,
    module_name: str = "",
    path: str = "",
) -> dict[str, Any]:
    """Return deterministic JSON-serializable Python function signatures."""
    try:
        tree = ast.parse(str(source or ""), filename=str(path or module_name or "<source>"))
    except (SyntaxError, ValueError) as exc:
        return {
            "ok": False,
            "schema_version": SOURCE_FILE_DELTA_SCHEMA_VERSION,
            "reason": "source_ast_parse_failed",
            "path": str(path or ""),
            "module": str(module_name or ""),
            "functions": [],
            "function_count": 0,
            "parse_error": {
                "type": exc.__class__.__name__,
                "message": str(exc),
                "lineno": int(getattr(exc, "lineno", 0) or 0),
                "offset": int(getattr(exc, "offset", 0) or 0),
            },
        }

    collector = _FunctionSignatureCollector(module_name=str(module_name or ""))
    collector.visit(tree)
    functions = sorted(
        collector.functions,
        key=lambda item: (
            str(item.get("qualified_name") or ""),
            int(item.get("lineno") or 0),
            int(item.get("end_lineno") or 0),
        ),
    )
    return {
        "ok": True,
        "schema_version": SOURCE_FILE_DELTA_SCHEMA_VERSION,
        "reason": "source_ast_parse_ok",
        "path": str(path or ""),
        "module": str(module_name or ""),
        "functions": functions,
        "function_count": len(functions),
    }


def _function_index(functions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("qualified_name") or ""): _json_clone(item)
        for item in functions
        if str(item.get("qualified_name") or "")
    }


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _line_range(item: dict[str, Any]) -> list[int]:
    start = int(item.get("lineno") or 0)
    end = int(item.get("end_lineno") or start or 0)
    return [start, end]


def _signature_hash(item: dict[str, Any]) -> str:
    existing = str(item.get("signature_hash") or "")
    if existing:
        return existing
    return _hash_json(item.get("signature") or {})


def function_signature_delta(
    *,
    before_signature: list[dict[str, Any]],
    after_signature: list[dict[str, Any]],
    identity_reason: str = "source_function_identity_changed",
    signature_reason: str = "source_function_signature_changed",
) -> dict[str, Any]:
    """Compare two function signature lists with deterministic JSON output."""
    before_by_name = _function_index(before_signature)
    after_by_name = _function_index(after_signature)
    before_names = set(before_by_name)
    after_names = set(after_by_name)

    added = sorted(after_names - before_names)
    removed = sorted(before_names - after_names)
    common = sorted(before_names & after_names)

    signature_changes: list[dict[str, Any]] = []
    line_range_changes: list[dict[str, Any]] = []
    unchanged: list[str] = []
    for qualified_name in common:
        before_item = before_by_name[qualified_name]
        after_item = after_by_name[qualified_name]
        before_hash = _signature_hash(before_item)
        after_hash = _signature_hash(after_item)
        if before_hash != after_hash:
            signature_changes.append({
                "qualified_name": qualified_name,
                "before_signature_hash": before_hash,
                "after_signature_hash": after_hash,
                "before_signature": _json_clone(before_item.get("signature") or {}),
                "after_signature": _json_clone(after_item.get("signature") or {}),
                "before_lines": _line_range(before_item),
                "after_lines": _line_range(after_item),
            })
            continue

        before_lines = _line_range(before_item)
        after_lines = _line_range(after_item)
        if before_lines != after_lines:
            line_range_changes.append({
                "qualified_name": qualified_name,
                "before_lines": before_lines,
                "after_lines": after_lines,
            })
        unchanged.append(qualified_name)

    identity_changed = bool(added or removed)
    signatures_changed = bool(signature_changes)
    reason = (
        identity_reason
        if identity_changed
        else signature_reason
        if signatures_changed
        else "source_function_signature_stable"
    )
    return {
        "schema_version": FUNCTION_DELTA_SCHEMA_VERSION,
        "stable_identity": not identity_changed,
        "stable_signatures": not signatures_changed,
        "requires_full_rebuild": bool(identity_changed or signatures_changed),
        "reason": reason,
        "identity_reason": identity_reason if identity_changed else "",
        "signature_reason": signature_reason if signatures_changed else "",
        "added_functions": added,
        "removed_functions": removed,
        "changed_functions": [item["qualified_name"] for item in signature_changes],
        "unchanged_functions": unchanged,
        "added_function_count": len(added),
        "removed_function_count": len(removed),
        "changed_function_count": len(signature_changes),
        "unchanged_function_count": len(unchanged),
        "before_function_count": len(before_names),
        "after_function_count": len(after_names),
        "signature_changes": signature_changes,
        "line_range_changed": bool(line_range_changes),
        "line_range_change_count": len(line_range_changes),
        "line_range_changes": line_range_changes,
    }


def source_file_function_delta(
    *,
    before_source: str,
    after_source: str,
    module_name: str = "",
    path: str = "",
    identity_reason: str = "source_function_identity_changed",
    signature_reason: str = "source_function_signature_changed",
) -> dict[str, Any]:
    before = python_source_function_signatures(before_source, module_name=module_name, path=path)
    after = python_source_function_signatures(after_source, module_name=module_name, path=path)
    if not before.get("ok") or not after.get("ok"):
        return {
            "ok": False,
            "schema_version": SOURCE_FILE_DELTA_SCHEMA_VERSION,
            "reason": "source_ast_parse_failed",
            "path": str(path or ""),
            "module": str(module_name or ""),
            "before": before,
            "after": after,
        }

    delta = function_signature_delta(
        before_signature=list(before.get("functions") or []),
        after_signature=list(after.get("functions") or []),
        identity_reason=identity_reason,
        signature_reason=signature_reason,
    )
    return {
        "ok": True,
        "schema_version": SOURCE_FILE_DELTA_SCHEMA_VERSION,
        "reason": str(delta.get("reason") or ""),
        "path": str(path or ""),
        "module": str(module_name or ""),
        "before_signature": before["functions"],
        "after_signature": after["functions"],
        "before_function_count": before["function_count"],
        "after_function_count": after["function_count"],
        "delta": delta,
    }


__all__ = [
    "FUNCTION_DELTA_SCHEMA_VERSION",
    "FUNCTION_SIGNATURE_SCHEMA_VERSION",
    "SOURCE_FILE_DELTA_SCHEMA_VERSION",
    "function_signature_delta",
    "python_source_function_signatures",
    "source_file_function_delta",
]
