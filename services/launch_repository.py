"""Durable SQLite-граница launch checker и provider CREATE claims.

Модуль не импортирует checker во время выполнения и не вызывает внешние API.
Все смены состояния выполняются через ``BEGIN IMMEDIATE`` в общей decisions.db.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence


RESERVATION_TTL = timedelta(minutes=30)
# Заливка одного видео живёт минуты; часовой возраст UPLOAD_STARTED означает,
# что процесс убили посреди upload-а (SIGKILL при рестарте) и попытка осиротела.
_ORPHANED_UPLOAD_TTL = timedelta(hours=1)
WATCHDOG_LEASE_TTL = timedelta(seconds=45)
WATCHDOG_VERIFY_TTL = timedelta(hours=24)
SCHEDULER_LEASE_TTL = timedelta(seconds=60)
WATCHDOG_RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=2),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=30),
)

_OPEN_AUTH_PHASES = frozenset(
    {"RESERVED", "CREATE_STARTED", "PARTIAL", "BLOCKED_RECONCILE"}
)
_ACTIVE_AUTH_PHASES = frozenset({"RESERVED", "CREATE_STARTED", "PARTIAL"})
_OPEN_TARGET_PHASES = frozenset(
    {"RESERVED", "CREATE_STARTED", "PARTIAL", "BLOCKED_RECONCILE"}
)
_OPEN_AD_PHASES = frozenset(
    {"RESERVED", "CREATE_STARTED", "BLOCKED_RECONCILE"}
)
_SOURCES = frozenset(
    {"CRON", "AUTO_LAUNCH_NOW", "MANUAL", "BATCH", "AGENT_RUN", "RECOVERY"}
)
_AUDIT_EVENTS = frozenset(
    {
        "CANDIDATE",
        "PREFLIGHT",
        "RESERVED",
        "DENIED",
        "OVERRIDE_ACCEPTED",
        "CREATE_CLAIMED",
        "CREATE_CONFIRMED",
        "PARTIAL",
        "COMPLETED",
        "BLOCKED",
        "RELEASED",
    }
)
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(access[_-]?token|api[_-]?key|authorization|password|secret)"
        r"\s*[:=]\s*['\"]?[^\s&,'\"}]+"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+\-/=]+"),
    re.compile(r"\bEAA[A-Za-z0-9_-]{8,}\b"),
)
_URL_WITH_QUERY_RE = re.compile(r"(?i)\bhttps?://[^\s?]+\?[^\s\"'<>]+")
_SENSITIVE_JSON_KEYS = frozenset(
    {
        "access_token",
        "accesstoken",
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "cookie",
        "set_cookie",
    }
)


class LaunchRepositoryError(RuntimeError):
    """Базовая ошибка durable launch repository."""


class LaunchRepositoryBlocked(LaunchRepositoryError):
    """Типизированный fail-closed результат, переводимый checker-слоем."""

    def __init__(
        self,
        code: str,
        reasons: tuple[str, ...] | Sequence[str],
        check_id: str | None = None,
    ) -> None:
        self.code = _required_text(code, "code")
        self.reasons = tuple(str(reason) for reason in reasons)
        self.check_id = _optional_text(check_id)
        super().__init__(f"{self.code}: {'; '.join(self.reasons)}")


class ProviderProofLike(Protocol):
    auth_id: str
    secret: str


class ProviderScopeLike(Protocol):
    account_kind: str
    account_id: str
    city: str
    adset_id: str
    ad_name: str
    media_sha256: str


@dataclass(frozen=True, slots=True)
class ProviderLaunchAuthorizationData:
    """Локальный совместимый DTO; repr никогда не раскрывает secret."""

    auth_id: str
    secret: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(auth_id={self.auth_id!r}, "
            "secret='<redacted>')"
        )


@dataclass(frozen=True, slots=True)
class ProviderCreateScopeData:
    account_kind: str
    account_id: str
    city: str
    adset_id: str
    ad_name: str
    media_sha256: str


@dataclass(frozen=True, slots=True)
class LaunchAuditEvent:
    check_id: str
    event_type: str
    source: str
    card_id: str
    actor: str
    auth_id: str | None = None
    reason_codes: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    event_id: str = field(default_factory=lambda: f"launch-event-{uuid.uuid4().hex}")


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    auth_id: str
    phase: str
    total_ads: int
    reserved_ads: int
    create_started_ads: int
    created_ads: int
    released_ads: int
    blocked_reconcile_ads: int
    created_ad_ids: tuple[str, ...]
    needs_reconcile: bool
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TtlTransitionResult:
    released_auth_ids: tuple[str, ...]
    blocked_reconcile_auth_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthorizedTargetScope:
    """Immutable exact target scope, разрешённый до provider upload."""

    city: str
    ordinal: int
    account_kind: str
    account_id: str
    adset_id: str
    identity_key: str
    expected_names: tuple[str, ...]
    expected_name_keys: tuple[str, ...]
    reserved_slots: int
    phase: str
    reservation_expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthorizationValidation:
    """Несекретный immutable snapshot успешно проверенного provider proof."""

    auth_id: str
    card_id: str
    source: str
    campaign_type: str
    account_kind: str
    account_id: str
    plan_sha256: str
    media_sha256: str
    phase: str
    expires_at: datetime
    targets: tuple[AuthorizedTargetScope, ...]


@dataclass(frozen=True, slots=True)
class TargetRenewalResult:
    """Результат атомарного продления одного exact target."""

    auth_id: str
    city: str
    adset_id: str
    expires_at: datetime
    authorization_phase: str
    target_phase: str


@dataclass(frozen=True, slots=True)
class TrustedLiveTarget:
    """Полный exact live-снимок одного target для trusted reconciliation."""

    city: str
    account_id: str
    adset_id: str
    inventory_complete: bool
    exact_matches: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class TrustedReconciliationResult:
    """Итог exact live reconciliation и опциональной ротации proof."""

    auth_id: str
    phase: str
    authorization: ProviderLaunchAuthorizationData | None
    expires_at: datetime | None
    created_ad_ids: tuple[str, ...]
    missing_names_by_city: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class LaunchWatchdogTarget:
    """Exact созданное объявление, которое должен подтвердить watchdog."""

    claim_id: str
    account_id: str
    adset_id: str
    expected_ad_name: str
    expected_fingerprint: str
    created_ad_id: str


@dataclass(frozen=True, slots=True)
class LaunchWatchdogLease:
    """Захваченная lease на read-only проверку одного запуска."""

    watchdog_id: str
    proposal_id: str
    decision_id: str
    job_id: str
    state: str
    attempt_no: int
    lease_token: str
    lease_until: datetime
    verify_deadline_at: datetime
    targets: tuple[LaunchWatchdogTarget, ...]


@dataclass(frozen=True, slots=True)
class LaunchTargetObservation:
    """Нормализованное live-наблюдение exact Facebook ad."""

    created_ad_id: str
    account_id: str
    adset_id: str
    ad_name: str
    configured_status: str
    effective_status: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class LaunchWatchdogTransition:
    """Результат одной durable проверки watchdog."""

    watchdog_id: str
    proposal_id: str
    state: str
    outcome: str
    verified_count: int
    expected_count: int
    next_verify_at: datetime | None
    reason_code: str


@dataclass(frozen=True, slots=True)
class SchedulerRunLease:
    """Durable unique slot планировщика, пригодный для resume."""

    run_id: str
    scheduler_name: str
    slot_key: str
    state: str
    proposal_id: str | None
    watchdog_id: str | None
    lease_token: str | None
    lease_until: datetime | None
    acquired: bool


@dataclass(frozen=True, slots=True)
class _NormalizedTarget:
    city: str
    ordinal: int
    account_kind: str
    account_id: str
    adset_id: str
    identity_key: str
    names: tuple[str, ...]
    name_keys: tuple[str, ...]
    reserved_slots: int


@dataclass(frozen=True, slots=True)
class _NormalizedPlan:
    auth_id: str
    check_id: str
    card_id: str
    card_name: str
    source: str
    campaign_type: str
    account_kind: str
    account_id: str
    plan_sha256: str
    media_sha256: str
    topic_override: bool
    topic_override_reason: str | None
    actor: str
    recovery_plan_id: str | None
    targets: tuple[_NormalizedTarget, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(moment: datetime) -> datetime:
    if not isinstance(moment, datetime):
        raise TypeError("now должен быть datetime")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("datetime должен содержать timezone")
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return _aware_utc(moment).isoformat(timespec="microseconds")


def _parse_iso(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise LaunchRepositoryError("В БД хранится некорректный timestamp") from exc
    return _aware_utc(parsed)


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} не должен быть пустым")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _enum_text(value: object, field_name: str) -> str:
    if isinstance(value, Enum):
        value = value.value
    return _required_text(value, field_name)


def _field(source: object, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _sha256(value: object, field_name: str) -> str:
    text = _required_text(value, field_name).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{field_name} должен быть SHA-256 hex")
    return text


def _ttl_seconds(ttl: timedelta) -> float:
    if not isinstance(ttl, timedelta):
        raise TypeError("ttl должен быть timedelta")
    seconds = ttl.total_seconds()
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("ttl должен быть положительным")
    return seconds


def sanitize_launch_text(value: object) -> str:
    """Удаляет query strings и известные формы secrets перед аудитом."""
    text = str(value or "")
    text = _URL_WITH_QUERY_RE.sub(
        lambda match: f"{match.group(0).split('?', 1)[0]}?<redacted>", text
    )
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, nested in value.items():
            key = sanitize_launch_text(raw_key)
            normalized_key = key.strip().lower().replace("-", "_")
            result[key] = (
                "[REDACTED]"
                if normalized_key in _SENSITIVE_JSON_KEYS
                else _sanitize_json(nested)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, str):
        return sanitize_launch_text(value)
    return value


def _json_dumps(value: Mapping[str, Any] | Sequence[Any]) -> str:
    return json.dumps(
        _sanitize_json(value),
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )


def _exact_json_dumps(value: Mapping[str, Any] | Sequence[Any]) -> str:
    """Канонический JSON exact plan-полей; audit использует sanitizer отдельно."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )


def normalize_launch_name(value: str) -> str:
    """NFKC → product-tag strip → trim → whitespace collapse → casefold."""
    from services.product_tags import PRODUCTS

    normalized = unicodedata.normalize("NFKC", _required_text(value, "name"))
    tag_names = "|".join(
        re.escape(str(metadata["tag"])[1:-1]) for metadata in PRODUCTS.values()
    )
    normalized = re.sub(
        rf"\s*\[(?:{tag_names})\]\s*$",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = " ".join(normalized.split()).casefold()
    if not normalized:
        raise ValueError("Нормализованное launch-имя не должно быть пустым")
    return normalized


def launch_identity_key(city: str, card_name: str) -> str:
    """Возвращает server-owned identity без asset suffix/product tag."""
    return normalize_launch_name(
        f"{_required_text(city, 'city')} | {_required_text(card_name, 'card_name')}"
    )


def _get_connection() -> sqlite3.Connection:
    """Открывает штатную SQLite с явным transaction control."""
    from services.creative_intelligence import DB_PATH

    if DB_PATH is None:
        raise RuntimeError("KB не инициализирована. Вызовите init_kb() при старте.")
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _request_from_plan(plan: object) -> object:
    request = _field(plan, "request")
    if request is None:
        raise ValueError("plan.request обязателен")
    return request


def _names_for_target(plan: object, target: object, city: str) -> tuple[str, ...]:
    expected_by_city = _field(plan, "expected_names_by_city")
    raw_names: object = None
    if isinstance(expected_by_city, Mapping):
        raw_names = expected_by_city.get(city)
    if raw_names is None:
        raw_names = _field(target, "expected_names")
    if not isinstance(raw_names, Sequence) or isinstance(raw_names, (str, bytes)):
        raise ValueError(f"Нет expected names для города {city}")
    names = tuple(_required_text(name, "ad_name") for name in raw_names)
    if not names or len(names) != len(set(names)):
        raise ValueError(f"Expected names для {city} должны быть непустыми и уникальными")
    keys = tuple(normalize_launch_name(name) for name in names)
    if len(keys) != len(set(keys)):
        raise ValueError(f"Expected names для {city} совпадают после нормализации")
    return names


def _canonical_plan_sha256(
    *,
    card_id: str,
    card_name: str,
    source: str,
    campaign_type: str,
    media_sha256: str,
    targets: Sequence[_NormalizedTarget],
) -> str:
    payload = {
        "card_id": card_id,
        "card_name": card_name,
        "source": source,
        "campaign_type": campaign_type,
        "media_sha256": media_sha256,
        "targets": [
            {
                "city": target.city,
                "ordinal": target.ordinal,
                "account_kind": target.account_kind,
                "account_id": target.account_id,
                "adset_id": target.adset_id,
                "identity_key": target.identity_key,
                "names": list(target.names),
            }
            for target in targets
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_plan(plan: object) -> _NormalizedPlan:
    request = _request_from_plan(plan)
    authorization = _field(plan, "authorization")
    if authorization is None:
        raise LaunchRepositoryBlocked(
            "AUTHORIZATION_REQUIRED",
            ("Enforce-план не содержит provider authorization",),
            _optional_text(_field(plan, "check_id")),
        )
    auth_id = _required_text(_field(authorization, "auth_id"), "authorization.auth_id")
    check_id = _required_text(_field(plan, "check_id"), "check_id")
    card_id = _required_text(_field(plan, "card_id"), "card_id")
    card_name = _required_text(_field(plan, "card_name"), "card_name")
    source = _enum_text(_field(request, "source"), "request.source").upper()
    if source not in _SOURCES:
        raise ValueError(f"Неподдерживаемый source: {source}")
    campaign_type = _required_text(
        _field(request, "campaign_type"), "request.campaign_type"
    )
    media_sha256 = _sha256(_field(plan, "media_sha256"), "media_sha256")
    actor = _required_text(_field(request, "actor", "system"), "request.actor")
    topic_override = bool(_field(request, "override_topic_veto", False))
    topic_override_reason = _optional_text(_field(request, "override_reason"))
    if topic_override and (
        topic_override_reason is None or not 10 <= len(topic_override_reason) <= 300
    ):
        raise ValueError("veto override reason должен содержать 10–300 символов")
    if not topic_override and topic_override_reason is not None:
        raise ValueError("override_reason допустим только с override_topic_veto=true")

    raw_targets = _field(plan, "targets")
    if not isinstance(raw_targets, Sequence) or isinstance(raw_targets, (str, bytes)):
        raise ValueError("plan.targets должен быть непустой Sequence")
    normalized_targets: list[_NormalizedTarget] = []
    cities: set[str] = set()
    ordinals: set[int] = set()
    for fallback_ordinal, target in enumerate(raw_targets):
        city = _required_text(_field(target, "city"), "target.city")
        if city in cities:
            raise ValueError(f"Город {city} повторяется в targets")
        cities.add(city)
        raw_ordinal = _field(target, "ordinal", fallback_ordinal)
        if type(raw_ordinal) is not int or raw_ordinal < 0:
            raise ValueError("target.ordinal должен быть целым >= 0")
        if raw_ordinal in ordinals:
            raise ValueError("target.ordinal должен быть уникальным")
        ordinals.add(raw_ordinal)
        account_kind = _required_text(
            _field(target, "account_kind"), "target.account_kind"
        ).lower()
        if account_kind not in {"offline", "online"}:
            raise ValueError("target.account_kind должен быть offline или online")
        account_id = _required_text(_field(target, "account_id"), "target.account_id")
        adset_id = _required_text(_field(target, "adset_id"), "target.adset_id")
        names = _names_for_target(plan, target, city)
        identity_key = launch_identity_key(city, card_name)
        supplied_identity = _optional_text(_field(target, "identity_key"))
        if supplied_identity is not None and supplied_identity != identity_key:
            raise ValueError("target.identity_key не совпадает с server-owned normalization")
        reserved_slots = _field(target, "reserved_slots", len(names))
        if type(reserved_slots) is not int or reserved_slots != len(names):
            raise ValueError("reserved_slots должен совпадать с expected ads count")
        normalized_targets.append(
            _NormalizedTarget(
                city=city,
                ordinal=raw_ordinal,
                account_kind=account_kind,
                account_id=account_id,
                adset_id=adset_id,
                identity_key=identity_key,
                names=names,
                name_keys=tuple(normalize_launch_name(name) for name in names),
                reserved_slots=reserved_slots,
            )
        )
    if not normalized_targets:
        raise ValueError("plan.targets не должен быть пустым")
    normalized_targets.sort(key=lambda item: item.ordinal)
    accounts = {(target.account_kind, target.account_id) for target in normalized_targets}
    if len(accounts) != 1:
        raise ValueError("Все targets одной authorization должны принадлежать одному account")
    account_kind, account_id = next(iter(accounts))
    recovery_plan_id = _optional_text(_field(plan, "recovery_plan_id"))
    if (source == "RECOVERY") != (recovery_plan_id is not None):
        raise ValueError("RECOVERY требует recovery_plan_id, остальные source запрещают его")
    plan_sha256 = _optional_text(_field(plan, "plan_sha256"))
    if plan_sha256 is None:
        plan_sha256 = _canonical_plan_sha256(
            card_id=card_id,
            card_name=card_name,
            source=source,
            campaign_type=campaign_type,
            media_sha256=media_sha256,
            targets=normalized_targets,
        )
    else:
        plan_sha256 = _sha256(plan_sha256, "plan_sha256")
    return _NormalizedPlan(
        auth_id=auth_id,
        check_id=check_id,
        card_id=card_id,
        card_name=card_name,
        source=source,
        campaign_type=campaign_type,
        account_kind=account_kind,
        account_id=account_id,
        plan_sha256=plan_sha256,
        media_sha256=media_sha256,
        topic_override=topic_override,
        topic_override_reason=topic_override_reason,
        actor=actor,
        recovery_plan_id=recovery_plan_id,
        targets=tuple(normalized_targets),
    )


def _insert_audit_locked(conn: sqlite3.Connection, event: LaunchAuditEvent) -> None:
    event_type = _required_text(event.event_type, "event_type").upper()
    if event_type not in _AUDIT_EVENTS:
        raise ValueError(f"Неподдерживаемый launch audit event: {event_type}")
    source = _enum_text(event.source, "source").upper()
    if source not in _SOURCES:
        raise ValueError(f"Неподдерживаемый source: {source}")
    conn.execute(
        """
        INSERT INTO launch_check_audit (
            event_id, check_id, auth_id, event_type, source, card_id, actor,
            reason_codes_json, evidence_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _required_text(event.event_id, "event_id"),
            _required_text(event.check_id, "check_id"),
            _optional_text(event.auth_id),
            event_type,
            source,
            _required_text(event.card_id, "card_id"),
            sanitize_launch_text(_required_text(event.actor, "actor")),
            _json_dumps(tuple(str(code) for code in event.reason_codes)),
            _json_dumps(event.evidence),
            _iso(event.created_at),
        ),
    )


def _audit_for_auth_locked(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    event_type: str,
    now: datetime,
    *,
    reason_codes: tuple[str, ...] = (),
    evidence: Mapping[str, Any] | None = None,
) -> None:
    _insert_audit_locked(
        conn,
        LaunchAuditEvent(
            check_id=str(row["check_id"]) if "check_id" in row.keys() else str(row["auth_id"]),
            auth_id=str(row["auth_id"]),
            event_type=event_type,
            source=str(row["source"]),
            card_id=str(row["card_id"]),
            actor=str(row["actor"]),
            reason_codes=reason_codes,
            evidence=evidence or {},
            created_at=now,
        ),
    )


def _authorization_audit_row(conn: sqlite3.Connection, auth_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT a.auth_id,
               COALESCE(
                   (SELECT audit.check_id
                    FROM launch_check_audit AS audit
                    WHERE audit.auth_id = a.auth_id
                    ORDER BY audit.id LIMIT 1),
                   a.auth_id
               ) AS check_id,
               a.source, a.card_id, a.actor
        FROM launch_authorizations AS a WHERE a.auth_id = ?
        """,
        (auth_id,),
    ).fetchone()
    if row is None:
        raise LaunchRepositoryBlocked(
            "AUTHORIZATION_NOT_FOUND", ("Authorization не найдена",), None
        )
    return row


def _transition_expired_locked(
    conn: sqlite3.Connection, now: datetime
) -> TtlTransitionResult:
    now_iso = _iso(now)
    rows = conn.execute(
        """
        SELECT DISTINCT a.auth_id, a.source, a.card_id, a.actor,
               COALESCE(
                   (SELECT audit.check_id
                    FROM launch_check_audit AS audit
                    WHERE audit.auth_id = a.auth_id
                    ORDER BY audit.id LIMIT 1),
                   a.auth_id
               ) AS check_id
        FROM launch_authorizations AS a
        JOIN launch_authorization_targets AS t ON t.auth_id = a.auth_id
        WHERE a.phase IN ('RESERVED','CREATE_STARTED','PARTIAL')
          AND t.phase IN ('RESERVED','CREATE_STARTED','PARTIAL')
          AND t.reservation_expires_at <= ?
        ORDER BY a.auth_id
        """,
        (now_iso,),
    ).fetchall()
    released: list[str] = []
    blocked: list[str] = []
    for row in rows:
        auth_id = str(row["auth_id"])
        claimed = conn.execute(
            """
            SELECT 1 FROM launch_authorization_ads
            WHERE auth_id = ?
              AND (claim_id IS NOT NULL OR created_ad_id IS NOT NULL
                   OR phase IN ('CREATE_STARTED','CREATED','BLOCKED_RECONCILE'))
            LIMIT 1
            """,
            (auth_id,),
        ).fetchone()
        if claimed is None:
            conn.execute(
                """
                UPDATE launch_authorization_ads
                SET phase = 'RELEASED', updated_at = ?
                WHERE auth_id = ? AND phase = 'RESERVED'
                """,
                (now_iso, auth_id),
            )
            conn.execute(
                """
                UPDATE launch_authorization_targets
                SET phase = 'RELEASED', updated_at = ?
                WHERE auth_id = ? AND phase = 'RESERVED'
                """,
                (now_iso, auth_id),
            )
            conn.execute(
                """
                UPDATE launch_authorizations
                SET phase = 'RELEASED', updated_at = ?, finished_at = ?
                WHERE auth_id = ? AND phase IN ('RESERVED','CREATE_STARTED','PARTIAL')
                """,
                (now_iso, now_iso, auth_id),
            )
            _audit_for_auth_locked(
                conn,
                row,
                "RELEASED",
                now,
                reason_codes=("RESERVATION_EXPIRED",),
            )
            released.append(auth_id)
            continue
        conn.execute(
            """
            UPDATE launch_authorization_ads
            SET phase = 'BLOCKED_RECONCILE', updated_at = ?
            WHERE auth_id = ? AND phase IN ('RESERVED','CREATE_STARTED')
            """,
            (now_iso, auth_id),
        )
        conn.execute(
            """
            UPDATE launch_authorization_targets
            SET phase = 'BLOCKED_RECONCILE', updated_at = ?
            WHERE auth_id = ? AND phase IN ('RESERVED','CREATE_STARTED','PARTIAL')
            """,
            (now_iso, auth_id),
        )
        conn.execute(
            """
            UPDATE launch_authorizations
            SET phase = 'BLOCKED_RECONCILE', updated_at = ?
            WHERE auth_id = ? AND phase IN ('RESERVED','CREATE_STARTED','PARTIAL')
            """,
            (now_iso, auth_id),
        )
        _audit_for_auth_locked(
            conn,
            row,
            "BLOCKED",
            now,
            reason_codes=("CREATE_RECONCILE_REQUIRED",),
        )
        blocked.append(auth_id)
    return TtlTransitionResult(tuple(released), tuple(blocked))


def expire_stale_reservations(now: datetime) -> TtlTransitionResult:
    """Освобождает только unclaimed TTL; claimed переводит в reconcile."""
    now = _aware_utc(now)
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _transition_expired_locked(conn, now)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_reserved_slots(
    adset_id: str,
    now: datetime,
    *,
    exclude_auth_id: str | None = None,
) -> int:
    """Read-only считает чужие OPEN reservations exact adset.

    Любая ошибка SQLite переводится в typed fail-closed результат: caller не
    должен считать ошибку чтения нулевой бронью. ``BLOCKED_RECONCILE`` не имеет
    безопасного TTL и занимает capacity до явного trusted reconciliation.
    """
    adset_id = _required_text(adset_id, "adset_id")
    exclude_auth_id = _optional_text(exclude_auth_id)
    now = _aware_utc(now)
    try:
        conn = _get_connection()
    except (sqlite3.Error, RuntimeError) as exc:
        raise LaunchRepositoryBlocked(
            "RESERVATION_LOOKUP_FAILED",
            ("Не удалось открыть durable reservation store",),
            None,
        ) from exc
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(reserved_slots), 0) AS reserved_slots
            FROM launch_authorization_targets
            WHERE adset_id = ?
              AND phase IN (
                  'RESERVED','CREATE_STARTED','PARTIAL','BLOCKED_RECONCILE'
              )
              AND (phase = 'BLOCKED_RECONCILE' OR reservation_expires_at > ?)
              AND (? IS NULL OR auth_id <> ?)
            """,
            (adset_id, _iso(now), exclude_auth_id, exclude_auth_id),
        ).fetchone()
        if row is None or type(row["reserved_slots"]) is not int:
            raise LaunchRepositoryBlocked(
                "RESERVATION_STATE_INVALID",
                ("Durable reservation sum имеет некорректный тип",),
                None,
            )
        reserved_slots = int(row["reserved_slots"])
        if reserved_slots < 0:
            raise LaunchRepositoryBlocked(
                "RESERVATION_STATE_INVALID",
                ("Durable reservation sum не может быть отрицательной",),
                None,
            )
        return reserved_slots
    except LaunchRepositoryBlocked:
        raise
    except sqlite3.Error as exc:
        raise LaunchRepositoryBlocked(
            "RESERVATION_LOOKUP_FAILED",
            ("Не удалось прочитать durable reservations",),
            None,
        ) from exc
    finally:
        conn.close()


def _find_collision(
    conn: sqlite3.Connection, plan: _NormalizedPlan
) -> tuple[str, str] | None:
    existing_card = conn.execute(
        """
        SELECT auth_id FROM launch_authorizations
        WHERE card_id = ? AND campaign_type = ?
          AND phase IN ('RESERVED','CREATE_STARTED','PARTIAL','BLOCKED_RECONCILE')
        LIMIT 1
        """,
        (plan.card_id, plan.campaign_type),
    ).fetchone()
    if existing_card is not None:
        return "card", str(existing_card["auth_id"])
    for target in plan.targets:
        identity = conn.execute(
            """
            SELECT auth_id FROM launch_authorization_targets
            WHERE account_id = ? AND adset_id = ? AND identity_key = ?
              AND phase IN ('RESERVED','CREATE_STARTED','PARTIAL','BLOCKED_RECONCILE')
            LIMIT 1
            """,
            (target.account_id, target.adset_id, target.identity_key),
        ).fetchone()
        if identity is not None:
            return "identity", str(identity["auth_id"])
        # Не строим SQL динамически даже для placeholders: число имён мало,
        # а отдельные parameterized SELECT сохраняют строгую SQL-границу.
        for name_key in target.name_keys:
            exact = conn.execute(
                """
                SELECT auth_id FROM launch_authorization_ads
                WHERE account_id = ? AND adset_id = ? AND ad_name_key = ?
                  AND phase IN ('RESERVED','CREATE_STARTED','BLOCKED_RECONCILE')
                LIMIT 1
                """,
                (target.account_id, target.adset_id, name_key),
            ).fetchone()
            if exact is not None:
                return "name", str(exact["auth_id"])
    return None


def _is_reservation_unique_error(exc: sqlite3.IntegrityError) -> bool:
    message = str(exc).lower()
    return "unique constraint failed" in message and any(
        table in message
        for table in (
            "launch_authorizations",
            "launch_authorization_targets",
            "launch_authorization_ads",
        )
    )


def _append_collision_audit(plan: _NormalizedPlan, reason: str, now: datetime) -> None:
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _insert_audit_locked(
            conn,
            LaunchAuditEvent(
                check_id=plan.check_id,
                event_type="DENIED",
                source=plan.source,
                card_id=plan.card_id,
                actor=plan.actor,
                reason_codes=("DUPLICATE_RESERVED",),
                evidence={"collision_scope": reason},
                created_at=now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _insert_reserved_plan_locked(
    conn: sqlite3.Connection,
    plan: _NormalizedPlan,
    secret_sha256: str,
    now: datetime,
) -> None:
    """Вставляет ordinary authorization и все дочерние строки в caller txn."""
    now_iso = _iso(now)
    expires_iso = _iso(now + RESERVATION_TTL)
    if plan.topic_override:
        _insert_audit_locked(
            conn,
            LaunchAuditEvent(
                check_id=plan.check_id,
                event_type="OVERRIDE_ACCEPTED",
                source=plan.source,
                card_id=plan.card_id,
                actor=plan.actor,
                reason_codes=("TOPIC_OVERRIDE",),
                evidence={"reason": plan.topic_override_reason},
                created_at=now,
            ),
        )
    conn.execute(
        """
        INSERT INTO launch_authorizations (
            auth_id, secret_sha256, card_id, card_name, source, checker_mode,
            campaign_type, account_kind, account_id, plan_sha256, media_sha256,
            phase, topic_override, topic_override_reason, actor,
            recovery_plan_id, created_at, updated_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, 'enforce', ?, ?, ?, ?, ?, 'RESERVED',
                  ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan.auth_id,
            secret_sha256,
            plan.card_id,
            plan.card_name,
            plan.source,
            plan.campaign_type,
            plan.account_kind,
            plan.account_id,
            plan.plan_sha256,
            plan.media_sha256,
            int(plan.topic_override),
            plan.topic_override_reason,
            sanitize_launch_text(plan.actor),
            plan.recovery_plan_id,
            now_iso,
            now_iso,
            expires_iso,
        ),
    )
    for target in plan.targets:
        conn.execute(
            """
            INSERT INTO launch_authorization_targets (
                auth_id, city, ordinal, account_kind, account_id, adset_id,
                identity_key, expected_names_json, expected_ads_count,
                reserved_slots, phase, reservation_expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?, ?)
            """,
            (
                plan.auth_id,
                target.city,
                target.ordinal,
                target.account_kind,
                target.account_id,
                target.adset_id,
                target.identity_key,
                _exact_json_dumps(target.names),
                len(target.names),
                target.reserved_slots,
                expires_iso,
                now_iso,
                now_iso,
            ),
        )
        for name_ordinal, (name, name_key) in enumerate(
            zip(target.names, target.name_keys, strict=True)
        ):
            conn.execute(
                """
                INSERT INTO launch_authorization_ads (
                    auth_id, city, ordinal, ad_name, ad_name_key, account_id,
                    adset_id, phase, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?)
                """,
                (
                    plan.auth_id,
                    target.city,
                    name_ordinal,
                    name,
                    name_key,
                    target.account_id,
                    target.adset_id,
                    now_iso,
                    now_iso,
                ),
            )
    _insert_audit_locked(
        conn,
        LaunchAuditEvent(
            check_id=plan.check_id,
            auth_id=plan.auth_id,
            event_type="RESERVED",
            source=plan.source,
            card_id=plan.card_id,
            actor=plan.actor,
            evidence={
                "target_count": len(plan.targets),
                "reserved_slots": sum(t.reserved_slots for t in plan.targets),
            },
            created_at=now,
        ),
    )


def reserve_authorization(
    plan: object,
    secret_sha256: str,
    now: datetime,
) -> None:
    """Атомарно резервирует authorization, все targets и все expected names."""
    normalized = _normalize_plan(plan)
    secret_sha256 = _sha256(secret_sha256, "secret_sha256")
    now = _aware_utc(now)
    collision_reason: str | None = None
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _transition_expired_locked(conn, now)
        collision = _find_collision(conn, normalized)
        if collision is not None:
            collision_reason = collision[0]
            conn.rollback()
            _append_collision_audit(normalized, collision_reason, now)
            raise LaunchRepositoryBlocked(
                "DUPLICATE_RESERVED",
                ("Карточка или её имена уже зарезервированы другим запуском",),
                normalized.check_id,
            )
        _insert_reserved_plan_locked(conn, normalized, secret_sha256, now)
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if _is_reservation_unique_error(exc):
            _append_collision_audit(normalized, "concurrent_unique_index", now)
            raise LaunchRepositoryBlocked(
                "DUPLICATE_RESERVED",
                ("Конкурентный запуск уже занял reservation",),
                normalized.check_id,
            ) from exc
        raise
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _recovery_json_names(raw: object, field_name: str) -> tuple[str, ...]:
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise LaunchRepositoryBlocked(
            "RECOVERY_PLAN_INVALID",
            (f"{field_name} содержит некорректный JSON",),
            None,
        ) from exc
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise LaunchRepositoryBlocked(
            "RECOVERY_PLAN_INVALID",
            (f"{field_name} должен быть непустым массивом строк",),
            None,
        )
    return tuple(str(item).strip() for item in value)


def _recovery_json_object(raw: object, field_name: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise LaunchRepositoryBlocked(
            "RECOVERY_PLAN_INVALID",
            (f"{field_name} содержит некорректный JSON",),
            None,
        ) from exc
    if not isinstance(value, dict):
        raise LaunchRepositoryBlocked(
            "RECOVERY_PLAN_INVALID",
            (f"{field_name} должен быть JSON-объектом",),
            None,
        )
    return value


def reserve_trusted_recovery_authorization(
    *,
    plan_id: str,
    approved_manifest_sha256: str,
    launch_attempt_key: str,
    card_id: str,
    card_name: str,
    campaign_type: str,
    account_kind: str,
    account_id: str,
    city: str,
    adset_id: str,
    expected_ad_names: tuple[str, ...],
    expected_ad_count: int,
    media_sha256: str,
    actor: str,
    now: datetime,
) -> ProviderLaunchAuthorizationData:
    """Проверяет durable recovery binding и атомарно выдаёт ordinary proof.

    В одной ``BEGIN IMMEDIATE`` проверяются parent/city LAUNCHING rows,
    approval, attempt, exact scope/names и оба media SHA. Legacy plan без
    pre-create canonical binding не может получить proof.
    """
    plan_id = _required_text(plan_id, "plan_id")
    approved_manifest_sha256 = _sha256(
        approved_manifest_sha256, "approved_manifest_sha256"
    )
    launch_attempt_key = _required_text(launch_attempt_key, "launch_attempt_key")
    card_id = _required_text(card_id, "card_id")
    card_name = _required_text(card_name, "card_name")
    campaign_type = _required_text(campaign_type, "campaign_type")
    account_kind = _required_text(account_kind, "account_kind").lower()
    if account_kind not in {"offline", "online"}:
        raise ValueError("account_kind должен быть offline или online")
    account_id = _required_text(account_id, "account_id")
    city = _required_text(city, "city")
    adset_id = _required_text(adset_id, "adset_id")
    if not isinstance(expected_ad_names, tuple):
        raise TypeError("expected_ad_names должен быть tuple")
    names = tuple(_required_text(name, "expected_ad_name") for name in expected_ad_names)
    name_keys = tuple(normalize_launch_name(name) for name in names)
    if (
        not names
        or len(names) != len(set(names))
        or len(name_keys) != len(set(name_keys))
        or type(expected_ad_count) is not int
        or expected_ad_count != len(names)
    ):
        raise ValueError("expected recovery names/count должны быть exact и уникальными")
    media_sha256 = _sha256(media_sha256, "media_sha256")
    actor = _required_text(actor, "actor")
    now = _aware_utc(now)

    auth_id = f"launch-auth-recovery-{uuid.uuid4().hex}"
    check_id = f"recovery-check-{uuid.uuid4().hex}"
    secret = secrets.token_urlsafe(32)
    secret_sha256 = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    target = _NormalizedTarget(
        city=city,
        ordinal=0,
        account_kind=account_kind,
        account_id=account_id,
        adset_id=adset_id,
        identity_key=launch_identity_key(city, card_name),
        names=names,
        name_keys=name_keys,
        reserved_slots=expected_ad_count,
    )
    normalized = _NormalizedPlan(
        auth_id=auth_id,
        check_id=check_id,
        card_id=card_id,
        card_name=card_name,
        source="RECOVERY",
        campaign_type=campaign_type,
        account_kind=account_kind,
        account_id=account_id,
        plan_sha256=_canonical_plan_sha256(
            card_id=card_id,
            card_name=card_name,
            source="RECOVERY",
            campaign_type=campaign_type,
            media_sha256=media_sha256,
            targets=(target,),
        ),
        media_sha256=media_sha256,
        topic_override=False,
        topic_override_reason=None,
        actor=actor,
        recovery_plan_id=plan_id,
        targets=(target,),
    )

    collision_reason: str | None = None
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _transition_expired_locked(conn, now)
        row = conn.execute(
            """
            SELECT
                p.*,
                c.card_id AS parent_card_id,
                c.card_name AS parent_card_name,
                c.campaign_type AS parent_campaign_type,
                c.account_kind AS parent_account_kind,
                c.account_id AS parent_account_id,
                c.phase AS parent_phase,
                c.expected_names_json AS parent_expected_names_json,
                c.target_cities_json AS parent_target_cities_json,
                c.missing_cities_json AS parent_missing_cities_json,
                c.media_manifest_sha256 AS parent_media_manifest_sha256,
                c.approved_by AS parent_approved_by,
                c.approved_at AS parent_approved_at,
                c.last_error AS parent_last_error
            FROM launch_recovery_city_plans AS p
            JOIN launch_recovery_cases AS c ON c.case_id = p.case_id
            WHERE p.plan_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM launch_recovery_cases AS newer
                  WHERE newer.card_id = c.card_id
                    AND newer.trello_action_id <> c.trello_action_id
                    AND newer.source_completed_at >= c.source_completed_at
              )
            """,
            (plan_id,),
        ).fetchone()
        if row is None:
            raise LaunchRepositoryBlocked(
                "RECOVERY_PLAN_NOT_TRUSTED",
                ("Recovery plan отсутствует или больше не является latest",),
                check_id,
            )
        if row["phase"] != "LAUNCHING" or row["parent_phase"] != "LAUNCHING":
            raise LaunchRepositoryBlocked(
                "RECOVERY_PLAN_NOT_LAUNCHING",
                ("Recovery case и city должны быть в LAUNCHING",),
                check_id,
            )
        if (
            row["approved_by"] is None
            or row["approved_at"] is None
            or row["parent_approved_by"] is None
            or row["parent_approved_at"] is None
        ):
            raise LaunchRepositoryBlocked(
                "RECOVERY_PLAN_NOT_APPROVED",
                ("Recovery case и city не имеют durable approval",),
                check_id,
            )
        if row["last_error"] is not None or row["parent_last_error"] is not None:
            raise LaunchRepositoryBlocked(
                "RECOVERY_PLAN_BLOCKED",
                ("Recovery case или city содержит unresolved error",),
                check_id,
            )
        stored_names = _recovery_json_names(
            row["expected_ad_names_json"], "expected_ad_names_json"
        )
        if (
            str(row["launch_attempt_key"] or "") != launch_attempt_key
            or str(row["parent_card_id"]) != card_id
            or str(row["parent_card_name"]) != card_name
            or str(row["parent_campaign_type"] or "") != campaign_type
            or str(row["parent_account_kind"] or "") != account_kind
            or str(row["parent_account_id"] or "") != account_id
            or str(row["city"]) != city
            or str(row["account_kind"]) != account_kind
            or str(row["account_id"]) != account_id
            or str(row["adset_id"]) != adset_id
            or stored_names != names
            or int(row["expected_ad_count"]) != expected_ad_count
        ):
            raise LaunchRepositoryBlocked(
                "RECOVERY_PLAN_DRIFT",
                ("Recovery attempt/scope/names не совпадают с durable plan",),
                check_id,
            )
        if (
            str(row["media_manifest_sha256"] or "")
            != approved_manifest_sha256
            or str(row["parent_media_manifest_sha256"] or "")
            != approved_manifest_sha256
        ):
            raise LaunchRepositoryBlocked(
                "RECOVERY_MANIFEST_DRIFT",
                ("Approved recovery manifest SHA изменился",),
                check_id,
            )
        evidence = _recovery_json_object(row["evidence_json"], "evidence_json")
        binding_sha256 = _optional_text(evidence.get("launch_media_manifest_sha256"))
        if binding_sha256 is None:
            raise LaunchRepositoryBlocked(
                "RECOVERY_LEGACY_MEDIA_BINDING",
                ("Recovery plan не содержит canonical pre-create media SHA",),
                check_id,
            )
        try:
            durable_media_sha256 = _sha256(
                binding_sha256, "launch_media_manifest_sha256"
            )
        except ValueError as exc:
            raise LaunchRepositoryBlocked(
                "RECOVERY_LEGACY_MEDIA_BINDING",
                ("Recovery canonical pre-create media SHA некорректен",),
                check_id,
            ) from exc
        if durable_media_sha256 != media_sha256:
            raise LaunchRepositoryBlocked(
                "RECOVERY_MEDIA_DRIFT",
                ("Actual launch media SHA не совпадает с durable binding",),
                check_id,
            )
        if evidence.get("provider_create_started_at"):
            raise LaunchRepositoryBlocked(
                "RECOVERY_RECONCILE_REQUIRED",
                ("Recovery provider CREATE уже мог начаться",),
                check_id,
            )
        try:
            found_ad_ids = json.loads(str(row["found_ad_ids_json"]))
            parent_names = json.loads(str(row["parent_expected_names_json"]))
            parent_targets = json.loads(str(row["parent_target_cities_json"]))
            parent_missing = json.loads(str(row["parent_missing_cities_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise LaunchRepositoryBlocked(
                "RECOVERY_PLAN_INVALID",
                ("Recovery parent/city JSON повреждён",),
                check_id,
            ) from exc
        if (
            found_ad_ids != []
            or not isinstance(parent_names, dict)
            or parent_names.get(city) != list(names)
            or not isinstance(parent_targets, list)
            or city not in parent_targets
            or not isinstance(parent_missing, list)
            or city not in parent_missing
            or row["last_rechecked_at"] is None
        ):
            raise LaunchRepositoryBlocked(
                "RECOVERY_PLAN_INVALID",
                ("Recovery durable pre-create evidence неполное",),
                check_id,
            )
        old_authorization = conn.execute(
            """
            SELECT phase FROM launch_authorizations
            WHERE recovery_plan_id = ? AND phase <> 'RELEASED'
            LIMIT 1
            """,
            (plan_id,),
        ).fetchone()
        if old_authorization is not None:
            old_phase = str(old_authorization["phase"])
            code = (
                "RECOVERY_RECONCILE_REQUIRED"
                if old_phase in {
                    "CREATE_STARTED",
                    "PARTIAL",
                    "COMPLETED",
                    "BLOCKED_RECONCILE",
                }
                else "DUPLICATE_RESERVED"
            )
            raise LaunchRepositoryBlocked(
                code,
                (f"Recovery plan уже имеет authorization phase={old_phase}",),
                check_id,
            )
        collision = _find_collision(conn, normalized)
        if collision is not None:
            collision_reason = collision[0]
            conn.rollback()
            _append_collision_audit(normalized, collision_reason, now)
            raise LaunchRepositoryBlocked(
                "DUPLICATE_RESERVED",
                ("Recovery scope уже зарезервирован другим запуском",),
                check_id,
            )
        _insert_reserved_plan_locked(conn, normalized, secret_sha256, now)
        conn.commit()
        return ProviderLaunchAuthorizationData(auth_id=auth_id, secret=secret)
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if _is_reservation_unique_error(exc):
            _append_collision_audit(normalized, "concurrent_unique_index", now)
            raise LaunchRepositoryBlocked(
                "DUPLICATE_RESERVED",
                ("Concurrent recovery reservation уже существует",),
                check_id,
            ) from exc
        raise
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def renew_authorization(
    auth_id: str,
    ttl: timedelta,
    now: datetime,
) -> datetime:
    """Продлевает только unclaimed RESERVED targets активной authorization."""
    auth_id = _required_text(auth_id, "auth_id")
    seconds = _ttl_seconds(ttl)
    now = _aware_utc(now)
    now_iso = _iso(now)
    expires = now + timedelta(seconds=seconds)
    expires_iso = _iso(expires)
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _transition_expired_locked(conn, now)
        row = conn.execute(
            "SELECT phase FROM launch_authorizations WHERE auth_id = ?", (auth_id,)
        ).fetchone()
        if row is None:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_NOT_FOUND", ("Authorization не найдена",), None
            )
        phase = str(row["phase"])
        if phase in {"CREATE_STARTED", "BLOCKED_RECONCILE"}:
            raise LaunchRepositoryBlocked(
                "CREATE_RECONCILE_REQUIRED",
                ("Authorization требует exact reconciliation",),
                auth_id,
            )
        if phase not in {"RESERVED", "PARTIAL"}:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_NOT_RENEWABLE",
                (f"Authorization нельзя продлить из phase={phase}",),
                auth_id,
            )
        pending_claim = conn.execute(
            """
            SELECT 1 FROM launch_authorization_ads
            WHERE auth_id = ? AND phase = 'CREATE_STARTED' LIMIT 1
            """,
            (auth_id,),
        ).fetchone()
        if pending_claim is not None:
            raise LaunchRepositoryBlocked(
                "CREATE_RECONCILE_REQUIRED",
                ("Есть CREATE claim без подтверждённого ad_id",),
                auth_id,
            )
        cursor = conn.execute(
            """
            UPDATE launch_authorization_targets
            SET reservation_expires_at = ?, updated_at = ?
            WHERE auth_id = ? AND phase = 'RESERVED'
            """,
            (expires_iso, now_iso, auth_id),
        )
        if cursor.rowcount < 1:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_NOT_RENEWABLE",
                ("Нет незавершённых RESERVED targets",),
                auth_id,
            )
        conn.execute(
            """
            UPDATE launch_authorizations SET expires_at = ?, updated_at = ?
            WHERE auth_id = ?
            """,
            (expires_iso, now_iso, auth_id),
        )
        conn.commit()
        return expires
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _proof_values(proof: ProviderProofLike | object) -> tuple[str, str]:
    return (
        _required_text(_field(proof, "auth_id"), "proof.auth_id"),
        _required_text(_field(proof, "secret"), "proof.secret"),
    )


def renew_authorization_target(
    proof: ProviderProofLike | object,
    city: str,
    adset_id: str,
    ttl: timedelta,
    now: datetime,
) -> TargetRenewalResult:
    """Атомарно продлевает один exact target после внешнего city lock.

    Provider передаёт proof, поэтому один ``auth_id`` не позволяет продлить
    чужую бронь. ``CREATE_STARTED`` поддержан для уже начатого city, но
    ``BLOCKED_RECONCILE`` никогда не оживляется без live reconciliation.
    """
    auth_id, secret = _proof_values(proof)
    city = _required_text(city, "city")
    adset_id = _required_text(adset_id, "adset_id")
    seconds = _ttl_seconds(ttl)
    now = _aware_utc(now)
    now_iso = _iso(now)
    expires = now + timedelta(seconds=seconds)
    expires_iso = _iso(expires)
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # TTL-переход обязан пережить последующий typed deny: иначе rollback
        # оставит expired RESERVED и вечный unique-index конфликт.
        _transition_expired_locked(conn, now)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        auth = conn.execute(
            "SELECT * FROM launch_authorizations WHERE auth_id = ?", (auth_id,)
        ).fetchone()
        if auth is None:
            raise LaunchRepositoryBlocked(
                "INVALID_AUTHORIZATION", ("Authorization не найдена",), None
            )
        expected_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(expected_hash, str(auth["secret_sha256"])):
            raise LaunchRepositoryBlocked(
                "INVALID_AUTHORIZATION",
                ("Authorization secret не прошёл проверку",),
                auth_id,
            )
        authorization_phase = str(auth["phase"])
        if authorization_phase == "BLOCKED_RECONCILE":
            raise LaunchRepositoryBlocked(
                "CREATE_RECONCILE_REQUIRED",
                ("Authorization требует exact reconciliation",),
                auth_id,
            )
        if authorization_phase not in {"RESERVED", "CREATE_STARTED", "PARTIAL"}:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_NOT_RENEWABLE",
                (f"Authorization нельзя продлить из phase={authorization_phase}",),
                auth_id,
            )
        if str(auth["checker_mode"]) != "enforce":
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_MODE_INVALID",
                ("Proof не относится к enforce mode",),
                auth_id,
            )
        target = conn.execute(
            """
            SELECT * FROM launch_authorization_targets
            WHERE auth_id = ? AND city = ? AND adset_id = ?
            """,
            (auth_id, city, adset_id),
        ).fetchone()
        if target is None:
            raise LaunchRepositoryBlocked(
                "PROVIDER_SCOPE_DRIFT",
                ("City/adset не совпадают с authorization",),
                auth_id,
            )
        target_phase = str(target["phase"])
        if target_phase == "BLOCKED_RECONCILE":
            raise LaunchRepositoryBlocked(
                "CREATE_RECONCILE_REQUIRED",
                ("Target требует exact reconciliation",),
                auth_id,
            )
        if target_phase not in {"RESERVED", "CREATE_STARTED", "PARTIAL"}:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_NOT_RENEWABLE",
                (f"Target нельзя продлить из phase={target_phase}",),
                auth_id,
            )
        cursor = conn.execute(
            """
            UPDATE launch_authorization_targets
            SET reservation_expires_at = ?, updated_at = ?
            WHERE auth_id = ? AND city = ? AND adset_id = ?
              AND phase IN ('RESERVED','CREATE_STARTED','PARTIAL')
            """,
            (expires_iso, now_iso, auth_id, city, adset_id),
        )
        if cursor.rowcount != 1:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_NOT_RENEWABLE",
                ("Concurrent target transition обнаружен",),
                auth_id,
            )
        conn.execute(
            """
            UPDATE launch_authorizations
            SET expires_at = CASE WHEN expires_at < ? THEN ? ELSE expires_at END,
                updated_at = ?
            WHERE auth_id = ?
            """,
            (expires_iso, expires_iso, now_iso, auth_id),
        )
        conn.commit()
        return TargetRenewalResult(
            auth_id=auth_id,
            city=city,
            adset_id=adset_id,
            expires_at=expires,
            authorization_phase=authorization_phase,
            target_phase=target_phase,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalize_live_targets(
    live_targets: Sequence[TrustedLiveTarget | object],
) -> tuple[TrustedLiveTarget, ...]:
    if not isinstance(live_targets, Sequence) or isinstance(
        live_targets, (str, bytes)
    ):
        raise TypeError("live_targets должен быть Sequence")
    normalized: list[TrustedLiveTarget] = []
    seen_cities: set[str] = set()
    for raw_target in live_targets:
        city = _required_text(_field(raw_target, "city"), "live_target.city")
        if city in seen_cities:
            raise ValueError(f"Live target города {city} повторяется")
        seen_cities.add(city)
        account_id = _required_text(
            _field(raw_target, "account_id"), "live_target.account_id"
        )
        adset_id = _required_text(
            _field(raw_target, "adset_id"), "live_target.adset_id"
        )
        inventory_complete = _field(raw_target, "inventory_complete")
        if type(inventory_complete) is not bool or not inventory_complete:
            raise LaunchRepositoryBlocked(
                "LIVE_INVENTORY_INCOMPLETE",
                (f"Live inventory города {city} не доказан как полный",),
                None,
            )
        raw_matches = _field(raw_target, "exact_matches")
        if not isinstance(raw_matches, Mapping):
            raise ValueError("live_target.exact_matches должен быть Mapping")
        matches: dict[str, tuple[str, ...]] = {}
        for raw_name, raw_ids in raw_matches.items():
            name = _required_text(raw_name, "live exact ad_name")
            if not isinstance(raw_ids, Sequence) or isinstance(raw_ids, (str, bytes)):
                raise ValueError("Live exact matches должны быть Sequence ad_id")
            ad_ids = tuple(_required_text(ad_id, "live ad_id") for ad_id in raw_ids)
            if len(ad_ids) != len(set(ad_ids)):
                raise ValueError("Live exact matches содержат повторяющийся ad_id")
            matches[name] = ad_ids
        normalized.append(
            TrustedLiveTarget(
                city=city,
                account_id=account_id,
                adset_id=adset_id,
                inventory_complete=True,
                exact_matches=matches,
            )
        )
    if not normalized:
        raise ValueError("live_targets не должен быть пустым")
    return tuple(normalized)


def reconcile_and_rotate_authorization(
    proof: ProviderProofLike | object,
    live_targets: Sequence[TrustedLiveTarget | object],
    now: datetime,
    ttl: timedelta = RESERVATION_TTL,
) -> TrustedReconciliationResult:
    """Сверяет полный live inventory и ротирует proof только для missing names.

    Все targets и все exact names должны присутствовать в evidence. Claimed name
    подтверждается только единственным exact ``ad_id``; отсутствие/дубликат остаются
    неоднозначными. Уже подтверждённые rows не возвращаются в ``RESERVED``.
    """
    auth_id, secret = _proof_values(proof)
    normalized_live = _normalize_live_targets(live_targets)
    seconds = _ttl_seconds(ttl)
    now = _aware_utc(now)
    now_iso = _iso(now)
    expires = now + timedelta(seconds=seconds)
    expires_iso = _iso(expires)
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _transition_expired_locked(conn, now)
        auth = conn.execute(
            "SELECT * FROM launch_authorizations WHERE auth_id = ?", (auth_id,)
        ).fetchone()
        if auth is None:
            raise LaunchRepositoryBlocked(
                "INVALID_AUTHORIZATION", ("Authorization не найдена",), None
            )
        expected_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(expected_hash, str(auth["secret_sha256"])):
            raise LaunchRepositoryBlocked(
                "INVALID_AUTHORIZATION",
                ("Authorization secret не прошёл проверку",),
                auth_id,
            )
        phase = str(auth["phase"])
        if phase not in {
            "RESERVED",
            "CREATE_STARTED",
            "PARTIAL",
            "BLOCKED_RECONCILE",
        }:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_NOT_RECONCILABLE",
                (f"Authorization нельзя reconcile из phase={phase}",),
                auth_id,
            )
        if str(auth["checker_mode"]) != "enforce":
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_MODE_INVALID",
                ("Proof не относится к enforce mode",),
                auth_id,
            )
        target_rows = conn.execute(
            """
            SELECT * FROM launch_authorization_targets
            WHERE auth_id = ? ORDER BY ordinal, city
            """,
            (auth_id,),
        ).fetchall()
        live_by_city = {target.city: target for target in normalized_live}
        if set(live_by_city) != {str(row["city"]) for row in target_rows}:
            raise LaunchRepositoryBlocked(
                "LIVE_RECONCILIATION_DRIFT",
                ("Live evidence не содержит exact набор target-городов",),
                auth_id,
            )

        audit_row = _authorization_audit_row(conn, auth_id)
        missing_by_city: dict[str, tuple[str, ...]] = {}
        created_ids: list[str] = []
        for target_row in target_rows:
            city = str(target_row["city"])
            live_target = live_by_city[city]
            if (
                live_target.account_id != str(target_row["account_id"])
                or live_target.adset_id != str(target_row["adset_id"])
            ):
                raise LaunchRepositoryBlocked(
                    "LIVE_RECONCILIATION_DRIFT",
                    (f"Live account/adset города {city} изменился",),
                    auth_id,
                )
            ad_rows = conn.execute(
                """
                SELECT * FROM launch_authorization_ads
                WHERE auth_id = ? AND city = ? ORDER BY ordinal, ad_name
                """,
                (auth_id, city),
            ).fetchall()
            expected_names = tuple(str(row["ad_name"]) for row in ad_rows)
            if set(live_target.exact_matches) != set(expected_names):
                raise LaunchRepositoryBlocked(
                    "LIVE_RECONCILIATION_DRIFT",
                    (f"Live evidence города {city} не содержит exact набор имён",),
                    auth_id,
                )
            city_missing: list[str] = []
            for ad_row in ad_rows:
                ad_name = str(ad_row["ad_name"])
                matches = tuple(live_target.exact_matches[ad_name])
                ad_phase = str(ad_row["phase"])
                claim_id = _optional_text(ad_row["claim_id"])
                created_ad_id = _optional_text(ad_row["created_ad_id"])
                if ad_phase == "CREATED":
                    if created_ad_id is None or matches != (created_ad_id,):
                        raise LaunchRepositoryBlocked(
                            "LIVE_RECONCILIATION_DRIFT",
                            (f"Подтверждённое объявление {city}/{ad_name} изменилось",),
                            auth_id,
                        )
                    created_ids.append(created_ad_id)
                    continue
                if claim_id is not None:
                    if created_ad_id is not None or ad_phase not in {
                        "CREATE_STARTED",
                        "BLOCKED_RECONCILE",
                    }:
                        raise LaunchRepositoryBlocked(
                            "LIVE_RECONCILIATION_DRIFT",
                            (f"Claimed durable state {city}/{ad_name} повреждён",),
                            auth_id,
                        )
                    if len(matches) != 1:
                        raise LaunchRepositoryBlocked(
                            "LIVE_RECONCILIATION_AMBIGUOUS",
                            (f"Claimed объявление {city}/{ad_name} не имеет одного exact match",),
                            auth_id,
                        )
                    reconciled_ad_id = matches[0]
                    try:
                        cursor = conn.execute(
                            """
                            UPDATE launch_authorization_ads
                            SET phase = 'CREATED', created_ad_id = ?, updated_at = ?
                            WHERE auth_id = ? AND city = ? AND ad_name = ?
                              AND claim_id = ? AND created_ad_id IS NULL
                              AND phase IN ('CREATE_STARTED','BLOCKED_RECONCILE')
                            """,
                            (
                                reconciled_ad_id,
                                now_iso,
                                auth_id,
                                city,
                                ad_name,
                                claim_id,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise LaunchRepositoryBlocked(
                            "LIVE_RECONCILIATION_AMBIGUOUS",
                            ("Live ad_id уже связан с другим provider claim",),
                            auth_id,
                        ) from exc
                    if cursor.rowcount != 1:
                        raise LaunchRepositoryBlocked(
                            "LIVE_RECONCILIATION_DRIFT",
                            (f"Claimed row {city}/{ad_name} изменился конкурентно",),
                            auth_id,
                        )
                    _audit_for_auth_locked(
                        conn,
                        audit_row,
                        "CREATE_CONFIRMED",
                        now,
                        evidence={
                            "claim_id": claim_id,
                            "ad_id": reconciled_ad_id,
                            "city": city,
                            "reconciled_from_live": True,
                        },
                    )
                    created_ids.append(reconciled_ad_id)
                    continue
                if created_ad_id is not None or ad_phase not in {
                    "RESERVED",
                    "BLOCKED_RECONCILE",
                }:
                    raise LaunchRepositoryBlocked(
                        "LIVE_RECONCILIATION_DRIFT",
                        (f"Durable state {city}/{ad_name} неоднозначен",),
                        auth_id,
                    )
                if matches:
                    raise LaunchRepositoryBlocked(
                        "LIVE_RECONCILIATION_AMBIGUOUS",
                        (f"Unclaimed объявление {city}/{ad_name} неожиданно найдено live",),
                        auth_id,
                    )
                city_missing.append(ad_name)

            if city_missing:
                missing_by_city[city] = tuple(city_missing)
                created_in_city = len(ad_rows) - len(city_missing)
                conn.execute(
                    """
                    UPDATE launch_authorization_ads
                    SET phase = 'RESERVED', updated_at = ?
                    WHERE auth_id = ? AND city = ?
                      AND claim_id IS NULL AND created_ad_id IS NULL
                      AND phase IN ('RESERVED','BLOCKED_RECONCILE')
                    """,
                    (now_iso, auth_id, city),
                )
                conn.execute(
                    """
                    UPDATE launch_authorization_targets
                    SET phase = ?, reserved_slots = ?, reservation_expires_at = ?,
                        updated_at = ?
                    WHERE auth_id = ? AND city = ?
                    """,
                    (
                        "PARTIAL" if created_in_city else "RESERVED",
                        len(city_missing),
                        expires_iso,
                        now_iso,
                        auth_id,
                        city,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE launch_authorization_targets
                    SET phase = 'COMPLETED', updated_at = ?
                    WHERE auth_id = ? AND city = ?
                    """,
                    (now_iso, auth_id, city),
                )

        created_ad_ids = tuple(dict.fromkeys(created_ids))
        if not missing_by_city:
            conn.execute(
                """
                UPDATE launch_authorizations
                SET phase = 'COMPLETED', updated_at = ?, finished_at = ?
                WHERE auth_id = ?
                """,
                (now_iso, now_iso, auth_id),
            )
            _audit_for_auth_locked(
                conn,
                audit_row,
                "COMPLETED",
                now,
                evidence={"reconciled_from_live": True},
            )
            conn.commit()
            return TrustedReconciliationResult(
                auth_id=auth_id,
                phase="COMPLETED",
                authorization=None,
                expires_at=None,
                created_ad_ids=created_ad_ids,
                missing_names_by_city={},
            )

        rotated_secret = secrets.token_urlsafe(32)
        rotated_secret_sha256 = hashlib.sha256(
            rotated_secret.encode("utf-8")
        ).hexdigest()
        conn.execute(
            """
            UPDATE launch_authorizations
            SET secret_sha256 = ?, phase = 'PARTIAL', expires_at = ?,
                updated_at = ?, finished_at = NULL
            WHERE auth_id = ?
            """,
            (rotated_secret_sha256, expires_iso, now_iso, auth_id),
        )
        _audit_for_auth_locked(
            conn,
            audit_row,
            "RESERVED",
            now,
            reason_codes=("PROOF_ROTATED_AFTER_RECONCILIATION",),
            evidence={
                "missing_names_by_city": missing_by_city,
                "created_ads": len(created_ad_ids),
            },
        )
        conn.commit()
        return TrustedReconciliationResult(
            auth_id=auth_id,
            phase="PARTIAL",
            authorization=ProviderLaunchAuthorizationData(
                auth_id=auth_id,
                secret=rotated_secret,
            ),
            expires_at=expires,
            created_ad_ids=created_ad_ids,
            missing_names_by_city=missing_by_city,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def validate_authorization_media(
    proof: ProviderProofLike | object,
    actual_media_sha256: str,
    now: datetime,
) -> AuthorizationValidation:
    """Read-only проверяет proof и media до первого provider upload.

    TTL здесь намеренно не меняет durable state: безопасные переходы выполняют
    reservation/claim/reconcile transactions. Истёкший proof всегда блокируется.
    """
    auth_id, secret = _proof_values(proof)
    actual_media_sha256 = _sha256(actual_media_sha256, "actual_media_sha256")
    now = _aware_utc(now)
    conn = _get_connection()
    try:
        auth = conn.execute(
            "SELECT * FROM launch_authorizations WHERE auth_id = ?", (auth_id,)
        ).fetchone()
        if auth is None:
            raise LaunchRepositoryBlocked(
                "INVALID_AUTHORIZATION", ("Authorization не найдена",), None
            )
        expected_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(expected_hash, str(auth["secret_sha256"])):
            raise LaunchRepositoryBlocked(
                "INVALID_AUTHORIZATION",
                ("Authorization secret не прошёл проверку",),
                auth_id,
            )
        phase = str(auth["phase"])
        if phase in {"CREATE_STARTED", "BLOCKED_RECONCILE"}:
            raise LaunchRepositoryBlocked(
                "CREATE_RECONCILE_REQUIRED",
                ("Authorization требует exact reconciliation",),
                auth_id,
            )
        if phase not in {"RESERVED", "PARTIAL"}:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_NOT_OPEN",
                (f"Provider upload запрещён из phase={phase}",),
                auth_id,
            )
        if str(auth["checker_mode"]) != "enforce":
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_MODE_INVALID",
                ("Proof не относится к enforce mode",),
                auth_id,
            )
        if not hmac.compare_digest(str(auth["media_sha256"]), actual_media_sha256):
            raise LaunchRepositoryBlocked(
                "MEDIA_DRIFT",
                ("Media SHA не совпадает с авторизованным планом",),
                auth_id,
            )
        auth_expires_at = _parse_iso(auth["expires_at"])
        expired_target = conn.execute(
            """
            SELECT 1 FROM launch_authorization_targets
            WHERE auth_id = ? AND phase IN ('RESERVED','CREATE_STARTED','PARTIAL')
              AND reservation_expires_at <= ?
            LIMIT 1
            """,
            (auth_id, _iso(now)),
        ).fetchone()
        if auth_expires_at <= now or expired_target is not None:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_EXPIRED", ("Reservation истекла",), auth_id
            )
        target_rows = conn.execute(
            """
            SELECT * FROM launch_authorization_targets
            WHERE auth_id = ? ORDER BY ordinal, city
            """,
            (auth_id,),
        ).fetchall()
        if not target_rows:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_SCOPE_INVALID",
                ("Authorization не содержит target scopes",),
                auth_id,
            )
        targets: list[AuthorizedTargetScope] = []
        for target_row in target_rows:
            city = str(target_row["city"])
            if (
                str(target_row["account_kind"]) != str(auth["account_kind"])
                or str(target_row["account_id"]) != str(auth["account_id"])
            ):
                raise LaunchRepositoryBlocked(
                    "AUTHORIZATION_SCOPE_INVALID",
                    ("Target account не совпадает с authorization",),
                    auth_id,
                )
            ad_rows = conn.execute(
                """
                SELECT ad_name, ad_name_key, account_id, adset_id, phase
                FROM launch_authorization_ads
                WHERE auth_id = ? AND city = ?
                ORDER BY ordinal, ad_name
                """,
                (auth_id, city),
            ).fetchall()
            names = tuple(str(ad_row["ad_name"]) for ad_row in ad_rows)
            name_keys = tuple(str(ad_row["ad_name_key"]) for ad_row in ad_rows)
            pending_slots = sum(
                1
                for ad_row in ad_rows
                if str(ad_row["phase"])
                in {"RESERVED", "CREATE_STARTED", "BLOCKED_RECONCILE"}
            )
            try:
                stored_names = json.loads(str(target_row["expected_names_json"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise LaunchRepositoryBlocked(
                    "AUTHORIZATION_SCOPE_INVALID",
                    ("Target expected names JSON повреждён",),
                    auth_id,
                ) from exc
            if (
                not isinstance(stored_names, list)
                or tuple(stored_names) != names
                or int(target_row["expected_ads_count"]) != len(names)
                or (
                    str(target_row["phase"]) != "COMPLETED"
                    and int(target_row["reserved_slots"]) != pending_slots
                )
                or not names
                or len(names) != len(set(names))
                or len(name_keys) != len(set(name_keys))
                or any(
                    str(ad_row["account_id"]) != str(target_row["account_id"])
                    or str(ad_row["adset_id"]) != str(target_row["adset_id"])
                    for ad_row in ad_rows
                )
                or tuple(normalize_launch_name(name) for name in names) != name_keys
                or str(target_row["identity_key"])
                != launch_identity_key(city, str(auth["card_name"]))
            ):
                raise LaunchRepositoryBlocked(
                    "AUTHORIZATION_SCOPE_INVALID",
                    ("Target scopes не совпадают с авторизованным планом",),
                    auth_id,
                )
            targets.append(
                AuthorizedTargetScope(
                    city=city,
                    ordinal=int(target_row["ordinal"]),
                    account_kind=str(target_row["account_kind"]),
                    account_id=str(target_row["account_id"]),
                    adset_id=str(target_row["adset_id"]),
                    identity_key=str(target_row["identity_key"]),
                    expected_names=names,
                    expected_name_keys=name_keys,
                    reserved_slots=int(target_row["reserved_slots"]),
                    phase=str(target_row["phase"]),
                    reservation_expires_at=_parse_iso(
                        target_row["reservation_expires_at"]
                    ),
                )
            )
        return AuthorizationValidation(
            auth_id=auth_id,
            card_id=str(auth["card_id"]),
            source=str(auth["source"]),
            campaign_type=str(auth["campaign_type"]),
            account_kind=str(auth["account_kind"]),
            account_id=str(auth["account_id"]),
            plan_sha256=_sha256(auth["plan_sha256"], "stored plan_sha256"),
            media_sha256=_sha256(auth["media_sha256"], "stored media_sha256"),
            phase=phase,
            expires_at=auth_expires_at,
            targets=tuple(targets),
        )
    finally:
        conn.close()


def claim_provider_create(
    proof: ProviderProofLike | object,
    scope: ProviderScopeLike | object,
    now: datetime,
    *,
    expected_fingerprint: str | None = None,
    expected_payload: Mapping[str, Any] | None = None,
) -> str:
    """CAS-фиксирует exact name claim до единственного provider ``/ads`` POST."""
    auth_id, secret = _proof_values(proof)
    account_kind = _required_text(_field(scope, "account_kind"), "scope.account_kind").lower()
    account_id = _required_text(_field(scope, "account_id"), "scope.account_id")
    city = _required_text(_field(scope, "city"), "scope.city")
    adset_id = _required_text(_field(scope, "adset_id"), "scope.adset_id")
    ad_name = _required_text(_field(scope, "ad_name"), "scope.ad_name")
    ad_name_key = normalize_launch_name(ad_name)
    media_sha256 = _sha256(_field(scope, "media_sha256"), "scope.media_sha256")
    now = _aware_utc(now)
    now_iso = _iso(now)
    if (expected_fingerprint is None) != (expected_payload is None):
        raise ValueError("Provider fingerprint и payload задаются вместе")
    normalized_fingerprint = (
        _sha256(expected_fingerprint, "expected_fingerprint")
        if expected_fingerprint is not None
        else None
    )
    expected_payload_json = (
        _exact_json_dumps(expected_payload) if expected_payload is not None else None
    )
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS launch_provider_ad_bindings (
                claim_id TEXT PRIMARY KEY, auth_id TEXT NOT NULL, ad_name TEXT NOT NULL,
                adset_id TEXT NOT NULL, expected_fingerprint TEXT NOT NULL,
                expected_payload_json TEXT NOT NULL,
                phase TEXT NOT NULL CHECK (phase IN ('CLAIMED','VERIFIED','AMBIGUOUS')),
                ad_id TEXT UNIQUE, creative_id TEXT, verified_fingerprint TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        _transition_expired_locked(conn, now)
        auth = conn.execute(
            "SELECT * FROM launch_authorizations WHERE auth_id = ?", (auth_id,)
        ).fetchone()
        if auth is None:
            raise LaunchRepositoryBlocked(
                "INVALID_AUTHORIZATION", ("Authorization не найдена",), None
            )
        expected_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(expected_hash, str(auth["secret_sha256"])):
            raise LaunchRepositoryBlocked(
                "INVALID_AUTHORIZATION", ("Authorization secret не прошёл проверку",), auth_id
            )
        phase = str(auth["phase"])
        if phase in {"CREATE_STARTED", "BLOCKED_RECONCILE"}:
            raise LaunchRepositoryBlocked(
                "CREATE_RECONCILE_REQUIRED",
                ("Authorization требует exact reconciliation",),
                auth_id,
            )
        if phase not in {"RESERVED", "PARTIAL"}:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_NOT_OPEN",
                (f"Provider CREATE запрещён из phase={phase}",),
                auth_id,
            )
        if str(auth["checker_mode"]) != "enforce":
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_MODE_INVALID", ("Proof не относится к enforce mode",), auth_id
            )
        if not hmac.compare_digest(str(auth["media_sha256"]), media_sha256):
            raise LaunchRepositoryBlocked(
                "MEDIA_DRIFT", ("Media SHA не совпадает с авторизованным планом",), auth_id
            )
        target = conn.execute(
            """
            SELECT * FROM launch_authorization_targets
            WHERE auth_id = ? AND city = ? AND account_kind = ?
              AND account_id = ? AND adset_id = ?
            """,
            (auth_id, city, account_kind, account_id, adset_id),
        ).fetchone()
        if target is None:
            raise LaunchRepositoryBlocked(
                "PROVIDER_SCOPE_DRIFT",
                ("Account/adset/city не совпадают с authorization",),
                auth_id,
            )
        if _parse_iso(target["reservation_expires_at"]) <= now:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_EXPIRED", ("Reservation истекла",), auth_id
            )
        ad_row = conn.execute(
            """
            SELECT * FROM launch_authorization_ads
            WHERE auth_id = ? AND city = ? AND ad_name = ? AND ad_name_key = ?
              AND account_id = ? AND adset_id = ?
            """,
            (auth_id, city, ad_name, ad_name_key, account_id, adset_id),
        ).fetchone()
        if ad_row is None:
            raise LaunchRepositoryBlocked(
                "PROVIDER_SCOPE_DRIFT", ("Ad name не входит в exact plan",), auth_id
            )
        if str(ad_row["phase"]) != "RESERVED" or ad_row["claim_id"] is not None:
            code = (
                "CREATE_RECONCILE_REQUIRED"
                if str(ad_row["phase"]) in {"CREATE_STARTED", "BLOCKED_RECONCILE"}
                else "PROVIDER_NAME_ALREADY_USED"
            )
            raise LaunchRepositoryBlocked(
                code, ("Expected ad name уже claimed или завершено",), auth_id
            )
        claim_id = f"launch-claim-{uuid.uuid4().hex}"
        cursor = conn.execute(
            """
            UPDATE launch_authorization_ads
            SET phase = 'CREATE_STARTED', claim_id = ?, create_started_at = ?, updated_at = ?
            WHERE auth_id = ? AND city = ? AND ad_name = ?
              AND phase = 'RESERVED' AND claim_id IS NULL
            """,
            (claim_id, now_iso, now_iso, auth_id, city, ad_name),
        )
        if cursor.rowcount != 1:
            raise LaunchRepositoryBlocked(
                "CREATE_RECONCILE_REQUIRED", ("Concurrent CREATE claim обнаружен",), auth_id
            )
        conn.execute(
            """
            UPDATE launch_authorization_targets
            SET phase = 'CREATE_STARTED', updated_at = ?
            WHERE auth_id = ? AND city = ? AND phase = 'RESERVED'
            """,
            (now_iso, auth_id, city),
        )
        conn.execute(
            """
            UPDATE launch_authorizations
            SET phase = CASE WHEN phase = 'RESERVED' THEN 'CREATE_STARTED' ELSE phase END,
                updated_at = ?
            WHERE auth_id = ? AND phase IN ('RESERVED','PARTIAL')
            """,
            (now_iso, auth_id),
        )
        audit_row = _authorization_audit_row(conn, auth_id)
        _audit_for_auth_locked(
            conn,
            audit_row,
            "CREATE_CLAIMED",
            now,
            evidence={"claim_id": claim_id, "city": city, "adset_id": adset_id},
        )
        if normalized_fingerprint is not None and expected_payload_json is not None:
            conn.execute(
                """INSERT INTO launch_provider_ad_bindings
                   (claim_id,auth_id,ad_name,adset_id,expected_fingerprint,
                    expected_payload_json,phase,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,'CLAIMED',?,?)""",
                (
                    claim_id,
                    auth_id,
                    ad_name,
                    adset_id,
                    normalized_fingerprint,
                    expected_payload_json,
                    now_iso,
                    now_iso,
                ),
            )
        conn.commit()
        return claim_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_provider_ad_verified(
    claim_id: str,
    ad_id: str,
    creative_id: str,
    verified_fingerprint: str,
    now: datetime,
) -> None:
    """До success-проекции связывает claim с force-live semantic fingerprint."""

    claim_id = _required_text(claim_id, "claim_id")
    ad_id = _required_text(ad_id, "ad_id")
    creative_id = _required_text(creative_id, "creative_id")
    verified_fingerprint = _sha256(verified_fingerprint, "verified_fingerprint")
    now_iso = _iso(now)
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM launch_provider_ad_bindings WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()
        if row is None:
            raise LaunchRepositoryBlocked(
                "SEMANTIC_BINDING_MISSING",
                ("Provider semantic claim отсутствует",),
                None,
            )
        expected = str(row["expected_fingerprint"])
        if not hmac.compare_digest(expected, verified_fingerprint):
            conn.execute(
                "UPDATE launch_provider_ad_bindings SET phase='AMBIGUOUS', updated_at=? WHERE claim_id=?",
                (now_iso, claim_id),
            )
            conn.commit()
            raise LaunchRepositoryBlocked(
                "PROVIDER_SEMANTIC_DRIFT",
                ("Live creative не совпадает с immutable provider payload",),
                str(row["auth_id"]),
            )
        if str(row["phase"]) == "VERIFIED":
            existing = (
                str(row["ad_id"]),
                str(row["creative_id"]),
                str(row["verified_fingerprint"]),
            )
            if existing == (ad_id, creative_id, verified_fingerprint):
                conn.commit()
                return
            raise LaunchRepositoryBlocked(
                "SEMANTIC_BINDING_CONFLICT",
                ("Provider semantic claim уже связан с другим ad",),
                str(row["auth_id"]),
            )
        updated = conn.execute(
            """UPDATE launch_provider_ad_bindings
               SET phase='VERIFIED',ad_id=?,creative_id=?,verified_fingerprint=?,updated_at=?
               WHERE claim_id=? AND phase='CLAIMED' AND ad_id IS NULL""",
            (ad_id, creative_id, verified_fingerprint, now_iso, claim_id),
        )
        if updated.rowcount != 1:
            raise LaunchRepositoryBlocked(
                "SEMANTIC_BINDING_CONFLICT",
                ("Provider semantic binding CAS не выполнен",),
                str(row["auth_id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sibling_launch_ad_ids(auth_id: str) -> frozenset[str]:
    """ad_id, уже созданные ДРУГИМИ claim'ами того же запуска (одного базового манифеста).

    Гейтвей исполняет запуск по одному объявлению: claim «база:N» резервирует свою авторизацию
    (check_id «gateway-check-<база>:<N>» в launch_check_audit). Объявления соседних claim'ов той же
    карточки законно живут в адсете и не являются «дублем карточки» (без этого второй и третий
    креатив города падали DUPLICATE_LIVE об первый). Любая неясность → пусто, то
    есть строгая проверка как раньше.
    """

    auth_id = _required_text(auth_id, "auth_id")
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT check_id FROM launch_check_audit WHERE auth_id = ? AND check_id LIKE 'gateway-check-%:%' LIMIT 1",
            (auth_id,),
        ).fetchone()
        if row is None:
            return frozenset()
        base = str(row["check_id"]).rsplit(":", 1)[0]
        rows = conn.execute(
            """
            SELECT DISTINCT b.ad_id FROM launch_provider_ad_bindings b
            WHERE b.phase = 'VERIFIED' AND b.ad_id IS NOT NULL AND b.auth_id != ?
              AND b.auth_id IN (
                  SELECT DISTINCT auth_id FROM launch_check_audit
                  WHERE auth_id IS NOT NULL AND check_id LIKE ? ESCAPE '\\'
              )
            """,
            (auth_id, base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%"),
        ).fetchall()
        return frozenset(str(item["ad_id"]) for item in rows)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return frozenset()
        raise
    finally:
        conn.close()


def get_provider_ad_bindings(auth_id: str) -> tuple[dict[str, Any], ...]:
    """Read-only возвращает semantic bindings одной authorization."""

    auth_id = _required_text(auth_id, "auth_id")
    conn = _get_connection()
    try:
        try:
            rows = conn.execute(
                "SELECT * FROM launch_provider_ad_bindings WHERE auth_id=? ORDER BY adset_id,ad_name",
                (auth_id,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return ()
            raise
        return tuple(dict(row) for row in rows)
    finally:
        conn.close()


def authorization_has_create_claims(auth_id: str) -> bool:
    """Read-only: был ли хоть один provider CREATE claim по авторизации.

    claim_id пишется только в ``claim_provider_create`` — ровно перед
    единственным ``/ads`` POST. Пустой результат означает, что провайдер
    объявлений не создавал: исполнитель умер на резервации, валидации медиа
    или загрузке ассетов. Ничего не переводит и TTL не применяет — нужен
    адаптеру, чтобы снять ложный CREATE-маркер auto_launch.
    """

    auth_id = _required_text(auth_id, "auth_id")
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT 1 FROM launch_authorization_ads
            WHERE auth_id = ? AND claim_id IS NOT NULL LIMIT 1
            """,
            (auth_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_provider_create_success(claim_id: str, ad_id: str, now: datetime) -> None:
    """Фиксирует provider ad_id до возврата caller и пересчитывает фазы."""
    claim_id = _required_text(claim_id, "claim_id")
    ad_id = _required_text(ad_id, "ad_id")
    now = _aware_utc(now)
    now_iso = _iso(now)
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM launch_authorization_ads WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if row is None:
            raise LaunchRepositoryBlocked(
                "CLAIM_NOT_FOUND", ("Provider CREATE claim не найден",), None
            )
        auth_id = str(row["auth_id"])
        if str(row["phase"]) == "CREATED":
            if str(row["created_ad_id"]) == ad_id:
                conn.commit()
                return
            raise LaunchRepositoryBlocked(
                "CLAIM_RESULT_DRIFT", ("Claim уже связан с другим ad_id",), auth_id
            )
        if str(row["phase"]) != "CREATE_STARTED" or row["created_ad_id"] is not None:
            raise LaunchRepositoryBlocked(
                "CLAIM_NOT_OPEN", ("Claim не находится в CREATE_STARTED",), auth_id
            )
        try:
            conn.execute(
                """
                UPDATE launch_authorization_ads
                SET phase = 'CREATED', created_ad_id = ?, updated_at = ?
                WHERE claim_id = ? AND phase = 'CREATE_STARTED' AND created_ad_id IS NULL
                """,
                (ad_id, now_iso, claim_id),
            )
        except sqlite3.IntegrityError as exc:
            if "created_ad_id" in str(exc):
                raise LaunchRepositoryBlocked(
                    "AD_ID_ALREADY_RECORDED", ("Provider ad_id уже записан",), auth_id
                ) from exc
            raise
        city = str(row["city"])
        city_counts = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN phase = 'CREATED' THEN 1 ELSE 0 END) AS created,
                   SUM(CASE WHEN phase = 'CREATE_STARTED' THEN 1 ELSE 0 END) AS started
            FROM launch_authorization_ads WHERE auth_id = ? AND city = ?
            """,
            (auth_id, city),
        ).fetchone()
        city_total = int(city_counts["total"] or 0)
        city_created = int(city_counts["created"] or 0)
        city_phase = "COMPLETED" if city_created == city_total else "PARTIAL"
        if city_phase == "COMPLETED":
            conn.execute(
                """
                UPDATE launch_authorization_targets SET phase = ?, updated_at = ?
                WHERE auth_id = ? AND city = ?
                """,
                (city_phase, now_iso, auth_id, city),
            )
        else:
            conn.execute(
                """
                UPDATE launch_authorization_targets
                SET phase = 'PARTIAL', reserved_slots = ?, updated_at = ?
                WHERE auth_id = ? AND city = ?
                """,
                (city_total - city_created, now_iso, auth_id, city),
            )
        all_counts = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN phase = 'CREATED' THEN 1 ELSE 0 END) AS created
            FROM launch_authorization_ads WHERE auth_id = ?
            """,
            (auth_id,),
        ).fetchone()
        complete = int(all_counts["created"] or 0) == int(all_counts["total"])
        conn.execute(
            """
            UPDATE launch_authorizations
            SET phase = ?, updated_at = ?, finished_at = ?
            WHERE auth_id = ?
            """,
            (
                "COMPLETED" if complete else "PARTIAL",
                now_iso,
                now_iso if complete else None,
                auth_id,
            ),
        )
        audit_row = _authorization_audit_row(conn, auth_id)
        _audit_for_auth_locked(
            conn,
            audit_row,
            "CREATE_CONFIRMED",
            now,
            evidence={"claim_id": claim_id, "ad_id": ad_id, "city": city},
        )
        if complete:
            _audit_for_auth_locked(conn, audit_row, "COMPLETED", now)
        else:
            _audit_for_auth_locked(conn, audit_row, "PARTIAL", now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_provider_asset_upload(
    auth_id: str,
    asset_id: str,
    content_sha256: str,
    now: datetime,
) -> dict[str, str] | None:
    """fsync-claim до upload; BOUND replay возвращает сохранённый provider ID."""
    auth_id = _required_text(auth_id, "auth_id")
    asset_id = _required_text(asset_id, "asset_id")
    content_sha256 = _required_text(content_sha256, "content_sha256")
    now_iso = _iso(_aware_utc(now))
    conn = _get_connection()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS launch_provider_asset_bindings (
                auth_id TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                phase TEXT NOT NULL CHECK (phase IN ('UPLOAD_STARTED','BOUND','AMBIGUOUS')),
                provider_payload_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(auth_id, asset_id)
            )"""
        )
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM launch_provider_asset_bindings WHERE auth_id = ? AND asset_id = ?",
            (auth_id, asset_id),
        ).fetchone()
        if row is not None:
            if str(row["content_sha256"]) != content_sha256:
                raise LaunchRepositoryBlocked(
                    "ASSET_BINDING_DRIFT", ("Media binding hash изменён",), auth_id
                )
            if str(row["phase"]) == "BOUND":
                payload = json.loads(str(row["provider_payload_json"]))
                if not isinstance(payload, dict) or not payload:
                    raise LaunchRepositoryBlocked(
                        "ASSET_BINDING_INVALID", ("Media binding повреждён",), auth_id
                    )
                conn.commit()
                return {str(key): str(value) for key, value in payload.items()}
            # UPLOAD_STARTED моложе TTL — заливка, возможно, идёт прямо сейчас
            # в другом прогоне: повтор запрещён, это защита от параллельного
            # двойного upload. Но старше TTL — это сирота: SIGKILL убил процесс
            # посреди заливки (например, при серии рестартов), и раньше такая
            # строка хоронила карточку НАВСЕГДА — auth_id детерминирован от
            # manifest_id, повтор упирался в неё вечно (bug9, п.1). Осиротевшую
            # попытку переоткрываем: лишний недозалитый asset в кабинете
            # безвреден, а карточка получает второй шанс.
            started = str(row["updated_at"])
            if started >= _iso(
                _aware_utc(now) - _ORPHANED_UPLOAD_TTL
            ):
                raise LaunchRepositoryBlocked(
                    "ASSET_UPLOAD_RECONCILE_REQUIRED",
                    ("Media upload уже начат; повтор запрещён",),
                    auth_id,
                )
            conn.execute(
                "UPDATE launch_provider_asset_bindings SET updated_at = ? "
                "WHERE auth_id = ? AND asset_id = ?",
                (now_iso, auth_id, asset_id),
            )
            conn.commit()
            return None
        conn.execute(
            "INSERT INTO launch_provider_asset_bindings"
            "(auth_id,asset_id,content_sha256,phase,provider_payload_json,created_at,updated_at) "
            "VALUES (?,?,?,'UPLOAD_STARTED',NULL,?,?)",
            (auth_id, asset_id, content_sha256, now_iso, now_iso),
        )
        conn.commit()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_provider_asset_upload(
    auth_id: str,
    asset_id: str,
    content_sha256: str,
    provider_payload: Mapping[str, str],
    now: datetime,
) -> None:
    """Связывает immutable asset с provider IDs до первого `/ads` POST."""
    if not provider_payload or any(not str(key) or not str(value) for key, value in provider_payload.items()):
        raise ValueError("provider_payload должен содержать provider IDs")
    payload_json = json.dumps(dict(provider_payload), sort_keys=True, separators=(",", ":"))
    now_iso = _iso(_aware_utc(now))
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE launch_provider_asset_bindings SET phase='BOUND', provider_payload_json=?, updated_at=? "
            "WHERE auth_id=? AND asset_id=? AND content_sha256=? AND phase='UPLOAD_STARTED'",
            (payload_json, now_iso, auth_id, asset_id, content_sha256),
        )
        if cursor.rowcount != 1:
            row = conn.execute(
                "SELECT phase,provider_payload_json FROM launch_provider_asset_bindings "
                "WHERE auth_id=? AND asset_id=? AND content_sha256=?",
                (auth_id, asset_id, content_sha256),
            ).fetchone()
            if row is None or row["phase"] != "BOUND" or row["provider_payload_json"] != payload_json:
                raise LaunchRepositoryBlocked(
                    "ASSET_BINDING_DRIFT", ("Media upload binding не записан",), auth_id
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_authorization(auth_id: str, outcome: str, now: datetime) -> None:
    """Завершает authorization только при доказуемо совместимом состоянии."""
    auth_id = _required_text(auth_id, "auth_id")
    normalized_outcome = _required_text(outcome, "outcome").upper()
    normalized_outcome = {
        "SUCCEEDED": "COMPLETED",
        "FAILED": "BLOCKED",
    }.get(normalized_outcome, normalized_outcome)
    if normalized_outcome not in {
        "COMPLETED",
        "PARTIAL",
        "RELEASED",
        "BLOCKED",
        "BLOCKED_RECONCILE",
    }:
        raise ValueError(f"Неподдерживаемый outcome: {outcome}")
    now = _aware_utc(now)
    now_iso = _iso(now)
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _transition_expired_locked(conn, now)
        auth = conn.execute(
            "SELECT * FROM launch_authorizations WHERE auth_id = ?", (auth_id,)
        ).fetchone()
        if auth is None:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_NOT_FOUND", ("Authorization не найдена",), None
            )
        if str(auth["phase"]) == normalized_outcome:
            conn.commit()
            return
        counts = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN phase = 'CREATED' THEN 1 ELSE 0 END) AS created,
                   SUM(CASE WHEN claim_id IS NOT NULL THEN 1 ELSE 0 END) AS claimed
            FROM launch_authorization_ads WHERE auth_id = ?
            """,
            (auth_id,),
        ).fetchone()
        total = int(counts["total"] or 0)
        created = int(counts["created"] or 0)
        claimed = int(counts["claimed"] or 0)
        if normalized_outcome == "COMPLETED" and (total == 0 or created != total):
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_INCOMPLETE",
                ("Нельзя завершить authorization без всех provider ad_id",),
                auth_id,
            )
        if normalized_outcome == "PARTIAL" and not (0 < created < total):
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_NOT_PARTIAL",
                ("PARTIAL требует хотя бы один created и один missing ad",),
                auth_id,
            )
        if normalized_outcome in {"RELEASED", "BLOCKED"} and claimed:
            raise LaunchRepositoryBlocked(
                "CREATE_RECONCILE_REQUIRED",
                ("Claimed authorization нельзя безопасно освободить",),
                auth_id,
            )
        if normalized_outcome in {"RELEASED", "BLOCKED"}:
            conn.execute(
                """
                UPDATE launch_authorization_ads SET phase = 'RELEASED', updated_at = ?
                WHERE auth_id = ? AND phase = 'RESERVED'
                """,
                (now_iso, auth_id),
            )
            conn.execute(
                """
                UPDATE launch_authorization_targets SET phase = 'RELEASED', updated_at = ?
                WHERE auth_id = ? AND phase = 'RESERVED'
                """,
                (now_iso, auth_id),
            )
        elif normalized_outcome == "BLOCKED_RECONCILE":
            conn.execute(
                """
                UPDATE launch_authorization_ads
                SET phase = 'BLOCKED_RECONCILE', updated_at = ?
                WHERE auth_id = ? AND phase IN ('RESERVED','CREATE_STARTED')
                """,
                (now_iso, auth_id),
            )
            conn.execute(
                """
                UPDATE launch_authorization_targets
                SET phase = 'BLOCKED_RECONCILE', updated_at = ?
                WHERE auth_id = ? AND phase IN ('RESERVED','CREATE_STARTED','PARTIAL')
                """,
                (now_iso, auth_id),
            )
        conn.execute(
            """
            UPDATE launch_authorizations SET phase = ?, updated_at = ?, finished_at = ?
            WHERE auth_id = ?
            """,
            (
                normalized_outcome,
                now_iso,
                now_iso if normalized_outcome in {"COMPLETED", "RELEASED", "BLOCKED"} else None,
                auth_id,
            ),
        )
        audit_row = _authorization_audit_row(conn, auth_id)
        audit_event = {
            "COMPLETED": "COMPLETED",
            "PARTIAL": "PARTIAL",
            "RELEASED": "RELEASED",
            "BLOCKED": "BLOCKED",
            "BLOCKED_RECONCILE": "BLOCKED",
        }[normalized_outcome]
        _audit_for_auth_locked(conn, audit_row, audit_event, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reconcile_authorization(auth_id: str, now: datetime | None = None) -> ReconcileResult:
    """Возвращает durable snapshot; попутно применяет безопасные TTL-переходы."""
    auth_id = _required_text(auth_id, "auth_id")
    now = _aware_utc(now or _utc_now())
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _transition_expired_locked(conn, now)
        auth = conn.execute(
            "SELECT * FROM launch_authorizations WHERE auth_id = ?", (auth_id,)
        ).fetchone()
        if auth is None:
            raise LaunchRepositoryBlocked(
                "AUTHORIZATION_NOT_FOUND", ("Authorization не найдена",), None
            )
        rows = conn.execute(
            """
            SELECT phase, created_ad_id FROM launch_authorization_ads
            WHERE auth_id = ? ORDER BY city, ordinal
            """,
            (auth_id,),
        ).fetchall()
        phase_counts = {
            phase: sum(1 for row in rows if str(row["phase"]) == phase)
            for phase in (
                "RESERVED",
                "CREATE_STARTED",
                "CREATED",
                "RELEASED",
                "BLOCKED_RECONCILE",
            )
        }
        result = ReconcileResult(
            auth_id=auth_id,
            phase=str(auth["phase"]),
            total_ads=len(rows),
            reserved_ads=phase_counts["RESERVED"],
            create_started_ads=phase_counts["CREATE_STARTED"],
            created_ads=phase_counts["CREATED"],
            released_ads=phase_counts["RELEASED"],
            blocked_reconcile_ads=phase_counts["BLOCKED_RECONCILE"],
            created_ad_ids=tuple(
                str(row["created_ad_id"])
                for row in rows
                if row["created_ad_id"] is not None
            ),
            needs_reconcile=(
                str(auth["phase"]) == "BLOCKED_RECONCILE"
                or phase_counts["CREATE_STARTED"] > 0
                or phase_counts["BLOCKED_RECONCILE"] > 0
            ),
            expires_at=_parse_iso(auth["expires_at"]),
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def append_launch_audit(event: LaunchAuditEvent | object) -> None:
    """Добавляет ровно одну sanitized audit-строку; UPDATE/DELETE отсутствуют."""
    if not isinstance(event, LaunchAuditEvent):
        event = LaunchAuditEvent(
            event_id=_required_text(_field(event, "event_id"), "event_id"),
            check_id=_required_text(_field(event, "check_id"), "check_id"),
            auth_id=_optional_text(_field(event, "auth_id")),
            event_type=_enum_text(_field(event, "event_type"), "event_type"),
            source=_enum_text(_field(event, "source"), "source"),
            card_id=_required_text(_field(event, "card_id"), "card_id"),
            actor=_required_text(_field(event, "actor"), "actor"),
            reason_codes=tuple(_field(event, "reason_codes", ()) or ()),
            evidence=_field(event, "evidence", {}) or {},
            created_at=_field(event, "created_at", _utc_now()),
        )
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _insert_audit_locked(conn, event)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class OwnerLaunchRepository:
    """Durable exact-ID lifecycle запуска из migration 022."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        if self._db_path is None:
            return _get_connection()
        connection = sqlite3.connect(
            self._db_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _append_owner_event(
        connection: sqlite3.Connection,
        *,
        proposal_id: str,
        event_type: str,
        actor: str,
        reason_code: str | None,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> None:
        payload_json = _exact_json_dumps(payload)
        sequence = connection.execute(
            """
            SELECT COALESCE(MAX(event_seq), 0) + 1 AS next_seq
            FROM owner_action_events
            WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()
        if sequence is None:
            raise LaunchRepositoryError("Не удалось получить owner event sequence")
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
                int(sequence["next_seq"]),
                event_type,
                actor,
                reason_code,
                payload_json,
                hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                _iso(now),
            ),
        )

    @staticmethod
    def _target_from_row(row: sqlite3.Row) -> LaunchWatchdogTarget:
        return LaunchWatchdogTarget(
            claim_id=str(row["claim_id"]),
            account_id=str(row["account_id"]),
            adset_id=str(row["adset_id"]),
            expected_ad_name=str(row["expected_ad_name"]),
            expected_fingerprint=str(row["expected_fingerprint"]),
            created_ad_id=str(row["created_ad_id"]),
        )

    @staticmethod
    def _scheduler_from_row(
        row: sqlite3.Row,
        *,
        acquired: bool,
    ) -> SchedulerRunLease:
        return SchedulerRunLease(
            run_id=str(row["run_id"]),
            scheduler_name=str(row["scheduler_name"]),
            slot_key=str(row["slot_key"]),
            state=str(row["state"]),
            proposal_id=_optional_text(row["proposal_id"]),
            watchdog_id=_optional_text(row["watchdog_id"]),
            lease_token=_optional_text(row["lease_token"]),
            lease_until=(
                _parse_iso(row["lease_until"])
                if row["lease_until"] is not None
                else None
            ),
            acquired=acquired,
        )

    def claim_scheduler_slot(
        self,
        *,
        scheduler_name: str,
        slot_key: str,
        worker_id: str,
        now: datetime,
    ) -> SchedulerRunLease:
        """Создаёт unique slot или возобновляет его после истечения lease."""

        claimed_at = _aware_utc(now)
        scheduler_name = _required_text(scheduler_name, "scheduler_name")
        slot_key = _required_text(slot_key, "slot_key")
        worker_id = _required_text(worker_id, "worker_id")
        lease_token = f"{worker_id}:{uuid.uuid4()}"
        lease_until = claimed_at + SCHEDULER_LEASE_TTL
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM scheduler_action_runs
                WHERE scheduler_name = ? AND slot_key = ?
                """,
                (scheduler_name, slot_key),
            ).fetchone()
            if row is None:
                run_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO scheduler_action_runs (
                        run_id, scheduler_name, slot_key, state,
                        lease_token, lease_until, created_at, updated_at
                    ) VALUES (?, ?, ?, 'CLAIMED', ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        scheduler_name,
                        slot_key,
                        lease_token,
                        _iso(lease_until),
                        _iso(claimed_at),
                        _iso(claimed_at),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM scheduler_action_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:  # pragma: no cover - INSERT invariant
                    raise LaunchRepositoryError("Scheduler run исчез после INSERT")
                connection.commit()
                return self._scheduler_from_row(row, acquired=True)
            terminal = str(row["state"]) in {"NO_ELIGIBLE", "VERIFIED"}
            lease_busy = (
                row["lease_until"] is not None
                and _parse_iso(row["lease_until"]) > claimed_at
            )
            if terminal or lease_busy:
                connection.commit()
                return self._scheduler_from_row(row, acquired=False)
            updated = connection.execute(
                """
                UPDATE scheduler_action_runs
                SET lease_token = ?, lease_until = ?, updated_at = ?
                WHERE run_id = ?
                  AND state NOT IN ('NO_ELIGIBLE','VERIFIED')
                  AND (lease_until IS NULL OR lease_until <= ?)
                """,
                (
                    lease_token,
                    _iso(lease_until),
                    _iso(claimed_at),
                    row["run_id"],
                    _iso(claimed_at),
                ),
            ).rowcount
            refreshed = connection.execute(
                "SELECT * FROM scheduler_action_runs WHERE run_id = ?",
                (row["run_id"],),
            ).fetchone()
            connection.commit()
            if refreshed is None:  # pragma: no cover - immutable identity
                raise LaunchRepositoryError("Scheduler run исчез")
            return self._scheduler_from_row(
                refreshed,
                acquired=updated == 1,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def attach_scheduler_proposal(
        self,
        lease: SchedulerRunLease,
        *,
        proposal_id: str,
        now: datetime,
    ) -> SchedulerRunLease:
        """Связывает slot с proposal без записи legacy last_run_date."""

        attached_at = _aware_utc(now)
        if not lease.acquired or lease.lease_token is None:
            raise LaunchRepositoryBlocked(
                "SCHEDULER_LEASE_REQUIRED",
                ("Scheduler slot не захвачен этим worker",),
            )
        proposal_id = _required_text(proposal_id, "proposal_id")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE scheduler_action_runs
                SET state = 'PENDING_OWNER', proposal_id = ?,
                    lease_token = NULL, lease_until = NULL,
                    updated_at = ?, last_reason_code = 'OWNER_APPROVAL_REQUIRED'
                WHERE run_id = ? AND state IN ('CLAIMED','PROPOSAL_CREATED')
                  AND lease_token = ? AND lease_until >= ?
                  AND (proposal_id IS NULL OR proposal_id = ?)
                """,
                (
                    proposal_id,
                    _iso(attached_at),
                    lease.run_id,
                    lease.lease_token,
                    _iso(attached_at),
                    proposal_id,
                ),
            ).rowcount
            if updated != 1:
                raise LaunchRepositoryBlocked(
                    "SCHEDULER_LEASE_LOST",
                    ("Scheduler slot изменился конкурентно",),
                )
            row = connection.execute(
                "SELECT * FROM scheduler_action_runs WHERE run_id = ?",
                (lease.run_id,),
            ).fetchone()
            connection.commit()
            if row is None:  # pragma: no cover - immutable identity
                raise LaunchRepositoryError("Scheduler run исчез")
            return self._scheduler_from_row(row, acquired=False)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_scheduler_no_eligible(
        self,
        lease: SchedulerRunLease,
        *,
        now: datetime,
    ) -> SchedulerRunLease:
        """Терминально завершает slot, когда eligible действий нет."""

        completed_at = _aware_utc(now)
        if not lease.acquired or lease.lease_token is None:
            raise LaunchRepositoryBlocked(
                "SCHEDULER_LEASE_REQUIRED",
                ("Scheduler slot не захвачен этим worker",),
            )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE scheduler_action_runs
                SET state = 'NO_ELIGIBLE', lease_token = NULL,
                    lease_until = NULL, updated_at = ?,
                    last_reason_code = 'NO_ELIGIBLE'
                WHERE run_id = ? AND state = 'CLAIMED'
                  AND lease_token = ? AND lease_until >= ?
                """,
                (
                    _iso(completed_at),
                    lease.run_id,
                    lease.lease_token,
                    _iso(completed_at),
                ),
            ).rowcount
            if updated != 1:
                raise LaunchRepositoryBlocked(
                    "SCHEDULER_LEASE_LOST",
                    ("Scheduler slot изменился конкурентно",),
                )
            row = connection.execute(
                "SELECT * FROM scheduler_action_runs WHERE run_id = ?",
                (lease.run_id,),
            ).fetchone()
            connection.commit()
            if row is None:  # pragma: no cover
                raise LaunchRepositoryError("Scheduler run исчез")
            return self._scheduler_from_row(row, acquired=False)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_watchdog(
        self,
        *,
        proposal_id: str,
        decision_id: str,
        job_id: str,
        targets: Sequence[LaunchWatchdogTarget],
        now: datetime,
        scheduler_run_id: str | None = None,
    ) -> str:
        """Связывает exact created IDs только с полностью EXECUTED lineage."""

        created_at = _aware_utc(now)
        proposal_id = _required_text(proposal_id, "proposal_id")
        decision_id = _required_text(decision_id, "decision_id")
        job_id = _required_text(job_id, "job_id")
        normalized_targets = tuple(targets)
        if not normalized_targets:
            raise ValueError("Watchdog должен содержать хотя бы один target")
        if len({target.claim_id for target in normalized_targets}) != len(
            normalized_targets
        ):
            raise ValueError("Watchdog target claim_id должны быть уникальны")
        if len({target.created_ad_id for target in normalized_targets}) != len(
            normalized_targets
        ):
            raise ValueError("Watchdog created_ad_id должны быть уникальны")
        for target in normalized_targets:
            _required_text(target.claim_id, "claim_id")
            _required_text(target.account_id, "account_id")
            _required_text(target.adset_id, "adset_id")
            _required_text(target.expected_ad_name, "expected_ad_name")
            _sha256(target.expected_fingerprint, "expected_fingerprint")
            _required_text(target.created_ad_id, "created_ad_id")

        watchdog_id = str(uuid.uuid4())
        deadline = created_at + WATCHDOG_VERIFY_TTL
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lineage = connection.execute(
                """
                SELECT p.proposal_kind, l.state AS lifecycle_state, l.version,
                       l.active_decision_id, l.active_job_id, j.state AS job_state
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                JOIN owner_execution_jobs j
                  ON j.proposal_id = p.proposal_id
                 AND j.decision_id = ?
                 AND j.job_id = ?
                WHERE p.proposal_id = ?
                """,
                (decision_id, job_id, proposal_id),
            ).fetchone()
            if lineage is None:
                raise LaunchRepositoryBlocked(
                    "WATCHDOG_LINEAGE_MISSING",
                    ("Owner launch lineage не найдена",),
                )
            if (
                str(lineage["proposal_kind"]) != "LAUNCH"
                or str(lineage["lifecycle_state"]) != "EXECUTED"
                or str(lineage["job_state"]) != "EXECUTED"
                or str(lineage["active_decision_id"]) != decision_id
                or str(lineage["active_job_id"]) != job_id
            ):
                raise LaunchRepositoryBlocked(
                    "WATCHDOG_LINEAGE_NOT_EXECUTED",
                    ("Watchdog разрешён только после exact EXECUTED",),
                )
            proposal_targets = connection.execute(
                """
                SELECT claim_id, account_id, adset_id
                FROM owner_action_proposal_targets
                WHERE proposal_id = ?
                ORDER BY ordinal
                """,
                (proposal_id,),
            ).fetchall()
            expected_scope = {
                str(row["claim_id"]): (
                    str(row["account_id"]),
                    str(row["adset_id"] or ""),
                )
                for row in proposal_targets
            }
            supplied_scope = {
                target.claim_id: (target.account_id, target.adset_id)
                for target in normalized_targets
            }
            if supplied_scope != expected_scope:
                raise LaunchRepositoryBlocked(
                    "WATCHDOG_TARGET_SCOPE_MISMATCH",
                    ("Watchdog targets не совпадают с approved proposal",),
                )

            connection.execute(
                """
                INSERT INTO launch_watchdogs (
                    watchdog_id, proposal_id, decision_id, job_id, state,
                    expected_count, verified_count, verification_attempts,
                    next_verify_at, verify_deadline_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'EXECUTED', ?, 0, 0, ?, ?, ?, ?)
                """,
                (
                    watchdog_id,
                    proposal_id,
                    decision_id,
                    job_id,
                    len(normalized_targets),
                    _iso(created_at),
                    _iso(deadline),
                    _iso(created_at),
                    _iso(created_at),
                ),
            )
            connection.executemany(
                """
                INSERT INTO launch_watchdog_targets (
                    proposal_id, watchdog_id, claim_id, account_id, adset_id,
                    expected_ad_name, expected_fingerprint, created_ad_id,
                    expected_configured_status, expected_effective_status,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 'ACTIVE', ?)
                """,
                (
                    (
                        proposal_id,
                        watchdog_id,
                        target.claim_id,
                        target.account_id,
                        target.adset_id,
                        target.expected_ad_name,
                        target.expected_fingerprint,
                        target.created_ad_id,
                        _iso(created_at),
                    )
                    for target in normalized_targets
                ),
            )
            connection.execute(
                """
                UPDATE launch_watchdogs
                SET state = 'VERIFYING', updated_at = ?
                WHERE watchdog_id = ? AND state = 'EXECUTED'
                """,
                (_iso(created_at), watchdog_id),
            )
            job_updated = connection.execute(
                """
                UPDATE owner_execution_jobs
                SET state = 'VERIFYING', next_attempt_at = ?,
                    last_reason_code = 'LAUNCH_VERIFYING', updated_at = ?
                WHERE proposal_id = ? AND decision_id = ? AND job_id = ?
                  AND state = 'EXECUTED'
                """,
                (
                    _iso(created_at),
                    _iso(created_at),
                    proposal_id,
                    decision_id,
                    job_id,
                ),
            ).rowcount
            lifecycle_updated = connection.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = 'VERIFYING', version = version + 1,
                    latest_reason_code = 'LAUNCH_VERIFYING',
                    next_action_at = ?, updated_at = ?
                WHERE proposal_id = ? AND state = 'EXECUTED' AND version = ?
                  AND active_decision_id = ? AND active_job_id = ?
                """,
                (
                    _iso(created_at),
                    _iso(created_at),
                    proposal_id,
                    int(lineage["version"]),
                    decision_id,
                    job_id,
                ),
            ).rowcount
            if job_updated != 1 or lifecycle_updated != 1:
                raise LaunchRepositoryBlocked(
                    "WATCHDOG_START_CAS_CONFLICT",
                    ("Owner launch state изменился конкурентно",),
                )
            if scheduler_run_id is not None:
                scheduler_updated = connection.execute(
                    """
                    UPDATE scheduler_action_runs
                    SET state = 'VERIFYING', watchdog_id = ?,
                        lease_token = NULL, lease_until = NULL,
                        updated_at = ?, last_reason_code = 'LAUNCH_VERIFYING'
                    WHERE run_id = ? AND proposal_id = ?
                      AND state NOT IN ('VERIFIED','FAILED_VISIBLE')
                    """,
                    (
                        watchdog_id,
                        _iso(created_at),
                        _required_text(scheduler_run_id, "scheduler_run_id"),
                        proposal_id,
                    ),
                ).rowcount
                if scheduler_updated != 1:
                    raise LaunchRepositoryBlocked(
                        "SCHEDULER_RUN_BINDING_MISMATCH",
                        ("Scheduler run не совпадает с proposal",),
                    )
            self._append_owner_event(
                connection,
                proposal_id=proposal_id,
                event_type="LAUNCH_VERIFYING",
                actor="launch_watchdog",
                reason_code=None,
                payload={
                    "watchdog_id": watchdog_id,
                    "expected_count": len(normalized_targets),
                    "created_ad_ids": sorted(
                        target.created_ad_id for target in normalized_targets
                    ),
                },
                now=created_at,
            )
            connection.commit()
            return watchdog_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_due(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
    ) -> tuple[LaunchWatchdogLease, ...]:
        """CAS-захватывает due watchdogs; просроченная lease переиспользуется."""

        checked_at = _aware_utc(now)
        worker_id = _required_text(worker_id, "worker_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit должен быть в диапазоне 1..100")
        lease_until = checked_at + WATCHDOG_LEASE_TTL
        connection = self._connect()
        leases: list[LaunchWatchdogLease] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT *
                FROM launch_watchdogs
                WHERE state IN ('VERIFYING','RECONCILE_REQUIRED')
                  AND (next_verify_at IS NULL OR next_verify_at <= ?)
                  AND (lease_until IS NULL OR lease_until <= ?)
                ORDER BY next_verify_at, created_at, watchdog_id
                LIMIT ?
                """,
                (_iso(checked_at), _iso(checked_at), limit),
            ).fetchall()
            for row in rows:
                lease_token = f"{worker_id}:{uuid.uuid4()}"
                updated = connection.execute(
                    """
                    UPDATE launch_watchdogs
                    SET lease_token = ?, lease_until = ?, updated_at = ?
                    WHERE watchdog_id = ?
                      AND state IN ('VERIFYING','RECONCILE_REQUIRED')
                      AND (lease_until IS NULL OR lease_until <= ?)
                    """,
                    (
                        lease_token,
                        _iso(lease_until),
                        _iso(checked_at),
                        row["watchdog_id"],
                        _iso(checked_at),
                    ),
                ).rowcount
                if updated != 1:
                    continue
                target_rows = connection.execute(
                    """
                    SELECT *
                    FROM launch_watchdog_targets
                    WHERE watchdog_id = ?
                    ORDER BY claim_id
                    """,
                    (row["watchdog_id"],),
                ).fetchall()
                leases.append(
                    LaunchWatchdogLease(
                        watchdog_id=str(row["watchdog_id"]),
                        proposal_id=str(row["proposal_id"]),
                        decision_id=str(row["decision_id"]),
                        job_id=str(row["job_id"]),
                        state=str(row["state"]),
                        attempt_no=int(row["verification_attempts"]) + 1,
                        lease_token=lease_token,
                        lease_until=lease_until,
                        verify_deadline_at=_parse_iso(row["verify_deadline_at"]),
                        targets=tuple(
                            self._target_from_row(target_row)
                            for target_row in target_rows
                        ),
                    )
                )
            connection.commit()
            return tuple(leases)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _classify_observations(
        targets: Sequence[LaunchWatchdogTarget],
        observations: Mapping[str, LaunchTargetObservation],
        *,
        fetch_complete: bool,
    ) -> tuple[str, int, str]:
        if not fetch_complete:
            return "UNKNOWN", 0, "LAUNCH_INVENTORY_UNKNOWN"
        verified_count = 0
        pending = False
        for target in targets:
            observed = observations.get(target.created_ad_id)
            if observed is None:
                pending = True
                continue
            if (
                observed.created_ad_id != target.created_ad_id
                or observed.account_id.removeprefix("act_")
                != target.account_id.removeprefix("act_")
                or observed.adset_id != target.adset_id
                or observed.ad_name != target.expected_ad_name
                or not hmac.compare_digest(
                    observed.fingerprint,
                    target.expected_fingerprint,
                )
            ):
                return "FAILED", verified_count, "LAUNCH_EXACT_TARGET_MISMATCH"
            if (
                observed.configured_status == "ACTIVE"
                and observed.effective_status == "ACTIVE"
            ):
                verified_count += 1
                continue
            if observed.effective_status in {"DISAPPROVED", "WITH_ISSUES"}:
                return "FAILED", verified_count, "LAUNCH_PROVIDER_REJECTED"
            pending = True
        if verified_count == len(targets) and not pending:
            return "VERIFIED", verified_count, "LAUNCH_EXACT_ACTIVE"
        return "PENDING", verified_count, "LAUNCH_NOT_EFFECTIVE_ACTIVE"

    @staticmethod
    def _retry_at(attempt_no: int, observed_at: datetime) -> datetime:
        index = min(attempt_no - 1, len(WATCHDOG_RETRY_DELAYS) - 1)
        return observed_at + WATCHDOG_RETRY_DELAYS[index]

    def record_observation(
        self,
        lease: LaunchWatchdogLease,
        *,
        observations: Mapping[str, LaunchTargetObservation],
        fetch_complete: bool,
        now: datetime,
    ) -> LaunchWatchdogTransition:
        """Фиксирует observation и выполняет VERIFIED side effects атомарно."""

        observed_at = _aware_utc(now)
        outcome, verified_count, reason_code = self._classify_observations(
            lease.targets,
            observations,
            fetch_complete=fetch_complete,
        )
        evidence = {
            "fetch_complete": fetch_complete,
            "targets": [
                {
                    "created_ad_id": target.created_ad_id,
                    "expected_account_id": target.account_id,
                    "expected_adset_id": target.adset_id,
                    "expected_ad_name": target.expected_ad_name,
                    "expected_fingerprint": target.expected_fingerprint,
                    "observed": (
                        {
                            "account_id": observed.account_id,
                            "adset_id": observed.adset_id,
                            "ad_name": observed.ad_name,
                            "configured_status": observed.configured_status,
                            "effective_status": observed.effective_status,
                            "fingerprint": observed.fingerprint,
                        }
                        if (
                            observed := observations.get(target.created_ad_id)
                        ) is not None
                        else None
                    ),
                }
                for target in lease.targets
            ],
        }
        evidence_json = _exact_json_dumps(evidence)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT w.*, l.version, l.state AS lifecycle_state,
                       j.state AS job_state
                FROM launch_watchdogs w
                JOIN owner_action_lifecycle l USING (proposal_id)
                JOIN owner_execution_jobs j
                  ON j.proposal_id = w.proposal_id
                 AND j.decision_id = w.decision_id
                 AND j.job_id = w.job_id
                WHERE w.watchdog_id = ?
                """,
                (lease.watchdog_id,),
            ).fetchone()
            if current is None:
                raise LaunchRepositoryBlocked(
                    "WATCHDOG_NOT_FOUND",
                    ("Watchdog не найден",),
                )
            if (
                str(current["lease_token"] or "") != lease.lease_token
                or current["lease_until"] is None
                or _parse_iso(current["lease_until"]) < observed_at
                or int(current["verification_attempts"]) + 1 != lease.attempt_no
                or str(current["state"]) not in {"VERIFYING", "RECONCILE_REQUIRED"}
            ):
                raise LaunchRepositoryBlocked(
                    "WATCHDOG_LEASE_LOST",
                    ("Verification lease истекла или захвачена другим worker",),
                )
            connection.execute(
                """
                INSERT INTO launch_verification_observations (
                    observation_id, watchdog_id, attempt_no, fetch_complete,
                    verified_count, outcome, evidence_json, evidence_sha256,
                    observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    lease.watchdog_id,
                    lease.attempt_no,
                    int(fetch_complete),
                    verified_count,
                    outcome,
                    evidence_json,
                    hashlib.sha256(evidence_json.encode("utf-8")).hexdigest(),
                    _iso(observed_at),
                ),
            )
            next_verify_at: datetime | None = None
            new_state = str(current["state"])
            if outcome == "VERIFIED":
                new_state = "VERIFIED"
                connection.execute(
                    """
                    UPDATE launch_watchdogs
                    SET state = 'VERIFIED', verified_count = ?,
                        verification_attempts = ?, lease_token = NULL,
                        lease_until = NULL, next_verify_at = NULL,
                        verified_at = ?, updated_at = ?,
                        last_reason_code = ?
                    WHERE watchdog_id = ? AND lease_token = ?
                    """,
                    (
                        verified_count,
                        lease.attempt_no,
                        _iso(observed_at),
                        _iso(observed_at),
                        reason_code,
                        lease.watchdog_id,
                        lease.lease_token,
                    ),
                )
                connection.execute(
                    """
                    UPDATE owner_execution_jobs
                    SET state = 'COMPLETE', lease_token = NULL,
                        lease_until = NULL, next_attempt_at = NULL,
                        last_reason_code = ?, updated_at = ?
                    WHERE proposal_id = ? AND decision_id = ? AND job_id = ?
                      AND state IN ('VERIFYING','RECONCILE_REQUIRED')
                    """,
                    (
                        reason_code,
                        _iso(observed_at),
                        lease.proposal_id,
                        lease.decision_id,
                        lease.job_id,
                    ),
                )
                lifecycle_updated = connection.execute(
                    """
                    UPDATE owner_action_lifecycle
                    SET state = 'VERIFIED', version = version + 1,
                        latest_reason_code = ?, next_action_at = NULL,
                        updated_at = ?
                    WHERE proposal_id = ? AND version = ?
                      AND state IN ('VERIFYING','RECONCILE_REQUIRED')
                      AND active_decision_id = ? AND active_job_id = ?
                    """,
                    (
                        reason_code,
                        _iso(observed_at),
                        lease.proposal_id,
                        int(current["version"]),
                        lease.decision_id,
                        lease.job_id,
                    ),
                ).rowcount
                if lifecycle_updated != 1:
                    raise LaunchRepositoryBlocked(
                        "WATCHDOG_VERIFY_CAS_CONFLICT",
                        ("Owner launch lifecycle изменился конкурентно",),
                    )
                connection.execute(
                    """
                    UPDATE scheduler_action_runs
                    SET state = 'VERIFIED', lease_token = NULL,
                        lease_until = NULL, verified_at = ?, updated_at = ?,
                        last_reason_code = ?
                    WHERE proposal_id = ? AND watchdog_id = ?
                      AND state = 'VERIFYING'
                    """,
                    (
                        _iso(observed_at),
                        _iso(observed_at),
                        reason_code,
                        lease.proposal_id,
                        lease.watchdog_id,
                    ),
                )
                outbox_payload = _exact_json_dumps(
                    {
                        "proposal_id": lease.proposal_id,
                        "watchdog_id": lease.watchdog_id,
                        "created_ad_ids": sorted(observations),
                    }
                )
                connection.execute(
                    """
                    INSERT INTO telegram_delivery_outbox (
                        delivery_id, purpose, proposal_id, generation,
                        dedupe_key, rendered_text, rendered_text_sha256,
                        button_spec_json, state, attempts, next_attempt_at,
                        created_at
                    ) VALUES (?, 'TRELLO_COMPLETE', ?, 0, ?, ?, ?, '[]',
                              'PENDING', 0, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        lease.proposal_id,
                        f"trello-complete:{lease.proposal_id}",
                        outbox_payload,
                        hashlib.sha256(outbox_payload.encode("utf-8")).hexdigest(),
                        _iso(observed_at),
                        _iso(observed_at),
                    ),
                )
                self._append_owner_event(
                    connection,
                    proposal_id=lease.proposal_id,
                    event_type="LAUNCH_VERIFIED",
                    actor="launch_watchdog",
                    reason_code=reason_code,
                    payload={
                        "watchdog_id": lease.watchdog_id,
                        "verified_count": verified_count,
                        "verified_date": observed_at.date().isoformat(),
                    },
                    now=observed_at,
                )
            else:
                deadline_reached = observed_at >= lease.verify_deadline_at
                terminal_mismatch = outcome == "FAILED"
                if deadline_reached or terminal_mismatch:
                    new_state = (
                        "FAILED_VERIFICATION"
                        if terminal_mismatch
                        else "RECONCILE_REQUIRED"
                    )
                    terminal_reason = (
                        reason_code
                        if terminal_mismatch
                        else "LAUNCH_VERIFY_DEADLINE"
                    )
                    connection.execute(
                        """
                        UPDATE launch_watchdogs
                        SET state = ?, verified_count = ?,
                            verification_attempts = ?, lease_token = NULL,
                            lease_until = NULL, next_verify_at = ?,
                            updated_at = ?, last_reason_code = ?
                        WHERE watchdog_id = ? AND lease_token = ?
                        """,
                        (
                            new_state,
                            verified_count,
                            lease.attempt_no,
                            (
                                None
                                if terminal_mismatch
                                else _iso(observed_at + timedelta(minutes=30))
                            ),
                            _iso(observed_at),
                            terminal_reason,
                            lease.watchdog_id,
                            lease.lease_token,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE owner_execution_jobs
                        SET state = 'RECONCILE_REQUIRED',
                            next_attempt_at = NULL, last_reason_code = ?,
                            updated_at = ?
                        WHERE proposal_id = ? AND decision_id = ? AND job_id = ?
                          AND state IN ('VERIFYING','RECONCILE_REQUIRED')
                        """,
                        (
                            terminal_reason,
                            _iso(observed_at),
                            lease.proposal_id,
                            lease.decision_id,
                            lease.job_id,
                        ),
                    )
                    if str(current["lifecycle_state"]) == "VERIFYING":
                        connection.execute(
                            """
                            UPDATE owner_action_lifecycle
                            SET state = 'RECONCILE_REQUIRED',
                                version = version + 1,
                                latest_reason_code = ?,
                                next_action_at = NULL, updated_at = ?
                            WHERE proposal_id = ? AND version = ?
                              AND state = 'VERIFYING'
                            """,
                            (
                                terminal_reason,
                                _iso(observed_at),
                                lease.proposal_id,
                                int(current["version"]),
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE scheduler_action_runs
                        SET state = 'FAILED_VISIBLE', lease_token = NULL,
                            lease_until = NULL, updated_at = ?,
                            last_reason_code = ?
                        WHERE proposal_id = ? AND watchdog_id = ?
                          AND state = 'VERIFYING'
                        """,
                        (
                            _iso(observed_at),
                            terminal_reason,
                            lease.proposal_id,
                            lease.watchdog_id,
                        ),
                    )
                    reason_code = terminal_reason
                else:
                    next_verify_at = self._retry_at(
                        lease.attempt_no,
                        observed_at,
                    )
                    connection.execute(
                        """
                        UPDATE launch_watchdogs
                        SET verified_count = ?, verification_attempts = ?,
                            lease_token = NULL, lease_until = NULL,
                            next_verify_at = ?, updated_at = ?,
                            last_reason_code = ?
                        WHERE watchdog_id = ? AND lease_token = ?
                        """,
                        (
                            verified_count,
                            lease.attempt_no,
                            _iso(next_verify_at),
                            _iso(observed_at),
                            reason_code,
                            lease.watchdog_id,
                            lease.lease_token,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE owner_execution_jobs
                        SET next_attempt_at = ?, last_reason_code = ?,
                            updated_at = ?
                        WHERE proposal_id = ? AND decision_id = ? AND job_id = ?
                          AND state IN ('VERIFYING','RECONCILE_REQUIRED')
                        """,
                        (
                            _iso(next_verify_at),
                            reason_code,
                            _iso(observed_at),
                            lease.proposal_id,
                            lease.decision_id,
                            lease.job_id,
                        ),
                    )
            connection.commit()
            return LaunchWatchdogTransition(
                watchdog_id=lease.watchdog_id,
                proposal_id=lease.proposal_id,
                state=new_state,
                outcome=outcome,
                verified_count=verified_count,
                expected_count=len(lease.targets),
                next_verify_at=next_verify_at,
                reason_code=reason_code,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_semantic_fingerprints(
        self,
        created_ad_ids: Sequence[str],
    ) -> dict[str, str]:
        """Возвращает только provider-confirmed semantic fingerprints."""

        normalized_ids = tuple(
            _required_text(ad_id, "created_ad_id") for ad_id in created_ad_ids
        )
        if not normalized_ids:
            return {}
        connection = self._connect()
        try:
            result: dict[str, str] = {}
            for ad_id in normalized_ids:
                row = connection.execute(
                    """
                SELECT ad_id, verified_fingerprint
                FROM launch_provider_ad_bindings
                WHERE phase = 'VERIFIED'
                  AND ad_id = ?
                    """,
                    (ad_id,),
                ).fetchone()
                if (
                    row is not None
                    and row["ad_id"] is not None
                    and row["verified_fingerprint"] is not None
                ):
                    result[str(row["ad_id"])] = str(row["verified_fingerprint"])
            return result
        finally:
            connection.close()


def create_owner_launch_watchdog(
    *,
    proposal_id: str,
    decision_id: str,
    job_id: str,
    targets: Sequence[LaunchWatchdogTarget],
    now: datetime,
    scheduler_run_id: str | None = None,
) -> str:
    """Публичный default-repository вход после owner executor."""

    return OwnerLaunchRepository().create_watchdog(
        proposal_id=proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        targets=targets,
        now=now,
        scheduler_run_id=scheduler_run_id,
    )
