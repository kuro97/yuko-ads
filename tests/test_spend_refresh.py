"""
Тесты для services/spend_refresh.py и крон-гейта _cron_refresh_active_spend.

Мокают FB API — реальных запросов не делают.
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Мокаем google.genai до импорта любого модуля который может его тянуть
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())

from services import creative_intelligence as ci
import services.spend_refresh as sr


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_kb_path():
    """Сбрасываем глобальный DB_PATH перед и после каждого теста."""
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


@pytest.fixture
def kb(tmp_path):
    """Инициализирует KB с тестовыми объявлениями (ACTIVE + PAUSED)."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)

    # Вставляем тестовые записи напрямую в БД
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # ACTIVE: два объявления
    conn.execute(
        """
        INSERT INTO creative_kb (ad_id, ad_name, status, effective_status, spend, leads, impressions)
        VALUES ('act_001', 'CityA | L2 | Тест 1', 'ACTIVE', 'ACTIVE', 0.0, 0, 0)
        """
    )
    conn.execute(
        """
        INSERT INTO creative_kb (ad_id, ad_name, status, effective_status, spend, leads, impressions)
        VALUES ('act_002', 'CityB | L1 | Тест 2', 'ACTIVE', 'ACTIVE', 5.0, 3, 500)
        """
    )
    # PAUSED: одно объявление — не должно обновляться
    conn.execute(
        """
        INSERT INTO creative_kb (ad_id, ad_name, status, effective_status, spend, leads, impressions)
        VALUES ('pau_003', 'CityC | CR | Пауза', 'PAUSED', 'PAUSED', 100.0, 10, 5000)
        """
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Тест 1: обновляет только ACTIVE, не трогает PAUSED
# ---------------------------------------------------------------------------


def test_refresh_updates_active_only(kb, monkeypatch):
    """refresh_active_ads_spend обновляет spend только для ACTIVE, PAUSED не трогает."""
    # Мок FB: возвращаем свежие данные только для активных
    mock_metrics = {
        "act_001": {"spend": 123.45, "leads": 5, "impressions": 10000,
                    "ctr": 1.0, "cpm": 12.0, "clicks": 100, "frequency": 1.5,
                    "video_views_3s": 0, "thruplay": 0,
                    "video_p25": 0, "video_p50": 0, "video_p75": 0, "video_p100": 0,
                    "hook_rate": 0, "hold_rate": 0},
        "act_002": {"spend": 67.89, "leads": 2, "impressions": 3000,
                    "ctr": 1.2, "cpm": 22.0, "clicks": 36, "frequency": 1.1,
                    "video_views_3s": 0, "thruplay": 0,
                    "video_p25": 0, "video_p50": 0, "video_p75": 0, "video_p100": 0,
                    "hook_rate": 0, "hold_rate": 0},
    }

    with patch("services.spend_refresh._fetch_lifetime_insights", return_value=mock_metrics):
        result = sr.refresh_active_ads_spend()

    assert result["active_count"] == 2, "KB должна содержать 2 активных объявления"
    assert result["fetched"] == 2, "FB вернул данные для 2 объявлений"
    assert result["updated"] == 2, "Обновлено должно быть ровно 2 записи"

    # Проверяем что данные реально записались
    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    row1 = conn.execute("SELECT spend, leads, impressions FROM creative_kb WHERE ad_id = 'act_001'").fetchone()
    row2 = conn.execute("SELECT spend, leads, impressions FROM creative_kb WHERE ad_id = 'act_002'").fetchone()
    row3 = conn.execute("SELECT spend, leads, impressions FROM creative_kb WHERE ad_id = 'pau_003'").fetchone()
    conn.close()

    # ACTIVE обновились
    assert row1["spend"] == pytest.approx(123.45)
    assert row1["leads"] == 5
    assert row1["impressions"] == 10000

    assert row2["spend"] == pytest.approx(67.89)

    # PAUSED не трогали — старые значения
    assert row3["spend"] == pytest.approx(100.0)
    assert row3["leads"] == 10
    assert row3["impressions"] == 5000


# ---------------------------------------------------------------------------
# Тест 2: нет активных — 0 обновлений, FB не вызывается
# ---------------------------------------------------------------------------


def test_refresh_no_active_ads(kb, monkeypatch):
    """Если нет ACTIVE объявлений — FB не вызывается, updated=0."""
    # Переключаем все на PAUSED
    conn = sqlite3.connect(kb)
    conn.execute("UPDATE creative_kb SET effective_status = 'PAUSED'")
    conn.commit()
    conn.close()

    with patch("services.spend_refresh._fetch_lifetime_insights") as mock_fb:
        result = sr.refresh_active_ads_spend()
        mock_fb.assert_not_called()

    assert result["active_count"] == 0
    assert result["updated"] == 0
    assert result["fetched"] == 0


# ---------------------------------------------------------------------------
# Тест 3: крон-гейт — Страж (ARCH-phase1-guardian G8): каждые 2ч в 08-22 CityA,
# дедуп по слоту «дата-час» (было: раз в день в 12:xx, дедуп по дате).
# ---------------------------------------------------------------------------

_REFRESH_HOURS = (8, 10, 12, 14, 16, 18, 20, 22)


def test_cron_gate_hour_12(tmp_path, monkeypatch):
    """_should_run_spend_refresh: True в _REFRESH_HOURS, дедуп по слоту дата-час."""
    # Подменяем путь к state-файлу чтобы не писать в реальный data/
    test_state_path = tmp_path / "spend_refresh_state.json"
    monkeypatch.setattr(sr, "_STATE_PATH", test_state_path)

    _TZ_LOCAL = timezone(timedelta(hours=5))

    # 12:30 CityA, state пустой → должен запускаться
    now_12 = datetime(2024, 6, 15, 12, 30, tzinfo=_TZ_LOCAL)
    slot_key = f"{now_12.date().isoformat()}-{now_12.hour}"
    assert slot_key not in sr._load_state().get("slots", {})

    # Симулируем state: слот 12 уже запускался сегодня
    test_state_path.write_text(json.dumps({"slots": {slot_key: "running"}}))
    assert slot_key in sr._load_state().get("slots", {})

    # Другой слот (14) в тот же день — снова можно запускать
    slot_key_14 = f"{now_12.date().isoformat()}-14"
    assert slot_key_14 not in sr._load_state().get("slots", {})


def test_cron_gate_wrong_hour(tmp_path, monkeypatch):
    """_cron_refresh_active_spend: при hour не из _REFRESH_HOURS ничего не вызывается."""
    import web.app as app_module

    test_state_path = tmp_path / "spend_refresh_state.json"
    monkeypatch.setattr(sr, "_STATE_PATH", test_state_path)

    _TZ_LOCAL = timezone(timedelta(hours=5))

    # Часы вне {8,10,12,14,16,18,20,22} → гейт не пропускает
    for bad_hour in (0, 6, 9, 11, 13, 19, 23):
        now_other = datetime(2024, 6, 15, bad_hour, 0, tzinfo=_TZ_LOCAL)
        with patch("web.app.datetime") as mock_dt:
            mock_dt.now.return_value = now_other
            with patch("services.spend_refresh._load_state", return_value={}):
                with patch("services.spend_refresh.refresh_active_ads_spend") as mock_refresh_full, \
                     patch("services.spend_refresh.refresh_active_ads_spend_light") as mock_refresh_light:
                    app_module._cron_refresh_active_spend()
                    mock_refresh_full.assert_not_called(), f"Не должен запускаться в {bad_hour}:00"
                    mock_refresh_light.assert_not_called(), f"Не должен запускаться в {bad_hour}:00"


def test_cron_gate_runs_once_per_slot(tmp_path, monkeypatch):
    """_cron_refresh_active_spend: при hour==12 (full) запускается один раз в слоте."""
    import web.app as app_module

    test_state_path = tmp_path / "spend_refresh_state.json"
    monkeypatch.setattr(sr, "_STATE_PATH", test_state_path)

    _TZ_LOCAL = timezone(timedelta(hours=5))
    now_12 = datetime(2024, 6, 15, 12, 0, tzinfo=_TZ_LOCAL)
    slot_key = "2024-06-15-12"

    # State пустой — первый запуск должен пройти, час 12 = полный рефреш
    with patch("web.app.datetime") as mock_dt, \
         patch("services.guardian.mark_spend_refresh_ok") as mock_mark, \
         patch("services.spend_refresh.refresh_active_ads_spend", return_value={"active_count": 5, "fetched": 5, "updated": 5}) as mock_refresh_full, \
         patch("services.spend_refresh.refresh_active_ads_spend_light") as mock_refresh_light:
        mock_dt.now.return_value = now_12
        app_module._cron_refresh_active_spend()
        mock_refresh_full.assert_called_once()
        mock_refresh_light.assert_not_called()
        mock_mark.assert_called_once()

    # Слот записан в state
    state_after = sr._load_state()
    assert slot_key in state_after.get("slots", {})

    # Тот же слот повторно в течение часа — второй запуск пропускается
    with patch("web.app.datetime") as mock_dt, \
         patch("services.spend_refresh.refresh_active_ads_spend") as mock_refresh2:
        mock_dt.now.return_value = now_12
        app_module._cron_refresh_active_spend()
        mock_refresh2.assert_not_called()


def test_cron_gate_light_on_non_full_hour(tmp_path, monkeypatch):
    """_cron_refresh_active_spend: в слоте, отличном от _REFRESH_FULL_HOUR (12), вызывается light."""
    import web.app as app_module

    test_state_path = tmp_path / "spend_refresh_state.json"
    monkeypatch.setattr(sr, "_STATE_PATH", test_state_path)

    _TZ_LOCAL = timezone(timedelta(hours=5))
    now_10 = datetime(2024, 6, 15, 10, 0, tzinfo=_TZ_LOCAL)

    with patch("web.app.datetime") as mock_dt, \
         patch("services.guardian.mark_spend_refresh_ok") as mock_mark, \
         patch("services.spend_refresh.refresh_active_ads_spend") as mock_refresh_full, \
         patch("services.spend_refresh.refresh_active_ads_spend_light", return_value={"active_count": 5, "fetched": 5, "updated": 5}) as mock_refresh_light:
        mock_dt.now.return_value = now_10
        app_module._cron_refresh_active_spend()
        mock_refresh_light.assert_called_once()
        mock_refresh_full.assert_not_called()
        mock_mark.assert_called_once()


# ---------------------------------------------------------------------------
# Тесты refresh_active_ads_status
# ---------------------------------------------------------------------------


def test_status_refresh_demotes_paused(kb, monkeypatch):
    """Объявление ACTIVE в KB, на FB стало PAUSED → effective_status обновляется."""
    # FB вернул: act_001 — PAUSED, act_002 — ACTIVE
    mock_statuses = {"act_001": "PAUSED", "act_002": "ACTIVE"}

    with patch("agent.analyzer._fetch_statuses_now", return_value=mock_statuses):
        result = sr.refresh_active_ads_status()

    assert result["active_count"] == 2
    assert result["status_demoted"] == 1  # act_001 выбыл из ACTIVE

    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    row1 = conn.execute("SELECT effective_status FROM creative_kb WHERE ad_id = 'act_001'").fetchone()
    row2 = conn.execute("SELECT effective_status FROM creative_kb WHERE ad_id = 'act_002'").fetchone()
    # pau_003 изначально PAUSED — не в ACTIVE, функция его не трогает
    row3 = conn.execute("SELECT effective_status FROM creative_kb WHERE ad_id = 'pau_003'").fetchone()
    conn.close()

    assert row1["effective_status"] == "PAUSED"   # обновлён
    assert row2["effective_status"] == "ACTIVE"   # остался
    assert row3["effective_status"] == "PAUSED"   # не тронут (не был ACTIVE в KB)


def test_status_refresh_marks_deleted_if_missing(kb, monkeypatch):
    """Объявление ACTIVE в KB, исчезло из выдачи FB (удалено) → ставим DELETED."""
    # FB вернул только act_002, act_001 пропал (удалено)
    mock_statuses = {"act_002": "ACTIVE"}

    with patch("agent.analyzer._fetch_statuses_now", return_value=mock_statuses):
        result = sr.refresh_active_ads_status()

    assert result["status_demoted"] == 1

    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    row1 = conn.execute("SELECT effective_status FROM creative_kb WHERE ad_id = 'act_001'").fetchone()
    row2 = conn.execute("SELECT effective_status FROM creative_kb WHERE ad_id = 'act_002'").fetchone()
    conn.close()

    assert row1["effective_status"] == "DELETED"  # помечен удалённым
    assert row2["effective_status"] == "ACTIVE"   # остался активным


def test_status_refresh_no_changes_if_all_active(kb, monkeypatch):
    """Все ACTIVE в KB подтверждены как ACTIVE на FB → ничего не меняем."""
    mock_statuses = {"act_001": "ACTIVE", "act_002": "ACTIVE"}

    with patch("agent.analyzer._fetch_statuses_now", return_value=mock_statuses):
        result = sr.refresh_active_ads_status()

    assert result["status_demoted"] == 0
    assert result["active_count"] == 2

    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ad_id, effective_status FROM creative_kb WHERE effective_status = 'ACTIVE'"
    ).fetchall()
    conn.close()

    active_ids = {r["ad_id"] for r in rows}
    # Оба должны остаться ACTIVE
    assert "act_001" in active_ids
    assert "act_002" in active_ids


def test_status_refresh_does_not_touch_spend_fields(kb, monkeypatch):
    """refresh_active_ads_status не трогает spend/leads/impressions."""
    # act_002 у нас с spend=5.0, leads=3 — убедимся что после статус-рефреша не изменились
    mock_statuses = {"act_001": "ACTIVE", "act_002": "PAUSED"}

    with patch("agent.analyzer._fetch_statuses_now", return_value=mock_statuses):
        sr.refresh_active_ads_status()

    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    row2 = conn.execute(
        "SELECT effective_status, spend, leads, impressions FROM creative_kb WHERE ad_id = 'act_002'"
    ).fetchone()
    conn.close()

    assert row2["effective_status"] == "PAUSED"   # статус обновлён
    assert row2["spend"] == pytest.approx(5.0)    # spend не тронут
    assert row2["leads"] == 3                      # leads не тронут
    assert row2["impressions"] == 500              # impressions не тронут


def test_status_refresh_graceful_on_fb_error(kb, monkeypatch):
    """При ошибке FB _fetch_statuses_now — функция логирует и возвращает нули, не падает."""
    with patch("agent.analyzer._fetch_statuses_now", side_effect=Exception("FB timeout")):
        result = sr.refresh_active_ads_status()

    assert result["status_updated"] == 0
    assert result["status_demoted"] == 0
    # active_count вернулся корректно (KB прочиталась до ошибки FB)
    assert result["active_count"] == 2


def test_status_refresh_graceful_on_empty_fb_response(kb, monkeypatch):
    """При пустом ответе FB — не обновляем ничего (защита от случайного DELETE всех)."""
    with patch("agent.analyzer._fetch_statuses_now", return_value={}):
        result = sr.refresh_active_ads_status()

    assert result["status_updated"] == 0
    assert result["status_demoted"] == 0

    # Проверяем что в KB всё осталось как было
    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    active_count = conn.execute(
        "SELECT COUNT(*) as n FROM creative_kb WHERE effective_status = 'ACTIVE'"
    ).fetchone()["n"]
    conn.close()
    assert active_count == 2  # ничего не деградировано


def test_spend_refresh_includes_status_result(kb, monkeypatch):
    """refresh_active_ads_spend теперь возвращает status_updated/status_demoted."""
    mock_metrics = {
        "act_001": {"spend": 10.0, "leads": 1, "impressions": 100,
                    "ctr": 0, "cpm": 0, "clicks": 0, "frequency": 0,
                    "video_views_3s": 0, "thruplay": 0,
                    "video_p25": 0, "video_p50": 0, "video_p75": 0, "video_p100": 0,
                    "hook_rate": 0, "hold_rate": 0},
        "act_002": {"spend": 20.0, "leads": 2, "impressions": 200,
                    "ctr": 0, "cpm": 0, "clicks": 0, "frequency": 0,
                    "video_views_3s": 0, "thruplay": 0,
                    "video_p25": 0, "video_p50": 0, "video_p75": 0, "video_p100": 0,
                    "hook_rate": 0, "hold_rate": 0},
    }
    # act_001 на FB стал PAUSED
    mock_statuses = {"act_001": "PAUSED", "act_002": "ACTIVE"}

    with patch("services.spend_refresh._fetch_lifetime_insights", return_value=mock_metrics), \
         patch("agent.analyzer._fetch_statuses_now", return_value=mock_statuses):
        result = sr.refresh_active_ads_spend()

    # Поля spend остались
    assert result["updated"] == 2
    assert result["fetched"] == 2
    # Новые поля статуса
    assert "status_updated" in result
    assert "status_demoted" in result
    assert result["status_demoted"] == 1  # act_001 выбыл из ACTIVE


# ---------------------------------------------------------------------------
# Тесты refresh_active_ads_spend_light (G6, docs/specs/ARCH-phase1-guardian.md §9)
# ---------------------------------------------------------------------------


def test_spend_refresh_light_does_not_call_status_refresh(kb, monkeypatch):
    """refresh_active_ads_spend_light НЕ вызывает refresh_active_ads_status — только spend."""
    mock_metrics = {
        "act_001": {"spend": 111.0, "leads": 4, "impressions": 1000,
                    "ctr": 0, "cpm": 0, "clicks": 0, "frequency": 0,
                    "video_views_3s": 0, "thruplay": 0,
                    "video_p25": 0, "video_p50": 0, "video_p75": 0, "video_p100": 0,
                    "hook_rate": 0, "hold_rate": 0},
        "act_002": {"spend": 222.0, "leads": 8, "impressions": 2000,
                    "ctr": 0, "cpm": 0, "clicks": 0, "frequency": 0,
                    "video_views_3s": 0, "thruplay": 0,
                    "video_p25": 0, "video_p50": 0, "video_p75": 0, "video_p100": 0,
                    "hook_rate": 0, "hold_rate": 0},
    }

    # Один кабинет: изолируем тест от боевой карты роутинга (data/settings.json) —
    # мультикабинетный сбор spend зовёт insights по числу кабинетов.
    monkeypatch.setattr(
        "services.launch_routing.accounts_to_scan", lambda: ("152882611033373",)
    )
    with patch("services.spend_refresh._fetch_lifetime_insights", return_value=mock_metrics) as mock_insights, \
         patch("services.spend_refresh.refresh_active_ads_status") as mock_status, \
         patch("agent.analyzer._fetch_statuses_now") as mock_fb_statuses:
        result = sr.refresh_active_ads_spend_light()

    # Статусы не тянутся ни напрямую, ни через refresh_active_ads_status
    mock_status.assert_not_called()
    mock_fb_statuses.assert_not_called()
    mock_insights.assert_called_once()

    # spend обновился
    assert result["updated"] == 2
    assert result["fetched"] == 2
    assert result["active_count"] == 2
    # Без status_* полей — light возвращает только spend-результат
    assert "status_updated" not in result
    assert "status_demoted" not in result

    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    row1 = conn.execute("SELECT spend, leads, impressions FROM creative_kb WHERE ad_id = 'act_001'").fetchone()
    row2 = conn.execute("SELECT spend, leads, impressions FROM creative_kb WHERE ad_id = 'act_002'").fetchone()
    conn.close()

    assert row1["spend"] == pytest.approx(111.0)
    assert row1["leads"] == 4
    assert row2["spend"] == pytest.approx(222.0)
    assert row2["leads"] == 8


def test_spend_refresh_light_updates_active_only(kb, monkeypatch):
    """refresh_active_ads_spend_light обновляет spend только для ACTIVE, PAUSED не трогает."""
    mock_metrics = {
        "act_001": {"spend": 55.5, "leads": 1, "impressions": 500,
                    "ctr": 0, "cpm": 0, "clicks": 0, "frequency": 0,
                    "video_views_3s": 0, "thruplay": 0,
                    "video_p25": 0, "video_p50": 0, "video_p75": 0, "video_p100": 0,
                    "hook_rate": 0, "hold_rate": 0},
        "act_002": {"spend": 77.7, "leads": 2, "impressions": 700,
                    "ctr": 0, "cpm": 0, "clicks": 0, "frequency": 0,
                    "video_views_3s": 0, "thruplay": 0,
                    "video_p25": 0, "video_p50": 0, "video_p75": 0, "video_p100": 0,
                    "hook_rate": 0, "hold_rate": 0},
    }

    with patch("services.spend_refresh._fetch_lifetime_insights", return_value=mock_metrics) as mock_insights:
        result = sr.refresh_active_ads_spend_light()

    # _fetch_lifetime_insights вызван только с ACTIVE ad_id (PAUSED не передан)
    called_ids = set(mock_insights.call_args[0][0])
    assert called_ids == {"act_001", "act_002"}
    assert "pau_003" not in called_ids

    assert result["active_count"] == 2
    assert result["updated"] == 2

    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    row3 = conn.execute("SELECT spend, leads, impressions FROM creative_kb WHERE ad_id = 'pau_003'").fetchone()
    conn.close()

    # PAUSED не тронут — исходные значения из фикстуры
    assert row3["spend"] == pytest.approx(100.0)
    assert row3["leads"] == 10
    assert row3["impressions"] == 5000


def test_spend_refresh_light_no_active_ads(kb, monkeypatch):
    """Если нет ACTIVE объявлений — FB не вызывается, updated=0, статусы тоже не трогаются."""
    conn = sqlite3.connect(kb)
    conn.execute("UPDATE creative_kb SET effective_status = 'PAUSED'")
    conn.commit()
    conn.close()

    with patch("services.spend_refresh._fetch_lifetime_insights") as mock_fb, \
         patch("services.spend_refresh.refresh_active_ads_status") as mock_status:
        result = sr.refresh_active_ads_spend_light()
        mock_fb.assert_not_called()
        mock_status.assert_not_called()

    assert result["active_count"] == 0
    assert result["updated"] == 0
    assert result["fetched"] == 0
