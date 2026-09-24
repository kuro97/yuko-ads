"""Тесты двух health-детекторов: CPL и серия FB-ошибок."""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_TZ = timezone(timedelta(hours=5))
_DEFAULT_CFG = {
    "enabled": True,
    "cpl_mult": 3.0,
    "cpl_min_spend": 20.0,
    # Legacy spend keys могут приходить из settings, но этот модуль их игнорирует.
    "spend_spike_mult": 1.5,
    "spend_spike_min_elapsed_hours": 12,
    "fb_error_burst_threshold": 5,
    "dedup_hours": 6,
}


def _cfg(**overrides) -> dict:
    cfg = dict(_DEFAULT_CFG)
    cfg.update(overrides)
    return cfg


@pytest.fixture
def isolate_state(tmp_path, monkeypatch):
    """Перенаправляет state дедупликации во временный каталог."""
    import services.anomaly_alerts as anomaly_alerts

    state_path = tmp_path / "anomaly_alerts_state.json"
    monkeypatch.setattr(anomaly_alerts, "_STATE_FILE", state_path)
    return state_path


@pytest.fixture(autouse=True)
def isolate_anomaly_config():
    """Тесты не читают реальные настройки autopilot."""
    with patch("services.anomaly_alerts._get_config", return_value=_cfg()):
        yield


@pytest.fixture(autouse=True)
def isolate_creative_intelligence_import(monkeypatch):
    """Подставляет только DB_PATH без side effects KB-модуля."""
    import services

    module = ModuleType("services.creative_intelligence")
    module.DB_PATH = None
    monkeypatch.setitem(sys.modules, "services.creative_intelligence", module)
    monkeypatch.setattr(services, "creative_intelligence", module, raising=False)
    yield module


@pytest.fixture(autouse=True)
def isolate_fb_common_import(monkeypatch):
    """Подставляет in-memory счётчик FB-ошибок без сетевых импортов."""
    import agent

    module = ModuleType("agent.fb_common")
    module.fb_error_count_last_hour = MagicMock(return_value=0)
    monkeypatch.setitem(sys.modules, "agent.fb_common", module)
    monkeypatch.setattr(agent, "fb_common", module, raising=False)
    yield module


@pytest.fixture
def sqlite_db(tmp_path):
    """Минимальная временная БД для CPL-детектора."""
    db_path = tmp_path / "creative_kb.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE ad_daily_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id TEXT NOT NULL,
            date TEXT NOT NULL,
            spend REAL NOT NULL DEFAULT 0,
            leads INTEGER NOT NULL DEFAULT 0,
            lead_semantics_version INTEGER NOT NULL DEFAULT 1,
            lead_parse_status TEXT NOT NULL DEFAULT 'legacy'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE creative_kb (
            ad_id TEXT PRIMARY KEY,
            city TEXT DEFAULT ''
        )
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_metrics(
    db_path: Path,
    ad_id: str,
    city: str,
    rows: list[tuple[str, float, int]],
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO creative_kb (ad_id, city) VALUES (?, ?)",
        (ad_id, city),
    )
    for date_str, spend, leads in rows:
        conn.execute(
            "INSERT INTO ad_daily_metrics "
            "(ad_id, date, spend, leads, lead_semantics_version, lead_parse_status) "
            "VALUES (?, ?, ?, ?, 2, 'ok')",
            (ad_id, date_str, spend, leads),
        )
    conn.commit()
    conn.close()


def _dstr(now: datetime, days_ago: int) -> str:
    return (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")


class TestDetectCplSpikeByCity:
    """CPL сравнивается с медианой города и фильтруется по качеству данных."""

    def test_cpl_spike_caught(self, sqlite_db):
        import services.anomaly_alerts as anomaly_alerts

        now = datetime.now(_TZ)
        history = [(_dstr(now, day), 100.0, 10) for day in range(2, 9)]
        _insert_metrics(sqlite_db, "ad1", "CityA", history)
        _insert_metrics(sqlite_db, "ad1", "CityA", [(_dstr(now, 1), 350.0, 1)])

        with patch("services.creative_intelligence.DB_PATH", str(sqlite_db)):
            alerts = anomaly_alerts.detect_cpl_spike_by_city(now)

        assert len(alerts) == 1
        assert "CityA" in alerts[0]

    def test_legacy_rows_are_excluded(self, sqlite_db):
        import services.anomaly_alerts as anomaly_alerts

        now = datetime.now(_TZ)
        conn = sqlite3.connect(sqlite_db)
        conn.execute("INSERT INTO creative_kb (ad_id, city) VALUES ('legacy', 'CityA')")
        for day in range(1, 9):
            conn.execute(
                "INSERT INTO ad_daily_metrics (ad_id, date, spend, leads) VALUES (?, ?, ?, ?)",
                ("legacy", _dstr(now, day), 500.0, 1),
            )
        conn.commit()
        conn.close()

        with patch("services.creative_intelligence.DB_PATH", str(sqlite_db)):
            alerts = anomaly_alerts.detect_cpl_spike_by_city(now)

        assert alerts == []

    def test_clean_data_no_alert(self, sqlite_db):
        import services.anomaly_alerts as anomaly_alerts

        now = datetime.now(_TZ)
        rows = [(_dstr(now, day), 100.0, 10) for day in range(1, 9)]
        _insert_metrics(sqlite_db, "ad1", "CityA", rows)

        with patch("services.creative_intelligence.DB_PATH", str(sqlite_db)):
            assert anomaly_alerts.detect_cpl_spike_by_city(now) == []

    def test_less_than_three_valid_history_days_no_alert(self, sqlite_db):
        import services.anomaly_alerts as anomaly_alerts

        now = datetime.now(_TZ)
        _insert_metrics(
            sqlite_db,
            "ad1",
            "CityA",
            [(_dstr(now, day), 100.0, 10) for day in range(2, 4)],
        )
        _insert_metrics(sqlite_db, "ad1", "CityA", [(_dstr(now, 1), 500.0, 1)])

        with patch("services.creative_intelligence.DB_PATH", str(sqlite_db)):
            assert anomaly_alerts.detect_cpl_spike_by_city(now) == []

    def test_low_spend_below_threshold_no_alert(self, sqlite_db):
        import services.anomaly_alerts as anomaly_alerts

        now = datetime.now(_TZ)
        history = [(_dstr(now, day), 10.0, 10) for day in range(2, 9)]
        _insert_metrics(sqlite_db, "ad1", "CityA", history)
        _insert_metrics(sqlite_db, "ad1", "CityA", [(_dstr(now, 1), 5.0, 1)])

        with patch("services.creative_intelligence.DB_PATH", str(sqlite_db)):
            assert anomaly_alerts.detect_cpl_spike_by_city(now) == []

    def test_database_unavailable_returns_empty(self):
        import services.anomaly_alerts as anomaly_alerts

        with patch("services.creative_intelligence.DB_PATH", None):
            assert anomaly_alerts.detect_cpl_spike_by_city(datetime.now(_TZ)) == []


class TestDetectFbErrorBurst:
    """Серия FB-ошибок срабатывает на фиксированной границе."""

    @pytest.mark.parametrize(
        ("count", "expected_count"),
        [(4, 0), (5, 1), (6, 1)],
    )
    def test_threshold(self, count, expected_count):
        import services.anomaly_alerts as anomaly_alerts

        with patch("agent.fb_common.fb_error_count_last_hour", return_value=count):
            alerts = anomaly_alerts.detect_fb_error_burst(datetime.now(_TZ))

        assert len(alerts) == expected_count
        if alerts:
            assert str(count) in alerts[0]


class TestRunAnomalyAlerts:
    """Runner сохраняет health routing, дедуп, retry и never-throw."""

    def test_cpl_and_fb_error_stay_in_health(self, isolate_state):
        import services.anomaly_alerts as anomaly_alerts

        with patch.object(
            anomaly_alerts,
            "detect_cpl_spike_by_city",
            return_value=["CPL CityA 4.0× медианы"],
        ) as cpl_detector, patch.object(
            anomaly_alerts,
            "detect_fb_error_burst",
            return_value=["Серия FB-ошибок: 7 за час"],
        ) as fb_detector, patch(
            "services.notifications.send_telegram", return_value=True
        ) as telegram:
            result = anomaly_alerts.run_anomaly_alerts(datetime.now(_TZ))

        assert result == {"alerts_sent": 2, "alerts_skipped": 0}
        cpl_detector.assert_called_once()
        fb_detector.assert_called_once()
        assert [call.kwargs["channel"] for call in telegram.call_args_list] == [
            "health",
            "health",
        ]
        assert set(json.loads(isolate_state.read_text())["alerts"]) == {
            "cpl_spike:CityA",
            "fb_error_burst",
        }

    def test_dedup_second_run_skipped(self, isolate_state):
        import services.anomaly_alerts as anomaly_alerts

        now = datetime.now(_TZ)
        with patch.object(
            anomaly_alerts,
            "detect_cpl_spike_by_city",
            return_value=["CPL CityA 3.5× медианы"],
        ), patch.object(
            anomaly_alerts, "detect_fb_error_burst", return_value=[]
        ), patch("services.notifications.send_telegram", return_value=True) as telegram:
            first = anomaly_alerts.run_anomaly_alerts(now)
            second = anomaly_alerts.run_anomaly_alerts(now + timedelta(minutes=5))

        assert first == {"alerts_sent": 1, "alerts_skipped": 0}
        assert second == {"alerts_sent": 0, "alerts_skipped": 1}
        telegram.assert_called_once()

    @pytest.mark.parametrize(
        "first_outcome",
        [False, RuntimeError("Telegram недоступен")],
        ids=["false", "exception"],
    )
    def test_telegram_failure_is_retried_without_dedup(
        self,
        isolate_state,
        first_outcome,
    ):
        import services.anomaly_alerts as anomaly_alerts

        now = datetime.now(_TZ)
        with patch.object(
            anomaly_alerts,
            "detect_cpl_spike_by_city",
            return_value=["CPL CityA 3.5× медианы"],
        ), patch.object(
            anomaly_alerts, "detect_fb_error_burst", return_value=[]
        ), patch(
            "services.notifications.send_telegram",
            side_effect=[first_outcome, True],
        ) as telegram:
            first = anomaly_alerts.run_anomaly_alerts(now)
            second = anomaly_alerts.run_anomaly_alerts(now + timedelta(minutes=5))

        assert first == {"alerts_sent": 0, "alerts_skipped": 1}
        assert second == {"alerts_sent": 1, "alerts_skipped": 0}
        assert telegram.call_count == 2
        assert set(json.loads(isolate_state.read_text())["alerts"]) == {
            "cpl_spike:CityA"
        }

    def test_enabled_false_is_noop(self):
        import services.anomaly_alerts as anomaly_alerts

        with patch(
            "services.anomaly_alerts._get_config",
            return_value=_cfg(enabled=False),
        ), patch.object(
            anomaly_alerts, "detect_cpl_spike_by_city"
        ) as cpl_detector, patch.object(
            anomaly_alerts, "detect_fb_error_burst"
        ) as fb_detector, patch("services.notifications.send_telegram") as telegram:
            result = anomaly_alerts.run_anomaly_alerts(datetime.now(_TZ))

        assert result == {"alerts_sent": 0, "alerts_skipped": 0}
        cpl_detector.assert_not_called()
        fb_detector.assert_not_called()
        telegram.assert_not_called()

    def test_database_unavailable_end_to_end_is_quiet(self):
        import services.anomaly_alerts as anomaly_alerts

        with patch("services.creative_intelligence.DB_PATH", None), patch(
            "agent.fb_common.fb_error_count_last_hour", return_value=0
        ), patch("services.notifications.send_telegram") as telegram:
            result = anomaly_alerts.run_anomaly_alerts(datetime.now(_TZ))

        assert result == {"alerts_sent": 0, "alerts_skipped": 0}
        telegram.assert_not_called()


def test_legacy_spend_detector_and_watchdog_import_are_gone():
    """Anomaly runner больше не производит spend и не зависит от ads_watchdog."""
    source_path = Path(__file__).parent.parent / "services" / "anomaly_alerts.py"
    source = source_path.read_text(encoding="utf-8")

    assert "def detect_spend_spike" not in source
    assert "services.ads_watchdog" not in source
    assert "spend_spike_mult" not in source
    assert "spend_spike_min_elapsed_hours" not in source
