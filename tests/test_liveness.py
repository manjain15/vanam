"""Telegram loop liveness: heartbeat, systemd watchdog ping, and bounded HTTP calls.

The daemon once stayed "active" for days with its loop stuck inside one call.
These cover the pieces that make that visible and keep the Health and Hevy
helpers from blocking forever.
"""

import os
import socket
import tempfile
from pathlib import Path

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import google_health
import hevy
import jarvis_telegram


def test_beat_touches_the_heartbeat_without_systemd(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    beat = tmp_path / "telegram_heartbeat"
    jarvis_telegram.beat(beat)
    assert beat.exists()
    assert jarvis_telegram._sd_notify("WATCHDOG=1") is False


def test_beat_pings_the_systemd_watchdog(tmp_path, monkeypatch):
    # AF_UNIX paths are short on macOS; keep the socket in a short temp dir.
    short = Path(tempfile.mkdtemp(prefix="sd"))
    addr = str(short / "notify")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        server.bind(addr)
        server.settimeout(2)
        monkeypatch.setenv("NOTIFY_SOCKET", addr)
        jarvis_telegram.beat(tmp_path / "telegram_heartbeat")
        assert server.recv(64) == b"WATCHDOG=1"
    finally:
        server.close()
        os.unlink(addr)
        short.rmdir()


def test_unreachable_notify_socket_is_ignored(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/notify")
    assert jarvis_telegram._sd_notify("WATCHDOG=1") is False


def _capture_urlopen(module, monkeypatch, error=None):
    calls = []

    def fake(req, context=None, timeout=None):
        calls.append(timeout)
        if error:
            raise error
        raise AssertionError("not reached")

    monkeypatch.setattr(module, "urlopen", fake)
    return calls


def test_health_and_hevy_requests_have_a_timeout(monkeypatch):
    for module in (google_health, hevy):
        calls = _capture_urlopen(module, monkeypatch, error=TimeoutError("timed out"))
        try:
            module._urlopen_ssl(module.Request("https://example.invalid/"))
        except TimeoutError:
            pass
        assert calls == [module.HTTP_TIMEOUT]
        assert 0 < module.HTTP_TIMEOUT <= 60


def test_a_timed_out_read_is_a_warning_not_a_hang(monkeypatch):
    _capture_urlopen(google_health, monkeypatch, error=TimeoutError("timed out"))
    assert google_health._get("dataTypes/steps", "token") is None
    assert google_health._post("dataTypes/steps:rollUp", {}, "token") is None
    _capture_urlopen(hevy, monkeypatch, error=TimeoutError("timed out"))
    assert hevy._get("workouts") is None


def test_listener_beats_every_pass(monkeypatch):
    beats, polls = [], []

    def updates(_offset):
        polls.append(1)
        if len(polls) == 3:
            raise KeyboardInterrupt
        return []

    monkeypatch.setattr(jarvis_telegram.config, "TELEGRAM_CHAT_ID", 1)
    monkeypatch.setattr(jarvis_telegram, "build_static_context", lambda: "")
    monkeypatch.setattr(jarvis_telegram, "ensure_bot_display_name", lambda: None)
    monkeypatch.setattr(jarvis_telegram, "send_message", lambda *a, **k: True)
    monkeypatch.setattr(jarvis_telegram, "load_offset", lambda: 0)
    monkeypatch.setattr(jarvis_telegram, "run_enforcer_tick", lambda: None)
    monkeypatch.setattr(jarvis_telegram, "enable_stack_dumps", lambda: None)
    monkeypatch.setattr(jarvis_telegram, "get_updates", updates)
    monkeypatch.setattr(jarvis_telegram, "beat", lambda *a: beats.append(1))
    jarvis_telegram.run_listener()
    assert len(beats) == 3
