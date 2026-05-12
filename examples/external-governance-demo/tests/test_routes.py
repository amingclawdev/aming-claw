import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from demo_app.routes import quote_breakdown_route, quote_export_route, quote_summary_route


def test_routes_fan_in_to_shared_quote_breakdown():
    payload = {"items": [{"sku": "fixture", "price": 20, "quantity": 1}]}
    assert quote_breakdown_route(payload)["total"] == 30.1
    assert quote_summary_route(payload)["summary"] == "30.10 total / 0 flags"
    assert quote_export_route(payload)["summary"]["total"] == 30.1


def test_route_flags_surface_review_state():
    payload = {"items": [{"sku": "acid", "price": 12, "quantity": 1, "hazmat": True}]}
    exported = quote_export_route(payload)
    assert exported["summary"]["requires_review"] is True
    assert exported["quote"]["flags"] == ["requires_hazmat_review"]
