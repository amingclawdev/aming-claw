"""Phase D --- Doc drift heuristic (REPORT ONLY).

Scans docs/**/*.md for:
1. Stale code references — mentions of .py files that no longer exist on disk
   (grace_period defaults to 14 days based on file mtime).
2. Missing required keywords — docs that lack any of a configurable keyword set.

Emits discrepancy types ``doc_stale`` and ``doc_missing_known_keyword``.
Phase D NEVER auto-fixes regardless of any flag.
"""
from __future__ import annotations

import os
import re
import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .context import ReconcileContext

log = logging.getLogger(__name__)

# --- constants ---------------------------------------------------------------
DEFAULT_GRACE_PERIOD_DAYS = 14
REQUIRED_KEYWORDS = frozenset({"## Overview", "## API", "## Usage"})
_PY_REF_RE = re.compile(r'`([a-zA-Z0-9_/\\]+\.py)`')


# --- core algorithm ----------------------------------------------------------

def _scan_docs(
    workspace_path: str,
    grace_period_days: int = DEFAULT_GRACE_PERIOD_DAYS,
    required_keywords: Optional[frozenset] = None,
) -> List[Dict[str, Any]]:
    """Walk docs/**/*.md and return raw findings."""
    if required_keywords is None:
        required_keywords = REQUIRED_KEYWORDS

    docs_root = Path(workspace_path) / "docs"
    if not docs_root.is_dir():
        return []

    now = time.time()
    grace_seconds = grace_period_days * 86400
    findings: List[Dict[str, Any]] = []

    for md_path in sorted(docs_root.rglob("*.md")):
        try:
            content = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rel = str(md_path.relative_to(Path(workspace_path))).replace("\\", "/")

        # 1. Stale code references
        py_refs = _PY_REF_RE.findall(content)
        for ref in py_refs:
            ref_path = Path(workspace_path) / ref.replace("\\", "/")
            if not ref_path.exists():
                # Check grace period via doc file mtime
                try:
                    mtime = md_path.stat().st_mtime
                except OSError:
                    mtime = 0
                age = now - mtime
                if age > grace_seconds:
                    findings.append({
                        "type": "doc_stale",
                        "doc": rel,
                        "ref": ref,
                        "age_days": int(age / 86400),
                    })

        # 2. Missing required keywords
        missing = [kw for kw in sorted(required_keywords) if kw not in content]
        if missing:
            findings.append({
                "type": "doc_missing_known_keyword",
                "doc": rel,
                "missing": missing,
            })

    return findings


def run(
    ctx: "ReconcileContext",
    *,
    grace_period_days: int = DEFAULT_GRACE_PERIOD_DAYS,
    required_keywords: Optional[frozenset] = None,
) -> list:
    """Run Phase D doc drift scan. REPORT ONLY — never auto-fixes."""
    from . import Discrepancy

    findings = _scan_docs(
        ctx.workspace_path,
        grace_period_days=grace_period_days,
        required_keywords=required_keywords,
    )

    results: list = []
    for f in findings:
        if f["type"] == "doc_stale":
            results.append(Discrepancy(
                type="doc_stale",
                node_id=None,
                field=None,
                detail=f"doc={f['doc']} ref={f['ref']} age_days={f['age_days']}",
                confidence="medium",
            ))
        elif f["type"] == "doc_missing_known_keyword":
            results.append(Discrepancy(
                type="doc_missing_known_keyword",
                node_id=None,
                field=None,
                detail=f"doc={f['doc']} missing={f['missing']}",
                confidence="low",
            ))

    return results
