"""Guard: the client can be constructed and timeout is untouched. Must stay
GREEN — a rename that breaks unrelated settings is a regression."""
from settings import get_setting


def test_timeout_untouched():
    assert get_setting({}, "timeout_s") == 30
