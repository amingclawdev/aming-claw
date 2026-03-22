"""Internal event bus for the governance service.

Supports synchronous in-process subscriptions. Webhook support is planned for P2.
"""

import logging
from collections import defaultdict
from typing import Callable

log = logging.getLogger(__name__)

# Well-known event names
EVENTS = [
    "node.status_changed",
    "node.created",
    "node.deleted",
    "gate.satisfied",
    "gate.blocked",
    "release.blocked",
    "release.approved",
    "role.registered",
    "role.expired",
    "role.missing",
    "rollback.executed",
    "task.created",
    "task.updated",
    "memory.written",
]


class EventBus:
    """Simple synchronous event bus for in-process subscriptions."""

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._history: list[dict] = []  # Recent events for debugging
        self._max_history = 1000

    def subscribe(self, event: str, callback: Callable) -> None:
        """Subscribe to an event.

        Args:
            event: Event name (e.g., "node.status_changed").
            callback: Function(payload: dict) to call when event fires.
        """
        self._subscribers[event].append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        """Remove a subscription."""
        subs = self._subscribers.get(event, [])
        if callback in subs:
            subs.remove(callback)

    def publish(self, event: str, payload: dict) -> None:
        """Publish an event to all subscribers.

        Args:
            event: Event name.
            payload: Event data dict.
        """
        # Record in history
        entry = {"event": event, "payload": payload}
        self._history.append(entry)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        # Dispatch to subscribers
        for callback in self._subscribers.get(event, []):
            try:
                callback(payload)
            except Exception:
                log.exception("Event subscriber error for %s", event)

        # Also dispatch to wildcard subscribers
        for callback in self._subscribers.get("*", []):
            try:
                callback(payload)
            except Exception:
                log.exception("Wildcard subscriber error for %s", event)

    def recent_events(self, limit: int = 50) -> list[dict]:
        """Get recent event history for debugging."""
        return self._history[-limit:]

    def clear(self) -> None:
        """Clear all subscriptions and history."""
        self._subscribers.clear()
        self._history.clear()


# Global event bus instance
_bus = EventBus()


def get_event_bus() -> EventBus:
    """Get the global event bus instance."""
    return _bus


def publish(event: str, payload: dict) -> None:
    """Convenience: publish to the global event bus."""
    _bus.publish(event, payload)


def subscribe(event: str, callback: Callable) -> None:
    """Convenience: subscribe on the global event bus."""
    _bus.subscribe(event, callback)
