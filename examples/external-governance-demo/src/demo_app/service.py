"""Order pricing service."""


def calculate_total(items):
    subtotal = sum(item["price"] * item.get("quantity", 1) for item in items)
    return round(subtotal + tax_for(subtotal), 2)


def tax_for(amount):
    return amount * 0.13

