"""Data models for the governance service.

Structured dataclasses for Evidence, GateRequirement, MemoryEntry, etc.
All models are serializable to/from dict/JSON.
"""

import json
import uuid
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from .enums import (
    VerifyStatus, VerifyLevel, BuildStatus, Role,
    SessionStatus, GateMode, GatePolicy, EvidenceType, MemoryKind,
)


def _gen_id(prefix: str) -> str:
    ts = int(time.time() * 1000)
    short = uuid.uuid4().hex[:6]
    return f"{prefix}-{ts}-{short}"


def _utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Evidence ---

@dataclass
class Evidence:
    """Structured evidence object — traceable, signable."""
    type: str                    # EvidenceType value
    producer: str = ""           # session_id of creator
    tool: Optional[str] = None   # pytest | playwright | git | manual
    summary: dict = field(default_factory=dict)
    artifact_uri: Optional[str] = None
    checksum: Optional[str] = None
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = _utc_iso()

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d) -> "Evidence":
        if not d:
            return cls(type="unknown")
        if isinstance(d, str):
            from .errors import ValidationError
            raise ValidationError(
                'evidence must be a dict, not a string. '
                'Example: {"type": "test_report", "summary": {"passed": 162, "exit_code": 0}}'
            )
        if not isinstance(d, dict):
            from .errors import ValidationError
            raise ValidationError(
                f'evidence must be a dict, got {type(d).__name__}. '
                'Example: {"type": "test_report", "summary": {"passed": 162, "exit_code": 0}}'
            )
        return cls(
            type=d.get("type", "unknown"),
            producer=d.get("producer", ""),
            tool=d.get("tool"),
            summary=d.get("summary", {}),
            artifact_uri=d.get("artifact_uri"),
            checksum=d.get("checksum"),
            created_at=d.get("created_at", ""),
        )


# --- Gate Requirement ---

@dataclass
class GateRequirement:
    """A single gate's requirement — configurable policy."""
    node_id: str
    min_status: str = "qa_pass"    # VerifyStatus value
    policy: str = "default"        # GatePolicy value
    waived_by: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GateRequirement":
        return cls(
            node_id=d["node_id"],
            min_status=d.get("min_status", "qa_pass"),
            policy=d.get("policy", "default"),
            waived_by=d.get("waived_by"),
        )


# --- Node Definition (for graph) ---

@dataclass
class NodeDef:
    """Node definition in the acceptance graph (Layer 1 — rules)."""
    id: str
    title: str = ""
    layer: str = "L0"
    verify_level: int = 1          # VerifyLevel as int
    gate_mode: str = "auto"        # GateMode value
    test_coverage: str = "none"
    primary: list = field(default_factory=list)
    secondary: list = field(default_factory=list)
    test: list = field(default_factory=list)
    propagation: Optional[str] = None
    guard: bool = False
    version: str = ""
    gates: list = field(default_factory=list)  # list of GateRequirement dicts
    verify_requires: list = field(default_factory=list)  # list of node IDs that must be verified first (R4)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "NodeDef":
        return cls(
            id=d["id"],
            title=d.get("title", ""),
            layer=d.get("layer", "L0"),
            verify_level=d.get("verify_level", 1),
            gate_mode=d.get("gate_mode", "auto"),
            test_coverage=d.get("test_coverage", "none"),
            primary=d.get("primary", []),
            secondary=d.get("secondary", []),
            test=d.get("test", []),
            propagation=d.get("propagation"),
            guard=d.get("guard", False),
            version=d.get("version", ""),
            gates=d.get("gates", []),
            verify_requires=d.get("verify_requires", []),
        )


# --- Memory Entry ---

@dataclass
class MemoryEntry:
    """Development memory entry with lifecycle tracking."""
    id: str = ""
    module_id: str = ""
    kind: str = "pattern"         # MemoryKind value
    content: str = ""
    applies_when: str = ""        # Applicability condition
    supersedes: Optional[str] = None  # ID of memory this replaces
    related_nodes: list = field(default_factory=list)
    structured: dict = field(default_factory=dict)
    created_by: str = ""
    created_at: str = ""
    is_active: bool = True

    def __post_init__(self):
        if not self.id:
            self.id = _gen_id("mem")
        if not self.created_at:
            self.created_at = _utc_iso()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        return cls(
            id=d.get("id", ""),
            module_id=d.get("module_id", d.get("module", "")),
            kind=d.get("kind", d.get("category", "pattern")),
            content=d.get("content", ""),
            applies_when=d.get("applies_when", d.get("summary", "")),
            supersedes=d.get("supersedes"),
            related_nodes=d.get("related_nodes", []),
            structured=d.get("structured", {}),
            created_by=d.get("created_by", ""),
            created_at=d.get("created_at", ""),
            is_active=d.get("is_active", True),
        )


# --- Session ---

@dataclass
class Session:
    """Runtime session — binds a principal to a project+role."""
    session_id: str = ""
    principal_id: str = ""
    project_id: str = ""
    role: str = ""                # Role value
    scope: list = field(default_factory=list)
    token_hash: str = ""
    status: str = "active"        # SessionStatus value
    created_at: str = ""
    expires_at: str = ""
    last_heartbeat: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.session_id:
            self.session_id = _gen_id("ses")
        if not self.created_at:
            self.created_at = _utc_iso()

    def to_dict(self) -> dict:
        return asdict(self)


# --- Impact Analysis Request ---

@dataclass
class FileHitPolicy:
    match_primary: bool = True
    match_secondary: bool = False
    match_config_glob: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "FileHitPolicy":
        if not d:
            return cls()
        return cls(
            match_primary=d.get("match_primary", True),
            match_secondary=d.get("match_secondary", False),
            match_config_glob=d.get("match_config_glob", []),
        )


@dataclass
class PropagationPolicy:
    follow_deps: bool = True
    follow_reverse_deps: bool = False
    propagation_tag_filter: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "PropagationPolicy":
        if not d:
            return cls()
        return cls(
            follow_deps=d.get("follow_deps", True),
            follow_reverse_deps=d.get("follow_reverse_deps", False),
            propagation_tag_filter=d.get("propagation_tag_filter", []),
        )


@dataclass
class VerificationPolicy:
    mode: str = "targeted"        # smoke | targeted | full_regression
    skip_already_passed: bool = True
    respect_gates: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "VerificationPolicy":
        if not d:
            return cls()
        return cls(
            mode=d.get("mode", "targeted"),
            skip_already_passed=d.get("skip_already_passed", True),
            respect_gates=d.get("respect_gates", True),
        )


# --- Subtask (PM decomposition) ---

@dataclass
class Subtask:
    """A single subtask within a PM decomposition."""
    id: str = ""
    title: str = ""
    target_files: list = field(default_factory=list)
    verification: dict = field(default_factory=dict)
    acceptance_criteria: list = field(default_factory=list)
    test_files: list = field(default_factory=list)
    depends_on: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Subtask":
        if not d:
            return cls()
        return cls(
            id=d.get("id", ""),
            title=d.get("title", ""),
            target_files=d.get("target_files", []),
            verification=d.get("verification", {}),
            acceptance_criteria=d.get("acceptance_criteria", []),
            test_files=d.get("test_files", []),
            depends_on=d.get("depends_on", []),
        )


@dataclass
class SubtaskGroup:
    """A group of subtasks from PM decomposition."""
    group_id: str = ""
    project_id: str = ""
    pm_task_id: str = ""
    total_count: int = 0
    completed_count: int = 0
    status: str = "active"
    created_at: str = ""
    completed_at: str = ""
    trace_id: str = ""
    chain_id: str = ""

    def __post_init__(self):
        if not self.group_id:
            self.group_id = _gen_id("sg")
        if not self.created_at:
            self.created_at = _utc_iso()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SubtaskGroup":
        if not d:
            return cls()
        return cls(
            group_id=d.get("group_id", ""),
            project_id=d.get("project_id", ""),
            pm_task_id=d.get("pm_task_id", ""),
            total_count=d.get("total_count", 0),
            completed_count=d.get("completed_count", 0),
            status=d.get("status", "active"),
            created_at=d.get("created_at", ""),
            completed_at=d.get("completed_at", ""),
            trace_id=d.get("trace_id", ""),
            chain_id=d.get("chain_id", ""),
        )


@dataclass
class ImpactAnalysisRequest:
    changed_files: list = field(default_factory=list)
    file_policy: Optional[FileHitPolicy] = None
    propagation_policy: Optional[PropagationPolicy] = None
    verification_policy: Optional[VerificationPolicy] = None
