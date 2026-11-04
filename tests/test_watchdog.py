"""Watchdog unit list. Does not call systemctl."""

import os

os.environ.setdefault("YOUR_EMAIL", "test@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("HEVY_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import jarvis_telegram
import watchdog


def test_agent_api_is_always_on():
    assert "jarvis-agent-api" in watchdog.ALWAYS_ON
    assert watchdog.ALWAYS_ON == jarvis_telegram.STATUS_ALWAYS_ON


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, _n):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_probe_ok_and_failures():
    def ok(_request, timeout=5):
        return _Resp(200, b'{"ok": true, "service": "jarvis-agent-api"}')

    assert watchdog.probe_agent_api(opener=ok, token="t" * 32, url="http://127.0.0.1:5557/health") is None

    def down(_request, timeout=5):
        raise OSError("connection refused")

    failed = watchdog.probe_agent_api(opener=down, token="t" * 32, url="http://127.0.0.1:5557/health")
    assert failed == "❌ jarvis-agent-api /health probe failed"
    assert "connection refused" not in failed

    import urllib.error

    def denied(_request, timeout=5):
        raise urllib.error.HTTPError("http://127.0.0.1/health", 401, "no", None, None)

    assert "401" in watchdog.probe_agent_api(opener=denied, token="t" * 32, url="http://127.0.0.1:5557/health")
    assert watchdog.probe_agent_api(opener=ok, token="", url="http://127.0.0.1:5557/health") == (
        "❌ jarvis-agent-api /health probe has no API token"
    )


def test_probe_reads_token_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_API_TOKEN", raising=False)
    monkeypatch.delenv("JARVIS_API_TOKENS", raising=False)
    env = tmp_path / ".env"
    env.write_text('JARVIS_API_TOKEN="abc"\n')
    assert watchdog.agent_api_token(env) == "abc"
    env.write_text("JARVIS_API_TOKENS=career:bot-secret,study:other\n")
    assert watchdog.agent_api_token(env) == "bot-secret"


def test_check_problems_includes_http_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "_systemctl", lambda *args: "active")
    monkeypatch.setattr(watchdog, "HEARTBEAT_FILE", tmp_path / "missing")
    usage = type("Usage", (), {"total": 100, "used": 10})()
    monkeypatch.setattr(watchdog.shutil, "disk_usage", lambda _path: usage)
    monkeypatch.setattr(watchdog, "probe_agent_api", lambda: "❌ jarvis-agent-api /health probe failed")
    problems = watchdog.check_problems()
    assert problems == ["❌ jarvis-agent-api /health probe failed"]


def test_repeated_failure_alerts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "STATE_FILE", tmp_path / "state.json")
    calls = []
    monkeypatch.setattr(watchdog, "_notify", lambda text: calls.append(text) or True)
    monkeypatch.setattr(watchdog, "check_problems", lambda: ["❌ jarvis-agent-api /health probe failed"])

    assert watchdog.run() == ["❌ jarvis-agent-api /health probe failed"]
    assert watchdog.run() == ["❌ jarvis-agent-api /health probe failed"]
    assert len(calls) == 1
    assert calls[0].startswith("🚨 Vanam watchdog")

    monkeypatch.setattr(watchdog, "check_problems", lambda: ["❌ jarvis-telegram service is inactive (should be active)"])
    watchdog.run()
    assert len(calls) == 2

    monkeypatch.setattr(watchdog, "check_problems", lambda: [])
    assert watchdog.run() == []
    assert len(calls) == 2

    monkeypatch.setattr(watchdog, "check_problems", lambda: ["❌ jarvis-telegram service is inactive (should be active)"])
    watchdog.run()
    assert len(calls) == 3

    watchdog.run(daily=True)
    watchdog.run(daily=True)
    assert len(calls) == 5


def test_heartbeat_fresh_missing_and_stale(tmp_path):
    beat = tmp_path / "telegram_heartbeat"
    assert watchdog.check_heartbeat(beat) is None  # not written yet
    beat.touch()
    now = beat.stat().st_mtime
    assert watchdog.check_heartbeat(beat, now=now + 44 * 60) is None
    stale = watchdog.check_heartbeat(beat, now=now + 3 * 24 * 3600)
    assert "jarvis-telegram is active but its loop has not run" in stale
    # Same text however long it has been stuck, so it alerts once.
    assert stale == watchdog.check_heartbeat(beat, now=now + 46 * 60)


def test_stuck_telegram_loop_is_a_problem_and_dumps_the_stack(tmp_path, monkeypatch):
    """The daemon stayed "active" for days while its loop was stuck in one call."""
    beat = tmp_path / "telegram_heartbeat"
    beat.touch()
    old = beat.stat().st_mtime - 3 * 24 * 3600
    os.utime(beat, (old, old))
    monkeypatch.setattr(watchdog, "HEARTBEAT_FILE", beat)
    monkeypatch.setattr(watchdog, "_systemctl", lambda *args: "active")
    usage = type("Usage", (), {"total": 100, "used": 10})()
    monkeypatch.setattr(watchdog.shutil, "disk_usage", lambda _path: usage)
    monkeypatch.setattr(watchdog, "probe_agent_api", lambda: None)
    dumped = []
    monkeypatch.setattr(watchdog, "dump_stuck_stack", lambda unit: dumped.append(unit) or True)
    problems = watchdog.check_problems()
    assert len(problems) == 1 and "loop has not run" in problems[0]
    assert dumped == ["jarvis-telegram"]

    beat.touch()
    assert watchdog.check_problems() == []


def test_dump_stuck_stack_signals_the_main_pid(monkeypatch):
    sent = []
    monkeypatch.setattr(watchdog, "_systemctl", lambda *args: "4242")
    monkeypatch.setattr(watchdog.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert watchdog.dump_stuck_stack() is True
    assert sent == [(4242, watchdog.signal.SIGUSR1)]
    sent.clear()
    monkeypatch.setattr(watchdog, "_systemctl", lambda *args: "0")
    assert watchdog.dump_stuck_stack() is False
    assert sent == []
