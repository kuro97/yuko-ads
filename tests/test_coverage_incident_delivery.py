"""Durable incident, reminder и подтверждаемая Telegram-доставка coverage."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.coverage_monitor import deliver_coverage_alerts
from services.coverage_repository import (
    CoverageAdsetSnapshot,
    CoverageGroupSnapshot,
    CoverageRepository,
    CoverageSnapshot,
    canonical_sha256,
)
from services.database_migrations import apply_runtime_migrations


NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
GROUP_KEY = "account-1|CityA|L2"
# Ключ до перехода на групповой скоуп: в хвосте ещё жил adset_id.
LEGACY_GROUP_KEY = f"{GROUP_KEY}|adset-1"


@pytest.fixture
def coverage_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "coverage.sqlite3"
    apply_runtime_migrations(str(db_path))
    return db_path


def _group(
    status: str,
    *,
    group_key: str = GROUP_KEY,
    city: str = "CityA",
    language: str = "L2",
    adset_ids: tuple[str, ...] = ("adset-1",),
) -> CoverageGroupSnapshot:
    count = {"ZERO": 0, "THIN": 1, "OK": 2, "UNKNOWN": None}[status]
    return CoverageGroupSnapshot(
        group_key=group_key,
        account_id="account-1",
        city=city,
        language=language,  # type: ignore[arg-type]
        adsets=tuple(
            CoverageAdsetSnapshot(
                adset_id=adset_id,
                status="ACTIVE" if index == 0 else "PAUSED",
                ads_total=None if count is None else count,
                active_count=None if count is None else (count if index == 0 else 0),
            )
            for index, adset_id in enumerate(adset_ids)
        ),
        min_active=2,
        effective_active_count=count,
        configured_active_count=count,
        status=status,  # type: ignore[arg-type]
        inventory_sha256=canonical_sha256(
            {"group_key": group_key, "status": status, "count": count}
        ),
    )


def _snapshot(
    snapshot_id: str,
    status: str,
    at: datetime,
    *,
    fetch_complete: bool = True,
    extra_unknown: bool = False,
    group_key: str = GROUP_KEY,
) -> CoverageSnapshot:
    groups = [_group(status, group_key=group_key)]
    observed_count = int(status != "UNKNOWN")
    if extra_unknown:
        groups.append(
            _group(
                "UNKNOWN",
                group_key="account-1|CityB|L1",
                city="CityB",
                language="L1",
                adset_ids=("adset-2",),
            )
        )
    return CoverageSnapshot(
        snapshot_id=snapshot_id,
        started_at=at,
        completed_at=at,
        fetch_complete=fetch_complete,
        configured_group_count=len(groups),
        observed_group_count=observed_count,
        page_count=len(groups),
        inventory_sha256=canonical_sha256(
            [
                (group.group_key, group.status, group.inventory_sha256)
                for group in groups
            ]
        ),
        error_code=None if fetch_complete else "INCOMPLETE_INVENTORY",
        groups=tuple(groups),
    )


def _rows(db_path: Path, sql: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def test_zero_incident_is_deduplicated_and_reminds_after_failed_delivery(
    coverage_db: Path,
) -> None:
    repository = CoverageRepository(coverage_db)
    opened = repository.record_snapshot_and_process_incidents(
        _snapshot("zero-open", "ZERO", NOW),
        now=NOW,
    )

    assert opened.opened_count == 1
    assert opened.queued_delivery_count == 1
    before_due = repository.record_snapshot_and_process_incidents(
        _snapshot("zero-before-due", "ZERO", NOW + timedelta(minutes=14)),
        now=NOW + timedelta(minutes=14),
    )
    assert before_due.queued_delivery_count == 0

    retry_times = (
        NOW,
        NOW + timedelta(minutes=1),
        NOW + timedelta(minutes=3),
        NOW + timedelta(minutes=8),
        NOW + timedelta(minutes=23),
    )
    outcomes: list[str] = []
    for attempt_time in retry_times:
        claimed = repository.claim_due_deliveries(
            worker_id="coverage-worker",
            now=attempt_time,
        )
        assert len(claimed) == 1
        outcomes.append(
            repository.record_delivery_failure(
                delivery=claimed[0],
                error_code="TELEGRAM_UNAVAILABLE",
                now=attempt_time,
            )
        )
    assert outcomes == ["RETRY", "RETRY", "RETRY", "RETRY", "FAILED_VISIBLE"]

    reminded = repository.record_snapshot_and_process_incidents(
        _snapshot("zero-reminder", "ZERO", NOW + timedelta(minutes=24)),
        now=NOW + timedelta(minutes=24),
    )
    assert reminded.opened_count == 0
    assert reminded.reminder_count == 1
    assert reminded.queued_delivery_count == 1
    outbox = _rows(
        coverage_db,
        """
        SELECT state, attempts, telegram_message_id
        FROM telegram_delivery_outbox
        ORDER BY created_at, delivery_id
        """,
    )
    assert [(row["state"], row["attempts"]) for row in outbox] == [
        ("FAILED_VISIBLE", 5),
        ("PENDING", 0),
    ]
    assert all(row["telegram_message_id"] is None for row in outbox)


class ConfirmingTelegram:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id
        self.calls: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str) -> int:
        self.calls.append((chat_id, text))
        return self.message_id


def test_zero_stops_reminders_only_after_sent_message_id(
    coverage_db: Path,
) -> None:
    repository = CoverageRepository(coverage_db)
    repository.record_snapshot_and_process_incidents(
        _snapshot("zero-confirm", "ZERO", NOW),
        now=NOW,
    )
    telegram = ConfirmingTelegram(message_id=777)

    result = deliver_coverage_alerts(
        repository=repository,
        client=telegram,
        telegram_chat_id=-100123,
        worker_id="coverage-worker",
        now=NOW,
    )

    assert (result.claimed_count, result.sent_count) == (1, 1)
    assert len(telegram.calls) == 1
    repository.record_snapshot_and_process_incidents(
        _snapshot("zero-after-confirm", "ZERO", NOW + timedelta(hours=1)),
        now=NOW + timedelta(hours=1),
    )
    delivery = _rows(
        coverage_db,
        """
        SELECT state, telegram_chat_id, telegram_message_id, sent_at
        FROM telegram_delivery_outbox
        """,
    )
    assert len(delivery) == 1
    assert dict(delivery[0]) == {
        "state": "SENT",
        "telegram_chat_id": -100123,
        "telegram_message_id": 777,
        "sent_at": NOW.isoformat(),
    }
    incident = _rows(
        coverage_db,
        "SELECT next_reminder_at FROM coverage_incidents",
    )
    assert incident[0]["next_reminder_at"] is None


def test_missing_message_id_is_not_confirmed(coverage_db: Path) -> None:
    repository = CoverageRepository(coverage_db)
    repository.record_snapshot_and_process_incidents(
        _snapshot("zero-no-message-id", "ZERO", NOW),
        now=NOW,
    )

    result = deliver_coverage_alerts(
        repository=repository,
        client=ConfirmingTelegram(message_id=0),
        telegram_chat_id=123,
        worker_id="coverage-worker",
        now=NOW,
    )

    assert (result.sent_count, result.retry_count) == (0, 1)
    row = _rows(
        coverage_db,
        """
        SELECT state, attempts, telegram_message_id, next_attempt_at
        FROM telegram_delivery_outbox
        """,
    )[0]
    assert row["state"] == "PENDING"
    assert row["attempts"] == 1
    assert row["telegram_message_id"] is None
    assert row["next_attempt_at"] == (NOW + timedelta(minutes=1)).isoformat()


def test_thin_sends_daily_reminder_after_confirmation(coverage_db: Path) -> None:
    repository = CoverageRepository(coverage_db)
    repository.record_snapshot_and_process_incidents(
        _snapshot("thin-open", "THIN", NOW),
        now=NOW,
    )
    deliver_coverage_alerts(
        repository=repository,
        client=ConfirmingTelegram(message_id=10),
        telegram_chat_id=123,
        worker_id="coverage-worker",
        now=NOW,
    )

    before_day = repository.record_snapshot_and_process_incidents(
        _snapshot("thin-before-day", "THIN", NOW + timedelta(hours=23)),
        now=NOW + timedelta(hours=23),
    )
    at_day = repository.record_snapshot_and_process_incidents(
        _snapshot("thin-at-day", "THIN", NOW + timedelta(days=1)),
        now=NOW + timedelta(days=1),
    )

    assert before_day.queued_delivery_count == 0
    assert at_day.reminder_count == 1
    assert [
        row["state"]
        for row in _rows(
            coverage_db,
            "SELECT state FROM telegram_delivery_outbox ORDER BY created_at",
        )
    ] == ["SENT", "PENDING"]


def test_two_consecutive_complete_ok_snapshots_resolve_incident(
    coverage_db: Path,
) -> None:
    repository = CoverageRepository(coverage_db)
    repository.record_snapshot_and_process_incidents(
        _snapshot("thin-for-resolve", "THIN", NOW),
        now=NOW,
    )
    incomplete_ok = repository.record_snapshot_and_process_incidents(
        _snapshot(
            "incomplete-ok",
            "OK",
            NOW + timedelta(minutes=2),
            fetch_complete=False,
            extra_unknown=True,
        ),
        now=NOW + timedelta(minutes=2),
    )
    first_ok_snapshot = _snapshot(
        "first-complete-ok",
        "OK",
        NOW + timedelta(minutes=4),
    )
    first_ok = repository.record_snapshot_and_process_incidents(
        first_ok_snapshot,
        now=NOW + timedelta(minutes=4),
    )
    duplicate = repository.record_snapshot_and_process_incidents(
        first_ok_snapshot,
        now=NOW + timedelta(minutes=5),
    )
    second_ok = repository.record_snapshot_and_process_incidents(
        _snapshot(
            "second-complete-ok",
            "OK",
            NOW + timedelta(minutes=6),
        ),
        now=NOW + timedelta(minutes=6),
    )

    assert incomplete_ok.resolved_count == 0
    assert first_ok.resolved_count == 0
    assert duplicate.deduplicated is True
    assert second_ok.resolved_count == 1
    incident = _rows(
        coverage_db,
        """
        SELECT state, consecutive_complete_ok, resolved_at
        FROM coverage_incidents
        WHERE incident_kind = 'THIN'
        """,
    )[0]
    assert incident["state"] == "RESOLVED"
    assert incident["consecutive_complete_ok"] == 2
    assert incident["resolved_at"] == (NOW + timedelta(minutes=6)).isoformat()


def test_zero_resolution_is_announced_once_per_group(coverage_db: Path) -> None:
    """Критический инцидент закрывается вслух — одним «✅ восстановлено»."""
    repository = CoverageRepository(coverage_db)
    repository.record_snapshot_and_process_incidents(
        _snapshot("zero-to-resolve", "ZERO", NOW),
        now=NOW,
    )
    repository.record_snapshot_and_process_incidents(
        _snapshot("ok-1", "OK", NOW + timedelta(minutes=30)),
        now=NOW + timedelta(minutes=30),
    )
    resolved = repository.record_snapshot_and_process_incidents(
        _snapshot("ok-2", "OK", NOW + timedelta(minutes=60)),
        now=NOW + timedelta(minutes=60),
    )

    assert resolved.resolved_count == 1
    assert resolved.queued_delivery_count == 1
    texts = [
        row["rendered_text"]
        for row in _rows(
            coverage_db,
            """
            SELECT rendered_text
            FROM telegram_delivery_outbox
            ORDER BY created_at, delivery_id
            """,
        )
    ]
    assert len(texts) == 2
    assert texts[0].startswith("❗")
    assert texts[1].startswith("✅ Покрытие восстановлено")
    assert "CityA/L2" in texts[1]


def test_thin_resolution_stays_silent(coverage_db: Path) -> None:
    """Тонкое покрытие закрывается тихо — лишний шум владельцу не нужен."""
    repository = CoverageRepository(coverage_db)
    repository.record_snapshot_and_process_incidents(
        _snapshot("thin-to-resolve", "THIN", NOW),
        now=NOW,
    )
    repository.record_snapshot_and_process_incidents(
        _snapshot("thin-ok-1", "OK", NOW + timedelta(minutes=30)),
        now=NOW + timedelta(minutes=30),
    )
    resolved = repository.record_snapshot_and_process_incidents(
        _snapshot("thin-ok-2", "OK", NOW + timedelta(minutes=60)),
        now=NOW + timedelta(minutes=60),
    )

    assert resolved.resolved_count == 1
    assert resolved.queued_delivery_count == 0


def test_legacy_adset_scoped_incident_is_matched_by_group_key(
    coverage_db: Path,
) -> None:
    """Инцидент со старым ключом (…|adset) не дублируется групповым снимком."""
    repository = CoverageRepository(coverage_db)
    repository.record_snapshot_and_process_incidents(
        _snapshot("legacy-zero", "ZERO", NOW, group_key=LEGACY_GROUP_KEY),
        now=NOW,
    )

    same_outage = repository.record_snapshot_and_process_incidents(
        _snapshot("group-zero", "ZERO", NOW + timedelta(minutes=30)),
        now=NOW + timedelta(minutes=30),
    )

    assert same_outage.opened_count == 0
    incidents = _rows(
        coverage_db,
        "SELECT group_key, state FROM coverage_incidents",
    )
    assert [(row["group_key"], row["state"]) for row in incidents] == [
        (LEGACY_GROUP_KEY, "OPEN")
    ]


def test_incident_survives_group_cabinet_migration(coverage_db: Path) -> None:
    """Миграция группы в другой кабинет не плодит второй инцидент по той же паре.

    L2 расщеплённых городов уехал из cabinet_a в «ACME cabinet_b»: голова
    group_key сменилась. Без матча по хвосту |city|language старый OPEN висел бы
    вечно (его группа в снимках больше не появляется), а рядом открылся бы дубль.
    """
    migrated_key = "account-2|CityA|L2"
    repository = CoverageRepository(coverage_db)
    repository.record_snapshot_and_process_incidents(
        _snapshot("pre-migration-zero", "ZERO", NOW),
        now=NOW,
    )

    same_outage = repository.record_snapshot_and_process_incidents(
        _snapshot(
            "post-migration-zero",
            "ZERO",
            NOW + timedelta(minutes=30),
            group_key=migrated_key,
        ),
        now=NOW + timedelta(minutes=30),
    )

    assert same_outage.opened_count == 0
    incidents = _rows(
        coverage_db,
        "SELECT group_key, state FROM coverage_incidents",
    )
    assert [(row["group_key"], row["state"]) for row in incidents] == [
        (GROUP_KEY, "OPEN")
    ]


def test_incident_opened_before_migration_is_closed_after_it(
    coverage_db: Path,
) -> None:
    """Инцидент, открытый до переезда группы, закрывается снимками нового кабинета."""
    migrated_key = "account-2|CityA|L2"
    repository = CoverageRepository(coverage_db)
    repository.record_snapshot_and_process_incidents(
        _snapshot("pre-migration-zero-close", "ZERO", NOW),
        now=NOW,
    )

    first_ok = repository.record_snapshot_and_process_incidents(
        _snapshot(
            "migrated-ok-1",
            "OK",
            NOW + timedelta(minutes=30),
            group_key=migrated_key,
        ),
        now=NOW + timedelta(minutes=30),
    )
    second_ok = repository.record_snapshot_and_process_incidents(
        _snapshot(
            "migrated-ok-2",
            "OK",
            NOW + timedelta(minutes=60),
            group_key=migrated_key,
        ),
        now=NOW + timedelta(minutes=60),
    )

    assert first_ok.resolved_count == 0
    assert second_ok.resolved_count == 1
    incidents = _rows(
        coverage_db,
        "SELECT group_key, state FROM coverage_incidents",
    )
    assert [(row["group_key"], row["state"]) for row in incidents] == [
        (GROUP_KEY, "RESOLVED")
    ]


def test_legacy_adset_scoped_incident_is_closed_by_group_snapshot(
    coverage_db: Path,
) -> None:
    """Адсеты пересозданы — ложный ноль закрывается сам."""
    repository = CoverageRepository(coverage_db)
    repository.record_snapshot_and_process_incidents(
        _snapshot("legacy-zero-close", "ZERO", NOW, group_key=LEGACY_GROUP_KEY),
        now=NOW,
    )

    first_ok = repository.record_snapshot_and_process_incidents(
        _snapshot(
            "group-ok-1",
            "OK",
            NOW + timedelta(minutes=30),
            group_key=GROUP_KEY,
        ),
        now=NOW + timedelta(minutes=30),
    )
    second_ok = repository.record_snapshot_and_process_incidents(
        _snapshot(
            "group-ok-2",
            "OK",
            NOW + timedelta(minutes=60),
            group_key=GROUP_KEY,
        ),
        now=NOW + timedelta(minutes=60),
    )

    assert first_ok.opened_count == 0
    assert first_ok.resolved_count == 0
    assert second_ok.resolved_count == 1
    incident = _rows(
        coverage_db,
        "SELECT group_key, state FROM coverage_incidents",
    )
    assert [(row["group_key"], row["state"]) for row in incident] == [
        (LEGACY_GROUP_KEY, "RESOLVED")
    ]
    resolution = _rows(
        coverage_db,
        """
        SELECT rendered_text
        FROM telegram_delivery_outbox
        ORDER BY created_at, delivery_id
        """,
    )[-1]["rendered_text"]
    assert resolution.startswith("✅ Покрытие восстановлено")


def test_reused_snapshot_id_with_different_content_is_rejected(
    coverage_db: Path,
) -> None:
    repository = CoverageRepository(coverage_db)
    repository.record_snapshot_and_process_incidents(
        _snapshot("immutable-id", "ZERO", NOW),
        now=NOW,
    )

    with pytest.raises(RuntimeError, match="immutable"):
        repository.record_snapshot_and_process_incidents(
            _snapshot("immutable-id", "THIN", NOW),
            now=NOW,
        )
