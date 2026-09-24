from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from services import approval_source_facebook
import pytest

from services.adset_pause_guard import inventory_state_sha256
from services.approval_checker_models import (
    ActionKind,
    ActionOrigin,
    CreativeSpec,
    EvidenceRequest,
    EvidenceState,
    FactCategory,
    FactClaim,
    LaunchDestination,
    LaunchManifest,
    MediaAssetSpec,
    MediaType,
    Metric,
    PlacementRole,
    PauseManifest,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
    TrelloPrecondition,
    UnpauseManifest,
)


def _request(*, ad_ids: tuple[str, ...] = (), adset_ids: tuple[str, ...] = ()) -> EvidenceRequest:
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    return EvidenceRequest(
        request_id="fb-test",
        purpose="ACTION",
        action_kind=None,
        generated_at=now,
        subjects=(),
        claims=(),
        required_sources=(SourceSystem.FACEBOOK,),
        windows=(),
        account_ids=("123",),
        adset_ids=adset_ids,
        ad_ids=ad_ids,
        card_ids=(),
        staged_relative_paths=(),
        include_full_inventory=bool(adset_ids),
        force_live=True,
        max_age_seconds=60,
    )


def test_facebook_exact_objects_are_chunked_by_50(monkeypatch):
    requested = tuple(str(value) for value in range(120))
    chunks: list[tuple[str, ...]] = []

    def fake_graph(path, params):
        if path == "act_123":
            return {"id": "act_123", "account_status": 1, "currency": "USD", "timezone_name": "UTC"}
        if path == "/":
            ids = tuple(params["ids"].split(","))
            chunks.append(ids)
            return {
                ad_id: {
                    "id": ad_id,
                    "name": f"ad-{ad_id}",
                    "status": "ACTIVE",
                    "effective_status": "ACTIVE",
                    "created_time": "2026-07-01T00:00:00+0000",
                    "adset_id": "set-1",
                    "creative": {"id": f"creative-{ad_id}"},
                }
                for ad_id in ids
            }
        raise AssertionError(path)

    monkeypatch.setattr(approval_source_facebook, "_graph_json", fake_graph)
    now = datetime(2026, 7, 1, 1, tzinfo=timezone.utc)

    evidence = approval_source_facebook.load_facebook_evidence(
        _request(ad_ids=requested), now, force_live=True
    )

    assert evidence.complete is True
    assert [len(chunk) for chunk in chunks] == [50, 50, 20]
    assert len([row for row in evidence.records if row.metric is Metric.EFFECTIVE_STATUS]) == 120


def test_facebook_incomplete_pagination_fails_closed(monkeypatch):
    def fake_graph(path, params):
        if path == "act_123":
            return {"id": "123", "account_status": 1, "currency": "USD", "timezone_name": "UTC"}
        if path == "set-1":
            return {"id": "set-1", "account_id": "123", "daily_budget": "1000", "effective_status": "ACTIVE", "name": "set"}
        if path == "set-1/ads":
            return {
                "data": [{"id": "ad-1", "name": "ad", "status": "ACTIVE", "effective_status": "ACTIVE", "adset_id": "set-1"}],
                "paging": {"next": "next-without-cursor"},
            }
        raise AssertionError(path)

    monkeypatch.setattr(approval_source_facebook, "_graph_json", fake_graph)
    now = datetime(2026, 7, 1, 1, tzinfo=timezone.utc)

    evidence = approval_source_facebook.load_facebook_evidence(
        _request(adset_ids=("set-1",)), now, force_live=True
    )

    assert evidence.complete is False
    assert evidence.state is EvidenceState.INCOMPLETE
    assert evidence.error_code == "FB_PAGING_CURSOR_INVALID"


def test_facebook_paginated_inventory_is_complete(monkeypatch):
    pages = 0

    def fake_graph(path, params):
        nonlocal pages
        if path == "act_123":
            return {"id": "123", "account_status": 1, "currency": "USD", "timezone_name": "UTC"}
        if path == "set-1":
            return {"id": "set-1", "account_id": "123", "daily_budget": "1000", "effective_status": "ACTIVE", "name": "set"}
        if path == "set-1/ads":
            pages += 1
            if params.get("after") is None:
                return {
                    "data": [{"id": "ad-1", "name": "one", "status": "ACTIVE", "effective_status": "ACTIVE", "adset_id": "set-1"}],
                    "paging": {"next": "yes", "cursors": {"after": "cursor"}},
                }
            return {
                "data": [{"id": "ad-2", "name": "two", "status": "PAUSED", "effective_status": "PAUSED", "adset_id": "set-1"}],
                "paging": {},
            }
        raise AssertionError(path)

    monkeypatch.setattr(approval_source_facebook, "_graph_json", fake_graph)
    now = datetime(2026, 7, 1, 1, tzinfo=timezone.utc)

    evidence = approval_source_facebook.load_facebook_evidence(
        _request(adset_ids=("set-1",)), now, force_live=True
    )

    capacity = next(record for record in evidence.records if record.metric is Metric.CAPACITY)
    assert evidence.complete is True
    assert pages == 2
    assert capacity.value == 48
    assert not any(record.metric is Metric.MATCH_STATE for record in evidence.records)


def _account_report_request(
    claims: tuple[FactClaim, ...],
    window: TimeWindow,
    generated_at: datetime,
) -> EvidenceRequest:
    return EvidenceRequest(
        request_id="fb-account-report",
        purpose="REPORT",
        action_kind=None,
        generated_at=generated_at,
        subjects=tuple(dict.fromkeys(claim.subject for claim in claims)),
        claims=claims,
        required_sources=(SourceSystem.FACEBOOK,),
        windows=(window,),
        account_ids=("123",),
        adset_ids=(),
        ad_ids=(),
        card_ids=(),
        staged_relative_paths=(),
        include_full_inventory=True,
        force_live=False,
        max_age_seconds=60,
    )


def _account_aggregate_claims(
    subject: SubjectRef, window: TimeWindow, start: datetime
) -> tuple[FactClaim, ...]:
    return tuple(
        FactClaim(
            claim_id=f"claim-{metric.value}",
            field_id=None,
            category=category,
            subject=subject,
            metric=metric,
            value=value,
            source=SourceSystem.FACEBOOK,
            window=window,
            currency=currency,
        )
        for metric, category, value, currency in (
            (Metric.SPEND, FactCategory.BUSINESS_METRIC, Decimal("15.5"), "USD"),
            (Metric.LEADS, FactCategory.BUSINESS_METRIC, 3, None),
            (Metric.WINDOW_START, FactCategory.WINDOW_BOUND, start.isoformat(), None),
            (
                Metric.WINDOW_END,
                FactCategory.WINDOW_BOUND,
                (start + timedelta(days=1)).isoformat(),
                None,
            ),
        )
    )


def test_facebook_account_aggregate_is_single_account_level_request(monkeypatch):
    """Агрегат кабинета — один insights level=account, без выкачки инвентаря."""

    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    window = TimeWindow(start, start + timedelta(days=1), "UTC", "closed_day")
    subject = SubjectRef(SubjectKind.ACCOUNT, "123")
    request = _account_report_request(
        _account_aggregate_claims(subject, window, start), window, start
    )
    insights_calls = 0

    def fake_graph(path, params):
        nonlocal insights_calls
        if path == "act_123":
            return {
                "id": "123",
                "account_status": 1,
                "currency": "USD",
                "timezone_name": "UTC",
            }
        if path == "act_123/ads":
            raise AssertionError("инвентарь кабинета не должен выкачиваться")
        if path == "act_123/insights":
            insights_calls += 1
            assert params["level"] == "account"
            assert "filtering" not in params
            assert '"since":"2026-07-01","until":"2026-07-01"' in params["time_range"]
            return {
                "data": [
                    {
                        "account_id": "123",
                        "spend": "15.5",
                        "actions": [
                            {"action_type": "lead", "value": "2"},
                            {"action_type": "on_facebook_lead", "value": "1"},
                        ],
                    }
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(approval_source_facebook, "_graph_json", fake_graph)
    evidence = approval_source_facebook.load_facebook_evidence(
        request,
        start + timedelta(hours=1),
        force_live=False,
    )

    aggregate_records = [
        record
        for record in evidence.records
        if record.subject == subject and record.window == window
    ]
    assert evidence.complete is True
    assert insights_calls == 1
    assert {record.metric: record.value for record in aggregate_records} == {
        Metric.SPEND: Decimal("15.5"),
        Metric.LEADS: 3,
        Metric.WINDOW_START: start.isoformat(),
        Metric.WINDOW_END: (start + timedelta(days=1)).isoformat(),
    }
    # Инвентарь не перечислялся — записи честно не ссылаются на сущности.
    assert all(record.entity_ids == () for record in aggregate_records)
    assert not any(record.metric is Metric.MATCH_STATE for record in evidence.records)


def test_facebook_account_aggregate_empty_insights_is_not_zero(monkeypatch):
    """Пустой data[] = «Graph ещё не посчитал», а не расход 0."""

    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    window = TimeWindow(start, start + timedelta(days=1), "UTC", "closed_day")
    subject = SubjectRef(SubjectKind.ACCOUNT, "123")
    request = _account_report_request(
        _account_aggregate_claims(subject, window, start), window, start
    )

    def fake_graph(path, params):
        if path == "act_123":
            return {
                "id": "123",
                "account_status": 1,
                "currency": "USD",
                "timezone_name": "UTC",
            }
        if path == "act_123/insights":
            return {"data": []}
        raise AssertionError(path)

    monkeypatch.setattr(approval_source_facebook, "_graph_json", fake_graph)
    evidence = approval_source_facebook.load_facebook_evidence(
        request,
        start + timedelta(hours=1),
        force_live=False,
    )

    assert evidence.complete is True
    assert not any(record.window == window for record in evidence.records)


def test_facebook_account_aggregate_rejects_foreign_account_row(monkeypatch):
    """Строка агрегата обязана принадлежать запрошенному кабинету."""

    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    window = TimeWindow(start, start + timedelta(days=1), "UTC", "closed_day")
    subject = SubjectRef(SubjectKind.ACCOUNT, "123")
    request = _account_report_request(
        _account_aggregate_claims(subject, window, start), window, start
    )

    def fake_graph(path, params):
        if path == "act_123":
            return {
                "id": "123",
                "account_status": 1,
                "currency": "USD",
                "timezone_name": "UTC",
            }
        if path == "act_123/insights":
            return {"data": [{"account_id": "999", "spend": "15.5", "actions": []}]}
        raise AssertionError(path)

    monkeypatch.setattr(approval_source_facebook, "_graph_json", fake_graph)
    evidence = approval_source_facebook.load_facebook_evidence(
        request,
        start + timedelta(hours=1),
        force_live=False,
    )

    assert evidence.complete is False
    assert evidence.state is EvidenceState.INCOMPLETE
    assert evidence.error_code == "FB_INSIGHT_SUBJECT_MISMATCH"


def test_facebook_account_aggregate_rejects_ambiguous_multi_row_answer(monkeypatch):
    """Две агрегатные строки на одно окно — неоднозначный ответ, fail-closed."""

    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    window = TimeWindow(start, start + timedelta(days=1), "UTC", "closed_day")
    subject = SubjectRef(SubjectKind.ACCOUNT, "123")
    request = _account_report_request(
        _account_aggregate_claims(subject, window, start), window, start
    )

    def fake_graph(path, params):
        if path == "act_123":
            return {
                "id": "123",
                "account_status": 1,
                "currency": "USD",
                "timezone_name": "UTC",
            }
        if path == "act_123/insights":
            return {
                "data": [
                    {"account_id": "123", "spend": "10", "actions": []},
                    {"account_id": "123", "spend": "5.5", "actions": []},
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(approval_source_facebook, "_graph_json", fake_graph)
    evidence = approval_source_facebook.load_facebook_evidence(
        request,
        start + timedelta(hours=1),
        force_live=False,
    )

    assert evidence.complete is False
    assert evidence.state is EvidenceState.INCOMPLETE
    assert evidence.error_code == "FB_ACCOUNT_INSIGHT_AMBIGUOUS"


def test_facebook_partial_day_does_not_self_assert_window(monkeypatch):
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=12)
    window = TimeWindow(start, end, "UTC", "partial_day")
    subject = SubjectRef(SubjectKind.ACCOUNT, "123")
    claim = FactClaim(
        claim_id="window-end",
        field_id=None,
        category=FactCategory.WINDOW_BOUND,
        subject=subject,
        metric=Metric.WINDOW_END,
        value=end.isoformat(),
        source=SourceSystem.FACEBOOK,
        window=window,
    )
    request = EvidenceRequest(
        request_id="fb-partial",
        purpose="REPORT",
        action_kind=None,
        generated_at=start,
        subjects=(subject,),
        claims=(claim,),
        required_sources=(SourceSystem.FACEBOOK,),
        windows=(window,),
        account_ids=("123",),
        adset_ids=(),
        ad_ids=(),
        card_ids=(),
        staged_relative_paths=(),
        include_full_inventory=True,
        force_live=False,
        max_age_seconds=60,
    )

    def fake_graph(path, params):
        if path == "act_123":
            return {
                "id": "123",
                "account_status": 1,
                "currency": "USD",
                "timezone_name": "UTC",
            }
        if path == "act_123/ads":
            raise AssertionError("инвентарь кабинета не должен выкачиваться")
        if path == "act_123/insights":
            raise AssertionError("partial window must not be rounded")
        raise AssertionError(path)

    monkeypatch.setattr(approval_source_facebook, "_graph_json", fake_graph)
    evidence = approval_source_facebook.load_facebook_evidence(
        request,
        end,
        force_live=False,
    )

    assert evidence.complete is True
    assert not any(
        record.metric is Metric.WINDOW_END and record.window == window
        for record in evidence.records
    )


def test_facebook_adset_aggregate_uses_full_inventory(monkeypatch):
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    window = TimeWindow(start, start + timedelta(days=1), "UTC", "closed_day")
    subject = SubjectRef(SubjectKind.ADSET, "set-1", "123")
    claims = tuple(
        FactClaim(
            claim_id=f"adset-{metric.value}",
            field_id=None,
            category=FactCategory.BUSINESS_METRIC,
            subject=subject,
            metric=metric,
            value=value,
            source=SourceSystem.FACEBOOK,
            window=window,
            currency=currency,
        )
        for metric, value, currency in (
            (Metric.SPEND, Decimal("12"), "USD"),
            (Metric.LEADS, 2, None),
        )
    )
    request = replace(
        _request(adset_ids=("set-1",)),
        purpose="REPORT",
        generated_at=start,
        subjects=(subject,),
        claims=claims,
        windows=(window,),
        force_live=False,
    )

    def fake_graph(path, params):
        if path == "act_123":
            return {
                "id": "123",
                "account_status": 1,
                "currency": "USD",
                "timezone_name": "UTC",
            }
        if path == "set-1":
            return {
                "id": "set-1",
                "account_id": "123",
                "daily_budget": "1000",
                "effective_status": "ACTIVE",
                "name": "set",
            }
        if path == "set-1/ads":
            return {
                "data": [
                    {
                        "id": "ad-1",
                        "name": "one",
                        "status": "ACTIVE",
                        "effective_status": "ACTIVE",
                        "adset_id": "set-1",
                    },
                    {
                        "id": "ad-2",
                        "name": "two",
                        "status": "ACTIVE",
                        "effective_status": "ACTIVE",
                        "adset_id": "set-1",
                    },
                ]
            }
        if path == "act_123/insights":
            return {
                "data": [
                    {
                        "ad_id": "ad-1",
                        "spend": "7",
                        "actions": [{"action_type": "lead", "value": "2"}],
                    },
                    {"ad_id": "ad-2", "spend": "5", "actions": []},
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(approval_source_facebook, "_graph_json", fake_graph)
    evidence = approval_source_facebook.load_facebook_evidence(
        request,
        start + timedelta(days=1),
        force_live=False,
    )

    aggregate = {
        record.metric: record.value
        for record in evidence.records
        if record.subject == subject and record.window == window
    }
    assert evidence.complete is True
    assert aggregate == {Metric.SPEND: Decimal("12"), Metric.LEADS: 2}


def _launch_manifest(signature: str = "signature-1") -> LaunchManifest:
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    sha = "a" * 64
    media = MediaAssetSpec(
        asset_id="asset-1",
        order_index=0,
        media_type=MediaType.VIDEO,
        placement_group_id=None,
        placement_role=PlacementRole.DEFAULT,
        staged_relative_path="manifest/video.mp4",
        original_attachment_id="attachment-1",
        mime_type="video/mp4",
        size_bytes=10,
        content_sha256=sha,
    )
    creative = CreativeSpec(
        creative_id="creative-1",
        order_index=0,
        ad_name="planned-ad",
        media_asset_ids=("asset-1",),
        body_staged_relative_path="manifest/body.txt",
        body_sha256=sha,
        product="PRODB",
        page_id="page-1",
        lead_form_id="form-1",
        call_to_action="LEARN_MORE",
        instagram_actor_id="instagram-1",
        title="title",
        link_url=None,
        expected_configured_status="ACTIVE",
    )
    destination = LaunchDestination(
        city="CityA",
        account_id="123",
        adset_id="set-1",
        adset_type="L2",
        current_daily_budget=Decimal("10"),
        currency="USD",
        capacity_available=49,
        hard_reserve_slots=1,
        creatives=(creative,),
        duplicate_signature=signature,
    )
    return LaunchManifest(
        kind=ActionKind.LAUNCH,
        manifest_id="manifest-1",
        origin=ActionOrigin.WEB,
        idempotency_key="00000000-0000-4000-8000-000000000001",
        prepared_at=now,
        config_version_sha256=sha,
        staging_root="/tmp/staging",
        staging_directory="/tmp/staging/manifest-1",
        trello=TrelloPrecondition(
            card_id="card-1",
            board_id="board-1",
            ready_list_id="ready-1",
            expected_list_id="ready-1",
            expected_due_complete=False,
            expected_closed=False,
            date_last_activity=now,
            attachment_ids=("attachment-1",),
            attachment_manifest_sha256=sha,
            labels_sha256=sha,
            card_content_sha256=sha,
        ),
        card_name_sha256=sha,
        media_manifest_sha256=sha,
        media_assets=(media,),
        campaign_type="leadgen",
        destinations=(destination,),
    )


def test_manifest_precondition_denies_exact_duplicate_signature(monkeypatch):
    now = datetime(2026, 7, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        approval_source_facebook,
        "load_facebook_evidence",
        lambda *args, **kwargs: SourceEvidence(
            source=SourceSystem.FACEBOOK,
            state=EvidenceState.FRESH_COMPLETE,
            fetched_at=now,
            data_as_of=now,
            from_cache=False,
            complete=True,
            records=(),
        ),
    )
    monkeypatch.setattr(
        approval_source_facebook,
        "_load_adset_inventory",
        # account_id обязателен: precondition сверяет живой кабинет адсета
        # с кабинетом цели манифеста (маршрутизация город→кабинет).
        lambda adset_id: (
            {"id": adset_id, "account_id": "123"},
            ({"id": "live-1", "name": "other", "duplicate_signature": "signature-1"},),
        ),
    )

    with pytest.raises(
        approval_source_facebook.FacebookEvidenceError,
        match="FB_LAUNCH_DUPLICATE_SIGNATURE",
    ):
        approval_source_facebook.read_action_precondition(_launch_manifest(), now)


def test_manifest_precondition_denies_adset_outside_destination_account(monkeypatch):
    """Адсет, живущий не в кабинете своей цели, — отказ до сравнения имён."""
    now = datetime(2026, 7, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        approval_source_facebook,
        "load_facebook_evidence",
        lambda *args, **kwargs: SourceEvidence(
            source=SourceSystem.FACEBOOK,
            state=EvidenceState.FRESH_COMPLETE,
            fetched_at=now,
            data_as_of=now,
            from_cache=False,
            complete=True,
            records=(),
        ),
    )
    monkeypatch.setattr(
        approval_source_facebook,
        "_load_adset_inventory",
        lambda adset_id: (
            {"id": adset_id, "account_id": "act_29716040622546856"},
            (),
        ),
    )

    with pytest.raises(
        approval_source_facebook.FacebookEvidenceError,
        match="FB_LAUNCH_DESTINATION_ACCOUNT_DRIFT",
    ):
        approval_source_facebook.read_action_precondition(_launch_manifest(), now)


def _inventory_rows(
    *,
    target_configured: str = "ACTIVE",
    target_effective: str = "ACTIVE",
    sibling_effective: str = "ACTIVE",
) -> tuple[dict[str, object], ...]:
    return (
        {
            "id": "ad-1",
            "name": "target",
            "status": target_configured,
            "effective_status": target_effective,
            "adset_id": "set-1",
        },
        {
            "id": "ad-2",
            "name": "sibling",
            "status": "ACTIVE",
            "effective_status": sibling_effective,
            "adset_id": "set-1",
        },
    )


def _guard_rows(
    rows: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "ad_id": row["id"],
            "adset_id": row["adset_id"],
            "configured_status": row["status"],
            "effective_status": row["effective_status"],
        }
        for row in rows
    )


def _pause_manifest(pre_inventory_sha256: str) -> PauseManifest:
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    window = TimeWindow(now - timedelta(days=7), now, "UTC", "decision")
    subject = SubjectRef(SubjectKind.AD, "ad-1", "set-1")
    facts = tuple(
        FactClaim(
            claim_id=f"pause-{source.value}",
            field_id=None,
            category=FactCategory.BUSINESS_METRIC,
            subject=subject,
            metric=metric,
            value=value,
            source=source,
            window=window,
            currency=currency,
        )
        for source, metric, value, currency in (
            (SourceSystem.FACEBOOK, Metric.SPEND, Decimal("10"), "USD"),
            (SourceSystem.AMO, Metric.QUALS, 1, None),
            (SourceSystem.CDP_ERP, Metric.PAYMENTS, 1, None),
        )
    )
    return PauseManifest(
        kind=ActionKind.PAUSE,
        manifest_id="pause-1",
        origin=ActionOrigin.AUTOPILOT_LIVE,
        idempotency_key="00000000-0000-4000-8000-000000000002",
        prepared_at=now,
        ad_id="ad-1",
        adset_id="set-1",
        reason_code="LOW_ROMI",
        expected_before_status="ACTIVE",
        expected_after_status="PAUSED",
        decision_window=window,
        facts=facts,
        pre_inventory_sha256=pre_inventory_sha256,
        sibling_active_ids=("ad-2",),
    )


def _unpause_manifest(pre_inventory_sha256: str) -> UnpauseManifest:
    return UnpauseManifest(
        kind=ActionKind.UNPAUSE,
        manifest_id="unpause-1",
        origin=ActionOrigin.WEB,
        idempotency_key="00000000-0000-4000-8000-000000000003",
        prepared_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ad_id="ad-1",
        adset_id="set-1",
        expected_before_status="PAUSED",
        expected_after_status="ACTIVE",
        pre_inventory_sha256=pre_inventory_sha256,
    )


def _complete_facebook(now: datetime) -> SourceEvidence:
    return SourceEvidence(
        source=SourceSystem.FACEBOOK,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
        records=(),
    )


def test_pause_precondition_digest_matches_guard_and_ignores_row_order(monkeypatch):
    now = datetime(2026, 7, 1, 1, tzinfo=timezone.utc)
    rows = _inventory_rows()
    expected = inventory_state_sha256(_guard_rows(rows), expected_adset_id="set-1")
    manifest = _pause_manifest(expected)
    inventories = iter((rows, tuple(reversed(rows))))
    monkeypatch.setattr(
        approval_source_facebook,
        "load_facebook_evidence",
        lambda *args, **kwargs: _complete_facebook(now),
    )
    monkeypatch.setattr(
        approval_source_facebook,
        "_load_adset_inventory",
        lambda adset_id: ({"id": adset_id}, next(inventories)),
    )

    first = approval_source_facebook.read_action_precondition(manifest, now)
    second = approval_source_facebook.read_action_precondition(manifest, now)

    assert first.digest == second.digest == manifest.pre_inventory_sha256
    assert first.target_state == "ACTIVE|ACTIVE"
    assert first.subject_ids == ("ad-1",)
    assert first.unrelated_state_digest == inventory_state_sha256(
        _guard_rows((rows[1],)),
        expected_adset_id="set-1",
    )


def test_pause_inventory_or_status_drift_changes_canonical_digest(monkeypatch):
    now = datetime(2026, 7, 1, 1, tzinfo=timezone.utc)
    baseline_rows = _inventory_rows()
    baseline = inventory_state_sha256(
        _guard_rows(baseline_rows),
        expected_adset_id="set-1",
    )
    manifest = _pause_manifest(baseline)
    target_drift = _inventory_rows(target_effective="PAUSED")
    added_sibling = (
        *baseline_rows,
        {
            "id": "ad-3",
            "name": "new",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "adset_id": "set-1",
        },
    )
    inventories = iter((target_drift, added_sibling))
    monkeypatch.setattr(
        approval_source_facebook,
        "load_facebook_evidence",
        lambda *args, **kwargs: _complete_facebook(now),
    )
    monkeypatch.setattr(
        approval_source_facebook,
        "_load_adset_inventory",
        lambda adset_id: ({"id": adset_id}, next(inventories)),
    )

    status_observation = approval_source_facebook.read_action_precondition(manifest, now)
    inventory_observation = approval_source_facebook.read_action_precondition(manifest, now)

    assert status_observation.digest != baseline
    assert status_observation.target_state == "ACTIVE|PAUSED"
    assert inventory_observation.digest != baseline
    assert (
        inventory_observation.unrelated_state_digest
        != status_observation.unrelated_state_digest
    )


def test_unpause_pre_and_post_share_truthful_unrelated_inventory_digest(monkeypatch):
    now = datetime(2026, 7, 1, 1, tzinfo=timezone.utc)
    before = _inventory_rows(
        target_configured="PAUSED",
        target_effective="PAUSED",
    )
    after = _inventory_rows()
    manifest = _unpause_manifest(
        inventory_state_sha256(_guard_rows(before), expected_adset_id="set-1")
    )
    inventories = iter((before, after))
    monkeypatch.setattr(
        approval_source_facebook,
        "load_facebook_evidence",
        lambda *args, **kwargs: _complete_facebook(now),
    )
    monkeypatch.setattr(
        approval_source_facebook,
        "_load_adset_inventory",
        lambda adset_id: ({"id": adset_id}, next(inventories)),
    )

    pre = approval_source_facebook.read_action_precondition(manifest, now)
    post = approval_source_facebook.read_action_postcondition(manifest, (), now)

    assert pre.digest == manifest.pre_inventory_sha256
    assert pre.target_state == "PAUSED|PAUSED"
    assert post.target_state == "ACTIVE|ACTIVE"
    assert post.digest != pre.digest
    assert post.unrelated_state_digest == pre.unrelated_state_digest


class _FakeResponse:
    """Минимальный http-ответ для проверки классификации ошибок Graph."""

    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _ad_row(ad_id: str, account_id: str) -> dict[str, object]:
    return {"id": ad_id, "account_id": account_id}


def test_ad_ownership_checks_only_requested_ids_in_batches_of_50(monkeypatch):
    requested = tuple(f"ad-{index}" for index in range(120))
    chunks: list[tuple[str, ...]] = []
    paths: list[str] = []

    def fake_response(path, params):
        paths.append(path)
        ids = tuple(params["ids"].split(","))
        chunks.append(ids)
        assert params["fields"] == "id,account_id"
        return _FakeResponse(
            200,
            {
                ad_id: _ad_row(
                    ad_id,
                    "123" if int(ad_id.removeprefix("ad-")) % 2 == 0 else "999",
                )
                for ad_id in ids
            },
        )

    monkeypatch.setattr(approval_source_facebook, "_graph_response", fake_response)

    owned = approval_source_facebook.load_account_ad_ownership("act_123", requested)

    assert sorted(len(chunk) for chunk in chunks) == [20, 50, 50]
    assert paths == ["/", "/", "/"]
    assert len(owned) == 60
    assert all(int(ad_id.removeprefix("ad-")) % 2 == 0 for ad_id in owned)


def test_ad_ownership_ignores_ads_of_another_account_and_missing_rows(monkeypatch):
    def fake_response(_path, params):
        ids = tuple(params["ids"].split(","))
        payload: dict[str, object] = {}
        for ad_id in ids:
            if ad_id == "ad-ours":
                payload[ad_id] = _ad_row(ad_id, "123")
            elif ad_id == "ad-foreign":
                payload[ad_id] = _ad_row(ad_id, "777")
            # ad-silent Graph не вернул вовсе — доказательства нет.
        return _FakeResponse(200, payload)

    monkeypatch.setattr(approval_source_facebook, "_graph_response", fake_response)

    owned = approval_source_facebook.load_account_ad_ownership(
        "123", ("ad-ours", "ad-foreign", "ad-silent")
    )

    assert owned == frozenset({"ad-ours"})


def test_ad_ownership_splits_batch_rejected_because_of_missing_ad(monkeypatch):
    requested = tuple(f"ad-{index}" for index in range(8)) + ("ad-ghost",)
    attempts: list[tuple[str, ...]] = []

    def fake_response(_path, params):
        ids = tuple(params["ids"].split(","))
        attempts.append(ids)
        if "ad-ghost" in ids:
            return _FakeResponse(
                400,
                {
                    "error": {
                        "code": 100,
                        "error_subcode": 33,
                        "message": "Unsupported get request",
                    }
                },
            )
        return _FakeResponse(200, {ad_id: _ad_row(ad_id, "123") for ad_id in ids})

    monkeypatch.setattr(approval_source_facebook, "_graph_response", fake_response)

    owned = approval_source_facebook.load_account_ad_ownership("123", requested)

    assert owned == frozenset(f"ad-{index}" for index in range(8))
    assert ("ad-ghost",) in attempts
    # Дробление пополам, а не по одному запросу на каждый id.
    assert len(attempts) < len(requested) + 1


def test_ad_ownership_raises_on_transport_error(monkeypatch):
    monkeypatch.setattr(
        approval_source_facebook,
        "_graph_response",
        lambda _path, _params: _FakeResponse(500, {"error": {"code": 2}}),
    )

    with pytest.raises(approval_source_facebook.FacebookEvidenceError) as failure:
        approval_source_facebook.load_account_ad_ownership("123", ("ad-1",))

    assert str(failure.value) == "FB_HTTP_500"


def test_ad_ownership_raises_on_auth_error_instead_of_answering_not_ours(monkeypatch):
    monkeypatch.setattr(
        approval_source_facebook,
        "_graph_response",
        lambda _path, _params: _FakeResponse(
            400, {"error": {"code": 190, "message": "token expired"}}
        ),
    )

    with pytest.raises(approval_source_facebook.FacebookEvidenceError) as failure:
        approval_source_facebook.load_account_ad_ownership("123", ("ad-1",))

    assert str(failure.value) == "FB_HTTP_400"


def test_ad_ownership_raises_when_account_id_is_absent(monkeypatch):
    monkeypatch.setattr(
        approval_source_facebook,
        "_graph_response",
        lambda _path, _params: _FakeResponse(200, {"ad-1": {"id": "ad-1"}}),
    )

    with pytest.raises(approval_source_facebook.FacebookEvidenceError) as failure:
        approval_source_facebook.load_account_ad_ownership("123", ("ad-1",))

    assert str(failure.value) == "FB_OWNERSHIP_ACCOUNT_MISSING"


def test_ad_ownership_without_ids_makes_no_graph_calls(monkeypatch):
    def unexpected(_path, _params):
        raise AssertionError("без id проверять нечего")

    monkeypatch.setattr(approval_source_facebook, "_graph_response", unexpected)

    assert approval_source_facebook.load_account_ad_ownership("123", ()) == frozenset()


def _second_cabinet_graph(explicit_default: str):
    """Graph-фейк: адсет и объявление живут в кабинете 456, запрошен другой."""

    def fake_graph(path, params):
        if path.startswith("act_") and not path.endswith("/insights"):
            return {
                "id": path.removeprefix("act_"),
                "account_status": 1,
                "currency": "USD",
                "timezone_name": "UTC",
            }
        if path == "set-w":
            return {
                "id": "set-w",
                "account_id": "act_456",
                "daily_budget": "1000",
                "effective_status": "ACTIVE",
                "name": "cab_b set",
            }
        if path == "set-w/ads":
            return {"data": [
                {"id": "ad-w", "name": "w", "status": "ACTIVE", "effective_status": "ACTIVE", "adset_id": "set-w"},
            ]}
        if path == "/":
            return {"ad-w": {
                "id": "ad-w", "name": "w", "status": "ACTIVE", "effective_status": "ACTIVE",
                "created_time": "2026-06-01T00:00:00+0000", "adset_id": "set-w", "account_id": "act_456",
            }}
        if path == "act_456/insights":
            return {"data": [{"ad_id": "ad-w", "spend": "9", "actions": [{"action_type": "lead", "value": "1"}]}]}
        if path == f"act_{explicit_default}/insights":
            raise AssertionError("инсайты объявления второго кабинета запрошены из чужого кабинета")
        raise AssertionError(path)

    return fake_graph


def test_default_accounts_fetch_object_account_on_demand(monkeypatch):
    """Регрессия: манифесты PAUSE/SCALE кабинет не несут, и сверка
    подставляла кабинет по умолчанию — любое действие по второму кабинету
    (cabinet_b) падало на FB_ADSET_ACCOUNT_NOT_REQUESTED, а ad-инсайты читались
    из чужого кабинета. Кабинет объекта — факт из Graph: при неявном списке он
    дозапрашивается, инсайты идут из его кабинета."""
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    window = TimeWindow(start, start + timedelta(days=1), "UTC", "closed_day")
    request = replace(
        _request(ad_ids=("ad-w",), adset_ids=("set-w",)),
        account_ids=(),
        windows=(window,),
        generated_at=start,
    )
    monkeypatch.setattr(approval_source_facebook, "get_fb_account_id", lambda: "123")
    monkeypatch.setattr(approval_source_facebook, "_graph_json", _second_cabinet_graph("123"))

    evidence = approval_source_facebook.load_facebook_evidence(
        request, start + timedelta(days=1), force_live=False
    )

    assert evidence.complete is True
    adset_accounts = {
        r.subject.parent_id for r in evidence.records if r.subject.kind is SubjectKind.ADSET
    }
    assert adset_accounts == {"456"}
    spend = next(
        r.value for r in evidence.records
        if r.subject.kind is SubjectKind.AD and r.metric is Metric.SPEND
    )
    assert spend == Decimal("9")


def test_explicit_accounts_still_reject_foreign_adset(monkeypatch):
    """При ЯВНОМ списке кабинетов (LAUNCH) чужой адсет — по-прежнему отказ."""
    request = replace(_request(adset_ids=("set-w",)), account_ids=("123",))
    monkeypatch.setattr(approval_source_facebook, "_graph_json", _second_cabinet_graph("123"))

    evidence = approval_source_facebook.load_facebook_evidence(
        request, datetime(2026, 7, 2, tzinfo=timezone.utc), force_live=False
    )

    assert evidence.complete is False
    assert evidence.error_code == "FB_ADSET_ACCOUNT_NOT_REQUESTED"
