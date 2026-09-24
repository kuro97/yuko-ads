"""Карточка предложения владельцу: цифры решения и дедуп живых предложений.

Жалоба владельца: карточки приходят пустыми — «Поставить на паузу Город |
Креатив А / 2» и три кнопки, без единой цифры. Здесь
проверяется, что карточка собирается при СОЗДАНИИ предложения (а не при
подготовке доставки, где два разных пути перетирают друг друга) и что на одну
рекламу не копится очередь из десятков живых предложений.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.action_producer_gateway import propose_pause, propose_scale, propose_unpause
from services.approval_checker_models import ActionOrigin
from services.owner_action_models import (
    EvidenceRecord,
    ProposalKind,
    ProposalOrigin,
    ProposedActionPlan,
    ProposedTarget,
    canonical_sha256,
)
from services.owner_action_repository import OwnerActionRepository
from services.owner_proposal_card import (
    DecisionContext,
    render_pause_card,
    render_scale_card,
)
from tests.gateway_test_helpers import init_action_db, install_proposal_recorder


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _install_live_ad(
    monkeypatch,
    *,
    ad_id: str = "ad-1",
    name: str = "CityA | Креатив А ver3",
    active_ids: set[str] | None = None,
) -> None:
    """Живой FB-контекст: реклама ACTIVE в адсете с ещё девятью соседями."""
    siblings = active_ids or {ad_id, *(f"other-{i}" for i in range(9))}
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts",
        lambda *_a, **_k: (
            {
                ad_id: {
                    "ad_id": ad_id,
                    "adset_id": "adset-77",
                    "name": name,
                    "configured_status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
            },
            None,
        ),
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory",
        lambda *_a, **_k: {
            "adset-77": {
                "adset_id": "adset-77",
                "complete": True,
                "active_ids": siblings,
                "state_sha256": "a" * 64,
                "error": None,
            }
        },
    )
    monkeypatch.setattr(
        "services.action_producer_gateway._scope_labels",
        lambda _adset_id: ("CityA", "L2"),
    )
    monkeypatch.setattr("config.FB_ACCOUNT_ID", "1234567890", raising=False)


def _live_pause_proposal(db_path: str, *, ad_id: str = "ad-1") -> str:
    """Уже висящее у владельца PAUSE-предложение на ту же рекламу."""
    payload = {"operation": "PAUSE_AD", "ad_id": ad_id}
    evidence_payload = {"effective_status": "ACTIVE"}
    plan = ProposedActionPlan(
        proposal_kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        idempotency_key="11111111-1111-4111-8111-111111111111",
        source_ref=f"autopilot-live:run-1:{ad_id}",
        actor="action_producer_gateway",
        summary="⏸ Предлагаю паузу",
        targets=(
            ProposedTarget(
                claim_id=f"claim-{ad_id}",
                ordinal=0,
                action_kind="PAUSE_AD",
                account_id="1234567890",
                adset_id="adset-77",
                subject_id=ad_id,
                city="CityA",
                language="L2",
                intended_payload=payload,
                intended_payload_sha256=canonical_sha256(payload),
            ),
        ),
        evidence=(
            EvidenceRecord(
                evidence_kind="PRODUCER_LIVE_INVENTORY",
                source_system="FACEBOOK",
                subject_id=ad_id,
                observed_at=NOW,
                complete=True,
                payload=evidence_payload,
                payload_sha256=canonical_sha256(evidence_payload),
            ),
        ),
        config_version_sha256="c" * 64,
        valid_until=NOW + timedelta(hours=24),
        staged_media_root=None,
    )
    return OwnerActionRepository(db_path).propose_action(plan, now=NOW).proposal_id


# ---------------------------------------------------------------------------
# Содержимое карточки
# ---------------------------------------------------------------------------


def test_pause_card_carries_city_direction_adset_metrics_reason_and_remainder(
    tmp_path, monkeypatch
) -> None:
    """Всё, что владелец требовал в ТЗ, должно быть в самой карточке."""
    _install_live_ad(monkeypatch)
    recorded = install_proposal_recorder(monkeypatch, tmp_path)

    propose_pause(
        "ad-1",
        origin=ActionOrigin.AUTOPILOT_LIVE,
        scope="autopilot-live:run-1:ad-1",
        reason_code="AUTOPILOT_LIVE",
        decision=DecisionContext(
            spend_usd=500.0,
            leads=30,
            cpl_usd=16.67,
            qual_pct=3.3,
            payments=0,
            business_reason="30 лидов, оплат 0 при расходе $500 — деньги уходят, продаж нет",
        ),
        now=NOW,
    )

    card = recorded.plans[0].summary
    assert card.splitlines()[0] == "⏸ Предлагаю паузу"
    assert "CityA · L2 · adset adset-77" in card
    assert "CityA | Креатив А ver3" in card
    assert "💸 $500 · 30 лидов · CPL $16.7" in card
    assert "👥 квал 1 (3%) · оплат 0" in card
    assert "📉 Причина: 30 лидов, оплат 0 при расходе $500" in card
    # 10 ACTIVE в адсете минус эта реклама.
    assert "✅ После паузы в адсете останется 9 активных" in card
    recorded.assert_no_direct_provider_mutation()


def test_card_without_decision_context_degrades_without_crashing(
    tmp_path, monkeypatch
) -> None:
    """Старые продюсеры контекст не передают — карточка обязана уцелеть."""
    _install_live_ad(monkeypatch)
    recorded = install_proposal_recorder(monkeypatch, tmp_path)

    propose_pause(
        "ad-1",
        origin=ActionOrigin.AUTOPILOT_CLASSIC,
        scope="autopilot-classic:run-1:ad-1",
        reason_code="AUTOPILOT_CLASSIC",
        now=NOW,
    )

    card = recorded.plans[0].summary
    assert card.splitlines()[0] == "⏸ Предлагаю паузу"
    assert "CityA · L2 · adset adset-77" in card
    assert "CityA | Креатив А ver3" in card
    assert "✅ После паузы в адсете останется 9 активных" in card
    # Метрик нет — и ложных нулей тоже нет.
    assert "💸" not in card
    assert "📉" not in card


def test_unknown_inventory_renders_honest_line_instead_of_invented_number() -> None:
    """FB не ответил — говорим об этом прямо, а не выдумываем число.

    В ``propose_pause`` неполный инвентарь остаётся fail-closed отказом (нельзя
    предлагать паузу, не зная, не последняя ли это ACTIVE), поэтому честная
    строка проверяется на самом рендере — её используют ветки без инвентаря.
    """
    card = render_pause_card(
        ad_id="ad-1",
        ad_name="Реклама",
        adset_id="adset-77",
        city="CityB",
        language="L1",
        remaining_active=None,
        decision=DecisionContext(spend_usd=10.0, leads=1, cpl_usd=10.0),
    )

    assert "ℹ️ Сколько реклам останется активными — не знаю (FB не ответил)" in card
    assert "CityB · L1 · adset adset-77" in card


def test_scale_card_shows_budget_move_and_winner_economics(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "services.action_producer_gateway._scope_labels",
        lambda _adset_id: ("CityC", "L1"),
    )
    monkeypatch.setattr("config.FB_ACCOUNT_ID", "1234567890", raising=False)
    recorded = install_proposal_recorder(monkeypatch, tmp_path)

    propose_scale(
        {
            "adset_id": "adset-77",
            "adset_name": "CityC | PRODA",
            "ad_id": "ad-9",
            "ad_name": "Победитель ver2",
            "current_budget_usd": "40",
            "new_budget_usd": "48",
        },
        scope="budget-scaler:2026-07-27:adset-77:ad-9:40:48",
        decision=DecisionContext(romi_pct=240.0, payments=6, leads=30),
        now=NOW,
    )

    card = recorded.plans[0].summary
    assert card.splitlines()[0] == "📈 Предлагаю поднять бюджет"
    assert "CityC · L1 · adset adset-77" in card
    assert "💰 Бюджет $40 → $48 в день" in card
    assert "ROMI 240%" in card


def test_scale_card_without_context_still_states_the_budget_move() -> None:
    card = render_scale_card(
        adset_id="adset-77",
        adset_name=None,
        ad_name=None,
        city=None,
        language=None,
        current_budget_usd=40,
        target_budget_usd=48,
    )

    assert "💰 Бюджет $40 → $48 в день" in card
    assert "adset adset-77" in card


# ---------------------------------------------------------------------------
# Дедуп живых предложений на создании
# ---------------------------------------------------------------------------


def test_second_pause_for_same_ad_reuses_live_proposal(tmp_path, monkeypatch) -> None:
    """Крон каждые 15 минут не имеет права плодить новое предложение.

    Scope продюсера содержит run_id прогона, поэтому дедуп по ключу
    идемпотентности здесь бессилен — дедуп идёт по бизнес-ключу (реклама + вид
    действия).
    """
    init_action_db(tmp_path)
    from agent import database

    existing_id = _live_pause_proposal(str(database.DB_PATH))
    _install_live_ad(monkeypatch)
    recorded = install_proposal_recorder(monkeypatch)

    outcome = propose_pause(
        "ad-1",
        origin=ActionOrigin.AUTOPILOT_LIVE,
        scope="autopilot-live:run-2:ad-1",
        reason_code="AUTOPILOT_LIVE",
        now=NOW + timedelta(minutes=15),
    )

    assert outcome.receipt is not None
    assert outcome.receipt.proposal_id == existing_id
    assert outcome.receipt.deduplicated is True
    assert outcome.reason == "LIVE_PROPOSAL_EXISTS"
    # Второго плана не создано вовсе.
    assert recorded.plans == []


def test_three_runs_on_one_ad_leave_exactly_one_live_proposal(
    tmp_path, monkeypatch
) -> None:
    init_action_db(tmp_path)
    _install_live_ad(monkeypatch)
    from agent import database

    ids = set()
    for run_no in range(3):
        outcome = propose_pause(
            "ad-1",
            origin=ActionOrigin.AUTOPILOT_LIVE,
            scope=f"autopilot-live:run-{run_no}:ad-1",
            reason_code="AUTOPILOT_LIVE",
            decision=DecisionContext(spend_usd=100.0 + run_no, leads=5),
            now=NOW + timedelta(minutes=15 * run_no),
        )
        assert outcome.receipt is not None
        ids.add(outcome.receipt.proposal_id)

    assert len(ids) == 1
    import sqlite3

    connection = sqlite3.connect(str(database.DB_PATH))
    try:
        live = connection.execute(
            """
            SELECT COUNT(*) FROM owner_action_lifecycle l
            JOIN owner_action_proposal_targets t USING (proposal_id)
            WHERE t.subject_id = 'ad-1' AND t.action_kind = 'PAUSE_AD'
              AND l.state IN ('DELIVERY_PENDING','PENDING_OWNER','POSTPONED')
            """
        ).fetchone()[0]
    finally:
        connection.close()
    assert live == 1


def test_decision_context_does_not_change_idempotency_key(
    tmp_path, monkeypatch
) -> None:
    """Разный контекст решения при том же intent — дедуп, а не конфликт ключа.

    ``intended_payload`` контекста не содержит, поэтому scope остаётся связан с
    тем же ``intent_sha256``: повтор не поднимает IDEMPOTENCY_PAYLOAD_CONFLICT.
    """
    init_action_db(tmp_path)
    _install_live_ad(monkeypatch)
    from services.action_producer_gateway import _proposal_key

    payload = {
        "schema_version": 1,
        "operation": "PAUSE_AD",
        "ad_id": "ad-1",
        "adset_id": "adset-77",
    }
    first = _proposal_key("autopilot-live:same-scope:ad-1", payload, None)
    second = _proposal_key("autopilot-live:same-scope:ad-1", payload, None)

    assert first == second

    # И полный путь: второе предложение с ДРУГИМ контекстом решения приходит
    # дедупликацией, а не исключением.
    outcome_one = propose_pause(
        "ad-1",
        origin=ActionOrigin.AUTOPILOT_LIVE,
        scope="autopilot-live:run-a:ad-1",
        reason_code="AUTOPILOT_LIVE",
        decision=DecisionContext(spend_usd=100.0, business_reason="первая причина"),
        now=NOW,
    )
    outcome_two = propose_pause(
        "ad-1",
        origin=ActionOrigin.AUTOPILOT_LIVE,
        scope="autopilot-live:run-b:ad-1",
        reason_code="AUTOPILOT_LIVE",
        decision=DecisionContext(spend_usd=999.0, business_reason="другая причина"),
        now=NOW + timedelta(minutes=15),
    )

    assert outcome_one.receipt is not None and outcome_two.receipt is not None
    assert outcome_one.receipt.proposal_id == outcome_two.receipt.proposal_id
    assert outcome_two.receipt.deduplicated is True


def test_unpause_dedup_is_independent_from_pause(tmp_path, monkeypatch) -> None:
    """PAUSE и UNPAUSE — разные действия: живая пауза не глушит возврат."""
    init_action_db(tmp_path)
    from agent import database

    _live_pause_proposal(str(database.DB_PATH))
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts",
        lambda *_a, **_k: (
            {
                "ad-1": {
                    "ad_id": "ad-1",
                    "adset_id": "adset-77",
                    "name": "Реклама",
                    "configured_status": "PAUSED",
                    "effective_status": "PAUSED",
                }
            },
            None,
        ),
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory",
        lambda *_a, **_k: {
            "adset-77": {
                "adset_id": "adset-77",
                "complete": True,
                "active_ids": {"other-1", "other-2"},
                "state_sha256": "b" * 64,
                "error": None,
            }
        },
    )
    monkeypatch.setattr(
        "services.action_producer_gateway._scope_labels",
        lambda _adset_id: ("CityA", "L2"),
    )
    monkeypatch.setattr("config.FB_ACCOUNT_ID", "1234567890", raising=False)
    recorded = install_proposal_recorder(monkeypatch)

    propose_unpause(
        "ad-1",
        origin=ActionOrigin.AUTOPILOT_MANUAL,
        scope="telegram:unpause:ad-1",
        now=NOW,
    )

    assert len(recorded.plans) == 1
    card = recorded.plans[0].summary
    assert card.splitlines()[0] == "▶️ Предлагаю вернуть рекламу"
    assert "✅ После возврата в адсете будет 3 активных" in card


def test_dedup_failure_does_not_block_producer(tmp_path, monkeypatch) -> None:
    """Сбой чтения дедупа не имеет права отменить предложение владельцу."""
    init_action_db(tmp_path)
    _install_live_ad(monkeypatch)

    def _boom(**_kwargs):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(
        "services.owner_action_repository.find_live_proposal_for_subject", _boom
    )
    recorded = install_proposal_recorder(monkeypatch)

    propose_pause(
        "ad-1",
        origin=ActionOrigin.AUTOPILOT_LIVE,
        scope="autopilot-live:run-1:ad-1",
        reason_code="AUTOPILOT_LIVE",
        now=NOW,
    )

    assert len(recorded.plans) == 1


@pytest.mark.parametrize(
    "value,expected",
    [(1, "1 лид"), (2, "2 лида"), (5, "5 лидов")],
)
def test_card_uses_russian_lead_pluralization(value: int, expected: str) -> None:
    card = render_pause_card(
        ad_id="ad-1",
        ad_name="Реклама",
        adset_id="adset-77",
        city="CityA",
        language="L2",
        remaining_active=3,
        decision=DecisionContext(spend_usd=100.0, leads=value, cpl_usd=5.0),
    )

    assert expected in card
