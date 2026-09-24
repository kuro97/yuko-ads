"""Тесты восстановления источника по истории контакта (web/sources_routes._enrich_baza).

Логика: лид без источника → смотрим корневые лиды контакта:
  - есть корень с реальным источником → копируем (станет тот же канал)
  - история есть, источника нет нигде → «База»
  - истории нет → «Неизвестно»
В AMO ничего не пишется — всё в памяти.
"""

from unittest.mock import patch

from web.sources_routes import _enrich_baza
from services.sources import classify_lead_source


def _lead(lead_id, contact_id, name="Сделка #1", tags=None, source_id=None):
    return {
        "id": lead_id, "name": name, "source_id": source_id,
        "custom_fields": [], "tags": list(tags or []),
        "contacts": [{"id": contact_id}],
    }


def _raw(lead_id, created_at, tags=None, source_id=None, name="Сделка"):
    """RAW-лид как из get_leads_batch (_embedded.tags, custom_fields_values)."""
    return {
        "id": lead_id, "name": name, "source_id": source_id, "created_at": created_at,
        "custom_fields_values": [],
        "_embedded": {"tags": [{"name": t} for t in (tags or [])]},
    }


def _patches(contact_leads, roots):
    return (
        patch("web.sources_routes.get_contacts_with_leads_batch", return_value=contact_leads),
        patch("web.sources_routes.get_leads_batch", return_value=roots),
    )


def test_recovers_real_source_from_root():
    """Корень с тегом fb_owner → лид становится Facebook Ads."""
    lead = _lead(1, contact_id=100)
    p1, p2 = _patches({100: [1, 50]}, [_raw(50, 1000, tags=["fb_owner"])])
    with p1, p2:
        _enrich_baza([lead])
    assert classify_lead_source(lead) == "Facebook Ads"


def test_recovers_oldest_source_when_multiple():
    """Из двух корней с источником берётся самый старый (каталог-карты, created раньше)."""
    lead = _lead(1, contact_id=100)
    roots = [_raw(50, 5000, tags=["fb_owner"]), _raw(51, 1000, tags=["maps_lead", "maps"])]
    p1, p2 = _patches({100: [1, 50, 51]}, roots)
    with p1, p2:
        _enrich_baza([lead])
    assert classify_lead_source(lead) == "Каталог-карты"


def test_baza_when_history_but_no_source():
    """История есть, но у корня источника нет → «База»."""
    lead = _lead(1, contact_id=100)
    p1, p2 = _patches({100: [1, 50]}, [_raw(50, 1000, tags=[])])
    with p1, p2:
        _enrich_baza([lead])
    assert classify_lead_source(lead) == "База"


def test_first_touch_stays_unknown():
    """Нет других лидов контакта → остаётся «Неизвестно»."""
    lead = _lead(1, contact_id=100)
    p1, p2 = _patches({100: [1]}, [])
    with p1, p2:
        _enrich_baza([lead])
    assert classify_lead_source(lead) == "Неизвестно"


def test_lead_with_source_not_touched():
    """Лид с готовым источником (Facebook) не трогаем."""
    lead = _lead(1, contact_id=100, tags=[{"name": "fb_owner"}])
    p1, p2 = _patches({100: [1, 50]}, [_raw(50, 1000, tags=["maps"])])
    with p1, p2:
        _enrich_baza([lead])
    assert classify_lead_source(lead) == "Facebook Ads"


def test_api_error_safe():
    """Сбой запроса истории → не падаем, лид остаётся «Неизвестно»."""
    lead = _lead(1, contact_id=100)
    with patch("web.sources_routes.get_contacts_with_leads_batch", side_effect=Exception("AMO down")):
        _enrich_baza([lead])
    assert classify_lead_source(lead) == "Неизвестно"


def test_bot_lead_recovers_original_source():
    """Бот:outbound с историей (корень Facebook) → Facebook Ads (первоисточник)."""
    lead = _lead(1, contact_id=100, tags=[{"name": "бот:outbound"}])
    assert classify_lead_source(lead) == "Бот/Рассылка"  # до восстановления
    p1, p2 = _patches({100: [1, 50]}, [_raw(50, 1000, tags=["fb_owner"])])
    with p1, p2:
        _enrich_baza([lead])
    assert classify_lead_source(lead) == "Facebook Ads"


def test_bot_lead_cold_stays_bot():
    """Бот без истории (холодный) → остаётся «Бот/Рассылка»."""
    lead = _lead(1, contact_id=100, tags=[{"name": "бот:outbound"}])
    p1, p2 = _patches({100: [1]}, [])
    with p1, p2:
        _enrich_baza([lead])
    assert classify_lead_source(lead) == "Бот/Рассылка"


def test_bot_lead_history_no_source_is_baza():
    """Бот с историей, но без реального источника в ней → «База»."""
    lead = _lead(1, contact_id=100, tags=[{"name": "бот:outbound"}])
    # корень — тоже бот, реального источника нет
    p1, p2 = _patches({100: [1, 50]}, [_raw(50, 1000, tags=["бот:стоп"])])
    with p1, p2:
        _enrich_baza([lead])
    assert classify_lead_source(lead) == "База"
