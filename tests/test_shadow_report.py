"""
Тесты services/shadow_report.py.

Рассылка «🔮 Тень — что бы я сделал сегодня» (build_shadow_recommendations,
format_shadow_report, send_shadow_report, should_send_shadow_report,
_load_shadow_state/_save_shadow_state) удалена по решению владельца —
дублировала и противоречила боевому контуру run_autopilot_live. Соответствующие
тесты убраны как покрывающие снятую функциональность.

_fetch_ads_from_local_db остаётся расчётной функцией — её используют
decision_policy-потребители (autopilot/budget_scaler/guardian/coverage_monitor/
brief_generator), поэтому тест на неё сохранён и должен оставаться зелёным.

Волна 2 недельных когорт добавила теневую секцию тренда (build_trend_shadow /
format_trend_shadow). Её тесты проверяют главное свойство тени: она читает и
рендерит, но ничего не предлагает, не мутирует и не отправляет, а при неполных
данных не выдаёт ни одного числа.

Волна 4 добавила в ту же секцию ROMI когорты. Её тесты следят за тем, чтобы
зрелость выручки была написана словами, а зреющая когорта не выглядела как
«ROMI 0%» — пустая касса первой недели это «деньги ещё не пришли».
"""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()


# ---------------------------------------------------------------------------
# _fetch_ads_from_local_db — реальный SQLite с нужными данными
# ---------------------------------------------------------------------------

def test_fetch_ads_from_local_db_with_sqlite(tmp_path, monkeypatch):
    """_fetch_ads_from_local_db корректно читает creative_kb + ad_daily_metrics."""
    import sqlite3
    import services.shadow_report as sr_module

    # Создаём тестовую БД с реальной схемой (effective_status, без hold_rate в ad_daily_metrics)
    db_path = str(tmp_path / "test_kb.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE creative_kb (
            ad_id TEXT PRIMARY KEY,
            ad_name TEXT DEFAULT '',
            city TEXT DEFAULT '',
            adset_type TEXT DEFAULT '',
            status TEXT DEFAULT '',
            effective_status TEXT DEFAULT '',
            spend REAL DEFAULT 0,
            leads INTEGER DEFAULT 0,
            qual_pct REAL,
            romi REAL,
            cpl REAL DEFAULT 0,
            ctr REAL DEFAULT 0,
            hook_rate REAL,
            impressions INTEGER DEFAULT 0,
            video_p25 INTEGER DEFAULT 0,
            video_p100 INTEGER DEFAULT 0,
            video_views_3s INTEGER DEFAULT 0,
            qual_leads INTEGER DEFAULT 0,
            payments INTEGER DEFAULT 0,
            outcomes_matched_at TEXT,
            days_running INTEGER DEFAULT 0,
            payments_erp INTEGER,
            revenue_erp_lcy REAL,
            payments_erp_synced_at TEXT
        );
        CREATE TABLE ad_daily_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id TEXT NOT NULL,
            date TEXT NOT NULL,
            leads INTEGER DEFAULT 0,
            day_since_launch INTEGER DEFAULT 0,
            spend REAL DEFAULT 0,
            impressions INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0,
            ctr REAL DEFAULT 0,
            cpl REAL DEFAULT 0,
            hook_rate REAL,
            video_views_3s INTEGER DEFAULT 0,
            lead_semantics_version INTEGER NOT NULL DEFAULT 1,
            lead_parse_status TEXT NOT NULL DEFAULT 'legacy',
            created_at TEXT,
            UNIQUE(ad_id, date)
        );
        INSERT INTO creative_kb
            (ad_id, ad_name, city, adset_type, status, effective_status, spend, leads, cpl, days_running)
            VALUES ('ad_active_1', 'Активное объявление 1', 'CityA', 'L2', 'ACTIVE', 'ACTIVE', 500.0, 10, 50.0, 7);
        INSERT INTO creative_kb
            (ad_id, ad_name, city, adset_type, status, effective_status, spend, leads, cpl, days_running)
            VALUES ('ad_active_2', 'Активное объявление 2', 'CityC', 'L1', 'ACTIVE', 'ACTIVE', 200.0, 3, 66.0, 4);
        INSERT INTO creative_kb
            (ad_id, ad_name, city, adset_type, status, effective_status, spend, leads, cpl, days_running)
            VALUES ('ad_paused', 'Паузное объявление', 'CityA', 'L2', 'PAUSED', 'PAUSED', 100.0, 5, 20.0, 15);
        INSERT INTO ad_daily_metrics
            (ad_id, date, leads, day_since_launch, lead_semantics_version, lead_parse_status)
            VALUES ('ad_active_1', '2026-06-20', 2, 1, 2, 'ok');
        INSERT INTO ad_daily_metrics
            (ad_id, date, leads, day_since_launch, lead_semantics_version, lead_parse_status)
            VALUES ('ad_active_1', '2026-06-21', 1, 2, 2, 'ok');
        INSERT INTO ad_daily_metrics
            (ad_id, date, leads, day_since_launch, lead_semantics_version, lead_parse_status)
            VALUES ('ad_active_1', '2026-06-22', 0, 3, 2, 'ok');
        INSERT INTO ad_daily_metrics
            (ad_id, date, leads, day_since_launch, lead_semantics_version, lead_parse_status)
            VALUES ('ad_active_1', '2026-06-23', 4, 4, 2, 'ok');
    """)
    conn.close()

    # Подменяем DB_PATH
    with patch("services.creative_intelligence.DB_PATH", db_path):
        result = sr_module._fetch_ads_from_local_db()

    # Только активные объявления
    assert len(result) == 2
    ids = {ad["ad_id"] for ad in result}
    assert "ad_active_1" in ids
    assert "ad_active_2" in ids
    assert "ad_paused" not in ids

    # Ранние лиды: день 1+2+3 = 2+1+0 = 3 (день 4 не входит, day_since_launch=4 > 3)
    ad1 = next(ad for ad in result if ad["ad_id"] == "ad_active_1")
    assert ad1.get("early_leads") == 3

    # ad_active_2 нет в daily_metrics — поле early_leads отсутствует
    ad2 = next(ad for ad in result if ad["ad_id"] == "ad_active_2")
    assert "early_leads" not in ad2

    # Все ключи нужные для score_and_decide на месте
    for key in ("ad_id", "ad_name", "city", "adset_type", "spend", "leads", "cpl",
                "ctr", "impressions", "days_running", "effective_status"):
        assert key in ad1, f"отсутствует ключ {key}"


def test_fetch_ads_from_local_db_empty_when_db_path_none(monkeypatch):
    """DB_PATH не инициализирован -> пустой список, без исключения."""
    import services.shadow_report as sr_module

    with patch("services.creative_intelligence.DB_PATH", None):
        result = sr_module._fetch_ads_from_local_db()

    assert result == []


def test_fetch_ads_from_local_db_no_shadow_send_functions():
    """Функции рассылки «Тень» удалены из модуля (сняты)."""
    import services.shadow_report as sr_module

    for removed_name in (
        "build_shadow_recommendations",
        "format_shadow_report",
        "send_shadow_report",
        "should_send_shadow_report",
        "_load_shadow_state",
        "_save_shadow_state",
    ):
        assert not hasattr(sr_module, removed_name), (
            f"{removed_name} должна быть удалена вместе с рассылкой теневого отчёта"
        )


# ---------------------------------------------------------------------------
# Теневая секция тренда (волна 2 недельных когорт)
# ---------------------------------------------------------------------------

# «Сегодня» тестов — понедельник 27.07.2026. Недели ниже дозрели все.
TREND_AS_OF = date(2026, 7, 27)
TREND_W3 = date(2026, 6, 29)   # 29.06–05.07
TREND_W2 = date(2026, 7, 6)    # 06–12.07
TREND_W1 = date(2026, 7, 13)   # 13–19.07
# Денежная часть когорт (волна 4): горизонт выручки и курс недели.
TREND_HORIZON = 14
TREND_RATE = 500.0


@pytest.fixture
def cohorts_db(tmp_path):
    """Временная KB со схемой миграции 024 и набором недельных когорт.

    Три адсета: выдыхающийся (три недели вниз), ровный (шум) и мелкий
    (объёма не хватает) — плюс адсет с дырой в данных недели.
    """
    import services.creative_intelligence as ci

    ci.DB_PATH = None
    db_path = str(tmp_path / "cohorts.db")
    ci.init_kb(db_path)

    def add(ad_id, adset_id, adset_name, week_start, leads, quals, spend,
            comparable=1, reason=None, revenue_lcy=None, revenue_mature=0):
        # Денежная часть (миграция 025): горизонт и курс пишутся вместе с
        # выручкой, недозревшая когорта остаётся с NULL — как в сборщике.
        conn.execute(
            """
            INSERT INTO ad_weekly_cohorts (
                ad_id, week_start, adset_id, adset_name, ad_name, city,
                spend_usd, impressions, fb_leads, amo_leads, quals,
                days_covered, days_expected, comparable, not_comparable_reason,
                builder_version, computed_at,
                revenue_lcy, payments, revenue_horizon_days, revenue_mature,
                usd_lcy_rate, romi_pct
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ad_id, week_start.isoformat(), adset_id, adset_name,
                f"CityA | Диагностика / {ad_id}", "CityA",
                spend, 10000 if comparable else None,
                leads if comparable else None, leads, quals,
                7 if comparable else 5, 7, comparable, reason,
                2, "2026-07-31T09:00:00+00:00",
                revenue_lcy,
                1 if revenue_lcy else 0,
                TREND_HORIZON if revenue_lcy is not None else None,
                revenue_mature,
                TREND_RATE if revenue_lcy is not None else None,
                (
                    round(revenue_lcy / (spend * TREND_RATE) * 100, 1)
                    if revenue_lcy is not None and revenue_mature and spend
                    else None
                ),
            ),
        )

    conn = ci._get_connection()
    try:
        # Выдыхающаяся звезда: 40% → 28% → 16%, два объявления по 50 лидов.
        # Когорты дозрели, деньги известны и падают вслед за квалом.
        revenue_by_week = {
            TREND_W3: 4_000_000.0, TREND_W2: 2_800_000.0, TREND_W1: 900_000.0,
        }
        for week_start, qual_pct in ((TREND_W3, 40), (TREND_W2, 28), (TREND_W1, 16)):
            add("ad-a1", "adset-A", "CityA | PRODA | Онлайн", week_start,
                50, qual_pct // 2, 600.0,
                revenue_lcy=revenue_by_week[week_start] / 2, revenue_mature=1)
            add("ad-a2", "adset-A", "CityA | PRODA | Онлайн", week_start,
                50, qual_pct // 2, 650.0,
                revenue_lcy=revenue_by_week[week_start] / 2, revenue_mature=1)
        # Ровный адсет: 20% → 22% → 19% — недельный шум. Выручки нет вовсе:
        # так выглядит неделя, у которой когорта ещё зреет.
        for week_start, quals in ((TREND_W3, 20), (TREND_W2, 22), (TREND_W1, 19)):
            add("ad-b1", "adset-B", "CityB | PRODB | Офлайн", week_start,
                100, quals, 900.0)
        # Мелкий адсет: 12 лидов в неделю — судить не о чем.
        for week_start, quals in ((TREND_W2, 4), (TREND_W1, 3)):
            add("ad-c1", "adset-C", "CityC | PRODA", week_start, 12, quals, 120.0)
        # Дыра в данных последней недели: FB не отдал день.
        add("ad-d1", "adset-D", "CityA | PRODB", TREND_W2, 80, 24, 700.0)
        add("ad-d1", "adset-D", "CityA | PRODB", TREND_W1, 80, 8, None,
            comparable=0, reason="FB_DAYS_MISSING:2026-07-15")
        conn.commit()
    finally:
        conn.close()

    yield db_path
    ci.DB_PATH = None


def test_build_trend_shadow_judges_adsets_from_cohorts(cohorts_db):
    """Секция тренда читает когорты и судит адсеты, а не объявления."""
    import services.shadow_report as sr_module
    from services.trend_policy import TrendLevel, TrendStatus

    with patch("services.creative_intelligence.DB_PATH", cohorts_db):
        verdicts = sr_module.build_trend_shadow(as_of=TREND_AS_OF)

    by_id = {verdict.entity_id: verdict for verdict in verdicts}
    assert set(by_id) == {"adset-A", "adset-B", "adset-C", "adset-D"}
    assert all(verdict.level is TrendLevel.ADSET for verdict in verdicts)

    fading = by_id["adset-A"]
    assert fading.status is TrendStatus.DECLINE
    assert fading.action_grade is True
    # Объявления недели сложены в адсет: 50 + 50.
    assert fading.recent.leads == 100
    assert fading.previous.week_start == TREND_W2
    assert fading.recent.week_start == TREND_W1

    assert by_id["adset-B"].status is TrendStatus.PLATEAU
    assert by_id["adset-C"].status is TrendStatus.INSUFFICIENT
    assert by_id["adset-C"].reason.startswith("VOLUME_BELOW_MIN")
    # Первым идёт то, на чём вообще можно было бы действовать.
    assert verdicts[0].entity_id == "adset-A"


def test_trend_shadow_renders_owner_format(cohorts_db):
    """Рендер по эталону проекта: недели, объёмы, деньги через fmt_money."""
    import services.shadow_report as sr_module

    with patch("services.creative_intelligence.DB_PATH", cohorts_db):
        text = sr_module.format_trend_shadow(
            sr_module.build_trend_shadow(as_of=TREND_AS_OF)
        )

    assert "тень: смотрю, ничего не предлагаю и не меняю" in text
    assert "1. CityA | PRODA | Онлайн" in text
    assert "📉 ПАДЕНИЕ · квал 28% → 16% (-12,0 п.п.)" in text
    # Сравниваемые недели с возрастом и объёмы — обязательная часть вывода.
    assert "📅 недели 06.07–12.07 → 13.07–19.07 · возраст 8 дн" in text
    assert "👥 100 → 100 лидов" in text
    # Деньги — через fmt_money (разделитель тысяч — точка, валюта рядом).
    assert "💸 $1.250 → $1.250" in text
    assert "CPL $12.5 → $12.5" in text
    assert "три недели подряд в одну сторону: 40% → 28% → 16%" in text
    assert "порог шума 13,8 п.п." in text
    # Ровный адсет показан как ПЛАТО с названной причиной.
    assert "➖ ПЛАТО" in text
    assert "разница в пределах недельного шума (3,0 из 13,8 п.п.)" in text


def test_trend_shadow_gives_no_numbers_for_incomplete_weeks(cohorts_db):
    """comparable=0 и малый объём — только сводка причин, ни одного числа."""
    import services.shadow_report as sr_module
    from services.trend_policy import TrendStatus

    with patch("services.creative_intelligence.DB_PATH", cohorts_db):
        verdicts = sr_module.build_trend_shadow(as_of=TREND_AS_OF)
        text = sr_module.format_trend_shadow(verdicts)

    broken = next(v for v in verdicts if v.entity_id == "adset-D")
    assert broken.status is TrendStatus.INSUFFICIENT
    assert broken.reason.startswith("WEEK_NOT_COMPARABLE")
    assert broken.delta_pp is None and broken.noise_floor_pp is None

    assert "❔ Мало данных: 2 —" in text
    assert "неполные данные недели 1" in text
    assert "меньше 20 лидов в неделю 1" in text
    # Ни адсет с дырой, ни мелкий адсет не получают блока с цифрами.
    assert "CityA | PRODB\n" not in text
    assert "CityC | PRODA\n" not in text


def test_trend_shadow_shows_romi_with_maturity(cohorts_db):
    """ROMI показан рядом с квалом и всегда со зрелостью когорты."""
    import services.shadow_report as sr_module

    with patch("services.creative_intelligence.DB_PATH", cohorts_db):
        text = sr_module.format_trend_shadow(
            sr_module.build_trend_shadow(as_of=TREND_AS_OF)
        )

    # Адсет A: 2,8 млн ¤ на $1250 и 0,9 млн ¤ на $1250 при курсе 500.
    previous_romi = round(2_800_000.0 / (1250.0 * TREND_RATE) * 100)
    recent_romi = round(900_000.0 / (1250.0 * TREND_RATE) * 100)
    assert f"ROMI {previous_romi}% → {recent_romi}%" in text
    assert "выручка ¤ 900.000" in text
    assert f"выручка дозрела: {TREND_HORIZON} из {TREND_HORIZON} дней" in text
    # ROMI справочный — об этом сказано и в подзаголовке, и в самой строке.
    assert "справочно" in text
    assert "порог шума" in text


def test_trend_shadow_does_not_show_zero_romi_for_maturing_cohort(cohorts_db):
    """Пустая касса зреющей когорты — «ещё зреет», а не ROMI 0%."""
    import services.shadow_report as sr_module

    with patch("services.creative_intelligence.DB_PATH", cohorts_db):
        verdicts = sr_module.build_trend_shadow(as_of=TREND_AS_OF)
        text = sr_module.format_trend_shadow(verdicts)

    plateau = next(v for v in verdicts if v.entity_id == "adset-B")
    assert plateau.romi_recent is None
    assert plateau.romi_change_pp is None
    assert "ROMI: нет данных · когорта ещё зреет" in text
    assert "ROMI 0% → 0%" not in text


def test_trend_shadow_reads_db_without_migration_025(tmp_path):
    """БД без денежных колонок читается как «выручка неизвестна», без падения."""
    import sqlite3

    import services.shadow_report as sr_module

    db_path = str(tmp_path / "old-schema.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE ad_weekly_cohorts (
                ad_id TEXT NOT NULL,
                week_start TEXT NOT NULL,
                adset_id TEXT,
                adset_name TEXT,
                ad_name TEXT,
                city TEXT,
                spend_usd REAL,
                impressions INTEGER,
                fb_leads INTEGER,
                amo_leads INTEGER,
                quals INTEGER,
                days_covered INTEGER NOT NULL,
                days_expected INTEGER NOT NULL,
                comparable INTEGER NOT NULL,
                not_comparable_reason TEXT,
                builder_version INTEGER NOT NULL,
                computed_at TEXT NOT NULL
            );
            """
        )
        for week_start, quals in ((TREND_W2, 40), (TREND_W1, 12)):
            conn.execute(
                """
                INSERT INTO ad_weekly_cohorts (
                    ad_id, week_start, adset_id, adset_name, ad_name, city,
                    spend_usd, impressions, fb_leads, amo_leads, quals,
                    days_covered, days_expected, comparable,
                    not_comparable_reason, builder_version, computed_at
                ) VALUES ('ad-x', ?, 'adset-X', 'CityA | PRODA', 'ad', 'CityA',
                          1000.0, 10000, 90, 100, ?, 7, 7, 1, NULL, 1,
                          '2026-07-31T09:00:00+00:00')
                """,
                (week_start.isoformat(), quals),
            )
        conn.commit()
    finally:
        conn.close()

    with patch("services.creative_intelligence.DB_PATH", db_path):
        verdicts = sr_module.build_trend_shadow(as_of=TREND_AS_OF)
        text = sr_module.format_trend_shadow(verdicts)

    assert verdicts, "старая схема должна читаться, а не отбрасываться"
    assert verdicts[0].romi_recent is None
    assert "ROMI: нет данных" in text


def test_trend_shadow_is_read_only(cohorts_db):
    """Тень ничего не меняет в БД и не умеет отправлять — это волна 3."""
    import sqlite3

    import services.shadow_report as sr_module

    def snapshot() -> list[tuple]:
        conn = sqlite3.connect(cohorts_db)
        try:
            return conn.execute(
                "SELECT * FROM ad_weekly_cohorts ORDER BY week_start, ad_id"
            ).fetchall()
        finally:
            conn.close()

    before = snapshot()
    with patch("services.creative_intelligence.DB_PATH", cohorts_db):
        sr_module.format_trend_shadow(sr_module.build_trend_shadow(as_of=TREND_AS_OF))

    assert snapshot() == before
    for forbidden in ("send_trend_shadow", "apply_trend", "propose_trend_actions"):
        assert not hasattr(sr_module, forbidden), (
            f"{forbidden}: волна 2 — только тень, действий в ней нет"
        )


def test_trend_shadow_survives_db_without_migration(tmp_path):
    """Старая БД без таблицы когорт — пустой список, а не исключение."""
    import sqlite3

    import services.shadow_report as sr_module

    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE creative_kb (ad_id TEXT PRIMARY KEY)")
    conn.close()

    with patch("services.creative_intelligence.DB_PATH", db_path):
        verdicts = sr_module.build_trend_shadow(as_of=TREND_AS_OF)

    assert verdicts == []
    assert "Недельных когорт нет" in sr_module.format_trend_shadow(verdicts)


def test_trend_shadow_ad_level_marks_diagnostic_only(cohorts_db):
    """На уровне объявления секция честно говорит, что действий не даёт."""
    import services.shadow_report as sr_module
    from services.trend_policy import TrendLevel

    with patch("services.creative_intelligence.DB_PATH", cohorts_db):
        verdicts = sr_module.build_trend_shadow(
            as_of=TREND_AS_OF, level=TrendLevel.AD
        )
    text = sr_module.format_trend_shadow(verdicts, level=TrendLevel.AD)

    assert all(verdict.action_grade is False for verdict in verdicts)
    assert "Уровень объявления — диагностика, действий не даёт." in text
