"""Guardrail test: every agent/**/*.py module with >30 significant lines
must appear in CODE_DOC_MAP, and route-containing modules must map to
at least one docs/api/*.md file.

AC3 (B3-GUARDRAIL-TEST): enumerates agent/**/*.py, checks >30 sig-line threshold
AC4 (B4-ROUTE-ENFORCEMENT): route modules map to docs/api/*.md
AC5 (B5-STALE-WARN): stale entries emit warnings, not assertion failures
"""

import os
import re
import warnings
from pathlib import Path

import pytest

# Import the map under test
from agent.governance.impact_analyzer import CODE_DOC_MAP

# Project root: two levels up from agent/tests/
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Patterns that indicate HTTP route registration
ROUTE_PATTERNS = [
    re.compile(r"@route\("),
    re.compile(r"\.add_route\("),
    re.compile(r"\.route\("),
    re.compile(r"app\.router\.add_"),
]

# Directories / filenames to exclude from enumeration
EXCLUDE_DIRS = {"tests", "__pycache__", "fixtures"}
EXCLUDE_FILES = {"__init__.py", "__main__.py"}

SIGNIFICANT_LINE_THRESHOLD = 30


def _enumerate_agent_modules():
    """Yield relative paths (posix-style) for all agent/**/*.py modules,
    excluding tests/, __init__.py, __pycache__, and fixtures/."""
    agent_dir = PROJECT_ROOT / "agent"
    for py_file in sorted(agent_dir.rglob("*.py")):
        rel = py_file.relative_to(PROJECT_ROOT)
        parts = rel.parts

        # Skip excluded dirs
        if any(d in EXCLUDE_DIRS for d in parts):
            continue

        # Skip excluded filenames
        if rel.name in EXCLUDE_FILES:
            continue

        yield rel.as_posix(), py_file


def _count_significant_lines(filepath):
    """Count non-blank, non-comment lines in a Python file."""
    count = 0
    in_docstring = False
    docstring_delim = None
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                line = raw_line.strip()

                # Handle triple-quote docstrings
                if in_docstring:
                    if docstring_delim in line:
                        in_docstring = False
                    continue

                if line.startswith('"""') or line.startswith("'''"):
                    delim = line[:3]
                    # Check if docstring opens and closes on same line
                    if line.count(delim) >= 2:
                        # Single-line docstring — still counts as a line
                        count += 1
                        continue
                    in_docstring = True
                    docstring_delim = delim
                    continue

                # Skip blank lines and comment-only lines
                if not line or line.startswith("#"):
                    continue

                count += 1
    except (OSError, UnicodeDecodeError):
        return 0
    return count


def _has_route_patterns(filepath):
    """Return True if file contains HTTP route registration patterns."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return False
    return any(pat.search(content) for pat in ROUTE_PATTERNS)


def _is_covered_by_map(rel_posix):
    """Check if a relative path is covered by any CODE_DOC_MAP key.

    Supports both exact match and prefix/substring match (e.g., the
    'agent/telegram_gateway/' directory prefix covers all files under it).
    """
    for pattern in CODE_DOC_MAP:
        if pattern == rel_posix:
            return True
        # Directory prefix match
        if pattern.endswith("/") and rel_posix.startswith(pattern):
            return True
    return False


def _get_mapped_docs(rel_posix):
    """Return the union of doc lists for all CODE_DOC_MAP entries covering
    this path (exact + prefix matches)."""
    docs = []
    for pattern, doc_list in CODE_DOC_MAP.items():
        if pattern == rel_posix or (pattern.endswith("/") and rel_posix.startswith(pattern)):
            docs.extend(doc_list)
    return docs


# ── Tests ────────────────────────────────────────────────────────────────


def test_all_significant_modules_in_code_doc_map():
    """AC3: every agent/**/*.py with >30 significant lines must be in CODE_DOC_MAP."""
    missing = []
    for rel_posix, abs_path in _enumerate_agent_modules():
        sig = _count_significant_lines(abs_path)
        if sig > SIGNIFICANT_LINE_THRESHOLD:
            if not _is_covered_by_map(rel_posix):
                missing.append(f"{rel_posix} ({sig} sig lines)")

    assert not missing, (
        f"CODE_DOC_MAP is missing {len(missing)} module(s) with >{SIGNIFICANT_LINE_THRESHOLD} "
        f"significant lines:\n  " + "\n  ".join(missing)
    )


def test_route_modules_map_to_api_docs():
    """AC4: modules with HTTP route patterns must map to at least one docs/api/*.md."""
    violations = []
    for rel_posix, abs_path in _enumerate_agent_modules():
        if _has_route_patterns(abs_path):
            mapped_docs = _get_mapped_docs(rel_posix)
            has_api_doc = any(d.startswith("docs/api/") and d.endswith(".md") for d in mapped_docs)
            if not has_api_doc:
                violations.append(rel_posix)

    assert not violations, (
        f"Route-containing module(s) missing docs/api/*.md mapping:\n  "
        + "\n  ".join(violations)
    )


def test_stale_entries_warn():
    """AC5: stale CODE_DOC_MAP entries (source file gone) emit warnings, not failures."""
    stale = []
    for pattern in CODE_DOC_MAP:
        # Directory prefixes: check directory exists
        if pattern.endswith("/"):
            if not (PROJECT_ROOT / pattern).is_dir():
                stale.append(pattern)
        else:
            if not (PROJECT_ROOT / pattern).is_file():
                stale.append(pattern)

    for entry in stale:
        warnings.warn(
            f"CODE_DOC_MAP entry '{entry}' refers to a source path that no longer exists",
            stacklevel=1,
        )
    # Explicitly pass — stale entries are warnings, not failures
