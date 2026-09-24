"""Закрытая read-only композиция источников Approval Checker.

Модуль намеренно не принимает callbacks/clients от вызывающего кода. Это не
даёт producer-у подменить независимую проверку заранее подготовленными данными.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

import config
from services.approval_audit import read_action_history
from services.approval_checker_models import (
    ActionBatchManifest,
    ActionManifest,
    AssetRecoveryManifest,
    EvidenceBundle,
    EvidenceRecord,
    EvidenceRequest,
    EvidenceState,
    FactCategory,
    LaunchManifest,
    Metric,
    PauseManifest,
    ScaleManifest,
    SourceEvidence,
    SourceFreshness,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
    UnpauseManifest,
    canonical_json,
)
from services.approval_source_amo import (
    AmoEvidenceError,
    load_amo_evidence,
    load_amo_window_ad_ids,
)
from services.approval_source_cdp import (
    CdpEvidenceError,
    load_cdp_evidence,
    load_cdp_window_ad_ids,
)
from services.approval_source_creative_kb import load_creative_kb_evidence
from services.approval_source_daily_metrics import load_daily_metrics_evidence
from services.approval_source_decisions import (
    load_decisions_evidence,
    load_feedback_evidence,
)
from services.approval_source_facebook import (
    FacebookEvidenceError,
    load_account_ad_ownership,
    load_facebook_evidence,
    read_action_precondition,
)
from services.approval_source_learning import (
    load_hypothesis_evidence,
    load_pattern_evidence,
)
from services.approval_source_media import load_media_evidence
from services.approval_source_pending_briefs import load_pending_brief_evidence
from services.approval_source_runtime import (
    load_fb_error_ring_evidence,
    load_operational_state_evidence,
    load_runtime_evidence,
)
from services.approval_source_trello import load_trello_evidence


_ACTION_SOURCES = (
    SourceSystem.FACEBOOK,
    SourceSystem.TRELLO,
    SourceSystem.MEDIA_BYTES,
    SourceSystem.AMO,
    SourceSystem.CDP_ERP,
)
_ACTION_SOURCE_SET = frozenset(_ACTION_SOURCES)
_OPERATIONAL_SOURCES = frozenset(
    {
        SourceSystem.SETTINGS_FILE,
        SourceSystem.GUARDIAN_STATE,
        SourceSystem.BRIEF_GENERATOR_STATE,
        SourceSystem.ANOMALY_ALERT_STATE,
        SourceSystem.EXPIRED_OFFER_STATE,
        SourceSystem.COVERAGE_STATE,
        SourceSystem.ADS_WATCHDOG_STATE,
        SourceSystem.ADSET_SPEND_GUARD_STATE,
        SourceSystem.CDP_SPEND_ALERT_STATE,
        SourceSystem.CRON_HEARTBEATS,
        SourceSystem.CRON_FAILURE_STATE,
    }
)
_MAX_REQUEST_CLAIMS = 2_000
_MAX_REQUEST_SUBJECTS = 2_000
_MAX_REQUEST_OBJECT_IDS = 2_000
_MAX_REQUEST_WINDOWS = 64
# Потолок точечных проверок принадлежности: за окно отчёта реально встречаются
# сотни разных ad_id. Больше — это уже не «лиды окна», а подозрительный ввод.
_MAX_OWNERSHIP_CANDIDATE_IDS = 1_000
_EXACT_PROVIDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_SimpleLoader = Callable[[EvidenceRequest, datetime], SourceEvidence]


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} должен содержать timezone")


def _source_error(source: SourceSystem, now: datetime, code: str) -> SourceEvidence:
    return SourceEvidence(
        source=source,
        state=EvidenceState.ERROR,
        fetched_at=now,
        data_as_of=None,
        from_cache=False,
        complete=False,
        records=(),
        error_code=code,
    )


def _source_incomplete(
    source: SourceSystem, now: datetime, code: str
) -> SourceEvidence:
    return SourceEvidence(
        source=source,
        state=EvidenceState.INCOMPLETE,
        fetched_at=now,
        data_as_of=None,
        from_cache=False,
        complete=False,
        records=(),
        error_code=code,
    )


def _validate_request(request: EvidenceRequest, now: datetime) -> None:
    _aware(now, "now")
    _aware(request.generated_at, "generated_at")
    if request.purpose not in {"REPORT", "ACTION"}:
        raise ValueError("purpose должен быть REPORT или ACTION")
    if request.purpose == "ACTION":
        if request.action_kind is None or not request.force_live:
            raise ValueError("ACTION evidence требует action_kind и force_live")
        if set(request.required_sources) != _ACTION_SOURCE_SET:
            raise ValueError("ACTION evidence требует ровно пять закрытых источников")
    elif request.action_kind is not None:
        raise ValueError("REPORT evidence не может содержать action_kind")
    if not request.required_sources:
        raise ValueError("required_sources не может быть пустым")
    if len(request.required_sources) != len(set(request.required_sources)):
        raise ValueError("required_sources должны быть уникальными")
    if len(request.claims) > _MAX_REQUEST_CLAIMS:
        raise ValueError("Слишком много claims в одном source request")
    if len(request.subjects) > _MAX_REQUEST_SUBJECTS:
        raise ValueError("Слишком много subjects в одном source request")
    if len(request.windows) > _MAX_REQUEST_WINDOWS:
        raise ValueError("Слишком много окон в одном source request")
    for field_name, values in (
        ("account_ids", request.account_ids),
        ("adset_ids", request.adset_ids),
        ("ad_ids", request.ad_ids),
        ("card_ids", request.card_ids),
        ("staged_relative_paths", request.staged_relative_paths),
    ):
        if len(values) > _MAX_REQUEST_OBJECT_IDS:
            raise ValueError(f"{field_name}: превышен закрытый лимит")
        if len(values) != len(set(values)):
            raise ValueError(f"{field_name} должны быть уникальными")
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError(f"{field_name} содержат пустой ID")
    if isinstance(request.max_age_seconds, bool) or request.max_age_seconds < 0:
        raise ValueError("max_age_seconds должен быть неотрицательным int")


def _load_checker_audit(request: EvidenceRequest, now: datetime) -> SourceEvidence:
    records: list[EvidenceRecord] = []
    latest: datetime | None = None
    try:
        for window in request.windows:
            history = read_action_history(window)
            pending = any(item.reconciliation_required for item in history)
            confirmed = sum(item.result.value == "CONFIRMED" for item in history)
            for claim in request.claims:
                if (
                    claim.source is not SourceSystem.CHECKER_AUDIT
                    or claim.window != window
                ):
                    continue
                value: int | str | None
                if claim.metric is Metric.ACTION_COUNT:
                    value = confirmed
                elif claim.metric is Metric.HISTORY_STATE:
                    value = "PENDING" if pending else "CLEAR"
                else:
                    value = None
                if value is None:
                    continue
                records.append(
                    EvidenceRecord(
                        category=claim.category,
                        subject=claim.subject,
                        metric=claim.metric,
                        value=value,
                        source=SourceSystem.CHECKER_AUDIT,
                        state=EvidenceState.FRESH_COMPLETE,
                        observed_at=now,
                        window=window,
                        currency=claim.currency,
                        entity_ids=tuple(sorted(item.item_id for item in history)),
                    )
                )
            completed = [
                item.completed_at for item in history if item.completed_at is not None
            ]
            if completed:
                window_latest = max(completed)
                latest = window_latest if latest is None else max(latest, window_latest)
    except Exception as exc:
        return _source_error(
            SourceSystem.CHECKER_AUDIT,
            now,
            f"AUDIT_READ_{type(exc).__name__.upper()}",
        )
    return SourceEvidence(
        source=SourceSystem.CHECKER_AUDIT,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=now,
        data_as_of=latest or now,
        from_cache=False,
        complete=True,
        records=tuple(records),
    )


def _load_checker_runtime(request: EvidenceRequest, now: datetime) -> SourceEvidence:
    audit_path = Path(config.REPORT_CHECKER_AUDIT_PATH)
    parent = audit_path.parent
    writable = (audit_path.exists() and audit_path.is_file()) or parent.exists()
    healthy = bool(config.REPORT_CHECKER_ENABLED and writable)
    records = tuple(
        EvidenceRecord(
            category=claim.category,
            subject=claim.subject,
            metric=claim.metric,
            value="HEALTHY" if healthy else "UNHEALTHY",
            source=SourceSystem.CHECKER_RUNTIME,
            state=EvidenceState.FRESH_COMPLETE if healthy else EvidenceState.ERROR,
            observed_at=now,
            window=claim.window,
            currency=claim.currency,
        )
        for claim in request.claims
        if claim.source is SourceSystem.CHECKER_RUNTIME
        and claim.metric is Metric.CHECKER_HEALTH
    )
    return SourceEvidence(
        source=SourceSystem.CHECKER_RUNTIME,
        state=EvidenceState.FRESH_COMPLETE if healthy else EvidenceState.ERROR,
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=healthy,
        records=records,
        error_code=None if healthy else "CHECKER_RUNTIME_UNHEALTHY",
    )


def _dispatch_source(
    source: SourceSystem,
    request: EvidenceRequest,
    now: datetime,
) -> SourceEvidence:
    simple_loaders: dict[SourceSystem, _SimpleLoader] = {
        SourceSystem.MEDIA_BYTES: load_media_evidence,
        SourceSystem.DECISIONS_DB: load_decisions_evidence,
        SourceSystem.AUTOPILOT_FEEDBACK: load_feedback_evidence,
        SourceSystem.CREATIVE_KB: load_creative_kb_evidence,
        SourceSystem.PATTERN_LEARNINGS: load_pattern_evidence,
        SourceSystem.HYPOTHESIS_JOURNAL: load_hypothesis_evidence,
        SourceSystem.AD_DAILY_METRICS: load_daily_metrics_evidence,
        SourceSystem.PENDING_BRIEFS: load_pending_brief_evidence,
        SourceSystem.FB_ERROR_RING: load_fb_error_ring_evidence,
        SourceSystem.NOTIFICATION_RUNTIME: load_runtime_evidence,
        SourceSystem.CHECKER_AUDIT: _load_checker_audit,
        SourceSystem.CHECKER_RUNTIME: _load_checker_runtime,
    }
    if source is SourceSystem.FACEBOOK:
        return load_facebook_evidence(request, now, force_live=request.force_live)
    if source is SourceSystem.AMO:
        return load_amo_evidence(request, now, force_live=request.force_live)
    if source is SourceSystem.CDP_ERP:
        return load_cdp_evidence(request, now, force_live=request.force_live)
    if source is SourceSystem.TRELLO:
        return load_trello_evidence(request, now, force_live=request.force_live)
    if source in _OPERATIONAL_SOURCES:
        matches = load_operational_state_evidence(request, now)
        return next(
            (item for item in matches if item.source is source),
            _source_error(source, now, "SOURCE_ROUTING_MISSING"),
        )
    loader = simple_loaders.get(source)
    if loader is None:
        return _source_error(source, now, "SOURCE_NOT_SUPPORTED")
    return loader(request, now)


def _needs_account_outcome_enrichment(request: EvidenceRequest) -> bool:
    has_facebook_account_claim = any(
        claim.source is SourceSystem.FACEBOOK
        and claim.subject.kind is SubjectKind.ACCOUNT
        for claim in request.claims
    )
    return (
        request.purpose == "REPORT"
        and has_facebook_account_claim
        and SourceSystem.FACEBOOK in request.required_sources
        and bool(
            {SourceSystem.AMO, SourceSystem.CDP_ERP}.intersection(
                request.required_sources
            )
        )
    )


def _is_exact_ad_id(raw_ad_id: str) -> bool:
    """Отсеивает не-id значения поля (name/UTM и прочий мусор из CRM)."""

    ad_id = raw_ad_id.strip()
    forbidden_prefixes = (
        "name:",
        "ad_name:",
        "adset_name:",
        "campaign_name:",
        "utm:",
    )
    return bool(
        ad_id
        and not ad_id.lower().startswith(forbidden_prefixes)
        and _EXACT_PROVIDER_ID.fullmatch(ad_id) is not None
    )


def _outcome_candidate_ad_ids(request: EvidenceRequest) -> tuple[str, ...]:
    """Кандидаты на проверку принадлежности: ad_id, встреченные в окне.

    Счётчики AMO/CDP может изменить только объявление, чей id реально попался
    в сделке или платеже окна, — их и проверяем. Полный инвентарь кабинета
    для этого не нужен и не масштабируется (десятки тысяч объявлений).
    """

    candidates: set[str] = set(request.ad_ids)
    if SourceSystem.AMO in request.required_sources:
        try:
            candidates.update(
                load_amo_window_ad_ids(_claim_windows(request, SourceSystem.AMO))
            )
        except Exception as exc:
            raise _candidates_unreadable("OUTCOME_CANDIDATES_AMO", exc) from exc
    if SourceSystem.CDP_ERP in request.required_sources:
        try:
            candidates.update(
                load_cdp_window_ad_ids(
                    _claim_windows(request, SourceSystem.CDP_ERP),
                    force_live=request.force_live,
                )
            )
        except Exception as exc:
            raise _candidates_unreadable("OUTCOME_CANDIDATES_CDP", exc) from exc
    return tuple(sorted(item.strip() for item in candidates if _is_exact_ad_id(item)))


def _claim_windows(
    request: EvidenceRequest, source: SourceSystem
) -> tuple[TimeWindow, ...]:
    """Окна, за которые источник реально подписывается значениями."""

    return tuple(
        dict.fromkeys(
            claim.window
            for claim in request.claims
            if claim.source is source and claim.window is not None
        )
    )


def _candidates_unreadable(prefix: str, exc: Exception) -> ValueError:
    detail = (
        str(exc)
        if isinstance(exc, (AmoEvidenceError, CdpEvidenceError))
        else type(exc).__name__.upper()
    )
    return ValueError(f"{prefix}_{detail}"[:120])


def _account_outcome_ad_ids(
    request: EvidenceRequest,
    facebook: SourceEvidence,
    now: datetime,
) -> tuple[str, ...]:
    """Даёт universe для AMO/CDP: только exact ad_id, доказанно наши.

    Раньше universe брался из полного инвентаря кабинета (marker
    ``INVENTORY_COMPLETE``) — стоимость O(кабинета), у большого кабинета это
    тысячи объявлений и гарантированный отказ по потолку. Теперь принадлежность
    проверяется точечно по тем id, что встретились в окне.
    """

    if (
        facebook.source is not SourceSystem.FACEBOOK
        or facebook.state is not EvidenceState.FRESH_COMPLETE
        or not facebook.complete
        or facebook.from_cache
    ):
        raise ValueError("FB_ACCOUNT_OWNERSHIP_SOURCE_INCOMPLETE")
    age_seconds = (now - facebook.fetched_at).total_seconds()
    if age_seconds < -1 or age_seconds > request.max_age_seconds:
        raise ValueError("FB_ACCOUNT_OWNERSHIP_SOURCE_STALE")

    account_claims = tuple(
        claim
        for claim in request.claims
        if claim.source is SourceSystem.FACEBOOK
        and claim.subject.kind is SubjectKind.ACCOUNT
    )
    claim_account_ids = {claim.subject.subject_id for claim in account_claims}
    if len(claim_account_ids) != 1 or set(request.account_ids) != claim_account_ids:
        raise ValueError("FB_ACCOUNT_OWNERSHIP_ACCOUNT_MISMATCH")
    if any(
        claim.window is not None and claim.window not in request.windows
        for claim in account_claims
    ):
        raise ValueError("FB_ACCOUNT_OWNERSHIP_WINDOW_MISMATCH")
    outcome_claims = tuple(
        claim
        for claim in request.claims
        if claim.source in {SourceSystem.AMO, SourceSystem.CDP_ERP}
    )
    if not outcome_claims or any(
        claim.window is None or claim.window not in request.windows
        for claim in outcome_claims
    ):
        raise ValueError("FB_ACCOUNT_OWNERSHIP_WINDOW_MISMATCH")
    account_id = next(iter(claim_account_ids))

    candidates = _outcome_candidate_ad_ids(request)
    if not candidates:
        raise ValueError("FB_ACCOUNT_OWNERSHIP_NO_CANDIDATES")
    if len(candidates) > _MAX_OWNERSHIP_CANDIDATE_IDS:
        raise ValueError("FB_ACCOUNT_OWNERSHIP_TOO_MANY_CANDIDATES")
    try:
        owned = load_account_ad_ownership(account_id, candidates)
    except Exception as exc:
        detail = (
            str(exc)
            if isinstance(exc, FacebookEvidenceError)
            else type(exc).__name__.upper()
        )
        raise ValueError(f"FB_ACCOUNT_OWNERSHIP_UNRESOLVED_{detail}"[:120]) from exc
    if not owned:
        raise ValueError("FB_ACCOUNT_OWNERSHIP_EMPTY")
    if len(owned) > _MAX_REQUEST_OBJECT_IDS:
        raise ValueError("FB_ACCOUNT_OWNERSHIP_TOO_LARGE")
    return tuple(sorted(owned))


def _record_state(record: EvidenceRecord) -> dict[str, object]:
    return {
        "category": record.category,
        "subject": record.subject,
        "metric": record.metric,
        "value": record.value,
        "source": record.source,
        "state": record.state,
        "window": record.window,
        "currency": record.currency,
        "entity_ids": record.entity_ids,
    }


def _source_digest(source: SourceEvidence) -> str:
    record_payloads = sorted(
        canonical_json(_record_state(item)) for item in source.records
    )
    payment_payloads = sorted(
        canonical_json(
            {
                "payment_id": item.payment_id,
                "contract_number": item.contract_number,
                "amo_lead_id": item.amo_lead_id,
                "fb_ad_id": item.fb_ad_id,
                "amount_lcy": item.amount_lcy,
                "direction": item.direction,
                "contract_net_lcy": item.contract_net_lcy,
                "document_at": item.document_at,
            }
        )
        for item in source.payments
    )
    payload = {
        "source": source.source,
        "records": tuple(item.decode("utf-8") for item in record_payloads),
        "payments": tuple(item.decode("utf-8") for item in payment_payloads),
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _freshness(
    source: SourceEvidence, now: datetime, max_age_seconds: int
) -> SourceFreshness:
    _aware(source.fetched_at, "source.fetched_at")
    observed = max(
        (record.observed_at for record in source.records),
        default=source.fetched_at,
    )
    age_seconds = (now - source.fetched_at).total_seconds()
    fresh = (
        source.state is EvidenceState.FRESH_COMPLETE
        and source.complete
        and -1 <= age_seconds <= max_age_seconds
    )
    return SourceFreshness(
        source=source.source,
        fetched_at=source.fetched_at,
        observed_at=observed,
        data_as_of=source.data_as_of,
        max_age_seconds=max_age_seconds,
        from_cache=source.from_cache,
        complete=source.complete,
        fresh=fresh,
    )


def _assemble_bundle(
    sources: tuple[SourceEvidence, ...],
    now: datetime,
    max_age_seconds: int,
    *,
    facebook_digest: str | None = None,
) -> EvidenceBundle:
    by_source = {source.source: source for source in sources}
    digests = {
        source: _source_digest(evidence) for source, evidence in by_source.items()
    }
    empty_digests = {
        source: _source_digest(
            SourceEvidence(
                source=source,
                state=EvidenceState.FRESH_COMPLETE,
                fetched_at=now,
                data_as_of=now,
                from_cache=False,
                complete=True,
                records=(),
            )
        )
        for source in _ACTION_SOURCES
        if source not in digests
    }
    digests.update(empty_digests)
    if facebook_digest is not None:
        digests[SourceSystem.FACEBOOK] = facebook_digest
    component_hashes = tuple(digests[source] for source in _ACTION_SOURCES)
    local_items = tuple(
        sorted(
            (source.value, digest)
            for source, digest in digests.items()
            if source not in _ACTION_SOURCE_SET
        )
    )
    return EvidenceBundle(
        loaded_at=now,
        sources=sources,
        freshness=tuple(_freshness(source, now, max_age_seconds) for source in sources),
        facebook_sha256=component_hashes[0],
        trello_sha256=component_hashes[1],
        media_sha256=component_hashes[2],
        amo_sha256=component_hashes[3],
        cdp_sha256=component_hashes[4],
        final_live_state_sha256=hashlib.sha256(
            canonical_json(component_hashes)
        ).hexdigest(),
        local_state_sha256=(
            hashlib.sha256(canonical_json(local_items)).hexdigest()
            if local_items
            else None
        ),
    )


def load_evidence(
    request: EvidenceRequest,
    now: datetime | None = None,
) -> EvidenceBundle:
    """Ограниченно читает только enum-маршруты из ``required_sources``."""

    checked_at = now or datetime.now(timezone.utc)
    _validate_request(request, checked_at)
    facebook_first: SourceEvidence | None = None
    outcome_request: EvidenceRequest | None = None
    outcome_error_code: str | None = None
    if _needs_account_outcome_enrichment(request):
        try:
            facebook_first = _dispatch_source(
                SourceSystem.FACEBOOK, request, checked_at
            )
        except Exception as exc:
            facebook_first = _source_error(
                SourceSystem.FACEBOOK,
                checked_at,
                f"SOURCE_READ_{type(exc).__name__.upper()}",
            )
        if facebook_first.source is not SourceSystem.FACEBOOK:
            facebook_first = _source_error(
                SourceSystem.FACEBOOK, checked_at, "SOURCE_ROUTING_MISMATCH"
            )
        try:
            exact_ad_ids = _account_outcome_ad_ids(request, facebook_first, checked_at)
            outcome_request = replace(request, ad_ids=exact_ad_ids)
        except ValueError as exc:
            outcome_error_code = str(exc)[:120]
        except Exception as exc:
            # Любой сбой чтения (сеть, провайдер) — INCOMPLETE, а не исключение
            # наружу: без доказанного universe AMO/CDP считать нельзя.
            outcome_error_code = (
                f"FB_ACCOUNT_OWNERSHIP_READ_{type(exc).__name__.upper()}"[:120]
            )

    sources: list[SourceEvidence] = []
    for source in request.required_sources:
        if source is SourceSystem.FACEBOOK and facebook_first is not None:
            evidence = facebook_first
        elif (
            source in {SourceSystem.AMO, SourceSystem.CDP_ERP}
            and facebook_first is not None
            and outcome_request is None
        ):
            evidence = _source_incomplete(
                source,
                checked_at,
                outcome_error_code or "FB_ACCOUNT_OWNERSHIP_SOURCE_INCOMPLETE",
            )
        else:
            source_request = (
                outcome_request
                if source in {SourceSystem.AMO, SourceSystem.CDP_ERP}
                and outcome_request is not None
                else request
            )
            try:
                evidence = _dispatch_source(source, source_request, checked_at)
            except Exception as exc:
                evidence = _source_error(
                    source,
                    checked_at,
                    f"SOURCE_READ_{type(exc).__name__.upper()}",
                )
        if evidence.source is not source:
            evidence = _source_error(source, checked_at, "SOURCE_ROUTING_MISMATCH")
        sources.append(evidence)
    return _assemble_bundle(tuple(sources), checked_at, request.max_age_seconds)


def _action_subjects(item: ActionManifest) -> tuple[SubjectRef, ...]:
    if isinstance(item, LaunchManifest):
        media_subjects = tuple(
            SubjectRef(SubjectKind.MEDIA, asset.asset_id, item.manifest_id)
            for asset in item.media_assets
        )
        body_subjects = tuple(
            SubjectRef(SubjectKind.MEDIA, f"body:{body_sha}", item.manifest_id)
            for body_sha in dict.fromkeys(
                creative.body_sha256
                for destination in item.destinations
                for creative in destination.creatives
            )
        )
        return (
            SubjectRef(SubjectKind.CARD, item.trello.card_id),
            *media_subjects,
            *body_subjects,
            *(
                SubjectRef(
                    SubjectKind.ADSET, destination.adset_id, destination.account_id
                )
                for destination in item.destinations
            ),
        )
    if isinstance(item, (PauseManifest, UnpauseManifest)):
        ad_ids = [item.ad_id]
        if isinstance(item, PauseManifest):
            ad_ids.extend(item.sibling_active_ids)
            if item.replacement_ad_id is not None:
                ad_ids.append(item.replacement_ad_id)
        return (
            SubjectRef(SubjectKind.AD, item.ad_id, item.adset_id),
            SubjectRef(SubjectKind.ADSET, item.adset_id),
            *(
                SubjectRef(SubjectKind.AD, ad_id, item.adset_id)
                for ad_id in dict.fromkeys(ad_ids[1:])
            ),
        )
    if isinstance(item, AssetRecoveryManifest):
        return (
            SubjectRef(SubjectKind.AD, item.source_ad_id, item.source_adset_id),
            SubjectRef(SubjectKind.ADSET, item.source_adset_id, item.account_id),
            SubjectRef(SubjectKind.ADSET, item.target_adset_id, item.account_id),
        )
    return (
        SubjectRef(SubjectKind.ADSET, item.adset_id),
        *(
            SubjectRef(SubjectKind.AD, ad_id, item.adset_id)
            for ad_id in item.candidate_ad_ids
        ),
    )


def _launch_paths(item: LaunchManifest) -> tuple[str, ...]:
    configured_root = Path(config.REPORT_CHECKER_STAGING_ROOT).expanduser().resolve()
    manifest_root = Path(item.staging_root).expanduser().resolve()
    manifest_directory = Path(item.staging_directory).expanduser().resolve()
    # manifest_id по-целевого манифеста имеет вид «<staging_uuid>:<ordinal>»:
    # суффикс добавляет разворот на exact claims (agent/launcher, один claim на
    # будущее объявление), а staged-каталог у всего предложения ОДИН — под
    # базовым uuid. Сверка с полным id не сходилась никогда, и ни один запуск
    # не прошёл live review за всю историю контура.
    base_manifest_id = item.manifest_id.split(":", 1)[0]
    if not base_manifest_id or "/" in base_manifest_id or base_manifest_id in (".", ".."):
        raise ValueError("LAUNCH manifest_id не безопасен для пути staging")
    expected_directory = configured_root / base_manifest_id
    if manifest_root != configured_root or manifest_directory != expected_directory:
        raise ValueError("LAUNCH staging root/directory не совпадают с закрытым root")
    relative_paths = [asset.staged_relative_path for asset in item.media_assets]
    relative_paths.extend(
        dict.fromkeys(
            creative.body_staged_relative_path
            for destination in item.destinations
            for creative in destination.creatives
        )
    )
    result: list[str] = []
    for raw_path in relative_paths:
        relative = PurePosixPath(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("LAUNCH staged path небезопасен")
        # Префикс — база id: файлы лежат в root/<staging_uuid>/…, каталога
        # «uuid:ordinal» на диске не существует.
        result.append((PurePosixPath(base_manifest_id) / relative).as_posix())
    return tuple(result)


def _action_windows(item: ActionManifest, now: datetime) -> tuple[TimeWindow, ...]:
    if isinstance(item, PauseManifest):
        return (item.decision_window,)
    if isinstance(item, ScaleManifest):
        return tuple(dict.fromkeys((item.facebook_window, item.outcome_window)))
    # AMO/CDP adapters требуют явное exact окно даже если у action нет business claims.
    return (
        TimeWindow(
            start=item.prepared_at - timedelta(seconds=1),
            end=now + timedelta(microseconds=1),
            timezone_name="UTC",
            semantic="action_precondition",
        ),
    )


def _action_request(item: ActionManifest, now: datetime) -> EvidenceRequest:
    subjects = _action_subjects(item)
    if isinstance(item, LaunchManifest):
        accounts = tuple(
            dict.fromkeys(destination.account_id for destination in item.destinations)
        )
        adsets = tuple(
            dict.fromkeys(destination.adset_id for destination in item.destinations)
        )
        ad_ids: tuple[str, ...] = ()
        card_ids = (item.trello.card_id,)
        staged_paths = _launch_paths(item)
        claims = ()
    elif isinstance(item, PauseManifest):
        accounts = ()
        adsets = (item.adset_id,)
        ad_ids = tuple(
            dict.fromkeys(
                (
                    item.ad_id,
                    *item.sibling_active_ids,
                    *((item.replacement_ad_id,) if item.replacement_ad_id else ()),
                )
            )
        )
        card_ids = ()
        staged_paths = ()
        claims = item.facts
    elif isinstance(item, UnpauseManifest):
        accounts = ()
        adsets = (item.adset_id,)
        ad_ids = (item.ad_id,)
        card_ids = ()
        staged_paths = ()
        claims = ()
    elif isinstance(item, AssetRecoveryManifest):
        accounts = (item.account_id,)
        adsets = tuple(dict.fromkeys((item.source_adset_id, item.target_adset_id)))
        ad_ids = (item.source_ad_id,)
        card_ids = ()
        staged_paths = ()
        claims = ()
    else:
        accounts = ()
        adsets = (item.adset_id,)
        ad_ids = item.candidate_ad_ids
        card_ids = ()
        staged_paths = ()
        claims = item.facts
    return EvidenceRequest(
        request_id=f"action:{item.manifest_id}",
        purpose="ACTION",
        action_kind=item.kind,
        generated_at=now,
        subjects=subjects,
        claims=claims,
        required_sources=(
            (SourceSystem.FACEBOOK,)
            if isinstance(item, AssetRecoveryManifest)
            else _ACTION_SOURCES
        ),
        windows=_action_windows(item, now),
        account_ids=accounts,
        adset_ids=adsets,
        ad_ids=ad_ids,
        card_ids=card_ids,
        staged_relative_paths=staged_paths,
        include_full_inventory=True,
        force_live=True,
        max_age_seconds=1,
    )


def _replace_source(
    sources: tuple[SourceEvidence, ...], replacement: SourceEvidence
) -> tuple[SourceEvidence, ...]:
    return tuple(
        replacement if source.source is replacement.source else source
        for source in sources
    )


def _incomplete(source: SourceEvidence, code: str) -> SourceEvidence:
    return replace(
        source,
        state=EvidenceState.INCOMPLETE,
        complete=False,
        records=(),
        payments=(),
        error_code=code,
    )


def _validate_launch_inputs(
    item: LaunchManifest,
    sources: tuple[SourceEvidence, ...],
) -> tuple[SourceEvidence, ...]:
    by_source = {source.source: source for source in sources}
    trello = by_source[SourceSystem.TRELLO]
    if trello.complete:
        record = next(
            (
                row
                for row in trello.records
                if row.subject == SubjectRef(SubjectKind.CARD, item.trello.card_id)
                and row.metric is Metric.MATCH_STATE
            ),
            None,
        )
        # Галочка и дата активности дрейфом не считаются (см. integrations.trello.load_card_snapshot).
        required_entities = {
            f"board:{item.trello.board_id}",
            f"list:{item.trello.expected_list_id}",
            f"closed:{str(item.trello.expected_closed).lower()}",
            f"attachments:{item.trello.attachment_manifest_sha256}",
            f"labels:{item.trello.labels_sha256}",
        }
        content_matches = record is not None and (
            record.value == item.trello.card_content_sha256
            # манифест собран по старой схеме отпечатка
            or f"content-legacy:{item.trello.card_content_sha256}" in record.entity_ids
        )
        if record is None or not content_matches or not required_entities.issubset(record.entity_ids):
            # Дрейф карточки — терминальный отказ, а не «источник недоступен»: раньше источник
            # помечался INCOMPLETE, исполнитель считал это транзиентом и молча крутил задание до
            # дедлайна (48 ч). Теперь запись MATCH_STATE со значением DRIFT доходит до правил
            # (approval_rules._launch_issues → LAUNCH_INPUT_DRIFT) и закрывает задание с сообщением.
            drifted = tuple(
                replace(row, value="DRIFT")
                if row.subject == SubjectRef(SubjectKind.CARD, item.trello.card_id)
                and row.metric is Metric.MATCH_STATE
                else row
                for row in trello.records
            )
            if record is None:
                drifted = (
                    *drifted,
                    EvidenceRecord(
                        FactCategory.MATCH,
                        SubjectRef(SubjectKind.CARD, item.trello.card_id),
                        Metric.MATCH_STATE,
                        "DRIFT",
                        SourceSystem.TRELLO,
                        trello.state,
                        trello.fetched_at,
                        None,
                        None,
                    ),
                )
            sources = _replace_source(sources, replace(trello, records=drifted))

    media = by_source[SourceSystem.MEDIA_BYTES]
    if media.complete:
        asset_records = {
            record.subject.subject_id: record
            for record in media.records
            if record.metric is Metric.MATCH_STATE
        }
        body_records = {
            record.subject.subject_id: record
            for record in media.records
            if record.metric is Metric.DISPLAY_CONTEXT
        }
        valid = all(
            asset_records.get(asset.asset_id) is not None
            and asset_records[asset.asset_id].value == asset.content_sha256
            for asset in item.media_assets
        )
        bodies = {
            creative.body_staged_relative_path: creative.body_sha256
            for destination in item.destinations
            for creative in destination.creatives
        }
        valid = valid and all(
            body_records.get(f"body:{body_sha}") is not None
            and body_records[f"body:{body_sha}"].value == body_sha
            for body_sha in bodies.values()
        )
        if len(bodies) == 1:
            body_path, body_sha = next(iter(bodies.items()))
            manifest_digest = hashlib.sha256(
                canonical_json(
                    {
                        "assets": item.media_assets,
                        "body_relative_path": body_path,
                        "body_sha256": body_sha,
                    }
                )
            ).hexdigest()
            valid = valid and manifest_digest == item.media_manifest_sha256
        else:
            valid = False
        if not valid:
            sources = _replace_source(
                sources, _incomplete(media, "MEDIA_MANIFEST_DRIFT")
            )
    return sources


def load_action_item_evidence(
    batch: ActionBatchManifest,
    item: ActionManifest,
    item_index: int,
    now: datetime | None = None,
) -> EvidenceBundle:
    """Загружает новый manifest-aware five-source snapshot ровно для одного item."""

    checked_at = now or datetime.now(timezone.utc)
    _aware(checked_at, "now")
    if (
        item_index < 0
        or item_index >= len(batch.actions)
        or batch.actions[item_index] != item
    ):
        raise ValueError("Item не находится на заявленной позиции batch")
    request = _action_request(item, checked_at)
    first_observation = None
    precondition_error: Exception | None = None
    try:
        # Первый read ограничивает пяти-source snapshot с левой стороны.
        first_observation = read_action_precondition(item, checked_at)
    except Exception as exc:
        precondition_error = exc
    bundle = load_evidence(request, checked_at)
    sources = bundle.sources
    facebook = next(
        source for source in sources if source.source is SourceSystem.FACEBOOK
    )
    facebook_digest: str | None = None
    if (
        facebook.complete
        and precondition_error is None
        and first_observation is not None
    ):
        try:
            # Второй read доказывает отсутствие provider drift, пока читались
            # Trello/media/AMO/CDP.
            observation = read_action_precondition(item, checked_at)
            if observation.digest != first_observation.digest:
                raise ValueError("FB_STATE_CHANGED_DURING_EVIDENCE_LOAD")
            facebook_digest = observation.digest
            if isinstance(item, LaunchManifest):
                duplicate_records = tuple(
                    EvidenceRecord(
                        category=FactCategory.MATCH,
                        subject=SubjectRef(
                            SubjectKind.ADSET,
                            destination.adset_id,
                            destination.account_id,
                        ),
                        metric=Metric.MATCH_STATE,
                        value="NO_DUPLICATE",
                        source=SourceSystem.FACEBOOK,
                        state=EvidenceState.FRESH_COMPLETE,
                        observed_at=checked_at,
                        window=None,
                        currency=None,
                        entity_ids=(
                            f"signature:{destination.duplicate_signature}",
                            f"precondition:{observation.digest}",
                        ),
                    )
                    for destination in item.destinations
                )
                facebook = replace(
                    facebook, records=(*facebook.records, *duplicate_records)
                )
                sources = _replace_source(sources, facebook)
        except Exception as exc:
            facebook = _incomplete(
                facebook,
                f"FB_MANIFEST_PRECONDITION_{type(exc).__name__.upper()}",
            )
            sources = _replace_source(sources, facebook)
    elif facebook.complete:
        facebook = _incomplete(
            facebook,
            "FB_MANIFEST_PRECONDITION_"
            + (
                type(precondition_error).__name__.upper()
                if precondition_error is not None
                else "MISSING"
            ),
        )
        sources = _replace_source(sources, facebook)
    if isinstance(item, LaunchManifest):
        sources = _validate_launch_inputs(item, sources)
    return _assemble_bundle(
        sources,
        checked_at,
        request.max_age_seconds,
        facebook_digest=facebook_digest,
    )
