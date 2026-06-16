"""Harness-aware worker transcript self-attestation checks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence


WORKER_TRANSCRIPT_ATTESTATION_SCHEMA_VERSION = "worker_transcript_self_attestation.v1"
SUPPORTED_HARNESS_TYPES = {"claude", "codex"}
HARNESS_TYPE_ALIASES = {
    "codex_builtin_subagent": "codex",
    "codex_built_in_subagent": "codex",
    "codex_builtin": "codex",
    "codex_cli": "codex",
    "claude_code": "claude",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_harness_type(value: Any) -> str:
    normalized = "_".join(_text(value).lower().replace("-", "_").split())
    return HARNESS_TYPE_ALIASES.get(normalized, normalized)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Mapping):
        values: list[str] = []
        for key in (
            "trace_ids",
            "graph_trace_ids",
            "graph_query_trace_ids",
            "changed_files",
            "owned_files",
            "files",
        ):
            values.extend(_string_list(value.get(key)))
        return _dedupe(values)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return _dedupe(_text(item) for item in value if _text(item))
    return []


def _dedupe(values: Sequence[str] | Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _flatten_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except TypeError:
        return str(value)


def _read_jsonl(path: Path) -> tuple[list[Mapping[str, Any]], str, list[str]]:
    events: list[Mapping[str, Any]] = []
    lines: list[str] = []
    errors: list[str] = []
    try:
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [], "", [f"transcript_read_failed:{exc}"]
    for lineno, line in enumerate(raw_lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        lines.append(stripped)
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            errors.append(f"jsonl_parse_error:{lineno}")
            continue
        if isinstance(parsed, Mapping):
            events.append(parsed)
    flattened = "\n".join([*lines, *(_flatten_json(event) for event in events)])
    return events, flattened, errors


def _safe_meta(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _select_claude_jsonl(path: Path, worker_session_id: str) -> tuple[Path | None, dict[str, Any]]:
    if path.is_file():
        return path, _safe_meta(path.parent / "meta.json")
    meta = _safe_meta(path / "meta.json")
    preferred_names = [
        f"agent-{worker_session_id}.jsonl",
        f"{worker_session_id}.jsonl",
    ]
    for name in preferred_names:
        candidate = path / name
        if candidate.exists():
            return candidate, meta
    matches = sorted(path.glob("agent-*.jsonl")) or sorted(path.glob("*.jsonl"))
    return (matches[0], meta) if matches else (None, meta)


def _select_codex_jsonl(path: Path, worker_session_id: str) -> tuple[Path | None, dict[str, Any]]:
    if path.is_file():
        return path, _safe_meta(path.parent / "meta.json")
    candidates = sorted(path.glob("**/*.jsonl"))
    if worker_session_id:
        for candidate in candidates:
            if worker_session_id in candidate.name or worker_session_id in str(candidate):
                return candidate, _safe_meta(candidate.parent / "meta.json")
    for prefix in ("session", "rollout"):
        for candidate in candidates:
            if candidate.name.startswith(prefix):
                return candidate, _safe_meta(candidate.parent / "meta.json")
    return (candidates[0], _safe_meta(candidates[0].parent / "meta.json")) if candidates else (None, {})


def _load_transcript(
    *,
    worker_session_id: str,
    worker_transcript_path: str,
    harness_type: str,
) -> dict[str, Any]:
    path = Path(worker_transcript_path).expanduser()
    if not path.exists():
        return {
            "ok": False,
            "blockers": ["worker_transcript_path_unresolvable"],
            "resolved_path": str(path),
            "events": [],
            "text": "",
            "meta": {},
        }
    if harness_type == "claude":
        jsonl, meta = _select_claude_jsonl(path, worker_session_id)
    elif harness_type == "codex":
        jsonl, meta = _select_codex_jsonl(path, worker_session_id)
    else:
        jsonl, meta = None, {}
    if jsonl is None or not jsonl.exists():
        return {
            "ok": False,
            "blockers": ["worker_transcript_jsonl_not_found"],
            "resolved_path": str(path),
            "events": [],
            "text": "",
            "meta": meta,
        }
    events, text, parse_errors = _read_jsonl(jsonl)
    meta_text = _flatten_json(meta) if meta else ""
    combined_text = "\n".join(part for part in (text, meta_text) if part)
    return {
        "ok": True,
        "blockers": parse_errors,
        "resolved_path": str(jsonl),
        "events": events,
        "text": combined_text,
        "meta": meta,
        "line_count": len(events),
    }


def _payload_graph_trace_ids(payload: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "graph_trace_ids",
        "graph_query_trace_ids",
        "trace_ids",
        "graph_trace_id",
        "trace_id",
    ):
        values.extend(_string_list(payload.get(key)))
    for nested_key in ("graph_trace_evidence", "evidence", "timeline_facts"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            values.extend(_payload_graph_trace_ids(nested))
    return _dedupe(values)


def _payload_changed_files(payload: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "changed_files",
        "owned_files_diff",
        "owned_changed_files",
        "worker_changed_files",
        "diff_files",
    ):
        values.extend(_string_list(payload.get(key)))
    nested = payload.get("finish_precheck")
    if isinstance(nested, Mapping):
        values.extend(_string_list(nested.get("claimed_files")))
    return _dedupe(values)


def _git_diff_truth(
    worktree_path: str,
    base_commit: str,
    head_commit: str = "",
) -> dict[str, Any]:
    worktree = Path(worktree_path)
    if not worktree.exists():
        return {
            "changed_files": [],
            "blockers": ["worktree_path_unresolvable"],
            "worktree_path": str(worktree),
            "base_commit": base_commit,
            "head_commit": head_commit,
        }
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is None or proc.returncode != 0:
        return {
            "changed_files": [],
            "blockers": ["git_worktree_unavailable"],
            "worktree_path": str(worktree),
            "base_commit": base_commit,
            "head_commit": head_commit,
        }
    base = _text(base_commit)
    head = _text(head_commit) or "HEAD"
    if not base:
        return {
            "changed_files": [],
            "blockers": ["missing_base_commit_for_git_diff"],
            "worktree_path": str(worktree),
            "base_commit": base,
            "head_commit": head,
        }
    command = ["git", "-C", str(worktree), "diff", "--name-only", f"{base}..{head}"]
    changed: list[str] = []
    blockers: list[str] = []
    try:
        diff = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "changed_files": [],
            "blockers": [f"git_diff_failed:{exc}"],
            "worktree_path": str(worktree),
            "base_commit": base,
            "head_commit": head,
        }
    if diff.returncode == 0:
        changed.extend(line.strip() for line in diff.stdout.splitlines() if line.strip())
    else:
        blockers.append("git_diff_failed")
    return {
        "changed_files": _dedupe(changed),
        "blockers": blockers,
        "worktree_path": str(worktree),
        "base_commit": base,
        "head_commit": head,
    }


def _graph_trace_db_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in (
        "graph_trace_db_evidence",
        "db_graph_trace_evidence",
        "verified_graph_trace_evidence",
        "graph_trace_evidence",
    ):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _contains_all(haystack: str, needles: Sequence[str]) -> list[str]:
    return [needle for needle in needles if needle and needle not in haystack]


_KNOWN_BAD_4178_STRUCTURED_KEYS = {
    "actor",
    "agent_id",
    "event_id",
    "event_kind",
    "event_ref",
    "filer_principal",
    "filed_on_behalf_by",
    "host_session_id",
    "host_startup_id",
    "id",
    "known_bad_playback_4178",
    "on_behalf_of",
    "parent_event_id",
    "playback_source",
    "read_receipt_event_id",
    "scenario",
    "scenario_id",
    "source",
    "source_event_id",
    "startup_source",
    "timeline_event_id",
    "worker_session_id",
}

_KNOWN_BAD_4178_MARKERS = (
    "codex-cli-thread:event-4178",
    "codex-multi-agent-4178",
    "event-4178",
    "multi_agent_v1:4178",
)


def _structured_known_bad_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key or "").strip().lower()
            if normalized_key in _KNOWN_BAD_4178_STRUCTURED_KEYS:
                if isinstance(item, bool):
                    if item and normalized_key == "known_bad_playback_4178":
                        values.append("known_bad_playback_4178:true")
                elif not isinstance(item, (Mapping, list, tuple, set)):
                    token = _text(item)
                    if token:
                        values.append(f"{normalized_key}:{token}")
                else:
                    values.extend(_structured_known_bad_values(item))
            elif isinstance(item, Mapping):
                values.extend(_structured_known_bad_values(item))
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                for child in item:
                    values.extend(_structured_known_bad_values(child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            values.extend(_structured_known_bad_values(item))
    return _dedupe(values)


def _structured_known_bad_playback_4178(
    payload: Mapping[str, Any],
    transcript_events: Sequence[Mapping[str, Any]],
) -> tuple[bool, list[str]]:
    """Detect known-bad 4178 replay only from structured identity evidence.

    Full transcript text may contain prompts, tests, or explanatory QA notes that
    reference 4178 as a regression example. Those are not startup identity.
    """

    structured_values = [
        *_structured_known_bad_values(payload),
        *_structured_known_bad_values(list(transcript_events)),
    ]
    matches: list[str] = []
    for value in structured_values:
        lower_value = value.lower()
        if lower_value == "known_bad_playback_4178:true":
            matches.append(value)
            continue
        if lower_value.endswith(":4178") and any(
            lower_value.startswith(prefix)
            for prefix in (
                "event_id:",
                "event_ref:",
                "parent_event_id:",
                "read_receipt_event_id:",
                "source_event_id:",
                "timeline_event_id:",
            )
        ):
            matches.append(value)
            continue
        if any(marker in lower_value for marker in _KNOWN_BAD_4178_MARKERS):
            matches.append(value)
    return bool(matches), _dedupe(matches)


def _layer(layer_id: str, blockers: Sequence[str], **facts: Any) -> dict[str, Any]:
    return {
        "id": layer_id,
        "status": "blocked" if blockers else "passed",
        "blockers": list(blockers),
        **facts,
    }


def verify_worker_transcript(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Verify that startup evidence is independently backed by worker transcript.

    The verifier is deliberately conservative: a transcript path by itself never
    attests a worker. The transcript must line up with runtime identity, owned
    diff scope, graph-query trace ids, and read-receipt/timeline facts.
    """

    worker_session_id = _text(
        payload.get("worker_session_id")
        or payload.get("session_id")
        or payload.get("host_session_id")
    )
    worker_transcript_path = _text(
        payload.get("worker_transcript_path") or payload.get("transcript_path")
    )
    worker_transcript_ref = _text(
        payload.get("worker_transcript_ref")
        or payload.get("transcript_ref")
        or payload.get("worker_transcript_uri")
        or payload.get("transcript_uri")
    )
    harness_type = _normalized_harness_type(
        payload.get("harness_type") or payload.get("worker_harness_type")
    )
    ref_only_transcript = bool(worker_transcript_ref and not worker_transcript_path)
    attestation_phase = _text(
        payload.get("attestation_phase") or payload.get("worker_attestation_phase")
    ).lower()
    if attestation_phase not in {"startup", "finish"}:
        attestation_phase = "finish"
    layers: list[dict[str, Any]] = []
    required_blockers: list[str] = []
    if not worker_session_id:
        required_blockers.append("missing_worker_session_id")
    if not (worker_transcript_path or worker_transcript_ref):
        required_blockers.append("missing_worker_transcript_ref_or_path")
    if harness_type not in SUPPORTED_HARNESS_TYPES:
        required_blockers.append("unsupported_or_missing_harness_type")
    layers.append(
        _layer(
            "required_fields",
            required_blockers,
            worker_session_id=worker_session_id,
            worker_transcript_path=worker_transcript_path,
            worker_transcript_ref=worker_transcript_ref,
            transcript_ref_only=ref_only_transcript,
            harness_type=harness_type,
            attestation_phase=attestation_phase,
        )
    )

    loaded = (
        _load_transcript(
            worker_session_id=worker_session_id,
            worker_transcript_path=worker_transcript_path,
            harness_type=harness_type,
        )
        if not required_blockers and worker_transcript_path
        else {"ok": False, "blockers": [], "text": "", "resolved_path": "", "events": [], "meta": {}}
    )
    transcript_text = str(loaded.get("text") or "")
    load_blockers = list(loaded.get("blockers") or [])
    if not loaded.get("ok") and not required_blockers and not ref_only_transcript:
        load_blockers.append("worker_transcript_unreadable")
    if worker_session_id and transcript_text and worker_session_id not in transcript_text:
        load_blockers.append("worker_session_id_not_in_transcript")
    layers.append(
        _layer(
            "transcript_resolution",
            load_blockers,
            resolved_path=loaded.get("resolved_path", ""),
            line_count=loaded.get("line_count", 0),
        )
    )

    runtime_fields = {
        "task_id": _text(payload.get("task_id")),
        "runtime_context_id": _text(payload.get("runtime_context_id")),
        "fence_token": _text(payload.get("fence_token")),
        "worktree_path": _text(
            payload.get("worktree_path")
            or payload.get("assigned_worktree")
            or payload.get("actual_git_root")
            or payload.get("actual_cwd")
        ),
        "branch_ref": _text(payload.get("branch_ref") or payload.get("branch")),
    }
    runtime_needles = [value for value in runtime_fields.values() if value]
    runtime_missing = (
        []
        if ref_only_transcript
        else _contains_all(transcript_text, runtime_needles)
        if transcript_text
        else runtime_needles
    )
    layers.append(
        _layer(
            "runtime_lane_match",
            [f"runtime_fact_missing:{value}" for value in runtime_missing],
            runtime_fields=runtime_fields,
        )
    )

    owned_files = _string_list(payload.get("owned_files") or payload.get("target_files"))
    claimed_changed_files = _payload_changed_files(payload)
    git_diff = _git_diff_truth(
        runtime_fields["worktree_path"],
        _text(payload.get("base_commit")),
        _text(payload.get("head_commit") or payload.get("branch_head")),
    )
    changed_files = _string_list(git_diff.get("changed_files"))
    diff_blockers: list[str] = []
    diff_blockers.extend(str(item) for item in git_diff.get("blockers") or [])
    if not changed_files:
        diff_blockers.append("no_owned_files_diff")
    if claimed_changed_files and set(claimed_changed_files) != set(changed_files):
        missing = sorted(set(changed_files).difference(claimed_changed_files))
        extra = sorted(set(claimed_changed_files).difference(changed_files))
        diff_blockers.append(
            "claimed_changed_files_do_not_match_git_diff"
            f":missing={','.join(missing)};extra={','.join(extra)}"
        )
    if owned_files:
        outside = sorted(set(changed_files).difference(owned_files))
        if outside:
            diff_blockers.append("changed_files_outside_owned_scope:" + ",".join(outside))
    changed_missing = (
        []
        if ref_only_transcript
        else _contains_all(transcript_text, changed_files)
        if transcript_text
        else changed_files
    )
    diff_blockers.extend(f"changed_file_missing_from_transcript:{path}" for path in changed_missing)
    layers.append(
        _layer(
            "owned_files_diff",
            diff_blockers,
            owned_files=owned_files,
            changed_files=changed_files,
            claimed_changed_files=claimed_changed_files,
            git_diff=git_diff,
        )
    )

    graph_db_evidence = _graph_trace_db_evidence(payload)
    graph_trace_ids = _string_list(
        graph_db_evidence.get("verified_trace_ids")
        or graph_db_evidence.get("trace_ids")
    )
    payload_graph_trace_ids = _payload_graph_trace_ids(payload)
    graph_blockers: list[str] = []
    if not graph_db_evidence:
        graph_blockers.append("missing_graph_trace_db_evidence")
    elif not bool(graph_db_evidence.get("db_verified")):
        graph_blockers.append("graph_trace_ids_not_db_verified")
    missing_db_traces = _string_list(graph_db_evidence.get("missing_trace_ids"))
    graph_blockers.extend(
        f"graph_trace_missing_from_db:{trace_id}" for trace_id in missing_db_traces
    )
    mismatches = graph_db_evidence.get("identity_mismatches")
    if isinstance(mismatches, Sequence) and not isinstance(mismatches, (str, bytes, bytearray)):
        graph_blockers.extend("graph_trace_identity_mismatch" for _ in mismatches)
    if not graph_trace_ids:
        graph_blockers.append("missing_mf_subagent_graph_trace_ids")
    if payload_graph_trace_ids and set(payload_graph_trace_ids) != set(graph_trace_ids):
        missing = sorted(set(graph_trace_ids).difference(payload_graph_trace_ids))
        extra = sorted(set(payload_graph_trace_ids).difference(graph_trace_ids))
        graph_blockers.append(
            "claimed_graph_trace_ids_do_not_match_db"
            f":missing={','.join(missing)};extra={','.join(extra)}"
        )
    graph_missing = (
        []
        if ref_only_transcript
        else _contains_all(transcript_text, graph_trace_ids)
        if transcript_text
        else graph_trace_ids
    )
    graph_blockers.extend(f"graph_trace_missing_from_transcript:{trace_id}" for trace_id in graph_missing)
    graph_marker = transcript_text.lower()
    if (
        graph_trace_ids
        and not ref_only_transcript
        and not ("mf_subagent" in graph_marker and ("graph_query" in graph_marker or "gqt-" in graph_marker))
    ):
        graph_blockers.append("transcript_missing_mf_subagent_graph_query_marker")
    layers.append(
        _layer(
            "graph_query_trace",
            graph_blockers,
            graph_trace_ids=graph_trace_ids,
            payload_graph_trace_ids=payload_graph_trace_ids,
            graph_trace_db_evidence=graph_db_evidence,
        )
    )

    timeline_values = _dedupe(
        value
        for value in (
            _text(payload.get("observer_command_id")),
            _text(payload.get("read_receipt_hash")),
            _text(payload.get("read_receipt_event_id")),
            _text(payload.get("route_token_ref")),
        )
        if value
    )
    timeline_blockers: list[str] = []
    if not _text(payload.get("observer_command_id")):
        timeline_blockers.append("missing_observer_command_id")
    if not (_text(payload.get("read_receipt_hash")) and _text(payload.get("read_receipt_event_id"))):
        timeline_blockers.append("missing_read_receipt_lineage")
    timeline_missing = (
        []
        if ref_only_transcript
        else _contains_all(transcript_text, timeline_values)
        if transcript_text
        else timeline_values
    )
    timeline_blockers.extend(f"timeline_fact_missing_from_transcript:{value}" for value in timeline_missing)
    layers.append(_layer("timeline_facts", timeline_blockers, timeline_values=timeline_values))

    filer_principal = _text(
        payload.get("filer_principal")
        or payload.get("actor")
        or payload.get("agent_id")
        or worker_session_id
    )
    on_behalf = bool(
        payload.get("filed_on_behalf")
        or payload.get("on_behalf")
        or _text(payload.get("on_behalf_of"))
        or filer_principal in {"observer", "mf_sub", "host-adapter", "host_adapter"}
    )
    playback_4178, playback_4178_evidence = _structured_known_bad_playback_4178(
        payload,
        list(loaded.get("events") or []),
    )
    principal_blockers: list[str] = []
    if on_behalf:
        principal_blockers.append("observer_or_generic_on_behalf_filer")
    if _text(payload.get("agent_id_match_mode")).lower() == "host_adapter_startup_token_surrogate":
        principal_blockers.append("host_adapter_startup_token_surrogate")
    if _text(payload.get("session_token_evidence_type")).lower() in {
        "surrogate",
        "claimed_unverified",
    }:
        principal_blockers.append("non_real_session_token_evidence")
    if bool(payload.get("host_adapter_startup_token_accepted")):
        principal_blockers.append("host_adapter_startup_token_accepted")
    if playback_4178:
        principal_blockers.append("known_bad_playback_4178_shape")
    layers.append(
        _layer(
            "filer_principal",
            principal_blockers,
            filer_principal=filer_principal,
            filed_on_behalf=on_behalf,
            known_bad_playback_4178=playback_4178,
            known_bad_playback_4178_evidence=playback_4178_evidence,
        )
    )

    all_blockers = [
        blocker
        for layer in layers
        for blocker in layer.get("blockers", [])
    ]
    if attestation_phase == "startup":
        blockers = [
            blocker
            for layer in layers
            if layer.get("id") != "owned_files_diff"
            for blocker in layer.get("blockers", [])
        ]
    else:
        blockers = list(all_blockers)
    finish_time_blockers = list(all_blockers)
    passed = not blockers
    return {
        "schema_version": WORKER_TRANSCRIPT_ATTESTATION_SCHEMA_VERSION,
        "attestation_phase": attestation_phase,
        "status": "passed" if passed else "blocked",
        "ok": passed,
        "worker_self_attesting": passed,
        "self_attesting": passed,
        "finish_time_self_attesting": not finish_time_blockers,
        "finish_time_blockers": finish_time_blockers,
        "worker_session_id": worker_session_id,
        "worker_transcript_path": worker_transcript_path,
        "worker_transcript_ref": worker_transcript_ref,
        "resolved_transcript_path": loaded.get("resolved_path", ""),
        "harness_type": harness_type,
        "layers": layers,
        "blockers": blockers,
        "known_bad_playback_4178": playback_4178,
    }
