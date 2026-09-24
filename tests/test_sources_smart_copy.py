"""Тесты восстановления первоисточника для «Умного копирования» и признака повторной сделки.

AMO при копировании сделки создаёт новый лид БЕЗ рекламных меток: source_id
перезаписывается на 24557225, теги и UTM пустые. Единственная связь с оригиналом —
имя вида «Сделка #<id оригинала>».

Логика:
  - парсим id оригинала из имени → догружаем оригинал → его канал становится каналом копии
  - имя не парсится («повторный заказ») → фолбэк на историю контакта
  - оригинал сам оказался копией → идём по цепочке вверх
  - источник не нашёлся нигде → остаётся «Умное копирование»
Во всех случаях лид помечается как повторная сделка (не новое привлечение).
"""

from unittest.mock import patch

from web.sources_routes import _enrich_smart_copy
from services.sources import (
    aggregate_by_source,
    classify_lead_source,
    is_repeat_lead,
    parse_parent_lead_id,
)

SMART_COPY_SID = 24557225


def _copy(lead_id, name="Сделка #50", contact_id=None):
    """Лид-копия AMO: source_id «Умное копирование», без тегов и UTM."""
    lead = {
        "id": lead_id, "name": name, "source_id": SMART_COPY_SID,
        "custom_fields": [], "tags": [],
    }
    if contact_id is not None:
        lead["contacts"] = [{"id": contact_id}]
    return lead


def _raw(lead_id, created_at=1000, tags=None, source_id=None, name="Сделка"):
    """RAW-лид как из get_leads_batch (_embedded.tags, custom_fields_values)."""
    return {
        "id": lead_id, "name": name, "source_id": source_id, "created_at": created_at,
        "custom_fields_values": [],
        "_embedded": {"tags": [{"name": t} for t in (tags or [])]},
    }


# --- Парсинг id оригинала из имени ---

def test_parse_parent_id_from_name():
    assert parse_parent_lead_id({"name": "Сделка #10000001"}) == 10000001


def test_parse_parent_id_tolerates_spaces():
    assert parse_parent_lead_id({"name": "  Сделка #10000001  "}) == 10000001


def test_parse_parent_id_handles_avtosdelka_prefix():
    """«Автосделка: Сделка #N» — та же ссылка на оригинал, только с префиксом AMO."""
    assert parse_parent_lead_id({"name": "Автосделка: Сделка #30004241"}) == 30004241


def test_parse_parent_id_ignores_avtosdelka_without_reference():
    assert parse_parent_lead_id({"name": "Автосделка: Заявка от (Анна)"}) is None


def test_parse_parent_id_returns_none_for_named_lead():
    """«повторный заказ» — осмысленное имя, id оригинала в нём нет."""
    assert parse_parent_lead_id({"name": "повторный заказ"}) is None


def test_parse_parent_id_returns_none_for_garbage():
    assert parse_parent_lead_id({"name": "Сделка #abc"}) is None
    assert parse_parent_lead_id({"name": ""}) is None
    assert parse_parent_lead_id({}) is None


# --- Восстановление источника по оригиналу ---

def test_recovers_channel_from_parent_by_name():
    """Оригинал с тегом fb_owner → копия становится Facebook Ads."""
    lead = _copy(99, name="Сделка #50")
    with patch("web.sources_routes.get_leads_batch",
               return_value=[_raw(50, tags=["fb_owner"])]):
        _enrich_smart_copy([lead])
    assert classify_lead_source(lead) == "Facebook Ads"


def test_recovered_copy_still_marked_repeat():
    """Канал восстановлен, но сделка остаётся повторной — не новое привлечение."""
    lead = _copy(99, name="Сделка #50")
    with patch("web.sources_routes.get_leads_batch",
               return_value=[_raw(50, tags=["fb_owner"])]):
        _enrich_smart_copy([lead])
    assert is_repeat_lead(lead) is True


def test_parent_without_source_stays_smart_copy():
    """У оригинала источника нет → копия остаётся «Умным копированием»."""
    lead = _copy(99, name="Сделка #50")
    with patch("web.sources_routes.get_leads_batch",
               return_value=[_raw(50, tags=[])]):
        _enrich_smart_copy([lead])
    assert classify_lead_source(lead) == "Умное копирование"


def test_follows_chain_of_copies():
    """Копия копии: оригинал сам «Умное копирование» → поднимаемся на шаг выше."""
    lead = _copy(99, name="Сделка #50")
    batches = [
        [_raw(50, source_id=SMART_COPY_SID, name="Сделка #10")],  # оригинал — тоже копия
        [_raw(10, tags=["maps"])],                                 # корень цепочки — настоящий источник
    ]
    with patch("web.sources_routes.get_leads_batch", side_effect=batches):
        _enrich_smart_copy([lead])
    assert classify_lead_source(lead) == "Каталог-карты"


def test_falls_back_to_history_when_parent_is_empty():
    """Оригинал найден, но сам без меток → ищем глубже, в прошлых сделках контакта.

    Раньше такая копия сдавалась на первом шаге: путь «по имени» и путь
    «по истории» не были связаны.
    """
    lead = _copy(99, name="Сделка #50", contact_id=100)
    with patch("web.sources_routes.get_leads_batch",
               side_effect=[[_raw(50, tags=[])],                    # оригинал пустой
                            [_raw(900, created_at=10, tags=["fb_owner"])]]), \
         patch("web.sources_routes.get_contacts_with_leads_batch",
               return_value={100: [99, 900]}):
        _enrich_smart_copy([lead])
    assert classify_lead_source(lead) == "Facebook Ads"
    assert is_repeat_lead(lead) is True


def test_falls_back_to_contact_history_when_name_has_no_id():
    """«повторный заказ» — id в имени нет, источник ищем по истории контакта."""
    lead = _copy(99, name="повторный заказ", contact_id=100)
    with patch("web.sources_routes.get_contacts_with_leads_batch",
               return_value={100: [99, 50]}), \
         patch("web.sources_routes.get_leads_batch",
               return_value=[_raw(50, tags=["taplink_lead"])]):
        _enrich_smart_copy([lead])
    assert classify_lead_source(lead) == "Taplink"


def test_named_copy_without_history_stays_smart_copy():
    """Истории нет — остаётся «Умным копированием», но всё ещё повторная."""
    lead = _copy(99, name="повторный заказ", contact_id=100)
    with patch("web.sources_routes.get_contacts_with_leads_batch",
               return_value={100: [99]}), \
         patch("web.sources_routes.get_leads_batch", return_value=[]):
        _enrich_smart_copy([lead])
    assert classify_lead_source(lead) == "Умное копирование"
    assert is_repeat_lead(lead) is True


def test_non_smart_copy_lead_untouched():
    """Обычный лид с источником не трогаем — ни канал, ни признак повторности."""
    lead = {"id": 1, "name": "Сделка #7", "source_id": None,
            "custom_fields": [], "tags": [{"name": "fb_owner"}]}
    with patch("web.sources_routes.get_leads_batch") as mock_batch:
        _enrich_smart_copy([lead])
        mock_batch.assert_not_called()
    assert classify_lead_source(lead) == "Facebook Ads"
    assert is_repeat_lead(lead) is False


def test_amo_error_is_safe():
    """Сбой AMO → не падаем, лид остаётся «Умным копированием»."""
    lead = _copy(99, name="Сделка #50")
    with patch("web.sources_routes.get_leads_batch", side_effect=Exception("AMO down")):
        _enrich_smart_copy([lead])
    assert classify_lead_source(lead) == "Умное копирование"


# --- Признак повторной сделки ---

def test_baza_is_repeat_without_flag():
    """«База» — повторное обращение по определению."""
    lead = {"id": 1, "name": "Сделка", "source_id": None,
            "custom_fields": [], "tags": [{"name": "база"}]}
    assert is_repeat_lead(lead) is True


def test_smart_copy_is_repeat_without_flag():
    assert is_repeat_lead(_copy(1)) is True


def test_copy_with_bot_tag_is_repeat_despite_channel():
    """Копия с тегом «бот:outbound» классифицируется как Бот/Рассылка,
    но повторной быть не перестаёт — иначе она утечёт в новое привлечение."""
    lead = _copy(1, name="повторный заказ")
    lead["tags"] = [{"name": "бот:outbound"}]
    assert classify_lead_source(lead) == "Бот/Рассылка"
    assert is_repeat_lead(lead) is True


def test_fresh_facebook_lead_is_not_repeat():
    lead = {"id": 1, "name": "Сделка", "source_id": None,
            "custom_fields": [], "tags": [{"name": "fb_owner"}]}
    assert is_repeat_lead(lead) is False


# --- Агрегация: повторные отделены от нового привлечения ---

def _agg(leads):
    return aggregate_by_source(
        leads=leads,
        qual_status_ids={100},
        paid_status_ids={200},
        date_from="2026-07-01",
        date_to="2026-07-01",
    )


def _lead_at(lead_id, tags, status_id, price=0, repeat=False):
    lead = {
        "id": lead_id, "name": "Сделка", "source_id": None,
        "custom_fields": [], "tags": [{"name": t} for t in tags],
        "status_id": status_id, "price": price,
        "created_at": 1783900800,  # 2026-07-01 в пределах диапазона
    }
    if repeat:
        lead["_is_repeat"] = True
    return lead


def test_channel_reports_repeat_share():
    """Facebook: 2 лида, один из них повторный с оплатой — обе метрики видны."""
    result = _agg([
        _lead_at(1, ["fb_owner"], status_id=200, price=1000),
        _lead_at(2, ["fb_owner"], status_id=200, price=500, repeat=True),
    ])
    fb = next(c for c in result["channels"] if c["name"] == "Facebook Ads")
    assert fb["leads"] == 2
    assert fb["revenue"] == 1500
    assert fb["repeat_leads"] == 1
    assert fb["repeat_sales"] == 1
    assert fb["repeat_revenue"] == 500


def test_total_splits_new_and_repeat():
    """total разложен на новое привлечение и повторные продажи."""
    result = _agg([
        _lead_at(1, ["fb_owner"], status_id=200, price=1000),
        _lead_at(2, ["fb_owner"], status_id=200, price=500, repeat=True),
        _lead_at(3, ["база"], status_id=200, price=300),
    ])
    total = result["total"]
    assert total["leads"] == 3
    assert total["revenue"] == 1800
    assert total["repeat"]["leads"] == 2
    assert total["repeat"]["revenue"] == 800
    assert total["new"]["leads"] == 1
    assert total["new"]["revenue"] == 1000


def test_new_plus_repeat_equals_total():
    """Инвариант: новое + повторное = всё. Ничего не теряется и не дублируется."""
    result = _agg([
        _lead_at(1, ["fb_owner"], status_id=200, price=1000),
        _lead_at(2, ["taplink_lead"], status_id=100),
        _lead_at(3, ["база"], status_id=200, price=300),
        _lead_at(4, ["fb_owner"], status_id=200, price=500, repeat=True),
    ])
    t = result["total"]
    for metric in ("leads", "quals", "sales", "revenue"):
        assert t["new"][metric] + t["repeat"][metric] == t[metric], metric
