"""Tests for task_accept.py - acceptance documents and finalization."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from task_accept import (  # noqa: E402
    acceptance_notice_text,
    acceptance_root,
    build_acceptance_cases,
    build_task_summary,
    finalize_codex_task,
    finalize_pipeline_task,
    format_elapsed,
    generate_stage_summary,
    json_sha256,
    task_inline_keyboard,
    to_pending_acceptance,
    write_acceptance_documents,
    write_run_log,
)
from utils import load_json, utc_iso  # noqa: E402


class TestJsonSha256(unittest.TestCase):
    def test_deterministic(self):
        data = {"key": "value", "num": 42}
        h1 = json_sha256(data)
        h2 = json_sha256(data)
        self.assertEqual(h1, h2)

    def test_different_data(self):
        h1 = json_sha256({"a": 1})
        h2 = json_sha256({"a": 2})
        self.assertNotEqual(h1, h2)

    def test_hash_length(self):
        h = json_sha256({"test": True})
        self.assertEqual(len(h), 64)  # SHA256 hex digest


class TestWriteRunLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_creates_log_file(self):
        path = write_run_log("task-log-1", {"cmd": "echo", "returncode": 0})
        self.assertTrue(path.exists())
        self.assertIn("task-log-1", str(path))


class TestBuildAcceptanceCases(unittest.TestCase):
    def test_all_cases_generated(self):
        task = {"task_id": "t1", "text": "修复bug", "action": "codex"}
        result = {
            "status": "completed",
            "executor": {
                "last_message": "done",
                "runlog_file": "/tmp/log.json",
                "git_changed_files": ["fix.py"],
            },
        }
        cases = build_acceptance_cases(task, result)
        self.assertEqual(len(cases), 5)
        ids = {c["case_id"] for c in cases}
        self.assertIn("AC-000", ids)
        self.assertIn("AC-001", ids)
        self.assertIn("AC-002", ids)
        self.assertIn("AC-003", ids)
        self.assertIn("UAT-001", ids)

    def test_empty_text_fails_ac000(self):
        task = {"task_id": "t2", "text": "", "action": "codex"}
        result = {"status": "completed", "executor": {}}
        cases = build_acceptance_cases(task, result)
        ac000 = next(c for c in cases if c["case_id"] == "AC-000")
        self.assertEqual(ac000["status"], "failed")

    def test_completed_passes_ac001(self):
        task = {"task_id": "t3", "text": "test", "action": "codex"}
        result = {"status": "completed", "executor": {"last_message": "ok"}}
        cases = build_acceptance_cases(task, result)
        ac001 = next(c for c in cases if c["case_id"] == "AC-001")
        self.assertEqual(ac001["status"], "passed")

    def test_failed_fails_ac001(self):
        task = {"task_id": "t4", "text": "test", "action": "codex"}
        result = {"status": "failed", "executor": {"last_message": "error"}}
        cases = build_acceptance_cases(task, result)
        ac001 = next(c for c in cases if c["case_id"] == "AC-001")
        self.assertEqual(ac001["status"], "failed")

    def test_uat001_always_pending(self):
        task = {"task_id": "t5", "text": "test", "action": "codex"}
        result = {"status": "completed", "executor": {}}
        cases = build_acceptance_cases(task, result)
        uat001 = next(c for c in cases if c["case_id"] == "UAT-001")
        self.assertEqual(uat001["status"], "pending")


class TestWriteAcceptanceDocuments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_generates_doc_and_cases(self):
        task = {"task_id": "task-doc-1", "text": "测试任务", "action": "codex"}
        result = {
            "task_code": "T0001",
            "status": "completed",
            "executor": {
                "elapsed_ms": 500,
                "returncode": 0,
                "workspace": "/ws",
                "last_message": "完成",
                "runlog_file": "",
                "git_changed_files": [],
            },
        }
        docs = write_acceptance_documents(task, result)
        self.assertTrue(Path(docs["doc_file"]).exists())
        self.assertTrue(Path(docs["cases_file"]).exists())

        # Check doc content
        content = Path(docs["doc_file"]).read_text(encoding="utf-8")
        self.assertIn("验收结论", content)
        self.assertIn("task-doc-1", content)

        # Check cases content
        cases_data = json.loads(Path(docs["cases_file"]).read_text(encoding="utf-8"))
        self.assertIn("cases", cases_data)


class TestToPendingAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_sets_pending_acceptance(self):
        task = {"task_id": "task-pa-1", "text": "test", "action": "codex"}
        result = {"status": "completed", "executor": {"last_message": "ok"}}
        out = to_pending_acceptance(task, result)
        self.assertEqual(out["status"], "pending_acceptance")
        self.assertEqual(out["execution_status"], "completed")
        self.assertEqual(out["acceptance"]["state"], "pending")
        self.assertTrue(out["acceptance"]["acceptance_required"])
        self.assertFalse(out["acceptance"]["archive_allowed"])

    def test_preserves_execution_status(self):
        task = {"task_id": "task-pa-2", "text": "test", "action": "codex"}
        result = {"status": "failed", "executor": {"last_message": "err"}}
        out = to_pending_acceptance(task, result)
        self.assertEqual(out["execution_status"], "failed")
        self.assertEqual(out["status"], "pending_acceptance")


class TestBuildTaskSummary(unittest.TestCase):
    def test_with_message(self):
        result = {"executor": {"last_message": "任务完成", "noop_reason": ""}}
        self.assertEqual(build_task_summary(result), "任务完成")

    def test_with_noop(self):
        result = {"executor": {"last_message": "", "noop_reason": "无执行"}}
        self.assertIn("失败原因", build_task_summary(result))

    def test_with_error(self):
        result = {"error": "timeout", "executor": {}}
        self.assertIn("错误", build_task_summary(result))

    def test_fallback(self):
        result = {"executor": {}}
        self.assertIn("日志文件", build_task_summary(result))


class TestAcceptanceNoticeText(unittest.TestCase):
    def test_completed_notice(self):
        result = {
            "execution_status": "completed",
            "executor": {"elapsed_ms": 1000, "last_message": "ok"},
        }
        text = acceptance_notice_text(result, "task-1", "T0001", detailed=True)
        self.assertIn("T0001", text)
        self.assertIn("\u2705", text)  # success emoji
        self.assertIn("\u6267\u884c\u5b8c\u6210", text)
        # New format: no UUID, no /accept, no pending_acceptance
        self.assertNotIn("task-1", text)
        self.assertNotIn("/accept", text)
        self.assertNotIn("pending_acceptance", text)

    def test_failed_notice(self):
        result = {
            "execution_status": "failed",
            "executor": {"elapsed_ms": 500, "noop_reason": "\u65e0\u6267\u884c"},
        }
        text = acceptance_notice_text(result, "task-2", "T0002", detailed=False)
        self.assertIn("\u6267\u884c\u5931\u8d25", text)
        self.assertIn("T0002", text)
        # New format: no /reject command prompt
        self.assertNotIn("/reject", text)
        self.assertNotIn("pending_acceptance", text)

    def test_notice_no_uuid(self):
        """UUID task_id should not appear in the notice text."""
        result = {
            "execution_status": "completed",
            "executor": {"elapsed_ms": 3000, "last_message": "done"},
        }
        text = acceptance_notice_text(result, "task-uuid-12345", "T0042", detailed=True)
        self.assertNotIn("task-uuid-12345", text)
        self.assertIn("T0042", text)

    def test_elapsed_human_readable(self):
        """Elapsed time should be in seconds/minutes, not ms."""
        result = {
            "execution_status": "completed",
            "executor": {"elapsed_ms": 83000, "last_message": "done"},
        }
        text = acceptance_notice_text(result, "t1", "T1", detailed=True)
        self.assertIn("\u79d2", text)  # "秒" in the output
        self.assertNotIn("83000 ms", text)

    def test_brief_same_format(self):
        """Brief and detailed now use the same format (simplified)."""
        result = {
            "execution_status": "completed",
            "executor": {"elapsed_ms": 1000, "last_message": "ok"},
        }
        brief = acceptance_notice_text(result, "t1", "T1", detailed=False)
        detailed = acceptance_notice_text(result, "t1", "T1", detailed=True)
        # Both should be the same simplified format
        self.assertEqual(brief, detailed)


class TestTaskInlineKeyboard(unittest.TestCase):
    def test_keyboard_structure(self):
        kb = task_inline_keyboard("T0001", "task-1")
        self.assertIn("inline_keyboard", kb)
        rows = kb["inline_keyboard"]
        self.assertGreater(len(rows), 0)
        # Check first row has buttons
        self.assertGreater(len(rows[0]), 0)
        # Check callback data contains task code
        all_data = [btn["callback_data"] for row in rows for btn in row]
        self.assertTrue(any("T0001" in d for d in all_data))
        # New format: has accept/reject and doc/detail, no status button
        self.assertIn("accept:T0001", all_data)
        self.assertIn("reject:T0001", all_data)
        self.assertIn("task_doc:T0001", all_data)
        self.assertIn("task_detail:T0001", all_data)


class TestFinalizePipelineTask(unittest.TestCase):
    """T3: Verify model/provider persistence in finalize_pipeline_task."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        from utils import tasks_root
        (tasks_root() / "processing").mkdir(parents=True, exist_ok=True)
        (tasks_root() / "logs").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_stage_summary_has_model_provider(self):
        """executor.stages[*] should include model and provider fields."""
        from utils import tasks_root
        task = {"task_id": "test-fp-1", "text": "test task"}
        processing = tasks_root() / "processing" / "test-fp-1.json"
        stage_results = [
            {
                "stage": "pm", "backend": "claude", "stage_index": 1,
                "model": "claude-opus-4-6", "provider": "anthropic",
                "run": {"returncode": 0, "elapsed_ms": 1000, "last_message": "done",
                        "stdout": "out", "stderr": "", "cmd": "echo", "noop_reason": None,
                        "attempt_count": 1, "git_changed_files": []},
            },
            {
                "stage": "dev", "backend": "claude", "stage_index": 2,
                "model": "gpt-4o", "provider": "openai",
                "run": {"returncode": 0, "elapsed_ms": 2000, "last_message": "code done",
                        "stdout": "out2", "stderr": "", "cmd": "echo2", "noop_reason": None,
                        "attempt_count": 1, "git_changed_files": ["a.py"]},
            },
        ]
        stages_model_info = [
            {"stage": "pm", "model": "claude-opus-4-6", "provider": "anthropic"},
            {"stage": "dev", "model": "gpt-4o", "provider": "openai"},
        ]
        result = finalize_pipeline_task(task, processing, stage_results, "completed",
                                        stages_model_info=stages_model_info)
        # Check executor.stages has model/provider
        stages = result["executor"]["stages"]
        self.assertEqual(stages[0]["model"], "claude-opus-4-6")
        self.assertEqual(stages[0]["provider"], "anthropic")
        self.assertEqual(stages[1]["model"], "gpt-4o")
        self.assertEqual(stages[1]["provider"], "openai")
        # Check stages_model_info top-level field
        self.assertIn("stages_model_info", result)
        self.assertEqual(len(result["stages_model_info"]), 2)

    def test_run_log_stage_details_has_model_provider(self):
        """logs/{task_id}.run.json stage_details should include model/provider."""
        from utils import tasks_root
        task = {"task_id": "test-fp-2", "text": "test task"}
        processing = tasks_root() / "processing" / "test-fp-2.json"
        stage_results = [
            {
                "stage": "test", "backend": "openai", "stage_index": 1,
                "model": "gpt-4o", "provider": "openai",
                "run": {"returncode": 0, "elapsed_ms": 500, "last_message": "ok",
                        "stdout": "ok", "stderr": "", "cmd": "run", "noop_reason": None,
                        "attempt_count": 1, "git_changed_files": []},
            },
        ]
        finalize_pipeline_task(task, processing, stage_results, "completed")
        # Read run log
        run_log_path = tasks_root() / "logs" / "test-fp-2.run.json"
        self.assertTrue(run_log_path.exists())
        run_data = load_json(run_log_path)
        sd = run_data["stage_details"]
        self.assertEqual(len(sd), 1)
        self.assertEqual(sd[0]["model"], "gpt-4o")
        self.assertEqual(sd[0]["provider"], "openai")

    def test_result_file_persisted_with_model(self):
        """Result JSON file should contain model/provider in stages."""
        from utils import tasks_root
        task = {"task_id": "test-fp-3", "text": "test task"}
        processing = tasks_root() / "processing" / "test-fp-3.json"
        stage_results = [
            {
                "stage": "qa", "backend": "claude", "stage_index": 1,
                "model": "claude-sonnet-4-6", "provider": "anthropic",
                "run": {"returncode": 0, "elapsed_ms": 300, "last_message": "pass",
                        "stdout": "pass", "stderr": "", "cmd": "test", "noop_reason": None,
                        "attempt_count": 1, "git_changed_files": []},
            },
        ]
        finalize_pipeline_task(task, processing, stage_results, "completed")
        # Read persisted file
        saved = load_json(processing)
        self.assertEqual(saved["executor"]["stages"][0]["model"], "claude-sonnet-4-6")
        self.assertEqual(saved["executor"]["stages"][0]["provider"], "anthropic")

    def test_backward_compat_no_model(self):
        """Stage results without model/provider should default to empty string."""
        from utils import tasks_root
        task = {"task_id": "test-fp-4", "text": "test task"}
        processing = tasks_root() / "processing" / "test-fp-4.json"
        stage_results = [
            {
                "stage": "code", "backend": "codex", "stage_index": 1,
                "run": {"returncode": 0, "elapsed_ms": 100, "last_message": "done",
                        "stdout": "out", "stderr": "", "cmd": "run", "noop_reason": None,
                        "attempt_count": 1, "git_changed_files": []},
            },
        ]
        result = finalize_pipeline_task(task, processing, stage_results, "completed")
        self.assertEqual(result["executor"]["stages"][0]["model"], "")
        self.assertEqual(result["executor"]["stages"][0]["provider"], "")


class TestFormatElapsed(unittest.TestCase):
    """Tests for format_elapsed helper."""

    def test_seconds(self):
        self.assertIn("\u79d2", format_elapsed(1500))
        self.assertIn("1.5", format_elapsed(1500))

    def test_minutes(self):
        result = format_elapsed(83000)
        self.assertIn("1 \u5206", result)
        self.assertIn("23 \u79d2", result)

    def test_zero(self):
        result = format_elapsed(0)
        self.assertIn("\u79d2", result)

    def test_none(self):
        result = format_elapsed(None)
        self.assertIn("\u79d2", result)

    def test_exact_minutes(self):
        result = format_elapsed(120000)
        self.assertIn("2 \u5206\u949f", result)


class TestGenerateStageSummary(unittest.TestCase):
    """Tests for generate_stage_summary."""

    def test_with_last_message(self):
        result = generate_stage_summary({
            "last_message": "\u5df2\u5b8c\u6210\u63a5\u53e3\u4e0e\u53c2\u6570\u6821\u9a8c",
            "git_changed_files": ["api/status.py"],
        })
        self.assertIn("\u5b8c\u6210", result)
        self.assertIn("status.py", result)
        self.assertLessEqual(len(result), 200)

    def test_with_noop(self):
        result = generate_stage_summary({
            "last_message": "",
            "noop_reason": "ack only",
            "git_changed_files": [],
        })
        self.assertIn("\u672a\u6267\u884c", result)

    def test_with_error(self):
        result = generate_stage_summary({
            "last_message": "",
            "error": "timeout exceeded",
        })
        self.assertIn("\u9519\u8bef", result)

    def test_empty_result(self):
        result = generate_stage_summary({})
        self.assertEqual(result, "(\u65e0\u6267\u884c\u8f93\u51fa)")
        self.assertLessEqual(len(result), 200)

    def test_max_200_chars(self):
        result = generate_stage_summary({
            "last_message": "A" * 500,
            "git_changed_files": ["file{}.py".format(i) for i in range(20)],
        })
        self.assertLessEqual(len(result), 200)


class TestSummaryFileGeneration(unittest.TestCase):
    """Test that summary files are generated during finalization."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        from utils import tasks_root
        (tasks_root() / "processing").mkdir(parents=True, exist_ok=True)
        (tasks_root() / "logs").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_codex_generates_summary_file(self):
        from utils import tasks_root
        task = {"task_id": "test-sum-1", "text": "test task"}
        processing = tasks_root() / "processing" / "test-sum-1.json"
        run = {
            "returncode": 0, "elapsed_ms": 1000, "last_message": "done ok",
            "stdout": "output", "stderr": "", "cmd": "echo",
            "git_changed_files": ["a.py"], "noop_reason": None,
            "attempt_count": 1, "attempt_tag": "", "workspace": "/ws",
        }
        finalize_codex_task(task, processing, run, "completed")
        summary_path = tasks_root() / "logs" / "test-sum-1.summary.txt"
        self.assertTrue(summary_path.exists())
        content = summary_path.read_text(encoding="utf-8")
        self.assertGreater(len(content), 0)
        self.assertLessEqual(len(content), 200)

    def test_codex_run_log_has_summary(self):
        from utils import tasks_root
        task = {"task_id": "test-sum-2", "text": "test task"}
        processing = tasks_root() / "processing" / "test-sum-2.json"
        run = {
            "returncode": 0, "elapsed_ms": 500, "last_message": "completed",
            "stdout": "out", "stderr": "", "cmd": "echo",
            "git_changed_files": [], "noop_reason": None,
            "attempt_count": 1, "attempt_tag": "", "workspace": "/ws",
        }
        finalize_codex_task(task, processing, run, "completed")
        run_log = load_json(tasks_root() / "logs" / "test-sum-2.run.json")
        self.assertIn("summary", run_log)
        self.assertGreater(len(run_log["summary"]), 0)

    def test_pipeline_generates_summary_file(self):
        from utils import tasks_root
        task = {"task_id": "test-sum-3", "text": "test task"}
        processing = tasks_root() / "processing" / "test-sum-3.json"
        stage_results = [
            {
                "stage": "dev", "backend": "claude", "stage_index": 1,
                "run": {"returncode": 0, "elapsed_ms": 1000, "last_message": "code done",
                        "stdout": "out", "stderr": "", "cmd": "echo", "noop_reason": None,
                        "attempt_count": 1, "git_changed_files": ["a.py"]},
            },
        ]
        finalize_pipeline_task(task, processing, stage_results, "completed")
        summary_path = tasks_root() / "logs" / "test-sum-3.summary.txt"
        self.assertTrue(summary_path.exists())

    def test_pipeline_stage_details_have_summary(self):
        from utils import tasks_root
        task = {"task_id": "test-sum-4", "text": "test task"}
        processing = tasks_root() / "processing" / "test-sum-4.json"
        stage_results = [
            {
                "stage": "pm", "backend": "claude", "stage_index": 1,
                "run": {"returncode": 0, "elapsed_ms": 500, "last_message": "plan done",
                        "stdout": "out", "stderr": "", "cmd": "echo", "noop_reason": None,
                        "attempt_count": 1, "git_changed_files": []},
            },
            {
                "stage": "dev", "backend": "codex", "stage_index": 2,
                "run": {"returncode": 0, "elapsed_ms": 2000, "last_message": "code done",
                        "stdout": "out2", "stderr": "", "cmd": "echo2", "noop_reason": None,
                        "attempt_count": 1, "git_changed_files": ["b.py"]},
            },
        ]
        finalize_pipeline_task(task, processing, stage_results, "completed")
        run_log = load_json(tasks_root() / "logs" / "test-sum-4.run.json")
        for sd in run_log["stage_details"]:
            self.assertIn("summary", sd)
            self.assertGreater(len(sd["summary"]), 0)
        self.assertIn("summary", run_log)


class TestTaskInlineKeyboardNew(unittest.TestCase):
    """Test the new simplified inline keyboard structure."""

    def test_no_status_button(self):
        """New keyboard should not have status button."""
        kb = task_inline_keyboard("T0001", "task-1")
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertFalse(any(d.startswith("status:") for d in all_data))

    def test_has_doc_and_detail_buttons(self):
        kb = task_inline_keyboard("T0001", "task-1")
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("task_doc:T0001", all_data)
        self.assertIn("task_detail:T0001", all_data)

    def test_has_accept_reject(self):
        kb = task_inline_keyboard("T0001", "task-1")
        all_data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        self.assertIn("accept:T0001", all_data)
        self.assertIn("reject:T0001", all_data)


if __name__ == "__main__":
    unittest.main()
