from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dashboard_asset_identity.py"


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
