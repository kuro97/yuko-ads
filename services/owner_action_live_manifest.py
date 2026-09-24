"""Сборка технического манифеста из одобренного намерения и ЖИВОГО состояния.

Продюсер кладёт в предложение бизнес-намерение (``ad_id``, ожидаемые статусы,
причина, целевой бюджет), а НЕ сериализованный манифест. «Восстановить» из него
манифест нельзя: манифест обязан описывать состояние Facebook на момент
исполнения — инвентарь адсета, статусы соседей, живые факты FB/AMO/CDP. Здесь
одобренное намерение соединяется со свежим read-only снимком и уходит в закрытые
фабрики ``services.action_manifests``.

Мутаций тут нет: только чтение. Любое расхождение живого состояния с тем, что
одобрял владелец, поднимается как ``stale`` — это честная остановка, а не баг
сборки, и коды у них разные.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from decimal import Decimal
from typing import Mapping

from services.action_manifests import (
    build_pause_manifest,
    build_scale_manifest,
    build_unpause_manifest,
)
from services.adset_pause_guard import (
    fetch_pause_inventory,
    unrelated_inventory_baseline,
)
from services.approval_checker_models import (
    ActionManifest,
    ActionKind,
    ActionOrigin,
    EvidenceRecord,
    EvidenceRequest,
    EvidenceState,
    FactCategory,
    FactClaim,
    Metric,
    PauseCandidate,
    ScaleCandidate,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
    UnpauseCandidate,
)
from services.owner_action_models import ProposalKind, ProposalOrigin


# Окно решения по одобренному действию: тот же трейлинг, что у replacement.
_DECISION_DAYS = 30
_PAUSE_WINDOW_SEMANTIC = "OWNER_PAUSE_TRAILING_30D"
_SCALE_WINDOW_SEMANTIC = "OWNER_SCALE_TRAILING_30D"


def _account_day_window(ad_id: str, now: datetime, semantic: str) -> TimeWindow:
    """Окно решения из ПОЛНЫХ суток кабинета — иначе расход недоказуем.

    Insights отдаёт точный расход только за целые дни в таймзоне кабинета:
    approval_source_facebook._load_insights честно пропускает окно, чьи края
    не лежат на полуночах. Окно «now-30d..now» на полуночи не попадало никогда,
    запись SPEND не собиралась, и НИ ОДНА одобренная мутация не исполнилась за
    всю историю контура. Конец окна — полночь текущего дня кабинета: сегодняшний
    день не закрыт, его расход Insights округлил бы.
    """
    from services.approval_source_facebook import load_ad_account_timezone

    try:
        timezone_name = load_ad_account_timezone(ad_id)
    except Exception as exc:  # noqa: BLE001 — недоступность FB не баг сборки
        raise LiveManifestError(
            "LIVE_ACCOUNT_TIMEZONE_UNAVAILABLE", stale=False, unavailable=True
        ) from exc
    try:
        account_tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise LiveManifestError(
            "LIVE_ACCOUNT_TIMEZONE_INVALID", stale=False
        ) from exc
    local_now = now.astimezone(account_tz)
    end = datetime.combine(local_now.date(), time.min, tzinfo=account_tz)
    return TimeWindow(
        start=end - timedelta(days=_DECISION_DAYS),
        end=end,
        timezone_name=timezone_name,
        semantic=semantic,
    )

# Origin манифеста берётся из origin предложения: он входит в digest, поэтому
# обязан быть детерминированным, а не «текущим настроением» исполнителя.
_ACTION_ORIGIN_BY_PROPOSAL_ORIGIN = {
    ProposalOrigin.WEB: ActionOrigin.WEB,
    ProposalOrigin.TELEGRAM_COMMAND: ActionOrigin.TELEGRAM,
    ProposalOrigin.AUTOPILOT: ActionOrigin.AUTOPILOT_LIVE,
    ProposalOrigin.CRON: ActionOrigin.AUTOPILOT_CLASSIC,
    ProposalOrigin.RECOVERY: ActionOrigin.REPLACEMENT,
}


class LiveManifestError(RuntimeError):
    """Манифест не собран.

    Три разных исхода, которые нельзя путать:

    * ``stale=True`` — живое состояние разошлось с одобренным (цель уже не
      ACTIVE, другой адсет, другой бюджет). Это терминальная и честная
      остановка: одобряли не это.
    * ``unavailable=True`` — источник не ответил или ответил неполно (FB/AMO/CDP
      таймаут, обрыв, неполная страница). Одобрение владельца при этом остаётся
      в силе: задание обязано доехать позже, а не сгореть из-за чужого downtime.
    * оба False — баг сборки или битое намерение продюсера: тоже повтор, но
      после починки кода.
    """

    __slots__ = ("code", "stale", "unavailable")

    def __init__(self, code: str, *, stale: bool, unavailable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.stale = stale
        self.unavailable = unavailable


def action_origin_for(origin: ProposalOrigin) -> ActionOrigin:
    return _ACTION_ORIGIN_BY_PROPOSAL_ORIGIN.get(origin, ActionOrigin.AUTOPILOT_LIVE)


def is_serialized_manifest(payload: Mapping[str, object]) -> bool:
    """LAUNCH кладёт в намерение сам манифест — его надо не собирать, а прочитать."""

    return {
        "kind",
        "manifest_id",
        "origin",
        "idempotency_key",
        "prepared_at",
    }.issubset(payload.keys())


def build_live_manifest(
    proposal_kind: ProposalKind,
    payload: Mapping[str, object],
    *,
    origin: ActionOrigin,
    idempotency_key: str,
    now: datetime,
) -> ActionManifest:
    """Собирает свежий манифест нужного вида из намерения и живого состояния."""

    builder = {
        ProposalKind.PAUSE: _build_pause,
        ProposalKind.UNPAUSE: _build_unpause,
        ProposalKind.SCALE: _build_scale,
    }.get(proposal_kind)
    if builder is None:
        raise LiveManifestError("PROPOSAL_KIND_NOT_BUILDABLE", stale=False)
    return builder(
        payload,
        origin=origin,
        idempotency_key=idempotency_key,
        now=now,
    )


# ---------------------------------------------------------------------------
# Чтение намерения
# ---------------------------------------------------------------------------


def _intent_text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise LiveManifestError(f"INTENT_FIELD_MISSING_{field.upper()}", stale=False)
    return value


def _intent_decimal(payload: Mapping[str, object], field: str) -> Decimal:
    value = payload.get(field)
    if isinstance(value, bool) or value is None:
        raise LiveManifestError(f"INTENT_FIELD_MISSING_{field.upper()}", stale=False)
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise LiveManifestError(
            f"INTENT_FIELD_INVALID_{field.upper()}",
            stale=False,
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise LiveManifestError(f"INTENT_FIELD_INVALID_{field.upper()}", stale=False)
    return parsed


# ---------------------------------------------------------------------------
# Живой инвентарь адсета
# ---------------------------------------------------------------------------


def _live_inventory(ad_id: str, adset_id: str) -> Mapping[str, object]:
    """Полный live inventory адсета вокруг цели; чужой адсет — честный дрейф."""

    try:
        inventories = fetch_pause_inventory([ad_id])
    except Exception as exc:  # noqa: BLE001 — недоступность FB не баг сборки
        raise LiveManifestError(
            "LIVE_INVENTORY_UNAVAILABLE", stale=False, unavailable=True
        ) from exc
    inventory = inventories.get(adset_id)
    if inventory is None:
        if inventories:
            # Реклама нашлась, но уже в другом адсете: одобряли не это.
            raise LiveManifestError("LIVE_ADSET_CHANGED", stale=True)
        raise LiveManifestError(
            "LIVE_INVENTORY_UNAVAILABLE", stale=False, unavailable=True
        )
    if inventory.get("complete") is not True:
        raise LiveManifestError(
            "LIVE_INVENTORY_UNAVAILABLE", stale=False, unavailable=True
        )
    return inventory


def _live_context(inventory: Mapping[str, object], ad_id: str) -> Mapping[str, object]:
    context = (inventory.get("inventory_context") or {}).get(ad_id)
    if not isinstance(context, Mapping):
        raise LiveManifestError("LIVE_TARGET_MISSING", stale=True)
    return context


def _inventory_digest(inventory: Mapping[str, object]) -> str:
    digest = str(inventory.get("state_sha256") or "")
    if len(digest) != 64:
        raise LiveManifestError(
            "LIVE_INVENTORY_UNAVAILABLE", stale=False, unavailable=True
        )
    return digest


def _sibling_baseline(
    inventory: Mapping[str, object],
    *,
    ad_id: str,
    adset_id: str,
) -> tuple[str, tuple]:
    try:
        return unrelated_inventory_baseline(
            inventory,
            target_ad_id=ad_id,
            expected_adset_id=adset_id,
        )
    except (TypeError, ValueError) as exc:
        raise LiveManifestError(
            "LIVE_INVENTORY_UNAVAILABLE", stale=False, unavailable=True
        ) from exc


# ---------------------------------------------------------------------------
# Живые факты FB/AMO/CDP
# ---------------------------------------------------------------------------


def _evidence_request(
    *,
    request_id: str,
    action_kind: ActionKind,
    subjects: tuple[SubjectRef, ...],
    windows: tuple[TimeWindow, ...],
    adset_id: str,
    ad_ids: tuple[str, ...],
    now: datetime,
) -> EvidenceRequest:
    return EvidenceRequest(
        request_id=request_id,
        purpose="ACTION",
        action_kind=action_kind,
        generated_at=now,
        subjects=subjects,
        claims=(),
        required_sources=(
            SourceSystem.FACEBOOK,
            SourceSystem.AMO,
            SourceSystem.CDP_ERP,
        ),
        windows=windows,
        account_ids=(),
        adset_ids=(adset_id,),
        ad_ids=ad_ids,
        card_ids=(),
        staged_relative_paths=(),
        include_full_inventory=True,
        force_live=True,
        max_age_seconds=1,
    )


def _load_live_sources(
    request: EvidenceRequest,
    now: datetime,
) -> tuple[SourceEvidence, SourceEvidence, SourceEvidence]:
    """Один force-live снимок трёх источников; gateway перечитает их ещё раз."""

    from services.approval_source_amo import load_amo_evidence
    from services.approval_source_cdp import load_cdp_evidence
    from services.approval_source_facebook import load_facebook_evidence

    try:
        facebook = load_facebook_evidence(request, now, force_live=True)
        amo = load_amo_evidence(request, now, force_live=True)
        cdp = load_cdp_evidence(request, now, force_live=True)
    except Exception as exc:  # noqa: BLE001 — недоступность источника не баг сборки
        raise LiveManifestError(
            "LIVE_FACTS_UNAVAILABLE", stale=False, unavailable=True
        ) from exc
    for evidence in (facebook, amo, cdp):
        if (
            evidence.state is not EvidenceState.FRESH_COMPLETE
            or evidence.complete is not True
            or evidence.from_cache is True
        ):
            # Код причины несёт и диагноз источника: иначе сотни ретраев
            # ложатся в журнал голым LIVE_FACTS_INCOMPLETE_AMO, и настоящую
            # причину (400 на фильтр) пришлось восстанавливать вслепую.
            code = f"LIVE_FACTS_INCOMPLETE_{evidence.source.value}"
            detail = (evidence.error_code or "").strip()
            raise LiveManifestError(
                f"{code}:{detail}" if detail else code,
                stale=False,
                unavailable=True,
            )
    return facebook, amo, cdp


def _exact_record(
    evidence: SourceEvidence,
    subject: SubjectRef,
    metric: Metric,
    window: TimeWindow | None,
    *,
    match_parent: bool = True,
) -> EvidenceRecord:
    """Ровно одна свежая запись источника. Иначе — не молчим, а отказываемся.

    ``match_parent=False`` нужен для ADSET-субъектов: Facebook подписывает их
    ``parent_id`` кабинета, а намерение продюсера кабинет не хранит.
    """

    matches = [
        record
        for record in evidence.records
        if (
            record.subject == subject
            if match_parent
            else (
                record.subject.kind is subject.kind
                and record.subject.subject_id == subject.subject_id
            )
        )
        and record.metric is metric
        and record.window == window
        and record.state is EvidenceState.FRESH_COMPLETE
    ]
    if len(matches) != 1:
        raise LiveManifestError(
            f"LIVE_FACT_NOT_EXACT_{evidence.source.value}_{metric.value}",
            stale=False,
        )
    return matches[0]


def _decimal_fact(
    evidence: SourceEvidence,
    subject: SubjectRef,
    metric: Metric,
    window: TimeWindow | None,
    *,
    match_parent: bool = True,
) -> tuple[Decimal, str | None]:
    record = _exact_record(
        evidence, subject, metric, window, match_parent=match_parent
    )
    if not isinstance(record.value, Decimal):
        raise LiveManifestError(
            f"LIVE_FACT_INVALID_{evidence.source.value}_{metric.value}",
            stale=False,
        )
    return record.value, record.currency


def _int_fact(
    evidence: SourceEvidence,
    subject: SubjectRef,
    metric: Metric,
    window: TimeWindow | None,
) -> int:
    record = _exact_record(evidence, subject, metric, window)
    if type(record.value) is not int:
        raise LiveManifestError(
            f"LIVE_FACT_INVALID_{evidence.source.value}_{metric.value}",
            stale=False,
        )
    return record.value


# ---------------------------------------------------------------------------
# PAUSE
# ---------------------------------------------------------------------------


def _build_pause(
    payload: Mapping[str, object],
    *,
    origin: ActionOrigin,
    idempotency_key: str,
    now: datetime,
) -> ActionManifest:
    ad_id = _intent_text(payload, "ad_id")
    adset_id = _intent_text(payload, "adset_id")
    reason_code = _intent_text(payload, "reason_code")
    before_status = _intent_text(payload, "expected_before_status")
    after_status = _intent_text(payload, "expected_after_status")

    inventory = _live_inventory(ad_id, adset_id)
    context = _live_context(inventory, ad_id)
    if str(context.get("effective_status") or "") != before_status:
        raise LiveManifestError("LIVE_TARGET_STATUS_CHANGED", stale=True)
    active_ids = tuple(sorted(str(item) for item in inventory.get("active_ids") or ()))
    if ad_id not in active_ids:
        raise LiveManifestError("LIVE_TARGET_STATUS_CHANGED", stale=True)
    sibling_active_ids = tuple(item for item in active_ids if item != ad_id)
    if not sibling_active_ids:
        # Пауза оставила бы адсет без единой активной рекламы.
        raise LiveManifestError("LIVE_LAST_ACTIVE_AD", stale=True)
    unrelated_sha256, sibling_snapshot = _sibling_baseline(
        inventory,
        ad_id=ad_id,
        adset_id=adset_id,
    )

    decision_window = _account_day_window(ad_id, now, _PAUSE_WINDOW_SEMANTIC)
    subject = SubjectRef(SubjectKind.AD, ad_id, adset_id)
    facebook, amo, cdp = _load_live_sources(
        _evidence_request(
            request_id=f"owner-pause:{ad_id}",
            action_kind=ActionKind.PAUSE,
            subjects=(subject,),
            windows=(decision_window,),
            adset_id=adset_id,
            ad_ids=(ad_id,),
            now=now,
        ),
        now,
    )
    spend, spend_currency = _decimal_fact(
        facebook, subject, Metric.SPEND, decision_window
    )
    if not spend_currency:
        raise LiveManifestError("LIVE_FACT_INVALID_FACEBOOK_CURRENCY", stale=False)
    revenue_lcy, _currency = _decimal_fact(
        cdp, subject, Metric.REVENUE, decision_window
    )
    candidate = PauseCandidate(
        ad_id=ad_id,
        adset_id=adset_id,
        display_name=str(context.get("name") or ad_id),
        reason_code=reason_code,
        decision_window=decision_window,
        expected_before_status=before_status,
        expected_after_status=after_status,
        spend=spend,
        spend_currency=spend_currency,
        leads=_int_fact(facebook, subject, Metric.LEADS, decision_window),
        quals=_int_fact(amo, subject, Metric.QUALS, decision_window),
        payments=tuple(
            payment for payment in cdp.payments if payment.fb_ad_id == ad_id
        ),
        revenue_lcy=revenue_lcy,
        pre_inventory_sha256=_inventory_digest(inventory),
        sibling_active_ids=sibling_active_ids,
        pre_unrelated_inventory_sha256=unrelated_sha256,
        sibling_status_snapshot=sibling_snapshot,
    )
    return _guarded(
        build_pause_manifest,
        candidate,
        origin=origin,
        idempotency_key=idempotency_key,
        now=now,
    )


# ---------------------------------------------------------------------------
# UNPAUSE
# ---------------------------------------------------------------------------


def _build_unpause(
    payload: Mapping[str, object],
    *,
    origin: ActionOrigin,
    idempotency_key: str,
    now: datetime,
) -> ActionManifest:
    ad_id = _intent_text(payload, "ad_id")
    adset_id = _intent_text(payload, "adset_id")
    before_status = _intent_text(payload, "expected_before_status")
    after_status = _intent_text(payload, "expected_after_status")

    inventory = _live_inventory(ad_id, adset_id)
    context = _live_context(inventory, ad_id)
    if str(context.get("effective_status") or "") != before_status:
        raise LiveManifestError("LIVE_TARGET_STATUS_CHANGED", stale=True)
    unrelated_sha256, sibling_snapshot = _sibling_baseline(
        inventory,
        ad_id=ad_id,
        adset_id=adset_id,
    )
    candidate = UnpauseCandidate(
        ad_id=ad_id,
        adset_id=adset_id,
        display_name=str(context.get("name") or ad_id),
        expected_before_status=before_status,
        expected_after_status=after_status,
        pre_inventory_sha256=_inventory_digest(inventory),
        pre_unrelated_inventory_sha256=unrelated_sha256,
        sibling_status_snapshot=sibling_snapshot,
    )
    return _guarded(
        build_unpause_manifest,
        candidate,
        origin=origin,
        idempotency_key=idempotency_key,
        now=now,
    )


# ---------------------------------------------------------------------------
# SCALE
# ---------------------------------------------------------------------------


def _build_scale(
    payload: Mapping[str, object],
    *,
    origin: ActionOrigin,
    idempotency_key: str,
    now: datetime,
) -> ActionManifest:
    adset_id = _intent_text(payload, "adset_id")
    candidate_ad_id = _intent_text(payload, "candidate_ad_id")
    expected_current = _intent_decimal(payload, "expected_current_budget_usd")
    target_budget = _intent_decimal(payload, "target_budget_usd")

    window = _account_day_window(candidate_ad_id, now, _SCALE_WINDOW_SEMANTIC)
    adset_subject = SubjectRef(SubjectKind.ADSET, adset_id)
    ad_subject = SubjectRef(SubjectKind.AD, candidate_ad_id, adset_id)
    facebook, amo, cdp = _load_live_sources(
        _evidence_request(
            request_id=f"owner-scale:{adset_id}",
            action_kind=ActionKind.SCALE,
            subjects=(adset_subject, ad_subject),
            windows=(window,),
            adset_id=adset_id,
            ad_ids=(candidate_ad_id,),
            now=now,
        ),
        now,
    )
    status = _exact_record(
        facebook,
        adset_subject,
        Metric.EFFECTIVE_STATUS,
        None,
        match_parent=False,
    )
    if str(status.value) != "ACTIVE":
        raise LiveManifestError("LIVE_ADSET_NOT_ACTIVE", stale=True)
    current_budget, currency = _decimal_fact(
        facebook,
        adset_subject,
        Metric.DAILY_BUDGET,
        None,
        match_parent=False,
    )
    if not currency:
        raise LiveManifestError("LIVE_FACT_INVALID_FACEBOOK_CURRENCY", stale=False)
    # Владелец одобрял подъём именно с этой суммы: живой другой бюджет —
    # честный дрейф, а не повод молча поднять с чего попало.
    if current_budget != expected_current or target_budget <= current_budget:
        raise LiveManifestError("LIVE_BUDGET_CHANGED", stale=True)

    spend, spend_currency = _decimal_fact(facebook, ad_subject, Metric.SPEND, window)
    revenue_lcy, revenue_currency = _decimal_fact(
        cdp, ad_subject, Metric.REVENUE, window
    )
    facts = (
        _claim("fb-spend", ad_subject, Metric.SPEND, spend, SourceSystem.FACEBOOK, window, spend_currency),
        _claim(
            "fb-leads",
            ad_subject,
            Metric.LEADS,
            _int_fact(facebook, ad_subject, Metric.LEADS, window),
            SourceSystem.FACEBOOK,
            window,
            None,
        ),
        _claim(
            "amo-quals",
            ad_subject,
            Metric.QUALS,
            _int_fact(amo, ad_subject, Metric.QUALS, window),
            SourceSystem.AMO,
            window,
            None,
        ),
        _claim(
            "cdp-revenue",
            ad_subject,
            Metric.REVENUE,
            revenue_lcy,
            SourceSystem.CDP_ERP,
            window,
            revenue_currency,
        ),
    )
    candidate = ScaleCandidate(
        adset_id=adset_id,
        expected_status="ACTIVE",
        current_budget=current_budget,
        target_budget=target_budget,
        currency=currency,
        facebook_window=window,
        outcome_window=window,
        candidate_ad_ids=(candidate_ad_id,),
        facts=facts,
    )
    return _guarded(
        build_scale_manifest,
        candidate,
        origin=origin,
        idempotency_key=idempotency_key,
        now=now,
    )


def _claim(
    suffix: str,
    subject: SubjectRef,
    metric: Metric,
    value: object,
    source: SourceSystem,
    window: TimeWindow,
    currency: str | None,
) -> FactClaim:
    return FactClaim(
        claim_id=f"{subject.subject_id}:{suffix}",
        field_id=None,
        category=FactCategory.BUSINESS_METRIC,
        subject=subject,
        metric=metric,
        value=value,
        source=source,
        window=window,
        currency=currency,
    )


def _guarded(builder, candidate, **kwargs) -> ActionManifest:
    """Фабрики манифестов fail-closed: их отказ — это несобранный манифест."""

    try:
        return builder(candidate, **kwargs)
    except LiveManifestError:
        raise
    except (TypeError, ValueError) as exc:
        raise LiveManifestError("MANIFEST_CONTRACT_REJECTED", stale=False) from exc
