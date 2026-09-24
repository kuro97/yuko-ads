"""
Тесты T2 + T3.

T2 — двухступенчатый бэкфилл creative_kb.target_product
(services/creative_intelligence.backfill_target_product).
Этап 1 — keyword-эвристика (classify_product) по ad_name+ad_body, для ВСЕХ
NULL-строк (включая архив).
Этап 2 — LLM-добор (product_tags._llm_classify_product) ТОЛЬКО для строк, где
keyword дал "ОБЩАЯ" (эвристика ничего не поймала) И объявление входит в
"живую" когорту (status ACTIVE/PAUSED или effective_status ACTIVE) —
архив/DELETED LLM НЕ добирает (боевой прогон вскрыл десятки тысяч NULL-строк
вместо ожидаемых сотен — почти всё оказалось мёртвым историческим архивом, LLM по
нему был бы дорогим и бессмысленным, см. правку после первого ревью).
LLM мокается — реальная сеть не трогается (см. tests/conftest.py::_no_real_network).

T3 — отчёт «какого продукта больше» (services/product_report.py):
build_product_breakdown читает creative_kb.target_product (НЕ парсит имена),
format_product_report собирает HTML для Telegram.

Используют tmp_path — не трогают реальную БД data/decisions.db.
См. docs/specs/ARCH-product-tags.md §5, §6, §9.
"""

import sqlite3
from unittest.mock import patch

import pytest

from services import creative_intelligence as ci
from services import product_report


# ---------------------------------------------------------------------------
# Фикстуры (по образцу tests/test_backfill_business_class.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_kb_path():
    """Сбрасываем глобальный DB_PATH перед и после каждого теста."""
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


@pytest.fixture
def kb(tmp_path):
    """Инициализирует пустую KB (полная схема через миграции) во временной директории."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)
    return db_path


def _insert_row(db_path: str, ad_id: str, ad_name: str, ad_body: str = "",
                 target_product: str | None = None, status: str = "ACTIVE",
                 effective_status: str = "", spend: float = 100.0) -> None:
    """Вставляет строку creative_kb напрямую (без бизнес-логики синка)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO creative_kb (ad_id, ad_name, ad_body, target_product, status, effective_status, spend)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ad_id, ad_name, ad_body, target_product, status, effective_status, spend),
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_row(db_path: str, ad_id: str) -> dict | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM creative_kb WHERE ad_id = ?", (ad_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Этап 2: LLM-добор для «эмоциональных» PRODA-креативов без ключевых слов
# ---------------------------------------------------------------------------


def test_backfill_llm_добор_для_критики_рынка(kb):
    """«Критика рынка» на ЖИВОМ объявлении (status=ACTIVE) — keyword-этап не
    находит ни одного совпадения → ОБЩАЯ, объявление живое → зовётся
    _llm_classify_product (мокнут → «PRODA»). Итог: target_product = «PRODA»,
    НЕ «ОБЩАЯ». LLM вызван ровно 1 раз, llm_scope_rows учитывает эту строку."""
    _insert_row(kb, "ad-1", "CityB | Критика рынка / История Олега", target_product=None, status="ACTIVE")

    with patch("services.product_tags._llm_classify_product", return_value="PRODA") as mock_llm:
        result = ci.backfill_target_product()

    mock_llm.assert_called_once()
    row = _fetch_row(kb, "ad-1")
    assert row["target_product"] == "PRODA"
    assert result["total"] == 1
    assert result["by_llm"] == 1
    assert result["by_keyword"] == 0
    assert result["llm_scope_rows"] == 1
    assert result["by_product"]["PRODA"] == 1


def test_backfill_архивная_строка_с_эмоциональным_текстом_llm_не_вызван(kb):
    """«Критика рынка» на АРХИВНОМ объявлении (status=DELETED, effective_status
    пуст/не ACTIVE) — keyword-этап тоже даёт ОБЩАЯ, но объявление НЕ входит
    в живую когорту → LLM НЕ зовётся (боевой инцидент: десятки тысяч NULL-строк на
    проде — почти всё архив, LLM по нему звать нельзя). target_product остаётся
    "ОБЩАЯ" (результат keyword-этапа), llm_scope_rows=0."""
    _insert_row(
        kb, "ad-archived", "CityB | Критика рынка / История Олега",
        target_product=None, status="DELETED", effective_status="",
    )

    with patch("services.product_tags._llm_classify_product") as mock_llm:
        result = ci.backfill_target_product()

    mock_llm.assert_not_called()
    row = _fetch_row(kb, "ad-archived")
    assert row["target_product"] == "ОБЩАЯ"
    assert result["by_llm"] == 0
    assert result["llm_scope_rows"] == 0


def test_backfill_живая_и_архивная_строки_вместе(kb):
    """Смешанный набор: живая (status=ACTIVE) с эмоциональным текстом → LLM
    вызывается; архивная (status=DELETED, effective_status='ARCHIVED') с тем
    же текстом → LLM НЕ вызывается, остаётся ОБЩАЯ. llm_scope_rows считает
    только живую когорту среди NULL-кандидатов (=1), by_llm тоже 1."""
    _insert_row(
        kb, "ad-live", "Критика рынка", target_product=None,
        status="ACTIVE", effective_status="ACTIVE",
    )
    _insert_row(
        kb, "ad-dead", "Критика рынка", target_product=None,
        status="DELETED", effective_status="ARCHIVED",
    )

    with patch("services.product_tags._llm_classify_product", return_value="PRODA") as mock_llm:
        result = ci.backfill_target_product()

    mock_llm.assert_called_once()
    assert result["total"] == 2
    assert result["llm_scope_rows"] == 1
    assert result["by_llm"] == 1
    assert _fetch_row(kb, "ad-live")["target_product"] == "PRODA"
    assert _fetch_row(kb, "ad-dead")["target_product"] == "ОБЩАЯ"


def test_backfill_keyword_поймал_llm_не_вызван(kb):
    """«пакет 6» — keyword-этап ловит PRODB сразу, LLM НЕ зовётся."""
    _insert_row(kb, "ad-2", "CityA | Тема А / пакет 6 | анимация", target_product="")

    with patch("services.product_tags._llm_classify_product") as mock_llm:
        result = ci.backfill_target_product()

    mock_llm.assert_not_called()
    row = _fetch_row(kb, "ad-2")
    assert row["target_product"] == "PRODB"
    assert result["by_keyword"] == 1
    assert result["by_llm"] == 0


# ---------------------------------------------------------------------------
# Идемпотентность
# ---------------------------------------------------------------------------


def test_backfill_идемпотентен_повторный_прогон_0_строк(kb):
    """После первого прогона NULL-строк не остаётся — повторный вызов находит
    0 строк, LLM больше не зовётся."""
    _insert_row(kb, "ad-3", "Критика рынка", target_product=None)
    _insert_row(kb, "ad-4", "пакет 6", target_product=None)

    with patch("services.product_tags._llm_classify_product", return_value="PRODA"):
        first = ci.backfill_target_product()

    assert first["total"] == 2

    with patch("services.product_tags._llm_classify_product") as mock_llm_second:
        second = ci.backfill_target_product()

    assert second["total"] == 0
    assert second["by_keyword"] == 0
    assert second["by_llm"] == 0
    assert second["llm_scope_rows"] == 0
    assert all(count == 0 for count in second["by_product"].values())
    mock_llm_second.assert_not_called()


def test_backfill_stale_status_но_effective_status_active_зовёт_llm(kb):
    """status в KB пуст/устарел, но effective_status='ACTIVE' (свежий FB-статус)
    — объявление всё равно считается живой когортой, LLM зовётся."""
    _insert_row(
        kb, "ad-stale", "Критика рынка", target_product=None,
        status="", effective_status="ACTIVE",
    )

    with patch("services.product_tags._llm_classify_product", return_value="PRODA") as mock_llm:
        result = ci.backfill_target_product()

    mock_llm.assert_called_once()
    assert result["llm_scope_rows"] == 1
    assert _fetch_row(kb, "ad-stale")["target_product"] == "PRODA"


# ---------------------------------------------------------------------------
# Заполняет все NULL, значения — только канон, ничего лишнего не затирает
# ---------------------------------------------------------------------------


def test_backfill_заполняет_все_null_значениями_из_канона(kb):
    """Интеграционный сценарий: несколько строк с разными путями классификации.
    После бэкфилла — 0 строк с NULL/пустым target_product, все значения из
    множества канонов продуктов."""
    _insert_row(kb, "ad-5", "Критика рынка", target_product=None)
    _insert_row(kb, "ad-6", "CityA | Тема А / пакет 6", target_product=None)
    _insert_row(kb, "ad-7", "Стартовый пакет для новичков", target_product="")
    _insert_row(kb, "ad-8", "просто консультация", target_product=None)

    def _fake_llm(name, body=""):
        # Реалистичный мок: «Критика рынка» — эмоциональный PRODA-заход, остальное — ОБЩАЯ
        return "PRODA" if "критика рынка" in name.lower() else "ОБЩАЯ"

    with patch("services.product_tags._llm_classify_product", side_effect=_fake_llm):
        result = ci.backfill_target_product()

    assert result["total"] == 4

    conn = sqlite3.connect(kb)
    try:
        null_count = conn.execute(
            "SELECT COUNT(*) FROM creative_kb WHERE target_product IS NULL OR target_product = ''"
        ).fetchone()[0]
        values = [r[0] for r in conn.execute("SELECT target_product FROM creative_kb").fetchall()]
    finally:
        conn.close()

    assert null_count == 0
    valid_products = {"PRODA", "PRODB", "СТАРТ", "ОБЩАЯ"}
    assert all(v in valid_products for v in values)
    # keyword-этап должен корректно классифицировать очевидные случаи без LLM
    assert _fetch_row(kb, "ad-6")["target_product"] == "PRODB"
    assert _fetch_row(kb, "ad-7")["target_product"] == "СТАРТ"
    # ad-5 и ad-8 оба ушли на LLM-добор (keyword дал ОБЩАЯ) — мок различает их по тексту
    assert _fetch_row(kb, "ad-5")["target_product"] == "PRODA"
    assert _fetch_row(kb, "ad-8")["target_product"] == "ОБЩАЯ"


def test_backfill_не_затирает_другие_колонки(kb):
    """UPDATE трогает только target_product — spend/status остаются как были."""
    _insert_row(kb, "ad-9", "Критика рынка", target_product=None, status="PAUSED", spend=555.5)

    with patch("services.product_tags._llm_classify_product", return_value="PRODA"):
        ci.backfill_target_product()

    row = _fetch_row(kb, "ad-9")
    assert row["target_product"] == "PRODA"
    assert row["status"] == "PAUSED"
    assert row["spend"] == 555.5


# ---------------------------------------------------------------------------
# NULL-safe: ad_body может быть None
# ---------------------------------------------------------------------------


def test_backfill_null_safe_ad_body_none(kb):
    """ad_body=NULL в БД не роняет бэкфилл (classify_product получает "" вместо None)."""
    _insert_row(kb, "ad-10", "пакет 6", ad_body=None, target_product=None)

    with patch("services.product_tags._llm_classify_product") as mock_llm:
        result = ci.backfill_target_product()

    assert result["total"] == 1
    assert _fetch_row(kb, "ad-10")["target_product"] == "PRODB"
    mock_llm.assert_not_called()


def test_backfill_пустая_база_без_null_строк(kb):
    """Нет строк с NULL/пустым target_product — backfill_target_product() no-op,
    LLM не вызывается, total=0."""
    _insert_row(kb, "ad-11", "CityB | тема", target_product="PRODB")

    with patch("services.product_tags._llm_classify_product") as mock_llm:
        result = ci.backfill_target_product()

    assert result["total"] == 0
    mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# Миграция 014: идемпотентная нормализация старых кодов (санитайзер)
# ---------------------------------------------------------------------------


def test_migration_014_нормализует_старые_коды_идемпотентно(kb):
    """Строка со старым кодом target_product='PRODA' (миграция 007) после init_kb
    (в котором подключён _apply_target_product_norm_migration) нормализуется в
    канон 'PRODA'. Повторный init_kb не бросает и не меняет уже-канон значение."""
    _insert_row(kb, "ad-12", "Старое имя", target_product="PRODA")

    conn = sqlite3.connect(kb)
    try:
        conn.execute("UPDATE creative_kb SET target_product = 'PRODA' WHERE ad_id = 'ad-12'")
        conn.commit()
        ci._apply_target_product_norm_migration(conn)
    finally:
        conn.close()

    assert _fetch_row(kb, "ad-12")["target_product"] == "PRODA"

    # Повторный запуск — идемпотентно, без ошибок, значение не меняется
    conn = sqlite3.connect(kb)
    try:
        ci._apply_target_product_norm_migration(conn)
    finally:
        conn.close()

    assert _fetch_row(kb, "ad-12")["target_product"] == "PRODA"


def test_init_kb_повторный_вызов_не_бросает(kb):
    """init_kb (со всеми миграциями, включая 014) безопасен для повторного
    вызова на той же БД — не бросает исключений."""
    ci.init_kb(kb)  # второй вызов на уже проинициализированной БД


# ---------------------------------------------------------------------------
# T3: product_report.build_product_breakdown / format_product_report
# ---------------------------------------------------------------------------


def test_breakdown_все_4_продукта_даже_с_нулями(kb):
    """2 PRODA ACTIVE + 1 PRODB ACTIVE → в выдаче всегда все 4 продукта,
    СТАРТ и ОБЩАЯ с нулями, share_pct суммарно ≈100."""
    _insert_row(kb, "ad-1", "CityA | тема", target_product="PRODA", spend=100.0)
    _insert_row(kb, "ad-2", "CityB | тема", target_product="PRODA", spend=50.0)
    _insert_row(kb, "ad-3", "CityC | тема", target_product="PRODB", spend=30.0)

    rows = product_report.build_product_breakdown()

    assert len(rows) == 4
    by_product = {row["product"]: row for row in rows}
    assert set(by_product.keys()) == {"PRODA", "PRODB", "СТАРТ", "ОБЩАЯ"}
    assert by_product["PRODA"]["ads_count"] == 2
    assert by_product["PRODA"]["spend"] == 150.0
    assert by_product["СТАРТ"]["ads_count"] == 0
    assert by_product["СТАРТ"]["spend"] == 0.0

    total_share = sum(row["share_pct"] for row in rows)
    assert 99.0 <= total_share <= 101.0  # округление до 1 знака


def test_breakdown_нет_активных_все_нули(kb):
    """Пустая KB (или без ACTIVE) → все 4 продукта с нулями, share_pct=0."""
    _insert_row(kb, "ad-1", "CityA | тема", target_product="PRODA", status="PAUSED", spend=100.0)

    rows = product_report.build_product_breakdown()

    assert len(rows) == 4
    for row in rows:
        assert row["ads_count"] == 0
        assert row["spend"] == 0.0
        assert row["share_pct"] == 0.0


def test_breakdown_сортировка_по_ads_count_desc(kb):
    """1 PRODA ACTIVE, 3 PRODB ACTIVE → первый элемент PRODB (больше штук)."""
    _insert_row(kb, "ad-1", "CityA | тема", target_product="PRODA", spend=10.0)
    _insert_row(kb, "ad-2", "CityB | тема", target_product="PRODB", spend=10.0)
    _insert_row(kb, "ad-3", "CityD | тема", target_product="PRODB", spend=10.0)
    _insert_row(kb, "ad-4", "CityE | тема", target_product="PRODB", spend=10.0)

    rows = product_report.build_product_breakdown()

    assert rows[0]["product"] == "PRODB"
    assert rows[0]["ads_count"] == 3


def test_breakdown_null_target_product_без_разметки_исключён(kb):
    """Объявление с target_product=NULL (ещё не бэкфилленное) НЕ попадает ни в
    один из 4 продуктов и не искажает долю: знаменатель считается только по
    уже размеченным ACTIVE-объявлениям."""
    _insert_row(kb, "ad-1", "CityA | тема", target_product="PRODA", spend=100.0)
    _insert_row(kb, "ad-2", "Без разметки", target_product=None, spend=999.0)

    rows = product_report.build_product_breakdown()

    assert len(rows) == 4  # без 5-го "неизвестного" продукта
    by_product = {row["product"]: row for row in rows}
    assert by_product["PRODA"]["ads_count"] == 1
    assert by_product["PRODA"]["share_pct"] == 100.0  # NULL-строка не в знаменателе
    total_ads = sum(row["ads_count"] for row in rows)
    assert total_ads == 1  # NULL-объявление не посчитано нигде


def test_format_product_report_пустой_список_не_падает():
    """format_product_report на нулевой разбивке (все 4 продукта с ads_count=0)
    не бросает и сообщает, что данных нет."""
    rows = [
        {"product": "PRODA", "ads_count": 0, "spend": 0.0, "share_pct": 0.0},
        {"product": "PRODB", "ads_count": 0, "spend": 0.0, "share_pct": 0.0},
        {"product": "СТАРТ", "ads_count": 0, "spend": 0.0, "share_pct": 0.0},
        {"product": "ОБЩАЯ", "ads_count": 0, "spend": 0.0, "share_pct": 0.0},
    ]

    text = product_report.format_product_report(rows)

    assert isinstance(text, str)
    assert "разметкой продукта пока нет" in text


def test_format_product_report_с_данными_содержит_медали():
    """format_product_report с непустыми данными содержит топ-медаль и итог."""
    rows = [
        {"product": "PRODB", "ads_count": 28, "spend": 412.0, "share_pct": 42.0},
        {"product": "PRODA", "ads_count": 22, "spend": 301.0, "share_pct": 33.0},
        {"product": "ОБЩАЯ", "ads_count": 12, "spend": 88.0, "share_pct": 18.0},
        {"product": "СТАРТ", "ads_count": 5, "spend": 30.0, "share_pct": 7.0},
    ]

    text = product_report.format_product_report(rows)

    assert "🥇" in text
    assert "PRODB" in text
    assert "Всего активных: 67" in text
    assert len(text) <= 4096
