import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import config
from services.approval_checker_models import EvidenceRequest, Metric, SourceSystem, TimeWindow
from services.approval_source_decisions import load_decisions_evidence, load_feedback_evidence


def _request(now: datetime, source: SourceSystem) -> EvidenceRequest:
    window = TimeWindow(now - timedelta(days=7), now, "UTC", "LAST_7D")
    return EvidenceRequest("req", "REPORT", None, now, (), (), (source,), (window,), (), (), (), (), (), False, False, 300)


def _db(path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE decisions(id INTEGER PRIMARY KEY, ad_id TEXT, action TEXT, confirmed_by TEXT, created_at TEXT);
        CREATE TABLE autopilot_feedback(id INTEGER PRIMARY KEY, ad_id TEXT, action TEXT, verdict TEXT, created_at TEXT);
        """
    )
    connection.executemany(
        "INSERT INTO autopilot_feedback VALUES(?,?,?,?,?)",
        [(1, "ad-1", "s", "up", "2026-07-20 10:00:00"), (2, "ad-2", "s", "up", "2026-07-20 11:00:00"), (3, "ad-3", "p", "down", "2026-07-20 12:00:00")],
    )
    connection.execute("INSERT INTO decisions VALUES(1,'ad-1','scale','autopilot','2026-07-20 10:00:00')")
    connection.executemany(
        "INSERT INTO decisions VALUES(?,?,?,?,?)",
        [
            (2, "ad-2", "PAUSED", "autopilot-live", "2026-07-20 11:00:00"),
            (3, "ad-3", "SCALED", "owner", "2026-07-20 12:00:00"),
            (4, "ad-4", "SCALE_BUDGET", "autopilot", "2026-07-21 13:00:00"),
            (5, "ad-5", "PAUSED", "autopilot", "2026-07-22 00:00:00"),
            (6, "ad-6", "PAUSED", "autopilot", "2026-07-15 00:00:00"),
        ],
    )
    connection.commit()
    connection.close()


def test_read_only_and_feedback_agreement(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    _db(path)
    monkeypatch.setattr(config, "REPORT_CHECKER_DECISIONS_DB_PATH", path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    decisions_request = _request(now, SourceSystem.DECISIONS_DB)
    feedback_request = _request(now, SourceSystem.AUTOPILOT_FEEDBACK)
    decisions = load_decisions_evidence(decisions_request, now)
    feedback = load_feedback_evidence(feedback_request, now)

    assert decisions.complete is True
    decision_counts = {
        record.subject.subject_id: record
        for record in decisions.records
        if record.metric is Metric.DECISION_COUNT
        and record.subject.subject_id in {"paused", "scaled"}
    }
    assert {key: record.value for key, record in decision_counts.items()} == {
        "paused": 2,
        "scaled": 2,
    }
    assert all(record.window == decisions_request.windows[0] for record in decision_counts.values())
    assert "decision:5" not in decision_counts["paused"].entity_ids
    assert {"decision:2", "decision:6"}.issubset(decision_counts["paused"].entity_ids)
    agreement = next(record for record in feedback.records if record.metric is Metric.AGREEMENT_PCT)
    assert agreement.value == Decimal("66.7")
    assert agreement.window == feedback_request.windows[0]
    assert "numerator-up:2" in agreement.entity_ids
    assert "denominator-total:3" in agreement.entity_ids
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_missing_table_is_incomplete_without_creation(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr(config, "REPORT_CHECKER_DECISIONS_DB_PATH", path)
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    result = load_feedback_evidence(_request(now, SourceSystem.AUTOPILOT_FEEDBACK), now)

    assert result.complete is False
    assert result.error_code == "SQLITE_TABLE_MISSING"
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    connection.close()
