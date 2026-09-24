"""Проверки атомарных миграций owner approval только на временных SQLite БД."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

import pytest

from agent import database as decisions_database
from services import creative_intelligence
from services import database_migrations as migrations


CREATED_AT = "2026-07-27T00:00:00+00:00"
LATER_AT = "2026-07-28T00:00:00+00:00"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    return conn


def _apply(path: Path) -> sqlite3.Connection:
    migrations.apply_runtime_migrations(str(path))
    return _connect(path)


def _insert_proposal(
    conn: sqlite3.Connection,
    number: int,
) -> tuple[str, str]:
    proposal_id = f"proposal-{number}"
    claim_id = f"claim-{number}"
    conn.execute(
        """
        INSERT INTO owner_action_proposals (
            proposal_id, proposal_kind, origin, idempotency_key, source_ref,
            requested_by_actor, summary, plan_json, plan_sha256, targets_sha256,
            evidence_sha256, config_version_sha256, proposal_sha256,
            staged_media_root, created_at, valid_until
        ) VALUES (?, 'LAUNCH', 'CRON', ?, ?, 'scheduler', ?, '{}', ?, ?, ?, ?, ?,
                  NULL, ?, ?)
        """,
        (
            proposal_id,
            f"idempotency-{number}",
            f"source-{number}",
            f"proposal {number}",
            _sha(f"plan-{number}"),
            _sha(f"targets-{number}"),
            _sha(f"evidence-{number}"),
            _sha(f"config-{number}"),
            _sha(f"proposal-{number}"),
            CREATED_AT,
            LATER_AT,
        ),
    )
    conn.execute(
        """
        INSERT INTO owner_action_proposal_targets (
            proposal_id, claim_id, ordinal, action_kind, account_id, adset_id,
            subject_id, city, language, intended_payload_json,
            intended_payload_sha256, created_at
        ) VALUES (?, ?, 0, 'CREATE_AD', ?, ?, ?, 'CityA', 'L1', '{}', ?, ?)
        """,
        (
            proposal_id,
            claim_id,
            f"account-{number}",
            f"adset-{number}",
            f"subject-{number}",
            _sha(f"payload-{number}"),
            CREATED_AT,
        ),
    )
    return proposal_id, claim_id


def _insert_approved_lineage(
    conn: sqlite3.Connection,
    number: int,
    *,
    target_count: int = 1,
) -> dict[str, str]:
    proposal_id, claim_id = _insert_proposal(conn, number)
    claim_ids = [claim_id]
    for ordinal in range(1, target_count):
        extra_claim_id = f"claim-{number}-{ordinal}"
        conn.execute(
            """
            INSERT INTO owner_action_proposal_targets (
                proposal_id, claim_id, ordinal, action_kind, account_id,
                adset_id, subject_id, city, language, intended_payload_json,
                intended_payload_sha256, created_at
            ) VALUES (?, ?, ?, 'CREATE_AD', ?, ?, ?, 'CityA', 'L1', '{}',
                      ?, ?)
            """,
            (
                proposal_id,
                extra_claim_id,
                ordinal,
                f"account-{number}-{ordinal}",
                f"adset-{number}-{ordinal}",
                f"subject-{number}-{ordinal}",
                _sha(f"payload-{number}-{ordinal}"),
                CREATED_AT,
            ),
        )
        claim_ids.append(extra_claim_id)
    delivery_id = f"delivery-{number}"
    update_id = 10_000 + number
    token_id = f"token-{number}"
    decision_id = f"decision-{number}"
    job_id = f"job-{number}"
    permit_id = f"permit-{number}"
    attempt_id = f"attempt-{number}"
    callback_query_id = f"callback-{number}"

    conn.execute(
        """
        INSERT INTO telegram_delivery_outbox (
            delivery_id, purpose, proposal_id, generation, dedupe_key,
            rendered_text, rendered_text_sha256, button_spec_json,
            button_spec_sha256, state, created_at
        ) VALUES (?, 'OWNER_PROPOSAL', ?, 1, ?, ?, ?, '[]', ?, 'PENDING', ?)
        """,
        (
            delivery_id,
            proposal_id,
            f"owner-proposal:{number}:1",
            f"proposal {number}",
            _sha(f"rendered-{number}"),
            _sha(f"buttons-{number}"),
            CREATED_AT,
        ),
    )
    conn.execute(
        """
        INSERT INTO telegram_update_inbox (
            update_id, ingress_kind, bot_token_identity_sha256, raw_update_json,
            raw_update_sha256, state, received_at, processed_at
        ) VALUES (?, 'GET_UPDATES', ?, '{}', ?, 'PROCESSED', ?, ?)
        """,
        (
            update_id,
            _sha(f"bot-{number}"),
            _sha(f"update-{number}"),
            CREATED_AT,
            CREATED_AT,
        ),
    )
    conn.execute(
        """
        INSERT INTO owner_callback_tokens (
            token_id, public_nonce, token_mac_sha256, proposal_id, delivery_id,
            delivery_generation, decision_kind, expected_owner_user_id,
            expected_chat_id, expected_message_id, created_at, expires_at,
            bound_at, consumed_at, consumed_update_id,
            consumed_callback_query_id
        ) VALUES (?, ?, ?, ?, ?, 1, 'APPROVE', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            token_id,
            f"nonce-{number}",
            _sha(f"token-{number}"),
            proposal_id,
            delivery_id,
            100 + number,
            200 + number,
            300 + number,
            CREATED_AT,
            LATER_AT,
            CREATED_AT,
            CREATED_AT,
            update_id,
            callback_query_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO owner_action_decisions (
            decision_id, proposal_id, proposal_sha256, decision_kind,
            owner_user_id, chat_id, message_id, delivery_generation,
            telegram_update_id, callback_query_id, callback_token_id,
            trusted_ingress_sha256, recorded_at
        ) VALUES (?, ?, ?, 'APPROVE', ?, ?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            proposal_id,
            _sha(f"proposal-{number}"),
            100 + number,
            200 + number,
            300 + number,
            update_id,
            callback_query_id,
            token_id,
            _sha(f"ingress-{number}"),
            CREATED_AT,
        ),
    )
    conn.execute(
        """
        INSERT INTO owner_execution_jobs (
            job_id, proposal_id, decision_id, state, created_at, updated_at
        ) VALUES (?, ?, ?, 'REVIEWING', ?, ?)
        """,
        (job_id, proposal_id, decision_id, CREATED_AT, CREATED_AT),
    )
    conn.execute(
        """
        INSERT INTO owner_action_lifecycle (
            proposal_id, state, version, delivery_generation,
            active_decision_id, active_job_id, updated_at
        ) VALUES (?, 'LIVE_REVIEW', 1, 1, ?, ?, ?)
        """,
        (proposal_id, decision_id, job_id, CREATED_AT),
    )
    conn.execute(
        """
        INSERT INTO owner_technical_permits (
            permit_id, secret_sha256, proposal_id, decision_id, job_id,
            claim_id, operation_kind, account_id, resource_id,
            exact_payload_sha256, manifest_json, manifest_sha256,
            live_evidence_sha256, phase, sequence_no, issued_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'CREATE_AD', ?, ?, ?, '{}', ?, ?,
                  'ISSUED', 1, ?, ?)
        """,
        (
            permit_id,
            _sha(f"secret-{number}"),
            proposal_id,
            decision_id,
            job_id,
            claim_id,
            f"account-{number}",
            f"adset-{number}",
            _sha(f"payload-{number}"),
            _sha(f"manifest-{number}"),
            _sha(f"live-{number}"),
            CREATED_AT,
            LATER_AT,
        ),
    )
    conn.execute(
        """
        UPDATE owner_execution_jobs
        SET state = 'PERMIT_ISSUED', updated_at = ?
        WHERE job_id = ?
        """,
        (CREATED_AT, job_id),
    )
    conn.execute(
        """
        UPDATE owner_action_lifecycle
        SET state = 'PERMIT_ISSUED', version = 2,
            latest_reason_code = 'PERMIT_ISSUED', updated_at = ?
        WHERE proposal_id = ?
        """,
        (CREATED_AT, proposal_id),
    )
    conn.execute(
        """
        UPDATE owner_technical_permits
        SET phase = 'CONSUMED', consumed_at = ?
        WHERE permit_id = ?
        """,
        (CREATED_AT, permit_id),
    )
    conn.execute(
        """
        INSERT INTO owner_action_attempts (
            attempt_id, permit_id, proposal_id, decision_id, job_id, claim_id,
            operation_kind, account_id, resource_id, exact_payload_sha256,
            state, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'CREATE_AD', ?, ?, ?,
                  'ATTEMPT_STARTED', ?)
        """,
        (
            attempt_id,
            permit_id,
            proposal_id,
            decision_id,
            job_id,
            claim_id,
            f"account-{number}",
            f"adset-{number}",
            _sha(f"payload-{number}"),
            CREATED_AT,
        ),
    )
    conn.execute(
        """
        UPDATE owner_execution_jobs
        SET state = 'ATTEMPT_STARTED', attempts = 1, updated_at = ?
        WHERE job_id = ?
        """,
        (CREATED_AT, job_id),
    )
    conn.execute(
        """
        UPDATE owner_action_lifecycle
        SET state = 'ATTEMPT_STARTED', version = 3,
            latest_reason_code = 'ATTEMPT_STARTED', updated_at = ?
        WHERE proposal_id = ?
        """,
        (CREATED_AT, proposal_id),
    )
    result = {
        "proposal_id": proposal_id,
        "claim_id": claim_id,
        "decision_id": decision_id,
        "job_id": job_id,
        "permit_id": permit_id,
        "attempt_id": attempt_id,
    }
    result.update(
        {
            f"claim_id_{ordinal}": extra_claim_id
            for ordinal, extra_claim_id in enumerate(claim_ids[1:], start=1)
        }
    )
    return result


def _insert_unbound_delivery_generation(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    generation: int,
    suffix: str,
) -> str:
    delivery_id = f"delivery-{suffix}"
    conn.execute(
        """
        INSERT INTO telegram_delivery_outbox (
            delivery_id, purpose, proposal_id, generation, dedupe_key,
            rendered_text, rendered_text_sha256, button_spec_json,
            button_spec_sha256, state, created_at
        ) VALUES (?, 'OWNER_PROPOSAL', ?, ?, ?, ?, ?, '[]', ?, 'PENDING', ?)
        """,
        (
            delivery_id,
            proposal_id,
            generation,
            f"owner-proposal:{proposal_id}:generation:{generation}",
            f"proposal {suffix}",
            _sha(f"rendered-{suffix}"),
            _sha(f"buttons-{suffix}"),
            CREATED_AT,
        ),
    )
    for decision_kind in ("APPROVE", "REJECT", "POSTPONE"):
        conn.execute(
            """
            INSERT INTO owner_callback_tokens (
                token_id, public_nonce, token_mac_sha256, proposal_id,
                delivery_id, delivery_generation, decision_kind,
                expected_owner_user_id, expected_chat_id, created_at,
                expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 101, 201, ?, ?)
            """,
            (
                f"token-{suffix}-{decision_kind.lower()}",
                f"nonce-{suffix}-{decision_kind.lower()}",
                _sha(f"token-{suffix}-{decision_kind.lower()}"),
                proposal_id,
                delivery_id,
                generation,
                decision_kind,
                CREATED_AT,
                LATER_AT,
            ),
        )
    return delivery_id


def _insert_pending_owner_lineage(
    conn: sqlite3.Connection,
    number: int,
) -> tuple[str, str]:
    proposal_id, _ = _insert_proposal(conn, number)
    delivery_id = _insert_unbound_delivery_generation(
        conn,
        proposal_id=proposal_id,
        generation=1,
        suffix=f"{number}-generation-1",
    )
    conn.execute(
        """
        INSERT INTO owner_action_lifecycle (
            proposal_id, state, version, delivery_generation, updated_at
        ) VALUES (?, 'PENDING_OWNER', 1, 1, ?)
        """,
        (proposal_id, CREATED_AT),
    )
    return proposal_id, delivery_id


def _move_to_next_claim_review(
    conn: sqlite3.Connection,
    lineage: dict[str, str],
    *,
    attempt_state: str = "CONFIRMED",
) -> None:
    conn.execute(
        """
        UPDATE owner_action_attempts
        SET state = ?, completed_at = ?, last_reason_code = ?
        WHERE attempt_id = ?
        """,
        (
            attempt_state,
            LATER_AT,
            attempt_state,
            lineage["attempt_id"],
        ),
    )
    conn.execute(
        """
        UPDATE owner_execution_jobs
        SET state = 'QUEUED', updated_at = ?
        WHERE job_id = ?
        """,
        (LATER_AT, lineage["job_id"]),
    )
    conn.execute(
        """
        UPDATE owner_action_lifecycle
        SET state = 'EXECUTION_QUEUED', version = version + 1,
            latest_reason_code = 'NEXT_CLAIM', updated_at = ?
        WHERE proposal_id = ?
        """,
        (LATER_AT, lineage["proposal_id"]),
    )
    conn.execute(
        """
        UPDATE owner_execution_jobs
        SET state = 'REVIEWING', updated_at = ?
        WHERE job_id = ?
        """,
        (LATER_AT, lineage["job_id"]),
    )
    conn.execute(
        """
        UPDATE owner_action_lifecycle
        SET state = 'LIVE_REVIEW', version = version + 1,
            latest_reason_code = 'LIVE_REVIEW', updated_at = ?
        WHERE proposal_id = ?
        """,
        (LATER_AT, lineage["proposal_id"]),
    )


def _issue_extra_claim_permit(
    conn: sqlite3.Connection,
    lineage: dict[str, str],
    *,
    permit_id: str,
    sequence_no: int,
    live_evidence: str,
) -> None:
    claim_id = lineage["claim_id_1"]
    target = conn.execute(
        """
        SELECT action_kind, account_id, adset_id, subject_id,
               intended_payload_sha256
        FROM owner_action_proposal_targets
        WHERE proposal_id = ? AND claim_id = ?
        """,
        (lineage["proposal_id"], claim_id),
    ).fetchone()
    assert target is not None
    conn.execute(
        """
        INSERT INTO owner_technical_permits (
            permit_id, secret_sha256, proposal_id, decision_id, job_id,
            claim_id, operation_kind, account_id, resource_id,
            exact_payload_sha256, manifest_json, manifest_sha256,
            live_evidence_sha256, phase, sequence_no, issued_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, 'ISSUED', ?, ?, ?)
        """,
        (
            permit_id,
            _sha(f"secret-{permit_id}"),
            lineage["proposal_id"],
            lineage["decision_id"],
            lineage["job_id"],
            claim_id,
            str(target[0]),
            str(target[1]),
            str(target[2] or target[3]),
            str(target[4]),
            _sha(f"manifest-{permit_id}"),
            _sha(live_evidence),
            sequence_no,
            CREATED_AT,
            LATER_AT,
        ),
    )


def test_migration_files_contain_statement_bodies_only() -> None:
    wrapper = re.compile(
        r"^\s*(?:PRAGMA\s+foreign_keys|BEGIN(?:\s+IMMEDIATE)?|COMMIT)\s*;",
        re.IGNORECASE | re.MULTILINE,
    )
    for version in (19, 20, 21, 22, 23, 24, 25, 26, 27, 28):
        sql = migrations.MIGRATION_FILES[version].read_text(encoding="utf-8")
        assert wrapper.search(sql) is None, version


def test_runner_tracks_raw_checksums_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "owner.db"

    first = migrations.apply_runtime_migrations(str(db_path))
    second = migrations.apply_runtime_migrations(str(db_path))
    health = migrations.verify_runtime_schema(str(db_path))

    assert first.applied_versions == (19, 20, 21, 22, 23, 24, 25, 26, 27, 28)
    assert second.applied_versions == ()
    assert second.verified_versions == (19, 20, 21, 22, 23, 24, 25, 26, 27, 28)
    assert health.healthy is True
    assert health.verified_versions == (19, 20, 21, 22, 23, 24, 25, 26, 27, 28)

    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT version, name, content_sha256, application_id
            FROM schema_migrations
            ORDER BY version
            """
        ).fetchall()
    assert [row[0] for row in rows] == [19, 20, 21, 22, 23, 24, 25, 26, 27, 28]
    for version, name, checksum, application_id in rows:
        path = migrations.MIGRATION_FILES[version]
        assert name == path.name
        assert checksum == hashlib.sha256(path.read_bytes()).hexdigest()
        assert application_id == migrations.APPLICATION_ID


def test_failed_migration_rolls_back_schema_and_checksum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "atomic.db"
    migration_19 = tmp_path / "019_good.sql"
    migration_20 = tmp_path / "020_broken.sql"
    migration_19.write_text(
        "CREATE TABLE IF NOT EXISTS committed_table (id TEXT PRIMARY KEY);\n",
        encoding="utf-8",
    )
    migration_20.write_text(
        "CREATE TABLE IF NOT EXISTS must_rollback (id TEXT PRIMARY KEY);\n"
        "THIS IS NOT VALID SQL;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        migrations,
        "MIGRATION_FILES",
        {19: migration_19, 20: migration_20},
    )

    with pytest.raises(sqlite3.Error):
        migrations.apply_runtime_migrations(str(db_path), required=(19, 20))

    with _connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
    assert "committed_table" in tables
    assert "must_rollback" not in tables
    assert versions == [19]


def test_checksum_mismatch_blocks_later_sql_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "checksum.db"
    migration_19 = tmp_path / "019_checksum.sql"
    migration_20 = tmp_path / "020_later.sql"
    migration_19.write_bytes(b"CREATE TABLE first_shape (id TEXT);\n")
    migration_20.write_bytes(b"CREATE TABLE forbidden_later (id TEXT);\n")
    monkeypatch.setattr(
        migrations,
        "MIGRATION_FILES",
        {19: migration_19, 20: migration_20},
    )
    migrations.apply_runtime_migrations(str(db_path), required=(19,))

    migration_19.write_bytes(
        b"CREATE TABLE first_shape (id TEXT, changed TEXT);\n"
    )
    with pytest.raises(migrations.MigrationChecksumMismatch):
        migrations.apply_runtime_migrations(str(db_path), required=(19, 20))

    with _connect(db_path) as conn:
        later = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'forbidden_later'
            """
        ).fetchone()
    assert later is None


@pytest.mark.parametrize(
    ("damage_sql", "message"),
    [
        (
            "DROP TRIGGER trg_owner_proposals_no_update",
            "отсутствуют обязательные объекты",
        ),
        (
            "DROP INDEX uq_owner_terminal_decision",
            "отсутствуют обязательные объекты",
        ),
        (
            "ALTER TABLE owner_action_events ADD COLUMN injected TEXT",
            "Колонки owner_action_events не совпадают",
        ),
    ],
)
def test_schema_manifest_fails_closed_on_manual_damage(
    tmp_path: Path,
    damage_sql: str,
    message: str,
) -> None:
    db_path = tmp_path / "damaged.db"
    migrations.apply_runtime_migrations(str(db_path))
    with _connect(db_path) as conn:
        conn.execute(damage_sql)
        conn.commit()

    with pytest.raises(migrations.SchemaVerificationError, match=message):
        migrations.verify_runtime_schema(str(db_path))


def test_decision_job_and_attempt_require_exact_trusted_lineage(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lineage.db"
    conn = _apply(db_path)
    try:
        lineage = _insert_approved_lineage(conn, 1)

        # Нельзя выдать новый permit после попытки или с чужим resource.
        with pytest.raises(
            sqlite3.IntegrityError,
            match="permit exact claim lineage mismatch",
        ):
            conn.execute(
                """
                INSERT INTO owner_technical_permits (
                    permit_id, secret_sha256, proposal_id, decision_id, job_id,
                    claim_id, operation_kind, account_id, resource_id,
                    exact_payload_sha256, manifest_json, manifest_sha256,
                    live_evidence_sha256, phase, sequence_no, issued_at,
                    expires_at
                ) VALUES ('issued-only', ?, ?, ?, ?, ?, 'CREATE_AD',
                          'account-1', 'subject-1', ?, '{}', ?, ?,
                          'ISSUED', 2, ?, ?)
                """,
                (
                    _sha("issued-secret"),
                    lineage["proposal_id"],
                    lineage["decision_id"],
                    lineage["job_id"],
                    lineage["claim_id"],
                    _sha("payload-1"),
                    _sha("issued-manifest"),
                    _sha("issued-live"),
                    CREATED_AT,
                    LATER_AT,
                ),
            )
    finally:
        conn.close()


def test_cross_proposal_watchdog_scheduler_and_outcome_are_rejected(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "cross-proposal.db"
    conn = _apply(db_path)
    try:
        first = _insert_approved_lineage(conn, 1)
        second = _insert_approved_lineage(conn, 2)

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                """
                INSERT INTO launch_watchdogs (
                    watchdog_id, proposal_id, decision_id, job_id, state,
                    expected_count, verify_deadline_at, created_at, updated_at
                ) VALUES ('wrong-watchdog', ?, ?, ?, 'EXECUTED', 1, ?, ?, ?)
                """,
                (
                    first["proposal_id"],
                    second["decision_id"],
                    second["job_id"],
                    LATER_AT,
                    CREATED_AT,
                    CREATED_AT,
                ),
            )

        conn.execute(
            """
            INSERT INTO launch_watchdogs (
                watchdog_id, proposal_id, decision_id, job_id, state,
                expected_count, verify_deadline_at, created_at, updated_at
            ) VALUES ('watchdog-1', ?, ?, ?, 'EXECUTED', 1, ?, ?, ?)
            """,
            (
                first["proposal_id"],
                first["decision_id"],
                first["job_id"],
                LATER_AT,
                CREATED_AT,
                CREATED_AT,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                """
                INSERT INTO launch_watchdog_targets (
                    proposal_id, watchdog_id, claim_id, account_id, adset_id,
                    expected_ad_name, expected_fingerprint, created_ad_id,
                    expected_configured_status, expected_effective_status,
                    created_at
                ) VALUES (?, 'watchdog-1', ?, 'account-1', 'adset-1', 'ad',
                          ?, 'created-cross', 'ACTIVE', 'ACTIVE', ?)
                """,
                (
                    first["proposal_id"],
                    second["claim_id"],
                    _sha("fingerprint-cross"),
                    CREATED_AT,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                """
                INSERT INTO scheduler_action_runs (
                    run_id, scheduler_name, slot_key, state, proposal_id,
                    watchdog_id, created_at, updated_at
                ) VALUES ('run-cross', 'launcher', 'slot-1', 'VERIFYING',
                          ?, 'watchdog-1', ?, ?)
                """,
                (second["proposal_id"], CREATED_AT, CREATED_AT),
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                """
                INSERT INTO owner_action_outcomes (
                    outcome_id, proposal_id, job_id, claim_id, attempt_id,
                    horizon, observed_at, metrics_json, metrics_sha256
                ) VALUES ('outcome-cross', ?, ?, ?, ?, 'IMMEDIATE', ?, '{}', ?)
                """,
                (
                    first["proposal_id"],
                    first["job_id"],
                    first["claim_id"],
                    second["attempt_id"],
                    CREATED_AT,
                    _sha("outcome-cross"),
                ),
            )
    finally:
        conn.close()


def test_lifecycle_requires_legal_cas_transition(tmp_path: Path) -> None:
    db_path = tmp_path / "lifecycle.db"
    conn = _apply(db_path)
    try:
        proposal_id, _ = _insert_proposal(conn, 1)
        conn.execute(
            """
            INSERT INTO owner_action_lifecycle (
                proposal_id, state, version, updated_at
            ) VALUES (?, 'DELIVERY_PENDING', 1, ?)
            """,
            (proposal_id, CREATED_AT),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="lifecycle CAS version mismatch",
        ):
            conn.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = 'PENDING_OWNER', updated_at = ?
                WHERE proposal_id = ?
                """,
                (LATER_AT, proposal_id),
            )
        conn.execute(
            """
            UPDATE owner_action_lifecycle
            SET state = 'PENDING_OWNER', version = 2, updated_at = ?
            WHERE proposal_id = ?
            """,
            (LATER_AT, proposal_id),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="illegal owner action lifecycle transition",
        ):
            conn.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = 'EXECUTED', version = 3, updated_at = ?
                WHERE proposal_id = ?
                """,
                (LATER_AT, proposal_id),
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "reason_code",
    ["CALLBACK_SECRET_ROTATION", "REDELIVERY"],
)
def test_pending_owner_redelivery_atomically_replaces_generation(
    tmp_path: Path,
    reason_code: str,
) -> None:
    db_path = tmp_path / f"redelivery-{reason_code.lower()}.db"
    conn = _apply(db_path)
    try:
        proposal_id, _ = _insert_pending_owner_lineage(conn, 21)
        conn.commit()

        with conn:
            delivery_id = _insert_unbound_delivery_generation(
                conn,
                proposal_id=proposal_id,
                generation=2,
                suffix=f"21-generation-2-{reason_code.lower()}",
            )
            updated = conn.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = 'DELIVERY_PENDING', version = version + 1,
                    delivery_generation = 2, latest_reason_code = ?,
                    updated_at = ?
                WHERE proposal_id = ? AND state = 'PENDING_OWNER'
                  AND version = 1
                """,
                (reason_code, LATER_AT, proposal_id),
            ).rowcount
            assert updated == 1

        lifecycle = conn.execute(
            """
            SELECT state, version, delivery_generation, active_decision_id,
                   active_job_id
            FROM owner_action_lifecycle
            WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()
        assert lifecycle == ("DELIVERY_PENDING", 2, 2, None, None)
        assert conn.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT decision_kind)
            FROM owner_callback_tokens
            WHERE proposal_id = ? AND delivery_id = ?
              AND delivery_generation = 2 AND revoked_at IS NULL
              AND consumed_at IS NULL AND expected_message_id IS NULL
              AND bound_at IS NULL
            """,
            (proposal_id, delivery_id),
        ).fetchone() == (3, 3)
        assert conn.execute(
            """
            SELECT COUNT(*)
            FROM owner_callback_tokens
            WHERE proposal_id = ? AND delivery_generation = 1
              AND consumed_at IS NULL AND revoked_at IS NOT NULL
              AND revoke_reason = 'NEW_DELIVERY_GENERATION'
            """,
            (proposal_id,),
        ).fetchone()[0] == 3
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("version", "reason_code", "message"),
    [
        (2, "NOT_ALLOWED", "illegal owner action lifecycle transition"),
        (1, "REDELIVERY", "lifecycle CAS version mismatch"),
    ],
)
def test_invalid_pending_owner_redelivery_rolls_back_new_generation(
    tmp_path: Path,
    version: int,
    reason_code: str,
    message: str,
) -> None:
    db_path = tmp_path / f"redelivery-invalid-{version}.db"
    conn = _apply(db_path)
    try:
        proposal_id, _ = _insert_pending_owner_lineage(conn, 22)
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match=message):
            with conn:
                _insert_unbound_delivery_generation(
                    conn,
                    proposal_id=proposal_id,
                    generation=2,
                    suffix=f"22-generation-2-{version}",
                )
                conn.execute(
                    """
                    UPDATE owner_action_lifecycle
                    SET state = 'DELIVERY_PENDING', version = ?,
                        delivery_generation = 2, latest_reason_code = ?,
                        updated_at = ?
                    WHERE proposal_id = ?
                    """,
                    (version, reason_code, LATER_AT, proposal_id),
                )

        assert conn.execute(
            """
            SELECT state, version, delivery_generation
            FROM owner_action_lifecycle
            WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone() == ("PENDING_OWNER", 1, 1)
        assert conn.execute(
            """
            SELECT COUNT(*) FROM telegram_delivery_outbox
            WHERE proposal_id = ? AND generation = 2
            """,
            (proposal_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT COUNT(*) FROM owner_callback_tokens
            WHERE proposal_id = ? AND delivery_generation = 1
              AND consumed_at IS NULL AND revoked_at IS NULL
            """,
            (proposal_id,),
        ).fetchone()[0] == 3
    finally:
        conn.close()


def test_pending_owner_redelivery_requires_complete_generation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "redelivery-incomplete.db"
    conn = _apply(db_path)
    try:
        proposal_id, _ = _insert_pending_owner_lineage(conn, 23)
        conn.commit()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="illegal owner action lifecycle transition",
        ):
            conn.execute(
                """
                UPDATE owner_action_lifecycle
                SET state = 'DELIVERY_PENDING', version = 2,
                    delivery_generation = 2,
                    latest_reason_code = 'REDELIVERY', updated_at = ?
                WHERE proposal_id = ?
                """,
                (LATER_AT, proposal_id),
            )
    finally:
        conn.close()


def test_pending_owner_redelivery_rejects_existing_decision_and_job(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "redelivery-after-decision.db"
    conn = _apply(db_path)
    try:
        proposal_id, delivery_id = _insert_pending_owner_lineage(conn, 25)
        update_id = 25_000
        conn.execute(
            """
            INSERT INTO telegram_update_inbox (
                update_id, ingress_kind, bot_token_identity_sha256,
                raw_update_json, raw_update_sha256, state, received_at,
                processed_at
            ) VALUES (?, 'GET_UPDATES', ?, '{}', ?, 'PROCESSED', ?, ?)
            """,
            (
                update_id,
                _sha("bot-25"),
                _sha("update-25"),
                CREATED_AT,
                CREATED_AT,
            ),
        )
        token = conn.execute(
            """
            SELECT token_id
            FROM owner_callback_tokens
            WHERE proposal_id = ? AND decision_kind = 'APPROVE'
            """,
            (proposal_id,),
        ).fetchone()
        assert token is not None
        token_id = str(token[0])
        conn.execute(
            """
            UPDATE owner_callback_tokens
            SET expected_message_id = 301, bound_at = ?
            WHERE token_id = ?
            """,
            (CREATED_AT, token_id),
        )
        conn.execute(
            """
            UPDATE owner_callback_tokens
            SET consumed_at = ?, consumed_update_id = ?,
                consumed_callback_query_id = 'callback-25'
            WHERE token_id = ?
            """,
            (CREATED_AT, update_id, token_id),
        )
        proposal_sha256 = conn.execute(
            """
            SELECT proposal_sha256 FROM owner_action_proposals
            WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()
        assert proposal_sha256 is not None
        conn.execute(
            """
            INSERT INTO owner_action_decisions (
                decision_id, proposal_id, proposal_sha256, decision_kind,
                owner_user_id, chat_id, message_id, delivery_generation,
                telegram_update_id, callback_query_id, callback_token_id,
                trusted_ingress_sha256, recorded_at
            ) VALUES (
                'decision-25', ?, ?, 'APPROVE', 101, 201, 301, 1, ?,
                'callback-25', ?, ?, ?
            )
            """,
            (
                proposal_id,
                str(proposal_sha256[0]),
                update_id,
                token_id,
                _sha("trusted-ingress-25"),
                CREATED_AT,
            ),
        )
        conn.execute(
            """
            INSERT INTO owner_execution_jobs (
                job_id, proposal_id, decision_id, state, created_at, updated_at
            ) VALUES ('job-25', ?, 'decision-25', 'QUEUED', ?, ?)
            """,
            (proposal_id, CREATED_AT, CREATED_AT),
        )
        conn.commit()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="illegal owner action lifecycle transition",
        ):
            with conn:
                _insert_unbound_delivery_generation(
                    conn,
                    proposal_id=proposal_id,
                    generation=2,
                    suffix="25-generation-2",
                )
                conn.execute(
                    """
                    UPDATE owner_action_lifecycle
                    SET state = 'DELIVERY_PENDING', version = 2,
                        delivery_generation = 2,
                        latest_reason_code = 'REDELIVERY', updated_at = ?
                    WHERE proposal_id = ?
                    """,
                    (LATER_AT, proposal_id),
                )

        assert conn.execute(
            """
            SELECT state, version, delivery_generation
            FROM owner_action_lifecycle
            WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone() == ("PENDING_OWNER", 1, 1)
        assert conn.execute(
            """
            SELECT COUNT(*) FROM telegram_delivery_outbox
            WHERE proposal_id = ? AND delivery_id <> ?
            """,
            (proposal_id, delivery_id),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_delivery_pending_to_pending_owner_remains_legal(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "initial-delivery.db"
    conn = _apply(db_path)
    try:
        proposal_id, _ = _insert_proposal(conn, 24)
        delivery_id = _insert_unbound_delivery_generation(
            conn,
            proposal_id=proposal_id,
            generation=1,
            suffix="24-generation-1",
        )
        conn.execute(
            """
            INSERT INTO owner_action_lifecycle (
                proposal_id, state, version, delivery_generation, updated_at
            ) VALUES (?, 'DELIVERY_PENDING', 1, 0, ?)
            """,
            (proposal_id, CREATED_AT),
        )
        conn.execute(
            """
            UPDATE telegram_delivery_outbox
            SET state = 'SENT', telegram_chat_id = 201,
                telegram_message_id = 301, sent_at = ?
            WHERE delivery_id = ?
            """,
            (LATER_AT, delivery_id),
        )
        conn.execute(
            """
            UPDATE owner_callback_tokens
            SET expected_message_id = 301, bound_at = ?
            WHERE delivery_id = ?
            """,
            (LATER_AT, delivery_id),
        )
        conn.execute(
            """
            UPDATE owner_action_lifecycle
            SET state = 'PENDING_OWNER', version = 2,
                delivery_generation = 1, latest_reason_code = 'DELIVERED',
                updated_at = ?
            WHERE proposal_id = ?
            """,
            (LATER_AT, proposal_id),
        )

        assert conn.execute(
            """
            SELECT state, version, delivery_generation
            FROM owner_action_lifecycle
            WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone() == ("PENDING_OWNER", 2, 1)
    finally:
        conn.close()


def test_claim_requeue_requires_terminal_known_attempt(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "claim-requeue.db"
    conn = _apply(db_path)
    try:
        lineage = _insert_approved_lineage(conn, 31, target_count=2)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="claim requeue requires terminal-known attempts",
        ):
            conn.execute(
                """
                UPDATE owner_execution_jobs
                SET state = 'QUEUED', updated_at = ?
                WHERE job_id = ?
                """,
                (LATER_AT, lineage["job_id"]),
            )

        _move_to_next_claim_review(conn, lineage)
        assert conn.execute(
            """
            SELECT j.state, l.state, l.version
            FROM owner_execution_jobs j
            JOIN owner_action_lifecycle l USING (proposal_id)
            WHERE j.job_id = ?
            """,
            (lineage["job_id"],),
        ).fetchone() == ("REVIEWING", "LIVE_REVIEW", 5)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("target_count", "attempt_state", "message"),
    [
        (
            2,
            "RECONCILE_REQUIRED",
            "claim requeue requires terminal-known attempts",
        ),
        (1, "CONFIRMED", "claim requeue requires untouched claim"),
    ],
)
def test_claim_requeue_fails_closed_without_safe_next_claim(
    tmp_path: Path,
    target_count: int,
    attempt_state: str,
    message: str,
) -> None:
    db_path = tmp_path / f"claim-requeue-{attempt_state.lower()}.db"
    conn = _apply(db_path)
    try:
        lineage = _insert_approved_lineage(
            conn,
            32 + target_count,
            target_count=target_count,
        )
        conn.execute(
            """
            UPDATE owner_action_attempts
            SET state = ?, completed_at = ?, last_reason_code = ?
            WHERE attempt_id = ?
            """,
            (
                attempt_state,
                LATER_AT,
                attempt_state,
                lineage["attempt_id"],
            ),
        )

        with pytest.raises(sqlite3.IntegrityError, match=message):
            conn.execute(
                """
                UPDATE owner_execution_jobs
                SET state = 'QUEUED', updated_at = ?
                WHERE job_id = ?
                """,
                (LATER_AT, lineage["job_id"]),
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("terminal_phase", "terminal_reason"),
    [("EXPIRED", "TTL_EXPIRED"), ("REVOKED", "LIVE_REVIEW_REPLACED")],
)
def test_permit_reissue_and_exact_claim_lineage_rules(
    tmp_path: Path,
    terminal_phase: str,
    terminal_reason: str,
) -> None:
    db_path = tmp_path / f"permit-reissue-{terminal_phase.lower()}.db"
    conn = _apply(db_path)
    try:
        lineage = _insert_approved_lineage(conn, 41, target_count=2)
        _move_to_next_claim_review(conn, lineage)

        with pytest.raises(
            sqlite3.IntegrityError,
            match="permit exact claim lineage mismatch",
        ):
            conn.execute(
                """
                INSERT INTO owner_technical_permits (
                    permit_id, secret_sha256, proposal_id, decision_id, job_id,
                    claim_id, operation_kind, account_id, resource_id,
                    exact_payload_sha256, manifest_json, manifest_sha256,
                    live_evidence_sha256, phase, sequence_no, issued_at,
                    expires_at
                ) VALUES (
                    'permit-wrong-claim', ?, ?, ?, ?, ?, 'CREATE_AD',
                    'account-41', 'adset-41', ?, '{}', ?, ?, 'ISSUED', 1, ?, ?
                )
                """,
                (
                    _sha("secret-wrong-claim"),
                    lineage["proposal_id"],
                    lineage["decision_id"],
                    lineage["job_id"],
                    lineage["claim_id_1"],
                    _sha("payload-41"),
                    _sha("manifest-wrong-claim"),
                    _sha("live-wrong-claim"),
                    CREATED_AT,
                    LATER_AT,
                ),
            )

        _issue_extra_claim_permit(
            conn,
            lineage,
            permit_id="permit-41-extra-1",
            sequence_no=1,
            live_evidence="live-41-extra-1",
        )
        conn.execute(
            """
            UPDATE owner_technical_permits
            SET phase = ?, revoked_at = ?, revoke_reason = ?
            WHERE permit_id = 'permit-41-extra-1'
            """,
            (terminal_phase, LATER_AT, terminal_reason),
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="permit reissue requires fresh live review",
        ):
            _issue_extra_claim_permit(
                conn,
                lineage,
                permit_id="permit-41-stale-review",
                sequence_no=2,
                live_evidence="live-41-extra-1",
            )

        _issue_extra_claim_permit(
            conn,
            lineage,
            permit_id="permit-41-extra-2",
            sequence_no=2,
            live_evidence="live-41-extra-2",
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="previous permit is not replaceable",
        ):
            _issue_extra_claim_permit(
                conn,
                lineage,
                permit_id="permit-41-open-replacement",
                sequence_no=3,
                live_evidence="live-41-extra-3",
            )

        conn.execute(
            """
            UPDATE owner_technical_permits
            SET phase = 'CONSUMED', consumed_at = ?
            WHERE permit_id = 'permit-41-extra-2'
            """,
            (LATER_AT,),
        )
        target = conn.execute(
            """
            SELECT action_kind, account_id, adset_id, subject_id,
                   intended_payload_sha256
            FROM owner_action_proposal_targets
            WHERE proposal_id = ? AND claim_id = ?
            """,
            (lineage["proposal_id"], lineage["claim_id_1"]),
        ).fetchone()
        assert target is not None
        with pytest.raises(
            sqlite3.IntegrityError,
            match="attempt requires consumed exact-lineage permit",
        ):
            conn.execute(
                """
                INSERT INTO owner_action_attempts (
                    attempt_id, permit_id, proposal_id, decision_id, job_id,
                    claim_id, operation_kind, account_id, resource_id,
                    exact_payload_sha256, state, started_at
                ) VALUES (
                    'attempt-41-wrong-resource', 'permit-41-extra-2',
                    ?, ?, ?, ?, ?, ?, 'wrong-resource', ?,
                    'ATTEMPT_STARTED', ?
                )
                """,
                (
                    lineage["proposal_id"],
                    lineage["decision_id"],
                    lineage["job_id"],
                    lineage["claim_id_1"],
                    str(target[0]),
                    str(target[1]),
                    str(target[4]),
                    LATER_AT,
                ),
            )
        conn.execute(
            """
            INSERT INTO owner_action_attempts (
                attempt_id, permit_id, proposal_id, decision_id, job_id,
                claim_id, operation_kind, account_id, resource_id,
                exact_payload_sha256, state, started_at
            ) VALUES (
                'attempt-41-extra', 'permit-41-extra-2', ?, ?, ?, ?, ?, ?, ?,
                ?, 'ATTEMPT_STARTED', ?
            )
            """,
            (
                lineage["proposal_id"],
                lineage["decision_id"],
                lineage["job_id"],
                lineage["claim_id_1"],
                str(target[0]),
                str(target[1]),
                str(target[2] or target[3]),
                str(target[4]),
                LATER_AT,
            ),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="attempted claim cannot receive another permit",
        ):
            _issue_extra_claim_permit(
                conn,
                lineage,
                permit_id="permit-41-after-attempt",
                sequence_no=3,
                live_evidence="live-41-extra-3",
            )
    finally:
        conn.close()


def test_immutable_business_facts_reject_update_and_delete(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "immutable.db"
    conn = _apply(db_path)
    try:
        lineage = _insert_approved_lineage(conn, 1)
        proposal_id = lineage["proposal_id"]
        claim_id = lineage["claim_id"]

        conn.execute(
            """
            INSERT INTO owner_action_evidence (
                evidence_id, proposal_id, evidence_kind, source_system,
                subject_id, observed_at, complete, payload_json, payload_sha256,
                created_at
            ) VALUES ('evidence-1', ?, 'LIVE_INVENTORY', 'META', 'subject-1',
                      ?, 1, '{}', ?, ?)
            """,
            (proposal_id, CREATED_AT, _sha("evidence-row"), CREATED_AT),
        )
        conn.execute(
            """
            INSERT INTO owner_action_events (
                event_id, proposal_id, event_seq, event_type, actor,
                payload_json, payload_sha256, created_at
            ) VALUES ('event-1', ?, 1, 'PROPOSED', 'scheduler', '{}', ?, ?)
            """,
            (proposal_id, _sha("event-1"), CREATED_AT),
        )
        conn.execute(
            """
            INSERT INTO coverage_snapshots (
                snapshot_id, started_at, completed_at, fetch_complete,
                configured_group_count, observed_group_count, page_count,
                inventory_sha256
            ) VALUES ('snapshot-1', ?, ?, 1, 1, 1, 1, ?)
            """,
            (CREATED_AT, LATER_AT, _sha("inventory-1")),
        )
        conn.execute(
            """
            INSERT INTO coverage_snapshot_groups (
                snapshot_id, group_key, account_id, city, language, adset_id,
                min_active, effective_active_count, configured_active_count,
                status, inventory_sha256
            ) VALUES ('snapshot-1', 'CityA:L1', 'account-1', 'CityA', 'L1',
                      'adset-1', 1, 1, 1, 'OK', ?)
            """,
            (_sha("group-inventory-1"),),
        )
        conn.execute(
            """
            INSERT INTO coverage_incidents (
                incident_id, group_key, incident_kind, state,
                opened_snapshot_id, latest_snapshot_id, opened_at, updated_at
            ) VALUES ('incident-1', 'CityA:L1', 'THIN', 'OPEN',
                      'snapshot-1', 'snapshot-1', ?, ?)
            """,
            (CREATED_AT, CREATED_AT),
        )
        conn.execute(
            """
            INSERT INTO coverage_incident_events (
                event_id, incident_id, snapshot_id, event_type, payload_json,
                payload_sha256, created_at
            ) VALUES ('incident-event-1', 'incident-1', 'snapshot-1',
                      'OPENED', '{}', ?, ?)
            """,
            (_sha("incident-event-1"), CREATED_AT),
        )
        conn.execute(
            """
            INSERT INTO launch_watchdogs (
                watchdog_id, proposal_id, decision_id, job_id, state,
                expected_count, verify_deadline_at, created_at, updated_at
            ) VALUES ('watchdog-1', ?, ?, ?, 'EXECUTED', 1, ?, ?, ?)
            """,
            (
                proposal_id,
                lineage["decision_id"],
                lineage["job_id"],
                LATER_AT,
                CREATED_AT,
                CREATED_AT,
            ),
        )
        conn.execute(
            """
            INSERT INTO launch_watchdog_targets (
                proposal_id, watchdog_id, claim_id, account_id, adset_id,
                expected_ad_name, expected_fingerprint, created_ad_id,
                expected_configured_status, expected_effective_status,
                created_at
            ) VALUES (?, 'watchdog-1', ?, 'account-1', 'adset-1', 'ad-1',
                      ?, 'created-1', 'ACTIVE', 'ACTIVE', ?)
            """,
            (proposal_id, claim_id, _sha("fingerprint-1"), CREATED_AT),
        )
        conn.execute(
            """
            INSERT INTO launch_verification_observations (
                observation_id, watchdog_id, attempt_no, fetch_complete,
                verified_count, outcome, evidence_json, evidence_sha256,
                observed_at
            ) VALUES ('observation-1', 'watchdog-1', 1, 1, 1, 'VERIFIED',
                      '{}', ?, ?)
            """,
            (_sha("observation-1"), CREATED_AT),
        )
        conn.execute(
            """
            INSERT INTO owner_action_outcomes (
                outcome_id, proposal_id, job_id, claim_id, attempt_id, horizon,
                observed_at, metrics_json, metrics_sha256
            ) VALUES ('outcome-1', ?, ?, ?, ?, 'IMMEDIATE', ?, '{}', ?)
            """,
            (
                proposal_id,
                lineage["job_id"],
                claim_id,
                lineage["attempt_id"],
                CREATED_AT,
                _sha("outcome-1"),
            ),
        )

        immutable_rows = [
            ("owner_action_proposals", "proposal_id", proposal_id),
            ("owner_action_proposal_targets", "claim_id", claim_id),
            ("owner_action_evidence", "evidence_id", "evidence-1"),
            (
                "owner_action_decisions",
                "decision_id",
                lineage["decision_id"],
            ),
            ("owner_action_events", "event_id", "event-1"),
            ("coverage_snapshots", "snapshot_id", "snapshot-1"),
            (
                "coverage_snapshot_groups",
                "group_key",
                "CityA:L1",
            ),
            (
                "coverage_incident_events",
                "event_id",
                "incident-event-1",
            ),
            ("launch_watchdog_targets", "claim_id", claim_id),
            (
                "launch_verification_observations",
                "observation_id",
                "observation-1",
            ),
            ("owner_action_outcomes", "outcome_id", "outcome-1"),
        ]
        for table, key_column, key_value in immutable_rows:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    f"""
                    UPDATE {table}
                    SET {key_column} = {key_column}
                    WHERE {key_column} = ?
                    """,
                    (key_value,),
                )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    f"DELETE FROM {table} WHERE {key_column} = ?",
                    (key_value,),
                )
    finally:
        conn.close()


@pytest.mark.parametrize("initializer", ["decisions", "creative_kb"])
def test_startup_initializers_apply_and_verify_runtime_schema(
    tmp_path: Path,
    initializer: str,
) -> None:
    db_path = tmp_path / f"{initializer}.db"
    if initializer == "decisions":
        decisions_database.init_db(
            str(db_path),
            str(tmp_path / "missing-decisions.json"),
        )
    else:
        creative_intelligence.init_kb(str(db_path))

    health = migrations.verify_runtime_schema(str(db_path))
    assert health.healthy is True
    with _connect(db_path) as conn:
        # Инициализаторы применяют ВСЕ зарегистрированные миграции (с 028 их 10).
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0] == len(migrations.MIGRATION_FILES) == 10


def test_migration_023_applies_from_scratch_with_all_wave_e_objects(
    tmp_path: Path,
) -> None:
    """Миграция 023 накатывается с нуля вместе с 019-022 и даёт объекты волны E."""

    db_path = tmp_path / "wave-e.db"

    report = migrations.apply_runtime_migrations(str(db_path))
    health = migrations.verify_runtime_schema(str(db_path))

    assert 23 in report.applied_versions
    assert health.healthy is True
    with _connect(db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        triggers = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        record = conn.execute(
            """
            SELECT name, content_sha256 FROM schema_migrations WHERE version = 23
            """
        ).fetchone()
    assert {
        "action_verifications",
        "action_verification_state",
        "owner_digest_runs",
        "owner_digest_items",
        "owner_digest_batch_tokens",
        "owner_feedback",
        "owner_trail_messages",
    } <= tables
    assert {
        "trg_action_verifications_no_update",
        "trg_action_verifications_no_delete",
        "trg_owner_feedback_no_update",
        "trg_owner_feedback_no_delete",
        "trg_action_verification_state_forward",
        "trg_owner_digest_token_single_consume",
    } <= triggers
    path = migrations.MIGRATION_FILES[23]
    assert record is not None
    assert str(record[0]) == path.name
    assert str(record[1]) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_migration_024_creates_weekly_cohorts_table_and_indexes(
    tmp_path: Path,
) -> None:
    """Миграция 024 даёт таблицу недельных когорт с уникальной парой и индексами."""

    db_path = tmp_path / "cohorts.db"

    report = migrations.apply_runtime_migrations(str(db_path))
    health = migrations.verify_runtime_schema(str(db_path))

    assert 24 in report.applied_versions
    assert health.healthy is True
    with _connect(db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        record = conn.execute(
            "SELECT name, content_sha256 FROM schema_migrations WHERE version = 24"
        ).fetchone()
    assert "ad_weekly_cohorts" in tables
    assert {
        "uq_ad_weekly_cohort",
        "idx_ad_weekly_cohorts_week",
        "idx_ad_weekly_cohorts_adset",
    } <= indexes
    path = migrations.MIGRATION_FILES[24]
    assert record is not None
    assert str(record[0]) == path.name
    assert str(record[1]) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_weekly_cohort_comparable_row_requires_full_metrics(tmp_path: Path) -> None:
    """comparable=1 без полных чисел и полной недели схема не принимает."""

    db_path = tmp_path / "cohorts-check.db"
    migrations.apply_runtime_migrations(str(db_path))
    insert = """
        INSERT INTO ad_weekly_cohorts (
            ad_id, week_start, spend_usd, impressions, fb_leads, amo_leads,
            quals, days_covered, days_expected, comparable,
            not_comparable_reason, builder_version, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
    """
    with _connect(db_path) as conn:
        # Полная строка проходит.
        conn.execute(
            insert,
            ("ad-1", "2026-06-01", 70.0, 7000, 14, 3, 1, 7, 7, 1, None, CREATED_AT),
        )
        # Сравнимая без расхода — нет.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                insert,
                ("ad-2", "2026-06-01", None, 7000, 14, 3, 1, 7, 7, 1, None,
                 CREATED_AT),
            )
        # Сравнимая с неполной неделей — нет.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                insert,
                ("ad-3", "2026-06-01", 50.0, 5000, 10, 3, 1, 5, 7, 1, None,
                 CREATED_AT),
            )
        # Несравнимая без причины — нет.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                insert,
                ("ad-4", "2026-06-01", None, None, None, 3, 1, 5, 7, 0, None,
                 CREATED_AT),
            )


def test_migration_025_adds_cohort_revenue_columns(tmp_path: Path) -> None:
    """Миграция 025 дописывает в когорты выручку, курс и ROMI."""

    db_path = tmp_path / "cohort-revenue.db"

    report = migrations.apply_runtime_migrations(str(db_path))
    health = migrations.verify_runtime_schema(str(db_path))

    assert 25 in report.applied_versions
    assert health.healthy is True
    with _connect(db_path) as conn:
        columns = {
            str(row[1]): (str(row[2]).upper(), int(row[3]), row[4])
            for row in conn.execute('PRAGMA table_info("ad_weekly_cohorts")')
        }
        record = conn.execute(
            "SELECT name, content_sha256 FROM schema_migrations WHERE version = 25"
        ).fetchone()
    # Деньги — REAL и nullable: NULL это «неизвестно», а не ноль.
    assert columns["revenue_lcy"] == ("REAL", 0, None)
    assert columns["usd_lcy_rate"] == ("REAL", 0, None)
    assert columns["romi_pct"] == ("REAL", 0, None)
    assert columns["payments"] == ("INTEGER", 0, None)
    assert columns["revenue_horizon_days"] == ("INTEGER", 0, None)
    # Зрелость — календарный факт, поэтому NOT NULL с честным дефолтом «нет».
    assert columns["revenue_mature"] == ("INTEGER", 1, "0")
    path = migrations.MIGRATION_FILES[25]
    assert record is not None
    assert str(record[0]) == path.name
    assert str(record[1]) == hashlib.sha256(path.read_bytes()).hexdigest()


def _insert_coverage_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> None:
    conn.execute(
        """
        INSERT INTO coverage_snapshots (
            snapshot_id, started_at, completed_at, fetch_complete,
            configured_group_count, observed_group_count, page_count,
            inventory_sha256, error_code
        ) VALUES (?, ?, ?, 1, 1, 1, 1, ?, NULL)
        """,
        (snapshot_id, CREATED_AT, CREATED_AT, _sha(f"snapshot-{snapshot_id}")),
    )


def _insert_coverage_group(
    conn: sqlite3.Connection, snapshot_id: str, language: str
) -> None:
    conn.execute(
        """
        INSERT INTO coverage_snapshot_groups (
            snapshot_id, group_key, account_id, city, language, adset_id,
            min_active, effective_active_count, configured_active_count,
            status, inventory_sha256
        ) VALUES (?, ?, '123', 'CityA', ?, ?, 2, 3, 3, 'OK', ?)
        """,
        (
            snapshot_id,
            f"123|CityA|{language}",
            language,
            f"adset-{language}",
            _sha(f"group-{snapshot_id}-{language}"),
        ),
    )


def test_migration_028_accepts_prodb_coverage_group_and_keeps_history(
    tmp_path: Path,
) -> None:
    """Страж покрытия пишет группы PRODB; снимки, сделанные до 028, переживают пересборку."""

    db_path = tmp_path / "coverage-prodb.db"
    migrations.apply_runtime_migrations(
        str(db_path), required=(19, 20, 21, 22, 23, 24, 25, 26, 27)
    )
    with _connect(db_path) as conn:
        _insert_coverage_snapshot(conn, "snap-before")
        _insert_coverage_group(conn, "snap-before", "L2")
        # До 028 схема PRODB не принимает — ровно тот отказ, который чиним.
        with pytest.raises(sqlite3.IntegrityError, match="language"):
            _insert_coverage_group(conn, "snap-before", "PRODB")

    report = migrations.apply_runtime_migrations(str(db_path))
    health = migrations.verify_runtime_schema(str(db_path))

    assert report.applied_versions == (28,)
    assert health.healthy is True
    with _connect(db_path) as conn:
        surviving = conn.execute(
            "SELECT group_key, language, status FROM coverage_snapshot_groups"
        ).fetchall()
        assert [tuple(row) for row in surviving] == [("123|CityA|L2", "L2", "OK")]

        _insert_coverage_snapshot(conn, "snap-after")
        _insert_coverage_group(conn, "snap-after", "PRODB")
        _insert_coverage_group(conn, "snap-after", "L1")
        languages = {
            row[0]
            for row in conn.execute(
                "SELECT language FROM coverage_snapshot_groups WHERE snapshot_id = 'snap-after'"
            )
        }
        assert languages == {"PRODB", "L1"}
        # Прочий мусор в language по-прежнему отвергается.
        with pytest.raises(sqlite3.IntegrityError, match="language"):
            _insert_coverage_group(conn, "snap-after", "XX")
        # Иммутабельность истории и индекс вернулись вместе с таблицей.
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE coverage_snapshot_groups SET status = 'ZERO' WHERE language = 'PRODB'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute("DELETE FROM coverage_snapshot_groups WHERE language = 'PRODB'")
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_coverage_group_status'"
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE '_mig028%'"
        ).fetchone() is None
    path = migrations.MIGRATION_FILES[28]
    with _connect(db_path) as conn:
        record = conn.execute(
            "SELECT name, content_sha256 FROM schema_migrations WHERE version = 28"
        ).fetchone()
    assert record is not None
    assert str(record[0]) == path.name
    assert str(record[1]) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_cohort_romi_requires_mature_revenue(tmp_path: Path) -> None:
    """ROMI по недозревшей когорте схема не принимает."""

    db_path = tmp_path / "cohort-romi-check.db"
    migrations.apply_runtime_migrations(str(db_path))
    insert = """
        INSERT INTO ad_weekly_cohorts (
            ad_id, week_start, spend_usd, impressions, fb_leads, amo_leads,
            quals, days_covered, days_expected, comparable,
            not_comparable_reason, builder_version, computed_at,
            revenue_lcy, payments, revenue_horizon_days, revenue_mature,
            usd_lcy_rate, romi_pct
        ) VALUES (?, ?, 70.0, 7000, 14, 3, 1, 7, 7, 1, NULL, 1, ?,
                  ?, ?, ?, ?, ?, ?)
    """
    with _connect(db_path) as conn:
        # Дозревшая когорта с деньгами и ROMI проходит.
        conn.execute(
            insert,
            ("ad-1", "2026-06-01", CREATED_AT, 200000.0, 2, 14, 1, 500.0, 571.4),
        )
        # Тот же ROMI при revenue_mature=0 — нет.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                insert,
                ("ad-2", "2026-06-01", CREATED_AT, None, None, None, 0, None,
                 571.4),
            )
        # Отрицательная выручка допустима: возврат перевесил приход.
        conn.execute(
            insert,
            ("ad-3", "2026-06-01", CREATED_AT, -20000.0, 0, 14, 1, 500.0, -57.1),
        )
        # Курс не может быть нулевым — это деление на ноль, а не курс.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                insert,
                ("ad-4", "2026-06-01", CREATED_AT, 1000.0, 1, 14, 1, 0.0, 10.0),
            )
        # Отрицательное число оплат бессмысленно.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                insert,
                ("ad-5", "2026-06-01", CREATED_AT, 1000.0, -1, 14, 1, 500.0, 10.0),
            )
        conn.commit()


def test_verification_state_never_leaves_terminal_verdict(tmp_path: Path) -> None:
    """Терминальный вердикт верификатора нельзя «разморозить» обратно."""

    db_path = tmp_path / "verification-state.db"
    migrations.apply_runtime_migrations(str(db_path))
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO owner_action_proposals (
                proposal_id, proposal_kind, origin, idempotency_key, source_ref,
                requested_by_actor, summary, plan_json, plan_sha256,
                targets_sha256, evidence_sha256, config_version_sha256,
                proposal_sha256, staged_media_root, created_at, valid_until
            ) VALUES ('p-1', 'PAUSE', 'CRON', 'idem-1', 'scope-1', 'cron',
                      'сводка', '{}', ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                _sha("plan"),
                _sha("targets"),
                _sha("evidence"),
                _sha("config"),
                _sha("proposal"),
                CREATED_AT,
                LATER_AT,
            ),
        )
        conn.execute(
            """
            INSERT INTO action_verification_state (
                proposal_id, claim_id, kind, state, check_seq, retry_count,
                last_verdict, first_seen_at, updated_at
            ) VALUES ('p-1', 'claim-1', 'PAUSE', 'VERIFIED', 1, 0, 'VERIFIED',
                      ?, ?)
            """,
            (CREATED_AT, CREATED_AT),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="moves forward"):
            conn.execute(
                """
                UPDATE action_verification_state SET state = 'PENDING'
                WHERE proposal_id = 'p-1'
                """
            )
