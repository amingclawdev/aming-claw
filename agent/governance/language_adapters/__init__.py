"""language_adapters — pluggable per-language analysis adapters (CR1 R4).

Re-exports the public surface used by ``reconcile_phases.cluster_grouper``
and downstream consumers.
"""
from __future__ import annotations

from .base import LanguageAdapter
from .c_family_adapter import CFamilyAdapter
from .filetree_adapter import FileTreeAdapter
from .javascript_typescript_adapter import JavaScriptTypescriptAdapter
from .python_adapter import PythonAdapter
from .ruby_adapter import RubyAdapter
from .registry import LanguageCapability, adapter_for_path, adapter_for_paths, capability_for_path

__all__ = [
    "LanguageAdapter",
    "CFamilyAdapter",
    "PythonAdapter",
    "JavaScriptTypescriptAdapter",
    "RubyAdapter",
    "FileTreeAdapter",
    "LanguageCapability",
    "adapter_for_path",
    "adapter_for_paths",
    "capability_for_path",
]
