"""Trace artifact helpers for state-only reconcile runs."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

_REPORT_JSON_MAX_BYTES = 24 * 1024 * 1024
_SUMMARY_SAMPLE_LIMIT = 5


def utc_ms() -> int:
    return int(time.time() * 1000)


def sha256_file(path: str | Path) -> str:
    p = Path(path)
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def write_json(path: str | Path, payload: Any) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return str(p)


def artifact_ref(path: str | Path) -> dict[str, Any]:
    if not str(path or ""):
        return {"path": "", "exists": False, "size_bytes": 0, "sha256": ""}
    p = Path(path)
    out = {
        "path": str(p),
        "exists": p.exists(),
        "size_bytes": 0,
        "sha256": "",
    }
    if p.exists() and p.is_file():
        out["size_bytes"] = p.stat().st_size
        out["sha256"] = sha256_file(p)
    return out


def _short_signal(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, list):
        return [_short_signal(item) for item in value[:20]]
    if isinstance(value, Mapping):
        return {
            str(key): _short_signal(item)
            for key, item in list(value.items())[:20]
        }
    return str(value)[:240]


def _read_json_mapping(path: str | Path) -> Mapping[str, Any] | None:
    try:
        p = Path(path)
        if not p.exists() or not p.is_file() or p.stat().st_size > _REPORT_JSON_MAX_BYTES:
            return None
        loaded = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, Mapping) else None


def _phase_metric_summary(metrics: Any) -> dict[str, Any]:
    if not isinstance(metrics, Mapping):
        return {}
    out: dict[str, Any] = {}
    for raw_key, value in metrics.items():
        key = str(raw_key or "")
        key_l = key.lower()
        if (
            key_l.endswith("_count")
            or key_l in {"parallel", "parallelized", "used_parallel", "worker_count", "workers", "max_workers"}
            or "candidate" in key_l
            or "fallback" in key_l
        ):
            out[key] = _short_signal(value)
    return out


def _phase_timing_projection(value: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    steps = []
    for item in value.get("steps") or []:
        if not isinstance(item, Mapping):
            continue
        step = {
            "name": str(item.get("name") or ""),
            "elapsed_ms": int(item.get("elapsed_ms") or 0),
        }
        metrics = _phase_metric_summary(item.get("metrics"))
        if metrics:
            step["metrics"] = metrics
        steps.append(step)
    slow_phases = sorted(
        steps,
        key=lambda item: int(item.get("elapsed_ms") or 0),
        reverse=True,
    )[:_SUMMARY_SAMPLE_LIMIT]
    return {
        "source": source,
        "schema_version": str(value.get("schema_version") or ""),
        "run_id": str(value.get("run_id") or ""),
        "step_count": int(value.get("step_count") or len(steps)),
        "total_elapsed_ms": int(value.get("total_elapsed_ms") or sum(
            int(item.get("elapsed_ms") or 0) for item in steps
        )),
        "slow_phases": slow_phases,
    }


def _phase_timings_from_payload(value: Any, *, source: str, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 4 or not isinstance(value, Mapping):
        return []
    out: list[dict[str, Any]] = []
    phase_timing = value.get("phase_timing")
    if isinstance(phase_timing, Mapping):
        out.append(_phase_timing_projection(phase_timing, source=f"{source}.phase_timing"))
    phase_trace = value.get("phase_trace")
    if isinstance(phase_trace, Mapping):
        out.append(_phase_timing_projection(phase_trace, source=f"{source}.phase_trace"))
    for key in ("report_path", "phase_report_path"):
        artifact_path = value.get(key)
        if isinstance(artifact_path, (str, Path)):
            artifact = _read_json_mapping(artifact_path)
            if artifact:
                out.extend(_phase_timings_from_payload(artifact, source=f"{source}.{key}", depth=depth + 1))
    for raw_key, item in value.items():
        if len(out) >= _SUMMARY_SAMPLE_LIMIT:
            break
        key = str(raw_key or "")
        if isinstance(item, Mapping):
            out.extend(_phase_timings_from_payload(item, source=f"{source}.{key}", depth=depth + 1))
        elif isinstance(item, list) and len(item) <= 10:
            for index, child in enumerate(item):
                if isinstance(child, Mapping):
                    out.extend(_phase_timings_from_payload(child, source=f"{source}.{key}[{index}]", depth=depth + 1))
    return out[:_SUMMARY_SAMPLE_LIMIT]


def _collect_observability_signals(
    value: Any,
    *,
    fallback_reasons: set[str],
    candidate_counts: dict[str, Any],
    parallelization_hints: dict[str, Any],
    strategies: set[str],
    depth: int = 0,
) -> None:
    if depth > 4:
        return
    if isinstance(value, Mapping):
        for raw_key, raw_item in value.items():
            key = str(raw_key or "")
            item = raw_item
            key_l = key.lower()
            if key_l in {"fallback_reason", "fallback_reasons"}:
                if isinstance(item, (list, tuple, set)):
                    for reason in item:
                        reason_text = str(reason or "").strip()
                        if reason_text:
                            fallback_reasons.add(reason_text)
                elif not isinstance(item, Mapping):
                    reason_text = str(item or "").strip()
                    if reason_text:
                        fallback_reasons.add(reason_text)
            elif key_l in {"strategy", "scope_reconcile_strategy", "scope_graph_delta_mode", "mode"} and str(item or "").strip():
                strategies.add(str(item).strip())
            elif ("candidate" in key_l or key_l in {"feature_cluster_count", "node_count", "edge_count"}) and (
                key_l.endswith("_count")
                or key_l.endswith("count")
                or key_l in {"candidate_count", "candidate_counts"}
            ):
                candidate_counts[key] = _short_signal(item)
            elif "parallel" in key_l or key_l in {"worker_count", "workers", "max_workers", "process_count", "thread_count"}:
                parallelization_hints[key] = _short_signal(item)
            _collect_observability_signals(
                item,
                fallback_reasons=fallback_reasons,
                candidate_counts=candidate_counts,
                parallelization_hints=parallelization_hints,
                strategies=strategies,
                depth=depth + 1,
            )
    elif isinstance(value, list):
        for item in value[:50]:
            _collect_observability_signals(
                item,
                fallback_reasons=fallback_reasons,
                candidate_counts=candidate_counts,
                parallelization_hints=parallelization_hints,
                strategies=strategies,
                depth=depth + 1,
            )


def _payload_observability(payload: Mapping[str, Any]) -> dict[str, Any]:
    fallback_reasons: set[str] = set()
    candidate_counts: dict[str, Any] = {}
    parallelization_hints: dict[str, Any] = {}
    strategies: set[str] = set()
    _collect_observability_signals(
        payload,
        fallback_reasons=fallback_reasons,
        candidate_counts=candidate_counts,
        parallelization_hints=parallelization_hints,
        strategies=strategies,
    )
    out: dict[str, Any] = {}
    if strategies:
        out["strategies"] = sorted(strategies)
    if fallback_reasons:
        out["fallback_reasons"] = sorted(fallback_reasons)
    if candidate_counts:
        out["candidate_counts"] = candidate_counts
    if parallelization_hints:
        out["parallelization_hints"] = parallelization_hints
    return out


def _step_observability(
    *,
    input_payload: Mapping[str, Any],
    output_payload: Mapping[str, Any],
) -> dict[str, Any]:
    input_summary = _payload_observability(input_payload)
    output_summary = _payload_observability(output_payload)
    phase_timing = (
        _phase_timings_from_payload(input_payload, source="input")
        + _phase_timings_from_payload(output_payload, source="output")
    )
    out: dict[str, Any] = {
        "input": input_summary,
        "output": output_summary,
    }
    strategies = sorted(set(input_summary.get("strategies", [])) | set(output_summary.get("strategies", [])))
    fallback_reasons = sorted(
        set(input_summary.get("fallback_reasons", [])) | set(output_summary.get("fallback_reasons", []))
    )
    candidate_counts = {
        **(input_summary.get("candidate_counts") or {}),
        **(output_summary.get("candidate_counts") or {}),
    }
    parallelization_hints = {
        **(input_summary.get("parallelization_hints") or {}),
        **(output_summary.get("parallelization_hints") or {}),
    }
    if strategies:
        out["strategies"] = strategies
    if fallback_reasons:
        out["fallback_reasons"] = fallback_reasons
    if candidate_counts:
        out["candidate_counts"] = candidate_counts
    if parallelization_hints:
        out["parallelization_hints"] = parallelization_hints
    if phase_timing:
        out["phase_timing"] = phase_timing
    return out


def _summary_observability(steps: list[dict[str, Any]]) -> dict[str, Any]:
    fallback_reasons: dict[str, int] = {}
    candidate_counts: list[dict[str, Any]] = []
    parallelization_hints: list[dict[str, Any]] = []
    phase_timings: list[dict[str, Any]] = []
    slow_phases: list[dict[str, Any]] = []
    step_summaries: list[dict[str, Any]] = []
    for step in steps:
        observability = step.get("observability") if isinstance(step.get("observability"), Mapping) else {}
        for reason in observability.get("fallback_reasons") or []:
            key = str(reason or "").strip()
            if key:
                fallback_reasons[key] = fallback_reasons.get(key, 0) + 1
        if observability.get("candidate_counts"):
            candidate_counts.append({
                "step": step.get("name", ""),
                "index": step.get("index", 0),
                "counts": observability.get("candidate_counts"),
            })
        if observability.get("parallelization_hints"):
            parallelization_hints.append({
                "step": step.get("name", ""),
                "index": step.get("index", 0),
                "hints": observability.get("parallelization_hints"),
            })
        phase_timings.append({
            "index": step.get("index", 0),
            "name": step.get("name", ""),
            "status": step.get("status", ""),
            "elapsed_ms": step.get("elapsed_ms", 0),
            "strategies": observability.get("strategies", []),
            "fallback_reasons": observability.get("fallback_reasons", []),
        })
        step_summary = {
            "index": step.get("index", 0),
            "name": step.get("name", ""),
            "status": step.get("status", ""),
            "elapsed_ms": step.get("elapsed_ms", 0),
            "input": observability.get("input", {}),
            "output": observability.get("output", {}),
            "phase_timing": observability.get("phase_timing", []),
            "candidate_counts": observability.get("candidate_counts", {}),
            "fallback_reasons": observability.get("fallback_reasons", []),
            "parallelization_hints": observability.get("parallelization_hints", {}),
        }
        step_summaries.append(step_summary)
        for timing in observability.get("phase_timing") or []:
            if not isinstance(timing, Mapping):
                continue
            for phase in timing.get("slow_phases") or []:
                if isinstance(phase, Mapping):
                    slow_phases.append({
                        "step": step.get("name", ""),
                        "index": step.get("index", 0),
                        "source": timing.get("source", ""),
                        "phase": phase.get("name", ""),
                        "elapsed_ms": phase.get("elapsed_ms", 0),
                    })
    slow_steps = sorted(
        phase_timings,
        key=lambda item: int(item.get("elapsed_ms") or 0),
        reverse=True,
    )[:5]
    slow_phases = sorted(
        slow_phases,
        key=lambda item: int(item.get("elapsed_ms") or 0),
        reverse=True,
    )[:_SUMMARY_SAMPLE_LIMIT]
    return {
        "schema_version": "reconcile_trace.summary.v1",
        "steps": step_summaries,
        "phase_timings": phase_timings,
        "slow_steps": slow_steps,
        "slow_phases": slow_phases,
        "fallback_reasons": fallback_reasons,
        "candidate_counts": candidate_counts[:20],
        "parallelization_hints": parallelization_hints[:20],
    }


class ReconcileTrace:
    """Write per-step input/output JSON artifacts for observer audit."""

    def __init__(
        self,
        *,
        project_id: str,
        run_id: str,
        snapshot_id: str,
        trace_dir: str | Path,
    ) -> None:
        self.project_id = project_id
        self.run_id = run_id
        self.snapshot_id = snapshot_id
        self.trace_dir = Path(trace_dir)
        self.steps_dir = self.trace_dir / "steps"
        self.steps: list[dict[str, Any]] = []
        self._counter = 0
        self.started_ms = utc_ms()

    def step(
        self,
        name: str,
        *,
        input_payload: dict[str, Any] | None = None,
        output_payload: dict[str, Any] | None = None,
        status: str = "ok",
    ) -> dict[str, Any]:
        self._counter += 1
        slug = f"{self._counter:03d}-{name}"
        step_dir = self.steps_dir / slug
        started_ms = utc_ms()
        input_data = input_payload or {}
        output_data = output_payload or {}
        input_path = write_json(step_dir / "input.json", input_data)
        output_path = write_json(step_dir / "output.json", output_data)
        finished_ms = utc_ms()
        record = {
            "index": self._counter,
            "name": name,
            "status": status,
            "started_ms": started_ms,
            "finished_ms": finished_ms,
            "elapsed_ms": max(0, finished_ms - started_ms),
            "input": artifact_ref(input_path),
            "output": artifact_ref(output_path),
            "observability": _step_observability(
                input_payload=input_data,
                output_payload=output_data,
            ),
        }
        write_json(step_dir / "step.json", record)
        self.steps.append(record)
        self._write_summary(status="running")
        return record

    def _write_summary(self, *, status: str) -> dict[str, Any]:
        now = utc_ms()
        observability_summary = _summary_observability(self.steps)
        summary = {
            "schema_version": 1,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "status": status,
            "started_ms": self.started_ms,
            "updated_ms": now,
            "elapsed_ms": max(0, now - self.started_ms),
            "step_count": len(self.steps),
            "steps": self.steps,
            "observability": observability_summary,
            "summary": observability_summary,
        }
        write_json(self.trace_dir / "summary.json", summary)
        return summary

    def finalize(self, *, status: str = "ok", extra: dict[str, Any] | None = None) -> dict[str, Any]:
        summary = self._write_summary(status=status)
        if extra:
            summary.update(extra)
            write_json(self.trace_dir / "summary.json", summary)
        return summary


__all__ = [
    "ReconcileTrace",
    "artifact_ref",
    "sha256_file",
    "utc_ms",
    "write_json",
]
