"""Тесты крона _cron_early_kill (web/app.py): гейт раз в час и дедуп по слоту."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from web.app import _EARLY_KILL_HOURS, _EARLY_KILL_STATE_KEY, _cron_early_kill, _should_run_early_kill

_TZ = timezone(timedelta(hours=5))


@pytest.fixture
def state_store(monkeypatch):
    """Подменяет backfill_state (_get_state_by_key/_save_state_by_key) на словарь в памяти."""
    store: dict = {}
    monkeypatch.setattr("services.creative_backfill._get_state_by_key", lambda key: dict(store.get(key, {})))
    monkeypatch.setattr("services.creative_backfill._save_state_by_key", lambda key, value: store.__setitem__(key, value))
    return store


def test_hours_cover_whole_day():
    assert _EARLY_KILL_HOURS == frozenset(range(24))


def test_gate_runs_once_per_hour_slot(state_store):
    now = datetime(2026, 9, 15, 3, 10, tzinfo=_TZ)
    assert _should_run_early_kill(now) is True
    state_store[_EARLY_KILL_STATE_KEY] = {"slots": {"2026-09-15-3": "running"}}
    assert _should_run_early_kill(now) is False
    assert _should_run_early_kill(now + timedelta(hours=1)) is True


def test_cron_marks_slot_and_calls_run_once(state_store):
    now = datetime(2026, 9, 15, 3, 10, tzinfo=_TZ)
    result = {"ran": True, "skipped": None, "mode": "shadow", "candidates": 2, "would_pause": ["a"], "amo_queries": 1, "errors": []}
    with patch("web.app.datetime") as dt, patch("services.early_kill.run_early_kill", return_value=result) as run:
        dt.now.return_value = now
        _cron_early_kill()
        _cron_early_kill()  # тот же час — повтор не бежит
    assert run.call_count == 1
    slot = state_store[_EARLY_KILL_STATE_KEY]["slots"]["2026-09-15-3"]
    assert slot["ran"] is True and slot["would_pause"] == 1 and slot["mode"] == "shadow"


def test_cron_prunes_old_slots(state_store):
    now = datetime(2026, 9, 15, 3, 10, tzinfo=_TZ)
    state_store[_EARLY_KILL_STATE_KEY] = {"slots": {"2026-09-10-3": True, "2026-09-14-23": True}}
    with patch("web.app.datetime") as dt, patch("services.early_kill.run_early_kill", return_value={"ran": True, "would_pause": []}):
        dt.now.return_value = now
        _cron_early_kill()
    slots = state_store[_EARLY_KILL_STATE_KEY]["slots"]
    assert "2026-09-10-3" not in slots and "2026-09-14-23" in slots and "2026-09-15-3" in slots


def test_cron_exception_does_not_raise(state_store):
    now = datetime(2026, 9, 15, 3, 10, tzinfo=_TZ)
    with patch("web.app.datetime") as dt, \
         patch("services.early_kill.run_early_kill", side_effect=RuntimeError("boom")), \
         patch("web.app.report_cron_failure") as failure:
        dt.now.return_value = now
        _cron_early_kill()
    failure.assert_called_once()
