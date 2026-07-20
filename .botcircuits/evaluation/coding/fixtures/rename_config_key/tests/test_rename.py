"""Target: the config key `max_retries` is renamed to `retry_limit`
EVERYWHERE it is defined or read. Starts RED; a complete rename turns it GREEN.

The trap: it is easy to rename the definition in settings.py but miss the
string key passed to get_setting() in client.py — that leaves the lookup
returning None, which this test catches."""
from settings import DEFAULTS
from client import Client


def test_key_renamed_in_defaults():
    assert "retry_limit" in DEFAULTS
    assert "max_retries" not in DEFAULTS


def test_client_still_reads_the_value():
    # After the rename, the client must still resolve the default (3), which
    # only works if BOTH the definition and the client's lookup key changed.
    assert Client().retries() == 3


def test_no_stale_key_in_describe():
    # The describe() default reference must also point at the renamed key
    # (it reads DEFAULTS[...] directly, so a missed rename raises KeyError).
    assert "3 retries" in Client().describe()
