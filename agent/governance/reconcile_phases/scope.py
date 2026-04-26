"""Scoped reconcile — ReconcileScope, ResolvedScope, FileOrigin + 5 resolvers.

Enables targeted reconcile runs limited to files/nodes affected by a specific
bug_id, commit, commit_range, node set, or explicit path list.  Multiple
dimensions compose via UNION semantics (not intersection).
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, FrozenSet, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from .context import ReconcileContext

log = logging.getLogger(__name__)


class EmptyScopeError(Exception):
    """Raised when strict=True and scope resolution yields no files."""


@dataclass
class FileOrigin:
    """Tracks why a file was included in the scope."""
    source: str          # 'bug_id' | 'commit' | 'commit_range' | 'node' | 'path' | 'test_sibling' | 'doc_ref'
    detail: str = ""     # e.g. the bug_id string or commit SHA


@dataclass
class ResolvedScope:
    """Result of ReconcileScope.resolve() — immutable after creation."""
    file_set: Dict[str, FileOrigin] = field(default_factory=dict)
    node_set: FrozenSet[str] = field(default_factory=frozenset)
    commit_set: FrozenSet[str] = field(default_factory=frozenset)

    def files(self) -> Set[str]:
        """Return the set of file paths in scope."""
        return set(self.file_set.keys())

    def is_empty(self) -> bool:
        return len(self.file_set) == 0 and len(self.node_set) == 0


@dataclass
class ReconcileScope:
    """Declarative scope specification — call .resolve(ctx) to materialise."""
    bug_id: Optional[str] = None
    commit: Optional[str] = None
    commit_range: Optional[str] = None
    nodes: Optional[List[str]] = None
    paths: Optional[List[str]] = None
    strict: bool = False
    include_tests: bool = True
    include_docs: bool = True

    def resolve(self, ctx: "ReconcileContext") -> ResolvedScope:
        """Run each non-None resolver, union results, return ResolvedScope."""
        file_set: Dict[str, FileOrigin] = {}
        node_ids: Set[str] = set()
        commit_ids: Set[str] = set()

        if self.bug_id:
            _resolve_bug_id(self.bug_id, ctx, file_set)

        if self.commit:
            commits = {self.commit}
            _resolve_commits(commits, ctx, file_set)
            commit_ids |= commits

        if self.commit_range:
            commits = _expand_commit_range(self.commit_range, ctx)
            _resolve_commits(commits, ctx, file_set)
            commit_ids |= commits

        if self.nodes:
            _resolve_nodes(self.nodes, ctx, file_set, node_ids)

        if self.paths:
            for p in self.paths:
                if p not in file_set:
                    file_set[p] = FileOrigin(source="path", detail=p)

        # Auto-expand: test siblings
        if self.include_tests and file_set:
            _expand_test_siblings(dict(file_set), ctx, file_set)

        # Auto-expand: doc references
        if self.include_docs and file_set:
            _expand_doc_refs(dict(file_set), ctx, file_set)

        resolved = ResolvedScope(
            file_set=file_set,
            node_set=frozenset(node_ids),
            commit_set=frozenset(commit_ids),
        )

        if resolved.is_empty():
            if self.strict:
                raise EmptyScopeError(
                    f"Scope resolved to empty (bug_id={self.bug_id}, "
                    f"commit={self.commit}, commit_range={self.commit_range}, "
                    f"nodes={self.nodes}, paths={self.paths})"
                )
            log.warning("ReconcileScope resolved to empty (non-strict mode)")

        return resolved


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

def _resolve_bug_id(
    bug_id: str,
    ctx: "ReconcileContext",
    file_set: Dict[str, FileOrigin],
) -> None:
    """Resolve bug_id → target_files from backlog_bugs table."""
    try:
        from ..dbservice import DBContext
        with DBContext(ctx.project_id) as conn:
            row = conn.execute(
                "SELECT target_files FROM backlog_bugs WHERE bug_id = ?",
                (bug_id,),
            ).fetchone()
        if row and row[0]:
            import json
            try:
                targets = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            except (json.JSONDecodeError, TypeError):
                targets = []
            for f in targets:
                if f not in file_set:
                    file_set[f] = FileOrigin(source="bug_id", detail=bug_id)
    except Exception as exc:
        log.warning("bug_id resolver failed for %s: %s", bug_id, exc)


def _resolve_commits(
    commits: Set[str],
    ctx: "ReconcileContext",
    file_set: Dict[str, FileOrigin],
) -> None:
    """Resolve commit SHAs → changed files via git diff-tree."""
    for sha in commits:
        try:
            result = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
                cwd=ctx.workspace_path,
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    path = line.strip()
                    if path and path not in file_set:
                        file_set[path] = FileOrigin(source="commit", detail=sha)
        except Exception as exc:
            log.warning("commit resolver failed for %s: %s", sha, exc)


def _expand_commit_range(
    commit_range: str,
    ctx: "ReconcileContext",
) -> Set[str]:
    """Expand A..B into individual commit SHAs."""
    commits: Set[str] = set()
    try:
        result = subprocess.run(
            ["git", "rev-list", commit_range],
            cwd=ctx.workspace_path,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                sha = line.strip()
                if sha:
                    commits.add(sha)
    except Exception as exc:
        log.warning("commit_range resolver failed for %s: %s", commit_range, exc)
    return commits


def _resolve_nodes(
    node_ids: List[str],
    ctx: "ReconcileContext",
    file_set: Dict[str, FileOrigin],
    out_node_set: Set[str],
) -> None:
    """Resolve node IDs → their primary/secondary/test files from graph."""
    out_node_set.update(node_ids)
    graph = ctx.graph
    if graph is None:
        return
    for nid in node_ids:
        try:
            node = graph.get_node(nid)
            if node is None:
                continue
            for f in _node_files(node):
                if f not in file_set:
                    file_set[f] = FileOrigin(source="node", detail=nid)
        except Exception as exc:
            log.warning("node resolver failed for %s: %s", nid, exc)


def _node_files(node: Any) -> List[str]:
    """Extract all file paths from a graph node."""
    files = []
    if hasattr(node, "primary") and node.primary:
        if isinstance(node.primary, list):
            files.extend(node.primary)
        else:
            files.append(node.primary)
    if hasattr(node, "secondary") and node.secondary:
        files.extend(node.secondary)
    if hasattr(node, "test") and node.test:
        files.extend(node.test)
    return files


def _expand_test_siblings(
    current: Dict[str, FileOrigin],
    ctx: "ReconcileContext",
    file_set: Dict[str, FileOrigin],
) -> None:
    """For each .py source file, add test_<stem>.py sibling if it exists."""
    workspace = Path(ctx.workspace_path)
    for path in list(current.keys()):
        p = PurePosixPath(path)
        if p.suffix != ".py" or p.name.startswith("test_"):
            continue
        # Look in same dir and agent/tests/
        test_name = f"test_{p.stem}.py"
        candidates = [
            str(p.parent / test_name),
            f"agent/tests/{test_name}",
        ]
        for cand in candidates:
            if cand not in file_set and (workspace / cand).exists():
                file_set[cand] = FileOrigin(source="test_sibling", detail=path)


def _expand_doc_refs(
    current: Dict[str, FileOrigin],
    ctx: "ReconcileContext",
    file_set: Dict[str, FileOrigin],
) -> None:
    """Grep docs/**/*.md for references to basenames of source files."""
    workspace = Path(ctx.workspace_path)
    docs_dir = workspace / "docs"
    if not docs_dir.is_dir():
        return

    # Collect basenames to search for
    basenames = set()
    for path in current.keys():
        name = PurePosixPath(path).name
        if name:
            basenames.add(name)

    if not basenames:
        return

    # Walk docs and check for references
    for md_path in docs_dir.rglob("*.md"):
        try:
            content = md_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = str(md_path.relative_to(workspace)).replace("\\", "/")
        if rel in file_set:
            continue
        for bn in basenames:
            if bn in content:
                file_set[rel] = FileOrigin(source="doc_ref", detail=bn)
                break
