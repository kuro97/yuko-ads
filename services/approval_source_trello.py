"""Force-live Trello evidence для exact launch precondition."""

from __future__ import annotations

import hashlib
from datetime import datetime

from integrations.trello import TrelloCardSnapshot, get_card_snapshot
from services.approval_checker_models import (
    EvidenceRecord,
    EvidenceRequest,
    EvidenceState,
    FactCategory,
    Metric,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    canonical_json,
)


class TrelloEvidenceError(RuntimeError):
    """Trello не смог доказать полный exact snapshot."""


def _attachment_sha256(snapshot: TrelloCardSnapshot) -> str:
    return hashlib.sha256(canonical_json(snapshot.attachments)).hexdigest()


def _labels_sha256(snapshot: TrelloCardSnapshot) -> str:
    return hashlib.sha256(canonical_json(snapshot.labels)).hexdigest()


def _error(now: datetime, code: str, state: EvidenceState) -> SourceEvidence:
    return SourceEvidence(
        source=SourceSystem.TRELLO,
        state=state,
        fetched_at=now,
        data_as_of=None,
        from_cache=False,
        complete=False,
        records=(),
        error_code=code,
    )


def snapshot_entity_ids(snapshot: TrelloCardSnapshot) -> tuple[str, ...]:
    """Возвращает безопасные exact precondition детали для аудита."""

    return (
        f"board:{snapshot.board_id}",
        f"list:{snapshot.list_id}",
        f"due-complete:{str(snapshot.due_complete).lower()}",
        f"closed:{str(snapshot.closed).lower()}",
        f"activity:{snapshot.date_last_activity.isoformat()}",
        f"attachments:{_attachment_sha256(snapshot)}",
        f"labels:{_labels_sha256(snapshot)}",
        *(f"attachment-id:{item['id']}" for item in snapshot.attachments),
        *((f"content-legacy:{snapshot.legacy_content_sha256}",) if snapshot.legacy_content_sha256 else ()),
    )


def load_trello_evidence(
    request: EvidenceRequest,
    now: datetime,
    *,
    force_live: bool,
) -> SourceEvidence:
    """Читает каждую карточку по exact ID; локальный cache запрещён."""

    del force_live  # Этот adapter всегда вызывает live GET.
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать timezone")
    if len(request.card_ids) != len(set(request.card_ids)):
        return _error(now, "TRELLO_CARD_IDS_DUPLICATE", EvidenceState.INCOMPLETE)

    records: list[EvidenceRecord] = []
    latest_activity: datetime | None = None
    try:
        for card_id in sorted(request.card_ids):
            snapshot = get_card_snapshot(card_id)
            if snapshot.card_id != card_id:
                raise TrelloEvidenceError("TRELLO_CARD_ID_MISMATCH")
            latest_activity = (
                snapshot.date_last_activity
                if latest_activity is None
                else max(latest_activity, snapshot.date_last_activity)
            )
            records.append(
                EvidenceRecord(
                    category=FactCategory.MATCH,
                    subject=SubjectRef(SubjectKind.CARD, card_id),
                    metric=Metric.MATCH_STATE,
                    value=snapshot.content_sha256,
                    source=SourceSystem.TRELLO,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=None,
                    currency=None,
                    entity_ids=snapshot_entity_ids(snapshot),
                )
            )
    except Exception as exc:
        code = str(exc) if isinstance(exc, TrelloEvidenceError) else type(exc).__name__
        state = (
            EvidenceState.INCOMPLETE
            if isinstance(exc, (TrelloEvidenceError, ValueError))
            else EvidenceState.ERROR
        )
        return _error(now, code[:120], state)

    return SourceEvidence(
        source=SourceSystem.TRELLO,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=now,
        data_as_of=latest_activity or now,
        from_cache=False,
        complete=True,
        records=tuple(records),
    )
