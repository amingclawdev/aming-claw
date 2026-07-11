"""Provider-neutral AI invocation contracts and adapters.

This module keeps prompt routing, backend selection, and audit evidence in one
place so observer/subagent runtime code can use CLI and API-key providers
without duplicating evidence policy.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


REQUEST_SCHEMA_VERSION = "ai_invocation_request.v1"
RESULT_SCHEMA_VERSION = "ai_invocation_result.v1"
DOCKER_LIVE_OBSERVER_ROUTE_SCHEMA_VERSION = "docker_live_observer_route_evidence.v1"

BACKEND_CODEX_CLI = "codex_cli"
BACKEND_CLAUDE_CLI = "claude_cli"
BACKEND_OPENAI_API = "openai_api"
BACKEND_ANTHROPIC_API = "anthropic_api"
BACKEND_FIXTURE = "fixture"
BACKEND_DOCKER_LIVE_AI = "docker_live_ai"

_SENSITIVE_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|credential)", re.I)
_SECRET_VALUE_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{8,})")


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _string(value: Any) -> str:
    return str(value or "").strip()


def _nested(mapping: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    cur: Any = mapping
    for key in keys:
        if not isinstance(cur, Mapping):
            return {}
        cur = cur.get(key)
    return cur if isinstance(cur, Mapping) else {}


def _first_string(mapping: Mapping[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = _string(mapping.get(name))
        if value:
            return value
    return ""


def redact_text(value: str, *, max_chars: int = 4000) -> str:
    text = _SECRET_VALUE_RE.sub("[REDACTED]", value or "")
    return text[:max_chars]


def redact_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for part in command:
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        lowered = part.lower()
        if _SENSITIVE_KEY_RE.search(lowered):
            redacted.append(part)
            if "=" not in part:
                redact_next = True
            continue
        redacted.append(redact_text(part, max_chars=300))
    return redacted


def safe_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    source = env or os.environ
    return {
        key: ("[REDACTED]" if _SENSITIVE_KEY_RE.search(key) else value)
        for key, value in source.items()
    }


@dataclass
class RoutePromptContract:
    route_context_hash: str = ""
    prompt_contract_id: str = ""
    prompt_contract_hash: str = ""
    route_token_ref: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "RoutePromptContract":
        data = payload if isinstance(payload, Mapping) else {}
        route_context = _nested(data, "route_context")
        route_prompt_contract = _nested(data, "route_prompt_contract")
        prompt_contract = _nested(data, "prompt_contract")
        route_token = _nested(data, "route_token")
        return cls(
            route_context_hash=(
                _first_string(data, ("route_context_hash",))
                or _first_string(route_context, ("route_context_hash",))
                or _first_string(route_prompt_contract, ("route_context_hash",))
                or _first_string(prompt_contract, ("route_context_hash",))
                or _first_string(route_token, ("route_context_hash",))
            ),
            prompt_contract_id=(
                _first_string(data, ("prompt_contract_id",))
                or _first_string(route_context, ("prompt_contract_id",))
                or _first_string(route_prompt_contract, ("prompt_contract_id", "id"))
                or _first_string(prompt_contract, ("prompt_contract_id", "id"))
                or _first_string(route_token, ("prompt_contract_id",))
            ),
            prompt_contract_hash=(
                _first_string(data, ("prompt_contract_hash",))
                or _first_string(route_context, ("prompt_contract_hash",))
                or _first_string(route_prompt_contract, ("prompt_contract_hash",))
                or _first_string(prompt_contract, ("prompt_contract_hash",))
                or _first_string(route_token, ("prompt_contract_hash",))
            ),
            route_token_ref=(
                _first_string(data, ("route_token_ref", "route_token_id", "token_id"))
                or _first_string(route_token, ("token_id", "route_token_id"))
            ),
        )

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "route_context_hash": self.route_context_hash,
            "prompt_contract_id": self.prompt_contract_id,
            "prompt_contract_hash": self.prompt_contract_hash,
            "route_token_ref": self.route_token_ref,
            "raw_context_exposed": False,
        }


@dataclass
class AIInvocationRequest:
    role: str
    provider: str
    model: str = ""
    backend_mode: str = ""
    cwd: str = ""
    prompt: str = ""
    system_prompt: str = ""
    timeout_sec: int = 120
    output_path: str = ""
    auth_mode: str = ""
    output_policy: str = "hash_and_summary_only"
    route: RoutePromptContract = field(default_factory=RoutePromptContract)
    metadata: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_agent_run(
        cls,
        run: Any,
        *,
        prompt: str,
        system_prompt: str = "",
        cwd: str = "",
        timeout_sec: int | None = None,
        output_path: str = "",
        route: RoutePromptContract | None = None,
        metadata: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> "AIInvocationRequest":
        """Adapt a pinned AgentRun into the existing invocation wire contract."""
        config = run.config
        request_metadata = dict(metadata or {})
        request_metadata["cli_agent_service"] = {
            "schema_version": "cli_agent_service.invocation_adapter.v1",
            "run_id": run.run_id,
            "profile_id": config.profile_id,
            "profile_version": config.profile_version,
            "runtime_id": config.runtime_id,
            "runtime_version": config.runtime_version,
            "endpoint_id": config.endpoint_id,
            "endpoint_version": config.endpoint_version,
            "launcher_id": config.launcher_id,
            "launcher_version": config.launcher_version,
            "role_policy_id": config.role_policy_id,
            "role_policy_version": config.role_policy_version,
            "credential_ref": config.credential_ref,
            "credential_ref_version": config.credential_ref_version,
            "resolution_sources": {
                resolution.field_name: resolution.source
                for resolution in config.resolutions
            },
            "raw_credential_material_exposed": False,
        }
        resolved_timeout = timeout_sec
        if resolved_timeout is None:
            resolved_timeout = (
                run.profile.role_policy.timeout_sec if run.profile is not None else 120
            )
        return cls(
            role=config.role,
            provider=config.provider,
            model=config.model,
            backend_mode=config.backend_mode,
            cwd=cwd,
            prompt=prompt,
            system_prompt=system_prompt,
            timeout_sec=resolved_timeout,
            output_path=output_path,
            auth_mode=config.auth_mode,
            output_policy=config.output_policy,
            route=route or RoutePromptContract(),
            metadata=request_metadata,
            env=dict(env or {}),
        )

    def resolved_backend(self) -> str:
        if self.backend_mode:
            return self.backend_mode
        provider = self.provider.lower()
        if provider == "openai":
            return BACKEND_CODEX_CLI
        if provider == "anthropic":
            return BACKEND_CLAUDE_CLI
        return BACKEND_FIXTURE

    def prompt_text(self) -> str:
        if self.system_prompt:
            return (
                "=== SYSTEM PROMPT START ===\n"
                f"{self.system_prompt}\n"
                "=== SYSTEM PROMPT END ===\n\n"
                "=== TASK PROMPT START ===\n"
                f"{self.prompt}\n"
                "=== TASK PROMPT END ===\n"
            )
        return self.prompt

    def to_evidence(self) -> dict[str, Any]:
        evidence = {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "backend_mode": self.resolved_backend(),
            "cwd": self.cwd,
            "timeout_sec": self.timeout_sec,
            "auth_mode": self.auth_mode,
            "output_policy": self.output_policy,
            "prompt_sha256": sha256_text(self.prompt_text()),
            "route_prompt_contract": self.route.as_dict(),
            "raw_prompt_exposed": False,
        }
        monitor_policy = _runtime_monitor_policy(self.metadata)
        if monitor_policy:
            evidence["runtime_monitor_policy"] = monitor_policy
        if self.env:
            evidence["env_keys"] = sorted(str(key) for key in self.env)
            evidence["raw_env_exposed"] = False
        return evidence


@dataclass
class AIInvocationResult:
    request: AIInvocationRequest
    status: str
    output_text: str = ""
    error: str = ""
    command: list[str] = field(default_factory=list)
    returncode: int = 0
    elapsed_ms: int = 0
    provider_backed: bool = False
    calls_models: bool = False
    raw_output_stored: bool = False
    auth_status: str = "unknown"
    output_path: str = ""
    blocker_id: str = ""
    runtime_monitor: dict[str, Any] = field(default_factory=dict)

    @property
    def output_sha256(self) -> str:
        return sha256_text(self.output_text)

    @property
    def prompt_sha256(self) -> str:
        return sha256_text(self.request.prompt_text())

    def to_evidence(self) -> dict[str, Any]:
        route = self.request.route.as_dict()
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "request_schema_version": REQUEST_SCHEMA_VERSION,
            "status": self.status,
            "role": self.request.role,
            "provider": self.request.provider,
            "model": self.request.model,
            "backend_mode": self.request.resolved_backend(),
            "auth_mode": self.request.auth_mode,
            "auth_status": self.auth_status,
            "provider_backed": self.provider_backed,
            "calls_models": self.calls_models,
            "returncode": self.returncode,
            "elapsed_ms": self.elapsed_ms,
            "command": redact_command(self.command),
            "route_prompt_contract": route,
            "route_alert_ack": {
                "status": "acknowledged" if route.get("route_context_hash") else "not_applicable",
                "route_context_hash": route.get("route_context_hash", ""),
                "prompt_contract_id": route.get("prompt_contract_id", ""),
                "prompt_contract_hash": route.get("prompt_contract_hash", ""),
            },
            "ordered_step_outputs": [
                {"step_id": "01_invocation_contract", "status": "passed"},
                {"step_id": "02_provider_backend", "status": "passed" if self.command or self.status != "failed" else "failed"},
                {"step_id": "03_sanitized_evidence", "status": "passed"},
            ],
            "prompt_sha256": self.prompt_sha256,
            "output_sha256": self.output_sha256,
            "output_empty": not bool((self.output_text or "").strip()),
            "raw_output_stored": self.raw_output_stored,
            "no_raw_prompt_output": True,
            "error": redact_text(self.error, max_chars=1000),
            "output_path": self.output_path,
            "blocker_id": self.blocker_id,
            "runtime_monitor": self.runtime_monitor,
        }


def build_codex_exec_command(
    *,
    model: str = "",
    cwd: str,
    output_path: str = "",
    dangerous: bool | None = None,
    sandbox: str = "workspace-write",
    ephemeral: bool = False,
    stream_json: bool = True,
) -> list[str]:
    codex_bin = os.getenv("CODEX_BIN", "").strip()
    if not codex_bin:
        codex_bin = "codex.cmd" if os.name == "nt" else "codex"
    use_dangerous = (
        os.getenv("CODEX_DANGEROUS", "1").strip().lower() not in {"0", "false", "no"}
        if dangerous is None
        else dangerous
    )
    cmd = [codex_bin, "exec"]
    if model:
        cmd.extend(["--model", model])
    if use_dangerous:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.extend(["--sandbox", sandbox])
    cmd.append("--skip-git-repo-check")
    if ephemeral:
        cmd.append("--ephemeral")
    if stream_json:
        cmd.append("--json")
    cmd.extend(["-C", cwd])
    if output_path:
        cmd.extend(["-o", output_path])
    return cmd


def build_claude_code_command(
    *,
    model: str = "",
    cwd: str = "",
    prompt_file: str = "",
    allowed_tools: str = "",
    max_turns: str = "",
) -> list[str]:
    claude_bin = os.getenv("CLAUDE_BIN", "claude")
    cmd = [claude_bin, "-p"]
    if prompt_file:
        cmd.extend(["--system-prompt-file", prompt_file])
    if model:
        cmd.extend(["--model", model])
    if cwd:
        cmd.extend(["--add-dir", cwd])
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])
    if max_turns:
        cmd.extend(["--max-turns", max_turns])
    return cmd


def _failed_result(
    request: AIInvocationRequest,
    *,
    error: str,
    command: list[str],
    elapsed_ms: int,
    auth_status: str = "unknown",
) -> AIInvocationResult:
    return AIInvocationResult(
        request=request,
        status="failed",
        error=error,
        command=command,
        returncode=1,
        elapsed_ms=elapsed_ms,
        provider_backed=request.resolved_backend() != BACKEND_FIXTURE,
        calls_models=False,
        auth_status=auth_status,
    )


def _metadata_float(metadata: Mapping[str, Any], key: str, *, default: float = 0.0) -> float:
    try:
        return max(0.0, float(metadata.get(key) or default))
    except (TypeError, ValueError):
        return default


def _runtime_monitor_policy(metadata: Mapping[str, Any]) -> dict[str, Any]:
    early_progress_timeout_sec = _metadata_float(
        metadata,
        "early_progress_timeout_sec",
    )
    heartbeat_callback = metadata.get("heartbeat_callback")
    heartbeat_enabled = callable(heartbeat_callback)
    if not early_progress_timeout_sec and not heartbeat_enabled:
        return {}
    return {
        "schema_version": "ai_invocation_runtime_monitor_policy.v1",
        "early_progress_timeout_sec": early_progress_timeout_sec,
        "heartbeat_enabled": heartbeat_enabled,
        "heartbeat_interval_sec": _metadata_float(
            metadata,
            "heartbeat_interval_sec",
            default=0.0,
        ),
    }


def _git_status_short(cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _output_file_nonempty(output_path: str) -> bool:
    if not output_path:
        return False
    try:
        return Path(output_path).exists() and Path(output_path).stat().st_size > 0
    except OSError:
        return False


def _codex_cli_progress_snapshot(
    *,
    cwd: str,
    output_path: str,
    baseline_git_status: str,
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
) -> dict[str, Any]:
    current_git_status = _git_status_short(cwd)
    git_status_available = bool(baseline_git_status or current_git_status)
    worktree_changed = (
        git_status_available
        and current_git_status != baseline_git_status
    )
    output_file_nonempty = _output_file_nonempty(output_path)
    stream_output_observed = stdout_bytes > 0 or stderr_bytes > 0
    return {
        "schema_version": "codex_cli_progress_snapshot.v1",
        "output_file_nonempty": output_file_nonempty,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "stream_output_observed": stream_output_observed,
        "worktree_status_available": git_status_available,
        "worktree_changed": worktree_changed,
        "progress_observed": output_file_nonempty
        or stream_output_observed
        or worktree_changed,
    }


def _read_temp_file(handle: Any) -> str:
    try:
        handle.seek(0)
        return handle.read()
    except Exception:
        return ""


def _read_output_text(output_path: str, fallback: str) -> str:
    if output_path:
        try:
            return Path(output_path).read_text(encoding="utf-8")
        except OSError:
            return fallback
    return fallback


def _temp_file_size(handle: Any) -> int:
    try:
        handle.flush()
        current = handle.tell()
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(current)
        return max(0, int(size))
    except Exception:
        return 0


def _stop_process(process: subprocess.Popen[str], *, terminate_first: bool = True) -> None:
    if process.poll() is not None:
        return
    try:
        if terminate_first:
            process.terminate()
            try:
                process.wait(timeout=3)
                return
            except subprocess.TimeoutExpired:
                pass
        process.kill()
        process.wait(timeout=3)
    except Exception:
        return


def _merged_env(additions: Mapping[str, str] | None = None) -> dict[str, str] | None:
    if not additions:
        return None
    env = dict(os.environ)
    env.update({str(key): str(value) for key, value in additions.items()})
    return env


def _call_heartbeat(callback: Any) -> dict[str, Any]:
    try:
        payload = callback()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if isinstance(payload, Mapping):
        return {
            "ok": bool(payload.get("ok")),
            "http_status": payload.get("http_status"),
            "observer_session_id": str(payload.get("observer_session_id") or ""),
            "phase": str(payload.get("phase") or "execute_child"),
        }
    return {"ok": bool(payload), "phase": "execute_child"}


def _invoke_codex_cli_monitored(
    *,
    request: AIInvocationRequest,
    command: list[str],
    cwd: str,
    prompt: str,
    output_path: str,
    started: float,
) -> AIInvocationResult:
    early_progress_timeout_sec = _metadata_float(
        request.metadata,
        "early_progress_timeout_sec",
    )
    heartbeat_callback = request.metadata.get("heartbeat_callback")
    heartbeat_enabled = callable(heartbeat_callback)
    heartbeat_interval_sec = _metadata_float(
        request.metadata,
        "heartbeat_interval_sec",
        default=10.0,
    )
    if heartbeat_enabled and heartbeat_interval_sec <= 0:
        heartbeat_interval_sec = 10.0
    monitor: dict[str, Any] = {
        "schema_version": "codex_cli_runtime_monitor.v1",
        "early_progress_timeout_sec": early_progress_timeout_sec,
        "heartbeat_enabled": heartbeat_enabled,
        "heartbeat_interval_sec": heartbeat_interval_sec if heartbeat_enabled else 0.0,
        "heartbeat_count": 0,
        "heartbeat_failures": 0,
        "progress_observed": False,
    }
    baseline_git_status = _git_status_short(cwd) if early_progress_timeout_sec else ""
    stdout_handle = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    stderr_handle = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            cwd=cwd,
            env=_merged_env(request.env),
        )
        if process.stdin is not None:
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except BrokenPipeError:
                pass

        deadline = started + max(0, int(request.timeout_sec or 0))
        progress_deadline = (
            started + early_progress_timeout_sec
            if early_progress_timeout_sec
            else 0.0
        )
        next_heartbeat = started if heartbeat_enabled else 0.0
        while process.poll() is None:
            now = time.perf_counter()
            if heartbeat_enabled and now >= next_heartbeat:
                heartbeat = _call_heartbeat(heartbeat_callback)
                monitor["heartbeat_count"] += 1
                if not heartbeat.get("ok"):
                    monitor["heartbeat_failures"] += 1
                    monitor["last_heartbeat"] = heartbeat
                    _stop_process(process)
                    return AIInvocationResult(
                        request=request,
                        status="blocked",
                        output_text="",
                        error="observer session heartbeat failed while codex_cli worker was active",
                        command=command,
                        returncode=126,
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        provider_backed=True,
                        calls_models=False,
                        auth_status="observer_heartbeat_failed",
                        output_path=output_path,
                        blocker_id="observer_session_heartbeat_failed_during_cli_worker",
                        runtime_monitor=monitor,
                    )
                monitor["last_heartbeat"] = heartbeat
                next_heartbeat = now + heartbeat_interval_sec

            if progress_deadline and now >= progress_deadline:
                progress = _codex_cli_progress_snapshot(
                    cwd=cwd,
                    output_path=output_path,
                    baseline_git_status=baseline_git_status,
                    stdout_bytes=_temp_file_size(stdout_handle),
                    stderr_bytes=_temp_file_size(stderr_handle),
                )
                monitor["early_progress"] = progress
                monitor["progress_observed"] = bool(progress.get("progress_observed"))
                if not progress.get("progress_observed"):
                    _stop_process(process)
                    return AIInvocationResult(
                        request=request,
                        status="blocked",
                        output_text="",
                        error=(
                            "codex_cli_worker_no_progress_no_read_receipt: no output "
                            "or worktree changes within early progress timeout"
                        ),
                        command=command,
                        returncode=125,
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        provider_backed=True,
                        calls_models=False,
                        auth_status="cli_no_progress",
                        output_path=output_path,
                        blocker_id="codex_cli_worker_no_progress_no_read_receipt",
                        runtime_monitor=monitor,
                    )
                progress_deadline = 0.0

            if request.timeout_sec and now >= deadline:
                _stop_process(process)
                partial_output = _read_output_text(output_path, _read_temp_file(stdout_handle))
                partial_error = _read_temp_file(stderr_handle).strip()
                message = f"codex_cli invocation timed out after {request.timeout_sec}s"
                if partial_error:
                    message = f"{message}: {partial_error}"
                return AIInvocationResult(
                    request=request,
                    status="blocked",
                    output_text=partial_output,
                    error=message,
                    command=command,
                    returncode=124,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    provider_backed=True,
                    calls_models=False,
                    auth_status="cli_timeout",
                    output_path=output_path,
                    blocker_id="codex_cli_timeout",
                    runtime_monitor=monitor,
                )

            next_wake = deadline if request.timeout_sec else now + 0.2
            if progress_deadline:
                next_wake = min(next_wake, progress_deadline)
            if heartbeat_enabled:
                next_wake = min(next_wake, next_heartbeat)
            sleep_for = max(0.01, min(0.2, next_wake - now))
            time.sleep(sleep_for)

        stdout_text = _read_temp_file(stdout_handle)
        stderr_text = _read_temp_file(stderr_handle)
        output_text = _read_output_text(output_path, stdout_text)
        return AIInvocationResult(
            request=request,
            status="completed" if process.returncode == 0 else "failed",
            output_text=output_text,
            error=stderr_text if process.returncode else "",
            command=command,
            returncode=int(process.returncode or 0),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            provider_backed=True,
            calls_models=process.returncode == 0,
            auth_status="cli_auth_unknown" if process.returncode == 0 else "cli_failed",
            output_path=output_path,
            runtime_monitor=monitor,
        )
    finally:
        if process is not None:
            _stop_process(process, terminate_first=False)
        try:
            stdout_handle.close()
            stderr_handle.close()
        except Exception:
            pass


def invoke_fixture(request: AIInvocationRequest) -> AIInvocationResult:
    output = '{"ok":true,"provider":"%s","backend":"fixture"}' % (request.provider or "fixture")
    return AIInvocationResult(
        request=request,
        status="completed",
        output_text=output,
        command=["fixture", request.provider or "fixture"],
        returncode=0,
        provider_backed=False,
        calls_models=False,
        auth_status="not_required",
    )


def invoke_api(request: AIInvocationRequest) -> AIInvocationResult:
    backend = request.resolved_backend()
    provider = "openai" if backend == BACKEND_OPENAI_API else "anthropic"
    model = request.model or ("gpt-4o" if provider == "openai" else "claude-sonnet-4-6")
    command = ["api", provider, model]
    started = time.perf_counter()
    prompt = request.prompt_text()
    try:
        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                return _failed_result(
                    request,
                    error="OPENAI_API_KEY not set",
                    command=command,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    auth_status="missing_api_key",
                )
            import requests as _req

            resp = _req.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": int(request.metadata.get("max_tokens") or 4096),
                },
                timeout=request.timeout_sec,
            )
            if resp.status_code >= 400:
                return _failed_result(
                    request,
                    error=_api_error("OpenAI", resp),
                    command=command,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    auth_status="api_error",
                )
            output = resp.json()["choices"][0]["message"]["content"]
        else:
            api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
            if not api_key:
                return _failed_result(
                    request,
                    error="ANTHROPIC_API_KEY not set",
                    command=command,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    auth_status="missing_api_key",
                )
            import requests as _req

            resp = _req.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": int(request.metadata.get("max_tokens") or 8192),
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=request.timeout_sec,
            )
            if resp.status_code >= 400:
                return _failed_result(
                    request,
                    error=_api_error("Anthropic", resp),
                    command=command,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    auth_status="api_error",
                )
            output = resp.json()["content"][0]["text"]
        return AIInvocationResult(
            request=request,
            status="completed",
            output_text=output,
            command=command,
            returncode=0,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            provider_backed=True,
            calls_models=True,
            auth_status="api_key_env",
        )
    except Exception as exc:
        return _failed_result(
            request,
            error=str(exc),
            command=command,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            auth_status="failed",
        )


def _api_error(provider: str, resp: Any) -> str:
    try:
        body = resp.json()
        err = body.get("error", {})
        if isinstance(err, Mapping):
            message = err.get("message") or err.get("type") or ""
        else:
            message = str(err)
    except Exception:
        message = getattr(resp, "text", "")[:500]
    return f"{provider} API error (HTTP {resp.status_code}): {message or 'unknown error'}"


def invoke_cli(request: AIInvocationRequest) -> AIInvocationResult:
    backend = request.resolved_backend()
    started = time.perf_counter()
    cwd = request.cwd or os.getcwd()
    output_path = request.output_path
    temp_dir = ""
    prompt = request.prompt_text()
    command: list[str] = []
    try:
        if not output_path and backend == BACKEND_CODEX_CLI:
            temp_dir = tempfile.mkdtemp(prefix="aming-claw-ai-invocation-")
            output_path = str(Path(temp_dir) / "last-message.txt")
        if backend == BACKEND_CODEX_CLI:
            command = build_codex_exec_command(model=request.model, cwd=cwd, output_path=output_path)
            if (
                _metadata_float(request.metadata, "early_progress_timeout_sec")
                or callable(request.metadata.get("heartbeat_callback"))
            ):
                return _invoke_codex_cli_monitored(
                    request=request,
                    command=command,
                    cwd=cwd,
                    prompt=prompt,
                    output_path=output_path,
                    started=started,
                )
            result = subprocess.run(
                command,
                input=prompt,
                text=True,
                cwd=cwd,
                env=_merged_env(request.env),
                capture_output=True,
                timeout=request.timeout_sec,
                check=False,
            )
            output_text = ""
            if output_path:
                try:
                    output_text = Path(output_path).read_text(encoding="utf-8")
                except OSError:
                    output_text = result.stdout
            return AIInvocationResult(
                request=request,
                status="completed" if result.returncode == 0 else "failed",
                output_text=output_text,
                error=result.stderr if result.returncode else "",
                command=command,
                returncode=result.returncode,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                provider_backed=True,
                calls_models=result.returncode == 0,
                auth_status="cli_auth_unknown" if result.returncode == 0 else "cli_failed",
                output_path=output_path,
            )
        command = build_claude_code_command(model=request.model, cwd=cwd)
        result = subprocess.run(
            command,
            input=prompt,
            text=True,
            cwd=cwd,
            env=_merged_env(request.env),
            capture_output=True,
            timeout=request.timeout_sec,
            check=False,
        )
        return AIInvocationResult(
            request=request,
            status="completed" if result.returncode == 0 else "failed",
            output_text=result.stdout,
            error=result.stderr if result.returncode else "",
            command=command,
            returncode=result.returncode,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            provider_backed=True,
            calls_models=result.returncode == 0,
            auth_status="cli_auth_unknown" if result.returncode == 0 else "cli_failed",
        )
    except subprocess.TimeoutExpired as exc:
        partial_output = ""
        if output_path:
            try:
                partial_output = Path(output_path).read_text(encoding="utf-8")
            except OSError:
                partial_output = ""
        if not partial_output and exc.stdout:
            partial_output = (
                exc.stdout.decode("utf-8", "replace")
                if isinstance(exc.stdout, bytes)
                else str(exc.stdout)
            )
        partial_error = (
            exc.stderr.decode("utf-8", "replace")
            if isinstance(exc.stderr, bytes)
            else str(exc.stderr or "")
        ).strip()
        message = f"{backend} invocation timed out after {request.timeout_sec}s"
        if partial_error:
            message = f"{message}: {partial_error}"
        return AIInvocationResult(
            request=request,
            status="blocked",
            output_text=partial_output,
            error=message,
            command=command,
            returncode=124,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            provider_backed=True,
            calls_models=False,
            auth_status="cli_timeout",
            output_path=output_path,
        )
    except Exception as exc:
        return _failed_result(
            request,
            error=str(exc),
            command=command,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            auth_status="failed",
        )
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def invoke_ai(request: AIInvocationRequest) -> AIInvocationResult:
    backend = request.resolved_backend()
    if backend == BACKEND_FIXTURE:
        return invoke_fixture(request)
    if backend in {BACKEND_OPENAI_API, BACKEND_ANTHROPIC_API}:
        return invoke_api(request)
    if backend in {BACKEND_CODEX_CLI, BACKEND_CLAUDE_CLI}:
        return invoke_cli(request)
    if backend == BACKEND_DOCKER_LIVE_AI:
        return _failed_result(
            request,
            error="docker_live_ai backend is a governed external harness; use docker/hn-install-audit/run-install-audit.sh",
            command=["docker_live_ai"],
            elapsed_ms=0,
            auth_status="external_harness_required",
        )
    return _failed_result(
        request,
        error=f"unsupported AI invocation backend: {backend}",
        command=[],
        elapsed_ms=0,
        auth_status="unsupported_backend",
    )
