"""OPT-BACKLOG-CH2: Tests for chain-level bug_id propagation.

Validates that metadata.bug_id becomes durable at the chain level via
chain_context store, so retry tasks can fallback-fill missing bug_id
even when the in-process metadata dict gets dropped between stages.

Covers AC1-AC6 from the PRD; AC7 is this file itself.
"""

import os
import sys
import unittest

_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.chain_context import ChainContext, ChainContextStore


BUG_ID = "OPT-BACKLOG-CH2-TEST"


class TestChainContextBugIdField(unittest.TestCase):
    """AC1: ChainContext.bug_id field exists and defaults to None."""

    def test_fresh_chain_bug_id_is_none(self):
        chain = ChainContext("root-1", "proj")
        self.assertIsNone(chain.bug_id)

    def test_bug_id_is_in_slots(self):
        # __slots__ must list bug_id — otherwise setattr raises AttributeError
        chain = ChainContext("root-1", "proj")
        chain.bug_id = "anything"
        self.assertEqual(chain.bug_id, "anything")

    def test_bug_id_slot_rejects_unknown_attrs(self):
        # Ensure __slots__ discipline is still enforced
        chain = ChainContext("root-1", "proj")
        with self.assertRaises(AttributeError):
            chain.not_a_real_slot = "x"


class TestOnTaskCreatedBugIdExtraction(unittest.TestCase):
    """AC2: on_task_created extracts metadata.bug_id, first-write-wins."""

    def setUp(self):
        self.store = ChainContextStore()
        self.pid = "proj"

    def _create(self, task_id, task_type="pm", parent="", metadata=None):
        self.store.on_task_created({
            "task_id": task_id,
            "type": task_type,
            "prompt": "p",
            "parent_task_id": parent,
            "project_id": self.pid,
            "metadata": metadata or {},
        })

    def test_bug_id_extracted_from_payload_metadata(self):
        self._create("t1", metadata={"bug_id": BUG_ID})
        self.assertEqual(self.store.get_bug_id("t1"), BUG_ID)

    def test_first_write_wins_across_stages(self):
        # AC2: first call sets bug_id; later stage with different bug_id does NOT overwrite
        self._create("t1", "pm", metadata={"bug_id": BUG_ID})
        self._create("t2", "dev", parent="t1", metadata={"bug_id": "DIFFERENT-BUG"})
        # Both t1 and t2 are in the same chain; bug_id is on the chain, not per-stage
        self.assertEqual(self.store.get_bug_id("t1"), BUG_ID)
        self.assertEqual(self.store.get_bug_id("t2"), BUG_ID)

    def test_missing_metadata_leaves_bug_id_none(self):
        self._create("t1")  # no metadata
        self.assertIsNone(self.store.get_bug_id("t1"))

    def test_empty_string_bug_id_treated_as_missing(self):
        self._create("t1", metadata={"bug_id": ""})
        self.assertIsNone(self.store.get_bug_id("t1"))

    def test_non_string_bug_id_rejected(self):
        # Defensive: ints / lists / dicts must not pollute chain state
        self._create("t1", metadata={"bug_id": 12345})
        self.assertIsNone(self.store.get_bug_id("t1"))

    def test_late_arrival_populates_when_root_had_none(self):
        # Root created without bug_id → chain.bug_id=None.
        # Later stage arrives WITH bug_id → first-write-wins populates.
        self._create("t1", "pm")
        self.assertIsNone(self.store.get_bug_id("t1"))
        self._create("t2", "dev", parent="t1", metadata={"bug_id": BUG_ID})
        self.assertEqual(self.store.get_bug_id("t1"), BUG_ID)
        self.assertEqual(self.store.get_bug_id("t2"), BUG_ID)


class TestGetBugIdApi(unittest.TestCase):
    """AC3: ChainContextStore.get_bug_id(task_id) returns chain.bug_id or None."""

    def setUp(self):
        self.store = ChainContextStore()
        self.pid = "proj"

    def test_returns_bug_id_for_any_task_in_chain(self):
        self.store.on_task_created({
            "task_id": "t-pm", "type": "pm", "prompt": "p",
            "parent_task_id": "", "project_id": self.pid,
            "metadata": {"bug_id": BUG_ID},
        })
        self.store.on_task_created({
            "task_id": "t-dev", "type": "dev", "prompt": "p",
            "parent_task_id": "t-pm", "project_id": self.pid,
        })
        self.store.on_task_created({
            "task_id": "t-test", "type": "test", "prompt": "p",
            "parent_task_id": "t-dev", "project_id": self.pid,
        })
        # Every task in the chain sees the same bug_id
        self.assertEqual(self.store.get_bug_id("t-pm"), BUG_ID)
        self.assertEqual(self.store.get_bug_id("t-dev"), BUG_ID)
        self.assertEqual(self.store.get_bug_id("t-test"), BUG_ID)

    def test_returns_none_for_unknown_task(self):
        self.assertIsNone(self.store.get_bug_id("never-created"))

    def test_returns_none_for_chain_without_bug_id(self):
        self.store.on_task_created({
            "task_id": "t1", "type": "pm", "prompt": "p",
            "parent_task_id": "", "project_id": self.pid,
        })
        self.assertIsNone(self.store.get_bug_id("t1"))


class TestSerializeIncludesBugId(unittest.TestCase):
    """AC (serialize): bug_id in get_chain() output only when set."""

    def setUp(self):
        self.store = ChainContextStore()
        self.pid = "proj"

    def test_serialize_includes_bug_id_when_set(self):
        self.store.on_task_created({
            "task_id": "t1", "type": "pm", "prompt": "p",
            "parent_task_id": "", "project_id": self.pid,
            "metadata": {"bug_id": BUG_ID},
        })
        chain = self.store.get_chain("t1")
        self.assertEqual(chain.get("bug_id"), BUG_ID)

    def test_serialize_omits_bug_id_when_unset(self):
        self.store.on_task_created({
            "task_id": "t1", "type": "pm", "prompt": "p",
            "parent_task_id": "", "project_id": self.pid,
        })
        chain = self.store.get_chain("t1")
        # Backward compatible: absent key rather than "bug_id": None
        self.assertNotIn("bug_id", chain)


class TestRetryMetadataFallback(unittest.TestCase):
    """AC5: auto_chain retry paths fallback-fill missing bug_id from store.

    We simulate the fallback logic directly against the store; the actual
    auto_chain integration is covered by the live chain run (Chain 2 dogfood).
    """

    def setUp(self):
        self.store = ChainContextStore()
        self.pid = "proj"

    def _seed_chain(self, bug_id):
        self.store.on_task_created({
            "task_id": "t-pm", "type": "pm", "prompt": "p",
            "parent_task_id": "", "project_id": self.pid,
            "metadata": {"bug_id": bug_id} if bug_id else {},
        })
        self.store.on_task_created({
            "task_id": "t-dev", "type": "dev", "prompt": "p",
            "parent_task_id": "t-pm", "project_id": self.pid,
        })

    def _simulate_retry_fallback(self, retry_meta, parent_task_id):
        """Replicate the auto_chain CH2 fallback block."""
        if not retry_meta.get("bug_id"):
            chain_bug = self.store.get_bug_id(parent_task_id)
            if chain_bug:
                retry_meta["bug_id"] = chain_bug
        return retry_meta

    def test_fallback_fills_missing_bug_id(self):
        self._seed_chain(BUG_ID)
        # Metadata dropped bug_id somewhere upstream
        retry_meta = {"target_files": ["agent/foo.py"], "parent_task_id": "t-dev"}
        filled = self._simulate_retry_fallback(retry_meta, "t-dev")
        self.assertEqual(filled["bug_id"], BUG_ID)

    def test_fallback_preserves_existing_bug_id(self):
        self._seed_chain(BUG_ID)
        retry_meta = {"bug_id": "EXPLICIT-OVERRIDE", "parent_task_id": "t-dev"}
        filled = self._simulate_retry_fallback(retry_meta, "t-dev")
        self.assertEqual(filled["bug_id"], "EXPLICIT-OVERRIDE")  # not overwritten

    def test_fallback_noop_when_chain_has_no_bug_id(self):
        self._seed_chain(None)
        retry_meta = {"parent_task_id": "t-dev"}
        filled = self._simulate_retry_fallback(retry_meta, "t-dev")
        self.assertNotIn("bug_id", filled)


class TestRecoveryRestoresBugId(unittest.TestCase):
    """AC6: recover_from_db replay restores chain.bug_id from persisted events."""

    def test_recovery_replays_bug_id(self):
        # Simulate DB replay: replay task.created events into a fresh store.
        # This mirrors what recover_from_db does after a crash.
        store = ChainContextStore()
        store._recovering = True  # suppress DB writes

        # Original event payload had metadata.bug_id (as written by our fix)
        store.on_task_created({
            "task_id": "r1", "type": "pm", "prompt": "p",
            "parent_task_id": "", "project_id": "proj",
            "metadata": {"bug_id": BUG_ID},
        })
        store.on_task_created({
            "task_id": "r2", "type": "dev", "prompt": "p",
            "parent_task_id": "r1", "project_id": "proj",
        })
        store._recovering = False

        # After replay, bug_id is back
        self.assertEqual(store.get_bug_id("r1"), BUG_ID)
        self.assertEqual(store.get_bug_id("r2"), BUG_ID)
        chain = store.get_chain("r1")
        self.assertEqual(chain.get("bug_id"), BUG_ID)


class TestIdempotency(unittest.TestCase):
    """bug_id extraction does not double-fire on idempotent re-creates."""

    def test_duplicate_task_created_does_not_reset_bug_id(self):
        store = ChainContextStore()
        store.on_task_created({
            "task_id": "t1", "type": "pm", "prompt": "p",
            "parent_task_id": "", "project_id": "proj",
            "metadata": {"bug_id": BUG_ID},
        })
        # Re-fire the same event — should be a no-op (idempotent per AC1 of original design)
        store.on_task_created({
            "task_id": "t1", "type": "pm", "prompt": "p",
            "parent_task_id": "", "project_id": "proj",
            "metadata": {"bug_id": "SOMETHING-ELSE"},
        })
        self.assertEqual(store.get_bug_id("t1"), BUG_ID)


if __name__ == "__main__":
    unittest.main()
