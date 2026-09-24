"""
Тесты крон-гейтов Стража (web/app.py), не покрытых другими файлами:

- _should_run_guardian_sweep / _cron_guardian_sweep (наблюдатель, вызывает
  guardian.run_guardian_sweep, дедуп по слоту дата-час, исключение не роняет)
- _cron_data_freshness_watchdog (сторож свежести данных: алерт >4ч в рабочее
  время, тишина при None/свежести/ночью, дедуп 3ч)

Новые слоты _should_run_autopilot_live (8 часов 08-22) уже покрыты в
tests/test_cron_gates_multi_hour.py — не дублируем.
Light-vs-full рефреш по часу + mark_spend_refresh_ok уже покрыты в
tests/test_spend_refresh.py (test_cron_gate_*) — не дублируем.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем тяжёлые зависимости ДО импорта web.app
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from web.app import (
    _should_run_guardian_sweep,
    _load_guardian_sweep_state,
    _save_guardian_sweep_state,
    _cron_guardian_sweep,
    _cron_data_freshness_watchdog,
    _AUTOPILOT_LIVE_HOURS,
    _FRESHNESS_MAX_AGE_HOURS,
    _FRESHNESS_ALERT_COOLDOWN_HOURS,
    _TZ_LOCAL,
)

_TZ = timezone(timedelta(hours=5))


def _dt(hour: int, minute: int = 15, day: int = 24) -> datetime:
    """Создаёт datetime для 2026-06-{day} {hour}:{minute} по локальному времени."""
    return datetime(2026, 6, day, hour, minute, tzinfo=_TZ)


# ---------------------------------------------------------------------------
# Фикстура: изолированный state-файл guardian_sweep
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_sweep_state(tmp_path, monkeypatch):
    """Подменяет путь к state-файлу крона guardian_sweep на временный."""
    state_file = tmp_path / "guardian_sweep_cron_state.json"
    monkeypatch.setattr("web.app._GUARDIAN_SWEEP_STATE", state_file)
    return state_file


@pytest.fixture
def isolated_freshness_state(monkeypatch):
    """Сбрасывает in-memory гейт «новый час» сторожа свежести перед каждым тестом."""
    monkeypatch.setattr("web.app._DATA_FRESHNESS_CRON_STATE", {"last_hour": None})
    yield


# ---------------------------------------------------------------------------
# _should_run_guardian_sweep — те же 8 слотов, что и autopilot_live
# ---------------------------------------------------------------------------

class TestShouldRunGuardianSweep:
    VALID_HOURS = {8, 10, 12, 14, 16, 18, 20, 22}
    INVALID_HOURS = set(range(24)) - VALID_HOURS

    def test_использует_те_же_часы_что_autopilot_live(self):
        """Слоты guardian_sweep = _AUTOPILOT_LIVE_HOURS (по дизайну §6.4 спеки)."""
        assert self.VALID_HOURS == set(_AUTOPILOT_LIVE_HOURS)

    def test_час_вне_слотов_не_бежит(self, isolated_sweep_state):
        """Час вне {8,10,...,22} → False."""
        for hour in sorted(self.INVALID_HOURS):
            now = _dt(hour)
            assert _should_run_guardian_sweep(now) is False, f"час={hour} должен быть False"

    def test_слот_впервые_бежит(self, isolated_sweep_state):
        """Час в слоте, state пустой → True."""
        for hour in sorted(self.VALID_HOURS):
            now = _dt(hour)
            assert _should_run_guardian_sweep(now) is True, f"час={hour} должен быть True"

    def test_повторный_тик_в_том_же_часу_не_бежит(self, isolated_sweep_state):
        """Слот уже помечен в state (дата-час) → повторный тик False."""
        now = _dt(12, day=24)
        slot_key = f"{now.date().isoformat()}-{now.hour}"
        _save_guardian_sweep_state({"slots": {slot_key: "running"}})

        assert _should_run_guardian_sweep(now) is False

    def test_другой_слот_в_тот_же_день_бежит_независимо(self, isolated_sweep_state):
        """Слот 12 занят, слот 14 свободен → True для 14."""
        now_12 = _dt(12, day=24)
        slot_key_12 = f"{now_12.date().isoformat()}-12"
        _save_guardian_sweep_state({"slots": {slot_key_12: "running"}})

        now_14 = _dt(14, day=24)
        assert _should_run_guardian_sweep(now_14) is True


# ---------------------------------------------------------------------------
# _cron_guardian_sweep — вызывает guardian.run_guardian_sweep, дедуп, try/except
# ---------------------------------------------------------------------------

class TestCronGuardianSweep:
    _SWEEP_RESULT = {
        "ran": True, "skipped": None, "analyzed": 3,
        "early_candidates": ["ad1"], "wnc_candidates": [], "paused": [],
        "dry_run_only": ["ad1"], "errors": [],
    }

    def test_слот_вне_часов_не_вызывает_run_guardian_sweep(self, isolated_sweep_state):
        """Час вне 8 слотов → guardian.run_guardian_sweep не вызывается."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.run_guardian_sweep") as mock_run:
            mock_dt.now.return_value = _dt(9)  # 9 не входит в _AUTOPILOT_LIVE_HOURS
            _cron_guardian_sweep()

        mock_run.assert_not_called()

    def test_слот_впервые_вызывает_run_guardian_sweep(self, isolated_sweep_state):
        """Слот свободен → run_guardian_sweep вызван ровно один раз, trigger='cron'."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.run_guardian_sweep", return_value=self._SWEEP_RESULT) as mock_run:
            mock_dt.now.return_value = _dt(8)
            _cron_guardian_sweep()

        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        args, kwargs = mock_run.call_args
        assert kwargs.get("trigger") == "cron" or args == ("cron",)

    def test_повторный_тик_в_том_же_часу_не_вызывает_повторно(self, isolated_sweep_state):
        """Первый тик запускает sweep и помечает слот; второй тик в том же часу — не вызывает."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.run_guardian_sweep", return_value=self._SWEEP_RESULT) as mock_run:
            mock_dt.now.return_value = _dt(10, minute=5)
            _cron_guardian_sweep()
            mock_dt.now.return_value = _dt(10, minute=20)
            _cron_guardian_sweep()

        mock_run.assert_called_once()

    def test_слот_помечается_до_запуска_run_guardian_sweep(self, isolated_sweep_state):
        """State помечается ДО вызова run_guardian_sweep — защита от дублей
        при долгом выполнении (тот же паттерн, что у autopilot_live/adset_cleaner)."""
        call_order = []

        def fake_save_state(state):
            call_order.append(("save", state))

        def fake_run_sweep(trigger="cron"):
            call_order.append(("run",))
            return self._SWEEP_RESULT

        with patch("web.app.datetime") as mock_dt, \
             patch("web.app._save_guardian_sweep_state", side_effect=fake_save_state), \
             patch("web.app._load_guardian_sweep_state", return_value={}), \
             patch("services.guardian.run_guardian_sweep", side_effect=fake_run_sweep):
            mock_dt.now.return_value = _dt(16)
            _cron_guardian_sweep()

        # Первый save (slots[slot]='running') должен произойти раньше run
        save_before_run = next(i for i, c in enumerate(call_order) if c[0] == "save")
        run_index = next(i for i, c in enumerate(call_order) if c[0] == "run")
        assert save_before_run < run_index

    def test_исключение_внутри_run_guardian_sweep_не_роняет_крон(self, isolated_sweep_state):
        """Исключение из guardian.run_guardian_sweep не должно проваливаться наружу."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.run_guardian_sweep", side_effect=RuntimeError("boom")):
            mock_dt.now.return_value = _dt(18)
            # Не должно бросить исключение
            _cron_guardian_sweep()

    def test_результат_слота_сохраняется_в_state(self, isolated_sweep_state):
        """После успешного прогона в state записан summary слота (ran/skipped/analyzed/...)."""
        now = _dt(20, day=24)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.run_guardian_sweep", return_value=self._SWEEP_RESULT):
            mock_dt.now.return_value = now
            _cron_guardian_sweep()

        state = _load_guardian_sweep_state()
        slot_key = f"{now.date().isoformat()}-20"
        assert slot_key in state.get("slots", {})
        slot_summary = state["slots"][slot_key]
        assert slot_summary["ran"] is True
        assert slot_summary["analyzed"] == 3
        assert slot_summary["early"] == 1
        assert slot_summary["wnc"] == 0


# ---------------------------------------------------------------------------
# _cron_data_freshness_watchdog
# ---------------------------------------------------------------------------

class TestCronDataFreshnessWatchdog:
    def test_возраст_none_молчит(self, isolated_freshness_state):
        """spend_refresh_age_hours вернул None (ещё не было прогонов) → нет алерта."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.spend_refresh_age_hours", return_value=None), \
             patch("services.notifications.send_telegram") as mock_tg:
            mock_dt.now.return_value = _dt(14)
            _cron_data_freshness_watchdog()

        mock_tg.assert_not_called()

    def test_5ч_в_рабочее_время_алерт(self, isolated_freshness_state):
        """Возраст 5ч (> порога 4ч) в рабочее время (08-22) → send_telegram(channel='health')."""
        assert _FRESHNESS_MAX_AGE_HOURS == 4.0
        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.spend_refresh_age_hours", return_value=5.0), \
             patch("services.guardian._load_guardian_state", return_value={"freshness_alert_at": None}), \
             patch("services.guardian._save_guardian_state") as mock_save, \
             patch("services.notifications.send_telegram") as mock_tg:
            mock_dt.now.return_value = _dt(14)
            _cron_data_freshness_watchdog()

        mock_tg.assert_called_once()
        args, kwargs = mock_tg.call_args
        assert kwargs.get("channel") == "health"
        mock_save.assert_called_once()

    def test_свежесть_меньше_порога_молчит(self, isolated_freshness_state):
        """Возраст 1ч (<= порога 4ч) → тишина, алерт не шлётся."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.spend_refresh_age_hours", return_value=1.0), \
             patch("services.notifications.send_telegram") as mock_tg:
            mock_dt.now.return_value = _dt(14)
            _cron_data_freshness_watchdog()

        mock_tg.assert_not_called()

    def test_ночью_молчит_без_проверки_возраста(self, isolated_freshness_state):
        """Час 23 (вне 08-22) → выходит без проверки возраста, тишина."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.spend_refresh_age_hours") as mock_age, \
             patch("services.notifications.send_telegram") as mock_tg:
            mock_dt.now.return_value = _dt(23)
            _cron_data_freshness_watchdog()

        mock_age.assert_not_called()
        mock_tg.assert_not_called()

    def test_ночью_час_3_молчит(self, isolated_freshness_state):
        """Час 3 (глубокая ночь, вне 08-22) → тишина."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.spend_refresh_age_hours") as mock_age, \
             patch("services.notifications.send_telegram") as mock_tg:
            mock_dt.now.return_value = _dt(3)
            _cron_data_freshness_watchdog()

        mock_age.assert_not_called()
        mock_tg.assert_not_called()

    def test_дедуп_алерт_недавно_отправлен_молчит(self, isolated_freshness_state):
        """freshness_alert_at было 1ч назад (< кулдауна 3ч) → повторно не шлём."""
        assert _FRESHNESS_ALERT_COOLDOWN_HOURS == 3
        now = _dt(16)
        recent_alert = (now - timedelta(hours=1)).isoformat()

        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.spend_refresh_age_hours", return_value=5.0), \
             patch("services.guardian._load_guardian_state",
                   return_value={"freshness_alert_at": recent_alert}), \
             patch("services.notifications.send_telegram") as mock_tg:
            mock_dt.now.return_value = now
            # fromisoformat должен реально парсить (мок класса datetime не трогает статику)
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            _cron_data_freshness_watchdog()

        mock_tg.assert_not_called()

    def test_дедуп_истёк_шлёт_снова(self, isolated_freshness_state):
        """freshness_alert_at было 4ч назад (>= кулдауна 3ч) → алерт шлётся снова."""
        now = _dt(16)
        old_alert = (now - timedelta(hours=4)).isoformat()

        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.spend_refresh_age_hours", return_value=5.0), \
             patch("services.guardian._load_guardian_state",
                   return_value={"freshness_alert_at": old_alert}), \
             patch("services.guardian._save_guardian_state") as mock_save, \
             patch("services.notifications.send_telegram") as mock_tg:
            mock_dt.now.return_value = now
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            _cron_data_freshness_watchdog()

        mock_tg.assert_called_once()
        mock_save.assert_called_once()

    def test_повторный_тик_в_том_же_часу_не_проверяет_снова(self, isolated_freshness_state):
        """In-memory гейт «новый час»: второй тик в том же часовом окне не идёт
        дальше проверки возраста (даже без изменения state-дедупа)."""
        now_1 = _dt(14, minute=5)
        now_2 = _dt(14, minute=20)

        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.spend_refresh_age_hours", return_value=5.0) as mock_age, \
             patch("services.guardian._load_guardian_state", return_value={"freshness_alert_at": None}), \
             patch("services.guardian._save_guardian_state"), \
             patch("services.notifications.send_telegram") as mock_tg:
            mock_dt.now.return_value = now_1
            _cron_data_freshness_watchdog()
            mock_dt.now.return_value = now_2
            _cron_data_freshness_watchdog()

        # Возраст проверяется только один раз (второй тик блокируется гейтом часа)
        assert mock_age.call_count == 1
        mock_tg.assert_called_once()

    def test_исключение_внутри_не_роняет_крон(self, isolated_freshness_state):
        """Исключение из spend_refresh_age_hours не должно проваливаться наружу."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.spend_refresh_age_hours", side_effect=RuntimeError("boom")):
            mock_dt.now.return_value = _dt(14)
            # Не должно бросить исключение
            _cron_data_freshness_watchdog()
