"""Тесты services/sources.py: classify_lead_source + aggregate_by_source.

Лиды в формате get_leads (integrations.amo): custom_fields, tags, source_id.
Без сетевых вызовов — только юнит-логика.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from services.sources import classify_lead_source, aggregate_by_source


# ---------------------------------------------------------------------------
# Фабрика лида (формат get_leads — нормализованный)
# ---------------------------------------------------------------------------

def _make_lead(
    *,
    lead_id: int = 1,
    name: str = "",
    tags: list[str] | None = None,
    utm: dict | None = None,
    source_id: int | None = None,
    status_id: int = 0,
    price: int = 0,
    created_at: int = 1718000000,  # 2024-06-10 UTC+3
    qualified: bool = False,
) -> dict:
    """Создаёт тестовый лид в формате integrations.amo.get_leads."""
    cf = []
    for k, v in (utm or {}).items():
        cf.append({"field_name": k, "values": [{"value": v}]})
    if qualified:
        cf.append({"field_name": "Квалификация пройдена", "values": [{"value": "ДА"}]})
    return {
        "id": lead_id,
        "name": name,
        "status_id": status_id,
        "price": price,
        "created_at": created_at,
        "custom_fields": cf,
        "tags": [{"id": i, "name": t} for i, t in enumerate(tags or [])],
        "source_id": source_id,
    }


# ---------------------------------------------------------------------------
# 1-5: Классификация по UTM
# ---------------------------------------------------------------------------

def test_classify_facebook_by_utm():
    """utm_source='facebook' → 'Facebook Ads'."""
    lead = _make_lead(utm={"utm_source": "facebook"})
    assert classify_lead_source(lead) == "Facebook Ads"


def test_classify_facebook_by_tag_fb_owner():
    """Тег 'fb_owner' (префикс fb_) → 'Facebook Ads'."""
    lead = _make_lead(tags=["fb_owner"])
    assert classify_lead_source(lead) == "Facebook Ads"


def test_classify_instagram_by_utm():
    """utm_source='instagram' → 'Instagram Ads'."""
    lead = _make_lead(utm={"utm_source": "instagram"})
    assert classify_lead_source(lead) == "Instagram Ads"


def test_classify_google_by_utm():
    """utm_source='google' → 'Google Ads'."""
    lead = _make_lead(utm={"utm_source": "google"})
    assert classify_lead_source(lead) == "Google Ads"


def test_classify_tiktok_by_utm():
    """utm_source='tiktok' → 'TikTok'."""
    lead = _make_lead(utm={"utm_source": "tiktok"})
    assert classify_lead_source(lead) == "TikTok"


# ---------------------------------------------------------------------------
# 6-12: Классификация по тегам
# ---------------------------------------------------------------------------

def test_classify_taplink_by_tag():
    """Тег 'taplink_lead' → 'Taplink'."""
    lead = _make_lead(tags=["taplink_lead"])
    assert classify_lead_source(lead) == "Taplink"


def test_classify_tilda_by_tag():
    """Тег 'tilda' → 'Tilda сайт'."""
    lead = _make_lead(tags=["tilda"])
    assert classify_lead_source(lead) == "Tilda сайт"


def test_classify_maps_by_utm():
    """utm_source='maps' → 'Каталог-карты'."""
    lead = _make_lead(utm={"utm_source": "maps"})
    assert classify_lead_source(lead) == "Каталог-карты"


def test_classify_maps_by_name():
    """Имя содержит 'карты' (кириллический маркер) → 'Каталог-карты'."""
    lead = _make_lead(name="Входящий карты CityA")
    assert classify_lead_source(lead) == "Каталог-карты"


# ---------------------------------------------------------------------------
# 10-12: Классификация звонков по имени
# ---------------------------------------------------------------------------

def test_classify_call_by_name_incoming():
    """Имя начинается с 'Входящий ...' → 'Звонок'."""
    lead = _make_lead(name="Входящий +10000000705")
    assert classify_lead_source(lead) == "Звонок"


def test_classify_call_by_name_missed():
    """Имя начинается с 'Пропущенный ...' → 'Звонок'."""
    lead = _make_lead(name="Пропущенный +10000000701")
    assert classify_lead_source(lead) == "Звонок"


def test_classify_call_by_phone_prefix():
    """Имя = номер телефона (одни цифры) → 'Звонок'."""
    lead = _make_lead(name="10000000482")
    assert classify_lead_source(lead) == "Звонок"


def test_classify_call_by_formatted_phone_name():
    """Имя = номер в международном виде с разделителями → 'Звонок' (формат любой страны)."""
    assert classify_lead_source(_make_lead(name="+1 (000) 000-04-82")) == "Звонок"
    assert classify_lead_source(_make_lead(name="+44 20 0000 0482")) == "Звонок"


def test_classify_call_by_plus_phone_inside_name():
    """Номер с «+» внутри имени («Иван +1...») → 'Звонок'."""
    assert classify_lead_source(_make_lead(name="Иван +10000000482")) == "Звонок"


def test_digits_inside_text_are_not_phone():
    """Цифры без «+» внутри текста (id формы, номер сделки) — не телефон."""
    assert classify_lead_source(_make_lead(name="Заявка form1000000001")) != "Звонок"
    assert classify_lead_source(_make_lead(name="Заявка 12345")) != "Звонок"


# ---------------------------------------------------------------------------
# 13-14: Классификация по source_id
# ---------------------------------------------------------------------------

def test_classify_call_by_source_id_telephony():
    """source_id=8851335 (интеграция телефонии) → 'Звонок'."""
    lead = _make_lead(source_id=8851335)
    assert classify_lead_source(lead) == "Звонок"


def test_classify_whatsapp_by_source_id():
    """source_id=25101252 → 'WhatsApp'."""
    lead = _make_lead(source_id=25101252)
    assert classify_lead_source(lead) == "WhatsApp"


# ---------------------------------------------------------------------------
# 15: Бот по тегу
# ---------------------------------------------------------------------------

def test_classify_bot_by_tag():
    """Тег 'бот:outbound' (префикс 'бот:') → 'Бот/Рассылка'."""
    lead = _make_lead(tags=["бот:outbound"])
    assert classify_lead_source(lead) == "Бот/Рассылка"


# ---------------------------------------------------------------------------
# 16: Неизвестно — пустой лид
# ---------------------------------------------------------------------------

def test_classify_unknown_empty_lead():
    """Пустой лид (нет utm/тегов/source_id/имени) → 'Неизвестно'."""
    lead = _make_lead()
    assert classify_lead_source(lead) == "Неизвестно"


# ---------------------------------------------------------------------------
# 17: Приоритет UTM над тегами
# ---------------------------------------------------------------------------

def test_classify_priority_utm_over_tag():
    """UTM=facebook + тег=tilda → UTM побеждает → 'Facebook Ads'."""
    lead = _make_lead(utm={"utm_source": "facebook"}, tags=["tilda"])
    assert classify_lead_source(lead) == "Facebook Ads"


# ---------------------------------------------------------------------------
# Дополнительные тесты классификации (охват каналов из спеки)
# ---------------------------------------------------------------------------

def test_classify_google_by_tag():
    """Тег 'google_search' (префикс google_) → 'Google Ads'."""
    lead = _make_lead(tags=["google_search"])
    assert classify_lead_source(lead) == "Google Ads"


def test_classify_tiktok_by_tag():
    """Тег 'tiktok_promo' содержит 'tiktok' → 'TikTok'."""
    lead = _make_lead(tags=["tiktok_promo"])
    assert classify_lead_source(lead) == "TikTok"


def test_classify_maps_by_tag():
    """Тег 'maps' → 'Каталог-карты'."""
    lead = _make_lead(tags=["maps"])
    assert classify_lead_source(lead) == "Каталог-карты"


def test_classify_whatsapp_by_tag():
    """Тег 'whatsapp' → 'WhatsApp'."""
    lead = _make_lead(tags=["whatsapp"])
    assert classify_lead_source(lead) == "WhatsApp"


def test_classify_bot_by_source_id():
    """source_id=25833115 → 'Бот/Рассылка'."""
    lead = _make_lead(source_id=25833115)
    assert classify_lead_source(lead) == "Бот/Рассылка"


def test_classify_priority_tag_over_source():
    """Тег 'tiktok' + source_id=25101252 (WA) → тег побеждает → 'TikTok'."""
    lead = _make_lead(tags=["tiktok_promo"], source_id=25101252)
    assert classify_lead_source(lead) == "TikTok"


def test_classify_priority_source_over_name():
    """source_id=8851335 (телефония) + name='WhatsApp лид' → source_id побеждает → 'Звонок'."""
    lead = _make_lead(source_id=8851335, name="WhatsApp лид")
    assert classify_lead_source(lead) == "Звонок"


def test_classify_other_with_unknown_tag():
    """Тег 'random_xxx' не матчится ни под один канал → 'Другое' (есть тег, но неизвестный)."""
    lead = _make_lead(tags=["random_xxx"])
    assert classify_lead_source(lead) == "Другое"


# ---------------------------------------------------------------------------
# Дораспознавание «Другого»: Taplink-коннектор, Instagram-direct, FB по имени, Telegram
# ---------------------------------------------------------------------------

def test_classify_taplink_by_source_id():
    """source_id 7720121 (старый Taplink-коннектор) → 'Taplink'."""
    lead = _make_lead(name="Taplink lead #2364", source_id=7720121, tags=["tap_city1"])
    assert classify_lead_source(lead) == "Taplink"


def test_classify_taplink_by_tag_prefix():
    """Тег 'tap_city1' (префикс tap) → 'Taplink'."""
    lead = _make_lead(tags=["tap_city1"])
    assert classify_lead_source(lead) == "Taplink"


def test_classify_instagram_by_tag_instd():
    """Тег 'instd_dir_a' (Instagram direct, префикс instd) → 'Instagram Ads'."""
    lead = _make_lead(name="вопрос в директе", source_id=24053259, tags=["instd_dir_a"])
    assert classify_lead_source(lead) == "Instagram Ads"


def test_classify_facebook_by_name():
    """Имя 'Facebook №...' (FB-лидформа без UTM) → 'Facebook Ads'."""
    lead = _make_lead(name="Facebook №1592491135499401", source_id=24557225)
    assert classify_lead_source(lead) == "Facebook Ads"


def test_classify_telegram_by_group_tag():
    """Тег 'tg_group' (Telegram-группа ОП) → 'Бот/Рассылка'."""
    lead = _make_lead(name="Автолид из группы ОП Онлайн", source_id=21266896, tags=["tg_group"])
    assert classify_lead_source(lead) == "Бот/Рассылка"


def test_classify_referral_channel():
    """Имя 'Рефералка - CityA' → отдельный канал 'Рефералка'."""
    lead = _make_lead(name="Рефералка - CityA", source_id=12615546)
    assert classify_lead_source(lead) == "Рефералка"


def test_classify_referral_by_recommendation_name():
    """Имя с 'рекоменд' → 'Рефералка' (сарафан)."""
    lead = _make_lead(name="Рекомендация (пришла по совету знакомых)")
    assert classify_lead_source(lead) == "Рефералка"


def test_classify_smart_copy_channel():
    """source_id 24557225 (AMO Умное копирование, дубль) → 'Умное копирование'."""
    lead = _make_lead(name="повторный заказ", source_id=24557225)
    assert classify_lead_source(lead) == "Умное копирование"


# Дораспознавание «Другого» (разбор месяца): три source_id покрывали большую
# часть лидов канала, но в классификаторе их не было.

def test_messenger_connector_source_id_is_whatsapp():
    """sid 26109513 — мессенджер-коннектор: большая часть его лидов уже ловится тегом wz."""
    assert classify_lead_source(_make_lead(name="Заявка от (Test)", source_id=26109513)) == "WhatsApp"


def test_site_source_id_is_tilda():
    """sid 18025273 — сайт onlineexample.com (основной поток лидов с сайта)."""
    lead = _make_lead(name="onlineexample.com form1000000001", source_id=18025273)
    assert classify_lead_source(lead) == "Tilda сайт"


def test_referral_source_id_without_name_hint():
    """sid 12615546 — источник «Рефералка», даже если имя не содержит подсказки."""
    assert classify_lead_source(_make_lead(name="Сделка #123", source_id=12615546)) == "Рефералка"


def test_messenger_connector_maps_channel_is_maps_not_whatsapp():
    """Номер мессенджер-коннектора, заведённый в карточке каталога-карт: источник —
    каталог-карты, а не мессенджер.

    Тег может быть записан и кириллицей («Карты») — ловится тоже, иначе такие
    лиды уходили бы в WhatsApp.
    """
    lead = _make_lead(name="Заявка от (Тест)", tags=["WZ (CityA maps)"])
    assert classify_lead_source(lead) == "Каталог-карты"
    lead_cyr = _make_lead(name="Заявка от (Тест)", tags=["WZ (CityA Карты)"])
    assert classify_lead_source(lead_cyr) == "Каталог-карты"


def test_plain_messenger_connector_channel_stays_whatsapp():
    """Номер без указания источника в имени канала остаётся WhatsApp."""
    lead = _make_lead(name="Заявка от (Тест)", tags=["WZ (CityB 10000000071)"])
    assert classify_lead_source(lead) == "WhatsApp"


def test_fb_leadform_via_site_connector_stays_facebook():
    """FB-лидформа, пришедшая через сайтовый коннектор, остаётся Facebook.

    sid сайта — признак слабее имени: «Facebook №...» это реклама, а не сайт.
    """
    lead = _make_lead(name="Facebook №1325734425", source_id=18025273)
    assert classify_lead_source(lead) == "Facebook Ads"


def test_call_via_site_connector_stays_call():
    """Звонок через тот же коннектор тоже не должен стать сайтом."""
    lead = _make_lead(name="Входящий звонок", source_id=18025273)
    assert classify_lead_source(lead) == "Звонок"


def test_bot_tag_still_wins_over_new_source_ids():
    """Тег бота приоритетнее: рассылка по базе не должна уехать в рекламный канал."""
    lead = _make_lead(name="Сделка #1", source_id=18025273, tags=["бот:outbound"])
    assert classify_lead_source(lead) == "Бот/Рассылка"


def test_classify_baza_by_tag():
    """Тег 'база' (повторное обращение из CRM) → 'База'."""
    lead = _make_lead(name="Сделка #123", tags=["база"])
    assert classify_lead_source(lead) == "База"


def test_classify_waba_tag_is_whatsapp():
    """Тег мессенджер-коннектора/WABA 'WZ (...)' → WhatsApp."""
    assert classify_lead_source(_make_lead(name="Заявка от (Test)", tags=["WZ (WABA NEW)"])) == "WhatsApp"


def test_classify_taplink_by_utm():
    """utm_source=taplink_citya → Taplink (не только по тегу)."""
    assert classify_lead_source(_make_lead(utm={"utm_source": "taplink_citya"})) == "Taplink"


def test_classify_call_tracking_tag_is_call():
    """Тег коллтрекинга 'CallTracking'/'Телефония' → Звонок (регистр не важен)."""
    assert classify_lead_source(_make_lead(name="Сделка #1", tags=["CallTracking"])) == "Звонок"
    assert classify_lead_source(_make_lead(name="пометка менеджера", tags=["Телефония"])) == "Звонок"


def test_classify_referral_by_tag():
    """Тег 'Рефералка' (имя дефолтное) → Рефералка."""
    assert classify_lead_source(_make_lead(name="Сделка #31674216", tags=["Рефералка"])) == "Рефералка"


def test_classify_gclid_is_google():
    """Реальный gclid при пустом utm_source → Google Ads (метка потерялась)."""
    lead = _make_lead(name="site.example.com", tags=["tilda"], utm={"gclid": "Cj0KCQjw_vnQ"})
    assert classify_lead_source(lead) == "Google Ads"


def test_classify_gclid_false_not_google():
    """gclid='False' (заглушка) → НЕ Google (остаётся сайт по тегу tilda)."""
    lead = _make_lead(name="x", tags=["tilda"], utm={"gclid": "False"})
    assert classify_lead_source(lead) == "Tilda сайт"


def test_classify_fbclid_is_facebook():
    """Реальный fbclid → Facebook Ads."""
    lead = _make_lead(name="x", utm={"fbclid": "IwAR123abc"})
    assert classify_lead_source(lead) == "Facebook Ads"


def test_classify_baza_lower_priority_than_real_source():
    """Лид с тегом 'база' И тегом 'fb_owner' → 'Facebook Ads' (реальный источник важнее)."""
    lead = _make_lead(tags=["база", "fb_owner"])
    assert classify_lead_source(lead) == "Facebook Ads"


def test_classify_facebook_name_beats_smart_copy():
    """'Facebook №...' с sid 24557225 → 'Facebook Ads' (имя приоритетнее дубля)."""
    lead = _make_lead(name="Facebook №1592491135499401", source_id=24557225)
    assert classify_lead_source(lead) == "Facebook Ads"


def test_classify_tilda_new_site_tag():
    """Тег 'new_site' → 'Tilda сайт' (особый тег для Tilda)."""
    lead = _make_lead(tags=["new_site"])
    assert classify_lead_source(lead) == "Tilda сайт"


def test_classify_facebook_by_tag_fb_cta():
    """Тег 'fb_cta_l2' (префикс fb_) → 'Facebook Ads'."""
    lead = _make_lead(tags=["fb_cta_l2"])
    assert classify_lead_source(lead) == "Facebook Ads"


def test_classify_instagram_by_tag():
    """Тег 'ig_promo' (префикс ig_) → 'Instagram Ads'."""
    lead = _make_lead(tags=["ig_promo"])
    assert classify_lead_source(lead) == "Instagram Ads"


# ---------------------------------------------------------------------------
# Агрегация aggregate_by_source
# ---------------------------------------------------------------------------

def test_aggregate_empty_leads():
    """Пустой список лидов → channels=[], total={0,...}, by_day содержит все даты."""
    result = aggregate_by_source(
        leads=[],
        qual_status_ids=set(),
        paid_status_ids=set(),
        date_from="2026-06-10",
        date_to="2026-06-12",
    )
    assert result["channels"] == []
    assert result["total"]["leads"] == 0
    assert result["total"]["quals"] == 0
    assert result["total"]["sales"] == 0
    assert result["total"]["revenue"] == 0
    # by_day: 3 дня (10, 11, 12)
    assert len(result["by_day"]) == 3


def test_aggregate_groups_by_channel():
    """3 FB + 2 TikTok лида → ровно 2 канала в results."""
    # 2026-06-10 UTC+3 = timestamp 1718046000 (10 июня 2024 00:00 UTC+3)
    ts = 1718046000
    fb_leads = [_make_lead(lead_id=i, utm={"utm_source": "facebook"}, created_at=ts) for i in range(3)]
    tt_leads = [_make_lead(lead_id=i + 10, tags=["tiktok_promo"], created_at=ts) for i in range(2)]
    leads = fb_leads + tt_leads

    result = aggregate_by_source(
        leads=leads,
        qual_status_ids=set(),
        paid_status_ids=set(),
        date_from="2026-06-10",
        date_to="2026-06-10",
    )
    channel_names = {c["name"] for c in result["channels"]}
    assert "Facebook Ads" in channel_names
    assert "TikTok" in channel_names
    assert len(result["channels"]) == 2


def test_aggregate_qual_rate_calculation():
    """10 лидов, 6 квалов → qual_rate=60.0 для канала."""
    ts = 1718046000
    qual_sid = 999
    leads = [
        _make_lead(lead_id=i, utm={"utm_source": "facebook"}, created_at=ts,
                   status_id=qual_sid if i < 6 else 0)
        for i in range(10)
    ]
    result = aggregate_by_source(
        leads=leads,
        qual_status_ids={qual_sid},
        paid_status_ids=set(),
        date_from="2026-06-10",
        date_to="2026-06-10",
    )
    fb = next(c for c in result["channels"] if c["name"] == "Facebook Ads")
    assert fb["leads"] == 10
    assert fb["quals"] == 6
    assert fb["qual_rate"] == 60.0


def test_aggregate_sale_rate_when_zero_quals():
    """5 лидов, 0 квалов → sale_rate=0 (нет деления на ноль)."""
    ts = 1718046000
    leads = [_make_lead(lead_id=i, tags=["whatsapp"], created_at=ts) for i in range(5)]
    result = aggregate_by_source(
        leads=leads,
        qual_status_ids=set(),
        paid_status_ids=set(),
        date_from="2026-06-10",
        date_to="2026-06-10",
    )
    wa = next(c for c in result["channels"] if c["name"] == "WhatsApp")
    assert wa["quals"] == 0
    assert wa["sale_rate"] == 0


def test_aggregate_revenue_only_paid_status():
    """5 лидов price=100000, только 2 в paid_status → revenue=200000."""
    ts = 1718046000
    paid_sid = 777
    leads = [
        _make_lead(lead_id=i, utm={"utm_source": "google"}, created_at=ts,
                   status_id=paid_sid if i < 2 else 0, price=100000)
        for i in range(5)
    ]
    result = aggregate_by_source(
        leads=leads,
        qual_status_ids=set(),
        paid_status_ids={paid_sid},
        date_from="2026-06-10",
        date_to="2026-06-10",
    )
    goog = next(c for c in result["channels"] if c["name"] == "Google Ads")
    assert goog["sales"] == 2
    assert goog["revenue"] == 200000


def test_aggregate_by_day_includes_all_dates():
    """Диапазон 3 дня → by_day содержит ровно 3 элемента."""
    result = aggregate_by_source(
        leads=[],
        qual_status_ids=set(),
        paid_status_ids=set(),
        date_from="2026-06-10",
        date_to="2026-06-12",
    )
    assert len(result["by_day"]) == 3
    dates = [b["date"] for b in result["by_day"]]
    assert "2026-06-10" in dates
    assert "2026-06-11" in dates
    assert "2026-06-12" in dates


def test_aggregate_fb_spend_and_cpl():
    """100 FB лидов, fb_spend=200 → spend=200.0, cpl=2.0."""
    ts = 1718046000
    leads = [_make_lead(lead_id=i, utm={"utm_source": "facebook"}, created_at=ts) for i in range(100)]
    result = aggregate_by_source(
        leads=leads,
        qual_status_ids=set(),
        paid_status_ids=set(),
        date_from="2026-06-10",
        date_to="2026-06-10",
        fb_spend=200.0,
    )
    fb = next(c for c in result["channels"] if c["name"] == "Facebook Ads")
    assert fb["spend"] == 200.0
    assert fb["cpl"] == 2.0


def test_aggregate_non_fb_spend_is_none():
    """WhatsApp лид → spend=None, cpl=None (только FB Ads имеет эти поля)."""
    ts = 1718046000
    leads = [_make_lead(lead_id=1, tags=["whatsapp"], created_at=ts)]
    result = aggregate_by_source(
        leads=leads,
        qual_status_ids=set(),
        paid_status_ids=set(),
        date_from="2026-06-10",
        date_to="2026-06-10",
    )
    wa = next(c for c in result["channels"] if c["name"] == "WhatsApp")
    assert wa["spend"] is None
    assert wa["cpl"] is None


def test_aggregate_total_matches_sum_of_channels():
    """total.leads == сумме leads по всем каналам."""
    ts = 1718046000
    leads = (
        [_make_lead(lead_id=i, utm={"utm_source": "facebook"}, created_at=ts) for i in range(3)]
        + [_make_lead(lead_id=i + 10, tags=["tiktok_promo"], created_at=ts) for i in range(2)]
        + [_make_lead(lead_id=i + 20, tags=["whatsapp"], created_at=ts) for i in range(4)]
    )
    result = aggregate_by_source(
        leads=leads,
        qual_status_ids=set(),
        paid_status_ids=set(),
        date_from="2026-06-10",
        date_to="2026-06-10",
    )
    total_from_channels = sum(c["leads"] for c in result["channels"])
    assert result["total"]["leads"] == total_from_channels
    assert result["total"]["leads"] == 9
