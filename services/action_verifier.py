"""Независимый верификатор исполненных owner-действий (волна E, блок E1).

Что делает: после каждого исполненного действия сверяет ЖИВОЕ состояние
Facebook с тем, что было заявлено, и пишет вердикт в память обучения
(``action_verifications`` из migration 023):

* ``PAUSE``   — объявление реально ``PAUSED``;
* ``UNPAUSE`` — объявление реально ``ACTIVE``;
* ``SCALE``   — ``daily_budget`` адсета равен целевому из manifest;
* ``LAUNCH``  — второго механизма НЕТ: читаем вердикт ``services/launch_verify``
  (``launch_watchdogs``) и включаем его в общий след.

Вердикты: ``VERIFIED`` / ``MISMATCH`` / ``UNVERIFIABLE``. ``UNVERIFIABLE``
(Facebook недоступен) НЕ завершает проверку — следующий тик повторит.

Модуль ничего не мутирует в Facebook: только GET. Любой повтор исполнения идёт
исключительно через существующий execution boundary
(``services/owner_action_executor.execute_owner_approved``) либо через новое
предложение владельцу (``services/action_producer_gateway``).

Про «повтор в рамках того же одобрения». Схема 022 намеренно закрывает вторую
попытку по уже исполненному claim: ``uq_owner_attempt_claim`` уникален по
(proposal_id, claim_id), а ``trg_owner_permit_issue_validate`` прямо запрещает
выдать permit на claim, у которого уже есть attempt. Поэтому «возврат задачи»
здесь означает: (1) заново дёрнуть execution boundary того же одобрения — он
доисполнит незатронутые claims, если они есть, и (2) если после трёх повторов
расхождение живо, остановиться и позвать владельца. Если же живое состояние цели
разошлось с тем, что владелец одобрял, — повтор запрещён вообще, создаётся новое
предложение «повтор невыполненного действия» плюс критический алерт с фактами.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from services.owner_action_models import (
    LifecycleState,
    canonical_json,
    canonical_sha256,
)

logger = logging.getLogger(__name__)

# Окно проверки: исполнения последних 24 часов.
VERIFY_WINDOW_HOURS = 24
# Потолок повторов исполнения в рамках одного одобрения.
MAX_VERIFY_RETRIES = 3
# Типы, которые верификатор умеет сверять (ASSET_RECOVERY сюда сознательно не
# входит: у него нет однозначного живого признака результата).
VERIFIABLE_KINDS = ("LAUNCH", "PAUSE", "UNPAUSE", "SCALE")

_VERDICT_VERIFIED = "VERIFIED"
_VERDICT_MISMATCH = "MISMATCH"
_VERDICT_UNVERIFIABLE = "UNVERIFIABLE"


class ActionVerifierError(RuntimeError):
    """Верификатор не смог безопасно завершить проверку."""


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    """Результат одной сверки одного claim."""

    proposal_id: str
    claim_id: str
    kind: str
    verdict: str
    reason_code: str
    retry_no: int
    expected: Mapping[str, object]
    observed: Mapping[str, object]
    terminal_state: str
    retried: bool
    escalated: bool
    retry_proposal_id: str | None


@dataclass(frozen=True, slots=True)
class VerifierRun:
    """Итог одного тика крона верификатора."""

    checked: int
    verified: int
    mismatch: int
    unverifiable: int
    retried: int
    escalated: int
    errors: tuple[str, ...]
    checks: tuple[VerificationCheck, ...] = ()


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} должен содержать timezone")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _require_aware(value, "datetime").isoformat()


def _owner_db_path() -> Path:
    from config import load_owner_approval_config

    return load_owner_approval_config().db_path


def _connect(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


# --------------------------------------------------------------------------
# Живое чтение Facebook — только GET.
# --------------------------------------------------------------------------


def read_live_ad_state(ad_id: str) -> dict[str, str] | None:
    """Живой ``status``/``effective_status`` объявления. None — FB недоступен."""

    from services.adset_pause_guard import fetch_exact_ad_contexts

    contexts, error = fetch_exact_ad_contexts([ad_id])
    context = contexts.get(ad_id)
    if error is not None or context is None:
        logger.warning(
            "action_verifier: живой статус объявления %s недоступен — %s",
            ad_id,
            error or "missing",
        )
        return None
    return {
        "configured_status": str(context.get("configured_status") or ""),
        "effective_status": str(context.get("effective_status") or ""),
    }


def read_live_adset_budget(adset_id: str) -> Decimal | None:
    """Живой ``daily_budget`` адсета в валюте кабинета. None — FB недоступен.

    Паттерн волны C: ``agent.fb_common._throttled_get`` напрямую, без импорта
    ``integrations.facebook`` целиком. FB отдаёт бюджет в минорных единицах.
    """
    from agent.fb_common import API, _throttled_get
    from services.fb_token_provider import get_fb_token

    try:
        response = _throttled_get(
            f"{API}/{adset_id}",
            params={
                "access_token": get_fb_token(),
                "fields": "id,daily_budget",
            },
        )
        if response.status_code != 200:
            logger.warning(
                "action_verifier: FB вернул %d для adset %s",
                response.status_code,
                adset_id,
            )
            return None
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — недоступность FB это UNVERIFIABLE
        logger.warning(
            "action_verifier: живой бюджет adset %s недоступен — %s",
            adset_id,
            type(exc).__name__,
        )
        return None
    if not isinstance(payload, dict) or str(payload.get("id") or "") != adset_id:
        return None
    try:
        return Decimal(str(payload.get("daily_budget"))) / Decimal("100")
    except (InvalidOperation, TypeError):
        return None


# --------------------------------------------------------------------------
# Сверка по типам действий.
# --------------------------------------------------------------------------


def _state_sha256(subject_id: str, state: object) -> str:
    """Канонический дайджест того самого измерения состояния, что одобрял владелец.

    Полный ``evidence_state_sha256`` из approval-контура включает AMO/CDP/Trello
    и по одному живому GET в Facebook не воспроизводится. Для «возврата задачи»
    важно ровно одно: осталась ли цель в том предсостоянии, действие над которым
    владелец одобрил. Его и хэшируем — тем же ``canonical_sha256``.
    """
    return canonical_sha256({"subject_id": subject_id, "state": state})


def _verify_pause_like(
    *,
    kind: str,
    payload: Mapping[str, object],
    subject_id: str,
) -> tuple[str, str, dict[str, object], dict[str, object], str, str]:
    """Сверяет PAUSE/UNPAUSE. Возвращает (verdict, reason, expected, observed, approved_sha, live_sha)."""

    expected_after = str(payload.get("expected_after_status") or "")
    expected_before = str(payload.get("expected_before_status") or "")
    expected = {
        "kind": kind,
        "ad_id": subject_id,
        "expected_after_status": expected_after,
        "expected_before_status": expected_before,
    }
    live = read_live_ad_state(subject_id)
    if live is None:
        return (
            _VERDICT_UNVERIFIABLE,
            "FB_AD_STATE_UNAVAILABLE",
            expected,
            {"error": "FB_UNAVAILABLE"},
            _state_sha256(subject_id, expected_before),
            "",
        )
    observed = {
        "ad_id": subject_id,
        "configured_status": live["configured_status"],
        "effective_status": live["effective_status"],
    }
    approved_sha = _state_sha256(subject_id, expected_before)
    live_sha = _state_sha256(subject_id, live["effective_status"])
    if live["effective_status"] == expected_after:
        return _VERDICT_VERIFIED, f"LIVE_{expected_after}", expected, observed, approved_sha, live_sha
    return (
        _VERDICT_MISMATCH,
        f"LIVE_IS_{live['effective_status'] or 'UNKNOWN'}",
        expected,
        observed,
        approved_sha,
        live_sha,
    )


def _verify_scale(
    *,
    payload: Mapping[str, object],
    adset_id: str,
) -> tuple[str, str, dict[str, object], dict[str, object], str, str]:
    """Сверяет SCALE: живой daily_budget адсета равен целевому из manifest."""

    try:
        target = Decimal(str(payload.get("target_budget_usd")))
        before = Decimal(str(payload.get("expected_current_budget_usd")))
    except (InvalidOperation, TypeError):
        raise ActionVerifierError("SCALE_PAYLOAD_INVALID") from None
    expected = {
        "kind": "SCALE",
        "adset_id": adset_id,
        "target_budget_usd": str(target),
        "expected_current_budget_usd": str(before),
    }
    live = read_live_adset_budget(adset_id)
    if live is None:
        return (
            _VERDICT_UNVERIFIABLE,
            "FB_ADSET_BUDGET_UNAVAILABLE",
            expected,
            {"error": "FB_UNAVAILABLE"},
            _state_sha256(adset_id, str(before)),
            "",
        )
    observed = {"adset_id": adset_id, "daily_budget_usd": str(live)}
    approved_sha = _state_sha256(adset_id, str(before))
    live_sha = _state_sha256(adset_id, str(live))
    if live == target:
        return _VERDICT_VERIFIED, "LIVE_BUDGET_MATCHES", expected, observed, approved_sha, live_sha
    return (
        _VERDICT_MISMATCH,
        "LIVE_BUDGET_DIFFERS",
        expected,
        observed,
        approved_sha,
        live_sha,
    )


def _verify_launch(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
) -> tuple[str, str, dict[str, object], dict[str, object], str, str]:
    """Читает вердикт services/launch_verify, второй механизм не строит."""

    row = connection.execute(
        """
        SELECT state, expected_count, verified_count, last_reason_code
        FROM launch_watchdogs
        WHERE proposal_id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        expected: dict[str, object] = {"kind": "LAUNCH", "source": "launch_watchdogs"}
        return (
            _VERDICT_UNVERIFIABLE,
            "LAUNCH_WATCHDOG_MISSING",
            expected,
            {"error": "WATCHDOG_MISSING"},
            "",
            "",
        )
    state = str(row["state"])
    expected = {
        "kind": "LAUNCH",
        "source": "launch_watchdogs",
        "expected_count": int(row["expected_count"]),
        "expected_state": "VERIFIED",
    }
    observed = {
        "watchdog_state": state,
        "verified_count": int(row["verified_count"]),
        "reason_code": str(row["last_reason_code"] or ""),
    }
    if state == "VERIFIED":
        return _VERDICT_VERIFIED, "LAUNCH_WATCHDOG_VERIFIED", expected, observed, "", ""
    if state in {"FAILED_VERIFICATION", "RECONCILE_REQUIRED"}:
        return (
            _VERDICT_MISMATCH,
            f"LAUNCH_WATCHDOG_{state}",
            expected,
            observed,
            "",
            "",
        )
    return (
        _VERDICT_UNVERIFIABLE,
        f"LAUNCH_WATCHDOG_{state}",
        expected,
        observed,
        "",
        "",
    )


# --------------------------------------------------------------------------
# Durable запись вердикта.
# --------------------------------------------------------------------------


def _append_event(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    event_type: str,
    reason_code: str,
    payload: Mapping[str, object],
    now: datetime,
) -> None:
    import hashlib

    encoded = canonical_json(payload)
    row = connection.execute(
        """
        SELECT COALESCE(MAX(event_seq), 0) + 1
        FROM owner_action_events
        WHERE proposal_id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:  # pragma: no cover — агрегат всегда возвращает строку
        raise ActionVerifierError("EVENT_SEQUENCE_UNAVAILABLE")
    connection.execute(
        """
        INSERT INTO owner_action_events (
            event_id, proposal_id, event_seq, event_type, actor,
            reason_code, payload_json, payload_sha256, created_at
        ) VALUES (?, ?, ?, ?, 'action-verifier', ?, ?, ?, ?)
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


def _persist_check(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    claim_id: str,
    attempt_id: str | None,
    kind: str,
    verdict: str,
    reason_code: str,
    expected: Mapping[str, object],
    observed: Mapping[str, object],
    retry_no: int,
    terminal_state: str,
    retry_proposal_id: str | None,
    first_seen_at: datetime,
    now: datetime,
) -> int:
    """Пишет проверку и продвигает курсор одной транзакцией. Возвращает check_seq."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """
            SELECT check_seq, retry_count, first_seen_at
            FROM action_verification_state
            WHERE proposal_id = ? AND claim_id = ?
            """,
            (proposal_id, claim_id),
        ).fetchone()
        if row is None:
            check_seq = 1
            connection.execute(
                """
                INSERT INTO action_verification_state (
                    proposal_id, claim_id, kind, state, check_seq, retry_count,
                    last_verdict, retry_proposal_id, first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    claim_id,
                    kind,
                    terminal_state,
                    check_seq,
                    retry_no,
                    verdict,
                    retry_proposal_id,
                    _iso(first_seen_at),
                    _iso(now),
                ),
            )
        else:
            check_seq = int(row["check_seq"]) + 1
            connection.execute(
                """
                UPDATE action_verification_state
                SET state = ?, check_seq = ?, retry_count = ?, last_verdict = ?,
                    retry_proposal_id = COALESCE(?, retry_proposal_id),
                    updated_at = ?
                WHERE proposal_id = ? AND claim_id = ? AND state = 'PENDING'
                """,
                (
                    terminal_state,
                    check_seq,
                    retry_no,
                    verdict,
                    retry_proposal_id,
                    _iso(now),
                    proposal_id,
                    claim_id,
                ),
            )
        connection.execute(
            """
            INSERT INTO action_verifications (
                verification_id, proposal_id, claim_id, attempt_id, kind,
                check_seq, retry_no, expected_json, observed_json, verdict,
                reason_code, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                proposal_id,
                claim_id,
                attempt_id,
                kind,
                check_seq,
                retry_no,
                canonical_json(expected).decode("utf-8"),
                canonical_json(observed).decode("utf-8"),
                verdict,
                reason_code,
                _iso(now),
            ),
        )
        _append_event(
            connection,
            proposal_id=proposal_id,
            event_type=f"ACTION_VERIFICATION_{verdict}",
            reason_code=reason_code,
            payload={
                "claim_id": claim_id,
                "kind": kind,
                "check_seq": check_seq,
                "retry_no": retry_no,
                "expected": expected,
                "observed": observed,
            },
            now=now,
        )
        connection.commit()
        return check_seq
    except Exception:
        connection.rollback()
        raise


# --------------------------------------------------------------------------
# Отчёт владельцу: третье сообщение в ветке решения (E5в).
# --------------------------------------------------------------------------


def _verdict_text(check_kind: str, verdict: str, reason_code: str, observed: Mapping[str, object]) -> str:
    if verdict == _VERDICT_VERIFIED:
        details = {
            "PAUSE": "реально PAUSED",
            "UNPAUSE": "реально ACTIVE",
            "SCALE": f"бюджет реально {observed.get('daily_budget_usd', '?')}",
            "LAUNCH": "запуск подтверждён живым ACTIVE",
        }.get(check_kind, reason_code)
        return f"🔎 Проверено в FB: {details}"
    if verdict == _VERDICT_UNVERIFIABLE:
        return (
            "🔎 Проверка отложена: Facebook не ответил "
            f"({reason_code}). Повторю на следующем тике."
        )
    return (
        f"⚠️ Расхождение: заявлено {check_kind}, в FB — {reason_code}. "
        f"Факты: {json.dumps(dict(observed), ensure_ascii=False)}"
    )


def _enqueue_verdict_message(
    db_path: str | Path,
    *,
    proposal_id: str,
    claim_id: str,
    check_seq: int,
    text: str,
    now: datetime,
) -> None:
    from services.owner_delivery_outbox import (
        enqueue_trail_message,
        latest_proposal_message_id,
    )

    try:
        enqueue_trail_message(
            db_path,
            trail_kind="VERIFICATION_VERDICT",
            dedupe_key=f"verify:{proposal_id}:{claim_id}:{check_seq}",
            rendered_text=text,
            proposal_id=proposal_id,
            reply_to_message_id=latest_proposal_message_id(db_path, proposal_id),
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — сбой отчёта не прячет вердикт
        logger.warning(
            "action_verifier: вердикт %s не поставлен в очередь — %s",
            proposal_id,
            type(exc).__name__,
        )


def _send_critical(title: str, detail: str) -> None:
    try:
        from services.notifications import send_critical_alert

        send_critical_alert(title, detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "action_verifier: критический алерт не ушёл — %s",
            type(exc).__name__,
        )


# --------------------------------------------------------------------------
# Реакция на MISMATCH.
# --------------------------------------------------------------------------


def _retry_same_approval(proposal_id: str, *, now: datetime) -> str:
    """Дёргает существующий execution boundary того же одобрения."""

    from services.owner_action_executor import execute_owner_approved

    run = execute_owner_approved(
        proposal_id,
        worker_id="action-verifier-retry",
        now=now,
    )
    return str(getattr(run, "reason_code", "") or "RETRY_DONE")


def _create_repeat_proposal(
    *,
    kind: str,
    proposal_id: str,
    claim_id: str,
    subject_id: str,
    adset_id: str | None,
    payload: Mapping[str, object],
    observed: Mapping[str, object],
    now: datetime,
) -> str | None:
    """Создаёт НОВОЕ предложение «повтор невыполненного действия»."""

    from services import action_producer_gateway as producer

    scope = f"verifier-repeat:{proposal_id}:{claim_id}"
    try:
        if kind == "PAUSE":
            outcome = producer.propose_pause(
                subject_id,
                origin="RECOVERY",
                scope=scope,
                reason_code="VERIFIER_REPEAT_UNDONE_ACTION",
                now=now,
            )
        elif kind == "UNPAUSE":
            outcome = producer.propose_unpause(
                subject_id,
                origin="RECOVERY",
                scope=scope,
                now=now,
            )
        elif kind == "SCALE":
            outcome = producer.propose_scale(
                {
                    "adset_id": adset_id or subject_id,
                    "ad_id": str(payload.get("candidate_ad_id") or subject_id),
                    "current_budget_usd": str(
                        observed.get("daily_budget_usd")
                        or payload.get("expected_current_budget_usd")
                    ),
                    "new_budget_usd": str(payload.get("target_budget_usd")),
                },
                scope=scope,
                now=now,
            )
        else:
            # LAUNCH повторяется своим контуром, второго механизма не строим.
            return None
    except Exception as exc:  # noqa: BLE001 — алерт всё равно уйдёт владельцу
        logger.warning(
            "action_verifier: повторное предложение для %s не создано — %s",
            proposal_id,
            exc,
        )
        return None
    return outcome.proposal_id


def _handle_mismatch(
    connection: sqlite3.Connection,
    *,
    db_path: str | Path,
    proposal_id: str,
    claim_id: str,
    kind: str,
    subject_id: str,
    adset_id: str | None,
    payload: Mapping[str, object],
    observed: Mapping[str, object],
    approved_sha: str,
    live_sha: str,
    valid_until: datetime,
    retry_count: int,
    now: datetime,
) -> tuple[str, int, str, bool, bool, str | None]:
    """Решает судьбу расхождения.

    Returns:
        (terminal_state, retry_no, reason_code, retried, escalated, retry_proposal_id)
    """
    state_unchanged = bool(approved_sha) and approved_sha == live_sha
    ttl_valid = now < valid_until

    if retry_count >= MAX_VERIFY_RETRIES:
        _send_critical(
            "Действие не подтвердилось после 3 повторов",
            f"proposal {proposal_id}, claim {claim_id}: заявлено {kind}, "
            f"в FB — {json.dumps(dict(observed), ensure_ascii=False)}. "
            "Автоматика остановлена, нужна ручная проверка в Facebook.",
        )
        return "ESCALATED", retry_count, "RETRY_BUDGET_EXHAUSTED", False, True, None

    if state_unchanged and ttl_valid:
        # (а) состояние цели то же, что одобрял владелец, (б) TTL жив,
        # (в) попыток меньше трёх → повтор в рамках того же одобрения.
        retry_no = retry_count + 1
        try:
            reason = _retry_same_approval(proposal_id, now=now)
        except Exception as exc:  # noqa: BLE001 — расхождение остаётся видимым
            logger.warning(
                "action_verifier: повтор исполнения %s не удался — %s",
                proposal_id,
                exc,
            )
            reason = f"RETRY_FAILED_{type(exc).__name__}"
        return "PENDING", retry_no, f"RETRY_SAME_APPROVAL:{reason}"[:120], True, False, None

    reason_code = (
        "APPROVAL_TTL_EXPIRED" if not ttl_valid else "STATE_CHANGED_SINCE_APPROVAL"
    )
    repeat_proposal_id = _create_repeat_proposal(
        kind=kind,
        proposal_id=proposal_id,
        claim_id=claim_id,
        subject_id=subject_id,
        adset_id=adset_id,
        payload=payload,
        observed=observed,
        now=now,
    )
    _send_critical(
        "Действие не выполнено в Facebook",
        f"proposal {proposal_id}, claim {claim_id}: заявлено {kind} "
        f"({json.dumps(dict(payload), ensure_ascii=False)}), "
        f"в FB — {json.dumps(dict(observed), ensure_ascii=False)}. "
        f"Причина запрета повтора: {reason_code}. "
        + (
            f"Создано новое предложение «повтор невыполненного действия»: {repeat_proposal_id}."
            if repeat_proposal_id
            else "Новое предложение создать не удалось — нужна ручная проверка."
        ),
    )
    return "ESCALATED", retry_count, reason_code, False, True, repeat_proposal_id


# --------------------------------------------------------------------------
# Основной проход.
# --------------------------------------------------------------------------


def _due_rows(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    limit: int,
) -> list[sqlite3.Row]:
    since = now - timedelta(hours=VERIFY_WINDOW_HOURS)
    placeholders = ",".join("?" for _ in VERIFIABLE_KINDS)
    return connection.execute(
        f"""
        SELECT a.proposal_id, a.claim_id, a.attempt_id, a.completed_at,
               p.proposal_kind, p.valid_until,
               t.subject_id, t.adset_id, t.intended_payload_json,
               l.state AS lifecycle_state, l.version AS lifecycle_version,
               s.state AS verify_state, s.retry_count, s.first_seen_at
        FROM owner_action_attempts a
        JOIN owner_action_proposals p USING (proposal_id)
        JOIN owner_action_lifecycle l ON l.proposal_id = a.proposal_id
        JOIN owner_action_proposal_targets t
          ON t.proposal_id = a.proposal_id AND t.claim_id = a.claim_id
        LEFT JOIN action_verification_state s
          ON s.proposal_id = a.proposal_id AND s.claim_id = a.claim_id
        WHERE a.state = 'CONFIRMED'
          AND a.completed_at IS NOT NULL
          AND a.completed_at >= ?
          AND p.proposal_kind IN ({placeholders})
          AND (s.state IS NULL OR s.state = 'PENDING')
        ORDER BY a.completed_at, a.claim_id
        LIMIT ?
        """,
        (_iso(since), *VERIFIABLE_KINDS, limit),
    ).fetchall()


def _mark_lifecycle_verified(
    db_path: str | Path,
    proposal_id: str,
    *,
    lifecycle_state: str,
    lifecycle_version: int,
    now: datetime,
) -> None:
    """EXECUTED → VERIFYING → VERIFIED. Для LAUNCH lifecycle ведёт watchdog."""

    from services.owner_action_repository import (
        OwnerActionLifecycleConflict,
        OwnerActionRepository,
    )

    repository = OwnerActionRepository(db_path)
    version = lifecycle_version
    state = lifecycle_state
    try:
        if state == LifecycleState.EXECUTED.value:
            version = repository.transition_lifecycle(
                proposal_id,
                expected_state=LifecycleState.EXECUTED.value,
                expected_version=version,
                new_state=LifecycleState.VERIFYING.value,
                actor="action_verifier",
                reason_code="INDEPENDENT_VERIFICATION_STARTED",
                now=now,
            )
            state = LifecycleState.VERIFYING.value
        if state == LifecycleState.VERIFYING.value:
            repository.transition_lifecycle(
                proposal_id,
                expected_state=LifecycleState.VERIFYING.value,
                expected_version=version,
                new_state=LifecycleState.VERIFIED.value,
                actor="action_verifier",
                reason_code="LIVE_STATE_MATCHES",
                now=now,
            )
    except OwnerActionLifecycleConflict as exc:
        # Параллельный worker уже продвинул lifecycle — вердикт от этого не врёт.
        logger.info(
            "action_verifier: lifecycle %s не продвинут (%s)",
            proposal_id,
            exc,
        )


def verify_executed_actions(
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 50,
    db_path: str | Path | None = None,
) -> VerifierRun:
    """Один проход верификатора по исполнениям последних 24 часов."""

    checked_at = _require_aware(now or datetime.now(timezone.utc), "now")
    if not worker_id.strip():
        raise ValueError("worker_id обязателен")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("limit должен быть в диапазоне 1..200")
    path = Path(db_path) if db_path is not None else _owner_db_path()

    connection = _connect(path)
    checks: list[VerificationCheck] = []
    errors: list[str] = []
    try:
        rows = _due_rows(connection, now=checked_at, limit=limit)
        for row in rows:
            proposal_id = str(row["proposal_id"])
            claim_id = str(row["claim_id"])
            kind = str(row["proposal_kind"])
            try:
                check = _verify_one(
                    connection,
                    row=row,
                    db_path=path,
                    now=checked_at,
                )
            except Exception as exc:  # noqa: BLE001 — одна цель не роняет проход
                errors.append(f"{proposal_id}:{claim_id}:{type(exc).__name__}")
                logger.warning(
                    "action_verifier: проверка %s/%s (%s) упала — %s",
                    proposal_id,
                    claim_id,
                    kind,
                    exc,
                )
                continue
            checks.append(check)
    finally:
        connection.close()

    return VerifierRun(
        checked=len(checks),
        verified=sum(check.verdict == _VERDICT_VERIFIED for check in checks),
        mismatch=sum(check.verdict == _VERDICT_MISMATCH for check in checks),
        unverifiable=sum(check.verdict == _VERDICT_UNVERIFIABLE for check in checks),
        retried=sum(check.retried for check in checks),
        escalated=sum(check.escalated for check in checks),
        errors=tuple(errors),
        checks=tuple(checks),
    )


def _verify_one(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    db_path: Path,
    now: datetime,
) -> VerificationCheck:
    proposal_id = str(row["proposal_id"])
    claim_id = str(row["claim_id"])
    kind = str(row["proposal_kind"])
    subject_id = str(row["subject_id"])
    adset_id = None if row["adset_id"] is None else str(row["adset_id"])
    payload = json.loads(str(row["intended_payload_json"]))
    if not isinstance(payload, dict):  # pragma: no cover — CHECK json_valid
        raise ActionVerifierError("INTENDED_PAYLOAD_INVALID")
    retry_count = 0 if row["retry_count"] is None else int(row["retry_count"])
    first_seen_at = (
        now
        if row["first_seen_at"] is None
        else datetime.fromisoformat(str(row["first_seen_at"]))
    )
    valid_until = datetime.fromisoformat(str(row["valid_until"]))

    if kind == "LAUNCH":
        verdict, reason_code, expected, observed, approved_sha, live_sha = _verify_launch(
            connection,
            proposal_id=proposal_id,
        )
    elif kind in {"PAUSE", "UNPAUSE"}:
        verdict, reason_code, expected, observed, approved_sha, live_sha = _verify_pause_like(
            kind=kind,
            payload=payload,
            subject_id=subject_id,
        )
    elif kind == "SCALE":
        verdict, reason_code, expected, observed, approved_sha, live_sha = _verify_scale(
            payload=payload,
            adset_id=adset_id or subject_id,
        )
    else:  # pragma: no cover — выборка отфильтрована по VERIFIABLE_KINDS
        raise ActionVerifierError(f"KIND_NOT_VERIFIABLE_{kind}")

    retried = escalated = False
    retry_proposal_id: str | None = None
    retry_no = retry_count
    if verdict == _VERDICT_VERIFIED:
        terminal_state = "VERIFIED"
    elif verdict == _VERDICT_UNVERIFIABLE:
        # Честно: FB недоступен — проверка НЕ завершена, повторим следующим тиком.
        terminal_state = "PENDING"
    else:
        (
            terminal_state,
            retry_no,
            reason_code,
            retried,
            escalated,
            retry_proposal_id,
        ) = _handle_mismatch(
            connection,
            db_path=db_path,
            proposal_id=proposal_id,
            claim_id=claim_id,
            kind=kind,
            subject_id=subject_id,
            adset_id=adset_id,
            payload=payload,
            observed=observed,
            approved_sha=approved_sha,
            live_sha=live_sha,
            valid_until=valid_until,
            retry_count=retry_count,
            now=now,
        )

    check_seq = _persist_check(
        connection,
        proposal_id=proposal_id,
        claim_id=claim_id,
        attempt_id=None if row["attempt_id"] is None else str(row["attempt_id"]),
        kind=kind,
        verdict=verdict,
        reason_code=reason_code,
        expected=expected,
        observed=observed,
        retry_no=retry_no,
        terminal_state=terminal_state,
        retry_proposal_id=retry_proposal_id,
        first_seen_at=first_seen_at,
        now=now,
    )

    if verdict == _VERDICT_VERIFIED and kind != "LAUNCH":
        _mark_lifecycle_verified(
            db_path,
            proposal_id,
            lifecycle_state=str(row["lifecycle_state"]),
            lifecycle_version=int(row["lifecycle_version"]),
            now=now,
        )

    if verdict != _VERDICT_UNVERIFIABLE:
        _enqueue_verdict_message(
            db_path,
            proposal_id=proposal_id,
            claim_id=claim_id,
            check_seq=check_seq,
            text=_verdict_text(kind, verdict, reason_code, observed),
            now=now,
        )

    return VerificationCheck(
        proposal_id=proposal_id,
        claim_id=claim_id,
        kind=kind,
        verdict=verdict,
        reason_code=reason_code,
        retry_no=retry_no,
        expected=expected,
        observed=observed,
        terminal_state=terminal_state,
        retried=retried,
        escalated=escalated,
        retry_proposal_id=retry_proposal_id,
    )


# --------------------------------------------------------------------------
# Reconciliation неизвестных исходов (RECONCILE_REQUIRED).
#
# Миграция 026 разрешает RECONCILE_REQUIRED → {VERIFYING, VERIFIED,
# FAILED_NO_EFFECT}, но исполнителя у перехода не было: задания с
# PROVIDER_OUTCOME_UNKNOWN висели навсегда и добивались руками. Здесь неизвестность разрешается ФАКТАМИ провайдера:
# эффект есть → VERIFIED, эффекта нет → FAILED_NO_EFFECT (штатный продюсер
# предложит действие заново полным циклом одобрения), недоступно → честно
# остаётся до следующего прогона.
# --------------------------------------------------------------------------

RECONCILE_WINDOW_DAYS = 14


@dataclass(frozen=True)
class ReconcileRun:
    """Итог одного прохода сверки неизвестных исходов."""

    checked: int
    confirmed: int
    no_effect: int
    unverifiable: int
    errors: tuple[str, ...]


def _reconcile_due_rows(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    limit: int,
) -> list[sqlite3.Row]:
    since = now - timedelta(days=RECONCILE_WINDOW_DAYS)
    return connection.execute(
        """
        SELECT l.proposal_id, l.version AS lifecycle_version,
               l.active_job_id,
               p.proposal_kind, p.plan_json, p.idempotency_key,
               t.claim_id, t.subject_id, t.adset_id, t.intended_payload_json
        FROM owner_action_lifecycle l
        JOIN owner_action_proposals p USING (proposal_id)
        JOIN owner_action_proposal_targets t ON t.proposal_id = l.proposal_id
        WHERE l.state = 'RECONCILE_REQUIRED'
          AND l.updated_at >= ?
        GROUP BY l.proposal_id
        ORDER BY l.updated_at
        LIMIT ?
        """,
        (_iso(since), limit),
    ).fetchall()


def _launch_account_context(account_id: str) -> str | None:
    """Thread-контекст кабинета назначения: online или оффлайн-карта."""

    import config
    from services.fb_token_provider import offline_account_context

    normalized = str(account_id or "").removeprefix("act_").strip()
    online = str(getattr(config, "FB_ACCOUNT_ID_ONLINE", "") or "").removeprefix("act_")
    if online and normalized == online:
        return "online"
    return offline_account_context(normalized)


def _fetch_adset_ad_names(adset_id: str, account_id: str | None) -> set[str] | None:
    """Имена неархивных объявлений адсета; None — полнота выборки не доказана."""

    ads = _fetch_adset_ads(adset_id, account_id)
    return None if ads is None else set(ads)


def _fetch_adset_ads(adset_id: str, account_id: str | None) -> dict[str, str] | None:
    """{имя: ad_id} неархивных объявлений адсета; None — полнота выборки не доказана.

    Полная cursor-пагинация обязательна: одна страница limit=200 с фильтром
    ARCHIVED после выборки вытесняла живые объявления за границу страницы на
    адсетах с хвостом архивных — и давала ложный «эффекта нет».
    """

    from agent.fb_common import API, _throttled_get
    from services.fb_token_provider import fb_account, get_fb_token

    try:
        context = _launch_account_context(account_id) if account_id else None
    except Exception:  # noqa: BLE001 — незарегистрированный кабинет: не читаем чужое
        return None
    rows: list[dict] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    try:
        with fb_account(context):
            for _page in range(20):
                params: dict = {
                    "access_token": get_fb_token(),
                    "fields": "id,name,status",
                    "limit": 200,
                }
                if cursor:
                    params["after"] = cursor
                response = _throttled_get(f"{API}/{adset_id}/ads", params=params)
                if response.status_code != 200:
                    return None
                payload = response.json()
                data = payload.get("data")
                if not isinstance(data, list):
                    return None
                rows.extend(item for item in data if isinstance(item, dict))
                paging = payload.get("paging") or {}
                if "next" not in paging:
                    return {
                        str(item.get("name") or ""): str(item.get("id") or "")
                        for item in rows
                        if str(item.get("status") or "") != "ARCHIVED"
                    }
                cursor = (paging.get("cursors") or {}).get("after")
                if not cursor or cursor in seen_cursors:
                    return None
                seen_cursors.add(cursor)
    except Exception:  # noqa: BLE001 — сеть/токен: честное «не доказано»
        return None
    return None


def _executed_claim_ids(path: Path, proposal_id: str) -> frozenset[str]:
    """Claim'ы с НЕИЗВЕСТНЫМ исходом (attempt в RECONCILE_REQUIRED) — только их и сверяем.

    Раньше брались все attempts: подтверждённые имена вживую плюс одно пропавшее
    давали вечный LAUNCH_PARTIAL_NAMES_LIVE (в логах reconcile — сплошь unverifiable).
    CONFIRMED уже доказан провайдером, FAILED_NO_EFFECT доказанно без эффекта.
    """

    connection = _connect(path)
    try:
        rows = connection.execute(
            "SELECT DISTINCT claim_id FROM owner_action_attempts "
            "WHERE proposal_id = ? AND state = 'RECONCILE_REQUIRED'",
            (proposal_id,),
        ).fetchall()
    finally:
        connection.close()
    return frozenset(str(row["claim_id"]) for row in rows)


def _reconcile_launch_by_names(
    plan_json: str,
    *,
    executed_claim_ids: frozenset[str] = frozenset(),
) -> tuple[str, str, dict[str, object]]:
    """LAUNCH-сверка по фактам кабинета: exact-имена ИСПОЛНЕННЫХ claim'ов.

    Watchdog для RECONCILE-заданий чаще всего не заведён (мутация упала до
    постусловия), поэтому сверяем напрямую. Три источника ложных вердиктов
    починены здесь:
    * сверялись имена ВСЕХ targets, хотя исполнялась часть, — вечный
      LAUNCH_PARTIAL_NAMES_LIVE у любого частичного прогона;
    * выборка без пагинации → ложный «эффекта нет» → повторный запуск → дубли;
    * чтение без account-контекста → адсеты второго кабинета недостижимы.
    FAILED_NO_EFFECT выносится ТОЛЬКО при доказанно полной выборке.
    """

    plan = json.loads(plan_json)
    wanted: dict[str, set[str]] = {}
    executed: dict[str, set[str]] = {}
    accounts: dict[str, str] = {}
    for target in plan.get("targets") or []:
        claim_id = str(target.get("claim_id") or "")
        payload = target.get("intended_payload") or {}
        for destination in payload.get("destinations") or []:
            adset_id = str(destination.get("adset_id") or "")
            destination_account = str(destination.get("account_id") or "")
            for creative in destination.get("creatives") or []:
                name = str(creative.get("ad_name") or "")
                if not adset_id or not name:
                    continue
                wanted.setdefault(adset_id, set()).add(name)
                if destination_account:
                    accounts.setdefault(adset_id, destination_account)
                if claim_id and claim_id in executed_claim_ids:
                    executed.setdefault(adset_id, set()).add(name)
    if not wanted:
        return _VERDICT_UNVERIFIABLE, "LAUNCH_PLAN_EMPTY", {}
    # Attempts нет вообще → доказательств CREATE нет, сверяем весь план
    # (прежняя семантика): полное отсутствие имён здесь — честный NO_EFFECT.
    check = executed if executed else wanted
    found = missing = 0
    observed: dict[str, object] = {}
    for adset_id, names in check.items():
        live = _fetch_adset_ad_names(adset_id, accounts.get(adset_id))
        if live is None:
            return _VERDICT_UNVERIFIABLE, "FB_ADSET_UNAVAILABLE", {"adset_id": adset_id}
        hit = names & live
        found += len(hit)
        missing += len(names - live)
        observed[adset_id] = {"expected": len(names), "live": len(hit)}
    checked_names = sum(len(names) for names in check.values())
    total_names = sum(len(names) for names in wanted.values())
    if missing == 0:
        if checked_names < total_names:
            # Исполненная часть подтверждена целиком; неисполненные claim'ы
            # не считаются «пропавшими» — их отсутствие в кабинете ожидаемо.
            observed["unexecuted_names"] = total_names - checked_names
            return _VERDICT_VERIFIED, "LAUNCH_EXECUTED_NAMES_LIVE", observed
        return _VERDICT_VERIFIED, "LAUNCH_ALL_NAMES_LIVE", observed
    if found == 0:
        return _VERDICT_MISMATCH, "LAUNCH_NO_NAMES_LIVE", observed
    # Частичное создание среди неизвестных claim'ов: живая часть — факт, пропавшая
    # часть записывается в попытку auto_launch по городам (см. _record_launch_reconcile),
    # и дозапуск идёт только по ней — дублей живой части не будет. Раньше это был
    # вечный UNVERIFIABLE.
    observed["missing_names"] = missing
    return _VERDICT_VERIFIED, "LAUNCH_PARTIAL_NAMES_LIVE", observed


def _record_launch_reconcile(
    plan_json: str, idempotency_key: str, *, executed_claim_ids: frozenset[str]
) -> None:
    """Пишет вердикт сверки в попытку auto_launch по городам (обратная синхронизация машин состояний).

    Город, все имена которого живы, получает ad_id (успех); город с доказанно пропавшими именами —
    ошибку и остаётся pending для дозапуска. Любой сбой здесь не меняет вердикт lifecycle.
    """

    from services.auto_launch import _find_gateway_attempt, _record_city_failure, _record_city_success

    if not idempotency_key:
        return
    key, _attempt = _find_gateway_attempt(idempotency_key)
    plan = json.loads(plan_json)
    by_city: dict[str, dict[str, object]] = {}
    for target in plan.get("targets") or []:
        claim_id = str(target.get("claim_id") or "")
        if executed_claim_ids and claim_id not in executed_claim_ids:
            continue
        for destination in (target.get("intended_payload") or {}).get("destinations") or []:
            city = str(destination.get("city") or "")
            adset_id = str(destination.get("adset_id") or "")
            if not city or not adset_id:
                continue
            entry = by_city.setdefault(
                city,
                {"adset_id": adset_id, "account_id": str(destination.get("account_id") or ""), "names": set()},
            )
            entry["names"].update(
                str(c.get("ad_name") or "") for c in destination.get("creatives") or [] if c.get("ad_name")
            )
    for city, entry in by_city.items():
        live = _fetch_adset_ads(str(entry["adset_id"]), str(entry["account_id"]) or None)
        if live is None:
            continue
        names = entry["names"]
        ids = [live[name] for name in sorted(names) if name in live and live[name]]
        if ids and len(ids) == len(names):
            _record_city_success(key, city, ids)
        elif not ids:
            _record_city_failure(key, city, "reconcile: объявления не найдены, город ждёт дозапуска")
        else:
            _record_city_failure(
                key, city, f"reconcile: живы {len(ids)} из {len(names)} объявлений, недостающие ждут дозапуска"
            )


def reconcile_unknown_outcomes(
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 10,
    db_path: str | Path | None = None,
) -> ReconcileRun:
    """Один проход по lifecycle=RECONCILE_REQUIRED: закрыть фактами провайдера."""

    from services.owner_action_repository import OwnerActionRepository

    checked_at = _require_aware(now or datetime.now(timezone.utc), "now")
    if not worker_id.strip():
        raise ValueError("worker_id обязателен")
    path = Path(db_path) if db_path is not None else _owner_db_path()

    confirmed = no_effect = unverifiable = 0
    errors: list[str] = []
    connection = _connect(path)
    try:
        rows = _reconcile_due_rows(connection, now=checked_at, limit=limit)
    finally:
        connection.close()

    repository = OwnerActionRepository(path)
    for row in rows:
        proposal_id = str(row["proposal_id"])
        kind = str(row["proposal_kind"])
        try:
            if kind == "LAUNCH":
                unknown_claims = _executed_claim_ids(path, proposal_id)
                verdict, reason_code, observed = _reconcile_launch_by_names(
                    str(row["plan_json"]),
                    executed_claim_ids=unknown_claims,
                )
                if verdict in {_VERDICT_VERIFIED, _VERDICT_MISMATCH}:
                    try:
                        _record_launch_reconcile(
                            str(row["plan_json"]),
                            str(row["idempotency_key"] or ""),
                            executed_claim_ids=unknown_claims,
                        )
                    except Exception as exc:  # noqa: BLE001 — журнал попытки не меняет вердикт
                        logger.warning(
                            "action_verifier: вердикт сверки %s не записан в попытку — %s", proposal_id, exc
                        )
            elif kind in {"PAUSE", "UNPAUSE"}:
                payload = json.loads(str(row["intended_payload_json"]))
                verdict, reason_code, _exp, observed, _a, _l = _verify_pause_like(
                    kind=kind, payload=payload, subject_id=str(row["subject_id"])
                )
            elif kind == "SCALE":
                payload = json.loads(str(row["intended_payload_json"]))
                verdict, reason_code, _exp, observed, _a, _l = _verify_scale(
                    payload=payload,
                    adset_id=str(row["adset_id"] or row["subject_id"]),
                )
            else:
                unverifiable += 1
                continue

            if verdict == _VERDICT_VERIFIED:
                new_state = "VERIFIED"
                job_state = "COMPLETE"
                confirmed += 1
            elif verdict == _VERDICT_MISMATCH:
                new_state = "FAILED_NO_EFFECT"
                job_state = "FAILED_NO_EFFECT"
                no_effect += 1
            else:
                unverifiable += 1
                continue

            repository.transition_lifecycle(
                proposal_id,
                expected_state="RECONCILE_REQUIRED",
                expected_version=int(row["lifecycle_version"]),
                new_state=new_state,
                actor=f"action-verifier:reconcile:{worker_id}",
                reason_code=f"RECONCILED_{reason_code}"[:64],
                now=checked_at,
            )
            job_id = row["active_job_id"]
            if job_id:
                job_connection = _connect(path)
                try:
                    job_connection.execute("BEGIN IMMEDIATE")
                    job_connection.execute(
                        """
                        UPDATE owner_execution_jobs
                        SET state = ?, last_reason_code = ?, updated_at = ?
                        WHERE job_id = ? AND state = 'RECONCILE_REQUIRED'
                        """,
                        (job_state, f"RECONCILED_{reason_code}"[:64], _iso(checked_at), str(job_id)),
                    )
                    _append_event(
                        job_connection,
                        proposal_id=proposal_id,
                        event_type="RECONCILED",
                        reason_code=f"RECONCILED_{reason_code}"[:64],
                        payload={"verdict": verdict, "observed": observed, "kind": kind},
                        now=checked_at,
                    )
                    job_connection.commit()
                finally:
                    job_connection.close()
            logger.info(
                "action_verifier: reconcile %s (%s) — %s (%s)",
                proposal_id,
                kind,
                verdict,
                reason_code,
            )
        except Exception as exc:  # noqa: BLE001 — одна цель не роняет проход
            errors.append(f"{proposal_id}:{type(exc).__name__}")
            logger.warning(
                "action_verifier: reconcile %s (%s) упал — %s", proposal_id, kind, exc
            )

    return ReconcileRun(
        checked=len(rows),
        confirmed=confirmed,
        no_effect=no_effect,
        unverifiable=unverifiable,
        errors=tuple(errors),
    )
