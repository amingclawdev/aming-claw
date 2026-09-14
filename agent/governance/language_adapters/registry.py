"""Import-safe language recognition and adapter capability registry."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from agent.governance.language_policy import DEFAULT_LANGUAGE_POLICY

from .filetree_adapter import FileTreeAdapter
from .c_family_adapter import CFamilyAdapter
from .javascript_typescript_adapter import JavaScriptTypescriptAdapter
from .python_adapter import PythonAdapter
from .ruby_adapter import RubyAdapter


_SEMANTIC_ADAPTERS = (PythonAdapter(), JavaScriptTypescriptAdapter(), RubyAdapter())
_FILETREE_ADAPTER = FileTreeAdapter()
_C_FAMILY_LANGUAGES = frozenset({"c", "cpp", "objective-c", "objective-cpp"})


@dataclass(frozen=True)
class LanguageCapability:
    """Discovery facts kept separate from compiler-backed semantic support."""

    recognized: bool
    language: str
    configuration_selected: bool
    parser_available: bool
    semantic_available: bool
    status: str
    adapter: str
    compilation_profile: str = ""
    platform: str = ""
    macro_conditions: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "recognized": self.recognized,
            "language": self.language,
            "configuration_selected": self.configuration_selected,
            "parser_available": self.parser_available,
            "semantic_available": self.semantic_available,
            "status": self.status,
            "adapter": self.adapter,
            "compilation_profile": self.compilation_profile,
            "platform": self.platform,
            "macro_conditions": list(self.macro_conditions),
            "provenance": list(self.provenance),
        }


def adapter_for_path(
    file_path: str,
    *,
    compilation_context: Mapping[str, object] | None = None,
):
    for adapter in _SEMANTIC_ADAPTERS:
        if adapter.supports(file_path):
            return adapter
    context = compilation_context or {}
    language = str(context.get("language") or DEFAULT_LANGUAGE_POLICY.language_for_path(file_path))
    helper_path = str(context.get("helper_path") or "")
    if (
        language in _C_FAMILY_LANGUAGES
        and context.get("compilation_action_id")
        and helper_path
    ):
        adapter = CFamilyAdapter(context, helper_path=helper_path, clang_path=str(context.get("clang_path") or ""))
        if adapter.supports(file_path):
            return adapter
    return _FILETREE_ADAPTER


def adapter_for_paths(
    file_paths: Iterable[str],
    *,
    compilation_contexts: Mapping[str, Mapping[str, object]] | None = None,
):
    contexts = compilation_contexts or {}
    adapters = [
        adapter_for_path(path, compilation_context=contexts.get(path))
        for path in file_paths if path
    ]
    if adapters and all(type(item) is type(adapters[0]) for item in adapters):
        return adapters[0]
    return _FILETREE_ADAPTER


def capability_for_path(
    file_path: str,
    *,
    compilation_context: Mapping[str, object] | None = None,
) -> LanguageCapability:
    """Describe recognition, configuration, parsing and semantics independently."""
    context = compilation_context or {}
    recognized = DEFAULT_LANGUAGE_POLICY.is_recognized_path(file_path)
    language = DEFAULT_LANGUAGE_POLICY.language_for_path(file_path)
    configured_language = str(context.get("language") or "")
    if DEFAULT_LANGUAGE_POLICY.is_dependency_path(file_path):
        language = configured_language if configured_language in _C_FAMILY_LANGUAGES else "unknown"

    adapter = adapter_for_path(file_path, compilation_context=context)
    adapter_name = adapter.language() or "filetree"
    parser_available = adapter_name in {"python", "javascript_typescript", "ruby", "c", "cpp", "objective-c", "objective-cpp"}
    configuration_selected = parser_available or bool(
        context.get("compilation_profile") or configured_language
    )
    semantic_available = parser_available and adapter_name not in _C_FAMILY_LANGUAGES or (
        adapter_name in _C_FAMILY_LANGUAGES
        and bool(context.get("helper_path"))
        and bool(context.get("compilation_action_id"))
    )
    if not recognized:
        status = "unrecognized"
    elif semantic_available:
        status = "semantic_available"
    elif configuration_selected:
        status = "unsupported"
    else:
        status = "unconfigured"
    return LanguageCapability(
        recognized=recognized,
        language=language,
        configuration_selected=configuration_selected,
        parser_available=parser_available,
        semantic_available=semantic_available,
        status=status,
        adapter=adapter_name,
        compilation_profile=str(context.get("compilation_profile") or ""),
        platform=str(context.get("platform") or ""),
        macro_conditions=tuple(str(v) for v in context.get("macro_conditions") or ()),
        provenance=tuple(str(v) for v in context.get("provenance") or ()),
    )


__all__ = ["LanguageCapability", "adapter_for_path", "adapter_for_paths", "capability_for_path"]
