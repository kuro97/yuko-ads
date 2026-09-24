"""
Тесты честного 7d-refresh (Wave 3A):
  - services.amo_outcomes.refresh_amo_payments_7d (AMO)
  - services.cdp_payments.refresh_payments_erp_7d (ERP/CDP)
  - services.cdp_payments.seven_day_window_local (границы окна)
  - reader/freshness: is_amo_7d_fresh_complete / is_erp_7d_fresh_complete /
    payments_7d_effective / seven_d_window_confirmed

Покрывают (план ревью, Wave 3A):
  * окно РОВНО 7 календарных дней в едином TZ CityA, явные границы (вкл/искл);
  * платёж на границе окна вкл/искл предсказуемо;
  * полный refresh пишет НУЛИ известным ads без оплат (0 + complete=1);
  * partial/pagination/CDP-failure НЕ помечает snapshot полным и не затирает
    последнее валидное окно;
  * ошибка транзакции → откат, нет смеси старых/новых строк;
  * fresh/stale matrix для amo/shadow/erp.

Внешние границы (AMO get_leads_window/match/calc, CDP get_payments) — мокаются.
БД — временная SQLite через creative_intelligence.init_kb. Реальной сети нет.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from services import creative_intelligence as ci
from services import amo_outcomes as ao
from services import cdp_payments as cp
from services.cdp_client import CdpError

_TZ = timezone(timedelta(hours=5))
_NOW = datetime(2026, 7, 17, 10, 0, tzinfo=_TZ)


@pytest.fixture(autouse=True)
def reset_kb_path():
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


@pytest.fixture(autouse=True)
def isolate_lead_cache(tmp_path, monkeypatch):
    """Изолирует дисковый кеш 2-го яруса маппинга ERP и глушит точечный get_lead."""
    from unittest.mock import patch
    monkeypatch.setattr(cp, "_LEAD_CACHE_FILE", tmp_path / "lead_cache.json")
    with patch("integrations.amo.get_lead", return_value=None):
        yield


@pytest.fixture
def kb(tmp_path):
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)
    return db_path


def _insert_ad(db_path, ad_id, ad_name=""):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO creative_kb (ad_id, ad_name) VALUES (?, ?)", (ad_id, ad_name)
        )
        conn.commit()
    finally:
        conn.close()


def _row(db_path, ad_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM creative_kb WHERE ad_id = ?", (ad_id,)
        ).fetchone()
    finally:
        conn.close()


# ===========================================================================
# Окно ровно 7 дней (единый TZ CityA), явные границы
# ===========================================================================

def test_window_is_exactly_7_calendar_days():
    win = cp.seven_day_window_local(_NOW)
    assert win["window_from"] == "2026-07-10"   # включительно
    assert win["window_to"] == "2026-07-17"     # ИСКЛючительно (= сегодня)
    # ровно 7 суток минус 1 секунда (верхняя граница исключительна)
    assert win["amo_to_ts"] - win["amo_from_ts"] == 7 * 86400 - 1


def test_window_boundary_inclusive_exclusive():
    """Нижняя граница (window_from 00:00) ВХОДИТ; верхняя (window_to 00:00) НЕ входит."""
    win = cp.seven_day_window_local(_NOW)
    from_midnight = int(datetime(2026, 7, 10, 0, 0, tzinfo=_TZ).timestamp())
    to_midnight = int(datetime(2026, 7, 17, 0, 0, tzinfo=_TZ).timestamp())
    assert win["amo_from_ts"] == from_midnight            # включительно
    assert win["amo_to_ts"] < to_midnight                 # window_to 00:00 исключён
    # ERP по дате: последний включённый день — вчера (window_to − 1)
    assert win["cdp_date_from"] == date(2026, 7, 10)
    assert win["cdp_date_to"] == date(2026, 7, 16)


# ===========================================================================
# AMO 7d refresh
# ===========================================================================

def _patch_amo(monkeypatch, leads_fn, matched):
    monkeypatch.setattr(ao, "get_leads_window", leads_fn)
    monkeypatch.setattr(ao, "match_leads_to_ads", lambda leads, fb, known=None: matched)
    monkeypatch.setattr(
        ao, "calc_ad_metrics",
        lambda m, spends: {aid: {"payments": d["payments"], "revenue": d["revenue"]} for aid, d in m.items()},
    )


def test_amo_full_refresh_writes_zeros_for_known_ads(kb, monkeypatch):
    """Полный успешный refresh: победитель получает 2 оплаты, известный ad без
    оплат → 0 + complete=1 (подтверждённый ноль окна, НЕ NULL)."""
    _insert_ad(kb, "ad_win", "Win")
    _insert_ad(kb, "ad_zero", "Zero")

    _patch_amo(
        monkeypatch,
        lambda from_ts, to_ts: [{"id": 1}],
        {"ad_win": {"payments": 2, "revenue": 500000.0}},
    )
    res = ao.refresh_amo_payments_7d(now=_NOW)

    assert res["complete"] is True
    assert res["ads_updated"] == 2
    assert res["window_from"] == "2026-07-10" and res["window_to"] == "2026-07-17"

    win = _row(kb, "ad_win")
    assert win["payments_amo_7d"] == 2
    assert win["revenue_amo_7d"] == 500000.0
    assert win["amo_7d_complete"] == 1

    zero = _row(kb, "ad_zero")
    assert zero["payments_amo_7d"] == 0     # подтверждённый ноль, НЕ NULL
    assert zero["amo_7d_complete"] == 1


def test_amo_refresh_passes_exact_window_bounds(kb, monkeypatch):
    """get_leads_window вызывается с границами seven_day_window_local(now)."""
    _insert_ad(kb, "ad_1", "A")
    captured = {}

    def _leads(from_ts, to_ts):
        captured["from_ts"], captured["to_ts"] = from_ts, to_ts
        return []

    _patch_amo(monkeypatch, _leads, {})
    ao.refresh_amo_payments_7d(now=_NOW)

    win = cp.seven_day_window_local(_NOW)
    assert captured["from_ts"] == win["amo_from_ts"]
    assert captured["to_ts"] == win["amo_to_ts"]


def test_amo_pagination_failure_not_complete_and_preserves_last_window(kb, monkeypatch):
    """get_leads_window бросает (пагинация/timeout) → снимок НЕ помечен полным,
    прошлый валидный снимок не затёрт, synced_at не сдвинут."""
    _insert_ad(kb, "ad_1", "A")

    # 1) Валидный refresh для окна прошлой даты — сохраняем «последнее валидное».
    prev_now = datetime(2026, 7, 16, 10, 0, tzinfo=_TZ)
    _patch_amo(monkeypatch, lambda f, t: [{"id": 1}], {"ad_1": {"payments": 5, "revenue": 111.0}})
    ok = ao.refresh_amo_payments_7d(now=prev_now)
    assert ok["complete"] is True
    before = _row(kb, "ad_1")
    assert before["payments_amo_7d"] == 5

    # 2) Следующий прогон падает на get_leads_window → fail-closed.
    def _boom(from_ts, to_ts):
        raise RuntimeError("pagination boom")

    monkeypatch.setattr(ao, "get_leads_window", _boom)
    bad = ao.refresh_amo_payments_7d(now=_NOW)
    assert bad["complete"] is False
    assert bad["error"] is not None

    after = _row(kb, "ad_1")
    # Прошлый валидный снимок цел (значение, окно, synced_at не тронуты).
    assert after["payments_amo_7d"] == 5
    assert after["amo_7d_window_to"] == before["amo_7d_window_to"]
    assert after["amo_7d_synced_at"] == before["amo_7d_synced_at"]


def test_amo_transaction_error_rolls_back_no_mix(kb, monkeypatch):
    """Ошибка на середине записи → rollback: нет смеси старых/новых строк,
    прошлый валидный снимок сохранён, complete не выставлен."""
    _insert_ad(kb, "ad_1", "A")
    _insert_ad(kb, "ad_2", "B")
    _insert_ad(kb, "ad_3", "C")

    # Валидный снимок окна прошлой даты.
    prev_now = datetime(2026, 7, 16, 10, 0, tzinfo=_TZ)
    _patch_amo(monkeypatch, lambda f, t: [{"id": 1}], {"ad_1": {"payments": 9, "revenue": 1.0}})
    ao.refresh_amo_payments_7d(now=prev_now)
    assert _row(kb, "ad_1")["payments_amo_7d"] == 9

    # Новый прогон (новое окно) с падением на 2-й UPDATE → откат всей транзакции.
    _patch_amo(monkeypatch, lambda f, t: [{"id": 2}], {"ad_1": {"payments": 99, "revenue": 2.0}})
    real_get_conn = ci._get_connection

    class _FailOn2ndUpdate:
        def __init__(self, real):
            self._real = real
            self._updates = 0

        def execute(self, sql, *a, **k):
            if sql.strip().upper().startswith("UPDATE"):
                self._updates += 1
                if self._updates == 2:
                    raise sqlite3.OperationalError("boom mid-transaction")
            return self._real.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._real, name)

    # Первый вызов _get_connection в refresh — чтение известных ad_id (пусть настоящий).
    # Второй — запись; его оборачиваем в падающий на 2-м UPDATE.
    calls = {"n": 0}

    def _fake_get_conn():
        calls["n"] += 1
        real = real_get_conn()
        return real if calls["n"] == 1 else _FailOn2ndUpdate(real)

    monkeypatch.setattr(ao, "_get_connection", _fake_get_conn)
    res = ao.refresh_amo_payments_7d(now=_NOW)

    assert res["complete"] is False
    assert res["error"] is not None
    # Никакой строки не переписано на новое окно — прошлый снимок цел (нет смеси).
    r1 = _row(kb, "ad_1")
    assert r1["payments_amo_7d"] == 9                 # старое значение, не 99
    assert r1["amo_7d_window_to"] == "2026-07-16"     # старое окно, не 2026-07-17


# ===========================================================================
# ERP 7d refresh
# ===========================================================================

def _lead(lid, ad):
    return {"id": lid, "custom_fields": [
        {"field_id": 902422, "field_name": "fb_ad_id", "values": [{"value": str(ad)}]}]}


def _pay(pid, cn, amount=10000.0, direction="income", doc_date="2026-07-14"):
    return {"id": pid, "deal_id": 900000 + cn, "contract_number": cn,
            "amount": amount, "direction": direction, "doc_date": doc_date}


def test_erp_full_refresh_writes_zeros_for_known_ads(kb, monkeypatch):
    from unittest.mock import patch
    _insert_ad(kb, "ad_win", "Win")
    _insert_ad(kb, "ad_zero", "Zero")

    with patch("integrations.amo.get_leads_window", return_value=[_lead(101, "ad_win")]), \
         patch("services.cdp_client.get_payments", return_value=[_pay(1, 101, 80000.0)]):
        res = cp.refresh_payments_erp_7d(now=_NOW)

    assert res["complete"] is True
    assert res["ads_updated"] == 2

    win = _row(kb, "ad_win")
    assert win["payments_erp_7d"] == 1
    assert win["revenue_erp_7d"] == 80000.0
    assert win["erp_7d_complete"] == 1

    zero = _row(kb, "ad_zero")
    assert zero["payments_erp_7d"] == 0      # подтверждённый ноль
    assert zero["erp_7d_complete"] == 1


def test_erp_refresh_boundary_by_doc_date(kb, monkeypatch):
    """Платежи на границе окна включаются/исключаются предсказуемо: get_payments
    вызывается с [cdp_date_from, cdp_date_to] и фильтрует по doc_date."""
    from unittest.mock import patch
    _insert_ad(kb, "ad_win", "Win")

    # Мастер-список: на нижней границе (вкл), на последнем дне (вкл), на window_to
    # (= сегодня, ИСКЛючён — get_payments зовётся с cdp_date_to = window_to − 1).
    master = [
        _pay(1, 101, 10000.0, doc_date="2026-07-10"),   # cdp_date_from → вкл
        _pay(2, 101, 20000.0, doc_date="2026-07-16"),   # cdp_date_to → вкл
        _pay(3, 101, 40000.0, doc_date="2026-07-17"),   # window_to (сегодня) → искл
    ]

    def _get_payments(date_from, date_to, direction=None):
        return [p for p in master if date_from <= date.fromisoformat(p["doc_date"]) <= date_to]

    with patch("integrations.amo.get_leads_window", return_value=[_lead(101, "ad_win")]), \
         patch("services.cdp_client.get_payments", side_effect=_get_payments):
        res = cp.refresh_payments_erp_7d(now=_NOW)

    assert res["complete"] is True
    win = _row(kb, "ad_win")
    # Учтены только платежи 10-го и 16-го (30000), платёж 17-го (сегодня) исключён.
    assert win["revenue_erp_7d"] == 30000.0


def test_erp_cdp_error_not_complete_preserves_snapshot(kb, monkeypatch):
    """CdpError → снимок НЕ полный, прошлый валидный снимок не затёрт."""
    from unittest.mock import patch
    _insert_ad(kb, "ad_1", "A")

    prev_now = datetime(2026, 7, 16, 10, 0, tzinfo=_TZ)
    with patch("integrations.amo.get_leads_window", return_value=[_lead(101, "ad_1")]), \
         patch("services.cdp_client.get_payments", return_value=[_pay(1, 101, 55000.0)]):
        ok = cp.refresh_payments_erp_7d(now=prev_now)
    assert ok["complete"] is True
    before = _row(kb, "ad_1")
    assert before["payments_erp_7d"] == 1

    with patch("integrations.amo.get_leads_window", return_value=[_lead(101, "ad_1")]), \
         patch("services.cdp_client.get_payments", side_effect=CdpError("CDP down")):
        bad = cp.refresh_payments_erp_7d(now=_NOW)
    assert bad["complete"] is False

    after = _row(kb, "ad_1")
    assert after["payments_erp_7d"] == 1     # прошлый валидный снимок цел
    assert after["erp_7d_window_to"] == before["erp_7d_window_to"]
    assert after["erp_7d_synced_at"] == before["erp_7d_synced_at"]


def test_erp_partial_attribution_not_complete(kb, monkeypatch):
    """Платежи в окне есть, но ни один не сопоставлен (AMO недоступна) → fail-closed:
    снимок НЕ полный, ложных нулей не пишем."""
    from unittest.mock import patch
    _insert_ad(kb, "ad_1", "A")

    with patch("integrations.amo.get_leads_window", side_effect=RuntimeError("amo down")), \
         patch("services.cdp_client.get_payments", return_value=[_pay(1, 999, 5000.0)]):
        res = cp.refresh_payments_erp_7d(now=_NOW)

    assert res["complete"] is False
    assert res["error"] is not None
    row = _row(kb, "ad_1")
    assert row["payments_erp_7d"] is None    # ложный ноль НЕ записан
    assert row["erp_7d_complete"] is None


def test_erp_empty_payments_is_complete_all_zeros(kb, monkeypatch):
    """Платежей в окне нет (пустой список от CDP) — это ПОЛНЫЙ успешный refresh:
    все известные ads получают 0 + complete=1."""
    from unittest.mock import patch
    _insert_ad(kb, "ad_1", "A")
    _insert_ad(kb, "ad_2", "B")

    with patch("integrations.amo.get_leads_window", return_value=[]), \
         patch("services.cdp_client.get_payments", return_value=[]):
        res = cp.refresh_payments_erp_7d(now=_NOW)

    assert res["complete"] is True
    for aid in ("ad_1", "ad_2"):
        row = _row(kb, aid)
        assert row["payments_erp_7d"] == 0
        assert row["erp_7d_complete"] == 1


# ===========================================================================
# Fresh/stale reader matrix (amo / shadow / erp)
# ===========================================================================

def _fresh_amo(**extra):
    ad = {"payments_amo_7d": 3, "amo_7d_complete": 1,
          "amo_7d_window_from": "2026-07-10", "amo_7d_window_to": "2026-07-17"}
    ad.update(extra)
    return ad


def _fresh_c1(**extra):
    ad = {"payments_erp_7d": 4, "erp_7d_complete": 1,
          "erp_7d_window_from": "2026-07-10", "erp_7d_window_to": "2026-07-17"}
    ad.update(extra)
    return ad


def test_reader_fresh_amo_is_confirmed():
    ad = _fresh_amo()
    assert cp.is_amo_7d_fresh_complete(ad, _NOW) is True
    assert cp.seven_d_window_confirmed([ad], "amo", _NOW) is True
    assert cp.seven_d_window_confirmed([ad], "shadow", _NOW) is True
    assert cp.payments_7d_effective(ad, "amo", _NOW) == 3


def test_reader_stale_window_not_confirmed():
    """Снимок полный, но окно ДРУГОЕ (прошлое) → не свежий → не подтверждён."""
    ad = _fresh_amo(amo_7d_window_from="2026-07-01", amo_7d_window_to="2026-07-08")
    assert cp.is_amo_7d_fresh_complete(ad, _NOW) is False
    assert cp.seven_d_window_confirmed([ad], "amo", _NOW) is False
    assert cp.payments_7d_effective(ad, "amo", _NOW) is None


def test_reader_incomplete_snapshot_not_confirmed():
    """complete=0/NULL → не подтверждён, даже если окно совпадает."""
    ad = _fresh_amo(amo_7d_complete=0)
    assert cp.is_amo_7d_fresh_complete(ad, _NOW) is False
    assert cp.payments_7d_effective(ad, "amo", _NOW) is None


def test_reader_shadow_ignores_erp():
    """shadow-режим: решение по AMO 7d, ERP 7d НЕ влияет — свежая ERP без свежей AMO
    не подтверждает окно."""
    ad = _fresh_c1()  # только ERP свежая, AMO нет
    assert cp.seven_d_window_confirmed([ad], "shadow", _NOW) is False
    assert cp.payments_7d_effective(ad, "shadow", _NOW) is None


def test_reader_erp_mode_max_among_fresh():
    """erp-режим: max(AMO 7d, ERP 7d) среди свежих полных источников."""
    ad = {**_fresh_amo(payments_amo_7d=2), **_fresh_c1(payments_erp_7d=5)}
    assert cp.payments_7d_effective(ad, "erp", _NOW) == 5
    assert cp.seven_d_window_confirmed([ad], "erp", _NOW) is True


def test_reader_erp_mode_only_erp_fresh():
    """erp-режим: AMO не свежая, ERP свежая → сигнал = ERP, окно подтверждено."""
    ad = _fresh_c1(payments_erp_7d=4)  # AMO-колонок нет → AMO не свежая
    assert cp.payments_7d_effective(ad, "erp", _NOW) == 4
    assert cp.seven_d_window_confirmed([ad], "erp", _NOW) is True


def test_reader_erp_mode_neither_fresh_fail_closed():
    """erp-режим: ни AMO, ни ERP не свежие → None (fail-closed), окно не подтверждено."""
    ad = {"payments_amo_7d": None, "payments_erp_7d": None}
    assert cp.payments_7d_effective(ad, "erp", _NOW) is None
    assert cp.seven_d_window_confirmed([ad], "erp", _NOW) is False
