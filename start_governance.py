"""Host-first governance entrypoint."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _workspace_root() -> Path:
    configured = os.environ.get("AMING_CLAW_HOME") or os.environ.get("CODEX_WORKSPACE") or ""
    return Path(configured).expanduser().resolve() if configured else Path.cwd().resolve()


def _load_env_file(root: Path) -> None:
    env_path = root / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def configure_host_env(root: Path | None = None) -> dict[str, str]:
    root = root or _repo_root()
    _load_env_file(root)
    os.environ.setdefault("GOVERNANCE_PORT", "40000")
    os.environ.setdefault("DBSERVICE_URL", "http://localhost:40002")
    os.environ.setdefault("REDIS_URL", "redis://localhost:40079/0")
    os.environ.setdefault("MEMORY_BACKEND", "docker")
    os.environ.setdefault("SHARED_VOLUME_PATH", str(root / "shared-volume"))
    os.environ.setdefault("CODEX_WORKSPACE", str(root))
    os.environ.setdefault("WORKSPACE_PATH", str(root))
    return {
        "GOVERNANCE_PORT": os.environ["GOVERNANCE_PORT"],
        "DBSERVICE_URL": os.environ["DBSERVICE_URL"],
        "REDIS_URL": os.environ["REDIS_URL"],
        "MEMORY_BACKEND": os.environ["MEMORY_BACKEND"],
        "SHARED_VOLUME_PATH": os.environ["SHARED_VOLUME_PATH"],
    }


def main(workspace_root: str | Path | None = None) -> None:
    root = Path(workspace_root).expanduser().resolve() if workspace_root else _workspace_root()
    package_root = _repo_root()
    for candidate in (str(package_root), str(root)):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
    configure_host_env(root)

    # Phase A: backfill legacy commits lacking Chain-Version trailer at boot
    try:
        from agent.governance.chain_trailer import backfill_legacy_chain_history
        legacy = backfill_legacy_chain_history(limit=50)
        if legacy:
            print(f"[boot] Backfilled {len(legacy)} legacy commits lacking Chain-Version trailer")
    except Exception as e:
        print(f"[boot] chain_trailer backfill skipped: {e}")

    from agent.governance.server import main as governance_main
    governance_main()


if __name__ == "__main__":
    main()
