"""Replacement orchestration доводит паузу старого объявления до владельца.

Раньше оркестратор сам исполнял PAUSE через sealed gateway и по CONFIRMED
закрывал workflow. Теперь `_execute_checked_replacement_pause` только создаёт
PAUSE-proposal: пока владелец не одобрил, старое объявление живо, поэтому
workflow обязан оставаться в ожидании, а не отмечаться завершённым.
"""

from __future__ import annotations

import ast
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import replacement_orchestrator as orchestrator
from services.approval_checker_models import (
    ActionKind,
    ActionOrigin,
    EvidenceRecord,
    EvidenceState,
    FactCategory,
    Metric,
    PauseCandidate,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
)
from tests.gateway_test_helpers import blocked_outcome, proposal_outcome


NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
SHA = "a" * 64


class _Storage:
    def __init__(self, workflow: dict[str, object], link: dict[str, object]) -> None:
        self.workflow = workflow
        self.link = link
        self.mark_old_paused = Mock()
        self.mark_workflow_blocked = Mock()
        self.mark_workflow_cancelled = Mock()

    def get_workflow(self, workflow_id: str):
        assert workflow_id == "workflow-1"
        return dict(self.workflow)

    def get_replacement_launch(self, workflow_id: str):
        assert workflow_id == "workflow-1"
        return dict(self.link)


def _workflow(status: str = "READY_TO_PAUSE") -> dict[str, object]:
    return {
        "workflow_id": "workflow-1",
        "phase": status,
        "old_ad_id": "old-1",
        "adset_id": "100",
        "city": "CityA",
        "replacement_created_at": (NOW - timedelta(minutes=5)).isoformat(),
        "replacement_active_at": NOW.isoformat(),
    }


def _link() -> dict[str, object]:
    return {
        "workflow_id": "workflow-1",
        "account_kind": "offline",
        "account_id": "123",
        "adset_id": "100",
        "expected_ad_count": 1,
        "expected_ad_names": ("Новая 1",),
        "created_ad_ids": ("new-1",),
    }


def _candidate() -> PauseCandidate:
    return PauseCandidate(
        ad_id="old-1",
        adset_id="100",
        display_name="Старая",
        reason_code="REPLACEMENT_READY",
        decision_window=TimeWindow(
            NOW - timedelta(days=30),
            NOW,
            "UTC",
            "REPLACEMENT_PAUSE_TRAILING_30D",
        ),
        expected_before_status="ACTIVE",
        expected_after_status="PAUSED",
        spend=Decimal("10"),
        spend_currency="USD",
        leads=1,
        quals=0,
        payments=(),
        revenue_lcy=Decimal("0"),
        pre_inventory_sha256=SHA,
        sibling_active_ids=("new-1",),
        replacement_ad_id="new-1",
    )


def _prepare_verifier(monkeypatch, storage: _Storage) -> None:
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)
    monkeypatch.setattr(
        orchestrator,
        "_replacement_config",
        lambda: {"enabled": True, "max_pending_hours": 48},
    )
    monkeypatch.setattr(
        orchestrator,
        "_list_replacement_workflows",
        lambda: [dict(storage.workflow)],
    )
    monkeypatch.setattr(
        orchestrator,
        "_account_scope",
        lambda _link: __import__("contextlib").nullcontext(),
    )
    monkeypatch.setattr(orchestrator, "_assert_account", lambda _link: None)
    monkeypatch.setattr(
        orchestrator,
        "_verify_created_contexts",
        lambda _workflow, _link: (
            "ACTIVE",
            ("new-1",),
            (
                {
                    "id": "new-1",
                    "name": "Новая 1",
                    "adset_id": "100",
                    "effective_status": "ACTIVE",
                },
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "fetch_exact_ad_contexts",
        lambda _ids, **_kwargs: (
            {
                "old-1": {
                    "ad_id": "old-1",
                    "name": "Старая",
                    "adset_id": "100",
                    "configured_status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
            },
            None,
        ),
    )


def test_local_enqueue_module_has_no_raw_pause_or_provider_mutation() -> None:
    source_path = Path(orchestrator.__file__)
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

    forbidden = {
        "safe_pause_ad",
        "pause_ad",
        "_pause_ad_unchecked",
        "launch_creative",
        "create_ad_from_existing_creative",
    }
    assert forbidden.isdisjoint(imported_names)
    assert forbidden.isdisjoint(called_names)


def test_spare_active_requires_gateway_and_never_enqueues_or_pauses(monkeypatch) -> None:
    storage = SimpleNamespace(enqueue_replacement=Mock())
    monkeypatch.setattr(
        orchestrator,
        "_validated_pause_snapshot",
        lambda _ad_id: ("100", {"ad_id": "old-1"}, {"old-1", "spare-1"}),
    )
    monkeypatch.setattr(orchestrator, "_workflow_module", lambda: storage)

    result = orchestrator.safe_pause_or_enqueue_replacement("old-1", "autopilot")

    assert result.action == "BLOCKED"
    assert result.reason == "pause_requires_approval_gateway"
    storage.enqueue_replacement.assert_not_called()


def test_checked_pause_creates_proposal_and_never_executes(monkeypatch) -> None:
    """Пауза старого объявления уходит владельцу, а не в провайдера.

    Детерминированный UUID5 replacement-идентити должен попасть в scope, а не в
    idempotency_key: proposal принимает только UUID4, и раньше UUID5 ронял
    создание предложения. Scope при этом стабилен, значит повтор verify не
    плодит второе предложение.
    """
    candidate = _candidate()
    propose = Mock(return_value=proposal_outcome("proposal-pause-old"))
    execute = Mock()
    monkeypatch.setattr("services.action_producer_gateway.propose_pause", propose)
    monkeypatch.setattr("services.action_gateway.execute_action", execute)
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, "replacement-identity"))

    result = orchestrator._execute_checked_replacement_pause(candidate, NOW, key)

    assert result == "proposal-pause-old"
    propose.assert_called_once()
    assert propose.call_args.args == ("old-1",)
    assert propose.call_args.kwargs["origin"] is ActionOrigin.REPLACEMENT
    assert propose.call_args.kwargs["scope"] == f"replacement:{key}:old-1"
    assert propose.call_args.kwargs["reason_code"] == "REPLACEMENT_READY"
    assert propose.call_args.kwargs["now"] == NOW
    execute.assert_not_called()


def test_checked_pause_refusal_is_dependency_error(monkeypatch) -> None:
    """Если предложение не создано — это ошибка зависимости, не тихий успех."""
    propose = Mock(return_value=blocked_outcome("LAST_EFFECTIVE_ACTIVE"))
    monkeypatch.setattr("services.action_producer_gateway.propose_pause", propose)

    with pytest.raises(
        orchestrator.ReplacementDependencyError,
        match="replacement_pause_proposal_missing",
    ):
        orchestrator._execute_checked_replacement_pause(
            _candidate(), NOW, str(uuid.uuid4())
        )


def test_verifier_never_completes_before_owner_approval(monkeypatch) -> None:
    """Активная замена рождает предложение, но не закрывает workflow.

    `mark_old_paused` — бизнес-факт «старое погашено». Пока владелец не одобрил
    паузу, этот факт неверен: старое объявление всё ещё тратит бюджет.
    """
    storage = _Storage(_workflow(), _link())
    _prepare_verifier(monkeypatch, storage)
    candidate = _candidate()
    monkeypatch.setattr(
        orchestrator,
        "_build_replacement_pause_candidate",
        Mock(return_value=(candidate, NOW, str(uuid.uuid4()))),
    )
    propose = Mock(return_value="proposal-pause-old")
    monkeypatch.setattr(orchestrator, "_execute_checked_replacement_pause", propose)

    result = orchestrator.verify_and_complete_replacements()

    propose.assert_called_once()
    assert result.completed_workflow_ids == ()
    assert result.waiting_workflow_ids == ("workflow-1",)
    assert any(
        "owner_pause_proposal_pending:proposal-pause-old" in error
        for error in result.errors
    ), result.errors
    storage.mark_old_paused.assert_not_called()


def test_proposal_failure_keeps_workflow_pending_without_completion(monkeypatch) -> None:
    """Сбой создания предложения оставляет workflow в ожидании, а не в успехе."""
    storage = _Storage(_workflow(), _link())
    _prepare_verifier(monkeypatch, storage)
    monkeypatch.setattr(
        orchestrator,
        "_build_replacement_pause_candidate",
        Mock(return_value=(_candidate(), NOW, str(uuid.uuid4()))),
    )
    monkeypatch.setattr(
        orchestrator,
        "_execute_checked_replacement_pause",
        Mock(
            side_effect=orchestrator.ReplacementDependencyError(
                "replacement_pause_proposal_missing"
            )
        ),
    )

    result = orchestrator.verify_and_complete_replacements()

    assert result.waiting_workflow_ids == ("workflow-1",)
    assert result.completed_workflow_ids == ()
    assert "replacement_pause_proposal_missing" in result.errors[0]
    storage.mark_old_paused.assert_not_called()


def test_already_paused_requires_executor_confirmation_not_completion(
    monkeypatch,
) -> None:
    """Внешняя пауза старого объявления не считается выполнением workflow."""
    storage = _Storage(_workflow(), _link())
    _prepare_verifier(monkeypatch, storage)
    monkeypatch.setattr(
        orchestrator,
        "fetch_exact_ad_contexts",
        lambda _ids, **_kwargs: (
            {
                "old-1": {
                    "ad_id": "old-1",
                    "name": "Старая",
                    "adset_id": "100",
                    "configured_status": "PAUSED",
                    "effective_status": "PAUSED",
                }
            },
            None,
        ),
    )
    propose = Mock()
    monkeypatch.setattr(orchestrator, "_execute_checked_replacement_pause", propose)

    result = orchestrator.verify_and_complete_replacements()

    # Живой PAUSED сам по себе не доказывает, что паузу выполнил ИМЕННО наш
    # одобренный proposal: объявление мог погасить кто-то вручную. Поэтому
    # workflow остаётся в ожидании подтверждения исполнителя, а не закрывается.
    assert result.completed_workflow_ids == ()
    assert result.waiting_workflow_ids == ("workflow-1",)
    assert any(
        "executor_confirmation_required" in error for error in result.errors
    ), result.errors
    propose.assert_not_called()
    storage.mark_old_paused.assert_not_called()


def test_candidate_uses_force_live_exact_metrics_and_stable_idempotency(monkeypatch) -> None:
    workflow = _workflow()
    link = _link()
    subject = SubjectRef(SubjectKind.AD, "old-1", "100")

    def evidence(
        source: SourceSystem,
        records: tuple[EvidenceRecord, ...],
    ) -> SourceEvidence:
        return SourceEvidence(
            source=source,
            state=EvidenceState.FRESH_COMPLETE,
            fetched_at=NOW,
            data_as_of=NOW,
            from_cache=False,
            complete=True,
            records=records,
        )

    def fb_loader(request, now, *, force_live):
        assert force_live is True
        window = request.windows[0]
        return evidence(
            SourceSystem.FACEBOOK,
            (
                EvidenceRecord(
                    category=FactCategory.BUSINESS_METRIC,
                    subject=subject,
                    metric=Metric.SPEND,
                    value=Decimal("15.25"),
                    source=SourceSystem.FACEBOOK,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=window,
                    currency="USD",
                ),
                EvidenceRecord(
                    category=FactCategory.BUSINESS_METRIC,
                    subject=subject,
                    metric=Metric.LEADS,
                    value=3,
                    source=SourceSystem.FACEBOOK,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=window,
                    currency=None,
                ),
            ),
        )

    def amo_loader(request, now, *, force_live):
        assert force_live is True
        return evidence(
            SourceSystem.AMO,
            (
                EvidenceRecord(
                    category=FactCategory.BUSINESS_METRIC,
                    subject=subject,
                    metric=Metric.QUALS,
                    value=1,
                    source=SourceSystem.AMO,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=request.windows[0],
                    currency=None,
                ),
            ),
        )

    def cdp_loader(request, now, *, force_live):
        assert force_live is True
        window = request.windows[0]
        return evidence(
            SourceSystem.CDP_ERP,
            (
                EvidenceRecord(
                    category=FactCategory.BUSINESS_METRIC,
                    subject=subject,
                    metric=Metric.PAYMENTS,
                    value=0,
                    source=SourceSystem.CDP_ERP,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=window,
                    currency="LCY",
                ),
                EvidenceRecord(
                    category=FactCategory.BUSINESS_METRIC,
                    subject=subject,
                    metric=Metric.REVENUE,
                    value=Decimal("0"),
                    source=SourceSystem.CDP_ERP,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=window,
                    currency="LCY",
                ),
            ),
        )

    monkeypatch.setattr("services.approval_source_facebook.load_facebook_evidence", fb_loader)
    monkeypatch.setattr("services.approval_source_amo.load_amo_evidence", amo_loader)
    monkeypatch.setattr("services.approval_source_cdp.load_cdp_evidence", cdp_loader)
    monkeypatch.setattr(
        orchestrator,
        "fetch_pause_inventory",
        lambda _ids: {
            "100": {
                "complete": True,
                "active_ids": {"old-1", "new-1", "spare-1"},
                "inventory_context": {
                    ad_id: {
                        "ad_id": ad_id,
                        "adset_id": "100",
                        "configured_status": "ACTIVE",
                        "effective_status": "ACTIVE",
                    }
                    for ad_id in ("old-1", "new-1", "spare-1")
                },
                "state_sha256": SHA,
            }
        },
    )
    old_context = {"name": "Старая"}

    first = orchestrator._build_replacement_pause_candidate(
        "workflow-1", workflow, link, ("new-1",), old_context, NOW
    )
    second = orchestrator._build_replacement_pause_candidate(
        "workflow-1", workflow, link, ("new-1",), old_context, NOW
    )

    candidate, prepared_at, key = first
    assert first == second
    assert prepared_at == NOW
    assert uuid.UUID(key).version == 5
    assert candidate.spend == Decimal("15.25")
    assert candidate.leads == 3
    assert candidate.quals == 1
    assert candidate.sibling_active_ids == ("new-1", "spare-1")
    assert candidate.replacement_ad_id == "new-1"
    assert candidate.pre_inventory_sha256 == SHA
    assert candidate.expected_after_status == "PAUSED"
    assert "DELETE" not in {kind.value for kind in ActionKind}


# ===========================================================================
# Полный цикл state machine: предложение → исполнение → COMPLETED, отказ → CANCELLED
# ===========================================================================

class _CycleStorage:
    """Мини-модель durable workflow: держит фазу и разрешённые переходы."""

    def __init__(self) -> None:
        self.workflow = _workflow("READY_TO_PAUSE")
        self.link = _link()
        self.events: list[str] = []

    def get_workflow(self, workflow_id: str):
        assert workflow_id == "workflow-1"
        return dict(self.workflow)

    def get_replacement_launch(self, workflow_id: str):
        assert workflow_id == "workflow-1"
        return dict(self.link)

    def mark_old_paused(self, workflow_id: str) -> None:
        assert self.workflow["phase"] == "READY_TO_PAUSE", self.workflow["phase"]
        self.workflow["phase"] = "COMPLETED"
        self.events.append("COMPLETED")

    def mark_workflow_blocked(self, workflow_id: str, reason: str) -> None:
        assert self.workflow["phase"] not in {"COMPLETED", "CANCELLED"}
        self.workflow["phase"] = "BLOCKED"
        self.events.append(f"BLOCKED:{reason}")

    def mark_workflow_cancelled(self, workflow_id: str, reason: str) -> None:
        assert self.workflow["phase"] not in {"COMPLETED", "CANCELLED"}
        self.workflow["phase"] = "CANCELLED"
        self.events.append(f"CANCELLED:{reason}")


def _install_old_status(monkeypatch, status: str) -> None:
    """Живой статус старого объявления в текущем тике."""
    monkeypatch.setattr(
        orchestrator,
        "fetch_exact_ad_contexts",
        lambda _ids, **_kwargs: (
            {
                "old-1": {
                    "ad_id": "old-1",
                    "name": "Старая",
                    "adset_id": "100",
                    "configured_status": status,
                    "effective_status": status,
                }
            },
            None,
        ),
    )


def _install_lifecycle(monkeypatch, state: str | None, asked: list[str]):
    """Подменяет журнал owner-предложений; пишет запрошенный source_ref в asked."""

    def fake_lookup(source_ref: str):
        asked.append(source_ref)
        return None if state is None else ("proposal-1", state)

    monkeypatch.setattr(
        "services.owner_action_repository.find_lifecycle_by_source_ref",
        fake_lookup,
    )


def _expected_scope(storage: _CycleStorage) -> str:
    """Тот же детерминированный scope, который producer положил в source_ref."""
    _prepared_at, identity = orchestrator._replacement_pause_identity(
        "workflow-1", dict(storage.workflow), ("new-1",)
    )
    return orchestrator._replacement_pause_scope(identity, "old-1")


def test_full_cycle_proposal_then_execution_completes_workflow(monkeypatch) -> None:
    """Создан proposal → ждём → исполнен → mark_old_paused → COMPLETED.

    Регрессия: `mark_old_paused` был недостижим (все ветки цикла заканчивались
    `continue`), поэтому фаза COMPLETED не наступала никогда — workflow вечно
    висел в WAITING даже после исполненной паузы.
    """
    storage = _CycleStorage()
    _prepare_verifier(monkeypatch, storage)
    monkeypatch.setattr(
        orchestrator,
        "_build_replacement_pause_candidate",
        Mock(return_value=(_candidate(), NOW, str(uuid.uuid4()))),
    )
    propose = Mock(return_value="proposal-1")
    monkeypatch.setattr(orchestrator, "_execute_checked_replacement_pause", propose)

    # Тик 1: старое ACTIVE, предложения ещё нет → просим владельца.
    asked: list[str] = []
    _install_old_status(monkeypatch, "ACTIVE")
    _install_lifecycle(monkeypatch, None, asked)
    first = orchestrator.verify_and_complete_replacements()

    assert propose.call_count == 1
    assert first.waiting_workflow_ids == ("workflow-1",)
    assert first.completed_workflow_ids == ()
    assert storage.events == []
    # Смотрим ровно на СВОЁ предложение: scope восстановлен из durable identity.
    assert asked == [_expected_scope(storage)]

    # Тик 2: предложение висит у владельца → второй раз не просим.
    _install_lifecycle(monkeypatch, "PENDING_OWNER", asked)
    second = orchestrator.verify_and_complete_replacements()

    assert propose.call_count == 1, "повторный тик не должен плодить предложения"
    assert second.waiting_workflow_ids == ("workflow-1",)
    assert second.errors == ("workflow-1:owner_pause_proposal_pending:proposal-1",)
    assert storage.events == []

    # Тик 3: владелец одобрил, execution boundary исполнил PAUSE.
    _install_old_status(monkeypatch, "PAUSED")
    _install_lifecycle(monkeypatch, "EXECUTED", asked)
    third = orchestrator.verify_and_complete_replacements()

    assert third.completed_workflow_ids == ("workflow-1",)
    assert third.waiting_workflow_ids == ()
    assert third.cancelled_workflow_ids == ()
    assert storage.events == ["COMPLETED"]
    assert storage.workflow["phase"] == "COMPLETED"
    assert propose.call_count == 1


@pytest.mark.parametrize("executed_state", ["EXECUTED", "VERIFYING", "VERIFIED"])
def test_paused_old_completes_only_after_executed_lifecycle(
    monkeypatch, executed_state: str
) -> None:
    """Любое post-EXECUTED состояние proposal закрывает workflow."""
    storage = _CycleStorage()
    _prepare_verifier(monkeypatch, storage)
    _install_old_status(monkeypatch, "PAUSED")
    _install_lifecycle(monkeypatch, executed_state, [])
    propose = Mock()
    monkeypatch.setattr(orchestrator, "_execute_checked_replacement_pause", propose)

    result = orchestrator.verify_and_complete_replacements()

    assert result.completed_workflow_ids == ("workflow-1",)
    assert storage.workflow["phase"] == "COMPLETED"
    propose.assert_not_called()


@pytest.mark.parametrize("pending_state", ["PENDING_OWNER", "APPROVED", "ATTEMPT_STARTED"])
def test_paused_old_without_executed_lifecycle_keeps_waiting(
    monkeypatch, pending_state: str
) -> None:
    """PAUSED + неисполненное предложение = ждём исполнителя, а не «готово».

    Объявление могли погасить руками в Ads Manager, пока предложение висит.
    Записать «старое погашено нами» в этот момент было бы неправдой.
    """
    storage = _CycleStorage()
    _prepare_verifier(monkeypatch, storage)
    _install_old_status(monkeypatch, "PAUSED")
    _install_lifecycle(monkeypatch, pending_state, [])

    result = orchestrator.verify_and_complete_replacements()

    assert result.completed_workflow_ids == ()
    assert result.waiting_workflow_ids == ("workflow-1",)
    assert result.errors == ("workflow-1:executor_confirmation_required",)
    assert storage.events == []


@pytest.mark.parametrize("refused_state", ["REJECTED", "CANCELLED"])
def test_owner_refusal_closes_workflow_terminally(monkeypatch, refused_state: str) -> None:
    """Владелец отклонил паузу → терминальный CANCELLED, а не бесконечные просьбы."""
    storage = _CycleStorage()
    _prepare_verifier(monkeypatch, storage)
    _install_old_status(monkeypatch, "ACTIVE")
    _install_lifecycle(monkeypatch, refused_state, [])
    propose = Mock()
    monkeypatch.setattr(orchestrator, "_execute_checked_replacement_pause", propose)

    result = orchestrator.verify_and_complete_replacements()

    propose.assert_not_called()
    assert result.cancelled_workflow_ids == ("workflow-1",)
    assert result.waiting_workflow_ids == ()
    assert result.completed_workflow_ids == ()
    assert result.errors == (
        f"workflow-1:owner_refused_pause:proposal-1:{refused_state}",
    )
    assert storage.workflow["phase"] == "CANCELLED"
    assert storage.events == [f"CANCELLED:owner_refused_pause:{refused_state}"]


def test_unavailable_lifecycle_journal_keeps_workflow_waiting(monkeypatch) -> None:
    """Недоступный журнал предложений = fail-closed: не завершаем и не отменяем."""
    storage = _CycleStorage()
    _prepare_verifier(monkeypatch, storage)
    _install_old_status(monkeypatch, "PAUSED")
    monkeypatch.setattr(
        "services.owner_action_repository.find_lifecycle_by_source_ref",
        Mock(side_effect=RuntimeError("БД не инициализирована")),
    )

    result = orchestrator.verify_and_complete_replacements()

    assert result.waiting_workflow_ids == ("workflow-1",)
    assert result.completed_workflow_ids == ()
    assert result.cancelled_workflow_ids == ()
    assert storage.events == []


@pytest.mark.parametrize(
    "stalled_state", ["EXPIRED", "BLOCKED_STALE", "FAILED_NO_EFFECT", "RECONCILE_REQUIRED"]
)
def test_stalled_proposal_is_reported_separately(monkeypatch, stalled_state: str) -> None:
    """Заглохшее предложение видно в отчёте отдельным маркером, а не «ждём владельца».

    TTL предложения — 24 часа. Одинаковый маркер «pending» для живого ожидания и
    для истёкшего предложения означал бы тихое вечное ожидание: старое
    объявление тратит бюджет, а в отчёте всё «нормально».
    """
    storage = _CycleStorage()
    _prepare_verifier(monkeypatch, storage)
    _install_old_status(monkeypatch, "ACTIVE")
    _install_lifecycle(monkeypatch, stalled_state, [])
    propose = Mock()
    monkeypatch.setattr(orchestrator, "_execute_checked_replacement_pause", propose)

    result = orchestrator.verify_and_complete_replacements()

    propose.assert_not_called()
    assert result.waiting_workflow_ids == ("workflow-1",)
    assert result.completed_workflow_ids == ()
    assert result.cancelled_workflow_ids == ()
    assert result.errors == (
        f"workflow-1:owner_pause_proposal_stalled:proposal-1:{stalled_state}",
    )
    assert storage.events == []
