"""Сборка манифеста из одобренного намерения и живого состояния.

Продюсер кладёт в предложение бизнес-намерение, а не сериализованный манифест.
Старый исполнитель пытался «восстановить» манифест из намерения и падал с
KeyError('kind') на каждом одобрении владельца. Здесь проверяется, что манифест
именно собирается по живым данным, а расхождение живого состояния с одобренным
отличимо от бага сборки.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services import owner_action_live_manifest as live
from services.adset_pause_guard import inventory_state_sha256
from services.approval_checker_models import (
    ActionOrigin,
    EvidenceRecord,
    EvidenceState,
    FactCategory,
    Metric,
    PauseManifest,
    ScaleManifest,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    UnpauseManifest,
)
from services.owner_action_models import ProposalKind, ProposalOrigin


NOW = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
KEY = str(uuid.uuid4())
AD_ID = "700100"
SIBLING_ID = "700200"
ADSET_ID = "800100"
ACCOUNT_ID = "900100"


def _rows(*, target_effective: str = "ACTIVE") -> dict[str, dict[str, str]]:
    return {
        AD_ID: {
            "ad_id": AD_ID,
            "adset_id": ADSET_ID,
            "configured_status": target_effective,
            "effective_status": target_effective,
        },
        SIBLING_ID: {
            "ad_id": SIBLING_ID,
            "adset_id": ADSET_ID,
            "configured_status": "ACTIVE",
            "effective_status": "ACTIVE",
        },
    }


def _inventory(
    *,
    target_effective: str = "ACTIVE",
    sibling_effective: str = "ACTIVE",
    complete: bool = True,
) -> dict:
    rows = _rows(target_effective=target_effective)
    rows[SIBLING_ID]["configured_status"] = sibling_effective
    rows[SIBLING_ID]["effective_status"] = sibling_effective
    return {
        "adset_id": ADSET_ID,
        "active_ids": {
            ad_id for ad_id, row in rows.items() if row["effective_status"] == "ACTIVE"
        },
        "candidate_context": {AD_ID: rows[AD_ID]},
        "inventory_context": rows,
        "state_sha256": inventory_state_sha256(
            rows.values(), expected_adset_id=ADSET_ID
        ),
        "complete": complete,
        "pages_read": 1,
    }


def _patch_inventory(monkeypatch, inventory: dict | None, *, other_adset: bool = False):
    def _fetch(ad_ids):
        if inventory is None:
            return {}
        key = "999999" if other_adset else ADSET_ID
        return {key: inventory}

    monkeypatch.setattr(live, "fetch_pause_inventory", _fetch)


def _record(subject, metric, value, source, window, currency=None):
    return EvidenceRecord(
        category=FactCategory.BUSINESS_METRIC,
        subject=subject,
        metric=metric,
        value=value,
        source=source,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=NOW,
        window=window,
        currency=currency,
    )


def _source(system, records) -> SourceEvidence:
    return SourceEvidence(
        source=system,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=NOW,
        data_as_of=NOW,
        from_cache=False,
        complete=True,
        records=tuple(records),
    )


def _patch_pause_sources(monkeypatch, *, leads: int = 4, quals: int = 1) -> None:
    # Таймзона кабинета: дневное окно строится до чтения источников.
    from services import approval_source_facebook
    monkeypatch.setattr(
        approval_source_facebook, "load_ad_account_timezone", lambda ad_id: "UTC"
    )
    def _load(request, now):
        subject = request.subjects[0]
        window = request.windows[0]
        return (
            _source(
                SourceSystem.FACEBOOK,
                (
                    _record(subject, Metric.SPEND, Decimal("9.5"), SourceSystem.FACEBOOK, window, "USD"),
                    _record(subject, Metric.LEADS, leads, SourceSystem.FACEBOOK, window),
                ),
            ),
            _source(
                SourceSystem.AMO,
                (_record(subject, Metric.QUALS, quals, SourceSystem.AMO, window),),
            ),
            _source(
                SourceSystem.CDP_ERP,
                (
                    _record(
                        subject, Metric.REVENUE, Decimal("0"), SourceSystem.CDP_ERP, window, "LCY"
                    ),
                ),
            ),
        )

    monkeypatch.setattr(live, "_load_live_sources", _load)


def _pause_intent(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "operation": "PAUSE_AD",
        "ad_id": AD_ID,
        "adset_id": ADSET_ID,
        "expected_before_status": "ACTIVE",
        "expected_after_status": "PAUSED",
        "reason_code": "LOW_ROMI",
        "producer_inventory_sha256": "a" * 64,
        "sibling_active_ids": [SIBLING_ID],
    }
    payload.update(overrides)
    return payload


def _build(kind: ProposalKind, payload: dict):
    return live.build_live_manifest(
        kind,
        payload,
        origin=ActionOrigin.AUTOPILOT_LIVE,
        idempotency_key=KEY,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# PAUSE
# ---------------------------------------------------------------------------


def test_pause_manifest_is_built_from_live_inventory(monkeypatch) -> None:
    inventory = _inventory()
    _patch_inventory(monkeypatch, inventory)
    _patch_pause_sources(monkeypatch)

    manifest = _build(ProposalKind.PAUSE, _pause_intent())

    assert isinstance(manifest, PauseManifest)
    assert manifest.idempotency_key == KEY
    assert manifest.ad_id == AD_ID and manifest.adset_id == ADSET_ID
    assert manifest.reason_code == "LOW_ROMI"
    # Digest — живой, а не producer_inventory_sha256 из намерения.
    assert manifest.pre_inventory_sha256 == inventory["state_sha256"]
    assert manifest.pre_inventory_sha256 != "a" * 64
    assert manifest.sibling_active_ids == (SIBLING_ID,)
    assert {claim.source for claim in manifest.facts} == {
        SourceSystem.FACEBOOK,
        SourceSystem.AMO,
        SourceSystem.CDP_ERP,
    }


def test_pause_intent_without_reason_code_is_build_failure(monkeypatch) -> None:
    _patch_inventory(monkeypatch, _inventory())
    _patch_pause_sources(monkeypatch)
    intent = _pause_intent()
    del intent["reason_code"]

    with pytest.raises(live.LiveManifestError) as error:
        _build(ProposalKind.PAUSE, intent)

    assert error.value.code == "INTENT_FIELD_MISSING_REASON_CODE"
    # Баг сборки, а не изменившийся Facebook.
    assert error.value.stale is False


def test_pause_target_already_paused_is_live_drift(monkeypatch) -> None:
    _patch_inventory(monkeypatch, _inventory(target_effective="PAUSED"))
    _patch_pause_sources(monkeypatch)

    with pytest.raises(live.LiveManifestError) as error:
        _build(ProposalKind.PAUSE, _pause_intent())

    assert error.value.code == "LIVE_TARGET_STATUS_CHANGED"
    assert error.value.stale is True


def test_pause_last_active_ad_is_live_drift(monkeypatch) -> None:
    _patch_inventory(monkeypatch, _inventory(sibling_effective="PAUSED"))
    _patch_pause_sources(monkeypatch)

    with pytest.raises(live.LiveManifestError) as error:
        _build(ProposalKind.PAUSE, _pause_intent())

    assert error.value.code == "LIVE_LAST_ACTIVE_AD"
    assert error.value.stale is True


def test_pause_target_moved_to_other_adset_is_live_drift(monkeypatch) -> None:
    _patch_inventory(monkeypatch, _inventory(), other_adset=True)
    _patch_pause_sources(monkeypatch)

    with pytest.raises(live.LiveManifestError) as error:
        _build(ProposalKind.PAUSE, _pause_intent())

    assert error.value.code == "LIVE_ADSET_CHANGED"
    assert error.value.stale is True


def test_pause_incomplete_inventory_is_retryable(monkeypatch) -> None:
    _patch_inventory(monkeypatch, _inventory(complete=False))
    _patch_pause_sources(monkeypatch)

    with pytest.raises(live.LiveManifestError) as error:
        _build(ProposalKind.PAUSE, _pause_intent())

    assert error.value.code == "LIVE_INVENTORY_UNAVAILABLE"
    assert error.value.stale is False
    # Недоступность источника — отдельный класс исхода: одобрение не сгорает.
    assert error.value.unavailable is True


def _patch_real_pause_loaders(
    monkeypatch,
    *,
    amo_loader=None,
) -> None:
    from services import approval_source_facebook
    monkeypatch.setattr(
        approval_source_facebook, "load_ad_account_timezone", lambda ad_id: "UTC"
    )
    """Патчит три реальных загрузчика (не сам _load_live_sources)."""

    def _facebook(request, now, *, force_live):
        subject = request.subjects[0]
        window = request.windows[0]
        return _source(
            SourceSystem.FACEBOOK,
            (
                _record(
                    subject, Metric.SPEND, Decimal("9.5"),
                    SourceSystem.FACEBOOK, window, "USD",
                ),
                _record(subject, Metric.LEADS, 4, SourceSystem.FACEBOOK, window),
            ),
        )

    def _amo(request, now, *, force_live):
        subject = request.subjects[0]
        return _source(
            SourceSystem.AMO,
            (_record(subject, Metric.QUALS, 1, SourceSystem.AMO, request.windows[0]),),
        )

    def _cdp(request, now, *, force_live):
        subject = request.subjects[0]
        return _source(
            SourceSystem.CDP_ERP,
            (
                _record(
                    subject, Metric.REVENUE, Decimal("0"),
                    SourceSystem.CDP_ERP, request.windows[0], "LCY",
                ),
            ),
        )

    monkeypatch.setattr(
        "services.approval_source_facebook.load_facebook_evidence",
        _facebook,
    )
    monkeypatch.setattr(
        "services.approval_source_amo.load_amo_evidence",
        amo_loader or _amo,
    )
    monkeypatch.setattr("services.approval_source_cdp.load_cdp_evidence", _cdp)


def test_pause_source_timeout_is_marked_unavailable(monkeypatch) -> None:
    """Таймаут AMO — «источник недоступен», а не баг сборки манифеста."""

    import requests

    def _timeout(request, now, *, force_live):
        raise requests.exceptions.ReadTimeout("AMO GET leads таймаут")

    _patch_inventory(monkeypatch, _inventory())
    _patch_real_pause_loaders(monkeypatch, amo_loader=_timeout)

    with pytest.raises(live.LiveManifestError) as error:
        _build(ProposalKind.PAUSE, _pause_intent())

    assert error.value.code == "LIVE_FACTS_UNAVAILABLE"
    assert (error.value.stale, error.value.unavailable) == (False, True)


def test_pause_incomplete_source_names_the_source(monkeypatch) -> None:
    """Код причины называет источник И его диагноз — иначе в логе не разобраться."""

    def _incomplete(request, now, *, force_live):
        return SourceEvidence(
            source=SourceSystem.AMO,
            state=EvidenceState.INCOMPLETE,
            fetched_at=now,
            data_as_of=None,
            from_cache=False,
            complete=False,
            records=(),
            error_code="AMO_PAGE_LIMIT_EXCEEDED",
        )

    _patch_inventory(monkeypatch, _inventory())
    _patch_real_pause_loaders(monkeypatch, amo_loader=_incomplete)

    with pytest.raises(live.LiveManifestError) as error:
        _build(ProposalKind.PAUSE, _pause_intent())

    assert error.value.code == "LIVE_FACTS_INCOMPLETE_AMO:AMO_PAGE_LIMIT_EXCEEDED"
    assert error.value.unavailable is True


def test_pause_manifest_contract_rejection_is_build_failure(monkeypatch) -> None:
    """Живые цифры, нарушающие контракт фабрики, не выдаются за дрейф."""

    _patch_inventory(monkeypatch, _inventory())
    _patch_pause_sources(monkeypatch, leads=1, quals=3)

    with pytest.raises(live.LiveManifestError) as error:
        _build(ProposalKind.PAUSE, _pause_intent())

    assert error.value.code == "MANIFEST_CONTRACT_REJECTED"
    assert error.value.stale is False


# ---------------------------------------------------------------------------
# UNPAUSE
# ---------------------------------------------------------------------------


def _unpause_intent(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "operation": "UNPAUSE_AD",
        "ad_id": AD_ID,
        "adset_id": ADSET_ID,
        "expected_before_status": "PAUSED",
        "expected_after_status": "ACTIVE",
        "producer_inventory_sha256": "b" * 64,
    }
    payload.update(overrides)
    return payload


def test_unpause_manifest_is_built_from_live_inventory(monkeypatch) -> None:
    inventory = _inventory(target_effective="PAUSED")
    _patch_inventory(monkeypatch, inventory)

    manifest = _build(ProposalKind.UNPAUSE, _unpause_intent())

    assert isinstance(manifest, UnpauseManifest)
    assert manifest.idempotency_key == KEY
    assert manifest.pre_inventory_sha256 == inventory["state_sha256"]
    assert manifest.expected_after_status == "ACTIVE"
    assert tuple(item.ad_id for item in manifest.sibling_status_snapshot) == (
        SIBLING_ID,
    )


def test_unpause_target_already_active_is_live_drift(monkeypatch) -> None:
    _patch_inventory(monkeypatch, _inventory())

    with pytest.raises(live.LiveManifestError) as error:
        _build(ProposalKind.UNPAUSE, _unpause_intent())

    assert error.value.code == "LIVE_TARGET_STATUS_CHANGED"
    assert error.value.stale is True


# ---------------------------------------------------------------------------
# SCALE
# ---------------------------------------------------------------------------


def _scale_intent(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "operation": "SET_ADSET_BUDGET",
        "adset_id": ADSET_ID,
        "candidate_ad_id": AD_ID,
        "expected_current_budget_usd": Decimal("100"),
        "target_budget_usd": Decimal("115"),
    }
    payload.update(overrides)
    return payload


def _patch_scale_sources(monkeypatch, *, live_budget: Decimal = Decimal("100")) -> None:
    # Таймзона кабинета: дневное окно строится до чтения источников.
    from services import approval_source_facebook
    monkeypatch.setattr(
        approval_source_facebook, "load_ad_account_timezone", lambda ad_id: "UTC"
    )
    def _load(request, now):
        window = request.windows[0]
        # Facebook подписывает ADSET кабинетом — намерение продюсера его не знает.
        adset = SubjectRef(SubjectKind.ADSET, ADSET_ID, ACCOUNT_ID)
        ad = SubjectRef(SubjectKind.AD, AD_ID, ADSET_ID)
        return (
            _source(
                SourceSystem.FACEBOOK,
                (
                    _record(adset, Metric.EFFECTIVE_STATUS, "ACTIVE", SourceSystem.FACEBOOK, None),
                    _record(adset, Metric.DAILY_BUDGET, live_budget, SourceSystem.FACEBOOK, None, "USD"),
                    _record(ad, Metric.SPEND, Decimal("40"), SourceSystem.FACEBOOK, window, "USD"),
                    _record(ad, Metric.LEADS, 12, SourceSystem.FACEBOOK, window),
                ),
            ),
            _source(
                SourceSystem.AMO,
                (_record(ad, Metric.QUALS, 6, SourceSystem.AMO, window),),
            ),
            _source(
                SourceSystem.CDP_ERP,
                (
                    _record(
                        ad, Metric.REVENUE, Decimal("250000"), SourceSystem.CDP_ERP, window, "LCY"
                    ),
                ),
            ),
        )

    monkeypatch.setattr(live, "_load_live_sources", _load)


def test_scale_manifest_uses_live_budget(monkeypatch) -> None:
    _patch_scale_sources(monkeypatch)

    manifest = _build(ProposalKind.SCALE, _scale_intent())

    assert isinstance(manifest, ScaleManifest)
    assert manifest.idempotency_key == KEY
    assert manifest.current_budget == Decimal("100")
    assert manifest.target_budget == Decimal("115")
    assert manifest.currency == "USD"
    assert manifest.candidate_ad_ids == (AD_ID,)
    assert {claim.source for claim in manifest.facts} == {
        SourceSystem.FACEBOOK,
        SourceSystem.AMO,
        SourceSystem.CDP_ERP,
    }


def test_scale_live_budget_drift_is_live_drift(monkeypatch) -> None:
    """Живой бюджет уже не тот, с которого одобряли подъём."""

    _patch_scale_sources(monkeypatch, live_budget=Decimal("140"))

    with pytest.raises(live.LiveManifestError) as error:
        _build(ProposalKind.SCALE, _scale_intent())

    assert error.value.code == "LIVE_BUDGET_CHANGED"
    assert error.value.stale is True


def test_scale_intent_without_budget_is_build_failure(monkeypatch) -> None:
    _patch_scale_sources(monkeypatch)
    intent = _scale_intent()
    del intent["target_budget_usd"]

    with pytest.raises(live.LiveManifestError) as error:
        _build(ProposalKind.SCALE, intent)

    assert error.value.code == "INTENT_FIELD_MISSING_TARGET_BUDGET_USD"
    assert error.value.stale is False


# ---------------------------------------------------------------------------
# Общие контракты
# ---------------------------------------------------------------------------


def test_serialized_manifest_is_recognised_and_intent_is_not() -> None:
    """LAUNCH кладёт манифест целиком — его читают, а не собирают заново."""

    assert live.is_serialized_manifest(
        {
            "kind": "LAUNCH",
            "manifest_id": "m",
            "origin": "WEB",
            "idempotency_key": KEY,
            "prepared_at": NOW.isoformat(),
        }
    )
    assert not live.is_serialized_manifest(_pause_intent())
    assert not live.is_serialized_manifest(_scale_intent())


def test_asset_recovery_has_no_live_builder() -> None:
    with pytest.raises(live.LiveManifestError) as error:
        _build(ProposalKind.ASSET_RECOVERY, {"operation": "RECOVER_AD"})

    assert error.value.code == "PROPOSAL_KIND_NOT_BUILDABLE"


def test_action_origin_is_deterministic_from_proposal_origin() -> None:
    """Origin входит в digest манифеста — он обязан быть воспроизводимым."""

    assert live.action_origin_for(ProposalOrigin.AUTOPILOT) is ActionOrigin.AUTOPILOT_LIVE
    assert live.action_origin_for(ProposalOrigin.TELEGRAM_COMMAND) is ActionOrigin.TELEGRAM
    assert live.action_origin_for(ProposalOrigin.WEB) is ActionOrigin.WEB
    assert live.action_origin_for(ProposalOrigin.RECOVERY) is ActionOrigin.REPLACEMENT
    assert all(
        isinstance(live.action_origin_for(origin), ActionOrigin)
        for origin in ProposalOrigin
    )


def test_pause_manifest_is_stable_across_rebuilds(monkeypatch) -> None:
    """Один и тот же живой снимок даёт один и тот же digest — иначе permit не выдать."""

    from services.approval_checker_models import manifest_sha256

    _patch_inventory(monkeypatch, _inventory())
    _patch_pause_sources(monkeypatch)

    first = _build(ProposalKind.PAUSE, _pause_intent())
    second = _build(ProposalKind.PAUSE, _pause_intent())

    assert manifest_sha256(first) == manifest_sha256(second)


def test_pause_window_is_thirty_full_account_days(monkeypatch) -> None:
    _patch_inventory(monkeypatch, _inventory())
    _patch_pause_sources(monkeypatch)

    manifest = _build(ProposalKind.PAUSE, _pause_intent())

    # Окно лежит на полуночах кабинета и не включает текущий незакрытый день:
    # Insights отдаёт точный расход только за целые сутки таймзоны кабинета.
    day_start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    assert manifest.decision_window.end == day_start
    assert manifest.decision_window.start == day_start - timedelta(days=30)
    assert manifest.decision_window.timezone_name == "UTC"
