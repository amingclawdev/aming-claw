import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))


def _wait_for(predicate, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for guided runtime E2E state")


def _git(cwd, *args):
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _dogfood_branch(tmp_path, *, suffix, fence_token):
    from governance.parallel_branch_runtime import (
        STATE_WORKTREE_READY,
        branch_runtime_allocation_evidence,
        plan_branch_runtime_context,
    )

    main = tmp_path / "main"
    main.mkdir(parents=True)
    _git(main, "init")
    _git(main, "checkout", "-b", "main")
    (main / "README.md").write_text("guided runtime fixture\n", encoding="utf-8")
    _git(main, "add", "README.md")
    _git(
        main,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "fixture",
    )
    head = _git(main, "rev-parse", "HEAD")
    context = plan_branch_runtime_context(
        project_id="aming-claw",
        task_id="task-guided-{}".format(suffix),
        workspace_root=str(tmp_path),
        backlog_id="AC-GUIDED-E2E",
        parent_task_id="AC-GUIDED-E2E",
        chain_id="AC-GUIDED-E2E",
        root_task_id="AC-GUIDED-E2E",
        stage_type="observer_dogfood",
        agent_id="dogfood_observer",
        worker_id="principal-guided-{}".format(suffix),
        allocation_owner="dogfood_observer",
        worker_slot_id="principal-guided-{}".format(suffix),
        target_files=("agent/owned-{}.py".format(suffix),),
        owned_files=("agent/owned-{}.py".format(suffix),),
        branch_prefix="guided",
        worktree_root=".worktrees",
        base_commit=head,
        target_head_commit=head,
        merge_queue_id="mq-guided-e2e",
        fence_token=fence_token,
        status=STATE_WORKTREE_READY,
    )
    worktree = Path(context.worktree_path)
    worktree.parent.mkdir(parents=True)
    branch_name = context.branch_ref.removeprefix("refs/heads/")
    _git(main, "worktree", "add", "-b", branch_name, str(worktree), head)
    evidence = branch_runtime_allocation_evidence(
        context,
        source_ref="/api/graph-governance/aming-claw/parallel-branches/allocate",
        route_identity={
            "route_id": "route-guided-{}".format(suffix),
            "route_context_hash": "sha256:" + ("b" * 64),
            "prompt_contract_id": "rprompt-guided-{}".format(suffix),
            "prompt_contract_hash": "sha256:" + ("c" * 64),
            "route_token_ref": "rtok-guided-{}".format(suffix),
            "visible_injection_manifest_hash": "sha256:" + ("d" * 64),
        },
    )
    return main, worktree, head, context, evidence


def _profiled_run(executable, *, role, suffix):
    from cli_agent_service.config import resolve_agent_config
    from cli_agent_service.models import (
        AgentProfile,
        CredentialRef,
        HarnessRuntime,
        InferenceEndpoint,
        LauncherAdapter,
        RolePolicy,
    )

    profile = AgentProfile(
        profile_id="profile-guided-{}".format(suffix),
        harness_runtime=HarnessRuntime(
            runtime_id="runtime-guided-{}".format(suffix),
            kind="codex_cli",
            executable_ref="path:{}".format(executable),
        ),
        inference_endpoint=InferenceEndpoint(
            endpoint_id="endpoint-guided-{}".format(suffix),
            provider="openai",
            model="gpt-5.4-codex",
            backend_mode="codex_cli",
            auth_mode="cli_auth",
        ),
        credential_ref=CredentialRef(
            ref_id="credential:codex-home:inherited",
            provider="openai",
            ref_kind="inherited_current",
        ),
        launcher_adapter=LauncherAdapter(
            launcher_id="launcher-guided-{}".format(suffix)
        ),
        role_policy=RolePolicy(
            policy_id="policy-guided-{}".format(suffix),
            roles=(role,),
            project_ids=("aming-claw",),
        ),
    )
    return resolve_agent_config(
        run_id="run-guided-{}".format(suffix),
        role=role,
        project_id="aming-claw",
        profile=profile,
        created_at="2026-07-14T00:00:00Z",
    )


def _canonical_ticket(
    run,
    *,
    role,
    suffix,
    worktree,
    runtime_context_id="",
    branch_ref="",
    base_commit="",
):
    from governance.contract_state_runtime import build_cli_agent_execution_ticket

    worker_id = "principal-guided-{}".format(suffix)
    action = {
        "id": "dispatch-guided-{}".format(suffix),
        "action": "dispatch_bounded_worker",
        "stage_id": "dispatch",
        "line_id": "dispatch-guided-{}".format(suffix),
        "evidence_kind": "dispatch_bounded_worker",
        "owner_role": "observer",
        "worker_role": role,
        "runtime_context_id": runtime_context_id
        or "mfrctx-guided-{}".format(suffix),
        "task_id": "task-guided-{}".format(suffix),
        "worker_id": worker_id,
        "worker_slot_id": worker_id,
        "observer_command_id": "task-guided-{}".format(suffix),
        "parent_task_id": "AC-GUIDED-E2E",
        "target_project_root": str(worktree),
        "branch_ref": branch_ref or "refs/heads/guided/{}".format(suffix),
        "base_commit": base_commit or "a" * 40,
        "target_head_commit": base_commit or "a" * 40,
        "merge_queue_id": "mq-guided-e2e",
        "route_id": "route-guided-{}".format(suffix),
        "route_context_hash": "sha256:" + ("b" * 64),
        "prompt_contract_id": "rprompt-guided-{}".format(suffix),
        "prompt_contract_hash": "sha256:" + ("c" * 64),
        "route_token_ref": "rtok-guided-{}".format(suffix),
        "visible_injection_manifest_hash": "sha256:" + ("d" * 64),
        "owned_files": ["agent/owned-{}.py".format(suffix)],
        "profile_requirements": {
            "profile_id": run.config.profile_id,
            "profile_kind": "governed",
            "role": role,
            "harness": "codex",
            "provider": "openai",
            "model": "gpt-5.4-codex",
            "independent_qa_required": role != "qa",
            "successor_budget": 1,
        },
        "retry_policy": {
            "attempt": 0,
            "max_attempts": 1,
            "successor_required": True,
        },
    }
    launch_identity = {
        "project_id": "aming-claw",
        "backlog_id": "AC-GUIDED-E2E",
        "task_id": action["task_id"],
        "worker_id": worker_id,
        "worker_slot_id": worker_id,
        "observer_command_id": action["observer_command_id"],
        "parent_task_id": action["parent_task_id"],
        "runtime_context_id": action["runtime_context_id"],
        "worker_role": role,
        "worktree_path": str(worktree),
        "branch_ref": action["branch_ref"],
        "base_commit": action["base_commit"],
        "target_head_commit": action["target_head_commit"],
        "merge_queue_id": action["merge_queue_id"],
        "owned_files": action["owned_files"],
        "route_id": action["route_id"],
        "route_context_hash": action["route_context_hash"],
        "prompt_contract_id": action["prompt_contract_id"],
        "prompt_contract_hash": action["prompt_contract_hash"],
        "route_token_ref": action["route_token_ref"],
        "visible_injection_manifest_hash": action[
            "visible_injection_manifest_hash"
        ],
    }
    authority = {
        "source_of_authority": "ContractRuntime",
        "authority_decision_source": "contract_runtime_completed_dispatch_line",
        "project_id": "aming-claw",
        "backlog_id": "AC-GUIDED-E2E",
        "contract_execution_id": "cex-guided-{}".format(suffix),
        "contract_revision_id": "revision-guided-{}".format(suffix),
        "execution_state_revision": 1,
        "execution_state_hash": "sha256:" + (suffix[0] * 64),
        "runtime_guide_hash": "sha256:" + ("e" * 64),
        "ticket_authority_status": "post_dispatch_pre_worker",
        "next_legal_action": action,
    }
    ticket = build_cli_agent_execution_ticket(
        contract_runtime_current_state=authority,
        launch_identity=launch_identity,
        expected_execution_state_revision=1,
    )
    assert ticket["status"] == "issued", ticket
    selectors = {
        "project_id": "aming-claw",
        "backlog_id": "AC-GUIDED-E2E",
        "contract_execution_id": authority["contract_execution_id"],
        "runtime_context_id": action["runtime_context_id"],
        "task_id": action["task_id"],
        "worker_id": worker_id,
        "worker_slot_id": worker_id,
        "observer_command_id": action["observer_command_id"],
        "role": role,
        "profile_id": run.config.profile_id,
        "principal_id": worker_id,
        "expected_execution_state_revision": 1,
        "expected_execution_state_hash": authority["execution_state_hash"],
        "expected_dispatch_identity_hash": ticket["dispatch_identity_hash"],
        "route_id": action["route_id"],
        "route_context_hash": action["route_context_hash"],
        "prompt_contract_id": action["prompt_contract_id"],
        "prompt_contract_hash": action["prompt_contract_hash"],
        "route_token_ref": action["route_token_ref"],
        "visible_injection_manifest_hash": action[
            "visible_injection_manifest_hash"
        ],
        "harness": "codex",
        "provider": "openai",
        "model": "gpt-5.4-codex",
        "backend_mode": "codex_cli",
    }
    return ticket, selectors


def _contract_runtime_authority(ticket):
    dispatch = ticket["dispatch_identity"]
    return {
        "source_of_authority": ticket["source_of_authority"],
        "authority_decision_source": ticket["authority_decision_source"],
        "project_id": dispatch["project_id"],
        "backlog_id": dispatch["backlog_id"],
        "contract_execution_id": ticket["contract_execution_id"],
        "contract_revision_id": ticket["contract_revision_id"],
        "execution_state_revision": ticket["execution_state_revision"],
        "execution_state_hash": ticket["execution_state_hash"],
        "runtime_guide_hash": ticket["runtime_guide_hash"],
        "readiness_state": "contract_active",
        "next_legal_action": ticket["next_legal_action"],
    }


def _fake_codex(path):
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib, json, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "output = pathlib.Path(args[args.index('-o') + 1])\n"
        "prompt = sys.stdin.read()\n"
        "with pathlib.Path('spawns.jsonl').open('a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({\n"
        "        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),\n"
        "        'spawned': True,\n"
        "    }, sort_keys=True) + '\\n')\n"
        "output.write_text('public-safe result', encoding='utf-8')\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


class _GovernanceFixture:
    def __init__(self, tickets):
        self.tickets = tickets
        self.ticket_requests = []
        self.receipts = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size).decode("utf-8"))
                if self.path.endswith("/cli-agent/execution-ticket/resolve"):
                    fixture.ticket_requests.append(body)
                    ticket = fixture.tickets[body["contract_execution_id"]]
                    response = {
                        "ok": True,
                        "status": "issued",
                        "execution_ticket": ticket,
                        "source_of_authority": "ContractRuntime",
                        "server_owned_authority_resolution": True,
                    }
                elif self.path.endswith("/cli-agent/run-receipts"):
                    fixture.receipts.append(body)
                    response = {
                        "ok": True,
                        "receipt_ingestion": {
                            "decision": "appended",
                            "idempotent": False,
                            "governance_authority": False,
                        },
                    }
                else:
                    self.send_error(404)
                    return
                encoded = json.dumps(response, sort_keys=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:{}".format(self.server.server_port)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def test_guided_runtime_uses_one_service_owned_spawn_for_l2_to_l3(
    tmp_path, monkeypatch
):
    import observer_runtime
    from ai_invocation import RoutePromptContract
    from cli_agent_service.service import CliAgentService, ServicePaths, request_service
    from observer_runtime import (
        DogfoodObserverPlanRequest,
        build_dogfood_observer_run_plan,
    )

    def fail_direct_invocation(_request):
        raise AssertionError("governed runtime must not invoke_ai directly")

    monkeypatch.setattr(observer_runtime, "invoke_ai", fail_direct_invocation)

    state_dir = tmp_path / "state"
    executable = _fake_codex(tmp_path / "codex")
    cases = (("mf_sub", "l3"),)
    fence_tokens = {
        suffix: "fence-{}-{}".format(suffix, secrets.token_urlsafe(24))
        for _role, suffix in cases
    }
    branches = {
        suffix: _dogfood_branch(
            tmp_path / suffix,
            suffix=suffix,
            fence_token=fence_tokens[suffix],
        )
        for _role, suffix in cases
    }
    runs = {
        suffix: _profiled_run(executable, role=role, suffix=suffix)
        for role, suffix in cases
    }
    admissions = {}
    for role, suffix in cases:
        _main, worktree, head, context, evidence = branches[suffix]
        admissions[suffix] = _canonical_ticket(
            runs[suffix],
            role=role,
            suffix=suffix,
            worktree=worktree,
            runtime_context_id=evidence["runtime_context_id"],
            branch_ref=context.branch_ref,
            base_commit=head,
        )
    governance = _GovernanceFixture(
        {ticket["contract_execution_id"]: ticket for ticket, _ in admissions.values()}
    )
    governance.start()
    monkeypatch.setenv("AMING_CLAW_GOVERNANCE_URL", governance.url)

    paths = ServicePaths.from_state_dir(state_dir)
    service = CliAgentService(paths)
    for run in runs.values():
        service.registry.register_profile(run.profile)
    service_thread = threading.Thread(target=service.serve_forever, daemon=True)
    service_thread.start()
    _wait_for(paths.socket_path.exists)
    private_memory = "PRIVATE-JB-MEMORY-{}".format(secrets.token_hex(16))
    raw_values = []
    responses = []
    try:
        for role, suffix in cases:
            main, worktree, head, context, evidence = branches[suffix]
            ticket, selectors = admissions[suffix]
            dispatch = ticket["dispatch_identity"]
            session_token = "session-{}-{}".format(
                suffix, secrets.token_urlsafe(24)
            )
            fence_token = fence_tokens[suffix]
            raw_values.extend((session_token, fence_token))
            monkeypatch.setenv("AMING_WORKER_SESSION_TOKEN", session_token)
            response = build_dogfood_observer_run_plan(
                DogfoodObserverPlanRequest(
                    project_id=selectors["project_id"],
                    backlog_id=selectors["backlog_id"],
                    route=RoutePromptContract(
                        route_id=dispatch["route_id"],
                        route_context_hash=dispatch["route_context_hash"],
                        prompt_contract_id=dispatch["prompt_contract_id"],
                        prompt_contract_hash=dispatch["prompt_contract_hash"],
                        route_token_ref=dispatch["route_token_ref"],
                        visible_injection_manifest_hash=dispatch[
                            "visible_injection_manifest_hash"
                        ],
                    ),
                    provider="openai",
                    backend_mode="codex_cli",
                    main_worktree=str(main),
                    workspace_root=str(tmp_path / suffix),
                    owned_files=("agent/owned-{}.py".format(suffix),),
                    task_id=selectors["task_id"],
                    worker_id=selectors["worker_id"],
                    allocation_owner="dogfood_observer",
                    branch_prefix="guided",
                    worktree_root=".worktrees",
                    merge_queue_id=context.merge_queue_id,
                    fence_token=fence_token,
                    graph_trace_ids=("gqt-guided-e2e-{}".format(suffix),),
                    branch_runtime_registration_ref=evidence["source_ref"],
                    branch_runtime_evidence=evidence,
                    runtime_context_id=evidence["runtime_context_id"],
                    base_commit=head,
                    target_head_commit=head,
                    prompt="public-safe intent role={}".format(role),
                    route_id=dispatch["route_id"],
                    visible_injection_manifest_hash=dispatch[
                        "visible_injection_manifest_hash"
                    ],
                    cli_agent_service_state_dir=str(state_dir),
                    contract_execution_id=ticket["contract_execution_id"],
                    contract_runtime_current_state=(
                        _contract_runtime_authority(ticket)
                    ),
                    expected_execution_state_revision=ticket[
                        "execution_state_revision"
                    ],
                    expected_execution_state_hash=ticket[
                        "execution_state_hash"
                    ],
                    expected_dispatch_identity_hash=ticket[
                        "dispatch_identity_hash"
                    ],
                    profile_requirements=ticket["profile_requirements"],
                    retry_policy=ticket["retry_policy"],
                ),
                execute=True,
            )
            responses.append(response)
            failure_context = {
                key: response.get(key)
                for key in (
                    "status",
                    "error",
                    "launch_backend_blocker",
                    "execute_preflight",
                    "dispatch_gate_validation",
                    "service_admission_blocker",
                    "service_dispatch_blocker",
                )
            }
            assert response["ok"] is True, json.dumps(
                failure_context,
                indent=2,
                sort_keys=True,
            )
            assert fence_token not in response["runtime_text"]["launch_text"]
            assert response["status"] == "started"
            observer_run = response["observer_run"]
            assert observer_run["status"] == "started"
            assert observer_run["one_hop_execution_gate"]["status"] == (
                "delegated_to_canonical_service_admission"
            )
            invocation = observer_run["invocation"]
            assert invocation["backend_mode"] == "cli_agent_service"
            service_dispatch = invocation["service_dispatch"]
            assert service_dispatch["run_id"] == "run-{}".format(
                ticket["ticket_id"]
            )
            assert service_dispatch["role"] == role
            assert service_dispatch["profile_id"] == selectors["profile_id"]
            assert service_dispatch["principal_id"] == selectors["principal_id"]
            assert service_dispatch["runtime_context_id"] == (
                selectors["runtime_context_id"]
            )
            assert service_dispatch["direct_invocation_fallback"] is False
            assert service_dispatch["caller_run_accepted"] is False
            assert service_dispatch["caller_prompt_accepted"] is False
            assert service_dispatch["caller_environment_accepted"] is False
            assert service.registry.get_run(runs[suffix].run_id) is None
            _wait_for(
                lambda run_id=service_dispatch["run_id"]: (
                    service.registry.get_run(run_id)
                    and service.registry.get_run(run_id).state == "completed"
                )
            )
            _wait_for(
                lambda run_id=service_dispatch["run_id"]: any(
                    item["receipt"]["run_id"] == run_id
                    and item["receipt"]["state"] == "completed"
                    for item in governance.receipts
                )
            )
            assert ticket["source_of_authority"] == "ContractRuntime"
    finally:
        if paths.socket_path.exists():
            request_service(paths, "stop")
        service_thread.join(timeout=5)
        governance.stop()

    assert service._restart_reconciled is True
    assert len(governance.ticket_requests) == len(cases)
    assert all("execution_ticket" not in request for request in governance.ticket_requests)
    assert all("profile_requirements" not in request for request in governance.ticket_requests)
    assert all("launch_identity" not in request for request in governance.ticket_requests)
    assert all("run" not in request for request in governance.ticket_requests)
    assert all("prompt" not in request for request in governance.ticket_requests)
    assert all("environment" not in request for request in governance.ticket_requests)
    assert all("host_envelope" not in request for request in governance.ticket_requests)

    spawn_records = []
    for _role, suffix in cases:
        worktree = branches[suffix][1]
        spawn_lines = (
            worktree / "spawns.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        assert len(spawn_lines) == 1
        spawn_records.append(json.loads(spawn_lines[0]))
    assert all(record["spawned"] is True for record in spawn_records)
    assert all(len(record["prompt_sha256"]) == 64 for record in spawn_records)

    with sqlite3.connect(state_dir / "registry" / "runs.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == len(
            cases
        )
        assert connection.execute("SELECT COUNT(*) FROM agent_leases").fetchone()[0] == len(
            cases
        )

    assert governance.receipts
    assert {item["receipt"]["state"] for item in governance.receipts} >= {
        "accepted",
        "started",
        "completed",
    }
    assert all(
        item["receipt"]["governance_authority"] is False
        and item["receipt"]["operational_state_only"] is True
        for item in governance.receipts
    )

    serialized_responses = json.dumps(responses, sort_keys=True)
    persisted = b"".join(
        path.read_bytes()
        for root in (state_dir, *(tmp_path / suffix for _role, suffix in cases))
        for path in root.rglob("*")
        if path.is_file()
    )
    for raw in (*raw_values, private_memory):
        assert raw not in serialized_responses
        assert raw.encode("utf-8") not in persisted
    service_persisted = b"".join(
        path.read_bytes() for path in state_dir.rglob("*") if path.is_file()
    )
    assert b"public-safe intent role=" not in service_persisted
    assert b"Proceed as the allocated Aming Claw" not in service_persisted


def test_restart_projects_lost_and_service_owns_selector_only_successor(
    tmp_path, monkeypatch
):
    from cli_agent_service.evidence import RunReceiptEmitter, hash_text
    from cli_agent_service.registry import AgentRegistry
    from cli_agent_service.service import CliAgentService, ServicePaths, request_service

    state_dir = tmp_path / "state"
    worktree = tmp_path / "worker"
    worktree.mkdir()
    executable = _fake_codex(worktree / "codex")
    paths = ServicePaths.from_state_dir(state_dir)
    registry = AgentRegistry(state_dir / "registry" / "runs.db")

    failed_run = _profiled_run(executable, role="observer", suffix="failed")
    failed_ticket, _ = _canonical_ticket(
        failed_run,
        role="observer",
        suffix="failed",
        worktree=worktree,
    )
    failed_dispatch = failed_ticket["dispatch_identity"]
    registry.register_run(
        failed_run,
        evidence_refs={
            "project_id": failed_dispatch["project_id"],
            "backlog_id": failed_dispatch["backlog_id"],
            "contract_execution_id": failed_ticket["contract_execution_id"],
            "runtime_context_id": failed_dispatch["runtime_context_id"],
            "task_id": failed_dispatch["task_id"],
        },
    )
    registry.acquire_lease(
        failed_run.run_id,
        "cli-agent-host-previous",
        ttl_seconds=1,
        now="2020-01-01T00:00:00Z",
    )
    registry.record_process_start(
        failed_run.run_id,
        pid=999999,
        process_start_identity="process-guided-failed",
        process_group_id=999999,
        argv_hash="sha256:" + ("1" * 64),
        now="2020-01-01T00:00:00Z",
    )
    seeded_receipts = []
    emitter = RunReceiptEmitter(
        run_id=failed_run.run_id,
        ticket_id=failed_ticket["ticket_id"],
        ticket_hash=failed_ticket["ticket_hash"],
        profile_id=failed_run.config.profile_id,
        runtime_context_id=failed_dispatch["runtime_context_id"],
        command_hash=hash_text("failed command"),
        sink=seeded_receipts.append,
    )
    emitter.emit("accepted", observed_at="2020-01-01T00:00:00Z")
    emitter.emit(
        "started",
        observed_at="2020-01-01T00:00:01Z",
        process_identity={
            "pid": 999999,
            "process_group_id": 999999,
            "process_start_identity_hash": hash_text("process-guided-failed"),
        },
    )
    from cli_agent_service.evidence import RunReceiptJournal

    journal = RunReceiptJournal(state_dir / "supervisor" / "run-receipts")
    for receipt in seeded_receipts:
        journal.append(receipt)

    successor_run = _profiled_run(
        executable,
        role="observer",
        suffix="successor",
    )
    successor_ticket, successor_selectors = _canonical_ticket(
        successor_run,
        role="observer",
        suffix="successor",
        worktree=worktree,
    )
    registry.register_profile(successor_run.profile)
    governance = _GovernanceFixture(
        {successor_ticket["contract_execution_id"]: successor_ticket}
    )
    governance.start()
    monkeypatch.setenv("AMING_CLAW_GOVERNANCE_URL", governance.url)
    service = CliAgentService(paths, registry=registry)
    service_thread = threading.Thread(target=service.serve_forever, daemon=True)
    service_thread.start()
    _wait_for(paths.socket_path.exists)
    _wait_for(
        lambda: any(
            item["receipt"]["state"] == "lost" for item in governance.receipts
        )
    )
    try:
        rejected = request_service(
            paths,
            "start_host_envelope_run",
            payload={
                "run": successor_run.to_public_dict(),
                "worktree": str(worktree),
                "prompt": "public-safe successor intent",
                "execution_ticket": successor_ticket,
            },
        )
        assert rejected["ok"] is False
        assert not (worktree / "spawns.jsonl").exists()

        response = request_service(
            paths,
            "start_host_envelope_run",
            payload={"authority_selectors": successor_selectors},
        )
        successor_run_id = "run-{}".format(successor_ticket["ticket_id"])
        assert response["ok"] is True
        assert response["status"] == "started"
        assert response["run_id"] == successor_run_id
        assert response["profile_id"] == successor_run.config.profile_id
        assert response["caller_run_accepted"] is False
        assert response["caller_prompt_accepted"] is False
        _wait_for(
            lambda: (
                registry.get_run(successor_run_id)
                and registry.get_run(successor_run_id).state == "completed"
            )
        )
    finally:
        if paths.socket_path.exists():
            request_service(paths, "stop")
        service_thread.join(timeout=5)
        governance.stop()

    assert registry.get_run(failed_run.run_id).state == "lost"
    assert service._restart_reconciled is True
    lost = [
        item["receipt"]
        for item in governance.receipts
        if item["receipt"]["run_id"] == failed_run.run_id
    ]
    assert [receipt["state"] for receipt in lost] == ["lost"]
    assert lost[0]["governance_authority"] is False
    assert lost[0]["operational_state_only"] is True
    assert len(governance.ticket_requests) == 1
    assert len((worktree / "spawns.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    persisted = b"".join(
        path.read_bytes() for path in state_dir.rglob("*") if path.is_file()
    )
    assert b"public-safe successor intent" not in persisted
