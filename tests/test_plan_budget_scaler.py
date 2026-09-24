"""
Тесты plan_reader + budget_scaler план-гейт.

Все внешние зависимости мокируются:
- requests.get (Google Sheets export)
- openpyxl (xlsx parsing) — через Workbook mock
- get_google_week_spend
- get_fb_week_spend
- get_amo_week_revenue
- integrations.amo.get_leads_window, classify_lead
- services.exchange_rate.get_usd_to_lcy
- services.notifications.send_telegram
"""

import sys
import io
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, call
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импортов проекта
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

_TZ = timezone(timedelta(hours=5))


@pytest.fixture(autouse=True)
def isolated_daily_cap_state(tmp_path, monkeypatch):
    """Перенаправляет state-файл дневного капа во временную папку для всех тестов.

    dry_run вызывает get_remaining_daily_pct/get_day_start_budget (чтение),
    что могло бы писать в реальный data/budget_daily_cap_state.json проекта.
    """
    import services.budget_daily_cap as cap_module
    state_file = tmp_path / "budget_daily_cap_state.json"
    monkeypatch.setattr(cap_module, "_CAP_STATE_FILE", state_file)


# ===========================================================================
# Вспомогательные: построение фейкового xlsx через openpyxl
# ===========================================================================

def _make_ws_mock(ws_rows: list, a1_value: str = "GENERAL") -> MagicMock:
    """Создаёт мок листа openpyxl с поддержкой .cell(row=1, column=1).value и iter_rows.

    Args:
        ws_rows: список кортежей строк (для iter_rows).
        a1_value: значение ячейки A1 (используется при поиске GENERAL-вкладки).
    """
    ws_mock = MagicMock()
    ws_mock.iter_rows.return_value = iter(ws_rows)
    # cell(row=1, column=1).value → a1_value
    cell_mock = MagicMock()
    cell_mock.value = a1_value
    ws_mock.cell.return_value = cell_mock
    return ws_mock


def _make_fake_workbook(rows_by_label: dict, col_idx: int = 4, sheet_name: str = "GENERAL") -> MagicMock:
    """Создаёт мок openpyxl.Workbook с одной вкладкой.

    Args:
        rows_by_label: {label: value_at_col_idx}
        col_idx: индекс колонки плана (0-based).
        sheet_name: имя вкладки (может отличаться от "GENERAL").
    """
    # Строим строки: row[0] = label, row[col_idx] = value, остальные = None
    ws_rows = []
    for label, val in rows_by_label.items():
        row = [None] * (col_idx + 1)
        row[0] = label
        row[col_idx] = val
        ws_rows.append(tuple(row))

    ws_mock = _make_ws_mock(ws_rows, a1_value="GENERAL")

    wb_mock = MagicMock()
    wb_mock.sheetnames = [sheet_name]
    wb_mock.__getitem__ = lambda self, name: ws_mock

    return wb_mock


def _make_resp_ok(content: bytes = b"xlsx") -> MagicMock:
    """Фейковый успешный HTTP-ответ с байтами."""
    resp = MagicMock()
    resp.status_code = 200
    resp.content = content
    return resp


# ===========================================================================
# Тест 1: plan_reader — правильный выбор недели по дню месяца
# ===========================================================================

class TestGetWeekCol:
    def test_week1_day1(self):
        from services.plan_reader import _get_week_col
        num, col, day_from = _get_week_col(1)
        assert num == 1 and col == 4 and day_from == 1

    def test_week1_day7(self):
        from services.plan_reader import _get_week_col
        num, col, _ = _get_week_col(7)
        assert num == 1 and col == 4

    def test_week2_day8(self):
        from services.plan_reader import _get_week_col
        num, col, _ = _get_week_col(8)
        assert num == 2 and col == 7

    def test_week2_day14(self):
        from services.plan_reader import _get_week_col
        num, col, _ = _get_week_col(14)
        assert num == 2 and col == 7

    def test_week3(self):
        from services.plan_reader import _get_week_col
        num, col, _ = _get_week_col(15)
        assert num == 3 and col == 10

    def test_week4_day28(self):
        from services.plan_reader import _get_week_col
        num, col, _ = _get_week_col(28)
        assert num == 4 and col == 13

    def test_week5_day29(self):
        from services.plan_reader import _get_week_col
        num, col, _ = _get_week_col(29)
        assert num == 5 and col == 16

    def test_week5_day31(self):
        from services.plan_reader import _get_week_col
        num, col, _ = _get_week_col(31)
        assert num == 5 and col == 16


# ===========================================================================
# Тест 2: plan_reader — строка «Бюджет» с #REF! (строка-ошибка) пропускается,
#          берётся числовая строка
# ===========================================================================

def test_plan_reader_budget_ref_skipped():
    """Верхняя строка «Бюджет» с #REF! пропускается, берётся числовая строка."""
    # День 1 → неделя 1 → col_idx=4
    now = datetime(2026, 7, 1, 12, 0, tzinfo=_TZ)

    # Строки листа: «Бюджет» дважды — сначала строка-ошибка, потом число
    rows = [
        ("Факт",         50_000_000.0),  # revenue_plan_lcy
        ("Бюджет",       "#REF!"),         # битая — должна быть пропущена
        ("Строка_пустая", None),
        ("Бюджет",       1000.0),          # числовая — должна взяться
        ("% от факта",   0.08),
    ]

    col_idx = 4  # неделя 1 → E(4)

    # Строим ws_rows: row[0]=label, row[col_idx]=value
    ws_rows = []
    for label, val in rows:
        row = [None] * (col_idx + 1)
        row[0] = label
        row[col_idx] = val
        ws_rows.append(tuple(row))

    ws_mock = _make_ws_mock(ws_rows, a1_value="GENERAL")

    wb_mock = MagicMock()
    wb_mock.sheetnames = ["GENERAL"]
    wb_mock.__getitem__ = lambda self, name: ws_mock

    with patch("requests.get", return_value=_make_resp_ok(b"fake")), \
         patch("openpyxl.load_workbook", return_value=wb_mock), \
         patch("services.plan_reader._load_cache", return_value={}), \
         patch("services.plan_reader._save_cache"):

        from services.plan_reader import read_general_plan
        result = read_general_plan("fake_sheet_id", now=now)

    assert result is not None
    assert result["budget_usd"] == pytest.approx(1000.0)
    assert result["revenue_plan_lcy"] == pytest.approx(50_000_000.0)
    assert result["unit_target"] == pytest.approx(0.08)


# ===========================================================================
# Тест 2б (регрессия июльского бага): битая строка «Бюджет» содержит
# ЧИСЛОВЫЕ НУЛИ (не #REF!) и раньше проходила проверку _is_numeric первой,
# из-за чего план недели становился $0. Теперь выбор идёт по максимальному
# положительному месячному плану (колонка B) — битая строка (B=0) отсеивается,
# берётся рабочая строка (B=30000).
# ===========================================================================

def test_plan_reader_budget_numeric_zero_row_skipped_july():
    """Битая строка «Бюджет» с числовыми нулями по неделям пропускается,
    выбирается рабочая строка по максимальному месячному плану B."""
    # День 1 → неделя 1 → col_idx=4 (E)
    now = datetime(2026, 7, 1, 12, 0, tzinfo=_TZ)
    col_idx = 4

    # Полные строки листа (не только col_idx, но и колонка B(1) — месячный план).
    # Индексы: 0=label, 1=B(месяц), 4=E(неделя1) ... как в июльском формате листа.
    def _row(label, month_val, week1_val):
        row = [None] * (col_idx + 1)
        row[0] = label
        row[1] = month_val
        row[col_idx] = week1_val
        return tuple(row)

    ws_rows = [
        _row("Факт",       None, 50_000_000.0),
        # Битая агрегатная строка (верхняя строка листа): B=0, неделя1=0 —
        # числовой ноль, а не #REF! — раньше проходил проверку _is_numeric и
        # ошибочно выбирался как план недели → $0.
        _row("Бюджет",     0.0, 0.0),
        # Рабочая строка (ниже по листу): B=30000 (месячный план), неделя1=5000.
        _row("Бюджет",     30000.0, 5000.0),
        _row("% от факта", None, 0.08),
    ]

    ws_mock = _make_ws_mock(ws_rows, a1_value="GENERAL")
    wb_mock = MagicMock()
    wb_mock.sheetnames = ["GENERAL"]
    wb_mock.__getitem__ = lambda self, name: ws_mock

    with patch("requests.get", return_value=_make_resp_ok(b"fake")), \
         patch("openpyxl.load_workbook", return_value=wb_mock), \
         patch("services.plan_reader._load_cache", return_value={}), \
         patch("services.plan_reader._save_cache"):

        from services.plan_reader import read_general_plan
        result = read_general_plan("fake_sheet_id", now=now)

    assert result is not None
    # Ключевая проверка бага: НЕ $0 (битая строка), а $5000 (рабочая строка)
    assert result["budget_usd"] == pytest.approx(5000.0)
    assert result["revenue_plan_lcy"] == pytest.approx(50_000_000.0)
    assert result["unit_target"] == pytest.approx(0.08)


# ===========================================================================
# Тест 3а: plan_reader — вкладка с именем НЕ "GENERAL", но A1="GENERAL" — выбирается
# ===========================================================================

def test_plan_reader_sheet_name_not_general_but_a1_is():
    """Вкладка называется 'CityA ' (с пробелом), но A1='GENERAL' — выбирается корректно."""
    now = datetime(2026, 7, 1, 12, 0, tzinfo=_TZ)
    col_idx = 4

    rows_data = [
        ("GENERAL",     None),           # A1 — маркер (читается отдельно через .cell)
        ("Факт",        50_000_000.0),
        ("Бюджет",      1000.0),
        ("% от факта",  0.08),
    ]
    ws_rows = []
    for label, val in rows_data:
        row = [None] * (col_idx + 1)
        row[0] = label
        row[col_idx] = val
        ws_rows.append(tuple(row))

    # Вкладка с именем "CityA " (с пробелом), A1="GENERAL"
    ws_mock = _make_ws_mock(ws_rows, a1_value="GENERAL")
    wb_mock = MagicMock()
    wb_mock.sheetnames = ["CityA ", "CityB", "CityC"]
    wb_mock.__getitem__ = lambda self, name: ws_mock

    with patch("requests.get", return_value=_make_resp_ok(b"fake")), \
         patch("openpyxl.load_workbook", return_value=wb_mock), \
         patch("services.plan_reader._load_cache", return_value={}), \
         patch("services.plan_reader._save_cache"):

        from services.plan_reader import read_general_plan
        result = read_general_plan("fake_sheet_id", now=now)

    assert result is not None
    assert result["budget_usd"] == pytest.approx(1000.0)
    assert result["unit_target"] == pytest.approx(0.08)


# ===========================================================================
# Тест 3б: plan_reader — ни у одной вкладки A1!="GENERAL" → fallback на первую
# ===========================================================================

def test_plan_reader_fallback_to_first_sheet():
    """Если ни у одной вкладки A1!='GENERAL' — fallback на первую вкладку (с warning)."""
    now = datetime(2026, 7, 1, 12, 0, tzinfo=_TZ)
    col_idx = 4

    rows_data = [
        ("Факт",       50_000_000.0),
        ("Бюджет",     1000.0),
        ("% от факта", 0.08),
    ]
    ws_rows = []
    for label, val in rows_data:
        row = [None] * (col_idx + 1)
        row[0] = label
        row[col_idx] = val
        ws_rows.append(tuple(row))

    # A1 у всех вкладок НЕ "GENERAL"
    ws_mock = _make_ws_mock(ws_rows, a1_value="Июль")
    wb_mock = MagicMock()
    wb_mock.sheetnames = ["Июль", "CityA", "CityB"]
    wb_mock.__getitem__ = lambda self, name: ws_mock

    with patch("requests.get", return_value=_make_resp_ok(b"fake")), \
         patch("openpyxl.load_workbook", return_value=wb_mock), \
         patch("services.plan_reader._load_cache", return_value={}), \
         patch("services.plan_reader._save_cache"):

        from services.plan_reader import read_general_plan
        result = read_general_plan("fake_sheet_id", now=now)

    # Fallback использует первую вкладку — данные присутствуют
    assert result is not None
    assert result["budget_usd"] == pytest.approx(1000.0)


# ===========================================================================
# Тест 4: plan_reader — unit_target > 1 (записан в %) → нормализуется в долю
# ===========================================================================

def test_plan_reader_unit_target_as_percent():
    """unit_target=8 (8%) → нормализуется в 0.08."""
    now = datetime(2026, 7, 1, 12, 0, tzinfo=_TZ)
    col_idx = 4

    ws_rows = [
        tuple([None] * (col_idx + 1)),
    ]
    rows_data = [
        ("Факт",        50_000_000.0),
        ("Бюджет",      1000.0),
        ("% от факта",  8.0),  # <-- записан как 8, не 0.08
    ]
    ws_rows = []
    for label, val in rows_data:
        row = [None] * (col_idx + 1)
        row[0] = label
        row[col_idx] = val
        ws_rows.append(tuple(row))

    ws_mock = _make_ws_mock(ws_rows, a1_value="GENERAL")
    wb_mock = MagicMock()
    wb_mock.sheetnames = ["GENERAL"]
    wb_mock.__getitem__ = lambda self, name: ws_mock

    with patch("requests.get", return_value=_make_resp_ok(b"fake")), \
         patch("openpyxl.load_workbook", return_value=wb_mock), \
         patch("services.plan_reader._load_cache", return_value={}), \
         patch("services.plan_reader._save_cache"):

        from services.plan_reader import read_general_plan
        result = read_general_plan("fake_sheet_id", now=now)

    assert result is not None
    assert result["unit_target"] == pytest.approx(0.08)


# ===========================================================================
# Тест 5: plan_reader — кеш используется при валидном TTL
# ===========================================================================

def test_plan_reader_uses_disk_cache():
    """При свежем кеше Google Sheets не запрашивается."""
    import time
    now = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ)
    # Ключ включает номер недели: день 15 → неделя 3 → :w3
    key = "sheet_abc:2026-07:w3"
    cached_data = {
        "week_label": "неделя 3 (2026-07-15–21)",
        "budget_usd": 1000.0,
        "revenue_plan_lcy": 10_000_000.0,
        "unit_target": 0.08,
    }
    cache = {key: {"saved_at": time.time() - 100, "data": cached_data}}  # 100с назад — свежий

    with patch("services.plan_reader._load_cache", return_value=cache), \
         patch("requests.get") as mock_get:

        from services.plan_reader import read_general_plan
        result = read_general_plan("sheet_abc", now=now)

    # Google не запрашивался
    mock_get.assert_not_called()
    assert result == cached_data


# ===========================================================================
# Тест 6: budget_scaler план-гейт — разрешает при запасе и ДРР в норме
# ===========================================================================

def _base_scale_cfg_with_plan() -> dict:
    """Базовый конфиг с plan_sheet_id."""
    return {
        "enabled": True,
        "kill_switch": False,
        "scale_enabled": False,
        "max_budget_increase_pct": 20,
        "max_adset_budget_mult": 2.0,
        "max_adset_daily_budget": 300,
        "max_total_daily_budget": 4000,
        "max_scales_per_run": 2,
        "plan_sheet_id": "test_sheet_id",
    }


def _make_local_ad_win(ad_id="ad1") -> dict:
    """Объявление-кандидат по новой (Фаза 2) семантике: payments>0 обязателен,
    иначе _select_sales_candidates отбросит его до план-гейта не дойдёт смысла."""
    return {
        "ad_id": ad_id, "ad_name": "Победитель", "city": "CityA",
        "adset_type": "L2", "adset_id": None,
        "spend": 80.0, "leads": 8, "qual_pct": 20.0, "romi": 150.0,
        "cpl": 10.0, "ctr": 2.0, "hook_rate": 30.0, "impressions": 5000,
        "video_p25": 0, "video_p100": 0, "video_views_3s": 0,
        "payments": 3, "outcomes_matched_at": None,
        "days_running": 10, "effective_status": "ACTIVE",
        "recommendation": "ЖДАТЬ", "reason": "",
    }


def _make_scale_dec(ad_id="ad1") -> dict:
    return {
        "ad_id": ad_id, "ad_name": "Победитель", "adset_id": "",
        "action": "SCALE", "score": 7, "reasons": [],
    }


def test_plan_gate_allows_when_headroom_and_drr_ok():
    """Гейт разрешает: запас > 0 и ДРР <= target."""
    plan_data = {
        "week_label": "неделя 1 (2026-07-01–07)",
        "budget_usd": 2000.0,
        "revenue_plan_lcy": 10_000_000.0,
        "unit_target": 0.10,  # 10% ДРР
    }
    # fb=500, google=300 → total=800, headroom=1200>0
    # drr = 800 * 100 / 8_000_000 = 0.01 < 0.10 → OK
    all_budgets = {"adset1": {"daily_budget_usd": 100.0, "effective_status": "ACTIVE", "name": "Тест"}}

    with patch("services.budget_scaler.get_scale_config", return_value=_base_scale_cfg_with_plan()), \
         patch("services.plan_reader.read_general_plan", return_value=plan_data), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=500.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=300.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=8_000_000.0), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=[_make_local_ad_win()]), \
         patch("services.decision_policy.score_and_decide", return_value=[_make_scale_dec()]), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler.set_adset_budget"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    # Гейт разрешил — ran=True, не skipped_reason plan_gate
    assert result["ran"] is True
    assert result.get("skipped_reason") is None or "plan_gate" not in str(result.get("skipped_reason", ""))


def test_plan_gate_blocks_when_no_headroom():
    """Гейт блокирует: headroom <= 0 (расход превысил план)."""
    plan_data = {
        "week_label": "неделя 1 (2026-07-01–07)",
        "budget_usd": 500.0,   # план $500
        "revenue_plan_lcy": 10_000_000.0,
        "unit_target": 0.10,
    }
    # fb=300 + google=300 = 600 > 500 → headroom=-100 < 0

    with patch("services.budget_scaler.get_scale_config", return_value=_base_scale_cfg_with_plan()), \
         patch("services.plan_reader.read_general_plan", return_value=plan_data), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=300.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=300.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=8_000_000.0), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is True
    assert "plan_gate" in str(result.get("skipped_reason", ""))
    assert "запас исчерпан" in str(result.get("skipped_reason", ""))


def test_plan_gate_blocks_when_drr_exceeded():
    """Гейт блокирует: ДРР > target."""
    plan_data = {
        "week_label": "неделя 1 (2026-07-01–07)",
        "budget_usd": 2000.0,
        "revenue_plan_lcy": 10_000_000.0,
        "unit_target": 0.05,  # target 5% ДРР
    }
    # fb=500 + google=500 = 1000; drr = 1000 * 100 / 1_000_000 = 0.1 > 0.05

    with patch("services.budget_scaler.get_scale_config", return_value=_base_scale_cfg_with_plan()), \
         patch("services.plan_reader.read_general_plan", return_value=plan_data), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=500.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=500.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=1_000_000.0), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is True
    assert "plan_gate" in str(result.get("skipped_reason", ""))
    assert "юнитка превышена" in str(result.get("skipped_reason", ""))


def test_plan_gate_blocks_when_revenue_zero():
    """Гейт блокирует: выручка AMO = 0 → ДРР неизвестен."""
    plan_data = {
        "week_label": "неделя 1 (2026-07-01–07)",
        "budget_usd": 2000.0,
        "revenue_plan_lcy": 10_000_000.0,
        "unit_target": 0.08,
    }

    with patch("services.budget_scaler.get_scale_config", return_value=_base_scale_cfg_with_plan()), \
         patch("services.plan_reader.read_general_plan", return_value=plan_data), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=300.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=100.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=0.0), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is True
    assert "plan_gate" in str(result.get("skipped_reason", ""))
    assert "выручка" in str(result.get("skipped_reason", "")).lower()


# ===========================================================================
# Тест 7: FB + Google суммируются в total_spend_usd
# ===========================================================================

def test_fb_google_summed_in_plan_gate():
    """Проверяем что FB + Google корректно суммируются при вычислении headroom."""
    plan_data = {
        "week_label": "неделя 2 (2026-07-08–14)",
        "budget_usd": 1000.0,  # план $1000
        "revenue_plan_lcy": 10_000_000.0,
        "unit_target": 0.10,
    }
    # fb=600, google=500 → total=1100 > 1000 → headroom=-100 → блок
    with patch("services.budget_scaler.get_scale_config", return_value=_base_scale_cfg_with_plan()), \
         patch("services.plan_reader.read_general_plan", return_value=plan_data), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=600.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=500.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=10_000_000.0), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    # Заблокировано — запас исчерпан (суммарный FB+Google превысил план)
    assert "plan_gate" in str(result.get("skipped_reason", ""))
    assert "запас исчерпан" in str(result.get("skipped_reason", ""))


# ===========================================================================
# Тест 8 (D4, fail-closed): без plan_sheet_id — план-гейт БЛОКИРУЕТ подъём
# ===========================================================================
# Раньше (fail-open) отсутствие медиаплана молча пропускало гейт и позволяло
# скейлеру предлагать/поднимать бюджеты без проверки недельного плана/ДРР.
# По решению CTO это опасно — заменено на fail-closed: нет плана → не поднимаем.

def test_no_plan_sheet_id_blocks_gate_fail_closed():
    """Если plan_sheet_id не задан — план-гейт БЛОКИРУЕТ (fail-closed), а не пропускает."""
    cfg = {
        "enabled": True,
        "kill_switch": False,
        "scale_enabled": False,
        "max_budget_increase_pct": 20,
        "max_adset_budget_mult": 2.0,
        "max_adset_daily_budget": 300,
        "max_total_daily_budget": 4000,
        "max_scales_per_run": 2,
        # plan_sheet_id отсутствует
    }
    all_budgets = {"adset1": {"daily_budget_usd": 100.0, "effective_status": "ACTIVE", "name": "Тест"}}

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=[_make_local_ad_win()]), \
         patch("services.decision_policy.score_and_decide", return_value=[_make_scale_dec()]), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler.set_adset_budget") as mock_set, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    # D4: план-гейт заблокировал — рекомендаций нет, бюджет не трогаем
    mock_set.assert_not_called()
    assert result["ran"] is True
    assert "plan_gate" in str(result.get("skipped_reason", ""))
    assert "fail-closed" in str(result.get("skipped_reason", ""))
    assert result["recommendations"] == []


# ===========================================================================
# Тест 9: get_google_week_spend — суммирует по дням и городам
# ===========================================================================

def test_get_google_week_spend_sums_search_and_youtube():
    """get_google_week_spend суммирует Search и YouTube по каждому дню."""
    from datetime import date

    # Каждый день: Search=$50 по городам, YouTube=$30
    search_day = {"CityA": 30.0, "CityB": 20.0}
    yt_day = {"CityA": 20.0, "CityB": 10.0}

    with patch("integrations.google_ads.get_google_search_costs_by_city", return_value=search_day), \
         patch("integrations.google_ads.get_youtube_costs_by_city", return_value=yt_day):

        from integrations.google_ads import get_google_week_spend
        result = get_google_week_spend(date(2026, 7, 1), date(2026, 7, 3))

    # 3 дня × ($50 + $30) = $240
    assert result == pytest.approx(240.0)


def test_get_google_week_spend_single_day():
    """get_google_week_spend для одного дня."""
    from datetime import date

    search_day = {"CityA": 100.0}
    yt_day = {"CityA": 50.0}

    with patch("integrations.google_ads.get_google_search_costs_by_city", return_value=search_day), \
         patch("integrations.google_ads.get_youtube_costs_by_city", return_value=yt_day):

        from integrations.google_ads import get_google_week_spend
        result = get_google_week_spend(date(2026, 7, 10), date(2026, 7, 10))

    assert result == pytest.approx(150.0)


# ===========================================================================
# Тест 10: plan_reader — корректно определяет неделю по дню 15 (неделя 3)
# ===========================================================================

def test_plan_reader_week3_col10():
    """День 15 → неделя 3 → колонка K(10)."""
    now = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ)
    col_idx = 10  # неделя 3

    rows_data = [
        ("Факт",        40_000_000.0),
        ("Бюджет",      "#REF!"),
        ("Бюджет",      6000.0),
        ("% от факта",  0.09),
    ]
    ws_rows = []
    for label, val in rows_data:
        row = [None] * (col_idx + 1)
        row[0] = label
        row[col_idx] = val
        ws_rows.append(tuple(row))

    ws_mock = _make_ws_mock(ws_rows, a1_value="GENERAL")
    wb_mock = MagicMock()
    wb_mock.sheetnames = ["GENERAL"]
    wb_mock.__getitem__ = lambda self, name: ws_mock

    with patch("requests.get", return_value=_make_resp_ok(b"fake")), \
         patch("openpyxl.load_workbook", return_value=wb_mock), \
         patch("services.plan_reader._load_cache", return_value={}), \
         patch("services.plan_reader._save_cache"):

        from services.plan_reader import read_general_plan
        result = read_general_plan("fake_sheet_id", now=now)

    assert result is not None
    assert result["budget_usd"] == pytest.approx(6000.0)
    assert result["unit_target"] == pytest.approx(0.09)
    assert "неделя 3" in result["week_label"]


# ===========================================================================
# Тест 11: get_amo_week_revenue — суммирует только оплаченные
# ===========================================================================

def test_amo_week_revenue_only_payments():
    """get_amo_week_revenue суммирует только лиды с classify_lead='оплата'."""
    leads = [
        {"id": 1, "status_id": 999, "price": 100_000, "contacts": [], "custom_fields": []},
        {"id": 2, "status_id": 888, "price": 200_000, "contacts": [], "custom_fields": []},
        {"id": 3, "status_id": 777, "price": 50_000,  "contacts": [], "custom_fields": []},
    ]

    # classify_lead: id=1 → 'оплата', id=2 → 'квал', id=3 → 'новый'
    def fake_classify(lead: dict) -> str:
        return {"1": "оплата", "2": "квал", "3": "новый"}.get(str(lead["id"]), "новый")

    now = datetime(2026, 7, 1, 12, 0, tzinfo=_TZ)

    with patch("integrations.amo.get_leads_window", return_value=leads), \
         patch("integrations.amo.classify_lead", side_effect=fake_classify):

        from services.budget_scaler import get_amo_week_revenue
        result = get_amo_week_revenue(now, now)

    # Только id=1 оплачен → ¤100_000
    assert result == pytest.approx(100_000.0)


# ===========================================================================
# Тест 12: _cache_key включает номер недели — разные недели, разные ключи
# ===========================================================================

def test_cache_key_includes_week_number():
    """_cache_key разный для разных недель одного месяца."""
    from services.plan_reader import _cache_key

    # Два дня одного месяца из разных недель
    day3 = datetime(2026, 7, 3, 10, 0, tzinfo=_TZ)   # неделя 1 (1-7)
    day30 = datetime(2026, 7, 30, 10, 0, tzinfo=_TZ)  # неделя 5 (29-31)

    key3 = _cache_key("sheet_abc", day3)
    key30 = _cache_key("sheet_abc", day30)

    assert key3 != key30
    assert ":w1" in key3
    assert ":w5" in key30


# ===========================================================================
# Тест 13: разные недели возвращают разные budget_usd (кеш не смешивает)
# ===========================================================================

def test_different_weeks_return_different_budgets():
    """Вызовы read_general_plan для нед.1 и нед.5 возвращают разные budget_usd.

    Пример: нед.1 col=4 → $1000, нед.5 col=16 → $500.
    При месячном ключе (баг) второй вызов вернул бы $1000 из кеша.
    С ключом по неделям каждый вызов идёт в Google и возвращает правильное значение.
    """
    # Воркбук с разными значениями по колонкам недель
    # col=4 (нед.1) → $1000, col=16 (нед.5) → $500
    BUDGET_W1 = 1000.0
    BUDGET_W5 = 500.0

    def make_ws_for_col(col_idx: int, budget: float) -> MagicMock:
        rows_data = [
            ("Факт",       50_000_000.0),
            ("Бюджет",     budget),
            ("% от факта", 0.08),
        ]
        ws_rows = []
        for label, val in rows_data:
            row = [None] * (col_idx + 1)
            row[0] = label
            row[col_idx] = val
            ws_rows.append(tuple(row))
        return _make_ws_mock(ws_rows, a1_value="GENERAL")

    now_w1 = datetime(2026, 7, 3, 10, 0, tzinfo=_TZ)   # нед.1 → col=4
    now_w5 = datetime(2026, 7, 30, 10, 0, tzinfo=_TZ)  # нед.5 → col=16

    # Два вызова — два отдельных mock-воркбука (кеш пустой для каждого)
    results = {}
    for now, col_idx, budget in [
        (now_w1, 4, BUDGET_W1),
        (now_w5, 16, BUDGET_W5),
    ]:
        ws_mock = make_ws_for_col(col_idx, budget)
        wb_mock = MagicMock()
        wb_mock.sheetnames = ["CityA "]
        wb_mock.__getitem__ = lambda self, name, ws=ws_mock: ws

        with patch("requests.get", return_value=_make_resp_ok(b"fake")), \
             patch("openpyxl.load_workbook", return_value=wb_mock), \
             patch("services.plan_reader._load_cache", return_value={}), \
             patch("services.plan_reader._save_cache"):

            from services.plan_reader import read_general_plan
            results[now.day] = read_general_plan("sheet_july", now=now)

    assert results[3] is not None
    assert results[30] is not None
    assert results[3]["budget_usd"] == pytest.approx(BUDGET_W1)
    assert results[30]["budget_usd"] == pytest.approx(BUDGET_W5)
    # Ключевая проверка: значения разные, кеш не смешал
    assert results[3]["budget_usd"] != results[30]["budget_usd"]


# ===========================================================================
# Тест 14 (июльский баг по-городной структуры листа): вкладки CityA/CityB/
# CityC/CityD/CityE с РАЗНЫМИ бюджетами — read_general_plan суммирует
# ВСЕ городские вкладки по имени, а не читает одну (как раньше по A1==GENERAL).
# ===========================================================================

def _make_city_ws(budget_month: float, budget_week: float, revenue: float,
                   unit_target: float, col_idx: int = 4, a1_value=None) -> MagicMock:
    """Строит мок вкладки одного города с рабочей строкой «Бюджет»."""
    def _row(label, month_val, week_val):
        row = [None] * (col_idx + 1)
        row[0] = label
        row[1] = month_val
        row[col_idx] = week_val
        return tuple(row)

    ws_rows = [
        _row("Факт", None, revenue),
        _row("Бюджет", budget_month, budget_week),
        _row("% от факта", None, unit_target),
    ]
    return _make_ws_mock(ws_rows, a1_value=a1_value)


def test_read_general_plan_sums_all_city_tabs():
    """Все городские вкладки реестра с разными бюджетами суммируются в budget_usd.

    Пример: неделя1 — CityA $5000, CityB $4000, CityC $3000,
    CityD $2000, CityE $1000 → сумма $15000.
    """
    now = datetime(2026, 7, 1, 12, 0, tzinfo=_TZ)  # неделя 1 → col_idx=4
    col_idx = 4

    city_budgets = {
        "CityA ": (30000.0, 5000.0, 20_000_000.0, 0.08),   # имя вкладки с хвостовым пробелом
        "CityB": (24000.0, 4000.0, 15_000_000.0, 0.09),
        "CityC": (18000.0, 3000.0, 8_000_000.0, 0.07),
        "CityD": (12000.0, 2000.0, 5_000_000.0, 0.06),
        "CityE": (6000.0, 1000.0, 4_000_000.0, 0.05),  # A1=None
    }

    ws_by_name = {
        name: _make_city_ws(month, week, rev, unit, col_idx=col_idx, a1_value=None)
        for name, (month, week, rev, unit) in city_budgets.items()
    }

    wb_mock = MagicMock()
    wb_mock.sheetnames = list(ws_by_name.keys()) + ["Онлайн"]
    # Вкладка "Онлайн" — отдельный кабинет, не должна попасть в матч по реестру городов
    online_ws = _make_city_ws(5000.0, 800.0, 1_000_000.0, 0.1, col_idx=col_idx, a1_value="GENERAL ТГ")

    def _getitem(self, name):
        if name == "Онлайн":
            return online_ws
        return ws_by_name[name]

    wb_mock.__getitem__ = _getitem

    with patch("requests.get", return_value=_make_resp_ok(b"fake")), \
         patch("openpyxl.load_workbook", return_value=wb_mock), \
         patch("services.plan_reader._load_cache", return_value={}), \
         patch("services.plan_reader._save_cache"):

        from services.plan_reader import read_general_plan
        result = read_general_plan("fake_sheet_id", now=now)

    assert result is not None
    expected_total = sum(week for _, week, _, _ in city_budgets.values())
    assert result["budget_usd"] == pytest.approx(expected_total)
    assert result["budget_usd"] == pytest.approx(15000.0)

    expected_revenue = sum(rev for _, _, rev, _ in city_budgets.values())
    assert result["revenue_plan_lcy"] == pytest.approx(expected_revenue)

    # cities_detail присутствует и содержит все города реестра, но НЕ "Онлайн"
    assert "cities_detail" in result
    assert set(result["cities_detail"].keys()) == {"CityA", "CityB", "CityC", "CityD", "CityE"}
    assert "Онлайн" not in result["cities_detail"]
    assert result["cities_detail"]["CityA"] == pytest.approx(5000.0)


def test_read_general_plan_prefers_budget_total_row():
    """Вкладка с несколькими строками «Бюджет» (разбивка по под-командам) +
    отдельной итоговой «Бюджет тотал» — берётся именно «Бюджет тотал», а не
    сумма/максимум обычных строк «Бюджет» (пример: на вкладке CityC
    строки «Бюджет» — это разбивка направлений, не подлежащая суммированию)."""
    now = datetime(2026, 7, 1, 12, 0, tzinfo=_TZ)
    col_idx = 4

    def _row(label, month_val, week_val):
        row = [None] * (col_idx + 1)
        row[0] = label
        row[1] = month_val
        row[col_idx] = week_val
        return tuple(row)

    # CityC: несколько строк «Бюджет» (разбивка направлений с меньшими суммами)
    # + «Бюджет тотал» — итоговая, которая и должна быть выбрана.
    cityc_ws = _make_ws_mock([
        _row("Факт", None, 10_000_000.0),
        _row("Бюджет", 1000.0, 200.0),
        _row("Бюджет", 1500.0, 300.0),
        _row("Бюджет", 2000.0, 400.0),   # макс. среди обычных «Бюджет», но НЕ итог
        _row("Бюджет тотал", 6000.0, 1000.0),  # итог (≠ сумме и ≠ максимуму обычных) — должен победить
        _row("% от факта", None, 0.10),
    ], a1_value=None)

    wb_mock = MagicMock()
    wb_mock.sheetnames = ["CityC"]
    wb_mock.__getitem__ = lambda self, name: cityc_ws

    with patch("requests.get", return_value=_make_resp_ok(b"fake")), \
         patch("openpyxl.load_workbook", return_value=wb_mock), \
         patch("services.plan_reader._load_cache", return_value={}), \
         patch("services.plan_reader._save_cache"):

        from services.plan_reader import read_general_plan
        result = read_general_plan("fake_sheet_id", now=now)

    assert result is not None
    assert result["budget_usd"] == pytest.approx(1000.0)
    assert result["cities_detail"]["CityC"] == pytest.approx(1000.0)


def test_read_general_plan_excludes_broken_city_tab():
    """Город с битой строкой «Бюджет» (нет числового значения) исключается из суммы,
    остальные города продолжают учитываться (logger.warning, не падение)."""
    now = datetime(2026, 7, 1, 12, 0, tzinfo=_TZ)
    col_idx = 4

    def _row(label, month_val, week_val):
        row = [None] * (col_idx + 1)
        row[0] = label
        row[1] = month_val
        row[col_idx] = week_val
        return tuple(row)

    # CityA — рабочая вкладка
    citya_ws = _make_ws_mock([
        _row("Факт", None, 20_000_000.0),
        _row("Бюджет", 30000.0, 5000.0),
        _row("% от факта", None, 0.08),
    ], a1_value=None)

    # CityB — битая вкладка: строка «Бюджет» есть, но значение недели — не число (#REF!)
    cityb_ws = _make_ws_mock([
        _row("Факт", None, 15_000_000.0),
        _row("Бюджет", "#REF!", "#REF!"),
        _row("% от факта", None, 0.09),
    ], a1_value=None)

    wb_mock = MagicMock()
    wb_mock.sheetnames = ["CityA ", "CityB"]
    wb_mock.__getitem__ = lambda self, name: {"CityA ": citya_ws, "CityB": cityb_ws}[name]

    with patch("requests.get", return_value=_make_resp_ok(b"fake")), \
         patch("openpyxl.load_workbook", return_value=wb_mock), \
         patch("services.plan_reader._load_cache", return_value={}), \
         patch("services.plan_reader._save_cache"):

        from services.plan_reader import read_general_plan
        result = read_general_plan("fake_sheet_id", now=now)

    assert result is not None
    # Только CityA вошла в сумму — CityB исключена
    assert result["budget_usd"] == pytest.approx(5000.0)
    assert result["revenue_plan_lcy"] == pytest.approx(20_000_000.0)
    assert set(result["cities_detail"].keys()) == {"CityA"}


def test_read_general_plan_all_city_tabs_broken_returns_none():
    """Если ВСЕ городские вкладки битые (валидных 0) — read_general_plan
    возвращает None (fail-closed выше по стеку), legacy-fallback тоже не
    срабатывает, т.к. ни у одной вкладки нет A1=='GENERAL'."""
    now = datetime(2026, 7, 1, 12, 0, tzinfo=_TZ)
    col_idx = 4

    def _row(label, val):
        row = [None] * (col_idx + 1)
        row[0] = label
        row[col_idx] = val
        return tuple(row)

    broken_ws = _make_ws_mock([
        _row("Факт", "#REF!"),
        _row("Бюджет", "#REF!"),
        _row("% от факта", "#REF!"),
    ], a1_value=None)

    wb_mock = MagicMock()
    wb_mock.sheetnames = ["CityA ", "CityB"]
    wb_mock.__getitem__ = lambda self, name: broken_ws

    with patch("requests.get", return_value=_make_resp_ok(b"fake")), \
         patch("openpyxl.load_workbook", return_value=wb_mock), \
         patch("services.plan_reader._load_cache", return_value={}), \
         patch("services.plan_reader._save_cache"):

        from services.plan_reader import read_general_plan
        result = read_general_plan("fake_sheet_id", now=now)

    assert result is None


def test_read_general_plan_online_tab_ignored_even_with_general_a1():
    """Вкладка «Онлайн» с A1=='GENERAL ТГ' игнорируется в city-пути (не входит
    в реестр городов), даже если бы её имя случайно совпало с A1-маркером."""
    now = datetime(2026, 7, 1, 12, 0, tzinfo=_TZ)
    col_idx = 4

    citya_ws = _make_city_ws(30000.0, 5000.0, 20_000_000.0, 0.08, col_idx=col_idx, a1_value=None)
    online_ws = _make_city_ws(5000.0, 800.0, 1_000_000.0, 0.1, col_idx=col_idx, a1_value="GENERAL ТГ")

    wb_mock = MagicMock()
    wb_mock.sheetnames = ["CityA ", "Онлайн"]
    wb_mock.__getitem__ = lambda self, name: {"CityA ": citya_ws, "Онлайн": online_ws}[name]

    with patch("requests.get", return_value=_make_resp_ok(b"fake")), \
         patch("openpyxl.load_workbook", return_value=wb_mock), \
         patch("services.plan_reader._load_cache", return_value={}), \
         patch("services.plan_reader._save_cache"):

        from services.plan_reader import read_general_plan
        result = read_general_plan("fake_sheet_id", now=now)

    assert result is not None
    # Только CityA, "Онлайн" не входит в сумму
    assert result["budget_usd"] == pytest.approx(5000.0)
    assert "Онлайн" not in result["cities_detail"]


# ===========================================================================
# Тест 15 (регресс единого формата): если городских вкладок НЕТ (по именам
# из реестра не найдено ни одной), но есть вкладка с A1=='GENERAL' — старый
# путь работает как раньше (совместимость со старым единым листом плана,
# к которому формат может вернуться).
# ===========================================================================

def test_read_general_plan_legacy_format_fallback_when_no_city_tabs():
    """Нет вкладок с именами городов из реестра → fallback на A1=='GENERAL' (legacy)."""
    now = datetime(2026, 6, 1, 12, 0, tzinfo=_TZ)
    col_idx = 4

    rows_data = [
        ("Факт", 50_000_000.0),
        ("Бюджет", 1000.0),
        ("% от факта", 0.08),
    ]
    ws_rows = []
    for label, val in rows_data:
        row = [None] * (col_idx + 1)
        row[0] = label
        row[col_idx] = val
        ws_rows.append(tuple(row))

    # Имя вкладки НЕ совпадает ни с одним городом из реестра — только A1 маркер
    ws_mock = _make_ws_mock(ws_rows, a1_value="GENERAL")
    wb_mock = MagicMock()
    wb_mock.sheetnames = ["Июнь-лист"]
    wb_mock.__getitem__ = lambda self, name: ws_mock

    with patch("requests.get", return_value=_make_resp_ok(b"fake")), \
         patch("openpyxl.load_workbook", return_value=wb_mock), \
         patch("services.plan_reader._load_cache", return_value={}), \
         patch("services.plan_reader._save_cache"):

        from services.plan_reader import read_general_plan
        result = read_general_plan("fake_sheet_id", now=now)

    assert result is not None
    assert result["budget_usd"] == pytest.approx(1000.0)
    assert result["revenue_plan_lcy"] == pytest.approx(50_000_000.0)
    assert result["unit_target"] == pytest.approx(0.08)
    assert result["cities_detail"] == {"Июнь-лист": pytest.approx(1000.0)}
