from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import json
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dashboard_asset_identity.py"
PROMOTION = SCRIPT.with_name("merge-and-deploy.sh")


def _run(root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), mode, "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def dashboard(tmp_path: Path) -> Path:
    source = tmp_path / "frontend" / "dashboard"
    source.mkdir(parents=True)
    for name in ("package.json", "package-lock.json", "index.html"):
        (source / name).write_text(name, encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "main.tsx").write_text("export const value = 1;\n", encoding="utf-8")
    package = tmp_path / "agent" / "governance" / "dashboard_dist"
    (package / "assets").mkdir(parents=True)
    js = b"console.log('dashboard');\n"
    css = b"body { color: blue; }\n"
    (package / "assets" / "index.js").write_bytes(js)
    (package / "assets" / "index.css").write_bytes(css)
    (package / "index.html").write_text(
        '<script src="/dashboard/assets/index.js?v=' + sha256(js).hexdigest()[:8] + '"></script>'
        '<link rel="stylesheet" href="/dashboard/assets/index.css?v=' + sha256(css).hexdigest()[:8] + '">',
        encoding="utf-8",
    )
    return tmp_path


def test_current_source_and_packaged_assets_verify_without_git(dashboard: Path) -> None:
    assert _run(dashboard, "record").returncode == 0
    assert _run(dashboard, "verify").returncode == 0


def test_source_change_rejects_stale_assets(dashboard: Path) -> None:
    assert _run(dashboard, "record").returncode == 0
    (dashboard / "frontend/dashboard/src/main.tsx").write_text("export const value = 2;\n", encoding="utf-8")
    result = _run(dashboard, "verify")
    assert result.returncode == 1
    assert "does not match frontend inputs" in result.stderr


@pytest.mark.parametrize("change", ["alter", "remove"])
def test_changed_or_missing_referenced_asset_rejected(dashboard: Path, change: str) -> None:
    assert _run(dashboard, "record").returncode == 0
    asset = dashboard / "agent/governance/dashboard_dist/assets/index.js"
    if change == "alter":
        asset.write_bytes(b"changed")
    else:
        asset.unlink()
    assert _run(dashboard, "verify").returncode == 1


def test_asset_reference_cannot_escape_package(dashboard: Path) -> None:
    index = dashboard / "agent/governance/dashboard_dist/index.html"
    index.write_text('<script src="/dashboard/assets/../outside.js?v=00000000"></script>', encoding="utf-8")
    assert _run(dashboard, "record").returncode == 1


@pytest.mark.parametrize("mode", ["activate", "recover"])
def test_promotion_shell_entry_does_not_precheck_unvalidated_plan_assets(dashboard: Path, mode: str) -> None:
    """An existing recovery plan must reach plan/journal handling even if DEV assets drift."""
    plan = dashboard / "plan.json"
    plan.write_text(json.dumps({"dev_worktree": str(dashboard)}), encoding="utf-8")
    result = subprocess.run(
        [str(PROMOTION), f"--{mode}", "--activation-plan", str(plan), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "activation plan has missing or extra fields" in result.stderr
    assert "dashboard asset identity" not in result.stderr.lower()


def _activation_runtime() -> dict[str, object]:
    lines = PROMOTION.read_text(encoding="utf-8").splitlines()
    start = next(i + 1 for i, line in enumerate(lines) if 'python3 - "$MODE" "$ACTIVATION_PLAN"' in line)
    end = next(i for i in range(start, len(lines)) if lines[i] == "PY")
    namespace: dict[str, object] = {"__name__": "dashboard_activation_test"}
    exec(compile("\n".join(lines[start:end]), str(PROMOTION), "exec"), namespace)
    return namespace


def test_fresh_activation_checks_assets_but_terminal_and_recover_skip_them(dashboard: Path) -> None:
    runtime = _activation_runtime()
    (dashboard / "scripts").mkdir()
    shutil.copy2(SCRIPT, dashboard / "scripts/dashboard_asset_identity.py")
    anchor, candidate, tree = "a" * 40, "b" * 40, "c" * 40
    stable = str(dashboard / "stable")
    dev = str(dashboard)
    plan = {
        "stable_worktree": stable,
        "dev_worktree": dev,
        "stable_anchor_commit": anchor,
        "candidate_commit": candidate,
        "candidate_tree_sha": tree,
        "stable_branch": "main",
        "dev_branch": "codex/ac-dev",
        "old_process": {"pid": 123},
        "stable_port": 40000,
        "stable_database_path": "db",
        "graph_identity_hash": "graph",
    }

    class Ops:
        def git(self, root: str, *args: str) -> str:
            answers = {
                (stable, "rev-parse", "HEAD"): anchor,
                (dev, "rev-parse", "HEAD"): candidate,
                (stable, "branch", "--show-current"): "main",
                (dev, "branch", "--show-current"): "codex/ac-dev",
                (stable, "status", "--porcelain"): "",
                (dev, "status", "--porcelain"): "",
                (dev, "rev-parse", f"{candidate}^{{tree}}"): tree,
            }
            return answers[(str(root), *args)]

        def pid_identity(self, pid: int) -> dict[str, int]:
            return {"pid": pid}

        def port_pids(self, _port: int) -> list[int]:
            return [123]

        def graph_hash(self, _path: str) -> str:
            return "graph"

        def run(self, args: list[str], cwd: str, code: str) -> bytes:
            result = subprocess.run(args, cwd=cwd, capture_output=True, check=False)
            if result.returncode:
                raise runtime["PromotionFailure"](code, result.stderr.decode())
            return result.stdout

    machine = object.__new__(runtime["ActivationMachine"])
    machine.plan = plan
    machine.ops = Ops()
    machine.journal = SimpleNamespace(rows=[])
    machine.preimage_branch = "main"
    machine.exact_stable_worktree = lambda: None
    machine.exact_main_preimage_refs = lambda: None
    machine.exact_health = lambda *args, **kwargs: None
    machine.exact_database = lambda: None
    machine.validate_signoff_liveness = lambda: None

    with pytest.raises(runtime["PromotionFailure"]) as stale:
        machine.activate(dry_run=True)
    assert stale.value.code == "activation_dashboard_asset_identity_stale"

    assert _run(dashboard, "record").returncode == 0
    assert machine.activate(dry_run=True)["writes_performed"] is False

    (dashboard / "frontend/dashboard/src/main.tsx").write_text("stale again", encoding="utf-8")
    machine.journal.rows = [{"state": "COMPLETED"}]
    assert machine.activate(dry_run=True)["idempotent"] is True
    assert machine.recover(dry_run=True)["idempotent"] is True
    machine.journal.rows = [{"state": "ROLLED_BACK"}]
    assert machine.recover(dry_run=True)["rolled_back"] is True
    machine.journal.rows = [{"state": "MUTATION_INTENT"}]
    machine.journal_process_identity = lambda *args: None
    machine.rollback = lambda cause: {"rolled_back_for": cause}
    assert machine.recover(dry_run=False) == {"rolled_back_for": "operator_recover"}
