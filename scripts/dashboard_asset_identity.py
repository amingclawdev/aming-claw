"""Bind packaged dashboard bytes to the frontend inputs that produced them."""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("frontend/dashboard")
PACKAGE = Path("agent/governance/dashboard_dist")
MANIFEST = "dashboard-asset-identity.json"
EXCLUDED_DIRS = {"dist", "node_modules", ".git", "coverage", "playwright-report", "test-results"}
EXCLUDED_FILES = {".DS_Store"}
ASSET_URL = re.compile(r"^/dashboard/assets/([A-Za-z0-9_./-]+)\?v=([0-9a-f]{8})$")


def _hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _read_inside(base: Path, relative: Path) -> bytes:
    if base.is_symlink():
        raise ValueError(f"symlink in dashboard root: {base}")
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"unsafe dashboard path: {relative}")
    path = base / relative
    for depth in range(1, len(relative.parts) + 1):
        if (base / Path(*relative.parts[:depth])).is_symlink():
            raise ValueError(f"symlink in dashboard path: {relative}")
    if not path.is_file():
        raise ValueError(f"missing dashboard file: {relative}")
    return path.read_bytes()


def _source_inputs(root: Path) -> dict[str, str]:
    source = root / SOURCE
    if not source.is_dir():
        raise ValueError(f"dashboard frontend missing: {source}")
    inputs: dict[str, str] = {}
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if relative.parts[0] in EXCLUDED_DIRS or path.name in EXCLUDED_FILES:
            continue
        if path.is_symlink():
            raise ValueError(f"symlink in dashboard input: {relative}")
        if path.is_file() and not path.name.endswith(".tsbuildinfo"):
            inputs[relative.as_posix()] = _hash(_read_inside(source, relative))
    if not {"package.json", "package-lock.json", "index.html"}.issubset(inputs):
        raise ValueError("dashboard frontend build inputs are incomplete")
    return inputs


class _Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.urls.append(values["src"] or "")
        if tag == "link" and values.get("rel") == "stylesheet" and values.get("href", "").startswith("/dashboard/assets/"):
            self.urls.append(values["href"] or "")


def identity(root: Path) -> dict[str, object]:
    inputs = _source_inputs(root)
    package = root / PACKAGE
    index = _read_inside(package, Path("index.html"))
    parser = _Assets()
    parser.feed(index.decode("utf-8"))
    assets: dict[str, str] = {}
    kinds: set[str] = set()
    for url in parser.urls:
        match = ASSET_URL.fullmatch(url)
        if not match:
            raise ValueError(f"unsafe or unversioned dashboard asset URL: {url}")
        if any(part in {"", ".", ".."} for part in match[1].split("/")):
            raise ValueError(f"unsafe dashboard asset URL: {url}")
        relative = Path("assets") / match[1]
        if relative.suffix not in {".js", ".css"}:
            raise ValueError(f"unexpected dashboard asset type: {relative}")
        data = _read_inside(package, relative)
        digest = _hash(data)
        if digest.removeprefix("sha256:")[:8] != match[2]:
            raise ValueError(f"dashboard asset version disagrees with bytes: {relative}")
        assets[relative.as_posix()] = digest
        kinds.add(relative.suffix)
    if kinds != {".js", ".css"} or len(assets) != len(parser.urls):
        raise ValueError("dashboard index must reference distinct JS and CSS assets")
    actual = {
        path.relative_to(package).as_posix()
        for path in (package / "assets").rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual != set(assets):
        raise ValueError("dashboard assets differ from index references")
    return {
        "schema_version": "dashboard_asset_identity.v1",
        "frontend_inputs_sha256": _hash(_canonical(inputs)),
        "index_sha256": _hash(index),
        "assets": assets,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("record", "verify"))
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        expected = identity(root)
        manifest = root / PACKAGE / MANIFEST
        if args.mode == "record":
            manifest.write_bytes(_canonical(expected))
        elif manifest.read_bytes() != _canonical(expected):
            raise ValueError("dashboard asset identity does not match frontend inputs or packaged bytes")
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Dashboard asset identity {args.mode} failed: {exc}", file=sys.stderr)
        return 1
    print(f"dashboard asset identity {args.mode} passed: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
