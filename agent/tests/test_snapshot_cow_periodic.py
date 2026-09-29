"""Isolated service/controller forward tests; no operational storage or policies."""
from __future__ import annotations

import copy
import http.client
import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from http.server import HTTPServer

import pytest

from agent.governance import snapshot_cow_periodic as periodic
from agent.governance import snapshot_cow_cleanup as cow, stale_artifact_cleanup as cleanup
from agent.governance import project_service, server, db
from agent.governance.errors import ValidationError, PermissionDeniedError
from agent.tests.test_snapshot_cow_cleanup import cow_fixture


@pytest.fixture
def controller(cow_fixture, monkeypatch):
    conn, root, base, config, _ = cow_fixture
    class Borrowed:
        def __getattr__(self, name):
            return getattr(conn, name)
        def close(self):
            pass
    clock = [0.0]
    custody = cow._custody(conn, 'proj', root)
    policy = {**periodic.parse_policy({'enabled': True}, stored=False), 'revision': 1,
              'operator_principal_hash': '1' * 64, 'custody': custody}
    config['governance']['snapshot_cow_cleanup_periodic'] = policy
    monkeypatch.setattr(cow, '_quiet', lambda *_args: None)
    obj = periodic.PeriodicController('proj', root, lambda: Borrowed(),
                                      custody_check=lambda *_args: cow._custody(conn, 'proj', root),
                                      monotonic=lambda: clock[0])
    obj._verify(conn)
    obj._supported = True
    obj._refresh_schedule()
    def due():
        clock[0] += policy['interval_seconds']
        return obj.tick()
    obj.due = due
    obj.clock = clock
    obj.fixture = cow_fixture
    obj.policy = policy
    yield obj
    obj.stop()


def add_snapshot(controller, sid, created='2020-02-01', *, oversized=False):
    conn, _, base, _, _ = controller.fixture
    target = cow.snapshots._snapshot_root('proj', sid)
    shutil.copytree(base, target)
    if oversized:
        for pair in cow.PAIRS:
            for relative in pair:
                (target / relative).write_bytes(b'x' * 400_000)
    conn.execute('INSERT INTO graph_snapshots(project_id,snapshot_id,snapshot_kind,status,created_at,commit_sha) '
                 'VALUES(?,?,?,?,?,?)', ('proj', sid, 'full', 'superseded', created, 'fixture'))
    conn.commit()
    return target


@pytest.mark.parametrize('bad', [ {'enabled': 1}, {'enabled': 'false'}, {'interval_seconds': True},
    {'interval_seconds': 59}, {'max_pairs_per_run': 1}, {'max_hash_bytes_per_run': 129 * periodic.GiB},
    {'max_snapshots_per_run': 2, 'max_pairs_per_run': 2}, {'snapshot_ids': ['../escape']},
    {'snapshot_ids': ['full-a', 'full-a']}, {'custody': {}}, {'unexpected': True}])
def test_policy_strict_rejects_incompatible_and_client_authority(bad):
    with pytest.raises(ValidationError):
        periodic.parse_policy(bad, stored=False)


def test_disabled_and_first_due_no_catchup(controller):
    assert not controller.tick()
    assert controller.status()['configured'] and not controller.status()['active']
    assert controller.status()['effective_max_snapshots_per_run'] == 1
    controller.policy['enabled'] = False
    controller.policy['revision'] += 1
    assert not controller.due()
    assert controller.status()['next_due_at'] is None
    assert not cow._state_root('proj').exists()
    controller.policy['enabled'] = True
    controller.policy['revision'] += 1
    assert not controller.tick()  # Reset to now + interval, not a catch-up run.


def test_registry_cas_stale_writers_and_parent_fsync(tmp_path, monkeypatch):
    path = tmp_path / 'projects.json'
    monkeypatch.setattr(project_service, '_projects_file', lambda: path)
    engine = {'max_hash_bytes': 4 * periodic.GiB, 'archive_root': 'fixture-engine-only'}
    project_service._save_projects({'projects': {'proj': {'project_config': {
        'governance': {'snapshot_cow_cleanup': engine}, 'unrelated': {'x': 1}}}}})
    stale = project_service._load_projects()
    calls = []
    real_fsync = project_service.os.fsync
    def fsync(fd):
        calls.append(os.fstat(fd).st_mode)
        return real_fsync(fd)
    monkeypatch.setattr(project_service.os, 'fsync', fsync)
    enabled = project_service.update_snapshot_cow_periodic_policy('proj', {'enabled': True},
        expected_revision=0, principal='fixture operator', custody={'fixture': True})
    assert enabled['revision'] == 1 and 'fixture operator' not in json.dumps(enabled)
    old_enabled = project_service._load_projects()
    disabled = project_service.update_snapshot_cow_periodic_policy('proj', {'enabled': False},
        expected_revision=1, principal='fixture operator', custody={'fixture': True})
    project_service._save_projects(old_enabled)
    project_service._save_projects(stale)
    assert project_service.get_snapshot_cow_periodic_policy('proj') == disabled
    config = project_service.get_project_config_metadata('proj')
    assert config['governance']['snapshot_cow_cleanup'] == engine and config['unrelated'] == {'x': 1}
    with pytest.raises(ValidationError, match='revision_conflict'):
        project_service.update_snapshot_cow_periodic_policy('proj', {}, expected_revision=1,
                                                            principal='fixture', custody={})
    import stat
    assert any(stat.S_ISDIR(mode) for mode in calls)


def test_registry_parent_fsync_failure_not_acknowledged(tmp_path, monkeypatch):
    path = tmp_path / 'projects.json'
    monkeypatch.setattr(project_service, '_projects_file', lambda: path)
    project_service._save_projects({'projects': {'proj': {'project_config': {}}}})
    real_fsync = os.fsync
    def fsync(fd):
        import stat
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('fixture directory sync unavailable')
        return real_fsync(fd)
    monkeypatch.setattr(project_service.os, 'fsync', fsync)
    with pytest.raises(OSError):
        project_service.update_snapshot_cow_periodic_policy('proj', {}, expected_revision=0,
                                                            principal='fixture', custody={})


def test_due_real_native_selection_and_meter(controller):
    add_snapshot(controller, 'full-later')
    engine_before = copy.deepcopy(controller.fixture[3]['governance']['snapshot_cow_cleanup'])
    assert controller.due()
    state = cow._read(controller.state_path)
    assert state['outcome'] == 'complete' and state['snapshot_ids'] == ['full-old']
    assert len(state['candidate_ids']) == 2 and state['digest_bytes'] > 0
    assert state['digest_bytes'] <= controller.policy['max_hash_bytes_per_run']
    assert not state['sweep_complete'] and state['cursor'] == ['2020-01-01', 'full-old']
    assert controller.fixture[3]['governance']['snapshot_cow_cleanup'] == engine_before
    assert controller.due()
    assert controller._state['snapshot_ids'] == ['full-later']
    assert controller._state['sweep_complete'] and controller._state['cursor'] is None
    assert controller.due()
    assert controller._state['candidate_count'] == 0  # Completion indexes prevent replay.


def test_bounded_progression_protected_oversized_excluded_then_safe(controller):
    conn = controller.fixture[0]
    for index in range(24):
        add_snapshot(controller, f'full-{index:02}', f'2020-01-{index + 2:02}', oversized=index == 1)
    conn.execute("INSERT INTO graph_snapshot_refs(project_id,ref_name,snapshot_id,updated_at,commit_sha) "
                 "VALUES('proj','fixture-pin','full-old','2020','fixture')")
    conn.commit()
    controller.policy['exclude_snapshot_ids'] = [f'full-{index:02}' for index in range(2, 23)]
    controller.policy['snapshot_ids'] = ['full-old', 'full-01', 'full-23']
    assert controller.due()
    assert controller._state['candidate_count'] == 0 and not controller._state['sweep_complete']
    assert controller._state['cursor'] == ['2020-01-20', 'full-18']
    assert controller.due()
    assert controller._state['outcome'] == 'complete' and controller._state['snapshot_ids'] == ['full-23']
    assert controller._state['cursor'] is None and controller._state['sweep_complete']


def test_large_budget_arithmetic_admits_1_6gib_and_native_hardmax():
    for aggregate, candidate in [(64 * periodic.GiB, int(1.6 * periodic.GiB)),
                                  (128 * periodic.GiB, 4 * periodic.GiB)]:
        assert candidate <= min(4 * periodic.GiB, aggregate // 32)
        assert 32 * candidate <= aggregate
    assert 4 * periodic.GiB > 64 * periodic.GiB // 32


def test_native_pair_cap_one_is_deferred_no_journal(controller):
    controller.fixture[3]['governance']['snapshot_cow_cleanup']['max_pairs'] = 1
    assert controller.due()
    assert controller._state['outcome'] == 'deferred' and controller._state['candidate_count'] == 0
    assert not cow._state_root('proj').exists()


def test_missing_archive_known_zeroeffect_reevaluates(controller, monkeypatch):
    archive = cow._archive
    monkeypatch.setattr(cow, '_archive', lambda *_a, **_k: (_ for _ in ()).throw(cow.CowRefusal('cow_archive_unavailable')))
    assert controller.due()
    assert controller._state['outcome'] == 'deferred'
    assert controller._state['effect_classification'] == 'zero_effect_deferred'
    assert not cow._state_root('proj').exists()
    monkeypatch.setattr(cow, '_archive', archive)
    assert controller.due() and controller._state['outcome'] == 'complete'


def test_generic_wrapper_false_does_not_clear_durable_hold(controller, monkeypatch):
    def ambiguous(*_args, **_kwargs):
        raise cleanup.StaleArtifactCleanupError('fixture', {'writes_performed': False})
    monkeypatch.setattr(cleanup, 'apply_stale_artifact_cleanup', ambiguous)
    assert not controller.due()
    assert controller._state['outcome'] == 'inspect_required'
    assert not controller.due()
    controller.policy['enabled'] = False
    controller.policy['revision'] += 1
    assert not controller.due() and controller.status()['inspect_required']


def test_pending_unknown_and_bounded_journals_refuse(controller):
    directory = cow._state_root('proj', create=True)
    (directory / ('cowop-' + '0' * 32 + '.json.pending')).write_text('{}')
    assert not controller.due() and controller.status()['inspect_required']


@pytest.mark.parametrize('inventory_kind', ['unknown', 'truncated'])
def test_unknown_or_truncated_inventory_never_safe(controller, inventory_kind):
    directory = cow._state_root('proj', create=True)
    for index in range(1001 if inventory_kind == 'truncated' else 1):
        name = f'cow-{index:024x}.complete.json' if inventory_kind == 'truncated' else 'unknown.json'
        (directory / name).write_text('{}')
    with pytest.raises(cleanup.StaleArtifactCleanupError):
        controller._readiness(controller.fixture[0])
    assert not controller.due() and controller.status()['inspect_required']


def test_running_intent_restart_zero_blind_apply(controller, monkeypatch):
    controller._ensure_directory()
    controller._persist({**controller._state, 'outcome': 'running', 'operation_id': 'cowop-' + '0' * 32})
    restarted = periodic.PeriodicController('proj', controller.root, controller.connection_factory,
        custody_check=controller.custody_check, monotonic=controller.monotonic)
    restarted._verify(controller.fixture[0])
    restarted._refresh_schedule()
    restarted.clock = controller.clock
    controller.clock[0] += controller.policy['interval_seconds']
    monkeypatch.setattr(cleanup, 'apply_stale_artifact_cleanup', lambda *_a, **_k: pytest.fail('blind replay'))
    assert not restarted.tick() and restarted.status()['inspect_required']
    restarted.stop()


def release_body(controller):
    return {'operation_id': controller._state['operation_id'],
            'expected_scheduler_revision': controller._state['revision'],
            'expected_scheduler_receipt_hash': cow._digest(controller._state),
            'expected_policy_revision': controller.policy['revision']}


def test_partial_restore_release_restart_later_candidate_with_original_retained(controller, monkeypatch):
    add_snapshot(controller, 'full-later')
    original_clone = cow._clone
    clones = [0]
    def second_clone_refusal(*args):
        clones[0] += 1
        if clones[0] == 2:
            raise cow.CowRefusal('fixture second pair stopped')
        original_clone(*args)
    monkeypatch.setattr(cow, '_clone', second_clone_refusal)
    assert not controller.due()
    held = copy.deepcopy(controller._state)
    operation = held['operation_id']
    original_path = cow._journal_path('proj', operation)
    assert cow._read(original_path)['state'] == 'partial_or_ambiguous'
    before = original_path.read_bytes()
    wrong = {**release_body(controller), 'expected_scheduler_revision': 999}
    with pytest.raises(ValidationError):
        controller.release_hold(controller.fixture[0], wrong, 'fixture operator')
    assert original_path.read_bytes() == before
    assert not controller.release_status(controller.fixture[0]).get('hold_release_available')
    restored = cleanup.recover_snapshot_cow_cleanup(controller.fixture[0], 'proj',
        repo_root_path=controller.root, operation_id=operation, action='restore')
    assert restored['ok'] and restored['restored_count'] == 1
    assert controller.release_status(controller.fixture[0])['hold_release_available']
    result = controller.release_hold(controller.fixture[0], release_body(controller), 'fixture operator')
    assert result['ok'] and result['resolution'] == 'restored'
    assert original_path.read_bytes() == before  # Retain exact nonterminal original.
    controller.policy['revision'] += 1  # Timer revision does not revoke resolved proof.
    monkeypatch.setattr(cow, '_clone', original_clone)
    restarted = periodic.PeriodicController('proj', controller.root, controller.connection_factory,
        custody_check=controller.custody_check, monotonic=controller.monotonic)
    restarted._verify(controller.fixture[0])
    restarted._state = restarted._load()
    restarted._refresh_schedule()
    controller.clock[0] += controller.policy['interval_seconds']
    assert restarted.tick() and restarted._state['outcome'] == 'complete'
    assert restarted._state['snapshot_ids'] == ['full-later']
    assert restarted._state['operation_id'] != operation
    assert original_path.read_bytes() == before
    ack_path = controller.directory / 'releases' / (operation + '.json')
    ack = cow._read(ack_path)
    ack['proof']['original_journal_hash'] = '0' * 64
    ack['ack_hash'] = cow._digest({k: v for k, v in ack.items() if k != 'ack_hash'})
    cow._write(ack_path, ack)
    controller.clock[0] += controller.policy['interval_seconds']
    assert not restarted.tick() and restarted.status()['inspect_required']
    restarted.stop()


def test_meter_exhaustion_after_replace_enters_inspection(controller, monkeypatch):
    original = cow._completed_readback
    def exhausted(*args):
        meter = cow._DIGEST_METER.get()
        if meter is not None:
            meter.debit(meter.limit + 1)
        original(*args)
    monkeypatch.setattr(cow, '_completed_readback', exhausted)
    assert not controller.due() and controller.status()['inspect_required']
    record = cow._read(cow._journal_path('proj', controller._state['operation_id']))
    assert record['state'] == 'partial_or_ambiguous'
    assert record['entries'][0].get('replacement')


def test_terminal_scheduler_write_failure_keeps_hold(controller, monkeypatch):
    original = cow._write
    def write(path, value):
        if path == controller.state_path and value.get('outcome') != 'running':
            raise OSError('fixture terminal persistence unavailable')
        return original(path, value)
    monkeypatch.setattr(cow, '_write', write)
    assert not controller.due()
    assert cow._read(controller.state_path)['outcome'] == 'running'
    assert controller.status()['inspect_required']
    assert not controller.due()


def test_os_overlap_and_slow_shutdown_join(controller, monkeypatch):
    import fcntl
    controller._ensure_directory()
    fd = os.open(controller.directory / 'maintenance.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValidationError, match='maintenance_busy'):
            with controller._maintenance_lock():
                pytest.fail('overlap')
    finally:
        os.close(fd)
    entered, finish = threading.Event(), threading.Event()
    def owned():
        with controller._admission:
            controller._inflight = True
            entered.set()
            finish.wait(3)
            controller._inflight = False
    worker = threading.Thread(target=owned)
    worker.start()
    assert entered.wait(1)
    assert not controller.tick()
    stopper = threading.Thread(target=controller.stop)
    stopper.start()
    assert controller._stop.wait(1)
    assert stopper.is_alive()  # Custody not released while native work owns the admission.
    finish.set()
    stopper.join(2)
    worker.join(2)
    assert not stopper.is_alive()


def test_service_custody_rejects_wrong_plane_project_and_certificate(controller, monkeypatch):
    conn, root, *_ = controller.fixture
    monkeypatch.setattr(server, '_runtime_plane', lambda: 'stable')
    with pytest.raises(ValidationError):
        periodic.service_custody(conn, 'aming-claw', root)
    monkeypatch.setattr(server, '_runtime_plane', lambda: 'dev')
    with pytest.raises(ValidationError):
        periodic.service_custody(conn, 'proj', root)
    monkeypatch.setattr(server, '_current_terminalization_manager_identity', lambda *_:
                        (_ for _ in ()).throw(RuntimeError('fixture lease absent')))
    with pytest.raises(RuntimeError):
        periodic.service_custody(conn, 'aming-claw', root)


def test_dev_lifecycle_stop_before_return_with_no_general_workers(controller, monkeypatch):
    events = []
    class Owned:
        def __init__(self, *_a, **_kw):
            pass
        def start(self):
            events.append('start')
        def stop(self):
            events.append('join')
    class HTTP:
        def serve_forever(self):
            events.append('serve')
            raise KeyboardInterrupt
        def server_close(self):
            events.append('close')
    monkeypatch.setattr(periodic, 'PeriodicController', Owned)
    monkeypatch.setattr(server, '_runtime_plane', lambda: 'dev')
    monkeypatch.setattr(server, '_GOVERNANCE_SINGLETON_LEASE', object())
    monkeypatch.setattr(server, '_GOVERNANCE_MANAGER_CERTIFICATES', {'aming-claw': {'fixture': True}})
    monkeypatch.setattr(server, 'create_server', lambda: HTTP())
    server._run_governance_service()
    assert events == ['start', 'serve', 'join', 'close']
    assert server._SNAPSHOT_COW_PERIODIC_CONTROLLER is None


def test_authenticated_policy_status_and_wrong_role_project(controller, tmp_path, monkeypatch):
    conn = controller.fixture[0]
    monkeypatch.setattr(server, 'get_connection', lambda _pid: controller.connection_factory())
    monkeypatch.setattr(server, '_SNAPSHOT_COW_PERIODIC_CONTROLLER', controller)
    monkeypatch.setattr(periodic, 'service_custody', controller.custody_check)
    monkeypatch.setattr(server.role_service, 'authenticate', lambda _conn, token:
                        {'project_id': 'other' if token == 'other' else 'proj',
                         'role': 'mf_sub' if token == 'worker' else 'observer'})
    path = tmp_path / 'registry.json'
    monkeypatch.setattr(project_service, '_projects_file', lambda: path)
    project_service._save_projects({'projects': {'proj': {'project_config': controller.fixture[3]}}},
                                  _periodic_owner=True)
    monkeypatch.setattr(project_service, 'get_project_config_metadata', lambda pid:
                        copy.deepcopy(project_service._load_projects()['projects'][pid]['project_config']))
    engine_hash = cow._digest(cow._config('proj'))
    ctx = lambda token, body={}: server.RequestContext(None, 'GET', {'project_id': 'proj'}, {}, body,
                                                       'req-periodic-fixture', token, '')
    for handler in [server.handle_snapshot_cow_periodic_config_get, server.handle_snapshot_cow_periodic_status,
                    server.handle_snapshot_cow_periodic_config_put, server.handle_snapshot_cow_periodic_release_hold]:
        assert handler(ctx(''))[0] == 401
        assert handler(ctx('other'))[0] == 403
        with pytest.raises(PermissionDeniedError):
            handler(ctx('worker'))
    status = server.handle_snapshot_cow_periodic_status(ctx('operator'))
    assert status['supported'] and status['configured'] and not status['active']
    assert str(controller.root) not in json.dumps(status) and 'operator' not in json.dumps(status)
    assert 'reclaimed_bytes' not in status
    assert server.handle_snapshot_cow_periodic_config_put(ctx('operator', {'policy': {'enabled': False},
        'expected_revision': 999}))[0] == 400

    changed = server.handle_snapshot_cow_periodic_config_put(ctx('operator', {
        'policy': {'enabled': False, 'interval_seconds': 120}, 'expected_revision': 1}))
    assert changed['ok'] and changed['source'] == 'aming_claw_registry'
    assert changed['policy']['revision'] == 2 and not changed['policy']['enabled']
    readback = server.handle_snapshot_cow_periodic_config_get(ctx('operator'))
    assert readback == changed
    assert cow._digest(cow._config('proj')) == engine_hash
    status = server.handle_snapshot_cow_periodic_status(ctx('operator'))
    assert status['supported'] and not status['configured'] and not status['active']


@pytest.fixture
def periodic_http(monkeypatch):
    # Keep the real dispatcher/world guard; this isolated project is generic.
    monkeypatch.setattr(server, '_runtime_plane', lambda: 'generic')
    httpd = HTTPServer(('127.0.0.1', 0), server.GovernanceHandler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()

    def request(method, path, body=None, token=''):
        conn = http.client.HTTPConnection(*httpd.server_address, timeout=5)
        try:
            payload = json.dumps(body) if body is not None else None
            conn.request(method, path, body=payload,
                         headers={'Content-Type': 'application/json', 'X-Gov-Token': token})
            response = conn.getresponse()
            raw = response.read()
            headers = dict(response.getheaders())
            value = json.loads(raw) if headers.get('Content-Type') == 'application/json' else raw
            return response.status, value, headers
        finally:
            conn.close()

    yield request
    httpd.shutdown()
    thread.join(5)
    httpd.server_close()
    assert not thread.is_alive()


def test_real_http_periodic_put_auth_disabled_cas_and_stale_no_mutation(
        controller, periodic_http, tmp_path, monkeypatch):
    conn, _, _, config, _ = controller.fixture
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    def connection():
        value = sqlite3.connect(database)
        value.row_factory = sqlite3.Row
        return value
    controller.connection_factory = connection
    controller.custody_check = lambda conn, pid, root: cow._custody(conn, pid, root)
    monkeypatch.setattr(server, 'get_connection', lambda _pid: connection())
    monkeypatch.setattr(server, '_SNAPSHOT_COW_PERIODIC_CONTROLLER', controller)
    monkeypatch.setattr(periodic, 'service_custody', controller.custody_check)
    # Real token lookup and permission checks, with an empty in-process cache.
    cache = SimpleNamespace(get_session_by_token=lambda _hash: None,
                            cache_session=lambda *_args: None,
                            cache_token_session=lambda *_args: None)
    monkeypatch.setattr(server.role_service, 'get_redis', lambda: cache)
    for token, role, project in [('operator', 'observer', 'proj'),
                                 ('worker', 'mf_sub', 'proj'),
                                 ('other', 'observer', 'other')]:
        conn.execute('INSERT INTO sessions(session_id,principal_id,project_id,role,scope_json,token_hash,'
                     'status,created_at,expires_at,last_heartbeat,metadata_json) VALUES(?,?,?,?,?,?,?, ?,?,?,?)',
                     (token, token, project, role, '[]', server.role_service._hash_token(token),
                      'active', '2020-01-01T00:00:00Z', '2099-01-01T00:00:00Z',
                      '2020-01-01T00:00:00Z', '{}'))
    conn.commit()
    path = tmp_path / 'http-registry.json'
    monkeypatch.setattr(project_service, '_projects_file', lambda: path)
    isolated_config = copy.deepcopy(config)
    isolated_config['governance'].pop('snapshot_cow_cleanup_periodic')
    project_service._save_projects({'projects': {'proj': {'project_config': isolated_config}}},
                                  _periodic_owner=True)
    monkeypatch.setattr(project_service, 'get_project_config_metadata', lambda pid:
                        copy.deepcopy(project_service._load_projects()['projects'][pid]['project_config']))
    controller.configuration_changed()
    engine_hash = cow._digest(cow._config('proj'))
    url = '/api/graph-governance/proj/snapshot-cow-periodic/config'
    body = {'policy': {'enabled': False, 'interval_seconds': 120}, 'expected_revision': 0}
    before = path.read_bytes()
    for token, expected in [('', 401), ('invalid', 401), ('worker', 403), ('other', 403)]:
        status, result, headers = periodic_http('PUT', url, body, token)
        assert status == expected, (status, result)
        assert 'PUT' in headers['Access-Control-Allow-Methods']
        assert path.read_bytes() == before
    status, changed, headers = periodic_http('PUT', url, body, 'operator')
    assert status == 200, changed
    assert changed['ok'] and changed['policy']['revision'] == 1
    assert changed['policy']['enabled'] is False and changed['policy']['interval_seconds'] == 120
    assert changed['source'] == 'aming_claw_registry' and changed['request_id'].startswith('req-')
    status, readback, _ = periodic_http('GET', url, token='operator')
    assert status == 200 and readback['policy'] == changed['policy']
    before = path.read_bytes()
    status, refused, _ = periodic_http('PUT', url, body, 'operator')
    assert status == 400 and refused['error'] == 'periodic_request_refused'
    assert refused['writes_performed'] is False and path.read_bytes() == before
    status, refused, _ = periodic_http('PUT', url, {'expected_revision': 1}, 'operator')
    assert status == 400 and refused['error'] == 'periodic_config_fields_invalid'
    assert path.read_bytes() == before and cow._digest(cow._config('proj')) == engine_hash
    assert not controller.status()['configured'] and not controller.status()['active']
    assert not controller.directory.exists() and not cow._state_root('proj').exists()
    # No POST config alias; the existing release-hold POST remains a guarded route.
    assert periodic_http('POST', url, body, 'operator')[0] == 404
    status, refused, _ = periodic_http('POST', url.replace('/config', '/release-hold'), {}, 'operator')
    assert status == 400 and refused['writes_performed'] is False
    assert path.read_bytes() == before
    for method in ('GET', 'POST', 'PUT', 'DELETE'):
        status, refused, cors = periodic_http(method, '/api/unknown-put-fixture')
        assert status == 404 and refused['error'] == 'not_found'
        assert cors['Access-Control-Allow-Methods'] == headers['Access-Control-Allow-Methods']
    status, empty, cors = periodic_http('OPTIONS', url)
    assert status == 204 and empty == b'' and cors['Content-Length'] == '0'
    assert cors['Access-Control-Allow-Methods'] == headers['Access-Control-Allow-Methods']
    monkeypatch.setattr(server, '_runtime_plane', lambda: 'dev')
    status, refused, _ = periodic_http('PUT', url, body, 'operator')
    assert status == 400 and refused['error'] == 'invalid_request'
    assert refused['details']['writes_performed'] is False and path.read_bytes() == before


@pytest.mark.parametrize('method', ['GET', 'POST', 'DELETE'])
def test_real_http_existing_dispatch_methods(periodic_http, monkeypatch, method):
    def echo(ctx):
        return {'method': ctx.method, 'body': ctx.body, 'query': ctx.query}
    monkeypatch.setattr(server, 'ROUTES', [(method, '/api/fixture/{project_id}', echo)])
    status, result, _ = periodic_http(method, '/api/fixture/proj?q=present', {'value': 1})
    assert status == 200 and result['method'] == method and result['query'] == {'q': 'present'}
    assert result['body'] == ({'value': 1} if method == 'POST' else {})


def test_busy_due_never_overwrites_other_scheduler_intent(controller):
    import fcntl
    controller._ensure_directory()
    held = controller._persist({**controller._state, 'outcome': 'running',
                                'operation_id': 'cowop-' + '1' * 32})
    before = controller.state_path.read_bytes()
    fd = os.open(controller.directory / 'maintenance.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not controller.due()
        assert controller.state_path.read_bytes() == before
        assert controller._state == held and not controller._fatal_hold
    finally:
        os.close(fd)


def test_real_inflight_disable_reconfigure_and_shutdown_freeze_run_and_join(controller, monkeypatch):
    import sqlite3
    database = controller.fixture[0].execute('PRAGMA database_list').fetchone()[2]
    def connection():
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        return conn
    controller.connection_factory = connection
    controller.custody_check = lambda conn, pid, root: cow._custody(conn, pid, root)
    entered, finish = threading.Event(), threading.Event()
    real_clone = cow._clone
    def slow(*args):
        entered.set()
        assert finish.wait(3)
        real_clone(*args)
    monkeypatch.setattr(cow, '_clone', slow)
    controller.clock[0] += controller.policy['interval_seconds']
    result = []
    worker = threading.Thread(target=lambda: result.append(controller.tick()))
    worker.start()
    assert entered.wait(2)
    assert controller.status()['active'] and not controller.tick()
    controller.policy.update(enabled=False, interval_seconds=60, revision=2)
    controller.configuration_changed()
    assert not controller.status()['configured']
    stopper = threading.Thread(target=controller.stop)
    stopper.start()
    assert controller._stop.wait(1) and stopper.is_alive()
    finish.set()
    stopper.join(3)
    worker.join(3)
    assert not stopper.is_alive() and result == [True]
    assert controller._state['outcome'] == 'complete' and controller._state['policy_revision'] == 1
    assert not controller.tick() and not controller.status()['active']
    assert controller.status()['next_due_at'] is None


def test_readiness_completed_history_is_metadata_only(controller, monkeypatch):
    assert controller.due() and controller._state['outcome'] == 'complete'
    monkeypatch.setattr(cow, '_hash', lambda *_a, **_k: pytest.fail('historical body digest in readiness'))
    assert controller._readiness(controller.fixture[0])['ready']


@pytest.mark.parametrize('fault', ['original_pending', 'recovery_pending', 'unknown_stage', 'target_drift'])
def test_exact_hold_release_pending_stage_or_fingerprint_refuses(controller, monkeypatch, fault):
    monkeypatch.setattr(cow, '_clone', lambda *_a: (_ for _ in ()).throw(cow.CowRefusal('fixture pre-replace stop')))
    assert not controller.due()
    state_before = controller.state_path.read_bytes()
    operation = controller._state['operation_id']
    path = cow._journal_path('proj', operation)
    if fault == 'original_pending':
        path.with_name(path.name + '.pending').write_text('{}')
    elif fault == 'recovery_pending':
        path.with_name(operation + '.recovery.json.pending').write_text('{}')
    elif fault == 'unknown_stage':
        (controller.fixture[2] / Path(cow.PAIRS[0][1]).parent / ('.snapshot-cow-' + '1' * 32)).write_text('fixture')
    else:
        (controller.fixture[2] / cow.PAIRS[0][1]).write_bytes(b'changed unrelated target')
    with pytest.raises((ValidationError, cleanup.StaleArtifactCleanupError)):
        controller.release_hold(controller.fixture[0], release_body(controller), 'fixture operator')
    assert controller.state_path.read_bytes() == state_before
    assert not (controller.directory / 'releases').exists()


def test_original_noop_release_skips_exact_old_candidates(controller, monkeypatch):
    add_snapshot(controller, 'full-later')
    original_clone = cow._clone
    monkeypatch.setattr(cow, '_clone', lambda *_a: (_ for _ in ()).throw(cow.CowRefusal('fixture first clone stopped')))
    assert not controller.due()
    operation = controller._state['operation_id']
    restored = cleanup.recover_snapshot_cow_cleanup(controller.fixture[0], 'proj',
        repo_root_path=controller.root, operation_id=operation, action='restore')
    assert restored['ok'] and restored['state'] == 'original_noop'
    assert controller.release_hold(controller.fixture[0], release_body(controller), 'fixture')['resolution'] == 'original_noop'
    monkeypatch.setattr(cow, '_clone', original_clone)
    assert controller.due() and controller._state['snapshot_ids'] == ['full-later']


def test_proven_source_binding_unavailable_before_admission_defers_then_clean_tick(controller):
    original = controller.connection_factory
    controller.connection_factory = lambda: (_ for _ in ()).throw(
        ValueError('AC dev first-start source binding is invalid'))
    assert not controller.due()
    status = controller.status()
    assert status['last_outcome'] == 'deferred' and not status['inspect_required']
    assert status['effect_classification'] == 'zero_effect_deferred'
    assert status['next_action'] == 'wait_for_source_custody_then_next_interval'
    assert not controller.directory.exists() and not cow._state_root('proj').exists()
    controller.connection_factory = original
    assert not controller.tick()  # No immediate catch-up after source becomes available.
    assert controller.due() and controller._state['outcome'] == 'complete'


def test_source_unavailable_does_not_clear_durable_running_intent(controller):
    controller._ensure_directory()
    controller._persist({**controller._state, 'outcome': 'running',
                          'operation_id': 'cowop-' + '1' * 32})
    before = controller.state_path.read_bytes()
    controller.connection_factory = lambda: (_ for _ in ()).throw(
        ValueError('AC dev first-start source binding is invalid'))
    assert not controller.due() and controller.status()['inspect_required']
    assert controller.state_path.read_bytes() == before


def test_not_due_probe_never_postpones_admission_and_release_without_hold_zero_write(controller):
    due = controller._next_due
    controller.clock[0] = due - 1
    assert not controller.tick() and controller._next_due == due
    with pytest.raises(ValidationError, match='release_no_hold'):
        controller.release_hold(controller.fixture[0], {'operation_id': 'cowop-' + '0' * 32,
            'expected_scheduler_revision': 0, 'expected_scheduler_receipt_hash': '0' * 64,
            'expected_policy_revision': 1}, 'fixture operator')
    assert not controller.directory.exists()
    controller.clock[0] = due
    assert controller.tick() and controller._state['outcome'] == 'complete'
