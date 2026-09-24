"""
Тесты крон-функции _cron_budget_scaler (web/app.py).

Проверяем:
- гейт: крон запускается только в час==13 (CityA)
- дедупликация: раз в день через state-файл
- крон вызывает run_budget_scaling с mode="active"
- повторный тик в тот же день не вызывает run_budget_scaling второй раз
- исключение внутри не рушит lifespan (try/except)
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Мокаем тяжёлые зависимости ДО импорта web.app
sys.path.insert(0, str(Path(__file__).parent.parent))

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from web.app import (
    _should_run_budget_scaler_cron,
    _load_budget_scaler_cron_state,
    _save_budget_scaler_cron_state,
    _cron_budget_scaler,
    _BUDGET_SCALER_CRON_STATE,
    _TZ_LOCAL,
)


# ---------------------------------------------------------------------------
# Фикстура: изолированный state-файл
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Подменяет путь к state-файлу на временный каталог."""
    state_file = tmp_path / "budget_scaler_cron_state.json"
    monkeypatch.setattr("web.app._BUDGET_SCALER_CRON_STATE", state_file)
    return state_file


# ---------------------------------------------------------------------------
# Тесты _should_run_budget_scaler_cron
# ---------------------------------------------------------------------------

class TestShouldRunBudgetScalerCron:
    def _now(self, hour: int, date_str: str = "2026-06-25") -> datetime:
        """Вспомогательный метод: datetime в TZ CityA с заданным часом."""
        return datetime.fromisoformat(f"{date_str}T{hour:02d}:07:00").replace(
            tzinfo=_TZ_LOCAL
        )

    def test_гейт_час_13_проходит(self, isolated_state):
        """Час 13 CityA → _should_run_budget_scaler_cron возвращает True (если не запускался)."""
        now = self._now(13)
        assert _should_run_budget_scaler_cron(now) is True

    def test_гейт_другой_час_не_проходит(self, isolated_state):
        """Любой час кроме 13 → False."""
        for hour in (0, 9, 10, 12, 14, 20, 23):
            now = self._now(hour)
            assert _should_run_budget_scaler_cron(now) is False, (
                f"час {hour} должен быть заблокирован"
            )

    def test_раз_в_день_дедупликация(self, isolated_state):
        """Если уже запускался сегодня (last_run_date == today) → False."""
        today_str = "2026-06-25"
        _save_budget_scaler_cron_state({"last_run_date": today_str})
        now = self._now(13, date_str=today_str)
        assert _should_run_budget_scaler_cron(now) is False

    def test_другой_день_в_state_проходит(self, isolated_state):
        """Если last_run_date = вчера → сегодня True."""
        _save_budget_scaler_cron_state({"last_run_date": "2026-06-24"})
        now = self._now(13, date_str="2026-06-25")
        assert _should_run_budget_scaler_cron(now) is True

    def test_пустой_state_проходит(self, isolated_state):
        """Нет state-файла → тоже True (первый запуск)."""
        now = self._now(13)
        assert _should_run_budget_scaler_cron(now) is True


# ---------------------------------------------------------------------------
# Тесты _cron_budget_scaler: вызов run_budget_scaling с mode="active"
# ---------------------------------------------------------------------------

class TestCronBudgetScalerMode:
    """Проверяем что крон вызывает run_budget_scaling с mode='active'."""

    def _make_now(self, hour: int = 13) -> datetime:
        return datetime(2026, 6, 25, hour, 5, 0, tzinfo=_TZ_LOCAL)

    def test_крон_вызывает_run_budget_scaling_с_mode_active(self, isolated_state):
        """_cron_budget_scaler вызывает run_budget_scaling(mode='active', ...)."""
        mock_result = {
            "ran": True,
            "skipped_reason": None,
            "mode": "active",
            "winners": [],
            "recommendations": [],
            "scaled": [],
            "errors": [],
        }
        cfg = {"max_scales_per_run": 2}

        with patch("web.app.datetime") as mock_dt, \
             patch("web.app._load_budget_scaler_cron_state", return_value={}), \
             patch("web.app._save_budget_scaler_cron_state"), \
             patch("services.budget_scaler.get_scale_config", return_value=cfg), \
             patch("services.budget_scaler.run_budget_scaling", return_value=mock_result) as mock_run:

            mock_dt.now.return_value = self._make_now(13)
            _cron_budget_scaler()

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        called_mode = args[0] if args else kwargs.get("mode")
        assert called_mode == "active", f"ожидался mode='active', получен '{called_mode}'"

    def test_max_scales_берётся_из_конфига(self, isolated_state):
        """max_scales в вызове run_budget_scaling == max_scales_per_run из конфига."""
        mock_result = {
            "ran": True, "skipped_reason": None, "mode": "active",
            "winners": [], "recommendations": [], "scaled": [], "errors": [],
        }
        cfg = {"max_scales_per_run": 3}

        with patch("web.app.datetime") as mock_dt, \
             patch("web.app._load_budget_scaler_cron_state", return_value={}), \
             patch("web.app._save_budget_scaler_cron_state"), \
             patch("services.budget_scaler.get_scale_config", return_value=cfg), \
             patch("services.budget_scaler.run_budget_scaling", return_value=mock_result) as mock_run:

            mock_dt.now.return_value = self._make_now(13)
            _cron_budget_scaler()

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        called_max = kwargs.get("max_scales") if "max_scales" in kwargs else (args[1] if len(args) > 1 else None)
        assert called_max == 3


# ---------------------------------------------------------------------------
# Тесты: гейт — крон не вызывает run_budget_scaling если час != 13 или уже запускался
# ---------------------------------------------------------------------------

class TestCronBudgetScalerGate:
    def test_час_не_13_не_вызывает_run_budget_scaling(self, isolated_state):
        """В час != 13 крон ничего не делает (run_budget_scaling не вызывается)."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.budget_scaler.run_budget_scaling") as mock_run:
            mock_dt.now.return_value = datetime(2026, 6, 25, 12, 30, 0, tzinfo=_TZ_LOCAL)
            _cron_budget_scaler()

        mock_run.assert_not_called()

    def test_повторный_тик_не_вызывает_run_budget_scaling(self, isolated_state):
        """Если state содержит сегодняшнюю дату — крон не вызывает run_budget_scaling."""
        _save_budget_scaler_cron_state({"last_run_date": "2026-06-25"})

        with patch("web.app.datetime") as mock_dt, \
             patch("services.budget_scaler.run_budget_scaling") as mock_run:
            mock_dt.now.return_value = datetime(2026, 6, 25, 13, 7, 0, tzinfo=_TZ_LOCAL)
            _cron_budget_scaler()

        mock_run.assert_not_called()

    def test_исключение_внутри_не_рушит_крон(self, isolated_state):
        """Исключение в run_budget_scaling не должно проваливаться наружу (try/except)."""
        with patch("web.app.datetime") as mock_dt, \
             patch("web.app._load_budget_scaler_cron_state", return_value={}), \
             patch("web.app._save_budget_scaler_cron_state"), \
             patch("services.budget_scaler.get_scale_config", side_effect=RuntimeError("test error")):
            mock_dt.now.return_value = datetime(2026, 6, 25, 13, 7, 0, tzinfo=_TZ_LOCAL)
            # Не должно бросить исключение
            _cron_budget_scaler()


# ---------------------------------------------------------------------------
# Задача D: таймаут прогона (20 мин) + пометка дня ПОСЛЕ успеха
# ---------------------------------------------------------------------------

class TestCronBudgetScalerTimeout:
    """Прогон скейлера завис 1ч43м (FB flaky) → таймаут-обёртка прерывает за 20 мин,
    шлёт алерт и НЕ помечает день выполненным (будет ретрай)."""

    def _make_now(self, hour: int = 13) -> datetime:
        return datetime(2026, 6, 25, hour, 5, 0, tzinfo=_TZ_LOCAL)

    def test_таймаут_прерывает_алертит_и_не_помечает_день(self, isolated_state, monkeypatch):
        # Сжимаем таймаут до 0.3с, а прогон «висит» 3с → сработает прерывание
        monkeypatch.setattr("web.app._SCALER_TIMEOUT_SEC", 0.3)

        def slow_run(**kwargs):
            time.sleep(3)
            return {"ran": True, "winners": [], "scaled": [], "skipped_reason": None}

        with patch("web.app.datetime") as mock_dt, \
             patch("services.budget_scaler.get_scale_config", return_value={"max_scales_per_run": 2}), \
             patch("services.budget_scaler.run_budget_scaling", side_effect=slow_run), \
             patch("services.notifications.send_critical_alert") as mock_alert:
            mock_dt.now.return_value = self._make_now(13)
            _cron_budget_scaler()

        # Алерт про прерванный скейлер ушёл
        mock_alert.assert_called_once()
        title = mock_alert.call_args[0][0].lower()
        assert "скейлер" in title and "прерван" in title

        # День НЕ помечен выполненным → следующий тик ретраит
        state = json.loads(isolated_state.read_text())
        assert state.get("last_run_date") != "2026-06-25"

    def test_успех_помечает_день_после_прогона(self, isolated_state):
        mock_result = {"ran": True, "winners": [], "scaled": [], "skipped_reason": None}
        with patch("web.app.datetime") as mock_dt, \
             patch("services.budget_scaler.get_scale_config", return_value={"max_scales_per_run": 2}), \
             patch("services.budget_scaler.run_budget_scaling", return_value=mock_result):
            mock_dt.now.return_value = self._make_now(13)
            _cron_budget_scaler()

        state = json.loads(isolated_state.read_text())
        assert state.get("last_run_date") == "2026-06-25"  # помечен ПОСЛЕ успеха
        assert "started_at" not in state                    # in-flight замок снят

    def test_ретрай_возможен_после_просроченного_замка(self, isolated_state):
        """Просроченный in-flight замок (started_at старше таймаута) не блокирует ретрай."""
        from web.app import _should_run_budget_scaler_cron, _save_budget_scaler_cron_state

        # Замок стартовал час назад (> 20 мин) — прогон завис/упал, можно ретраить
        stale = (self._make_now(13) - timedelta(hours=1)).isoformat()
        _save_budget_scaler_cron_state({"started_at": stale})
        assert _should_run_budget_scaler_cron(self._make_now(13)) is True

    def test_свежий_замок_блокирует_параллельный_запуск(self, isolated_state):
        """Свежий in-flight замок (прогон идёт прямо сейчас) блокирует повторный тик."""
        from web.app import _should_run_budget_scaler_cron, _save_budget_scaler_cron_state

        fresh = self._make_now(13).isoformat()
        _save_budget_scaler_cron_state({"started_at": fresh})
        # Тик через 5 минут — прогон ещё идёт (таймаут 20 мин по умолчанию)
        assert _should_run_budget_scaler_cron(self._make_now(13) + timedelta(minutes=5)) is False
