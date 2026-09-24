"""T6: replacement launch от full capacity до безопасной паузы старой рекламы."""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from datetime import datetime, timezone
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services import auto_launch
from services import creative_intelligence as ci
from services import launch_repository
from services import replacement_orchestrator as orchestrator
from services.approval_checker_models import (
    ActionOrigin,
    LaunchDestination,
)
from services.launch_checker import LaunchCheckBlocked, ProviderLaunchAuthorization
from tests.gateway_test_helpers import proposal_receipt


# _owner_launch_plan использует dataclasses.replace и order_index креативов,
# поэтому двойники manifest/creative — dataclass'ы, а не SimpleNamespace.
@dataclass(frozen=True)
class _Creative:
    order_index: int
    ad_name: str


@dataclass(frozen=True)
class _Trello:
    card_id: str
    card_content_sha256: str


@dataclass(frozen=True)
class _ActionManifest:
    manifest_id: str
    idempotency_key: str
    prepared_at: datetime
    origin: ActionOrigin
    destinations: tuple
    trello: _Trello
    media_manifest_sha256: str
    config_version_sha256: str
    staging_directory: str



@pytest.fixture(autouse=True)
def isolated_launch_repository(tmp_path):
    """Replacement integration никогда не использует production decisions.db."""
    ci.DB_PATH = None
    ci.init_kb(str(tmp_path / "replacement-launch.db"))
    yield
    ci.DB_PATH = None


def test_full_50_replacement_launch_then_exact_active_pauses_old(tmp_path, monkeypatch):
    """50/50 -> DELETE x2 -> CREATE -> exact ACTIVE -> PAUSE old."""
    monkeypatch.setattr(auto_launch, "_AUTO_LAUNCH_STATE_FILE", tmp_path / "auto-launch.json")
    monkeypatch.setattr(auto_launch, "_mark_card_done", lambda _card_id: None)
    monkeypatch.setattr(
        auto_launch,
        "_resolve_launch_account",
        lambda _campaign_type: ("offline", "123"),
    )
    monkeypatch.setattr(
        auto_launch,
        "_get_autopilot_config",
        lambda: {
            "replacement": {"enabled": True},
            "hypothesist": {"enabled": False},
        },
    )

    events: list[str] = []
    workflow = {
        "workflow_id": "workflow-1",
        "old_ad_id": "9001",
        "adset_id": "1001",
        "city": "CityA",
        "phase": "WAITING_SLOT",
    }
    launch_link: dict = {}
    available = {"slots": 0}
    def bind(*args, **_kwargs):
        events.append("BIND")
        launch_link.update(
            {
                "workflow_id": args[0],
                "launch_attempt_key": args[-1],
                "card_id": args[1],
                "city": args[3],
                "adset_id": args[6],
                "expected_ad_names_json": '["CityA | Replacement card"]',
                "media_manifest_sha256": args[-2],
            }
        )

    def ensure(workflow_id):
        assert workflow_id == "workflow-1"
        events.extend(["DELETE 7001", "DELETE 7002"])
        available["slots"] = 2
        workflow["phase"] = "WAITING_CARD"
        return orchestrator.ReplacementSlotOutcome(
            workflow_id="workflow-1",
            action="SLOTS_RELEASED",
            required_slots=2,
            available_before=0,
            available_after=2,
            deficit_before=2,
            deficit_after=0,
            deleted_ad_ids=("7001", "7002"),
            claim_ids=("claim-1", "claim-2"),
            reason=None,
        )

    def claim(workflow_id):
        events.append("CLAIM")
        workflow["phase"] = "LAUNCHING"
        return {**workflow, "launch_attempt_key": launch_link["launch_attempt_key"]}

    def record(workflow_id, attempt_key, city, ad_ids):
        assert workflow_id == "workflow-1"
        assert attempt_key == launch_link["launch_attempt_key"]
        assert city == "CityA"
        assert tuple(ad_ids) == ("8001",)
        events.append("RECORD 8001")
        workflow["phase"] = "WAITING_ACTIVE"

    def capacity(_adset_id):
        events.append(f"CAPACITY {available['slots']}")
        return {
            "ad_count": 50 - available["slots"],
            "max_ads": 50,
            "available": available["slots"],
            "stale_ads": [],
            "daily_budget": 45.0,
            "recommended": 3,
        }

    destination = LaunchDestination(
        city="CityA",
        account_id="123",
        adset_id="1001",
        adset_type="L2",
        current_daily_budget=Decimal("45"),
        currency="USD",
        capacity_available=0,
        hard_reserve_slots=1,
        creatives=(_Creative(order_index=0, ad_name="CityA | Replacement card"),),
        duplicate_signature="d" * 64,
    )
    prepared = SimpleNamespace(
        manifest_id="staged-replacement",
        trello=SimpleNamespace(card_id="card-1"),
        card_name="Replacement card",
        campaign_type="leadgen",
        media_manifest_sha256="e" * 64,
        destinations=(destination,),
    )
    refreshed_prepared: dict[str, object] = {}

    def refresh_staging(current, destinations):
        assert current is prepared
        assert destinations[0].replacement_workflow_id == "workflow-1"
        events.append("PERSIST workflow-1")
        refreshed = SimpleNamespace(**vars(prepared))
        refreshed.destinations = destinations
        refreshed_prepared["value"] = refreshed
        return refreshed

    def build_manifest(current, *, origin, idempotency_key, now):
        assert current is refreshed_prepared["value"]
        assert origin is ActionOrigin.AUTO_LAUNCH
        events.append("BUILD SEALED")
        return _ActionManifest(
            manifest_id="action-replacement",
            idempotency_key=idempotency_key,
            prepared_at=now,
            origin=origin,
            destinations=tuple(current.destinations),
            trello=_Trello(card_id="card-1", card_content_sha256="0c" * 32),
            media_manifest_sha256="0d" * 32,
            config_version_sha256="0e" * 32,
            staging_directory=str(tmp_path / "staged-replacement"),
        )

    def propose_owner_launch(_plan, now=None):
        # Producer доводит замену только до предложения владельцу: никакого
        # CREATE, никакой записи ad_id и никакого освобождения слота здесь нет.
        del now
        events.append("OWNER PROPOSAL")
        return proposal_receipt("proposal-replacement")

    rec = {
        "card_id": "card-1",
        "card_name": "Replacement card",
        "campaign_type": "leadgen",
        "cities": ["CityA"],
    }
    authorization = ProviderLaunchAuthorization("auth-replacement", "replacement-secret")
    request = SimpleNamespace(
        source="CRON",
        campaign_type="leadgen",
        actor="system:auto-launch",
        override_topic_veto=False,
        override_reason=None,
    )
    target = SimpleNamespace(
        city="CityA",
        ordinal=0,
        account_kind="offline",
        account_id="123",
        adset_id="1001",
        reserved_slots=1,
    )
    checked_plan = SimpleNamespace(
        check_id="check-replacement",
        card_id="card-1",
        card_name="Replacement card",
        request=request,
        media={"type": "image", "paths": ["sealed-by-staging"]},
        media_sha256="e" * 64,
        targets=(target,),
        expected_names_by_city={"CityA": ("CityA | Replacement card",)},
        authorization=authorization,
    )
    launch_repository.reserve_authorization(
        checked_plan,
        hashlib.sha256(authorization.secret.encode()).hexdigest(),
        datetime.now(timezone.utc),
    )
    state = auto_launch._load_auto_launch_state()
    with patch(
        "services.cleanup_repository.get_cleanup_status",
        return_value={"replacement_workflows": [workflow]},
    ), patch(
        "services.replacement_workflow.get_replacement_launch",
        side_effect=lambda _workflow_id: dict(launch_link) if launch_link else None,
    ), patch(
        "services.replacement_workflow.get_workflow",
        side_effect=lambda _workflow_id: dict(workflow),
    ), patch(
        "services.replacement_orchestrator.bind_replacement_card",
        side_effect=bind,
    ), patch(
        "services.replacement_orchestrator.ensure_slot_for_workflow",
        side_effect=ensure,
    ), patch(
        "services.replacement_orchestrator.claim_waiting_workflow_for_launch",
        side_effect=claim,
    ), patch(
        "services.replacement_orchestrator.record_workflow_launch_result",
        side_effect=record,
    ), patch(
        "agent.launcher.stage_launch", return_value=prepared
    ), patch(
        "services.launch_staging.refresh_staged_destinations", side_effect=refresh_staging
    ), patch(
        "agent.launcher.build_launch_manifest", side_effect=build_manifest
    ), patch(
        "agent.launcher.propose_action", side_effect=propose_owner_launch
    ), patch(
        "agent.launcher.release_staging"
    ), patch.object(
        auto_launch, "_mark_city_create_started"
    ) as mark_started, patch(
        "integrations.facebook.get_adset_capacity", side_effect=capacity
    ), patch(
        "integrations.facebook._get_launch_hard_reserve_slots", return_value=1
    ), patch(
        "services.adset_cleaner.get_cleaner_config",
        return_value={"hard_reserve_slots": 1},
    ):
        # Producer доводит замену до предложения владельцу и на этом упирается
        # в fail-closed выход: city_plan уже зафиксирован, а объявлений ещё нет,
        # поэтому reconciler честно требует ручной проверки вместо «успеха».
        # Главное для безопасности: слот освобождён, но ничего не создано.
        with pytest.raises(
            RuntimeError, match="partial CREATE требует ручной проверки"
        ):
            auto_launch._execute_launch(
                rec,
                state,
                "2026-07-21",
                checked_plan=checked_plan,
            )

    # Слот освобождён и запуск подготовлен, но объявление НЕ создано: последний
    # шаг — предложение владельцу. Освобождение слота (DELETE) обратимо только
    # через cleaner workflow и происходит до предложения, как и раньше.
    assert events == [
        "BIND",
        "DELETE 7001",
        "DELETE 7002",
        "CLAIM",
        "CAPACITY 2",
        "PERSIST workflow-1",
        "BUILD SEALED",
        "OWNER PROPOSAL",
    ]
    assert "CREATE 8001" not in events
    assert "RECORD 8001" not in events
    # CREATE даже не «начинался»: durable-пометка старта не ставится
    mark_started.assert_not_called()
    # Слоты освобождены удалением двух нулевых объявлений и НЕ израсходованы:
    # их займёт объявление, созданное после одобрения владельца.
    assert available["slots"] == 2

    class Storage:
        def get_workflow(self, workflow_id):
            assert workflow_id == "workflow-1"
            return dict(workflow)

        def get_replacement_launch(self, workflow_id):
            assert workflow_id == "workflow-1"
            return {
                **launch_link,
                "expected_ad_count": 1,
                "expected_ad_names": ("CityA | Replacement card",),
                "created_ad_ids": ("8001",),
            }

        def confirm_replacement_active(self, workflow_id, ad_ids, *, evidence):
            assert tuple(ad_ids) == ("8001",)
            assert evidence["ads"] == [
                {
                    "id": "8001",
                    "name": "CityA | Replacement card",
                    "adset_id": "1001",
                    "effective_status": "ACTIVE",
                }
            ]
            events.append("ACTIVE 8001")
            workflow["phase"] = "READY_TO_PAUSE"
            return True

        def mark_old_paused(self, workflow_id):
            assert workflow_id == "workflow-1"
            events.append("COMPLETED")
            workflow["phase"] = "COMPLETED"

        def mark_workflow_blocked(self, *_args):
            raise AssertionError("Exact ACTIVE workflow не должен блокироваться")

    old_contexts = {
        "9001": {
            "ad_id": "9001",
            "name": "Old",
            "adset_id": "1001",
            "configured_status": "ACTIVE",
            "effective_status": "ACTIVE",
        },
    }

    def propose_pause_of_old(_candidate, _prepared_at, _idempotency_key):
        # Пауза старого объявления тоже стала предложением владельцу.
        events.append("PROPOSE PAUSE 9001")
        return "proposal-pause-9001"

    # --- Вторая стадия: владелец одобрил, исполнитель создал объявление ---
    # В approval-first между стадиями стоит решение владельца, поэтому переход
    # workflow в WAITING_ACTIVE и запись созданного ad_id делает пост-approval
    # исполнитель, а не producer. Здесь этот шаг воспроизводится явно.
    workflow["phase"] = "WAITING_ACTIVE"
    events.append("RECORD 8001 (после одобрения)")

    storage = Storage()
    with patch.object(orchestrator, "_workflow_module", return_value=storage), patch.object(
        orchestrator,
        "_replacement_config",
        return_value={"enabled": True, "max_pending_hours": 48},
    ), patch.object(
        orchestrator,
        "_list_replacement_workflows",
        return_value=[dict(workflow)],
    ), patch.object(orchestrator, "_account_scope", return_value=nullcontext()), patch.object(
        orchestrator,
        "_assert_account",
        return_value=None,
    ), patch.object(
        orchestrator,
        "_verify_created_contexts",
        return_value=(
            "ACTIVE",
            ("8001",),
            ({
                "id": "8001",
                "name": "CityA | Replacement card",
                "adset_id": "1001",
                "effective_status": "ACTIVE",
            },),
            None,
        ),
    ), patch.object(
        orchestrator,
        "fetch_exact_ad_contexts",
        side_effect=lambda ids, **_kwargs: (
            {ad_id: old_contexts[ad_id] for ad_id in ids},
            None,
        ),
    ), patch.object(
        orchestrator,
        "_build_replacement_pause_candidate",
        return_value=(SimpleNamespace(), datetime.now(timezone.utc), "pause-key"),
    ), patch.object(
        orchestrator,
        "_execute_checked_replacement_pause",
        side_effect=propose_pause_of_old,
    ):
        verified = orchestrator.verify_and_complete_replacements()

    # Замена подтверждена как ACTIVE, но старое объявление НЕ погашено: создано
    # предложение владельцу. Пока он не одобрил, workflow остаётся в ожидании —
    # помечать его завершённым значило бы соврать, что старое уже на паузе.
    assert verified.completed_workflow_ids == ()
    assert verified.waiting_workflow_ids == ("workflow-1",), verified
    assert any(
        "owner_pause_proposal_pending:proposal-pause-9001" in error
        for error in verified.errors
    ), verified.errors
    assert events[-2:] == ["ACTIVE 8001", "PROPOSE PAUSE 9001"]
    assert "COMPLETED" not in events
    assert workflow["phase"] == "READY_TO_PAUSE"


def test_changed_media_bytes_before_binding_causes_zero_delete_and_create(
    tmp_path,
):
    """Те же ad names не разрешают slot mutation после подмены bytes."""
    from integrations import facebook

    launch_creative = facebook.launch_creative

    media_path = tmp_path / "same-name.jpg"
    media_path.write_bytes(b"version-one")
    media = {"type": "image", "paths": [str(media_path)]}
    prepare = MagicMock()
    create = MagicMock(return_value="8001")

    authorization = ProviderLaunchAuthorization("auth-media-drift", "media-secret")
    plan = SimpleNamespace(
        check_id="check-media-drift",
        card_id="card-media-drift",
        card_name="Same card",
        request=SimpleNamespace(
            source="CRON",
            campaign_type="leadgen",
            actor="system:auto-launch",
            override_topic_veto=False,
            override_reason=None,
        ),
        media_sha256=facebook.calculate_launch_media_sha256(media),
        targets=(
            SimpleNamespace(
                city="CityA",
                ordinal=0,
                account_kind="offline",
                account_id="123",
                adset_id="1001",
                reserved_slots=1,
            ),
        ),
        expected_names_by_city={"CityA": ("CityA | Same card",)},
        authorization=authorization,
    )
    launch_repository.reserve_authorization(
        plan,
        hashlib.sha256(authorization.secret.encode()).hexdigest(),
        datetime.now(timezone.utc),
    )

    def upload_and_replace(_path):
        media_path.write_bytes(b"version-two")
        return "image-hash"

    with patch(
        "agent.adset_discovery.get_adsets_dict",
        return_value={"CityA": {"L2": "1001"}},
    ), patch(
        "integrations.facebook.upload_image",
        side_effect=upload_and_replace,
    ), patch(
        "integrations.facebook.create_image_ad",
        create,
    ), patch(
        "integrations.facebook.get_adset_capacity",
    ) as capacity, patch(
        "integrations.facebook.get_fb_account_id",
        return_value="123",
    ), patch(
        "integrations.facebook.fetch_complete_account_ad_inventory",
        return_value=[],
    ):
        with pytest.raises(LaunchCheckBlocked) as error:
            launch_creative(
                card_name="Same card",
                adset_type="L2",
                media=media,
                body="Текст",
                campaign_type="leadgen",
                cities=["CityA"],
                prepare_city_cb=prepare,
                authorization=authorization,
            )

    assert error.value.code == "LEGACY_LAUNCH_BYPASS_FORBIDDEN"
    prepare.assert_not_called()
    capacity.assert_not_called()
    create.assert_not_called()
