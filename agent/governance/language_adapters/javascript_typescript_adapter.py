"""JavaScript/TypeScript adapter for deterministic graph hints."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.governance.language_policy import DEFAULT_LANGUAGE_POLICY


_JS_TS_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
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
    r"""(?m)^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\("""
)
_ARROW_RE = re.compile(
    r"""(?m)^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"""
)
_CLASS_RE = re.compile(
    r"""(?m)^\s*(?:export\s+)?(?:default\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)\b"""
)
_API_LITERAL_RE_FRAGMENT = r"""(?P<quote>["'`])(?P<endpoint>[^"'`\r\n]*?/api/[^"'`\r\n]*)(?P=quote)"""
_FETCH_RE = re.compile(
    r"""\bfetch\(\s*""" + _API_LITERAL_RE_FRAGMENT,
    re.MULTILINE,
)
_JSON_HELPER_CALL_RE = re.compile(
    r"""\b(?P<helper>getJSON|postJSON)\s*(?:<[^>]+>)?\s*\(\s*""" + _API_LITERAL_RE_FRAGMENT,
    re.MULTILINE,
)
_HTTP_HELPER_CALL_RE = re.compile(
    r"""\bhttp\s*\(\s*(?P<method_quote>["'`])(?P<method>[A-Z]+)(?P=method_quote)\s*,\s*"""
    + _API_LITERAL_RE_FRAGMENT,
    re.MULTILINE,
)
_HELPER_VAR_CALL_RE = re.compile(
    r"""\b(?P<helper>getJSON|postJSON)\s*(?:<[^>]+>)?\s*\(\s*(?P<var>[A-Za-z_$][\w$]*)\b""",
    re.MULTILINE,
)
_CONST_ASSIGN_RE = re.compile(
    r"""\bconst\s+(?P<name>[A-Za-z_$][\w$]*)\s*=""",
    re.MULTILINE,
)
_STRING_LITERAL_RE = re.compile(
    r"""(?P<quote>["'`])(?P<value>.*?)(?P=quote)""",
    re.DOTALL,
)
_OBJECT_EXPORT_RE = re.compile(
    r"""(?m)^\s*export\s+const\s+(?P<object>[A-Za-z_$][\w$]*)\s*=\s*\{"""
)
_OBJECT_METHOD_LINE_RE = re.compile(
    r"""^\s*(?:async\s+)?(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>{}]+>)?\s*\(""",
    re.MULTILINE,
)
_HTTP_METHOD_RE = re.compile(
    r"""\b(?P<client>axios|api|client|http)\.(?P<method>get|post|put|delete|patch)\s*(?:<[^>]+>)?\(\s*"""
    + _API_LITERAL_RE_FRAGMENT,
    re.IGNORECASE | re.MULTILINE,
)
_CALL_RE = re.compile(
    r"""\b(?P<name>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)\s*(?:<[^>\r\n]+>)?\(""",
    re.MULTILINE,
)
_CONTROL_CALL_NAMES = {
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "function",
    "return",
    "typeof",
    "new",
}


class JavaScriptTypescriptAdapter:
    """Static JS/TS adapter with no npm, network, or typechecker dependency."""

    def supports(self, file_path: str) -> bool:
        if not file_path:
            return False
        normalized = file_path.replace("\\", "/").lower()
        if DEFAULT_LANGUAGE_POLICY.is_declaration_path(normalized):
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
                lineno = _line_number(source, match.start("name"))
                end_lineno = _block_end_lineno(source, match.end(), lineno)
                key = (kind, name, lineno)
                if key in seen:
                    continue
                seen.add(key)
                symbols.append({
                    "name": name,
                    "kind": kind,
                    "lineno": lineno,
                    "end_lineno": end_lineno,
                    "decorators": [],
                })
        for symbol in _exported_object_method_symbols(source or ""):
            key = (str(symbol.get("kind") or ""), str(symbol.get("name") or ""), int(symbol.get("lineno") or 0))
            if key in seen:
                continue
            seen.add(key)
            symbols.append(symbol)
        symbols.sort(key=lambda item: (int(item.get("lineno") or 0), str(item.get("name") or "")))
        for symbol in symbols:
            if symbol.get("kind") != "function":
                continue
            symbol["calls"] = _extract_calls_for_symbol(
                source or "",
                int(symbol.get("lineno") or 1),
                int(symbol.get("end_lineno") or symbol.get("lineno") or 1),
                str(symbol.get("name") or ""),
            )
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
        endpoint_vars = _endpoint_variables(source or "")
        for match in _FETCH_RE.finditer(source or ""):
            endpoint = _normalize_endpoint(match.group("endpoint"))
            if endpoint:
                relations.append(_api_relation("fetch", "GET", endpoint, source, match.start()))
        for match in _JSON_HELPER_CALL_RE.finditer(source or ""):
            endpoint = _normalize_endpoint(match.group("endpoint"))
            if endpoint:
                helper = match.group("helper")
                method = "POST" if helper == "postJSON" else "GET"
                relations.append(_api_relation(helper, method, endpoint, source, match.start()))
        for match in _HTTP_HELPER_CALL_RE.finditer(source or ""):
            endpoint = _normalize_endpoint(match.group("endpoint"))
            if endpoint:
                relations.append(_api_relation("http", match.group("method").upper(), endpoint, source, match.start()))
        for match in _HELPER_VAR_CALL_RE.finditer(source or ""):
            endpoint = _endpoint_var_before(endpoint_vars, match.group("var"), match.start())
            if endpoint:
                helper = match.group("helper")
                method = "POST" if helper == "postJSON" else "GET"
                relations.append(_api_relation(f"{helper}:var", method, endpoint, source, match.start()))
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


def _extract_calls_for_symbol(source: str, start_lineno: int, end_lineno: int, owner_name: str) -> List[str]:
    lines = (source or "").splitlines()
    start = max(0, int(start_lineno or 1) - 1)
    end = max(start + 1, min(len(lines), int(end_lineno or start_lineno or 1)))
    body = "\n".join(lines[start:end])
    owner_short = owner_name.split(".")[-1]
    calls: List[str] = []
    seen: set[str] = set()
    for match in _CALL_RE.finditer(body):
        name = str(match.group("name") or "").strip()
        first = name.split(".", 1)[0]
        if not name or first in _CONTROL_CALL_NAMES:
            continue
        if name == owner_name or name == owner_short or name.endswith(f".{owner_short}"):
            continue
        if name in seen:
            continue
        seen.add(name)
        calls.append(name)
    return calls


def _find_matching_brace(source: str, open_offset: int) -> int:
    if open_offset < 0 or open_offset >= len(source) or source[open_offset] != "{":
        return -1
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    for index in range(open_offset, len(source)):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _block_end_lineno(source: str, start_offset: int, fallback_lineno: int) -> int:
    brace = _body_open_brace(source, start_offset)
    if brace == -1:
        return fallback_lineno
    close = _find_matching_brace(source, brace)
    if close == -1:
        close = _find_matching_brace_raw(source, brace)
    if close == -1:
        return fallback_lineno
    return _line_number(source, close)


def _find_matching_brace_raw(source: str, open_offset: int) -> int:
    if open_offset < 0 or open_offset >= len(source) or source[open_offset] != "{":
        return -1
    depth = 0
    for index in range(open_offset, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _body_open_brace(source: str, start_offset: int) -> int:
    paren_depth = 1 if start_offset > 0 and source[start_offset - 1] == "(" else 0
    bracket_depth = 0
    angle_depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = max(0, start_offset)
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
                continue
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            paren_depth += 1
        elif char == ")" and paren_depth > 0:
            paren_depth -= 1
        elif char == "[":
            bracket_depth += 1
        elif char == "]" and bracket_depth > 0:
            bracket_depth -= 1
        elif char == "<" and paren_depth == 0 and bracket_depth == 0:
            angle_depth += 1
        elif char == ">" and angle_depth > 0:
            angle_depth -= 1
        elif char == "{" and paren_depth == 0 and bracket_depth == 0 and angle_depth == 0:
            if _previous_nonspace(source, index) in {":", "<", ","}:
                close = _find_matching_brace(source, index)
                if close > index:
                    index = close + 1
                    continue
            return index
        index += 1
    return -1


def _previous_nonspace(source: str, offset: int) -> str:
    index = offset - 1
    while index >= 0:
        char = source[index]
        if not char.isspace():
            return char
        index -= 1
    return ""


def _exported_object_method_symbols(source: str) -> List[Dict[str, Any]]:
    symbols: List[Dict[str, Any]] = []
    for match in _OBJECT_EXPORT_RE.finditer(source or ""):
        object_name = match.group("object")
        open_offset = source.find("{", match.start())
        close_offset = _find_matching_brace(source, open_offset)
        if open_offset == -1 or close_offset == -1:
            continue
        offset = open_offset + 1
        while offset < close_offset:
            line_end = source.find("\n", offset, close_offset)
            if line_end == -1:
                line_end = close_offset
            line = source[offset:line_end]
            method_match = _OBJECT_METHOD_LINE_RE.match(line)
            if method_match:
                name = method_match.group("name")
                absolute_start = offset + method_match.start("name")
                signature_end = line_end
                brace = source.find("{", absolute_start, signature_end + 1)
                end_lineno = _line_number(source, absolute_start)
                if brace != -1:
                    method_close = _find_matching_brace(source, brace)
                    if method_close != -1:
                        end_lineno = _line_number(source, method_close)
                        offset = method_close + 1
                    else:
                        offset = line_end + 1
                else:
                    offset = line_end + 1
                symbols.append({
                    "name": f"{object_name}.{name}",
                    "kind": "function",
                    "lineno": _line_number(source, absolute_start),
                    "end_lineno": end_lineno,
                    "decorators": [],
                })
                continue
            offset = line_end + 1
    return symbols


def _endpoint_variables(source: str) -> Dict[str, List[Tuple[int, str]]]:
    out: Dict[str, List[Tuple[int, str]]] = {}
    for match in _CONST_ASSIGN_RE.finditer(source or ""):
        name = match.group("name")
        semi = source.find(";", match.end())
        if semi == -1:
            continue
        expr = source[match.end():semi]
        if len(expr) > 1200:
            continue
        parts = [part.group("value") for part in _STRING_LITERAL_RE.finditer(expr)]
        if not parts or not any("/api/" in part for part in parts):
            continue
        endpoint = _normalize_endpoint("".join(parts))
        if endpoint:
            out.setdefault(name, []).append((match.start(), endpoint))
    return out


def _endpoint_var_before(endpoint_vars: Dict[str, List[Tuple[int, str]]], name: str, offset: int) -> str:
    candidates = [
        (position, endpoint)
        for position, endpoint in endpoint_vars.get(name, [])
        if position < offset
    ]
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def _locals_from_import_body(body: str) -> List[str]:
    text = " ".join(str(body or "").replace("\n", " ").split())
    if not text:
        return []
    if text.startswith("type "):
        text = text[5:].strip()
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
        if item.startswith("type "):
            item = item[5:].strip()
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
    raw = re.sub(r"""\$\{[^}]*\}""", "{expr}", raw)
    raw = "".join(raw.split())
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
