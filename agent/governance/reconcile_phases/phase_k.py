"""Phase K — Contract-Test-Coverage Invariant (DRY-RUN).

AST-extracts 4 contract types from scoped .py files, then emits:
  - contract_no_test: endpoint/service-port with no test coverage
  - doc_value_drift: doc value (port, path) mismatches code value

DRY-RUN only in this PR — suggested_action is set but no spawn.
"""
from __future__ import annotations

import ast
import os
import re
import logging
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from .context import ReconcileContext
    from .scope import ResolvedScope

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Contract dataclasses (§6.0 verbatim)
# ---------------------------------------------------------------------------

@dataclass
class EndpointContract:
    method: str
    path: str
    handler_qname: str
    source_file: str
    source_line: int

    def doc_fingerprints(self) -> List[str]:
        return [
            f"{self.method} `{self.path}`",
            f"`{self.method} {self.path}`",
            f"curl.*{self.method}.*{self.path}",
            f"endpoint.*{self.path}",
        ]


@dataclass
class ServicePortContract:
    service_name: str
    port: int
    constant_name: str
    source_file: str
    source_line: int

    def doc_fingerprints(self) -> List[str]:
        return [
            f"{self.service_name}.*port",
            f"localhost:{self.port}",
            f":{self.port}/api/",
            self.constant_name,
        ]


@dataclass
class URLExampleContract:
    base_url: str
    path: str
    appearing_in: List[str] = field(default_factory=list)


@dataclass
class PublicConstantContract:
    qname: str
    name: str
    value: Any
    value_kind: str
    source_file: str
    source_line: int

    def doc_fingerprints(self) -> List[str]:
        return [self.name, str(self.value)] if self.value_kind == "int" else [self.name]


# ---------------------------------------------------------------------------
# Phase K discrepancy (superset of base Discrepancy fields)
# ---------------------------------------------------------------------------

@dataclass
class PhaseKDiscrepancy:
    """Discrepancy emitted by Phase K with contract-specific fields."""
    type: str
    contract_kind: str = ""
    contract_id: str = ""
    contract_summary: str = ""
    expected_test_location: str = ""
    doc: str = ""
    doc_line: int = 0
    doc_value: Any = None
    code_value: Any = None
    drift_role: str = ""
    confidence: str = "high"
    priority: str = "P0"
    suggested_action: str = ""
    # compat with base Discrepancy
    node_id: Optional[str] = None
    field: Optional[str] = None
    detail: str = ""


# ---------------------------------------------------------------------------
# AST extractors
# ---------------------------------------------------------------------------

def _read_file(path: str, workspace: str = "") -> str:
    """Read file content; returns '' on error."""
    full = os.path.join(workspace, path) if workspace else path
    try:
        with open(full, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def extract_endpoints(source_file: str, workspace: str = "") -> List[EndpointContract]:
    """Find @route(...) decorated functions via AST."""
    src = _read_file(source_file, workspace)
    if not src:
        return []
    try:
        tree = ast.parse(src, filename=source_file)
    except SyntaxError:
        return []
    results: List[EndpointContract] = []
    module_stem = PurePosixPath(source_file).stem
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            method, path = _parse_route_decorator(dec)
            if method and path:
                qname = f"{module_stem}.{node.name}"
                results.append(EndpointContract(
                    method=method, path=path,
                    handler_qname=qname,
                    source_file=source_file,
                    source_line=node.lineno,
                ))
    return results


def _parse_route_decorator(dec: ast.expr):
    """Extract (method, path) from @route('METHOD', '/path') decorator."""
    if isinstance(dec, ast.Call):
        func = dec.func
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name == "route" and len(dec.args) >= 2:
            method = _const_value(dec.args[0])
            path = _const_value(dec.args[1])
            if isinstance(method, str) and isinstance(path, str):
                return method, path
    return None, None


def _const_value(node: ast.expr):
    """Extract constant value from AST node."""
    if isinstance(node, ast.Constant):
        return node.value
    # Python 3.7 compat
    if isinstance(node, ast.Str):
        return node.s
    if isinstance(node, ast.Num):
        return node.n
    return None


def extract_service_ports(source_file: str, workspace: str = "") -> List[ServicePortContract]:
    """Find *PORT or *HOST via module-level Assign OR os.environ.setdefault calls.

    R1: Detects os.environ.setdefault('NAME', 'VALUE') where NAME matches
        ^[A-Z_]*(PORT|HOST)$ and VALUE is a numeric string or int.
    R4: Existing top-level Assign extraction (PORT = 40101) continues unchanged.
    """
    src = _read_file(source_file, workspace)
    if not src:
        return []
    try:
        tree = ast.parse(src, filename=source_file)
    except SyntaxError:
        return []
    service_name = _infer_service_name(source_file)
    results: List[ServicePortContract] = []
    seen_names: Set[str] = set()

    # Path 1: top-level Assign (existing, R4 regression safety)
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id
            if not re.match(r"^[A-Z_]*(PORT|HOST)$", name):
                continue
            val = _const_value(node.value)
            if isinstance(val, int):
                seen_names.add(name)
                results.append(ServicePortContract(
                    service_name=service_name,
                    port=val,
                    constant_name=name,
                    source_file=source_file,
                    source_line=node.lineno,
                ))

    # Path 2: os.environ.setdefault('NAME', 'VALUE') calls (R1)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_environ_setdefault(node):
            continue
        if len(node.args) < 2:
            continue
        env_name = _const_value(node.args[0])
        env_val = _const_value(node.args[1])
        if not isinstance(env_name, str):
            continue
        if not re.match(r"^[A-Z_]*(PORT|HOST)$", env_name):
            continue
        # Already found via Assign — skip duplicate
        if env_name in seen_names:
            continue
        # Value can be int directly or numeric string
        port = None
        if isinstance(env_val, int):
            port = env_val
        elif isinstance(env_val, str) and env_val.isdigit():
            port = int(env_val)
        if port is not None:
            seen_names.add(env_name)
            results.append(ServicePortContract(
                service_name=service_name,
                port=port,
                constant_name=env_name,
                source_file=source_file,
                source_line=node.lineno,
            ))

    return results


def _is_environ_setdefault(node: ast.Call) -> bool:
    """Check if node is os.environ.setdefault(...)."""
    func = node.func
    # os.environ.setdefault(...)
    if isinstance(func, ast.Attribute) and func.attr == "setdefault":
        val = func.value
        if isinstance(val, ast.Attribute) and val.attr == "environ":
            if isinstance(val.value, ast.Name) and val.value.id == "os":
                return True
    return False


def _infer_service_name(source_file: str) -> str:
    """Infer service name from filename."""
    stem = PurePosixPath(source_file).stem
    # server.py → 'governance', start_governance.py → 'governance'
    if stem == "server" or stem == "start_governance":
        return "governance"
    # manager_http_server.py → 'manager_http_server'
    return stem


def extract_public_constants(source_file: str, workspace: str = "") -> List[PublicConstantContract]:
    """Find UPPER_CASE module-level Assign with Constant value."""
    src = _read_file(source_file, workspace)
    if not src:
        return []
    try:
        tree = ast.parse(src, filename=source_file)
    except SyntaxError:
        return []
    module_stem = PurePosixPath(source_file).stem
    results: List[PublicConstantContract] = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id
            if not re.match(r"^[A-Z][A-Z0-9_]*$", name):
                continue
            val = _const_value(node.value)
            if val is None:
                continue
            if isinstance(val, int):
                vk = "int"
            elif isinstance(val, str):
                vk = "str"
            elif isinstance(val, list):
                vk = "list"
            else:
                vk = type(val).__name__
            qname = f"{module_stem}.{name}"
            results.append(PublicConstantContract(
                qname=qname, name=name, value=val, value_kind=vk,
                source_file=source_file, source_line=node.lineno,
            ))
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fingerprint_in_test(test_file: str, fingerprint: str, workspace: str = "") -> bool:
    """Check if fingerprint (substring or regex) appears in test file."""
    content = _read_file(test_file, workspace)
    if not content:
        return False
    if fingerprint in content:
        return True
    try:
        if re.search(fingerprint, content):
            return True
    except re.error:
        pass
    return False


def derive_test_path_for(contract) -> str:
    """Convention-based test path for a contract."""
    sf = getattr(contract, "source_file", "")
    stem = PurePosixPath(sf).stem if sf else "unknown"
    return f"agent/tests/test_{stem}.py"


def service_port_for_handler(handler_qname: str, ports: List[ServicePortContract]) -> Optional[int]:
    """Look up port whose source_file is in same module/dir as handler."""
    handler_module = handler_qname.split(".")[0] if handler_qname else ""
    for sp in ports:
        sp_stem = PurePosixPath(sp.source_file).stem
        sp_dir = str(PurePosixPath(sp.source_file).parent)
        if handler_module == sp_stem or handler_module in sp_dir:
            return sp.port
    return None


def _line_of(content: str, offset: int) -> int:
    """Return 1-based line number for byte offset in content."""
    return content[:offset].count("\n") + 1


def context_mentions_service(
    content: str, offset: int, service_name: str,
    aliases: Optional[Dict[str, List[str]]] = None,
    window: int = 5,
) -> bool:
    """Check if service_name (or alias) appears within +-window lines of offset."""
    lines = content.splitlines()
    line_no = content[:offset].count("\n")
    start = max(0, line_no - window)
    end = min(len(lines), line_no + window + 1)
    snippet = "\n".join(lines[start:end]).lower()
    names = [service_name.lower()]
    if aliases:
        names.extend(a.lower() for a in aliases.get(service_name, []))
    # Also check underscore-to-space and partial matches
    for n in names:
        if n in snippet:
            return True
        # 'manager_http_server' → check 'manager' too
        parts = n.split("_")
        if any(p in snippet for p in parts if len(p) > 3):
            return True
    return False


def score_service_port_match(
    sp: ServicePortContract,
    doc_content: str,
    port_offset: int,
    endpoints: Optional[List[EndpointContract]] = None,
) -> float:
    """Score how well a ServicePortContract matches a localhost:NNNN occurrence.

    R3 scoring factors:
      +3.0  constant_name within ±10 lines
      +2.0  handler_qname (from endpoints sharing source_file) within ±5 lines
      +1.0  service_name within ±5 lines
      +0.5  source_file path-fragment in same paragraph
    R6: Factored out for attribution scoring.
    """
    lines = doc_content.splitlines()
    line_no = doc_content[:port_offset].count("\n")
    score = 0.0

    # +3 constant_name within ±10 lines
    start10 = max(0, line_no - 10)
    end10 = min(len(lines), line_no + 11)
    snippet10 = "\n".join(lines[start10:end10]).lower()
    if sp.constant_name.lower() in snippet10:
        score += 3.0

    # +2 handler_qname within ±5 lines
    start5 = max(0, line_no - 5)
    end5 = min(len(lines), line_no + 6)
    snippet5 = "\n".join(lines[start5:end5]).lower()
    if endpoints:
        for ep in endpoints:
            if ep.source_file == sp.source_file:
                if ep.handler_qname.lower() in snippet5:
                    score += 2.0
                    break

    # +1 service_name within ±5 lines (exact match gets full score,
    # partial word-fragment match gets half to avoid false positives
    # like "server" matching for both "governance" and "manager_http_server")
    sn = sp.service_name.lower()
    if sn in snippet5:
        score += 1.0
    else:
        # Check underscore parts, but only unique long parts (>4 chars)
        parts = sn.split("_")
        long_parts = [p for p in parts if len(p) > 4]
        if long_parts and any(p in snippet5 for p in long_parts):
            score += 0.5

    # +0.5 source_file path-fragment in same paragraph
    # Find paragraph boundaries (blank lines)
    para_start = line_no
    while para_start > 0 and lines[para_start - 1].strip():
        para_start -= 1
    para_end = line_no
    while para_end < len(lines) - 1 and lines[para_end + 1].strip():
        para_end += 1
    paragraph = "\n".join(lines[para_start:para_end + 1]).lower()
    # Check source_file stem (exact stem match: +0.5, partial: +0.25)
    sf_stem = PurePosixPath(sp.source_file).stem.lower()
    if sf_stem in paragraph:
        score += 0.5
    else:
        sf_parts = [p for p in sf_stem.split("_") if len(p) > 4]
        if sf_parts and any(p in paragraph for p in sf_parts):
            score += 0.25

    # Path-prefix additive axis (R2: dominant weight, purely additive over R5 keyword_score)
    score += score_path_prefix_match(sp, doc_content, port_offset)

    return score


# ---------------------------------------------------------------------------
# Path-prefix scoring (R1-R4)
# ---------------------------------------------------------------------------

# R4: normalisation aliases — short name → list of full service_names it matches
_PATH_PREFIX_ALIASES: Dict[str, List[str]] = {
    "manager": ["manager_http_server"],
    "governance": ["governance"],
}


def score_path_prefix_match(
    sp: ServicePortContract,
    doc_content: str,
    port_offset: int,
    window: int = 5,
) -> float:
    """Score path-prefix match for a ServicePortContract near a localhost:NNNN.

    R1: Extracts /api/<svc>/ prefixes from HTTP URLs within ±window lines.
    R3: Only considers paths inside HTTP context (curl/http://).
    R4: Normalises via _PATH_PREFIX_ALIASES.
    R2: Returns +5.0 exact, +3.0 alias, 0.0 no match.
    """
    lines = doc_content.splitlines()
    line_no = doc_content[:port_offset].count("\n")
    start = max(0, line_no - window)
    end = min(len(lines), line_no + window + 1)
    snippet = "\n".join(lines[start:end])

    # R3: only extract /api/<svc>/ from http:// or curl contexts
    prefixes: List[str] = []
    for m in re.finditer(r'(?:curl\s+|https?://)[^\s]*?/api/([a-z_]+)/', snippet, re.IGNORECASE):
        prefixes.append(m.group(1).lower())

    if not prefixes:
        return 0.0

    sn_lower = sp.service_name.lower()
    # Build set of names that match this service port
    match_names = {sn_lower}
    # Add the short alias keys that map TO this service_name
    for alias_key, targets in _PATH_PREFIX_ALIASES.items():
        if sn_lower in (t.lower() for t in targets):
            match_names.add(alias_key)
        if alias_key == sn_lower:
            match_names.update(t.lower() for t in targets)

    best = 0.0
    for prefix in prefixes:
        if prefix == sn_lower or prefix in match_names:
            best = max(best, 5.0)  # exact or direct alias
        else:
            # Check reverse: prefix is an alias key that maps to sn_lower
            alias_targets = _PATH_PREFIX_ALIASES.get(prefix, [])
            if sn_lower in (t.lower() for t in alias_targets):
                best = max(best, 5.0)
            # Check if sn_lower starts with prefix (partial alias → +3.0)
            elif sn_lower.startswith(prefix + "_") or prefix.startswith(sn_lower + "_"):
                best = max(best, 3.0)
    return best


def find_endpoint_occurrence(content: str, method: str, path: str):
    """Find method+path mentions in doc content. Yield (line_no, port_in_curl)."""
    # Escape path for regex, allow placeholder params like {target}
    escaped = re.escape(path).replace(r"\{", "{").replace(r"\}", "}")
    pattern = re.compile(rf"{re.escape(method)}\s+.*?{escaped}", re.IGNORECASE)
    for m in pattern.finditer(content):
        line_no = _line_of(content, m.start())
        # Look for curl port in nearby lines
        lines = content.splitlines()
        start = max(0, line_no - 3)
        end = min(len(lines), line_no + 5)
        nearby = "\n".join(lines[start:end])
        port_match = re.search(r"localhost:(\d+)", nearby)
        port = int(port_match.group(1)) if port_match else None
        yield type("Occurrence", (), {"line_no": line_no, "port_in_curl_example": port})()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(ctx: "ReconcileContext", *, scope: Optional["ResolvedScope"] = None) -> list:
    """Run Phase K contract-test-coverage invariant check.

    Returns list of PhaseKDiscrepancy. Skips (returns []) when scope is None.
    """
    if scope is None:
        return []

    workspace = getattr(ctx, "workspace_path", "")
    all_files = scope.files()

    endpoints: List[EndpointContract] = []
    service_ports: List[ServicePortContract] = []
    constants: List[PublicConstantContract] = []

    for f in all_files:
        if not f.endswith(".py"):
            continue
        endpoints.extend(extract_endpoints(f, workspace))
        service_ports.extend(extract_service_ports(f, workspace))
        constants.extend(extract_public_constants(f, workspace))

    results: List[PhaseKDiscrepancy] = []

    # Step 2: contract_no_test
    test_files = [f for f in all_files if "test" in PurePosixPath(f).name.lower()]
    for c in list(endpoints) + list(service_ports):
        test_hits = []
        qname = getattr(c, "handler_qname", "")
        fingerprints = c.doc_fingerprints() + ([qname] if qname else [])
        for t in test_files:
            for fp in fingerprints:
                if not fp:
                    continue
                if fingerprint_in_test(t, fp, workspace):
                    test_hits.append((t, fp))
                    break
        if not test_hits:
            cid = getattr(c, "handler_qname", None) or getattr(c, "constant_name", "")
            results.append(PhaseKDiscrepancy(
                type="contract_no_test",
                contract_kind=type(c).__name__,
                contract_id=cid,
                contract_summary=str(c),
                expected_test_location=derive_test_path_for(c),
                confidence="high",
                priority="P0",
                suggested_action="spawn_pm_write_test",
                detail=f"No test coverage for {type(c).__name__}: {cid}",
            ))

    # Step 3: doc_value_drift — by contract role
    doc_files = [f for f in all_files if f.endswith(".md")]

    # 3a: Endpoint drift (method+path in doc, check curl port)
    for ep in endpoints:
        for doc in doc_files:
            doc_content = _read_file(doc, workspace)
            if not doc_content:
                continue
            for occ in find_endpoint_occurrence(doc_content, ep.method, ep.path):
                if occ.port_in_curl_example:
                    expected_port = service_port_for_handler(ep.handler_qname, service_ports)
                    if expected_port and occ.port_in_curl_example != expected_port:
                        results.append(PhaseKDiscrepancy(
                            type="doc_value_drift",
                            contract_kind="EndpointContract",
                            contract_id=f"{ep.method} {ep.path}",
                            doc=doc,
                            doc_line=occ.line_no,
                            doc_value=occ.port_in_curl_example,
                            code_value=expected_port,
                            drift_role="service_port",
                            confidence="high",
                            priority="P1",
                            suggested_action="spawn_pm_fix_doc",
                            detail=f"Doc {doc} line {occ.line_no}: port {occ.port_in_curl_example} != code {expected_port}",
                        ))

    # 3b: ServicePort drift (localhost:PORT near service context in docs)
    # R2: Score ALL known ServicePortContracts for each localhost:NNNN,
    # select the best-scoring candidate rather than greedily matching first.
    for doc in doc_files:
        doc_content = _read_file(doc, workspace)
        if not doc_content:
            continue
        for m in re.finditer(r"localhost:(\d+)", doc_content):
            doc_port = int(m.group(1))
            # Skip if port matches any known contract exactly
            if any(sp.port == doc_port for sp in service_ports):
                continue
            # Score each service port candidate
            candidates = []  # type: List[tuple]
            for sp in service_ports:
                sc = score_service_port_match(sp, doc_content, m.start(), endpoints)
                if sc > 0:
                    candidates.append((sc, sp))
            if not candidates:
                continue
            candidates.sort(key=lambda x: x[0], reverse=True)
            best_score, best_sp = candidates[0]
            # R3: ties → medium confidence; score<1 with multiple → ambiguous
            if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
                confidence = "medium"
            elif best_score < 1 and len(candidates) > 1:
                confidence = "ambiguous attribution"
            else:
                confidence = "high"
            results.append(PhaseKDiscrepancy(
                type="doc_value_drift",
                contract_kind="ServicePortContract",
                contract_id=best_sp.constant_name,
                doc=doc,
                doc_line=_line_of(doc_content, m.start()),
                doc_value=doc_port,
                code_value=best_sp.port,
                drift_role="service_port",
                confidence=confidence,
                priority="P1",
                suggested_action="spawn_pm_fix_doc",
                detail=f"Doc {doc}: localhost:{doc_port} attributed to '{best_sp.constant_name}' (score={best_score:.1f}) but code has {best_sp.port}",
            ))

    return results
