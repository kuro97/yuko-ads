"""T5: replacement-aware pause orchestration без live mutations."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import replacement_orchestrator as orchestrator
from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import (
    install_proposal_recorder,
    install_provider_mutation_guard,
)


def _workflow(
    phase: str = "WAITING_SLOT",
    *,
    workflow_id: str = "workflow-1",
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "workflow_id": workflow_id,
        "old_ad_id": "old-1",
        "adset_id": "100",
        "city": "CityA",
        "phase": phase,
        "replacement_created_at": now,
        "replacement_active_at": now,
    }


def _link(*, created: tuple[str, ...] = ("new-1",)) -> dict:
    return {
        "workflow_id": "workflow-1",
        "launch_attempt_key": "attempt-1",
        "card_id": "card-1",
        "card_name": "Карточка",
        "city": "CityA",
        "account_kind": "offline",
        "account_id": "account-1",
        "adset_id": "100",
        "expected_ad_count": len(created),
        "expected_ad_names": tuple(f"Новая {index}" for index, _ in enumerate(created, 1)),
        "created_ad_ids": created,
        "media_manifest_sha256": "manifest-sha",
    }


class _Storage:
    def __init__(self, workflow: dict | None = None, link: dict | None = None):
        self.workflow = workflow
        self.link = link
        self.enqueue = Mock(return_value="workflow-1")
        self.link_call = Mock()
        self.record = Mock()
        self.mark_old = Mock()
        self.block = Mock()

    def get_workflow(self, workflow_id: str):
        assert workflow_id == "workflow-1"
        return None if self.workflow is None else dict(self.workflow)

    def get_replacement_launch(self, workflow_id: str):
        assert workflow_id == "workflow-1"
        return None if self.link is None else dict(self.link)

    def enqueue_replacement(self, **kwargs):
        return self.enqueue(**kwargs)

    def link_replacement_launch(self, **kwargs):
        return self.link_call(**kwargs)

    def record_replacement_created(self, *args):
        return self.record(*args)

    def mark_old_paused(self, workflow_id: str):
        self.mark_old(workflow_id)

    def mark_workflow_blocked(self, workflow_id: str, reason: str):
        self.block(workflow_id, reason)


@pytest.fixture
def unlocked():
    """Сохраняет сигнатуры старых тестов без возврата removed mutation lock."""


def test_spare_active_requires_checked_gateway_and_is_not_enqueued(monkeypatch, unlocked):
    storage = _Storage()
    monkeypatch.setattr(
        orchestrator,
        "_validated_pause_snapshot",
        Mock(return_value=("100", {"ad_id": "old-1"}, {"old-1", "spare-1"})),
    )
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)

    result = orchestrator.safe_pause_or_enqueue_replacement("old-1", "test")

    assert result.action == "BLOCKED"
    assert result.reason == "pause_requires_approval_gateway"
    assert result.workflow_id is None
    storage.enqueue.assert_not_called()


def test_last_active_with_replacement_disabled_stays_active(monkeypatch, unlocked):
    monkeypatch.setattr(
        orchestrator,
        "_validated_pause_snapshot",
        Mock(return_value=("100", {"ad_id": "old-1"}, {"old-1"})),
    )
    monkeypatch.setattr(orchestrator, "_replacement_config", lambda: {"enabled": False})

    result = orchestrator.safe_pause_or_enqueue_replacement("old-1", "test")

    assert result.action == "BLOCKED"
    assert result.reason == "last_active_no_replacement"


def test_unknown_mapping_blocks_enqueue_and_pause(monkeypatch, unlocked):
    storage = _Storage()
    monkeypatch.setattr(
        orchestrator,
        "_validated_pause_snapshot",
        Mock(return_value=("100", {"ad_id": "old-1"}, {"old-1"})),
    )
    monkeypatch.setattr(orchestrator, "_replacement_config", lambda: {"enabled": True})
    monkeypatch.setattr(orchestrator, "resolve_replacement_mapping", lambda _adset_id: None)
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)

    result = orchestrator.safe_pause_or_enqueue_replacement("old-1", "test")

    assert result.reason == "replacement_mapping_unknown"
    storage.enqueue.assert_not_called()


def test_last_active_enqueue_is_idempotent_through_storage(monkeypatch, unlocked):
    storage = _Storage()
    monkeypatch.setattr(
        orchestrator,
        "_validated_pause_snapshot",
        Mock(return_value=("100", {"ad_id": "old-1"}, {"old-1"})),
    )
    monkeypatch.setattr(orchestrator, "_replacement_config", lambda: {"enabled": True})
    monkeypatch.setattr(
        orchestrator,
        "resolve_replacement_mapping",
        lambda _adset_id: orchestrator.ReplacementMapping("CityA", "L1", "offline"),
    )
    monkeypatch.setattr(
        orchestrator,
        "fetch_exact_ad_contexts",
        lambda _ids: (
            {
                "old-1": {
                    "ad_id": "old-1",
                    "adset_id": "100",
                    "name": "Старая",
                    "configured_status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
            },
            None,
        ),
    )
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)

    first = orchestrator.safe_pause_or_enqueue_replacement("old-1", "test")
    second = orchestrator.safe_pause_or_enqueue_replacement("old-1", "test")

    assert first == second
    assert first.action == "REPLACEMENT_ENQUEUED"
    assert storage.enqueue.call_count == 2
    assert all(call.kwargs["adset_id"] == "100" for call in storage.enqueue.call_args_list)


def test_spare_active_never_enters_local_replacement_or_pause(monkeypatch, unlocked):
    storage = _Storage()
    monkeypatch.setattr(
        orchestrator,
        "_validated_pause_snapshot",
        Mock(return_value=("100", {"ad_id": "old-1"}, {"old-1", "spare-1"})),
    )
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)

    result = orchestrator.safe_pause_or_enqueue_replacement("old-1", "race")

    assert result.action == "BLOCKED"
    assert result.reason == "pause_requires_approval_gateway"
    storage.enqueue.assert_not_called()


def test_incomplete_inventory_fails_closed_before_any_mutation(monkeypatch):
    storage = _Storage()
    monkeypatch.setattr(
        orchestrator,
        "_validated_pause_snapshot",
        Mock(return_value=(None, None, None)),
    )
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)
    result = orchestrator.safe_pause_or_enqueue_replacement("old-1", "test")

    assert result.action == "BLOCKED"
    storage.enqueue.assert_not_called()


def test_resolve_mapping_rejects_fallback_and_ambiguous_discovery(monkeypatch):
    monkeypatch.setattr("services.fb_token_provider.get_active_account", lambda: None)
    monkeypatch.setattr(
        "agent.adset_discovery.discover_adsets",
        lambda force_refresh: {"source": "fallback", "leadgen": {"CityA": {"L1": "100"}}},
    )
    assert orchestrator.resolve_replacement_mapping("100") is None

    monkeypatch.setattr(
        "agent.adset_discovery.discover_adsets",
        lambda force_refresh: {
            "source": "fb_api",
            "leadgen": {"CityA": {"L1": "100"}, "CityB": {"L1": "100"}},
        },
    )
    assert orchestrator.resolve_replacement_mapping("100") is None


def test_bind_replacement_card_delegates_full_immutable_scope(monkeypatch):
    storage = _Storage()
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)

    orchestrator.bind_replacement_card(
        "workflow-1",
        "card-1",
        "Карточка",
        "CityA",
        "offline",
        "account-1",
        "100",
        ("Новая 1",),
        1,
        "manifest-sha",
        "attempt-1",
    )

    assert storage.link_call.call_args.kwargs["media_manifest_sha256"] == "manifest-sha"
    assert storage.link_call.call_args.kwargs["expected_ad_names"] == ("Новая 1",)


def test_ensure_slot_never_calls_cleaner_before_binding(monkeypatch):
    storage = _Storage(_workflow(), None)
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)
    cleaner = Mock()
    monkeypatch.setattr("services.adset_cleaner.run_cleaner", cleaner)

    result = orchestrator.ensure_slot_for_workflow("workflow-1")

    assert result.action == "BLOCKED"
    assert result.reason == "bound_workflow_required"
    cleaner.assert_not_called()


@pytest.mark.parametrize(
    "cleaner_result,expected_action",
    [
        (
            {
                "action": "NO_DELETE_REQUIRED",
                "capacity_before": {"100": 3},
                "capacity_after": {"100": 3},
                "deficit_before": 0,
                "deficit_after": 0,
                "deleted": [],
                "claim_ids": [],
                "errors": [],
            },
            "NO_DELETE_REQUIRED",
        ),
        (
            {
                "action": "SLOTS_RELEASED",
                "capacity_before": {"100": 0},
                "capacity_after": {"100": 2},
                "deficit_before": 2,
                "deficit_after": 0,
                "deleted": ["zero-1", "zero-2"],
                "claim_ids": ["claim-1", "claim-2"],
                "errors": [],
            },
            "SLOTS_RELEASED",
        ),
    ],
)
def test_ensure_slot_preserves_reserve_and_exact_single_claim_results(
    monkeypatch, cleaner_result, expected_action
):
    storage = _Storage(_workflow(), _link())
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)
    monkeypatch.setattr(
        "services.adset_cleaner.get_cleaner_config",
        lambda: {"hard_reserve_slots": 1},
    )
    cleaner = Mock(return_value=cleaner_result)
    monkeypatch.setattr("services.adset_cleaner.run_cleaner", cleaner)

    result = orchestrator.ensure_slot_for_workflow("workflow-1")

    assert result.action == expected_action
    assert result.required_slots == 2
    assert result.deleted_ad_ids == tuple(cleaner_result["deleted"])
    assert result.claim_ids == tuple(cleaner_result["claim_ids"])
    cleaner.assert_called_once_with(mode="active", workflow_id="workflow-1")


def test_ensure_slot_unknown_cleaner_action_is_blocked(monkeypatch):
    storage = _Storage(_workflow(), _link())
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)
    monkeypatch.setattr(
        "services.adset_cleaner.get_cleaner_config",
        lambda: {"hard_reserve_slots": 1},
    )
    monkeypatch.setattr(
        "services.adset_cleaner.run_cleaner",
        lambda **_kwargs: {
            "action": "PAUSED",
            "capacity_before": {"100": 0},
            "capacity_after": {"100": 0},
            "deficit_before": 2,
            "deficit_after": 2,
            "deleted": [],
            "claim_ids": [],
        },
    )

    result = orchestrator.ensure_slot_for_workflow("workflow-1")

    assert result.action == "BLOCKED"
    assert result.reason == "cleaner_result_fail_closed"


def test_claim_requires_exact_dependency_and_never_uses_adset_claim(monkeypatch, unlocked):
    storage = _Storage(_workflow("WAITING_CARD"), _link())
    storage.claim_waiting_workflow = Mock()
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)

    with pytest.raises(orchestrator.ReplacementDependencyError, match="claim_workflow_for_launch"):
        orchestrator.claim_waiting_workflow_for_launch("workflow-1")

    storage.claim_waiting_workflow.assert_not_called()


def test_claim_uses_exact_workflow_cas_and_rejects_mismatch(monkeypatch, unlocked):
    storage = _Storage(_workflow("WAITING_CARD"), _link())
    storage.claim_workflow_for_launch = Mock(
        return_value={"workflow_id": "workflow-1", "phase": "LAUNCHING"}
    )
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)

    claimed = orchestrator.claim_waiting_workflow_for_launch("workflow-1")

    assert claimed["workflow_id"] == "workflow-1"
    storage.claim_workflow_for_launch.assert_called_once_with("workflow-1")

    storage.claim_workflow_for_launch.return_value = {"workflow_id": "workflow-other"}
    with pytest.raises(orchestrator.ReplacementDependencyError, match="mismatch"):
        orchestrator.claim_waiting_workflow_for_launch("workflow-1")


def test_record_launch_result_checks_attempt_and_city_before_storage(monkeypatch):
    storage = _Storage(_workflow("LAUNCHING"), _link())
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)

    with pytest.raises(ValueError, match="binding_mismatch"):
        orchestrator.record_workflow_launch_result(
            "workflow-1", "wrong-attempt", "CityA", ("8001",)
        )
    storage.record.assert_not_called()

    orchestrator.record_workflow_launch_result(
        "workflow-1", "attempt-1", "CityA", ("8001",)
    )
    storage.record.assert_called_once_with("workflow-1", ("8001",), "attempt-1")


def _prepare_verify(monkeypatch, storage: _Storage, *, config: dict | None = None):
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)
    monkeypatch.setattr(
        orchestrator,
        "_replacement_config",
        lambda: config or {"enabled": True, "max_pending_hours": 48},
    )
    monkeypatch.setattr(
        orchestrator,
        "_list_replacement_workflows",
        lambda: [dict(storage.workflow)],
    )
    monkeypatch.setattr(orchestrator, "_account_scope", lambda _link: nullcontext())
    monkeypatch.setattr(orchestrator, "_assert_account", lambda _link: None)


def test_verify_all_exact_active_then_pauses_and_completes(monkeypatch):
    storage = _Storage(_workflow("WAITING_ACTIVE"), _link(created=("8001", "8002")))

    def confirm(workflow_id, replacement_ad_ids, *, evidence):
        assert workflow_id == "workflow-1"
        assert replacement_ad_ids == ("8001", "8002")
        assert [ad["id"] for ad in evidence["ads"]] == ["8001", "8002"]
        storage.workflow["phase"] = "READY_TO_PAUSE"
        return True

    storage.confirm_replacement_active = Mock(side_effect=confirm)
    _prepare_verify(monkeypatch, storage)
    contexts = {
        "8001": {
            "ad_id": "8001", "name": "Новая 1", "adset_id": "100",
            "configured_status": "ACTIVE", "effective_status": "ACTIVE",
        },
        "8002": {
            "ad_id": "8002", "name": "Новая 2", "adset_id": "100",
            "configured_status": "ACTIVE", "effective_status": "ACTIVE",
        },
        "old-1": {
            "ad_id": "old-1", "name": "Старая", "adset_id": "100",
            "configured_status": "ACTIVE", "effective_status": "ACTIVE",
        },
    }
    monkeypatch.setattr(
        orchestrator,
        "fetch_exact_ad_contexts",
        lambda ids, **_kwargs: ({ad_id: contexts[ad_id] for ad_id in ids}, None),
    )
    candidate = object()
    build_candidate = Mock(
        return_value=(candidate, datetime.now(timezone.utc), "approval-key")
    )
    # Теперь эта граница отдаёт proposal_id (str), а не результат исполнения:
    # старую рекламу гасит владелец, оркестратор только просит.
    checked_pause = Mock(return_value="proposal-77")
    monkeypatch.setattr(
        orchestrator,
        "_build_replacement_pause_candidate",
        build_candidate,
    )
    monkeypatch.setattr(
        orchestrator,
        "_execute_checked_replacement_pause",
        checked_pause,
    )
    mutation_guard = install_provider_mutation_guard(monkeypatch)

    result = orchestrator.verify_and_complete_replacements()

    assert result.checked == 1
    assert result.ready_workflow_ids == ("workflow-1",)
    storage.confirm_replacement_active.assert_called_once()
    build_candidate.assert_called_once()
    checked_pause.assert_called_once()

    # Замена подтверждена ACTIVE, но workflow НЕ завершён: старое объявление
    # ещё живо, и его выключение ждёт одобрения владельца. Завершить workflow
    # здесь означало бы записать «старая погашена», пока она тратит бюджет.
    assert result.completed_workflow_ids == ()
    assert result.waiting_workflow_ids == ("workflow-1",)
    assert result.errors == ("workflow-1:owner_pause_proposal_pending:proposal-77",)
    storage.mark_old.assert_not_called()
    mutation_guard.assert_untouched()


def test_verify_pending_replacement_never_pauses_old(monkeypatch):
    storage = _Storage(_workflow("WAITING_ACTIVE"), _link())
    storage.confirm_replacement_active = Mock()
    _prepare_verify(monkeypatch, storage)
    monkeypatch.setattr(
        orchestrator,
        "fetch_exact_ad_contexts",
        lambda ids, **_kwargs: (
            {
                "new-1": {
                    "ad_id": "new-1", "name": "Новая 1", "adset_id": "100",
                    "configured_status": "ACTIVE", "effective_status": "PENDING_REVIEW",
                }
            },
            None,
        ),
    )
    result = orchestrator.verify_and_complete_replacements()

    assert result.waiting_workflow_ids == ("workflow-1",)
    storage.mark_old.assert_not_called()


@pytest.mark.parametrize(
    "created_context,reason",
    [
        (
            {
                "ad_id": "new-1", "name": "Новая 1", "adset_id": "other",
                "configured_status": "ACTIVE", "effective_status": "ACTIVE",
            },
            "replacement_wrong_adset",
        ),
        (
            {
                "ad_id": "new-1", "name": "Другое имя", "adset_id": "100",
                "configured_status": "ACTIVE", "effective_status": "ACTIVE",
            },
            "replacement_names_mismatch",
        ),
    ],
)
def test_verify_wrong_scope_or_name_blocks_before_pause(
    monkeypatch, created_context, reason
):
    created_context = {**created_context, "ad_id": "8001"}
    storage = _Storage(_workflow("WAITING_ACTIVE"), _link(created=("8001",)))
    storage.confirm_replacement_active = Mock()
    _prepare_verify(monkeypatch, storage)
    monkeypatch.setattr(
        orchestrator,
        "fetch_exact_ad_contexts",
        lambda ids, **_kwargs: ({"8001": created_context}, None),
    )
    result = orchestrator.verify_and_complete_replacements()

    assert result.blocked_workflow_ids == ("workflow-1",)
    storage.block.assert_called_once_with("workflow-1", reason)


def test_verify_missing_exact_confirm_dependency_is_fail_closed(monkeypatch):
    storage = _Storage(_workflow("WAITING_ACTIVE"), _link())
    _prepare_verify(monkeypatch, storage)
    monkeypatch.setattr(
        orchestrator,
        "_verify_created_contexts",
        lambda _workflow, _link: (
            "ACTIVE",
            ("new-1",),
            ({"id": "new-1", "name": "Новая 1", "adset_id": "100", "effective_status": "ACTIVE"},),
            None,
        ),
    )
    result = orchestrator.verify_and_complete_replacements()

    assert result.waiting_workflow_ids == ("workflow-1",)
    assert "confirm_replacement_active_unavailable" in result.errors[0]


def test_verify_old_already_paused_requires_matching_confirmed_operation(monkeypatch):
    storage = _Storage(_workflow("READY_TO_PAUSE"), _link())
    _prepare_verify(monkeypatch, storage)
    monkeypatch.setattr(
        orchestrator,
        "_verify_created_contexts",
        lambda _workflow, _link: (
            "ACTIVE",
            ("new-1",),
            ({"id": "new-1", "name": "Новая 1", "adset_id": "100", "effective_status": "ACTIVE"},),
            None,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "fetch_exact_ad_contexts",
        lambda ids, **_kwargs: (
            {
                "old-1": {
                    "ad_id": "old-1", "adset_id": "100", "name": "Старая",
                    "configured_status": "PAUSED", "effective_status": "PAUSED",
                }
            },
            None,
        ),
    )
    mutation_guard = install_provider_mutation_guard(monkeypatch)

    result = orchestrator.verify_and_complete_replacements()

    # Живой PAUSED сам по себе НЕ доказывает, что паузу применил именно наш
    # одобренный proposal: рекламу мог отключить человек в Ads Manager, и тогда
    # «завершено» скрыло бы, что замена так и не была подтверждена исполнителем.
    # Прежде эту проверку делал _resume_checked_replacement_pause (сверял
    # CONFIRMED-операцию) — функция удалена вместе с прямой мутацией, поэтому
    # оркестратор честно ждёт подтверждения исполнителя.
    assert result.completed_workflow_ids == ()
    assert result.waiting_workflow_ids == ("workflow-1",)
    assert result.errors == ("workflow-1:executor_confirmation_required",)
    storage.mark_old.assert_not_called()
    mutation_guard.assert_untouched()


def test_checked_replacement_pause_creates_recovery_proposal(monkeypatch, tmp_path):
    """`_execute_checked_replacement_pause` создаёт PAUSE-proposal, а не паузит FB.

    Ранее «пауза старой рекламы после замены» проверялась через мутатор. Здесь та
    же бизнес-ситуация проверяется на новом контракте: origin RECOVERY (это не
    автопилот, а восстановление после замены), replacement-идентити уходит в
    scope (proposal принимает только UUID4 в качестве ключа), наружу отдаётся
    proposal_id.
    """
    from services import adset_pause_guard

    def fake_inventory(ad_ids):
        del ad_ids
        context = {
            current_id: {
                "ad_id": current_id,
                "adset_id": "100",
                "name": current_id,
                "configured_status": "ACTIVE",
                "effective_status": "ACTIVE",
            }
            for current_id in ("old-1", "new-1")
        }
        return {
            "100": {
                "adset_id": "100",
                "active_ids": {"old-1", "new-1"},
                "candidate_context": {"old-1": context["old-1"]},
                "inventory_context": context,
                "complete": True,
                "pages_read": 1,
                "error": None,
            }
        }

    monkeypatch.setattr(adset_pause_guard, "fetch_pause_inventory", fake_inventory)
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory", fake_inventory
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts",
        lambda ids, **_kwargs: (
            {ad_id: fake_inventory(ids)["100"]["inventory_context"][ad_id] for ad_id in ids},
            None,
        ),
    )
    recorder = install_proposal_recorder(monkeypatch, tmp_path)

    proposal_id = orchestrator._execute_checked_replacement_pause(
        SimpleNamespace(ad_id="old-1"),
        datetime.now(timezone.utc),
        "replacement-identity-key",
    )

    assert proposal_id == "proposal-1"
    plan = recorder.assert_proposed(
        "old-1",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.RECOVERY,
        action_kind="PAUSE_AD",
    )
    assert plan.source_ref == "replacement:replacement-identity-key:old-1"
    assert plan.targets[0].intended_payload["reason_code"] == "REPLACEMENT_READY"
    recorder.assert_no_direct_provider_mutation()


def test_verify_timeout_blocks_without_pause(monkeypatch):
    workflow = _workflow("WAITING_ACTIVE")
    workflow["replacement_created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=49)
    ).isoformat()
    storage = _Storage(workflow, _link())
    _prepare_verify(monkeypatch, storage)
    monkeypatch.setattr(
        orchestrator,
        "_verify_created_contexts",
        lambda _workflow, _link: ("WAITING", ("new-1",), (), None),
    )
    result = orchestrator.verify_and_complete_replacements()

    assert result.blocked_workflow_ids == ("workflow-1",)
    storage.block.assert_called_once_with("workflow-1", "replacement_active_timeout")


def test_replacement_enqueued_is_not_pause(monkeypatch):
    outcome = orchestrator.PauseOrReplacementOutcome(
        "REPLACEMENT_ENQUEUED", "old-1", "workflow-1", None
    )

    assert outcome.action != "PAUSED"
    assert not hasattr(outcome, "ok")


def test_paused_old_does_not_change_slot_arithmetic(monkeypatch):
    """PAUSE не освобождает object slot: outcome берётся только из cleaner inventory."""
    storage = _Storage(_workflow(), _link())
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)
    monkeypatch.setattr(
        "services.adset_cleaner.get_cleaner_config",
        lambda: {"hard_reserve_slots": 1},
    )
    monkeypatch.setattr(
        "services.adset_cleaner.run_cleaner",
        lambda **_kwargs: {
            "action": "WAITING_SAFE_CANDIDATES",
            "capacity_before": {"100": 0},
            "capacity_after": {"100": 0},
            "deficit_before": 2,
            "deficit_after": 2,
            "deleted": [],
            "claim_ids": [],
            "reason": "safe_preflight_below_exact_deficit",
        },
    )

    result = orchestrator.ensure_slot_for_workflow("workflow-1")

    assert result.available_after == 0
    assert result.deficit_after == 2
    assert result.action == "WAITING_SAFE_CANDIDATES"
