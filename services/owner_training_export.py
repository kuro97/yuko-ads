"""Read-only JSONL export полного owner-action lineage для обучения."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO

from services.owner_action_models import canonical_json


_MAX_EXPORT_RANGE = timedelta(days=90)


class OwnerTrainingExportError(RuntimeError):
    """Базовая fail-closed ошибка training export."""


class OwnerTrainingExportRangeError(OwnerTrainingExportError):
    """Границы периода не образуют допустимый интервал."""


class OwnerTrainingExportDataError(OwnerTrainingExportError):
    """В immutable lineage найден повреждённый JSON."""


@dataclass(frozen=True, slots=True)
class ExportSummary:
    """Сводка детерминированного JSONL export."""

    date_from: datetime
    date_to: datetime
    proposal_count: int
    decision_count: int
    claim_count: int
    attempt_count: int
    outcome_count: int
    verification_observation_count: int
    byte_count: int
    content_sha256: str
    # Волна E: свободный фидбек владельца и вердикты независимого верификатора.
    owner_feedback_count: int = 0
    action_verification_count: int = 0


def _require_aware(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise OwnerTrainingExportRangeError(
            f"{field_name} должен содержать timezone"
        )
    return value.astimezone(timezone.utc)


def _validate_range(
    date_from: datetime,
    date_to: datetime,
) -> tuple[datetime, datetime]:
    start = _require_aware(date_from, "date_from")
    end = _require_aware(date_to, "date_to")
    if end <= start:
        raise OwnerTrainingExportRangeError(
            "date_to должен быть позже date_from"
        )
    if end - start > _MAX_EXPORT_RANGE:
        raise OwnerTrainingExportRangeError(
            "Период export не может превышать 90 дней"
        )
    return start, end


def _resolve_db_path(db_path: str | Path | None) -> Path:
    if db_path is None:
        from agent import database

        if database.DB_PATH is None:
            raise OwnerTrainingExportError("БД owner actions не инициализирована")
        path = Path(database.DB_PATH)
    else:
        path = Path(db_path)
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite БД не найдена: {resolved}")
    return resolved


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=30,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _parse_json(value: object, field_name: str) -> object:
    if not isinstance(value, str):
        raise OwnerTrainingExportDataError(
            f"{field_name} должен быть JSON-строкой"
        )
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise OwnerTrainingExportDataError(
            f"{field_name} содержит повреждённый JSON"
        ) from exc


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _proposal_document(row: sqlite3.Row) -> dict[str, object]:
    return {
        "proposal_id": str(row["proposal_id"]),
        "proposal_version": int(row["proposal_version"]),
        "proposal_kind": str(row["proposal_kind"]),
        "origin": str(row["origin"]),
        "idempotency_key": str(row["idempotency_key"]),
        "source_ref": str(row["source_ref"]),
        "requested_by_actor": str(row["requested_by_actor"]),
        "summary": str(row["summary"]),
        "plan": _parse_json(row["plan_json"], "owner_action_proposals.plan_json"),
        "plan_sha256": str(row["plan_sha256"]),
        "targets_sha256": str(row["targets_sha256"]),
        "evidence_sha256": str(row["evidence_sha256"]),
        "config_version_sha256": str(row["config_version_sha256"]),
        "proposal_sha256": str(row["proposal_sha256"]),
        "staged_media_root": _optional_text(row["staged_media_root"]),
        "created_at": str(row["created_at"]),
        "valid_until": str(row["valid_until"]),
    }


def _load_lifecycle(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> dict[str, object] | None:
    row = connection.execute(
        """
        SELECT state, version, delivery_generation, active_decision_id,
               active_job_id, latest_reason_code, next_action_at, updated_at
        FROM owner_action_lifecycle
        WHERE proposal_id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "state": str(row["state"]),
        "version": int(row["version"]),
        "delivery_generation": int(row["delivery_generation"]),
        "active_decision_id": _optional_text(row["active_decision_id"]),
        "active_job_id": _optional_text(row["active_job_id"]),
        "latest_reason_code": _optional_text(row["latest_reason_code"]),
        "next_action_at": _optional_text(row["next_action_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _load_evidence(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT evidence_id, evidence_kind, source_system, subject_id,
               observed_at, complete, payload_json, payload_sha256, created_at
        FROM owner_action_evidence
        WHERE proposal_id = ?
        ORDER BY observed_at, evidence_id
        """,
        (proposal_id,),
    ).fetchall()
    return [
        {
            "evidence_id": str(row["evidence_id"]),
            "evidence_kind": str(row["evidence_kind"]),
            "source_system": str(row["source_system"]),
            "subject_id": str(row["subject_id"]),
            "observed_at": str(row["observed_at"]),
            "complete": bool(row["complete"]),
            "payload": _parse_json(
                row["payload_json"],
                "owner_action_evidence.payload_json",
            ),
            "payload_sha256": str(row["payload_sha256"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def _load_owner_feedback(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> list[dict[str, object]]:
    """Свободный фидбек владельца реплаем (миграция 023, волна E, блок E4).

    В датасет попадает ЛЮБОЙ текст, включая нераспознанный: именно он объясняет,
    почему решение было таким, и учит модель формулировкам владельца.
    """
    rows = connection.execute(
        """
        SELECT feedback_id, digest_id, telegram_update_id, message_id,
               reply_to_message_id, owner_user_id, text, parsed_action,
               parsed_until, created_at
        FROM owner_feedback
        WHERE proposal_id = ?
        ORDER BY created_at, feedback_id
        """,
        (proposal_id,),
    ).fetchall()
    return [
        {
            "feedback_id": str(row["feedback_id"]),
            "digest_id": _optional_text(row["digest_id"]),
            "telegram_update_id": int(row["telegram_update_id"]),
            "message_id": int(row["message_id"]),
            "reply_to_message_id": (
                None
                if row["reply_to_message_id"] is None
                else int(row["reply_to_message_id"])
            ),
            "owner_user_id": int(row["owner_user_id"]),
            "text": str(row["text"]),
            "parsed_action": str(row["parsed_action"]),
            "parsed_until": _optional_text(row["parsed_until"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def _load_action_verifications(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> list[dict[str, object]]:
    """Вердикты независимого верификатора (миграция 023, волна E, блок E1)."""

    rows = connection.execute(
        """
        SELECT verification_id, claim_id, attempt_id, kind, check_seq, retry_no,
               expected_json, observed_json, verdict, reason_code, checked_at
        FROM action_verifications
        WHERE proposal_id = ?
        ORDER BY checked_at, check_seq, verification_id
        """,
        (proposal_id,),
    ).fetchall()
    return [
        {
            "verification_id": str(row["verification_id"]),
            "claim_id": str(row["claim_id"]),
            "attempt_id": _optional_text(row["attempt_id"]),
            "kind": str(row["kind"]),
            "check_seq": int(row["check_seq"]),
            "retry_no": int(row["retry_no"]),
            "expected": _parse_json(
                row["expected_json"],
                "action_verifications.expected_json",
            ),
            "observed": _parse_json(
                row["observed_json"],
                "action_verifications.observed_json",
            ),
            "verdict": str(row["verdict"]),
            "reason_code": str(row["reason_code"]),
            "checked_at": str(row["checked_at"]),
        }
        for row in rows
    ]


def _load_decisions(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT decision_id, proposal_sha256, decision_kind, owner_user_id,
               chat_id, message_id, delivery_generation, telegram_update_id,
               callback_query_id, trusted_ingress_sha256, reason_text,
               recorded_at, decision_source, automation_rule
        FROM owner_action_decisions
        WHERE proposal_id = ?
        ORDER BY recorded_at, decision_id
        """,
        (proposal_id,),
    ).fetchall()
    return [
        {
            "decision_id": str(row["decision_id"]),
            "proposal_sha256": str(row["proposal_sha256"]),
            "decision_kind": str(row["decision_kind"]),
            # Автоматическое решение бота телеграм-родословной не имеет и иметь
            # не может (миграция 026): у него decision_source='SYSTEM' и все эти
            # поля NULL. Датасет обучения обязан отличать «нажал владелец» от
            # «решил бот», поэтому здесь None, а не подставленный ноль.
            "decision_source": str(row["decision_source"]),
            "automation_rule": _optional_text(row["automation_rule"]),
            "owner_user_id": _optional_int(row["owner_user_id"]),
            "chat_id": _optional_int(row["chat_id"]),
            "message_id": _optional_int(row["message_id"]),
            "delivery_generation": _optional_int(row["delivery_generation"]),
            "telegram_update_id": _optional_int(row["telegram_update_id"]),
            "callback_query_id": _optional_text(row["callback_query_id"]),
            "trusted_ingress_sha256": _optional_text(row["trusted_ingress_sha256"]),
            "reason_text": _optional_text(row["reason_text"]),
            "recorded_at": str(row["recorded_at"]),
        }
        for row in rows
    ]


def _load_jobs(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT job_id, decision_id, state, attempts, next_attempt_at,
               operation_id, last_reason_code, created_at, updated_at
        FROM owner_execution_jobs
        WHERE proposal_id = ?
        ORDER BY created_at, job_id
        """,
        (proposal_id,),
    ).fetchall()
    return [
        {
            "job_id": str(row["job_id"]),
            "decision_id": str(row["decision_id"]),
            "state": str(row["state"]),
            "attempts": int(row["attempts"]),
            "next_attempt_at": _optional_text(row["next_attempt_at"]),
            "operation_id": _optional_text(row["operation_id"]),
            "last_reason_code": _optional_text(row["last_reason_code"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


def _load_outcomes(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    claim_id: str,
    attempt_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT outcome_id, job_id, horizon, observed_at,
               metrics_json, metrics_sha256
        FROM owner_action_outcomes
        WHERE proposal_id = ? AND claim_id = ? AND attempt_id = ?
        ORDER BY CASE horizon
            WHEN 'IMMEDIATE' THEN 0
            WHEN 'D1' THEN 1
            WHEN 'D3' THEN 2
            WHEN 'D7' THEN 3
            WHEN 'D30' THEN 4
            ELSE 5
        END
        """,
        (proposal_id, claim_id, attempt_id),
    ).fetchall()
    return [
        {
            "outcome_id": str(row["outcome_id"]),
            "job_id": str(row["job_id"]),
            "horizon": str(row["horizon"]),
            "observed_at": str(row["observed_at"]),
            "metrics": _parse_json(
                row["metrics_json"],
                "owner_action_outcomes.metrics_json",
            ),
            "metrics_sha256": str(row["metrics_sha256"]),
        }
        for row in rows
    ]


def _load_attempts(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    claim_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT attempt_id, permit_id, decision_id, job_id, operation_kind,
               account_id, resource_id, exact_payload_sha256, state,
               provider_request_id, provider_result_sha256, started_at,
               completed_at, last_reason_code
        FROM owner_action_attempts
        WHERE proposal_id = ? AND claim_id = ?
        ORDER BY started_at, attempt_id
        """,
        (proposal_id, claim_id),
    ).fetchall()
    attempts: list[dict[str, object]] = []
    for row in rows:
        attempt_id = str(row["attempt_id"])
        attempts.append(
            {
                "attempt_id": attempt_id,
                "permit_id": str(row["permit_id"]),
                "decision_id": str(row["decision_id"]),
                "job_id": str(row["job_id"]),
                "operation_kind": str(row["operation_kind"]),
                "account_id": str(row["account_id"]),
                "resource_id": str(row["resource_id"]),
                "exact_payload_sha256": str(row["exact_payload_sha256"]),
                "state": str(row["state"]),
                "provider_request_id": _optional_text(
                    row["provider_request_id"]
                ),
                "provider_result_sha256": _optional_text(
                    row["provider_result_sha256"]
                ),
                "started_at": str(row["started_at"]),
                "completed_at": _optional_text(row["completed_at"]),
                "last_reason_code": _optional_text(row["last_reason_code"]),
                "outcomes": _load_outcomes(
                    connection,
                    proposal_id=proposal_id,
                    claim_id=claim_id,
                    attempt_id=attempt_id,
                ),
            }
        )
    return attempts


def _load_claims(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT claim_id, ordinal, action_kind, account_id, adset_id,
               subject_id, city, language, intended_payload_json,
               intended_payload_sha256, created_at
        FROM owner_action_proposal_targets
        WHERE proposal_id = ?
        ORDER BY ordinal, claim_id
        """,
        (proposal_id,),
    ).fetchall()
    claims: list[dict[str, object]] = []
    for row in rows:
        claim_id = str(row["claim_id"])
        claims.append(
            {
                "claim_id": claim_id,
                "ordinal": int(row["ordinal"]),
                "action_kind": str(row["action_kind"]),
                "account_id": str(row["account_id"]),
                "adset_id": _optional_text(row["adset_id"]),
                "subject_id": str(row["subject_id"]),
                "city": _optional_text(row["city"]),
                "language": _optional_text(row["language"]),
                "intended_payload": _parse_json(
                    row["intended_payload_json"],
                    "owner_action_proposal_targets.intended_payload_json",
                ),
                "intended_payload_sha256": str(
                    row["intended_payload_sha256"]
                ),
                "created_at": str(row["created_at"]),
                "attempts": _load_attempts(
                    connection,
                    proposal_id=proposal_id,
                    claim_id=claim_id,
                ),
            }
        )
    return claims


def _load_events(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT event_id, event_seq, event_type, actor, reason_code,
               payload_json, payload_sha256, created_at
        FROM owner_action_events
        WHERE proposal_id = ?
        ORDER BY event_seq
        """,
        (proposal_id,),
    ).fetchall()
    return [
        {
            "event_id": str(row["event_id"]),
            "event_seq": int(row["event_seq"]),
            "event_type": str(row["event_type"]),
            "actor": str(row["actor"]),
            "reason_code": _optional_text(row["reason_code"]),
            "payload": _parse_json(
                row["payload_json"],
                "owner_action_events.payload_json",
            ),
            "payload_sha256": str(row["payload_sha256"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def _load_watchdog_targets(
    connection: sqlite3.Connection,
    watchdog_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT claim_id, account_id, adset_id, expected_ad_name,
               expected_fingerprint, created_ad_id,
               expected_configured_status, expected_effective_status,
               created_at
        FROM launch_watchdog_targets
        WHERE watchdog_id = ?
        ORDER BY claim_id
        """,
        (watchdog_id,),
    ).fetchall()
    return [
        {
            "claim_id": str(row["claim_id"]),
            "account_id": str(row["account_id"]),
            "adset_id": str(row["adset_id"]),
            "expected_ad_name": str(row["expected_ad_name"]),
            "expected_fingerprint": str(row["expected_fingerprint"]),
            "created_ad_id": str(row["created_ad_id"]),
            "expected_configured_status": str(
                row["expected_configured_status"]
            ),
            "expected_effective_status": str(
                row["expected_effective_status"]
            ),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def _load_watchdog_observations(
    connection: sqlite3.Connection,
    watchdog_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT observation_id, attempt_no, fetch_complete, verified_count,
               outcome, evidence_json, evidence_sha256, observed_at
        FROM launch_verification_observations
        WHERE watchdog_id = ?
        ORDER BY attempt_no
        """,
        (watchdog_id,),
    ).fetchall()
    return [
        {
            "observation_id": str(row["observation_id"]),
            "attempt_no": int(row["attempt_no"]),
            "fetch_complete": bool(row["fetch_complete"]),
            "verified_count": int(row["verified_count"]),
            "outcome": str(row["outcome"]),
            "evidence": _parse_json(
                row["evidence_json"],
                "launch_verification_observations.evidence_json",
            ),
            "evidence_sha256": str(row["evidence_sha256"]),
            "observed_at": str(row["observed_at"]),
        }
        for row in rows
    ]


def _load_launch_verification(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT watchdog_id, decision_id, job_id, state, expected_count,
               verified_count, verification_attempts, next_verify_at,
               verify_deadline_at, created_at, updated_at, verified_at,
               last_reason_code
        FROM launch_watchdogs
        WHERE proposal_id = ?
        ORDER BY created_at, watchdog_id
        """,
        (proposal_id,),
    ).fetchall()
    watchdogs: list[dict[str, object]] = []
    for row in rows:
        watchdog_id = str(row["watchdog_id"])
        watchdogs.append(
            {
                "watchdog_id": watchdog_id,
                "decision_id": str(row["decision_id"]),
                "job_id": str(row["job_id"]),
                "state": str(row["state"]),
                "expected_count": int(row["expected_count"]),
                "verified_count": int(row["verified_count"]),
                "verification_attempts": int(row["verification_attempts"]),
                "next_verify_at": _optional_text(row["next_verify_at"]),
                "verify_deadline_at": str(row["verify_deadline_at"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "verified_at": _optional_text(row["verified_at"]),
                "last_reason_code": _optional_text(row["last_reason_code"]),
                "targets": _load_watchdog_targets(connection, watchdog_id),
                "observations": _load_watchdog_observations(
                    connection,
                    watchdog_id,
                ),
            }
        )
    return watchdogs


def _load_scheduler_runs(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT run_id, scheduler_name, slot_key, state, watchdog_id,
               created_at, updated_at, verified_at, last_reason_code
        FROM scheduler_action_runs
        WHERE proposal_id = ?
        ORDER BY created_at, run_id
        """,
        (proposal_id,),
    ).fetchall()
    return [
        {
            "run_id": str(row["run_id"]),
            "scheduler_name": str(row["scheduler_name"]),
            "slot_key": str(row["slot_key"]),
            "state": str(row["state"]),
            "watchdog_id": _optional_text(row["watchdog_id"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "verified_at": _optional_text(row["verified_at"]),
            "last_reason_code": _optional_text(row["last_reason_code"]),
        }
        for row in rows
    ]


def _lineage_document(
    connection: sqlite3.Connection,
    proposal_row: sqlite3.Row,
) -> dict[str, object]:
    proposal_id = str(proposal_row["proposal_id"])
    return {
        "schema_version": 1,
        "proposal": _proposal_document(proposal_row),
        "lifecycle": _load_lifecycle(connection, proposal_id),
        "evidence": _load_evidence(connection, proposal_id),
        "owner_decisions": _load_decisions(connection, proposal_id),
        "execution_jobs": _load_jobs(connection, proposal_id),
        "claims": _load_claims(connection, proposal_id),
        "events": _load_events(connection, proposal_id),
        "launch_verification": _load_launch_verification(
            connection,
            proposal_id,
        ),
        "scheduler_runs": _load_scheduler_runs(connection, proposal_id),
        "owner_feedback": _load_owner_feedback(connection, proposal_id),
        "action_verifications": _load_action_verifications(connection, proposal_id),
    }


def _document_counts(
    document: dict[str, object],
) -> tuple[int, int, int, int, int, int, int]:
    decisions = document["owner_decisions"]
    claims = document["claims"]
    watchdogs = document["launch_verification"]
    feedback = document["owner_feedback"]
    verifications = document["action_verifications"]
    if not isinstance(feedback, list) or not isinstance(verifications, list):
        raise OwnerTrainingExportDataError("Wave E lineage shape повреждён")
    if not isinstance(decisions, list) or not isinstance(claims, list):
        raise OwnerTrainingExportDataError("Export document shape повреждён")
    if not isinstance(watchdogs, list):
        raise OwnerTrainingExportDataError("Launch verification shape повреждён")
    attempt_count = 0
    outcome_count = 0
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim["attempts"], list):
            raise OwnerTrainingExportDataError("Claim lineage shape повреждён")
        attempt_count += len(claim["attempts"])
        for attempt in claim["attempts"]:
            if not isinstance(attempt, dict) or not isinstance(
                attempt["outcomes"],
                list,
            ):
                raise OwnerTrainingExportDataError(
                    "Attempt lineage shape повреждён"
                )
            outcome_count += len(attempt["outcomes"])
    observation_count = 0
    for watchdog in watchdogs:
        if not isinstance(watchdog, dict) or not isinstance(
            watchdog["observations"],
            list,
        ):
            raise OwnerTrainingExportDataError(
                "Verification lineage shape повреждён"
            )
        observation_count += len(watchdog["observations"])
    return (
        len(decisions),
        len(claims),
        attempt_count,
        outcome_count,
        observation_count,
        len(feedback),
        len(verifications),
    )


def export_owner_training_dataset(
    *,
    date_from: datetime,
    date_to: datetime,
    sink: TextIO,
    db_path: str | Path | None = None,
) -> ExportSummary:
    """Экспортирует proposal в полуоткрытом UTC-интервале ``[from, to)``.

    Экспорт использует SQLite ``mode=ro`` и не читает callback/permit secrets.
    Один canonical JSONL-документ содержит полную связанную историю proposal.
    """

    start, end = _validate_range(date_from, date_to)
    if not callable(getattr(sink, "write", None)):
        raise TypeError("sink должен поддерживать write(str)")
    path = _resolve_db_path(db_path)
    connection = _connect_read_only(path)
    digest = hashlib.sha256()
    proposal_count = 0
    decision_count = 0
    claim_count = 0
    attempt_count = 0
    outcome_count = 0
    observation_count = 0
    feedback_count = 0
    verification_count = 0
    byte_count = 0
    try:
        connection.execute("BEGIN")
        proposals = connection.execute(
            """
            SELECT proposal_id, proposal_version, proposal_kind, origin,
                   idempotency_key, source_ref, requested_by_actor, summary,
                   plan_json, plan_sha256, targets_sha256, evidence_sha256,
                   config_version_sha256, proposal_sha256, staged_media_root,
                   created_at, valid_until
            FROM owner_action_proposals
            WHERE created_at >= ? AND created_at < ?
            ORDER BY created_at, proposal_id
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        for proposal_row in proposals:
            document = _lineage_document(connection, proposal_row)
            (
                document_decisions,
                document_claims,
                document_attempts,
                document_outcomes,
                document_observations,
                document_feedback,
                document_verifications,
            ) = _document_counts(document)
            line_bytes = canonical_json(document) + b"\n"
            line = line_bytes.decode("utf-8")
            written = sink.write(line)
            if written is not None and written != len(line):
                raise OwnerTrainingExportError("sink записал JSONL не полностью")
            digest.update(line_bytes)
            proposal_count += 1
            decision_count += document_decisions
            claim_count += document_claims
            attempt_count += document_attempts
            outcome_count += document_outcomes
            observation_count += document_observations
            feedback_count += document_feedback
            verification_count += document_verifications
            byte_count += len(line_bytes)
    finally:
        connection.close()
    return ExportSummary(
        date_from=start,
        date_to=end,
        proposal_count=proposal_count,
        decision_count=decision_count,
        claim_count=claim_count,
        attempt_count=attempt_count,
        outcome_count=outcome_count,
        verification_observation_count=observation_count,
        byte_count=byte_count,
        content_sha256=digest.hexdigest(),
        owner_feedback_count=feedback_count,
        action_verification_count=verification_count,
    )


__all__ = [
    "ExportSummary",
    "OwnerTrainingExportDataError",
    "OwnerTrainingExportError",
    "OwnerTrainingExportRangeError",
    "export_owner_training_dataset",
]
