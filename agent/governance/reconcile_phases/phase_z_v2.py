"""Phase Z v2 PR1 — Symbol-level topology infrastructure.

AST-based parsing, import-aware call graph construction, Tarjan SCC,
and hybrid cycle handling.  No CLI, no graph.json writes, no migration
state machine — those are PR 2/3 scope.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
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
    prod_dirs: Tuple[str, ...] = DEFAULT_PROD_DIRS,
) -> Dict[str, ModuleInfo]:
    """Walk prod_dirs under project_root, parse each .py file via AST.

    Returns dict keyed by dotted module name -> ModuleInfo.
    Skips directories in EXCLUDE_DIRS.
    """
    modules: Dict[str, ModuleInfo] = {}

    for prod_dir in prod_dirs:
        base = os.path.join(project_root, prod_dir)
        if not os.path.isdir(base):
            continue

        for dirpath, dirnames, filenames in os.walk(base):
            # Filter excluded dirs IN-PLACE so os.walk skips them
            dirnames[:] = [
                d for d in dirnames
                if d not in EXCLUDE_DIRS
            ]

            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
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
