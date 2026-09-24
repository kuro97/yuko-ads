"""Force-live Facebook evidence без mutation API."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent import fb_common
from services.adset_pause_guard import inventory_state_sha256
from services.approval_checker_models import (
    ActionManifest,
    ActionObservation,
    AssetRecoveryManifest,
    EvidenceRecord,
    EvidenceRequest,
    EvidenceState,
    FactCategory,
    LaunchManifest,
    Metric,
    PauseManifest,
    ScaleManifest,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
    UnpauseManifest,
    canonical_json,
)
from services.fb_token_provider import get_fb_account_id, get_fb_token

_OBJECT_CHUNK_SIZE = 50
_MAX_PAGES = 1000
_MAX_ADS_PER_ADSET = 50
# Коды Graph, означающие «объекта нет / нет доступа», а не сбой чтения.
_MISSING_OBJECT_ERROR_CODES = frozenset({100, 803})


class FacebookEvidenceError(RuntimeError):
    """Graph не доказал полный точный снимок."""


def _normalized_account_id(raw: object) -> str:
    value = str(raw or "").removeprefix("act_").strip()
    if not value:
        raise FacebookEvidenceError("FB_ACCOUNT_ID_INVALID")
    return value


def _graph_response(path: str, params: dict[str, object]):
    query = dict(params)
    query["access_token"] = get_fb_token()
    return fb_common._throttled_get(f"{fb_common.API}/{path.lstrip('/')}", params=query)  # noqa: SLF001


def _graph_payload(response) -> dict[str, object]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise FacebookEvidenceError("FB_JSON_INVALID") from exc
    if not isinstance(payload, dict):
        raise FacebookEvidenceError("FB_PAYLOAD_INVALID")
    return payload


def _graph_json(path: str, params: dict[str, object]) -> dict[str, object]:
    response = _graph_response(path, params)
    if response.status_code != 200:
        raise FacebookEvidenceError(f"FB_HTTP_{response.status_code}")
    payload = _graph_payload(response)
    if payload.get("error"):
        raise FacebookEvidenceError("FB_GRAPH_ERROR")
    return payload


def load_ad_account_timezone(ad_id: str) -> str:
    """Таймзона кабинета, которому принадлежит объявление.

    Нужна манифесту ДО сборки evidence: окно решения обязано лежать на
    полуночах кабинета, иначе _load_insights честно не отдаст точный расход.
    Внутри live_read_scope один ad_id читается один раз на прогон.
    """
    from services.live_read_scope import scoped_read

    def _read() -> str:
        ad_payload = _graph_json(f"/{ad_id}", {"fields": "account_id"})
        account_id = _normalized_account_id(ad_payload.get("account_id"))
        info = _graph_json(f"/act_{account_id}", {"fields": "timezone_name"})
        timezone_name = info.get("timezone_name")
        if not isinstance(timezone_name, str) or not timezone_name:
            raise FacebookEvidenceError("FB_TIMEZONE_INVALID")
        return timezone_name

    return scoped_read(("fb-ad-account-timezone", ad_id), _read)


def _paginate(path: str, params: dict[str, object]) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    after: str | None = None
    for _page in range(_MAX_PAGES):
        query = dict(params)
        if after is not None:
            query["after"] = after
        payload = _graph_json(path, query)
        page_rows = payload.get("data") or []
        if not isinstance(page_rows, list) or any(not isinstance(row, dict) for row in page_rows):
            raise FacebookEvidenceError("FB_PAGE_ROWS_INVALID")
        for row in page_rows:
            row_id = row.get("id") or row.get("ad_id")
            if row_id is None:
                # Aggregated account rows may legitimately have no object ID.
                row_key = hashlib.sha256(canonical_json(row)).hexdigest()
            else:
                row_key = str(row_id)
            if row_key in seen_ids:
                raise FacebookEvidenceError("FB_DUPLICATE_ROW")
            seen_ids.add(row_key)
            rows.append(dict(row))
        paging = payload.get("paging")
        if paging is None:
            return tuple(rows)
        if not isinstance(paging, dict):
            raise FacebookEvidenceError("FB_PAGING_INVALID")
        if not paging.get("next"):
            return tuple(rows)
        cursors = paging.get("cursors")
        next_after = cursors.get("after") if isinstance(cursors, dict) else None
        if not isinstance(next_after, str) or not next_after or next_after in seen_cursors:
            raise FacebookEvidenceError("FB_PAGING_CURSOR_INVALID")
        seen_cursors.add(next_after)
        after = next_after
    raise FacebookEvidenceError("FB_PAGE_LIMIT_EXCEEDED")


def _load_exact_ads(ad_ids: tuple[str, ...]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for offset in range(0, len(ad_ids), _OBJECT_CHUNK_SIZE):
        chunk = ad_ids[offset : offset + _OBJECT_CHUNK_SIZE]
        payload = _graph_json(
            "/",
            {
                "ids": ",".join(chunk),
                "fields": (
                    "id,name,status,effective_status,created_time,adset_id,account_id,"
                    "creative{id,object_story_spec,asset_feed_spec}"
                ),
            },
        )
        for ad_id in chunk:
            row = payload.get(ad_id)
            if not isinstance(row, dict) or str(row.get("id") or ad_id) != ad_id:
                raise FacebookEvidenceError("FB_EXACT_AD_MISSING")
            if ad_id in result:
                raise FacebookEvidenceError("FB_DUPLICATE_AD")
            result[ad_id] = dict(row)
    return result


def _lead_count(row: dict[str, object]) -> int:
    actions = row.get("actions") or []
    if not isinstance(actions, list):
        raise FacebookEvidenceError("FB_ACTIONS_INVALID")
    total = Decimal("0")
    for action in actions:
        if not isinstance(action, dict):
            raise FacebookEvidenceError("FB_ACTION_INVALID")
        if action.get("action_type") not in {"lead", "on_facebook_lead"}:
            continue
        try:
            value = Decimal(str(action.get("value")))
        except (InvalidOperation, ValueError) as exc:
            raise FacebookEvidenceError("FB_LEAD_VALUE_INVALID") from exc
        if not value.is_finite() or value < 0 or value != value.to_integral_value():
            raise FacebookEvidenceError("FB_LEAD_VALUE_INVALID")
        total += value
    return int(total)


def _load_insights(
    account_id: str,
    ad_ids: tuple[str, ...],
    request: EvidenceRequest,
    *,
    timezone_name: str,
) -> tuple[
    dict[tuple[str, TimeWindow], dict[str, object]],
    frozenset[TimeWindow],
]:
    """Читает только окна, которые Graph способен доказать без округления."""

    result: dict[tuple[str, TimeWindow], dict[str, object]] = {}
    exact_windows: set[TimeWindow] = set()
    try:
        account_tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise FacebookEvidenceError("FB_TIMEZONE_INVALID") from exc
    for window in request.windows:
        local_start = window.start.astimezone(account_tz)
        local_end = window.end.astimezone(account_tz)
        if (
            local_start.time() != datetime.min.time()
            or local_end.time() != datetime.min.time()
            or local_end <= local_start
        ):
            # Insights time_range имеет дневную гранулярность. Частичное окно
            # нельзя округлять и затем выдавать за точное доказательство.
            continue
        for offset in range(0, len(ad_ids), _OBJECT_CHUNK_SIZE):
            chunk = ad_ids[offset : offset + _OBJECT_CHUNK_SIZE]
            rows = _paginate(
                f"act_{account_id}/insights",
                {
                    "level": "ad",
                    "fields": "ad_id,spend,actions",
                    "time_range": json.dumps(
                        {
                            "since": local_start.date().isoformat(),
                            "until": (
                                local_end - timedelta(microseconds=1)
                            ).date().isoformat(),
                        },
                        separators=(",", ":"),
                    ),
                    "filtering": json.dumps(
                        [{"field": "ad.id", "operator": "IN", "value": list(chunk)}],
                        separators=(",", ":"),
                    ),
                    "limit": 500,
                },
            )
            for row in rows:
                ad_id = str(row.get("ad_id") or "")
                if ad_id not in chunk:
                    raise FacebookEvidenceError("FB_INSIGHT_SUBJECT_MISMATCH")
                key = (ad_id, window)
                if key in result:
                    raise FacebookEvidenceError("FB_DUPLICATE_INSIGHT")
                result[key] = row
        if ad_ids:
            exact_windows.add(window)
    return result, frozenset(exact_windows)


def _load_account_insights(
    account_id: str,
    request: EvidenceRequest,
    *,
    timezone_name: str,
) -> dict[TimeWindow, dict[str, object]]:
    """Агрегат кабинета — один запрос level=account на каждое точное окно.

    Сумму по объявлениям считает сам Graph: выкачивать инвентарь кабинета
    (у большого кабинета — тысячи объявлений → десятки страниц и сотни чанков
    insights на окно) для ACCOUNT-претензий не нужно. Окно без агрегатной строки не
    доказано: пустой data[] — «Graph ещё не посчитал», а не «расход 0»
    (ровно так его трактует и продюсер отчёта —
    morning_digest._get_fb_spend_for_day).
    """

    result: dict[TimeWindow, dict[str, object]] = {}
    try:
        account_tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise FacebookEvidenceError("FB_TIMEZONE_INVALID") from exc
    for window in request.windows:
        local_start = window.start.astimezone(account_tz)
        local_end = window.end.astimezone(account_tz)
        if (
            local_start.time() != datetime.min.time()
            or local_end.time() != datetime.min.time()
            or local_end <= local_start
        ):
            # Insights time_range имеет дневную гранулярность. Частичное окно
            # нельзя округлять и затем выдавать за точное доказательство.
            continue
        rows = _paginate(
            f"act_{account_id}/insights",
            {
                "level": "account",
                "fields": "account_id,spend,actions",
                "time_range": json.dumps(
                    {
                        "since": local_start.date().isoformat(),
                        "until": (
                            local_end - timedelta(microseconds=1)
                        ).date().isoformat(),
                    },
                    separators=(",", ":"),
                ),
                "limit": 500,
            },
        )
        if not rows:
            # Нет доказательства — нет записи: окно молча пропускается,
            # claim получит EVIDENCE_MISSING вместо ложного нуля.
            continue
        if len(rows) > 1:
            # level=account без time_increment обязан вернуть одну строку.
            raise FacebookEvidenceError("FB_ACCOUNT_INSIGHT_AMBIGUOUS")
        row = rows[0]
        row_account = str(row.get("account_id") or "")
        if not row_account or _normalized_account_id(row_account) != account_id:
            raise FacebookEvidenceError("FB_INSIGHT_SUBJECT_MISMATCH")
        result[window] = row
    return result


def _insight_values(row: dict[str, object] | None) -> tuple[Decimal, int]:
    if row is None:
        return Decimal("0"), 0
    try:
        spend = Decimal(str(row.get("spend", "0")))
    except InvalidOperation as exc:
        raise FacebookEvidenceError("FB_SPEND_INVALID") from exc
    if not spend.is_finite() or spend < 0:
        raise FacebookEvidenceError("FB_SPEND_INVALID")
    return spend, _lead_count(row)


def _window_claim_subjects(
    request: EvidenceRequest,
    *,
    kind: SubjectKind,
    subject_id: str,
    window: TimeWindow,
) -> tuple[SubjectRef, ...]:
    return tuple(
        dict.fromkeys(
            claim.subject
            for claim in request.claims
            if claim.source is SourceSystem.FACEBOOK
            and claim.subject.kind is kind
            and claim.subject.subject_id == subject_id
            and claim.window == window
            and claim.metric
            in {Metric.SPEND, Metric.LEADS, Metric.WINDOW_START, Metric.WINDOW_END}
        )
    )


def _aggregate_records(
    request: EvidenceRequest,
    *,
    kind: SubjectKind,
    subject_id: str,
    window: TimeWindow,
    spend: Decimal,
    leads: int,
    currency: str,
    entity_ids: tuple[str, ...],
    now: datetime,
) -> tuple[EvidenceRecord, ...]:
    """Материализует только independently computed значения для exact claims."""

    values: dict[Metric, object] = {
        Metric.SPEND: spend,
        Metric.LEADS: leads,
        Metric.WINDOW_START: window.start.isoformat(),
        Metric.WINDOW_END: window.end.isoformat(),
    }
    records: list[EvidenceRecord] = []
    subjects = _window_claim_subjects(
        request,
        kind=kind,
        subject_id=subject_id,
        window=window,
    )
    for claim in request.claims:
        if (
            claim.source is not SourceSystem.FACEBOOK
            or claim.subject not in subjects
            or claim.window != window
            or claim.metric not in values
            or (claim.metric is Metric.SPEND and claim.currency != currency)
        ):
            continue
        records.append(
            _record(
                subject=claim.subject,
                metric=claim.metric,
                value=values[claim.metric],
                now=now,
                window=window,
                currency=claim.currency,
                entity_ids=entity_ids,
                category=claim.category,
            )
        )
    return tuple(records)


def _load_adset_inventory(adset_id: str) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    adset = _graph_json(
        adset_id,
        {"fields": "id,name,account_id,daily_budget,effective_status"},
    )
    if str(adset.get("id") or adset_id) != adset_id:
        raise FacebookEvidenceError("FB_ADSET_MISMATCH")
    rows = _paginate(
        f"{adset_id}/ads",
        {
            "fields": "id,name,status,effective_status,created_time,adset_id,creative{id}",
            "limit": 500,
        },
    )
    for row in rows:
        if str(row.get("adset_id") or "") != adset_id:
            raise FacebookEvidenceError("FB_INVENTORY_ADSET_MISMATCH")
        for field in ("id", "name", "status", "effective_status"):
            if not isinstance(row.get(field), str) or not row.get(field):
                raise FacebookEvidenceError("FB_INVENTORY_ROW_INVALID")
    return adset, rows


def _load_account_inventory(account_id: str) -> tuple[dict[str, object], ...]:
    """Полный инвентарь кабинета — O(кабинета), только для стража покрытия.

    Evidence-путь ACCOUNT-претензий сюда больше не ходит (см.
    _load_account_insights); единственный потребитель — services.
    coverage_guard.FacebookAccountCoverageClient, один вызов на прогон.
    """

    rows = _paginate(
        f"act_{account_id}/ads",
        {
            "fields": "id,name,status,effective_status,created_time,adset_id,creative{id}",
            "limit": 500,
        },
    )
    for row in rows:
        for field in ("id", "name", "status", "effective_status", "adset_id"):
            if not isinstance(row.get(field), str) or not row.get(field):
                raise FacebookEvidenceError("FB_ACCOUNT_INVENTORY_ROW_INVALID")
    return rows


def _is_missing_object_error(error: object) -> bool:
    """Graph-ошибка «объекта нет или он недоступен токену», а не сбой связи.

    100 (+subcode 33) — объект не существует либо не виден токену, 803 — часть
    запрошенных id не существует. Только эти коды дают право ответить «не наш
    объект»; всё остальное (190, 5xx, rate limit) обязано подниматься наверх.
    """

    if not isinstance(error, dict):
        return False
    code = error.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        return False
    return code in _MISSING_OBJECT_ERROR_CODES


def _fetch_ad_accounts(chunk: tuple[str, ...]) -> dict[str, str] | None:
    """Читает account_id пачкой exact id; ``None`` — Graph отклонил весь батч."""

    response = _graph_response("/", {"ids": ",".join(chunk), "fields": "id,account_id"})
    if response.status_code != 200:
        try:
            rejected = _graph_payload(response).get("error")
        except FacebookEvidenceError:
            rejected = None
        if _is_missing_object_error(rejected):
            return None
        raise FacebookEvidenceError(f"FB_HTTP_{response.status_code}")
    payload = _graph_payload(response)
    error = payload.get("error")
    if error:
        if _is_missing_object_error(error):
            return None
        raise FacebookEvidenceError("FB_GRAPH_ERROR")
    owners: dict[str, str] = {}
    for ad_id in chunk:
        row = payload.get(ad_id)
        if row is None:
            # Graph не вернул объект в успешном ответе — доказательства
            # принадлежности нет, объявление считается чужим.
            continue
        if not isinstance(row, dict) or str(row.get("id") or ad_id) != ad_id:
            raise FacebookEvidenceError("FB_OWNERSHIP_ROW_INVALID")
        account_id = row.get("account_id")
        if not isinstance(account_id, str) or not account_id.strip():
            raise FacebookEvidenceError("FB_OWNERSHIP_ACCOUNT_MISSING")
        owners[ad_id] = _normalized_account_id(account_id)
    return owners


def load_account_ad_ownership(
    account_id: str, ad_ids: tuple[str, ...]
) -> frozenset[str]:
    """Возвращает подмножество ``ad_ids``, реально принадлежащее кабинету.

    Стоимость — O(числа проверяемых id): батчи по 50 точных id вместо выкачки
    всего инвентаря кабинета (у большого кабинета это десятки тысяч объявлений).
    Несуществующий или чужой id — честный отрицательный ответ; отклонённый
    целиком батч дробится пополам, чтобы найти конкретный «плохой» id. Любая
    другая ошибка Graph поднимается наверх — источник обязан деградировать.
    """

    account = _normalized_account_id(account_id)
    unique = tuple(sorted(set(ad_ids)))
    if not unique:
        return frozenset()
    owned: set[str] = set()
    pending: list[tuple[str, ...]] = [
        unique[offset : offset + _OBJECT_CHUNK_SIZE]
        for offset in range(0, len(unique), _OBJECT_CHUNK_SIZE)
    ]
    while pending:
        chunk = pending.pop()
        owners = _fetch_ad_accounts(chunk)
        if owners is None:
            if len(chunk) == 1:
                continue
            middle = len(chunk) // 2
            pending.append(chunk[:middle])
            pending.append(chunk[middle:])
            continue
        owned.update(ad_id for ad_id, owner in owners.items() if owner == account)
    return frozenset(owned)


def _record(
    *,
    subject: SubjectRef,
    metric: Metric,
    value: object,
    now: datetime,
    window: object = None,
    currency: str | None = None,
    entity_ids: tuple[str, ...] = (),
    category: FactCategory = FactCategory.BUSINESS_METRIC,
) -> EvidenceRecord:
    return EvidenceRecord(
        category=category,
        subject=subject,
        metric=metric,
        value=value,  # type: ignore[arg-type]
        source=SourceSystem.FACEBOOK,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=now,
        window=window,  # type: ignore[arg-type]
        currency=currency,
        entity_ids=entity_ids,
    )


def _error(now: datetime, code: str) -> SourceEvidence:
    return SourceEvidence(
        source=SourceSystem.FACEBOOK,
        state=EvidenceState.ERROR,
        fetched_at=now,
        data_as_of=None,
        from_cache=False,
        complete=False,
        records=(),
        error_code=code,
    )


def load_facebook_evidence(
    request: EvidenceRequest,
    now: datetime,
    *,
    force_live: bool,
) -> SourceEvidence:
    """Читает exact objects, chunks≤50, insights и полные adset inventories.

    ACCOUNT-агрегаты (spend/leads/границы окна) читаются одним запросом
    level=account на окно, без выкачки инвентаря кабинета.
    """

    del force_live  # Этот adapter всегда делает Graph GET и не имеет business cache.
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать timezone")
    records: list[EvidenceRecord] = []
    try:
        # Кабинеты заявки. Пустой список = «кабинет по умолчанию» (манифесты
        # PAUSE/UNPAUSE/SCALE кабинет не несут). Кабинет адсета и объявления —
        # факт из Graph, и при неявном списке он ДОЗАПРАШИВАЕТСЯ: без этого
        # любое действие по второму кабинету (cabinet_b) падало бы здесь на
        # FB_ADSET_ACCOUNT_NOT_REQUESTED, а ad-инсайты читались бы из чужого
        # кабинета. При ЯВНОМ списке (LAUNCH с destinations) строгость прежняя:
        # объект вне перечисленных кабинетов — отказ.
        explicit_accounts = bool(request.account_ids)
        account_ids = tuple(
            sorted({_normalized_account_id(value) for value in request.account_ids})
        ) or (_normalized_account_id(get_fb_account_id()),)
        account_currency: dict[str, str] = {}
        account_timezone: dict[str, str] = {}

        def _load_account(account_id: str) -> None:
            account = _graph_json(
                f"act_{account_id}",
                {"fields": "id,account_status,currency,timezone_name"},
            )
            if _normalized_account_id(account.get("id") or account_id) != account_id:
                raise FacebookEvidenceError("FB_ACCOUNT_MISMATCH")
            currency = account.get("currency")
            timezone_name = account.get("timezone_name")
            account_status = account.get("account_status")
            if (
                not isinstance(currency, str)
                or not currency
                or not isinstance(timezone_name, str)
                or not timezone_name
                or isinstance(account_status, bool)
                or not isinstance(account_status, int)
            ):
                raise FacebookEvidenceError("FB_ACCOUNT_FIELDS_INVALID")
            account_currency[account_id] = currency
            account_timezone[account_id] = timezone_name
            subject = SubjectRef(SubjectKind.ACCOUNT, account_id)
            records.extend(
                (
                    _record(
                        subject=subject,
                        metric=Metric.CONFIGURED_STATUS,
                        value=str(account_status),
                        now=now,
                        category=FactCategory.ACTION_STATE,
                    ),
                    _record(
                        subject=subject,
                        metric=Metric.DISPLAY_CONTEXT,
                        value=f"{currency}|{timezone_name}",
                        now=now,
                        category=FactCategory.DISPLAY_CONTEXT,
                    ),
                )
            )

        def _ensure_account(account_id: str, error_code: str) -> None:
            """Кабинет объекта известен из Graph: при неявном списке — дозапрос."""
            if account_id in account_currency and account_id in account_timezone:
                return
            if explicit_accounts:
                raise FacebookEvidenceError(error_code)
            _load_account(account_id)

        for account_id in account_ids:
            _load_account(account_id)

        for account_id in account_ids:
            # ACCOUNT-агрегат считает сам Graph одним запросом level=account —
            # без O(кабинета) выкачки инвентаря и посуммирования по ad.id
            # (см. _load_account_insights). Перечислять entity_ids нечего:
            # значение принадлежит кабинету-субъекту целиком, как и остальные
            # ACCOUNT-записи (CONFIGURED_STATUS/DISPLAY_CONTEXT) выше.
            needs_aggregate = any(
                claim.source is SourceSystem.FACEBOOK
                and claim.subject.kind is SubjectKind.ACCOUNT
                and claim.subject.subject_id == account_id
                and claim.metric
                in {Metric.SPEND, Metric.LEADS, Metric.WINDOW_START, Metric.WINDOW_END}
                for claim in request.claims
            )
            if not needs_aggregate:
                continue
            account_insights = _load_account_insights(
                account_id,
                request,
                timezone_name=account_timezone[account_id],
            )
            for window in request.windows:
                row = account_insights.get(window)
                if row is None:
                    continue
                spend, leads = _insight_values(row)
                records.extend(
                    _aggregate_records(
                        request,
                        kind=SubjectKind.ACCOUNT,
                        subject_id=account_id,
                        window=window,
                        spend=spend,
                        leads=leads,
                        currency=account_currency[account_id],
                        entity_ids=(),
                        now=now,
                    )
                )

        ad_ids = tuple(sorted(set(request.ad_ids)))
        ads = _load_exact_ads(ad_ids) if ad_ids else {}
        # Кабинет каждого объявления — из самого объекта Graph; без него
        # инсайты читались бы из кабинета по умолчанию, и объявление второго
        # кабинета выглядело бы «без расхода и лидов».
        ad_account: dict[str, str] = {}
        for ad_id, ad in ads.items():
            raw_account = ad.get("account_id")
            resolved = _normalized_account_id(raw_account) if raw_account else ""
            if not resolved:
                if len(account_ids) != 1:
                    raise FacebookEvidenceError("FB_EXACT_AD_ACCOUNT_AMBIGUOUS")
                resolved = account_ids[0]
            _ensure_account(resolved, "FB_EXACT_AD_ACCOUNT_NOT_REQUESTED")
            ad_account[ad_id] = resolved
        insights: dict[tuple[str, TimeWindow], dict[str, object]] = {}
        exact_ad_windows: frozenset[TimeWindow] = frozenset()
        if ad_ids and request.windows:
            by_account: dict[str, list[str]] = {}
            for ad_id in ad_ids:
                by_account.setdefault(ad_account[ad_id], []).append(ad_id)
            window_sets: list[frozenset[TimeWindow]] = []
            for account_id, account_ad_ids in by_account.items():
                part, part_windows = _load_insights(
                    account_id,
                    tuple(account_ad_ids),
                    request,
                    timezone_name=account_timezone[account_id],
                )
                insights.update(part)
                window_sets.append(part_windows)
            # Окно точное, только если оно точное для КАЖДОГО кабинета.
            exact_ad_windows = frozenset.intersection(*window_sets) if window_sets else frozenset()
        for ad_id, ad in ads.items():
            adset_id = str(ad.get("adset_id") or "")
            status = ad.get("status")
            effective_status = ad.get("effective_status")
            if not adset_id or not isinstance(status, str) or not isinstance(effective_status, str):
                raise FacebookEvidenceError("FB_AD_FIELDS_INVALID")
            subject = SubjectRef(SubjectKind.AD, ad_id, adset_id)
            records.extend(
                (
                    _record(
                        subject=subject,
                        metric=Metric.CONFIGURED_STATUS,
                        value=status,
                        now=now,
                        category=FactCategory.ACTION_STATE,
                        entity_ids=(ad_id,),
                    ),
                    _record(
                        subject=subject,
                        metric=Metric.EFFECTIVE_STATUS,
                        value=effective_status,
                        now=now,
                        category=FactCategory.ACTION_STATE,
                        entity_ids=(ad_id,),
                    ),
                )
            )
            for window in request.windows:
                if window not in exact_ad_windows:
                    continue
                spend, leads = _insight_values(insights.get((ad_id, window)))
                records.extend(
                    (
                        _record(
                            subject=subject,
                            metric=Metric.SPEND,
                            value=spend,
                            now=now,
                            window=window,
                            currency=account_currency[ad_account[ad_id]],
                            entity_ids=(ad_id,),
                        ),
                        _record(
                            subject=subject,
                            metric=Metric.LEADS,
                            value=leads,
                            now=now,
                            window=window,
                            entity_ids=(ad_id,),
                        ),
                    )
                )

        for adset_id in sorted(set(request.adset_ids)):
            adset, inventory = _load_adset_inventory(adset_id)
            try:
                budget = Decimal(str(adset.get("daily_budget"))) / Decimal("100")
            except (InvalidOperation, TypeError) as exc:
                raise FacebookEvidenceError("FB_BUDGET_INVALID") from exc
            if not budget.is_finite() or budget < 0:
                raise FacebookEvidenceError("FB_BUDGET_INVALID")
            account_id = _normalized_account_id(adset.get("account_id") or account_ids[0])
            _ensure_account(account_id, "FB_ADSET_ACCOUNT_NOT_REQUESTED")
            inventory_ids = tuple(sorted(str(row["id"]) for row in inventory))
            subject = SubjectRef(SubjectKind.ADSET, adset_id, account_id)
            records.extend(
                (
                    _record(
                        subject=subject,
                        metric=Metric.EFFECTIVE_STATUS,
                        value=str(adset.get("effective_status") or ""),
                        now=now,
                        category=FactCategory.ACTION_STATE,
                        entity_ids=inventory_ids,
                    ),
                    _record(
                        subject=subject,
                        metric=Metric.DAILY_BUDGET,
                        value=budget,
                        now=now,
                        currency=account_currency.get(account_id),
                        entity_ids=inventory_ids,
                    ),
                    _record(
                        subject=subject,
                        metric=Metric.CAPACITY,
                        value=max(0, _MAX_ADS_PER_ADSET - len(inventory_ids)),
                        now=now,
                        entity_ids=inventory_ids,
                    ),
                )
            )
            adset_insights, exact_windows = _load_insights(
                account_id,
                inventory_ids,
                request,
                timezone_name=account_timezone[account_id],
            )
            for window in request.windows:
                if window not in exact_windows:
                    continue
                total_spend = Decimal("0")
                total_leads = 0
                for ad_id in inventory_ids:
                    spend, leads = _insight_values(adset_insights.get((ad_id, window)))
                    total_spend += spend
                    total_leads += leads
                records.extend(
                    _aggregate_records(
                        request,
                        kind=SubjectKind.ADSET,
                        subject_id=adset_id,
                        window=window,
                        spend=total_spend,
                        leads=total_leads,
                        currency=account_currency[account_id],
                        entity_ids=inventory_ids,
                        now=now,
                    )
                )
    except Exception as exc:
        code = str(exc) if isinstance(exc, FacebookEvidenceError) else type(exc).__name__
        evidence = _error(now, code[:120])
        if isinstance(exc, FacebookEvidenceError):
            return SourceEvidence(
                source=evidence.source,
                state=EvidenceState.INCOMPLETE,
                fetched_at=evidence.fetched_at,
                data_as_of=evidence.data_as_of,
                from_cache=evidence.from_cache,
                complete=False,
                records=(),
                error_code=evidence.error_code,
            )
        return evidence

    return SourceEvidence(
        source=SourceSystem.FACEBOOK,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
        records=tuple(records),
    )


def _manifest_request(manifest: ActionManifest, now: datetime, created_ids: tuple[str, ...] = ()) -> EvidenceRequest:
    if isinstance(manifest, LaunchManifest):
        account_ids = tuple(destination.account_id for destination in manifest.destinations)
        adset_ids = tuple(destination.adset_id for destination in manifest.destinations)
        ad_ids = created_ids
        windows = ()
    elif isinstance(manifest, PauseManifest):
        account_ids = ()
        adset_ids = (manifest.adset_id,)
        ad_ids = (manifest.ad_id, *manifest.sibling_active_ids)
        if manifest.replacement_ad_id:
            ad_ids = (*ad_ids, manifest.replacement_ad_id)
        windows = (manifest.decision_window,)
    elif isinstance(manifest, UnpauseManifest):
        account_ids = ()
        adset_ids = (manifest.adset_id,)
        ad_ids = (manifest.ad_id,)
        windows = ()
    elif isinstance(manifest, ScaleManifest):
        account_ids = ()
        adset_ids = (manifest.adset_id,)
        ad_ids = manifest.candidate_ad_ids
        windows = tuple(claim.window for claim in manifest.facts if claim.window is not None)
    elif isinstance(manifest, AssetRecoveryManifest):
        account_ids = (manifest.account_id,)
        adset_ids = tuple(
            dict.fromkeys((manifest.source_adset_id, manifest.target_adset_id))
        )
        ad_ids = tuple(dict.fromkeys((manifest.source_ad_id, *created_ids)))
        windows = ()
    else:  # pragma: no cover - union exhaustive, fail-closed for runtime misuse
        raise TypeError("Неизвестный ActionManifest")
    return EvidenceRequest(
        request_id=f"facebook:{manifest.manifest_id}",
        purpose="ACTION",
        action_kind=manifest.kind,
        generated_at=now,
        subjects=(),
        claims=manifest.facts if isinstance(manifest, (PauseManifest, ScaleManifest)) else (),
        required_sources=(SourceSystem.FACEBOOK,),
        windows=tuple(dict.fromkeys(windows)),
        account_ids=tuple(dict.fromkeys(account_ids)),
        adset_ids=tuple(dict.fromkeys(adset_ids)),
        ad_ids=tuple(dict.fromkeys(ad_ids)),
        card_ids=(),
        staged_relative_paths=(),
        include_full_inventory=True,
        force_live=True,
        max_age_seconds=0,
    )


def _observation(evidence: SourceEvidence, now: datetime, target_ids: set[str]) -> ActionObservation:
    business = tuple(
        {
            "subject": record.subject,
            "metric": record.metric,
            "value": record.value,
            "window": record.window,
            "currency": record.currency,
            "entity_ids": record.entity_ids,
        }
        for record in evidence.records
    )
    unrelated = tuple(
        row for row, record in zip(business, evidence.records, strict=True)
        if record.subject.subject_id not in target_ids
    )
    target_state = "|".join(
        str(record.value)
        for record in evidence.records
        if record.subject.subject_id in target_ids
        and record.metric in {Metric.CONFIGURED_STATUS, Metric.EFFECTIVE_STATUS}
    ) or evidence.state.value
    return ActionObservation(
        observed_at=now,
        digest=hashlib.sha256(canonical_json(business)).hexdigest(),
        target_state=target_state,
        subject_ids=tuple(sorted(target_ids)),
        unrelated_state_digest=hashlib.sha256(canonical_json(unrelated)).hexdigest(),
    )


def _pause_inventory_observation(
    manifest: PauseManifest | UnpauseManifest,
    now: datetime,
) -> ActionObservation:
    """Canonical provider state, совместимый с pause guard и manifest digest."""

    _adset, inventory = _load_adset_inventory(manifest.adset_id)
    normalized: list[dict[str, str]] = []
    target: dict[str, str] | None = None
    for row in inventory:
        exact = {
            "ad_id": str(row.get("id") or ""),
            "adset_id": str(row.get("adset_id") or ""),
            "configured_status": str(row.get("status") or ""),
            "effective_status": str(row.get("effective_status") or ""),
        }
        normalized.append(exact)
        if exact["ad_id"] == manifest.ad_id:
            target = exact
    if target is None:
        raise FacebookEvidenceError("FB_TARGET_NOT_IN_INVENTORY")
    try:
        digest = inventory_state_sha256(
            normalized,
            expected_adset_id=manifest.adset_id,
        )
        unrelated_digest = inventory_state_sha256(
            (row for row in normalized if row["ad_id"] != manifest.ad_id),
            expected_adset_id=manifest.adset_id,
        )
    except (TypeError, ValueError) as exc:
        raise FacebookEvidenceError("FB_INVENTORY_CANONICAL_INVALID") from exc
    return ActionObservation(
        observed_at=now,
        digest=digest,
        target_state=(
            f"{target['configured_status']}|{target['effective_status']}"
        ),
        subject_ids=(manifest.ad_id,),
        unrelated_state_digest=unrelated_digest,
    )


def _launch_duplicate_records(
    manifest: LaunchManifest,
    now: datetime,
    expected_inventory_by_adset: dict[str, tuple[str, ...]],
) -> tuple[EvidenceRecord, ...]:
    """Сравнивает opaque manifest signature только в manifest-aware контуре."""

    records: list[EvidenceRecord] = []
    for destination in manifest.destinations:
        adset, inventory = _load_adset_inventory(destination.adset_id)
        # Манифест «Все города» может нести цели в ДВУХ кабинетах (cabinet_a и
        # cabinet_b, карта маршрутизации город→кабинет). Каждый adset обязан
        # живьём принадлежать кабинету СВОЕЙ цели — иначе план противоречив.
        live_account = _normalized_account_id(adset.get("account_id"))
        if live_account != _normalized_account_id(destination.account_id):
            raise FacebookEvidenceError("FB_LAUNCH_DESTINATION_ACCOUNT_DRIFT")
        inventory_ids = tuple(sorted(str(row.get("id")) for row in inventory))
        expected_inventory = expected_inventory_by_adset.get(destination.adset_id)
        if expected_inventory is not None and inventory_ids != expected_inventory:
            raise FacebookEvidenceError("FB_INVENTORY_CHANGED")
        expected_names = {creative.ad_name for creative in destination.creatives}
        live_names = {str(row.get("name") or "") for row in inventory}
        live_signatures: set[str] = set()
        for row in inventory:
            direct = row.get("duplicate_signature")
            creative = row.get("creative") or {}
            nested = creative.get("duplicate_signature") if isinstance(creative, dict) else None
            for candidate in (direct, nested):
                if isinstance(candidate, str) and candidate:
                    live_signatures.add(candidate)
        if destination.duplicate_signature in live_signatures:
            raise FacebookEvidenceError("FB_LAUNCH_DUPLICATE_SIGNATURE")
        if expected_names & live_names:
            raise FacebookEvidenceError("FB_LAUNCH_PARTIAL_DUPLICATE")
        records.append(
            _record(
                subject=SubjectRef(
                    SubjectKind.ADSET,
                    destination.adset_id,
                    destination.account_id,
                ),
                metric=Metric.MATCH_STATE,
                value="NO_DUPLICATE",
                now=now,
                category=FactCategory.MATCH,
                entity_ids=(*inventory_ids, f"signature:{destination.duplicate_signature}"),
            )
        )
    return tuple(records)


def read_action_precondition(manifest: ActionManifest, now: datetime) -> ActionObservation:
    if isinstance(manifest, AssetRecoveryManifest):
        return _asset_recovery_observation(manifest, now, created_ids=())
    evidence = load_facebook_evidence(_manifest_request(manifest, now), now, force_live=True)
    if not evidence.complete:
        raise FacebookEvidenceError(evidence.error_code or "FB_PRECONDITION_INCOMPLETE")
    if isinstance(manifest, (PauseManifest, UnpauseManifest)):
        return _pause_inventory_observation(manifest, now)
    if isinstance(manifest, LaunchManifest):
        expected_inventory_by_adset = {
            record.subject.subject_id: record.entity_ids
            for record in evidence.records
            if record.source is SourceSystem.FACEBOOK
            and record.subject.kind is SubjectKind.ADSET
            and record.metric is Metric.CAPACITY
        }
        duplicate_records = _launch_duplicate_records(
            manifest,
            now,
            expected_inventory_by_adset,
        )
        evidence = SourceEvidence(
            source=evidence.source,
            state=evidence.state,
            fetched_at=evidence.fetched_at,
            data_as_of=evidence.data_as_of,
            from_cache=evidence.from_cache,
            complete=evidence.complete,
            records=(*evidence.records, *duplicate_records),
            payments=evidence.payments,
            error_code=evidence.error_code,
        )
        target_ids = {destination.adset_id for destination in manifest.destinations}
    elif isinstance(manifest, ScaleManifest):
        target_ids = {manifest.adset_id}
    else:
        target_ids = {manifest.ad_id}
    return _observation(evidence, now, target_ids)


def read_action_postcondition(
    manifest: ActionManifest,
    created_ids: tuple[str, ...],
    now: datetime,
) -> ActionObservation:
    if isinstance(manifest, AssetRecoveryManifest):
        return _asset_recovery_observation(manifest, now, created_ids=created_ids)
    evidence = load_facebook_evidence(
        _manifest_request(manifest, now, created_ids),
        now,
        force_live=True,
    )
    if not evidence.complete:
        raise FacebookEvidenceError(evidence.error_code or "FB_POSTCONDITION_INCOMPLETE")
    if isinstance(manifest, (PauseManifest, UnpauseManifest)):
        return _pause_inventory_observation(manifest, now)
    if isinstance(manifest, LaunchManifest):
        target_ids = set(created_ids)
    elif isinstance(manifest, ScaleManifest):
        target_ids = {manifest.adset_id}
    else:
        target_ids = {manifest.ad_id}
    return _observation(evidence, now, target_ids)


def _asset_recovery_inventory_payload(info: dict[str, object]) -> tuple[dict[str, str], ...]:
    ads = info.get("ads")
    if not isinstance(ads, list) or any(not isinstance(row, dict) for row in ads):
        raise FacebookEvidenceError("FB_ASSET_INVENTORY_INVALID")
    return tuple(
        sorted(
            (
                {
                    "id": str(row.get("id") or ""),
                    "name": str(row.get("name") or ""),
                    "status": str(row.get("status") or ""),
                    "effective_status": str(row.get("effective_status") or ""),
                    "creative_id": str((row.get("creative") or {}).get("id") or "")
                    if isinstance(row.get("creative"), dict)
                    else "",
                }
                for row in ads
            ),
            key=lambda row: row["id"],
        )
    )


def asset_recovery_inventory_sha256(info: dict[str, object]) -> str:
    """Canonical digest полного target inventory для immutable manifest."""

    return hashlib.sha256(canonical_json(_asset_recovery_inventory_payload(info))).hexdigest()


def asset_recovery_source_sha256(source: dict[str, object]) -> str:
    """Canonical digest exact source identity без токенов и transport данных."""

    payload = {
        key: str(source.get(key) or "")
        for key in (
            "ad_id",
            "name",
            "account_id",
            "adset_id",
            "adset_name",
            "status",
            "effective_status",
            "creative_id",
        )
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _asset_recovery_observation(
    manifest: AssetRecoveryManifest,
    now: datetime,
    *,
    created_ids: tuple[str, ...],
) -> ActionObservation:
    """Два force-live чтения используют один exact recovery contract."""

    from integrations.facebook import get_adset_info, get_existing_ad_creative_source
    from services.ad_asset_recovery import _asset_recovery_hard_reserve_slots
    from services.launch_repository import normalize_launch_name

    if str(get_fb_account_id()).removeprefix("act_") != manifest.account_id:
        raise FacebookEvidenceError("FB_ASSET_ACCOUNT_DRIFT")
    if manifest.hard_reserve_slots != _asset_recovery_hard_reserve_slots():
        raise FacebookEvidenceError("FB_ASSET_HARD_RESERVE_DRIFT")
    source = get_existing_ad_creative_source(
        manifest.source_ad_id,
        manifest.account_id,
        manifest.source_adset_id,
        manifest.source_adset_name,
        manifest.adset_type,
    )
    if (
        source.get("name") != manifest.source_ad_name
        or source.get("creative_id") != manifest.source_creative_id
        or asset_recovery_source_sha256(source) != manifest.source_identity_sha256
    ):
        raise FacebookEvidenceError("FB_ASSET_SOURCE_DRIFT")
    target = get_adset_info(manifest.target_adset_id)
    if (
        target.get("inventory_complete") is not True
        or target.get("unknown_effective_status_ids") != []
        or target.get("name") != manifest.target_adset_name
        or target.get("adset_effective_status") != "ACTIVE"
        or str(target.get("account_id") or "").removeprefix("act_")
        != manifest.account_id
        or type(target.get("ad_count")) is not int
        or target.get("ad_count") != len(target.get("ads") or [])
    ):
        raise FacebookEvidenceError("FB_ASSET_TARGET_INCOMPLETE")
    inventory = _asset_recovery_inventory_payload(target)
    exact = [row for row in inventory if row["name"] == manifest.target_ad_name]
    identity = [
        row
        for row in inventory
        if normalize_launch_name(row["name"]) == manifest.target_identity_key
    ]
    if not created_ids:
        if exact or identity:
            raise FacebookEvidenceError("FB_ASSET_TARGET_DUPLICATE")
        if asset_recovery_inventory_sha256(target) != manifest.pre_inventory_sha256:
            raise FacebookEvidenceError("FB_ASSET_INVENTORY_DRIFT")
        available = _MAX_ADS_PER_ADSET - int(target["ad_count"])
        if available != manifest.capacity_available or available < 1 + manifest.hard_reserve_slots:
            raise FacebookEvidenceError("FB_ASSET_CAPACITY_DRIFT")
        if int(target.get("effective_active_count") or 0) < 1:
            raise FacebookEvidenceError("FB_ASSET_ZERO_ACTIVE")
        target_state = "ABSENT"
    else:
        if len(created_ids) != 1 or not created_ids[0].isdigit():
            raise FacebookEvidenceError("FB_ASSET_CREATED_ID_INVALID")
        matches = [row for row in exact if row["id"] == created_ids[0]]
        if (
            len(exact) != 1
            or len(identity) != 1
            or len(matches) != 1
            or matches[0]["creative_id"] != manifest.source_creative_id
            or matches[0]["status"] != "ACTIVE"
            or matches[0]["effective_status"] != "ACTIVE"
        ):
            raise FacebookEvidenceError("FB_ASSET_POSTCONDITION_DRIFT")
        target_state = f"ACTIVE:{created_ids[0]}"
    payload = {"source": source, "target_inventory": inventory}
    return ActionObservation(
        observed_at=now,
        digest=hashlib.sha256(canonical_json(payload)).hexdigest(),
        target_state=target_state,
        subject_ids=(f"adset:{manifest.target_adset_id}",),
        unrelated_state_digest=hashlib.sha256(canonical_json(source)).hexdigest(),
    )
