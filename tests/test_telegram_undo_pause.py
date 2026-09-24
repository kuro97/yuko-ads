"""
Тесты services/telegram_bot.py — callback undo_pause:<ad_id> + _execute_undo_pause
(кнопка «↩️ Вернуть» в отчётах пауз автопилота — классика + Live).

Approval-first: кнопка НЕ возвращает рекламу сама. Она создаёт UNPAUSE-предложение
владельцу через services.action_producer_gateway.propose_unpause и отвечает
«📨 Создано предложение №…, ждёт одобрения». Прямой мутации Facebook в этом пути
быть не должно вообще — её делает только execution boundary после одобрения.

Producer здесь живой (install_proposal_recorder подменяет лишь запись в БД),
поэтому его fail-closed проверки настоящие: объявление существует, live inventory
полон, статус действительно PAUSED. Подменяем только границы FB-чтения.

Конвенции — как tests/test_telegram_stop_launch.py (_make_cb, patch_config, OWNER_ID).
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import install_proposal_recorder

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
    sys.modules["google.genai.types"] = MagicMock()


OWNER_ID = "111"
AD_ID = "123456789"
ADSET_ID = "100"


def _make_cb(from_id=OWNER_ID, chat_id=OWNER_ID, data=f"undo_pause:{AD_ID}", cb_id="cid1"):
    """Создаёт словарь callback_query (как в test_telegram_bot.py)."""
    return {
        "id": cb_id,
        "from": {"id": int(from_id)},
        "message": {"chat": {"id": int(chat_id)}},
        "data": data,
    }


def _make_entry(ad_name="CityA / Петров / Тема А", returned=False):
    """Тестовая запись undo_map."""
    return {
        "ad_id": AD_ID,
        "ad_name": ad_name,
        "returned": returned,
        "at": "2026-07-08T10:00:00+05:00",
    }


@pytest.fixture()
def patch_config():
    """Патч config.TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID."""
    with patch("config.TELEGRAM_BOT_TOKEN", "test-token"), \
         patch("config.TELEGRAM_CHAT_ID", OWNER_ID):
        yield


def _install_fb_reads(monkeypatch, *, effective_status="PAUSED", complete=True):
    """Подменяет ТОЛЬКО чтение FB внутри producer'а (мутаторы остаются под Mock)."""
    context = {
        "ad_id": AD_ID,
        "adset_id": ADSET_ID,
        "name": "CityA / Петров / Тема А",
        "configured_status": effective_status,
        "effective_status": effective_status,
    }

    def fake_exact(ad_ids, *, require_names=True):
        del require_names
        return {ad_id: context for ad_id in ad_ids if ad_id == AD_ID}, None

    def fake_inventory(ad_ids):
        del ad_ids
        return {
            ADSET_ID: {
                "adset_id": ADSET_ID,
                "active_ids": {"other-active"},
                "candidate_context": {AD_ID: context},
                "inventory_context": {AD_ID: context},
                "complete": complete,
                "pages_read": 1,
                "error": None if complete else "inventory_incomplete",
                "state_sha256": "b" * 64,
            }
        }

    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts", fake_exact
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory", fake_inventory
    )


@pytest.fixture()
def producer_boundary(monkeypatch, tmp_path):
    """Живой propose_unpause; план пишется в память, FB-мутаторы под Mock."""
    return install_proposal_recorder(monkeypatch, tmp_path)


# ===========================================================================
# _execute_undo_pause — создание предложения вместо мутации
# ===========================================================================

class TestExecuteUndoPause:
    def test_создаёт_предложение_и_не_трогает_facebook(
        self, patch_config, producer_boundary, monkeypatch
    ):
        """PAUSED объявление → UNPAUSE-предложение владельцу, FB не тронут."""
        _install_fb_reads(monkeypatch)
        with patch("services.autopilot.mark_pause_undone") as mock_mark, \
             patch("services.autopilot.add_manual_override") as mock_override, \
             patch("agent.repositories.decisions_repo.save_decision") as mock_save, \
             patch("services.telegram_bot._answer_callback") as mock_answer, \
             patch("services.telegram_bot._deliver_owner_proposals") as mock_deliver:
            from services.telegram_bot import _execute_undo_pause
            _execute_undo_pause(AD_ID, "cbid1")

        plan = producer_boundary.assert_proposed(
            AD_ID,
            kind=ProposalKind.UNPAUSE,
            origin=ProposalOrigin.TELEGRAM_COMMAND,
            action_kind="UNPAUSE_AD",
        )
        assert plan.source_ref == f"telegram-undo-pause:{AD_ID}"
        producer_boundary.assert_no_direct_provider_mutation()

        # Ни одного локального side effect «уже вернул» — возврата ещё не было.
        mock_mark.assert_not_called()
        mock_override.assert_not_called()
        mock_save.assert_not_called()

        text = mock_answer.call_args.args[1]
        assert "Создано предложение" in text
        assert "ждёт одобрения" in text
        assert plan.targets[0].intended_payload["expected_after_status"] == "ACTIVE"
        mock_deliver.assert_called_once()

    def test_объявление_уже_активно_честный_отказ(
        self, patch_config, producer_boundary, monkeypatch
    ):
        """Реклама уже ACTIVE → gateway отказывает, предложения нет."""
        _install_fb_reads(monkeypatch, effective_status="ACTIVE")
        with patch("services.telegram_bot._answer_callback") as mock_answer, \
             patch("services.telegram_bot._deliver_owner_proposals") as mock_deliver:
            from services.telegram_bot import _execute_undo_pause
            _execute_undo_pause(AD_ID, "cbid1")

        assert producer_boundary.plans == []
        producer_boundary.assert_no_direct_provider_mutation()
        text = mock_answer.call_args.args[1]
        assert "UNPAUSE_TARGET_NOT_PAUSED" in text
        mock_deliver.assert_not_called()

    def test_неполный_inventory_fail_closed(
        self, patch_config, producer_boundary, monkeypatch
    ):
        """Неполный live inventory → предложение НЕ создаётся (fail-closed)."""
        _install_fb_reads(monkeypatch, complete=False)
        with patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_undo_pause
            _execute_undo_pause(AD_ID, "cbid1")

        assert producer_boundary.plans == []
        producer_boundary.assert_no_direct_provider_mutation()
        assert "inventory_incomplete" in mock_answer.call_args.args[1]

    def test_ошибка_producer_не_роняет_обработчик(
        self, patch_config, producer_boundary, monkeypatch
    ):
        """Неожиданное исключение producer'а → честный ответ, без падения."""
        monkeypatch.setattr(
            "services.action_producer_gateway.propose_unpause",
            MagicMock(side_effect=RuntimeError("FB упал")),
        )
        with patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _execute_undo_pause
            _execute_undo_pause(AD_ID, "cbid1")  # не должно бросить исключение

        assert producer_boundary.plans == []
        producer_boundary.assert_no_direct_provider_mutation()
        assert "попробуй позже" in mock_answer.call_args.args[1]

    def test_повторный_клик_идёт_в_тот_же_scope(
        self, patch_config, producer_boundary, monkeypatch
    ):
        """Второе нажатие на ту же кнопку использует тот же idempotency scope."""
        _install_fb_reads(monkeypatch)
        with patch("services.telegram_bot._answer_callback"), \
             patch("services.telegram_bot._deliver_owner_proposals"):
            from services.telegram_bot import _execute_undo_pause
            _execute_undo_pause(AD_ID, "cbid1")
            _execute_undo_pause(AD_ID, "cbid2")

        assert producer_boundary.source_refs == [
            f"telegram-undo-pause:{AD_ID}",
            f"telegram-undo-pause:{AD_ID}",
        ]
        producer_boundary.assert_no_direct_provider_mutation()

    def test_undo_map_запись_не_нужна_для_предложения(
        self, patch_config, producer_boundary, monkeypatch
    ):
        """Предложение строится по live-состоянию FB, а не по локальному undo_map.

        Раньше кнопка опиралась на запись undo_map («что мы паузили»). Теперь
        источник истины — живой статус объявления, поэтому отсутствие записи
        не мешает предложить возврат.
        """
        _install_fb_reads(monkeypatch)
        with patch("services.autopilot.get_pause_undo_entry", return_value=None), \
             patch("services.telegram_bot._answer_callback") as mock_answer, \
             patch("services.telegram_bot._deliver_owner_proposals"):
            from services.telegram_bot import _execute_undo_pause
            _execute_undo_pause(AD_ID, "cbid1")

        producer_boundary.assert_proposed(
            AD_ID,
            kind=ProposalKind.UNPAUSE,
            origin=ProposalOrigin.TELEGRAM_COMMAND,
            action_kind="UNPAUSE_AD",
        )
        assert "Создано предложение" in mock_answer.call_args.args[1]

    def test_запись_undo_map_не_меняет_поведение(
        self, patch_config, producer_boundary, monkeypatch
    ):
        """Наличие записи undo_map тоже ничего не меняет — контур один."""
        _install_fb_reads(monkeypatch)
        with patch("services.autopilot.get_pause_undo_entry", return_value=_make_entry()), \
             patch("services.autopilot.mark_pause_undone") as mock_mark, \
             patch("services.telegram_bot._answer_callback"), \
             patch("services.telegram_bot._deliver_owner_proposals"):
            from services.telegram_bot import _execute_undo_pause
            _execute_undo_pause(AD_ID, "cbid1")

        mock_mark.assert_not_called()
        assert len(producer_boundary.plans) == 1


# ===========================================================================
# Безопасность и валидация ad_id через _handle_callback (whitelist)
# ===========================================================================

class TestHandleCallbackUndoPauseSecurity:
    def test_чужой_chat_id_игнор(self, patch_config):
        """Чужой from_id/chat_id -> _execute_undo_pause НЕ вызван."""
        with patch("services.telegram_bot._execute_undo_pause") as mock_exec, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(from_id="999", chat_id="999")
            _handle_callback(cb)

            mock_exec.assert_not_called()
            mock_answer.assert_not_called()

    def test_невалидный_ad_id_буквы_игнор(self, patch_config):
        """undo_pause:abc (не цифры) -> предложение не строится."""
        with patch("services.telegram_bot._execute_undo_pause") as mock_exec, \
             patch("services.telegram_bot._answer_callback"):
            from services.telegram_bot import _handle_callback
            cb = _make_cb(data="undo_pause:abc")
            _handle_callback(cb)

            mock_exec.assert_not_called()

    def test_невалидный_ad_id_слишком_короткий_игнор(self, patch_config):
        """undo_pause:1234 (4 цифры — меньше минимума 5) -> предложения нет."""
        with patch("services.telegram_bot._execute_undo_pause") as mock_exec, \
             patch("services.telegram_bot._answer_callback"):
            from services.telegram_bot import _handle_callback
            cb = _make_cb(data="undo_pause:1234")
            _handle_callback(cb)

            mock_exec.assert_not_called()

    def test_пустой_ad_id_игнор(self, patch_config):
        """Malformed legacy prefix получает честный stale без предложения."""
        with patch("services.telegram_bot._execute_undo_pause") as mock_exec, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(data="undo_pause:")
            _handle_callback(cb)

            mock_exec.assert_not_called()
            assert "устарела" in mock_answer.call_args.args[1].lower()

    def test_валидный_маршрут(self, patch_config):
        """Валидный ad_id доходит до обработчика предложения."""
        with patch("services.telegram_bot._execute_undo_pause") as mock_exec, \
             patch("services.telegram_bot._answer_callback") as mock_answer:
            from services.telegram_bot import _handle_callback
            cb = _make_cb(data=f"undo_pause:{AD_ID}", cb_id="cid9")
            _handle_callback(cb)

            mock_exec.assert_called_once_with(AD_ID, "cid9")
            mock_answer.assert_not_called()
