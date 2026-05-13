from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

import setuptools.build_meta as build_meta


ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], cwd: Path = ROOT) -> None:
    print("+ " + " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd), check=True)


def _npm_cmd() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Aming Claw pip package with dashboard assets.")
    parser.add_argument("--wheel-dir", default="dist/python", help="Output directory for built wheels.")
    parser.add_argument("--skip-dashboard-build", action="store_true", help="Reuse existing dashboard dist assets.")
    args = parser.parse_args(argv)

    dashboard_package = ROOT / "agent" / "governance" / "dashboard_dist" / "index.html"
    if not args.skip_dashboard_build:
        _run([_npm_cmd(), "--prefix", "frontend/dashboard", "run", "build"])
    if not dashboard_package.is_file():
        raise SystemExit(
            "Packaged dashboard assets are missing. Run "
            "`npm --prefix frontend/dashboard run build` before building the wheel."
        )

    wheel_dir = ROOT / args.wheel_dir
    wheel_dir.mkdir(parents=True, exist_ok=True)
    wheel_name = build_meta.build_wheel(str(wheel_dir))
    print(f"wheel output: {wheel_dir / wheel_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
