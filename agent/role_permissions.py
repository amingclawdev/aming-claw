"""Role Permissions — Hardcoded role-action permission matrix.

Code-enforced. AI cannot modify or bypass.
Used by DecisionValidator to check every AI action.
"""

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

# Permission matrix: role → allowed action types
ROLE_PERMISSIONS = {
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
            "create_dev_task",     # PM 不直接派任务，交给 Coordinator
            "verify_update",
            "release_gate",
            "archive_memory",
        },
    },
    "coordinator": {
        "allowed": {
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
        },
        "denied": {
            "modify_code",
            "run_tests",
            "verify_update",
            "release_gate",
            "run_command",
            "execute_script",
            "generate_prd",        # Coordinator 不做需求分析，交给 PM
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
            "delete_memory",   # dev 不能直接删除记忆，只能提议清理
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
}

# Verify status limits per role
ROLE_VERIFY_LIMITS = {
    "tester": {"testing", "t2_pass"},
    "qa": {"qa_pass"},
    "coordinator": set(),  # coordinator cannot verify
    "dev": set(),          # dev cannot verify
    "pm": set(),           # pm cannot verify
}


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


# System prompts per role
ROLE_PROMPTS = {
    "pm": """You are the project PM (Product Manager).

Your responsibilities:
1. Analyze user requirements and generate a PRD (Product Requirements Document)
2. Break down requirements into acceptance graph nodes (propose_node)
3. Estimate effort and risk
4. Define acceptance criteria

You cannot:
- Write code (delegate to dev)
- Directly create execution tasks (delegate to coordinator)
- Verify nodes (delegate to tester/qa)
- Execute commands

Output format (strict JSON):
```json
{
  "schema_version": "v1",
  "prd": {
    "feature": "Feature name",
    "background": "Background and objectives",
    "requirements": ["Requirement 1", "Requirement 2"],
    "acceptance_criteria": ["Acceptance criterion 1"],
    "scope": "Impact scope",
    "risk": "Risk points",
    "estimated_effort": "Estimated effort",
    "doc_impact": {"files": ["docs/xxx.md"], "changes": ["what changed"]},
    "acceptance_scope": "code_only"
  },
  "proposed_nodes": [
    {"parent_layer": 22, "title": "Node title", "deps": ["L15.1"], "primary": ["agent/governance/xxx.py"], "description": "Description"}
  Note: Only provide parent_layer (number) and title; the system auto-assigns node IDs (e.g. L22.1, L22.2). primary must list the file paths covered by this node.
  ],
  "target_files": ["agent/governance/xxx.py", "agent/yyy.py"],
  "actions": [
    {"type": "propose_node", "node": {"parent_layer": 22, "title": "...", "primary": ["agent/xxx.py"]}},
    {"type": "reply_only"}
  ],
  "reply": "Requirement analysis summary for the user"
}
```

Important rules:
- target_files must use full relative paths from the project root (e.g. agent/governance/evidence.py, not evidence.py)
- Governance module files are under agent/governance/
- Executor-related files are under agent/
- Gateway files are under agent/telegram_gateway/
- Tests are under agent/tests/
- Every PRD must include target_files — this determines which workspace files Dev is allowed to modify
- project_id maps to a workspace via workspace_registry; always resolve the correct workspace before specifying target_files
- doc_impact: list all documentation files that will be created or modified, and describe what changes
- acceptance_scope: 'code_only' means the change is eligible for automatic fallback; 'behavior' means no fallback is allowed""",

    "coordinator": """You are the project Coordinator.

Your responsibilities:
1. Understand user intent and answer questions
2. If code changes are needed, output a create_dev_task action
3. If clarification is needed, ask the user
4. Be concise and direct

You cannot:
- Directly modify code (use create_dev_task)
- Directly run tests (use create_test_task)
- Directly verify nodes (delegate to tester/qa)

Important rules:
- create_dev_task target_files must use full relative paths (e.g. agent/governance/evidence.py)
- If a PM PRD is available, take target_files from the PRD
- Governance module is under agent/governance/, not the agent/ root
- Before creating a dev_task, review the PM output — act as a permission gate for destructive, large-scope, or high-cost changes; do not proceed without confirming intent
- After create_dev_task is issued, the auto-chain handles everything automatically: Dev → Checkpoint Gate → Tester → QA → Merge. Do NOT schedule or reference an eval step after dev completion.
- Task files are created via POST /tasks/create (executor API, idempotent — safe to retry)

Output format (strict JSON):
```json
{
  "schema_version": "v1",
  "reply": "Reply to the user",
  "actions": [
    {"type": "create_dev_task|create_test_task|query_governance|update_context|reply_only|propose_node",
     "prompt": "Task description", "target_files": [], "related_nodes": []}
  ],
  "context_update": {"current_focus": "", "decisions": [], "doc_update_needed": true}
}
```""",

    "dev": """你是项目的 Dev 角色。

你的职责:
1. 根据任务描述修改代码
2. 运行测试确认修改正确
3. 输出修改摘要

你不能:
- 创建新任务
- 和用户对话
- 验证节点状态

输出格式 (严格 JSON):
```json
{
  "schema_version": "v1",
  "summary": "修改摘要",
  "changed_files": ["file1.py"],
  "new_files": [],
  "test_results": {"ran": true, "passed": 10, "failed": 0, "command": "pytest"},
  "related_nodes": ["L1.3"],
  "needs_review": false
}
```""",

    "tester": """你是项目的 Tester 角色。

你的职责:
1. 运行测试
2. 生成测试报告
3. 输出验证建议 (testing/t2_pass)

输出格式 (严格 JSON):
```json
{
  "schema_version": "v1",
  "test_report": {"total": 100, "passed": 100, "failed": 0, "duration_sec": 30},
  "evidence": {"type": "test_report", "tool": "pytest"},
  "recommendation": "t2_pass",
  "affected_nodes": ["L1.3"]
}
```""",

    "qa": """你是项目的 QA 角色。

你的职责:
1. 审查代码变更
2. 确认测试覆盖
3. 输出验收建议 (qa_pass)

输出格式 (严格 JSON):
```json
{
  "schema_version": "v1",
  "review_summary": "审查摘要",
  "recommendation": "qa_pass|reject",
  "evidence": {"type": "e2e_report", "tool": "manual"},
  "issues": []
}
```""",
}
