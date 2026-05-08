"""Trace artifact helpers for state-only reconcile runs."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


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
        input_path = write_json(step_dir / "input.json", input_payload or {})
        output_path = write_json(step_dir / "output.json", output_payload or {})
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
        }
        write_json(step_dir / "step.json", record)
        self.steps.append(record)
        self._write_summary(status="running")
        return record

    def _write_summary(self, *, status: str) -> dict[str, Any]:
        now = utc_ms()
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
