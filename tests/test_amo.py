"""Тесты AMO CRM интеграции: классификация, матчинг, расчёт метрик."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from itertools import count

from integrations.amo import (
    classify_lead, match_leads_to_ads, calc_ad_metrics,
    _extract_utm, _extract_fb_fields, _build_fb_lookup,
    get_leads_window,
)

# Генератор уникальных lead_id для тестовых лидов.
# Важно: match_leads_to_ads дедуплицирует по lead_id (фикс двойного счёта),
# поэтому каждый созданный в тесте лид должен иметь СВОЙ id — как в реальной AMO,
# где у каждого лида уникальный идентификатор.
_lead_id_seq = count(1)


# --- classify_lead ---

def _make_classified_lead(status_id, qualified=False):
    """Создаёт лид для тестов classify_lead."""
    fields = []
    if qualified:
        fields.append({"field_name": "Квалификация пройдена", "values": [{"value": "ДА"}]})
    return {"status_id": status_id, "custom_fields": fields, "price": 0}


def test_classify_payment():
    """Статус из списка оплат = оплата."""
    with patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [100, 200, 300]):
        assert classify_lead(_make_classified_lead(100)) == "оплата"
        assert classify_lead(_make_classified_lead(300)) == "оплата"


def test_classify_qual():
    """Поле 'Квалификация пройдена' = ДА → квал."""
    with patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", []):
        assert classify_lead(_make_classified_lead(30, qualified=True)) == "квал"
        assert classify_lead(_make_classified_lead(50, qualified=True)) == "квал"


def test_classify_new():
    """Нет квалификации и не оплата = новый."""
    with patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", []):
        assert classify_lead(_make_classified_lead(30)) == "новый"


# --- _extract_utm ---

def test_extract_utm():
    """UTM-метки из кастомных полей."""
    lead = {
        "custom_fields": [
            {"field_name": "utm_source", "values": [{"value": "facebook"}]},
            {"field_name": "utm_content", "values": [{"value": "6346674498521"}]},
            {"field_name": "utm_campaign", "values": [{"value": "acme"}]},
        ]
    }
    utm = _extract_utm(lead)
    assert utm["utm_source"] == "facebook"
    assert utm["utm_content"] == "6346674498521"
    assert utm["utm_campaign"] == "acme"


def test_extract_utm_empty():
    """Нет UTM-полей — пустой словарь."""
    assert _extract_utm({"custom_fields": []}) == {}
    assert _extract_utm({}) == {}


def test_extract_utm_case_insensitive():
    """Поля UTM нечувствительны к регистру."""
    lead = {
        "custom_fields": [
            {"field_name": "UTM_SOURCE", "values": [{"value": "fb"}]},
        ]
    }
    utm = _extract_utm(lead)
    assert utm["utm_source"] == "fb"


# --- match_leads_to_ads ---

def _make_lead(ad_id, status_id=30, price=0, qualified=False):
    """Создаёт тестовый лид."""
    fields = [
        {"field_name": "utm_content", "values": [{"value": ad_id}]},
    ]
    if qualified:
        fields.append({"field_name": "Квалификация пройдена", "values": [{"value": "ДА"}]})
    return {
        "id": next(_lead_id_seq),
        "name": "Test",
        "status_id": status_id,
        "pipeline_id": 1,
        "price": price,
        "created_at": 1700000000,
        "contacts": [],
        "custom_fields": fields,
    }


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [100])
def test_match_groups_by_ad():
    """Матчинг группирует лиды по fb_ad_name."""
    fb_lookup = {"креатив 1": "ad1", "креатив 2": "ad2"}
    leads = [
        _make_lead_with_fb("Креатив 1", status_id=30),
        _make_lead_with_fb("Креатив 1", status_id=50, qualified=True),
        _make_lead_with_fb("Креатив 2", status_id=100, price=100000),
    ]
    result = match_leads_to_ads(leads, fb_lookup=fb_lookup)
    assert "ad1" in result
    assert "ad2" in result
    assert result["ad1"]["total"] == 2
    assert result["ad1"]["quals"] == 1  # qualified=True
    assert result["ad2"]["payments"] == 1
    assert result["ad2"]["revenue"] == 100000


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [])
def test_match_no_fb_fields():
    """Лиды без FB полей — пропускаются."""
    leads = [{"id": 1, "status_id": 30, "price": 0, "custom_fields": []}]
    result = match_leads_to_ads(leads)
    assert result == {}


# --- calc_ad_metrics ---

@patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0)
def test_calc_metrics(mock_rate):
    """Расчёт ROMI, CPQL, qual_pct. Spend в USD, revenue в LCY. Курс зафиксирован 100 ¤/$."""
    matched = {
        "ad1": {"leads": [], "total": 10, "quals": 4, "payments": 2, "revenue": 60000},
    }
    spends = {"ad1": 300}  # $300 USD
    result = calc_ad_metrics(matched, spends)

    assert result["ad1"]["total_leads"] == 10
    assert result["ad1"]["qual_leads"] == 4
    assert result["ad1"]["qual_pct"] == 40.0
    assert result["ad1"]["cpql"] == 75.0  # $300 / 4
    assert result["ad1"]["payments"] == 2
    assert result["ad1"]["revenue"] == 60000
    assert result["ad1"]["romi"] == 200.0  # 60000 / (300 * 100) * 100


def test_calc_metrics_no_quals():
    """Нет квалов — cpql = None."""
    matched = {"ad1": {"leads": [], "total": 5, "quals": 0, "payments": 0, "revenue": 0}}
    result = calc_ad_metrics(matched, {"ad1": 100})
    assert result["ad1"]["cpql"] is None
    assert result["ad1"]["qual_pct"] == 0


def test_calc_metrics_no_spend():
    """Нет расхода — romi = None."""
    matched = {"ad1": {"leads": [], "total": 3, "quals": 1, "payments": 1, "revenue": 50000}}
    result = calc_ad_metrics(matched, {})
    assert result["ad1"]["romi"] is None


# --- _extract_fb_fields ---

def test_extract_fb_fields():
    """Извлечение FB полей из кастомных полей лида."""
    lead = {
        "custom_fields": [
            {"field_name": "fb_ad_name", "values": [{"value": "CityA | Карточка 1"}]},
            {"field_name": "fb_adset_name", "values": [{"value": "CityA L1"}]},
            {"field_name": "fb_campaign_name", "values": [{"value": "ACME PRODA 2026"}]},
        ]
    }
    fb = _extract_fb_fields(lead)
    assert fb["ad_name"] == "CityA | Карточка 1"
    assert fb["adset_name"] == "CityA L1"
    assert fb["campaign_name"] == "ACME PRODA 2026"


def test_extract_fb_fields_empty():
    """Нет FB полей — пустой словарь."""
    assert _extract_fb_fields({"custom_fields": []}) == {}
    assert _extract_fb_fields({}) == {}


# --- _build_fb_lookup ---

def test_build_fb_lookup():
    """Обратный индекс FB: только имена объявлений — adset/campaign НЕ включаются (фикс бага)."""
    ads = [
        {"id": "111", "name": "CityA | Креатив 1", "campaign_name": "Camp A", "adset_name": "Adset X"},
        {"id": "222", "name": "CityB | Креатив 2", "campaign_name": "Camp B", "adset_name": "Adset Y"},
    ]
    lookup = _build_fb_lookup(ads)
    # Имена объявлений присутствуют
    assert lookup["citya | креатив 1"] == "111"
    assert lookup["cityb | креатив 2"] == "222"
    # adset и campaign НЕ должны быть в lookup (иначе бесплатные лиды адсета приписываются одному объявлению)
    assert "camp a" not in lookup
    assert "adset x" not in lookup
    assert "camp b" not in lookup
    assert "adset y" not in lookup


# --- match_leads_to_ads с fb_lookup ---

def _make_lead_with_fb(fb_ad_name, status_id=30, price=0, qualified=False):
    """Создаёт тестовый лид с fb_ad_name."""
    fields = [
        {"field_name": "fb_ad_name", "values": [{"value": fb_ad_name}]},
    ]
    if qualified:
        fields.append({"field_name": "Квалификация пройдена", "values": [{"value": "ДА"}]})
    return {
        "id": next(_lead_id_seq), "name": "Test", "status_id": status_id,
        "pipeline_id": 1, "price": price, "created_at": 1700000000,
        "contacts": [],
        "custom_fields": fields,
    }


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [100])
def test_match_by_ad_name():
    """Матчинг лида по fb_ad_name → ad_id через lookup."""
    fb_lookup = {"citya | креатив 1": "111"}
    leads = [
        _make_lead_with_fb("CityA | Креатив 1", status_id=50, qualified=True),
        _make_lead_with_fb("CityA | Креатив 1", status_id=100, price=200000),
    ]
    result = match_leads_to_ads(leads, fb_lookup=fb_lookup)
    # Оба лида привязаны к ad_id "111"
    assert "111" in result
    assert result["111"]["total"] == 2
    assert result["111"]["quals"] == 2  # qualified + оплата (тоже квал)
    assert result["111"]["payments"] == 1
    assert result["111"]["revenue"] == 200000


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [])
def test_match_by_adset_name_no_longer_works():
    """Матчинг по fb_adset_name БОЛЬШЕ НЕ работает — лид без точного fb_ad_name пропускается (фикс бага)."""
    fb_lookup = {"citya l1": "333"}
    lead = {
        "id": 1, "name": "Test", "status_id": 30,
        "pipeline_id": 1, "price": 0, "created_at": 1700000000,
        "contacts": [],
        "custom_fields": [
            {"field_name": "fb_adset_name", "values": [{"value": "CityA L1"}]},
        ],
    }
    result = match_leads_to_ads([lead], fb_lookup=fb_lookup)
    # Лид без fb_ad_name не должен приписываться никуда
    assert result == {}


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [100])
def test_match_no_fb_fields_skipped():
    """Лиды без FB полей пропускаются (даже с utm_content)."""
    leads = [_make_lead("direct_ad_id", status_id=30)]
    result = match_leads_to_ads(leads, fb_lookup={})
    assert result == {}


# --- Тесты на фикс бага: выручка не должна приписываться через фоллбэк адсета ---

def _make_payment_lead(fb_ad_name=None, fb_adset_name=None, fb_campaign_name=None, status_id=143, price=500000):
    """Создаёт лид-оплату с указанными FB полями."""
    fields = []
    if fb_ad_name:
        fields.append({"field_name": "fb_ad_name", "values": [{"value": fb_ad_name}]})
    if fb_adset_name:
        fields.append({"field_name": "fb_adset_name", "values": [{"value": fb_adset_name}]})
    if fb_campaign_name:
        fields.append({"field_name": "fb_campaign_name", "values": [{"value": fb_campaign_name}]})
    return {
        "id": 42, "name": "Test payment", "status_id": status_id,
        "pipeline_id": 1, "price": price, "created_at": 1700000000,
        "contacts": [], "custom_fields": fields,
    }


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])
def test_payment_not_attributed_via_adset_fallback():
    """Воспроизводит баг: оплата с чужим fb_ad_name НЕ должна приписываться к AD_TEMA_A через адсет.

    Было: лид fb_ad_name='CityD | Промо' → не совпал → фоллбэк по adset → приписан к AD_TEMA_A.
    Стало: лид без совпадения по fb_ad_name → пропускается.
    """
    ads = [{"id": "AD_TEMA_A", "name": "CityD | Тема А / 2", "adset_name": "Adset CityD", "campaign_name": "Camp SO"}]
    lookup = _build_fb_lookup(ads)

    # Лид-оплата с fb_ad_name другого объявления того же адсета
    lead = _make_payment_lead(
        fb_ad_name="CityD | Промо",
        fb_adset_name="Adset CityD",
        price=1000000,
    )
    matched = match_leads_to_ads([lead], fb_lookup=lookup)

    # AD_TEMA_A не должен получить эту оплату — ни revenue, ни payments
    if "AD_TEMA_A" in matched:
        assert matched["AD_TEMA_A"]["payments"] == 0
        assert matched["AD_TEMA_A"]["revenue"] == 0


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])
def test_payment_attributed_by_exact_ad_name():
    """Позитив: оплата с точным fb_ad_name корректно приписывается к объявлению."""
    ads = [{"id": "AD_TEMA_A", "name": "CityD | Тема А / 2", "adset_name": "Adset CityD", "campaign_name": "Camp SO"}]
    lookup = _build_fb_lookup(ads)

    lead = _make_payment_lead(
        fb_ad_name="CityD | Тема А / 2",
        fb_adset_name="Adset CityD",
        price=500000,
    )
    matched = match_leads_to_ads([lead], fb_lookup=lookup)

    assert "AD_TEMA_A" in matched
    assert matched["AD_TEMA_A"]["payments"] == 1
    assert matched["AD_TEMA_A"]["revenue"] == 500000


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])
def test_lead_unmatched_ad_name_dropped():
    """Лид с fb_ad_name которого нет среди объявлений — не приписывается никуда."""
    ads = [{"id": "AD_TEMA_A", "name": "CityD | Тема А / 2"}]
    lookup = _build_fb_lookup(ads)

    lead = _make_payment_lead(
        fb_ad_name="Совсем другое объявление",
        fb_adset_name="Adset CityD",
        price=999999,
    )
    matched = match_leads_to_ads([lead], fb_lookup=lookup)

    # Лид должен быть полностью проигнорирован
    assert "AD_TEMA_A" not in matched
    assert matched == {}


# --- Дедуп атрибуции: один lead_id (= один платёж) считается РОВНО ОДИН раз ---

@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])
def test_match_dedup_same_lead_id_counted_once():
    """Фикс двойного счёта: если один и тот же lead_id пришёл дважды
    (например из-за overlap окон на границе created_at) — его платёж
    засчитывается ОДИН раз, revenue не удваивается.
    """
    ads = [{"id": "AD1", "name": "CityE | Клиент Игорь"}]
    lookup = _build_fb_lookup(ads)

    # Один и тот же лид-оплата 500000, продублированный в списке
    dup_lead = {
        "id": 36246120, "name": "Сделка", "status_id": 143,
        "pipeline_id": 1, "price": 500000, "created_at": 1781153976,
        "contacts": [{"id": 41818335}],
        "custom_fields": [
            {"field_name": "fb_ad_name", "values": [{"value": "CityE | Клиент Игорь"}]},
        ],
    }
    matched = match_leads_to_ads([dup_lead, dict(dup_lead)], fb_lookup=lookup)

    assert "AD1" in matched
    # Платёж учтён один раз: payments=1, revenue=500000 (не 2 и не 1000000)
    assert matched["AD1"]["payments"] == 1
    assert matched["AD1"]["revenue"] == 500000
    assert matched["AD1"]["total"] == 1


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])
def test_match_payment_not_smeared_across_ads():
    """Платёж конкретного лида привязан к ЕГО объявлению и не размазывается
    на другое объявление, даже если суммы платежей совпадают (один тариф).

    Пример: два РАЗНЫХ контакта заплатили одну
    сумму на разные объявления — каждое объявление получает свой платёж один раз,
    а не суммарную выручку обоих.
    """
    ads = [
        {"id": "AD_IGOR", "name": "CityE | Клиент Игорь"},
        {"id": "AD_TEMA_B", "name": "CityE | Тема Б"},
    ]
    lookup = _build_fb_lookup(ads)

    lead_igor = {
        "id": 36246120, "name": "Сделка1", "status_id": 143,
        "pipeline_id": 1, "price": 500000, "created_at": 1781153976,
        "contacts": [{"id": 41818335}],
        "custom_fields": [
            {"field_name": "fb_ad_name", "values": [{"value": "CityE | Клиент Игорь"}]},
        ],
    }
    lead_tema_b = {
        "id": 38040363, "name": "Сделка2", "status_id": 143,
        "pipeline_id": 1, "price": 500000, "created_at": 1781759358,
        "contacts": [{"id": 47720205}],
        "custom_fields": [
            {"field_name": "fb_ad_name", "values": [{"value": "CityE | Тема Б"}]},
        ],
    }
    matched = match_leads_to_ads([lead_igor, lead_tema_b], fb_lookup=lookup)

    # Каждое объявление получает РОВНО свой платёж 500000 один раз
    assert matched["AD_IGOR"]["revenue"] == 500000
    assert matched["AD_IGOR"]["payments"] == 1
    assert matched["AD_TEMA_B"]["revenue"] == 500000
    assert matched["AD_TEMA_B"]["payments"] == 1


# --- get_leads_window ---

def _make_raw_amo_lead(lead_id: int, created_at: int, fb_ad_name: str = "Test Ad") -> dict:
    """Сырой лид как его отдаёт AMO API (custom_fields_values, _embedded)."""
    return {
        "id": lead_id,
        "name": f"Lead {lead_id}",
        "status_id": 30,
        "pipeline_id": 1,
        "price": 0,
        "created_at": created_at,
        "created_by": None,
        "responsible_user_id": None,
        "custom_fields_values": [
            {"field_name": "fb_ad_name", "values": [{"value": fb_ad_name}]},
        ],
        "_embedded": {"contacts": [], "tags": []},
    }


@patch("integrations.amo.AMO_PIPELINE_ID", "")
@patch("integrations.amo._amo_get")
def test_get_leads_window_basic(mock_amo_get):
    """get_leads_window возвращает лиды из одного запроса (меньше 250 — последняя страница)."""
    raw_leads = [_make_raw_amo_lead(1, 1700000100), _make_raw_amo_lead(2, 1700000200)]
    mock_amo_get.return_value = {"_embedded": {"leads": raw_leads}}

    result = get_leads_window(from_ts=1700000000, to_ts=1700086400)

    assert len(result) == 2
    assert result[0]["id"] == 1
    assert result[1]["id"] == 2
    # Убеждаемся что поля нормализованы как в get_leads
    assert "custom_fields" in result[0]
    assert "contacts" in result[0]


@patch("integrations.amo.AMO_PIPELINE_ID", "")
@patch("integrations.amo._amo_get")
def test_get_leads_window_passes_timestamps(mock_amo_get):
    """get_leads_window передаёт from/to в параметрах запроса к AMO API."""
    mock_amo_get.return_value = {"_embedded": {"leads": []}}

    get_leads_window(from_ts=1700000000, to_ts=1700086400)

    call_params = mock_amo_get.call_args[0][1]
    assert call_params["filter[created_at][from]"] == 1700000000
    assert call_params["filter[created_at][to]"] == 1700086400


@patch("integrations.amo.AMO_PIPELINE_ID", "99")
@patch("integrations.amo._amo_get")
def test_get_leads_window_filters_by_pipeline(mock_amo_get):
    """get_leads_window добавляет filter[pipeline_id] если AMO_PIPELINE_ID задан."""
    mock_amo_get.return_value = {"_embedded": {"leads": []}}

    get_leads_window(from_ts=1700000000, to_ts=1700086400)

    call_params = mock_amo_get.call_args[0][1]
    assert call_params["filter[pipeline_id]"] == "99"


@patch("integrations.amo.AMO_PIPELINE_ID", "")
@patch("integrations.amo._amo_get")
def test_get_leads_window_no_pipeline_no_filter(mock_amo_get):
    """get_leads_window НЕ добавляет filter[pipeline_id] если AMO_PIPELINE_ID пустой."""
    mock_amo_get.return_value = {"_embedded": {"leads": []}}

    get_leads_window(from_ts=1700000000, to_ts=1700086400)

    call_params = mock_amo_get.call_args[0][1]
    assert "filter[pipeline_id]" not in call_params


@patch("integrations.amo.AMO_PIPELINE_ID", "")
@patch("integrations.amo._amo_get")
def test_get_leads_window_pagination(mock_amo_get):
    """get_leads_window пагинирует: запрашивает следующую страницу если вернулось 250 лидов."""
    # Первая страница: 250 лидов (полная)
    page1 = [_make_raw_amo_lead(i, 1700000000 + i) for i in range(250)]
    # Вторая страница: 3 лида (конец)
    page2 = [_make_raw_amo_lead(i + 250, 1700000000 + i + 250) for i in range(3)]

    mock_amo_get.side_effect = [
        {"_embedded": {"leads": page1}},
        {"_embedded": {"leads": page2}},
    ]

    result = get_leads_window(from_ts=1700000000, to_ts=1700086400)

    assert len(result) == 253
    assert mock_amo_get.call_count == 2
    # Вторая страница передаётся с page=2
    second_call_params = mock_amo_get.call_args_list[1][0][1]
    assert second_call_params["page"] == 2


@patch("integrations.amo.AMO_PIPELINE_ID", "")
@patch("integrations.amo._amo_get")
def test_get_leads_window_empty_result(mock_amo_get):
    """get_leads_window возвращает пустой список если AMO не нашла лидов в окне."""
    mock_amo_get.return_value = {"_embedded": {"leads": []}}

    result = get_leads_window(from_ts=1700000000, to_ts=1700086400)

    assert result == []
    assert mock_amo_get.call_count == 1


@patch("integrations.amo.AMO_PIPELINE_ID", "")
@patch("integrations.amo._amo_get")
def test_get_leads_window_same_format_as_get_leads(mock_amo_get):
    """get_leads_window возвращает те же поля что и get_leads (совместимость форматов)."""
    raw_lead = _make_raw_amo_lead(42, 1700000100, fb_ad_name="CityA | CR001")
    mock_amo_get.return_value = {"_embedded": {"leads": [raw_lead]}}

    result = get_leads_window(from_ts=1700000000, to_ts=1700086400)

    assert len(result) == 1
    lead = result[0]
    # Проверяем все поля которые возвращает get_leads
    expected_keys = {"id", "name", "status_id", "pipeline_id", "price",
                     "created_at", "contacts", "custom_fields",
                     "created_by", "responsible_user_id", "tags"}
    assert expected_keys.issubset(set(lead.keys()))
    # custom_fields_values должен быть преобразован в custom_fields
    assert lead["custom_fields"][0]["field_name"] == "fb_ad_name"


# ---------------------------------------------------------------------------
# get_latest_fb_lead_ts — пагинация и стоп по возрасту
# ---------------------------------------------------------------------------

def _make_raw_lead_for_watchdog(created_at: int, with_fb: bool = False) -> dict:
    """Сырой лид как отдаёт AMO API (для get_latest_fb_lead_ts)."""
    from config import AMO_FB_LEAD_ID_FIELD
    fields = []
    if with_fb:
        # fb_lead_id — числовой ID лида из Facebook (15-17 цифр)
        fields.append({"field_name": AMO_FB_LEAD_ID_FIELD, "values": [{"value": "123456789012345"}]})
    return {
        "id": 1,
        "created_at": created_at,
        "custom_fields_values": fields,
    }


@patch("integrations.amo.AMO_PIPELINE_ID", "")
@patch("integrations.amo.time")
@patch("integrations.amo._amo_get")
def test_get_latest_fb_lead_ts_paginates_to_second_page(mock_amo_get, mock_time):
    """Если на первой странице нет FB-лида — запрашивает вторую страницу."""
    now_ts = int(time.time())
    # Первая страница: 250 лидов без FB, все свежие
    page1 = [_make_raw_lead_for_watchdog(now_ts - 60, with_fb=False)] * 250
    # Вторая страница: один лид с fb_lead_id
    fb_ts = now_ts - 120
    page2 = [_make_raw_lead_for_watchdog(fb_ts, with_fb=True)]

    mock_amo_get.side_effect = [
        {"_embedded": {"leads": page1}},
        {"_embedded": {"leads": page2}},
    ]

    from integrations.amo import get_latest_fb_lead_ts
    result = get_latest_fb_lead_ts()

    assert result == fb_ts
    assert mock_amo_get.call_count == 2
    # Троттлинг: time.sleep вызван один раз (перед 2-й страницей)
    mock_time.sleep.assert_called_once_with(0.2)


@patch("integrations.amo.AMO_PIPELINE_ID", "")
@patch("integrations.amo.time")
@patch("integrations.amo._amo_get")
def test_get_latest_fb_lead_ts_stops_at_48h_boundary(mock_amo_get, mock_time):
    """Если на странице есть лид старее 48 ч — стоп, возвращает None."""
    now_ts = int(time.time())
    cutoff = now_ts - 48 * 3600

    # Первая страница: 250 лидов без FB.
    # AMO отдаёт sorted desc → свежие впереди, старый лид ПОСЛЕДНИМ (индекс 249).
    old_lead = _make_raw_lead_for_watchdog(cutoff - 3600, with_fb=False)  # старее 48ч
    page1 = [_make_raw_lead_for_watchdog(now_ts - 60, with_fb=False)] * 249 + [old_lead]

    mock_amo_get.return_value = {"_embedded": {"leads": page1}}

    from integrations.amo import get_latest_fb_lead_ts
    result = get_latest_fb_lead_ts()

    assert result is None
    # Должна остановиться на первой странице (не пошла дальше)
    assert mock_amo_get.call_count == 1


# ---------------------------------------------------------------------------
# Дедуп по контакту (Фаза 1): один contact_id — один revenue
# ---------------------------------------------------------------------------


def _make_payment_lead_with_contact(
    fb_ad_name: str,
    contact_id,
    price: int,
    created_at: int,
    lead_id: int,
    status_id: int = 143,
) -> dict:
    """Создаёт лид-оплату с contact_id для тестов дедупа по контакту."""
    contacts = [{"id": contact_id}] if contact_id is not None else []
    return {
        "id": lead_id,
        "name": "Тест сделка",
        "status_id": status_id,
        "pipeline_id": 1,
        "price": price,
        "created_at": created_at,
        "contacts": contacts,
        "custom_fields": [
            {"field_name": "fb_ad_name", "values": [{"value": fb_ad_name}]},
        ],
    }


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])
def test_contact_dedup_one_contact_two_deals_one_revenue():
    """Один контакт с двумя оплаченными сделками на РАЗНЫЕ объявления.
    Revenue засчитан ТОЛЬКО ОДИН РАЗ (у первой по created_at сделки).
    Вторая сделка того же контакта — revenue=0, payments=0.
    """
    ads = [
        {"id": "AD_IGOR", "name": "CityE | Клиент Игорь"},
        {"id": "AD_TEMA_B",   "name": "CityE | Тема Б / Отзыв"},
    ]
    lookup = _build_fb_lookup(ads)

    # Один контакт (contact_id=41818335) с двумя сделками:
    # - первая (created_at меньше) на AD_IGOR
    # - вторая (created_at больше) на AD_TEMA_B
    lead_first = _make_payment_lead_with_contact(
        fb_ad_name="CityE | Клиент Игорь",
        contact_id=41818335,
        price=500000,
        created_at=1781000000,
        lead_id=1001,
    )
    lead_second = _make_payment_lead_with_contact(
        fb_ad_name="CityE | Тема Б / Отзыв",
        contact_id=41818335,  # тот же контакт!
        price=500000,
        created_at=1781100000,
        lead_id=1002,
    )

    matched = match_leads_to_ads([lead_first, lead_second], fb_lookup=lookup)

    # Первое объявление (AD_IGOR) получает revenue, т.к. его сделка раньше
    assert "AD_IGOR" in matched
    assert matched["AD_IGOR"]["payments"] == 1
    assert matched["AD_IGOR"]["revenue"] == 500000

    # Второе объявление (AD_TEMA_B) — лид добавляется, но revenue/payments не засчитаны
    assert "AD_TEMA_B" in matched
    assert matched["AD_TEMA_B"]["total"] == 1       # лид попал в группу
    assert matched["AD_TEMA_B"]["payments"] == 0    # но revenue не засчитан
    assert matched["AD_TEMA_B"]["revenue"] == 0


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])
def test_contact_dedup_no_contact_id_falls_back():
    """Лид без contact_id (contacts=[]) — старое поведение: revenue засчитывается.
    Не должен падать.
    """
    ads = [{"id": "AD1", "name": "Тест Ad"}]
    lookup = _build_fb_lookup(ads)

    lead = _make_payment_lead_with_contact(
        fb_ad_name="Тест Ad",
        contact_id=None,  # нет контакта
        price=500000,
        created_at=1781000000,
        lead_id=2001,
    )

    matched = match_leads_to_ads([lead], fb_lookup=lookup)

    assert "AD1" in matched
    assert matched["AD1"]["payments"] == 1
    assert matched["AD1"]["revenue"] == 500000


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])
def test_contact_dedup_different_contacts_sum_up():
    """Разные контакты с оплатами на одно объявление — суммируются (не режутся).
    Дедуп только внутри одного контакта, не между разными.
    """
    ads = [{"id": "AD1", "name": "CityE | L2"}]
    lookup = _build_fb_lookup(ads)

    lead_a = _make_payment_lead_with_contact(
        fb_ad_name="CityE | L2", contact_id=111, price=500000, created_at=1781000000, lead_id=3001
    )
    lead_b = _make_payment_lead_with_contact(
        fb_ad_name="CityE | L2", contact_id=222, price=500000, created_at=1781100000, lead_id=3002
    )
    lead_c = _make_payment_lead_with_contact(
        fb_ad_name="CityE | L2", contact_id=333, price=500000, created_at=1781200000, lead_id=3003
    )

    matched = match_leads_to_ads([lead_a, lead_b, lead_c], fb_lookup=lookup)

    assert "AD1" in matched
    assert matched["AD1"]["payments"] == 3
    assert matched["AD1"]["revenue"] == 500000 * 3


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])
def test_contact_dedup_qual_counts_not_affected():
    """Дедуп по контакту касается только revenue/payments.
    quals и total считаются независимо — один контакт может дать несколько квалов.
    """
    ads = [
        {"id": "AD1", "name": "Реклама 1"},
        {"id": "AD2", "name": "Реклама 2"},
    ]
    lookup = _build_fb_lookup(ads)

    # Один контакт: первая сделка на AD1 (оплата), вторая на AD2 (оплата)
    lead1 = _make_payment_lead_with_contact(
        fb_ad_name="Реклама 1", contact_id=777, price=500000, created_at=1781000000, lead_id=4001
    )
    lead2 = _make_payment_lead_with_contact(
        fb_ad_name="Реклама 2", contact_id=777, price=500000, created_at=1781100000, lead_id=4002
    )

    matched = match_leads_to_ads([lead1, lead2], fb_lookup=lookup)

    # total на каждом объявлении = 1 (оба лида попали в группы)
    assert matched["AD1"]["total"] == 1
    assert matched["AD2"]["total"] == 1

    # quals: оба лида статус оплата → оба квал
    assert matched["AD1"]["quals"] == 1
    assert matched["AD2"]["quals"] == 1

    # revenue: только первый контакт (AD1), второй заблокирован дедупом
    assert matched["AD1"]["payments"] == 1
    assert matched["AD1"]["revenue"] == 500000
    assert matched["AD2"]["payments"] == 0
    assert matched["AD2"]["revenue"] == 0


# ---------------------------------------------------------------------------
# _extract_fb_fields — извлечение fb_ad_id
# ---------------------------------------------------------------------------


def test_extract_fb_fields_includes_ad_id():
    """_extract_fb_fields читает fb_ad_id как строку в ключе 'ad_id'."""
    lead = {
        "custom_fields": [
            {"field_name": "fb_ad_name", "values": [{"value": "Денис | Тест"}]},
            {"field_name": "fb_ad_id",   "values": [{"value": "6559403328503"}]},
        ]
    }
    fb = _extract_fb_fields(lead)
    assert fb["ad_name"] == "Денис | Тест"
    assert fb["ad_id"] == "6559403328503"


def test_extract_fb_fields_ad_id_fallback_by_field_id():
    """Если field_name не содержит 'fb_ad_id' — читаем по field_id=902422 (запасной вариант)."""
    lead = {
        "custom_fields": [
            {"field_id": 902422, "field_name": "Какое-то поле", "values": [{"value": "6502170510211"}]},
        ]
    }
    fb = _extract_fb_fields(lead)
    assert fb.get("ad_id") == "6502170510211"


def test_extract_fb_fields_no_ad_id():
    """Лид без fb_ad_id поля — ключа 'ad_id' нет в результате."""
    lead = {
        "custom_fields": [
            {"field_name": "fb_ad_name", "values": [{"value": "Тест"}]},
        ]
    }
    fb = _extract_fb_fields(lead)
    assert "ad_id" not in fb


# ---------------------------------------------------------------------------
# match_leads_to_ads — матчинг по fb_ad_id с приоритетом над именем
# ---------------------------------------------------------------------------


def _make_lead_with_fb_id(fb_ad_name: str, fb_ad_id: str, status_id=30, price=0, qualified=False):
    """Создаёт тестовый лид с fb_ad_name И fb_ad_id."""
    fields = [
        {"field_name": "fb_ad_name", "values": [{"value": fb_ad_name}]},
        {"field_name": "fb_ad_id",   "values": [{"value": fb_ad_id}]},
    ]
    if qualified:
        fields.append({"field_name": "Квалификация пройдена", "values": [{"value": "ДА"}]})
    return {
        "id": next(_lead_id_seq), "name": "Test", "status_id": status_id,
        "pipeline_id": 1, "price": price, "created_at": 1700000000,
        "contacts": [],
        "custom_fields": fields,
    }


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [100])
def test_match_by_ad_id_priority_over_name():
    """ГЛАВНЫЙ ТЕСТ: два объявления с одинаковым именем, разные fb_ad_id.
    Лиды разводятся по ad_id — коллизия имён устраняется.
    """
    # Два объявления с ОДНИМ именем «Денис» но разными ad_id
    known_ad_ids = {"6559403328503", "6502170510211"}
    # fb_lookup по имени — оба имени одинаковы, побеждает первый вставленный
    fb_lookup = {"денис": "6559403328503"}  # имитируем: первый в lookup = первый ad_id

    # Много лидов с ad_id 6559403328503
    leads_first = [
        _make_lead_with_fb_id("Денис", "6559403328503", status_id=30)
        for _ in range(3)  # для теста достаточно нескольких
    ]
    # Несколько лидов с ad_id 6502170510211
    leads_second = [
        _make_lead_with_fb_id("Денис", "6502170510211", status_id=30)
        for _ in range(2)
    ]

    result = match_leads_to_ads(
        leads_first + leads_second,
        fb_lookup=fb_lookup,
        known_ad_ids=known_ad_ids,
    )

    # Каждый ad_id получает ТОЛЬКО свои лиды — нет смешивания
    assert "6559403328503" in result
    assert "6502170510211" in result
    assert result["6559403328503"]["total"] == 3
    assert result["6502170510211"]["total"] == 2


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [100])
def test_match_by_ad_id_known_in_kb():
    """Лид с fb_ad_id, который есть в known_ad_ids → привязка прямо по ad_id (не по имени)."""
    # fb_lookup указывает на ДРУГОЙ ad_id чем тот что в лиде
    # (симулируем ситуацию когда по имени совпал бы другой ad)
    fb_lookup = {"мой креатив": "WRONG_AD_ID"}
    known_ad_ids = {"CORRECT_AD_ID"}

    lead = _make_lead_with_fb_id("Мой креатив", "CORRECT_AD_ID")
    result = match_leads_to_ads([lead], fb_lookup=fb_lookup, known_ad_ids=known_ad_ids)

    # Должен привязаться к CORRECT_AD_ID (по fb_ad_id), а не WRONG_AD_ID (по имени)
    assert "CORRECT_AD_ID" in result
    assert "WRONG_AD_ID" not in result
    assert result["CORRECT_AD_ID"]["total"] == 1


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [100])
def test_match_fallback_to_name_when_no_ad_id():
    """Лид БЕЗ fb_ad_id → fallback на матч по имени (обратная совместимость)."""
    fb_lookup = {"старый креатив": "AD_OLD"}
    known_ad_ids = {"AD_OLD"}

    # Лид только с fb_ad_name, без fb_ad_id
    lead = _make_lead_with_fb("Старый креатив", status_id=30)
    result = match_leads_to_ads([lead], fb_lookup=fb_lookup, known_ad_ids=known_ad_ids)

    assert "AD_OLD" in result
    assert result["AD_OLD"]["total"] == 1


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [100])
def test_match_ad_id_not_in_known_fallback_to_name():
    """Лид с fb_ad_id которого НЕТ в known_ad_ids → fallback на матч по имени.

    Решение: безопасный fallback — не теряем лид, пробуем по имени.
    Это покрывает кейс "объявление есть в FB, но ещё не попало в KB".
    """
    # ad_id лида НЕТ в known_ad_ids (не в KB)
    known_ad_ids = {"OTHER_AD"}
    # Но имя есть в fb_lookup
    fb_lookup = {"известный": "AD_BY_NAME"}

    lead = _make_lead_with_fb_id("Известный", "UNKNOWN_AD_ID", status_id=30)
    result = match_leads_to_ads([lead], fb_lookup=fb_lookup, known_ad_ids=known_ad_ids)

    # Fallback по имени должен сработать
    assert "AD_BY_NAME" in result
    assert result["AD_BY_NAME"]["total"] == 1


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [100])
def test_match_ad_id_not_in_known_and_no_name_match():
    """Лид с fb_ad_id не в KB И без имени в fb_lookup → не падает, просто пропускается."""
    known_ad_ids = {"OTHER_AD"}
    fb_lookup = {}  # имени тоже нет

    lead = _make_lead_with_fb_id("Неизвестный", "UNKNOWN_AD_ID", status_id=30)
    result = match_leads_to_ads([lead], fb_lookup=fb_lookup, known_ad_ids=known_ad_ids)

    # Лид пропускается — результат пустой, функция не падает
    assert result == {}


@patch("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])
def test_match_collision_resolution_preserves_dedup():
    """Коллизия имён разводится по fb_ad_id, при этом дедуп по lead_id и contact_id работает."""
    known_ad_ids = {"AD_A", "AD_B"}
    fb_lookup = {"денис": "AD_A"}  # по имени всё шло бы в AD_A

    # Два лида разных контактов на AD_B (по fb_ad_id)
    lead1 = {
        "id": next(_lead_id_seq), "status_id": 143, "price": 100000,
        "created_at": 1700000000, "contacts": [{"id": 1}],
        "custom_fields": [
            {"field_name": "fb_ad_name", "values": [{"value": "Денис"}]},
            {"field_name": "fb_ad_id",   "values": [{"value": "AD_B"}]},
        ],
    }
    lead2 = {
        "id": next(_lead_id_seq), "status_id": 143, "price": 100000,
        "created_at": 1700000001, "contacts": [{"id": 2}],
        "custom_fields": [
            {"field_name": "fb_ad_name", "values": [{"value": "Денис"}]},
            {"field_name": "fb_ad_id",   "values": [{"value": "AD_B"}]},
        ],
    }

    result = match_leads_to_ads([lead1, lead2], fb_lookup=fb_lookup, known_ad_ids=known_ad_ids)

    # AD_A не получает ничего (fb_ad_id указывает на AD_B)
    assert "AD_A" not in result
    # AD_B получает оба лида (разные контакты → оба засчитываются)
    assert result["AD_B"]["total"] == 2
    assert result["AD_B"]["payments"] == 2
    assert result["AD_B"]["revenue"] == 200000


# ---------------------------------------------------------------------------
# Токен AMO: атомарная запись + устойчивое чтение (ревью, волна 1, п.1)
# AMO ротирует одноразовый refresh_token — обрыв записи или битый файл не должны
# убивать связку с CRM: чтение падает в {}, срабатывает фолбэк на AMO_REFRESH_TOKEN.
# ---------------------------------------------------------------------------


def test_load_token_happy_path(tmp_path, monkeypatch):
    """Валидный JSON читается как есть — happy path не изменился."""
    token_file = tmp_path / "amo_token.json"
    data = {"access_token": "abc", "refresh_token": "r1", "expires_at": 123.0}
    token_file.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr("integrations.amo.TOKEN_FILE", token_file)

    from integrations.amo import _load_token
    assert _load_token() == data


def test_load_token_missing_file_returns_empty(tmp_path, monkeypatch):
    """Файла нет → {} (без ошибок)."""
    monkeypatch.setattr("integrations.amo.TOKEN_FILE", tmp_path / "nope.json")

    from integrations.amo import _load_token
    assert _load_token() == {}


def test_load_token_broken_json_returns_empty(tmp_path, monkeypatch):
    """Битый JSON → {} (лог warning), а не JSONDecodeError, валящий все AMO-кроны."""
    token_file = tmp_path / "amo_token.json"
    token_file.write_text("{ это не JSON, обрыв записи...", encoding="utf-8")
    monkeypatch.setattr("integrations.amo.TOKEN_FILE", token_file)

    from integrations.amo import _load_token
    assert _load_token() == {}


def test_load_token_empty_file_returns_empty(tmp_path, monkeypatch):
    """Файл есть, но пустой (0 байт / пробелы) → {}."""
    token_file = tmp_path / "amo_token.json"
    token_file.write_text("   \n", encoding="utf-8")
    monkeypatch.setattr("integrations.amo.TOKEN_FILE", token_file)

    from integrations.amo import _load_token
    assert _load_token() == {}


def test_save_token_atomic_roundtrip(tmp_path, monkeypatch):
    """Запись → чтение возвращает то же, временных .tmp-файлов не остаётся."""
    token_file = tmp_path / "amo_token.json"
    monkeypatch.setattr("integrations.amo.TOKEN_FILE", token_file)

    from integrations.amo import _save_token, _load_token
    data = {"access_token": "xyz", "refresh_token": "r2", "expires_at": 999.0}
    _save_token(data)

    assert _load_token() == data
    # Никаких хвостов от tempfile.mkstemp
    assert list(tmp_path.glob(".amo_token.*.tmp")) == []


def test_save_token_interrupted_write_keeps_original_intact(tmp_path, monkeypatch):
    """Обрыв на os.replace НЕ оставляет обрезанного файла: оригинал цел, .tmp убран."""
    token_file = tmp_path / "amo_token.json"
    old = {"access_token": "OLD", "refresh_token": "OLD_R", "expires_at": 111.0}
    token_file.write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setattr("integrations.amo.TOKEN_FILE", token_file)

    from integrations.amo import _save_token, _load_token
    new = {"access_token": "NEW", "refresh_token": "NEW_R", "expires_at": 222.0}

    # Симулируем обрыв в момент атомарной подмены
    with patch("integrations.amo.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            _save_token(new)

    # Оригинал не тронут — старое содержимое читается целиком (не обрезано)
    assert _load_token() == old
    # Временный файл не остался мусором
    assert list(tmp_path.glob(".amo_token.*.tmp")) == []


@patch("integrations.amo.session.post")
def test_get_access_token_env_fallback_on_broken_file(mock_post, tmp_path, monkeypatch):
    """Битый файл токена → _load_token()={} → get_access_token берёт refresh из env."""
    broken = tmp_path / "amo_token.json"
    broken.write_text("{ битый ", encoding="utf-8")
    monkeypatch.setattr("integrations.amo.TOKEN_FILE", broken)
    monkeypatch.setattr("integrations.amo.AMO_REFRESH_TOKEN", "env-refresh-xyz")

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": "fresh-access",
        "refresh_token": "fresh-refresh",
        "expires_in": 86400,
    }
    mock_post.return_value = resp

    from integrations.amo import get_access_token, _load_token
    token = get_access_token()

    assert token == "fresh-access"
    # В запрос ушёл именно env-refresh (файл битый → токена из файла нет)
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["refresh_token"] == "env-refresh-xyz"
    # Новый токен сохранён атомарно и читается обратно
    saved = _load_token()
    assert saved["access_token"] == "fresh-access"
    assert saved["refresh_token"] == "fresh-refresh"


# --- Бюджет чтений AMO (исполнение одобренных действий) ---
#
# Дефолт (connect 10 / read 60, 3 попытки) держал ОДНУ зависшую страницу до
# 3.5 минут. В контуре исполнения одобренных владельцем действий таких чтений
# несколько на задание — прогон крона переставал укладываться в свой интервал.


@patch("integrations.amo.get_access_token", return_value="access-token")
@patch("integrations.amo.session.get")
def test_amo_get_keeps_long_timeouts_without_budget(mock_get, _token):
    """Снаружи бюджета поведение прежнее: connect 10 / read 60, три попытки."""
    from integrations.amo import _amo_get

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"_embedded": {"leads": []}}
    mock_get.return_value = resp

    _amo_get("leads", {})

    assert mock_get.call_args.kwargs["timeout"] == (10, 60)


@patch("integrations.amo.get_access_token", return_value="access-token")
@patch("integrations.amo.session.get")
def test_request_budget_shortens_timeout_and_drops_retries(mock_get, _token):
    """Внутри бюджета — короткие таймауты и ровно одна попытка."""
    from integrations.amo import _amo_get, request_budget

    mock_get.side_effect = requests.exceptions.ReadTimeout("AMO висит")

    with request_budget(connect_seconds=5, read_seconds=20, attempts=1):
        with pytest.raises(requests.exceptions.ReadTimeout):
            _amo_get("leads", {})

    assert mock_get.call_count == 1
    assert mock_get.call_args.kwargs["timeout"] == (5, 20)


@patch("integrations.amo.get_access_token", return_value="access-token")
@patch("integrations.amo.session.get")
def test_request_budget_expired_deadline_skips_request(mock_get, _token):
    """Срок вышел — запрос не уходит вовсе, а не занимает поток ещё на таймаут."""
    from integrations.amo import _amo_get, request_budget

    with request_budget(deadline_monotonic=time.monotonic() - 1):
        with pytest.raises(requests.exceptions.Timeout):
            _amo_get("leads", {})

    assert mock_get.call_count == 0


def test_request_budget_is_restored_after_block():
    """Бюджет живёт только внутри блока — соседние кроны его не наследуют."""
    from integrations.amo import _REQUEST_BUDGET, request_budget

    assert _REQUEST_BUDGET.get() is None
    with request_budget(read_seconds=7):
        assert _REQUEST_BUDGET.get().read_seconds == 7
    assert _REQUEST_BUDGET.get() is None
