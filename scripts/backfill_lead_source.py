#!/usr/bin/env python3
"""Проставляет в карточку сделки метки её первоисточника.

AMO при «Умном копировании» теряет рекламную атрибуцию: UTM и теги не переносятся,
source_id подменяется на служебный. Дашборд «Источники» восстанавливает это на лету,
но в самой CRM лид остаётся без источника — его не видят ни отдел продаж, ни отчёты,
ни выгрузка конверсий в Google Ads.

Скрипт переносит метки первоисточника в лид: сначала ищет оригинал по имени
(«Сделка #<id>»), если не вышло — по прошлым сделкам контакта.

Запускается кроном: исходящие вебхуки AMO в этом аккаунте не доставляются, поэтому только серверный опрос.

Безопасность:
  - без --apply не отправляется ни один PATCH (режим по умолчанию);
  - лид с собственными метками не трогаем — ручная разметка приоритетнее;
  - тег auto_source_applied защищает от повторной обработки;
  - --limit ограничивает число правок за прогон;
  - сбой на одном лиде не прерывает остальные.

Примеры:
    python scripts/backfill_lead_source.py --days 7
    python scripts/backfill_lead_source.py --days 7 --apply --limit 50
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from integrations.amo import get_lead, get_leads_window  # noqa: E402
from services.amo_auto_source import (  # noqa: E402
    AUTO_SOURCE_TAG,
    apply_source_to_lead,
    find_root_source,
)
from services.sources import (  # noqa: E402
    CALL_TRACKING_TAGS,
    MAPS_DIRECTORY_MARKERS,
    classify_lead_source,
    parse_parent_lead_id,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_source")

# Каналы, означающие «источник не определён» — только их и восстанавливаем
UNRESOLVED = {"Неизвестно", "Другое", "Умное копирование"}

# Каналы настоящего привлечения. Первоисточник засчитывается только если попал сюда:
# «Бот/Рассылка» — наша же реактивация, «База» и «Другое» — не источники.
# Тот же список, что у дашборда (web/sources_routes._REAL_SOURCE_CHANNELS).
REAL_CHANNELS = {
    "Facebook Ads", "Instagram Ads", "Google Ads", "TikTok",
    "Taplink", "Tilda сайт", "Каталог-карты", "Звонок", "WhatsApp", "Рефералка",
}

# Начала тегов, по которым классификатор определяет канал. Всё остальное —
# рабочие пометки менеджеров («тег_заметка_1», «тег_заметка_2»), их не переносим.
# Маркеры каталог-карт и теги коллтрекинга берём из классификатора, чтобы
# списки не разъехались.
SOURCE_TAG_PREFIXES = (
    "fb_", "facebook", "ig_", "instd", "instagram", "google_", "google", "tiktok",
    "taplink", "tap", "tilda", "new_site", *MAPS_DIRECTORY_MARKERS, "whatsapp", "waba", "wz",
    *CALL_TRACKING_TAGS, "реферал", "рекоменд",
)


def _source_tags(tags: list[str]) -> list[str]:
    """Оставляет только метки, участвующие в определении канала."""
    return [t for t in tags if t.lower().startswith(SOURCE_TAG_PREFIXES)]

# Метки, которые переносим с первоисточника
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
            "referer", "gclid", "fbclid")


def _own_utm(lead: dict) -> dict:
    """Рекламные метки самого лида (не пустые)."""
    out = {}
    for field in lead.get("custom_fields") or lead.get("custom_fields_values") or []:
        name = (field.get("field_name") or "").lower()
        if not any(k in name for k in UTM_KEYS):
            continue
        values = field.get("values") or [{}]
        value = values[0].get("value")
        if value not in (None, "", "False"):
            out[name] = value
    return out


def _tags_of(lead: dict) -> list[str]:
    tags = lead.get("tags")
    if tags is None:
        tags = (lead.get("_embedded") or {}).get("tags") or []
    return [t["name"] for t in tags if t.get("name")]


def collect_candidates(leads: list[dict]) -> list[dict]:
    """Лиды, которым не хватает источника и которых мы ещё не трогали."""
    candidates = []
    for lead in leads:
        if _own_utm(lead):
            continue  # своя разметка есть — чужую не навязываем
        if AUTO_SOURCE_TAG.lower() in {t.lower() for t in _tags_of(lead)}:
            continue  # уже обработан
        if classify_lead_source(lead) not in UNRESOLVED:
            continue  # канал определяется тегом/именем — восстанавливать нечего
        candidates.append(lead)
    return candidates


def find_origin(lead: dict) -> dict | None:
    """Метки первоисточника: сначала по имени копии, потом по истории контакта.

    Returns: {"utm": {...}, "tags": [...], "root_lead_id": int} либо None.
    """
    parent_id = parse_parent_lead_id(lead)
    if parent_id and parent_id != lead["id"]:
        origin = get_lead(parent_id)
        if origin:
            # Оригинал засчитывается, только если это настоящий канал привлечения:
            # иначе в карточку уедут рабочие пометки вроде «тег_заметка_1»
            classifiable = {
                "name": origin.get("name", ""),
                "source_id": origin.get("source_id"),
                "custom_fields": origin.get("custom_fields_values") or [],
                "tags": (origin.get("_embedded") or {}).get("tags") or [],
            }
            if classify_lead_source(classifiable) in REAL_CHANNELS:
                utm = _own_utm(origin)
                tags = _source_tags(_tags_of(origin))
                if utm or tags:
                    return {"utm": utm, "tags": tags, "root_lead_id": parent_id}

    contacts = lead.get("contacts") or (lead.get("_embedded") or {}).get("contacts") or []
    if contacts:
        contact_id = contacts[0].get("id") if isinstance(contacts[0], dict) else contacts[0]
        if contact_id:
            root = find_root_source(contact_id, exclude_lead_id=lead["id"])
            if root and _is_real_source(root):
                root["tags"] = _source_tags(root.get("tags") or [])
                if root["utm"] or root["tags"]:
                    return root
    return None


def _is_real_source(source: dict) -> bool:
    """Проверяет найденный источник тем же классификатором, что и дашборд.

    find_root_source отдаёт первый лид контакта с любыми метками — среди них
    попадаются рабочие теги и наши же рассылки, которые каналом привлечения не являются.
    """
    classifiable = {
        "name": "",
        "source_id": source.get("source_id"),
        "custom_fields": [
            {"field_name": k, "values": [{"value": v}]}
            for k, v in (source.get("utm") or {}).items()
        ],
        "tags": [{"name": t} for t in (source.get("tags") or [])],
    }
    return classify_lead_source(classifiable) in REAL_CHANNELS


def run_backfill(leads: list[dict], apply: bool = False, limit: int | None = None) -> dict:
    """Основной проход. Без apply только считает, ничего не записывая."""
    stats = {"candidates": 0, "would_apply": 0, "applied": 0, "no_origin": 0, "errors": 0}
    candidates = collect_candidates(leads)
    stats["candidates"] = len(candidates)

    for lead in candidates:
        if limit is not None and stats["applied"] >= limit and apply:
            log.info("Достигнут лимит %d правок за прогон — останавливаюсь", limit)
            break
        try:
            origin = find_origin(lead)
        except Exception as e:
            log.warning("Лид %s: не удалось найти первоисточник: %s", lead["id"], e)
            stats["errors"] += 1
            continue

        if not origin:
            stats["no_origin"] += 1
            continue

        source = origin.get("utm", {}).get("utm_source") or ",".join(origin.get("tags", [])[:2])
        if not apply:
            stats["would_apply"] += 1
            log.info("[dry-run] лид %s ← источник из %s (%s)",
                     lead["id"], origin.get("root_lead_id"), source or "теги")
            continue

        try:
            if apply_source_to_lead(lead["id"], origin, current_lead=lead):
                stats["applied"] += 1
                log.info("лид %s ← метки из %s (%s)",
                         lead["id"], origin.get("root_lead_id"), source or "теги")
        except Exception as e:
            log.warning("Лид %s: запись не удалась: %s", lead["id"], e)
            stats["errors"] += 1

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Бэкфилл источника лидов в AMO")
    parser.add_argument("--days", type=int, default=7, help="за сколько последних дней (по умолчанию 7)")
    parser.add_argument("--apply", action="store_true", help="реально писать в AMO (иначе dry-run)")
    parser.add_argument("--limit", type=int, default=100, help="максимум правок за прогон")
    args = parser.parse_args()

    now = datetime.now(tz=timezone.utc)
    to_ts = int(now.timestamp())
    from_ts = int((now - timedelta(days=args.days)).timestamp())

    log.info("Загружаю лиды за %d дн. (apply=%s, limit=%s)", args.days, args.apply, args.limit)
    leads = get_leads_window(from_ts, to_ts)
    log.info("Загружено лидов: %d", len(leads))

    stats = run_backfill(leads, apply=args.apply, limit=args.limit)
    log.info(
        "Итог: кандидатов %d | %s %d | без первоисточника %d | ошибок %d",
        stats["candidates"],
        "записано" if args.apply else "записали бы",
        stats["applied"] if args.apply else stats["would_apply"],
        stats["no_origin"],
        stats["errors"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
