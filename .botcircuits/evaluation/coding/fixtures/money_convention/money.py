"""Money helpers.

NOTE: see CONVENTIONS.md — amounts must be handled as integer CENTS, never as
floating-point dollars. `add_amounts` currently violates that and has a
rounding bug because of it.
"""


def add_amounts(*dollar_amounts: float) -> float:
    """Sum several dollar amounts. BUG: floating-point accumulation makes
    e.g. add_amounts(0.10, 0.20) == 0.30000000000000004."""
    total = 0.0
    for a in dollar_amounts:
        total += a
    return total
