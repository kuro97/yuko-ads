"""
Тесты services/telegram_console.py — команда пульта /brief (ARCH-brief-approval-flow.md).

Мокаем: services.brief_generator.generate_and_push_briefs, services.notifications.send_telegram,
services.telegram_console.threading (для проверки фонового потока).
Конвенции — как tests/test_telegram_console.py (_make_msg, patch_config, OWNER_ID).
"""

import sys
from unittest.mock import patch, MagicMock

import pytest

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
    sys.modules["google.genai.types"] = MagicMock()


OWNER_ID = "111"


def _make_msg(from_id=OWNER_ID, chat_id=OWNER_ID, text="/brief"):
    """Создаёт словарь update['message'] (как в test_telegram_console.py)."""
    return {
        "message_id": 5,
        "from": {"id": int(from_id) if from_id is not None else None, "is_bot": False},
        "chat": {"id": int(chat_id) if chat_id is not None else None, "type": "private"},
        "date": 1751880000,
        "text": text,
    }


@pytest.fixture()
def patch_config():
    """Патч config.TELEGRAM_CHAT_ID — владелец пульта."""
    with patch("config.TELEGRAM_CHAT_ID", OWNER_ID):
        yield


# ===========================================================================
# /brief в _COMMANDS и /help
# ===========================================================================

class TestBriefInHelp:
    def test_brief_in_commands_and_help(self, patch_config):
        """/help от владельца содержит /brief."""
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            assert "brief" in console._COMMANDS
            console.handle_message(_make_msg(text="/help"))

            mock_tg.assert_called_once()
            text = mock_tg.call_args[0][0]
            assert "/brief" in text


# ===========================================================================
# /brief → мгновенный ack + фоновый поток
# ===========================================================================

class TestBriefDispatch:
    def test_brief_dispatch_acks_and_spawns_thread(self, patch_config):
        """/brief → мгновенный send_telegram ack, threading.Thread запущен
        с target=_run_brief_async, daemon=True (не блокирует поллер)."""
        with patch("services.notifications.send_telegram") as mock_tg, \
             patch("services.telegram_console.threading.Thread") as mock_thread:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/brief"))

            mock_tg.assert_called_once()
            ack_text = mock_tg.call_args[0][0]
            assert "генери" in ack_text.lower()

            mock_thread.assert_called_once()
            call_kwargs = mock_thread.call_args[1]
            assert call_kwargs["daemon"] is True
            assert call_kwargs["target"] == console._run_brief_async
            mock_thread.return_value.start.assert_called_once()

    def test_brief_foreign_chat_ignored(self, patch_config):
        """/brief от чужого chat_id -> генерация и ack не вызываются."""
        with patch("services.notifications.send_telegram") as mock_tg, \
             patch("services.telegram_console.threading.Thread") as mock_thread:
            import services.telegram_console as console
            console.handle_message(_make_msg(from_id="999", chat_id="999", text="/brief"))

            mock_tg.assert_not_called()
            mock_thread.assert_not_called()


# ===========================================================================
# _run_brief_async — фоновый воркер
# ===========================================================================

class TestRunBriefAsync:
    def test_run_brief_async_calls_generate_and_sends_summary(self, patch_config):
        """Фоновый воркер вызывает generate_and_push_briefs(max_briefs=3) и шлёт сводку."""
        with patch("services.brief_generator.generate_and_push_briefs",
                   return_value={"queued": 2, "created": 2, "blocked": 0, "error": None}) as mock_run, \
             patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console._run_brief_async()

            mock_run.assert_called_once_with(max_briefs=3)
            mock_tg.assert_called_once()
            assert "2 ТЗ" in mock_tg.call_args[0][0]

    def test_run_brief_async_exception_sends_failure_message_not_silent(self, patch_config):
        """Исключение в фоновом воркере /brief -> «❌» сообщение, поток не молчит."""
        with patch("services.brief_generator.generate_and_push_briefs",
                   side_effect=RuntimeError("boom")), \
             patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console._run_brief_async()  # не должно бросить исключение наружу

            mock_tg.assert_called_once()
            assert "❌" in mock_tg.call_args[0][0]


# ===========================================================================
# _format_brief_summary — чистая функция
# ===========================================================================

class TestFormatBriefSummary:
    def test_format_brief_summary_queued(self):
        """queued>0 -> «отправил N ТЗ тебе на одобрение»."""
        import services.telegram_console as console
        text = console._format_brief_summary({"queued": 2, "blocked": 0, "error": None})

        assert "отправил 2 ТЗ" in text
        assert "одобрение" in text

    def test_format_brief_summary_queued_with_blocked(self):
        """queued>0 и blocked>0 -> обе строки в сводке."""
        import services.telegram_console as console
        text = console._format_brief_summary({"queued": 1, "blocked": 2, "error": None})

        assert "отправил 1 ТЗ" in text
        assert "заблокировано проверками: 2" in text

    def test_format_brief_summary_no_topics(self):
        """queued=0, blocked=0, error='Все темы уже созданы (дубли)' -> «всё уже было»."""
        import services.telegram_console as console
        text = console._format_brief_summary(
            {"queued": 0, "blocked": 0, "error": "Все темы уже созданы (дубли)"},
        )

        assert "всё уже было" in text or "Все темы уже созданы" in text

    def test_format_brief_summary_all_blocked(self):
        """queued=0, blocked=3 -> «заблокированы проверками»."""
        import services.telegram_console as console
        text = console._format_brief_summary({"queued": 0, "blocked": 3, "error": None})

        assert "заблокированы проверками" in text
        assert "3" in text

    def test_format_brief_summary_no_topics_no_error(self):
        """queued=0, blocked=0, error=None -> дефолтное «новых тем нет — всё уже было»."""
        import services.telegram_console as console
        text = console._format_brief_summary({"queued": 0, "blocked": 0, "error": None})

        assert "новых тем нет" in text
