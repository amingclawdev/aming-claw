"""Tests for GET /kpi endpoint in executor_api.

Covers:
  - /kpi with no task files → total_tasks 0, all metrics null
  - /kpi counts tasks from both archive/ and results/
  - first_pass_rate computed correctly (retry_count == 0)
  - avg_retry_rounds computed correctly
  - validator_reject_rate for "rejected" validator_result
  - wrong_file_rate for truthy wrong_file field
  - manual_downgrade_rate for truthy manual_downgrade field
  - observer_report fields used as fallback for missing top-level fields
  - malformed JSON files are silently skipped
  - metrics rounded to 4 decimal places
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


def _find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get_json(url: str, timeout: int = 5) -> tuple:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestKpiEndpoint(unittest.TestCase):
    """Integration tests for GET /kpi via live HTTP server."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._shared_root = Path(self._tmpdir.name)

        port = _find_free_port()
        self._port = port
        self._base = f"http://127.0.0.1:{port}"

        import executor_api
        from http.server import HTTPServer
        server = HTTPServer(("127.0.0.1", port), executor_api.ExecutorAPIHandler)
        self._server = server
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        time.sleep(0.15)

    def tearDown(self):
        self._server.shutdown()
        self._tmpdir.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────

    def _tasks_root(self):
        return self._shared_root / "codex-tasks"

    def _write_task(self, stage: str, task_id: str, data: dict):
        d = self._tasks_root() / stage
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{task_id}.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def _get_kpi(self):
        with patch_env({"SHARED_VOLUME_PATH": str(self._shared_root)}):
            return _get_json(f"{self._base}/kpi")

    # ── tests ─────────────────────────────────────────────────────────────

    def test_no_tasks_returns_null_metrics(self):
        """With no task files the endpoint returns total_tasks=0 and all null metrics."""
        status, body = self._get_kpi()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["total_tasks"], 0)
        for key in ("first_pass_rate", "avg_retry_rounds",
                    "validator_reject_rate", "wrong_file_rate",
                    "manual_downgrade_rate"):
            self.assertIsNone(body[key], f"{key} should be null when no tasks")

    def test_counts_tasks_from_archive_and_results(self):
        """Tasks from both archive/ and results/ directories are counted."""
        self._write_task("archive", "t1", {"retry_count": 0})
        self._write_task("results", "t2", {"retry_count": 0})
        status, body = self._get_kpi()
        self.assertEqual(status, 200)
        self.assertEqual(body["total_tasks"], 2)

    def test_first_pass_rate_all_pass(self):
        """first_pass_rate == 1.0 when all tasks have retry_count == 0."""
        for i in range(3):
            self._write_task("archive", f"t{i}", {"retry_count": 0})
        _, body = self._get_kpi()
        self.assertEqual(body["first_pass_rate"], 1.0)

    def test_first_pass_rate_none_pass(self):
        """first_pass_rate == 0.0 when all tasks have retry_count > 0."""
        for i in range(4):
            self._write_task("archive", f"t{i}", {"retry_count": 2})
        _, body = self._get_kpi()
        self.assertEqual(body["first_pass_rate"], 0.0)

    def test_first_pass_rate_partial(self):
        """first_pass_rate computed correctly for mixed retry counts."""
        self._write_task("archive", "t0", {"retry_count": 0})
        self._write_task("archive", "t1", {"retry_count": 1})
        self._write_task("archive", "t2", {"retry_count": 0})
        self._write_task("archive", "t3", {"retry_count": 3})
        _, body = self._get_kpi()
        self.assertEqual(body["first_pass_rate"], 0.5)

    def test_avg_retry_rounds(self):
        """avg_retry_rounds is the mean of all retry_count values."""
        self._write_task("archive", "t0", {"retry_count": 0})
        self._write_task("archive", "t1", {"retry_count": 2})
        self._write_task("archive", "t2", {"retry_count": 4})
        _, body = self._get_kpi()
        # (0 + 2 + 4) / 3 = 2.0
        self.assertAlmostEqual(body["avg_retry_rounds"], 2.0, places=4)

    def test_validator_reject_rate(self):
        """validator_reject_rate counts tasks with validator_result == 'rejected'."""
        self._write_task("archive", "t0", {"validator_result": "approved"})
        self._write_task("archive", "t1", {"validator_result": "rejected"})
        self._write_task("archive", "t2", {"validator_result": "rejected"})
        _, body = self._get_kpi()
        self.assertAlmostEqual(body["validator_reject_rate"], round(2 / 3, 4), places=4)

    def test_validator_reject_case_insensitive(self):
        """validator_result 'REJECTED' (uppercase) is counted."""
        self._write_task("archive", "t0", {"validator_result": "REJECTED"})
        self._write_task("archive", "t1", {"validator_result": "approved"})
        _, body = self._get_kpi()
        self.assertEqual(body["validator_reject_rate"], 0.5)

    def test_wrong_file_rate(self):
        """wrong_file_rate counts tasks with truthy wrong_file field."""
        self._write_task("archive", "t0", {"wrong_file": True})
        self._write_task("archive", "t1", {"wrong_file": False})
        self._write_task("archive", "t2", {"wrong_file": True})
        _, body = self._get_kpi()
        self.assertAlmostEqual(body["wrong_file_rate"], round(2 / 3, 4), places=4)

    def test_manual_downgrade_rate(self):
        """manual_downgrade_rate counts tasks with truthy manual_downgrade field."""
        self._write_task("archive", "t0", {"manual_downgrade": True})
        self._write_task("archive", "t1", {"manual_downgrade": False})
        _, body = self._get_kpi()
        self.assertEqual(body["manual_downgrade_rate"], 0.5)

    def test_observer_report_fallback(self):
        """Fields missing at top-level are read from observer_report dict."""
        self._write_task("archive", "t0", {
            "observer_report": {
                "retry_count": 3,
                "validator_result": "rejected",
                "wrong_file": True,
                "manual_downgrade": True,
            }
        })
        _, body = self._get_kpi()
        self.assertEqual(body["total_tasks"], 1)
        self.assertEqual(body["first_pass_rate"], 0.0)
        self.assertAlmostEqual(body["avg_retry_rounds"], 3.0, places=4)
        self.assertEqual(body["validator_reject_rate"], 1.0)
        self.assertEqual(body["wrong_file_rate"], 1.0)
        self.assertEqual(body["manual_downgrade_rate"], 1.0)

    def test_top_level_overrides_observer_report(self):
        """Top-level fields take precedence over observer_report values."""
        self._write_task("archive", "t0", {
            "retry_count": 0,  # top-level: first pass
            "observer_report": {
                "retry_count": 5,  # should be ignored
            }
        })
        _, body = self._get_kpi()
        self.assertEqual(body["first_pass_rate"], 1.0)
        self.assertEqual(body["avg_retry_rounds"], 0.0)

    def test_malformed_json_skipped(self):
        """A task file with invalid JSON is silently ignored."""
        d = self._tasks_root() / "archive"
        d.mkdir(parents=True, exist_ok=True)
        (d / "bad.json").write_text("{not valid json}", encoding="utf-8")
        self._write_task("archive", "good", {"retry_count": 0})
        _, body = self._get_kpi()
        self.assertEqual(body["total_tasks"], 1)

    def test_missing_fields_default_gracefully(self):
        """Task files with no KPI fields default to 0/False (first pass, no rejects)."""
        self._write_task("archive", "empty", {})
        _, body = self._get_kpi()
        self.assertEqual(body["total_tasks"], 1)
        self.assertEqual(body["first_pass_rate"], 1.0)
        self.assertEqual(body["avg_retry_rounds"], 0.0)
        self.assertEqual(body["validator_reject_rate"], 0.0)
        self.assertEqual(body["wrong_file_rate"], 0.0)
        self.assertEqual(body["manual_downgrade_rate"], 0.0)

    def test_metrics_rounded_to_4_decimal_places(self):
        """Each metric is rounded to exactly 4 decimal places."""
        # 1/3 ≈ 0.3333 (4dp)
        for i in range(3):
            self._write_task("archive", f"t{i}", {
                "retry_count": i,
                "validator_result": "rejected" if i == 0 else "approved",
                "wrong_file": i == 0,
                "manual_downgrade": i == 0,
            })
        _, body = self._get_kpi()
        for key in ("first_pass_rate", "avg_retry_rounds",
                    "validator_reject_rate", "wrong_file_rate",
                    "manual_downgrade_rate"):
            val = body[key]
            self.assertIsInstance(val, (int, float), key)
            # Must not exceed 4 decimal places
            formatted = f"{val:.10f}".rstrip("0")
            decimals = len(formatted.split(".")[1]) if "." in formatted else 0
            self.assertLessEqual(decimals, 4, f"{key}={val} has too many decimals")

    def test_pending_tasks_not_counted(self):
        """Tasks in pending/ stage are NOT included in KPI stats."""
        self._write_task("pending", "pending1", {"retry_count": 5})
        self._write_task("archive", "done1", {"retry_count": 0})
        _, body = self._get_kpi()
        self.assertEqual(body["total_tasks"], 1)


# ── Minimal patch_env context-manager helper ──────────────────────────────

class patch_env:
    """Temporarily override os.environ keys for the duration of a with block."""

    def __init__(self, overrides: dict):
        self._overrides = overrides
        self._saved = {}

    def __enter__(self):
        for k, v in self._overrides.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
