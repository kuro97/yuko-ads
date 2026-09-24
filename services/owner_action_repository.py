"""SQLite repository для immutable proposal и exact-lineage CAS.

В модуле нет внешнего I/O. Каждая связанная смена состояния выполняется в одной
``BEGIN IMMEDIATE`` транзакции и либо фиксируется целиком, либо откатывается.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from services.owner_action_models import (
    ActionAttemptAttestation,
    AttemptTransitionResult,
    ClaimProgress,
    EvidenceRecord,
    LifecycleState,
    OwnerDecisionKind,
    OwnerDecisionResult,
    ProposalKind,
    ProposalOrigin,
    ProposalPage,
    ProposalReceipt,
    ProposalView,
    ProposedActionPlan,
    ProposedTarget,
    PreparedOwnerRedelivery,
    PermitRetirementReason,
    SystemRedeliveryReason,
    TechnicalPermit,
    canonical_json,
    canonical_sha256,
    evidence_document,
    plan_document,
    proposal_hashes,
    target_document,
)


# Задание ещё «открыто»: диспетчер или live review могут его двигать.
_OPEN_EXECUTION_JOB_STATES = ("QUEUED", "REVIEWING", "WAITING_RETRY", "PERMIT_ISSUED")

# Предложение «в полёте»: ждёт владельца или исполняется гейтвеем. Продюсер с
# durable-попыткой (auto_launch) по этому признаку отличает исполняющуюся
# попытку от осиротевшей после падения процесса. EXECUTED/VERIFYING и
# терминальные состояния сюда не входят: провайдер уже отработал, сверка по
# живому инвентарю там уместна.
_IN_FLIGHT_LIFECYCLE_STATES = (
    LifecycleState.DELIVERY_PENDING.value,
    LifecycleState.PENDING_OWNER.value,
    LifecycleState.POSTPONED.value,
    LifecycleState.APPROVED.value,
    LifecycleState.EXECUTION_QUEUED.value,
    LifecycleState.LIVE_REVIEW.value,
    LifecycleState.EXECUTION_RETRY_WAIT.value,
    LifecycleState.PERMIT_ISSUED.value,
    LifecycleState.ATTEMPT_STARTED.value,
)
# Задание исполнителя, у которого провайдер ещё впереди либо прямо сейчас.
_IN_FLIGHT_EXECUTION_JOB_STATES = (
    "QUEUED",
    "WAITING_RETRY",
    "REVIEWING",
    "PERMIT_ISSUED",
    "ATTEMPT_STARTED",
)

# Парное состояние задания для остановочных переходов lifecycle. Остальные
# переходы задание двигают сами (queue_execution / begin_claim_live_review /
# issue_technical_permit / transition_attempt) и здесь не участвуют.
_JOB_STATE_BY_LIFECYCLE = {
    LifecycleState.BLOCKED_STALE.value: "BLOCKED_STALE",
    LifecycleState.FAILED_NO_EFFECT.value: "FAILED_NO_EFFECT",
    LifecycleState.EXECUTION_RETRY_WAIT.value: "WAITING_RETRY",
    # Протухшее одобренное закрывается терминально той же транзакцией, что и
    # lifecycle: задание уходит из клейма и из стража зависаний. Своего
    # состояния EXPIRED у заданий нет (CHECK в схеме) — терминальную пару даёт
    # BLOCKED_STALE, истинную причину несут lifecycle и last_reason_code.
    LifecycleState.EXPIRED.value: "BLOCKED_STALE",
}


class OwnerActionRepositoryError(RuntimeError):
    """Базовая fail-closed ошибка owner-action persistence."""


class OwnerActionIdempotencyConflict(OwnerActionRepositoryError):
    """Idempotency key уже связан с другим immutable plan."""


class OwnerActionLifecycleConflict(OwnerActionRepositoryError):
    """CAS не выполнен из-за состояния, версии или параллельного worker."""


class OwnerActionLineageError(OwnerActionRepositoryError):
    """Связи proposal/decision/job/permit/claim не совпали."""


class OwnerActionTokenUnavailable(OwnerActionRepositoryError):
    """Callback token не привязан, истёк, отозван или уже использован."""


class OwnerActionPermitUnavailable(OwnerActionRepositoryError):
    """Technical permit не может начать provider attempt."""


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} должен содержать timezone")


def _iso(value: datetime) -> str:
    _require_aware(value, "datetime")
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str):
        raise OwnerActionRepositoryError("В БД сохранён невалидный datetime")
    parsed = datetime.fromisoformat(value)
    _require_aware(parsed, "datetime")
    return parsed


def _optional_iso(value: object) -> datetime | None:
    return None if value is None else _parse_iso(value)


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} должен быть непустой строкой")
    return value


def _require_sha256(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} должен быть lowercase SHA-256")
    return value


def _proposal_sha256(
    *,
    proposal_id: str,
    plan_sha256: str,
    targets_sha256: str,
    evidence_sha256: str,
    config_version_sha256: str,
    created_at: datetime,
    valid_until: datetime,
) -> str:
    return canonical_sha256(
        {
            "proposal_version": 1,
            "proposal_id": proposal_id,
            "plan_sha256": plan_sha256,
            "targets_sha256": targets_sha256,
            "evidence_sha256": evidence_sha256,
            "config_version_sha256": config_version_sha256,
            "created_at": created_at,
            "valid_until": valid_until,
        }
    )


def _event_payload(payload: Mapping[str, object] | None) -> tuple[str, str]:
    encoded = canonical_json(payload or {})
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()


def _callback_token_mac(
    *,
    secret: str,
    nonce: str,
    proposal_id: str,
    generation: int,
    decision: OwnerDecisionKind,
) -> str:
    material = (
        f"v1|{nonce}|{proposal_id}|{generation}|{decision.value}"
    ).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


class OwnerActionRepository:
    """Тонкая repository-граница поверх утверждённого контракта migration 022."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        if not self._db_path:
            raise ValueError("db_path обязателен")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        proposal_id: str,
        event_type: str,
        actor: str,
        reason_code: str | None,
        payload: Mapping[str, object] | None,
        now: datetime,
    ) -> AttemptTransitionResult:
        payload_json, payload_sha256 = _event_payload(payload)
        row = connection.execute(
            """
            SELECT COALESCE(MAX(event_seq), 0) + 1 AS next_seq
            FROM owner_action_events
            WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise OwnerActionRepositoryError("Не удалось получить event sequence")
        connection.execute(
            """
            INSERT INTO owner_action_events (
                event_id, proposal_id, event_seq, event_type, actor,
                reason_code, payload_json, payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                proposal_id,
                int(row["next_seq"]),
                _required_text(event_type, "event_type"),
                _required_text(actor, "actor"),
                reason_code,
                payload_json,
                payload_sha256,
                _iso(now),
            ),
        )

    def propose_action(
        self,
        plan: ProposedActionPlan,
        *,
        now: datetime | None = None,
    ) -> ProposalReceipt:
        created_at = now or datetime.now(timezone.utc)
        _require_aware(created_at, "now")
        if plan.valid_until <= created_at:
            raise ValueError("valid_until должен быть позже created_at")
        hashes = proposal_hashes(plan)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT p.proposal_id, p.plan_sha256, p.proposal_sha256,
                       p.created_at, p.valid_until, l.state
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                WHERE p.idempotency_key = ?
                """,
                (plan.idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["plan_sha256"]) != hashes.plan_sha256:
                    raise OwnerActionIdempotencyConflict("IDEMPOTENCY_PAYLOAD_CONFLICT")
                state = str(existing["state"])
                receipt_state = (
                    "DELIVERY_PENDING" if state == "DELIVERY_PENDING" else "PENDING_OWNER"
                )
                connection.commit()
                return ProposalReceipt(
                    proposal_id=str(existing["proposal_id"]),
                    idempotency_key=plan.idempotency_key,
                    state=receipt_state,
                    proposal_sha256=str(existing["proposal_sha256"]),
                    created_at=_parse_iso(existing["created_at"]),
                    valid_until=_parse_iso(existing["valid_until"]),
                    deduplicated=True,
                )

            proposal_id = str(uuid.uuid4())
            proposal_sha256 = _proposal_sha256(
                proposal_id=proposal_id,
                plan_sha256=hashes.plan_sha256,
                targets_sha256=hashes.targets_sha256,
                evidence_sha256=hashes.evidence_sha256,
                config_version_sha256=plan.config_version_sha256,
                created_at=created_at,
                valid_until=plan.valid_until,
            )
            connection.execute(
                """
                INSERT INTO owner_action_proposals (
                    proposal_id, proposal_version, proposal_kind, origin,
                    idempotency_key, source_ref, requested_by_actor, summary,
                    plan_json, plan_sha256, targets_sha256, evidence_sha256,
                    config_version_sha256, proposal_sha256, staged_media_root,
                    created_at, valid_until
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    plan.proposal_kind.value,
                    plan.origin.value,
                    plan.idempotency_key,
                    plan.source_ref,
                    plan.actor,
                    plan.summary,
                    hashes.plan_json,
                    hashes.plan_sha256,
                    hashes.targets_sha256,
                    hashes.evidence_sha256,
                    plan.config_version_sha256,
                    proposal_sha256,
                    str(plan.staged_media_root) if plan.staged_media_root else None,
                    _iso(created_at),
                    _iso(plan.valid_until),
                ),
            )
            for target in plan.targets:
                connection.execute(
                    """
                    INSERT INTO owner_action_proposal_targets (
                        proposal_id, claim_id, ordinal, action_kind, account_id,
                        adset_id, subject_id, city, language,
                        intended_payload_json, intended_payload_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal_id,
                        target.claim_id,
                        target.ordinal,
                        target.action_kind,
                        target.account_id,
                        target.adset_id,
                        target.subject_id,
                        target.city,
                        target.language,
                        canonical_json(target.intended_payload).decode("utf-8"),
                        target.intended_payload_sha256,
                        _iso(created_at),
                    ),
                )
            for evidence in plan.evidence:
                connection.execute(
                    """
                    INSERT INTO owner_action_evidence (
                        evidence_id, proposal_id, evidence_kind, source_system,
                        subject_id, observed_at, complete, payload_json,
                        payload_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        proposal_id,
                        evidence.evidence_kind,
                        evidence.source_system,
                        evidence.subject_id,
                        _iso(evidence.observed_at),
                        int(evidence.complete),
                        canonical_json(evidence.payload).decode("utf-8"),
                        evidence.payload_sha256,
                        _iso(created_at),
                    ),
                )
            connection.execute(
                """
                INSERT INTO owner_action_lifecycle (
                    proposal_id, state, version, delivery_generation, updated_at
                ) VALUES (?, 'DELIVERY_PENDING', 1, 0, ?)
                """,
                (proposal_id, _iso(created_at)),
            )
            self._append_event(
                connection,
                proposal_id=proposal_id,
                event_type="PROPOSAL_CREATED",
                actor=plan.actor,
                reason_code=None,
                payload={
                    "plan_sha256": hashes.plan_sha256,
                    "targets_sha256": hashes.targets_sha256,
                    "evidence_sha256": hashes.evidence_sha256,
                    "proposal_sha256": proposal_sha256,
                },
                now=created_at,
            )
            connection.commit()
            return ProposalReceipt(
                proposal_id=proposal_id,
                idempotency_key=plan.idempotency_key,
                state="DELIVERY_PENDING",
                proposal_sha256=proposal_sha256,
                created_at=created_at.astimezone(timezone.utc),
                valid_until=plan.valid_until.astimezone(timezone.utc),
                deduplicated=False,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def find_receipt_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        targets_sha256: str,
    ) -> ProposalReceipt | None:
        """Read-only: receipt уже сохранённого proposal с тем же intent.

        Сравнение идёт по ``targets_sha256`` — это стабильная часть плана: она
        не зависит от времени наблюдения (``observed_at``, ``valid_until``) и от
        метрик evidence. Повтор того же действия так остаётся идемпотентным, а
        расхождение intent находкой не считается: такой конфликт по-прежнему
        поднимает ``propose_action``.
        """
        _required_text(idempotency_key, "idempotency_key")
        _require_sha256(targets_sha256, "targets_sha256")
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT p.proposal_id, p.targets_sha256, p.proposal_sha256,
                       p.created_at, p.valid_until, l.state
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                WHERE p.idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or str(row["targets_sha256"]) != targets_sha256:
            return None
        state = str(row["state"])
        return ProposalReceipt(
            proposal_id=str(row["proposal_id"]),
            idempotency_key=idempotency_key,
            state=(
                "DELIVERY_PENDING" if state == "DELIVERY_PENDING" else "PENDING_OWNER"
            ),
            proposal_sha256=str(row["proposal_sha256"]),
            created_at=_parse_iso(row["created_at"]),
            valid_until=_parse_iso(row["valid_until"]),
            deduplicated=True,
        )

    @staticmethod
    def _decode_plan(plan_json: str) -> ProposedActionPlan:
        try:
            raw = json.loads(plan_json)
            targets = tuple(
                ProposedTarget(
                    claim_id=item["claim_id"],
                    ordinal=item["ordinal"],
                    action_kind=item["action_kind"],
                    account_id=item["account_id"],
                    adset_id=item["adset_id"],
                    subject_id=item["subject_id"],
                    city=item["city"],
                    language=item["language"],
                    intended_payload=item["intended_payload"],
                    intended_payload_sha256=item["intended_payload_sha256"],
                )
                for item in raw["targets"]
            )
            evidence = tuple(
                EvidenceRecord(
                    evidence_kind=item["evidence_kind"],
                    source_system=item["source_system"],
                    subject_id=item["subject_id"],
                    observed_at=_parse_iso(item["observed_at"]),
                    complete=item["complete"],
                    payload=item["payload"],
                    payload_sha256=item["payload_sha256"],
                )
                for item in raw["evidence"]
            )
            return ProposedActionPlan(
                proposal_kind=ProposalKind(raw["proposal_kind"]),
                origin=ProposalOrigin(raw["origin"]),
                idempotency_key=raw["idempotency_key"],
                source_ref=raw["source_ref"],
                actor=raw["actor"],
                summary=raw["summary"],
                targets=targets,
                evidence=evidence,
                config_version_sha256=raw["config_version_sha256"],
                valid_until=_parse_iso(raw["valid_until"]),
                staged_media_root=(
                    Path(raw["staged_media_root"]) if raw["staged_media_root"] else None
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OwnerActionRepositoryError("Сохранённый proposal plan повреждён") from exc

    def _view_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ProposalView:
        plan_json = str(row["plan_json"])
        hashes = proposal_hashes(self._decode_plan(plan_json))
        if plan_json.encode("utf-8") != canonical_json(plan_document(self._decode_plan(plan_json))):
            raise OwnerActionRepositoryError("plan_json не canonical")
        expected_hashes = (
            hashes.plan_sha256,
            hashes.targets_sha256,
            hashes.evidence_sha256,
        )
        stored_hashes = (
            str(row["plan_sha256"]),
            str(row["targets_sha256"]),
            str(row["evidence_sha256"]),
        )
        if expected_hashes != stored_hashes:
            raise OwnerActionRepositoryError("Proposal hashes не совпадают с plan_json")

        plan = self._decode_plan(plan_json)
        target_rows = connection.execute(
            """
            SELECT claim_id, ordinal, action_kind, account_id, adset_id,
                   subject_id, city, language, intended_payload_json,
                   intended_payload_sha256
            FROM owner_action_proposal_targets
            WHERE proposal_id = ?
            ORDER BY ordinal
            """,
            (row["proposal_id"],),
        ).fetchall()
        stored_target_documents = tuple(
            {
                "claim_id": str(target["claim_id"]),
                "ordinal": int(target["ordinal"]),
                "action_kind": str(target["action_kind"]),
                "account_id": str(target["account_id"]),
                "adset_id": target["adset_id"],
                "subject_id": str(target["subject_id"]),
                "city": target["city"],
                "language": target["language"],
                "intended_payload": json.loads(str(target["intended_payload_json"])),
                "intended_payload_sha256": str(target["intended_payload_sha256"]),
            }
            for target in target_rows
        )
        if canonical_sha256(stored_target_documents) != hashes.targets_sha256:
            raise OwnerActionRepositoryError("Target rows не совпадают с proposal")
        if canonical_sha256(tuple(target_document(item) for item in plan.targets)) != hashes.targets_sha256:
            raise OwnerActionRepositoryError("Target plan повреждён")

        evidence_rows = connection.execute(
            """
            SELECT evidence_kind, source_system, subject_id, observed_at,
                   complete, payload_json, payload_sha256
            FROM owner_action_evidence
            WHERE proposal_id = ?
            ORDER BY evidence_kind, subject_id, payload_sha256
            """,
            (row["proposal_id"],),
        ).fetchall()
        stored_evidence_documents = tuple(
            {
                "evidence_kind": str(evidence["evidence_kind"]),
                "source_system": str(evidence["source_system"]),
                "subject_id": str(evidence["subject_id"]),
                "observed_at": _parse_iso(evidence["observed_at"]),
                "complete": bool(evidence["complete"]),
                "payload": json.loads(str(evidence["payload_json"])),
                "payload_sha256": str(evidence["payload_sha256"]),
            }
            for evidence in evidence_rows
        )
        plan_evidence_documents = tuple(
            evidence_document(item) for item in plan.evidence
        )
        if canonical_sha256(stored_evidence_documents) != canonical_sha256(
            sorted(
                plan_evidence_documents,
                key=lambda item: (
                    str(item["evidence_kind"]),
                    str(item["subject_id"]),
                    str(item["payload_sha256"]),
                ),
            )
        ):
            raise OwnerActionRepositoryError("Evidence rows не совпадают с proposal")
        if canonical_sha256(plan_evidence_documents) != hashes.evidence_sha256:
            raise OwnerActionRepositoryError("Evidence plan повреждён")

        created_at = _parse_iso(row["created_at"])
        valid_until = _parse_iso(row["valid_until"])
        expected_proposal_sha256 = _proposal_sha256(
            proposal_id=str(row["proposal_id"]),
            plan_sha256=hashes.plan_sha256,
            targets_sha256=hashes.targets_sha256,
            evidence_sha256=hashes.evidence_sha256,
            config_version_sha256=plan.config_version_sha256,
            created_at=created_at,
            valid_until=valid_until,
        )
        if expected_proposal_sha256 != str(row["proposal_sha256"]):
            raise OwnerActionRepositoryError("proposal_sha256 повреждён")
        return ProposalView(
            proposal_id=str(row["proposal_id"]),
            plan=plan,
            proposal_sha256=expected_proposal_sha256,
            plan_sha256=hashes.plan_sha256,
            targets_sha256=hashes.targets_sha256,
            evidence_sha256=hashes.evidence_sha256,
            state=str(row["state"]),
            lifecycle_version=int(row["version"]),
            delivery_generation=int(row["delivery_generation"]),
            active_decision_id=(
                str(row["active_decision_id"])
                if row["active_decision_id"] is not None
                else None
            ),
            active_job_id=(
                str(row["active_job_id"]) if row["active_job_id"] is not None else None
            ),
            latest_reason_code=(
                str(row["latest_reason_code"])
                if row["latest_reason_code"] is not None
                else None
            ),
            next_action_at=_optional_iso(row["next_action_at"]),
            created_at=created_at,
            # Колонка есть только у запросов с join на решения (get_proposal);
            # list_proposals собирает view без неё — там approved_at не нужен.
            approved_at=(
                _optional_iso(row["approved_recorded_at"])
                if "approved_recorded_at" in row.keys()
                else None
            ),
        )

    def get_proposal(self, proposal_id: str) -> ProposalView | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT p.*, l.state, l.version, l.delivery_generation,
                       l.active_decision_id, l.active_job_id,
                       l.latest_reason_code, l.next_action_at,
                       d.recorded_at AS approved_recorded_at
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                LEFT JOIN owner_action_decisions d
                  ON d.decision_id = l.active_decision_id
                 AND d.decision_kind = 'APPROVE'
                WHERE p.proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
            return None if row is None else self._view_from_row(connection, row)
        finally:
            connection.close()

    def find_lifecycle_by_source_ref(
        self,
        source_ref: str,
    ) -> tuple[str, str] | None:
        """Read-only: ``(proposal_id, lifecycle state)`` по идемпотентному scope.

        Producer'ы кладут в ``source_ref`` детерминированный scope действия
        (например ``replacement:<identity>:<ad_id>``), а ``idempotency_key``
        уникален — значит одному scope соответствует не больше одного proposal.
        Нужен продюсерам-долгожителям (replacement workflow), чтобы узнать
        судьбу собственного предложения: исполнено оно или владелец отказал.
        """
        _required_text(source_ref, "source_ref")
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT p.proposal_id, l.state
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                WHERE p.source_ref = ?
                ORDER BY p.created_at DESC, p.proposal_id DESC
                LIMIT 1
                """,
                (source_ref,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return str(row["proposal_id"]), str(row["state"])

    def proposal_in_flight_by_idempotency_key(self, idempotency_key: str) -> bool:
        """Read-only: предложение по ключу ждёт владельца или исполняется.

        Нужен продюсерам, чья durable-попытка живёт дольше одного процесса
        (auto_launch): цепочка из N объявлений исполняется по одному claim за
        тик исполнителя и часами стоит в QUEUED/WAITING_RETRY/REVIEWING, а
        между стейджингом и одобрением предложение ждёт владельца. Сверять
        такую попытку по живому инвентарю как «зависшую после падения» нельзя —
        она исполняется. False — ключ свободен либо предложение уже прошло
        провайдера (EXECUTED/VERIFYING/VERIFIED) или терминально.
        """
        _required_text(idempotency_key, "idempotency_key")
        lifecycle_states = ",".join("?" for _ in _IN_FLIGHT_LIFECYCLE_STATES)
        job_states = ",".join("?" for _ in _IN_FLIGHT_EXECUTION_JOB_STATES)
        connection = self._connect()
        try:
            row = connection.execute(
                f"""
                SELECT 1
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                LEFT JOIN owner_execution_jobs j USING (proposal_id)
                WHERE p.idempotency_key = ?
                  AND (l.state IN ({lifecycle_states}) OR j.state IN ({job_states}))
                LIMIT 1
                """,
                (
                    idempotency_key,
                    *_IN_FLIGHT_LIFECYCLE_STATES,
                    *_IN_FLIGHT_EXECUTION_JOB_STATES,
                ),
            ).fetchone()
        finally:
            connection.close()
        return row is not None

    def find_live_proposal_for_subject(
        self,
        *,
        subject_id: str,
        action_kind: str,
        now: datetime | None = None,
    ) -> ProposalReceipt | None:
        """Read-only: живое предложение того же действия по тому же объекту.

        «Живое» — ещё ждёт владельца: ``DELIVERY_PENDING`` (готовится/копится
        в дайджест), ``PENDING_OWNER`` (карточка у него на руках) и
        ``POSTPONED`` (отложено им самим). Терминальные и уже исполняемые
        состояния живыми не считаются: по ним предложить действие заново можно
        и нужно. Протухшее (``valid_until`` в прошлом) тоже не живое: висяк,
        который свипер ещё не смёл, не должен давить новые предложения по
        тому же объекту.

        Зачем: producer'ы кладут в ``source_ref`` scope с run_id прогона
        (``autopilot-live:<uuid4>:<ad_id>``), поэтому ``find_lifecycle_by_source_ref``
        для дедупа не годится — каждые 15 минут это новый scope и новый
        idempotency-ключ. Стабилизировать scope нельзя: в payload лежат
        ``producer_inventory_sha256``/``sibling_active_ids``, которые меняются
        между прогонами, и тот же scope с другим payload даёт
        ``IDEMPOTENCY_PAYLOAD_CONFLICT``. Поэтому дедуп идёт по бизнес-ключу
        (объект + вид действия), а не по ключу идемпотентности.
        """
        _required_text(subject_id, "subject_id")
        _required_text(action_kind, "action_kind")
        checked_at = now or datetime.now(timezone.utc)
        _require_aware(checked_at, "now")
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT p.proposal_id, p.idempotency_key, p.proposal_sha256,
                       p.created_at, p.valid_until, l.state
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                JOIN owner_action_proposal_targets t
                  ON t.proposal_id = p.proposal_id
                WHERE t.subject_id = ? AND t.action_kind = ?
                  AND l.state IN ('DELIVERY_PENDING','PENDING_OWNER','POSTPONED')
                  AND p.valid_until > ?
                ORDER BY p.created_at DESC, p.proposal_id DESC
                LIMIT 1
                """,
                (subject_id, action_kind, _iso(checked_at)),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        state = str(row["state"])
        return ProposalReceipt(
            proposal_id=str(row["proposal_id"]),
            idempotency_key=str(row["idempotency_key"]),
            # POSTPONED в receipt не существует: владелец карточку уже видел.
            state="DELIVERY_PENDING" if state == "DELIVERY_PENDING" else "PENDING_OWNER",
            proposal_sha256=str(row["proposal_sha256"]),
            created_at=_parse_iso(row["created_at"]),
            valid_until=_parse_iso(row["valid_until"]),
            deduplicated=True,
        )

    def list_proposals(
        self,
        *,
        state: str | None,
        cursor: str | None,
        limit: int = 50,
    ) -> ProposalPage:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit должен быть в диапазоне 1..100")
        cursor_created_at: str | None = None
        cursor_proposal_id: str | None = None
        if cursor is not None:
            try:
                decoded = json.loads(
                    base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
                )
                cursor_created_at = str(decoded["created_at"])
                cursor_proposal_id = str(decoded["proposal_id"])
            except (ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("cursor невалиден") from exc

        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT p.*, l.state, l.version, l.delivery_generation,
                       l.active_decision_id, l.active_job_id,
                       l.latest_reason_code, l.next_action_at
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                WHERE (? IS NULL OR l.state = ?)
                  AND (
                      ? IS NULL
                      OR p.created_at < ?
                      OR (p.created_at = ? AND p.proposal_id < ?)
                  )
                ORDER BY p.created_at DESC, p.proposal_id DESC
                LIMIT ?
                """,
                (
                    state,
                    state,
                    cursor_created_at,
                    cursor_created_at,
                    cursor_created_at,
                    cursor_proposal_id,
                    limit + 1,
                ),
            ).fetchall()
            page_rows = rows[:limit]
            items = tuple(self._view_from_row(connection, row) for row in page_rows)
            next_cursor = None
            if len(rows) > limit and page_rows:
                last = page_rows[-1]
                cursor_payload = canonical_json(
                    {
                        "created_at": str(last["created_at"]),
                        "proposal_id": str(last["proposal_id"]),
                    }
                )
                next_cursor = base64.urlsafe_b64encode(cursor_payload).decode("ascii")
            return ProposalPage(items=items, next_cursor=next_cursor)
        finally:
            connection.close()

    def prepare_system_redelivery(
        self,
        proposal_id: str,
        *,
        expected_lifecycle_version: int,
        reason: SystemRedeliveryReason,
        rendered_text: str,
        owner_user_id: int,
        chat_id: int,
        callback_secret: str,
        actor: str,
        now: datetime | None = None,
    ) -> PreparedOwnerRedelivery:
        """Атомарно заменяет кнопки без создания owner decision или job.

        Telegram ``sendMessage`` выполняется вызывающим кодом только после commit.
        До успешной привязки нового ``message_id`` lifecycle остаётся
        ``DELIVERY_PENDING``, поэтому ни старые, ни новые кнопки не доверены.
        """

        created_at = now or datetime.now(timezone.utc)
        _require_aware(created_at, "now")
        if not isinstance(reason, SystemRedeliveryReason):
            raise ValueError("reason должен быть SystemRedeliveryReason")
        _required_text(rendered_text, "rendered_text")
        _required_text(callback_secret, "callback_secret")
        _required_text(actor, "actor")
        if (
            not isinstance(owner_user_id, int)
            or isinstance(owner_user_id, bool)
            or owner_user_id <= 0
        ):
            raise ValueError("owner_user_id должен быть положительным integer")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            raise ValueError("chat_id должен быть integer")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            proposal = connection.execute(
                """
                SELECT p.valid_until, l.state, l.version, l.delivery_generation,
                       l.active_decision_id, l.active_job_id
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                WHERE p.proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise OwnerActionLineageError("Proposal не найден")
            if (
                str(proposal["state"]) != LifecycleState.PENDING_OWNER.value
                or int(proposal["version"]) != expected_lifecycle_version
            ):
                raise OwnerActionLifecycleConflict("REDELIVERY_LIFECYCLE_CAS_CONFLICT")
            if (
                proposal["active_decision_id"] is not None
                or proposal["active_job_id"] is not None
            ):
                raise OwnerActionLineageError("Redelivery запрещён после decision/job")
            persisted_lineage = connection.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM owner_action_decisions
                        WHERE proposal_id = ?
                    ) AS has_decision,
                    EXISTS(
                        SELECT 1 FROM owner_execution_jobs
                        WHERE proposal_id = ?
                    ) AS has_job
                """,
                (proposal_id, proposal_id),
            ).fetchone()
            if persisted_lineage is None or any(int(value) for value in persisted_lineage):
                raise OwnerActionLineageError("Redelivery требует proposal без decision/job")
            valid_until = _parse_iso(proposal["valid_until"])
            if valid_until <= created_at:
                raise OwnerActionTokenUnavailable("PROPOSAL_EXPIRED")

            generation = int(proposal["delivery_generation"]) + 1
            maximum_generation = connection.execute(
                """
                SELECT COALESCE(MAX(generation), 0)
                FROM telegram_delivery_outbox
                WHERE proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
            if (
                maximum_generation is None
                or int(maximum_generation[0]) != int(proposal["delivery_generation"])
            ):
                raise OwnerActionLifecycleConflict("REDELIVERY_GENERATION_DRIFT")

            delivery_id = str(uuid.uuid4())
            token_rows: list[tuple[str, str, str, OwnerDecisionKind]] = []
            button_spec: list[dict[str, str]] = []
            for decision in OwnerDecisionKind:
                nonce = base64.urlsafe_b64encode(
                    secrets.token_bytes(12)
                ).decode("ascii").rstrip("=")
                token_rows.append(
                    (
                        str(uuid.uuid4()),
                        nonce,
                        _callback_token_mac(
                            secret=callback_secret,
                            nonce=nonce,
                            proposal_id=proposal_id,
                            generation=generation,
                            decision=decision,
                        ),
                        decision,
                    )
                )
                button_spec.append({"decision": decision.value, "nonce": nonce})

            button_spec_json = canonical_json(button_spec).decode("utf-8")
            connection.execute(
                """
                INSERT INTO telegram_delivery_outbox (
                    delivery_id, purpose, proposal_id, generation, dedupe_key,
                    rendered_text, rendered_text_sha256, button_spec_json,
                    button_spec_sha256, state, attempts, next_attempt_at, created_at
                ) VALUES (
                    ?, 'OWNER_PROPOSAL', ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?
                )
                """,
                (
                    delivery_id,
                    proposal_id,
                    generation,
                    f"owner-proposal:{proposal_id}:generation:{generation}",
                    rendered_text,
                    hashlib.sha256(rendered_text.encode("utf-8")).hexdigest(),
                    button_spec_json,
                    canonical_sha256(button_spec),
                    _iso(created_at),
                    _iso(created_at),
                ),
            )
            for token_id, nonce, full_mac, decision in token_rows:
                connection.execute(
                    """
                    INSERT INTO owner_callback_tokens (
                        token_id, public_nonce, token_mac_sha256, proposal_id,
                        delivery_id, delivery_generation, decision_kind,
                        expected_owner_user_id, expected_chat_id, created_at,
                        expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        token_id,
                        nonce,
                        full_mac,
                        proposal_id,
                        delivery_id,
                        generation,
                        decision.value,
                        owner_user_id,
                        chat_id,
                        _iso(created_at),
                        _iso(valid_until),
                    ),
                )
            connection.execute(
                """
                UPDATE owner_callback_tokens
                SET revoked_at = COALESCE(revoked_at, ?), revoke_reason = ?
                WHERE proposal_id = ?
                  AND delivery_generation < ?
                  AND consumed_at IS NULL
                """,
                (_iso(created_at), reason.value, proposal_id, generation),
            )
            connection.execute(
                """
                UPDATE telegram_delivery_outbox
                SET state = 'FAILED_VISIBLE', lease_token = NULL,
                    lease_until = NULL, last_error_code = ?
                WHERE proposal_id = ?
                  AND generation < ?
                  AND state IN ('PENDING','LEASED')
                """,
                (reason.value, proposal_id, generation),
            )
            updated = connection.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = 'DELIVERY_PENDING', version = version + 1,
                    delivery_generation = ?, latest_reason_code = ?,
                    next_action_at = ?, updated_at = ?
                WHERE proposal_id = ? AND state = 'PENDING_OWNER'
                  AND version = ?
                  AND delivery_generation = ?
                  AND active_decision_id IS NULL
                  AND active_job_id IS NULL
                """,
                (
                    generation,
                    reason.value,
                    _iso(created_at),
                    _iso(created_at),
                    proposal_id,
                    expected_lifecycle_version,
                    generation - 1,
                ),
            ).rowcount
            if updated != 1:
                raise OwnerActionLifecycleConflict("REDELIVERY_LIFECYCLE_CAS_CONFLICT")
            self._append_event(
                connection,
                proposal_id=proposal_id,
                event_type="SYSTEM_REDELIVERY_PREPARED",
                actor=actor,
                reason_code=reason.value,
                payload={
                    "delivery_id": delivery_id,
                    "generation": generation,
                    "previous_generation": generation - 1,
                },
                now=created_at,
            )
            connection.commit()
            return PreparedOwnerRedelivery(
                delivery_id=delivery_id,
                proposal_id=proposal_id,
                generation=generation,
                lifecycle_version=expected_lifecycle_version + 1,
                state="PENDING",
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_active_lineage(
        connection: sqlite3.Connection,
        *,
        proposal_id: str,
        decision_id: str | None,
        job_id: str | None,
    ) -> None:
        if decision_id is not None:
            decision = connection.execute(
                """
                SELECT 1 FROM owner_action_decisions
                WHERE proposal_id = ? AND decision_id = ?
                """,
                (proposal_id, decision_id),
            ).fetchone()
            if decision is None:
                raise OwnerActionLineageError("active decision не принадлежит proposal")
        if job_id is not None:
            if decision_id is None:
                raise OwnerActionLineageError("Job требует active decision")
            job = connection.execute(
                """
                SELECT 1 FROM owner_execution_jobs
                WHERE proposal_id = ? AND decision_id = ? AND job_id = ?
                """,
                (proposal_id, decision_id, job_id),
            ).fetchone()
            if job is None:
                raise OwnerActionLineageError("active job не принадлежит decision")

    def transition_lifecycle(
        self,
        proposal_id: str,
        *,
        expected_state: str,
        expected_version: int,
        new_state: str,
        actor: str,
        reason_code: str | None = None,
        active_decision_id: str | None = None,
        active_job_id: str | None = None,
        delivery_generation: int | None = None,
        next_action_at: datetime | None = None,
        now: datetime | None = None,
    ) -> int:
        changed_at = now or datetime.now(timezone.utc)
        _require_aware(changed_at, "now")
        if next_action_at is not None:
            _require_aware(next_action_at, "next_action_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT active_decision_id, active_job_id, delivery_generation
                FROM owner_action_lifecycle
                WHERE proposal_id = ? AND state = ? AND version = ?
                """,
                (proposal_id, expected_state, expected_version),
            ).fetchone()
            if row is None:
                raise OwnerActionLifecycleConflict("LIFECYCLE_CAS_CONFLICT")
            resolved_decision_id = active_decision_id or row["active_decision_id"]
            resolved_job_id = active_job_id or row["active_job_id"]
            current_generation = int(row["delivery_generation"])
            resolved_generation = (
                current_generation
                if delivery_generation is None
                else delivery_generation
            )
            if (
                not isinstance(resolved_generation, int)
                or isinstance(resolved_generation, bool)
                or resolved_generation < current_generation
            ):
                raise OwnerActionLifecycleConflict("DELIVERY_GENERATION_REGRESSION")
            self._validate_active_lineage(
                connection,
                proposal_id=proposal_id,
                decision_id=resolved_decision_id,
                job_id=resolved_job_id,
            )
            updated = connection.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = ?, version = version + 1,
                    delivery_generation = ?,
                    active_decision_id = ?, active_job_id = ?,
                    latest_reason_code = ?, next_action_at = ?, updated_at = ?
                WHERE proposal_id = ? AND state = ? AND version = ?
                """,
                (
                    new_state,
                    resolved_generation,
                    resolved_decision_id,
                    resolved_job_id,
                    reason_code,
                    _iso(next_action_at) if next_action_at else None,
                    _iso(changed_at),
                    proposal_id,
                    expected_state,
                    expected_version,
                ),
            ).rowcount
            if updated != 1:
                raise OwnerActionLifecycleConflict("LIFECYCLE_CAS_CONFLICT")
            self._sync_execution_job_state(
                connection,
                proposal_id=proposal_id,
                decision_id=resolved_decision_id,
                job_id=resolved_job_id,
                new_state=new_state,
                reason_code=reason_code,
                next_action_at=next_action_at,
                now=changed_at,
            )
            self._append_event(
                connection,
                proposal_id=proposal_id,
                event_type=f"LIFECYCLE_{new_state}",
                actor=actor,
                reason_code=reason_code,
                payload={
                    "from_state": expected_state,
                    "to_state": new_state,
                    "from_version": expected_version,
                    "to_version": expected_version + 1,
                    "delivery_generation": resolved_generation,
                },
                now=changed_at,
            )
            connection.commit()
            return expected_version + 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _sync_execution_job_state(
        connection: sqlite3.Connection,
        *,
        proposal_id: str,
        decision_id: str | None,
        job_id: str | None,
        new_state: str,
        reason_code: str | None,
        next_action_at: datetime | None,
        now: datetime,
    ) -> None:
        """Задание идёт следом за lifecycle, а не остаётся висеть в REVIEWING.

        Переход lifecycle двигал только ``owner_action_lifecycle``. Из-за этого
        задание, упавшее после начала live review, навсегда застревало в
        ``REVIEWING``: диспетчер берёт только QUEUED/WAITING_RETRY, а страж
        зависаний терминальный lifecycle не тревожит. Здесь задание переводится
        в парное состояние тем же transaction, что и сам lifecycle.
        """

        job_state = _JOB_STATE_BY_LIFECYCLE.get(new_state)
        if job_state is None or not decision_id or not job_id:
            return
        retry_at = _iso(next_action_at or now) if job_state == "WAITING_RETRY" else None
        placeholders = ",".join("?" for _ in _OPEN_EXECUTION_JOB_STATES)
        connection.execute(
            f"""
            UPDATE owner_execution_jobs
            SET state = ?, last_reason_code = ?, next_attempt_at = ?,
                lease_token = NULL, lease_until = NULL, updated_at = ?
            WHERE proposal_id = ? AND decision_id = ? AND job_id = ?
              AND state IN ({placeholders})
            """,
            (
                job_state,
                reason_code,
                retry_at,
                _iso(now),
                proposal_id,
                decision_id,
                job_id,
                *_OPEN_EXECUTION_JOB_STATES,
            ),
        )

    def _record_owner_decision(
        self,
        *,
        proposal_id: str,
        token_id: str,
        decision: OwnerDecisionKind,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
        delivery_generation: int,
        telegram_update_id: int,
        callback_query_id: str,
        trusted_ingress_sha256: str,
        reason_text: str | None,
        actor: str,
        expected_lifecycle_version: int | None = None,
        now: datetime | None = None,
    ) -> OwnerDecisionResult:
        """Единственная repository-транзакция записи trusted owner decision."""

        recorded_at = now or datetime.now(timezone.utc)
        _require_aware(recorded_at, "now")
        if (
            not isinstance(owner_user_id, int)
            or isinstance(owner_user_id, bool)
            or owner_user_id <= 0
        ):
            raise ValueError("owner_user_id должен быть положительным integer")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            raise ValueError("chat_id должен быть integer")
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            raise ValueError("message_id должен быть положительным integer")
        _require_sha256(trusted_ingress_sha256, "trusted_ingress_sha256")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                """
                SELECT d.proposal_id, d.decision_id, d.decision_kind,
                       d.callback_token_id, l.state
                FROM owner_action_decisions d
                JOIN owner_action_lifecycle l USING (proposal_id)
                WHERE d.telegram_update_id = ? OR d.callback_query_id = ?
                """,
                (telegram_update_id, callback_query_id),
            ).fetchone()
            if replay is not None:
                if (
                    str(replay["proposal_id"]) != proposal_id
                    or str(replay["callback_token_id"]) != token_id
                    or str(replay["decision_kind"]) != decision.value
                ):
                    raise OwnerActionLineageError("Callback replay lineage не совпала")
                connection.commit()
                return OwnerDecisionResult(
                    proposal_id=str(replay["proposal_id"]),
                    decision_id=str(replay["decision_id"]),
                    decision=OwnerDecisionKind(str(replay["decision_kind"])),
                    state=str(replay["state"]),
                    accepted=False,
                    reason_code="CALLBACK_REPLAY",
                )

            proposal = connection.execute(
                """
                SELECT p.proposal_sha256, p.valid_until, l.state, l.version,
                       l.delivery_generation
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                WHERE p.proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise OwnerActionLineageError("Proposal не найден")
            if str(proposal["state"]) != LifecycleState.PENDING_OWNER.value:
                raise OwnerActionLifecycleConflict("PROPOSAL_NOT_PENDING_OWNER")
            version = int(proposal["version"])
            if expected_lifecycle_version is not None and version != expected_lifecycle_version:
                raise OwnerActionLifecycleConflict("LIFECYCLE_CAS_CONFLICT")
            if int(proposal["delivery_generation"]) != delivery_generation:
                raise OwnerActionTokenUnavailable("DELIVERY_GENERATION_MISMATCH")
            if _parse_iso(proposal["valid_until"]) <= recorded_at:
                raise OwnerActionTokenUnavailable("PROPOSAL_EXPIRED")

            inbox = connection.execute(
                """
                SELECT raw_update_sha256
                FROM telegram_update_inbox
                WHERE update_id = ? AND state IN ('RECEIVED','PROCESSING','PROCESSED')
                """,
                (telegram_update_id,),
            ).fetchone()
            if inbox is None or str(inbox["raw_update_sha256"]) != trusted_ingress_sha256:
                raise OwnerActionLineageError("Trusted Telegram ingress не совпал")

            token = connection.execute(
                """
                SELECT *
                FROM owner_callback_tokens
                WHERE token_id = ?
                """,
                (token_id,),
            ).fetchone()
            if token is None:
                raise OwnerActionTokenUnavailable("TOKEN_NOT_FOUND")
            token_matches = (
                str(token["proposal_id"]) == proposal_id,
                str(token["decision_kind"]) == decision.value,
                int(token["delivery_generation"]) == delivery_generation,
                int(token["expected_owner_user_id"]) == owner_user_id,
                int(token["expected_chat_id"]) == chat_id,
                token["expected_message_id"] is not None
                and int(token["expected_message_id"]) == message_id,
                token["bound_at"] is not None,
                token["consumed_at"] is None,
                token["revoked_at"] is None,
                _parse_iso(token["expires_at"]) > recorded_at,
            )
            if not all(token_matches):
                raise OwnerActionTokenUnavailable("TOKEN_BINDING_MISMATCH")
            consumed = connection.execute(
                """
                UPDATE owner_callback_tokens
                SET consumed_at = ?, consumed_update_id = ?,
                    consumed_callback_query_id = ?
                WHERE token_id = ?
                  AND consumed_at IS NULL
                  AND revoked_at IS NULL
                  AND expected_message_id = ?
                  AND expires_at > ?
                """,
                (
                    _iso(recorded_at),
                    telegram_update_id,
                    callback_query_id,
                    token_id,
                    message_id,
                    _iso(recorded_at),
                ),
            ).rowcount
            if consumed != 1:
                raise OwnerActionTokenUnavailable("TOKEN_CONSUME_CAS_CONFLICT")

            decision_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO owner_action_decisions (
                    decision_id, proposal_id, proposal_sha256, decision_kind,
                    owner_user_id, chat_id, message_id, delivery_generation,
                    telegram_update_id, callback_query_id, callback_token_id,
                    trusted_ingress_sha256, reason_text, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    proposal_id,
                    str(proposal["proposal_sha256"]),
                    decision.value,
                    owner_user_id,
                    chat_id,
                    message_id,
                    delivery_generation,
                    telegram_update_id,
                    callback_query_id,
                    token_id,
                    trusted_ingress_sha256,
                    reason_text,
                    _iso(recorded_at),
                ),
            )
            job_id: str | None = None
            if decision is OwnerDecisionKind.APPROVE:
                job_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO owner_execution_jobs (
                        job_id, proposal_id, decision_id, state, attempts,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'QUEUED', 0, ?, ?)
                    """,
                    (
                        job_id,
                        proposal_id,
                        decision_id,
                        _iso(recorded_at),
                        _iso(recorded_at),
                    ),
                )
                new_state = LifecycleState.APPROVED.value
            elif decision is OwnerDecisionKind.REJECT:
                new_state = LifecycleState.REJECTED.value
            else:
                new_state = LifecycleState.POSTPONED.value

            updated = connection.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = ?, version = version + 1,
                    active_decision_id = ?, active_job_id = ?,
                    latest_reason_code = ?, next_action_at = NULL, updated_at = ?
                WHERE proposal_id = ? AND state = 'PENDING_OWNER' AND version = ?
                """,
                (
                    new_state,
                    decision_id,
                    job_id,
                    f"OWNER_{decision.value}",
                    _iso(recorded_at),
                    proposal_id,
                    version,
                ),
            ).rowcount
            if updated != 1:
                raise OwnerActionLifecycleConflict("DECISION_LIFECYCLE_CAS_CONFLICT")
            self._append_event(
                connection,
                proposal_id=proposal_id,
                event_type=f"OWNER_{decision.value}",
                actor=actor,
                reason_code=f"OWNER_{decision.value}",
                payload={
                    "decision_id": decision_id,
                    "job_id": job_id,
                    "delivery_generation": delivery_generation,
                    "telegram_update_id": telegram_update_id,
                },
                now=recorded_at,
            )
            connection.commit()
            return OwnerDecisionResult(
                proposal_id=proposal_id,
                decision_id=decision_id,
                decision=decision,
                state=new_state,
                accepted=True,
                reason_code=f"OWNER_{decision.value}",
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def approve_by_system(
        self,
        *,
        proposal_id: str,
        automation_rule: str,
        actor: str,
        reason_text: str | None = None,
        evidence: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> OwnerDecisionResult:
        """Самоодобрение бота — единственная альтернатива кнопке владельца.

        Существует ровно для одного класса действий (автономная пауза
        подтверждённого слива, решение владельца). Ничего не упрощает
        в исполнении: после этой записи задание проходит ТОТ ЖЕ путь, что и
        одобренное владельцем — диспетчер, живой перечит состояния, permit,
        транспорт с аттестациями, независимая верификация факта.

        Отличие от ``_record_owner_decision`` ровно одно и оно намеренно
        видимо в данных: строка решения помечена ``decision_source='SYSTEM'``
        и несёт ``automation_rule`` — машинное имя правила, по которому бот
        решил сам. Ни телеграм-родословной, ни owner_user_id у неё нет и быть
        не может (табличный CHECK). Так владелец в аудите и датасет обучения
        всегда отличают «нажал владелец» от «решил бот».

        Одобрять можно ТОЛЬКО предложение, которое ещё не уехало владельцу
        (lifecycle ``DELIVERY_PENDING``). Если доставка уже успела перевести
        его в ``PENDING_OWNER``, самоодобрение честно отказывается: предложение
        остаётся обычным, с кнопками — двух решений по одному предложению не
        бывает.
        """

        recorded_at = now or datetime.now(timezone.utc)
        _require_aware(recorded_at, "now")
        rule = _required_text(automation_rule, "automation_rule")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            proposal = connection.execute(
                """
                SELECT p.proposal_sha256, p.valid_until, l.state, l.version
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                WHERE p.proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise OwnerActionLineageError("Proposal не найден")
            if str(proposal["state"]) != LifecycleState.DELIVERY_PENDING.value:
                raise OwnerActionLifecycleConflict("PROPOSAL_NOT_SELF_APPROVABLE")
            if _parse_iso(proposal["valid_until"]) <= recorded_at:
                raise OwnerActionTokenUnavailable("PROPOSAL_EXPIRED")
            existing = connection.execute(
                "SELECT 1 FROM owner_action_decisions WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if existing is not None:
                raise OwnerActionLifecycleConflict("DECISION_ALREADY_RECORDED")

            version = int(proposal["version"])
            decision_id = str(uuid.uuid4())
            job_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO owner_action_decisions (
                    decision_id, proposal_id, proposal_sha256, decision_kind,
                    reason_text, recorded_at, decision_source, automation_rule
                ) VALUES (?, ?, ?, 'APPROVE', ?, ?, 'SYSTEM', ?)
                """,
                (
                    decision_id,
                    proposal_id,
                    str(proposal["proposal_sha256"]),
                    reason_text,
                    _iso(recorded_at),
                    rule,
                ),
            )
            connection.execute(
                """
                INSERT INTO owner_execution_jobs (
                    job_id, proposal_id, decision_id, state, attempts,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'QUEUED', 0, ?, ?)
                """,
                (
                    job_id,
                    proposal_id,
                    decision_id,
                    _iso(recorded_at),
                    _iso(recorded_at),
                ),
            )
            updated = connection.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = ?, version = version + 1,
                    active_decision_id = ?, active_job_id = ?,
                    latest_reason_code = 'SYSTEM_APPROVE',
                    next_action_at = NULL, updated_at = ?
                WHERE proposal_id = ? AND state = 'DELIVERY_PENDING' AND version = ?
                """,
                (
                    LifecycleState.APPROVED.value,
                    decision_id,
                    job_id,
                    _iso(recorded_at),
                    proposal_id,
                    version,
                ),
            ).rowcount
            if updated != 1:
                raise OwnerActionLifecycleConflict("DECISION_LIFECYCLE_CAS_CONFLICT")
            self._append_event(
                connection,
                proposal_id=proposal_id,
                event_type="SYSTEM_APPROVE",
                actor=actor,
                reason_code="SYSTEM_APPROVE",
                payload={
                    "decision_id": decision_id,
                    "job_id": job_id,
                    "automation_rule": rule,
                    "decision_source": "SYSTEM",
                    "evidence": dict(evidence or {}),
                },
                now=recorded_at,
            )
            connection.commit()
            return OwnerDecisionResult(
                proposal_id=proposal_id,
                decision_id=decision_id,
                decision=OwnerDecisionKind.APPROVE,
                state=LifecycleState.APPROVED.value,
                accepted=True,
                reason_code="SYSTEM_APPROVE",
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _claim_progress_in_transaction(
        connection: sqlite3.Connection,
        *,
        proposal_id: str,
        decision_id: str,
        job_id: str,
        lifecycle_version: int,
    ) -> ClaimProgress:
        rows = connection.execute(
            """
            SELECT t.claim_id, t.ordinal, a.state AS attempt_state,
                   EXISTS(
                       SELECT 1
                       FROM owner_technical_permits p
                       WHERE p.proposal_id = t.proposal_id
                         AND p.decision_id = ?
                         AND p.job_id = ?
                         AND p.claim_id = t.claim_id
                         AND p.phase = 'ISSUED'
                   ) AS has_issued_permit
            FROM owner_action_proposal_targets t
            LEFT JOIN owner_action_attempts a
              ON a.proposal_id = t.proposal_id
             AND a.job_id = ?
             AND a.claim_id = t.claim_id
            WHERE t.proposal_id = ?
            ORDER BY t.ordinal
            """,
            (decision_id, job_id, job_id, proposal_id),
        ).fetchall()
        if not rows:
            raise OwnerActionLineageError("Proposal targets не найдены")

        attempt_states = [
            str(row["attempt_state"]) if row["attempt_state"] is not None else None
            for row in rows
        ]
        issued_permits = [bool(row["has_issued_permit"]) for row in rows]
        if "RECONCILE_REQUIRED" in attempt_states:
            return ClaimProgress(
                proposal_id=proposal_id,
                decision_id=decision_id,
                job_id=job_id,
                state="RECONCILE_REQUIRED",
                next_claim_id=None,
                next_ordinal=None,
                completed_claims=sum(
                    state in {"CONFIRMED", "FAILED_NO_EFFECT"}
                    for state in attempt_states
                ),
                total_claims=len(rows),
                lifecycle_version=lifecycle_version,
            )

        completed_claims = 0
        saw_failed_no_effect = False
        for index, row in enumerate(rows):
            attempt_state = attempt_states[index]
            if attempt_state in {"CONFIRMED", "FAILED_NO_EFFECT"}:
                completed_claims += 1
                saw_failed_no_effect |= attempt_state == "FAILED_NO_EFFECT"
                continue
            if any(state is not None for state in attempt_states[index + 1 :]):
                raise OwnerActionLineageError("Attempt ordinals нарушают последовательность")
            if not issued_permits[index] and any(issued_permits[index + 1 :]):
                raise OwnerActionLineageError("Permit ordinals нарушают последовательность")
            if attempt_state == "ATTEMPT_STARTED" or issued_permits[index]:
                return ClaimProgress(
                    proposal_id=proposal_id,
                    decision_id=decision_id,
                    job_id=job_id,
                    state="IN_FLIGHT",
                    next_claim_id=str(row["claim_id"]),
                    next_ordinal=int(row["ordinal"]),
                    completed_claims=completed_claims,
                    total_claims=len(rows),
                    lifecycle_version=lifecycle_version,
                )
            if attempt_state is not None:
                raise OwnerActionLineageError("Неизвестное attempt state")
            return ClaimProgress(
                proposal_id=proposal_id,
                decision_id=decision_id,
                job_id=job_id,
                state="READY",
                next_claim_id=str(row["claim_id"]),
                next_ordinal=int(row["ordinal"]),
                completed_claims=completed_claims,
                total_claims=len(rows),
                lifecycle_version=lifecycle_version,
            )

        return ClaimProgress(
            proposal_id=proposal_id,
            decision_id=decision_id,
            job_id=job_id,
            state="FAILED_NO_EFFECT" if saw_failed_no_effect else "COMPLETE",
            next_claim_id=None,
            next_ordinal=None,
            completed_claims=completed_claims,
            total_claims=len(rows),
            lifecycle_version=lifecycle_version,
        )

    def get_claim_progress(
        self,
        *,
        proposal_id: str,
        decision_id: str,
        job_id: str,
    ) -> ClaimProgress:
        """Возвращает только следующий ordinal из exact approved lineage."""

        connection = self._connect()
        try:
            lineage = connection.execute(
                """
                SELECT l.version, l.active_decision_id, l.active_job_id,
                       d.decision_kind
                FROM owner_action_lifecycle l
                JOIN owner_action_decisions d
                  ON d.proposal_id = l.proposal_id AND d.decision_id = ?
                JOIN owner_execution_jobs j
                  ON j.proposal_id = d.proposal_id
                 AND j.decision_id = d.decision_id
                 AND j.job_id = ?
                WHERE l.proposal_id = ?
                """,
                (decision_id, job_id, proposal_id),
            ).fetchone()
            if (
                lineage is None
                or str(lineage["active_decision_id"]) != decision_id
                or str(lineage["active_job_id"]) != job_id
                or str(lineage["decision_kind"]) != OwnerDecisionKind.APPROVE.value
            ):
                raise OwnerActionLineageError("Claim progress lineage не совпала")
            return self._claim_progress_in_transaction(
                connection,
                proposal_id=proposal_id,
                decision_id=decision_id,
                job_id=job_id,
                lifecycle_version=int(lineage["version"]),
            )
        finally:
            connection.close()

    def issue_technical_permit(
        self,
        *,
        proposal_id: str,
        decision_id: str,
        job_id: str,
        claim_id: str,
        operation_kind: str,
        account_id: str,
        resource_id: str,
        exact_payload_sha256: str,
        manifest: Mapping[str, object],
        manifest_sha256: str,
        live_evidence_sha256: str,
        expires_at: datetime,
        actor: str,
        expected_lifecycle_version: int | None = None,
        now: datetime | None = None,
    ) -> TechnicalPermit:
        issued_at = now or datetime.now(timezone.utc)
        _require_aware(issued_at, "now")
        _require_aware(expires_at, "expires_at")
        if expires_at <= issued_at:
            raise ValueError("expires_at должен быть позже issued_at")
        _require_sha256(exact_payload_sha256, "exact_payload_sha256")
        _require_sha256(manifest_sha256, "manifest_sha256")
        _require_sha256(live_evidence_sha256, "live_evidence_sha256")
        if canonical_sha256(manifest) != manifest_sha256:
            raise ValueError("manifest_sha256 не совпадает с manifest")
        connection = self._connect()
        secret = secrets.token_urlsafe(32)
        permit_id = str(uuid.uuid4())
        try:
            connection.execute("BEGIN IMMEDIATE")
            lineage = connection.execute(
                """
                SELECT l.state, l.version, l.active_decision_id, l.active_job_id,
                       d.decision_kind, j.state AS job_state,
                       t.action_kind, t.account_id, t.adset_id, t.subject_id,
                       t.intended_payload_sha256
                FROM owner_action_lifecycle l
                JOIN owner_action_decisions d
                  ON d.proposal_id = l.proposal_id AND d.decision_id = ?
                JOIN owner_execution_jobs j
                  ON j.proposal_id = d.proposal_id
                 AND j.decision_id = d.decision_id
                 AND j.job_id = ?
                JOIN owner_action_proposal_targets t
                  ON t.proposal_id = l.proposal_id AND t.claim_id = ?
                WHERE l.proposal_id = ?
                """,
                (decision_id, job_id, claim_id, proposal_id),
            ).fetchone()
            if lineage is None:
                raise OwnerActionLineageError("Permit lineage не найдена")
            target_resource_id = (
                str(lineage["subject_id"])
                if operation_kind in {"PAUSE_AD", "UNPAUSE_AD"}
                else str(lineage["adset_id"] or lineage["subject_id"])
            )
            if (
                str(lineage["state"]) != LifecycleState.LIVE_REVIEW.value
                or str(lineage["active_decision_id"]) != decision_id
                or str(lineage["active_job_id"]) != job_id
                or str(lineage["decision_kind"]) != OwnerDecisionKind.APPROVE.value
                or str(lineage["job_state"]) != "REVIEWING"
                or str(lineage["action_kind"]) != operation_kind
                or str(lineage["account_id"]) != account_id
                or str(lineage["intended_payload_sha256"]) != exact_payload_sha256
                or target_resource_id != resource_id
            ):
                raise OwnerActionLineageError("Permit exact lineage не совпала")
            version = int(lineage["version"])
            if expected_lifecycle_version is not None and version != expected_lifecycle_version:
                raise OwnerActionLifecycleConflict("LIFECYCLE_CAS_CONFLICT")
            progress = self._claim_progress_in_transaction(
                connection,
                proposal_id=proposal_id,
                decision_id=decision_id,
                job_id=job_id,
                lifecycle_version=version,
            )
            if progress.state != "READY" or progress.next_claim_id != claim_id:
                raise OwnerActionLineageError("Permit claim не является следующим")
            previous_attempt = connection.execute(
                """
                SELECT 1 FROM owner_action_attempts
                WHERE proposal_id = ? AND claim_id = ?
                """,
                (proposal_id, claim_id),
            ).fetchone()
            if previous_attempt is not None:
                raise OwnerActionPermitUnavailable("CLAIM_ALREADY_ATTEMPTED")
            nonreplaceable_permit = connection.execute(
                """
                SELECT 1 FROM owner_technical_permits
                WHERE proposal_id = ? AND claim_id = ?
                  AND phase NOT IN ('EXPIRED','REVOKED')
                """,
                (proposal_id, claim_id),
            ).fetchone()
            if nonreplaceable_permit is not None:
                raise OwnerActionPermitUnavailable("PREVIOUS_PERMIT_NOT_REPLACEABLE")
            repeated_live_evidence = connection.execute(
                """
                SELECT 1 FROM owner_technical_permits
                WHERE proposal_id = ? AND claim_id = ?
                  AND live_evidence_sha256 = ?
                """,
                (proposal_id, claim_id, live_evidence_sha256),
            ).fetchone()
            if repeated_live_evidence is not None:
                raise OwnerActionPermitUnavailable("FRESH_LIVE_REVIEW_REQUIRED")
            sequence = connection.execute(
                """
                SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence
                FROM owner_technical_permits
                WHERE proposal_id = ? AND claim_id = ?
                """,
                (proposal_id, claim_id),
            ).fetchone()
            if sequence is None:
                raise OwnerActionRepositoryError("Permit sequence получить не удалось")
            connection.execute(
                """
                INSERT INTO owner_technical_permits (
                    permit_id, secret_sha256, proposal_id, decision_id, job_id,
                    claim_id, operation_kind, account_id, resource_id,
                    exact_payload_sha256, manifest_json, manifest_sha256,
                    live_evidence_sha256, phase, sequence_no, issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ISSUED', ?, ?, ?)
                """,
                (
                    permit_id,
                    hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                    proposal_id,
                    decision_id,
                    job_id,
                    claim_id,
                    operation_kind,
                    account_id,
                    resource_id,
                    exact_payload_sha256,
                    canonical_json(manifest).decode("utf-8"),
                    manifest_sha256,
                    live_evidence_sha256,
                    int(sequence["next_sequence"]),
                    _iso(issued_at),
                    _iso(expires_at),
                ),
            )
            job_updated = connection.execute(
                """
                UPDATE owner_execution_jobs
                SET state = 'PERMIT_ISSUED', updated_at = ?
                WHERE proposal_id = ? AND decision_id = ? AND job_id = ?
                  AND state = 'REVIEWING'
                """,
                (_iso(issued_at), proposal_id, decision_id, job_id),
            ).rowcount
            lifecycle_updated = connection.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = 'PERMIT_ISSUED', version = version + 1,
                    latest_reason_code = 'PERMIT_ISSUED', updated_at = ?
                WHERE proposal_id = ? AND state = 'LIVE_REVIEW' AND version = ?
                  AND active_decision_id = ? AND active_job_id = ?
                """,
                (_iso(issued_at), proposal_id, version, decision_id, job_id),
            ).rowcount
            if job_updated != 1 or lifecycle_updated != 1:
                raise OwnerActionLifecycleConflict("PERMIT_ISSUE_CAS_CONFLICT")
            self._append_event(
                connection,
                proposal_id=proposal_id,
                event_type="PERMIT_ISSUED",
                actor=actor,
                reason_code=None,
                payload={
                    "permit_id": permit_id,
                    "decision_id": decision_id,
                    "job_id": job_id,
                    "claim_id": claim_id,
                    "manifest_sha256": manifest_sha256,
                    "live_evidence_sha256": live_evidence_sha256,
                },
                now=issued_at,
            )
            connection.commit()
            return TechnicalPermit(
                permit_id=permit_id,
                secret=secret,
                proposal_id=proposal_id,
                decision_id=decision_id,
                job_id=job_id,
                claim_id=claim_id,
                expires_at=expires_at.astimezone(timezone.utc),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def queue_execution(
        self,
        *,
        proposal_id: str,
        decision_id: str,
        job_id: str,
        expected_lifecycle_version: int,
        actor: str,
        now: datetime | None = None,
    ) -> int:
        """CAS-связывает APPROVED proposal с уже созданным exact job."""

        return self.transition_lifecycle(
            proposal_id,
            expected_state=LifecycleState.APPROVED.value,
            expected_version=expected_lifecycle_version,
            new_state=LifecycleState.EXECUTION_QUEUED.value,
            active_decision_id=decision_id,
            active_job_id=job_id,
            actor=actor,
            now=now,
        )

    def begin_live_review(
        self,
        *,
        proposal_id: str,
        decision_id: str,
        job_id: str,
        expected_lifecycle_version: int,
        actor: str,
        claim_id: str | None = None,
        expected_lifecycle_state: str = LifecycleState.EXECUTION_QUEUED.value,
        now: datetime | None = None,
    ) -> int:
        """Одной транзакцией claim-ит job и lifecycle для fresh live review."""

        reviewed_at = now or datetime.now(timezone.utc)
        _require_aware(reviewed_at, "now")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lineage = connection.execute(
                """
                SELECT l.state, l.version, l.active_decision_id, l.active_job_id,
                       d.decision_kind, j.state AS job_state
                FROM owner_action_lifecycle l
                JOIN owner_action_decisions d
                  ON d.proposal_id = l.proposal_id AND d.decision_id = ?
                JOIN owner_execution_jobs j
                  ON j.proposal_id = d.proposal_id
                 AND j.decision_id = d.decision_id
                 AND j.job_id = ?
                WHERE l.proposal_id = ?
                """,
                (decision_id, job_id, proposal_id),
            ).fetchone()
            if lineage is None:
                raise OwnerActionLineageError("Live-review lineage не найдена")
            expected_job_state = {
                LifecycleState.EXECUTION_QUEUED.value: "QUEUED",
                LifecycleState.EXECUTION_RETRY_WAIT.value: "WAITING_RETRY",
            }.get(expected_lifecycle_state)
            if expected_job_state is None:
                raise ValueError("Live review допускается только из queued/retry")
            # Брошенное REVIEWING — законный вход в новый live review, а не
            # «чужая lineage». Такие пары остаются после воркера, умершего посреди
            # обзора, и от старого кода, который двигал lifecycle без задания;
            # раньше диспетчер на них падал с LineageError и после трёх проходов
            # объявлял владельцу «не сработало», хотя проваленных попыток не было
            # вовсе. Безопасность держат ДРУГИЕ проверки, они на месте: аренда
            # задания (один воркер), CAS lifecycle по state+version и progress
            # READY ниже, который доказывает отсутствие permit и попытки по claim.
            allowed_job_states = (expected_job_state, "REVIEWING")
            if (
                str(lineage["state"]) != expected_lifecycle_state
                or int(lineage["version"]) != expected_lifecycle_version
                or str(lineage["active_decision_id"]) != decision_id
                or str(lineage["active_job_id"]) != job_id
                or str(lineage["decision_kind"]) != OwnerDecisionKind.APPROVE.value
                or str(lineage["job_state"]) not in allowed_job_states
            ):
                raise OwnerActionLineageError("Live-review exact lineage не совпала")
            progress = self._claim_progress_in_transaction(
                connection,
                proposal_id=proposal_id,
                decision_id=decision_id,
                job_id=job_id,
                lifecycle_version=expected_lifecycle_version,
            )
            resolved_claim_id = claim_id or progress.next_claim_id
            if (
                progress.state != "READY"
                or resolved_claim_id is None
                or progress.next_claim_id != resolved_claim_id
            ):
                raise OwnerActionLineageError("Live review claim не является следующим")
            job_updated = connection.execute(
                f"""
                UPDATE owner_execution_jobs
                SET state = 'REVIEWING', updated_at = ?
                WHERE proposal_id = ? AND decision_id = ? AND job_id = ?
                  AND state IN ({",".join("?" for _ in allowed_job_states)})
                """,
                (
                    _iso(reviewed_at),
                    proposal_id,
                    decision_id,
                    job_id,
                    *allowed_job_states,
                ),
            ).rowcount
            lifecycle_updated = connection.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = 'LIVE_REVIEW', version = version + 1,
                    latest_reason_code = 'LIVE_REVIEW', updated_at = ?
                WHERE proposal_id = ? AND state = ?
                  AND version = ? AND active_decision_id = ? AND active_job_id = ?
                """,
                (
                    _iso(reviewed_at),
                    proposal_id,
                    expected_lifecycle_state,
                    expected_lifecycle_version,
                    decision_id,
                    job_id,
                ),
            ).rowcount
            if job_updated != 1 or lifecycle_updated != 1:
                raise OwnerActionLifecycleConflict("LIVE_REVIEW_CAS_CONFLICT")
            self._append_event(
                connection,
                proposal_id=proposal_id,
                event_type="LIVE_REVIEW",
                actor=actor,
                reason_code=None,
                payload={
                    "decision_id": decision_id,
                    "job_id": job_id,
                    "claim_id": resolved_claim_id,
                },
                now=reviewed_at,
            )
            connection.commit()
            return expected_lifecycle_version + 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def begin_claim_live_review(
        self,
        *,
        proposal_id: str,
        decision_id: str,
        job_id: str,
        claim_id: str,
        expected_lifecycle_state: str,
        expected_lifecycle_version: int,
        actor: str,
        now: datetime | None = None,
    ) -> int:
        """Явный T4 API для CAS-захвата следующего exact claim."""

        return self.begin_live_review(
            proposal_id=proposal_id,
            decision_id=decision_id,
            job_id=job_id,
            claim_id=claim_id,
            expected_lifecycle_state=expected_lifecycle_state,
            expected_lifecycle_version=expected_lifecycle_version,
            actor=actor,
            now=now,
        )

    def retire_unattempted_permit(
        self,
        *,
        permit_id: str,
        expected_lifecycle_version: int,
        reason: PermitRetirementReason,
        actor: str,
        now: datetime | None = None,
    ) -> int:
        """Освобождает claim для новой fresh review только до ATTEMPT_STARTED."""

        retired_at = now or datetime.now(timezone.utc)
        _require_aware(retired_at, "now")
        if not isinstance(reason, PermitRetirementReason):
            raise ValueError("reason должен быть PermitRetirementReason")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            permit = connection.execute(
                """
                SELECT p.*, l.state AS lifecycle_state, l.version,
                       l.active_decision_id, l.active_job_id,
                       j.state AS job_state
                FROM owner_technical_permits p
                JOIN owner_action_lifecycle l ON l.proposal_id = p.proposal_id
                JOIN owner_execution_jobs j
                  ON j.proposal_id = p.proposal_id
                 AND j.decision_id = p.decision_id
                 AND j.job_id = p.job_id
                WHERE p.permit_id = ?
                """,
                (permit_id,),
            ).fetchone()
            if permit is None:
                raise OwnerActionPermitUnavailable("PERMIT_NOT_FOUND")
            if (
                str(permit["phase"]) != "ISSUED"
                or permit["consumed_at"] is not None
                or str(permit["lifecycle_state"]) != LifecycleState.PERMIT_ISSUED.value
                or int(permit["version"]) != expected_lifecycle_version
                or str(permit["active_decision_id"]) != str(permit["decision_id"])
                or str(permit["active_job_id"]) != str(permit["job_id"])
                or str(permit["job_state"]) != "PERMIT_ISSUED"
            ):
                raise OwnerActionPermitUnavailable("PERMIT_RETIRE_LINEAGE_MISMATCH")
            if (
                reason is PermitRetirementReason.PERMIT_EXPIRED
                and _parse_iso(permit["expires_at"]) > retired_at
            ):
                raise OwnerActionPermitUnavailable("PERMIT_NOT_EXPIRED")
            attempted = connection.execute(
                """
                SELECT 1 FROM owner_action_attempts
                WHERE proposal_id = ? AND claim_id = ?
                """,
                (permit["proposal_id"], permit["claim_id"]),
            ).fetchone()
            if attempted is not None:
                raise OwnerActionPermitUnavailable("CLAIM_ALREADY_ATTEMPTED")

            new_phase = (
                "EXPIRED"
                if reason is PermitRetirementReason.PERMIT_EXPIRED
                else "REVOKED"
            )
            permit_updated = connection.execute(
                """
                UPDATE owner_technical_permits
                SET phase = ?,
                    revoked_at = ?,
                    revoke_reason = ?
                WHERE permit_id = ? AND phase = 'ISSUED'
                  AND consumed_at IS NULL
                """,
                (
                    new_phase,
                    _iso(retired_at),
                    reason.value,
                    permit_id,
                ),
            ).rowcount
            job_updated = connection.execute(
                """
                UPDATE owner_execution_jobs
                SET state = 'WAITING_RETRY', next_attempt_at = ?,
                    last_reason_code = ?, updated_at = ?
                WHERE proposal_id = ? AND decision_id = ? AND job_id = ?
                  AND state = 'PERMIT_ISSUED'
                """,
                (
                    _iso(retired_at),
                    reason.value,
                    _iso(retired_at),
                    permit["proposal_id"],
                    permit["decision_id"],
                    permit["job_id"],
                ),
            ).rowcount
            lifecycle_updated = connection.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = 'EXECUTION_RETRY_WAIT', version = version + 1,
                    latest_reason_code = ?, next_action_at = ?, updated_at = ?
                WHERE proposal_id = ? AND state = 'PERMIT_ISSUED'
                  AND version = ? AND active_decision_id = ? AND active_job_id = ?
                """,
                (
                    reason.value,
                    _iso(retired_at),
                    _iso(retired_at),
                    permit["proposal_id"],
                    expected_lifecycle_version,
                    permit["decision_id"],
                    permit["job_id"],
                ),
            ).rowcount
            if permit_updated != 1 or job_updated != 1 or lifecycle_updated != 1:
                raise OwnerActionLifecycleConflict("PERMIT_RETIRE_CAS_CONFLICT")
            self._append_event(
                connection,
                proposal_id=str(permit["proposal_id"]),
                event_type=new_phase,
                actor=actor,
                reason_code=reason.value,
                payload={
                    "permit_id": permit_id,
                    "claim_id": str(permit["claim_id"]),
                },
                now=retired_at,
            )
            connection.commit()
            return expected_lifecycle_version + 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def consume_technical_permit(
        self,
        *,
        permit_secret: str,
        exact_payload_sha256: str,
        actor: str,
        expected_lifecycle_version: int | None = None,
        now: datetime | None = None,
    ) -> ActionAttemptAttestation:
        """Атомарно CONSUMED + ATTEMPT_STARTED + event до provider I/O."""

        consumed_at = now or datetime.now(timezone.utc)
        _require_aware(consumed_at, "now")
        _require_sha256(exact_payload_sha256, "exact_payload_sha256")
        secret_sha256 = hashlib.sha256(
            _required_text(permit_secret, "permit_secret").encode("utf-8")
        ).hexdigest()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            permit = connection.execute(
                """
                SELECT p.*, l.state AS lifecycle_state, l.version,
                       l.active_decision_id, l.active_job_id,
                       j.state AS job_state, d.decision_kind,
                       t.action_kind AS target_action_kind,
                       t.account_id AS target_account_id
                FROM owner_technical_permits p
                JOIN owner_action_lifecycle l ON l.proposal_id = p.proposal_id
                JOIN owner_action_decisions d
                  ON d.proposal_id = p.proposal_id
                 AND d.decision_id = p.decision_id
                JOIN owner_execution_jobs j
                  ON j.proposal_id = p.proposal_id
                 AND j.decision_id = p.decision_id
                 AND j.job_id = p.job_id
                JOIN owner_action_proposal_targets t
                  ON t.proposal_id = p.proposal_id
                 AND t.claim_id = p.claim_id
                WHERE p.secret_sha256 = ?
                """,
                (secret_sha256,),
            ).fetchone()
            if permit is None:
                raise OwnerActionPermitUnavailable("PERMIT_NOT_FOUND")
            lineage_matches = (
                str(permit["phase"]) == "ISSUED",
                str(permit["lifecycle_state"]) == LifecycleState.PERMIT_ISSUED.value,
                str(permit["active_decision_id"]) == str(permit["decision_id"]),
                str(permit["active_job_id"]) == str(permit["job_id"]),
                str(permit["job_state"]) == "PERMIT_ISSUED",
                str(permit["decision_kind"]) == OwnerDecisionKind.APPROVE.value,
                str(permit["target_action_kind"]) == str(permit["operation_kind"]),
                str(permit["target_account_id"]) == str(permit["account_id"]),
                str(permit["exact_payload_sha256"]) == exact_payload_sha256,
                _parse_iso(permit["expires_at"]) > consumed_at,
                permit["consumed_at"] is None,
                permit["revoked_at"] is None,
            )
            if not all(lineage_matches):
                raise OwnerActionPermitUnavailable("PERMIT_SCOPE_OR_STATE_MISMATCH")
            version = int(permit["version"])
            if expected_lifecycle_version is not None and version != expected_lifecycle_version:
                raise OwnerActionLifecycleConflict("LIFECYCLE_CAS_CONFLICT")
            progress = self._claim_progress_in_transaction(
                connection,
                proposal_id=str(permit["proposal_id"]),
                decision_id=str(permit["decision_id"]),
                job_id=str(permit["job_id"]),
                lifecycle_version=version,
            )
            if (
                progress.state != "IN_FLIGHT"
                or progress.next_claim_id != str(permit["claim_id"])
            ):
                raise OwnerActionLineageError("Permit claim sequence нарушена")

            attempt_id = str(uuid.uuid4())
            permit_updated = connection.execute(
                """
                UPDATE owner_technical_permits
                SET phase = 'CONSUMED', consumed_at = ?
                WHERE permit_id = ? AND phase = 'ISSUED'
                  AND consumed_at IS NULL AND revoked_at IS NULL
                  AND expires_at > ?
                """,
                (_iso(consumed_at), permit["permit_id"], _iso(consumed_at)),
            ).rowcount
            if permit_updated != 1:
                raise OwnerActionPermitUnavailable("PERMIT_CONSUME_CAS_CONFLICT")
            connection.execute(
                """
                INSERT INTO owner_action_attempts (
                    attempt_id, permit_id, proposal_id, decision_id, job_id,
                    claim_id, operation_kind, account_id, resource_id,
                    exact_payload_sha256, state, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ATTEMPT_STARTED', ?)
                """,
                (
                    attempt_id,
                    permit["permit_id"],
                    permit["proposal_id"],
                    permit["decision_id"],
                    permit["job_id"],
                    permit["claim_id"],
                    permit["operation_kind"],
                    permit["account_id"],
                    permit["resource_id"],
                    permit["exact_payload_sha256"],
                    _iso(consumed_at),
                ),
            )
            job_updated = connection.execute(
                """
                UPDATE owner_execution_jobs
                SET state = 'ATTEMPT_STARTED', attempts = attempts + 1,
                    updated_at = ?
                WHERE proposal_id = ? AND decision_id = ? AND job_id = ?
                  AND state = 'PERMIT_ISSUED'
                """,
                (
                    _iso(consumed_at),
                    permit["proposal_id"],
                    permit["decision_id"],
                    permit["job_id"],
                ),
            ).rowcount
            lifecycle_updated = connection.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = 'ATTEMPT_STARTED', version = version + 1,
                    latest_reason_code = 'ATTEMPT_STARTED', updated_at = ?
                WHERE proposal_id = ? AND state = 'PERMIT_ISSUED'
                  AND version = ? AND active_decision_id = ? AND active_job_id = ?
                """,
                (
                    _iso(consumed_at),
                    permit["proposal_id"],
                    version,
                    permit["decision_id"],
                    permit["job_id"],
                ),
            ).rowcount
            if job_updated != 1 or lifecycle_updated != 1:
                raise OwnerActionLifecycleConflict("ATTEMPT_START_CAS_CONFLICT")
            self._append_event(
                connection,
                proposal_id=str(permit["proposal_id"]),
                event_type="ATTEMPT_STARTED",
                actor=actor,
                reason_code=None,
                payload={
                    "attempt_id": attempt_id,
                    "permit_id": str(permit["permit_id"]),
                    "claim_id": str(permit["claim_id"]),
                },
                now=consumed_at,
            )
            connection.commit()
            return ActionAttemptAttestation(
                attempt_id=attempt_id,
                permit_id=str(permit["permit_id"]),
                proposal_id=str(permit["proposal_id"]),
                decision_id=str(permit["decision_id"]),
                claim_id=str(permit["claim_id"]),
                operation_kind=str(permit["operation_kind"]),
                account_id=str(permit["account_id"]),
                resource_id=str(permit["resource_id"]),
                payload_sha256=str(permit["exact_payload_sha256"]),
                consumed_at=consumed_at.astimezone(timezone.utc),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def transition_attempt(
        self,
        attempt_id: str,
        *,
        expected_state: str,
        new_state: str,
        actor: str,
        provider_request_id: str | None = None,
        provider_result: Mapping[str, object] | None = None,
        reason_code: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """CAS-завершает exact attempt и синхронно двигает job/lifecycle."""

        completed_at = now or datetime.now(timezone.utc)
        _require_aware(completed_at, "now")
        if new_state not in {
            "CONFIRMED",
            "FAILED_NO_EFFECT",
            "RECONCILE_REQUIRED",
        }:
            raise ValueError("Неподдерживаемое terminal attempt state")
        provider_result_sha256 = (
            canonical_sha256(provider_result) if provider_result is not None else None
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT a.*, l.state AS lifecycle_state, l.version,
                       l.active_decision_id, l.active_job_id, j.state AS job_state
                FROM owner_action_attempts a
                JOIN owner_action_lifecycle l ON l.proposal_id = a.proposal_id
                JOIN owner_execution_jobs j
                  ON j.proposal_id = a.proposal_id
                 AND j.decision_id = a.decision_id
                 AND j.job_id = a.job_id
                WHERE a.attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise OwnerActionLineageError("Attempt не найден")
            if (
                str(row["state"]) != expected_state
                or str(row["lifecycle_state"]) != LifecycleState.ATTEMPT_STARTED.value
                or str(row["job_state"]) != "ATTEMPT_STARTED"
                or str(row["active_decision_id"]) != str(row["decision_id"])
                or str(row["active_job_id"]) != str(row["job_id"])
            ):
                raise OwnerActionLineageError("Attempt exact lineage не совпала")
            attempt_updated = connection.execute(
                """
                UPDATE owner_action_attempts
                SET state = ?, provider_request_id = ?,
                    provider_result_sha256 = ?, completed_at = ?,
                    last_reason_code = ?
                WHERE attempt_id = ? AND state = ?
                """,
                (
                    new_state,
                    provider_request_id,
                    provider_result_sha256,
                    _iso(completed_at),
                    reason_code,
                    attempt_id,
                    expected_state,
                ),
            ).rowcount
            progress = self._claim_progress_in_transaction(
                connection,
                proposal_id=str(row["proposal_id"]),
                decision_id=str(row["decision_id"]),
                job_id=str(row["job_id"]),
                lifecycle_version=int(row["version"]) + 1,
            )
            if progress.state == "RECONCILE_REQUIRED":
                job_state = "RECONCILE_REQUIRED"
                lifecycle_state = LifecycleState.RECONCILE_REQUIRED.value
                next_claim_id = None
            elif progress.state == "READY":
                job_state = "QUEUED"
                lifecycle_state = LifecycleState.EXECUTION_QUEUED.value
                next_claim_id = progress.next_claim_id
            elif progress.state == "FAILED_NO_EFFECT":
                job_state = "FAILED_NO_EFFECT"
                lifecycle_state = LifecycleState.FAILED_NO_EFFECT.value
                next_claim_id = None
            elif progress.state == "COMPLETE":
                job_state = "EXECUTED"
                lifecycle_state = LifecycleState.EXECUTED.value
                next_claim_id = None
            else:
                raise OwnerActionLineageError(
                    "Terminal attempt оставил другой claim в полузапущенном состоянии"
                )
            job_updated = connection.execute(
                """
                UPDATE owner_execution_jobs
                SET state = ?, next_attempt_at = ?,
                    last_reason_code = ?, updated_at = ?
                WHERE proposal_id = ? AND decision_id = ? AND job_id = ?
                  AND state = 'ATTEMPT_STARTED'
                """,
                (
                    job_state,
                    _iso(completed_at) if next_claim_id is not None else None,
                    reason_code,
                    _iso(completed_at),
                    row["proposal_id"],
                    row["decision_id"],
                    row["job_id"],
                ),
            ).rowcount
            lifecycle_updated = connection.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = ?, version = version + 1,
                    latest_reason_code = ?, next_action_at = ?, updated_at = ?
                WHERE proposal_id = ? AND state = 'ATTEMPT_STARTED'
                  AND version = ? AND active_decision_id = ? AND active_job_id = ?
                """,
                (
                    lifecycle_state,
                    reason_code,
                    _iso(completed_at) if next_claim_id is not None else None,
                    _iso(completed_at),
                    row["proposal_id"],
                    row["version"],
                    row["decision_id"],
                    row["job_id"],
                ),
            ).rowcount
            if attempt_updated != 1 or job_updated != 1 or lifecycle_updated != 1:
                raise OwnerActionLifecycleConflict("ATTEMPT_RESULT_CAS_CONFLICT")
            self._append_event(
                connection,
                proposal_id=str(row["proposal_id"]),
                event_type=f"ATTEMPT_{new_state}",
                actor=actor,
                reason_code=reason_code,
                payload={
                    "attempt_id": attempt_id,
                    "provider_request_id": provider_request_id,
                    "provider_result_sha256": provider_result_sha256,
                    "aggregate_state": lifecycle_state,
                    "next_claim_id": next_claim_id,
                },
                now=completed_at,
            )
            connection.commit()
            return AttemptTransitionResult(
                attempt_id=attempt_id,
                claim_id=str(row["claim_id"]),
                attempt_state=new_state,  # type: ignore[arg-type]
                aggregate_state=lifecycle_state,
                next_claim_id=next_claim_id,
                lifecycle_version=int(row["version"]) + 1,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _default_repository() -> OwnerActionRepository:
    from agent import database

    if database.DB_PATH is None:
        raise OwnerActionRepositoryError("БД не инициализирована")
    return OwnerActionRepository(database.DB_PATH)


def propose_action(
    plan: ProposedActionPlan,
    *,
    now: datetime | None = None,
) -> ProposalReceipt:
    return _default_repository().propose_action(plan, now=now)


def find_receipt_by_idempotency_key(
    idempotency_key: str,
    *,
    targets_sha256: str,
) -> ProposalReceipt | None:
    return _default_repository().find_receipt_by_idempotency_key(
        idempotency_key,
        targets_sha256=targets_sha256,
    )


def approve_by_system(
    *,
    proposal_id: str,
    automation_rule: str,
    actor: str,
    reason_text: str | None = None,
    evidence: Mapping[str, object] | None = None,
    now: datetime | None = None,
) -> OwnerDecisionResult:
    return _default_repository().approve_by_system(
        proposal_id=proposal_id,
        automation_rule=automation_rule,
        actor=actor,
        reason_text=reason_text,
        evidence=evidence,
        now=now,
    )


def get_proposal(proposal_id: str) -> ProposalView | None:
    return _default_repository().get_proposal(proposal_id)


def find_proposal_state_by_idempotency_key(idempotency_key: str) -> str | None:
    """Lifecycle-состояние предложения, занимающего ключ идемпотентности.

    None — ключ свободен. Нужен продюсерам, чьи durable-ключи переживают
    предложение: терминальное предложение навсегда занимает ключ, и без этой
    проверки следующий план с тем же ключом падает в
    IDEMPOTENCY_PAYLOAD_CONFLICT (см. ротацию в services/auto_launch.py).
    """
    repository = _default_repository()
    connection = repository._connect()
    try:
        row = connection.execute(
            """
            SELECT l.state
            FROM owner_action_proposals p
            JOIN owner_action_lifecycle l USING (proposal_id)
            WHERE p.idempotency_key = ?
            """,
            (str(idempotency_key),),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else str(row["state"])


def proposal_in_flight_by_idempotency_key(idempotency_key: str) -> bool:
    return _default_repository().proposal_in_flight_by_idempotency_key(idempotency_key)


def find_lifecycle_by_source_ref(source_ref: str) -> tuple[str, str] | None:
    return _default_repository().find_lifecycle_by_source_ref(source_ref)


def find_live_proposal_for_subject(
    *,
    subject_id: str,
    action_kind: str,
    now: datetime | None = None,
) -> ProposalReceipt | None:
    return _default_repository().find_live_proposal_for_subject(
        subject_id=subject_id,
        action_kind=action_kind,
        now=now,
    )


def list_proposals(
    *,
    state: str | None,
    cursor: str | None,
    limit: int = 50,
) -> ProposalPage:
    return _default_repository().list_proposals(
        state=state,
        cursor=cursor,
        limit=limit,
    )
