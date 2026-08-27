#!/bin/bash
# AC stable promotion. All evidence is projected from the live AC timeline
# before any Git/process write; legacy branch shortcuts are fail-closed.

set -euo pipefail
cd "$(dirname "$0")/.."

BOOTSTRAP_ANCHOR="a25838f15f949ac434cf78e03f20760e82ff81f0"
STABLE_BRANCH="codex/direct-no-pass-post-reconcile-r2"
DEV_BRANCH="codex/ac-dev"
STABLE_PORT="40000"
MANIFEST=""
DRY_RUN="false"

usage() { echo "Usage: $0 --promotion-manifest <manifest.json> [--dry-run]" >&2; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --promotion-manifest)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            MANIFEST="$2"; shift 2 ;;
        --dry-run) DRY_RUN="true"; shift ;;
        *)
            echo "Refusing legacy merge-and-deploy argument: $1" >&2
            usage; exit 2 ;;
    esac
done

if [ -z "$MANIFEST" ] || [ ! -f "$MANIFEST" ]; then
    echo "Promotion blocked: --promotion-manifest must name an existing file." >&2
    exit 2
fi
if [ -z "${SHARED_VOLUME_PATH:-}" ]; then
    echo "Promotion blocked: SHARED_VOLUME_PATH must identify the live AC volume." >&2
    exit 2
fi
if [ -z "${GOV_COORDINATOR_TOKEN:-}" ] && [ "$DRY_RUN" != "true" ]; then
    echo "Promotion blocked: GOV_COORDINATOR_TOKEN is required to record completion." >&2
    exit 2
fi

manifest_value() {
    python3 - "$MANIFEST" "$1" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for part in sys.argv[2].split("."):
    value = value[part]
print("true" if value is True else "false" if value is False else value)
PY
}

STABLE_WORKTREE="$(python3 - "$STABLE_BRANCH" <<'PY'
import subprocess, sys
raw = subprocess.check_output(["git", "worktree", "list", "--porcelain"], text=True)
expected = "refs/heads/" + sys.argv[1]
for block in raw.strip().split("\n\n"):
    values = dict(
        line.split(" ", 1) if " " in line else (line, "")
        for line in block.splitlines()
    )
    if values.get("branch") == expected:
        print(values.get("worktree", "")); break
PY
)"
if [ -z "$STABLE_WORKTREE" ] || [ ! -d "$STABLE_WORKTREE" ]; then
    echo "Promotion blocked: stable branch worktree is unavailable." >&2; exit 1
fi
CANONICAL_SHARED_VOLUME="$STABLE_WORKTREE/shared-volume"

CANDIDATE_COMMIT="$(manifest_value candidate_commit)"
MANIFEST_ANCHOR="$(manifest_value stable_anchor_commit)"
CURRENT_STABLE="$(git -C "$STABLE_WORKTREE" rev-parse HEAD)"

if [ "$(git branch --show-current)" != "$DEV_BRANCH" ]; then
    echo "Promotion blocked: only $DEV_BRANCH may be promoted." >&2; exit 1
fi
if [ "$(git rev-parse HEAD)" != "$CANDIDATE_COMMIT" ]; then
    echo "Promotion blocked: candidate_commit must equal the clean dev branch tip." >&2; exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "Promotion blocked: AC dev candidate worktree must be clean." >&2; exit 1
fi
if [ "$(git -C "$STABLE_WORKTREE" branch --show-current)" != "$STABLE_BRANCH" ]; then
    echo "Promotion blocked: stable worktree branch identity mismatch." >&2; exit 1
fi
if [ -n "$(git -C "$STABLE_WORKTREE" status --porcelain)" ]; then
    echo "Promotion blocked: stable branch worktree must be clean." >&2; exit 1
fi
if [ "$MANIFEST_ANCHOR" != "$CURRENT_STABLE" ]; then
    echo "Promotion blocked: manifest anchor is stale relative to current stable HEAD." >&2; exit 1
fi
if ! git merge-base --is-ancestor "$CURRENT_STABLE" "$CANDIDATE_COMMIT"; then
    echo "Promotion blocked: candidate is not a linear descendant of current stable." >&2; exit 1
fi

DIFF_SHA256="sha256:$(git diff --no-ext-diff --no-textconv --binary --full-index -M "$CURRENT_STABLE..$CANDIDATE_COMMIT" -- . | shasum -a 256 | awk '{print $1}')"
VERIFIER_SHA256="sha256:$(shasum -a 256 "$0" | awk '{print $1}')"
LIVE_DB="$(python3 - "$STABLE_WORKTREE" "${SHARED_VOLUME_PATH}" <<'PY'
from pathlib import Path
import sys
stable = Path(sys.argv[1]).expanduser().resolve(strict=True)
requested_input = Path(sys.argv[2]).expanduser().absolute()
if requested_input.is_symlink():
    raise SystemExit("Promotion blocked: alternate SHARED_VOLUME_PATH is forbidden")
requested = requested_input.resolve(strict=True)
root = (stable / "shared-volume").absolute()
if root.is_symlink() or root.resolve(strict=True) != root or requested != root:
    raise SystemExit("Promotion blocked: alternate SHARED_VOLUME_PATH is forbidden")
path = root / "codex-tasks" / "state" / "governance" / "aming-claw" / "governance.db"
if path.is_symlink() or path.parent.is_symlink():
    raise SystemExit("Promotion blocked: live AC database cannot be a symlink")
resolved = path.resolve(strict=True)
if resolved != path.absolute() or not path.is_file():
    raise SystemExit("Promotion blocked: live AC database escaped its canonical path")
print(path.absolute())
PY
)"
STABLE_DATABASE_IDENTITY="$(python3 - "$LIVE_DB" <<'PY'
import hashlib, json, os, stat, sys
from pathlib import Path
path = Path(sys.argv[1]).absolute()
metadata = path.stat(follow_symlinks=False)
if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or path.resolve(strict=True) != path:
    raise SystemExit("Promotion blocked: canonical AC database identity is invalid")
relative = "shared-volume/codex-tasks/state/governance/aming-claw/governance.db"
print(json.dumps({
    "schema_version": "ac_stable_database_identity.v1",
    "device": int(metadata.st_dev),
    "inode": int(metadata.st_ino),
    "stable_relative_path_sha256": "sha256:" + hashlib.sha256(relative.encode()).hexdigest(),
}, sort_keys=True, separators=(",", ":")))
PY
)"

# Read-only projector: the manifest cannot self-declare PASS. One canonical,
# role-bound QA verdict owns every QA sub-result.  A separate append-only
# stable-plane operator audit event must already exist; this script never
# creates that signoff. No migration or DDL is executed.
promotion_precheck() {
python3 - "$MANIFEST" "$LIVE_DB" "$CURRENT_STABLE" "$CANDIDATE_COMMIT" "$DIFF_SHA256" "$VERIFIER_SHA256" "$BOOTSTRAP_ANCHOR" "$STABLE_DATABASE_IDENTITY" <<'PY'
import datetime as dt, hashlib, json, os, re, sqlite3, stat, subprocess, sys
from pathlib import Path

manifest_path, db_raw, stable, candidate, diff_hash, verifier_hash, bootstrap, database_identity_raw = sys.argv[1:]
manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
database_identity = json.loads(database_identity_raw)

def fail(message): raise SystemExit("Promotion blocked: " + message)
def sha(value):
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
def exact_sha(value): return bool(re.fullmatch(r"sha256:[0-9a-f]{64}", str(value or "")))
def operator_principal(value):
    return bool(
        isinstance(value, str) and value and value == value.strip()
        and len(value) <= 256 and not value.endswith(":route_ref")
    )
def deep_values(value, key):
    if isinstance(value, dict):
        for name, item in value.items():
            if name == key: yield item
            yield from deep_values(item, key)
    elif isinstance(value, list):
        for item in value: yield from deep_values(item, key)
def contains(value, key, expected): return any(item == expected for item in deep_values(value, key))
def safe_fence(value):
    return bool(
        isinstance(value, list) and value and len(set(value)) == len(value)
        and all(
            isinstance(item, str) and item.strip() == item and item
            and not Path(item).is_absolute() and ".." not in Path(item).parts
            for item in value
        )
    )

required_top = {
    "schema_version", "project_id", "backlog_id", "contract_execution_id",
    "stable_anchor_commit", "stable_branch", "branch", "candidate_commit",
    "file_fence", "diff_sha256", "promotion_intent_sha256", "gates", "deploy",
    "prior_promotion", "promotion_manifest_sha256", "stable_database_identity",
}
if manifest.get("schema_version") != "ac_stable_promotion_manifest.v1": fail("schema_version mismatch")
if set(manifest) != required_top: fail("manifest has missing or extra fields")
if manifest.get("project_id") != "aming-claw": fail("project_id must be aming-claw")
if manifest.get("stable_branch") != "codex/direct-no-pass-post-reconcile-r2": fail("stable_branch mismatch")
if manifest.get("branch") != "codex/ac-dev": fail("candidate branch mismatch")
if manifest.get("stable_anchor_commit") != stable: fail("stable anchor mismatch")
if manifest.get("candidate_commit") != candidate: fail("candidate commit mismatch")
if manifest.get("diff_sha256") != diff_hash or not exact_sha(diff_hash): fail("binary diff hash mismatch")
if manifest.get("stable_database_identity") != database_identity:
    fail("stable database identity mismatch")
fence = manifest.get("file_fence")
if not safe_fence(fence): fail("file_fence must be a non-empty exact safe relative list")
actual_files = subprocess.check_output(["git", "diff", "--name-only", stable, candidate], text=True).splitlines()
if set(actual_files) != set(fence) or len(actual_files) != len(fence): fail("actual files differ from file_fence")

intent = {key: manifest[key] for key in (
    "schema_version", "project_id", "backlog_id", "contract_execution_id",
    "stable_anchor_commit", "stable_branch", "branch", "candidate_commit",
    "file_fence", "diff_sha256", "deploy", "stable_database_identity",
)}
intent_hash = sha(intent)
if manifest.get("promotion_intent_sha256") != intent_hash: fail("promotion intent digest mismatch")
deploy = manifest.get("deploy")
if deploy != {"authorized": True, "mode": "host_supervisor", "stable_port": 40000}:
    fail("deploy identity mismatch")
gates = manifest.get("gates")
if not isinstance(gates, dict) or set(gates) != {"qa_verdict", "operator_signoff"}:
    fail("exact qa_verdict and operator_signoff gates required")
qa_gate = gates.get("qa_verdict")
operator_gate = gates.get("operator_signoff")
if not isinstance(qa_gate, dict) or set(qa_gate) != {"timeline_event_id", "status"} or qa_gate.get("status") != "passed":
    fail("qa_verdict gate shape/status mismatch")
if not isinstance(operator_gate, dict) or set(operator_gate) != {"status", "nonce", "operator_principal_id", "expires_at", "queue_event_id"} or operator_gate.get("status") != "approved":
    fail("operator_signoff gate shape/status mismatch")
signable_operator = {
    key: operator_gate.get(key)
    for key in ("status", "nonce", "operator_principal_id", "expires_at")
}
signable_manifest = {
    **intent,
    "promotion_intent_sha256": intent_hash,
    "prior_promotion": manifest.get("prior_promotion"),
    "gates": {
        "qa_verdict": dict(qa_gate),
        "operator_signoff": signable_operator,
    },
}
manifest_hash = sha(signable_manifest)
if manifest.get("promotion_manifest_sha256") != manifest_hash:
    fail("promotion manifest digest mismatch")

db_path = Path(db_raw).absolute()
before = db_path.stat(follow_symlinks=False)
if db_path.is_symlink() or not stat.S_ISREG(before.st_mode) or db_path.resolve(strict=True) != db_path:
    fail("live DB must be the canonical non-symlink AC database")
if (int(before.st_dev), int(before.st_ino)) != (
    int(database_identity["device"]), int(database_identity["inode"])
): fail("live DB physical identity differs from promotion manifest")
conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA query_only=ON")
try:
    opened = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve(strict=True)
    after = db_path.stat(follow_symlinks=False)
    if opened != db_path or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or db_path.is_symlink():
        fail("live DB identity changed during read-only open")
    schema_row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if schema_row is None or int(schema_row[0]) != 47: fail("live DB schema is not exact version 47")
    def event(event_id):
        if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 1:
            fail("every gate requires a positive timeline_event_id")
        row = conn.execute(
            "SELECT * FROM task_timeline_events WHERE id=? AND project_id='aming-claw'", (event_id,)
        ).fetchone()
        if row is None: fail(f"timeline event {event_id} is missing")
        result = dict(row)
        for key in ("payload_json", "verification_json", "artifact_refs_json"):
            try: result[key[:-5]] = json.loads(result.get(key) or "{}")
            except Exception: fail(f"timeline event {event_id} has invalid {key}")
        return result

    qa_row = event(qa_gate.get("timeline_event_id"))
    if qa_row.get("backlog_id") != manifest["backlog_id"]: fail("QA backlog mismatch")
    if qa_row.get("task_id") != manifest["contract_execution_id"]: fail("QA CEX mismatch")
    if qa_row.get("commit_sha") != candidate: fail("QA candidate mismatch")
    if qa_row.get("event_type") != "qa.independent_verification": fail("QA event_type mismatch")
    if qa_row.get("event_kind") != "independent_verification": fail("QA event_kind mismatch")
    if qa_row.get("phase") != "qa": fail("QA stage mismatch")
    if str(qa_row.get("status") or "").lower() not in {"passed", "pass"}: fail("QA verdict is not PASS")
    qa_evidence = {
        "payload": qa_row["payload"],
        "verification": qa_row["verification"],
        "artifact_refs": qa_row["artifact_refs"],
    }
    authority = next((value for value in deep_values(qa_evidence, "source_backed_contract_gate_authority") if isinstance(value, dict)), {})
    proof = authority.get("qa_session_proof") if isinstance(authority.get("qa_session_proof"), dict) else {}
    if not (
        authority.get("schema_version") == "source_backed_contract_gate_authority.v1"
        and authority.get("source") == "server_qa_session_verification"
        and authority.get("source_of_authority") == "qa_session_verification"
        and authority.get("authority_hash") == sha({key: value for key, value in authority.items() if key != "authority_hash"})
        and proof.get("schema_version") == "qa_session_scope_proof.v1"
        and proof.get("source") == "authenticated_qa_session"
        and proof.get("role") == "qa" and proof.get("verified") is True
        and proof.get("observer_impersonation") is False
        and str(proof.get("evidence_status") or "").lower() in {"accepted", "ok", "pass", "passed", "succeeded", "success"}
        and proof.get("close_satisfying") is True
        and proof.get("audit_only") is False
        and proof.get("passing_status_required_for_close") is True
        and proof.get("authority_scope") == "close_satisfying"
        and authority.get("authority_scope") == "close_satisfying"
        and authority.get("close_satisfying") is True
        and authority.get("audit_only") is False
        and proof.get("db_verified_graph_trace") is True
        and proof.get("query_source") == "qa"
        and proof.get("query_purpose") == "independent_verification"
        and proof.get("project_id") == "aming-claw"
        and proof.get("backlog_id") == manifest["backlog_id"]
        and proof.get("task_id") == manifest["contract_execution_id"]
        and proof.get("commit_sha") == candidate
        and proof.get("snapshot_commit_sha") == candidate
        and proof.get("principal_id") == qa_row.get("actor")
    ): fail("QA verdict lacks role-bound server authority")
    required_proof = ("principal_id", "qa_session_id", "qa_scope_binding_ref", "snapshot_id")
    if any(not isinstance(proof.get(key), str) or not proof.get(key) for key in required_proof):
        fail("QA verdict role-bound proof is incomplete")
    trace_ids = proof.get("graph_trace_ids")
    if not isinstance(trace_ids, list) or not trace_ids or len(trace_ids) != len(set(trace_ids)) or any(not isinstance(item, str) or not item for item in trace_ids):
        fail("QA verdict graph trace authority is incomplete")
    session = conn.execute(
        "SELECT principal_id, project_id, role, scope_json, status FROM sessions WHERE session_id=?",
        (proof["qa_session_id"],),
    ).fetchone()
    if session is None: fail("QA role-bound session is missing")
    try: session_scope = json.loads(session["scope_json"] or "[]")
    except Exception: fail("QA role-bound session scope is malformed")
    if not (
        session["principal_id"] == proof["principal_id"]
        and session["project_id"] == proof["project_id"]
        and session["role"] == "qa" and session["status"] == "active"
        and isinstance(session_scope, list)
        and proof["qa_scope_binding_ref"] in session_scope
    ): fail("QA role-bound session identity mismatch")
    placeholders = ",".join("?" for _ in trace_ids)
    trace_rows = conn.execute(
        f"""SELECT t.trace_id, t.project_id, t.snapshot_id, t.actor,
                   t.query_source, t.query_purpose, t.task_id, t.backlog_id,
                   t.commit_sha, t.qa_session_id, t.qa_scope_binding_ref,
                   t.status, s.commit_sha AS snapshot_commit_sha
            FROM graph_query_traces t
            JOIN graph_snapshots s
              ON s.project_id=t.project_id AND s.snapshot_id=t.snapshot_id
            WHERE t.project_id=? AND t.trace_id IN ({placeholders})""",
        (proof["project_id"], *trace_ids),
    ).fetchall()
    traces = {row["trace_id"]: row for row in trace_rows}
    trace_expected = {
        "project_id": proof["project_id"], "snapshot_id": proof["snapshot_id"],
        "actor": proof["principal_id"], "query_source": "qa",
        "query_purpose": "independent_verification", "task_id": proof["task_id"],
        "backlog_id": proof["backlog_id"], "commit_sha": proof["commit_sha"],
        "qa_session_id": proof["qa_session_id"],
        "qa_scope_binding_ref": proof["qa_scope_binding_ref"], "status": "complete",
        "snapshot_commit_sha": proof["snapshot_commit_sha"],
    }
    if not all(
        trace_id in traces
        and all(str(traces[trace_id][key] or "") == expected for key, expected in trace_expected.items())
        for trace_id in trace_ids
    ): fail("QA graph trace authority mismatch")
    canonical_line = next((value for value in deep_values(qa_evidence, "contract_runtime_canonical_line") if isinstance(value, dict)), {})
    if not (
        canonical_line.get("stage_id") == "qa"
        and canonical_line.get("line_id") == "qa_independent_verification"
        and canonical_line.get("contract_execution_id") == manifest["contract_execution_id"]
        and exact_sha(canonical_line.get("runtime_guide_hash"))
    ): fail("QA verdict lacks exact ContractRuntime line identity")
    review = proof.get("candidate_review_context") if isinstance(proof.get("candidate_review_context"), dict) else {}
    if not review:
        review = next((value for value in deep_values(qa_evidence, "candidate_review_context") if isinstance(value, dict)), {})
    if not (
        review.get("candidate_commit_sha") == candidate
        and review.get("comparison_base_commit_sha") == stable
        and review.get("comparison_authority_required") is True
        and review.get("candidate_diff_hash") == diff_hash
        and review.get("changed_files") == fence
    ): fail("QA exact candidate/parent/fence/diff authority mismatch")
    if not (
        contains(qa_evidence, "stable_anchor_commit", stable)
        and contains(qa_evidence, "promotion_intent_sha256", intent_hash)
        and any(item == fence for item in deep_values(qa_evidence, "file_fence"))
        and contains(qa_evidence, "stable_database_identity", database_identity)
    ): fail("QA verdict lacks exact stable/fence/intent binding")
    if any(item is True for item in deep_values(qa_evidence, "pass_synthesized")):
        fail("QA verdict synthesized PASS")
    promotion_results = next((value for value in deep_values(qa_evidence, "promotion_gate_results") if isinstance(value, dict)), {})
    branch = promotion_results.get("branch_service") if isinstance(promotion_results.get("branch_service"), dict) else {}
    if not (
        branch.get("test_id") and branch.get("status") == "passed"
        and exact_sha(branch.get("report_sha256"))
        and branch.get("runtime_plane") == "dev" and branch.get("port") == 40008
        and branch.get("bind_host") == "127.0.0.1"
    ): fail("branch-service QA subresult is incomplete")
    lanes = promotion_results.get("lanes") if isinstance(promotion_results.get("lanes"), dict) else {}
    if set(lanes) != {"direct_main", "mf_parallel", "mf_batch_parallel"}:
        fail("three-lane QA subresults are incomplete")
    for lane_name, lane in lanes.items():
        if not isinstance(lane, dict) or not (
            lane.get("test_id") and lane.get("status") == "passed"
            and exact_sha(lane.get("report_sha256"))
        ): fail(f"{lane_name} QA subresult is incomplete")
    same_authority = []
    for row in conn.execute(
        "SELECT * FROM task_timeline_events WHERE project_id=?",
        ("aming-claw",),
    ).fetchall():
        evidence = {}
        for key in ("payload_json", "verification_json", "artifact_refs_json"):
            try: evidence[key[:-5]] = json.loads(row[key] or "{}")
            except Exception: fail("QA replay candidate evidence is malformed")
        if contains(evidence, "authority_hash", authority["authority_hash"]):
            same_authority.append(dict(row))
    if len(same_authority) != 1 or int(same_authority[0]["id"]) != int(qa_row["id"]):
        fail("QA authority replay/ambiguity detected")

    queue_event_id = operator_gate.get("queue_event_id")
    if not isinstance(queue_event_id, int) or isinstance(queue_event_id, bool) or queue_event_id < 1:
        fail("operator signoff requires a positive queue_event_id")
    signoff_row = conn.execute(
        "SELECT * FROM release_operator_head_queue_events WHERE id=? AND project_id='aming-claw'",
        (queue_event_id,),
    ).fetchone()
    if signoff_row is None: fail("operator signoff event is missing")
    signoff_row = dict(signoff_row)
    try:
        before = json.loads(signoff_row.get("before_json") or "{}")
        after = json.loads(signoff_row.get("after_json") or "{}")
        signoff = json.loads(signoff_row.get("reason") or "")
    except Exception: fail("operator signoff event is not canonical JSON")
    if not (
        operator_principal(operator_gate.get("operator_principal_id"))
        and operator_principal(signoff_row.get("actor"))
        and signoff_row.get("action") == "reorder" and signoff_row.get("backlog_id") == ""
        and before == after and signoff_row.get("actor") == operator_gate.get("operator_principal_id")
    ): fail("operator signoff is not an authenticated stable no-op queue decision")
    expected_signoff = {
        "schema_version": "ac_stable_promotion_operator_signoff.v1",
        "nonce": operator_gate.get("nonce"),
        "operator_principal_id": operator_gate.get("operator_principal_id"),
        "expires_at": operator_gate.get("expires_at"),
        "project_id": "aming-claw",
        "backlog_id": manifest["backlog_id"],
        "contract_execution_id": manifest["contract_execution_id"],
        "stable_anchor_commit": stable,
        "candidate_commit": candidate,
        "promotion_intent_sha256": intent_hash,
        "promotion_manifest_sha256": manifest_hash,
        "verifier_sha256": verifier_hash,
        "diff_sha256": diff_hash,
        "file_fence": fence,
        "deploy": deploy,
        "stable_database_identity": database_identity,
    }
    if signoff != expected_signoff or signoff_row.get("reason") != json.dumps(signoff, sort_keys=True, separators=(",", ":")):
        fail("operator signoff canonical body mismatch")
    if not re.fullmatch(r"[0-9a-f]{32}", str(signoff.get("nonce") or "")):
        fail("operator signoff nonce is malformed")
    try:
        created = dt.datetime.fromisoformat(str(signoff_row["created_at"]).replace("Z", "+00:00"))
        expires = dt.datetime.fromisoformat(str(signoff["expires_at"]).replace("Z", "+00:00"))
    except ValueError: fail("operator signoff timestamps are invalid")
    now = dt.datetime.now(dt.timezone.utc)
    if created.tzinfo is None: created = created.replace(tzinfo=dt.timezone.utc)
    if expires.tzinfo is None: expires = expires.replace(tzinfo=dt.timezone.utc)
    if expires <= created or expires - created > dt.timedelta(hours=1) or now >= expires:
        fail("operator signoff is expired or exceeds the one-hour window")
    nonce_matches = []
    for row in conn.execute(
        "SELECT id, reason FROM release_operator_head_queue_events WHERE project_id='aming-claw' AND action='reorder'"
    ).fetchall():
        try: candidate_reason = json.loads(row["reason"] or "")
        except Exception: continue
        if candidate_reason.get("schema_version") == "ac_stable_promotion_operator_signoff.v1" and candidate_reason.get("nonce") == signoff["nonce"]:
            nonce_matches.append(int(row["id"]))
    if sorted(nonce_matches) != [queue_event_id]: fail("operator signoff nonce replay/ambiguity detected")
    approval_ref = f"release-operator-head-queue-event:{queue_event_id}"

    prior = manifest.get("prior_promotion"); previous_receipt = ""; prior_event_id = 0
    if stable == bootstrap:
        if prior != {"kind": "bootstrap", "stable_commit": bootstrap}: fail("exact bootstrap receipt required")
    else:
        if not isinstance(prior, dict) or prior.get("kind") != "timeline_receipt": fail("successor prior receipt required")
        previous_receipt = str(prior.get("receipt_hash") or "")
        if not exact_sha(previous_receipt): fail("prior receipt hash malformed")
        prior_event_id = prior.get("timeline_event_id")
        prior_row = event(prior_event_id)
        if prior_row.get("event_type") != "ac.stable_promotion_completed": fail("prior event type mismatch")
        if prior_row.get("commit_sha") != stable: fail("prior event not bound to current stable")
        if not contains(prior_row, "promoted_commit", stable): fail("prior promoted commit mismatch")
        if not contains(prior_row, "promotion_receipt_hash", previous_receipt): fail("prior receipt not durable")
        if not contains(prior_row, "stable_database_identity", database_identity):
            fail("prior receipt stable database identity mismatch")

    completion_rows = conn.execute(
        "SELECT * FROM task_timeline_events WHERE project_id='aming-claw' AND event_type='ac.stable_promotion_completed' ORDER BY id"
    ).fetchall()
    completion_events = []
    for row in completion_rows:
        item = dict(row)
        try: item["payload"] = json.loads(item.get("payload_json") or "{}")
        except Exception: fail("promotion completion chain contains malformed evidence")
        completion_events.append(item)
    if any(item.get("commit_sha") == candidate for item in completion_events):
        fail("candidate already has a promotion completion receipt")
    if any(
        isinstance(item.get("payload"), dict)
        and item["payload"].get("previous_stable_commit") == stable
        and item["payload"].get("promoted_commit") != candidate
        for item in completion_events
    ):
        fail("stable promotion chain already has a different successor")
    if stable != bootstrap:
        matching_prior = [
            item for item in completion_events
            if item.get("commit_sha") == stable
            and item.get("payload", {}).get("promoted_commit") == stable
            and item.get("payload", {}).get("promotion_receipt_hash") == previous_receipt
            and item.get("payload", {}).get("stable_database_identity") == database_identity
        ]
        if len(matching_prior) != 1 or int(matching_prior[0].get("id") or 0) != prior_event_id:
            fail("prior promotion receipt is missing or ambiguous")

    evidence_hashes = {
        "qa_verdict": sha({key: qa_row.get(key) for key in (
            "id", "project_id", "backlog_id", "task_id", "event_type", "phase",
            "event_kind", "actor", "status", "payload", "verification", "artifact_refs", "commit_sha",
        )}),
        "operator_signoff": sha({key: signoff_row.get(key) for key in (
            "id", "project_id", "action", "backlog_id", "actor", "reason",
            "before_json", "after_json", "created_at",
        )}),
    }
finally: conn.close()

body = {
    "schema_version": "ac_stable_promotion_precheck_receipt.v1",
    "verifier_version": "readonly_timeline_projector.v1",
    "verifier_sha256": verifier_hash,
    "promotion_intent_sha256": intent_hash,
    "promotion_manifest_sha256": manifest_hash,
    "stable_anchor_commit": stable,
    "candidate_commit": candidate,
    "diff_sha256": diff_hash,
    "stable_database_identity": database_identity,
    "previous_promotion_receipt_hash": previous_receipt,
    "prior_promotion_event_id": prior_event_id,
    "gate_event_ids": {
        "qa_verdict": qa_row["id"],
        "operator_signoff": queue_event_id,
    },
    "operator_approval_ref": approval_ref,
    "gate_evidence_hashes": evidence_hashes,
    "pass_synthesized": False,
    "writes_performed": False,
}
print(json.dumps({**body, "receipt_hash": sha(body)}, sort_keys=True))
PY
}
PRECHECK_RECEIPT="$(promotion_precheck)"
PRECHECK_RECEIPT_HASH="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["receipt_hash"])' <<<"$PRECHECK_RECEIPT")"

# Fallible source checks and stable-anchor health all happen before mutation.
python3 -m py_compile agent/cli.py agent/governance/db.py agent/governance/server.py
bash -n scripts/merge-and-deploy.sh
OLD_PID="$(python3 - "$CURRENT_STABLE" "$STABLE_PORT" "$STABLE_DATABASE_IDENTITY" <<'PY'
import json, sys, urllib.request
anchor, port, database_identity_raw = sys.argv[1], int(sys.argv[2]), sys.argv[3]
database_identity = json.loads(database_identity_raw)
with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
    health = json.load(response)
plane_identity = health.get("runtime_plane_identity") or {}
health_database_identity = plane_identity.get("stable_database_identity")
database_identity_ok = (
    health_database_identity == database_identity
    if health_database_identity is not None
    else anchor == "a25838f15f949ac434cf78e03f20760e82ff81f0"
)
if not (
    health.get("status") == "ok" and health.get("service") == "governance"
    and health.get("port") == port and health.get("runtime_loaded_version") == anchor
    and health.get("runtime_stale") is False
    and database_identity_ok
): raise SystemExit("Promotion blocked: stable health is not exact anchor")
print(int(health.get("pid") or 0))
PY
)"
COMMON_GIT_DIR="$(git -C "$STABLE_WORKTREE" rev-parse --git-common-dir)"
if [[ "$COMMON_GIT_DIR" = /* ]]; then
    REPO_ROOT="$(cd "$COMMON_GIT_DIR/.." && pwd)"
else
    REPO_ROOT="$(cd "$STABLE_WORKTREE/$COMMON_GIT_DIR/.." && pwd)"
fi
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "Promotion blocked: repository Python runtime unavailable at $PYTHON_BIN." >&2; exit 1
fi

if [ "$DRY_RUN" = "true" ]; then
    echo "Promotion preflight passed; receipt=$PRECHECK_RECEIPT_HASH; no mutation performed."
    exit 0
fi
if [ "$OLD_PID" -le 1 ]; then
    echo "Promotion blocked: stable process PID is not safely identifiable." >&2; exit 1
fi

# Re-project every durable gate at the last possible pre-mutation point.  This
# catches signoff expiry, nonce replay, DB replacement, QA replay, or promotion
# chain changes that occur while source/health checks are running.
FINAL_PRECHECK_RECEIPT="$(promotion_precheck)"
if [ "$FINAL_PRECHECK_RECEIPT" != "$PRECHECK_RECEIPT" ]; then
    echo "Promotion blocked: durable promotion evidence changed after precheck." >&2
    exit 1
fi

# Final compare-and-swap guard after every fallible precheck and immediately
# before the first stable Git mutation. A concurrent promotion must restart
# from a fresh manifest; it is never merged on top implicitly.
PRE_MERGE_STABLE="$(git -C "$STABLE_WORKTREE" rev-parse HEAD)"
if [ "$PRE_MERGE_STABLE" != "$CURRENT_STABLE" ] \
    || [ "$(git -C "$STABLE_WORKTREE" branch --show-current)" != "$STABLE_BRANCH" ] \
    || [ -n "$(git -C "$STABLE_WORKTREE" status --porcelain)" ]; then
    echo "Promotion blocked: stable HEAD/branch/worktree changed after precheck." >&2
    exit 1
fi

# No rebase/fallback merge/branch deletion/DB copy. The stable ref advances to
# the exact already-reviewed commit, never to a synthetic merge commit.
git -C "$STABLE_WORKTREE" merge --ff-only "$CANDIDATE_COMMIT"
PROMOTED_COMMIT="$(git -C "$STABLE_WORKTREE" rev-parse HEAD)"
if [ "$PROMOTED_COMMIT" != "$CANDIDATE_COMMIT" ]; then
    echo "Promotion failed: stable HEAD is not exact candidate." >&2; exit 1
fi

kill -TERM "$OLD_PID"
for _attempt in $(seq 1 50); do
    if ! kill -0 "$OLD_PID" 2>/dev/null; then break; fi
    sleep 0.2
done
if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Promotion failed: old stable process did not stop; no force-kill attempted." >&2; exit 1
fi

RUNTIME_LOG="${TMPDIR:-/tmp}/aming-claw-stable-${PROMOTED_COMMIT}.log"
(
    cd "$STABLE_WORKTREE"
    PYTHONPATH="$STABLE_WORKTREE" nohup "$PYTHON_BIN" -m agent.cli start \
        --runtime-plane stable --port "$STABLE_PORT" \
        --stable-anchor-commit "$PROMOTED_COMMIT" \
        --workspace "$STABLE_WORKTREE" \
        --shared-volume-path "$CANONICAL_SHARED_VOLUME" >"$RUNTIME_LOG" 2>&1 &
)

POST_HEALTH=""
for _attempt in $(seq 1 50); do
    POST_HEALTH="$(curl -sf "http://127.0.0.1:${STABLE_PORT}/api/health" 2>/dev/null || true)"
    if [ -n "$POST_HEALTH" ]; then break; fi
    sleep 0.2
done
python3 -c '
import json,sys
commit,branch,port,db_raw=sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4]; h=json.load(sys.stdin); i=h.get("runtime_plane_identity") or {}; db=json.loads(db_raw)
ok=(h.get("status")=="ok" and h.get("service")=="governance" and h.get("port")==port and h.get("runtime_plane")=="stable" and h.get("runtime_loaded_version")==commit and h.get("runtime_stale") is False and i.get("status")=="ready" and i.get("branch")==branch and i.get("commit")==commit and i.get("stable_anchor_commit")==commit and i.get("stable_database_identity")==db)
raise SystemExit(0 if ok else "Promotion failed: deployed identity is not exact candidate")
' "$PROMOTED_COMMIT" "$STABLE_BRANCH" "$STABLE_PORT" "$STABLE_DATABASE_IDENTITY" <<<"$POST_HEALTH"

COMPLETION="$(python3 - "$MANIFEST" "$PRECHECK_RECEIPT" "$POST_HEALTH" "$VERIFIER_SHA256" <<'PY'
import json, os, sys, urllib.request
m=json.load(open(sys.argv[1], encoding="utf-8")); pre=json.loads(sys.argv[2]); health=json.loads(sys.argv[3])
body={
 "project_id":"aming-claw", "backlog_id":m["backlog_id"],
 "contract_execution_id":m["contract_execution_id"], "candidate_commit":m["candidate_commit"],
 "previous_stable_commit":m["stable_anchor_commit"], "promotion_intent_sha256":m["promotion_intent_sha256"],
 "promotion_manifest_sha256":m["promotion_manifest_sha256"],
 "precheck_receipt_hash":pre["receipt_hash"], "verifier_sha256":sys.argv[4],
 "precheck_receipt":pre, "promotion_manifest":m,
 "previous_promotion_receipt_hash":pre.get("previous_promotion_receipt_hash", ""),
 "diff_sha256":m["diff_sha256"], "file_fence":m["file_fence"],
 "deploy":m["deploy"],
 "stable_database_identity":m["stable_database_identity"],
 "operator_approval_ref":pre["operator_approval_ref"],
 "health_identity":{"runtime_loaded_version":health["runtime_loaded_version"],"runtime_plane_identity":health["runtime_plane_identity"],"runtime_stale":health["runtime_stale"]},
}
req=urllib.request.Request("http://127.0.0.1:40000/api/projects/aming-claw/ac-stable-promotion/complete",data=json.dumps(body,sort_keys=True).encode(),headers={"Content-Type":"application/json","X-Gov-Token":os.environ["GOV_COORDINATOR_TOKEN"]},method="POST")
with urllib.request.urlopen(req,timeout=10) as response: result=json.load(response)
if not result.get("ok") or not result.get("promotion_receipt_hash"): raise SystemExit("completion receipt not persisted")
print(json.dumps(result,sort_keys=True))
PY
)"
echo "AC stable promotion complete: $COMPLETION"
