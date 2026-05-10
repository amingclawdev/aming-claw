"""JavaScript/TypeScript adapter for deterministic graph hints."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.governance.language_policy import DEFAULT_LANGUAGE_POLICY


_JS_TS_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx")
_IMPORT_FROM_RE = re.compile(
    r"""(?m)^\s*import\s+(?P<body>[\s\S]*?)\s+from\s+["'](?P<specifier>[^"']+)["']\s*;?"""
)
_SIDE_EFFECT_IMPORT_RE = re.compile(
    r"""(?m)^\s*import\s+["'](?P<specifier>[^"']+)["']\s*;?"""
)
_EXPORT_FROM_RE = re.compile(
    r"""(?m)^\s*export\s+(?:\*|\{[\s\S]*?\})\s+from\s+["'](?P<specifier>[^"']+)["']\s*;?"""
)
_REQUIRE_RE = re.compile(
    r"""(?m)(?:const|let|var)\s+(?P<body>[\w${}\s,]+?)\s*=\s*require\(\s*["'](?P<specifier>[^"']+)["']\s*\)"""
)
_FUNCTION_RE = re.compile(
    r"""(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\("""
)
_ARROW_RE = re.compile(
    r"""(?m)^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"""
)
_CLASS_RE = re.compile(
    r"""(?m)^\s*(?:export\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)\b"""
)
_FETCH_RE = re.compile(
    r"""\bfetch\(\s*["'](?P<endpoint>[^"']+)["']""",
    re.MULTILINE,
)
_HTTP_METHOD_RE = re.compile(
    r"""\b(?P<client>axios|api|client|http)\.(?P<method>get|post|put|delete|patch)\(\s*["'](?P<endpoint>[^"']+)["']""",
    re.IGNORECASE | re.MULTILINE,
)


class JavaScriptTypescriptAdapter:
    """Static JS/TS adapter with no npm, network, or typechecker dependency."""

    def supports(self, file_path: str) -> bool:
        if not file_path:
            return False
        return Path(file_path).suffix.lower() in _JS_TS_EXTENSIONS

    def language(self) -> str:
        return "javascript_typescript"

    def classify_file(self, file_path: str) -> Dict[str, Any]:
        language = DEFAULT_LANGUAGE_POLICY.language_for_path(file_path)
        return {
            "file_kind": "source" if self.supports(file_path) else "",
            "language": language,
            "adapter": "javascript_typescript",
        }

    def collect_decorators(self, ast_node: Any) -> List[str]:
        return []

    def find_module_root(self, file_path: str) -> str:
        if not file_path:
            return ""
        return str(Path(file_path.replace("\\", "/")).parent).replace("\\", "/")

    def detect_test_pairing(self, source_file: str) -> Optional[str]:
        return self.find_test_pairing(source_file)

    def find_test_pairing(self, source_file: str) -> Optional[str]:
        if not source_file or not self.supports(source_file):
            return None
        path = Path(source_file.replace("\\", "/"))
        name = path.name
        lower = name.lower()
        if ".test." in lower or ".spec." in lower:
            return None
        suffix = path.suffix
        stem = path.stem
        return str(path.with_name(f"{stem}.test{suffix}")).replace("\\", "/")

    def parse_symbols(self, file_path: str, source: str = "") -> List[Dict[str, Any]]:
        symbols: List[Dict[str, Any]] = []
        seen: set[tuple[str, str, int]] = set()
        for kind, regex in (("function", _FUNCTION_RE), ("function", _ARROW_RE), ("class", _CLASS_RE)):
            for match in regex.finditer(source or ""):
                name = match.group("name")
                lineno = _line_number(source, match.start())
                key = (kind, name, lineno)
                if key in seen:
                    continue
                seen.add(key)
                symbols.append({
                    "name": name,
                    "kind": kind,
                    "lineno": lineno,
                    "end_lineno": lineno,
                    "decorators": [],
                })
        symbols.sort(key=lambda item: (int(item.get("lineno") or 0), str(item.get("name") or "")))
        return symbols

    def parse_imports(self, file_path: str, source: str = "") -> List[Dict[str, Any]]:
        imports: List[Dict[str, Any]] = []
        for match in _IMPORT_FROM_RE.finditer(source or ""):
            specifier = match.group("specifier")
            for local in _locals_from_import_body(match.group("body")):
                imports.append(_import_row("import", local, specifier, source, match.start()))
        for match in _SIDE_EFFECT_IMPORT_RE.finditer(source or ""):
            imports.append(_import_row("side_effect_import", match.group("specifier"), match.group("specifier"), source, match.start()))
        for match in _EXPORT_FROM_RE.finditer(source or ""):
            imports.append(_import_row("export_from", match.group("specifier"), match.group("specifier"), source, match.start()))
        for match in _REQUIRE_RE.finditer(source or ""):
            specifier = match.group("specifier")
            for local in _locals_from_require_body(match.group("body")):
                imports.append(_import_row("require", local, specifier, source, match.start()))
        return _dedupe_rows(imports)

    def extract_relations(
        self,
        file_path: str,
        source: str = "",
        *,
        symbols: Optional[List[Dict[str, Any]]] = None,
        imports: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        relations: List[Dict[str, Any]] = []
        for match in _FETCH_RE.finditer(source or ""):
            endpoint = _normalize_endpoint(match.group("endpoint"))
            if endpoint:
                relations.append(_api_relation("fetch", "GET", endpoint, source, match.start()))
        for match in _HTTP_METHOD_RE.finditer(source or ""):
            endpoint = _normalize_endpoint(match.group("endpoint"))
            if endpoint:
                relations.append(_api_relation(
                    f"{match.group('client')}.{match.group('method')}",
                    match.group("method").upper(),
                    endpoint,
                    source,
                    match.start(),
                ))
        return _dedupe_rows(relations)


def _line_number(source: str, offset: int) -> int:
    return (source or "")[:offset].count("\n") + 1


def _locals_from_import_body(body: str) -> List[str]:
    text = " ".join(str(body or "").replace("\n", " ").split())
    if not text:
        return []
    if text.startswith("{") and text.endswith("}"):
        names = text.strip("{} ")
    elif text.startswith("* as "):
        return [text[5:].strip()] if text[5:].strip() else []
    elif "," in text and "{" in text:
        default, rest = text.split(",", 1)
        names = rest.strip().strip("{}")
        out = [default.strip()] if default.strip() else []
        out.extend(_split_named_imports(names))
        return out
    else:
        return [text.strip()]
    return _split_named_imports(names)


def _split_named_imports(names: str) -> List[str]:
    out: List[str] = []
    for part in names.split(","):
        item = part.strip()
        if not item:
            continue
        if " as " in item:
            item = item.rsplit(" as ", 1)[1].strip()
        out.append(item)
    return out


def _locals_from_require_body(body: str) -> List[str]:
    text = str(body or "").strip()
    if not text:
        return []
    if text.startswith("{") and text.endswith("}"):
        return _split_named_imports(text.strip("{} "))
    return [text]


def _import_row(kind: str, local: str, specifier: str, source: str, offset: int) -> Dict[str, Any]:
    return {
        "kind": kind,
        "local": local,
        "imported": specifier,
        "specifier": specifier,
        "lineno": _line_number(source, offset),
    }


def _normalize_endpoint(endpoint: str) -> str:
    raw = str(endpoint or "").strip()
    if not raw:
        return ""
    if raw.startswith("/api/"):
        return raw
    marker = "/api/"
    if marker in raw:
        return marker + raw.split(marker, 1)[1]
    return ""


def _api_relation(evidence: str, method: str, endpoint: str, source: str, offset: int) -> Dict[str, Any]:
    return {
        "relation_type": "calls_api",
        "target": endpoint,
        "target_kind": "interface",
        "evidence": f"{method} {endpoint} via {evidence}",
        "lineno": _line_number(source, offset),
    }


def _dedupe_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        key = tuple(sorted((str(k), str(v)) for k, v in row.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


__all__ = ["JavaScriptTypescriptAdapter"]
