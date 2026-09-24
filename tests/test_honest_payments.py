"""
Тесты B1 — «честные данные»: outcomes_matched_at и семантика NULL vs 0.

Покрывают:
1) Правило is_confirmed_waster в services/decision_policy.score_and_decide:
   - без сверки (outcomes_matched_at=None) слив НЕ подтверждён, даже если payments=0
     и тир A/B выполнен по расходу/лидам/квалу;
   - со сверкой (outcomes_matched_at задан) и payments=0 при тир A/B — слив подтверждён,
     как и раньше;
   - payments=None (нет данных) — никогда не слив, даже со сверкой.
2) Миграцию 010 (migrations/010_null_unconfirmed_payments.sql), применяемую через
   services.creative_intelligence._apply_null_payments_migration на временной SQLite:
   - несверённые нули (outcomes_matched_at IS NULL) → NULL;
   - сверённые нули (outcomes_matched_at IS NOT NULL) → НЕ трогаются;
   - несверённые ненулевые значения → НЕ трогаются;
   - повторный прогон идемпотентен (0 изменённых строк).
3) Edge: ad-dict без ключа outcomes_matched_at (например из классического пути
   автопилота, который не читает creative_kb) не роняет score_and_decide и трактуется
   как «сверки не было».

Сеть не используется, внешние границы не участвуют — всё через временную SQLite
и чистые dict, мокать нечего.
"""

import sqlite3

import pytest

from services import creative_intelligence as ci
from services.decision_policy import score_and_decide


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_kb_path():
    """Сбрасываем глобальный DB_PATH creative_intelligence перед и после каждого теста
    (по образцу tests/test_creative_intelligence.py) — тесты не должны трогать
    реальную data/decisions.db."""
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


@pytest.fixture
def kb(tmp_path):
    """Инициализирует пустую KB во временной директории (со всеми миграциями,
    включая 010) и возвращает путь к файлу."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)
    return db_path


def _make_ad(
    ad_id: str = "ad_1",
    spend: float = 400.0,
    leads: int = 20,
    payments=0,
    qual_pct=None,
    outcomes_matched_at=None,
    **extra,
) -> dict:
    """Собирает минимальный ad-dict для score_and_decide с тир-A условием по умолчанию
    (spend>300, leads>=15) — удобно для тестов правила confirmed_waster."""
    ad = {
        "ad_id": ad_id,
        "ad_name": f"CityA | Тест / {ad_id}",
        "adset_id": f"adset_{ad_id}",
        "city": "CityA",
        "adset_type": "L2",
        "spend": spend,
        "leads": leads,
        "cpl": spend / leads if leads else 0,
        "ctr": 1.5,
        "hook_rate": 30.0,
        "qual_pct": qual_pct,
        "romi": None,
        "payments": payments,
        "days_running": 20,  # зрелый возраст: тир A требует >= 14 дн. (гейт цикла оплаты)
        "impressions": 10000,
        "video_views_3s": 0,
        "video_p25": 0,
        "video_p50": 0,
        "video_p75": 0,
        "video_p100": 0,
    }
    if outcomes_matched_at is not None or "outcomes_matched_at" in extra:
        ad["outcomes_matched_at"] = outcomes_matched_at
    ad.update(extra)
    return ad


# ---------------------------------------------------------------------------
# 1. Правило is_confirmed_waster в score_and_decide
# ---------------------------------------------------------------------------


def test_confirmed_waster_requires_outcomes_matched():
    """payments=0 + outcomes_matched_at=None (сверки не было) → is_confirmed_waster=False,
    даже при выполненном тир-A (spend>$300, leads>=15). Прибыльную рекламу с потерянной
    связкой CRM автопилот НЕ должен паузить как слив."""
    ad = _make_ad(ad_id="no_sync", spend=400.0, leads=20, payments=0,
                   qual_pct=None, outcomes_matched_at=None)

    results = score_and_decide([ad])
    result = next(r for r in results if r["ad_id"] == "no_sync")

    assert result["is_confirmed_waster"] is False, (
        f"Без сверки с AMO слив не должен подтверждаться: {result}"
    )
    # Раз слив не подтверждён — PAUSE-причина «подтверждённый слив» не должна фигурировать
    assert not any("подтверждённый слив" in r for r in result["reasons"]), result["reasons"]


def test_null_payments_not_paused():
    """payments=None (нет данных из AMO) — никогда не слив, даже если outcomes_matched_at
    задан (сверка была, но конкретно оплаты неизвестны — защитный payments==0 строгий,
    None != 0)."""
    ad = _make_ad(ad_id="null_payments", spend=400.0, leads=20, payments=None,
                   qual_pct=None, outcomes_matched_at="2026-06-01T10:00:00")

    results = score_and_decide([ad])
    result = next(r for r in results if r["ad_id"] == "null_payments")

    assert result["is_confirmed_waster"] is False, (
        f"payments=None не равно payments=0 — слив не должен подтверждаться: {result}"
    )


def test_matched_zero_payments_is_waster():
    """payments=0 + outcomes_matched_at задан (сверка реально прошла) + тир-A выполнен
    → is_confirmed_waster=True, PAUSE. Правило «слив» работает как раньше, когда данные
    подтверждены сверкой."""
    ad = _make_ad(ad_id="synced_zero", spend=400.0, leads=20, payments=0,
                   qual_pct=None, outcomes_matched_at="2026-06-01T10:00:00")
    companion = _make_ad(
        ad_id="synced_zero_companion",
        adset_id=ad["adset_id"],
        spend=30.0,
        leads=8,
        payments=1,
        qual_pct=25.0,
        outcomes_matched_at="2026-06-01T10:00:00",
    )

    results = score_and_decide([ad, companion])
    result = next(r for r in results if r["ad_id"] == "synced_zero")

    assert result["is_confirmed_waster"] is True, (
        f"Со сверкой и payments=0 при тир-A слив должен подтверждаться: {result}"
    )
    assert result["action"] == "PAUSE"
    assert any("подтверждённый слив" in r for r in result["reasons"]), result["reasons"]


def test_tier_b_matching_is_diagnostic_not_maturity_proof():
    """outcomes_matched_at подтверждает сверку tier B, но не зрелость лидов."""
    ad_no_sync = _make_ad(ad_id="tier_b_no_sync", spend=200.0, leads=5, payments=0,
                           qual_pct=5.0, outcomes_matched_at=None)
    ad_synced = _make_ad(ad_id="tier_b_synced", spend=200.0, leads=5, payments=0,
                          qual_pct=5.0, outcomes_matched_at="2026-06-01T10:00:00")

    results = score_and_decide([ad_no_sync, ad_synced])
    r_no_sync = next(r for r in results if r["ad_id"] == "tier_b_no_sync")
    r_synced = next(r for r in results if r["ad_id"] == "tier_b_synced")

    assert r_no_sync["is_confirmed_waster"] is False, r_no_sync
    assert r_no_sync["is_tier_b_diagnostic"] is False, r_no_sync
    assert r_synced["is_confirmed_waster"] is False, r_synced
    assert r_synced["is_tier_b_diagnostic"] is True, r_synced
    assert r_synced["action"] != "PAUSE", r_synced
    assert "тир b" in " ".join(r_synced["reasons"]).lower(), r_synced


def test_outcomes_matched_at_key_missing_defaults_to_not_confirmed():
    """Edge: ad-dict вообще БЕЗ ключа outcomes_matched_at (как приходит из классического
    пути автопилота _enrich_with_amo, который не читает creative_kb) не роняет
    score_and_decide и трактуется как «сверки не было» — is_confirmed_waster=False."""
    ad = _make_ad(ad_id="legacy_path", spend=400.0, leads=20, payments=0, qual_pct=None)
    assert "outcomes_matched_at" not in ad  # убеждаемся, что ключ реально отсутствует

    # Не должно бросать исключение (KeyError и т.п.)
    results = score_and_decide([ad])

    result = next(r for r in results if r["ad_id"] == "legacy_path")
    assert result["is_confirmed_waster"] is False, (
        f"Отсутствие ключа outcomes_matched_at должно трактоваться как None: {result}"
    )


# ---------------------------------------------------------------------------
# 2. Миграция 010 (обнуление несверённых нулей в NULL)
# ---------------------------------------------------------------------------


def _insert_row(db_path: str, ad_id: str, payments, qual_leads, revenue, outcomes_matched_at):
    """Вставляет строку creative_kb напрямую (минуя sync/backfill) для контроля
    исходного состояния перед прогоном миграции."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO creative_kb (ad_id, payments, qual_leads, revenue, outcomes_matched_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ad_id, payments, qual_leads, revenue, outcomes_matched_at),
        )
        conn.commit()
    finally:
        conn.close()


def _read_row(db_path: str, ad_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT payments, qual_leads, revenue, outcomes_matched_at FROM creative_kb WHERE ad_id = ?",
            (ad_id,),
        )
        row = cur.fetchone()
        assert row is not None, f"Строка {ad_id} должна существовать"
        return row
    finally:
        conn.close()


def test_migration_nulls_unconfirmed(kb):
    """payments=0 AND outcomes_matched_at IS NULL (несверённая строка) → после миграции
    payments/qual_leads/revenue становятся NULL («нет данных», а не «точно 0»)."""
    _insert_row(kb, "unconfirmed_zero", payments=0, qual_leads=0, revenue=0,
                outcomes_matched_at=None)

    conn = sqlite3.connect(kb)
    try:
        ci._apply_null_payments_migration(conn)
    finally:
        conn.close()

    row = _read_row(kb, "unconfirmed_zero")
    assert row["payments"] is None, "payments у несверённой строки с 0 должен стать NULL"
    assert row["qual_leads"] is None, "qual_leads у несверённой строки с 0 должен стать NULL"
    assert row["revenue"] is None, "revenue у несверённой строки с 0 должен стать NULL"


def test_migration_keeps_matched_zero(kb):
    """payments=0 AND outcomes_matched_at IS NOT NULL (сверка была, реально 0 оплат) →
    миграция НЕ трогает строку — 0 остаётся 0 (это настоящий факт, не «нет данных»)."""
    _insert_row(kb, "confirmed_zero", payments=0, qual_leads=0, revenue=0,
                outcomes_matched_at="2026-06-01T10:00:00")

    conn = sqlite3.connect(kb)
    try:
        ci._apply_null_payments_migration(conn)
    finally:
        conn.close()

    row = _read_row(kb, "confirmed_zero")
    assert row["payments"] == 0, "Сверенный 0 — реальный факт, миграция не должна его менять"
    assert row["qual_leads"] == 0
    assert row["revenue"] == 0
    assert row["outcomes_matched_at"] == "2026-06-01T10:00:00"


def test_migration_keeps_unconfirmed_nonzero(kb):
    """Несверённая строка с ненулевыми значениями (payments=5, qual_leads=2, revenue=100)
    — WHERE-условие миграции (payments/qual_leads/revenue = 0) не совпадает, значения
    остаются как есть, даже без сверки."""
    _insert_row(kb, "unconfirmed_nonzero", payments=5, qual_leads=2, revenue=100.0,
                outcomes_matched_at=None)

    conn = sqlite3.connect(kb)
    try:
        ci._apply_null_payments_migration(conn)
    finally:
        conn.close()

    row = _read_row(kb, "unconfirmed_nonzero")
    assert row["payments"] == 5
    assert row["qual_leads"] == 2
    assert row["revenue"] == 100.0


def test_migration_idempotent(kb):
    """Повторный прогон миграции после первого не находит подходящих строк —
    0 изменений (WHERE payments=0 больше не совпадает, т.к. уже NULL)."""
    _insert_row(kb, "idempotent_check", payments=0, qual_leads=0, revenue=0,
                outcomes_matched_at=None)

    conn = sqlite3.connect(kb)
    try:
        ci._apply_null_payments_migration(conn)  # первый прогон — обнуляет в NULL
        cur = conn.execute(
            "UPDATE creative_kb SET payments = NULL WHERE payments = 0 AND outcomes_matched_at IS NULL"
        )
        conn.commit()
        # Второй прогон миграции — 0 затронутых строк (проверяем через rowcount на
        # прямом повторе того же UPDATE, что и в SQL-файле)
        assert cur.rowcount == 0, "Второй запуск не должен находить уже обнулённые строки"

        ci._apply_null_payments_migration(conn)  # ещё раз через саму функцию — не должно падать
    finally:
        conn.close()

    row = _read_row(kb, "idempotent_check")
    assert row["payments"] is None
    assert row["qual_leads"] is None
    assert row["revenue"] is None


def test_migration_applied_automatically_via_init_kb(tmp_path):
    """init_kb (вызываемый при старте) сам регистрирует и применяет миграцию 010 —
    несверённый ноль, вставленный до повторного init_kb, обнуляется в NULL."""
    db_path = str(tmp_path / "auto.db")
    ci.init_kb(db_path)

    _insert_row(db_path, "auto_unconfirmed", payments=0, qual_leads=0, revenue=0,
                outcomes_matched_at=None)

    # Повторный init_kb (как при рестарте сервера) должен снова прогнать миграцию 010
    ci.DB_PATH = None
    ci.init_kb(db_path)

    row = _read_row(db_path, "auto_unconfirmed")
    assert row["payments"] is None, "init_kb должен применить миграцию 010 при (пере)старте"
