"""Target: add_amounts must not exhibit float drift. Starts RED
(add_amounts(0.10, 0.20) == 0.30000000000000004). Any fix that removes the
drift turns it GREEN.

The OBJECTIVE test only checks the drift is gone (both `round(total, 2)` and
proper integer-cents math pass it). Whether the fix actually FOLLOWED the
documented cents convention (vs. papering over it with a trailing round) is a
CONVENTION/quality question — scored by the LLM judge against CONVENTIONS.md,
not pinned here, because a value test can't cleanly separate the two."""
from money import add_amounts


def test_no_float_drift():
    assert add_amounts(0.10, 0.20) == 0.30


def test_sum_many_small():
    assert add_amounts(*([0.01] * 100)) == 1.00


def test_single_amount():
    assert add_amounts(4.99) == 4.99
