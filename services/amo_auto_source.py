"""
Автокопирование источника лида при ручном создании в AMO CRM.

Когда оператор вручную создаёт лид без UTM-меток, этот сервис находит
самый старый лид того же контакта с источником и копирует
UTM-поля, теги и source_id в новый лид. Защита от петли — тег auto_source_applied.
"""

import logging

from config import AMO_OPERATOR_USER_IDS, AMO_SOURCE_ID_FIELD
from integrations.amo import (
    _amo_patch,
    get_lead,
    get_contact_with_leads,
    get_leads_batch,
)

log = logging.getLogger(__name__)

# Теги-шум: игнорируются при определении "есть ли источник" (lowercase для сравнения)
NOISE_TAGS: frozenset[str] = frozenset({
    "залётный", "залетный",   # ё/е варианты
    "база_оператора",
    "рекомендация",
})

# Тег-маркер защиты от петли
AUTO_SOURCE_TAG: str = "auto_source_applied"

# UTM-поля для копирования
UTM_FIELD_NAMES: list[str] = [
    "utm_source", "utm_medium", "utm_campaign",
    "utm_content", "utm_term", "referer", "gclid", "fbclid",
]


# ─── Публичный API ────────────────────────────────────────────────────────────

def process_lead_created(lead_id: int) -> dict:
    """Главная точка входа. Вызывается из webhook-обработчика.

    Шаги:
    1. Получаем лид с контактами из AMO
    2. Проверяем защиту от петли (тег auto_source_applied)
    3. Проверяем что создан оператором из whitelist
    4. Берём contact_id из первого контакта лида
    5. Ищем корневой лид с источником по тому же контакту
    6. Копируем UTM, теги, source_id в новый лид

    Возвращает:
        {"status": "applied"|"skipped"|"error", "reason": str,
         "lead_id": int, "source_data": dict|None}
    """
    try:
        # Шаг 1: получаем лид с контактами
        lead = get_lead(lead_id, with_params=["contacts"])
        if lead is None:
            log.info("amo auto-source: lead=%s not found", lead_id)
            return _skipped(lead_id, "lead not found")

        # Шаг 2: защита от петли — тег auto_source_applied уже стоит
        tags = _extract_tags(lead)
        if AUTO_SOURCE_TAG in (t.lower() for t in tags):
            log.info("amo auto-source: lead=%s already applied", lead_id)
            return _skipped(lead_id, "already applied")

        # Шаг 3: проверяем whitelist операторов
        if not AMO_OPERATOR_USER_IDS:
            log.warning(
                "amo auto-source: AMO_OPERATOR_USER_IDS пуст — пропускаем lead=%s", lead_id
            )
            return _skipped(lead_id, "operator whitelist empty")

        if not is_operator_created(lead, AMO_OPERATOR_USER_IDS):
            log.info(
                "amo auto-source: lead=%s not operator (created_by=%s)",
                lead_id, lead.get("created_by"),
            )
            return _skipped(lead_id, "not operator")

        # Шаг 4: берём contact_id
        contacts = (lead.get("_embedded") or {}).get("contacts") or []
        if not contacts:
            log.info("amo auto-source: lead=%s no contact", lead_id)
            return _skipped(lead_id, "no contact")

        contact_id = contacts[0].get("id")
        if not contact_id:
            log.info("amo auto-source: lead=%s contact has no id", lead_id)
            return _skipped(lead_id, "no contact")

        # Шаг 5: ищем корневой источник
        source_data = find_root_source(contact_id, exclude_lead_id=lead_id)
        if source_data is None:
            log.info("amo auto-source: lead=%s no source in contact history", lead_id)
            return _skipped(lead_id, "no source in history")

        # Шаг 6: применяем источник
        applied = apply_source_to_lead(lead_id, source_data, current_lead=lead)
        if not applied:
            log.info(
                "amo auto-source: lead=%s nothing to copy (all fields filled)", lead_id
            )
            return _skipped(lead_id, "nothing to copy")

        log.info(
            "amo auto-source: lead=%s applied from root_lead=%s",
            lead_id, source_data.get("root_lead_id"),
        )
        return {
            "status": "applied",
            "reason": "source copied",
            "lead_id": lead_id,
            "source_data": source_data,
        }

    except Exception as exc:
        log.exception("amo auto-source: lead=%s error=%s", lead_id, exc)
        return {
            "status": "error",
            "reason": str(exc),
            "lead_id": lead_id,
            "source_data": None,
        }


def find_root_source(contact_id: int, exclude_lead_id: int) -> dict | None:
    """Находит самый старый лид контакта с источником.

    1. Получаем контакт с привязанными лидами
    2. Исключаем exclude_lead_id
    3. Загружаем все остальные лиды батчем
    4. Сортируем по created_at ASC (самый старый первый)
    5. Берём первый, у которого _has_source() == True

    Возвращает:
        {"utm": {...}, "tags": [...], "source_id": int|None, "root_lead_id": int}
        или None если не нашли.
    """
    # Получаем контакт со списком лидов
    contact = get_contact_with_leads(contact_id)
    if contact is None:
        log.info("amo auto-source: contact=%s not found", contact_id)
        return None

    embedded_leads = (contact.get("_embedded") or {}).get("leads") or []
    # Фильтруем текущий лид и собираем только ID
    other_lead_ids = [
        item["id"] for item in embedded_leads
        if item.get("id") and item["id"] != exclude_lead_id
    ]

    if not other_lead_ids:
        log.info("amo auto-source: contact=%s has no other leads", contact_id)
        return None

    # Загружаем полные данные лидов батчем
    leads = get_leads_batch(other_lead_ids)
    if not leads:
        return None

    # Сортируем по created_at ASC (самый старый первый)
    leads_sorted = sorted(leads, key=lambda l: l.get("created_at") or 0)

    # Берём первый с источником
    for lead in leads_sorted:
        if _has_source(lead):
            utm = _extract_utm_from_raw(lead)
            filtered_tags = _filter_noise_tags(_extract_tags(lead))
            source_id = _extract_source_id(lead)
            return {
                "utm": utm,
                "tags": filtered_tags,
                "source_id": source_id,
                "root_lead_id": lead["id"],
            }

    return None


def apply_source_to_lead(lead_id: int, source_data: dict, current_lead: dict) -> bool:
    """Применяет источник к лиду через PATCH /leads/{id}.

    Логика НЕ-перезаписи:
    - UTM пишем только в пустые поля
    - source_id пишем только если пусто
    - Теги объединяем (добавляем новые, не затираем существующие)
    - Всегда добавляем AUTO_SOURCE_TAG в конце

    Возвращает True если PATCH был отправлен (хоть что-то изменилось).
    """
    # Текущие значения нового лида
    current_utm = _extract_utm_from_raw(current_lead)
    current_tags = _extract_tags(current_lead)
    current_source_id = _extract_source_id(current_lead)

    # Готовим UTM-поля — только те которых ещё нет у лида
    new_custom_fields: list[dict] = []
    for utm_name in UTM_FIELD_NAMES:
        source_value = source_data.get("utm", {}).get(utm_name)
        if not current_utm.get(utm_name) and source_value:
            new_custom_fields.append({
                "field_code": utm_name.upper(),
                "values": [{"value": source_value}],
            })

    # source_id — только если поле задано в конфиге и у лида пустое
    if (
        AMO_SOURCE_ID_FIELD is not None
        and current_source_id is None
        and source_data.get("source_id") is not None
    ):
        new_custom_fields.append({
            "field_id": AMO_SOURCE_ID_FIELD,
            "values": [{"value": source_data["source_id"]}],
        })

    # Теги: объединение без дубликатов (case-insensitive), AUTO_SOURCE_TAG добавляем всегда
    current_tags_lower = {t.lower() for t in current_tags}
    tags_to_add = [
        t for t in source_data.get("tags", [])
        if t.lower() not in current_tags_lower
        and t.lower() != AUTO_SOURCE_TAG.lower()
    ]
    if AUTO_SOURCE_TAG.lower() not in current_tags_lower:
        tags_to_add.append(AUTO_SOURCE_TAG)

    # Если нечего копировать — не делаем PATCH
    if not new_custom_fields and not tags_to_add:
        return False

    # Формируем тело PATCH
    patch_body: dict = {"id": lead_id}

    if new_custom_fields:
        patch_body["custom_fields_values"] = new_custom_fields

    if tags_to_add:
        # AMO при PATCH заменяет теги целиком — передаём все теги (существующие + новые)
        seen_lower: set[str] = set()
        unique_tags: list[dict] = []
        for tag_name in list(current_tags) + tags_to_add:
            if tag_name.lower() not in seen_lower:
                seen_lower.add(tag_name.lower())
                unique_tags.append({"name": tag_name})
        patch_body["_embedded"] = {"tags": unique_tags}

    _amo_patch(f"leads/{lead_id}", patch_body)
    return True


def is_operator_created(lead: dict, operator_ids: set[int]) -> bool:
    """True если lead.created_by входит в whitelist операторов.

    Пустой set → False (защита от копирования при недонастроенном AMO_OPERATOR_USER_IDS).
    """
    if not operator_ids:
        return False
    return lead.get("created_by") in operator_ids


# ─── Внутренние утилиты ───────────────────────────────────────────────────────

def _extract_tags(lead: dict) -> list[str]:
    """Возвращает список имён тегов из lead._embedded.tags или lead.tags.

    Поддерживает оба формата: list[dict{"name": ...}] и list[str].
    """
    embedded = lead.get("_embedded") or {}
    tags_raw = embedded.get("tags") or lead.get("tags") or []
    result = []
    for tag in tags_raw:
        if isinstance(tag, dict):
            name = tag.get("name") or ""
        else:
            name = str(tag)
        if name:
            result.append(name)
    return result


def _filter_noise_tags(tags: list[str]) -> list[str]:
    """Удаляет NOISE_TAGS и AUTO_SOURCE_TAG (case-insensitive), сохраняет оригинальный регистр."""
    blocked = NOISE_TAGS | {AUTO_SOURCE_TAG}
    return [t for t in tags if t.lower() not in blocked]


def _has_source(lead: dict) -> bool:
    """True если у лида есть хотя бы один признак источника:
    - непустой UTM в custom_fields_values
    - заполнен source_id через кастомное поле
    - есть теги после фильтрации шумовых
    """
    # Проверяем UTM-поля
    utm = _extract_utm_from_raw(lead)
    if any(utm.values()):
        return True

    # Проверяем source_id через кастомное поле
    if _extract_source_id(lead) is not None:
        return True

    # Проверяем теги (после фильтра шума)
    tags = _extract_tags(lead)
    if _filter_noise_tags(tags):
        return True

    return False


def _extract_source_id(lead: dict) -> int | None:
    """Извлекает значение source_id из кастомного поля AMO.

    AMO_SOURCE_ID_FIELD — числовой ID поля в AMO (задаётся в env).
    Если не задан — возвращает None.
    """
    if AMO_SOURCE_ID_FIELD is None:
        return None

    for field in lead.get("custom_fields_values") or []:
        if field.get("field_id") == AMO_SOURCE_ID_FIELD:
            values = field.get("values") or []
            if values:
                raw = values[0].get("value")
                if raw is not None:
                    try:
                        return int(raw)
                    except (ValueError, TypeError):
                        return None
    return None


# ─── Приватные вспомогательные функции ───────────────────────────────────────

def _extract_utm_from_raw(lead: dict) -> dict:
    """Извлекает UTM-метки из custom_fields_values RAW лида AMO.

    Матчинг по field_code (UTM_SOURCE, UTM_MEDIUM, ...) и частичному совпадению field_name.
    """
    utm: dict = {}
    for field in lead.get("custom_fields_values") or []:
        code = (field.get("field_code") or "").lower()
        name = (field.get("field_name") or "").lower()
        values = field.get("values") or []
        value = values[0].get("value") if values else None
        if not value:
            continue
        for utm_name in UTM_FIELD_NAMES:
            if code == utm_name or utm_name in name:
                utm[utm_name] = value
                break
    return utm


def _skipped(lead_id: int, reason: str) -> dict:
    """Формирует стандартный ответ 'skipped'."""
    return {
        "status": "skipped",
        "reason": reason,
        "lead_id": lead_id,
        "source_data": None,
    }
