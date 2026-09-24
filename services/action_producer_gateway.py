"""Единая proposal-only граница PAUSE/UNPAUSE/SCALE/RECOVERY producers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

from services.adset_pause_guard import (
    fetch_exact_ad_contexts,
    fetch_pause_inventory,
)
from services.owner_action_models import (
    EvidenceRecord,
    ProposalKind,
    ProposalOrigin,
    ProposalReceipt,
    ProposedActionPlan,
    ProposedTarget,
    canonical_sha256,
)
from services.owner_proposal_card import (
    DecisionContext,
    render_pause_card,
    render_recovery_card,
    render_scale_card,
    render_unpause_card,
)


# 48 часов, а не 24: дайджест уходит раз в сутки, и предложение, созданное
# сразу после отправки пакета, при суточном TTL умирало бы, не доехав до
# владельца. Свипер протухших рассчитан на любое значение. Константа публичная:
# ей обязаны пользоваться ВСЕ продюсеры предложений (в том числе launcher) —
# свой укороченный TTL уже приводил к пачке PROPOSAL_EXPIRED по запускам.
PROPOSAL_TTL = timedelta(hours=48)
# Старое имя — для тестов и внешних читателей.
_PROPOSAL_TTL = PROPOSAL_TTL
_PRODUCER_SCHEMA_VERSION = 1


class ProducerActionError(RuntimeError):
    """Read-only discovery не позволяет безопасно создать proposal."""


class ProducerIdempotencyConflict(ProducerActionError):
    """Один idempotency scope уже связан с другим payload."""


@dataclass(frozen=True, slots=True)
class ProducerActionOutcome:
    """Совместимый результат producer: он никогда не означает execution success."""

    action: str
    receipt: ProposalReceipt | None = None
    run: None = None
    workflow_id: str | None = None
    reason: str | None = None

    @property
    def confirmed(self) -> bool:
        return False

    @property
    def proposal_id(self) -> str | None:
        return None if self.receipt is None else self.receipt.proposal_id


def require_uuid4(value: str) -> str:
    """Принимает только canonical lowercase UUID4."""
    if not isinstance(value, str):
        raise ValueError("Idempotency-Key должен быть UUID4")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError("Idempotency-Key должен быть UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("Idempotency-Key должен быть canonical UUID4")
    return value


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


def _payload_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def reserve_idempotency(
    scope: str,
    payload: Mapping[str, object],
    key: str | None = None,
) -> str:
    """Сохраняет legacy UUID binding без выдачи разрешения на mutation."""
    if not isinstance(scope, str) or not scope:
        raise ValueError("scope обязателен")
    requested = require_uuid4(key) if key is not None else None
    digest = _payload_sha256(payload)
    from agent.database import _get_connection

    resolved = requested or new_idempotency_key()
    connection = _get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        by_scope = connection.execute(
            """
            SELECT idempotency_key, command_payload_sha256
            FROM action_producer_commands
            WHERE scope = ?
            """,
            (scope,),
        ).fetchone()
        by_key = connection.execute(
            """
            SELECT scope, command_payload_sha256
            FROM action_producer_commands
            WHERE idempotency_key = ?
            """,
            (resolved,),
        ).fetchone()
        if by_scope is not None:
            if by_scope["command_payload_sha256"] != digest:
                raise ProducerIdempotencyConflict("IDEMPOTENCY_PAYLOAD_CONFLICT")
            if requested is not None and by_scope["idempotency_key"] != requested:
                raise ProducerIdempotencyConflict("IDEMPOTENCY_KEY_CONFLICT")
            connection.commit()
            return require_uuid4(str(by_scope["idempotency_key"]))
        if by_key is not None and (
            by_key["scope"] != scope
            or by_key["command_payload_sha256"] != digest
        ):
            raise ProducerIdempotencyConflict("IDEMPOTENCY_KEY_CONFLICT")
        connection.execute(
            """
            INSERT INTO action_producer_commands(
                idempotency_key, scope, command_payload_sha256
            ) VALUES (?, ?, ?)
            """,
            (resolved, scope, digest),
        )
        connection.commit()
        return resolved
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise ProducerIdempotencyConflict("IDEMPOTENCY_CONFLICT") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _aware_utc(now: datetime | None) -> datetime:
    resolved = now or datetime.now(timezone.utc)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ValueError("now должен содержать timezone")
    return resolved.astimezone(timezone.utc)


def _proposal_origin(origin: object) -> ProposalOrigin:
    if isinstance(origin, ProposalOrigin):
        return origin
    value = str(getattr(origin, "value", origin))
    if value == "WEB":
        return ProposalOrigin.WEB
    if value == "TELEGRAM":
        return ProposalOrigin.TELEGRAM_COMMAND
    if value in {"REPLACEMENT", "LAUNCH_RECOVERY", "ASSET_RECOVERY"}:
        return ProposalOrigin.RECOVERY
    if value.startswith("AUTOPILOT"):
        return ProposalOrigin.AUTOPILOT
    return ProposalOrigin.CRON


def _proposal_key(
    scope: str,
    payload: Mapping[str, object],
    requested: str | None,
) -> str:
    """Durable binding scope → UUID4: ключ всегда UUID4, scope живёт отдельно.

    Детерминированность идемпотентности даёт не сам ключ, а immutable таблица
    ``action_producer_commands``: тот же scope с тем же intent всегда отдаёт тот
    же UUID4, а тот же scope с другим intent — честный конфликт, а не второй
    proposal. Раньше здесь возвращалась scope-строка ``producer:<sha256>``, и
    execution-манифесты (``LaunchManifest``/``AssetRecoveryManifest``) ломались
    на ней с «idempotency_key должен быть UUID4».
    """
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("scope обязателен")
    # canonical_sha256 понимает Decimal/datetime/Enum, обычный json.dumps — нет.
    return reserve_idempotency(
        scope,
        {"intent_sha256": canonical_sha256(payload)},
        requested,
    )


def _claim_id(idempotency_key: str, ordinal: int, payload_sha256: str) -> str:
    """Глобально уникальный, но детерминированный claim_id одного target.

    ``owner_action_proposal_targets.claim_id`` уникален по всей таблице, поэтому
    хэша одного payload недостаточно: два разных proposal с одинаковым payload
    (другой день, другая попытка) роняли INSERT по UNIQUE. Ключ идемпотентности
    уникален на proposal, значит связка (ключ, ordinal, payload) уникальна
    глобально и при этом воспроизводима — повтор того же действия не плодит
    дубли, он отсекается ещё дедупликацией по idempotency_key.
    """
    return "producer-" + canonical_sha256(
        {
            "idempotency_key": idempotency_key,
            "ordinal": ordinal,
            "intended_payload_sha256": payload_sha256,
        }
    )[:32]


def _scope_labels(adset_id: str) -> tuple[str | None, str | None]:
    """Город и язык адсета для карточки предложения — по живому каталогу FB.

    Раньше читалось напрямую из config.ADSETS. Карта статична: владелец
    пересоздаёт адсеты, новый ID в ней не появляется, и карточка молча теряет
    город (а после переиспользования городом другого адсета — показала бы
    чужой). agent.adset_discovery ходит в кабинет, кеширует на 30 минут и сам
    падает обратно на config.ADSETS, когда FB недоступен, поэтому офлайн
    поведение остаётся прежним.

    ``old`` — предыдущие адсеты той же группы: по ним предложения тоже должны
    подписываться городом, иначе после ротации вся история становится безымянной.
    """
    from config import ADSETS

    matches: set[tuple[str, str]] = set()
    try:
        from agent.adset_discovery import discover_adsets

        discovered = discover_adsets()
        live = discovered.get("leadgen") or {}
        previous = discovered.get("old") or {}
    except Exception:  # noqa: BLE001 — карточка не должна падать из-за FB
        live, previous = {}, {}

    for city, languages in (*live.items(), *previous.items()):
        if not isinstance(languages, Mapping):
            continue
        for language, ids in languages.items():
            if language not in ("L2", "L1"):
                continue
            candidates = ids if isinstance(ids, (list, tuple)) else (ids,)
            if any(str(candidate) == adset_id for candidate in candidates):
                matches.add((str(city), str(language)))

    if not matches:
        matches = {
            (str(city), str(language))
            for city, languages in ADSETS.items()
            for language, configured_adset_id in languages.items()
            if str(configured_adset_id) == adset_id
        }
    if len(matches) != 1:
        return None, None
    return next(iter(matches))


def _account_id(context: Mapping[str, object] | None = None) -> str:
    from config import FB_ACCOUNT_ID

    raw = None if context is None else context.get("account_id")
    account_id = str(raw or FB_ACCOUNT_ID or "").removeprefix("act_")
    if not account_id:
        raise ProducerActionError("ACCOUNT_ID_MISSING")
    return account_id


def _live_duplicate(
    *,
    subject_id: str,
    action_kind: str,
    now: datetime | None = None,
) -> ProducerActionOutcome | None:
    """Уже висящее у владельца предложение того же действия по тому же объекту.

    Без этой проверки каждый прогон крона рождал НОВОЕ предложение: scope
    producer'а содержит run_id прогона, поэтому idempotency-дедуп по ключу тут
    бессилен (стабилизировать scope нельзя — payload с живым inventory меняется
    между прогонами и даёт ``IDEMPOTENCY_PAYLOAD_CONFLICT``). Так на ~30 реклам
    накопилось 96 предложений. Дедуп в дайджесте эту очередь не чистит:
    проигравшие остаются живыми и всплывают на следующий день.

    Сбой чтения не блокирует producer: лучше лишнее предложение, чем молча
    потерянное.
    """
    from services.owner_action_repository import find_live_proposal_for_subject

    try:
        existing = find_live_proposal_for_subject(
            subject_id=subject_id,
            action_kind=action_kind,
            now=now,
        )
    except Exception:  # noqa: BLE001 — дедуп не имеет права ронять producer
        return None
    if existing is None:
        return None
    return ProducerActionOutcome(
        action="PROPOSAL_CREATED",
        receipt=existing,
        reason="LIVE_PROPOSAL_EXISTS",
    )


def _evidence(
    *,
    kind: str,
    source: str,
    subject_id: str,
    observed_at: datetime,
    payload: Mapping[str, object],
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_kind=kind,
        source_system=source,
        subject_id=subject_id,
        observed_at=observed_at,
        complete=True,
        payload=payload,
        payload_sha256=canonical_sha256(payload),
    )


def _persist_proposal(
    *,
    proposal_kind: ProposalKind,
    origin: ProposalOrigin,
    idempotency_key: str,
    source_ref: str,
    actor: str,
    summary: str,
    account_id: str,
    adset_id: str | None,
    subject_id: str,
    city: str | None,
    language: str | None,
    intended_payload: Mapping[str, object],
    evidence: tuple[EvidenceRecord, ...],
    now: datetime,
) -> ProducerActionOutcome:
    from services.owner_action_models import proposal_hashes
    from services.owner_action_repository import (
        OwnerActionIdempotencyConflict,
        find_receipt_by_idempotency_key,
        propose_action,
    )

    action_kind = {
        ProposalKind.PAUSE: "PAUSE_AD",
        ProposalKind.UNPAUSE: "UNPAUSE_AD",
        ProposalKind.SCALE: "SET_ADSET_BUDGET",
        ProposalKind.ASSET_RECOVERY: "RECOVER_AD",
    }[proposal_kind]
    payload_hash = canonical_sha256(intended_payload)
    plan = ProposedActionPlan(
        proposal_kind=proposal_kind,
        origin=origin,
        idempotency_key=idempotency_key,
        source_ref=source_ref,
        actor=actor,
        summary=summary,
        targets=(
            ProposedTarget(
                claim_id=_claim_id(idempotency_key, 0, payload_hash),
                ordinal=0,
                action_kind=action_kind,
                account_id=account_id,
                adset_id=adset_id,
                subject_id=subject_id,
                city=city,
                language=language,  # type: ignore[arg-type]
                intended_payload=intended_payload,
                intended_payload_sha256=payload_hash,
            ),
        ),
        evidence=evidence,
        config_version_sha256=canonical_sha256(
            {
                "producer_schema_version": _PRODUCER_SCHEMA_VERSION,
                "proposal_kind": proposal_kind.value,
            }
        ),
        valid_until=now + _PROPOSAL_TTL,
        staged_media_root=None,
    )
    try:
        receipt = propose_action(plan, now=now)
    except OwnerActionIdempotencyConflict:
        # Повтор ТОГО ЖЕ действия расходится с сохранённым планом только по
        # времени наблюдения (observed_at/valid_until) и по метрикам evidence.
        # Это idempotency-hit, а не крэш: отдаём уже созданный proposal.
        existing = find_receipt_by_idempotency_key(
            idempotency_key,
            targets_sha256=proposal_hashes(plan).targets_sha256,
        )
        if existing is None:
            raise
        return ProducerActionOutcome(action="PROPOSAL_CREATED", receipt=existing)
    return ProducerActionOutcome(action="PROPOSAL_CREATED", receipt=receipt)


def propose_pause(
    ad_id: str,
    *,
    origin: object,
    scope: str,
    reason_code: str,
    idempotency_key: str | None = None,
    decision: DecisionContext | None = None,
    now: datetime | None = None,
) -> ProducerActionOutcome:
    """Создаёт PAUSE proposal; последняя effective ACTIVE блокируется сразу.

    ``decision`` — цифры, на которых бот принял решение (расход, лиды, CPL,
    квалы, оплаты, бизнес-причина). Они попадают в карточку владельца и НЕ
    участвуют в ``intended_payload``, поэтому ключ идемпотентности от них не
    зависит: повтор того же действия остаётся дедупликацией, а не конфликтом.
    """
    prepared_at = _aware_utc(now)
    duplicate = _live_duplicate(
        subject_id=ad_id, action_kind="PAUSE_AD", now=prepared_at
    )
    if duplicate is not None:
        return duplicate
    exact, exact_error = fetch_exact_ad_contexts([ad_id], require_names=True)
    context = exact.get(ad_id)
    if exact_error is not None or context is None:
        raise ProducerActionError(exact_error or "PAUSE_TARGET_MISSING")
    adset_id = str(context.get("adset_id") or "")
    inventory = fetch_pause_inventory([ad_id]).get(adset_id)
    if inventory is None or inventory.get("complete") is not True:
        raise ProducerActionError(
            str((inventory or {}).get("error") or "INVENTORY_INCOMPLETE")
        )
    active_ids = tuple(sorted(str(item) for item in inventory.get("active_ids") or ()))
    if context.get("effective_status") != "ACTIVE" or ad_id not in active_ids:
        raise ProducerActionError("PAUSE_TARGET_NOT_ACTIVE")
    if len(active_ids) <= 1:
        raise ProducerActionError("LAST_EFFECTIVE_ACTIVE")

    city, language = _scope_labels(adset_id)
    intended_payload = {
        "schema_version": _PRODUCER_SCHEMA_VERSION,
        "operation": "PAUSE_AD",
        "ad_id": ad_id,
        "adset_id": adset_id,
        "expected_before_status": "ACTIVE",
        "expected_after_status": "PAUSED",
        "reason_code": reason_code,
        "producer_inventory_sha256": str(inventory.get("state_sha256") or ""),
        "sibling_active_ids": [
            active_id for active_id in active_ids if active_id != ad_id
        ],
    }
    key = _proposal_key(scope, intended_payload, idempotency_key)
    evidence_payload = {
        "ad_id": ad_id,
        "adset_id": adset_id,
        "effective_status": context.get("effective_status"),
        "active_ids": active_ids,
        "inventory_sha256": inventory.get("state_sha256"),
    }
    return _persist_proposal(
        proposal_kind=ProposalKind.PAUSE,
        origin=_proposal_origin(origin),
        idempotency_key=key,
        source_ref=scope,
        actor="action_producer_gateway",
        # Карточка собирается здесь и живёт в summary: доставку могут подготовить
        # два разных пути (_ensure_initial_deliveries и prepare_owner_delivery),
        # и второй молча игнорирует переданный rendered_text.
        summary=render_pause_card(
            ad_id=ad_id,
            ad_name=str(context.get("name") or ""),
            adset_id=adset_id,
            city=city,
            language=language,
            # Инвентарь тут заведомо complete (иначе выше был бы отказ), поэтому
            # остаток активных известен точно: все ACTIVE минус эта реклама.
            remaining_active=len(active_ids) - 1,
            decision=decision,
        ),
        account_id=_account_id(context),
        adset_id=adset_id,
        subject_id=ad_id,
        city=city,
        language=language,
        intended_payload=intended_payload,
        evidence=(
            _evidence(
                kind="PRODUCER_LIVE_INVENTORY",
                source="FACEBOOK",
                subject_id=ad_id,
                observed_at=prepared_at,
                payload=evidence_payload,
            ),
        ),
        now=prepared_at,
    )


def propose_unpause(
    ad_id: str,
    *,
    origin: object,
    scope: str,
    idempotency_key: str | None = None,
    decision: DecisionContext | None = None,
    now: datetime | None = None,
) -> ProducerActionOutcome:
    """Создаёт отдельный UNPAUSE proposal без вызова Facebook adapter."""
    prepared_at = _aware_utc(now)
    duplicate = _live_duplicate(
        subject_id=ad_id, action_kind="UNPAUSE_AD", now=prepared_at
    )
    if duplicate is not None:
        return duplicate
    exact, exact_error = fetch_exact_ad_contexts([ad_id], require_names=True)
    context = exact.get(ad_id)
    if exact_error is not None or context is None:
        raise ProducerActionError(exact_error or "UNPAUSE_TARGET_MISSING")
    adset_id = str(context.get("adset_id") or "")
    inventory = fetch_pause_inventory([ad_id]).get(adset_id)
    if inventory is None or inventory.get("complete") is not True:
        raise ProducerActionError(
            str((inventory or {}).get("error") or "INVENTORY_INCOMPLETE")
        )
    if context.get("effective_status") != "PAUSED":
        raise ProducerActionError("UNPAUSE_TARGET_NOT_PAUSED")
    city, language = _scope_labels(adset_id)
    # Возврат добавляет к текущим ACTIVE ровно одну рекламу — эту.
    active_after = len(inventory.get("active_ids") or ()) + 1
    intended_payload = {
        "schema_version": _PRODUCER_SCHEMA_VERSION,
        "operation": "UNPAUSE_AD",
        "ad_id": ad_id,
        "adset_id": adset_id,
        "expected_before_status": "PAUSED",
        "expected_after_status": "ACTIVE",
        "producer_inventory_sha256": str(inventory.get("state_sha256") or ""),
    }
    key = _proposal_key(scope, intended_payload, idempotency_key)
    return _persist_proposal(
        proposal_kind=ProposalKind.UNPAUSE,
        origin=_proposal_origin(origin),
        idempotency_key=key,
        source_ref=scope,
        actor="action_producer_gateway",
        summary=render_unpause_card(
            ad_id=ad_id,
            ad_name=str(context.get("name") or ""),
            adset_id=adset_id,
            city=city,
            language=language,
            active_after=active_after,
            decision=decision,
        ),
        account_id=_account_id(context),
        adset_id=adset_id,
        subject_id=ad_id,
        city=city,
        language=language,
        intended_payload=intended_payload,
        evidence=(
            _evidence(
                kind="PRODUCER_LIVE_INVENTORY",
                source="FACEBOOK",
                subject_id=ad_id,
                observed_at=prepared_at,
                payload={
                    "ad_id": ad_id,
                    "adset_id": adset_id,
                    "effective_status": context.get("effective_status"),
                    "inventory_sha256": inventory.get("state_sha256"),
                },
            ),
        ),
        now=prepared_at,
    )


def propose_scale(
    recommendation: Mapping[str, object],
    *,
    scope: str,
    idempotency_key: str | None = None,
    decision: DecisionContext | None = None,
    now: datetime | None = None,
) -> ProducerActionOutcome:
    """Фиксирует точный SCALE intent; budget adapter здесь недоступен."""
    prepared_at = _aware_utc(now)
    adset_id = str(recommendation.get("adset_id") or "")
    ad_id = str(recommendation.get("ad_id") or "")
    if not adset_id or not ad_id:
        raise ProducerActionError("SCALE_TARGET_MISSING")
    duplicate = _live_duplicate(
        subject_id=adset_id, action_kind="SET_ADSET_BUDGET", now=prepared_at
    )
    if duplicate is not None:
        return duplicate
    current = Decimal(str(recommendation.get("current_budget_usd")))
    target = Decimal(str(recommendation.get("new_budget_usd")))
    if (
        not current.is_finite()
        or not target.is_finite()
        or current <= 0
        or target <= 0
        or current == target
    ):
        raise ProducerActionError("SCALE_BUDGET_INVALID")
    city, language = _scope_labels(adset_id)
    intended_payload = {
        "schema_version": _PRODUCER_SCHEMA_VERSION,
        "operation": "SET_ADSET_BUDGET",
        "adset_id": adset_id,
        "candidate_ad_id": ad_id,
        "expected_current_budget_usd": current,
        "target_budget_usd": target,
    }
    key = _proposal_key(scope, intended_payload, idempotency_key)
    recommendation_payload = {
        key: value
        for key, value in recommendation.items()
        if isinstance(value, (str, int, float, bool, Decimal, type(None)))
    }
    return _persist_proposal(
        proposal_kind=ProposalKind.SCALE,
        origin=ProposalOrigin.AUTOPILOT,
        idempotency_key=key,
        source_ref=scope,
        actor="budget_scaler",
        summary=render_scale_card(
            adset_id=adset_id,
            adset_name=str(recommendation.get("adset_name") or ""),
            ad_name=str(recommendation.get("ad_name") or ""),
            city=city,
            language=language,
            current_budget_usd=current,
            target_budget_usd=target,
            decision=decision,
        ),
        account_id=_account_id(),
        adset_id=adset_id,
        subject_id=adset_id,
        city=city,
        language=language,
        intended_payload=intended_payload,
        evidence=(
            _evidence(
                kind="PRODUCER_RECOMMENDATION",
                source="BUDGET_SCALER",
                subject_id=adset_id,
                observed_at=prepared_at,
                payload=recommendation_payload,
            ),
        ),
        now=prepared_at,
    )


def propose_asset_recovery(
    *,
    account_id: str,
    adset_id: str,
    subject_id: str,
    city: str | None,
    language: str | None,
    recovery_payload: Mapping[str, object],
    source_ref: str,
    actor: str,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> ProducerActionOutcome:
    """Создаёт отдельный ASSET_RECOVERY proposal без подготовки/CREATE."""
    prepared_at = _aware_utc(now)
    payload = {
        "schema_version": _PRODUCER_SCHEMA_VERSION,
        "operation": "RECOVER_AD",
        **dict(recovery_payload),
    }
    key = _proposal_key(source_ref, payload, idempotency_key)
    return _persist_proposal(
        proposal_kind=ProposalKind.ASSET_RECOVERY,
        origin=ProposalOrigin.RECOVERY,
        idempotency_key=key,
        source_ref=source_ref,
        actor=actor,
        summary=render_recovery_card(
            subject_id=subject_id,
            adset_id=adset_id,
            city=city,
            language=language,
            reason=str(recovery_payload.get("reason") or "") or None,
        ),
        account_id=account_id.removeprefix("act_"),
        adset_id=adset_id,
        subject_id=subject_id,
        city=city,
        language=language,
        intended_payload=payload,
        evidence=(
            _evidence(
                kind="RECOVERY_REQUEST",
                source="RECOVERY",
                subject_id=subject_id,
                observed_at=prepared_at,
                payload=payload,
            ),
        ),
        now=prepared_at,
    )


# Временные compatibility aliases для T7 и старых producer callers.
# Они намеренно только создают proposals и не импортируют execution boundary.
execute_pause = propose_pause
execute_unpause = propose_unpause
execute_scale = propose_scale
