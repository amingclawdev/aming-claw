"""Tests for governance event bus."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from governance.event_bus import EventBus


class TestEventBus(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_subscribe_and_publish(self):
        received = []
        self.bus.subscribe("test.event", lambda p: received.append(p))
        self.bus.publish("test.event", {"key": "value"})
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["key"], "value")

    def test_no_subscribers(self):
        # Should not raise
        self.bus.publish("no.subscribers", {"data": 1})

    def test_wildcard_subscriber(self):
        received = []
        self.bus.subscribe("*", lambda p: received.append(p))
        self.bus.publish("any.event", {"x": 1})
        self.bus.publish("other.event", {"x": 2})
        self.assertEqual(len(received), 2)

    def test_unsubscribe(self):
        received = []
        cb = lambda p: received.append(p)
        self.bus.subscribe("test.event", cb)
        self.bus.unsubscribe("test.event", cb)
        self.bus.publish("test.event", {"x": 1})
        self.assertEqual(len(received), 0)

    def test_recent_events(self):
        self.bus.publish("e1", {"a": 1})
        self.bus.publish("e2", {"a": 2})
        recent = self.bus.recent_events(10)
        self.assertEqual(len(recent), 2)

    def test_subscriber_error_does_not_crash(self):
        def bad_callback(p):
            raise ValueError("boom")
        self.bus.subscribe("test.event", bad_callback)
        # Should not raise
        self.bus.publish("test.event", {"data": 1})

    def test_clear(self):
        self.bus.subscribe("test", lambda p: None)
        self.bus.publish("test", {})
        self.bus.clear()
        self.assertEqual(len(self.bus.recent_events()), 0)


if __name__ == "__main__":
    unittest.main()
