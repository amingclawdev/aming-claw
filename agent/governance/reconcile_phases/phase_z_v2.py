"""Phase Z v2 — Symbol-level topology infrastructure + driver.

PR1: AST-based parsing, import-aware call graph construction, Tarjan SCC,
and hybrid cycle handling.
PR2: Scoring + aggregation.
PR3: Driver function, coverage lookup, diff, artifact write, CLI.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# R6: Directories to exclude from production module scanning
# ---------------------------------------------------------------------------
EXCLUDE_DIRS: frozenset[str] = frozenset({
    "__pycache__", ".git", "node_modules", ".venv", "venv",
    ".claude", ".worktrees", "shared-volume", "runtime",
    ".mypy_cache", ".pytest_cache", "build", "dist",
})

# Default production directories to scan
DEFAULT_PROD_DIRS: Tuple[str, ...] = ("agent", "scripts")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class FunctionMeta:
    """Metadata for a single function/method extracted via AST."""
    module: str
    name: str
    qualified_name: str  # module::func_name
    lineno: int
    end_lineno: int
    decorators: List[str] = field(default_factory=list)
    calls: List[str] = field(default_factory=list)
    is_entry: bool = False


@dataclass
class ModuleInfo:
    """Parsed information for a single Python module."""
    path: str
    module_name: str  # dotted module name
    import_map: Dict[str, str] = field(default_factory=dict)
    # import_map: local_name -> fully_qualified_name
    # e.g. {"get_config": "agent.config.get_config", "os": "os"}
    functions: List[FunctionMeta] = field(default_factory=list)


@dataclass
class CallEdge:
    """A resolved call edge in the call graph."""
    caller: str  # qualified_name of caller
    target: str  # qualified_name of target
    confidence: str = "strong"  # "strong" | "weak"


@dataclass
class WeakEdge:
    """An ambiguous call that could not be uniquely resolved."""
    caller: str
    target: str  # the raw call target as written in source
    candidates: List[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class CallGraph:
    """The full call graph with resolved and ambiguous edges."""
    edges: Dict[str, List[str]] = field(default_factory=dict)
    # caller -> [target qualified names]
    weak_edges: List[WeakEdge] = field(default_factory=list)
    all_functions: Dict[str, FunctionMeta] = field(default_factory=dict)


@dataclass
class CycleDecision:
    """Result of handle_cycle() for a single SCC."""
    scc: List[str]
    action: str  # "auto_break" | "block_for_observer"
    reason: str = ""
    weak_edge: Optional[str] = None  # edge to break if auto_break


# ---------------------------------------------------------------------------
# R1: AST-based parse_production_modules
# ---------------------------------------------------------------------------

class _ImportExtractor(ast.NodeVisitor):
    """Extract import map from a module AST."""

    def __init__(self) -> None:
        self.import_map: Dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name
            self.import_map[local] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            local = alias.asname or alias.name
            fqn = f"{module}.{alias.name}" if module else alias.name
            self.import_map[local] = fqn
        self.generic_visit(node)


class _FunctionExtractor(ast.NodeVisitor):
    """Extract function metadata from a module AST."""

    def __init__(self, module_name: str, import_map: Dict[str, str]) -> None:
        self.module_name = module_name
        self.import_map = import_map
        self.functions: List[FunctionMeta] = []
        self._current_class: Optional[str] = None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        old = self._current_class
        self._current_class = node.name
        self.generic_visit(node)
        self._current_class = old

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._extract_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._extract_function(node)

    def _extract_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        name = node.name
        if self._current_class:
            name = f"{self._current_class}.{name}"

        qualified = f"{self.module_name}::{name}"

        decorators = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name):
                decorators.append(dec.id)
            elif isinstance(dec, ast.Attribute):
                decorators.append(dec.attr)
            elif isinstance(dec, ast.Call):
                if isinstance(dec.func, ast.Name):
                    decorators.append(dec.func.id)
                elif isinstance(dec.func, ast.Attribute):
                    decorators.append(dec.func.attr)

        calls = _extract_calls(node)

        is_entry = any(d in ("app", "route", "cli", "command", "main")
                       for d in decorators) or name in ("main", "__main__")

        end_lineno = getattr(node, "end_lineno", node.lineno)

        fm = FunctionMeta(
            module=self.module_name,
            name=name,
            qualified_name=qualified,
            lineno=node.lineno,
            end_lineno=end_lineno,
            decorators=decorators,
            calls=calls,
            is_entry=is_entry,
        )
        self.functions.append(fm)
        # Don't visit nested functions as separate top-level
        self.generic_visit(node)


def _extract_calls(node: ast.AST) -> List[str]:
    """Extract all function call targets from a function body."""
    calls: List[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            target = _call_target_name(child)
            if target:
                calls.append(target)
    return calls


def _call_target_name(node: ast.Call) -> Optional[str]:
    """Get the string representation of a call target."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = []
        current = func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        parts.reverse()
        return ".".join(parts)
    return None


def _path_to_module(path: str, root: str) -> str:
    """Convert a file path to a dotted module name relative to root's parent."""
    rel = os.path.relpath(path, os.path.dirname(root) if not os.path.isdir(root) else os.path.dirname(root))
    # Actually, root is the project root containing agent/ and scripts/
    rel = os.path.relpath(path, root)
    rel = rel.replace(os.sep, "/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    if rel.endswith("/__init__"):
        rel = rel[:-9]
    return rel.replace("/", ".")


def parse_production_modules(
    project_root: str,
    prod_dirs: Optional[Tuple[str, ...]] = None,
    profile: Optional[Any] = None,
) -> Dict[str, ModuleInfo]:
    """Walk prod_dirs under project_root, parse each .py file via AST.

    Returns dict keyed by dotted module name -> ModuleInfo.
    Skips excluded/test/doc directories so DFS operates on production code.
    """
    if profile is None:
        from agent.governance.project_profile import discover_project_profile
        profile = discover_project_profile(project_root)
    if prod_dirs is None:
        prod_dirs = tuple(getattr(profile, "source_roots", None) or DEFAULT_PROD_DIRS)

    modules: Dict[str, ModuleInfo] = {}

    for prod_dir in prod_dirs:
        base = project_root if prod_dir in ("", ".") else os.path.join(project_root, prod_dir)
        if not os.path.isdir(base):
            continue

        for dirpath, dirnames, filenames in os.walk(base):
            # Filter excluded dirs IN-PLACE so os.walk skips them
            kept_dirs = []
            for dirname in dirnames:
                rel_dir = os.path.relpath(os.path.join(dirpath, dirname), project_root)
                if dirname in EXCLUDE_DIRS:
                    continue
                if profile.is_excluded_path(rel_dir) or profile.is_test_path(rel_dir) or profile.is_doc_path(rel_dir):
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
                rel_file = os.path.relpath(fpath, project_root)
                if not profile.is_production_source_path(rel_file):
                    continue
                try:
                    source = _read_file(fpath)
                    tree = ast.parse(source, filename=fpath)
                except (SyntaxError, UnicodeDecodeError, OSError):
                    continue

                mod_name = _path_to_module(fpath, project_root)

                # Extract imports
                imp_ext = _ImportExtractor()
                imp_ext.visit(tree)

                # Extract functions
                func_ext = _FunctionExtractor(mod_name, imp_ext.import_map)
                func_ext.visit(tree)

                modules[mod_name] = ModuleInfo(
                    path=fpath,
                    module_name=mod_name,
                    import_map=imp_ext.import_map,
                    functions=func_ext.functions,
                )

    return modules


def _read_file(path: str) -> str:
    """Read file with fallback encoding."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(path, "r", encoding="latin-1") as f:
            return f.read()


# ---------------------------------------------------------------------------
# R2: Import-aware call graph construction
# ---------------------------------------------------------------------------

def build_call_graph(modules: Dict[str, ModuleInfo]) -> CallGraph:
    """Build a call graph with import-aware resolution.

    For each call in each function:
    1. Check if the call target is a local function in the same module
    2. Check if the call target resolves via the module's import map
    3. If ambiguous (multiple candidates), record as weak_edge
    4. Never use naive last-segment matching
    """
    graph = CallGraph()

    # Build lookup: qualified_name -> FunctionMeta
    all_funcs: Dict[str, FunctionMeta] = {}
    # Also build: short_name -> [qualified_names] for resolution
    name_to_qualified: Dict[str, List[str]] = {}
    # module_name -> {func_short_name -> qualified_name}
    module_local_funcs: Dict[str, Dict[str, str]] = {}

    for mod_name, mod_info in modules.items():
        module_local_funcs[mod_name] = {}
        for func in mod_info.functions:
            all_funcs[func.qualified_name] = func
            module_local_funcs[mod_name][func.name] = func.qualified_name

            short = func.name
            if short not in name_to_qualified:
                name_to_qualified[short] = []
            name_to_qualified[short].append(func.qualified_name)

    graph.all_functions = all_funcs

    for mod_name, mod_info in modules.items():
        local_funcs = module_local_funcs.get(mod_name, {})

        for func in mod_info.functions:
            caller = func.qualified_name
            if caller not in graph.edges:
                graph.edges[caller] = []

            for call_target in func.calls:
                resolved = _resolve_call(
                    call_target=call_target,
                    caller_module=mod_name,
                    import_map=mod_info.import_map,
                    local_funcs=local_funcs,
                    all_funcs=all_funcs,
                    name_to_qualified=name_to_qualified,
                    module_local_funcs=module_local_funcs,
                )

                if resolved is None:
                    # External / builtin — skip
                    continue
                elif isinstance(resolved, str):
                    # Uniquely resolved
                    graph.edges[caller].append(resolved)
                elif isinstance(resolved, list):
                    # Ambiguous — weak edge
                    graph.weak_edges.append(WeakEdge(
                        caller=caller,
                        target=call_target,
                        candidates=resolved,
                        reason=f"ambiguous: {len(resolved)} candidates for '{call_target}'",
                    ))

    return graph


def _resolve_call(
    call_target: str,
    caller_module: str,
    import_map: Dict[str, str],
    local_funcs: Dict[str, str],
    all_funcs: Dict[str, FunctionMeta],
    name_to_qualified: Dict[str, List[str]],
    module_local_funcs: Dict[str, Dict[str, str]],
) -> Optional[str | List[str]]:
    """Resolve a call target to a qualified function name.

    Returns:
        None — external/builtin, not in our codebase
        str — uniquely resolved qualified name
        list — ambiguous, multiple candidates (weak edge)
    """
    # 1. Check local scope first (same module)
    if call_target in local_funcs:
        return local_funcs[call_target]

    # 2. Check import map
    # Handle dotted calls like "config.get_value" -> check if "config" is imported
    parts = call_target.split(".")
    first_part = parts[0]

    if first_part in import_map:
        fqn_base = import_map[first_part]
        if len(parts) > 1:
            # e.g. call is "config.get_value", import_map["config"] = "agent.config"
            # resolved target = "agent.config.get_value"
            fqn_target = fqn_base + "." + ".".join(parts[1:])
        else:
            # Direct imported name, e.g. "get_config" -> "agent.config.get_config"
            fqn_target = fqn_base

        # Try to find this in all_funcs
        # The qualified name format is "module::func_name"
        # So we need to find module::func from the fqn
        resolved = _find_function_by_fqn(fqn_target, all_funcs, module_local_funcs)
        if resolved is not None:
            return resolved

    # 3. If simple name and NOT in imports, check if it exists in name_to_qualified
    # But we do NOT do naive last-segment matching — only if there's exactly one match
    # AND the name is not in imports (i.e., it's truly unknown)
    if "." not in call_target and call_target not in import_map:
        candidates = name_to_qualified.get(call_target, [])
        # Filter out self (don't count calls within the same function)
        candidates = [c for c in candidates if not c.startswith(f"{caller_module}::")]
        if len(candidates) == 1:
            return candidates[0]
        elif len(candidates) > 1:
            return candidates  # Ambiguous

    # Not resolved — external or builtin
    return None


def _find_function_by_fqn(
    fqn: str,
    all_funcs: Dict[str, FunctionMeta],
    module_local_funcs: Dict[str, Dict[str, str]],
) -> Optional[str]:
    """Find a function by its fully qualified name.

    The fqn might be like "agent.config.get_config" — we need to find
    a module "agent.config" with function "get_config".
    """
    # Try splitting at each dot from right to left
    parts = fqn.split(".")
    for i in range(len(parts) - 1, 0, -1):
        mod = ".".join(parts[:i])
        func_name = ".".join(parts[i:])
        if mod in module_local_funcs:
            local = module_local_funcs[mod]
            if func_name in local:
                return local[func_name]
    return None


# ---------------------------------------------------------------------------
# R3: Tarjan SCC algorithm
# ---------------------------------------------------------------------------

def tarjan_scc(graph: Dict[str, List[str]]) -> List[List[str]]:
    """Standard Tarjan's SCC algorithm.

    Returns ALL SCCs including singletons.
    Each SCC is a list of node names.
    """
    index_counter = [0]
    stack: List[str] = []
    on_stack: Set[str] = set()
    index: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    result: List[List[str]] = []

    # Ensure all targets are in the graph as keys (even if they have no outgoing edges)
    all_nodes: Set[str] = set(graph.keys())
    for targets in graph.values():
        for t in targets:
            all_nodes.add(t)

    def strongconnect(v: str) -> None:
        index[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in graph.get(v, []):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])

        if lowlink[v] == index[v]:
            scc: List[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            result.append(scc)

    for v in sorted(all_nodes):  # sorted for determinism
        if v not in index:
            strongconnect(v)

    return result


# ---------------------------------------------------------------------------
# R4 + R5: Cycle handling
# ---------------------------------------------------------------------------

def handle_cycle(
    scc: List[str],
    all_functions: Dict[str, FunctionMeta],
    graph_edges: Dict[str, List[str]],
) -> CycleDecision:
    """Decide how to handle a cycle (SCC with size >= 2).

    auto_break for false-positive shapes:
    - All members are __init__, test_, or decorator-only functions
    - Same-module size-2 cycles

    block_for_observer for:
    - Cross-module cycles
    - Size >= 3 cycles (unless all false-positive)
    """
    if len(scc) < 2:
        return CycleDecision(
            scc=scc,
            action="auto_break",
            reason="singleton — no cycle",
        )

    # Check if all members are likely false positives
    all_fp = all(
        _is_likely_false_positive_cycle_member(node, all_functions)
        for node in scc
    )

    # Check if same module
    modules_in_scc = set()
    for node in scc:
        mod = node.split("::")[0] if "::" in node else ""
        modules_in_scc.add(mod)
    same_module = len(modules_in_scc) == 1

    cross_module = not same_module

    # Decision logic
    if all_fp:
        weak = _pick_weakest_edge_in_cycle(scc, all_functions, graph_edges)
        return CycleDecision(
            scc=scc,
            action="auto_break",
            reason="all members are likely false positives (init/test/decorator)",
            weak_edge=weak,
        )

    if same_module and len(scc) == 2:
        weak = _pick_weakest_edge_in_cycle(scc, all_functions, graph_edges)
        return CycleDecision(
            scc=scc,
            action="auto_break",
            reason="same-module size-2 cycle",
            weak_edge=weak,
        )

    if cross_module:
        return CycleDecision(
            scc=scc,
            action="block_for_observer",
            reason="cross-module cycle requires observer review",
        )

    if len(scc) >= 3:
        return CycleDecision(
            scc=scc,
            action="block_for_observer",
            reason=f"size-{len(scc)} cycle requires observer review",
        )

    # Fallback: block
    return CycleDecision(
        scc=scc,
        action="block_for_observer",
        reason="unhandled cycle shape",
    )


def _is_likely_false_positive_cycle_member(
    node: str,
    all_functions: Dict[str, FunctionMeta],
) -> bool:
    """Check if a node is likely a false-positive cycle member.

    False-positive indicators:
    - __init__ methods
    - test_ prefixed functions
    - Functions with only decorator-related calls
    """
    func = all_functions.get(node)
    if func is None:
        return False

    short_name = func.name
    # __init__ methods
    if short_name.endswith("__init__") or short_name == "__init__":
        return True
    # test functions
    if short_name.startswith("test_") or ".test_" in short_name:
        return True
    # Decorator-only functions (e.g., property, staticmethod)
    decorator_only_names = {"property", "staticmethod", "classmethod", "abstractmethod"}
    if func.decorators and all(d in decorator_only_names for d in func.decorators):
        return True

    return False


def _pick_weakest_edge_in_cycle(
    scc: List[str],
    all_functions: Dict[str, FunctionMeta],
    graph_edges: Dict[str, List[str]],
) -> Optional[str]:
    """Pick the weakest edge in a cycle to break.

    Confidence ranking (weakest first):
    1. function-internal import (call inside function body to imported name)
    2. top-level import (call to top-level imported name)
    3. direct module reference (call via module.func pattern)

    Returns "caller -> target" string for the weakest edge, or None.
    """
    scc_set = set(scc)
    cycle_edges: List[Tuple[str, str, int]] = []  # (caller, target, strength)

    for node in scc:
        for target in graph_edges.get(node, []):
            if target in scc_set:
                strength = _edge_strength(node, target, all_functions)
                cycle_edges.append((node, target, strength))

    if not cycle_edges:
        return None

    # Pick weakest (lowest strength)
    cycle_edges.sort(key=lambda x: x[2])
    weakest = cycle_edges[0]
    return f"{weakest[0]} -> {weakest[1]}"


def _edge_strength(
    caller: str,
    target: str,
    all_functions: Dict[str, FunctionMeta],
) -> int:
    """Rate the strength of a call edge.

    Lower = weaker = better candidate for breaking.
    1 = function-internal import (weakest)
    2 = top-level import
    3 = direct module reference (strongest)
    """
    caller_func = all_functions.get(caller)
    target_func = all_functions.get(target)

    if caller_func is None or target_func is None:
        return 2  # default mid-strength

    caller_mod = caller_func.module
    target_mod = target_func.module

    # Same module = direct reference (strongest)
    if caller_mod == target_mod:
        return 3

    # Check if the target's short name appears in caller's calls
    # as a dotted reference (module.func pattern) — mid strength
    target_short = target_func.name
    for call in caller_func.calls:
        if "." in call and call.endswith(target_short):
            return 2  # top-level import

    # Otherwise assume function-internal import (weakest)
    return 1


# ---------------------------------------------------------------------------
# PR2: Scoring + Aggregation
# ---------------------------------------------------------------------------

def score_function_layer(
    func_qname: str,
    scc_index: Dict[str, int],
    graph_edges: Dict[str, List[str]],
    all_functions: Dict[str, FunctionMeta],
) -> int:
    """Score a function's layer based on SCC topology order and call depth.

    Lower layer = closer to leaf (no outgoing calls).
    Returns an integer layer score >= 0.
    """
    base = scc_index.get(func_qname, 0)
    outgoing = graph_edges.get(func_qname, [])
    if not outgoing:
        return base
    max_target = max(scc_index.get(t, 0) for t in outgoing)
    return max(base, max_target + 1)


def aggregate_functions_into_nodes(
    modules: Dict[str, ModuleInfo],
    layer_scores: Dict[str, int],
) -> List[Dict[str, Any]]:
    """Aggregate per-function layer scores into per-module node dicts.

    Returns a list of node dicts with keys:
      node_id, primary_file, module, layer, functions, function_count
    """
    nodes: List[Dict[str, Any]] = []
    for mod_name, mod_info in modules.items():
        func_layers = [layer_scores.get(f.qualified_name, 0) for f in mod_info.functions]
        agg_layer = max(func_layers) if func_layers else 0
        nodes.append({
            "node_id": mod_name,
            "primary_file": mod_info.path,
            "module": mod_name,
            "layer": agg_layer,
            "functions": [f.qualified_name for f in mod_info.functions],
            "function_count": len(mod_info.functions),
        })
    return nodes


# ---------------------------------------------------------------------------
# PR3 R2: Coverage lookup
# ---------------------------------------------------------------------------

def find_test_coverage(
    project_root: str,
    primary_file: str,
) -> Dict[str, Any]:
    """Find test files and coverage for a given primary source file.

    Returns dict with test_files (list of paths) and covered_lines (int).
    """
    test_files: List[str] = []
    covered_lines = 0

    # Derive the module basename for matching
    base = os.path.basename(primary_file)
    if base.endswith(".py"):
        base = base[:-3]

    # Search common test dirs
    test_dirs = ["tests", "agent/tests", "test"]
    for td in test_dirs:
        tpath = os.path.join(project_root, td)
        if not os.path.isdir(tpath):
            continue
        for fname in os.listdir(tpath):
            if not fname.endswith(".py"):
                continue
            # Match test_<module>.py or test_<module>_*.py patterns
            if fname == f"test_{base}.py" or fname.startswith(f"test_{base}_"):
                full = os.path.join(tpath, fname)
                test_files.append(full)
                try:
                    content = _read_file(full)
                    covered_lines += content.count("\n")
                except OSError:
                    pass

    return {"test_files": test_files, "covered_lines": covered_lines}


def find_doc_coverage(
    project_root: str,
    primary_file: str,
) -> Dict[str, Any]:
    """Find doc files referencing a given primary source file.

    Returns dict with doc_files (list of paths) and covered_lines (int).
    """
    doc_files: List[str] = []
    covered_lines = 0

    rel = os.path.relpath(primary_file, project_root) if os.path.isabs(primary_file) else primary_file
    rel_normalized = rel.replace(os.sep, "/")

    docs_dir = os.path.join(project_root, "docs")
    if not os.path.isdir(docs_dir):
        return {"doc_files": doc_files, "covered_lines": covered_lines}

    for dirpath, _dirnames, filenames in os.walk(docs_dir):
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                content = _read_file(fpath)
                if rel_normalized in content or os.path.basename(primary_file) in content:
                    doc_files.append(fpath)
                    covered_lines += content.count("\n")
            except OSError:
                pass

    return {"doc_files": doc_files, "covered_lines": covered_lines}


# ---------------------------------------------------------------------------
# PR3 R3: Dry-run artifact
# ---------------------------------------------------------------------------

def write_dry_run_artifact(
    project_root: str,
    nodes: List[Dict[str, Any]],
    diff_report: Dict[str, Any],
    scratch_dir: Optional[str] = None,
    feature_clusters: Optional[List[Dict[str, Any]]] = None,
    file_inventory: Optional[List[Dict[str, Any]]] = None,
    file_inventory_summary: Optional[Dict[str, Any]] = None,
) -> str:
    """Write docs/dev/scratch/graph-v2-{date}.json with diff-vs-current report.

    Returns the path to the written file.
    """
    if scratch_dir is None:
        scratch_dir = os.path.join(project_root, "docs", "dev", "scratch")
    os.makedirs(scratch_dir, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = os.path.join(scratch_dir, f"graph-v2-{date_str}.json")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "node_count": len(nodes),
        "nodes": nodes,
        "diff_report": diff_report,
        "feature_clusters": feature_clusters or [],
        "file_inventory": file_inventory or [],
        "file_inventory_summary": file_inventory_summary or {},
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return out_path


# ---------------------------------------------------------------------------
# Reconcile feature-cluster synthesis
# ---------------------------------------------------------------------------

def _repo_relpath(project_root: str, path: str) -> str:
    raw = str(path or "")
    try:
        if os.path.isabs(raw):
            raw = os.path.relpath(raw, project_root)
    except ValueError:
        pass
    return raw.replace("\\", "/")


def _module_from_qname(qname: str) -> str:
    return qname.split("::", 1)[0] if "::" in qname else ""


def _package_key(path: str) -> str:
    """Return a generic file-tree bucket key for bounded batch coalescing."""
    normal = str(path or "").replace("\\", "/").strip("/")
    if not normal:
        return ""
    parent = os.path.dirname(normal)
    return parent or normal


def _cluster_fingerprint(entries: List[str], primary_files: List[str]) -> str:
    payload = "|".join(sorted(entries)) + "||" + "|".join(sorted(primary_files))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _cluster_file_cap() -> int:
    try:
        from agent.governance.reconcile_config import RECONCILE_FEATURE_CLUSTER_FILE_CAP
        return max(1, int(RECONCILE_FEATURE_CLUSTER_FILE_CAP))
    except Exception:
        return 6


def synthesize_feature_clusters(
    *,
    project_root: str,
    modules: Dict[str, ModuleInfo],
    call_graph: CallGraph,
    sccs: List[List[str]],
    nodes: Optional[List[Dict[str, Any]]] = None,
    file_cap: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Build reconcile FeatureCluster candidates from SCC roots.

    The synthesis path is intentionally source-only: tests/docs are attached
    as secondary coverage after DFS, but they never become traversal roots.
    """
    if not modules or not call_graph.all_functions:
        return []

    cap = max(1, int(file_cap or _cluster_file_cap()))
    component_nodes: Dict[int, Set[str]] = {
        idx: {fn for fn in scc if fn in call_graph.all_functions}
        for idx, scc in enumerate(sccs)
    }
    component_nodes = {idx: members for idx, members in component_nodes.items() if members}
    if not component_nodes:
        return []

    component_by_function: Dict[str, int] = {
        fn: idx
        for idx, members in component_nodes.items()
        for fn in members
    }
    dag: Dict[int, Set[int]] = {idx: set() for idx in component_nodes}
    indegree: Dict[int, int] = {idx: 0 for idx in component_nodes}
    for caller, targets in call_graph.edges.items():
        caller_component = component_by_function.get(caller)
        if caller_component is None:
            continue
        for target in targets:
            target_component = component_by_function.get(target)
            if target_component is None or target_component == caller_component:
                continue
            if target_component not in dag[caller_component]:
                dag[caller_component].add(target_component)
                indegree[target_component] += 1

    root_components = sorted(idx for idx, count in indegree.items() if count == 0)
    if not root_components:
        root_components = sorted(component_nodes)

    module_functions: Dict[str, Set[str]] = {}
    for module_name, module_info in modules.items():
        module_functions[module_name] = {
            func.qualified_name for func in module_info.functions
        }

    node_by_module = {
        str(node.get("module") or node.get("node_id") or ""): node
        for node in (nodes or [])
    }

    reach_cache: Dict[int, Set[int]] = {}

    def reachable_components(start: int) -> Set[int]:
        if start in reach_cache:
            return set(reach_cache[start])
        seen: Set[int] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(sorted(dag.get(current, set()) - seen, reverse=True))
        reach_cache[start] = set(seen)
        return seen

    def files_for_modules(module_names: Set[str]) -> Tuple[List[str], List[str]]:
        primary_files: Set[str] = set()
        secondary_files: Set[str] = set()
        for module_name in sorted(module_names):
            node = node_by_module.get(module_name)
            if node:
                pf = _repo_relpath(project_root, str(node.get("primary_file") or ""))
                if pf:
                    primary_files.add(pf)
                for test_file in (node.get("test_coverage") or {}).get("test_files", []):
                    rel = _repo_relpath(project_root, str(test_file))
                    if rel:
                        secondary_files.add(rel)
                for doc_file in (node.get("doc_coverage") or {}).get("doc_files", []):
                    rel = _repo_relpath(project_root, str(doc_file))
                    if rel:
                        secondary_files.add(rel)
                continue
            module_info = modules.get(module_name)
            if module_info:
                rel = _repo_relpath(project_root, module_info.path)
                if rel:
                    primary_files.add(rel)
        return sorted(primary_files), sorted(secondary_files)

    branches: List[Dict[str, Any]] = []
    for root_component in root_components:
        root_functions = sorted(component_nodes[root_component])
        if not root_functions:
            continue
        entry_qname = root_functions[0]
        entry_module = _module_from_qname(entry_qname)
        reached_components = reachable_components(root_component)
        reached_functions: Set[str] = set()
        for component in reached_components:
            reached_functions.update(component_nodes.get(component, set()))

        # Keep a root module's local helpers with its root branch. This avoids
        # fragmenting plain, undecorated modules into one cluster per helper.
        reached_functions.update(module_functions.get(entry_module, set()))

        reached_modules = {
            module for module in (_module_from_qname(fn) for fn in reached_functions) if module
        }
        primary_files, secondary_files = files_for_modules(reached_modules)
        if not primary_files:
            continue
        entry_file = _repo_relpath(project_root, modules.get(entry_module, ModuleInfo("", "")).path)
        package_key = _package_key(entry_file or primary_files[0])
        decorators = sorted({
            dec
            for fn in reached_functions
            for dec in (call_graph.all_functions.get(fn).decorators if call_graph.all_functions.get(fn) else [])
        })
        branches.append({
            "entry_qname": entry_qname,
            "root_functions": root_functions,
            "root_component_size": len(root_functions),
            "is_cycle_root": len(root_functions) > 1,
            "package_key": package_key,
            "primary_files": primary_files,
            "secondary_files": secondary_files,
            "functions": sorted(reached_functions),
            "modules": sorted(reached_modules),
            "decorators": decorators,
        })

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for branch in branches:
        buckets.setdefault(branch["package_key"], []).append(branch)

    clusters: List[Dict[str, Any]] = []

    def flush_chunk(package_key: str, chunk: List[Dict[str, Any]]) -> None:
        if not chunk:
            return
        entries = sorted({branch["entry_qname"] for branch in chunk if branch.get("entry_qname")})
        primary_files = sorted({pf for branch in chunk for pf in branch.get("primary_files", [])})
        secondary_files = sorted({sf for branch in chunk for sf in branch.get("secondary_files", [])})
        functions = sorted({fn for branch in chunk for fn in branch.get("functions", [])})
        modules_in_cluster = sorted({mod for branch in chunk for mod in branch.get("modules", [])})
        decorators = sorted({dec for branch in chunk for dec in branch.get("decorators", [])})
        clusters.append({
            "cluster_fingerprint": _cluster_fingerprint(entries, primary_files),
            "entries": entries,
            "primary_files": primary_files,
            "secondary_files": secondary_files,
            "functions": functions,
            "modules": modules_in_cluster,
            "decorators": decorators,
            "synthesis": {
                "strategy": "scc_indegree_root_dfs_filetree_coalesce",
                "package_key": package_key,
                "root_count": len(entries),
                "cycle_root_count": sum(1 for branch in chunk if branch.get("is_cycle_root")),
                "function_count": len(functions),
                "module_count": len(modules_in_cluster),
                "file_cap": cap,
            },
        })

    for package_key, package_branches in sorted(buckets.items()):
        current: List[Dict[str, Any]] = []
        current_files: Set[str] = set()
        for branch in sorted(package_branches, key=lambda b: (
            b.get("primary_files", [""])[0] if b.get("primary_files") else "",
            b.get("entry_qname", ""),
        )):
            branch_files = set(branch.get("primary_files", []))
            next_files = current_files | branch_files
            if current and len(next_files) > cap:
                flush_chunk(package_key, current)
                current = []
                current_files = set()
            current.append(branch)
            current_files.update(branch_files)
        flush_chunk(package_key, current)

    clusters.sort(key=lambda c: c["cluster_fingerprint"])
    return clusters


# ---------------------------------------------------------------------------
# PR3 R4: Diff against existing graph
# ---------------------------------------------------------------------------

def _default_existing_graph_path(project_root: str) -> Optional[str]:
    """Return the best-effort current governance graph path for *project_root*."""
    explicit = os.environ.get("PHASE_Z_EXISTING_GRAPH_PATH")
    if explicit and os.path.isfile(explicit):
        return explicit

    project_id = (
        os.environ.get("AMING_PROJECT_ID")
        or os.environ.get("PROJECT_ID")
        or "aming-claw"
    )
    candidates = [
        os.path.join(project_root, "agent", "governance", "graph.json"),
        os.path.join(
            project_root,
            "shared-volume",
            "codex-tasks",
            "state",
            "governance",
            project_id,
            "graph.json",
        ),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    governance_root = os.path.join(
        project_root, "shared-volume", "codex-tasks", "state", "governance"
    )
    if os.path.isdir(governance_root):
        found = []
        for name in sorted(os.listdir(governance_root)):
            candidate = os.path.join(governance_root, name, "graph.json")
            if os.path.isfile(candidate):
                found.append(candidate)
        if len(found) == 1:
            return found[0]
    return None


def _normalize_graph_path(project_root: str, path: Any) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        if os.path.isabs(raw):
            raw = os.path.relpath(raw, project_root)
    except ValueError:
        pass
    normalized = raw.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _extract_graph_nodes(payload: Any) -> List[Dict[str, Any]]:
    """Extract node dictionaries from supported graph.json shapes."""
    if not isinstance(payload, dict):
        if isinstance(payload, list):
            return [n for n in payload if isinstance(n, dict)]
        return []

    nodes = payload.get("nodes")
    if isinstance(nodes, list):
        return [n for n in nodes if isinstance(n, dict)]
    if isinstance(nodes, dict):
        return [n for n in nodes.values() if isinstance(n, dict)]

    deps_graph = payload.get("deps_graph")
    if isinstance(deps_graph, dict):
        deps_nodes = deps_graph.get("nodes")
        if isinstance(deps_nodes, list):
            return [n for n in deps_nodes if isinstance(n, dict)]
        if isinstance(deps_nodes, dict):
            return [n for n in deps_nodes.values() if isinstance(n, dict)]
    return []


def _node_id(node: Dict[str, Any]) -> str:
    return str(node.get("node_id") or node.get("id") or "")


def _node_layer(node: Dict[str, Any]) -> Any:
    return node.get("layer")


def _node_primary_files(project_root: str, node: Dict[str, Any]) -> List[str]:
    raw = (
        node.get("primary_file")
        or node.get("primary")
        or node.get("primary_files")
        or []
    )
    if isinstance(raw, str):
        raw_values = [raw]
    elif isinstance(raw, list):
        raw_values = raw
    else:
        raw_values = []
    return sorted({
        normalized
        for normalized in (_normalize_graph_path(project_root, p) for p in raw_values)
        if normalized
    })


def _index_primary_files(
    project_root: str,
    nodes: List[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]]]:
    by_primary: Dict[str, Dict[str, Any]] = {}
    owners: Dict[str, List[str]] = {}
    for node in nodes:
        nid = _node_id(node)
        for primary in _node_primary_files(project_root, node):
            owners.setdefault(primary, []).append(nid)
            by_primary.setdefault(primary, node)
    return by_primary, owners


def diff_against_existing_graph(
    project_root: str,
    new_nodes: List[Dict[str, Any]],
    graph_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare new derived nodes vs existing graph.json.

    Returns ID-based drift plus primary-file drift.  The latter is the useful
    calibration signal when rebasing an old Lx graph into symbol-derived module
    nodes whose IDs are intentionally different.
    """
    existing_graph_path = graph_path or _default_existing_graph_path(project_root)

    old_nodes_by_id: Dict[str, Any] = {}
    if existing_graph_path and os.path.isfile(existing_graph_path):
        try:
            with open(existing_graph_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for n in _extract_graph_nodes(data):
                nid = _node_id(n)
                if nid:
                    old_nodes_by_id[nid] = n
        except (json.JSONDecodeError, OSError):
            pass

    new_ids = {_node_id(n) for n in new_nodes if _node_id(n)}
    old_ids = set(old_nodes_by_id.keys())

    only_in_new = sorted(new_ids - old_ids)
    only_in_old = sorted(old_ids - new_ids)

    layer_changes: List[Dict[str, Any]] = []
    new_by_id = {_node_id(n): n for n in new_nodes if _node_id(n)}
    for nid in new_ids & old_ids:
        old_layer = _node_layer(old_nodes_by_id[nid])
        new_layer = _node_layer(new_by_id[nid])
        if old_layer is not None and new_layer is not None and old_layer != new_layer:
            layer_changes.append({
                "node_id": nid,
                "old_layer": old_layer,
                "new_layer": new_layer,
            })

    old_nodes = list(old_nodes_by_id.values())
    old_by_primary, old_primary_owners = _index_primary_files(project_root, old_nodes)
    new_by_primary, new_primary_owners = _index_primary_files(project_root, new_nodes)
    old_primaries = set(old_by_primary)
    new_primaries = set(new_by_primary)

    layer_changes_by_primary: List[Dict[str, Any]] = []
    for primary in sorted(old_primaries & new_primaries):
        old_node = old_by_primary[primary]
        new_node = new_by_primary[primary]
        old_layer = _node_layer(old_node)
        new_layer = _node_layer(new_node)
        if old_layer is not None and new_layer is not None and old_layer != new_layer:
            layer_changes_by_primary.append({
                "primary_file": primary,
                "old_node_id": _node_id(old_node),
                "new_node_id": _node_id(new_node),
                "old_layer": old_layer,
                "new_layer": new_layer,
            })

    duplicate_old = {
        primary: sorted([owner for owner in owners if owner])
        for primary, owners in old_primary_owners.items()
        if len([owner for owner in owners if owner]) > 1
    }
    duplicate_new = {
        primary: sorted([owner for owner in owners if owner])
        for primary, owners in new_primary_owners.items()
        if len([owner for owner in owners if owner]) > 1
    }

    return {
        "graph_path": existing_graph_path or "",
        "old_node_count": len(old_nodes_by_id),
        "new_node_count": len(new_nodes),
        "only_in_new": only_in_new,
        "only_in_old": only_in_old,
        "layer_changes": layer_changes,
        "primary_file_diff": {
            "matched": len(old_primaries & new_primaries),
            "only_in_new": sorted(new_primaries - old_primaries),
            "only_in_old": sorted(old_primaries - new_primaries),
            "layer_changes": layer_changes_by_primary,
            "duplicates_in_old": duplicate_old,
            "duplicates_in_new": duplicate_new,
        },
    }


# ---------------------------------------------------------------------------
# PR3 R5: Write graph.v2.json
# ---------------------------------------------------------------------------

def write_graph_v2_json(
    project_root: str,
    nodes: List[Dict[str, Any]],
) -> str:
    """Write agent/governance/graph.v2.json (NOT graph.json).

    Returns the path to the written file.
    """
    out_path = os.path.join(project_root, "agent", "governance", "graph.v2.json")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "v2",
        "node_count": len(nodes),
        "nodes": nodes,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return out_path


# ---------------------------------------------------------------------------
# PR2a: DFS coloring from entry points
# ---------------------------------------------------------------------------

_ENTRY_DECORATORS_SET = frozenset(
    {"route", "app", "get", "post", "put", "delete", "patch", "cli"}
)
_MCP_HANDLER_PATTERNS = frozenset(
    {"mcp_tool", "server.tool", "server.resource", "server.prompt"}
)


def identify_entries(modules: Dict[str, ModuleInfo]) -> List[str]:
    """Detect entry-point functions from module metadata.

    Entry criteria:
    - Decorated with @route/@app/@get/@post/@put/@delete/@patch/@cli
    - MCP handler patterns (mcp_tool, server.tool, etc.)
    - __main__ guard or scripts/ path with __main__ block
    """
    entries: List[str] = []
    for _mod_name, mod_info in modules.items():
        is_script = "scripts/" in mod_info.path.replace("\\", "/")
        for func in mod_info.functions:
            if _is_entry_func(func, is_script):
                entries.append(func.qualified_name)
    return entries


def _is_entry_func(func: FunctionMeta, is_script: bool) -> bool:
    for dec in func.decorators:
        dec_lower = dec.lower()
        for pat in _ENTRY_DECORATORS_SET:
            if pat in dec_lower:
                return True
        for pat in _MCP_HANDLER_PATTERNS:
            if pat in dec_lower:
                return True
    if "__main__" in func.name or "__main__" in func.qualified_name:
        return True
    if is_script and func.is_entry:
        return True
    return False


def dfs_color_from_entries(
    edges: Dict[str, List[str]],
    entries: List[str],
    track_distance: bool = False,
) -> Tuple[Dict[str, Set[str]], Dict[str, int]]:
    """Perform DFS from each entry through strong call-graph edges.

    Args:
        edges: Strong call-graph edges (caller -> [targets]).
        entries: List of entry-point qualified names.
        track_distance: Reserved for future re-add of min_distance computation
            via in-DFS hashmap. Currently unused.

    Returns:
        (color_sets, color_count_map) where:
        - color_sets[entry_qname] = set of all reachable function qnames
        - color_count_map[fn_qname] = count of distinct entries reaching fn
    """
    color_sets: Dict[str, Set[str]] = {}
    color_count_map: Dict[str, int] = {}

    for entry in entries:
        visited: Set[str] = set()
        stack = [entry]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            for target in edges.get(node, []):
                if target not in visited:
                    stack.append(target)
        color_sets[entry] = visited
        for fn in visited:
            color_count_map[fn] = color_count_map.get(fn, 0) + 1

    return color_sets, color_count_map


# ---------------------------------------------------------------------------
# PR3 R1/R10/R11: Driver function
# ---------------------------------------------------------------------------

CYCLE_ABORT_THRESHOLD = 30


def build_graph_v2_from_symbols(
    project_root: str,
    dry_run: bool = True,
    owner: Optional[str] = None,
    scratch_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Orchestrate the full symbol-level topology pipeline.

    Calls: parse_production_modules, build_call_graph, tarjan_scc,
    score_function_layer, aggregate_functions_into_nodes,
    find_test_coverage, find_doc_coverage.

    If dry_run=True, writes to docs/dev/scratch/ and returns report_path.
    If dry_run=False, writes graph.v2.json and calls create_baseline.
    If >30 cycles detected, returns status='aborted'.
    """
    # Step 1: PR1 — parse + call graph + SCC
    modules = parse_production_modules(project_root)
    call_graph = build_call_graph(modules)
    sccs = tarjan_scc(call_graph.edges)

    # Handle cycles
    real_cycles = [scc for scc in sccs if len(scc) >= 2]

    # R10: Cycle abort threshold
    if len(real_cycles) > CYCLE_ABORT_THRESHOLD:
        return {
            "status": "aborted",
            "abort_reason": f"Too many cycles: {len(real_cycles)} exceeds threshold {CYCLE_ABORT_THRESHOLD}",
        }

    for scc in real_cycles:
        handle_cycle(scc, call_graph.all_functions, call_graph.edges)

    # Step 2a: DFS coloring from entries
    entry_qnames = identify_entries(modules)
    _color_sets, color_count_map = dfs_color_from_entries(
        call_graph.edges, entry_qnames
    )
    max_color_count = max(color_count_map.values()) if color_count_map else 0

    # Step 2: PR2 — scoring + aggregation
    # Build SCC index (topological order)
    scc_index: Dict[str, int] = {}
    for idx, scc in enumerate(sccs):
        for node in scc:
            scc_index[node] = idx

    layer_scores: Dict[str, int] = {}
    for qname in call_graph.all_functions:
        layer_scores[qname] = score_function_layer(
            qname, scc_index, call_graph.edges, call_graph.all_functions
        )

    nodes = aggregate_functions_into_nodes(modules, layer_scores)

    # Step 3: PR3 — coverage lookup
    for node in nodes:
        pf = node.get("primary_file", "")
        test_cov = find_test_coverage(project_root, pf)
        doc_cov = find_doc_coverage(project_root, pf)
        node["test_coverage"] = test_cov
        node["doc_coverage"] = doc_cov

    feature_clusters = synthesize_feature_clusters(
        project_root=project_root,
        modules=modules,
        call_graph=call_graph,
        sccs=sccs,
        nodes=nodes,
    )

    # Step 4: Diff against existing
    diff_report = diff_against_existing_graph(project_root, nodes)

    run_id = datetime.now(timezone.utc).strftime("phase-z-v2-%Y%m%dT%H%M%SZ")
    try:
        from agent.governance.reconcile_file_inventory import (
            build_file_inventory,
            summarize_file_inventory,
        )
        file_inventory = build_file_inventory(
            project_root=project_root,
            run_id=run_id,
            nodes=nodes,
            feature_clusters=feature_clusters,
        )
        file_inventory_summary = summarize_file_inventory(file_inventory)
    except Exception:
        file_inventory = []
        file_inventory_summary = {
            "total": 0,
            "by_kind": {},
            "by_status": {"error": 1},
            "pending_decision_count": 0,
            "pending_decision_sample": [],
        }

    if dry_run:
        # R3: Write dry-run artifact
        report_path = write_dry_run_artifact(
            project_root,
            nodes,
            diff_report,
            scratch_dir=scratch_dir,
            feature_clusters=feature_clusters,
            file_inventory=file_inventory,
            file_inventory_summary=file_inventory_summary,
        )
        return {
            "status": "ok",
            "run_id": run_id,
            "report_path": report_path,
            "node_count": len(nodes),
            "nodes": nodes,
            "feature_clusters": feature_clusters,
            "file_inventory": file_inventory,
            "file_inventory_summary": file_inventory_summary,
            "diff_report": diff_report,
        }
    else:
        # R5: Write graph.v2.json
        graph_path = write_graph_v2_json(project_root, nodes)

        # R11: Call create_baseline with scope_kind='symbol-bootstrap'
        try:
            from agent.governance.baseline_service import create_baseline
            from agent.governance.db import get_connection
            conn = get_connection("aming-claw")
            create_baseline(
                conn=conn,
                project_id="aming-claw",
                chain_version="",
                trigger="phase-z-v2",
                triggered_by=owner or "phase-z-v2",
                scope_kind="symbol-bootstrap",
            )
            conn.close()
        except Exception:
            pass  # Best-effort baseline creation

        return {
            "status": "ok",
            "run_id": run_id,
            "graph_path": graph_path,
            "node_count": len(nodes),
            "nodes": nodes,
            "feature_clusters": feature_clusters,
            "file_inventory": file_inventory,
            "file_inventory_summary": file_inventory_summary,
            "diff_report": diff_report,
        }
