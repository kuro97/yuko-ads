"""Deprecated scheduler также обязан ходить через approval-first producer.

`agent/scheduler.py` — легаси-контур с `auto_apply`. Он больше НЕ может
запаузить рекламу сам: `execute_pause` (алиас `propose_pause`) только создаёт
предложение владельцу. Значит запись решения PAUSED в этом контуре недопустима
ни при отказе guard'а, ни при успешно созданном предложении — «PAUSED» в базе
означало бы применённое отключение, которого не было.
"""

from unittest.mock import patch

import pytest

from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import blocked_outcome, install_proposal_recorder


def _candidate() -> dict:
    return {
        "id": "1",
        "name": "Тест",
        "recommendation": "ОТКЛЮЧИТЬ",
        "reason": "слив",
    }


def _inventory_with_replacement(ad_ids) -> dict:
    """У кандидата есть доказанная ACTIVE-замена — иначе откажет last-active guard."""
    inventories: dict = {}
    for index, ad_id in enumerate(ad_ids):
        adset_id = str(100 + index)
        replacement = f"replacement-{ad_id}"
        context = {
            current_id: {
                "ad_id": current_id,
                "adset_id": adset_id,
                "name": current_id,
                "configured_status": "ACTIVE",
                "effective_status": "ACTIVE",
            }
            for current_id in (str(ad_id), replacement)
        }
        inventories[adset_id] = {
            "adset_id": adset_id,
            "active_ids": {str(ad_id), replacement},
            "candidate_context": {str(ad_id): context[str(ad_id)]},
            "inventory_context": context,
            "complete": True,
            "pages_read": 1,
            "error": None,
        }
    return inventories


@pytest.fixture(autouse=True)
def producer_boundary(tmp_path, monkeypatch):
    """Живой propose_pause с фейковым inventory; запись плана вместо БД.

    FB-мутаторы и execution boundary под Mock'ами: любой прямой вызов уронит
    тест — легаси-контур не должен иметь обходного пути к Facebook.
    """
    from services import adset_pause_guard

    def current_inventory(ad_ids):
        return adset_pause_guard.fetch_pause_inventory(list(ad_ids))

    def fake_exact_contexts(ad_ids, *, require_names=True):
        del require_names
        wanted = {str(ad_id) for ad_id in ad_ids}
        contexts = {
            ad_id: context
            for inventory in current_inventory(ad_ids).values()
            for ad_id, context in (inventory.get("candidate_context") or {}).items()
            if ad_id in wanted
        }
        return contexts, None

    monkeypatch.setattr(
        adset_pause_guard, "fetch_pause_inventory", _inventory_with_replacement
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory", current_inventory
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts", fake_exact_contexts
    )
    return install_proposal_recorder(monkeypatch, tmp_path)


def test_daily_analysis_does_not_save_pause_when_guard_blocks(producer_boundary):
    """Guard отказал → решение PAUSED не сохраняется, success=False."""
    blocked = blocked_outcome("last_active_without_replacement")

    with patch("agent.scheduler.load_settings", return_value={"auto_apply": True, "thresholds": {}}), \
         patch("agent.scheduler.analyze_all", return_value=[_candidate()]), \
         patch("services.action_producer_gateway.execute_pause", return_value=blocked) as mock_safe, \
         patch("agent.scheduler.save_decision") as mock_save, \
         patch("agent.scheduler._log_batch"), \
         patch("agent.scheduler.save_settings"):
        from agent.scheduler import daily_analysis

        result = daily_analysis()

    # Легаси-контур импортирует именно алиас execute_pause — патч по нему и нужен.
    assert mock_safe.call_args.args == ("1",)
    mock_save.assert_not_called()
    assert result["actions"][0]["success"] is False
    producer_boundary.assert_no_direct_provider_mutation()


def test_daily_analysis_creates_proposal_without_saving_pause(producer_boundary):
    """Guard разрешил → создано предложение, но PAUSED в базу НЕ пишется.

    Раньше контур сам паузил рекламу и сразу фиксировал решение PAUSED. Прямой
    мутатор удалён: на этом шаге существует только предложение, поэтому записи
    PAUSED нет — она появится лишь после одобрения владельца.

    При этом сам ШАГ планировщика успешен: в журнале стоит action
    `PROPOSAL_CREATED` и `success=True` («предложение создано»), а не ложный
    отказ. Раньше `success = confirmed and run is not None` всегда давал False,
    и контур писал в лог «PAUSE заблокирована safety guard: None».
    """
    with patch("agent.scheduler.load_settings", return_value={"auto_apply": True, "thresholds": {}}), \
         patch("agent.scheduler.analyze_all", return_value=[_candidate()]), \
         patch("agent.scheduler.save_decision") as mock_save, \
         patch("agent.scheduler._log_batch"), \
         patch("agent.scheduler.save_settings"):
        from agent.scheduler import daily_analysis

        result = daily_analysis()

    producer_boundary.assert_proposed(
        "1",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.CRON,
        action_kind="PAUSE_AD",
    )
    mock_save.assert_not_called()
    assert result["actions"][0]["action"] == "PROPOSAL_CREATED"
    assert result["actions"][0]["success"] is True, \
        "созданное предложение — успешный шаг планировщика, а не отказ guard'а"
    producer_boundary.assert_no_direct_provider_mutation()


def test_daily_analysis_proposes_for_every_candidate_without_break(producer_boundary):
    """Созданное предложение НЕ обрывает батч: три кандидата → три предложения.

    Регрессия: `success` всегда был False, первый же кандидат попадал в ветку
    «заблокирована safety guard» и делал `break` — остальные кандидаты дня
    молча терялись, владелец о них даже не узнавал.
    """
    candidates = [
        {"id": ad_id, "name": f"Тест {ad_id}", "recommendation": "ОТКЛЮЧИТЬ", "reason": "слив"}
        for ad_id in ("11111", "22222", "33333")
    ]

    with patch("agent.scheduler.load_settings", return_value={"auto_apply": True, "thresholds": {}}), \
         patch("agent.scheduler.analyze_all", return_value=candidates), \
         patch("agent.scheduler.save_decision") as mock_save, \
         patch("agent.scheduler._log_batch"), \
         patch("agent.scheduler.save_settings"):
        from agent.scheduler import daily_analysis

        result = daily_analysis()

    assert [item["ad_id"] for item in result["actions"]] == ["11111", "22222", "33333"]
    assert all(item["success"] is True for item in result["actions"])
    assert producer_boundary.subject_ids == ["11111", "22222", "33333"]
    mock_save.assert_not_called()
    producer_boundary.assert_no_direct_provider_mutation()


def test_daily_analysis_logs_replacement_without_saving_pause(producer_boundary):
    """Замена вместо паузы → тоже без записи PAUSED и без мутации FB."""
    enqueued = blocked_outcome(
        "replacement_enqueued",
        action="REPLACEMENT_ENQUEUED",
        workflow_id="workflow-1",
    )

    with patch("agent.scheduler.load_settings", return_value={"auto_apply": True, "thresholds": {}}), \
         patch("agent.scheduler.analyze_all", return_value=[_candidate()]), \
         patch("services.action_producer_gateway.execute_pause", return_value=enqueued), \
         patch("agent.scheduler.save_decision") as mock_save, \
         patch("agent.scheduler._log_batch"), \
         patch("agent.scheduler.save_settings"):
        from agent.scheduler import daily_analysis

        result = daily_analysis()

    mock_save.assert_not_called()
    assert result["actions"][0]["action"] == "REPLACEMENT_ENQUEUED"
    assert result["actions"][0]["success"] is False
    producer_boundary.assert_no_direct_provider_mutation()
