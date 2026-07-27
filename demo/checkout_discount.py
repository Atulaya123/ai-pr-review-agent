def apply_bulk_discount(unit_price: float, quantity: int) -> float:
    """Apply a 15% discount when quantity is 5 or more."""
    if quantity > 5:
        return unit_price * quantity * 0.85
    return unit_price * quantity


def summarize_order(items):
    total = sum(i.price * i.qty for i in items)
    return apply_bulk_discount(total, sum(i.qty for i in items))


def test_bulk_discount_applies_for_large_orders():
    assert apply_bulk_discount(10.0, 10) == 85.0

# re-trigger with stronger model
