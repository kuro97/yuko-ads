"""Сбор неизменяемых outcome-метрик для exact owner-action claim."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from services.owner_action_models import canonical_json


class OutcomeHorizon(StrEnum):
    """Поддерживаемые горизонты наблюдения после provider attempt."""

    IMMEDIATE = "IMMEDIATE"
    D1 = "D1"
    D3 = "D3"
    D7 = "D7"
    D30 = "D30"


_HORIZON_DELAYS = {
    OutcomeHorizon.IMMEDIATE: timedelta(0),
    OutcomeHorizon.D1: timedelta(days=1),
    OutcomeHorizon.D3: timedelta(days=3),
    OutcomeHorizon.D7: timedelta(days=7),
    OutcomeHorizon.D30: timedelta(days=30),
}
_OUTCOME_NAMESPACE = uuid.UUID("283e0f7a-36ea-5f85-b985-21980292fd50")


class OwnerOutcomeError(RuntimeError):
    """Базовая fail-closed ошибка сбора owner-action outcome."""


class OwnerOutcomeLineageNotFound(OwnerOutcomeError):
    """Exact proposal/claim/attempt lineage не найдена."""


class OwnerOutcomeNotDue(OwnerOutcomeError):
    """Запрошенный горизонт ещё не наступил."""


class OwnerOutcomeConflict(OwnerOutcomeError):
    """Горизонт уже записан с другим неизменяемым содержимым."""


@dataclass(frozen=True, slots=True)
class OutcomeMetricsContext:
    """Read-only контекст, передаваемый инъецированному сборщику метрик."""

    proposal_id: str
    job_id: str
    claim_id: str
    attempt_id: str
    operation_kind: str
    account_id: str
    resource_id: str
    exact_payload_sha256: str
    attempt_state: str
    provider_request_id: str | None
    started_at: datetime
    completed_at: datetime | None
    horizon: OutcomeHorizon
    observed_at: datetime


OutcomeMetricsReader = Callable[[OutcomeMetricsContext], Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class OutcomeCollectionResult:
    """Результат immutable insert либо точного idempotent replay."""

    outcome_id: str
    proposal_id: str
    claim_id: str
    attempt_id: str
    horizon: OutcomeHorizon
    observed_at: datetime
    metrics_sha256: str
    deduplicated: bool


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} должен быть непустой строкой")
    return value


def _require_aware(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field_name} должен содержать timezone")
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise OwnerOutcomeError(f"{field_name} в БД должен быть datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise OwnerOutcomeError(
            f"{field_name} в БД содержит невалидный datetime"
        ) from exc
    return _require_aware(parsed, field_name)


def _read_only_connection(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"SQLite БД не найдена: {path}")
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _load_context(
    db_path: str | Path,
    *,
    proposal_id: str,
    claim_id: str,
    horizon: OutcomeHorizon,
    observed_at: datetime,
) -> OutcomeMetricsContext:
    connection = _read_only_connection(db_path)
    try:
        row = connection.execute(
            """
            SELECT a.proposal_id, a.job_id, a.claim_id, a.attempt_id,
                   a.operation_kind, a.account_id, a.resource_id,
                   a.exact_payload_sha256, a.state, a.provider_request_id,
                   a.started_at, a.completed_at
            FROM owner_action_attempts AS a
            JOIN owner_action_proposal_targets AS t
              ON t.proposal_id = a.proposal_id
             AND t.claim_id = a.claim_id
            WHERE a.proposal_id = ? AND a.claim_id = ?
            """,
            (proposal_id, claim_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise OwnerOutcomeLineageNotFound(
            "OUTCOME_EXACT_CLAIM_ATTEMPT_NOT_FOUND"
        )
    started_at = _parse_datetime(row["started_at"], "started_at")
    completed_at = (
        None
        if row["completed_at"] is None
        else _parse_datetime(row["completed_at"], "completed_at")
    )
    anchor = completed_at or started_at
    if observed_at < anchor + _HORIZON_DELAYS[horizon]:
        raise OwnerOutcomeNotDue(f"OUTCOME_{horizon.value}_NOT_DUE")
    return OutcomeMetricsContext(
        proposal_id=str(row["proposal_id"]),
        job_id=str(row["job_id"]),
        claim_id=str(row["claim_id"]),
        attempt_id=str(row["attempt_id"]),
        operation_kind=str(row["operation_kind"]),
        account_id=str(row["account_id"]),
        resource_id=str(row["resource_id"]),
        exact_payload_sha256=str(row["exact_payload_sha256"]),
        attempt_state=str(row["state"]),
        provider_request_id=(
            None
            if row["provider_request_id"] is None
            else str(row["provider_request_id"])
        ),
        started_at=started_at,
        completed_at=completed_at,
        horizon=horizon,
        observed_at=observed_at,
    )


def _encode_metrics(metrics: Mapping[str, object]) -> tuple[str, str]:
    if not isinstance(metrics, Mapping):
        raise TypeError("metrics_reader должен вернуть Mapping")
    if not metrics:
        raise ValueError("Outcome metrics не могут быть пустыми")
    encoded = canonical_json(metrics)
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError("Outcome metrics должны быть JSON object")
    return encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()


def _matches_existing(
    row: sqlite3.Row,
    *,
    context: OutcomeMetricsContext,
    observed_at_iso: str,
    metrics_json: str,
    metrics_sha256: str,
) -> bool:
    return (
        str(row["job_id"]) == context.job_id
        and str(row["attempt_id"]) == context.attempt_id
        and str(row["observed_at"]) == observed_at_iso
        and str(row["metrics_json"]) == metrics_json
        and str(row["metrics_sha256"]) == metrics_sha256
    )


def collect_owner_action_outcome(
    db_path: str | Path,
    *,
    proposal_id: str,
    claim_id: str,
    horizon: OutcomeHorizon,
    metrics_reader: OutcomeMetricsReader,
    now: datetime,
) -> OutcomeCollectionResult:
    """Собирает и атомарно сохраняет один horizon для exact claim.

    Повтор с теми же ``now`` и metrics возвращает существующую запись. Любое
    расхождение по уже занятому horizon считается immutable-конфликтом.
    """

    normalized_proposal_id = _require_text(proposal_id, "proposal_id")
    normalized_claim_id = _require_text(claim_id, "claim_id")
    if not isinstance(horizon, OutcomeHorizon):
        raise TypeError("horizon должен быть OutcomeHorizon")
    if not callable(metrics_reader):
        raise TypeError("metrics_reader должен быть callable")
    observed_at = _require_aware(now, "now")
    context = _load_context(
        db_path,
        proposal_id=normalized_proposal_id,
        claim_id=normalized_claim_id,
        horizon=horizon,
        observed_at=observed_at,
    )
    metrics_json, metrics_sha256 = _encode_metrics(metrics_reader(context))
    observed_at_iso = observed_at.isoformat()
    outcome_id = str(
        uuid.uuid5(
            _OUTCOME_NAMESPACE,
            f"{context.proposal_id}|{context.claim_id}|{horizon.value}",
        )
    )

    connection = sqlite3.connect(
        str(Path(db_path)),
        timeout=30,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT outcome_id, job_id, attempt_id, observed_at,
                   metrics_json, metrics_sha256
            FROM owner_action_outcomes
            WHERE proposal_id = ? AND claim_id = ? AND horizon = ?
            """,
            (context.proposal_id, context.claim_id, horizon.value),
        ).fetchone()
        if existing is not None:
            if not _matches_existing(
                existing,
                context=context,
                observed_at_iso=observed_at_iso,
                metrics_json=metrics_json,
                metrics_sha256=metrics_sha256,
            ):
                raise OwnerOutcomeConflict("OUTCOME_HORIZON_CONFLICT")
            connection.commit()
            return OutcomeCollectionResult(
                outcome_id=str(existing["outcome_id"]),
                proposal_id=context.proposal_id,
                claim_id=context.claim_id,
                attempt_id=context.attempt_id,
                horizon=horizon,
                observed_at=observed_at,
                metrics_sha256=metrics_sha256,
                deduplicated=True,
            )
        connection.execute(
            """
            INSERT INTO owner_action_outcomes (
                outcome_id, proposal_id, job_id, claim_id, attempt_id,
                horizon, observed_at, metrics_json, metrics_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outcome_id,
                context.proposal_id,
                context.job_id,
                context.claim_id,
                context.attempt_id,
                horizon.value,
                observed_at_iso,
                metrics_json,
                metrics_sha256,
            ),
        )
        connection.commit()
        return OutcomeCollectionResult(
            outcome_id=outcome_id,
            proposal_id=context.proposal_id,
            claim_id=context.claim_id,
            attempt_id=context.attempt_id,
            horizon=horizon,
            observed_at=observed_at,
            metrics_sha256=metrics_sha256,
            deduplicated=False,
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "OutcomeCollectionResult",
    "OutcomeHorizon",
    "OutcomeMetricsContext",
    "OutcomeMetricsReader",
    "OwnerOutcomeConflict",
    "OwnerOutcomeError",
    "OwnerOutcomeLineageNotFound",
    "OwnerOutcomeNotDue",
    "collect_owner_action_outcome",
]
