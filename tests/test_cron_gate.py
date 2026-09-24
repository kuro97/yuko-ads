"""
Юнит-тесты для общего крон-гейт хелпера web/cron_gate.py::should_run_hourly_slot.

Проверяем контракт (идентичный текущим _should_run_*_cron в web/app.py):
- час вне allowed_hours → False
- час в наборе, слот ещё не отмечен в state → True
- час в наборе, слот уже отмечен в state["slots"] → False
- независимость разных слотов и дат
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from web.cron_gate import should_run_hourly_slot

_TZ = timezone(timedelta(hours=5))


def _dt(hour: int, day: int = 2, month: int = 7, year: int = 2026) -> datetime:
    """Создаёт datetime для указанного часа CityA (дефолт — 2026-07-02)."""
    return datetime(year, month, day, hour, 15, tzinfo=_TZ)


class TestShouldRunHourlySlot:
    """Тесты гейта should_run_hourly_slot."""

    def test_hour_outside_allowed_set_returns_false(self):
        """Час=3, allowed={8,10} → False (даже при пустом state)."""
        now = _dt(hour=3)
        result = should_run_hourly_slot(now, allowed_hours={8, 10}, state={})
        assert result is False

    def test_hour_in_set_slot_free_returns_true(self):
        """Час=8, allowed={8}, state без слотов → True."""
        now = _dt(hour=8)
        result = should_run_hourly_slot(now, allowed_hours={8}, state={})
        assert result is True

    def test_hour_in_set_slot_taken_returns_false(self):
        """Час=8, слот 2026-07-02-8 уже отмечен в state → False."""
        now = _dt(hour=8)
        state = {"slots": {"2026-07-02-8": True}}
        result = should_run_hourly_slot(now, allowed_hours={8}, state=state)
        assert result is False

    def test_frozenset_allowed_hours_works(self):
        """allowed_hours как frozenset (тип из сигнатуры) работает так же, как set."""
        now = _dt(hour=6)
        result = should_run_hourly_slot(now, allowed_hours=frozenset({0, 6, 12, 18}), state={})
        assert result is True

    def test_empty_allowed_hours_always_false(self):
        """Пустой allowed_hours → всегда False, независимо от часа."""
        now = _dt(hour=0)
        result = should_run_hourly_slot(now, allowed_hours=set(), state={})
        assert result is False

    def test_missing_slots_key_treated_as_no_slots(self):
        """state без ключа 'slots' — не падает, трактуется как пустой словарь."""
        now = _dt(hour=8)
        result = should_run_hourly_slot(now, allowed_hours={8}, state={"other_key": 1})
        assert result is True

    def test_different_hour_slot_independent(self):
        """Слот часа 12 занят, но слот часа 18 свободен → True для 18."""
        now_18 = _dt(hour=18)
        state = {"slots": {"2026-07-02-12": True}}
        result = should_run_hourly_slot(now_18, allowed_hours={12, 18}, state=state)
        assert result is True

    def test_different_date_same_hour_independent(self):
        """Слот занят вчера (2026-07-01-8), сегодня (2026-07-02-8) — свободен."""
        now = _dt(hour=8, day=2)
        state = {"slots": {"2026-07-01-8": True}}
        result = should_run_hourly_slot(now, allowed_hours={8}, state=state)
        assert result is True

    def test_all_allowed_hours_fire_once_per_day(self):
        """Каждый час из набора срабатывает один раз, повтор в тот же час → False."""
        allowed = {0, 6, 12, 18}
        slots: dict = {}
        fired = []

        for hour in sorted(allowed):
            now = _dt(hour=hour)
            state = {"slots": slots}
            should = should_run_hourly_slot(now, allowed_hours=allowed, state=state)
            if should:
                fired.append(hour)
                slots[f"{now.date().isoformat()}-{hour}"] = True

            # Повторный тик в тот же час — уже занят
            should_again = should_run_hourly_slot(now, allowed_hours=allowed, state={"slots": slots})
            assert should_again is False, f"Повтор час={hour} должен возвращать False"

        assert sorted(fired) == sorted(allowed)

    def test_slot_marked_falsy_still_treated_as_free(self):
        """Слот в state со значением False — не считается выполненным (как в текущих кронах)."""
        now = _dt(hour=8)
        state = {"slots": {"2026-07-02-8": False}}
        result = should_run_hourly_slot(now, allowed_hours={8}, state=state)
        assert result is True
