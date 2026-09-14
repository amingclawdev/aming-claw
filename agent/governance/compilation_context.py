"""Commit-local compilation database normalization for C-family analysis."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shlex
from typing import Any, Iterable, Mapping


COMPILATION_ACTION_SCHEMA_VERSION = "aming_claw.cfamily_compilation_action.v1"
_C_FAMILY_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".m", ".mm"}
_INCLUDE_RE = re.compile(r"^\s*#\s*(?:include|import)\s*([<\"])([^>\"]+)[>\"]", re.MULTILINE)


def _sha256_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _absolute(path: str, directory: Path) -> Path:
    value = Path(path)
    return (value if value.is_absolute() else directory / value).resolve()


def _language(file_path: Path, arguments: tuple[str, ...]) -> str:
    for index, arg in enumerate(arguments):
        if arg == "-x" and index + 1 < len(arguments):
            explicit = arguments[index + 1].lower()
            return {
                "c": "c",
                "c++": "cpp",
                "objective-c": "objective-c",
                "objective-c++": "objective-cpp",
            }.get(explicit, explicit)
        if arg.startswith("-x") and len(arg) > 2:
            explicit = arg[2:].lower()
            return {"c++": "cpp", "objective-c++": "objective-cpp"}.get(explicit, explicit)
    return {
        ".c": "c",
        ".m": "objective-c",
        ".mm": "objective-cpp",
    }.get(file_path.suffix.lower(), "cpp")


def _option_values(arguments: tuple[str, ...], names: set[str], prefixes: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        if arg in names and index + 1 < len(arguments):
            values.append(arguments[index + 1])
            index += 2
            continue
        matched = next((prefix for prefix in prefixes if arg.startswith(prefix) and arg != prefix), "")
        if matched:
            values.append(arg[len(matched):])
        index += 1
    return values


def _dependency_hashes(file_path: Path, include_inputs: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    include_roots = [Path(item).resolve() for item in include_inputs]
    rows: list[tuple[str, str]] = []
    pending = [file_path.resolve()]
    visited: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        try:
            source = current.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        quoted_roots = [current.parent, *include_roots]
        for opener, name in _INCLUDE_RE.findall(source):
            # Quoted includes search beside their including file first.  Angle
            # includes are still project dependencies when the compilation
            # action resolves them through an explicit include input.
            roots = quoted_roots if opener == '"' else include_roots
            resolved = next((root / name for root in roots if (root / name).is_file()), None)
            if resolved is None:
                continue
            real = resolved.resolve()
            rows.append((str(real), _sha256_file(real)))
            if real not in visited:
                pending.append(real)
    return tuple(sorted(set(rows)))


@dataclass(frozen=True)
class CompilationAction:
    compilation_action_id: str
    translation_unit_id: str
    profile_id: str
    file: str
    directory: str
    arguments: tuple[str, ...]
    argv_sha256: str
    source_sha256: str
    language: str
    target: str
    sdk: str
    include_inputs: tuple[str, ...]
    macro_inputs: tuple[str, ...]
    dependency_hashes: tuple[tuple[str, str], ...]
    provenance: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": COMPILATION_ACTION_SCHEMA_VERSION,
            "compilation_action_id": self.compilation_action_id,
            "translation_unit_id": self.translation_unit_id,
            "profile_id": self.profile_id,
            "file": self.file,
            "directory": self.directory,
            "arguments": list(self.arguments),
            "argv_sha256": self.argv_sha256,
            "source_sha256": self.source_sha256,
            "language": self.language,
            "target": self.target,
            "sdk": self.sdk,
            "include_inputs": list(self.include_inputs),
            "macro_inputs": list(self.macro_inputs),
            "dependency_hashes": [
                {"path": path, "sha256": digest}
                for path, digest in self.dependency_hashes
            ],
            "provenance": list(self.provenance),
        }


def _normalize_entry(entry: Mapping[str, Any], *, database_path: Path) -> CompilationAction | None:
    directory = Path(str(entry.get("directory") or database_path.parent)).resolve()
    file_value = str(entry.get("file") or "").strip()
    if not file_value:
        return None
    file_path = _absolute(file_value, directory)
    if file_path.suffix.lower() not in _C_FAMILY_SUFFIXES:
        return None
    raw_arguments = entry.get("arguments")
    if isinstance(raw_arguments, list):
        arguments = tuple(str(item) for item in raw_arguments)
    else:
        command = str(entry.get("command") or "")
        arguments = tuple(shlex.split(command)) if command else ()
    if not arguments:
        return None
    include_values = _option_values(arguments, {"-I", "-isystem", "-iquote"}, ("-I", "-isystem", "-iquote"))
    include_inputs = tuple(sorted({str(_absolute(item, directory)) for item in include_values if item}))
    macro_inputs = tuple(sorted(set(_option_values(arguments, {"-D", "-U"}, ("-D", "-U")))))
    target_values = _option_values(arguments, {"-target"}, ("--target=", "-target="))
    sdk_values = _option_values(arguments, {"-isysroot"}, ("-isysroot",))
    language = _language(file_path, arguments)
    dependency_hashes = _dependency_hashes(file_path, include_inputs)
    profile_payload = {
        "language": language,
        "target": target_values[-1] if target_values else "",
        "sdk": str(_absolute(sdk_values[-1], directory)) if sdk_values else "",
        "include_inputs": include_inputs,
        "macro_inputs": macro_inputs,
    }
    profile_id = _sha256_json(profile_payload)
    argv_sha256 = _sha256_json(list(arguments))
    action_payload = {
        "file": str(file_path),
        "directory": str(directory),
        "argv_sha256": argv_sha256,
        "source_sha256": _sha256_file(file_path),
        "profile_id": profile_id,
        "dependency_hashes": dependency_hashes,
    }
    action_id = _sha256_json(action_payload)
    return CompilationAction(
        compilation_action_id=action_id,
        translation_unit_id=_sha256_json({"file": str(file_path), "profile_id": profile_id}),
        profile_id=profile_id,
        file=str(file_path),
        directory=str(directory),
        arguments=arguments,
        argv_sha256=argv_sha256,
        source_sha256=_sha256_file(file_path),
        language=language,
        target=target_values[-1] if target_values else "",
        sdk=str(_absolute(sdk_values[-1], directory)) if sdk_values else "",
        include_inputs=include_inputs,
        macro_inputs=macro_inputs,
        dependency_hashes=dependency_hashes,
        provenance=(str(database_path),),
    )


def load_compilation_actions(project_root: str | Path, *, database_path: str | Path | None = None) -> list[CompilationAction]:
    root = Path(project_root).resolve()
    db_path = Path(database_path).resolve() if database_path else root / "compile_commands.json"
    try:
        payload = json.loads(db_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    by_id: dict[str, CompilationAction] = {}
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        action = _normalize_entry(entry, database_path=db_path)
        if action is not None:
            by_id.setdefault(action.compilation_action_id, action)
    return sorted(by_id.values(), key=lambda item: (item.file, item.profile_id, item.compilation_action_id))


def action_for_file(actions: Iterable[CompilationAction], file_path: str | Path) -> CompilationAction | None:
    requested = str(Path(file_path).resolve())
    exact = [item for item in actions if item.file == requested]
    if len(exact) == 1:
        return exact[0]
    dependent = [
        item for item in actions
        if any(path == requested for path, _digest in item.dependency_hashes)
    ]
    return dependent[0] if len(dependent) == 1 else None


def compilation_context_for_file(project_root: str | Path, file_path: str | Path) -> dict[str, Any]:
    action = action_for_file(load_compilation_actions(project_root), file_path)
    return action.as_dict() if action is not None else {}


__all__ = [
    "COMPILATION_ACTION_SCHEMA_VERSION",
    "CompilationAction",
    "action_for_file",
    "compilation_context_for_file",
    "load_compilation_actions",
]
