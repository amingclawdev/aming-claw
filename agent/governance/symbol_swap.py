"""Atomic graph.json swap operation with deterministic smoke validation.

This module replaces the V5-era staged migration_state_machine. The new
contract (spec §4.4 v6 / GPT R4) is:

* :func:`atomic_swap` — single atomic rename pair with an in-process
  rollback fallback when the new graph fails :func:`smoke_validate`.
* :func:`smoke_validate` — deterministic JSON-only validation: parses,
  unique node_ids, layer in L0..L7, every primary path exists on disk.
* :func:`rollback` — restore the previous ``graph.json`` from the
  ``.json.bak`` written by :func:`atomic_swap`, refusing to restore a
  backup older than :data:`BAK_RETENTION_DAYS`.

Spec reference: docs/governance/reconcile-workflow.md §8 (Atomic Swap)
and §10 (Rollback).
"""
from __future__ import annotations

import json
import pathlib
import shutil
import time
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

BAK_RETENTION_DAYS: int = 30
"""Maximum age of a ``.json.bak`` that :func:`rollback` will restore."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_VALID_LAYERS = {"L0", "L1", "L2", "L3", "L4", "L5", "L6", "L7"}


def _bak_path(graph_path: pathlib.Path) -> pathlib.Path:
    """Return the ``.json.bak`` sibling for a given graph path."""
    return graph_path.with_suffix(".json.bak")


def _layer_of(node: Any) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    layer = node.get("layer") or node.get("parent_layer")
    if isinstance(layer, str):
        return layer
    return None


def _primary_paths(node: Any) -> List[str]:
    if not isinstance(node, dict):
        return []
    primary = node.get("primary")
    if primary is None:
        primary = node.get("primary_files")
    if primary is None:
        return []
    if isinstance(primary, str):
        return [primary]
    if isinstance(primary, (list, tuple)):
        return [str(p) for p in primary]
    return []


def _iter_nodes(graph: Any) -> List[Dict[str, Any]]:
    if not isinstance(graph, dict):
        return []
    raw = graph.get("nodes")
    if isinstance(raw, dict):
        return [n for n in raw.values() if isinstance(n, dict)]
    if isinstance(raw, list):
        return [n for n in raw if isinstance(n, dict)]
    return []


# ---------------------------------------------------------------------------
# smoke_validate
# ---------------------------------------------------------------------------

def smoke_validate(graph_path: pathlib.Path) -> Dict[str, Any]:
    """Deterministic post-swap validation — NO AI / NO network.

    Checks (in order):

    1. file exists and parses as JSON;
    2. ``nodes`` key resolves to a list / dict of dicts;
    3. every node has a unique ``node_id`` (or ``id``);
    4. every node's ``layer`` is in L0..L7 inclusive;
    5. every primary path on every node exists on the local filesystem.

    The function NEVER raises — it always returns a dict with ``ok``
    (bool) and ``reason`` (string) keys, plus the failure-specific lists
    (``duplicates``, ``bad_layers``, ``missing_paths``).

    Calling :func:`smoke_validate` twice on the same input always returns
    the same result (deterministic).
    """
    graph_path = pathlib.Path(graph_path)
    result: Dict[str, Any] = {
        "ok": False,
        "reason": "",
        "duplicates": [],
        "bad_layers": [],
        "missing_paths": [],
    }

    if not graph_path.exists():
        result["reason"] = f"graph file not found: {graph_path}"
        return result

    try:
        with graph_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        result["reason"] = f"failed to parse JSON: {exc}"
        return result

    nodes = _iter_nodes(data)

    # 1. unique node_ids
    seen: Dict[str, int] = {}
    duplicates: List[str] = []
    for node in nodes:
        nid = node.get("node_id") or node.get("id")
        if not isinstance(nid, str) or not nid:
            continue
        seen[nid] = seen.get(nid, 0) + 1
        if seen[nid] == 2:
            duplicates.append(nid)
    if duplicates:
        result["duplicates"] = sorted(duplicates)
        result["reason"] = f"duplicate node_ids: {duplicates}"
        return result

    # 2. layers within L0..L6
    bad_layers: List[Dict[str, str]] = []
    for node in nodes:
        layer = _layer_of(node)
        if layer is None:
            continue  # missing layer is tolerated; layer-presence is a separate gate
        if layer not in _VALID_LAYERS:
            nid = node.get("node_id") or node.get("id") or ""
            bad_layers.append({"node_id": str(nid), "layer": layer})
    if bad_layers:
        result["bad_layers"] = bad_layers
        result["reason"] = f"layers outside L0-L7: {bad_layers}"
        return result

    # 3. primary paths exist on filesystem
    missing: List[Dict[str, str]] = []
    for node in nodes:
        for p in _primary_paths(node):
            path = pathlib.Path(p)
            if not path.exists():
                # try relative to graph file's parent for typical repo layout
                rel = graph_path.parent / p
                if not rel.exists():
                    nid = node.get("node_id") or node.get("id") or ""
                    missing.append({"node_id": str(nid), "primary": p})
    if missing:
        result["missing_paths"] = missing
        result["reason"] = f"missing primary paths: {len(missing)} entries"
        return result

    result["ok"] = True
    result["reason"] = "smoke_validate ok"
    return result


# ---------------------------------------------------------------------------
# atomic_swap
# ---------------------------------------------------------------------------

def atomic_swap(
    graph_path: pathlib.Path,
    candidate_path: pathlib.Path,
    *,
    observer_alert: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Atomically swap *candidate_path* into *graph_path*.

    Sequence:

    1. ``shutil.move(graph_path, graph_path.json.bak)`` (existing graph
       backed up).
    2. ``shutil.move(candidate_path, graph_path)`` (candidate becomes the
       canonical graph).
    3. :func:`smoke_validate` is called on the new ``graph_path``.

    On smoke validation failure, the function auto-rolls-back: it restores
    the ``.json.bak`` to ``graph_path`` and moves the candidate back to
    ``candidate_path`` (so no work is lost). The optional ``observer_alert``
    callable is invoked exactly once with a ``{ok: False, reason: str}``
    dict.

    Returns a dict with ``ok`` (bool), ``reason`` (str), and ``rolled_back``
    (bool). Never raises on validation failure — only on impossible I/O
    failures (e.g. permission denied) which are bubbled up unchanged.
    """
    graph_path = pathlib.Path(graph_path)
    candidate_path = pathlib.Path(candidate_path)
    bak_path = _bak_path(graph_path)

    if not candidate_path.exists():
        info = {"ok": False, "reason": f"candidate path not found: {candidate_path}"}
        if observer_alert is not None:
            observer_alert(info)
        return {"ok": False, "reason": info["reason"], "rolled_back": False}

    if not graph_path.exists():
        info = {"ok": False, "reason": f"graph path not found: {graph_path}"}
        if observer_alert is not None:
            observer_alert(info)
        return {"ok": False, "reason": info["reason"], "rolled_back": False}

    # Step 1 + 2: move
    shutil.move(str(graph_path), str(bak_path))
    try:
        shutil.move(str(candidate_path), str(graph_path))
    except Exception:
        # restore the backup if step 2 itself fails
        shutil.move(str(bak_path), str(graph_path))
        raise

    # Step 3: smoke validate the freshly moved graph
    validation = smoke_validate(graph_path)
    if validation.get("ok"):
        return {"ok": True, "reason": validation.get("reason", "ok"), "rolled_back": False}

    # Auto-rollback
    # Move new graph back to candidate location, restore backup
    try:
        shutil.move(str(graph_path), str(candidate_path))
    except Exception:
        # if we cannot put the candidate back, drop it; the .bak restore is
        # what matters for a working graph.json
        try:
            graph_path.unlink(missing_ok=True)  # type: ignore[call-arg]
        except TypeError:
            # py<3.8 fallback (defensive — repo runs 3.10+)
            if graph_path.exists():
                graph_path.unlink()
    shutil.move(str(bak_path), str(graph_path))

    info = {"ok": False, "reason": validation.get("reason", "smoke_validate failed")}
    if observer_alert is not None:
        observer_alert(info)
    return {"ok": False, "reason": info["reason"], "rolled_back": True}


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------

def rollback(
    graph_path: pathlib.Path,
    *,
    max_age_days: int = BAK_RETENTION_DAYS,
) -> Dict[str, Any]:
    """Restore ``graph_path`` from its ``.json.bak`` sibling.

    Refuses to restore a backup older than ``max_age_days`` (default
    :data:`BAK_RETENTION_DAYS`) — this prevents a stale backup from
    overwriting weeks of subsequent work.

    Returns a dict with ``ok`` (bool), ``reason`` (str), and
    ``age_days`` (float, the age of the backup at the time of inspection).
    """
    graph_path = pathlib.Path(graph_path)
    bak_path = _bak_path(graph_path)
    if not bak_path.exists():
        return {"ok": False, "reason": f"no backup at {bak_path}", "age_days": None}

    age_seconds = time.time() - bak_path.stat().st_mtime
    age_days = age_seconds / 86400.0
    if age_days > max_age_days:
        return {
            "ok": False,
            "reason": (
                f"backup too old: age_days={age_days:.2f} > max_age_days={max_age_days}"
            ),
            "age_days": age_days,
        }

    if graph_path.exists():
        # Preserve the broken graph alongside for forensics
        broken_path = graph_path.with_suffix(".json.broken")
        try:
            if broken_path.exists():
                broken_path.unlink()
        except OSError:
            pass
        try:
            shutil.move(str(graph_path), str(broken_path))
        except OSError:
            graph_path.unlink()

    shutil.move(str(bak_path), str(graph_path))
    return {"ok": True, "reason": "rollback ok", "age_days": age_days}


# ---------------------------------------------------------------------------
# status helper (used by the CLI)
# ---------------------------------------------------------------------------

def status(graph_path: pathlib.Path) -> Dict[str, Any]:
    """Return current swap status: whether a ``.bak`` exists and its age."""
    graph_path = pathlib.Path(graph_path)
    bak_path = _bak_path(graph_path)
    if not bak_path.exists():
        return {"bak_exists": False, "age_days": None, "graph_path": str(graph_path)}
    age_seconds = time.time() - bak_path.stat().st_mtime
    age_days = age_seconds / 86400.0
    return {
        "bak_exists": True,
        "age_days": age_days,
        "graph_path": str(graph_path),
        "bak_path": str(bak_path),
        "expired": age_days > BAK_RETENTION_DAYS,
    }


__all__ = [
    "BAK_RETENTION_DAYS",
    "atomic_swap",
    "smoke_validate",
    "rollback",
    "status",
]
