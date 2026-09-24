"""
Тесты services/launch_verify.py — Контроль запуска.

Проверяем:
1. get_todays_launched_ads: читает ad_ids за сегодня из auto_launch_state.json,
   пропускает старый формат записей (без ad_ids) и записи не за сегодня.
2. Классификация _classify_ad: disapproved / pending / not_delivering / ok.
3. verify_todays_launches: потолок MAX_ADS_PER_RUN, "запусков не было" — тихий скип,
   Telegram отправляется ТОЛЬКО при наличии проблем, state сохраняется.
4. load_verify_state читает то, что сохранил verify_todays_launches.

Все FB/provider границы мокаются, сеть отрезана pytest_socket. Обычная daily
проверка read-only; replacement mutation разрешена только exact orchestrator.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.launch_verify import (  # noqa: E402
    get_todays_launched_ads,
    verify_todays_launches,
    verify_replacement_workflows,
    _classify_ad,
    MAX_ADS_PER_RUN,
)
from services import state_store  # noqa: E402


# ---------------------------------------------------------------------------
# Фикстуры изоляции
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_auto_launch_state(tmp_path, monkeypatch):
    """Подменяет путь к auto_launch_state.json на временный файл."""
    state_file = tmp_path / "auto_launch_state.json"
    monkeypatch.setattr("services.launch_verify._AUTO_LAUNCH_STATE_FILE", state_file)
    return state_file


@pytest.fixture
def isolated_verify_state(tmp_path, monkeypatch):
    """Подменяет путь к launch_verify_state.json на временный файл."""
    state_file = tmp_path / "launch_verify_state.json"
    monkeypatch.setattr("services.launch_verify._LAUNCH_VERIFY_STATE_FILE", state_file)
    return state_file


@pytest.fixture
def fixed_today(monkeypatch):
    """Фиксирует _get_today_str на '2026-07-07' и текущее время на 14:10 CityA."""
    from datetime import datetime, timezone, timedelta

    tz = timezone(timedelta(hours=5))
    fixed_now = datetime(2026, 7, 7, 14, 10, tzinfo=tz)

    monkeypatch.setattr("services.launch_verify._get_today_str", lambda: "2026-07-07")

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr("services.launch_verify.datetime", _FixedDatetime)
    return fixed_now


def _write_auto_launch_state(path: Path, launched_ever: dict) -> None:
    state_store.save_json_state(path, {
        "launched_today": [],
        "launched_ever": launched_ever,
        "last_launch_date": "2026-07-07",
    })


# ---------------------------------------------------------------------------
# get_todays_launched_ads
# ---------------------------------------------------------------------------

class TestGetTodaysLaunchedAds:
    def test_читает_ad_ids_за_сегодня(self, isolated_auto_launch_state, fixed_today):
        _write_auto_launch_state(isolated_auto_launch_state, {
            "card1": {
                "at": "2026-07-07T10:05:00+05:00",
                "name": "Карточка 1",
                "ad_ids": ["111", "222"],
            },
        })

        result = get_todays_launched_ads()

        assert len(result) == 2
        ad_ids = {item["ad_id"] for item in result}
        assert ad_ids == {"111", "222"}
        assert all(item["card_name"] == "Карточка 1" for item in result)

    def test_игнорирует_запуски_не_за_сегодня(self, isolated_auto_launch_state, fixed_today):
        _write_auto_launch_state(isolated_auto_launch_state, {
            "card_old": {
                "at": "2026-07-06T10:05:00+05:00",
                "name": "Вчерашняя карточка",
                "ad_ids": ["999"],
            },
        })

        result = get_todays_launched_ads()

        assert result == []

    def test_игнорирует_старый_формат_без_ad_ids(self, isolated_auto_launch_state, fixed_today):
        """Старая запись — просто строка iso_datetime (до фичи ad_ids) — не ломает чтение."""
        _write_auto_launch_state(isolated_auto_launch_state, {
            "card_legacy": "2026-07-07T10:05:00+05:00",
        })

        result = get_todays_launched_ads()

        assert result == []

    def test_игнорирует_запись_без_поля_ad_ids(self, isolated_auto_launch_state, fixed_today):
        """Новая запись (dict), но ad_ids не было добавлено (лог пустой) — не падает."""
        _write_auto_launch_state(isolated_auto_launch_state, {
            "card_no_ads": {"at": "2026-07-07T10:05:00+05:00", "name": "Без объявлений"},
        })

        result = get_todays_launched_ads()

        assert result == []

    def test_пустой_state_возвращает_пустой_список(self, isolated_auto_launch_state, fixed_today):
        result = get_todays_launched_ads()
        assert result == []


# ---------------------------------------------------------------------------
# Классификация _classify_ad — все 4 случая
# ---------------------------------------------------------------------------

class TestClassifyAd:
    def test_disapproved(self):
        assert _classify_ad("DISAPPROVED", 0) == "disapproved"

    def test_with_issues_тоже_disapproved(self):
        assert _classify_ad("WITH_ISSUES", 100) == "disapproved"

    def test_pending_review(self):
        assert _classify_ad("PENDING_REVIEW", 0) == "pending"

    def test_in_process_тоже_pending(self):
        assert _classify_ad("IN_PROCESS", 0) == "pending"

    def test_active_с_нулевыми_показами_не_крутится(self):
        assert _classify_ad("ACTIVE", 0) == "not_delivering"

    def test_active_с_показами_ok(self):
        assert _classify_ad("ACTIVE", 150) == "ok"

    def test_регистронезависимость(self):
        assert _classify_ad("disapproved", 0) == "disapproved"


class TestVerifyReplacementWorkflows:
    def test_disabled_by_default_never_touches_workflow(self):
        with patch(
            "services.autopilot.get_autopilot_config",
            return_value={"kill_switch": False},
        ), patch(
            "services.replacement_orchestrator.verify_and_complete_replacements"
        ) as verify, patch(
            "services.replacement_orchestrator.ensure_slot_for_workflow"
        ) as ensure:
            result = verify_replacement_workflows()

        assert result == {"ran": False, "skipped_reason": "replacement_disabled"}
        verify.assert_not_called()
        ensure.assert_not_called()

    @pytest.mark.parametrize("enabled", ["true", 1, None, []])
    def test_malformed_enabled_fails_closed(self, enabled):
        with patch(
            "services.autopilot.get_autopilot_config",
            return_value={
                "kill_switch": False,
                "replacement": {"enabled": enabled},
            },
        ), patch(
            "services.replacement_orchestrator.verify_and_complete_replacements"
        ) as verify:
            result = verify_replacement_workflows()

        assert result["ran"] is False
        assert result["skipped_reason"] == "invalid_replacement_enabled"
        verify.assert_not_called()

    def test_enabled_delegates_bound_slot_and_exact_active_completion(self):
        from services.replacement_orchestrator import (
            ReplacementSlotOutcome,
            ReplacementVerifyResult,
        )

        slot_result = ReplacementSlotOutcome(
            workflow_id="workflow-slot",
            action="SLOTS_RELEASED",
            required_slots=2,
            available_before=0,
            available_after=2,
            deficit_before=2,
            deficit_after=0,
            deleted_ad_ids=("zero-1", "zero-2"),
            claim_ids=("claim-1", "claim-2"),
            reason=None,
        )
        verify_result = ReplacementVerifyResult(
            ran=True,
            checked=2,
            waiting_workflow_ids=("workflow-pending",),
            ready_workflow_ids=("workflow-exact-active",),
            completed_workflow_ids=("workflow-exact-active",),
            blocked_workflow_ids=("workflow-wrong-adset",),
            errors=(),
        )
        with patch(
            "services.autopilot.get_autopilot_config",
            return_value={
                "kill_switch": False,
                "replacement": {"enabled": True},
            },
        ), patch(
            "services.cleanup_repository.get_cleanup_status",
            return_value={
                "replacement_workflows": [
                    {"workflow_id": "workflow-slot", "phase": "WAITING_SLOT"},
                    {"workflow_id": "workflow-unbound", "phase": "WAITING_SLOT"},
                ]
            },
        ), patch(
            "services.replacement_workflow.get_replacement_launch",
            side_effect=lambda workflow_id: (
                {"workflow_id": workflow_id} if workflow_id == "workflow-slot" else None
            ),
        ), patch(
            "services.replacement_orchestrator.ensure_slot_for_workflow",
            return_value=slot_result,
        ) as ensure, patch(
            "services.replacement_orchestrator.verify_and_complete_replacements",
            return_value=verify_result,
        ) as verify, patch(
            "integrations.facebook_ads_mutation_transport.set_ad_status"
        ) as provider_status:
            result = verify_replacement_workflows(limit=7)

        ensure.assert_called_once_with("workflow-slot")
        verify.assert_called_once_with(limit=7)
        # Прямого мутатора pause_ad больше нет; сторожим единственную реальную
        # точку смены статуса — она доступна только после одобрения владельца.
        provider_status.assert_not_called()
        assert result["ran"] is True
        assert result["waiting_workflow_ids"] == ("workflow-pending",)
        assert result["ready_workflow_ids"] == ("workflow-exact-active",)
        assert result["completed_workflow_ids"] == ("workflow-exact-active",)
        assert result["blocked_workflow_ids"] == ("workflow-wrong-adset",)
        assert result["slot_results"][0]["deleted_ad_ids"] == ("zero-1", "zero-2")
        assert result["errors"] == []


# ---------------------------------------------------------------------------
# verify_todays_launches — happy path + потолок + "запусков не было" + telegram
# ---------------------------------------------------------------------------

class TestVerifyTodaysLaunches:
    def test_запусков_сегодня_не_было_тихий_скип(
        self, isolated_auto_launch_state, isolated_verify_state, fixed_today,
    ):
        """Пустой auto_launch_state за сегодня -> launched=0, Telegram не шлётся."""
        with patch("services.launch_verify._send_problems_telegram") as mock_send:
            result = verify_todays_launches()

        assert result == {"launched": 0, "running": 0, "problems": []}
        mock_send.assert_not_called()
        # State НЕ должен сохраняться при полном скипе (нет данных за сегодня)
        assert not isolated_verify_state.exists()

    def test_все_объявления_крутятся_telegram_не_шлётся(
        self, isolated_auto_launch_state, isolated_verify_state, fixed_today,
    ):
        _write_auto_launch_state(isolated_auto_launch_state, {
            "card1": {
                "at": "2026-07-07T10:05:00+05:00",
                "name": "Карточка 1",
                "ad_ids": ["111", "222"],
            },
        })
        fb_info = {
            "111": {"name": "CityA | Тема А", "effective_status": "ACTIVE", "impressions": 500},
            "222": {"name": "CityB | Тема Б", "effective_status": "ACTIVE", "impressions": 300},
        }

        with patch("services.launch_verify._fetch_fb_status_and_impressions", return_value=fb_info), \
             patch("services.launch_verify._send_problems_telegram") as mock_send:
            result = verify_todays_launches()

        assert result["launched"] == 2
        assert result["running"] == 2
        assert result["problems"] == []
        mock_send.assert_not_called()

        # State сохранён — дайджест сможет прочитать
        saved = json.loads(isolated_verify_state.read_text(encoding="utf-8"))
        assert saved["launched"] == 2
        assert saved["running"] == 2
        assert saved["problems_count"] == 0

    def test_есть_проблемы_telegram_отправляется(
        self, isolated_auto_launch_state, isolated_verify_state, fixed_today,
    ):
        _write_auto_launch_state(isolated_auto_launch_state, {
            "card1": {
                "at": "2026-07-07T10:05:00+05:00",
                "name": "Карточка 1",
                "ad_ids": ["111", "222", "333", "444"],
            },
        })
        fb_info = {
            "111": {"name": "CityA | Тема А", "effective_status": "DISAPPROVED", "impressions": 0},
            "222": {"name": "CityB | Тема Б", "effective_status": "PENDING_REVIEW", "impressions": 0},
            "333": {"name": "CityC | Тема В", "effective_status": "ACTIVE", "impressions": 0},
            "444": {"name": "CityD | Тема Г", "effective_status": "ACTIVE", "impressions": 10},
        }

        with patch("services.launch_verify._fetch_fb_status_and_impressions", return_value=fb_info), \
             patch("services.launch_verify._send_problems_telegram") as mock_send:
            result = verify_todays_launches()

        assert result["launched"] == 4
        assert result["running"] == 1  # только 444
        kinds = {p["kind"] for p in result["problems"]}
        assert kinds == {"disapproved", "pending", "not_delivering"}
        mock_send.assert_called_once()
        problems_arg = mock_send.call_args[0][0]
        assert len(problems_arg) == 3

    def test_потолок_max_ads_per_run_соблюдён(
        self, isolated_auto_launch_state, isolated_verify_state, fixed_today,
    ):
        """При >MAX_ADS_PER_RUN объявлений — FB-запрос идёт только по первым N."""
        ad_ids = [str(i) for i in range(MAX_ADS_PER_RUN + 5)]
        _write_auto_launch_state(isolated_auto_launch_state, {
            "card1": {
                "at": "2026-07-07T10:05:00+05:00",
                "name": "Много объявлений",
                "ad_ids": ad_ids,
            },
        })

        with patch("services.launch_verify._fetch_fb_status_and_impressions", return_value={}) as mock_fetch, \
             patch("services.launch_verify._send_problems_telegram"):
            result = verify_todays_launches()

        called_ids = mock_fetch.call_args[0][0]
        assert len(called_ids) == MAX_ADS_PER_RUN
        assert result["launched"] == MAX_ADS_PER_RUN

    def test_fb_не_ответил_по_ad_id_не_считается_проблемой(
        self, isolated_auto_launch_state, isolated_verify_state, fixed_today,
    ):
        """Если FB не вернул данные по ad_id (chunk упал) — не в problems и не в running."""
        _write_auto_launch_state(isolated_auto_launch_state, {
            "card1": {
                "at": "2026-07-07T10:05:00+05:00",
                "name": "Карточка 1",
                "ad_ids": ["111"],
            },
        })

        with patch("services.launch_verify._fetch_fb_status_and_impressions", return_value={}), \
             patch("services.launch_verify._send_problems_telegram") as mock_send:
            result = verify_todays_launches()

        assert result["running"] == 0
        assert result["problems"] == []
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# load_verify_state
# ---------------------------------------------------------------------------

class TestLoadVerifyState:
    def test_читает_сохранённый_state(self, isolated_verify_state):
        from services.launch_verify import load_verify_state

        state_store.save_json_state(isolated_verify_state, {
            "date": "2026-07-06", "launched": 3, "running": 2, "problems_count": 1,
        })

        result = load_verify_state()
        assert result["date"] == "2026-07-06"
        assert result["running"] == 2

    def test_отсутствующий_файл_возвращает_пустой_dict(self, isolated_verify_state):
        from services.launch_verify import load_verify_state

        assert load_verify_state() == {}
