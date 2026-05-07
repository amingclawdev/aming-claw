"""Thin route handlers for the demo service."""

from demo_app.service import calculate_total


def quote_order(payload):
    return {"total": calculate_total(payload.get("items", []))}

