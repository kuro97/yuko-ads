"""T14: public launcher не может обойти approval-контур.

Раньше launcher сам выполнял запуск через sealed Approval Gateway
(`execute_action`) и по CONFIRMED закрывал карточку Trello и писал историю.
Теперь launcher — producer: он готовит exact-план и создаёт LAUNCH-proposal
владельцу (`propose_action`), а провайдерский CREATE выполняется отдельно и
только после одобрения. Поэтому «успех» launcher'а = созданное предложение,
а бизнес-эффекты (Trello done, история) launcher не пишет вообще.
"""

from __future__ import annotations

import ast
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import launcher
from services.approval_checker_models import ActionOrigin, ActionResult
from services.launch_checker import LaunchCheckBlocked, ProviderLaunchAuthorization
from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import proposal_receipt


def _status() -> dict:
    return {
        "running": True,
        "current": "Карточка",
        "progress": 0,
        "total": 0,
        "step": "",
        "step_pct": None,
        "log": [],
    }


def _prepared() -> SimpleNamespace:
    return SimpleNamespace(
        manifest_id="staged-manifest",
        card_name="Точное имя Trello",
    )


# Минимальные dataclass-двойники staged manifest: _owner_launch_plan использует
# dataclasses.replace, поэтому SimpleNamespace тут не подходит. Полный контракт
# LaunchManifest проверяется в tests/test_approval_models.py.
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


def _manifest(*, creative_counts: tuple[int, ...] = (1, 2)) -> _Manifest:
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
        for city, count in zip(("CityA", "CityB"), creative_counts, strict=True)
    )
    return _Manifest(
        manifest_id="staged-manifest",
        destinations=destinations,
        trello=_Trello(card_id="card-1", card_content_sha256="0c" * 32),
        media_manifest_sha256="0d" * 32,
        config_version_sha256="0e" * 32,
        idempotency_key="11111111-1111-4111-8111-111111111111",
        origin=ActionOrigin.WEB,
        staging_directory="/tmp/staged-manifest",
    )


def test_launch_single_requires_idempotency_before_staging_or_proposal():
    status = _status()
    with patch("agent.launcher.stage_launch") as stage, patch(
        "agent.launcher.propose_action"
    ) as propose, pytest.raises(LaunchCheckBlocked) as error:
        launcher.launch_single("card-1", "имя", "описание", status)

    assert error.value.code == "INVALID_IDEMPOTENCY_KEY"
    assert status["outcome"] == "blocked"
    stage.assert_not_called()
    propose.assert_not_called()


@pytest.mark.parametrize(
    "legacy_kwargs",
    [
        {"authorization": ProviderLaunchAuthorization("auth", "secret")},
        {"prepared_media": {"type": "video", "paths": ["/tmp/raw.mp4"]}},
        {"prepare_city_cb": lambda *_args: None},
        {"city_success_cb": lambda *_args: None},
    ],
)
def test_legacy_raw_launch_arguments_fail_closed(legacy_kwargs):
    status = _status()
    with patch("agent.launcher.stage_launch") as stage, patch(
        "agent.launcher.propose_action"
    ) as propose, pytest.raises(LaunchCheckBlocked) as error:
        launcher.launch_single(
            "card-1",
            "имя",
            "описание",
            status,
            idempotency_key=str(uuid.uuid4()),
            **legacy_kwargs,
        )

    assert error.value.code == "LEGACY_LAUNCH_BYPASS_FORBIDDEN"
    stage.assert_not_called()
    propose.assert_not_called()


def test_launch_single_creates_owner_proposal_without_side_effects():
    """Запуск заканчивается предложением владельцу, а не созданием объявлений.

    Проверяем и содержимое предложения (LAUNCH, по одному CREATE_AD-claim на
    каждый креатив, точное имя карточки), и отсутствие бизнес-эффектов: Trello
    не закрывается, история не пишется, staging остаётся закреплён за planом.

    Про имя: раньше в durable-запись истории шло проверенное имя из Trello
    (`prepared.card_name`), а не аргумент caller'а. Теперь единственный
    owner-facing текст — summary предложения, и в нём должно быть ровно то же
    проверенное имя: владелец одобряет по достоверным данным, иначе caller
    может подписать одобрение под чужим названием.
    """
    status = _status()
    prepared = _prepared()
    manifest = _manifest()
    receipt = proposal_receipt("proposal-launch-1", state="PENDING_OWNER")
    captured: list[object] = []

    with patch("agent.launcher.stage_launch", return_value=prepared) as stage, patch(
        "agent.launcher.build_launch_manifest", return_value=manifest
    ) as build, patch(
        "agent.launcher.propose_action",
        side_effect=lambda plan, now=None: captured.append(plan) or receipt,
    ) as propose, patch("agent.launcher.release_staging") as release:
        result = launcher.launch_single(
            "card-1",
            "недоверенное имя",
            "недоверенное описание",
            status,
            tenant_id="tenant-1",
            campaign_type="leadgen",
            cities=["CityA", "CityB"],
            idempotency_key=str(uuid.uuid4()),
            origin=ActionOrigin.WEB,
        )

    assert result == {"proposal_id": "proposal-launch-1"}
    assert status["outcome"] == "pending_owner"
    assert status["proposal_id"] == "proposal-launch-1"
    assert status["proposal_state"] == "PENDING_OWNER"
    assert status["running"] is False
    assert status["total"] == 2

    source = stage.call_args.args[0]
    assert source.card_id == "card-1"
    assert source.requested_cities == ("CityA", "CityB")
    assert source.campaign_type == "leadgen"
    assert build.call_args.args[0] is prepared
    assert build.call_args.kwargs["origin"] is ActionOrigin.WEB
    propose.assert_called_once()

    plan = captured[0]
    assert plan.proposal_kind is ProposalKind.LAUNCH
    assert plan.origin is ProposalOrigin.WEB
    # По одному exact-claim на каждый креатив каждого города (1 + 2)
    assert [target.action_kind for target in plan.targets] == ["CREATE_AD"] * 3
    assert {target.subject_id for target in plan.targets} == {"card-1"}
    assert [target.city for target in plan.targets] == ["CityA", "CityB", "CityB"]
    # Имя из Trello, а не недоверенный ввод caller'а
    assert "недоверенное имя" not in plan.summary, (
        "владелец видит имя от caller'а вместо проверенного имени карточки: "
        f"{plan.summary}"
    )
    assert prepared.card_name in plan.summary

    # Staging принадлежит immutable proposal — launcher его не освобождает
    release.assert_not_called()


def test_launch_single_never_marks_trello_or_history():
    """Launcher не имеет права закрывать карточку и писать историю запуска.

    До approval-first эти эффекты выполнялись сразу после CONFIRMED-мутации.
    Теперь мутации в launcher нет, значит и бизнес-эффектов быть не может —
    иначе владелец увидел бы «запущено» по ещё не одобренному плану.
    """
    source = Path(launcher.__file__).read_text(encoding="utf-8")

    assert "mark_card_done" not in source
    assert "save_history_entry" not in source

    status = _status()
    with patch("agent.launcher.stage_launch", return_value=_prepared()), patch(
        "agent.launcher.build_launch_manifest", return_value=_manifest()
    ), patch(
        "agent.launcher.propose_action", return_value=proposal_receipt("proposal-2")
    ), patch("integrations.trello.mark_card_done") as mark_done, patch(
        "agent.repositories.history_repo.save_history_entry"
    ) as save_history:
        launcher.launch_single(
            "card-1",
            "имя",
            "описание",
            status,
            tenant_id="tenant-1",
            idempotency_key=str(uuid.uuid4()),
        )

    mark_done.assert_not_called()
    save_history.assert_not_called()


def test_failed_proposal_releases_staging_and_reports_reason():
    """Если предложение создать не удалось — staging освобождается, причина видна.

    Иначе staged media навсегда осталась бы занятой, а caller получил бы пустой
    результат без объяснения (и отрапортовал бы «нет proposal_id»).
    """
    status = _status()
    with patch("agent.launcher.stage_launch", return_value=_prepared()), patch(
        "agent.launcher.build_launch_manifest", return_value=_manifest()
    ), patch(
        "agent.launcher.propose_action", side_effect=RuntimeError("owner store down")
    ), patch("agent.launcher.release_staging") as release:
        returned = launcher.launch_single(
            "card-1",
            "имя",
            "описание",
            status,
            tenant_id="tenant-1",
            idempotency_key=str(uuid.uuid4()),
        )

    assert returned == {}
    assert status["outcome"] == "failed"
    assert "owner store down" in status["error"]
    assert status["reconciliation_required"] is False
    assert "proposal_id" not in status
    release.assert_called_once_with(
        "staged-manifest",
        terminal_result=ActionResult.FAILED,
    )


def test_run_stops_after_first_nonconfirmed_card():
    cards = [
        {"id": "card-1", "name": "Первая", "desc": "", "pos": 1.0},
        {"id": "card-2", "name": "Вторая", "desc": "", "pos": 2.0},
    ]

    def nonconfirmed(*_args, **kwargs):
        status = _args[3]
        status["outcome"] = "unknown"
        status["operation_id"] = "operation-1"
        status["running"] = False
        return {}

    with patch("agent.launcher.get_done_list_id", return_value="ready"), patch(
        "agent.launcher.get_unlaunched_cards", return_value=cards
    ), patch("agent.launcher.launch_single", side_effect=nonconfirmed) as launch:
        results = launcher.run(idempotency_key_factory=lambda: str(uuid.uuid4()))

    assert len(results) == 1
    assert results[0]["status"] == "unknown"
    launch.assert_called_once()


def test_default_run_bonus_veto_blocks_before_staging_and_proposal():
    card = {
        "id": "bonus-card",
        "name": "Акция с бонусом",
        "desc": "",
        "pos": 1.0,
    }
    with patch("agent.launcher.get_done_list_id", return_value="ready"), patch(
        "agent.launcher.get_unlaunched_cards", return_value=[card]
    ), patch("agent.launcher.stage_launch") as stage, patch(
        "agent.launcher.propose_action"
    ) as propose, patch("agent.launcher.launch_single") as launch:
        results = launcher.run()

    assert results[0]["status"] == "blocked"
    assert results[0]["reason_codes"] == ["TOPIC_VETO"]
    launch.assert_not_called()
    stage.assert_not_called()
    propose.assert_not_called()


def test_launcher_ast_has_no_direct_facebook_create_boundary():
    source_path = Path(launcher.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "integrations.facebook" not in imported_names
    assert "launch_creative" not in imported_names | called_names
    assert "create_ad_from_existing_creative" not in imported_names | called_names
    # Launcher — producer: он предлагает план, а не исполняет мутацию
    assert "execute_action" not in imported_names | called_names
    assert "execute_action_batch" not in imported_names | called_names
    assert "propose_action" in called_names
    assert "stage_launch" in called_names


def test_owner_launch_plan_uses_shared_proposal_ttl():
    """Запуски живут по общему TTL предложений.

    Свои 24 часа здесь уже убивали одобренные запуски пачками PROPOSAL_EXPIRED:
    при суточном цикле дайджеста владелец видел карточку, когда жить ей
    оставалось меньше пары часов.
    """
    from datetime import datetime, timezone

    from services.action_producer_gateway import PROPOSAL_TTL

    now = datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc)
    plan = launcher._owner_launch_plan(
        _manifest(), card_name="Точное имя Trello", now=now
    )
    assert plan.valid_until == now + PROPOSAL_TTL
