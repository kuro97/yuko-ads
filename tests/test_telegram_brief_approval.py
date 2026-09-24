"""
Тесты services/telegram_bot.py — callback-обработчики approve_brief:<id>/
reject_brief:<id> (ARCH-brief-approval-flow.md).

Мокаем внешние границы:
- services.pending_briefs (get_brief/mark_approved/mark_rejected)
- integrations.trello (get_or_create_list/create_card/BRIEF_LIST_NAME)
- services.notifications.send_telegram
- services.telegram_bot._answer_callback

Конвенции — как tests/test_telegram_bot.py (_make_cb, patch_config, OWNER_ID).
"""

import sys
from unittest.mock import patch, MagicMock

import pytest

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
    sys.modules["google.genai.types"] = MagicMock()


OWNER_ID = "111"


def _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="approve_brief:abc123", cb_id="cid1"):
    """Создаёт словарь callback_query (как в test_telegram_bot.py)."""
    return {
        "id": cb_id,
        "from": {"id": int(from_id)},
        "message": {"chat": {"id": int(chat_id)}},
        "data": data,
    }


def _make_brief(brief_id="abc123", status="pending", **overrides):
    """Создаёт тестовую запись очереди pending_briefs."""
    brief = {
        "id": brief_id,
        "name": "CityA / Страх упустить результат",
        "desc": "Сценарий целиком\n\n---\n*служебный блок*",
        "product": "PRODA",
        "signature": "Страх упустить результат::CityA::video_speaker",
        "status": status,
        "created_at": "2026-07-08T08:15:00+05:00",
        "decided_at": None,
        "card_id": None,
        "card_url": None,
    }
    brief.update(overrides)
    return brief


@pytest.fixture()
def patch_config():
    """Патч config.TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID."""
    with patch("config.TELEGRAM_BOT_TOKEN", "test-token"), \
         patch("config.TELEGRAM_CHAT_ID", OWNER_ID):
        yield


@pytest.fixture(autouse=True)
def tmp_state_file(tmp_path):
    """Каждый тест получает чистый временный STATE_FILE (не используется здесь
    напрямую, но изолирует telegram_bot.STATE_FILE от параллельных тестов)."""
    import services.telegram_bot as bot
    original = bot.STATE_FILE
    bot.STATE_FILE = tmp_path / "telegram_state.json"
    yield
    bot.STATE_FILE = original


# ===========================================================================
# _execute_approve_brief
# ===========================================================================

class TestApproveBrief:
    """Одобрение ТЗ владельцем создаёт карточку в «ТЗ реклам»."""

    def test_approve_creates_card_and_marks_approved(self, patch_config):
        """Happy path: get_or_create_list(BRIEF_LIST_NAME) + create_card(...product...),
        mark_approved вызван, send_telegram со ссылкой."""
        with patch("services.pending_briefs.get_brief", return_value=_make_brief()), \
             patch("services.pending_briefs.mark_approved") as mock_mark, \
             patch("integrations.trello.get_or_create_list", return_value="list_id_1") as mock_get_list, \
             patch("integrations.trello.create_card",
                   return_value={"id": "card1", "url": "https://trello.com/c/card1"}) as mock_create, \
             patch("services.telegram_bot._answer_callback") as mock_answer, \
             patch("services.notifications.send_telegram") as mock_tg:
            from services.telegram_bot import _execute_approve_brief
            _execute_approve_brief("abc123", "cbid1")

            mock_get_list.assert_called_once_with("ТЗ реклам")
            mock_create.assert_called_once()
            _, kwargs = mock_create.call_args
            assert kwargs.get("product") == "PRODA"
            mock_mark.assert_called_once_with(
                "abc123", card_id="card1", card_url="https://trello.com/c/card1",
            )
            mock_answer.assert_called_once()
            mock_tg.assert_called_once()
            assert "ТЗ реклам" in mock_tg.call_args[0][0]
            assert "https://trello.com/c/card1" in mock_tg.call_args[0][0]

    def test_approve_uses_brief_list_name_constant(self, patch_config):
        """get_or_create_list вызван с BRIEF_LIST_NAME (== 'ТЗ реклам')."""
        from integrations.trello import BRIEF_LIST_NAME
        assert BRIEF_LIST_NAME == "ТЗ реклам"

        with patch("services.pending_briefs.get_brief", return_value=_make_brief()), \
             patch("services.pending_briefs.mark_approved"), \
             patch("integrations.trello.get_or_create_list", return_value="list_id_1") as mock_get_list, \
             patch("integrations.trello.create_card", return_value={"id": "card1", "url": "u"}), \
             patch("services.telegram_bot._answer_callback"), \
             patch("services.notifications.send_telegram"):
            from services.telegram_bot import _execute_approve_brief
            _execute_approve_brief("abc123", "cbid1")

            mock_get_list.assert_called_once_with(BRIEF_LIST_NAME)

    def test_approve_idempotent_on_already_approved(self, patch_config):
        """Повторный approve по уже approved записи -> create_card НЕ вызван,
        ответ «уже одобрено» (карточка не дублируется, AC4)."""
        with patch("services.pending_briefs.get_brief", return_value=_make_brief(status="approved")), \
             patch("services.pending_briefs.mark_approved") as mock_mark, \
             patch("integrations.trello.create_card") as mock_create, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_approve_brief
            _execute_approve_brief("abc123", "cbid1")

            assert not mock_create.called
            assert not mock_mark.called
            mock_answer.assert_called_once()
            assert "уже одобрено" in mock_answer.call_args[0][1].lower()

    def test_approve_trello_error_keeps_pending(self, patch_config):
        """create_card бросает -> mark_approved НЕ вызван, ответ «нажми ещё раз»."""
        with patch("services.pending_briefs.get_brief", return_value=_make_brief()), \
             patch("services.pending_briefs.mark_approved") as mock_mark, \
             patch("integrations.trello.get_or_create_list", return_value="list_id_1"), \
             patch("integrations.trello.create_card", side_effect=RuntimeError("Trello недоступен")), \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_approve_brief
            _execute_approve_brief("abc123", "cbid1")

            assert not mock_mark.called
            mock_answer.assert_called_once()
            assert "нажми ещё раз" in mock_answer.call_args[0][1].lower()

    def test_approve_missing_id(self, patch_config):
        """approve по неизвестному id -> ответ «не найдено», create_card не вызван."""
        with patch("services.pending_briefs.get_brief", return_value=None), \
             patch("integrations.trello.create_card") as mock_create, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_approve_brief
            _execute_approve_brief("ffffff", "cbid1")

            assert not mock_create.called
            mock_answer.assert_called_once()
            assert "не найдено" in mock_answer.call_args[0][1].lower()

    def test_approve_rejected_does_not_create_card(self, patch_config):
        """approve по уже rejected записи -> create_card не вызван, честный ответ."""
        with patch("services.pending_briefs.get_brief", return_value=_make_brief(status="rejected")), \
             patch("integrations.trello.create_card") as mock_create, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_approve_brief
            _execute_approve_brief("abc123", "cbid1")

            assert not mock_create.called
            mock_answer.assert_called_once()
            assert "отклонено" in mock_answer.call_args[0][1].lower()


# ===========================================================================
# _execute_reject_brief
# ===========================================================================

class TestRejectBrief:
    """Отклонение ТЗ владельцем помечает запись rejected."""

    def test_reject_marks_rejected(self, patch_config):
        """reject по pending -> mark_rejected вызван, ответ подтверждения,
        create_card не вызван."""
        with patch("services.pending_briefs.get_brief", return_value=_make_brief()), \
             patch("services.pending_briefs.mark_rejected") as mock_mark, \
             patch("integrations.trello.create_card") as mock_create, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_reject_brief
            _execute_reject_brief("abc123", "cbid1")

            mock_mark.assert_called_once_with("abc123")
            assert not mock_create.called
            mock_answer.assert_called_once()

    def test_reject_already_approved(self, patch_config):
        """reject по approved -> mark_rejected НЕ вызван, ответ «уже одобрено»."""
        with patch("services.pending_briefs.get_brief", return_value=_make_brief(status="approved")), \
             patch("services.pending_briefs.mark_rejected") as mock_mark, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_reject_brief
            _execute_reject_brief("abc123", "cbid1")

            assert not mock_mark.called
            mock_answer.assert_called_once()
            assert "одобрено" in mock_answer.call_args[0][1].lower()

    def test_reject_missing_id(self, patch_config):
        """reject по неизвестному id -> «не найдено», mark_rejected не вызван."""
        with patch("services.pending_briefs.get_brief", return_value=None), \
             patch("services.pending_briefs.mark_rejected") as mock_mark, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_reject_brief
            _execute_reject_brief("ffffff", "cbid1")

            assert not mock_mark.called
            mock_answer.assert_called_once()
            assert "не найдено" in mock_answer.call_args[0][1].lower()


# ===========================================================================
# Безопасность и валидация id через _handle_callback (whitelist)
# ===========================================================================

class TestHandleCallbackBriefSecurity:
    """Чужой chat_id/from_id и некорректный <id> игнорируются молча."""

    def test_approve_brief_foreign_chat_ignored(self, patch_config):
        """Чужой from_id/chat_id -> _execute_approve_brief НЕ вызван."""
        with patch("services.telegram_bot._execute_approve_brief") as mock_exec, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id="999", chat_id="999", data="approve_brief:abc123")
            _handle_callback(cb)

            mock_exec.assert_not_called()
            mock_answer.assert_not_called()

    def test_reject_brief_foreign_chat_ignored(self, patch_config):
        """Чужой from_id/chat_id -> _execute_reject_brief НЕ вызван."""
        with patch("services.telegram_bot._execute_reject_brief") as mock_exec, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id="999", chat_id="999", data="reject_brief:abc123")
            _handle_callback(cb)

            mock_exec.assert_not_called()
            mock_answer.assert_not_called()

    def test_approve_brief_invalid_id_ignored(self, patch_config):
        """approve_brief:;DROP TABLE -> некорректный id, игнор без ответа."""
        with patch("services.telegram_bot._execute_approve_brief") as mock_exec, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(data="approve_brief:;DROP TABLE")
            _handle_callback(cb)

            mock_exec.assert_not_called()
            mock_answer.assert_not_called()

    def test_reject_brief_invalid_id_ignored(self, patch_config):
        """reject_brief: (пустой id) -> некорректный id, игнор без ответа."""
        with patch("services.telegram_bot._execute_reject_brief") as mock_exec, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(data="reject_brief:")
            _handle_callback(cb)

            mock_exec.assert_not_called()
            mock_answer.assert_not_called()

    def test_approve_brief_valid_id_routes_to_execute(self, patch_config):
        """approve_brief:<валидный id> -> роутится в _execute_approve_brief."""
        with patch("services.telegram_bot._execute_approve_brief") as mock_exec:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(data="approve_brief:a1b2c3", cb_id="cid9")
            _handle_callback(cb)

            mock_exec.assert_called_once_with("a1b2c3", "cid9")

    def test_max_id_length_regex_fits_64_bytes_callback(self, patch_config):
        """Верхняя граница _BRIEF_ID_RE (40 буквоцифр) — даже с самым длинным
        префиксом (reject_brief:) callback_data укладывается в лимит 64 байта."""
        from services.telegram_bot import _BRIEF_ID_RE
        max_id = "a" * 40
        assert _BRIEF_ID_RE.match(max_id)
        cb = f"reject_brief:{max_id}".encode()
        assert len(cb) <= 64


# ===========================================================================
# Битый JSON в pending_briefs.json — колбэки не должны падать (используем
# РЕАЛЬНЫЙ pending_briefs на tmp-файле, а не мок get_brief, чтобы проверить
# именно деградацию через _load(), как она реально работает в проде).
# ===========================================================================

class TestCorruptPendingFile:
    """Битый JSON в очереди не роняет approve/reject-колбэки (AC: pending_briefs
    деградирует на пустую очередь, а не бросает исключение)."""

    def test_approve_execute_survives_corrupt_pending_file(self, patch_config, tmp_path, monkeypatch):
        """Битый JSON -> get_brief видит пустую очередь -> «не найдено»,
        исключение наружу не летит."""
        import services.pending_briefs as pb
        pending_file = tmp_path / "pending_briefs.json"
        pending_file.write_text("{не json", encoding="utf-8")
        monkeypatch.setattr(pb, "PENDING_FILE", pending_file)

        with patch("integrations.trello.create_card") as mock_create, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_approve_brief
            _execute_approve_brief("abc123", "cbid1")  # не должно бросить исключение

            assert not mock_create.called
            mock_answer.assert_called_once()
            assert "не найдено" in mock_answer.call_args[0][1].lower()

    def test_reject_execute_survives_corrupt_pending_file(self, patch_config, tmp_path, monkeypatch):
        """Битый JSON -> get_brief видит пустую очередь -> «не найдено»,
        mark_rejected не падает и не вызывается по несуществующей записи."""
        import services.pending_briefs as pb
        pending_file = tmp_path / "pending_briefs.json"
        pending_file.write_text("{не json", encoding="utf-8")
        monkeypatch.setattr(pb, "PENDING_FILE", pending_file)

        with patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_reject_brief
            _execute_reject_brief("abc123", "cbid1")  # не должно бросить исключение

            mock_answer.assert_called_once()
            assert "не найдено" in mock_answer.call_args[0][1].lower()
            # Файл остался нетронутым (get_brief его не мутирует) — записи нет
            assert pb._load() == {"briefs": []}


# ===========================================================================
# Ошибка Trello на РЕАЛЬНОЙ (не замоканной) очереди — проверяем, что запись
# физически остаётся status=pending в файле, а не только что mark_approved
# не был вызван (защита от расхождения мока и реального поведения).
# ===========================================================================

class TestApproveTrelloErrorRealQueue:
    def test_approve_trello_error_keeps_pending_in_real_queue(self, patch_config, tmp_path, monkeypatch):
        """Ошибка Trello при approve -> в РЕАЛЬНОМ файле очереди запись
        остаётся status=pending, card_id/card_url не проставлены."""
        import services.pending_briefs as pb
        monkeypatch.setattr(pb, "PENDING_FILE", tmp_path / "pending_briefs.json")
        record = pb.add_pending("Тема", "desc", "PRODA", "sig-1")

        with patch("integrations.trello.get_or_create_list", return_value="list_id_1"), \
             patch("integrations.trello.create_card", side_effect=RuntimeError("Trello недоступен")), \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_approve_brief
            _execute_approve_brief(record["id"], "cbid1")

        updated = pb.get_brief(record["id"])
        assert updated["status"] == pb.STATUS_PENDING
        assert updated["card_id"] is None
        assert updated["card_url"] is None
        mock_answer.assert_called_once()
        assert "нажми ещё раз" in mock_answer.call_args[0][1].lower()
