"""Explicit registry delegation to certified DEV native maintenance, disabled by default.

No import-time work, token custody, automatic recovery or timer catch-up. The
service owns the thread and joins it before surrendering its native lease.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import project_service, snapshot_cow_cleanup as cow, stale_artifact_cleanup as cleanup
from .errors import ValidationError

GiB = 1024**3
FIELDS = {'enabled', 'interval_seconds', 'snapshot_ids', 'exclude_snapshot_ids',
          'max_snapshots_per_run', 'max_pairs_per_run', 'max_hash_bytes_per_run', 'max_run_seconds'}
AUDIT_FIELDS = {'revision', 'operator_principal_hash', 'custody', 'updated_at'}
ENVIRONMENTAL = {'cow_external_archive_not_configured', 'cow_archive_unavailable',
                 'cow_archive_mount_unverified', 'cow_archive_volume_mismatch',
                 'cow_archive_capacity_insufficient', 'cow_volume_unreadable'}
PAGE_SIZE = 20
PRE_ADMISSION_UNAVAILABLE = frozenset({
    'AC dev first-start source binding is invalid',
    'AC dev first-start source identity is invalid',
})


def parse_policy(value: dict[str, Any], *, stored: bool = True) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - FIELDS - (AUDIT_FIELDS if stored else set()):
        raise ValidationError('periodic_policy_fields_invalid')
    enabled = value.get('enabled', False)
    if type(enabled) is not bool:
        raise ValidationError('periodic_enabled_invalid')
    result = {'enabled': enabled}
    for key, default, lower, upper in (
        ('interval_seconds', 3600, 60, 86400), ('max_snapshots_per_run', 1, 1, 20),
        ('max_pairs_per_run', 2, 2, 40), ('max_hash_bytes_per_run', 64 * GiB, 1, 128 * GiB),
        ('max_run_seconds', 60, 1, 600)):
        item = value.get(key, default)
        if type(item) is not int or not lower <= item <= upper:
            raise ValidationError('periodic_budget_invalid:' + key)
        result[key] = item
    if result['max_pairs_per_run'] < result['max_snapshots_per_run'] * 2:
        raise ValidationError('periodic_complete_window_required')
    for key in ('snapshot_ids', 'exclude_snapshot_ids'):
        if key not in value:
            continue
        ids = value[key]
        if (not isinstance(ids, list) or len(ids) > 1000
                or any(not isinstance(sid, str) or not cow.snapshots._snapshot_id_is_component(sid) for sid in ids)
                or len(set(ids)) != len(ids)):
            raise ValidationError('periodic_selection_invalid')
        result[key] = list(ids)
    if stored:
        revision = value.get('revision', 0)
        if type(revision) is not int or revision < 0:
            raise ValidationError('periodic_revision_invalid')
        result['revision'] = revision
        if enabled and (revision == 0 or not isinstance(value.get('custody'), dict)
                        or not re.fullmatch(r'[0-9a-f]{64}', str(value.get('operator_principal_hash', '')))):
            raise ValidationError('periodic_delegation_invalid')
        for key in AUDIT_FIELDS - {'revision'}:
            if key in value:
                result[key] = value[key]
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def service_custody(conn, project_id: str, root: Path) -> dict[str, Any]:
    from . import server
    if server._runtime_plane() != 'dev' or project_id != 'aming-claw':
        raise ValidationError('periodic_dev_owner_required')
    # Existing live lease/certificate/process proof, never a caller supplied certificate.
    server._current_terminalization_manager_identity(project_id)
    from .db import canonical_ac_database_identity
    canonical_ac_database_identity(conn)
    return cow._custody(conn, project_id, root)


class PeriodicController:
    def __init__(self, project_id: str, root: Path, connection_factory, *,
                 custody_check=service_custody, monotonic=time.monotonic):
        self.project_id, self.root = project_id, Path(root)
        self.connection_factory, self.custody_check = connection_factory, custody_check
        self.monotonic = monotonic
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._admission = threading.Lock()
        self._state_lock = threading.RLock()
        self._thread = None
        self._inflight = False
        self._next_due = None
        self._policy_revision = None
        self._supported = False
        self._custody = None
        self._state = {'revision': 0, 'outcome': 'idle', 'cursor': None, 'sweep_complete': False}
        self._fatal_hold = False
        self._source_unavailable = False

    @property
    def directory(self) -> Path:
        return cow._state_root(self.project_id).with_name('snapshot-cow-periodic')

    @property
    def state_path(self) -> Path:
        return self.directory / 'scheduler.json'

    def _verify(self, conn):
        custody = self.custody_check(conn, self.project_id, self.root)
        if self._custody is not None and custody != self._custody:
            raise ValidationError('periodic_custody_drift')
        self._custody = custody
        return custody

    def _load(self):
        cow._path(self.directory, exists=False)
        if self.state_path.with_name('scheduler.json.pending').exists():
            raise ValidationError('periodic_scheduler_pending')
        if not self.state_path.exists():
            return {'schema': 'snapshot_cow_periodic_scheduler.v1', 'custody': self._custody,
                    'revision': 0, 'outcome': 'idle', 'cursor': None, 'sweep_complete': False}
        state = cow._read(self.state_path)
        cursor = state.get('cursor')
        if (state.get('schema') != 'snapshot_cow_periodic_scheduler.v1'
                or state.get('custody') != self._custody or type(state.get('revision')) is not int
                or state['revision'] < 0 or state.get('outcome') not in
                {'idle', 'running', 'complete', 'noop', 'deferred', 'inspect_required', 'released'}
                or (cursor is not None and (not isinstance(cursor, list) or len(cursor) != 2
                    or any(not isinstance(v, str) or len(v) > 256 for v in cursor)
                    or not cow.snapshots._snapshot_id_is_component(cursor[1])))):
            raise ValidationError('periodic_scheduler_invalid')
        if state.get('operation_id') is not None and not re.fullmatch(r'cowop-[0-9a-f]{32}', str(state['operation_id'])):
            raise ValidationError('periodic_scheduler_invalid')
        for key in ('last_started_at', 'last_finished_at'):
            if state.get(key) is not None:
                try:
                    parsed = datetime.fromisoformat(state[key])
                    if parsed.tzinfo is None:
                        raise ValueError('timezone required')
                except (ValueError, TypeError):
                    raise ValidationError('periodic_scheduler_invalid')
        for key, maximum in [('candidate_count', 40), ('refusal_count', 1000), ('digest_bytes', 128 * GiB)]:
            if key in state and (type(state[key]) is not int or not 0 <= state[key] <= maximum):
                raise ValidationError('periodic_scheduler_invalid')
        if state.get('effect_classification', 'unknown') not in {
                'unknown', 'zero_effect_deferred', 'complete', 'partial_or_ambiguous'}:
            raise ValidationError('periodic_scheduler_invalid')
        if state.get('observed_net_delta') is not None and type(state['observed_net_delta']) is not int:
            raise ValidationError('periodic_scheduler_invalid')
        return state

    def _persist(self, state):
        state = {**state, 'schema': 'snapshot_cow_periodic_scheduler.v1', 'custody': self._custody,
                 'revision': self._state['revision'] + 1}
        cow._write(self.state_path, state)
        with self._state_lock:
            self._state = state
        return state

    def _ensure_directory(self):
        cow._path(self.directory, exists=False)
        if not self.directory.exists():
            self.directory.mkdir(mode=0o700)
            cow._fsync(self.directory.parent, directory=True)

    @contextmanager
    def _maintenance_lock(self):
        self._ensure_directory()
        path = cow._path(self.directory / 'maintenance.lock', exists=False)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValidationError('periodic_maintenance_busy')
            yield
        finally:
            os.close(fd)

    def _acks(self):
        directory = self.directory / 'releases'
        if not directory.exists():
            return {}
        cow._path(directory)
        result = {}
        with os.scandir(directory) as inventory:
            for index, item in enumerate(inventory):
                if (index >= 1000 or item.is_symlink() or not item.is_file(follow_symlinks=False)
                        or not re.fullmatch(r'cowop-[0-9a-f]{32}\.json', item.name)):
                    raise ValidationError('periodic_release_inventory_invalid')
                ack = cow._read(Path(item.path))
                held = ack.get('held_intent', {})
                proof = ack.get('proof', {})
                if (ack.get('ack_hash') != cow._digest({k: v for k, v in ack.items() if k != 'ack_hash'})
                        or ack.get('held_intent_hash') != cow._digest(held)
                        or held.get('custody') != self._custody
                        or held.get('operation_id') != item.name[:-5]
                        or proof.get('plan_hash') != held.get('plan_hash')
                        or proof.get('config_hash') != held.get('engine_config_hash')
                        or proof.get('run_selection') != held.get('run_selection')
                        or proof.get('candidate_ids') != held.get('candidate_ids')
                        or proof.get('snapshot_ids') != sorted(held.get('snapshot_ids', []))
                        or ack.get('scheduler_revision') != held.get('revision')
                        or ack.get('policy_revision') != held.get('policy_revision')):
                    raise ValidationError('periodic_release_ack_invalid')
                result[item.name[:-5]] = ack
        return result

    def _readiness(self, conn):
        return cleanup._periodic_native_check(conn, self.project_id, self.root, 'readiness',
                                               acknowledgements=self._acks())

    def start(self):
        conn = self.connection_factory()
        try:
            self._verify(conn)
            self._supported = True
            self._state = self._load()
        except Exception:
            self._fatal_hold = True
            if not self._supported:
                raise
        finally:
            conn.close()
        self._refresh_schedule()
        self._thread = threading.Thread(target=self._loop, name='snapshot-cow-periodic', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()  # Native irreversible replacement is never interrupted.
        # Also join an explicit owned due admission invoked by an integration caller.
        with self._admission:
            pass
        self._next_due = None

    def configuration_changed(self):
        self._wake.set()

    def _policy(self):
        return parse_policy(project_service.get_snapshot_cow_periodic_policy(self.project_id))

    def _refresh_schedule(self):
        try:
            policy = self._policy()
            revision = policy['revision']
            if revision != self._policy_revision:
                self._policy_revision = revision
                self._next_due = self.monotonic() + policy['interval_seconds'] if policy['enabled'] else None
            if not policy['enabled']:
                self._next_due = None
            return policy
        except Exception:
            self._next_due = None
            return None

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            self._refresh_schedule()
            now = self.monotonic()
            if self._next_due is not None and now >= self._next_due:
                self.tick()
            wait = 60 if self._next_due is None else max(0, self._next_due - self.monotonic())
            self._wake.wait(min(wait, 60))

    def _page(self, conn, policy, skipped):
        cursor = self._state.get('cursor')
        where, args = '', []
        if cursor is not None:
            where = ' AND (created_at>? OR (created_at=? AND snapshot_id>?))'
            args = [cursor[0], cursor[0], cursor[1]]
        rows = conn.execute("SELECT created_at,snapshot_id FROM graph_snapshots WHERE project_id=? "
            "AND snapshot_kind='full' AND status='superseded'" + where +
            ' ORDER BY created_at,snapshot_id LIMIT ?', (self.project_id, *args, PAGE_SIZE + 1)).fetchall()
        end = len(rows) <= PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        allow = set(policy['snapshot_ids']) if 'snapshot_ids' in policy else None
        exclude = set(policy.get('exclude_snapshot_ids', [])) | set(skipped)
        return rows, end, allow, exclude

    def _finish(self, outcome, cursor, sweep_complete, **fields):
        self._persist({**self._state, **fields, 'outcome': outcome, 'cursor': cursor,
                       'sweep_complete': sweep_complete, 'last_finished_at': _now()})

    def _zero_effect(self, conn, plan):
        return cleanup._periodic_native_check(conn, self.project_id, self.root, 'zero_effect',
            plan=plan, acknowledgements=self._acks()).get('zero_effect_verified') is True

    def tick(self):
        """One due admission, never catch-up. Native fixtures can advance the clock."""
        if not self._admission.acquire(blocking=False):
            return False
        conn = None
        meter = None
        maintenance_entered = False
        due_attempted = False
        policy = self._refresh_schedule()
        try:
            if (self._stop.is_set() or not policy or not policy['enabled'] or self._fatal_hold
                    or self._next_due is None or self.monotonic() < self._next_due):
                return False
            due_attempted = True
            conn = self.connection_factory()
            custody = self._verify(conn)
            self._source_unavailable = False
            if policy.get('custody') != custody:
                raise ValidationError('periodic_policy_custody_mismatch')
            with self._maintenance_lock():
                maintenance_entered = True
                self._state = self._load()
                if self._state['outcome'] in {'running', 'inspect_required'}:
                    self._fatal_hold = True
                    return False
                readiness = self._readiness(conn)
                self._inflight = True
                meter = cow.DigestMeter(policy['max_hash_bytes_per_run'],
                                        deadline=time.monotonic() + policy['max_run_seconds'])
                base_budgets = cow._budgets(cow._config(self.project_id))
                snapshot_limit = min(policy['max_snapshots_per_run'], base_budgets['max_snapshots'])
                pair_limit = min(policy['max_pairs_per_run'], base_budgets['max_pairs'])
                byte_limit = min(base_budgets['max_hash_bytes'], meter.limit // 32)
                rows, end, allow, exclude = self._page(conn, policy, readiness['skipped_snapshot_ids'])
                cursor = self._state.get('cursor')
                candidates, refusals, selected, inspected = [], 0, [], 0
                self._state['last_started_at'] = _now()
                with cow.digest_budget(meter):
                    for created, sid in rows:
                        if self._stop.is_set() or time.monotonic() >= meter.deadline:
                            break
                        if (not isinstance(created, str) or len(created) > 256
                                or not isinstance(sid, str) or not cow.snapshots._snapshot_id_is_component(sid)):
                            raise ValidationError('periodic_inventory_key_invalid')
                        cursor = [created, sid]
                        inspected += 1
                        if sid in exclude or (allow is not None and sid not in allow):
                            refusals += 1
                            continue
                        if pair_limit < 2 or byte_limit < 1:
                            refusals += 1
                            continue
                        # Reserve the complete window before any digest read.
                        try:
                            candidate_size = sum(cow._metadata(cow.snapshots._snapshot_root(
                                self.project_id, sid) / relative)['size'] for pair in cow.PAIRS for relative in pair)
                        except (cow.CowRefusal, OSError):
                            refusals += 1
                            continue
                        if candidate_size > byte_limit or 32 * candidate_size > meter.limit - meter.consumed:
                            refusals += 1
                            continue
                        selection = cow.RunSelection((str(sid),), min(snapshot_limit, 1), 2, byte_limit)
                        plan = cleanup.build_stale_artifact_cleanup_projection(conn, self.project_id,
                            repo_root_path=self.root, dimension=cow.DIMENSION, _run_selection=selection)
                        refusals += len(plan['refusals'])
                        if not plan['apply_plan_available'] or not plan['candidates']:
                            continue
                        # One complete native snapshot per tick; limits are caps, not a quota.
                        candidates, selected = plan['candidates'], [str(sid)]
                        if (len(candidates) != 2 or any(r['candidate_id'] in readiness['skipped_candidate_ids'] for r in candidates)
                                or 32 * candidate_size > meter.limit - meter.consumed):
                            candidates = []
                            refusals += 1
                            continue
                        sweep = end and inspected == len(rows)
                        next_cursor = None if sweep else cursor
                        if self._stop.is_set() or time.monotonic() >= meter.deadline:
                            candidates = []
                            break
                        self._persist({**self._state, 'outcome': 'running', 'policy_revision': policy['revision'],
                            'snapshot_ids': selected, 'candidate_ids': [r['candidate_id'] for r in candidates],
                            'operation_id': plan['operation_id'], 'plan_hash': plan['plan_hash'],
                            'engine_config_hash': plan['config_hash'],
                            'run_selection': selection.receipt(), 'cursor': next_cursor,
                            'sweep_complete': sweep, 'candidate_count': len(candidates), 'refusal_count': refusals})
                        try:
                            result = cleanup.apply_stale_artifact_cleanup(conn, self.project_id,
                                repo_root_path=self.root, dimension=cow.DIMENSION,
                                candidate_ids=self._state['candidate_ids'], plan_hash=plan['plan_hash'],
                                plan_revision=plan['plan_revision'], operation_id=plan['operation_id'],
                                _run_selection=selection)
                        except cleanup.StaleArtifactCleanupError as exc:
                            reason = exc.payload.get('refusal_reason')
                            if (exc.payload.get('native_prejournal_refusal') is True and reason in ENVIRONMENTAL
                                    and self._zero_effect(conn, plan)):
                                self._finish('deferred', next_cursor, sweep, effect_classification='zero_effect_deferred',
                                             digest_bytes=meter.consumed, refusal_count=refusals + 1)
                                return True
                            raise
                        if not result.get('ok') or result.get('state') != 'complete':
                            raise ValidationError('periodic_native_inspect_required')
                        self._finish('complete', next_cursor, sweep, effect_classification='complete',
                            digest_bytes=meter.consumed, observed_net_delta=(result.get('filesystem') or {}).get('observed_net_delta'))
                        return True
                sweep = end and inspected == len(rows)
                self._finish('deferred' if refusals else 'noop', None if sweep else cursor, sweep,
                    candidate_count=0, refusal_count=refusals, digest_bytes=meter.consumed,
                    effect_classification='zero_effect_deferred' if refusals else 'complete')
                return True
        except Exception as exc:
            if (not maintenance_entered and type(exc) is ValueError
                    and str(exc) in PRE_ADMISSION_UNAVAILABLE
                    and self._custody is not None):
                try:
                    state = self._load()  # Pending/held receipts still dominate availability.
                    if state['outcome'] not in {'running', 'inspect_required'}:
                        self._state = {**state, 'outcome': 'deferred',
                            'effect_classification': 'zero_effect_deferred', 'last_finished_at': _now()}
                        self._source_unavailable = True
                        return False  # No intent, journal, apply or persistence effect was admitted.
                except Exception:
                    pass
            if isinstance(exc, ValidationError) and str(exc) == 'periodic_maintenance_busy':
                return False
            self._fatal_hold = True
            try:
                if maintenance_entered and self.directory.exists():
                    with self._maintenance_lock():
                        self._state = self._load()
                        self._persist({**self._state, 'outcome': 'inspect_required',
                            'digest_bytes': meter.consumed if meter is not None else 0,
                            'effect_classification': 'partial_or_ambiguous'})
            except Exception:
                pass  # Retain running/pending intent on uncertain persistence.
            return False
        finally:
            self._inflight = False
            if conn is not None:
                conn.close()
            current = self._refresh_schedule()
            if current and due_attempted:
                self._next_due = self.monotonic() + current['interval_seconds'] if current['enabled'] else None
            self._admission.release()

    def release_hold(self, conn, body, principal: str):
        required = {'operation_id', 'expected_scheduler_revision', 'expected_scheduler_receipt_hash',
                    'expected_policy_revision'}
        if not isinstance(body, dict) or set(body) != required:
            raise ValidationError('periodic_release_fields_invalid')
        if not self.state_path.exists():
            raise ValidationError('periodic_release_no_hold')
        if not self._admission.acquire(blocking=False):
            raise ValidationError('periodic_maintenance_busy')
        try:
            with self._maintenance_lock(), self._state_lock:
                self._verify(conn)
                state = self._load()
                policy = self._policy()
                if (state['outcome'] not in {'running', 'inspect_required'}
                        or body['operation_id'] != state.get('operation_id')
                        or type(body['expected_scheduler_revision']) is not int
                        or body['expected_scheduler_revision'] != state['revision']
                        or body['expected_scheduler_receipt_hash'] != cow._digest(state)
                        or type(body['expected_policy_revision']) is not int
                        or body['expected_policy_revision'] != policy['revision']):
                    raise ValidationError('periodic_release_conflict')
                # Inventory may contain this exact unresolved original, but no other unknown journal.
                proof = cleanup._periodic_native_check(conn, self.project_id, self.root, 'resolution',
                                                       operation_id=body['operation_id'])
                if (proof.get('plan_hash') != state.get('plan_hash')
                        or proof.get('config_hash') != state.get('engine_config_hash')
                        or proof.get('run_selection') != state.get('run_selection')
                        or proof['candidate_ids'] != state.get('candidate_ids')
                        or proof['snapshot_ids'] != sorted(state.get('snapshot_ids', []))):
                    raise ValidationError('periodic_release_candidate_mismatch')
                ack = {'schema': 'snapshot_cow_periodic_release.v1', 'custody': self._custody,
                    'held_intent': state, 'held_intent_hash': cow._digest(state),
                    'scheduler_revision': state['revision'], 'policy_revision': state['policy_revision'],
                    'release_policy_revision': policy['revision'],
                    'operator_principal_hash': hashlib.sha256(principal.encode()).hexdigest(), 'proof': proof}
                ack['ack_hash'] = cow._digest(ack)
                acknowledgements = self._acks()
                acknowledgements[body['operation_id']] = ack
                cleanup._periodic_native_check(conn, self.project_id, self.root, 'readiness',
                                               acknowledgements=acknowledgements)
                directory = self.directory / 'releases'
                cow._path(directory, exists=False)
                if not directory.exists():
                    directory.mkdir(mode=0o700)
                    cow._fsync(directory.parent, directory=True)
                path = directory / (body['operation_id'] + '.json')
                if path.exists():
                    if cow._read(path) != ack:
                        raise ValidationError('periodic_release_ack_conflict')
                else:
                    cow._write(path, ack)
                self._state = state
                self._persist({**state, 'outcome': 'released', 'effect_classification': 'complete',
                               'last_finished_at': _now(), 'release_ack_hash': ack['ack_hash']})
                self._fatal_hold = False
                self._next_due = self.monotonic() + policy['interval_seconds'] if policy['enabled'] else None
                self._wake.set()
                return {'ok': True, 'operation_id': body['operation_id'], 'resolution': proof['resolution'],
                        'scheduler_revision': self._state['revision'], 'release_ack_hash': ack['ack_hash']}
        except Exception:
            self._fatal_hold = True
            raise
        finally:
            self._admission.release()

    def release_status(self, conn):
        if self._inflight or self._state['outcome'] not in {'running', 'inspect_required'}:
            return {}
        if not self._admission.acquire(blocking=False):
            return {}
        try:
            with self._maintenance_lock():
                self._verify(conn)
                state = self._load()
                policy = self._policy()
                operation_id = state.get('operation_id')
                proof = cleanup._periodic_native_check(conn, self.project_id, self.root, 'resolution',
                                                       operation_id=operation_id)
                if (proof.get('plan_hash') != state.get('plan_hash')
                        or proof.get('config_hash') != state.get('engine_config_hash')
                        or proof.get('run_selection') != state.get('run_selection')
                        or proof['candidate_ids'] != state.get('candidate_ids')
                        or proof['snapshot_ids'] != sorted(state.get('snapshot_ids', []))):
                    return {}
                ack = {'schema': 'snapshot_cow_periodic_release.v1', 'custody': self._custody,
                    'held_intent_hash': cow._digest(state), 'scheduler_revision': state['revision'],
                    'policy_revision': state['policy_revision'],
                    'operator_principal_hash': '0' * 64, 'proof': proof}
                acks = self._acks()
                acks[operation_id] = ack
                cleanup._periodic_native_check(conn, self.project_id, self.root, 'readiness', acknowledgements=acks)
                return {'hold_release_available': True, 'release_hold_request': {
                    'operation_id': operation_id, 'expected_scheduler_revision': state['revision'],
                    'expected_scheduler_receipt_hash': cow._digest(state),
                    'expected_policy_revision': policy['revision']}, 'next_action': 'release_exact_scheduler_hold'}
        except Exception:
            return {}
        finally:
            self._admission.release()

    def status(self):
        try:
            policy = self._policy()
        except Exception:
            policy = None
        with self._state_lock:
            state = dict(self._state)
        held = self._fatal_hold or state['outcome'] in {'running', 'inspect_required'}
        remaining = max(0, self._next_due - self.monotonic()) if self._next_due is not None else None
        due = (datetime.fromtimestamp(time.time() + remaining, timezone.utc).isoformat()
               if remaining is not None and not held and not self._stop.is_set() else None)
        return {'supported': self._supported, 'configured': bool(policy and policy['enabled']),
            'active': bool(self._inflight), 'runtime_plane': 'dev',
            'same_world_custody_status': 'temporarily_unavailable' if self._source_unavailable else
                                         'verified' if self._supported else 'unverified',
            'policy_revision': policy['revision'] if policy else None, 'enabled': bool(policy and policy['enabled']),
            'interval_seconds': policy['interval_seconds'] if policy else None, 'next_due_at': due,
            'last_started_at': state.get('last_started_at'), 'last_finished_at': state.get('last_finished_at'),
            'last_outcome': 'inspect_required' if held else state['outcome'],
            'last_operation_id': state.get('operation_id'), 'candidate_count': state.get('candidate_count', 0),
            'refusal_count': state.get('refusal_count', 0), 'cursor': {
                'snapshot_id': state['cursor'][1], 'keyset_hash': cow._digest(state['cursor'])
            } if state.get('cursor') else None,
            'sweep_complete': state.get('sweep_complete', False), 'inspect_required': held,
            'effect_classification': state.get('effect_classification', 'unknown'),
            'observed_net_delta': state.get('observed_net_delta'), 'digest_bytes': state.get('digest_bytes', 0),
            'effective_max_snapshots_per_run': 1, 'effective_complete_pairs_per_snapshot': 2,
            'effective_max_run_seconds': policy['max_run_seconds'] if policy else None,
            'scheduler_revision': state['revision'], 'scheduler_receipt_hash': cow._digest(state),
            'hold_release_available': False,
            'next_action': ('inspect_native_operation_then_release_hold' if state.get('operation_id') else
                            'inspect_custody_and_scheduler_inventory') if held else
                           'wait_for_source_custody_then_next_interval' if self._source_unavailable else
                           'configure_enabled_policy' if not policy or not policy['enabled'] else 'wait_for_due'}
