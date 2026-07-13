"""Host-private profile, run, lease, heartbeat, and restart registry."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .db import connect_registry_db, immediate_transaction, initialize_registry_db
from .models import (
    AgentProfile,
    AgentRun,
    CredentialRef,
    FieldResolution,
    GovernanceRef,
    HarnessRuntime,
    InferenceEndpoint,
    LauncherAdapter,
    ProcessObservation,
    ProfileState,
    ProfileStateRecord,
    ReconciliationResult,
    RegistryLease,
    RegistryRun,
    ResolutionCandidate,
    ResolvedAgentConfig,
    RolePolicy,
    RunState,
)


class RegistryError(RuntimeError):
    pass


class LeaseConflictError(RegistryError):
    pass


class LeaseNotOwnedError(RegistryError):
    pass


class PersistenceRejectedError(ValueError):
    pass


_SAFE_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_SAFE_REF_KEY = re.compile(r"[a-z][a-z0-9_]{1,127}")
_SAFE_REASON_CODE = re.compile(r"[a-z][a-z0-9_-]{0,127}")
_SECRET_VALUE = re.compile(
    r"(?:^|\s)(?:bearer\s+|sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_FORBIDDEN_KEYS = {
    "api_key",
    "credential",
    "credential_value",
    "password",
    "private_prompt",
    "prompt",
    "prompt_body",
    "raw_credential",
    "raw_output",
    "refresh_token",
    "route_token",
    "secret",
    "session_token",
}
_ALLOWED_REF_KEYS = {
    "commit_sha",
    "timeline_ref",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return _utc_now()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _timestamp(value: datetime | str | None = None) -> str:
    return _as_datetime(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _looks_like_path(value: str) -> bool:
    return (
        value.startswith(("/", "~/", "file://", "\\\\"))
        or bool(re.match(r"^[A-Za-z]:[\\/]", value))
        or "/../" in value
        or value.startswith("../")
    )


def _validate_persisted_value(value: Any, *, key: str = "") -> None:
    normalized_key = str(key or "").strip().lower()
    if normalized_key in _FORBIDDEN_KEYS or (
        "prompt" in normalized_key
        and normalized_key not in {"prompt_contract_id", "prompt_contract_hash"}
    ):
        raise PersistenceRejectedError("private or credential-bearing field cannot be persisted")
    if isinstance(value, Mapping):
        for child_key, child_value in value.items():
            _validate_persisted_value(child_value, key=str(child_key))
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _validate_persisted_value(child, key=normalized_key)
        return
    if not isinstance(value, str):
        return
    if _SECRET_VALUE.search(value) or _looks_like_path(value):
        raise PersistenceRejectedError("raw secret or shared-volume path cannot be persisted")


def sanitize_evidence_refs(values: Mapping[str, str] | None) -> tuple[GovernanceRef, ...]:
    refs: list[GovernanceRef] = []
    for name, value in (values or {}).items():
        normalized_name = str(name or "").strip()
        normalized_value = str(value or "").strip()
        if not _SAFE_REF_KEY.fullmatch(normalized_name):
            raise PersistenceRejectedError("evidence reference name is invalid")
        if not (
            normalized_name.endswith(("_ref", "_id", "_hash"))
            or normalized_name in _ALLOWED_REF_KEYS
        ):
            raise PersistenceRejectedError("only sanitized evidence references may be persisted")
        _validate_persisted_value(normalized_value, key=normalized_name)
        try:
            refs.append(GovernanceRef(normalized_name, normalized_value))
        except ValueError as exc:
            raise PersistenceRejectedError(str(exc)) from exc
    names = [ref.name for ref in refs]
    if len(names) != len(set(names)):
        raise PersistenceRejectedError("evidence references must be unique")
    return tuple(sorted(refs, key=lambda item: item.name))


def _profile_from_dict(data: Mapping[str, Any]) -> AgentProfile:
    runtime = data["harness_runtime"]
    endpoint = data["inference_endpoint"]
    credential = data["credential_ref"]
    launcher = data["launcher_adapter"]
    policy = data["role_policy"]
    return AgentProfile(
        profile_id=data["profile_id"],
        version=data["version"],
        harness_runtime=HarnessRuntime(
            runtime_id=runtime["runtime_id"], version=runtime["version"],
            kind=runtime.get("kind", ""), executable_ref=runtime.get("executable_ref", ""),
            capabilities=tuple(runtime.get("capabilities", ())),
        ),
        inference_endpoint=InferenceEndpoint(
            endpoint_id=endpoint["endpoint_id"], version=endpoint["version"],
            provider=endpoint["provider"], model=endpoint["model"],
            backend_mode=endpoint["backend_mode"], auth_mode=endpoint["auth_mode"],
            endpoint_kind=endpoint.get("endpoint_kind", ""),
        ),
        credential_ref=CredentialRef(
            ref_id=credential["credential_ref"], version=credential["version"],
            provider=credential.get("provider", ""), ref_kind=credential.get("ref_kind", "host_owned"),
        ),
        launcher_adapter=LauncherAdapter(
            launcher_id=launcher["launcher_id"], version=launcher["version"],
            kind=launcher.get("kind", "process"),
            environment_keys=tuple(launcher.get("environment_keys", ())),
            supports_host_handoff=bool(launcher.get("supports_host_handoff", False)),
        ),
        role_policy=RolePolicy(
            policy_id=policy["policy_id"], version=policy["version"],
            roles=tuple(policy.get("roles", ())), project_ids=tuple(policy.get("project_ids", ())),
            max_concurrency=int(policy.get("max_concurrency", 1)),
            timeout_sec=int(policy.get("timeout_sec", 120)),
            cooldown_sec=int(policy.get("cooldown_sec", 0)),
            successor_budget=int(policy.get("successor_budget", 0)),
        ),
        output_policy=data.get("output_policy", "hash_and_summary_only"),
        privacy_mode=data.get("privacy_mode", "host_private"),
    )


def _config_from_dict(data: Mapping[str, Any]) -> ResolvedAgentConfig:
    resolutions = []
    for field_name, item in data["resolution"].items():
        resolutions.append(FieldResolution(
            field_name=field_name, value=item["value"], source=item["source"],
            precedence=int(item["precedence"]),
            candidates=tuple(ResolutionCandidate(
                value=candidate["value"], source=candidate["source"],
                precedence=int(candidate["precedence"]), selected=bool(candidate.get("selected")),
            ) for candidate in item.get("candidates", ())),
        ))
    fields = {name: data[name] for name in (
        "profile_id", "profile_version", "runtime_id", "runtime_version", "endpoint_id",
        "endpoint_version", "credential_ref", "credential_ref_version", "launcher_id",
        "launcher_version", "role_policy_id", "role_policy_version", "provider", "model",
        "backend_mode", "auth_mode", "output_policy", "project_id", "role",
    )}
    return ResolvedAgentConfig(resolutions=tuple(resolutions), **fields)


def _run_from_dict(data: Mapping[str, Any], profile: AgentProfile | None) -> AgentRun:
    return AgentRun(
        run_id=data["run_id"], created_at=data.get("created_at", ""),
        parent_run_id=data.get("parent_run_id", ""),
        successor_of_run_id=data.get("successor_of_run_id", ""),
        profile=profile, config=_config_from_dict(data["config"]),
        governance_refs=tuple((data.get("governance_refs") or {}).items()),
    )


def process_start_identity(pid: int) -> str | None:
    """Return an OS process birth identity, or None when the PID is absent."""
    if int(pid) <= 0:
        return None
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        stat = stat_path.read_text(encoding="utf-8")
        tail = stat[stat.rfind(")") + 2 :].split()
        if len(tail) > 19:
            return "proc-start:{}".format(tail[19])
    except (OSError, UnicodeError):
        pass
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False, capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = " ".join(completed.stdout.split())
    return "ps-start:{}".format(started) if completed.returncode == 0 and started else None


class AgentRegistry:
    """Operational registry. It deliberately exposes no merge or close methods."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] = _utc_now,
        process_identity_reader: Callable[[int], Any] = process_start_identity,
    ) -> None:
        self.db_path = str(Path(db_path).expanduser())
        self._clock = clock
        self._process_identity_reader = process_identity_reader
        with self._connect() as conn:
            initialize_registry_db(conn)

    def _connect(self):
        return connect_registry_db(self.db_path)

    def register_profile(self, profile: AgentProfile) -> AgentProfile:
        payload = profile.to_public_dict()
        _validate_persisted_value(payload)
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        now = _timestamp(self._clock())
        with self._connect() as conn, immediate_transaction(conn):
            existing = conn.execute(
                "SELECT profile_version, profile_json FROM agent_profiles WHERE profile_id=?",
                (profile.profile_id,),
            ).fetchone()
            if existing:
                existing_profile = _profile_from_dict(json.loads(existing["profile_json"]))
                if existing["profile_version"] != profile.version or existing_profile != profile:
                    raise RegistryError("registered profile identity is immutable")
                if existing["profile_json"] != serialized:
                    conn.execute(
                        "UPDATE agent_profiles SET profile_json=?, max_concurrency=?, updated_at=? WHERE profile_id=?",
                        (
                            serialized,
                            profile.role_policy.max_concurrency,
                            now,
                            profile.profile_id,
                        ),
                    )
            conn.execute(
                "INSERT OR IGNORE INTO agent_profiles(profile_id, profile_version, profile_json, max_concurrency, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                (profile.profile_id, profile.version, serialized, profile.role_policy.max_concurrency, now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO agent_profile_states(profile_id, state, updated_at) VALUES(?, 'ready', ?)",
                (profile.profile_id, now),
            )
        return profile

    def get_profile(self, profile_id: str) -> AgentProfile | None:
        with self._connect() as conn:
            row = conn.execute("SELECT profile_json FROM agent_profiles WHERE profile_id=?", (profile_id,)).fetchone()
        return _profile_from_dict(json.loads(row["profile_json"])) if row else None

    def list_profiles(self) -> tuple[AgentProfile, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT profile_json FROM agent_profiles ORDER BY profile_id"
            ).fetchall()
        return tuple(_profile_from_dict(json.loads(row["profile_json"])) for row in rows)

    @staticmethod
    def _profile_state_from_row(row: Any) -> ProfileStateRecord:
        return ProfileStateRecord(
            profile_id=row["profile_id"],
            state=row["state"],
            reason_code=row["reason_code"],
            cooldown_until=row["cooldown_until"],
            quota_reset_at=row["quota_reset_at"],
            consecutive_crashes=int(row["consecutive_crashes"]),
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _deadline_elapsed(value: str, current: datetime) -> bool:
        if not str(value or "").strip():
            return False
        try:
            return _as_datetime(value) <= current
        except (TypeError, ValueError):
            return False

    def _expire_leases_tx(
        self,
        conn: Any,
        *,
        current_text: str,
    ) -> None:
        conn.execute(
            "UPDATE agent_leases SET status='expired', released_at=? "
            "WHERE status='active' AND expires_at<=?",
            (current_text, current_text),
        )

    def _profile_state_snapshot_tx(
        self,
        conn: Any,
        profile_id: str,
        *,
        current: datetime,
        current_text: str,
    ) -> tuple[ProfileStateRecord, int, int]:
        profile = conn.execute(
            "SELECT max_concurrency FROM agent_profiles WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        if not profile:
            raise KeyError(profile_id)
        conn.execute(
            "INSERT OR IGNORE INTO agent_profile_states(profile_id, state, updated_at) "
            "VALUES(?, 'ready', ?)",
            (profile_id, current_text),
        )
        row = conn.execute(
            "SELECT * FROM agent_profile_states WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        active_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM agent_leases "
                "WHERE profile_id=? AND status='active' AND expires_at>?",
                (profile_id, current_text),
            ).fetchone()[0]
        )
        max_concurrency = int(profile["max_concurrency"])
        state = str(row["state"])
        deadline_elapsed = (
            state in {ProfileState.COOLING_DOWN.value, ProfileState.UNHEALTHY.value}
            and self._deadline_elapsed(row["cooldown_until"], current)
        ) or (
            state == ProfileState.QUOTA_EXHAUSTED.value
            and self._deadline_elapsed(row["quota_reset_at"], current)
        )
        next_state = state
        clear_temporary = False
        if deadline_elapsed:
            next_state = ProfileState.READY.value
            clear_temporary = True
        if next_state == ProfileState.BUSY.value and active_count < max_concurrency:
            next_state = ProfileState.READY.value
        elif next_state == ProfileState.READY.value and active_count >= max_concurrency:
            next_state = ProfileState.BUSY.value
        if next_state != state or clear_temporary:
            conn.execute(
                "UPDATE agent_profile_states SET state=?, reason_code=?, "
                "cooldown_until=?, quota_reset_at=?, consecutive_crashes=?, updated_at=? "
                "WHERE profile_id=?",
                (
                    next_state,
                    "" if clear_temporary else row["reason_code"],
                    "" if clear_temporary else row["cooldown_until"],
                    "" if clear_temporary else row["quota_reset_at"],
                    0 if clear_temporary else int(row["consecutive_crashes"]),
                    current_text,
                    profile_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_profile_states WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
        return self._profile_state_from_row(row), active_count, max_concurrency

    def get_profile_state(
        self,
        profile_id: str,
        *,
        now: datetime | str | None = None,
    ) -> ProfileStateRecord:
        current = _as_datetime(now) if now is not None else self._clock()
        current_text = _timestamp(current)
        with self._connect() as conn, immediate_transaction(conn):
            self._expire_leases_tx(conn, current_text=current_text)
            state, _, _ = self._profile_state_snapshot_tx(
                conn,
                profile_id,
                current=current,
                current_text=current_text,
            )
        return state

    def list_profile_states(
        self,
        *,
        now: datetime | str | None = None,
    ) -> tuple[ProfileStateRecord, ...]:
        current = _as_datetime(now) if now is not None else self._clock()
        current_text = _timestamp(current)
        with self._connect() as conn, immediate_transaction(conn):
            self._expire_leases_tx(conn, current_text=current_text)
            profile_ids = [
                row["profile_id"]
                for row in conn.execute(
                    "SELECT profile_id FROM agent_profiles ORDER BY profile_id"
                ).fetchall()
            ]
            states = tuple(
                self._profile_state_snapshot_tx(
                    conn,
                    profile_id,
                    current=current,
                    current_text=current_text,
                )[0]
                for profile_id in profile_ids
            )
        return states

    def profile_capacity(
        self,
        profile_id: str,
        *,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        current = _as_datetime(now) if now is not None else self._clock()
        current_text = _timestamp(current)
        with self._connect() as conn, immediate_transaction(conn):
            self._expire_leases_tx(conn, current_text=current_text)
            state, active_count, max_concurrency = self._profile_state_snapshot_tx(
                conn,
                profile_id,
                current=current,
                current_text=current_text,
            )
        return {
            "profile_state": state,
            "active_lease_count": active_count,
            "max_concurrency": max_concurrency,
            "available": (
                state.state == ProfileState.READY.value
                and active_count < max_concurrency
            ),
        }

    def set_profile_state(
        self,
        profile_id: str,
        state: ProfileState | str,
        *,
        reason_code: str = "",
        cooldown_until: datetime | str | None = None,
        quota_reset_at: datetime | str | None = None,
        consecutive_crashes: int | None = None,
        now: datetime | str | None = None,
    ) -> ProfileStateRecord:
        normalized_state = (
            state.value if isinstance(state, ProfileState) else str(state or "").strip().lower()
        )
        if normalized_state not in {item.value for item in ProfileState}:
            raise ValueError("invalid profile state: {}".format(state))
        normalized_reason = str(reason_code or "").strip().lower()
        if normalized_reason and not _SAFE_REASON_CODE.fullmatch(normalized_reason):
            raise PersistenceRejectedError("profile reason_code must be a structured category")
        _validate_persisted_value(normalized_reason, key="reason_code")
        current = _as_datetime(now) if now is not None else self._clock()
        current_text = _timestamp(current)
        cooldown_text = _timestamp(cooldown_until) if cooldown_until else ""
        quota_text = _timestamp(quota_reset_at) if quota_reset_at else ""
        with self._connect() as conn, immediate_transaction(conn):
            self._expire_leases_tx(conn, current_text=current_text)
            current_state, _, _ = self._profile_state_snapshot_tx(
                conn,
                profile_id,
                current=current,
                current_text=current_text,
            )
            crash_count = (
                int(consecutive_crashes)
                if consecutive_crashes is not None
                else current_state.consecutive_crashes
            )
            if crash_count < 0:
                raise ValueError("consecutive_crashes cannot be negative")
            if normalized_state in {ProfileState.READY.value, ProfileState.BUSY.value}:
                normalized_reason = ""
                cooldown_text = ""
                quota_text = ""
                crash_count = 0
            conn.execute(
                "UPDATE agent_profile_states SET state=?, reason_code=?, "
                "cooldown_until=?, quota_reset_at=?, consecutive_crashes=?, updated_at=? "
                "WHERE profile_id=?",
                (
                    normalized_state,
                    normalized_reason,
                    cooldown_text,
                    quota_text,
                    crash_count,
                    current_text,
                    profile_id,
                ),
            )
            result, _, _ = self._profile_state_snapshot_tx(
                conn,
                profile_id,
                current=current,
                current_text=current_text,
            )
        return result

    def record_profile_signal(
        self,
        profile_id: str,
        signal: str,
        *,
        reason_code: str = "",
        retry_at: datetime | str | None = None,
        cooldown_seconds: int | None = None,
        now: datetime | str | None = None,
    ) -> ProfileStateRecord:
        normalized = str(signal or "").strip().lower().replace("-", "_")
        current = _as_datetime(now) if now is not None else self._clock()
        profile = self.get_profile(profile_id)
        if profile is None:
            raise KeyError(profile_id)
        seconds = profile.role_policy.cooldown_sec if cooldown_seconds is None else int(cooldown_seconds)
        if seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        deadline = retry_at or (current + timedelta(seconds=seconds) if seconds else None)
        current_state = self.get_profile_state(profile_id, now=current)
        reason = reason_code or normalized
        if normalized in {"ready", "healthy", "auth_restored", "quota_restored"}:
            return self.set_profile_state(profile_id, ProfileState.READY, now=current)
        if normalized in {"cooldown", "cooling_down", "rate_limited"}:
            return self.set_profile_state(
                profile_id,
                ProfileState.COOLING_DOWN,
                reason_code=reason,
                cooldown_until=deadline,
                now=current,
            )
        if normalized in {"quota", "quota_exhausted"}:
            return self.set_profile_state(
                profile_id,
                ProfileState.QUOTA_EXHAUSTED,
                reason_code=reason,
                quota_reset_at=deadline,
                now=current,
            )
        if normalized in {"auth", "auth_required", "auth_expired", "auth_revoked"}:
            return self.set_profile_state(
                profile_id,
                ProfileState.AUTH_REQUIRED,
                reason_code=reason,
                now=current,
            )
        if normalized in {
            "crash",
            "process_crash",
            "heartbeat_timeout",
            "endpoint_unavailable",
            "unhealthy",
        }:
            return self.set_profile_state(
                profile_id,
                ProfileState.UNHEALTHY,
                reason_code=reason,
                cooldown_until=deadline,
                consecutive_crashes=current_state.consecutive_crashes + 1,
                now=current,
            )
        if normalized in {"disable", "disabled"}:
            return self.set_profile_state(
                profile_id,
                ProfileState.DISABLED,
                reason_code=reason,
                now=current,
            )
        raise ValueError("unsupported profile signal: {}".format(signal))

    def record_quota_exhausted(self, profile_id: str, **kwargs: Any) -> ProfileStateRecord:
        return self.record_profile_signal(profile_id, "quota_exhausted", **kwargs)

    def record_auth_required(self, profile_id: str, **kwargs: Any) -> ProfileStateRecord:
        return self.record_profile_signal(profile_id, "auth_required", **kwargs)

    def record_profile_crash(self, profile_id: str, **kwargs: Any) -> ProfileStateRecord:
        return self.record_profile_signal(profile_id, "process_crash", **kwargs)

    def register_run(
        self,
        run: AgentRun,
        *,
        evidence_refs: Mapping[str, str] | None = None,
    ) -> RegistryRun:
        if run.profile is None:
            raise ValueError("registry runs require their immutable AgentProfile")
        self.register_profile(run.profile)
        refs = sanitize_evidence_refs(evidence_refs)
        payload = run.to_public_dict()
        _validate_persisted_value(payload)
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        refs_json = json.dumps({item.name: item.value for item in refs}, sort_keys=True, separators=(",", ":"))
        now = _timestamp(self._clock())
        with self._connect() as conn, immediate_transaction(conn):
            existing = conn.execute("SELECT run_json, evidence_refs_json FROM agent_runs WHERE run_id=?", (run.run_id,)).fetchone()
            if existing and (existing["run_json"] != serialized or existing["evidence_refs_json"] != refs_json):
                raise RegistryError("registered run identity is immutable")
            conn.execute(
                "INSERT OR IGNORE INTO agent_runs(run_id, profile_id, profile_version, project_id, role, run_json, state, parent_run_id, successor_of_run_id, evidence_refs_json, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run.run_id, run.profile.profile_id, run.profile.version, run.config.project_id, run.config.role, serialized, RunState.REGISTERED.value, run.parent_run_id, run.successor_of_run_id, refs_json, now, now),
            )
        record = self.get_run(run.run_id)
        assert record is not None
        return record

    create_run = register_run

    def _lease_from_row(self, row: Any) -> RegistryLease | None:
        if row is None or not row["lease_id"]:
            return None
        return RegistryLease(
            lease_id=row["lease_id"], run_id=row["lease_run_id"], profile_id=row["lease_profile_id"],
            owner_id=row["owner_id"], status=row["lease_status"], acquired_at=row["acquired_at"],
            expires_at=row["expires_at"], heartbeat_at=row["heartbeat_at"], released_at=row["released_at"],
        )

    @staticmethod
    def _lease_from_direct_row(row: Any) -> RegistryLease:
        return RegistryLease(
            lease_id=row["lease_id"],
            run_id=row["run_id"],
            profile_id=row["profile_id"],
            owner_id=row["owner_id"],
            status=row["status"],
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
            heartbeat_at=row["heartbeat_at"],
            released_at=row["released_at"],
        )

    def get_run(self, run_id: str) -> RegistryRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT r.*, l.lease_id, l.run_id AS lease_run_id, l.profile_id AS lease_profile_id, l.owner_id, l.status AS lease_status, l.acquired_at, l.expires_at, l.heartbeat_at, l.released_at FROM agent_runs r LEFT JOIN agent_leases l ON l.run_id=r.run_id AND l.status='active' WHERE r.run_id=?",
                (run_id,),
            ).fetchone()
        if not row:
            return None
        run_data = json.loads(row["run_json"])
        profile = self.get_profile(row["profile_id"])
        refs = tuple(GovernanceRef(name, value) for name, value in json.loads(row["evidence_refs_json"]).items())
        return RegistryRun(
            run=_run_from_dict(run_data, profile), state=row["state"], pid=row["pid"],
            process_start_identity=row["process_start_identity"], process_group_id=row["process_group_id"],
            argv_hash=row["argv_hash"], last_heartbeat_at=row["last_heartbeat_at"],
            lease=self._lease_from_row(row), evidence_refs=refs, exit_code=row["exit_code"],
            failure_category=row["failure_category"], updated_at=row["updated_at"],
        )

    def _acquire_lease_tx(
        self,
        conn: Any,
        run_id: str,
        owner_id: str,
        *,
        ttl_seconds: int,
        current: datetime,
        idempotent_owner: bool = False,
    ) -> RegistryLease:
        current_text = _timestamp(current)
        expires_text = _timestamp(current + timedelta(seconds=ttl_seconds))
        self._expire_leases_tx(conn, current_text=current_text)
        row = conn.execute(
            "SELECT r.profile_id, r.state, p.max_concurrency "
            "FROM agent_runs r JOIN agent_profiles p ON p.profile_id=r.profile_id "
            "WHERE r.run_id=?",
            (run_id,),
        ).fetchone()
        if not row:
            raise KeyError(run_id)
        if row["state"] in {
            RunState.COMPLETED.value,
            RunState.FAILED.value,
            RunState.LOST.value,
        }:
            raise LeaseConflictError("terminal run cannot be leased")
        duplicate = conn.execute(
            "SELECT * FROM agent_leases "
            "WHERE run_id=? AND status='active' AND expires_at>?",
            (run_id, current_text),
        ).fetchone()
        if duplicate:
            if idempotent_owner and duplicate["owner_id"] == owner_id:
                return self._lease_from_direct_row(duplicate)
            raise LeaseConflictError("run already has an active lease")
        profile_state, active_count, max_concurrency = self._profile_state_snapshot_tx(
            conn,
            row["profile_id"],
            current=current,
            current_text=current_text,
        )
        if profile_state.state != ProfileState.READY.value:
            raise LeaseConflictError(
                "profile state '{}' is not schedulable".format(profile_state.state)
            )
        if active_count >= max_concurrency:
            raise LeaseConflictError("profile has no available active lease capacity")
        lease_id = "lease-{}".format(uuid.uuid4().hex)
        conn.execute(
            "INSERT INTO agent_leases(lease_id, run_id, profile_id, owner_id, status, acquired_at, expires_at, heartbeat_at) "
            "VALUES(?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                lease_id,
                run_id,
                row["profile_id"],
                owner_id,
                current_text,
                expires_text,
                current_text,
            ),
        )
        conn.execute(
            "UPDATE agent_runs SET state=?, last_heartbeat_at=?, updated_at=? WHERE run_id=?",
            (RunState.LEASED.value, current_text, current_text, run_id),
        )
        self._profile_state_snapshot_tx(
            conn,
            row["profile_id"],
            current=current,
            current_text=current_text,
        )
        return RegistryLease(
            lease_id,
            run_id,
            row["profile_id"],
            owner_id,
            "active",
            current_text,
            expires_text,
            current_text,
        )

    def register_run_and_acquire_lease(
        self,
        run: AgentRun,
        owner_id: str,
        *,
        ttl_seconds: int = 60,
        evidence_refs: Mapping[str, str] | None = None,
        now: datetime | str | None = None,
    ) -> RegistryLease:
        """Atomically pin one run and reserve its selected profile capacity."""
        if run.profile is None:
            raise ValueError("registry runs require their immutable AgentProfile")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        owner_id = str(owner_id or "").strip()
        if not owner_id:
            raise ValueError("owner_id is required")
        _validate_persisted_value(owner_id, key="owner_id")
        self.register_profile(run.profile)
        refs = sanitize_evidence_refs(evidence_refs)
        payload = run.to_public_dict()
        _validate_persisted_value(payload)
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        refs_json = json.dumps(
            {item.name: item.value for item in refs},
            sort_keys=True,
            separators=(",", ":"),
        )
        current = _as_datetime(now) if now is not None else self._clock()
        current_text = _timestamp(current)
        with self._connect() as conn, immediate_transaction(conn):
            existing = conn.execute(
                "SELECT run_json, evidence_refs_json FROM agent_runs WHERE run_id=?",
                (run.run_id,),
            ).fetchone()
            if existing and (
                existing["run_json"] != serialized
                or existing["evidence_refs_json"] != refs_json
            ):
                raise RegistryError("registered run identity is immutable")
            conn.execute(
                "INSERT OR IGNORE INTO agent_runs(run_id, profile_id, profile_version, project_id, role, run_json, state, parent_run_id, successor_of_run_id, evidence_refs_json, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.run_id,
                    run.profile.profile_id,
                    run.profile.version,
                    run.config.project_id,
                    run.config.role,
                    serialized,
                    RunState.REGISTERED.value,
                    run.parent_run_id,
                    run.successor_of_run_id,
                    refs_json,
                    current_text,
                    current_text,
                ),
            )
            lease = self._acquire_lease_tx(
                conn,
                run.run_id,
                owner_id,
                ttl_seconds=ttl_seconds,
                current=current,
                idempotent_owner=True,
            )
        return lease

    reserve_run = register_run_and_acquire_lease

    def acquire_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl_seconds: int = 60,
        now: datetime | str | None = None,
    ) -> RegistryLease:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        owner_id = str(owner_id or "").strip()
        if not owner_id:
            raise ValueError("owner_id is required")
        _validate_persisted_value(owner_id, key="owner_id")
        current = _as_datetime(now) if now is not None else self._clock()
        with self._connect() as conn, immediate_transaction(conn):
            lease = self._acquire_lease_tx(
                conn,
                run_id,
                owner_id,
                ttl_seconds=ttl_seconds,
                current=current,
            )
        return lease

    def heartbeat(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl_seconds: int = 60,
        now: datetime | str | None = None,
    ) -> RegistryLease:
        _validate_persisted_value(owner_id, key="owner_id")
        current = _as_datetime(now) if now is not None else self._clock()
        current_text = _timestamp(current)
        expires_text = _timestamp(current + timedelta(seconds=ttl_seconds))
        with self._connect() as conn, immediate_transaction(conn):
            row = conn.execute(
                "SELECT * FROM agent_leases WHERE (run_id=? OR lease_id=?) AND owner_id=? AND status='active' AND expires_at>?",
                (run_id, run_id, owner_id, current_text),
            ).fetchone()
            if not row:
                raise LeaseNotOwnedError("active lease is absent, expired, or owned by another principal")
            conn.execute(
                "UPDATE agent_leases SET heartbeat_at=?, expires_at=? WHERE lease_id=?",
                (current_text, expires_text, row["lease_id"]),
            )
            conn.execute(
                "UPDATE agent_runs SET last_heartbeat_at=?, updated_at=? WHERE run_id=?",
                (current_text, current_text, row["run_id"]),
            )
        return RegistryLease(
            row["lease_id"], row["run_id"], row["profile_id"], owner_id,
            "active", row["acquired_at"], expires_text, current_text,
        )

    heartbeat_lease = heartbeat

    def release_lease(self, run_id: str, owner_id: str, *, now: datetime | str | None = None) -> None:
        _validate_persisted_value(owner_id, key="owner_id")
        current = _as_datetime(now) if now is not None else self._clock()
        current_text = _timestamp(current)
        with self._connect() as conn, immediate_transaction(conn):
            lease_row = conn.execute(
                "SELECT profile_id FROM agent_leases "
                "WHERE run_id=? AND owner_id=? AND status='active'",
                (run_id, owner_id),
            ).fetchone()
            if not lease_row:
                raise LeaseNotOwnedError("active lease is not owned by caller")
            cursor = conn.execute(
                "UPDATE agent_leases SET status='released', released_at=? WHERE run_id=? AND owner_id=? AND status='active'",
                (current_text, run_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise LeaseNotOwnedError("active lease is not owned by caller")
            conn.execute("UPDATE agent_runs SET state=?, updated_at=? WHERE run_id=?", (RunState.REGISTERED.value, current_text, run_id))
            self._profile_state_snapshot_tx(
                conn,
                lease_row["profile_id"],
                current=current,
                current_text=current_text,
            )

    def record_process_start(
        self,
        run_id: str,
        *,
        pid: int,
        process_start_identity: str,
        process_group_id: int | None = None,
        argv_hash: str = "",
        now: datetime | str | None = None,
    ) -> RegistryRun:
        if int(pid) <= 0 or not str(process_start_identity or "").strip():
            raise ValueError("pid and process_start_identity are required")
        _validate_persisted_value(
            str(process_start_identity).strip(),
            key="process_start_identity",
        )
        if argv_hash and not _SAFE_HASH.fullmatch(argv_hash):
            raise ValueError("argv_hash must be a sha256 content hash")
        current_text = _timestamp(now if now is not None else self._clock())
        with self._connect() as conn, immediate_transaction(conn):
            active = conn.execute("SELECT 1 FROM agent_leases WHERE run_id=? AND status='active' AND expires_at>?", (run_id, current_text)).fetchone()
            if not active:
                raise LeaseNotOwnedError("process start requires an active lease")
            cursor = conn.execute(
                "UPDATE agent_runs SET state=?, pid=?, process_start_identity=?, process_group_id=?, argv_hash=?, updated_at=? WHERE run_id=?",
                (RunState.RUNNING.value, int(pid), str(process_start_identity).strip(), process_group_id, argv_hash, current_text, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
        record = self.get_run(run_id)
        assert record is not None
        return record

    record_process = record_process_start

    def record_exit(
        self,
        run_id: str,
        exit_code: int,
        *,
        failure_category: str = "",
        now: datetime | str | None = None,
    ) -> RegistryRun:
        _validate_persisted_value(failure_category, key="failure_category")
        current = _as_datetime(now) if now is not None else self._clock()
        current_text = _timestamp(current)
        state = RunState.COMPLETED.value if int(exit_code) == 0 else RunState.FAILED.value
        with self._connect() as conn, immediate_transaction(conn):
            run_row = conn.execute(
                "SELECT profile_id FROM agent_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not run_row:
                raise KeyError(run_id)
            cursor = conn.execute(
                "UPDATE agent_runs SET state=?, exit_code=?, failure_category=?, updated_at=? WHERE run_id=?",
                (state, int(exit_code), str(failure_category or "").strip(), current_text, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)
            conn.execute("UPDATE agent_leases SET status=?, released_at=? WHERE run_id=? AND status='active'", (state, current_text, run_id))
            self._profile_state_snapshot_tx(
                conn,
                run_row["profile_id"],
                current=current,
                current_text=current_text,
            )
        record = self.get_run(run_id)
        assert record is not None
        return record

    def _observation(self, pid: int, reader: Callable[[int], Any]) -> ProcessObservation:
        try:
            value = reader(pid)
        except (OSError, PermissionError):
            return ProcessObservation(alive=False, observable=False)
        if isinstance(value, ProcessObservation):
            return value
        if isinstance(value, Mapping):
            return ProcessObservation(
                alive=bool(value.get("alive")), start_identity=str(value.get("start_identity") or ""),
                exit_code=value.get("exit_code"), observable=bool(value.get("observable", True)),
            )
        if isinstance(value, str):
            return ProcessObservation(alive=True, start_identity=value)
        return ProcessObservation(alive=False, observable=True)

    def reconcile_runs(
        self,
        *,
        process_identity_reader: Callable[[int], Any] | None = None,
        now: datetime | str | None = None,
    ) -> tuple[ReconciliationResult, ...]:
        reader = process_identity_reader or self._process_identity_reader
        current = _as_datetime(now) if now is not None else self._clock()
        current_text = _timestamp(current)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.*, l.status AS lease_status, l.expires_at AS lease_expires_at FROM agent_runs r LEFT JOIN agent_leases l ON l.run_id=r.run_id AND l.status='active' ORDER BY r.created_at, r.run_id"
            ).fetchall()
        results = []
        for row in rows:
            previous = row["state"]
            if previous in {RunState.COMPLETED.value, RunState.FAILED.value}:
                classification = previous
                matched = False
                detail = "terminal host result retained"
            elif not row["pid"] or not row["process_start_identity"]:
                classification = RunState.ORPHANED.value
                matched = False
                detail = "run has no complete persisted process identity"
            else:
                observation = self._observation(int(row["pid"]), reader)
                matched = observation.alive and observation.start_identity == row["process_start_identity"]
                lease_active = row["lease_status"] == "active" and row["lease_expires_at"] > current_text
                observed_exit = observation.exit_code if observation.exit_code is not None else row["exit_code"]
                if not observation.observable:
                    classification = RunState.ORPHANED.value
                    detail = "process ownership cannot be observed"
                elif observation.alive and not matched:
                    classification = RunState.LOST.value
                    detail = "PID exists with a different process-start identity"
                elif matched and lease_active:
                    classification = RunState.LIVE.value
                    detail = "PID and process-start identity match an active lease"
                elif matched:
                    classification = RunState.ORPHANED.value
                    detail = "owned process is live without an active lease"
                elif observed_exit is not None:
                    classification = RunState.COMPLETED.value if int(observed_exit) == 0 else RunState.FAILED.value
                    detail = "process exited with a persisted result"
                else:
                    classification = RunState.LOST.value
                    detail = "persisted process is no longer present"
            with self._connect() as conn, immediate_transaction(conn):
                conn.execute("UPDATE agent_runs SET state=?, updated_at=? WHERE run_id=?", (classification, current_text, row["run_id"]))
                if classification != RunState.LIVE.value:
                    conn.execute(
                        "UPDATE agent_leases SET status=?, released_at=? WHERE run_id=? AND status='active'",
                        (classification, current_text, row["run_id"]),
                    )
                self._profile_state_snapshot_tx(
                    conn,
                    row["profile_id"],
                    current=current,
                    current_text=current_text,
                )
            results.append(ReconciliationResult(row["run_id"], classification, previous, matched, detail))
        return tuple(results)

    reconcile_restart = reconcile_runs


PrivateAgentRegistry = AgentRegistry
CliAgentRegistry = AgentRegistry
Registry = AgentRegistry
DuplicateActiveLeaseError = LeaseConflictError
