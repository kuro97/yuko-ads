from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from integrations import trello
from services import approval_source_trello
from services.approval_checker_models import (
    EvidenceRequest,
    EvidenceState,
    Metric,
    SourceSystem,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)


def _payload() -> dict:
    return {
        "id": "card-1",
        "idBoard": "board-1",
        "idList": "ready-1",
        "name": "Exact card",
        "desc": "Exact description",
        "due": "2026-07-23T08:00:00.000Z",
        "dueComplete": False,
        "closed": False,
        "dateLastActivity": "2026-07-22T07:59:00.000Z",
        "labels": [{"id": "label-1", "name": "PRODB", "color": "blue"}],
        "attachments": [
            {
                "id": "attachment-1",
                "name": "creative",
                "url": "https://drive.google.com/file/d/exact",
                "mimeType": "video/mp4",
                "bytes": 123,
                "date": "2026-07-22T07:00:00.000Z",
            }
        ],
    }


def _request() -> EvidenceRequest:
    return EvidenceRequest(
        request_id="trello-test",
        purpose="ACTION",
        action_kind=None,
        generated_at=NOW,
        subjects=(),
        claims=(),
        required_sources=(SourceSystem.TRELLO,),
        windows=(),
        account_ids=(),
        adset_ids=(),
        ad_ids=(),
        card_ids=("card-1",),
        staged_relative_paths=(),
        include_full_inventory=False,
        force_live=True,
        max_age_seconds=60,
    )


def test_snapshot_requests_and_binds_all_exact_precondition_fields(monkeypatch):
    response = SimpleNamespace(json=lambda: _payload())
    calls: list[tuple[str, str, dict]] = []

    def safe_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return response

    monkeypatch.setattr(trello, "safe_request", safe_request)

    snapshot = trello.get_card_snapshot("card-1")

    assert snapshot.board_id == "board-1"
    assert snapshot.list_id == "ready-1"
    assert snapshot.due_complete is False
    assert snapshot.closed is False
    assert snapshot.date_last_activity == datetime(
        2026, 7, 22, 7, 59, tzinfo=timezone.utc
    )
    assert tuple(item["id"] for item in snapshot.attachments) == ("attachment-1",)
    assert len(snapshot.content_sha256) == 64
    params = calls[0][2]["params"]
    assert "dateLastActivity" in params["fields"]
    assert params["attachments"] == "true"


def test_snapshot_hash_changes_on_list_activity_and_attachment_drift(monkeypatch):
    payloads = [_payload(), _payload(), _payload()]
    payloads[1]["idList"] = "other-list"
    payloads[2]["attachments"][0]["bytes"] = 124
    monkeypatch.setattr(
        trello,
        "safe_request",
        lambda *_args, **_kwargs: SimpleNamespace(json=lambda: payloads.pop(0)),
    )

    hashes = [trello.get_card_snapshot("card-1").content_sha256 for _ in range(3)]

    assert len(set(hashes)) == 3


def test_trello_evidence_returns_exact_hash_and_audit_fields(monkeypatch):
    monkeypatch.setattr(
        approval_source_trello,
        "get_card_snapshot",
        lambda _card_id: trello.TrelloCardSnapshot(
            card_id="card-1",
            board_id="board-1",
            list_id="ready-1",
            name="Exact card",
            description="Exact description",
            due=None,
            due_complete=False,
            closed=False,
            date_last_activity=NOW,
            labels=({"id": "label-1", "name": "PRODB", "color": "blue"},),
            attachments=(
                {
                    "id": "attachment-1",
                    "name": "creative",
                    "url": "https://drive.google.com/file/d/exact",
                    "mimeType": "video/mp4",
                    "bytes": 123,
                    "date": None,
                },
            ),
            content_sha256="a" * 64,
        ),
    )

    evidence = approval_source_trello.load_trello_evidence(
        _request(), NOW, force_live=True
    )

    assert evidence.state is EvidenceState.FRESH_COMPLETE
    assert evidence.complete is True
    assert evidence.records[0].metric is Metric.MATCH_STATE
    assert evidence.records[0].value == "a" * 64
    assert "list:ready-1" in evidence.records[0].entity_ids
    assert "attachment-id:attachment-1" in evidence.records[0].entity_ids


def test_trello_malformed_snapshot_fails_closed(monkeypatch):
    payload = _payload()
    payload.pop("dateLastActivity")
    monkeypatch.setattr(
        trello,
        "safe_request",
        lambda *_args, **_kwargs: SimpleNamespace(json=lambda: payload),
    )
    monkeypatch.setattr(
        approval_source_trello,
        "get_card_snapshot",
        trello.get_card_snapshot,
    )

    evidence = approval_source_trello.load_trello_evidence(
        _request(), NOW, force_live=True
    )

    assert evidence.complete is False
    assert evidence.state is EvidenceState.ERROR

