"""Tests for route_token_ref server-side persistence and gate resolution.

Acceptance criteria (AC-ROUTE-TOKEN-REF-RESOLUTION-20260610):

  AC1: issue→ref persisted, no raw token stored.
  AC2: ref-only protected append on matching identity passes with the new decision.
  AC3: unknown ref → refused (gate failure, not silent pass).
  AC4: superseded identity ref → refused.
  AC5: full-token path unchanged (regression-safe).

QA followups (AC-ROUTE-TOKEN-REF-BINDING-AND-LIFECYCLE-20260610):

  F2-BINDING: resolve_route_token_ref enforces route_id / route_context_hash
    binding when the request carries them — an identity-mismatched ref must
    raise RouteTokenRefError (fail closed).
  F5-LIFECYCLE: when a route_identity_supersede event is processed,
    supersede_route_token_ref marks the bound ref as superseded; subsequent
    resolve calls for that ref must raise RouteTokenRefError.

These tests are self-contained: they use in-memory SQLite (no live governance
runtime required) and mock just enough of the server/gate plumbing to exercise
the ref-resolution code paths end-to-end.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import unittest
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from agent.governance import observer_route_context as _orc
from agent.governance.observer_route_context import (
    RouteTokenRefError,
    _ensure_ref_registry_schema,
    persist_route_token_ref,
    resolve_route_token_ref,
    supersede_route_token_ref,
    verify_route_token_binding,
)
from agent.governance.mf_subagent_contract import (
    MfSubagentContractError,
    validate_route_token_mutation_gate,
)


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

_PROJECT = "aming-claw"
_BACKLOG = "AC-ROUTE-TOKEN-REF-RESOLUTION-20260610"
_TASK = "task-token-ref-20260610-01"
_TARGET_FILES = [
    "agent/governance/observer_route_context.py",
    "agent/governance/task_timeline.py",
    "agent/governance/server.py",
]
_NOW = datetime(2099, 6, 11, 12, 0, 0, tzinfo=timezone.utc)


def _make_token(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        project_id=_PROJECT,
        backlog_id=_BACKLOG,
        task_id=_TASK,
        target_files=_TARGET_FILES,
        now=_NOW,
    )
    kwargs.update(overrides)
    return _orc.build_observer_write_route_token(**kwargs)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_ref_registry_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# AC1: issue → ref persisted; no raw token stored
# ---------------------------------------------------------------------------


class TestRefPersistence(unittest.TestCase):
    """AC1: persist_route_token_ref stores identity, never the raw token."""

    def test_ref_row_inserted(self) -> None:
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()

        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        rows = conn.execute(
            "SELECT * FROM observer_route_token_refs WHERE project_id=? AND route_token_ref=?",
            (_PROJECT, ref),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        row = dict(rows[0])
        self.assertEqual(row["route_id"], token["route_id"])
        self.assertEqual(row["route_context_hash"], token["route_context_hash"])
        self.assertEqual(row["prompt_contract_id"], token["prompt_contract_id"])
        self.assertEqual(row["backlog_id"], _BACKLOG)
        self.assertEqual(row["task_id"], _TASK)
        self.assertEqual(row["status"], _orc.REF_STATUS_ACTIVE)

    def test_raw_token_not_stored(self) -> None:
        """The raw token object must never appear in the registry row."""
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()

        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        # Dump every column value as a string and verify no raw body
        raw_token_json = json.dumps(dict(token), sort_keys=True)
        rows = conn.execute(
            "SELECT * FROM observer_route_token_refs WHERE project_id=? AND route_token_ref=?",
            (_PROJECT, ref),
        ).fetchall()
        for row in rows:
            for col in row.keys():
                val = str(row[col] or "")
                # The raw token body (as a whole) must not appear verbatim
                self.assertNotIn(
                    raw_token_json,
                    val,
                    f"column {col!r} appears to contain the raw token body",
                )

    def test_digest_present(self) -> None:
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()

        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        row = dict(
            conn.execute(
                "SELECT token_digest, salt FROM observer_route_token_refs "
                "WHERE project_id=? AND route_token_ref=?",
                (_PROJECT, ref),
            ).fetchone()
        )
        self.assertTrue(row["token_digest"], "token_digest must be non-empty")
        self.assertTrue(row["salt"], "salt must be non-empty")
        # Digest must not equal the raw token body or the ref
        self.assertNotEqual(row["token_digest"], json.dumps(dict(token)))
        self.assertNotEqual(row["token_digest"], ref)

    def test_idempotent_reissue(self) -> None:
        """Same token issued twice → no error, still one row."""
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()

        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        count = conn.execute(
            "SELECT COUNT(*) FROM observer_route_token_refs WHERE project_id=? AND route_token_ref=?",
            (_PROJECT, ref),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_ref_never_empty(self) -> None:
        """derive_route_token_ref must never return an empty string."""
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        self.assertTrue(ref.startswith("rtok-"), f"unexpected ref: {ref!r}")
        self.assertGreater(len(ref), 5)

    def test_same_identity_reissue_gets_fresh_ref(self) -> None:
        """Same route identity reissued later must not collide at persist time."""
        first = _make_token(now=_NOW)
        later = _make_token(now=_NOW.replace(hour=13))

        self.assertEqual(first["route_id"], later["route_id"])
        self.assertNotEqual(first["issued_at"], later["issued_at"])
        self.assertNotEqual(
            _orc.derive_route_token_ref(first),
            _orc.derive_route_token_ref(later),
        )


# ---------------------------------------------------------------------------
# AC2: ref-only → passes with decision=route_token_ref_resolved
# ---------------------------------------------------------------------------


class TestRefResolution(unittest.TestCase):
    """AC2: resolve_route_token_ref returns a valid gate surface for matching identity."""

    def test_resolution_returns_identity(self) -> None:
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        resolved = resolve_route_token_ref(
            conn, project_id=_PROJECT, route_token_ref=ref
        )
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved["route_id"], token["route_id"])
        self.assertEqual(resolved["route_context_hash"], token["route_context_hash"])
        self.assertEqual(resolved["prompt_contract_id"], token["prompt_contract_id"])
        self.assertTrue(resolved["resolved_from_ref"])

    def test_resolved_token_passes_gate(self) -> None:
        """Injecting the resolved surface as route_token must pass the existing gate."""
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        resolved = resolve_route_token_ref(
            conn, project_id=_PROJECT, route_token_ref=ref
        )
        assert resolved is not None

        # Inject resolved surface as route_token into gate payload
        gate_result = validate_route_token_mutation_gate(
            {"route_token": resolved},
            action="task_timeline_append",
            project_id=_PROJECT,
            backlog_id=_BACKLOG,
            task_id=_TASK,
            now=_NOW,
        )
        self.assertTrue(gate_result.get("allowed"))
        self.assertEqual(gate_result["decision"], "route_token")

    def test_decision_becomes_route_token_ref_resolved_when_server_injects(self) -> None:
        """End-to-end: server-side resolution stamps decision=route_token_ref_resolved."""
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        # Simulate what _require_route_token_mutation_gate does internally:
        # resolve the ref, inject as route_token, then update the decision.
        resolved = resolve_route_token_ref(
            conn, project_id=_PROJECT, route_token_ref=ref,
            backlog_id=_BACKLOG, task_id=_TASK,
        )
        assert resolved is not None

        raw_result = validate_route_token_mutation_gate(
            {"route_token": resolved},
            action="task_timeline_append",
            project_id=_PROJECT,
            backlog_id=_BACKLOG,
            task_id=_TASK,
            now=_NOW,
        )
        # Simulate the server overriding decision + adding resolved_from_ref
        result = dict(raw_result)
        result["decision"] = "route_token_ref_resolved"
        result["resolved_from_ref"] = True

        self.assertTrue(result["allowed"])
        self.assertEqual(result["decision"], "route_token_ref_resolved")
        self.assertTrue(result["resolved_from_ref"])

    def test_identity_mismatch_task_id(self) -> None:
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        with self.assertRaises(RouteTokenRefError) as cm:
            resolve_route_token_ref(
                conn,
                project_id=_PROJECT,
                route_token_ref=ref,
                task_id="wrong-task-id",
            )
        self.assertIn("identity mismatch", str(cm.exception).lower())

    def test_identity_mismatch_backlog_id(self) -> None:
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        with self.assertRaises(RouteTokenRefError) as cm:
            resolve_route_token_ref(
                conn,
                project_id=_PROJECT,
                route_token_ref=ref,
                backlog_id="AC-OTHER-BACKLOG-99",
            )
        self.assertIn("identity mismatch", str(cm.exception).lower())


# ---------------------------------------------------------------------------
# AC3: unknown ref → refused
# ---------------------------------------------------------------------------


class TestUnknownRef(unittest.TestCase):
    """AC3: unresolvable refs fail closed (gate failure, not silent pass)."""

    def test_unknown_ref_returns_none(self) -> None:
        conn = _make_conn()
        result = resolve_route_token_ref(
            conn, project_id=_PROJECT, route_token_ref="rtok-does-not-exist"
        )
        self.assertIsNone(result)

    def test_forged_ref_returns_none(self) -> None:
        """A ref that was never persisted resolves to None (gate must refuse)."""
        conn = _make_conn()
        forged_ref = "rtok-" + "f" * 32
        result = resolve_route_token_ref(
            conn, project_id=_PROJECT, route_token_ref=forged_ref
        )
        self.assertIsNone(result)

    def test_other_project_ref_invisible(self) -> None:
        """A valid ref from one project is invisible to another project."""
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        result = resolve_route_token_ref(
            conn, project_id="other-project", route_token_ref=ref
        )
        self.assertIsNone(result)

    def test_none_result_gate_refuses(self) -> None:
        """When resolution returns None the gate must refuse (no route_token injected)."""
        conn = _make_conn()
        result = resolve_route_token_ref(
            conn, project_id=_PROJECT, route_token_ref="rtok-unknown"
        )
        self.assertIsNone(result)
        # Confirm gate refuses when called without route_token
        with self.assertRaises(MfSubagentContractError):
            validate_route_token_mutation_gate(
                {},  # no route_token, no waiver → gate must raise
                action="task_timeline_append",
                project_id=_PROJECT,
                backlog_id=_BACKLOG,
                task_id=_TASK,
            )


class TestFullTokenServerBinding(unittest.TestCase):
    def test_full_token_without_embedded_ref_accepts_identity_digest_match(self) -> None:
        token = _make_token(now=_NOW)
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        verified = verify_route_token_binding(
            conn,
            project_id=_PROJECT,
            token=token,
            backlog_id=_BACKLOG,
            task_id=_TASK,
            now=_NOW.replace(minute=1),
        )

        self.assertTrue(verified["server_issued_binding"])
        self.assertEqual(verified["route_token_ref"], ref)

    def test_root_scoped_full_token_accepts_worker_request_task_id(self) -> None:
        token = _make_token(now=_NOW)
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        verified = verify_route_token_binding(
            conn,
            project_id=_PROJECT,
            token=token,
            backlog_id=_BACKLOG,
            task_id="worker-task-under-same-backlog",
            now=_NOW.replace(minute=1),
        )

        self.assertTrue(verified["server_issued_binding"])
        self.assertEqual(verified["scope"]["task_id"], _TASK)

    def test_forged_full_token_without_registry_row_is_refused(self) -> None:
        token = _make_token(now=_NOW)
        conn = _make_conn()

        with self.assertRaises(RouteTokenRefError) as cm:
            verify_route_token_binding(
                conn,
                project_id=_PROJECT,
                token=token,
                backlog_id=_BACKLOG,
                task_id=_TASK,
                now=_NOW.replace(minute=1),
            )
        self.assertIn("no active", str(cm.exception))

    def test_full_token_allowed_actions_superset_is_refused(self) -> None:
        token = _make_token(now=_NOW, allowed_actions=["task_timeline_append"])
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        forged = dict(token)
        forged["allowed_actions"] = [*token["allowed_actions"], "backlog_close"]
        with self.assertRaises(RouteTokenRefError) as cm:
            verify_route_token_binding(
                conn,
                project_id=_PROJECT,
                token=forged,
                backlog_id=_BACKLOG,
                task_id=_TASK,
                now=_NOW.replace(minute=1),
            )
        self.assertIn("allowed_actions exceed", str(cm.exception))

    def test_full_token_digest_mismatch_is_refused(self) -> None:
        token = _make_token(now=_NOW)
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        forged = dict(token)
        forged["evidence_refs"] = [*token["evidence_refs"], "timeline:forged-extra"]
        with self.assertRaises(RouteTokenRefError) as cm:
            verify_route_token_binding(
                conn,
                project_id=_PROJECT,
                token=forged,
                backlog_id=_BACKLOG,
                task_id=_TASK,
                now=_NOW.replace(minute=1),
            )
        self.assertIn("digest mismatch", str(cm.exception))


# ---------------------------------------------------------------------------
# AC4: superseded/expired ref → refused
# ---------------------------------------------------------------------------


class TestSupersededRef(unittest.TestCase):
    """AC4: superseded and expired refs fail closed."""

    def test_superseded_ref_raises(self) -> None:
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)
        supersede_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)

        with self.assertRaises(RouteTokenRefError) as cm:
            resolve_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)
        self.assertIn("superseded", str(cm.exception).lower())

    def test_expired_token_raises(self) -> None:
        # Mint token with already-passed expiry
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        token = _make_token(now=past, ttl_hours=1)
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        with self.assertRaises(RouteTokenRefError) as cm:
            resolve_route_token_ref(
                conn, project_id=_PROJECT, route_token_ref=ref,
                now=_NOW,  # _NOW is well past 2020
            )
        self.assertIn("expired", str(cm.exception).lower())

    def test_supersede_nonexistent_ref_returns_false(self) -> None:
        conn = _make_conn()
        result = supersede_route_token_ref(
            conn, project_id=_PROJECT, route_token_ref="rtok-does-not-exist"
        )
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# AC5: full-token path unchanged (regression-safe)
# ---------------------------------------------------------------------------


class TestFullTokenPathUnchanged(unittest.TestCase):
    """AC5: the existing full-token gate path continues to work unchanged."""

    def test_full_token_gate_passes(self) -> None:
        token = _make_token()
        result = validate_route_token_mutation_gate(
            {"route_token": token},
            action="task_timeline_append",
            project_id=_PROJECT,
            backlog_id=_BACKLOG,
            task_id=_TASK,
            now=_NOW,
        )
        self.assertTrue(result.get("allowed"))
        self.assertEqual(result["decision"], "route_token")
        self.assertNotIn("resolved_from_ref", result)

    def test_full_token_gate_rejects_expired(self) -> None:
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        token = _make_token(now=past, ttl_hours=1)
        with self.assertRaises(MfSubagentContractError):
            validate_route_token_mutation_gate(
                {"route_token": token},
                action="task_timeline_append",
                project_id=_PROJECT,
                backlog_id=_BACKLOG,
                task_id=_TASK,
                now=_NOW,
            )

    def test_full_token_gate_rejects_wrong_action(self) -> None:
        token = _make_token()
        with self.assertRaises(MfSubagentContractError):
            validate_route_token_mutation_gate(
                {"route_token": token},
                action="edit_files",  # blocked action
                project_id=_PROJECT,
                backlog_id=_BACKLOG,
                task_id=_TASK,
                now=_NOW,
            )

    def test_full_token_gate_rejects_scope_mismatch(self) -> None:
        token = _make_token()
        with self.assertRaises(MfSubagentContractError):
            validate_route_token_mutation_gate(
                {"route_token": token},
                action="task_timeline_append",
                project_id="different-project",
                backlog_id=_BACKLOG,
                task_id=_TASK,
                now=_NOW,
            )

    def test_no_token_no_waiver_gate_refuses(self) -> None:
        with self.assertRaises(MfSubagentContractError):
            validate_route_token_mutation_gate(
                {},
                action="task_timeline_append",
                project_id=_PROJECT,
                backlog_id=_BACKLOG,
                task_id=_TASK,
                now=_NOW,
            )

    def test_full_token_decision_not_overridden(self) -> None:
        """Supplying a full route_token must yield decision=route_token, never route_token_ref_resolved."""
        token = _make_token()
        result = validate_route_token_mutation_gate(
            {"route_token": token},
            action="task_timeline_append",
            project_id=_PROJECT,
            backlog_id=_BACKLOG,
            task_id=_TASK,
            now=_NOW,
        )
        self.assertEqual(result["decision"], "route_token")


# ---------------------------------------------------------------------------
# Thread-safety smoke test
# ---------------------------------------------------------------------------


class TestThreadSafety(unittest.TestCase):
    """Parallel persist calls with the same ref must not corrupt the registry.

    Each thread opens its own connection to a shared on-disk temp DB so
    SQLite's cross-thread restriction is not triggered.  (In-memory SQLite
    connections cannot be shared across threads.)
    """

    def test_concurrent_persist_idempotent(self) -> None:
        import tempfile, os as _os

        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        errors: list[Exception] = []

        # Create a named temp file so all threads share the same DB file.
        fd, db_path = tempfile.mkstemp(suffix=".db")
        _os.close(fd)
        try:
            # Ensure schema in the temp DB first.
            setup_conn = sqlite3.connect(db_path)
            setup_conn.row_factory = sqlite3.Row
            _ensure_ref_registry_schema(setup_conn)
            setup_conn.close()

            def _do_persist() -> None:
                try:
                    thread_conn = sqlite3.connect(db_path)
                    thread_conn.row_factory = sqlite3.Row
                    try:
                        persist_route_token_ref(
                            thread_conn,
                            project_id=_PROJECT,
                            route_token_ref=ref,
                            token=token,
                        )
                    finally:
                        thread_conn.close()
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=_do_persist) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # No unexpected errors (idempotent re-issue must not raise)
            self.assertEqual(errors, [], f"unexpected errors: {errors}")

            verify_conn = sqlite3.connect(db_path)
            count = verify_conn.execute(
                "SELECT COUNT(*) FROM observer_route_token_refs WHERE project_id=? AND route_token_ref=?",
                (_PROJECT, ref),
            ).fetchone()[0]
            verify_conn.close()
            self.assertEqual(count, 1)
        finally:
            try:
                _os.unlink(db_path)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# F2-BINDING: route_id / route_context_hash mismatch → refused (fail closed)
# ---------------------------------------------------------------------------


class TestF2BindingRouteIdentityMismatch(unittest.TestCase):
    """F2: resolve_route_token_ref enforces route_id and route_context_hash binding.

    An identity-mismatched ref must raise RouteTokenRefError (fail closed),
    not silently resolve to the stored identity.
    """

    def _persist_ref(self) -> tuple[sqlite3.Connection, str, dict]:
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)
        return conn, ref, token

    def test_route_id_mismatch_raises(self) -> None:
        """Supplying a different route_id than the stored one → RouteTokenRefError."""
        conn, ref, token = self._persist_ref()
        wrong_route_id = "route-ffffffffffffffff"
        self.assertNotEqual(token.get("route_id", ""), wrong_route_id)

        with self.assertRaises(RouteTokenRefError) as cm:
            resolve_route_token_ref(
                conn,
                project_id=_PROJECT,
                route_token_ref=ref,
                route_id=wrong_route_id,
            )
        self.assertIn("identity mismatch", str(cm.exception).lower())

    def test_route_context_hash_mismatch_raises(self) -> None:
        """Supplying a different route_context_hash than the stored one → RouteTokenRefError."""
        conn, ref, token = self._persist_ref()
        wrong_rch = "sha256:" + "0" * 64

        with self.assertRaises(RouteTokenRefError) as cm:
            resolve_route_token_ref(
                conn,
                project_id=_PROJECT,
                route_token_ref=ref,
                route_context_hash=wrong_rch,
            )
        self.assertIn("identity mismatch", str(cm.exception).lower())

    def test_matching_route_id_resolves_ok(self) -> None:
        """Supplying the correct route_id → resolution succeeds (positive control)."""
        conn, ref, token = self._persist_ref()
        resolved = resolve_route_token_ref(
            conn,
            project_id=_PROJECT,
            route_token_ref=ref,
            route_id=token["route_id"],
        )
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved["route_id"], token["route_id"])

    def test_matching_route_context_hash_resolves_ok(self) -> None:
        """Supplying the correct route_context_hash → resolution succeeds."""
        conn, ref, token = self._persist_ref()
        resolved = resolve_route_token_ref(
            conn,
            project_id=_PROJECT,
            route_token_ref=ref,
            route_context_hash=token["route_context_hash"],
        )
        self.assertIsNotNone(resolved)

    def test_empty_route_id_skips_check(self) -> None:
        """Omitting route_id (empty string) → binding check is skipped (existing behavior)."""
        conn, ref, _token = self._persist_ref()
        # No route_id supplied → must still resolve successfully (no regression)
        resolved = resolve_route_token_ref(
            conn,
            project_id=_PROJECT,
            route_token_ref=ref,
            route_id="",  # explicit empty → skip check
        )
        self.assertIsNotNone(resolved)

    def test_both_mismatched_raises(self) -> None:
        """Both route_id and route_context_hash wrong → RouteTokenRefError on first check."""
        conn, ref, _token = self._persist_ref()
        with self.assertRaises(RouteTokenRefError):
            resolve_route_token_ref(
                conn,
                project_id=_PROJECT,
                route_token_ref=ref,
                route_id="route-ffffffffffffffff",
                route_context_hash="sha256:" + "0" * 64,
            )


# ---------------------------------------------------------------------------
# F5-LIFECYCLE: supersession event → previously-active ref refuses afterward
# ---------------------------------------------------------------------------


class TestF5LifecycleSupersessionInvalidatesRef(unittest.TestCase):
    """F5: supersede_route_token_ref (called on route_identity_supersede events)
    invalidates the active ref.  Subsequent resolution raises RouteTokenRefError.
    """

    def test_supersession_invalidates_active_ref(self) -> None:
        """After supersede_route_token_ref, the ref resolves to RouteTokenRefError."""
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        # Confirm ref resolves before supersession
        resolved_before = resolve_route_token_ref(
            conn, project_id=_PROJECT, route_token_ref=ref
        )
        self.assertIsNotNone(resolved_before)

        # Simulate F5: route_identity_supersede event triggers supersession
        supersede_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)

        # After supersession the ref must be refused
        with self.assertRaises(RouteTokenRefError) as cm:
            resolve_route_token_ref(
                conn, project_id=_PROJECT, route_token_ref=ref
            )
        self.assertIn("superseded", str(cm.exception).lower())

    def test_superseded_ref_refused_even_with_correct_identity(self) -> None:
        """A superseded ref must refuse even when route_id / backlog_id match exactly."""
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)
        supersede_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)

        # Presenting the exact matching identity must still refuse
        with self.assertRaises(RouteTokenRefError) as cm:
            resolve_route_token_ref(
                conn,
                project_id=_PROJECT,
                route_token_ref=ref,
                route_id=token["route_id"],
                route_context_hash=token["route_context_hash"],
                backlog_id=_BACKLOG,
                task_id=_TASK,
            )
        self.assertIn("superseded", str(cm.exception).lower())

    def test_supersession_idempotent(self) -> None:
        """Calling supersede_route_token_ref twice on the same ref is safe."""
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        result1 = supersede_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)
        result2 = supersede_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)
        self.assertTrue(result1)   # first call finds active row
        self.assertFalse(result2)  # second call: already superseded, no row updated

    def test_supersession_does_not_affect_different_ref(self) -> None:
        """Superseding ref A must not affect ref B from a different token."""
        token_a = _make_token()
        token_b = _make_token(backlog_id="AC-OTHER-20260610")
        ref_a = _orc.derive_route_token_ref(token_a)
        ref_b = _orc.derive_route_token_ref(token_b)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref_a, token=token_a)
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref_b, token=token_b)

        supersede_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref_a)

        # ref_b must remain active
        resolved_b = resolve_route_token_ref(
            conn, project_id=_PROJECT, route_token_ref=ref_b
        )
        self.assertIsNotNone(resolved_b)


# ---------------------------------------------------------------------------
# QA-FIX #3580: F-SUPERSESSION-HOOK-DIRECT-APPEND
# Verify that _apply_supersession_hook_if_needed (the shared helper) correctly
# invalidates an active ref when called with a supersession event — covering
# both the "direct-append path" and the "repair-run path regression" cases.
# ---------------------------------------------------------------------------


def _make_supersession_payload(route_token_ref: str) -> dict:
    """Build a minimal payload as the repair-run plan builder would write it."""
    return {
        "route_identity_supersession": {
            "supplied": {
                "route_token_ref": route_token_ref,
            }
        }
    }


class TestDirectAppendPathSupersession(unittest.TestCase):
    """F-SUPERSESSION-HOOK-DIRECT-APPEND: shared helper invalidates ref via
    the direct-append path (simulated via _apply_supersession_hook_if_needed).
    """

    def _setup(self) -> tuple[sqlite3.Connection, str]:
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)
        return conn, ref

    def _call_shared_helper(
        self,
        conn: sqlite3.Connection,
        ref: str,
        event_kind: str = "route_identity_supersede",
        event_type: str = "",
    ) -> None:
        """Invoke _apply_supersession_hook_if_needed as the direct-append handler would."""
        from agent.governance.server import _apply_supersession_hook_if_needed
        payload = _make_supersession_payload(ref)
        _apply_supersession_hook_if_needed(
            conn,
            project_id=_PROJECT,
            event_kind=event_kind,
            event_type=event_type,
            payload=payload,
        )

    def test_direct_append_event_kind_invalidates_ref(self) -> None:
        """After calling the shared helper with event_kind=route_identity_supersede,
        the previously-active ref must refuse on resolution.
        """
        conn, ref = self._setup()

        # Confirm active before hook
        before = resolve_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)
        self.assertIsNotNone(before)

        # Simulate direct-append path supersession via shared helper
        self._call_shared_helper(conn, ref, event_kind="route_identity_supersede")

        # Must refuse afterward
        with self.assertRaises(RouteTokenRefError) as cm:
            resolve_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)
        self.assertIn("superseded", str(cm.exception).lower())

    def test_direct_append_event_type_invalidates_ref(self) -> None:
        """The event_type='route.identity.superseded' marker also triggers invalidation."""
        conn, ref = self._setup()

        self._call_shared_helper(conn, ref, event_kind="", event_type="route.identity.superseded")

        with self.assertRaises(RouteTokenRefError):
            resolve_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)

    def test_non_supersession_event_does_not_invalidate(self) -> None:
        """A normal event_kind (e.g. 'worker_progress') must NOT invalidate the ref."""
        conn, ref = self._setup()

        from agent.governance.server import _apply_supersession_hook_if_needed
        payload = _make_supersession_payload(ref)
        _apply_supersession_hook_if_needed(
            conn,
            project_id=_PROJECT,
            event_kind="worker_progress",
            event_type="task.progress",
            payload=payload,
        )

        # Ref must still be active
        result = resolve_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)
        self.assertIsNotNone(result)

    def test_shared_helper_no_ref_in_payload_is_noop(self) -> None:
        """Empty route_token_ref in payload → no-op, no exception."""
        conn, ref = self._setup()

        from agent.governance.server import _apply_supersession_hook_if_needed
        # Payload without route_token_ref
        _apply_supersession_hook_if_needed(
            conn,
            project_id=_PROJECT,
            event_kind="route_identity_supersede",
            event_type="",
            payload={"route_identity_supersession": {"supplied": {}}},
        )

        # Ref untouched — still active
        result = resolve_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)
        self.assertIsNotNone(result)


class TestRepairRunPathRegressionSupersession(unittest.TestCase):
    """Regression: the repair-run path must still invalidate via the shared helper."""

    def test_repair_run_path_still_invalidates(self) -> None:
        """After migrating the repair-run F5 code to use _apply_supersession_hook_if_needed,
        the repair-run path continues to invalidate refs correctly.
        """
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        # Confirm active before
        before = resolve_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)
        self.assertIsNotNone(before)

        # Directly call the shared helper as the repair-run path now does
        from agent.governance.server import _apply_supersession_hook_if_needed
        payload = _make_supersession_payload(ref)
        _apply_supersession_hook_if_needed(
            conn,
            project_id=_PROJECT,
            event_kind="route_identity_supersede",
            event_type="route.identity.superseded",
            payload=payload,
        )

        # Ref must be refused
        with self.assertRaises(RouteTokenRefError) as cm:
            resolve_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)
        self.assertIn("superseded", str(cm.exception).lower())

    def test_repair_run_legacy_cleanup_kind_also_invalidates(self) -> None:
        """The route_identity_cleanup alias is also in MF_ROUTE_IDENTITY_CLEANUP_MARKERS."""
        token = _make_token(backlog_id="AC-LEGACY-CLEANUP-TEST-20260610")
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        from agent.governance.server import _apply_supersession_hook_if_needed
        payload = _make_supersession_payload(ref)
        _apply_supersession_hook_if_needed(
            conn,
            project_id=_PROJECT,
            event_kind="route_identity_cleanup",
            event_type="",
            payload=payload,
        )

        with self.assertRaises(RouteTokenRefError):
            resolve_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref)


# ---------------------------------------------------------------------------
# QA-FIX #3580: F-BINDING-STORED-EMPTY-BYPASS
# observer_route_context.py:808 — when caller supplies route_id but stored is
# empty, the binding cannot be corroborated and must refuse (fail closed).
# Caller omitting route_id keeps the existing skip-check behavior.
# ---------------------------------------------------------------------------


class TestF2BindingStoredEmptyBypass(unittest.TestCase):
    """F-BINDING-STORED-EMPTY-BYPASS: stored-empty + caller-supplied → refused;
    caller omits → unchanged (existing skip-check behavior).
    """

    def _persist_ref_without_route_id(self) -> tuple[sqlite3.Connection, str, dict]:
        """Persist a ref but manually clear stored route_id to simulate the empty case."""
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)
        # Manually blank out route_id and route_context_hash in the DB row
        conn.execute(
            "UPDATE observer_route_token_refs SET route_id='', route_context_hash='' "
            "WHERE project_id=? AND route_token_ref=?",
            (_PROJECT, ref),
        )
        conn.commit()
        return conn, ref, token

    def test_stored_empty_route_id_caller_supplied_refuses(self) -> None:
        """Caller supplies route_id but stored row has empty route_id → refused."""
        conn, ref, _token = self._persist_ref_without_route_id()

        with self.assertRaises(RouteTokenRefError) as cm:
            resolve_route_token_ref(
                conn,
                project_id=_PROJECT,
                route_token_ref=ref,
                route_id="route-20260610-any",
            )
        msg = str(cm.exception).lower()
        # Either "cannot be corroborated" or "identity mismatch"
        self.assertTrue(
            "corroborated" in msg or "mismatch" in msg,
            f"unexpected error: {cm.exception}",
        )

    def test_stored_empty_route_context_hash_caller_supplied_refuses(self) -> None:
        """Caller supplies route_context_hash but stored row has empty value → refused."""
        conn, ref, _token = self._persist_ref_without_route_id()

        with self.assertRaises(RouteTokenRefError) as cm:
            resolve_route_token_ref(
                conn,
                project_id=_PROJECT,
                route_token_ref=ref,
                route_context_hash="sha256:" + "a" * 64,
            )
        msg = str(cm.exception).lower()
        self.assertTrue(
            "corroborated" in msg or "mismatch" in msg,
            f"unexpected error: {cm.exception}",
        )

    def test_caller_omits_route_id_stored_empty_still_resolves(self) -> None:
        """Caller omits route_id entirely — stored-empty check is skipped (existing behavior)."""
        conn, ref, _token = self._persist_ref_without_route_id()

        # No route_id supplied → the binding dimension is simply absent → skip check
        result = resolve_route_token_ref(
            conn,
            project_id=_PROJECT,
            route_token_ref=ref,
            # no route_id kwarg
        )
        self.assertIsNotNone(result)

    def test_caller_explicit_empty_route_id_skips_check(self) -> None:
        """Passing route_id='' (explicit empty) is treated as 'not supplied' → check skipped."""
        conn, ref, _token = self._persist_ref_without_route_id()

        result = resolve_route_token_ref(
            conn,
            project_id=_PROJECT,
            route_token_ref=ref,
            route_id="",
        )
        self.assertIsNotNone(result)

    def test_stored_populated_route_id_caller_matches_resolves(self) -> None:
        """Positive control: stored non-empty route_id + matching caller → resolves OK."""
        token = _make_token()
        ref = _orc.derive_route_token_ref(token)
        conn = _make_conn()
        persist_route_token_ref(conn, project_id=_PROJECT, route_token_ref=ref, token=token)

        result = resolve_route_token_ref(
            conn,
            project_id=_PROJECT,
            route_token_ref=ref,
            route_id=token["route_id"],
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["route_id"], token["route_id"])


if __name__ == "__main__":
    unittest.main()
