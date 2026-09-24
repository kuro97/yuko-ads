"""Классификация лидов по каналу привлечения и агрегация метрик по источникам.

Приоритет определения источника: UTM > теги > source_id > имя лида > "Другое"/"Неизвестно".
"""

import re
from datetime import datetime, date, timedelta

# --- Константы каналов ---

# Порядок каналов зафиксирован — используется в API-ответе и фронте
CHANNELS_ORDER: list[str] = [
    "Facebook Ads",
    "Instagram Ads",
    "Google Ads",
    "TikTok",
    "Taplink",
    "Tilda сайт",
    "Каталог-карты",
    "Звонок",
    "WhatsApp",
    "Бот/Рассылка",
    "Рефералка",
    "Умное копирование",
    "База",
    "Другое",
    "Неизвестно",
]

# Псевдоним для совместимости с промптом задачи
SOURCE_CHANNELS = CHANNELS_ORDER

# Палитра цветов каналов (для ApexCharts на фронте)
SOURCE_COLORS: dict[str, str] = {
    "Facebook Ads":   "#4267B2",
    "Instagram Ads":  "#E1306C",
    "Google Ads":     "#4285F4",
    "TikTok":         "#000000",
    "Taplink":        "#00C49F",
    "Tilda сайт":     "#FFB300",
    "Каталог-карты":  "#1BB75B",
    "Звонок":         "#8E24AA",
    "WhatsApp":       "#25D366",
    "Бот/Рассылка":   "#FF7043",
    "Рефералка":      "#C0CA33",
    "Умное копирование": "#78909C",
    "База":           "#6D4C41",
    "Другое":         "#9E9E9E",
    "Неизвестно":     "#5C6BC0",
}

# Идентификаторы кастомных полей UTM в AMO CRM
# Используются как fallback — основная логика читает field_name
UTM_FIELD_IDS: dict[str, int] = {
    "utm_source":   0,  # точные ID зависят от аккаунта AMO — читаем по field_name
    "utm_medium":   0,
    "utm_campaign": 0,
    "utm_content":  0,
}

# Source ID из AMO CRM (поле lead["source_id"], НЕ кастомное поле)
TELEPHONY_SOURCE_IDS: set[int] = {8851335}       # Интеграция телефонии → Звонок
WHATSAPP_SOURCE_IDS: set[int] = {                # WhatsApp интеграции AMO
    25101252, 24531403, 21147332, 20343501,
    5147243,                                     # Мессенджер-коннектор / WABA (теги «WZ (...)»)
    26109513,                                    # Мессенджер-коннектор («Заявка от (Имя)»)
}
BOT_SOURCE_IDS: set[int] = {                      # Боты/рассылки/Telegram-группы
    25833115, 24479850,
    21266896,  # Автолиды из Telegram-групп ОП (тег tg_group)
}
TAPLINK_SOURCE_IDS: set[int] = {7720121, 20233312}  # Taplink-коннекторы (sid вместо тега)
SITE_SOURCE_IDS: set[int] = {18025273}           # Формы сайта onlineexample.com
REFERRAL_SOURCE_IDS: set[int] = {12615546}       # Источник «Рефералка» (имена «Рефералка - <город>»)
SMART_COPY_SOURCE_IDS: set[int] = {24557225}     # AMO "Умное копирование" — дубли/реанимация

# Каналы, которые сами по себе означают повторное обращение, а не новое привлечение.
# Копия с восстановленным первоисточником уходит в реальный канал, но остаётся
# повторной — для таких лидов ставится флаг _is_repeat (см. is_repeat_lead).
REPEAT_CHANNELS: set[str] = {"Умное копирование", "База"}

# Каталог-карты (картографические справочники с карточкой компании): маркеры,
# по которым канал узнаётся в UTM, тегах и имени лида (подстрокой, в нижнем
# регистре). Список настраивается под метки своего аккаунта; кириллический
# вариант нужен, если канал мессенджера назван кириллицей («WZ (CityA Карты)»).
MAPS_DIRECTORY_MARKERS: tuple[str, ...] = ("maps", "карты")

# Теги, которыми интеграции коллтрекинга/телефонии помечают сделку (точное
# совпадение, в нижнем регистре). Канал определяется тегом, когда source_id
# интеграции не проставлен. Список настраивается под теги своего аккаунта.
CALL_TRACKING_TAGS: tuple[str, ...] = ("calltracking", "телефония")

# Имя лида = номер телефона (телефония подписывает сделку номером звонящего).
# Формат любой страны: необязательный «+», цифры, пробелы, дефисы, скобки.
# Код страны и национальные префиксы не проверяем — только число цифр (E.164).
_PHONE_NAME_RE = re.compile(r"^\+?[\d\s\-()]+$")
# Номер внутри имени распознаём только в международном виде — с «+»: так
# номер заказа или id формы («form1000000001») за телефон не принимается.
_PLUS_PHONE_RE = re.compile(r"\+\d[\d\s\-()]*\d")
_PHONE_MIN_DIGITS = 10
_PHONE_MAX_DIGITS = 15

# Имя лида-копии, которое AMO ставит по умолчанию: «Сделка #<id оригинала>».
# Единственная связь копии с первоисточником — сами метки при копировании теряются.
# Префикс «Автосделка:» добавляет служебный источник AMO (sid 4637486) — ссылка та же.
_PARENT_NAME_RE = re.compile(r"^\s*(?:Автосделка:\s*)?Сделка\s*#\s*(\d+)\s*$", re.IGNORECASE)

# Статусы квалифицированных лидов
QUAL_STATUS_IDS: set[int] = {
    49310601,  # КВАЛИФИКАЦИЯ ПРОЙДЕНА
    31239894, 34482950, 44446055, 30441545, 30173381,
    54593317, 51568792, 63098758, 67230121, 50403629,
    35509010, 53115039, 142,  # Успешно реализовано
}


# --- Вспомогательные функции ---

def _get_utm(lead: dict) -> dict:
    """Возвращает {utm_source, utm_medium, utm_campaign, utm_content} (lowercase, без None).
    Читает кастомные поля лида по field_name — совместимо с integrations.amo._extract_utm.
    """
    utm: dict[str, str] = {}
    for field in lead.get("custom_fields", []):
        name = (field.get("field_name") or "").lower()
        values = field.get("values", [])
        value = values[0]["value"] if values else None
        if not value:
            continue
        if "utm_source" in name:
            utm["utm_source"] = str(value).lower()
        elif "utm_medium" in name:
            utm["utm_medium"] = str(value).lower()
        elif "utm_campaign" in name:
            utm["utm_campaign"] = str(value).lower()
        elif "utm_content" in name:
            utm["utm_content"] = str(value).lower()
        elif "gclid" in name:
            utm["gclid"] = str(value).lower()
        elif "fbclid" in name:
            utm["fbclid"] = str(value).lower()
    return utm


def _is_real_click_id(value: str | None) -> bool:
    """True если click-id (gclid/fbclid) реальный, а не заглушка ('', 'false', '0')."""
    return bool(value) and value.strip().lower() not in ("false", "0", "none", "-", "")


def _get_tags_lower(lead: dict) -> list[str]:
    """Возвращает список имён тегов в lowercase.
    Читает lead['tags'] в формате [{id, name}, ...].
    """
    return [
        tag["name"].lower()
        for tag in (lead.get("tags") or [])
        if tag.get("name")
    ]


def _get_source_id(lead: dict) -> int | None:
    """Возвращает встроенное поле lead['source_id'] (int) либо None.
    Это НЕ кастомное поле — отдельное поле объекта лида AMO API.
    """
    raw = lead.get("source_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _has_maps_marker(text: str) -> bool:
    """True, если в тексте (в нижнем регистре) есть маркер каталог-карт."""
    return any(marker in text for marker in MAPS_DIRECTORY_MARKERS)


def _phone_digits_ok(text: str) -> bool:
    """Число цифр в тексте укладывается в длину телефонного номера (E.164)."""
    count = sum(ch.isdigit() for ch in text)
    return _PHONE_MIN_DIGITS <= count <= _PHONE_MAX_DIGITS


def _looks_like_phone(name: str) -> bool:
    """Имя лида — телефонный номер (или содержит номер в международном виде с «+»).

    Страно-независимо: «+10000000001», «1 (000) 000-00-01», «Иван +10000000001».
    Не телефон: «form1000000001», «Сделка #123» — цифры без «+» внутри текста.
    """
    stripped = (name or "").strip()
    if not stripped:
        return False
    if _PHONE_NAME_RE.match(stripped) and _phone_digits_ok(stripped):
        return True
    return any(_phone_digits_ok(m.group(0)) for m in _PLUS_PHONE_RE.finditer(stripped))


def parse_parent_lead_id(lead: dict) -> int | None:
    """id оригинальной сделки из имени копии («Сделка #10000001» → 10000001).

    None — если имя осмысленное («повторный заказ») или не содержит числового id:
    такие копии восстанавливаются по истории контакта, а не по имени.
    """
    match = _PARENT_NAME_RE.match(lead.get("name") or "")
    return int(match.group(1)) if match else None


def is_repeat_lead(lead: dict, channel: str | None = None) -> bool:
    """True — повторная продажа по своему клиенту, а не новое привлечение.

    Флаг _is_repeat ставит дашборд при восстановлении первоисточника (копия
    уезжает в реальный канал, но повторной быть не перестаёт). Без флага
    ориентируемся на сам канал.

    Args:
        lead: лид.
        channel: уже вычисленный канал — чтобы не классифицировать дважды.
    """
    if lead.get("_is_repeat"):
        return True
    # Копия сделки остаётся копией, даже если канал определился по тегу
    # (например копия с тегом «бот:outbound» уходит в «Бот/Рассылка»)
    if _get_source_id(lead) in SMART_COPY_SOURCE_IDS:
        return True
    ch = channel if channel is not None else classify_lead_source(lead)
    return ch in REPEAT_CHANNELS


def _is_qualified(lead: dict, qual_status_ids: set[int]) -> bool:
    """True если lead['status_id'] in qual_status_ids ИЛИ кастомное поле 'Квалификация пройдена'=='ДА'.
    Совместимо с integrations.amo._is_qualified, но принимает произвольный набор статусов.
    """
    if lead.get("status_id") in qual_status_ids:
        return True
    # Проверяем кастомное поле квалификации
    for field in lead.get("custom_fields", []):
        if (field.get("field_name") or "") == "Квалификация пройдена":
            values = field.get("values", [])
            value = str(values[0]["value"] if values else "").upper().strip()
            return value == "ДА"
    return False


def _format_day(unix_ts: int | None) -> str:
    """Конвертирует unix-секунды в 'YYYY-MM-DD' в TZ аккаунта CRM (UTC+3).
    0 или None → пустая строка.
    """
    if not unix_ts:
        return ""
    # AMO хранит время в UTC — прибавляем 3 часа (часовой пояс аккаунта CRM)
    dt = datetime.utcfromtimestamp(unix_ts + 3 * 3600)
    return dt.strftime("%Y-%m-%d")


# --- Классификация ---

def classify_lead_source(lead: dict) -> str:
    """Определяет канал привлечения лида.

    Приоритет (первое совпадение побеждает):
      1) UTM (utm_source / utm_medium / utm_campaign)
      2) Теги лида
      3) source_id (телефония=Звонок, WhatsApp IDs, Bot IDs)
      4) Имя лида (для звонков и каталог-карт)
      5) "Другое" — если есть хоть что-то (utm/теги/source_id)
      6) "Неизвестно" — пусто

    Возвращает один из CHANNELS_ORDER.
    """
    # Override канала (ставится дашбордом при восстановлении источника по истории
    # контакта — детерминированно, без подмены меток). Высший приоритет.
    override = lead.get("_channel_override")
    if override:
        return override

    utm = _get_utm(lead)
    tags = _get_tags_lower(lead)
    sid = _get_source_id(lead)
    name = (lead.get("name") or "").lower()

    src = (utm.get("utm_source") or "").lower()
    med = (utm.get("utm_medium") or "").lower()
    camp = (utm.get("utm_campaign") or "").lower()
    utm_blob = " ".join([src, med, camp])

    # --- 1. UTM ---
    if src in {"facebook", "fb"} or "facebook" in med:
        return "Facebook Ads"
    if src in {"instagram", "ig"} or "instagram" in med:
        return "Instagram Ads"
    if src in {"google", "google_search"} or "google" in src:
        return "Google Ads"
    if src == "tiktok" or "tiktok" in utm_blob:
        return "TikTok"
    if _has_maps_marker(utm_blob):
        return "Каталог-карты"
    if "taplink" in src:
        return "Taplink"
    # Click-id как источник: gclid = Google, fbclid = Facebook (utm_source мог потеряться)
    if _is_real_click_id(utm.get("gclid")):
        return "Google Ads"
    if _is_real_click_id(utm.get("fbclid")):
        return "Facebook Ads"

    # --- 2. Теги ---
    if any(t.startswith("fb_") or t == "facebook" for t in tags):
        return "Facebook Ads"
    if any(t.startswith("ig_") or t.startswith("instd") or "instagram" in t or t == "ig" for t in tags):
        return "Instagram Ads"
    if any(t.startswith("google_") or t == "google" for t in tags):
        return "Google Ads"
    if any("tiktok" in t for t in tags):
        return "TikTok"
    if any(t == "taplink_lead" or t.startswith("taplink") or t.startswith("tap") for t in tags):
        return "Taplink"
    if any("tg_group" in t or t.startswith("тг") for t in tags):
        return "Бот/Рассылка"
    if any(t in {"tilda", "new_site"} or t.startswith("tilda") for t in tags):
        return "Tilda сайт"
    # Маркер каталог-карт в теге канала мессенджер-коннектора на номер из карточки
    # каталога («WZ (CityA maps)») — источник каталог-карты, мессенджер лишь способ связи.
    if any(_has_maps_marker(t) for t in tags):
        return "Каталог-карты"
    # WhatsApp: прямой тег ИЛИ мессенджер-коннектор/WABA (теги вида «WZ (...)», «...Waba»)
    if any("whatsapp" in t or "waba" in t or t.startswith("wz ") or t == "wz" for t in tags):
        return "WhatsApp"
    # Коллтрекинг тегами (а не source_id): см. CALL_TRACKING_TAGS
    if any(t in CALL_TRACKING_TAGS for t in tags):
        return "Звонок"
    if any(t.startswith("бот:") or t == "бот" for t in tags):
        return "Бот/Рассылка"
    # Рефералка/рекомендация тегом (не только по имени)
    if any("реферал" in t or "рекоменд" in t for t in tags):
        return "Рефералка"

    # --- 3. source_id ---
    if sid is not None:
        if sid in TELEPHONY_SOURCE_IDS:
            return "Звонок"
        if sid in WHATSAPP_SOURCE_IDS:
            return "WhatsApp"
        if sid in TAPLINK_SOURCE_IDS:
            return "Taplink"
        if sid in BOT_SOURCE_IDS:
            return "Бот/Рассылка"

    # --- 4. Имя лида ---
    if _has_maps_marker(name):
        return "Каталог-карты"
    # Лиды с именем "Facebook №..." — это FB-лидформы без UTM
    if name.startswith("facebook"):
        return "Facebook Ads"
    # Звонок: "Входящий"/"Пропущенный"/"Исходящий" в любом месте имени
    if "входящ" in name or "пропущ" in name or "исходящ" in name:
        return "Звонок"
    # Телефонный номер вместо имени (формат любой страны, см. _looks_like_phone)
    if _looks_like_phone(name):
        return "Звонок"

    # --- 4.5 Слабые признаки: коннектор, через который лид попал в AMO ---
    # Проверяются ПОСЛЕ имени: через сайтовый коннектор приходят и FB-лидформы,
    # и звонки — имя про источник говорит точнее, чем канал доставки.
    if sid is not None:
        if sid in SITE_SOURCE_IDS:
            return "Tilda сайт"
        if sid in REFERRAL_SOURCE_IDS:
            return "Рефералка"

    # --- 5. Внутренние источники (низкий приоритет — после всех реальных каналов) ---
    # Рефералка (сарафан) — по имени лида
    if "реферал" in name or "рекоменд" in name:
        return "Рефералка"
    # AMO "Умное копирование" — дубли/реанимация старых сделок, не настоящий источник
    if sid in SMART_COPY_SOURCE_IDS:
        return "Умное копирование"
    # «База»: контакт уже был в CRM (есть история), но источника нигде нет —
    # повторное обращение из базы, не новый канал. Тег проставляется бэкфиллом/вебхуком.
    if any(t == "база" or t == "base" for t in tags):
        return "База"

    # --- 5. Fallback ---
    if utm.get("utm_source") or tags or sid is not None:
        return "Другое"
    return "Неизвестно"


# --- Агрегация ---

def aggregate_by_source(
    leads: list[dict],
    qual_status_ids: set[int],
    paid_status_ids: set[int],
    date_from: str,
    date_to: str,
    fb_spend: float = 0.0,
    fb_leads: int = 0,
) -> dict:
    """Считает метрики на канал + по дням.

    Args:
        leads: список лидов из integrations.amo.get_leads
               (tags, custom_fields, source_id, status_id, name, price, created_at).
        qual_status_ids: множество status_id квалифицированных лидов.
                         Лид с custom_field 'Квалификация пройдена'='ДА' — тоже квал.
        paid_status_ids: множество status_id оплат (config.AMO_PAYMENT_STATUS_IDS).
        date_from: 'YYYY-MM-DD' — левая граница диапазона by_day (включительно).
        date_to:   'YYYY-MM-DD' — правая граница (включительно).
        fb_spend:  суммарный расход FB Ads за период (USD).
        fb_leads:  суммарное число FB-лидов из FB API (CPL fallback если AMO-лидов нет).

    Returns:
        {
            "from": "YYYY-MM-DD",
            "to": "YYYY-MM-DD",
            "channels": [{"name", "leads", "quals", "qual_rate", "sales",
                          "sale_rate", "revenue", "spend", "cpl",
                          "repeat_leads", "repeat_quals", "repeat_sales",
                          "repeat_revenue"}, ...],
            "total": {"leads", "quals", "sales", "revenue",
                      "new": {...}, "repeat": {...}},
            "by_day": [{"date": "YYYY-MM-DD", "<channel>": int, ...}, ...],
        }
    """
    # Инициализируем счётчики для каждого канала.
    # repeat_* — доля повторных продаж внутри канала (копии сделок и работа по базе):
    # они входят в общие leads/revenue, но новым привлечением не являются.
    channel_stats: dict[str, dict] = {
        ch: {
            "leads": 0, "quals": 0, "sales": 0, "revenue": 0,
            "repeat_leads": 0, "repeat_quals": 0, "repeat_sales": 0, "repeat_revenue": 0,
        }
        for ch in CHANNELS_ORDER
    }

    # Строим список дней диапазона
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    day_count = (d_to - d_from).days + 1
    # bucket_map: "YYYY-MM-DD" -> {channel: count}
    bucket_map: dict[str, dict[str, int]] = {}
    current = d_from
    while current <= d_to:
        bucket_map[current.isoformat()] = {}
        current += timedelta(days=1)

    # Проходим по лидам и накапливаем статистику
    for lead in leads:
        channel = classify_lead_source(lead)
        stats = channel_stats[channel]
        repeat = is_repeat_lead(lead, channel)

        stats["leads"] += 1
        if repeat:
            stats["repeat_leads"] += 1

        # Квалификация: по статусу ИЛИ кастомному полю
        if _is_qualified(lead, qual_status_ids):
            stats["quals"] += 1
            if repeat:
                stats["repeat_quals"] += 1

        # Продажа: только если статус входит в paid_status_ids
        if lead.get("status_id") in paid_status_ids:
            price = int(lead.get("price") or 0)
            stats["sales"] += 1
            stats["revenue"] += price
            if repeat:
                stats["repeat_sales"] += 1
                stats["repeat_revenue"] += price

        # Распределение по дням (UTC+3)
        day_key = _format_day(lead.get("created_at"))
        if day_key and day_key in bucket_map:
            day_bucket = bucket_map[day_key]
            day_bucket[channel] = day_bucket.get(channel, 0) + 1

    # Собираем channels — только каналы с leads > 0, сортировка по убыванию leads
    channels_list = []
    for ch in CHANNELS_ORDER:
        s = channel_stats[ch]
        if s["leads"] == 0:
            continue

        qual_rate = round(s["quals"] / s["leads"] * 100, 1) if s["leads"] > 0 else 0.0
        sale_rate = round(s["sales"] / s["quals"] * 100, 1) if s["quals"] > 0 else 0.0

        # spend и cpl — только для Facebook Ads
        if ch == "Facebook Ads":
            spend = fb_spend
            if s["leads"] > 0:
                cpl = round(fb_spend / s["leads"], 2)
            elif fb_leads > 0:
                # Fallback: если в AMO нет FB-лидов, но FB API вернул их число
                cpl = round(fb_spend / fb_leads, 2)
            else:
                cpl = 0.0
        else:
            spend = None
            cpl = None

        channels_list.append({
            "name":           ch,
            "leads":          s["leads"],
            "quals":          s["quals"],
            "qual_rate":      qual_rate,
            "sales":          s["sales"],
            "sale_rate":      sale_rate,
            "revenue":        s["revenue"],
            "spend":          spend,
            "cpl":            cpl,
            "repeat_leads":   s["repeat_leads"],
            "repeat_quals":   s["repeat_quals"],
            "repeat_sales":   s["repeat_sales"],
            "repeat_revenue": s["repeat_revenue"],
        })

    # Сортируем по убыванию лидов
    channels_list.sort(key=lambda x: x["leads"], reverse=True)

    # Тотал + разрез «новое привлечение / повторные продажи».
    # new = total − repeat, поэтому суммы всегда сходятся.
    metrics = ("leads", "quals", "sales", "revenue")
    total = {m: sum(s[m] for s in channel_stats.values()) for m in metrics}
    total["repeat"] = {
        m: sum(s[f"repeat_{m}"] for s in channel_stats.values()) for m in metrics
    }
    total["new"] = {m: total[m] - total["repeat"][m] for m in metrics}

    # by_day: один объект на каждый день, каналы присутствуют только если были лиды
    by_day = [
        {"date": day_key, **day_channels}
        for day_key, day_channels in sorted(bucket_map.items())
    ]

    return {
        "from":     date_from,
        "to":       date_to,
        "channels": channels_list,
        "total":    total,
        "by_day":   by_day,
    }
