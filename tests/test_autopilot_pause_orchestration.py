"""Контракт replacement-aware PAUSE для production-контуров автопилота.

Проект approval-first: прямых мутаторов (`agent.analyzer.pause_ad`) больше нет,
все три контура — owner-callback (`approve_pause`), классика (`run_autopilot`) и
LIVE (`run_autopilot_live`) — только СОЗДАЮТ PAUSE-proposal через
`services.action_producer_gateway.propose_pause`.

Отсюда главный инвариант файла: созданное предложение — это ещё НЕ пауза.
Поэтому ни один контур не имеет права по факту предложения:
  • положить ad_id в `result["paused"]`,
  • записать решение PAUSED (`decisions_repo.save_decision`),
  • зарегистрировать undo (`record_pause_undo`) — возвращать пока нечего,
  • списать дневную квоту пауз (`_save_*_daily_state`),
  • тронуть Facebook напрямую.
Replacement-aware часть сохранена отдельным тестом: последний ACTIVE без
доказанной замены не получает предложения вовсе.
"""

from datetime import date
from unittest.mock import patch

import pytest

from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import blocked_outcome, install_proposal_recorder


def _inventory(ad_id: str, adset_id: str = "100", *, replacement: str | None = None) -> dict:
    """Live inventory одного adset.

    replacement=None воспроизводит ситуацию «в adset единственный ACTIVE» —
    именно её обязан блокировать last-active guard.
    """
    active = {ad_id} | ({replacement} if replacement else set())
    context = {
        current_id: {
            "ad_id": current_id,
            "adset_id": adset_id,
            "name": current_id,
            "configured_status": "ACTIVE",
            "effective_status": "ACTIVE",
        }
        for current_id in active
    }
    return {
        adset_id: {
            "adset_id": adset_id,
            "active_ids": active,
            "candidate_context": {ad_id: context[ad_id]},
            "inventory_context": context,
            "complete": True,
            "pages_read": 1,
            "error": None,
        }
    }


def _inventory_with_replacement(ad_ids) -> dict:
    """По умолчанию у каждого кандидата есть доказанная ACTIVE-замена.

    Без замены propose_pause честно отказал бы (LAST_EFFECTIVE_ACTIVE), и тесты
    про «предложение — не пауза» проверяли бы guard вместо своего инварианта.
    """
    inventories: dict = {}
    for index, ad_id in enumerate(ad_ids):
        inventories.update(
            _inventory(
                str(ad_id),
                adset_id=str(100 + index),
                replacement=f"replacement-{ad_id}",
            )
        )
    return inventories


@pytest.fixture(autouse=True)
def producer_boundary(tmp_path, monkeypatch):
    """Живой discovery/guard propose_pause + запись плана вместо БД + запрет мутаций.

    Producer читает inventory ЧЕРЕЗ `adset_pause_guard.fetch_pause_inventory`,
    поэтому patch инвентаря в тесте меняет картину мира сразу для guard и для
    producer — в проде расхождения между ними не бывает.
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


def _classic_ad() -> dict:
    return {
        "id": "classic-1",
        "name": "Classic candidate",
        "recommendation": "ОТКЛЮЧИТЬ",
        "reason": "слив",
        "effective_status": "ACTIVE",
        "spend": 100.0,
        "leads": 1,
        "cpl": 100.0,
        "ctr": 0.5,
        "romi": 0.0,
        "qual_pct": 0.0,
    }


def test_owner_approval_creates_proposal_without_counting_pause(producer_boundary):
    """Owner-callback создаёт PAUSE-proposal и НЕ отмечает объявление запаузенным.

    Раньше `approve_pause` дёргал мутатор и при успехе писал решение + undo.
    Сейчас он отдаёт proposal_id, а мутация будет только после одобрения —
    значит writes «как будто пауза применена» здесь были бы ложью в отчётах.
    """
    meta = {"name": "Owner candidate", "reason": "слив"}

    with patch("agent.repositories.decisions_repo.save_decision") as mock_save, patch(
        "services.autopilot._write_auto_action"
    ) as mock_action, patch("services.autopilot.record_pause_undo") as mock_undo:
        from services.autopilot import approve_pause

        success, message = approve_pause("owner-1", meta)

    assert success is True, f"proposal должен создаться, got: {message}"
    plan = producer_boundary.assert_proposed(
        "owner-1",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    assert plan.targets[0].intended_payload["reason_code"] == "TELEGRAM_PAUSE_INTENT"
    assert message == meta["_proposal_id"], "наружу отдаётся id созданного предложения"
    mock_save.assert_not_called()
    mock_action.assert_not_called()
    mock_undo.assert_not_called()
    producer_boundary.assert_no_direct_provider_mutation()


def test_owner_approval_blocked_does_not_write_paused_or_undo(producer_boundary):
    """Producer отказал → success=False, причина наружу, никаких записей о паузе."""
    outcome = blocked_outcome("replacement_enqueued", workflow_id="workflow-owner")
    meta = {"name": "Owner candidate", "reason": "слив"}

    with patch(
        "services.action_producer_gateway.propose_pause",
        return_value=outcome,
    ) as mock_producer, patch(
        "agent.repositories.decisions_repo.save_decision"
    ) as mock_save, patch(
        "services.autopilot._write_auto_action"
    ) as mock_action, patch(
        "services.autopilot.record_pause_undo"
    ) as mock_undo:
        from services.autopilot import approve_pause

        success, message = approve_pause("owner-1", meta)

    # Боевой код импортирует propose_pause локально из модуля — патч по этому
    # имени перехватывает вызов, а патч алиаса execute_pause не перехватил бы.
    assert mock_producer.call_args.args == ("owner-1",)
    assert success is False
    assert "replacement_enqueued" in message
    mock_save.assert_not_called()
    mock_action.assert_not_called()
    mock_undo.assert_not_called()
    producer_boundary.assert_no_direct_provider_mutation()


def test_classic_proposal_is_not_counted_as_pause_or_undo(producer_boundary):
    """Классика: предложение создано, но пауза не «применена» ни в чём.

    paused пуст, решение PAUSED не сохранено, undo не зарегистрирован, дневной
    счётчик пауз не тронут — иначе отклонённое владельцем предложение съедало бы
    квоту и рисовало в отчёте отключение, которого не было.
    """
    ad = _classic_ad()
    cfg = {
        "enabled": True,
        "kill_switch": False,
        "mode": "active",
        "max_pauses_per_run": 1,
        "max_pauses_per_day": 6,
        "min_hours_between_runs": 3,
    }

    with patch("services.autopilot.get_autopilot_config", return_value=cfg), patch(
        "services.autopilot._load_state",
        return_value={"last_run_at": None, "manual_overrides": {}},
    ), patch("services.autopilot._save_state"), patch(
        "services.autopilot._get_ads_for_analysis", return_value=([ad], "live")
    ), patch(
        "services.autopilot._enrich_with_amo", return_value=([ad], True)
    ), patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), patch(
        "agent.analyzer.refresh_statuses_in_place"
    ), patch("agent.analyzer.apply_portfolio_decisions"), patch(
        "services.autopilot.get_active_overrides", return_value={}
    ), patch(
        "agent.repositories.decisions_repo.save_decision"
    ) as mock_save, patch(
        "services.autopilot.record_pause_undo"
    ) as mock_undo, patch(
        "services.autopilot._save_classic_daily_state"
    ) as mock_daily, patch(
        "services.notifications.send_telegram"
    ), patch("services.notifications.send_critical_alert"):
        from services.autopilot import run_autopilot

        result = run_autopilot(trigger="manual")

    producer_boundary.assert_proposed(
        ad["id"],
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    assert result["proposals"] == ["proposal-1"]
    assert result["paused"] == []
    mock_save.assert_not_called()
    mock_undo.assert_not_called()
    mock_daily.assert_not_called()
    producer_boundary.assert_no_direct_provider_mutation()


def test_live_proposal_is_not_counted_as_pause_or_undo(producer_boundary):
    """LIVE: то же, что в классике — предложение не равно применённой паузе."""
    ad_id = "live-1"
    local_ad = {
        "ad_id": ad_id,
        "ad_name": "Live candidate",
        "days_running": 10,
        "spend": 250.0,
        "leads": 2,
        "cpl": 125.0,
        "ctr": 0.3,
        "romi": 0.0,
        "qual_pct": 0.0,
        "payments": 0,
    }
    decision = {
        "ad_id": ad_id,
        "ad_name": "Live candidate",
        "action": "PAUSE",
        "score": 0,
        "reasons": ["confirmed waste"],
        "is_confirmed_waster": True,
    }
    cfg = {
        "enabled": True,
        "kill_switch": False,
        "max_pauses_per_run": 1,
        "max_pauses_per_day": 6,
        "min_days_protect": 5,
        "hold_enabled": False,
    }
    daily_state = {"date": date.today().isoformat(), "pauses_today": 0}

    with patch("services.autopilot.get_autopilot_config", return_value=cfg), patch(
        "services.autopilot._load_live_daily_state", return_value=daily_state
    ), patch(
        "services.shadow_report._fetch_ads_from_local_db", return_value=[local_ad]
    ), patch(
        "services.decision_policy.score_and_decide", return_value=[decision]
    ), patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), patch(
        "services.autopilot.get_active_overrides", return_value={}
    ), patch(
        "services.autopilot._fetch_candidate_fb_info",
        return_value={ad_id: {"adset_id": "100", "effective_status": "ACTIVE"}},
    ), patch(
        "agent.repositories.decisions_repo.save_decision"
    ) as mock_save, patch(
        "services.autopilot.record_pause_undo"
    ) as mock_undo, patch(
        "services.autopilot._save_live_daily_state"
    ) as mock_daily, patch(
        "services.autopilot._write_auto_action"
    ), patch("services.notifications.send_telegram"), patch(
        "services.notifications.send_critical_alert"
    ), patch("services.telegram_bot.send_with_buttons", return_value=True):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live(max_pauses=1)

    producer_boundary.assert_proposed(
        ad_id,
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    assert result["proposals"] == ["proposal-1"]
    assert result["paused"] == []
    mock_save.assert_not_called()
    mock_undo.assert_not_called()
    mock_daily.assert_not_called()
    producer_boundary.assert_no_direct_provider_mutation()


def test_live_last_active_without_replacement_gets_no_proposal(producer_boundary):
    """Replacement-aware edge: единственный ACTIVE в adset не предлагается вовсе.

    Пустой adset означает остановленный трафик города, поэтому propose_pause
    обязан отказать (LAST_EFFECTIVE_ACTIVE), а не переложить решение на владельца.
    """
    ad_id = "live-solo"
    local_ad = {
        "ad_id": ad_id,
        "ad_name": "Live solo",
        "days_running": 10,
        "spend": 250.0,
        # leads > 0: локальный ноль заставил бы автопилот идти в FB за lifetime-
        # evidence, а этот тест про guard, не про дозагрузку лидов.
        "leads": 1,
        "cpl": 250.0,
        "ctr": 0.2,
        "romi": 0.0,
        "qual_pct": 0.0,
        "payments": 0,
    }
    decision = {
        "ad_id": ad_id,
        "ad_name": "Live solo",
        "action": "PAUSE",
        "score": 0,
        "reasons": ["confirmed waste"],
        "is_confirmed_waster": True,
    }
    cfg = {
        "enabled": True,
        "kill_switch": False,
        "max_pauses_per_run": 1,
        "max_pauses_per_day": 6,
        "min_days_protect": 5,
        "hold_enabled": False,
    }
    daily_state = {"date": date.today().isoformat(), "pauses_today": 0}

    with patch("services.autopilot.get_autopilot_config", return_value=cfg), patch(
        "services.autopilot._load_live_daily_state", return_value=daily_state
    ), patch(
        "services.shadow_report._fetch_ads_from_local_db", return_value=[local_ad]
    ), patch(
        "services.decision_policy.score_and_decide", return_value=[decision]
    ), patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), patch(
        "services.autopilot.get_active_overrides", return_value={}
    ), patch(
        "services.autopilot._fetch_candidate_fb_info",
        return_value={ad_id: {"adset_id": "100", "effective_status": "ACTIVE"}},
    ), patch(
        "services.adset_pause_guard.fetch_pause_inventory",
        return_value=_inventory(ad_id),
    ), patch(
        "agent.repositories.decisions_repo.save_decision"
    ) as mock_save, patch(
        "services.autopilot.record_pause_undo"
    ) as mock_undo, patch(
        "services.autopilot._save_live_daily_state"
    ) as mock_daily, patch(
        "services.autopilot._write_auto_action"
    ), patch("services.notifications.send_telegram"), patch(
        "services.notifications.send_critical_alert"
    ), patch("services.telegram_bot.send_with_buttons", return_value=True):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live(max_pauses=1)

    assert producer_boundary.plans == [], "последний ACTIVE не должен попасть владельцу"
    assert result["paused"] == []
    assert result.get("proposals", []) == []
    assert any(ad_id in str(err) for err in result["errors"]), (
        f"отказ должен быть виден в errors, got: {result['errors']}"
    )
    mock_save.assert_not_called()
    mock_undo.assert_not_called()
    mock_daily.assert_not_called()
    producer_boundary.assert_no_direct_provider_mutation()


def test_owner_unknown_mapping_leaves_ad_active(producer_boundary):
    """Неизвестный маппинг замены → отказ producer'а, объявление остаётся ACTIVE."""
    outcome = blocked_outcome("replacement_mapping_unknown")

    with patch(
        "services.action_producer_gateway.propose_pause",
        return_value=outcome,
    ), patch("agent.repositories.decisions_repo.save_decision") as mock_save, patch(
        "services.autopilot._write_auto_action"
    ) as mock_action:
        from services.autopilot import approve_pause

        success, message = approve_pause("owner-unknown", {"name": "Unknown"})

    assert success is False
    assert "replacement_mapping_unknown" in message
    mock_save.assert_not_called()
    mock_action.assert_not_called()
    producer_boundary.assert_no_direct_provider_mutation()


def _live_cfg_autonomous(**extra) -> dict:
    cfg = {
        "enabled": True,
        "kill_switch": False,
        "max_pauses_per_run": 1,
        "max_pauses_per_day": 6,
        "min_days_protect": 5,
        "hold_enabled": False,
        "autonomous": {"pause_confirmed_wasters": True, "anomaly_guard": False},
    }
    cfg.update(extra)
    return cfg


def _live_local_ad(ad_id: str, name: str) -> dict:
    return {
        "ad_id": ad_id, "ad_name": name, "days_running": 10, "spend": 200.0,
        "leads": 20, "cpl": 10.0, "ctr": 0.3, "romi": 0.0, "qual_pct": 0.0,
        "payments": 0,
    }


def test_v2_candidate_survives_cap_slice_and_self_approves(producer_boundary):
    """Регрессия (блокер 1): v2-слив без тир-A флага сидел в классе «по
    score», cap-срез забивали шумные кандидаты, пересечение с автономным пулом
    получалось пустым МОЛЧА — автономия «включена», исполнений ноль."""
    noisy = {
        "ad_id": "noisy-1", "ad_name": "Noisy", "action": "PAUSE",
        "score": -10, "reasons": ["слабый score"],
    }
    v2 = {
        "ad_id": "v2-1", "ad_name": "V2 waster", "action": "PAUSE",
        "score": 5, "reasons": ["зрелый ноль"],
    }
    daily_state = {"date": date.today().isoformat(), "pauses_today": 0}
    with patch(
        "services.autopilot.get_autopilot_config", return_value=_live_cfg_autonomous()
    ), patch(
        "services.autopilot._load_live_daily_state", return_value=daily_state
    ), patch(
        "services.shadow_report._fetch_ads_from_local_db",
        return_value=[_live_local_ad("noisy-1", "Noisy"), _live_local_ad("v2-1", "V2 waster")],
    ), patch(
        "services.decision_policy.score_and_decide", return_value=[noisy, v2]
    ), patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), patch(
        "services.autopilot.get_active_overrides", return_value={}
    ), patch(
        "services.autopilot._fetch_candidate_fb_info",
        return_value={
            "noisy-1": {"adset_id": "100", "effective_status": "ACTIVE"},
            "v2-1": {"adset_id": "101", "effective_status": "ACTIVE"},
        },
    ), patch(
        "services.autonomous_pause.select_autonomous_candidates",
        return_value={"v2-1"},
    ), patch(
        "services.autonomous_pause.approve_autonomously", return_value=True
    ) as mock_approve, patch(
        "services.autonomous_pause.record_autonomous_pause"
    ), patch(
        "agent.repositories.decisions_repo.save_decision"
    ), patch("services.autopilot.record_pause_undo"), patch(
        "services.autopilot._save_live_daily_state"
    ), patch("services.autopilot._write_auto_action"), patch(
        "services.notifications.send_telegram"
    ), patch("services.notifications.send_critical_alert"), patch(
        "services.telegram_bot.send_with_buttons", return_value=True
    ):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live(max_pauses=1)

    autonomous_ids = [entry.get("ad_id") if isinstance(entry, dict) else entry for entry in result.get("autonomous", [])]
    assert autonomous_ids == ["v2-1"], (
        "v2-кандидат обязан пережить cap-срез и исполниться автономно; "
        f"получили autonomous={autonomous_ids}, paused={result.get('paused')}"
    )
    mock_approve.assert_called_once()


def test_deduplicated_autonomous_candidate_still_self_approves(producer_boundary):
    """Регрессия (блокер 2): у v2-слива карточка почти всегда уже висит
    с прошлых дней; ранний continue по deduplicated означал «самоодобрение
    никогда» — бот слал предложения вместо исполнения."""
    from tests.gateway_test_helpers import proposal_outcome

    v2 = {
        "ad_id": "v2-2", "ad_name": "V2 dedup", "action": "PAUSE",
        "score": 5, "reasons": ["зрелый ноль"],
    }
    daily_state = {"date": date.today().isoformat(), "pauses_today": 0}
    with patch(
        "services.autopilot.get_autopilot_config", return_value=_live_cfg_autonomous()
    ), patch(
        "services.autopilot._load_live_daily_state", return_value=daily_state
    ), patch(
        "services.shadow_report._fetch_ads_from_local_db",
        return_value=[_live_local_ad("v2-2", "V2 dedup")],
    ), patch(
        "services.decision_policy.score_and_decide", return_value=[v2]
    ), patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), patch(
        "services.autopilot.get_active_overrides", return_value={}
    ), patch(
        "services.autopilot._fetch_candidate_fb_info",
        return_value={"v2-2": {"adset_id": "100", "effective_status": "ACTIVE"}},
    ), patch(
        "services.action_producer_gateway.propose_pause",
        return_value=proposal_outcome("proposal-dedup", deduplicated=True),
    ), patch(
        "services.autonomous_pause.select_autonomous_candidates",
        return_value={"v2-2"},
    ), patch(
        "services.autonomous_pause.approve_autonomously", return_value=True
    ) as mock_approve, patch(
        "services.autonomous_pause.record_autonomous_pause"
    ), patch(
        "agent.repositories.decisions_repo.save_decision"
    ), patch("services.autopilot.record_pause_undo"), patch(
        "services.autopilot._save_live_daily_state"
    ), patch("services.autopilot._write_auto_action"), patch(
        "services.notifications.send_telegram"
    ), patch("services.notifications.send_critical_alert"), patch(
        "services.telegram_bot.send_with_buttons", return_value=True
    ):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live(max_pauses=1)

    autonomous_ids = [entry.get("ad_id") if isinstance(entry, dict) else entry for entry in result.get("autonomous", [])]
    assert autonomous_ids == ["v2-2"], (
        "деduplicated-кандидат автономии обязан самоодобрить СУЩЕСТВУЮЩЕЕ "
        f"предложение; получили autonomous={autonomous_ids}"
    )
    mock_approve.assert_called_once_with(
        "proposal-dedup",
        evidence=mock_approve.call_args.kwargs["evidence"],
    )


def test_fresh_ad_with_unknown_age_is_not_pause_candidate(producer_boundary):
    """Регрессия: у свежесозданного объявления в KB нет возраста, и
    дефолт days_running=999 превращал «неизвестно» в «очень старое» — карточки
    предлагали паузить рекламы, запущенные накануне. Неизвестный возраст =
    не кандидат (fail-closed)."""
    fresh = {
        "ad_id": "fresh-1", "ad_name": "Вчерашний запуск", "action": "PAUSE",
        "score": -5, "reasons": ["слабый score"],
    }
    local_ad = {
        "ad_id": "fresh-1", "ad_name": "Вчерашний запуск", "spend": 12.0,
        "leads": 1, "cpl": 12.0, "ctr": 0.4, "romi": 0.0, "qual_pct": 0.0,
        "payments": 0,
        # ни days_running, ни day_since_launch — как у свежесозданных в KB
    }
    daily_state = {"date": date.today().isoformat(), "pauses_today": 0}
    with patch(
        "services.autopilot.get_autopilot_config", return_value=_live_cfg_autonomous()
    ), patch(
        "services.autopilot._load_live_daily_state", return_value=daily_state
    ), patch(
        "services.shadow_report._fetch_ads_from_local_db", return_value=[local_ad]
    ), patch(
        "services.decision_policy.score_and_decide", return_value=[fresh]
    ), patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), patch(
        "services.autopilot.get_active_overrides", return_value={}
    ), patch(
        "services.autopilot._fetch_candidate_fb_info",
        return_value={"fresh-1": {"adset_id": "100", "effective_status": "ACTIVE"}},
    ), patch(
        "services.autonomous_pause.select_autonomous_candidates", return_value=set()
    ), patch(
        "agent.repositories.decisions_repo.save_decision"
    ), patch("services.autopilot.record_pause_undo"), patch(
        "services.autopilot._save_live_daily_state"
    ), patch("services.autopilot._write_auto_action"), patch(
        "services.notifications.send_telegram"
    ), patch("services.notifications.send_critical_alert"), patch(
        "services.telegram_bot.send_with_buttons", return_value=True
    ):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live(max_pauses=5)

    assert result.get("proposals", []) == [], (
        "свежая реклама без известного возраста не должна становиться "
        f"кандидатом: {result}"
    )


def test_slot_report_silent_except_errors(monkeypatch):
    """Решение владельца: днём тишина — слот-отчёты «предлагаю паузу»
    не шлются (серая зона уходит дайджестом 9:00); наружу прорываются только
    ошибки, потому что заблокированная пауза до утра стоила бы денег."""
    from unittest.mock import MagicMock

    from services import autopilot

    sent = MagicMock()
    monkeypatch.setattr("services.notifications.send_telegram", sent)

    autopilot._send_live_telegram_report(
        analyzed=100,
        trigger="cron",
        paused_details=[{"id": "1", "name": "X", "reason": "r", "score": 0}],
        errors=[],
        held=[],
        already_pending=2,
    )
    sent.assert_not_called()

    autopilot._send_live_telegram_report(
        analyzed=100,
        trigger="cron",
        paused_details=[],
        errors=["PAUSE_GUARD_BLOCKED: adset-1"],
        held=[],
    )
    sent.assert_called_once()
    assert "Ошибки" in sent.call_args.args[0]


def test_full_autonomy_executes_non_v2_candidates(producer_boundary):
    """Решение владельца: при pause_all_candidates=true бот сам исполняет
    и серую зону (не-v2 кандидатов), владелец читает итоги в сводке 9:00."""
    grey = {
        "ad_id": "grey-1", "ad_name": "Серая зона", "action": "PAUSE",
        "score": 3, "reasons": ["0 лидов за 3 дня"],
        "is_zero_leads_after_3d": True,
    }
    cfg = _live_cfg_autonomous()
    cfg["autonomous"]["pause_all_candidates"] = True
    daily_state = {"date": date.today().isoformat(), "pauses_today": 0}
    with patch(
        "services.autopilot.get_autopilot_config", return_value=cfg
    ), patch(
        "services.autopilot._load_live_daily_state", return_value=daily_state
    ), patch(
        "services.shadow_report._fetch_ads_from_local_db",
        return_value=[_live_local_ad("grey-1", "Серая зона")],
    ), patch(
        "services.decision_policy.score_and_decide", return_value=[grey]
    ), patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), patch(
        "services.autopilot.get_active_overrides", return_value={}
    ), patch(
        "services.autopilot._fetch_candidate_fb_info",
        return_value={"grey-1": {"adset_id": "100", "effective_status": "ACTIVE"}},
    ), patch(
        "services.autonomous_pause.select_autonomous_candidates", return_value=set()
    ), patch(
        "services.autonomous_pause.approve_autonomously", return_value=True
    ) as mock_approve, patch(
        "services.autonomous_pause.record_autonomous_pause"
    ), patch(
        "agent.repositories.decisions_repo.save_decision"
    ), patch("services.autopilot.record_pause_undo"), patch(
        "services.autopilot._save_live_daily_state"
    ), patch("services.autopilot._write_auto_action"), patch(
        "services.notifications.send_telegram"
    ), patch("services.notifications.send_critical_alert"), patch(
        "services.telegram_bot.send_with_buttons", return_value=True
    ):
        from services.autopilot import run_autopilot_live

        result = run_autopilot_live(max_pauses=5)

    autonomous_ids = [e.get("ad_id") if isinstance(e, dict) else e
                      for e in result.get("autonomous", [])]
    assert autonomous_ids == ["grey-1"], result
    mock_approve.assert_called_once()
