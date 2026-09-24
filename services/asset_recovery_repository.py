"""Durable claim для одного existing-creative CREATE."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from services.approval_audit import verify_attempt_attestation
from services.approval_checker_models import (
    AssetRecoveryManifest,
    OperationAttemptAttestation,
    manifest_sha256,
)


class AssetRecoveryRepositoryError(RuntimeError):
    """Durable authorization нельзя безопасно создать или использовать."""


@dataclass(frozen=True, slots=True)
class AssetRecoveryAuthorization:
    auth_id: str
    secret: str = field(repr=False)


def _connect() -> sqlite3.Connection:
    from services.creative_intelligence import DB_PATH

    if DB_PATH is None:
        raise AssetRecoveryRepositoryError("KB не инициализирована")
    connection = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime должен содержать timezone")
    return value.isoformat()


def reserve_authorization(
    manifest: AssetRecoveryManifest,
    attempt: OperationAttemptAttestation,
    now: datetime,
) -> AssetRecoveryAuthorization:
    """WAL attestation проверяется до SQL reservation; replay не выдаёт secret."""

    verify_attempt_attestation(attempt)
    if (
        attempt.action_kind is not manifest.kind
        or attempt.item_id != manifest.manifest_id
        or attempt.item_manifest_sha256 != manifest_sha256(manifest)
    ):
        raise AssetRecoveryRepositoryError("attempt не совпадает с manifest")
    secret = secrets.token_urlsafe(32)
    authorization = AssetRecoveryAuthorization(str(uuid.uuid4()), secret)
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT phase FROM asset_recovery_authorizations WHERE attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()
        if existing is not None:
            raise AssetRecoveryRepositoryError(
                "attempt уже зарезервирован; secret повторно не выдаётся"
            )
        connection.execute(
            """
            INSERT INTO asset_recovery_authorizations (
                auth_id, attempt_id, secret_sha256, item_manifest_sha256,
                account_id, source_ad_id, source_adset_id, source_creative_id,
                source_identity_sha256, target_adset_id, target_ad_name,
                target_identity_key, pre_inventory_sha256, phase,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?)
            """,
            (
                authorization.auth_id,
                attempt.attempt_id,
                hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                attempt.item_manifest_sha256,
                manifest.account_id,
                manifest.source_ad_id,
                manifest.source_adset_id,
                manifest.source_creative_id,
                manifest.source_identity_sha256,
                manifest.target_adset_id,
                manifest.target_ad_name,
                manifest.target_identity_key,
                manifest.pre_inventory_sha256,
                _iso(now),
                _iso(now),
            ),
        )
        connection.commit()
        return authorization
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_create(
    authorization: AssetRecoveryAuthorization,
    manifest: AssetRecoveryManifest,
    now: datetime,
) -> None:
    """Фиксирует CREATE_STARTED до первого сетевого POST."""

    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM asset_recovery_authorizations WHERE auth_id = ?",
            (authorization.auth_id,),
        ).fetchone()
        if row is None or row["phase"] != "RESERVED":
            raise AssetRecoveryRepositoryError("authorization не находится в RESERVED")
        if not hmac.compare_digest(
            str(row["secret_sha256"]),
            hashlib.sha256(authorization.secret.encode("utf-8")).hexdigest(),
        ):
            raise AssetRecoveryRepositoryError("authorization secret неверен")
        expected = (
            str(row["item_manifest_sha256"]) == manifest_sha256(manifest),
            str(row["account_id"]) == manifest.account_id,
            str(row["source_ad_id"]) == manifest.source_ad_id,
            str(row["source_adset_id"]) == manifest.source_adset_id,
            str(row["source_creative_id"]) == manifest.source_creative_id,
            str(row["source_identity_sha256"]) == manifest.source_identity_sha256,
            str(row["target_adset_id"]) == manifest.target_adset_id,
            str(row["target_ad_name"]) == manifest.target_ad_name,
            str(row["target_identity_key"]) == manifest.target_identity_key,
            str(row["pre_inventory_sha256"]) == manifest.pre_inventory_sha256,
        )
        if not all(expected):
            raise AssetRecoveryRepositoryError("authorization scope изменён")
        updated = connection.execute(
            """
            UPDATE asset_recovery_authorizations
            SET phase = 'CREATE_STARTED', updated_at = ?
            WHERE auth_id = ? AND phase = 'RESERVED'
            """,
            (_iso(now), authorization.auth_id),
        ).rowcount
        if updated != 1:
            raise AssetRecoveryRepositoryError("CREATE_STARTED CAS не выполнен")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_failed(auth_id: str, now: datetime) -> None:
    """Освобождает exact target только если CREATE точно не начинался."""

    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE asset_recovery_authorizations
            SET phase = 'FAILED', updated_at = ?
            WHERE auth_id = ? AND phase = 'RESERVED'
            """,
            (_iso(now), auth_id),
        ).rowcount
        if updated != 1:
            raise AssetRecoveryRepositoryError("FAILED CAS не выполнен")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_created(auth_id: str, ad_id: str, now: datetime) -> None:
    """Сохраняет numeric provider ID; невалидный ответ остаётся reconcile-only."""

    if not isinstance(ad_id, str) or not ad_id.isdigit():
        raise AssetRecoveryRepositoryError("created ad_id должен быть numeric")
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE asset_recovery_authorizations
            SET phase = 'CREATED', created_ad_id = ?, updated_at = ?
            WHERE auth_id = ? AND phase = 'CREATE_STARTED' AND created_ad_id IS NULL
            """,
            (ad_id, _iso(now), auth_id),
        ).rowcount
        if updated != 1:
            raise AssetRecoveryRepositoryError("CREATED CAS не выполнен")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_reconcile_required(auth_id: str, now: datetime) -> None:
    """После CREATE_STARTED любой неоднозначный исход запрещает новый POST."""

    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE asset_recovery_authorizations
            SET phase = 'BLOCKED_RECONCILE', updated_at = ?
            WHERE auth_id = ? AND phase IN ('CREATE_STARTED','CREATED')
            """,
            (_iso(now), auth_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_created_id_reconcile_required(created_ad_id: str, now: datetime) -> None:
    """Переводит provider success в reconcile-only при postcondition drift."""

    if not created_ad_id.isdigit():
        raise AssetRecoveryRepositoryError("created_ad_id должен быть numeric")
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE asset_recovery_authorizations
            SET phase = 'BLOCKED_RECONCILE', updated_at = ?
            WHERE created_ad_id = ? AND phase = 'CREATED'
            """,
            (_iso(now), created_ad_id),
        ).rowcount
        if updated != 1:
            raise AssetRecoveryRepositoryError("created authorization не найдена")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_attempt_state(attempt_id: str) -> dict[str, str] | None:
    """Возвращает минимальное состояние без secret для crash reconciliation."""

    connection = _connect()
    try:
        row = connection.execute(
            """
            SELECT auth_id, phase, created_ad_id
            FROM asset_recovery_authorizations WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "auth_id": str(row["auth_id"]),
            "phase": str(row["phase"]),
            "created_ad_id": str(row["created_ad_id"] or ""),
        }
    finally:
        connection.close()
