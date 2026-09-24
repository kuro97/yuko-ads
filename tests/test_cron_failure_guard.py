"""
Тесты стража повторных провалов кронов (services/cron_heartbeat.py):
report_cron_failure / report_cron_success.

Проверяем:
1. 3 провала ПОДРЯД -> ровно один критический алерт (на 3-м).
2. 2 провала + успех -> алерта нет, счётчик сброшен.
3. Антиспам: после алерта повторные провалы в течение 6ч не алертят, после 6ч — алертят.
4. «Крон ожил» шлётся однократно после серии провалов (только если ДО этого алертили).
5. Битый state-файл НЕ роняет крон и НЕ шлёт ложный алерт (fail-safe, self-heal).
6. Интеграционно: сломанный внутренний вызов внутри _cron_sync_mql (web/app.py) 3× подряд
   -> один алерт через report_cron_failure (существующий except сохранён, крон не падает).

Изоляция: _FAILURE_STATE_FILE и _HB_FILE — на tmp_path (monkeypatch), send_critical_alert —
мок (patch). Никакого глобального состояния между тестами.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_TZ = timezone(timedelta(hours=5))


@pytest.fixture
def isolate_failure_state(tmp_path, monkeypatch):
    """Перенаправляет _FAILURE_STATE_FILE (и _HB_FILE) на tmp_path — изоляция между тестами."""
    import services.cron_heartbeat as hb

    fail_file = tmp_path / "cron_failure_state.json"
    monkeypatch.setattr(hb, "_FAILURE_STATE_FILE", fail_file)
    # heartbeat-декоратор пишет отметку в _HB_FILE — тоже уводим в tmp, чтобы не трогать data/
    monkeypatch.setattr(hb, "_HB_FILE", tmp_path / "cron_heartbeats.json")
    return fail_file


def _read_entry(fail_file: Path, cron_name: str) -> dict:
    """Читает запись крона из state-файла (или {} если файла/записи нет)."""
    if not fail_file.exists():
        return {}
    data = json.loads(fail_file.read_text(encoding="utf-8"))
    return data.get("crons", {}).get(cron_name, {})


# ---------------------------------------------------------------------------
# 1. Порог 3 подряд -> один алерт
# ---------------------------------------------------------------------------

class TestThreshold:
    def test_three_consecutive_failures_trigger_single_alert(self, isolate_failure_state):
        """3 провала подряд -> send_critical_alert ровно 1 раз (на 3-м провале)."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        with patch("services.notifications.send_critical_alert") as mock_alert:
            hb.report_cron_failure("_cron_x", "err1", now=now)
            assert not mock_alert.called  # 1-й провал — молчим
            hb.report_cron_failure("_cron_x", "err2", now=now + timedelta(minutes=30))
            assert not mock_alert.called  # 2-й — тоже
            hb.report_cron_failure("_cron_x", "boom-token", now=now + timedelta(minutes=60))
            assert mock_alert.call_count == 1  # 3-й — алерт

        entry = _read_entry(isolate_failure_state, "_cron_x")
        assert entry["consecutive_failures"] == 3
        assert entry["alerted"] is True

    def test_alert_message_contains_cron_name_and_error(self, isolate_failure_state):
        """Текст алерта содержит имя крона и текст ошибки (для диагностики)."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        with patch("services.notifications.send_critical_alert") as mock_alert:
            for i in range(3):
                hb.report_cron_failure("_cron_sync_mql", "FB CAPI token expired", now=now + timedelta(minutes=30 * i))

        args, kwargs = mock_alert.call_args
        title = args[0]
        detail = args[1]
        assert "_cron_sync_mql" in title
        assert "3" in title  # 3-й провал подряд
        assert "FB CAPI token expired" in detail
        assert kwargs.get("channel") == "health"

    def test_two_failures_no_alert(self, isolate_failure_state):
        """Только 2 провала подряд -> алерта нет (порог 3)."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        with patch("services.notifications.send_critical_alert") as mock_alert:
            hb.report_cron_failure("_cron_y", "e1", now=now)
            hb.report_cron_failure("_cron_y", "e2", now=now + timedelta(minutes=30))

        assert not mock_alert.called
        assert _read_entry(isolate_failure_state, "_cron_y")["consecutive_failures"] == 2


# ---------------------------------------------------------------------------
# 2. Успех сбрасывает счётчик
# ---------------------------------------------------------------------------

class TestSuccessResets:
    def test_two_failures_then_success_resets_no_alert(self, isolate_failure_state):
        """2 провала + успех -> счётчик 0, алерта нет, «ожил» нет (не алертили)."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        with patch("services.notifications.send_critical_alert") as mock_alert:
            hb.report_cron_failure("_cron_z", "e1", now=now)
            hb.report_cron_failure("_cron_z", "e2", now=now + timedelta(minutes=30))
            hb.report_cron_success("_cron_z", now=now + timedelta(minutes=60))

        assert not mock_alert.called
        entry = _read_entry(isolate_failure_state, "_cron_z")
        assert entry["consecutive_failures"] == 0
        assert entry["alerted"] is False

    def test_success_on_clean_cron_does_not_create_file(self, isolate_failure_state):
        """Успех крона, который ни разу не падал -> файл не создаётся (только чтение)."""
        import services.cron_heartbeat as hb

        with patch("services.notifications.send_critical_alert") as mock_alert:
            hb.report_cron_success("_cron_never_failed")

        assert not mock_alert.called
        assert not isolate_failure_state.exists()

    def test_failure_counter_restarts_after_reset(self, isolate_failure_state):
        """После успеха счётчик обнуляется: следующая серия снова копится с нуля."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        with patch("services.notifications.send_critical_alert") as mock_alert:
            hb.report_cron_failure("_cron_r", "e1", now=now)
            hb.report_cron_failure("_cron_r", "e2", now=now + timedelta(minutes=30))
            hb.report_cron_success("_cron_r", now=now + timedelta(minutes=60))
            # снова 2 провала — по-прежнему < 3, алерта быть не должно
            hb.report_cron_failure("_cron_r", "e3", now=now + timedelta(minutes=90))
            hb.report_cron_failure("_cron_r", "e4", now=now + timedelta(minutes=120))

        assert not mock_alert.called
        assert _read_entry(isolate_failure_state, "_cron_r")["consecutive_failures"] == 2


# ---------------------------------------------------------------------------
# 3. Антиспам 6ч
# ---------------------------------------------------------------------------

class TestAntiSpam:
    def test_repeat_failures_within_6h_do_not_realert(self, isolate_failure_state):
        """После алерта на 3-м провале — провалы в течение 6ч не шлют новый алерт."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        with patch("services.notifications.send_critical_alert") as mock_alert:
            for i in range(3):  # 3 провала -> 1 алерт
                hb.report_cron_failure("_cron_spam", "e", now=now + timedelta(minutes=30 * i))
            assert mock_alert.call_count == 1
            # ещё провалы в пределах 6ч от алерта (алерт был на 3-м, т.е. +60 мин)
            hb.report_cron_failure("_cron_spam", "e", now=now + timedelta(hours=2))
            hb.report_cron_failure("_cron_spam", "e", now=now + timedelta(hours=5))
            assert mock_alert.call_count == 1  # без нового алерта

    def test_realert_after_6h_of_continued_failures(self, isolate_failure_state):
        """Если провалы длятся дольше 6ч — шлётся повторный алерт (не чаще раза в 6ч)."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        with patch("services.notifications.send_critical_alert") as mock_alert:
            for i in range(3):
                hb.report_cron_failure("_cron_long", "e", now=now + timedelta(minutes=30 * i))
            assert mock_alert.call_count == 1
            last_alert = now + timedelta(minutes=60)  # алерт был на 3-м провале
            # ровно на границе 6ч — ещё дедуп (строгое <), а через 6ч+1мин — новый алерт
            hb.report_cron_failure("_cron_long", "e", now=last_alert + timedelta(hours=6, minutes=1))
            assert mock_alert.call_count == 2


# ---------------------------------------------------------------------------
# 4. «Крон ожил»
# ---------------------------------------------------------------------------

class TestRecovery:
    def test_recovery_message_once_after_alert(self, isolate_failure_state):
        """3 провала (алерт) + успех -> одно «ожил»; повторный успех молчит."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        with patch("services.notifications.send_critical_alert") as mock_alert:
            for i in range(3):
                hb.report_cron_failure("_cron_rev", "e", now=now + timedelta(minutes=30 * i))
            assert mock_alert.call_count == 1  # алерт о провалах
            hb.report_cron_success("_cron_rev", now=now + timedelta(hours=2))
            assert mock_alert.call_count == 2  # «ожил»
            revive_args = mock_alert.call_args[0]
            assert "ожил" in revive_args[0]
            # повторный успех — уже чисто, молчим
            hb.report_cron_success("_cron_rev", now=now + timedelta(hours=3))
            assert mock_alert.call_count == 2

        entry = _read_entry(isolate_failure_state, "_cron_rev")
        assert entry["consecutive_failures"] == 0
        assert entry["alerted"] is False


# ---------------------------------------------------------------------------
# 5. Битый state — fail-safe
# ---------------------------------------------------------------------------

class TestBrokenStateFailSafe:
    def test_corrupt_state_does_not_crash_and_does_not_alert(self, isolate_failure_state):
        """Битый JSON в state -> report_cron_failure не бросает и не алертит (self-heal)."""
        import services.cron_heartbeat as hb

        isolate_failure_state.write_text("{ это не json ", encoding="utf-8")

        with patch("services.notifications.send_critical_alert") as mock_alert:
            # не должно бросить
            hb.report_cron_failure("_cron_corrupt", "e", now=datetime.now(_TZ))

        assert not mock_alert.called
        # self-heal: файл снова валидный JSON
        healed = json.loads(isolate_failure_state.read_text(encoding="utf-8"))
        assert healed == {"crons": {}}

    def test_corrupt_state_success_path_fail_safe(self, isolate_failure_state):
        """Битый state в report_cron_success -> не бросает, не шлёт «ожил», чинит файл."""
        import services.cron_heartbeat as hb

        isolate_failure_state.write_text("broken", encoding="utf-8")

        with patch("services.notifications.send_critical_alert") as mock_alert:
            hb.report_cron_success("_cron_corrupt2", now=datetime.now(_TZ))

        assert not mock_alert.called
        healed = json.loads(isolate_failure_state.read_text(encoding="utf-8"))
        assert healed == {"crons": {}}

    def test_alert_send_exception_does_not_crash(self, isolate_failure_state):
        """Если сам send_critical_alert бросит — report_cron_failure это гасит (не роняет крон)."""
        import services.cron_heartbeat as hb

        now = datetime.now(_TZ)
        with patch("services.notifications.send_critical_alert", side_effect=RuntimeError("tg down")):
            # 3 провала: на 3-м попытка алерта бросает — не должно всплыть наружу
            for i in range(3):
                hb.report_cron_failure("_cron_tgdown", "e", now=now + timedelta(minutes=30 * i))

        # счётчик всё равно учтён
        assert _read_entry(isolate_failure_state, "_cron_tgdown")["consecutive_failures"] == 3


# ---------------------------------------------------------------------------
# 6. Интеграция: сломанный внутренний вызов внутри _cron_sync_mql
# ---------------------------------------------------------------------------

# Мокаем тяжёлые опциональные зависимости ДО импорта web.app (паттерн из
# test_cron_budget_scaler.py) — иначе импорт может тянуть google.genai.
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()


class TestIntegrationSyncMql:
    def test_broken_internal_call_three_times_one_alert(self, tmp_path, monkeypatch):
        """_cron_sync_mql со сломанным get_qualified_leads_with_fb_id 3× подряд ->
        один алерт, крон не падает (существующий except сохранён)."""
        import services.cron_heartbeat as hb
        from web import app as webapp

        # Изоляция state-файлов
        monkeypatch.setattr(hb, "_FAILURE_STATE_FILE", tmp_path / "cron_failure_state.json")
        monkeypatch.setattr(hb, "_HB_FILE", tmp_path / "cron_heartbeats.json")

        # Внутренний вызов крона бросает — имитируем протухший AMO-доступ
        def _boom(*a, **k):
            raise RuntimeError("AMO 401 unauthorized")

        with patch("integrations.amo.get_qualified_leads_with_fb_id", side_effect=_boom), \
             patch("services.notifications.send_critical_alert") as mock_alert:
            # крон НЕ должен бросать наружу ни разу (except внутри сохранён)
            webapp._cron_sync_mql()
            webapp._cron_sync_mql()
            assert not mock_alert.called
            webapp._cron_sync_mql()
            assert mock_alert.call_count == 1

        # Проверяем что алерт про нужный крон и содержит текст ошибки
        args, kwargs = mock_alert.call_args
        assert "_cron_sync_mql" in args[0]
        assert "AMO 401" in args[1]

    def test_recovery_after_broken_then_fixed(self, tmp_path, monkeypatch):
        """3 провала (алерт) -> починка -> успешный прогон шлёт «ожил»."""
        import services.cron_heartbeat as hb
        from web import app as webapp

        monkeypatch.setattr(hb, "_FAILURE_STATE_FILE", tmp_path / "cron_failure_state.json")
        monkeypatch.setattr(hb, "_HB_FILE", tmp_path / "cron_heartbeats.json")

        def _boom(*a, **k):
            raise RuntimeError("boom")

        with patch("services.notifications.send_critical_alert") as mock_alert:
            with patch("integrations.amo.get_qualified_leads_with_fb_id", side_effect=_boom):
                for _ in range(3):
                    webapp._cron_sync_mql()
            assert mock_alert.call_count == 1  # алерт о провалах

            # Теперь всё чинится: leads пусто, батчи ничего не шлют
            with patch("integrations.amo.get_qualified_leads_with_fb_id", return_value=[]), \
                 patch("integrations.fb_capi.send_mql_batch", return_value={"sent": 0, "skipped": 0, "errors": 0}), \
                 patch("integrations.ga_mp.send_mql_batch_to_ga4", return_value={"sent": 0, "skipped": 0, "errors": 0}):
                webapp._cron_sync_mql()

            assert mock_alert.call_count == 2  # «ожил»
            assert "ожил" in mock_alert.call_args[0][0]
