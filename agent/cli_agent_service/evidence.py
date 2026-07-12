"""Public-safe execution receipts for supervised CLI agent runs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


EXECUTION_RECEIPT_SCHEMA_VERSION = "cli_agent_service.execution_receipt.v1"


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def hash_text(value: str) -> str:
    return sha256_bytes(str(value).encode("utf-8"))


def hash_command(command: Sequence[str]) -> str:
    payload = json.dumps(list(command), separators=(",", ":"), ensure_ascii=True)
    return hash_text(payload)


def hash_file(path: str | Path) -> str:
    try:
        value = Path(path).read_bytes()
    except OSError:
        value = b""
    return sha256_bytes(value)


@dataclass(frozen=True)
class ExecutionReceipt:
    run_id: str
    status: str
    exit_code: int
    pid: int
    process_group_id: int
    command_hash: str
    prompt_hash: str
    output_hash: str
    stdout_hash: str
    stderr_hash: str
    started_at: str
    finished_at: str
    failure_category: str = ""
    schema_version: str = EXECUTION_RECEIPT_SCHEMA_VERSION

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "pid": self.pid,
            "process_group_id": self.process_group_id,
            "command_hash": self.command_hash,
            "prompt_hash": self.prompt_hash,
            "output_hash": self.output_hash,
            "stdout_hash": self.stdout_hash,
            "stderr_hash": self.stderr_hash,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "failure_category": self.failure_category,
            "raw_prompt_stored": False,
            "raw_output_stored": False,
            "operational_state_only": True,
            "governance_authority": False,
        }
