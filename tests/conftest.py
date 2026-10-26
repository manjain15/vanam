"""Shared pytest setup: put the repo root on sys.path so tests can import
Jarvis's top-level modules (study_tracker, uni_timetable, etc.)."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


import pytest


@pytest.fixture(autouse=True)
def _no_real_google_health(monkeypatch):
    """
    Tests never reach the real Google Health API. Closing a day reconciles
    steps with Health, and on a machine with real credentials an unstubbed
    test would read Manav's actual step counts. A test that exercises Health
    patches get_access_token / fetch_steps itself, which overrides this.
    """
    try:
        import google_health
    except Exception:
        return

    def _offline():
        raise RuntimeError("Google Health is not reachable in tests")
    monkeypatch.setattr(google_health, "get_access_token", _offline)


@pytest.fixture(autouse=True)
def _isolated_spending_sources(tmp_path, monkeypatch):
    """
    Spending tests never read the real Back Tap log or the reselling sheet.
    A test that needs logged spends writes to the patched SPEND_FILE; one
    that needs commission rows patches finance_tracker._load_sheet_items.
    """
    try:
        import live_spend
        monkeypatch.setattr(live_spend, "SPEND_FILE", tmp_path / "live_spend.jsonl")
    except Exception:
        pass
    try:
        import finance_tracker
        monkeypatch.setattr(finance_tracker, "_load_sheet_items", lambda: [])
    except Exception:
        pass
