#!/usr/bin/env python3
"""
i18n Transform Pass 4: Replace remaining ~160 unicode-escaped Chinese strings
in bot_commands.py with t() calls, and add corresponding locale keys.
"""
import json
import re
import sys
import os

agent_dir = os.path.dirname(os.path.abspath(__file__))
bc_path = os.path.join(agent_dir, "bot_commands.py")
zh_path = os.path.join(agent_dir, "locales", "zh.json")
en_path = os.path.join(agent_dir, "locales", "en.json")

# ============================================================
# Step 1: Add new locale keys
# ============================================================
NEW_ZH_KEYS = {
    "msg": {
        "pipeline_exec_header": "\u2699\ufe0f \u6d41\u6c34\u7ebf\u6267\u884c\u8be6\u60c5:",
        "stage_line": "  {idx}. {emoji} {label} \u2192 {model_display} {status_icon}{time_str}",
        "stage_detail_header": "\U0001f50d \u9636\u6bb5\u8be6\u60c5 [{code}]",
        "stage_detail_line": "\n{emoji} {idx}. {label} {status_icon} \u2192 {model_display}{time_str}",
        "model_list_header": "\U0001f4cb \u6a21\u578b\u6e05\u5355\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n",
        "model_list_footer": "\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u9ed8\u8ba4\u6a21\u578b: {default_model}",
        "pipeline_config_panel": "\u2699\ufe0f \u6d41\u6c34\u7ebf\u914d\u7f6e\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u70b9\u51fb\u9884\u8bbe\u76f4\u63a5\u5e94\u7528\uff0c\u6216\u9009\u62e9\u81ea\u5b9a\u4e49:\n\n{preset_info}",
        "role_pipeline_panel": "{title}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{role_set}\n\n{config}",
        "role_pipeline_view": "{title}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{config}",
        "ws_remove_panel": "\u2796 \u5220\u9664\u5de5\u4f5c\u76ee\u5f55\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u8bf7\u9009\u62e9\u8981\u5220\u9664\u7684\u5de5\u4f5c\u76ee\u5f55:",
        "ws_set_default_panel": "\u2b50 \u8bbe\u7f6e\u9ed8\u8ba4\u5de5\u4f5c\u76ee\u5f55\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u8bf7\u9009\u62e9\u8981\u8bbe\u7f6e\u4e3a\u9ed8\u8ba4\u7684\u5de5\u4f5c\u76ee\u5f55:",
        "search_roots_has_items": "\U0001f50d \u641c\u7d22\u6839\u76ee\u5f55\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{items}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u70b9\u51fb \u2796 \u5220\u9664\uff0c\u6216\u70b9 \u2795 \u6dfb\u52a0\u65b0\u6839\u76ee\u5f55",
        "search_roots_empty": "\U0001f50d \u641c\u7d22\u6839\u76ee\u5f55\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u5c1a\u672a\u914d\u7f6e\u641c\u7d22\u6839\u76ee\u5f55\u3002\n\u9ed8\u8ba4\u4f7f\u7528\u5f53\u524d\u6d3b\u8dc3\u5de5\u4f5c\u76ee\u5f55\u53ca\u5176\u7236\u76ee\u5f55\u3002\n\n\u70b9\u51fb \u2795 \u6dfb\u52a0\u641c\u7d22\u6839\u76ee\u5f55\uff0c\u6269\u5927\u6a21\u7cca\u641c\u7d22\u8303\u56f4\u3002",
        "no_queued_tasks_all": "\u5f53\u524d\u6240\u6709\u5de5\u4f5c\u533a\u57df\u65e0\u6392\u961f\u4efb\u52a1\u3002",
        "ws_queue_header": "\U0001f4ca \u5de5\u4f5c\u533a\u57df\u4efb\u52a1\u961f\u5217:",
        "ws_queue_section": "\n{label} ({count}\u4e2a\u6392\u961f):",
        "task_overview_panel": "\U0001f4ca \u4efb\u52a1\u6982\u89c8\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u6d3b\u8dc3\u4efb\u52a1\u603b\u6570: {total}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u70b9\u51fb\u4e0b\u65b9\u6309\u94ae\u67e5\u770b\u5bf9\u5e94\u7c7b\u522b:",
        "no_tasks_in_category": "\u5f53\u524d\u65e0{label}\u4efb\u52a1\u3002",
        "task_list_header": "{label}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u5171 {count} \u4e2a\u4efb\u52a1\uff0c\u70b9\u51fb\u67e5\u770b\u8be6\u60c5:",
        "no_tasks_current": "\u5f53\u524d\u65e0\u4efb\u52a1\u3002",
        "task_list_paged": "{label}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u5171 {count} \u4e2a\u4efb\u52a1\uff08\u7b2c {page} \u9875\uff09:",
        "task_detail_snapshot": "{emoji} \u4efb\u52a1\u8be6\u60c5\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u4ee3\u53f7: {code}\n\u72b6\u6001: {status}\n\u521b\u5efa\u65f6\u95f4: {created}\n\u8fed\u4ee3\u6b21\u6570: {iteration}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u63cf\u8ff0: {text}\n\u6982\u8981: {summary}",
        "task_detail_full": "{emoji} \u4efb\u52a1\u8be6\u60c5\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u4ee3\u53f7: {code}\n\u72b6\u6001: {status}\n\u521b\u5efa\u65f6\u95f4: {created}\n\u8fed\u4ee3\u6b21\u6570: {iteration}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u63cf\u8ff0: {text}\n\u6267\u884c\u6982\u8981: {summary}",
        "rejection_reason_append": "\n\u62d2\u7edd\u539f\u56e0: {reason}",
        "git_checkpoint_append": "\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nGit\u68c0\u67e5\u70b9: {ckpt}",
        "separator_line": "\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n",
        "task_doc_panel": "\U0001f4c4 \u4efb\u52a1\u6587\u6863 [{ref}]\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{content}",
        "task_summary_panel": "\U0001f4d1 \u4efb\u52a1\u6982\u8981 [{ref}]\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{content}",
        "task_log_caption": "\u6267\u884c\u65e5\u5fd7 [{ref}]",
        "task_log_panel": "\U0001f4dc \u6267\u884c\u65e5\u5fd7 [{ref}]\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{content}",
        "archive_detail_panel": "\U0001f4c1 \u5f52\u6863\u8be6\u60c5\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u4efb\u52a1\u4ee3\u53f7: {code}\n\u5f52\u6863ID: {archive_id}\n\u72b6\u6001: {status}\n\u7c7b\u578b: {action}\n\u5b8c\u6210\u65f6\u95f4: {completed}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u63cf\u8ff0: {text}\n\u6982\u8981: {summary}",
        "btn_view_archive_detail": "\U0001f4c4 \u67e5\u770b\u5f52\u6863\u8be6\u60c5",
        "btn_view_log": "\U0001f4dc \u67e5\u770b\u6267\u884c\u65e5\u5fd7",
        "btn_view_accept_doc": "\U0001f4c4 \u67e5\u770b\u9a8c\u6536\u6587\u6863",
        "btn_delete_archive": "\U0001f5d1 \u5220\u9664\u5f52\u6863\u8bb0\u5f55",
        "btn_back_list": "\u00ab \u8fd4\u56de\u5217\u8868",
        "task_created_simple": "\u4efb\u52a1\u5df2\u521b\u5efa: [{code}] {task_id}\n\u72b6\u6001: pending\n\u5185\u5bb9: {text}",
        "ws_deleted_fallback": "\u26a0\ufe0f \u6240\u9009\u5de5\u4f5c\u533a\u5df2\u88ab\u5220\u9664\uff0c\u5df2\u56de\u9000\u5230\u9ed8\u8ba4\u5de5\u4f5c\u533a: {label}",
        "ws_deleted_no_fallback": "\u274c \u6240\u9009\u5de5\u4f5c\u533a\u5df2\u88ab\u5220\u9664\uff0c\u4e14\u65e0\u53ef\u7528\u5de5\u4f5c\u533a\u3002",
        "task_queued_ws": "\u5de5\u4f5c\u533a [{ws}] \u5f53\u524d\u6709\u4efb\u52a1\u6267\u884c\u4e2d\uff0c\u5df2\u52a0\u5165\u961f\u5217\u3002\n\u961f\u5217\u4f4d\u7f6e: \u7b2c{pos}\u4e2a\n\u5185\u5bb9: {text}\n\n\u524d\u4e00\u4efb\u52a1\u9a8c\u6536\u901a\u8fc7\u540e\u5c06\u81ea\u52a8\u542f\u52a8\u3002",
        "task_created_ws": "\u4efb\u52a1\u5df2\u521b\u5efa: [{code}] {task_id}\n\u5de5\u4f5c\u533a: {ws}\n\u72b6\u6001: pending\n\u5185\u5bb9: {text}",
        "summary_panel": "\U0001f4ca \u9879\u76ee\u603b\u7ed3\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u8bf7\u9009\u62e9\u5de5\u4f5c\u533a:",
        "new_task_ws_panel": "\U0001f4dd \u65b0\u5efa\u4efb\u52a1\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u8bf7\u9009\u62e9\u4efb\u52a1\u6267\u884c\u7684\u5de5\u4f5c\u533a\u57df:",
        "switch_backend_panel": "\U0001f504 \u5207\u6362\u540e\u7aef\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u5f53\u524d\u540e\u7aef: {backend}\n\u8bf7\u9009\u62e9\u65b0\u7684\u540e\u7aef\uff1a",
        "unknown_backend": "\u672a\u77e5\u540e\u7aef: {backend}\n\u53ef\u7528: {available}",
        "pipeline_arrow": "\u2192",
        "no_accept_tasks": "\U0001f4ed \u5f53\u524d\u6ca1\u6709\u5f85\u9a8c\u6536\u7684\u4efb\u52a1\u3002",
        "accept_list_header": "\u2705 \u5f85\u9a8c\u6536\u4efb\u52a1\uff08\u5171 {count} \u4e2a\uff09\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u8bf7\u9009\u62e9\u8981\u9a8c\u6536\u7684\u4efb\u52a1\uff1a",
        "no_reject_tasks": "\U0001f4ed \u5f53\u524d\u6ca1\u6709\u53ef\u62d2\u7edd\u7684\u4efb\u52a1\u3002",
        "reject_list_header": "\u274c \u53ef\u62d2\u7edd\u4efb\u52a1\uff08\u5171 {count} \u4e2a\uff09\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u8bf7\u9009\u62e9\u8981\u62d2\u7edd\u7684\u4efb\u52a1\uff1a",
        "no_retry_tasks": "\U0001f4ed \u5f53\u524d\u6ca1\u6709\u53ef\u91cd\u8bd5\u7684\u4efb\u52a1\u3002",
        "retry_list_header": "\U0001f504 \u53ef\u91cd\u8bd5\u4efb\u52a1\uff08\u5171 {count} \u4e2a\uff09\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u8bf7\u9009\u62e9\u8981\u91cd\u8bd5\u7684\u4efb\u52a1\uff1a",
        "no_cancel_tasks": "\U0001f4ed \u5f53\u524d\u6ca1\u6709\u53ef\u53d6\u6d88\u7684\u4efb\u52a1\u3002",
        "cancel_list_header": "\u26d4 \u53ef\u53d6\u6d88\u4efb\u52a1\uff08\u5171 {count} \u4e2a\uff09\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u8bf7\u9009\u62e9\u8981\u53d6\u6d88\u7684\u4efb\u52a1\uff1a",
        "search_roots_cmd_header": "\U0001f50d \u641c\u7d22\u6839\u76ee\u5f55:",
        "search_roots_usage": "\n\u7528\u6cd5:\n  /workspace_search_roots add <\u8def\u5f84>\n  /workspace_search_roots remove <\u5e8f\u53f7>\n  /workspace_search_roots clear",
        "search_roots_not_configured": "\u5c1a\u672a\u914d\u7f6e\u641c\u7d22\u6839\u76ee\u5f55\u3002\n\u9ed8\u8ba4\u4f7f\u7528\u5f53\u524d\u6d3b\u8dc3\u5de5\u4f5c\u76ee\u5f55\u53ca\u5176\u7236\u76ee\u5f55\u3002\n\n\u7528\u6cd5: /workspace_search_roots add <\u8def\u5f84>",
        "usage_search_roots_add": "\u7528\u6cd5: /workspace_search_roots add <\u8def\u5f84>",
        "search_root_added": "\u2705 \u5df2\u6dfb\u52a0:",
        "search_root_add_failed": "\u274c \u5931\u8d25:",
        "no_valid_paths": "\u65e0\u6709\u6548\u8def\u5f84",
        "usage_search_roots_remove": "\u7528\u6cd5: /workspace_search_roots remove <\u5e8f\u53f7>",
        "index_must_be_number": "\u5e8f\u53f7\u5fc5\u987b\u662f\u6570\u5b57",
        "search_root_removed": "\u2705 \u5df2\u5220\u9664: {msg}",
        "search_root_remove_failed": "\u5220\u9664\u5931\u8d25: {msg}",
        "search_roots_cleared": "\u2705 \u5df2\u6e05\u7a7a\u6240\u6709\u641c\u7d22\u6839\u76ee\u5f55\u3002\n\u5c06\u56de\u9000\u5230\u9ed8\u8ba4\u641c\u7d22\u8303\u56f4\u3002",
        "unknown_subcommand": "\u672a\u77e5\u5b50\u547d\u4ee4: {sub}\n\u7528\u6cd5: add / remove / clear",
        "queue_auto_start": "\U0001f504 \u961f\u5217\u4efb\u52a1\u81ea\u52a8\u542f\u52a8\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\u5de5\u4f5c\u533a: {ws}\n\u4efb\u52a1: [{code}] {task_id}\n\u5185\u5bb9: {text}\n\u72b6\u6001: pending (\u5df2\u52a0\u5165\u6267\u884c\u961f\u5217)\n\u5269\u4f59\u6392\u961f: {remaining}\u4e2a",
        "rejection_reason_doc": "\n\n\u62d2\u7edd\u539f\u56e0: {reason}",
        "error_info_doc": "\n\n\u9519\u8bef\u4fe1\u606f: {err}",
        "preset_arrow": "  {key} \u2192\n{value}",
        "preset_arrow_inline": "  {key} \u2192 {value}",
    },
    "status": {
        "icon_not_executed": "(\u672a\u6267\u884c)",
        "icon_fail": "\u274c",
        "icon_pass": "\u2705",
    },
}

NEW_EN_KEYS = {
    "msg": {
        "pipeline_exec_header": "\u2699\ufe0f Pipeline execution details:",
        "stage_line": "  {idx}. {emoji} {label} \u2192 {model_display} {status_icon}{time_str}",
        "stage_detail_header": "\U0001f50d Stage Detail [{code}]",
        "stage_detail_line": "\n{emoji} {idx}. {label} {status_icon} \u2192 {model_display}{time_str}",
        "model_list_header": "\U0001f4cb Model List\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n",
        "model_list_footer": "\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nDefault model: {default_model}",
        "pipeline_config_panel": "\u2699\ufe0f Pipeline Config\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nClick preset to apply, or choose custom:\n\n{preset_info}",
        "role_pipeline_panel": "{title}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{role_set}\n\n{config}",
        "role_pipeline_view": "{title}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{config}",
        "ws_remove_panel": "\u2796 Remove Workspace\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nSelect workspace to remove:",
        "ws_set_default_panel": "\u2b50 Set Default Workspace\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nSelect workspace to set as default:",
        "search_roots_has_items": "\U0001f50d Search Roots\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{items}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nClick \u2796 to remove, or \u2795 to add new root",
        "search_roots_empty": "\U0001f50d Search Roots\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nNo search roots configured.\nDefault: current workspace and parent.\n\nClick \u2795 to add search root, expand search scope.",
        "no_queued_tasks_all": "No queued tasks in any workspace.",
        "ws_queue_header": "\U0001f4ca Workspace Task Queue:",
        "ws_queue_section": "\n{label} ({count} queued):",
        "task_overview_panel": "\U0001f4ca Task Overview\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nActive tasks: {total}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nClick below to view by category:",
        "no_tasks_in_category": "No {label} tasks.",
        "task_list_header": "{label}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{count} tasks, click for details:",
        "no_tasks_current": "No tasks.",
        "task_list_paged": "{label}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{count} tasks (page {page}):",
        "task_detail_snapshot": "{emoji} Task Detail\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nCode: {code}\nStatus: {status}\nCreated: {created}\nIteration: {iteration}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nDescription: {text}\nSummary: {summary}",
        "task_detail_full": "{emoji} Task Detail\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nCode: {code}\nStatus: {status}\nCreated: {created}\nIteration: {iteration}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nDescription: {text}\nExecution summary: {summary}",
        "rejection_reason_append": "\nRejection reason: {reason}",
        "git_checkpoint_append": "\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nGit checkpoint: {ckpt}",
        "separator_line": "\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n",
        "task_doc_panel": "\U0001f4c4 Task Document [{ref}]\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{content}",
        "task_summary_panel": "\U0001f4d1 Task Summary [{ref}]\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{content}",
        "task_log_caption": "Execution log [{ref}]",
        "task_log_panel": "\U0001f4dc Execution Log [{ref}]\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{content}",
        "archive_detail_panel": "\U0001f4c1 Archive Detail\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nTask code: {code}\nArchive ID: {archive_id}\nStatus: {status}\nType: {action}\nCompleted: {completed}\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nDescription: {text}\nSummary: {summary}",
        "btn_view_archive_detail": "\U0001f4c4 View Archive Detail",
        "btn_view_log": "\U0001f4dc View Log",
        "btn_view_accept_doc": "\U0001f4c4 View Acceptance Doc",
        "btn_delete_archive": "\U0001f5d1 Delete Archive",
        "btn_back_list": "\u00ab Back",
        "task_created_simple": "Task created: [{code}] {task_id}\nStatus: pending\nContent: {text}",
        "ws_deleted_fallback": "\u26a0\ufe0f Selected workspace deleted, fell back to default: {label}",
        "ws_deleted_no_fallback": "\u274c Selected workspace deleted, no available workspace.",
        "task_queued_ws": "Workspace [{ws}] busy, task queued.\nQueue position: #{pos}\nContent: {text}\n\nWill auto-start when previous task is accepted.",
        "task_created_ws": "Task created: [{code}] {task_id}\nWorkspace: {ws}\nStatus: pending\nContent: {text}",
        "summary_panel": "\U0001f4ca Project Summary\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nSelect workspace:",
        "new_task_ws_panel": "\U0001f4dd New Task\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nSelect workspace for task execution:",
        "switch_backend_panel": "\U0001f504 Switch Backend\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nCurrent backend: {backend}\nSelect new backend:",
        "unknown_backend": "Unknown backend: {backend}\nAvailable: {available}",
        "pipeline_arrow": "\u2192",
        "no_accept_tasks": "\U0001f4ed No tasks awaiting acceptance.",
        "accept_list_header": "\u2705 Tasks awaiting acceptance ({count})\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nSelect task to accept:",
        "no_reject_tasks": "\U0001f4ed No tasks to reject.",
        "reject_list_header": "\u274c Tasks to reject ({count})\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nSelect task to reject:",
        "no_retry_tasks": "\U0001f4ed No tasks to retry.",
        "retry_list_header": "\U0001f504 Tasks to retry ({count})\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nSelect task to retry:",
        "no_cancel_tasks": "\U0001f4ed No tasks to cancel.",
        "cancel_list_header": "\u26d4 Tasks to cancel ({count})\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nSelect task to cancel:",
        "search_roots_cmd_header": "\U0001f50d Search Roots:",
        "search_roots_usage": "\nUsage:\n  /workspace_search_roots add <path>\n  /workspace_search_roots remove <index>\n  /workspace_search_roots clear",
        "search_roots_not_configured": "No search roots configured.\nDefault: current workspace and parent.\n\nUsage: /workspace_search_roots add <path>",
        "usage_search_roots_add": "Usage: /workspace_search_roots add <path>",
        "search_root_added": "\u2705 Added:",
        "search_root_add_failed": "\u274c Failed:",
        "no_valid_paths": "No valid paths",
        "usage_search_roots_remove": "Usage: /workspace_search_roots remove <index>",
        "index_must_be_number": "Index must be a number",
        "search_root_removed": "\u2705 Removed: {msg}",
        "search_root_remove_failed": "Remove failed: {msg}",
        "search_roots_cleared": "\u2705 All search roots cleared.\nWill fall back to default search scope.",
        "unknown_subcommand": "Unknown subcommand: {sub}\nUsage: add / remove / clear",
        "queue_auto_start": "\U0001f504 Queue Task Auto-start\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nWorkspace: {ws}\nTask: [{code}] {task_id}\nContent: {text}\nStatus: pending (queued for execution)\nRemaining queue: {remaining}",
        "rejection_reason_doc": "\n\nRejection reason: {reason}",
        "error_info_doc": "\n\nError info: {err}",
        "preset_arrow": "  {key} \u2192\n{value}",
        "preset_arrow_inline": "  {key} \u2192 {value}",
    },
    "status": {
        "icon_not_executed": "(Not Executed)",
        "icon_fail": "\u274c",
        "icon_pass": "\u2705",
    },
}


def merge_keys(locale_data, new_keys):
    """Merge new keys into locale data without overwriting existing."""
    for section, keys in new_keys.items():
        if section not in locale_data:
            locale_data[section] = {}
        for key, value in keys.items():
            if key not in locale_data[section]:
                locale_data[section][key] = value


# Load and update locale files
with open(zh_path, "r", encoding="utf-8") as f:
    zh = json.load(f)
with open(en_path, "r", encoding="utf-8") as f:
    en = json.load(f)

merge_keys(zh, NEW_ZH_KEYS)
merge_keys(en, NEW_EN_KEYS)

with open(zh_path, "w", encoding="utf-8") as f:
    json.dump(zh, f, ensure_ascii=False, indent=2)
    f.write("\n")
with open(en_path, "w", encoding="utf-8") as f:
    json.dump(en, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("Locale files updated.")

# ============================================================
# Step 2: Replace strings in bot_commands.py
# ============================================================
with open(bc_path, "r", encoding="utf-8") as f:
    content = f.read()

replacements = 0


def do_replace(old, new, count=1):
    global content, replacements
    if old not in content:
        print(f"  WARNING: pattern not found: {old[:80]}...")
        return False
    if count == 0:
        occurrences = content.count(old)
        content = content.replace(old, new)
        replacements += occurrences
        return True
    content = content.replace(old, new, count)
    replacements += count
    return True


# ---- Line 676: Pipeline execution detail header ----
do_replace(
    'lines = ["\\u2699\\ufe0f \\u6d41\\u6c34\\u7ebf\\u6267\\u884c\\u8be6\\u60c5:"]',
    'lines = [t("msg.pipeline_exec_header")]'
)

# ---- Line 700: Status icons for pipeline stages ----
do_replace(
    'status_icon = "(\\u672a\\u6267\\u884c)"',
    'status_icon = t("status.icon_not_executed")'
)

# Replace all \\u274c (❌) status icons that are NOT in callback_data or inside strings with other context
# Lines 703, 709: standalone status_icon = "\\u274c"
do_replace(
    'status_icon = "\\u274c"\n            time_str = " {:.1f}s".format(elapsed / 1000.0) if elapsed else ""\n        elif rc == 0',
    'status_icon = t("status.icon_fail")\n            time_str = " {:.1f}s".format(elapsed / 1000.0) if elapsed else ""\n        elif rc == 0'
)
do_replace(
    'status_icon = "\\u2705"',
    'status_icon = t("status.icon_pass")',
    count=1  # Just the first one in format_stage_execution_summary
)
do_replace(
    '        else:\n            status_icon = "\\u274c"\n            time_str = " {:.1f}s".format(elapsed / 1000.0) if elapsed else ""\n\n        line = "  {}. {} {}',
    '        else:\n            status_icon = t("status.icon_fail")\n            time_str = " {:.1f}s".format(elapsed / 1000.0) if elapsed else ""\n\n        line = "  {}. {} {}'
)

# ---- Line 712: Stage line format with arrow ----
do_replace(
    'line = "  {}. {} {} \\u2192 {} {}{}".format(idx, emoji, label, model_display, status_icon, time_str)',
    'line = t("msg.stage_line", idx=idx, emoji=emoji, label=label, model_display=model_display, status_icon=status_icon, time_str=time_str)'
)

# ---- Line 991: Set failed message ----
do_replace(
    'send_text(chat_id, "\\u274c \\u8bbe\\u7f6e\\u5931\\u8d25\\uff1a\\u6a21\\u578b {} \\u5f53\\u524d\\u4e0d\\u53ef\\u7528\\uff08{}\\uff09".format(model_id, reason),',
    'send_text(chat_id, t("msg.set_failed", model=model_id, reason=reason),'
)

# ---- Line 1098: Role pipeline panel separator ----
do_replace(
    't("msg.role_pipeline_config") + "\\n"\n                "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n                + t("msg.role_set", emoji=role_def.get("emoji", ""), label=role_def.get("label", role_name), tag=tag, model=model_id) + "\\n\\n"\n                + t("msg.role_config_current", config=format_role_pipeline_stages(stages)),',
    't("msg.role_pipeline_panel", title=t("msg.role_pipeline_config"), role_set=t("msg.role_set", emoji=role_def.get("emoji", ""), label=role_def.get("label", role_name), tag=tag, model=model_id), config=t("msg.role_config_current", config=format_role_pipeline_stages(stages))),'
)

# ---- Line 1138: Stage emoji default ----
do_replace(
    'emoji = role_def.get("emoji", "") if role_def else STAGE_EMOJI.get(stage_name, "\\u2699\\ufe0f")',
    'emoji = role_def.get("emoji", "") if role_def else STAGE_EMOJI.get(stage_name, "\\u2699\\ufe0f")'
)
# This is fine as-is, it's an emoji fallback not Chinese text.

# ---- Line 1229: Stage default label ----
do_replace(
    'summary_lines.append("  {}: \\uff08\\u9ed8\\u8ba4\\uff09".format(name))',
    'summary_lines.append("  {}: {}".format(name, t("task.stage_default")))'
)

# ---- Lines 1637-1640: Model list header/footer ----
do_replace(
    'header = (\n            "\\U0001f4cb \\u6a21\\u578b\\u6e05\\u5355\\n"\n            "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n        )',
    'header = t("msg.model_list_header")'
)
do_replace(
    'footer = "\\n\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n\\u9ed8\\u8ba4\\u6a21\\u578b: {}".format(current_default or t("msg.not_set"))',
    'footer = t("msg.model_list_footer", default_model=current_default or t("msg.not_set"))'
)

# ---- Lines 1671/1673: Preset arrows ----
do_replace(
    'preset_lines.append("  {} \\u2192\\n{}".format(k, format_role_pipeline_stages(display_stages)))',
    'preset_lines.append(t("msg.preset_arrow", key=k, value=format_role_pipeline_stages(display_stages)))'
)
do_replace(
    'preset_lines.append("  {} \\u2192 {}".format(k, format_pipeline_stages(v)))',
    'preset_lines.append(t("msg.preset_arrow_inline", key=k, value=format_pipeline_stages(v)))'
)

# ---- Lines 1677-1680: Pipeline config panel ----
do_replace(
    '"\\u2699\\ufe0f \\u6d41\\u6c34\\u7ebf\\u914d\\u7f6e\\n"\n            "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n            "\\u70b9\\u51fb\\u9884\\u8bbe\\u76f4\\u63a5\\u5e94\\u7528\\uff0c\\u6216\\u9009\\u62e9\\u81ea\\u5b9a\\u4e49:\\n\\n"\n            "{}".format(preset_info),',
    't("msg.pipeline_config_panel", preset_info=preset_info),'
)

# ---- Lines 1729-1731: Role pipeline view ----
do_replace(
    't("msg.role_pipeline_config") + "\\n"\n            "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n            + t("msg.role_config_current", config=format_role_pipeline_stages(stages)),',
    't("msg.role_pipeline_view", title=t("msg.role_pipeline_config"), config=t("msg.role_config_current", config=format_role_pipeline_stages(stages))),'
)

# ---- Line 1938: Workspace remove panel ----
do_replace(
    '"\\u2796 \\u5220\\u9664\\u5de5\\u4f5c\\u76ee\\u5f55\\n\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n\\u8bf7\\u9009\\u62e9\\u8981\\u5220\\u9664\\u7684\\u5de5\\u4f5c\\u76ee\\u5f55:",',
    't("msg.ws_remove_panel"),'
)

# ---- Line 1954: Workspace set default panel ----
do_replace(
    '"\\u2b50 \\u8bbe\\u7f6e\\u9ed8\\u8ba4\\u5de5\\u4f5c\\u76ee\\u5f55\\n\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n\\u8bf7\\u9009\\u62e9\\u8981\\u8bbe\\u7f6e\\u4e3a\\u9ed8\\u8ba4\\u7684\\u5de5\\u4f5c\\u76ee\\u5f55:",',
    't("msg.ws_set_default_panel"),'
)

# ---- Lines 1964-1977: Search roots with items / empty ----
do_replace(
    '            lines = ["\\U0001f50d \\u641c\\u7d22\\u6839\\u76ee\\u5f55"]\n            lines.append("\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501")\n            for idx, r in enumerate(roots, 1):\n                lines.append("{}. {}".format(idx, r))\n            lines.append("\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501")\n            lines.append("\\u70b9\\u51fb \\u2796 \\u5220\\u9664\\uff0c\\u6216\\u70b9 \\u2795 \\u6dfb\\u52a0\\u65b0\\u6839\\u76ee\\u5f55")\n            text = "\\n".join(lines)',
    '            items_lines = []\n            for idx, r in enumerate(roots, 1):\n                items_lines.append("{}. {}".format(idx, r))\n            text = t("msg.search_roots_has_items", items="\\n".join(items_lines))'
)
do_replace(
    'text = (\n                "\\U0001f50d \\u641c\\u7d22\\u6839\\u76ee\\u5f55\\n"\n                "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n                "\\u5c1a\\u672a\\u914d\\u7f6e\\u641c\\u7d22\\u6839\\u76ee\\u5f55\\u3002\\n"\n                "\\u9ed8\\u8ba4\\u4f7f\\u7528\\u5f53\\u524d\\u6d3b\\u8dc3\\u5de5\\u4f5c\\u76ee\\u5f55\\u53ca\\u5176\\u7236\\u76ee\\u5f55\\u3002\\n\\n"\n                "\\u70b9\\u51fb \\u2795 \\u6dfb\\u52a0\\u641c\\u7d22\\u6839\\u76ee\\u5f55\\uff0c\\u6269\\u5927\\u6a21\\u7cca\\u641c\\u7d22\\u8303\\u56f4\\u3002"\n            )',
    'text = t("msg.search_roots_empty")'
)

# ---- Line 2000: No queued tasks ----
do_replace(
    '"\\u5f53\\u524d\\u6240\\u6709\\u5de5\\u4f5c\\u533a\\u57df\\u65e0\\u6392\\u961f\\u4efb\\u52a1\\u3002",',
    't("msg.no_queued_tasks_all"),'
)

# ---- Line 2006: Queue header ----
do_replace(
    'lines = ["\\U0001f4ca \\u5de5\\u4f5c\\u533a\\u57df\\u4efb\\u52a1\\u961f\\u5217:"]',
    'lines = [t("msg.ws_queue_header")]'
)

# ---- Line 2012: Queue section ----
do_replace(
    'lines.append("\\n{} ({}\\u4e2a\\u6392\\u961f):".format(ws_label, len(tasks)))',
    'lines.append(t("msg.ws_queue_section", label=ws_label, count=len(tasks)))'
)

# ---- Lines 2225-2229: Task overview panel ----
do_replace(
    '"\\U0001f4ca \\u4efb\\u52a1\\u6982\\u89c8\\n"\n            "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n            "\\u6d3b\\u8dc3\\u4efb\\u52a1\\u603b\\u6570: {}\\n"\n            "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n            "\\u70b9\\u51fb\\u4e0b\\u65b9\\u6309\\u94ae\\u67e5\\u770b\\u5bf9\\u5e94\\u7c7b\\u522b:".format(total_active),',
    't("msg.task_overview_panel", total=total_active),'
)

# ---- Line 2242: No tasks in category ----
do_replace(
    '"\\u5f53\\u524d\\u65e0{}\\u4efb\\u52a1\\u3002".format(empty_label),',
    't("msg.no_tasks_in_category", label=empty_label),'
)

# ---- Line 2248: Task list header ----
do_replace(
    'header = "{}\\n\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n\\u5171 {} \\u4e2a\\u4efb\\u52a1\\uff0c\\u70b9\\u51fb\\u67e5\\u770b\\u8be6\\u60c5:".format(label, len(tasks))',
    'header = t("msg.task_list_header", label=label, count=len(tasks))'
)

# ---- Line 2276: No tasks current ----
do_replace(
    '"\\u5f53\\u524d\\u65e0\\u4efb\\u52a1\\u3002",',
    't("msg.no_tasks_current"),'
)

# ---- Line 2284: Task list paged ----
do_replace(
    '"{}\\n\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n\\u5171 {} \\u4e2a\\u4efb\\u52a1\\uff08\\u7b2c {} \\u9875\\uff09:".format(label, len(tasks), page + 1),',
    't("msg.task_list_paged", label=label, count=len(tasks), page=page + 1),'
)

# ---- Lines 2293-2298: _detail_status_emoji mapping ----
# These are just emoji symbols (not Chinese text really), but they contain unicode escapes
# Actually ⏳ ⚙️ 📋 ✅ ❌ 💥 are all emojis. Let's leave this as-is since they're just emoji mappings.
# No change needed.

# ---- Lines 2318-2327: Task detail snapshot ----
do_replace(
    'detail = (\n                "{emoji} \\u4efb\\u52a1\\u8be6\\u60c5\\n"\n                "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n                "\\u4ee3\\u53f7: {code}\\n"\n                "\\u72b6\\u6001: {status}\\n"\n                "\\u521b\\u5efa\\u65f6\\u95f4: {created}\\n"\n                "\\u8fed\\u4ee3\\u6b21\\u6570: {iteration}\\n"\n                "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n                "\\u63cf\\u8ff0: {text}\\n"\n                "\\u6982\\u8981: {summary}"\n            ).format(\n                emoji=emoji,\n                code=task_code,\n                status=status_tag(status),\n                created=st.get("created_at", ""),\n                iteration=iteration,\n                text=text_preview,\n                summary=str(st.get("summary") or "").strip()[:500] or t("msg.none_short"),\n            )',
    'detail = t("msg.task_detail_snapshot",\n                emoji=emoji,\n                code=task_code,\n                status=status_tag(status),\n                created=st.get("created_at", ""),\n                iteration=iteration,\n                text=text_preview,\n                summary=str(st.get("summary") or "").strip()[:500] or t("msg.none_short"),\n            )'
)

# ---- Lines 2364-2382: Task detail full ----
do_replace(
    'detail = (\n        "{emoji} \\u4efb\\u52a1\\u8be6\\u60c5\\n"\n        "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n        "\\u4ee3\\u53f7: {code}\\n"\n        "\\u72b6\\u6001: {status}\\n"\n        "\\u521b\\u5efa\\u65f6\\u95f4: {created}\\n"\n        "\\u8fed\\u4ee3\\u6b21\\u6570: {iteration}\\n"\n        "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n        "\\u63cf\\u8ff0: {text}\\n"\n        "\\u6267\\u884c\\u6982\\u8981: {summary}"\n    ).format(\n        emoji=emoji,\n        code=found.get("task_code", task_code),\n        status=status_tag(status),\n        created=found.get("created_at") or st.get("created_at", ""),\n        iteration=iteration,\n        text=text_preview,\n        summary=summary or t("msg.none_short"),\n    )',
    'detail = t("msg.task_detail_full",\n        emoji=emoji,\n        code=found.get("task_code", task_code),\n        status=status_tag(status),\n        created=found.get("created_at") or st.get("created_at", ""),\n        iteration=iteration,\n        text=text_preview,\n        summary=summary or t("msg.none_short"),\n    )'
)

# ---- Line 2384: Rejection reason append ----
do_replace(
    'detail += "\\n\\u62d2\\u7edd\\u539f\\u56e0: {}".format(str(acceptance["reason"])[:500])',
    'detail += t("msg.rejection_reason_append", reason=str(acceptance["reason"])[:500])'
)

# ---- Line 2389: Git checkpoint append ----
do_replace(
    'detail += "\\n\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\nGit\\u68c0\\u67e5\\u70b9: {}".format(ckpt[:12])',
    'detail += t("msg.git_checkpoint_append", ckpt=ckpt[:12])'
)

# ---- Line 2394: Separator + stage summary ----
do_replace(
    'detail += "\\n\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n" + stage_summary',
    'detail += t("msg.separator_line") + stage_summary'
)

# ---- Lines 2427: Stage detail header ----
do_replace(
    'lines = ["\\U0001f50d \\u9636\\u6bb5\\u8be6\\u60c5 [{}]".format(task_code), "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501"]',
    'lines = [t("msg.stage_detail_header", code=task_code), "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501"]'
)

# ---- Line 2449: Stage detail status icon ----
do_replace(
    'status_icon = "\\u274c" if noop or (rc and rc != 0) else "\\u2705"',
    'status_icon = t("status.icon_fail") if noop or (rc and rc != 0) else t("status.icon_pass")'
)

# ---- Line 2450: Stage detail line ----
do_replace(
    'lines.append("\\n{} {}. {} {} \\u2192 {}{}".format(emoji, idx, label, status_icon, model_display, time_str))',
    'lines.append(t("msg.stage_detail_line", emoji=emoji, idx=idx, label=label, status_icon=status_icon, model_display=model_display, time_str=time_str))'
)

# ---- Line 2458: Truncated text ----
do_replace(
    'preview += "\\n... (\\u5df2\\u622a\\u65ad)"',
    'preview += "\\n" + t("msg.truncated")'
)

# ---- Line 2461: No output ----
do_replace(
    'lines.append("  (\\u65e0\\u8f93\\u51fa)")',
    'lines.append("  " + t("msg.no_output"))'
)

# ---- Line 2466: Truncated text (general) ----
do_replace(
    'text = text[:4000] + "\\n... (\\u5df2\\u622a\\u65ad)"',
    'text = text[:4000] + "\\n" + t("msg.truncated")'
)

# ---- Line 2492: Task doc panel (archive) ----
do_replace(
    '"\\U0001f4c4 \\u4efb\\u52a1\\u6587\\u6863 [{}]\\n\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n{}".format(ref, doc_text[:3500]),',
    't("msg.task_doc_panel", ref=ref, content=doc_text[:3500]),'
)

# ---- Line 2509: Task doc panel (status snapshot) ----
do_replace(
    '"\\U0001f4c4 \\u4efb\\u52a1\\u6587\\u6863 [{}]\\n\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n{}".format(ref, doc_text[:3500]),',
    't("msg.task_doc_panel", ref=ref, content=doc_text[:3500]),'
)

# ---- Line 2548: Rejection reason in doc ----
do_replace(
    'doc_text += "\\n\\n\\u62d2\\u7edd\\u539f\\u56e0: {}".format(str(acceptance["reason"])[:500])',
    'doc_text += t("msg.rejection_reason_doc", reason=str(acceptance["reason"])[:500])'
)

# ---- Line 2551: Error info in doc ----
do_replace(
    'doc_text += "\\n\\n\\u9519\\u8bef\\u4fe1\\u606f: {}".format(error[:500])',
    'doc_text += t("msg.error_info_doc", err=error[:500])'
)

# ---- Line 2558: Task doc panel (main) ----
do_replace(
    '"\\U0001f4c4 \\u4efb\\u52a1\\u6587\\u6863 [{}]\\n\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n{}".format(\n            found.get("task_code", ref), doc_text[:3500]\n        ),',
    't("msg.task_doc_panel", ref=found.get("task_code", ref), content=doc_text[:3500]),'
)

# ---- Line 2595: Task summary panel ----
do_replace(
    '"\\U0001f4d1 \\u4efb\\u52a1\\u6982\\u8981 [{}]\\n\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n{}".format(ref, summary_text[:3500]),',
    't("msg.task_summary_panel", ref=ref, content=summary_text[:3500]),'
)

# ---- Line 2622: Task log caption ----
do_replace(
    'send_document(chat_id, run_log_path, caption="\\u6267\\u884c\\u65e5\\u5fd7 [{}]".format(ref))',
    'send_document(chat_id, run_log_path, caption=t("msg.task_log_caption", ref=ref))'
)

# ---- Line 2624: Task log panel (fallback) ----
do_replace(
    'send_text(chat_id, "\\U0001f4dc \\u6267\\u884c\\u65e5\\u5fd7 [{}]\\n\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n{}...".format(ref, log_text[:3500]),',
    'send_text(chat_id, t("msg.task_log_panel", ref=ref, content=log_text[:3500] + "..."),',
)

# ---- Line 2629: Task log panel (short) ----
do_replace(
    '"\\U0001f4dc \\u6267\\u884c\\u65e5\\u5fd7 [{}]\\n\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n{}".format(ref, log_text[:3500]),',
    't("msg.task_log_panel", ref=ref, content=log_text[:3500]),'
)

# ---- Lines 2678-2688: Archive detail panel ----
do_replace(
    'detail = (\n        "\\U0001f4c1 \\u5f52\\u6863\\u8be6\\u60c5\\n"\n        "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n        "\\u4efb\\u52a1\\u4ee3\\u53f7: {code}\\n"\n        "\\u5f52\\u6863ID: {archive_id}\\n"\n        "\\u72b6\\u6001: {status}\\n"\n        "\\u7c7b\\u578b: {action}\\n"\n        "\\u5b8c\\u6210\\u65f6\\u95f4: {completed}\\n"\n        "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n        "\\u63cf\\u8ff0: {text}\\n"\n        "\\u6982\\u8981: {summary}"\n    ).format(',
    'detail = t("msg.archive_detail_panel",'
)

# ---- Lines 2703: Archive button texts ----
do_replace(
    '{"text": "\\U0001f4c4 \\u67e5\\u770b\\u5f52\\u6863\\u8be6\\u60c5", "callback_data": safe_callback_data("task_doc", aid)},',
    '{"text": t("msg.btn_view_archive_detail"), "callback_data": safe_callback_data("task_doc", aid)},'
)
do_replace(
    '{"text": "\\U0001f4dc \\u67e5\\u770b\\u6267\\u884c\\u65e5\\u5fd7", "callback_data": "task_log:{}".format(task_code)},',
    '{"text": t("msg.btn_view_log"), "callback_data": "task_log:{}".format(task_code)},'
)
do_replace(
    '{"text": "\\U0001f4c4 \\u67e5\\u770b\\u9a8c\\u6536\\u6587\\u6863", "callback_data": safe_callback_data("task_doc", entry.get("task_code", aid))},',
    '{"text": t("msg.btn_view_accept_doc"), "callback_data": safe_callback_data("task_doc", entry.get("task_code", aid))},'
)
do_replace(
    '{"text": "\\U0001f5d1 \\u5220\\u9664\\u5f52\\u6863\\u8bb0\\u5f55", "callback_data": safe_callback_data("archive_delete", aid)},',
    '{"text": t("msg.btn_delete_archive"), "callback_data": safe_callback_data("archive_delete", aid)},'
)
do_replace(
    '{"text": "\\u00ab \\u8fd4\\u56de\\u5217\\u8868", "callback_data": "menu:tasks_archived"},',
    '{"text": t("msg.btn_back_list"), "callback_data": "menu:tasks_archived"},'
)

# ---- Line 2813: Task created simple ----
do_replace(
    '"\\u4efb\\u52a1\\u5df2\\u521b\\u5efa: [{code}] {task_id}\\n\\u72b6\\u6001: pending\\n\\u5185\\u5bb9: {text}".format(\n                code=task_code,\n                task_id=task_id,\n                text=txt[:200],\n            ),',
    't("msg.task_created_simple", code=task_code, task_id=task_id, text=txt[:200]),'
)

# ---- Line 2837: WS deleted fallback ----
do_replace(
    '"\\u26a0\\ufe0f \\u6240\\u9009\\u5de5\\u4f5c\\u533a\\u5df2\\u88ab\\u5220\\u9664\\uff0c\\u5df2\\u56de\\u9000\\u5230\\u9ed8\\u8ba4\\u5de5\\u4f5c\\u533a: {}".format(ws_label),',
    't("msg.ws_deleted_fallback", label=ws_label),'
)

# ---- Line 2840: WS deleted no fallback ----
do_replace(
    '"\\u274c \\u6240\\u9009\\u5de5\\u4f5c\\u533a\\u5df2\\u88ab\\u5220\\u9664\\uff0c\\u4e14\\u65e0\\u53ef\\u7528\\u5de5\\u4f5c\\u533a\\u3002"',
    't("msg.ws_deleted_no_fallback")'
)

# ---- Lines 2858-2861: Task queued WS ----
do_replace(
    '"\\u5de5\\u4f5c\\u533a [{ws}] \\u5f53\\u524d\\u6709\\u4efb\\u52a1\\u6267\\u884c\\u4e2d\\uff0c\\u5df2\\u52a0\\u5165\\u961f\\u5217\\u3002\\n"\n                "\\u961f\\u5217\\u4f4d\\u7f6e: \\u7b2c{pos}\\u4e2a\\n"\n                "\\u5185\\u5bb9: {text}\\n\\n"\n                "\\u524d\\u4e00\\u4efb\\u52a1\\u9a8c\\u6536\\u901a\\u8fc7\\u540e\\u5c06\\u81ea\\u52a8\\u542f\\u52a8\\u3002".format(\n                    ws=ws_label,\n                    pos=pos,\n                    text=txt[:200],\n                ),',
    't("msg.task_queued_ws", ws=ws_label, pos=pos, text=txt[:200]),'
)

# ---- Lines 2875: Task created WS ----
do_replace(
    '"\\u4efb\\u52a1\\u5df2\\u521b\\u5efa: [{code}] {task_id}\\n\\u5de5\\u4f5c\\u533a: {ws}\\n\\u72b6\\u6001: pending\\n\\u5185\\u5bb9: {text}".format(\n                    code=task_code,\n                    task_id=task_id,\n                    ws=ws_label,\n                    text=txt[:200],\n                ),',
    't("msg.task_created_ws", code=task_code, task_id=task_id, ws=ws_label, text=txt[:200]),'
)

# ---- Lines 3147-3149: Summary panel ----
do_replace(
    '"\\U0001f4ca \\u9879\\u76ee\\u603b\\u7ed3\\n"\n            "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n            "\\u8bf7\\u9009\\u62e9\\u5de5\\u4f5c\\u533a:",',
    't("msg.summary_panel"),'
)

# ---- Line 3175: Content truncated ----
do_replace(
    'report = report[:4090] + "\\n... (\\u5185\\u5bb9\\u5df2\\u622a\\u65ad)"',
    'report = report[:4090] + "\\n" + t("msg.content_truncated")'
)

# ---- Lines 3282-3284: New task WS panel ----
do_replace(
    '"\\U0001f4dd \\u65b0\\u5efa\\u4efb\\u52a1\\n"\n                "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n                "\\u8bf7\\u9009\\u62e9\\u4efb\\u52a1\\u6267\\u884c\\u7684\\u5de5\\u4f5c\\u533a\\u57df:",',
    't("msg.new_task_ws_panel"),'
)

# ---- Lines 3376-3378: Switch backend panel ----
do_replace(
    '"\\U0001f504 \\u5207\\u6362\\u540e\\u7aef\\n"\n                "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n                "\\u5f53\\u524d\\u540e\\u7aef: {}\\n\\u8bf7\\u9009\\u62e9\\u65b0\\u7684\\u540e\\u7aef\\uff1a".format(get_agent_backend()),',
    't("msg.switch_backend_panel", backend=get_agent_backend()),'
)

# ---- Lines 3385: Unknown backend ----
do_replace(
    '"\\u672a\\u77e5\\u540e\\u7aef: {}\\n\\u53ef\\u7528: {}".format(\n                    backend, "|".join(sorted(KNOWN_BACKENDS))\n                ),',
    't("msg.unknown_backend", backend=backend, available="|".join(sorted(KNOWN_BACKENDS))),'
)

# ---- Lines 3564/3566/3571: Pipeline show arrows ----
do_replace(
    'lines.append("  {}. {} {} \\u2192 {} {}".format(i, emoji, label, model, tag).rstrip())',
    'lines.append("  {}. {} {} {} {} {}".format(i, emoji, label, t("msg.pipeline_arrow"), model, tag).rstrip())'
)
do_replace(
    'lines.append("  {}. {} {} \\u2192 ({})".format(i, emoji, label, s.get("backend", "?")))',
    'lines.append("  {}. {} {} {} ({})".format(i, emoji, label, t("msg.pipeline_arrow"), s.get("backend", "?")))'
)
do_replace(
    'lines.append("  {}. {} \\u2192 {} {}".format(i, name, model, tag).rstrip())',
    'lines.append("  {}. {} {} {} {}".format(i, name, t("msg.pipeline_arrow"), model, tag).rstrip())'
)

# ---- Line 3726: No accept tasks ----
do_replace(
    '"\\U0001f4ed \\u5f53\\u524d\\u6ca1\\u6709\\u5f85\\u9a8c\\u6536\\u7684\\u4efb\\u52a1\\u3002",',
    't("msg.no_accept_tasks"),'
)

# ---- Lines 3732-3734: Accept list header ----
do_replace(
    '"\\u2705 \\u5f85\\u9a8c\\u6536\\u4efb\\u52a1\\uff08\\u5171 {} \\u4e2a\\uff09\\n"\n                "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n                "\\u8bf7\\u9009\\u62e9\\u8981\\u9a8c\\u6536\\u7684\\u4efb\\u52a1\\uff1a".format(len(tasks)),',
    't("msg.accept_list_header", count=len(tasks)),'
)

# ---- Line 3889: No reject tasks ----
do_replace(
    '"\\U0001f4ed \\u5f53\\u524d\\u6ca1\\u6709\\u53ef\\u62d2\\u7edd\\u7684\\u4efb\\u52a1\\u3002",',
    't("msg.no_reject_tasks"),'
)

# ---- Lines 3895-3897: Reject list header ----
do_replace(
    '"\\u274c \\u53ef\\u62d2\\u7edd\\u4efb\\u52a1\\uff08\\u5171 {} \\u4e2a\\uff09\\n"\n                "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n                "\\u8bf7\\u9009\\u62e9\\u8981\\u62d2\\u7edd\\u7684\\u4efb\\u52a1\\uff1a".format(len(tasks)),',
    't("msg.reject_list_header", count=len(tasks)),'
)

# ---- Line 4039: No retry tasks ----
do_replace(
    '"\\U0001f4ed \\u5f53\\u524d\\u6ca1\\u6709\\u53ef\\u91cd\\u8bd5\\u7684\\u4efb\\u52a1\\u3002",',
    't("msg.no_retry_tasks"),'
)

# ---- Lines 4045-4047: Retry list header ----
do_replace(
    '"\\U0001f504 \\u53ef\\u91cd\\u8bd5\\u4efb\\u52a1\\uff08\\u5171 {} \\u4e2a\\uff09\\n"\n                "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n                "\\u8bf7\\u9009\\u62e9\\u8981\\u91cd\\u8bd5\\u7684\\u4efb\\u52a1\\uff1a".format(len(tasks)),',
    't("msg.retry_list_header", count=len(tasks)),'
)

# ---- Line 4200: No cancel tasks ----
do_replace(
    '"\\U0001f4ed \\u5f53\\u524d\\u6ca1\\u6709\\u53ef\\u53d6\\u6d88\\u7684\\u4efb\\u52a1\\u3002",',
    't("msg.no_cancel_tasks"),'
)

# ---- Lines 4206-4208: Cancel list header ----
do_replace(
    '"\\u26d4 \\u53ef\\u53d6\\u6d88\\u4efb\\u52a1\\uff08\\u5171 {} \\u4e2a\\uff09\\n"\n            "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n            "\\u8bf7\\u9009\\u62e9\\u8981\\u53d6\\u6d88\\u7684\\u4efb\\u52a1\\uff1a".format(len(all_cancellable)),',
    't("msg.cancel_list_header", count=len(all_cancellable)),'
)

# ---- Lines 4607-4613: Search roots command header ----
do_replace(
    'lines = ["\\U0001f50d \\u641c\\u7d22\\u6839\\u76ee\\u5f55:"]',
    'lines = [t("msg.search_roots_cmd_header")]'
)
do_replace(
    'lines.append("\\n\\u7528\\u6cd5:")\n                lines.append("  /workspace_search_roots add <\\u8def\\u5f84>")\n                lines.append("  /workspace_search_roots remove <\\u5e8f\\u53f7>")\n                lines.append("  /workspace_search_roots clear")',
    'lines.append(t("msg.search_roots_usage"))'
)

# ---- Lines 4618-4620: Search roots not configured (command) ----
do_replace(
    '"\\u5c1a\\u672a\\u914d\\u7f6e\\u641c\\u7d22\\u6839\\u76ee\\u5f55\\u3002\\n"\n                    "\\u9ed8\\u8ba4\\u4f7f\\u7528\\u5f53\\u524d\\u6d3b\\u8dc3\\u5de5\\u4f5c\\u76ee\\u5f55\\u53ca\\u5176\\u7236\\u76ee\\u5f55\\u3002\\n\\n"\n                    "\\u7528\\u6cd5: /workspace_search_roots add <\\u8def\\u5f84>",',
    't("msg.search_roots_not_configured"),'
)

# ---- Line 4628: Usage search roots add ----
do_replace(
    'send_text(chat_id, "\\u7528\\u6cd5: /workspace_search_roots add <\\u8def\\u5f84>")',
    'send_text(chat_id, t("msg.usage_search_roots_add"))'
)

# ---- Line 4644: Search root added ----
do_replace(
    'lines.append("\\u2705 \\u5df2\\u6dfb\\u52a0:")',
    'lines.append(t("msg.search_root_added"))'
)

# ---- Line 4648: Search root add failed ----
do_replace(
    'lines.append("\\u274c \\u5931\\u8d25:")',
    'lines.append(t("msg.search_root_add_failed"))'
)

# ---- Line 4654: No valid paths ----
do_replace(
    '"\\n".join(lines) if lines else "\\u65e0\\u6709\\u6548\\u8def\\u5f84",',
    '"\\n".join(lines) if lines else t("msg.no_valid_paths"),'
)

# ---- Line 4661: Usage search roots remove ----
do_replace(
    'send_text(chat_id, "\\u7528\\u6cd5: /workspace_search_roots remove <\\u5e8f\\u53f7>")',
    'send_text(chat_id, t("msg.usage_search_roots_remove"))'
)

# ---- Line 4666: Index must be number ----
do_replace(
    'send_text(chat_id, "\\u5e8f\\u53f7\\u5fc5\\u987b\\u662f\\u6570\\u5b57")',
    'send_text(chat_id, t("msg.index_must_be_number"))'
)

# ---- Line 4673: Search root removed ----
do_replace(
    '"\\u2705 \\u5df2\\u5220\\u9664: {}".format(msg),',
    't("msg.search_root_removed", msg=msg),'
)

# ---- Line 4677: Search root remove failed ----
do_replace(
    'send_text(chat_id, "\\u5220\\u9664\\u5931\\u8d25: {}".format(msg))',
    'send_text(chat_id, t("msg.search_root_remove_failed", msg=msg))'
)

# ---- Line 4684: Search roots cleared ----
do_replace(
    '"\\u2705 \\u5df2\\u6e05\\u7a7a\\u6240\\u6709\\u641c\\u7d22\\u6839\\u76ee\\u5f55\\u3002\\n\\u5c06\\u56de\\u9000\\u5230\\u9ed8\\u8ba4\\u641c\\u7d22\\u8303\\u56f4\\u3002",',
    't("msg.search_roots_cleared"),'
)

# ---- Line 4688: Unknown subcommand ----
do_replace(
    'send_text(chat_id, "\\u672a\\u77e5\\u5b50\\u547d\\u4ee4: {}\\n\\u7528\\u6cd5: add / remove / clear".format(sub))',
    'send_text(chat_id, t("msg.unknown_subcommand", sub=sub))'
)

# ---- Lines 4849-4855: Queue auto start ----
do_replace(
    '"\\U0001f504 \\u961f\\u5217\\u4efb\\u52a1\\u81ea\\u52a8\\u542f\\u52a8\\n"\n        "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n        "\\u5de5\\u4f5c\\u533a: {ws}\\n"\n        "\\u4efb\\u52a1: [{code}] {task_id}\\n"\n        "\\u5185\\u5bb9: {text}\\n"\n        "\\u72b6\\u6001: pending (\\u5df2\\u52a0\\u5165\\u6267\\u884c\\u961f\\u5217)\\n"\n        "\\u5269\\u4f59\\u6392\\u961f: {remaining}\\u4e2a".format(',
    't("msg.queue_auto_start",'
)

# ---- Line 586: "耗时" detection (actual Chinese, not escaped) ----
# This is an AI detection pattern, should NOT be modified.

print(f"\nTotal replacements: {replacements}")

# Write the result
with open(bc_path, "w", encoding="utf-8") as f:
    f.write(content)

print("bot_commands.py updated.")
