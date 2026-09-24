"""Хелперы для тестов approval-first границы действий.

Проект переведён на approval-first: producer'ы (autopilot, scaler, launcher,
web) НЕ мутируют Facebook напрямую — они только создают proposal, который
владелец одобряет в Telegram. Поэтому в тестах есть два уровня:

1. `proposal_outcome` / `blocked_outcome` — подменяют producer-границу
   (`services.action_producer_gateway.propose_*`) готовым результатом, когда
   тест проверяет поведение вызывающего кода (сколько предложений создано,
   что попало в отчёт, что случилось при отказе).
2. `install_proposal_recorder` — оставляет всю discovery/guard-логику
   `propose_*` живой (live inventory, last-active guard, inventory_incomplete),
   подменяет ТОЛЬКО запись в БД и заодно вешает Mock'и на реальные
   FB-мутаторы. Так тест доказывает и «прямой мутации не было», и
   «создан корректный proposal (kind/target/origin)».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from services.action_producer_gateway import ProducerActionOutcome
from services.approval_checker_models import ActionResult, OperationState
from services.owner_action_models import (
    ProposalKind,
    ProposalOrigin,
    ProposalReceipt,
    ProposedActionPlan,
)

# Точки, где производится РЕАЛЬНАЯ мутация Facebook. Ни один producer не имеет
# права их дёрнуть — только execution boundary после одобрения владельца.
PROVIDER_MUTATION_TARGETS = (
    "integrations.facebook_ads_mutation_transport.set_ad_status",
    "integrations.facebook_ads_mutation_transport.set_adset_budget",
    "integrations.facebook_ads_mutation_transport.create_ad",
)
EXECUTION_BOUNDARY_TARGETS = (
    "services.action_gateway.execute_action",
    "services.action_gateway.execute_action_batch",
)


def _now() -> datetime:
    return datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def proposal_receipt(
    proposal_id: str = "44444444-4444-4444-8444-444444444444",
    *,
    state: str = "PENDING_OWNER",
    deduplicated: bool = False,
) -> ProposalReceipt:
    """Квитанция созданного proposal (владелец ещё НЕ одобрил)."""
    created = _now()
    return ProposalReceipt(
        proposal_id=proposal_id,
        idempotency_key="22222222-2222-4222-8222-222222222222",
        state=state,  # type: ignore[arg-type]
        proposal_sha256="a" * 64,
        created_at=created,
        valid_until=created + timedelta(hours=24),
        deduplicated=deduplicated,
    )


def proposal_outcome(
    proposal_id: str = "44444444-4444-4444-8444-444444444444",
    *,
    action: str = "PROPOSAL_CREATED",
    deduplicated: bool = False,
) -> ProducerActionOutcome:
    """Producer создал proposal. Это НЕ успех исполнения: `confirmed` = False."""
    return ProducerActionOutcome(
        action=action,
        receipt=proposal_receipt(proposal_id, deduplicated=deduplicated),
    )


def proposal_outcomes(*proposal_ids: str) -> list[ProducerActionOutcome]:
    """side_effect для последовательных вызовов propose_* с разными id."""
    return [proposal_outcome(proposal_id) for proposal_id in proposal_ids]


def blocked_outcome(
    reason: str,
    *,
    action: str = "BLOCKED",
    workflow_id: str | None = None,
) -> ProducerActionOutcome:
    """Producer честно отказался создавать proposal (receipt=None)."""
    return ProducerActionOutcome(
        action=action,
        reason=reason,
        workflow_id=workflow_id,
    )


@dataclass
class RecordedProposals:
    """Что producer предложил владельцу и чего он при этом НЕ сделал."""

    plans: list[ProposedActionPlan] = field(default_factory=list)
    provider_mutations: dict[str, Mock] = field(default_factory=dict)
    execution_boundary: dict[str, Mock] = field(default_factory=dict)

    @property
    def subject_ids(self) -> list[str]:
        return [target.subject_id for plan in self.plans for target in plan.targets]

    @property
    def kinds(self) -> list[str]:
        return [str(plan.proposal_kind) for plan in self.plans]

    @property
    def origins(self) -> list[str]:
        return [str(plan.origin) for plan in self.plans]

    @property
    def action_kinds(self) -> list[str]:
        return [target.action_kind for plan in self.plans for target in plan.targets]

    @property
    def source_refs(self) -> list[str]:
        return [plan.source_ref for plan in self.plans]

    def targets_for(self, subject_id: str) -> list[object]:
        return [
            target
            for plan in self.plans
            for target in plan.targets
            if target.subject_id == subject_id
        ]

    def assert_no_direct_provider_mutation(self) -> None:
        """Ни один FB-мутатор и ни одна точка исполнения не были вызваны."""
        for name, mock in {**self.provider_mutations, **self.execution_boundary}.items():
            assert not mock.called, f"producer напрямую вызвал {name}"

    def assert_proposed(
        self,
        subject_id: str,
        *,
        kind: ProposalKind,
        origin: ProposalOrigin,
        action_kind: str | None = None,
    ) -> ProposedActionPlan:
        """Ровно один proposal нужного вида на указанный объект."""
        matched = [
            plan
            for plan in self.plans
            if any(target.subject_id == subject_id for target in plan.targets)
        ]
        assert len(matched) == 1, (
            f"ожидался ровно 1 proposal на {subject_id}, найдено {len(matched)}"
        )
        plan = matched[0]
        assert plan.proposal_kind is kind
        assert plan.origin is origin
        if action_kind is not None:
            assert [target.action_kind for target in plan.targets] == [action_kind]
        return plan


@dataclass
class ProviderMutationGuard:
    """Все точки реальной мутации Facebook закрыты Mock'ами, падающими при вызове."""

    mocks: dict[str, Mock] = field(default_factory=dict)

    def assert_untouched(self) -> None:
        for name, mock in self.mocks.items():
            assert not mock.called, f"вызвана прямая FB-мутация {name}"


def install_provider_mutation_guard(monkeypatch) -> ProviderMutationGuard:
    """Запрещает любую провайдерскую мутацию (в т.ч. HTTP-сессию транспорта).

    Нужен там, где тест доказывает «операция удалена/запрещена»: если код
    когда-нибудь снова начнёт мутировать FB напрямую, тест упадёт.
    """
    guard = ProviderMutationGuard()
    targets = (*PROVIDER_MUTATION_TARGETS, "integrations.facebook_ads_mutation_transport.session")
    for target in targets:
        mock = Mock(side_effect=AssertionError(f"прямая FB-мутация {target}"))
        monkeypatch.setattr(target, mock, raising=False)
        guard.mocks[target] = mock
    return guard


def init_action_db(tmp_path) -> None:
    """Свежая БД действий на каждый тест: idempotency-scope'ы не протекают."""
    from agent.database import init_db

    init_db(str(tmp_path / "decisions.db"), str(tmp_path / "missing-settings.json"))


def install_proposal_recorder(
    monkeypatch,
    tmp_path=None,
    *,
    fail_with: Exception | None = None,
):
    """Записывает proposal-планы вместо записи в БД и блокирует FB-мутации.

    Discovery/guard внутри propose_* остаётся живым: last-active guard,
    inventory_incomplete и прочие fail-closed ветки продолжают работать.
    Idempotency-биндинг (`reserve_idempotency`) требует БД — передайте tmp_path.
    """
    if tmp_path is not None:
        init_action_db(tmp_path)
    recorded = RecordedProposals()

    def _fake_propose_action(plan, *, now=None):
        del now
        recorded.plans.append(plan)
        if fail_with is not None:
            raise fail_with
        return proposal_receipt(f"proposal-{len(recorded.plans)}")

    monkeypatch.setattr(
        "services.owner_action_repository.propose_action", _fake_propose_action
    )
    for target in PROVIDER_MUTATION_TARGETS:
        mock = Mock(side_effect=AssertionError(f"прямая FB-мутация {target}"))
        monkeypatch.setattr(target, mock, raising=False)
        recorded.provider_mutations[target] = mock
    for target in EXECUTION_BOUNDARY_TARGETS:
        mock = Mock(side_effect=AssertionError(f"обход approval через {target}"))
        monkeypatch.setattr(target, mock, raising=False)
        recorded.execution_boundary[target] = mock
    return recorded


def confirmed_outcome(
    action: str,
    operation_id: str = "11111111-1111-4111-8111-111111111111",
) -> ProducerActionOutcome:
    """Legacy execution-outcome. Используется только там, где тест мокает
    ИСПОЛНИТЕЛЬНУЮ границу (services.action_gateway), а не producer'а."""
    run = SimpleNamespace(
        operation_id=operation_id,
        idempotency_key="22222222-2222-4222-8222-222222222222",
        batch_manifest_id="33333333-3333-4333-8333-333333333333",
        state=OperationState.CONFIRMED,
        result=ActionResult.CONFIRMED,
        reconciliation_required=False,
        stop_reason_code=None,
        provider_mutation_count=1,
        first_unprocessed_index=None,
        dry_run=False,
        executions=(),
    )
    return ProducerActionOutcome(action=action, run=run)


def non_confirmed_outcome(
    action: str,
    *,
    reason: str | None = None,
    workflow_id: str | None = None,
) -> ProducerActionOutcome:
    return ProducerActionOutcome(
        action=action,
        reason=reason,
        workflow_id=workflow_id,
    )
