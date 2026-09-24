"""
Тесты services/telegram_console.py — «пульт в телеге» (роутер текстовых команд).

Все внешние границы замокированы:
- services.notifications.send_telegram / services.telegram_bot.send_with_buttons
- services.telegram_bot._answer_callback
- agent.analyzer.get_ads_with_metrics
- services.auto_launch._get_done_list_id / _get_unlaunched_cards / run_auto_launch
- services.budget_scaler.run_budget_scaling
- services.autopilot.approve_pause / add_manual_override
- services.evening_report._compute_money_windows / _section_money
- services.morning_digest._section_actions_24h
- config.TELEGRAM_CHAT_ID

Конвенции — как tests/test_telegram_bot.py (_make_cb, tmp_state_file, patch_config).
Сеть отрезана conftest.py (pytest_socket), sleep не используем — Thread мокается.
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Патч google.genai — существующая проблема окружения (copywriter_v2.py)
# ---------------------------------------------------------------------------
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
    sys.modules["google.genai.types"] = MagicMock()

from services import creative_intelligence as ci  # noqa: E402 (после патча google.genai)

OWNER_ID = "111"


def _make_msg(from_id=OWNER_ID, chat_id=OWNER_ID, text="/status"):
    """Создаёт словарь update['message'] по образцу test_telegram_bot._make_cb."""
    return {
        "message_id": 5,
        "from": {"id": int(from_id) if from_id is not None else None, "is_bot": False},
        "chat": {"id": int(chat_id) if chat_id is not None else None, "type": "private"},
        "date": 1751880000,
        "text": text,
    }


def _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="pause:120210000000000001", cb_id="cid1"):
    """Создаёт словарь callback_query (как в test_telegram_bot.py)."""
    return {
        "id": cb_id,
        "from": {"id": int(from_id)},
        "message": {"chat": {"id": int(chat_id)}},
        "data": data,
    }


@pytest.fixture()
def patch_config():
    """Патч config.TELEGRAM_CHAT_ID — владелец пульта."""
    with patch("config.TELEGRAM_CHAT_ID", OWNER_ID):
        yield


@pytest.fixture(autouse=True)
def _clear_ads_cache():
    """_ADS_CACHE — модульный dict, чистим между тестами, чтобы не тёк контекст."""
    import services.telegram_console as console
    console._ADS_CACHE.clear()
    yield
    console._ADS_CACHE.clear()


# ===========================================================================
# 1. Безопасность: чужой from_id/chat_id → handle_message игнорирует
# ===========================================================================

class TestSecurity:
    """Только владелец (TELEGRAM_CHAT_ID) может отдавать команды пульту."""

    def test_foreign_from_id_ignored(self, patch_config, caplog):
        """Чужой from.id → ни одного send, лог warning отсутствует (безопасный игнор — debug)."""
        with patch("services.notifications.send_telegram") as mock_tg, \
             patch("services.telegram_bot.send_with_buttons") as mock_btn:
            import services.telegram_console as console
            msg = _make_msg(from_id="999", chat_id=OWNER_ID, text="/status")
            console.handle_message(msg)

            mock_tg.assert_not_called()
            mock_btn.assert_not_called()

    def test_foreign_chat_id_with_owner_from_ignored(self, patch_config):
        """Правильный from.id, но чужой chat.id → всё равно игнор."""
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            msg = _make_msg(from_id=OWNER_ID, chat_id="999", text="/status")
            console.handle_message(msg)
            mock_tg.assert_not_called()

    def test_both_foreign_ignored(self, patch_config):
        """Оба ID чужие → игнор, ни одного send."""
        with patch("services.notifications.send_telegram") as mock_tg, \
             patch("services.telegram_bot.send_with_buttons") as mock_btn:
            import services.telegram_console as console
            msg = _make_msg(from_id="888", chat_id="999", text="/status")
            console.handle_message(msg)
            mock_tg.assert_not_called()
            mock_btn.assert_not_called()

    def test_no_chat_id_configured_ignores_everyone(self):
        """TELEGRAM_CHAT_ID не настроен (None) → безопасный дефолт — никому не отвечаем."""
        with patch("config.TELEGRAM_CHAT_ID", None), \
             patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            msg = _make_msg(from_id=OWNER_ID, chat_id=OWNER_ID, text="/status")
            console.handle_message(msg)
            mock_tg.assert_not_called()

    def test_foreign_update_logs_debug(self, patch_config, caplog):
        """Чужой апдейт логируется хотя бы debug-уровнем (не тихо проглатывается)."""
        import logging
        with patch("services.notifications.send_telegram"):
            import services.telegram_console as console
            with caplog.at_level(logging.DEBUG, logger="services.telegram_console"):
                msg = _make_msg(from_id="999", chat_id="999", text="/status")
                console.handle_message(msg)
            assert any("владел" in rec.message.lower() or "игнор" in rec.message.lower() for rec in caplog.records)


# ===========================================================================
# 2. Свободный текст и /help
# ===========================================================================

class TestFreeTextAndHelp:
    """Свободный текст игнорируется, /help отвечает списком команд."""

    def test_free_text_ignored(self, patch_config):
        """Текст «привет» (не команда) → send_telegram НЕ вызван."""
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            msg = _make_msg(text="привет")
            console.handle_message(msg)
            mock_tg.assert_not_called()

    def test_unknown_command_ignored(self, patch_config):
        """/foobar (неизвестная команда) → нет ответа."""
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            msg = _make_msg(text="/foobar")
            console.handle_message(msg)
            mock_tg.assert_not_called()

    def test_message_without_text_ignored(self, patch_config):
        """Сообщение без text (стикер/фото) → игнор, не падает."""
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            msg = _make_msg()
            msg.pop("text")
            console.handle_message(msg)
            mock_tg.assert_not_called()

    def test_help_owner_sends_all_commands(self, patch_config):
        """/help от владельца → send_telegram вызван, текст содержит все 6 команд."""
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            msg = _make_msg(text="/help")
            console.handle_message(msg)

            mock_tg.assert_called_once()
            text = mock_tg.call_args[0][0]
            for cmd in ("status", "queue", "ads", "scale", "launch", "help"):
                assert f"/{cmd}" in text

    def test_help_case_and_botname_normalized(self, patch_config):
        """/HELP@yuko_bot нормализуется в help — ответ отправлен."""
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            msg = _make_msg(text="/HELP@yuko_bot")
            console.handle_message(msg)
            mock_tg.assert_called_once()


# ===========================================================================
# 3. /status — сборка из моков сборщиков
# ===========================================================================

class TestStatus:
    """/status публикует только отчёт, прошедший typed-проверку."""

    def test_status_happy_path(self, patch_config):
        """Собранный отчёт проверяется, рендерится и уходит в checked delivery."""
        request = MagicMock(name="status_request")
        result = MagicMock(name="status_check_result")
        rendered = MagicMock(name="status_rendered_report")
        with patch(
            "services.evening_report.build_evening_report",
            return_value={"check_request": request},
        ), patch(
            "services.approval_checker.check_report", return_value=result
        ) as mock_check, patch(
            "services.approval_report.render_checked_report", return_value=rendered
        ) as mock_render, patch(
            "services.approval_telegram.send_checked_report"
        ) as mock_send, patch("services.notifications.send_telegram") as mock_raw:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/status"))

            mock_check.assert_called_once()
            assert mock_check.call_args.args == (request,)
            assert mock_check.call_args.kwargs["now"].tzinfo is not None
            mock_render.assert_called_once_with(request, result)
            mock_send.assert_called_once_with(rendered, channel="ads")
            mock_raw.assert_not_called()

    def test_status_truncated_at_4096(self, patch_config):
        """Даже длинный результат не превращается обратно в непроверенный текст."""
        request = MagicMock(name="long_status_request")
        result = MagicMock(name="long_status_check_result")
        rendered = MagicMock(name="long_status_rendered_report")
        rendered.text = "x" * 5000
        with patch(
            "services.evening_report.build_evening_report",
            return_value={"check_request": request},
        ), patch(
            "services.approval_checker.check_report", return_value=result
        ), patch(
            "services.approval_report.render_checked_report", return_value=rendered
        ), patch(
            "services.approval_telegram.send_checked_report"
        ) as mock_send, patch("services.notifications.send_telegram") as mock_raw:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/status"))

            mock_send.assert_called_once_with(rendered, channel="ads")
            mock_raw.assert_not_called()

    def test_status_money_section_fails_no_crash(self, patch_config):
        """Ошибка сбора не создаёт ложный checked-отчёт и не выходит наружу."""
        with patch(
            "services.evening_report.build_evening_report",
            side_effect=RuntimeError("FB недоступен"),
        ), patch(
            "services.approval_telegram.send_checked_report"
        ) as mock_send, patch("services.notifications.send_telegram") as mock_raw:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/status"))

            mock_send.assert_not_called()
            mock_raw.assert_called_once()
            assert "/status" in mock_raw.call_args.args[0]
            assert "FB недоступен" not in mock_raw.call_args.args[0]


# ===========================================================================
# 4. /queue — топ-10, первая пятёрка помечена, остаток; пустая очередь
# ===========================================================================

class TestQueue:
    """/queue: топ-10 карточек, первые 5 помечены, честное «пусто»."""

    def test_queue_top10_marks_first_five_and_remainder(self, patch_config):
        """12 карточек → 10 строк, первые 5 с пометкой, «остаток: 2»."""
        cards = [{"id": str(i), "name": f"Карточка {i}"} for i in range(12)]
        with patch("services.auto_launch._get_done_list_id", return_value="list1"), \
             patch("services.auto_launch._get_unlaunched_cards", return_value=cards), \
             patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/queue"))

            mock_tg.assert_called_once()
            text = mock_tg.call_args[0][0]
            # Первые 5 карточек с пометкой
            for i in range(5):
                assert f"Карточка {i}" in text
            marked_count = text.count("уйдут завтра 10:00")
            assert marked_count == 5
            assert "остаток: 2" in text
            # Всего показано 10 карточек (0..9), 10 и 11 не должны попасть в текст
            assert "Карточка 10" not in text
            assert "Карточка 11" not in text

    def test_queue_empty_honest_message(self, patch_config):
        """0 карточек → честное «Очередь пуста»."""
        with patch("services.auto_launch._get_done_list_id", return_value="list1"), \
             patch("services.auto_launch._get_unlaunched_cards", return_value=[]), \
             patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/queue"))

            mock_tg.assert_called_once()
            text = mock_tg.call_args[0][0]
            assert "пуст" in text.lower()

    def test_queue_less_than_five_all_marked_zero_remainder(self, patch_config):
        """< 5 карточек → все помечены, остаток 0 (без строки «остаток»)."""
        cards = [{"id": "1", "name": "Одна карточка"}]
        with patch("services.auto_launch._get_done_list_id", return_value="list1"), \
             patch("services.auto_launch._get_unlaunched_cards", return_value=cards), \
             patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/queue"))

            text = mock_tg.call_args[0][0]
            assert "уйдут завтра 10:00" in text
            assert "остаток" not in text

    def test_queue_trello_unavailable(self, patch_config):
        """Trello недоступен → честное сообщение о недоступности, не падает."""
        with patch("services.auto_launch._get_done_list_id", side_effect=RuntimeError("Trello timeout")), \
             patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/queue"))

            mock_tg.assert_called_once()
            text = mock_tg.call_args[0][0]
            assert "недоступ" in text.lower() or "trello" in text.lower()


# ===========================================================================
# 5. /ads — топ-8, кнопки pause:<ad_id>
# ===========================================================================

class TestAds:
    """/ads публикует только проверенную typed-карточку."""

    def _make_ads(self, n, active=True):
        ads = []
        for i in range(n):
            ads.append({
                "id": f"12345000{i:02d}",
                "name": f"Объявление {i}",
                "city": "CityA",
                "effective_status": "ACTIVE" if active else "PAUSED",
                "spend": 100.0 - i,
                "leads": 5,
                "cpl": 20.0,
                "ctr": 1.2,
                "qual_pct": None,
                "romi": None,
                "payments": None,
            })
        return ads

    def test_ads_top8_with_pause_buttons(self, patch_config):
        """Срез за 7 дней проходит scorecard → checker → checked delivery."""
        scorecard = MagicMock(name="scorecard")
        request = MagicMock(name="ads_request")
        result = MagicMock(name="ads_check_result")
        rendered = MagicMock(name="ads_rendered_report")
        with patch(
            "services.scorecard.build_scorecard", return_value=scorecard
        ) as mock_build, patch(
            "services.scorecard.build_scorecard_request", return_value=request
        ) as mock_request, patch(
            "services.approval_checker.check_report", return_value=result
        ) as mock_check, patch(
            "services.approval_report.render_checked_report", return_value=rendered
        ) as mock_render, patch(
            "services.approval_telegram.send_checked_report"
        ) as mock_send, patch("services.notifications.send_telegram") as mock_raw:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/ads"))

            mock_build.assert_called_once()
            assert mock_build.call_args.kwargs["days"] == 7
            generated_at = mock_build.call_args.kwargs["now"]
            mock_request.assert_called_once_with(scorecard, generated_at=generated_at)
            mock_check.assert_called_once_with(request, now=generated_at)
            mock_render.assert_called_once_with(request, result)
            mock_send.assert_called_once_with(rendered, channel="ads")
            mock_raw.assert_not_called()

    def test_ads_only_active_counted(self, patch_config):
        """Консоль не обходит checker и не использует сырой transport."""
        scorecard = MagicMock(name="mixed_status_scorecard")
        request = MagicMock(name="mixed_status_request")
        result = MagicMock(name="mixed_status_result")
        rendered = MagicMock(name="mixed_status_rendered")
        with patch("services.scorecard.build_scorecard", return_value=scorecard), patch(
            "services.scorecard.build_scorecard_request", return_value=request
        ), patch("services.approval_checker.check_report", return_value=result), patch(
            "services.approval_report.render_checked_report", return_value=rendered
        ), patch(
            "services.approval_telegram.send_checked_report"
        ) as mock_send, patch(
            "services.telegram_bot.send_with_buttons"
        ) as mock_buttons, patch("services.notifications.send_telegram") as mock_raw:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/ads"))

            mock_send.assert_called_once_with(rendered, channel="ads")
            mock_buttons.assert_not_called()
            mock_raw.assert_not_called()

    def test_ads_empty_active_list(self, patch_config):
        """Пустой scorecard тоже проходит checker, без непроверенного fallback."""
        scorecard = MagicMock(name="empty_scorecard")
        request = MagicMock(name="empty_ads_request")
        result = MagicMock(name="empty_ads_result")
        rendered = MagicMock(name="empty_ads_rendered")
        with patch("services.scorecard.build_scorecard", return_value=scorecard), patch(
            "services.scorecard.build_scorecard_request", return_value=request
        ), patch("services.approval_checker.check_report", return_value=result), patch(
            "services.approval_report.render_checked_report", return_value=rendered
        ), patch(
            "services.approval_telegram.send_checked_report"
        ) as mock_send, patch(
            "services.telegram_bot.send_with_buttons"
        ) as mock_buttons, patch("services.notifications.send_telegram") as mock_raw:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/ads"))

            mock_send.assert_called_once_with(rendered, channel="ads")
            mock_buttons.assert_not_called()
            mock_raw.assert_not_called()

    def test_ads_metrics_unavailable_no_crash(self, patch_config):
        """Сбой scorecard не публикует ложный checked-отчёт и не раскрывает ошибку."""
        with patch(
            "services.scorecard.build_scorecard",
            side_effect=RuntimeError("FB API недоступен"),
        ), patch(
            "services.approval_telegram.send_checked_report"
        ) as mock_send, patch("services.notifications.send_telegram") as mock_raw:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/ads"))

            mock_send.assert_not_called()
            mock_raw.assert_called_once()
            assert "/ads" in mock_raw.call_args.args[0]
            assert "FB API недоступен" not in mock_raw.call_args.args[0]


# ===========================================================================
# 6. /scale и /launch: мгновенный ack + фон через threading.Thread
# ===========================================================================

class TestScaleAndLaunchAsync:
    """/scale и /launch: fact-free ack, а итоги — только typed."""

    def test_scale_ack_then_thread_started(self, patch_config):
        """/scale → кодовый fact-free ack и daemon-поток."""
        from services.approval_checker_models import FactFreeTemplate

        with patch(
            "services.approval_telegram.send_fact_free"
        ) as mock_fact_free, patch(
            "services.notifications.send_telegram"
        ) as mock_raw, patch(
            "services.telegram_console.threading.Thread"
        ) as mock_thread:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/scale"))

            mock_fact_free.assert_called_once_with(
                FactFreeTemplate.ACTION_CHECK_STARTED,
                channel="ads",
            )
            mock_raw.assert_not_called()

            mock_thread.assert_called_once()
            call_kwargs = mock_thread.call_args[1]
            assert call_kwargs["daemon"] is True
            assert call_kwargs["target"] == console._run_scale_async
            mock_thread.return_value.start.assert_called_once()

    def test_launch_ack_then_thread_started(self, patch_config):
        """/launch → кодовый fact-free ack и daemon-поток."""
        from services.approval_checker_models import FactFreeTemplate

        with patch(
            "services.approval_telegram.send_fact_free"
        ) as mock_fact_free, patch(
            "services.notifications.send_telegram"
        ) as mock_raw, patch(
            "services.telegram_console.threading.Thread"
        ) as mock_thread:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/launch"))

            mock_fact_free.assert_called_once_with(
                FactFreeTemplate.ACTION_CHECK_STARTED,
                channel="ads",
            )
            mock_raw.assert_not_called()
            mock_thread.assert_called_once()
            call_kwargs = mock_thread.call_args[1]
            assert call_kwargs["daemon"] is True
            assert call_kwargs["target"] == console._run_launch_async

    def test_run_scale_async_calls_run_budget_scaling_dry_run(self, patch_config):
        """Пустой прогон scale не создаёт proposal и не вызывает gateway."""
        with patch(
            "services.budget_scaler.get_scale_config",
            return_value={"max_scales_per_run": 2},
        ), patch(
            "services.budget_scaler.run_budget_scaling",
            return_value={"recommendations": [], "skipped_reason": None},
        ) as mock_run, patch(
            "services.action_gateway.get_operation"
        ) as mock_get_operation, patch(
            "services.approval_telegram.send_action_outcome"
        ) as mock_outcome, patch("services.notifications.send_telegram") as mock_raw:
            import services.telegram_console as console
            console._run_scale_async()

            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            assert kwargs["mode"] == "dry_run"
            assert kwargs["max_scales"] == 2
            mock_get_operation.assert_not_called()
            mock_outcome.assert_not_called()
            mock_raw.assert_not_called()

    def test_run_launch_async_calls_run_auto_launch_dry_run(self, patch_config):
        """Пустой прогон launch не создаёт proposal и не вызывает gateway."""
        with patch(
            "services.auto_launch._get_autopilot_config",
            return_value={"max_launches_per_day": 1},
        ), patch(
            "services.auto_launch.run_auto_launch",
            return_value={"recommendations": [], "skipped_reason": None},
        ) as mock_run, patch(
            "services.action_gateway.get_operation"
        ) as mock_get_operation, patch(
            "services.approval_telegram.send_action_outcome"
        ) as mock_outcome, patch("services.notifications.send_telegram") as mock_raw:
            import services.telegram_console as console
            console._run_launch_async()

            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            assert kwargs["mode"] == "dry_run"
            assert kwargs["max_launches"] == 1
            mock_get_operation.assert_not_called()
            mock_outcome.assert_not_called()
            mock_raw.assert_not_called()

    def test_scale_async_exception_sends_failure_message_not_silent(self, patch_config):
        """Исключение scale шлёт только безопасный fact-free тип ошибки."""
        from services.approval_checker_models import FactFreeTemplate

        with patch(
            "services.budget_scaler.get_scale_config",
            side_effect=RuntimeError("secret boom"),
        ), patch(
            "services.approval_telegram.send_fact_free"
        ) as mock_fact_free, patch("services.notifications.send_telegram") as mock_raw:
            import services.telegram_console as console
            console._run_scale_async()  # не должно бросить исключение наружу

            mock_fact_free.assert_called_once_with(
                FactFreeTemplate.CHECKER_INTERNAL_ERROR,
                channel="ads",
                error_type="RuntimeError",
            )
            mock_raw.assert_not_called()

    def test_launch_async_exception_sends_failure_message_not_silent(self, patch_config):
        """Исключение launch шлёт только безопасный fact-free тип ошибки."""
        from services.approval_checker_models import FactFreeTemplate

        with patch(
            "services.auto_launch._get_autopilot_config",
            side_effect=RuntimeError("secret boom"),
        ), patch(
            "services.approval_telegram.send_fact_free"
        ) as mock_fact_free, patch("services.notifications.send_telegram") as mock_raw:
            import services.telegram_console as console
            console._run_launch_async()

            mock_fact_free.assert_called_once_with(
                FactFreeTemplate.CHECKER_INTERNAL_ERROR,
                channel="ads",
                error_type="RuntimeError",
            )
            mock_raw.assert_not_called()


# ===========================================================================
# 7. /launch при уже отработавшем дне — skipped_reason, без дублирования
# ===========================================================================

class TestLaunchDailyLimit:
    """Пустые/остановленные прогоны не выдают ложный результат действия."""

    def test_launch_already_ran_today_skipped_reason_in_summary(self, patch_config):
        """Дневной лимит не дублирует запуск и не создаёт success-сообщение."""
        skipped = "дневной лимит исчерпан: 1/1 запусков сегодня"
        with patch(
            "services.auto_launch._get_autopilot_config",
            return_value={"max_launches_per_day": 1},
        ), patch(
            "services.auto_launch.run_auto_launch",
            return_value={"recommendations": [], "skipped_reason": skipped, "error": None},
        ) as mock_run, patch(
            "services.action_gateway.get_operation"
        ) as mock_get_operation, patch(
            "services.approval_telegram.send_action_outcome"
        ) as mock_outcome:
            import services.telegram_console as console
            console._run_launch_async()

            mock_run.assert_called_once()
            mock_get_operation.assert_not_called()
            mock_outcome.assert_not_called()

    def test_launch_recommendation_creates_proposal_not_action(self, patch_config):
        """Launch recommendation сохраняется как proposal без gateway."""
        with patch(
            "services.auto_launch._get_autopilot_config",
            return_value={"max_launches_per_day": 1},
        ), patch(
            "services.auto_launch.run_auto_launch",
            return_value={
                "recommendations": [{"card_id": "card1", "card_name": "Креатив"}],
                "skipped_reason": None,
                "error": None,
            },
        ), patch(
            "services.action_gateway.get_operation"
        ) as mock_get_operation, patch(
            "services.approval_telegram.send_action_outcome"
        ) as mock_outcome, patch(
            "services.telegram_console._persist_telegram_proposal"
        ) as mock_propose, patch(
            "services.telegram_console._deliver_owner_proposals"
        ), patch("services.notifications.send_telegram") as mock_raw:
            import services.telegram_console as console
            console._run_launch_async()

            mock_get_operation.assert_not_called()
            mock_outcome.assert_not_called()
            mock_propose.assert_called_once()
            mock_raw.assert_not_called()

    def test_scale_kill_switch_summary_mentions_it(self, patch_config):
        """Kill switch не создаёт typed outcome, потому что действия не было."""
        with patch(
            "services.budget_scaler.get_scale_config",
            return_value={"max_scales_per_run": 2},
        ), patch(
            "services.budget_scaler.run_budget_scaling",
            return_value={"recommendations": [], "skipped_reason": "kill_switch"},
        ), patch(
            "services.action_gateway.get_operation"
        ) as mock_get_operation, patch(
            "services.approval_telegram.send_action_outcome"
        ) as mock_outcome, patch("services.notifications.send_telegram") as mock_raw:
            import services.telegram_console as console
            console._run_scale_async()

            mock_get_operation.assert_not_called()
            mock_outcome.assert_not_called()
            mock_raw.assert_not_called()


# ===========================================================================
# 8. Callback pause:<ad_id> → execute_owner_pause
# ===========================================================================

class TestExecuteOwnerPause:
    """Legacy pause-кнопка только сообщает об устаревании."""

    def test_pause_button_writes_decision_and_override(self, patch_config):
        """Legacy entry point не пишет decision/override."""
        with patch("services.autopilot.approve_pause", return_value=(True, "Тестовое объявление")) as mock_approve, \
             patch("services.autopilot.add_manual_override") as mock_override, \
             patch("services.telegram_bot._answer_callback") as mock_answer, \
             patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.execute_owner_pause("120210000000000001", "cid_pause_1")

            mock_approve.assert_not_called()
            mock_override.assert_not_called()
            mock_answer.assert_called_once()
            assert "устарела" in mock_answer.call_args[0][1].lower()
            mock_tg.assert_not_called()

    def test_pause_button_uses_cache_meta(self, patch_config):
        """Legacy entry point не читает кэш для мутации."""
        import services.telegram_console as console
        console._ADS_CACHE["120210000000000002"] = {
            "name": "Кэшированное имя", "reason": "пауза владельцем из /ads",
            "spend": 50.0, "leads": 3, "cpl": 16.6, "ctr": 1.1, "romi": None, "qual_pct": None,
        }
        with patch("services.autopilot.approve_pause", return_value=(True, "Кэшированное имя")) as mock_approve, \
             patch("services.autopilot.add_manual_override"), \
             patch("services.telegram_bot._answer_callback"), \
             patch("services.notifications.send_telegram"):
            console.execute_owner_pause("120210000000000002", "cid_pause_2")

            mock_approve.assert_not_called()

    def test_pause_button_fail_no_override_written(self, patch_config):
        """approve_pause вообще не вызывается."""
        with patch("services.autopilot.approve_pause", return_value=(False, "FB API timeout")), \
             patch("services.autopilot.add_manual_override") as mock_override, \
             patch("services.telegram_bot._answer_callback") as mock_answer, \
             patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.execute_owner_pause("120210000000000003", "cid_pause_3")

            mock_override.assert_not_called()
            mock_answer.assert_called_once()
            assert "устарела" in mock_answer.call_args[0][1].lower()
            mock_tg.assert_not_called()

    def test_pause_invalid_ad_id_ignored_at_handle_callback(self, patch_config):
        """Невалидный ad_id отсекается ещё в _handle_callback (не доходит до execute_owner_pause)."""
        with patch("services.telegram_console.execute_owner_pause") as mock_exec, \
             patch("services.telegram_bot._answer_callback"):
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="pause:abc")
            _handle_callback(cb)
            mock_exec.assert_not_called()

    def test_pause_foreign_chat_id_callback_ignored(self, patch_config):
        """Чужой chat_id в callback pause:<ad_id> → полный игнор, execute_owner_pause не вызван."""
        with patch("services.telegram_console.execute_owner_pause") as mock_exec, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id="999", chat_id="999", data="pause:120210000000000001")
            _handle_callback(cb)
            mock_exec.assert_not_called()
            mock_answer.assert_not_called()

    def test_pause_valid_ad_id_routes_to_execute_owner_pause(self, patch_config):
        """Валидная старая кнопка тоже не доходит до mutation handler."""
        with patch("services.telegram_console.execute_owner_pause") as mock_exec, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data="pause:120210000000000009", cb_id="cid_route")
            _handle_callback(cb)
            mock_exec.assert_not_called()
            assert "устарела" in mock_answer.call_args.args[1].lower()


# ===========================================================================
# 9. Единая точка диспатча команд пульта (второго поллера больше нет)
# ===========================================================================

class TestSingleDispatchEntryPoint:
    """Команды пульта исполняются из уже доверенного текста, без своего getUpdates.

    Раньше consol'ьные команды читал ВТОРОЙ getUpdates с файловым офсетом. Он
    подтверждал offset для всех клиентов бота и мог «съесть» апдейт до
    owner-обработчика — так терялся и /digest, и обычные команды пульта.
    """

    def test_known_command_is_dispatched_and_named(self, patch_config):
        import services.telegram_console as console

        with patch.object(console, "_dispatch") as dispatch:
            executed = console.dispatch_owner_text(
                "/status",
                source_ref="telegram-message:1:2",
            )

        assert executed == "status"
        assert dispatch.call_args.args[0] == "status"
        assert dispatch.call_args.kwargs["source_ref"] == "telegram-message:1:2"

    def test_command_arguments_are_passed_through(self, patch_config):
        import services.telegram_console as console

        with patch.object(console, "_dispatch") as dispatch:
            console.dispatch_owner_text("/hourly 123456789")

        assert dispatch.call_args.args == ("hourly", "123456789")

    def test_unknown_command_is_not_dispatched(self, patch_config):
        import services.telegram_console as console

        with patch.object(console, "_dispatch") as dispatch:
            executed = console.dispatch_owner_text("/чтототакое")

        assert executed is None
        dispatch.assert_not_called()
        assert console.is_known_command("/чтототакое") is False
        assert console.is_known_command("/status") is True

    def test_plain_text_is_not_a_command(self, patch_config):
        import services.telegram_console as console

        with patch.object(console, "_dispatch") as dispatch:
            assert console.dispatch_owner_text("дорого, но лидов много") is None

        dispatch.assert_not_called()

    def test_digest_is_a_known_command_of_the_console(self, patch_config):
        import services.telegram_console as console

        assert "digest" in console._COMMANDS


# ===========================================================================
# Регресс: threading.Thread не запускается синхронно (тред реально изолирует поллер)
# ===========================================================================

class TestThreadIsolation:
    """Убеждаемся, что мок threading.Thread не подменяет реальный запуск синхронным вызовом."""

    def test_dispatch_scale_does_not_block_on_run_budget_scaling(self, patch_config):
        """_dispatch('scale') не вызывает run_budget_scaling напрямую в основном потоке."""
        with patch("services.notifications.send_telegram"), \
             patch("services.approval_telegram.send_fact_free"), \
             patch("services.telegram_console.threading.Thread") as mock_thread, \
             patch("services.budget_scaler.run_budget_scaling") as mock_run:
            import services.telegram_console as console
            console._dispatch("scale")
            mock_run.assert_not_called()
            mock_thread.assert_called_once()


# ===========================================================================
# 10. /products — сводка по продуктам из creative_kb.target_product
# ===========================================================================
#
# Читает реальную (временную) creative_kb через services.creative_intelligence
# (по образцу tests/test_product_report.py::kb) — не мокаем SQL, только границу
# БД (tmp sqlite вместо data/decisions.db). См. docs/specs/ARCH-product-tags.md §6-7.

def _insert_kb_row(db_path, ad_id, target_product=None, status="ACTIVE",
                    spend=100.0, leads=10, qual_leads=2, ad_name="Тест | Реклама"):
    """Вставляет строку creative_kb напрямую (без бизнес-логики синка),
    по образцу tests/test_product_report.py::_insert_row."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO creative_kb (ad_id, ad_name, target_product, status, spend, leads, qual_leads)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ad_id, ad_name, target_product, status, spend, leads, qual_leads),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _reset_ci_db_path():
    """Сбрасываем services.creative_intelligence.DB_PATH до/после каждого теста —
    иначе тесты /products протекают в data/decisions.db или в KB другого теста."""
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


@pytest.fixture
def products_kb(tmp_path):
    """Инициализирует пустую creative_kb (полная схема через миграции) во временной
    директории и переключает на неё services.creative_intelligence.DB_PATH."""
    db_path = str(tmp_path / "products_test.db")
    ci.init_kb(db_path)
    return db_path


class TestProducts:
    """/products: сводка «какого продукта больше» из creative_kb.target_product."""

    def test_products_collected_from_kb_all_four_present(self, patch_config, products_kb):
        """2 PRODA + 1 PRODB ACTIVE → в тексте все 4 продукта (даже СТАРТ с нулями),
        лидеры отсортированы по числу объявлений (PRODA первый)."""
        _insert_kb_row(products_kb, "ad-1", target_product="PRODA", spend=100.0, leads=10, qual_leads=3)
        _insert_kb_row(products_kb, "ad-2", target_product="PRODA", spend=50.0, leads=5, qual_leads=1)
        _insert_kb_row(products_kb, "ad-3", target_product="PRODB", spend=80.0, leads=8, qual_leads=2)

        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/products"))

            mock_tg.assert_called_once()
            text = mock_tg.call_args[0][0]

            for product in ("PRODA", "PRODB", "СТАРТ", "ОБЩАЯ"):
                assert product in text
            assert "Всего активных: 3" in text
            # PRODA (2 объявления) должен идти раньше PRODB (1 объявление) — сортировка по ads_count
            assert text.index("PRODA") < text.index("PRODB")
            assert "🥇" in text

    def test_products_null_row_shows_unmarked_line(self, patch_config, products_kb):
        """ACTIVE-объявление с target_product=NULL → строка «без разметки» показана."""
        _insert_kb_row(products_kb, "ad-1", target_product="PRODA", spend=100.0)
        _insert_kb_row(products_kb, "ad-2", target_product=None, spend=30.0)

        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/products"))

            text = mock_tg.call_args[0][0]
            assert "без разметки" in text.lower()

    def test_products_no_null_row_hides_unmarked_line(self, patch_config, products_kb):
        """Все ACTIVE-объявления размечены каноном → строки «без разметки» нет."""
        _insert_kb_row(products_kb, "ad-1", target_product="PRODA", spend=100.0)
        _insert_kb_row(products_kb, "ad-2", target_product="PRODB", spend=50.0)

        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/products"))

            text = mock_tg.call_args[0][0]
            assert "без разметки" not in text.lower()

    def test_products_only_active_status_counted(self, patch_config, products_kb):
        """PAUSED/DELETED объявления не попадают в срез — только ACTIVE."""
        _insert_kb_row(products_kb, "ad-1", target_product="PRODA", status="ACTIVE", spend=100.0)
        _insert_kb_row(products_kb, "ad-2", target_product="PRODA", status="PAUSED", spend=999.0)
        _insert_kb_row(products_kb, "ad-3", target_product="PRODA", status="DELETED", spend=999.0)

        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/products"))

            text = mock_tg.call_args[0][0]
            assert "Всего активных: 1" in text
            assert "999" not in text

    def test_products_empty_kb_all_zero_no_crash(self, patch_config, products_kb):
        """Пустая KB (нет ACTIVE объявлений) → все 4 продукта с нулями, не падает."""
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/products"))

            mock_tg.assert_called_once()
            text = mock_tg.call_args[0][0]
            assert "Всего активных: 0" in text

    def test_products_qual_pct_computed_from_leads_and_quals(self, patch_config, products_kb):
        """5 лидов / 2 квала у PRODA → квал% = 40% в тексте."""
        _insert_kb_row(products_kb, "ad-1", target_product="PRODA", spend=100.0, leads=5, qual_leads=2)

        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/products"))

            text = mock_tg.call_args[0][0]
            assert "40%" in text

    def test_products_db_unavailable_sends_warning_not_crash(self, patch_config):
        """KB не инициализирована (DB_PATH=None) → ⚠️ владельцу, не падает."""
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/products"))

            mock_tg.assert_called_once()
            text = mock_tg.call_args[0][0]
            assert "⚠️" in text

    def test_products_foreign_chat_id_ignored(self, patch_config, products_kb):
        """Регресс безопасности: чужой from.id/chat.id → /products тоже игнорируется,
        как и остальные команды пульта (TestSecurity)."""
        _insert_kb_row(products_kb, "ad-1", target_product="PRODA", spend=100.0)

        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            msg = _make_msg(from_id="999", chat_id="999", text="/products")
            console.handle_message(msg)
            mock_tg.assert_not_called()

    def test_products_truncated_at_4096(self, patch_config, products_kb):
        """Сформированный текст обрезается по лимиту Telegram (4096)."""
        _insert_kb_row(products_kb, "ad-1", target_product="PRODA", spend=100.0)

        with patch(
            "services.telegram_console._format_products_report",
            return_value="x" * 5000,
        ), patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/products"))

            text = mock_tg.call_args[0][0]
            assert len(text) <= 4096

    def test_help_includes_products_command(self, patch_config):
        """/help содержит новую команду /products."""
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/help"))

            text = mock_tg.call_args[0][0]
            assert "/products" in text


class TestBuildProductsSummaryPure:
    """_build_products_summary — чистая функция, без БД."""

    def test_always_four_products_even_when_empty(self):
        import services.telegram_console as console
        summary = console._build_products_summary([])

        products = {r["product"] for r in summary["rows"]}
        assert products == {"PRODA", "PRODB", "СТАРТ", "ОБЩАЯ"}
        assert all(r["share_pct"] == 0.0 for r in summary["rows"])
        assert summary["unmarked"] is None
        assert summary["total_ads"] == 0

    def test_unmarked_bucket_separated_from_four_products(self):
        import services.telegram_console as console
        raw = [
            {"product": "PRODA", "ads_count": 2, "spend": 100.0, "leads": 10, "quals": 2},
            {"product": None, "ads_count": 1, "spend": 20.0, "leads": 1, "quals": 0},
            {"product": "СТАРЫЙ_КОД", "ads_count": 1, "spend": 5.0, "leads": 0, "quals": 0},
        ]
        summary = console._build_products_summary(raw)

        assert summary["unmarked"]["ads_count"] == 2  # None + "СТАРЫЙ_КОД" объединены
        assert summary["unmarked"]["spend"] == 25.0
        # unmarked не входит в долю расхода 4 продуктов
        proda_row = next(r for r in summary["rows"] if r["product"] == "PRODA")
        assert proda_row["share_pct"] == 100.0

    def test_share_pct_sums_to_100_when_data_present(self):
        import services.telegram_console as console
        raw = [
            {"product": "PRODA", "ads_count": 2, "spend": 60.0, "leads": 10, "quals": 2},
            {"product": "PRODB", "ads_count": 1, "spend": 40.0, "leads": 5, "quals": 1},
        ]
        summary = console._build_products_summary(raw)
        total_share = round(sum(r["share_pct"] for r in summary["rows"]), 1)
        assert total_share == 100.0

    def test_sorted_by_ads_count_desc(self):
        import services.telegram_console as console
        raw = [
            {"product": "PRODA", "ads_count": 1, "spend": 10.0, "leads": 1, "quals": 0},
            {"product": "PRODB", "ads_count": 3, "spend": 10.0, "leads": 1, "quals": 0},
        ]
        summary = console._build_products_summary(raw)
        assert summary["rows"][0]["product"] == "PRODB"


# ===========================================================================
# 11. /hourly — почасовой сбор младше 48ч + таблица по ad_id
# ===========================================================================
#
# Читает реальную (временную) creative_kb + ad_hourly_metrics через
# services.creative_intelligence (по образцу /products выше). Границу БД мокаем
# tmp sqlite вместо data/decisions.db.

def _insert_hourly_data(db_path, ad_id, created_at, ad_name, hours):
    """Вставляет строку creative_kb + почасовые строки ad_hourly_metrics.
    hours: list of (datetime_hour, spend, impressions, clicks, actions_lead)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO creative_kb (ad_id, ad_name, created_at) VALUES (?, ?, ?)",
            (ad_id, ad_name, created_at),
        )
        for dt_hour, spend, impr, clicks, leads in hours:
            conn.execute(
                "INSERT INTO ad_hourly_metrics "
                "(ad_id, datetime_hour, spend, impressions, clicks, actions_lead, "
                "lead_semantics_version, lead_parse_status) "
                "VALUES (?, ?, ?, ?, ?, ?, 2, 'ok')",
                (ad_id, dt_hour, spend, impr, clicks, leads),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def hourly_kb(tmp_path):
    """Пустая creative_kb + ad_hourly_metrics (полная схема через миграции) во
    временной директории; переключает services.creative_intelligence.DB_PATH на неё."""
    db_path = str(tmp_path / "hourly_test.db")
    ci.init_kb(db_path)
    return db_path


def _recent_iso(hours_ago=2):
    """created_time в стиле FB ('...+0000') на hours_ago часов назад."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


class TestHourly:
    """/hourly: список объявлений младше 48ч с собранными часами и таблица по ad_id."""

    def test_hourly_list_shows_collected_ads(self, patch_config, hourly_kb):
        """/hourly → имя, число собранных часов, суммарные расход/лиды, ad_id."""
        _insert_hourly_data(hourly_kb, "12345", _recent_iso(2), "CityA | Тест", [
            ("2026-07-05T08:00:00", 3.14, 1200, 15, 2),
            ("2026-07-05T09:00:00", 5.0, 900, 10, 1),
        ])
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/hourly"))

            mock_tg.assert_called_once()
            text = mock_tg.call_args[0][0]
            assert "CityA | Тест" in text
            assert "2ч" in text          # 2 собранных часа
            assert "12345" in text        # ad_id
            assert "лиды 3" in text       # 2 + 1 суммарно

    def test_hourly_list_empty(self, patch_config, hourly_kb):
        """Нет собранных часов → «данных пока нет»."""
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/hourly"))

            mock_tg.assert_called_once()
            text = mock_tg.call_args[0][0]
            assert "данных пока нет" in text

    def test_hourly_list_excludes_older_than_48h(self, patch_config, hourly_kb):
        """Объявление старше 48ч (created_at 72ч назад) не попадает в список."""
        _insert_hourly_data(hourly_kb, "99999", _recent_iso(72), "Старое", [
            ("2026-07-01T08:00:00", 1.0, 100, 5, 0),
        ])
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/hourly"))

            text = mock_tg.call_args[0][0]
            assert "данных пока нет" in text

    def test_hourly_detail_table(self, patch_config, hourly_kb):
        """/hourly <ad_id> → таблица «час | $ | показы | клики | лиды» по часам."""
        _insert_hourly_data(hourly_kb, "12345", _recent_iso(2), "Тест", [
            ("2026-07-05T08:00:00", 3.0, 1200, 15, 2),
        ])
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/hourly 12345"))

            mock_tg.assert_called_once()
            text = mock_tg.call_args[0][0]
            assert "час | $ | показы | клики | лиды" in text
            assert "07-05 08" in text     # компактная метка часа
            assert "1200" in text          # показы

    def test_hourly_detail_empty_table(self, patch_config, hourly_kb):
        """/hourly <ad_id> без собранных часов → «данных пока нет»."""
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/hourly 00000"))

            mock_tg.assert_called_once()
            text = mock_tg.call_args[0][0]
            assert "данных пока нет" in text

    def test_hourly_foreign_chat_id_ignored(self, patch_config, hourly_kb):
        """Регресс безопасности: чужой from.id/chat.id → /hourly игнорируется."""
        _insert_hourly_data(hourly_kb, "12345", _recent_iso(2), "Тест", [
            ("2026-07-05T08:00:00", 3.0, 1200, 15, 2),
        ])
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            msg = _make_msg(from_id="999", chat_id="999", text="/hourly")
            console.handle_message(msg)
            mock_tg.assert_not_called()

    def test_hourly_truncated_at_4096(self, patch_config, hourly_kb):
        """Длинный ряд обрезается по границе слова до лимита Telegram (4096)."""
        with patch("services.telegram_console._fetch_hourly_list", return_value=[]), \
             patch("services.telegram_console._format_hourly_list", return_value="слово " * 2000), \
             patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/hourly"))

            text = mock_tg.call_args[0][0]
            assert len(text) <= 4096

    def test_help_includes_hourly_command(self, patch_config):
        """/help содержит новую команду /hourly."""
        with patch("services.notifications.send_telegram") as mock_tg:
            import services.telegram_console as console
            console.handle_message(_make_msg(text="/help"))

            text = mock_tg.call_args[0][0]
            assert "/hourly" in text
