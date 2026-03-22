"""Tests for governance DAG graph module."""
import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from governance.graph import AcceptanceGraph
from governance.models import NodeDef
from governance.errors import NodeNotFoundError, DAGError


class TestAcceptanceGraph(unittest.TestCase):
    def setUp(self):
        self.graph = AcceptanceGraph()

    def test_add_node_basic(self):
        node = NodeDef(id="L0.1", title="Test node", layer="L0", verify_level=1)
        warnings = self.graph.add_node(node)
        self.assertTrue(self.graph.has_node("L0.1"))
        self.assertEqual(self.graph.node_count(), 1)

    def test_add_node_with_deps(self):
        self.graph.add_node(NodeDef(id="L0.1", layer="L0"))
        self.graph.add_node(NodeDef(id="L1.1", layer="L1"), deps=["L0.1"])
        self.assertIn("L0.1", self.graph.direct_deps("L1.1"))
        self.assertIn("L1.1", self.graph.direct_dependents("L0.1"))

    def test_add_node_missing_dep_raises(self):
        with self.assertRaises(NodeNotFoundError):
            self.graph.add_node(NodeDef(id="L1.1", layer="L1"), deps=["L0.99"])

    def test_cycle_detection(self):
        self.graph.add_node(NodeDef(id="L0.1", layer="L0"))
        self.graph.add_node(NodeDef(id="L1.1", layer="L1"), deps=["L0.1"])
        # Manually add reverse edge to create cycle
        self.graph.G.add_edge("L1.1", "L0.1")
        errors = self.graph.validate_dag()
        self.assertTrue(len(errors) > 0)

    def test_topological_order(self):
        self.graph.add_node(NodeDef(id="L0.1", layer="L0"))
        self.graph.add_node(NodeDef(id="L0.2", layer="L0"))
        self.graph.add_node(NodeDef(id="L1.1", layer="L1"), deps=["L0.1", "L0.2"])
        order = self.graph.topological_order()
        self.assertIn("L0.1", order)
        self.assertIn("L1.1", order)
        self.assertLess(order.index("L0.1"), order.index("L1.1"))

    def test_ancestors_descendants(self):
        self.graph.add_node(NodeDef(id="L0.1", layer="L0"))
        self.graph.add_node(NodeDef(id="L1.1", layer="L1"), deps=["L0.1"])
        self.graph.add_node(NodeDef(id="L2.1", layer="L2"), deps=["L1.1"])

        self.assertEqual(self.graph.ancestors("L2.1"), {"L0.1", "L1.1"})
        self.assertEqual(self.graph.descendants("L0.1"), {"L1.1", "L2.1"})

    def test_ancestors_nonexistent(self):
        with self.assertRaises(NodeNotFoundError):
            self.graph.ancestors("L99.1")

    def test_remove_node(self):
        self.graph.add_node(NodeDef(id="L0.1", layer="L0"))
        self.graph.remove_node("L0.1")
        self.assertFalse(self.graph.has_node("L0.1"))

    def test_affected_by_files(self):
        self.graph.add_node(NodeDef(id="L0.1", layer="L0", primary=["server.js"]))
        self.graph.add_node(NodeDef(id="L1.1", layer="L1"), deps=["L0.1"])
        affected = self.graph.affected_nodes_by_files(["server.js"])
        self.assertIn("L0.1", affected)
        self.assertIn("L1.1", affected)  # downstream

    def test_max_verify_level(self):
        self.graph.add_node(NodeDef(id="L0.1", layer="L0", verify_level=1))
        self.graph.add_node(NodeDef(id="L1.1", layer="L1", verify_level=2), deps=["L0.1"])
        self.graph.add_node(NodeDef(id="L2.1", layer="L2", verify_level=4), deps=["L1.1"])
        self.assertEqual(self.graph.max_verify_level("L0.1"), 4)

    def test_save_load_roundtrip(self):
        self.graph.add_node(NodeDef(id="L0.1", layer="L0", title="Test", primary=["a.js"]))
        self.graph.add_node(NodeDef(id="L1.1", layer="L1"), deps=["L0.1"])

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            path = f.name
        try:
            self.graph.save(path)
            loaded = AcceptanceGraph()
            loaded.load(path)
            self.assertTrue(loaded.has_node("L0.1"))
            self.assertTrue(loaded.has_node("L1.1"))
            self.assertEqual(loaded.node_count(), 2)
        finally:
            os.unlink(path)

    def test_export_mermaid(self):
        self.graph.add_node(NodeDef(id="L0.1", title="Test"))
        mermaid = self.graph.export_mermaid({"L0.1": "qa_pass"})
        self.assertIn("graph TD", mermaid)
        self.assertIn("L0.1", mermaid)

    def test_auto_derive_gates(self):
        self.graph.add_node(NodeDef(id="L0.1", verify_level=2))
        self.graph.add_node(NodeDef(id="L0.2", verify_level=4))
        self.graph.add_node(NodeDef(id="L1.1", gate_mode="auto"), deps=["L0.1", "L0.2"])
        gates = self.graph.auto_derive_gates("L1.1")
        self.assertIn("L0.2", gates)     # verify_level=4 >= 3
        self.assertNotIn("L0.1", gates)  # verify_level=2 < 3

    def test_get_gates(self):
        self.graph.add_node(NodeDef(id="L0.1"))
        gates_data = [{"node_id": "L0.1", "min_status": "t2_pass", "policy": "default"}]
        self.graph.add_node(NodeDef(id="L1.1", gates=gates_data, gate_mode="explicit"), deps=["L0.1"])
        gates = self.graph.get_gates("L1.1")
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0].node_id, "L0.1")
        self.assertEqual(gates[0].min_status, "t2_pass")


class TestMarkdownImport(unittest.TestCase):
    def test_import_simple(self):
        md_content = """# Test Graph

## L0

```
L0.1  Test Node  [impl:done] [verify:pass] v1.0 GUARD
      deps:[]
      gate_mode: auto
      verify: L1
      test_coverage: none
      primary:[server.js]
      secondary:[]
      test:[server.test.js]

L0.2  Another Node  [impl:done] [verify:T2-pass] v1.0
      deps:[]
      gate_mode: auto
      verify: L2
      test_coverage: partial
      primary:[config.js]
      secondary:[utils.js]
      test:[config.test.js]
```

## L1

```
L1.1  Service  [impl:done] [verify:pending] v1.0
      deps:[L0.1, L0.2]
      gate_mode: explicit
      gates:[L0.1]
      verify: L2
      test_coverage: partial
      primary:[service.js]
      secondary:[]
      test:[service.test.js]
```
"""
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
            f.write(md_content)
            path = f.name

        try:
            graph = AcceptanceGraph()
            result = graph.import_from_markdown(path)
            self.assertGreaterEqual(result["nodes_parsed"], 3)
            self.assertTrue(graph.has_node("L0.1"))
            self.assertTrue(graph.has_node("L0.2"))
            self.assertTrue(graph.has_node("L1.1"))

            # Check parsed attributes
            node = graph.get_node("L0.1")
            self.assertIn("server.js", node.get("primary", []))
            self.assertTrue(node.get("guard", False))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
