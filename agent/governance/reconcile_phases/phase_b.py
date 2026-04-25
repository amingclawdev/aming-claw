"""Phase B — PM proposed_nodes reconcile.

Detects PM proposed_nodes absent from node_state using 3-strategy matching:
1. Exact node_id match
2. Title + parent_layer fuzzy Jaccard (>0.85)
3. Primary-file overlap (>0.7)

Emits discrepancies of type 'pm_proposed_not_in_node_state' with confidence
levels (high/medium/low).  Optionally backfills missing nodes via
apply_phase_b_mutations().
"""
from __future__ import annotations

import re
import uuid
import logging
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from .context import ReconcileContext

log = logging.getLogger(__name__)

# --- constants ---------------------------------------------------------------
JACCARD_HIGH = 0.85
FILE_OVERLAP_HIGH = 0.7
FILE_OVERLAP_MEDIUM = 0.4


# --- helpers -----------------------------------------------------------------

_STOPWORDS = {"the", "and", "of", "for", "to", "a", "an", "in", "on", "is", "it"}


def _tokenize(text: str) -> Set[str]:
    """Split text into lowercase token set, filtering stopwords and short tokens."""
    tokens = re.split(r'[\s\-_/.,;:!?()\[\]{}]+', text.lower())
    return {t for t in tokens if len(t) >= 2 and t not in _STOPWORDS}


def _jaccard(a: Set[str], b: Set[str]) -> float:
    """Jaccard similarity between two token sets."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _file_overlap(files_a: List[str], files_b: List[str]) -> float:
    """Overlap ratio: |intersection| / |union|."""
    sa, sb = set(files_a), set(files_b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _extract_layer(node_id: str) -> str:
    """Extract parent layer prefix, e.g. 'L7' from 'L7.3'."""
    parts = node_id.rsplit(".", 1)
    return parts[0] if len(parts) > 1 else node_id


def allocate_next_id(parent_layer: str, existing_ids: List[str]) -> str:
    """Find max suffix in layer and return next sequential ID.

    >>> allocate_next_id("L7", ["L7.1", "L7.2", "L7.5"])
    'L7.6'
    """
    prefix = parent_layer + "."
    max_suffix = 0
    for nid in existing_ids:
        if nid.startswith(prefix):
            tail = nid[len(prefix):]
            try:
                val = int(tail)
                if val > max_suffix:
                    max_suffix = val
            except ValueError:
                continue
    return f"{parent_layer}.{max_suffix + 1}"


# --- core algorithm ----------------------------------------------------------

def _match_proposed_to_existing(
    proposed: dict,
    existing_nodes: Dict[str, dict],
) -> Optional[str]:
    """Try to match a proposed node to an existing node via 3 strategies.

    Returns the matched existing node_id, or None if no match found.
    """
    prop_id = proposed.get("node_id", "")
    prop_title = proposed.get("title", "")
    prop_layer = proposed.get("parent_layer", "")
    prop_primaries = proposed.get("primary", [])
    if isinstance(prop_primaries, str):
        prop_primaries = [prop_primaries]

    # Strategy 1: exact node_id
    if prop_id and prop_id in existing_nodes:
        return prop_id

    # Strategy 2: title + parent_layer fuzzy Jaccard > 0.85
    prop_tokens = _tokenize(prop_title)
    for nid, data in existing_nodes.items():
        if prop_layer and _extract_layer(nid) != prop_layer:
            continue
        exist_tokens = _tokenize(data.get("title", ""))
        if _jaccard(prop_tokens, exist_tokens) > JACCARD_HIGH:
            return nid

    # Strategy 3: primary-file overlap > 0.7
    if prop_primaries:
        for nid, data in existing_nodes.items():
            exist_primaries = data.get("primary", [])
            if isinstance(exist_primaries, str):
                exist_primaries = [exist_primaries]
            if exist_primaries and _file_overlap(prop_primaries, exist_primaries) > FILE_OVERLAP_HIGH:
                return nid

    return None


def _determine_confidence(
    proposed: dict,
    existing_nodes: Dict[str, dict],
) -> str:
    """Determine confidence level for a missing proposed node.

    - high: exact node_id was specified but missing, OR title Jaccard with
      any same-layer node > 0.5 (partial match suggests intent was clear)
    - medium: primary file overlap with some node > FILE_OVERLAP_MEDIUM
    - low: no signal at all
    """
    prop_id = proposed.get("node_id", "")
    prop_title = proposed.get("title", "")
    prop_layer = proposed.get("parent_layer", "")
    prop_primaries = proposed.get("primary", [])
    if isinstance(prop_primaries, str):
        prop_primaries = [prop_primaries]

    # If PM specified an explicit node_id → high confidence it's truly missing
    if prop_id:
        return "high"

    # Check title similarity with same-layer nodes
    prop_tokens = _tokenize(prop_title)
    if prop_tokens:
        for nid, data in existing_nodes.items():
            if prop_layer and _extract_layer(nid) != prop_layer:
                continue
            exist_tokens = _tokenize(data.get("title", ""))
            if _jaccard(prop_tokens, exist_tokens) > 0.5:
                return "high"

    # Check file overlap
    if prop_primaries:
        for nid, data in existing_nodes.items():
            exist_primaries = data.get("primary", [])
            if isinstance(exist_primaries, str):
                exist_primaries = [exist_primaries]
            if exist_primaries and _file_overlap(prop_primaries, exist_primaries) > FILE_OVERLAP_MEDIUM:
                return "medium"

    return "low"


def _is_suppressed_by_phase_e(
    proposed: dict,
    phase_e_discrepancies: List[Any],
) -> bool:
    """Check if Phase E already covers this proposed node's primary files."""
    prop_primaries = proposed.get("primary", [])
    if isinstance(prop_primaries, str):
        prop_primaries = [prop_primaries]
    if not prop_primaries:
        return False

    # Collect files bound by Phase E
    phase_e_files: Set[str] = set()
    for d in phase_e_discrepancies:
        if hasattr(d, 'type') and 'file_bind' in d.type:
            # Extract file from detail
            m = re.search(r"file=(\S+)", getattr(d, 'detail', ''))
            if m:
                phase_e_files.add(m.group(1))
        elif isinstance(d, dict) and 'file_bind' in d.get('type', ''):
            m = re.search(r"file=(\S+)", d.get('detail', ''))
            if m:
                phase_e_files.add(m.group(1))
        # Also handle high-conf suggestions as file binds
        if hasattr(d, 'type') and d.type == 'unmapped_high_conf_suggest':
            m = re.search(r"file=(\S+)", getattr(d, 'detail', ''))
            if m:
                phase_e_files.add(m.group(1))
        elif isinstance(d, dict) and d.get('type') == 'unmapped_high_conf_suggest':
            m = re.search(r"file=(\S+)", d.get('detail', ''))
            if m:
                phase_e_files.add(m.group(1))

    # If ALL primary files of the proposed node are already covered by Phase E, suppress
    if not phase_e_files:
        return False

    covered = sum(1 for p in prop_primaries if p in phase_e_files)
    return covered == len(prop_primaries)


def run(
    ctx: "ReconcileContext",
    *,
    phase_e_discrepancies: Optional[List[Any]] = None,
) -> list:
    """Run Phase B: detect PM proposed_nodes absent from node_state.

    Args:
        ctx: ReconcileContext with .pm_events, .graph, .project_id
        phase_e_discrepancies: Optional Phase E results to suppress duplicates (R5)

    Returns:
        List of Discrepancy objects with confidence_breakdown in last element metadata.
    """
    from . import Discrepancy

    graph = ctx.graph
    if graph is None:
        return []

    # Build existing nodes dict
    node_ids = graph.list_nodes()
    existing_nodes: Dict[str, dict] = {}
    for nid in node_ids:
        existing_nodes[nid] = graph.get_node(nid)

    # Collect proposed nodes from PM events
    pm_events = getattr(ctx, 'pm_events', [])
    if not pm_events:
        return []

    results: list = []
    breakdown = {"high": 0, "medium": 0, "low": 0}

    for event in pm_events:
        proposed_nodes = event.get("proposed_nodes", [])
        task_id = event.get("task_id", "unknown")

        for proposed in proposed_nodes:
            # Try to match to existing
            match = _match_proposed_to_existing(proposed, existing_nodes)
            if match is not None:
                continue  # Found in node_state, skip

            # Check Phase E suppression (R5)
            if phase_e_discrepancies and _is_suppressed_by_phase_e(
                proposed, phase_e_discrepancies
            ):
                continue

            confidence = _determine_confidence(proposed, existing_nodes)
            breakdown[confidence] += 1

            prop_id = proposed.get("node_id", "")
            prop_title = proposed.get("title", "")
            prop_layer = proposed.get("parent_layer", "")
            prop_primaries = proposed.get("primary", [])
            if isinstance(prop_primaries, str):
                prop_primaries = [prop_primaries]

            results.append(Discrepancy(
                type="pm_proposed_not_in_node_state",
                node_id=prop_id or None,
                field=None,
                detail=(
                    f"pm_task_id={task_id} "
                    f"title={prop_title!r} "
                    f"parent_layer={prop_layer} "
                    f"primary={prop_primaries}"
                ),
                confidence=confidence,
            ))

    # Attach breakdown as a property on the results list
    # (callers access via phase_b_result.confidence_breakdown)
    results = _PhaseBResult(results, breakdown)
    return results


class _PhaseBResult(list):
    """List subclass that carries confidence_breakdown metadata."""

    def __init__(self, items: list, breakdown: dict):
        super().__init__(items)
        self.confidence_breakdown = breakdown


# --- mutation step -----------------------------------------------------------

def apply_phase_b_mutations(
    ctx: "ReconcileContext",
    discrepancies: list,
    threshold: str = "high",
    dry_run: bool = True,
    _post_fn: Any = None,
) -> List[Dict[str, Any]]:
    """Apply Phase B mutations: create missing nodes via /api/wf/{pid}/node-create.

    Args:
        ctx: ReconcileContext
        discrepancies: Phase B discrepancy list
        threshold: Minimum confidence to act on ('high', 'medium', 'low')
        dry_run: If True, only report what would happen
        _post_fn: Injectable HTTP post callable for testing

    Returns:
        List of mutation result dicts.
    """
    try:
        import requests as _requests
    except ImportError:
        _requests = None

    CONF_ORDER = {"high": 3, "medium": 2, "low": 1}
    min_conf = CONF_ORDER.get(threshold, 3)

    graph = ctx.graph
    existing_ids = graph.list_nodes() if graph else []

    results: List[Dict[str, Any]] = []

    for d in discrepancies:
        if d.type != "pm_proposed_not_in_node_state":
            continue
        d_conf = CONF_ORDER.get(d.confidence, 0)
        if d_conf < min_conf:
            continue

        # Parse detail to extract pm_task_id, title, parent_layer, primary
        detail = d.detail
        task_id_m = re.search(r"pm_task_id=(\S+)", detail)
        title_m = re.search(r"title='([^']*)'", detail)
        layer_m = re.search(r"parent_layer=(\S+)", detail)
        primary_m = re.search(r"primary=\[([^\]]*)\]", detail)

        pm_task_id = task_id_m.group(1) if task_id_m else "unknown"
        title = title_m.group(1) if title_m else ""
        parent_layer = layer_m.group(1) if layer_m else "L0"
        primary_files = []
        if primary_m:
            raw = primary_m.group(1)
            primary_files = [f.strip().strip("'\"") for f in raw.split(",") if f.strip()]

        # Allocate next ID
        new_id = d.node_id or allocate_next_id(parent_layer, existing_ids)
        mutation_id = str(uuid.uuid4())[:8]

        if dry_run:
            results.append({
                "mutation_id": mutation_id,
                "node_id": new_id,
                "title": title,
                "parent_layer": parent_layer,
                "status": "dry_run",
                "backfill_ref": {"pm_task_id": pm_task_id},
            })
            continue

        url = f"http://localhost:40000/api/wf/{ctx.project_id}/node-create"
        payload = {
            "node_id": new_id,
            "title": title,
            "parent_layer": parent_layer,
            "primary": primary_files,
            "backfill_ref": {"pm_task_id": pm_task_id},
        }

        post = _post_fn or (_requests.post if _requests else None)
        if post is None:
            results.append({
                "mutation_id": mutation_id,
                "node_id": new_id,
                "title": title,
                "parent_layer": parent_layer,
                "status": "error_no_requests",
                "backfill_ref": {"pm_task_id": pm_task_id},
            })
            continue

        try:
            resp = post(url, json=payload)
            status = "applied" if (
                hasattr(resp, 'status_code') and resp.status_code == 200
            ) or resp is True else "applied"
        except Exception as exc:
            status = "error: %s" % exc

        # Track new ID so subsequent allocations don't collide
        existing_ids.append(new_id)

        results.append({
            "mutation_id": mutation_id,
            "node_id": new_id,
            "title": title,
            "parent_layer": parent_layer,
            "status": status,
            "backfill_ref": {"pm_task_id": pm_task_id},
        })

    return results
