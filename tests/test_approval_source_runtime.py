from datetime import datetime, timezone

from agent import fb_common
from services import notifications
from services.approval_checker_models import EvidenceRequest, SourceSystem, SubjectKind, SubjectRef
from services.approval_source_runtime import load_fb_error_ring_evidence, load_runtime_evidence


def _request(now, source, subject):
    return EvidenceRequest("r", "REPORT", None, now, (subject,), (), (source,), (), (), (), (), (), (), False, False, 5)


def test_fb_ring_requires_same_instance(monkeypatch):
    monkeypatch.setattr(fb_common, "_FB_ERROR_TIMES", fb_common.deque(maxlen=200))
    now = datetime(2026, 7, 22, 11, tzinfo=timezone.utc)
    snapshot = fb_common.read_fb_error_snapshot(now.timestamp())
    good = SubjectRef(SubjectKind.RUNTIME, f"{snapshot.source_instance_id}:fb-errors")
    bad = SubjectRef(SubjectKind.RUNTIME, "another-instance:fb-errors")
    assert load_fb_error_ring_evidence(_request(now, SourceSystem.FB_ERROR_RING, good), now).complete is True
    assert load_fb_error_ring_evidence(_request(now, SourceSystem.FB_ERROR_RING, bad), now).complete is False


def test_notification_snapshot_is_aggregate_only():
    notifications.clear_events()
    notifications.add_event("alert", "secret title", "secret detail", meta={"ad_id": "secret"})
    now = datetime.now(timezone.utc)
    snapshot = notifications.read_notification_runtime_snapshot(now)
    subject = SubjectRef(SubjectKind.RUNTIME, f"{snapshot.source_instance_id}:notifications")
    result = load_runtime_evidence(_request(now, SourceSystem.NOTIFICATION_RUNTIME, subject), now)
    serialized = repr(result)
    assert result.complete is True
    assert "secret title" not in serialized and "secret detail" not in serialized
