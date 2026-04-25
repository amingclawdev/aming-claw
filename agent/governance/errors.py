"""Unified exception hierarchy for the governance service.

All governance errors inherit from GovernanceError and carry:
  - code:    machine-readable error code
  - message: human-readable description
  - status:  HTTP status code
  - details: optional structured context
"""


class GovernanceError(Exception):
    """Base class for all governance service errors."""

    def __init__(self, code: str, message: str, status: int = 400, details: dict = None):
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> dict:
        d = {"error": self.code, "message": self.message}
        if self.details:
            d["details"] = self.details
        return d


# --- 400 Bad Request ---

class ValidationError(GovernanceError):
    """Invalid request body, missing fields, or format errors."""

    def __init__(self, message: str, details: dict = None):
        super().__init__("invalid_request", message, 400, details)


class InvalidTransitionError(GovernanceError):
    """Attempted a state transition that doesn't exist in the state machine."""

    def __init__(self, from_status: str, to_status: str):
        super().__init__(
            "invalid_transition",
            f"No rule for transition {from_status} -> {to_status}",
            400,
            {"from": from_status, "to": to_status},
        )


class ForbiddenTransitionError(GovernanceError):
    """Attempted a forbidden transition (e.g., pending -> qa_pass skipping t2)."""

    def __init__(self, from_status: str, to_status: str, reason: str = ""):
        super().__init__(
            "forbidden_transition",
            reason or f"Transition {from_status} -> {to_status} is forbidden",
            403,
            {"from": from_status, "to": to_status},
        )


class InvalidEvidenceError(GovernanceError):
    """Evidence does not satisfy validation rules for the transition."""

    def __init__(self, message: str, details: dict = None):
        super().__init__("invalid_evidence", message, 400, details)


class NodeNotFoundError(GovernanceError):
    """Node ID does not exist in the DAG."""

    def __init__(self, node_id: str):
        super().__init__(
            "node_not_found",
            f"Node {node_id!r} not found in graph",
            400,
            {"node_id": node_id},
        )


class DAGError(GovernanceError):
    """DAG validation failed (e.g., cycle detected)."""

    def __init__(self, message: str, details: dict = None):
        super().__init__("dag_validation_failed", message, 400, details)


# --- 401 Unauthorized ---

class AuthError(GovernanceError):
    """Authentication failed — missing, invalid, or expired token."""

    def __init__(self, message: str = "Authentication required", code: str = "auth_required"):
        super().__init__(code, message, 401)


class TokenExpiredError(AuthError):
    def __init__(self):
        super().__init__("Token has expired", "token_expired")


class TokenInvalidError(AuthError):
    def __init__(self):
        super().__init__("Token does not match any active session", "token_invalid")


# --- 403 Forbidden ---

class PermissionDeniedError(GovernanceError):
    """Role lacks permission for the requested operation."""

    def __init__(self, role: str, action: str, details: dict = None):
        super().__init__(
            "permission_denied",
            f"Role {role!r} cannot perform {action}",
            403,
            details,
        )


class ScopeViolationError(GovernanceError):
    """Operation targets nodes outside the session's scope."""

    def __init__(self, node_id: str, scope: list):
        super().__init__(
            "scope_violation",
            f"Node {node_id!r} is outside session scope {scope}",
            403,
            {"node_id": node_id, "scope": scope},
        )


class GateUnsatisfiedError(GovernanceError):
    """One or more gate prerequisites are not met."""

    def __init__(self, node_id: str, unsatisfied: list):
        super().__init__(
            "gate_unsatisfied",
            f"Gate prerequisites not met for {node_id!r}",
            403,
            {"node_id": node_id, "unsatisfied_gates": unsatisfied},
        )


class ReleaseBlockedError(GovernanceError):
    """Release gate check failed — not all nodes are green."""

    def __init__(self, blockers: list, summary: dict):
        super().__init__(
            "release_blocked",
            f"{len(blockers)} node(s) blocking release",
            403,
            {"blockers": blockers, "summary": summary},
        )


# --- 409 Conflict ---

class ConflictError(GovernanceError):
    """Optimistic locking conflict — state was modified by another request."""

    def __init__(self, message: str = "State was modified by another request", details: dict = None):
        super().__init__("conflict", message, 409, details)


class DuplicateRoleError(GovernanceError):
    """Agent already has an active session with a different role."""

    def __init__(self, principal_id: str, existing_role: str):
        super().__init__(
            "duplicate_role",
            f"Principal {principal_id!r} already has active session as {existing_role!r}",
            409,
            {"principal_id": principal_id, "existing_role": existing_role},
        )


# --- 503 Service Unavailable ---

class RoleUnavailableError(GovernanceError):
    """Required role has no active registered session."""

    def __init__(self, role: str, needed_for: str = "", blocked_nodes: list = None):
        super().__init__(
            "role_unavailable",
            f"Required role {role!r} has no active session"
            + (f" (needed for {needed_for})" if needed_for else ""),
            503,
            {"role": role, "needed_for": needed_for, "blocked_nodes": blocked_nodes or []},
        )


# --- Baseline errors (Phase I) ---

class BaselineMissingError(GovernanceError):
    """Required baseline does not exist."""

    def __init__(self, project_id: str, baseline_id: int = None):
        bid_str = str(baseline_id) if baseline_id is not None else "any"
        super().__init__(
            "baseline_missing",
            f"Baseline {bid_str} not found for project {project_id!r}",
            404,
            {"project_id": project_id, "baseline_id": baseline_id},
        )


class BaselineCorruptedError(GovernanceError):
    """Baseline companion file failed sha256 verification."""

    def __init__(self, project_id: str, baseline_id: int, detail: str = ""):
        super().__init__(
            "baseline_corrupted",
            detail or f"Baseline {baseline_id} for project {project_id!r} is corrupted",
            500,
            {"project_id": project_id, "baseline_id": baseline_id},
        )


# --- 500 Internal ---

class InternalError(GovernanceError):
    """Unexpected server-side error."""

    def __init__(self, message: str = "Internal server error"):
        super().__init__("internal_error", message, 500)
