"""Тесты устойчивости дашборда «Источники»: окно дат и частичные сбои AMO.

Два дефекта, найденные при пересчёте прошлого месяца:
  1. `get_leads(days)` считает N дней ОТ СЕГОДНЯ — запрос прошлого месяца, сделанный
     в следующем, терял лиды первых дней (в ответе стояли нули). Нужен запрос по окну [from, to].
  2. Один оборванный батч AMO (RemoteDisconnected) ронял восстановление источника
     ЦЕЛИКОМ: «База» обнулилась, «Неизвестно» выросло в разы. Сбой одного
     батча не должен отменять восстановление для остальных.
"""

from unittest.mock import AsyncMock, patch

import pytest

from web.sources_routes import _cache, _compute_sources, _recover_from_history
from services.sources import classify_lead_source, is_repeat_lead


@pytest.fixture(autouse=True)
def _clear_sources_cache():
    """Дашборд кеширует ответ на 15 минут — между тестами кеш не переносим."""
    _cache.clear()
    yield
    _cache.clear()


def _lead(lead_id, contact_id):
    return {
        "id": lead_id, "name": "Сделка", "source_id": None,
        "custom_fields": [], "tags": [], "contacts": [{"id": contact_id}],
    }


def _raw(lead_id, created_at=1000, tags=None):
    return {
        "id": lead_id, "name": "Сделка", "source_id": None, "created_at": created_at,
        "custom_fields_values": [],
        "_embedded": {"tags": [{"name": t} for t in (tags or [])]},
    }


# --- 1. Окно дат: исторический период не должен терять начало ---

@pytest.mark.asyncio
async def test_history_range_loads_by_window_not_days_from_now():
    """Запрос июля в августе грузится по окну [from, to], а не «N дней назад»."""
    captured = []

    def fake_window(from_ts, to_ts):
        captured.append((from_ts, to_ts))
        return []

    with patch("web.sources_routes.get_leads_window", side_effect=fake_window), \
         patch("web.app._cached_analytics_async", new_callable=AsyncMock, return_value=[]):
        await _compute_sources("2026-07-01", "2026-07-31")

    # Старый путь (N дней от сегодня) не используется — иначе начало периода теряется
    import web.sources_routes as sr
    assert not hasattr(sr, "get_leads"), "вернулась загрузка «N дней от сегодня»"
    assert captured, "лиды должны грузиться по окнам дат"
    # Окна вместе покрывают весь запрошенный период
    assert min(f for f, _ in captured) <= 1782939600, "левая граница позже 01.07.2026"
    assert max(t for _, t in captured) >= 1785531599, "правая граница раньше 31.07.2026"


@pytest.mark.asyncio
async def test_window_covers_full_first_day():
    """Лид, созданный в первый день периода, попадает в выборку (не отсекается)."""
    first_day_lead = {
        "id": 1, "name": "Сделка", "source_id": None, "custom_fields": [], "tags": [],
        "status_id": 1, "price": 0,
        "created_at": 1782946800,  # 01.07.2026, утро
    }
    # Лид отдаётся только тем окном, в границы которого он попадает — как в AMO
    def window_with_first_day(from_ts, to_ts):
        return [first_day_lead] if from_ts <= first_day_lead["created_at"] <= to_ts else []

    with patch("web.sources_routes.get_leads_window", side_effect=window_with_first_day), \
         patch("web.app._cached_analytics_async", new_callable=AsyncMock, return_value=[]):
        result = await _compute_sources("2026-07-01", "2026-07-31")
    assert result["total"]["leads"] == 1, "лид первого дня периода потерялся"


# --- 2. Частичный сбой AMO не должен отменять всё восстановление ---

def test_partial_batch_failure_keeps_other_recoveries():
    """Один упавший батч контактов не лишает источника остальных лидов."""
    leads = [_lead(i, contact_id=100 + i) for i in range(3)]
    calls = {"n": 0}

    def flaky_contacts(contact_ids):
        # Первый вызов рвётся — как RemoteDisconnected на проде
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("Remote end closed connection without response")
        return {c: [c - 100 + 900, c - 100] for c in contact_ids}

    with patch("web.sources_routes.get_contacts_with_leads_batch", side_effect=flaky_contacts), \
         patch("web.sources_routes.get_leads_batch",
               return_value=[_raw(900 + i, tags=["fb_owner"]) for i in range(3)]), \
         patch("web.sources_routes._CONTACT_CHUNK", 1):
        _recover_from_history(leads, fallback="База")

    recovered = [l for l in leads if classify_lead_source(l) == "Facebook Ads"]
    assert len(recovered) == 2, "сбой одного батча не должен ронять восстановление целиком"


def test_total_amo_failure_is_still_safe():
    """Если AMO недоступен полностью — не падаем, лиды остаются как были."""
    leads = [_lead(1, contact_id=100)]
    with patch("web.sources_routes.get_contacts_with_leads_batch",
               side_effect=ConnectionError("AMO down")):
        _recover_from_history(leads, fallback="База")
    assert classify_lead_source(leads[0]) == "Неизвестно"
    assert is_repeat_lead(leads[0]) is False


def test_partial_history_load_failure_keeps_going():
    """Батч догрузки прошлых лидов упал — остальные кандидаты всё равно обработаны."""
    leads = [_lead(i, contact_id=100 + i) for i in range(2)]
    calls = {"n": 0}

    def flaky_leads(ids):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("оборвалось")
        return [_raw(i, tags=["maps"]) for i in ids]

    with patch("web.sources_routes.get_contacts_with_leads_batch",
               return_value={100: [0, 900], 101: [1, 901]}), \
         patch("web.sources_routes.get_leads_batch", side_effect=flaky_leads), \
         patch("web.sources_routes._LEADS_CHUNK", 1):
        _recover_from_history(leads, fallback="База")

    channels = [classify_lead_source(l) for l in leads]
    assert "Каталог-карты" in channels, "часть истории догрузилась — источник должен восстановиться"
