"""SQLite-тесты durable replacement workflow и cleanup audit (migration 016)."""

import json
import sqlite3
import threading

import pytest

from services import creative_intelligence as ci
from services import replacement_workflow as workflow


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Каждый тест работает только со своей временной SQLite без сети."""
    ci.DB_PATH = None
    db_path = str(tmp_path / "replacement.db")
    ci.init_kb(db_path)
    yield db_path
    ci.DB_PATH = None


def _enqueue(old_ad_id: str = "old-1", adset_id: str = "adset-1") -> str:
    return workflow.enqueue_replacement(
        old_ad_id=old_ad_id,
        old_ad_name="Старая реклама",
        adset_id=adset_id,
        city="CityA",
        adset_type="L1",
    )


def _link(
    workflow_id: str,
    *,
    launch_attempt_key: str = "attempt-1",
    adset_id: str = "adset-1",
    expected_ad_names: tuple[str, ...] = ("CityA | Новый креатив",),
) -> None:
    workflow.link_replacement_launch(
        workflow_id=workflow_id,
        launch_attempt_key=launch_attempt_key,
        card_id="card-1",
        card_name="Карточка 1",
        city="CityA",
        account_kind="offline",
        account_id="account-1",
        adset_id=adset_id,
        expected_ad_names=expected_ad_names,
        expected_ad_count=len(expected_ad_names),
        media_manifest_sha256="sha256-manifest",
    )


def _advance_to_launching(
    old_ad_id: str = "old-1",
    adset_id: str = "adset-1",
    *,
    expected_ad_names: tuple[str, ...] = ("CityA | Новый креатив",),
    released_ad_id: str | None = None,
) -> str:
    workflow_id = _enqueue(old_ad_id=old_ad_id, adset_id=adset_id)
    _link(
        workflow_id,
        adset_id=adset_id,
        expected_ad_names=expected_ad_names,
    )
    workflow.mark_slot_available(workflow_id, released_ad_id=released_ad_id)
    claimed = workflow.claim_workflow_for_launch(workflow_id)
    assert claimed is not None
    assert claimed["workflow_id"] == workflow_id
    return workflow_id


def _advance_to_waiting_active(
    old_ad_id: str = "old-1",
    adset_id: str = "adset-1",
    replacement_ad_id: str = "new-1",
) -> str:
    workflow_id = _advance_to_launching(
        old_ad_id=old_ad_id,
        adset_id=adset_id,
    )
    workflow.record_replacement_created(
        workflow_id,
        (replacement_ad_id,),
        "attempt-1",
    )
    return workflow_id


def _event_types(db_path: str, workflow_id: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            row[0]
            for row in conn.execute(
                """
                SELECT event_type FROM ad_replacement_events
                WHERE workflow_id = ? ORDER BY id
                """,
                (workflow_id,),
            )
        ]
    finally:
        conn.close()


def _active_evidence(
    ad_ids: tuple[str, ...],
    names: tuple[str, ...],
    *,
    adset_id: str = "adset-1",
) -> dict:
    return {
        "ads": [
            {
                "id": ad_id,
                "name": name,
                "adset_id": adset_id,
                "effective_status": "ACTIVE",
            }
            for ad_id, name in zip(ad_ids, names, strict=True)
        ]
    }


def test_migration_creates_tables_indexes_and_is_idempotent(isolated_db):
    conn = sqlite3.connect(isolated_db)
    try:
        ci._apply_replacement_workflow_migration(conn)
        ci._apply_replacement_workflow_migration(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    finally:
        conn.close()

    assert {
        "ad_replacement_workflows",
        "ad_cleanup_audit",
        "ad_replacement_launch_links",
        "ad_replacement_events",
    } <= tables
    assert {
        "uq_replacement_open_old",
        "idx_replacement_phase",
        "idx_replacement_adset",
        "idx_cleanup_audit_run",
        "idx_cleanup_audit_ad",
        "uq_cleanup_delete_attempt_workflow",
        "idx_replacement_launch_card",
        "idx_replacement_events_workflow",
    } <= indexes


def test_launch_link_is_immutable_and_exact_retry_is_idempotent(isolated_db):
    workflow_id = _enqueue()
    _link(workflow_id)
    first = workflow.get_replacement_launch(workflow_id)

    _link(workflow_id)

    assert workflow.get_replacement_launch(workflow_id) == first
    with pytest.raises(workflow.WorkflowStateError, match="immutable"):
        _link(
            workflow_id,
            expected_ad_names=("CityA | Другой креатив",),
        )
    assert workflow.get_replacement_launch(workflow_id) == first
    assert _event_types(isolated_db, workflow_id) == ["ENQUEUED"]


def test_launch_link_rejects_scope_and_manifest_drift():
    workflow_id = _enqueue()

    with pytest.raises(workflow.WorkflowStateError, match="scope"):
        _link(workflow_id, adset_id="wrong-adset")
    with pytest.raises(ValueError, match="уникальные"):
        _link(
            workflow_id,
            expected_ad_names=("Один", "Один"),
        )

    assert workflow.get_replacement_launch(workflow_id) is None


def test_launch_link_concurrent_exact_bind_creates_one_row(isolated_db):
    workflow_id = _enqueue()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def bind() -> None:
        try:
            barrier.wait(timeout=5)
            _link(workflow_id)
        except BaseException as exc:  # pragma: no cover - диагностика thread
            errors.append(exc)

    threads = [threading.Thread(target=bind) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    conn = sqlite3.connect(isolated_db)
    try:
        link_count = conn.execute(
            "SELECT COUNT(*) FROM ad_replacement_launch_links"
        ).fetchone()[0]
    finally:
        conn.close()
    assert errors == []
    assert link_count == 1


def test_launch_phase_guards_require_bound_link_and_waiting_card():
    workflow_id = _enqueue()

    with pytest.raises(workflow.WorkflowStateError, match="launch link"):
        workflow.mark_slot_available(workflow_id)
    assert workflow.claim_waiting_workflow("adset-1") is None

    _link(workflow_id)
    with pytest.raises(workflow.WorkflowStateError, match="фазы WAITING_SLOT"):
        workflow.record_replacement_created(
            workflow_id,
            ("new-1",),
            "attempt-1",
        )
    with pytest.raises(workflow.WorkflowStateError, match="фазы WAITING_SLOT"):
        workflow.mark_old_paused(workflow_id)

    workflow.mark_slot_available(workflow_id)
    assert workflow.claim_waiting_workflow("adset-1") is not None
    with pytest.raises(workflow.WorkflowStateError, match="фазы LAUNCHING"):
        workflow.mark_slot_available(workflow_id)


def test_launch_cas_fails_closed_when_durable_link_drifted(isolated_db):
    workflow_id = _enqueue()
    _link(workflow_id)
    conn = sqlite3.connect(isolated_db)
    try:
        conn.execute(
            """
            UPDATE ad_replacement_launch_links
            SET expected_ad_count = 2 WHERE workflow_id = ?
            """,
            (workflow_id,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(workflow.WorkflowStateError, match="expected_ad_count"):
        workflow.mark_slot_available(workflow_id)

    assert workflow.get_workflow(workflow_id)["phase"] == "WAITING_SLOT"
    assert _event_types(isolated_db, workflow_id) == ["ENQUEUED"]


def test_launch_claim_concurrency_has_one_cas_winner_and_one_event(isolated_db):
    workflow_id = _enqueue()
    _link(workflow_id)
    workflow.mark_slot_available(workflow_id)
    barrier = threading.Barrier(2)
    results: list[dict | None] = []
    errors: list[BaseException] = []

    def claim() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(workflow.claim_workflow_for_launch(workflow_id))
        except BaseException as exc:  # pragma: no cover - диагностика thread
            errors.append(exc)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(results) == 2
    assert sum(result is not None for result in results) == 1
    assert workflow.get_workflow(workflow_id)["phase"] == "LAUNCHING"
    assert _event_types(isolated_db, workflow_id).count("LAUNCH_CLAIMED") == 1


def test_exact_launch_claim_never_claims_another_workflow_or_adset():
    first = _enqueue(old_ad_id="old-first", adset_id="adset-1")
    second = _enqueue(old_ad_id="old-second", adset_id="adset-1")
    third = _enqueue(old_ad_id="old-third", adset_id="adset-2")
    _link(first, launch_attempt_key="attempt-first")
    _link(second, launch_attempt_key="attempt-second")
    _link(
        third,
        launch_attempt_key="attempt-third",
        adset_id="adset-2",
    )
    workflow.mark_slot_available(first)
    workflow.mark_slot_available(second)
    workflow.mark_slot_available(third)

    claimed = workflow.claim_workflow_for_launch(second)

    assert claimed is not None
    assert claimed["workflow_id"] == second
    assert claimed["launch_attempt_key"] == "attempt-second"
    assert claimed["expected_ad_names"] == ["CityA | Новый креатив"]
    assert workflow.get_workflow(first)["phase"] == "WAITING_CARD"
    assert workflow.get_workflow(third)["phase"] == "WAITING_CARD"
    assert workflow.claim_workflow_for_launch("missing-workflow") is None
    assert workflow.claim_workflow_for_launch(second) is None


def test_record_created_persists_all_ids_and_retry_is_idempotent(isolated_db):
    workflow_id = _advance_to_launching(
        expected_ad_names=("Креатив 1", "Креатив 2"),
    )

    workflow.record_replacement_created(
        workflow_id,
        ("new-1", "new-2"),
        "attempt-1",
    )
    first_events = _event_types(isolated_db, workflow_id)
    workflow.record_replacement_created(
        workflow_id,
        ("new-1", "new-2"),
        "attempt-1",
    )

    row = workflow.get_workflow(workflow_id)
    link = workflow.get_replacement_launch(workflow_id)
    assert row["phase"] == "WAITING_ACTIVE"
    assert row["replacement_ad_id"] == "new-1"
    assert row["card_id"] == "card-1"
    assert json.loads(link["created_ad_ids_json"]) == ["new-1", "new-2"]
    assert _event_types(isolated_db, workflow_id) == first_events
    assert first_events.count("CREATE_RECORDED") == 1

    with pytest.raises(workflow.WorkflowStateError, match="другой launch result"):
        workflow.record_replacement_created(
            workflow_id,
            ("new-1", "different-2"),
            "attempt-1",
        )


def test_record_created_transaction_rolls_back_and_reconcile_retry_succeeds(
    isolated_db,
):
    workflow_id = _advance_to_launching()
    conn = sqlite3.connect(isolated_db)
    try:
        conn.execute(
            """
            CREATE TRIGGER fail_create_recorded_event
            BEFORE INSERT ON ad_replacement_events
            WHEN NEW.event_type = 'CREATE_RECORDED'
            BEGIN
                SELECT RAISE(ABORT, 'forced event failure');
            END
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(sqlite3.IntegrityError, match="forced event failure"):
        workflow.record_replacement_created(
            workflow_id,
            ("new-1",),
            "attempt-1",
        )

    row = workflow.get_workflow(workflow_id)
    link = workflow.get_replacement_launch(workflow_id)
    assert row["phase"] == "LAUNCHING"
    assert row["replacement_ad_id"] is None
    assert json.loads(link["created_ad_ids_json"]) == []

    conn = sqlite3.connect(isolated_db)
    try:
        conn.execute("DROP TRIGGER fail_create_recorded_event")
        conn.commit()
    finally:
        conn.close()
    workflow.record_replacement_created(
        workflow_id,
        ("new-1",),
        "attempt-1",
    )
    assert workflow.get_workflow(workflow_id)["phase"] == "WAITING_ACTIVE"
    assert _event_types(isolated_db, workflow_id).count("CREATE_RECORDED") == 1


def test_record_created_concurrent_exact_callbacks_are_once_only(isolated_db):
    workflow_id = _advance_to_launching()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def record() -> None:
        try:
            barrier.wait(timeout=5)
            workflow.record_replacement_created(
                workflow_id,
                ("new-1",),
                "attempt-1",
            )
        except BaseException as exc:  # pragma: no cover - диагностика thread
            errors.append(exc)

    threads = [threading.Thread(target=record) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert workflow.get_workflow(workflow_id)["phase"] == "WAITING_ACTIVE"
    assert _event_types(isolated_db, workflow_id).count("CREATE_RECORDED") == 1


def test_confirm_active_requires_all_exact_durable_ids_and_is_idempotent(isolated_db):
    names = ("Креатив 1", "Креатив 2")
    ad_ids = ("new-1", "new-2")
    workflow_id = _advance_to_launching(expected_ad_names=names)
    workflow.record_replacement_created(workflow_id, ad_ids, "attempt-1")
    evidence = _active_evidence(ad_ids, names)

    assert workflow.confirm_replacement_active(
        workflow_id,
        ad_ids,
        evidence=evidence,
    ) is True
    assert workflow.confirm_replacement_active(
        workflow_id,
        ad_ids,
        evidence=evidence,
    ) is False

    assert workflow.get_workflow(workflow_id)["phase"] == "READY_TO_PAUSE"
    assert _event_types(isolated_db, workflow_id).count("ACTIVE_CONFIRMED") == 1


def test_confirm_active_concurrency_has_exactly_one_cas_winner(isolated_db):
    ad_ids = ("new-1",)
    names = ("CityA | Новый креатив",)
    workflow_id = _advance_to_waiting_active(replacement_ad_id=ad_ids[0])
    evidence = _active_evidence(ad_ids, names)
    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[BaseException] = []

    def confirm() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(
                workflow.confirm_replacement_active(
                    workflow_id,
                    ad_ids,
                    evidence=evidence,
                )
            )
        except BaseException as exc:  # pragma: no cover - диагностика thread
            errors.append(exc)

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert sorted(results) == [False, True]
    assert workflow.get_workflow(workflow_id)["phase"] == "READY_TO_PAUSE"
    assert _event_types(isolated_db, workflow_id).count("ACTIVE_CONFIRMED") == 1


@pytest.mark.parametrize(
    ("passed_ids", "evidence", "error"),
    [
        (
            ("different-id",),
            _active_evidence(
                ("different-id",),
                ("CityA | Новый креатив",),
            ),
            "durable created IDs",
        ),
        (
            ("new-1",),
            _active_evidence(("new-1",), ("Другое имя",)),
            "names",
        ),
        (
            ("new-1",),
            _active_evidence(
                ("new-1",),
                ("CityA | Новый креатив",),
                adset_id="wrong-adset",
            ),
            "другого adset",
        ),
    ],
)
def test_confirm_active_rejects_mismatch_without_phase_change(
    passed_ids,
    evidence,
    error,
):
    workflow_id = _advance_to_waiting_active()

    with pytest.raises(workflow.WorkflowStateError, match=error):
        workflow.confirm_replacement_active(
            workflow_id,
            passed_ids,
            evidence=evidence,
        )

    assert workflow.get_workflow(workflow_id)["phase"] == "WAITING_ACTIVE"


def test_confirm_active_rejects_non_active_evidence():
    workflow_id = _advance_to_waiting_active()
    evidence = _active_evidence(
        ("new-1",),
        ("CityA | Новый креатив",),
    )
    evidence["ads"][0]["effective_status"] = "PENDING_REVIEW"

    with pytest.raises(workflow.WorkflowStateError, match="неактивную"):
        workflow.confirm_replacement_active(
            workflow_id,
            ("new-1",),
            evidence=evidence,
        )

    assert workflow.get_workflow(workflow_id)["phase"] == "WAITING_ACTIVE"


def test_record_created_rejects_released_and_other_open_workflow_references():
    released_workflow = _advance_to_launching(released_ad_id="released-1")
    with pytest.raises(workflow.WorkflowStateError, match="old/released"):
        workflow.record_replacement_created(
            released_workflow,
            ("released-1",),
            "attempt-1",
        )

    referenced_id = "other-old"
    _enqueue(old_ad_id=referenced_id, adset_id="adset-other")
    with pytest.raises(workflow.WorkflowStateError, match="другом open workflow"):
        workflow.record_replacement_created(
            released_workflow,
            (referenced_id,),
            "attempt-1",
        )


@pytest.mark.parametrize("reference_kind", ["replacement", "released", "created"])
def test_record_created_rejects_every_other_open_reference(
    isolated_db,
    reference_kind,
):
    workflow_id = _advance_to_launching()
    other_workflow_id = _enqueue(old_ad_id="other-old", adset_id="adset-other")
    referenced_ad_id = "shared-ad"
    conn = sqlite3.connect(isolated_db)
    try:
        if reference_kind in {"replacement", "released"}:
            conn.execute(
                f"""
                UPDATE ad_replacement_workflows
                SET {reference_kind}_ad_id = ? WHERE workflow_id = ?
                """,
                (referenced_ad_id, other_workflow_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO ad_replacement_launch_links (
                    workflow_id, launch_attempt_key, card_id, card_name, city,
                    account_kind, account_id, adset_id, expected_ad_count,
                    expected_ad_names_json, media_manifest_sha256,
                    created_ad_ids_json, created_at, updated_at
                ) VALUES (?, 'attempt-other', 'card-other', 'Карточка', 'CityA',
                          'offline', 'account-1', 'adset-other', 1, '["Имя"]',
                          'sha256-other', ?, '2026-07-20T00:00:00+05:00',
                          '2026-07-20T00:00:00+05:00')
                """,
                (other_workflow_id, json.dumps([referenced_ad_id])),
            )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(workflow.WorkflowStateError, match="другом open workflow"):
        workflow.record_replacement_created(
            workflow_id,
            (referenced_ad_id,),
            "attempt-1",
        )

    assert workflow.get_workflow(workflow_id)["phase"] == "LAUNCHING"


def test_append_events_are_sanitized_append_only_and_validate_type(isolated_db):
    workflow_id = _enqueue()
    secret = "EAA_REALLY_SECRET_TOKEN_123456789"
    opaque_header_secret = "opaque-header-value-123"
    for _ in range(2):
        workflow.append_replacement_event(
            workflow_id,
            "RECONCILED",
            actor=f"authorization=Bearer {secret}",
            evidence={
                "Authorization": secret,
                "headers": {"X-Custom-Auth": opaque_header_secret},
                "refresh_token": opaque_header_secret,
                "source": f"https://example.test/path?access_token={secret}",
            },
            error=f"api_key={secret}",
        )
    with pytest.raises(ValueError, match="неподдерживаемый"):
        workflow.append_replacement_event(
            workflow_id,
            "MUTATED",
            actor="test",
        )

    conn = sqlite3.connect(isolated_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT event_type, actor, evidence_json, error
            FROM ad_replacement_events WHERE workflow_id = ? ORDER BY id
            """,
            (workflow_id,),
        ).fetchall()
    finally:
        conn.close()
    persisted = " ".join(str(value) for row in rows for value in tuple(row))
    assert [row["event_type"] for row in rows] == [
        "ENQUEUED",
        "RECONCILED",
        "RECONCILED",
    ]
    assert secret not in persisted
    assert opaque_header_secret not in persisted
    assert "?<redacted>" in persisted
    assert "[REDACTED]" in persisted


def test_enqueue_deduplicates_open_workflow_including_blocked():
    first = _enqueue()
    assert _enqueue() == first

    workflow.mark_workflow_blocked(first, "manual_review")

    assert _enqueue() == first
    assert workflow.get_workflow(first)["phase"] == "BLOCKED"


def test_completed_workflow_allows_new_workflow(monkeypatch):
    first = _advance_to_waiting_active()
    monkeypatch.setattr(
        workflow,
        "_fetch_live_ad",
        lambda ad_id: {
            "id": ad_id,
            "name": "CityA | Новый креатив",
            "adset_id": "adset-1",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
        },
    )
    assert workflow.refresh_replacement_statuses()["ready"] == 1
    workflow.mark_old_paused(first)

    second = _enqueue()

    assert second != first
    assert workflow.get_workflow(first)["phase"] == "COMPLETED"
    assert workflow.get_workflow(second)["phase"] == "WAITING_SLOT"


def test_happy_path_requires_exact_active_replacement(monkeypatch, isolated_db):
    workflow_id = _advance_to_waiting_active()
    monkeypatch.setattr(
        workflow,
        "_fetch_live_ad",
        lambda ad_id: {
            "id": ad_id,
            "name": "CityA | Новый креатив",
            "adset_id": "adset-1",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
        },
    )

    result = workflow.refresh_replacement_statuses()

    ready = workflow.get_workflow(workflow_id)
    assert result == {
        "checked": 1,
        "ready": 1,
        "waiting": 0,
        "blocked": 0,
        "errors": [],
    }
    assert ready["phase"] == "READY_TO_PAUSE"
    assert ready["replacement_active_at"] is not None

    workflow.mark_old_paused(workflow_id)
    completed = workflow.get_workflow(workflow_id)
    assert completed["phase"] == "COMPLETED"
    assert completed["old_paused_at"] is not None
    assert _event_types(isolated_db, workflow_id) == [
        "ENQUEUED",
        "SLOT_RESERVED",
        "LAUNCH_CLAIMED",
        "CREATE_RECORDED",
        "ACTIVE_CONFIRMED",
        "OLD_PAUSED",
    ]


def test_refresh_checks_every_created_id_and_name_before_ready(monkeypatch):
    names = ("Креатив 1", "Креатив 2")
    ad_ids = ("new-1", "new-2")
    workflow_id = _advance_to_launching(expected_ad_names=names)
    workflow.record_replacement_created(workflow_id, ad_ids, "attempt-1")
    fetched: list[str] = []

    def fetch(ad_id):
        fetched.append(ad_id)
        index = ad_ids.index(ad_id)
        return {
            "id": ad_id,
            "name": names[index],
            "adset_id": "adset-1",
            "status": "ACTIVE",
            "effective_status": "ACTIVE" if index == 0 else "PENDING_REVIEW",
        }

    monkeypatch.setattr(workflow, "_fetch_live_ad", fetch)

    result = workflow.refresh_replacement_statuses()

    assert fetched == ["new-1", "new-2"]
    assert result["ready"] == 0
    assert result["waiting"] == 1
    assert workflow.get_workflow(workflow_id)["phase"] == "WAITING_ACTIVE"


@pytest.mark.parametrize(
    "effective_status",
    ["PENDING_REVIEW", "IN_PROCESS", "PREAPPROVED", "WITH_ISSUES", "DISAPPROVED", ""],
)
def test_non_active_replacement_never_becomes_ready(monkeypatch, effective_status):
    workflow_id = _advance_to_waiting_active()
    monkeypatch.setattr(
        workflow,
        "_fetch_live_ad",
        lambda ad_id: {
            "id": ad_id,
            "name": "CityA | Новый креатив",
            "adset_id": "adset-1",
            "status": "ACTIVE",
            "effective_status": effective_status,
        },
    )

    result = workflow.refresh_replacement_statuses()

    assert result["ready"] == 0
    assert result["waiting"] == 1
    assert workflow.get_workflow(workflow_id)["phase"] == "WAITING_ACTIVE"
    with pytest.raises(workflow.WorkflowStateError):
        workflow.mark_old_paused(workflow_id)


def test_fb_error_fails_closed_and_keeps_old_active(monkeypatch):
    workflow_id = _advance_to_waiting_active()

    def _fail(_ad_id):
        raise RuntimeError("temporary FB error")

    monkeypatch.setattr(workflow, "_fetch_live_ad", _fail)

    result = workflow.refresh_replacement_statuses()
    row = workflow.get_workflow(workflow_id)

    assert result["ready"] == 0
    assert result["waiting"] == 1
    assert result["errors"]
    assert row["phase"] == "WAITING_ACTIVE"
    assert "temporary FB error" in row["last_error"]


def test_replacement_in_wrong_adset_blocks_workflow(monkeypatch):
    workflow_id = _advance_to_waiting_active()
    monkeypatch.setattr(
        workflow,
        "_fetch_live_ad",
        lambda ad_id: {
            "id": ad_id,
            "name": "CityA | Новый креатив",
            "adset_id": "another-adset",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
        },
    )

    result = workflow.refresh_replacement_statuses()

    row = workflow.get_workflow(workflow_id)
    assert result["blocked"] == 1
    assert result["ready"] == 0
    assert row["phase"] == "BLOCKED"
    assert row["last_error"] == "replacement_wrong_adset"


def test_replacement_id_cannot_equal_old_ad_id():
    workflow_id = _advance_to_launching()

    with pytest.raises(workflow.WorkflowStateError, match="совпадает"):
        workflow.record_replacement_created(
            workflow_id,
            ("old-1",),
            "attempt-1",
        )

    assert workflow.get_workflow(workflow_id)["phase"] == "LAUNCHING"


def test_claim_is_atomic_and_claims_only_one_workflow():
    first = _enqueue(old_ad_id="old-1")
    second = _enqueue(old_ad_id="old-2")
    _link(first, launch_attempt_key="attempt-first")
    _link(second, launch_attempt_key="attempt-second")
    workflow.mark_slot_available(first)
    workflow.mark_slot_available(second)

    claimed = workflow.claim_waiting_workflow("adset-1")

    assert claimed["workflow_id"] in {first, second}
    assert claimed["phase"] == "LAUNCHING"
    other = second if claimed["workflow_id"] == first else first
    assert workflow.get_workflow(other)["phase"] == "WAITING_CARD"


def test_mark_slot_available_preserves_null_when_no_delete():
    workflow_id = _enqueue()
    _link(workflow_id)

    workflow.mark_slot_available(workflow_id)

    row = workflow.get_workflow(workflow_id)
    assert row["phase"] == "WAITING_CARD"
    assert row["released_ad_id"] is None


def test_cleanup_audit_preserves_nulls_and_json_evidence(isolated_db):
    audit_id = workflow.append_cleanup_audit(
        run_id="run-1",
        workflow_id=None,
        ad_id="paused-1",
        ad_name="Старая реклама",
        adset_id="adset-1",
        action="DELETE_ATTEMPT",
        reason="live_and_local_zero",
        configured_status="PAUSED",
        effective_status="ADSET_PAUSED",
        age_days=45,
        lifetime_spend_usd=0.0,
        local_spend_usd=None,
        capacity_before=50,
        capacity_after=None,
        evidence={"live": {"spend": 0.0}, "local": None},
        error=None,
        actor="adset_cleaner",
    )

    conn = sqlite3.connect(isolated_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM ad_cleanup_audit WHERE id = ?", (audit_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row["local_spend_usd"] is None
    assert row["capacity_after"] is None
    assert row["error"] is None
    assert json.loads(row["evidence_json"]) == {
        "live": {"spend": 0.0},
        "local": None,
    }


def test_cleanup_audit_redacts_secrets_from_error_and_evidence(isolated_db):
    secret = "EAA_REALLY_SECRET_TOKEN_123456789"
    audit_id = workflow.append_cleanup_audit(
        run_id="run-secret",
        workflow_id=None,
        ad_id="paused-1",
        ad_name=f"name access_token={secret}",
        adset_id="adset-1",
        action="DELETE_FAILED",
        reason=f"authorization=Bearer {secret}",
        evidence={"nested": {"api_key": f"api_key={secret}"}},
        error=f"access_token={secret}&failed=true",
        actor="test",
    )

    conn = sqlite3.connect(isolated_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM ad_cleanup_audit WHERE id = ?", (audit_id,)
        ).fetchone()
    finally:
        conn.close()
    persisted = " ".join(
        str(row[field]) for field in ("ad_name", "reason", "evidence_json", "error")
    )
    assert secret not in persisted
    assert "[REDACTED]" in persisted


def test_cleanup_audit_rejects_unknown_action():
    with pytest.raises(ValueError, match="неподдерживаемое"):
        workflow.append_cleanup_audit(
            run_id="run-1",
            workflow_id=None,
            ad_id="paused-1",
            ad_name="",
            adset_id="adset-1",
            action="DELETE",
            reason="unsafe action",
            actor="test",
        )
