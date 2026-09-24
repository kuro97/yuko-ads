"""Strict read-only CDP/ERP evidence и exact payment→AMO→fb_ad_id chain."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from services import cdp_client
from services.approval_checker_models import (
    EvidenceRecord,
    EvidenceRequest,
    EvidenceState,
    FactCategory,
    Metric,
    PaymentEvidence,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
)
from services.approval_source_amo import _exact_fb_ad_id, _load_exact_leads_by_ids
from services.live_read_scope import scoped_read

logger = logging.getLogger(__name__)

# Допуск на платежи, чьи сделки удалены в AMO. Успешное exact-чтение по списку
# id — само по себе доказательство: сетевая дыра или неполная страница дают
# исключение, а «AMO ответил и этих id не вернул» означает, что сделок больше
# нет (пример: платёж из ERP ссылается на сделку, удалённую руками, и один
# этот id намертво бракует любое длинное окно). Но массовая
# пропажа — это уже не удалённые сделки, а дрейф самого источника (например,
# сломался парсинг contract_number), поэтому выше порога окно бракуется.
_CHAIN_DELETED_ABS_MAX = 10
_CHAIN_DELETED_SHARE_MAX = Decimal("0.01")


def _chain_leads_tolerating_deleted(
    parsed: list[tuple],
    lead_ids: set[int],
) -> tuple[dict[int, dict[str, object]], list[tuple]]:
    """Читает сделки цепочки, отбрасывая платежи подтверждённо удалённых.

    Возвращает (leads, parsed без платежей удалённых сделок). Пропажа сверх
    порога — по-прежнему CDP_AMO_CHAIN_INCOMPLETE: неполный universe занизил
    бы выручку, и это ловушка, ради которой проверка существует.
    """
    leads = _load_exact_leads_by_ids(lead_ids)
    missing = lead_ids - set(leads)
    if not missing:
        return leads, parsed
    allowed = max(
        _CHAIN_DELETED_ABS_MAX,
        int(len(lead_ids) * _CHAIN_DELETED_SHARE_MAX),
    )
    if len(missing) > allowed:
        raise CdpEvidenceError("CDP_AMO_CHAIN_INCOMPLETE")
    logger.warning(
        "approval_source_cdp: платежи ссылаются на удалённые в AMO сделки %s — "
        "исключены из universe как неатрибутируемые",
        sorted(missing),
    )
    return leads, [item for item in parsed if item[1] not in missing]


class CdpEvidenceError(RuntimeError):
    """CDP/AMO chain не смог доказать полный снимок."""


def _payments_strict(date_from: date, date_to: date, *, force_live: bool):
    """Строгое чтение платежей окна, один раз на область живых чтений.

    Точечного среза по объявлению у ``/revenue`` нет: выручку объявления
    доказывает цепочка «платёж окна → сделка AMO → exact ``fb_ad_id``», и рвать
    её нельзя — незакрытая цепочка занизила бы выручку. Зато само окно платежей
    у всех заданий одного прогона одно и то же (окно решения считается от общего
    ``checked_at``), поэтому внутри ``live_read_scope`` оно читается однажды.
    Метаданные чтения (``fetched_at``/``from_cache``/``complete``) отдаются как
    есть, кеш их не подменяет: переиспользуется ровно тот объект, что вернуло
    живое чтение.
    """

    return scoped_read(
        ("cdp-payments-strict", date_from, date_to, force_live),
        lambda: cdp_client.get_payments_strict(
            date_from,
            date_to,
            force_live=force_live,
        ),
    )


def _window_dates(window: TimeWindow) -> tuple[date, date]:
    first = window.start.date()
    last = (window.end - timedelta(microseconds=1)).date()
    if last < first:
        raise CdpEvidenceError("CDP_WINDOW_INVALID")
    return first, last


def _is_unattributed(payment: dict[str, object]) -> bool:
    """Платёж без номера договора — ничей: он не относится ни к какому объявлению.

    В ERP такие встречаются регулярно (замер показал, что это меньшинство
    приходов, но они бывают почти каждый день). Это не брак выгрузки,
    а платежи вне сделок AMO. Ронять из-за них проверку всего окна нельзя:
    именно так проверка денежных фактов и не могла подтвердить ни одного
    отчёта — сначала через CDP_PAGINATION_INCOMPLETE, потом через
    CDP_CONTRACT_INVALID.

    Пропускаем ТОЛЬКО пустой договор. Непустой, но неразбираемый (например с
    буквенным префиксом) по-прежнему валит проверку: это признак смены формата,
    и молча его проглотить значит занизить выручку объявления.
    """
    contract = payment.get("contract_number")
    return contract is None or (isinstance(contract, str) and not contract.strip())


def _payment_parts(payment: dict[str, object]) -> tuple[str, int, Decimal, str, date]:
    payment_id = payment.get("id")
    if isinstance(payment_id, bool) or payment_id in (None, ""):
        raise CdpEvidenceError("CDP_PAYMENT_ID_MISSING")
    contract = payment.get("contract_number")
    try:
        lead_id = int(contract)
    except (TypeError, ValueError) as exc:
        raise CdpEvidenceError("CDP_CONTRACT_INVALID") from exc
    if lead_id <= 0:
        raise CdpEvidenceError("CDP_CONTRACT_INVALID")
    try:
        amount = Decimal(str(payment.get("amount")))
    except (InvalidOperation, ValueError) as exc:
        raise CdpEvidenceError("CDP_AMOUNT_INVALID") from exc
    if not amount.is_finite() or amount < 0:
        raise CdpEvidenceError("CDP_AMOUNT_INVALID")
    direction = payment.get("direction")
    if direction not in {"income", "refund"}:
        raise CdpEvidenceError("CDP_DIRECTION_INVALID")
    try:
        document_date = date.fromisoformat(str(payment.get("doc_date")))
    except ValueError as exc:
        raise CdpEvidenceError("CDP_DOCUMENT_DATE_INVALID") from exc
    return str(payment_id), lead_id, amount, str(direction).upper(), document_date


def _error(now: datetime, code: str, *, state: EvidenceState = EvidenceState.ERROR) -> SourceEvidence:
    return SourceEvidence(
        source=SourceSystem.CDP_ERP,
        state=state,
        fetched_at=now,
        data_as_of=None,
        from_cache=False,
        complete=False,
        records=(),
        error_code=code,
    )


def _ad_subject(request: EvidenceRequest, ad_id: str) -> SubjectRef:
    candidates = {
        subject
        for subject in (
            *request.subjects,
            *(claim.subject for claim in request.claims),
        )
        if subject.kind is SubjectKind.AD and subject.subject_id == ad_id
    }
    if len(candidates) > 1:
        raise CdpEvidenceError("CDP_SUBJECT_CONFLICT")
    return next(iter(candidates), SubjectRef(SubjectKind.AD, ad_id))


def _daily_online_records(
    request: EvidenceRequest,
    window: TimeWindow,
    now: datetime,
    *,
    force_live: bool,
) -> tuple[tuple[EvidenceRecord, ...], datetime | None, datetime, bool]:
    """Отдаёт независимо вычисленные Online-факты и явный режим DRR."""

    relevant = tuple(
        claim
        for claim in request.claims
        if claim.source is SourceSystem.CDP_ERP
        and claim.window == window
        and claim.subject.kind is SubjectKind.ACCOUNT
        and claim.subject.subject_id == "ONLINE"
        and claim.metric
        in {
            Metric.DISPLAY_CONTEXT,
            Metric.SPEND,
            Metric.LEADS,
            Metric.QUALS,
            Metric.QUAL_PCT,
            Metric.REVENUE,
            Metric.DRR_PCT,
            Metric.MATCH_STATE,
        }
    )
    if not relevant:
        return (), None, now, False
    date_from, date_to = _window_dates(window)
    response = cdp_client._request_with_meta(  # noqa: SLF001 - checker-only strict API
        "/analytics/daily-report",
        {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
        force_live=force_live,
    )
    if not isinstance(response.payload, dict):
        raise CdpEvidenceError("CDP_DAILY_PAYLOAD_INVALID")
    items = response.payload.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise CdpEvidenceError("CDP_DAILY_ITEMS_INVALID")

    online_rows: list[dict[str, object]] = []
    seen_dates: set[date] = set()
    for item in items:
        if item.get("city") != "Онлайн":
            continue
        try:
            row_date = date.fromisoformat(str(item.get("report_date")))
        except ValueError as exc:
            raise CdpEvidenceError("CDP_DAILY_DATE_INVALID") from exc
        if not date_from <= row_date <= date_to:
            raise CdpEvidenceError("CDP_DAILY_WINDOW_MISMATCH")
        if row_date in seen_dates:
            raise CdpEvidenceError("CDP_DAILY_DUPLICATE")
        seen_dates.add(row_date)
        online_rows.append(item)
    if not online_rows:
        return (), response.data_as_of, response.fetched_at, response.from_cache

    def decimal_field(
        row: dict[str, object],
        field: str,
        *,
        positive: bool = False,
    ) -> Decimal:
        try:
            value = Decimal(str(row.get(field)))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise CdpEvidenceError("CDP_DAILY_NUMBER_INVALID") from exc
        if not value.is_finite() or value < 0 or (positive and value <= 0):
            raise CdpEvidenceError("CDP_DAILY_NUMBER_INVALID")
        return value

    def count_field(row: dict[str, object], field: str) -> int:
        value = decimal_field(row, field)
        if value != value.to_integral_value():
            raise CdpEvidenceError("CDP_DAILY_COUNT_INVALID")
        return int(value)

    metrics = {claim.metric for claim in relevant}
    needs_money = bool(
        metrics
        & {Metric.SPEND, Metric.REVENUE, Metric.DRR_PCT, Metric.MATCH_STATE}
    )
    spend = Decimal("0")
    spend_lcy: Decimal | None = None
    revenue: Decimal | None = None
    drr: Decimal | None = None
    mode: str | None = None
    if needs_money:
        spend_rows = [decimal_field(row, "ad_spend") for row in online_rows]
        spend = sum(spend_rows, Decimal("0"))
        exact_revenue = all(
            row.get("revenue_new") is not None and row.get("usd_rate") is not None
            for row in online_rows
        )
        if exact_revenue:
            spend_lcy = Decimal("0")
            revenue = Decimal("0")
            for row, row_spend in zip(online_rows, spend_rows, strict=True):
                row_revenue = decimal_field(row, "revenue_new")
                usd_rate = decimal_field(row, "usd_rate", positive=True)
                revenue += row_revenue
                spend_lcy += row_spend * usd_rate
            mode = "EXACT_REVENUE"
            if revenue > 0:
                drr = spend_lcy / revenue * Decimal("100")
        elif all(row.get("drr_new") is not None for row in online_rows):
            row_drrs = [decimal_field(row, "drr_new") for row in online_rows]
            mode = "WEIGHTED_FALLBACK"
            if spend > 0:
                weighted_num = sum(
                    (
                        row_spend * row_drr
                        for row_spend, row_drr in zip(
                            spend_rows,
                            row_drrs,
                            strict=True,
                        )
                    ),
                    Decimal("0"),
                )
                drr = weighted_num / spend
            elif len(row_drrs) == 1:
                # За один день CDP уже отдал DRR как source fact; валюты
                # здесь не перемножаются и не сравниваются между собой.
                drr = row_drrs[0]

    leads: int | None = None
    quals: int | None = None
    qual_pct: Decimal | None = None
    if metrics & {Metric.LEADS, Metric.QUALS, Metric.QUAL_PCT}:
        leads = sum(count_field(row, "leads") for row in online_rows)
        quals = sum(count_field(row, "qleads") for row in online_rows)
        if len(online_rows) == 1 and online_rows[0].get("pct_qleads") is not None:
            qual_pct = decimal_field(online_rows[0], "pct_qleads")
        elif leads > 0:
            qual_pct = Decimal(quals) / Decimal(leads) * Decimal("100")

    expected_dates = {
        date_from + timedelta(days=offset)
        for offset in range((date_to - date_from).days + 1)
    }
    coverage_complete = seen_dates == expected_dates
    context = None
    if coverage_complete:
        context = (
            date_from.isoformat()
            if date_from == date_to
            else f"{date_from.isoformat()}..{date_to.isoformat()}"
        )
    base_entity_ids = tuple(sorted(day.isoformat() for day in seen_dates)) + (
        (
            f"coverage:complete:{date_from.isoformat()}..{date_to.isoformat()}"
            if coverage_complete
            else f"coverage:partial:{len(seen_dates)}/{len(expected_dates)}"
        ),
        f"rows:{len(online_rows)}",
    )
    entity_ids = base_entity_ids + ((f"mode:{mode}",) if mode is not None else ())
    values: dict[tuple[Metric, str | None], object | None] = {
        (Metric.DISPLAY_CONTEXT, None): context,
        (Metric.SPEND, "USD"): spend if needs_money else None,
        (Metric.SPEND, "LCY"): spend_lcy,
        (Metric.LEADS, None): leads,
        (Metric.QUALS, None): quals,
        (Metric.QUAL_PCT, None): qual_pct,
        (Metric.REVENUE, "LCY"): revenue,
        (Metric.DRR_PCT, None): drr,
        (Metric.MATCH_STATE, None): mode,
    }
    records: list[EvidenceRecord] = []
    emitted: set[tuple[object, ...]] = set()
    for claim in relevant:
        value = values.get((claim.metric, claim.currency))
        if value is None:
            continue
        identity = (
            claim.category,
            claim.subject,
            claim.metric,
            claim.window,
            claim.currency,
        )
        if identity in emitted:
            continue
        emitted.add(identity)
        records.append(
            EvidenceRecord(
                category=claim.category,
                subject=claim.subject,
                metric=claim.metric,
                value=value,
                source=SourceSystem.CDP_ERP,
                state=EvidenceState.FRESH_COMPLETE,
                observed_at=now,
                window=window,
                currency=claim.currency,
                entity_ids=entity_ids,
            )
        )

    # Same-currency inputs остаются в evidence даже когда текущий producer
    # пока показывает расход в USD: это доказательство безопасной формулы DRR.
    lcy_identity = (
        FactCategory.BUSINESS_METRIC,
        SubjectRef(SubjectKind.ACCOUNT, "ONLINE"),
        Metric.SPEND,
        window,
        "LCY",
    )
    if spend_lcy is not None and lcy_identity not in emitted:
        records.append(
            EvidenceRecord(
                category=FactCategory.BUSINESS_METRIC,
                subject=SubjectRef(SubjectKind.ACCOUNT, "ONLINE"),
                metric=Metric.SPEND,
                value=spend_lcy,
                source=SourceSystem.CDP_ERP,
                state=EvidenceState.FRESH_COMPLETE,
                observed_at=now,
                window=window,
                currency="LCY",
                entity_ids=entity_ids,
            )
        )
    return tuple(records), response.data_as_of, response.fetched_at, response.from_cache


def load_cdp_window_ad_ids(
    windows: tuple[TimeWindow, ...],
    *,
    force_live: bool,
) -> tuple[str, ...]:
    """Собирает exact ``fb_ad_id`` по цепочке платёж → AMO-сделка за окна.

    Платежи окна могут относиться к сделкам, созданным раньше окна, поэтому
    их ad_id нельзя вывести из лидов окна — их приходится читать отдельно.
    Незакрытая цепочка (нет сделки или в ней нет exact id) — отказ, а не
    молчаливый пропуск: неполный universe занизил бы выручку.
    """

    observed: set[str] = set()
    for window in windows:
        date_from, date_to = _window_dates(window)
        strict = _payments_strict(date_from, date_to, force_live=force_live)
        if not strict.complete:
            raise CdpEvidenceError("CDP_PAGINATION_INCOMPLETE")
        # Ничьи платежи (без договора) в universe объявлений не участвуют.
        parsed = [
            _payment_parts(item)
            for item in strict.items
            if not _is_unattributed(item)
        ]
        lead_ids = {item[1] for item in parsed}
        leads, parsed = _chain_leads_tolerating_deleted(parsed, lead_ids)
        # Сделка без fb_ad_id — это органика, повторная продажа или офлайн:
        # её выручка легитимно ничья и в universe объявлений не входит.
        # Брак остаётся для случая «метки нет ВООБЩЕ ни у одной сделки окна» —
        # так выглядит не органика, а пропажа самого поля (сменили field_id).
        tagged = [
            ad_id for lead in leads.values() if (ad_id := _exact_fb_ad_id(lead))
        ]
        if leads and not tagged:
            raise CdpEvidenceError("CDP_AMO_EXACT_AD_MISSING")
        observed.update(tagged)
    return tuple(sorted(observed))


def load_cdp_evidence(
    request: EvidenceRequest,
    now: datetime,
    *,
    force_live: bool,
) -> SourceEvidence:
    """Строит payment entities только после exact AMO lead→ad проверки."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать timezone")
    if not request.windows:
        return _error(now, "CDP_WINDOW_MISSING")

    requested_ad_ids = set(request.ad_ids)
    records: list[EvidenceRecord] = []
    entities: list[PaymentEvidence] = []
    fetched_at: datetime | None = None
    data_as_of: datetime | None = None
    from_cache = False
    try:
        for window in request.windows:
            need_payments = bool(requested_ad_ids)
            if need_payments:
                date_from, date_to = _window_dates(window)
                strict = _payments_strict(date_from, date_to, force_live=force_live)
                fetched_at = strict.fetched_at if fetched_at is None else max(fetched_at, strict.fetched_at)
                from_cache = from_cache or strict.from_cache
                if strict.data_as_of is not None:
                    data_as_of = strict.data_as_of if data_as_of is None else max(data_as_of, strict.data_as_of)
                if not strict.complete:
                    return _error(now, "CDP_PAGINATION_INCOMPLETE", state=EvidenceState.INCOMPLETE)

                # Ничьи платежи (без договора) не относятся ни к какому
                # объявлению — в доказательство выручки не идут.
                parsed = [
                    _payment_parts(item)
                    for item in strict.items
                    if not _is_unattributed(item)
                ]
                lead_ids = {item[1] for item in parsed}
                leads, parsed = _chain_leads_tolerating_deleted(parsed, lead_ids)

                # Сделки без fb_ad_id (органика, повторные, офлайн) — ничьи:
                # их платежи к объявлениям не привязываются. Брак только когда
                # метки нет ни у одной сделки окна — так выглядит пропажа поля.
                ad_by_lead: dict[int, str] = {}
                for lead_id, lead in leads.items():
                    ad_id = _exact_fb_ad_id(lead)
                    if not ad_id:
                        continue
                    ad_by_lead[lead_id] = ad_id
                if leads and not ad_by_lead:
                    raise CdpEvidenceError("CDP_AMO_EXACT_AD_MISSING")
                parsed = [item for item in parsed if item[1] in ad_by_lead]

                net_by_contract: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
                for _, lead_id, amount, direction, _ in parsed:
                    net_by_contract[lead_id] += amount if direction == "INCOME" else -amount

                payments_by_ad: dict[str, set[int]] = {ad_id: set() for ad_id in requested_ad_ids}
                revenue_by_ad: dict[str, Decimal] = {
                    ad_id: Decimal("0") for ad_id in requested_ad_ids
                }
                entity_ids_by_ad: dict[str, list[str]] = {ad_id: [] for ad_id in requested_ad_ids}
                for lead_id, net in net_by_contract.items():
                    ad_id = ad_by_lead[lead_id]
                    if ad_id not in requested_ad_ids:
                        continue
                    revenue_by_ad[ad_id] += net
                    if net > 0:
                        payments_by_ad[ad_id].add(lead_id)

                for payment_id, lead_id, amount, direction, document_date in parsed:
                    ad_id = ad_by_lead[lead_id]
                    net = net_by_contract[lead_id]
                    if ad_id not in requested_ad_ids or net <= 0:
                        continue
                    document_at = datetime.combine(document_date, time.min, tzinfo=window.start.tzinfo)
                    entities.append(
                        PaymentEvidence(
                            payment_id=payment_id,
                            contract_number=str(lead_id),
                            amo_lead_id=lead_id,
                            fb_ad_id=ad_id,
                            amount_lcy=amount,
                            direction=direction,
                            contract_net_lcy=net,
                            document_at=document_at,
                            fetched_at=strict.fetched_at,
                        )
                    )
                    entity_ids_by_ad[ad_id].append(payment_id)

                for ad_id in sorted(requested_ad_ids):
                    subject = _ad_subject(request, ad_id)
                    entity_ids = tuple(sorted(entity_ids_by_ad[ad_id]))
                    records.extend(
                        (
                            EvidenceRecord(
                                category=FactCategory.BUSINESS_METRIC,
                                subject=subject,
                                metric=Metric.PAYMENTS,
                                value=len(payments_by_ad[ad_id]),
                                source=SourceSystem.CDP_ERP,
                                state=EvidenceState.FRESH_COMPLETE,
                                observed_at=now,
                                window=window,
                                currency=None,
                                entity_ids=entity_ids,
                            ),
                            EvidenceRecord(
                                category=FactCategory.BUSINESS_METRIC,
                                subject=subject,
                                metric=Metric.REVENUE,
                                value=revenue_by_ad[ad_id],
                                source=SourceSystem.CDP_ERP,
                                state=EvidenceState.FRESH_COMPLETE,
                                observed_at=now,
                                window=window,
                                currency="LCY",
                                entity_ids=entity_ids,
                            ),
                        )
                    )

                aggregate_contracts = set().union(*payments_by_ad.values())
                aggregate_revenue = sum(revenue_by_ad.values(), Decimal("0"))
                aggregate_entity_ids = tuple(
                    sorted(
                        payment_id
                        for payment_ids in entity_ids_by_ad.values()
                        for payment_id in payment_ids
                    )
                )
                aggregate_values = {
                    Metric.PAYMENTS: len(aggregate_contracts),
                    Metric.REVENUE: aggregate_revenue,
                }
                emitted: set[tuple[object, ...]] = set()
                for claim in request.claims:
                    if (
                        claim.source is not SourceSystem.CDP_ERP
                        or claim.subject.kind is not SubjectKind.ACCOUNT
                        or claim.subject.subject_id == "ONLINE"
                        or claim.window != window
                        or claim.metric not in aggregate_values
                    ):
                        continue
                    identity = (
                        claim.category,
                        claim.subject,
                        claim.metric,
                        claim.window,
                        claim.currency,
                    )
                    if identity in emitted:
                        continue
                    emitted.add(identity)
                    records.append(
                        EvidenceRecord(
                            category=claim.category,
                            subject=claim.subject,
                            metric=claim.metric,
                            value=aggregate_values[claim.metric],
                            source=SourceSystem.CDP_ERP,
                            state=EvidenceState.FRESH_COMPLETE,
                            observed_at=now,
                            window=window,
                            currency=claim.currency,
                            entity_ids=aggregate_entity_ids,
                        )
                    )
            daily_records, daily_as_of, daily_fetched_at, daily_from_cache = _daily_online_records(
                request, window, now, force_live=force_live
            )
            records.extend(daily_records)
            if daily_records:
                fetched_at = daily_fetched_at if fetched_at is None else max(fetched_at, daily_fetched_at)
                from_cache = from_cache or daily_from_cache
                if daily_as_of is not None:
                    data_as_of = daily_as_of if data_as_of is None else max(data_as_of, daily_as_of)
    except Exception as exc:
        code = str(exc) if isinstance(exc, CdpEvidenceError) else type(exc).__name__
        state = EvidenceState.INCOMPLETE if isinstance(exc, CdpEvidenceError) else EvidenceState.ERROR
        return _error(now, code[:120], state=state)

    # Живое чтение не может быть «из будущего» относительно опорного now.
    # Пять источников читаются последовательно, а now у наблюдения один, поэтому
    # HTTP-фетч CDP физически приходит на секунды позже. Свежесть считается как
    # (now - fetched_at) и снизу ограничена -1с: настоящее время фетча делало
    # CDP «устаревшим» уже при отставании 2с и роняло действие в SOURCE_STALE.
    # Остальные четыре адаптера рапортуют ровно now; здесь то же самое, но
    # кешированное чтение (fetched_at < now) остаётся честно старым — его
    # ACTION и так запрещает через from_cache.
    live_fetched_at = min(fetched_at, now) if fetched_at is not None else now
    return SourceEvidence(
        source=SourceSystem.CDP_ERP,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=live_fetched_at,
        data_as_of=data_as_of,
        from_cache=from_cache,
        complete=True,
        records=tuple(records),
        payments=tuple(entities),
    )
