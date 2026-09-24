"""
Юнит-тесты для services/hourly_collector.py.
Используют временную SQLite-БД (init_kb) и моки FB API (_throttled_get / _fetch_hourly_for_ad).
Сеть не используется.
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

from agent.fb_common import FBApiError
from services.hourly_collector import (
    _TZ_LOCAL,
    _fetch_hourly_for_ad,
    _fetch_recent_ads_from_fb,
    _parse_hourly_rows,
    _prioritize_candidates,
    _select_candidate_ads,
    _select_candidates_from_kb,
    _upsert_hourly,
    collect_hourly_metrics,
    get_hourly_lead_semantics_status,
)

# Фикстура реального формата hourly-строки FB (см. hourly_probe.py).
FB_HOURLY_ROW = {
    "ad_id": "123",
    "date_start": "2026-07-05",
    "date_stop": "2026-07-05",
    "hourly_stats_aggregated_by_advertiser_time_zone": "08:00:00 - 08:59:59",
    "spend": "3.14",
    "impressions": "1200",
    "clicks": "15",
    "actions": [
        {"action_type": "lead", "value": "2"},
        {"action_type": "video_view", "value": "50"},
    ],
}


# ---------------------------------------------------------------------------
# Фикстуры БД
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    """Создаёт временную SQLite БД с таблицами creative_kb + ad_hourly_metrics (миграция 013)."""
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


def _insert_ad(conn: sqlite3.Connection, ad_id: str, created_at: str) -> None:
    """Вставляет минимальную строку creative_kb для тестов кандидатов."""
    conn.execute(
        "INSERT INTO creative_kb (ad_id, ad_name, created_at) VALUES (?, ?, ?)",
        (ad_id, f"name_{ad_id}", created_at),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _stub_fb_candidates():
    """FB — внешняя HTTP-граница: по умолчанию мокаем её недоступной, чтобы тесты
    KB/collect детерминированно и БЫСТРО уходили на фолбэк через creative_kb (без
    реальных сетевых попыток и троттлинга). Патчим атрибут модуля
    services.hourly_collector._fetch_recent_ads_from_fb — тесты FB-пути вызывают
    функцию по её МОДУЛЬНОМУ имени, импортированному на уровне тест-файла (эта
    ссылка патчем не затрагивается), поэтому проверяют реальную реализацию."""
    from services import hourly_collector as hc_mod
    with patch.object(hc_mod, "_fetch_recent_ads_from_fb",
                      side_effect=FBApiError("stub: FB недоступен в тестах", 500)):
        yield


# ---------------------------------------------------------------------------
# Парсинг hourly-строки
# ---------------------------------------------------------------------------

def test_parse_hourly_row_basic():
    """Фикстура реальной FB-строки → dict с ожидаемыми полями."""
    result = _parse_hourly_rows([FB_HOURLY_ROW], ad_id="123")

    assert len(result) == 1
    row = result[0]
    assert row["datetime_hour"] == "2026-07-05T08:00:00"
    assert row["ad_id"] == "123"
    assert row["spend"] == pytest.approx(3.14)
    assert row["impressions"] == 1200
    assert row["clicks"] == 15
    assert row["actions_lead"] == 2
    assert row["video_3s"] == 50


def test_parse_hourly_row_missing_hourly_key():
    """Строка без hourly_stats_... ключа → пропущена."""
    row = {**FB_HOURLY_ROW}
    del row["hourly_stats_aggregated_by_advertiser_time_zone"]

    result = _parse_hourly_rows([row], ad_id="123")

    assert result == []


def test_parse_hourly_row_bad_hour_string():
    """Час не парсится (мусор вместо 'HH:MM:SS - HH:MM:SS') → строка пропущена."""
    row = {**FB_HOURLY_ROW, "hourly_stats_aggregated_by_advertiser_time_zone": "garbage"}

    result = _parse_hourly_rows([row], ad_id="123")

    assert result == []


def test_parse_hourly_row_leads_from_two_action_types():
    """lead — агрегат: grouped-компонент повторно не прибавляется."""
    row = {
        **FB_HOURLY_ROW,
        "actions": [
            {"action_type": "lead", "value": "1"},
            {"action_type": "onsite_conversion.lead_grouped", "value": "1"},
        ],
    }

    result = _parse_hourly_rows([row], ad_id="123")

    assert result[0]["actions_lead"] == 1


def test_parse_hourly_row_no_actions():
    """actions отсутствует → actions_lead=0, video_3s=0, без падения."""
    row = {**FB_HOURLY_ROW}
    del row["actions"]

    result = _parse_hourly_rows([row], ad_id="123")

    assert result[0]["actions_lead"] == 0
    assert result[0]["video_3s"] == 0


def test_parse_hourly_row_null_numeric_fields():
    """spend/impressions/clicks = None → приводятся к 0."""
    row = {**FB_HOURLY_ROW, "spend": None, "impressions": None, "clicks": None}

    result = _parse_hourly_rows([row], ad_id="123")

    assert result[0]["spend"] == 0
    assert result[0]["impressions"] == 0
    assert result[0]["clicks"] == 0


# ---------------------------------------------------------------------------
# UPSERT
# ---------------------------------------------------------------------------

def test_upsert_idempotent(tmp_conn):
    """Дважды UPSERT одной (ad_id, datetime_hour) с разным spend → 1 строка, значение второго вызова."""
    row_first = {
        "ad_id": "ad_1", "datetime_hour": "2026-07-05T08:00:00",
        "spend": 1.0, "impressions": 100, "clicks": 5, "actions_lead": 1, "video_3s": 10,
        "lead_semantics_version": 2, "lead_parse_status": "ok",
    }
    row_second = {**row_first, "spend": 99.0, "actions_lead": 5}

    _upsert_hourly(tmp_conn, [row_first])
    _upsert_hourly(tmp_conn, [row_second])

    rows = tmp_conn.execute(
        "SELECT * FROM ad_hourly_metrics WHERE ad_id=? AND datetime_hour=?",
        ("ad_1", "2026-07-05T08:00:00"),
    ).fetchall()

    assert len(rows) == 1
    assert float(rows[0]["spend"]) == 99.0
    assert int(rows[0]["actions_lead"]) == 5
    assert int(rows[0]["lead_semantics_version"]) == 2
    assert rows[0]["lead_parse_status"] == "ok"


def test_upsert_empty_list_returns_zero(tmp_conn):
    """Пустой список → 0, таблица пуста, без лишнего commit."""
    count = _upsert_hourly(tmp_conn, [])

    assert count == 0
    rows = tmp_conn.execute("SELECT * FROM ad_hourly_metrics").fetchall()
    assert rows == []


# ---------------------------------------------------------------------------
# Кандидаты младше 48ч
# ---------------------------------------------------------------------------

def test_select_candidates_younger_than_48h(tmp_conn):
    """Ad A создан 10ч назад (кандидат), ad B — 5 дней назад (не кандидат)."""
    now = datetime.now(timezone.utc)
    ad_a_created = (now - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    ad_b_created = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S+0000")

    _insert_ad(tmp_conn, "ad_A", ad_a_created)
    _insert_ad(tmp_conn, "ad_B", ad_b_created)

    now_local = now.astimezone(_TZ_LOCAL)
    candidates = _select_candidate_ads(tmp_conn, now_local)

    ad_ids = [c[0] for c in candidates]
    assert "ad_A" in ad_ids
    assert "ad_B" not in ad_ids


def test_select_candidates_skips_missing_created_at(tmp_conn):
    """created_at пустой/NULL → объявление не кандидат, без падения (фолбэк-путь KB)."""
    tmp_conn.execute(
        "INSERT INTO creative_kb (ad_id, ad_name, created_at) VALUES (?, ?, ?)",
        ("ad_no_date", "name", ""),
    )
    tmp_conn.commit()

    now_local = datetime.now(_TZ_LOCAL)
    candidates = _select_candidate_ads(tmp_conn, now_local)

    assert candidates == []


# ---------------------------------------------------------------------------
# Самодостаточный источник кандидатов из FB (created_time) + фолбэк на creative_kb
# ---------------------------------------------------------------------------

def _fb_ads_response(ads: list[dict], after: str | None = None) -> MagicMock:
    """Мок ответа FB /ads: data=ads, paging с next/after если after задан."""
    resp = MagicMock()
    resp.status_code = 200
    paging: dict = {}
    if after:
        paging = {"next": "http://next", "cursors": {"after": after}}
    resp.json.return_value = {"data": ads, "paging": paging}
    return resp


def test_fb_candidates_created_time_filter():
    """FB-мок: объявление 24ч → кандидат, 72ч → отсеивается (локальная перепроверка 48ч)."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    recent = (now_utc - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    old = (now_utc - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    ads = [
        {"id": "ad_recent", "name": "r", "created_time": recent, "effective_status": "ACTIVE"},
        {"id": "ad_old", "name": "o", "created_time": old, "effective_status": "ACTIVE"},
    ]

    with patch("services.hourly_collector._throttled_get", return_value=_fb_ads_response(ads)), \
         patch("services.hourly_collector.get_fb_account_id", return_value="12345"), \
         patch("services.hourly_collector.get_fb_token", return_value="tok"):
        candidates = _fetch_recent_ads_from_fb(now_utc, max_age_hours=48)

    ids = [c[0] for c in candidates]
    assert "ad_recent" in ids
    assert "ad_old" not in ids


def test_fb_candidates_cap_30():
    """40 свежих объявлений на одной странице → потолок 30 кандидатов."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    created = (now_utc - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    ads = [
        {"id": f"ad_{i}", "name": "n", "created_time": created, "effective_status": "ACTIVE"}
        for i in range(40)
    ]

    with patch("services.hourly_collector._throttled_get", return_value=_fb_ads_response(ads)), \
         patch("services.hourly_collector.get_fb_account_id", return_value="12345"), \
         patch("services.hourly_collector.get_fb_token", return_value="tok"):
        candidates = _fetch_recent_ads_from_fb(now_utc, max_age_hours=48)

    assert len(candidates) == 30


def test_fb_candidates_non_200_raises():
    """FB вернул не 200 → FBApiError (верхний уровень уходит на фолбэк)."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    bad = MagicMock()
    bad.status_code = 500
    bad.text = "Server Error"

    with patch("services.hourly_collector._throttled_get", return_value=bad), \
         patch("services.hourly_collector.get_fb_account_id", return_value="12345"), \
         patch("services.hourly_collector.get_fb_token", return_value="tok"):
        with pytest.raises(FBApiError):
            _fetch_recent_ads_from_fb(now_utc, max_age_hours=48)


def test_select_candidate_ads_uses_fb_when_available(tmp_conn):
    """FB-путь доступен → _select_candidate_ads возвращает кандидатов из FB (не KB)."""
    _insert_ad(tmp_conn, "ad_kb_only", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000"))
    now_local = datetime.now(_TZ_LOCAL)

    with patch("services.hourly_collector._fetch_recent_ads_from_fb",
               return_value=[("ad_fb", "2026-07-05")]):
        candidates = _select_candidate_ads(tmp_conn, now_local)

    ids = [c[0] for c in candidates]
    assert ids == ["ad_fb"]  # FB-путь, KB не читается


def test_select_candidate_ads_fallback_to_kb_on_fb_error(tmp_conn):
    """FB-путь упал (FBApiError) → фолбэк на creative_kb (по created_at)."""
    created = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    _insert_ad(tmp_conn, "ad_kb", created)
    now_local = datetime.now(_TZ_LOCAL)

    with patch("services.hourly_collector._fetch_recent_ads_from_fb",
               side_effect=FBApiError("boom", 500)):
        candidates = _select_candidate_ads(tmp_conn, now_local)

    ids = [c[0] for c in candidates]
    assert "ad_kb" in ids


def test_select_candidates_from_kb_direct(tmp_conn):
    """Фолбэк-функция KB напрямую: 10ч → кандидат, 5 дней → нет."""
    now = datetime.now(timezone.utc)
    _insert_ad(tmp_conn, "ad_young", (now - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S+0000"))
    _insert_ad(tmp_conn, "ad_old", (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S+0000"))

    now_utc = now.replace(tzinfo=None)
    candidates = _select_candidates_from_kb(tmp_conn, now_utc)

    ids = [c[0] for c in candidates]
    assert "ad_young" in ids
    assert "ad_old" not in ids


# ---------------------------------------------------------------------------
# Приоритизация
# ---------------------------------------------------------------------------

def test_prioritize_ads_without_data_first(tmp_conn):
    """A есть строки в ad_hourly_metrics, B нет → B раньше A в списке."""
    tmp_conn.execute(
        """INSERT INTO ad_hourly_metrics
           (ad_id, datetime_hour, spend, lead_semantics_version, lead_parse_status)
           VALUES (?, ?, ?, ?, ?)""",
        ("ad_A", "2026-07-05T08:00:00", 1.0, 2, "ok"),
    )
    tmp_conn.commit()

    candidates = [("ad_A", "2026-07-05"), ("ad_B", "2026-07-05")]
    prioritized = _prioritize_candidates(tmp_conn, candidates)

    ad_ids = [c[0] for c in prioritized]
    assert ad_ids.index("ad_B") < ad_ids.index("ad_A")
    # Состав не меняется
    assert set(ad_ids) == {"ad_A", "ad_B"}


def test_hourly_legacy_remediation_is_bounded_and_visible(tmp_db):
    """Свежий legacy refetch становится v2; старый остаётся явным cleaner veto."""
    fixed_now = datetime(2026, 7, 22, 12, 0, tzinfo=_TZ_LOCAL)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO creative_kb (ad_id, ad_name, created_at) VALUES (?, ?, ?)",
        ("ad_recent", "recent", "2026-07-22T06:00:00+0000"),
    )
    conn.execute(
        "INSERT INTO ad_hourly_metrics (ad_id, datetime_hour, actions_lead) VALUES (?, ?, ?)",
        ("ad_recent", "2026-07-21T08:00:00", 9),
    )
    conn.execute(
        "INSERT INTO ad_hourly_metrics (ad_id, datetime_hour, actions_lead) VALUES (?, ?, ?)",
        ("ad_old", "2026-06-01T08:00:00", 9),
    )
    conn.commit()

    before = get_hourly_lead_semantics_status(conn, fixed_now)
    conn.close()

    refreshed_row = {
        **FB_HOURLY_ROW,
        "ad_id": "ad_recent",
        "date_start": "2026-07-21",
        "actions": [{"action_type": "lead", "value": "2"}],
    }
    with patch("services.creative_intelligence.DB_PATH", tmp_db), \
         patch("services.hourly_collector._fetch_hourly_for_ad", return_value=[refreshed_row]):
        result = collect_hourly_metrics(now_local=fixed_now)

    assert before["legacy_rows"] == 2
    assert before["refetchable_48h_rows"] == 1
    assert before["older_fail_closed_rows"] == 1
    assert result["lead_semantics_status"]["legacy_rows"] == 1
    assert result["lead_semantics_status"]["older_fail_closed_rows"] == 1
    assert result["lead_semantics_status"]["blocked_reason"] == (
        "hourly_legacy_outside_48h_requires_explicit_provider_refetch"
    )

    conn = sqlite3.connect(tmp_db)
    rows = conn.execute(
        "SELECT ad_id, actions_lead, lead_semantics_version, lead_parse_status "
        "FROM ad_hourly_metrics ORDER BY ad_id"
    ).fetchall()
    conn.close()
    assert rows == [
        ("ad_old", 9, 1, "legacy"),
        ("ad_recent", 2, 2, "ok"),
    ]


# ---------------------------------------------------------------------------
# Потолок max_ads_per_run
# ---------------------------------------------------------------------------

def test_collect_respects_cap(tmp_db):
    """40 кандидатов, max_ads_per_run=30 → processed_ads<=30, capped=True, ровно 30 fetch-вызовов."""
    conn = sqlite3.connect(tmp_db)
    now = datetime.now(timezone.utc)
    for i in range(40):
        created = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        conn.execute(
            "INSERT INTO creative_kb (ad_id, ad_name, created_at) VALUES (?, ?, ?)",
            (f"ad_{i}", f"name_{i}", created),
        )
    conn.commit()
    conn.close()

    fetch_calls = []

    def _mock_fetch(ad_id, launch_date):
        fetch_calls.append(ad_id)
        return []

    with patch("services.creative_intelligence.DB_PATH", tmp_db), \
         patch("services.hourly_collector._fetch_hourly_for_ad", side_effect=_mock_fetch):
        result = collect_hourly_metrics(max_ads_per_run=30)

    assert result["candidates"] == 40
    assert result["processed_ads"] <= 30
    assert result["capped"] is True
    assert len(fetch_calls) == 30


# ---------------------------------------------------------------------------
# Fail-safe: rate-limit и неожиданная ошибка
# ---------------------------------------------------------------------------

def test_collect_does_not_raise_on_rate_limit(tmp_db):
    """_fetch_hourly_for_ad бросает FBApiError → rate_limited=True, без исключения наружу."""
    conn = sqlite3.connect(tmp_db)
    now = datetime.now(timezone.utc)
    created = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    conn.execute(
        "INSERT INTO creative_kb (ad_id, ad_name, created_at) VALUES (?, ?, ?)",
        ("ad_rl", "name", created),
    )
    conn.commit()
    conn.close()

    with patch("services.creative_intelligence.DB_PATH", tmp_db), \
         patch("services.hourly_collector._fetch_hourly_for_ad",
               side_effect=FBApiError("rate limit", 429)):
        result = collect_hourly_metrics()

    assert result["rate_limited"] is True
    assert result["error"] is None


def test_collect_does_not_raise_on_db_error(tmp_path):
    """_get_connection бросает → error не None, без исключения наружу."""
    with patch("services.hourly_collector._get_connection",
               side_effect=RuntimeError("КБ не инициализирована")):
        result = collect_hourly_metrics()

    assert result["error"] is not None
    assert result["rate_limited"] is False


# ---------------------------------------------------------------------------
# collect_hourly_metrics: кандидатов нет / пустой ответ FB
# ---------------------------------------------------------------------------

def test_collect_no_candidates(tmp_db):
    """Нет кандидатов вообще → candidates=0, processed_ads=0, без ошибок."""
    with patch("services.creative_intelligence.DB_PATH", tmp_db):
        result = collect_hourly_metrics()

    assert result["candidates"] == 0
    assert result["processed_ads"] == 0
    assert result["upserted_rows"] == 0
    assert result["error"] is None
    assert result["rate_limited"] is False


def test_collect_upserts_rows_end_to_end(tmp_db):
    """1 кандидат, мок fetch вернул фикстуру hourly-строки → 1 строка в ad_hourly_metrics."""
    conn = sqlite3.connect(tmp_db)
    now = datetime.now(timezone.utc)
    created = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    conn.execute(
        "INSERT INTO creative_kb (ad_id, ad_name, created_at) VALUES (?, ?, ?)",
        ("123", "name", created),
    )
    conn.commit()
    conn.close()

    with patch("services.creative_intelligence.DB_PATH", tmp_db), \
         patch("services.hourly_collector._fetch_hourly_for_ad", return_value=[FB_HOURLY_ROW]):
        result = collect_hourly_metrics()

    assert result["candidates"] == 1
    assert result["processed_ads"] == 1
    assert result["upserted_rows"] == 1

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT * FROM ad_hourly_metrics WHERE ad_id=?", ("123",)
    ).fetchone()
    conn.close()

    assert row is not None


# ---------------------------------------------------------------------------
# _fetch_hourly_for_ad: breakdowns, не fields
# ---------------------------------------------------------------------------

def test_fetch_hourly_for_ad_uses_breakdowns_not_fields():
    """hourly_stats_... указывается ТОЛЬКО в params['breakdowns'], НЕ в params['fields']."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": [FB_HOURLY_ROW]}

    captured_params = {}

    def _mock_get(url, params=None, **kwargs):
        captured_params.update(params or {})
        return mock_resp

    with patch("services.hourly_collector._throttled_get", side_effect=_mock_get), \
         patch("services.hourly_collector.get_fb_account_id", return_value="12345"), \
         patch("services.hourly_collector.get_fb_token", return_value="fake_token"):
        rows = _fetch_hourly_for_ad("123", "2026-07-05")

    assert captured_params["breakdowns"] == "hourly_stats_aggregated_by_advertiser_time_zone"
    assert "hourly_stats_aggregated_by_advertiser_time_zone" not in captured_params["fields"]
    assert rows == [FB_HOURLY_ROW]


def test_fetch_hourly_for_ad_non_200_returns_empty():
    """FB вернул не 200 (не rate-limit) → [] без падения."""
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request"

    with patch("services.hourly_collector._throttled_get", return_value=mock_resp), \
         patch("services.hourly_collector.get_fb_account_id", return_value="12345"), \
         patch("services.hourly_collector.get_fb_token", return_value="fake_token"):
        rows = _fetch_hourly_for_ad("123", "2026-07-05")

    assert rows == []


# ---------------------------------------------------------------------------
# Крон _cron_hourly_collector (web/app.py, T3 ARCH-hourly-collector §8)
# Паттерн — как у _cron_metrics_snapshot: гейт по часу CityA (02-04) + дедуп
# по дню через backfill_state['hourly_collector'] (_get_state_by_key/_save_state_by_key).
# ---------------------------------------------------------------------------

# Мокаем тяжёлые зависимости ДО импорта web.app (см. шапку файла — уже сделано выше,
# но web.app тянет свою собственную цепочку, импортируем лениво здесь).
from web.app import _cron_hourly_collector, _cron_sync_recent_ads  # noqa: E402
import services.cron_heartbeat as hb  # noqa: E402


def _dt_local(hour: int, minute: int = 10, day: int = 5, month: int = 7, year: int = 2026) -> datetime:
    """Создаёт datetime для указанного часа CityA (для гейта крона)."""
    return datetime(year, month, day, hour, minute, tzinfo=_TZ_LOCAL)


@pytest.fixture
def isolate_heartbeats(tmp_path, monkeypatch):
    """Перенаправляет cron_heartbeats.json на временный файл — изоляция между тестами."""
    hb_file = tmp_path / "cron_heartbeats.json"
    monkeypatch.setattr(hb, "_HB_FILE", hb_file)
    return hb_file


class TestCronHourlyCollectorGate:
    """Гейт по часу CityA (02-04) — независимо от 4 углов диапазона."""

    def test_гейт_час_2_в_окне(self, tmp_db, isolate_heartbeats):
        now = _dt_local(2)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.hourly_collector.collect_hourly_metrics") as mock_collect:
            mock_dt.now.return_value = now
            mock_collect.return_value = {
                "candidates": 0, "processed_ads": 0, "upserted_rows": 0,
                "capped": False, "rate_limited": False, "error": None,
            }
            _cron_hourly_collector()

        mock_collect.assert_called_once()

    def test_гейт_час_3_в_окне(self, tmp_db, isolate_heartbeats):
        now = _dt_local(3)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.hourly_collector.collect_hourly_metrics") as mock_collect:
            mock_dt.now.return_value = now
            mock_collect.return_value = {
                "candidates": 0, "processed_ads": 0, "upserted_rows": 0,
                "capped": False, "rate_limited": False, "error": None,
            }
            _cron_hourly_collector()

        mock_collect.assert_called_once()

    def test_гейт_час_1_вне_окна(self, tmp_db, isolate_heartbeats):
        """01:xx — ДО окна 02-04 → collect не вызывается."""
        now = _dt_local(1)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.hourly_collector.collect_hourly_metrics") as mock_collect:
            mock_dt.now.return_value = now
            _cron_hourly_collector()

        mock_collect.assert_not_called()

    def test_гейт_час_4_вне_окна(self, tmp_db, isolate_heartbeats):
        """04:xx — ПОСЛЕ окна 02-04 (окно = часы 2,3) → collect не вызывается."""
        now = _dt_local(4)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.hourly_collector.collect_hourly_metrics") as mock_collect:
            mock_dt.now.return_value = now
            _cron_hourly_collector()

        mock_collect.assert_not_called()

    def test_гейт_день_12_00_вне_окна(self, tmp_db, isolate_heartbeats):
        """Полдень — далеко вне окна → collect не вызывается."""
        now = _dt_local(12)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.hourly_collector.collect_hourly_metrics") as mock_collect:
            mock_dt.now.return_value = now
            _cron_hourly_collector()

        mock_collect.assert_not_called()


class TestCronHourlyCollectorDedup:
    """Дедупликация по дню через backfill_state['hourly_collector']."""

    def test_первый_тик_в_окне_вызывает_и_помечает_state(self, tmp_db, isolate_heartbeats):
        from services.creative_backfill import _get_state_by_key

        now = _dt_local(2)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.hourly_collector.collect_hourly_metrics") as mock_collect:
            mock_dt.now.return_value = now
            mock_collect.return_value = {
                "candidates": 1, "processed_ads": 1, "upserted_rows": 1,
                "capped": False, "rate_limited": False, "error": None,
            }
            _cron_hourly_collector()

        mock_collect.assert_called_once()
        with patch("services.creative_intelligence.DB_PATH", tmp_db):
            state = _get_state_by_key("hourly_collector")
        assert state["last_window"] == now.date().isoformat()

    def test_повторный_тик_в_том_же_дне_не_дублирует(self, tmp_db, isolate_heartbeats):
        from services.creative_backfill import _save_state_by_key

        now = _dt_local(3, minute=40)
        with patch("services.creative_intelligence.DB_PATH", tmp_db):
            _save_state_by_key("hourly_collector", {"last_window": now.date().isoformat()})

        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.hourly_collector.collect_hourly_metrics") as mock_collect:
            mock_dt.now.return_value = now
            _cron_hourly_collector()

        mock_collect.assert_not_called()

    def test_rate_limited_сбрасывает_пометку(self, tmp_db, isolate_heartbeats):
        """collect вернул rate_limited=True → last_window сброшен в None,
        чтобы следующий тик окна повторил попытку."""
        from services.creative_backfill import _get_state_by_key

        now = _dt_local(2)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.hourly_collector.collect_hourly_metrics") as mock_collect:
            mock_dt.now.return_value = now
            mock_collect.return_value = {
                "candidates": 5, "processed_ads": 2, "upserted_rows": 0,
                "capped": False, "rate_limited": True, "error": None,
            }
            _cron_hourly_collector()

        with patch("services.creative_intelligence.DB_PATH", tmp_db):
            state = _get_state_by_key("hourly_collector")
        assert state["last_window"] is None

    def test_исключение_внутри_крона_не_роняет(self, tmp_db, isolate_heartbeats):
        """collect_hourly_metrics бросил неожиданное исключение → крон ловит,
        не падает наружу (соседние кроны не роняются)."""
        now = _dt_local(2)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.hourly_collector.collect_hourly_metrics",
                   side_effect=RuntimeError("boom")):
            mock_dt.now.return_value = now
            # Не должно бросить исключение
            _cron_hourly_collector()

    def test_heartbeat_пишет_отметку_даже_при_гейт_скипе(self, tmp_db, isolate_heartbeats):
        """@heartbeat('_cron_hourly_collector', 30, critical=False) — успешный
        прогон (даже гейт-скип вне окна, без исключения) пишет отметку."""
        now = _dt_local(12)  # вне окна -> гейт-скип
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.hourly_collector.collect_hourly_metrics") as mock_collect:
            mock_dt.now.return_value = now
            _cron_hourly_collector()

        mock_collect.assert_not_called()
        data = hb.load_heartbeats()
        assert "_cron_hourly_collector" in data["heartbeats"]
        entry = data["heartbeats"]["_cron_hourly_collector"]
        assert entry["expected_minutes"] == 30
        assert entry["critical"] is False


# ---------------------------------------------------------------------------
# Крон _cron_sync_recent_ads (web/app.py) — ежедневный досинк недавних объявлений.
# Гейт по часу CityA (01) + дедуп по дню через backfill_state['sync_recent_ads'].
# ---------------------------------------------------------------------------

class TestCronSyncRecentAdsGate:
    """Гейт по часу CityA: только 01:xx (перед окном сборщика 02-03)."""

    def test_гейт_час_1_в_окне(self, tmp_db, isolate_heartbeats):
        now = _dt_local(1)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.creative_backfill.sync_recent_ads") as mock_sync:
            mock_dt.now.return_value = now
            mock_sync.return_value = {
                "fetched": 3, "upserted": 2, "rate_limited": False, "error": None,
            }
            _cron_sync_recent_ads()

        mock_sync.assert_called_once()
        # days=7 по контракту
        assert mock_sync.call_args.kwargs.get("days", 7) == 7

    def test_гейт_час_0_вне_окна(self, tmp_db, isolate_heartbeats):
        now = _dt_local(0)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.creative_backfill.sync_recent_ads") as mock_sync:
            mock_dt.now.return_value = now
            _cron_sync_recent_ads()

        mock_sync.assert_not_called()

    def test_гейт_час_2_вне_окна_это_окно_сборщика(self, tmp_db, isolate_heartbeats):
        """02:xx — окно почасового сборщика, досинк уже НЕ запускается."""
        now = _dt_local(2)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.creative_backfill.sync_recent_ads") as mock_sync:
            mock_dt.now.return_value = now
            _cron_sync_recent_ads()

        mock_sync.assert_not_called()


class TestCronSyncRecentAdsDedup:
    """Дедупликация по дню через backfill_state['sync_recent_ads']."""

    def test_первый_тик_вызывает_и_помечает_state(self, tmp_db, isolate_heartbeats):
        from services.creative_backfill import _get_state_by_key

        now = _dt_local(1)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.creative_backfill.sync_recent_ads") as mock_sync:
            mock_dt.now.return_value = now
            mock_sync.return_value = {
                "fetched": 1, "upserted": 1, "rate_limited": False, "error": None,
            }
            _cron_sync_recent_ads()

        mock_sync.assert_called_once()
        with patch("services.creative_intelligence.DB_PATH", tmp_db):
            state = _get_state_by_key("sync_recent_ads")
        assert state["last_window"] == now.date().isoformat()

    def test_повторный_тик_в_том_же_дне_не_дублирует(self, tmp_db, isolate_heartbeats):
        from services.creative_backfill import _save_state_by_key

        now = _dt_local(1, minute=40)
        with patch("services.creative_intelligence.DB_PATH", tmp_db):
            _save_state_by_key("sync_recent_ads", {"last_window": now.date().isoformat()})

        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.creative_backfill.sync_recent_ads") as mock_sync:
            mock_dt.now.return_value = now
            _cron_sync_recent_ads()

        mock_sync.assert_not_called()

    def test_rate_limited_сбрасывает_пометку(self, tmp_db, isolate_heartbeats):
        """rate_limited=True → last_window сброшен в None (следующий тик повторит)."""
        from services.creative_backfill import _get_state_by_key

        now = _dt_local(1)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.creative_backfill.sync_recent_ads") as mock_sync:
            mock_dt.now.return_value = now
            mock_sync.return_value = {
                "fetched": 0, "upserted": 0, "rate_limited": True, "error": None,
            }
            _cron_sync_recent_ads()

        with patch("services.creative_intelligence.DB_PATH", tmp_db):
            state = _get_state_by_key("sync_recent_ads")
        assert state["last_window"] is None

    def test_исключение_внутри_крона_не_роняет(self, tmp_db, isolate_heartbeats):
        now = _dt_local(1)
        with patch("web.app.datetime") as mock_dt, \
             patch("services.creative_intelligence.DB_PATH", tmp_db), \
             patch("services.creative_backfill.sync_recent_ads",
                   side_effect=RuntimeError("boom")):
            mock_dt.now.return_value = now
            _cron_sync_recent_ads()  # не должно бросить
