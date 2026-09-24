import sqlite3
from datetime import datetime, timedelta, timezone

import config
from services.approval_checker_models import EvidenceRequest, Metric, SourceSystem, TimeWindow
from services.approval_source_learning import load_hypothesis_evidence, load_pattern_evidence


def _request(now, source):
    window = TimeWindow(now - timedelta(days=7), now, "UTC", "LAST_7D")
    return EvidenceRequest("r", "REPORT", None, now, (), (), (source,), (window,), (), (), (), (), (), False, False, 300)


def test_malformed_learning_json_is_incomplete(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE learnings(id INTEGER PRIMARY KEY,statement TEXT,evidence_ad_ids TEXT,confidence TEXT,source TEXT,tags TEXT,created_at TEXT,updated_at TEXT)")
    connection.execute("INSERT INTO learnings VALUES(1,'x','{','probable','manual','tag','2026-07-20 10:00:00','2026-07-20 10:00:00')")
    connection.commit()
    connection.close()
    monkeypatch.setattr(config, "REPORT_CHECKER_DECISIONS_DB_PATH", path)
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    assert load_pattern_evidence(_request(now, SourceSystem.PATTERN_LEARNINGS), now).complete is False


def test_learning_totals_confidence_and_exact_hashes_use_half_open_window(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE learnings(id INTEGER PRIMARY KEY,statement TEXT,evidence_ad_ids TEXT,confidence TEXT,source TEXT,tags TEXT,created_at TEXT,updated_at TEXT)")
    connection.executemany(
        "INSERT INTO learnings VALUES(?,?,?,?,?,?,?,?)",
        [
            (1, "first", '["ad-1"]', "confirmed", "pattern_miner", "hook:a", "2026-07-15 00:00:00", "2026-07-21 10:00:00"),
            (2, "second", '["ad-2"]', "probable", "hypothesis", "city:x", "2026-07-20 10:00:00", "2026-07-21 11:00:00"),
            (3, "excluded", '["ad-3"]', "hypothesis", "pattern_engine", "metric:cpl", "2026-07-22 00:00:00", "2026-07-22 00:00:00"),
        ],
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(config, "REPORT_CHECKER_DECISIONS_DB_PATH", path)
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)

    request = _request(now, SourceSystem.PATTERN_LEARNINGS)
    result = load_pattern_evidence(request, now)

    assert result.complete is True
    counts = {
        record.subject.subject_id: record.value
        for record in result.records
        if record.metric is Metric.PATTERN_COUNT
    }
    assert counts == {
        "confidence:confirmed": 1,
        "confidence:hypothesis": 0,
        "confidence:probable": 1,
        "total": 2,
    }
    assert all(
        record.window == request.windows[0]
        for record in result.records
        if record.metric is Metric.PATTERN_COUNT
    )
    rows = [record for record in result.records if record.metric is Metric.RECORD_STATUS]
    assert {record.subject.subject_id for record in rows} == {"1", "2"}
    assert all(any(item.startswith("content-sha256:") for item in record.entity_ids) for record in rows)


def test_hypothesis_uses_verdict_window(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE hypotheses(id INTEGER PRIMARY KEY,ad_ids TEXT,expectation_json TEXT,status TEXT,verdict_at TEXT,facts_json TEXT,created_at TEXT)")
    connection.executemany(
        "INSERT INTO hypotheses VALUES(?,?,?,?,?,?,?)",
        [
            (1, '["ad-1"]', "{}", "confirmed", "2026-07-20 10:00:00", "{}", "2026-01-01 00:00:00"),
            (2, '["ad-2"]', "{}", "open", None, None, "2026-07-15 00:00:00"),
            (3, '["ad-3"]', "{}", "refuted", "2026-07-22 00:00:00", "{}", "2026-01-01 00:00:00"),
            (4, '["ad-4"]', "{}", "inconclusive", "2026-07-21 10:00:00", "{}", "2026-01-01 00:00:00"),
        ],
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(config, "REPORT_CHECKER_DECISIONS_DB_PATH", path)
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    result = load_hypothesis_evidence(_request(now, SourceSystem.HYPOTHESIS_JOURNAL), now)
    assert result.complete is True
    assert any(record.subject.subject_id == "1" for record in result.records)
    counts = {
        record.subject.subject_id: record.value
        for record in result.records
        if record.metric is Metric.HYPOTHESIS_COUNT
    }
    assert counts == {
        "status:confirmed": 1,
        "status:inconclusive": 1,
        "status:open": 1,
        "status:refuted": 0,
        "total": 3,
    }
    assert not any(record.subject.subject_id == "3" for record in result.records)
