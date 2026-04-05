"""Notification gateway abstraction.

Defines the NotificationGateway ABC and a ConsoleGateway implementation
for local/testing use. TelegramGateway adapts the existing gateway.py.
"""

import sys
import logging
from abc import ABC, abstractmethod
from typing import Any, List, Optional

log = logging.getLogger(__name__)


class NotificationGateway(ABC):
    """Abstract base class for notification backends."""

    @abstractmethod
    def send_message(self, chat_id: str, text: str) -> None:
        """Send a text message to the given chat."""
        ...

    @abstractmethod
    def send_reply(self, chat_id: str, reply_to_id: str, text: str) -> None:
        """Send a reply to a specific message."""
        ...

    @abstractmethod
    def get_updates(self, offset: int = 0) -> List[dict]:
        """Poll for new messages. Returns list of update dicts."""
        ...


class ConsoleGateway(NotificationGateway):
    """Prints notifications to stdout — useful for local dev and tests."""

    def __init__(self, stream=None):
        self._stream = stream or sys.stdout

    def send_message(self, chat_id: str, text: str) -> None:
        self._stream.write(f"[ConsoleGateway] to={chat_id}: {text}\n")
        self._stream.flush()

    def send_reply(self, chat_id: str, reply_to_id: str, text: str) -> None:
        self._stream.write(
            f"[ConsoleGateway] to={chat_id} reply_to={reply_to_id}: {text}\n"
        )
        self._stream.flush()

    def get_updates(self, offset: int = 0) -> List[dict]:
        return []


def get_gateway(backend: str = "telegram") -> NotificationGateway:
    """Factory: return a gateway instance based on backend name."""
    if backend == "console":
        return ConsoleGateway()
    if backend == "telegram":
        from agent.telegram_gateway.gateway import TelegramGateway
        return TelegramGateway()
    raise ValueError(f"Unknown notification backend: {backend}")
