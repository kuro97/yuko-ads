"""
Тесты авто-сторожа FB-лидов:
  - get_latest_fb_lead_ts (интеграция с AMO)
  - send_telegram (Telegram-отправка)
  - _cron_fb_lead_watchdog (логика тишины + recovery)
"""

import sys
import time
from datetime import datetime, timedelta, timezone
from types import ModuleType
from unittest.mock import patch, MagicMock

import pytest

from services.notifications import clear_events


# ---------------------------------------------------------------------------
# Патч google.genai на уровне модуля — чтобы импорт web.app не падал.
# В venv установлен google-generativeai (старый), но не google-genai (новый).
# copywriter_v2 использует новый SDK. Это существующая проблема окружения.
# ---------------------------------------------------------------------------
def _patch_google_genai():
    """Добавляет заглушку google.genai в sys.modules если её нет."""
    if "google.genai" not in sys.modules:
        mock_genai = MagicMock()
        # google пакет уже в sys.modules (google-auth и т.д.) — добавляем подмодуль
        sys.modules["google.genai"] = mock_genai
        sys.modules["google.genai.types"] = MagicMock()


_patch_google_genai()


@pytest.fixture(autouse=True)
def _clean_notifications():
    """Очищаем ленту событий перед и после каждого теста."""
    clear_events()
    yield
    clear_events()


# ---------------------------------------------------------------------------
# get_latest_fb_lead_ts
# ---------------------------------------------------------------------------

class TestGetLatestFbLeadTs:
    """Функция get_latest_fb_lead_ts: парсинг ответа AMO."""

    def _make_amo_response(self, leads: list) -> dict:
        """Строит фейковый ответ AMO _embedded."""
        return {"_embedded": {"leads": leads}}

    def _make_lead_with_fb_id(self, created_at: int, fb_lead_id: str = "123456") -> dict:
        """Лид с полем fb_lead_id (FB Lead Ads).

        Имя поля берём из конфига AMO_FB_LEAD_ID_FIELD (по умолчанию "fb_lead_id").
        """
        from config import AMO_FB_LEAD_ID_FIELD
        return {
            "id": 1,
            "created_at": created_at,
            "custom_fields_values": [
                {
                    "field_name": AMO_FB_LEAD_ID_FIELD,
                    "values": [{"value": fb_lead_id}],
                }
            ],
        }

    def _make_lead_without_fb_id(self, created_at: int) -> dict:
        """Лид без поля fb_lead_id (органика / другой источник)."""
        return {
            "id": 2,
            "created_at": created_at,
            "custom_fields_values": [
                {
                    "field_name": "utm_source",
                    "values": [{"value": "google"}],
                }
            ],
        }

    def test_returns_created_at_of_fb_lead(self):
        """Из двух лидов возвращает created_at того, у кого есть fb_lead_id."""
        ts_fb = int(time.time()) - 600     # FB-лид — 10 минут назад
        ts_org = int(time.time()) - 300    # Органика — 5 минут назад, но без FB ID

        leads = [
            self._make_lead_without_fb_id(ts_org),   # первый (свежее, но без FB ID)
            self._make_lead_with_fb_id(ts_fb),        # второй (старее, но с FB ID)
        ]
        fake_response = self._make_amo_response(leads)

        with patch("integrations.amo._amo_get", return_value=fake_response):
            from integrations.amo import get_latest_fb_lead_ts
            result = get_latest_fb_lead_ts()

        assert result == ts_fb

    def test_returns_none_when_no_fb_leads(self):
        """Если в выборке нет ни одного FB-лида — возвращает None."""
        leads = [
            self._make_lead_without_fb_id(int(time.time()) - 60),
        ]
        fake_response = self._make_amo_response(leads)

        with patch("integrations.amo._amo_get", return_value=fake_response):
            from integrations.amo import get_latest_fb_lead_ts
            result = get_latest_fb_lead_ts()

        assert result is None

    def test_returns_none_on_amo_error(self):
        """При ошибке AMO (исключение) возвращает None без прокидывания."""
        with patch("integrations.amo._amo_get", side_effect=Exception("AMO 500")):
            from integrations.amo import get_latest_fb_lead_ts
            result = get_latest_fb_lead_ts()

        assert result is None

    def test_returns_max_ts_among_multiple_fb_leads(self):
        """AMO отдаёт лиды в порядке desc → первый FB-лид и есть самый свежий.

        Имитируем desc-сортировку: новый лид стоит первым в списке.
        Функция возвращает created_at первого найденного FB-лида (= максимальный).
        """
        ts_old = int(time.time()) - 3600
        ts_new = int(time.time()) - 60

        # desc-порядок: новый лид первым
        leads = [
            self._make_lead_with_fb_id(ts_new, fb_lead_id="222"),
            self._make_lead_with_fb_id(ts_old, fb_lead_id="111"),
        ]
        fake_response = self._make_amo_response(leads)

        with patch("integrations.amo._amo_get", return_value=fake_response):
            from integrations.amo import get_latest_fb_lead_ts
            result = get_latest_fb_lead_ts()

        assert result == ts_new

    def test_empty_leads_list(self):
        """Пустой список лидов → None."""
        fake_response = self._make_amo_response([])

        with patch("integrations.amo._amo_get", return_value=fake_response):
            from integrations.amo import get_latest_fb_lead_ts
            result = get_latest_fb_lead_ts()

        assert result is None


# ---------------------------------------------------------------------------
# send_telegram
# ---------------------------------------------------------------------------

class TestSendTelegram:
    """Функция send_telegram: отправка в Telegram."""

    def test_returns_false_when_token_missing(self):
        """Без токена/chat_id возвращает False, не падает."""
        with patch("config.TELEGRAM_BOT_TOKEN", ""), \
             patch("config.TELEGRAM_CHAT_ID", ""):
            from services.notifications import send_telegram
            result = send_telegram("тест")

        assert result is False

    def test_returns_false_when_chat_id_missing(self):
        """Без chat_id (None) возвращает False."""
        with patch("config.TELEGRAM_BOT_TOKEN", "bot123"), \
             patch("config.TELEGRAM_CHAT_ID", None):
            from services.notifications import send_telegram
            result = send_telegram("тест")

        assert result is False

    def test_returns_true_via_lazy_import_mock(self):
        """send_telegram возвращает True при ok=True (патчим через sys.modules)."""
        import sys
        mock_requests = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_requests.post.return_value = mock_resp

        # send_telegram делает lazy import requests внутри функции
        with patch.dict(sys.modules, {"requests": mock_requests}), \
             patch("config.TELEGRAM_BOT_TOKEN", "token-abc"), \
             patch("config.TELEGRAM_CHAT_ID", "123456"):
            # Импортируем ПОСЛЕ патча sys.modules
            import importlib
            import services.notifications as notif_mod
            importlib.reload(notif_mod)
            result = notif_mod.send_telegram("Привет")

        assert result is True
        mock_requests.post.assert_called_once()

    def test_returns_false_on_request_exception(self):
        """При исключении в requests.post возвращает False."""
        import sys
        mock_requests = MagicMock()
        mock_requests.post.side_effect = ConnectionError("timeout")

        with patch.dict(sys.modules, {"requests": mock_requests}), \
             patch("config.TELEGRAM_BOT_TOKEN", "token-abc"), \
             patch("config.TELEGRAM_CHAT_ID", "123456"):
            import importlib
            import services.notifications as notif_mod
            importlib.reload(notif_mod)
            result = notif_mod.send_telegram("Ошибка")

        assert result is False

    def test_returns_false_when_ok_false(self):
        """Если Telegram вернул ok=False — возвращаем False."""
        import sys
        mock_requests = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False, "description": "Bad Request"}
        mock_requests.post.return_value = mock_resp

        with patch.dict(sys.modules, {"requests": mock_requests}), \
             patch("config.TELEGRAM_BOT_TOKEN", "token-abc"), \
             patch("config.TELEGRAM_CHAT_ID", "123456"):
            import importlib
            import services.notifications as notif_mod
            importlib.reload(notif_mod)
            result = notif_mod.send_telegram("тест")

        assert result is False


# ---------------------------------------------------------------------------
# _cron_fb_lead_watchdog
# ---------------------------------------------------------------------------

class TestFbLeadWatchdog:
    """Логика сторожа: тишина, алерт, дедупликация, восстановление."""

    def _now_local(self) -> datetime:
        """Текущее время в часовом поясе CityA."""
        TZ = timezone(timedelta(hours=5))
        return datetime.now(TZ)

    def _ts_minutes_before(self, fake_now: "datetime", minutes: float) -> int:
        """unix-timestamp N минут до переданного fake_now."""
        dt = fake_now - timedelta(minutes=minutes)
        return int(dt.timestamp())

    def _reset_watchdog_state(self):
        """Сбрасываем состояние сторожа перед тестом."""
        import web.app as app_mod
        app_mod._fb_watchdog_state["alerted"] = False
        app_mod._fb_watchdog_state["last_alert_at"] = None

    def _set_watchdog_alerted(self, last_alert_minutes_ago: float = 1.0, relative_to=None):
        """Устанавливаем флаг — уже был алерт.

        last_alert_at выставляем относительно relative_to (или real now).
        По умолчанию last_alert_at = 1 минуту назад → повтор ещё не должен уйти.
        Если тест патчит datetime.now — передай fake_now в relative_to, иначе
        сравнение (fake_now - last_alert_at) даст непредсказуемый результат.
        """
        import web.app as app_mod
        from datetime import datetime, timedelta, timezone
        TZ = timezone(timedelta(hours=5))
        if relative_to is None:
            relative_to = datetime.now(TZ)
        app_mod._fb_watchdog_state["alerted"] = True
        app_mod._fb_watchdog_state["last_alert_at"] = relative_to - timedelta(minutes=last_alert_minutes_ago)

    def test_smoke_no_crash_when_ts_none(self):
        """Smoke: если get_latest_fb_lead_ts вернул None — функция не падает."""
        self._reset_watchdog_state()
        with patch("integrations.amo.get_latest_fb_lead_ts", return_value=None):
            from web.app import _cron_fb_lead_watchdog
            _cron_fb_lead_watchdog()  # не должна бросать исключение

    def test_alert_sent_when_silence_exceeds_threshold(self):
        """Если тишина > 60 мин (дневной порог) в рабочее время — отправляется алерт."""
        self._reset_watchdog_state()

        TZ = timezone(timedelta(hours=5))
        # fake_now = 14:00 CityA сегодня
        fake_now = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
        # ts 130 минут до fake_now — превышает порог 60 мин (дневной)
        ts_old = self._ts_minutes_before(fake_now, 130)

        with patch("integrations.amo.get_latest_fb_lead_ts", return_value=ts_old), \
             patch("services.notifications.send_critical_alert") as mock_alert, \
             patch("services.notifications.send_telegram", return_value=False), \
             patch("services.notifications._try_send_email"), \
             patch("web.app.datetime") as mock_dt:
            # Возвращаем fake_now из datetime.now; fromtimestamp оставляем реальным
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp.side_effect = datetime.fromtimestamp
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            from web.app import _cron_fb_lead_watchdog
            _cron_fb_lead_watchdog()

        mock_alert.assert_called_once()
        call_args = mock_alert.call_args
        assert "не падают" in call_args[0][0]  # title содержит "не падают"

    def test_no_duplicate_alert(self):
        """Повторный вызов при уже выставленном флаге НЕ шлёт второй алерт."""
        TZ = timezone(timedelta(hours=5))
        fake_now = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
        # last_alert_at = 1 минута до fake_now → 3ч интервал не вышел → нет повтора
        self._set_watchdog_alerted(last_alert_minutes_ago=1, relative_to=fake_now)

        ts_old = self._ts_minutes_before(fake_now, 130)

        with patch("integrations.amo.get_latest_fb_lead_ts", return_value=ts_old), \
             patch("services.notifications.send_critical_alert") as mock_alert, \
             patch("services.notifications.send_telegram", return_value=False), \
             patch("services.notifications._try_send_email"), \
             patch("web.app.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp.side_effect = datetime.fromtimestamp
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            from web.app import _cron_fb_lead_watchdog
            _cron_fb_lead_watchdog()

        mock_alert.assert_not_called()

    def test_recovery_alert_sent_when_leads_resume(self):
        """Если после тревоги лиды пошли снова — шлём recovery и сбрасываем флаг."""
        TZ = timezone(timedelta(hours=5))
        fake_now = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
        self._set_watchdog_alerted(relative_to=fake_now)
        # Последний лид всего 5 минут до fake_now — норм
        ts_recent = self._ts_minutes_before(fake_now, 5)

        with patch("integrations.amo.get_latest_fb_lead_ts", return_value=ts_recent), \
             patch("services.notifications.send_critical_alert") as mock_alert, \
             patch("services.notifications.send_telegram", return_value=False), \
             patch("services.notifications._try_send_email"), \
             patch("web.app.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp.side_effect = datetime.fromtimestamp
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            from web.app import _cron_fb_lead_watchdog
            _cron_fb_lead_watchdog()

        mock_alert.assert_called_once()
        call_args = mock_alert.call_args
        assert "восстановился" in call_args[0][1]  # detail содержит "восстановился"

        # Флаги сброшены
        import web.app as app_mod
        assert app_mod._fb_watchdog_state["alerted"] is False
        assert app_mod._fb_watchdog_state["last_alert_at"] is None

    def test_alerted_flag_set_after_first_alert(self):
        """После первого алерта флаг _fb_watchdog_state['alerted'] = True."""
        self._reset_watchdog_state()

        TZ = timezone(timedelta(hours=5))
        fake_now = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
        ts_old = self._ts_minutes_before(fake_now, 130)

        with patch("integrations.amo.get_latest_fb_lead_ts", return_value=ts_old), \
             patch("services.notifications.send_critical_alert"), \
             patch("services.notifications.send_telegram", return_value=False), \
             patch("services.notifications._try_send_email"), \
             patch("web.app.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp.side_effect = datetime.fromtimestamp
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            from web.app import _cron_fb_lead_watchdog
            _cron_fb_lead_watchdog()

        import web.app as app_mod
        assert app_mod._fb_watchdog_state["alerted"] is True

    def test_repeat_not_sent_before_3h(self):
        """Повтор НЕ шлётся если с последнего алерта прошло менее 3 часов (1 час)."""
        self._reset_watchdog_state()
        TZ = timezone(timedelta(hours=5))
        fake_now = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
        # Устанавливаем: alerted=True, last_alert_at = 1 час до fake_now
        self._set_watchdog_alerted(last_alert_minutes_ago=60, relative_to=fake_now)

        ts_old = self._ts_minutes_before(fake_now, 130)

        with patch("integrations.amo.get_latest_fb_lead_ts", return_value=ts_old), \
             patch("services.notifications.send_critical_alert") as mock_alert, \
             patch("services.notifications.send_telegram", return_value=False), \
             patch("services.notifications._try_send_email"), \
             patch("web.app.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp.side_effect = datetime.fromtimestamp
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            from web.app import _cron_fb_lead_watchdog
            _cron_fb_lead_watchdog()

        mock_alert.assert_not_called()

    def test_repeat_sent_after_3h(self):
        """Повтор шлётся если с последнего алерта прошло >= 3 часов (3.1 ч)."""
        self._reset_watchdog_state()
        TZ = timezone(timedelta(hours=5))
        fake_now = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
        # Устанавливаем: alerted=True, last_alert_at = 3.1 часа до fake_now
        self._set_watchdog_alerted(last_alert_minutes_ago=186, relative_to=fake_now)

        ts_old = self._ts_minutes_before(fake_now, 250)

        with patch("integrations.amo.get_latest_fb_lead_ts", return_value=ts_old), \
             patch("services.notifications.send_critical_alert") as mock_alert, \
             patch("services.notifications.send_telegram", return_value=False), \
             patch("services.notifications._try_send_email"), \
             patch("web.app.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp.side_effect = datetime.fromtimestamp
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            from web.app import _cron_fb_lead_watchdog
            _cron_fb_lead_watchdog()

        mock_alert.assert_called_once()
        call_args = mock_alert.call_args
        assert "всё ещё" in call_args[0][1]  # detail содержит "всё ещё"

    def test_none_and_alerted_sends_repeat_after_3h(self):
        """ts=None и alerted=True → повтор шлётся если прошло >= 3 часов."""
        self._reset_watchdog_state()
        TZ = timezone(timedelta(hours=5))
        fake_now = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
        self._set_watchdog_alerted(last_alert_minutes_ago=186, relative_to=fake_now)

        with patch("integrations.amo.get_latest_fb_lead_ts", return_value=None), \
             patch("services.notifications.send_critical_alert") as mock_alert, \
             patch("services.notifications.send_telegram", return_value=False), \
             patch("services.notifications._try_send_email"), \
             patch("web.app.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp.side_effect = datetime.fromtimestamp
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            from web.app import _cron_fb_lead_watchdog
            _cron_fb_lead_watchdog()

        mock_alert.assert_called_once()
        call_args = mock_alert.call_args
        assert ">48ч" in call_args[0][1]

    def test_none_and_alerted_no_repeat_before_3h(self):
        """ts=None и alerted=True → повтор НЕ шлётся если с последнего алерта < 3 часов."""
        self._reset_watchdog_state()
        TZ = timezone(timedelta(hours=5))
        fake_now = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
        self._set_watchdog_alerted(last_alert_minutes_ago=60, relative_to=fake_now)

        with patch("integrations.amo.get_latest_fb_lead_ts", return_value=None), \
             patch("services.notifications.send_critical_alert") as mock_alert, \
             patch("services.notifications.send_telegram", return_value=False), \
             patch("services.notifications._try_send_email"), \
             patch("web.app.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp.side_effect = datetime.fromtimestamp
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            from web.app import _cron_fb_lead_watchdog
            _cron_fb_lead_watchdog()

        mock_alert.assert_not_called()

    def test_recovery_resets_both_fields(self):
        """Восстановление сбрасывает alerted=False и last_alert_at=None."""
        self._reset_watchdog_state()
        TZ = timezone(timedelta(hours=5))
        fake_now = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
        self._set_watchdog_alerted(last_alert_minutes_ago=60, relative_to=fake_now)
        ts_recent = self._ts_minutes_before(fake_now, 5)

        with patch("integrations.amo.get_latest_fb_lead_ts", return_value=ts_recent), \
             patch("services.notifications.send_critical_alert"), \
             patch("services.notifications.send_telegram", return_value=False), \
             patch("services.notifications._try_send_email"), \
             patch("web.app.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.fromtimestamp.side_effect = datetime.fromtimestamp
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            from web.app import _cron_fb_lead_watchdog
            _cron_fb_lead_watchdog()

        import web.app as app_mod
        assert app_mod._fb_watchdog_state["alerted"] is False
        assert app_mod._fb_watchdog_state["last_alert_at"] is None


# ---------------------------------------------------------------------------
# _fb_silence_threshold_min (хелпер: порог по часу суток)
# ---------------------------------------------------------------------------

class TestFbSilenceThresholdMin:
    """Хелпер _fb_silence_threshold_min: день=60, ночь=180."""

    @pytest.fixture(autouse=True)
    def _import_helper(self):
        from web.app import _fb_silence_threshold_min
        self.f = _fb_silence_threshold_min

    def test_day_hours(self):
        """Дневные часы 08:00–21:59 → порог 60 мин."""
        assert self.f(8) == 60
        assert self.f(9) == 60
        assert self.f(12) == 60
        assert self.f(21) == 60

    def test_night_hours(self):
        """Ночные часы 22:00–07:59 → порог 180 мин."""
        assert self.f(22) == 180
        assert self.f(23) == 180
        assert self.f(0) == 180
        assert self.f(2) == 180
        assert self.f(7) == 180

    def test_boundary_day_start(self):
        """08:00 — первый дневной час, должен быть 60."""
        assert self.f(8) == 60

    def test_boundary_night_start(self):
        """22:00 — первый ночной час, должен быть 180."""
        assert self.f(22) == 180

    def test_boundary_day_end(self):
        """21:xx — последний дневной час, должен быть 60."""
        assert self.f(21) == 60

    def test_boundary_night_end(self):
        """07:xx — последний ночной час, должен быть 180."""
        assert self.f(7) == 180


# ---------------------------------------------------------------------------
# _should_send_heartbeat (хелпер: пора ли слать пульс)
# ---------------------------------------------------------------------------

class TestShouldSendHeartbeat:
    """Хелпер _should_send_heartbeat: день (08-21) — раз в час, ночь (22-07) — раз в 3 часа."""

    @pytest.fixture(autouse=True)
    def _import_helper(self):
        from web.app import _should_send_heartbeat
        self.f = _should_send_heartbeat

    def _make_now(self, hour: int) -> datetime:
        """Создаёт datetime в TZ CityA с заданным часом (дата 2026-06-09)."""
        TZ = timezone(timedelta(hours=5))
        return datetime(2026, 6, 9, hour, 0, tzinfo=TZ)

    def test_day_no_last_sent_returns_true(self):
        """День, час 12, last_sent_at=None → слать (никогда ещё не слали)."""
        now = self._make_now(12)
        assert self.f(now, None) is True

    def test_day_sent_30min_ago_returns_false(self):
        """День, час 12, отправляли 30 мин назад → ещё рано (интервал 60 мин)."""
        now = self._make_now(12)
        last = now - timedelta(minutes=30)
        assert self.f(now, last) is False

    def test_day_sent_61min_ago_returns_true(self):
        """День, час 12, отправляли 61 мин назад → пора (интервал 60 мин истёк)."""
        now = self._make_now(12)
        last = now - timedelta(minutes=61)
        assert self.f(now, last) is True

    def test_night_sent_2h_ago_returns_false(self):
        """Ночь, час 2, отправляли 2 часа назад → ещё рано (интервал 3 часа)."""
        now = self._make_now(2)
        last = now - timedelta(hours=2)
        assert self.f(now, last) is False

    def test_night_sent_3h1min_ago_returns_true(self):
        """Ночь, час 2, отправляли 3ч 1 мин назад → пора (интервал 3 часа истёк)."""
        now = self._make_now(2)
        last = now - timedelta(hours=3, minutes=1)
        assert self.f(now, last) is True

    def test_boundary_hour8_day_interval_61min(self):
        """Граница: час 8 → дневной интервал 60 мин; 61 мин назад → True."""
        now = self._make_now(8)
        last = now - timedelta(minutes=61)
        assert self.f(now, last) is True

    def test_boundary_hour22_night_interval_61min(self):
        """Граница: час 22 → ночной интервал 180 мин; 61 мин назад → False."""
        now = self._make_now(22)
        last = now - timedelta(minutes=61)
        assert self.f(now, last) is False
