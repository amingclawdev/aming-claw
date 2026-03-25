"""Tests for governance doc_generator module (L9.4)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from governance.doc_generator import (
    generate_doc_skeleton,
    _make_section_key,
    on_node_created,
)


class TestMakeSectionKey(unittest.TestCase):
    def test_lowercase_conversion(self):
        # Uppercase lowered, dots replaced with underscores
        self.assertEqual(_make_section_key("L9.4"), "l9_4")

    def test_dot_to_underscore(self):
        self.assertEqual(_make_section_key("L1.1"), "l1_1")

    def test_already_lowercase(self):
        self.assertEqual(_make_section_key("mynode"), "mynode")


class TestGenerateDocSkeleton(unittest.TestCase):
    def test_no_primary_files_returns_none(self):
        result = generate_doc_skeleton("L1.1", {}, "proj")
        self.assertIsNone(result)

    def test_empty_primary_list_returns_none(self):
        result = generate_doc_skeleton("L1.1", {"primary": []}, "proj")
        self.assertIsNone(result)

    def test_missing_file_returns_none(self):
        result = generate_doc_skeleton(
            "L1.1",
            {"primary": ["/nonexistent/path.py"]},
            "proj",
        )
        self.assertIsNone(result)

    def test_non_py_file_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            js_file = os.path.join(tmpdir, "app.js")
            with open(js_file, "w") as f:
                f.write('@route("GET", "/hello")\n')

            result = generate_doc_skeleton(
                "L1.1",
                {"primary": [js_file]},
                "proj",
            )
            self.assertIsNone(result)

    def test_py_file_with_route_returns_skeleton(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            py_file = os.path.join(tmpdir, "api.py")
            with open(py_file, "w", encoding="utf-8") as f:
                f.write('@route("GET", "/users")\ndef get_users(): pass\n')
                f.write('@route("POST", "/users")\ndef create_user(): pass\n')

            result = generate_doc_skeleton(
                "L2.1",
                {"primary": [py_file], "title": "User API"},
                "proj",
            )

            self.assertIsNotNone(result)
            self.assertIn("api", result)
            self.assertIn("GET /users", result["api"])
            self.assertIn("POST /users", result["api"])
            self.assertTrue(result.get("_skeleton"))
            self.assertEqual(result["_node_id"], "L2.1")
            self.assertIn("L2.1", result["title"])
            self.assertIn("User API", result["title"])

    def test_py_file_without_routes_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            py_file = os.path.join(tmpdir, "utils.py")
            with open(py_file, "w", encoding="utf-8") as f:
                f.write("def helper(): pass\n")

            result = generate_doc_skeleton(
                "L1.1",
                {"primary": [py_file]},
                "proj",
            )
            self.assertIsNone(result)

    def test_description_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            py_file = os.path.join(tmpdir, "api.py")
            with open(py_file, "w", encoding="utf-8") as f:
                f.write('@route("DELETE", "/item")\ndef delete_item(): pass\n')

            result = generate_doc_skeleton(
                "L3.1",
                {"primary": [py_file]},
                "proj",
            )
            self.assertIsNotNone(result)
            # No description provided — should fall back to auto-generated text
            self.assertIn("L3.1", result["description"])

    def test_skeleton_api_value_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            py_file = os.path.join(tmpdir, "route.py")
            with open(py_file, "w", encoding="utf-8") as f:
                f.write('@route("GET", "/ping")\ndef ping(): pass\n')

            result = generate_doc_skeleton(
                "L1.1",
                {"primary": [py_file]},
                "proj",
            )
            api_value = result["api"]["GET /ping"]
            self.assertIn("AUTO-GENERATED SKELETON", api_value)
            self.assertIn("TODO", api_value)

    def test_generated_at_field_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            py_file = os.path.join(tmpdir, "api.py")
            with open(py_file, "w", encoding="utf-8") as f:
                f.write('@route("GET", "/health")\ndef health(): pass\n')

            result = generate_doc_skeleton(
                "L1.1",
                {"primary": [py_file]},
                "proj",
            )
            self.assertIn("_generated_at", result)
            self.assertRegex(result["_generated_at"], r"\d{4}-\d{2}-\d{2}T")


class TestOnNodeCreated(unittest.TestCase):
    def test_empty_payload_no_error(self):
        """on_node_created with empty payload should not raise."""
        on_node_created({})

    def test_missing_node_id_no_error(self):
        on_node_created({"node_data": {"primary": []}, "project_id": "p"})

    def test_missing_node_data_no_error(self):
        on_node_created({"node_id": "L1.1", "project_id": "p"})

    def test_full_payload_no_routes_no_registration(self):
        """Payload with no routes found should not raise."""
        with tempfile.TemporaryDirectory() as tmpdir:
            py_file = os.path.join(tmpdir, "mod.py")
            with open(py_file, "w", encoding="utf-8") as f:
                f.write("x = 1\n")
            # Should run without error even if registration fails
            on_node_created({
                "node_id": "L1.1",
                "project_id": "p",
                "node_data": {"primary": [py_file], "title": "Mod"},
            })


if __name__ == "__main__":
    unittest.main()
