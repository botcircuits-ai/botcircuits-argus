"""Existing behavior — MUST stay green (regression guard)."""
from utils import shout


def test_shout():
    assert shout("hi") == "HI"
