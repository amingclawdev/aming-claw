"""Thin route handlers for the demo service."""

from demo_app.service import (
    calculate_total,
    export_quote_payload,
    quote_breakdown,
    summarize_quote,
)


def quote_order(payload):
    return {"total": calculate_total(payload.get("items", []))}


def quote_breakdown_route(payload):
    return quote_breakdown(payload)


def quote_summary_route(payload):
    return {"summary": summarize_quote(payload)}


def quote_export_route(payload):
    return export_quote_payload(payload)
