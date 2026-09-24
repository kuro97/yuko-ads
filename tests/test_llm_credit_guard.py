"""
Тесты стража кредитов Anthropic (services/llm_credit_guard.py, Задача A).

Без стража бот молча жёг LLM-вызовы на «credit balance is too low». Страж:
- распознаёт ошибку кредитов по тексту исключения;
- шлёт критический алерт ОДИН раз в час (антиспам через state-файл);
- бросает CreditBalanceError, чтобы батч-циклы разметки прервались сразу.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import llm_credit_guard as guard
from services.llm_credit_guard import CreditBalanceError, is_credit_error

_TZ = timezone(timedelta(hours=5))


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Уводит state-файл дедупа алерта во временный каталог."""
    state_file = tmp_path / "llm_credit_alert_state.json"
    monkeypatch.setattr(guard, "_STATE_FILE", state_file)
    return state_file


# ---------------------------------------------------------------------------
# is_credit_error — распознавание
# ---------------------------------------------------------------------------

class TestIsCreditError:
    def test_реальное_сообщение_anthropic(self):
        exc = Exception(
            "Error code: 400 - Your credit balance is too low to access the "
            "Anthropic API. Please go to Plan & Billing to upgrade or purchase credits."
        )
        assert is_credit_error(exc) is True

    def test_маркер_purchase_credits(self):
        assert is_credit_error(Exception("insufficient credit — purchase credits")) is True

    def test_обычная_ошибка_не_кредиты(self):
        assert is_credit_error(Exception("rate limit exceeded")) is False
        assert is_credit_error(Exception("Connection timeout")) is False
        assert is_credit_error(ValueError("bad json from model")) is False

    def test_пустая_ошибка(self):
        assert is_credit_error(Exception("")) is False


# ---------------------------------------------------------------------------
# alert_credit_exhausted — антиспам раз в час
# ---------------------------------------------------------------------------

class TestAlertDedup:
    def test_первый_алерт_уходит(self, isolated_state):
        with patch("services.notifications.send_critical_alert") as mock_alert:
            sent = guard.alert_credit_exhausted("ctx", "credit balance is too low")
        assert sent is True
        mock_alert.assert_called_once()

    def test_повтор_в_течение_часа_задедуплен(self, isolated_state):
        now = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ)
        with patch("services.notifications.send_critical_alert") as mock_alert:
            first = guard.alert_credit_exhausted("ctx", "err", now=now)
            second = guard.alert_credit_exhausted("ctx", "err", now=now + timedelta(minutes=30))
        assert first is True
        assert second is False
        mock_alert.assert_called_once()

    def test_после_часа_алерт_снова_уходит(self, isolated_state):
        now = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ)
        with patch("services.notifications.send_critical_alert") as mock_alert:
            guard.alert_credit_exhausted("ctx", "err", now=now)
            again = guard.alert_credit_exhausted("ctx", "err", now=now + timedelta(hours=2))
        assert again is True
        assert mock_alert.call_count == 2


# ---------------------------------------------------------------------------
# raise_if_credit_error — сигнал прерывания
# ---------------------------------------------------------------------------

class TestRaiseIfCreditError:
    def test_ошибка_кредитов_бросает_и_алертит(self, isolated_state):
        with patch("services.notifications.send_critical_alert") as mock_alert:
            with pytest.raises(CreditBalanceError):
                guard.raise_if_credit_error("mod.fn", Exception("credit balance is too low"))
        mock_alert.assert_called_once()

    def test_обычная_ошибка_не_бросает_и_не_алертит(self, isolated_state):
        with patch("services.notifications.send_critical_alert") as mock_alert:
            # Не должно бросить и не должно слать алерт
            guard.raise_if_credit_error("mod.fn", Exception("network timeout"))
        mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# Интеграция: батч-цикл backfill_target_product прерывается на ошибке кредитов
# ---------------------------------------------------------------------------

class TestBatchLoopBreaks:
    def test_backfill_прерывается_и_не_молотит(self, tmp_path, monkeypatch):
        """3 живых строки без target_product; на первом же LLM-вызове кредиты кончились →
        цикл прерывается сразу (credit_aborted=True), остальные строки не трогаем."""
        from services import creative_intelligence as ci

        ci.DB_PATH = None
        db_path = str(tmp_path / "kb.db")
        ci.init_kb(db_path)

        conn = ci._get_connection()
        for i in range(3):
            conn.execute(
                "INSERT INTO creative_kb (ad_id, ad_name, ad_body, status, effective_status, target_product) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (f"ad{i}", "нейтральное объявление", "обычный текст без ключевых слов", "ACTIVE", "ACTIVE"),
            )
        conn.commit()
        conn.close()

        monkeypatch.setattr(guard, "_STATE_FILE", tmp_path / "credit.json")

        # _llm_classify_product бросает ошибку кредитов на ПЕРВОМ вызове.
        # backfill импортирует его из services.product_tags → патчим там.
        with patch(
            "services.product_tags._llm_classify_product",
            side_effect=CreditBalanceError("credit balance is too low"),
        ) as mock_llm:
            result = ci.backfill_target_product()

        assert result["credit_aborted"] is True
        assert result["by_llm"] == 0          # ни один LLM-добор не засчитан (прервались)
        assert mock_llm.call_count == 1        # позвали ровно один раз и остановились
        ci.DB_PATH = None
