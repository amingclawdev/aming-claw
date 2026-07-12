"""Public-safe health projections for the CLI Agent Service daemon."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


HEALTH_SCHEMA_VERSION = "cli_agent_service.health.v1"
SERVICE_NAME = "cli_agent_service"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def health_payload(
    *,
    pid: int,
    started_at: datetime,
    socket_ready: bool,
    stopping: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the stable local status payload without paths, argv, or credentials."""
    current = now or _utc_now()
    started = started_at.astimezone(timezone.utc) if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
    status = "stopping" if stopping else "running"
    return {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "service": SERVICE_NAME,
        "ok": bool(socket_ready and not stopping),
        "status": status,
        "pid": int(pid),
        "started_at": _timestamp(started),
        "uptime_seconds": max(0, int((current - started).total_seconds())),
        "socket_ready": bool(socket_ready),
        "accepting_agent_runs": False,
        "raw_credentials_exposed": False,
    }


def stopped_payload(*, pid: int = 0, stopped_at: datetime | None = None) -> dict[str, Any]:
    current = stopped_at or _utc_now()
    return {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "service": SERVICE_NAME,
        "ok": False,
        "status": "stopped",
        "pid": int(pid),
        "stopped_at": _timestamp(current),
        "uptime_seconds": 0,
        "socket_ready": False,
        "accepting_agent_runs": False,
        "raw_credentials_exposed": False,
    }
