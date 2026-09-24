"""
Unit-тесты для services/amo_outcomes.py.

Проверяют матчинг AMO-исходов к объявлениям creative_kb по точному имени,
корректность обновления метрик и graceful degradation при недоступности AMO.

Используют tmp_path — не трогают реальную БД data/decisions.db.
"""

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Мокаем google.genai до импорта любого модуля который может его тянуть
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())

from services import creative_intelligence as ci
import services.amo_outcomes as ao


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_kb_path():
    """Сбрасываем глобальный DB_PATH перед и после каждого теста."""
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


@pytest.fixture
def kb(tmp_path):
    """Инициализирует пустую KB во временной директории и возвращает путь."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)
    return db_path


def _insert_ad(db_path: str, ad_id: str, ad_name: str, spend: float = 100.0) -> None:
    """Вставляет объявление в creative_kb для тестов."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO creative_kb (ad_id, ad_name, spend, is_full_cabinet)
            VALUES (?, ?, ?, 1)
            """,
            (ad_id, ad_name, spend),
        )
        conn.commit()
    finally:
        conn.close()


def _make_lead(fb_ad_name: str, is_qual: bool = True, is_payment: bool = False,
               price: float = 0.0) -> dict:
    """Создаёт тестовый лид AMO в формате match_leads_to_ads."""
    return {
        "id": 1,
        "name": "Тест лид",
        "created_at": 1700000000,
        "status_id": 999,
        "price": price,
        "_embedded": {
            "custom_fields_values": [
                {
                    "field_id": 123456,  # fb_ad_name field
                    "values": [{"value": fb_ad_name}],
                }
            ]
        },
        # Поля, которые extract_fb_fields будет искать
        "_fb_ad_name": fb_ad_name,
    }


# ---------------------------------------------------------------------------
# Тест 1: матч по точному имени — qual_leads и outcomes_matched_at заполняются
# ---------------------------------------------------------------------------


def test_amo_outcomes_match_by_name(kb, monkeypatch):
    """AMO лид с точным fb_ad_name совпадает с ad_name в KB → qual_leads обновляется."""
    ad_name = "CityA | CR001"
    _insert_ad(kb, "ad_001", ad_name, spend=100.0)

    # Мок get_leads_window — возвращает 1 лид с точным именем
    def fake_get_leads_window(from_ts, to_ts):
        return [{"_fb_ad_name": ad_name, "id": 1, "price": 0}]

    # Мок match_leads_to_ads — уже совпавший результат
    def fake_match(leads, fb_lookup, known_ad_ids=None):
        # Проверяем что fb_lookup содержит наш ключ
        key = ad_name.strip().lower()
        if key in fb_lookup:
            return {
                fb_lookup[key]: {
                    "leads": leads,
                    "total": 1,
                    "quals": 1,
                    "payments": 0,
                    "revenue": 0.0,
                }
            }
        return {}

    # Мок calc_ad_metrics — возвращаем предсказуемые метрики
    def fake_calc(matched, ad_spends):
        result = {}
        for ad_id in matched:
            result[ad_id] = {
                "total_leads": 1,
                "qual_leads": 1,
                "qual_pct": 100.0,
                "cpql": 100.0,
                "payments": 0,
                "revenue": 0.0,
                "romi": None,
                "updated_at": "2026-06-11T00:00:00",
            }
        return result

    monkeypatch.setattr(ao, "get_leads_window", fake_get_leads_window)
    monkeypatch.setattr(ao, "match_leads_to_ads", fake_match)
    monkeypatch.setattr(ao, "calc_ad_metrics", fake_calc)

    result = ao.attach_amo_outcomes(months_back=1, batch_days=30)

    assert result["ads_updated"] == 1
    assert result["ads_matched"] == 1

    # Проверяем что qual_leads и outcomes_matched_at заполнены
    conn = ci._get_connection()
    try:
        row = conn.execute(
            "SELECT qual_leads, outcomes_matched_at FROM creative_kb WHERE ad_id = 'ad_001'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["qual_leads"] == 1
    assert row["outcomes_matched_at"] is not None


# ---------------------------------------------------------------------------
# Тест 2: нет совпадения — строки в KB не трогаются
# ---------------------------------------------------------------------------


def test_amo_outcomes_no_match(kb, monkeypatch):
    """AMO лид с чужим именем не трогает строки в KB."""
    _insert_ad(kb, "ad_002", "CityB | L2 | Наш ад", spend=50.0)

    def fake_get_leads_window(from_ts, to_ts):
        return [{"_fb_ad_name": "Совсем другое объявление", "id": 99, "price": 0}]

    # match_leads_to_ads вернёт пустой словарь (нет совпадений)
    def fake_match(leads, fb_lookup, known_ad_ids=None):
        return {}

    def fake_calc(matched, ad_spends):
        return {}

    monkeypatch.setattr(ao, "get_leads_window", fake_get_leads_window)
    monkeypatch.setattr(ao, "match_leads_to_ads", fake_match)
    monkeypatch.setattr(ao, "calc_ad_metrics", fake_calc)

    result = ao.attach_amo_outcomes(months_back=1, batch_days=30)

    assert result["ads_updated"] == 0
    assert result["ads_matched"] == 0

    # Объявление в KB не тронуто — outcomes_matched_at остался NULL
    conn = ci._get_connection()
    try:
        row = conn.execute(
            "SELECT outcomes_matched_at FROM creative_kb WHERE ad_id = 'ad_002'"
        ).fetchone()
    finally:
        conn.close()

    assert row["outcomes_matched_at"] is None


# ---------------------------------------------------------------------------
# Тест 3: AMO недоступна → {"error": ...}, не падает
# ---------------------------------------------------------------------------


def test_amo_outcomes_amo_unavailable(kb, monkeypatch):
    """Если get_leads_window бросает исключение — attach_amo_outcomes возвращает error, не падает."""
    _insert_ad(kb, "ad_003", "CityC | L1 | Тест", spend=75.0)

    def raise_exc(from_ts, to_ts):
        raise Exception("AMO connection timeout")

    monkeypatch.setattr(ao, "get_leads_window", raise_exc)

    result = ao.attach_amo_outcomes(months_back=1, batch_days=30)

    # Функция не падает — возвращает error
    assert "error" in result
    assert result["ads_updated"] == 0
    assert "AMO" in result["error"] or "connection" in result["error"].lower()


# ---------------------------------------------------------------------------
# Тест 4: romi считается при наличии spend и revenue
# ---------------------------------------------------------------------------


def test_amo_outcomes_romi_calculated(kb, monkeypatch):
    """При наличии spend и revenue — romi должен быть положительным."""
    ad_name = "CityA | Оплата"
    _insert_ad(kb, "ad_004", ad_name, spend=20.0)

    def fake_get_leads_window(from_ts, to_ts):
        return [{"_fb_ad_name": ad_name, "id": 2, "price": 30000.0}]

    def fake_match(leads, fb_lookup, known_ad_ids=None):
        key = ad_name.strip().lower()
        if key in fb_lookup:
            return {
                fb_lookup[key]: {
                    "leads": leads,
                    "total": 1,
                    "quals": 1,
                    "payments": 1,
                    "revenue": 30000.0,
                }
            }
        return {}

    def fake_calc(matched, ad_spends):
        result = {}
        for ad_id in matched:
            spend = ad_spends.get(ad_id, 0)
            result[ad_id] = {
                "total_leads": 1,
                "qual_leads": 1,
                "qual_pct": 100.0,
                "cpql": spend,
                "payments": 1,
                "revenue": 30000.0,
                # Упрощённая формула ROMI (для теста)
                "romi": round(30000.0 / (spend * 100) * 100, 1) if spend > 0 else None,
                "updated_at": "2026-06-11T00:00:00",
            }
        return result

    monkeypatch.setattr(ao, "get_leads_window", fake_get_leads_window)
    monkeypatch.setattr(ao, "match_leads_to_ads", fake_match)
    monkeypatch.setattr(ao, "calc_ad_metrics", fake_calc)

    result = ao.attach_amo_outcomes(months_back=1, batch_days=30)

    assert result["ads_updated"] == 1

    conn = ci._get_connection()
    try:
        row = conn.execute(
            "SELECT romi FROM creative_kb WHERE ad_id = 'ad_004'"
        ).fetchone()
    finally:
        conn.close()

    # romi должен быть не None и > 0
    assert row["romi"] is not None
    assert row["romi"] > 0


# ---------------------------------------------------------------------------
# Тест 5: несколько объявлений — совпавшее обновляется, несовпавшее нет
# ---------------------------------------------------------------------------


def test_amo_outcomes_updates_only_matched(kb, monkeypatch):
    """Только совпавшее объявление обновляется, чужое остаётся нетронутым."""
    _insert_ad(kb, "ad_match", "Наш ад | Тест", spend=100.0)
    _insert_ad(kb, "ad_other", "Другой ад | Тест", spend=200.0)

    def fake_get_leads_window(from_ts, to_ts):
        return []  # не важно — match будет замокан

    def fake_match(leads, fb_lookup, known_ad_ids=None):
        # Только ad_match в fb_lookup совпадает
        if "наш ад | тест" in fb_lookup:
            return {
                "ad_match": {
                    "leads": [],
                    "total": 2,
                    "quals": 1,
                    "payments": 0,
                    "revenue": 0.0,
                }
            }
        return {}

    def fake_calc(matched, ad_spends):
        return {
            ad_id: {
                "total_leads": d["total"],
                "qual_leads": d["quals"],
                "qual_pct": 50.0,
                "cpql": 100.0,
                "payments": 0,
                "revenue": 0.0,
                "romi": None,
                "updated_at": "2026-06-11",
            }
            for ad_id, d in matched.items()
        }

    monkeypatch.setattr(ao, "get_leads_window", fake_get_leads_window)
    monkeypatch.setattr(ao, "match_leads_to_ads", fake_match)
    monkeypatch.setattr(ao, "calc_ad_metrics", fake_calc)

    ao.attach_amo_outcomes(months_back=1, batch_days=30)

    conn = ci._get_connection()
    try:
        matched_row = conn.execute(
            "SELECT outcomes_matched_at FROM creative_kb WHERE ad_id = 'ad_match'"
        ).fetchone()
        other_row = conn.execute(
            "SELECT outcomes_matched_at FROM creative_kb WHERE ad_id = 'ad_other'"
        ).fetchone()
    finally:
        conn.close()

    # Совпавшее — обновлено
    assert matched_row["outcomes_matched_at"] is not None
    # Чужое — не тронуто
    assert other_row["outcomes_matched_at"] is None


# ---------------------------------------------------------------------------
# Тест 6: _build_windows строит правильное количество окон
# ---------------------------------------------------------------------------


def test_build_windows_count():
    """_build_windows создаёт корректные окна для months_back=1, batch_days=10."""
    windows = ao._build_windows(months_back=1, batch_days=10)
    # 1 месяц ≈ 30.44 дней / 10 = ~3 окна
    assert len(windows) >= 3
    # Каждое окно: (from_ts, to_ts) с from < to
    for from_ts, to_ts in windows:
        assert from_ts < to_ts


# ---------------------------------------------------------------------------
# Тест 7: ФИКС ДВОЙНОГО СЧЁТА — лид на границе окон засчитан ОДИН раз
# ---------------------------------------------------------------------------


def test_amo_outcomes_dedup_lead_across_windows(kb, monkeypatch):
    """Лид-оплата, попавший в ДВА соседних окна (overlap на границе created_at),
    не должен удваивать revenue/payments объявления.

    Воспроизводит корневую причину завышенного revenue/romi: окна batch_days
    смежные, AMO-фильтр created_at включает обе границы → один лид приходит дважды.
    Раньше revenue суммировался по окнам и платёж считался повторно.
    Теперь attach_amo_outcomes дедуплицирует по lead_id перед матчингом.
    """
    ad_name = "CityE | Клиент Игорь"
    _insert_ad(kb, "ad_igor", ad_name, spend=300.0)

    # Один и тот же лид-оплата 500000 — возвращается в КАЖДОМ окне (дубль на границе)
    payment_lead = {
        "id": 36246120,
        "name": "Сделка",
        "status_id": 143,
        "pipeline_id": 1,
        "price": 500000,
        "created_at": 1781153976,
        "contacts": [{"id": 41818335}],
        "custom_fields": [
            {"field_name": "fb_ad_name", "values": [{"value": ad_name}]},
        ],
    }

    def fake_get_leads_window(from_ts, to_ts):
        # Каждое окно отдаёт КОПИЮ одного и того же лида — имитируем overlap
        return [dict(payment_lead)]

    # Реальная логика матчинга и расчёта — НЕ мокаем, проверяем сквозной дедуп
    monkeypatch.setattr(ao, "get_leads_window", fake_get_leads_window)
    monkeypatch.setattr("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])

    # months_back=2, batch_days=30 → 2 окна, лид придёт ДВАЖДЫ
    result = ao.attach_amo_outcomes(months_back=2, batch_days=30)

    assert result["ads_updated"] == 1

    conn = ci._get_connection()
    try:
        row = conn.execute(
            "SELECT payments, revenue FROM creative_kb WHERE ad_id = 'ad_igor'"
        ).fetchone()
    finally:
        conn.close()

    # Платёж засчитан РОВНО ОДИН раз: revenue=500000 (а не 1000000), payments=1 (а не 2)
    assert row["payments"] == 1
    assert row["revenue"] == 500000


# ---------------------------------------------------------------------------
# Тест 8: windowed spend из ad_daily_metrics (Фаза 1б)
# ---------------------------------------------------------------------------


def _insert_daily_metric(db_path: str, ad_id: str, date_str: str, spend: float) -> None:
    """Вставляет строку в ad_daily_metrics для тестов."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO ad_daily_metrics (ad_id, date, spend)
            VALUES (?, ?, ?)
            ON CONFLICT(ad_id, date) DO UPDATE SET spend = excluded.spend
            """,
            (ad_id, date_str, spend),
        )
        conn.commit()
    finally:
        conn.close()


def test_amo_outcomes_spend_from_daily_metrics(kb, monkeypatch):
    """Spend берётся из ad_daily_metrics за окно (SUM), а не из creative_kb.spend.

    Сценарий: lifetime spend в KB = 1000, windowed spend в ad_daily_metrics = 200.
    Ожидаем что romi считается с windowed spend (200), а не lifetime (1000).
    """
    ad_name = "CityA | Windowed Test"
    _insert_ad(kb, "ad_wind", ad_name, spend=1000.0)  # lifetime = 1000

    # Вставляем дневные метрики за "недавние" дни (войдут в окно months_back=1)
    from datetime import date, timedelta
    today = date.today()
    _insert_daily_metric(kb, "ad_wind", (today - timedelta(days=5)).isoformat(), 120.0)
    _insert_daily_metric(kb, "ad_wind", (today - timedelta(days=3)).isoformat(), 80.0)
    # Итого windowed spend = 200.0

    # Мокаем AMO — одна оплата на это объявление
    def fake_get_leads_window(from_ts, to_ts):
        return [{
            "id": 9001,
            "name": "Тест",
            "status_id": 143,
            "price": 60000,
            "created_at": 1781000000,
            "contacts": [{"id": 12345}],
            "custom_fields": [
                {"field_name": "fb_ad_name", "values": [{"value": ad_name}]},
            ],
        }]

    monkeypatch.setattr(ao, "get_leads_window", fake_get_leads_window)
    monkeypatch.setattr("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])

    # Мокаем exchange rate: 100 ¤/$
    monkeypatch.setattr("services.exchange_rate.get_usd_to_lcy", lambda: 100.0)

    result = ao.attach_amo_outcomes(months_back=1, batch_days=31)
    assert result["ads_updated"] == 1

    conn = ci._get_connection()
    try:
        row = conn.execute(
            "SELECT romi, revenue FROM creative_kb WHERE ad_id = 'ad_wind'"
        ).fetchone()
    finally:
        conn.close()

    assert row["revenue"] == 60000
    # romi = 60000 / (200 * 100) * 100 = 300.0
    # (НЕ 60000 / (1000 * 100) * 100 = 60.0 — это было бы при lifetime spend)
    assert row["romi"] is not None
    assert abs(row["romi"] - 300.0) < 0.1, f"Ожидался romi=300.0, получили {row['romi']}"


def test_amo_outcomes_spend_fallback_when_no_daily_metrics(kb, monkeypatch):
    """Если для ad_id нет строк в ad_daily_metrics за окно — fallback на creative_kb.spend.

    Сценарий: в ad_daily_metrics нет строк → spend берётся из KB = 50.0.
    """
    ad_name = "CityB | Fallback Test"
    _insert_ad(kb, "ad_fall", ad_name, spend=50.0)  # lifetime = 50, данных в ad_daily_metrics нет

    def fake_get_leads_window(from_ts, to_ts):
        return [{
            "id": 9002,
            "name": "Тест",
            "status_id": 143,
            "price": 30000,
            "created_at": 1781000000,
            "contacts": [{"id": 99999}],
            "custom_fields": [
                {"field_name": "fb_ad_name", "values": [{"value": ad_name}]},
            ],
        }]

    monkeypatch.setattr(ao, "get_leads_window", fake_get_leads_window)
    monkeypatch.setattr("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])
    monkeypatch.setattr("services.exchange_rate.get_usd_to_lcy", lambda: 100.0)

    result = ao.attach_amo_outcomes(months_back=1, batch_days=31)
    assert result["ads_updated"] == 1

    conn = ci._get_connection()
    try:
        row = conn.execute(
            "SELECT romi FROM creative_kb WHERE ad_id = 'ad_fall'"
        ).fetchone()
    finally:
        conn.close()

    # romi = 30000 / (50 * 100) * 100 = 600.0 (fallback на lifetime spend=50)
    assert row["romi"] is not None
    assert abs(row["romi"] - 600.0) < 0.1, f"Ожидался romi=600.0, получили {row['romi']}"


# ---------------------------------------------------------------------------
# Тест 9: коллизия имён — два объявления с одинаковым именем, разные fb_ad_id
# ---------------------------------------------------------------------------


def test_amo_outcomes_collision_resolved_by_ad_id(kb, monkeypatch):
    """ГЛАВНЫЙ ТЕСТ: два объявления с одинаковым именем «Денис» в KB.
    Лиды несут разные fb_ad_id → каждая реклама получает СВОИ исходы.
    До фикса: оба валились на первое совпавшее имя.
    После фикса: разводятся по точному fb_ad_id.
    """
    # Два объявления с одним именем в KB — пример коллизии
    _insert_ad(kb, "6559403328503", "Денис", spend=200.0)
    _insert_ad(kb, "6502170510211", "Денис", spend=100.0)

    # Лиды несут разные fb_ad_id
    def fake_get_leads_window(from_ts, to_ts):
        leads = []
        # 3 лида с первым ad_id
        for i in range(3):
            leads.append({
                "id": 1000 + i,
                "name": "Тест",
                "status_id": 30,
                "price": 0,
                "created_at": 1700000000 + i,
                "contacts": [],
                "custom_fields": [
                    {"field_name": "fb_ad_name", "values": [{"value": "Денис"}]},
                    {"field_name": "fb_ad_id",   "values": [{"value": "6559403328503"}]},
                ],
            })
        # 2 лида со вторым ad_id
        for i in range(2):
            leads.append({
                "id": 2000 + i,
                "name": "Тест",
                "status_id": 30,
                "price": 0,
                "created_at": 1700001000 + i,
                "contacts": [],
                "custom_fields": [
                    {"field_name": "fb_ad_name", "values": [{"value": "Денис"}]},
                    {"field_name": "fb_ad_id",   "values": [{"value": "6502170510211"}]},
                ],
            })
        return leads

    monkeypatch.setattr(ao, "get_leads_window", fake_get_leads_window)
    monkeypatch.setattr("integrations.amo.AMO_PAYMENT_STATUS_IDS", [143])
    monkeypatch.setattr("services.exchange_rate.get_usd_to_lcy", lambda: 100.0)

    result = ao.attach_amo_outcomes(months_back=1, batch_days=31)

    # Оба обновлены
    assert result["ads_updated"] == 2

    conn = ci._get_connection()
    try:
        row_first = conn.execute(
            "SELECT qual_leads FROM creative_kb WHERE ad_id = '6559403328503'"
        ).fetchone()
        row_second = conn.execute(
            "SELECT qual_leads FROM creative_kb WHERE ad_id = '6502170510211'"
        ).fetchone()
    finally:
        conn.close()

    # Первое получило 3 лида, второе — 2 лида (не 5 и 0)
    assert row_first is not None and row_second is not None
    # qual_leads = 0 т.к. status_id=30 (новый), но total_leads должны быть разделены
    # Проверяем через ads_matched что каждый ad_id в matched
    assert result["ads_matched"] == 2, (
        f"Ожидалось 2 совпавших объявления (разведены по fb_ad_id), получили {result['ads_matched']}"
    )


# ---------------------------------------------------------------------------
# Тест 10: known_ad_ids передаётся в match_leads_to_ads — проверяем сигнатуру
# ---------------------------------------------------------------------------


def test_amo_outcomes_passes_known_ad_ids_to_match(kb, monkeypatch):
    """attach_amo_outcomes передаёт known_ad_ids в match_leads_to_ads.
    Это обеспечивает приоритетный матч по fb_ad_id в реальном флоу.
    """
    _insert_ad(kb, "ad_x", "Тест ад", spend=10.0)

    received_known: list = []

    def fake_get_leads_window(from_ts, to_ts):
        return []

    def fake_match(leads, fb_lookup, known_ad_ids=None):
        received_known.append(known_ad_ids)
        return {}

    def fake_calc(matched, ad_spends):
        return {}

    monkeypatch.setattr(ao, "get_leads_window", fake_get_leads_window)
    monkeypatch.setattr(ao, "match_leads_to_ads", fake_match)
    monkeypatch.setattr(ao, "calc_ad_metrics", fake_calc)

    ao.attach_amo_outcomes(months_back=1, batch_days=30)

    # match_leads_to_ads вызван хотя бы раз с known_ad_ids
    assert len(received_known) > 0
    last_known = received_known[-1]
    assert last_known is not None
    assert "ad_x" in last_known
