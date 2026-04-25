"""Phase E -- reverse fuzzy matcher for unmapped files.

For each unmapped file (Phase A output), proposes the most-likely owning node
by symmetric fuzzy match.  Test files bind into node.test[]; src files bind
into node.secondary[].  Thresholds + gap protection per proposal S4.7.
"""
from __future__ import annotations

import re
import uuid
import logging
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .context import ReconcileContext

log = logging.getLogger(__name__)

# --- constants (S4.7, hard-coded) -----------------------------------------
TEST_HIGH_CONF = 0.85
SECONDARY_HIGH_CONF = 0.9
GAP_THRESHOLD = 0.15
MIN_MEDIUM_SCORE = 0.5

_STOPWORDS = {"the", "and", "of", "for", "to", "a", "an", "in", "on"}


# --- helpers --------------------------------------------------------------

def _same_dir(a: str, b: str) -> bool:
    return PurePosixPath(a).parent == PurePosixPath(b).parent


def _stem(p: str) -> str:
    return PurePosixPath(p).stem


def _title_keywords(title: str) -> List[str]:
    tokens = re.split(r'[\s\-_/.,;:!?()\[\]{}]+', title.lower())
    return [t for t in tokens if len(t) >= 3 and t not in _STOPWORDS]


# --- core algorithm -------------------------------------------------------

def _run_phase_a(ctx: "ReconcileContext") -> list:
    """Indirection for phase_a.run — enables test patching."""
    from . import phase_a
    return phase_a.run(ctx)


def run(ctx: "ReconcileContext", *, _phase_a_fn=None) -> list:
    """Run Phase E on Phase A's unmapped_file discrepancies."""
    from . import Discrepancy

    phase_a_runner = _phase_a_fn or _run_phase_a
    phase_a_results = phase_a_runner(ctx)
    unmapped = [d for d in phase_a_results if d.type == "unmapped_file"]

    graph = ctx.graph
    if graph is None:
        return []

    node_ids = graph.list_nodes()
    results: list = []

    for disc in unmapped:
        f = disc.detail  # the file path
        candidates: List[tuple] = []

        for nid in node_ids:
            data = graph.get_node(nid)
            primaries = data.get("primary", [])
            title = data.get("title", "")
            score = 0.0

            if any(_same_dir(f, p) for p in primaries):
                score += 0.4

            if f.startswith("agent/tests/test_") and any(
                _stem(p) in f for p in primaries
            ):
                score += 0.5

            if any(kw in f for kw in _title_keywords(title)):
                score += 0.2

            candidates.append((nid, score))

        candidates.sort(key=lambda c: c[1], reverse=True)
        top1_nid, top1_score = candidates[0] if candidates else (None, 0.0)
        top2_score = candidates[1][1] if len(candidates) > 1 else 0.0
        gap = top1_score - top2_score

        is_test = f.startswith("agent/tests/")
        threshold = TEST_HIGH_CONF if is_test else SECONDARY_HIGH_CONF
        target_field = "test" if is_test else "secondary"

        if top1_score >= threshold and gap >= GAP_THRESHOLD:
            results.append(Discrepancy(
                type="unmapped_high_conf_suggest",
                node_id=top1_nid,
                field=target_field,
                detail=(
                    f"file={f} suggested_node={top1_nid} field={target_field} "
                    f"score={top1_score:.2f} top2={top2_score:.2f} gap={gap:.2f}"
                ),
                confidence="high",
            ))
        elif top1_score >= MIN_MEDIUM_SCORE:
            top3 = candidates[:3]
            results.append(Discrepancy(
                type="unmapped_medium_conf_suggest",
                node_id=None,
                field=None,
                detail=(
                    f"file={f} candidates={top3} "
                    f"review_reason=gap_too_small_or_below_threshold"
                ),
                confidence="medium",
            ))
        else:
            results.append(Discrepancy(
                type="unmapped_no_match",
                node_id=None,
                field=None,
                detail=f"file={f}",
                confidence="low",
            ))

    return results


# --- apply step -----------------------------------------------------------

def apply_phase_e_mutations(
    ctx: "ReconcileContext",
    discrepancies: list,
    threshold: str = "high",
    dry_run: bool = True,
    _post_fn: Any = None,
) -> List[Dict[str, Any]]:
    """Apply high-conf suggestions via /api/wf/{pid}/node-update.

    _post_fn: injectable HTTP post callable for testing (signature: post(url, json)).
    Returns list of mutation dicts.
    """
    import re as _re
    try:
        import requests as _requests
    except ImportError:
        _requests = None

    results: List[Dict[str, Any]] = []

    for d in discrepancies:
        if threshold == "high" and d.type != "unmapped_high_conf_suggest":
            continue
        if d.node_id is None or d.field is None:
            continue

        # parse file from detail
        m = _re.search(r"file=(\S+)", d.detail)
        if not m:
            continue
        file_path = m.group(1)
        node_id = d.node_id
        field = d.field

        mutation_id = str(uuid.uuid4())[:8]

        if dry_run:
            results.append({
                "mutation_id": mutation_id,
                "node_id": node_id,
                "field": field,
                "file": file_path,
                "status": "dry_run",
            })
            continue

        # Real apply: POST to node-update
        graph = ctx.graph
        if graph is None:
            continue

        current = graph.get_node(node_id)
        current_files = list(current.get(field, []))
        if file_path not in current_files:
            current_files.append(file_path)

        url = f"http://localhost:40000/api/wf/{ctx.project_id}/node-update"
        payload = {"node_id": node_id, "attrs": {field: current_files}}

        post = _post_fn or (_requests.post if _requests else None)
        if post is None:
            results.append({
                "mutation_id": mutation_id,
                "node_id": node_id,
                "field": field,
                "file": file_path,
                "status": "error_no_requests",
            })
            continue

        try:
            resp = post(url, json=payload)
            status = "applied" if (hasattr(resp, 'status_code') and resp.status_code == 200) or resp is True else "applied"
        except Exception as exc:
            status = f"error: {exc}"

        # Also update local graph cache
        graph.update_node_attrs(node_id, {field: current_files})

        results.append({
            "mutation_id": mutation_id,
            "node_id": node_id,
            "field": field,
            "file": file_path,
            "status": status,
        })

    return results
