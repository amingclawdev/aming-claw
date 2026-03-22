"""HTTP server for the governance service.

Uses stdlib http.server (Starlette upgrade deferred to when dependencies are added).
Provides routing, middleware (auth, idempotency, request_id, audit), and JSON handling.
"""

import json
import sys
import uuid
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from .errors import GovernanceError
from .db import get_connection, DBContext
from . import role_service
from . import state_service
from . import project_service
from . import memory_service
from . import audit_service
from .idempotency import check_idempotency, store_idempotency
from .redis_client import get_redis
from .models import Evidence, MemoryEntry, NodeDef
from .enums import VerifyStatus
from .impact_analyzer import ImpactAnalyzer
from .models import ImpactAnalysisRequest, FileHitPolicy

import os
PORT = int(os.environ.get("GOVERNANCE_PORT", "30006"))

# --- Route Registry ---
ROUTES = []


def route(method: str, path: str):
    def decorator(fn):
        ROUTES.append((method, path, fn))
        return fn
    return decorator


class GovernanceHandler(BaseHTTPRequestHandler):
    """HTTP request handler with routing and middleware."""

    def _find_handler(self, method: str):
        path = urlparse(self.path).path.rstrip("/")
        for m, prefix, handler in ROUTES:
            if m != method:
                continue
            # Exact match or parameterized match
            if path == prefix:
                return handler, {}, ""
            # Simple path parameter matching: /api/wf/{project_id}/...
            parts_route = prefix.split("/")
            parts_path = path.split("/")
            if len(parts_route) != len(parts_path):
                continue
            params = {}
            match = True
            for rp, pp in zip(parts_route, parts_path):
                if rp.startswith("{") and rp.endswith("}"):
                    params[rp[1:-1]] = pp
                elif rp != pp:
                    match = False
                    break
            if match:
                return handler, params, ""
        return None, {}, ""

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _query_params(self) -> dict:
        parsed = urlparse(self.path)
        return {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()}

    def _respond(self, code: int, body: dict):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self, method: str):
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        handler, path_params, _ = self._find_handler(method)
        if not handler:
            self._respond(404, {"error": "not_found", "message": "Endpoint not found"})
            return
        try:
            ctx = RequestContext(
                handler=self,
                method=method,
                path_params=path_params,
                query=self._query_params(),
                body=self._read_body() if method == "POST" else {},
                request_id=request_id,
                token=self.headers.get("X-Gov-Token", ""),
                idem_key=self.headers.get("Idempotency-Key", ""),
            )
            result = handler(ctx)
            if isinstance(result, tuple):
                code, body = result
            else:
                code, body = 200, result
            body["request_id"] = request_id
            self._respond(code, body)
        except GovernanceError as e:
            body = e.to_dict()
            body["request_id"] = request_id
            self._respond(e.status, body)
        except Exception as e:
            traceback.print_exc()
            self._respond(500, {
                "error": "internal_error",
                "message": str(e),
                "request_id": request_id,
            })

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def log_message(self, format, *args):
        pass  # Suppress default logging


class RequestContext:
    """Encapsulates a single request's state."""
    def __init__(self, handler, method, path_params, query, body, request_id, token, idem_key):
        self.handler = handler
        self.method = method
        self.path_params = path_params
        self.query = query
        self.body = body
        self.request_id = request_id
        self.token = token
        self.idem_key = idem_key
        self._session = None
        self._conn = None

    def get_project_id(self) -> str:
        return self.path_params.get("project_id", self.body.get("project_id", ""))

    def require_auth(self, conn) -> dict:
        """Authenticate and return session. Caches result."""
        if self._session is None:
            self._session = role_service.authenticate(conn, self.token)
        return self._session


# ============================================================
# ROUTES
# ============================================================

# --- Init (one-time project initialization) ---

@route("POST", "/api/init")
def handle_init(ctx: RequestContext):
    """Human calls this once to create project + get coordinator token.
    Repeat call without password → 403.
    Repeat call with correct password → reset coordinator token.
    """
    result = project_service.init_project(
        project_id=ctx.body.get("project_id", ctx.body.get("project", "")),
        password=ctx.body.get("password", ""),
        project_name=ctx.body.get("project_name", ctx.body.get("name", "")),
        workspace_path=ctx.body.get("workspace_path", ""),
    )
    return 201, result


# --- Project ---


@route("GET", "/api/project/list")
def handle_project_list(ctx: RequestContext):
    return {"projects": project_service.list_projects()}


# --- Role (coordinator assigns roles to other agents) ---

@route("POST", "/api/role/assign")
def handle_role_assign(ctx: RequestContext):
    """Coordinator assigns a role+token to another agent."""
    project_id = ctx.body.get("project_id", "")
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = project_service.assign_role(
            conn, project_id, session,
            principal_id=ctx.body.get("principal_id", ""),
            role=ctx.body.get("role", ""),
            scope=ctx.body.get("scope"),
        )
    return 201, result


@route("POST", "/api/role/revoke")
def handle_role_revoke(ctx: RequestContext):
    """Coordinator revokes an agent's session."""
    project_id = ctx.body.get("project_id", "")
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = project_service.revoke_role(
            conn, project_id, session,
            session_id=ctx.body.get("session_id", ""),
        )
    return result


@route("POST", "/api/role/heartbeat")
def handle_heartbeat(ctx: RequestContext):
    # Need to find which project this session belongs to
    # First authenticate to get session
    # We check all projects (or the session tells us)
    # For simplicity, authenticate against a known project
    project_id = ctx.body.get("project_id", "")
    if not project_id:
        # Try to find from token
        rc = get_redis()
        from .role_service import _hash_token
        token_hash = _hash_token(ctx.token)
        session_id = rc.get_session_by_token(token_hash)
        if session_id:
            cached = rc.get_cached_session(session_id)
            if cached:
                project_id = cached.get("project_id", "")

    if not project_id:
        from .errors import AuthError
        raise AuthError("Cannot determine project. Provide project_id or use a valid token.")

    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = role_service.heartbeat(
            conn, session["session_id"],
            ctx.body.get("status", "idle"),
        )
    return result


@route("GET", "/api/role/{project_id}/sessions")
def handle_list_sessions(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        sessions = role_service.list_sessions(conn, project_id)
    return {"sessions": sessions}


# --- Workflow ---

@route("POST", "/api/wf/{project_id}/import-graph")
def handle_import_graph(ctx: RequestContext):
    """Import acceptance graph from a markdown file. Coordinator only."""
    project_id = ctx.get_project_id()
    md_path = ctx.body.get("md_path", ctx.body.get("graph_source", ""))
    if not md_path:
        from .errors import ValidationError
        raise ValidationError("md_path is required")
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        if session.get("role") != "coordinator":
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(session.get("role", ""), "import-graph",
                                        {"detail": "Only coordinator can import graphs"})
    result = project_service.import_graph(project_id, md_path)
    return result


@route("POST", "/api/wf/{project_id}/verify-update")
def handle_verify_update(ctx: RequestContext):
    project_id = ctx.get_project_id()

    with DBContext(project_id) as conn:
        # Idempotency check
        rc = get_redis()
        if ctx.idem_key:
            cached = rc.check_idempotency(ctx.idem_key)
            if cached:
                return cached

        session = ctx.require_auth(conn)
        graph = project_service.load_project_graph(project_id)

        result = state_service.verify_update(
            conn, project_id, graph,
            node_ids=ctx.body.get("nodes", []),
            target_status=ctx.body.get("status", ""),
            session=session,
            evidence_dict=ctx.body.get("evidence"),
        )

        # Store idempotency
        if ctx.idem_key:
            rc.store_idempotency(ctx.idem_key, result)

    return result


@route("POST", "/api/wf/{project_id}/baseline")
def handle_baseline(ctx: RequestContext):
    """Coordinator batch-sets historical node states, bypassing checks."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = state_service.set_baseline(
            conn, project_id,
            node_statuses=ctx.body.get("nodes", {}),
            session=session,
            reason=ctx.body.get("reason", ""),
        )
    return result


@route("POST", "/api/wf/{project_id}/release-gate")
def handle_release_gate(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        graph = project_service.load_project_graph(project_id)
        result = state_service.release_gate(
            conn, project_id, graph,
            scope=ctx.body.get("scope"),
            profile=ctx.body.get("profile"),
        )
    return result


@route("GET", "/api/wf/{project_id}/summary")
def handle_summary(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        return state_service.get_summary(conn, project_id)


@route("GET", "/api/wf/{project_id}/node/{node_id}")
def handle_get_node(ctx: RequestContext):
    project_id = ctx.get_project_id()
    node_id = ctx.path_params.get("node_id", "")
    with DBContext(project_id) as conn:
        state = state_service.get_node_status(conn, project_id, node_id)
        if state is None:
            from .errors import NodeNotFoundError
            raise NodeNotFoundError(node_id)
    graph = project_service.load_project_graph(project_id)
    node_def = graph.get_node(node_id)
    return {**state, "definition": node_def}


@route("GET", "/api/wf/{project_id}/impact")
def handle_impact(ctx: RequestContext):
    project_id = ctx.get_project_id()
    files_str = ctx.query.get("files", "")
    files = [f.strip() for f in files_str.split(",") if f.strip()] if files_str else []
    include_secondary = ctx.query.get("file_policy", "") == "primary+secondary"

    graph = project_service.load_project_graph(project_id)

    with DBContext(project_id) as conn:
        def get_status(nid):
            row = conn.execute(
                "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
                (project_id, nid),
            ).fetchone()
            return VerifyStatus.from_str(row["verify_status"]) if row else VerifyStatus.PENDING

        analyzer = ImpactAnalyzer(graph, get_status)
        request = ImpactAnalysisRequest(
            changed_files=files,
            file_policy=FileHitPolicy(match_primary=True, match_secondary=include_secondary),
        )
        return analyzer.analyze(request)


@route("GET", "/api/wf/{project_id}/export")
def handle_export(ctx: RequestContext):
    project_id = ctx.get_project_id()
    fmt = ctx.query.get("format", "json")
    graph = project_service.load_project_graph(project_id)

    if fmt == "mermaid":
        with DBContext(project_id) as conn:
            rows = conn.execute(
                "SELECT node_id, verify_status FROM node_state WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            statuses = {r["node_id"]: r["verify_status"] for r in rows}
        return {"mermaid": graph.export_mermaid(statuses), "node_count": graph.node_count()}
    elif fmt == "json":
        return {"nodes": {nid: graph.get_node(nid) for nid in graph.list_nodes()}}
    else:
        from .errors import ValidationError
        raise ValidationError(f"Unknown export format: {fmt}")


@route("POST", "/api/wf/{project_id}/rollback")
def handle_rollback(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = state_service.rollback(
            conn, project_id,
            target_version=ctx.body.get("target_version", 0),
            session=session,
        )
    return result


# --- Memory ---

@route("POST", "/api/mem/{project_id}/write")
def handle_mem_write(ctx: RequestContext):
    project_id = ctx.get_project_id()
    entry = MemoryEntry.from_dict(ctx.body)
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn) if ctx.token else {}
        result = memory_service.write_memory(conn, project_id, entry, session)
    return 201, result


@route("GET", "/api/mem/{project_id}/query")
def handle_mem_query(ctx: RequestContext):
    project_id = ctx.get_project_id()
    module = ctx.query.get("module")
    kind = ctx.query.get("kind")
    node = ctx.query.get("node")

    if node:
        entries = memory_service.query_by_related_node(project_id, node)
    elif kind:
        entries = memory_service.query_by_kind(project_id, kind, module)
    elif module:
        entries = memory_service.query_by_module(project_id, module)
    else:
        entries = memory_service.query_all(project_id)
    return {"entries": entries, "count": len(entries)}


# --- Audit ---

@route("GET", "/api/audit/{project_id}/log")
def handle_audit_log(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        entries = audit_service.read_log(
            conn, project_id,
            limit=int(ctx.query.get("limit", "100")),
            event_filter=ctx.query.get("event"),
            since=ctx.query.get("since"),
        )
    return {"entries": entries, "count": len(entries)}


@route("GET", "/api/audit/{project_id}/violations")
def handle_audit_violations(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        entries = audit_service.read_violations(
            conn, project_id,
            limit=int(ctx.query.get("limit", "100")),
            since=ctx.query.get("since"),
        )
    return {"entries": entries, "count": len(entries)}


# --- Health ---

@route("GET", "/api/health")
def handle_health(ctx: RequestContext):
    return {"status": "ok", "service": "governance", "port": PORT}


# ============================================================
# Server Entry Point
# ============================================================

def create_server(port: int = None) -> HTTPServer:
    p = port or PORT
    server = HTTPServer(("0.0.0.0", p), GovernanceHandler)
    return server


def main():
    # Enable Redis Pub/Sub bridge for EventBus
    from .event_bus import get_event_bus
    redis = get_redis()
    if redis.available:
        get_event_bus().enable_redis_bridge()
        print("EventBus: Redis Pub/Sub bridge enabled")
    else:
        print("EventBus: Redis unavailable, in-process only")

    server = create_server()
    print(f"Governance service listening on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
