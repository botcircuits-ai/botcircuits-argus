"""Application settings. Defaults live here."""

DEFAULTS = {
    "max_retries": 3,
    "timeout_s": 30,
}


def get_setting(overrides: dict, key: str):
    """Read a setting, falling back to DEFAULTS."""
    return overrides.get(key, DEFAULTS.get(key))
