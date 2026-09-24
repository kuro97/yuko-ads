"""Read-only AMO evidence с атрибуцией только по exact ``fb_ad_id``."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from integrations import amo
from services.approval_checker_models import (
    EvidenceRecord,
    EvidenceRequest,
    EvidenceState,
    FactCategory,
    Metric,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
)
from services.live_read_scope import scoped_read

_PAGE_SIZE = 250
_MAX_PAGES = 1000
_EXACT_FB_AD_FIELD_ID = 902422
# Потолок страниц точечного чтения по ad_id. Тридцатидневное окно всей воронки
# большого аккаунта — это тысячи лидов и десятки страниц, у одного объявления
# столько быть не может. Если AMO проигнорировал фильтр по кастомному полю и
# отдаёт поток воронки, читать его до конца нельзя: упираемся в потолок за 8
# запросов и честно говорим INCOMPLETE вместо многоминутного зависания.
_EXACT_AD_MAX_PAGES = 8
# Сколько ad_id разрешено спрашивать одним exact-чтением. PAUSE спрашивает цель
# плюс активных соседей адсета — это единицы. Тысячи id означают, что кто-то
# пытается протащить через exact-путь отчётный агрегат.
_EXACT_AD_MAX_IDS = 128


class AmoEvidenceError(RuntimeError):
    """AMO не смог доказать полный exact-ID снимок."""


def _exact_fb_ad_id(lead: dict[str, object]) -> str | None:
    fields = lead.get("custom_fields_values")
    if fields is None:
        fields = lead.get("custom_fields")
    if not isinstance(fields, list):
        return None
    by_name: str | None = None
    by_id: str | None = None
    for field in fields:
        if not isinstance(field, dict):
            continue
        values = field.get("values")
        if not isinstance(values, list) or not values or not isinstance(values[0], dict):
            continue
        value = values[0].get("value")
        if value in (None, ""):
            continue
        normalized = str(value).strip()
        if field.get("field_id") == _EXACT_FB_AD_FIELD_ID:
            by_id = normalized
        if (field.get("field_name") or "").strip().lower() == "fb_ad_id":
            by_name = normalized
    if by_id and by_name and by_id != by_name:
        raise AmoEvidenceError("AMO_FB_AD_ID_CONFLICT")
    return by_id or by_name


def _raw_leads_page(params: object) -> list[dict[str, object]]:
    payload = amo._amo_get("leads", params)  # noqa: SLF001 - read-only provider boundary
    if not isinstance(payload, dict):
        raise AmoEvidenceError("AMO_PAYLOAD_INVALID")
    embedded = payload.get("_embedded") or {}
    if not isinstance(embedded, dict):
        raise AmoEvidenceError("AMO_EMBEDDED_INVALID")
    leads = embedded.get("leads") or []
    if not isinstance(leads, list) or any(not isinstance(item, dict) for item in leads):
        raise AmoEvidenceError("AMO_LEADS_INVALID")
    return [dict(item) for item in leads]


def _window_bounds(window: TimeWindow) -> tuple[int, int]:
    return int(window.start.timestamp()), int(window.end.timestamp()) - 1


def _collect_window_pages(
    window: TimeWindow,
    base_params: list[tuple[str, object]],
    *,
    max_pages: int,
    page_limit_code: str,
) -> tuple[dict[str, object], ...]:
    """Читает страницы одного окна и доказывает полноту снимка.

    Общая часть оконного и точечного чтения: у обоих одинаковые инварианты
    (уникальный lead.id, целый created_at внутри окна, пагинация дочитана до
    неполной страницы). Разница только в фильтрах запроса.
    """

    from_ts, to_ts = _window_bounds(window)
    if to_ts < from_ts:
        return ()
    result: list[dict[str, object]] = []
    seen_ids: set[int] = set()
    for page in range(1, max_pages + 1):
        leads = _raw_leads_page([*base_params, ("page", page)])
        for lead in leads:
            lead_id = lead.get("id")
            if isinstance(lead_id, bool) or not isinstance(lead_id, int):
                raise AmoEvidenceError("AMO_LEAD_ID_INVALID")
            if lead_id in seen_ids:
                raise AmoEvidenceError("AMO_LEAD_DUPLICATE")
            seen_ids.add(lead_id)
            created_at = lead.get("created_at")
            if isinstance(created_at, bool) or not isinstance(created_at, int):
                raise AmoEvidenceError("AMO_CREATED_AT_INVALID")
            if not from_ts <= created_at <= to_ts:
                raise AmoEvidenceError("AMO_WINDOW_MISMATCH")
            result.append(lead)
        if len(leads) < _PAGE_SIZE:
            return tuple(result)
    raise AmoEvidenceError(page_limit_code)


def _window_params(window: TimeWindow) -> list[tuple[str, object]]:
    from_ts, to_ts = _window_bounds(window)
    params: list[tuple[str, object]] = [
        ("filter[created_at][from]", from_ts),
        ("filter[created_at][to]", to_ts),
        ("with", "contacts,source_id"),
        ("limit", _PAGE_SIZE),
    ]
    if amo.AMO_PIPELINE_ID:
        params.append(("filter[pipeline_id]", amo.AMO_PIPELINE_ID))
    return params


def _load_window_leads(window: TimeWindow) -> tuple[dict[str, object], ...]:
    """Полный поток лидов воронки за окно. Стоимость — O(воронки).

    Нужен там, где счётчики строятся по ВСЕМ объявлениям окна (отчётные
    ACCOUNT-агрегаты, перечисление ad_id окна). Для исполнения действия по
    одному объявлению это запрещено дорого — там точечный
    ``_load_exact_leads_by_ad_ids``.
    """

    return scoped_read(
        ("amo-window-leads", window, amo.AMO_PIPELINE_ID),
        lambda: _collect_window_pages(
            window,
            _window_params(window),
            max_pages=_MAX_PAGES,
            page_limit_code="AMO_PAGE_LIMIT_EXCEEDED",
        ),
    )


def _load_exact_leads_by_ad_ids(
    window: TimeWindow,
    ad_ids: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    """Лиды окна ТОЛЬКО по exact ``fb_ad_id`` — стоимость O(объявления).

    Точечный запрос идёт через полнотекстовый ``query``: фильтр по значению
    кастомного поля (``filter[custom_fields_values][...]``) аккаунту acme
    недоступен — AMO отвечает на него 400 «Invalid filter for current
    account», и на этом зависало бы исполнение одобренных пауз. ``query`` — один терм на запрос, поэтому по запросу на
    каждый ad_id; результаты сливаются с дедупом по ``lead.id`` (полнотекст
    может заматчить один лид двумя термами).

    Границы окна, воронка и инварианты полноты — те же, что у оконного чтения.
    ``query`` матчит шире, чем поле 902422 (телефоны, имена, другие поля), но
    это даёт только надмножество: отбор «наш ad_id или чужой» дальше делает
    общий агрегатор ``load_amo_evidence`` по exact-значению поля, поэтому
    семантика подсчёта не расходится между путями.
    """

    if not ad_ids:
        return ()
    if len(ad_ids) > _EXACT_AD_MAX_IDS:
        raise AmoEvidenceError("AMO_EXACT_AD_LIMIT_EXCEEDED")
    requested = tuple(sorted(set(ad_ids)))
    return scoped_read(
        ("amo-exact-ad-leads", window, amo.AMO_PIPELINE_ID, requested),
        lambda: _read_exact_leads_by_queries(window, requested),
    )


def _read_exact_leads_by_queries(
    window: TimeWindow,
    requested: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    merged: dict[int, dict[str, object]] = {}
    for ad_id in requested:
        leads = _collect_window_pages(
            window,
            [*_window_params(window), ("query", ad_id)],
            max_pages=_EXACT_AD_MAX_PAGES,
            page_limit_code="AMO_EXACT_PAGE_LIMIT_EXCEEDED",
        )
        for lead in leads:
            merged.setdefault(int(lead["id"]), lead)
    return tuple(merged.values())


_EXACT_LEADS_CHUNK = 50  # столько id уходит в один запрос AMO


def _load_exact_leads_by_ids(lead_ids: Iterable[int]) -> dict[int, dict[str, object]]:
    """Полностью читает exact lead IDs батчами; никакого name/UTM fallback.

    Внутри области ``live_read_scope`` один и тот же набор id читается один раз:
    цепочка «платёж → сделка» у всех заданий прогона одинаковая, потому что окно
    решения считается от общего ``checked_at``.
    """

    requested = sorted(set(lead_ids))
    # Кэш по отдельной сделке, а не по набору: задания одного прогона проверяют разные окна платежей,
    # наборы id у них разные, но пересекаются на ~90%. Кэш по набору промахивался, и каждое окно
    # заново читало сотни сделок (минуты на задание). Вне области live_read_scope
    # scoped_read отдаёт свежий словарь — поведение прежнее. None = AMO ответил и сделку не вернул.
    memo: dict[int, dict[str, object] | None] = scoped_read(("amo-exact-lead-memo",), dict)
    unread = [lead_id for lead_id in requested if lead_id not in memo]
    # Запоминаем порциями по ходу чтения. Платежи ERP идут по всем клиентам, поэтому окно 30 дней —
    # это тысячи сделок и минуты чтения. Если бюджет задания истёк на середине, прочитанное остаётся
    # следующему заданию прогона; раньше оно выбрасывалось, и каждое задание начинало с нуля.
    for offset in range(0, len(unread), _EXACT_LEADS_CHUNK):
        chunk = unread[offset : offset + _EXACT_LEADS_CHUNK]
        fetched = _read_exact_leads_by_ids(chunk)
        for lead_id in chunk:
            memo[lead_id] = fetched.get(lead_id)
    return {lead_id: lead for lead_id in requested if (lead := memo[lead_id]) is not None}


def _read_exact_leads_by_ids(requested: list[int]) -> dict[int, dict[str, object]]:
    result: dict[int, dict[str, object]] = {}
    for offset in range(0, len(requested), 50):
        chunk = requested[offset : offset + 50]
        params: list[tuple[str, object]] = [("with", "contacts"), ("limit", _PAGE_SIZE)]
        params.extend(("filter[id][]", lead_id) for lead_id in chunk)
        for page in range(1, _MAX_PAGES + 1):
            page_params = [*params, ("page", page)]
            leads = _raw_leads_page(page_params)
            for lead in leads:
                lead_id = lead.get("id")
                if isinstance(lead_id, bool) or not isinstance(lead_id, int) or lead_id not in chunk:
                    raise AmoEvidenceError("AMO_EXACT_LEAD_MISMATCH")
                if lead_id in result:
                    raise AmoEvidenceError("AMO_LEAD_DUPLICATE")
                result[lead_id] = lead
            if len(leads) < _PAGE_SIZE:
                break
        else:
            raise AmoEvidenceError("AMO_PAGE_LIMIT_EXCEEDED")
    return result


def load_amo_window_ad_ids(windows: tuple[TimeWindow, ...]) -> tuple[str, ...]:
    """Собирает exact ``fb_ad_id``, встретившиеся в лидах окон.

    Это кандидаты на проверку принадлежности кабинету: только они способны
    повлиять на счётчики leads/quals. Никакого name/UTM fallback — сырые
    значения поля отдаются как есть, отбор «похоже на id» делает вызывающий.
    """

    observed: set[str] = set()
    for window in windows:
        for lead in _load_window_leads(window):
            ad_id = _exact_fb_ad_id(lead)
            if ad_id:
                observed.add(ad_id)
    return tuple(sorted(observed))


def _source_error(
    now: datetime,
    code: str,
    *,
    state: EvidenceState = EvidenceState.ERROR,
) -> SourceEvidence:
    return SourceEvidence(
        source=SourceSystem.AMO,
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
        raise AmoEvidenceError("AMO_SUBJECT_CONFLICT")
    return next(iter(candidates), SubjectRef(SubjectKind.AD, ad_id))


def load_amo_evidence(
    request: EvidenceRequest,
    now: datetime,
    *,
    force_live: bool,
) -> SourceEvidence:
    """Возвращает leads/quals только для exact custom ``fb_ad_id``.

    Два пути чтения, один и тот же агрегатор:

    * ``purpose="ACTION"`` с заявленными ``ad_ids`` — точечный запрос по этим
      ad_id. Исполнение одобренного действия не имеет права вычитывать всю
      воронку за 30 дней (тысячи лидов, минуты чтения) ради счётчиков одного объявления.
    * всё остальное (отчёты, ACCOUNT-агрегаты по всему окну) — оконное чтение:
      там счётчики строятся по всем объявлениям окна, и точечный путь был бы
      дороже, а не дешевле.

    Подсчёт после чтения общий, поэтому на одинаковых данных значения обоих
    путей совпадают: это и есть гарантия, что факт манифеста и факт свежей сводки
    сравниваются как равные.
    """

    del force_live  # AMO adapter не имеет локального business cache.
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать timezone")
    if not request.windows:
        return _source_error(now, "AMO_WINDOW_MISSING")

    requested_ad_ids = set(request.ad_ids)
    exact_path = request.purpose == "ACTION" and bool(requested_ad_ids)
    records: list[EvidenceRecord] = []
    latest_source_ts: int | None = None
    try:
        for window in request.windows:
            leads = (
                _load_exact_leads_by_ad_ids(window, tuple(requested_ad_ids))
                if exact_path
                else _load_window_leads(window)
            )
            per_ad: dict[str, dict[str, object]] = {
                ad_id: {"leads": 0, "quals": 0, "lead_ids": []}
                for ad_id in requested_ad_ids
            }
            aggregate_leads = 0
            aggregate_quals = 0
            aggregate_lead_ids: list[str] = []
            for lead in leads:
                exact_ad_id = _exact_fb_ad_id(lead)
                lead_id = int(lead["id"])
                source_ts = lead.get("updated_at") or lead.get("created_at")
                if isinstance(source_ts, int) and not isinstance(source_ts, bool):
                    latest_source_ts = source_ts if latest_source_ts is None else max(latest_source_ts, source_ts)
                if exact_ad_id not in requested_ad_ids:
                    continue
                normalized_lead = {
                    **lead,
                    "custom_fields": lead.get("custom_fields_values") or [],
                }
                qualified = amo._is_qualified(normalized_lead)  # noqa: SLF001
                aggregate_leads += 1
                aggregate_quals += int(qualified)
                aggregate_lead_ids.append(str(lead_id))

                bucket = per_ad[exact_ad_id]
                bucket["leads"] = int(bucket["leads"]) + 1
                if qualified:
                    bucket["quals"] = int(bucket["quals"]) + 1
                cast_ids = bucket["lead_ids"]
                if isinstance(cast_ids, list):
                    cast_ids.append(str(lead_id))

            for ad_id in sorted(per_ad):
                bucket = per_ad[ad_id]
                subject = _ad_subject(request, ad_id)
                entity_ids = tuple(sorted(bucket["lead_ids"])) if isinstance(bucket["lead_ids"], list) else ()
                records.extend(
                    (
                        EvidenceRecord(
                            category=FactCategory.BUSINESS_METRIC,
                            subject=subject,
                            metric=Metric.LEADS,
                            value=int(bucket["leads"]),
                            source=SourceSystem.AMO,
                            state=EvidenceState.FRESH_COMPLETE,
                            observed_at=now,
                            window=window,
                            currency=None,
                            entity_ids=entity_ids,
                        ),
                        EvidenceRecord(
                            category=FactCategory.BUSINESS_METRIC,
                            subject=subject,
                            metric=Metric.QUALS,
                            value=int(bucket["quals"]),
                            source=SourceSystem.AMO,
                            state=EvidenceState.FRESH_COMPLETE,
                            observed_at=now,
                            window=window,
                            currency=None,
                            entity_ids=entity_ids,
                        ),
                    )
                )

            aggregate_values = {
                Metric.LEADS: aggregate_leads,
                Metric.QUALS: aggregate_quals,
            }
            emitted: set[tuple[object, ...]] = set()
            for claim in request.claims:
                if (
                    not requested_ad_ids
                    or claim.source is not SourceSystem.AMO
                    or claim.subject.kind is not SubjectKind.ACCOUNT
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
                        source=SourceSystem.AMO,
                        state=EvidenceState.FRESH_COMPLETE,
                        observed_at=now,
                        window=window,
                        currency=claim.currency,
                        entity_ids=tuple(sorted(aggregate_lead_ids)),
                    )
                )
    except Exception as exc:
        # Голое имя класса теряет причину: истёкший бюджет задания приходит как
        # requests Timeout("AMO_REQUEST_BUDGET_EXPIRED"), и в журнал ложилось
        # просто «Timeout». Такой журнал читался как «AMO лежит», хотя AMO
        # отвечал быстро, а бюджет задания съедали чтения Facebook. Сообщение исключения дописываем к имени класса — секретов в
        # нём нет: токен AMO живёт в заголовке, а не в URL.
        detail = "" if isinstance(exc, AmoEvidenceError) else str(exc).strip()
        code = (
            str(exc)
            if isinstance(exc, AmoEvidenceError)
            else (
                f"{type(exc).__name__}:{detail}" if detail else type(exc).__name__
            )
        )
        state = EvidenceState.INCOMPLETE if isinstance(exc, AmoEvidenceError) else EvidenceState.ERROR
        return _source_error(now, code[:120], state=state)

    data_as_of = (
        datetime.fromtimestamp(latest_source_ts, tz=timezone.utc)
        if latest_source_ts is not None
        else now
    )
    return SourceEvidence(
        source=SourceSystem.AMO,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=now,
        data_as_of=data_as_of,
        from_cache=False,
        complete=True,
        records=tuple(records),
    )
