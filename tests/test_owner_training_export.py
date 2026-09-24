"""Outcome collection и read-only 90-дневный owner training export."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.database_migrations import apply_runtime_migrations
from services.owner_action_outcomes import (
    OutcomeHorizon,
    OutcomeMetricsContext,
    OwnerOutcomeConflict,
    OwnerOutcomeNotDue,
    collect_owner_action_outcome,
)
from services.owner_training_export import (
    OwnerTrainingExportRangeError,
    export_owner_training_dataset,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
ATTEMPT_AT = BASE + timedelta(days=10)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    """Создаёт отдельную мигрированную SQLite БД."""

    path = tmp_path / "owner-training.db"
    apply_runtime_migrations(str(path))
    yield path


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _insert_proposal(
    connection: sqlite3.Connection,
    *,
    name: str,
    number: int,
    created_at: datetime,
    target_count: int = 1,
) -> tuple[str, tuple[str, ...]]:
    proposal_id = f"proposal-{name}"
    connection.execute(
        """
        INSERT INTO owner_action_proposals (
            proposal_id, proposal_kind, origin, idempotency_key, source_ref,
            requested_by_actor, summary, plan_json, plan_sha256, targets_sha256,
            evidence_sha256, config_version_sha256, proposal_sha256,
            staged_media_root, created_at, valid_until
        ) VALUES (?, 'LAUNCH', 'CRON', ?, ?, 'scheduler', ?, '{}', ?, ?, ?, ?,
                  ?, NULL, ?, ?)
        """,
        (
            proposal_id,
            f"idempotency-{name}",
            f"source-{name}",
            f"proposal {name}",
            _sha("{}"),
            _sha(f"targets-{name}"),
            _sha(f"evidence-{name}"),
            _sha(f"config-{name}"),
            _sha(f"proposal-{name}"),
            _iso(created_at),
            _iso(created_at + timedelta(days=120)),
        ),
    )
    claim_ids: list[str] = []
    for ordinal in range(target_count):
        claim_id = f"claim-{name}-{ordinal}"
        claim_ids.append(claim_id)
        connection.execute(
            """
            INSERT INTO owner_action_proposal_targets (
                proposal_id, claim_id, ordinal, action_kind, account_id,
                adset_id, subject_id, city, language, intended_payload_json,
                intended_payload_sha256, created_at
            ) VALUES (?, ?, ?, 'CREATE_AD', ?, ?, ?, 'CityA', 'L1', '{}',
                      ?, ?)
            """,
            (
                proposal_id,
                claim_id,
                ordinal,
                f"account-{name}-{ordinal}",
                f"adset-{name}-{ordinal}",
                f"subject-{name}-{ordinal}",
                _sha("{}"),
                _iso(created_at),
            ),
        )
    connection.execute(
        """
        INSERT INTO owner_action_evidence (
            evidence_id, proposal_id, evidence_kind, source_system, subject_id,
            observed_at, complete, payload_json, payload_sha256, created_at
        ) VALUES (?, ?, 'LIVE_INVENTORY', 'META', ?, ?, 1, '{}', ?, ?)
        """,
        (
            f"evidence-{name}",
            proposal_id,
            f"subject-{name}-0",
            _iso(created_at),
            _sha("{}"),
            _iso(created_at),
        ),
    )
    connection.execute(
        """
        INSERT INTO owner_action_events (
            event_id, proposal_id, event_seq, event_type, actor,
            payload_json, payload_sha256, created_at
        ) VALUES (?, ?, 1, 'PROPOSED', 'scheduler', '{}', ?, ?)
        """,
        (
            f"event-{name}",
            proposal_id,
            _sha("{}"),
            _iso(created_at),
        ),
    )
    return proposal_id, tuple(claim_ids)


def _insert_decision(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    name: str,
    number: int,
    generation: int,
    decision_kind: str,
    recorded_at: datetime,
) -> str:
    delivery_id = f"delivery-{name}-{generation}"
    token_id = f"token-{name}-{generation}-{decision_kind.lower()}"
    decision_id = f"decision-{name}-{generation}-{decision_kind.lower()}"
    update_id = number * 100 + generation
    message_id = number * 1_000 + generation
    connection.execute(
        """
        INSERT INTO telegram_delivery_outbox (
            delivery_id, purpose, proposal_id, generation, dedupe_key,
            rendered_text, rendered_text_sha256, button_spec_json,
            button_spec_sha256, state, telegram_chat_id, telegram_message_id,
            created_at, sent_at
        ) VALUES (?, 'OWNER_PROPOSAL', ?, ?, ?, ?, ?, '[]', ?, 'SENT', 201,
                  ?, ?, ?)
        """,
        (
            delivery_id,
            proposal_id,
            generation,
            f"owner:{proposal_id}:{generation}",
            f"proposal {name}",
            _sha(f"rendered-{name}-{generation}"),
            _sha("[]"),
            message_id,
            _iso(recorded_at),
            _iso(recorded_at),
        ),
    )
    connection.execute(
        """
        INSERT INTO telegram_update_inbox (
            update_id, ingress_kind, bot_token_identity_sha256, raw_update_json,
            raw_update_sha256, state, received_at, processed_at
        ) VALUES (?, 'GET_UPDATES', ?, '{}', ?, 'PROCESSED', ?, ?)
        """,
        (
            update_id,
            _sha(f"bot-{name}-{generation}"),
            _sha(f"update-{name}-{generation}"),
            _iso(recorded_at),
            _iso(recorded_at),
        ),
    )
    connection.execute(
        """
        INSERT INTO owner_callback_tokens (
            token_id, public_nonce, token_mac_sha256, proposal_id, delivery_id,
            delivery_generation, decision_kind, expected_owner_user_id,
            expected_chat_id, expected_message_id, created_at, expires_at,
            bound_at, consumed_at, consumed_update_id,
            consumed_callback_query_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 101, 201, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            token_id,
            f"nonce-{name}-{generation}-{decision_kind.lower()}",
            _sha(f"token-{name}-{generation}-{decision_kind.lower()}"),
            proposal_id,
            delivery_id,
            generation,
            decision_kind,
            message_id,
            _iso(recorded_at),
            _iso(recorded_at + timedelta(days=1)),
            _iso(recorded_at),
            _iso(recorded_at),
            update_id,
            f"callback-{name}-{generation}",
        ),
    )
    proposal_sha256 = connection.execute(
        """
        SELECT proposal_sha256 FROM owner_action_proposals
        WHERE proposal_id = ?
        """,
        (proposal_id,),
    ).fetchone()
    assert proposal_sha256 is not None
    connection.execute(
        """
        INSERT INTO owner_action_decisions (
            decision_id, proposal_id, proposal_sha256, decision_kind,
            owner_user_id, chat_id, message_id, delivery_generation,
            telegram_update_id, callback_query_id, callback_token_id,
            trusted_ingress_sha256, reason_text, recorded_at
        ) VALUES (?, ?, ?, ?, 101, 201, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            proposal_id,
            str(proposal_sha256[0]),
            decision_kind,
            message_id,
            generation,
            update_id,
            f"callback-{name}-{generation}",
            token_id,
            _sha(f"ingress-{name}-{generation}"),
            f"reason {decision_kind.lower()}",
            _iso(recorded_at),
        ),
    )
    return decision_id


def _insert_decision_only_proposal(
    connection: sqlite3.Connection,
    *,
    name: str,
    number: int,
    created_at: datetime,
    decision_kind: str,
) -> str:
    proposal_id, _ = _insert_proposal(
        connection,
        name=name,
        number=number,
        created_at=created_at,
    )
    decision_id = _insert_decision(
        connection,
        proposal_id=proposal_id,
        name=name,
        number=number,
        generation=1,
        decision_kind=decision_kind,
        recorded_at=created_at + timedelta(minutes=5),
    )
    state = "REJECTED" if decision_kind == "REJECT" else "POSTPONED"
    connection.execute(
        """
        INSERT INTO owner_action_lifecycle (
            proposal_id, state, version, delivery_generation,
            active_decision_id, updated_at
        ) VALUES (?, ?, 1, 1, ?, ?)
        """,
        (
            proposal_id,
            state,
            decision_id,
            _iso(created_at + timedelta(minutes=5)),
        ),
    )
    return proposal_id


def _insert_approved_proposal(
    connection: sqlite3.Connection,
    *,
    name: str,
    number: int,
    created_at: datetime,
    attempt_states: tuple[str, ...],
    with_postpone: bool = False,
) -> tuple[str, tuple[str, ...], tuple[str, ...], str, str]:
    proposal_id, claim_ids = _insert_proposal(
        connection,
        name=name,
        number=number,
        created_at=created_at,
        target_count=len(attempt_states),
    )
    generation = 1
    if with_postpone:
        _insert_decision(
            connection,
            proposal_id=proposal_id,
            name=name,
            number=number,
            generation=generation,
            decision_kind="POSTPONE",
            recorded_at=created_at + timedelta(minutes=2),
        )
        generation += 1
    decision_id = _insert_decision(
        connection,
        proposal_id=proposal_id,
        name=name,
        number=number,
        generation=generation,
        decision_kind="APPROVE",
        recorded_at=created_at + timedelta(minutes=5),
    )
    job_id = f"job-{name}"
    connection.execute(
        """
        INSERT INTO owner_execution_jobs (
            job_id, proposal_id, decision_id, state, created_at, updated_at
        ) VALUES (?, ?, ?, 'REVIEWING', ?, ?)
        """,
        (
            job_id,
            proposal_id,
            decision_id,
            _iso(created_at + timedelta(minutes=5)),
            _iso(created_at + timedelta(minutes=5)),
        ),
    )
    connection.execute(
        """
        INSERT INTO owner_action_lifecycle (
            proposal_id, state, version, delivery_generation,
            active_decision_id, active_job_id, updated_at
        ) VALUES (?, 'LIVE_REVIEW', 1, ?, ?, ?, ?)
        """,
        (
            proposal_id,
            generation,
            decision_id,
            job_id,
            _iso(created_at + timedelta(minutes=5)),
        ),
    )

    attempt_ids: list[str] = []
    for ordinal, (claim_id, attempt_state) in enumerate(
        zip(claim_ids, attempt_states, strict=True)
    ):
        target = connection.execute(
            """
            SELECT action_kind, account_id, adset_id, intended_payload_sha256
            FROM owner_action_proposal_targets
            WHERE proposal_id = ? AND claim_id = ?
            """,
            (proposal_id, claim_id),
        ).fetchone()
        assert target is not None
        permit_id = f"permit-{name}-{ordinal}"
        attempt_id = f"attempt-{name}-{ordinal}"
        attempt_ids.append(attempt_id)
        connection.execute(
            """
            INSERT INTO owner_technical_permits (
                permit_id, secret_sha256, proposal_id, decision_id, job_id,
                claim_id, operation_kind, account_id, resource_id,
                exact_payload_sha256, manifest_json, manifest_sha256,
                live_evidence_sha256, phase, sequence_no, issued_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, 'ISSUED', 1,
                      ?, ?)
            """,
            (
                permit_id,
                _sha(f"secret-{name}-{ordinal}"),
                proposal_id,
                decision_id,
                job_id,
                claim_id,
                str(target[0]),
                str(target[1]),
                str(target[2]),
                str(target[3]),
                _sha(f"manifest-{name}-{ordinal}"),
                _sha(f"live-{name}-{ordinal}"),
                _iso(ATTEMPT_AT - timedelta(minutes=1)),
                _iso(ATTEMPT_AT + timedelta(minutes=5)),
            ),
        )
        connection.execute(
            """
            UPDATE owner_technical_permits
            SET phase = 'CONSUMED', consumed_at = ?
            WHERE permit_id = ?
            """,
            (_iso(ATTEMPT_AT), permit_id),
        )
        connection.execute(
            """
            INSERT INTO owner_action_attempts (
                attempt_id, permit_id, proposal_id, decision_id, job_id,
                claim_id, operation_kind, account_id, resource_id,
                exact_payload_sha256, state, provider_request_id, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ATTEMPT_STARTED', ?, ?)
            """,
            (
                attempt_id,
                permit_id,
                proposal_id,
                decision_id,
                job_id,
                claim_id,
                str(target[0]),
                str(target[1]),
                str(target[2]),
                str(target[3]),
                f"request-{name}-{ordinal}",
                _iso(ATTEMPT_AT),
            ),
        )
        connection.execute(
            """
            UPDATE owner_action_attempts
            SET state = ?, provider_result_sha256 = ?, completed_at = ?,
                last_reason_code = ?
            WHERE attempt_id = ?
            """,
            (
                attempt_state,
                _sha(f"provider-{name}-{ordinal}"),
                _iso(ATTEMPT_AT),
                attempt_state,
                attempt_id,
            ),
        )
    return (
        proposal_id,
        claim_ids,
        tuple(attempt_ids),
        decision_id,
        job_id,
    )


def _set_aggregate_state(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    job_id: str,
    final_state: str,
) -> None:
    connection.execute(
        """
        UPDATE owner_action_lifecycle
        SET state = 'PERMIT_ISSUED', version = 2, updated_at = ?
        WHERE proposal_id = ?
        """,
        (_iso(ATTEMPT_AT), proposal_id),
    )
    connection.execute(
        """
        UPDATE owner_action_lifecycle
        SET state = 'ATTEMPT_STARTED', version = 3, updated_at = ?
        WHERE proposal_id = ?
        """,
        (_iso(ATTEMPT_AT), proposal_id),
    )
    if final_state == "VERIFIED":
        connection.execute(
            """
            UPDATE owner_action_lifecycle
            SET state = 'EXECUTED', version = 4, updated_at = ?
            WHERE proposal_id = ?
            """,
            (_iso(ATTEMPT_AT), proposal_id),
        )
        connection.execute(
            """
            UPDATE owner_action_lifecycle
            SET state = 'VERIFYING', version = 5, updated_at = ?
            WHERE proposal_id = ?
            """,
            (_iso(ATTEMPT_AT), proposal_id),
        )
        connection.execute(
            """
            UPDATE owner_action_lifecycle
            SET state = 'VERIFIED', version = 6, updated_at = ?
            WHERE proposal_id = ?
            """,
            (_iso(ATTEMPT_AT), proposal_id),
        )
        job_state = "COMPLETE"
    else:
        connection.execute(
            """
            UPDATE owner_action_lifecycle
            SET state = 'RECONCILE_REQUIRED', version = 4, updated_at = ?
            WHERE proposal_id = ?
            """,
            (_iso(ATTEMPT_AT), proposal_id),
        )
        job_state = "RECONCILE_REQUIRED"
    connection.execute(
        """
        UPDATE owner_execution_jobs
        SET state = ?, updated_at = ?
        WHERE job_id = ?
        """,
        (job_state, _iso(ATTEMPT_AT), job_id),
    )


def _insert_watchdog(
    connection: sqlite3.Connection,
    *,
    name: str,
    proposal_id: str,
    decision_id: str,
    job_id: str,
    claim_ids: tuple[str, ...],
    verified_count: int,
    state: str,
) -> None:
    watchdog_id = f"watchdog-{name}"
    verified_at = _iso(ATTEMPT_AT) if state == "VERIFIED" else None
    connection.execute(
        """
        INSERT INTO launch_watchdogs (
            watchdog_id, proposal_id, decision_id, job_id, state,
            expected_count, verified_count, verification_attempts,
            verify_deadline_at, created_at, updated_at, verified_at,
            last_reason_code
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            watchdog_id,
            proposal_id,
            decision_id,
            job_id,
            state,
            len(claim_ids),
            verified_count,
            _iso(ATTEMPT_AT + timedelta(days=1)),
            _iso(ATTEMPT_AT),
            _iso(ATTEMPT_AT),
            verified_at,
            state,
        ),
    )
    for ordinal, claim_id in enumerate(claim_ids):
        connection.execute(
            """
            INSERT INTO launch_watchdog_targets (
                proposal_id, watchdog_id, claim_id, account_id, adset_id,
                expected_ad_name, expected_fingerprint, created_ad_id,
                expected_configured_status, expected_effective_status,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'ACTIVE', ?)
            """,
            (
                proposal_id,
                watchdog_id,
                claim_id,
                f"account-{name}-{ordinal}",
                f"adset-{name}-{ordinal}",
                f"ad-{name}-{ordinal}",
                _sha(f"fingerprint-{name}-{ordinal}"),
                f"created-{name}-{ordinal}",
                _iso(ATTEMPT_AT),
            ),
        )
    observation_outcome = "VERIFIED" if state == "VERIFIED" else "UNKNOWN"
    evidence_json = json.dumps(
        {"verified_count": verified_count},
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        """
        INSERT INTO launch_verification_observations (
            observation_id, watchdog_id, attempt_no, fetch_complete,
            verified_count, outcome, evidence_json, evidence_sha256,
            observed_at
        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"observation-{name}",
            watchdog_id,
            1 if state == "VERIFIED" else 0,
            verified_count,
            observation_outcome,
            evidence_json,
            _sha(evidence_json),
            _iso(ATTEMPT_AT),
        ),
    )
    connection.execute(
        """
        INSERT INTO scheduler_action_runs (
            run_id, scheduler_name, slot_key, state, proposal_id, watchdog_id,
            created_at, updated_at, verified_at, last_reason_code
        ) VALUES (?, 'owner-launch', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"run-{name}",
            f"slot-{name}",
            "VERIFIED" if state == "VERIFIED" else "VERIFYING",
            proposal_id,
            watchdog_id,
            _iso(ATTEMPT_AT),
            _iso(ATTEMPT_AT),
            verified_at,
            state,
        ),
    )


def _create_export_fixture(db_path: Path) -> dict[str, object]:
    with _connect(db_path) as connection:
        _insert_decision_only_proposal(
            connection,
            name="before",
            number=1,
            created_at=BASE - timedelta(seconds=1),
            decision_kind="REJECT",
        )
        rejected = _insert_decision_only_proposal(
            connection,
            name="rejected",
            number=2,
            created_at=BASE,
            decision_kind="REJECT",
        )
        postponed = _insert_decision_only_proposal(
            connection,
            name="postponed",
            number=3,
            created_at=BASE + timedelta(days=2),
            decision_kind="POSTPONE",
        )
        (
            verified,
            verified_claims,
            _,
            verified_decision,
            verified_job,
        ) = _insert_approved_proposal(
            connection,
            name="verified",
            number=4,
            created_at=BASE + timedelta(days=3),
            attempt_states=("CONFIRMED",),
            with_postpone=True,
        )
        _set_aggregate_state(
            connection,
            proposal_id=verified,
            job_id=verified_job,
            final_state="VERIFIED",
        )
        _insert_watchdog(
            connection,
            name="verified",
            proposal_id=verified,
            decision_id=verified_decision,
            job_id=verified_job,
            claim_ids=verified_claims,
            verified_count=1,
            state="VERIFIED",
        )
        (
            reconcile,
            reconcile_claims,
            _,
            reconcile_decision,
            reconcile_job,
        ) = _insert_approved_proposal(
            connection,
            name="reconcile",
            number=5,
            created_at=BASE + timedelta(days=4),
            attempt_states=("CONFIRMED", "RECONCILE_REQUIRED"),
        )
        _set_aggregate_state(
            connection,
            proposal_id=reconcile,
            job_id=reconcile_job,
            final_state="RECONCILE_REQUIRED",
        )
        _insert_watchdog(
            connection,
            name="reconcile",
            proposal_id=reconcile,
            decision_id=reconcile_decision,
            job_id=reconcile_job,
            claim_ids=reconcile_claims,
            verified_count=1,
            state="RECONCILE_REQUIRED",
        )
        _insert_decision_only_proposal(
            connection,
            name="at-end",
            number=6,
            created_at=BASE + timedelta(days=90),
            decision_kind="REJECT",
        )

    for claim_id, metric in (
        (verified_claims[0], 11),
        (reconcile_claims[0], 22),
        (reconcile_claims[1], 33),
    ):
        proposal_id = (
            verified if claim_id in verified_claims else reconcile
        )
        collect_owner_action_outcome(
            db_path,
            proposal_id=proposal_id,
            claim_id=claim_id,
            horizon=OutcomeHorizon.IMMEDIATE,
            metrics_reader=lambda _context, value=metric: {
                "spend": value,
                "source": "fixture",
            },
            now=ATTEMPT_AT,
        )
    collect_owner_action_outcome(
        db_path,
        proposal_id=verified,
        claim_id=verified_claims[0],
        horizon=OutcomeHorizon.D1,
        metrics_reader=lambda _context: {"spend": 12, "source": "fixture"},
        now=ATTEMPT_AT + timedelta(days=1),
    )
    return {
        "rejected": rejected,
        "postponed": postponed,
        "verified": verified,
        "verified_claim": verified_claims[0],
        "reconcile": reconcile,
        "reconcile_claims": reconcile_claims,
    }


def test_outcome_horizons_are_immutable_and_idempotent(
    db_path: Path,
) -> None:
    """Exact replay дедуплицируется, другое содержимое конфликтует."""

    with _connect(db_path) as connection:
        proposal_id, claim_ids, _, _, _ = _insert_approved_proposal(
            connection,
            name="outcome",
            number=10,
            created_at=BASE,
            attempt_states=("CONFIRMED",),
        )
    contexts: list[OutcomeMetricsContext] = []

    def read_metrics(context: OutcomeMetricsContext) -> dict[str, object]:
        contexts.append(context)
        return {"leads": 3, "revenue": 150_000}

    first = collect_owner_action_outcome(
        db_path,
        proposal_id=proposal_id,
        claim_id=claim_ids[0],
        horizon=OutcomeHorizon.IMMEDIATE,
        metrics_reader=read_metrics,
        now=ATTEMPT_AT,
    )
    replay = collect_owner_action_outcome(
        db_path,
        proposal_id=proposal_id,
        claim_id=claim_ids[0],
        horizon=OutcomeHorizon.IMMEDIATE,
        metrics_reader=read_metrics,
        now=ATTEMPT_AT,
    )

    assert first.deduplicated is False
    assert replay.deduplicated is True
    assert replay.outcome_id == first.outcome_id
    assert contexts[0].attempt_state == "CONFIRMED"
    with pytest.raises(
        OwnerOutcomeConflict,
        match="OUTCOME_HORIZON_CONFLICT",
    ):
        collect_owner_action_outcome(
            db_path,
            proposal_id=proposal_id,
            claim_id=claim_ids[0],
            horizon=OutcomeHorizon.IMMEDIATE,
            metrics_reader=lambda _context: {"leads": 4},
            now=ATTEMPT_AT,
        )
    with pytest.raises(
        OwnerOutcomeConflict,
        match="OUTCOME_HORIZON_CONFLICT",
    ):
        collect_owner_action_outcome(
            db_path,
            proposal_id=proposal_id,
            claim_id=claim_ids[0],
            horizon=OutcomeHorizon.IMMEDIATE,
            metrics_reader=lambda _context: {
                "leads": 3,
                "revenue": 150_000,
            },
            now=ATTEMPT_AT + timedelta(seconds=1),
        )

    with _connect(db_path) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT horizon)
            FROM owner_action_outcomes
            WHERE proposal_id = ? AND claim_id = ?
            """,
            (proposal_id, claim_ids[0]),
        ).fetchone() == (1, 1)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE owner_action_outcomes SET observed_at = observed_at
                WHERE outcome_id = ?
                """,
                (first.outcome_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM owner_action_outcomes WHERE outcome_id = ?",
                (first.outcome_id,),
            )


@pytest.mark.parametrize(
    ("horizon", "days"),
    [
        (OutcomeHorizon.IMMEDIATE, 0),
        (OutcomeHorizon.D1, 1),
        (OutcomeHorizon.D3, 3),
        (OutcomeHorizon.D7, 7),
        (OutcomeHorizon.D30, 30),
    ],
)
def test_outcome_collection_supports_every_due_horizon(
    db_path: Path,
    horizon: OutcomeHorizon,
    days: int,
) -> None:
    """Каждый утверждённый horizon сохраняется только после срока."""

    name = f"horizon-{horizon.value.lower()}"
    with _connect(db_path) as connection:
        proposal_id, claim_ids, _, _, _ = _insert_approved_proposal(
            connection,
            name=name,
            number=20 + days,
            created_at=BASE,
            attempt_states=("CONFIRMED",),
        )
    if days:
        with pytest.raises(OwnerOutcomeNotDue):
            collect_owner_action_outcome(
                db_path,
                proposal_id=proposal_id,
                claim_id=claim_ids[0],
                horizon=horizon,
                metrics_reader=lambda _context: {"spend": 1},
                now=ATTEMPT_AT + timedelta(days=days, seconds=-1),
            )

    result = collect_owner_action_outcome(
        db_path,
        proposal_id=proposal_id,
        claim_id=claim_ids[0],
        horizon=horizon,
        metrics_reader=lambda _context: {"spend": 1},
        now=ATTEMPT_AT + timedelta(days=days),
    )

    assert result.horizon is horizon
    assert result.deduplicated is False


def test_90_day_export_has_full_owner_lineage_and_exact_bounds(
    db_path: Path,
) -> None:
    """Полуоткрытый 90-дневный интервал содержит все требуемые цепочки."""

    ids = _create_export_fixture(db_path)
    before_counts: tuple[int, ...]
    with _connect(db_path) as connection:
        before_counts = tuple(
            connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "owner_action_proposals",
                "owner_action_decisions",
                "owner_action_attempts",
                "owner_action_outcomes",
            )
        )
    sink = io.StringIO()

    summary = export_owner_training_dataset(
        db_path=db_path,
        date_from=BASE,
        date_to=BASE + timedelta(days=90),
        sink=sink,
    )
    repeated_sink = io.StringIO()
    repeated = export_owner_training_dataset(
        db_path=db_path,
        date_from=BASE,
        date_to=BASE + timedelta(days=90),
        sink=repeated_sink,
    )

    rows = [json.loads(line) for line in sink.getvalue().splitlines()]
    by_id = {row["proposal"]["proposal_id"]: row for row in rows}
    assert set(by_id) == {
        ids["rejected"],
        ids["postponed"],
        ids["verified"],
        ids["reconcile"],
    }
    assert summary.proposal_count == 4
    assert summary.decision_count == 5
    assert summary.claim_count == 5
    assert summary.attempt_count == 3
    assert summary.outcome_count == 4
    assert summary.verification_observation_count == 2
    assert repeated_sink.getvalue() == sink.getvalue()
    assert repeated.content_sha256 == summary.content_sha256
    assert hashlib.sha256(sink.getvalue().encode()).hexdigest() == (
        summary.content_sha256
    )

    rejected = by_id[str(ids["rejected"])]
    postponed = by_id[str(ids["postponed"])]
    verified = by_id[str(ids["verified"])]
    reconcile = by_id[str(ids["reconcile"])]
    assert [item["decision_kind"] for item in rejected["owner_decisions"]] == [
        "REJECT"
    ]
    assert [item["decision_kind"] for item in postponed["owner_decisions"]] == [
        "POSTPONE"
    ]
    assert [item["decision_kind"] for item in verified["owner_decisions"]] == [
        "POSTPONE",
        "APPROVE",
    ]
    verified_attempt = verified["claims"][0]["attempts"][0]
    assert verified_attempt["state"] == "CONFIRMED"
    assert [item["horizon"] for item in verified_attempt["outcomes"]] == [
        "IMMEDIATE",
        "D1",
    ]
    assert verified["launch_verification"][0]["state"] == "VERIFIED"
    assert verified["launch_verification"][0]["observations"][0]["outcome"] == (
        "VERIFIED"
    )
    assert [claim["attempts"][0]["state"] for claim in reconcile["claims"]] == [
        "CONFIRMED",
        "RECONCILE_REQUIRED",
    ]
    assert reconcile["launch_verification"][0]["verified_count"] == 1
    assert reconcile["launch_verification"][0]["expected_count"] == 2
    assert reconcile["launch_verification"][0]["observations"][0]["outcome"] == (
        "UNKNOWN"
    )

    with _connect(db_path) as connection:
        after_counts = tuple(
            connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "owner_action_proposals",
                "owner_action_decisions",
                "owner_action_attempts",
                "owner_action_outcomes",
            )
        )
    assert after_counts == before_counts


@pytest.mark.parametrize(
    ("date_from", "date_to"),
    [
        (BASE, BASE),
        (BASE + timedelta(days=1), BASE),
        (BASE, BASE + timedelta(days=90, seconds=1)),
        (
            datetime(2026, 1, 1),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        ),
    ],
)
def test_export_rejects_invalid_or_over_90_day_range(
    db_path: Path,
    date_from: datetime,
    date_to: datetime,
) -> None:
    """Невалидный период отклоняется до чтения dataset."""

    with pytest.raises(OwnerTrainingExportRangeError):
        export_owner_training_dataset(
            db_path=db_path,
            date_from=date_from,
            date_to=date_to,
            sink=io.StringIO(),
        )


def test_training_modules_have_strict_read_only_import_boundary() -> None:
    """Training/outcome код не импортирует mutation или permit issuer."""

    project_root = Path(__file__).resolve().parent.parent
    module_paths = (
        project_root / "services" / "owner_action_outcomes.py",
        project_root / "services" / "owner_training_export.py",
    )
    forbidden_modules = {
        "integrations.facebook",
        "integrations.facebook_ads_mutation_transport",
        "services.action_gateway",
        "services.action_gateway_core",
        "services.owner_action_executor",
        "services.owner_action_repository",
    }
    forbidden_calls = {
        "execute_action",
        "issue_technical_permit",
        "consume_technical_permit",
        "create_ad",
        "set_ad_status",
        "set_adset_budget",
    }
    for path in module_paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        called_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called_names.add(node.func.attr)
        assert imported.isdisjoint(forbidden_modules), path.name
        assert called_names.isdisjoint(forbidden_calls), path.name
        assert "requests" not in imported
        assert "owner_technical_permits" not in source
