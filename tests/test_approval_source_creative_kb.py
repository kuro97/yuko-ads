import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import config
from services.approval_checker_models import (
    EvidenceRequest,
    Metric,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
)
from services.approval_source_creative_kb import load_creative_kb_evidence


def test_product_share_and_missing_outcome_not_loser(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE creative_kb(ad_id TEXT PRIMARY KEY,status TEXT,qual_pct REAL,romi REAL,target_product TEXT,synced_at TEXT,outcomes_matched_at TEXT)")
    rows = [
        ("a", "ACTIVE", 20, 100, "PRODA", "2026-07-22T10:00:00+00:00", "2026-07-22T09:00:00+00:00"),
        ("b", "ACTIVE", None, None, "PRODB", "2026-07-22T10:00:00+00:00", None),
        ("c", "ACTIVE", None, None, "", "2026-07-22T10:00:00+00:00", None),
        ("d", "PAUSED", 1, -1, "PRODA", "2026-07-22T10:00:00+00:00", "2026-07-22T09:00:00+00:00"),
        ("e", "ACTIVE", None, None, "PRODD", "2026-07-22T10:00:00+00:00", None),
        ("f", "ACTIVE", None, None, "PRODA", "2026-07-22T10:00:00+00:00", None),
    ]
    connection.executemany("INSERT INTO creative_kb VALUES(?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()
    monkeypatch.setattr(config, "REPORT_CHECKER_DECISIONS_DB_PATH", path)
    now = datetime(2026, 7, 22, 11, tzinfo=timezone.utc)
    window = TimeWindow(now - timedelta(days=7), now, "UTC", "LAST_7D")
    request = EvidenceRequest("r", "REPORT", None, now, (), (), (SourceSystem.CREATIVE_KB,), (window,), (), (), ("a", "b"), (), (), False, False, 300)

    result = load_creative_kb_evidence(request, now)

    assert result.complete is True
    shares = {record.subject.subject_id: record.value for record in result.records if record.metric is Metric.PRODUCT_SHARE_PCT}
    assert shares == {"PRODB": Decimal("33.3"), "PRODA": Decimal("66.7")}
    untagged = next(
        record
        for record in result.records
        if record.subject.subject_id == "UNTAGGED"
    )
    assert untagged.value == 2
    assert "tagged-denominator:3" in untagged.entity_ids
    assert not any(record.subject.subject_id == "b" and record.metric is Metric.ROMI_PCT for record in result.records)


def _accuracy_request(now: datetime) -> EvidenceRequest:
    window = TimeWindow(now - timedelta(days=7), now, "UTC", "LAST_7D")
    subjects = tuple(
        SubjectRef(SubjectKind.CREATIVE, subject_id)
        for subject_id in ("scale-total", "scale-winners", "scale-accuracy")
    )
    return EvidenceRequest(
        "accuracy",
        "REPORT",
        None,
        now,
        subjects,
        (),
        (SourceSystem.CREATIVE_KB,),
        (window,),
        (),
        (),
        (),
        (),
        (),
        False,
        False,
        300,
    )


def test_scale_accuracy_uses_distinct_decisions_and_exact_denominator(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE creative_kb(
            ad_id TEXT PRIMARY KEY,status TEXT,qual_pct REAL,romi REAL,target_product TEXT,
            synced_at TEXT,outcomes_matched_at TEXT
        );
        CREATE TABLE decisions(
            id INTEGER PRIMARY KEY,ad_id TEXT,action TEXT,confirmed_by TEXT,created_at TEXT
        );
        """
    )
    connection.executemany(
        "INSERT INTO creative_kb VALUES(?,?,?,?,?,?,?)",
        [
            ("winner", "ACTIVE", 20, 1, "PRODA", "2026-07-22 10:00:00", "2026-07-22 09:00:00"),
            ("loser", "ACTIVE", 10, 1, "PRODB", "2026-07-22 10:00:00", "2026-07-22 09:00:00"),
            ("loser-2", "ACTIVE", 20, 0, "PRODB", "2026-07-22 10:00:00", "2026-07-22 09:00:00"),
            ("outside", "ACTIVE", 30, 2, "PRODB", "2026-07-22 10:00:00", "2026-07-22 09:00:00"),
        ],
    )
    connection.executemany(
        "INSERT INTO decisions VALUES(?,?,?,?,?)",
        [
            (1, "winner", "SCALED", "autopilot", "2026-07-20 10:00:00"),
            (2, "winner", "scale_more", "autopilot-live", "2026-07-21 10:00:00"),
            (3, "loser", "SCALED", "autopilot", "2026-07-20 11:00:00"),
            (4, "loser-2", "SCALED", "autopilot", "2026-07-21 11:00:00"),
            (5, "outside", "SCALED", "autopilot", "2026-07-15 00:00:00"),
        ],
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(config, "REPORT_CHECKER_DECISIONS_DB_PATH", path)
    now = datetime(2026, 7, 22, 11, tzinfo=timezone.utc)

    request = _accuracy_request(now)
    result = load_creative_kb_evidence(request, now)

    assert result.complete is True
    aggregates = {
        record.subject.subject_id: record
        for record in result.records
        if record.subject.subject_id.startswith("scale-")
    }
    assert aggregates["scale-total"].value == 3
    assert aggregates["scale-winners"].value == 1
    assert aggregates["scale-accuracy"].value == Decimal("33.3")
    assert all(record.window == request.windows[0] for record in aggregates.values())
    assert "denominator-checked:3" in aggregates["scale-accuracy"].entity_ids
    assert "numerator-winners:1" in aggregates["scale-accuracy"].entity_ids


def test_scale_accuracy_missing_outcome_is_incomplete(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE creative_kb(
            ad_id TEXT PRIMARY KEY,status TEXT,qual_pct REAL,romi REAL,target_product TEXT,
            synced_at TEXT,outcomes_matched_at TEXT
        );
        CREATE TABLE decisions(
            id INTEGER PRIMARY KEY,ad_id TEXT,action TEXT,confirmed_by TEXT,created_at TEXT
        );
        INSERT INTO creative_kb VALUES(
            'missing-outcome','ACTIVE',NULL,NULL,'PRODA','2026-07-22 10:00:00',NULL
        );
        INSERT INTO decisions VALUES(
            1,'missing-outcome','SCALED','autopilot','2026-07-20 10:00:00'
        );
        """
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(config, "REPORT_CHECKER_DECISIONS_DB_PATH", path)
    now = datetime(2026, 7, 22, 11, tzinfo=timezone.utc)

    result = load_creative_kb_evidence(_accuracy_request(now), now)

    assert result.complete is False
    assert result.error_code == "CREATIVE_KB_ACCURACY_OUTCOME_MISSING"
