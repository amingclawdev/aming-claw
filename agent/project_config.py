"""
project_config.py — Multi-project configuration loader.

Any project can register with the auto-chain workflow by providing a
.aming-claw.yaml (or .aming-claw.json) file at its workspace root.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirrors other agent modules)
# ---------------------------------------------------------------------------
_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class E2ETriggerConfig:
    paths: List[str] = field(default_factory=list)
    """Path globs or prefixes that make a suite relevant."""
    nodes: List[str] = field(default_factory=list)
    """Graph node ids or stable node titles that make a suite relevant."""
    tags: List[str] = field(default_factory=list)
    """Free-form feature tags for dashboard grouping."""


@dataclass
class E2ESuiteConfig:
    suite_id: str = ""
    label: str = ""
    command: str = ""
    trigger: E2ETriggerConfig = field(default_factory=E2ETriggerConfig)
    auto_run: bool = False
    live_ai: bool = False
    mutates_db: bool = True
    requires_human_approval: bool = False
    isolation_project: str = ""
    timeout_sec: int = 900
    max_parallel: int = 1


@dataclass
class E2EConfig:
    auto_run: bool = False
    default_timeout_sec: int = 900
    max_parallel: int = 1
    require_clean_worktree: bool = True
    evidence_retention_days: int = 14
    suites: Dict[str, E2ESuiteConfig] = field(default_factory=dict)


@dataclass
class TestingConfig:
    unit_command: str = "python -m pytest"
    e2e_command: str = ""
    allowed_commands: List[Dict[str, Any]] = field(default_factory=list)
    """Each entry: {"executable": str, "args_prefixes": list[str]}"""
    e2e: E2EConfig = field(default_factory=E2EConfig)


@dataclass
class BuildConfig:
    command: str = ""
    release_checks: List[str] = field(default_factory=list)


@dataclass
class ServiceRule:
    patterns: List[str] = field(default_factory=list)
    """Glob patterns (forward-slash normalised) that trigger this service."""
    services: List[str] = field(default_factory=list)
    """Service names that should be restarted / reloaded."""


@dataclass
class SmokeCheck:
    name: str = ""
    url: str = ""
    expected_status: int = 200
    timeout: int = 10


@dataclass
class DeployConfig:
    strategy: str = "none"
    """One of: docker | electron | systemd | process | none"""
    service_rules: List[ServiceRule] = field(default_factory=list)
    commands: Dict[str, str] = field(default_factory=dict)
    smoke_checks: List[SmokeCheck] = field(default_factory=list)


@dataclass
class GovernanceConfig:
    enabled: bool = False
    test_tool_label: str = "pytest"
    exclude_roots: List[str] = field(default_factory=list)
    """Workspace-relative directories/path prefixes excluded from governance scans."""


@dataclass
class NestedProjectsConfig:
    mode: str = "exclude"
    """How nested projects are treated by the parent graph. MVP: exclude."""
    roots: List[str] = field(default_factory=list)


@dataclass
class GraphConfig:
    exclude_paths: List[str] = field(default_factory=list)
    """Workspace-relative directories/path prefixes excluded from graph scans."""
    ignore_globs: List[str] = field(default_factory=list)
    """Glob patterns excluded from graph scans."""
    nested_projects: NestedProjectsConfig = field(default_factory=NestedProjectsConfig)


@dataclass
class AiConfig:
    routing: Dict[str, Dict[str, str]] = field(default_factory=dict)
    """Role -> {provider, model}; roles include pm/dev/tester/qa/semantic."""


@dataclass
class ProjectConfig:
    project_id: str = ""
    language: str = "python"
    testing: TestingConfig = field(default_factory=TestingConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    deploy: DeployConfig = field(default_factory=DeployConfig)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    ai: AiConfig = field(default_factory=AiConfig)


# ---------------------------------------------------------------------------
# DEFAULT_CONFIG — aming-claw hardcoded fallback
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = ProjectConfig(
    project_id="aming-claw",
    language="python",
    testing=TestingConfig(
        unit_command="python -m unittest discover -s agent/tests -p 'test_*.py' -v",
        e2e_command="",
        allowed_commands=[
            {"executable": "python", "args_prefixes": ["-m unittest", "-m pytest"]},
            {"executable": "pytest", "args_prefixes": []},
        ],
    ),
    build=BuildConfig(
        command="",
        release_checks=[],
    ),
    deploy=DeployConfig(
        strategy="docker",
        service_rules=[
            ServiceRule(
                patterns=["agent/telegram_gateway/**"],
                services=["gateway"],
            ),
        ],
        commands={
            "restart_gateway": "docker compose restart telegram-gateway",
            "logs_gateway": "docker compose logs --tail 30 telegram-gateway",
        },
        smoke_checks=[
            SmokeCheck(
                name="executor-api",
                url="http://localhost:40100/status",
                expected_status=200,
                timeout=5,
            ),
            SmokeCheck(
                name="governance",
                url="http://localhost:40000/api/health",
                expected_status=200,
                timeout=5,
            ),
            SmokeCheck(
                name="container-running",
                url="",
                expected_status=0,
                timeout=5,
            ),
        ],
    ),
    governance=GovernanceConfig(
        enabled=True,
        test_tool_label="pytest",
        exclude_roots=[],
    ),
    graph=GraphConfig(
        exclude_paths=["examples", "docs/dev", ".worktrees", ".claude/worktrees"],
        nested_projects=NestedProjectsConfig(mode="exclude", roots=[]),
    ),
)
"""Hardcoded fallback for the aming-claw project itself.

Used ONLY when no .aming-claw.yaml / .aming-claw.json is found at the
workspace root.  A deprecation warning is emitted whenever this fallback
is active so projects are encouraged to migrate to an explicit config file.
"""

# ---------------------------------------------------------------------------
# Command-safety helpers
# ---------------------------------------------------------------------------

_SHELL_METACHARACTERS = (";", "&&", "|", "`")


def validate_commands(config: ProjectConfig) -> List[str]:
    """Return a list of violation messages for unsafe shell metacharacters.

    Checks all command strings in testing, build, and deploy sections.
    """
    violations: List[str] = []

    def _check(label: str, value: str) -> None:
        for meta in _SHELL_METACHARACTERS:
            if meta in value:
                violations.append(
                    f"Command '{label}' contains unsafe metacharacter '{meta}': {value!r}"
                )

    _check("testing.unit_command", config.testing.unit_command)
    _check("testing.e2e_command", config.testing.e2e_command)
    for suite_id, suite in config.testing.e2e.suites.items():
        _check(f"testing.e2e.suites.{suite_id}.command", suite.command)
    _check("build.command", config.build.command)
    for key, cmd in config.deploy.commands.items():
        _check(f"deploy.commands.{key}", cmd)

    return violations


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("project_id", "language")
_KNOWN_TOP_LEVEL = {
    "version",
    "project_id",
    "name",
    "workspace_path",
    "language",
    "testing",
    "build",
    "deploy",
    "governance",
    "graph",
    "ai",
}
_VALID_STRATEGIES = {"docker", "electron", "systemd", "process", "none"}
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_project_config(raw: dict) -> Tuple[bool, List[str]]:
    """Validate raw config dict.

    Returns (is_valid, messages) where messages may include errors and
    warnings.  is_valid is False if any error is present.
    """
    errors: List[str] = []
    warnings: List[str] = []

    # Required fields
    for f in _REQUIRED_FIELDS:
        if f not in raw:
            errors.append(f"Missing required field: '{f}'")

    # kebab-case project_id
    pid = raw.get("project_id", "")
    if pid and not _KEBAB_RE.match(pid):
        errors.append(
            f"project_id must be kebab-case (lowercase letters, digits, hyphens): got {pid!r}"
        )

    # Deploy strategy
    deploy = raw.get("deploy", {})
    if isinstance(deploy, dict):
        strategy = deploy.get("strategy", "none")
        if strategy not in _VALID_STRATEGIES:
            errors.append(
                f"deploy.strategy must be one of {sorted(_VALID_STRATEGIES)}: got {strategy!r}"
            )

    # Unknown top-level fields → warnings
    for key in raw:
        if key not in _KNOWN_TOP_LEVEL:
            warnings.append(f"Unknown top-level field (ignored): '{key}'")

    # Shell metacharacter safety — build a temporary config for checking
    if not errors:
        try:
            tmp = _parse_raw(raw)
            cmd_violations = validate_commands(tmp)
            errors.extend(cmd_violations)
        except Exception as exc:
            warnings.append(f"Could not run command-safety check: {exc}")

    messages = errors + warnings
    return (len(errors) == 0, messages)


# ---------------------------------------------------------------------------
# YAML / JSON parsing
# ---------------------------------------------------------------------------


def _try_load_yaml(path: Path) -> dict:
    """Load YAML file; fall back to stdlib json if pyyaml is unavailable."""
    try:
        import yaml  # type: ignore[import]

        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except ImportError:
        logger.debug("pyyaml not available; falling back to json for %s", path)
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)


def _try_load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_yaml(path: Path, raw: dict) -> None:
    """Persist YAML using PyYAML when available.

    The dashboard only edits structured config blocks. Full comment-preserving
    round-tripping would need ruamel.yaml, which is intentionally not a runtime
    dependency here.
    """
    try:
        import yaml  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError("pyyaml is required to update .aming-claw.yaml") from exc
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True)


def _write_json(path: Path, raw: dict) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(raw, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Raw dict → dataclass conversion
# ---------------------------------------------------------------------------


def _parse_raw(raw: dict) -> ProjectConfig:
    """Convert a validated raw dict to a ProjectConfig, merging with defaults."""

    # ---- testing ----
    t_raw = raw.get("testing", {})
    testing = TestingConfig(
        unit_command=t_raw.get("unit_command", DEFAULT_CONFIG.testing.unit_command),
        e2e_command=t_raw.get("e2e_command", DEFAULT_CONFIG.testing.e2e_command),
        allowed_commands=t_raw.get("allowed_commands", []),
        e2e=_parse_e2e_config(t_raw.get("e2e", {}) if isinstance(t_raw, dict) else {}),
    )

    # ---- build ----
    b_raw = raw.get("build", {})
    build = BuildConfig(
        command=b_raw.get("command", ""),
        release_checks=b_raw.get("release_checks", []),
    )

    # ---- deploy ----
    d_raw = raw.get("deploy", {})
    service_rules: List[ServiceRule] = []
    for sr in d_raw.get("service_rules", []):
        patterns = [p.replace("\\", "/") for p in sr.get("patterns", [])]
        service_rules.append(
            ServiceRule(patterns=patterns, services=sr.get("services", []))
        )

    smoke_checks: List[SmokeCheck] = []
    for sc in d_raw.get("smoke_checks", []):
        smoke_checks.append(
            SmokeCheck(
                name=sc.get("name", ""),
                url=sc.get("url", ""),
                expected_status=sc.get("expected_status", 200),
                timeout=sc.get("timeout", 10),
            )
        )

    deploy = DeployConfig(
        strategy=d_raw.get("strategy", "none"),
        service_rules=service_rules,
        commands=d_raw.get("commands", {}),
        smoke_checks=smoke_checks,
    )

    # ---- governance ----
    g_raw = raw.get("governance", {})
    governance = GovernanceConfig(
        enabled=g_raw.get("enabled", False),
        test_tool_label=g_raw.get("test_tool_label", "pytest"),
        exclude_roots=_string_list(g_raw.get("exclude_roots", [])),
    )

    # ---- graph ----
    graph_raw = raw.get("graph", {})
    nested_raw = graph_raw.get("nested_projects", {}) if isinstance(graph_raw, dict) else {}
    graph = GraphConfig(
        exclude_paths=_string_list(graph_raw.get("exclude_paths", []) if isinstance(graph_raw, dict) else []),
        ignore_globs=_string_list(graph_raw.get("ignore_globs", []) if isinstance(graph_raw, dict) else []),
        nested_projects=NestedProjectsConfig(
            mode=str(nested_raw.get("mode", "exclude") or "exclude"),
            roots=_string_list(nested_raw.get("roots", [])),
        ),
    )

    # ---- ai ----
    ai_raw = raw.get("ai", {})
    ai = AiConfig(
        routing=_routing_map(ai_raw.get("routing", {}) if isinstance(ai_raw, dict) else {}),
    )

    return ProjectConfig(
        project_id=raw.get("project_id", ""),
        language=raw.get("language", "python"),
        testing=testing,
        build=build,
        deploy=deploy,
        governance=governance,
        graph=graph,
        ai=ai,
    )


# ---------------------------------------------------------------------------
# Public loaders
# ---------------------------------------------------------------------------


def load_project_config(workspace_path: Path) -> ProjectConfig:
    """Discover and load the project config from *workspace_path*.

    Search order:
      1. .aming-claw.yaml
      2. .aming-claw.json

    Falls back to DEFAULT_CONFIG only when the project_id would be
    'aming-claw' and no file is found (with a deprecation warning).

    Raises FileNotFoundError for non-aming-claw projects with no config.
    """
    workspace_path = Path(workspace_path)

    config_file: Optional[Path] = None
    raw: Optional[dict] = None

    for candidate_name, loader in [
        (".aming-claw.yaml", _try_load_yaml),
        (".aming-claw.json", _try_load_json),
    ]:
        candidate = workspace_path / candidate_name
        if candidate.is_file():
            config_file = candidate
            raw = loader(candidate)
            break

    if raw is None:
        # Attempt to derive project_id from workspace path basename
        basename = workspace_path.name.lower().replace("_", "-")
        if basename == "aming-claw" or "aming-claw" in str(workspace_path).replace(
            "\\", "/"
        ):
            logger.warning(
                "DEPRECATION: No .aming-claw.yaml found at %s; using hardcoded "
                "DEFAULT_CONFIG. Please create a .aming-claw.yaml config file.",
                workspace_path,
            )
            return DEFAULT_CONFIG
        raise FileNotFoundError(
            f"No .aming-claw.yaml or .aming-claw.json found at {workspace_path}"
        )

    is_valid, messages = validate_project_config(raw)
    for msg in messages:
        if msg.startswith("Unknown") or msg.startswith("Could not"):
            logger.warning("Config warning (%s): %s", config_file, msg)
        else:
            logger.error("Config error (%s): %s", config_file, msg)

    if not is_valid:
        raise ValueError(
            f"Invalid project config at {config_file}: "
            + "; ".join(m for m in messages if not m.startswith("Unknown"))
        )

    return _parse_raw(raw)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CONFIG_CACHE: Dict[Tuple[str, str, str], ProjectConfig] = {}


def _config_cache_key(
    workspace_path: Path, config_file: Optional[Path]
) -> Tuple[str, str, str]:
    ws_str = str(workspace_path)
    cf_str = str(config_file) if config_file else ""
    if config_file and config_file.is_file():
        content = config_file.read_bytes()
        content_hash = hashlib.md5(content).hexdigest()
    else:
        content_hash = ""
    return (ws_str, cf_str, content_hash)


def _find_config_file(workspace_path: Path) -> Optional[Path]:
    for name in (".aming-claw.yaml", ".aming-claw.json"):
        p = workspace_path / name
        if p.is_file():
            return p
    return None


def _string_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        norm = str(value or "").replace("\\", "/").strip().strip("/")
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _bool_value(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _int_value(raw: Any, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _parse_e2e_trigger(raw: Any) -> E2ETriggerConfig:
    if not isinstance(raw, dict):
        raw = {}
    return E2ETriggerConfig(
        paths=_string_list(raw.get("paths", [])),
        nodes=_string_list(raw.get("nodes", [])),
        tags=_string_list(raw.get("tags", [])),
    )


def _parse_e2e_suite(suite_id: str, raw: Any, parent: E2EConfig) -> E2ESuiteConfig:
    if not isinstance(raw, dict):
        raw = {}
    sid = str(raw.get("suite_id") or suite_id or "").strip()
    return E2ESuiteConfig(
        suite_id=sid,
        label=str(raw.get("label") or sid).strip(),
        command=str(raw.get("command") or "").strip(),
        trigger=_parse_e2e_trigger(raw.get("trigger", {})),
        auto_run=_bool_value(raw.get("auto_run"), parent.auto_run),
        live_ai=_bool_value(raw.get("live_ai"), False),
        mutates_db=_bool_value(raw.get("mutates_db"), True),
        requires_human_approval=_bool_value(raw.get("requires_human_approval"), False),
        isolation_project=str(raw.get("isolation_project") or "").strip(),
        timeout_sec=_int_value(raw.get("timeout_sec"), parent.default_timeout_sec, minimum=1),
        max_parallel=_int_value(raw.get("max_parallel"), parent.max_parallel, minimum=1),
    )


def _parse_e2e_config(raw: Any) -> E2EConfig:
    if not isinstance(raw, dict):
        raw = {}
    config = E2EConfig(
        auto_run=_bool_value(raw.get("auto_run"), False),
        default_timeout_sec=_int_value(raw.get("default_timeout_sec"), 900, minimum=1),
        max_parallel=_int_value(raw.get("max_parallel"), 1, minimum=1),
        require_clean_worktree=_bool_value(raw.get("require_clean_worktree"), True),
        evidence_retention_days=_int_value(raw.get("evidence_retention_days"), 14, minimum=0),
        suites={},
    )
    suites_raw = raw.get("suites", {})
    if isinstance(suites_raw, list):
        iterable = ((str(item.get("suite_id") or item.get("id") or ""), item) for item in suites_raw if isinstance(item, dict))
    elif isinstance(suites_raw, dict):
        iterable = suites_raw.items()
    else:
        iterable = []
    for key, value in iterable:
        suite = _parse_e2e_suite(str(key), value, config)
        if suite.suite_id:
            config.suites[suite.suite_id] = suite
    return config


def e2e_config_to_dict(config: E2EConfig) -> Dict[str, Any]:
    return {
        "auto_run": config.auto_run,
        "default_timeout_sec": config.default_timeout_sec,
        "max_parallel": config.max_parallel,
        "require_clean_worktree": config.require_clean_worktree,
        "evidence_retention_days": config.evidence_retention_days,
        "suites": {
            suite_id: {
                "suite_id": suite.suite_id,
                "label": suite.label,
                "command": suite.command,
                "trigger": {
                    "paths": list(suite.trigger.paths),
                    "nodes": list(suite.trigger.nodes),
                    "tags": list(suite.trigger.tags),
                },
                "auto_run": suite.auto_run,
                "live_ai": suite.live_ai,
                "mutates_db": suite.mutates_db,
                "requires_human_approval": suite.requires_human_approval,
                "isolation_project": suite.isolation_project,
                "timeout_sec": suite.timeout_sec,
                "max_parallel": suite.max_parallel,
            }
            for suite_id, suite in sorted(config.suites.items())
        },
    }


def _routing_map(raw: Any) -> Dict[str, Dict[str, str]]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for role, value in raw.items():
        role_key = str(role or "").strip().lower()
        if not role_key:
            continue
        if isinstance(value, str):
            entry = {"provider": "", "model": value.strip()}
        elif isinstance(value, dict):
            entry = {
                "provider": str(value.get("provider", "") or "").strip(),
                "model": str(value.get("model", "") or "").strip(),
            }
        else:
            continue
        if entry["provider"] or entry["model"]:
            out[role_key] = entry
    return out


def update_project_ai_routing(
    workspace_path: Path,
    routing: Dict[str, Dict[str, str]],
    *,
    project_id: str = "",
) -> ProjectConfig:
    """Update ``ai.routing`` in a project's local config file and reload it.

    Empty provider/model pairs remove the role override. Other top-level config
    blocks are preserved as raw YAML/JSON data.
    """
    workspace_path = Path(workspace_path)
    config_file = _find_config_file(workspace_path)
    if config_file:
        if config_file.suffix == ".json":
            raw = _try_load_json(config_file)
        else:
            raw = _try_load_yaml(config_file)
    else:
        detected = generate_default_config(str(workspace_path), project_id or workspace_path.name)
        raw = {
            "project_id": project_id or detected.project_id,
            "language": detected.language,
            "testing": {
                "unit_command": detected.testing.unit_command,
            },
            "governance": {
                "enabled": detected.governance.enabled,
                "test_tool_label": detected.governance.test_tool_label,
            },
        }
        config_file = workspace_path / ".aming-claw.yaml"
    if not isinstance(raw, dict):
        raw = {}

    next_routing = _routing_map(routing)
    ai_raw = raw.get("ai")
    if not isinstance(ai_raw, dict):
        ai_raw = {}
    ai_raw["routing"] = next_routing
    raw["ai"] = ai_raw

    is_valid, messages = validate_project_config(raw)
    if not is_valid:
        raise ValueError(
            "Invalid project config after ai.routing update: "
            + "; ".join(m for m in messages if not m.startswith("Unknown"))
        )

    if config_file.suffix == ".json":
        _write_json(config_file, raw)
    else:
        _write_yaml(config_file, raw)
    return load_project_config(workspace_path)


def effective_graph_exclude_roots(config: ProjectConfig) -> List[str]:
    """Return all project-level path prefixes excluded from graph governance."""
    graph = getattr(config, "graph", None)
    governance = getattr(config, "governance", None)
    nested = getattr(graph, "nested_projects", None)
    groups: List[Iterable[str]] = [
        getattr(governance, "exclude_roots", []) or [],
        getattr(graph, "exclude_paths", []) or [],
    ]
    if getattr(nested, "mode", "exclude") == "exclude":
        groups.append(getattr(nested, "roots", []) or [])
    return _merge_string_lists(*groups)


def _merge_string_lists(*groups: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group or []:
            norm = str(value or "").replace("\\", "/").strip().strip("/")
            if not norm or norm in seen:
                continue
            seen.add(norm)
            out.append(norm)
    return out


# ---------------------------------------------------------------------------
# Registry-backed resolution
# ---------------------------------------------------------------------------


def _resolve_workspace_path(project_id: str) -> Path:
    """Resolve workspace path from governance projects.json."""
    from utils import normalize_project_id  # noqa: PLC0415
    normalized = normalize_project_id(project_id)
    # Read governance projects.json directly
    import json
    state_dir = os.path.join(
        os.environ.get("SHARED_VOLUME_PATH",
                        os.path.join(os.path.dirname(__file__), "..", "shared-volume")),
        "codex-tasks", "state", "governance", "projects.json")
    try:
        with open(state_dir) as f:
            data = json.load(f)
        projects = data.get("projects", {})
        proj = projects.get(normalized, {})
        wp = proj.get("workspace_path", "")
        if wp:
            return Path(wp)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    raise LookupError(f"No workspace registered for project_id={normalized!r}")


def resolve_project_config(project_id: str) -> ProjectConfig:
    """Look up *project_id* in governance projects, then load its config.

    Falls back to DEFAULT_CONFIG when the project is 'aming-claw' and no
    config file exists.

    Raises LookupError when the workspace cannot be found.
    """
    workspace_path = _resolve_workspace_path(project_id)
    return load_project_config(workspace_path)


def get_project_config(project_id: str) -> ProjectConfig:
    """Cached version of :func:`resolve_project_config`.

    Cache key = (workspace_path, config_file_path, md5_of_config_content).
    """
    workspace_path = _resolve_workspace_path(project_id)
    config_file = _find_config_file(workspace_path)
    key = _config_cache_key(workspace_path, config_file)

    if key not in _CONFIG_CACHE:
        _CONFIG_CACHE[key] = load_project_config(workspace_path)

    return _CONFIG_CACHE[key]


# ---------------------------------------------------------------------------
# Convenience accessors
# ---------------------------------------------------------------------------


def generate_default_config(workspace_path: str, project_name: str = "") -> ProjectConfig:
    """Auto-detect language and generate sensible default config (R5).

    Detects test runner based on project marker files:
      - pyproject.toml → pytest
      - package.json → npm test
      - Cargo.toml → cargo test
      - go.mod → go test

    Does NOT write any files to workspace_path (AC7).
    """
    ws = Path(workspace_path)
    project_id = project_name or ws.name.lower().replace("_", "-").replace(" ", "-")

    # Detect language
    if (ws / "pyproject.toml").exists() or (ws / "setup.py").exists():
        language = "python"
        unit_command = "python -m pytest"
    elif (ws / "Cargo.toml").exists():
        language = "rust"
        unit_command = "cargo test"
    elif (ws / "go.mod").exists():
        language = "go"
        unit_command = "go test ./..."
    elif (ws / "tsconfig.json").exists():
        language = "typescript"
        unit_command = "npm test"
    elif (ws / "package.json").exists():
        language = "javascript"
        unit_command = "npm test"
    else:
        language = "unknown"
        unit_command = ""

    # Detect deploy strategy
    if (ws / "Dockerfile").exists() or (ws / "docker-compose.yml").exists():
        strategy = "docker"
    elif (ws / "electron-builder.yml").exists() or (ws / "electron-builder.json").exists():
        strategy = "electron"
    else:
        strategy = "none"

    return ProjectConfig(
        project_id=project_id,
        language=language,
        testing=TestingConfig(
            unit_command=unit_command,
            e2e_command="",
            allowed_commands=[],
        ),
        build=BuildConfig(command="", release_checks=[]),
        deploy=DeployConfig(strategy=strategy),
        governance=GovernanceConfig(enabled=True, test_tool_label=language),
    )


def get_test_command(project_id: str) -> str:
    """Return the unit test command for *project_id*."""
    return get_project_config(project_id).testing.unit_command


def get_service_rules(project_id: str) -> List[ServiceRule]:
    """Return the list of ServiceRule objects for *project_id*."""
    return get_project_config(project_id).deploy.service_rules


def get_smoke_checks(project_id: str) -> List[SmokeCheck]:
    """Return the list of SmokeCheck objects for *project_id*."""
    return get_project_config(project_id).deploy.smoke_checks


# ---------------------------------------------------------------------------
# explain_config
# ---------------------------------------------------------------------------


def explain_config(
    project_id: str,
    changed_files: Optional[List[str]] = None,
) -> dict:
    """Return a human-readable summary of the resolved config.

    If *changed_files* is provided, also reports which services are
    affected according to the deploy service_rules.
    """
    config = get_project_config(project_id)

    affected_services: List[str] = []
    if changed_files:
        # Normalise file paths to forward slashes for fnmatch
        normalised_files = [f.replace("\\", "/") for f in changed_files]
        seen: set = set()
        for rule in config.deploy.service_rules:
            for pattern in rule.patterns:
                norm_pattern = pattern.replace("\\", "/")
                for f in normalised_files:
                    if fnmatch.fnmatch(f, norm_pattern):
                        for svc in rule.services:
                            if svc not in seen:
                                seen.add(svc)
                                affected_services.append(svc)
                        break

    return {
        "project_id": config.project_id,
        "language": config.language,
        "testing": {
            "unit_command": config.testing.unit_command,
            "e2e_command": config.testing.e2e_command,
            "allowed_commands": config.testing.allowed_commands,
            "e2e": e2e_config_to_dict(config.testing.e2e),
        },
        "build": {
            "command": config.build.command,
            "release_checks": config.build.release_checks,
        },
        "deploy": {
            "strategy": config.deploy.strategy,
            "service_rules": [
                {"patterns": r.patterns, "services": r.services}
                for r in config.deploy.service_rules
            ],
            "commands": config.deploy.commands,
            "smoke_checks": [
                {
                    "name": s.name,
                    "url": s.url,
                    "expected_status": s.expected_status,
                    "timeout": s.timeout,
                }
                for s in config.deploy.smoke_checks
            ],
        },
        "governance": {
            "enabled": config.governance.enabled,
            "test_tool_label": config.governance.test_tool_label,
        },
        "affected_services": affected_services,
    }
