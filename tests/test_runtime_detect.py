"""Runtime selection: explicit config first, then env/binary auto-detect."""

import botcircuits.runtime.detect as d


def test_explicit_env_wins(monkeypatch):
    monkeypatch.setenv(d.RUNTIME_ENV, "native")
    # Even with a claude-code env marker present, the explicit env wins.
    monkeypatch.setenv("CLAUDECODE", "1")
    assert d.detect_runtime_name({"runtime": "claude-code"}) == "native"


def test_explicit_settings_when_no_env(monkeypatch):
    monkeypatch.delenv(d.RUNTIME_ENV, raising=False)
    assert d.detect_runtime_name({"runtime": "claude-code"}) == "claude-code"


def test_env_marker_autodetect(monkeypatch):
    monkeypatch.delenv(d.RUNTIME_ENV, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert d.detect_runtime_name({}) == "claude-code"


_ALL_MARKERS = (
    "CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT",
    "CODEX_SANDBOX", "CODEX_HOME", "OPENCLAW", "OPENCLAW_SESSION",
)


def test_falsy_env_marker_ignored(monkeypatch):
    monkeypatch.delenv(d.RUNTIME_ENV, raising=False)
    for m in _ALL_MARKERS:
        monkeypatch.delenv(m, raising=False)
    monkeypatch.setenv("CLAUDECODE", "0")
    # No truthy markers and (mock) no binaries -> native.
    monkeypatch.setattr(d.shutil, "which", lambda _b: None)
    assert d.detect_runtime_name({}) == "native"


def test_binary_probe_fallback(monkeypatch):
    monkeypatch.delenv(d.RUNTIME_ENV, raising=False)
    for m in ("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT",
              "CODEX_SANDBOX", "CODEX_HOME", "OPENCLAW", "OPENCLAW_SESSION"):
        monkeypatch.delenv(m, raising=False)
    monkeypatch.setattr(d.shutil, "which", lambda b: "/usr/bin/codex" if b == "codex" else None)
    assert d.detect_runtime_name({}) == "codex"


def test_default_native_when_nothing(monkeypatch):
    monkeypatch.delenv(d.RUNTIME_ENV, raising=False)
    for m in ("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT",
              "CODEX_SANDBOX", "CODEX_HOME", "OPENCLAW", "OPENCLAW_SESSION"):
        monkeypatch.delenv(m, raising=False)
    monkeypatch.setattr(d.shutil, "which", lambda _b: None)
    assert d.detect_runtime_name({}) == "native"


def test_runtime_config_settings_override():
    cfg = d.runtime_config(
        "claude-code",
        {"runtimes": {"claude-code": {"command": ["x", "{prompt}"], "timeout": 42}}},
    )
    assert cfg.command == ["x", "{prompt}"]
    assert cfg.timeout == 42.0


def test_runtime_config_defaults():
    cfg = d.runtime_config("claude-code", {})
    assert cfg.command[0] == "claude"
    assert "{prompt}" in cfg.command


def test_explicit_hermes_settings(monkeypatch):
    monkeypatch.delenv(d.RUNTIME_ENV, raising=False)
    assert d.detect_runtime_name({"runtime": "hermes"}) == "hermes"


def test_hermes_binary_probe(monkeypatch):
    monkeypatch.delenv(d.RUNTIME_ENV, raising=False)
    for m in _ALL_MARKERS + ("HERMES_HOME", "HERMES_SESSION"):
        monkeypatch.delenv(m, raising=False)
    # Only `hermes` on PATH -> hermes selected via the binary probe.
    monkeypatch.setattr(
        d.shutil, "which", lambda b: "/usr/bin/hermes" if b == "hermes" else None)
    assert d.detect_runtime_name({}) == "hermes"


def test_explicit_runtime_beats_hermes_probe(monkeypatch):
    # An explicitly-pinned host (claude-code env marker) wins over the hermes
    # binary merely being installed — hermes is probed last for this reason.
    monkeypatch.delenv(d.RUNTIME_ENV, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(d.shutil, "which", lambda _b: "/usr/bin/hermes")
    assert d.detect_runtime_name({}) == "claude-code"


def test_hermes_runtime_config_defaults():
    cfg = d.runtime_config("hermes", {})
    assert cfg.command[0] == "hermes"
    assert "-z" in cfg.command
    assert "--yolo" in cfg.command
    assert "{prompt}" in cfg.command
