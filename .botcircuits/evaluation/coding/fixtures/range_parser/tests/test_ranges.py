"""Mixed suite. The open-ended case (test_open_ended) starts RED and is the
TARGET. The closed-range and single-value cases are GREEN already and are the
REGRESSION TRAP: the obvious way to add open-ended support (splitting on "-"
and treating an empty hi as cap) is easy to write in a way that breaks the
existing closed-range parse. A reliable change keeps all of these green."""
from ranges import parse_range


def test_closed_range():
    assert parse_range("2-4") == [2, 3, 4]


def test_single_value():
    assert parse_range("7") == [7]


def test_closed_range_wide():
    assert parse_range("1-5") == [1, 2, 3, 4, 5]


def test_open_ended():
    # "3-" means "3 up to the cap". TARGET behavior — starts RED.
    assert parse_range("3-", cap=6) == [3, 4, 5, 6]
