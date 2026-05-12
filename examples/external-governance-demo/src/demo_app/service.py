"""Order pricing service with deliberate fan-in and fan-out shapes."""

TAX_RATES = {
    "CA-ON": 0.13,
    "US-NY": 0.08875,
    "DEFAULT": 0.10,
}


def normalize_item(item):
    return {
        "sku": str(item.get("sku") or "unknown"),
        "price": float(item.get("price", 0)),
        "quantity": int(item.get("quantity", 1)),
        "hazmat": bool(item.get("hazmat", False)),
    }


def normalize_items(items):
    return [normalize_item(item) for item in items]


def line_total(item):
    normalized = normalize_item(item)
    return normalized["price"] * normalized["quantity"]


def subtotal_for(items):
    return sum(line_total(item) for item in items)


def discount_for(subtotal, customer_tier="standard"):
    if customer_tier == "enterprise":
        return subtotal * 0.15
    if customer_tier == "member":
        return subtotal * 0.05
    return 0.0


def tax_for(amount, region="CA-ON"):
    rate = TAX_RATES.get(region, TAX_RATES["DEFAULT"])
    return amount * rate


def shipping_for(items):
    quantity = sum(item["quantity"] for item in normalize_items(items))
    return 0.0 if quantity >= 5 else 7.5


def compliance_flags(items):
    normalized = normalize_items(items)
    flags = []
    if any(item["hazmat"] for item in normalized):
        flags.append("requires_hazmat_review")
    if any(item["price"] <= 0 for item in normalized):
        flags.append("invalid_price")
    return flags


def quote_breakdown(payload):
    items = payload.get("items", [])
    subtotal = subtotal_for(items)
    discount = discount_for(subtotal, payload.get("customer_tier", "standard"))
    taxable = subtotal - discount
    tax = tax_for(taxable, payload.get("region", "CA-ON"))
    shipping = shipping_for(items)
    total = round(taxable + tax + shipping, 2)
    return {
        "subtotal": round(subtotal, 2),
        "discount": round(discount, 2),
        "tax": round(tax, 2),
        "shipping": round(shipping, 2),
        "total": total,
        "flags": compliance_flags(items),
    }


def quote_contract_summary(payload):
    quote = quote_breakdown(payload)
    return {
        "line_count": len(payload.get("items", [])),
        "total": quote["total"],
        "requires_review": bool(quote["flags"]),
    }


def calculate_total(items):
    return quote_breakdown({"items": items})["total"]


def summarize_quote(payload):
    quote = quote_breakdown(payload)
    return f"{quote['total']:.2f} total / {len(quote['flags'])} flags"


def export_quote_payload(payload):
    quote = quote_breakdown(payload)
    return {
        "contract": "quote.v1",
        "summary": quote_contract_summary(payload),
        "quote": quote,
    }
