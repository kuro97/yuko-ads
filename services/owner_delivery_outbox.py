"""Durable Telegram outbox для персонального одобрения владельцем."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol

from config import (
    OwnerApprovalConfig,
    load_owner_approval_config,
    validate_owner_approval_config,
)
from services.owner_action_models import (
    OwnerDecisionKind,
    PreparedOwnerRedelivery,
    SystemRedeliveryReason,
    canonical_json,
    canonical_sha256,
)
from services.owner_action_repository import (
    OwnerActionLifecycleConflict,
    OwnerActionRepository,
)


logger = logging.getLogger(__name__)

DELIVERY_LEASE_SECONDS = 60
DELIVERY_RETRY_MINUTES = (1, 2, 5, 15, 30)
POSTPONE_DELAY_MINUTES = 60
# Telegram душит примерно на одном сообщении в секунду в один чат. Боевые точки
# входа ставят эту паузу между отправками; в конструкторе дефолт нулевой, чтобы
# тесты не платили секунду за карточку.
SEND_INTERVAL_SECONDS = 1.0
# Потолок поколений одной карточки. Каждый неоднозначный ответ Telegram отзывает
# кнопки и создаёт поколение N+1; без потолка 429-шторм плодил бесконечные копии
# карточки с мёртвыми кнопками.
MAX_DELIVERY_GENERATIONS = 5
# Сколько ждать, если Telegram не назвал retry_after в ответе 429.
DEFAULT_RATE_LIMIT_SECONDS = 30
_BUTTON_LABELS = {
    OwnerDecisionKind.APPROVE: "Одобрить",
    OwnerDecisionKind.REJECT: "Отклонить",
    OwnerDecisionKind.POSTPONE: "Отложить",
}
_BUTTON_CODES = {
    OwnerDecisionKind.APPROVE: "a",
    OwnerDecisionKind.REJECT: "r",
    OwnerDecisionKind.POSTPONE: "p",
}

# Дневной дайджест (E2): предложения копятся молча и уходят одним пакетом.
_TZ_LOCAL = timezone(timedelta(hours=5))
DIGEST_DEFAULT_HOUR = 9
# Насколько «свежей» считается проверка предложения к часу дайджеста. Позже
# карточка всё равно уходит, но с пометкой «требует свежей проверки»
# (перечитка живого состояния при одобрении есть в execution boundary).
DIGEST_EVIDENCE_FRESH_HOURS = 6
_BATCH_BUTTON_CODES = {
    "APPROVE_ALL": "a",
    "REJECT_ALL": "r",
    "ONE_BY_ONE": "o",
}
_BATCH_KIND_BY_CODE = {code: kind for kind, code in _BATCH_BUTTON_CODES.items()}


class OwnerDeliveryError(RuntimeError):
    """Базовая fail-closed ошибка Telegram delivery."""


class OwnerDeliveryStateConflict(OwnerDeliveryError):
    """Состояние proposal не допускает требуемую delivery-транзакцию."""


class TelegramDeliveryRejected(OwnerDeliveryError):
    """Telegram явно отверг sendMessage, поэтому сообщение не появилось."""


class TelegramRateLimited(TelegramDeliveryRejected):
    """429: сообщение НЕ показано, повтор разрешён без смены поколения.

    Наследуется от ``TelegramDeliveryRejected`` намеренно: 429 — это явный
    отказ, а не неоднозначный результат. Раньше он попадал в общий ``except``
    и уводил карточку в ``FAILED_VISIBLE`` с отзывом кнопок и новым поколением.
    """

    def __init__(self, retry_after: int) -> None:
        super().__init__("TELEGRAM_RATE_LIMITED")
        self.retry_after = max(1, int(retry_after))


class OwnerDeliveryGenerationCap(OwnerDeliveryStateConflict):
    """Поколения карточки исчерпаны — новые копии не создаём."""


class TelegramDeliveryClient(Protocol):
    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]],
    ) -> int:
        """Возвращает положительный Telegram message_id."""


@dataclass(frozen=True, slots=True)
class DeliveryRun:
    claimed_count: int
    sent_count: int
    retry_count: int
    failed_visible_count: int
    replacement_count: int
    trail_sent_count: int = 0
    digest_id: str | None = None
    trail_failed_count: int = 0
    rate_limited_count: int = 0
    rotation_failed_count: int = 0
    generation_capped_count: int = 0


@dataclass(frozen=True, slots=True)
class PreparedDelivery:
    delivery_id: str
    proposal_id: str
    generation: int
    state: str


@dataclass(frozen=True, slots=True)
class DigestGroup:
    """Группа предложений в шапке дайджеста: город × направление × тип."""

    city: str
    language: str
    proposal_kind: str
    count: int


@dataclass(frozen=True, slots=True)
class PreparedDigest:
    digest_id: str
    digest_date: str
    digest_hour: int
    trigger: str
    item_count: int
    stale_count: int
    groups: tuple[DigestGroup, ...]


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} должен содержать timezone")


def _iso(value: datetime) -> str:
    _require_aware(value, "datetime")
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str):
        raise OwnerDeliveryError("В outbox сохранён невалидный datetime")
    parsed = datetime.fromisoformat(value)
    _require_aware(parsed, "datetime")
    return parsed


def _callback_mac(
    *,
    secret: str,
    nonce: str,
    proposal_id: str,
    generation: int,
    decision: OwnerDecisionKind,
) -> str:
    material = (f"v1|{nonce}|{proposal_id}|{generation}|{decision.value}").encode(
        "utf-8"
    )
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


def _mac_prefix(full_mac_sha256: str) -> str:
    encoded = base64.urlsafe_b64encode(bytes.fromhex(full_mac_sha256))
    return encoded.decode("ascii").rstrip("=")[:22]


def build_owner_callback_data(
    *,
    secret: str,
    nonce: str,
    proposal_id: str,
    generation: int,
    decision: OwnerDecisionKind,
) -> str:
    """Строит короткий callback без proposal id и других бизнес-данных."""

    full_mac = _callback_mac(
        secret=secret,
        nonce=nonce,
        proposal_id=proposal_id,
        generation=generation,
        decision=decision,
    )
    return f"oa:{_BUTTON_CODES[decision]}:{nonce}.{_mac_prefix(full_mac)}"


def digest_batch_mac(*, secret: str, nonce: str, digest_id: str, batch_kind: str) -> str:
    """MAC батч-кнопки дайджеста; отдельная область от кнопок предложений."""

    material = f"v1|digest|{nonce}|{digest_id}|{batch_kind}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


def build_digest_callback_data(
    *,
    secret: str,
    nonce: str,
    digest_id: str,
    batch_kind: str,
) -> str:
    """Короткий callback батч-кнопки: префикс od: вместо oa:."""

    if batch_kind not in _BATCH_BUTTON_CODES:
        raise ValueError(f"Неизвестный batch_kind: {batch_kind}")
    full_mac = digest_batch_mac(
        secret=secret,
        nonce=nonce,
        digest_id=digest_id,
        batch_kind=batch_kind,
    )
    return f"od:{_BATCH_BUTTON_CODES[batch_kind]}:{nonce}.{_mac_prefix(full_mac)}"


def load_digest_hour() -> int:
    """Час дайджеста по локальному времени из settings.json (approval.digest_hour).

    Невалидное значение не роняет доставку: возвращаем дефолт. Проверку
    диапазона делает web/settings_validation.py на входе в POST /api/settings.
    """
    try:
        from agent.scheduler import load_settings

        raw = load_settings().get("approval", {})
        value = raw.get("digest_hour") if isinstance(raw, dict) else None
    except Exception:  # noqa: BLE001 — настройки не должны ронять worker
        return DIGEST_DEFAULT_HOUR
    if type(value) is not int or not 0 <= value <= 23:
        return DIGEST_DEFAULT_HOUR
    return value


def next_digest_moment(now: datetime, hour: int) -> datetime:
    """Ближайший момент hour:00 по локальному времени, начиная с ``now`` (в UTC).

    Ровно в hour:00:00 возвращает сам ``now`` — карточки уходят этим же тиком.
    """
    _require_aware(now, "now")
    local = now.astimezone(_TZ_LOCAL)
    boundary = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if boundary < local:
        boundary += timedelta(days=1)
    return boundary.astimezone(timezone.utc)


def latest_proposal_message_id(db_path: str | Path, proposal_id: str) -> int | None:
    """message_id последней отправленной карточки предложения (ветка следа)."""

    connection = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT telegram_message_id
            FROM telegram_delivery_outbox
            WHERE proposal_id = ? AND purpose = 'OWNER_PROPOSAL'
              AND state = 'SENT' AND telegram_message_id IS NOT NULL
            ORDER BY generation DESC
            LIMIT 1
            """,
            (proposal_id,),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else int(row["telegram_message_id"])


def enqueue_trail_message(
    db_path: str | Path,
    *,
    trail_kind: str,
    dedupe_key: str,
    rendered_text: str,
    proposal_id: str | None = None,
    digest_id: str | None = None,
    reply_to_message_id: int | None = None,
    button_spec: list[dict[str, str]] | None = None,
    now: datetime | None = None,
) -> str | None:
    """Ставит в очередь сообщение следа. Повтор dedupe_key возвращает None.

    Единая durable очередь для шапки дайджеста, результата исполнения, вердикта
    верификатора, отчёта батча и подтверждений фидбека (миграция 023).
    """
    created_at = now or datetime.now(timezone.utc)
    _require_aware(created_at, "now")
    if not rendered_text.strip():
        raise ValueError("rendered_text обязателен")
    buttons = button_spec or []
    connection = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    trail_id = str(uuid.uuid4())
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT trail_id FROM owner_trail_messages WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        if existing is not None:
            connection.commit()
            return None
        connection.execute(
            """
            INSERT INTO owner_trail_messages (
                trail_id, trail_kind, proposal_id, digest_id, dedupe_key,
                reply_to_message_id, rendered_text, rendered_text_sha256,
                button_spec_json, state, attempts, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)
            """,
            (
                trail_id,
                trail_kind,
                proposal_id,
                digest_id,
                dedupe_key,
                reply_to_message_id,
                rendered_text,
                hashlib.sha256(rendered_text.encode("utf-8")).hexdigest(),
                canonical_json(buttons).decode("utf-8"),
                _iso(created_at),
            ),
        )
        connection.commit()
        return trail_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _safe_json(response: object) -> object:
    """Тело ответа Telegram, если оно вообще разбирается в JSON."""
    try:
        return response.json()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — 429 без тела остаётся 429
        return None


class RequestsTelegramDeliveryClient:
    """Минимальный HTTPS client; тесты подменяют его протокольным double."""

    def __init__(self, bot_token: str, *, timeout_seconds: int = 10) -> None:
        self._bot_token = bot_token
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def _retry_after(payload: object) -> int:
        """``parameters.retry_after`` из ответа Telegram, иначе дефолт."""
        if isinstance(payload, dict):
            parameters = payload.get("parameters")
            if isinstance(parameters, dict):
                value = parameters.get("retry_after")
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
        return DEFAULT_RATE_LIMIT_SECONDS

    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]],
        reply_to_message_id: int | None = None,
    ) -> int:
        import requests

        body: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": {"inline_keyboard": inline_keyboard},
        }
        if reply_to_message_id is not None:
            body["reply_to_message_id"] = reply_to_message_id
        response = requests.post(
            f"https://api.telegram.org/bot{self._bot_token}/sendMessage",
            json=body,
            timeout=self._timeout_seconds,
        )
        # 429 обязан быть распознан ДО raise_for_status: иначе он приходил как
        # неоднозначная ошибка, карточка уходила в FAILED_VISIBLE и рождалось
        # лишнее поколение с новыми кнопками.
        if response.status_code == 429:
            raise TelegramRateLimited(self._retry_after(_safe_json(response)))
        response.raise_for_status()
        payload = response.json()
        if payload.get("ok") is not True:
            if payload.get("error_code") == 429:
                raise TelegramRateLimited(self._retry_after(payload))
            raise TelegramDeliveryRejected("TELEGRAM_SEND_REJECTED")
        message = payload.get("result")
        message_id = message.get("message_id") if isinstance(message, dict) else None
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
        ):
            raise OwnerDeliveryError("TELEGRAM_MESSAGE_ID_INVALID")
        return message_id


class OwnerDeliveryOutbox:
    """Создаёт поколения, арендует delivery и атомарно привязывает message_id."""

    def __init__(
        self,
        db_path: str | Path,
        settings: OwnerApprovalConfig,
        client: TelegramDeliveryClient,
        *,
        digest_hour: int | None = None,
        send_interval_seconds: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """``digest_hour=None`` — карточка уходит как только готова (транспорт).

        Целое число включает дневной дайджест (E2): первое поколение ждёт
        указанного часа по локальному времени и уходит одним пакетом с шапкой. Боевой worker
        (``deliver_owner_outbox``) передаёт ``load_digest_hour()``.

        ``send_interval_seconds`` — пауза между сообщениями против 429 Telegram.
        Дефолт нулевой (тесты), боевые точки входа ставят ``SEND_INTERVAL_SECONDS``.
        """
        self._db_path = str(db_path)
        self._settings = validate_owner_approval_config(settings)
        self._client = client
        self._repository = OwnerActionRepository(db_path)
        if digest_hour is not None and (
            type(digest_hour) is not int or not 0 <= digest_hour <= 23
        ):
            raise ValueError("digest_hour должен быть int 0..23 или None")
        self._digest_hour = digest_hour
        if send_interval_seconds < 0:
            raise ValueError("send_interval_seconds не может быть отрицательным")
        self._send_interval_seconds = float(send_interval_seconds)
        self._sleep = sleep
        self._sent_in_run = 0

    def _throttle(self) -> None:
        """Пауза перед КАЖДЫМ сообщением кроме первого в прогоне."""
        if self._sent_in_run and self._send_interval_seconds:
            self._sleep(self._send_interval_seconds)
        self._sent_in_run += 1

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        proposal_id: str,
        event_type: str,
        reason_code: str,
        payload: dict[str, object],
        now: datetime,
    ) -> None:
        encoded = canonical_json(payload)
        row = connection.execute(
            """
            SELECT COALESCE(MAX(event_seq), 0) + 1
            FROM owner_action_events
            WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise OwnerDeliveryError("EVENT_SEQUENCE_UNAVAILABLE")
        connection.execute(
            """
            INSERT INTO owner_action_events (
                event_id, proposal_id, event_seq, event_type, actor,
                reason_code, payload_json, payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, 'owner-delivery-worker', ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                proposal_id,
                int(row[0]),
                event_type,
                reason_code,
                encoded.decode("utf-8"),
                hashlib.sha256(encoded).hexdigest(),
                _iso(now),
            ),
        )

    def _create_generation(
        self,
        connection: sqlite3.Connection,
        *,
        proposal: sqlite3.Row,
        rendered_text: str,
        now: datetime,
        next_attempt_at: datetime | None,
        reason_code: str,
    ) -> PreparedDelivery:
        proposal_id = str(proposal["proposal_id"])
        state = str(proposal["state"])
        if state not in {"DELIVERY_PENDING", "POSTPONED"}:
            raise OwnerDeliveryStateConflict(f"DELIVERY_NOT_ALLOWED_FROM_{state}")
        if _parse_iso(proposal["valid_until"]) <= now:
            raise OwnerDeliveryStateConflict("PROPOSAL_EXPIRED")

        generation_row = connection.execute(
            """
            SELECT MAX(generation)
            FROM telegram_delivery_outbox
            WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()
        generation = (
            max(
                int(proposal["delivery_generation"]),
                int(generation_row[0] or 0) if generation_row is not None else 0,
            )
            + 1
        )
        if generation > MAX_DELIVERY_GENERATIONS:
            raise OwnerDeliveryGenerationCap("DELIVERY_GENERATION_CAP")
        delivery_id = str(uuid.uuid4())
        button_spec: list[dict[str, str]] = []
        token_rows: list[tuple[str, str, str, OwnerDecisionKind]] = []
        for decision in OwnerDecisionKind:
            # 12 случайных байт дают ровно 96 бит публичного nonce.
            nonce = (
                base64.urlsafe_b64encode(secrets.token_bytes(12))
                .decode("ascii")
                .rstrip("=")
            )
            full_mac = _callback_mac(
                secret=self._settings.callback_secret,
                nonce=nonce,
                proposal_id=proposal_id,
                generation=generation,
                decision=decision,
            )
            button_spec.append({"decision": decision.value, "nonce": nonce})
            token_rows.append((str(uuid.uuid4()), nonce, full_mac, decision))

        text_sha256 = hashlib.sha256(rendered_text.encode("utf-8")).hexdigest()
        button_json = canonical_json(button_spec).decode("utf-8")
        connection.execute(
            """
            INSERT INTO telegram_delivery_outbox (
                delivery_id, purpose, proposal_id, generation, dedupe_key,
                rendered_text, rendered_text_sha256, button_spec_json,
                button_spec_sha256, state, attempts, next_attempt_at, created_at
            ) VALUES (?, 'OWNER_PROPOSAL', ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?)
            """,
            (
                delivery_id,
                proposal_id,
                generation,
                f"owner-proposal:{proposal_id}:generation:{generation}",
                rendered_text,
                text_sha256,
                button_json,
                canonical_sha256(button_spec),
                _iso(next_attempt_at) if next_attempt_at else None,
                _iso(now),
            ),
        )
        expires_at = _parse_iso(proposal["valid_until"])
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
                    self._settings.owner_user_id,
                    self._settings.chat_id,
                    _iso(now),
                    _iso(expires_at),
                ),
            )

        if state == "POSTPONED":
            updated = connection.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = 'DELIVERY_PENDING', version = version + 1,
                    delivery_generation = ?, latest_reason_code = ?,
                    next_action_at = ?, updated_at = ?
                WHERE proposal_id = ? AND state = 'POSTPONED' AND version = ?
                """,
                (
                    generation,
                    reason_code,
                    _iso(next_attempt_at) if next_attempt_at else None,
                    _iso(now),
                    proposal_id,
                    int(proposal["version"]),
                ),
            ).rowcount
            if updated != 1:
                raise OwnerDeliveryStateConflict("POSTPONE_REDELIVERY_CAS_CONFLICT")
            self._append_event(
                connection,
                proposal_id=proposal_id,
                event_type="DELIVERY_REQUEUED",
                reason_code=reason_code,
                payload={"generation": generation},
                now=now,
            )
        return PreparedDelivery(
            delivery_id=delivery_id,
            proposal_id=proposal_id,
            generation=generation,
            state="PENDING",
        )

    def prepare_owner_delivery(
        self,
        proposal_id: str,
        *,
        rendered_text: str | None = None,
        now: datetime | None = None,
        next_attempt_at: datetime | None = None,
        reason_code: str = "INITIAL_DELIVERY",
    ) -> PreparedDelivery:
        created_at = now or datetime.now(timezone.utc)
        _require_aware(created_at, "now")
        if next_attempt_at is not None:
            _require_aware(next_attempt_at, "next_attempt_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            proposal = connection.execute(
                """
                SELECT p.proposal_id, p.summary, p.valid_until, l.state, l.version,
                       l.delivery_generation
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                WHERE p.proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise OwnerDeliveryStateConflict("PROPOSAL_NOT_FOUND")
            active = connection.execute(
                """
                SELECT delivery_id, proposal_id, generation, state
                FROM telegram_delivery_outbox
                WHERE proposal_id = ? AND state IN ('PENDING','LEASED')
                ORDER BY generation DESC
                LIMIT 1
                """,
                (proposal_id,),
            ).fetchone()
            if active is not None:
                connection.commit()
                return PreparedDelivery(
                    delivery_id=str(active["delivery_id"]),
                    proposal_id=str(active["proposal_id"]),
                    generation=int(active["generation"]),
                    state=str(active["state"]),
                )
            prepared = self._create_generation(
                connection,
                proposal=proposal,
                rendered_text=rendered_text or str(proposal["summary"]),
                now=created_at,
                next_attempt_at=next_attempt_at,
                reason_code=reason_code,
            )
            connection.commit()
            return prepared
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def schedule_postponed_redelivery(
        self,
        proposal_id: str,
        *,
        now: datetime | None = None,
    ) -> PreparedDelivery:
        scheduled_at = now or datetime.now(timezone.utc)
        return self.prepare_owner_delivery(
            proposal_id,
            now=scheduled_at,
            next_attempt_at=scheduled_at + timedelta(minutes=POSTPONE_DELAY_MINUTES),
            reason_code="OWNER_POSTPONED_REDELIVERY",
        )

    def prepare_system_redelivery(
        self,
        proposal_id: str,
        *,
        expected_lifecycle_version: int,
        reason: SystemRedeliveryReason,
        rendered_text: str | None = None,
        actor: str = "owner-delivery-worker",
        now: datetime | None = None,
    ) -> PreparedOwnerRedelivery:
        """Готовит N+1 атомарно через единственную T2R repository-границу."""

        prepared_at = now or datetime.now(timezone.utc)
        _require_aware(prepared_at, "now")
        if rendered_text is None:
            proposal = self._repository.get_proposal(proposal_id)
            if proposal is None:
                raise OwnerDeliveryStateConflict("PROPOSAL_NOT_FOUND")
            rendered_text = proposal.plan.summary
        return self._repository.prepare_system_redelivery(
            proposal_id,
            expected_lifecycle_version=expected_lifecycle_version,
            reason=reason,
            rendered_text=rendered_text,
            owner_user_id=self._settings.owner_user_id,
            chat_id=self._settings.chat_id,
            callback_secret=self._settings.callback_secret,
            actor=actor,
            now=prepared_at,
        )

    def _prepare_callback_secret_rotations(self, *, now: datetime) -> tuple[int, int]:
        """На старте worker заменяет поколения, подписанные прежним secret.

        Возвращает ``(подготовлено, провалено)``. Провал одного предложения
        больше НЕ роняет весь прогон: раньше исключение отсюда летело наружу
        первым же шагом ``deliver()``, и один битый proposal навсегда
        останавливал доставку всей очереди.
        """

        connection = self._connect()
        try:
            proposals = connection.execute(
                """
                SELECT p.proposal_id, p.summary, l.version,
                       l.delivery_generation, d.rendered_text
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                LEFT JOIN telegram_delivery_outbox d
                  ON d.proposal_id = p.proposal_id
                 AND d.generation = l.delivery_generation
                WHERE l.state = 'PENDING_OWNER'
                ORDER BY p.created_at, p.proposal_id
                """
            ).fetchall()
            rotations: list[tuple[str, int, str]] = []
            for proposal in proposals:
                tokens = connection.execute(
                    """
                    SELECT public_nonce, token_mac_sha256, decision_kind
                    FROM owner_callback_tokens
                    WHERE proposal_id = ? AND delivery_generation = ?
                      AND consumed_at IS NULL AND revoked_at IS NULL
                    ORDER BY decision_kind
                    """,
                    (
                        proposal["proposal_id"],
                        proposal["delivery_generation"],
                    ),
                ).fetchall()
                matches_current_secret = len(tokens) == len(OwnerDecisionKind)
                for token in tokens:
                    decision = OwnerDecisionKind(str(token["decision_kind"]))
                    expected_mac = _callback_mac(
                        secret=self._settings.callback_secret,
                        nonce=str(token["public_nonce"]),
                        proposal_id=str(proposal["proposal_id"]),
                        generation=int(proposal["delivery_generation"]),
                        decision=decision,
                    )
                    matches_current_secret = (
                        matches_current_secret
                        and hmac.compare_digest(
                            expected_mac,
                            str(token["token_mac_sha256"]),
                        )
                    )
                if not matches_current_secret:
                    rotations.append(
                        (
                            str(proposal["proposal_id"]),
                            int(proposal["version"]),
                            str(
                                proposal["rendered_text"]
                                if proposal["rendered_text"] is not None
                                else proposal["summary"]
                            ),
                        )
                    )
        finally:
            connection.close()

        prepared_count = 0
        failed_count = 0
        for proposal_id, lifecycle_version, rendered_text in rotations:
            try:
                self.prepare_system_redelivery(
                    proposal_id,
                    expected_lifecycle_version=lifecycle_version,
                    reason=SystemRedeliveryReason.CALLBACK_SECRET_ROTATION,
                    rendered_text=rendered_text,
                    actor="callback-secret-rotation",
                    now=now,
                )
                prepared_count += 1
            except OwnerActionLifecycleConflict:
                # Параллельный worker мог первым подготовить то же поколение.
                current = self._repository.get_proposal(proposal_id)
                if current is None or current.state != "DELIVERY_PENDING":
                    failed_count += 1
                    logger.warning(
                        "Ротация secret: предложение %s пропущено (состояние %s)",
                        proposal_id,
                        None if current is None else current.state,
                    )
            except Exception as exc:  # noqa: BLE001 — один битый не глушит очередь
                failed_count += 1
                logger.warning(
                    "Ротация secret: предложение %s пропущено (%s)",
                    proposal_id,
                    type(exc).__name__,
                )
        return prepared_count, failed_count

    def _recover_expired_leases(self, *, now: datetime) -> int:
        connection = self._connect()
        replacements = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT d.*, p.summary, p.valid_until, l.state, l.version,
                       l.delivery_generation
                FROM telegram_delivery_outbox d
                JOIN owner_action_proposals p USING (proposal_id)
                JOIN owner_action_lifecycle l USING (proposal_id)
                WHERE d.purpose = 'OWNER_PROPOSAL'
                  AND d.state = 'LEASED'
                  AND d.lease_until <= ?
                ORDER BY d.created_at
                """,
                (_iso(now),),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE telegram_delivery_outbox
                    SET state = 'FAILED_VISIBLE', lease_token = NULL,
                        lease_until = NULL, last_error_code = 'UNKNOWN_SEND_RESULT'
                    WHERE delivery_id = ? AND state = 'LEASED'
                    """,
                    (row["delivery_id"],),
                )
                connection.execute(
                    """
                    UPDATE owner_callback_tokens
                    SET revoked_at = ?, revoke_reason = 'UNKNOWN_SEND_RESULT'
                    WHERE delivery_id = ? AND consumed_at IS NULL
                      AND revoked_at IS NULL
                    """,
                    (_iso(now), row["delivery_id"]),
                )
                proposal = connection.execute(
                    """
                    SELECT p.proposal_id, p.summary, p.valid_until, l.state,
                           l.version, l.delivery_generation
                    FROM owner_action_proposals p
                    JOIN owner_action_lifecycle l USING (proposal_id)
                    WHERE p.proposal_id = ?
                    """,
                    (row["proposal_id"],),
                ).fetchone()
                if proposal is None or str(proposal["state"]) != "DELIVERY_PENDING":
                    continue
                if _parse_iso(proposal["valid_until"]) <= now:
                    connection.execute(
                        """
                        UPDATE owner_action_lifecycle
                        SET state = 'EXPIRED', version = version + 1,
                            latest_reason_code = 'PROPOSAL_EXPIRED',
                            next_action_at = NULL, updated_at = ?
                        WHERE proposal_id = ? AND state = 'DELIVERY_PENDING'
                          AND version = ?
                        """,
                        (
                            _iso(now),
                            proposal["proposal_id"],
                            int(proposal["version"]),
                        ),
                    )
                    self._append_event(
                        connection,
                        proposal_id=str(proposal["proposal_id"]),
                        event_type="PROPOSAL_EXPIRED",
                        reason_code="PROPOSAL_EXPIRED",
                        payload={"during": "LEASE_RECOVERY"},
                        now=now,
                    )
                    continue
                try:
                    self._create_generation(
                        connection,
                        proposal=proposal,
                        rendered_text=str(row["rendered_text"]),
                        now=now,
                        next_attempt_at=None,
                        reason_code="UNKNOWN_SEND_RESULT_REDELIVERY",
                    )
                except OwnerDeliveryGenerationCap:
                    logger.warning(
                        "Поколения карточки исчерпаны, замена не создаётся: %s",
                        row["proposal_id"],
                    )
                    continue
                replacements += 1
            connection.commit()
            return replacements
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_initial_deliveries(self, *, now: datetime) -> int:
        """Готовит первое поколение, но НЕ показывает его сразу.

        Карточка ждёт часа дайджеста (E2): ``next_attempt_at`` ставится на
        ближайший ``approval.digest_hour`` по локальному времени, и обычный claim выберет её
        только тогда. Создание proposals при этом не меняется.
        """
        digest_moment = (
            None
            if self._digest_hour is None
            else next_digest_moment(now, self._digest_hour)
        )
        connection = self._connect()
        try:
            proposal_ids = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT l.proposal_id
                    FROM owner_action_lifecycle l
                    JOIN owner_action_proposals p USING (proposal_id)
                    WHERE l.state = 'DELIVERY_PENDING'
                      AND p.valid_until > ?
                      AND NOT EXISTS (
                          SELECT 1 FROM telegram_delivery_outbox d
                          WHERE d.proposal_id = l.proposal_id
                            AND d.state IN ('PENDING','LEASED')
                    )
                    ORDER BY l.updated_at
                    """,
                    (_iso(now),),
                ).fetchall()
            ]
        finally:
            connection.close()
        capped = 0
        for proposal_id in proposal_ids:
            try:
                self.prepare_owner_delivery(
                    proposal_id,
                    now=now,
                    next_attempt_at=digest_moment,
                    reason_code="DIGEST_WAIT" if digest_moment else "INITIAL_DELIVERY",
                )
            except OwnerDeliveryStateConflict as exc:
                # Одно застрявшее предложение не имеет права остановить очередь.
                capped += 1
                logger.warning(
                    "Первичная доставка %s пропущена: %s",
                    proposal_id,
                    exc,
                )
        return capped

    def _due_digest_rows(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime,
    ) -> list[sqlite3.Row]:
        """Предложения, готовые уйти пакетом и ещё не попавшие ни в один дайджест."""

        return connection.execute(
            """
            SELECT d.proposal_id, p.proposal_kind, p.created_at, p.summary,
                   t.city, t.language
            FROM telegram_delivery_outbox d
            JOIN owner_action_proposals p USING (proposal_id)
            JOIN owner_action_lifecycle l USING (proposal_id)
            JOIN owner_action_proposal_targets t
              ON t.proposal_id = d.proposal_id AND t.ordinal = 0
            WHERE d.purpose = 'OWNER_PROPOSAL' AND d.state = 'PENDING'
              AND l.state = 'DELIVERY_PENDING'
              AND p.valid_until > ?
              AND d.next_attempt_at IS NOT NULL
              AND d.next_attempt_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM owner_digest_items i
                  WHERE i.proposal_id = d.proposal_id
              )
            ORDER BY p.created_at, d.proposal_id
            """,
            (_iso(now), _iso(now)),
        ).fetchall()

    @staticmethod
    def _render_digest_header(digest: PreparedDigest) -> str:
        lines = [
            f"📋 Дайджест предложений — {digest.digest_date}",
            f"Всего: {digest.item_count}",
            "",
        ]
        lines.extend(
            f"• {group.city} · {group.language} · {group.proposal_kind} — {group.count}"
            for group in digest.groups
        )
        if digest.stale_count:
            lines.append("")
            lines.append(
                f"⚠️ {digest.stale_count} требуют свежей проверки — "
                "живое состояние всё равно перечитывается при одобрении."
            )
        return "\n".join(lines)

    def _digest_keyboard(
        self,
        *,
        digest_id: str,
        item_count: int,
        nonces: Mapping[str, str],
    ) -> list[list[dict[str, str]]]:
        labels = {
            "APPROVE_ALL": f"✅ Одобрить все ({item_count})",
            "REJECT_ALL": f"❌ Отклонить все ({item_count})",
            "ONE_BY_ONE": "🔎 Решать по одной",
        }
        return [
            [
                {
                    "text": labels[batch_kind],
                    "callback_data": build_digest_callback_data(
                        secret=self._settings.callback_secret,
                        nonce=nonces[batch_kind],
                        digest_id=digest_id,
                        batch_kind=batch_kind,
                    ),
                }
            ]
            for batch_kind in ("APPROVE_ALL", "REJECT_ALL", "ONE_BY_ONE")
        ]

    def collect_digest(
        self,
        *,
        now: datetime,
        trigger: str = "SCHEDULE",
    ) -> PreparedDigest | None:
        """Собирает дневной пакет предложений и ставит шапку с батч-кнопками.

        Дедуп расписания живёт в уникальном индексе ``uq_owner_digest_schedule_slot``:
        за сутки ровно один плановый дайджест. Ручной ``/digest`` (trigger=MANUAL)
        дедупа по дате не имеет — владелец может запросить пакет в любой момент.
        """
        _require_aware(now, "now")
        if trigger not in {"SCHEDULE", "MANUAL"}:
            raise ValueError("trigger должен быть SCHEDULE или MANUAL")
        digest_hour = (
            DIGEST_DEFAULT_HOUR if self._digest_hour is None else self._digest_hour
        )
        digest_date = now.astimezone(_TZ_LOCAL).date().isoformat()
        fresh_before = now - timedelta(hours=DIGEST_EVIDENCE_FRESH_HOURS)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if trigger == "SCHEDULE":
                already = connection.execute(
                    """
                    SELECT digest_id FROM owner_digest_runs
                    WHERE digest_date = ? AND digest_trigger = 'SCHEDULE'
                    """,
                    (digest_date,),
                ).fetchone()
                if already is not None:
                    connection.commit()
                    return None
            rows = self._due_digest_rows(connection, now=now)
            if not rows:
                connection.commit()
                return None

            digest_id = str(uuid.uuid4())
            groups: dict[tuple[str, str, str], int] = {}
            stale_count = 0
            connection.execute(
                """
                INSERT INTO owner_digest_runs (
                    digest_id, digest_date, digest_hour, digest_trigger,
                    item_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    digest_id,
                    digest_date,
                    digest_hour,
                    trigger,
                    len(rows),
                    _iso(now),
                ),
            )
            for ordinal, row in enumerate(rows):
                city = str(row["city"] or "—")
                language = str(row["language"] or "—")
                kind = str(row["proposal_kind"])
                stale = _parse_iso(row["created_at"]) < fresh_before
                stale_count += int(stale)
                group_key = f"{city}|{language}|{kind}"
                groups[(city, language, kind)] = groups.get((city, language, kind), 0) + 1
                connection.execute(
                    """
                    INSERT INTO owner_digest_items (
                        digest_id, proposal_id, ordinal, group_key,
                        evidence_stale, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        digest_id,
                        str(row["proposal_id"]),
                        ordinal,
                        group_key,
                        int(stale),
                        _iso(now),
                    ),
                )
            expires_at = now + timedelta(hours=24)
            nonces: dict[str, str] = {}
            for batch_kind in ("APPROVE_ALL", "REJECT_ALL", "ONE_BY_ONE"):
                nonce = (
                    base64.urlsafe_b64encode(secrets.token_bytes(12))
                    .decode("ascii")
                    .rstrip("=")
                )
                nonces[batch_kind] = nonce
                connection.execute(
                    """
                    INSERT INTO owner_digest_batch_tokens (
                        token_id, public_nonce, token_mac_sha256, digest_id,
                        batch_kind, expected_owner_user_id, expected_chat_id,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        nonce,
                        digest_batch_mac(
                            secret=self._settings.callback_secret,
                            nonce=nonce,
                            digest_id=digest_id,
                            batch_kind=batch_kind,
                        ),
                        digest_id,
                        batch_kind,
                        self._settings.owner_user_id,
                        self._settings.chat_id,
                        _iso(now),
                        _iso(expires_at),
                    ),
                )
            digest = PreparedDigest(
                digest_id=digest_id,
                digest_date=digest_date,
                digest_hour=digest_hour,
                trigger=trigger,
                item_count=len(rows),
                stale_count=stale_count,
                groups=tuple(
                    DigestGroup(
                        city=city,
                        language=language,
                        proposal_kind=kind,
                        count=count,
                    )
                    for (city, language, kind), count in sorted(groups.items())
                ),
            )
            header_buttons = self._digest_keyboard(
                digest_id=digest_id,
                item_count=digest.item_count,
                nonces=nonces,
            )
            rendered = self._render_digest_header(digest)
            connection.execute(
                """
                INSERT INTO owner_trail_messages (
                    trail_id, trail_kind, proposal_id, digest_id, dedupe_key,
                    reply_to_message_id, rendered_text, rendered_text_sha256,
                    button_spec_json, state, attempts, created_at
                ) VALUES (?, 'DIGEST_HEADER', NULL, ?, ?, NULL, ?, ?, ?, 'PENDING', 0, ?)
                """,
                (
                    str(uuid.uuid4()),
                    digest_id,
                    f"digest-header:{digest_id}",
                    rendered,
                    hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
                    canonical_json(header_buttons).decode("utf-8"),
                    _iso(now),
                ),
            )
            connection.commit()
            return digest
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release_pending_for_digest(self, *, now: datetime) -> int:
        """Ручной /digest: снимает ожидание дайджеста со всех готовых карточек.

        Отложенные САМИМ владельцем карточки не трогаем: «Отложить» ставит
        повторную доставку через ``schedule_postponed_redelivery`` (до часа, а
        через фидбек — до нескольких дней) и помечает lifecycle кодом
        ``OWNER_POSTPONED_REDELIVERY``. Раньше ``/digest`` двигал next_attempt_at
        у ВСЕХ PENDING и тем самым отменял решение владельца «не сейчас».
        """

        _require_aware(now, "now")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE telegram_delivery_outbox
                SET next_attempt_at = ?
                WHERE purpose = 'OWNER_PROPOSAL' AND state = 'PENDING'
                  AND next_attempt_at IS NOT NULL AND next_attempt_at > ?
                  AND NOT EXISTS (
                      SELECT 1 FROM owner_action_lifecycle l
                      WHERE l.proposal_id = telegram_delivery_outbox.proposal_id
                        AND l.latest_reason_code = 'OWNER_POSTPONED_REDELIVERY'
                  )
                """,
                (_iso(now), _iso(now)),
            ).rowcount
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _claim_trail(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
    ) -> list[sqlite3.Row]:
        lease_token = f"{worker_id}:{uuid.uuid4()}"
        lease_until = now + timedelta(seconds=DELIVERY_LEASE_SECONDS)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            trail_ids = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT trail_id FROM owner_trail_messages
                    WHERE state = 'PENDING'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY created_at, trail_id
                    LIMIT ?
                    """,
                    (_iso(now), limit),
                ).fetchall()
            ]
            for trail_id in trail_ids:
                connection.execute(
                    """
                    UPDATE owner_trail_messages
                    SET state = 'LEASED', attempts = attempts + 1,
                        lease_token = ?, lease_until = ?
                    WHERE trail_id = ? AND state = 'PENDING'
                    """,
                    (lease_token, _iso(lease_until), trail_id),
                )
            rows = []
            for trail_id in trail_ids:
                row = connection.execute(
                    """
                    SELECT * FROM owner_trail_messages
                    WHERE trail_id = ? AND state = 'LEASED' AND lease_token = ?
                    """,
                    (trail_id, lease_token),
                ).fetchone()
                if row is not None:
                    rows.append(row)
            connection.commit()
            return rows
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _requeue_trail(
        self,
        row: sqlite3.Row,
        *,
        next_attempt: datetime,
        error_code: str,
    ) -> None:
        """Возвращает сообщение следа в очередь вместо терминального провала.

        FAILED_VISIBLE необратим, и на 429 это стоило бы дорого: шапка дайджеста
        никогда бы не привязала батч-токены, а ``uq_owner_digest_item_proposal``
        навсегда запретил бы её предложениям попасть в другой пакет.
        """
        connection = self._connect()
        try:
            connection.execute(
                """
                UPDATE owner_trail_messages
                SET state = 'PENDING', lease_token = NULL, lease_until = NULL,
                    next_attempt_at = ?, last_error_code = ?
                WHERE trail_id = ? AND state = 'LEASED' AND lease_token = ?
                """,
                (
                    _iso(next_attempt),
                    error_code,
                    row["trail_id"],
                    row["lease_token"],
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _finish_trail(
        self,
        row: sqlite3.Row,
        *,
        message_id: int | None,
        now: datetime,
        error_code: str | None,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if message_id is not None:
                connection.execute(
                    """
                    UPDATE owner_trail_messages
                    SET state = 'SENT', telegram_chat_id = ?,
                        telegram_message_id = ?, sent_at = ?, lease_token = NULL,
                        lease_until = NULL, last_error_code = NULL
                    WHERE trail_id = ? AND state = 'LEASED' AND lease_token = ?
                    """,
                    (
                        self._settings.chat_id,
                        message_id,
                        _iso(now),
                        row["trail_id"],
                        row["lease_token"],
                    ),
                )
                if str(row["trail_kind"]) == "DIGEST_HEADER":
                    bound = connection.execute(
                        """
                        UPDATE owner_digest_batch_tokens
                        SET expected_message_id = ?, bound_at = ?
                        WHERE digest_id = ? AND expected_message_id IS NULL
                          AND bound_at IS NULL AND consumed_at IS NULL
                        """,
                        (message_id, _iso(now), row["digest_id"]),
                    ).rowcount
                    if bound != len(_BATCH_BUTTON_CODES):
                        raise OwnerDeliveryStateConflict("DIGEST_TOKEN_BIND_INVALID")
            else:
                connection.execute(
                    """
                    UPDATE owner_trail_messages
                    SET state = 'FAILED_VISIBLE', lease_token = NULL,
                        lease_until = NULL, last_error_code = ?
                    WHERE trail_id = ? AND state = 'LEASED' AND lease_token = ?
                    """,
                    (error_code or "UNKNOWN_SEND_RESULT", row["trail_id"], row["lease_token"]),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _deliver_trail(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
    ) -> tuple[int, int]:
        """Отправляет сообщения следа. Возвращает ``(отправлено, провалено)``.

        Реплай — не обязательное условие видимости. Telegram отвечает ошибкой,
        если сообщение-адресат удалено, слишком старое или чужое, и раньше
        подтверждение владельцу («📋 Собираю дайджест сейчас») просто оседало в
        ``FAILED_VISIBLE``: два из трёх подтверждений в проде так и не дошли.
        Теперь при отказе реплая пробуем обычным сообщением.
        """

        sent = 0
        failed = 0
        for row in self._claim_trail(worker_id=worker_id, now=now, limit=limit):
            keyboard: list[list[dict[str, str]]] = json.loads(
                str(row["button_spec_json"])
            )
            reply_to = row["reply_to_message_id"]
            payload: dict[str, object] = {
                "chat_id": self._settings.chat_id,
                "text": str(row["rendered_text"]),
                "inline_keyboard": keyboard,
            }
            message_id: int | None = None
            last_error: str | None = None
            throttled: TelegramRateLimited | None = None
            attempts: list[dict[str, object]] = [payload]
            if reply_to is not None:
                attempts = [
                    {**payload, "reply_to_message_id": int(reply_to)},
                    payload,
                ]
            for attempt_no, body in enumerate(attempts):
                try:
                    self._throttle()
                    message_id = int(self._client.send_message(**body))  # type: ignore[arg-type]
                    if attempt_no:
                        logger.warning(
                            "След %s ушёл без реплая (адресат %s недоступен)",
                            row["trail_id"],
                            reply_to,
                        )
                    break
                except TelegramRateLimited as exc:
                    throttled = exc
                    break
                except Exception as exc:  # noqa: BLE001 — след не роняет доставку
                    last_error = type(exc).__name__[:60]
            if throttled is not None:
                # 429 не сжигает сообщение: вернём в очередь и остановим прогон.
                self._requeue_trail(
                    row,
                    next_attempt=now + timedelta(seconds=throttled.retry_after),
                    error_code="TELEGRAM_RATE_LIMITED",
                )
                logger.warning(
                    "Telegram троттлит след, пауза %s сек",
                    throttled.retry_after,
                )
                break
            if message_id is not None:
                self._finish_trail(row, message_id=message_id, now=now, error_code=None)
                sent += 1
                continue
            failed += 1
            logger.warning(
                "След %s (%s) не доставлен: %s",
                row["trail_id"],
                row["trail_kind"],
                last_error,
            )
            self._finish_trail(
                row,
                message_id=None,
                now=now,
                error_code=last_error or "UNKNOWN_SEND_RESULT",
            )
        return sent, failed

    def flush_trail(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int = 20,
    ) -> tuple[int, int]:
        """Отправляет уже поставленные в очередь сообщения следа прямо сейчас.

        Нужна подтверждениям владельцу (ack на /digest и на реплай): без неё
        подтверждение лежало в очереди до следующего тика крона.
        """
        _require_aware(now, "now")
        return self._deliver_trail(worker_id=worker_id, now=now, limit=limit)

    def _claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
    ) -> list[sqlite3.Row]:
        if not worker_id.strip():
            raise ValueError("worker_id обязателен")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise ValueError("limit должен быть в диапазоне 1..100")
        lease_token = f"{worker_id}:{uuid.uuid4()}"
        lease_until = now + timedelta(seconds=DELIVERY_LEASE_SECONDS)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            delivery_ids = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT delivery_id
                    FROM telegram_delivery_outbox d
                    JOIN owner_action_proposals p USING (proposal_id)
                    WHERE d.purpose = 'OWNER_PROPOSAL' AND d.state = 'PENDING'
                      AND p.valid_until > ?
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY d.created_at
                    LIMIT ?
                    """,
                    (_iso(now), _iso(now), limit),
                ).fetchall()
            ]
            for delivery_id in delivery_ids:
                connection.execute(
                    """
                    UPDATE telegram_delivery_outbox
                    SET state = 'LEASED', attempts = attempts + 1,
                        lease_token = ?, lease_until = ?
                    WHERE delivery_id = ? AND state = 'PENDING'
                    """,
                    (lease_token, _iso(lease_until), delivery_id),
                )
            rows = []
            for delivery_id in delivery_ids:
                row = connection.execute(
                    """
                    SELECT * FROM telegram_delivery_outbox
                    WHERE delivery_id = ? AND state = 'LEASED'
                      AND lease_token = ?
                    """,
                    (delivery_id, lease_token),
                ).fetchone()
                if row is not None:
                    rows.append(row)
            connection.commit()
            return rows
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _keyboard(self, delivery: sqlite3.Row) -> list[list[dict[str, str]]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT public_nonce, token_mac_sha256, proposal_id,
                       delivery_generation, decision_kind
                FROM owner_callback_tokens
                WHERE delivery_id = ? AND revoked_at IS NULL
                ORDER BY CASE decision_kind
                    WHEN 'APPROVE' THEN 1 WHEN 'REJECT' THEN 2 ELSE 3 END
                """,
                (delivery["delivery_id"],),
            ).fetchall()
        finally:
            connection.close()
        if len(rows) != 3:
            raise OwnerDeliveryStateConflict("DELIVERY_TOKEN_SET_INVALID")
        buttons: list[dict[str, str]] = []
        for row in rows:
            decision = OwnerDecisionKind(str(row["decision_kind"]))
            expected_mac = _callback_mac(
                secret=self._settings.callback_secret,
                nonce=str(row["public_nonce"]),
                proposal_id=str(row["proposal_id"]),
                generation=int(row["delivery_generation"]),
                decision=decision,
            )
            if not hmac.compare_digest(expected_mac, str(row["token_mac_sha256"])):
                raise OwnerDeliveryStateConflict("CALLBACK_SECRET_ROTATED")
            buttons.append(
                {
                    "text": _BUTTON_LABELS[decision],
                    "callback_data": build_owner_callback_data(
                        secret=self._settings.callback_secret,
                        nonce=str(row["public_nonce"]),
                        proposal_id=str(row["proposal_id"]),
                        generation=int(row["delivery_generation"]),
                        decision=decision,
                    ),
                }
            )
        return [buttons]

    def _bind_sent(
        self,
        delivery: sqlite3.Row,
        *,
        message_id: int,
        now: datetime,
    ) -> None:
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
        ):
            raise OwnerDeliveryError("TELEGRAM_MESSAGE_ID_INVALID")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT state, lease_token FROM telegram_delivery_outbox
                WHERE delivery_id = ?
                """,
                (delivery["delivery_id"],),
            ).fetchone()
            if (
                current is None
                or str(current["state"]) != "LEASED"
                or str(current["lease_token"]) != str(delivery["lease_token"])
            ):
                raise OwnerDeliveryStateConflict("DELIVERY_LEASE_LOST")
            token_count = connection.execute(
                """
                UPDATE owner_callback_tokens
                SET expected_message_id = ?, bound_at = ?
                WHERE delivery_id = ? AND expected_message_id IS NULL
                  AND bound_at IS NULL AND consumed_at IS NULL
                  AND revoked_at IS NULL
                """,
                (message_id, _iso(now), delivery["delivery_id"]),
            ).rowcount
            if token_count != 3:
                raise OwnerDeliveryStateConflict("TOKEN_BIND_COUNT_INVALID")
            connection.execute(
                """
                UPDATE telegram_delivery_outbox
                SET state = 'SENT', telegram_chat_id = ?,
                    telegram_message_id = ?, sent_at = ?,
                    lease_token = NULL, lease_until = NULL,
                    next_attempt_at = NULL, last_error_code = NULL
                WHERE delivery_id = ? AND state = 'LEASED'
                  AND lease_token = ?
                """,
                (
                    self._settings.chat_id,
                    message_id,
                    _iso(now),
                    delivery["delivery_id"],
                    delivery["lease_token"],
                ),
            )
            lifecycle = connection.execute(
                """
                SELECT state, version FROM owner_action_lifecycle
                WHERE proposal_id = ?
                """,
                (delivery["proposal_id"],),
            ).fetchone()
            if lifecycle is None or str(lifecycle["state"]) != "DELIVERY_PENDING":
                raise OwnerDeliveryStateConflict("PROPOSAL_NOT_DELIVERY_PENDING")
            updated = connection.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = 'PENDING_OWNER', version = version + 1,
                    delivery_generation = ?, latest_reason_code = 'DELIVERED',
                    next_action_at = NULL, updated_at = ?
                WHERE proposal_id = ? AND state = 'DELIVERY_PENDING'
                  AND version = ?
                """,
                (
                    int(delivery["generation"]),
                    _iso(now),
                    delivery["proposal_id"],
                    int(lifecycle["version"]),
                ),
            ).rowcount
            if updated != 1:
                raise OwnerDeliveryStateConflict("DELIVERY_LIFECYCLE_CAS_CONFLICT")
            self._append_event(
                connection,
                proposal_id=str(delivery["proposal_id"]),
                event_type="OWNER_MESSAGE_DELIVERED",
                reason_code="DELIVERED",
                payload={
                    "delivery_id": str(delivery["delivery_id"]),
                    "generation": int(delivery["generation"]),
                    "message_id": message_id,
                },
                now=now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _requeue(
        self,
        delivery: sqlite3.Row,
        *,
        next_attempt: datetime,
        error_code: str,
    ) -> None:
        """Возврат карточки в PENDING без смены поколения и отзыва кнопок."""
        connection = self._connect()
        try:
            connection.execute(
                """
                UPDATE telegram_delivery_outbox
                SET state = 'PENDING', lease_token = NULL, lease_until = NULL,
                    next_attempt_at = ?, last_error_code = ?
                WHERE delivery_id = ? AND state = 'LEASED' AND lease_token = ?
                """,
                (
                    _iso(next_attempt),
                    error_code,
                    delivery["delivery_id"],
                    delivery["lease_token"],
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _retry_rejected(
        self,
        delivery: sqlite3.Row,
        *,
        now: datetime,
    ) -> None:
        attempt = max(1, int(delivery["attempts"]))
        delay_index = min(attempt - 1, len(DELIVERY_RETRY_MINUTES) - 1)
        self._requeue(
            delivery,
            next_attempt=now + timedelta(minutes=DELIVERY_RETRY_MINUTES[delay_index]),
            error_code="TELEGRAM_REJECTED",
        )

    def _defer_rate_limited(
        self,
        delivery: sqlite3.Row,
        *,
        now: datetime,
        retry_after: int,
    ) -> None:
        """429: ждём столько, сколько назвал Telegram. Кнопки остаются живыми."""
        self._requeue(
            delivery,
            next_attempt=now + timedelta(seconds=retry_after),
            error_code="TELEGRAM_RATE_LIMITED",
        )

    def _replace_unknown(
        self,
        delivery: sqlite3.Row,
        *,
        now: datetime,
        error_code: str,
    ) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE telegram_delivery_outbox
                SET state = 'FAILED_VISIBLE', lease_token = NULL,
                    lease_until = NULL, last_error_code = ?
                WHERE delivery_id = ? AND state = 'LEASED' AND lease_token = ?
                """,
                (
                    error_code,
                    delivery["delivery_id"],
                    delivery["lease_token"],
                ),
            ).rowcount
            if changed != 1:
                connection.commit()
                return False
            connection.execute(
                """
                UPDATE owner_callback_tokens
                SET revoked_at = ?, revoke_reason = ?
                WHERE delivery_id = ? AND consumed_at IS NULL
                  AND revoked_at IS NULL
                """,
                (_iso(now), error_code, delivery["delivery_id"]),
            )
            proposal = connection.execute(
                """
                SELECT p.proposal_id, p.summary, p.valid_until, l.state,
                       l.version, l.delivery_generation
                FROM owner_action_proposals p
                JOIN owner_action_lifecycle l USING (proposal_id)
                WHERE p.proposal_id = ?
                """,
                (delivery["proposal_id"],),
            ).fetchone()
            if proposal is not None and str(proposal["state"]) == "DELIVERY_PENDING":
                if _parse_iso(proposal["valid_until"]) <= now:
                    connection.execute(
                        """
                        UPDATE owner_action_lifecycle
                        SET state = 'EXPIRED', version = version + 1,
                            latest_reason_code = 'PROPOSAL_EXPIRED',
                            next_action_at = NULL, updated_at = ?
                        WHERE proposal_id = ? AND state = 'DELIVERY_PENDING'
                          AND version = ?
                        """,
                        (
                            _iso(now),
                            proposal["proposal_id"],
                            int(proposal["version"]),
                        ),
                    )
                    self._append_event(
                        connection,
                        proposal_id=str(proposal["proposal_id"]),
                        event_type="PROPOSAL_EXPIRED",
                        reason_code="PROPOSAL_EXPIRED",
                        payload={"during": "UNKNOWN_SEND_RESULT"},
                        now=now,
                    )
                else:
                    try:
                        self._create_generation(
                            connection,
                            proposal=proposal,
                            rendered_text=str(delivery["rendered_text"]),
                            now=now,
                            next_attempt_at=None,
                            reason_code=error_code,
                        )
                    except OwnerDeliveryGenerationCap:
                        # Потолок поколений: копия с новыми кнопками не создаётся,
                        # карточка остаётся FAILED_VISIBLE и видна в дашборде.
                        logger.warning(
                            "Поколения карточки исчерпаны: %s",
                            delivery["proposal_id"],
                        )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def deliver(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        limit: int = 20,
    ) -> DeliveryRun:
        started_at = now or datetime.now(timezone.utc)
        _require_aware(started_at, "now")
        self._sent_in_run = 0
        replacements, rotation_failed = self._prepare_callback_secret_rotations(
            now=started_at
        )
        replacements += self._recover_expired_leases(now=started_at)
        capped = self._ensure_initial_deliveries(now=started_at)
        # Шапка дайджеста ставится в очередь ЗДЕСЬ (пакет фиксируется), но
        # уходит ПОСЛЕ карточек: её кнопки «Одобрить все» берут только позиции
        # в PENDING_OWNER и требуют доставленной карточки, а батч-токен
        # одноразовый. Нажатие до доставки карточек сжигало токен впустую, и
        # uq_owner_digest_item_proposal навсегда запрещал этим предложениям
        # попасть в другой дайджест.
        digest = (
            None
            if self._digest_hour is None
            else self.collect_digest(now=started_at, trigger="SCHEDULE")
        )
        claimed = self._claim(worker_id=worker_id, now=started_at, limit=limit)
        sent = retries = failed_visible = rate_limited = 0
        for index, delivery in enumerate(claimed):
            try:
                self._throttle()
                message_id = self._client.send_message(
                    chat_id=self._settings.chat_id,
                    text=str(delivery["rendered_text"]),
                    inline_keyboard=self._keyboard(delivery),
                )
                self._bind_sent(delivery, message_id=message_id, now=started_at)
                sent += 1
            except TelegramRateLimited as exc:
                # Дальше по очереди упрёмся в тот же лимит — прекращаем прогон.
                # Уже арендованные карточки возвращаем в очередь ЯВНО: иначе их
                # лиз протухнет, и восстановление лизов пометит их
                # FAILED_VISIBLE, отзовёт кнопки и создаст лишние поколения.
                for pending in claimed[index:]:
                    self._defer_rate_limited(
                        pending,
                        now=started_at,
                        retry_after=exc.retry_after,
                    )
                rate_limited += 1
                logger.warning(
                    "Telegram троттлит доставку, пауза %s сек (отложено %d карточек)",
                    exc.retry_after,
                    len(claimed) - index,
                )
                break
            except TelegramDeliveryRejected:
                self._retry_rejected(delivery, now=started_at)
                retries += 1
            except Exception:
                # Любая неоднозначная ошибка после начала send может означать,
                # что Telegram показал сообщение: старые кнопки отзываются.
                replaced = self._replace_unknown(
                    delivery,
                    now=started_at,
                    error_code="UNKNOWN_SEND_RESULT",
                )
                failed_visible += int(replaced)
                replacements += int(replaced)
        # Троттлинг на карточках означает, что и след сейчас не пройдёт. Шапка
        # дайджеста обязана уйти ПОСЛЕ своих карточек, поэтому переносим весь
        # след на следующий тик, а не отправляем шапку к недоставленному пакету.
        trail_sent = trail_failed = 0
        if not rate_limited:
            trail_sent, trail_failed = self._deliver_trail(
                worker_id=worker_id,
                now=started_at,
                limit=limit,
            )
        return DeliveryRun(
            claimed_count=len(claimed),
            sent_count=sent,
            retry_count=retries,
            failed_visible_count=failed_visible,
            replacement_count=replacements,
            trail_sent_count=trail_sent,
            digest_id=None if digest is None else digest.digest_id,
            trail_failed_count=trail_failed,
            rate_limited_count=rate_limited,
            rotation_failed_count=rotation_failed,
            generation_capped_count=capped,
        )


def deliver_owner_outbox(
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 20,
) -> DeliveryRun:
    """Публичный worker entry point с обязательной fail-fast конфигурацией."""

    settings = load_owner_approval_config()
    client = RequestsTelegramDeliveryClient(settings.bot_token)
    return OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        client,
        digest_hour=load_digest_hour(),
        send_interval_seconds=SEND_INTERVAL_SECONDS,
    ).deliver(worker_id=worker_id, now=now, limit=limit)


def run_owner_digest_now(
    outbox: OwnerDeliveryOutbox,
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 20,
) -> DeliveryRun:
    """Полный ручной цикл дайджеста на ГОТОВОМ outbox.

    Порядок обязателен: ``_ensure_initial_deliveries`` первым, иначе предложения,
    созданные после последнего тика крона, не имеют строки доставки и в ручной
    пакет вообще не попадают — владелец жмёт «/digest» и видит вчерашний остаток.
    """

    started_at = now or datetime.now(timezone.utc)
    _require_aware(started_at, "now")
    outbox._ensure_initial_deliveries(now=started_at)  # noqa: SLF001
    outbox.release_pending_for_digest(now=started_at)
    outbox.collect_digest(now=started_at, trigger="MANUAL")
    return outbox.deliver(worker_id=worker_id, now=started_at, limit=limit)


def send_owner_digest_now(
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 20,
) -> DeliveryRun:
    """Ручная команда «/digest»: собрать и прислать пакет прямо сейчас."""

    settings = load_owner_approval_config()
    client = RequestsTelegramDeliveryClient(settings.bot_token)
    outbox = OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        client,
        digest_hour=load_digest_hour(),
        send_interval_seconds=SEND_INTERVAL_SECONDS,
    )
    return run_owner_digest_now(
        outbox,
        worker_id=worker_id,
        now=now,
        limit=limit,
    )
