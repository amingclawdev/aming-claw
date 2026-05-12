import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from demo_app.service import (
    calculate_total,
    compliance_flags,
    export_quote_payload,
    quote_breakdown,
    quote_contract_summary,
    shipping_for,
)


def test_calculate_total_adds_tax():
    total = calculate_total([{"price": 10, "quantity": 2}])
    assert total == 30.1


def test_quote_breakdown_fans_out_pricing_steps():
    quote = quote_breakdown({
        "customer_tier": "member",
        "region": "CA-ON",
        "items": [
            {"sku": "book", "price": 10, "quantity": 3},
            {"sku": "battery", "price": 5, "quantity": 2, "hazmat": True},
        ],
    })
    assert quote["subtotal"] == 40
    assert quote["discount"] == 2
    assert quote["shipping"] == 0
    assert quote["total"] == 42.94
    assert quote["flags"] == ["requires_hazmat_review"]


def test_l4_state_helpers_are_independently_addressable():
    assert shipping_for([{"price": 1, "quantity": 6}]) == 0
    assert compliance_flags([{"price": 0, "quantity": 1}]) == ["invalid_price"]


def test_contract_summary_and_export_share_breakdown():
    payload = {"items": [{"sku": "fixture", "price": 20, "quantity": 1}]}
    assert quote_contract_summary(payload) == {
        "line_count": 1,
        "total": 30.1,
        "requires_review": False,
    }
    exported = export_quote_payload(payload)
    assert exported["contract"] == "quote.v1"
    assert exported["quote"]["total"] == exported["summary"]["total"]
