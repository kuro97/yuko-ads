from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from services import action_gateway, owner_action_executor
from services.approval_checker_models import (
    ActionExecution,
    ActionKind,
    ActionObservation,
    ActionOrigin,
    ActionResult,
    ActionReview,
    CheckIssue,
    EvidenceBundle,
    EvidenceState,
    SafetyDecision,
    SourceEvidence,
    SourceSystem,
    UnpauseManifest,
    canonical_json,
    manifest_sha256,
)
from services.owner_action_models import (
    ActionAttemptAttestation,
    AttemptTransitionResult,
    ClaimProgress,
    LifecycleState,
    PermitRetirementReason,
    ProposalKind,
    ProposalOrigin,
    ProposalView,
    ProposedActionPlan,
    ProposedTarget,
    TechnicalPermit,
    canonical_sha256,
)
from services.owner_action_repository import OwnerActionPermitUnavailable


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
LIVE_SHA = "e" * 64
CONFIG_SHA = "c" * 64


def _manifest(index: int = 1) -> UnpauseManifest:
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"owner-executor:{index}"))
    return UnpauseManifest(
        kind=ActionKind.UNPAUSE,
        manifest_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"manifest:{index}")),
        origin=ActionOrigin.TELEGRAM,
        idempotency_key=key,
        prepared_at=NOW - timedelta(hours=6),
        ad_id=f"ad-{index}",
        adset_id=f"adset-{index}",
        expected_before_status="PAUSED",
        expected_after_status="ACTIVE",
        pre_inventory_sha256="a" * 64,
    )


def _target(manifest: UnpauseManifest, ordinal: int) -> ProposedTarget:
    payload = json.loads(canonical_json(manifest).decode("utf-8"))
    return ProposedTarget(
        claim_id=f"claim-{ordinal}",
        ordinal=ordinal,
        action_kind="UNPAUSE_AD",
        account_id="act-1",
        adset_id=manifest.adset_id,
        subject_id=manifest.ad_id,
        city="CityA",
        language="L1",
        intended_payload=payload,
        intended_payload_sha256=canonical_sha256(payload),
    )


def _proposal(*, targets: tuple[ProposedTarget, ...] | None = None) -> ProposalView:
    selected = targets or (_target(_manifest(), 0),)
    plan = ProposedActionPlan(
        proposal_kind=ProposalKind.UNPAUSE,
        origin=ProposalOrigin.TELEGRAM_COMMAND,
        idempotency_key=_manifest().idempotency_key,
        source_ref="telegram:test",
        actor="telegram-owner",
        summary="Вернуть объявление",
        targets=selected,
        evidence=(),
        config_version_sha256=CONFIG_SHA,
        valid_until=NOW + timedelta(hours=1),
        staged_media_root=None,
    )
    return ProposalView(
        proposal_id="proposal-1",
        plan=plan,
        proposal_sha256="b" * 64,
        plan_sha256="c" * 64,
        targets_sha256="d" * 64,
        evidence_sha256="f" * 64,
        state=LifecycleState.APPROVED.value,
        lifecycle_version=3,
        delivery_generation=1,
        active_decision_id="decision-1",
        active_job_id="job-1",
        latest_reason_code="OWNER_APPROVE",
        next_action_at=None,
        created_at=NOW - timedelta(minutes=1),
    )


class _FakeRepository:
    def __init__(self, proposal: ProposalView) -> None:
        self.proposal = proposal
        self.events: list[str] = []
        self.attempted = False
        self.completed_claims = 0
        self.active_claim_id: str | None = None

    def get_proposal(self, proposal_id: str):
        assert proposal_id == self.proposal.proposal_id
        return self.proposal

    def queue_execution(self, **kwargs):
        self.events.append("QUEUED")
        self.proposal = replace(
            self.proposal,
            state=LifecycleState.EXECUTION_QUEUED.value,
            lifecycle_version=self.proposal.lifecycle_version + 1,
        )
        return self.proposal.lifecycle_version

    def begin_live_review(self, **kwargs):
        return self.begin_claim_live_review(
            claim_id=self.proposal.targets[self.completed_claims].claim_id,
            **kwargs,
        )

    def begin_claim_live_review(self, **kwargs):
        self.events.append("LIVE_REVIEW")
        self.active_claim_id = kwargs["claim_id"]
        self.proposal = replace(
            self.proposal,
            state=LifecycleState.LIVE_REVIEW.value,
            lifecycle_version=self.proposal.lifecycle_version + 1,
        )
        return self.proposal.lifecycle_version

    def get_claim_progress(self, **kwargs):
        if self.proposal.state == LifecycleState.RECONCILE_REQUIRED.value:
            state = "RECONCILE_REQUIRED"
            claim_id = None
            ordinal = None
        elif self.completed_claims == len(self.proposal.targets):
            state = "COMPLETE"
            claim_id = None
            ordinal = None
        elif self.proposal.state in {
            LifecycleState.PERMIT_ISSUED.value,
            LifecycleState.ATTEMPT_STARTED.value,
        }:
            state = "IN_FLIGHT"
            claim_id = self.proposal.targets[self.completed_claims].claim_id
            ordinal = self.completed_claims
        else:
            state = "READY"
            claim_id = self.proposal.targets[self.completed_claims].claim_id
            ordinal = self.completed_claims
        return ClaimProgress(
            proposal_id=self.proposal.proposal_id,
            decision_id="decision-1",
            job_id="job-1",
            state=state,
            next_claim_id=claim_id,
            next_ordinal=ordinal,
            completed_claims=self.completed_claims,
            total_claims=len(self.proposal.targets),
            lifecycle_version=self.proposal.lifecycle_version,
        )

    def transition_lifecycle(self, proposal_id: str, **kwargs):
        self.events.append(kwargs["new_state"])
        self.proposal = replace(
            self.proposal,
            state=kwargs["new_state"],
            lifecycle_version=self.proposal.lifecycle_version + 1,
            latest_reason_code=kwargs.get("reason_code"),
        )
        return self.proposal.lifecycle_version

    def issue_technical_permit(self, **kwargs):
        assert kwargs["claim_id"] == self.active_claim_id
        assert kwargs["manifest_sha256"] == canonical_sha256(kwargs["manifest"])
        self.events.append("PERMIT_ISSUED")
        self.proposal = replace(
            self.proposal,
            state=LifecycleState.PERMIT_ISSUED.value,
            lifecycle_version=self.proposal.lifecycle_version + 1,
        )
        return TechnicalPermit(
            permit_id="permit-1",
            secret="permit-secret",
            proposal_id=self.proposal.proposal_id,
            decision_id="decision-1",
            job_id="job-1",
            claim_id=kwargs["claim_id"],
            expires_at=NOW + timedelta(minutes=2),
        )

    def consume_technical_permit(self, **kwargs):
        assert kwargs["permit_secret"] == "permit-secret"
        assert self.proposal.state == LifecycleState.PERMIT_ISSUED.value
        self.attempted = True
        self.events.append("ATTEMPT_STARTED")
        self.proposal = replace(
            self.proposal,
            state=LifecycleState.ATTEMPT_STARTED.value,
            lifecycle_version=self.proposal.lifecycle_version + 1,
        )
        target = self.proposal.targets[self.completed_claims]
        return ActionAttemptAttestation(
            attempt_id=f"attempt-{self.completed_claims + 1}",
            permit_id="permit-1",
            proposal_id=self.proposal.proposal_id,
            decision_id="decision-1",
            claim_id=target.claim_id,
            operation_kind=target.action_kind,
            account_id=target.account_id,
            resource_id=target.subject_id,
            payload_sha256=target.intended_payload_sha256,
            consumed_at=NOW,
        )

    def retire_unattempted_permit(self, **kwargs):
        assert kwargs["reason"] in {
            PermitRetirementReason.PERMIT_EXPIRED,
            PermitRetirementReason.PERMIT_REVOKED,
        }
        self.events.append(kwargs["reason"].value)
        self.proposal = replace(
            self.proposal,
            state=LifecycleState.EXECUTION_RETRY_WAIT.value,
            lifecycle_version=self.proposal.lifecycle_version + 1,
        )
        return self.proposal.lifecycle_version

    def transition_attempt(self, attempt_id: str, **kwargs):
        assert attempt_id == f"attempt-{self.completed_claims + 1}"
        assert self.attempted
        self.events.append(kwargs["new_state"])
        target = self.proposal.targets[self.completed_claims]
        if kwargs["new_state"] == "RECONCILE_REQUIRED":
            next_state = LifecycleState.RECONCILE_REQUIRED.value
            next_claim_id = None
        else:
            self.completed_claims += 1
            self.attempted = False
            next_claim_id = (
                self.proposal.targets[self.completed_claims].claim_id
                if self.completed_claims < len(self.proposal.targets)
                else None
            )
            next_state = (
                LifecycleState.EXECUTION_QUEUED.value
                if next_claim_id is not None
                else LifecycleState.EXECUTED.value
            )
        self.proposal = replace(
            self.proposal,
            state=next_state,
            lifecycle_version=self.proposal.lifecycle_version + 1,
        )
        return AttemptTransitionResult(
            attempt_id=attempt_id,
            claim_id=target.claim_id,
            attempt_state=kwargs["new_state"],
            aggregate_state=next_state,
            next_claim_id=next_claim_id,
            lifecycle_version=self.proposal.lifecycle_version,
        )


class _FakeSession:
    def __init__(
        self,
        repository: _FakeRepository,
        item: UnpauseManifest,
    ) -> None:
        self.repository = repository
        self.item = item

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read_precondition(self):
        return ActionObservation(
            observed_at=NOW,
            digest=LIVE_SHA,
            target_state="PAUSED",
            subject_ids=(f"ad:{self.item.ad_id}",),
            unrelated_state_digest="9" * 64,
        )

    def execute_attempt(self, attestation):
        # Главный порядок: durable attempt уже существует до provider adapter.
        assert self.repository.attempted
        self.repository.events.append("PROVIDER_IO")
        return ActionExecution(
            attempt_id=attestation.attempt_id,
            item_id="item-1",
            item_index=0,
            action_manifest_id="item-1",
            result=ActionResult.CONFIRMED,
            started_at=attestation.consumed_at,
            completed_at=NOW,
            created_ids=(),
            reason_code="POST_STATE_CONFIRMED",
            remote_may_have_changed=True,
        )

    def finalize_after_scope(self, execution):
        return execution


class _UnknownSession(_FakeSession):
    def execute_attempt(self, attestation):
        confirmed = super().execute_attempt(attestation)
        return replace(
            confirmed,
            result=ActionResult.UNKNOWN,
            reason_code="PROVIDER_OUTCOME_UNKNOWN",
        )


def _live_evidence() -> EvidenceBundle:
    facebook = SourceEvidence(
        source=SourceSystem.FACEBOOK,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=NOW,
        data_as_of=NOW,
        from_cache=False,
        complete=True,
        records=(),
    )
    return EvidenceBundle(
        loaded_at=NOW,
        sources=(facebook,),
        freshness=(),
        facebook_sha256="1" * 64,
        trello_sha256="2" * 64,
        media_sha256="3" * 64,
        amo_sha256="4" * 64,
        cdp_sha256="5" * 64,
        final_live_state_sha256=LIVE_SHA,
        local_state_sha256=None,
    )


def _safe_review(batch, item, item_index, evidence, now):
    return ActionReview(
        check_id="check-1",
        operation_id=batch.idempotency_key,
        idempotency_key=batch.idempotency_key,
        batch_manifest_id=batch.batch_manifest_id,
        item_id=item.manifest_id,
        item_index=item_index,
        action_kind=item.kind,
        decision=SafetyDecision.SAFE,
        checked_at=now,
        expires_at=now + timedelta(minutes=2),
        batch_manifest_sha256=batch.manifest_sha256,
        item_manifest_sha256=manifest_sha256(item),
        evidence_state_sha256=evidence.final_live_state_sha256,
        subject_ids=batch.subject_ids,
        issues=(),
        audit_persisted=False,
    )


def test_automatic_verdict_has_only_safe_or_denied_values() -> None:
    assert {decision.value for decision in SafetyDecision} == {"SAFE", "DENIED"}
    assert SafetyDecision("APPROVED") is SafetyDecision.SAFE


def test_public_gateway_refuses_technically_safe_direct_execution(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        action_gateway._adapter_registry,
        "_adapter_for_kind",
        lambda kind: pytest.fail("adapter не должен создаваться"),
    )
    with pytest.raises(action_gateway.OwnerConsentRequired):
        action_gateway.execute_action(_manifest(), NOW)


def test_owner_executor_consumes_attempt_before_provider_io_and_never_reuses(
    monkeypatch,
) -> None:
    repository = _FakeRepository(_proposal())
    monkeypatch.setattr(owner_action_executor, "_repository", lambda: repository)
    monkeypatch.setattr(
        owner_action_executor,
        "load_action_item_evidence",
        lambda *args: _live_evidence(),
    )
    monkeypatch.setattr(owner_action_executor, "evaluate_action_item", _safe_review)
    monkeypatch.setattr(
        owner_action_executor,
        "_open_owner_execution_session",
        lambda item, now: _FakeSession(repository, item),
    )

    run = owner_action_executor.execute_owner_approved(
        "proposal-1",
        worker_id="worker-1",
        now=NOW,
    )

    assert run.state == LifecycleState.EXECUTED.value
    assert repository.events == [
        "QUEUED",
        "LIVE_REVIEW",
        "PERMIT_ISSUED",
        "ATTEMPT_STARTED",
        "PROVIDER_IO",
        "CONFIRMED",
    ]
    replay = owner_action_executor.execute_owner_approved(
        "proposal-1",
        worker_id="worker-2",
        now=NOW,
    )
    assert replay.provider_mutation_count == 0
    assert repository.events.count("PROVIDER_IO") == 1


def test_multi_target_executes_each_claim_in_ordinal_order(monkeypatch) -> None:
    first = _target(_manifest(1), 0)
    second_manifest = replace(
        _manifest(2),
        idempotency_key=_manifest(1).idempotency_key,
    )
    second = _target(second_manifest, 1)
    repository = _FakeRepository(_proposal(targets=(first, second)))
    monkeypatch.setattr(owner_action_executor, "_repository", lambda: repository)
    monkeypatch.setattr(
        owner_action_executor,
        "load_action_item_evidence",
        lambda *args: _live_evidence(),
    )
    monkeypatch.setattr(owner_action_executor, "evaluate_action_item", _safe_review)
    monkeypatch.setattr(
        owner_action_executor,
        "_open_owner_execution_session",
        lambda item, now: _FakeSession(repository, item),
    )

    run = owner_action_executor.execute_owner_approved(
        "proposal-1",
        worker_id="worker-1",
        now=NOW,
    )

    assert run.state == LifecycleState.EXECUTED.value
    assert run.provider_mutation_count == 2
    assert tuple(item.attempt_id for item in run.executions) == (
        "attempt-1",
        "attempt-2",
    )
    assert repository.events == [
        "QUEUED",
        "LIVE_REVIEW",
        "PERMIT_ISSUED",
        "ATTEMPT_STARTED",
        "PROVIDER_IO",
        "CONFIRMED",
        "LIVE_REVIEW",
        "PERMIT_ISSUED",
        "ATTEMPT_STARTED",
        "PROVIDER_IO",
        "CONFIRMED",
    ]


def test_incomplete_live_review_blocks_without_permit_or_provider(
    monkeypatch,
) -> None:
    """Неполная живая сводка не мутирует НИЧЕГО, но и одобрение не сжигает.

    Граница безопасности прежняя: без полных пяти источников ни permit, ни
    provider I/O. Изменился только исход для владельца — раньше недоступность
    источника уводила предложение в терминальный BLOCKED_STALE (минутный сбой
    AMO хоронил одобрение навсегда), теперь задание возвращается в повтор.
    """

    repository = _FakeRepository(_proposal())
    incomplete = replace(
        _live_evidence(),
        sources=(replace(_live_evidence().sources[0], complete=False),),
    )
    monkeypatch.setattr(owner_action_executor, "_repository", lambda: repository)
    monkeypatch.setattr(
        owner_action_executor,
        "load_action_item_evidence",
        lambda *args: incomplete,
    )
    monkeypatch.setattr(
        owner_action_executor,
        "_open_owner_execution_session",
        lambda item, now: _FakeSession(repository, item),
    )

    run = owner_action_executor.execute_owner_approved(
        "proposal-1",
        worker_id="worker-1",
        now=NOW,
    )

    assert run.state == LifecycleState.EXECUTION_RETRY_WAIT.value
    assert run.reason_code.startswith("SOURCE_UNAVAILABLE:LIVE_EVIDENCE_INCOMPLETE")
    assert owner_action_executor.is_transient_reason(run.reason_code) is True
    assert "PERMIT_ISSUED" not in repository.events
    assert "PROVIDER_IO" not in repository.events


def test_reconcile_required_first_claim_blocks_remaining_targets(
    monkeypatch,
) -> None:
    first = _target(_manifest(1), 0)
    second = _target(
        replace(_manifest(2), idempotency_key=_manifest(1).idempotency_key),
        1,
    )
    repository = _FakeRepository(_proposal(targets=(first, second)))
    monkeypatch.setattr(owner_action_executor, "_repository", lambda: repository)
    monkeypatch.setattr(
        owner_action_executor,
        "load_action_item_evidence",
        lambda *args: _live_evidence(),
    )
    monkeypatch.setattr(owner_action_executor, "evaluate_action_item", _safe_review)
    monkeypatch.setattr(
        owner_action_executor,
        "_open_owner_execution_session",
        lambda item, now: _UnknownSession(repository, item),
    )

    run = owner_action_executor.execute_owner_approved(
        "proposal-1",
        worker_id="worker-1",
        now=NOW,
    )

    assert run.state == LifecycleState.RECONCILE_REQUIRED.value
    assert run.provider_mutation_count == 1
    assert repository.events.count("LIVE_REVIEW") == 1
    assert repository.events.count("PROVIDER_IO") == 1


def test_unattempted_permit_failure_is_retired_for_fresh_review(
    monkeypatch,
) -> None:
    repository = _FakeRepository(_proposal())
    monkeypatch.setattr(owner_action_executor, "_repository", lambda: repository)
    monkeypatch.setattr(
        owner_action_executor,
        "load_action_item_evidence",
        lambda *args: _live_evidence(),
    )
    monkeypatch.setattr(owner_action_executor, "evaluate_action_item", _safe_review)
    monkeypatch.setattr(
        owner_action_executor,
        "_open_owner_execution_session",
        lambda item, now: _FakeSession(repository, item),
    )
    monkeypatch.setattr(
        owner_action_executor,
        "consume_owner_technical_permit",
        lambda **kwargs: (_ for _ in ()).throw(
            OwnerActionPermitUnavailable("PERMIT_CONSUME_CAS_CONFLICT")
        ),
    )

    run = owner_action_executor.execute_owner_approved(
        "proposal-1",
        worker_id="worker-1",
        now=NOW,
    )

    assert run.state == LifecycleState.EXECUTION_RETRY_WAIT.value
    assert run.provider_mutation_count == 0
    assert "PERMIT_REVOKED" in repository.events
    assert "PROVIDER_IO" not in repository.events


# ---------------------------------------------------------------------------
# Дедлайн исполнения: одобрение продлевает жизнь задания, а терминальный
# прогресс отчитывается честно даже после протухания карточки.
# ---------------------------------------------------------------------------


def test_execution_deadline_extends_with_approval() -> None:
    proposal = _proposal()
    # Без одобрения дедлайн — это valid_until самой карточки.
    assert owner_action_executor._execution_deadline(proposal) == proposal.valid_until
    # Позднее одобрение продлевает дедлайн на OWNER_EXECUTION_APPROVAL_TTL.
    late_approval = replace(
        proposal, approved_at=proposal.valid_until - timedelta(minutes=5)
    )
    assert owner_action_executor._execution_deadline(late_approval) == (
        proposal.valid_until
        - timedelta(minutes=5)
        + owner_action_executor.OWNER_EXECUTION_APPROVAL_TTL
    )
    # Раннее одобрение ничего не укорачивает: берётся max с valid_until.
    early_approval = replace(
        proposal,
        approved_at=proposal.valid_until
        - owner_action_executor.OWNER_EXECUTION_APPROVAL_TTL
        - timedelta(hours=1),
    )
    assert (
        owner_action_executor._execution_deadline(early_approval)
        == proposal.valid_until
    )


def test_expired_deadline_blocks_only_new_claim_work(monkeypatch) -> None:
    """Протухание не начинает новую работу, но не маскирует уже сделанную."""

    # Все claims уже терминальны: даже спустя сутки после valid_until
    # возвращается честный агрегат, а не PROPOSAL_EXPIRED.
    repository = _FakeRepository(_proposal())
    repository.completed_claims = len(repository.proposal.targets)
    monkeypatch.setattr(owner_action_executor, "_repository", lambda: repository)
    run = owner_action_executor._execute_owner_approved_claims(
        "proposal-1",
        worker_id="unit",
        now=repository.proposal.valid_until + timedelta(days=1),
    )
    assert run.state == LifecycleState.EXECUTED.value
    assert run.reason_code == "ALL_CLAIMS_TERMINAL"

    # А новая работа после дедлайна не начинается вовсе.
    fresh = _FakeRepository(_proposal())
    monkeypatch.setattr(owner_action_executor, "_repository", lambda: fresh)
    with pytest.raises(
        owner_action_executor.OwnerActionExecutionError, match="PROPOSAL_EXPIRED"
    ):
        owner_action_executor._execute_owner_approved_claims(
            "proposal-1",
            worker_id="unit",
            now=fresh.proposal.valid_until + timedelta(minutes=1),
        )
    assert fresh.events == []


def test_approval_extends_execution_window(monkeypatch) -> None:
    """Одобренное при живой карточке задание переживает valid_until."""

    approved_at = NOW + timedelta(minutes=30)
    proposal = replace(_proposal(), approved_at=approved_at)
    repository = _FakeRepository(proposal)
    monkeypatch.setattr(owner_action_executor, "_repository", lambda: repository)

    class _Proceeded(RuntimeError):
        pass

    def _sentinel(*args, **kwargs):
        raise _Proceeded("PROCEEDED_PAST_DEADLINE")

    monkeypatch.setattr(owner_action_executor, "_enter_live_review", _sentinel)

    # Час после valid_until, но внутри окна одобрения — работа продолжается
    # (и утыкается в наш сентинел вместо PROPOSAL_EXPIRED).
    with pytest.raises(_Proceeded):
        owner_action_executor._execute_owner_approved_claims(
            "proposal-1",
            worker_id="unit",
            now=proposal.valid_until + timedelta(hours=1),
        )
    # А после окна одобрения — честный PROPOSAL_EXPIRED.
    with pytest.raises(
        owner_action_executor.OwnerActionExecutionError, match="PROPOSAL_EXPIRED"
    ):
        owner_action_executor._execute_owner_approved_claims(
            "proposal-1",
            worker_id="unit",
            now=approved_at
            + owner_action_executor.OWNER_EXECUTION_APPROVAL_TTL
            + timedelta(minutes=1),
        )


# --- Классификация отказа live review по составу issues ----------------------


def _capacity_review(
    issues: tuple[CheckIssue, ...],
    *,
    evidence_sha: str | None = "9" * 64,
) -> ActionReview:
    return ActionReview(
        check_id="check-1",
        operation_id="op-1",
        idempotency_key=str(uuid.uuid4()),
        batch_manifest_id="batch-1",
        item_id="item-1",
        item_index=0,
        action_kind=ActionKind.LAUNCH,
        decision=SafetyDecision.DENIED,
        checked_at=NOW,
        expires_at=None,
        batch_manifest_sha256="0" * 64,
        item_manifest_sha256="1" * 64,
        evidence_state_sha256=evidence_sha,
        subject_ids=("adset-1",),
        issues=issues,
        audit_persisted=True,
    )


def _capacity_issue(code: str) -> CheckIssue:
    return CheckIssue(code=code, message="test", blocking=True)


def test_capacity_only_denial_is_retry_wait():
    review = _capacity_review((_capacity_issue("LAUNCH_CAPACITY_INSUFFICIENT"),))
    reason = owner_action_executor._review_denial_reason(review)
    assert reason == "LAUNCH_CAPACITY_WAIT"
    assert (
        owner_action_executor._stop_state_for(reason)
        == LifecycleState.EXECUTION_RETRY_WAIT.value
    )
    assert owner_action_executor.is_transient_reason(reason)


def test_capacity_mixed_with_other_issue_stays_terminal():
    review = _capacity_review(
        (
            _capacity_issue("LAUNCH_CAPACITY_INSUFFICIENT"),
            _capacity_issue("LAUNCH_SCOPE_DRIFT"),
        )
    )
    reason = owner_action_executor._review_denial_reason(review)
    assert reason == "LIVE_REVIEW_DENIED"
    assert (
        owner_action_executor._stop_state_for(reason)
        == LifecycleState.BLOCKED_STALE.value
    )


def test_denial_without_issues_stays_terminal():
    review = _capacity_review(())
    assert owner_action_executor._review_denial_reason(review) == "LIVE_REVIEW_DENIED"


def test_capacity_denial_without_evidence_sha_stays_terminal():
    review = _capacity_review(
        (_capacity_issue("LAUNCH_CAPACITY_INSUFFICIENT"),), evidence_sha=None
    )
    assert owner_action_executor._review_denial_reason(review) == "LIVE_REVIEW_DENIED"
