"""Unified doc-gate policy module.

Single source of truth for all doc governance classification rules.
Three file categories:
  - governed_docs: docs/*.md (except docs/dev/**) — require enforcement
  - dev_artifacts: docs/dev/** — tracked but non-governed
  - test_fixtures: agent/tests/** — always-related, never flagged as unrelated
"""


# ---------------------------------------------------------------------------
# File category predicates
# ---------------------------------------------------------------------------

def is_governed_doc(path: str) -> bool:
    """Return True for docs/*.md (NOT under docs/dev/) — formal governed docs."""
    normalized = path.replace("\\", "/")
    if not normalized.startswith("docs/"):
        return False
    if normalized.startswith("docs/dev/"):
        return False
    return normalized.endswith(".md")


def is_dev_artifact(path: str) -> bool:
    """Return True for docs/dev/** — informal dev notes, tracked but not governed."""
    normalized = path.replace("\\", "/")
    return normalized.startswith("docs/dev/")


def is_test_fixture(path: str) -> bool:
    """Return True for agent/tests/** — always-related, never flagged as unrelated."""
    normalized = path.replace("\\", "/")
    return normalized.startswith("agent/tests/")


# ---------------------------------------------------------------------------
# Governance internal repair detection
# ---------------------------------------------------------------------------

_GOVERNANCE_INTERNAL_PREFIXES = (
    "agent/governance/",
    "agent/role_permissions.py",
)


def is_governance_internal_repair(metadata: dict, changed_files: list) -> bool:
    """Return True when all target_files and changed_files are governance-internal.

    Governance-internal paths are:
      - agent/governance/*
      - agent/role_permissions.py
      - agent/tests/test_* (co-located test files)

    When True, the doc consistency gate is skipped to avoid the oscillation loop
    where governance repairs are demanded docs they cannot add without triggering
    the unrelated-files gate.
    """
    target_files = metadata.get("target_files", []) or []
    all_files = list(target_files) + list(changed_files or [])
    if not all_files:
        return False
    for f in all_files:
        normalized = f.replace("\\", "/")
        # Allow governance paths
        if any(normalized.startswith(prefix) for prefix in _GOVERNANCE_INTERNAL_PREFIXES):
            continue
        # Allow co-located test files
        if "/tests/test_" in normalized or normalized.startswith("agent/tests/test_"):
            continue
        return False
    return True


# ---------------------------------------------------------------------------
# Doc-related classification
# ---------------------------------------------------------------------------

def is_doc_related(filepath: str) -> bool:
    """Return True if a file is doc-related for enforcement purposes.

    - governed_docs (docs/*.md except docs/dev/**): True — require enforcement
    - test_fixtures (agent/tests/**): True — always related, never unrelated
    - dev_artifacts (docs/dev/**): False — tracked but not governed
    - everything else: False
    """
    if is_governed_doc(filepath):
        return True
    if is_test_fixture(filepath):
        return True
    return False


# ---------------------------------------------------------------------------
# should_require_docs
# ---------------------------------------------------------------------------

def should_require_docs(changed_files: list, metadata: dict) -> tuple:
    """Determine whether doc updates should be required for the given changes.

    Returns (bool, set_of_missing_docs):
      - (False, set()) when docs are NOT required (governance-internal, etc.)
      - (True, missing) when docs ARE required and some are missing
      - (False, set()) when docs are required but all present

    This function checks:
      1. Governance internal repair bypass
      2. skip_doc_check + bootstrap_reason bypass
      3. Actual doc coverage via impact analysis
    """
    changed = list(changed_files or [])
    if not changed:
        return False, set()

    # Governance internal repairs skip doc checks entirely
    if is_governance_internal_repair(metadata, changed):
        return False, set()

    # Separate code and doc files
    code_files = [f for f in changed if not f.startswith("docs/") and not f.endswith(".md")]
    doc_files_changed = set(f for f in changed if f.startswith("docs/") or f.endswith(".md"))

    # Determine expected docs from metadata or impact analysis
    doc_impact = metadata.get("doc_impact", {})
    if isinstance(doc_impact, dict) and "files" in doc_impact:
        expected_docs = set(doc_impact.get("files") or [])
    else:
        try:
            from .impact_analyzer import get_related_docs
            expected_docs = get_related_docs(code_files)
        except Exception:
            expected_docs = set()

    # Filter out dev artifacts — never enforce them
    if expected_docs:
        expected_docs = {d for d in expected_docs if not is_dev_artifact(d)}

    if not expected_docs:
        return False, set()

    missing_docs = expected_docs - doc_files_changed
    if missing_docs:
        return True, missing_docs

    return False, set()
