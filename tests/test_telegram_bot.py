"""
Тесты services/telegram_bot.py — двусторонний Telegram-бот.

Все внешние зависимости замокированы:
- requests.post / requests.get
- services.action_producer_gateway.execute_unpause (legacy path не вызывает)
- agent.repositories.decisions_repo.save_decision
- services.autopilot.add_manual_override
- STATE_FILE → временный файл
"""

import sys
from unittest.mock import patch, MagicMock

import pytest

from tests.gateway_test_helpers import confirmed_outcome

# ---------------------------------------------------------------------------
# Патч google.genai — существующая проблема окружения (copywriter_v2.py)
# ---------------------------------------------------------------------------
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
    sys.modules["google.genai.types"] = MagicMock()


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _make_cb(from_id="111", chat_id="111", data="ack", cb_id="cid1"):
    """Создаёт словарь callback_query."""
    return {
        "id": cb_id,
        "from": {"id": int(from_id)},
        "message": {"chat": {"id": int(chat_id)}},
        "data": data,
    }


def _mock_get_ok(updates=None):
    """Возвращает mock requests.get с успешным ответом getUpdates."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True, "result": updates or []}
    return mock_resp


def _mock_post_ok():
    """Возвращает mock requests.post с успешным ответом sendMessage."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True}
    return mock_resp


# ---------------------------------------------------------------------------
# Фикстура: перенаправляем STATE_FILE во временный каталог
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def tmp_state_file(tmp_path):
    """Каждый тест получает чистый временный STATE_FILE."""
    import services.telegram_bot as bot
    original = bot.STATE_FILE
    bot.STATE_FILE = tmp_path / "telegram_state.json"
    yield bot.STATE_FILE
    bot.STATE_FILE = original


# ---------------------------------------------------------------------------
# Фикстура: конфиг с тестовыми значениями токена и chat_id
# ---------------------------------------------------------------------------

OWNER_ID = "111"

@pytest.fixture()
def patch_config():
    """Патч config.TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID."""
    with patch("config.TELEGRAM_BOT_TOKEN", "test-token"), \
         patch("config.TELEGRAM_CHAT_ID", OWNER_ID):
        yield


# ===========================================================================
# Тесты безопасности _handle_callback
# ===========================================================================

class TestHandleCallbackSecurity:
    """Только владелец (TELEGRAM_CHAT_ID) может отдавать команды."""

    def test_foreign_from_id_ignored(self, patch_config):
        """Чужой from.id → ничего не вызывается."""
        with patch("services.telegram_bot._answer_callback") as mock_answer, \
             patch("services.telegram_bot._execute_undo") as mock_undo:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id="999", chat_id=OWNER_ID, data="ack")
            _handle_callback(cb)
            mock_answer.assert_not_called()
            mock_undo.assert_not_called()

    def test_foreign_chat_id_with_owner_from_ignored(self, patch_config):
        """Правильный from.id, но чужой chat.id → всё равно игнор."""
        with patch("services.telegram_bot._answer_callback") as mock_answer, \
             patch("services.telegram_bot._execute_undo") as mock_undo:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id="999", data="ack")
            _handle_callback(cb)
            mock_answer.assert_not_called()
            mock_undo.assert_not_called()

    def test_both_foreign_ignored(self, patch_config):
        """Оба ID чужие → игнор."""
        with patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id="888", chat_id="999", data="ack")
            _handle_callback(cb)
            mock_answer.assert_not_called()


# ===========================================================================
# Тест: "ack" → answerCallbackQuery
# ===========================================================================

class TestAck:
    """Callback 'ack' — подтверждает получение."""

    def test_ack_calls_answer_callback(self, patch_config):
        """ack → _answer_callback('Принято 👍')."""
        with patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="ack", cb_id="cid42")
            _handle_callback(cb)
            mock_answer.assert_called_once_with("cid42", "Принято 👍")

    def test_ack_does_not_call_undo(self, patch_config):
        """ack → _execute_undo НЕ вызывается."""
        with patch("services.telegram_bot._answer_callback"), \
             patch("services.telegram_bot._execute_undo") as mock_undo:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="ack")
            _handle_callback(cb)
            mock_undo.assert_not_called()


# ===========================================================================
# Тест: "undo:<ad_id>" → unpause + save + override
# ===========================================================================

class TestUndo:
    """Legacy undo всегда протух и не вызывает mutation boundary."""

    def test_valid_undo_calls_all_three(self, patch_config):
        """undo отвечает stale и ничего не выполняет."""
        with patch(
            "services.action_producer_gateway.execute_unpause",
            return_value=confirmed_outcome("UNPAUSED"),
        ) as mock_unpause, \
             patch("agent.repositories.decisions_repo.save_decision") as mock_save, \
             patch.dict("sys.modules", {"services.autopilot": MagicMock(add_manual_override=MagicMock())}), \
             patch("services.telegram_bot._answer_callback") as mock_answer, \
             patch("services.notifications.send_telegram") as mock_tg:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="undo:123456789", cb_id="cid7")
            _handle_callback(cb)

            mock_unpause.assert_not_called()
            mock_save.assert_not_called()
            mock_tg.assert_not_called()
            mock_answer.assert_called_once()
            assert "устарела" in mock_answer.call_args.args[1].lower()

    def test_save_decision_correct_fields(self, patch_config):
        """Legacy callback не имеет права писать owner decision."""
        with patch(
            "services.action_producer_gateway.execute_unpause",
            return_value=confirmed_outcome("UNPAUSED"),
        ), \
             patch("agent.repositories.decisions_repo.save_decision") as mock_save, \
             patch.dict("sys.modules", {"services.autopilot": MagicMock(add_manual_override=MagicMock())}), \
             patch("services.telegram_bot._answer_callback"), \
             patch("services.notifications.send_telegram"):
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="undo:987654321")
            _handle_callback(cb)

            mock_save.assert_not_called()

    def test_invalid_ad_id_letters_ignored(self, patch_config):
        """Даже malformed legacy callback получает stale без provider call."""
        with patch("services.action_producer_gateway.execute_unpause") as mock_unpause, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="undo:abc")
            _handle_callback(cb)
            mock_unpause.assert_not_called()
            assert "устарела" in mock_answer.call_args.args[1].lower()

    def test_invalid_ad_id_too_short_ignored(self, patch_config):
        """undo:1234 (4 цифры — меньше минимума) → игнорируется."""
        with patch("services.action_producer_gateway.execute_unpause") as mock_unpause, \
             patch("services.telegram_bot._answer_callback"):
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="undo:1234")
            _handle_callback(cb)
            mock_unpause.assert_not_called()

    def test_invalid_ad_id_mixed_ignored(self, patch_config):
        """undo:123abc456 (смешанный) → игнорируется."""
        with patch("services.action_producer_gateway.execute_unpause") as mock_unpause, \
             patch("services.telegram_bot._answer_callback"):
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="undo:123abc456")
            _handle_callback(cb)
            mock_unpause.assert_not_called()

    def test_add_manual_override_import_error_not_raises(self, patch_config):
        """Legacy callback не импортирует mutation service и не падает."""
        with patch(
            "services.action_producer_gateway.execute_unpause",
            return_value=confirmed_outcome("UNPAUSED"),
        ), \
             patch("agent.repositories.decisions_repo.save_decision"), \
             patch("services.telegram_bot._answer_callback"), \
             patch("services.notifications.send_telegram"):
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="undo:111111111")
            _handle_callback(cb)


# ===========================================================================
# Тест: неизвестный callback_data игнорируется
# ===========================================================================

class TestUnknownCallbackData:
    """Неизвестные команды игнорируются (whitelist)."""

    def test_drop_tables_ignored(self, patch_config):
        """data='drop tables' → ничего не вызывается."""
        with patch("services.telegram_bot._answer_callback") as mock_answer, \
             patch("services.telegram_bot._execute_undo") as mock_undo:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="drop tables")
            _handle_callback(cb)
            mock_answer.assert_not_called()
            mock_undo.assert_not_called()

    def test_empty_data_ignored(self, patch_config):
        """data='' → ничего не вызывается."""
        with patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="")
            _handle_callback(cb)
            mock_answer.assert_not_called()

    def test_random_data_ignored(self, patch_config):
        """data='hello world' → игнор."""
        with patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="hello world")
            _handle_callback(cb)
            mock_answer.assert_not_called()


# ===========================================================================
# Тест: offset сохраняется ДО обработки callback
# ===========================================================================

class TestOffsetOrder:
    """Единственный поллер на токен: своего getUpdates у telegram_bot больше нет.

    Раньше poll_updates сразу после trusted ingress ходил в Telegram со своим
    файловым офсетом (data/telegram_state.json). Переданный offset подтверждает
    и удаляет апдейты для ВСЕХ клиентов бота, поэтому две ветки воровали
    апдейты друг у друга: /digest мог уйти в консоль (которая его не знала) и
    пропасть, а команды пульта — исчезнуть до owner-обработчика.
    """

    def test_poller_does_not_call_get_updates_itself(self, patch_config, tmp_state_file):
        """Ни одного собственного HTTP-запроса: источник апдейтов ровно один."""
        import services.telegram_bot as bot

        with patch("config.TELEGRAM_BOT_TOKEN", "test-token"), \
             patch("config.TELEGRAM_CHAT_ID", OWNER_ID), \
             patch("requests.get") as mock_get, \
             patch("services.approval_telegram.process_owner_decisions") as ingress:
            bot.poll_updates()

        mock_get.assert_not_called()
        ingress.assert_called_once()
        assert ingress.call_args.kwargs["worker_id"] == "telegram-poll"

    def test_file_offset_is_no_longer_touched(self, patch_config, tmp_state_file):
        """Файловый офсет выведен из использования: курсор живёт в БД."""
        import services.telegram_bot as bot

        with patch("config.TELEGRAM_BOT_TOKEN", "test-token"), \
             patch("config.TELEGRAM_CHAT_ID", OWNER_ID), \
             patch.object(bot, "_save_offset") as save, \
             patch("services.approval_telegram.process_owner_decisions"):
            bot.poll_updates()

        save.assert_not_called()
        assert not tmp_state_file.exists()

    def test_ingress_failure_does_not_raise_out_of_poller(
        self, patch_config, tmp_state_file
    ):
        """Сбой ingress не роняет крон: апдейт уже durable, повторим на след. тике."""
        import services.telegram_bot as bot

        with patch("config.TELEGRAM_BOT_TOKEN", "test-token"), \
             patch("config.TELEGRAM_CHAT_ID", OWNER_ID), \
             patch(
                 "services.approval_telegram.process_owner_decisions",
                 side_effect=RuntimeError("инбокс недоступен"),
             ):
            bot.poll_updates()  # не должно бросить исключение наружу


# ===========================================================================
# Тест: send_with_buttons формирует корректный inline_keyboard JSON
# ===========================================================================

class TestSendWithButtons:
    """send_with_buttons формирует правильный payload для Telegram API."""

    def test_inline_keyboard_structure(self, patch_config):
        """Проверяем структуру inline_keyboard в JSON payload."""
        mock_post = MagicMock(return_value=_mock_post_ok())

        buttons = [
            [("✅ Продолжай", "ack")],
            [("↩️ Вернуть CityA", "undo:123456789"), ("↩️ Вернуть CityC", "undo:987654321")],
        ]

        with patch("requests.post", mock_post):
            from services.telegram_bot import send_with_buttons
            result = send_with_buttons("<b>Отчёт</b>", buttons)

        assert result is True
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]

        assert payload["parse_mode"] == "HTML"
        assert payload["text"] == "<b>Отчёт</b>"
        keyboard = payload["reply_markup"]["inline_keyboard"]

        # Первый ряд: одна кнопка
        assert len(keyboard[0]) == 1
        assert keyboard[0][0]["text"] == "✅ Продолжай"
        assert keyboard[0][0]["callback_data"] == "ack"

        # Второй ряд: две кнопки
        assert len(keyboard[1]) == 2
        assert keyboard[1][0]["callback_data"] == "undo:123456789"
        assert keyboard[1][1]["callback_data"] == "undo:987654321"

    def test_no_token_returns_false(self):
        """Без токена → False, запрос не отправляется."""
        with patch("config.TELEGRAM_BOT_TOKEN", ""), \
             patch("config.TELEGRAM_CHAT_ID", "111"), \
             patch("requests.post") as mock_post:
            from services.telegram_bot import send_with_buttons
            result = send_with_buttons("Тест", [[("Кнопка", "ack")]])
            assert result is False
            mock_post.assert_not_called()

    def test_api_error_returns_false(self, patch_config):
        """Telegram вернул ok=false → возвращаем False."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False, "description": "Bad Request"}

        with patch("requests.post", return_value=mock_resp):
            from services.telegram_bot import send_with_buttons
            result = send_with_buttons("Тест", [[("Кнопка", "ack")]])
            assert result is False

    def test_network_error_returns_false(self, patch_config):
        """Сетевая ошибка → возвращаем False, не бросаем исключение."""
        with patch("requests.post", side_effect=ConnectionError("Нет сети")):
            from services.telegram_bot import send_with_buttons
            result = send_with_buttons("Тест", [[("Кнопка", "ack")]])
            assert result is False


# ===========================================================================
# Тест: poll_updates при отсутствии конфига
# ===========================================================================

class TestPollUpdates:
    """poll_updates: гейты и базовое поведение."""

    def test_no_token_returns_early(self, tmp_state_file):
        """Без токена poll_updates просто возвращается."""
        with patch("config.TELEGRAM_BOT_TOKEN", ""), \
             patch("config.TELEGRAM_CHAT_ID", "111"), \
             patch("services.approval_telegram.process_owner_decisions") as ingress:
            from services.telegram_bot import poll_updates
            poll_updates()
            ingress.assert_not_called()

    def test_no_chat_id_returns_early(self, tmp_state_file):
        """Без chat_id poll_updates просто возвращается."""
        with patch("config.TELEGRAM_BOT_TOKEN", "test-token"), \
             patch("config.TELEGRAM_CHAT_ID", ""), \
             patch("services.approval_telegram.process_owner_decisions") as ingress:
            from services.telegram_bot import poll_updates
            poll_updates()
            ingress.assert_not_called()

    def test_poll_delegates_to_single_db_cursor_ingress(
        self, patch_config, tmp_state_file
    ):
        """Апдейты забирает только owner-ingress с курсором в telegram_poll_cursor."""
        import services.telegram_bot as bot

        with patch("requests.get") as mock_get, \
             patch("services.approval_telegram.process_owner_decisions") as ingress:
            bot.poll_updates()

        mock_get.assert_not_called()
        assert ingress.call_args.kwargs["limit"] == 50

    def test_network_error_does_not_raise(self, patch_config, tmp_state_file):
        """Сетевая ошибка внутри ingress → poll_updates не падает."""
        with patch(
            "services.approval_telegram.process_owner_decisions",
            side_effect=ConnectionError("Нет сети"),
        ):
            from services.telegram_bot import poll_updates
            poll_updates()  # Не должно бросить исключение


# ===========================================================================
# НОВЫЕ тесты: apply и applyrun — одобрение паузы через Telegram
# ===========================================================================

class TestApplyCallbacks:
    """Тесты для callback_data 'apply:<ad_id>' и 'applyrun:<key>'."""

    # -----------------------------------------------------------------------
    # apply: безопасность — чужой chat.id игнорируется
    # -----------------------------------------------------------------------

    def test_apply_foreign_chat_ignored(self, patch_config):
        """apply с чужим chat.id → полный игнор (безопасность)."""
        with patch("services.telegram_bot._answer_callback") as mock_answer, \
             patch("services.telegram_bot._execute_apply") as mock_execute:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id="999", chat_id="999", data="apply:11111111")
            _handle_callback(cb)
            mock_answer.assert_not_called()
            mock_execute.assert_not_called()

    # -----------------------------------------------------------------------
    # apply: протухший прогон → "устарел", pause_ad НЕ вызван
    # -----------------------------------------------------------------------

    def test_apply_stale_run_answers_expired(self, patch_config):
        """apply по протухшей записи → _answer_callback «устарел», approve_pause не зовётся."""
        # get_pending_approval для всех ключей возвращает None (устарело)
        mock_ap = MagicMock()
        mock_ap.get_pending_approval.return_value = None
        mock_ap._load_state.return_value = {
            "pending_approvals": {
                "run-test": {"ads": [{"id": "11111111", "name": "Тест"}], "created_at": "2020-01-01"}
            }
        }
        mock_ap.approve_pause = MagicMock()

        with patch.dict("sys.modules", {"services.autopilot": mock_ap}), \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_apply
            _execute_apply("11111111", "cid_test")

        # Ответ о протухании
        mock_answer.assert_called_once()
        answer_text = mock_answer.call_args[0][1]
        assert "устарел" in answer_text.lower()
        # approve_pause не вызывался
        mock_ap.approve_pause.assert_not_called()

    # -----------------------------------------------------------------------
    # applyrun: happy-path — pause_ad на все, подтверждение отправлено
    # -----------------------------------------------------------------------

    def test_applyrun_happy_path_all_paused(self, patch_config):
        """Legacy applyrun не вызывает pause ни для одного объявления."""
        ads = [
            {"id": "11111111", "name": "Объявление 1", "spend": 100, "reason": "CPL"},
            {"id": "22222222", "name": "Объявление 2", "spend": 80, "reason": "CTR"},
        ]
        approval_entry = {
            "ads": ads,
            "created_at": "2099-01-01T00:00:00",
        }

        mock_ap = MagicMock()
        mock_ap.get_pending_approval.return_value = approval_entry
        # approve_pause возвращает (True, ad_name)
        mock_ap.approve_pause.side_effect = [
            (True, "Объявление 1"),
            (True, "Объявление 2"),
        ]
        mock_ap.pop_applied = MagicMock()

        with patch.dict("sys.modules", {"services.autopilot": mock_ap}), \
             patch("services.telegram_bot._answer_callback") as mock_answer, \
             patch("services.notifications.send_telegram") as mock_tg:
            from services.telegram_bot import _execute_applyrun
            _execute_applyrun("run-test", "cid_run")

        mock_ap.approve_pause.assert_not_called()
        mock_ap.pop_applied.assert_not_called()
        mock_tg.assert_not_called()
        assert "устарела" in mock_answer.call_args.args[1].lower()

    # -----------------------------------------------------------------------
    # applyrun: ошибка pause_ad одного из трёх → остальные применены, в сообщении ошибка
    # -----------------------------------------------------------------------

    def test_applyrun_one_error_others_applied(self, patch_config):
        """Legacy applyrun всегда zero-write независимо от pending списка."""
        ads = [
            {"id": "11111111", "name": "Объявление 1", "spend": 100, "reason": "CPL"},
            {"id": "22222222", "name": "Объявление 2", "spend": 80, "reason": "CTR"},
            {"id": "33333333", "name": "Объявление 3", "spend": 60, "reason": "ROMI"},
        ]
        approval_entry = {"ads": ads, "created_at": "2099-01-01T00:00:00"}

        mock_ap = MagicMock()
        mock_ap.get_pending_approval.return_value = approval_entry
        # Второе объявление падает
        mock_ap.approve_pause.side_effect = [
            (True, "Объявление 1"),
            (False, "FB API timeout"),
            (True, "Объявление 3"),
        ]
        mock_ap.pop_applied = MagicMock()

        with patch.dict("sys.modules", {"services.autopilot": mock_ap}), \
             patch("services.telegram_bot._answer_callback"), \
             patch("services.notifications.send_telegram") as mock_tg:
            from services.telegram_bot import _execute_applyrun
            _execute_applyrun("run-multi", "cid_multi")

        mock_ap.approve_pause.assert_not_called()
        mock_ap.pop_applied.assert_not_called()
        mock_tg.assert_not_called()

    # -----------------------------------------------------------------------
    # applyrun: протухший ключ → "устарело"
    # -----------------------------------------------------------------------

    def test_applyrun_stale_key_answers_expired(self, patch_config):
        """applyrun по протухшему ключу → _answer_callback «устарело»."""
        mock_ap = MagicMock()
        mock_ap.get_pending_approval.return_value = None

        with patch.dict("sys.modules", {"services.autopilot": mock_ap}), \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_applyrun
            _execute_applyrun("run-expired", "cid_exp")

        mock_answer.assert_called_once()
        answer_text = mock_answer.call_args[0][1]
        assert "устарел" in answer_text.lower()

    # -----------------------------------------------------------------------
    # whitelist: apply и applyrun с некорректными данными игнорируются
    # -----------------------------------------------------------------------

    def test_apply_invalid_ad_id_ignored(self, patch_config):
        """apply:abc (не цифры) → игнорируется."""
        with patch("services.telegram_bot._execute_apply") as mock_exec, \
             patch("services.telegram_bot._answer_callback"):
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="apply:abc")
            _handle_callback(cb)
            mock_exec.assert_not_called()

    def test_applyrun_invalid_key_ignored(self, patch_config):
        """applyrun:! (некорректный символ) → игнорируется."""
        with patch("services.telegram_bot._execute_applyrun") as mock_exec, \
             patch("services.telegram_bot._answer_callback"):
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="applyrun:!")
            _handle_callback(cb)
            mock_exec.assert_not_called()
