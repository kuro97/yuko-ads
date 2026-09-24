"""Доверенный Telegram ingress и единственный writer owner decision."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from config import (
    OwnerApprovalConfig,
    load_owner_approval_config,
    validate_owner_approval_config,
)
from services.owner_action_models import (
    OwnerDecisionKind,
    OwnerDecisionResult,
    canonical_json,
)
from services.owner_action_repository import (
    OwnerActionLifecycleConflict,
    OwnerActionLineageError,
    OwnerActionRepository,
    OwnerActionTokenUnavailable,
)
from services.owner_delivery_outbox import (
    SEND_INTERVAL_SECONDS,
    OwnerDeliveryOutbox,
    RequestsTelegramDeliveryClient,
    _callback_mac,
    _mac_prefix,
    digest_batch_mac,
    enqueue_trail_message,
    latest_proposal_message_id,
    load_digest_hour,
    run_owner_digest_now,
)


logger = logging.getLogger(__name__)

TELEGRAM_INBOX_LEASE_SECONDS = 60
MAX_TELEGRAM_WEBHOOK_BYTES = 1024 * 1024
# Ручной /digest исполняется ВНУТРИ обработки инбокса, у которого лиз 60 секунд.
# При ~1 сообщении в секунду 20 карточек укладываются с запасом, остаток уходит
# следующим тиком крона.
MANUAL_DIGEST_LIMIT = 20
TRAIL_FLUSH_LIMIT = 5
# Ответ на нажатие Telegram принимает считанные минуты, поэтому ack идёт первым
# делом и с коротким таймаутом: просроченный ack ничем не лучше потерянного, а
# ждать зависший api.telegram.org 10 секунд посреди обработки решения нельзя.
ACK_ANSWER_TIMEOUT_SECONDS = 4
_TZ_LOCAL = timezone(timedelta(hours=5))
_CALLBACK_RE = re.compile(
    r"^oa:(?P<decision>[arp]):"
    r"(?P<nonce>[A-Za-z0-9_-]{16})\.(?P<mac>[A-Za-z0-9_-]{22})$"
)
_DIGEST_CALLBACK_RE = re.compile(
    r"^od:(?P<batch>[aro]):"
    r"(?P<nonce>[A-Za-z0-9_-]{16})\.(?P<mac>[A-Za-z0-9_-]{22})$"
)
_DECISION_BY_CODE = {
    "a": OwnerDecisionKind.APPROVE,
    "r": OwnerDecisionKind.REJECT,
    "p": OwnerDecisionKind.POSTPONE,
}
_BATCH_KIND_BY_CODE = {
    "a": "APPROVE_ALL",
    "r": "REJECT_ALL",
    "o": "ONE_BY_ONE",
}
_BATCH_DECISION = {
    "APPROVE_ALL": OwnerDecisionKind.APPROVE,
    "REJECT_ALL": OwnerDecisionKind.REJECT,
}

# E4: свободный фидбек реплаем. Простые сроки распознаём регуляркой, остальное
# сохраняется как комментарий без попытки «угадать» намерение.
_POSTPONE_TOMORROW_RE = re.compile(
    r"(?:^|\W)(?:завтра|ещ[её]\s+(?:один\s+)?д(?:ень|енёк)|да[йи]\s+д(?:ень|енёк)"
    r"|ещ[её]\s*1\s*д(?:ень|н[яей])?)(?:\W|$)",
    re.IGNORECASE,
)
_POSTPONE_DAYS_RE = re.compile(
    r"(?:^|\W)(?:чере[зc]\s+)?(?P<days>\d{1,2})\s*"
    r"(?:д|дн|дня|дней|день|дневн\w*)(?:\W|$)",
    re.IGNORECASE,
)
_DIGEST_COMMAND_RE = re.compile(r"^\s*/digest(?:@\w+)?\s*$", re.IGNORECASE)
# Батч-кнопка «Одобрить все» живёт только на шапке дайджеста. Карточка, чья
# первая отправка сорвалась, пересоздаётся и уходит НЕМЕДЛЕННО, вне дайджеста —
# так пачка запусков может прийти поштучно, и одобрить её разом было бы нечем.
# Команда закрывает эту дыру: то же самое решение владельца, тем же путём
# (_record_owner_decision с его owner_user_id), но по всем висящим карточкам.
_APPROVE_ALL_COMMAND_RE = re.compile(
    r"^\s*/approve_all(?:@\w+)?\s*$", re.IGNORECASE
)
# Потолок одной команды: обработчик крутится внутри инбокса с лизом 60 секунд,
# а каждое решение — отдельная транзакция. Остаток берётся повторной командой.
APPROVE_ALL_LIMIT = 40
MAX_POSTPONE_DAYS = 30


class OwnerTelegramIngressError(RuntimeError):
    """Доверенный Telegram ingress не прошёл fail-closed проверку."""


class OwnerTelegramWebhookUnauthorized(OwnerTelegramIngressError):
    """Webhook secret отсутствует или не совпал."""


class OwnerTelegramUpdateInvalid(OwnerTelegramIngressError):
    """Telegram update не соответствует закрытому контракту."""


class TelegramPollingClient(Protocol):
    def get_updates(self, *, offset: int) -> Sequence[Mapping[str, object]]:
        """Получает updates напрямую от Telegram HTTPS API."""


class TelegramAckClient(Protocol):
    """Мгновенный ack нажатия: ответ на callback и правка текста карточки."""

    def answer_callback(self, *, callback_query_id: str, text: str) -> None:
        ...

    def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> None:
        ...


class RequestsTelegramAckClient:
    """HTTPS ack; сбой ack никогда не отменяет уже записанное решение."""

    def __init__(
        self,
        bot_token: str,
        *,
        timeout_seconds: int = 10,
        answer_timeout_seconds: int = ACK_ANSWER_TIMEOUT_SECONDS,
    ) -> None:
        self._bot_token = bot_token
        self._timeout_seconds = timeout_seconds
        self._answer_timeout_seconds = answer_timeout_seconds

    def answer_callback(self, *, callback_query_id: str, text: str) -> None:
        import requests

        # Короткий таймаут: ack держит обработку нажатия и обязан либо уйти
        # быстро, либо не уйти вовсе — решение владельца от него не зависит.
        requests.post(
            f"https://api.telegram.org/bot{self._bot_token}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=self._answer_timeout_seconds,
        ).raise_for_status()

    def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> None:
        import requests

        requests.post(
            f"https://api.telegram.org/bot{self._bot_token}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": text},
            timeout=self._timeout_seconds,
        ).raise_for_status()


@dataclass(frozen=True, slots=True)
class BatchDecisionReport:
    """Честный частичный исход батч-кнопки дайджеста."""

    digest_id: str
    batch_kind: str
    total: int
    approved: int
    rejected: int
    blocked: int
    blocked_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OwnerFeedbackRecord:
    """Свободный фидбек владельца реплаем."""

    feedback_id: str
    proposal_id: str | None
    digest_id: str | None
    parsed_action: str
    parsed_until: str | None


@dataclass(frozen=True, slots=True)
class TelegramPollResult:
    fetched_count: int
    inserted_count: int
    next_update_id: int


@dataclass(frozen=True, slots=True)
class TelegramWebhookResult:
    update_id: int
    accepted: bool
    deduplicated: bool


@dataclass(frozen=True, slots=True)
class OwnerDecisionBatch:
    claimed_count: int
    processed_count: int
    decision_count: int
    failed_count: int
    results: tuple[OwnerDecisionResult, ...]
    batch_reports: tuple["BatchDecisionReport", ...] = ()
    feedback: tuple["OwnerFeedbackRecord", ...] = ()


class RequestsTelegramPollingClient:
    """Закрытый getUpdates client; payload нельзя передать business-кодом."""

    def __init__(self, bot_token: str, *, timeout_seconds: int = 10) -> None:
        self._bot_token = bot_token
        self._timeout_seconds = timeout_seconds

    def get_updates(self, *, offset: int) -> Sequence[Mapping[str, object]]:
        import requests

        response = requests.get(
            f"https://api.telegram.org/bot{self._bot_token}/getUpdates",
            params={
                # "message" нужен для свободного фидбека владельца реплаем (E4).
                "offset": offset,
                "timeout": 0,
                "allowed_updates": ["callback_query", "message"],
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("ok") is not True or not isinstance(payload.get("result"), list):
            raise OwnerTelegramIngressError("TELEGRAM_GET_UPDATES_REJECTED")
        result = payload["result"]
        if any(not isinstance(update, Mapping) for update in result):
            raise OwnerTelegramUpdateInvalid("TELEGRAM_RESULT_INVALID")
        return result


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} должен содержать timezone")


def _iso(value: datetime) -> str:
    _require_aware(value, "datetime")
    return value.astimezone(timezone.utc).isoformat()


def _parse_update_id(update: Mapping[str, object]) -> int:
    update_id = update.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
        raise OwnerTelegramUpdateInvalid("UPDATE_ID_INVALID")
    return update_id


def _bot_identity(bot_token: str) -> str:
    return hashlib.sha256(bot_token.encode("utf-8")).hexdigest()


class OwnerApprovalTelegram:
    """Сохраняет ingress до dispatch и пишет решение только через T2 boundary."""

    def __init__(
        self,
        db_path: str | Path,
        settings: OwnerApprovalConfig,
        poll_client: TelegramPollingClient,
        *,
        delivery_outbox: OwnerDeliveryOutbox | None = None,
        ack_client: TelegramAckClient | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._settings = validate_owner_approval_config(settings)
        self._poll_client = poll_client
        self._repository = OwnerActionRepository(db_path)
        self._delivery_outbox = delivery_outbox or OwnerDeliveryOutbox(
            db_path,
            settings,
            RequestsTelegramDeliveryClient(settings.bot_token),
            digest_hour=load_digest_hour(),
            send_interval_seconds=SEND_INTERVAL_SECONDS,
        )
        self._ack_client = ack_client or RequestsTelegramAckClient(settings.bot_token)
        # На один callback_query Telegram принимает ровно один ответ; набор живёт
        # в пределах одного прохода инбокса, поэтому не растёт.
        self._answered_queries: set[str] = set()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _current_poll_cursor(self, *, now: datetime) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT next_update_id FROM telegram_poll_cursor WHERE singleton = 1
                """
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO telegram_poll_cursor (
                        singleton, next_update_id, version, updated_at
                    ) VALUES (1, 0, 1, ?)
                    """,
                    (_iso(now),),
                )
                cursor = 0
            else:
                cursor = int(row["next_update_id"])
            connection.commit()
            return cursor
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _persist_updates(
        self,
        updates: Sequence[Mapping[str, object]],
        *,
        ingress_kind: str,
        now: datetime,
        expected_offset: int | None,
        raw_bodies: Sequence[bytes] | None = None,
    ) -> tuple[int, int]:
        if ingress_kind not in {"GET_UPDATES", "WEBHOOK"}:
            raise ValueError("Неизвестный ingress_kind")
        if raw_bodies is not None and len(raw_bodies) != len(updates):
            raise ValueError("raw_bodies не совпадает с updates")
        update_ids = [_parse_update_id(update) for update in updates]
        if update_ids != sorted(set(update_ids)):
            raise OwnerTelegramUpdateInvalid("UPDATE_SEQUENCE_INVALID")
        if expected_offset is not None and any(
            update_id < expected_offset for update_id in update_ids
        ):
            raise OwnerTelegramUpdateInvalid("UPDATE_BEFORE_CURSOR")

        connection = self._connect()
        inserted = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            if expected_offset is not None:
                cursor = connection.execute(
                    """
                    SELECT next_update_id, version
                    FROM telegram_poll_cursor WHERE singleton = 1
                    """
                ).fetchone()
                if cursor is None or int(cursor["next_update_id"]) != expected_offset:
                    raise OwnerTelegramIngressError("POLL_CURSOR_CAS_CONFLICT")
            for index, update in enumerate(updates):
                encoded = (
                    raw_bodies[index]
                    if raw_bodies is not None
                    else canonical_json(update)
                )
                try:
                    raw_update_json = encoded.decode("utf-8")
                    parsed = json.loads(raw_update_json)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise OwnerTelegramUpdateInvalid("UPDATE_JSON_INVALID") from exc
                if (
                    not isinstance(parsed, dict)
                    or _parse_update_id(parsed) != update_ids[index]
                ):
                    raise OwnerTelegramUpdateInvalid("UPDATE_ENVELOPE_MISMATCH")
                raw_sha256 = hashlib.sha256(encoded).hexdigest()
                existing = connection.execute(
                    """
                    SELECT raw_update_sha256 FROM telegram_update_inbox
                    WHERE update_id = ?
                    """,
                    (update_ids[index],),
                ).fetchone()
                if existing is not None:
                    if not hmac.compare_digest(
                        str(existing["raw_update_sha256"]),
                        raw_sha256,
                    ):
                        raise OwnerTelegramUpdateInvalid("UPDATE_ID_PAYLOAD_CONFLICT")
                    continue
                connection.execute(
                    """
                    INSERT INTO telegram_update_inbox (
                        update_id, ingress_kind, bot_token_identity_sha256,
                        raw_update_json, raw_update_sha256, state, attempts,
                        received_at
                    ) VALUES (?, ?, ?, ?, ?, 'RECEIVED', 0, ?)
                    """,
                    (
                        update_ids[index],
                        ingress_kind,
                        _bot_identity(self._settings.bot_token),
                        raw_update_json,
                        raw_sha256,
                        _iso(now),
                    ),
                )
                inserted += 1
            next_update_id = (
                max(update_ids) + 1
                if update_ids
                else expected_offset
                if expected_offset is not None
                else 0
            )
            if expected_offset is not None and next_update_id != expected_offset:
                updated = connection.execute(
                    """
                    UPDATE telegram_poll_cursor
                    SET next_update_id = ?, version = version + 1, updated_at = ?
                    WHERE singleton = 1 AND next_update_id = ?
                    """,
                    (next_update_id, _iso(now), expected_offset),
                ).rowcount
                if updated != 1:
                    raise OwnerTelegramIngressError("POLL_CURSOR_CAS_CONFLICT")
            connection.commit()
            return inserted, next_update_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def poll(self, *, now: datetime | None = None) -> TelegramPollResult:
        received_at = now or datetime.now(timezone.utc)
        _require_aware(received_at, "now")
        cursor = self._current_poll_cursor(now=received_at)
        updates = self._poll_client.get_updates(offset=cursor)
        # Копия фиксирует ответ клиента до проверки и durable insert.
        immutable_updates = tuple(dict(update) for update in updates)
        inserted, next_update_id = self._persist_updates(
            immutable_updates,
            ingress_kind="GET_UPDATES",
            now=received_at,
            expected_offset=cursor,
        )
        return TelegramPollResult(
            fetched_count=len(immutable_updates),
            inserted_count=inserted,
            next_update_id=next_update_id,
        )

    def webhook(
        self,
        *,
        raw_body: bytes,
        secret_header: str,
        remote_addr: str,
        now: datetime | None = None,
    ) -> TelegramWebhookResult:
        received_at = now or datetime.now(timezone.utc)
        _require_aware(received_at, "now")
        # Secret проверяется до размера, UTF-8, JSON и любого обращения к БД.
        if not hmac.compare_digest(
            (secret_header or "").encode("utf-8"),
            self._settings.webhook_secret.encode("utf-8"),
        ):
            raise OwnerTelegramWebhookUnauthorized("WEBHOOK_SECRET_MISMATCH")
        if (
            not isinstance(raw_body, bytes)
            or len(raw_body) > MAX_TELEGRAM_WEBHOOK_BYTES
        ):
            raise OwnerTelegramUpdateInvalid("WEBHOOK_BODY_INVALID")
        if not isinstance(remote_addr, str) or not remote_addr.strip():
            raise OwnerTelegramUpdateInvalid("REMOTE_ADDR_INVALID")
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OwnerTelegramUpdateInvalid("WEBHOOK_JSON_INVALID") from exc
        if not isinstance(parsed, dict):
            raise OwnerTelegramUpdateInvalid("WEBHOOK_UPDATE_INVALID")
        update_id = _parse_update_id(parsed)
        inserted, _ = self._persist_updates(
            (parsed,),
            ingress_kind="WEBHOOK",
            now=received_at,
            expected_offset=None,
            raw_bodies=(raw_body,),
        )
        return TelegramWebhookResult(
            update_id=update_id,
            accepted=True,
            deduplicated=inserted == 0,
        )

    def _claim_inbox(
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
        lease_until = now + timedelta(seconds=TELEGRAM_INBOX_LEASE_SECONDS)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            update_ids = [
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT update_id
                    FROM telegram_update_inbox
                    WHERE state = 'RECEIVED'
                       OR (state = 'PROCESSING' AND lease_until <= ?)
                    ORDER BY update_id
                    LIMIT ?
                    """,
                    (_iso(now), limit),
                ).fetchall()
            ]
            for update_id in update_ids:
                connection.execute(
                    """
                    UPDATE telegram_update_inbox
                    SET state = 'PROCESSING', attempts = attempts + 1,
                        lease_token = ?, lease_until = ?
                    WHERE update_id = ?
                      AND (state = 'RECEIVED'
                           OR (state = 'PROCESSING' AND lease_until <= ?))
                    """,
                    (
                        lease_token,
                        _iso(lease_until),
                        update_id,
                        _iso(now),
                    ),
                )
            rows = []
            for update_id in update_ids:
                row = connection.execute(
                    """
                    SELECT * FROM telegram_update_inbox
                    WHERE update_id = ? AND state = 'PROCESSING'
                      AND lease_token = ?
                    """,
                    (update_id, lease_token),
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

    def _callback_fields(
        self,
        row: sqlite3.Row,
    ) -> tuple[
        str,
        OwnerDecisionKind,
        int,
        int,
        int,
        int,
        str,
        str,
    ]:
        try:
            update = json.loads(str(row["raw_update_json"]))
            callback = update["callback_query"]
            callback_query_id = callback["id"]
            owner_user_id = callback["from"]["id"]
            message = callback["message"]
            chat_id = message["chat"]["id"]
            message_id = message["message_id"]
            callback_data = callback["data"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise OwnerTelegramUpdateInvalid("CALLBACK_ENVELOPE_INVALID") from exc
        if not isinstance(callback_query_id, str) or not callback_query_id:
            raise OwnerTelegramUpdateInvalid("CALLBACK_QUERY_ID_INVALID")
        if not isinstance(callback_data, str):
            raise OwnerTelegramUpdateInvalid("CALLBACK_DATA_INVALID")
        if owner_user_id != self._settings.owner_user_id:
            raise OwnerTelegramUpdateInvalid("OWNER_USER_MISMATCH")
        if chat_id != self._settings.chat_id:
            raise OwnerTelegramUpdateInvalid("OWNER_CHAT_MISMATCH")
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
        ):
            raise OwnerTelegramUpdateInvalid("MESSAGE_ID_INVALID")
        match = _CALLBACK_RE.fullmatch(callback_data)
        if match is None:
            raise OwnerTelegramUpdateInvalid("CALLBACK_DATA_INVALID")
        decision = _DECISION_BY_CODE[match.group("decision")]
        return (
            match.group("nonce"),
            decision,
            int(owner_user_id),
            int(chat_id),
            message_id,
            int(row["update_id"]),
            callback_query_id,
            match.group("mac"),
        )

    def _record_callback(
        self,
        row: sqlite3.Row,
        *,
        now: datetime,
    ) -> OwnerDecisionResult:
        (
            nonce,
            decision,
            owner_user_id,
            chat_id,
            message_id,
            update_id,
            callback_query_id,
            supplied_prefix,
        ) = self._callback_fields(row)
        connection = self._connect()
        try:
            token = connection.execute(
                """
                SELECT token_id, proposal_id, token_mac_sha256,
                       delivery_generation, decision_kind,
                       consumed_at, revoked_at, revoke_reason, expires_at
                FROM owner_callback_tokens
                WHERE public_nonce = ?
                """,
                (nonce,),
            ).fetchone()
        finally:
            connection.close()
        if token is None or str(token["decision_kind"]) != decision.value:
            raise OwnerActionTokenUnavailable("TOKEN_NOT_FOUND")
        expected_mac = _callback_mac(
            secret=self._settings.callback_secret,
            nonce=nonce,
            proposal_id=str(token["proposal_id"]),
            generation=int(token["delivery_generation"]),
            decision=decision,
        )
        if not hmac.compare_digest(
            expected_mac,
            str(token["token_mac_sha256"]),
        ) or not hmac.compare_digest(_mac_prefix(expected_mac), supplied_prefix):
            raise OwnerActionTokenUnavailable("TOKEN_MAC_MISMATCH")
        # Ответ на нажатие уходит СИНХРОННО и первым: решение уже провалидировано
        # (нонс, MAC, чат, владелец), а Telegram отклоняет ответ на устаревший
        # callback. Раньше ack шёл после записи решения, постановки задания в
        # очередь и чтения карточки — к моменту отправки query успевал протухнуть
        # и владелец не видел ни «Принято», ни причины отказа.
        #
        # Ранний ack обязан быть ЧЕСТНЫМ: второй answerCallbackQuery по тому же
        # query Telegram не примет (дедуп _answered_queries), поэтому отказ,
        # видимый уже по строке токена (протух, отозван, использован),
        # объявляется прямо здесь. Финальная проверка всё равно за
        # _record_owner_decision.
        refusal_code = self._press_refusal_code(token, now)
        self._answer_press(
            self._ACK_TEXTS[decision]
            if refusal_code is None
            else self._refusal_ack_text(refusal_code),
            callback_query_id=callback_query_id,
        )
        try:
            result = self._repository._record_owner_decision(  # noqa: SLF001
                proposal_id=str(token["proposal_id"]),
                token_id=str(token["token_id"]),
                decision=decision,
                owner_user_id=owner_user_id,
                chat_id=chat_id,
                message_id=message_id,
                delivery_generation=int(token["delivery_generation"]),
                telegram_update_id=update_id,
                callback_query_id=callback_query_id,
                trusted_ingress_sha256=str(row["raw_update_sha256"]),
                reason_text=None,
                actor="telegram-owner",
                now=now,
            )
        except (
            OwnerActionTokenUnavailable,
            OwnerActionLineageError,
            OwnerActionLifecycleConflict,
        ) as exc:
            # Тост мог уйти ложным «Принято» (гонка: токен был жив на прочтении)
            # или вовсе не дойти — отказ дублируется durable следом на карточку.
            self._trail_press_refusal(
                proposal_id=str(token["proposal_id"]),
                error_code=str(exc) or type(exc).__name__,
                now=now,
            )
            raise
        if decision is OwnerDecisionKind.POSTPONE:
            self._delivery_outbox.schedule_postponed_redelivery(
                result.proposal_id,
                now=now,
            )
        self._ack_decision(
            decision=decision,
            chat_id=chat_id,
            message_id=message_id,
            callback_query_id=callback_query_id,
            proposal_id=result.proposal_id,
        )
        return result

    @staticmethod
    def _press_refusal_code(token: sqlite3.Row, now: datetime) -> str | None:
        """Отказ, видимый уже по строке токена — до транзакции решения.

        Порядок проверок повторяет token_matches в _record_owner_decision:
        использованная кнопка честнее всего объясняется как «решение уже
        принято», отозванная — причиной отзыва (у свипера это PROPOSAL_EXPIRED),
        протухшая — истечением срока карточки.
        """

        if token["consumed_at"] is not None:
            return "TOKEN_ALREADY_USED"
        if token["revoked_at"] is not None:
            # Префикс сохраняет маркер TOKEN для любых причин отзыва, а отзыв
            # свипером (PROPOSAL_EXPIRED) ловится маркером EXPIRED раньше.
            return f"TOKEN_REVOKED:{token['revoke_reason'] or ''}"
        expires_raw = token["expires_at"]
        if expires_raw is not None:
            try:
                expires_at = datetime.fromisoformat(str(expires_raw))
            except ValueError:
                return None
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                return "PROPOSAL_EXPIRED"
        return None

    def _trail_press_refusal(
        self,
        *,
        proposal_id: str,
        error_code: str,
        now: datetime,
    ) -> None:
        """Durable след об отказе нажатия: ответом на карточку, с дедупом.

        Дедуп по (предложение, причина): сколько бы раз владелец ни жал
        мёртвую кнопку, сообщение в чате будет одно.
        """

        try:
            head = error_code.split(":", 1)[0][:40]
            enqueue_trail_message(
                self._db_path,
                trail_kind="EXECUTION_RESULT",
                dedupe_key=f"press-refusal:{proposal_id}:{head}",
                rendered_text=(
                    f"⚠️ Нажатие не принято: {self._refusal_ack_text(error_code)}"
                ),
                proposal_id=proposal_id,
                reply_to_message_id=latest_proposal_message_id(
                    self._db_path, proposal_id
                ),
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 — след не важнее самого отказа
            logger.warning(
                "Отказ нажатия %s не записан в след: %s",
                proposal_id,
                type(exc).__name__,
            )

    def _answer_press(self, text: str, *, callback_query_id: str) -> bool:
        """Единственный ответ Telegram на этот callback. Сбой не роняет обработку.

        Telegram принимает ровно один answerCallbackQuery на query, поэтому
        повторные попытки (например ack отказа после уже отправленного «Принято»)
        отсекаются здесь, а не превращаются в 400 и мусор в логе. Неудачная
        попытка тоже считается: второй заход по тому же query почти всегда
        упирается в «query is too old» и только тратит таймаут обработки.
        """

        if callback_query_id in self._answered_queries:
            return False
        self._answered_queries.add(callback_query_id)
        try:
            self._ack_client.answer_callback(
                callback_query_id=callback_query_id,
                text=text[:190],
            )
        except Exception as exc:  # noqa: BLE001 — решение владельца от ack не зависит
            logger.warning("Ack callback не ушёл: %s", type(exc).__name__)
            return False
        return True

    # ------------------------------------------------------------------
    # E5а: мгновенный ack нажатия.
    # ------------------------------------------------------------------

    _ACK_TEXTS = {
        OwnerDecisionKind.APPROVE: "⏳ Принято, исполняю",
        OwnerDecisionKind.REJECT: "❌ Отклонено",
        OwnerDecisionKind.POSTPONE: "⏸ Отложено",
    }

    # Отказ нажатия обязан быть объяснён. Раньше ack уходил ТОЛЬКО на успехе, и
    # владелец видел молчание — отсюда жалоба «кнопки не работают».
    _REFUSAL_ACKS: tuple[tuple[str, str], ...] = (
        ("EXPIRED", "⌛ Предложение устарело — пришлю свежее"),
        ("TOKEN", "🔄 Кнопка устарела: решение уже принято или пришла новая карточка"),
        ("LINEAGE", "🔁 Состояние в Facebook изменилось — пришлю свежее предложение"),
        ("STATE_", "✅ Уже обработано"),
        ("CANCELLED", "🚫 Предложение отменено"),
        ("OWNER_", "🙈 Это нажатие не из твоего чата"),
        ("CALLBACK", "🤷 Не разобрал нажатие — пришлю карточку заново"),
    )

    @classmethod
    def _refusal_ack_text(cls, error_code: str) -> str:
        upper = error_code.upper()
        for marker, text in cls._REFUSAL_ACKS:
            if marker in upper:
                return text
        return "⚠️ Не смог принять нажатие — карточка придёт заново"

    @staticmethod
    def _callback_query_id(row: sqlite3.Row) -> str | None:
        """Толерантно достаёт callback_query.id: тут уже идёт обработка ошибки."""
        try:
            update = json.loads(str(row["raw_update_json"]))
            callback = update["callback_query"]
            query_id = callback["id"]
        except (KeyError, TypeError, ValueError):
            return None
        return query_id if isinstance(query_id, str) and query_id else None

    def _ack_refusal(self, row: sqlite3.Row, error_code: str) -> None:
        """Best-effort ответ владельцу на отклонённое нажатие."""
        callback_query_id = self._callback_query_id(row)
        if callback_query_id is None:
            return
        self._answer_press(
            self._refusal_ack_text(error_code),
            callback_query_id=callback_query_id,
        )

    def _card_text(self, proposal_id: str) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT rendered_text FROM telegram_delivery_outbox
                WHERE proposal_id = ? AND purpose = 'OWNER_PROPOSAL'
                  AND state = 'SENT'
                ORDER BY generation DESC
                LIMIT 1
                """,
                (proposal_id,),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else str(row["rendered_text"])

    def _ack_decision(
        self,
        *,
        decision: OwnerDecisionKind,
        chat_id: int,
        message_id: int,
        callback_query_id: str,
        proposal_id: str,
        note: str | None = None,
    ) -> None:
        """Дописывает статус в саму карточку (ответ на нажатие уже ушёл раньше).

        Best-effort: недоступный Telegram не откатывает durable решение.
        """
        text = note or self._ACK_TEXTS[decision]
        # Ответ на нажатие уже ушёл в _record_callback; здесь вызов срабатывает
        # только для путей без раннего ack и сам себя отсекает по query id.
        self._answer_press(text, callback_query_id=callback_query_id)
        card = self._card_text(proposal_id)
        if card is None:
            return
        try:
            self._ack_client.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"{card}\n\n{text}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ack правка карточки не ушла: %s", type(exc).__name__)

    # ------------------------------------------------------------------
    # E3: батч-кнопки дайджеста.
    # ------------------------------------------------------------------

    def _digest_callback_fields(
        self,
        row: sqlite3.Row,
    ) -> tuple[str, str, int, int, int, str, str]:
        try:
            update = json.loads(str(row["raw_update_json"]))
            callback = update["callback_query"]
            callback_query_id = callback["id"]
            owner_user_id = callback["from"]["id"]
            message = callback["message"]
            chat_id = message["chat"]["id"]
            message_id = message["message_id"]
            callback_data = callback["data"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise OwnerTelegramUpdateInvalid("CALLBACK_ENVELOPE_INVALID") from exc
        if not isinstance(callback_query_id, str) or not callback_query_id:
            raise OwnerTelegramUpdateInvalid("CALLBACK_QUERY_ID_INVALID")
        if owner_user_id != self._settings.owner_user_id:
            raise OwnerTelegramUpdateInvalid("OWNER_USER_MISMATCH")
        if chat_id != self._settings.chat_id:
            raise OwnerTelegramUpdateInvalid("OWNER_CHAT_MISMATCH")
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
        ):
            raise OwnerTelegramUpdateInvalid("MESSAGE_ID_INVALID")
        match = _DIGEST_CALLBACK_RE.fullmatch(str(callback_data))
        if match is None:
            raise OwnerTelegramUpdateInvalid("CALLBACK_DATA_INVALID")
        return (
            match.group("nonce"),
            _BATCH_KIND_BY_CODE[match.group("batch")],
            int(owner_user_id),
            int(chat_id),
            message_id,
            callback_query_id,
            match.group("mac"),
        )

    def _consume_digest_token(
        self,
        *,
        nonce: str,
        batch_kind: str,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
        update_id: int,
        callback_query_id: str,
        supplied_prefix: str,
        now: datetime,
    ) -> str:
        """Одноразово гасит батч-токен. Возвращает digest_id."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            token = connection.execute(
                """
                SELECT token_id, digest_id, batch_kind, token_mac_sha256,
                       expected_owner_user_id, expected_chat_id,
                       expected_message_id, expires_at, consumed_at
                FROM owner_digest_batch_tokens
                WHERE public_nonce = ?
                """,
                (nonce,),
            ).fetchone()
            if token is None or str(token["batch_kind"]) != batch_kind:
                raise OwnerActionTokenUnavailable("DIGEST_TOKEN_NOT_FOUND")
            expected_mac = digest_batch_mac(
                secret=self._settings.callback_secret,
                nonce=nonce,
                digest_id=str(token["digest_id"]),
                batch_kind=batch_kind,
            )
            if not hmac.compare_digest(
                expected_mac,
                str(token["token_mac_sha256"]),
            ) or not hmac.compare_digest(_mac_prefix(expected_mac), supplied_prefix):
                raise OwnerActionTokenUnavailable("DIGEST_TOKEN_MAC_MISMATCH")
            if (
                int(token["expected_owner_user_id"]) != owner_user_id
                or int(token["expected_chat_id"]) != chat_id
                or token["expected_message_id"] is None
                or int(token["expected_message_id"]) != message_id
                or token["consumed_at"] is not None
                or datetime.fromisoformat(str(token["expires_at"])) <= now
            ):
                raise OwnerActionTokenUnavailable("DIGEST_TOKEN_BINDING_MISMATCH")
            consumed = connection.execute(
                """
                UPDATE owner_digest_batch_tokens
                SET consumed_at = ?, consumed_update_id = ?,
                    consumed_callback_query_id = ?
                WHERE token_id = ? AND consumed_at IS NULL
                """,
                (_iso(now), update_id, callback_query_id, token["token_id"]),
            ).rowcount
            if consumed != 1:
                raise OwnerActionTokenUnavailable("DIGEST_TOKEN_CONSUME_CONFLICT")
            digest_id = str(token["digest_id"])
            connection.commit()
            return digest_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _digest_pending_items(self, digest_id: str) -> list[sqlite3.Row]:
        connection = self._connect()
        try:
            return connection.execute(
                """
                SELECT i.proposal_id, i.ordinal, l.state, l.delivery_generation
                FROM owner_digest_items i
                JOIN owner_action_lifecycle l USING (proposal_id)
                WHERE i.digest_id = ?
                ORDER BY i.ordinal
                """,
                (digest_id,),
            ).fetchall()
        finally:
            connection.close()

    def _derive_batch_ingress(
        self,
        *,
        source: sqlite3.Row,
        index: int,
        proposal_id: str,
        now: datetime,
    ) -> tuple[int, str]:
        """Отдельная trusted ingress-запись на каждое решение батча.

        ``owner_action_decisions.telegram_update_id`` уникален и ссылается на
        inbox, поэтому N решений из одного нажатия требуют N производных записей.
        Производный ``update_id`` отрицательный: настоящие Telegram id всегда
        положительные, коллизия невозможна. Тело записи содержит исходный update
        плюс индекс элемента, то есть остаётся привязанным к тому же нажатию.
        """
        original = json.loads(str(source["raw_update_json"]))
        derived = {
            "batch_source_update_id": int(source["update_id"]),
            "batch_item_index": index,
            "batch_proposal_id": proposal_id,
            "source_update": original,
        }
        encoded = canonical_json(derived)
        raw_sha256 = hashlib.sha256(encoded).hexdigest()
        derived_update_id = -(int(source["update_id"]) * 1000 + index + 1)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT raw_update_sha256 FROM telegram_update_inbox WHERE update_id = ?",
                (derived_update_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO telegram_update_inbox (
                        update_id, ingress_kind, bot_token_identity_sha256,
                        raw_update_json, raw_update_sha256, state, attempts,
                        received_at, processed_at
                    ) VALUES (?, ?, ?, ?, ?, 'PROCESSED', 1, ?, ?)
                    """,
                    (
                        derived_update_id,
                        str(source["ingress_kind"]),
                        _bot_identity(self._settings.bot_token),
                        encoded.decode("utf-8"),
                        raw_sha256,
                        _iso(now),
                        _iso(now),
                    ),
                )
            elif not hmac.compare_digest(
                str(existing["raw_update_sha256"]),
                raw_sha256,
            ):
                raise OwnerTelegramUpdateInvalid("BATCH_INGRESS_CONFLICT")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return derived_update_id, raw_sha256

    def _proposal_token_id(
        self,
        *,
        proposal_id: str,
        decision: OwnerDecisionKind,
        generation: int,
    ) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT token_id FROM owner_callback_tokens
                WHERE proposal_id = ? AND decision_kind = ?
                  AND delivery_generation = ? AND consumed_at IS NULL
                  AND revoked_at IS NULL
                """,
                (proposal_id, decision.value, generation),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else str(row["token_id"])

    def _record_digest_batch(
        self,
        row: sqlite3.Row,
        *,
        now: datetime,
    ) -> BatchDecisionReport:
        """Батч = N независимых решений, каждое своим обычным путём."""

        (
            nonce,
            batch_kind,
            owner_user_id,
            chat_id,
            message_id,
            callback_query_id,
            supplied_prefix,
        ) = self._digest_callback_fields(row)
        digest_id = self._consume_digest_token(
            nonce=nonce,
            batch_kind=batch_kind,
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            message_id=message_id,
            update_id=int(row["update_id"]),
            callback_query_id=callback_query_id,
            supplied_prefix=supplied_prefix,
            now=now,
        )
        # Батч-нажатие тоже подтверждается сразу после валидации токена: N решений
        # ниже — это N транзакций, к их концу ответ на callback уже просрочен.
        # Подробный итог батча всё равно уходит отдельным сообщением следа.
        # ONE_BY_ONE решений не пишет (одно чтение и ответ), поэтому его ack
        # остаётся содержательным и уходит в _finish_batch.
        if batch_kind != "ONE_BY_ONE":
            self._answer_press(
                "⏳ Принято, обрабатываю",
                callback_query_id=callback_query_id,
            )
        items = self._digest_pending_items(digest_id)

        if batch_kind == "ONE_BY_ONE":
            report = BatchDecisionReport(
                digest_id=digest_id,
                batch_kind=batch_kind,
                total=len(items),
                approved=0,
                rejected=0,
                blocked=0,
                blocked_reasons=(),
            )
            self._finish_batch(
                report,
                chat_id=chat_id,
                message_id=message_id,
                callback_query_id=callback_query_id,
                now=now,
            )
            return report

        decision = _BATCH_DECISION[batch_kind]
        approved = rejected = blocked = 0
        reasons: list[str] = []
        for index, item in enumerate(items):
            proposal_id = str(item["proposal_id"])
            generation = int(item["delivery_generation"])
            token_id = self._proposal_token_id(
                proposal_id=proposal_id,
                decision=decision,
                generation=generation,
            )
            if str(item["state"]) != "PENDING_OWNER":
                # Состояние изменилось после дайджеста — решение блокируется.
                blocked += 1
                reasons.append(f"{proposal_id}:STATE_{item['state']}")
                continue
            if token_id is None:
                blocked += 1
                reasons.append(f"{proposal_id}:TOKEN_UNAVAILABLE")
                continue
            derived_update_id, ingress_sha256 = self._derive_batch_ingress(
                source=row,
                index=index,
                proposal_id=proposal_id,
                now=now,
            )
            try:
                self._repository._record_owner_decision(  # noqa: SLF001
                    proposal_id=proposal_id,
                    token_id=token_id,
                    decision=decision,
                    owner_user_id=owner_user_id,
                    chat_id=chat_id,
                    message_id=self._card_message_id(proposal_id) or message_id,
                    delivery_generation=generation,
                    telegram_update_id=derived_update_id,
                    callback_query_id=f"{callback_query_id}:{index}",
                    trusted_ingress_sha256=ingress_sha256,
                    reason_text=f"digest-batch:{batch_kind}",
                    actor="telegram-owner-batch",
                    now=now,
                )
            except (
                OwnerActionTokenUnavailable,
                OwnerActionLineageError,
                OwnerActionLifecycleConflict,
            ) as exc:
                blocked += 1
                reasons.append(f"{proposal_id}:{str(exc)[:60]}")
                continue
            if decision is OwnerDecisionKind.APPROVE:
                approved += 1
            else:
                rejected += 1

        report = BatchDecisionReport(
            digest_id=digest_id,
            batch_kind=batch_kind,
            total=len(items),
            approved=approved,
            rejected=rejected,
            blocked=blocked,
            blocked_reasons=tuple(reasons),
        )
        self._finish_batch(
            report,
            chat_id=chat_id,
            message_id=message_id,
            callback_query_id=callback_query_id,
            now=now,
        )
        return report

    def _card_message_id(self, proposal_id: str) -> int | None:
        from services.owner_delivery_outbox import latest_proposal_message_id

        return latest_proposal_message_id(self._db_path, proposal_id)

    @staticmethod
    def _batch_report_text(report: BatchDecisionReport) -> str:
        if report.batch_kind == "ONE_BY_ONE":
            return (
                f"🔎 Решаем по одной: {report.total} карточек уже в этой ветке — "
                "у каждой свои кнопки «Одобрить / Отклонить / Отложить»."
            )
        lines = [
            f"Итог батча: одобрено {report.approved}, "
            f"отклонено {report.rejected}, заблокировано {report.blocked}"
        ]
        if report.blocked_reasons:
            lines.append("Причины блокировок:")
            lines.extend(f"• {reason}" for reason in report.blocked_reasons)
        return "\n".join(lines)

    def _finish_batch(
        self,
        report: BatchDecisionReport,
        *,
        chat_id: int,
        message_id: int,
        callback_query_id: str,
        now: datetime,
    ) -> None:
        text = self._batch_report_text(report)
        # Ответ на нажатие уже отправлен до обработки батча — второй раз Telegram
        # его не примет. Здесь вызов остаётся только для путей, где раннего ack
        # не было (ONE_BY_ONE из чужого места), и сам себя отсекает по query id.
        self._answer_press(text, callback_query_id=callback_query_id)
        try:
            enqueue_trail_message(
                self._db_path,
                trail_kind="BATCH_REPORT",
                dedupe_key=f"batch-report:{report.digest_id}:{report.batch_kind}",
                rendered_text=text,
                digest_id=report.digest_id,
                reply_to_message_id=message_id,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Отчёт батча не поставлен в очередь: %s", type(exc).__name__)

    # ------------------------------------------------------------------
    # E4: свободный фидбек владельца реплаем.
    # ------------------------------------------------------------------

    @staticmethod
    def parse_postpone_days(text: str) -> int | None:
        """Простые сроки: «ещё день», «завтра», «через N дней», «N дня»."""

        if _POSTPONE_TOMORROW_RE.search(text):
            return 1
        match = _POSTPONE_DAYS_RE.search(text)
        if match is None:
            return None
        days = int(match.group("days"))
        if not 1 <= days <= MAX_POSTPONE_DAYS:
            return None
        return days

    def _reply_target(self, reply_to_message_id: int | None) -> tuple[str | None, str | None]:
        """(proposal_id, digest_id) по message_id, на который отвечает владелец."""

        if reply_to_message_id is None:
            return None, None
        connection = self._connect()
        try:
            card = connection.execute(
                """
                SELECT proposal_id FROM telegram_delivery_outbox
                WHERE purpose = 'OWNER_PROPOSAL' AND state = 'SENT'
                  AND telegram_message_id = ?
                ORDER BY generation DESC
                LIMIT 1
                """,
                (reply_to_message_id,),
            ).fetchone()
            if card is not None:
                return str(card["proposal_id"]), None
            trail = connection.execute(
                """
                SELECT proposal_id, digest_id FROM owner_trail_messages
                WHERE state = 'SENT' AND telegram_message_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (reply_to_message_id,),
            ).fetchone()
            if trail is None:
                return None, None
            return (
                None if trail["proposal_id"] is None else str(trail["proposal_id"]),
                None if trail["digest_id"] is None else str(trail["digest_id"]),
            )
        finally:
            connection.close()

    def _message_fields(
        self,
        row: sqlite3.Row,
    ) -> tuple[int, int, int, str, int | None] | None:
        """(owner_user_id, chat_id, message_id, text, reply_to_message_id).

        None — это не текстовое сообщение владельца: реплаи чужих user_id и
        любые не-текстовые апдейты игнорируются молча, без FAILED и без ответа.
        """
        try:
            update = json.loads(str(row["raw_update_json"]))
        except json.JSONDecodeError:
            return None
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            return None
        sender = message.get("from")
        chat = message.get("chat")
        text = message.get("text")
        message_id = message.get("message_id")
        if (
            not isinstance(sender, dict)
            or not isinstance(chat, dict)
            or not isinstance(text, str)
            or not text.strip()
            or not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
        ):
            return None
        if sender.get("id") != self._settings.owner_user_id:
            return None
        if chat.get("id") != self._settings.chat_id:
            return None
        reply_to = message.get("reply_to_message")
        reply_to_message_id = None
        if isinstance(reply_to, dict):
            candidate = reply_to.get("message_id")
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                reply_to_message_id = candidate
        return (
            int(self._settings.owner_user_id),
            int(self._settings.chat_id),
            message_id,
            text,
            reply_to_message_id,
        )

    def _persist_feedback(
        self,
        *,
        update_id: int,
        proposal_id: str | None,
        digest_id: str | None,
        message_id: int,
        reply_to_message_id: int | None,
        owner_user_id: int,
        chat_id: int,
        text: str,
        parsed_action: str,
        parsed_until: str | None,
        now: datetime,
    ) -> str:
        feedback_id = str(uuid.uuid4())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            # Идемпотентность по update_id обязательна: Telegram переотдаёт
            # апдейты, пока offset не подтверждён, а любое падение обработки
            # как раз и оставляет offset неподтверждённым. Голый INSERT
            # превращал это в замкнутый круг — инбокс падал на
            # каждом тике с UNIQUE constraint и не читал НИ ОДНОЙ новой
            # команды владельца, пока цикл не разорвали руками.
            existing = connection.execute(
                "SELECT feedback_id FROM owner_feedback WHERE telegram_update_id = ?",
                (update_id,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                logger.info(
                    "owner_feedback: апдейт %s уже обработан — пропускаю", update_id
                )
                return str(existing["feedback_id"])
            connection.execute(
                """
                INSERT INTO owner_feedback (
                    feedback_id, proposal_id, digest_id, telegram_update_id,
                    message_id, reply_to_message_id, owner_user_id, chat_id,
                    text, parsed_action, parsed_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    proposal_id,
                    digest_id,
                    update_id,
                    message_id,
                    reply_to_message_id,
                    owner_user_id,
                    chat_id,
                    text,
                    parsed_action,
                    parsed_until,
                    _iso(now),
                ),
            )
            connection.commit()
            return feedback_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _postpone_by_reply(
        self,
        *,
        proposal_id: str,
        days: int,
        row: sqlite3.Row,
        message_id: int,
        reply_to_message_id: int,
        owner_user_id: int,
        chat_id: int,
        text: str,
        now: datetime,
    ) -> datetime | None:
        """POSTPONE тем же контуром решений: токен POSTPONE текущего поколения."""

        proposal = self._repository.get_proposal(proposal_id)
        if proposal is None or proposal.state != "PENDING_OWNER":
            return None
        token_id = self._proposal_token_id(
            proposal_id=proposal_id,
            decision=OwnerDecisionKind.POSTPONE,
            generation=proposal.delivery_generation,
        )
        if token_id is None:
            return None
        try:
            self._repository._record_owner_decision(  # noqa: SLF001
                proposal_id=proposal_id,
                token_id=token_id,
                decision=OwnerDecisionKind.POSTPONE,
                owner_user_id=owner_user_id,
                chat_id=chat_id,
                message_id=reply_to_message_id,
                delivery_generation=proposal.delivery_generation,
                telegram_update_id=int(row["update_id"]),
                callback_query_id=f"reply:{int(row['update_id'])}",
                trusted_ingress_sha256=str(row["raw_update_sha256"]),
                reason_text=text[:500],
                actor="telegram-owner-reply",
                now=now,
            )
        except (
            OwnerActionTokenUnavailable,
            OwnerActionLineageError,
            OwnerActionLifecycleConflict,
        ) as exc:
            logger.warning("Реплай-POSTPONE отклонён: %s", exc)
            return None
        until = now + timedelta(days=days)
        try:
            self._delivery_outbox.prepare_owner_delivery(
                proposal_id,
                now=now,
                next_attempt_at=until,
                reason_code="OWNER_POSTPONED_REDELIVERY",
            )
        except Exception as exc:  # noqa: BLE001 — решение уже записано
            logger.warning(
                "Реплай-POSTPONE: переотправка не запланирована — %s",
                type(exc).__name__,
            )
        return until

    def _record_message(
        self,
        row: sqlite3.Row,
        *,
        now: datetime,
    ) -> OwnerFeedbackRecord | None:
        fields = self._message_fields(row)
        if fields is None:
            return None
        owner_user_id, chat_id, message_id, text, reply_to_message_id = fields
        proposal_id, digest_id = self._reply_target(reply_to_message_id)

        if _DIGEST_COMMAND_RE.match(text):
            feedback_id = self._persist_feedback(
                update_id=int(row["update_id"]),
                proposal_id=None,
                digest_id=digest_id,
                message_id=message_id,
                reply_to_message_id=reply_to_message_id,
                owner_user_id=owner_user_id,
                chat_id=chat_id,
                text=text,
                parsed_action="DIGEST_NOW",
                parsed_until=None,
                now=now,
            )
            ack_text = self._deliver_manual_digest(now=now)
            self._enqueue_feedback_ack(
                text=ack_text,
                reply_to_message_id=message_id,
                proposal_id=None,
                digest_id=digest_id,
                update_id=int(row["update_id"]),
                now=now,
            )
            return OwnerFeedbackRecord(
                feedback_id=feedback_id,
                proposal_id=None,
                digest_id=digest_id,
                parsed_action="DIGEST_NOW",
                parsed_until=None,
            )

        if _APPROVE_ALL_COMMAND_RE.match(text):
            feedback_id = self._persist_feedback(
                update_id=int(row["update_id"]),
                proposal_id=None,
                digest_id=digest_id,
                message_id=message_id,
                reply_to_message_id=reply_to_message_id,
                owner_user_id=owner_user_id,
                chat_id=chat_id,
                text=text,
                # Справочник parsed_action ограничен CHECK-ом миграции 023
                # ('POSTPONE','COMMENT','DIGEST_NOW'). Своё значение потребовало
                # бы пересборки owner_feedback — отдельная миграция, не в один
                # присест с фиксом. Сам текст команды в строке сохраняется, так
                # что событие в истории отличимо.
                parsed_action="COMMENT",
                parsed_until=None,
                now=now,
            )
            ack_text = self._approve_all_pending(
                row,
                owner_user_id=owner_user_id,
                chat_id=chat_id,
                message_id=message_id,
                now=now,
            )
            self._enqueue_feedback_ack(
                text=ack_text,
                reply_to_message_id=message_id,
                proposal_id=None,
                digest_id=digest_id,
                update_id=int(row["update_id"]),
                now=now,
            )
            return OwnerFeedbackRecord(
                feedback_id=feedback_id,
                proposal_id=None,
                digest_id=digest_id,
                parsed_action="COMMENT",
                parsed_until=None,
            )

        if text.lstrip().startswith("/"):
            # Единый поллер: команды пульта приходят сюда же, что и решения по
            # кнопкам. Раньше их читал ВТОРОЙ getUpdates (файловый офсет в
            # telegram_bot), и апдейты терялись — кто первым подтвердил offset,
            # тот и «съел» их для всех клиентов бота.
            return self._record_console_command(
                row,
                text=text,
                message_id=message_id,
                reply_to_message_id=reply_to_message_id,
                owner_user_id=owner_user_id,
                chat_id=chat_id,
                digest_id=digest_id,
                now=now,
            )

        days = self.parse_postpone_days(text)
        until: datetime | None = None
        if days is not None and proposal_id is not None and reply_to_message_id is not None:
            until = self._postpone_by_reply(
                proposal_id=proposal_id,
                days=days,
                row=row,
                message_id=message_id,
                reply_to_message_id=reply_to_message_id,
                owner_user_id=owner_user_id,
                chat_id=chat_id,
                text=text,
                now=now,
            )
        parsed_action = "POSTPONE" if until is not None else "COMMENT"
        feedback_id = self._persist_feedback(
            update_id=int(row["update_id"]),
            proposal_id=proposal_id,
            digest_id=digest_id,
            message_id=message_id,
            reply_to_message_id=reply_to_message_id,
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            text=text,
            parsed_action=parsed_action,
            parsed_until=None if until is None else _iso(until),
            now=now,
        )
        if until is not None:
            local_date = until.astimezone(_TZ_LOCAL).date().isoformat()
            ack = f"⏸ Понял: отложено до {local_date}, комментарий записан"
        elif proposal_id is not None:
            ack = f"💬 Комментарий записан к предложению №{proposal_id}"
        else:
            ack = "💬 Комментарий записан"
        self._enqueue_feedback_ack(
            text=ack,
            reply_to_message_id=message_id,
            proposal_id=proposal_id,
            digest_id=digest_id,
            update_id=int(row["update_id"]),
            now=now,
        )
        return OwnerFeedbackRecord(
            feedback_id=feedback_id,
            proposal_id=proposal_id,
            digest_id=digest_id,
            parsed_action=parsed_action,
            parsed_until=None if until is None else _iso(until),
        )

    def _record_console_command(
        self,
        row: sqlite3.Row,
        *,
        text: str,
        message_id: int,
        reply_to_message_id: int | None,
        owner_user_id: int,
        chat_id: int,
        digest_id: str | None,
        now: datetime,
    ) -> OwnerFeedbackRecord:
        """Исполняет команду пульта и всегда отвечает владельцу.

        Неизвестная команда получает явный ответ, а не молчание: раньше пульт
        (``telegram_console.handle_message``) просто игнорировал незнакомое, и
        владелец не мог отличить «команды нет» от «бот умер».
        """
        from services import telegram_console

        executed: str | None = None
        try:
            executed = telegram_console.dispatch_owner_text(
                text,
                source_ref=f"telegram-message:{chat_id}:{message_id}",
                requested_at=now,
            )
        except Exception as exc:  # noqa: BLE001 — пульт не роняет ingress
            logger.warning(
                "Команда пульта не исполнена: %s",
                type(exc).__name__,
            )
        feedback_id = self._persist_feedback(
            update_id=int(row["update_id"]),
            proposal_id=None,
            digest_id=digest_id,
            message_id=message_id,
            reply_to_message_id=reply_to_message_id,
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            text=text,
            parsed_action="COMMENT",
            parsed_until=None,
            now=now,
        )
        if executed is None:
            command = text.strip().split()[0][:32]
            self._enqueue_feedback_ack(
                text=f"🤷 Не знаю команду {command}. Что умею — /help",
                reply_to_message_id=message_id,
                proposal_id=None,
                digest_id=digest_id,
                update_id=int(row["update_id"]),
                now=now,
            )
        return OwnerFeedbackRecord(
            feedback_id=feedback_id,
            proposal_id=None,
            digest_id=digest_id,
            parsed_action="COMMENT",
            parsed_until=None,
        )

    def _pending_owner_items(self, *, now: datetime) -> list[sqlite3.Row]:
        """Живые карточки, ждущие решения владельца, старые первыми.

        Берём ровно то же, что видит владелец в чате: PENDING_OWNER с не
        истёкшим сроком. Привязки к дайджесту здесь нет намеренно — команда
        существует как раз для карточек, которые в дайджест не попали.
        """
        connection = self._connect()
        try:
            return connection.execute(
                """
                SELECT l.proposal_id, l.state, l.delivery_generation,
                       p.proposal_kind, p.summary
                FROM owner_action_lifecycle l
                JOIN owner_action_proposals p USING (proposal_id)
                WHERE l.state = 'PENDING_OWNER'
                  AND p.valid_until > ?
                ORDER BY p.created_at, l.proposal_id
                LIMIT ?
                """,
                (_iso(now), APPROVE_ALL_LIMIT),
            ).fetchall()
        finally:
            connection.close()

    def _approve_all_pending(
        self,
        row: sqlite3.Row,
        *,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
        now: datetime,
    ) -> str:
        """Одобряет все висящие карточки — N обычных решений владельца.

        Каждое идёт тем же ``_record_owner_decision``, что и нажатие кнопки:
        со своим callback-токеном, своей trusted ingress-записью и родословной
        владельца. Команда не создаёт нового вида решений и не обходит проверки
        — она лишь избавляет от N нажатий, когда карточки пришли поштучно.
        """
        items = self._pending_owner_items(now=now)
        if not items:
            return "📭 Нечего одобрять — карточек, ждущих решения, нет."

        decision = OwnerDecisionKind.APPROVE
        approved = 0
        blocked: list[str] = []
        for index, item in enumerate(items):
            proposal_id = str(item["proposal_id"])
            generation = int(item["delivery_generation"])
            token_id = self._proposal_token_id(
                proposal_id=proposal_id,
                decision=decision,
                generation=generation,
            )
            if token_id is None:
                blocked.append(f"{item['proposal_kind']}:TOKEN_UNAVAILABLE")
                continue
            derived_update_id, ingress_sha256 = self._derive_batch_ingress(
                source=row,
                index=index,
                proposal_id=proposal_id,
                now=now,
            )
            try:
                self._repository._record_owner_decision(  # noqa: SLF001
                    proposal_id=proposal_id,
                    token_id=token_id,
                    decision=decision,
                    owner_user_id=owner_user_id,
                    chat_id=chat_id,
                    message_id=self._card_message_id(proposal_id) or message_id,
                    delivery_generation=generation,
                    telegram_update_id=derived_update_id,
                    callback_query_id=f"approve-all:{row['update_id']}:{index}",
                    trusted_ingress_sha256=ingress_sha256,
                    reason_text="owner-command:/approve_all",
                    actor="telegram-owner-command",
                    now=now,
                )
            except (
                OwnerActionTokenUnavailable,
                OwnerActionLineageError,
                OwnerActionLifecycleConflict,
            ) as exc:
                blocked.append(f"{item['proposal_kind']}:{str(exc)[:50]}")
                continue
            approved += 1

        lines = [f"✅ Одобрил {approved} из {len(items)}."]
        if blocked:
            lines.append(f"Не прошло {len(blocked)}:")
            lines.extend(f"• {reason}" for reason in blocked[:10])
        if len(items) == APPROVE_ALL_LIMIT:
            lines.append(
                "Показан первый пакет — повтори команду, если осталось ещё."
            )
        return "\n".join(lines)

    def _deliver_manual_digest(self, *, now: datetime) -> str:
        """Собирает И отправляет пакет прямо сейчас. Возвращает текст ack.

        Раньше здесь не было ``deliver()``: пакет собирался, но уходил только
        со следующим тиком крона — владелец нажимал ``/digest`` и до 15 минут
        не видел ничего. Пустая очередь тоже молчала.

        Лимит намеренно небольшой: обработчик крутится ВНУТРИ
        ``process_trusted_telegram_inbox``, у которого лиз инбокса 60 секунд.
        Длинная синхронная отправка пережила бы лиз и уронила всю пачку на
        ``INBOX_LEASE_LOST``; остаток уйдёт следующим тиком.
        """
        try:
            run = run_owner_digest_now(
                self._delivery_outbox,
                worker_id="owner-manual-digest",
                now=now,
                limit=MANUAL_DIGEST_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001 — фидбек уже сохранён
            logger.warning("Ручной дайджест не собран: %s", type(exc).__name__)
            return "⚠️ Дайджест собрать не удалось — повторю на ближайшем тике."
        if run.sent_count == 0 and run.trail_sent_count == 0:
            return "📭 Предложений нет — очередь пуста."
        return f"📋 Отправил {run.sent_count} карточек."

    def _enqueue_feedback_ack(
        self,
        *,
        text: str,
        reply_to_message_id: int,
        proposal_id: str | None,
        digest_id: str | None,
        update_id: int,
        now: datetime,
    ) -> None:
        try:
            enqueue_trail_message(
                self._db_path,
                trail_kind="FEEDBACK_ACK",
                dedupe_key=f"feedback-ack:{update_id}",
                rendered_text=text,
                proposal_id=proposal_id,
                digest_id=digest_id,
                reply_to_message_id=reply_to_message_id,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Подтверждение фидбека не поставлено в очередь: %s",
                type(exc).__name__,
            )
            return
        try:
            # Подтверждение обязано уйти в этом же вызове: иначе владелец видит
            # ответ на своё сообщение только через тик крона (до 15 минут).
            self._delivery_outbox.flush_trail(
                worker_id="owner-feedback-ack",
                now=now,
                limit=TRAIL_FLUSH_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001 — очередь уже durable, повторим
            logger.warning(
                "Подтверждение фидбека не отправлено сразу: %s",
                type(exc).__name__,
            )

    def _finish_inbox(
        self,
        row: sqlite3.Row,
        *,
        state: str,
        now: datetime,
        error_code: str | None,
    ) -> None:
        connection = self._connect()
        try:
            changed = connection.execute(
                """
                UPDATE telegram_update_inbox
                SET state = ?, processed_at = ?, lease_token = NULL,
                    lease_until = NULL, last_error_code = ?
                WHERE update_id = ? AND state = 'PROCESSING'
                  AND lease_token = ?
                """,
                (
                    state,
                    _iso(now),
                    error_code,
                    row["update_id"],
                    row["lease_token"],
                ),
            ).rowcount
            connection.commit()
            if changed != 1:
                raise OwnerTelegramIngressError("INBOX_LEASE_LOST")
        finally:
            connection.close()

    def process_inbox(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        limit: int = 50,
    ) -> OwnerDecisionBatch:
        processed_at = now or datetime.now(timezone.utc)
        _require_aware(processed_at, "now")
        claimed = self._claim_inbox(
            worker_id=worker_id,
            now=processed_at,
            limit=limit,
        )
        results: list[OwnerDecisionResult] = []
        batch_reports: list[BatchDecisionReport] = []
        feedback: list[OwnerFeedbackRecord] = []
        failed = 0
        expected_errors = (
            OwnerTelegramUpdateInvalid,
            OwnerActionTokenUnavailable,
            OwnerActionLineageError,
            OwnerActionLifecycleConflict,
        )
        for row in claimed:
            try:
                kind = self._update_kind(row)
                if kind == "DIGEST_BATCH":
                    batch_reports.append(
                        self._record_digest_batch(row, now=processed_at)
                    )
                elif kind == "MESSAGE":
                    record = self._record_message(row, now=processed_at)
                    if record is not None:
                        feedback.append(record)
                else:
                    results.append(self._record_callback(row, now=processed_at))
                self._finish_inbox(
                    row,
                    state="PROCESSED",
                    now=processed_at,
                    error_code=None,
                )
            except expected_errors as exc:
                error_code = str(exc)[:120] or type(exc).__name__
                logger.warning(
                    "Trusted Telegram callback отклонён: %s",
                    error_code,
                )
                self._ack_refusal(row, error_code)
                self._finish_inbox(
                    row,
                    state="FAILED",
                    now=processed_at,
                    error_code=error_code,
                )
                failed += 1
        return OwnerDecisionBatch(
            claimed_count=len(claimed),
            processed_count=len(results),
            decision_count=sum(result.accepted for result in results),
            failed_count=failed,
            results=tuple(results),
            batch_reports=tuple(batch_reports),
            feedback=tuple(feedback),
        )

    @staticmethod
    def _update_kind(row: sqlite3.Row) -> str:
        """CALLBACK / DIGEST_BATCH / MESSAGE по сырому телу update."""

        try:
            update = json.loads(str(row["raw_update_json"]))
        except json.JSONDecodeError:
            return "CALLBACK"
        if not isinstance(update, dict):
            return "CALLBACK"
        if isinstance(update.get("message"), dict):
            return "MESSAGE"
        callback = update.get("callback_query")
        if isinstance(callback, dict) and str(callback.get("data") or "").startswith(
            "od:"
        ):
            return "DIGEST_BATCH"
        return "CALLBACK"


def _default_service() -> OwnerApprovalTelegram:
    settings = load_owner_approval_config()
    return OwnerApprovalTelegram(
        settings.db_path,
        settings,
        RequestsTelegramPollingClient(settings.bot_token),
    )


def poll_telegram_updates(
    *,
    now: datetime | None = None,
) -> TelegramPollResult:
    """Сам получает updates из Telegram; caller-supplied payload отсутствует."""

    return _default_service().poll(now=now)


def handle_telegram_webhook(
    *,
    raw_body: bytes,
    secret_header: str,
    remote_addr: str,
    now: datetime | None = None,
) -> TelegramWebhookResult:
    return _default_service().webhook(
        raw_body=raw_body,
        secret_header=secret_header,
        remote_addr=remote_addr,
        now=now,
    )


def process_trusted_telegram_inbox(
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 50,
) -> OwnerDecisionBatch:
    return _default_service().process_inbox(
        worker_id=worker_id,
        now=now,
        limit=limit,
    )
