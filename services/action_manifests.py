"""Точные фабрики immutable-манифестов для approval gateway.

Модуль не читает внешние источники и не принимает callbacks. Он только
переносит уже подготовленные типизированные данные в закрытые contracts.
"""

from __future__ import annotations

import json
import hashlib
import uuid
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from services.approval_checker_models import (
    AdStatusSnapshot,
    ActionBatchManifest,
    AssetRecoveryManifest,
    ActionKind,
    ActionManifest,
    ActionOrigin,
    FactCategory,
    FactClaim,
    LaunchManifest,
    LaunchDestination,
    CreativeSpec,
    MediaAssetSpec,
    MediaType,
    PlacementRole,
    TrelloPrecondition,
    Metric,
    PauseCandidate,
    PauseManifest,
    PreparedLaunch,
    ScaleCandidate,
    ScaleManifest,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
    UnpauseCandidate,
    UnpauseManifest,
    canonical_json,
    manifest_sha256,
)


def _ad_status_snapshot_from_payload(payload: dict) -> AdStatusSnapshot:
    return AdStatusSnapshot(
        ad_id=payload["ad_id"],
        adset_id=payload["adset_id"],
        configured_status=payload["configured_status"],
        effective_status=payload["effective_status"],
    )


def _time_window_from_payload(payload: dict | None) -> TimeWindow | None:
    if payload is None:
        return None
    return TimeWindow(
        start=datetime.fromisoformat(payload["start"]),
        end=datetime.fromisoformat(payload["end"]),
        timezone_name=payload["timezone_name"],
        semantic=payload["semantic"],
    )


def _claim_from_payload(payload: dict) -> FactClaim:
    raw_value = payload["value"]
    metric = Metric(payload["metric"])
    if metric in {Metric.SPEND, Metric.REVENUE, Metric.DAILY_BUDGET}:
        raw_value = Decimal(str(raw_value))
    return FactClaim(
        claim_id=payload["claim_id"],
        field_id=payload.get("field_id"),
        category=FactCategory(payload["category"]),
        subject=SubjectRef(
            SubjectKind(payload["subject"]["kind"]),
            payload["subject"]["subject_id"],
            payload["subject"].get("parent_id"),
        ),
        metric=metric,
        value=raw_value,
        source=SourceSystem(payload["source"]),
        window=_time_window_from_payload(payload.get("window")),
        currency=payload.get("currency"),
        required=payload.get("required", True),
        tolerance=Decimal(str(payload.get("tolerance", "0"))),
    )


def manifest_from_operation_record(record, expected_type):
    """Восстанавливает typed PAUSE/UNPAUSE/SCALE из sealed WAL и rehash-ит."""
    if record.action_manifest_json is None or record.action_manifest_payload_sha256 is None:
        raise ValueError("WAL не содержит immutable action manifest")
    raw = record.action_manifest_json.encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != record.action_manifest_payload_sha256:
        raise ValueError("WAL payload hash не совпадает")
    payload = json.loads(record.action_manifest_json)
    common = {
        "kind": ActionKind(payload["kind"]),
        "manifest_id": payload["manifest_id"],
        "origin": ActionOrigin(payload["origin"]),
        "idempotency_key": payload["idempotency_key"],
        "prepared_at": datetime.fromisoformat(payload["prepared_at"]),
    }
    if expected_type is LaunchManifest:
        trello_payload = payload["trello"]
        trello = TrelloPrecondition(
            card_id=trello_payload["card_id"],
            board_id=trello_payload["board_id"],
            ready_list_id=trello_payload["ready_list_id"],
            expected_list_id=trello_payload["expected_list_id"],
            expected_due_complete=trello_payload["expected_due_complete"],
            expected_closed=trello_payload["expected_closed"],
            date_last_activity=datetime.fromisoformat(trello_payload["date_last_activity"]),
            attachment_ids=tuple(trello_payload["attachment_ids"]),
            attachment_manifest_sha256=trello_payload["attachment_manifest_sha256"],
            labels_sha256=trello_payload["labels_sha256"],
            card_content_sha256=trello_payload["card_content_sha256"],
        )
        assets = tuple(
            MediaAssetSpec(
                asset_id=item["asset_id"], order_index=item["order_index"],
                media_type=MediaType(item["media_type"]),
                placement_group_id=item.get("placement_group_id"),
                placement_role=PlacementRole(item["placement_role"]),
                staged_relative_path=item["staged_relative_path"],
                original_attachment_id=item["original_attachment_id"],
                mime_type=item["mime_type"], size_bytes=item["size_bytes"],
                content_sha256=item["content_sha256"],
            )
            for item in payload["media_assets"]
        )
        destinations = []
        for destination in payload["destinations"]:
            creatives = tuple(
                CreativeSpec(
                    creative_id=item["creative_id"], order_index=item["order_index"],
                    ad_name=item["ad_name"], media_asset_ids=tuple(item["media_asset_ids"]),
                    body_staged_relative_path=item["body_staged_relative_path"],
                    body_sha256=item["body_sha256"], product=item["product"],
                    page_id=item["page_id"], lead_form_id=item.get("lead_form_id"),
                    call_to_action=item["call_to_action"],
                    instagram_actor_id=item["instagram_actor_id"], title=item["title"],
                    link_url=item.get("link_url"),
                    expected_configured_status=item["expected_configured_status"],
                )
                for item in destination["creatives"]
            )
            destinations.append(
                LaunchDestination(
                    city=destination["city"], account_id=destination["account_id"],
                    adset_id=destination["adset_id"], adset_type=destination["adset_type"],
                    current_daily_budget=Decimal(str(destination["current_daily_budget"])),
                    currency=destination["currency"],
                    capacity_available=destination["capacity_available"],
                    hard_reserve_slots=destination["hard_reserve_slots"],
                    creatives=creatives,
                    duplicate_signature=destination["duplicate_signature"],
                    replacement_workflow_id=destination.get("replacement_workflow_id"),
                )
            )
        manifest = LaunchManifest(
            **common,
            config_version_sha256=payload["config_version_sha256"],
            staging_root=payload["staging_root"],
            staging_directory=payload["staging_directory"],
            trello=trello,
            card_name_sha256=payload["card_name_sha256"],
            media_manifest_sha256=payload["media_manifest_sha256"],
            media_assets=assets,
            campaign_type=payload["campaign_type"],
            destinations=tuple(destinations),
        )
    elif expected_type is PauseManifest:
        manifest = PauseManifest(
            **common,
            ad_id=payload["ad_id"], adset_id=payload["adset_id"],
            reason_code=payload["reason_code"],
            expected_before_status=payload["expected_before_status"],
            expected_after_status=payload["expected_after_status"],
            decision_window=_time_window_from_payload(payload["decision_window"]),
            facts=tuple(_claim_from_payload(item) for item in payload["facts"]),
            pre_inventory_sha256=payload["pre_inventory_sha256"],
            sibling_active_ids=tuple(payload["sibling_active_ids"]),
            pre_unrelated_inventory_sha256=payload["pre_unrelated_inventory_sha256"],
            sibling_status_snapshot=tuple(
                _ad_status_snapshot_from_payload(item)
                for item in payload["sibling_status_snapshot"]
            ),
            replacement_ad_id=payload.get("replacement_ad_id"),
        )
    elif expected_type is UnpauseManifest:
        manifest = UnpauseManifest(
            **common, ad_id=payload["ad_id"], adset_id=payload["adset_id"],
            expected_before_status=payload["expected_before_status"],
            expected_after_status=payload["expected_after_status"],
            pre_inventory_sha256=payload["pre_inventory_sha256"],
            pre_unrelated_inventory_sha256=payload["pre_unrelated_inventory_sha256"],
            sibling_status_snapshot=tuple(
                _ad_status_snapshot_from_payload(item)
                for item in payload["sibling_status_snapshot"]
            ),
        )
    elif expected_type is ScaleManifest:
        manifest = ScaleManifest(
            **common, adset_id=payload["adset_id"],
            expected_status=payload["expected_status"],
            current_budget=Decimal(str(payload["current_budget"])),
            target_budget=Decimal(str(payload["target_budget"])),
            currency=payload["currency"],
            candidate_ad_ids=tuple(payload["candidate_ad_ids"]),
            facebook_window=_time_window_from_payload(payload["facebook_window"]),
            outcome_window=_time_window_from_payload(payload["outcome_window"]),
            facts=tuple(_claim_from_payload(item) for item in payload["facts"]),
        )
    else:
        raise TypeError("Неподдерживаемый WAL manifest type")
    expected_manifest_sha256 = getattr(record, "item_manifest_sha256", None)
    if type(manifest) is not expected_type or (
        expected_manifest_sha256 is not None
        and manifest_sha256(manifest) != expected_manifest_sha256
    ):
        raise ValueError("WAL typed manifest hash не совпадает")
    return manifest


def manifest_from_payload(
    payload: dict[str, object],
    expected_type: type[ActionManifest],
) -> ActionManifest:
    """Строго восстанавливает typed manifest из immutable proposal payload."""

    from types import SimpleNamespace

    raw = canonical_json(payload)
    record = SimpleNamespace(
        action_manifest_json=raw.decode("utf-8"),
        action_manifest_payload_sha256=hashlib.sha256(raw).hexdigest(),
        item_manifest_sha256=None,
    )
    return manifest_from_operation_record(record, expected_type)


def _manifest_id(kind: ActionKind, idempotency_key: str, subject_id: str) -> str:
    """Строит стабильный ID без названий, текста рекламы и иных PII."""

    seed = f"acme:{kind.value}:{idempotency_key}:{subject_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать timezone")


def _require_origin(origin: ActionOrigin) -> None:
    if not isinstance(origin, ActionOrigin):
        raise ValueError("origin должен быть ActionOrigin")


def _require_non_negative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} должен быть целым неотрицательным")


def _require_non_negative_decimal(value: Decimal, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{field_name} должен быть конечным неотрицательным Decimal")


def build_launch_manifest(
    prepared: PreparedLaunch,
    *,
    origin: ActionOrigin,
    idempotency_key: str,
    now: datetime,
) -> LaunchManifest:
    """Фиксирует ровно staged launch без повторного discovery."""

    _require_aware(now)
    _require_origin(origin)
    if now < prepared.staged_at:
        raise ValueError("manifest нельзя подготовить раньше staging")
    return LaunchManifest(
        kind=ActionKind.LAUNCH,
        manifest_id=prepared.manifest_id,
        origin=origin,
        idempotency_key=idempotency_key,
        prepared_at=now,
        config_version_sha256=prepared.config_version_sha256,
        staging_root=str(prepared.staging_root),
        staging_directory=str(prepared.staging_directory),
        trello=prepared.trello,
        card_name_sha256=prepared.card_name_sha256,
        media_manifest_sha256=prepared.media_manifest_sha256,
        media_assets=tuple(prepared.media_assets),
        campaign_type=prepared.campaign_type,
        destinations=tuple(prepared.destinations),
    )


def _pause_facts(candidate: PauseCandidate, manifest_id: str) -> tuple[FactClaim, ...]:
    subject = SubjectRef(SubjectKind.AD, candidate.ad_id, candidate.adset_id)
    payment_contracts = {
        payment.contract_number
        for payment in candidate.payments
        if payment.contract_net_lcy > 0
    }
    return (
        FactClaim(
            claim_id=f"{manifest_id}:fb-status",
            field_id=None,
            category=FactCategory.ACTION_STATE,
            subject=subject,
            metric=Metric.EFFECTIVE_STATUS,
            value=candidate.expected_before_status,
            source=SourceSystem.FACEBOOK,
            window=None,
        ),
        FactClaim(
            claim_id=f"{manifest_id}:fb-spend",
            field_id=None,
            category=FactCategory.BUSINESS_METRIC,
            subject=subject,
            metric=Metric.SPEND,
            value=candidate.spend,
            source=SourceSystem.FACEBOOK,
            window=candidate.decision_window,
            currency=candidate.spend_currency,
        ),
        FactClaim(
            claim_id=f"{manifest_id}:fb-leads",
            field_id=None,
            category=FactCategory.BUSINESS_METRIC,
            subject=subject,
            metric=Metric.LEADS,
            value=candidate.leads,
            source=SourceSystem.FACEBOOK,
            window=candidate.decision_window,
        ),
        FactClaim(
            claim_id=f"{manifest_id}:amo-quals",
            field_id=None,
            category=FactCategory.BUSINESS_METRIC,
            subject=subject,
            metric=Metric.QUALS,
            value=candidate.quals,
            source=SourceSystem.AMO,
            window=candidate.decision_window,
        ),
        FactClaim(
            claim_id=f"{manifest_id}:cdp-payments",
            field_id=None,
            category=FactCategory.BUSINESS_METRIC,
            subject=subject,
            metric=Metric.PAYMENTS,
            value=len(payment_contracts),
            source=SourceSystem.CDP_ERP,
            window=candidate.decision_window,
        ),
        FactClaim(
            claim_id=f"{manifest_id}:cdp-revenue",
            field_id=None,
            category=FactCategory.BUSINESS_METRIC,
            subject=subject,
            metric=Metric.REVENUE,
            value=candidate.revenue_lcy,
            source=SourceSystem.CDP_ERP,
            window=candidate.decision_window,
            currency="LCY",
        ),
    )


def build_pause_manifest(
    candidate: PauseCandidate,
    *,
    origin: ActionOrigin,
    idempotency_key: str,
    now: datetime,
) -> PauseManifest:
    """Создаёт PAUSE-манифест с exact ID и прямыми FB/AMO/CDP фактами."""

    _require_aware(now)
    _require_origin(origin)
    _require_non_negative_decimal(candidate.spend, "spend")
    _require_non_negative_decimal(candidate.revenue_lcy, "revenue_lcy")
    _require_non_negative_int(candidate.leads, "leads")
    _require_non_negative_int(candidate.quals, "quals")
    if candidate.quals > candidate.leads:
        raise ValueError("quals не может быть больше leads")
    payment_ids: set[str] = set()
    for payment in candidate.payments:
        if payment.payment_id in payment_ids:
            raise ValueError("payment_id в PAUSE candidate должен быть уникальным")
        payment_ids.add(payment.payment_id)
        if payment.fb_ad_id != candidate.ad_id:
            raise ValueError("payment должен быть связан с exact candidate.ad_id")
    if (
        any(payment.contract_net_lcy > 0 for payment in candidate.payments)
        and candidate.revenue_lcy <= 0
    ):
        raise ValueError("payments > 0 требует положительную revenue_lcy")

    manifest_id = _manifest_id(ActionKind.PAUSE, idempotency_key, candidate.ad_id)
    return PauseManifest(
        kind=ActionKind.PAUSE,
        manifest_id=manifest_id,
        origin=origin,
        idempotency_key=idempotency_key,
        prepared_at=now,
        ad_id=candidate.ad_id,
        adset_id=candidate.adset_id,
        reason_code=candidate.reason_code,
        expected_before_status=candidate.expected_before_status,
        expected_after_status=candidate.expected_after_status,
        decision_window=candidate.decision_window,
        facts=_pause_facts(candidate, manifest_id),
        pre_inventory_sha256=candidate.pre_inventory_sha256,
        sibling_active_ids=tuple(candidate.sibling_active_ids),
        pre_unrelated_inventory_sha256=candidate.pre_unrelated_inventory_sha256,
        sibling_status_snapshot=tuple(candidate.sibling_status_snapshot),
        replacement_ad_id=candidate.replacement_ad_id,
    )


def build_unpause_manifest(
    candidate: UnpauseCandidate,
    *,
    origin: ActionOrigin,
    idempotency_key: str,
    now: datetime,
) -> UnpauseManifest:
    """Создаёт точный обратимый UNPAUSE; DELETE здесь не представим."""

    _require_aware(now)
    _require_origin(origin)
    # Digest пока не входит в UnpauseManifest contract, но невалидный snapshot
    # нельзя пропускать даже на границе фабрики.
    if len(candidate.pre_inventory_sha256) != 64 or any(
        character not in "0123456789abcdef"
        for character in candidate.pre_inventory_sha256
    ):
        raise ValueError("pre_inventory_sha256 должен быть lowercase SHA-256")
    return UnpauseManifest(
        kind=ActionKind.UNPAUSE,
        manifest_id=_manifest_id(ActionKind.UNPAUSE, idempotency_key, candidate.ad_id),
        origin=origin,
        idempotency_key=idempotency_key,
        prepared_at=now,
        ad_id=candidate.ad_id,
        adset_id=candidate.adset_id,
        expected_before_status=candidate.expected_before_status,
        expected_after_status=candidate.expected_after_status,
        pre_inventory_sha256=candidate.pre_inventory_sha256,
        pre_unrelated_inventory_sha256=candidate.pre_unrelated_inventory_sha256,
        sibling_status_snapshot=tuple(candidate.sibling_status_snapshot),
    )


def build_scale_manifest(
    candidate: ScaleCandidate,
    *,
    origin: ActionOrigin,
    idempotency_key: str,
    now: datetime,
) -> ScaleManifest:
    """Фиксирует budget change и уже собранные exact candidate facts."""

    _require_aware(now)
    _require_origin(origin)
    if not candidate.candidate_ad_ids or len(candidate.candidate_ad_ids) != len(
        set(candidate.candidate_ad_ids)
    ):
        raise ValueError("candidate_ad_ids должны быть непустыми и уникальными")
    direct_sources = {claim.source for claim in candidate.facts}
    required_sources = {
        SourceSystem.FACEBOOK,
        SourceSystem.AMO,
        SourceSystem.CDP_ERP,
    }
    if not required_sources.issubset(direct_sources):
        raise ValueError("SCALE facts должны включать Facebook, AMO и CDP")
    for claim in candidate.facts:
        expected_window = (
            candidate.facebook_window
            if claim.source is SourceSystem.FACEBOOK
            else candidate.outcome_window
        )
        if claim.window is not None and claim.window != expected_window:
            raise ValueError("SCALE fact использует окно вне candidate contract")
    return ScaleManifest(
        kind=ActionKind.SCALE,
        manifest_id=_manifest_id(ActionKind.SCALE, idempotency_key, candidate.adset_id),
        origin=origin,
        idempotency_key=idempotency_key,
        prepared_at=now,
        adset_id=candidate.adset_id,
        expected_status=candidate.expected_status,
        current_budget=candidate.current_budget,
        target_budget=candidate.target_budget,
        currency=candidate.currency,
        candidate_ad_ids=tuple(candidate.candidate_ad_ids),
        facebook_window=candidate.facebook_window,
        outcome_window=candidate.outcome_window,
        facts=tuple(candidate.facts),
    )


def _subject_id(action: ActionManifest) -> str:
    if isinstance(action, LaunchManifest):
        return f"card:{action.trello.card_id}"
    if isinstance(action, (PauseManifest, UnpauseManifest)):
        return f"ad:{action.ad_id}"
    if isinstance(action, AssetRecoveryManifest):
        return f"adset:{action.target_adset_id}"
    if isinstance(action, ScaleManifest):
        return f"adset:{action.adset_id}"
    raise TypeError(f"Неподдерживаемый action manifest: {type(action).__name__}")


def build_action_batch(
    manifests: tuple[ActionManifest, ...],
    *,
    correlation_id: str,
    idempotency_key: str,
    now: datetime,
) -> ActionBatchManifest:
    """Связывает однородные actions с одним batch idempotency key и digest."""

    _require_aware(now)
    actions = tuple(manifests)
    batch_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"acme:batch:{idempotency_key}:{correlation_id}",
        )
    )
    draft = ActionBatchManifest(
        batch_manifest_id=batch_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        prepared_at=now,
        subject_ids=tuple(_subject_id(action) for action in actions),
        actions=actions,
        manifest_sha256="0" * 64,
    )
    return replace(draft, manifest_sha256=manifest_sha256(draft))
