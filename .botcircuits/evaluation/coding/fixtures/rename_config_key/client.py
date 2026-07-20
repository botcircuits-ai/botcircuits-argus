"""A network client that reads the retry setting in two places."""

from settings import DEFAULTS, get_setting


class Client:
    def __init__(self, overrides: dict | None = None):
        self.overrides = overrides or {}

    def retries(self) -> int:
        # Reads the setting by key — this key must match settings.DEFAULTS.
        return get_setting(self.overrides, "max_retries")

    def describe(self) -> str:
        # Also references the default directly.
        return f"client with up to {DEFAULTS['max_retries']} retries"
