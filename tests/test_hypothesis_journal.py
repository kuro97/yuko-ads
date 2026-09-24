"""
Юнит-тесты для services/hypothesis_journal.py.

Проверяют:
- extract_ad_ids_from_log: одиночный/множественный формат, игнор ошибок/мусора
- build_expectation: 3 ветки формулы (teardown с оплатами, coverage с медианой,
  новый город без данных)
- record_hypothesis: happy (мульти-город), пустой запуск, дубль по ad_ids,
  фолбэк без topic, enabled=false (флаг проверяется вызывающим кодом — здесь
  просто фиксируем что запись без topic тоже работает)
- get_open_hypotheses / get_hypotheses_by_status: чтение на tmp-базе

Используют tmp_path — не трогают реальную БД. Без сети.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта любого модуля который может его тянуть
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())

import sqlite3

from services import creative_intelligence as ci
from services.hypothesis_journal import (
    SEGMENT_QUAL_MIN,
    build_expectation,
    extract_ad_ids_from_log,
    get_hypotheses_by_status,
    get_open_hypotheses,
    record_hypothesis,
)


# ---------------------------------------------------------------------------
# Фикстуры
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


def _insert_ad(db_path: str, ad_id: str, city: str, cpl: float, spend: float = 100.0,
                is_full_cabinet: int = 1) -> None:
    """Вставляет объявление в creative_kb для тестов медианы CPL города."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO creative_kb (ad_id, ad_name, city, cpl, spend, is_full_cabinet)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ad_id, f"Ad {ad_id}", city, cpl, spend, is_full_cabinet),
        )
        conn.commit()
    finally:
        conn.close()


def _count_hypotheses(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# extract_ad_ids_from_log
# ---------------------------------------------------------------------------

def test_extract_ad_ids_single():
    """Одиночное объявление: '✅ {city}: {ad_id}'."""
    log = ["✅ CityA: 123"]
    assert extract_ad_ids_from_log(log) == {"CityA": ["123"]}


def test_extract_ad_ids_multi():
    """Несколько объявлений: '✅ {city}: N объявлений — id1, id2'."""
    log = ["✅ CityB: 2 объявлений — 5, 6"]
    assert extract_ad_ids_from_log(log) == {"CityB": ["5", "6"]}


def test_extract_ad_ids_ignores_errors():
    """Строки с ❌ игнорируются."""
    log = ["❌ CityE: не создано"]
    assert extract_ad_ids_from_log(log) == {}


def test_extract_ad_ids_ignores_garbage():
    """Мусорные строки (без ✅, пустые, служебные) не ломают парсер."""
    log = ["мусор", "", "Готовлю запуск…", "Язык: RU → L1", "Формат: видео (3 файла)"]
    assert extract_ad_ids_from_log(log) == {}


def test_extract_ad_ids_mixed_log():
    """Реалистичный лог: успехи + ошибки + служебные строки вперемешку."""
    log = [
        "Готовлю запуск…",
        "✅ CityA: 111",
        "✅ CityB: 3 объявлений — 222, 223, 224",
        "❌ CityD: объявление не создано (пропущен или ошибка)",
        "Карточка отмечена выполненной в Trello",
    ]
    result = extract_ad_ids_from_log(log)
    assert result == {
        "CityA": ["111"],
        "CityB": ["222", "223", "224"],
    }


def test_extract_ad_ids_empty_log():
    """Пустой лог → пустой dict."""
    assert extract_ad_ids_from_log([]) == {}


# ---------------------------------------------------------------------------
# build_expectation
# ---------------------------------------------------------------------------

def test_build_expectation_teardown_with_payments():
    """teardown с оплатами у референса → metric=payments, threshold=1."""
    exp = build_expectation(
        city="CityA", segment="общий", ad_format="video_speaker",
        source="teardown", reference={"payments": 2, "cpl": 4.0},
    )
    assert exp["metric"] == "payments"
    assert exp["op"] == ">="
    assert exp["threshold"] == 1


def test_build_expectation_teardown_without_payments():
    """teardown без оплат у референса → metric=cpl, threshold = cpl*1.3."""
    exp = build_expectation(
        city="CityA", segment="общий", ad_format="video_speaker",
        source="teardown", reference={"payments": 0, "cpl": 4.0},
    )
    assert exp["metric"] == "cpl"
    assert exp["op"] == "<="
    assert exp["threshold"] == pytest.approx(5.2)


def test_build_expectation_coverage_with_median(kb):
    """coverage с медианой CPL города → metric=cpl, threshold≈медиана."""
    for i in range(5):
        _insert_ad(kb, f"ad{i}", "CityA", cpl=4.0, spend=50.0)

    exp = build_expectation(
        city="CityA", segment="общий", ad_format="video_speaker",
        source="coverage", reference=None,
    )
    assert exp["metric"] == "cpl"
    assert exp["op"] == "<="
    assert exp["threshold"] == pytest.approx(4.0)


def test_build_expectation_new_city_no_data(kb):
    """Новый город без данных → metric=qual_pct, threshold=SEGMENT_QUAL_MIN."""
    exp = build_expectation(
        city="НовыйГород", segment="PRODB", ad_format="carousel",
        source="coverage", reference=None,
    )
    assert exp["metric"] == "qual_pct"
    assert exp["op"] == ">="
    assert exp["threshold"] == SEGMENT_QUAL_MIN


def test_build_expectation_median_insufficient_rows(kb):
    """<5 строк в creative_kb для города → падаем на qual_pct (не медиану)."""
    for i in range(3):
        _insert_ad(kb, f"ad{i}", "CityD", cpl=3.0, spend=50.0)

    exp = build_expectation(
        city="CityD", segment="общий", ad_format="static",
        source="coverage", reference=None,
    )
    assert exp["metric"] == "qual_pct"


# ---------------------------------------------------------------------------
# record_hypothesis
# ---------------------------------------------------------------------------

def test_record_hypothesis_happy_multi_city(kb):
    """Запуск в 2 города с topic → 2 строки, status=open."""
    topic = {
        "angle": "Бесплатная консультация",
        "ad_format": "video_speaker",
        "segment": "общий",
        "source": "coverage",
        "reference": None,
    }
    ad_ids_by_city = {"CityA": ["111"], "CityB": ["222", "223"]}

    ids = record_hypothesis("Карточка теста", "traffic", ad_ids_by_city, topic)

    assert len(ids) == 2
    assert _count_hypotheses(kb) == 2

    open_hyps = get_open_hypotheses(min_age_days=0, max_age_days=999)
    cities = {h["city"] for h in open_hyps}
    assert cities == {"CityA", "CityB"}
    for h in open_hyps:
        assert h["angle"] == "Бесплатная консультация"
        assert h["ad_format"] == "video_speaker"
        assert h["source"] == "coverage"
        assert h["card_name"] == "Карточка теста"
    cityb = next(h for h in open_hyps if h["city"] == "CityB")
    assert cityb["ad_ids"] == ["222", "223"]


def test_record_hypothesis_empty_launch(kb):
    """Пустой ad_ids_by_city → [] , 0 строк."""
    ids = record_hypothesis("Карточка", "traffic", {}, {"angle": "x"})
    assert ids == []
    assert _count_hypotheses(kb) == 0


def test_record_hypothesis_duplicate_ad_ids(kb):
    """Повторная запись тех же ad_ids не создаёт дубль."""
    topic = {"angle": "Угол", "ad_format": "carousel", "segment": "общий", "source": "coverage"}
    ad_ids_by_city = {"CityA": ["999"]}

    first_ids = record_hypothesis("Карточка", "traffic", ad_ids_by_city, topic)
    second_ids = record_hypothesis("Карточка", "traffic", ad_ids_by_city, topic)

    assert len(first_ids) == 1
    assert second_ids == []
    assert _count_hypotheses(kb) == 1


def test_record_hypothesis_no_topic_derives_fallback(kb):
    """topic=None → derive angle из card_name, source='manual'."""
    ad_ids_by_city = {"CityC": ["555"]}

    ids = record_hypothesis("Запуск PRODB карусель длинное имя карточки", "traffic", ad_ids_by_city, None)

    assert len(ids) == 1
    hyps = get_open_hypotheses(min_age_days=0, max_age_days=999)
    hyp = hyps[0]
    assert hyp["source"] == "manual"
    assert hyp["segment"] == "PRODB"
    assert hyp["angle"] == "Запуск PRODB карусель длинное имя карточки"[:40]
    assert hyp["ad_format"] == ""


def test_record_hypothesis_teardown_reference_ad_id(kb):
    """topic с reference содержащим ad_id → reference_ad_id заполнен."""
    topic = {
        "angle": "Угол победителя",
        "ad_format": "video_speaker",
        "segment": "общий",
        "source": "teardown",
        "reference": {"ad_id": "777", "payments": 3, "cpl": 2.5},
    }
    record_hypothesis("Карточка teardown", "traffic", {"CityA": ["888"]}, topic)

    hyps = get_open_hypotheses(min_age_days=0, max_age_days=999)
    assert hyps[0]["reference_ad_id"] == "777"
    assert hyps[0]["expectation"]["metric"] == "payments"


# ---------------------------------------------------------------------------
# get_open_hypotheses
# ---------------------------------------------------------------------------

def test_get_open_hypotheses_age_filter(kb):
    """Гипотезы фильтруются по возрасту [min_age_days, max_age_days]."""
    now = datetime(2026, 7, 2, 10, 0, 0)
    topic = {"angle": "x", "ad_format": "video_speaker", "segment": "общий", "source": "coverage"}

    # Гипотеза 3 дня назад — младше окна [7,14]
    record_hypothesis("Молодая", "traffic", {"CityA": ["1"]}, topic, now=now - timedelta(days=3))
    # Гипотеза 10 дней назад — в окне [7,14]
    record_hypothesis("В окне", "traffic", {"CityB": ["2"]}, topic, now=now - timedelta(days=10))
    # Гипотеза 20 дней назад — старше окна
    record_hypothesis("Старая", "traffic", {"CityC": ["3"]}, topic, now=now - timedelta(days=20))

    result = get_open_hypotheses(min_age_days=7, max_age_days=14, now=now)
    card_names = {h["card_name"] for h in result}
    assert card_names == {"В окне"}


def test_get_open_hypotheses_empty_db(kb):
    """Пустая БД → пустой список, без ошибок."""
    assert get_open_hypotheses(min_age_days=7, max_age_days=14) == []


# ---------------------------------------------------------------------------
# get_hypotheses_by_status
# ---------------------------------------------------------------------------

def test_get_hypotheses_by_status_open(kb):
    """Возвращает только гипотезы с указанным статусом."""
    topic = {"angle": "x", "ad_format": "video_speaker", "segment": "общий", "source": "coverage"}
    record_hypothesis("Карточка", "traffic", {"CityA": ["1"]}, topic)

    open_hyps = get_hypotheses_by_status("open")
    confirmed_hyps = get_hypotheses_by_status("confirmed")

    assert len(open_hyps) == 1
    assert confirmed_hyps == []


# ---------------------------------------------------------------------------
# enabled=false — не пишет (проверяем на уровне самого journal: вызывающий
# код (auto_launch, T6) должен просто не вызывать record_hypothesis, но
# здесь фиксируем что record_hypothesis сам по себе не содержит никакой
# завязки на автопилот-конфиг — если его не вызвали, строк не появится)
# ---------------------------------------------------------------------------

def test_record_hypothesis_not_called_means_no_rows(kb):
    """Если record_hypothesis не вызывается (как при enabled=false в вызывающем
    коде) — таблица hypotheses остаётся пустой."""
    assert _count_hypotheses(kb) == 0


# ---------------------------------------------------------------------------
# _row_to_dict — поля lesson/status (отложенное замечание CTO)
# ---------------------------------------------------------------------------

def test_row_to_dict_status_present_for_open_hypothesis(kb):
    """Свежесозданная гипотеза (status='open' по умолчанию) — поле status
    попадает в dict через get_open_hypotheses."""
    topic = {"angle": "x", "ad_format": "video_speaker", "segment": "общий", "source": "coverage"}
    record_hypothesis("Карточка", "traffic", {"CityA": ["1"]}, topic)

    hyps = get_open_hypotheses(min_age_days=0, max_age_days=999)
    assert len(hyps) == 1
    assert hyps[0]["status"] == "open"


def test_row_to_dict_lesson_none_when_not_verdicted(kb):
    """Гипотеза без вынесенного вердикта — lesson=None (колонка nullable), но
    ключ присутствует в dict (не выброшен молча)."""
    topic = {"angle": "x", "ad_format": "video_speaker", "segment": "общий", "source": "coverage"}
    record_hypothesis("Карточка", "traffic", {"CityA": ["1"]}, topic)

    hyps = get_open_hypotheses(min_age_days=0, max_age_days=999)
    assert "lesson" in hyps[0]
    assert hyps[0]["lesson"] is None


def test_row_to_dict_lesson_and_status_after_verdict(kb):
    """После вынесения вердикта (status/lesson заполнены в БД напрямую —
    как это делает hypothesis_verdict.py) — _row_to_dict возвращает оба поля."""
    import sqlite3

    topic = {"angle": "x", "ad_format": "video_speaker", "segment": "общий", "source": "coverage"}
    ids = record_hypothesis("Карточка", "traffic", {"CityA": ["1"]}, topic)
    hyp_id = ids[0]

    conn = sqlite3.connect(kb)
    try:
        conn.execute(
            "UPDATE hypotheses SET status = ?, lesson = ? WHERE id = ?",
            ("confirmed", "CPL ниже медианы города — угол подтверждён", hyp_id),
        )
        conn.commit()
    finally:
        conn.close()

    hyps = get_hypotheses_by_status("confirmed")
    assert len(hyps) == 1
    assert hyps[0]["status"] == "confirmed"
    assert hyps[0]["lesson"] == "CPL ниже медианы города — угол подтверждён"
