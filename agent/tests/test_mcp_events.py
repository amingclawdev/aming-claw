from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from agent.mcp import events


def test_event_bridge_forwards_observer_command_pending(monkeypatch):
    notifications: list[tuple[str, dict]] = []
    reminder = {
        "kind": "observer_command_pending",
        "project_id": "demo",
        "message": "pending observer commands exist; call observer_command_next",
        "payload_included": False,
    }

    bridge = events.EventBridge(
        "redis://example.invalid:6379/0",
        lambda event_name, payload: notifications.append((event_name, payload)),
    )

    class FakePubSub:
        def psubscribe(self, pattern: str) -> None:
            assert pattern == "gov:events:*"

        def listen(self):
            yield {
                "type": "pmessage",
                "data": json.dumps({
                    "event": "observer_command_pending",
                    "payload": reminder,
                }),
            }
            yield {
                "type": "pmessage",
                "data": json.dumps({
                    "event": "unlisted.event",
                    "payload": {"project_id": "demo"},
                }),
            }
            bridge._running = False

    class FakeRedis:
        def ping(self) -> bool:
            return True

        def pubsub(self) -> FakePubSub:
            return FakePubSub()

    fake_redis_module = SimpleNamespace(from_url=lambda *args, **kwargs: FakeRedis())
    monkeypatch.setitem(sys.modules, "redis", fake_redis_module)

    assert "observer_command_pending" in events.NOTIFY_EVENTS
    bridge._running = True
    bridge._subscribe_loop()

    assert notifications == [("observer_command_pending", reminder)]
