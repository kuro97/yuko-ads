"""
Тесты гейт-функций match_outcomes и autopilot_live для нескольких часовых слотов (Часть B).

Проверяем:
- _should_run_match_outcomes: True в {0, 6, 12, 18}, False в других часах
- _should_run_autopilot_live: True в {8, 10, 12, 14, 16, 18, 20, 22}, False в других часах
- Повтор в тот же час не дублирует (дедуп по слоту)
- Разные слоты в один день запускаются независимо
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())

_TZ = timezone(timedelta(hours=5))


# ---------------------------------------------------------------------------
# Вспомогательная функция
# ---------------------------------------------------------------------------

def _dt(hour: int, day: int = 24) -> datetime:
    """Создаёт datetime для 2026-06-{day} {hour}:15 CityA."""
    return datetime(2026, 6, day, hour, 15, tzinfo=_TZ)


# ---------------------------------------------------------------------------
# БЛОК 1: _should_run_match_outcomes — часы {0, 6, 12, 18}
# ---------------------------------------------------------------------------

class TestMatchOutcomesGate:
    """Тесты гейта _should_run_match_outcomes с множеством часовых слотов."""

    VALID_HOURS = {0, 6, 12, 18}
    ALL_HOURS = set(range(24))
    INVALID_HOURS = ALL_HOURS - VALID_HOURS

    def test_returns_true_at_valid_hours_no_state(self, tmp_path):
        """True при допустимых часах {0, 6, 12, 18} без state-файла."""
        from web.app import _should_run_match_outcomes

        for hour in self.VALID_HOURS:
            now = _dt(hour)
            with patch("web.app._MATCH_OUTCOMES_STATE", tmp_path / f"state_{hour}.json"):
                result = _should_run_match_outcomes(now)
            assert result is True, f"Ожидали True для час={hour}, получили False"

    @pytest.mark.parametrize("hour", sorted({1, 2, 3, 4, 5, 7, 8, 9, 10, 11,
                                              13, 14, 15, 16, 17, 19, 20, 21, 22, 23}))
    def test_returns_false_at_invalid_hours(self, hour, tmp_path):
        """False при любом часу не из {0, 6, 12, 18}."""
        from web.app import _should_run_match_outcomes

        now = _dt(hour)
        with patch("web.app._MATCH_OUTCOMES_STATE", tmp_path / "state.json"):
            result = _should_run_match_outcomes(now)
        assert result is False, f"Ожидали False для час={hour}, получили True"

    def test_dedup_same_slot_returns_false(self, tmp_path):
        """Повтор в тот же слот (час == 0, уже запускался) → False."""
        from web.app import _should_run_match_outcomes, _save_match_outcomes_state

        state_file = tmp_path / "match_outcomes_state.json"
        # Записываем что слот 2026-06-24-0 уже выполнен
        state_file.write_text(
            json.dumps({"slots": {"2026-06-24-0": True}}),
            encoding="utf-8",
        )

        now = _dt(hour=0, day=24)
        with patch("web.app._MATCH_OUTCOMES_STATE", state_file):
            result = _should_run_match_outcomes(now)

        assert result is False, "Повтор в тот же слот должен возвращать False"

    def test_different_slots_run_independently(self, tmp_path):
        """Слот 12 выполнен, но слот 18 ещё нет → True для 18."""
        from web.app import _should_run_match_outcomes

        state_file = tmp_path / "match_outcomes_state.json"
        # Слот 12 уже был, слот 18 — ещё нет
        state_file.write_text(
            json.dumps({"slots": {"2026-06-24-12": True}}),
            encoding="utf-8",
        )

        now_18 = _dt(hour=18, day=24)
        with patch("web.app._MATCH_OUTCOMES_STATE", state_file):
            result = _should_run_match_outcomes(now_18)

        assert result is True, "Слот 18 должен быть доступен, если только 12 уже выполнен"

    def test_all_four_slots_fire_once_per_day(self, tmp_path):
        """Каждый из 4 слотов {0, 6, 12, 18} срабатывает ровно один раз в день."""
        from web.app import (
            _should_run_match_outcomes,
            _save_match_outcomes_state,
            _load_match_outcomes_state,
        )

        state_file = tmp_path / "match_outcomes_state.json"
        fired = []

        for hour in sorted(self.VALID_HOURS):
            now = _dt(hour=hour, day=24)
            with patch("web.app._MATCH_OUTCOMES_STATE", state_file):
                should = _should_run_match_outcomes(now)
                if should:
                    fired.append(hour)
                    # Имитируем сохранение после запуска
                    st = _load_match_outcomes_state()
                    slots = st.get("slots", {})
                    slots[f"2026-06-24-{hour}"] = True
                    _save_match_outcomes_state({"slots": slots})

                # Второй вызов в тот же час — должен вернуть False
                should_again = _should_run_match_outcomes(now)
                assert should_again is False, f"Повтор час={hour} должен возвращать False"

        assert len(fired) == 4, f"Ожидали 4 срабатывания, получили: {fired}"
        assert sorted(fired) == sorted(self.VALID_HOURS)


# ---------------------------------------------------------------------------
# БЛОК 2: _should_run_autopilot_live — часы {9, 13, 17, 21}
# ---------------------------------------------------------------------------

class TestAutopilotLiveGate:
    """Тесты гейта _should_run_autopilot_live с множеством часовых слотов.

    ARCH-phase1-guardian (G8): расписание учащено с {9,13,17,21} (4×/день)
    до {8,10,12,14,16,18,20,22} (каждые 2ч, 08-22 CityA, 8×/день) — свежий
    слив должен ловиться в течение дня открутки, а не через сутки.
    """

    VALID_HOURS = {8, 10, 12, 14, 16, 18, 20, 22}
    INVALID_HOURS = set(range(24)) - VALID_HOURS

    def test_returns_true_at_valid_hours_no_state(self, tmp_path):
        """True при допустимых часах {8,10,...,22} без state-файла."""
        from web.app import _should_run_autopilot_live

        for hour in self.VALID_HOURS:
            now = _dt(hour)
            with patch("web.app._AUTOPILOT_LIVE_STATE", tmp_path / f"state_{hour}.json"):
                result = _should_run_autopilot_live(now)
            assert result is True, f"Ожидали True для час={hour}, получили False"

    @pytest.mark.parametrize("hour", sorted(set(range(24)) - {8, 10, 12, 14, 16, 18, 20, 22}))
    def test_returns_false_at_invalid_hours(self, hour, tmp_path):
        """False при любом часу не из {8,10,...,22}."""
        from web.app import _should_run_autopilot_live

        now = _dt(hour)
        with patch("web.app._AUTOPILOT_LIVE_STATE", tmp_path / "state.json"):
            result = _should_run_autopilot_live(now)
        assert result is False, f"Ожидали False для час={hour}, получили True"

    def test_dedup_same_slot_returns_false(self, tmp_path):
        """Повтор в тот же слот (час == 8, уже запускался) → False."""
        from web.app import _should_run_autopilot_live

        state_file = tmp_path / "autopilot_live_state.json"
        # Записываем что слот 2026-06-24-8 уже выполнен
        state_file.write_text(
            json.dumps({"slots": {"2026-06-24-8": {"ran": True}}}),
            encoding="utf-8",
        )

        now = _dt(hour=8, day=24)
        with patch("web.app._AUTOPILOT_LIVE_STATE", state_file):
            result = _should_run_autopilot_live(now)

        assert result is False, "Повтор в тот же слот должен возвращать False"

    def test_different_slots_run_independently(self, tmp_path):
        """Слот 8 выполнен, но слот 10 ещё нет → True для 10."""
        from web.app import _should_run_autopilot_live

        state_file = tmp_path / "autopilot_live_state.json"
        # Слот 8 уже был, слот 10 — ещё нет
        state_file.write_text(
            json.dumps({"slots": {"2026-06-24-8": {"ran": True}}}),
            encoding="utf-8",
        )

        now_10 = _dt(hour=10, day=24)
        with patch("web.app._AUTOPILOT_LIVE_STATE", state_file):
            result = _should_run_autopilot_live(now_10)

        assert result is True, "Слот 10 должен быть доступен, если только 8 уже выполнен"

    def test_all_four_slots_fire_once_per_day(self, tmp_path):
        """Каждый из 8 слотов {8,10,...,22} срабатывает ровно один раз в день."""
        from web.app import (
            _should_run_autopilot_live,
            _save_autopilot_live_state,
            _load_autopilot_live_state,
        )

        state_file = tmp_path / "autopilot_live_state.json"
        fired = []

        for hour in sorted(self.VALID_HOURS):
            now = _dt(hour=hour, day=24)
            with patch("web.app._AUTOPILOT_LIVE_STATE", state_file):
                should = _should_run_autopilot_live(now)
                if should:
                    fired.append(hour)
                    # Имитируем сохранение после запуска
                    st = _load_autopilot_live_state()
                    slots = st.get("slots", {})
                    slots[f"2026-06-24-{hour}"] = {"ran": True}
                    _save_autopilot_live_state({**st, "slots": slots})

                # Второй вызов в тот же час — должен вернуть False
                should_again = _should_run_autopilot_live(now)
                assert should_again is False, f"Повтор час={hour} должен возвращать False"

        assert len(fired) == 8, f"Ожидали 8 срабатываний, получили: {fired}"
        assert sorted(fired) == sorted(self.VALID_HOURS)
