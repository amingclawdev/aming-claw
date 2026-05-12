from __future__ import annotations

from pathlib import Path

from agent.governance import server


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    _write(
        dist / "index.html",
        '<div id="root"></div><script type="module" src="/dashboard/assets/app.js"></script>'
        '<link rel="stylesheet" href="/dashboard/assets/app.css">',
    )
    _write(dist / "assets" / "app.js", "console.log('dashboard');")
    _write(dist / "assets" / "app.css", "body { color: #111; }")
    return dist


def test_dashboard_root_serves_index(tmp_path):
    dist = _dist(tmp_path)

    result = server._resolve_dashboard_static_request("/dashboard", dist)

    assert result["handled"] is True
    assert result["status"] == 200
    assert result["path"] == dist / "index.html"
    assert result["content_type"].startswith("text/html")
    assert result["cache_control"] == "no-cache"


def test_dashboard_asset_serves_immutable_file(tmp_path):
    dist = _dist(tmp_path)

    result = server._resolve_dashboard_static_request("/dashboard/assets/app.js", dist)

    assert result["handled"] is True
    assert result["status"] == 200
    assert result["path"] == dist / "assets" / "app.js"
    assert result["content_type"].startswith("application/javascript")
    assert "immutable" in result["cache_control"]


def test_dashboard_spa_fallback_serves_index(tmp_path):
    dist = _dist(tmp_path)

    result = server._resolve_dashboard_static_request("/dashboard/projects/smoke", dist)

    assert result["handled"] is True
    assert result["status"] == 200
    assert result["path"] == dist / "index.html"


def test_dashboard_missing_asset_is_404(tmp_path):
    dist = _dist(tmp_path)

    result = server._resolve_dashboard_static_request("/dashboard/assets/missing.js", dist)

    assert result["handled"] is True
    assert result["status"] == 404


def test_dashboard_path_traversal_is_404(tmp_path):
    dist = _dist(tmp_path)

    result = server._resolve_dashboard_static_request("/dashboard/assets/../index.html", dist)

    assert result["handled"] is True
    assert result["status"] == 404


def test_dashboard_unbuilt_dist_returns_503(tmp_path):
    result = server._resolve_dashboard_static_request("/dashboard", tmp_path / "missing-dist")

    assert result["handled"] is True
    assert result["status"] == 503
    assert b"Dashboard build not found" in result["body"]


def test_non_dashboard_path_is_not_handled(tmp_path):
    dist = _dist(tmp_path)

    result = server._resolve_dashboard_static_request("/api/health", dist)

    assert result == {"handled": False}
