"""Tests for governance.llm_utils — keyword extraction and translation via CLI."""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestExtractKeywords(unittest.TestCase):
    """Test keyword extraction with mocked API."""

    @patch("governance.llm_utils._call_cli")
    def test_english_input(self, mock_api):
        mock_api.return_value = '["executor", "timeout", "heartbeat", "subprocess"]'
        from governance.llm_utils import extract_keywords
        result = extract_keywords("Fix executor subprocess timeout, implement heartbeat")
        self.assertEqual(result, ["executor", "timeout", "heartbeat", "subprocess"])

    @patch("governance.llm_utils._call_cli")
    def test_chinese_input(self, mock_api):
        mock_api.return_value = '["executor", "timeout", "heartbeat"]'
        from governance.llm_utils import extract_keywords
        result = extract_keywords("修改executor的subprocess超时机制，实现心跳延长")
        self.assertIn("executor", result)

    @patch("governance.llm_utils._call_cli")
    def test_fallback_on_api_failure(self, mock_api):
        mock_api.return_value = ""  # API failed
        from governance.llm_utils import extract_keywords
        result = extract_keywords("Fix the executor subprocess timeout")
        self.assertIsInstance(result, list)
        self.assertTrue(len(result) > 0)
        # Fallback uses naive word extraction
        self.assertIn("executor", result)

    @patch("governance.llm_utils._call_cli")
    def test_empty_input(self, mock_api):
        from governance.llm_utils import extract_keywords
        result = extract_keywords("")
        self.assertEqual(result, [])
        mock_api.assert_not_called()

    @patch("governance.llm_utils._call_cli")
    def test_max_keywords_limit(self, mock_api):
        mock_api.return_value = '["a", "b", "c", "d", "e", "f", "g"]'
        from governance.llm_utils import extract_keywords
        result = extract_keywords("some text", max_keywords=3)
        self.assertLessEqual(len(result), 3)


class TestTranslateToEnglish(unittest.TestCase):
    """Test English translation with mocked API."""

    @patch("governance.llm_utils._call_cli")
    def test_chinese_input_translated(self, mock_api):
        mock_api.return_value = "Gate blocked: PRD missing mandatory fields"
        from governance.llm_utils import translate_to_english
        result = translate_to_english("Gate blocked: PRD缺少必填字段")
        self.assertEqual(result, "Gate blocked: PRD missing mandatory fields")

    @patch("governance.llm_utils._call_cli")
    def test_english_input_unchanged(self, mock_api):
        from governance.llm_utils import translate_to_english
        result = translate_to_english("Already in English")
        self.assertEqual(result, "Already in English")
        mock_api.assert_not_called()  # Should not call API

    @patch("governance.llm_utils._call_cli")
    def test_fallback_on_failure(self, mock_api):
        mock_api.return_value = ""  # API failed
        from governance.llm_utils import translate_to_english
        original = "中文内容 with some English"
        result = translate_to_english(original)
        self.assertEqual(result, original)  # Returns original

    def test_empty_input(self):
        from governance.llm_utils import translate_to_english
        self.assertEqual(translate_to_english(""), "")
        self.assertEqual(translate_to_english(None), None)


if __name__ == "__main__":
    unittest.main()
