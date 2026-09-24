"""
Юнит-тесты для services/weekly_learning_report.py.

Проверяют:
- build_weekly_report: секции наполняются данными (confirmed/refuted/уроки/
  предикторы/мёртвые комбо), пустая неделя → «пока нет»/«без новых уроков»,
  сбой одной секции (pattern_engine) не роняет весь отчёт.
- send_weekly_learning_report: never-throw обёртка, отправляет через
  check→render→checked delivery; enabled=False — не шлёт.

Используют tmp-базу через ci.init_kb(tmp_path) — не трогают реальную БД.
Без сети (checked delivery мокается).
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта любого модуля, который может его тянуть
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())

from services import creative_intelligence as ci  # noqa: E402
from services.hypothesis_journal import record_hypothesis  # noqa: E402
from services.weekly_learning_report import (  # noqa: E402
    build_weekly_report,
    send_weekly_learning_report,
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


def _create_closed_hypothesis(
    db_path: str,
    now: datetime,
    status: str,
    verdict_at: datetime,
    angle: str = "Карусель",
    city: str = "CityD",
    ad_format: str = "carousel",
    lesson: str | None = None,
) -> int:
    """Создаёт открытую гипотезу через record_hypothesis, затем закрывает её вручную
    (hypothesis_verdict.py не участвует — тестируем отчёт изолированно)."""
    topic = {
        "angle": angle, "ad_format": ad_format, "segment": "общий",
        "source": "manual", "reference": None,
    }
    created_at = verdict_at - timedelta(days=1)
    ids = record_hypothesis("Карточка теста", "traffic", {city: [f"ad_{angle}_{city}"]}, topic, now=created_at)
    assert len(ids) == 1
    hyp_id = ids[0]

    lesson_text = lesson or f"{ad_format} / {angle} в {city}: тестовый урок"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE hypotheses SET status = ?, verdict_at = ?, lesson = ? WHERE id = ?",
            (status, verdict_at.strftime("%Y-%m-%d %H:%M:%S"), lesson_text, hyp_id),
        )
        conn.commit()
    finally:
        conn.close()
    return hyp_id


def _insert_creative_kb_row(
    db_path: str,
    ad_id: str,
    ad_name: str,
    target_product: str | None = None,
    status: str = "ACTIVE",
    spend: float = 100.0,
) -> None:
    """Вставляет строку creative_kb напрямую (по образцу tests/test_product_report.py)
    — для секции _section_products недельного отчёта."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO creative_kb (ad_id, ad_name, target_product, status, spend) "
            "VALUES (?, ?, ?, ?, ?)",
            (ad_id, ad_name, target_product, status, spend),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_hypothesis_learning(db_path: str, statement: str, created_at: datetime) -> None:
    """Пишет урок source='hypothesis' напрямую в learnings (эмулирует hypothesis_verdict)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO learnings (statement, evidence_ad_ids, confidence, source, tags, created_at)
            VALUES (?, '[]', 'confirmed', 'hypothesis', 'hypothesis', ?)
            """,
            (statement, created_at.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# build_weekly_report — пустая неделя
# ---------------------------------------------------------------------------

def test_build_weekly_report_empty_db_returns_valid_text(kb):
    """Пустая БД → валидный текст, все секции показывают «пока нет», не падает."""
    now = datetime(2026, 7, 5, 20, 0, 0)  # воскресенье

    with patch("services.pattern_engine.get_patterns_summary", return_value={
        "predictors": [], "data_sufficiency": "insufficient", "n_success": 0, "n_fail": 0,
    }):
        text = build_weekly_report(now=now)

    assert isinstance(text, str)
    assert "Чему я научился" in text
    assert "подтверждений не было" in text
    assert "опровержений не было" in text
    assert "эта неделя без новых уроков" in text
    assert "данных недостаточно" in text
    assert "мёртвых комбо пока нет" in text


# ---------------------------------------------------------------------------
# build_weekly_report — секции наполняются
# ---------------------------------------------------------------------------

def test_build_weekly_report_sections_filled_with_seeded_data(kb):
    """Засеянные confirmed/refuted гипотезы и уроки → секции наполняются реальными данными."""
    now = datetime(2026, 7, 5, 20, 0, 0)

    _create_closed_hypothesis(
        kb, now, status="confirmed", verdict_at=now - timedelta(days=2),
        angle="Видео-спикер", city="CityA", ad_format="video_speaker",
        lesson="video_speaker / Видео-спикер в CityA: подтвердилось — 3 оплаты за 12 дней",
    )
    _create_closed_hypothesis(
        kb, now, status="refuted", verdict_at=now - timedelta(days=1),
        angle="Карусель", city="CityD", ad_format="carousel",
        lesson="carousel / Карусель в CityD не конвертит: 3 запуска, 0 оплат за 12 дней",
    )
    _insert_hypothesis_learning(
        kb, "carousel / Карусель в CityD не конвертит: 3 запуска, 0 оплат за 12 дней",
        now - timedelta(days=1),
    )

    with patch("services.pattern_engine.get_patterns_summary", return_value={
        "predictors": [{"metric": "hook_rate", "statement": "Ранний предиктор: hook_rate >= 30% связан с успехом"}],
        "data_sufficiency": "ok", "n_success": 10, "n_fail": 10,
    }):
        text = build_weekly_report(now=now)

    # hypothesis_journal.get_hypotheses_by_status (контракт T3) не отдаёт поле
    # lesson в _row_to_dict, поэтому отчёт строит читаемую строку из angle/city
    # как fallback (см. _section_confirmed/_section_refuted).
    assert "Видео-спикер в CityA" in text
    assert "Карусель в CityD" in text
    assert "Ранний предиктор: hook_rate" in text
    # ad_id НЕ должны попадать в человекочитаемый текст отчёта
    assert "ad_Видео-спикер_CityA" not in text


def test_build_weekly_report_dead_combo_in_what_changes(kb):
    """Мёртвое комбо (2 refuted, 0 confirmed) попадает в секцию «что меняем»."""
    now = datetime(2026, 7, 5, 20, 0, 0)

    _create_closed_hypothesis(
        kb, now, status="refuted", verdict_at=now - timedelta(days=1),
        angle="Мёртвый угол", city="CityH", ad_format="static",
    )
    _create_closed_hypothesis(
        kb, now, status="refuted", verdict_at=now - timedelta(days=2),
        angle="Мёртвый угол", city="CityH", ad_format="static",
    )

    with patch("services.pattern_engine.get_patterns_summary", return_value={
        "predictors": [], "data_sufficiency": "insufficient", "n_success": 0, "n_fail": 0,
    }):
        text = build_weekly_report(now=now)

    assert "меньше пробуем" in text
    assert "Мёртвый угол" in text
    assert "CityH" in text


def test_build_weekly_report_learning_outside_week_excluded(kb):
    """Урок старше недели не попадает в топ-уроки (фильтр по created_at)."""
    now = datetime(2026, 7, 5, 20, 0, 0)
    _insert_hypothesis_learning(kb, "Старый урок вне недели", now - timedelta(days=20))

    with patch("services.pattern_engine.get_patterns_summary", return_value={
        "predictors": [], "data_sufficiency": "insufficient", "n_success": 0, "n_fail": 0,
    }):
        text = build_weekly_report(now=now)

    assert "Старый урок вне недели" not in text
    assert "эта неделя без новых уроков" in text


# ---------------------------------------------------------------------------
# build_weekly_report — сбой pattern_engine не роняет отчёт
# ---------------------------------------------------------------------------

def test_build_weekly_report_pattern_engine_failure_does_not_crash(kb):
    """pattern_engine.get_patterns_summary бросает исключение → секция подставляет
    заглушку, остальной отчёт строится нормально (каждая секция в своём try)."""
    now = datetime(2026, 7, 5, 20, 0, 0)

    with patch("services.pattern_engine.get_patterns_summary", side_effect=RuntimeError("KB не инициализирована")):
        text = build_weekly_report(now=now)

    assert isinstance(text, str)
    assert "Чему я научился" in text
    # Остальные секции всё равно на месте
    assert "подтверждений не было" in text
    assert "мёртвых комбо пока нет" in text


def test_build_weekly_report_never_raises_on_full_failure(kb):
    """Даже если несколько секций падают — build_weekly_report не бросает исключений."""
    now = datetime(2026, 7, 5, 20, 0, 0)

    with patch("services.pattern_engine.get_patterns_summary", side_effect=RuntimeError("boom")), \
         patch("services.hypothesis_influence.load_verdict_weights", side_effect=RuntimeError("boom2")):
        text = build_weekly_report(now=now)

    assert isinstance(text, str)
    assert len(text) > 0


# ---------------------------------------------------------------------------
# send_weekly_learning_report
# ---------------------------------------------------------------------------

def test_send_weekly_learning_report_calls_telegram(kb):
    """Успешный сценарий проходит check→render→checked delivery."""
    now = datetime(2026, 7, 5, 20, 0, 0)
    enabled_cfg = {"hypothesist": {"enabled": True}}
    request = MagicMock()
    request.payload.generated_at = now.replace(tzinfo=timezone.utc)
    check_result = MagicMock()
    rendered = MagicMock()

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_cfg), \
         patch("services.weekly_learning_report.build_weekly_report_request", return_value=request), \
         patch("services.weekly_learning_report.check_report", return_value=check_result) as mock_check, \
         patch("services.weekly_learning_report.render_checked_report", return_value=rendered) as mock_render, \
         patch("services.weekly_learning_report.send_checked_report", return_value=MagicMock(sent=True)) as mock_send:
        result = send_weekly_learning_report(now=now)

    assert result is True
    mock_check.assert_called_once_with(request, now=request.payload.generated_at)
    mock_render.assert_called_once_with(request, check_result)
    mock_send.assert_called_once_with(rendered, channel="ads")


def test_send_weekly_learning_report_disabled_does_not_send(kb):
    """hypothesist.enabled=False → отчёт не строится и не шлётся, возвращает False."""
    now = datetime(2026, 7, 5, 20, 0, 0)
    disabled_cfg = {"hypothesist": {"enabled": False}}

    with patch("services.autopilot.get_autopilot_config", return_value=disabled_cfg), \
         patch("services.weekly_learning_report.send_checked_report") as mock_send:
        result = send_weekly_learning_report(now=now)

    assert result is False
    mock_send.assert_not_called()


def test_send_weekly_learning_report_telegram_failure_returns_false(kb):
    """Checked delivery вернул False → функция возвращает False, не падает."""
    now = datetime(2026, 7, 5, 20, 0, 0)
    enabled_cfg = {"hypothesist": {"enabled": True}}
    request = MagicMock()
    request.payload.generated_at = now.replace(tzinfo=timezone.utc)

    with patch("services.autopilot.get_autopilot_config", return_value=enabled_cfg), \
         patch("services.weekly_learning_report.build_weekly_report_request", return_value=request), \
         patch("services.weekly_learning_report.check_report", return_value=MagicMock()), \
         patch("services.weekly_learning_report.render_checked_report", return_value=MagicMock()), \
         patch("services.weekly_learning_report.send_checked_report", return_value=MagicMock(sent=False)):
        result = send_weekly_learning_report(now=now)

    assert result is False


def test_send_weekly_learning_report_never_throws_on_exception(kb):
    """Любое исключение (например, БД недоступна) — функция ловит, возвращает False, не падает."""
    now = datetime(2026, 7, 5, 20, 0, 0)

    with patch("services.autopilot.get_autopilot_config", side_effect=RuntimeError("config boom")), \
         patch("services.weekly_learning_report.send_fact_free"):
        result = send_weekly_learning_report(now=now)

    assert result is False


# ---------------------------------------------------------------------------
# _section_products / build_weekly_report — секция «Какого продукта больше»
# (T5, docs/specs/ARCH-product-tags.md §7, §9)
# ---------------------------------------------------------------------------

def test_build_weekly_report_products_section_present_with_data(kb):
    """Засеянные ACTIVE объявления с target_product → секция «Какого продукта
    больше» присутствует в отчёте с долями по продуктам."""
    now = datetime(2026, 7, 5, 20, 0, 0)

    _insert_creative_kb_row(kb, "ad-p1", "CityA | тема", target_product="PRODA", spend=100.0)
    _insert_creative_kb_row(kb, "ad-p2", "CityB | тема", target_product="PRODB", spend=50.0)
    _insert_creative_kb_row(kb, "ad-p3", "CityD | тема", target_product="PRODB", spend=50.0)

    with patch("services.pattern_engine.get_patterns_summary", return_value={
        "predictors": [], "data_sufficiency": "insufficient", "n_success": 0, "n_fail": 0,
    }):
        text = build_weekly_report(now=now)

    assert "Какого продукта больше" in text
    assert "PRODB" in text
    assert "PRODA" in text
    assert "67%" in text  # 2 из 3 объявлений — PRODB
    assert "33%" in text  # 1 из 3 — PRODA


def test_build_weekly_report_products_section_empty_shows_placeholder(kb):
    """Нет активных объявлений с разметкой продукта → секция показывает
    заглушку «пока нет», не падает, остальные секции на месте."""
    now = datetime(2026, 7, 5, 20, 0, 0)

    with patch("services.pattern_engine.get_patterns_summary", return_value={
        "predictors": [], "data_sufficiency": "insufficient", "n_success": 0, "n_fail": 0,
    }):
        text = build_weekly_report(now=now)

    assert "Какого продукта больше" in text
    assert "разметкой продукта пока нет" in text
    # Остальные секции не тронуты
    assert "подтверждений не было" in text
    assert "мёртвых комбо пока нет" in text


def test_build_weekly_report_products_section_null_ads_excluded(kb):
    """Объявление с target_product=NULL (ещё не бэкфилленное, «без разметки»)
    не попадает ни в один продукт и не искажает долю PRODA: 1 PRODA из 1
    размеченного ACTIVE-объявления → 100%, NULL-объявление не в знаменателе."""
    now = datetime(2026, 7, 5, 20, 0, 0)

    _insert_creative_kb_row(kb, "ad-null", "Без разметки", target_product=None, spend=999.0)
    _insert_creative_kb_row(kb, "ad-proda", "CityA | тема", target_product="PRODA", spend=100.0)

    with patch("services.pattern_engine.get_patterns_summary", return_value={
        "predictors": [], "data_sufficiency": "insufficient", "n_success": 0, "n_fail": 0,
    }):
        text = build_weekly_report(now=now)

    assert "Какого продукта больше" in text
    assert "PRODA 100%" in text


def test_build_weekly_report_products_section_failure_does_not_crash_report(kb):
    """product_report.build_product_breakdown упал → секция подставляет
    заглушку об ошибке, остальной отчёт строится нормально (каждая секция
    в своём try, как и остальные пять)."""
    now = datetime(2026, 7, 5, 20, 0, 0)

    with patch("services.pattern_engine.get_patterns_summary", return_value={
        "predictors": [], "data_sufficiency": "insufficient", "n_success": 0, "n_fail": 0,
    }), patch("services.product_report.build_product_breakdown", side_effect=RuntimeError("KB недоступна")):
        text = build_weekly_report(now=now)

    assert isinstance(text, str)
    assert "Чему я научился" in text
    assert "подтверждений не было" in text
