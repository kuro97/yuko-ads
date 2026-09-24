"""Юнит/интеграционные тесты сверки платежей ERP (services/cdp_payments.py) —
ARCH-cdp-payments, T8 + T6.

1. compute_ad_payments_erp — агрегация income/refund/clamp/дедуп/unmapped/None.
2. _build_deal_to_ad_map — fail-closed при исключении AMO, фильтр known_ad_ids.
3. refresh_creative_kb_payments_erp — UPDATE только совпавшим, AMO-поля целы,
   CdpError → error-результат без изменения БД, state пишется.
4. payments_effective — все ветки таблицы §6.4.
5. Интеграция источника-каскада (T6): decision_policy.score_and_decide и
   budget_scaler._select_sales_candidates/_is_confirmed_waster под переключателем
   autopilot.cdp.payments_source (shadow/amo → AMO 1:1, erp → payments_effective).

Ключ маппинга платежа ERP к AMO-лиду — payment["contract_number"] (= AMO lead_id),
а НЕ payment["deal_id"] (внутренний surrogate-ключ CDP — маппинг по deal_id не
находит платящих лидов; contract_number резолвится в существующие лиды). Фикстура _payment() ниже
задаёт deal_id заведомо "мусорным" (не совпадающим с lead_id) во всех сценариях —
если код регрессирует и снова начнёт матчить по deal_id, тесты на contract_number
провалятся (см. test_compute_deal_id_matches_lead_but_ignored).

Мокаем cdp_client.get_payments и integrations.amo.get_leads_window (внешние
границы). БД creative_kb — временный SQLite через services.creative_intelligence.init_kb
(образец: tests/test_cdp_payments_migration.py). Реальная сеть заблокирована
conftest._no_real_network. Комментарии на русском.
"""

import json
import sqlite3
from datetime import date
from unittest.mock import patch

import pytest

from services import cdp_payments
from services import creative_intelligence as ci
from services.cdp_client import CdpError


# --- фикстуры ---


@pytest.fixture(autouse=True)
def reset_kb_path():
    """Сбрасываем глобальный DB_PATH перед и после каждого теста (образец
    tests/test_cdp_payments_migration.py) — не трогаем реальную data/decisions.db."""
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


@pytest.fixture(autouse=True)
def isolate_lead_cache(tmp_path, monkeypatch):
    """Изолирует дисковый кеш второго яруса маппинга (data/cdp_payments_lead_cache.json)
    от реальной data/ и по умолчанию мокает integrations.amo.get_lead(→None), чтобы
    старые тесты (не знающие про ярус 2) не делали реальных/заблокированных сетевых
    попыток при unmapped contract_number. Тесты яруса 2 переопределяют мок явно."""
    cache_file = tmp_path / "cdp_payments_lead_cache.json"
    monkeypatch.setattr(cdp_payments, "_LEAD_CACHE_FILE", cache_file)
    with patch("integrations.amo.get_lead", return_value=None):
        yield


@pytest.fixture
def kb(tmp_path):
    """Инициализирует временную creative_kb (SQLite) с миграцией 012 применённой."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)
    return db_path


def _insert_ad(db_path: str, ad_id: str, payments=None, revenue=None) -> None:
    """Вставляет минимальную строку creative_kb с опциональным AMO payments/revenue."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO creative_kb (ad_id, payments, revenue) VALUES (?, ?, ?)",
            (ad_id, payments, revenue),
        )
        conn.commit()
    finally:
        conn.close()


def _row(db_path: str, ad_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM creative_kb WHERE ad_id = ?", (ad_id,)
        ).fetchone()
    finally:
        conn.close()


def _lead(lead_id, ad_id):
    """Минимальный лид AMO с fb_ad_id (902422) для _build_deal_to_ad_map (ярус 1,
    get_leads_window)."""
    return {
        "id": lead_id,
        "custom_fields": [
            {
                "field_id": 902422,
                "field_name": "fb_ad_id",
                "values": [{"value": str(ad_id)}],
            }
        ],
    }


def _raw_lead(lead_id, ad_id=None):
    """RAW-лид в формате integrations.amo.get_lead (custom_fields_values, НЕ
    custom_fields) — для точечного fallback'а яруса 2. ad_id=None → лид без
    fb_ad_id (например сделка не по FB)."""
    custom_fields_values = []
    if ad_id is not None:
        custom_fields_values.append({
            "field_id": 902422,
            "field_name": "fb_ad_id",
            "values": [{"value": str(ad_id)}],
        })
    return {"id": lead_id, "custom_fields_values": custom_fields_values}


def _payment(pid, contract_number, amount=10000.0, direction="income", doc_date="2026-06-15"):
    """Платёж ERP. contract_number = AMO lead_id — настоящий ключ маппинга (deal_id —
    внутренний surrogate CDP, НЕ AMO lead_id: маппинг по deal_id не находит платящих
    лидов, contract_number резолвится в существующие лиды). deal_id намеренно оставлен "мусорным"
    (заведомо НЕ равным contract_number) — если код регрессирует и снова начнёт
    матчить по deal_id, тесты на contract_number провалятся."""
    return {
        "id": pid,
        "deal_id": 900000 + int(contract_number),  # мусорное поле — не должно использоваться
        "contract_number": contract_number,
        "amount": amount,
        "direction": direction,
        "doc_date": doc_date,
    }


# =====================================================================
# 1. compute_ad_payments_erp
# =====================================================================


def test_compute_income_aggregates(kb):
    """3 income-документа на 2 РАЗНЫЕ сделки (contract_number 101,102,201) → 2 ad.

    Семантика: сделки, не документы — здесь каждая сделка имеет
    ровно 1 документ, поэтому payments_erp совпадает со «старым» подсчётом по
    документам: ad_1 = 2 сделки (101,102), ad_2 = 1 сделка (201)."""
    _insert_ad(kb, "ad_1")
    _insert_ad(kb, "ad_2")

    leads = [_lead(101, "ad_1"), _lead(102, "ad_1"), _lead(201, "ad_2")]
    payments = [
        _payment(1, contract_number=101, amount=50000.0),
        _payment(2, contract_number=102, amount=30000.0),
        _payment(3, contract_number=201, amount=20000.0),
    ]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", return_value=payments):
        result = cdp_payments.compute_ad_payments_erp(date(2026, 6, 1), date(2026, 6, 30))

    assert result == {
        "ad_1": {"payments_erp": 2, "revenue_erp_lcy": 80000.0},
        "ad_2": {"payments_erp": 1, "revenue_erp_lcy": 20000.0},
    }


def test_compute_installment_multiple_docs_count_as_one_deal(kb):
    """Рассрочка банка на 12 месяцев: ОДНА сделка (contract_number=101) — 3 income-документа
    (по одному в месяц). Семантика: сделки, не документы (иначе AMO и ERP расходятся
    в обе стороны — payments_erp становится счётчиком документов, а не сделок). payments_erp должен быть 1 (одна оплатившая сделка),
    НЕ 3 (число документов); revenue_erp_lcy — честная сумма всех документов."""
    _insert_ad(kb, "ad_1")
    leads = [_lead(101, "ad_1")]
    payments = [
        _payment(1, contract_number=101, amount=10000.0, doc_date="2026-06-05"),
        _payment(2, contract_number=101, amount=10000.0, doc_date="2026-06-20"),  # тот же контракт
        _payment(3, contract_number=101, amount=10000.0, doc_date="2026-06-28"),  # тот же контракт
    ]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", return_value=payments):
        result = cdp_payments.compute_ad_payments_erp(date(2026, 6, 1), date(2026, 6, 30))

    assert result == {"ad_1": {"payments_erp": 1, "revenue_erp_lcy": 30000.0}}


def test_compute_refund_subtracts(kb):
    """2 income + 1 refund на ОДНУ сделку (contract_number=101) — net=60000>0,
    сделка считается оплаченной → payments_erp=1 (не 2, хотя документов 3),
    revenue минус сумму refund (семантика: сделки, не документы)."""
    _insert_ad(kb, "ad_1")
    leads = [_lead(101, "ad_1")]
    payments = [
        _payment(1, contract_number=101, amount=50000.0, direction="income"),
        _payment(2, contract_number=101, amount=30000.0, direction="income"),
        _payment(3, contract_number=101, amount=20000.0, direction="refund"),
    ]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", return_value=payments):
        result = cdp_payments.compute_ad_payments_erp(date(2026, 6, 1), date(2026, 6, 30))

    assert result["ad_1"]["payments_erp"] == 1
    assert result["ad_1"]["revenue_erp_lcy"] == 60000.0  # 50000 + 30000 - 20000


def test_compute_refund_eats_deal_to_zero_not_counted(kb):
    """1 refund, 0 income по сделке — net=-15000 (<=0) → сделка НЕ считается
    оплаченной (payments_erp=0 по этой сделке), но revenue_erp_lcy отражает честное
    нетто окна (может быть отрицательным). Семантика: сделки, не документы —
    раньше это называлось "clamp", теперь это часть общего правила net>0."""
    _insert_ad(kb, "ad_1")
    leads = [_lead(101, "ad_1")]
    payments = [_payment(1, contract_number=101, amount=15000.0, direction="refund")]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", return_value=payments):
        result = cdp_payments.compute_ad_payments_erp(date(2026, 6, 1), date(2026, 6, 30))

    assert result["ad_1"]["payments_erp"] == 0
    assert result["ad_1"]["revenue_erp_lcy"] == -15000.0  # честная выручка окна (может быть отриц.)


def test_compute_refund_eats_one_of_two_deals_to_zero(kb):
    """2 сделки на одном ad: сделка А (101) — income+refund съедает её в 0 (net<=0,
    не оплачена), сделка Б (102) — чистый income (net>0, оплачена). payments_erp=1
    (только Б), revenue_erp_lcy = сумма нетто ОБЕИХ сделок (включая нулевую А)."""
    _insert_ad(kb, "ad_1")
    leads = [_lead(101, "ad_1"), _lead(102, "ad_1")]
    payments = [
        _payment(1, contract_number=101, amount=20000.0, direction="income"),
        _payment(2, contract_number=101, amount=20000.0, direction="refund"),  # сделка А → net=0
        _payment(3, contract_number=102, amount=40000.0, direction="income"),  # сделка Б → net=40000
    ]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", return_value=payments):
        result = cdp_payments.compute_ad_payments_erp(date(2026, 6, 1), date(2026, 6, 30))

    assert result["ad_1"]["payments_erp"] == 1  # только сделка Б (102)
    assert result["ad_1"]["revenue_erp_lcy"] == 40000.0  # 0 (сделка А) + 40000 (сделка Б)


def test_compute_dedup_by_payment_id(kb):
    """Один и тот же payment_id (документ) встречается дважды (стык страниц) —
    учтён 1 раз при расчёте нетто сделки (дедуп на уровне документа, ДО агрегации
    по сделке — иначе рассрочка задвоилась бы ещё сильнее)."""
    _insert_ad(kb, "ad_1")
    leads = [_lead(101, "ad_1")]
    payments = [
        _payment(1, contract_number=101, amount=50000.0),
        _payment(1, contract_number=101, amount=50000.0),  # дубль того же payment_id
    ]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", return_value=payments):
        result = cdp_payments.compute_ad_payments_erp(date(2026, 6, 1), date(2026, 6, 30))

    assert result["ad_1"]["payments_erp"] == 1
    assert result["ad_1"]["revenue_erp_lcy"] == 50000.0


def test_compute_unmapped_deal_skipped(kb):
    """Платёж с contract_number, которого нет в deal_map (сделка не наша/не FB) —
    пропущен, но другие платежи того же прогона по замапленным сделкам
    агрегируются нормально."""
    _insert_ad(kb, "ad_1")
    leads = [_lead(101, "ad_1")]  # только сделка 101 замаплена
    payments = [
        _payment(1, contract_number=101, amount=50000.0),
        _payment(2, contract_number=999, amount=30000.0),  # contract_number вне deal_map
    ]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", return_value=payments):
        result = cdp_payments.compute_ad_payments_erp(date(2026, 6, 1), date(2026, 6, 30))

    assert result == {"ad_1": {"payments_erp": 1, "revenue_erp_lcy": 50000.0}}


def test_compute_deal_id_none_skipped(kb):
    """Платёж без contract_number (None) — пропущен, не падает. deal_id намеренно
    задан (и не совпадает с lead_id) — проверяем, что он игнорируется (T9)."""
    _insert_ad(kb, "ad_1")
    leads = [_lead(101, "ad_1")]
    payments = [
        {"id": 1, "deal_id": 555555, "contract_number": None, "amount": 10000.0,
         "direction": "income", "doc_date": "2026-06-15"},
        _payment(2, contract_number=101, amount=50000.0),
    ]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", return_value=payments):
        result = cdp_payments.compute_ad_payments_erp(date(2026, 6, 1), date(2026, 6, 30))

    assert result == {"ad_1": {"payments_erp": 1, "revenue_erp_lcy": 50000.0}}


def test_compute_deal_id_matches_lead_but_ignored(kb):
    """Регрессионный тест T9: платёж, где deal_id СЛУЧАЙНО совпадает с lead_id
    (замаплен в deal_map), но contract_number — другой/чужой lead → платёж НЕ
    засчитывается тому ad, на который бы указал deal_id. Ловит регресс "снова
    стали матчить по deal_id"."""
    _insert_ad(kb, "ad_1")
    _insert_ad(kb, "ad_2")
    leads = [_lead(101, "ad_1"), _lead(202, "ad_2")]
    # deal_id=101 совпадает с lead_id объявления ad_1, но реальный ключ —
    # contract_number=202 (объявление ad_2). Если бы код матчил по deal_id —
    # платёж ошибочно попал бы в ad_1.
    payment = {
        "id": 1,
        "deal_id": 101,
        "contract_number": 202,
        "amount": 70000.0,
        "direction": "income",
        "doc_date": "2026-06-15",
    }

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", return_value=[payment]):
        result = cdp_payments.compute_ad_payments_erp(date(2026, 6, 1), date(2026, 6, 30))

    assert result == {"ad_2": {"payments_erp": 1, "revenue_erp_lcy": 70000.0}}
    assert "ad_1" not in result, "регресс: платёж засчитан по deal_id вместо contract_number"


def test_compute_non_numeric_contract_number_skipped(kb):
    """contract_number нечисловой/пустая строка — платёж пропускается (skip),
    не падает с ValueError."""
    _insert_ad(kb, "ad_1")
    leads = [_lead(101, "ad_1")]
    payments = [
        {"id": 1, "deal_id": 1, "contract_number": "не число", "amount": 5000.0,
         "direction": "income", "doc_date": "2026-06-15"},
        {"id": 2, "deal_id": 2, "contract_number": "", "amount": 6000.0,
         "direction": "income", "doc_date": "2026-06-15"},
        _payment(3, contract_number=101, amount=50000.0),
    ]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", return_value=payments):
        result = cdp_payments.compute_ad_payments_erp(date(2026, 6, 1), date(2026, 6, 30))

    assert result == {"ad_1": {"payments_erp": 1, "revenue_erp_lcy": 50000.0}}


def test_compute_cdp_error_returns_empty(kb):
    """get_payments бросает CdpError — {} (fail-closed), исключение не наружу."""
    _insert_ad(kb, "ad_1")
    leads = [_lead(101, "ad_1")]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", side_effect=CdpError("CDP недоступен")):
        result = cdp_payments.compute_ad_payments_erp(date(2026, 6, 1), date(2026, 6, 30))

    assert result == {}


def test_compute_no_known_ad_ids_returns_empty_without_calling_cdp(kb):
    """Пустая creative_kb (нет ad_id) — {} сразу, cdp_client.get_payments не зовём."""
    with patch("integrations.amo.get_leads_window") as mock_leads, \
         patch("services.cdp_client.get_payments") as mock_get_payments:
        result = cdp_payments.compute_ad_payments_erp(date(2026, 6, 1), date(2026, 6, 30))

    assert result == {}
    mock_leads.assert_not_called()
    mock_get_payments.assert_not_called()


# =====================================================================
# 2. _build_deal_to_ad_map
# =====================================================================


def test_deal_to_ad_map_from_fb_ad_id():
    """Лиды с fb_ad_id ∈ known_ad_ids → {deal_id: ad_id}; лид без fb_ad_id и лид
    с ad_id не из known_ad_ids — пропущены."""
    leads = [
        _lead(101, "ad_1"),
        _lead(102, "ad_2"),
        _lead(103, "ad_other"),  # не наше объявление
        {"id": 104, "custom_fields": []},  # нет fb_ad_id
    ]
    known_ad_ids = {"ad_1", "ad_2"}

    with patch("integrations.amo.get_leads_window", return_value=leads):
        result = cdp_payments._build_deal_to_ad_map(known_ad_ids)

    assert result == {"101": "ad_1", "102": "ad_2"}


def test_deal_to_ad_map_fail_closed_on_amo_exception():
    """Исключение AMO (get_leads_window бросает) → {} (fail-closed), не наружу."""
    known_ad_ids = {"ad_1"}

    with patch("integrations.amo.get_leads_window", side_effect=RuntimeError("AMO недоступна")):
        result = cdp_payments._build_deal_to_ad_map(known_ad_ids)

    assert result == {}


def test_deal_to_ad_map_empty_ad_ids_returns_empty_without_amo_call():
    """Пустой ad_ids — {} сразу, AMO не дёргаем."""
    with patch("integrations.amo.get_leads_window") as mock_leads:
        result = cdp_payments._build_deal_to_ad_map(set())

    assert result == {}
    mock_leads.assert_not_called()


# =====================================================================
# 2b. Второй ярус маппинга: точечный fallback
#
# Пакетное окно (ярус 1) не покрывает лиды, созданные раньше окна
# get_leads_window — точечный get_lead + дисковый кеш (ярус 2) их дозапрашивает.
# =====================================================================


def test_deal_to_ad_map_pointed_fallback_resolves_missing_contract_number():
    """contract_number не найден в пакетном окне (ярус 1) — точечный get_lead
    (ярус 2) резолвит его в ad_id."""
    leads_batch = [_lead(101, "ad_1")]  # ярус 1 знает только про 101
    raw_lead_202 = _raw_lead(202, ad_id="ad_2")  # лид 202 создан раньше окна

    with patch("integrations.amo.get_leads_window", return_value=leads_batch), \
         patch("integrations.amo.get_lead", return_value=raw_lead_202) as mock_get_lead:
        result = cdp_payments._build_deal_to_ad_map({"ad_1", "ad_2"}, contract_numbers={"101", "202"})

    assert result == {"101": "ad_1", "202": "ad_2"}
    mock_get_lead.assert_called_once_with(202)


def test_deal_to_ad_map_pointed_fallback_not_called_when_already_covered():
    """contract_number уже покрыт ярусом 1 — точечный get_lead НЕ вызывается
    (нет лишней нагрузки на AMO)."""
    leads_batch = [_lead(101, "ad_1")]

    with patch("integrations.amo.get_leads_window", return_value=leads_batch), \
         patch("integrations.amo.get_lead") as mock_get_lead:
        result = cdp_payments._build_deal_to_ad_map({"ad_1"}, contract_numbers={"101"})

    assert result == {"101": "ad_1"}
    mock_get_lead.assert_not_called()


def test_compute_finds_payment_outside_batch_window_via_pointed_fallback(kb):
    """Интеграционно: платёж на contract_number вне пакетного окна get_leads_window —
    compute_ad_payments_erp всё равно находит ad_id через точечный fallback."""
    _insert_ad(kb, "ad_2")
    leads_batch: list = []  # ярус 1 пуст — сделка создана раньше 100-дневного окна
    raw_lead = _raw_lead(202, ad_id="ad_2")
    payments = [_payment(1, contract_number=202, amount=45000.0)]

    with patch("integrations.amo.get_leads_window", return_value=leads_batch), \
         patch("integrations.amo.get_lead", return_value=raw_lead), \
         patch("services.cdp_client.get_payments", return_value=payments):
        result = cdp_payments.compute_ad_payments_erp(date(2026, 6, 1), date(2026, 6, 30))

    assert result == {"ad_2": {"payments_erp": 1, "revenue_erp_lcy": 45000.0}}


def test_pointed_fallback_cache_hit_skips_amo_call(tmp_path, monkeypatch):
    """Кеш-хит (lead_id уже резолвлен ранее, ad_id известен) — get_lead НЕ
    вызывается повторно (кроссрановой кеш)."""
    cache_file = tmp_path / "lead_cache.json"
    monkeypatch.setattr(cdp_payments, "_LEAD_CACHE_FILE", cache_file)
    cdp_payments._save_lead_cache({"202": "ad_2"})

    with patch("integrations.amo.get_lead") as mock_get_lead:
        result = cdp_payments._resolve_contract_numbers_pointed({"202"}, {"ad_2"})

    assert result == {"202": "ad_2"}
    mock_get_lead.assert_not_called()


def test_pointed_fallback_null_cache_hit_skips_amo_call(tmp_path, monkeypatch):
    """Null-кеш (ранее точечно проверили — лид без fb_ad_id / не наш) — get_lead
    НЕ вызывается повторно, lead_id остаётся unmapped."""
    cache_file = tmp_path / "lead_cache.json"
    monkeypatch.setattr(cdp_payments, "_LEAD_CACHE_FILE", cache_file)
    cdp_payments._save_lead_cache({"303": None})

    with patch("integrations.amo.get_lead") as mock_get_lead:
        result = cdp_payments._resolve_contract_numbers_pointed({"303"}, {"ad_2"})

    assert result == {}
    mock_get_lead.assert_not_called()


def test_pointed_fallback_writes_null_cache_for_lead_without_fb_ad_id(tmp_path, monkeypatch):
    """Лид найден в AMO, но без fb_ad_id (не FB-сделка) — кешируется как null,
    результат пуст (lead_id unmapped)."""
    cache_file = tmp_path / "lead_cache.json"
    monkeypatch.setattr(cdp_payments, "_LEAD_CACHE_FILE", cache_file)

    raw_lead_no_fb = _raw_lead(404, ad_id=None)
    with patch("integrations.amo.get_lead", return_value=raw_lead_no_fb):
        result = cdp_payments._resolve_contract_numbers_pointed({"404"}, {"ad_2"})

    assert result == {}
    cache = cdp_payments._load_lead_cache()
    assert cache["404"] is None


def test_pointed_fallback_writes_null_cache_for_lead_not_found():
    """get_lead возвращает None (лид не существует, 404) — кешируется null,
    результат пуст, исключение не бросается."""
    with patch("integrations.amo.get_lead", return_value=None):
        result = cdp_payments._resolve_contract_numbers_pointed({"505"}, {"ad_2"})

    assert result == {}


def test_pointed_fallback_respects_cap(monkeypatch):
    """Потолок точечных lookup'ов за прогон (_POINTED_LOOKUP_CAP) — свыше потолка
    lead_id не резолвятся (unmapped), но функция не падает и не зависает."""
    monkeypatch.setattr(cdp_payments, "_POINTED_LOOKUP_CAP", 2)
    missing_ids = {"1", "2", "3", "4", "5"}

    call_count = 0

    def _fake_get_lead(lead_id):
        nonlocal call_count
        call_count += 1
        return _raw_lead(lead_id, ad_id="ad_2")

    with patch("integrations.amo.get_lead", side_effect=_fake_get_lead), \
         patch("time.sleep"):  # без реальных задержек в тесте
        result = cdp_payments._resolve_contract_numbers_pointed(missing_ids, {"ad_2"})

    assert call_count == 2, "должно быть ровно _POINTED_LOOKUP_CAP запросов, не больше"
    assert len(result) == 2, "только 2 из 5 lead_id резолвлены — потолок соблюдён"


def test_pointed_fallback_throttles_between_calls(monkeypatch):
    """Между точечными get_lead вызывается time.sleep (троттлинг AMO) — кроме
    самого первого запроса."""
    sleep_calls = []
    monkeypatch.setattr(cdp_payments.time, "sleep", lambda s: sleep_calls.append(s))

    with patch("integrations.amo.get_lead", return_value=None):
        cdp_payments._resolve_contract_numbers_pointed({"1", "2", "3"}, {"ad_2"})

    assert len(sleep_calls) == 2, "sleep между запросами, не перед первым (3 запроса → 2 паузы)"


# =====================================================================
# 3. refresh_creative_kb_payments_erp
# =====================================================================


def test_refresh_writes_only_matched(kb):
    """Агрегат по 1 ad из 3 в KB — обновлён 1, у остальных payments_erp=NULL."""
    _insert_ad(kb, "ad_1")
    _insert_ad(kb, "ad_2")
    _insert_ad(kb, "ad_3")
    leads = [_lead(101, "ad_1")]
    payments = [_payment(1, contract_number=101, amount=40000.0)]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", return_value=payments):
        result = cdp_payments.refresh_creative_kb_payments_erp(months_back=3)

    assert result["ads_updated"] == 1
    assert result["payments_total"] == 1
    assert result["revenue_total_lcy"] == 40000.0
    assert result["error"] is None

    row1 = _row(kb, "ad_1")
    assert row1["payments_erp"] == 1
    assert row1["revenue_erp_lcy"] == 40000.0
    assert row1["payments_erp_synced_at"] is not None

    row2 = _row(kb, "ad_2")
    row3 = _row(kb, "ad_3")
    assert row2["payments_erp"] is None
    assert row3["payments_erp"] is None


def test_refresh_does_not_clobber_amo_fields(kb):
    """AMO-поля (payments/revenue) НЕ затёрты при записи payments_erp/revenue_erp_lcy."""
    _insert_ad(kb, "ad_1", payments=7, revenue=123456.0)
    leads = [_lead(101, "ad_1")]
    payments = [_payment(1, contract_number=101, amount=40000.0)]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", return_value=payments):
        cdp_payments.refresh_creative_kb_payments_erp(months_back=3)

    row = _row(kb, "ad_1")
    assert row["payments"] == 7
    assert row["revenue"] == 123456.0
    assert row["payments_erp"] == 1
    assert row["revenue_erp_lcy"] == 40000.0


def test_refresh_cdp_down_preserves_amo(kb):
    """CdpError → error-результат, AMO payments целы, payments_erp остаётся NULL
    (БД не изменилась в части ERP-колонок)."""
    _insert_ad(kb, "ad_1", payments=5, revenue=99999.0)
    leads = [_lead(101, "ad_1")]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", side_effect=CdpError("CDP недоступен")):
        result = cdp_payments.refresh_creative_kb_payments_erp(months_back=3)

    assert result["ads_updated"] == 0
    assert result["error"] is not None

    row = _row(kb, "ad_1")
    assert row["payments"] == 5
    assert row["revenue"] == 99999.0
    assert row["payments_erp"] is None
    assert row["revenue_erp_lcy"] is None
    assert row["payments_erp_synced_at"] is None


def test_refresh_writes_state(kb, tmp_path, monkeypatch):
    """Успешный прогон пишет state-файл (last_synced_doc_date/last_run_at)."""
    state_file = tmp_path / "cdp_payments_state.json"
    monkeypatch.setattr(cdp_payments, "_STATE_FILE", state_file)

    _insert_ad(kb, "ad_1")
    leads = [_lead(101, "ad_1")]
    payments = [_payment(1, contract_number=101, amount=10000.0)]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", return_value=payments):
        cdp_payments.refresh_creative_kb_payments_erp(months_back=3)

    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "last_synced_doc_date" in state
    assert "last_run_at" in state


def test_refresh_state_not_written_on_cdp_failure(kb, tmp_path, monkeypatch):
    """При падении CDP (пустой агрегат) state-файл не создаётся — нечего фиксировать."""
    state_file = tmp_path / "cdp_payments_state.json"
    monkeypatch.setattr(cdp_payments, "_STATE_FILE", state_file)

    _insert_ad(kb, "ad_1")
    leads = [_lead(101, "ad_1")]

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("services.cdp_client.get_payments", side_effect=CdpError("CDP недоступен")):
        cdp_payments.refresh_creative_kb_payments_erp(months_back=3)

    assert not state_file.exists()


# =====================================================================
# 4. payments_effective
# =====================================================================


def test_effective_both_none():
    """payments=None, payments_erp=None → None (нет данных ни от кого)."""
    assert cdp_payments.payments_effective({"payments": None, "payments_erp": None}) is None


def test_effective_max():
    """amo=3, erp=5 → max = 5."""
    assert cdp_payments.payments_effective({"payments": 3, "payments_erp": 5}) == 5


def test_effective_amo_zero_erp_two():
    """amo=0, erp=2 → 2 (защита победителя — ERP видит деньги раньше AMO)."""
    assert cdp_payments.payments_effective({"payments": 0, "payments_erp": 2}) == 2


def test_effective_amo_none_erp_one():
    """amo=None, erp=1 → 1 (только ERP знает)."""
    assert cdp_payments.payments_effective({"payments": None, "payments_erp": 1}) == 1


def test_effective_amo_four_erp_zero():
    """amo=4, erp=0 → 4 (AMO видит, ERP ещё нет — синк вечером, сигнал не теряем)."""
    assert cdp_payments.payments_effective({"payments": 4, "payments_erp": 0}) == 4


def test_effective_amo_present_erp_none():
    """amo=3, erp=None → 3 (только AMO знает)."""
    assert cdp_payments.payments_effective({"payments": 3, "payments_erp": None}) == 3


def test_effective_missing_keys_treated_as_none():
    """Оба ключа отсутствуют в dict (не только None) → None, не падает KeyError."""
    assert cdp_payments.payments_effective({}) is None


# =====================================================================
# 5. Интеграция источника-каскада: decision_policy + budget_scaler (T6, §9)
#
# Проверяем переключатель autopilot.cdp.payments_source на реальных вызовах
# score_and_decide (Страж) и _select_sales_candidates/_is_confirmed_waster
# (Бюджет-пилот): shadow/amo — поведение 1:1 (только AMO), erp — payments_effective
# (max AMO/ERP). Критично: ОБА модуля должны быть синхронны (§8 спеки —
# «рассинхрон Страж↔скейлер»), иначе Страж по ERP-оплате не паузит, а скейлер
# по устаревшему AMO=0 блокирует долив в адсет.
# =====================================================================


def _waster_ad(ad_id="waster_1", adset_id="adset_w", payments=0, payments_erp=None):
    """Объявление с диагностическим tier B сигналом по AMO:
    spend>150, qual_pct<10, сверка прошла (outcomes_matched_at не None)."""
    return {
        "ad_id": ad_id,
        "ad_name": "CityA | Тест / слив",
        "adset_id": adset_id,
        "city": "CityA",
        "adset_type": "L2",
        "spend": 300.0,
        "leads": 10,
        "cpl": 30.0,
        "ctr": 1.0,
        "hook_rate": None,
        "qual_pct": 5.0,
        "romi": None,
        "days_running": 5,
        "impressions": 10000,
        "video_views_3s": 0, "video_p25": 0, "video_p50": 0, "video_p75": 0, "video_p100": 0,
        "payments": payments,
        "payments_erp": payments_erp,
        "outcomes_matched_at": "2026-06-01T00:00:00",
    }


def _companion_ad(ad_id="other", adset_id="adset_other"):
    """ACTIVE-компаньон; нужный adset задаётся конкретным сценарием теста."""
    return {
        "ad_id": ad_id,
        "ad_name": "CityA | Тест / хорошее",
        "adset_id": adset_id,
        "status": "ACTIVE",
        "effective_status": "ACTIVE",
        "city": "CityA",
        "adset_type": "L2",
        "spend": 30.0,
        "leads": 8,
        "cpl": 4.0,
        "ctr": 2.0,
        "hook_rate": 30.0,
        "qual_pct": 25.0,
        "romi": 120.0,
        "days_running": 5,
        "impressions": 10000,
        "video_views_3s": 0, "video_p25": 0, "video_p50": 0, "video_p75": 0, "video_p100": 0,
        "payments": 3,
        "payments_erp": None,
        "outcomes_matched_at": "2026-06-01T00:00:00",
    }


def test_decision_shadow_uses_amo():
    """mode=shadow: AMO=0 оставляет tier B диагностикой.

    Независимый portfolio outsider по-прежнему может дать PAUSE.
    """
    from services.decision_policy import score_and_decide

    waster = _waster_ad(payments=0, payments_erp=2)
    other = _companion_ad(adset_id=waster["adset_id"])

    cfg = {"cdp": {"payments_source": "shadow"}}
    with patch("services.autopilot.get_autopilot_config", return_value=cfg):
        results = score_and_decide([waster, other])

    result_map = {r["ad_id"]: r for r in results}
    waster_result = result_map["waster_1"]
    assert waster_result["action"] == "PAUSE"
    assert waster_result["is_confirmed_waster"] is False, waster_result
    assert waster_result["is_tier_b_diagnostic"] is True, waster_result
    reasons = " ".join(waster_result["reasons"]).lower()
    assert "портфельный аутсайдер" in reasons, waster_result
    assert "тир b" in reasons, waster_result


def test_decision_erp_uses_max():
    """mode=erp: payments_effective = max(amo=0, erp=2) = 2 → НЕ confirmed_waster.

    ERP видит деньги раньше AMO (лаг статуса сделки) — защита победителя от паузы."""
    from services.decision_policy import score_and_decide

    waster = _waster_ad(payments=0, payments_erp=2)
    other = _companion_ad()

    cfg = {"cdp": {"payments_source": "erp"}}
    with patch("services.autopilot.get_autopilot_config", return_value=cfg):
        results = score_and_decide([waster, other])

    result_map = {r["ad_id"]: r for r in results}
    waster_result = result_map["waster_1"]
    assert waster_result["is_confirmed_waster"] is False, (
        f"erp: payments_effective=2 (max) — НЕ должен считаться подтверждённым сливом: {waster_result}"
    )
    assert waster_result["action"] != "PAUSE", (
        f"erp: с эффективной оплатой=2 объявление не должно паузиться как слив: {waster_result}"
    )


def test_scaler_erp_candidate_from_erp():
    """mode=erp: amo=None, erp=3 → payments_effective=3 → попадает в sales-кандидаты.

    В amo/shadow-режиме такое объявление НЕ попало бы (payments=None не считается
    оплатой в _has_payment)."""
    from services.budget_scaler import _select_sales_candidates

    ad = {"ad_id": "ad_erp_only", "payments": None, "payments_erp": 3, "qual_pct": 20.0, "romi": 100.0}

    # amo/shadow (дефолт) — не кандидат, AMO ничего не знает
    assert _select_sales_candidates([ad], use_erp_payments=False) == []

    # erp — payments_effective(None, 3) = 3 → кандидат
    result = _select_sales_candidates([ad], use_erp_payments=True)
    assert len(result) == 1
    assert result[0]["ad_id"] == "ad_erp_only"


def test_scaler_erp_guardrail_not_blocked_by_stale_amo():
    """mode=erp: адсет с объявлением amo=0 (сверено) но erp=2 — guardrail
    _is_confirmed_waster НЕ должен заблокировать долив (§8 спеки — устраняем
    рассинхрон Страж↔скейлер: если Страж по ERP-оплате не паузит, скейлер тоже
    не должен блокировать адсет по устаревшему AMO=0)."""
    from services.budget_scaler import _is_confirmed_waster

    stale_amo_ad = {
        "ad_id": "stale_amo",
        "outcomes_matched_at": "2026-06-01T00:00:00",  # сверка AMO прошла
        "payments": 0,       # AMO ещё не видит деньги (лаг статуса)
        "payments_erp": 2,    # ERP уже видит 2 оплаты
    }

    # amo/shadow (дефолт) — старое поведение: payments=0 и сверено → waster=True
    # (блокирует долив в адсет, как раньше)
    assert _is_confirmed_waster(stale_amo_ad, use_erp_payments=False) is True

    # erp — payments_effective = max(0, 2) = 2 → НЕ waster, guardrail L1645 не
    # заблокирует подъём адсета из-за устаревшего AMO
    assert _is_confirmed_waster(stale_amo_ad, use_erp_payments=True) is False

    # Симулируем сам guardrail (any(...) в run_budget_scaling): в erp-режиме
    # адсет с этим объявлением НЕ исключается из кандидатов на подъём.
    adset_ads = [stale_amo_ad]
    blocked_erp = any(_is_confirmed_waster(a, use_erp_payments=True) for a in adset_ads)
    assert blocked_erp is False, "erp: адсет не должен блокироваться устаревшим AMO=0 при свежей ERP-оплате"
