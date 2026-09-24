from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from services import approval_source_cdp, cdp_client
from services.approval_checker_models import (
    CdpResponse,
    EvidenceRequest,
    FactCategory,
    FactClaim,
    Metric,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
)
from services.cdp_client import StrictPaymentsRead


def _response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.headers = {"Content-Type": "application/json"}
    response.json.return_value = payload
    response.text = ""
    return response


def _request(ad_ids: tuple[str, ...] = ("ad-1",)) -> EvidenceRequest:
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    window = TimeWindow(start, start + timedelta(days=7), "UTC", "7d")
    return EvidenceRequest(
        request_id="cdp-test",
        purpose="ACTION",
        action_kind=None,
        generated_at=start,
        subjects=(),
        claims=(),
        required_sources=(SourceSystem.CDP_ERP,),
        windows=(window,),
        account_ids=(),
        adset_ids=(),
        ad_ids=ad_ids,
        card_ids=(),
        staged_relative_paths=(),
        include_full_inventory=False,
        force_live=True,
        max_age_seconds=60,
    )


def _payment(
    payment_id: int,
    *,
    amount: int,
    direction: str,
    contract_number: int = 101,
) -> dict[str, object]:
    return {
        "id": payment_id,
        "contract_number": contract_number,
        "amount": amount,
        "direction": direction,
        "doc_date": "2026-07-03",
        "synced_at": "2026-07-08T01:00:00+00:00",
    }


def _lead(ad_id: str, lead_id: int = 101) -> dict[str, object]:
    return {
        "id": lead_id,
        "custom_fields_values": [
            {"field_id": 902422, "field_name": "fb_ad_id", "values": [{"value": ad_id}]}
        ],
    }


def test_strict_payments_detects_total_drift(monkeypatch):
    cdp_client._cache.clear()
    cdp_client._cache_metadata.clear()
    monkeypatch.setenv("CDP_API_KEY", "test")
    first = [_payment(index, amount=100, direction="income") for index in range(1, 101)]
    responses = [
        _response({"items": first, "total": 101, "data_as_of": "2026-07-08T01:00:00Z"}),
        _response({"items": [_payment(101, amount=100, direction="income")], "total": 102, "data_as_of": "2026-07-08T01:00:00Z"}),
    ]
    monkeypatch.setattr(cdp_client.requests, "get", MagicMock(side_effect=responses))

    result = cdp_client.get_payments_strict(date(2026, 7, 1), date(2026, 7, 7), force_live=True)

    assert result.complete is False
    assert result.page_item_counts == (100, 1)


def test_strict_force_live_bypasses_warm_cache(monkeypatch):
    cdp_client._cache.clear()
    cdp_client._cache_metadata.clear()
    monkeypatch.setenv("CDP_API_KEY", "test")
    getter = MagicMock(
        side_effect=[
            _response({"items": [], "total": 0, "data_as_of": "2026-07-08T01:00:00Z"}),
            _response({"items": [], "total": 0, "data_as_of": "2026-07-08T02:00:00Z"}),
        ]
    )
    monkeypatch.setattr(cdp_client.requests, "get", getter)

    cached = cdp_client.get_payments_strict(date(2026, 7, 1), date(2026, 7, 7))
    live = cdp_client.get_payments_strict(
        date(2026, 7, 1), date(2026, 7, 7), force_live=True
    )

    assert cached.from_cache is False
    assert live.from_cache is False
    assert getter.call_count == 2
    assert live.data_as_of > cached.data_as_of


def test_cdp_chain_uses_contract_and_positive_net(monkeypatch):
    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    strict = StrictPaymentsRead(
        items=(
            _payment(1, amount=100_000, direction="income"),
            _payment(2, amount=20_000, direction="refund"),
        ),
        declared_total=2,
        collected_total=2,
        page_item_counts=(2,),
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
    )
    monkeypatch.setattr(cdp_client, "get_payments_strict", lambda *args, **kwargs: strict)
    monkeypatch.setattr(
        approval_source_cdp,
        "_load_exact_leads_by_ids",
        lambda lead_ids: {101: _lead("ad-1")},
    )

    evidence = approval_source_cdp.load_cdp_evidence(_request(), now, force_live=True)

    assert evidence.complete is True
    payment_record = next(record for record in evidence.records if record.metric.value == "PAYMENTS")
    revenue_record = next(record for record in evidence.records if record.metric.value == "REVENUE")
    assert payment_record.value == 1
    assert payment_record.currency is None
    assert revenue_record.value == Decimal("80000")
    assert {item.payment_id for item in evidence.payments} == {"1", "2"}
    assert all(item.contract_net_lcy == Decimal("80000") for item in evidence.payments)


def test_cdp_chain_without_exact_fb_ad_id_is_incomplete(monkeypatch):
    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    strict = StrictPaymentsRead(
        items=(_payment(1, amount=100_000, direction="income"),),
        declared_total=1,
        collected_total=1,
        page_item_counts=(1,),
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
    )
    monkeypatch.setattr(cdp_client, "get_payments_strict", lambda *args, **kwargs: strict)
    monkeypatch.setattr(
        approval_source_cdp,
        "_load_exact_leads_by_ids",
        lambda lead_ids: {101: {"id": 101, "custom_fields_values": []}},
    )

    evidence = approval_source_cdp.load_cdp_evidence(_request(), now, force_live=True)

    assert evidence.complete is False
    assert evidence.error_code == "CDP_AMO_EXACT_AD_MISSING"


def test_cdp_account_aggregate_uses_exact_requested_ad_universe(monkeypatch):
    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    base = _request(("ad-1", "ad-2"))
    window = base.windows[0]
    subject = SubjectRef(SubjectKind.ACCOUNT, "123")
    claims = tuple(
        FactClaim(
            claim_id=f"account-{metric.value}",
            field_id=None,
            category=FactCategory.BUSINESS_METRIC,
            subject=subject,
            metric=metric,
            value=value,
            source=SourceSystem.CDP_ERP,
            window=window,
            currency=None if metric is Metric.PAYMENTS else "LCY",
        )
        for metric, value in (
            (Metric.PAYMENTS, 2),
            (Metric.REVENUE, Decimal("150000")),
        )
    )
    request = replace(base, subjects=(subject,), claims=claims, account_ids=("123",))
    strict = StrictPaymentsRead(
        items=(
            _payment(1, amount=100_000, direction="income", contract_number=101),
            _payment(2, amount=50_000, direction="income", contract_number=202),
        ),
        declared_total=2,
        collected_total=2,
        page_item_counts=(2,),
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
    )
    monkeypatch.setattr(cdp_client, "get_payments_strict", lambda *args, **kwargs: strict)
    monkeypatch.setattr(
        approval_source_cdp,
        "_load_exact_leads_by_ids",
        lambda lead_ids: {101: _lead("ad-1", 101), 202: _lead("ad-2", 202)},
    )

    evidence = approval_source_cdp.load_cdp_evidence(request, now, force_live=True)

    aggregate = {
        record.metric: record.value
        for record in evidence.records
        if record.subject == subject and record.window == window
    }
    assert evidence.complete is True
    assert aggregate == {Metric.PAYMENTS: 2, Metric.REVENUE: Decimal("150000")}
    assert next(
        record
        for record in evidence.records
        if record.subject == subject and record.metric is Metric.PAYMENTS
    ).currency is None


def test_online_drr_uses_exact_revenue_inputs_without_payment_read(monkeypatch):
    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    window = TimeWindow(
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 8, tzinfo=timezone.utc),
        "UTC",
        "month",
    )
    subject = SubjectRef(SubjectKind.ACCOUNT, "ONLINE")
    claim = FactClaim(
        claim_id="drr",
        field_id=None,
        category=FactCategory.BUSINESS_METRIC,
        subject=subject,
        metric=Metric.DRR_PCT,
        value=Decimal("10"),
        source=SourceSystem.CDP_ERP,
        window=window,
    )
    request = EvidenceRequest(
        request_id="online",
        purpose="REPORT",
        action_kind=None,
        generated_at=now,
        subjects=(subject,),
        claims=(claim,),
        required_sources=(SourceSystem.CDP_ERP,),
        windows=(window,),
        account_ids=(),
        adset_ids=(),
        ad_ids=(),
        card_ids=(),
        staged_relative_paths=(),
        include_full_inventory=False,
        force_live=True,
        max_age_seconds=60,
    )
    response = CdpResponse(
        payload={
            "items": [
                {
                    "report_date": "2026-07-03",
                    "city": "Онлайн",
                    "ad_spend": 100,
                    "revenue_new": 100_000,
                    "usd_rate": 100,
                    "drr_new": 99,
                }
            ]
        },
        status_code=200,
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        cache_age_seconds=0,
        response_headers_sha256="a" * 64,
    )
    monkeypatch.setattr(cdp_client, "_request_with_meta", lambda *args, **kwargs: response)
    monkeypatch.setattr(
        cdp_client,
        "get_payments_strict",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("payment read forbidden")),
    )

    evidence = approval_source_cdp.load_cdp_evidence(request, now, force_live=True)

    drr = next(record for record in evidence.records if record.metric is Metric.DRR_PCT)
    assert evidence.complete is True
    assert drr.value == Decimal("10")
    assert "mode:EXACT_REVENUE" in drr.entity_ids


def test_online_records_expose_counts_context_and_same_currency_inputs(monkeypatch):
    start = datetime(2026, 7, 3, tzinfo=timezone.utc)
    now = start + timedelta(days=1, hours=1)
    window = TimeWindow(start, start + timedelta(days=1), "UTC", "online_day")
    subject = SubjectRef(SubjectKind.ACCOUNT, "ONLINE")
    specifications = (
        (Metric.DISPLAY_CONTEXT, FactCategory.DISPLAY_CONTEXT, "2026-07-03", None),
        (Metric.SPEND, FactCategory.BUSINESS_METRIC, Decimal("100"), "USD"),
        (Metric.LEADS, FactCategory.BUSINESS_METRIC, 12, None),
        (Metric.QUALS, FactCategory.BUSINESS_METRIC, 4, None),
        (Metric.QUAL_PCT, FactCategory.BUSINESS_METRIC, Decimal("33.3"), None),
        (Metric.REVENUE, FactCategory.BUSINESS_METRIC, Decimal("100000"), "LCY"),
        (Metric.DRR_PCT, FactCategory.BUSINESS_METRIC, Decimal("10"), None),
        (Metric.MATCH_STATE, FactCategory.DISPLAY_CONTEXT, "EXACT_REVENUE", None),
    )
    claims = tuple(
        FactClaim(
            claim_id=f"online-{index}",
            field_id=None,
            category=category,
            subject=subject,
            metric=metric,
            value=value,
            source=SourceSystem.CDP_ERP,
            window=window,
            currency=currency,
        )
        for index, (metric, category, value, currency) in enumerate(specifications)
    )
    request = EvidenceRequest(
        request_id="online-complete",
        purpose="REPORT",
        action_kind=None,
        generated_at=now,
        subjects=(subject,),
        claims=claims,
        required_sources=(SourceSystem.CDP_ERP,),
        windows=(window,),
        account_ids=("ONLINE",),
        adset_ids=(),
        ad_ids=(),
        card_ids=(),
        staged_relative_paths=(),
        include_full_inventory=False,
        force_live=False,
        max_age_seconds=60,
    )
    response = CdpResponse(
        payload={
            "items": [
                {
                    "report_date": "2026-07-03",
                    "city": "Онлайн",
                    "ad_spend": 100,
                    "leads": 12,
                    "qleads": 4,
                    "pct_qleads": "33.3",
                    "revenue_new": 100_000,
                    "usd_rate": 100,
                    "drr_new": 99,
                }
            ]
        },
        status_code=200,
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        cache_age_seconds=0,
        response_headers_sha256="a" * 64,
    )
    monkeypatch.setattr(cdp_client, "_request_with_meta", lambda *args, **kwargs: response)

    evidence = approval_source_cdp.load_cdp_evidence(request, now, force_live=False)

    values = {
        (record.metric, record.currency): record.value
        for record in evidence.records
        if record.subject == subject and record.window == window
    }
    assert evidence.complete is True
    assert values == {
        (Metric.DISPLAY_CONTEXT, None): "2026-07-03",
        (Metric.SPEND, "USD"): Decimal("100"),
        (Metric.SPEND, "LCY"): Decimal("10000"),
        (Metric.LEADS, None): 12,
        (Metric.QUALS, None): 4,
        (Metric.QUAL_PCT, None): Decimal("33.3"),
        (Metric.REVENUE, "LCY"): Decimal("100000"),
        (Metric.DRR_PCT, None): Decimal("10"),
        (Metric.MATCH_STATE, None): "EXACT_REVENUE",
    }
    drr = next(record for record in evidence.records if record.metric is Metric.DRR_PCT)
    assert "coverage:complete:2026-07-03..2026-07-03" in drr.entity_ids
    assert "mode:EXACT_REVENUE" in drr.entity_ids


def test_online_drr_gap_stays_unproven_without_exact_or_fallback_inputs(monkeypatch):
    start = datetime(2026, 7, 3, tzinfo=timezone.utc)
    window = TimeWindow(start, start + timedelta(days=1), "UTC", "online_day")
    subject = SubjectRef(SubjectKind.ACCOUNT, "ONLINE")
    claims = tuple(
        FactClaim(
            claim_id=f"missing-{metric.value}",
            field_id=None,
            category=(
                FactCategory.DISPLAY_CONTEXT
                if metric is Metric.MATCH_STATE
                else FactCategory.BUSINESS_METRIC
            ),
            subject=subject,
            metric=metric,
            value=value,
            source=SourceSystem.CDP_ERP,
            window=window,
        )
        for metric, value in (
            (Metric.DRR_PCT, Decimal("5")),
            (Metric.MATCH_STATE, "WEIGHTED_FALLBACK"),
        )
    )
    request = EvidenceRequest(
        request_id="online-gap",
        purpose="REPORT",
        action_kind=None,
        generated_at=start,
        subjects=(subject,),
        claims=claims,
        required_sources=(SourceSystem.CDP_ERP,),
        windows=(window,),
        account_ids=("ONLINE",),
        adset_ids=(),
        ad_ids=(),
        card_ids=(),
        staged_relative_paths=(),
        include_full_inventory=False,
        force_live=False,
        max_age_seconds=60,
    )
    response = CdpResponse(
        payload={
            "items": [
                {
                    "report_date": "2026-07-03",
                    "city": "Онлайн",
                    "ad_spend": 100,
                }
            ]
        },
        status_code=200,
        fetched_at=start,
        data_as_of=start,
        from_cache=False,
        cache_age_seconds=0,
        response_headers_sha256="a" * 64,
    )
    monkeypatch.setattr(cdp_client, "_request_with_meta", lambda *args, **kwargs: response)

    evidence = approval_source_cdp.load_cdp_evidence(request, start, force_live=False)

    assert evidence.complete is True
    assert not any(
        record.metric in {Metric.DRR_PCT, Metric.MATCH_STATE}
        for record in evidence.records
    )


def test_window_ad_ids_walks_payment_to_lead_chain(monkeypatch):
    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    strict = StrictPaymentsRead(
        items=(
            _payment(1, amount=100_000, direction="income", contract_number=101),
            _payment(2, amount=50_000, direction="income", contract_number=102),
        ),
        declared_total=2,
        collected_total=2,
        page_item_counts=(2,),
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
    )
    monkeypatch.setattr(cdp_client, "get_payments_strict", lambda *args, **kwargs: strict)
    monkeypatch.setattr(
        approval_source_cdp,
        "_load_exact_leads_by_ids",
        lambda lead_ids: {101: _lead("ad-9", 101), 102: _lead("ad-1", 102)},
    )

    observed = approval_source_cdp.load_cdp_window_ad_ids(
        _request().windows, force_live=True
    )

    assert observed == ("ad-1", "ad-9")


def test_window_ad_ids_rejects_payment_without_exact_ad_id(monkeypatch):
    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    strict = StrictPaymentsRead(
        items=(_payment(1, amount=100_000, direction="income"),),
        declared_total=1,
        collected_total=1,
        page_item_counts=(1,),
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
    )
    monkeypatch.setattr(cdp_client, "get_payments_strict", lambda *args, **kwargs: strict)
    monkeypatch.setattr(
        approval_source_cdp,
        "_load_exact_leads_by_ids",
        lambda lead_ids: {101: {"id": 101, "custom_fields_values": []}},
    )

    try:
        approval_source_cdp.load_cdp_window_ad_ids(_request().windows, force_live=True)
    except approval_source_cdp.CdpEvidenceError as failure:
        assert str(failure) == "CDP_AMO_EXACT_AD_MISSING"
    else:
        raise AssertionError("незакрытая цепочка платёж→сделка обязана отказывать")


def test_live_read_is_not_reported_as_fetched_in_the_future(monkeypatch):
    """HTTP-фетч позже опорного now не делает живой CDP «устаревшим».

    Пять источников действия читаются последовательно с одним ``now``, а
    ``_freshness`` считает возраст как ``now - fetched_at`` с нижней границей
    -1с. Настоящее время фетча (now + секунды) давало SOURCE_STALE и терминальный
    LIVE_REVIEW_DENIED на каждом одобренном действии. Кеш при этом обязан
    остаться честно старым.
    """

    from services.approval_sources import _freshness

    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    late = now + timedelta(seconds=15)
    early = now - timedelta(seconds=15)

    def _strict(*_args, **_kwargs):
        return StrictPaymentsRead(
            items=(),
            declared_total=0,
            collected_total=0,
            page_item_counts=(0,),
            fetched_at=late,
            data_as_of=now,
            from_cache=False,
            complete=True,
        )

    monkeypatch.setattr(cdp_client, "get_payments_strict", _strict)
    monkeypatch.setattr(
        approval_source_cdp, "_load_exact_leads_by_ids", lambda lead_ids: {}
    )

    live = approval_source_cdp.load_cdp_evidence(_request(), now, force_live=True)

    assert live.fetched_at == now
    assert _freshness(live, now, 1).fresh is True

    cached = StrictPaymentsRead(
        items=(),
        declared_total=0,
        collected_total=0,
        page_item_counts=(0,),
        fetched_at=early,
        data_as_of=now,
        from_cache=True,
        complete=True,
    )
    monkeypatch.setattr(cdp_client, "get_payments_strict", lambda *a, **k: cached)

    stale = approval_source_cdp.load_cdp_evidence(_request(), now, force_live=False)

    assert stale.fetched_at == early
    assert (stale.from_cache, _freshness(stale, now, 1).fresh) == (True, False)


def test_payments_window_and_lead_chain_are_read_once_per_scope(monkeypatch):
    """Окно платежей и цепочка сделок читаются один раз на прогон.

    Одно задание читает живые источники трижды, а окно решения у всех заданий
    прогона одно и то же. Без переиспользования это 3×N одинаковых запросов к
    CDP и AMO — именно они не влезали в бюджет задания.
    """

    from services import approval_source_amo
    from services.live_read_scope import live_read_scope

    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    strict = StrictPaymentsRead(
        items=(_payment(1, amount=100_000, direction="income"),),
        declared_total=1,
        collected_total=1,
        page_item_counts=(1,),
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
    )
    payment_calls: list[tuple] = []
    lead_pages: list[object] = []

    def _strict(*args, **kwargs):
        payment_calls.append((args, tuple(sorted(kwargs.items()))))
        return strict

    def _leads_page(params):
        lead_pages.append(params)
        return [_lead("ad-1")]

    monkeypatch.setattr(cdp_client, "get_payments_strict", _strict)
    monkeypatch.setattr(approval_source_amo, "_raw_leads_page", _leads_page)

    with live_read_scope():
        first = approval_source_cdp.load_cdp_evidence(
            _request(), now, force_live=True
        )
        second = approval_source_cdp.load_cdp_evidence(
            _request(), now, force_live=True
        )
        # Другое задание того же прогона: другое объявление, то же окно.
        third = approval_source_cdp.load_cdp_evidence(
            _request(("ad-2",)), now, force_live=True
        )

    assert (first.complete, second.complete, third.complete) == (True, True, True)
    assert len(payment_calls) == 1
    assert len(lead_pages) == 1
    assert first.fetched_at == second.fetched_at == strict.fetched_at
    assert (first.from_cache, second.from_cache) == (False, False)
    assert [record.value for record in first.records] == [
        record.value for record in second.records
    ]

    payment_calls.clear()
    lead_pages.clear()
    approval_source_cdp.load_cdp_evidence(_request(), now, force_live=True)
    approval_source_cdp.load_cdp_evidence(_request(), now, force_live=True)
    assert (len(payment_calls), len(lead_pages)) == (2, 2)


def test_payment_without_contract_does_not_break_the_window(monkeypatch):
    """Приход без договора — ничей, а не повод завалить проверку окна.

    В ERP такие встречаются регулярно (замер показал, что это меньшинство
    приходов). Пока они валили окно, проверка денежных фактов не могла
    подтвердить ни одного отчёта.
    """
    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    strict = StrictPaymentsRead(
        items=(
            _payment(1, amount=100_000, direction="income"),
            # Ничей платёж: договора нет, к объявлению не относится.
            _payment(9, amount=50_000, direction="income", contract_number=""),
        ),
        declared_total=2,
        collected_total=2,
        page_item_counts=(2,),
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
    )
    monkeypatch.setattr(cdp_client, "get_payments_strict", lambda *args, **kwargs: strict)
    monkeypatch.setattr(
        approval_source_cdp,
        "_load_exact_leads_by_ids",
        lambda lead_ids: {101: _lead("ad-1")},
    )

    evidence = approval_source_cdp.load_cdp_evidence(_request(), now, force_live=True)

    assert evidence.complete is True
    assert evidence.error_code is None
    revenue_record = next(
        record for record in evidence.records if record.metric.value == "REVENUE"
    )
    # Ничьи деньги не приписаны объявлению — в выручку вошли только 100 000.
    assert revenue_record.value == Decimal("100000")
    assert {item.payment_id for item in evidence.payments} == {"1"}


def test_unparseable_contract_still_fails_the_window(monkeypatch):
    """Непустой, но неразбираемый договор по-прежнему валит проверку.

    Пустой договор — известное свойство данных. Договор с буквами — признак
    смены формата: проглотить его молча значит занизить выручку объявления.
    """
    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    strict = StrictPaymentsRead(
        items=(_payment(1, amount=100_000, direction="income", contract_number="ДГ-77"),),
        declared_total=1,
        collected_total=1,
        page_item_counts=(1,),
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
    )
    monkeypatch.setattr(cdp_client, "get_payments_strict", lambda *args, **kwargs: strict)

    evidence = approval_source_cdp.load_cdp_evidence(_request(), now, force_live=True)

    assert evidence.complete is False
    assert evidence.error_code == "CDP_CONTRACT_INVALID"


def test_cdp_chain_tolerates_confirmed_deleted_lead(monkeypatch):
    """Платёж на удалённую в AMO сделку не бракует окно, а выпадает из выручки.

    Успешное exact-чтение — доказательство отсутствия сделки: сетевые дыры дают
    исключение, а не пустой ответ.
    """
    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    strict = StrictPaymentsRead(
        items=(
            _payment(1, amount=100_000, direction="income", contract_number=101),
            _payment(2, amount=50_000, direction="income", contract_number=999),
        ),
        declared_total=2,
        collected_total=2,
        page_item_counts=(2,),
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
    )
    monkeypatch.setattr(cdp_client, "get_payments_strict", lambda *a, **k: strict)
    # AMO успешно ответил и вернул только сделку 101 — 999 удалена.
    monkeypatch.setattr(
        approval_source_cdp,
        "_load_exact_leads_by_ids",
        lambda lead_ids: {101: _lead("ad-1")},
    )

    evidence = approval_source_cdp.load_cdp_evidence(_request(), now, force_live=True)

    assert evidence.complete is True
    revenue_record = next(
        record for record in evidence.records if record.metric.value == "REVENUE"
    )
    # Выручка считается только по живой сделке: платёж удалённой не участвует.
    assert revenue_record.value == Decimal("100000")


def test_cdp_chain_mass_missing_still_blocks(monkeypatch):
    """Массовая пропажа сделок — дрейф источника, а не удаления: окно бракуется."""
    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    items = tuple(
        _payment(i, amount=10_000, direction="income", contract_number=100 + i)
        for i in range(1, 21)
    )
    strict = StrictPaymentsRead(
        items=items,
        declared_total=len(items),
        collected_total=len(items),
        page_item_counts=(len(items),),
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
    )
    monkeypatch.setattr(cdp_client, "get_payments_strict", lambda *a, **k: strict)
    # AMO вернул только одну сделку из двадцати — так удаления не выглядят.
    monkeypatch.setattr(
        approval_source_cdp,
        "_load_exact_leads_by_ids",
        lambda lead_ids: {101: _lead("ad-1")},
    )

    evidence = approval_source_cdp.load_cdp_evidence(_request(), now, force_live=True)

    assert evidence.complete is False
    assert "CDP_AMO_CHAIN_INCOMPLETE" in (evidence.error_code or "")


def test_cdp_chain_loader_failure_still_raises(monkeypatch):
    """Сбой чтения AMO (не пустота!) по-прежнему валит проверку, а не глотается."""
    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    strict = StrictPaymentsRead(
        items=(_payment(1, amount=100_000, direction="income"),),
        declared_total=1,
        collected_total=1,
        page_item_counts=(1,),
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
    )
    monkeypatch.setattr(cdp_client, "get_payments_strict", lambda *a, **k: strict)

    def _boom(lead_ids):
        raise RuntimeError("AMO обрыв")

    monkeypatch.setattr(approval_source_cdp, "_load_exact_leads_by_ids", _boom)

    evidence = approval_source_cdp.load_cdp_evidence(_request(), now, force_live=True)
    assert evidence.complete is False


def test_cdp_chain_untagged_organic_lead_is_skipped_not_fatal(monkeypatch):
    """Оплаченная органика без fb_ad_id не бракует окно — она просто ничья."""
    now = datetime(2026, 7, 8, 2, tzinfo=timezone.utc)
    strict = StrictPaymentsRead(
        items=(
            _payment(1, amount=100_000, direction="income", contract_number=101),
            _payment(2, amount=70_000, direction="income", contract_number=202),
        ),
        declared_total=2,
        collected_total=2,
        page_item_counts=(2,),
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
    )
    monkeypatch.setattr(cdp_client, "get_payments_strict", lambda *a, **k: strict)
    monkeypatch.setattr(
        approval_source_cdp,
        "_load_exact_leads_by_ids",
        lambda lead_ids: {
            101: _lead("ad-1"),
            202: {"id": 202, "custom_fields_values": []},  # органика без метки
        },
    )

    evidence = approval_source_cdp.load_cdp_evidence(_request(), now, force_live=True)

    assert evidence.complete is True
    revenue_record = next(
        record for record in evidence.records if record.metric.value == "REVENUE"
    )
    # Выручка объявления — только по меченой сделке; органика не приписана.
    assert revenue_record.value == Decimal("100000")
