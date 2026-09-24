"""
Юнит-тесты для services/hypothesis_verdict.py.

Проверяют:
- _fetch_facts: агрегация из creative_kb, None-семантика payments
  (outcomes_matched_at IS NULL != 0 оплат)
- _evaluate: формулы confirmed/refuted/inconclusive по метрикам
  payments/cpl/qual_pct (§8 спеки)
- run_hypothesis_verdict: полный проход — confirmed/refuted/inconclusive,
  возрастные гейты (7-14 дней), принудительное закрытие >14 дней,
  запись урока в learnings (source='hypothesis') со связкой learning_id
- run_verdict_pass: never-throw обёртка

Используют tmp-базу через ci.init_kb(tmp_path) — не трогают реальную БД.
Без сети (creative_kb наполняется напрямую через sqlite3, без FB/AMO).
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта любого модуля который может его тянуть
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())

from services import creative_intelligence as ci
from services.hypothesis_journal import record_hypothesis
from services.hypothesis_verdict import (
    HYP_MIN_SPEND,
    MAX_AGE_DAYS,
    _evaluate,
    _fetch_facts,
    run_hypothesis_verdict,
    run_verdict_pass,
)


# ---------------------------------------------------------------------------
# Фикстуры и хелперы
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_kb_path():
    """Сбрасываем DB_PATH до и после теста."""
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


@pytest.fixture
def kb(tmp_path):
    """Инициализирует пустую KB во временной директории и возвращает путь."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)
    return db_path


def _insert_ad(
    db_path: str,
    ad_id: str,
    city: str = "CityA",
    spend: float = 100.0,
    leads: int = 10,
    payments=0,
    qual_leads=0,
    matched: bool = True,
) -> None:
    """Вставляет объявление в creative_kb для тестов фактов вердикта.

    matched=True → outcomes_matched_at заполнен (сверка была).
    matched=False → outcomes_matched_at NULL (сверки не было — payments IS NULL,
    т.к. миграция 010 обнуляет payments=0/qual_leads=0 в NULL при NULL-сверке;
    здесь эмулируем то же самое явной вставкой NULL, чтобы не зависеть от миграции).
    """
    outcomes_matched_at = "2026-06-20 10:00:00" if matched else None
    payments_value = payments if matched else None
    qual_leads_value = qual_leads if matched else None

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO creative_kb
                (ad_id, ad_name, city, spend, leads, payments, qual_leads, outcomes_matched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ad_id, f"Ad {ad_id}", city, spend, leads, payments_value, qual_leads_value, outcomes_matched_at),
        )
        conn.commit()
    finally:
        conn.close()


def _create_open_hypothesis(
    db_path: str,
    now: datetime,
    age_days: int,
    ad_ids: list[str],
    city: str = "CityA",
    metric: str = "payments",
    threshold=1,
    ad_format: str = "carousel",
    angle: str = "Карусель",
) -> int:
    """Создаёт открытую гипотезу нужного возраста через record_hypothesis.

    build_expectation здесь не участвует напрямую — гипотеза создаётся с
    topic, содержащим reference с payments=threshold-1 (чтобы выйти на
    metric=payments), либо мы просто перезаписываем expectation_json вручную
    для точного контроля над метрикой теста.
    """
    created_at = now - timedelta(days=age_days)
    topic = {
        "angle": angle,
        "ad_format": ad_format,
        "segment": "общий",
        "source": "manual",
        "reference": None,
    }
    ids = record_hypothesis("Карточка теста", "traffic", {city: ad_ids}, topic, now=created_at)
    assert len(ids) == 1
    hyp_id = ids[0]

    expectation = {"metric": metric, "op": ">=" if metric != "cpl" else "<=", "threshold": threshold, "basis": "тест"}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE hypotheses SET expectation_json = ? WHERE id = ?",
            (json.dumps(expectation, ensure_ascii=False), hyp_id),
        )
        conn.commit()
    finally:
        conn.close()
    return hyp_id


def _get_hypothesis(db_path: str, hyp_id: int) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM hypotheses WHERE id = ?", (hyp_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _count_learnings(db_path: str, source: str = "hypothesis") -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM learnings WHERE source = ?", (source,)
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _fetch_facts
# ---------------------------------------------------------------------------

def test_fetch_facts_none_payments_not_treated_as_zero(kb):
    """payments IS NULL (не сверялись) != 0: matched_rows=0, payments=0 в выводе,
    но verdict-логика (_evaluate) обязана отличать это от «сверка была, 0 оплат»."""
    _insert_ad(kb, "ad1", spend=100.0, leads=10, matched=False)

    facts = _fetch_facts(["ad1"])

    assert facts["matched_rows"] == 0
    assert facts["payments"] == 0  # COALESCE эффект, но matched_rows=0 сигнализирует "нет данных"
    assert facts["spend"] == pytest.approx(100.0)
    assert facts["leads"] == 10


def test_fetch_facts_matched_with_payments(kb):
    """Сверка была, есть оплаты — агрегируется корректно."""
    _insert_ad(kb, "ad1", spend=100.0, leads=10, payments=3, qual_leads=5, matched=True)

    facts = _fetch_facts(["ad1"])

    assert facts["matched_rows"] == 1
    assert facts["payments"] == 3
    assert facts["qual_pct"] == pytest.approx(50.0)
    assert facts["cpl"] == pytest.approx(10.0)


def test_fetch_facts_empty_ad_ids(kb):
    """Пустой список ad_ids → нулевые факты, без запроса к БД."""
    facts = _fetch_facts([])
    assert facts["matched_rows"] == 0
    assert facts["spend"] == 0.0
    assert facts["payments"] == 0


def test_fetch_facts_mixed_matched_unmatched(kb):
    """Несколько объявлений: одни сверены, другие нет — учитываем только сверенные для payments/qual."""
    _insert_ad(kb, "ad1", spend=50.0, leads=5, payments=2, qual_leads=3, matched=True)
    _insert_ad(kb, "ad2", spend=30.0, leads=3, matched=False)

    facts = _fetch_facts(["ad1", "ad2"])

    assert facts["matched_rows"] == 1
    assert facts["payments"] == 2
    assert facts["spend"] == pytest.approx(80.0)  # spend считается по ВСЕМ строкам
    assert facts["leads"] == 8


# ---------------------------------------------------------------------------
# _evaluate
# ---------------------------------------------------------------------------

def test_evaluate_confirmed_by_payments():
    """Есть оплаты >= порога → confirmed, урок человекочитаемый без ad_id/SQL."""
    hyp = {
        "angle": "Карусель", "city": "CityD", "ad_format": "carousel",
        "ad_ids": ["1", "2", "3"], "age_days": 12,
        "expectation": {"metric": "payments", "op": ">=", "threshold": 1},
    }
    facts = {"payments": 3, "matched_rows": 3, "spend": 60.0, "leads": 20, "qual_pct": None, "cpl": None}

    verdict, lesson = _evaluate(hyp, facts)

    assert verdict == "confirmed"
    assert "3 оплат" in lesson
    assert "12 дней" in lesson
    assert "sql" not in lesson.lower()
    assert "1" not in lesson.split(":")[0]  # ad_id не должны попадать в текст


def test_evaluate_refuted_zero_payments():
    """Сверка была (matched_rows>0), оплат 0, расход достаточный → refuted, урок «не конвертит»."""
    hyp = {
        "angle": "Карусель", "city": "CityD", "ad_format": "carousel",
        "ad_ids": ["1", "2", "3"], "age_days": 12,
        "expectation": {"metric": "payments", "op": ">=", "threshold": 1},
    }
    facts = {"payments": 0, "matched_rows": 3, "spend": 60.0, "leads": 20, "qual_pct": None, "cpl": None}

    verdict, lesson = _evaluate(hyp, facts)

    assert verdict == "refuted"
    assert "не конвертит" in lesson
    assert "3 запусков" in lesson
    assert "0 оплат" in lesson
    assert "12 дней" in lesson


def test_evaluate_inconclusive_low_spend():
    """Расход ниже HYP_MIN_SPEND → inconclusive, даже если сверка была."""
    hyp = {
        "angle": "Карусель", "city": "CityD", "ad_format": "carousel",
        "ad_ids": ["1"], "age_days": 10,
        "expectation": {"metric": "payments", "op": ">=", "threshold": 1},
    }
    facts = {"payments": 0, "matched_rows": 1, "spend": HYP_MIN_SPEND - 1, "leads": 2, "qual_pct": None, "cpl": None}

    verdict, lesson = _evaluate(hyp, facts)

    assert verdict == "inconclusive"
    assert "Мало данных" in lesson


def test_evaluate_inconclusive_matched_zero():
    """matched_rows == 0 (сверка вообще не пришла) → inconclusive, а НЕ refuted."""
    hyp = {
        "angle": "Карусель", "city": "CityD", "ad_format": "carousel",
        "ad_ids": ["1"], "age_days": 10,
        "expectation": {"metric": "payments", "op": ">=", "threshold": 1},
    }
    facts = {"payments": 0, "matched_rows": 0, "spend": 100.0, "leads": 10, "qual_pct": None, "cpl": None}

    verdict, lesson = _evaluate(hyp, facts)

    assert verdict == "inconclusive"
    assert verdict != "refuted"


def test_evaluate_cpl_confirmed():
    """CPL факта <= порога → confirmed."""
    hyp = {
        "angle": "Видео", "city": "CityA", "ad_format": "video_speaker",
        "ad_ids": ["1"], "age_days": 10,
        "expectation": {"metric": "cpl", "op": "<=", "threshold": 5.0},
    }
    facts = {"payments": 0, "matched_rows": 1, "spend": 40.0, "leads": 10, "qual_pct": None, "cpl": 4.0}

    verdict, lesson = _evaluate(hyp, facts)

    assert verdict == "confirmed"
    assert "подтвердилось" in lesson


def test_evaluate_cpl_refuted():
    """CPL факта > порога → refuted, «дороже ожидания»."""
    hyp = {
        "angle": "Видео", "city": "CityA", "ad_format": "video_speaker",
        "ad_ids": ["1"], "age_days": 10,
        "expectation": {"metric": "cpl", "op": "<=", "threshold": 5.0},
    }
    facts = {"payments": 0, "matched_rows": 1, "spend": 100.0, "leads": 10, "qual_pct": None, "cpl": 10.0}

    verdict, lesson = _evaluate(hyp, facts)

    assert verdict == "refuted"
    assert "дороже ожидания" in lesson


def test_evaluate_qual_pct_confirmed():
    """qual_pct факта >= порога → confirmed."""
    hyp = {
        "angle": "Статика", "city": "CityC", "ad_format": "static",
        "ad_ids": ["1"], "age_days": 10,
        "expectation": {"metric": "qual_pct", "op": ">=", "threshold": 12.0},
    }
    facts = {"payments": 0, "matched_rows": 1, "spend": 50.0, "leads": 10, "qual_pct": 20.0, "cpl": 5.0}

    verdict, lesson = _evaluate(hyp, facts)

    assert verdict == "confirmed"
    assert "квал" in lesson


# ---------------------------------------------------------------------------
# run_hypothesis_verdict
# ---------------------------------------------------------------------------

def test_run_verdict_confirmed_writes_learning(kb):
    """Гипотеза 12 дней, факт подтверждает ожидание → confirmed + запись урока со связкой."""
    now = datetime(2026, 7, 2, 6, 0, 0)
    _insert_ad(kb, "100", city="CityD", spend=60.0, leads=10, payments=3, qual_leads=4, matched=True)
    hyp_id = _create_open_hypothesis(
        kb, now, age_days=12, ad_ids=["100"], city="CityD",
        metric="payments", threshold=1, ad_format="carousel", angle="Карусель",
    )

    result = run_hypothesis_verdict(now=now)

    assert result["confirmed"] == 1
    assert result["learnings_written"] == 1

    hyp = _get_hypothesis(kb, hyp_id)
    assert hyp["status"] == "confirmed"
    assert hyp["verdict_at"] is not None
    assert hyp["lesson"] is not None
    assert hyp["learning_id"] is not None
    assert _count_learnings(kb) == 1


def test_run_verdict_refuted(kb):
    """Гипотеза 12 дней, сверка была, оплат 0, расход достаточный → refuted."""
    now = datetime(2026, 7, 2, 6, 0, 0)
    _insert_ad(kb, "200", city="CityD", spend=60.0, leads=15, payments=0, qual_leads=0, matched=True)
    hyp_id = _create_open_hypothesis(
        kb, now, age_days=12, ad_ids=["200"], city="CityD",
        metric="payments", threshold=1, ad_format="carousel", angle="Карусель",
    )

    result = run_hypothesis_verdict(now=now)

    assert result["refuted"] == 1
    hyp = _get_hypothesis(kb, hyp_id)
    assert hyp["status"] == "refuted"
    assert "не конвертит" in hyp["lesson"]


def test_run_verdict_inconclusive_low_spend_stays_open(kb):
    """Мало расхода, возраст 10 дней (< MAX_AGE_DAYS) → остаётся open, still_open++."""
    now = datetime(2026, 7, 2, 6, 0, 0)
    _insert_ad(kb, "300", city="CityB", spend=5.0, leads=1, matched=True)
    hyp_id = _create_open_hypothesis(
        kb, now, age_days=10, ad_ids=["300"], city="CityB",
        metric="payments", threshold=1,
    )

    result = run_hypothesis_verdict(now=now)

    assert result["still_open"] == 1
    assert result["confirmed"] == 0
    assert result["refuted"] == 0
    hyp = _get_hypothesis(kb, hyp_id)
    assert hyp["status"] == "open"


def test_run_verdict_inconclusive_matched_zero_stays_open(kb):
    """matched_rows=0 (payments NULL — не сверялись), возраст 10 дней → still_open, НЕ refuted."""
    now = datetime(2026, 7, 2, 6, 0, 0)
    _insert_ad(kb, "400", city="CityG", spend=100.0, leads=10, matched=False)
    hyp_id = _create_open_hypothesis(
        kb, now, age_days=10, ad_ids=["400"], city="CityG",
        metric="payments", threshold=1,
    )

    result = run_hypothesis_verdict(now=now)

    assert result["still_open"] == 1
    assert result["refuted"] == 0
    hyp = _get_hypothesis(kb, hyp_id)
    assert hyp["status"] == "open"


def test_run_verdict_force_close_after_max_age(kb):
    """Возраст > MAX_AGE_DAYS (14) при недостатке данных → принудительно inconclusive, урок «не накопилось»."""
    now = datetime(2026, 7, 2, 6, 0, 0)
    _insert_ad(kb, "500", city="CityH", spend=100.0, leads=10, matched=False)
    hyp_id = _create_open_hypothesis(
        kb, now, age_days=MAX_AGE_DAYS + 1, ad_ids=["500"], city="CityH",
        metric="payments", threshold=1,
    )

    result = run_hypothesis_verdict(now=now)

    assert result["inconclusive"] == 1
    assert result["still_open"] == 0
    hyp = _get_hypothesis(kb, hyp_id)
    assert hyp["status"] == "inconclusive"
    assert "данных не накопилось" in hyp["lesson"]
    assert hyp["learning_id"] is not None
    assert _count_learnings(kb) == 1


def test_run_verdict_learning_has_source_and_link(kb):
    """Закрытая гипотеза порождает урок в learnings source='hypothesis', связанный learning_id."""
    now = datetime(2026, 7, 2, 6, 0, 0)
    _insert_ad(kb, "600", city="CityI", spend=60.0, leads=10, payments=2, qual_leads=2, matched=True)
    hyp_id = _create_open_hypothesis(
        kb, now, age_days=12, ad_ids=["600"], city="CityI",
        metric="payments", threshold=1,
    )

    run_hypothesis_verdict(now=now)

    hyp = _get_hypothesis(kb, hyp_id)
    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    try:
        learning = conn.execute(
            "SELECT * FROM learnings WHERE id = ?", (hyp["learning_id"],)
        ).fetchone()
    finally:
        conn.close()

    assert learning is not None
    assert learning["source"] == "hypothesis"
    assert "600" not in learning["statement"]  # ad_id не в тексте
    assert "600" in learning["evidence_ad_ids"]  # ad_id в evidence


def test_run_verdict_below_min_age_not_touched(kb):
    """Гипотеза младше 7 дней вообще не попадает в выборку get_open_hypotheses."""
    now = datetime(2026, 7, 2, 6, 0, 0)
    _insert_ad(kb, "700", city="CityN", spend=100.0, leads=10, payments=5, matched=True)
    hyp_id = _create_open_hypothesis(
        kb, now, age_days=5, ad_ids=["700"], city="CityN",
        metric="payments", threshold=1,
    )

    result = run_hypothesis_verdict(now=now)

    assert result["evaluated"] == 0
    hyp = _get_hypothesis(kb, hyp_id)
    assert hyp["status"] == "open"


def test_run_verdict_no_open_hypotheses(kb):
    """Пустая БД гипотез → нулевая статистика, не падает."""
    now = datetime(2026, 7, 2, 6, 0, 0)
    result = run_hypothesis_verdict(now=now)
    assert result == {
        "evaluated": 0, "confirmed": 0, "refuted": 0,
        "inconclusive": 0, "still_open": 0, "learnings_written": 0,
    }


# ---------------------------------------------------------------------------
# run_verdict_pass — never-throw обёртка
# ---------------------------------------------------------------------------

def test_run_verdict_pass_kb_not_initialized():
    """KB не инициализирована → never-throw обёртка ловит RuntimeError, возвращает error-ключ."""
    ci.DB_PATH = None  # гарантируем что _get_connection бросит RuntimeError

    result = run_verdict_pass()

    assert "error" in result
    assert result["evaluated"] == 0


def test_run_verdict_pass_happy(kb):
    """При нормальной работе обёртка отдаёт ту же статистику, без ключа error."""
    now = datetime(2026, 7, 2, 6, 0, 0)
    result = run_verdict_pass(now=now)
    assert "error" not in result
    assert result["evaluated"] == 0


# ---------------------------------------------------------------------------
# Смешанные aware/naive даты (встречались в журнале)
# ---------------------------------------------------------------------------
#
# «hypothesis_verdict: сбой прохода вердикта: can't subtract offset-naive and
# offset-aware datetimes». Крон web/app.py:2131 передаёт datetime.now(_TZ_LOCAL)
# (aware), а hypotheses.created_at хранится naive-локальной строкой — вычитание
# в hypothesis_journal._row_to_dict роняло весь проход.

_TZ_LOCAL_TEST = timezone(timedelta(hours=5))


def test_run_verdict_accepts_aware_now_from_cron(kb):
    """Aware-now крона не роняет проход и даёт тот же вердикт, что naive."""
    naive_now = datetime(2026, 7, 28, 6, 21, 0)
    aware_now = naive_now.astimezone().astimezone(_TZ_LOCAL_TEST)

    _insert_ad(kb, "200", city="CityB", spend=60.0, leads=10, payments=3, qual_leads=4, matched=True)
    hyp_id = _create_open_hypothesis(
        kb, naive_now, age_days=12, ad_ids=["200"], city="CityB",
        metric="payments", threshold=1,
    )

    result = run_hypothesis_verdict(now=aware_now)

    assert "error" not in result
    assert result["confirmed"] == 1
    hyp = _get_hypothesis(kb, hyp_id)
    assert hyp["status"] == "confirmed"
    assert hyp["verdict_at"] is not None


def test_verdict_pass_no_longer_swallows_tz_error(kb):
    """Never-throw обёртка больше не прячет TypeError смешанных дат."""
    naive_now = datetime(2026, 7, 28, 6, 21, 0)
    aware_now = naive_now.astimezone().astimezone(_TZ_LOCAL_TEST)

    _insert_ad(kb, "201", city="CityA", spend=60.0, leads=10, payments=3, qual_leads=4, matched=True)
    _create_open_hypothesis(
        kb, naive_now, age_days=12, ad_ids=["201"], metric="payments", threshold=1,
    )

    result = run_verdict_pass(now=aware_now)

    assert "error" not in result
    assert result["evaluated"] == 1


def test_open_hypotheses_age_is_same_for_aware_and_naive_now(kb):
    """age_days не зависит от того, aware или naive пришёл now."""
    from services.hypothesis_journal import get_open_hypotheses

    naive_now = datetime(2026, 7, 28, 6, 21, 0)
    aware_now = naive_now.astimezone().astimezone(_TZ_LOCAL_TEST)
    _create_open_hypothesis(
        kb, naive_now, age_days=9, ad_ids=["202"], metric="payments", threshold=1,
    )

    naive_rows = get_open_hypotheses(min_age_days=7, max_age_days=999, now=naive_now)
    aware_rows = get_open_hypotheses(min_age_days=7, max_age_days=999, now=aware_now)

    assert [row["age_days"] for row in naive_rows] == [9]
    assert [row["age_days"] for row in aware_rows] == [9]


def test_record_hypothesis_accepts_aware_now(kb):
    """Запись гипотезы с aware-now кладёт created_at в той же конвенции."""
    from services.hypothesis_journal import get_open_hypotheses

    naive_now = datetime(2026, 7, 18, 6, 21, 0)
    aware_created = naive_now.astimezone().astimezone(_TZ_LOCAL_TEST)
    topic = {"angle": "Карусель", "ad_format": "carousel", "segment": "общий",
             "source": "manual", "reference": None}

    ids = record_hypothesis("Карточка", "traffic", {"CityA": ["203"]}, topic, now=aware_created)

    assert len(ids) == 1
    rows = get_open_hypotheses(
        min_age_days=7, max_age_days=999, now=datetime(2026, 7, 28, 6, 21, 0)
    )
    assert [row["age_days"] for row in rows] == [10]
