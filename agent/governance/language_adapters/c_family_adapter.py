"""Compilation-action-backed C/C++/Objective-C adapter for macOS."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import signal
from typing import Any, Mapping, Optional

from agent.governance.compilation_context import CompilationAction
from agent.governance.language_policy import DEFAULT_LANGUAGE_POLICY


CFAMILY_INDEX_SCHEMA_VERSION = "aming_claw.cfamily_clang_index.v1"
CFAMILY_EXTRACTOR_VERSION = "1"
_FUNCTION_KINDS = {"FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl", "ObjCMethodDecl"}
_TYPE_KINDS = {"CXXRecordDecl", "RecordDecl", "EnumDecl", "ObjCInterfaceDecl", "ObjCCategoryDecl"}
_REFERENCE_KINDS = {"FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl", "ObjCMethodDecl", "VarDecl", "FieldDecl"}


def _is_c_family_test_path(file_path: str) -> bool:
    path = Path(file_path)
    stem = path.stem.lower()
    return (
        DEFAULT_LANGUAGE_POLICY.is_test_path(file_path)
        or stem.startswith("test_")
        or stem.endswith("_test")
        or ".test." in path.name.lower()
        or ".spec." in path.name.lower()
    )


def _include_trace(stderr: str) -> list[str]:
    rows: list[str] = []
    for raw in stderr.splitlines():
        match = re.match(r"^\.+\s+(.+)$", raw.strip())
        if match:
            rows.append(match.group(1).strip())
    return rows


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _location(
    node: Mapping[str, Any],
    default_file: str,
    source_cache: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    loc = node.get("loc") if isinstance(node.get("loc"), Mapping) else {}
    begin = (node.get("range") or {}).get("begin") if isinstance(node.get("range"), Mapping) else {}
    if not isinstance(begin, Mapping):
        begin = {}
    candidates: list[Mapping[str, Any]] = []
    for raw in (loc, begin):
        expansion = raw.get("expansionLoc") if isinstance(raw.get("expansionLoc"), Mapping) else None
        spelling = raw.get("spellingLoc") if isinstance(raw.get("spellingLoc"), Mapping) else None
        if expansion is not None:
            candidates.append(expansion)
        candidates.append(raw)
        if spelling is not None:
            candidates.append(spelling)
    file_path = str(next((row.get("file") for row in candidates if row.get("file")), default_file))
    line = int(next((row.get("line") for row in candidates if row.get("line") is not None), 0) or 0)
    column = int(next((row.get("col") for row in candidates if row.get("col") is not None), 0) or 0)
    offset_value = next((row.get("offset") for row in candidates if row.get("offset") is not None), None)
    offset = int(offset_value or 0)
    if (line <= 0 or column <= 0) and offset_value is not None and file_path:
        cache = source_cache if source_cache is not None else {}
        try:
            data = cache.get(file_path)
            if data is None:
                data = Path(file_path).read_bytes()
                cache[file_path] = data
            if 0 <= offset <= len(data):
                prefix = data[:offset]
                recovered_line = prefix.count(b"\n") + 1
                last_newline = prefix.rfind(b"\n")
                recovered_column = offset - (last_newline + 1) + 1
                line = line or recovered_line
                column = column or recovered_column
        except OSError:
            pass
    return {
        "file": file_path,
        "line": line,
        "column": column,
        "offset": offset,
    }


def _diagnostics(stderr: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pattern = re.compile(r"^(.*?):(\d+):(\d+):\s+(fatal error|error|warning|note):\s+(.*)$")
    for raw in stderr.splitlines():
        match = pattern.match(raw.strip())
        if match:
            rows.append({
                "file": match.group(1),
                "line": int(match.group(2)),
                "column": int(match.group(3)),
                "severity": match.group(4),
                "message": match.group(5),
            })
        elif raw.strip() and not re.match(r"^\.+\s+", raw.strip()):
            rows.append({"file": "", "line": 0, "column": 0, "severity": "note", "message": raw.strip()})
    return rows


class CFamilyAdapter:
    """Analyze one normalized compilation action through the native helper."""

    def __init__(
        self,
        action: CompilationAction | Mapping[str, Any],
        *,
        helper_path: str = "",
        clang_path: str = "",
    ) -> None:
        if isinstance(action, CompilationAction):
            self.action = action.as_dict()
        else:
            self.action = dict(action)
        self.helper_path = str(helper_path or os.environ.get("AC_CFAMILY_CLANG_INDEXER") or "")
        self.clang_path = str(
            clang_path
            or os.environ.get("AC_CFAMILY_CLANG")
            or "/Library/Developer/CommandLineTools/usr/bin/clang"
        )
        self._analysis: dict[str, Any] | None = None
        self.analysis_runs = 0

    def supports(self, file_path: str) -> bool:
        return bool(self.action and Path(file_path).resolve() == Path(str(self.action.get("file") or "")).resolve())

    def language(self) -> str:
        return str(self.action.get("language") or "")

    def classify_file(self, file_path: str) -> dict[str, Any]:
        return {
            "file_kind": "test" if _is_c_family_test_path(file_path) else "source" if DEFAULT_LANGUAGE_POLICY.is_production_source_path(file_path) else "dependency",
            "language": self.language(),
            "adapter": "c_family_clang",
            "compilation_profile": self.action.get("profile_id", ""),
        }

    def collect_decorators(self, ast_node: Any) -> list[str]:
        return []

    def find_module_root(self, file_path: str) -> str:
        return str(Path(file_path).parent)

    def detect_test_pairing(self, source_file: str) -> Optional[str]:
        return None

    def find_test_pairing(self, source_file: str) -> Optional[str]:
        return None

    def parse_symbols(self, file_path: str, source: str = "") -> list[dict[str, Any]]:
        return list(self.analyze_action().get("symbols") or [])

    def parse_imports(self, file_path: str, source: str = "") -> list[dict[str, Any]]:
        return [
            {
                "kind": "include",
                "specifier": row.get("target_file", ""),
                "imported": row.get("target_file", ""),
                "local": "",
            }
            for row in self.analyze_action().get("relations") or []
            if row.get("relation_type") == "includes"
        ]

    def extract_relations(
        self,
        file_path: str,
        source: str = "",
        *,
        symbols: Optional[list[dict[str, Any]]] = None,
        imports: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        return list(self.analyze_action().get("relations") or [])

    def analyze_action(self) -> dict[str, Any]:
        if self._analysis is not None:
            return self._analysis
        self.analysis_runs += 1
        if not self.helper_path or not Path(self.helper_path).is_file() or not os.access(self.helper_path, os.X_OK):
            self._analysis = self._failed("helper_unavailable", [])
            return self._analysis
        argv = self._clang_arguments()
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                [self.helper_path, "--clang", self.clang_path, "--", *argv],
                cwd=str(self.action.get("directory") or "."),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            self._analysis = self._failed("clang_timeout", [{"severity": "fatal error", "message": str(exc)}])
            return self._analysis
        except OSError as exc:
            self._analysis = self._failed(type(exc).__name__, [{"severity": "fatal error", "message": str(exc)}])
            return self._analysis
        if process.returncode != 0:
            self._analysis = self._failed("helper_failed", _diagnostics(stderr), returncode=process.returncode)
            return self._analysis
        try:
            helper_payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            self._analysis = self._failed("helper_json_invalid", _diagnostics(stderr) + [{"severity": "fatal error", "message": str(exc)}])
            return self._analysis
        if helper_payload.get("schema_version") != CFAMILY_INDEX_SCHEMA_VERSION:
            self._analysis = self._failed("helper_schema_mismatch", [])
            return self._analysis
        clang_stderr = str(helper_payload.get("stderr") or "")
        diagnostics = _diagnostics(clang_stderr)
        if helper_payload.get("status") != "ok" or not isinstance(helper_payload.get("ast_json"), Mapping):
            self._analysis = self._failed("clang_failed", diagnostics, returncode=int(helper_payload.get("returncode") or 0))
            return self._analysis
        self._analysis = self._from_ast(helper_payload["ast_json"], diagnostics, include_trace=_include_trace(clang_stderr))
        return self._analysis

    def _failed(self, reason: str, diagnostics: list[dict[str, Any]], *, returncode: int | None = None) -> dict[str, Any]:
        return {
            "schema_version": CFAMILY_INDEX_SCHEMA_VERSION,
            "extractor_version": CFAMILY_EXTRACTOR_VERSION,
            "status": "failed",
            "reason": reason,
            "returncode": returncode,
            "action": dict(self.action),
            "files": [],
            "symbols": [],
            "occurrences": [],
            "relations": [],
            "macro_analysis": {"state": "not_collected", "macro_refs": list(self.action.get("macro_inputs") or [])},
            "diagnostics": diagnostics,
        }

    def _clang_arguments(self) -> list[str]:
        raw = list(self.action.get("arguments") or [])
        args = raw[1:] if raw else []
        cleaned: list[str] = []
        skip = False
        for index, arg in enumerate(args):
            if skip:
                skip = False
                continue
            if arg in {"-c", "-fsyntax-only"}:
                continue
            if arg == "-o" and index + 1 < len(args):
                skip = True
                continue
            if arg.startswith("-o") and len(arg) > 2:
                continue
            cleaned.append(str(arg))
        return [*cleaned, "-H", "-fsyntax-only", "-Xclang", "-ast-dump=json"]

    def _from_ast(self, payload: Mapping[str, Any], diagnostics: list[dict[str, Any]], *, include_trace: list[str]) -> dict[str, Any]:
        default_file = str(self.action.get("file") or "")
        action_identity = {
            "compilation_action_id": self.action.get("compilation_action_id", ""),
            "translation_unit_id": self.action.get("translation_unit_id", ""),
            "profile_id": self.action.get("profile_id", ""),
        }
        owned_files = {
            str(Path(default_file).resolve()),
            *(
                str(Path(str(row.get("path") or "")).resolve())
                for row in (self.action.get("dependency_hashes") or [])
                if isinstance(row, Mapping) and row.get("path")
            ),
        }
        decl_by_clang_id: dict[str, dict[str, Any]] = {}
        symbols: list[dict[str, Any]] = []
        source_cache: dict[str, bytes] = {}

        def walk_declarations(
            node: Any,
            scope: tuple[str, ...] = (),
            inherited_file: str = default_file,
            inherited_internal_linkage: bool = False,
        ) -> None:
            if not isinstance(node, Mapping):
                return
            kind = str(node.get("kind") or "")
            name = str(node.get("name") or "")
            location = _location(node, inherited_file, source_cache)
            if (
                kind in (_FUNCTION_KINDS | _TYPE_KINDS)
                and name
                and not node.get("isImplicit")
                and str(Path(str(location["file"])).resolve()) in owned_files
            ):
                signature = str((node.get("type") or {}).get("qualType") or "") if isinstance(node.get("type"), Mapping) else ""
                qualified = str(node.get("qualifiedName") or "::".join((*scope, name)))
                internal_linkage = inherited_internal_linkage or (
                    kind == "FunctionDecl" and str(node.get("storageClass") or "") == "static"
                )
                symbol_identity = {
                    "language": self.language(),
                    "qualified_name": qualified,
                    "signature": signature,
                }
                if internal_linkage:
                    symbol_identity["translation_unit_id"] = action_identity["translation_unit_id"]
                symbol_id = _hash(symbol_identity)
                child_kinds = {
                    str(child.get("kind") or "")
                    for child in (node.get("inner") or [])
                    if isinstance(child, Mapping)
                }
                symbol = {
                    **action_identity,
                    "symbol_id": symbol_id,
                    "clang_id": str(node.get("id") or ""),
                    "name": name,
                    "qualified_name": qualified,
                    "kind": kind,
                    "signature": signature,
                    "linkage": "internal" if internal_linkage else "external",
                    "file": location["file"],
                    "lineno": location["line"],
                    "end_lineno": location["line"],
                    "column": location["column"],
                    "is_definition": bool(
                        node.get("completeDefinition")
                        or node.get("isThisDeclarationADefinition")
                        or child_kinds.intersection({"CompoundStmt", "CXXTryStmt"})
                    ),
                    "provenance": ["clang_ast_json", *list(self.action.get("provenance") or [])],
                }
                symbols.append(symbol)
                if symbol["clang_id"]:
                    decl_by_clang_id[symbol["clang_id"]] = symbol
            nested_scope = scope
            nested_internal_linkage = inherited_internal_linkage
            if kind in {"NamespaceDecl", "CXXRecordDecl", "RecordDecl", "ObjCInterfaceDecl", "ObjCCategoryDecl"} and name:
                nested_scope = (*scope, name)
            if kind == "NamespaceDecl" and not name:
                nested_internal_linkage = True
            for child in node.get("inner") or []:
                walk_declarations(
                    child,
                    nested_scope,
                    str(location["file"] or inherited_file),
                    nested_internal_linkage,
                )

        walk_declarations(payload)
        occurrences: list[dict[str, Any]] = []
        for symbol in symbols:
            occurrence = {
                **action_identity,
                "occurrence_id": _hash({
                    **action_identity,
                    "symbol_id": symbol["symbol_id"],
                    "file": symbol["file"],
                    "line": symbol["lineno"],
                    "column": symbol["column"],
                    "role": "definition" if symbol["is_definition"] else "declaration",
                }),
                "symbol_id": symbol["symbol_id"],
                "role": "definition" if symbol["is_definition"] else "declaration",
                "file": symbol["file"],
                "line": symbol["lineno"],
                "column": symbol["column"],
                "provenance": list(symbol["provenance"]),
            }
            occurrences.append(occurrence)

        relations: list[dict[str, Any]] = []
        macro_refs = list(self.action.get("macro_inputs") or [])
        condition_ref = _hash(macro_refs) if macro_refs else ""

        def referenced_decl(node: Mapping[str, Any]) -> Mapping[str, Any] | None:
            ref = node.get("referencedDecl")
            if isinstance(ref, Mapping) and str(ref.get("kind") or "") in _REFERENCE_KINDS:
                return ref
            for child in node.get("inner") or []:
                if isinstance(child, Mapping):
                    found = referenced_decl(child)
                    if found is not None:
                        return found
            return None

        def callee_endpoint(node: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
            member_id = str(node.get("referencedMemberDecl") or "")
            if member_id:
                known = decl_by_clang_id.get(member_id)
                if known:
                    return str(known["symbol_id"]), str(known["name"]), str(known["qualified_name"]), "resolved"
                return None
            ref = node.get("referencedDecl")
            if isinstance(ref, Mapping) and str(ref.get("kind") or "") in _REFERENCE_KINDS:
                return endpoint(ref)
            for child in node.get("inner") or []:
                if isinstance(child, Mapping):
                    found = callee_endpoint(child)
                    if found is not None:
                        return found
            return None

        def endpoint(ref: Mapping[str, Any]) -> tuple[str, str, str, str]:
            clang_id = str(ref.get("id") or "")
            known = decl_by_clang_id.get(clang_id)
            if known:
                return str(known["symbol_id"]), str(known["name"]), str(known["qualified_name"]), "resolved"
            qualified = str(ref.get("qualifiedName") or ref.get("name") or "")
            name = str(ref.get("name") or qualified.rsplit("::", 1)[-1])
            signature = str((ref.get("type") or {}).get("qualType") or "") if isinstance(ref.get("type"), Mapping) else ""
            return _hash({"language": self.language(), "qualified_name": qualified, "signature": signature}), name, qualified, "external" if name else "unresolved"

        def walk_relations(node: Any, current: dict[str, Any] | None = None) -> None:
            if not isinstance(node, Mapping):
                return
            next_current = current
            if str(node.get("id") or "") in decl_by_clang_id:
                next_current = decl_by_clang_id[str(node.get("id"))]
            kind = str(node.get("kind") or "")
            if next_current and kind in _TYPE_KINDS:
                for base in node.get("bases") or []:
                    if not isinstance(base, Mapping):
                        continue
                    base_type = base.get("type") if isinstance(base.get("type"), Mapping) else {}
                    target_name = str(base_type.get("desugaredQualType") or base_type.get("qualType") or "")
                    if target_name:
                        location = _location(node, str(next_current.get("file") or default_file), source_cache)
                        target_id = _hash({"language": self.language(), "qualified_name": target_name, "signature": "type"})
                        relations.append(self._relation(next_current, target_id, target_name.rsplit("::", 1)[-1], "inherits", "external", location, condition_ref, macro_refs, target_qualified_name=target_name))
            if next_current and kind == "OverrideAttr":
                location = _location(node, str(next_current.get("file") or default_file), source_cache)
                target_name = str(next_current.get("name") or "")
                target_id = _hash({"language": self.language(), "qualified_name": target_name, "signature": str(next_current.get("signature") or "")})
                relations.append(self._relation(next_current, target_id, target_name, "overrides", "potential", location, condition_ref, macro_refs, target_qualified_name=target_name))
            if next_current and kind in {"CallExpr", "CXXMemberCallExpr", "CXXConstructExpr"}:
                target = callee_endpoint(node)
                if target is not None:
                    target_id, target_name, target_qualified_name, resolution = target
                    location = _location(node, str(next_current.get("file") or default_file), source_cache)
                    relations.append(self._relation(next_current, target_id, target_name, "calls", resolution, location, condition_ref, macro_refs, target_qualified_name=target_qualified_name))
            elif next_current and kind == "DeclRefExpr":
                ref = referenced_decl(node)
                if ref is not None:
                    target_id, target_name, target_qualified_name, resolution = endpoint(ref)
                    location = _location(node, str(next_current.get("file") or default_file), source_cache)
                    relations.append(self._relation(next_current, target_id, target_name, "references", resolution, location, condition_ref, macro_refs, target_qualified_name=target_qualified_name))
            for child in node.get("inner") or []:
                walk_relations(child, next_current)

        walk_relations(payload)
        try:
            source = Path(default_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            source = ""
        for target_file in include_trace:
            include = Path(target_file).name
            relations.append({
                **action_identity,
                "relation_id": _hash({**action_identity, "type": "includes", "source": default_file, "target": target_file}),
                "relation_type": "includes",
                "direction": "out",
                "source_symbol_id": "",
                "source_name": default_file,
                "source_file": default_file,
                "target_symbol_id": "",
                "target_name": include,
                "target_file": target_file,
                "resolution": "resolved" if Path(target_file).is_file() else "external",
                "condition_ref": condition_ref,
                "macro_refs": macro_refs,
                "provenance": ["clang_preprocessor_include_trace", *list(self.action.get("provenance") or [])],
            })
        unique_relations = {str(item["relation_id"]): item for item in relations}
        files = [{
            **action_identity,
            "file": default_file,
            "content_sha256": _hash(source),
            "role": "test" if _is_c_family_test_path(default_file) else "source",
            "provenance": list(self.action.get("provenance") or []),
        }]
        return {
            "schema_version": CFAMILY_INDEX_SCHEMA_VERSION,
            "extractor_version": CFAMILY_EXTRACTOR_VERSION,
            "status": "ok",
            "reason": "clang_ast_json_ok",
            "action": dict(self.action),
            "files": files,
            "symbols": symbols,
            "occurrences": occurrences,
            "relations": list(unique_relations.values()),
            "macro_analysis": {"state": "clang_preprocessing_applied_relationships_partially_collected", "macro_refs": macro_refs},
            "diagnostics": diagnostics,
        }

    def _relation(self, source: Mapping[str, Any], target_id: str, target_name: str, relation_type: str, resolution: str, location: Mapping[str, Any], condition_ref: str, macro_refs: list[str], *, target_qualified_name: str = "") -> dict[str, Any]:
        identity = {
            "compilation_action_id": self.action.get("compilation_action_id", ""),
            "translation_unit_id": self.action.get("translation_unit_id", ""),
            "profile_id": self.action.get("profile_id", ""),
        }
        payload = {
            **identity,
            "relation_type": relation_type,
            "source_symbol_id": source.get("symbol_id", ""),
            "target_symbol_id": target_id,
            "file": location.get("file", ""),
            "line": location.get("line", 0),
            "column": location.get("column", 0),
        }
        return {
            **payload,
            "relation_id": _hash(payload),
            "direction": "out",
            "source_name": source.get("qualified_name", ""),
            "source_file": source.get("file", ""),
            "target_name": target_name,
            "target_qualified_name": target_qualified_name,
            "target_file": "",
            "resolution": resolution,
            "condition_ref": condition_ref,
            "macro_refs": list(macro_refs),
            "provenance": ["clang_ast_json", *list(self.action.get("provenance") or [])],
        }


__all__ = ["CFAMILY_EXTRACTOR_VERSION", "CFAMILY_INDEX_SCHEMA_VERSION", "CFamilyAdapter"]
