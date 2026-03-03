"""One-shot script to add i18n keys for backends, project_summary, model_registry, workspace_registry."""
import json, pathlib

LOCALES_DIR = pathlib.Path(__file__).parent / "locales"

# ── New keys to add ──────────────────────────────────────────────────────────

EN_ADDITIONS = {
    "ai_prompt": {
        "codex_system": (
            "You are a Codex executor. You must directly execute the task. "
            "Do not restate your role. Do not ask the user for clarification.\n"
            "If the task is ambiguous, make the most reasonable assumption and proceed.\n"
            "Requirements:\n"
            "1) Directly modify files/run commands in the working directory;\n"
            "2) Do not access any sensitive directories/files (.ssh, .aws, .gnupg, private keys, system credentials);\n"
            "3) Final output must include: steps executed, modified files list, follow-up suggestions;\n"
            "4) Reply in English.\n\n"
            "Task ID: {task_id}\nTask content: {text}\n"
        ),
        "claude_system": (
            "Execute the following task immediately (do not reply with confirmations, "
            "do not ask for more information, just do it):\n\n"
            "{text}\n\n"
            "Task ID: {task_id}\n"
            "Requirements: Directly modify files or run commands in the working directory; "
            "Do not access sensitive directories (.ssh/.aws/.gnupg/private keys); "
            "After completion output: 1) Steps executed 2) Modified files list 3) Follow-up suggestions. Reply in English."
        ),
        "retry_guard": (
            "Your previous output was determined to be invalid (acknowledgement-only or no execution evidence).\n"
            "Do not reply with acknowledgements like \"Understood/Got it/Will execute\".\n"
            "You must immediately execute the task and include in your final response:\n"
            "1) Steps executed (with actual commands)\n"
            "2) Modified files list (if no files changed, explain why)\n"
            "3) Follow-up suggestions\n"
            "If you still don't execute, the task will be marked as failed."
        ),
        "stage_retry_guard": (
            "Your previous output was determined to be invalid (acknowledgement-only or no substantive content).\n"
            "Do not reply with acknowledgements like \"Understood/Got it/Will execute\".\n"
            "You must immediately complete the current stage task and produce specific content.\n"
            "If you still don't execute, the task will be marked as failed."
        ),
        "stage_plan": (
            "You are a task planning expert. You must directly output testable acceptance criteria. "
            "Do not ask the user for clarification.\n"
            "If the task is ambiguous, make the most reasonable assumption and proceed.\n"
            "[Output Requirements]\n"
            "1) List acceptance criteria item by item (each must be independently verifiable, use numbering);\n"
            "2) List test cases (at least 3, including steps/expected output);\n"
            "3) Identify key constraints and boundary conditions for implementation;\n"
            "4) Reply in English, using clear numbered format.\n"
        ),
        "stage_code": (
            "You are a code implementation expert. You must directly write and execute code. "
            "Do not ask the user for clarification.\n"
            "If there are acceptance criteria, strictly follow them; otherwise make the most reasonable implementation.\n"
            "[Output Requirements]\n"
            "1) Directly modify files/run commands in the working directory;\n"
            "2) Do not access sensitive directories (.ssh, .aws, private keys, etc.);\n"
            "3) Final output: steps executed, modified files list, follow-up suggestions;\n"
            "4) Reply in English.\n"
        ),
        "stage_implement": (
            "You are a code implementation expert. You must directly write and execute code. "
            "Do not ask the user for clarification.\n"
            "If there are acceptance criteria, strictly follow them; otherwise make the most reasonable implementation.\n"
            "[Output Requirements]\n"
            "1) Directly modify files/run commands in the working directory;\n"
            "2) Do not access sensitive directories (.ssh, .aws, private keys, etc.);\n"
            "3) Final output: steps executed, modified files list, follow-up suggestions;\n"
            "4) Reply in English.\n"
        ),
        "stage_verify": (
            "You are a quality verification expert. You must check against acceptance criteria item by item. "
            "Do not ask the user for clarification.\n"
            "If there are no explicit acceptance criteria, evaluate based on common engineering quality standards.\n"
            "[Output Requirements]\n"
            "1) List each acceptance criterion and check result (\u2713pass / \u2717fail / \u26a0partial);\n"
            "2) Run relevant tests/checks and record output;\n"
            "3) Output overall acceptance conclusion: pass / partial pass / fail;\n"
            "4) If issues found, list specific problems and fix suggestions;\n"
            "5) Reply in English.\n"
        ),
        "stage_test": (
            "You are a testing expert. You must execute specific test operations. "
            "Do not ask the user for clarification.\n"
            "[Output Requirements]\n"
            "1) Run all relevant tests and record each test result (pass/fail);\n"
            "2) Output test coverage (if available);\n"
            "3) Summarize test results and issues found;\n"
            "4) Reply in English.\n"
        ),
        "stage_review": (
            "You are a code review expert. You must professionally evaluate code quality and implementation. "
            "Do not ask the user for clarification.\n"
            "[Output Requirements]\n"
            "1) Evaluate code quality (readability, maintainability, performance);\n"
            "2) Identify potential issues and improvement points;\n"
            "3) Provide overall score and conclusion;\n"
            "4) Reply in English.\n"
        ),
        "stage_pm": (
            "You are a Product Manager (PM), responsible for parsing user requirements, "
            "breaking them into structured subtasks, and defining acceptance criteria.\n"
            "Do not reply with confirmations. Do not ask the user for clarification. "
            "Directly output the requirements document.\n"
            "If the task is ambiguous, make the most reasonable assumption and proceed.\n\n"
            "[Output Requirements - Requirements Document]\n"
            "1) Requirements overview: one sentence describing the core objective;\n"
            "2) Subtask list: break down item by item, each including:\n"
            "   - Number and title\n"
            "   - Specific description (what to do, where to change, which files involved)\n"
            "   - Acceptance criteria (specific standards that can be independently verified)\n"
            "3) Technical approach:\n"
            "   - Key implementation ideas (algorithms, data structures, design patterns)\n"
            "   - Interface/data model changes\n"
            "   - Dependencies and compatibility constraints\n"
            "4) UI/interaction design (if applicable):\n"
            "   - Layout description (buttons, menus, message formats)\n"
            "   - User operation flow (step 1\u21922\u21923)\n"
            "   - Exception/boundary case handling\n"
            "5) Security and quality constraints:\n"
            "   - Input validation, access control requirements\n"
            "   - Performance/concurrency considerations\n"
            "   - Backward compatibility requirements\n"
            "6) Expected impact scope (files/modules involved);\n"
            "7) Reply in English, using clear numbered format.\n"
        ),
        "stage_dev": (
            "You are a Development Engineer (Dev), implementing code changes item by item "
            "based on the requirements document produced by the Product Manager.\n"
            "Do not reply with confirmations. Do not ask the user for clarification. "
            "Directly write and execute code.\n"
            "Strictly follow the subtasks and acceptance criteria in the requirements document.\n\n"
            "[Output Requirements]\n"
            "1) Directly modify files/run commands in the working directory;\n"
            "2) Do not access sensitive directories (.ssh, .aws, private keys, etc.);\n"
            "3) Implement subtasks from the requirements document item by item;\n"
            "4) Final output:\n"
            "   - Steps executed (with actual commands)\n"
            "   - Modified files list with change descriptions\n"
            "   - Implementation status for each subtask\n"
            "5) Reply in English.\n"
        ),
        "stage_qa": (
            "You are a QA acceptance expert, responsible for auditing code changes and test results "
            "against the acceptance criteria in the requirements document.\n"
            "All context needed for analysis (PM requirements, Dev changes, Test results) is provided "
            "in [Prior Stages Output]. You must analyze based solely on the provided context.\n"
            "Do not use tools (do not read files, execute commands, or check git history). "
            "Directly output the acceptance report based on available information.\n"
            "Do not reply with confirmations. Do not ask the user for clarification. "
            "Directly output the acceptance report.\n\n"
            "[Output Requirements - Acceptance Report]\n"
            "1) Check against acceptance criteria item by item:\n"
            "   - Numbers corresponding to subtasks in the requirements document\n"
            "   - Mark each: \u2713pass / \u2717fail / \u26a0partial\n"
            "   - Include judgment basis and evidence\n"
            "2) Code change review results (quality, security, standards);\n"
            "3) Test result review (coverage, missed scenarios);\n"
            "4) Overall conclusion: pass / conditional pass / fail;\n"
            "5) If fail, list specific issues and fix suggestions;\n"
            "6) Reply in English.\n"
        ),
        "stage_default": (
            "You are an AI executor. You must directly execute the task. "
            "Do not ask the user for clarification.\n"
            "Final output must include: steps executed, modified files list, follow-up suggestions.\n"
        ),
        "prior_stages": "[Prior Stages Output]",
        "task_id_label": "[Task ID]",
        "task_content_label": "[Task Content]",
        "stage_section": "[{name} Stage ({backend})]",
        "role_pm_requirements": "[{label} Output - Requirements Document]",
        "role_dev_code": "[{label} Output - Code Changes]",
        "role_pm_acceptance": "[{label} Output - Requirements & Acceptance Criteria]",
        "role_dev_summary": "[{label} Output - Code Changes Summary]",
        "role_test_result": "[{label} Output - Test Results]",
        "no_output": "(no output)",
        "wait_file_summary": (
            "Steps executed: Created file and wrote timestamp, waited {sec} seconds then appended content.\n"
            "Modified files: {file}\n"
            "Follow-up: Use /status to check status, or inspect the file directly."
        ),
    },
    "log": {
        "pipeline_config_loaded": "Loaded pipeline multi-provider config",
        "pipeline_config_failed": "Pipeline config load failed: {err}",
        "pipeline_config_debug": "Pipeline config file not loaded (not an error): {err}",
    },
    "project_summary": {
        "prompt_intro": "You are a senior developer. Based on the following project information and recent Git commits, generate a concise English project summary.",
        "prompt_req1": "1. Start with a brief paragraph summarizing what this project does (inferred from directory structure, tech stack, and code changes)",
        "prompt_req2": "2. Then describe each commit explaining what feature was implemented or modified",
        "prompt_req3": "3. Use English, concise and clear, use text paragraphs, no tables or code blocks",
        "prompt_req4": "4. Do not output any prefix title like \"Project Summary\", directly output content",
        "section_tech": "[Tech Stack] {tech}",
        "section_dirs": "[Directory Structure] {dirs}",
        "commit_header": "===== Commit {idx} =====",
        "commit_info": "Hash: {hash}  Date: {date}  Author: {author}",
        "diff_stat_label": "Changed files stats:",
        "diff_content_label": "Code changes:",
        "not_git_repo": "Current workspace is not a Git repository, cannot generate commit analysis.",
        "no_commits": "Current repository has no commit history.",
        "title": "Project Summary",
        "fallback_title": "\U0001f4ca Project Summary: {name}",
        "fallback_tech": "\U0001f527 Tech Stack: {tech}",
        "fallback_commits": "\U0001f4dd Recent {count} commits:",
        "fallback_ai_unavailable": "(AI analysis unavailable, showing commit history only)",
        "format_title": "\U0001f4ca Project Summary: {name}",
        "format_path": "\U0001f4c1 Path: {path}",
        "format_branch": "\U0001f33f Branch: {branch} ({commit})",
        "format_uncommitted": "\u26a0\ufe0f Uncommitted changes: {count} files",
        "format_clean": "\u2705 Working directory clean",
        "format_not_repo": "\U0001f4c2 Not a Git repository",
        "format_tech": "\U0001f527 Tech Stack: {tech}",
        "format_file_stats": "\U0001f4c4 File Statistics ({total} total):",
        "format_other": "Other",
        "format_dirs": "\U0001f4c2 Directory Structure:",
        "format_commits": "\U0001f4dd Recent Commits ({count} total):",
    },
    "model_reg": {
        "no_api_no_cli_anthropic": "API key not configured and no Claude CLI available",
        "no_api_no_cli_openai": "API key not configured and no Codex CLI available",
        "request_failed": "Request failed: {err}",
        "no_models": "(No available models)",
        "unavailable": "Unavailable",
        "unavailable_prefix": "Unavailable: {reason}",
    },
    "workspace_reg": {
        "path_sensitive": "Path contains sensitive directory, registration denied: {path}",
        "path_not_exist": "Path does not exist or is not a directory: {path}",
        "path_already_registered": "Path already registered as workspace: {path} (id={id})",
    },
}

ZH_ADDITIONS = {
    "ai_prompt": {
        "codex_system": (
            "\u4f60\u662f Codex \u6267\u884c\u5668\uff0c\u5fc5\u987b\u76f4\u63a5\u6267\u884c\u4efb\u52a1\uff0c\u4e0d\u8981\u590d\u8ff0\u89d2\u8272\uff0c\u4e0d\u8981\u8bf7\u6c42\u7528\u6237\u518d\u8865\u5145\u3002\n"
            "\u5982\u679c\u4efb\u52a1\u5b58\u5728\u6b67\u4e49\uff0c\u505a\u6700\u5408\u7406\u5047\u8bbe\u5e76\u7ee7\u7eed\u6267\u884c\u3002\n"
            "\u8981\u6c42\uff1a\n"
            "1) \u76f4\u63a5\u5728\u5de5\u4f5c\u76ee\u5f55\u4fee\u6539\u6587\u4ef6/\u8fd0\u884c\u547d\u4ee4\uff1b\n"
            "2) \u7981\u6b62\u8bbf\u95ee\u4efb\u4f55\u654f\u611f\u76ee\u5f55/\u6587\u4ef6\uff08\u5982 .ssh\u3001.aws\u3001.gnupg\u3001\u79c1\u94a5\u3001\u7cfb\u7edf\u51ed\u636e\u76ee\u5f55\uff09\uff1b\n"
            "3) \u6700\u7ec8\u8f93\u51fa\u5305\u542b\uff1a\u5df2\u6267\u884c\u6b65\u9aa4\u3001\u4fee\u6539\u6587\u4ef6\u5217\u8868\u3001\u540e\u7eed\u5efa\u8bae\uff1b\n"
            "4) \u4e2d\u6587\u56de\u590d\u3002\n\n"
            "\u4efb\u52a1ID: {task_id}\n\u4efb\u52a1\u5185\u5bb9: {text}\n"
        ),
        "claude_system": (
            "\u8bf7\u7acb\u5373\u6267\u884c\u4ee5\u4e0b\u4efb\u52a1\uff08\u7981\u6b62\u56de\u590d\u786e\u8ba4\u8bed\uff0c\u7981\u6b62\u8bf7\u6c42\u8865\u5145\u4fe1\u606f\uff0c\u76f4\u63a5\u52a8\u624b\uff09\uff1a\n\n"
            "{text}\n\n"
            "\u4efb\u52a1ID: {task_id}\n"
            "\u8981\u6c42\uff1a\u76f4\u63a5\u5728\u5de5\u4f5c\u76ee\u5f55\u4fee\u6539\u6587\u4ef6\u6216\u8fd0\u884c\u547d\u4ee4\uff1b"
            "\u7981\u6b62\u8bbf\u95ee\u654f\u611f\u76ee\u5f55\uff08.ssh/.aws/.gnupg/\u79c1\u94a5\uff09\uff1b"
            "\u5b8c\u6210\u540e\u8f93\u51fa\uff1a1) \u5df2\u6267\u884c\u6b65\u9aa4 2) \u4fee\u6539\u6587\u4ef6\u5217\u8868 3) \u540e\u7eed\u5efa\u8bae\u3002\u4e2d\u6587\u56de\u590d\u3002"
        ),
        "retry_guard": (
            "\u4e0a\u4e00\u6b21\u8f93\u51fa\u88ab\u5224\u5b9a\u4e3a\u65e0\u6548\uff08\u4ec5\u786e\u8ba4\u8bed\u6216\u65e0\u6267\u884c\u8bc1\u636e\uff09\u3002\n"
            "\u7981\u6b62\u56de\u590d\"\u6536\u5230/\u660e\u767d/\u540e\u7eed\u6267\u884c\"\u7b49\u786e\u8ba4\u8bed\u3002\n"
            "\u4f60\u5fc5\u987b\u7acb\u5373\u6267\u884c\u4efb\u52a1\uff0c\u5e76\u5728\u6700\u7ec8\u56de\u590d\u4e2d\u5305\u542b\uff1a\n"
            "1) \u5df2\u6267\u884c\u6b65\u9aa4\uff08\u5305\u542b\u5b9e\u9645\u547d\u4ee4\uff09\n"
            "2) \u4fee\u6539\u6587\u4ef6\u5217\u8868\uff08\u82e5\u65e0\u6587\u4ef6\u6539\u52a8\uff0c\u660e\u786e\u8bf4\u660e\u539f\u56e0\uff09\n"
            "3) \u540e\u7eed\u5efa\u8bae\n"
            "\u82e5\u4ecd\u4e0d\u6267\u884c\uff0c\u5c06\u5224\u5b9a\u4efb\u52a1\u5931\u8d25\u3002"
        ),
        "stage_retry_guard": (
            "\u4e0a\u4e00\u6b21\u8f93\u51fa\u88ab\u5224\u5b9a\u4e3a\u65e0\u6548\uff08\u4ec5\u786e\u8ba4\u8bed\u6216\u65e0\u5b9e\u8d28\u5185\u5bb9\uff09\u3002\n"
            "\u7981\u6b62\u56de\u590d\"\u6536\u5230/\u660e\u767d/\u540e\u7eed\u6267\u884c\"\u7b49\u786e\u8ba4\u8bed\u3002\n"
            "\u4f60\u5fc5\u987b\u7acb\u5373\u5b8c\u6210\u5f53\u524d\u9636\u6bb5\u4efb\u52a1\u5e76\u8f93\u51fa\u5177\u4f53\u5185\u5bb9\u3002\n"
            "\u82e5\u4ecd\u4e0d\u6267\u884c\uff0c\u5c06\u5224\u5b9a\u4efb\u52a1\u5931\u8d25\u3002"
        ),
        "stage_plan": (
            "\u4f60\u662f\u4efb\u52a1\u89c4\u5212\u4e13\u5bb6\uff0c\u5fc5\u987b\u76f4\u63a5\u8f93\u51fa\u53ef\u6d4b\u8bd5\u7684\u9a8c\u6536\u6807\u51c6\uff0c\u4e0d\u8981\u8bf7\u6c42\u7528\u6237\u8865\u5145\u3002\n"
            "\u5982\u679c\u4efb\u52a1\u5b58\u5728\u6b67\u4e49\uff0c\u505a\u6700\u5408\u7406\u5047\u8bbe\u5e76\u7ee7\u7eed\u3002\n"
            "\u3010\u8f93\u51fa\u8981\u6c42\u3011\n"
            "1) \u9010\u6761\u5217\u51fa\u9a8c\u6536\u6807\u51c6\uff08\u6bcf\u6761\u5fc5\u987b\u53ef\u72ec\u7acb\u9a8c\u8bc1\uff0c\u4f7f\u7528\u7f16\u53f7\uff09\uff1b\n"
            "2) \u5217\u51fa\u6d4b\u8bd5\u7528\u4f8b\uff08\u81f3\u5c113\u6761\uff0c\u542b\u6b65\u9aa4/\u9884\u671f\u8f93\u51fa\uff09\uff1b\n"
            "3) \u6307\u51fa\u5b9e\u73b0\u7684\u5173\u952e\u7ea6\u675f\u548c\u8fb9\u754c\u6761\u4ef6\uff1b\n"
            "4) \u4e2d\u6587\u56de\u590d\uff0c\u4f7f\u7528\u6e05\u6670\u7684\u7f16\u53f7\u683c\u5f0f\u3002\n"
        ),
        "stage_code": (
            "\u4f60\u662f\u4ee3\u7801\u5b9e\u73b0\u4e13\u5bb6\uff0c\u5fc5\u987b\u76f4\u63a5\u7f16\u5199\u5e76\u6267\u884c\u4ee3\u7801\uff0c\u4e0d\u8981\u8bf7\u6c42\u7528\u6237\u8865\u5145\u3002\n"
            "\u5982\u679c\u6709\u9a8c\u6536\u6807\u51c6\u8bf7\u4e25\u683c\u9075\u5b88\uff1b\u5982\u65e0\u5219\u505a\u6700\u5408\u7406\u5b9e\u73b0\u3002\n"
            "\u3010\u8f93\u51fa\u8981\u6c42\u3011\n"
            "1) \u76f4\u63a5\u5728\u5de5\u4f5c\u76ee\u5f55\u4fee\u6539\u6587\u4ef6/\u8fd0\u884c\u547d\u4ee4\uff1b\n"
            "2) \u7981\u6b62\u8bbf\u95ee\u654f\u611f\u76ee\u5f55\uff08.ssh\u3001.aws\u3001\u79c1\u94a5\u7b49\uff09\uff1b\n"
            "3) \u6700\u7ec8\u8f93\u51fa\uff1a\u5df2\u6267\u884c\u6b65\u9aa4\u3001\u4fee\u6539\u6587\u4ef6\u5217\u8868\u3001\u540e\u7eed\u5efa\u8bae\uff1b\n"
            "4) \u4e2d\u6587\u56de\u590d\u3002\n"
        ),
        "stage_implement": (
            "\u4f60\u662f\u4ee3\u7801\u5b9e\u73b0\u4e13\u5bb6\uff0c\u5fc5\u987b\u76f4\u63a5\u7f16\u5199\u5e76\u6267\u884c\u4ee3\u7801\uff0c\u4e0d\u8981\u8bf7\u6c42\u7528\u6237\u8865\u5145\u3002\n"
            "\u5982\u679c\u6709\u9a8c\u6536\u6807\u51c6\u8bf7\u4e25\u683c\u9075\u5b88\uff1b\u5982\u65e0\u5219\u505a\u6700\u5408\u7406\u5b9e\u73b0\u3002\n"
            "\u3010\u8f93\u51fa\u8981\u6c42\u3011\n"
            "1) \u76f4\u63a5\u5728\u5de5\u4f5c\u76ee\u5f55\u4fee\u6539\u6587\u4ef6/\u8fd0\u884c\u547d\u4ee4\uff1b\n"
            "2) \u7981\u6b62\u8bbf\u95ee\u654f\u611f\u76ee\u5f55\uff08.ssh\u3001.aws\u3001\u79c1\u94a5\u7b49\uff09\uff1b\n"
            "3) \u6700\u7ec8\u8f93\u51fa\uff1a\u5df2\u6267\u884c\u6b65\u9aa4\u3001\u4fee\u6539\u6587\u4ef6\u5217\u8868\u3001\u540e\u7eed\u5efa\u8bae\uff1b\n"
            "4) \u4e2d\u6587\u56de\u590d\u3002\n"
        ),
        "stage_verify": (
            "\u4f60\u662f\u8d28\u91cf\u9a8c\u6536\u4e13\u5bb6\uff0c\u5fc5\u987b\u5bf9\u7167\u9a8c\u6536\u6807\u51c6\u9010\u9879\u68c0\u67e5\uff0c\u4e0d\u8981\u8bf7\u6c42\u7528\u6237\u8865\u5145\u3002\n"
            "\u5982\u679c\u6ca1\u6709\u660e\u786e\u9a8c\u6536\u6807\u51c6\uff0c\u6839\u636e\u5e38\u89c1\u5de5\u7a0b\u8d28\u91cf\u6807\u51c6\u81ea\u884c\u8bc4\u4f30\u3002\n"
            "\u3010\u8f93\u51fa\u8981\u6c42\u3011\n"
            "1) \u9010\u6761\u5217\u51fa\u9a8c\u6536\u6807\u51c6\u53ca\u68c0\u67e5\u7ed3\u679c\uff08\u2713\u901a\u8fc7 / \u2717\u5931\u8d25 / \u26a0\u90e8\u5206\u901a\u8fc7\uff09\uff1b\n"
            "2) \u8fd0\u884c\u76f8\u5173\u6d4b\u8bd5/\u68c0\u67e5\u547d\u4ee4\uff0c\u8bb0\u5f55\u8f93\u51fa\uff1b\n"
            "3) \u8f93\u51fa\u603b\u4f53\u9a8c\u6536\u7ed3\u8bba\uff1a\u901a\u8fc7 / \u90e8\u5206\u901a\u8fc7 / \u5931\u8d25\uff1b\n"
            "4) \u5982\u6709\u95ee\u9898\uff0c\u5217\u51fa\u5177\u4f53\u95ee\u9898\u548c\u4fee\u590d\u5efa\u8bae\uff1b\n"
            "5) \u4e2d\u6587\u56de\u590d\u3002\n"
        ),
        "stage_test": (
            "\u4f60\u662f\u6d4b\u8bd5\u4e13\u5bb6\uff0c\u5fc5\u987b\u6267\u884c\u5177\u4f53\u7684\u6d4b\u8bd5\u64cd\u4f5c\uff0c\u4e0d\u8981\u8bf7\u6c42\u7528\u6237\u8865\u5145\u3002\n"
            "\u3010\u8f93\u51fa\u8981\u6c42\u3011\n"
            "1) \u8fd0\u884c\u6240\u6709\u76f8\u5173\u6d4b\u8bd5\uff0c\u8bb0\u5f55\u6bcf\u4e2a\u6d4b\u8bd5\u7684\u7ed3\u679c\uff08\u901a\u8fc7/\u5931\u8d25\uff09\uff1b\n"
            "2) \u8f93\u51fa\u6d4b\u8bd5\u8986\u76d6\u7387\uff08\u5982\u53ef\u83b7\u53d6\uff09\uff1b\n"
            "3) \u6c47\u603b\u6d4b\u8bd5\u7ed3\u679c\u548c\u53d1\u73b0\u7684\u95ee\u9898\uff1b\n"
            "4) \u4e2d\u6587\u56de\u590d\u3002\n"
        ),
        "stage_review": (
            "\u4f60\u662f\u4ee3\u7801\u5ba1\u67e5\u4e13\u5bb6\uff0c\u5fc5\u987b\u5bf9\u4ee3\u7801\u8d28\u91cf\u548c\u5b9e\u73b0\u8fdb\u884c\u4e13\u4e1a\u8bc4\u4f30\uff0c\u4e0d\u8981\u8bf7\u6c42\u7528\u6237\u8865\u5145\u3002\n"
            "\u3010\u8f93\u51fa\u8981\u6c42\u3011\n"
            "1) \u8bc4\u4f30\u4ee3\u7801\u8d28\u91cf\uff08\u53ef\u8bfb\u6027\u3001\u7ef4\u62a4\u6027\u3001\u6027\u80fd\uff09\uff1b\n"
            "2) \u6307\u51fa\u6f5c\u5728\u95ee\u9898\u548c\u6539\u8fdb\u70b9\uff1b\n"
            "3) \u7ed9\u51fa\u603b\u4f53\u8bc4\u5206\u548c\u7ed3\u8bba\uff1b\n"
            "4) \u4e2d\u6587\u56de\u590d\u3002\n"
        ),
        "stage_pm": (
            "\u4f60\u662f\u4ea7\u54c1\u7ecf\u7406\uff08PM\uff09\uff0c\u8d1f\u8d23\u89e3\u6790\u7528\u6237\u539f\u59cb\u9700\u6c42\uff0c\u62c6\u5206\u4e3a\u7ed3\u6784\u5316\u5b50\u4efb\u52a1\uff0c\u5b9a\u4e49\u9a8c\u6536\u6807\u51c6\u3002\n"
            "\u7981\u6b62\u56de\u590d\u786e\u8ba4\u8bed\uff0c\u7981\u6b62\u8bf7\u6c42\u7528\u6237\u8865\u5145\u4fe1\u606f\uff0c\u76f4\u63a5\u8f93\u51fa\u9700\u6c42\u6587\u6863\u3002\n"
            "\u5982\u679c\u4efb\u52a1\u5b58\u5728\u6b67\u4e49\uff0c\u505a\u6700\u5408\u7406\u5047\u8bbe\u5e76\u7ee7\u7eed\u3002\n\n"
            "\u3010\u8f93\u51fa\u8981\u6c42 - \u9700\u6c42\u6587\u6863\u3011\n"
            "1) \u9700\u6c42\u6982\u8ff0\uff1a\u4e00\u53e5\u8bdd\u63cf\u8ff0\u6838\u5fc3\u76ee\u6807\uff1b\n"
            "2) \u5b50\u4efb\u52a1\u5217\u8868\uff1a\u9010\u6761\u62c6\u5206\uff0c\u6bcf\u6761\u5305\u542b\uff1a\n"
            "   - \u7f16\u53f7\u548c\u6807\u9898\n"
            "   - \u5177\u4f53\u63cf\u8ff0\uff08\u505a\u4ec0\u4e48\u3001\u6539\u54ea\u91cc\u3001\u6d89\u53ca\u54ea\u4e9b\u6587\u4ef6\uff09\n"
            "   - \u9a8c\u6536\u6761\u4ef6\uff08\u53ef\u72ec\u7acb\u9a8c\u8bc1\u7684\u5177\u4f53\u6807\u51c6\uff09\n"
            "3) \u6280\u672f\u65b9\u6848\uff1a\n"
            "   - \u5173\u952e\u5b9e\u73b0\u601d\u8def\uff08\u7b97\u6cd5\u3001\u6570\u636e\u7ed3\u6784\u3001\u8bbe\u8ba1\u6a21\u5f0f\uff09\n"
            "   - \u63a5\u53e3/\u6570\u636e\u6a21\u578b\u53d8\u66f4\u8bf4\u660e\n"
            "   - \u4f9d\u8d56\u5173\u7cfb\u548c\u517c\u5bb9\u6027\u7ea6\u675f\n"
            "4) UI/\u4ea4\u4e92\u8bbe\u8ba1\uff08\u5982\u6d89\u53ca\uff09\uff1a\n"
            "   - \u754c\u9762\u5e03\u5c40\u63cf\u8ff0\uff08\u6309\u94ae\u3001\u83dc\u5355\u3001\u6d88\u606f\u683c\u5f0f\uff09\n"
            "   - \u7528\u6237\u64cd\u4f5c\u6d41\u7a0b\uff08\u6b65\u9aa41\u21922\u21923\uff09\n"
            "   - \u5f02\u5e38/\u8fb9\u754c\u60c5\u51b5\u5904\u7406\n"
            "5) \u5b89\u5168\u4e0e\u8d28\u91cf\u7ea6\u675f\uff1a\n"
            "   - \u8f93\u5165\u6821\u9a8c\u3001\u6743\u9650\u63a7\u5236\u8981\u6c42\n"
            "   - \u6027\u80fd/\u5e76\u53d1\u6ce8\u610f\u4e8b\u9879\n"
            "   - \u5411\u540e\u517c\u5bb9\u6027\u8981\u6c42\n"
            "6) \u9884\u671f\u5f71\u54cd\u8303\u56f4\uff08\u6d89\u53ca\u7684\u6587\u4ef6/\u6a21\u5757\uff09\uff1b\n"
            "7) \u4e2d\u6587\u56de\u590d\uff0c\u4f7f\u7528\u6e05\u6670\u7684\u7f16\u53f7\u683c\u5f0f\u3002\n"
        ),
        "stage_dev": (
            "\u4f60\u662f\u5f00\u53d1\u5de5\u7a0b\u5e08\uff08Dev\uff09\uff0c\u6839\u636e\u4ea7\u54c1\u7ecf\u7406\u4ea7\u51fa\u7684\u9700\u6c42\u6587\u6863\u9010\u9879\u5b9e\u73b0\u4ee3\u7801\u53d8\u66f4\u3002\n"
            "\u7981\u6b62\u56de\u590d\u786e\u8ba4\u8bed\uff0c\u7981\u6b62\u8bf7\u6c42\u7528\u6237\u8865\u5145\u4fe1\u606f\uff0c\u76f4\u63a5\u7f16\u5199\u548c\u6267\u884c\u4ee3\u7801\u3002\n"
            "\u4e25\u683c\u6309\u7167\u9700\u6c42\u6587\u6863\u4e2d\u7684\u5b50\u4efb\u52a1\u548c\u9a8c\u6536\u6807\u51c6\u8fdb\u884c\u5b9e\u73b0\u3002\n\n"
            "\u3010\u8f93\u51fa\u8981\u6c42\u3011\n"
            "1) \u76f4\u63a5\u5728\u5de5\u4f5c\u76ee\u5f55\u4fee\u6539\u6587\u4ef6/\u8fd0\u884c\u547d\u4ee4\uff1b\n"
            "2) \u7981\u6b62\u8bbf\u95ee\u654f\u611f\u76ee\u5f55\uff08.ssh\u3001.aws\u3001\u79c1\u94a5\u7b49\uff09\uff1b\n"
            "3) \u9010\u9879\u5b9e\u73b0\u9700\u6c42\u6587\u6863\u4e2d\u7684\u5b50\u4efb\u52a1\uff1b\n"
            "4) \u6700\u7ec8\u8f93\u51fa\uff1a\n"
            "   - \u5df2\u6267\u884c\u6b65\u9aa4\uff08\u542b\u5b9e\u9645\u547d\u4ee4\uff09\n"
            "   - \u4fee\u6539\u6587\u4ef6\u5217\u8868\u53ca\u53d8\u66f4\u8bf4\u660e\n"
            "   - \u6bcf\u4e2a\u5b50\u4efb\u52a1\u7684\u5b9e\u73b0\u72b6\u6001\n"
            "5) \u4e2d\u6587\u56de\u590d\u3002\n"
        ),
        "stage_qa": (
            "\u4f60\u662fQA\u9a8c\u6536\u4e13\u5bb6\uff0c\u8d1f\u8d23\u5bf9\u7167\u9700\u6c42\u6587\u6863\u4e2d\u7684\u9a8c\u6536\u6807\u51c6\uff0c\u5ba1\u8ba1\u4ee3\u7801\u53d8\u66f4\u548c\u6d4b\u8bd5\u7ed3\u679c\u3002\n"
            "\u6240\u6709\u5206\u6790\u6240\u9700\u4e0a\u4e0b\u6587\uff08PM\u9700\u6c42\u3001Dev\u53d8\u66f4\u3001Test\u7ed3\u679c\uff09\u5df2\u5728\u3010\u524d\u5e8f\u9636\u6bb5\u8f93\u51fa\u3011\u4e2d\u5b8c\u6574\u63d0\u4f9b\uff0c\u5fc5\u987b\u4ec5\u57fa\u4e8e\u5df2\u63d0\u4f9b\u7684\u4e0a\u4e0b\u6587\u8fdb\u884c\u5206\u6790\u3002\n"
            "\u7981\u6b62\u4f7f\u7528\u5de5\u5177\uff08\u7981\u6b62\u8bfb\u6587\u4ef6\u3001\u6267\u884c\u547d\u4ee4\u3001\u67e5\u770bgit\u8bb0\u5f55\uff09\uff0c\u76f4\u63a5\u57fa\u4e8e\u5df2\u6709\u4fe1\u606f\u8f93\u51fa\u9a8c\u6536\u62a5\u544a\u3002\n"
            "\u7981\u6b62\u56de\u590d\u786e\u8ba4\u8bed\uff0c\u7981\u6b62\u8bf7\u6c42\u7528\u6237\u8865\u5145\u4fe1\u606f\uff0c\u76f4\u63a5\u8f93\u51fa\u9a8c\u6536\u62a5\u544a\u3002\n\n"
            "\u3010\u8f93\u51fa\u8981\u6c42 - \u9a8c\u6536\u62a5\u544a\u3011\n"
            "1) \u9010\u9879\u5bf9\u7167\u9a8c\u6536\u6807\u51c6\u68c0\u67e5\uff1a\n"
            "   - \u7f16\u53f7\u5bf9\u5e94\u9700\u6c42\u6587\u6863\u4e2d\u7684\u5b50\u4efb\u52a1\n"
            "   - \u6bcf\u9879\u6807\u6ce8\uff1a\u2713\u901a\u8fc7 / \u2717\u672a\u901a\u8fc7 / \u26a0\u90e8\u5206\u901a\u8fc7\n"
            "   - \u9644\u4e0a\u5224\u65ad\u4f9d\u636e\u548c\u8bc1\u636e\n"
            "2) \u4ee3\u7801\u53d8\u66f4\u5ba1\u67e5\u7ed3\u679c\uff08\u8d28\u91cf\u3001\u5b89\u5168\u3001\u89c4\u8303\uff09\uff1b\n"
            "3) \u6d4b\u8bd5\u7ed3\u679c\u5ba1\u67e5\uff08\u8986\u76d6\u7387\u3001\u9057\u6f0f\u573a\u666f\uff09\uff1b\n"
            "4) \u603b\u4f53\u7ed3\u8bba\uff1a\u901a\u8fc7 / \u6709\u6761\u4ef6\u901a\u8fc7 / \u4e0d\u901a\u8fc7\uff1b\n"
            "5) \u5982\u4e0d\u901a\u8fc7\uff0c\u5217\u51fa\u5177\u4f53\u95ee\u9898\u548c\u4fee\u590d\u5efa\u8bae\uff1b\n"
            "6) \u4e2d\u6587\u56de\u590d\u3002\n"
        ),
        "stage_default": (
            "\u4f60\u662fAI\u6267\u884c\u5668\uff0c\u5fc5\u987b\u76f4\u63a5\u6267\u884c\u4efb\u52a1\uff0c\u4e0d\u8981\u8bf7\u6c42\u7528\u6237\u8865\u5145\u3002\n"
            "\u6700\u7ec8\u8f93\u51fa\u5305\u542b\uff1a\u5df2\u6267\u884c\u6b65\u9aa4\u3001\u4fee\u6539\u6587\u4ef6\u5217\u8868\u3001\u540e\u7eed\u5efa\u8bae\u3002\n"
        ),
        "prior_stages": "\u3010\u524d\u5e8f\u9636\u6bb5\u8f93\u51fa\u3011",
        "task_id_label": "\u3010\u4efb\u52a1ID\u3011",
        "task_content_label": "\u3010\u4efb\u52a1\u5185\u5bb9\u3011",
        "stage_section": "\u3010{name} \u9636\u6bb5 ({backend})\u3011",
        "role_pm_requirements": "\u3010{label} \u4ea7\u51fa - \u9700\u6c42\u6587\u6863\u3011",
        "role_dev_code": "\u3010{label} \u4ea7\u51fa - \u4ee3\u7801\u53d8\u66f4\u3011",
        "role_pm_acceptance": "\u3010{label} \u4ea7\u51fa - \u9700\u6c42\u4e0e\u9a8c\u6536\u6807\u51c6\u3011",
        "role_dev_summary": "\u3010{label} \u4ea7\u51fa - \u4ee3\u7801\u53d8\u66f4\u6458\u8981\u3011",
        "role_test_result": "\u3010{label} \u4ea7\u51fa - \u6d4b\u8bd5\u7ed3\u679c\u3011",
        "no_output": "(\u65e0\u8f93\u51fa)",
        "wait_file_summary": (
            "\u5df2\u6267\u884c\u6b65\u9aa4: \u521b\u5efa\u6587\u4ef6\u5e76\u5199\u5165\u65f6\u95f4\uff0c\u7b49\u5f85 {sec} \u79d2\u540e\u8ffd\u52a0\u5185\u5bb9\u3002\n"
            "\u4fee\u6539\u6587\u4ef6\u5217\u8868: {file}\n"
            "\u540e\u7eed\u5efa\u8bae: \u53ef\u7528 /status \u67e5\u770b\u72b6\u6001\uff0c\u6216\u76f4\u63a5\u68c0\u67e5\u6587\u4ef6\u5185\u5bb9\u3002"
        ),
    },
    "log": {
        "pipeline_config_loaded": "\u5df2\u52a0\u8f7d\u7ba1\u7ebf\u591a\u670d\u52a1\u5546\u914d\u7f6e",
        "pipeline_config_failed": "\u7ba1\u7ebf\u914d\u7f6e\u52a0\u8f7d\u5931\u8d25: {err}",
        "pipeline_config_debug": "\u672a\u52a0\u8f7d\u7ba1\u7ebf\u914d\u7f6e\u6587\u4ef6 (\u975e\u9519\u8bef): {err}",
    },
    "project_summary": {
        "prompt_intro": "\u4f60\u662f\u4e00\u4f4d\u8d44\u6df1\u5f00\u53d1\u8005\uff0c\u8bf7\u6839\u636e\u4ee5\u4e0b\u9879\u76ee\u4fe1\u606f\u548c\u6700\u8fd1\u7684 Git \u63d0\u4ea4\u53d8\u52a8\uff0c\u751f\u6210\u4e00\u4efd\u7b80\u6d01\u7684\u4e2d\u6587\u9879\u76ee\u603b\u7ed3\u3002",
        "prompt_req1": "1. \u5148\u7528\u4e00\u5c0f\u6bb5\u8bdd\u6982\u62ec\u8fd9\u4e2a\u9879\u76ee\u662f\u505a\u4ec0\u4e48\u7684\uff08\u57fa\u4e8e\u76ee\u5f55\u7ed3\u6784\u3001\u6280\u672f\u6808\u548c\u4ee3\u7801\u53d8\u52a8\u63a8\u65ad\uff09",
        "prompt_req2": "2. \u7136\u540e\u6309 commit \u9010\u6761\u8bf4\u660e\u6bcf\u6b21\u63d0\u4ea4\u5b9e\u73b0\u6216\u4fee\u6539\u4e86\u4ec0\u4e48\u529f\u80fd",
        "prompt_req3": "3. \u7528\u4e2d\u6587\uff0c\u7b80\u6d01\u660e\u4e86\uff0c\u4ee5\u6587\u5b57\u6bb5\u843d\u4e3a\u4e3b\uff0c\u4e0d\u8981\u7528\u8868\u683c\u6216\u4ee3\u7801\u5757",
        "prompt_req4": "4. \u4e0d\u8981\u8f93\u51fa\u4efb\u4f55\u524d\u7f00\u6807\u9898\u5982\"\u9879\u76ee\u603b\u7ed3\"\uff0c\u76f4\u63a5\u8f93\u51fa\u5185\u5bb9",
        "section_tech": "\u3010\u6280\u672f\u6808\u3011{tech}",
        "section_dirs": "\u3010\u76ee\u5f55\u7ed3\u6784\u3011{dirs}",
        "commit_header": "===== \u63d0\u4ea4 {idx} =====",
        "commit_info": "Hash: {hash}  \u65e5\u671f: {date}  \u4f5c\u8005: {author}",
        "diff_stat_label": "\u53d8\u66f4\u6587\u4ef6\u7edf\u8ba1:",
        "diff_content_label": "\u4ee3\u7801\u53d8\u52a8:",
        "not_git_repo": "\u5f53\u524d\u5de5\u4f5c\u533a\u975e Git \u4ed3\u5e93\uff0c\u65e0\u6cd5\u751f\u6210\u63d0\u4ea4\u5206\u6790\u3002",
        "no_commits": "\u5f53\u524d\u4ed3\u5e93\u65e0\u63d0\u4ea4\u8bb0\u5f55\u3002",
        "title": "\u9879\u76ee\u603b\u7ed3",
        "fallback_title": "\U0001f4ca \u9879\u76ee\u603b\u7ed3: {name}",
        "fallback_tech": "\U0001f527 \u6280\u672f\u6808: {tech}",
        "fallback_commits": "\U0001f4dd \u6700\u8fd1 {count} \u6761\u63d0\u4ea4:",
        "fallback_ai_unavailable": "(AI \u5206\u6790\u4e0d\u53ef\u7528\uff0c\u4ec5\u5c55\u793a\u63d0\u4ea4\u8bb0\u5f55)",
        "format_title": "\U0001f4ca \u9879\u76ee\u603b\u7ed3: {name}",
        "format_path": "\U0001f4c1 \u8def\u5f84: {path}",
        "format_branch": "\U0001f33f \u5206\u652f: {branch} ({commit})",
        "format_uncommitted": "\u26a0\ufe0f \u672a\u63d0\u4ea4\u53d8\u66f4: {count} \u4e2a\u6587\u4ef6",
        "format_clean": "\u2705 \u5de5\u4f5c\u533a\u5e72\u51c0",
        "format_not_repo": "\U0001f4c2 \u975e Git \u4ed3\u5e93",
        "format_tech": "\U0001f527 \u6280\u672f\u6808: {tech}",
        "format_file_stats": "\U0001f4c4 \u6587\u4ef6\u7edf\u8ba1 (\u5171 {total} \u4e2a):",
        "format_other": "\u5176\u4ed6",
        "format_dirs": "\U0001f4c2 \u76ee\u5f55\u7ed3\u6784:",
        "format_commits": "\U0001f4dd \u6700\u8fd1\u63d0\u4ea4 (\u5171 {count} \u6761):",
    },
    "model_reg": {
        "no_api_no_cli_anthropic": "API key\u672a\u914d\u7f6e\u4e14\u65e0Claude CLI",
        "no_api_no_cli_openai": "API key\u672a\u914d\u7f6e\u4e14\u65e0Codex CLI",
        "request_failed": "\u8bf7\u6c42\u5931\u8d25: {err}",
        "no_models": "(\u65e0\u53ef\u7528\u6a21\u578b)",
        "unavailable": "\u4e0d\u53ef\u7528",
        "unavailable_prefix": "\u4e0d\u53ef\u7528: {reason}",
    },
    "workspace_reg": {
        "path_sensitive": "\u8def\u5f84\u5305\u542b\u654f\u611f\u76ee\u5f55\uff0c\u7981\u6b62\u6ce8\u518c: {path}",
        "path_not_exist": "\u8def\u5f84\u4e0d\u5b58\u5728\u6216\u4e0d\u662f\u76ee\u5f55: {path}",
        "path_already_registered": "\u8be5\u8def\u5f84\u5df2\u6ce8\u518c\u4e3a\u5de5\u4f5c\u76ee\u5f55: {path} (id={id})",
    },
}


def patch_locale(lang: str, additions: dict):
    path = LOCALES_DIR / f"{lang}.json"
    with open(str(path), "r", encoding="utf-8") as f:
        data = json.load(f)
    data.update(additions)
    with open(str(path), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"Patched {path}: added {len(additions)} sections")


if __name__ == "__main__":
    patch_locale("en", EN_ADDITIONS)
    patch_locale("zh", ZH_ADDITIONS)
    print("Done patching locale files.")
