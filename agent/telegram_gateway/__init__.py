"""Telegram Gateway - Docker container module.

Responsibilities:
- Poll Telegram for user messages (long polling)
- Parse commands and route to appropriate handlers
- Call governance API via HTTP for workflow operations
- Write task files to shared volume for executor pickup
- Subscribe to Redis Pub/Sub for governance events
- Push event notifications to Telegram
"""
