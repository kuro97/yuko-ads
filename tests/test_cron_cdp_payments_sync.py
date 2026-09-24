"""
Тесты крона синка ERP-платежей _cron_cdp_payments_sync (Шаг B, ARCH-cdp-payments.md).

Крон раз в сутки в _CDP_PAYMENTS_SYNC_HOUR:xx CityA (после AMO-исходов 6:xx,
до пересчёта движка CDP к 09:30) зовёт cdp_payments.refresh_creative_kb_payments_erp
и пишет payments_erp/revenue_erp_lcy в creative_kb. Паттерн гейта — как у
_cron_db_backup/_cron_hypothesis_verdict (state с last_run_date, дедуп по дате).

Покрываем:
1. Гейт _should_run_cdp_payments_sync — час/дедуп по дате.
2. Happy path: в нужный час refresh вызывается один раз, state обновляется.
3. Гейт-скип вне часа — refresh не вызывается, state не трогается.
4. Повторный тик в том же часу того же дня — refresh не вызывается повторно.
5. Fail-safe: исключение из refresh не роняет крон наружу.
6. heartbeat-обёртка пишет отметку даже при гейт-скипе (critical=False).
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем тяжёлые зависимости ДО импорта web.app (как в других тестах кронов)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from web.app import (  # noqa: E402
    _cron_cdp_payments_sync,
    _should_run_cdp_payments_sync,
    _load_cdp_payments_sync_state,
    _save_cdp_payments_sync_state,
    _CDP_PAYMENTS_SYNC_HOUR,
)
import services.cron_heartbeat as hb  # noqa: E402

_TZ_LOCAL = timezone(timedelta(hours=5))


def _dt(hour: int, minute: int = 10, day: int = 2, month: int = 7, year: int = 2026) -> datetime:
    """Создаёт datetime для указанного часа CityA."""
    return datetime(year, month, day, hour, minute, tzinfo=_TZ_LOCAL)


# ---------------------------------------------------------------------------
# Фикстуры изоляции
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.fixture
def isolate_heartbeats(tmp_path, monkeypatch):
    """Перенаправляет cron_heartbeats.json на временный файл — изоляция между тестами."""
    hb_file = tmp_path / "cron_heartbeats.json"
    monkeypatch.setattr(hb, "_HB_FILE", hb_file)
    return hb_file


@pytest.fixture
def isolated_sync_state(tmp_path, monkeypatch):
    """Подменяет путь к state-файлу крона синка ERP-платежей на временный."""
    state_file = tmp_path / "cdp_payments_sync_cron_state.json"
    monkeypatch.setattr("web.app._CDP_PAYMENTS_SYNC_STATE", state_file)
    return state_file


_REFRESH_OK = {
    "window_from": "2026-05-03",
    "window_to": "2026-07-02",
    "ads_updated": 5,
    "payments_total": 12,
    "revenue_total_lcy": 150000.0,
    "error": None,
}


# ---------------------------------------------------------------------------
# Гейт _should_run_cdp_payments_sync — час/дедуп по дате
# ---------------------------------------------------------------------------

class TestShouldRunCdpPaymentsSyncGate:
    def test_правильный_час_ещё_не_бежал_сегодня_true(self):
        """Час == _CDP_PAYMENTS_SYNC_HOUR, last_run_date=None → True."""
        now = _dt(_CDP_PAYMENTS_SYNC_HOUR)
        assert _should_run_cdp_payments_sync(now, last_run_date=None) is True

    def test_правильный_час_бежал_вчера_true(self):
        """Час совпадает, last_run_date=вчера → True (новый день)."""
        now = _dt(_CDP_PAYMENTS_SYNC_HOUR, day=2)
        assert _should_run_cdp_payments_sync(now, last_run_date="2026-07-01") is True

    def test_неправильный_час_false(self):
        """Час != _CDP_PAYMENTS_SYNC_HOUR → False, независимо от даты."""
        now = _dt(_CDP_PAYMENTS_SYNC_HOUR + 3)
        assert _should_run_cdp_payments_sync(now, last_run_date=None) is False

    def test_уже_бежал_сегодня_false(self):
        """Час совпадает, но last_run_date == сегодня → False (не дублируем)."""
        now = _dt(_CDP_PAYMENTS_SYNC_HOUR, day=2)
        assert _should_run_cdp_payments_sync(now, last_run_date="2026-07-02") is False


# ---------------------------------------------------------------------------
# Happy path + гейт-скип + дедуп + fail-safe
# ---------------------------------------------------------------------------

class TestCronCdpPaymentsSync:
    def test_в_нужный_час_зовёт_refresh_и_пишет_state(
        self, isolate_heartbeats, isolated_sync_state,
    ):
        """Час == _CDP_PAYMENTS_SYNC_HOUR, слот свободен → refresh вызван с
        months_back=2, state.last_run_date обновлён на сегодня."""
        now = _dt(_CDP_PAYMENTS_SYNC_HOUR)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.cdp_payments.refresh_creative_kb_payments_erp",
                   return_value=_REFRESH_OK) as mock_refresh:
            mock_dt.now.return_value = now
            _cron_cdp_payments_sync()

        mock_refresh.assert_called_once_with(months_back=2)
        state = _load_cdp_payments_sync_state()
        assert state["last_run_date"] == "2026-07-02"

    def test_вне_часа_refresh_не_вызывается(
        self, isolate_heartbeats, isolated_sync_state,
    ):
        """Час != _CDP_PAYMENTS_SYNC_HOUR → refresh не вызывается, state не трогается."""
        now = _dt(_CDP_PAYMENTS_SYNC_HOUR + 5)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.cdp_payments.refresh_creative_kb_payments_erp") as mock_refresh:
            mock_dt.now.return_value = now
            _cron_cdp_payments_sync()

        mock_refresh.assert_not_called()
        state = _load_cdp_payments_sync_state()
        assert state == {}

    def test_повторный_тик_в_том_же_часу_не_дублирует(
        self, isolate_heartbeats, isolated_sync_state,
    ):
        """Слот уже отработал сегодня (state.last_run_date == сегодня) →
        повторный тик в том же часу НЕ вызывает refresh снова."""
        now = _dt(_CDP_PAYMENTS_SYNC_HOUR, minute=40)
        _save_cdp_payments_sync_state({"last_run_date": now.date().isoformat()})

        with patch("web.app.datetime") as mock_dt, \
             patch("services.cdp_payments.refresh_creative_kb_payments_erp") as mock_refresh:
            mock_dt.now.return_value = now
            _cron_cdp_payments_sync()

        mock_refresh.assert_not_called()

    def test_ошибка_в_результате_логируется_не_падает(
        self, isolate_heartbeats, isolated_sync_state,
    ):
        """refresh вернул error (CDP/AMO лёг, fail-closed) → крон логирует
        warning, но не падает и не бросает исключение."""
        now = _dt(_CDP_PAYMENTS_SYNC_HOUR)
        failed_result = {**_REFRESH_OK, "ads_updated": 0, "error": "CDP недоступен"}
        with patch("web.app.datetime") as mock_dt, \
             patch("services.cdp_payments.refresh_creative_kb_payments_erp",
                   return_value=failed_result):
            mock_dt.now.return_value = now
            # Не должно бросить исключение
            _cron_cdp_payments_sync()

        # State всё равно помечен (слот занят — не долбим CDP каждый тик в этом часу)
        state = _load_cdp_payments_sync_state()
        assert state["last_run_date"] == "2026-07-02"

    def test_исключение_из_refresh_не_роняет_крон(
        self, isolate_heartbeats, isolated_sync_state,
    ):
        """refresh_creative_kb_payments_erp бросил исключение (неожиданный баг) →
        крон не падает наружу (fail-safe try/except)."""
        now = _dt(_CDP_PAYMENTS_SYNC_HOUR)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.cdp_payments.refresh_creative_kb_payments_erp",
                   side_effect=RuntimeError("boom")):
            mock_dt.now.return_value = now
            # Не должно бросить исключение
            _cron_cdp_payments_sync()

    def test_битый_state_файл_не_падает(
        self, isolate_heartbeats, isolated_sync_state,
    ):
        """state-файл содержит невалидный JSON → _load вернёт {}, крон отрабатывает
        как при первом запуске (last_run_date отсутствует)."""
        isolated_sync_state.write_text("{not valid json", encoding="utf-8")
        now = _dt(_CDP_PAYMENTS_SYNC_HOUR)

        with patch("web.app.datetime") as mock_dt, \
             patch("services.cdp_payments.refresh_creative_kb_payments_erp",
                   return_value=_REFRESH_OK) as mock_refresh:
            mock_dt.now.return_value = now
            _cron_cdp_payments_sync()

        mock_refresh.assert_called_once()

    def test_heartbeat_пишет_отметку_даже_при_гейт_скипе(
        self, isolate_heartbeats, isolated_sync_state,
    ):
        """_cron_cdp_payments_sync обёрнут @heartbeat(..., 30, critical=False) —
        успешный прогон (даже гейт-скип вне часа, т.к. исключения не было)
        пишет отметку в cron_heartbeats.json."""
        now = _dt(_CDP_PAYMENTS_SYNC_HOUR + 4)  # вне часа -> гейт-скип, без исключения
        with patch("web.app.datetime") as mock_dt, \
             patch("services.cdp_payments.refresh_creative_kb_payments_erp") as mock_refresh:
            mock_dt.now.return_value = now
            _cron_cdp_payments_sync()

        mock_refresh.assert_not_called()
        data = hb.load_heartbeats()
        assert "_cron_cdp_payments_sync" in data["heartbeats"]
        entry = data["heartbeats"]["_cron_cdp_payments_sync"]
        assert entry["expected_minutes"] == 30
        assert entry["critical"] is False
