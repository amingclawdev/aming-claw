"""HTTP server for the governance service.

Uses stdlib http.server (Starlette upgrade deferred to when dependencies are added).
Provides routing, middleware (auth, idempotency, request_id, audit), and JSON handling.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
import hashlib
import traceback
from datetime import datetime, timezone
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from .errors import GovernanceError
from .auto_chain import _DIRTY_IGNORE
import logging
import sqlite3
import time

log = logging.getLogger(__name__)
from .db import get_connection, DBContext, independent_connection
from . import role_service
from . import state_service
from . import project_service
from . import memory_service
from . import audit_service
from . import reconcile_session
from . import backlog_runtime
from .idempotency import check_idempotency, store_idempotency
from .redis_client import get_redis
from .models import Evidence, MemoryEntry, NodeDef
from .enums import VerifyStatus
from .impact_analyzer import ImpactAnalyzer
from .models import ImpactAnalysisRequest, FileHitPolicy

import os
import signal
import subprocess
PORT = int(os.environ.get("GOVERNANCE_PORT", "40000"))

# --- Server Version (dynamic with 30s cache) ---
_version_cache = {"value": "unknown", "ts": 0}


def get_server_version():
    """Return current git HEAD hash, cached for 30 seconds."""
    if time.time() - _version_cache["ts"] < 30:
        return _version_cache["value"]
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        ).stdout.strip()
        _version_cache["value"] = head or "unknown"
        _version_cache["ts"] = time.time()
    except Exception:
        pass
    return _version_cache["value"]


# Backward compatibility alias
SERVER_VERSION = get_server_version()
SERVER_PID = os.getpid()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_get(row, key: str, default=""):
    if row is None:
        return default
    try:
        value = row[key]
    except Exception:
        if isinstance(row, dict):
            value = row.get(key, default)
        else:
            value = default
    return default if value is None else value


def _apply_mf_takeover(conn, project_id: str, bug_id: str, body: dict, row, policy: dict) -> dict:
    """Hold/cancel an unfinished chain task when MF takes ownership."""
    current_task_id = str(_row_get(row, "current_task_id", "") or "")
    task_id = (
        str(body.get("taken_over_task_id") or body.get("takeover_task_id") or current_task_id or "")
        .strip()
    )
    action = str(body.get("takeover_action") or "").strip().lower()
    if not action and task_id:
        action = "hold_current_chain"
    if not action:
        action = "none"

    allowed = {"none", "hold_current_chain", "cancel_current_chain"}
    if action not in allowed:
        raise GovernanceError(
            "invalid_takeover_action",
            f"takeover_action must be one of {sorted(allowed)}, got: {action}",
            422,
        )
    takeover = {
        "action": action,
        "taken_over_task_id": task_id,
        "mf_id": body.get("mf_id", ""),
        "mf_type": policy.get("mf_type", ""),
        "actor": body.get("actor", "api"),
        "reason": body.get("takeover_reason") or body.get("reason", ""),
        "ts": _utc_now(),
    }
    if action == "none":
        takeover["outcome"] = "none"
        return takeover
    if not task_id:
        takeover["outcome"] = "no_task_id"
        return takeover

    task_row = conn.execute(
        "SELECT task_id, status, execution_status, metadata_json FROM tasks "
        "WHERE project_id = ? AND task_id = ?",
        (project_id, task_id),
    ).fetchone()
    if not task_row:
        takeover["outcome"] = "task_missing"
        return takeover

    prior_status = str(_row_get(task_row, "status", "") or "")
    prior_exec = str(_row_get(task_row, "execution_status", prior_status) or prior_status)
    takeover["prior_status"] = prior_status
    takeover["prior_execution_status"] = prior_exec

    task_meta = backlog_runtime.parse_json_object(_row_get(task_row, "metadata_json", "{}"))
    task_meta["mf_takeover"] = takeover
    task_meta["mf_superseded"] = True
    task_meta["mf_type"] = policy.get("mf_type", "")
    task_meta["bug_id"] = task_meta.get("bug_id") or bug_id

    terminal = {"succeeded", "failed", "cancelled", "timed_out", "design_mismatch"}
    if prior_exec in terminal or prior_status in terminal:
        conn.execute(
            "UPDATE tasks SET metadata_json = ?, updated_at = ? WHERE task_id = ?",
            (json.dumps(task_meta, ensure_ascii=False), _utc_now(), task_id),
        )
        takeover["outcome"] = "already_terminal"
        return takeover

    if action == "cancel_current_chain":
        new_status = "cancelled"
        error_message = takeover["reason"] or "Cancelled by MF takeover"
        conn.execute(
            """UPDATE tasks
               SET status = ?, execution_status = ?, completed_at = ?,
                   updated_at = ?, error_message = ?, metadata_json = ?
               WHERE task_id = ?""",
            (
                new_status,
                new_status,
                _utc_now(),
                _utc_now(),
                error_message,
                json.dumps(task_meta, ensure_ascii=False),
                task_id,
            ),
        )
    else:
        new_status = "observer_hold"
        conn.execute(
            """UPDATE tasks
               SET status = ?, execution_status = ?, updated_at = ?,
                   error_message = ?, metadata_json = ?
               WHERE task_id = ?""",
            (
                new_status,
                new_status,
                _utc_now(),
                takeover["reason"] or "Held by MF takeover",
                json.dumps(task_meta, ensure_ascii=False),
                task_id,
            ),
        )
    takeover["outcome"] = new_status
    return takeover


# ---------------------------------------------------------------------------
# SQLite BUSY retry helper
# ---------------------------------------------------------------------------
_BUSY_RETRY_DELAYS = (0.5, 1.0, 2.0)  # seconds between attempts 1→2, 2→3


def _retry_on_busy(fn, *args, **kwargs):
    """Call *fn* up to 3 times, retrying on SQLITE_BUSY / 'database is locked'.

    Uses an exponential-style back-off: 0.5 s → 1 s → 2 s between attempts.
    Intended for short write transactions (version-update, version-sync).

    Args:
        fn: Callable that performs the SQLite operation.  It must be
            idempotent or use INSERT OR REPLACE semantics so retries are safe.
        *args / **kwargs: Forwarded verbatim to *fn*.

    Returns:
        The return value of *fn* on success.

    Raises:
        sqlite3.OperationalError: Re-raised after all 3 attempts are exhausted.
    """
    last_exc = None
    for attempt, delay in enumerate(_BUSY_RETRY_DELAYS, start=1):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                last_exc = exc
                time.sleep(delay)
            else:
                raise
    # Final attempt (no sleep after this one)
    try:
        return fn(*args, **kwargs)
    except sqlite3.OperationalError:
        raise last_exc


def _acquire_pid_lock():
    """Write PID lockfile. Kill old process if still alive."""
    lock_dir = os.path.join(
        os.environ.get("SHARED_VOLUME_PATH",
                        os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "shared-volume")),
        "codex-tasks", "state")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, "governance.pid")

    # Check old PID
    if os.path.exists(lock_path):
        try:
            old_pid = int(open(lock_path).read().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, signal.SIGTERM)
                import logging
                logging.getLogger(__name__).info("Killed old governance process PID %d", old_pid)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass  # Old process already dead

    # Write new PID
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))

# --- Route Registry ---
ROUTES = []


def route(method: str, path: str):
    def decorator(fn):
        ROUTES.append((method, path, fn))
        return fn
    return decorator


class GovernanceHandler(BaseHTTPRequestHandler):
    """HTTP request handler with routing and middleware."""

    CORS_HEADERS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": (
            "Content-Type, Authorization, X-Gov-Token, Idempotency-Key, X-Requested-With"
        ),
        "Access-Control-Expose-Headers": "X-Request-Id",
    }

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

    def _respond(self, code: int, body: dict, extra_headers: dict | None = None):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            headers = dict(self.CORS_HEADERS)
            if extra_headers:
                headers.update(extra_headers)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            # observer-hotfix 2026-04-25: Windows clients drop connections
            # mid-write (gateway timeouts, executor restarts). Don't let
            # the connection death propagate up the request thread and
            # crash the gov server. Just log and move on.
            log.debug("client connection dropped during _respond: %s", e)

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
            if isinstance(result, tuple) and len(result) == 3:
                code, body, extra_headers = result
            elif isinstance(result, tuple):
                # Support both (code, body) and (body, code) return styles
                if isinstance(result[0], int):
                    code, body = result[0], result[1]
                else:
                    body, code = result[0], result[1]
                extra_headers = None
            else:
                code, body = 200, result
                extra_headers = None
            body["request_id"] = request_id
            self._respond(code, body, extra_headers)
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

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            for k, v in self.CORS_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            log.debug("client connection dropped during CORS preflight: %s", e)

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
        raw = self.path_params.get("project_id", self.body.get("project_id", ""))
        return project_service._normalize_project_id(raw) if raw else raw

    def require_auth(self, conn) -> dict:
        """Authenticate and return session. Caches result.

        Token-free mode: when no token is provided, returns a default
        coordinator session so all APIs work without authentication.
        Tokens still work if provided (for backward compatibility).
        """
        if self._session is None:
            if not self.token:
                # Anonymous access — full coordinator permissions
                project_id = self.get_project_id()
                self._session = {
                    "session_id": "anonymous",
                    "principal_id": "anonymous",
                    "project_id": project_id,
                    "role": "coordinator",
                    "scope": [],
                    "token": "",
                    "permissions": ["*"],
                }
            else:
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


@route("POST", "/api/project/bootstrap")
def handle_project_bootstrap(ctx: RequestContext):
    """Bootstrap a project from workspace (R1).

    Body: {
        "workspace_path": "/path/to/project" (required),
        "project_name": "my-project" (optional),
        "config_override": {} (optional),
        "scan_depth": 3 (optional),
        "exclude_patterns": [] (optional),
    }
    Returns: {project_id, graph_stats, config, preflight, warning?}
    """
    workspace_path = ctx.body.get("workspace_path", "").strip()
    if not workspace_path:
        return 400, {"error": "workspace_path is required"}

    try:
        result = project_service.bootstrap_project(
            workspace_path=workspace_path,
            project_name=ctx.body.get("project_name", ""),
            config_override=ctx.body.get("config_override"),
            scan_depth=ctx.body.get("scan_depth", 3),
            exclude_patterns=ctx.body.get("exclude_patterns"),
        )
        return 200, result
    except Exception as e:
        return 400, {"error": str(e)}


@route("GET", "/api/project/list")
def handle_project_list(ctx: RequestContext):
    return {"projects": project_service.list_projects()}


@route("POST", "/api/projects/register")
def handle_project_register(ctx: RequestContext):
    """Register a project workspace with config validation.

    Body: {"workspace_path": "/path/to/project"}
    Returns: {"project_id", "config_hash", "registered": true}
    """
    import sys as _sys
    _agent_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)))
    if _agent_dir not in _sys.path:
        _sys.path.insert(0, _agent_dir)

    workspace_path = ctx.body.get("workspace_path", "").strip()
    if not workspace_path:
        return 400, {"error": "workspace_path is required"}

    from pathlib import Path
    ws = Path(workspace_path)

    # In Docker, host paths are not accessible — skip path validation
    # but still validate config if accessible
    try:
        from project_config import load_project_config, validate_commands
        config = load_project_config(ws)
    except (ValueError, FileNotFoundError) as e:
        # Path not accessible (Docker) or no config — try /workspace mount
        workspace_mount = Path("/workspace")
        if workspace_mount.exists():
            try:
                config = load_project_config(workspace_mount)
            except (ValueError, FileNotFoundError) as e2:
                return 400, {"error": f"config not found: {e2}"}
        else:
            return 400, {"error": f"config not found: {e}"}

    # Command safety
    cmd_violations = validate_commands(config)
    if cmd_violations:
        return 400, {"error": "unsafe commands", "violations": cmd_violations}

    # Check uniqueness
    existing = project_service.get_project(config.project_id)
    if existing and existing.get("workspace_path") and existing["workspace_path"] != str(ws):
        return 409, {"error": f"project_id '{config.project_id}' already registered to different workspace"}

    # Register in governance
    project_id = config.project_id
    try:
        if not existing:
            project_service.init_project(
                project_id=project_id,
                password="auto-registered",
                project_name=config.project_id,
                workspace_path=str(ws),
            )
    except Exception as e:
        # May already exist with different password — that's OK
        if "already exists" not in str(e).lower():
            return 500, {"error": f"registration failed: {e}"}

    # workspace_registry removed — workspace info stored in governance projects.json

    return 201, {
        "project_id": project_id,
        "config_hash": str(hash(str(config))),
        "registered": True,
        "language": config.language,
        "test_command": config.testing.unit_command,
        "deploy_strategy": config.deploy.strategy,
    }


@route("GET", "/api/projects/{project_id}/config")
def handle_project_config(ctx: RequestContext):
    """Return resolved project config."""
    import sys as _sys
    _agent_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)))
    if _agent_dir not in _sys.path:
        _sys.path.insert(0, _agent_dir)

    project_id = ctx.get_project_id()
    try:
        from project_config import load_project_config
        from pathlib import Path
        # Try governance project workspace_path, then /workspace fallback
        proj_data = project_service.list_projects()
        ws_path = None
        for p in proj_data:
            if p.get("project_id") == project_id:
                ws_path = p.get("workspace_path", "")
                break
        if ws_path:
            config = load_project_config(Path(ws_path))
        elif Path('/workspace').exists():
            config = load_project_config(Path('/workspace'))
        else:
            return 404, {'error': f'no workspace registered for {project_id}'}
        return {
            "project_id": config.project_id,
            "language": config.language,
            "testing": {"unit_command": config.testing.unit_command, "e2e_command": config.testing.e2e_command},
            "build": {"command": config.build.command, "release_checks": config.build.release_checks},
            "deploy": {"strategy": config.deploy.strategy, "service_rules_count": len(config.deploy.service_rules)},
            "governance": {"enabled": config.governance.enabled, "test_tool_label": config.governance.test_tool_label},
        }
    except Exception as e:
        return 404, {"error": f"config not found: {e}"}


@route("POST", "/api/projects/{project_id}/explain")
def handle_project_explain(ctx: RequestContext):
    """Dry-run: explain what would happen for given changed files."""
    import sys as _sys
    _agent_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)))
    if _agent_dir not in _sys.path:
        _sys.path.insert(0, _agent_dir)

    project_id = ctx.get_project_id()
    changed_files = ctx.body.get("changed_files", [])
    try:
        from project_config import explain_config, load_project_config
        from pathlib import Path
        # Resolve workspace from governance project data
        proj_data = project_service.list_projects()
        ws_entry = None
        for p in proj_data:
            if p.get("project_id") == project_id and p.get("workspace_path"):
                ws_entry = {"path": p["workspace_path"]}
                break
        if ws_entry:
            config = load_project_config(Path(ws_entry['path']))
            # Build explain manually since explain_config uses registry
            from deploy_chain import detect_affected_services
            affected = detect_affected_services(changed_files, project_id=project_id) if changed_files else []
            return {
                "project_id": config.project_id,
                "test_command": config.testing.unit_command,
                "deploy_strategy": config.deploy.strategy,
                "affected_services": affected,
                "changed_files": changed_files,
            }
        else:
            ws = Path('/workspace')
            if ws.exists():
                config = load_project_config(ws)
                from deploy_chain import detect_affected_services
                affected = detect_affected_services(changed_files, project_id=project_id) if changed_files else []
                return {
                    "project_id": config.project_id,
                    "test_command": config.testing.unit_command,
                    "deploy_strategy": config.deploy.strategy,
                    "affected_services": affected,
                    "changed_files": changed_files,
                }
            else:
                return 404, {'error': f'no workspace registered for {project_id}'}
        return explain_config(project_id, changed_files=changed_files)
    except Exception as e:
        return 404, {"error": f"explain failed: {e}"}


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


@route("GET", "/api/role/verify")
def handle_role_verify(ctx: RequestContext):
    """Verify a token and return session info. Used by Gateway for auth."""
    if not ctx.token:
        from .errors import AuthError
        raise AuthError("Missing token")

    # Try to find session from token across all projects
    rc = get_redis()
    from .role_service import _hash_token
    th = _hash_token(ctx.token)
    session_id = rc.get_session_by_token(th) if rc else None
    project_id = ""

    if session_id:
        cached = rc.get_cached_session(session_id)
        if cached:
            project_id = cached.get("project_id", "")

    if not project_id:
        # Fallback: scan projects
        for p in project_service.list_projects():
            try:
                with DBContext(p["project_id"]) as conn:
                    session = role_service.authenticate(conn, ctx.token)
                    return {
                        "valid": True,
                        "session_id": session["session_id"],
                        "principal_id": session.get("principal_id", ""),
                        "role": session.get("role", ""),
                        "project_id": p["project_id"],
                    }
            except Exception:
                continue
        from .errors import AuthError
        raise AuthError("Invalid token")

    with DBContext(project_id) as conn:
        session = role_service.authenticate(conn, ctx.token)
        return {
            "valid": True,
            "session_id": session["session_id"],
            "principal_id": session.get("principal_id", ""),
            "role": session.get("role", ""),
            "project_id": project_id,
        }


@route("GET", "/api/role/{project_id}/sessions")
def handle_list_sessions(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        sessions = role_service.list_sessions(conn, project_id)
    return {"sessions": sessions}


# --- Token ---

@route("POST", "/api/token/revoke")
def handle_token_revoke(ctx: RequestContext):
    """Revoke a refresh token."""
    refresh_token = ctx.body.get("refresh_token", "")
    if not refresh_token:
        from .errors import ValidationError
        raise ValidationError("refresh_token required")

    from . import token_service
    for p in project_service.list_projects():
        try:
            with DBContext(p["project_id"]) as conn:
                return token_service.revoke_refresh_token(conn, refresh_token)
        except Exception:
            continue
    from .errors import AuthError
    raise AuthError("Token not found")


@route("POST", "/api/token/rotate")
def handle_token_rotate(ctx: RequestContext):
    """DEPRECATED (v5): Use revoke + re-init instead.
    Removal timeline: deprecated since v5, scheduled for removal in v8.
    """
    # Deprecation headers: deprecated since v5, removal planned for v8
    _deprecation_headers = {
        "X-Deprecated-Since": "v5",
        "X-Removal-Date": "v8",
    }
    refresh_token = ctx.body.get("refresh_token", "")
    if not refresh_token:
        from .errors import ValidationError
        raise ValidationError("refresh_token required")

    from . import token_service
    for p in project_service.list_projects():
        try:
            with DBContext(p["project_id"]) as conn:
                result = token_service.rotate_refresh_token(conn, refresh_token)
                return 200, result, _deprecation_headers
        except Exception:
            continue
    from .errors import AuthError
    raise AuthError("Token not found")


# --- Agent Lifecycle ---

@route("POST", "/api/agent/register")
def handle_agent_register(ctx: RequestContext):
    """Register an agent and get a lease."""
    project_id = ctx.body.get("project_id", "")
    if not project_id:
        from .errors import ValidationError
        raise ValidationError("project_id required")

    from . import agent_lifecycle
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        return agent_lifecycle.register_agent(
            conn, project_id, session,
            expected_duration_sec=int(ctx.body.get("expected_duration_sec", 0)),
        )


@route("POST", "/api/agent/heartbeat")
def handle_agent_heartbeat(ctx: RequestContext):
    """Renew agent lease."""
    lease_id = ctx.body.get("lease_id", "")
    if not lease_id:
        from .errors import ValidationError
        raise ValidationError("lease_id required")

    from . import agent_lifecycle
    return agent_lifecycle.heartbeat(
        lease_id, status=ctx.body.get("status", "idle"),
    )


@route("POST", "/api/agent/deregister")
def handle_agent_deregister(ctx: RequestContext):
    """Deregister an agent."""
    lease_id = ctx.body.get("lease_id", "")
    if not lease_id:
        from .errors import ValidationError
        raise ValidationError("lease_id required")

    from . import agent_lifecycle
    return agent_lifecycle.deregister(lease_id)


@route("GET", "/api/agent/orphans")
def handle_agent_orphans(ctx: RequestContext):
    """List orphaned agents (expired leases)."""
    project_id = ctx.query.get("project_id", "")
    from . import agent_lifecycle
    orphans = agent_lifecycle.find_orphans(project_id or None)
    return {"orphans": orphans, "count": len(orphans)}


@route("POST", "/api/agent/cleanup")
def handle_agent_cleanup(ctx: RequestContext):
    """Clean up orphaned agents. Coordinator only."""
    project_id = ctx.body.get("project_id", "")
    if not project_id:
        from .errors import ValidationError
        raise ValidationError("project_id required")

    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        if session.get("role") != "coordinator":
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(session.get("role", ""), "agent.cleanup",
                                        {"detail": "Only coordinator can cleanup orphans"})

    from . import agent_lifecycle
    return agent_lifecycle.cleanup_orphans(project_id)


# --- Session Context ---

@route("POST", "/api/context/{project_id}/save")
def handle_context_save(ctx: RequestContext):
    """Save session context snapshot."""
    project_id = ctx.get_project_id()
    from . import session_context
    return session_context.save_snapshot(
        project_id, ctx.body.get("context", ctx.body),
        expected_version=ctx.body.get("expected_version"),
    )


@route("GET", "/api/context/{project_id}/load")
def handle_context_load(ctx: RequestContext):
    """Load session context snapshot."""
    project_id = ctx.get_project_id()
    from . import session_context
    data = session_context.load_snapshot(project_id)
    if data is None:
        return {"context": None, "exists": False}
    return {"context": data, "exists": True}


@route("POST", "/api/context/{project_id}/log")
def handle_context_log_append(ctx: RequestContext):
    """Append entry to session log."""
    project_id = ctx.get_project_id()
    from . import session_context
    return session_context.append_log(
        project_id,
        entry_type=ctx.body.get("type", "action"),
        content=ctx.body.get("content", {}),
    )


@route("GET", "/api/context/{project_id}/log")
def handle_context_log_read(ctx: RequestContext):
    """Read session log entries."""
    project_id = ctx.get_project_id()
    from . import session_context
    entries = session_context.read_log(project_id, limit=int(ctx.query.get("limit", "50")))
    return {"entries": entries, "count": len(entries)}


@route("POST", "/api/context/{project_id}/assemble")
def handle_context_assemble(ctx: RequestContext):
    """Assemble context from dbservice for a task type."""
    project_id = ctx.get_project_id()
    task_type = ctx.body.get("task_type", "dev_general")
    token_budget = int(ctx.body.get("token_budget", 5000))

    import requests as http_requests
    dbservice_url = os.environ.get("DBSERVICE_URL", "")
    if not dbservice_url:
        return {"context": [], "degraded": True, "reason": "DBSERVICE_URL not set"}

    try:
        resp = http_requests.post(
            f"{dbservice_url}/assemble-context",
            json={"taskType": task_type, "scope": project_id, "tokenBudget": token_budget},
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"context": [], "degraded": True, "reason": f"dbservice returned {resp.status_code}"}
    except Exception as e:
        return {"context": [], "degraded": True, "reason": str(e)}


@route("POST", "/api/context/{project_id}/archive")
def handle_context_archive(ctx: RequestContext):
    """Archive context to long-term memory and clear."""
    project_id = ctx.get_project_id()
    from . import session_context
    return session_context.archive_context(project_id)


# --- Workflow ---

@route("POST", "/api/wf/{project_id}/import-graph")
def handle_import_graph(ctx: RequestContext):
    """Import acceptance graph from a markdown file.

    Coordinator can always import. Observer can import only as a governance
    recovery action and must provide a non-empty reason.
    """
    project_id = ctx.get_project_id()
    md_path = ctx.body.get("md_path", ctx.body.get("graph_source", ""))
    reason = str(ctx.body.get("reason", "")).strip()
    if not md_path:
        from .errors import ValidationError
        raise ValidationError("md_path is required")
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        role = session.get("role", "")
        if role not in ("coordinator", "observer"):
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(role, "import-graph",
                                        {"detail": "Only coordinator or observer can import graphs"})
        if role == "observer" and not reason:
            from .errors import ValidationError
            raise ValidationError("reason is required for observer import-graph")
    result = project_service.import_graph(project_id, md_path)
    with DBContext(project_id) as conn:
        audit_service.record(
            conn, project_id,
            "observer_graph_import" if role == "observer" else "graph_import",
            actor=session.get("principal_id", ""),
            role=role,
            reason=reason,
            graph_source=md_path,
            graph_nodes=result.get("node_count", 0),
            node_states_initialized=result.get("node_states_initialized", 0),
        )
    return result


@route("POST", "/api/wf/{project_id}/observer-sync-node-state")
def handle_observer_sync_node_state(ctx: RequestContext):
    """Rebuild runtime node_state rows from the persisted graph definition.

    This is a governance recovery path only. It does not mark nodes as verified.
    """
    project_id = ctx.get_project_id()
    reason = str(ctx.body.get("reason", "")).strip()
    if not reason:
        from .errors import ValidationError
        raise ValidationError("reason is required")

    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        role = session.get("role", "")
        if role not in ("coordinator", "observer"):
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(role, "observer-sync-node-state",
                                        {"detail": "Only coordinator or observer can sync node_state"})

    result = project_service.sync_node_state_from_graph(project_id)
    with DBContext(project_id) as conn:
        audit_service.record(
            conn, project_id,
            "observer_node_state_sync" if role == "observer" else "node_state_sync",
            actor=session.get("principal_id", ""),
            role=role,
            reason=reason,
            graph_nodes=result.get("graph_nodes", 0),
            node_states_initialized=result.get("node_states_initialized", 0),
            node_state_total=result.get("node_state_total", 0),
            repair_mode=result.get("repair_mode", ""),
        )
    return result


@route("POST", "/api/wf/{project_id}/reconcile")
def handle_reconcile(ctx: RequestContext):
    """Unified reconcile: scan/diff/merge/sync/verify with two-phase commit.

    Body: {workspace_path, scan_depth?, dry_run?, auto_fix_stale?, require_high_confidence_only?,
           max_auto_fix_count?, mark_orphans_waived?, update_version?, operator_id?}
    """
    from .reconcile import reconcile_project, MergeOptions

    project_id = ctx.get_project_id()
    body = ctx.body

    workspace_path = body.get("workspace_path", "")
    if not workspace_path:
        from .errors import ValidationError
        raise ValidationError("workspace_path is required")

    merge_options = MergeOptions(
        auto_fix_stale=body.get("auto_fix_stale", True),
        require_high_confidence_only=body.get("require_high_confidence_only", True),
        mark_orphans_waived=body.get("mark_orphans_waived", False),
        max_auto_fix_count=body.get("max_auto_fix_count", 50),
        dry_run=body.get("dry_run", False),
    )

    result = reconcile_project(
        project_id=project_id,
        workspace_path=workspace_path,
        scan_depth=body.get("scan_depth", 3),
        merge_options=merge_options,
        update_version=body.get("update_version", False),
        dry_run=body.get("dry_run", False),
        operator_id=body.get("operator_id", "observer"),
    )
    return result


@route("POST", "/api/wf/{project_id}/reconcile-v2")
def handle_reconcile_v2(ctx: RequestContext):
    """Reconcile V2: creates a reconcile task and returns task_id + status_url (R9).

    Body: {metadata?, _meta_circular?, scenario?, reason?, observer_acknowledged_by?}

    Returns: {task_id, status_url, status}
    """
    from . import task_registry

    project_id = ctx.get_project_id()
    body = ctx.body

    metadata = body.get("metadata") or {}
    if isinstance(metadata, str):
        import json as _json
        try:
            metadata = _json.loads(metadata)
        except Exception:
            metadata = {}

    # Forward meta-circular fields
    for key in ("_meta_circular", "scenario", "reason", "observer_acknowledged_by"):
        if key in body and key not in metadata:
            metadata[key] = body[key]

    # Forward scope fields if present
    scope_data = body.get("scope")
    if scope_data and isinstance(scope_data, dict):
        metadata["scope"] = scope_data

    # Forward legacy fields for compat
    for key in ("workspace_path", "dry_run", "phases", "auto_fix_threshold",
                "scan_depth", "since"):
        if key in body:
            metadata[key] = body[key]

    prompt = body.get("prompt", "Reconcile project graph and node state")

    with DBContext(project_id) as conn:
        result = task_registry.create_task(
            conn,
            project_id,
            prompt=prompt,
            task_type="reconcile",
            metadata=metadata,
            created_by=body.get("operator_id", "reconcile-v2-api"),
        )
        conn.commit()

    task_id = result["task_id"]
    return {
        "task_id": task_id,
        "status_url": f"/api/task/{project_id}/{task_id}",
        "status": result.get("status", "queued"),
    }


# ---------------------------------------------------------------------------
# CR0b: Reconcile session HTTP API
# ---------------------------------------------------------------------------

def _session_to_dict(sess) -> dict:
    """Serialize a ReconcileSession dataclass to a JSON-safe dict."""
    if sess is None:
        return None
    return {
        "project_id": sess.project_id,
        "session_id": sess.session_id,
        "run_id": sess.run_id,
        "status": sess.status,
        "started_at": sess.started_at,
        "finalized_at": sess.finalized_at,
        "cluster_count_total": sess.cluster_count_total,
        "cluster_count_resolved": sess.cluster_count_resolved,
        "cluster_count_failed": sess.cluster_count_failed,
        "bypass_gates": list(sess.bypass_gates or []),
        "started_by": sess.started_by,
        "snapshot_path": sess.snapshot_path,
        "snapshot_head_sha": sess.snapshot_head_sha,
        "base_commit_sha": getattr(sess, "base_commit_sha", "") or "",
        "target_branch": getattr(sess, "target_branch", "") or "",
        "target_head_sha": getattr(sess, "target_head_sha", "") or "",
        "finalize_error": dict(getattr(sess, "finalize_error", {}) or {}),
    }


def _row_to_session_dict(row) -> dict:
    """Serialize a sqlite3.Row from reconcile_sessions to a JSON-safe dict."""
    if row is None:
        return None
    raw = row["bypass_gates_json"] if row["bypass_gates_json"] is not None else "[]"
    try:
        bypass = list(json.loads(raw) or [])
    except Exception:
        bypass = []
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    finalize_error = {}
    if "finalize_error_json" in keys:
        try:
            finalize_error = dict(json.loads(row["finalize_error_json"] or "{}") or {})
        except Exception:
            finalize_error = {}
    return {
        "project_id": row["project_id"],
        "session_id": row["session_id"],
        "run_id": row["run_id"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finalized_at": row["finalized_at"],
        "cluster_count_total": int(row["cluster_count_total"] or 0),
        "cluster_count_resolved": int(row["cluster_count_resolved"] or 0),
        "cluster_count_failed": int(row["cluster_count_failed"] or 0),
        "bypass_gates": bypass,
        "started_by": row["started_by"],
        "snapshot_path": row["snapshot_path"],
        "snapshot_head_sha": row["snapshot_head_sha"],
        "base_commit_sha": row["base_commit_sha"] if "base_commit_sha" in keys else "",
        "target_branch": (
            row["target_branch"] if "target_branch" in keys and row["target_branch"]
            else reconcile_session.default_target_branch(row["project_id"], row["session_id"])
        ),
        "target_head_sha": (
            row["target_head_sha"] if "target_head_sha" in keys and row["target_head_sha"]
            else (row["base_commit_sha"] if "base_commit_sha" in keys else "")
        ),
        "finalize_error": finalize_error,
    }


@route("POST", "/api/reconcile/{project_id}/sessions/start")
def handle_reconcile_session_start(ctx: RequestContext):
    """Start a new reconcile session. 409 if one already exists."""
    project_id = ctx.get_project_id()
    body = ctx.body or {}
    bypass_gates = body.get("bypass_gates") or []
    started_by = body.get("started_by") or ""
    run_id = body.get("run_id")
    full_rebase = bool(body.get("full_rebase", False))
    dropped = body.get("dropped_cluster_fingerprints")
    base_commit_sha = body.get("base_commit_sha") or body.get("base_commit")
    target_branch = body.get("target_branch")
    try:
        with DBContext(project_id) as conn:
            from .db import _resolve_project_dir

            project_dir = _resolve_project_dir(project_id)
            # Pre-check: active session already exists?
            existing = reconcile_session.get_active_session(conn, project_id)
            if existing is not None:
                return 409, {
                    "error": "reconcile_session_active_exists",
                    "session_id": existing.session_id,
                    "status": existing.status,
                }
            sess = reconcile_session.start_session(
                conn, project_id,
                run_id=run_id,
                started_by=started_by or None,
                bypass_gates=list(bypass_gates),
                full_rebase=full_rebase,
                dropped_cluster_fingerprints=dropped,
                base_commit_sha=base_commit_sha,
                target_branch=target_branch,
                governance_dir=project_dir,
            )
    except reconcile_session.SessionAlreadyActiveError as exc:
        return 409, {
            "error": "reconcile_session_active_exists",
            "message": str(exc),
        }
    except ValueError as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
    return 201, {"session": _session_to_dict(sess)}


@route("GET", "/api/reconcile/{project_id}/sessions/active")
def handle_reconcile_session_active(ctx: RequestContext):
    """Return the active/finalizing session for a project, else null."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        sess = reconcile_session.get_active_session(conn, project_id)
    return {"session": _session_to_dict(sess)}


@route("GET", "/api/reconcile/{project_id}/sessions/history")
def handle_reconcile_session_history(ctx: RequestContext):
    """Return all sessions for a project ordered by started_at DESC."""
    project_id = ctx.get_project_id()
    try:
        limit = int(ctx.query.get("limit", "50"))
    except (TypeError, ValueError):
        limit = 50
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM reconcile_sessions WHERE project_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (project_id, max(1, limit)),
        ).fetchall()
    sessions = [_row_to_session_dict(r) for r in rows]
    return {"sessions": sessions, "count": len(sessions)}


@route("GET", "/api/reconcile/{project_id}/sessions/{session_id}")
def handle_reconcile_session_get(ctx: RequestContext):
    """Return a single session by id, else 404."""
    project_id = ctx.get_project_id()
    session_id = ctx.path_params.get("session_id", "")
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM reconcile_sessions WHERE project_id=? AND session_id=?",
            (project_id, session_id),
        ).fetchone()
    if row is None:
        return 404, {"error": "session_not_found", "session_id": session_id}
    return {"session": _row_to_session_dict(row)}


@route("POST", "/api/reconcile/{project_id}/sessions/{session_id}/doc-index")
def handle_reconcile_session_doc_index(ctx: RequestContext):
    """Generate final reconcile doc/test/source coverage report for signoff."""
    project_id = ctx.get_project_id()
    session_id = ctx.path_params.get("session_id", "")
    body = ctx.body or {}
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM reconcile_sessions WHERE project_id=? AND session_id=?",
            (project_id, session_id),
        ).fetchone()
        if row is None:
            return 404, {"error": "session_not_found", "session_id": session_id}
        try:
            from .db import _resolve_project_dir

            project_dir = _resolve_project_dir(project_id)
            candidate = Path(body.get("candidate_graph_path") or project_dir / "graph.rebase.candidate.json")
            overlay = Path(body.get("overlay_path") or project_dir / "graph.rebase.overlay.json")
            report = reconcile_session.generate_final_doc_index_report(
                conn,
                project_id,
                session_id,
                governance_dir=project_dir,
                candidate_graph_path=candidate,
                overlay_path=overlay,
                output_dir=project_dir,
            )
            conn.commit()
        except ValueError as exc:
            return 400, {"error": "doc_index_failed", "message": str(exc)}
    return {"result": report}


@route("POST", "/api/reconcile/{project_id}/sessions/{session_id}/finalize")
def handle_reconcile_session_finalize(ctx: RequestContext):
    """Transition session to finalizing then finalize it. Idempotent on already-finalized sessions."""
    project_id = ctx.get_project_id()
    session_id = ctx.path_params.get("session_id", "")
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM reconcile_sessions WHERE project_id=? AND session_id=?",
            (project_id, session_id),
        ).fetchone()
        if row is None:
            return 404, {"error": "session_not_found", "session_id": session_id}
        current_status = row["status"]
        if current_status == "finalized":
            # Idempotent: already finalized
            row2 = conn.execute(
                "SELECT * FROM reconcile_sessions WHERE project_id=? AND session_id=?",
                (project_id, session_id),
            ).fetchone()
            return {
                "result": {
                    "project_id": project_id,
                    "session_id": session_id,
                    "status": "finalized",
                    "finalized_at": row2["finalized_at"] if row2 else None,
                },
                "idempotent": True,
            }
        if current_status == "rolled_back":
            return 409, {
                "error": "session_terminal",
                "status": current_status,
                "message": "session is rolled_back; cannot finalize",
            }
        body = ctx.body or {}
        try:
            from .db import _resolve_project_dir

            project_dir = _resolve_project_dir(project_id)
            candidate_graph_path = project_dir / "graph.rebase.candidate.json"
            result = reconcile_session.finalize_session(
                conn,
                project_id,
                session_id,
                governance_dir=project_dir,
                graph_path=project_dir / "graph.json",
                workspace_dir=Path(__file__).resolve().parents[2],
                candidate_graph_path=(
                    candidate_graph_path if candidate_graph_path.exists() else None
                ),
                full_rebase=bool(body.get("full_rebase", False)),
            )
        except reconcile_session.SessionClusterGateError as exc:
            return 409, {
                "error": "reconcile_clusters_incomplete",
                "message": str(exc),
                "summary": exc.summary,
            }
        except ValueError as exc:
            return 400, {"error": "invalid_state", "message": str(exc)}
    return {
        "result": {
            "project_id": result.project_id,
            "session_id": result.session_id,
            "status": result.status,
            "finalized_at": result.finalized_at,
            "overlay_archived_to": result.overlay_archived_to,
            "graph_path": result.graph_path,
            "graph_backup_path": result.graph_backup_path,
            "materialized_node_count": result.materialized_node_count,
            "materialization_counts": result.materialization_counts,
        },
        "idempotent": False,
    }


@route("POST", "/api/reconcile/{project_id}/sessions/{session_id}/rollback")
def handle_reconcile_session_rollback(ctx: RequestContext):
    """Roll back an active or finalizing session. Writes audit event."""
    project_id = ctx.get_project_id()
    session_id = ctx.path_params.get("session_id", "")
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM reconcile_sessions WHERE project_id=? AND session_id=?",
            (project_id, session_id),
        ).fetchone()
        if row is None:
            return 404, {"error": "session_not_found", "session_id": session_id}
        try:
            body = ctx.body or {}
            from .db import _resolve_project_dir

            project_dir = _resolve_project_dir(project_id)
            result = reconcile_session.rollback_session(
                conn,
                project_id,
                session_id,
                governance_dir=project_dir,
                restore_graph_snapshot=bool(body.get("restore_graph_snapshot", False)),
            )
        except ValueError as exc:
            return 409, {"error": "invalid_state", "message": str(exc)}
        # Audit the rollback event
        try:
            audit_service.record(
                conn, project_id,
                event="reconcile_session.rolled_back",
                actor=(ctx.body or {}).get("actor", "anonymous"),
                ok=True, node_ids=None, request_id=ctx.request_id,
                session_id=session_id,
            )
        except Exception:
            log.debug("rollback audit failed (non-critical)", exc_info=True)
    return {
        "result": {
            "project_id": result.project_id,
            "session_id": result.session_id,
            "status": result.status,
            "rolled_back_at": result.rolled_back_at,
            "snapshot_path": result.snapshot_path,
        },
    }


# ---------------------------------------------------------------------------
# Reconcile Batch Memory HTTP API
# ---------------------------------------------------------------------------

@route("POST", "/api/reconcile/{project_id}/batch-memory")
def handle_reconcile_batch_memory_create(ctx: RequestContext):
    """Create or fetch durable batch memory for PM semantic merge context."""
    from . import reconcile_batch_memory as bm

    project_id = ctx.get_project_id()
    body = ctx.body or {}
    with DBContext(project_id) as conn:
        batch = bm.create_or_get_batch(
            conn,
            project_id,
            session_id=str(body.get("session_id") or ""),
            batch_id=body.get("batch_id"),
            created_by=str(body.get("created_by") or body.get("actor") or ""),
            initial_memory=body.get("initial_memory") if isinstance(body.get("initial_memory"), dict) else None,
        )
    return 201, {"ok": True, "batch": batch}


@route("GET", "/api/reconcile/{project_id}/batch-memory/{batch_id}")
def handle_reconcile_batch_memory_get(ctx: RequestContext):
    """Return one reconcile batch memory document."""
    from . import reconcile_batch_memory as bm

    project_id = ctx.get_project_id()
    batch_id = ctx.path_params.get("batch_id", "")
    with DBContext(project_id) as conn:
        batch = bm.get_batch(conn, project_id, batch_id)
    if not batch:
        return 404, {"error": "batch_memory_not_found", "batch_id": batch_id}
    return {"ok": True, "batch": batch}


@route("POST", "/api/reconcile/{project_id}/batch-memory/{batch_id}/pm-decision")
def handle_reconcile_batch_memory_pm_decision(ctx: RequestContext):
    """Record one PM semantic decision into batch memory."""
    from . import reconcile_batch_memory as bm

    project_id = ctx.get_project_id()
    batch_id = ctx.path_params.get("batch_id", "")
    body = ctx.body or {}
    cluster_fp = str(
        body.get("cluster_fingerprint")
        or body.get("cluster_id")
        or ctx.path_params.get("cluster_fingerprint", "")
    )
    try:
        with DBContext(project_id) as conn:
            batch = bm.record_pm_decision(
                conn,
                project_id,
                batch_id,
                cluster_fp,
                body,
            )
    except KeyError:
        return 404, {"error": "batch_memory_not_found", "batch_id": batch_id}
    except ValueError as exc:
        return 400, {"error": "invalid_pm_decision", "message": str(exc)}
    return {"ok": True, "batch": batch}


# ---------------------------------------------------------------------------
# CR3 — Reconcile Deferred-Cluster Queue HTTP API (R7)
# ---------------------------------------------------------------------------


def _deferred_cluster_row_to_dict(row) -> dict:
    if row is None:
        return {}
    out = {}
    for key in row.keys():
        out[key] = row[key]
    if isinstance(out.get("payload_json"), str):
        try:
            out["payload"] = json.loads(out["payload_json"]) if out["payload_json"] else {}
        except Exception:
            out["payload"] = {}
    return out


@route("GET", "/api/reconcile/{project_id}/deferred-clusters")
def handle_reconcile_deferred_clusters_list(ctx: RequestContext):
    """List queue rows; supports ?status=&priority=&run_id= filters."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    status_filter = ctx.query.get("status")
    priority_filter = ctx.query.get("priority")
    run_id_filter = ctx.query.get("run_id")
    sql = (
        "SELECT * FROM reconcile_deferred_clusters WHERE project_id = ?"
    )
    args: list = [project_id]
    if run_id_filter:
        sql += " AND run_id = ?"
        args.append(run_id_filter)
    if status_filter:
        sql += " AND status = ?"
        args.append(status_filter)
    if priority_filter is not None and priority_filter != "":
        try:
            sql += " AND priority = ?"
            args.append(int(priority_filter))
        except (TypeError, ValueError):
            pass
    sql += " ORDER BY priority ASC, first_seen_at ASC"

    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        q.ensure_schema(conn)
        rows = conn.execute(sql, tuple(args)).fetchall()
    items = [_deferred_cluster_row_to_dict(r) for r in rows]
    return {"clusters": items, "count": len(items)}


@route("GET", "/api/reconcile/{project_id}/deferred-clusters/summary")
def handle_reconcile_deferred_clusters_summary(ctx: RequestContext):
    """Return completion gate state for a project/run."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    run_id = ctx.query.get("run_id") or ""
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        summary = q.completion_summary(
            project_id,
            run_id=run_id or None,
            conn=conn,
        )
    return {"summary": summary}


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/register-run")
def handle_reconcile_deferred_clusters_register_run(ctx: RequestContext):
    """Register all FeatureClusters from a Phase Z run into the durable queue."""
    from . import reconcile_deferred_queue as q
    from . import auto_backlog_bridge

    project_id = ctx.get_project_id()
    body = ctx.body or {}
    run_id = str(body.get("run_id") or "").strip()
    clusters = body.get("feature_clusters") or body.get("clusters") or []
    if not run_id:
        return 400, {"error": "missing_run_id"}
    if not isinstance(clusters, list):
        return 400, {"error": "invalid_feature_clusters"}
    try:
        priority = int(body.get("priority", 100))
    except (TypeError, ValueError):
        priority = 100
    try:
        from .db import _resolve_project_dir

        project_dir = _resolve_project_dir(project_id)
        candidate_path = project_dir / "graph.rebase.candidate.json"
        overlay_path = project_dir / "graph.rebase.overlay.json"
        candidate_graph = {}
        if candidate_path.exists():
            candidate_graph = json.loads(candidate_path.read_text(encoding="utf-8"))
        clusters = [
            auto_backlog_bridge.enrich_feature_cluster_payload(
                cluster,
                candidate_graph=candidate_graph,
                candidate_graph_path=str(candidate_path),
                overlay_path=str(overlay_path),
                run_id=run_id,
            )
            for cluster in clusters
            if isinstance(cluster, dict)
        ]
    except Exception:
        log.warning(
            "reconcile_deferred_clusters.register-run: payload enrichment skipped",
            exc_info=True,
        )
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        result = q.register_feature_clusters(
            project_id,
            run_id,
            clusters,
            conn=conn,
            priority=priority,
        )
    return {"result": result}


@route("GET", "/api/reconcile/{project_id}/file-inventory")
def handle_reconcile_file_inventory_list(ctx: RequestContext):
    """List file inventory rows; supports ?run_id=&scan_status=&file_kind=&limit=."""
    from .reconcile_file_inventory import query_file_inventory

    project_id = ctx.get_project_id()
    try:
        limit = int(ctx.query.get("limit", "200"))
    except (TypeError, ValueError):
        limit = 200

    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        result = query_file_inventory(
            conn,
            project_id,
            run_id=ctx.query.get("run_id", ""),
            scan_status=ctx.query.get("scan_status", ""),
            file_kind=ctx.query.get("file_kind", ""),
            limit=limit,
        )
    result["project_id"] = project_id
    return result


# ---------------------------------------------------------------------------
# Graph Governance State API (proposal-graph-governance-unified-v3)
# ---------------------------------------------------------------------------

def _graph_governance_project_root(project_id: str, body: dict) -> Path:
    raw = (
        body.get("project_root")
        or body.get("workspace_path")
        or body.get("repo_root")
        or ""
    )
    if raw:
        return Path(str(raw)).resolve()
    for project in project_service.list_projects():
        if project.get("project_id") == project_id and project.get("workspace_path"):
            return Path(project["workspace_path"]).resolve()
    if project_id == "aming-claw":
        return Path(__file__).resolve().parents[2]
    from .errors import ValidationError
    raise ValidationError("project_root or workspace_path is required")


def _semantic_use_ai_from_body(body: dict) -> bool | None:
    if body.get("semantic_use_ai") is not None:
        return bool(body["semantic_use_ai"])
    if body.get("use_ai") is not None:
        return bool(body["use_ai"])
    if body.get("reviewer_use_ai") is not None:
        return bool(body["reviewer_use_ai"])
    if body.get("use_reviewer_ai") is not None:
        return bool(body["use_reviewer_ai"])
    return None


def _automation_mode_from_body(body: dict, *keys: str, default: str = "manual") -> str:
    for key in keys:
        if body.get(key) is None:
            continue
        mode = str(body.get(key) or "").strip().lower().replace("-", "_")
        break
    else:
        mode = default
    if mode in {"off", "disabled", "false"}:
        mode = "manual"
    if mode not in {"manual", "enqueue_only", "auto"}:
        from .errors import ValidationError
        raise ValidationError(
            f"automation mode must be one of manual, enqueue_only, auto; got {mode}"
        )
    return mode


def _semantic_ai_call_from_body(project_id: str, root: Path, body: dict):
    use_ai = _semantic_use_ai_from_body(body)
    if use_ai is False:
        return None
    try:
        from .reconcile_semantic_ai import build_semantic_ai_call
        from .reconcile_semantic_config import load_semantic_enrichment_config
        semantic_config = load_semantic_enrichment_config(
            project_root=root,
            config_path=body.get("semantic_config_path"),
        )
        if body.get("semantic_ai_provider") is not None:
            semantic_config.provider = str(body.get("semantic_ai_provider") or "")
        if body.get("semantic_ai_model") is not None:
            semantic_config.model = str(body.get("semantic_ai_model") or "")
        if body.get("semantic_ai_role") is not None:
            semantic_config.role = str(body.get("semantic_ai_role") or "")
        effective_use_ai = semantic_config.use_ai_default if use_ai is None else use_ai
        if not effective_use_ai:
            return None
        return build_semantic_ai_call(
            semantic_config=semantic_config,
            project_id=project_id,
            snapshot_id=str(body.get("snapshot_id") or body.get("run_id") or "candidate"),
            project_root=root,
        )
    except Exception:
        return None


def _semantic_ai_feature_limit_from_body(body: dict) -> int | None:
    value = body.get("semantic_ai_feature_limit")
    if value is None:
        value = body.get("ai_feature_limit")
    if value is None:
        return None
    return int(value)


def _semantic_ai_batch_kwargs_from_body(body: dict) -> dict:
    size = body.get("semantic_ai_batch_size")
    if size is None:
        size = body.get("ai_batch_size")
    return {
        "semantic_ai_batch_size": int(size) if size is not None else None,
        "semantic_ai_batch_by": str(
            body.get("semantic_ai_batch_by")
            or body.get("ai_batch_by")
            or "subsystem"
        ),
        "semantic_ai_input_mode": (
            body.get("semantic_ai_input_mode")
            or body.get("semantic_input_mode")
            or body.get("ai_input_mode")
        ),
        "semantic_dynamic_graph_state": _semantic_bool_from_body(
            body,
            "semantic_dynamic_graph_state",
            "dynamic_semantic_graph_state",
            default=None,
        ),
    }


def _semantic_bool_from_body(body: dict, *keys: str, default: bool | None = None) -> bool | None:
    for key in keys:
        if body.get(key) is None:
            continue
        value = body.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", ""}
        return bool(value)
    return default


def _semantic_state_kwargs_from_body(body: dict) -> dict:
    return {
        "semantic_graph_state": bool(
            _semantic_bool_from_body(body, "semantic_graph_state", "graph_state", default=True)
        ),
        "semantic_skip_completed": bool(
            _semantic_bool_from_body(body, "semantic_skip_completed", "skip_completed", default=True)
        ),
        "semantic_batch_memory": _semantic_bool_from_body(
            body,
            "semantic_batch_memory",
            "batch_memory",
            default=False,
        ),
        "semantic_batch_memory_id": body.get("semantic_batch_memory_id") or body.get("batch_memory_id"),
        "semantic_base_snapshot_id": body.get("semantic_base_snapshot_id") or body.get("base_snapshot_id"),
    }


def _semantic_selector_kwargs_from_body(body: dict) -> dict:
    return {
        "semantic_ai_scope": body.get("semantic_ai_scope") or body.get("ai_scope"),
        "semantic_node_ids": body.get("semantic_node_ids") or body.get("node_ids"),
        "semantic_layers": body.get("semantic_layers") or body.get("layers"),
        "semantic_quality_flags": body.get("semantic_quality_flags") or body.get("quality_flags"),
        "semantic_missing": body.get("semantic_missing") or body.get("missing"),
        "semantic_changed_paths": body.get("semantic_changed_paths") or body.get("changed_paths"),
        "semantic_path_prefixes": body.get("semantic_path_prefixes") or body.get("path_prefixes"),
        "semantic_selector_match": body.get("semantic_selector_match") or body.get("selector_match"),
        "semantic_include_structural": bool(
            body.get("semantic_include_structural")
            or body.get("include_structural")
        ),
    }


def _semantic_ai_config_kwargs_from_body(body: dict) -> dict:
    return {
        "semantic_ai_provider": (
            str(body.get("semantic_ai_provider"))
            if body.get("semantic_ai_provider") is not None
            else None
        ),
        "semantic_ai_model": (
            str(body.get("semantic_ai_model"))
            if body.get("semantic_ai_model") is not None
            else None
        ),
        "semantic_ai_role": (
            str(body.get("semantic_ai_role"))
            if body.get("semantic_ai_role") is not None
            else None
        ),
    }


def _query_int(query: dict, key: str, default: int) -> int:
    try:
        return int(query.get(key, default))
    except (TypeError, ValueError):
        return default


def _query_bool(query: dict, key: str, default: bool = False) -> bool:
    value = query.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _query_statuses(query: dict, key: str = "status") -> list[str]:
    raw = query.get(key, "")
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    return [str(value).strip() for value in values if str(value).strip()]


def _require_graph_governance_operator(ctx: RequestContext, conn, action: str) -> dict:
    session = ctx.require_auth(conn)
    role = session.get("role", "")
    if role not in ("observer", "coordinator"):
        from .errors import PermissionDeniedError
        raise PermissionDeniedError(role, action, {"detail": "Graph governance state operations are observer/coordinator only"})
    return session


def _raise_graph_api_validation(exc: Exception):
    from .errors import ValidationError
    raise ValidationError(str(exc)) from exc


def _raise_graph_api_conflict(exc: Exception):
    raise GovernanceError("graph_snapshot_conflict", str(exc), 409) from exc


def _resolve_graph_snapshot_id(conn, project_id: str, snapshot_id: str) -> str:
    if snapshot_id and snapshot_id != "active":
        return snapshot_id
    from . import graph_snapshot_store as store

    active = store.get_active_graph_snapshot(conn, project_id)
    if not active:
        from .errors import ValidationError
        raise ValidationError("no active graph snapshot for project")
    return active["snapshot_id"]


def _graph_drift_backlog_id(snapshot_id: str, path: str, drift_type: str, target_symbol: str) -> str:
    seed = f"{snapshot_id}|{path}|{drift_type}|{target_symbol}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    safe_type = re.sub(r"[^A-Za-z0-9]+", "-", drift_type or "drift").strip("-").upper()
    return f"GRAPH-DRIFT-{safe_type}-{digest}"


def _summarize_graph_drift_rows(rows: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    open_sample: list[dict] = []
    for row in rows:
        status = str(row.get("status") or "")
        drift_type = str(row.get("drift_type") or "")
        if status:
            by_status[status] = by_status.get(status, 0) + 1
        if drift_type:
            by_type[drift_type] = by_type.get(drift_type, 0) + 1
        if status == "open" and len(open_sample) < 20:
            open_sample.append({
                "path": row.get("path", ""),
                "drift_type": drift_type,
                "node_id": row.get("node_id", ""),
                "target_symbol": row.get("target_symbol", ""),
            })
    return {
        "total": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "by_type": dict(sorted(by_type.items())),
        "open_sample": open_sample,
    }


@route("GET", "/api/graph-governance/{project_id}/status")
def handle_graph_governance_status(ctx: RequestContext):
    """Return active graph snapshot, scan baseline, and pending scope status."""
    project_id = ctx.get_project_id()
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        status = store.graph_governance_status(conn, project_id)
        target_commit = str(ctx.query.get("target_commit") or "")
        if target_commit:
            status["strict_ready"] = store.strict_graph_ready(
                conn,
                project_id,
                target_commit=target_commit,
            )
        return {"ok": True, **status}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/dashboard")
def handle_graph_governance_dashboard(ctx: RequestContext):
    """Return a compact dashboard projection over graph, drift, and file state."""
    project_id = ctx.get_project_id()
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        status = store.graph_governance_status(conn, project_id)
        snapshot_id = str(ctx.query.get("snapshot_id") or status.get("active_snapshot_id") or "")
        snapshots = store.list_graph_snapshots(conn, project_id, limit=_query_int(ctx.query, "snapshot_limit", 10))
        file_state: dict = {
            "summary": {},
            "total_count": 0,
            "sample": [],
        }
        if snapshot_id:
            try:
                files = store.list_graph_snapshot_files(
                    conn,
                    project_id,
                    snapshot_id,
                    limit=_query_int(ctx.query, "file_sample_limit", 10),
                    scan_status=str(ctx.query.get("scan_status") or ""),
                )
                file_state = {
                    "summary": files["summary"],
                    "total_count": files["total_count"],
                    "filtered_count": files["filtered_count"],
                    "sample": files["files"],
                }
            except KeyError:
                file_state["error"] = "snapshot_file_inventory_not_found"
        drift_rows = store.list_graph_drift(
            conn,
            project_id,
            snapshot_id=snapshot_id,
            limit=_query_int(ctx.query, "drift_limit", 1000),
        ) if snapshot_id else []
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "status": status,
            "recent_snapshots": snapshots,
            "file_state": file_state,
            "drift_summary": _summarize_graph_drift_rows(drift_rows),
            "drift_sample": drift_rows[:_query_int(ctx.query, "drift_sample_limit", 20)],
        }
    finally:
        conn.close()


def _git_commit_subject(commit_sha: str) -> str:
    commit_sha = str(commit_sha or "").strip()
    if not commit_sha:
        return ""
    try:
        result = subprocess.run(
            ["git", "show", "-s", "--format=%s", commit_sha],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parents[2],
        )
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


@route("GET", "/api/graph-governance/{project_id}/commits")
def handle_graph_governance_commit_timeline(ctx: RequestContext):
    """Return commit-anchored graph snapshot timeline for dashboard navigation."""
    project_id = ctx.get_project_id()
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        status = store.graph_governance_status(conn, project_id)
        commits = store.list_commit_timeline(
            conn,
            project_id,
            limit=_query_int(ctx.query, "limit", 50),
            include_backlog=_query_bool(ctx.query, "include_backlog", True),
        )
        if _query_bool(ctx.query, "include_git_subject", True):
            for row in commits:
                row["subject"] = row.get("subject") or _git_commit_subject(row.get("commit_sha", ""))
        return {
            "ok": True,
            "project_id": project_id,
            "active_commit_sha": status.get("graph_snapshot_commit", ""),
            "active_snapshot_id": status.get("active_snapshot_id", ""),
            "pending_scope_reconcile_count": status.get("pending_scope_reconcile_count", 0),
            "commits": commits,
            "count": len(commits),
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/commits/{commit_sha}/graph-state")
def handle_graph_governance_commit_graph_state(ctx: RequestContext):
    """Resolve a commit to the graph snapshot dashboard should display."""
    project_id = ctx.get_project_id()
    commit_sha = ctx.path_params["commit_sha"]
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        try:
            result = store.resolve_commit_graph_state(conn, project_id, commit_sha)
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        result["subject"] = _git_commit_subject(commit_sha) if _query_bool(ctx.query, "include_git_subject", True) else ""
        return {"ok": True, **result}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots")
def handle_graph_governance_snapshot_list(ctx: RequestContext):
    """List graph snapshots for operator/dashboard review."""
    project_id = ctx.get_project_id()
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        snapshots = store.list_graph_snapshots(
            conn,
            project_id,
            statuses=_query_statuses(ctx.query),
            limit=_query_int(ctx.query, "limit", 50),
        )
        return {"ok": True, "project_id": project_id, "snapshots": snapshots, "count": len(snapshots)}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/correction-patches")
def handle_graph_governance_correction_patch_list(ctx: RequestContext):
    """List auditable graph correction patches for dashboard/observer review."""
    project_id = ctx.get_project_id()
    from . import graph_correction_patches as patches

    conn = get_connection(project_id)
    try:
        rows = patches.list_correction_patches(
            conn,
            project_id,
            statuses=_query_statuses(ctx.query),
            patch_type=str(ctx.query.get("patch_type") or ""),
            target_node_id=str(ctx.query.get("target_node_id") or ""),
            limit=_query_int(ctx.query, "limit", 100),
        )
        return {"ok": True, "project_id": project_id, "patches": rows, "count": len(rows)}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/correction-patches")
def handle_graph_governance_correction_patch_create(ctx: RequestContext):
    """Create a graph correction patch suggestion without mutating the graph."""
    project_id = ctx.get_project_id()
    body = ctx.body
    from . import graph_correction_patches as patches

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.correction-patch.create")
        try:
            result = patches.create_patch(
                conn,
                project_id,
                patch_id=body.get("patch_id"),
                patch_type=str(body.get("patch_type") or ""),
                patch_json=body.get("patch_json") if isinstance(body.get("patch_json"), dict) else {},
                evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {},
                status=str(body.get("status") or patches.PATCH_STATUS_PROPOSED),
                risk_level=str(body.get("risk_level") or "medium"),
                base_snapshot_id=str(body.get("base_snapshot_id") or ""),
                base_commit=str(body.get("base_commit") or ""),
                target_node_id=str(body.get("target_node_id") or ""),
                stable_key=str(body.get("stable_node_key") or ""),
                created_by=str(body.get("actor") or "observer"),
            )
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        conn.commit()
        return 201, {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/correction-patches/{patch_id}/accept")
def handle_graph_governance_correction_patch_accept(ctx: RequestContext):
    """Accept a graph correction patch so future reconcile runs replay it."""
    project_id = ctx.get_project_id()
    patch_id = ctx.path_params["patch_id"]
    body = ctx.body
    from . import graph_correction_patches as patches

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.correction-patch.accept")
        changed = patches.accept_patch(
            conn,
            project_id,
            patch_id,
            accepted_by=str(body.get("actor") or "observer"),
        )
        if not changed:
            _raise_graph_api_validation(ValueError(f"patch not found or not proposed: {patch_id}"))
        conn.commit()
        return {"ok": True, "project_id": project_id, "patch_id": patch_id, "status": "accepted"}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/correction-patches/{patch_id}/reject")
def handle_graph_governance_correction_patch_reject(ctx: RequestContext):
    """Reject a proposed/accepted graph correction patch."""
    project_id = ctx.get_project_id()
    patch_id = ctx.path_params["patch_id"]
    body = ctx.body
    from . import graph_correction_patches as patches

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.correction-patch.reject")
        changed = patches.reject_patch(
            conn,
            project_id,
            patch_id,
            rejected_by=str(body.get("actor") or "observer"),
            reason=str(body.get("reason") or ""),
        )
        if not changed:
            _raise_graph_api_validation(ValueError(f"patch not found or already terminal: {patch_id}"))
        conn.commit()
        return {"ok": True, "project_id": project_id, "patch_id": patch_id, "status": "rejected"}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/summary")
def handle_graph_governance_snapshot_summary(ctx: RequestContext):
    """Return compact dashboard summary for one graph snapshot."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        try:
            summary = store.summarize_graph_snapshot(conn, project_id, snapshot_id)
        except KeyError as exc:
            _raise_graph_api_validation(exc)
        return {"ok": True, **summary}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/nodes")
def handle_graph_governance_snapshot_nodes(ctx: RequestContext):
    """List indexed graph nodes for one snapshot."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        nodes = store.list_graph_snapshot_nodes(
            conn,
            project_id,
            snapshot_id,
            limit=_query_int(ctx.query, "limit", 200),
            offset=_query_int(ctx.query, "offset", 0),
            layer=str(ctx.query.get("layer") or ""),
            kind=str(ctx.query.get("kind") or ""),
        )
        return {"ok": True, "project_id": project_id, "snapshot_id": snapshot_id, "nodes": nodes, "count": len(nodes)}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/edges")
def handle_graph_governance_snapshot_edges(ctx: RequestContext):
    """List indexed graph edges for one snapshot."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        edges = store.list_graph_snapshot_edges(
            conn,
            project_id,
            snapshot_id,
            limit=_query_int(ctx.query, "limit", 500),
            offset=_query_int(ctx.query, "offset", 0),
            edge_type=str(ctx.query.get("edge_type") or ""),
        )
        return {"ok": True, "project_id": project_id, "snapshot_id": snapshot_id, "edges": edges, "count": len(edges)}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/files")
def handle_graph_governance_snapshot_files(ctx: RequestContext):
    """List snapshot file inventory rows for dashboard orphan/doc/test review."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        try:
            result = store.list_graph_snapshot_files(
                conn,
                project_id,
                snapshot_id,
                limit=_query_int(ctx.query, "limit", 200),
                offset=_query_int(ctx.query, "offset", 0),
                file_kind=str(ctx.query.get("file_kind") or ""),
                scan_status=str(ctx.query.get("scan_status") or ""),
                graph_status=str(ctx.query.get("graph_status") or ""),
                decision=str(ctx.query.get("decision") or ""),
                path_contains=str(ctx.query.get("path") or ""),
            )
        except KeyError as exc:
            _raise_graph_api_validation(exc)
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "summary": result["summary"],
            "total_count": result["total_count"],
            "filtered_count": result["filtered_count"],
            "files": result["files"],
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/dashboard-review")
def handle_graph_governance_snapshot_dashboard_review(ctx: RequestContext):
    """Return a dashboard-ready bundle with two graph views and review state."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    from . import reconcile_dashboard_review

    conn = get_connection(project_id)
    try:
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        try:
            return reconcile_dashboard_review.build_dashboard_review_bundle(
                conn,
                project_id,
                snapshot_id,
                node_limit=_query_int(ctx.query, "node_limit", 120),
                edge_limit=_query_int(ctx.query, "edge_limit", 240),
                queue_group_limit=_query_int(ctx.query, "queue_group_limit", 20),
                persist=_query_bool(ctx.query, "persist", True),
            )
        except KeyError as exc:
            _raise_graph_api_validation(exc)
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/query-traces/start")
def handle_graph_governance_query_trace_start(ctx: RequestContext):
    """Start an audited graph query trace for dashboard/AI/chain consumers."""
    project_id = ctx.get_project_id()
    body = ctx.body
    from . import graph_query_trace
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.query-trace.start")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, str(body.get("snapshot_id") or "active"))
        try:
            with sqlite_write_lock():
                result = graph_query_trace.start_trace(
                    conn,
                    project_id,
                    snapshot_id,
                    actor=str(body.get("actor") or "observer"),
                    query_source=str(body.get("query_source") or "api_debug"),
                    query_purpose=str(body.get("query_purpose") or "api_debug"),
                    run_id=str(body.get("run_id") or ""),
                    parent_task_id=str(body.get("parent_task_id") or ""),
                    budget=body.get("query_budget") if isinstance(body.get("query_budget"), dict) else None,
                    trace_id=body.get("trace_id") or None,
                )
                conn.commit()
            return result
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/query")
def handle_graph_governance_query(ctx: RequestContext):
    """Run one graph query and append it to an auditable trace."""
    project_id = ctx.get_project_id()
    body = ctx.body
    from . import graph_query_trace
    from .db import sqlite_write_lock

    tool = str(body.get("tool") or "")
    root = None
    if (
        body.get("project_root")
        or body.get("workspace_path")
        or body.get("repo_root")
        or tool in {"search_docs", "get_file_excerpt"}
    ):
        root = _graph_governance_project_root(project_id, body)
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.query")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, str(body.get("snapshot_id") or "active"))
        try:
            with sqlite_write_lock():
                result = graph_query_trace.traced_query(
                    conn,
                    project_id,
                    snapshot_id,
                    tool=tool,
                    args=body.get("args") if isinstance(body.get("args"), dict) else {},
                    trace_id=str(body.get("trace_id") or ""),
                    actor=str(body.get("actor") or "observer"),
                    query_source=str(body.get("query_source") or "api_debug"),
                    query_purpose=str(body.get("query_purpose") or "api_debug"),
                    run_id=str(body.get("run_id") or ""),
                    parent_task_id=str(body.get("parent_task_id") or ""),
                    budget=body.get("query_budget") if isinstance(body.get("query_budget"), dict) else None,
                    project_root=root,
                )
                conn.commit()
            return result
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/query-traces/{trace_id}/finish")
def handle_graph_governance_query_trace_finish(ctx: RequestContext):
    """Finish an audited graph query trace."""
    project_id = ctx.get_project_id()
    trace_id = ctx.path_params["trace_id"]
    body = ctx.body
    from . import graph_query_trace
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.query-trace.finish")
        try:
            with sqlite_write_lock():
                result = graph_query_trace.finish_trace(
                    conn,
                    project_id,
                    trace_id,
                    status=str(body.get("status") or "complete"),
                    reason=str(body.get("reason") or ""),
                )
                conn.commit()
            return result
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/query-traces/{trace_id}")
def handle_graph_governance_query_trace_get(ctx: RequestContext):
    """Return one audited graph query trace and event summary."""
    project_id = ctx.get_project_id()
    trace_id = ctx.path_params["trace_id"]
    from . import graph_query_trace

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.query-trace.get")
        try:
            return graph_query_trace.get_trace(conn, project_id, trace_id)
        except KeyError as exc:
            _raise_graph_api_validation(exc)
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/export-cache")
def handle_graph_governance_snapshot_export_cache(ctx: RequestContext):
    """Export a non-authoritative .aming-claw/cache graph.current.json."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.export-cache")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        try:
            result = store.export_graph_snapshot_cache(
                conn,
                project_id,
                snapshot_id,
                project_root=root,
                cache_dir=body.get("cache_dir") or None,
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        return 201, {"ok": True, **result}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/drift")
def handle_graph_governance_drift_list(ctx: RequestContext):
    """List graph drift ledger rows for dashboard/operator review."""
    project_id = ctx.get_project_id()
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        rows = store.list_graph_drift(
            conn,
            project_id,
            snapshot_id=str(ctx.query.get("snapshot_id") or ""),
            status=str(ctx.query.get("status") or ""),
            drift_type=str(ctx.query.get("drift_type") or ""),
            limit=_query_int(ctx.query, "limit", 200),
            offset=_query_int(ctx.query, "offset", 0),
        )
        return {"ok": True, "project_id": project_id, "drift": rows, "count": len(rows)}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/drift")
def handle_graph_governance_drift_record(ctx: RequestContext):
    """Record one graph drift row with evidence."""
    project_id = ctx.get_project_id()
    body = ctx.body
    required = ["snapshot_id", "commit_sha", "path", "drift_type"]
    missing = [key for key in required if not str(body.get(key) or "").strip()]
    if missing:
        from .errors import ValidationError
        raise ValidationError(f"missing required drift field(s): {', '.join(missing)}")
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.drift.record")
        with sqlite_write_lock():
            store.record_drift(
                conn,
                project_id,
                snapshot_id=str(body.get("snapshot_id") or ""),
                commit_sha=str(body.get("commit_sha") or ""),
                path=str(body.get("path") or ""),
                drift_type=str(body.get("drift_type") or ""),
                target_symbol=str(body.get("target_symbol") or ""),
                node_id=str(body.get("node_id") or ""),
                status=str(body.get("status") or "open"),
                evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {
                    "source": "graph_governance_api",
                    "actor": body.get("actor", "api"),
                },
            )
            conn.commit()
        row = store.list_graph_drift(
            conn,
            project_id,
            snapshot_id=str(body.get("snapshot_id") or ""),
            drift_type=str(body.get("drift_type") or ""),
            status=str(body.get("status") or "open"),
            limit=20,
        )
        return 201, {"ok": True, "project_id": project_id, "drift": row}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/drift/file-backlog")
def handle_graph_governance_drift_file_backlog(ctx: RequestContext):
    """File one graph drift row into backlog and mark it backlog_filed."""
    project_id = ctx.get_project_id()
    body = ctx.body
    required = ["snapshot_id", "path", "drift_type"]
    missing = [key for key in required if not str(body.get(key) or "").strip()]
    if missing:
        from .errors import ValidationError
        raise ValidationError(f"missing required drift field(s): {', '.join(missing)}")
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.drift.file-backlog")
        with sqlite_write_lock():
            snapshot_id = _resolve_graph_snapshot_id(conn, project_id, str(body.get("snapshot_id") or ""))
            target_symbol_raw = body.get("target_symbol")
            target_symbol = None if target_symbol_raw is None else str(target_symbol_raw)
            try:
                drift = store.get_graph_drift(
                    conn,
                    project_id,
                    snapshot_id=snapshot_id,
                    path=str(body.get("path") or ""),
                    drift_type=str(body.get("drift_type") or ""),
                    target_symbol=target_symbol,
                )
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)

            bug_id = str(body.get("bug_id") or "").strip() or _graph_drift_backlog_id(
                snapshot_id,
                drift["path"],
                drift["drift_type"],
                drift["target_symbol"],
            )
            now = _utc_now()
            actor = str(body.get("actor") or "graph_governance_api")
            title = str(body.get("title") or "").strip() or (
                f"Resolve graph drift: {drift['drift_type']} in {drift['path']}"
            )
            details_md = str(body.get("details_md") or "").strip()
            if not details_md:
                details_md = "\n".join([
                    f"Graph drift row filed from snapshot `{snapshot_id}`.",
                    "",
                    f"- path: `{drift['path']}`",
                    f"- drift_type: `{drift['drift_type']}`",
                    f"- node_id: `{drift['node_id']}`",
                    f"- target_symbol: `{drift['target_symbol']}`",
                    f"- commit: `{drift['commit_sha']}`",
                    "",
                    "Review the graph/drift evidence, then either repair through chain or explicitly waive.",
                ])
            acceptance = body.get("acceptance_criteria")
            if not isinstance(acceptance, list):
                acceptance = [
                    "Drift row is fixed, waived, or converted into a more precise graph/document/test task.",
                    "Backlog close evidence references the graph snapshot and affected path.",
                    "Scope reconcile materializes graph state after any merge.",
                ]
            priority = str(body.get("priority") or "P2")
            target_files = body.get("target_files") if isinstance(body.get("target_files"), list) else [drift["path"]]
            conn.execute(
                """INSERT INTO backlog_bugs
                   (bug_id, title, status, priority, target_files, test_files,
                    acceptance_criteria, chain_task_id, "commit", discovered_at,
                    fixed_at, details_md, chain_trigger_json, required_docs,
                    provenance_paths, bypass_policy_json, mf_type, takeover_json,
                    created_at, updated_at)
                   VALUES (?, ?, 'OPEN', ?, ?, ?, ?, '', ?, ?, '', ?, ?, ?, ?, '{}', '', '{}', ?, ?)
                   ON CONFLICT(bug_id) DO UPDATE SET
                     title = excluded.title,
                     status = 'OPEN',
                     priority = excluded.priority,
                     target_files = excluded.target_files,
                     acceptance_criteria = excluded.acceptance_criteria,
                     details_md = excluded.details_md,
                     chain_trigger_json = excluded.chain_trigger_json,
                     provenance_paths = excluded.provenance_paths,
                     updated_at = excluded.updated_at
                """,
                (
                    bug_id,
                    title,
                    priority,
                    json.dumps(target_files, ensure_ascii=False, sort_keys=True),
                    json.dumps(body.get("test_files") if isinstance(body.get("test_files"), list) else [], ensure_ascii=False),
                    json.dumps(acceptance, ensure_ascii=False, sort_keys=True),
                    drift["commit_sha"],
                    now,
                    details_md,
                    json.dumps({
                        "source": "graph_drift_ledger",
                        "snapshot_id": snapshot_id,
                        "drift_type": drift["drift_type"],
                        "graph_gate_mode": "advisory",
                    }, ensure_ascii=False, sort_keys=True),
                    json.dumps(body.get("required_docs") if isinstance(body.get("required_docs"), list) else [], ensure_ascii=False),
                    json.dumps([drift["path"], f"graph_snapshot:{snapshot_id}"], ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            filed = store.update_graph_drift_status(
                conn,
                project_id,
                snapshot_id=snapshot_id,
                path=drift["path"],
                drift_type=drift["drift_type"],
                target_symbol=drift["target_symbol"],
                status="backlog_filed",
                evidence={
                    "backlog_bug_id": bug_id,
                    "filed_by": actor,
                    "filed_at": now,
                },
            )
            try:
                audit_service.record(
                    conn,
                    project_id,
                    "graph_drift_backlog_filed",
                    actor=actor,
                    bug_id=bug_id,
                    details=json.dumps({
                        "snapshot_id": snapshot_id,
                        "path": drift["path"],
                        "drift_type": drift["drift_type"],
                        "target_symbol": drift["target_symbol"],
                    }, ensure_ascii=False, sort_keys=True),
                )
            except Exception:
                pass
            conn.commit()
        return 201, {
            "ok": True,
            "project_id": project_id,
            "bug_id": bug_id,
            "drift": filed,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/pending-scope")
def handle_graph_governance_pending_scope_queue(ctx: RequestContext):
    """Queue or update one pending scope-reconcile row."""
    project_id = ctx.get_project_id()
    body = ctx.body
    commit_sha = str(body.get("commit_sha") or body.get("target_commit_sha") or "").strip()
    if not commit_sha:
        from .errors import ValidationError
        raise ValidationError("commit_sha is required")
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.pending-scope")
        with sqlite_write_lock():
            try:
                row = store.queue_pending_scope_reconcile(
                    conn,
                    project_id,
                    commit_sha=commit_sha,
                    parent_commit_sha=str(body.get("parent_commit_sha") or ""),
                    status=str(body.get("status") or store.PENDING_STATUS_QUEUED),
                    snapshot_id=str(body.get("snapshot_id") or ""),
                    evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {
                        "source": "graph_governance_api",
                        "actor": body.get("actor", "api"),
                    },
                )
            except ValueError as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return 201, {"ok": True, "project_id": project_id, "pending_scope_reconcile": row}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/index")
def handle_graph_governance_index_build(ctx: RequestContext):
    """Build and persist governance index artifacts without source mutation."""
    project_id = ctx.get_project_id()
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from .governance_index import build_and_persist_governance_index

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.index")
        result = build_and_persist_governance_index(
            conn,
            project_id,
            root,
            run_id=str(body.get("run_id") or ""),
            commit_sha=str(body.get("commit_sha") or ""),
            include_active_graph=bool(body.get("include_active_graph", True)),
            persist_inventory=bool(body.get("persist_inventory", True)),
        )
        conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "run_id": result.get("run_id"),
            "commit_sha": result.get("commit_sha"),
            "active_snapshot": result.get("active_snapshot") or {},
            "file_inventory_summary": result.get("file_inventory_summary") or {},
            "symbol_count": (result.get("symbol_index") or {}).get("symbol_count", 0),
            "doc_heading_count": (result.get("doc_index") or {}).get("heading_count", 0),
            "coverage_state": result.get("coverage_state") or {},
            "persist_summary": result.get("persist_summary") or {},
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/import-existing")
def handle_graph_governance_import_existing(ctx: RequestContext):
    """Import the latest non-empty legacy/baseline graph as a graph snapshot."""
    project_id = ctx.get_project_id()
    body = ctx.body
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.import-existing")
        with sqlite_write_lock():
            try:
                result = store.import_existing_graph_snapshot(
                    conn,
                    project_id,
                    commit_sha=str(body.get("commit_sha") or ""),
                    snapshot_id=body.get("snapshot_id"),
                    created_by=str(body.get("actor") or "observer"),
                    activate=bool(body.get("activate", False)),
                    expected_old_snapshot_id=body.get("expected_old_snapshot_id"),
                    extra_graph_paths=body.get("extra_graph_paths") or [],
                )
            except store.GraphSnapshotConflictError as exc:
                _raise_graph_api_conflict(exc)
            except (FileNotFoundError, KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return 201, {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/reconcile/full")
def handle_graph_governance_full_reconcile(ctx: RequestContext):
    """Create a state-only full-reconcile candidate snapshot at current HEAD."""
    project_id = ctx.get_project_id()
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from .state_reconcile import run_state_only_full_reconcile
    semantic_use_ai = _semantic_use_ai_from_body(body)
    semantic_ai_call = _semantic_ai_call_from_body(project_id, root, body)

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.reconcile.full")
        try:
            result = run_state_only_full_reconcile(
                conn,
                project_id,
                root,
                run_id=str(body.get("run_id") or ""),
                commit_sha=str(body.get("commit_sha") or ""),
                snapshot_id=body.get("snapshot_id"),
                snapshot_kind=str(body.get("snapshot_kind") or "full"),
                created_by=str(body.get("actor") or "observer"),
                activate=bool(body.get("activate", False)),
                expected_old_snapshot_id=body.get("expected_old_snapshot_id"),
                notes_extra=body.get("notes_extra") if isinstance(body.get("notes_extra"), dict) else None,
                semantic_enrich=bool(body.get("semantic_enrich", True)),
                semantic_use_ai=semantic_use_ai,
                semantic_feedback_items=body.get("semantic_feedback_items") or body.get("feedback_items"),
                semantic_feedback_round=body.get("semantic_feedback_round"),
                semantic_max_excerpt_chars=(
                    int(body["semantic_max_excerpt_chars"])
                    if body.get("semantic_max_excerpt_chars") is not None
                    else None
                ),
                semantic_ai_call=semantic_ai_call,
                semantic_ai_feature_limit=_semantic_ai_feature_limit_from_body(body),
                **_semantic_ai_batch_kwargs_from_body(body),
                **_semantic_state_kwargs_from_body(body),
                semantic_classify_feedback=bool(
                    _semantic_bool_from_body(body, "semantic_classify_feedback", "classify_feedback", default=True)
                ),
                **_semantic_ai_config_kwargs_from_body(body),
                **_semantic_selector_kwargs_from_body(body),
                semantic_config_path=body.get("semantic_config_path"),
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        conn.commit()
        return 201, result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/reconcile/pending-scope")
def handle_graph_governance_pending_scope_materialize(ctx: RequestContext):
    """Create a state-only scope candidate from queued pending scope rows."""
    project_id = ctx.get_project_id()
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from .state_reconcile import run_pending_scope_reconcile_candidate
    semantic_use_ai = _semantic_use_ai_from_body(body)
    semantic_ai_call = _semantic_ai_call_from_body(project_id, root, body)

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.reconcile.pending-scope")
        try:
            result = run_pending_scope_reconcile_candidate(
                conn,
                project_id,
                root,
                target_commit_sha=str(body.get("target_commit_sha") or ""),
                run_id=str(body.get("run_id") or ""),
                snapshot_id=body.get("snapshot_id"),
                created_by=str(body.get("actor") or "observer"),
                semantic_enrich=bool(body.get("semantic_enrich", True)),
                semantic_use_ai=semantic_use_ai,
                semantic_feedback_items=body.get("semantic_feedback_items") or body.get("feedback_items"),
                semantic_feedback_round=body.get("semantic_feedback_round"),
                semantic_max_excerpt_chars=(
                    int(body["semantic_max_excerpt_chars"])
                    if body.get("semantic_max_excerpt_chars") is not None
                    else None
                ),
                semantic_ai_call=semantic_ai_call,
                semantic_ai_feature_limit=_semantic_ai_feature_limit_from_body(body),
                **_semantic_ai_batch_kwargs_from_body(body),
                **_semantic_state_kwargs_from_body(body),
                semantic_classify_feedback=bool(
                    _semantic_bool_from_body(body, "semantic_classify_feedback", "classify_feedback", default=True)
                ),
                **_semantic_ai_config_kwargs_from_body(body),
                **_semantic_selector_kwargs_from_body(body),
                semantic_config_path=body.get("semantic_config_path"),
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        conn.commit()
        return 201, result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/reconcile/backfill-escape")
def handle_graph_governance_backfill_escape(ctx: RequestContext):
    """Activate a HEAD full snapshot and waive stuck pending scope rows."""
    project_id = ctx.get_project_id()
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from .state_reconcile import run_backfill_escape_hatch

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.reconcile.backfill-escape")
        try:
            result = run_backfill_escape_hatch(
                conn,
                project_id,
                root,
                target_commit_sha=str(body.get("target_commit_sha") or ""),
                run_id=str(body.get("run_id") or ""),
                snapshot_id=body.get("snapshot_id"),
                created_by=str(body.get("actor") or "observer"),
                reason=str(body.get("reason") or ""),
                expected_old_snapshot_id=body.get("expected_old_snapshot_id"),
            )
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        conn.commit()
        return 201, result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/finalize")
def handle_graph_governance_snapshot_finalize(ctx: RequestContext):
    """Activate a candidate graph snapshot with compare-and-swap signoff."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.finalize")
        with sqlite_write_lock():
            try:
                result = store.finalize_graph_snapshot(
                    conn,
                    project_id,
                    snapshot_id,
                    target_commit_sha=str(body.get("target_commit_sha") or ""),
                    expected_old_snapshot_id=body.get("expected_old_snapshot_id"),
                    ref_name=str(body.get("ref_name") or "active"),
                    actor=str(body.get("actor") or "observer"),
                    materialize_pending=bool(body.get("materialize_pending", True)),
                    covered_commit_shas=body.get("covered_commit_shas") or None,
                    evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else None,
                )
            except store.GraphSnapshotConflictError as exc:
                _raise_graph_api_conflict(exc)
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/abandon")
def handle_graph_governance_snapshot_abandon(ctx: RequestContext):
    """Abandon a non-active candidate/finalizing graph snapshot."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.abandon")
        with sqlite_write_lock():
            try:
                result = store.abandon_graph_snapshot(
                    conn,
                    project_id,
                    snapshot_id,
                    actor=str(body.get("actor") or "observer"),
                    reason=str(body.get("reason") or ""),
                )
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic-feedback")
def handle_graph_governance_snapshot_semantic_feedback(ctx: RequestContext):
    """Append review feedback for the next snapshot semantic-enrichment round."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_semantic_enrichment as semantic
    from .db import sqlite_write_lock

    feedback_items = body.get("feedback_items", body.get("feedback", []))
    if isinstance(feedback_items, dict):
        feedback_items = [feedback_items]
    if not isinstance(feedback_items, list) or not feedback_items:
        from .errors import ValidationError
        raise ValidationError("feedback_items must be a non-empty object or list")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic-feedback")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        with sqlite_write_lock():
            try:
                result = semantic.append_review_feedback(
                    conn,
                    project_id,
                    snapshot_id,
                    feedback_items,
                    created_by=str(body.get("actor") or "observer"),
                )
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback")
def handle_graph_governance_snapshot_feedback_list(ctx: RequestContext):
    """List classified reconcile feedback items for a graph snapshot."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.list")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        items = reconcile_feedback.list_feedback_items(
            project_id,
            snapshot_id,
            feedback_kind=str(ctx.query.get("feedback_kind") or ctx.query.get("kind") or ""),
            status=str(ctx.query.get("status") or ""),
            node_id=str(ctx.query.get("node_id") or ""),
            limit=_query_int(ctx.query, "limit", 200),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "summary": reconcile_feedback.feedback_summary(project_id, snapshot_id),
            "items": items,
            "count": len(items),
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/queue")
def handle_graph_governance_snapshot_feedback_queue(ctx: RequestContext):
    """Return grouped, dashboard-safe reconcile feedback review lanes."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.queue")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        return {
            "ok": True,
            **reconcile_feedback.build_feedback_review_queue(
                project_id,
                snapshot_id,
                feedback_kind=str(ctx.query.get("feedback_kind") or ctx.query.get("kind") or ""),
                status=str(ctx.query.get("status") or ""),
                node_id=str(ctx.query.get("node_id") or ""),
                source_round=str(ctx.query.get("source_round") or ctx.query.get("feedback_round") or ""),
                lane=str(ctx.query.get("lane") or ""),
                group_by=str(ctx.query.get("group_by") or "target"),
                include_status_observations=_query_bool(ctx.query, "include_status_observations", False),
                include_resolved=_query_bool(ctx.query, "include_resolved", False),
                include_claimed=_query_bool(ctx.query, "include_claimed", True),
                claimable_only=_query_bool(ctx.query, "claimable_only", False),
                worker_id=str(ctx.query.get("worker_id") or ""),
                limit=_query_int(ctx.query, "limit", 100),
            ),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/classify")
def handle_graph_governance_snapshot_feedback_classify(ctx: RequestContext):
    """Classify semantic open issues into graph/project/reviewer feedback lanes."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.classify")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        result = reconcile_feedback.classify_semantic_open_issues(
            project_id,
            snapshot_id,
            source_round=body.get("source_round") or body.get("feedback_round") or "",
            created_by=str(body.get("actor") or body.get("created_by") or "observer"),
            issues=body.get("issues") if isinstance(body.get("issues"), list) else None,
            limit=int(body["limit"]) if body.get("limit") is not None else None,
            node_ids=body.get("node_ids") if isinstance(body.get("node_ids"), list) else None,
        )
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_classified",
                actor=str(body.get("actor") or "observer"),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "created": result.get("created", 0),
                    "updated": result.get("updated", 0),
                    "summary": result.get("summary", {}),
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass
        return result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/retrieval")
def handle_graph_governance_snapshot_feedback_retrieval(ctx: RequestContext):
    """Run bounded read-only graph/grep/excerpt retrieval for a feedback item."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_feedback
    from .errors import ValidationError

    root = _graph_governance_project_root(project_id, body)
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.retrieval")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        feedback_id = str(body.get("feedback_id") or "").strip()
        item = {}
        if feedback_id:
            state = reconcile_feedback.load_feedback_state(project_id, snapshot_id)
            item = dict((state.get("items") or {}).get(feedback_id) or {})
            if not item:
                raise ValidationError(f"feedback item not found: {feedback_id}")
        node_ids = body.get("node_ids") if isinstance(body.get("node_ids"), list) else None
        if not node_ids and item:
            node_ids = item.get("source_node_ids") or item.get("node_ids") or []
        operations = body.get("operations") if isinstance(body.get("operations"), list) else []
        results: list[dict] = []
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            op = str(operation.get("tool") or operation.get("type") or "").strip()
            if op == "graph_query":
                results.append({
                    "tool": op,
                    "result": reconcile_feedback.graph_query_context(
                        project_id,
                        snapshot_id,
                        node_ids=operation.get("node_ids") or node_ids or [],
                        depth=int(operation.get("depth") or 1),
                    ),
                })
            elif op == "grep_in_scope":
                results.append({
                    "tool": op,
                    "result": reconcile_feedback.grep_in_scope(
                        project_id,
                        snapshot_id,
                        project_root=root,
                        pattern=str(operation.get("pattern") or ""),
                        node_ids=operation.get("node_ids") or node_ids or [],
                        paths=operation.get("paths") if isinstance(operation.get("paths"), list) else None,
                        case_sensitive=bool(operation.get("case_sensitive")),
                        regex=bool(operation.get("regex")),
                        max_matches=int(operation.get("max_matches") or 20),
                    ),
                })
            elif op == "read_excerpt":
                results.append({
                    "tool": op,
                    "result": reconcile_feedback.read_project_excerpt(
                        root,
                        str(operation.get("path") or ""),
                        line_start=int(operation.get("line_start") or 1),
                        line_end=(
                            int(operation["line_end"])
                            if operation.get("line_end") is not None
                            else None
                        ),
                    ),
                })
            else:
                raise ValidationError(f"unsupported retrieval tool: {op}")
        if not operations and item:
            results.append({
                "tool": "feedback_retrieval_context",
                "result": reconcile_feedback.build_feedback_retrieval_context(
                    project_id,
                    snapshot_id,
                    item,
                    project_root=root,
                    grep_patterns=(
                        body.get("grep_patterns")
                        if isinstance(body.get("grep_patterns"), list)
                        else None
                    ),
                    max_grep_matches=int(body.get("max_grep_matches") or 12),
                    max_chars=int(body.get("max_context_chars") or 12000),
                ),
            })
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "feedback_id": feedback_id,
            "results": results,
            "count": len(results),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/status-observations")
def handle_graph_governance_snapshot_feedback_status_observations(ctx: RequestContext):
    """Classify deterministic graph/index drift candidates as status observations."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_status_observations

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.status-observations")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = reconcile_status_observations.classify_status_observations(
                conn,
                project_id,
                snapshot_id,
                test_failures=body.get("test_failures") if isinstance(body.get("test_failures"), list) else [],
                actor=str(body.get("actor") or "status-observation-detector"),
                limit=(
                    int(body["limit"])
                    if body.get("limit") is not None
                    else reconcile_status_observations.DEFAULT_LIMIT
                ),
                include_missing_bindings=bool(body.get("include_missing_bindings", True)),
                include_file_state=bool(body.get("include_file_state", True)),
                include_scope_delta=bool(body.get("include_scope_delta", True)),
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_status_observations_classified",
                actor=str(body.get("actor") or "status-observation-detector"),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "classified_count": result.get("detector", {}).get("classified_count", 0),
                }, ensure_ascii=False, sort_keys=True),
            )
        except Exception:
            pass
        conn.commit()
        return result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/review")
def handle_graph_governance_snapshot_feedback_review(ctx: RequestContext):
    """Review one feedback item and route it toward graph correction or backlog."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    feedback_id = str(body.get("feedback_id") or "").strip()
    if not feedback_id:
        from .errors import ValidationError
        raise ValidationError("feedback_id is required")
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.review")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        use_reviewer_ai = bool(
            body.get("reviewer_use_ai")
            or body.get("use_reviewer_ai")
            or body.get("semantic_use_ai")
            or body.get("use_ai")
        )
        ai_call = None
        review_project_root = None
        if use_reviewer_ai and not body.get("decision") and not body.get("reviewer_decision"):
            review_project_root = _graph_governance_project_root(project_id, body)
            ai_call = _semantic_ai_call_from_body(project_id, review_project_root, {**body, "snapshot_id": snapshot_id})
        try:
            result = reconcile_feedback.review_feedback_item(
                project_id,
                snapshot_id,
                feedback_id,
                decision=str(body.get("decision") or body.get("reviewer_decision") or ""),
                rationale=str(body.get("rationale") or body.get("reviewer_rationale") or ""),
                confidence=float(body["confidence"]) if body.get("confidence") is not None else None,
                status_observation_category=str(
                    body.get("status_observation_category")
                    or body.get("observation_category")
                    or body.get("category")
                    or ""
                ),
                actor=str(body.get("actor") or body.get("reviewed_by") or "observer"),
                accept=bool(body.get("accept") or body.get("accepted")),
                ai_call=ai_call,
                project_root=review_project_root,
                max_context_chars=int(body.get("review_context_chars") or 6000),
                enable_read_tools=not bool(body.get("disable_read_tools")),
                grep_patterns=(
                    body.get("grep_patterns")
                    if isinstance(body.get("grep_patterns"), list)
                    else None
                ),
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_reviewed",
                actor=str(body.get("actor") or "observer"),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "feedback_id": feedback_id,
                    "decision": (result.get("items") or [{}])[0].get("reviewer_decision", ""),
                    "status_observation_category": (
                        (result.get("items") or [{}])[0].get("reviewed_status_observation_category", "")
                    ),
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass
        return result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/queue/claim")
def handle_graph_governance_snapshot_feedback_queue_claim(ctx: RequestContext):
    """Claim grouped feedback queue items for one reviewer worker."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_feedback
    from .errors import ValidationError

    worker_id = str(body.get("worker_id") or body.get("reviewer_worker_id") or "").strip()
    if not worker_id:
        raise ValidationError("worker_id is required")
    limit_groups = int(body.get("limit_groups") or body.get("group_limit") or body.get("limit") or 1)
    max_items = int(body.get("max_items") or body.get("item_limit") or 25)
    if limit_groups < 0 or max_items < 0:
        raise ValidationError("limit_groups and max_items must be non-negative")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.queue.claim")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = reconcile_feedback.claim_feedback_review_queue(
                project_id,
                snapshot_id,
                worker_id=worker_id,
                feedback_kind=str(body.get("feedback_kind") or body.get("kind") or ""),
                status=str(body.get("status") or "classified"),
                node_id=str(body.get("node_id") or ""),
                source_round=str(body.get("source_round") or body.get("feedback_round") or ""),
                lane=str(body.get("lane") or ""),
                group_by=str(body.get("group_by") or "feature"),
                include_status_observations=bool(body.get("include_status_observations")),
                include_resolved=bool(body.get("include_resolved")),
                limit_groups=limit_groups,
                max_items=max_items,
                lease_seconds=int(body.get("lease_seconds") or body.get("claim_lease_seconds") or 1800),
                actor=str(body.get("actor") or worker_id),
            )
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_queue_claimed",
                actor=str(body.get("actor") or worker_id),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "worker_id": worker_id,
                    "claim_id": result.get("claim_id", ""),
                    "claimed_count": result.get("claimed_count", 0),
                    "lane": body.get("lane", ""),
                    "group_by": body.get("group_by", "feature"),
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass
        return result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/decision")
def handle_graph_governance_snapshot_feedback_decision(ctx: RequestContext):
    """Apply explicit user/observer decisions to feedback items."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_feedback
    from .errors import ValidationError

    raw_ids = body.get("feedback_ids")
    if raw_ids is None:
        raw_ids = [body.get("feedback_id")]
    if isinstance(raw_ids, str):
        feedback_ids = [raw_ids]
    elif isinstance(raw_ids, list):
        feedback_ids = [str(item or "").strip() for item in raw_ids]
    else:
        raise ValidationError("feedback_ids must be a string or list")
    action = str(body.get("action") or body.get("decision_action") or "").strip()
    if not action:
        raise ValidationError("action is required")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.decision")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = reconcile_feedback.decide_feedback_items(
                project_id,
                snapshot_id,
                feedback_ids,
                action=action,
                actor=str(body.get("actor") or "observer"),
                rationale=str(body.get("rationale") or body.get("reviewer_rationale") or ""),
                decision=str(body.get("decision") or body.get("reviewer_decision") or ""),
                status_observation_category=str(
                    body.get("status_observation_category")
                    or body.get("observation_category")
                    or body.get("category")
                    or ""
                ),
                accept=(
                    bool(body.get("accept") or body.get("accepted"))
                    if body.get("accept") is not None or body.get("accepted") is not None
                    else None
                ),
            )
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_decision",
                actor=str(body.get("actor") or "observer"),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "feedback_ids": feedback_ids,
                    "action": action,
                    "decided_count": result.get("decided_count", 0),
                    "error_count": result.get("error_count", 0),
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass
        return result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/review-queue")
def handle_graph_governance_snapshot_feedback_review_queue(ctx: RequestContext):
    """Review feedback items selected from the grouped feedback queue."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_feedback
    from .errors import ValidationError

    limit_groups = int(body.get("limit_groups") or body.get("group_limit") or body.get("limit") or 10)
    max_items = int(body.get("max_items") or body.get("item_limit") or 25)
    if limit_groups < 0 or max_items < 0:
        raise ValidationError("limit_groups and max_items must be non-negative")
    review_automation_mode = _automation_mode_from_body(
        body,
        "feedback_review_mode",
        "review_automation_mode",
        "automation_mode",
        default="manual",
    )

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.review-queue")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        worker_id = str(body.get("worker_id") or body.get("reviewer_worker_id") or "").strip()
        claim_before_review = bool(body.get("claim_before_review") or worker_id)
        claim_result: dict = {}
        if claim_before_review and not bool(body.get("dry_run")):
            if not worker_id:
                raise ValidationError("worker_id is required when claim_before_review=true")
            claim_result = reconcile_feedback.claim_feedback_review_queue(
                project_id,
                snapshot_id,
                worker_id=worker_id,
                feedback_kind=str(body.get("feedback_kind") or body.get("kind") or ""),
                status=str(body.get("status") or "classified"),
                node_id=str(body.get("node_id") or ""),
                source_round=str(body.get("source_round") or body.get("feedback_round") or ""),
                lane=str(body.get("lane") or ""),
                group_by=str(body.get("group_by") or "feature"),
                include_status_observations=bool(body.get("include_status_observations")),
                include_resolved=bool(body.get("include_resolved")),
                limit_groups=limit_groups,
                max_items=max_items,
                lease_seconds=int(body.get("lease_seconds") or body.get("claim_lease_seconds") or 1800),
                actor=str(body.get("actor") or worker_id),
            )
            queue = {
                "summary": claim_result.get("queue_summary", {}),
                "groups": claim_result.get("selected_groups", []),
            }
            feedback_ids = [str(item) for item in (claim_result.get("feedback_ids") or []) if str(item or "")]
        else:
            queue = reconcile_feedback.build_feedback_review_queue(
                project_id,
                snapshot_id,
                feedback_kind=str(body.get("feedback_kind") or body.get("kind") or ""),
                status=str(body.get("status") or "classified"),
                node_id=str(body.get("node_id") or ""),
                source_round=str(body.get("source_round") or body.get("feedback_round") or ""),
                lane=str(body.get("lane") or ""),
                group_by=str(body.get("group_by") or "feature"),
                include_status_observations=bool(body.get("include_status_observations")),
                include_resolved=bool(body.get("include_resolved")),
                include_claimed=bool(body.get("include_claimed", True)),
                claimable_only=bool(body.get("claimable_only")),
                worker_id=worker_id,
                limit=limit_groups,
            )
            feedback_ids: list[str] = []
            for group in queue.get("groups") or []:
                for feedback_id in group.get("feedback_ids") or []:
                    feedback_id = str(feedback_id or "").strip()
                    if not feedback_id or feedback_id in feedback_ids:
                        continue
                    feedback_ids.append(feedback_id)
                    if max_items and len(feedback_ids) >= max_items:
                        break
                if max_items and len(feedback_ids) >= max_items:
                    break

        if bool(body.get("dry_run")):
            return {
                "ok": True,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "dry_run": True,
                "automation": {"feedback_review_mode": review_automation_mode},
                "queue_summary": queue.get("summary", {}),
                "group_count": len(queue.get("groups") or []),
                "selected_count": len(feedback_ids),
                "feedback_ids": feedback_ids,
            }

        use_reviewer_ai = bool(
            body.get("reviewer_use_ai")
            or body.get("use_reviewer_ai")
            or body.get("semantic_use_ai")
            or body.get("use_ai")
        )
        decision = str(body.get("decision") or body.get("reviewer_decision") or "")
        ai_call = None
        review_project_root = None
        if use_reviewer_ai and not decision:
            review_project_root = _graph_governance_project_root(project_id, body)
            ai_call = _semantic_ai_call_from_body(project_id, review_project_root, {**body, "snapshot_id": snapshot_id})
            if ai_call is None and not bool(body.get("allow_rule_fallback")):
                raise ValidationError("reviewer_use_ai=true but reviewer AI call could not be built")

        reviewed: list[dict] = []
        errors: list[dict] = []
        batch_review = bool(body.get("batch_review") or body.get("batch_ai_review") or body.get("use_batch_reviewer_ai"))
        if use_reviewer_ai and batch_review and not decision:
            batch_result = reconcile_feedback.review_feedback_items_batch(
                project_id,
                snapshot_id,
                feedback_ids,
                ai_call=ai_call,
                project_root=review_project_root,
                max_context_chars=int(body.get("review_context_chars") or 6000),
                enable_read_tools=not bool(body.get("disable_read_tools")),
                grep_patterns=(
                    body.get("grep_patterns")
                    if isinstance(body.get("grep_patterns"), list)
                    else None
                ),
                actor=str(body.get("actor") or body.get("reviewed_by") or "observer"),
                accept=bool(body.get("accept") or body.get("accepted")),
            )
            for item in batch_result.get("items") or []:
                reviewed.append({
                    "feedback_id": item.get("feedback_id", ""),
                    "status": item.get("status", ""),
                    "reviewer_decision": item.get("reviewer_decision", ""),
                    "final_feedback_kind": item.get("final_feedback_kind", ""),
                    "requires_human_signoff": bool(item.get("requires_human_signoff")),
                    "reviewer_confidence": item.get("reviewer_confidence", 0.0),
                    "source_node_ids": item.get("source_node_ids") or [],
                    "target_type": item.get("target_type", ""),
                    "target_id": item.get("target_id", ""),
                })
            errors.extend(batch_result.get("errors") or [])
        else:
            for feedback_id in feedback_ids:
                try:
                    result = reconcile_feedback.review_feedback_item(
                        project_id,
                        snapshot_id,
                        feedback_id,
                        decision=decision,
                        rationale=str(body.get("rationale") or body.get("reviewer_rationale") or ""),
                        confidence=float(body["confidence"]) if body.get("confidence") is not None else None,
                        status_observation_category=str(
                            body.get("status_observation_category")
                            or body.get("observation_category")
                            or body.get("category")
                            or ""
                        ),
                        actor=str(body.get("actor") or body.get("reviewed_by") or "observer"),
                        accept=bool(body.get("accept") or body.get("accepted")),
                        ai_call=ai_call,
                        project_root=review_project_root,
                        max_context_chars=int(body.get("review_context_chars") or 6000),
                        enable_read_tools=not bool(body.get("disable_read_tools")),
                        grep_patterns=(
                            body.get("grep_patterns")
                            if isinstance(body.get("grep_patterns"), list)
                            else None
                        ),
                    )
                    item = (result.get("items") or [{}])[0]
                    reviewed.append({
                        "feedback_id": feedback_id,
                        "status": item.get("status", ""),
                        "reviewer_decision": item.get("reviewer_decision", ""),
                        "final_feedback_kind": item.get("final_feedback_kind", ""),
                        "requires_human_signoff": bool(item.get("requires_human_signoff")),
                        "reviewer_confidence": item.get("reviewer_confidence", 0.0),
                        "source_node_ids": item.get("source_node_ids") or [],
                        "target_type": item.get("target_type", ""),
                        "target_id": item.get("target_id", ""),
                    })
                except Exception as exc:
                    errors.append({"feedback_id": feedback_id, "error": str(exc)})
                    if not bool(body.get("continue_on_error")):
                        break

        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_queue_reviewed",
                actor=str(body.get("actor") or "observer"),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "selected_count": len(feedback_ids),
                    "reviewed_count": len(reviewed),
                    "error_count": len(errors),
                    "lane": body.get("lane", ""),
                    "group_by": body.get("group_by", "feature"),
                    "use_reviewer_ai": use_reviewer_ai,
                    "claim_before_review": claim_before_review,
                    "claim_id": claim_result.get("claim_id", ""),
                    "feedback_review_mode": review_automation_mode,
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass

        return {
            "ok": not errors,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "automation": {"feedback_review_mode": review_automation_mode},
            "claim": claim_result,
            "queue_summary": queue.get("summary", {}),
            "group_count": len(queue.get("groups") or []),
            "selected_count": len(feedback_ids),
            "reviewed_count": len(reviewed),
            "error_count": len(errors),
            "reviewed": reviewed,
            "errors": errors,
            "summary": reconcile_feedback.feedback_summary(project_id, snapshot_id),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/file-backlog")
def handle_graph_governance_snapshot_feedback_file_backlog(ctx: RequestContext):
    """File an accepted project-improvement feedback item into backlog."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    feedback_id = str(body.get("feedback_id") or "").strip()
    if not feedback_id:
        from .errors import ValidationError
        raise ValidationError("feedback_id is required")
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.file-backlog")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            backlog = reconcile_feedback.build_project_improvement_backlog(
                project_id,
                snapshot_id,
                feedback_id,
                bug_id=str(body.get("bug_id") or ""),
                actor=str(body.get("actor") or "observer"),
                allow_status_observation=bool(body.get("allow_status_observation")),
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        bug_id = backlog["bug_id"]
        payload = backlog["payload"]
        now = _utc_now()
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, target_files, test_files,
                acceptance_criteria, chain_task_id, "commit", discovered_at,
                fixed_at, details_md, chain_trigger_json, required_docs,
                provenance_paths, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, '', '', ?, '', ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bug_id) DO UPDATE SET
                 title = excluded.title,
                 status = excluded.status,
                 priority = excluded.priority,
                 target_files = excluded.target_files,
                 test_files = excluded.test_files,
                 acceptance_criteria = excluded.acceptance_criteria,
                 details_md = excluded.details_md,
                 chain_trigger_json = excluded.chain_trigger_json,
                 required_docs = excluded.required_docs,
                 provenance_paths = excluded.provenance_paths,
                 updated_at = excluded.updated_at
            """,
            (
                bug_id,
                payload.get("title", ""),
                payload.get("status", "OPEN"),
                payload.get("priority", "P2"),
                json.dumps(payload.get("target_files", []), ensure_ascii=False),
                json.dumps(payload.get("test_files", []), ensure_ascii=False),
                json.dumps(payload.get("acceptance_criteria", []), ensure_ascii=False),
                now,
                payload.get("details_md", ""),
                json.dumps(payload.get("chain_trigger_json", {}), ensure_ascii=False, sort_keys=True),
                json.dumps(payload.get("required_docs", []), ensure_ascii=False),
                json.dumps(payload.get("provenance_paths", []), ensure_ascii=False),
                now,
                now,
            ),
        )
        conn.commit()
        mark = reconcile_feedback.mark_feedback_backlog_filed(
            project_id,
            snapshot_id,
            feedback_id,
            bug_id=bug_id,
            actor=str(body.get("actor") or "observer"),
        )
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_backlog_filed",
                actor=str(body.get("actor") or "observer"),
                bug_id=bug_id,
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "feedback_id": feedback_id,
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "feedback_id": feedback_id,
            "bug_id": bug_id,
            "payload": payload,
            "feedback": (mark.get("items") or [{}])[0],
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic-enrich")
def handle_graph_governance_snapshot_semantic_enrich(ctx: RequestContext):
    """Build/rebuild semantic companion artifacts for a graph snapshot."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from . import reconcile_semantic_enrichment as semantic
    from . import reconcile_feedback
    semantic_use_ai = _semantic_use_ai_from_body(body)
    semantic_mode = _automation_mode_from_body(
        body,
        "semantic_mode",
        "semantic_automation_mode",
        default=("auto" if semantic_use_ai else "manual"),
    )
    feedback_review_mode = _automation_mode_from_body(
        body,
        "feedback_review_mode",
        "review_automation_mode",
        default="manual",
    )
    if semantic_mode in {"manual", "enqueue_only"}:
        semantic_use_ai = False
    elif semantic_mode == "auto" and semantic_use_ai is None:
        semantic_use_ai = True
    semantic_ai_call = _semantic_ai_call_from_body(project_id, root, {**body, "snapshot_id": snapshot_id})

    feedback_items = body.get("feedback_items")
    if feedback_items is not None and not isinstance(feedback_items, (list, dict)):
        from .errors import ValidationError
        raise ValidationError("feedback_items must be an object or list when provided")
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic-enrich")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = semantic.run_semantic_enrichment(
                conn,
                project_id,
                snapshot_id,
                root,
                feedback_items=feedback_items,
                feedback_round=body.get("feedback_round"),
                use_ai=semantic_use_ai,
                ai_call=semantic_ai_call,
                created_by=str(body.get("actor") or "observer"),
                max_excerpt_chars=(
                    int(body["max_excerpt_chars"])
                    if body.get("max_excerpt_chars") is not None
                    else None
                ),
                ai_feature_limit=_semantic_ai_feature_limit_from_body(body),
                **_semantic_ai_batch_kwargs_from_body(body),
                **_semantic_state_kwargs_from_body(body),
                **_semantic_ai_config_kwargs_from_body(body),
                **_semantic_selector_kwargs_from_body(body),
                semantic_config_path=body.get("semantic_config_path"),
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        result.setdefault("automation", {})
        result["automation"].update({
            "semantic_mode": semantic_mode,
            "feedback_review_mode": feedback_review_mode,
        })
        if feedback_review_mode in {"enqueue_only", "auto"}:
            review_gate = semantic.feedback_review_gate(
                result.get("summary") or {},
                allow_heuristic_feedback_review=bool(body.get("allow_heuristic_feedback_review")),
            )
            if not review_gate.get("allowed"):
                result["feedback_queue"] = {
                    "mode": feedback_review_mode,
                    "blocked": True,
                    "gate": review_gate,
                }
                conn.commit()
                return {"ok": True, **result}
            round_label = f"round-{int(result.get('feedback_round') or 0):03d}"
            classified = reconcile_feedback.classify_semantic_open_issues(
                project_id,
                snapshot_id,
                source_round=round_label,
                created_by=str(body.get("actor") or "observer"),
                limit=(
                    int(body["feedback_classify_limit"])
                    if body.get("feedback_classify_limit") is not None
                    else None
                ),
                base_snapshot_id=str(body.get("semantic_base_snapshot_id") or body.get("base_snapshot_id") or ""),
            )
            result["feedback_queue"] = {
                "mode": feedback_review_mode,
                "source_round": round_label,
                "classification": classified,
            }
        conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/global-review/incremental")
def handle_graph_governance_snapshot_incremental_global_review(ctx: RequestContext):
    """Run post-scope semantic catch-up plus incremental global review."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from . import reconcile_global_review

    semantic_use_ai = _semantic_use_ai_from_body(body)
    semantic_mode = _automation_mode_from_body(
        body,
        "semantic_mode",
        "semantic_automation_mode",
        default=("auto" if semantic_use_ai else "manual"),
    )
    if semantic_mode in {"manual", "enqueue_only"}:
        semantic_use_ai = False
    elif semantic_mode == "auto" and semantic_use_ai is None:
        semantic_use_ai = True
    semantic_ai_call = _semantic_ai_call_from_body(project_id, root, {**body, "snapshot_id": snapshot_id})
    global_review_use_ai = bool(
        _semantic_bool_from_body(
            body,
            "global_review_use_ai",
            "use_global_review_ai",
            default=False,
        )
    )
    global_review_ai_call = (
        _semantic_ai_call_from_body(project_id, root, {**body, "snapshot_id": snapshot_id})
        if global_review_use_ai
        else None
    )
    semantic_batch_kwargs = _semantic_ai_batch_kwargs_from_body(body)
    raw_budget = body.get("query_budget")
    query_budget = raw_budget if isinstance(raw_budget, dict) else None

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.global-review.incremental")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = reconcile_global_review.run_incremental_global_review(
                conn,
                project_id,
                snapshot_id,
                root,
                base_snapshot_id=str(body.get("base_snapshot_id") or body.get("semantic_base_snapshot_id") or ""),
                changed_paths=body.get("changed_paths") or body.get("semantic_changed_paths"),
                changed_node_ids=body.get("changed_node_ids") or body.get("node_ids"),
                run_semantic=bool(
                    _semantic_bool_from_body(body, "run_semantic", "semantic_enrich", default=True)
                ),
                semantic_use_ai=semantic_use_ai,
                semantic_ai_call=semantic_ai_call,
                semantic_ai_feature_limit=_semantic_ai_feature_limit_from_body(body),
                semantic_ai_batch_size=semantic_batch_kwargs["semantic_ai_batch_size"],
                semantic_ai_batch_by=semantic_batch_kwargs["semantic_ai_batch_by"],
                semantic_ai_input_mode=semantic_batch_kwargs["semantic_ai_input_mode"],
                semantic_config_path=body.get("semantic_config_path"),
                classify_feedback=bool(
                    _semantic_bool_from_body(body, "classify_feedback", "semantic_classify_feedback", default=True)
                ),
                global_review_use_ai=global_review_use_ai,
                global_review_ai_call=global_review_ai_call,
                actor=str(body.get("actor") or "observer"),
                run_id=str(body.get("run_id") or ""),
                query_budget=query_budget,
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/global-review/full")
def handle_graph_governance_snapshot_full_global_review(ctx: RequestContext):
    """Build a full semantic health picture for a graph snapshot."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from . import reconcile_global_review

    global_review_use_ai = bool(
        _semantic_bool_from_body(
            body,
            "global_review_use_ai",
            "use_global_review_ai",
            default=False,
        )
    )
    global_review_ai_call = (
        _semantic_ai_call_from_body(project_id, root, {**body, "snapshot_id": snapshot_id})
        if global_review_use_ai
        else None
    )
    raw_budget = body.get("query_budget")
    query_budget = raw_budget if isinstance(raw_budget, dict) else None

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.global-review.full")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = reconcile_global_review.run_full_global_review(
                conn,
                project_id,
                snapshot_id,
                root,
                global_review_use_ai=global_review_use_ai,
                global_review_ai_call=global_review_ai_call,
                actor=str(body.get("actor") or "observer"),
                run_id=str(body.get("run_id") or ""),
                query_budget=query_budget,
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/queue/claim")
def handle_graph_governance_snapshot_semantic_queue_claim(ctx: RequestContext):
    """Claim semantic AI jobs for an executor-backed runner."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_semantic_enrichment as semantic
    from .errors import ValidationError

    worker_id = str(body.get("worker_id") or body.get("semantic_worker_id") or "").strip()
    if not worker_id:
        raise ValidationError("worker_id is required")
    limit = int(body.get("limit") or body.get("job_limit") or 10)
    if limit < 0:
        raise ValidationError("limit must be non-negative")
    raw_statuses = body.get("statuses") or body.get("status") or None
    if isinstance(raw_statuses, str):
        statuses = [raw_statuses]
    elif isinstance(raw_statuses, list):
        statuses = [str(item or "") for item in raw_statuses]
    else:
        statuses = None

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic.queue.claim")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = semantic.claim_semantic_jobs(
                conn,
                project_id,
                snapshot_id,
                worker_id=worker_id,
                statuses=statuses,
                limit=limit,
                lease_seconds=int(body.get("lease_seconds") or body.get("claim_lease_seconds") or 1800),
                actor=str(body.get("actor") or worker_id),
            )
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_semantic_jobs_claimed",
                actor=str(body.get("actor") or worker_id),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "worker_id": worker_id,
                    "claim_id": result.get("claim_id", ""),
                    "claimed_count": result.get("claimed_count", 0),
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass
        return result
    finally:
        conn.close()


@route("GET", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}")
def handle_reconcile_deferred_cluster_get(ctx: RequestContext):
    """Get a single deferred-cluster row by fingerprint."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        q.ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM reconcile_deferred_clusters "
            "WHERE project_id = ? AND cluster_fingerprint = ?",
            (project_id, fp),
        ).fetchone()
    if row is None:
        return 404, {"error": "deferred_cluster_not_found",
                     "cluster_fingerprint": fp}
    return {"cluster": _deferred_cluster_row_to_dict(row)}


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/skip")
def handle_reconcile_deferred_cluster_skip(ctx: RequestContext):
    """Mark a deferred-cluster as skipped with a reason."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    reason = str(body.get("reason") or "observer_skipped")
    changed = q.mark_terminal(project_id, fp, "skipped", reason)
    return {"ok": bool(changed), "cluster_fingerprint": fp, "status": "skipped",
            "reason": reason}


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/file-now")
def handle_reconcile_deferred_cluster_file_now(ctx: RequestContext):
    """Force-file a queued cluster as a backlog/PM task immediately."""
    from . import reconcile_deferred_queue as q
    from . import auto_backlog_bridge

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        q.ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM reconcile_deferred_clusters "
            "WHERE project_id = ? AND cluster_fingerprint = ?",
            (project_id, fp),
        ).fetchone()
    if row is None:
        return 404, {"error": "deferred_cluster_not_found",
                     "cluster_fingerprint": fp}
    rec = _deferred_cluster_row_to_dict(row)
    payload = rec.get("payload") or {}
    run_id = rec.get("run_id") or ""
    if rec.get("status") not in ("queued", "failed_retryable"):
        return 409, {
            "error": "deferred_cluster_not_fileable",
            "cluster_fingerprint": fp,
            "status": rec.get("status"),
        }
    q.mark_filing(project_id, fp)
    try:
        out = auto_backlog_bridge.file_cluster_as_backlog(
            cluster_group=payload,
            cluster_report=payload.get("cluster_report") or {},
            run_id=run_id,
            project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001
        q.requeue_after_failure(project_id, fp, reason=f"file_cluster_exception: {exc}")
        return 500, {"error": "file_cluster_failed", "message": str(exc)}
    if out.get("filed") and out.get("task_id"):
        q.mark_in_chain(project_id, fp, out["task_id"], bug_id=out.get("backlog_id"))
    else:
        q.requeue_after_failure(
            project_id,
            fp,
            reason=str(out.get("reason") or "file_cluster_failed"),
        )
    return {"result": out, "cluster_fingerprint": fp}


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/withdraw")
def handle_reconcile_deferred_cluster_withdraw(ctx: RequestContext):
    """Withdraw a filed cluster: cancels filed root_task_id and marks skipped."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    reason = str(body.get("reason") or "observer_withdraw")
    cancelled_task: str = ""
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        q.ensure_schema(conn)
        row = conn.execute(
            "SELECT root_task_id FROM reconcile_deferred_clusters "
            "WHERE project_id = ? AND cluster_fingerprint = ?",
            (project_id, fp),
        ).fetchone()
        if row is not None:
            cancelled_task = row[0] or ""
            if cancelled_task:
                try:
                    from . import task_registry
                    task_registry.cancel_task(
                        conn,
                        cancelled_task,
                        reason,
                        project_id=project_id,
                    )
                except Exception:
                    pass
    q.mark_terminal(project_id, fp, "skipped", reason)
    return {"ok": True, "cluster_fingerprint": fp,
            "cancelled_root_task_id": cancelled_task, "reason": reason}


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/retry")
def handle_reconcile_deferred_cluster_retry(ctx: RequestContext):
    """Retry a failed_retryable cluster.  When body.force=True resets retry_count to 0."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    force = bool(body.get("force", False))
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        q.ensure_schema(conn)
        if force:
            changed = q.force_retry(
                project_id,
                fp,
                reason=str(body.get("reason") or "force_retry"),
                conn=conn,
            )
        else:
            cur = conn.execute(
                "UPDATE reconcile_deferred_clusters SET status = 'queued', "
                "  next_retry_at = NULL "
                "WHERE project_id = ? AND cluster_fingerprint = ? "
                "  AND status IN ('failed_retryable','expired')",
                (project_id, fp),
            )
            conn.commit()
            changed = (cur.rowcount or 0) > 0
    return {"ok": changed, "cluster_fingerprint": fp,
            "force": force, "status": "queued" if changed else "unchanged"}


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/observer-hold")
def handle_reconcile_deferred_cluster_observer_hold(ctx: RequestContext):
    """Pause a cluster queue row before auto-flow picks it up again."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    reason = str(body.get("reason") or "observer_hold")
    actor = str(body.get("actor") or "observer")
    changed = q.mark_observer_hold(project_id, fp, reason=reason, actor=actor)
    return {
        "ok": bool(changed),
        "cluster_fingerprint": fp,
        "status": "observer_hold" if changed else "unchanged",
        "reason": reason,
    }


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/observer-takeover")
def handle_reconcile_deferred_cluster_observer_takeover(ctx: RequestContext):
    """Transfer a chain-owned cluster row to observer/MF ownership."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    reason = str(body.get("reason") or "observer_takeover")
    actor = str(body.get("actor") or "observer")
    changed = q.mark_observer_takeover(project_id, fp, reason=reason, actor=actor)
    return {
        "ok": bool(changed),
        "cluster_fingerprint": fp,
        "status": "observer_takeover" if changed else "unchanged",
        "reason": reason,
    }


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/observer-release")
def handle_reconcile_deferred_cluster_observer_release(ctx: RequestContext):
    """Release observer ownership back to queue or a terminal audit state."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    next_status = str(body.get("next_status") or "queued")
    reason = str(body.get("reason") or "observer_release")
    actor = str(body.get("actor") or "observer")
    try:
        changed = q.release_observer_takeover(
            project_id,
            fp,
            next_status=next_status,
            reason=reason,
            actor=actor,
        )
    except ValueError as exc:
        return 422, {"error": "invalid_next_status", "message": str(exc)}
    return {
        "ok": bool(changed),
        "cluster_fingerprint": fp,
        "status": next_status if changed else "unchanged",
        "reason": reason,
    }


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/patch-accepted")
def handle_reconcile_deferred_cluster_patch_accepted(ctx: RequestContext):
    """Close an observer/MF repaired cluster as accepted."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    patch_id = str(body.get("patch_id") or "")
    reason = str(body.get("reason") or "observer_patch_accepted")
    changed = q.mark_patch_accepted(project_id, fp, patch_id=patch_id, reason=reason)
    return {
        "ok": bool(changed),
        "cluster_fingerprint": fp,
        "status": "patch_accepted" if changed else "unchanged",
        "patch_id": patch_id,
        "reason": reason,
    }


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/supersede-bad-run")
def handle_reconcile_deferred_cluster_supersede_bad_run(ctx: RequestContext):
    """Quarantine a bad cluster run so finalize does not consume it."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    reason = str(body.get("reason") or "superseded_bad_run")
    changed = q.mark_superseded_bad_run(project_id, fp, reason=reason)
    return {
        "ok": bool(changed),
        "cluster_fingerprint": fp,
        "status": "superseded_bad_run" if changed else "unchanged",
        "reason": reason,
    }


@route("POST", "/api/wf/{project_id}/node-create")
def handle_node_create(ctx: RequestContext):
    """Create a single node. System allocates display_id.

    AI provides: parent_layer (int) + title + deps + primary
    System provides: display_id (L{layer}.{next_index})

    Body: {
        "parent_layer": 22,          // required: which layer
        "title": "ContextStore",     // required
        "node": {                    // optional extras
            "deps": ["L15.1"],
            "primary": ["agent/context_store.py"],
            "description": "..."
        }
    }
    """
    project_id = ctx.get_project_id()
    parent_layer = ctx.body.get("parent_layer")
    title = ctx.body.get("title", "")
    node = ctx.body.get("node", {})

    if not parent_layer and not title:
        # Fallback: try to read from node.id (legacy)
        node_id = node.get("id", "")
        if node_id:
            parent_layer = int(node_id.split(".")[0][1:]) if "." in node_id else None
            title = node.get("title", node_id)

    if parent_layer is None:
        from .errors import ValidationError
        raise ValidationError("parent_layer is required (e.g., 22 for L22.x)")

    if not title:
        from .errors import ValidationError
        raise ValidationError("title is required")

    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        if session.get("role") not in ("coordinator", "pm"):
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(session.get("role", ""), "node-create",
                                        {"detail": "Only coordinator or PM can create nodes"})

        # System allocates display_id: find max index in this layer
        prefix = f"L{parent_layer}."
        existing = conn.execute(
            "SELECT node_id FROM node_state WHERE project_id = ? AND node_id LIKE ?",
            (project_id, f"{prefix}%")
        ).fetchall()

        max_index = 0
        for row in existing:
            try:
                idx = int(row["node_id"].split(".")[1])
                max_index = max(max_index, idx)
            except (ValueError, IndexError):
                pass

        new_index = max_index + 1
        display_id = f"L{parent_layer}.{new_index}"

        # Insert node state
        now = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
        conn.execute(
            """INSERT OR IGNORE INTO node_state
               (project_id, node_id, verify_status, build_status, updated_at, version)
               VALUES (?, ?, 'pending', 'unknown', ?, 1)""",
            (project_id, display_id, now)
        )

        # Record in history (use role field which exists in all schema versions)
        try:
            conn.execute(
                """INSERT INTO node_history (project_id, node_id, from_status, to_status, role, evidence_json, created_at)
                   VALUES (?, ?, 'none', 'pending', ?, ?, ?)""",
                (project_id, display_id, session.get("role", "coordinator"),
                 json.dumps({"title": title, "deps": node.get("deps", []), "primary": node.get("primary", [])}),
                 now)
            )
        except Exception:
            pass  # History is nice-to-have, don't block node creation

        # P0-2 fix: also add node to in-memory graph + persist graph.json
        try:
            from .models import NodeDef
            from .db import _resolve_project_dir
            graph = project_service.load_project_graph(project_id)
            node_def = NodeDef(
                id=display_id,
                title=title,
                layer=f"L{parent_layer}",
                primary=node.get("primary", []),
            )
            deps = node.get("deps", [])
            # Filter deps to only existing graph nodes
            valid_deps = [d for d in deps if graph.has_node(d)]
            graph.add_node(node_def, deps=valid_deps)
            graph.save(_resolve_project_dir(project_id) / "graph.json")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("node-create graph update failed: %s", e)

    return {
        "node_id": display_id,
        "parent_layer": parent_layer,
        "title": title,
        "created": True,
    }


@route("POST", "/api/wf/{project_id}/verify-update")
def handle_verify_update(ctx: RequestContext):
    project_id = ctx.get_project_id()

    # Input validation with helpful messages
    nodes = ctx.body.get("nodes", [])
    status = ctx.body.get("status", "")
    evidence = ctx.body.get("evidence")

    if not nodes:
        from .errors import ValidationError
        raise ValidationError(
            'Missing "nodes" field. Example: {"nodes": ["L1.3"], "status": "testing", '
            '"evidence": {"type": "test_report", "producer": "tester-001"}}'
        )
    if not isinstance(nodes, list):
        from .errors import ValidationError
        raise ValidationError(f'"nodes" must be a list, got {type(nodes).__name__}')
    if not status:
        from .errors import ValidationError
        raise ValidationError(
            'Missing "status" field. Valid values: pending, testing, t2_pass, qa_pass, failed, waived, skipped'
        )
    if evidence is not None and not isinstance(evidence, dict):
        from .errors import ValidationError
        raise ValidationError(
            f'"evidence" must be a dict, got {type(evidence).__name__}. '
            'Example: {"type": "test_report", "producer": "tester-001", "tool": "pytest", '
            '"summary": {"passed": 42, "failed": 0}}'
        )

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
            node_ids=nodes,
            target_status=status,
            session=session,
            evidence_dict=evidence,
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
            min_status=ctx.body.get("min_status", "qa_pass"),
        )
    return result


@route("POST", "/api/wf/{project_id}/artifacts-check")
def handle_artifacts_check(ctx: RequestContext):
    """Check artifacts for nodes before qa_pass."""
    project_id = ctx.get_project_id()
    node_ids = ctx.body.get("nodes", [])
    if not node_ids:
        from .errors import ValidationError
        raise ValidationError('Missing "nodes" field.')

    graph = project_service.load_project_graph(project_id)
    from .artifacts import check_artifacts_for_qa_pass
    return check_artifacts_for_qa_pass(node_ids, graph, project_id)


@route("POST", "/api/wf/{project_id}/coverage-check")
def handle_coverage_check(ctx: RequestContext):
    """Check if changed files are covered by acceptance graph nodes. Records result for gatekeeper."""
    project_id = ctx.get_project_id()
    changed_files = ctx.body.get("files", [])
    if not changed_files:
        from .errors import ValidationError
        raise ValidationError('Missing "files" field. Provide list of changed file paths.')

    graph = project_service.load_project_graph(project_id)
    from .coverage_check import check_feature_coverage
    result = check_feature_coverage(graph, changed_files)

    # Record result for gatekeeper
    try:
        from . import gatekeeper
        with DBContext(project_id) as conn:
            session = None
            try:
                session = ctx.require_auth(conn)
            except Exception:
                pass
            gatekeeper.record_check(
                conn, project_id, "coverage_check",
                passed=result.get("pass", False),
                result=result,
                created_by=session.get("principal_id", "") if session else "",
            )
    except Exception:
        pass  # Non-critical

    return result


@route("GET", "/api/wf/{project_id}/summary")
def handle_summary(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        return state_service.get_summary(conn, project_id)


@route("GET", "/api/wf/{project_id}/preflight-check")
def handle_preflight_check(ctx: RequestContext):
    project_id = ctx.get_project_id()
    auto_fix = ctx.query.get("auto_fix", "false").lower() == "true"
    from .preflight import run_preflight
    with DBContext(project_id) as conn:
        return run_preflight(conn, project_id, auto_fix=auto_fix)


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


@route("POST", "/api/wf/{project_id}/node-update")
def handle_node_update(ctx: RequestContext):
    """Update node attributes (e.g. secondary doc bindings). Coordinator only."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        if session.get("role") != "coordinator":
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(session.get("role", ""), "node-update",
                                        {"detail": "Only coordinator can update node attributes"})
    node_id = ctx.body.get("node_id")
    attrs = ctx.body.get("attrs", {})
    if not node_id or not attrs:
        from .errors import GovernanceError
        raise GovernanceError("missing node_id or attrs", "invalid_request")
    # Only allow safe attributes to be updated
    ALLOWED_ATTRS = {"secondary", "test", "description", "propagation"}
    rejected = set(attrs.keys()) - ALLOWED_ATTRS
    if rejected:
        from .errors import GovernanceError
        raise GovernanceError(f"Cannot update attrs: {rejected}. Allowed: {ALLOWED_ATTRS}", "forbidden_attr")
    graph = project_service.load_project_graph(project_id)
    graph.update_node_attrs(node_id, attrs)
    from .db import _resolve_project_dir
    graph.save(_resolve_project_dir(project_id) / "graph.json")
    return {"node_id": node_id, "updated_attrs": list(attrs.keys())}


@route("POST", "/api/wf/{project_id}/node-batch-update")
def handle_node_batch_update(ctx: RequestContext):
    """Batch update secondary doc bindings for multiple nodes. Coordinator only."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        if session.get("role") != "coordinator":
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(session.get("role", ""), "node-batch-update",
                                        {"detail": "Only coordinator can batch update node attributes"})
    updates = ctx.body.get("updates", [])
    if not updates:
        from .errors import GovernanceError
        raise GovernanceError("missing updates array", "invalid_request")
    graph = project_service.load_project_graph(project_id)
    results = []
    for upd in updates:
        node_id = upd.get("node_id")
        attrs = upd.get("attrs", {})
        try:
            ALLOWED_ATTRS = {"secondary", "test", "description", "propagation"}
            safe_attrs = {k: v for k, v in attrs.items() if k in ALLOWED_ATTRS}
            graph.update_node_attrs(node_id, safe_attrs)
            results.append({"node_id": node_id, "status": "updated"})
        except Exception as e:
            results.append({"node_id": node_id, "status": "error", "error": str(e)})
    from .db import _resolve_project_dir
    graph.save(_resolve_project_dir(project_id) / "graph.json")
    return {"updated": len([r for r in results if r["status"] == "updated"]), "results": results}


@route("POST", "/api/wf/{project_id}/node-delete")
def handle_node_delete(ctx: RequestContext):
    """Delete nodes from graph and node_state. Coordinator only.

    Body: {"nodes": ["L1.1", "L1.2", ...], "reason": "..."}
    """
    project_id = ctx.get_project_id()
    nodes = ctx.body.get("nodes", [])
    reason = ctx.body.get("reason", "")
    if not nodes:
        from .errors import GovernanceError
        raise GovernanceError("missing nodes array", "invalid_request")

    graph = project_service.load_project_graph(project_id)
    deleted = []
    skipped = []
    for nid in nodes:
        try:
            graph.remove_node(nid)
            deleted.append(nid)
        except Exception:
            skipped.append({"node_id": nid, "reason": "not in graph"})

    # Save graph
    from .db import _resolve_project_dir
    graph.save(_resolve_project_dir(project_id) / "graph.json")

    # Remove from node_state DB + audit
    with DBContext(project_id) as conn:
        for nid in deleted:
            conn.execute("DELETE FROM node_state WHERE project_id = ? AND node_id = ?",
                         (project_id, nid))
        audit_service.record(conn, project_id, "node.batch_delete",
                             node_ids=deleted, reason=reason)

    return {"deleted": len(deleted), "skipped": skipped, "reason": reason}


@route("POST", "/api/wf/{project_id}/node-soft-delete")
def handle_node_soft_delete(ctx: RequestContext):
    """Soft-delete nodes by setting verify_status to 'rolled_back'.

    PR-C scaffold: no production callsite yet. Sets status and writes audit record.

    Body: {"node_ids": ["L1.1", "L1.2"], "reason": "rolled back by graph delta"}
    """
    project_id = ctx.get_project_id()
    node_ids = ctx.body.get("node_ids", [])
    reason = ctx.body.get("reason", "")
    if not node_ids:
        from .errors import GovernanceError
        raise GovernanceError("missing node_ids array", "invalid_request")

    now = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
    updated = []
    skipped = []

    with DBContext(project_id) as conn:
        for nid in node_ids:
            row = conn.execute(
                "SELECT verify_status, version FROM node_state WHERE project_id = ? AND node_id = ?",
                (project_id, nid),
            ).fetchone()
            if not row:
                skipped.append({"node_id": nid, "reason": "not found"})
                continue

            old_status = row["verify_status"]
            new_version = row["version"] + 1
            conn.execute(
                """UPDATE node_state SET verify_status = 'rolled_back',
                   updated_by = 'node-soft-delete', updated_at = ?, version = ?
                   WHERE project_id = ? AND node_id = ?""",
                (now, new_version, project_id, nid),
            )

            # Write audit record to node_history
            try:
                conn.execute(
                    """INSERT INTO node_history
                       (project_id, node_id, from_status, to_status, role, evidence_json, session_id, ts, version)
                       VALUES (?, ?, ?, 'rolled_back', 'coordinator', ?, 'node-soft-delete', ?, ?)""",
                    (project_id, nid, old_status,
                     json.dumps({"reason": reason, "type": "soft_delete"}),
                     now, new_version),
                )
            except Exception:
                pass  # History is best-effort

            updated.append(nid)

        # Audit
        audit_service.record(conn, project_id, "node.soft_delete",
                             node_ids=updated, reason=reason)

    return {"updated": updated, "skipped": skipped, "reason": reason}


@route("POST", "/api/wf/{project_id}/node-promote-backfill")
def handle_node_promote_backfill(ctx: RequestContext):
    """Promote a backfilled node from pending → qa_pass.

    Body: {
        "node_id": "L7.6",
        "merge_commit": "abc1234",
        "operator_id": "observer-1",
        "reason": "BF-005 historical backfill"
    }

    Role check: only observer or coordinator allowed.
    Returns 403 if node lacks backfill_ref, 400 if merge_commit invalid, 200 on success.
    """
    project_id = ctx.get_project_id()
    node_id = ctx.body.get("node_id", "")
    merge_commit = ctx.body.get("merge_commit", "")
    operator_id = ctx.body.get("operator_id", "")
    reason = ctx.body.get("reason", "")

    if not node_id or not merge_commit:
        return 400, {"error": "node_id and merge_commit are required"}

    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        role = session.get("role", "")
        if role not in ("observer", "coordinator"):
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(
                role, "node-promote-backfill",
                {"detail": "Only observer or coordinator can promote backfill nodes"},
            )

        try:
            result = state_service.promote_backfill_node(
                conn=conn,
                project_id=project_id,
                node_id=node_id,
                merge_commit=merge_commit,
                operator_id=operator_id or session.get("principal_id", "anonymous"),
                reason=reason,
            )
            conn.commit()
            return 200, result
        except GovernanceError:
            raise


@route("GET", "/api/wf/{project_id}/impact")
def handle_impact(ctx: RequestContext):
    project_id = ctx.get_project_id()
    files_str = ctx.query.get("files", "")
    files = [f.strip() for f in files_str.split(",") if f.strip()] if files_str else []
    # file_policy query param: "primary_only" disables secondary matching
    # Default: match both primary and secondary (doc/test reverse traceability)
    primary_only = ctx.query.get("file_policy", "") == "primary_only"

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
            file_policy=FileHitPolicy(match_primary=True, match_secondary=not primary_only),
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


@route("POST", "/api/mem/{project_id}/ttl-cleanup")
def handle_mem_ttl_cleanup(ctx: RequestContext):
    """Archive active memories whose durability TTL has elapsed (per domain pack)."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        result = memory_service.archive_expired_memories(conn, project_id)
    return result


@route("POST", "/api/mem/{project_id}/flush-index")
def handle_mem_flush_index(ctx: RequestContext):
    """Flush pending dbservice reindex queue (DockerBackend only)."""
    from .memory_backend import get_backend
    backend = get_backend()
    if hasattr(backend, "flush_pending_index"):
        flushed = backend.flush_pending_index()
        remaining = backend.pending_index_count()
    else:
        flushed, remaining = 0, 0
    return {"flushed": flushed, "remaining": remaining}


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


@route("GET", "/api/mem/{project_id}/search")
def handle_mem_search(ctx: RequestContext):
    """Full-text search across memories (FTS5 or semantic depending on backend)."""
    project_id = ctx.get_project_id()
    q = ctx.query.get("q", "")
    top_k = int(ctx.query.get("top_k", "5"))
    if not q:
        return {"error": "MISSING_QUERY", "message": "q parameter required"}, 400
    with DBContext(project_id) as conn:
        results = memory_service.search_memories(conn, project_id, q, top_k)
    return {"results": results, "count": len(results), "query": q}


@route("POST", "/api/mem/{project_id}/relate")
def handle_mem_relate(ctx: RequestContext):
    """Create a relation between two ref_ids."""
    project_id = ctx.get_project_id()
    body = ctx.body or {}
    from_ref = body.get("from_ref_id", "")
    relation = body.get("relation", "")
    to_ref = body.get("to_ref_id", "")
    if not from_ref or not relation or not to_ref:
        return {"error": "MISSING_FIELDS", "message": "from_ref_id, relation, to_ref_id required"}, 400
    from .memory_backend import get_backend
    with DBContext(project_id) as conn:
        result = get_backend().relate(conn, project_id, from_ref, relation, to_ref, body.get("metadata"))
    return 201, result


@route("GET", "/api/mem/{project_id}/expand")
def handle_mem_expand(ctx: RequestContext):
    """Traverse relation graph from a ref_id."""
    project_id = ctx.get_project_id()
    ref_id = ctx.query.get("ref_id", "")
    depth = int(ctx.query.get("depth", "2"))
    if not ref_id:
        return {"error": "MISSING_REF_ID", "message": "ref_id parameter required"}, 400
    from .memory_backend import get_backend
    with DBContext(project_id) as conn:
        results = get_backend().expand(conn, project_id, ref_id, depth)
    return {"results": results, "count": len(results), "ref_id": ref_id, "depth": depth}


@route("POST", "/api/mem/{project_id}/promote")
def handle_mem_promote(ctx: RequestContext):
    """Promote a memory to global scope (creates a cross-project copy)."""
    project_id = ctx.get_project_id()
    body = ctx.body or {}
    memory_id = body.get("memory_id", "")
    target_scope = body.get("target_scope", "global")
    reason = body.get("reason", "")
    if not memory_id:
        return {"error": "MISSING_FIELD", "message": "memory_id required"}, 400
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn) if ctx.token else {}
        result = memory_service.promote_memory(
            conn, project_id, memory_id,
            target_scope=target_scope, reason=reason,
            actor_id=session.get("principal_id", ""),
        )
    return result


@route("POST", "/api/mem/{project_id}/register-pack")
def handle_mem_register_pack(ctx: RequestContext):
    """Register a domain pack (kind definitions) for a project."""
    project_id = ctx.get_project_id()
    body = ctx.body or {}
    domain = body.get("domain", "development")
    types = body.get("types", {})
    if not types:
        return {"error": "MISSING_FIELD", "message": "types dict required"}, 400
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn) if ctx.token else {}
        result = memory_service.register_domain_pack(
            conn, project_id, domain, types,
            actor_id=session.get("principal_id", ""),
        )
    return result


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


# --- Task Registry ---


def _publish_event(event_name, payload):
    """Best-effort event publish to event bus (mirrors auto_chain pattern)."""
    try:
        from . import event_bus
        event_bus._bus.publish(event_name, payload)
    except Exception:
        pass


@route("POST", "/api/task/{project_id}/create")
def handle_task_create(ctx: RequestContext):
    """Create a task. Auth optional — uses principal_id if token provided, else 'anonymous'.

    Phase 4: Auto-enriches metadata with operation_type, intent_hash, and
    runs conflict rules for non-system task types.
    """
    project_id = ctx.get_project_id()
    log.info("API task.create: project=%s type=%s prompt=%r",
             project_id, ctx.body.get("type", "task"), (ctx.body.get("prompt", ""))[:80])
    from . import task_registry
    from .conflict_rules import extract_operation_type, compute_intent_hash, check_conflicts
    created_by = "anonymous"
    if ctx.token:
        try:
            with DBContext(project_id) as conn:
                session = ctx.require_auth(conn)
                created_by = session.get("principal_id", "anonymous")
        except Exception:
            pass

    prompt = ctx.body.get("prompt", "")
    task_type = ctx.body.get("type", "task")
    metadata = ctx.body.get("metadata") or {}
    if isinstance(metadata, str):
        import json as _json
        try:
            metadata = _json.loads(metadata)
        except Exception:
            metadata = {}

    # Auto-enrich metadata
    if "operation_type" not in metadata:
        metadata["operation_type"] = extract_operation_type(prompt)
    if "intent_hash" not in metadata:
        metadata["intent_hash"] = compute_intent_hash(prompt)
    if "intent_summary" not in metadata:
        metadata["intent_summary"] = prompt[:200]

    # §2.1-§2.3: Reconcile task creator allowlist + audit + rate limit (R2/R3/R4)
    _is_reconcile = task_type == "reconcile" or task_type.startswith("reconcile_")
    if _is_reconcile:
        # §2.1: Soft enforcement — warn for non-allowed creators (R2)
        _allowed_prefixes = ("observer-", "coordinator", "auto-chain-reconcile")
        if not any(created_by.startswith(p) for p in _allowed_prefixes):
            log.warning("reconcile_task: creator %r not in allowlist (soft enforce §2.1)", created_by)
        # §2.3: 3-tier rate limiting (R4)
        with DBContext(project_id) as _rl_conn:
            # Tier-1: max 1 active reconcile_run
            _active_runs = _rl_conn.execute(
                "SELECT COUNT(DISTINCT json_extract(metadata_json, '$.reconcile_run_id')) "
                "FROM tasks WHERE project_id=? AND type LIKE 'reconcile%' AND status IN ('pending','claimed','running')",
                (project_id,),
            ).fetchone()[0]
            if _active_runs > 1:
                raise GovernanceError("rate_limit", "Tier-1: max 1 active reconcile_run exceeded", status=429)
            # Tier-2: max 3 concurrent tasks per run
            _run_id = metadata.get("reconcile_run_id", "")
            if _run_id:
                _concurrent = _rl_conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE project_id=? AND type LIKE 'reconcile%' "
                    "AND status IN ('pending','claimed','running') AND json_extract(metadata_json, '$.reconcile_run_id')=?",
                    (project_id, _run_id),
                ).fetchone()[0]
                if _concurrent >= 3:
                    raise GovernanceError("rate_limit", "Tier-2: max 3 concurrent tasks per reconcile_run exceeded", status=429)
                # Tier-3: max 10 actions per task
                _actions = _rl_conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE project_id=? AND type LIKE 'reconcile%' "
                    "AND json_extract(metadata_json, '$.reconcile_run_id')=?",
                    (project_id, _run_id),
                ).fetchone()[0]
                if _actions >= 10:
                    raise GovernanceError("rate_limit", "Tier-3: max 10 actions per reconcile_run exceeded", status=429)

    # --- Backlog gate: check bug_id for code-change task types (R1/R4) ---
    # Z3 observer-hotfix 2026-04-24 (P0-1 + P0-2):
    #   - Default enforce mode changed from 'warn' to 'strict' (P0-1).
    #     Rollback: set env OPT_BACKLOG_ENFORCE=warn to revert.
    #   - Added bug_id existence check in backlog_bugs (P0-2). Reject if bug_id
    #     given but not found in backlog_bugs. Prevents typo'd or fabricated IDs
    #     (observed 2026-04-24: MCP task_create silently dropped metadata and
    #     3 tasks landed with bug_id=missing in `warn` mode).
    #   - auto-chain internal creator is exempt (auto_chain already copies
    #     bug_id from parent's metadata; gate would create chicken-and-egg).
    _CODE_CHANGE_TYPES = ("pm", "dev", "test", "qa", "gatekeeper", "merge", "deploy")
    if task_type in _CODE_CHANGE_TYPES and created_by not in ("auto-chain", "auto-chain-retry"):
        _bug_id = metadata.get("bug_id") or ""
        _force_bypass = metadata.get("force_no_backlog") is True
        _enforce_mode = os.environ.get("OPT_BACKLOG_ENFORCE", "strict")
        if _force_bypass:
            # R3: tighter force_no_backlog requirements — validate before bypass
            _bypass_reason = metadata.get("force_reason", "")
            _mf_id = metadata.get("mf_id", "")
            if not _bypass_reason or len(_bypass_reason) < 30:
                _msg = "force_no_backlog requires force_reason of at least 30 chars"
                log.warning("backlog_gate: %s (mode=%s)", _msg, _enforce_mode)
                if _enforce_mode == "strict":
                    raise GovernanceError("force_reason too short", _msg, status=422)
            if not _mf_id or not re.match(r'^MF-\d{4}-\d{2}-\d{2}-\d{3}$', _mf_id):
                _msg = "force_no_backlog requires mf_id matching MF-YYYY-MM-DD-NNN"
                log.warning("backlog_gate: %s (mode=%s)", _msg, _enforce_mode)
                if _enforce_mode == "strict":
                    raise GovernanceError("mf_id invalid", _msg, status=422)
            # R4: observer bypass — audit the event
            if not _bypass_reason:
                _bypass_reason = "no reason given"
            try:
                _publish_event("backlog_gate.observer_bypass", {
                    "project_id": project_id,
                    "task_type": task_type,
                    "force_reason": _bypass_reason,
                    "created_by": created_by,
                })
                with DBContext(project_id) as _evt_conn:
                    _evt_conn.execute(
                        "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
                        "VALUES (?, ?, ?, ?, datetime('now'))",
                        ("backlog_gate", "backlog_gate",
                         "backlog_gate.observer_bypass",
                         json.dumps({"project_id": project_id, "task_type": task_type,
                                     "force_reason": _bypass_reason, "created_by": created_by})),
                    )
                    _evt_conn.commit()
            except Exception:
                log.debug("backlog_gate: failed to audit observer bypass", exc_info=True)
            log.info("backlog_gate: observer bypass for %s task (reason: %s)", task_type, _bypass_reason)
        elif not _bug_id:
            log.warning("backlog_gate: missing bug_id for %s task in project %s (mode=%s)",
                        task_type, project_id, _enforce_mode)
            if _enforce_mode == "strict":
                raise GovernanceError(
                    "bug_id required",
                    f"Task type '{task_type}' requires metadata.bug_id (OPT_BACKLOG_ENFORCE=strict). "
                    f"Set metadata.force_no_backlog=true with force_reason to bypass.",
                    status=422,
                )
        else:
            # P0-2: bug_id existence check — must correspond to a real backlog row
            try:
                with DBContext(project_id) as _chk_conn:
                    _row = _chk_conn.execute(
                        "SELECT status, bypass_policy_json FROM backlog_bugs WHERE bug_id = ?",
                        (_bug_id,),
                    ).fetchone()
            except Exception:
                _row = None
            if _row is None:
                log.warning("backlog_gate: bug_id %r not found in backlog_bugs for %s task (mode=%s)",
                            _bug_id, task_type, _enforce_mode)
                if _enforce_mode == "strict":
                    raise GovernanceError(
                        "bug_id not in backlog",
                        f"metadata.bug_id '{_bug_id}' does not exist in backlog_bugs. "
                        f"Create the backlog row first via POST /api/backlog/{project_id}/{_bug_id}, "
                        f"or set force_no_backlog=true with force_reason to bypass.",
                        status=422,
                    )
            elif _row["status"] not in ("OPEN", "IN_PROGRESS", "MF_IN_PROGRESS"):
                # R2: bug_id status must be active; MF_IN_PROGRESS is allowed
                # because manual fixes are now audited through backlog runtime.
                _bug_status = _row["status"]
                _msg = (f"bug_id {_bug_id} is not active (current status={_bug_status}); "
                        f"cannot attach new work to closed bug")
                log.warning("backlog_gate: %s (mode=%s)", _msg, _enforce_mode)
                if _enforce_mode == "strict":
                    raise GovernanceError("bug_id not open", _msg, status=422)
            else:
                _policy = backlog_runtime.parse_json_object(_row["bypass_policy_json"])
                if _policy:
                    metadata = backlog_runtime.merge_policy_into_metadata(metadata, _policy)

        # R1: parent_task_id requirement for non-pm code-change types
        _PARENT_REQUIRED_TYPES = ("dev", "test", "qa", "gatekeeper", "merge", "deploy")
        if task_type in _PARENT_REQUIRED_TYPES and not _force_bypass:
            _parent_task_id = metadata.get("parent_task_id") or ""
            if not _parent_task_id:
                _msg = "parent_task_id required for code-change type from non-auto-chain creator"
                log.warning("backlog_gate: %s (type=%s, mode=%s)", _msg, task_type, _enforce_mode)
                if _enforce_mode == "strict":
                    raise GovernanceError("parent_task_id missing", _msg, status=422)
            else:
                # Verify parent_task_id exists in tasks table
                try:
                    with DBContext(project_id) as _ptid_conn:
                        _ptid_row = _ptid_conn.execute(
                            "SELECT task_id FROM tasks WHERE task_id = ?",
                            (_parent_task_id,),
                        ).fetchone()
                except Exception:
                    _ptid_row = None
                if _ptid_row is None:
                    _msg = "parent_task_id not found in tasks table"
                    log.warning("backlog_gate: %s (parent=%r, mode=%s)",
                                _msg, _parent_task_id, _enforce_mode)
                    if _enforce_mode == "strict":
                        raise GovernanceError("parent_task_id invalid", _msg, status=422)

    # Run conflict rules for user-facing task types (not auto-chain internal)
    rule_decision = None
    if task_type in ("pm", "dev", "coordinator") and created_by not in ("auto-chain", "auto-chain-retry"):
        with DBContext(project_id) as conn:
            rule_decision = check_conflicts(
                conn, project_id,
                target_files=metadata.get("target_files", []),
                operation_type=metadata["operation_type"],
                intent_hash=metadata["intent_hash"],
                prompt=prompt,
                depends_on=metadata.get("depends_on"),
            )
        metadata["rule_decision"] = rule_decision["decision"]
        metadata["rule_reason"] = rule_decision["reason"]
        log.info("API conflict_rules: project=%s decision=%s reason=%s",
                 project_id, rule_decision["decision"], rule_decision["reason"])

    # CR0b R2: scoped-task blocker — when an active reconcile session exists,
    # block new scoped (reconcile_*) task dispatch BEFORE inserting. Existing
    # in-flight tasks are NOT cancelled; only NEW dispatch is blocked.
    if task_type.startswith("reconcile_"):
        try:
            with DBContext(project_id) as _sess_conn:
                _active_sess = reconcile_session.get_active_session(_sess_conn, project_id)
        except Exception:
            log.debug("reconcile session lookup failed (non-critical)", exc_info=True)
            _active_sess = None
        if _active_sess is not None:
            return 409, {
                "error": "reconcile_session_active_blocks_scoped",
                "session_id": _active_sess.session_id,
                "task_type": task_type,
            }

    with DBContext(project_id) as conn:
        result = task_registry.create_task(
            conn, project_id,
            prompt=prompt,
            task_type=task_type,
            related_nodes=ctx.body.get("related_nodes"),
            created_by=created_by,
            priority=int(ctx.body.get("priority", 0)),
            max_attempts=int(ctx.body.get("max_attempts", 3)),
            metadata=metadata,
        )
        _created_bug_id = metadata.get("bug_id", "")
        if _created_bug_id:
            backlog_runtime.update_backlog_runtime(
                conn,
                _created_bug_id,
                f"{task_type}_queued",
                project_id=project_id,
                task_id=result.get("task_id", ""),
                task_type=task_type,
                metadata=metadata,
                runtime_state=result.get("status", "queued"),
            )
    # §2.2: Audit reconcile task creation (R3)
    if _is_reconcile:
        try:
            with DBContext(project_id) as _ac:
                audit_service.record(_ac, project_id, event="reconcile_task.created",
                                     actor=created_by, ok=True, node_ids=None, request_id="",
                                     task_id=result.get("task_id", ""), task_type=task_type)
        except Exception:
            log.debug("reconcile_task.created audit failed (non-critical)", exc_info=True)
    # Best-effort publish task.created event to event bus
    try:
        _publish_event("task.created", {
            "task_id": result.get("task_id"),
            "project_id": project_id,
            "type": task_type,
            "created_by": created_by,
        })
    except Exception:
        pass
    # Attach rule decision to response
    if rule_decision:
        result["rule_decision"] = rule_decision
    return result


@route("POST", "/api/task/{project_id}/claim")
def handle_task_claim(ctx: RequestContext):
    """Claim a task. Auth optional — uses principal_id if token provided, else body worker_id."""
    project_id = ctx.get_project_id()
    log.info("API task.claim: project=%s worker=%s", project_id, ctx.body.get("worker_id", "anonymous"))
    from . import task_registry
    worker_id = ctx.body.get("worker_id", "anonymous")
    if ctx.token:
        try:
            with DBContext(project_id) as conn:
                session = ctx.require_auth(conn)
                worker_id = session.get("principal_id", worker_id)
        except Exception:
            pass
    caller_pid = int(ctx.body.get("caller_pid", 0) or 0)
    with DBContext(project_id) as conn:
        claimed = task_registry.claim_task(conn, project_id, worker_id, caller_pid=caller_pid)
        if isinstance(claimed, tuple):
            task, fence_token = claimed
        else:
            task, fence_token = claimed, ""
        if task is None:
            return {"task": None, "message": "No tasks available"}
        metadata = task.get("metadata", {}) if isinstance(task, dict) else {}
        bug_id = metadata.get("bug_id", "")
        if bug_id:
            backlog_runtime.update_backlog_runtime(
                conn,
                bug_id,
                f"{task.get('type', 'task')}_claimed",
                project_id=project_id,
                task_id=task.get("task_id", ""),
                task_type=task.get("type", "task"),
                metadata=metadata,
                runtime_state="claimed",
            )
        return {"task": task, "fence_token": fence_token}


@route("POST", "/api/task/{project_id}/complete")
def handle_task_complete(ctx: RequestContext):
    """Complete a task. No auth required."""
    project_id = ctx.get_project_id()
    log.info("API task.complete: project=%s task=%s status=%s result_keys=%s",
             project_id, ctx.body.get("task_id", "?"), ctx.body.get("status", "?"),
             list((ctx.body.get("result") or {}).keys()))
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.complete_task(
            conn, ctx.body.get("task_id", ""),
            status=ctx.body.get("status", "succeeded"),
            result=ctx.body.get("result"),
            error_message=ctx.body.get("error_message", ""),
            project_id=project_id,
            completed_by=ctx.body.get("worker_id", ""),
            override_reason=ctx.body.get("override_reason", ""),
        )


@route("POST", "/api/task/{project_id}/hold")
def handle_task_hold(ctx: RequestContext):
    """Put a queued task into observer_hold — stops executor and auto-chain from touching it."""
    project_id = ctx.get_project_id()
    from . import task_registry
    task_id = ctx.body.get("task_id", "")
    if not task_id:
        return {"error": "missing task_id"}, 400
    with DBContext(project_id) as conn:
        return task_registry.hold_task(conn, task_id)


@route("POST", "/api/task/{project_id}/cancel")
def handle_task_cancel(ctx: RequestContext):
    """Cancel a task. No auto-chain, no retry. Terminal state."""
    project_id = ctx.get_project_id()
    log.info("API task.cancel: project=%s task=%s", project_id, ctx.body.get("task_id", "?"))
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.cancel_task(
            conn,
            ctx.body.get("task_id", ""),
            ctx.body.get("reason", ""),
            project_id=project_id,
        )


@route("POST", "/api/task/{project_id}/release")
def handle_task_release(ctx: RequestContext):
    """Release an observer_hold task back to queued flow."""
    project_id = ctx.get_project_id()
    from . import task_registry
    task_id = ctx.body.get("task_id", "")
    if not task_id:
        return {"error": "missing task_id"}, 400
    with DBContext(project_id) as conn:
        return task_registry.release_task(conn, task_id)


@route("GET", "/api/project/{project_id}/observer-mode")
def handle_observer_mode_get(ctx: RequestContext):
    """Get current observer_mode flag for a project."""
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        enabled = task_registry.get_observer_mode(conn, project_id)
    return {"project_id": project_id, "observer_mode": enabled}


@route("POST", "/api/project/{project_id}/observer-mode")
def handle_observer_mode_set(ctx: RequestContext):
    """Enable or disable observer_mode. When on, all new tasks start as observer_hold."""
    project_id = ctx.get_project_id()
    from . import task_registry
    enabled = ctx.body.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("true", "1", "on")
    with DBContext(project_id) as conn:
        return task_registry.set_observer_mode(conn, project_id, bool(enabled))


@route("GET", "/api/task/{project_id}/list")
def handle_task_list(ctx: RequestContext):
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        tasks = task_registry.list_tasks(
            conn, project_id,
            status=ctx.query.get("status"),
            limit=int(ctx.query.get("limit", "50")),
        )
    return {"tasks": tasks, "count": len(tasks)}


@route("GET", "/api/task/{project_id}/subtask-group/{group_id}")
def handle_subtask_group(ctx: RequestContext):
    """Return subtask group status and member tasks (R8)."""
    project_id = ctx.get_project_id()
    group_id = ctx.path_params.get("group_id", "")
    if not group_id:
        raise GovernanceError("group_id is required", 400)
    with DBContext(project_id) as conn:
        group_row = conn.execute(
            "SELECT * FROM subtask_groups WHERE group_id = ? AND project_id = ?",
            (group_id, project_id),
        ).fetchone()
        if not group_row:
            raise GovernanceError(f"Subtask group not found: {group_id}", 404)
        tasks = conn.execute(
            """SELECT task_id, status, execution_status, type, subtask_local_id,
                      subtask_depends_on, created_at, updated_at, completed_at
               FROM tasks WHERE subtask_group_id = ?
               ORDER BY created_at ASC""",
            (group_id,),
        ).fetchall()
    return {
        "group_id": group_row["group_id"],
        "project_id": group_row["project_id"],
        "pm_task_id": group_row["pm_task_id"],
        "status": group_row["status"],
        "total_count": group_row["total_count"],
        "completed_count": group_row["completed_count"],
        "created_at": group_row["created_at"],
        "completed_at": group_row["completed_at"],
        "tasks": [dict(t) for t in tasks],
    }


@route("GET", "/api/task/{project_id}/trace/{trace_id}")
def handle_task_trace(ctx: RequestContext):
    """List all tasks sharing a trace_id, ordered by creation time."""
    project_id = ctx.get_project_id()
    trace_id = ctx.path_params.get("trace_id", "")
    if not trace_id:
        raise GovernanceError("trace_id is required", 400)
    with DBContext(project_id) as conn:
        rows = conn.execute(
            """SELECT task_id, status, type, prompt, assigned_to, created_by,
                      created_at, updated_at, trace_id, chain_id,
                      result_json, metadata_json
               FROM tasks
               WHERE project_id = ? AND trace_id = ?
               ORDER BY created_at ASC""",
            (project_id, trace_id),
        ).fetchall()
    tasks = [dict(r) for r in rows]
    return {"tasks": tasks, "count": len(tasks), "trace_id": trace_id}


@route("GET", "/api/task/{project_id}/{task_id}/gates")
def handle_task_gates(ctx: RequestContext):
    """List all gate events for a specific task."""
    project_id = ctx.get_project_id()
    task_id = ctx.path_params.get("task_id", "")
    if not task_id:
        raise GovernanceError("task_id is required", 400)
    with DBContext(project_id) as conn:
        rows = conn.execute(
            """SELECT id, gate_name, passed, reason, trace_id, created_at
               FROM gate_events
               WHERE project_id = ? AND task_id = ?
               ORDER BY created_at ASC""",
            (project_id, task_id),
        ).fetchall()
    events = [dict(r) for r in rows]
    return {"task_id": task_id, "gate_events": events, "count": len(events)}


@route("GET", "/api/runtime/{project_id}")
def handle_runtime(ctx: RequestContext):
    """Runtime projection — read-only view from Task Registry. No state of its own."""
    project_id = ctx.get_project_id()
    from . import task_registry, session_context
    with DBContext(project_id) as conn:
        active = task_registry.list_tasks(conn, project_id, status="running")
        queued = task_registry.list_tasks(conn, project_id, status="queued")
        claimed = task_registry.list_tasks(conn, project_id, status="claimed")
        pending_notify = task_registry.list_pending_notifications(conn, project_id)

    context = session_context.load_snapshot(project_id)

    return {
        "project_id": project_id,
        "active_tasks": active,
        "queued_tasks": queued,
        "claimed_tasks": claimed,
        "pending_notifications": pending_notify,
        "context": context,
        "summary": {
            "active": len(active),
            "queued": len(queued),
            "claimed": len(claimed),
            "pending_notify": len(pending_notify),
        },
    }


@route("POST", "/api/task/{project_id}/progress")
def handle_task_progress(ctx: RequestContext):
    """Update task progress heartbeat."""
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.update_progress(
            conn, ctx.body.get("task_id", ""),
            phase=ctx.body.get("phase", "running"),
            percent=int(ctx.body.get("percent", 0)),
            message=ctx.body.get("message", ""),
        )


@route("POST", "/api/task/{project_id}/notify")
def handle_task_notify(ctx: RequestContext):
    """Mark task notification as sent."""
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.mark_notified(conn, ctx.body.get("task_id", ""))


@route("POST", "/api/task/{project_id}/recover")
def handle_task_recover(ctx: RequestContext):
    """Recover stale tasks with expired leases."""
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.recover_stale_tasks(conn, project_id)


# --- Health ---

@route("GET", "/api/health")
def handle_health(ctx: RequestContext):
    return {"status": "ok", "service": "governance", "port": PORT,
            "version": get_server_version(), "pid": SERVER_PID}


@route("GET", "/api/version-check/{project_id}")
def handle_version_check(ctx: RequestContext):
    """Check chain version vs git HEAD.

    Phase A hybrid: reads DB state (synced by executor) AND derives trailer
    state from git. Returns 'source' field indicating where version came from.
    """
    pid = ctx.get_project_id()
    conn = get_connection(pid)

    # Derive trailer state (best-effort, non-blocking)
    trailer_state = None
    try:
        from .chain_trailer import get_chain_state
        trailer_state = get_chain_state()
    except Exception as e:
        log.debug("version-check: chain_trailer unavailable: %s", e)

    row = conn.execute(
        "SELECT chain_version, updated_at, git_head, dirty_files, git_synced_at "
        "FROM project_version WHERE project_id=?", (pid,)
    ).fetchone()

    # Runtime version baking — detect stale-process-after-deploy
    gov_runtime = ""
    sm_runtime = ""
    runtime_match = False
    try:
        from .chain_trailer import get_runtime_version
        gov_runtime = get_runtime_version()
    except Exception as e:
        log.debug("version-check: gov runtime_version unavailable: %s", e)
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:40101/api/manager/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            sm_data = json.loads(resp.read().decode())
            sm_runtime = sm_data.get("runtime_version", "")
    except Exception as e:
        log.debug("version-check: sm runtime_version unavailable: %s", e)

    if not row:
        source = trailer_state["source"] if trailer_state else "none"
        version = trailer_state["version"] if trailer_state else "unknown"
        runtime_match = bool(gov_runtime and (gov_runtime.startswith(version) or version.startswith(gov_runtime))
                             and sm_runtime and (sm_runtime.startswith(version) or version.startswith(sm_runtime)))
        return {
            "ok": True, "project_id": pid,
            "head": version if trailer_state else "unknown",
            "chain_version": version if trailer_state else "(not set)",
            "dirty": trailer_state["dirty"] if trailer_state else False,
            "dirty_files": trailer_state["dirty_files"] if trailer_state else [],
            "source": source,
            "message": "Project not initialized" + (f" (trailer source: {source})" if trailer_state else ""),
            "generated_at": _utc_now(), "project_version": version if trailer_state else "unknown",
            "gov_runtime_version": gov_runtime,
            "sm_runtime_version": sm_runtime,
            "runtime_match": runtime_match,
        }

    # R1/R2: Trailer-priority chain_ver. When trailer source='trailer', the git
    # log--first-parent trailer is authoritative (auto_chain._gate_version_check
    # uses chain_state.chain_sha for the same reason). Fall back to DB row
    # otherwise (preserves prior behavior, including post-deploy DB sync state).
    if trailer_state and trailer_state.get("source") == "trailer":
        chain_ver = trailer_state.get("version", "") or trailer_state.get("chain_sha", "")
    else:
        chain_ver = row["chain_version"]
    git_head = row["git_head"] or ""
    dirty_files_raw = json.loads(row["dirty_files"] or "[]")
    # B31: apply _DIRTY_IGNORE filter (same as auto_chain._gate_version_check)
    dirty_files = [f for f in dirty_files_raw if not any(f.startswith(p) for p in _DIRTY_IGNORE)]
    git_synced = row["git_synced_at"] or ""

    # Determine source: prefer trailer if available
    source = "db"
    if trailer_state:
        source = trailer_state["source"]  # 'trailer' or 'head'
        # Merge trailer dirty_files with DB dirty_files (union, filtered)
        if trailer_state.get("dirty_files"):
            trailer_dirty = [f for f in trailer_state["dirty_files"]
                             if not any(f.startswith(p) for p in _DIRTY_IGNORE)]
            for f in trailer_dirty:
                if f not in dirty_files:
                    dirty_files.append(f)

    # Compare
    ok = True
    parts = []

    if not git_head:
        parts.append("Executor has not synced git status yet")
    elif not (git_head.startswith(chain_ver) or chain_ver.startswith(git_head)):
        ok = False
        parts.append(f"HEAD ({git_head}) != CHAIN_VERSION ({chain_ver})")
    if dirty_files:
        ok = False
        parts.append(f"{len(dirty_files)} uncommitted files")

    runtime_match = bool(gov_runtime and (gov_runtime.startswith(chain_ver) or chain_ver.startswith(gov_runtime))
                         and sm_runtime and (sm_runtime.startswith(chain_ver) or chain_ver.startswith(sm_runtime)))
    return {
        "ok": ok,
        "project_id": pid,
        "head": git_head or (trailer_state["version"] if trailer_state else "unknown"),
        "chain_version": chain_ver,
        "chain_updated_at": row["updated_at"],
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files,
        "git_synced_at": git_synced,
        "source": source,
        "message": "; ".join(parts),
        "generated_at": _utc_now(),
        "project_version": chain_ver,
        "gov_runtime_version": gov_runtime,
        "sm_runtime_version": sm_runtime,
        "runtime_match": runtime_match,
    }


@route("POST", "/api/version-sync/{project_id}")
def handle_version_sync(ctx: RequestContext):
    """Executor syncs git status from host machine. Lightweight, no auth."""
    pid = ctx.get_project_id()
    body = ctx.body or {}

    git_head = body.get("git_head", "")
    dirty_files = body.get("dirty_files", [])
    if not git_head:
        return {"error": "missing git_head"}, 400

    now = _utc_now()

    def _do_sync():
        conn = independent_connection(pid)
        try:
            conn.execute("""
                UPDATE project_version
                SET git_head = ?, dirty_files = ?, git_synced_at = ?
                WHERE project_id = ?
            """, (git_head, json.dumps(dirty_files), now, pid))
            conn.commit()
        finally:
            conn.close()

    _retry_on_busy(_do_sync)
    return {"ok": True, "git_head": git_head, "dirty_files": dirty_files, "synced_at": now}


@route("POST", "/api/version-update/{project_id}")
def handle_version_update(ctx: RequestContext):
    """DEPRECATED (Phase A §4.4): All writes are ignored. Returns git-derived chain_version.

    R7: This endpoint no longer writes to project_version. It logs a deprecation
    warning, audits the ignored call, and returns the current chain state derived
    from git trailers. The endpoint is preserved (R10) but neutered.
    """
    pid = ctx.get_project_id()
    body = ctx.body or {}
    now = _utc_now()

    log.warning("deprecated_write_ignored: handle_version_update called for %s by %s — "
                "writes are no longer accepted (Phase A §4.4, R7)",
                pid, body.get("updated_by", "unknown"))

    # Audit the ignored call
    conn = independent_connection(pid)
    try:
        audit_service.record(
            conn, pid, "version.update_attempt",
            actor=body.get("updated_by", "unknown"),
            details={
                "task_id": body.get("task_id", ""),
                "new_version": body.get("chain_version", ""),
                "updated_by": body.get("updated_by", ""),
                "result": "deprecated_write_ignored",
                "reason": "Phase A: version-update writes are ignored; git trailers are source of truth",
            },
        )
        conn.commit()
    except Exception as e:
        log.debug("version-update audit failed (non-fatal): %s", e)
    finally:
        conn.close()

    # Return git-derived chain state
    derived_state = None
    try:
        from .chain_trailer import get_chain_state
        derived_state = get_chain_state()
    except Exception as e:
        log.debug("version-update: chain_trailer unavailable: %s", e)

    chain_version = derived_state["chain_sha"] if derived_state else "unknown"
    result = {
        "ok": True,
        "chain_version": chain_version,
        "updated_at": now,
        "deprecated_write_ignored": True,
        "source": "git_trailer",
    }
    if derived_state:
        result["derived_state"] = derived_state
    return result


def _audit_version_update(conn, pid, body, result, reason):
    """Write audit record for every version-update attempt."""
    try:
        audit_service.record(
            conn, pid, "version.update_attempt",
            actor=body.get("updated_by", "unknown"),
            details={
                "task_id": body.get("task_id", ""),
                "old_version": body.get("old_version", ""),
                "new_version": body.get("chain_version", ""),
                "chain_stage": body.get("chain_stage", ""),
                "updated_by": body.get("updated_by", ""),
                "manual_fix_reason": body.get("manual_fix_reason", ""),
                "result": result,
                "reject_reason": reason,
            },
        )
    except Exception:
        pass  # audit failure should not block


# --- Redeploy-after-merge endpoint ---

@route("POST", "/api/governance/redeploy-after-merge/{project_id}")
def handle_redeploy_after_merge(ctx: RequestContext):
    """Audit-only ack; executor (deploy_chain) orchestrates SM calls."""
    pid = ctx.get_project_id()
    body = ctx.body
    task_id = body.get("task_id", "")
    chain_version = body.get("chain_version", "")
    with DBContext(pid) as conn:
        try:
            audit_service.record(conn, pid, "redeploy_after_merge.requested",
                                 actor="deploy_chain", details={"task_id": task_id, "chain_version": chain_version})
        except Exception:
            pass
    return {"ok": True, "message": "audit recorded; executor must orchestrate sm calls"}


# --- Redeploy Endpoints (PR-2) ---

@route("POST", "/api/governance/redeploy/executor")
def handle_redeploy_executor(ctx: RequestContext):
    """Redeploy executor via 5-step pipeline. See redeploy_handler.py."""
    from .redeploy_handler import handle_redeploy_executor as _handler
    return _handler(ctx)


@route("POST", "/api/governance/redeploy/gateway")
def handle_redeploy_gateway(ctx: RequestContext):
    """Redeploy gateway via 5-step pipeline. See redeploy_handler.py."""
    from .redeploy_handler import handle_redeploy_gateway as _handler
    return _handler(ctx)


@route("POST", "/api/governance/redeploy/coordinator")
def handle_redeploy_coordinator(ctx: RequestContext):
    """Redeploy coordinator via 5-step pipeline. See redeploy_handler.py."""
    from .redeploy_handler import handle_redeploy_coordinator as _handler
    return _handler(ctx)


@route("POST", "/api/governance/redeploy/service_manager")
def handle_redeploy_service_manager(ctx: RequestContext):
    """Redeploy service_manager via 5-step pipeline. See redeploy_handler.py."""
    from .redeploy_handler import handle_redeploy_service_manager as _handler
    return _handler(ctx)


@route("GET", "/api/metrics")
def handle_metrics(ctx: RequestContext):
    """Return in-memory metrics snapshot."""
    from .observability import get_metrics
    return get_metrics()


@route("GET", "/api/health/deep")
def handle_deep_health(ctx: RequestContext):
    """Deep health check: Redis, SQLite, outbox, queues."""
    from .observability import check_outbox_health
    checks = {"governance": "ok", "port": PORT}

    # Redis
    rc = get_redis()
    checks["redis"] = "ok" if rc.available else "degraded"

    # Outbox alerts
    alerts = []
    for p in project_service.list_projects():
        alerts.extend(check_outbox_health(p["project_id"]))
    checks["alerts"] = alerts
    checks["alert_count"] = len(alerts)

    return checks


@route("GET", "/api/context-snapshot/{project_id}")
def handle_context_snapshot(ctx: RequestContext):
    """Return minimal base context for AI session startup (~500 tokens).

    Single API call providing point-in-time consistent snapshot.
    AI can query on-demand APIs for more details.
    """
    pid = ctx.get_project_id()
    conn = get_connection(pid)
    role_raw = ctx.query.get("role", "coordinator")
    task_id_raw = ctx.query.get("task_id", "")
    role = role_raw[0] if isinstance(role_raw, list) else role_raw
    task_id = task_id_raw[0] if isinstance(task_id_raw, list) else task_id_raw
    now = _utc_now()

    # Task summary — recent 3 tasks
    task_summary = []
    try:
        for row in conn.execute(
            "SELECT task_id, type, status FROM tasks ORDER BY created_at DESC LIMIT 3"
        ).fetchall():
            task_summary.append({
                "task_id": row["task_id"],
                "type": row["type"],
                "status": row["status"],
            })
    except Exception:
        pass

    # Project state
    ver_row = conn.execute(
        "SELECT chain_version, updated_at, dirty_files FROM project_version WHERE project_id=?",
        (pid,)
    ).fetchone()
    dirty_files = json.loads(ver_row["dirty_files"] or "[]") if ver_row and ver_row["dirty_files"] else []
    project_state = {
        "chain_version": ver_row["chain_version"] if ver_row else "unknown",
        "dirty": bool(dirty_files),
    }

    # Node summary (one-line)
    node_counts = {}
    for row in conn.execute(
        "SELECT verify_status, COUNT(*) as cnt FROM node_state WHERE project_id=? GROUP BY verify_status",
        (pid,)
    ).fetchall():
        node_counts[row["verify_status"]] = row["cnt"]

    # Session context snapshot from DB/Redis
    session_snapshot = None
    try:
        from . import session_context
        session_snapshot = session_context.load_snapshot(pid)
    except Exception:
        pass

    # Recent memories (top 3 by relevance)
    recent_memories = []
    try:
        all_mems = memory_service.query_all(pid, active_only=True)
        task_prompt = ""
        if session_snapshot:
            task_prompt = (
                session_snapshot.get("current_focus", "")
                or session_snapshot.get("last_decision", "")
            )
        scored = []
        for m in all_mems:
            score = 0
            s = m.get("structured", {}) or {}
            if s.get("followup_needed"):
                score += 10
            if m.get("kind") == "failure_pattern":
                score += 5
            if m.get("kind") == "decision":
                score += 2
            if m.get("module", "") and m["module"] in task_prompt:
                score += 3
            scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        for _, m in scored[:3]:
            recent_memories.append({
                "module": m.get("module", ""),
                "kind": m.get("kind", ""),
                "content": (m.get("content", ""))[:200],
            })
    except Exception:
        pass

    # Task chain context (if task_id provided)
    task_chain = None
    if task_id:
        try:
            from .chain_context import get_store
            task_chain = get_store().get_chain(task_id, role=role)
        except Exception:
            pass

    result = {
        "snapshot_at": now,
        "project_id": pid,
        "role": role,
        "task_summary": task_summary,
        "project_state": project_state,
        "node_summary": node_counts,
        "recent_memories": recent_memories,
        "constraints": "All changes through auto-chain",
        "generated_at": now,
        "project_version": project_state["chain_version"],
    }
    if session_snapshot:
        result["session_context"] = {
            "current_focus": session_snapshot.get("current_focus", ""),
            "last_decision": session_snapshot.get("last_decision", ""),
            "version": session_snapshot.get("version", 0),
            "updated_at": session_snapshot.get("updated_at", ""),
        }
    if task_chain:
        result["task_chain"] = task_chain
    return result


# --- Documentation ---

_DOCS = {
    "overview": {
        "title": "Governance Service Overview",
        "description": "Workflow governance service for multi-agent coordination. Manages project initialization, role assignment, node verification, release gating, memory, and audit.",
        "base_url": "http://localhost:40000",
        "api_prefix": "/api",
        "gateway_prefix": "/gateway",
        "auth": "No authentication required. All APIs work without tokens. Optional X-Gov-Token header is accepted for backward compatibility but not enforced.",
    },
    "quickstart": {
        "title": "Coordinator Session Quickstart",
        "base_url": "http://localhost:40000",
        "prerequisites": "Human has already run init_project.py and has the coordinator refresh_token (gov-xxx).",
        "steps": [
            {
                "step": 1,
                "phase": "AUTH",
                "action": "Exchange refresh_token for access_token (4h TTL)",
                "method": "POST /api/token/refresh",
                "body": {"refresh_token": "gov-xxx (from init_project.py)"},
                "returns": "access_token (gat-xxx), expires_in_sec, session_id, project_id, role",
                "note": "Use access_token for all subsequent API calls. Auto-renew before expiry.",
            },
            {
                "step": 2,
                "phase": "LIFECYCLE",
                "action": "Register agent and get a lease",
                "method": "POST /api/agent/register",
                "headers": {"X-Gov-Token": "gat-xxx (access_token)"},
                "body": {"project_id": "amingClaw", "expected_duration_sec": 3600},
                "returns": "lease_id, heartbeat_interval_sec (120s)",
                "note": "Heartbeat every 2 min to renew lease. Lease expires in 5 min without heartbeat.",
            },
            {
                "step": 3,
                "phase": "CONTEXT",
                "action": "Load previous session context (if any)",
                "method": "GET /api/context/{project_id}/load",
                "headers": {"X-Gov-Token": "gat-xxx"},
                "returns": "{context: {...}, exists: true/false}",
                "note": "Contains current_focus, active_nodes, pending_tasks, recent_messages from last session.",
            },
            {
                "step": 4,
                "phase": "CONTEXT",
                "action": "Assemble task-aware context from memory",
                "method": "POST /api/context/{project_id}/assemble",
                "headers": {"X-Gov-Token": "gat-xxx"},
                "body": {"task_type": "dev_general", "token_budget": 5000},
                "returns": "Prioritized memories (pitfalls, decisions, architecture) within token budget",
                "note": "Task types: dev_general, telegram_handler, verify_node, code_review, release_check",
            },
            {
                "step": 5,
                "phase": "TELEGRAM",
                "action": "Bind to Telegram chat for message relay",
                "method": "POST /gateway/bind",
                "body": {"token": "gat-xxx", "chat_id": 7848961760, "project_id": "amingClaw"},
                "note": "After binding, user messages in Telegram are pushed to Redis Stream chat:inbox:{hash}.",
            },
            {
                "step": 6,
                "phase": "TELEGRAM",
                "action": "Consume messages from Redis Stream",
                "code": "from telegram_gateway.chat_proxy import ChatProxy\nproxy = ChatProxy(token='gat-xxx', gateway_url='http://localhost:40000', redis_url='redis://localhost:40079/0')\nproxy.start(on_message=handler)  # background thread",
                "note": "ChatProxy uses XREADGROUP+ACK. Unacked messages survive crashes.",
            },
            {
                "step": 7,
                "phase": "WORK",
                "action": "Check project status",
                "method": "GET /api/wf/{project_id}/summary",
                "headers": {"X-Gov-Token": "gat-xxx"},
                "returns": "{total_nodes, by_status: {pending, testing, t2_pass, qa_pass, ...}}",
            },
            {
                "step": 8,
                "phase": "WORK",
                "action": "Verify nodes (tester role)",
                "method": "POST /api/wf/{project_id}/verify-update",
                "headers": {"X-Gov-Token": "tester-token"},
                "body": {
                    "nodes": ["L1.3"],
                    "status": "t2_pass",
                    "evidence": {"type": "test_report", "producer": "tester-001", "tool": "pytest", "summary": {"passed": 42, "failed": 0}},
                },
                "note": "Flow: pending->testing->t2_pass (tester), t2_pass->qa_pass (qa). Evidence required.",
            },
            {
                "step": 9,
                "phase": "WORK",
                "action": "Reply to Telegram user",
                "method": "POST /gateway/reply",
                "body": {"token": "gat-xxx", "chat_id": 7848961760, "text": "Task completed"},
                "note": "Or use proxy.reply('text') from ChatProxy.",
            },
            {
                "step": 10,
                "phase": "SAVE",
                "action": "Save session context before exit",
                "method": "POST /api/context/{project_id}/save",
                "headers": {"X-Gov-Token": "gat-xxx"},
                "body": {"context": {"current_focus": "...", "active_nodes": ["..."], "pending_tasks": ["..."], "recent_messages": []}},
                "note": "Use expected_version for optimistic locking. Context persists to Redis (24h TTL) + SQLite.",
            },
            {
                "step": 11,
                "phase": "EXIT",
                "action": "Deregister agent",
                "method": "POST /api/agent/deregister",
                "body": {"lease_id": "lease-xxx"},
                "note": "Releases lease. Gateway detects offline, queues messages for next session.",
            },
        ],
        "lifecycle_summary": "AUTH(token) -> LIFECYCLE(register) -> CONTEXT(load+assemble) -> TELEGRAM(bind+consume) -> WORK(verify+reply) -> SAVE(context) -> EXIT(deregister)",
    },
    "endpoints": {
        "title": "API Endpoints",
        "groups": {
            "init": {
                "POST /api/init": "Create project + get coordinator token. Repeat with password to reset token.",
            },
            "project": {
                "GET /api/project/list": "List all projects with node counts.",
            },
            "role": {
                "POST /api/role/assign": "Coordinator assigns role+token to agent. Body: {project_id, principal_id, role}",
                "POST /api/role/revoke": "Revoke agent session. Body: {project_id, session_id}",
                "POST /api/role/heartbeat": "Agent keepalive. Body: {project_id?, status?}",
                "GET /api/role/verify": "Verify token, returns session info. Used by Gateway.",
                "GET /api/role/{project_id}/sessions": "List active sessions for a project.",
            },
            "workflow": {
                "POST /api/wf/{project_id}/import-graph": "Import acceptance graph from markdown.",
                "POST /api/wf/{project_id}/verify-update": "Update node verification status. Body: {nodes, status, evidence}",
                "POST /api/wf/{project_id}/baseline": "Batch set historical state (coordinator only). Body: {nodes: {id: status}, reason}",
                "POST /api/wf/{project_id}/release-gate": "Check if all nodes pass for release.",
                "POST /api/wf/{project_id}/rollback": "Rollback node state to a version.",
                "GET /api/wf/{project_id}/summary": "Status summary (counts by status).",
                "GET /api/wf/{project_id}/node/{node_id}": "Single node details.",
                "GET /api/wf/{project_id}/export": "Export graph as JSON or Mermaid. Query: format=json|mermaid",
                "GET /api/wf/{project_id}/impact": "File change impact analysis. Query: files=a.py,b.py",
            },
            "memory": {
                "POST /api/mem/{project_id}/write": "Write memory entry. Body: {module, kind, content, related_nodes?, supersedes?}",
                "GET /api/mem/{project_id}/query": "Query memory. Query: module=, kind=, node=",
            },
            "audit": {
                "GET /api/audit/{project_id}/log": "Query audit log. Query: limit=, event=, since=",
                "GET /api/audit/{project_id}/violations": "Query violations. Query: limit=, since=",
            },
        },
    },
    "workflow_rules": {
        "title": "Workflow Verification Rules",
        "status_flow": {
            "states": ["pending", "testing", "t2_pass", "qa_pass", "failed", "waived", "skipped"],
            "transitions": {
                "pending": ["testing"],
                "testing": ["t2_pass", "failed"],
                "t2_pass": ["qa_pass", "failed"],
                "qa_pass": "(terminal - verified)",
                "failed": ["testing"],
            },
        },
        "role_permissions": {
            "coordinator": "Can do everything: baseline, assign roles, rollback, import graph, verify-update.",
            "tester": "Can transition: pending->testing, testing->t2_pass/failed.",
            "qa": "Can transition: t2_pass->qa_pass/failed.",
            "dev": "Can transition: pending->testing, testing->t2_pass/failed (same as tester).",
            "observer": "Read-only. Can query status, summary, export.",
        },
        "evidence_format": {
            "description": "Evidence must be a dict, not a string.",
            "required_fields": ["type", "producer"],
            "optional_fields": ["tool", "summary", "artifact_uri", "checksum", "created_at"],
            "example": {
                "type": "test_report",
                "producer": "tester-001",
                "tool": "pytest",
                "summary": {"passed": 42, "failed": 0},
            },
        },
        "verify_update_example": {
            "method": "POST /api/wf/{project_id}/verify-update",
            "headers": {"X-Gov-Token": "agent-token"},
            "body": {
                "nodes": ["L1.3"],
                "status": "t2_pass",
                "evidence": {
                    "type": "test_report",
                    "producer": "tester-001",
                    "tool": "pytest",
                    "summary": {"passed": 10, "failed": 0},
                },
            },
        },
        "gate_rules": "Nodes with dependencies (gates) cannot advance until upstream nodes satisfy their gate policy. Use GET /api/wf/{project_id}/node/{node_id} to check gate status.",
        "release_gate": "POST /api/wf/{project_id}/release-gate checks if all nodes in scope are qa_pass. Returns {release: true/false, blocking_nodes: [...]}.",
    },
    "memory_guide": {
        "title": "Memory Service Guide",
        "description": "Store and query development knowledge (patterns, pitfalls, decisions, workarounds) per project.",
        "kinds": ["decision", "pitfall", "workaround", "invariant", "ownership", "pattern"],
        "write_example": {
            "method": "POST /api/mem/{project_id}/write",
            "headers": {"X-Gov-Token": "token"},
            "body": {
                "module": "auth",
                "kind": "pitfall",
                "content": "Never store session tokens in localStorage - use httpOnly cookies.",
                "related_nodes": ["L2.3"],
                "applies_when": "Implementing any auth-related feature",
            },
        },
        "query_examples": [
            "GET /api/mem/{project_id}/query?module=auth",
            "GET /api/mem/{project_id}/query?kind=pitfall",
            "GET /api/mem/{project_id}/query?node=L2.3",
        ],
    },
    "telegram_integration": {
        "title": "Telegram Gateway Integration (v5.1)",
        "description": "Gateway handles only message sending/receiving. Non-command messages start a Coordinator CLI session. Coordinator handles conversation, decisions, and task orchestration.",
        "architecture": "Telegram <-> Gateway (Docker) -> Claude CLI session (Coordinator) -> Governance API",
        "v5_1_change": "Gateway no longer classifies query/task/chat and no longer creates tasks directly. All decision-making belongs to Coordinator.",
        "role_boundary": {
            "gateway": "Message sending/receiving + /command handling. No decision-making, no task creation.",
            "coordinator": "Conversation + decision-making + task orchestration. Does not write code itself.",
            "dev_executor": "Code execution. Does not interact with users.",
        },
        "gateway_api": {
            "POST /gateway/bind": "Bind coordinator token to chat_id. Body: {token, chat_id, project_id}",
            "POST /gateway/reply": "Send message to Telegram. Body: {token, chat_id?, text}. If no chat_id, uses bound chat.",
            "POST /gateway/unbind": "Unbind chat_id. Body: {chat_id}",
            "GET /gateway/health": "Gateway health check.",
            "GET /gateway/status": "List all active routes (bound coordinators).",
        },
        "message_flow": {
            "user_to_coordinator": "User sends text -> Gateway launches Claude CLI session (Coordinator) with context -> Coordinator processes -> reply via Gateway",
            "coordinator_to_user": "Coordinator stdout -> Gateway sends to Telegram",
            "task_creation": "Only Coordinator can create tasks (POST /api/task/create). Gateway cannot.",
            "governance_events": "Governance publishes events to Redis gov:events:{project_id} -> Gateway formats and sends to admin chat",
        },
        "telegram_commands": {
            "/menu": "Interactive menu showing registered coordinators with switch buttons",
            "/bind <token>": "Bind coordinator to current chat",
            "/unbind": "Unbind current coordinator",
            "/status [project]": "Show project verification status",
            "/projects": "List all projects",
            "/health": "Service health check",
        },
    },
    "coverage_check": {
        "title": "Feature Coverage Check (workflow assurance)",
        "description": "Detect untracked code changes before release. Reverse impact analysis: checks if all changed files have corresponding acceptance graph nodes.",
        "problem_solved": "Prevents features from being shipped without workflow tracking. Catches cases where developers implement code without first creating acceptance nodes.",
        "api": {
            "POST /api/wf/{project_id}/coverage-check": {
                "description": "Check if changed files are covered by acceptance graph nodes.",
                "headers": {"X-Gov-Token": "required"},
                "body": {
                    "files": ["agent/governance/outbox.py", "agent/new_feature.py"],
                },
                "returns": {
                    "covered": [{"file": "agent/governance/outbox.py", "nodes": ["L5.2", "L7.2"]}],
                    "uncovered": [{"file": "agent/new_feature.py", "suggestion": "Create a new node..."}],
                    "coverage_pct": 50.0,
                    "pass": False,
                },
            },
        },
        "integration_with_release_gate": {
            "description": "Run coverage-check before release-gate. If pass=false, block release until all files have nodes.",
            "recommended_flow": [
                "1. git diff --name-only main..HEAD → get changed files",
                "2. POST /api/wf/{pid}/coverage-check {files: [...]}",
                "3. If pass=false → create missing nodes, verify them",
                "4. POST /api/wf/{pid}/release-gate {profile: 'full'}",
            ],
        },
        "gate_types": {
            "L9.1 Feature Coverage Check": "Checks file→node mapping. Uncovered files → warning/block.",
            "L9.2 Node-Before-Code Gate": "verify-update checks if evidence.changed_files are all covered by some node's primary/secondary. Enforces 'create node before writing code'.",
            "L9.3 Artifacts Check": "qa_pass time checks if companion deliverables (api_docs, tests) are complete.",
            "L9.5 Gatekeeper Coverage": "release-gate auto-checks latest coverage-check result. No run / stale / failed → block release.",
        },
    },
    "gatekeeper": {
        "title": "Gatekeeper (pre-release validation)",
        "description": "Gatekeeper is a program (not an AI role) embedded in the governance service. It enforces pre-release checks at two levels: verify-update time and release-gate time.",
        "check_points": {
            "verify-update (pre-check intercept)": {
                "when": "Any node transitions to t2_pass or qa_pass",
                "what": "Checks that the node's declared primary files are all covered by graph nodes",
                "blocks": "If primary files are uncovered → rejects verify-update with error message",
                "module": "state_service._check_node_coverage → coverage_check.check_feature_coverage",
            },
            "release-gate (release intercept)": {
                "when": "POST /api/wf/{pid}/release-gate is called",
                "what": "Checks that a coverage-check was run recently (within 1 hour) and passed",
                "blocks": "If never run → 'Run coverage-check first'. If stale → 'Re-run'. If failed → 'Uncovered files'.",
                "module": "gatekeeper.verify_pre_release → reads gatekeeper_checks table",
            },
        },
        "api": {
            "POST /api/wf/{project_id}/coverage-check": {
                "description": "Run coverage check AND auto-record result for gatekeeper.",
                "body": {"files": ["agent/governance/server.py"]},
                "side_effect": "Result written to gatekeeper_checks table for release-gate to read.",
            },
            "POST /api/wf/{project_id}/artifacts-check": {
                "description": "Check if nodes have required companion artifacts (docs, tests).",
                "body": {"nodes": ["L9.3"]},
            },
            "POST /api/wf/{project_id}/release-gate": {
                "description": "Release gate now includes gatekeeper check automatically.",
                "gatekeeper_field": "Response includes 'gatekeeper': {pass, checks, missing, stale}",
            },
        },
        "flow": [
            "1. Developer changes code",
            "2. POST /api/wf/{pid}/coverage-check {files: [changed files]}",
            "3a. pass:true → gatekeeper records pass → can proceed to release",
            "3b. pass:false → create missing nodes → re-run coverage-check",
            "4. POST /api/wf/{pid}/release-gate → gatekeeper auto-checks latest coverage result",
            "5. All pass → release approved",
        ],
        "storage": "gatekeeper_checks table in project SQLite DB. Each coverage-check auto-records.",
        "config": {
            "max_age_sec": "3600 (1 hour). Stale results require re-running coverage-check.",
            "required_checks": ["coverage_check"],
            "future_checks": ["security_scan", "dependency_audit", "performance_regression"],
        },
        "artifacts_auto_infer": {
            "title": "L9.6 Artifacts Auto-Inference",
            "description": "Nodes without explicit artifacts: declaration are auto-analyzed. If primary files contain @route → api_docs required. If test files declared → test_file required.",
            "rules": [
                "primary .py file has @route() → auto-require api_docs (section inferred from title)",
                "node declares test:[] with files → auto-require test_file existence",
                "declared artifacts take precedence over inferred",
            ],
            "module": "artifacts.infer_required_artifacts",
        },
        "deploy_coverage_check": {
            "title": "L9.7 Deploy Pre-flight Coverage-Check",
            "description": "deploy-governance.sh automatically runs coverage-check before building. Uncovered files block deployment.",
            "usage": "GOV_COORDINATOR_TOKEN=gov-xxx ./deploy-governance.sh",
            "bypass": "SKIP_COVERAGE_CHECK=1 ./deploy-governance.sh (not recommended)",
            "limitation": "Only protects deploy-governance.sh path. docker compose up --build bypasses this check.",
            "mitigation": "verify_loop.sh should be run after any deployment to catch violations.",
        },
        "verify_loop": {
            "title": "Post-Verification Self-Check Script",
            "description": "scripts/verify_loop.sh runs 7 checks after any verification. Catches process violations that individual checks miss.",
            "usage": "bash scripts/verify_loop.sh <token> <project_id>",
            "checks": [
                "1. Node status — all qa_pass?",
                "2. Coverage — all changed files have graph nodes?",
                "3. Docs/Artifacts — nodes with @route have api_docs?",
                "4. Memory — code changes have corresponding dbservice entries? (L9.8)",
                "5. Docs update — API nodes have documentation sections?",
                "6. Gatekeeper — release-gate passes?",
            ],
            "memory_check_rule": "If >5 code files changed but <5 memories → FAIL. If >10 changed but <10 memories → WARN. Forces developers to document decisions and pitfalls.",
        },
        "scheduled_task_management": {
            "title": "L9.9 Scheduled Task Management",
            "description": "Task prompt templates reside in scripts/task-templates/, tracked by git and protected by coverage-check.",
            "template_location": "scripts/task-templates/telegram-handler.md",
            "variables": "{PROJECT_ID}, {TOKEN}, {CHAT_ID}, {STREAM}, {GROUP}, {BASE}",
            "key_fix": "Messages must be consumed with XREADGROUP + XACK confirmation; XRANGE cannot be used (does not track consumption progress).",
        },
        "human_intervention": {
            "title": "Human Intervention Flow",
            "guide": "docs/human-intervention-guide.md",
            "boundaries": {
                "fully_automated": ["Code testing", "verify-update", "coverage-check", "Memory writes", "Message replies (non-sensitive)"],
                "needs_human_confirm": ["New node creation", "Baseline batch changes", "Cross-project operations"],
                "must_be_human": ["Token management", "Release confirmation", "rollback", "delete", "Scheduled task authorization"],
                "human_verification": ["Telegram interaction behavior", "UI changes", "Security features"],
            },
            "trigger_keywords": ["urgent", "urgent", "manual", "manual", "rollback", "delete", "release", "deploy"],
            "verification_flow": "AI notifies human → human tests → replies 'acceptance pass/fail' → AI submits verify-update",
        },
    },
    "token_model": {
        "title": "Token Model (v5 simplified)",
        "description": "Token simplified for message-driven mode: project_token never expires, Gateway proxies auth. Removed refresh/access dual-token design.",
        "tokens": {
            "project_token (gov-xxx)": {
                "holder": "Gateway / Human",
                "ttl": "non-expiring",
                "scope": "Full project API access (coordinator level)",
                "obtain": "POST /api/init {project_id, password}",
            },
            "agent_token (gov-xxx)": {
                "holder": "dev/tester/qa processes",
                "ttl": "24h",
                "scope": "Restricted API (verify-update, heartbeat, and other role operations)",
                "obtain": "POST /api/role/assign (coordinator assigns)",
            },
        },
        "api": {
            "POST /api/init": "Create project and obtain project_token",
            "POST /api/token/revoke": "Manually revoke project_token (requires password)",
            "POST /api/role/assign": "coordinator assigns agent_token",
        },
        "deprecated": [
            "POST /api/token/refresh — No longer needed; project_token never expires [deprecated: v5, removal: v8]",
            "POST /api/token/rotate — Simplified to revoke + re-init [deprecated: v5, removal: v8]",
            "access_token (gat-*) — no longer in use",
        ],
        "security": [
            "init password protection (reset token requires password)",
            "revoke capability retained (manually revocable)",
            "Network isolation (token only within localhost / Docker internal network)",
            "Gateway proxies auth (CLI session does not hold token directly)",
            "agent_token still has 24h TTL (independent process permissions are time-limited)",
        ],
    },
    "agent_lifecycle": {
        "title": "Agent Lifecycle (lease management)",
        "description": "Register/heartbeat/deregister agents with lease-based lifecycle. Orphan detection for stale agents.",
        "api": {
            "POST /api/agent/register": {
                "description": "Register an agent, get a lease.",
                "headers": {"X-Gov-Token": "required"},
                "body": {"project_id": "amingClaw", "expected_duration_sec": 3600},
                "returns": {"lease_id": "lease-xxx", "heartbeat_interval_sec": 120, "lease_ttl_sec": 600},
            },
            "POST /api/agent/heartbeat": {
                "description": "Renew lease. Call every 2 minutes.",
                "body": {"lease_id": "lease-xxx", "status": "idle|busy|processing", "worker_pid": 12345},
                "returns": {"ok": True, "lease_renewed_until": "..."},
            },
            "POST /api/agent/deregister": {
                "description": "Release lease on exit.",
                "body": {"lease_id": "lease-xxx"},
            },
            "GET /api/agent/orphans": {
                "description": "List agents with expired leases.",
                "query": "project_id=amingClaw (optional)",
                "returns": {"orphans": [{"session_id": "...", "principal_id": "...", "worker_pid": 12345, "reason": "no_active_lease"}]},
            },
            "POST /api/agent/cleanup": {
                "description": "Coordinator cleans up orphaned agents.",
                "headers": {"X-Gov-Token": "coordinator token"},
                "body": {"project_id": "amingClaw"},
            },
        },
        "lease_mechanism": "Agent registers → gets lease (5min TTL in Redis). Heartbeat every 2min renews. No heartbeat for 5min → lease expires → agent marked orphan. Gateway checks lease before routing messages.",
    },
    "session_context": {
        "title": "Session Context (cross-session state)",
        "description": "Persist coordinator working state across sessions. Snapshot + append log with optimistic locking.",
        "api": {
            "POST /api/context/{project_id}/save": {
                "description": "Save session context snapshot.",
                "body": {
                    "context": {"current_focus": "...", "active_nodes": ["L1.3"], "pending_tasks": ["..."], "chat_id": 123, "recent_messages": []},
                    "expected_version": 5,
                },
                "returns": {"ok": True, "version": 6},
                "note": "expected_version enables optimistic locking. Omit for unconditional save.",
            },
            "GET /api/context/{project_id}/load": {
                "description": "Load latest session context.",
                "returns": {"context": {"...": "..."}, "exists": True},
            },
            "POST /api/context/{project_id}/log": {
                "description": "Append entry to session log.",
                "body": {"type": "decision|msg_in|msg_out|action", "content": {"text": "..."}},
            },
            "GET /api/context/{project_id}/log": {
                "description": "Read session log entries.",
                "query": "limit=50",
            },
            "POST /api/context/{project_id}/assemble": {
                "description": "Assemble task-aware context from dbservice memory.",
                "body": {"task_type": "dev_general|telegram_handler|verify_node|code_review|release_check", "token_budget": 5000},
            },
            "POST /api/context/{project_id}/archive": {
                "description": "Archive valuable content to long-term memory, clear expired context.",
            },
        },
        "storage": "Redis (24h TTL) + SQLite (durable fallback). Auto-archived by OutboxWorker after 24h inactivity.",
    },
    "task_registry": {
        "title": "Task Registry (task management)",
        "description": "SQLite-backed task lifecycle with dual-field status: execution_status (queued/claimed/running/succeeded/failed/cancelled/timed_out) + notification_status (none/pending/notified).",
        "api": {
            "POST /api/task/{project_id}/create": {
                "description": "Create a new task. DB is source of truth, task file is secondary.",
                "headers": {"X-Gov-Token": "required"},
                "body": {"prompt": "...", "type": "task", "related_nodes": ["L1.3"], "priority": 1, "max_attempts": 3},
                "returns": {"task_id": "task-xxx", "status": "created"},
            },
            "POST /api/task/{project_id}/claim": {
                "description": "Claim next available task (FIFO by priority). Sets worker_id and lease_expires_at.",
                "body": {"task_id": "task-xxx", "worker_id": "executor-hostname"},
                "returns": {"task": {"task_id": "...", "prompt": "...", "attempt_num": 1}},
            },
            "POST /api/task/{project_id}/complete": {
                "description": "Mark task completed. Sets execution_status and notification_status=pending.",
                "body": {"task_id": "task-xxx", "execution_status": "succeeded|failed", "error_message": ""},
                "note": "Failed tasks auto-retry if attempt_count < max_attempts.",
            },
            "POST /api/task/{project_id}/notify": {
                "description": "Mark task as notified (user has been informed).",
                "body": {"task_id": "task-xxx"},
            },
            "GET /api/task/{project_id}/list": {
                "description": "List tasks.",
                "query": "status=running&limit=50",
            },
        },
    },
    "executor": {
        "title": "Executor (host machine task executor)",
        "description": "Persistent process monitors the pending/ directory, claims and executes Claude/Codex CLI tasks. Integrates Task Registry + Redis notifications.",
        "flow": {
            "1_pick": "scan pending/*.json (skip .tmp.json) → oldest first",
            "2_claim": "move to processing/ + Task Registry claim (DB insert queued→claimed→running)",
            "3_execute": "run_claude / run_codex / run_pipeline",
            "4_complete": "Task Registry complete (succeeded/failed) + Redis publish task:completed",
            "5_notify": "Gateway polls pending notifications → sends Telegram",
        },
        "features": {
            "atomic_write": "Gateway writes .tmp.json → fsync → rename to .json",
            "startup_recovery": "Scans processing/ for stale tasks (>5min), re-queues them",
            "heartbeat": "Background thread updates heartbeat_at every 30s",
            "tool_policy": "Commands checked against auto_allow/needs_approval/always_deny lists",
        },
    },
    "tool_policy": {
        "title": "Tool Policy (command security policy)",
        "description": "Executor checks security policy before executing commands. Three-tier classification.",
        "levels": {
            "auto_allow": "git diff, pytest, npm test, and other read-only/test commands → auto-execute",
            "needs_approval": "git push, docker compose down, npm publish → requires human confirmation",
            "always_deny": "rm -rf /, shutdown, reboot → always denied",
        },
        "note": "Currently string-matching; will be upgraded to a structured command capability model.",
    },
    "deployment": {
        "title": "Deployment (deployment workflow)",
        "description": "Automated detection and deployment workflow for switching from development to production.",
        "scripts": {
            "scripts/startup.sh": "One-click start of all services (Docker + domain pack + executor)",
            "scripts/pre-deploy-check.sh": "Pre-deploy checks (node status/coverage/docs/memory/gatekeeper/staging/config/gateway)",
            "deploy-governance.sh": "Zero-downtime deployment (auto-calls pre-deploy-check → build → staging verify → swap)",
        },
        "checks": {
            "node_status": "All nodes qa_pass",
            "coverage": "All changed files have corresponding nodes",
            "docs": "API docs >= 10 sections",
            "memory": "dbservice memories >= 5 entries",
            "gatekeeper": "release-gate PASS",
            "config_consistency": "dev/prod environment variables consistent",
            "staging": "staging container health + smoke test",
            "gateway_channel": "Telegram message channel reachable",
        },
        "usage": "GOV_COORDINATOR_TOKEN=gov-xxx ./deploy-governance.sh",
    },
    "executor_api": {
        "title": "Executor API (session intervention interface)",
        "description": "Host machine Executor embeds an HTTP API (:40100). Claude Code sessions can directly monitor, intervene, and debug via curl.",
        "port": 40100,
        "endpoints": {
            "monitoring": {
                "GET /health": "API health check",
                "GET /status": "Overall status (pending/processing/active sessions)",
                "GET /sessions": "Active AI process list",
                "GET /tasks": "Task list (supports project_id, status filtering)",
                "GET /task/{id}": "Single task details (including evidence, validator logs)",
                "GET /trace/{id}": "Trace details",
            },
            "intervention": {
                "POST /task/{id}/pause": "Pause a running task",
                "POST /task/{id}/cancel": "Cancel a task (terminate AI process)",
                "POST /task/{id}/retry": "Retry a failed task (move back to pending)",
                "POST /cleanup-orphans": "Clean up zombie processes and stuck tasks",
            },
            "direct_chat": {
                "POST /coordinator/chat": "Directly launch a Coordinator session (bypasses Telegram)",
                "body": {"message": "...", "project_id": "amingClaw", "chat_id": 0},
                "note": "Synchronously waits for AI to complete before returning, maximum 120s",
            },
            "debugging": {
                "GET /validator/last-result": "Most recent validation result (tier/pass/reject details)",
                "GET /context/{project_id}": "Current assembled context result",
                "GET /ai-session/{id}/output": "Raw AI output (stdout/stderr/exit_code)",
            },
        },
        "access": "Only accessible from host machine localhost:40100, does not go through nginx, no token required",
        "guide": "See docs/executor-api-guide.md for details",
    },
}


# ---------------------------------------------------------------------------
# Baseline Endpoints (Phase I)
# ---------------------------------------------------------------------------

@route("GET", "/api/baseline/{project_id}/list")
def handle_baseline_list(ctx: RequestContext):
    """List all baselines for a project."""
    pid = ctx.path_params["project_id"]
    conn = get_connection(pid)
    try:
        from . import baseline_service
        baselines = baseline_service.list_baselines(conn, pid)
        return {"ok": True, "baselines": baselines}
    finally:
        conn.close()


@route("GET", "/api/baseline/{project_id}/latest")
def handle_baseline_latest(ctx: RequestContext):
    """Get the latest baseline for a project."""
    pid = ctx.path_params["project_id"]
    conn = get_connection(pid)
    try:
        from . import baseline_service
        baselines = baseline_service.list_baselines(conn, pid)
        if not baselines:
            return ctx.handler._respond(404, {"error": "baseline_missing", "message": "No baselines found"})
        return {"ok": True, "baseline": baselines[0]}
    finally:
        conn.close()


@route("GET", "/api/baseline/{project_id}/{baseline_id}")
def handle_baseline_get(ctx: RequestContext):
    """Get a single baseline by ID."""
    pid = ctx.path_params["project_id"]
    baseline_id = int(ctx.path_params["baseline_id"])
    conn = get_connection(pid)
    try:
        from . import baseline_service
        bl = baseline_service.get_baseline(conn, pid, baseline_id)
        return {"ok": True, "baseline": bl}
    except baseline_service.BaselineMissingError as e:
        return ctx.handler._respond(404, e.to_dict())
    finally:
        conn.close()


@route("GET", "/api/baseline/{project_id}/by-commit/{sha}")
def handle_baseline_by_commit(ctx: RequestContext):
    """Get baseline by commit SHA."""
    pid = ctx.path_params["project_id"]
    sha = ctx.path_params["sha"]
    conn = get_connection(pid)
    try:
        from . import baseline_service
        bl = baseline_service.get_by_commit(conn, pid, sha)
        return {"ok": True, "baseline": bl}
    except baseline_service.BaselineMissingError as e:
        return ctx.handler._respond(404, e.to_dict())
    finally:
        conn.close()


@route("POST", "/api/baseline/{project_id}/diff")
def handle_baseline_diff(ctx: RequestContext):
    """Diff two baselines. Body: {from, to, scope}."""
    pid = ctx.path_params["project_id"]
    body = ctx.body
    from_id = body.get("from")
    to_id = body.get("to")
    scope = body.get("scope", "full")
    if from_id is None or to_id is None:
        return ctx.handler._respond(400, {"error": "invalid_request", "message": "'from' and 'to' are required"})
    conn = get_connection(pid)
    try:
        from . import baseline_service
        delta = baseline_service.diff(conn, pid, int(from_id), int(to_id), scope)
        return {"ok": True, "delta": delta}
    except baseline_service.BaselineMissingError as e:
        return ctx.handler._respond(404, e.to_dict())
    finally:
        conn.close()


@route("POST", "/api/baseline/{project_id}/create")
def handle_baseline_create(ctx: RequestContext):
    """Create a new baseline. R7: trigger allowlist enforcement."""
    pid = ctx.path_params["project_id"]
    body = ctx.body
    triggered_by = body.get("triggered_by", "")
    from . import baseline_service
    if triggered_by not in baseline_service.TRIGGER_ALLOWLIST:
        return ctx.handler._respond(400, {
            "error": "invalid_request",
            "message": f"triggered_by must be one of {sorted(baseline_service.TRIGGER_ALLOWLIST)}, got {triggered_by!r}"
        })
    conn = get_connection(pid)
    try:
        bl = baseline_service.create_baseline(
            conn, pid,
            chain_version=body.get("chain_version", ""),
            trigger=body.get("trigger", triggered_by),
            triggered_by=triggered_by,
            graph_json=body.get("graph_json", {}),
            code_doc_map_json=body.get("code_doc_map_json", {}),
            node_state_snap=body.get("node_state_snap", "{}"),
            chain_event_max=body.get("chain_event_max", 0),
            notes=body.get("notes", ""),
            reconstructed=body.get("reconstructed", 0),
        )
        return {"ok": True, "baseline": bl}
    except ValueError as e:
        return ctx.handler._respond(400, {"error": "invalid_request", "message": str(e)})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Baseline GC Endpoint
# ---------------------------------------------------------------------------

@route("POST", "/api/baseline/{project_id}/gc")
def handle_baseline_gc(ctx: RequestContext):
    """Run baseline garbage collection (coordinator-only). R8."""
    pid = ctx.path_params["project_id"]
    conn = get_connection(pid)
    try:
        session = ctx.require_auth(conn)
        if session.get("role") != "coordinator":
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(
                session.get("role", ""), "baseline.gc",
                {"detail": "Only coordinator can run baseline GC"})
    finally:
        conn.close()

    body = ctx.body
    dry_run = body.get("dry_run", True)
    keep_last_n = body.get("keep_last_n", 100)

    from . import baseline_gc
    result = baseline_gc.gc_baselines(pid, dry_run=dry_run, keep_last_n=keep_last_n)
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# Backlog Endpoints (OPT-DB-BACKLOG)
# ---------------------------------------------------------------------------

@route("GET", "/api/backlog/{project_id}")
def handle_backlog_list(ctx: RequestContext):
    """List backlog bugs, optionally filtered by ?status= and ?priority=."""
    pid = ctx.path_params["project_id"]
    status_filter = ctx.query.get("status", "")
    priority_filter = ctx.query.get("priority", "")
    conn = get_connection(pid)
    try:
        sql = "SELECT * FROM backlog_bugs WHERE 1=1"
        params = []
        if status_filter:
            sql += " AND status = ?"
            params.append(status_filter)
        if priority_filter:
            sql += " AND priority = ?"
            params.append(priority_filter)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()
        bugs = []
        for r in rows:
            bug = dict(r)
            # Parse required_docs from JSON string to list
            try:
                bug["required_docs"] = json.loads(bug.get("required_docs", "[]"))
            except (json.JSONDecodeError, TypeError):
                bug["required_docs"] = []
            # Parse provenance_paths from JSON string to list
            try:
                bug["provenance_paths"] = json.loads(bug.get("provenance_paths", "[]"))
            except (json.JSONDecodeError, TypeError):
                bug["provenance_paths"] = []
            bug["bypass_policy"] = backlog_runtime.parse_json_object(bug.get("bypass_policy_json", "{}"))
            bug["takeover"] = backlog_runtime.parse_json_object(bug.get("takeover_json", "{}"))
            bugs.append(bug)
        return {"bugs": bugs, "count": len(bugs)}
    finally:
        conn.close()


@route("GET", "/api/backlog/{project_id}/{bug_id}")
def handle_backlog_get(ctx: RequestContext):
    """Get a single backlog bug by ID. Returns 404 if missing."""
    pid = ctx.path_params["project_id"]
    bug_id = ctx.path_params["bug_id"]
    conn = get_connection(pid)
    try:
        row = conn.execute(
            "SELECT * FROM backlog_bugs WHERE bug_id = ?", (bug_id,)
        ).fetchone()
        if not row:
            raise GovernanceError("not_found", f"Bug {bug_id} not found", 404)
        result = dict(row)
        # Parse required_docs from JSON string to list
        try:
            result["required_docs"] = json.loads(result.get("required_docs", "[]"))
        except (json.JSONDecodeError, TypeError):
            result["required_docs"] = []
        # Parse provenance_paths from JSON string to list
        try:
            result["provenance_paths"] = json.loads(result.get("provenance_paths", "[]"))
        except (json.JSONDecodeError, TypeError):
            result["provenance_paths"] = []
        result["bypass_policy"] = backlog_runtime.parse_json_object(result.get("bypass_policy_json", "{}"))
        result["takeover"] = backlog_runtime.parse_json_object(result.get("takeover_json", "{}"))
        return result
    finally:
        conn.close()


@route("POST", "/api/backlog/{project_id}/{bug_id}")
def handle_backlog_upsert(ctx: RequestContext):
    """Upsert a backlog bug (ON CONFLICT DO UPDATE)."""
    pid = ctx.path_params["project_id"]
    bug_id = ctx.path_params["bug_id"]
    body = ctx.body
    now = _utc_now()
    conn = get_connection(pid)
    try:
        # --- AI triage gate (R2: before INSERT, skip if force_admit) ---
        decision = None
        if not body.get("force_admit"):
            try:
                from .backlog_triage import triage_backlog_insert
                open_rows = conn.execute(
                    "SELECT bug_id, title, target_files FROM backlog_bugs WHERE status='OPEN'"
                ).fetchall()
                open_rows = [dict(r) for r in open_rows]
                decision = triage_backlog_insert(body | {"bug_id": bug_id}, open_rows)
                action = decision.get("action", "admit")
                try:
                    audit_service.record(conn, pid, "backlog_triage", actor="ai_triage",
                                         bug_id=bug_id, details=json.dumps(decision))
                    conn.commit()
                except Exception:
                    pass
                if action == "reject_dup":
                    return 409, {"ok": False, "error": "duplicate", "duplicate_of": decision["related_bug_ids"],
                                 "reason": decision["reason"]}
                if action == "supersede":
                    # Insert new row first (fall through), then close old rows
                    pass  # insert happens below; old rows closed after
                if action == "merge_into" and decision["related_bug_ids"]:
                    target_id = decision["related_bug_ids"][0]
                    conn.execute(
                        "UPDATE backlog_bugs SET details_md = details_md || ? , updated_at = ? WHERE bug_id = ?",
                        ("\n\n---\nMerged from %s: %s" % (bug_id, body.get("details_md", "")), now, target_id))
                    conn.commit()
                    return {"ok": True, "bug_id": target_id, "action": "merge_into", "merged_from": bug_id}
            except Exception:
                try:
                    audit_service.record(conn, pid, "backlog_triage_failed", actor="ai_triage", bug_id=bug_id)
                    conn.commit()
                except Exception:
                    pass
                decision = {"action": "admit", "reason": "agent failure", "related_bug_ids": [], "confidence": 0.0}
        bypass_policy = backlog_runtime.parse_json_object(body.get("bypass_policy_json"))
        bypass_policy.update(backlog_runtime.parse_json_object(body.get("bypass_policy")))
        if body.get("mf_type"):
            bypass_policy["mf_type"] = backlog_runtime.normalize_mf_type(body.get("mf_type"), bypass_policy)
        bypass_policy_raw = backlog_runtime.policy_json(bypass_policy)
        takeover_raw = backlog_runtime.policy_json(backlog_runtime.parse_json_object(body.get("takeover_json")))
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, target_files, test_files,
                acceptance_criteria, chain_task_id, "commit", discovered_at,
                fixed_at, details_md, chain_trigger_json, required_docs,
                provenance_paths, bypass_policy_json, mf_type, takeover_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bug_id) DO UPDATE SET
                 title = excluded.title,
                 status = excluded.status,
                 priority = excluded.priority,
                 target_files = excluded.target_files,
                 test_files = excluded.test_files,
                 acceptance_criteria = excluded.acceptance_criteria,
                 chain_task_id = excluded.chain_task_id,
                 "commit" = excluded."commit",
                 discovered_at = excluded.discovered_at,
                 fixed_at = excluded.fixed_at,
                 details_md = excluded.details_md,
                 chain_trigger_json = excluded.chain_trigger_json,
                 required_docs = excluded.required_docs,
                 provenance_paths = excluded.provenance_paths,
                 bypass_policy_json = CASE
                   WHEN excluded.bypass_policy_json != '{}' THEN excluded.bypass_policy_json
                   ELSE backlog_bugs.bypass_policy_json
                 END,
                 mf_type = CASE
                   WHEN excluded.mf_type != '' THEN excluded.mf_type
                   ELSE backlog_bugs.mf_type
                 END,
                 takeover_json = CASE
                   WHEN excluded.takeover_json != '{}' THEN excluded.takeover_json
                   ELSE backlog_bugs.takeover_json
                 END,
                 updated_at = excluded.updated_at
            """,
            (
                bug_id,
                body.get("title", ""),
                body.get("status", "OPEN"),
                body.get("priority", "P3"),
                json.dumps(body.get("target_files", [])),
                json.dumps(body.get("test_files", [])),
                json.dumps(body.get("acceptance_criteria", [])),
                body.get("chain_task_id", ""),
                body.get("commit", ""),
                body.get("discovered_at", ""),
                body.get("fixed_at", ""),
                body.get("details_md", ""),
                json.dumps(body.get("chain_trigger_json", {})),
                json.dumps(body.get("required_docs", [])),
                json.dumps(body.get("provenance_paths", [])),
                bypass_policy_raw,
                backlog_runtime.normalize_mf_type(body.get("mf_type"), bypass_policy) if body.get("mf_type") else "",
                takeover_raw,
                now,
                now,
            ),
        )
        conn.commit()
        # Audit: backlog_upsert event
        try:
            audit_service.record(
                conn, pid, "backlog_upsert",
                actor=body.get("actor", "api"),
                bug_id=bug_id,
            )
            conn.commit()
        except Exception:
            pass  # best-effort audit
        # Supersede: close old rows after inserting new one
        if decision and decision.get("action") == "supersede":
            for old_id in decision.get("related_bug_ids", []):
                conn.execute("UPDATE backlog_bugs SET status='FIXED', updated_at=? WHERE bug_id=?", (now, old_id))
            conn.commit()
            return {"ok": True, "bug_id": bug_id, "action": "superseded", "closed_bugs": decision["related_bug_ids"]}
        return {"ok": True, "bug_id": bug_id, "action": "upserted"}
    finally:
        conn.close()


@route("POST", "/api/backlog/{project_id}/{bug_id}/predeclare-mf")
def handle_backlog_predeclare_mf(ctx: RequestContext):
    """Predeclare a manual fix: transition OPEN -> MF_PLANNED with mf_id validation."""
    pid = ctx.path_params["project_id"]
    bug_id = ctx.path_params["bug_id"]
    body = ctx.body
    now = _utc_now()

    # Validate mf_id format
    mf_id = body.get("mf_id", "")
    if not re.match(r"^MF-\d{4}-\d{2}-\d{2}-\d{3}$", mf_id):
        raise GovernanceError(
            "invalid_mf_id",
            f"mf_id must match MF-YYYY-MM-DD-NNN, got: {mf_id}",
            422,
        )

    # Validate reason length
    reason = body.get("reason", "")
    if len(reason) < 20:
        raise GovernanceError(
            "reason_too_short",
            f"reason must be >= 20 chars, got {len(reason)}",
            422,
        )

    conn = get_connection(pid)
    try:
        row = conn.execute(
            "SELECT bug_id, status, details_md, current_task_id, root_task_id, runtime_state, "
            "bypass_policy_json, mf_type, takeover_json FROM backlog_bugs WHERE bug_id = ?",
            (bug_id,),
        ).fetchone()
        if not row:
            raise GovernanceError("not_found", f"Bug {bug_id} not found", 404)

        current_status = row["status"]
        if current_status != "OPEN":
            raise GovernanceError(
                "invalid_status",
                f"Bug must be OPEN to predeclare MF, currently: {current_status}",
                422,
            )

        # Store mf_id and reason in details_md for start-mf ownership check
        existing_md = row["details_md"] or ""
        marker = f"\n\n<!-- MF-PREDECLARE mf_id={mf_id} reason={reason} -->"
        new_details = existing_md + marker

        conn.execute(
            """UPDATE backlog_bugs
               SET status = 'MF_PLANNED',
                   details_md = ?,
                   updated_at = ?
               WHERE bug_id = ?""",
            (new_details, now, bug_id),
        )
        predeclare_policy = backlog_runtime.parse_json_object(body.get("bypass_policy"))
        mf_type = backlog_runtime.normalize_mf_type(body.get("mf_type"), predeclare_policy)
        predeclare_policy = backlog_runtime.build_mf_policy(
            mf_type,
            mf_id=mf_id,
            observer_authorized=bool(body.get("observer_authorized", True)),
            reason=reason,
            existing_policy=predeclare_policy,
        )
        predeclare_policy.update({
            "mf_id": mf_id,
            "observer_authorized": bool(body.get("observer_authorized", True)),
        })
        backlog_runtime.update_backlog_runtime(
            conn,
            bug_id,
            "manual_fix_planned",
            project_id=pid,
            metadata=predeclare_policy,
            runtime_state="manual_fix_planned",
            bypass_policy=predeclare_policy,
            mf_type=mf_type,
        )
        conn.commit()

        # Audit: best-effort
        try:
            audit_service.record(
                conn, pid, "backlog_predeclare_mf",
                actor=body.get("actor", "api"),
                bug_id=bug_id,
                mf_id=mf_id,
            )
            conn.commit()
        except Exception:
            pass

        return {
            "ok": True,
            "bug_id": bug_id,
            "status": "MF_PLANNED",
            "mf_id": mf_id,
            "mf_type": mf_type,
        }
    finally:
        conn.close()


@route("POST", "/api/backlog/{project_id}/{bug_id}/start-mf")
def handle_backlog_start_mf(ctx: RequestContext):
    """Start a manual fix: transition MF_PLANNED -> MF_IN_PROGRESS with mf_id ownership check."""
    pid = ctx.path_params["project_id"]
    bug_id = ctx.path_params["bug_id"]
    body = ctx.body
    now = _utc_now()

    mf_id = body.get("mf_id", "")

    conn = get_connection(pid)
    try:
        row = conn.execute(
            "SELECT bug_id, status, details_md FROM backlog_bugs WHERE bug_id = ?",
            (bug_id,),
        ).fetchone()
        if not row:
            raise GovernanceError("not_found", f"Bug {bug_id} not found", 404)

        current_status = row["status"]
        if current_status != "MF_PLANNED":
            raise GovernanceError(
                "invalid_status",
                f"Bug must be MF_PLANNED to start MF, currently: {current_status}",
                422,
            )

        # Verify mf_id ownership via substring check on details_md
        details_md = row["details_md"] or ""
        if mf_id not in details_md:
            raise GovernanceError(
                "mf_id_mismatch",
                f"mf_id {mf_id} not found in bug details; ownership check failed",
                422,
            )

        existing_policy = backlog_runtime.parse_json_object(_row_get(row, "bypass_policy_json", "{}"))
        start_policy = {**existing_policy, **backlog_runtime.parse_json_object(body.get("bypass_policy"))}
        requested_mf_type = body.get("mf_type") or _row_get(row, "mf_type", "") or start_policy.get("mf_type", "")
        if body.get("bypass_graph_governance") is True and not requested_mf_type:
            requested_mf_type = backlog_runtime.MF_TYPE_SYSTEM_RECOVERY
        mf_type = backlog_runtime.normalize_mf_type(requested_mf_type, start_policy)
        if mf_type == backlog_runtime.MF_TYPE_CHAIN_RESCUE and body.get("bypass_graph_governance") is True:
            raise GovernanceError(
                "invalid_mf_policy",
                "chain_rescue MF cannot bypass graph governance; use mf_type='system_recovery'",
                422,
            )
        start_policy = backlog_runtime.build_mf_policy(
            mf_type,
            mf_id=mf_id,
            observer_authorized=bool(body.get("observer_authorized", True)),
            reason=body.get("reason", ""),
            existing_policy=start_policy,
        )

        takeover = _apply_mf_takeover(conn, pid, bug_id, body, row, start_policy)

        conn.execute(
            """UPDATE backlog_bugs
               SET status = 'MF_IN_PROGRESS',
                   updated_at = ?
               WHERE bug_id = ?""",
            (now, bug_id),
        )
        backlog_runtime.update_backlog_runtime(
            conn,
            bug_id,
            "manual_fix_in_progress",
            project_id=pid,
            metadata=start_policy,
            runtime_state="manual_fix_in_progress",
            bypass_policy=start_policy,
            mf_type=mf_type,
            takeover=takeover,
        )
        conn.commit()

        # Audit: best-effort
        try:
            audit_service.record(
                conn, pid, "backlog_start_mf",
                actor=body.get("actor", "api"),
                bug_id=bug_id,
                mf_id=mf_id,
                mf_type=mf_type,
                takeover=json.dumps(takeover, ensure_ascii=False),
            )
            conn.commit()
        except Exception:
            pass

        return {
            "ok": True,
            "bug_id": bug_id,
            "status": "MF_IN_PROGRESS",
            "mf_id": mf_id,
            "mf_type": mf_type,
            "bypass_policy": start_policy,
            "takeover": takeover,
        }
    finally:
        conn.close()


@route("POST", "/api/backlog/{project_id}/{bug_id}/close")
def handle_backlog_close(ctx: RequestContext):
    """Close a backlog bug: set status=FIXED, commit, fixed_at."""
    pid = ctx.path_params["project_id"]
    bug_id = ctx.path_params["bug_id"]
    body = ctx.body
    now = _utc_now()
    conn = get_connection(pid)
    try:
        row = conn.execute(
            "SELECT bug_id, status FROM backlog_bugs WHERE bug_id = ?", (bug_id,)
        ).fetchone()
        if not row:
            raise GovernanceError("not_found", f"Bug {bug_id} not found", 404)

        prior_status = row["status"]

        # Allow closing from OPEN or MF_IN_PROGRESS
        if prior_status not in ("OPEN", "MF_IN_PROGRESS"):
            raise GovernanceError(
                "invalid_status",
                f"Bug must be OPEN or MF_IN_PROGRESS to close, currently: {prior_status}",
                422,
            )

        # Verify commit SHA exists in git log (best-effort)
        commit_sha = body.get("commit", "")
        if commit_sha:
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--verify", commit_sha],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode != 0:
                    raise GovernanceError(
                        "commit_not_found",
                        f"Commit {commit_sha} does not resolve to a real commit",
                        422,
                    )
            except subprocess.TimeoutExpired:
                log.warning("git rev-parse timed out for commit %s; allowing close", commit_sha)
            except FileNotFoundError:
                log.warning("git not found; skipping commit verification for %s", commit_sha)

        # Determine chain_stage based on prior status
        chain_stage = "manual-fix" if prior_status == "MF_IN_PROGRESS" else None

        update_sql = """UPDATE backlog_bugs
               SET status = 'FIXED',
                   "commit" = ?,
                   fixed_at = ?,
                   updated_at = ?"""
        params = [body.get("commit", ""), now, now]

        if chain_stage:
            update_sql += """,
                   chain_stage = ?"""
            params.append(chain_stage)

        update_sql += """
               WHERE bug_id = ?"""
        params.append(bug_id)

        conn.execute(update_sql, params)
        backlog_runtime.update_backlog_runtime(
            conn,
            bug_id,
            "manual_fix" if prior_status == "MF_IN_PROGRESS" else "fixed",
            project_id=pid,
            result={"commit": body.get("commit", "")},
            runtime_state="fixed",
        )
        conn.commit()
        # Audit: backlog_close event
        try:
            audit_service.record(
                conn, pid, "backlog_close",
                actor=body.get("actor", "auto-chain"),
                bug_id=bug_id,
            )
            conn.commit()
        except Exception:
            pass  # best-effort audit
        result = {"ok": True, "bug_id": bug_id, "status": "FIXED", "fixed_at": now}
        if chain_stage:
            result["chain_stage"] = chain_stage
        return result
    finally:
        conn.close()


@route("GET", "/api/docs")
def handle_docs_index(ctx: RequestContext):
    """Return available documentation sections."""
    sections = []
    for key, doc in _DOCS.items():
        sections.append({
            "section": key,
            "title": doc.get("title", key),
            "url": f"/api/docs/{key}",
        })
    return {"sections": sections}


@route("GET", "/api/docs/{section}")
def handle_docs_section(ctx: RequestContext):
    """Return a specific documentation section."""
    section = ctx.path_params.get("section", "")
    if section not in _DOCS:
        from .errors import GovernanceError
        raise GovernanceError(f"Unknown doc section: {section}. Available: {list(_DOCS.keys())}", 404)
    return _DOCS[section]


# ============================================================
# Server Entry Point
# ============================================================

def create_server(port: int = None) -> HTTPServer:
    p = port or PORT
    # Z0-sequel observer-hotfix 2026-04-24: ThreadingHTTPServer so a slow
    # handler (e.g. on_task_completed waiting on Z1's 60s busy_timeout DB lock)
    # doesn't starve every other HTTP request — the "post-completion wedge"
    # symptom that blocked the Z0+Z2 verification chain 3× in 30min.
    server = ThreadingHTTPServer(("0.0.0.0", p), GovernanceHandler)
    return server


def main():
    # Configure logging to INFO level for observability
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    # PID lock — kill old process, prevent zombies
    _acquire_pid_lock()
    print(f"Governance v{get_server_version()} (PID {SERVER_PID})")

    # Enable Redis Pub/Sub bridge for EventBus
    from .event_bus import get_event_bus
    redis = get_redis()
    if redis.available:
        get_event_bus().enable_redis_bridge()
        print("EventBus: Redis Pub/Sub bridge enabled")
    else:
        print("EventBus: Redis unavailable, in-process only")

    # Register chain context EventBus subscribers + recover active chains
    try:
        from .chain_context import register_events, get_store
        register_events()
        # Recover active chains for known projects
        from .db import _governance_root
        gov_root = _governance_root()
        if gov_root.exists():
            for pdir in gov_root.iterdir():
                if pdir.is_dir() and (pdir / "governance.db").exists():
                    get_store().recover_from_db(pdir.name)
        print("ChainContext: registered + recovered")
    except Exception as e:
        print(f"ChainContext: failed to start ({e})")

    # Start doc generator listener
    try:
        from .doc_generator import setup_listener
        setup_listener()
        print("DocGenerator: listening for node.created events")
    except Exception as e:
        print(f"DocGenerator: failed to start ({e})")

    # Start outbox worker for reliable event delivery
    try:
        from .outbox import OutboxWorker
        outbox_worker = OutboxWorker()
        outbox_worker.start()
        print("OutboxWorker: started")
    except Exception as e:
        print(f"OutboxWorker: failed to start ({e})")

    # Per-project chain history backfill at startup (R5)
    try:
        from .chain_trailer import backfill_legacy_chain_history
        _conn = get_connection("aming-claw")
        try:
            _rows = _conn.execute(
                "SELECT DISTINCT project_id FROM project_version"
            ).fetchall()
            _pids = [r["project_id"] if isinstance(r, dict) else r[0] for r in _rows]
        except Exception:
            _pids = ["aming-claw"]
        finally:
            _conn.close()
        for _pid in _pids:
            try:
                _res = backfill_legacy_chain_history(project_id=_pid, incremental=True)
                print(f"ChainTrailer: backfill[{_pid}] {_res.get('scan_mode','?')} — "
                      f"{_res.get('new_entries',0)} new, {_res.get('total_entries',0)} total")
            except Exception as _e:
                print(f"ChainTrailer: backfill[{_pid}] failed ({_e})")
    except Exception as e:
        print(f"ChainTrailer: backfill failed ({e})")

    server = create_server()
    print(f"Governance service listening on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
