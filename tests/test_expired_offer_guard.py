"""
Тесты стража просроченных офферов (services/expired_offer_guard.py, Задача C).

Покрываем:
- разбор дат: месяцы L1, месяцы L2, DD.MM, явный год, будущее (не флажим),
  «следующий год» (январь в июле — не флажим);
- извлечение текста креатива (body/title/object_story_spec);
- «слепая зона» _contains_date;
- оркестратор run_expired_offer_guard: мастер-ключ, алерт с кнопками «⏸ Остановить»,
  фильтр по расходу>0 вчера, счётчик непроверяемых.
"""

import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import expired_offer_guard as guard

_TZ = timezone(timedelta(hours=5))
_TODAY = date(2026, 7, 15)


# ---------------------------------------------------------------------------
# Разбор истёкших дат
# ---------------------------------------------------------------------------

class TestFindExpiredDeadline:
    def test_русский_месяц_истёк(self):
        assert guard._find_expired_deadline("Акция до 15 июня", _TODAY) == date(2026, 6, 15)

    def test_русский_месяц_истёк_другой(self):
        assert guard._find_expired_deadline("Скидка до 30 июня!", _TODAY) == date(2026, 6, 30)

    def test_месяц_второго_языка_истёк(self):
        # june = июнь (словарь L2)
        assert guard._find_expired_deadline("offer until 15 june", _TODAY) == date(2026, 6, 15)

    def test_месяц_второго_языка_точная_форма(self):
        # may = май — точная словоформа L2, не спутается с рус. «ма»
        assert guard._find_expired_deadline("20 may", _TODAY) == date(2026, 5, 20)
        # сравнение точное: «marketing» не месяц «mar»
        assert guard._find_expired_deadline("15 marketing ideas", _TODAY) is None

    def test_числовая_дата_истекла(self):
        assert guard._find_expired_deadline("PRODA CityB 30.06", _TODAY) == date(2026, 6, 30)

    def test_явный_год_в_прошлом(self):
        assert guard._find_expired_deadline("до 15.06.2025", _TODAY) == date(2025, 6, 15)

    def test_явный_год_2значный(self):
        assert guard._find_expired_deadline("15.06.25", _TODAY) == date(2025, 6, 15)

    def test_будущая_дата_не_флажится(self):
        assert guard._find_expired_deadline("до 20 августа", _TODAY) is None
        assert guard._find_expired_deadline("20.08", _TODAY) is None

    def test_следующий_год_не_флажится(self):
        # «до 5 января» в июле — рекламодатель имел в виду январь СЛЕДУЮЩЕГО года
        assert guard._find_expired_deadline("до 5 января", _TODAY) is None

    def test_явный_будущий_год_не_флажится(self):
        assert guard._find_expired_deadline("15.06.2027", _TODAY) is None

    def test_нет_даты(self):
        assert guard._find_expired_deadline("Просто рекламный текст", _TODAY) is None

    def test_берётся_самая_ранняя_истёкшая(self):
        # две истёкшие даты — возвращаем раннюю
        assert guard._find_expired_deadline("с 10 июня по 30 июня", _TODAY) == date(2026, 6, 10)


class TestContainsDate:
    def test_есть_числовая_дата(self):
        assert guard._contains_date("CityB 15.06") is True

    def test_есть_словесная_дата(self):
        assert guard._contains_date("до 20 августа") is True

    def test_нет_даты(self):
        assert guard._contains_date("Видео без дат") is False


# ---------------------------------------------------------------------------
# Извлечение текста креатива
# ---------------------------------------------------------------------------

class TestExtractCreativeText:
    def test_body_и_title(self):
        creative = {"body": "текст оффера до 15 июня", "title": "Заголовок"}
        text = guard._extract_creative_text(creative)
        assert "до 15 июня" in text and "Заголовок" in text

    def test_object_story_spec_link_data(self):
        creative = {"object_story_spec": {"link_data": {"message": "скидка до 30 июня"}}}
        assert "до 30 июня" in guard._extract_creative_text(creative)

    def test_carousel_child_attachments(self):
        creative = {"object_story_spec": {"link_data": {"child_attachments": [
            {"name": "карточка", "description": "до 10 июня"},
        ]}}}
        assert "до 10 июня" in guard._extract_creative_text(creative)

    def test_пустой_креатив(self):
        assert guard._extract_creative_text(None) == ""
        assert guard._extract_creative_text({}) == ""


# ---------------------------------------------------------------------------
# Оркестратор run_expired_offer_guard
# ---------------------------------------------------------------------------

class TestRunGuard:
    def _now(self):
        return datetime(2026, 7, 15, 3, 5, tzinfo=_TZ)

    @pytest.fixture
    def isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_STATE_FILE", tmp_path / "guard_state.json")

    def test_мастер_ключ_выключен(self, isolated_state):
        with patch("services.autopilot.get_autopilot_config",
                   return_value={"expired_offer_guard": {"enabled": False}}):
            result = guard.run_expired_offer_guard(self._now())
        assert result["enabled"] is False
        assert result["skipped_reason"] == "disabled"

    def test_алерт_с_кнопками_по_просроченным_жёгшим_бюджет(self, isolated_state):
        ads = [
            # истёкшая дата в имени + жёг вчера → в алерт
            {"id": "111111111", "name": "CityA PRODA до 15 июня",
             "effective_status": "ACTIVE", "creative": {}},
            # будущая дата → не флажим
            {"id": "222222222", "name": "CityB до 20 августа",
             "effective_status": "ACTIVE", "creative": {}},
            # нет текста и нет даты в имени → непроверяемый
            {"id": "333333333", "name": "Видео креатив",
             "effective_status": "ACTIVE", "creative": {}},
        ]

        with patch("services.autopilot.get_autopilot_config",
                   return_value={"expired_offer_guard": {"enabled": True, "dedup_hours": 12}}), \
             patch.object(guard, "_fetch_active_ads", return_value=ads), \
             patch.object(guard, "_spent_yesterday", return_value={"111111111": 42.5}), \
             patch("services.telegram_bot.send_with_buttons", return_value=True) as mock_send:
            result = guard.run_expired_offer_guard(self._now())

        assert result["flagged"] == 1
        assert result["wasters"] == 1
        assert result["alerted"] == 1
        assert result["unverifiable"] == 1

        mock_send.assert_called_once()
        text, buttons = mock_send.call_args[0][0], mock_send.call_args[0][1]
        # Кнопка «Остановить» на просроченное объявление — callback pause:<ad_id>
        assert buttons == [[("⏸ Остановить", "pause:111111111")]]
        # Честная строка про непроверяемых
        assert "епроверяемых" in text and "1" in text

    def test_просроченный_но_без_расхода_не_алертит(self, isolated_state):
        ads = [{"id": "111111111", "name": "до 15 июня", "effective_status": "ACTIVE", "creative": {}}]
        with patch("services.autopilot.get_autopilot_config",
                   return_value={"expired_offer_guard": {"enabled": True}}), \
             patch.object(guard, "_fetch_active_ads", return_value=ads), \
             patch.object(guard, "_spent_yesterday", return_value={}), \
             patch("services.telegram_bot.send_with_buttons", return_value=True) as mock_send:
            result = guard.run_expired_offer_guard(self._now())
        assert result["flagged"] == 1
        assert result["wasters"] == 0
        assert result["alerted"] == 0
        mock_send.assert_not_called()

    def test_дедуп_по_ad_id(self, isolated_state):
        ads = [{"id": "111111111", "name": "до 15 июня", "effective_status": "ACTIVE", "creative": {}}]
        cfg = {"expired_offer_guard": {"enabled": True, "dedup_hours": 12}}
        with patch("services.autopilot.get_autopilot_config", return_value=cfg), \
             patch.object(guard, "_fetch_active_ads", return_value=ads), \
             patch.object(guard, "_spent_yesterday", return_value={"111111111": 10.0}), \
             patch("services.telegram_bot.send_with_buttons", return_value=True) as mock_send:
            first = guard.run_expired_offer_guard(datetime(2026, 7, 15, 3, 5, tzinfo=_TZ))
            second = guard.run_expired_offer_guard(datetime(2026, 7, 15, 4, 0, tzinfo=_TZ))
        assert first["alerted"] == 1
        assert second["alerted"] == 0  # задедуплен в течение 12ч
        mock_send.assert_called_once()

    def test_fb_недоступен_fail_safe(self, isolated_state):
        with patch("services.autopilot.get_autopilot_config",
                   return_value={"expired_offer_guard": {"enabled": True}}), \
             patch.object(guard, "_fetch_active_ads", side_effect=RuntimeError("fb down")):
            result = guard.run_expired_offer_guard(self._now())
        assert result["error"] is not None
        assert result["alerted"] == 0


# ---------------------------------------------------------------------------
# _spent_yesterday — реальный запрос к ad_daily_metrics
# ---------------------------------------------------------------------------

class TestSpentYesterday:
    def test_возвращает_только_расход_больше_нуля(self, tmp_path):
        from services import creative_intelligence as ci
        ci.DB_PATH = None
        ci.init_kb(str(tmp_path / "kb.db"))
        conn = ci._get_connection()
        conn.execute(
            "INSERT INTO ad_daily_metrics (ad_id, date, spend, day_since_launch) VALUES (?,?,?,?)",
            ("111", "2026-07-14", 42.5, 30),
        )
        conn.execute(
            "INSERT INTO ad_daily_metrics (ad_id, date, spend, day_since_launch) VALUES (?,?,?,?)",
            ("222", "2026-07-14", 0.0, 30),
        )
        conn.commit()
        conn.close()

        spent = guard._spent_yesterday(["111", "222", "333"], "2026-07-14")
        assert spent == {"111": 42.5}
        ci.DB_PATH = None

    def test_пустой_список(self, tmp_path):
        assert guard._spent_yesterday([], "2026-07-14") == {}
