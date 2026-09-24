"""SQLite-контракты migration 017 и workflow-only cleanup repository."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from services import cleanup_repository as repository
from services import creative_intelligence as ci


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Каждый тест использует отдельную локальную SQLite без сети."""
    ci.DB_PATH = None
    db_path = str(tmp_path / "cleanup.db")
    ci.init_kb(db_path)
    yield db_path
    ci.DB_PATH = None


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _insert_workflow(
    db_path: str,
    *,
    workflow_id: str = "workflow-1",
    old_ad_id: str = "old-1",
    adset_id: str = "adset-1",
    phase: str = "WAITING_SLOT",
    replacement_ad_id: str | None = None,
    released_ad_id: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO ad_replacement_workflows (
                workflow_id, old_ad_id, old_ad_name, adset_id, city,
                adset_type, phase, replacement_ad_id, released_ad_id,
                created_at, updated_at
            ) VALUES (?, ?, 'Старая реклама', ?, 'CityA', 'L1', ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                old_ad_id,
                adset_id,
                phase,
                replacement_ad_id,
                released_ad_id,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_launch_link(
    db_path: str,
    *,
    workflow_id: str = "workflow-1",
    adset_id: str = "adset-1",
    expected_names: tuple[str, ...] = ("CityA | Новый креатив",),
    created_ad_ids: tuple[str, ...] = (),
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO ad_replacement_launch_links (
                workflow_id, launch_attempt_key, card_id, card_name, city,
                account_kind, account_id, adset_id, expected_ad_count,
                expected_ad_names_json, media_manifest_sha256,
                created_ad_ids_json, created_at, updated_at
            ) VALUES (?, ?, 'card-1', 'Карточка', 'CityA', 'offline',
                      'account-1', ?, ?, ?, 'sha256-manifest', ?, ?, ?)
            """,
            (
                workflow_id,
                f"attempt-{workflow_id}",
                adset_id,
                len(expected_names),
                json.dumps(expected_names, ensure_ascii=False),
                json.dumps(created_ad_ids, ensure_ascii=False),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _acquire_slot_run(
    *,
    workflow_id: str = "workflow-1",
    lease_owner: str = "worker-1",
    max_deletes: int = 2,
) -> repository.CleanupRunLease:
    return repository.acquire_cleanup_run(
        tenant_id="default",
        run_kind="REPLACEMENT_SLOT",
        scheduled_date=date(2026, 7, 20),
        workflow_id=workflow_id,
        lease_owner=lease_owner,
        lease_ttl=timedelta(minutes=5),
        config={
            "requested_mode": "active",
            "effective_mode": "active",
            "enabled": True,
            "dry_run": False,
            "allow_irreversible_delete": True,
            "replacement_enabled": True,
            "kill_switch": False,
            "stale_days": 30,
            "hard_reserve_slots": 1,
            "max_deletes_per_workflow": max_deletes,
            "other_reserved_slots": 0,
            "config_generation": 7,
            "config_hash": "a" * 64,
        },
    )


def _fresh_local_zero(**overrides) -> dict:
    evidence = {
        "kb_found": True,
        "local_spend_usd": 0.0,
        "local_impressions": 0,
        "local_clicks": 0,
        "local_leads": 0,
        "local_payments": 0,
        "any_positive_delivery": False,
        "any_positive_outcome": False,
        "complete": True,
        "error": None,
    }
    evidence.update(overrides)
    return evidence


def _start_delete_http(claim: repository.CleanupDeleteClaim) -> None:
    token_id = f"token-{claim.claim_id}"
    repository.create_cleanup_delete_authorization(
        token_id=token_id,
        claim=claim,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    repository.mark_cleanup_delete_http_started(
        token_id=token_id,
        claim_id=claim.claim_id,
        workflow_id=claim.workflow_id,
        adset_id=claim.adset_id,
        ad_id=claim.ad_id,
        live_capacity_before=claim.capacity_before,
    )


def _candidate(
    ad_id: str,
    *,
    ordinal: int,
    capacity_before: int,
    adset_id: str = "adset-1",
    state: str = "ELIGIBLE",
    age_days: int = 45,
) -> repository.CleanupCandidateRecord:
    return {
        "account_kind": "offline",
        "adset_id": adset_id,
        "adset_name": "CityA L1",
        "ad_id": ad_id,
        "ad_name": f"Кандидат {ad_id}",
        "ordinal": ordinal,
        "state": state,
        "reason": "exact_zero_evidence",
        "configured_status": "PAUSED",
        "effective_status": "PAUSED",
        "age_days": age_days,
        "lifetime_spend_usd": 0.0,
        "lifetime_impressions": 0,
        "lifetime_clicks": 0,
        "local_spend_usd": 0.0,
        "local_impressions": 0,
        "local_clicks": 0,
        "local_leads": 0,
        "local_payments": 0,
        "evidence": {
            "live_status": "ok",
            "active_count": 3,
            "inventory_complete": True,
            "lifetime_row_count": 1,
            "local_evidence_complete": True,
            "kb_present": True,
        },
        "capacity_before": capacity_before,
    }


def _prepare_claimable_run(
    db_path: str,
    *,
    candidates: tuple[repository.CleanupCandidateRecord, ...],
    max_deletes: int = 2,
) -> repository.CleanupRunLease:
    _insert_workflow(db_path)
    _insert_launch_link(db_path)
    lease = _acquire_slot_run(max_deletes=max_deletes)
    assert lease.acquired is True
    for candidate in candidates:
        repository.upsert_cleanup_candidate(
            lease.run_id,
            candidate,
            lease_owner="worker-1",
        )
    return lease


def _claim_surfaces(
    db_path: str,
    *,
    run_id: str,
    ad_id: str,
) -> tuple[tuple[object, ...], list[tuple[object, ...]], list[tuple[object, ...]]]:
    """Снимок трёх таблиц, которые атомарный claim имеет право менять."""
    conn = _connect(db_path)
    try:
        candidate = conn.execute(
            """
            SELECT state, claim_id, updated_at, evidence_json
            FROM ad_cleanup_candidates WHERE run_id = ? AND ad_id = ?
            """,
            (run_id, ad_id),
        ).fetchone()
        claims = conn.execute(
            "SELECT * FROM ad_cleanup_delete_claims ORDER BY claim_id"
        ).fetchall()
        audits = conn.execute("SELECT * FROM ad_cleanup_audit ORDER BY id").fetchall()
    finally:
        conn.close()
    assert candidate is not None
    return (
        tuple(candidate),
        [tuple(row) for row in claims],
        [tuple(row) for row in audits],
    )


def test_migration_twice_preserves_016_rows_and_replaces_audit_index():
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        migration_016 = (
            Path(__file__).parent.parent
            / "migrations"
            / "016_replacement_and_cleanup_audit.sql"
        ).read_text(encoding="utf-8")
        conn.executescript(migration_016)
        conn.execute(
            """
            INSERT INTO ad_replacement_workflows (
                workflow_id, old_ad_id, adset_id, phase, created_at, updated_at
            ) VALUES ('workflow-1', 'old-1', 'adset-1', 'WAITING_SLOT', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO ad_cleanup_audit (
                run_id, workflow_id, ad_id, adset_id, action, reason, actor, created_at
            ) VALUES ('legacy-run', 'workflow-1', ?, 'adset-1',
                      'DELETE_ATTEMPT', 'legacy', 'test', ?)
            """,
            ("legacy-ad-1", now),
        )
        conn.commit()

        ci._apply_proactive_cleaner_migration(conn)
        conn.execute(
            """
            INSERT INTO ad_cleanup_audit (
                run_id, workflow_id, ad_id, adset_id, action, reason, actor, created_at
            ) VALUES ('second-run', 'workflow-1', 'legacy-ad-2', 'adset-1',
                      'DELETE_ATTEMPT', 'second single-ad claim', 'test', ?)
            """,
            (now,),
        )
        conn.commit()
        before_second_apply = conn.execute(
            "SELECT workflow_id, ad_id, action FROM ad_cleanup_audit ORDER BY ad_id"
        ).fetchall()
        ci._apply_proactive_cleaner_migration(conn)

        after_rows = conn.execute(
            "SELECT workflow_id, ad_id, action FROM ad_cleanup_audit ORDER BY ad_id"
        ).fetchall()
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO ad_cleanup_audit (
                    run_id, workflow_id, ad_id, adset_id, action, reason, actor, created_at
                ) VALUES ('duplicate', 'workflow-1', 'legacy-ad-1', 'adset-1',
                          'DELETE_ATTEMPT', 'duplicate', 'test', ?)
                """,
                (now,),
            )
    finally:
        conn.close()

    assert [tuple(row) for row in after_rows] == [
        tuple(row) for row in before_second_apply
    ]
    assert "uq_cleanup_delete_attempt_workflow" not in indexes
    assert "uq_cleanup_delete_attempt_workflow_ad" in indexes


def test_migration_creates_all_t1_tables(isolated_db):
    conn = _connect(isolated_db)
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()

    assert {
        "ad_cleanup_runs",
        "ad_cleanup_candidates",
        "ad_cleanup_delete_claims",
        "ad_cleanup_delete_authorizations",
        "ad_replacement_launch_links",
        "ad_replacement_events",
        "launch_recovery_cases",
        "launch_recovery_city_plans",
    } <= tables


def test_db_rejects_active_proactive_daily(isolated_db):
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(isolated_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO ad_cleanup_runs (
                    run_id, run_kind, scheduled_date, requested_mode,
                    effective_mode, phase, created_at, updated_at
                ) VALUES ('bad-proactive', 'PROACTIVE_DAILY', '2026-07-20',
                          'active', 'active', 'RUNNING', ?, ?)
                """,
                (now, now),
            )
    finally:
        conn.close()


def test_repository_rejects_proactive_active_before_db_write(isolated_db):
    with pytest.raises(ValueError, match="только dry_run"):
        repository.acquire_cleanup_run(
            "default",
            "PROACTIVE_DAILY",
            date(2026, 7, 20),
            None,
            "worker-1",
            timedelta(minutes=5),
            {"requested_mode": "active", "effective_mode": "active"},
        )

    conn = _connect(isolated_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ad_cleanup_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_requested_dry_run_cannot_become_effective_active(isolated_db):
    _insert_workflow(isolated_db)
    with pytest.raises(ValueError, match="должны совпадать"):
        repository.acquire_cleanup_run(
            "default",
            "REPLACEMENT_SLOT",
            date(2026, 7, 20),
            "workflow-1",
            "worker-1",
            timedelta(minutes=5),
            {"requested_mode": "dry_run", "effective_mode": "active"},
        )

    conn = _connect(isolated_db)
    now = datetime.now(timezone.utc).isoformat()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO ad_cleanup_runs (
                    run_id, run_kind, workflow_id, scheduled_date,
                    requested_mode, effective_mode, phase, created_at, updated_at
                ) VALUES (
                    'invalid-mode-run', 'REPLACEMENT_SLOT', 'workflow-1',
                    '2026-07-20', 'dry_run', 'active', 'PLANNED', ?, ?
                )
                """,
                (now, now),
            )
    finally:
        conn.close()


def test_db_rejects_claim_without_concrete_workflow(isolated_db):
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(isolated_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO ad_cleanup_delete_claims (
                    claim_id, run_id, workflow_id, ad_id, adset_id, purpose,
                    state, capacity_before, claimed_by, claimed_at, created_at, updated_at
                ) VALUES ('bad-claim', 'missing-run', NULL, 'ad-1', 'adset-1',
                          'REPLACEMENT_SLOT', 'CLAIMED', 0, 'test', ?, ?, ?)
                """,
                (now, now, now),
            )
    finally:
        conn.close()


def test_db_rejects_claim_tied_to_proactive_run(isolated_db):
    _insert_workflow(isolated_db)
    lease = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {},
    )
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(isolated_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO ad_cleanup_delete_claims (
                    claim_id, run_id, workflow_id, ad_id, adset_id, purpose,
                    state, capacity_before, claimed_by, claimed_at, created_at, updated_at
                ) VALUES ('bad-proactive-claim', ?, 'workflow-1', 'ad-1', 'adset-1',
                          'REPLACEMENT_SLOT', 'CLAIMED', 0, 'test', ?, ?, ?)
                """,
                (lease.run_id, now, now, now),
            )
    finally:
        conn.close()


def test_recovery_city_plan_is_separate_typed_row_per_city(isolated_db):
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(isolated_db)
    try:
        conn.execute(
            """
            INSERT INTO launch_recovery_cases (
                case_id, trello_action_id, card_id, source_completed_at,
                scan_since, phase, created_at, updated_at
            ) VALUES ('case-1', 'action-1', 'card-1', ?, ?, 'DISCOVERED', ?, ?)
            """,
            (now, now, now, now),
        )
        conn.executemany(
            """
            INSERT INTO launch_recovery_city_plans (
                plan_id, case_id, city, account_kind, account_id, adset_id,
                expected_ad_names_json, expected_ad_count,
                reconcile_from, reconcile_until, phase,
                media_manifest_sha256, created_at, updated_at
            ) VALUES (?, 'case-1', ?, ?, ?, ?, ?, 1, ?, ?, 'MISSING',
                      'manifest-sha', ?, ?)
            """,
            [
                (
                    "plan-citya",
                    "CityA",
                    "offline",
                    "account-offline",
                    "adset-citya",
                    '["CityA | Креатив"]',
                    now,
                    now,
                    now,
                    now,
                ),
                (
                    "plan-online",
                    "Онлайн",
                    "online",
                    "account-online",
                    "adset-online",
                    '["Онлайн | Креатив"]',
                    now,
                    now,
                    now,
                    now,
                ),
            ],
        )
        conn.commit()
        rows = conn.execute(
            """
            SELECT city, account_kind, account_id, adset_id,
                   expected_ad_names_json, expected_ad_count,
                   reconcile_from, reconcile_until
            FROM launch_recovery_city_plans ORDER BY city
            """
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO launch_recovery_city_plans (
                    plan_id, case_id, city, account_kind, account_id, adset_id,
                    expected_ad_names_json, expected_ad_count,
                    reconcile_from, reconcile_until, phase,
                    media_manifest_sha256, created_at, updated_at
                ) VALUES ('bad-kind', 'case-1', 'CityB', 'other', 'account',
                          'adset', '["name"]', 1, ?, ?, 'MISSING', 'sha', ?, ?)
                """,
                (now, now, now, now),
            )
    finally:
        conn.close()

    assert len(rows) == 2
    assert rows[0]["account_id"] != rows[1]["account_id"]
    assert all(row["expected_ad_count"] == 1 for row in rows)
    assert all(json.loads(row["expected_ad_names_json"]) for row in rows)


def test_recovery_open_card_index_only_blocks_runnable_inflight_phases(isolated_db):
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(isolated_db)
    try:
        conn.execute(
            """
            INSERT INTO launch_recovery_cases (
                case_id, trello_action_id, card_id, source_completed_at,
                scan_since, phase, created_at, updated_at
            ) VALUES ('terminal-case', 'action-terminal', 'same-card', ?, ?,
                      'NO_ACTION', ?, ?)
            """,
            (now, now, now, now),
        )
        conn.execute(
            """
            INSERT INTO launch_recovery_cases (
                case_id, trello_action_id, card_id, source_completed_at,
                scan_since, phase, created_at, updated_at
            ) VALUES ('open-case', 'action-open', 'same-card', ?, ?,
                      'REVIEW_REQUIRED', ?, ?)
            """,
            (now, now, now, now),
        )
        conn.execute(
            """
            INSERT INTO launch_recovery_cases (
                case_id, trello_action_id, card_id, source_completed_at,
                scan_since, phase, created_at, updated_at
            ) VALUES ('blocked-case', 'action-blocked', 'same-card', ?, ?,
                      'BLOCKED', ?, ?)
            """,
            (now, now, now, now),
        )
        conn.execute(
            """
            INSERT INTO launch_recovery_cases (
                case_id, trello_action_id, card_id, source_completed_at,
                scan_since, phase, created_at, updated_at
            ) VALUES ('runnable-case', 'action-runnable', 'same-card', ?, ?,
                      'DISCOVERED', ?, ?)
            """,
            (now, now, now, now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO launch_recovery_cases (
                    case_id, trello_action_id, card_id, source_completed_at,
                    scan_since, phase, created_at, updated_at
                ) VALUES ('second-runnable', 'action-second-runnable', 'same-card', ?, ?,
                          'WAITING_SLOT', ?, ?)
                """,
                (now, now, now, now),
            )
    finally:
        conn.close()


def test_same_day_proactive_run_has_one_live_lease():
    first = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {},
    )
    second = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-2",
        timedelta(minutes=5),
        {},
    )

    assert first.acquired is True
    assert second.run_id == first.run_id
    assert second.acquired is False
    assert second.reason == "already_running"
    assert second.lease_owner == "worker-1"


def test_cross_thread_lease_cas_has_single_owner():
    barrier = threading.Barrier(2)
    results: list[repository.CleanupRunLease] = []
    errors: list[BaseException] = []

    def acquire(owner: str) -> None:
        try:
            barrier.wait(timeout=5)
            results.append(
                repository.acquire_cleanup_run(
                    "default",
                    "PROACTIVE_DAILY",
                    date(2026, 7, 20),
                    None,
                    owner,
                    timedelta(minutes=5),
                    {},
                )
            )
        except BaseException as exc:  # pragma: no cover - только диагностика thread
            errors.append(exc)

    threads = [
        threading.Thread(target=acquire, args=(f"worker-{index}",))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(results) == 2
    assert sum(result.acquired for result in results) == 1
    assert {result.run_id for result in results} == {results[0].run_id}


def test_expired_clean_lease_resumes_same_run(isolated_db):
    first = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {},
    )
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "UPDATE ad_cleanup_runs SET lease_expires_at = ? WHERE run_id = ?",
            (expired, first.run_id),
        )
        conn.commit()
    finally:
        conn.close()

    resumed = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-2",
        timedelta(minutes=5),
        {},
    )

    assert resumed.run_id == first.run_id
    assert resumed.acquired is True
    assert resumed.reason == "lease_resumed"
    assert resumed.lease_owner == "worker-2"


def test_renew_lease_requires_exact_owner():
    lease = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {},
    )

    assert (
        repository.renew_cleanup_run_lease(
            lease.run_id, "worker-2", timedelta(minutes=5)
        )
        is False
    )
    assert (
        repository.renew_cleanup_run_lease(
            lease.run_id, "worker-1", timedelta(minutes=5)
        )
        is True
    )


def test_run_evidence_persists_adset_without_candidates_and_checks_owner():
    lease = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {},
    )
    with pytest.raises(repository.CleanupLeaseError):
        repository.update_cleanup_run_evidence(
            lease.run_id,
            {"adsets": []},
            lease_owner="worker-2",
        )
    expected_adset = {
        "account_kind": "offline",
        "adset_id": "empty-adset",
        "adset_name": "CityA L1",
        "live_status": "ok",
        "used": 39,
        "available": 11,
        "active_count": 12,
        "safe_candidate_count": 0,
        "severity": "ok",
        "reason": None,
    }
    repository.update_cleanup_run_evidence(
        lease.run_id,
        {"adsets": [expected_adset]},
        lease_owner="worker-1",
    )

    status = repository.get_cleanup_status()

    assert status["adsets"] == [expected_adset]


def test_replacement_run_does_not_hide_latest_proactive_status(isolated_db):
    proactive = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "daily-worker",
        timedelta(minutes=5),
        {},
    )
    expected_adset = {
        "account_kind": "offline",
        "adset_id": "daily-adset",
        "adset_name": "Daily snapshot",
        "live_status": "ok",
        "used": 40,
        "available": 10,
        "active_count": 3,
        "safe_candidate_count": 0,
        "severity": "ok",
        "reason": None,
    }
    repository.update_cleanup_run_evidence(
        proactive.run_id,
        {"adsets": [expected_adset]},
        lease_owner="daily-worker",
    )
    _insert_workflow(isolated_db, workflow_id="workflow-status")
    replacement = repository.acquire_cleanup_run(
        "default",
        "REPLACEMENT_SLOT",
        date(2026, 7, 21),
        "workflow-status",
        "slot-worker",
        timedelta(minutes=5),
        {
            "requested_mode": "active",
            "effective_mode": "active",
            "enabled": True,
            "dry_run": False,
            "allow_irreversible_delete": True,
            "replacement_enabled": True,
            "kill_switch": False,
        },
    )
    assert replacement.acquired is True

    status = repository.get_cleanup_status()

    assert status["last_run"]["run_id"] == proactive.run_id
    assert status["last_run"]["run_kind"] == "PROACTIVE_DAILY"
    assert status["adsets"] == [expected_adset]


def test_candidate_upsert_is_idempotent_and_writes_one_dry_run_audit(isolated_db):
    lease = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {},
    )
    candidate = _candidate("ad-1", ordinal=0, capacity_before=2)

    repository.upsert_cleanup_candidate(lease.run_id, candidate, lease_owner="worker-1")
    repository.upsert_cleanup_candidate(lease.run_id, candidate, lease_owner="worker-1")

    conn = _connect(isolated_db)
    try:
        candidate_count = conn.execute(
            "SELECT COUNT(*) FROM ad_cleanup_candidates WHERE run_id = ?",
            (lease.run_id,),
        ).fetchone()[0]
        audits = conn.execute(
            "SELECT action, evidence_json FROM ad_cleanup_audit WHERE run_id = ?",
            (lease.run_id,),
        ).fetchall()
    finally:
        conn.close()

    assert candidate_count == 1
    assert [row["action"] for row in audits] == ["DRY_RUN_CANDIDATE"]
    assert json.loads(audits[0]["evidence_json"])["live_status"] == "ok"


def test_candidate_upsert_requires_lease_owner_argument():
    lease = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {},
    )

    with pytest.raises(TypeError, match="lease_owner"):
        repository.upsert_cleanup_candidate(  # type: ignore[call-arg]
            lease.run_id,
            _candidate("ad-1", ordinal=0, capacity_before=2),
        )


def test_candidate_upsert_wrong_owner_has_no_durable_side_effects(isolated_db):
    lease = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {},
    )

    with pytest.raises(repository.CleanupLeaseError, match="live lease_owner"):
        repository.upsert_cleanup_candidate(
            lease.run_id,
            _candidate("ad-1", ordinal=0, capacity_before=2),
            lease_owner="worker-2",
        )

    conn = _connect(isolated_db)
    try:
        candidates = conn.execute(
            "SELECT COUNT(*) FROM ad_cleanup_candidates"
        ).fetchone()[0]
        audits = conn.execute("SELECT COUNT(*) FROM ad_cleanup_audit").fetchone()[0]
    finally:
        conn.close()
    assert (candidates, audits) == (0, 0)


def test_stale_candidate_upsert_cannot_write_after_lease_takeover(isolated_db):
    first = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {},
    )
    original = _candidate("ad-1", ordinal=0, capacity_before=2)
    repository.upsert_cleanup_candidate(
        first.run_id,
        original,
        lease_owner="worker-1",
    )
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "UPDATE ad_cleanup_runs SET lease_expires_at = ? WHERE run_id = ?",
            (expired, first.run_id),
        )
        conn.commit()
    finally:
        conn.close()
    second = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-2",
        timedelta(minutes=5),
        {},
    )
    changed = _candidate("ad-1", ordinal=0, capacity_before=2)
    changed["evidence"] = {
        **changed["evidence"],
        "live_status": "changed",
        "active_count": 4,
    }

    with pytest.raises(repository.CleanupLeaseError, match="live lease_owner"):
        repository.upsert_cleanup_candidate(
            first.run_id,
            changed,
            lease_owner="worker-1",
        )

    conn = _connect(isolated_db)
    try:
        durable_evidence = conn.execute(
            "SELECT evidence_json FROM ad_cleanup_candidates WHERE run_id = ?",
            (first.run_id,),
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM ad_cleanup_audit WHERE run_id = ?",
            (first.run_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert second.acquired is True
    assert second.lease_owner == "worker-2"
    assert json.loads(durable_evidence)["live_status"] == "ok"
    assert audit_count == 1

    repository.upsert_cleanup_candidate(
        second.run_id,
        changed,
        lease_owner="worker-2",
    )
    conn = _connect(isolated_db)
    try:
        durable_evidence = conn.execute(
            "SELECT evidence_json FROM ad_cleanup_candidates WHERE run_id = ?",
            (second.run_id,),
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM ad_cleanup_audit WHERE run_id = ?",
            (second.run_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert json.loads(durable_evidence)["live_status"] == "changed"
    assert audit_count == 2


def test_candidate_upsert_cannot_forge_claimed_state():
    lease = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {},
    )
    candidate = _candidate(
        "ad-1",
        ordinal=0,
        capacity_before=2,
        state="CLAIMED",
    )

    with pytest.raises(repository.CleanupClaimError, match="claim/outcome state"):
        repository.upsert_cleanup_candidate(
            lease.run_id, candidate, lease_owner="worker-1"
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("configured_status", "ACTIVE"),
        ("effective_status", "ACTIVE"),
        ("age_days", 14),
        ("lifetime_spend_usd", 0.01),
        ("lifetime_impressions", 1),
        ("lifetime_clicks", None),
        ("local_spend_usd", None),
        ("local_impressions", 1),
        ("local_clicks", 1),
        ("local_leads", 1),
        ("local_payments", 1),
    ],
)
def test_eligible_candidate_requires_exact_safe_zero_evidence(field, value):
    lease = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {},
    )
    candidate = _candidate("ad-1", ordinal=0, capacity_before=2)
    candidate[field] = value

    with pytest.raises(repository.CleanupClaimError, match="ELIGIBLE candidate"):
        repository.upsert_cleanup_candidate(
            lease.run_id, candidate, lease_owner="worker-1"
        )


def test_eligible_candidate_accepts_inclusive_15_day_boundary(isolated_db):
    """Общий durable manifest принимает кандидата ровно в 15 полных дней."""
    lease = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {"stale_days": 15},
    )
    candidate = _candidate(
        "ad-boundary-15",
        ordinal=0,
        capacity_before=2,
        age_days=15,
    )

    repository.upsert_cleanup_candidate(
        lease.run_id,
        candidate,
        lease_owner="worker-1",
    )

    conn = _connect(isolated_db)
    try:
        durable = conn.execute(
            "SELECT state, age_days FROM ad_cleanup_candidates WHERE run_id = ?",
            (lease.run_id,),
        ).fetchone()
    finally:
        conn.close()
    assert tuple(durable) == ("ELIGIBLE", 15)


@pytest.mark.parametrize(
    "evidence_key",
    ["inventory_complete", "lifetime_row_count", "local_evidence_complete", "kb_present"],
)
def test_eligible_candidate_requires_complete_live_and_local_evidence(
    evidence_key,
):
    lease = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {},
    )
    candidate = _candidate("ad-1", ordinal=0, capacity_before=2)
    candidate["evidence"].pop(evidence_key)

    with pytest.raises(repository.CleanupClaimError, match="completeness"):
        repository.upsert_cleanup_candidate(
            lease.run_id, candidate, lease_owner="worker-1"
        )


def test_claim_without_bound_launch_link_rolls_back(isolated_db):
    _insert_workflow(isolated_db)
    lease = _acquire_slot_run()
    candidate = _candidate("ad-1", ordinal=0, capacity_before=1)
    repository.upsert_cleanup_candidate(lease.run_id, candidate, lease_owner="worker-1")

    with pytest.raises(repository.CleanupClaimError, match="launch link"):
        repository.claim_replacement_slot_delete(
            lease.run_id,
            "workflow-1",
            candidate,
            actor="test",
            lease_owner="worker-1",
        )

    conn = _connect(isolated_db)
    try:
        claims = conn.execute(
            "SELECT COUNT(*) FROM ad_cleanup_delete_claims"
        ).fetchone()[0]
        candidate_state = conn.execute(
            "SELECT state FROM ad_cleanup_candidates WHERE run_id = ? AND ad_id = 'ad-1'",
            (lease.run_id,),
        ).fetchone()[0]
        delete_attempts = conn.execute(
            "SELECT COUNT(*) FROM ad_cleanup_audit WHERE action = 'DELETE_ATTEMPT'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert claims == 0
    assert candidate_state == "ELIGIBLE"
    assert delete_attempts == 0


@pytest.mark.parametrize(
    "reference_location",
    [
        "current_old",
        "current_replacement",
        "current_released",
        "current_created",
        "other_old",
        "other_replacement",
        "other_released",
        "other_created",
    ],
)
def test_claim_rejects_any_ad_reference_from_any_open_workflow_without_mutation(
    isolated_db,
    reference_location,
):
    candidate_ad_id = "protected-ad"
    current_kind = reference_location.removeprefix("current_")
    current_kwargs = {
        "old_ad_id": candidate_ad_id if current_kind == "old" else "old-1",
        "replacement_ad_id": (
            candidate_ad_id if current_kind == "replacement" else None
        ),
        "released_ad_id": candidate_ad_id if current_kind == "released" else None,
    }
    _insert_workflow(isolated_db, **current_kwargs)
    _insert_launch_link(
        isolated_db,
        created_ad_ids=(candidate_ad_id,) if current_kind == "created" else (),
    )
    if reference_location.startswith("other_"):
        other_kind = reference_location.removeprefix("other_")
        _insert_workflow(
            isolated_db,
            workflow_id="workflow-other",
            old_ad_id=candidate_ad_id if other_kind == "old" else "old-other",
            adset_id="adset-other",
            phase="BLOCKED",
            replacement_ad_id=(
                candidate_ad_id if other_kind == "replacement" else None
            ),
            released_ad_id=(candidate_ad_id if other_kind == "released" else None),
        )
        _insert_launch_link(
            isolated_db,
            workflow_id="workflow-other",
            adset_id="adset-other",
            created_ad_ids=(candidate_ad_id,) if other_kind == "created" else (),
        )
    lease = _acquire_slot_run()
    candidate = _candidate(candidate_ad_id, ordinal=0, capacity_before=1)
    repository.upsert_cleanup_candidate(
        lease.run_id,
        candidate,
        lease_owner="worker-1",
    )
    before = _claim_surfaces(
        isolated_db,
        run_id=lease.run_id,
        ad_id=candidate_ad_id,
    )

    with pytest.raises(repository.CleanupClaimError, match="open replacement workflow"):
        repository.claim_replacement_slot_delete(
            lease.run_id,
            "workflow-1",
            candidate,
            actor="operator",
            lease_owner="worker-1",
        )

    after = _claim_surfaces(
        isolated_db,
        run_id=lease.run_id,
        ad_id=candidate_ad_id,
    )
    assert after == before


def test_claim_fails_closed_on_malformed_created_references_without_mutation(
    isolated_db,
):
    _insert_workflow(isolated_db)
    _insert_launch_link(isolated_db)
    _insert_workflow(
        isolated_db,
        workflow_id="workflow-other",
        old_ad_id="old-other",
        adset_id="adset-other",
        phase="BLOCKED",
    )
    _insert_launch_link(
        isolated_db,
        workflow_id="workflow-other",
        adset_id="adset-other",
    )
    conn = _connect(isolated_db)
    try:
        conn.execute(
            """
            UPDATE ad_replacement_launch_links
            SET created_ad_ids_json = '{broken'
            WHERE workflow_id = 'workflow-other'
            """
        )
        conn.commit()
    finally:
        conn.close()
    lease = _acquire_slot_run()
    candidate = _candidate("ad-1", ordinal=0, capacity_before=1)
    repository.upsert_cleanup_candidate(
        lease.run_id,
        candidate,
        lease_owner="worker-1",
    )
    before = _claim_surfaces(
        isolated_db,
        run_id=lease.run_id,
        ad_id="ad-1",
    )

    with pytest.raises(repository.CleanupClaimError, match="created_ad_ids_json"):
        repository.claim_replacement_slot_delete(
            lease.run_id,
            "workflow-1",
            candidate,
            actor="operator",
            lease_owner="worker-1",
        )

    after = _claim_surfaces(
        isolated_db,
        run_id=lease.run_id,
        ad_id="ad-1",
    )
    assert after == before


def test_claim_candidate_and_delete_attempt_commit_atomically(isolated_db):
    candidate = _candidate("ad-1", ordinal=0, capacity_before=1)
    lease = _prepare_claimable_run(isolated_db, candidates=(candidate,))

    claim = repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        candidate,
        actor="operator",
        lease_owner="worker-1",
    )

    conn = _connect(isolated_db)
    try:
        durable_claim = conn.execute(
            "SELECT * FROM ad_cleanup_delete_claims WHERE claim_id = ?",
            (claim.claim_id,),
        ).fetchone()
        durable_candidate = conn.execute(
            "SELECT state, claim_id FROM ad_cleanup_candidates WHERE run_id = ? AND ad_id = ?",
            (lease.run_id, "ad-1"),
        ).fetchone()
        audit = conn.execute(
            """
            SELECT workflow_id, ad_id FROM ad_cleanup_audit
            WHERE run_id = ? AND action = 'DELETE_ATTEMPT'
            """,
            (lease.run_id,),
        ).fetchone()
    finally:
        conn.close()

    assert claim.purpose == "REPLACEMENT_SLOT"
    assert durable_claim["state"] == "CLAIMED"
    assert tuple(durable_candidate) == ("CLAIMED", claim.claim_id)
    assert tuple(audit) == ("workflow-1", "ad-1")


@pytest.mark.parametrize("age_days", [15, 20, 29])
def test_stored_30_day_run_rejects_younger_candidate_at_claim(
    isolated_db, age_days
):
    """Сохранённый порог 30 не понижается новым default при claim."""
    candidate = _candidate(
        "ad-stored-threshold",
        ordinal=0,
        capacity_before=1,
        age_days=age_days,
    )
    lease = _prepare_claimable_run(isolated_db, candidates=(candidate,))

    with pytest.raises(repository.CleanupClaimError, match="30"):
        repository.claim_replacement_slot_delete(
            lease.run_id,
            "workflow-1",
            candidate,
            actor="operator",
            lease_owner="worker-1",
        )

    conn = _connect(isolated_db)
    try:
        stored_config = json.loads(
            conn.execute(
                "SELECT config_json FROM ad_cleanup_runs WHERE run_id = ?",
                (lease.run_id,),
            ).fetchone()[0]
        )
        candidate_state = conn.execute(
            "SELECT state FROM ad_cleanup_candidates WHERE run_id = ?",
            (lease.run_id,),
        ).fetchone()[0]
        claim_count = conn.execute(
            "SELECT COUNT(*) FROM ad_cleanup_delete_claims"
        ).fetchone()[0]
    finally:
        conn.close()

    assert stored_config["stale_days"] == 30
    assert candidate_state == "ELIGIBLE"
    assert claim_count == 0


@pytest.mark.parametrize("age_days", [15, 20, 29])
def test_stored_30_day_run_rejects_younger_candidate_at_authorization(
    isolated_db, age_days
):
    """Authorization повторно применяет неизменённый durable порог 30."""
    candidate = _candidate(
        "ad-stored-auth-threshold",
        ordinal=0,
        capacity_before=1,
        age_days=30,
    )
    lease = _prepare_claimable_run(isolated_db, candidates=(candidate,))
    claim = repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        candidate,
        actor="operator",
        lease_owner="worker-1",
    )
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "UPDATE ad_cleanup_candidates SET age_days = ? WHERE run_id = ?",
            (age_days, lease.run_id),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(repository.CleanupClaimError, match="30"):
        repository.create_cleanup_delete_authorization(
            token_id=f"token-stored-threshold-{age_days}",
            claim=claim,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    conn = _connect(isolated_db)
    try:
        stored_config = json.loads(
            conn.execute(
                "SELECT config_json FROM ad_cleanup_runs WHERE run_id = ?",
                (lease.run_id,),
            ).fetchone()[0]
        )
        authorization_count = conn.execute(
            "SELECT COUNT(*) FROM ad_cleanup_delete_authorizations"
        ).fetchone()[0]
    finally:
        conn.close()

    assert stored_config["stale_days"] == 30
    assert authorization_count == 0


def test_authorization_http_start_is_durable_and_one_shot(isolated_db):
    candidate = _candidate("ad-1", ordinal=0, capacity_before=1)
    lease = _prepare_claimable_run(isolated_db, candidates=(candidate,))
    claim = repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        candidate,
        actor="operator",
        lease_owner="worker-1",
    )
    token_id = "token-durable-1"
    repository.create_cleanup_delete_authorization(
        token_id=token_id,
        claim=claim,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    with pytest.raises(repository.CleanupClaimError, match="уже выдавалась"):
        repository.create_cleanup_delete_authorization(
            token_id="token-after-restart",
            claim=claim,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    repository.mark_cleanup_delete_http_started(
        token_id=token_id,
        claim_id=claim.claim_id,
        workflow_id=claim.workflow_id,
        adset_id=claim.adset_id,
        ad_id=claim.ad_id,
        live_capacity_before=claim.capacity_before,
    )
    with pytest.raises(repository.CleanupClaimError):
        repository.mark_cleanup_delete_http_started(
            token_id=token_id,
            claim_id=claim.claim_id,
            workflow_id=claim.workflow_id,
            adset_id=claim.adset_id,
            ad_id=claim.ad_id,
            live_capacity_before=claim.capacity_before,
        )

    conn = _connect(isolated_db)
    try:
        durable = conn.execute(
            """
            SELECT authorization.state AS auth_state, claim.state AS claim_state,
                   candidate.state AS candidate_state,
                   authorization.http_started_at
            FROM ad_cleanup_delete_authorizations AS authorization
            JOIN ad_cleanup_delete_claims AS claim USING (claim_id)
            JOIN ad_cleanup_candidates AS candidate
              ON candidate.run_id = claim.run_id AND candidate.ad_id = claim.ad_id
            """
        ).fetchone()
    finally:
        conn.close()

    assert tuple(durable)[:3] == (
        "HTTP_STARTED",
        "RECONCILE_REQUIRED",
        "RECONCILE_REQUIRED",
    )
    assert durable["http_started_at"] is not None


def test_http_start_rechecks_kill_switch_and_keeps_claim_unconsumed(isolated_db):
    candidate = _candidate("ad-1", ordinal=0, capacity_before=1)
    lease = _prepare_claimable_run(isolated_db, candidates=(candidate,))
    claim = repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        candidate,
        actor="operator",
        lease_owner="worker-1",
    )
    repository.create_cleanup_delete_authorization(
        token_id="token-gate-drift",
        claim=claim,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    conn = _connect(isolated_db)
    try:
        config = json.loads(
            conn.execute(
                "SELECT config_json FROM ad_cleanup_runs WHERE run_id = ?",
                (lease.run_id,),
            ).fetchone()[0]
        )
        config["kill_switch"] = True
        conn.execute(
            "UPDATE ad_cleanup_runs SET config_json = ? WHERE run_id = ?",
            (json.dumps(config, sort_keys=True), lease.run_id),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(repository.CleanupClaimError, match="gates закрыты"):
        repository.mark_cleanup_delete_http_started(
            token_id="token-gate-drift",
            claim_id=claim.claim_id,
            workflow_id=claim.workflow_id,
            adset_id=claim.adset_id,
            ad_id=claim.ad_id,
            live_capacity_before=claim.capacity_before,
        )

    conn = _connect(isolated_db)
    try:
        auth_state = conn.execute(
            "SELECT state FROM ad_cleanup_delete_authorizations"
        ).fetchone()[0]
        claim_state = conn.execute(
            "SELECT state FROM ad_cleanup_delete_claims"
        ).fetchone()[0]
    finally:
        conn.close()
    assert (auth_state, claim_state) == ("ISSUED", "CLAIMED")


@pytest.mark.parametrize(
    "boundary_change",
    [
        {"local_spend_usd": 0.01, "any_positive_delivery": True},
        {"local_leads": 1, "any_positive_outcome": True},
        {"complete": False},
        {"kb_found": False},
    ],
)
def test_fresh_local_evidence_race_blocks_unconsumed_authorization(
    isolated_db, boundary_change
):
    candidate = _candidate("ad-1", ordinal=0, capacity_before=1)
    lease = _prepare_claimable_run(isolated_db, candidates=(candidate,))
    claim = repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        candidate,
        actor="operator",
        lease_owner="worker-1",
    )
    repository.create_cleanup_delete_authorization(
        token_id="token-fresh-race",
        claim=claim,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    with pytest.raises(repository.CleanupClaimError, match="fresh local evidence"):
        repository.prepare_cleanup_delete_boundary(
            claim_id=claim.claim_id,
            workflow_id=claim.workflow_id,
            adset_id=claim.adset_id,
            ad_id=claim.ad_id,
            runtime_config_hash="a" * 64,
            runtime_config_generation=7,
            local_zero_evidence=_fresh_local_zero(**boundary_change),
        )

    conn = _connect(isolated_db)
    try:
        auth_state = conn.execute(
            "SELECT state FROM ad_cleanup_delete_authorizations"
        ).fetchone()[0]
        claim_state = conn.execute(
            "SELECT state FROM ad_cleanup_delete_claims"
        ).fetchone()[0]
    finally:
        conn.close()
    assert (auth_state, claim_state) == ("ISSUED", "CLAIMED")


def test_fresh_boundary_persists_hash_before_http_start(isolated_db):
    candidate = _candidate("ad-1", ordinal=0, capacity_before=1)
    lease = _prepare_claimable_run(isolated_db, candidates=(candidate,))
    claim = repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        candidate,
        actor="operator",
        lease_owner="worker-1",
    )
    repository.create_cleanup_delete_authorization(
        token_id="token-fresh-zero",
        claim=claim,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    evidence_hash = repository.prepare_cleanup_delete_boundary(
        claim_id=claim.claim_id,
        workflow_id=claim.workflow_id,
        adset_id=claim.adset_id,
        ad_id=claim.ad_id,
        runtime_config_hash="a" * 64,
        runtime_config_generation=7,
        local_zero_evidence=_fresh_local_zero(),
    )

    conn = _connect(isolated_db)
    try:
        evidence = json.loads(
            conn.execute(
                "SELECT evidence_json FROM ad_cleanup_delete_claims WHERE claim_id = ?",
                (claim.claim_id,),
            ).fetchone()[0]
        )
        auth_state = conn.execute(
            "SELECT state FROM ad_cleanup_delete_authorizations"
        ).fetchone()[0]
    finally:
        conn.close()
    boundary = evidence["delete_boundary"]
    assert boundary["local_zero_evidence_sha256"] == evidence_hash
    assert boundary["runtime_config_hash"] == "a" * 64
    assert boundary["runtime_config_generation"] == 7
    assert auth_state == "ISSUED"


def test_runtime_config_hash_drift_blocks_before_http_start(isolated_db):
    candidate = _candidate("ad-1", ordinal=0, capacity_before=1)
    lease = _prepare_claimable_run(isolated_db, candidates=(candidate,))
    claim = repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        candidate,
        actor="operator",
        lease_owner="worker-1",
    )
    repository.create_cleanup_delete_authorization(
        token_id="token-config-drift",
        claim=claim,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    with pytest.raises(repository.CleanupClaimError, match="config hash drifted"):
        repository.prepare_cleanup_delete_boundary(
            claim_id=claim.claim_id,
            workflow_id=claim.workflow_id,
            adset_id=claim.adset_id,
            ad_id=claim.ad_id,
            runtime_config_hash="b" * 64,
            runtime_config_generation=7,
            local_zero_evidence=_fresh_local_zero(),
        )

    conn = _connect(isolated_db)
    try:
        states = conn.execute(
            """
            SELECT authorization.state, claim.state
            FROM ad_cleanup_delete_authorizations AS authorization
            JOIN ad_cleanup_delete_claims AS claim USING (claim_id)
            """
        ).fetchone()
    finally:
        conn.close()
    assert tuple(states) == ("ISSUED", "CLAIMED")


def test_atomic_claim_rolls_back_when_audit_insert_fails(isolated_db):
    candidate = _candidate("ad-1", ordinal=0, capacity_before=1)
    lease = _prepare_claimable_run(isolated_db, candidates=(candidate,))
    conn = _connect(isolated_db)
    try:
        conn.execute(
            """
            CREATE TRIGGER fail_delete_attempt
            BEFORE INSERT ON ad_cleanup_audit
            WHEN NEW.action = 'DELETE_ATTEMPT'
            BEGIN
                SELECT RAISE(ABORT, 'forced audit failure');
            END
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(repository.CleanupClaimError, match="automatic retry запрещён"):
        repository.claim_replacement_slot_delete(
            lease.run_id,
            "workflow-1",
            candidate,
            actor="operator",
            lease_owner="worker-1",
        )

    conn = _connect(isolated_db)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM ad_cleanup_delete_claims").fetchone()[0]
            == 0
        )
        state = conn.execute(
            "SELECT state, claim_id FROM ad_cleanup_candidates WHERE run_id = ? AND ad_id = ?",
            (lease.run_id, "ad-1"),
        ).fetchone()
    finally:
        conn.close()
    assert tuple(state) == ("ELIGIBLE", None)


def test_two_distinct_single_ad_claims_allowed_within_exact_deficit(isolated_db):
    first = _candidate("ad-1", ordinal=0, capacity_before=0)
    second_initial = _candidate("ad-2", ordinal=1, capacity_before=0)
    lease = _prepare_claimable_run(
        isolated_db,
        candidates=(first, second_initial),
        max_deletes=2,
    )

    first_claim = repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        first,
        actor="operator",
        lease_owner="worker-1",
    )
    _start_delete_http(first_claim)
    repository.finish_cleanup_delete(
        first_claim.claim_id,
        "DELETED",
        capacity_after=1,
        evidence={"candidate_absent": True, "active_count": 3},
        error=None,
    )
    second_fresh = _candidate("ad-2", ordinal=1, capacity_before=1)
    repository.upsert_cleanup_candidate(
        lease.run_id, second_fresh, lease_owner="worker-1"
    )
    second_claim = repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        second_fresh,
        actor="operator",
        lease_owner="worker-1",
    )
    _start_delete_http(second_claim)
    repository.finish_cleanup_delete(
        second_claim.claim_id,
        "DELETED",
        capacity_after=2,
        evidence={"candidate_absent": True, "active_count": 3},
        error=None,
    )

    conn = _connect(isolated_db)
    try:
        claims = conn.execute(
            "SELECT ad_id, state FROM ad_cleanup_delete_claims ORDER BY claimed_at, ad_id"
        ).fetchall()
        attempts = conn.execute(
            """
            SELECT ad_id FROM ad_cleanup_audit
            WHERE workflow_id = 'workflow-1' AND action = 'DELETE_ATTEMPT'
            ORDER BY ad_id
            """
        ).fetchall()
    finally:
        conn.close()

    assert {tuple(row) for row in claims} == {("ad-1", "DELETED"), ("ad-2", "DELETED")}
    assert [row["ad_id"] for row in attempts] == ["ad-1", "ad-2"]


def test_third_claim_is_rejected_after_exact_cap(isolated_db):
    first = _candidate("ad-1", ordinal=0, capacity_before=0)
    second = _candidate("ad-2", ordinal=1, capacity_before=0)
    third = _candidate("ad-3", ordinal=2, capacity_before=0)
    lease = _prepare_claimable_run(
        isolated_db,
        candidates=(first, second, third),
        max_deletes=2,
    )
    first_claim = repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        first,
        actor="operator",
        lease_owner="worker-1",
    )
    _start_delete_http(first_claim)
    repository.finish_cleanup_delete(
        first_claim.claim_id,
        "DELETED",
        capacity_after=1,
        evidence={"candidate_absent": True},
        error=None,
    )
    second_fresh = _candidate("ad-2", ordinal=1, capacity_before=1)
    repository.upsert_cleanup_candidate(
        lease.run_id, second_fresh, lease_owner="worker-1"
    )
    second_claim = repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        second_fresh,
        actor="operator",
        lease_owner="worker-1",
    )
    _start_delete_http(second_claim)
    repository.finish_cleanup_delete(
        second_claim.claim_id,
        "DELETED",
        capacity_after=2,
        evidence={"candidate_absent": True},
        error=None,
    )
    third_fresh = _candidate("ad-3", ordinal=2, capacity_before=2)
    repository.upsert_cleanup_candidate(
        lease.run_id, third_fresh, lease_owner="worker-1"
    )

    with pytest.raises(repository.CleanupClaimError, match="исчерпан"):
        repository.claim_replacement_slot_delete(
            lease.run_id,
            "workflow-1",
            third_fresh,
            actor="operator",
            lease_owner="worker-1",
        )


def test_unresolved_claim_blocks_resume_and_automatic_retry(isolated_db):
    candidate = _candidate("ad-1", ordinal=0, capacity_before=1)
    lease = _prepare_claimable_run(isolated_db, candidates=(candidate,))
    claim = repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        candidate,
        actor="operator",
        lease_owner="worker-1",
    )
    _start_delete_http(claim)
    repository.finish_cleanup_delete(
        claim.claim_id,
        "RECONCILE_REQUIRED",
        capacity_after=None,
        evidence={"http_result": "unknown"},
        error="timeout after request",
    )

    reacquire = _acquire_slot_run(lease_owner="worker-2")
    unresolved = repository.list_unresolved_delete_claims("adset-1")

    assert reacquire.run_id == lease.run_id
    assert reacquire.acquired is False
    assert reacquire.reason == "terminal_run"
    assert len(unresolved) == 1
    assert unresolved[0]["state"] == "RECONCILE_REQUIRED"
    with pytest.raises(repository.CleanupClaimError):
        repository.claim_replacement_slot_delete(
            lease.run_id,
            "workflow-1",
            candidate,
            actor="operator",
            lease_owner="worker-1",
        )


def test_expired_slot_lease_with_claim_is_blocked_on_reacquire(isolated_db):
    candidate = _candidate("ad-1", ordinal=0, capacity_before=1)
    lease = _prepare_claimable_run(isolated_db, candidates=(candidate,))
    repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        candidate,
        actor="operator",
        lease_owner="worker-1",
    )
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    conn = _connect(isolated_db)
    try:
        conn.execute(
            "UPDATE ad_cleanup_runs SET lease_expires_at = ? WHERE run_id = ?",
            (expired, lease.run_id),
        )
        conn.commit()
    finally:
        conn.close()

    resumed = _acquire_slot_run(lease_owner="worker-2")

    assert resumed.acquired is False
    assert resumed.reason == "reconcile_required"
    assert resumed.phase == "BLOCKED"


def test_finish_delete_requires_capacity_growth_exactly_one(isolated_db):
    candidate = _candidate("ad-1", ordinal=0, capacity_before=1)
    lease = _prepare_claimable_run(isolated_db, candidates=(candidate,))
    claim = repository.claim_replacement_slot_delete(
        lease.run_id,
        "workflow-1",
        candidate,
        actor="operator",
        lease_owner="worker-1",
    )
    _start_delete_http(claim)

    with pytest.raises(repository.CleanupClaimError, match="ровно на один"):
        repository.finish_cleanup_delete(
            claim.claim_id,
            "DELETED",
            capacity_after=1,
            evidence={"candidate_absent": True},
            error=None,
        )

    assert (
        repository.list_unresolved_delete_claims()[0]["state"]
        == "RECONCILE_REQUIRED"
    )


def test_finish_run_requires_same_live_lease_owner(isolated_db):
    lease = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {},
    )
    counters = {
        "discovered": 2,
        "eligible": 1,
        "would_delete": 1,
        "deleted": 0,
        "skipped": 1,
        "warnings": 0,
        "errors": 0,
    }
    with pytest.raises(repository.CleanupLeaseError):
        repository.finish_cleanup_run(
            lease.run_id,
            "COMPLETED",
            counters,
            [],
            lease_owner="worker-2",
        )

    repository.finish_cleanup_run(
        lease.run_id,
        "COMPLETED",
        counters,
        [],
        lease_owner="worker-1",
    )
    conn = _connect(isolated_db)
    try:
        row = conn.execute(
            "SELECT phase, discovered_count, deleted_count, lease_owner FROM ad_cleanup_runs"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(row) == ("COMPLETED", 2, 0, None)


def test_config_and_evidence_are_redacted_before_durable_write(isolated_db):
    lease = repository.acquire_cleanup_run(
        "default",
        "PROACTIVE_DAILY",
        date(2026, 7, 20),
        None,
        "worker-1",
        timedelta(minutes=5),
        {
            "note": "access_token=secret-token",
            "source": "https://example.test/path?access_token=secret-token",
        },
    )
    candidate = _candidate("ad-1", ordinal=0, capacity_before=2)
    candidate["evidence"] = {
        **candidate["evidence"],
        "Authorization": "plain-secret",
    }
    repository.upsert_cleanup_candidate(lease.run_id, candidate, lease_owner="worker-1")

    conn = _connect(isolated_db)
    try:
        run_json = conn.execute(
            "SELECT config_json FROM ad_cleanup_runs WHERE run_id = ?", (lease.run_id,)
        ).fetchone()[0]
        candidate_json = conn.execute(
            "SELECT evidence_json FROM ad_cleanup_candidates WHERE run_id = ?",
            (lease.run_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert "secret-token" not in run_json
    assert "plain-secret" not in candidate_json
    assert "?<redacted>" in run_json
    assert "[REDACTED]" in candidate_json


def test_cleanup_status_returns_typed_city_plans_and_open_cases_first(isolated_db):
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(isolated_db)
    try:
        conn.executemany(
            """
            INSERT INTO launch_recovery_cases (
                case_id, trello_action_id, card_id, source_completed_at,
                scan_since, phase, missing_cities_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, '["CityA"]', ?, ?)
            """,
            [
                (
                    "case-terminal",
                    "action-terminal",
                    "card-terminal",
                    now,
                    now,
                    "NO_ACTION",
                    now,
                    now,
                ),
                (
                    "case-open",
                    "action-open",
                    "card-open",
                    now,
                    now,
                    "REVIEW_REQUIRED",
                    now,
                    now,
                ),
            ],
        )
        conn.execute(
            """
            INSERT INTO launch_recovery_city_plans (
                plan_id, case_id, city, account_kind, account_id, adset_id,
                expected_ad_names_json, expected_ad_count,
                reconcile_from, reconcile_until, phase, found_ad_ids_json,
                media_manifest_sha256, evidence_json, created_at, updated_at
            ) VALUES ('plan-open', 'case-open', 'CityA', 'offline',
                      'account-1', 'adset-1', '["CityA | Креатив"]', 1,
                      ?, ?, 'REVIEW_REQUIRED', '[]', 'manifest-sha', '{}', ?, ?)
            """,
            (now, now, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    status = repository.get_cleanup_status()

    assert [case["case_id"] for case in status["recovery_cases"]] == [
        "case-open",
        "case-terminal",
    ]
    plan = status["recovery_cases"][0]["city_plans"][0]
    assert plan["account_kind"] == "offline"
    assert plan["expected_ad_names"] == ["CityA | Креатив"]
    assert plan["expected_ad_count"] == 1
