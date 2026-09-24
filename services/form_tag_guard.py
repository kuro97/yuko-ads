"""Тег лид-формы на сделке — дописывает тег той формы, с которой пришёл лид.

Онлайн-лиды FB доезжают до AMO двумя конкурирующими интеграциями, и тег формы
достаётся сделке лотереей: замер показал, что тег конкретной формы
(form_a_tag, form_b_tag) ставится лишь меньшинству сделок с неё. Остальные
получают только общий тег направления (dir_a / dir_b). По адсетам, кампаниям
и дням расхождения нет: один и тот же адсет даёт и помеченные, и непомеченные
лиды — значит дело в том, чья интеграция успела создать сделку, а не в рекламе.

Страж чинит результат на выходе, ему безразлично, кто создал сделку: смотрит
поле fb_form_id (981236) и дописывает тег из FORM_TAGS, если того нет.
Существующие теги сохраняются — короткие dir_a / dir_b и ручные пометки
менеджеров не трогаем (решение владельца). Маркер идемпотентности —
сам тег, отдельный state не нужен.

Исходящие вебхуки AMO в этом аккаунте не доставляются, поэтому серверный
опрос по крону — как у services/repeat_lead_guard.py. Запуск:
scripts/run_form_tag_guard.py.
"""

import logging
import time

from integrations.amo import _amo_get, _amo_patch

log = logging.getLogger(__name__)

FIELD_FB_FORM_ID = 981236  # поле сделки «fb_form_id» — id лид-формы FB

# fb_form_id → тег сделки. Новая форма добавляется одной строкой.
FORM_TAGS: dict[str, str] = {
    "2858269299251592": "form_a_tag",  # лид-форма A направления dir_a
    "1285256302102941": "form_b_tag",  # лид-форма B направления dir_b
}

LEADS_PAGE_LIMIT = 250
LEADS_MAX_PAGES = 20  # окно крона укладывается в 1-2 страницы, это страховка

# Пауза между записями. Бэкфилл это сотни сделок подряд по два запроса на каждую,
# а _amo_patch ретраев не имеет: упёршись в лимит AMO, он просто отдаст ошибку.
WRITE_PAUSE_SEC = 0.2


def _lead_form_id(lead: dict) -> str | None:
    """Значение поля fb_form_id у сделки либо None."""
    for field in lead.get("custom_fields_values") or []:
        if field.get("field_id") != FIELD_FB_FORM_ID:
            continue
        values = field.get("values") or []
        value = values[0].get("value") if values else None
        return str(value).strip() if value else None
    return None


def _lead_tags(lead: dict) -> list[str]:
    """Имена тегов сделки. AMO кладёт их в _embedded.tags, но одиночный GET
    в отдельных ответах отдаёт плоский tags — читаем оба места."""
    raw = ((lead.get("_embedded") or {}).get("tags")) or lead.get("tags") or []
    names = []
    for tag in raw:
        name = tag.get("name") if isinstance(tag, dict) else str(tag)
        if name:
            names.append(name)
    return names


def plan_tag(lead: dict) -> dict:
    """Решение по одной сделке. Чистая функция — вся политика здесь.

    Returns:
        {"decision": "skip", "reason": ...} либо {"decision": "tag", "tag": ...}
    """
    form_id = _lead_form_id(lead)
    if not form_id:
        return {"decision": "skip", "reason": "no_form_field"}
    tag = FORM_TAGS.get(form_id)
    if not tag:
        return {"decision": "skip", "reason": "foreign_form"}
    if tag.lower() in {t.lower() for t in _lead_tags(lead)}:
        return {"decision": "skip", "reason": "already_tagged"}
    return {"decision": "tag", "reason": "form_matched", "tag": tag}


def _fetch_leads_page(params: dict) -> list[dict]:
    """Страница /leads. AMO на пустой выдаче отвечает 204 без тела — это не ошибка."""
    data = _amo_get("leads", params) or {}
    return (data.get("_embedded") or {}).get("leads") or []


def fetch_leads_window(window_minutes: int) -> list[dict]:
    """Сделки, созданные за последние window_minutes — рабочий режим крона."""
    from_ts = int(time.time()) - window_minutes * 60
    leads = []
    for page in range(1, LEADS_MAX_PAGES + 1):
        batch = _fetch_leads_page({
            "filter[created_at][from]": from_ts,
            "limit": LEADS_PAGE_LIMIT,
            "page": page,
        })
        leads += batch
        if len(batch) < LEADS_PAGE_LIMIT:
            break
    else:
        log.warning(
            "Окно %d мин не поместилось в %d страниц — часть сделок не просмотрена",
            window_minutes, LEADS_MAX_PAGES,
        )
    return leads


def fetch_leads_by_form(form_id: str) -> list[dict]:
    """Сделки конкретной формы через поиск AMO — режим бэкфилла.

    filter[custom_fields_values] в этом аккаунте не поддерживается (400),
    поэтому ищем значение через query. Поиск
    отдаёт совпадения по любому полю, поэтому форму всё равно перепроверяет
    plan_tag по полю 981236. Глубина ограничена индексом поиска AMO.
    """
    leads = []
    for page in range(1, LEADS_MAX_PAGES + 1):
        batch = _fetch_leads_page({
            "query": form_id,
            "limit": LEADS_PAGE_LIMIT,
            "page": page,
        })
        leads += batch
        if len(batch) < LEADS_PAGE_LIMIT:
            break
    else:
        log.warning("Форма %s: выдача поиска упёрлась в %d страниц", form_id, LEADS_MAX_PAGES)
    return leads


def collect_candidates(window_minutes: int | None = None, backfill: bool = False) -> list[dict]:
    """Сделки-кандидаты без дублей по id: окно крона либо бэкфилл по формам."""
    leads: list[dict] = []
    if backfill:
        for form_id in FORM_TAGS:
            leads += fetch_leads_by_form(form_id)
    else:
        leads = fetch_leads_window(window_minutes or 60)

    unique: dict[int, dict] = {}
    for lead in leads:
        lead_id = lead.get("id")
        if lead_id:
            unique.setdefault(lead_id, lead)
    return list(unique.values())


def add_tag(lead_id: int, tag: str) -> bool:
    """Дописывает тег сделке, сохраняя существующие. False — если писать нечего.

    Лид перечитывается непосредственно перед записью: AMO заменяет теги PATCH'ем
    целиком, поэтому список должен быть максимально свежим, иначе тег, который
    менеджер поставил минуту назад, будет затёрт. Между этим GET и PATCH окно
    гонки остаётся, но оно уже миллисекундное.
    """
    fresh = _amo_get(f"leads/{lead_id}") or {}
    if not fresh.get("id"):
        log.warning("Сделка %s исчезла между выборкой и записью — пропускаю", lead_id)
        return False

    current = _lead_tags(fresh)
    if tag.lower() in {t.lower() for t in current}:
        return False  # тег появился, пока мы шли сюда — писать нечего

    _amo_patch(f"leads/{lead_id}", {"_embedded": {"tags": [{"name": t} for t in current + [tag]]}})
    return True


def run(
    window_minutes: int = 60,
    backfill: bool = False,
    apply: bool = False,
    limit: int = 100,
) -> dict:
    """Основной проход. Без apply — dry-run: только лог, в AMO ничего не пишем."""
    stats = {"scanned": 0, "candidates": 0, "tagged": 0, "skipped": {}, "errors": 0}

    leads = collect_candidates(window_minutes=window_minutes, backfill=backfill)
    stats["scanned"] = len(leads)

    planned = []
    for lead in leads:
        plan = plan_tag(lead)
        if plan["decision"] == "skip":
            stats["skipped"][plan["reason"]] = stats["skipped"].get(plan["reason"], 0) + 1
            continue
        planned.append((lead["id"], plan["tag"]))

    stats["candidates"] = len(planned)
    if len(planned) > limit:
        log.warning(
            "Кандидатов %d, лимит прогона %d — %d сделок останутся без тега до следующего прогона",
            len(planned), limit, len(planned) - limit,
        )
        planned = planned[:limit]

    for lead_id, tag in planned:
        if not apply:
            log.info("[dry-run] сделка %s: поставил бы тег %s", lead_id, tag)
            continue
        try:
            if add_tag(lead_id, tag):
                stats["tagged"] += 1
                log.info("сделка %s: тег %s поставлен", lead_id, tag)
            else:
                # Тег появился, пока мы шли к записи — это не обычный пропуск,
                # а обгон интеграцией; считаем отдельно, чтобы видеть частоту.
                stats["skipped"]["tagged_meanwhile"] = stats["skipped"].get("tagged_meanwhile", 0) + 1
        except Exception as e:
            log.warning("сделка %s: тег %s не поставлен: %s", lead_id, tag, e)
            stats["errors"] += 1
        if WRITE_PAUSE_SEC:
            time.sleep(WRITE_PAUSE_SEC)

    return stats
