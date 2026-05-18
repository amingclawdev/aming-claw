"""Scan source-controlled graph structure hint blocks.

Hints are source truth for manual graph structure corrections. This module only
indexes blocks; projection/materialization lives in graph_hint_projection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.governance.language_policy import DEFAULT_LANGUAGE_POLICY


_START_RE = re.compile(r"aming-claw-hint:start\s+(?P<attrs>.*)$")
_END_RE = re.compile(r"aming-claw-hint:end")
_ATTR_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*)=(?P<value>\"[^\"]*\"|'[^']*'|\S+)")
_DEF_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_SCAN_EXTENSIONS = (
    DEFAULT_LANGUAGE_POLICY.source_extensions
    | DEFAULT_LANGUAGE_POLICY.script_extensions
    | DEFAULT_LANGUAGE_POLICY.config_extensions
    | DEFAULT_LANGUAGE_POLICY.doc_extensions
)
_SCAN_FILENAMES = DEFAULT_LANGUAGE_POLICY.config_filenames | DEFAULT_LANGUAGE_POLICY.index_doc_filenames


@dataclass(frozen=True)
class GraphStructureHint:
    hint_id: str
    op: str
    source_path: str
    target_node_id: str = ""
    edge: str = ""
    role: str = ""
    reason: str = ""
    evidence: str = ""
    line_start: int = 0
    line_end: int = 0
    anchor_symbol: str = ""
    status: str = "indexed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "hint_id": self.hint_id,
            "op": self.op,
            "edge": self.edge,
            "role": self.role,
            "target_node_id": self.target_node_id,
            "source_path": self.source_path,
            "reason": self.reason,
            "evidence": self.evidence,
            "anchor": {
                "symbol": self.anchor_symbol,
                "line_start": self.line_start,
                "line_end": self.line_end,
            },
            "status": self.status,
        }


def scan_graph_structure_hints(files: Mapping[str, str]) -> dict[str, Any]:
    """Return a deterministic index of graph structure hints from text files."""
    hints: list[GraphStructureHint] = []
    for source_path in sorted(files):
        hints.extend(_scan_one_file(source_path, files[source_path] or ""))
    return {
        "hint_count": len(hints),
        "hints": [hint.to_dict() for hint in hints],
    }


def load_graph_structure_hints(project_root: str | Path) -> dict[str, Any]:
    """Scan governed text/source files under a project root for source hint blocks."""
    root = Path(project_root).resolve()
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if DEFAULT_LANGUAGE_POLICY.is_excluded_path(rel):
            continue
        if path.name not in _SCAN_FILENAMES and path.suffix.lower() not in _SCAN_EXTENSIONS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "aming-claw-hint:start" not in text:
            continue
        files[rel] = text
    return scan_graph_structure_hints(files)


def write_graph_structure_hints(
    project_root: str | Path,
    hints: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Write source-controlled graph structure hint blocks.

    This is the accept side of the AI graph-structure flow. It writes only to
    the source files named by each accepted hint and is idempotent by
    ``hint_id`` within that file.
    """
    root = Path(project_root).resolve()
    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for raw_hint in hints:
        hint = raw_hint if isinstance(raw_hint, Mapping) else {}
        source_path = _norm_relpath(hint.get("source_path"))
        hint_id = str(hint.get("hint_id") or "").strip()
        if not source_path or not hint_id:
            errors.append({"source_path": source_path, "hint_id": hint_id, "error": "missing_hint_identity"})
            continue
        target = (root / source_path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            errors.append({"source_path": source_path, "hint_id": hint_id, "error": "path_outside_project"})
            continue
        if not target.exists() or not target.is_file():
            errors.append({"source_path": source_path, "hint_id": hint_id, "error": "source_file_missing"})
            continue
        marker = _comment_marker_for_path(source_path)
        if not marker:
            errors.append({"source_path": source_path, "hint_id": hint_id, "error": "unsupported_comment_style"})
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append({"source_path": source_path, "hint_id": hint_id, "error": "source_file_not_utf8"})
            continue
        current = scan_graph_structure_hints({source_path: text})
        if any(str(item.get("hint_id") or "") == hint_id for item in current.get("hints") or []):
            skipped.append({"source_path": source_path, "hint_id": hint_id, "reason": "already_present"})
            continue
        block = render_graph_structure_hint_block(source_path, hint)
        if not block:
            errors.append({"source_path": source_path, "hint_id": hint_id, "error": "render_failed"})
            continue
        separator = "" if not text or text.endswith("\n") else "\n"
        target.write_text(text + separator + block + "\n", encoding="utf-8")
        written.append({"source_path": source_path, "hint_id": hint_id, "bytes_written": len(block.encode("utf-8"))})

    return {
        "ok": not errors,
        "written_count": len(written),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "written": written,
        "skipped": skipped,
        "errors": errors,
    }


def render_graph_structure_hint_block(source_path: str, hint: Mapping[str, Any]) -> str:
    marker = _comment_marker_for_path(source_path)
    if not marker:
        return ""
    hint_id = str(hint.get("hint_id") or "").strip()
    op = str(hint.get("op") or "").strip()
    target_node_id = str(hint.get("target_node_id") or "").strip()
    edge = str(hint.get("edge") or "").strip()
    role = str(hint.get("role") or "").strip()
    attrs = [
        f"id={_quote_attr(hint_id)}",
        f"op={_quote_attr(op)}",
    ]
    if edge:
        attrs.append(f"edge={_quote_attr(edge)}")
    if role:
        attrs.append(f"role={_quote_attr(role)}")
    if target_node_id:
        attrs.append(f"target={_quote_attr(target_node_id)}")
    reason = str(hint.get("reason") or "").strip()
    evidence = str(hint.get("evidence") or "").strip()
    lines = [
        _comment_line(marker, "aming-claw-hint:start " + " ".join(attrs)),
    ]
    if reason:
        lines.append(_comment_line(marker, f"reason: {reason}"))
    if evidence:
        lines.append(_comment_line(marker, f"evidence: {evidence}"))
    lines.append(_comment_line(marker, "aming-claw-hint:end"))
    return "\n".join(lines)


def _scan_one_file(source_path: str, text: str) -> list[GraphStructureHint]:
    hints: list[GraphStructureHint] = []
    lines = text.splitlines()
    current_symbol = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        symbol_match = _DEF_RE.match(line)
        if symbol_match:
            current_symbol = symbol_match.group(1)
        start_match = _START_RE.search(_comment_text(line))
        if not start_match:
            index += 1
            continue

        attrs = _parse_attrs(start_match.group("attrs"))
        body: list[str] = []
        line_start = index + 1
        line_end = line_start
        index += 1
        while index < len(lines):
            line_end = index + 1
            if _END_RE.search(_comment_text(lines[index])):
                break
            body.append(lines[index])
            index += 1
        hints.append(
            GraphStructureHint(
                hint_id=str(attrs.get("id") or ""),
                op=str(attrs.get("op") or ""),
                edge=str(attrs.get("edge") or ""),
                role=str(attrs.get("role") or ""),
                target_node_id=str(attrs.get("target") or attrs.get("target_node_id") or ""),
                source_path=source_path,
                reason=_body_value(body, "reason"),
                evidence=_body_value(body, "evidence"),
                line_start=line_start,
                line_end=line_end,
                anchor_symbol=current_symbol,
            )
        )
        index += 1
    return hints


def _comment_text(raw: str) -> str:
    text = str(raw or "").lstrip()
    for marker in ("#", "//", "<!--"):
        if text.startswith(marker):
            text = text[len(marker):].strip()
            if marker == "<!--" and text.endswith("-->"):
                text = text[:-3].strip()
            return text
    return ""


def _parse_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw or ""):
        value = match.group("value").strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        attrs[match.group("key")] = value
    return attrs


def _body_value(lines: list[str], key: str) -> str:
    prefix = f"{key}:"
    for raw in lines:
        text = raw.strip()
        for marker in ("#", "//", "<!--"):
            if text.startswith(marker):
                text = text[len(marker):].strip()
                break
        if text.startswith(prefix):
            return text[len(prefix):].strip().removesuffix("-->").strip().strip("-").strip()
    return ""


def _norm_relpath(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip().strip("/")


def _quote_attr(value: str) -> str:
    escaped = str(value or "").replace('"', '\\"')
    return f'"{escaped}"'


def _comment_marker_for_path(source_path: str) -> str:
    path = Path(str(source_path or ""))
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix in {".py", ".pyw", ".sh", ".bash", ".ps1", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".txt", ".rst", ".adoc"} or name in {"dockerfile", "makefile"}:
        return "#"
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css", ".scss"}:
        return "//"
    if suffix in {".md", ".mdx", ".html", ".htm"}:
        return "<!--"
    return ""


def _comment_line(marker: str, text: str) -> str:
    if marker == "<!--":
        return f"<!-- {text} -->"
    return f"{marker} {text}"
