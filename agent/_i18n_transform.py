"""One-time transform script: replace hardcoded Chinese strings in bot_commands.py with t() calls."""
import sys

path = "bot_commands.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

original = content

# ============================================================
# Simple string replacements (old -> new)
# We apply them in order; each is applied once unless noted.
# ============================================================

def r(old, new, count=1):
    """Replace old with new in content, up to count times."""
    global content
    if old not in content:
        print(f"  WARNING: not found: {repr(old[:80])}")
        return
    content = content.replace(old, new, count)

def ra(old, new):
    """Replace ALL occurrences."""
    global content
    if old not in content:
        print(f"  WARNING(all): not found: {repr(old[:80])}")
        return
    content = content.replace(old, new)

# ---- status_tag() ----
r('"pending": "\u5f85\u5904\u7406"', '"pending": t("status.pending")')
r('"processing": "\u6267\u884c\u4e2d"', '"processing": t("status.processing")')
r('"pending_acceptance": "\u5f85\u9a8c\u6536"', '"pending_acceptance": t("status.pending_acceptance")')
r('"accepted": "\u9a8c\u6536\u901a\u8fc7"', '"accepted": t("status.accepted")')
r('"rejected": "\u9a8c\u6536\u62d2\u7edd"', '"rejected": t("status.rejected")')
r('"completed": "\u5df2\u5b8c\u6210"', '"completed": t("status.completed")')
r('"succeeded": "\u5df2\u5b8c\u6210"', '"succeeded": t("status.succeeded")')
r('"failed": "\u6267\u884c\u5931\u8d25"', '"failed": t("status.failed")')
# the fallback in status_tag
r('str(status or "unknown")', 'str(status or t("status.unknown"))')

# ---- acceptance_tag() ----
r('return "\u9a8c\u6536\u901a\u8fc7"\n    if status == "rejected"', 'return t("acceptance.tag_accepted")\n    if status == "rejected"')
r('return "\u9a8c\u6536\u62d2\u7edd"', 'return t("acceptance.tag_rejected")')
r('return "\u5f85\u9a8c\u6536"\n    if stage', 'return t("acceptance.tag_pending")\n    if stage')
r('return "\u672a\u5230\u9a8c\u6536\u9636\u6bb5"', 'return t("acceptance.tag_not_ready")')
r('return "\u5f85\u9a8c\u6536(\u517c\u5bb9\u65e7\u4efb\u52a1)"', 'return t("acceptance.tag_pending_compat")')
r('return "\u9a8c\u6536\u901a\u8fc7"\n    return "\u672a\u77e5"', 'return t("acceptance.tag_accepted")\n    return t("acceptance.tag_unknown")')

# ---- acceptance_next_action() ----
r('tag in {"\u5f85\u9a8c\u6536", "\u5f85\u9a8c\u6536(\u517c\u5bb9\u65e7\u4efb\u52a1)", "\u9a8c\u6536\u62d2\u7edd"}',
  'tag in {t("acceptance.tag_pending"), t("acceptance.tag_pending_compat"), t("acceptance.tag_rejected")}')
r('return "\u901a\u8fc7 /accept {code} \u9a8c\u6536\u901a\u8fc7\u5f52\u6863\uff1b\u6216 /reject {code} <\u539f\u56e0> \u4fdd\u6301\u4e0d\u5f52\u6863".format(code=code)',
  'return t("acceptance.next_accept_or_reject", code=code)')
r('tag == "\u9a8c\u6536\u901a\u8fc7"', 'tag == t("acceptance.tag_accepted")')
r('return "\u5df2\u9a8c\u6536\u901a\u8fc7\uff0c\u53ef\u7528 /archive_show {code} \u67e5\u770b\u5f52\u6863\u8be6\u60c5".format(code=code)',
  'return t("acceptance.next_already_accepted", code=code)')
r('return "\u5f53\u524d\u65e0\u9700\u9a8c\u6536\u64cd\u4f5c"', 'return t("acceptance.next_no_action")')

# ---- build_status_summary() ----
r('return ("\u5931\u8d25\u539f\u56e0: " + noop_reason)[:300]',
  'return (t("summary.failure_reason", reason=noop_reason))[:300]')
r('return ("\u9519\u8bef: " + err)[:300]',
  'return (t("summary.error_prefix", err=err))[:300]')
r('return "(\u6682\u65e0\u6982\u8981)"', 'return t("msg.no_summary_short")')

# ---- format_stage_execution_summary() ----
r('lines = ["\u2699\ufe0f \u6d41\u6c34\u7ebf\u6267\u884c\u8be6\u60c5:"]',
  'lines = [t("msg.pipeline_exec_detail")]')
r('status_icon = "(\u672a\u6267\u884c)"', 'status_icon = t("status.not_executed")')

# ---- build_events_text() ----
r('return "\u4efb\u52a1 [{}] {} \u6682\u65e0\u4e8b\u4ef6\u8bb0\u5f55\u3002".format(task_code or "-", task_id)',
  'return t("msg.no_events", code=task_code or "-", task_id=task_id)')
r('lines = ["\u4efb\u52a1 [{}] {} \u6700\u8fd1\u4e8b\u4ef6:".format(task_code or "-", task_id)]',
  'lines = [t("msg.recent_events", code=task_code or "-", task_id=task_id)]')

# ---- task_inline_keyboard() ----
r('{"text": "\u67e5\u770b\u72b6\u6001", "callback_data": "status:{}".format(ref)}',
  '{"text": t("task.view_progress"), "callback_data": "status:{}".format(ref)}')
r('{"text": "\u9a8c\u6536\u901a\u8fc7", "callback_data": "accept:{}".format(ref)}',
  '{"text": t("task.accept"), "callback_data": "accept:{}".format(ref)}')
r('{"text": "\u9a8c\u6536\u62d2\u7edd", "callback_data": "reject:{}".format(ref)}',
  '{"text": t("task.reject"), "callback_data": "reject:{}".format(ref)}')
r('{"text": "\u67e5\u770b\u4e8b\u4ef6", "callback_data": "events:{}".format(ref)}',
  '{"text": t("task.view_events"), "callback_data": "events:{}".format(ref)}')

# ---- handle_callback_query() ----
r('answer_callback_query(cb_id, "\u65e0\u6548\u6309\u94ae")', 'answer_callback_query(cb_id, t("callback.invalid_button"))')
r('answer_callback_query(cb_id, "\u5df2\u67e5\u8be2\u72b6\u6001")', 'answer_callback_query(cb_id, t("callback.status_queried"))')
r('answer_callback_query(cb_id, "\u5df2\u67e5\u8be2\u4e8b\u4ef6")', 'answer_callback_query(cb_id, t("callback.events_queried"))')

# accept callback
r('"\u9a8c\u6536\u4efb\u52a1 [{}] \u9700\u89812FA\u8ba4\u8bc1\u3002\n\u8bf7\u8f93\u51656\u4f4dOTP\u9a8c\u8bc1\u7801\uff1a".format(ref)',
  't("msg.accept_need_2fa", ref=ref)')
r('answer_callback_query(cb_id, "\u8bf7\u8f93\u5165OTP")', 'answer_callback_query(cb_id, t("callback.enter_otp"))', 1)
r('answer_callback_query(cb_id, "\u9a8c\u6536\u5df2\u63d0\u4ea4")', 'answer_callback_query(cb_id, t("callback.acceptance_submitted"))')

# reject callback
r('"\u62d2\u7edd\u4efb\u52a1 [{}] \u9700\u89812FA\u8ba4\u8bc1\u3002\n\u8bf7\u8f93\u5165: <OTP> <\u62d2\u7edd\u539f\u56e0>".format(ref)',
  't("msg.reject_need_2fa", ref=ref)')
r('answer_callback_query(cb_id, "\u8bf7\u8f93\u5165OTP\u548c\u539f\u56e0")', 'answer_callback_query(cb_id, t("callback.enter_otp_reason"))')
r('"\u8bf7\u8f93\u5165\u62d2\u7edd\u4efb\u52a1 [{}] \u7684\u539f\u56e0\uff1a".format(ref)',
  't("msg.enter_reject_reason", ref=ref)')
r('answer_callback_query(cb_id, "\u8bf7\u8f93\u5165\u62d2\u7edd\u539f\u56e0")', 'answer_callback_query(cb_id, t("callback.enter_reject_reason"))')

# retry callback
r('"\u91cd\u65b0\u5f00\u53d1\u4efb\u52a1 [{}] \u9700\u89812FA\u8ba4\u8bc1\u3002\n\u8bf7\u8f93\u5165: <OTP> [\u8865\u5145\u8bf4\u660e]".format(ref)',
  't("msg.retry_need_2fa", ref=ref)')
# second "请输入OTP"
r('answer_callback_query(cb_id, "\u8bf7\u8f93\u5165OTP")', 'answer_callback_query(cb_id, t("callback.enter_otp"))', 1)
r('answer_callback_query(cb_id, "\u91cd\u65b0\u5f00\u53d1\u5df2\u63d0\u4ea4")', 'answer_callback_query(cb_id, t("callback.retry_submitted"))')

# restart callbacks
ra('answer_callback_query(cb_id, "\u65e0\u6743\u9650", show_alert=True)', 'answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)')
r('answer_callback_query(cb_id, "\u6b63\u5728\u91cd\u542f\u670d\u52a1...")', 'answer_callback_query(cb_id, t("callback.restarting"))')
r('send_text(chat_id, "\U0001f504 \u670d\u52a1\u91cd\u542f\u5df2\u6267\u884c (\u4efb\u52a1 [{}])\u3002".format(ref))',
  'send_text(chat_id, t("msg.restart_done", ref=ref))')
r('send_text(chat_id, "\u274c \u91cd\u542f\u5931\u8d25: \u91cd\u542f\u811a\u672c\u672a\u627e\u5230\u6216\u6267\u884c\u51fa\u9519 (\u4efb\u52a1 [{}])\u3002".format(ref))',
  'send_text(chat_id, t("msg.restart_failed_script", ref=ref))')
r('send_text(chat_id, "\u274c \u91cd\u542f\u5931\u8d25: {} (\u4efb\u52a1 [{}])".format(str(exc)[:200], ref))',
  'send_text(chat_id, t("msg.restart_failed", err=str(exc)[:200], ref=ref))')
r('answer_callback_query(cb_id, "\u5df2\u8df3\u8fc7\u91cd\u542f")', 'answer_callback_query(cb_id, t("callback.skip_restart"))')
r('send_text(chat_id, "\u23ed \u5df2\u8df3\u8fc7\u91cd\u542f (\u4efb\u52a1 [{}])\u3002".format(ref))',
  'send_text(chat_id, t("msg.restart_skipped", ref=ref))')

# model_select callback
r('answer_callback_query(cb_id, "\u5df2\u5207\u6362: {} {}".format(tag, model))',
  'answer_callback_query(cb_id, t("callback.switched", tag=tag, model=model))')
r('"\u6a21\u578b\u5df2\u5207\u6362\u4e3a: {} `{}`".format(tag, model)',
  't("msg.model_switched", tag=tag, model=model)')

# model_default callback
r('answer_callback_query(cb_id, "\u26a0\ufe0f \u6743\u9650\u4e0d\u8db3\uff0c\u4ec5\u6388\u6743\u7528\u6237\u53ef\u4fee\u6539\u6a21\u578b\u914d\u7f6e", show_alert=True)',
  'answer_callback_query(cb_id, t("callback.perm_insufficient"), show_alert=True)')
r('reason = m.get("unavailable_reason", "\u4e0d\u53ef\u7528")',
  'reason = m.get("unavailable_reason", t("callback.model_unavailable"))')
r('answer_callback_query(cb_id, "\u6a21\u578b\u4e0d\u53ef\u7528", show_alert=True)',
  'answer_callback_query(cb_id, t("callback.model_unavailable"), show_alert=True)')
r('"\u274c \u8bbe\u7f6e\u5931\u8d25\uff1a\u6a21\u578b {} \u5f53\u524d\u4e0d\u53ef\u7528\uff08{}\uff09".format(model_id, reason)',
  't("msg.set_failed", model=model_id, reason=reason)')
r('answer_callback_query(cb_id, "\u5df2\u8bbe\u4e3a\u9ed8\u8ba4")',
  'answer_callback_query(cb_id, t("callback.set_as_default"))')
r('"\u5df2\u5c06\u9ed8\u8ba4\u6a21\u578b\u8bbe\u4e3a {} `{}`\uff0c\u7ba1\u7ebf\u4e2d\u672a\u5355\u72ec\u914d\u7f6e\u7684\u8282\u70b9\u5c06\u4f7f\u7528\u6b64\u6a21\u578b".format(tag, model_id)',
  't("msg.default_model_set", tag=tag, model=model_id)')

# pipeline preset callback
r('answer_callback_query(cb_id, "\u672a\u77e5\u9884\u8bbe", show_alert=True)',
  'answer_callback_query(cb_id, t("callback.unknown_preset"), show_alert=True)')

# role config callbacks
ra('answer_callback_query(cb_id, "\u672a\u77e5\u89d2\u8272", show_alert=True)',
   'answer_callback_query(cb_id, t("callback.unknown_role"), show_alert=True)')
r('"\u9009\u62e9 {} {} \u4f7f\u7528\u7684\u6a21\u578b\uff1a".format(role_def.get("emoji", ""), role_def.get("label", role_name))',
  't("msg.select_role_model", emoji=role_def.get("emoji", ""), label=role_def.get("label", role_name))')
ra('answer_callback_query(cb_id, "\u9009\u62e9\u6a21\u578b")',
   'answer_callback_query(cb_id, t("callback.select_model"))')
ra('answer_callback_query(cb_id, "\u65e0\u6548\u6570\u636e", show_alert=True)',
   'answer_callback_query(cb_id, t("callback.invalid_data"), show_alert=True)')
r('answer_callback_query(cb_id, "\u4fdd\u5b58\u5931\u8d25", show_alert=True)',
  'answer_callback_query(cb_id, t("callback.save_failed"), show_alert=True)')
r('"\u274c \u4fdd\u5b58\u5931\u8d25\uff1a{}".format(exc)',
  't("msg.save_failed", err=str(exc))')
ra('answer_callback_query(cb_id, "\u5df2\u8bbe\u7f6e: {} {}".format(tag, model_id))',
   'answer_callback_query(cb_id, t("callback.saved", tag=tag, model=model_id))')

# pipeline stage config callbacks
ra('answer_callback_query(cb_id, "\u65e0\u6743\u9650", show_alert=True)',
   'answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)')
ra('answer_callback_query(cb_id, "\u65e0\u6548\u9636\u6bb5", show_alert=True)',
   'answer_callback_query(cb_id, t("callback.invalid_stage"), show_alert=True)')
ra('answer_callback_query(cb_id, "\u8bf7\u91cd\u65b0\u9009\u62e9")',
   'answer_callback_query(cb_id, t("callback.please_reselect"))')
ra('answer_callback_query(cb_id, "\u914d\u7f6e\u5df2\u5e94\u7528")',
   'answer_callback_query(cb_id, t("callback.config_applied"))')

# stage config expired text
ra('"\u2699\ufe0f \u914d\u7f6e\u5df2\u8fc7\u671f\uff0c\u8bf7\u91cd\u65b0\u9009\u62e9\u9884\u8bbe\u3002"',
   't("msg.config_expired")')

# stage config overview text - with format
ra('"\\u2699\\ufe0f \\u9636\\u6bb5\\u914d\\u7f6e\\u6982\\u89c8\\n"\n'
   '                    "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n'
   '                    "\\u5f53\\u524d\\u6d41\\u6c34\\u7ebf: {}\\n\\n"\n'
   '                    "\\u70b9\\u51fb\\u9636\\u6bb5\\u6309\\u94ae\\u4fee\\u6539\\u6a21\\u578b\\uff0c\\u5b8c\\u6210\\u540e\\u70b9\\u51fb\\u300c\\u2705 \\u786e\\u8ba4\\u5e94\\u7528\\u300d\\u751f\\u6548".format(\n'
   '                        preset_display\n'
   '                    )',
   't("msg.stage_config_overview", pipeline=preset_display)')

# configure stage model text
ra('"\U0001f527 \u914d\u7f6e\u300c{} {}\u300d\u9636\u6bb5\u6a21\u578b\\n"\n'
   '                "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n'
   '                "\\u9009\\u62e9\\u8981\\u4f7f\\u7528\\u7684\\u6a21\\u578b\\uff1a".format(emoji, stage_name)',
   't("msg.configure_stage_model", emoji=emoji, name=stage_name)')

print("Starting complex replacements...")

# These need to match the actual bytes in the file
# Let me just do direct unicode replacements

# pipeline applied text
r('\u2705 \u6d41\u6c34\u7ebf\u914d\u7f6e\u5df2\u751f\u6548\uff01\\n"\n                "\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\u2501\\n"\n                "{}\\n\\n"\n                "\\u540e\\u7aef\\u5df2\\u5207\\u6362\\u4e3a: pipeline".format(summary)',
  't("msg.pipeline_applied", summary=summary)')

# default stage name
ra('"  {}: \uff08\u9ed8\u8ba4\uff09".format(name)',
   '"  {}: {}".format(name, t("task.stage_default"))')

# preset display for role_pipeline
ra('"\U0001f3ad \u89d2\u8272\u6d41\u6c34\u7ebf"', 't("menu.role_pipeline_config")')

# pipeline select callback
ra('"\u9009\u62e9\u9884\u8bbe: {}".format(preset_display)',
   't("callback.select_preset", name=preset_display)')

# workspace selection callbacks
ra('answer_callback_query(cb_id, "\u5de5\u4f5c\u533a\u57df\u4e0d\u5b58\u5728", show_alert=True)',
   'answer_callback_query(cb_id, t("callback.workspace_not_found"), show_alert=True)')
ra('answer_callback_query(cb_id, "\u5de5\u4f5c\u533a\u4e0d\u5b58\u5728", show_alert=True)',
   'answer_callback_query(cb_id, t("callback.workspace_not_found"), show_alert=True)')
r('answer_callback_query(cb_id, "\u5df2\u9009: {}".format(ws.get("label", ws_id)))',
  'answer_callback_query(cb_id, t("callback.selected", label=ws.get("label", ws_id)))')
r('answer_callback_query(cb_id, "\u751f\u6210\u603b\u7ed3...")',
  'answer_callback_query(cb_id, t("callback.generating_summary"))')

# workspace remove/default callbacks
ra('"\u5de5\u4f5c\u76ee\u5f55\u5df2\u79fb\u9664: {}".format(ws_id)',
   't("msg.workspace_dir_removed", id=ws_id)')
ra('"\u5de5\u4f5c\u76ee\u5f55\u672a\u627e\u5230: {}".format(ws_id)',
   't("msg.workspace_dir_not_found", id=ws_id)')
ra('answer_callback_query(cb_id, "\u5df2\u5220\u9664")',
   'answer_callback_query(cb_id, t("callback.deleted"))')
ra('answer_callback_query(cb_id, "\u672a\u627e\u5230", show_alert=True)',
   'answer_callback_query(cb_id, t("callback.ws_not_found"), show_alert=True)')
r('"\u9ed8\u8ba4\u5de5\u4f5c\u76ee\u5f55\u5df2\u8bbe\u7f6e: {} ({})".format(ws_id, ws.get("label", "") if ws else "")',
  't("msg.default_workspace_set", id=ws_id, label=ws.get("label", "") if ws else "")')
ra('answer_callback_query(cb_id, "\u5df2\u8bbe\u7f6e\u9ed8\u8ba4")',
   'answer_callback_query(cb_id, t("callback.default_set"))')

# fuzzy add callbacks
ra('answer_callback_query(cb_id, "\u65e0\u6548\u5e8f\u53f7", show_alert=True)',
   'answer_callback_query(cb_id, t("callback.invalid_index"), show_alert=True)')
r('send_text(chat_id, "\u5019\u9009\u5217\u8868\u5df2\u8fc7\u671f\uff0c\u8bf7\u91cd\u65b0\u641c\u7d22\u3002", reply_markup=back_to_menu_keyboard())',
  'send_text(chat_id, t("msg.candidates_expired"), reply_markup=back_to_menu_keyboard())')
r('answer_callback_query(cb_id, "\u5df2\u8fc7\u671f")', 'answer_callback_query(cb_id, t("callback.expired"))')
r('send_text(chat_id, "\u5e8f\u53f7\u8d8a\u754c\uff0c\u6709\u6548\u8303\u56f4: 1-{}".format(len(candidates)))',
  'send_text(chat_id, t("msg.index_out_of_range", max=len(candidates)))')
ra('answer_callback_query(cb_id, "\u5e8f\u53f7\u8d8a\u754c", show_alert=True)',
   'answer_callback_query(cb_id, t("callback.index_out_of_range"), show_alert=True)')
ra('send_text(chat_id, "\u62d2\u7edd\u6dfb\u52a0\u9ad8\u98ce\u9669\u76ee\u5f55: {}".format(str(target)))',
   'send_text(chat_id, t("msg.reject_risky_dir", path=str(target)))')
r('answer_callback_query(cb_id, "\u9ad8\u98ce\u9669\u76ee\u5f55", show_alert=True)',
  'answer_callback_query(cb_id, t("callback.risky_dir"), show_alert=True)')
ra('answer_callback_query(cb_id, "\u5df2\u6dfb\u52a0")',
   'answer_callback_query(cb_id, t("callback.added"))')
ra('send_text(chat_id, "\u6dfb\u52a0\u5931\u8d25: {}".format(str(exc)))',
   'send_text(chat_id, t("msg.add_failed", err=str(exc)))')
ra('answer_callback_query(cb_id, "\u6dfb\u52a0\u5931\u8d25", show_alert=True)',
   'answer_callback_query(cb_id, t("callback.add_failed"), show_alert=True)')

# search root callbacks
r('"\u2705 \u5df2\u5220\u9664\u641c\u7d22\u6839\u76ee\u5f55: {}".format(msg)',
  't("msg.search_root_deleted", path=msg)')
r('"\u5220\u9664\u5931\u8d25: {}".format(msg)',
  't("msg.search_root_delete_failed", msg=msg)')
r('answer_callback_query(cb_id, "\u5220\u9664\u5931\u8d25", show_alert=True)',
  'answer_callback_query(cb_id, t("callback.delete_failed"), show_alert=True)')

# task management callbacks
r('"\u786e\u8ba4\u53d6\u6d88\u4efb\u52a1 [{}]\uff1f\u53d6\u6d88\u540e\u4efb\u52a1\u5c06\u4e0d\u4f1a\u88ab\u6267\u884c\u3002".format(ref)',
  't("msg.confirm_cancel_task", ref=ref)')
ra('answer_callback_query(cb_id, "\u8bf7\u786e\u8ba4\u53d6\u6d88")',
   'answer_callback_query(cb_id, t("callback.confirm_cancel"))')
r('"\u786e\u8ba4\u5220\u9664\u4efb\u52a1 [{}]\uff1f\u5220\u9664\u540e\u5c06\u4ece\u6d3b\u8dc3\u5217\u8868\u79fb\u9664\u3002".format(ref)',
  't("msg.confirm_delete_task", ref=ref)')
ra('answer_callback_query(cb_id, "\u8bf7\u786e\u8ba4\u5220\u9664")',
   'answer_callback_query(cb_id, t("callback.confirm_delete"))')
r('"\u786e\u8ba4\u5220\u9664\u5f52\u6863\u8bb0\u5f55 [{}]\uff1f".format(ref)',
  't("msg.confirm_delete_archive", ref=ref)')

# unknown button
r('answer_callback_query(cb_id, "\u672a\u77e5\u6309\u94ae")',
  'answer_callback_query(cb_id, t("callback.unknown_button"))')
r('answer_callback_query(cb_id, "\u64cd\u4f5c\u5931\u8d25", show_alert=True)',
  'answer_callback_query(cb_id, t("callback.operation_failed"), show_alert=True)')
r('send_text(chat_id, "\u6309\u94ae\u64cd\u4f5c\u5931\u8d25: {}".format(str(exc)[:500]))',
  'send_text(chat_id, t("callback.button_failed", err=str(exc)[:500]))')

# ---- _handle_menu_callback() ----
# auth_ready / model / not_set patterns (used in welcome text formatting)
ra('auth_ready = "\u5df2\u542f\u7528" if get_auth_state() else "\u672a\u521d\u59cb\u5316"',
   'auth_ready = t("msg.enabled") if get_auth_state() else t("msg.not_initialized")')
ra('model = get_claude_model() or "(\u672a\u8bbe\u7f6e)"',
   'model = get_claude_model() or t("msg.not_set")')
r('provider = get_model_provider() or "(\u672a\u8bbe\u7f6e)"',
  'provider = get_model_provider() or t("msg.not_set")')
ra('answer_callback_query(cb_id, "\u4e3b\u83dc\u5355")',
   'answer_callback_query(cb_id, t("callback.main_menu"))')
r('"\u5df2\u53d6\u6d88\u64cd\u4f5c\u3002"', 't("msg.cancelled_op")')
ra('answer_callback_query(cb_id, "\u5df2\u53d6\u6d88")',
   'answer_callback_query(cb_id, t("callback.cancelled"))')
r('answer_callback_query(cb_id, "\u7cfb\u7edf\u8bbe\u7f6e")',
  'answer_callback_query(cb_id, t("callback.system_settings"))')

# Sub-menu answer texts
r('answer_callback_query(cb_id, "\u5f52\u6863\u7ba1\u7406")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u4efb\u52a1\u7ba1\u7406")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u8fd0\u7ef4\u64cd\u4f5c")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u5b89\u5168\u8ba4\u8bc1")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u6280\u80fd\u7ba1\u7406")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u5de5\u4f5c\u533a\u7ba1\u7406")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# New task menu
r('answer_callback_query(cb_id, "\u9009\u62e9\u5de5\u4f5c\u533a")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u8bf7\u8f93\u5165\u4efb\u52a1\u5185\u5bb9")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# Various menu answer_callback_query strings
r('answer_callback_query(cb_id, "\u67e5\u8be2\u4efb\u52a1\u5217\u8868")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# clear tasks
r('send_text(chat_id, "\u5f53\u524d\u6ca1\u6709\u6d3b\u52a8\u4efb\u52a1\uff0c\u65e0\u9700\u6e05\u7a7a\u3002", reply_markup=back_to_menu_keyboard())',
  'send_text(chat_id, t("msg.no_active_tasks"), reply_markup=back_to_menu_keyboard())', 1)
r('answer_callback_query(cb_id, "\u65e0\u6d3b\u52a8\u4efb\u52a1")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('send_text(chat_id, "\u5f53\u524d\u6240\u6709\u4efb\u52a1\u5747\u5728\u8fd0\u884c\u4e2d\uff0c\u65e0\u6cd5\u6e05\u7a7a\u3002", reply_markup=back_to_menu_keyboard())',
  'send_text(chat_id, t("msg.all_tasks_running"), reply_markup=back_to_menu_keyboard())', 1)
r('answer_callback_query(cb_id, "\u5168\u90e8\u8fd0\u884c\u4e2d")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# Various remaining menu strings
r('answer_callback_query(cb_id, "\u8bf7\u8f93\u5165\u622a\u56fe\u8bf4\u660e")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u7cfb\u7edf\u4fe1\u606f")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u9879\u76ee\u603b\u7ed3")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# No permission strings
ra('send_text(chat_id, "\u65e0\u6743\u9650\u6267\u884c\u6b64\u64cd\u4f5c\u3002", reply_markup=back_to_menu_keyboard())',
   'send_text(chat_id, t("callback.no_permission"), reply_markup=back_to_menu_keyboard())')

# Backend select
r('"\u5f53\u524d\u540e\u7aef: {}\\n\u8bf7\u9009\u62e9\u65b0\u7684\u6267\u884c\u540e\u7aef\uff1a".format(current)',
  't("msg.current_backend_select", backend=current)')
r('answer_callback_query(cb_id, "\u9009\u62e9\u540e\u7aef")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u9009\u62e9\u6a21\u578b")', 'answer_callback_query(cb_id, t("callback.select_model"))')

# Model list
r('answer_callback_query(cb_id, "\u5df2\u5237\u65b0" if force else "\u6a21\u578b\u6e05\u5355")',
  'answer_callback_query(cb_id, t("callback.submitted"))')

# Pipeline config
r('answer_callback_query(cb_id, "\u9009\u62e9\u6d41\u6c34\u7ebf\u914d\u7f6e")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u9636\u6bb5\u6982\u89c8")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# Role pipeline config
r('answer_callback_query(cb_id, "\u89d2\u8272\u6d41\u6c34\u7ebf\u914d\u7f6e")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# Various prompt answer texts
ra('answer_callback_query(cb_id, "\u8bf7\u8f93\u5165OTP")', 'answer_callback_query(cb_id, t("callback.enter_otp"))')
r('answer_callback_query(cb_id, "\u8bf7\u8f93\u5165\u914d\u7f6e")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u6d41\u6c34\u7ebf\u72b6\u6001")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u5f52\u6863\u6982\u89c8")', 'answer_callback_query(cb_id, t("callback.submitted"))')
ra('answer_callback_query(cb_id, "\u8bf7\u8f93\u5165\u5173\u952e\u8bcd")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u8bf7\u8f93\u5165ID")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u7ba1\u7406\u670d\u52a1\u72b6\u6001")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "2FA\u521d\u59cb\u5316")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "2FA\u72b6\u6001")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u8eab\u4efd\u4fe1\u606f")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u8bf7\u8f93\u5165\u5de5\u4f5c\u533a+OTP")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u5de5\u4f5c\u76ee\u5f55\u5217\u8868")', 'answer_callback_query(cb_id, t("callback.submitted"))')
ra('answer_callback_query(cb_id, "\u8bf7\u8f93\u5165\u8def\u5f84")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u8c03\u5ea6\u5668\u72b6\u6001")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u672a\u77e5\u83dc\u5355\u64cd\u4f5c")', 'answer_callback_query(cb_id, t("callback.unknown_button"))')
r('answer_callback_query(cb_id, "\u8bf7\u786e\u8ba4\u6e05\u7a7a")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u672a\u77e5\u786e\u8ba4\u64cd\u4f5c")', 'answer_callback_query(cb_id, t("callback.unknown_button"))')

# workspace_remove menu
r('"\u5c1a\u672a\u6ce8\u518c\u4efb\u4f55\u5de5\u4f5c\u76ee\u5f55\u3002"', 't("msg.no_results")')
r('answer_callback_query(cb_id, "\u65e0\u5de5\u4f5c\u76ee\u5f55")', 'answer_callback_query(cb_id, t("callback.submitted"))', 1)
r('answer_callback_query(cb_id, "\u9009\u62e9\u8981\u5220\u9664\u7684\u5de5\u4f5c\u76ee\u5f55")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('\u5c1a\u672a\u6ce8\u518c\u4efb\u4f55\u5de5\u4f5c\u76ee\u5f55\u3002', t("msg.no_results") if False else 'placeholder')
# Fix: the second "尚未注册" is for workspace_set_default
r('answer_callback_query(cb_id, "\u65e0\u5de5\u4f5c\u76ee\u5f55")', 'answer_callback_query(cb_id, t("callback.submitted"))', 1)
r('answer_callback_query(cb_id, "\u9009\u62e9\u9ed8\u8ba4\u5de5\u4f5c\u76ee\u5f55")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# search roots
r('answer_callback_query(cb_id, "\u641c\u7d22\u6839\u76ee\u5f55")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# queue status
r('answer_callback_query(cb_id, "\u65e0\u6392\u961f\u4efb\u52a1")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u961f\u5217\u72b6\u6001")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# backend select callback
r('answer_callback_query(cb_id, "\u65e0\u6743\u9650", show_alert=True)\n        return\n    handle_command(chat_id, user_id, "/switch_backend {}".format(backend))\n    answer_callback_query(cb_id, "\u5df2\u5207\u6362: {}".format(backend))',
  'answer_callback_query(cb_id, t("callback.no_permission"), show_alert=True)\n        return\n    handle_command(chat_id, user_id, "/switch_backend {}".format(backend))\n    answer_callback_query(cb_id, t("callback.switched", tag="", model=backend))')

# ---- _handle_confirm_callback() ----
# confirm: clear_tasks (remaining uses of already used strings handled by ra)
# confirm: task_cancel
r('answer_callback_query(cb_id, "\u65e0\u6548\u64cd\u4f5c", show_alert=True)', 'answer_callback_query(cb_id, t("callback.operation_failed"), show_alert=True)', 1)
r('"\u4efb\u52a1\u4e0d\u5b58\u5728: {}".format(ctx)', 't("msg.error_prefix", err=ctx)', 1)
r('answer_callback_query(cb_id, "\u4efb\u52a1\u4e0d\u5b58\u5728")', 'answer_callback_query(cb_id, t("callback.ws_not_found"))', 1)
r('"\u4ec5\u53ef\u53d6\u6d88\u5f85\u5904\u7406\u4efb\u52a1\uff0c\u5f53\u524d\u72b6\u6001: {}".format(current_status)', 't("msg.error_prefix", err=current_status)', 1)
r('answer_callback_query(cb_id, "\u65e0\u6cd5\u53d6\u6d88", show_alert=True)', 'answer_callback_query(cb_id, t("callback.operation_failed"), show_alert=True)', 1)
r('error="\u7528\u6237\u53d6\u6d88"', 'error=t("callback.cancelled")', 1)
r('"\u2705 \u4efb\u52a1 [{}] \u5df2\u53d6\u6d88\u3002".format(ctx)', 't("msg.cancelled_op")', 1)
ra('answer_callback_query(cb_id, "\u5df2\u53d6\u6d88")', 'answer_callback_query(cb_id, t("callback.cancelled"))')
# confirm: task_delete
r('answer_callback_query(cb_id, "\u65e0\u6548\u64cd\u4f5c", show_alert=True)', 'answer_callback_query(cb_id, t("callback.operation_failed"), show_alert=True)', 1)
r('"\u2705 \u4efb\u52a1 [{}] \u5df2\u5220\u9664\u3002".format(ctx)', 't("msg.cancelled_op")', 1)
# confirm: archive_delete
r('answer_callback_query(cb_id, "\u65e0\u6548\u64cd\u4f5c", show_alert=True)', 'answer_callback_query(cb_id, t("callback.operation_failed"), show_alert=True)', 1)
r('"\u2705 \u5f52\u6863\u8bb0\u5f55 [{}] \u5df2\u5220\u9664\u3002".format(ctx)', 't("msg.cancelled_op")', 1)
r('"\u5f52\u6863\u8bb0\u5f55\u672a\u627e\u5230: {}".format(ctx)', 't("msg.error_prefix", err=ctx)', 1)

# ---- _handle_task_status_menu() ----
r('answer_callback_query(cb_id, "\u672a\u77e5\u64cd\u4f5c")', 'answer_callback_query(cb_id, t("callback.unknown_button"))')

# task overview
r('answer_callback_query(cb_id, "\u4efb\u52a1\u6982\u89c8")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u65e0\u4efb\u52a1")', 'answer_callback_query(cb_id, t("callback.submitted"))', 1)
r('answer_callback_query(cb_id, "\u65e0\u4efb\u52a1")', 'answer_callback_query(cb_id, t("callback.submitted"))', 1)

# pages
r('answer_callback_query(cb_id, "\u65e0\u6548\u5206\u9875")', 'answer_callback_query(cb_id, t("callback.invalid_data"))')
r('answer_callback_query(cb_id, "\u65e0\u6548\u9875\u7801")', 'answer_callback_query(cb_id, t("callback.invalid_data"))')

# task detail callback
r('answer_callback_query(cb_id, "\u4efb\u52a1\u8be6\u60c5")', 'answer_callback_query(cb_id, t("callback.submitted"))', 1)
r('"\u4efb\u52a1\u4e0d\u5b58\u5728: {}".format(task_code)', 't("msg.error_prefix", err=task_code)', 1)
r('answer_callback_query(cb_id, "\u4efb\u52a1\u4e0d\u5b58\u5728")', 'answer_callback_query(cb_id, t("callback.operation_failed"))', 1)
r('answer_callback_query(cb_id, "\u4efb\u52a1\u8be6\u60c5")', 'answer_callback_query(cb_id, t("callback.submitted"))', 1)

# stage detail
r('answer_callback_query(cb_id, "\u65e0\u8bb0\u5f55")', 'answer_callback_query(cb_id, t("callback.submitted"))', 1)
r('answer_callback_query(cb_id, "\u8bfb\u53d6\u5931\u8d25")', 'answer_callback_query(cb_id, t("callback.operation_failed"))', 1)
r('answer_callback_query(cb_id, "\u65e0\u8bb0\u5f55")', 'answer_callback_query(cb_id, t("callback.submitted"))', 1)
r('answer_callback_query(cb_id, "\u9636\u6bb5\u8be6\u60c5")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# task doc callback
r('answer_callback_query(cb_id, "\u67e5\u770b\u6587\u6863")', 'answer_callback_query(cb_id, t("callback.submitted"))', 1)
r('answer_callback_query(cb_id, "\u67e5\u770b\u6587\u6863")', 'answer_callback_query(cb_id, t("callback.submitted"))', 1)
r('"\u4efb\u52a1\u4e0d\u5b58\u5728: {}".format(ref)', 't("msg.error_prefix", err=ref)', 1)
r('answer_callback_query(cb_id, "\u4efb\u52a1\u4e0d\u5b58\u5728")', 'answer_callback_query(cb_id, t("callback.operation_failed"))', 1)
r('answer_callback_query(cb_id, "\u67e5\u770b\u6587\u6863")', 'answer_callback_query(cb_id, t("callback.submitted"))', 1)

# task summary callback
r('answer_callback_query(cb_id, "\u67e5\u770b\u6982\u8981")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# task log callback
r('answer_callback_query(cb_id, "\u65e0\u65e5\u5fd7")', 'answer_callback_query(cb_id, t("callback.submitted"))')
r('answer_callback_query(cb_id, "\u8bfb\u53d6\u5931\u8d25")', 'answer_callback_query(cb_id, t("callback.operation_failed"))')
r('answer_callback_query(cb_id, "\u67e5\u770b\u65e5\u5fd7")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# archive detail callback
r('"\u5f52\u6863\u8bb0\u5f55\u672a\u627e\u5230: {}".format(archive_ref)', 't("msg.error_prefix", err=archive_ref)')
r('answer_callback_query(cb_id, "\u672a\u627e\u5230")', 'answer_callback_query(cb_id, t("callback.ws_not_found"))')
r('answer_callback_query(cb_id, "\u5f52\u6863\u8be6\u60c5")', 'answer_callback_query(cb_id, t("callback.submitted"))')

# ---- screenshot strings ----
r('send_text(chat_id, "\u622a\u56fe\u5b8c\u6210\uff0c\u5df2\u56de\u4f20 {} \u5f20\u56fe\u7247\u3002".format(sent))',
  'send_text(chat_id, t("msg.screenshot_done", count=sent))')

# ---- verify_risky_operation ----
r('return False, "2FA \u672a\u521d\u59cb\u5316\u3002\u8bf7\u5148\u6267\u884c /auth_init"',
  'return False, t("msg.2fa_not_init")')
r('return False, "\u7528\u6cd5: {}".format(usage)',
  'return False, usage')
r('return False, "\u4e8c\u6b21\u8ba4\u8bc1\u5931\u8d25\uff1aOTP \u65e0\u6548\u6216\u5df2\u8fc7\u671f"',
  'return False, t("msg.2fa_failed")')

# ---- handle_pending_action: task created messages ----
r('"\u4efb\u52a1\u5df2\u521b\u5efa: [{code}] {task_id}\\n\u72b6\u6001: pending\\n\u5185\u5bb9: {text}"',
  't("msg.task_created", code="{code}", task_id="{task_id}", text="{text}")')

# ---- screenshot in pending action ----
r('run_screenshot_once(chat_id, txt or "\u8bf7\u622a\u56fe")', 'run_screenshot_once(chat_id, txt or "screenshot")')
r('send_text(chat_id, "\u622a\u56fe\u5931\u8d25: {}".format(str(exc)[:1000]))', 'send_text(chat_id, t("msg.screenshot_failed", err=str(exc)[:1000]))', 1)

# ---- handle_command: screenshot ----
r('run_screenshot_once(chat_id, body or "\u8bf7\u622a\u56fe")', 'run_screenshot_once(chat_id, body or "screenshot")')
r('send_text(chat_id, "\u622a\u56fe\u5931\u8d25: {}".format(str(exc)[:1000]))', 'send_text(chat_id, t("msg.screenshot_failed", err=str(exc)[:1000]))')

# ---- handle_command: /auth_status ----
r('"2FA \u672a\u521d\u59cb\u5316\u3002\u8bf7\u5148\u6267\u884c /auth_init"',
  't("msg.2fa_not_init")')
r('"2FA \u5df2\u542f\u7528', 't("msg.2fa_enabled") + "')

# Done - write back
with open(path, "w", encoding="utf-8") as f:
    f.write(content)

changed = sum(1 for a, b in zip(original, content) if a != b)
print(f"File written. ~{changed} character positions changed.")
print("Transform complete.")
