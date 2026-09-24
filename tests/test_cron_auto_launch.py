"""
Тесты крон-функции _cron_auto_launch (web/app.py).

Проверяем:
- гейт: крон запускается только в час==10 (CityA)
- дедупликация: раз в день через state-файл
- при launch_enabled=false → run_auto_launch вызывается с mode="dry_run"
- при launch_enabled=true + enabled=true + kill_switch=false → mode="active"
- при kill_switch=true → mode="dry_run" (не active)
- при enabled=false → mode="dry_run"
- исключение внутри не рушит lifespan (try/except)
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

# Мокаем тяжёлые зависимости ДО импорта web.app
sys.path.insert(0, str(Path(__file__).parent.parent))

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

# Импортируем нужные функции напрямую из web.app
from web.app import (
    _should_run_auto_launch_cron,
    _load_auto_launch_cron_state,
    _save_auto_launch_cron_state,
    _cron_auto_launch,
    _AUTO_LAUNCH_CRON_STATE,
    _TZ_LOCAL,
)


# ---------------------------------------------------------------------------
# Фикстура: изолированный state-файл
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Подменяет путь к state-файлу на временный каталог."""
    state_file = tmp_path / "auto_launch_cron_state.json"
    monkeypatch.setattr("web.app._AUTO_LAUNCH_CRON_STATE", state_file)
    return state_file


# ---------------------------------------------------------------------------
# Тесты _should_run_auto_launch_cron
# ---------------------------------------------------------------------------

class TestShouldRunAutoLaunchCron:
    def _now(self, hour: int, date_str: str = "2026-06-25") -> datetime:
        """Вспомогательный метод: datetime в TZ CityA с заданным часом."""
        d = datetime.fromisoformat(f"{date_str}T{hour:02d}:07:00").replace(
            tzinfo=_TZ_LOCAL
        )
        return d

    def test_гейт_час_10_проходит(self, isolated_state):
        """Час 10 CityA → _should_run_auto_launch_cron возвращает True (если не запускался)."""
        now = self._now(10)
        assert _should_run_auto_launch_cron(now) is True

    def test_гейт_другой_час_не_проходит(self, isolated_state):
        """Любой час кроме 10 → False."""
        for hour in (0, 9, 11, 12, 20, 23):
            now = self._now(hour)
            assert _should_run_auto_launch_cron(now) is False, f"час {hour} должен быть заблокирован"

    def test_раз_в_день_дедупликация(self, isolated_state):
        """Если уже запускался сегодня (last_run_date == today) → False."""
        today_str = "2026-06-25"
        _save_auto_launch_cron_state({"last_run_date": today_str})
        now = self._now(10, date_str=today_str)
        assert _should_run_auto_launch_cron(now) is False

    def test_другой_день_в_state_проходит(self, isolated_state):
        """Если last_run_date = вчера → сегодня True."""
        _save_auto_launch_cron_state({"last_run_date": "2026-06-24"})
        now = self._now(10, date_str="2026-06-25")
        assert _should_run_auto_launch_cron(now) is True

    def test_пустой_state_проходит(self, isolated_state):
        """Нет state-файла → тоже True (первый запуск)."""
        now = self._now(10)
        assert _should_run_auto_launch_cron(now) is True


# ---------------------------------------------------------------------------
# Тесты _cron_auto_launch: выбор режима
# ---------------------------------------------------------------------------

class TestCronAutoLaunchMode:
    """Проверяем что крон вызывает run_auto_launch с правильным mode."""

    def _make_now(self, hour: int = 10) -> datetime:
        return datetime(2026, 6, 25, hour, 5, 0, tzinfo=_TZ_LOCAL)

    def _run_cron(self, cfg: dict, isolated_state, hour: int = 10):
        """Запускает _cron_auto_launch с подменёнными зависимостями."""
        mock_result = {
            "mode": cfg.get("__expected_mode", "dry_run"),
            "launched": [],
            "recommendations": [],
            "skipped_reason": None,
        }
        with patch("web.app.datetime") as mock_dt, \
             patch("web.app._load_auto_launch_cron_state", return_value={}), \
             patch("web.app._save_auto_launch_cron_state") as mock_save, \
             patch("services.auto_launch._get_autopilot_config", return_value=cfg), \
             patch("services.auto_launch.run_auto_launch", return_value=mock_result) as mock_run:

            mock_dt.now.return_value = self._make_now(hour)

            # Также мокаем web.app._should_run_auto_launch_cron чтобы обойти проверку
            # внутри крона (используем реальный now через monkeypatch datetime)
            _cron_auto_launch()

        return mock_run, mock_save

    def test_launch_enabled_false_вызывает_dry_run(self, isolated_state):
        """launch_enabled=False → крон вызывает run_auto_launch с mode='dry_run'."""
        cfg = {
            "enabled": True,
            "launch_enabled": False,  # выключено
            "kill_switch": False,
            "max_launches_per_day": 1,
        }
        mock_run, _ = self._run_cron(cfg, isolated_state)
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs.get("mode") == "dry_run" or mock_run.call_args[0][0] == "dry_run"

    def test_launch_enabled_true_вызывает_active(self, isolated_state):
        """launch_enabled=True + enabled=True + kill_switch=False → mode='active'."""
        cfg = {
            "enabled": True,
            "launch_enabled": True,
            "kill_switch": False,
            "max_launches_per_day": 2,
        }
        mock_run, _ = self._run_cron(cfg, isolated_state)
        mock_run.assert_called_once()
        # Проверяем что mode="active"
        args, kwargs = mock_run.call_args
        called_mode = args[0] if args else kwargs.get("mode")
        assert called_mode == "active"

    def test_kill_switch_true_вызывает_dry_run(self, isolated_state):
        """kill_switch=True → mode='dry_run' (не active, несмотря на enabled=True)."""
        cfg = {
            "enabled": True,
            "launch_enabled": True,
            "kill_switch": True,  # аварийная остановка
            "max_launches_per_day": 1,
        }
        mock_run, _ = self._run_cron(cfg, isolated_state)
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        called_mode = args[0] if args else kwargs.get("mode")
        assert called_mode == "dry_run"

    def test_enabled_false_вызывает_dry_run(self, isolated_state):
        """enabled=False → mode='dry_run'."""
        cfg = {
            "enabled": False,  # автопилот выключен
            "launch_enabled": True,
            "kill_switch": False,
            "max_launches_per_day": 1,
        }
        mock_run, _ = self._run_cron(cfg, isolated_state)
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        called_mode = args[0] if args else kwargs.get("mode")
        assert called_mode == "dry_run"

    def test_max_launches_берётся_из_конфига(self, isolated_state):
        """max_launches в вызове run_auto_launch == max_launches_per_day из конфига."""
        cfg = {
            "enabled": True,
            "launch_enabled": True,
            "kill_switch": False,
            "max_launches_per_day": 3,  # нестандартный лимит
        }
        mock_run, _ = self._run_cron(cfg, isolated_state)
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        called_max = kwargs.get("max_launches") if "max_launches" in kwargs else (args[1] if len(args) > 1 else None)
        assert called_max == 3


# ---------------------------------------------------------------------------
# Тест: гейт — крон не вызывает run_auto_launch если час != 10
# ---------------------------------------------------------------------------

class TestCronGate:
    def test_час_не_10_не_вызывает_run_auto_launch(self, isolated_state):
        """В час != 10 крон ничего не делает (run_auto_launch не вызывается)."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.auto_launch.run_auto_launch") as mock_run:
            # Час 9 — монитор покрытия
            mock_dt.now.return_value = datetime(2026, 6, 25, 9, 30, 0, tzinfo=_TZ_LOCAL)
            _cron_auto_launch()

        mock_run.assert_not_called()

    def test_уже_запускался_сегодня_не_вызывает_run_auto_launch(self, isolated_state):
        """Если state содержит сегодняшнюю дату — крон не вызывает run_auto_launch."""
        _save_auto_launch_cron_state({"last_run_date": "2026-06-25"})

        with patch("web.app.datetime") as mock_dt, \
             patch("services.auto_launch.run_auto_launch") as mock_run:
            mock_dt.now.return_value = datetime(2026, 6, 25, 10, 7, 0, tzinfo=_TZ_LOCAL)
            _cron_auto_launch()

        mock_run.assert_not_called()

    def test_исключение_внутри_не_рушит_крон(self, isolated_state):
        """Исключение в run_auto_launch не должно проваливаться наружу (try/except)."""
        with patch("web.app.datetime") as mock_dt, \
             patch("web.app._load_auto_launch_cron_state", return_value={}), \
             patch("web.app._save_auto_launch_cron_state"), \
             patch("services.auto_launch._get_autopilot_config", side_effect=RuntimeError("test error")):
            mock_dt.now.return_value = datetime(2026, 6, 25, 10, 7, 0, tzinfo=_TZ_LOCAL)
            # Не должно бросить исключение
            _cron_auto_launch()
