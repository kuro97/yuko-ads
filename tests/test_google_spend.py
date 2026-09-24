"""
Тесты services/google_spend.py.

ВАЖНО (смена навигации): раньше снимок читался CSV-экспортом по фиксированному
gid=0, но выгрузка создаёт НОВУЮ ВКЛАДКУ НА КАЖДЫЙ ДЕНЬ — gid новых
вкладок заранее неизвестен, бот годами читал одну и ту же старую вкладку.
Теперь читаем ВЕСЬ файл целиком через xlsx-экспорт (openpyxl) и ищем нужную
вкладку по имени «DD.MM», с фолбэком на скан A1-периода всех вкладок.
Поэтому тесты read_daily_google_spend/capture_daily_google_spend обновлены —
мокают xlsx-ответ (или read_daily_google_spend напрямую) вместо CSV.
Бизнес-поведение (парсинг «Итого», upsert истории, недельная сумма) не менялось.

Все внешние зависимости мокируются:
- requests.get (Google Sheets xlsx export)
- _load_spend_history / _save_spend_history (файловая история)
"""

import sys
from datetime import date
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from openpyxl import Workbook, load_workbook

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Вспомогательные
# ---------------------------------------------------------------------------

def _sample_tab_rows(
    date_line: str = "28 июня 2026 г. - 28 июня 2026 г.",
    spend_val: float | str = 189.42,
) -> list[list]:
    """Строки одной вкладки в формате выгрузки Google Ads (как в xlsx-экспорте)."""
    return [
        [date_line],
        ["Статус кампании", "Кампания", "Тип", "Показы", "Клики", "Ср. цена",
         "CTR", "Расходы", "CPM", "Конв."],
        ["Активна", "CityA Ретаргет", "Поиск", 1000, 50, 1.5, 0.05, 89.21, 15.3, 3],
        ["Активна", "CityB Lead", "Видео", 2000, 80, 1.1, 0.04, 100.21, 10.2, 2],
        ["Итого (Кампании)", "", "", 3000, 130, "", "", spend_val, "", 5],
    ]


def _shifted_tab_rows(
    date_line: str = "12 июля 2026 г. - 12 июля 2026 г.",
    spend_val: float = 104.05,
    ctr_val: float = 0.1868,
) -> list[list]:
    """Вкладка со ВСТАВЛЕННОЙ подрядчиком колонкой «Бюджет».

    Колонка «Бюджет» добавлена перед CTR → «Расходы» съезжают с индекса 7 на 8,
    а на историческом индексе 7 теперь оказывается CTR. Старый код по индексу 7
    записал бы вместо расхода ($104.05) значение CTR ($0.1868) — ровно тот баг.
    """
    return [
        [date_line],
        # 0 Статус, 1 Кампания, 2 Тип, 3 Показы, 4 Клики, 5 Ср.цена,
        # 6 Бюджет(вставлена), 7 CTR, 8 Расходы, 9 CPM, 10 Конв.
        ["Статус кампании", "Кампания", "Тип", "Показы", "Клики", "Ср. цена",
         "Бюджет", "CTR", "Расходы", "CPM", "Конв."],
        ["Активна", "CityA Ретаргет", "Поиск", 1000, 50, 1.5, 200, ctr_val, 45.0, 15.3, 3],
        ["Активна", "CityB Lead", "Видео", 2000, 80, 1.1, 300, ctr_val, 59.05, 10.2, 2],
        ["Итого (Кампании)", "", "", 3000, 130, "", "", ctr_val, spend_val, "", 5],
    ]


def _no_spend_header_tab_rows(
    date_line: str = "12 июля 2026 г. - 12 июля 2026 г.",
    val_at_idx7: float = 88.0,
) -> list[list]:
    """Вкладка БЕЗ распознаваемого заголовка «Расходы» (тут «Затраты»).

    Проверяем фолбэк на исторический индекс 7 с warning: колонку по заголовку
    не найти, поэтому читаем позицию 7 строки «Итого».
    """
    return [
        [date_line],
        # индекс 7 = «Затраты» (не «Расходы») — по заголовку не распознаётся
        ["Статус", "Кампания", "Тип", "Показы", "Клики", "Ср. цена",
         "CTR", "Затраты", "CPM", "Конв."],
        ["Активна", "CityA", "Поиск", 1000, 50, 1.5, 0.05, 88.0, 15.3, 3],
        ["Итого (Кампании)", "", "", 1000, 50, "", "", val_at_idx7, "", 3],
    ]


def _build_workbook_bytes(sheets: dict[str, list[list]]) -> bytes:
    """Строит xlsx-файл в памяти с заданными вкладками (имя → список строк)."""
    wb = Workbook()
    wb.remove(wb.active)  # убираем дефолтный пустой "Sheet"
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_xlsx_resp(sheets: dict[str, list[list]], status: int = 200) -> MagicMock:
    """Фейковый HTTP-ответ с xlsx-содержимым (или пустой при ошибке статуса)."""
    resp = MagicMock()
    resp.status_code = status
    resp.content = _build_workbook_bytes(sheets) if status == 200 and sheets else b""
    return resp


# ===========================================================================
# Тест 1: _parse_ru_date — корректно разбирает русские даты
# ===========================================================================

class TestParseRuDate:
    def test_june(self):
        from services.google_spend import _parse_ru_date
        assert _parse_ru_date("28 июня 2026 г.") == "2026-06-28"

    def test_july(self):
        from services.google_spend import _parse_ru_date
        assert _parse_ru_date("1 июля 2026 г.") == "2026-07-01"

    def test_range_takes_first_date(self):
        from services.google_spend import _parse_ru_date
        # Период «от - до» — берём левую дату
        result = _parse_ru_date("28 июня 2026 г. - 28 июня 2026 г.")
        assert result == "2026-06-28"

    def test_december(self):
        from services.google_spend import _parse_ru_date
        assert _parse_ru_date("31 декабря 2026 г.") == "2026-12-31"

    def test_invalid_returns_none(self):
        from services.google_spend import _parse_ru_date
        assert _parse_ru_date("not a date") is None

    def test_unknown_month_returns_none(self):
        from services.google_spend import _parse_ru_date
        assert _parse_ru_date("10 октября 2026 г.") == "2026-10-10"

    def test_january(self):
        from services.google_spend import _parse_ru_date
        assert _parse_ru_date("5 января 2027 г.") == "2027-01-05"


# ===========================================================================
# Тест 2: _parse_spend_value — запятая-десятичная и пробел-тысячный
# ===========================================================================

class TestParseSpendValue:
    def test_comma_decimal(self):
        from services.google_spend import _parse_spend_value
        assert _parse_spend_value("189,42") == pytest.approx(189.42)

    def test_thousands_separator(self):
        from services.google_spend import _parse_spend_value
        assert _parse_spend_value("1 234,56") == pytest.approx(1234.56)

    def test_integer(self):
        from services.google_spend import _parse_spend_value
        assert _parse_spend_value("500") == pytest.approx(500.0)

    def test_invalid_returns_zero(self):
        from services.google_spend import _parse_spend_value
        assert _parse_spend_value("n/a") == pytest.approx(0.0)


# ===========================================================================
# Тест 3: _find_target_sheet — выбор вкладки по имени «DD.MM», фолбэк по A1
# ===========================================================================

class TestFindTargetSheet:
    def test_matches_by_name(self):
        """Основной путь: вкладка называется «DD.MM» — ищем по имени."""
        from services.google_spend import _find_target_sheet
        wb = load_workbook(BytesIO(_build_workbook_bytes({
            "27.06": _sample_tab_rows("27 июня 2026 г. - 27 июня 2026 г."),
            "28.06": _sample_tab_rows("28 июня 2026 г. - 28 июня 2026 г."),
        })))
        result = _find_target_sheet(wb, date(2026, 6, 28))
        assert result == ("28.06", "2026-06-28")

    def test_fallback_by_a1_when_name_is_irregular(self):
        """Встречаются «кривые» имена вкладок («0804») — находим по периоду в A1."""
        from services.google_spend import _find_target_sheet
        wb = load_workbook(BytesIO(_build_workbook_bytes({
            "0804": _sample_tab_rows("8 апреля 2026 г. - 8 апреля 2026 г."),
        })))
        result = _find_target_sheet(wb, date(2026, 4, 8))
        assert result == ("0804", "2026-04-08")

    def test_not_found_returns_none(self):
        from services.google_spend import _find_target_sheet
        wb = load_workbook(BytesIO(_build_workbook_bytes({
            "27.06": _sample_tab_rows("27 июня 2026 г. - 27 июня 2026 г."),
        })))
        result = _find_target_sheet(wb, date(2026, 6, 28))
        assert result is None


# ===========================================================================
# Тест 4: _extract_spend_from_sheet — «Итого», не сумма строк
# ===========================================================================

class TestExtractSpendFromSheet:
    def test_numeric_value_direct(self):
        """xlsx отдаёт числа как float напрямую (не строку с запятой, как CSV)."""
        from services.google_spend import _extract_spend_from_sheet
        wb = load_workbook(BytesIO(_build_workbook_bytes({
            "28.06": _sample_tab_rows(spend_val=189.42),
        })))
        assert _extract_spend_from_sheet(wb["28.06"]) == pytest.approx(189.42)

    def test_string_value_with_comma_still_parsed(self):
        """На случай если ячейка всё же строка «189,42» — парсим как раньше."""
        from services.google_spend import _extract_spend_from_sheet
        wb = load_workbook(BytesIO(_build_workbook_bytes({
            "28.06": _sample_tab_rows(spend_val="189,42"),
        })))
        assert _extract_spend_from_sheet(wb["28.06"]) == pytest.approx(189.42)

    def test_no_itogo_row_returns_none(self):
        rows = [
            ["28 июня 2026 г. - 28 июня 2026 г."],
            ["Статус", "Кампания", "Тип", "Расходы"],
            ["Активна", "CityA", "Поиск", 100.0],
        ]
        from services.google_spend import _extract_spend_from_sheet
        wb = load_workbook(BytesIO(_build_workbook_bytes({"28.06": rows})))
        assert _extract_spend_from_sheet(wb["28.06"]) is None


# ===========================================================================
# Тест 4b: колонка «Расходы» по ЗАГОЛОВКУ, а не по индексу 7
# (баг: подрядчик вставил колонку «Бюджет» → сдвиг → записался CTR)
# ===========================================================================

class TestSpendColumnByHeader:
    def test_shifted_column_reads_rasxody_not_ctr(self):
        """Вставлена колонка «Бюджет» → «Расходы» на индексе 8.

        По заголовку берём $104.05 (Расходы), а НЕ $0.1868 (CTR на индексе 7).
        """
        from services.google_spend import _extract_spend_from_sheet
        wb = load_workbook(BytesIO(_build_workbook_bytes({
            "12.07": _shifted_tab_rows(spend_val=104.05, ctr_val=0.1868),
        })))
        result = _extract_spend_from_sheet(wb["12.07"])
        assert result == pytest.approx(104.05)
        assert result != pytest.approx(0.1868)  # не CTR по мёртвому индексу 7

    def test_normal_column_still_index7_via_header(self):
        """Обычная (несдвинутая) вкладка — «Расходы» на индексе 7, читаем как раньше."""
        from services.google_spend import _extract_spend_from_sheet
        wb = load_workbook(BytesIO(_build_workbook_bytes({
            "28.06": _sample_tab_rows(spend_val=189.42),
        })))
        assert _extract_spend_from_sheet(wb["28.06"]) == pytest.approx(189.42)

    def test_header_variants_case_and_spaces(self):
        """Заголовок в разном регистре/с пробелами/с суффиксом — всё распознаётся."""
        from services.google_spend import _extract_spend_from_sheet
        rows = [
            ["12 июля 2026 г. - 12 июля 2026 г."],
            ["Статус", "Кампания", "Тип", "  РАСХОДЫ, $  "],  # регистр + пробелы + суффикс
            ["Активна", "CityA", "Поиск", 77.0],
            ["Итого (Кампании)", "", "", 123.45],
        ]
        wb = load_workbook(BytesIO(_build_workbook_bytes({"12.07": rows})))
        assert _extract_spend_from_sheet(wb["12.07"]) == pytest.approx(123.45)

    def test_header_not_found_falls_back_to_index7_with_warning(self, caplog):
        """Заголовок «Расходы» отсутствует («Затраты») → фолбэк на индекс 7 + warning."""
        import logging
        from services.google_spend import _extract_spend_from_sheet
        wb = load_workbook(BytesIO(_build_workbook_bytes({
            "12.07": _no_spend_header_tab_rows(val_at_idx7=88.0),
        })))
        with caplog.at_level(logging.WARNING, logger="services.google_spend"):
            result = _extract_spend_from_sheet(wb["12.07"])
        assert result == pytest.approx(88.0)  # фолбэк на исторический индекс 7
        assert any(
            "не найдена по заголовку" in rec.message or "фолбэк на индекс" in rec.message
            for rec in caplog.records
        )

    def test_spend_index_from_header_direct(self):
        """_spend_index_from_header возвращает 0-based индекс ячейки «Расходы»."""
        from services.google_spend import _spend_index_from_header
        header = ["Статус", "Кампания", "Тип", "Показы", "Клики", "Ср. цена",
                  "Бюджет", "CTR", "Расходы", "CPM", "Конв."]
        assert _spend_index_from_header(header) == 8
        # Нет колонки расхода → None
        assert _spend_index_from_header(["Статус", "Кампания", "CTR"]) is None


# ===========================================================================
# Тест 5: read_daily_google_spend — навигация по вкладке + парсинг «Итого»
# ===========================================================================

def test_read_daily_google_spend_takes_itogo_not_sum():
    """Расход берётся из «Итого (Кампании)» — не сумма строк кампаний."""
    # 89.21 + 100.21 = 189.42 — но в строке Итого тоже 189.42
    # Важно: не складываем строки, берём Итого
    sheets = {"28.06": _sample_tab_rows(spend_val=189.42)}

    with patch("requests.get", return_value=_make_xlsx_resp(sheets)):
        from services.google_spend import read_daily_google_spend
        result = read_daily_google_spend("fake_sheet_id", target_date=date(2026, 6, 28))

    assert result is not None
    date_iso, spend = result
    assert date_iso == "2026-06-28"
    assert spend == pytest.approx(189.42)


def test_read_daily_google_spend_finds_tab_by_name_for_target_date():
    """Вкладка ищется по имени «DD.MM» для запрошенной даты."""
    sheets = {"15.07": _sample_tab_rows("15 июля 2026 г. - 15 июля 2026 г.", spend_val=350.0)}

    with patch("requests.get", return_value=_make_xlsx_resp(sheets)):
        from services.google_spend import read_daily_google_spend
        result = read_daily_google_spend("fake_sheet_id", target_date=date(2026, 7, 15))

    assert result is not None
    date_iso, spend = result
    assert date_iso == "2026-07-15"
    assert spend == pytest.approx(350.0)


def test_read_daily_google_spend_shifted_column_reads_correct_spend():
    """Сквозной путь дневного сбора: вкладка 12.07 с вставленной «Бюджет» →
    расход $104.05 (по заголовку), а не CTR $0.1868 (по мёртвому индексу 7)."""
    sheets = {"12.07": _shifted_tab_rows(spend_val=104.05, ctr_val=0.1868)}

    with patch("requests.get", return_value=_make_xlsx_resp(sheets)):
        from services.google_spend import read_daily_google_spend
        result = read_daily_google_spend("fake_sheet_id", target_date=date(2026, 7, 12))

    assert result is not None
    date_iso, spend = result
    assert date_iso == "2026-07-12"
    assert spend == pytest.approx(104.05)


def test_read_daily_google_spend_fallback_by_a1_when_tab_name_irregular():
    """Вкладка с «кривым» именем («0804») находится через скан A1-периода."""
    sheets = {"0804": _sample_tab_rows("8 апреля 2026 г. - 8 апреля 2026 г.", spend_val=120.0)}

    with patch("requests.get", return_value=_make_xlsx_resp(sheets)):
        from services.google_spend import read_daily_google_spend
        result = read_daily_google_spend("fake_sheet_id", target_date=date(2026, 4, 8))

    assert result is not None
    date_iso, spend = result
    assert date_iso == "2026-04-08"
    assert spend == pytest.approx(120.0)


def test_read_daily_google_spend_defaults_to_yesterday_local():
    """Без target_date снимок ищется за вчера по времени CityA."""
    sheets = {"28.06": _sample_tab_rows("28 июня 2026 г. - 28 июня 2026 г.", spend_val=50.0)}

    with patch("services.google_spend._yesterday_local", return_value=date(2026, 6, 28)), \
         patch("requests.get", return_value=_make_xlsx_resp(sheets)):
        from services.google_spend import read_daily_google_spend
        result = read_daily_google_spend("fake_sheet_id")

    assert result is not None
    date_iso, spend = result
    assert date_iso == "2026-06-28"
    assert spend == pytest.approx(50.0)


def test_read_daily_google_spend_http_error_returns_none():
    """При ошибке HTTP — возвращает None."""
    with patch("requests.get", return_value=_make_xlsx_resp({}, status=403)):
        from services.google_spend import read_daily_google_spend
        result = read_daily_google_spend("fake_sheet_id", target_date=date(2026, 6, 28))
    assert result is None


def test_read_daily_google_spend_no_itogo_returns_none():
    """Если строки «Итого» нет — возвращает None."""
    rows = [
        ["28 июня 2026 г. - 28 июня 2026 г."],
        ["Статус", "Кампания", "Тип", "Расходы"],
        ["Активна", "CityA", "Поиск", 100.0],
    ]
    sheets = {"28.06": rows}
    with patch("requests.get", return_value=_make_xlsx_resp(sheets)):
        from services.google_spend import read_daily_google_spend
        result = read_daily_google_spend("fake_sheet_id", target_date=date(2026, 6, 28))
    assert result is None


def test_read_daily_google_spend_missing_tab_for_target_date_returns_none():
    """Нет вкладки за вчера (или другой запрошенный день) → честный None (не ошибка/исключение)."""
    sheets = {"27.06": _sample_tab_rows("27 июня 2026 г. - 27 июня 2026 г.")}
    with patch("requests.get", return_value=_make_xlsx_resp(sheets)):
        from services.google_spend import read_daily_google_spend
        result = read_daily_google_spend("fake_sheet_id", target_date=date(2026, 6, 28))
    assert result is None


# ===========================================================================
# Тест 6: capture_daily_google_spend — добавляет в историю, не удаляет старое
#
# read_daily_google_spend уже покрыт своими тестами (xlsx-навигация выше) —
# здесь мокаем его напрямую, чтобы изолированно проверить только upsert-логику
# capture_daily_google_spend (было так же и раньше, просто мокался requests.get
# вместо самой функции, т.к. раньше вся логика чтения была в одной функции).
# ===========================================================================

def test_capture_adds_to_history():
    """capture_daily_google_spend дописывает новый день, сохраняет старые."""
    existing = {"2026-06-28": 189.42}

    with patch("services.google_spend.read_daily_google_spend",
               return_value=("2026-06-29", 210.50)), \
         patch("services.google_spend._load_spend_history", return_value=existing.copy()), \
         patch("services.google_spend._save_spend_history") as mock_save:

        from services.google_spend import capture_daily_google_spend
        capture_daily_google_spend("fake_sheet_id")

    # Проверяем что save вызвали с обоими днями
    saved = mock_save.call_args[0][0]
    assert saved["2026-06-28"] == pytest.approx(189.42)  # старый день сохранён
    assert saved["2026-06-29"] == pytest.approx(210.50)  # новый день добавлен


def test_capture_upserts_existing_day():
    """capture_daily_google_spend перезаписывает день при повторном захвате."""
    existing = {"2026-06-28": 189.42}  # старое значение

    with patch("services.google_spend.read_daily_google_spend",
               return_value=("2026-06-28", 195.0)), \
         patch("services.google_spend._load_spend_history", return_value=existing.copy()), \
         patch("services.google_spend._save_spend_history") as mock_save:

        from services.google_spend import capture_daily_google_spend
        capture_daily_google_spend("fake_sheet_id")

    saved = mock_save.call_args[0][0]
    assert saved["2026-06-28"] == pytest.approx(195.0)  # обновлено


def test_capture_does_nothing_on_error():
    """При ошибке чтения снимка — history не сохраняется."""
    with patch("services.google_spend.read_daily_google_spend", return_value=None), \
         patch("services.google_spend._save_spend_history") as mock_save:

        from services.google_spend import capture_daily_google_spend
        capture_daily_google_spend("fake_sheet_id")

    mock_save.assert_not_called()


# ===========================================================================
# Тест 7: backfill_google_spend_from_tabs — восстановление истории по вкладкам
# ===========================================================================

class TestBackfillGoogleSpendFromTabs:
    def test_backfills_missing_days_from_multiple_tabs(self):
        """Несколько вкладок без снимка в истории — все добавляются разом."""
        sheets = {
            "10.06": _sample_tab_rows("10 июня 2026 г. - 10 июня 2026 г.", spend_val=150.0),
            "11.06": _sample_tab_rows("11 июня 2026 г. - 11 июня 2026 г.", spend_val=200.0),
            "12.06": _sample_tab_rows("12 июня 2026 г. - 12 июня 2026 г.", spend_val=175.5),
        }
        with patch("requests.get", return_value=_make_xlsx_resp(sheets)), \
             patch("services.google_spend._load_spend_history", return_value={}), \
             patch("services.google_spend._save_spend_history") as mock_save:

            from services.google_spend import backfill_google_spend_from_tabs
            stats = backfill_google_spend_from_tabs("fake_sheet_id")

        saved = mock_save.call_args[0][0]
        assert saved["2026-06-10"] == pytest.approx(150.0)
        assert saved["2026-06-11"] == pytest.approx(200.0)
        assert saved["2026-06-12"] == pytest.approx(175.5)
        assert stats["added"] == 3
        assert stats["skipped_existing"] == 0
        assert sorted(stats["days"]) == ["2026-06-10", "2026-06-11", "2026-06-12"]

    def test_does_not_overwrite_existing_days(self):
        """День уже есть в истории — backfill его НЕ трогает без необходимости."""
        sheets = {
            "10.06": _sample_tab_rows("10 июня 2026 г. - 10 июня 2026 г.", spend_val=999.0),
        }
        existing = {"2026-06-10": 150.0}
        with patch("requests.get", return_value=_make_xlsx_resp(sheets)), \
             patch("services.google_spend._load_spend_history", return_value=existing.copy()), \
             patch("services.google_spend._save_spend_history") as mock_save:

            from services.google_spend import backfill_google_spend_from_tabs
            stats = backfill_google_spend_from_tabs("fake_sheet_id")

        assert stats["added"] == 0
        assert stats["skipped_existing"] == 1
        mock_save.assert_not_called()  # нечего сохранять — новых дней нет

    def test_unparsed_tabs_are_skipped_and_counted(self):
        """Вкладки без распознаваемого A1-периода (старый формат) — пропускаются."""
        sheets = {
            "10.06": _sample_tab_rows("10 июня 2026 г. - 10 июня 2026 г.", spend_val=150.0),
            "0804": [["Кампания"], ["Статус", "Тип"]],  # старый формат без периода в A1
        }
        with patch("requests.get", return_value=_make_xlsx_resp(sheets)), \
             patch("services.google_spend._load_spend_history", return_value={}), \
             patch("services.google_spend._save_spend_history") as mock_save:

            from services.google_spend import backfill_google_spend_from_tabs
            stats = backfill_google_spend_from_tabs("fake_sheet_id")

        assert stats["added"] == 1
        assert stats["unparsed"] == 1
        mock_save.assert_called_once()  # одна распознанная вкладка — сохраняем

    def test_download_failure_returns_empty_stats_no_save(self):
        """Ошибка скачивания файла — пустая статистика, история не трогается."""
        with patch("requests.get", return_value=_make_xlsx_resp({}, status=500)), \
             patch("services.google_spend._save_spend_history") as mock_save:

            from services.google_spend import backfill_google_spend_from_tabs
            stats = backfill_google_spend_from_tabs("fake_sheet_id")

        # Контракт расширен ключом "updated" (рескан окна дотягивает изменившиеся
        # дни).
        assert stats == {"added": 0, "updated": 0, "skipped_existing": 0, "unparsed": 0, "days": []}
        mock_save.assert_not_called()


# ===========================================================================
# Тест 8: get_google_week_spend — сумма по снимкам, отсутствующие дни = 0
# ===========================================================================

def test_get_google_week_spend_full_coverage():
    """Все 3 дня покрыты — возвращает точную сумму."""
    history = {
        "2026-07-01": 100.0,
        "2026-07-02": 150.0,
        "2026-07-03": 200.0,
    }
    with patch("services.google_spend._load_spend_history", return_value=history):
        from services.google_spend import get_google_week_spend
        result = get_google_week_spend("2026-07-01", "2026-07-03")

    assert result == pytest.approx(450.0)


def test_get_google_week_spend_partial_coverage():
    """Часть дней отсутствует — считаются как 0."""
    history = {
        "2026-07-01": 100.0,
        # 2026-07-02 отсутствует
        "2026-07-03": 200.0,
    }
    with patch("services.google_spend._load_spend_history", return_value=history):
        from services.google_spend import get_google_week_spend
        result = get_google_week_spend("2026-07-01", "2026-07-03")

    assert result == pytest.approx(300.0)  # только покрытые дни


def test_get_google_week_spend_no_coverage():
    """Ни одного дня нет в истории — возвращает 0.0."""
    with patch("services.google_spend._load_spend_history", return_value={}):
        from services.google_spend import get_google_week_spend
        result = get_google_week_spend("2026-07-01", "2026-07-07")

    assert result == pytest.approx(0.0)


def test_get_google_week_spend_single_day():
    """Один день в диапазоне."""
    history = {"2026-07-10": 333.33}
    with patch("services.google_spend._load_spend_history", return_value=history):
        from services.google_spend import get_google_week_spend
        result = get_google_week_spend("2026-07-10", "2026-07-10")

    assert result == pytest.approx(333.33)


# ===========================================================================
# Тест 9: budget_scaler get_google_week_spend → перенаправлен на google_spend
# ===========================================================================

def test_budget_scaler_google_week_spend_uses_google_spend_module():
    """budget_scaler.get_google_week_spend вызывает services.google_spend.get_google_week_spend."""
    from datetime import date

    with patch("services.google_spend.get_google_week_spend", return_value=999.0) as mock_impl:
        from services.budget_scaler import get_google_week_spend as bs_fn
        result = bs_fn(date(2026, 7, 1), date(2026, 7, 7))

    assert result == pytest.approx(999.0)
    mock_impl.assert_called_once_with("2026-07-01", "2026-07-07")


# ===========================================================================
# Тест 10: get_daily_google_spend — различает «0» и «нет данных»
# (раньше отсутствие дня отдавалось как «$0» и вводило в заблуждение)
# ===========================================================================

def test_get_daily_google_spend_present_returns_value():
    """День есть в истории → возвращает число (в т.ч. настоящий 0.0)."""
    history = {"2026-07-13": 250.0, "2026-07-12": 0.0}
    with patch("services.google_spend._load_spend_history", return_value=history):
        from services.google_spend import get_daily_google_spend
        assert get_daily_google_spend("2026-07-13") == pytest.approx(250.0)
        # Настоящий 0.0 — это ноль, НЕ «нет данных»
        assert get_daily_google_spend("2026-07-12") == pytest.approx(0.0)


def test_get_daily_google_spend_missing_returns_none_not_zero():
    """Дня НЕТ в истории → None (маркер отсутствия), а НЕ 0."""
    history = {"2026-07-13": 250.0}
    with patch("services.google_spend._load_spend_history", return_value=history):
        from services.google_spend import get_daily_google_spend
        result = get_daily_google_spend("2026-07-14")  # вкладку ещё не залили
    assert result is None


# ===========================================================================
# Тест 11: get_last_known_google_spend — последний известный день для honest-строки
# ===========================================================================

def test_get_last_known_returns_latest_on_or_before():
    """Возвращает самый свежий день с датой <= указанной."""
    history = {"2026-07-10": 100.0, "2026-07-12": 300.0, "2026-07-13": 250.0}
    with patch("services.google_spend._load_spend_history", return_value=history):
        from services.google_spend import get_last_known_google_spend
        # За 14.07 данных нет — последний известный день <= 14.07 это 13.07
        result = get_last_known_google_spend("2026-07-14")
    assert result == ("2026-07-13", pytest.approx(250.0))


def test_get_last_known_ignores_future_days():
    """Дни ПОЗЖЕ указанной границы не учитываются."""
    history = {"2026-07-10": 100.0, "2026-07-20": 999.0}
    with patch("services.google_spend._load_spend_history", return_value=history):
        from services.google_spend import get_last_known_google_spend
        result = get_last_known_google_spend("2026-07-14")
    assert result == ("2026-07-10", pytest.approx(100.0))


def test_get_last_known_empty_history_returns_none():
    with patch("services.google_spend._load_spend_history", return_value={}):
        from services.google_spend import get_last_known_google_spend
        assert get_last_known_google_spend("2026-07-14") is None


# ===========================================================================
# Тест 12: count_missing_google_days — счётчик неполноты окна для подписи ДРР
# ===========================================================================

def test_count_missing_google_days_counts_absent_days():
    """Считает дни диапазона без снимка (для пометки «Google неполный за N дней»)."""
    history = {"2026-07-01": 100.0, "2026-07-03": 200.0}  # нет 02, 04, 05
    with patch("services.google_spend._load_spend_history", return_value=history):
        from services.google_spend import count_missing_google_days
        assert count_missing_google_days("2026-07-01", "2026-07-05") == 3


def test_count_missing_google_days_full_coverage_zero():
    history = {"2026-07-01": 100.0, "2026-07-02": 200.0, "2026-07-03": 300.0}
    with patch("services.google_spend._load_spend_history", return_value=history):
        from services.google_spend import count_missing_google_days
        assert count_missing_google_days("2026-07-01", "2026-07-03") == 0


# ===========================================================================
# Тест 13: рескан последних N дней — дотягивает появившийся/изменившийся день
# ===========================================================================

def test_rescan_pulls_newly_appeared_day():
    """Подрядчик залил вкладку за пропущенный день позже — рескан её дотягивает."""
    from datetime import date as _date

    # В окне рескана появилась вкладка 12.07, которой не было в истории
    sheets = {
        "12.07": _sample_tab_rows("12 июля 2026 г. - 12 июля 2026 г.", spend_val=175.0),
    }
    existing = {"2026-07-11": 100.0}  # 12.07 отсутствует
    with patch("requests.get", return_value=_make_xlsx_resp(sheets)), \
         patch("services.google_spend._load_spend_history", return_value=existing.copy()), \
         patch("services.google_spend._save_spend_history") as mock_save:

        from services.google_spend import rescan_recent_google_spend
        stats = rescan_recent_google_spend("fake_sheet_id", days=7, now=_date(2026, 7, 15))

    saved = mock_save.call_args[0][0]
    assert saved["2026-07-12"] == pytest.approx(175.0)  # дотянут
    assert saved["2026-07-11"] == pytest.approx(100.0)  # старый сохранён
    assert stats["added"] == 1


def test_rescan_updates_changed_day():
    """Подрядчик поправил число за уже сохранённый день (в окне) — рескан обновляет."""
    from datetime import date as _date

    sheets = {
        "12.07": _sample_tab_rows("12 июля 2026 г. - 12 июля 2026 г.", spend_val=200.0),
    }
    existing = {"2026-07-12": 150.0}  # старое значение, вкладка теперь 200
    with patch("requests.get", return_value=_make_xlsx_resp(sheets)), \
         patch("services.google_spend._load_spend_history", return_value=existing.copy()), \
         patch("services.google_spend._save_spend_history") as mock_save:

        from services.google_spend import rescan_recent_google_spend
        stats = rescan_recent_google_spend("fake_sheet_id", days=7, now=_date(2026, 7, 15))

    saved = mock_save.call_args[0][0]
    assert saved["2026-07-12"] == pytest.approx(200.0)  # обновлено
    assert stats["updated"] == 1


def test_rescan_idempotent_no_changes_no_save():
    """Ничего не изменилось (значение то же) — рескан не пишет на диск."""
    from datetime import date as _date

    sheets = {
        "12.07": _sample_tab_rows("12 июля 2026 г. - 12 июля 2026 г.", spend_val=150.0),
    }
    existing = {"2026-07-12": 150.0}  # то же значение
    with patch("requests.get", return_value=_make_xlsx_resp(sheets)), \
         patch("services.google_spend._load_spend_history", return_value=existing.copy()), \
         patch("services.google_spend._save_spend_history") as mock_save:

        from services.google_spend import rescan_recent_google_spend
        stats = rescan_recent_google_spend("fake_sheet_id", days=7, now=_date(2026, 7, 15))

    assert stats["added"] == 0
    assert stats["updated"] == 0
    mock_save.assert_not_called()


def test_rescan_ignores_days_outside_window():
    """Вкладка старше окна (за пределами последних N дней) — не трогается."""
    from datetime import date as _date

    sheets = {
        # 01.07 — вне окна last-7 от 15.07 (окно 09.07..15.07)
        "01.07": _sample_tab_rows("1 июля 2026 г. - 1 июля 2026 г.", spend_val=999.0),
    }
    with patch("requests.get", return_value=_make_xlsx_resp(sheets)), \
         patch("services.google_spend._load_spend_history", return_value={}), \
         patch("services.google_spend._save_spend_history") as mock_save:

        from services.google_spend import rescan_recent_google_spend
        stats = rescan_recent_google_spend("fake_sheet_id", days=7, now=_date(2026, 7, 15))

    assert stats["added"] == 0
    mock_save.assert_not_called()


def test_missing_day_not_stored_as_zero():
    """Дня без вкладки capture НЕ пишет 0 — в истории он остаётся отсутствующим,
    а не превращается в фиктивный ноль."""
    # Вкладки за 14.07 в файле нет — read вернёт None, capture ничего не сохранит
    sheets = {"13.07": _sample_tab_rows("13 июля 2026 г. - 13 июля 2026 г.", spend_val=250.0)}
    with patch("requests.get", return_value=_make_xlsx_resp(sheets)), \
         patch("services.google_spend._load_spend_history", return_value={"2026-07-13": 250.0}), \
         patch("services.google_spend._save_spend_history") as mock_save:

        from services.google_spend import capture_daily_google_spend
        capture_daily_google_spend("fake_sheet_id", target_date=date(2026, 7, 14))

    # Ничего не сохранили — 14.07 НЕ записан нулём
    mock_save.assert_not_called()
