"""Tests for agent.notification_gateway — AC2, AC3, AC4."""

import io
import inspect
from abc import ABC, abstractmethod

from agent.notification_gateway import (
    NotificationGateway,
    ConsoleGateway,
    get_gateway,
)


class TestNotificationGatewayABC:
    """AC2: NotificationGateway is an ABC with the required abstract methods."""

    def test_is_abc(self):
        assert issubclass(NotificationGateway, ABC)

    def test_send_message_abstract(self):
        m = getattr(NotificationGateway, "send_message")
        assert getattr(m, "__isabstractmethod__", False)

    def test_send_reply_abstract(self):
        m = getattr(NotificationGateway, "send_reply")
        assert getattr(m, "__isabstractmethod__", False)

    def test_get_updates_abstract(self):
        m = getattr(NotificationGateway, "get_updates")
        assert getattr(m, "__isabstractmethod__", False)


class TestConsoleGateway:
    """AC3 + AC4: ConsoleGateway implements the interface and writes to stdout."""

    def test_is_subclass(self):
        assert issubclass(ConsoleGateway, NotificationGateway)

    def test_send_message_stdout(self):
        buf = io.StringIO()
        gw = ConsoleGateway(stream=buf)
        gw.send_message("123", "hello world")
        output = buf.getvalue()
        assert "123" in output
        assert "hello world" in output

    def test_send_reply_stdout(self):
        buf = io.StringIO()
        gw = ConsoleGateway(stream=buf)
        gw.send_reply("123", "456", "reply text")
        output = buf.getvalue()
        assert "reply_to=456" in output
        assert "reply text" in output

    def test_get_updates_returns_list(self):
        gw = ConsoleGateway()
        assert gw.get_updates() == []


class TestGetGateway:
    def test_console_backend(self):
        gw = get_gateway("console")
        assert isinstance(gw, ConsoleGateway)

    def test_unknown_raises(self):
        try:
            get_gateway("nonexistent")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass


class TestNotificationBackendEnv:
    """AC4: NOTIFICATION_BACKEND=console produces stdout output."""

    def test_console_via_factory(self, capsys):
        gw = get_gateway("console")
        gw.send_message("test_chat", "AC4 test message")
        captured = capsys.readouterr()
        assert "AC4 test message" in captured.out
