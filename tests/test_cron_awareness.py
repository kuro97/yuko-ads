"""
Интеграционные тесты кронов Фазы 5 (осведомлённость) в web/app.py.

Покрываем:
1. _cron_morning_digest — гейт enabled=false (send не зовётся), гейт по часу/
   уже-слали (state.last_morning_digest_date), успешная отправка пишет state.
2. _cron_cron_watchdog — гейт enabled=false (no-op), зовёт run_cron_watchdog;
   у него НЕТ heartbeat-обёртки (проверяем отсутствие отметки в data heartbeats
   после прогона — по спеке §7.2 сторож сторожа не заводим).
3. Интеграция run_anomaly_alerts в _cron_guardian_sweep — вызывается ПОСЛЕ sweep
   в слоте; вне слота (гейт _should_run_guardian_sweep=False) — не вызывается.
4. heartbeat-обёртки живы: успешный прогон обёрнутого крона пишет отметку в
   cron_heartbeats.json (2 примера: _cron_db_backup и _cron_morning_digest,
   при этом не ломает существующее поведение самих кронов).

Паттерны моков времени/state — как в tests/test_cron_guardian.py и
tests/test_cron_adset_cleaner.py. Только фикстуры/context-managers, никакого
глобального состояния между тестами.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем тяжёлые зависимости ДО импорта web.app (как в других тестах кронов)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from web.app import (
    _cron_morning_digest,
    _cron_cron_watchdog,
    _cron_guardian_sweep,
    _cron_db_backup,
    _save_guardian_sweep_state,
    _TZ_LOCAL,
)
import services.cron_heartbeat as hb

_TZ = timezone(timedelta(hours=5))


def _dt(hour: int, minute: int = 3, day: int = 24) -> datetime:
    """Создаёт datetime для 2026-06-{day} {hour}:{minute} CityA."""
    return datetime(2026, 6, day, hour, minute, tzinfo=_TZ)


# ---------------------------------------------------------------------------
# Фикстуры изоляции: heartbeat-файл, autopilot state, guardian sweep state
# ---------------------------------------------------------------------------

@pytest.fixture
def isolate_heartbeats(tmp_path, monkeypatch):
    """Перенаправляет cron_heartbeats.json на временный файл — изоляция между тестами."""
    hb_file = tmp_path / "cron_heartbeats.json"
    monkeypatch.setattr(hb, "_HB_FILE", hb_file)
    return hb_file


@pytest.fixture
def isolated_autopilot_state(tmp_path, monkeypatch):
    """Подменяет STATE_FILE автопилота (last_morning_digest_date) на временный файл."""
    state_file = tmp_path / "autopilot_state.json"
    monkeypatch.setattr("services.autopilot.STATE_FILE", state_file)
    return state_file


@pytest.fixture
def isolated_sweep_state(tmp_path, monkeypatch):
    """Подменяет путь к state-файлу крона guardian_sweep на временный."""
    state_file = tmp_path / "guardian_sweep_cron_state.json"
    monkeypatch.setattr("web.app._GUARDIAN_SWEEP_STATE", state_file)
    return state_file


def _digest_config(enabled: bool = True) -> dict:
    return {"morning_digest": {"enabled": enabled}}


def _watchdog_config(enabled: bool = True) -> dict:
    return {"cron_watchdog": {"enabled": enabled}}


# ---------------------------------------------------------------------------
# _cron_morning_digest
# ---------------------------------------------------------------------------

class TestCronMorningDigest:
    def test_гейт_enabled_false_send_не_зовётся(self, isolate_heartbeats, isolated_autopilot_state):
        """morning_digest.enabled=false → send_morning_digest не вызывается вообще."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.get_autopilot_config", return_value=_digest_config(False)), \
             patch("services.morning_digest.send_morning_digest") as mock_send:
            mock_dt.now.return_value = _dt(8)
            _cron_morning_digest()

        mock_send.assert_not_called()

    def test_8ч_ещё_не_слали_зовёт_send(self, isolate_heartbeats, isolated_autopilot_state):
        """Час=8, last_morning_digest_date отсутствует → send_morning_digest вызывается,
        state обновляется today."""
        now = _dt(8)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.get_autopilot_config", return_value=_digest_config(True)), \
             patch("services.morning_digest.send_morning_digest", return_value=True) as mock_send:
            mock_dt.now.return_value = now
            _cron_morning_digest()

        mock_send.assert_called_once()
        from services.autopilot import _load_state
        state = _load_state()
        assert state["last_morning_digest_date"] == now.date().isoformat()

    def test_уже_слали_сегодня_send_не_зовётся(self, isolate_heartbeats, isolated_autopilot_state):
        """last_morning_digest_date == сегодня → повторный тик в 08ч не шлёт снова."""
        now = _dt(8, minute=25)
        from services.autopilot import _save_state
        _save_state({"last_morning_digest_date": now.date().isoformat()})

        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.get_autopilot_config", return_value=_digest_config(True)), \
             patch("services.morning_digest.send_morning_digest") as mock_send:
            mock_dt.now.return_value = now
            _cron_morning_digest()

        mock_send.assert_not_called()

    def test_не_8_час_send_не_зовётся(self, isolate_heartbeats, isolated_autopilot_state):
        """Час != 8 (напр. 14ч) → should_send_digest=False → send не вызывается."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.get_autopilot_config", return_value=_digest_config(True)), \
             patch("services.morning_digest.send_morning_digest") as mock_send:
            mock_dt.now.return_value = _dt(14)
            _cron_morning_digest()

        mock_send.assert_not_called()

    def test_успешная_отправка_пишет_state(self, isolate_heartbeats, isolated_autopilot_state):
        """send_morning_digest=True → last_morning_digest_date записан в autopilot_state.json."""
        now = _dt(8, day=25)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.get_autopilot_config", return_value=_digest_config(True)), \
             patch("services.morning_digest.send_morning_digest", return_value=True):
            mock_dt.now.return_value = now
            _cron_morning_digest()

        from services.autopilot import _load_state
        state = _load_state()
        assert state.get("last_morning_digest_date") == "2026-06-25"

    def test_send_вернул_false_state_не_пишется(self, isolate_heartbeats, isolated_autopilot_state):
        """send_morning_digest=False (ошибка отправки) → state НЕ обновляется (повторим позже)."""
        now = _dt(8, day=26)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.get_autopilot_config", return_value=_digest_config(True)), \
             patch("services.morning_digest.send_morning_digest", return_value=False):
            mock_dt.now.return_value = now
            _cron_morning_digest()

        from services.autopilot import _load_state
        state = _load_state()
        assert state.get("last_morning_digest_date") is None

    def test_исключение_внутри_не_роняет_крон(self, isolate_heartbeats, isolated_autopilot_state):
        """Исключение из send_morning_digest не должно проваливаться наружу (try/except)."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.get_autopilot_config", return_value=_digest_config(True)), \
             patch("services.morning_digest.send_morning_digest", side_effect=RuntimeError("boom")):
            mock_dt.now.return_value = _dt(8)
            # Не должно бросить исключение
            _cron_morning_digest()

    def test_heartbeat_пишет_отметку_при_успехе(self, isolate_heartbeats, isolated_autopilot_state):
        """_cron_morning_digest обёрнут @heartbeat("_cron_morning_digest", 10) — успешный
        прогон (даже гейт-скип вне часа 8, т.к. исключения не было) пишет отметку."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.get_autopilot_config", return_value=_digest_config(True)), \
             patch("services.morning_digest.send_morning_digest"):
            mock_dt.now.return_value = _dt(14)  # вне часа 8 -> гейт-скип, но БЕЗ исключения
            _cron_morning_digest()

        data = hb.load_heartbeats()
        assert "_cron_morning_digest" in data["heartbeats"]
        assert data["heartbeats"]["_cron_morning_digest"]["expected_minutes"] == 10


# ---------------------------------------------------------------------------
# _cron_cron_watchdog
# ---------------------------------------------------------------------------

class TestCronCronWatchdog:
    def test_гейт_enabled_false_no_op(self, isolate_heartbeats):
        """cron_watchdog.enabled=false → run_cron_watchdog вообще не вызывается."""
        with patch("services.autopilot.get_autopilot_config", return_value=_watchdog_config(False)), \
             patch("services.cron_heartbeat.run_cron_watchdog") as mock_run:
            _cron_cron_watchdog()

        mock_run.assert_not_called()

    def test_enabled_true_зовёт_run_cron_watchdog(self, isolate_heartbeats):
        """cron_watchdog.enabled=true → run_cron_watchdog вызывается ровно один раз."""
        with patch("services.autopilot.get_autopilot_config", return_value=_watchdog_config(True)), \
             patch("services.cron_heartbeat.run_cron_watchdog",
                   return_value={"stale": 0, "alerts_sent": 0, "alerts_skipped": 0}) as mock_run:
            _cron_cron_watchdog()

        mock_run.assert_called_once()

    def test_исключение_внутри_не_роняет_крон(self, isolate_heartbeats):
        """Исключение из run_cron_watchdog не должно проваливаться наружу (try/except)."""
        with patch("services.autopilot.get_autopilot_config", return_value=_watchdog_config(True)), \
             patch("services.cron_heartbeat.run_cron_watchdog", side_effect=RuntimeError("boom")):
            # Не должно бросить исключение
            _cron_cron_watchdog()

    def test_без_heartbeat_обёртки_отметка_не_пишется(self, isolate_heartbeats):
        """_cron_cron_watchdog НЕ обёрнут @heartbeat (по спеке §7.2 — не сторожим
        сторожа). После успешного прогона в cron_heartbeats.json НЕТ ключа
        '_cron_cron_watchdog' (в отличие от других кронов)."""
        with patch("services.autopilot.get_autopilot_config", return_value=_watchdog_config(True)), \
             patch("services.cron_heartbeat.run_cron_watchdog",
                   return_value={"stale": 0, "alerts_sent": 0, "alerts_skipped": 0}):
            _cron_cron_watchdog()

        data = hb.load_heartbeats()
        assert "_cron_cron_watchdog" not in data.get("heartbeats", {})


# ---------------------------------------------------------------------------
# Интеграция run_anomaly_alerts в _cron_guardian_sweep
# ---------------------------------------------------------------------------

class TestGuardianSweepAnomalyIntegration:
    _SWEEP_RESULT = {
        "ran": True, "skipped": None, "analyzed": 3,
        "early_candidates": ["ad1"], "wnc_candidates": [], "paused": [],
        "dry_run_only": ["ad1"], "errors": [],
    }

    def test_run_anomaly_alerts_вызван_после_sweep_в_слоте(
        self, isolate_heartbeats, isolated_sweep_state,
    ):
        """Слот свободен (час в _AUTOPILOT_LIVE_HOURS, ещё не бежал) → run_guardian_sweep
        И run_anomaly_alerts вызываются в этом порядке (anomaly ПОСЛЕ sweep)."""
        call_order = []

        def fake_sweep(trigger="cron"):
            call_order.append("sweep")
            return self._SWEEP_RESULT

        def fake_anomaly(now=None):
            call_order.append("anomaly")
            return {"alerts_sent": 0, "alerts_skipped": 0}

        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.run_guardian_sweep", side_effect=fake_sweep), \
             patch("services.anomaly_alerts.run_anomaly_alerts", side_effect=fake_anomaly) as mock_anomaly:
            mock_dt.now.return_value = _dt(8)
            _cron_guardian_sweep()

        mock_anomaly.assert_called_once()
        assert call_order == ["sweep", "anomaly"]
        # anomaly вызван с текущим now (передан по имени)
        _, kwargs = mock_anomaly.call_args
        assert kwargs.get("now") == _dt(8)

    def test_вне_слота_run_anomaly_alerts_не_вызывается(
        self, isolate_heartbeats, isolated_sweep_state,
    ):
        """Час вне _AUTOPILOT_LIVE_HOURS → гейт _should_run_guardian_sweep=False →
        ни sweep, ни anomaly_alerts не вызываются."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.run_guardian_sweep") as mock_sweep, \
             patch("services.anomaly_alerts.run_anomaly_alerts") as mock_anomaly:
            mock_dt.now.return_value = _dt(9)  # 9 не входит в _AUTOPILOT_LIVE_HOURS
            _cron_guardian_sweep()

        mock_sweep.assert_not_called()
        mock_anomaly.assert_not_called()

    def test_повторный_тик_в_слоте_anomaly_alerts_не_вызывается_повторно(
        self, isolate_heartbeats, isolated_sweep_state,
    ):
        """Слот уже отработал (помечен в state) → повторный тик не вызывает ни sweep,
        ни anomaly_alerts."""
        now = _dt(12, day=24)
        slot_key = f"{now.date().isoformat()}-{now.hour}"
        _save_guardian_sweep_state({"slots": {slot_key: "running"}})

        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.run_guardian_sweep") as mock_sweep, \
             patch("services.anomaly_alerts.run_anomaly_alerts") as mock_anomaly:
            mock_dt.now.return_value = now
            _cron_guardian_sweep()

        mock_sweep.assert_not_called()
        mock_anomaly.assert_not_called()

    def test_исключение_в_anomaly_alerts_не_роняет_крон(
        self, isolate_heartbeats, isolated_sweep_state,
    ):
        """run_anomaly_alerts бросил исключение (по спеке never-throw, но крон и так
        обёрнут в try/except) → _cron_guardian_sweep не падает наружу."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.guardian.run_guardian_sweep", return_value=self._SWEEP_RESULT), \
             patch("services.anomaly_alerts.run_anomaly_alerts", side_effect=RuntimeError("boom")):
            mock_dt.now.return_value = _dt(16)
            # Не должно бросить исключение
            _cron_guardian_sweep()


# ---------------------------------------------------------------------------
# heartbeat-обёртки живы (существующие кроны не сломаны декоратором)
# ---------------------------------------------------------------------------

class TestHeartbeatWrappersAlive:
    def test_cron_db_backup_пишет_отметку_при_успехе(
        self, isolate_heartbeats, isolated_autopilot_state,
    ):
        """_cron_db_backup обёрнут @heartbeat("_cron_db_backup", 30, critical=True).
        Гейт-скип (час != _DB_BACKUP_HOUR, внутренности бэкапа не трогаем) — тоже
        считается успехом (исключения нет) и пишет отметку (§7.2 спеки: heartbeat
        протухает только при реальной смерти крона, а не при гейт-скипе)."""
        with patch("web.app.datetime") as mock_dt:
            mock_dt.now.return_value = _dt(9)  # не _DB_BACKUP_HOUR(=4) -> гейт-скип без исключения
            _cron_db_backup()

        data = hb.load_heartbeats()
        assert "_cron_db_backup" in data["heartbeats"]
        entry = data["heartbeats"]["_cron_db_backup"]
        assert entry["expected_minutes"] == 30
        assert entry["critical"] is True

    def test_cron_morning_digest_отметка_независима_от_отправки_дайджеста(
        self, isolate_heartbeats, isolated_autopilot_state,
    ):
        """Даже когда дайджест реально не шлётся (гейт по часу), heartbeat всё равно
        пишет отметку — декоратор не завязан на бизнес-исход тела функции, только
        на отсутствие исключения (§8.1 спеки)."""
        with patch("web.app.datetime") as mock_dt, \
             patch("services.autopilot.get_autopilot_config", return_value=_digest_config(True)), \
             patch("services.morning_digest.send_morning_digest") as mock_send:
            mock_dt.now.return_value = _dt(11)  # не 8ч -> send не вызывается
            _cron_morning_digest()

        mock_send.assert_not_called()
        data = hb.load_heartbeats()
        assert "_cron_morning_digest" in data["heartbeats"]
