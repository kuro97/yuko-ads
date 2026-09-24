import json
from datetime import datetime, timezone

import config
from services.approval_checker_models import EvidenceRequest, SourceSystem, SubjectKind, SubjectRef
from services.approval_source_pending_briefs import load_pending_brief_evidence


def _request(now):
    return EvidenceRequest("r", "REPORT", None, now, (SubjectRef(SubjectKind.BRIEF, "b1"),), (), (SourceSystem.PENDING_BRIEFS,), (), (), (), (), (), (), False, False, 30)


def test_pending_record_is_exact_and_status_drift_blocks(tmp_path, monkeypatch):
    path = tmp_path / "pending_briefs.json"
    record = {"id":"b1","name":"name","desc":"desc","product":"PRODA","signature":"sig","status":"pending","created_at":"2026-07-22T10:00:00+00:00","decided_at":None,"card_id":None,"card_url":None}
    path.write_text(json.dumps({"briefs": [record]}), encoding="utf-8")
    monkeypatch.setattr(config, "REPORT_CHECKER_PENDING_BRIEFS_PATH", path)
    now = datetime(2026, 7, 22, 11, tzinfo=timezone.utc)
    first = load_pending_brief_evidence(_request(now), now)
    record["status"] = "approved"
    path.write_text(json.dumps({"briefs": [record]}), encoding="utf-8")
    second = load_pending_brief_evidence(_request(now), now)
    assert first.complete is True
    assert second.complete is False
    assert second.error_code == "PENDING_BRIEF_NOT_PENDING"


def test_symlink_is_rejected(tmp_path, monkeypatch):
    target = tmp_path / "real.json"
    target.write_text('{"briefs": []}', encoding="utf-8")
    link = tmp_path / "pending.json"
    link.symlink_to(target)
    monkeypatch.setattr(config, "REPORT_CHECKER_PENDING_BRIEFS_PATH", link)
    now = datetime(2026, 7, 22, 11, tzinfo=timezone.utc)
    assert load_pending_brief_evidence(_request(now), now).complete is False
