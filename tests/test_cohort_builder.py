"""
Тесты сборщика недельных когорт (services/cohort_builder.py).

Проверяют дисциплину полноты, а не «числа сошлись»:
- полная закрытая неделя → comparable=1 и заполненные метрики;
- неделя с пропущенным днём FB → comparable=0, причина FB_DAYS_MISSING,
  расход NULL, а НЕ занижённая сумма по оставшимся дням;
- текущая (незакрытая) неделя → comparable=0 с причиной WEEK_NOT_CLOSED;
- лиды AMO без FB-разметки не попадают в счётчики объявления, но видны в сводке;
- объявление, сменившее адсет между неделями, даёт разные adset_id по строкам;
- повторный прогон идемпотентен: UPSERT перезаписывает, дублей нет;
- rate limit FB → CohortRateLimitError и НИ ОДНОЙ записанной строки.

Волна 4 (выручка и ROMI) добавила сюда:
- выручка приписывается неделе СОЗДАНИЯ ЛИДА, а не неделе платежа;
- недозревшая когорта → NULL и revenue_mature=0, никогда не ноль;
- зрелость считается от воскресенья недели, а не от понедельника;
- возврат уменьшает выручку, полностью возвращённая сделка не «оплата»;
- платёж по чужому лиду виден в счётчике, а не теряется молча;
- нет курса → ROMI NULL, а не по сегодняшнему курсу;
- отравленный кеш services/cdp_payments не используется вовсе;
- недоступная ERP не стирает уже известную выручку.

Работают на временной SQLite БД (init_kb в tmp_path), FB, AMO, ERP и источник
курса мокаются.
"""

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())

from agent.fb_common import FBApiError  # noqa: E402
from services import cdp_client  # noqa: E402
from services import cohort_builder as cb  # noqa: E402
from services import creative_intelligence as ci  # noqa: E402


_TZ_LOCAL = timezone(timedelta(hours=5))

# Опорные недели: обе давно закрыты по любому календарю.
WEEK_A = date(2026, 6, 1)   # понедельник, 01.06–07.06
WEEK_B = date(2026, 6, 8)   # понедельник, 08.06–14.06
# «Сейчас» для тестов — середина недели 29.06–05.07, поэтому WEEK_A и WEEK_B
# закрыты, а неделя 29.06 ещё нет.
NOW = datetime(2026, 7, 1, 18, 0, tzinfo=_TZ_LOCAL)
WEEK_CURRENT = date(2026, 6, 29)
# Неделя 15.06–21.06: закрыта, но по выручке ещё зреет. От понедельника
# горизонт (15.06 + 14 = 29.06) уже прошёл, от воскресенья (21.06 + 14 = 05.07)
# — нет. Ровно та развилка, на которой мерка «week_start + H» врёт.
WEEK_MATURING = date(2026, 6, 15)
# Неделя из ДРУГОГО куска выгрузки платежей: первый кусок окна, начатого от
# WEEK_A, кончается 30.06, поэтому 06.07 гарантированно попадает во второй.
# Нужна для проверки, что упавший кусок не гасит деньги чужих недель.
WEEK_LATE = date(2026, 7, 6)
# «Сейчас» для тестов с WEEK_LATE: её горизонт (12.07 + 14 = 26.07) закрыт.
NOW_LATE = datetime(2026, 8, 1, 18, 0, tzinfo=_TZ_LOCAL)
# Курс, по которому считается ROMI в тестах.
RATE = 100.0


@pytest.fixture(autouse=True)
def isolated_kb(tmp_path):
    """Изолированная временная KB на каждый тест — боевая БД не трогается."""
    ci.DB_PATH = None
    db_path = str(tmp_path / "cohorts.db")
    ci.init_kb(db_path)
    yield db_path
    ci.DB_PATH = None


@pytest.fixture(autouse=True)
def no_poll_sleep(monkeypatch):
    """Опрос статуса отчёта не должен реально спать в тестах."""
    monkeypatch.setattr(cb, "_FB_POLL_INTERVAL_SEC", 0.0)


@pytest.fixture(autouse=True)
def no_live_money(monkeypatch):
    """Ни один тест не ходит в ERP и во внешний источник курса.

    По умолчанию платежей нет — у дозревшей недели это ДОКАЗАННЫЙ ноль
    выручки, а не «нет данных». Тесты про деньги переопределяют фикстуру
    через install_payments/install_rate.
    """
    monkeypatch.setattr(
        cb,
        "get_payments_strict",
        lambda date_from, date_to, direction=None: strict_read([]),
    )
    monkeypatch.setattr(cb, "get_usd_to_lcy_on", lambda day: RATE)


# ---------------------------------------------------------------------------
# Хелперы: фейковый FB и фейковый AMO
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Минимальный дублёр requests.Response для Graph-ответов."""

    def __init__(self, payload: dict, status_code: int = 200):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def fb_row(
    ad_id: str,
    day: date,
    *,
    spend: float = 10.0,
    impressions: int = 1000,
    leads: int = 2,
    adset_id: str = "adset-1",
    adset_name: str = "CityA | PRODA",
    ad_name: str = "CityA | Консультация / Креатив",
) -> dict:
    """Строка отчёта FB в том виде, в каком её отдаёт Graph."""
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "adset_id": adset_id,
        "adset_name": adset_name,
        "date_start": day.isoformat(),
        "spend": str(spend),
        "impressions": str(impressions),
        "actions": [
            {"action_type": "onsite_conversion.lead_grouped", "value": str(leads)},
        ],
    }


def _requested_window(kwargs: dict) -> tuple[date, date]:
    """Достаёт time_range из тела POST — как его отправляет сборщик."""
    payload = kwargs.get("data") or {}
    time_range = json.loads(payload["time_range"])
    return (
        date.fromisoformat(time_range["since"]),
        date.fromisoformat(time_range["until"]),
    )


def install_fake_fb(monkeypatch, rows: list[dict]) -> dict:
    """Полный путь асинхронного отчёта: POST → опрос статуса → выдача страниц.

    Отдаёт только строки запрошенного окна: настоящий Graph за пределы
    time_range не выходит, и сборщик на этом настаивает.
    """
    calls = {"post": 0, "status": 0, "pages": 0, "windows": []}

    def fake_post(url, **kwargs):
        calls["post"] += 1
        calls["windows"].append(_requested_window(kwargs))
        return _FakeResponse({"report_run_id": f"report-{calls['post']}"})

    def fake_get(url, **kwargs):
        if url.endswith("/insights"):
            calls["pages"] += 1
            since, until = calls["windows"][-1]
            window_rows = [
                row for row in rows
                if since <= date.fromisoformat(row["date_start"]) <= until
            ]
            return _FakeResponse({"data": window_rows, "paging": {}})
        calls["status"] += 1
        return _FakeResponse({"async_status": "Job Completed"})

    monkeypatch.setattr(cb, "_throttled_post", fake_post)
    monkeypatch.setattr(cb, "_throttled_get", fake_get)
    return calls


def amo_lead(
    lead_id: int,
    day: date,
    *,
    ad_id: str | None = "ad-1",
    qualified: bool = False,
    ad_name: str = "CityA | Консультация / Креатив",
) -> dict:
    """Лид AMO в формате get_leads_window (ad_id=None → лид без разметки)."""
    custom_fields = []
    if ad_id is not None:
        custom_fields.append(
            {"field_id": 902422, "field_name": "fb_ad_id",
             "values": [{"value": ad_id}]}
        )
        custom_fields.append(
            {"field_id": 930141, "field_name": "fb_ad_name",
             "values": [{"value": ad_name}]}
        )
    custom_fields.append(
        {"field_id": 804012, "field_name": "Квалификация пройдена",
         "values": [{"value": "ДА" if qualified else "НЕТ"}]}
    )
    created_at = int(
        datetime.combine(day, datetime.min.time(), _TZ_LOCAL).timestamp()
    ) + 12 * 3600
    return {
        "id": lead_id,
        "name": f"lead-{lead_id}",
        "created_at": created_at,
        "custom_fields": custom_fields,
    }


def install_fake_amo(monkeypatch, leads: list[dict]) -> None:
    """get_leads_window отдаёт только лиды, попавшие в запрошенное окно."""

    def fake_window(from_ts: int, to_ts: int) -> list[dict]:
        return [
            lead for lead in leads
            if from_ts <= int(lead["created_at"]) <= to_ts
        ]

    monkeypatch.setattr(cb, "get_leads_window", fake_window)


def payment(
    payment_id: int,
    lead_id: int,
    day: date,
    *,
    amount: float = 100_000.0,
    direction: str = "income",
) -> dict:
    """Платёж ERP в формате cdp_client.get_payments (contract_number = lead_id)."""
    return {
        "id": payment_id,
        "deal_id": 900_000 + payment_id,  # мусорное поле CDP, не используется
        "contract_number": lead_id,
        "amount": amount,
        "direction": direction,
        "doc_date": day.isoformat(),
    }


def strict_read(items: list[dict], *, complete: bool = True):
    """Мок ответа get_payments_strict: важны items и признак полноты."""
    return cdp_client.StrictPaymentsRead(
        items=tuple(items),
        declared_total=len(items),
        collected_total=len(items),
        page_item_counts=(len(items),),
        fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        data_as_of=None,
        from_cache=False,
        complete=complete,
    )


def install_payments(
    monkeypatch,
    payments: list[dict],
    *,
    calls: list | None = None,
) -> None:
    """get_payments_strict отдаёт платежи запрошенного окна по doc_date."""

    def fake_get_payments_strict(date_from, date_to, direction=None):
        # Контракт CDP: параметры именно date, а не строки.
        assert isinstance(date_from, date) and not isinstance(date_from, datetime)
        assert isinstance(date_to, date) and not isinstance(date_to, datetime)
        if calls is not None:
            calls.append((date_from, date_to))
        return strict_read([
            item for item in payments
            if date_from <= date.fromisoformat(item["doc_date"]) <= date_to
            and (direction is None or item.get("direction") == direction)
        ])

    monkeypatch.setattr(cb, "get_payments_strict", fake_get_payments_strict)


def install_rate(monkeypatch, rate: float | None) -> None:
    """Курс на дату недели; None — курса нет."""
    monkeypatch.setattr(cb, "get_usd_to_lcy_on", lambda day: rate)


def fetch_rows() -> list[sqlite3.Row]:
    conn = ci._get_connection()
    try:
        return conn.execute(
            "SELECT * FROM ad_weekly_cohorts ORDER BY week_start, ad_id"
        ).fetchall()
    finally:
        conn.close()


def full_week_rows(ad_id: str, week_start: date, **kwargs) -> list[dict]:
    """Семь дней FB-данных на неделю."""
    return [
        fb_row(ad_id, week_start + timedelta(days=offset), **kwargs)
        for offset in range(7)
    ]


# ---------------------------------------------------------------------------
# Календарь
# ---------------------------------------------------------------------------

def test_week_start_of_monday_is_itself():
    """Понедельник — начало своей же недели."""
    assert cb.week_start_of(date(2026, 6, 1)) == date(2026, 6, 1)


def test_week_start_of_sunday_points_back_to_monday():
    """Воскресенье относится к неделе предыдущего понедельника."""
    assert cb.week_start_of(date(2026, 6, 7)) == date(2026, 6, 1)


def test_week_not_closed_until_account_day_passes_sunday():
    """Закрытость недели считается по календарю кабинета, а не CityA."""
    assert cb._is_week_closed(WEEK_A, NOW) is True
    assert cb._is_week_closed(WEEK_CURRENT, NOW) is False


# ---------------------------------------------------------------------------
# Полная закрытая неделя
# ---------------------------------------------------------------------------

def test_full_closed_week_is_comparable(monkeypatch):
    """7 из 7 дней + закрытая неделя + оба источника → comparable=1."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A, spend=10.0, leads=2))
    install_fake_amo(monkeypatch, [
        amo_lead(1, WEEK_A + timedelta(days=1), qualified=True),
        amo_lead(2, WEEK_A + timedelta(days=3)),
    ])

    summary = cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert summary["rows"] == 1
    assert summary["comparable"] == 1
    rows = fetch_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["comparable"] == 1
    assert row["not_comparable_reason"] is None
    assert row["days_covered"] == 7
    assert row["days_expected"] == 7
    assert row["spend_usd"] == pytest.approx(70.0)
    assert row["impressions"] == 7000
    assert row["fb_leads"] == 14
    assert row["amo_leads"] == 2
    assert row["quals"] == 1
    assert row["builder_version"] == cb.BUILDER_VERSION
    assert row["city"] == "CityA"


# ---------------------------------------------------------------------------
# Пропущенный день FB
# ---------------------------------------------------------------------------

def test_missing_fb_day_blocks_comparable_and_nulls_spend(monkeypatch):
    """День недели не пришёл из FB → comparable=0, расход NULL, а не занижен."""
    rows = full_week_rows("ad-1", WEEK_A, spend=10.0)
    missing_day = WEEK_A + timedelta(days=3)
    rows = [row for row in rows if row["date_start"] != missing_day.isoformat()]
    install_fake_fb(monkeypatch, rows)
    install_fake_amo(monkeypatch, [amo_lead(1, WEEK_A + timedelta(days=1))])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    row = fetch_rows()[0]
    assert row["comparable"] == 0
    assert cb.REASON_FB_DAYS_MISSING in row["not_comparable_reason"]
    assert missing_day.isoformat() in row["not_comparable_reason"]
    assert row["days_covered"] == 6
    assert row["days_expected"] == 7
    # Ключевое: занижённая сумма по 6 дням не записана вовсе.
    assert row["spend_usd"] is None
    assert row["impressions"] is None
    assert row["fb_leads"] is None


def test_missing_fb_day_does_not_write_partial_sum_as_full(monkeypatch):
    """Расход шести дней (60) не должен всплыть ни в одной колонке недели."""
    rows = [
        row for row in full_week_rows("ad-1", WEEK_A, spend=10.0)
        if row["date_start"] != (WEEK_A + timedelta(days=6)).isoformat()
    ]
    install_fake_fb(monkeypatch, rows)
    install_fake_amo(monkeypatch, [])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    row = fetch_rows()[0]
    assert row["spend_usd"] is None
    assert row["days_covered"] < row["days_expected"]


# ---------------------------------------------------------------------------
# Незакрытая неделя
# ---------------------------------------------------------------------------

def test_current_week_is_not_comparable(monkeypatch):
    """Текущая неделя ещё идёт → comparable=0 с причиной WEEK_NOT_CLOSED."""
    days = [WEEK_CURRENT + timedelta(days=offset) for offset in range(3)]
    install_fake_fb(monkeypatch, [fb_row("ad-1", day) for day in days])
    install_fake_amo(monkeypatch, [amo_lead(1, days[0])])

    summary = cb.build_cohorts(WEEK_CURRENT, days[-1], now=NOW)

    assert summary["comparable"] == 0
    row = fetch_rows()[0]
    assert row["comparable"] == 0
    assert cb.REASON_WEEK_NOT_CLOSED in row["not_comparable_reason"]
    assert cb.REASON_WEEK_PARTIAL_RANGE in row["not_comparable_reason"]
    # Все запрошенные дни получены, поэтому суммы по ним честные и записаны.
    assert row["days_covered"] == row["days_expected"] == 3
    assert row["spend_usd"] == pytest.approx(30.0)


def test_closed_week_with_full_coverage_beats_current_week(monkeypatch):
    """В одном прогоне закрытая неделя сравнима, текущая — нет."""
    rows = full_week_rows("ad-1", WEEK_B)
    rows += [
        fb_row("ad-1", WEEK_CURRENT + timedelta(days=offset))
        for offset in range(3)
    ]
    install_fake_fb(monkeypatch, rows)
    install_fake_amo(monkeypatch, [])

    cb.build_cohorts(WEEK_B, WEEK_CURRENT + timedelta(days=2), now=NOW)

    by_week = {row["week_start"]: row for row in fetch_rows()}
    assert by_week[WEEK_B.isoformat()]["comparable"] == 1
    assert by_week[WEEK_CURRENT.isoformat()]["comparable"] == 0


# ---------------------------------------------------------------------------
# Лиды без разметки
# ---------------------------------------------------------------------------

def test_unmarked_leads_are_counted_separately_not_attributed(monkeypatch):
    """Лид без fb_ad_id не попадает в счётчики объявления, но виден в сводке."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [
        amo_lead(1, WEEK_A + timedelta(days=1), ad_id="ad-1", qualified=True),
        amo_lead(2, WEEK_A + timedelta(days=2), ad_id=None),
        amo_lead(3, WEEK_A + timedelta(days=3), ad_id=None, qualified=True),
    ])

    summary = cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert summary["amo_leads_total"] == 3
    assert summary["amo_leads_marked"] == 1
    assert summary["amo_leads_unmarked"] == 2
    row = fetch_rows()[0]
    assert row["amo_leads"] == 1
    assert row["quals"] == 1


def test_lead_without_created_at_is_counted_not_guessed(monkeypatch):
    """Лид без даты создания не приписывается неделе наугад — он в счётчике."""
    broken = amo_lead(9, WEEK_A + timedelta(days=1), ad_id="ad-1")
    broken["created_at"] = None
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))

    def fake_window(from_ts, to_ts):
        return [amo_lead(1, WEEK_A + timedelta(days=1), ad_id="ad-1"), broken]

    monkeypatch.setattr(cb, "get_leads_window", fake_window)

    summary = cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert summary["amo_leads_bad_created_at"] == 1
    assert fetch_rows()[0]["amo_leads"] == 1


def test_unmarked_share_is_logged(monkeypatch, caplog):
    """Доля размеченных лидов уходит в лог — дыру атрибуции видно в проде."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [
        amo_lead(1, WEEK_A + timedelta(days=1), ad_id="ad-1"),
        amo_lead(2, WEEK_A + timedelta(days=2), ad_id=None),
    ])

    with caplog.at_level("INFO", logger="services.cohort_builder"):
        cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert any("без разметки" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# Переезд объявления между адсетами
# ---------------------------------------------------------------------------

def test_adset_binding_is_frozen_per_week(monkeypatch):
    """Объявление сменило адсет → в строках недель разные adset_id."""
    rows = full_week_rows(
        "ad-1", WEEK_A, adset_id="adset-old", adset_name="CityA | Старый",
    )
    rows += full_week_rows(
        "ad-1", WEEK_B, adset_id="adset-new", adset_name="CityA | Новый",
    )
    install_fake_fb(monkeypatch, rows)
    install_fake_amo(monkeypatch, [])

    cb.build_cohorts(WEEK_A, WEEK_B + timedelta(days=6), now=NOW)

    by_week = {row["week_start"]: row for row in fetch_rows()}
    assert by_week[WEEK_A.isoformat()]["adset_id"] == "adset-old"
    assert by_week[WEEK_A.isoformat()]["adset_name"] == "CityA | Старый"
    assert by_week[WEEK_B.isoformat()]["adset_id"] == "adset-new"
    assert by_week[WEEK_B.isoformat()]["adset_name"] == "CityA | Новый"


def test_adset_binding_takes_last_day_of_week(monkeypatch):
    """Переезд в середине недели: неделя держит привязку последнего дня."""
    rows = [
        fb_row("ad-1", WEEK_A + timedelta(days=offset), adset_id="adset-old")
        for offset in range(4)
    ]
    rows += [
        fb_row("ad-1", WEEK_A + timedelta(days=offset), adset_id="adset-new")
        for offset in range(4, 7)
    ]
    # Порядок страниц FB не гарантирован — перемешиваем намеренно.
    rows = rows[::-1]
    install_fake_fb(monkeypatch, rows)
    install_fake_amo(monkeypatch, [])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert fetch_rows()[0]["adset_id"] == "adset-new"


# ---------------------------------------------------------------------------
# Идемпотентность
# ---------------------------------------------------------------------------

def test_rerun_is_idempotent_no_duplicates(monkeypatch):
    """Повторный прогон того же диапазона перезаписывает строку, не дублирует."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(1, WEEK_A + timedelta(days=1))])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)
    first = fetch_rows()
    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)
    second = fetch_rows()

    assert len(first) == 1
    assert len(second) == 1
    assert second[0]["spend_usd"] == first[0]["spend_usd"]


def test_rerun_picks_up_matured_quals(monkeypatch):
    """Квалы дозрели — пересчёт свежей недели обязан их подхватить."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(1, WEEK_A + timedelta(days=1))])
    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)
    assert fetch_rows()[0]["quals"] == 0

    install_fake_amo(monkeypatch, [
        amo_lead(1, WEEK_A + timedelta(days=1), qualified=True),
    ])
    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    rows = fetch_rows()
    assert len(rows) == 1
    assert rows[0]["quals"] == 1


def test_incomplete_rerun_does_not_overwrite_comparable_row(monkeypatch):
    """Узкий пересчёт с дырой не обесценивает уже собранную полную неделю."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A, spend=10.0))
    install_fake_amo(monkeypatch, [])
    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)
    assert fetch_rows()[0]["comparable"] == 1

    broken = [
        row for row in full_week_rows("ad-1", WEEK_A, spend=10.0)
        if row["date_start"] != (WEEK_A + timedelta(days=2)).isoformat()
    ]
    install_fake_fb(monkeypatch, broken)
    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    row = fetch_rows()[0]
    assert row["comparable"] == 1
    assert row["spend_usd"] == pytest.approx(70.0)


# ---------------------------------------------------------------------------
# Rate limit и неполные ответы
# ---------------------------------------------------------------------------

def test_rate_limit_raises_and_writes_nothing(monkeypatch):
    """Rate limit FB → честная ошибка и НИ ОДНОЙ записанной строки."""
    def angry_post(url, **kwargs):
        raise FBApiError("Facebook временно ограничил запросы", 429)

    monkeypatch.setattr(cb, "_throttled_post", angry_post)
    install_fake_amo(monkeypatch, [amo_lead(1, WEEK_A + timedelta(days=1))])

    with pytest.raises(cb.CohortRateLimitError):
        cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert fetch_rows() == []


def test_rate_limit_does_not_downgrade_existing_rows(monkeypatch):
    """Уже собранная неделя переживает rate limit следующего прогона."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [])
    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)
    before = fetch_rows()[0]["spend_usd"]

    def angry_get(url, **kwargs):
        raise FBApiError("Facebook временно ограничил запросы", 17)

    monkeypatch.setattr(cb, "_throttled_get", angry_get)
    with pytest.raises(cb.CohortRateLimitError):
        cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert fetch_rows()[0]["spend_usd"] == before


def test_failed_report_is_error_not_empty_week(monkeypatch):
    """async_status='Job Failed' — ошибка, а не отчёт из нуля строк."""
    def fake_post(url, **kwargs):
        return _FakeResponse({"report_run_id": "report-1"})

    def fake_get(url, **kwargs):
        return _FakeResponse({"async_status": "Job Failed"})

    monkeypatch.setattr(cb, "_throttled_post", fake_post)
    monkeypatch.setattr(cb, "_throttled_get", fake_get)
    install_fake_amo(monkeypatch, [])

    with pytest.raises(cb.CohortBuildError):
        cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert fetch_rows() == []


def test_reduce_data_splits_window_instead_of_failing(monkeypatch):
    """Reduce-data (код 1) — не лимит: окно дробится, данные всё равно собраны."""
    rows = full_week_rows("ad-1", WEEK_A)
    state = {"rejected_wide": False, "window": None}

    def fake_post(url, **kwargs):
        window = _requested_window(kwargs)
        # Первый широкий запрос отбиваем reduce-data, дальше отдаём отчёт.
        if not state["rejected_wide"]:
            state["rejected_wide"] = True
            return _FakeResponse(
                {"error": {"code": 1, "message": "Please reduce the amount of data"}},
                status_code=400,
            )
        state["window"] = window
        return _FakeResponse({"report_run_id": "report-1"})

    def fake_get(url, **kwargs):
        if url.endswith("/insights"):
            since, until = state["window"]
            return _FakeResponse({
                "data": [
                    row for row in rows
                    if since <= date.fromisoformat(row["date_start"]) <= until
                ],
                "paging": {},
            })
        return _FakeResponse({"async_status": "Job Completed"})

    monkeypatch.setattr(cb, "_throttled_post", fake_post)
    monkeypatch.setattr(cb, "_throttled_get", fake_get)
    install_fake_amo(monkeypatch, [])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert state["rejected_wide"] is True
    assert len(fetch_rows()) == 1


def test_broken_lead_semantics_is_error(monkeypatch):
    """Битые lead-actions — ошибка сборки, а не тихий ноль лидов."""
    row = fb_row("ad-1", WEEK_A)
    row["actions"] = [
        {"action_type": "onsite_conversion.lead_grouped", "value": "3"},
        {"action_type": "onsite_conversion.lead_grouped", "value": "5"},
    ]
    install_fake_fb(monkeypatch, [row])
    install_fake_amo(monkeypatch, [])

    with pytest.raises(cb.CohortBuildError):
        cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert fetch_rows() == []


# ---------------------------------------------------------------------------
# Строки только из AMO и служебное
# ---------------------------------------------------------------------------

def test_amo_only_ad_gets_row_with_verified_zero_spend(monkeypatch):
    """Лид пришёл позже открутки: неделя выгружена целиком → расход 0, не NULL."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [
        amo_lead(1, WEEK_A + timedelta(days=2), ad_id="ad-2"),
    ])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    by_ad = {row["ad_id"]: row for row in fetch_rows()}
    assert by_ad["ad-2"]["spend_usd"] == pytest.approx(0.0)
    assert by_ad["ad-2"]["impressions"] == 0
    assert by_ad["ad-2"]["amo_leads"] == 1
    assert by_ad["ad-2"]["adset_id"] is None


def test_empty_range_is_rejected():
    """since > until — ошибка вызова, а не пустая тихая сборка."""
    with pytest.raises(ValueError):
        cb.build_cohorts(WEEK_B, WEEK_A)


def test_refresh_recent_weeks_aligns_to_monday(monkeypatch):
    """Крон-вход выравнивает начало диапазона на понедельник."""
    captured = {}

    def fake_build(since, until, *, now=None):
        captured["since"] = since
        captured["until"] = until
        return {"rows": 0}

    monkeypatch.setattr(cb, "build_cohorts", fake_build)
    cb.refresh_recent_weeks(3, now=NOW)

    assert captured["since"] == WEEK_CURRENT - timedelta(days=14)
    assert captured["since"].weekday() == 0
    assert captured["until"] == NOW.date()


def test_refresh_recent_weeks_rejects_zero():
    """weeks=0 — ошибка: пустой пересчёт молча ничего не обновил бы."""
    with pytest.raises(ValueError):
        cb.refresh_recent_weeks(0, now=NOW)


# ---------------------------------------------------------------------------
# Схема
# ---------------------------------------------------------------------------

def test_schema_rejects_comparable_row_without_metrics():
    """CHECK не даёт объявить строку сравнимой без чисел."""
    conn = ci._get_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO ad_weekly_cohorts (
                    ad_id, week_start, days_covered, days_expected,
                    comparable, builder_version, computed_at
                ) VALUES ('ad-x', '2026-06-01', 7, 7, 1, 1, '2026-06-09')
                """
            )
    finally:
        conn.close()


def test_schema_requires_reason_for_not_comparable():
    """CHECK не даёт объявить строку несравнимой без причины."""
    conn = ci._get_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO ad_weekly_cohorts (
                    ad_id, week_start, days_covered, days_expected,
                    comparable, builder_version, computed_at
                ) VALUES ('ad-x', '2026-06-01', 3, 7, 0, 1, '2026-06-09')
                """
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Выручка когорты и ROMI (волна 4)
# ---------------------------------------------------------------------------

def test_revenue_belongs_to_lead_week_not_payment_week(monkeypatch):
    """Деньги ложатся на неделю СОЗДАНИЯ ЛИДА, а не на неделю платежа.

    Главное требование волны: get_payments фильтрует по doc_date, и наивная
    раскладка «касса недели W → расход недели W» вернула бы усреднение.
    """
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A)
                    + full_week_rows("ad-1", WEEK_B))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    # Лид создан 02.06 (неделя A), оплата пришла 10.06 — это уже неделя B.
    install_payments(monkeypatch, [payment(1, 101, WEEK_B + timedelta(days=2))])

    cb.build_cohorts(WEEK_A, WEEK_B + timedelta(days=6), now=NOW)

    by_week = {row["week_start"]: row for row in fetch_rows()}
    assert by_week[WEEK_A.isoformat()]["revenue_lcy"] == pytest.approx(100_000.0)
    assert by_week[WEEK_A.isoformat()]["payments"] == 1
    # Неделя платежа денег не получает — иначе это касса, а не когорта.
    assert by_week[WEEK_B.isoformat()]["revenue_lcy"] == pytest.approx(0.0)
    assert by_week[WEEK_B.isoformat()]["payments"] == 0


def test_revenue_query_window_reaches_beyond_range_for_horizon(monkeypatch):
    """Окно запроса платежей шире диапазона когорт на горизонт.

    Иначе хвост оплат последней недели остался бы за краем запроса и выручка
    последней когорты была бы занижена — молча.
    """
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    calls: list = []
    install_payments(monkeypatch, [], calls=calls)

    until = WEEK_A + timedelta(days=6)
    cb.build_cohorts(WEEK_A, until, now=NOW)

    assert calls, "get_payments_strict не вызывался"
    query_to = max(day_to for _, day_to in calls)
    assert query_to >= until + timedelta(days=cb.REVENUE_HORIZON_DAYS)


def test_immature_cohort_has_null_revenue_not_zero(monkeypatch):
    """Недозревшая когорта → выручка NULL и revenue_mature=0, никогда не 0."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_MATURING))
    install_fake_amo(
        monkeypatch, [amo_lead(101, WEEK_MATURING + timedelta(days=1))]
    )
    install_payments(
        monkeypatch, [payment(1, 101, WEEK_MATURING + timedelta(days=2))]
    )

    cb.build_cohorts(
        WEEK_MATURING, WEEK_MATURING + timedelta(days=6), now=NOW
    )

    row = fetch_rows()[0]
    assert row["revenue_mature"] == 0
    assert row["revenue_lcy"] is None
    assert row["payments"] is None
    assert row["romi_pct"] is None
    assert row["revenue_horizon_days"] is None


def test_revenue_maturity_counts_from_sunday_not_monday(monkeypatch):
    """Зрелость меряется от воскресенья недели: у последних лидов свой горизонт."""
    # 15.06 + 14 = 29.06 — по понедельнику неделя «дозрела» бы уже 29.06.
    assert WEEK_MATURING + timedelta(days=cb.REVENUE_HORIZON_DAYS) <= NOW.date()
    assert cb._is_revenue_mature(WEEK_MATURING, cb.REVENUE_HORIZON_DAYS, NOW) is False
    # Ровно на 05.07 (воскресенье 21.06 + 14) неделя становится зрелой.
    matured_at = datetime(2026, 7, 5, 9, 0, tzinfo=_TZ_LOCAL)
    assert cb._is_revenue_mature(
        WEEK_MATURING, cb.REVENUE_HORIZON_DAYS, matured_at
    ) is True


def test_mature_week_without_payments_writes_proven_zero(monkeypatch):
    """Дозревшая неделя без оплат — доказанный ноль, а не «нет данных»."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A, spend=10.0))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    install_payments(monkeypatch, [])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    row = fetch_rows()[0]
    assert row["revenue_mature"] == 1
    assert row["revenue_lcy"] == pytest.approx(0.0)
    assert row["payments"] == 0
    # Расход был, денег нет → ROMI ровно 0%, и это честное число.
    assert row["romi_pct"] == pytest.approx(0.0)


def test_romi_uses_week_rate_and_project_formula(monkeypatch):
    """ROMI = выручка / (расход × курс) × 100 — конвенция проекта."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A, spend=10.0))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    install_payments(monkeypatch, [
        payment(1, 101, WEEK_A + timedelta(days=2), amount=40_000.0),
    ])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    row = fetch_rows()[0]
    assert row["spend_usd"] == pytest.approx(70.0)
    assert row["usd_lcy_rate"] == pytest.approx(RATE)
    assert row["revenue_horizon_days"] == cb.REVENUE_HORIZON_DAYS
    assert row["romi_pct"] == pytest.approx(
        round(40_000.0 / (70.0 * RATE) * 100, 1)
    )


def test_refund_reduces_cohort_revenue(monkeypatch):
    """Возврат вычитается из выручки когорты, а не игнорируется."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [
        amo_lead(101, WEEK_A + timedelta(days=1)),
        amo_lead(102, WEEK_A + timedelta(days=1)),
    ])
    install_payments(monkeypatch, [
        payment(1, 101, WEEK_A + timedelta(days=2), amount=100_000.0),
        payment(2, 101, WEEK_A + timedelta(days=3), amount=30_000.0,
                direction="refund"),
        # Сделка 102 возвращена целиком — оплатой она не считается.
        payment(3, 102, WEEK_A + timedelta(days=2), amount=50_000.0),
        payment(4, 102, WEEK_A + timedelta(days=4), amount=50_000.0,
                direction="refund"),
    ])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    row = fetch_rows()[0]
    assert row["revenue_lcy"] == pytest.approx(70_000.0)
    assert row["payments"] == 1


def test_refund_can_push_cohort_revenue_below_zero(monkeypatch):
    """Возврат больше прихода уводит выручку в минус, а не обнуляет её."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    install_payments(monkeypatch, [
        payment(1, 101, WEEK_A + timedelta(days=2), amount=40_000.0),
        payment(2, 101, WEEK_A + timedelta(days=3), amount=60_000.0,
                direction="refund"),
    ])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    row = fetch_rows()[0]
    assert row["revenue_lcy"] == pytest.approx(-20_000.0)
    assert row["payments"] == 0
    assert row["romi_pct"] < 0


def test_installment_documents_count_as_one_payment(monkeypatch):
    """Рассрочка банка: N документов одной сделки — одна оплата, не N."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    install_payments(monkeypatch, [
        payment(1, 101, WEEK_A + timedelta(days=2), amount=30_000.0),
        payment(2, 101, WEEK_A + timedelta(days=5), amount=30_000.0),
        payment(3, 101, WEEK_A + timedelta(days=9), amount=30_000.0),
    ])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    row = fetch_rows()[0]
    assert row["payments"] == 1
    assert row["revenue_lcy"] == pytest.approx(90_000.0)


def test_duplicate_payment_document_counted_once(monkeypatch):
    """Один payment_id — одна сумма, сколько бы раз CDP его ни вернул."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    duplicate = payment(1, 101, WEEK_A + timedelta(days=2), amount=100_000.0)
    install_payments(monkeypatch, [duplicate, dict(duplicate)])

    summary = cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert summary["payments_duplicate"] == 1
    assert fetch_rows()[0]["revenue_lcy"] == pytest.approx(100_000.0)


def test_payment_for_foreign_lead_is_counted_not_silently_dropped(monkeypatch):
    """Платёж по лиду не из выгрузки виден в сводке, а не пропадает молча."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    install_payments(monkeypatch, [
        payment(1, 999_999, WEEK_A + timedelta(days=2), amount=500_000.0),
    ])

    summary = cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert summary["payments_unmatched_lead"] == 1
    assert summary["payments_matched"] == 0
    row = fetch_rows()[0]
    assert row["revenue_lcy"] == pytest.approx(0.0)


def test_payment_outside_horizon_is_counted_not_attributed(monkeypatch):
    """Оплата позже горизонта в когорту не попадает, но в счётчике видна."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    created = WEEK_A + timedelta(days=1)
    install_fake_amo(monkeypatch, [amo_lead(101, created)])
    late = created + timedelta(days=cb.REVENUE_HORIZON_DAYS + 3)
    install_payments(monkeypatch, [payment(1, 101, late, amount=300_000.0)])

    summary = cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert summary["payments_outside_horizon"] == 1
    assert fetch_rows()[0]["revenue_lcy"] == pytest.approx(0.0)


def test_missing_rate_leaves_romi_null_not_today_rate(monkeypatch):
    """Курс недоступен → ROMI NULL. По сегодняшнему курсу не считаем."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    install_payments(monkeypatch, [
        payment(1, 101, WEEK_A + timedelta(days=2), amount=200_000.0),
    ])
    install_rate(monkeypatch, None)

    summary = cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    row = fetch_rows()[0]
    assert row["revenue_lcy"] == pytest.approx(200_000.0)
    assert row["usd_lcy_rate"] is None
    assert row["romi_pct"] is None
    assert summary["revenue_rate_missing"] == 1


def test_rate_is_requested_once_per_week(monkeypatch):
    """Курс тянется один раз на неделю, а не на каждое объявление."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A)
                    + full_week_rows("ad-2", WEEK_A))
    install_fake_amo(monkeypatch, [])
    install_payments(monkeypatch, [])
    asked: list = []

    def counting_rate(day):
        asked.append(day)
        return RATE

    monkeypatch.setattr(cb, "get_usd_to_lcy_on", counting_rate)

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert asked == [WEEK_A]


def test_cdp_unavailable_leaves_revenue_null_and_keeps_quals(monkeypatch):
    """ERP лежит → выручка NULL (не ноль), но квалы недели всё равно записаны."""
    from services.cdp_client import CdpError

    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [
        amo_lead(101, WEEK_A + timedelta(days=1), qualified=True),
    ])

    def angry_cdp(date_from, date_to, direction=None):
        raise CdpError("CDP недоступен")

    monkeypatch.setattr(cb, "get_payments_strict", angry_cdp)

    summary = cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert summary["revenue_source_ok"] is False
    row = fetch_rows()[0]
    assert row["quals"] == 1
    assert row["comparable"] == 1
    assert row["revenue_lcy"] is None
    assert row["payments"] is None
    assert row["romi_pct"] is None
    # Зрелость — свойство календаря: горизонт закрыт, а денег мы не знаем.
    assert row["revenue_mature"] == 1


def test_cdp_outage_does_not_erase_known_revenue(monkeypatch):
    """Разовая недоступность ERP не стирает уже посчитанную выручку недели."""
    from services.cdp_client import CdpError

    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    install_payments(monkeypatch, [
        payment(1, 101, WEEK_A + timedelta(days=2), amount=200_000.0),
    ])
    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    def angry_cdp(date_from, date_to, direction=None):
        raise CdpError("CDP недоступен")

    monkeypatch.setattr(cb, "get_payments_strict", angry_cdp)
    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    row = fetch_rows()[0]
    assert row["revenue_lcy"] == pytest.approx(200_000.0)
    assert row["romi_pct"] is not None


def test_revenue_rerun_is_idempotent(monkeypatch):
    """Повторный прогон не удваивает деньги и не плодит строк."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    install_payments(monkeypatch, [
        payment(1, 101, WEEK_A + timedelta(days=2), amount=120_000.0),
    ])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)
    first = fetch_rows()
    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)
    second = fetch_rows()

    assert len(first) == len(second) == 1
    assert second[0]["revenue_lcy"] == pytest.approx(120_000.0)
    assert second[0]["payments"] == 1
    assert second[0]["romi_pct"] == pytest.approx(first[0]["romi_pct"])


def test_poisoned_cdp_payments_lead_cache_is_not_used(monkeypatch):
    """Карта лид→объявление строится из своей выгрузки, а не из кеша ERP.

    data/cdp_payments_lead_cache.json отравлен (94% записей «не найдено», и
    код их не перепроверяет), поэтому _build_deal_to_ad_map здесь запрещён.
    """
    from services import cdp_payments

    called: list = []

    def forbidden(*args, **kwargs):
        called.append(args)
        return {}

    monkeypatch.setattr(cdp_payments, "_build_deal_to_ad_map", forbidden)
    monkeypatch.setattr(cdp_payments, "_load_lead_cache", forbidden)

    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    install_payments(monkeypatch, [
        payment(1, 101, WEEK_A + timedelta(days=2), amount=100_000.0),
    ])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert called == []
    assert fetch_rows()[0]["revenue_lcy"] == pytest.approx(100_000.0)


def test_revenue_follows_lead_to_its_own_ad(monkeypatch):
    """Деньги идут тому объявлению, чей лид оплатил, а не соседнему."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A)
                    + full_week_rows("ad-2", WEEK_A))
    install_fake_amo(monkeypatch, [
        amo_lead(101, WEEK_A + timedelta(days=1), ad_id="ad-1"),
        amo_lead(202, WEEK_A + timedelta(days=2), ad_id="ad-2"),
    ])
    install_payments(monkeypatch, [
        payment(1, 202, WEEK_A + timedelta(days=3), amount=250_000.0),
    ])

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    by_ad = {row["ad_id"]: row for row in fetch_rows()}
    assert by_ad["ad-1"]["revenue_lcy"] == pytest.approx(0.0)
    assert by_ad["ad-2"]["revenue_lcy"] == pytest.approx(250_000.0)


def test_schema_rejects_romi_on_immature_cohort():
    """CHECK не даёт записать ROMI недозревшей когорте — на уровне схемы."""
    conn = ci._get_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO ad_weekly_cohorts (
                    ad_id, week_start, days_covered, days_expected, comparable,
                    not_comparable_reason, builder_version, computed_at,
                    revenue_mature, romi_pct
                ) VALUES ('ad-x', '2026-06-01', 7, 7, 0, 'WEEK_NOT_CLOSED',
                          1, '2026-06-09', 0, 250.0)
                """
            )
    finally:
        conn.close()


def test_refresh_window_covers_revenue_maturity():
    """Окно крона шире зрелости выручки — иначе деньги не проставятся никогда."""
    # Неделя дозревает через 6 + горизонт дней после своего понедельника.
    maturity_days = 6 + cb.REVENUE_HORIZON_DAYS
    default_weeks = 5
    # Худший случай — понедельник: окно начинается ровно 7×(weeks−1) дней назад.
    assert 7 * (default_weeks - 1) >= maturity_days


# ---------------------------------------------------------------------------
# Курс на дату недели (services/exchange_rate.get_usd_to_lcy_on)
# ---------------------------------------------------------------------------

def test_dated_rate_returns_none_without_fallback(monkeypatch):
    """Источник курса не ответил → None. Ни константы, ни «последнего известного»."""
    from services import exchange_rate

    monkeypatch.setattr(exchange_rate, "_dated_rate_cache", {})
    monkeypatch.setattr(exchange_rate, "_rate_cache", {"2026-01-01": 95.0})
    monkeypatch.setattr(exchange_rate, "_fetch_usd_lcy", lambda date_str: None)

    assert exchange_rate.get_usd_to_lcy_on(WEEK_A) is None


def test_dated_rate_asks_source_for_that_date(monkeypatch):
    """Запрашивается именно дата недели, а не «сегодня»."""
    from services import exchange_rate

    asked: list = []
    monkeypatch.setattr(exchange_rate, "_dated_rate_cache", {})

    def fake_fetch(date_str):
        asked.append(date_str)
        return 105.0

    monkeypatch.setattr(exchange_rate, "_fetch_usd_lcy", fake_fetch)

    assert exchange_rate.get_usd_to_lcy_on(WEEK_A) == pytest.approx(105.0)
    # Второй вызов берётся из кеша: курс за день не меняется.
    assert exchange_rate.get_usd_to_lcy_on(WEEK_A) == pytest.approx(105.0)
    assert asked == [WEEK_A.isoformat()]


def test_dated_rate_does_not_pollute_today_cache(monkeypatch):
    """Датированный вход не подкладывает чужие даты оперативному курсу."""
    from services import exchange_rate

    monkeypatch.setattr(exchange_rate, "_dated_rate_cache", {})
    monkeypatch.setattr(exchange_rate, "_rate_cache", {})
    monkeypatch.setattr(exchange_rate, "_fetch_usd_lcy", lambda date_str: 105.0)

    exchange_rate.get_usd_to_lcy_on(WEEK_A)

    assert exchange_rate._rate_cache == {}


def test_dated_rate_rejects_datetime(monkeypatch):
    """datetime вместо date — ошибка: «дата недели» не имеет времени."""
    from services import exchange_rate

    with pytest.raises(TypeError):
        exchange_rate.get_usd_to_lcy_on(NOW)


def test_rate_source_fixed_mode_uses_config_without_network(monkeypatch):
    """Без EXCHANGE_RATE_URL курс — фиксированный из config, сеть не трогается."""
    from services import exchange_rate

    monkeypatch.delenv("EXCHANGE_RATE_URL", raising=False)
    monkeypatch.setattr(exchange_rate, "_FALLBACK_RATE", 95)

    def no_network(*args, **kwargs):
        raise AssertionError("в режиме фиксированного курса сеть не нужна")

    monkeypatch.setattr(exchange_rate.requests, "get", no_network)

    assert exchange_rate._fetch_usd_lcy("2026-06-01") == pytest.approx(95.0)


def test_rate_source_url_with_date_placeholder(monkeypatch):
    """EXCHANGE_RATE_URL с «{date}»: дата подставляется в URL, курс из {"rate": ...}."""
    from services import exchange_rate

    calls: list = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"rate": 105.0}

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        return _Resp()

    monkeypatch.setenv("EXCHANGE_RATE_URL", "https://rates.example.org/usd/{date}")
    monkeypatch.setattr(exchange_rate.requests, "get", fake_get)

    assert exchange_rate._fetch_usd_lcy("2026-06-01") == pytest.approx(105.0)
    assert calls == [("https://rates.example.org/usd/2026-06-01", None)]


def test_rate_source_url_without_placeholder_sends_date_param(monkeypatch):
    """EXCHANGE_RATE_URL без «{date}»: дата уходит параметром, ответ — голое число."""
    from services import exchange_rate

    calls: list = []

    class _Resp:
        status_code = 200

        def json(self):
            return 110.0

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        return _Resp()

    monkeypatch.setenv("EXCHANGE_RATE_URL", "https://rates.example.org/usd")
    monkeypatch.setattr(exchange_rate.requests, "get", fake_get)

    assert exchange_rate._fetch_usd_lcy("2026-06-01") == pytest.approx(110.0)
    assert calls == [("https://rates.example.org/usd", {"date": "2026-06-01"})]


@pytest.mark.parametrize("status, payload", [
    (500, {"rate": 105.0}),   # источник лежит
    (200, {"rate": 0}),       # неположительный курс
    (200, {"value": 105.0}),  # нет ключа rate
    (200, True),              # bool — не число
])
def test_rate_source_url_bad_answer_gives_none(monkeypatch, status, payload):
    """Плохой ответ источника → None (без подстановки фиксированного курса)."""
    from services import exchange_rate

    class _Resp:
        status_code = status

        def json(self):
            return payload

    monkeypatch.setenv("EXCHANGE_RATE_URL", "https://rates.example.org/usd/{date}")
    monkeypatch.setattr(
        exchange_rate.requests, "get", lambda url, params=None, timeout=None: _Resp()
    )

    assert exchange_rate._fetch_usd_lcy("2026-06-01") is None


# ---------------------------------------------------------------------------
# Скрипт бэкфилла
# ---------------------------------------------------------------------------

def test_backfill_script_dry_run_writes_nothing(monkeypatch, isolated_kb, capsys):
    """Без --apply скрипт всё считает, но в таблицу не пишет ни строки."""
    from scripts import backfill_cohorts

    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(1, WEEK_A + timedelta(days=1))])

    code = backfill_cohorts.main([
        "--since", WEEK_A.isoformat(),
        "--until", (WEEK_A + timedelta(days=6)).isoformat(),
        "--db-path", isolated_kb,
    ])

    assert code == 0
    assert "dry-run" in capsys.readouterr().out
    assert fetch_rows() == []


def test_backfill_script_apply_requires_confirmation(monkeypatch, isolated_kb):
    """--apply без слова-подтверждения отказывает и ничего не пишет."""
    from scripts import backfill_cohorts

    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [])

    code = backfill_cohorts.main([
        "--since", WEEK_A.isoformat(),
        "--until", (WEEK_A + timedelta(days=6)).isoformat(),
        "--db-path", isolated_kb,
        "--apply",
    ])

    assert code == 2
    assert fetch_rows() == []


def test_backfill_script_apply_writes_rows(monkeypatch, isolated_kb):
    """--apply с подтверждением собирает когорты и пишет их в таблицу."""
    from scripts import backfill_cohorts

    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(1, WEEK_A + timedelta(days=1))])

    code = backfill_cohorts.main([
        "--since", WEEK_A.isoformat(),
        "--until", (WEEK_A + timedelta(days=6)).isoformat(),
        "--db-path", isolated_kb,
        "--apply",
        "--confirm-production", backfill_cohorts.CONFIRM_PRODUCTION,
    ])

    assert code == 0
    assert len(fetch_rows()) == 1


def test_backfill_script_reports_rate_limit_without_writing(monkeypatch, isolated_kb):
    """Rate limit FB — ненулевой код возврата и пустая таблица."""
    from scripts import backfill_cohorts

    def angry_post(url, **kwargs):
        raise FBApiError("Facebook временно ограничил запросы", 429)

    monkeypatch.setattr(cb, "_throttled_post", angry_post)
    install_fake_amo(monkeypatch, [])

    code = backfill_cohorts.main([
        "--since", WEEK_A.isoformat(),
        "--until", (WEEK_A + timedelta(days=6)).isoformat(),
        "--db-path", isolated_kb,
        "--apply",
        "--confirm-production", backfill_cohorts.CONFIRM_PRODUCTION,
    ])

    assert code == 3
    assert fetch_rows() == []


def test_backfill_script_range_defaults_to_closed_weeks():
    """Без --since/--until диапазон кончается последней закрытой неделей."""
    from scripts import backfill_cohorts

    args = backfill_cohorts.build_parser().parse_args(["--weeks", "4"])
    since, until = backfill_cohorts.resolve_range(args)

    assert since.weekday() == 0
    assert until.weekday() == 6
    assert (until - since).days == 4 * 7 - 1


def test_schema_unique_pair_ad_and_week():
    """Пара (объявление, неделя) уникальна — дублей быть не может."""
    conn = ci._get_connection()
    try:
        values = (
            "ad-x", "2026-06-01", 7, 7, 0, "WEEK_NOT_CLOSED", 1, "2026-06-09",
        )
        sql = """
            INSERT INTO ad_weekly_cohorts (
                ad_id, week_start, days_covered, days_expected, comparable,
                not_comparable_reason, builder_version, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        conn.execute(sql, values)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, values)
    finally:
        conn.close()




# ---------------------------------------------------------------------------
# Выгрузка платежей по дням (замер показал: длинное окно теряет заметную долю записей)
# ---------------------------------------------------------------------------

def test_revenue_is_read_day_by_day(monkeypatch):
    """Платежи тянутся по одному дню, а не длинным окном.

    Короткое окно (несколько страниц) отдаёт все записи без потерь, а длинное
    (десятки страниц) — заметно меньше: глубокая пагинация молча теряет записи.
    """
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    calls: list = []
    install_payments(monkeypatch, [], calls=calls)

    until = WEEK_A + timedelta(days=6)
    cb.build_cohorts(WEEK_A, until, now=NOW)

    assert calls, "get_payments_strict не вызывался"
    # Каждое окно — ровно один день.
    assert all(day_from == day_to for day_from, day_to in calls)
    days = sorted({day_from for day_from, _ in calls})
    # Дни идут подряд, без дыр, и покрывают горизонт последней недели.
    assert days == [days[0] + timedelta(days=i) for i in range(len(days))]
    assert days[0] <= WEEK_A
    assert days[-1] >= until + timedelta(days=cb.REVENUE_HORIZON_DAYS)


def test_incomplete_day_does_not_become_zero(monkeypatch):
    """День, чью полноту ERP не подтвердила, гасит деньги недели в NULL.

    Главная защита: «выгрузка не бросила исключение» НЕ значит «все записи
    получены». Заявленный total обязан сойтись с собранным.
    """
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    install_rate(monkeypatch, RATE)

    torn_day = WEEK_A + timedelta(days=3)

    def torn_read(date_from, date_to, direction=None):
        # Один день посреди недели возвращает усечённую страницу.
        return strict_read([], complete=date_from != torn_day)

    monkeypatch.setattr(cb, "get_payments_strict", torn_read)

    summary = cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert summary["revenue_failed_spans"], "усечённый день обязан попасть в сводку"
    row = fetch_rows()[0]
    assert row["revenue_lcy"] is None, "неполный день не может давать ноль"
    assert row["payments"] is None
    assert row["romi_pct"] is None
    # Зрелость — свойство календаря, а не выгрузки.
    assert row["revenue_mature"] == 1
    # Квалы недели при этом сохранены: дыра в деньгах не рушит всю строку.
    assert row["comparable"] == 1


def test_failed_day_spares_weeks_outside_it(monkeypatch):
    """Упавший день гасит деньги только своих недель, остальные считаются."""
    from services.cdp_client import CdpError

    install_fake_fb(
        monkeypatch,
        full_week_rows("ad-1", WEEK_A) + full_week_rows("ad-2", WEEK_LATE),
    )
    install_fake_amo(monkeypatch, [
        amo_lead(101, WEEK_A + timedelta(days=1)),
        amo_lead(202, WEEK_LATE + timedelta(days=1)),
    ])
    paid = [
        payment(1, 101, WEEK_A + timedelta(days=2), amount=150_000.0),
        payment(2, 202, WEEK_LATE + timedelta(days=2), amount=900_000.0),
    ]
    install_rate(monkeypatch, RATE)

    broken_day = WEEK_LATE + timedelta(days=2)

    def flaky_read(date_from, date_to, direction=None):
        if date_from == broken_day:
            raise CdpError("CDP недоступен")
        return strict_read([
            item for item in paid
            if date_from <= date.fromisoformat(item["doc_date"]) <= date_to
            and (direction is None or item.get("direction") == direction)
        ])

    monkeypatch.setattr(cb, "get_payments_strict", flaky_read)

    summary = cb.build_cohorts(
        WEEK_A, WEEK_LATE + timedelta(days=6), now=NOW_LATE
    )

    assert summary["revenue_source_ok"] is True
    assert summary["revenue_failed_spans"]

    by_week = {row["week_start"]: row for row in fetch_rows()}
    early = by_week[WEEK_A.isoformat()]
    late = by_week[WEEK_LATE.isoformat()]

    # Ранняя неделя: её горизонт выгружен целиком — деньги известны.
    assert early["revenue_lcy"] == pytest.approx(150_000.0)
    assert early["payments"] == 1
    # Поздняя: часть оплат невидима, ноль был бы враньём.
    assert late["revenue_lcy"] is None
    assert late["payments"] is None
    assert late["romi_pct"] is None
    assert late["revenue_mature"] == 1


def test_all_days_down_means_source_unavailable(monkeypatch):
    """Легли все дни → источник недоступен, выручка NULL."""
    from services.cdp_client import CdpError

    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])

    def angry_cdp(date_from, date_to, direction=None):
        raise CdpError("CDP недоступен")

    monkeypatch.setattr(cb, "get_payments_strict", angry_cdp)

    summary = cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=6), now=NOW)

    assert summary["revenue_source_ok"] is False
    assert fetch_rows()[0]["revenue_lcy"] is None


def test_revenue_days_cover_window_without_gaps():
    """Перебор дней не теряет и не дублирует ни одного дня окна."""
    start, end = date(2026, 2, 2), date(2026, 8, 17)
    days = cb._revenue_days(start, end)

    assert days == sorted(days)
    assert len(days) == len(set(days))
    assert days[0] == start and days[-1] == end
    assert len(days) == (end - start).days + 1
    assert cb._revenue_days(end, start) == []


def test_revenue_window_covered_requires_whole_horizon():
    """Неделя покрыта, только если выгружен ВЕСЬ её горизонт оплат."""
    horizon = 14
    full = set(cb._revenue_days(WEEK_A, WEEK_A + timedelta(days=6 + horizon)))

    assert cb._revenue_window_covered(WEEK_A, horizon, full) is True
    # Нет последнего дня горизонта — хвост оплат невидим.
    assert cb._revenue_window_covered(
        WEEK_A, horizon, full - {WEEK_A + timedelta(days=6 + horizon)}
    ) is False
    # Нет дня внутри недели — тем более.
    assert cb._revenue_window_covered(
        WEEK_A, horizon, full - {WEEK_A + timedelta(days=3)}
    ) is False
    # None — «покрытие не отслеживалось», решает общий флаг источника.
    assert cb._revenue_window_covered(WEEK_A, horizon, None) is True


def test_truncated_range_does_not_lose_known_revenue(monkeypatch):
    """Край усечённого диапазона не теряет честно посчитанные деньги.

    Диапазон обрывается посреди недели: лиды есть только за 01–03.06, их
    горизонт оплат полностью внутри запрошенного окна. Требовать покрытия до
    воскресенья + горизонт — значит требовать дней, которые никогда не
    запрашивались, и молча обнулять известную выручку.
    """
    install_fake_fb(monkeypatch, [
        fb_row("ad-1", WEEK_A + timedelta(days=offset)) for offset in range(3)
    ])
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    install_payments(monkeypatch, [
        payment(1, 101, WEEK_A + timedelta(days=5), amount=150_000.0),
    ])
    install_rate(monkeypatch, RATE)

    cb.build_cohorts(WEEK_A, WEEK_A + timedelta(days=2), now=NOW_LATE)

    row = fetch_rows()[0]
    assert row["revenue_lcy"] == pytest.approx(150_000.0)
    assert row["payments"] == 1


def test_covered_window_clipped_to_requested_range():
    """Урезка окна недели границами диапазона — на уровне функции."""
    horizon = 14
    since, until = WEEK_A, WEEK_A + timedelta(days=2)
    # Выгружено ровно то, что запрашивалось: [since, until + горизонт].
    covered = set(cb._revenue_days(since, until + timedelta(days=horizon)))

    assert cb._revenue_window_covered(
        WEEK_A, horizon, covered, since, until
    ) is True
    # Без урезки та же выгрузка считалась бы неполной.
    assert cb._revenue_window_covered(WEEK_A, horizon, covered) is False
    # Дыра внутри урезанного окна по-прежнему валит проверку.
    assert cb._revenue_window_covered(
        WEEK_A, horizon, covered - {WEEK_A + timedelta(days=1)}, since, until
    ) is False


def test_revenue_window_does_not_reach_into_future(monkeypatch):
    """Дни после сегодняшнего у ERP не спрашиваем — платежей оттуда не бывает.

    В кроне until = сегодня, и без обрезки треть запросов прогона (горизонт
    в 14 дней × два направления) уходила в пустоту.
    """
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    calls: list = []
    install_payments(monkeypatch, [], calls=calls)

    until = WEEK_A + timedelta(days=6)
    cb.build_cohorts(WEEK_A, until, now=NOW)

    today = NOW.astimezone(_TZ_LOCAL).date()
    assert calls
    assert max(day_to for _, day_to in calls) <= today
    # При этом горизонт прошлых недель по-прежнему выгружается целиком.
    assert max(day_to for _, day_to in calls) >= until


def test_past_range_still_gets_full_horizon(monkeypatch):
    """Для давно закрытой недели горизонт запрашивается полностью."""
    install_fake_fb(monkeypatch, full_week_rows("ad-1", WEEK_A))
    install_fake_amo(monkeypatch, [amo_lead(101, WEEK_A + timedelta(days=1))])
    calls: list = []
    install_payments(monkeypatch, [], calls=calls)

    until = WEEK_A + timedelta(days=6)
    # «Сейчас» — намного позже недели, обрезка по сегодня ничего не отрежет.
    cb.build_cohorts(WEEK_A, until, now=NOW_LATE)

    assert max(day_to for _, day_to in calls) >= until + timedelta(
        days=cb.REVENUE_HORIZON_DAYS
    )
