"""
Тесты services/telegram_bot.py — callback stop_launch:<id> + _execute_stop_launch
(ARCH-auto-launch-report.md).

Мокаем внешние границы:
- services.auto_launch (get_launch_stop_entry/mark_launch_stopped)
- services.autopilot.approve_pause
- services.notifications.send_telegram
- services.telegram_bot._answer_callback

Конвенции — как tests/test_telegram_brief_approval.py (_make_cb, patch_config, OWNER_ID).
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
    sys.modules["google.genai.types"] = MagicMock()


OWNER_ID = "111"


def _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="stop_launch:abc123def456", cb_id="cid1"):
    """Создаёт словарь callback_query (как в test_telegram_bot.py)."""
    return {
        "id": cb_id,
        "from": {"id": int(from_id)},
        "message": {"chat": {"id": int(chat_id)}},
        "data": data,
    }


def _make_entry(name="Петров / Тема А", ad_ids=None, stopped=False):
    """Тестовая запись stop_map."""
    return {
        "name": name,
        "ad_ids": ad_ids if ad_ids is not None else ["111", "222"],
        "stopped": stopped,
        "at": "2026-07-08T10:00:00+05:00",
    }


@pytest.fixture()
def patch_config():
    """Патч config.TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID."""
    with patch("config.TELEGRAM_BOT_TOKEN", "test-token"), \
         patch("config.TELEGRAM_CHAT_ID", OWNER_ID):
        yield


# ===========================================================================
# _execute_stop_launch
# ===========================================================================

class TestExecuteStopLaunch:
    def test_happy_все_паузы_ок(self, patch_config):
        """Даже happy legacy state не разрешает прямую паузу."""
        with patch("services.auto_launch.get_launch_stop_entry", return_value=_make_entry()), \
             patch("services.auto_launch.mark_launch_stopped") as mock_mark, \
             patch("services.autopilot.approve_pause", return_value=(True, "имя")) as mock_pause, \
             patch("services.notifications.send_telegram") as mock_tg, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_stop_launch
            _execute_stop_launch("abc123def456", "cbid1")

            mock_pause.assert_not_called()
            mock_mark.assert_not_called()
            mock_tg.assert_not_called()
            assert "устарела" in mock_answer.call_args.args[1].lower()

    def test_идемпотентность_уже_остановлено(self, patch_config):
        """entry.stopped=True -> ответ «Уже остановлено», approve_pause НЕ вызван,
        mark_launch_stopped НЕ вызван."""
        with patch("services.auto_launch.get_launch_stop_entry", return_value=_make_entry(stopped=True)), \
             patch("services.auto_launch.mark_launch_stopped") as mock_mark, \
             patch("services.autopilot.approve_pause") as mock_pause, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_stop_launch
            _execute_stop_launch("abc123def456", "cbid1")

            mock_pause.assert_not_called()
            mock_mark.assert_not_called()
            mock_answer.assert_called_once()
            assert "устарела" in mock_answer.call_args[0][1].lower()

    def test_запуск_не_найден(self, patch_config):
        """get_launch_stop_entry вернул None -> ответ «Запуск не найден»,
        approve_pause НЕ вызван."""
        with patch("services.auto_launch.get_launch_stop_entry", return_value=None), \
             patch("services.autopilot.approve_pause") as mock_pause, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_stop_launch
            _execute_stop_launch("nonexistent0001", "cbid1")

            mock_pause.assert_not_called()
            mock_answer.assert_called_once()
            assert "устарела" in mock_answer.call_args[0][1].lower()

    def test_частичная_ошибка_fb_2_из_3(self, patch_config):
        """2 из 3 успешны -> mark_launch_stopped НЕ вызван, честный остаток
        «Остановил 2 из 3 … нажми ещё раз»."""
        entry = _make_entry(ad_ids=["111", "222", "333"])
        with patch("services.auto_launch.get_launch_stop_entry", return_value=entry), \
             patch("services.auto_launch.mark_launch_stopped") as mock_mark, \
             patch("services.autopilot.approve_pause", side_effect=[(True, "a"), (True, "b"), (False, "err")]), \
             patch("services.notifications.send_telegram") as mock_tg, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_stop_launch
            _execute_stop_launch("abc123def456", "cbid1")

            mock_mark.assert_not_called()
            mock_tg.assert_not_called()
            assert "устарела" in mock_answer.call_args.args[1].lower()

    def test_все_паузы_упали(self, patch_config):
        """approve_pause бросает исключение на всех -> mark_launch_stopped НЕ
        вызван, «Остановил 0 из N»."""
        entry = _make_entry(ad_ids=["111", "222"])
        with patch("services.auto_launch.get_launch_stop_entry", return_value=entry), \
             patch("services.auto_launch.mark_launch_stopped") as mock_mark, \
             patch("services.autopilot.approve_pause", side_effect=RuntimeError("FB упал")), \
             patch("services.notifications.send_telegram") as mock_tg, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_stop_launch
            _execute_stop_launch("abc123def456", "cbid1")

            mock_mark.assert_not_called()
            mock_tg.assert_not_called()
            assert "устарела" in mock_answer.call_args.args[1].lower()

    def test_нет_объявлений_для_остановки(self, patch_config):
        """ad_ids пуст -> ответ «Нет объявлений для остановки», approve_pause
        не вызван."""
        with patch("services.auto_launch.get_launch_stop_entry", return_value=_make_entry(ad_ids=[])), \
             patch("services.autopilot.approve_pause") as mock_pause, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_stop_launch
            _execute_stop_launch("abc123def456", "cbid1")

            mock_pause.assert_not_called()
            assert "устарела" in mock_answer.call_args[0][1].lower()


# ===========================================================================
# Безопасность и валидация id через _handle_callback (whitelist)
# ===========================================================================

class TestHandleCallbackStopLaunchSecurity:
    def test_чужой_chat_id_игнор(self, patch_config):
        """Чужой from_id/chat_id -> _execute_stop_launch НЕ вызван."""
        with patch("services.telegram_bot._execute_stop_launch") as mock_exec, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id="999", chat_id="999")
            _handle_callback(cb)

            mock_exec.assert_not_called()
            mock_answer.assert_not_called()

    def test_невалидный_card_id_игнор(self, patch_config):
        """Malformed legacy callback тоже stale, без вызова executor."""
        with patch("services.telegram_bot._execute_stop_launch") as mock_exec, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(data="stop_launch:bad:id")
            _handle_callback(cb)

            mock_exec.assert_not_called()
            assert "устарела" in mock_answer.call_args.args[1].lower()

    def test_пустой_card_id_игнор(self, patch_config):
        """Пустой legacy callback тоже stale, без вызова executor."""
        with patch("services.telegram_bot._execute_stop_launch") as mock_exec, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(data="stop_launch:")
            _handle_callback(cb)

            mock_exec.assert_not_called()
            assert "устарела" in mock_answer.call_args.args[1].lower()

    def test_валидный_маршрут(self, patch_config):
        """Валидный legacy callback не маршрутизируется в executor."""
        with patch("services.telegram_bot._execute_stop_launch") as mock_exec, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(data="stop_launch:abc123def456", cb_id="cid9")
            _handle_callback(cb)

            mock_exec.assert_not_called()
            assert "устарела" in mock_answer.call_args.args[1].lower()

    def test_max_id_length_regex_fits_64_bytes_callback(self, patch_config):
        """Верхняя граница _STOP_LAUNCH_ID_RE (40 буквоцифр) — callback_data
        укладывается в лимит 64 байта."""
        from services.telegram_bot import _STOP_LAUNCH_ID_RE
        max_id = "a" * 40
        assert _STOP_LAUNCH_ID_RE.match(max_id)
        cb = f"stop_launch:{max_id}".encode()
        assert len(cb) <= 64
