"""Launcher никогда не рапортует запуск как успех — он только предлагает.

Раньше здесь проверялось, что launcher закрывает карточку Trello ровно по
terminal CONFIRMED из sealed gateway и не закрывает по FAILED/PARTIAL/UNKNOWN.
Провайдерская мутация из launcher удалена: CREATE выполняется отдельно и только
после одобрения владельца. Поэтому «частичный успех» в launcher стал невозможен
структурно, а инвариант усилился: успешного исхода у launcher нет вообще, есть
только созданное предложение (`pending_owner`) либо честная ошибка.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.launcher import launch_single, run
from services.approval_checker_models import ActionOrigin, ActionResult
from services.launch_checker import LaunchCheckBlocked, ProviderLaunchAuthorization
from tests.gateway_test_helpers import (
    install_provider_mutation_guard,
    proposal_receipt,
)


def _status() -> dict:
    return {
        "running": True,
        "current": "Test Card",
        "progress": 0,
        "total": 0,
        "step": "",
        "step_pct": None,
        "log": [],
    }


def _prepared() -> SimpleNamespace:
    return SimpleNamespace(manifest_id="staged-manifest", card_name="Точное имя")


# _owner_launch_plan использует dataclasses.replace, поэтому двойники manifest —
# именно dataclass'ы, а не SimpleNamespace.
@dataclass(frozen=True)
class _Creative:
    order_index: int
    ad_name: str


@dataclass(frozen=True)
class _Destination:
    city: str
    adset_type: str
    account_id: str
    adset_id: str
    creatives: tuple[_Creative, ...]


@dataclass(frozen=True)
class _Trello:
    card_id: str
    card_content_sha256: str


@dataclass(frozen=True)
class _Manifest:
    manifest_id: str
    destinations: tuple[_Destination, ...]
    trello: _Trello
    media_manifest_sha256: str
    config_version_sha256: str
    idempotency_key: str
    origin: ActionOrigin
    staging_directory: str


def _manifest(*, creative_counts: tuple[int, ...] = (1, 1, 1)) -> _Manifest:
    destinations = tuple(
        _Destination(
            city=city,
            adset_type="L1",
            account_id="10001",
            adset_id=f"adset-{city}",
            creatives=tuple(
                _Creative(order_index=index, ad_name=f"{city}-{index}")
                for index in range(count)
            ),
        )
        for city, count in zip(
            ("CityA", "CityB", "CityC"), creative_counts, strict=True
        )
    )
    return _Manifest(
        manifest_id="staged-manifest",
        destinations=destinations,
        trello=_Trello(card_id="card1", card_content_sha256="0c" * 32),
        media_manifest_sha256="0d" * 32,
        config_version_sha256="0e" * 32,
        idempotency_key="11111111-1111-4111-8111-111111111111",
        origin=ActionOrigin.WEB,
        staging_directory="/tmp/staged-manifest",
    )


def _launch(*, propose, monkeypatch) -> dict:
    """Прогоняет launch_single с подменённым producer'ом и запретом мутаций."""
    status = _status()
    guard = install_provider_mutation_guard(monkeypatch)
    monkeypatch.setattr("agent.launcher.stage_launch", lambda *_a, **_k: _prepared())
    monkeypatch.setattr(
        "agent.launcher.build_launch_manifest", lambda *_a, **_k: _manifest()
    )
    monkeypatch.setattr("agent.launcher.propose_action", propose)
    monkeypatch.setattr("agent.launcher.release_staging", MagicMock())
    mark_done = MagicMock()
    monkeypatch.setattr("integrations.trello.mark_card_done", mark_done)

    result = launch_single(
        "card1",
        "Недоверенное имя",
        "Недоверенное описание",
        status,
        cities=["CityA", "CityB", "CityC"],
        idempotency_key=str(uuid.uuid4()),
        origin=ActionOrigin.WEB,
    )
    guard.assert_untouched()
    mark_done.assert_not_called()
    return {"status": status, "result": result}


def test_launcher_success_is_pending_owner_and_never_marks_trello_done(monkeypatch):
    """Единственный положительный исход launcher — предложение владельцу.

    Ни «succeeded», ни закрытая карточка Trello: объявления ещё не существуют,
    и любое обратное сообщение владельцу было бы ложью.
    """
    outcome = _launch(
        propose=lambda _plan, now=None: proposal_receipt("proposal-1"),
        monkeypatch=monkeypatch,
    )
    status = outcome["status"]

    assert outcome["result"] == {"proposal_id": "proposal-1"}
    assert status["outcome"] == "pending_owner"
    assert status["outcome"] != "succeeded"
    assert status["proposal_id"] == "proposal-1"
    assert status["running"] is False
    assert any("владельцу" in line for line in status["log"])


def test_launcher_reports_no_created_ads_before_owner_decision(monkeypatch):
    """В результате launcher нет ни одного ad_id — их пока не существует.

    Прежде частичный набор created_ids трактовался как UNKNOWN и требовал
    reconcile. Теперь CREATE в launcher нет, значит и «частично созданных»
    объявлений быть не может: результат содержит только proposal_id.
    """
    outcome = _launch(
        propose=lambda _plan, now=None: proposal_receipt("proposal-2"),
        monkeypatch=monkeypatch,
    )

    assert set(outcome["result"]) == {"proposal_id"}
    assert "reason_codes" in outcome["status"]
    assert outcome["status"]["reason_codes"] == []
    assert outcome["status"].get("reconciliation_required") is not True


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("owner store down"), ValueError("plan rejected")],
    ids=["store-down", "plan-rejected"],
)
def test_producer_failure_is_reported_and_never_marks_trello_done(failure, monkeypatch):
    """Сбой создания предложения — честный failed с причиной, без Trello-эффектов.

    Пустой результат сам по себе не объясняет причину, поэтому caller обязан
    получить её из status: иначе крон отрапортует «нет proposal_id» вместо
    настоящей ошибки.
    """
    outcome = _launch(
        propose=MagicMock(side_effect=failure),
        monkeypatch=monkeypatch,
    )
    status = outcome["status"]

    assert outcome["result"] == {}
    assert status["outcome"] == "failed"
    assert str(failure) in status["error"]
    assert status["reconciliation_required"] is False
    assert "proposal_id" not in status


def test_launch_single_rejects_legacy_proof_before_staging():
    status = _status()
    proof = ProviderLaunchAuthorization("auth-launcher-test", "secret-launcher-test")

    with patch("agent.launcher.stage_launch") as stage, pytest.raises(LaunchCheckBlocked) as error:
        launch_single(
            "card1",
            "Test Card",
            "desc",
            status,
            authorization=proof,
            idempotency_key=str(uuid.uuid4()),
        )

    assert error.value.code == "LEGACY_LAUNCH_BYPASS_FORBIDDEN"
    stage.assert_not_called()


def test_launch_single_requires_canonical_uuid4_before_staging():
    status = _status()
    with patch("agent.launcher.stage_launch") as stage, pytest.raises(LaunchCheckBlocked) as error:
        launch_single("card1", "Test Card", "desc", status, idempotency_key="legacy-key")

    assert error.value.code == "INVALID_IDEMPOTENCY_KEY"
    stage.assert_not_called()


def test_agent_run_checker_block_has_no_staging_or_trello_done(monkeypatch):
    card = {"id": "card1", "name": "Верхняя карточка", "desc": "desc", "pos": 1.0, "labels": []}
    blocked = LaunchCheckBlocked("CHECKER_OBSERVE", ("Режим observe",), "check-run")
    guard = install_provider_mutation_guard(monkeypatch)
    mark_done = MagicMock()
    monkeypatch.setattr("integrations.trello.mark_card_done", mark_done)
    monkeypatch.setattr("agent.launcher.get_done_list_id", lambda: "done")
    monkeypatch.setattr("agent.launcher.get_unlaunched_cards", lambda _list_id: [card])
    stage = MagicMock()
    monkeypatch.setattr("agent.launcher.stage_launch", stage)
    propose = MagicMock()
    monkeypatch.setattr("agent.launcher.propose_action", propose)

    results = run(plan_provider=MagicMock(side_effect=blocked))

    assert results == [{
        "card": "Верхняя карточка",
        "status": "blocked",
        "check_id": "check-run",
        "reason_codes": ["CHECKER_OBSERVE"],
        "reasons": ["Режим observe"],
    }]
    stage.assert_not_called()
    propose.assert_not_called()
    mark_done.assert_not_called()
    guard.assert_untouched()


@pytest.mark.parametrize("receipt_state", ["DELIVERY_PENDING", "PENDING_OWNER"])
def test_no_receipt_state_yields_succeeded_outcome(receipt_state, monkeypatch):
    """Ни одно состояние квитанции не даёт launcher права сказать «succeeded».

    Страховка от возврата старого поведения: раньше DELIVERY-подобный CONFIRMED
    считался успехом. Теперь квитанция — только факт постановки задачи владельцу,
    поэтому исход всегда pending_owner, а карточка Trello остаётся открытой.
    """
    outcome = _launch(
        propose=lambda _plan, now=None: proposal_receipt(
            f"proposal-{receipt_state}", state=receipt_state
        ),
        monkeypatch=monkeypatch,
    )

    assert outcome["status"]["outcome"] == "pending_owner"
    assert outcome["status"]["proposal_state"] == receipt_state
    assert ActionResult.CONFIRMED.value.lower() not in str(
        outcome["status"]["outcome"]
    ).lower()
