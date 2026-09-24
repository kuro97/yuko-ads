from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from services import approval_source_amo
from services.approval_checker_models import (
    EvidenceRequest,
    FactCategory,
    FactClaim,
    Metric,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
)


def _request() -> EvidenceRequest:
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    return EvidenceRequest(
        request_id="amo-test",
        purpose="ACTION",
        action_kind=None,
        generated_at=start,
        subjects=(),
        claims=(),
        required_sources=(SourceSystem.AMO,),
        windows=(TimeWindow(start, start + timedelta(days=1), "UTC", "day"),),
        account_ids=(),
        adset_ids=(),
        ad_ids=("ad-1", "ad-2"),
        card_ids=(),
        staged_relative_paths=(),
        include_full_inventory=False,
        force_live=True,
        max_age_seconds=60,
    )


def _lead(lead_id: int, ad_id: str | None, *, name: str = "same") -> dict[str, object]:
    fields: list[dict[str, object]] = [
        {"field_name": "fb_ad_name", "values": [{"value": name}]}
    ]
    if ad_id is not None:
        fields.append(
            {"field_id": 902422, "field_name": "fb_ad_id", "values": [{"value": ad_id}]}
        )
    return {
        "id": lead_id,
        "created_at": int(datetime(2026, 7, 1, 12, tzinfo=timezone.utc).timestamp()),
        "updated_at": int(datetime(2026, 7, 1, 13, tzinfo=timezone.utc).timestamp()),
        "custom_fields_values": fields,
    }


def test_amo_same_names_are_attributed_by_exact_id(monkeypatch):
    rows = [_lead(1, "ad-1"), _lead(2, "ad-2")]
    monkeypatch.setattr(approval_source_amo, "_raw_leads_page", lambda params: rows)
    now = datetime(2026, 7, 1, 14, tzinfo=timezone.utc)

    evidence = approval_source_amo.load_amo_evidence(_request(), now, force_live=True)

    lead_records = {record.subject.subject_id: record.value for record in evidence.records if record.metric is Metric.LEADS}
    assert evidence.complete is True
    assert lead_records == {"ad-1": 1, "ad-2": 1}


def test_amo_name_only_never_proves_attribution(monkeypatch):
    monkeypatch.setattr(
        approval_source_amo,
        "_raw_leads_page",
        lambda params: [_lead(1, None, name="name-of-ad-1")],
    )
    now = datetime(2026, 7, 1, 14, tzinfo=timezone.utc)

    evidence = approval_source_amo.load_amo_evidence(_request(), now, force_live=True)

    lead_records = {record.subject.subject_id: record.value for record in evidence.records if record.metric is Metric.LEADS}
    assert lead_records == {"ad-1": 0, "ad-2": 0}


def test_amo_duplicate_lead_makes_source_incomplete(monkeypatch):
    rows = [_lead(1, "ad-1"), _lead(1, "ad-1")]
    monkeypatch.setattr(approval_source_amo, "_raw_leads_page", lambda params: rows)
    now = datetime(2026, 7, 1, 14, tzinfo=timezone.utc)

    evidence = approval_source_amo.load_amo_evidence(_request(), now, force_live=True)

    assert evidence.complete is False
    assert evidence.error_code == "AMO_LEAD_DUPLICATE"


def test_amo_account_aggregate_stays_unproven_without_exact_ad_universe(
    monkeypatch,
):
    base = _request()
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
            source=SourceSystem.AMO,
            window=window,
        )
        for metric, value in ((Metric.LEADS, 2), (Metric.QUALS, 1))
    )
    request = replace(
        base,
        purpose="REPORT",
        subjects=(subject,),
        claims=claims,
        account_ids=("123",),
        ad_ids=(),
        force_live=False,
    )
    rows = [_lead(1, None, name="organic"), _lead(2, "ad-other")]
    monkeypatch.setattr(approval_source_amo, "_raw_leads_page", lambda params: rows)

    evidence = approval_source_amo.load_amo_evidence(
        request,
        datetime(2026, 7, 1, 14, tzinfo=timezone.utc),
        force_live=False,
    )

    aggregate = {
        record.metric: record.value
        for record in evidence.records
        if record.subject == subject and record.window == window
    }
    assert evidence.complete is True
    assert aggregate == {}


def test_amo_account_aggregate_uses_only_exact_requested_ad_universe(monkeypatch):
    base = _request()
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
            source=SourceSystem.AMO,
            window=window,
        )
        for metric, value in ((Metric.LEADS, 2), (Metric.QUALS, 1))
    )
    request = replace(
        base,
        purpose="REPORT",
        subjects=(subject,),
        claims=claims,
        account_ids=("123",),
        force_live=False,
    )
    rows = [
        _lead(1, "ad-1"),
        _lead(2, "ad-2"),
        _lead(3, "ad-other"),
        _lead(4, None, name="ad-1"),
    ]
    qualifications = iter((True, False))
    monkeypatch.setattr(approval_source_amo, "_raw_leads_page", lambda params: rows)
    monkeypatch.setattr(
        approval_source_amo.amo,
        "_is_qualified",
        lambda lead: next(qualifications),
    )

    evidence = approval_source_amo.load_amo_evidence(
        request,
        datetime(2026, 7, 1, 14, tzinfo=timezone.utc),
        force_live=False,
    )

    aggregate = {
        record.metric: record.value
        for record in evidence.records
        if record.subject == subject and record.window == window
    }
    assert evidence.complete is True
    assert aggregate == {Metric.LEADS: 2, Metric.QUALS: 1}


def test_window_ad_ids_collects_distinct_exact_field_values(monkeypatch):
    rows = [
        _lead(1, "ad-1"),
        _lead(2, "ad-2"),
        _lead(3, "ad-1"),
        _lead(4, None, name="ad-3"),
    ]
    monkeypatch.setattr(approval_source_amo, "_raw_leads_page", lambda params: rows)
    windows = _request().windows

    assert approval_source_amo.load_amo_window_ad_ids(windows) == ("ad-1", "ad-2")


def test_window_ad_ids_fails_closed_on_broken_payload(monkeypatch):
    def broken(_params):
        raise approval_source_amo.AmoEvidenceError("AMO_LEADS_INVALID")

    monkeypatch.setattr(approval_source_amo, "_raw_leads_page", broken)

    try:
        approval_source_amo.load_amo_window_ad_ids(_request().windows)
    except approval_source_amo.AmoEvidenceError as failure:
        assert str(failure) == "AMO_LEADS_INVALID"
    else:
        raise AssertionError("сломанный AMO обязан подниматься наверх")


# ---------------------------------------------------------------------------
# Точечное чтение по ad_id (путь исполнения одобренного действия)
# ---------------------------------------------------------------------------


def _params_items(params) -> list[tuple[str, object]]:
    return list(params.items()) if isinstance(params, dict) else list(params)


def _lead_matches_query(row: dict[str, object], term: str) -> bool:
    """Полнотекст AMO: терм встречается в любом значении полей лида."""

    for field in row.get("custom_fields_values") or []:
        for value in field.get("values") or []:
            if isinstance(value, dict) and term in str(value.get("value") or ""):
                return True
    return False


def _fake_amo_api(rows, calls: list | None = None):
    """Отвечает как настоящий AMO аккаунта acme: query и окно — да, фильтр — 400.

    Честный фейк нужен именно здесь: фейк, «уважающий» фильтр по кастомному
    полю, которого у аккаунта нет (AMO отвечает 400 «Invalid filter for
    current account»), давал бы зелёный тест, а исполнение зависало бы
    на каждом одобрении. Теперь фейк отвергает фильтр так же, как настоящий API.
    """

    def _page(params):
        items = _params_items(params)
        if calls is not None:
            calls.append(items)
        if any(key.startswith("filter[custom_fields_values]") for key, _ in items):
            raise Exception(
                "AMO API ошибка (400): Invalid filter for current account"
            )
        page = next(int(value) for key, value in items if key == "page")
        from_ts = next(
            int(value) for key, value in items if key == "filter[created_at][from]"
        )
        to_ts = next(
            int(value) for key, value in items if key == "filter[created_at][to]"
        )
        term = next((str(value) for key, value in items if key == "query"), None)
        if page > 1:
            return []
        return [
            row
            for row in rows
            if from_ts <= int(row["created_at"]) <= to_ts
            and (term is None or _lead_matches_query(row, term))
        ]

    return _page


def _forbid_window_reads(monkeypatch):
    def _forbidden(_window):
        raise AssertionError(
            "оконное чтение AMO на пути исполнения действия запрещено"
        )

    monkeypatch.setattr(approval_source_amo, "_load_window_leads", _forbidden)


def _metric_values(evidence, metric: Metric) -> dict[str, object]:
    return {
        record.subject.subject_id: record.value
        for record in evidence.records
        if record.metric is metric and record.subject.kind is SubjectKind.AD
    }


def test_action_evidence_never_scans_the_whole_funnel(monkeypatch):
    """Пауза одного объявления не имеет права вычитывать поток воронки.

    Точечный путь — это ``query`` по каждому ad_id: фильтр по кастомному полю
    аккаунту недоступен (AMO даёт на него 400), и запрос с этим
    фильтром ломает исполнение одобренных пауз. Фейк здесь отвечает
    как настоящий API, поэтому регресс на фильтр падает сразу.
    """

    rows = [_lead(1, "ad-1"), _lead(2, "ad-2"), _lead(3, "ad-foreign")]
    calls: list = []
    _forbid_window_reads(monkeypatch)
    monkeypatch.setattr(
        approval_source_amo, "_raw_leads_page", _fake_amo_api(rows, calls)
    )
    now = datetime(2026, 7, 1, 14, tzinfo=timezone.utc)

    evidence = approval_source_amo.load_amo_evidence(_request(), now, force_live=True)

    assert evidence.complete is True
    assert len(calls) == 2, "по одному query-запросу на каждый ad_id"
    assert [
        next(value for key, value in call if key == "query") for call in calls
    ] == ["ad-1", "ad-2"]
    assert not any(
        key.startswith("filter[custom_fields_values]")
        for call in calls
        for key, _ in call
    ), "фильтр по кастомному полю мёртв для этого аккаунта (400)"
    assert _metric_values(evidence, Metric.LEADS) == {"ad-1": 1, "ad-2": 1}


def test_pointed_path_returns_the_same_values_as_window_path(monkeypatch):
    """На одних данных точечный и оконный путь считают одинаково."""

    rows = [
        _lead(1, "ad-1"),
        _lead(2, "ad-1"),
        _lead(3, "ad-2"),
        _lead(4, "ad-foreign"),
        _lead(5, None, name="organic"),
    ]
    monkeypatch.setattr(approval_source_amo, "_raw_leads_page", _fake_amo_api(rows))
    monkeypatch.setattr(
        approval_source_amo.amo, "_is_qualified", lambda lead: lead["id"] % 2 == 1
    )
    now = datetime(2026, 7, 1, 14, tzinfo=timezone.utc)

    pointed = approval_source_amo.load_amo_evidence(_request(), now, force_live=True)
    windowed = approval_source_amo.load_amo_evidence(
        replace(_request(), purpose="REPORT", force_live=False),
        now,
        force_live=False,
    )

    assert (pointed.complete, windowed.complete) == (True, True)
    assert _metric_values(pointed, Metric.LEADS) == {"ad-1": 2, "ad-2": 1}
    assert _metric_values(pointed, Metric.LEADS) == _metric_values(
        windowed, Metric.LEADS
    )
    assert _metric_values(pointed, Metric.QUALS) == _metric_values(
        windowed, Metric.QUALS
    )
    assert {
        (record.subject, record.metric, record.value, record.entity_ids)
        for record in pointed.records
    } == {
        (record.subject, record.metric, record.value, record.entity_ids)
        for record in windowed.records
    }


def test_manifest_fact_and_live_summary_read_amo_identically(monkeypatch):
    """Обе стороны сравнения факта читают AMO одним путём — FACT_MISMATCH нет.

    Слева — чтение сборки манифеста (``owner_action_live_manifest``), справа —
    чтение свежей сводки gateway (``approval_sources._action_request``). Запросы
    разные (у второго ещё и соседи адсета), поэтому расхождение путей чтения дало
    бы FACT_MISMATCH и одобренная пауза упиралась бы в DENIED.
    """

    import uuid

    from services import approval_rules, approval_sources
    from services import owner_action_live_manifest as live
    from services.action_manifests import build_pause_manifest
    from services.approval_checker_models import (
        ActionKind,
        ActionOrigin,
        ClaimState,
        PauseCandidate,
    )

    now = datetime(2026, 7, 1, 14, tzinfo=timezone.utc)
    window = TimeWindow(
        now - timedelta(days=30), now, "UTC", live._PAUSE_WINDOW_SEMANTIC
    )
    rows = [
        _lead(1, "ad-1"),
        _lead(2, "ad-1"),
        _lead(3, "ad-2"),
        _lead(4, "ad-foreign"),
    ]
    monkeypatch.setattr(approval_source_amo, "_raw_leads_page", _fake_amo_api(rows))
    monkeypatch.setattr(
        approval_source_amo.amo, "_is_qualified", lambda lead: lead["id"] == 1
    )
    subject = SubjectRef(SubjectKind.AD, "ad-1", "adset-1")

    build_evidence = approval_source_amo.load_amo_evidence(
        live._evidence_request(
            request_id="owner-pause:ad-1",
            action_kind=ActionKind.PAUSE,
            subjects=(subject,),
            windows=(window,),
            adset_id="adset-1",
            ad_ids=("ad-1",),
            now=now,
        ),
        now,
        force_live=True,
    )
    quals = live._int_fact(build_evidence, subject, Metric.QUALS, window)
    assert quals == 1

    manifest = build_pause_manifest(
        PauseCandidate(
            ad_id="ad-1",
            adset_id="adset-1",
            display_name="ad-1",
            reason_code="LOW_ROMI",
            decision_window=window,
            expected_before_status="ACTIVE",
            expected_after_status="PAUSED",
            spend=Decimal("12.34"),
            spend_currency="USD",
            leads=5,
            quals=quals,
            payments=(),
            revenue_lcy=Decimal("0"),
            pre_inventory_sha256="a" * 64,
            sibling_active_ids=("ad-2",),
        ),
        origin=ActionOrigin.TELEGRAM,
        idempotency_key=str(uuid.uuid4()),
        now=now,
    )
    live_evidence = approval_source_amo.load_amo_evidence(
        approval_sources._action_request(manifest, now),
        now,
        force_live=True,
    )

    claim = next(fact for fact in manifest.facts if fact.source is SourceSystem.AMO)
    record, duplicates = approval_rules._record_for_claim(claim, live_evidence.records)
    outcome = approval_rules.compare_claim(claim, record)

    assert duplicates == ()
    assert (outcome.state, outcome.issues) == (ClaimState.MATCH, ())


def test_pointed_read_is_reused_inside_one_run_scope(monkeypatch):
    """Три чтения одного задания и несколько заданий прогона — один запрос."""

    from services.live_read_scope import live_read_scope

    rows = [_lead(1, "ad-1"), _lead(2, "ad-2")]
    calls: list = []
    monkeypatch.setattr(
        approval_source_amo, "_raw_leads_page", _fake_amo_api(rows, calls)
    )
    now = datetime(2026, 7, 1, 14, tzinfo=timezone.utc)
    request = _request()

    with live_read_scope():
        first = approval_source_amo.load_amo_evidence(request, now, force_live=True)
        second = approval_source_amo.load_amo_evidence(request, now, force_live=True)
        with live_read_scope():  # вложенный вход не сбрасывает уже прочитанное
            third = approval_source_amo.load_amo_evidence(request, now, force_live=True)

    # Два ad_id = два query-запроса, но ровно один раз на всю область.
    assert len(calls) == 2
    assert _metric_values(first, Metric.LEADS) == _metric_values(second, Metric.LEADS)
    assert _metric_values(first, Metric.QUALS) == _metric_values(third, Metric.QUALS)

    calls.clear()
    approval_source_amo.load_amo_evidence(request, now, force_live=True)
    approval_source_amo.load_amo_evidence(request, now, force_live=True)
    assert len(calls) == 4, "вне области кеша быть не должно"


def test_pointed_read_fails_closed_when_query_floods(monkeypatch):
    """Поток вместо точечного ответа — INCOMPLETE за считанные запросы, не зависание."""

    calls: list[int] = []

    def _page(params):
        page = next(
            int(value) for key, value in _params_items(params) if key == "page"
        )
        calls.append(page)
        base = page * 10_000
        return [
            _lead(base + index, "ad-foreign")
            for index in range(approval_source_amo._PAGE_SIZE)
        ]

    monkeypatch.setattr(approval_source_amo, "_raw_leads_page", _page)
    now = datetime(2026, 7, 1, 14, tzinfo=timezone.utc)

    evidence = approval_source_amo.load_amo_evidence(_request(), now, force_live=True)

    assert evidence.complete is False
    assert evidence.error_code == "AMO_EXACT_PAGE_LIMIT_EXCEEDED"
    assert len(calls) == approval_source_amo._EXACT_AD_MAX_PAGES


def test_pointed_read_refuses_report_sized_ad_universe(monkeypatch):
    """Тысячи ad_id — это отчётный агрегат, ему точечный путь не подходит."""

    monkeypatch.setattr(
        approval_source_amo,
        "_raw_leads_page",
        lambda _params: (_ for _ in ()).throw(
            AssertionError("запрос не должен уйти в AMO")
        ),
    )
    too_many = tuple(
        f"ad-{index}" for index in range(approval_source_amo._EXACT_AD_MAX_IDS + 1)
    )
    now = datetime(2026, 7, 1, 14, tzinfo=timezone.utc)

    evidence = approval_source_amo.load_amo_evidence(
        replace(_request(), ad_ids=too_many),
        now,
        force_live=True,
    )

    assert evidence.complete is False
    assert evidence.error_code == "AMO_EXACT_AD_LIMIT_EXCEEDED"


def test_expired_job_budget_is_named_not_hidden_behind_timeout(monkeypatch):
    """Истёкший бюджет задания обязан назвать себя, а не притвориться «AMO лежит».

    Сценарий: журнал писал голое «Timeout», и корень (чтения Facebook не
    влезают в бюджет задания) искали бы в AMO, который на самом деле отвечал
    быстро.
    """
    import requests

    def expired(_params):
        raise requests.exceptions.Timeout("AMO_REQUEST_BUDGET_EXPIRED")

    monkeypatch.setattr(approval_source_amo, "_raw_leads_page", expired)
    now = datetime(2026, 7, 1, 14, tzinfo=timezone.utc)

    evidence = approval_source_amo.load_amo_evidence(_request(), now, force_live=True)

    assert evidence.complete is False
    assert evidence.error_code == "Timeout:AMO_REQUEST_BUDGET_EXPIRED"


def test_transport_error_without_message_keeps_bare_class_name(monkeypatch):
    """Пустое сообщение не должно превращаться в «Timeout:» с висящим двоеточием."""

    import requests

    def broken(_params):
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr(approval_source_amo, "_raw_leads_page", broken)
    now = datetime(2026, 7, 1, 14, tzinfo=timezone.utc)

    evidence = approval_source_amo.load_amo_evidence(_request(), now, force_live=True)

    assert evidence.error_code == "ConnectionError"


def test_exact_lead_cache_is_per_lead_inside_scope(monkeypatch):
    """Окна платежей разных заданий дают разные, но пересекающиеся наборы сделок: читаются только новые id.

    Регрессия: кэш по набору id промахивался, каждое окно заново читало сотни сделок из AMO,
    и пауза исполнялась минутами.
    """

    from services.live_read_scope import live_read_scope

    calls: list[list[int]] = []

    def _read(requested: list[int]):
        calls.append(list(requested))
        return {lead_id: {"id": lead_id} for lead_id in requested if lead_id != 404}

    monkeypatch.setattr(approval_source_amo, "_read_exact_leads_by_ids", _read)
    with live_read_scope():
        first = approval_source_amo._load_exact_leads_by_ids({1, 2, 404})
        second = approval_source_amo._load_exact_leads_by_ids({2, 3, 404})
        third = approval_source_amo._load_exact_leads_by_ids({1, 3})
    assert calls == [[1, 2, 404], [3]]  # удалённая сделка 404 тоже запомнена и не перечитывается
    assert set(first) == {1, 2} and set(second) == {2, 3} and set(third) == {1, 3}

    # вне live_read_scope кэша нет — каждое чтение идёт в AMO
    calls.clear()
    approval_source_amo._load_exact_leads_by_ids({1})
    approval_source_amo._load_exact_leads_by_ids({1})
    assert calls == [[1], [1]]


def test_exact_lead_cache_keeps_progress_when_job_budget_expires(monkeypatch):
    """Бюджет задания истёк на середине чтения сделок — прочитанные порции остаются следующему заданию прогона.

    Регрессия: окно платежей 30 дней — тысячи сделок, чтение не влезало в бюджет задания,
    кэш заполнялся только после полного чтения, и каждое задание прогона начинало с нуля.
    """

    import pytest
    import requests

    from services.live_read_scope import live_read_scope

    monkeypatch.setattr(approval_source_amo, "_EXACT_LEADS_CHUNK", 2)
    calls: list[list[int]] = []
    budget = {"left": 2}

    def _read(requested: list[int]):
        if budget["left"] == 0:
            raise requests.exceptions.Timeout("AMO_REQUEST_BUDGET_EXPIRED")
        budget["left"] -= 1
        calls.append(list(requested))
        return {lead_id: {"id": lead_id} for lead_id in requested}

    monkeypatch.setattr(approval_source_amo, "_read_exact_leads_by_ids", _read)
    with live_read_scope():
        with pytest.raises(requests.exceptions.Timeout):
            approval_source_amo._load_exact_leads_by_ids({1, 2, 3, 4, 5, 6})
        assert calls == [[1, 2], [3, 4]]
        budget["left"] = 5  # следующее задание прогона: свой бюджет
        leads = approval_source_amo._load_exact_leads_by_ids({1, 2, 3, 4, 5, 6})
    assert calls == [[1, 2], [3, 4], [5, 6]], "первые две порции перечитываться не должны"
    assert set(leads) == {1, 2, 3, 4, 5, 6}
