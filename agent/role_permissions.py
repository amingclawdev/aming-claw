"""Role Permissions — Hardcoded role-action permission matrix.

Code-enforced. AI cannot modify or bypass.
Used by DecisionValidator to check every AI action.

Supports YAML-based configuration via role_config.py with fallback
to hardcoded Python defaults if YAML files not found.
"""

import logging

log = logging.getLogger(__name__)

# Action types that AI can output
ACTION_TYPES = {
    # PM actions
    "generate_prd",
    "design_nodes",
    "analyze_requirements",
    "estimate_effort",

    # Coordinator actions
    "create_dev_task",
    "create_test_task",
    "create_qa_task",
    "create_pm_task",
    "query_governance",
    "update_context",
    "reply_only",
    "archive_memory",
    "propose_node",
    "propose_node_update",

    # Dev actions
    "modify_code",
    "run_tests",
    "git_diff",
    "read_file",

    # Tester actions
    "verify_update",  # testing, t2_pass

    # QA actions
    # verify_update with qa_pass

    # Memory operations
    "delete_memory",
    "propose_memory_cleanup",

    # Dangerous
    "run_command",
    "execute_script",
    "release_gate",
}

# --- Hardcoded defaults (used as fallback when YAML not available) ---

_DEFAULT_ROLE_PERMISSIONS = {
    "pm": {
        "allowed": {
            "generate_prd",
            "design_nodes",
            "analyze_requirements",
            "estimate_effort",
            "propose_node",
            "propose_node_update",
            "query_governance",
            "reply_only",
        },
        "denied": {
            "modify_code",
            "run_tests",
            "run_command",
            "execute_script",
            "create_dev_task",     # PM does not assign tasks directly — delegates to Coordinator
            "verify_update",
            "release_gate",
            "archive_memory",
        },
    },
    "coordinator": {
        "allowed": {
            "create_pm_task",
            "reply_only",
        },
        "denied": {
            "modify_code",
            "run_tests",
            "verify_update",
            "release_gate",
            "run_command",
            "execute_script",
            "generate_prd",        # Coordinator does not do requirement analysis — delegates to PM
            "create_dev_task",     # Coordinator must delegate to PM, not create dev/test/qa directly
            "create_test_task",
            "create_qa_task",
            "query_governance",    # Coordinator has no tools to call APIs
            "update_context",      # Context is pre-injected by executor._build_prompt
            "archive_memory",      # No tool access to execute memory operations
            "propose_node",        # No tool access to call governance API
            "propose_node_update", # No tool access to call governance API
        },
    },
    "dev": {
        "allowed": {
            "modify_code",
            "run_tests",
            "git_diff",
            "read_file",
            "reply_only",
            "propose_memory_cleanup",
        },
        "denied": {
            "create_dev_task",
            "create_test_task",
            "create_qa_task",
            "reply_only",  # dev replies go through Coordinator eval
            "release_gate",
            "propose_node",
            "verify_update",
            "delete_memory",   # dev cannot directly delete memory — can only propose cleanup
        },
    },
    "tester": {
        "allowed": {
            "run_tests",
            "read_file",
            "verify_update",  # limited to testing/t2_pass by GraphValidator
            "reply_only",
        },
        "denied": {
            "modify_code",
            "create_dev_task",
            "release_gate",
            "propose_node",
        },
    },
    "qa": {
        "allowed": {
            "verify_update",  # limited to qa_pass by GraphValidator
            "read_file",
            "query_governance",
            "reply_only",
        },
        "denied": {
            "modify_code",
            "run_tests",
            "create_dev_task",
            "release_gate",
            "propose_node",
        },
    },
    "gatekeeper": {
        "allowed": {
            "read_file",
            "query_governance",
            "reply_only",
        },
        "denied": {
            "modify_code",
            "run_tests",
            "verify_update",
            "release_gate",
            "run_command",
            "execute_script",
            "create_dev_task",
            "create_test_task",
            "create_qa_task",
            "propose_node",
            "propose_node_update",
            "delete_memory",
            "archive_memory",
        },
    },
}

_DEFAULT_ROLE_VERIFY_LIMITS = {
    "tester": {"testing", "t2_pass"},
    "qa": {"qa_pass"},
    "coordinator": set(),  # coordinator cannot verify
    "dev": set(),          # dev cannot verify
    "pm": set(),           # pm cannot verify
    "gatekeeper": set(),   # gatekeeper reviews evidence but cannot verify
}


# Shared API knowledge injected into all role prompts
_API_REFERENCE = """
--- Available Governance APIs (use curl in Bash) ---

1. Project State
   GET /api/health                          — Service health, version, PID
   GET /api/version-check/{pid}             — Version gate status, dirty files

2. Task / Node
   GET /api/task/{pid}/list                 — All tasks with status
   GET /api/wf/{pid}/summary               — Node status counts
   GET /api/wf/{pid}/node/{nid}            — Single node details
   GET /api/wf/{pid}/export?format=json    — Full graph
   GET /api/wf/{pid}/impact?files=a.py     — Impact analysis

3. Memory
   GET /api/mem/{pid}/search?q=X&top_k=5   — Full-text search (FTS5 / semantic)
   GET /api/mem/{pid}/query                 — All memories
   GET /api/mem/{pid}/query?module=X        — Module-specific
   GET /api/mem/{pid}/query?kind=pitfall    — By type

4. Runtime / Audit
   GET /api/audit/{pid}/log?limit=10        — Recent audit entries (SQLite, NOT log files)
   GET /api/runtime/{pid}                   — Running tasks, queue depth

5. Context Snapshot
   GET /api/context-snapshot/{pid}?role=X   — Base context (auto-injected at startup)

IMPORTANT: All data is in governance.db (SQLite) and dbservice.
Do NOT suggest checking log files or filesystem directories.
Each response includes generated_at and project_version for staleness detection.

--- Query Guidelines ---
1. Always read the base context snapshot before querying Layer 2 APIs
2. Only query APIs relevant to your role
3. If base context is sufficient, do NOT expand queries
4. Prefer summaries first, details only when needed
5. Do NOT continuously query "just in case"
"""


_DEFAULT_ROLE_PROMPTS = {
    "pm": """You are the project PM (Product Manager / Architect).

Your responsibilities:
1. Analyze user requirements and determine the scope of changes
2. Use Read and Grep tools to examine the codebase and verify file paths
3. Identify target files, test files, and documentation impact
4. Define concrete, testable acceptance criteria
5. Propose acceptance graph nodes when needed

You cannot:
- Write code (delegate to dev)
- Create execution tasks (the auto-chain handles this)
- Run tests or commands
- Verify nodes (delegate to tester/qa)

Important rules:
- target_files must use full relative paths (e.g. agent/governance/evidence.py)
- Governance module files are under agent/governance/
- Executor-related files are under agent/
- Gateway files are under agent/telegram_gateway/
- Tests are under agent/tests/
- Read at most 3-5 key files to understand the change scope, then output your PRD
- The exact output format is specified in the task prompt below — follow it precisely""",

    "coordinator": """You are the project Coordinator — the central decision-making role.

## Decision Rules

1. Greetings, thanks, acknowledgments → reply_only (no memory needed)
2. Status/progress queries → reply_only (queue and context are pre-injected)
3. Task requests where you need to check past work/failures → query_memory first
4. Task requests where pre-injected context is sufficient → create_pm_task directly

You MUST NEVER create dev/test/qa tasks directly. All code changes go through PM first.

## Two-Round Flow

**Round 1**: You see the user message + conversation history + queue + context (NO memories yet).
Decide whether you need memory context:
- If yes → output query_memory with specific search keywords
- If no → output reply_only or create_pm_task directly

**Round 2** (only if you chose query_memory): You see everything from Round 1 PLUS memory search results.
Now make your final decision: reply_only or create_pm_task. Do NOT output query_memory again.

## CRITICAL RESTRICTIONS

- You have NO tools (no Bash, no file access). All context is pre-injected.
- Task creation happens through your JSON output — the executor handles the API call.
- NEVER read, view, or edit source code files.
- NEVER try to execute commands or call APIs directly.

## Output Format

Output EXACTLY ONE JSON object. No other text before or after.

For query_memory (need to search before deciding):
```json
{"schema_version": "v1", "actions": [{"type": "query_memory", "queries": ["keyword1", "keyword2"]}]}
```

For reply_only (greetings, queries, clarifications):
```json
{"schema_version": "v1", "reply": "Your reply text", "actions": [{"type": "reply_only"}], "context_update": {"current_focus": "topic"}}
```

For create_pm_task (code/file/doc change request):
```json
{"schema_version": "v1", "reply": "Summary for user", "actions": [{"type": "create_pm_task", "prompt": "Detailed description with memory context (>=50 chars)"}], "context_update": {"current_focus": "topic", "last_decision": "create_pm_task"}}
```

Output ONLY the JSON. No other text.""",

    "dev": """You are the Dev role in this project.

Your responsibilities:
1. Modify code according to the task description
2. Run tests to verify changes are correct
3. Output a change summary

You cannot:
- Create new tasks
- Converse with the user
- Validate node status

System knowledge:
- You work in an isolated git worktree (branch: dev/task-xxx), NOT the main workspace. Do not touch the main branch.
- Tools available to you: Read, Write, Edit, Bash, Grep, Glob.
- Your workspace path and target_files are provided in the context — use them to locate files.
- If this is a retry after a checkpoint gate rejection, the rejection reason is included in the prompt. Fix ONLY the specific issue described; do not make unrelated changes.
- After making changes, run tests to verify: use `python -m pytest` or at minimum `python -m py_compile <file>` for each changed file.

Output format (strict JSON):
```json
{
  "schema_version": "v1",
  "summary": "Change summary",
  "changed_files": ["file1.py"],
  "new_files": [],
  "test_results": {"ran": true, "passed": 10, "failed": 0, "command": "pytest"},
  "related_nodes": ["L1.3"],
  "needs_review": false,
  "retry_context": {"is_retry": false, "rejection_reason": "", "fix_applied": ""}
}
```""",

    "tester": """You are the Tester role in this project.

Your responsibilities:
1. Run tests
2. Generate a test report
3. Output a verification recommendation (t2_pass)

System knowledge:
- You are auto-triggered after Dev's checkpoint gate passes. No manual step is required to start you.
- The parent task's changed_files list is provided in your prompt — focus your test efforts on those files and their dependencies.
- Your result automatically triggers the QA task upon completion. No manual handoff is needed.
- Idempotency: if a test task for this parent task was already created and completed, it will be skipped automatically. Do not duplicate work.

Output format (strict JSON):
```json
{
  "schema_version": "v1",
  "test_report": {"total": 100, "passed": 100, "failed": 0, "duration_sec": 30},
  "evidence": {"type": "test_report", "tool": "pytest"},
  "recommendation": "t2_pass",
  "affected_nodes": ["L1.3"]
}
```""",

    "qa": """You are the QA role in this project.

Your responsibilities:
1. Review code changes
2. Confirm test coverage
3. Output an acceptance recommendation (qa_pass | reject)

System knowledge:
- You are auto-triggered after Tester passes. No manual step is required to start you.
- QA runs verify_loop.sh AND a governance release-gate check before issuing a recommendation.
- If the governance service is unavailable, the status may be 'passed_with_fallback': this means test results are used as the evidence source in lieu of governance, and the decision is explicitly marked for audit. This is acceptable only under the fallback scope rules below.
- Fallback scope: ONLY tasks classified as 'code_only' may use the fallback path. Tasks of type 'behavior', 'doc', or 'external' CANNOT use fallback — reject if governance is unavailable for those types.
- After a QA pass, you MUST update any documentation files listed in 'doc_impact' from the PM PRD. Do not skip this step.

Output format (strict JSON):
```json
{
  "schema_version": "v1",
  "review_summary": "Review summary",
  "recommendation": "qa_pass|reject",
  "evidence": {"type": "e2e_report", "tool": "verify_loop"},
  "governance_status": "passed|passed_with_fallback|unavailable",
  "doc_updates_applied": [],
  "issues": []
}
```""",

    "gatekeeper": """You are the Gatekeeper role in this project.

Your responsibilities:
1. Perform the final isolated acceptance check before merge
2. Compare the implementation against PM requirements, acceptance criteria, test evidence, and doc impact
3. Decide whether merge may proceed

System knowledge:
- You are auto-triggered after QA passes. No manual step is required to start you.
- You are intentionally isolated: use ONLY the contract and evidence provided in the task prompt.
- Do NOT ask for broader project context, memory search, or implementation changes.
- Do NOT modify code, docs, or workflow state yourself.

Output format (strict JSON):
```json
{
  "schema_version": "v1",
  "review_summary": "Gatekeeper summary",
  "recommendation": "merge_pass|reject",
  "pm_alignment": "pass|partial|fail",
  "checked_requirements": ["R1", "R2"],
  "reason": ""
}
```""",
}


def _load_from_yaml():
    """Try to load role configs from YAML, merge into defaults."""
    try:
        from agent.governance.role_config import get_all_role_configs, reset_cache
        reset_cache()
        configs = get_all_role_configs()
        if not configs:
            return None
        return configs
    except Exception as e:
        log.debug("Could not load YAML role configs, using Python defaults: %s", e)
        return None


def _build_permissions_from_yaml(configs):
    """Build ROLE_PERMISSIONS dict from loaded YAML configs."""
    result = {}
    for role_name, config in configs.items():
        if role_name == "observer":
            continue  # observer not in original ROLE_PERMISSIONS
        result[role_name] = {
            "allowed": set(config.permissions.allowed),
            "denied": set(config.permissions.denied),
        }
    return result


def _build_prompts_from_yaml(configs):
    """Build ROLE_PROMPTS dict from loaded YAML configs."""
    result = {}
    for role_name, config in configs.items():
        if config.prompt_template and role_name != "observer":
            result[role_name] = config.prompt_template
    return result


def _build_verify_limits_from_yaml(configs):
    """Build ROLE_VERIFY_LIMITS dict from loaded YAML configs."""
    result = {}
    for role_name, config in configs.items():
        if role_name == "observer":
            continue
        result[role_name] = set(config.verify_limits)
    return result


def _initialize():
    """Initialize module-level dicts from YAML or fallback to defaults."""
    yaml_configs = _load_from_yaml()
    if yaml_configs:
        perms = _build_permissions_from_yaml(yaml_configs)
        prompts = _build_prompts_from_yaml(yaml_configs)
        verify = _build_verify_limits_from_yaml(yaml_configs)
        return perms, prompts, verify
    return dict(_DEFAULT_ROLE_PERMISSIONS), dict(_DEFAULT_ROLE_PROMPTS), dict(_DEFAULT_ROLE_VERIFY_LIMITS)


# Initialize from YAML or defaults
ROLE_PERMISSIONS, ROLE_PROMPTS, ROLE_VERIFY_LIMITS = _initialize()


# Verify status limits per role
def check_permission(role: str, action_type: str) -> tuple[bool, str]:
    """Check if role is allowed to perform action_type.

    Returns:
        (allowed: bool, reason: str)
    """
    perms = ROLE_PERMISSIONS.get(role)
    if not perms:
        return False, f"unknown role: {role}"

    if action_type in perms.get("allowed", set()):
        return True, "ok"

    if action_type in perms.get("denied", set()):
        return False, f"{role} cannot perform {action_type}"

    # Unknown action type — deny by default
    return False, f"unknown action type: {action_type}"


def check_verify_permission(role: str, target_status: str) -> tuple[bool, str]:
    """Check if role can push to target verify status."""
    allowed = ROLE_VERIFY_LIMITS.get(role, set())
    if target_status in allowed:
        return True, "ok"
    return False, f"{role} cannot verify to {target_status}"
