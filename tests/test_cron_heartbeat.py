"""
Тесты для services/cron_heartbeat.py.

Проверяем:
1. Декоратор @heartbeat пишет отметку при успехе / НЕ пишет при исключении (re-raise)
2. functools.wraps сохраняет __name__
3. check_stale: свежий не протух, протухший в списке (с сохранённым critical),
   «нет отметки вообще» — не считается протухшим
4. Конкурентные отметки (несколько потоков) не бьют файл — итоговый JSON валиден,
   все ключи на месте
5. run_cron_watchdog: алерт по протухшему + дедуп 6ч (2-й прогон — skip)

Все моки/патчи — через monkeypatch/patch (контекст-менеджеры), никакого глобального
состояния между тестами. Файлы heartbeat/watchdog-state — на tmp_path.
"""

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_TZ = timezone(timedelta(hours=5))


@pytest.fixture
def isolate_files(tmp_path, monkeypatch):
    """Перенаправляет _HB_FILE и _WATCHDOG_STATE_FILE на tmp_path — изоляция между тестами."""
    import services.cron_heartbeat as hb

    hb_file = tmp_path / "cron_heartbeats.json"
    watchdog_file = tmp_path / "cron_watchdog_state.json"
    monkeypatch.setattr(hb, "_HB_FILE", hb_file)
    monkeypatch.setattr(hb, "_WATCHDOG_STATE_FILE", watchdog_file)
    return hb_file, watchdog_file


# ---------------------------------------------------------------------------
# Декоратор heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeatDecorator:
    """Декоратор @heartbeat пишет отметку только при успешном прогоне."""

    def test_writes_mark_on_success(self, isolate_files):
        """При успешном вызове обёрнутой функции — отметка появляется в файле."""
        import services.cron_heartbeat as hb

        @hb.heartbeat("test_cron_ok", 15, critical=False)
        def my_cron():
            return "done"

        result = my_cron()

        assert result == "done"
        data = hb.load_heartbeats()
        assert "test_cron_ok" in data["heartbeats"]
        entry = data["heartbeats"]["test_cron_ok"]
        assert entry["expected_minutes"] == 15
        assert entry["critical"] is False

        at = datetime.fromisoformat(entry["at"])
        if at.tzinfo is None:
            at = at.replace(tzinfo=_TZ)
        age_seconds = (datetime.now(_TZ) - at).total_seconds()
        assert age_seconds < 5, "Отметка должна быть примерно 'сейчас'"

    def test_does_not_write_mark_on_exception_and_reraises(self, isolate_files):
        """При исключении функции — отметка НЕ пишется, исключение пробрасывается."""
        import services.cron_heartbeat as hb

        @hb.heartbeat("test_cron_fail", 15, critical=True)
        def failing_cron():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            failing_cron()

        data = hb.load_heartbeats()
        assert "test_cron_fail" not in data["heartbeats"]

    def test_functools_wraps_preserves_name(self, isolate_files):
        """functools.wraps сохраняет __name__ обёрнутой функции (важно для логов APScheduler)."""
        import services.cron_heartbeat as hb

        @hb.heartbeat("test_cron_named", 10)
        def _cron_some_named_job():
            return None

        assert _cron_some_named_job.__name__ == "_cron_some_named_job"

    def test_critical_flag_persisted(self, isolate_files):
        """critical=True корректно сохраняется в отметке."""
        import services.cron_heartbeat as hb

        @hb.heartbeat("test_cron_critical", 30, critical=True)
        def cron_critical():
            return None

        cron_critical()
        data = hb.load_heartbeats()
        assert data["heartbeats"]["test_cron_critical"]["critical"] is True


# ---------------------------------------------------------------------------
# check_stale
# ---------------------------------------------------------------------------

class TestCheckStale:
    """check_stale: свежий не протух, протухший в списке, нет отметки — не протух."""

    def test_fresh_mark_not_stale(self, isolate_files):
        """Отметка 5 минут назад при expected=15 (порог 45мин) — не протухла."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        hb.mark_ok("fresh_cron", 15, False, now=now - timedelta(minutes=5))

        stale = hb.check_stale(now)
        names = [s["name"] for s in stale]
        assert "fresh_cron" not in names

    def test_stale_mark_appears_with_critical_preserved(self, isolate_files):
        """Отметка 60 минут назад при expected=15 (порог 45мин) — протухла, critical сохранён."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        hb.mark_ok("stale_cron", 15, True, now=now - timedelta(minutes=60))

        stale = hb.check_stale(now)
        matched = [s for s in stale if s["name"] == "stale_cron"]
        assert len(matched) == 1
        entry = matched[0]
        assert entry["critical"] is True
        assert entry["expected_minutes"] == 15
        assert entry["age_minutes"] >= 59

    def test_no_mark_at_all_is_not_stale(self, isolate_files):
        """Пустой файл (крон ни разу не отметился) -> [] (не алертим сразу после деплоя)."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        stale = hb.check_stale(now)
        assert stale == []

    def test_boundary_exactly_at_threshold_not_stale(self, isolate_files):
        """Возраст ровно на пороге (age == expected*K) НЕ считается протухшим (строгое >)."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        # expected=10, K=3 -> порог 30 минут. Ставим отметку ровно 30 минут назад.
        hb.mark_ok("boundary_cron", 10, False, now=now - timedelta(minutes=30))

        stale = hb.check_stale(now)
        names = [s["name"] for s in stale]
        assert "boundary_cron" not in names


# ---------------------------------------------------------------------------
# Конкурентные отметки не бьют файл
# ---------------------------------------------------------------------------

class TestConcurrentMarks:
    """Несколько потоков пишут отметки параллельно — итоговый файл валиден, ключи на месте."""

    def test_concurrent_mark_ok_calls_produce_valid_file(self, isolate_files):
        """20 потоков пишут 20 разных кронов параллельно -> все 20 ключей есть, JSON не битый."""
        import services.cron_heartbeat as hb

        names = [f"concurrent_cron_{i}" for i in range(20)]
        threads = []

        def _write(name):
            hb.mark_ok(name, 15, False)

        for name in names:
            t = threading.Thread(target=_write, args=(name,))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Файл должен быть валидным JSON и содержать все 20 ключей — лок предотвращает
        # потерю записей при read-modify-write.
        data = hb.load_heartbeats()
        for name in names:
            assert name in data["heartbeats"], f"Отметка {name} потеряна при конкурентной записи"


# ---------------------------------------------------------------------------
# run_cron_watchdog: алерт + дедуп 6ч
# ---------------------------------------------------------------------------

class TestRunCronWatchdog:
    """run_cron_watchdog шлёт алерт по протухшим и дедупит повтор в течение 6ч."""

    def test_sends_alert_for_stale_cron(self, isolate_files):
        """Протухший крон -> алерт отправлен через send_telegram(channel='health')."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        hb.mark_ok("dead_cron", 15, True, now=now - timedelta(hours=2))

        with patch("services.notifications.send_telegram", return_value=True) as mock_tg:
            result = hb.run_cron_watchdog(now)

        assert result["stale"] == 1
        assert result["alerts_sent"] == 1
        assert result["alerts_skipped"] == 0
        assert mock_tg.called
        call_kwargs = mock_tg.call_args
        assert call_kwargs.kwargs.get("channel") == "health"

    def test_dedup_second_run_within_6h_is_skipped(self, isolate_files):
        """Два прогона подряд по одному и тому же протухшему крону: 1-й sent, 2-й skipped."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        hb.mark_ok("dead_cron_2", 15, False, now=now - timedelta(hours=2))

        with patch("services.notifications.send_telegram", return_value=True) as mock_tg:
            first = hb.run_cron_watchdog(now)
            second = hb.run_cron_watchdog(now + timedelta(minutes=10))

        assert first["alerts_sent"] == 1
        assert first["alerts_skipped"] == 0

        assert second["alerts_sent"] == 0
        assert second["alerts_skipped"] == 1
        # send_telegram вызывался только 1 раз (второй прогон дедуплицирован до отправки)
        assert mock_tg.call_count == 1

    def test_no_stale_no_alerts(self, isolate_files):
        """Нет протухших кронов -> ничего не отправляется."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        hb.mark_ok("healthy_cron", 15, False, now=now - timedelta(minutes=2))

        with patch("services.notifications.send_telegram") as mock_tg:
            result = hb.run_cron_watchdog(now)

        assert result["stale"] == 0
        assert result["alerts_sent"] == 0
        mock_tg.assert_not_called()
