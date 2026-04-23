"""Lint guardrail: scan agent/tests/*.py for test isolation violations.

AC6: Detects hardcoded 'aming-claw' project_id in SQL mutation statements.
AC7: Detects direct sqlite3.connect() calls to file-based governance.db paths.
"""

import ast
import os
import re
import glob


_TESTS_DIR = os.path.dirname(__file__)


_SELF = os.path.abspath(__file__)


def _get_test_files():
    """Return all *.py files under agent/tests/, excluding this guardrail file."""
    return [
        f for f in glob.glob(os.path.join(_TESTS_DIR, "*.py"))
        if os.path.abspath(f) != _SELF
    ]


# ---------------------------------------------------------------------------
# AC6: No hardcoded 'aming-claw' in SQL mutation statements
# ---------------------------------------------------------------------------

def test_no_hardcoded_project_id_in_sql_mutations():
    """Scan agent/tests/*.py for 'aming-claw' appearing in INSERT/UPDATE/DELETE SQL."""
    # Pattern: a SQL mutation keyword followed (possibly with intervening text) by 'aming-claw'
    # We check each line that contains a SQL mutation keyword for the literal project id.
    sql_mutation_re = re.compile(r"\b(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
    violations = []

    for filepath in _get_test_files():
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, start=1):
                if sql_mutation_re.search(line) and "aming-claw" in line:
                    violations.append(f"{os.path.basename(filepath)}:{lineno}: {line.strip()}")

    assert not violations, (
        "Hardcoded 'aming-claw' project_id found in SQL mutation statements:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# AC7: No direct sqlite3.connect() to file-based governance.db
# ---------------------------------------------------------------------------

def test_no_direct_sqlite3_connect_to_governance_db():
    """Scan agent/tests/*.py for sqlite3.connect() calls targeting governance.db files.

    Allowed: sqlite3.connect(':memory:')
    Forbidden: sqlite3.connect('path/to/governance.db') or similar file-based paths.
    """
    # Match sqlite3.connect(...) where the argument contains 'governance.db'
    # but is NOT ':memory:'
    connect_re = re.compile(r"""sqlite3\.connect\s*\(\s*['"](?!:memory:)([^'"]*governance\.db[^'"]*)['"]""")
    violations = []

    for filepath in _get_test_files():
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, start=1):
                match = connect_re.search(line)
                if match:
                    violations.append(
                        f"{os.path.basename(filepath)}:{lineno}: "
                        f"sqlite3.connect('{match.group(1)}')"
                    )

    assert not violations, (
        "Direct sqlite3.connect() to file-based governance.db found:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Bonus: Verify _make_in_memory_db is no longer defined in migrated files
# ---------------------------------------------------------------------------

def test_no_local_make_in_memory_db_in_migrated_files():
    """Verify that migrated test files no longer define _make_in_memory_db."""
    migrated_files = [
        "test_auto_chain_routing.py",
        "test_auto_chain_related_nodes.py",
    ]
    define_re = re.compile(r"^\s*def\s+_make_in_memory_db\s*\(")
    violations = []

    for filename in migrated_files:
        filepath = os.path.join(_TESTS_DIR, filename)
        if not os.path.exists(filepath):
            continue
        with open(filepath, encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                if define_re.match(line):
                    violations.append(f"{filename}:{lineno}")

    assert not violations, (
        "_make_in_memory_db() still defined in migrated files:\n"
        + "\n".join(violations)
    )
