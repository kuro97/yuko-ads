"""
Тесты Стража бюджета 24/7 (services/guardian.py).

Мокаем все внешние границы:
- services.autopilot.get_autopilot_config — kill_switch
- services.shadow_report._fetch_ads_from_local_db — локальная БД (0 FB)
- services.decision_policy.score_and_decide — чистая функция решений
- services.notifications.{send_telegram, send_critical_alert} — Telegram
- _GUARDIAN_STATE — перенаправлен на tmp_path (state-файл guardian_state.json)
- services.creative_intelligence.DB_PATH — для метрики guardian_time_to_pause_stats
  используем временную sqlite-БД с реальной схемой decisions/ad_daily_metrics.

Ничего не бросает наружу, сеть не используется.
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта (чужой незакоммиченный код,
# см. паттерн в tests/test_autopilot.py)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

import services.guardian as guardian_module

_TZ_LOCAL = timezone(timedelta(hours=5))


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_guardian_state(tmp_path, monkeypatch):
    """Перенаправляет _GUARDIAN_STATE на tmp_path — не трогаем реальный data/guardian_state.json."""
    state_path = tmp_path / "guardian_state.json"
    monkeypatch.setattr(guardian_module, "_GUARDIAN_STATE", state_path)
    yield state_path


def _make_ad(ad_id: str = "ad1", name: str = "Тест объявление", spend: float = 50.0,
             leads: int = 3, cpl: float = 15.0, outcomes_matched_at=None,
             days_running: int = 5, city: str = "CityA") -> dict:
    """Минимальное объявление в формате _fetch_ads_from_local_db."""
    return {
        "ad_id": ad_id,
        "ad_name": name,
        "adset_id": "adset1",
        "spend": spend,
        "leads": leads,
        "cpl": cpl,
        "outcomes_matched_at": outcomes_matched_at,
        "days_running": days_running,
        "day_since_launch": days_running,
        "city": city,
        "effective_status": "ACTIVE",
    }


def _make_decision(ad_id: str, action: str = "KEEP", is_early_waster: bool = False,
                    is_wasted_no_crm: bool = False, ad_name: str = "Тест объявление",
                    reasons=None) -> dict:
    """Результат score_and_decide (для мока)."""
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "adset_id": "adset1",
        "action": action,
        "score": 0.5,
        "reasons": reasons or [],
        "is_confirmed_waster": False,
        "is_early_waster": is_early_waster,
        "is_wasted_no_crm": is_wasted_no_crm,
    }


_KILL_SWITCH_OFF_CFG = {"enabled": True, "kill_switch": False, "mode": "dry_run"}
_KILL_SWITCH_ON_CFG = {"enabled": True, "kill_switch": True, "mode": "dry_run"}


# ---------------------------------------------------------------------------
# run_guardian_sweep: kill_switch глушит
# ---------------------------------------------------------------------------

def test_kill_switch_returns_ran_false_and_skipped():
    """kill_switch=True → run_guardian_sweep не трогает ничего, skipped='kill_switch'."""
    with patch("services.autopilot.get_autopilot_config", return_value=_KILL_SWITCH_ON_CFG), \
         patch("services.shadow_report._fetch_ads_from_local_db") as mock_fetch, \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert") as mock_alert:

        result = guardian_module.run_guardian_sweep(trigger="cron")

    assert result["ran"] is False
    assert result["skipped"] == "kill_switch"
    # Данные даже не читались — kill_switch гейт стоит перед fetch
    mock_fetch.assert_not_called()
    mock_tg.assert_not_called()
    mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# run_guardian_sweep: broken-sync guard
# ---------------------------------------------------------------------------

def test_broken_sync_guard_triggers_critical_alert_and_skips_pauses():
    """Все ads с outcomes_matched_at=None → critical alert, skipped='broken_sync', пауз нет."""
    ads = [_make_ad("ad1", outcomes_matched_at=None), _make_ad("ad2", outcomes_matched_at=None)]
    decisions = [_make_decision("ad1"), _make_decision("ad2")]

    with patch("services.autopilot.get_autopilot_config", return_value=_KILL_SWITCH_OFF_CFG), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=ads), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert") as mock_alert:

        result = guardian_module.run_guardian_sweep(trigger="cron")

    assert result["ran"] is True
    assert result["skipped"] == "broken_sync"
    assert result["paused"] == []
    mock_alert.assert_called_once()
    alert_args = mock_alert.call_args.args
    assert "сверка" in alert_args[0].lower() or "сверка" in (alert_args[1] if len(alert_args) > 1 else "")
    # dry_run-сводка не шлётся — broken_sync прерывает раньше
    mock_tg.assert_not_called()


def test_broken_sync_guard_deduplicates_repeat_alert():
    """Повторный broken-sync прогон в пределах кулдауна (6ч) НЕ шлёт алерт повторно."""
    ads = [_make_ad("ad1", outcomes_matched_at=None)]
    decisions = [_make_decision("ad1")]

    recent_alert = (datetime.now(_TZ_LOCAL) - timedelta(hours=1)).isoformat()
    with patch("services.autopilot.get_autopilot_config", return_value=_KILL_SWITCH_OFF_CFG), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=ads), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch.object(guardian_module, "_load_guardian_state",
                       return_value={"last_spend_refresh_at": None, "freshness_alert_at": None,
                                     "broken_sync_alert_at": recent_alert}), \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert") as mock_alert:

        result = guardian_module.run_guardian_sweep(trigger="cron")

    assert result["skipped"] == "broken_sync"
    # Дедуп: алерт был час назад (< 6ч кулдаун) — повторно НЕ шлём
    mock_alert.assert_not_called()
    mock_tg.assert_not_called()


def test_broken_sync_does_not_trigger_when_partially_matched():
    """Если хотя бы у одного ada outcomes_matched_at не None — broken-sync НЕ срабатывает."""
    ads = [
        _make_ad("ad1", outcomes_matched_at=None),
        _make_ad("ad2", outcomes_matched_at="2026-07-01 10:00:00"),
    ]
    decisions = [_make_decision("ad1"), _make_decision("ad2")]

    with patch("services.autopilot.get_autopilot_config", return_value=_KILL_SWITCH_OFF_CFG), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=ads), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert") as mock_alert:

        result = guardian_module.run_guardian_sweep(trigger="cron")

    assert result["ran"] is True
    assert result["skipped"] is None
    mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# run_guardian_sweep: dry_run сводка (нормальные данные, early/wnc кандидаты)
# ---------------------------------------------------------------------------

def test_dry_run_candidates_send_telegram_summary_and_no_pauses():
    """1 early_waster кандидат в dry_run (action не PAUSE) → dry_run_only содержит ad_id,
    send_telegram вызван со сводкой, paused пуст."""
    ads = [
        _make_ad("ad1", outcomes_matched_at="2026-07-01 09:00:00"),
        _make_ad("ad2", outcomes_matched_at="2026-07-01 09:00:00"),
    ]
    decisions = [
        _make_decision("ad1", action="KEEP", is_early_waster=True,
                        reasons=["dry_run: поймал бы ранний слив (день 1-3) — 0 лидов"]),
        _make_decision("ad2", action="KEEP"),
    ]

    with patch("services.autopilot.get_autopilot_config", return_value=_KILL_SWITCH_OFF_CFG), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=ads), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert") as mock_alert, \
         patch("services.action_producer_gateway.propose_pause") as mock_propose:

        # Кандидат Стража теперь превращается в штатную карточку с кнопками
        # (вопрос владельца «почему без кнопки») — мокаем успех продюсера.
        mock_propose.return_value = SimpleNamespace(
            receipt=SimpleNamespace(proposal_id="p1", deduplicated=False)
        )
        result = guardian_module.run_guardian_sweep(trigger="cron")

    assert result["ran"] is True
    assert result["skipped"] is None
    assert "ad1" in result["dry_run_only"]
    assert result["paused"] == []
    assert "ad1" in result["early_candidates"]
    mock_propose.assert_called_once()
    assert mock_propose.call_args.args[0] == "ad1"
    mock_tg.assert_called_once()
    tg_text = mock_tg.call_args.args[0]
    assert "Страж" in tg_text and "отправил 1" in tg_text
    mock_alert.assert_not_called()


def test_candidates_with_action_pause_are_reported_as_paused_not_dry_run():
    """Кандидат, у которого action уже PAUSE (боевой режим — dry_run снят выше),
    попадает в 'paused', а не в 'dry_run_only' — guardian сам не паузит,
    но фиксирует что autopilot_live его подхватит."""
    ads = [_make_ad("ad1", outcomes_matched_at="2026-07-01 09:00:00", spend=200, leads=15, days_running=5)]
    decisions = [_make_decision("ad1", action="PAUSE", is_wasted_no_crm=True)]

    with patch("services.autopilot.get_autopilot_config", return_value=_KILL_SWITCH_OFF_CFG), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=ads), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert"):

        result = guardian_module.run_guardian_sweep(trigger="cron")

    assert "ad1" in result["paused"]
    assert "ad1" not in result["dry_run_only"]
    # Нет dry_run-кандидатов к сводке → send_telegram не вызывается
    mock_tg.assert_not_called()


# ---------------------------------------------------------------------------
# run_guardian_sweep: пустая локальная БД
# ---------------------------------------------------------------------------

def test_empty_local_db_returns_ran_true_zero_analyzed_no_alerts():
    """_fetch_ads_from_local_db вернул [] → ran=True, analyzed=0, без алертов."""
    with patch("services.autopilot.get_autopilot_config", return_value=_KILL_SWITCH_OFF_CFG), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=[]), \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert") as mock_alert:

        result = guardian_module.run_guardian_sweep(trigger="cron")

    assert result["ran"] is True
    assert result["skipped"] is None
    assert result["analyzed"] == 0
    mock_tg.assert_not_called()
    mock_alert.assert_not_called()


def test_run_guardian_sweep_never_raises_on_internal_exception():
    """Исключение внутри (например, score_and_decide упал) → верхний try/except
    ловит, возвращает {'ran': False, 'errors': [...]}, не бросает наружу."""
    ads = [_make_ad("ad1")]

    with patch("services.autopilot.get_autopilot_config", return_value=_KILL_SWITCH_OFF_CFG), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=ads), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.decision_policy.score_and_decide", side_effect=RuntimeError("boom")):

        result = guardian_module.run_guardian_sweep(trigger="cron")

    assert result["ran"] is False
    assert result["errors"] == ["boom"]


# ---------------------------------------------------------------------------
# mark_spend_refresh_ok / spend_refresh_age_hours — сторож свежести
# ---------------------------------------------------------------------------

def test_spend_refresh_age_hours_none_before_first_run():
    """Пустой state (ещё ни одного прогона) → spend_refresh_age_hours возвращает None."""
    assert guardian_module.spend_refresh_age_hours() is None


def test_mark_spend_refresh_ok_writes_timestamp_and_age_is_near_zero():
    """mark_spend_refresh_ok пишет timestamp → spend_refresh_age_hours читает его,
    возраст близок к 0 (только что записан)."""
    now = datetime.now(_TZ_LOCAL)
    guardian_module.mark_spend_refresh_ok(now=now)

    age = guardian_module.spend_refresh_age_hours(now=now)
    assert age is not None
    assert age == pytest.approx(0.0, abs=0.01)


def test_mark_spend_refresh_ok_state_persists_atomically(tmp_path):
    """Состояние пишется атомарно (tmp+rename) — файл существует и парсится как JSON
    после записи, промежуточный .tmp не остаётся."""
    guardian_module.mark_spend_refresh_ok()

    state_path = guardian_module._GUARDIAN_STATE
    assert state_path.exists()
    tmp_file = state_path.with_suffix(".json.tmp")
    assert not tmp_file.exists()

    state = guardian_module._load_guardian_state()
    assert state["last_spend_refresh_at"] is not None


def test_spend_refresh_age_hours_computes_correct_delta():
    """mark_spend_refresh_ok(3ч назад) → spend_refresh_age_hours(сейчас) ~ 3.0."""
    three_hours_ago = datetime.now(_TZ_LOCAL) - timedelta(hours=3)
    guardian_module.mark_spend_refresh_ok(now=three_hours_ago)

    age = guardian_module.spend_refresh_age_hours(now=datetime.now(_TZ_LOCAL))
    assert age == pytest.approx(3.0, abs=0.05)


# ---------------------------------------------------------------------------
# guardian_time_to_pause_stats — на временной sqlite БД
# ---------------------------------------------------------------------------

def _init_tmp_db(db_path: Path) -> None:
    """Создаёт временную БД с реальной схемой decisions + ad_daily_metrics
    (та же DDL, что в agent/database.py и migrations/009_ad_daily_metrics.sql)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id TEXT NOT NULL,
                ad_name TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                confirmed_by TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                spend REAL,
                leads INTEGER,
                cpl REAL,
                ctr REAL, cpm REAL, romi REAL, qual_pct REAL
            );
            CREATE INDEX IF NOT EXISTS idx_decisions_action ON decisions(action);
            CREATE INDEX IF NOT EXISTS idx_decisions_created_at ON decisions(created_at);
            CREATE INDEX IF NOT EXISTS idx_decisions_ad_id ON decisions(ad_id);

            CREATE TABLE IF NOT EXISTS ad_daily_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id TEXT NOT NULL,
                date TEXT NOT NULL,
                spend REAL NOT NULL DEFAULT 0,
                impressions INTEGER NOT NULL DEFAULT 0,
                clicks INTEGER NOT NULL DEFAULT 0,
                ctr REAL NOT NULL DEFAULT 0,
                leads INTEGER NOT NULL DEFAULT 0,
                cpl REAL NOT NULL DEFAULT 0,
                hook_rate REAL, hold_rate REAL,
                video_views_3s INTEGER NOT NULL DEFAULT 0,
                day_since_launch INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(ad_id, date)
            );
            CREATE INDEX IF NOT EXISTS idx_adm_ad_id ON ad_daily_metrics(ad_id);
            CREATE INDEX IF NOT EXISTS idx_adm_date ON ad_daily_metrics(date);
            CREATE INDEX IF NOT EXISTS idx_adm_dsl ON ad_daily_metrics(day_since_launch);
        """)
        conn.commit()
    finally:
        conn.close()


def test_guardian_time_to_pause_stats_empty_db_returns_zero_structure(tmp_path):
    """Пустая БД (нет пауз за 7д) → нулевая структура, не падает."""
    db_path = tmp_path / "decisions_empty.db"
    _init_tmp_db(db_path)

    with patch("services.creative_intelligence.DB_PATH", str(db_path)):
        stats = guardian_module.guardian_time_to_pause_stats(days=7)

    assert stats == {"pauses": 0, "avg_hours_to_pause": None, "saved_usd_week": 0.0, "sample": []}


def test_guardian_time_to_pause_stats_two_pauses_computes_hours_and_saved_usd(tmp_path):
    """2 паузы в decisions (confirmed_by=autopilot/guardian) + ad_daily_metrics,
    из которых можно вычислить момент 'стал сливом' → pauses=2, avg_hours_to_pause>0,
    saved_usd_week = сумма spend запауженных."""
    db_path = tmp_path / "decisions_two.db"
    _init_tmp_db(db_path)

    now = datetime.now(_TZ_LOCAL)
    # Даты вычисляем относительно now (а не хардкодим), чтобы тест не протухал
    # при смене текущей даты — окно теста days=7.
    day_minus_3 = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    day_minus_2 = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    paused_at_ad1 = (now - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    conn = sqlite3.connect(db_path)
    try:
        # ad1: пробивает waster_min_spend=150 в ad_daily_metrics (day_minus_2),
        # запаузен autopilot day_minus_1 00:00:00 → ~24ч до паузы (пробой в 00:00 дня day_minus_2)
        conn.execute(
            "INSERT INTO ad_daily_metrics (ad_id, date, spend, leads, cpl, day_since_launch) "
            "VALUES (?,?,?,?,?,?)",
            ("ad1", day_minus_3, 80.0, 5, 16.0, 3),
        )
        conn.execute(
            "INSERT INTO ad_daily_metrics (ad_id, date, spend, leads, cpl, day_since_launch) "
            "VALUES (?,?,?,?,?,?)",
            ("ad1", day_minus_2, 90.0, 3, 30.0, 4),  # кумулятив 170 >= 150 (порог waster)
        )
        conn.execute(
            "INSERT INTO decisions (ad_id, ad_name, action, reason, confirmed_by, created_at, spend, leads, cpl) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("ad1", "Объявление 1", "PAUSED", "confirmed_waster", "autopilot",
             paused_at_ad1, 170.0, 8, 21.25),
        )

        # ad2: нет строк в ad_daily_metrics → hours_to_pause пропускается,
        # но spend всё равно учитывается в saved_usd_week
        conn.execute(
            "INSERT INTO decisions (ad_id, ad_name, action, reason, confirmed_by, created_at, spend, leads, cpl) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("ad2", "Объявление 2", "PAUSED", "wasted_no_crm", "guardian",
             now.strftime("%Y-%m-%d %H:%M:%S"), 200.0, 12, 16.67),
        )
        conn.commit()
    finally:
        conn.close()

    with patch("services.creative_intelligence.DB_PATH", str(db_path)):
        stats = guardian_module.guardian_time_to_pause_stats(days=7)

    assert stats["pauses"] == 2
    assert stats["avg_hours_to_pause"] is not None
    assert stats["avg_hours_to_pause"] > 0
    # saved_usd_week = 170 + 200 = 370
    assert stats["saved_usd_week"] == pytest.approx(370.0)
    assert len(stats["sample"]) == 2
    ad1_sample = next(s for s in stats["sample"] if s["ad_id"] == "ad1")
    assert ad1_sample["hours_to_pause"] is not None
    ad2_sample = next(s for s in stats["sample"] if s["ad_id"] == "ad2")
    assert ad2_sample["hours_to_pause"] is None
    assert ad2_sample["spend"] == pytest.approx(200.0)


def test_guardian_time_to_pause_stats_ignores_pauses_outside_window(tmp_path):
    """Паузы старше `days` окна не попадают в статистику."""
    db_path = tmp_path / "decisions_old.db"
    _init_tmp_db(db_path)

    old_date = (datetime.now(_TZ_LOCAL) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO decisions (ad_id, ad_name, action, reason, confirmed_by, created_at, spend, leads, cpl) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("ad_old", "Старое объявление", "PAUSED", "тест", "autopilot", old_date, 100.0, 5, 20.0),
        )
        conn.commit()
    finally:
        conn.close()

    with patch("services.creative_intelligence.DB_PATH", str(db_path)):
        stats = guardian_module.guardian_time_to_pause_stats(days=7)

    assert stats["pauses"] == 0
    assert stats["saved_usd_week"] == 0.0


def test_guardian_time_to_pause_stats_ignores_confirmed_by_other_than_autopilot_guardian(tmp_path):
    """Пауза с confirmed_by='user' (ручная) НЕ учитывается — метрика только про Стража/автопилот."""
    db_path = tmp_path / "decisions_manual.db"
    _init_tmp_db(db_path)

    now_str = datetime.now(_TZ_LOCAL).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO decisions (ad_id, ad_name, action, reason, confirmed_by, created_at, spend, leads, cpl) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("ad_manual", "Ручная пауза", "PAUSED", "тест", "user", now_str, 100.0, 5, 20.0),
        )
        conn.commit()
    finally:
        conn.close()

    with patch("services.creative_intelligence.DB_PATH", str(db_path)):
        stats = guardian_module.guardian_time_to_pause_stats(days=7)

    assert stats["pauses"] == 0


def test_guardian_time_to_pause_stats_never_raises_on_db_error(tmp_path):
    """Ошибка чтения БД (например, несуществующий путь) → нулевая структура, не бросает."""
    nonexistent_path = tmp_path / "does_not_exist_dir" / "decisions.db"

    # DB_PATH указывает на путь в несуществующей директории — sqlite3.connect создаст
    # файл в новой директории и упадёт, т.к. директория отсутствует (OperationalError).
    with patch("services.creative_intelligence.DB_PATH", str(nonexistent_path)):
        stats = guardian_module.guardian_time_to_pause_stats(days=7)

    assert stats == {"pauses": 0, "avg_hours_to_pause": None, "saved_usd_week": 0.0, "sample": []}


def test_guardian_time_to_pause_stats_none_db_path_returns_zero_structure():
    """DB_PATH is None (БД ещё не инициализирована) → нулевая структура, не падает."""
    with patch("services.creative_intelligence.DB_PATH", None):
        stats = guardian_module.guardian_time_to_pause_stats(days=7)

    assert stats == {"pauses": 0, "avg_hours_to_pause": None, "saved_usd_week": 0.0, "sample": []}


def test_full_autonomy_suppresses_guardian_cards():
    """Полная автономия пауз: диагностические кандидаты Стража
    карточками владельцу не едут — иначе страж-инвариант считает каждую
    карточку протечкой и алертит каждые 15 минут. Находки остаются в
    результате sweep."""
    ads = [_make_ad("ad1", outcomes_matched_at="2026-07-01 09:00:00")]
    decisions = [
        _make_decision("ad1", action="KEEP", is_early_waster=True,
                        reasons=["dry_run: поймал бы ранний слив (день 1-3) — 0 лидов"]),
    ]
    full_autonomy_cfg = {
        **_KILL_SWITCH_OFF_CFG,
        "autonomous": {"pause_confirmed_wasters": True, "pause_all_candidates": True},
    }

    with patch("services.autopilot.get_autopilot_config", return_value=full_autonomy_cfg), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=ads), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert") as mock_alert, \
         patch("services.action_producer_gateway.propose_pause") as mock_propose:
        result = guardian_module.run_guardian_sweep(trigger="cron")

    assert result["ran"] is True
    mock_propose.assert_not_called()
    assert result["dry_run_only"] == []
    assert result["suppressed_by_autonomy"] == ["ad1"]
    assert "ad1" in result["early_candidates"]
    mock_alert.assert_not_called()


def _init_contour_tables(db_path: Path) -> None:
    """Минимальная схема нового контура одобрений для метрики Стража."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS owner_action_attempts (
                attempt_id TEXT PRIMARY KEY, decision_id TEXT, operation_kind TEXT,
                resource_id TEXT, state TEXT, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS owner_action_decisions (
                decision_id TEXT PRIMARY KEY, decision_source TEXT
            );
            CREATE TABLE IF NOT EXISTS creative_kb (ad_id TEXT PRIMARY KEY, ad_name TEXT,
                spend REAL, leads INTEGER, cpl REAL, qual_pct REAL);
        """)
        conn.commit()
    finally:
        conn.close()


def test_time_to_pause_stats_reads_new_approval_contour(tmp_path):
    """Регрессия: паузы идут через контур одобрений (owner_action_attempts),
    старая таблица decisions пуста — метрика обязана считать по контуру:
    момент паузы = completed_at, остановленный расход = расход за 7 дней до паузы."""
    db_path = tmp_path / "contour.db"
    _init_tmp_db(db_path)
    _init_contour_tables(db_path)
    now = datetime.now(_TZ_LOCAL)
    d3 = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    d2 = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    paused_at = (now - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO ad_daily_metrics (ad_id, date, spend, leads, cpl, day_since_launch) VALUES (?,?,?,?,?,?)",
                     ("adc", d3, 100.0, 4, 25.0, 3))
        conn.execute("INSERT INTO ad_daily_metrics (ad_id, date, spend, leads, cpl, day_since_launch) VALUES (?,?,?,?,?,?)",
                     ("adc", d2, 90.0, 2, 45.0, 4))  # кумулятив 190 >= 150 → пробой в d2
        conn.execute("INSERT INTO owner_action_decisions VALUES (?,?)", ("dec1", "SYSTEM"))
        conn.execute("INSERT INTO owner_action_attempts VALUES (?,?,?,?,?,?)",
                     ("att1", "dec1", "PAUSE_AD", "adc", "CONFIRMED", paused_at))
        conn.execute("INSERT INTO owner_action_attempts VALUES (?,?,?,?,?,?)",
                     ("att2", "dec1", "PAUSE_AD", "adx", "RECONCILE_REQUIRED", paused_at))  # не подтверждена
        conn.execute("INSERT INTO creative_kb VALUES (?,?,?,?,?,?)", ("adc", "CityA | Тест", 190.0, 6, 31.6, 0.0))
        conn.commit()
    finally:
        conn.close()

    with patch("services.creative_intelligence.DB_PATH", str(db_path)):
        stats = guardian_module.guardian_time_to_pause_stats(days=7)

    assert stats["pauses"] == 1
    assert stats["avg_hours_to_pause"] is not None and stats["avg_hours_to_pause"] > 0
    assert stats["saved_usd_week"] == pytest.approx(190.0)  # расход 7 дней до паузы
    assert stats["sample"][0]["ad_name"] == "CityA | Тест"
