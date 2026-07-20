"""Mixed suite. The boundary case starts RED (the bug); the others are GREEN
already and must stay GREEN after the fix."""
from pricing import apply_discount


def test_no_discount():
    assert apply_discount(20) == 20.0


def test_mid_tier():
    assert apply_discount(60) == 57.0  # 5% off


def test_high_tier_above_boundary():
    assert apply_discount(200) == 180.0  # 10% off


def test_high_tier_at_boundary():
    # RED until the >100 bug is fixed to >=100.
    assert apply_discount(100) == 90.0
