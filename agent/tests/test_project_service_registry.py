from __future__ import annotations

import json
import threading

from agent.governance import project_service


def test_project_registry_save_is_atomic_and_loadable(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    monkeypatch.setattr(project_service, "_governance_root", lambda: state_root)

    first = {
        "version": 1,
        "projects": {
            "demo": {
                "project_id": "demo",
                "workspace_path": str(tmp_path),
                "status": "active",
            }
        },
    }
    project_service._save_projects(first)

    loaded = project_service._load_projects()
    assert loaded["projects"]["demo"]["workspace_path"] == str(tmp_path)
    assert json.loads((state_root / "projects.json").read_text(encoding="utf-8"))["version"] == 1
    assert not list(state_root.glob(".projects.json.*.tmp"))


def test_project_registry_concurrent_readers_never_observe_partial_json(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    monkeypatch.setattr(project_service, "_governance_root", lambda: state_root)
    project_service._save_projects({"version": 1, "projects": {}})
    errors: list[Exception] = []

    def _reader() -> None:
        try:
            for _ in range(25):
                data = project_service._load_projects()
                assert isinstance(data.get("projects"), dict)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    readers = [threading.Thread(target=_reader) for _ in range(4)]
    for reader in readers:
        reader.start()
    for idx in range(25):
        project_service._save_projects(
            {
                "version": 1,
                "projects": {
                    "demo": {
                        "project_id": "demo",
                        "workspace_path": str(tmp_path),
                        "status": "active",
                        "counter": idx,
                    }
                },
            }
        )
    for reader in readers:
        reader.join()

    assert errors == []


def test_project_ai_routing_metadata_merges_partial_updates(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    monkeypatch.setattr(project_service, "_governance_root", lambda: state_root)
    project_service._save_projects(
        {
            "version": 1,
            "projects": {
                "demo": {
                    "project_id": "demo",
                    "workspace_path": str(tmp_path),
                    "status": "active",
                    "project_config": {
                        "project_id": "demo",
                        "ai": {
                            "routing": {
                                "pm": {"provider": "openai", "model": "gpt-5.5"},
                                "dev": {"provider": "openai", "model": "gpt-5.4"},
                                "semantic": {
                                    "provider": "anthropic",
                                    "model": "claude-opus-4-7",
                                },
                            }
                        },
                    },
                }
            },
        }
    )

    updated = project_service.update_project_ai_routing_metadata(
        "demo",
        {"semantic": {"provider": "openai", "model": "gpt-5.4-mini"}},
        actor="dashboard-test",
    )

    routing = updated["project_config"]["ai"]["routing"]
    assert routing["semantic"] == {"provider": "openai", "model": "gpt-5.4-mini"}
    assert routing["pm"] == {"provider": "openai", "model": "gpt-5.5"}
    assert routing["dev"] == {"provider": "openai", "model": "gpt-5.4"}
