"""Order pricing with a tiered discount.

Discount policy:
  - subtotal >= 100  -> 10% off
  - subtotal >= 50   -> 5% off
  - otherwise        -> no discount

There is a bug in `apply_discount`: the boundary at exactly 100 is handled
wrong, so a subtotal of exactly 100 does not get the 10% tier.
"""


def apply_discount(subtotal: float) -> float:
    """Return the total after the tiered discount."""
    if subtotal > 100:          # BUG: should be >= 100
        return round(subtotal * 0.90, 2)
    if subtotal >= 50:
        return round(subtotal * 0.95, 2)
    return round(subtotal, 2)
