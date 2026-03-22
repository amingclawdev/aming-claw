"""Telegram Gateway main entry point.

Runs inside Docker container. Combines:
1. Telegram long-polling (existing coordinator logic)
2. Redis Pub/Sub listener for governance events → Telegram notifications
"""

import os
import sys
import logging
import time

# Setup path
_agent_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, _agent_dir)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("telegram_gateway")


def send_telegram_notification(text: str) -> None:
    """Send a notification message to the configured admin chat."""
    from utils import send_text
    admin_chat_id = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")
    if not admin_chat_id:
        log.warning("TELEGRAM_ADMIN_CHAT_ID not set, skipping notification: %s", text)
        return
    try:
        send_text(int(admin_chat_id), text)
    except Exception as e:
        log.error("Failed to send Telegram notification: %s", e)


def start_event_listener() -> None:
    """Start Redis Pub/Sub listener for governance events."""
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    from telegram_gateway.gov_event_listener import GovEventListener
    listener = GovEventListener(redis_url, send_telegram_notification)
    if listener.start():
        log.info("Governance event listener started (Redis: %s)", redis_url)
    else:
        log.warning("Governance event listener disabled (Redis unavailable)")


def run() -> None:
    """Main entry: start event listener + coordinator polling loop."""
    log.info("Telegram Gateway starting...")
    log.info("  GOVERNANCE_URL: %s", os.environ.get("GOVERNANCE_URL", "not set"))
    log.info("  REDIS_URL: %s", os.environ.get("REDIS_URL", "not set"))
    log.info("  TELEGRAM_ADMIN_CHAT_ID: %s", os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "not set"))

    # Start governance event listener (Redis Pub/Sub → Telegram)
    start_event_listener()

    # Run existing coordinator polling loop
    from coordinator import run as coordinator_run
    coordinator_run()


if __name__ == "__main__":
    run()
