"""FastAPI роутер для дашборда "Источники" — откуда приходят лиды по каналам."""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta, date

from fastapi import APIRouter, HTTPException, Query

from integrations.amo import (
    get_leads_window, get_contacts_with_leads_batch, get_leads_batch,
)
from services.sources import (
    aggregate_by_source, classify_lead_source, parse_parent_lead_id,
    SOURCE_COLORS, QUAL_STATUS_IDS,
)
from config import AMO_PAYMENT_STATUS_IDS

logger = logging.getLogger(__name__)

# Переменная называется router (не sources_router) — подключается в app.py как alias
router = APIRouter(tags=["sources"])
sources_router = router  # alias для подключения в app.py

# Кеш в памяти: ключ -> {"data": dict, "ts": float}
_cache: dict = {}
_CACHE_TTL = 900  # 15 минут
_FB_TIMEOUT = 20  # сек — максимум ждём FB Ads spend, иначе отдаём без него


# Каналы — «настоящий первоисточник» при восстановлении из корня.
# НЕ включаем «Бот/Рассылка» (это наша реактивация, а не происхождение),
# «Умное копирование», «База», «Другое» — это не источники привлечения.
_REAL_SOURCE_CHANNELS = {
    "Facebook Ads", "Instagram Ads", "Google Ads", "TikTok",
    "Taplink", "Tilda сайт", "Каталог-карты", "Звонок", "WhatsApp", "Рефералка",
}

# Максимальная глубина цепочки «копия копии» при поиске оригинала сделки
_MAX_COPY_CHAIN = 3

# Размер чанка при запросах в AMO. Чанки идут независимо: один оборванный запрос
# (AMO периодически отдаёт RemoteDisconnected) не должен отменять восстановление
# для всех остальных лидов — лучше частичный результат, чем нулевой.
_CONTACT_CHUNK = 50
_LEADS_CHUNK = 50

# Период грузим окнами по неделе. Месяц одним запросом AMO не вывозит: одна страница,
# не отданная за 60с даже после ретраев, роняла бы весь расчёт в 502.
_WINDOW_CHUNK_DAYS = 7


def _ts_to_day(unix_ts: int) -> str:
    """unix → 'YYYY-MM-DD' в тех же сутках (UTC+3), что и разбивка by_day."""
    return datetime.fromtimestamp(unix_ts + 3 * 3600, tz=timezone.utc).strftime("%Y-%m-%d")


def _load_leads_windowed(from_ts: int, to_ts: int) -> tuple[list[dict], list[dict]]:
    """Грузит лиды недельными окнами.

    Returns: (лиды, пропущенные диапазоны). Сбой окна не отменяет остальные —
    частичные данные с честной пометкой полезнее, чем 502 на весь месяц.
    """
    leads: list[dict] = []
    gaps: list[dict] = []
    span = _WINDOW_CHUNK_DAYS * 86400
    start = from_ts
    while start <= to_ts:
        end = min(start + span - 1, to_ts)
        try:
            leads += get_leads_window(start, end)
        except Exception as e:
            gap = {"from": _ts_to_day(start), "to": _ts_to_day(end)}
            gaps.append(gap)
            logger.warning("AMO не отдал окно %s..%s: %s", gap["from"], gap["to"], e)
        start = end + 1
    return leads, gaps


def _safe_contacts(contact_ids: list[int]) -> dict[int, list[int]]:
    """{contact_id: [lead_id]} чанками. Сбой чанка логируется и пропускается."""
    out: dict[int, list[int]] = {}
    failed = 0
    for i in range(0, len(contact_ids), _CONTACT_CHUNK):
        chunk = contact_ids[i:i + _CONTACT_CHUNK]
        try:
            out.update(get_contacts_with_leads_batch(chunk))
        except Exception as e:
            failed += len(chunk)
            logger.warning("AMO не отдал историю %d контактов: %s", len(chunk), e)
    if failed:
        logger.warning(
            "История контактов получена частично: %d из %d не пришли",
            failed, len(contact_ids),
        )
    return out


def _safe_leads(lead_ids: list[int]) -> dict[int, dict]:
    """{lead_id: classifiable} чанками. Сбой чанка логируется и пропускается."""
    out: dict[int, dict] = {}
    failed = 0
    for i in range(0, len(lead_ids), _LEADS_CHUNK):
        chunk = lead_ids[i:i + _LEADS_CHUNK]
        try:
            for raw in get_leads_batch(chunk):
                out[raw["id"]] = _to_classifiable(raw)
        except Exception as e:
            failed += len(chunk)
            logger.warning("AMO не отдал %d прошлых лидов: %s", len(chunk), e)
    if failed:
        logger.warning("Прошлые лиды получены частично: %d из %d не пришли", failed, len(lead_ids))
    return out


def _to_classifiable(raw_lead: dict) -> dict:
    """RAW-лид из get_leads_batch → dict в формате, который понимает classify."""
    return {
        "name": raw_lead.get("name", ""),
        "source_id": raw_lead.get("source_id"),
        "custom_fields": raw_lead.get("custom_fields_values") or [],
        "tags": (raw_lead.get("_embedded") or {}).get("tags") or [],
        "created_at": raw_lead.get("created_at") or 0,
    }


def _enrich_baza(leads: list[dict]) -> None:
    """Для лидов-«Неизвестно» восстанавливает источник по истории контакта.

    Логика (в памяти, в AMO ничего не пишется):
      1. Берём корневые лиды контакта.
      2. Если у самого старого есть РЕАЛЬНЫЙ источник → канал корня
         (Facebook/каталог-карты/… — первоисточник).
      3. Если история есть, но источника нет нигде → «База».
      4. Нет истории → остаётся как было («Неизвестно» / «Бот/Рассылка»).

    Кандидаты — «Неизвестно» И «Бот/Рассылка»: бот:outbound = мы написали
    первыми по базе, значит у человека есть прошлый лид с настоящим источником.
    «Бот/Рассылка» остаётся только у реально холодных (без истории).
    """
    candidates = [
        l for l in leads
        if classify_lead_source(l) in ("Неизвестно", "Бот/Рассылка")
    ]
    _recover_from_history(candidates, fallback="База")


def _enrich_smart_copy(leads: list[dict]) -> None:
    """Для копий сделок AMO («Умное копирование») восстанавливает первоисточник.

    При копировании AMO не переносит на новый лид ни UTM, ни теги, а source_id
    подменяет на 24557225 — рекламная атрибуция теряется. Единственная связь
    с оригиналом остаётся в имени: «Сделка #<id оригинала>».

    Порядок:
      1. Имя содержит id оригинала → догружаем его и берём канал оттуда
         (цепочка «копия копии» разматывается до _MAX_COPY_CHAIN шагов).
      2. Не помогло — имя без ссылки («повторный заказ»), оригинал не отдался
         или сам без меток → ищем первоисточник в прошлых сделках контакта.
      3. Не нашли нигде → остаётся «Умное копирование».

    Канал может уехать в Facebook/каталог-карты/…, но сделка всё равно повторная —
    поэтому _is_repeat ставится всем копиям безусловно.
    """
    copies = [l for l in leads if classify_lead_source(l) == "Умное копирование"]
    if not copies:
        return

    for lead in copies:
        lead["_is_repeat"] = True

    parent_by_lead: dict[int, int] = {}  # id копии → id оригинала из имени
    for lead in copies:
        parent_id = parse_parent_lead_id(lead)
        # «Сделка #<свой id>» — дефолтное имя AMO, а не ссылка на оригинал
        if parent_id and parent_id != lead["id"]:
            parent_by_lead[lead["id"]] = parent_id

    if parent_by_lead:
        # Оригиналы, уже загруженные в периоде, второй раз из AMO не тянем
        known = {l["id"]: l for l in leads}
        _recover_from_parents({l["id"]: l for l in copies}, parent_by_lead, known)

    # Оригинала в имени не было, он не нашёлся или сам оказался без меток —
    # ищем первоисточник глубже, в прошлых сделках того же контакта.
    still_stuck = [l for l in copies if classify_lead_source(l) == "Умное копирование"]
    if still_stuck:
        _recover_from_history(still_stuck, fallback=None)

    left = sum(1 for l in copies if classify_lead_source(l) == "Умное копирование")
    # warning, а не info: в веб-приложении нет basicConfig, root-логгер на WARNING —
    # info в журнал не попадает. Соседние модули по той же причине пишут warning.
    logger.warning("Копии сделок: восстановлено %d из %d, без источника %d",
                   len(copies) - left, len(copies), left)


def _recover_from_parents(
    lead_by_id: dict[int, dict],
    pending: dict[int, int],
    known: dict[int, dict] | None = None,
) -> None:
    """Переносит канал оригинала на копию.

    pending: id копии → id оригинала. Если оригинал сам оказался копией —
    поднимаемся выше, но не глубже _MAX_COPY_CHAIN (защита от циклов).
    known: лиды, уже загруженные за период — их из AMO не запрашиваем.
    """
    known = known or {}
    for _ in range(_MAX_COPY_CHAIN):
        if not pending:
            return
        missing = [pid for pid in set(pending.values()) if pid not in known]
        fetched = _safe_leads(missing) if missing else {}
        if not fetched and not known:
            return

        next_pending: dict[int, int] = {}
        for lead_id, parent_id in pending.items():
            parent = known.get(parent_id) or fetched.get(parent_id)
            if parent is None:
                continue
            channel = classify_lead_source(parent)
            if channel in _REAL_SOURCE_CHANNELS:
                lead_by_id[lead_id]["_channel_override"] = channel
            elif channel == "Умное копирование":
                grandparent_id = parse_parent_lead_id(parent)
                # Самоссылка («Сделка #<свой id>») — тупик, а не следующий уровень
                if grandparent_id and grandparent_id != parent_id:
                    next_pending[lead_id] = grandparent_id
        pending = next_pending


def _recover_from_history(candidates: list[dict], fallback: str | None) -> None:
    """Ищет первоисточник среди прошлых лидов контакта и ставит override канала.

    Args:
        candidates: лиды, которым нужен источник.
        fallback: канал, если история есть, но источника нет нигде.
                  None — оставить лид как классифицировался.
    """
    if not candidates:
        return

    # Контакт каждого кандидата
    lead_cid: dict[int, int] = {}
    for lead in candidates:
        contacts = lead.get("contacts") or []
        if contacts:
            cid = contacts[0].get("id") if isinstance(contacts[0], dict) else contacts[0]
            if cid:
                lead_cid[lead["id"]] = cid
    if not lead_cid:
        return

    # Шаг 1: контакт → все его лиды
    contact_leads = _safe_contacts(list(set(lead_cid.values())))
    if not contact_leads:
        logger.warning("История контактов недоступна — источник не восстанавливаем")
        return

    # Шаг 2: собираем ID всех корневых лидов (кроме самих кандидатов) и догружаем
    candidate_ids = {l["id"] for l in candidates}
    root_ids = {
        lid for ids in contact_leads.values() for lid in ids
        if lid not in candidate_ids
    }
    roots_by_id = _safe_leads(list(root_ids)) if root_ids else {}

    # Шаг 3: для каждого кандидата — ищем самый старый корень с реальным источником
    for lead in candidates:
        cid = lead_cid.get(lead["id"])
        if not cid:
            continue
        other_ids = [lid for lid in contact_leads.get(cid, []) if lid != lead["id"]]
        if not other_ids:
            continue  # нет истории — остаётся как было (Неизвестно / Бот-Рассылка)

        # У контакта есть прошлые лиды → это повторное обращение, а не новое привлечение
        lead["_is_repeat"] = True

        roots = sorted(
            (roots_by_id[i] for i in other_ids if i in roots_by_id),
            key=lambda r: r["created_at"],
        )
        recovered_channel = None
        for root in roots:
            ch = classify_lead_source(root)
            if ch in _REAL_SOURCE_CHANNELS:
                recovered_channel = ch
                break

        # Прямой override канала — детерминированно, не ломается на имя-зависимых корнях
        channel = recovered_channel or fallback
        if channel:
            lead["_channel_override"] = channel

# Допустимые значения days
_VALID_DAYS = {7, 14, 30, 90}


async def _compute_sources(date_from: str, date_to: str) -> dict:
    """Основная логика: кеш → AMO → FB spend → агрегация.

    date_from / date_to — строки 'YYYY-MM-DD'.
    Возвращает dict по схеме §6 спеки.
    """
    cache_key = f"sources:{date_from}:{date_to}"
    cached = _cache.get(cache_key)

    # Отдаём из кеша если TTL не истёк
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        return cached["data"]

    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)

    # Границы окна. Сутки считаются по UTC+3 — так же, как разбивка by_day в _format_day.
    from_ts_filter = int(datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc).timestamp()) - 3 * 3600
    to_ts_filter = int(datetime(d_to.year, d_to.month, d_to.day, 23, 59, 59, tzinfo=timezone.utc).timestamp()) - 3 * 3600

    # Загружаем лиды строго за период, недельными окнами. Раньше звали get_leads(days),
    # а он отсчитывает N дней ОТ СЕГОДНЯ — запрос прошлого месяца терял его начало
    # (при запросе в начале текущего месяца первые дни прошлого приходили нулями).
    leads, gaps = await asyncio.to_thread(_load_leads_windowed, from_ts_filter, to_ts_filter)

    if not leads and gaps:
        # Не пришло вообще ничего — отдать нули значило бы соврать
        logger.error("AMO не отдал ни одного окна для %s..%s", date_from, date_to)
        if cached:
            logger.warning("AMO недоступен — отдаём stale кеш для %s..%s", date_from, date_to)
            return cached["data"]
        raise HTTPException(
            status_code=502,
            detail=f"Источники недоступны: AMO не отдал данные за {date_from}..{date_to}",
        )

    # Страховка от boundary-эффектов на стороне AMO
    leads = [
        lead for lead in leads
        if (lead.get("created_at") or 0) >= from_ts_filter
        and (lead.get("created_at") or 0) <= to_ts_filter
    ]

    # Помечаем «База» — лиды без источника, но с историей контакта (без записи в AMO)
    await asyncio.to_thread(_enrich_baza, leads)

    # Восстанавливаем первоисточник копий сделок — AMO теряет метки при копировании
    await asyncio.to_thread(_enrich_smart_copy, leads)

    # Получаем FB spend из аналитического кеша приложения.
    # FB API может тормозить/висеть (throttle) — поэтому жёсткий таймаут.
    # spend/cpl не критичны: лиды из AMO важнее, при таймауте отдаём данные без spend.
    fb_spend = 0.0
    fb_leads_count = 0
    try:
        from web.app import _cached_analytics_async
        ads = await asyncio.wait_for(
            _cached_analytics_async(date_from, date_to),
            timeout=_FB_TIMEOUT,
        )
        for ad in ads:
            fb_spend += float(ad.get("spend") or 0)
            fb_leads_count += int(ad.get("leads") or 0)
    except asyncio.TimeoutError:
        logger.warning("FB analytics таймаут (%sс) — spend/cpl будут 0", _FB_TIMEOUT)
    except Exception as e:
        # FB spend не критичен — лиды из AMO важнее
        logger.warning("FB analytics недоступны, spend/cpl будут 0: %s", e)

    paid_set = set(AMO_PAYMENT_STATUS_IDS)
    qual_set = set(QUAL_STATUS_IDS)

    result = aggregate_by_source(
        leads=leads,
        qual_status_ids=qual_set,
        paid_status_ids=paid_set,
        date_from=date_from,
        date_to=date_to,
        fb_spend=fb_spend,
        fb_leads=fb_leads_count,
    )

    # Добавляем цвета каналов для фронта
    result["colors"] = SOURCE_COLORS

    # Честно помечаем неполный ответ: часть периода AMO не отдал
    result["partial"] = bool(gaps)
    result["gaps"] = gaps

    # Неполные данные в кеш не кладём — иначе дыра залипнет на 15 минут
    if not gaps:
        _cache[cache_key] = {"data": result, "ts": time.time()}
    return result


@router.get("/api/sources")
async def api_sources(days: int = Query(default=7)) -> dict:
    """Источники лидов за последние N дней. days ∈ {7, 14, 30, 90}."""
    if days not in _VALID_DAYS:
        raise HTTPException(status_code=400, detail="days должен быть 7, 14, 30 или 90")

    now = datetime.now(tz=timezone.utc)
    date_to = now.strftime("%Y-%m-%d")
    date_from = (now - timedelta(days=days)).strftime("%Y-%m-%d")

    return await _compute_sources(date_from, date_to)


@router.get("/api/sources/range")
async def api_sources_range(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
) -> dict:
    """Источники лидов за произвольный период [from, to]. Формат YYYY-MM-DD."""
    # Валидация формата дат
    try:
        d_from = datetime.strptime(from_, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный формат даты")
    try:
        d_to = datetime.strptime(to, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный формат даты")

    # from не должен быть позже to
    if d_from > d_to:
        raise HTTPException(status_code=400, detail="from > to")

    # Ограничение: максимум 366 дней
    if (d_to - d_from).days > 365:
        raise HTTPException(status_code=400, detail="Диапазон больше 366 дней")

    return await _compute_sources(from_, to)
