"""Мультикабинетный почасовой сборщик (cabinet_a + cabinet_b).

L2-адсеты части городов живут в кабинете «ACME cabinet_b»
(ACCOUNT_CABINET_B) — discovery кандидатов обязан обходить каждый кабинет
из accounts_to_scan() через fb_account(offline_account_context(...)), а сами
hourly insights идут на узел объявления {ad_id}/insights и кабинета не требуют.
Образец — tests/test_cohort_multi_account.py.
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта web-модулей (известная проблема WIP, паттерн
# test_metrics_snapshot.py) — creative_intelligence тянет цепочку импортов.
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())

from agent.fb_common import API, FBApiError
from config import FB_ACCOUNT_ID
from services import hourly_collector
from services.fb_token_provider import get_fb_account_id
from services.hourly_collector import (
    _TZ_LOCAL,
    _fetch_hourly_for_ad,
    _fetch_recent_ads_all_accounts,
    _select_candidate_ads,
    collect_hourly_metrics,
)
from services.launch_routing import ACCOUNT_CABINET_B

CABINET_A = str(FB_ACCOUNT_ID).removeprefix("act_")
CABINET_B = ACCOUNT_CABINET_B


@pytest.fixture
def tmp_db(tmp_path):
    """Временная SQLite БД с таблицами creative_kb + ad_hourly_metrics."""
    db_path = str(tmp_path / "test.db")

    from services.creative_intelligence import init_kb
    init_kb(db_path=db_path)

    return db_path


@pytest.fixture
def tmp_conn(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def route_map_two_accounts(monkeypatch):
    """Карта роутинга: CityA в cabinet_a, CityF в cabinet_b.

    Патчим get_route_table (единственный источник карты — пары
    (город, тип)): от него зависят и accounts_to_scan (порядок обхода), и
    реестр offline_account_context (fail-closed регистрация кабинета) —
    settings.json не влияет на тест.
    """
    monkeypatch.setattr(
        "services.launch_routing.get_route_table",
        lambda: {
            ("CityA", "L2"): CABINET_A,
            ("CityA", "L1"): CABINET_A,
            ("CityF", "L2"): CABINET_B,
            ("CityF", "L1"): CABINET_B,
        },
    )


def _fake_fetch_by_account(rows_by_account: dict, calls: list[str]):
    """Фейк _fetch_recent_ads_from_fb: отдаёт кандидатов активного кабинета."""

    def fake_fetch(now_utc, max_age_hours=48, max_candidates=30):
        account = get_fb_account_id()
        calls.append(account)
        rows = rows_by_account[account]
        if isinstance(rows, Exception):
            raise rows
        return rows

    return fake_fetch


_NOW_UTC = datetime(2026, 8, 18, 10, 0)


# ---------------------------------------------------------------------------
# Discovery кандидатов обходит все кабинеты карты
# ---------------------------------------------------------------------------

def test_candidates_merge_both_accounts(monkeypatch, route_map_two_accounts):
    """Кандидаты cabinet_a и cabinet_b сливаются в один список, обход — оба кабинета."""
    calls: list[str] = []
    monkeypatch.setattr(
        hourly_collector, "_fetch_recent_ads_from_fb",
        _fake_fetch_by_account({
            CABINET_A: [("525_1", "2026-08-18")],
            CABINET_B: [("120_1", "2026-08-18")],
        }, calls),
    )

    candidates = _fetch_recent_ads_all_accounts(_NOW_UTC)

    assert calls == [CABINET_A, CABINET_B], "discovery ставится на оба кабинета"
    assert candidates == [("525_1", "2026-08-18"), ("120_1", "2026-08-18")]


def test_candidates_partial_failure_keeps_healthy_account(monkeypatch, route_map_two_accounts):
    """Отказ cabinet_b не гасит cabinet_a: сборщик fail-safe, недобор — не искажение."""
    calls: list[str] = []
    monkeypatch.setattr(
        hourly_collector, "_fetch_recent_ads_from_fb",
        _fake_fetch_by_account({
            CABINET_A: [("525_1", "2026-08-18")],
            CABINET_B: FBApiError("cabinet_b недоступен", 500),
        }, calls),
    )

    candidates = _fetch_recent_ads_all_accounts(_NOW_UTC)

    assert calls == [CABINET_A, CABINET_B]
    assert candidates == [("525_1", "2026-08-18")]


def test_candidates_all_accounts_fail_raises(monkeypatch, route_map_two_accounts):
    """Упали ВСЕ кабинеты → FBApiError (верхний уровень уходит на KB-фолбэк)."""
    monkeypatch.setattr(
        hourly_collector, "_fetch_recent_ads_from_fb",
        _fake_fetch_by_account({
            CABINET_A: FBApiError("cabinet_a недоступен", 500),
            CABINET_B: FBApiError("cabinet_b недоступен", 500),
        }, []),
    )

    with pytest.raises(FBApiError):
        _fetch_recent_ads_all_accounts(_NOW_UTC)


def test_candidates_cross_account_dupe_skipped(monkeypatch, route_map_two_accounts):
    """Дубль ad_id из второго кабинета — аномалия, владельцем остаётся первый."""
    monkeypatch.setattr(
        hourly_collector, "_fetch_recent_ads_from_fb",
        _fake_fetch_by_account({
            CABINET_A: [("525_1", "2026-08-18")],
            CABINET_B: [("525_1", "2026-08-17"), ("120_1", "2026-08-18")],
        }, []),
    )

    candidates = _fetch_recent_ads_all_accounts(_NOW_UTC)

    assert candidates == [("525_1", "2026-08-18"), ("120_1", "2026-08-18")]


def test_candidates_single_account_map_unchanged(monkeypatch):
    """Карта без cabinet_b (все города в cabinet_a) — один обход, без регрессии."""
    monkeypatch.setattr(
        "services.launch_routing.get_route_table",
        lambda: {("CityA", "L2"): CABINET_A, ("CityB", "L1"): CABINET_A},
    )
    calls: list[str] = []
    monkeypatch.setattr(
        hourly_collector, "_fetch_recent_ads_from_fb",
        _fake_fetch_by_account({CABINET_A: [("525_1", "2026-08-18")]}, calls),
    )

    candidates = _fetch_recent_ads_all_accounts(_NOW_UTC)

    assert calls == [CABINET_A]
    assert candidates == [("525_1", "2026-08-18")]


def test_select_candidates_kb_fallback_when_all_accounts_fail(
    monkeypatch, route_map_two_accounts, tmp_conn
):
    """Все кабинеты легли → _select_candidate_ads уходит на creative_kb."""
    created = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime(
        "%Y-%m-%dT%H:%M:%S+0000"
    )
    tmp_conn.execute(
        "INSERT INTO creative_kb (ad_id, ad_name, created_at) VALUES (?, ?, ?)",
        ("ad_kb", "name", created),
    )
    tmp_conn.commit()
    monkeypatch.setattr(
        hourly_collector, "_fetch_recent_ads_from_fb",
        _fake_fetch_by_account({
            CABINET_A: FBApiError("cabinet_a недоступен", 500),
            CABINET_B: FBApiError("cabinet_b недоступен", 500),
        }, []),
    )

    candidates = _select_candidate_ads(tmp_conn, datetime.now(_TZ_LOCAL))

    assert [c[0] for c in candidates] == ["ad_kb"]


# ---------------------------------------------------------------------------
# Hourly insights — узел объявления, кабинет не нужен
# ---------------------------------------------------------------------------

def test_fetch_hourly_uses_ad_node_without_account():
    """Запрос идёт на {ad_id}/insights (не act_...), кабинет НЕ резолвится."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"data": []}

    captured: dict = {}

    def _mock_get(url, params=None, **kwargs):
        captured["url"] = url
        captured["params"] = params or {}
        return resp

    with patch("services.hourly_collector._throttled_get", side_effect=_mock_get), \
         patch("services.hourly_collector.get_fb_token", return_value="tok"), \
         patch("services.hourly_collector.get_fb_account_id",
               side_effect=RuntimeError("кабинет не должен резолвиться")):
        rows = _fetch_hourly_for_ad("120_1", "2026-08-18")

    assert rows == []
    assert captured["url"] == f"{API}/120_1/insights"
    assert "act_" not in captured["url"]
    assert "filtering" not in captured["params"]


# ---------------------------------------------------------------------------
# Сквозной прогон: объявления обоих кабинетов доезжают до ad_hourly_metrics
# ---------------------------------------------------------------------------

def _hourly_row(ad_id: str) -> dict:
    """Строка реального формата hourly-ответа FB для данного ad_id."""
    return {
        "ad_id": ad_id,
        "date_start": "2026-08-18",
        "date_stop": "2026-08-18",
        "hourly_stats_aggregated_by_advertiser_time_zone": "08:00:00 - 08:59:59",
        "spend": "3.14",
        "impressions": "1200",
        "clicks": "15",
        "actions": [{"action_type": "lead", "value": "2"}],
    }


def test_collect_upserts_rows_from_both_accounts(monkeypatch, route_map_two_accounts, tmp_db):
    """collect_hourly_metrics пишет hourly-строки объявлений обоих кабинетов."""
    monkeypatch.setattr(
        hourly_collector, "_fetch_recent_ads_from_fb",
        _fake_fetch_by_account({
            CABINET_A: [("525_1", "2026-08-18")],
            CABINET_B: [("120_1", "2026-08-18")],
        }, []),
    )
    fetched_ads: list[str] = []

    def fake_hourly(ad_id, launch_date):
        fetched_ads.append(ad_id)
        return [_hourly_row(ad_id)]

    with patch("services.creative_intelligence.DB_PATH", tmp_db), \
         patch("services.hourly_collector._fetch_hourly_for_ad", side_effect=fake_hourly):
        result = collect_hourly_metrics()

    assert result["candidates"] == 2
    assert result["processed_ads"] == 2
    assert result["upserted_rows"] == 2
    assert set(fetched_ads) == {"525_1", "120_1"}

    conn = sqlite3.connect(tmp_db)
    ad_ids = {
        row[0]
        for row in conn.execute("SELECT ad_id FROM ad_hourly_metrics").fetchall()
    }
    conn.close()
    assert ad_ids == {"525_1", "120_1"}
