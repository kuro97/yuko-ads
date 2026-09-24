"""T20: все PAUSE-ветки автопилота проходят через proposal-only границу.

Автопилот — approval-first producer. Он не имеет права ни мутировать Facebook,
ни писать бизнес-успех: максимум — создать PAUSE-proposal владельцу. Здесь
проверяются сама граница (`propose_pause`) и её fail-closed ветки, а полный
контур автопилота — в tests/test_autopilot.py.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import Mock

import pytest

from services import autopilot
from services.action_producer_gateway import (
    ProducerActionError,
    propose_pause,
    propose_unpause,
)
from services.approval_checker_models import ActionOrigin
from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import (
    blocked_outcome,
    init_action_db,
    install_proposal_recorder,
    proposal_outcome,
)


ROOT = Path(__file__).resolve().parents[1]


def test_autopilot_does_not_import_or_call_raw_pause_boundaries() -> None:
    """Автопилот не должен видеть ни одного прямого мутатора."""
    tree = ast.parse((ROOT / "services/autopilot.py").read_text(encoding="utf-8"))
    forbidden = {
        "pause_ad",
        "unpause_ad",
        "safe_pause_or_enqueue_replacement",
        "set_adset_budget",
        # Исполнительная граница тоже вне досягаемости producer'а
        "execute_action",
        "execute_action_batch",
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert imported.isdisjoint(forbidden)
    assert called.isdisjoint(forbidden)
    assert "propose_pause" in imported


def test_manual_pause_blocked_producer_has_zero_side_effects(monkeypatch) -> None:
    """Отказ producer'а не превращается в бизнес-успех.

    Legacy Telegram-кнопка «применить» больше не паузит — она ставит задачу
    владельцу. Если предложение создать не удалось, ни решение, ни запись в
    auto_actions писать нельзя: иначе дашборд покажет паузу, которой не было.
    """
    propose = Mock(return_value=blocked_outcome("LAST_EFFECTIVE_ACTIVE"))
    save = Mock()
    write = Mock()
    monkeypatch.setattr("services.action_producer_gateway.propose_pause", propose)
    monkeypatch.setattr("agent.repositories.decisions_repo.save_decision", save)
    monkeypatch.setattr(autopilot, "_write_auto_action", write)

    ok, message = autopilot.approve_pause(
        "ad-1", {"name": "Реклама", "reason": "дорого"}, command_id="click-1"
    )

    assert ok is False
    assert "LAST_EFFECTIVE_ACTIVE" in message
    save.assert_not_called()
    write.assert_not_called()


def test_manual_pause_replay_reuses_same_scope_and_writes_no_success(monkeypatch) -> None:
    """Повторный клик по той же кнопке не создаёт второе действие.

    Scope предложения детерминирован по command_id и ad_id, поэтому дедупликация
    на стороне owner-store сводит повтор к тому же proposal. Бизнес-успех при
    этом не пишется ни разу — владелец ещё не решил.
    """
    propose = Mock(return_value=proposal_outcome("proposal-1"))
    save = Mock()
    write = Mock()
    monkeypatch.setattr("services.action_producer_gateway.propose_pause", propose)
    monkeypatch.setattr("agent.repositories.decisions_repo.save_decision", save)
    monkeypatch.setattr(autopilot, "_write_auto_action", write)

    first_meta = {"name": "Реклама", "reason": "дорого"}
    replay_meta = {"name": "Реклама", "reason": "дорого"}
    assert autopilot.approve_pause("ad-1", first_meta, command_id="click-1")[0]
    assert autopilot.approve_pause("ad-1", replay_meta, command_id="click-1")[0]

    assert propose.call_count == 2
    assert {call.kwargs["scope"] for call in propose.call_args_list} == {
        "autopilot-owner:click-1:ad-1"
    }
    assert {call.kwargs["origin"] for call in propose.call_args_list} == {
        ActionOrigin.AUTOPILOT_MANUAL
    }
    # Оба вызова вернули один и тот же proposal — второго действия нет
    assert first_meta["_proposal_id"] == replay_meta["_proposal_id"] == "proposal-1"
    save.assert_not_called()
    write.assert_not_called()


def test_pause_requires_fresh_complete_full_inventory_before_proposal(
    tmp_path, monkeypatch
) -> None:
    """Неполный live inventory запрещает даже создание предложения.

    Предложение — это то, что владелец увидит и одобрит. Если полнота
    инвентаря не доказана, предложить нечего: одобрение по неполным данным
    может оставить adset пустым.
    """
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts",
        lambda *_args, **_kwargs: (
            {
                "ad-1": {
                    "adset_id": "adset-1",
                    "name": "Реклама",
                    "effective_status": "ACTIVE",
                }
            },
            None,
        ),
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory",
        lambda *_args, **_kwargs: {
            "adset-1": {"complete": False, "error": "PAGINATION_INCOMPLETE"}
        },
    )
    recorded = install_proposal_recorder(monkeypatch, tmp_path)

    with pytest.raises(ProducerActionError, match="PAGINATION_INCOMPLETE"):
        propose_pause(
            "ad-1",
            origin=ActionOrigin.AUTOPILOT_CLASSIC,
            scope="autopilot:test:ad-1",
            reason_code="AUTOPILOT_CLASSIC",
        )

    assert recorded.plans == []
    recorded.assert_no_direct_provider_mutation()


def test_last_active_pause_is_blocked_and_creates_no_proposal(
    tmp_path, monkeypatch
) -> None:
    """Последний effective ACTIVE не предлагается вообще.

    Раньше эта ситуация делегировалась replacement-оркестратору. Теперь producer
    отказывает fail-closed до создания предложения: владельцу нельзя предлагать
    действие, которое опустошит adset.
    """
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts",
        lambda *_args, **_kwargs: (
            {
                "ad-1": {
                    "adset_id": "adset-1",
                    "name": "Последняя реклама",
                    "effective_status": "ACTIVE",
                }
            },
            None,
        ),
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory",
        lambda *_args, **_kwargs: {
            "adset-1": {
                "complete": True,
                "active_ids": {"ad-1"},
                "state_sha256": "a" * 64,
            }
        },
    )
    replacement = Mock()
    monkeypatch.setattr(
        "services.replacement_orchestrator.safe_pause_or_enqueue_replacement",
        replacement,
    )
    recorded = install_proposal_recorder(monkeypatch, tmp_path)

    with pytest.raises(ProducerActionError, match="LAST_EFFECTIVE_ACTIVE"):
        propose_pause(
            "ad-1",
            origin=ActionOrigin.AUTOPILOT_CLASSIC,
            scope="autopilot:test:last-active",
            reason_code="AUTOPILOT_CLASSIC",
        )

    assert recorded.plans == []
    replacement.assert_not_called()
    recorded.assert_no_direct_provider_mutation()


def test_pause_with_proven_replacement_creates_exact_proposal(
    tmp_path, monkeypatch
) -> None:
    """Happy path: есть доказанная ACTIVE-замена → корректное предложение.

    В payload обязаны быть и ожидаемый переход ACTIVE→PAUSED, и sibling'и,
    чтобы execution boundary после одобрения могла перепроверить обстановку.
    """
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts",
        lambda *_args, **_kwargs: (
            {
                "ad-1": {
                    "adset_id": "adset-1",
                    "name": "Дорогая реклама",
                    "effective_status": "ACTIVE",
                }
            },
            None,
        ),
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory",
        lambda *_args, **_kwargs: {
            "adset-1": {
                "complete": True,
                "active_ids": {"ad-1", "ad-replacement"},
                "state_sha256": "b" * 64,
            }
        },
    )
    recorded = install_proposal_recorder(monkeypatch, tmp_path)

    outcome = propose_pause(
        "ad-1",
        origin=ActionOrigin.AUTOPILOT_CLASSIC,
        scope="autopilot:test:happy",
        reason_code="AUTOPILOT_CLASSIC",
    )

    assert outcome.receipt is not None
    # Producer-успех никогда не означает исполнение
    assert outcome.confirmed is False
    plan = recorded.assert_proposed(
        "ad-1",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="PAUSE_AD",
    )
    payload = plan.targets[0].intended_payload
    assert payload["expected_before_status"] == "ACTIVE"
    assert payload["expected_after_status"] == "PAUSED"
    assert payload["reason_code"] == "AUTOPILOT_CLASSIC"
    assert tuple(payload["sibling_active_ids"]) == ("ad-replacement",)
    recorded.assert_no_direct_provider_mutation()


def test_unpause_requires_fresh_complete_inventory_before_proposal(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts",
        lambda *_args, **_kwargs: (
            {
                "ad-1": {
                    "adset_id": "adset-1",
                    "name": "Реклама",
                    "effective_status": "PAUSED",
                }
            },
            None,
        ),
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory",
        lambda *_args, **_kwargs: {"adset-1": {"complete": False}},
    )
    recorded = install_proposal_recorder(monkeypatch, tmp_path)

    with pytest.raises(ProducerActionError, match="INVENTORY_INCOMPLETE"):
        propose_unpause(
            "ad-1",
            origin=ActionOrigin.WEB,
            scope="web:test:ad-1",
        )

    assert recorded.plans == []
    recorded.assert_no_direct_provider_mutation()


def test_proposal_persistence_requires_initialized_action_db(monkeypatch) -> None:
    """Без durable-хранилища предложение не «создаётся молча».

    Idempotency-биндинг живёт в БД действий: если её нет, producer обязан упасть,
    а не вернуть владельцу несохранённое предложение.
    """
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts",
        lambda *_args, **_kwargs: (
            {
                "ad-1": {
                    "adset_id": "adset-1",
                    "name": "Реклама",
                    "effective_status": "ACTIVE",
                }
            },
            None,
        ),
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory",
        lambda *_args, **_kwargs: {
            "adset-1": {
                "complete": True,
                "active_ids": {"ad-1", "ad-2"},
                "state_sha256": "c" * 64,
            }
        },
    )
    import agent.database as database

    monkeypatch.setattr(database, "DB_PATH", None)

    with pytest.raises(RuntimeError, match="БД не инициализирована"):
        propose_pause(
            "ad-1",
            origin=ActionOrigin.AUTOPILOT_CLASSIC,
            scope="autopilot:test:no-db",
            reason_code="AUTOPILOT_CLASSIC",
        )


@pytest.fixture(autouse=True)
def _restore_action_db(tmp_path):
    """Возвращает БД действий в рабочее состояние после теста без неё."""
    yield
    init_action_db(tmp_path)
