"""Durable scheduler/verification worker без legacy last_run_date."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from services.database_migrations import apply_runtime_migrations
from services.launch_repository import OwnerLaunchRepository
from services.launch_verify import VerificationRun, verify_launch_watchdogs


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)


def test_scheduler_slot_is_unique_and_unfinished_run_resumes(tmp_path) -> None:
    db_path = tmp_path / "scheduler.db"
    apply_runtime_migrations(str(db_path))
    repository = OwnerLaunchRepository(str(db_path))

    first = repository.claim_scheduler_slot(
        scheduler_name="auto-launch",
        slot_key="2026-07-27",
        worker_id="cron-a",
        now=NOW,
    )
    busy = repository.claim_scheduler_slot(
        scheduler_name="auto-launch",
        slot_key="2026-07-27",
        worker_id="cron-b",
        now=NOW + timedelta(seconds=30),
    )
    resumed = repository.claim_scheduler_slot(
        scheduler_name="auto-launch",
        slot_key="2026-07-27",
        worker_id="cron-b",
        now=NOW + timedelta(seconds=60),
    )

    assert first.acquired is True
    assert busy.acquired is False
    assert resumed.acquired is True
    assert resumed.run_id == first.run_id
    assert resumed.state == "CLAIMED"


def test_no_eligible_slot_is_terminal_without_last_run_file(tmp_path) -> None:
    db_path = tmp_path / "scheduler.db"
    apply_runtime_migrations(str(db_path))
    repository = OwnerLaunchRepository(str(db_path))
    lease = repository.claim_scheduler_slot(
        scheduler_name="auto-launch",
        slot_key="2026-07-27",
        worker_id="cron-a",
        now=NOW,
    )

    completed = repository.mark_scheduler_no_eligible(
        lease,
        now=NOW + timedelta(seconds=1),
    )
    replay = repository.claim_scheduler_slot(
        scheduler_name="auto-launch",
        slot_key="2026-07-27",
        worker_id="cron-b",
        now=NOW + timedelta(hours=1),
    )

    assert completed.state == "NO_ELIGIBLE"
    assert replay.acquired is False
    assert replay.run_id == lease.run_id


class _FakeVerificationRepository:
    def __init__(self) -> None:
        self.lease = SimpleNamespace(
            watchdog_id="watchdog-1",
            targets=(),
        )
        self.recorded: list[tuple[dict, bool]] = []

    def claim_due(self, **_kwargs):
        return (self.lease,)

    def record_observation(self, _lease, *, observations, fetch_complete, now):
        del now
        self.recorded.append((dict(observations), fetch_complete))
        return SimpleNamespace(state="VERIFYING")


def test_verify_worker_turns_fetch_error_into_durable_unknown() -> None:
    repository = _FakeVerificationRepository()
    with (
        patch(
            "services.launch_verify._owner_launch_repository",
            return_value=repository,
        ),
        patch(
            "services.launch_verify._fetch_watchdog_live_ads",
            side_effect=RuntimeError("facebook unavailable"),
        ),
    ):
        result = verify_launch_watchdogs(
            worker_id="verify-a",
            now=NOW,
            limit=10,
        )

    assert result == VerificationRun(
        checked=1,
        verified=0,
        pending=1,
        failed=0,
        reconcile_required=0,
        errors=("watchdog-1:RuntimeError",),
    )
    assert repository.recorded == [({}, False)]
