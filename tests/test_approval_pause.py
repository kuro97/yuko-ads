"""Locked pause guard для sealed Approval Checker gateway."""

from __future__ import annotations

import inspect
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest

from services import action_locks
from services import adset_pause_guard as guard
from services.approval_checker_models import (
    AdStatusSnapshot,
    ActionKind,
    ActionOrigin,
    ActionResult,
    FactCategory,
    FactClaim,
    Metric,
    PauseManifest,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
    canonical_json,
)


NOW = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)


class _Response:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return self.payload


def _fact(source: SourceSystem, metric: Metric, value: int | Decimal | str) -> FactClaim:
    return FactClaim(
        claim_id=f"{source.value}:{metric.value}",
        field_id=None,
        category=FactCategory.BUSINESS_METRIC,
        subject=SubjectRef(SubjectKind.AD, "1", "100"),
        metric=metric,
        value=value,
        source=source,
        window=TimeWindow(
            start=NOW - timedelta(days=7),
            end=NOW,
            timezone_name="Etc/GMT-5",
            semantic="decision_window",
        ),
        currency="USD" if metric is Metric.SPEND else None,
    )


def _manifest(
    *,
    sibling_active_ids: tuple[str, ...],
    replacement_ad_id: str | None = None,
) -> PauseManifest:
    sibling_snapshot = (
        AdStatusSnapshot(
            ad_id="2",
            adset_id="100",
            configured_status="ACTIVE",
            effective_status="ACTIVE" if "2" in sibling_active_ids else "PAUSED",
        ),
    )
    return PauseManifest(
        kind=ActionKind.PAUSE,
        manifest_id="pause-manifest-1",
        origin=ActionOrigin.AUTOPILOT_LIVE,
        idempotency_key=str(uuid.uuid4()),
        prepared_at=NOW,
        ad_id="1",
        adset_id="100",
        reason_code="LOW_ROMI",
        expected_before_status="ACTIVE",
        expected_after_status="PAUSED",
        decision_window=TimeWindow(
            start=NOW - timedelta(days=7),
            end=NOW,
            timezone_name="Etc/GMT-5",
            semantic="decision_window",
        ),
        facts=(
            _fact(SourceSystem.FACEBOOK, Metric.SPEND, Decimal("10")),
            _fact(SourceSystem.AMO, Metric.QUALS, 0),
            _fact(SourceSystem.CDP_ERP, Metric.PAYMENTS, 0),
        ),
        pre_inventory_sha256="a" * 64,
        sibling_active_ids=sibling_active_ids,
        pre_unrelated_inventory_sha256=hashlib.sha256(
            canonical_json(sibling_snapshot)
        ).hexdigest(),
        sibling_status_snapshot=sibling_snapshot,
        replacement_ad_id=replacement_ad_id,
    )


def _context(
    ad_id: str,
    *,
    configured_status: str = "ACTIVE",
    effective_status: str = "ACTIVE",
) -> guard.AdLiveContext:
    return {
        "ad_id": ad_id,
        "adset_id": "100",
        "configured_status": configured_status,
        "effective_status": effective_status,
    }


def _inventory(
    *,
    active_ids: set[str],
    exact_ids: tuple[str, ...],
) -> guard.AdsetInventory:
    contexts = {
        ad_id: _context(ad_id, effective_status="ACTIVE" if ad_id in active_ids else "PAUSED")
        for ad_id in {"1", "2", *exact_ids}
    }
    inventory: guard.AdsetInventory = {
        "adset_id": "100",
        "active_ids": set(active_ids),
        "candidate_context": {ad_id: contexts[ad_id] for ad_id in exact_ids},
        "inventory_context": contexts,
        "state_sha256": "b" * 64,
        "complete": True,
        "pages_read": 1,
        "error": None,
    }
    return inventory


@pytest.fixture
def approval_lock_root(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(action_locks, "_lock_root", lambda: tmp_path / "locks")
    return tmp_path


def test_locked_validation_requires_external_action_lock(monkeypatch: pytest.MonkeyPatch):
    manifest = _manifest(sibling_active_ids=("2",))
    monkeypatch.setattr(
        guard,
        "fetch_pause_inventory",
        Mock(return_value={"100": _inventory(active_ids={"1", "2"}, exact_ids=("1",))}),
    )

    with pytest.raises(guard.PauseGuardError, match="adset_lock_required"):
        guard._validate_pause_locked(manifest)


def test_fetch_pause_inventory_reads_full_exact_inventory(monkeypatch: pytest.MonkeyPatch):
    responses = iter(
        (
            _Response(
                {
                    "1": {
                        "id": "1",
                        "adset_id": "100",
                        "status": "ACTIVE",
                        "effective_status": "ACTIVE",
                    }
                }
            ),
            _Response(
                {
                    "data": [
                        {
                            "id": "1",
                            "adset_id": "100",
                            "status": "ACTIVE",
                            "effective_status": "ACTIVE",
                        },
                        {
                            "id": "2",
                            "adset_id": "100",
                            "status": "PAUSED",
                            "effective_status": "PAUSED",
                        },
                    ],
                    "paging": {},
                }
            ),
        )
    )
    get = Mock(side_effect=lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr("agent.fb_common._throttled_get", get)
    monkeypatch.setattr("services.fb_token_provider.get_fb_token", lambda: "token")

    inventory = guard.fetch_pause_inventory(["1"])["100"]

    assert inventory["complete"] is True
    assert inventory["active_ids"] == {"1"}
    assert set(inventory["inventory_context"]) == {"1", "2"}
    assert len(inventory["state_sha256"]) == 64
    assert "effective_status" not in get.call_args.kwargs["params"]


def test_inventory_state_sha256_is_order_independent_and_matches_internal():
    rows = (_context("2"), _context("1"))
    forward = guard.inventory_state_sha256(rows, expected_adset_id="100")
    reverse = guard.inventory_state_sha256(
        tuple(reversed(rows)),
        expected_adset_id="100",
    )
    inventory = _inventory(active_ids={"1", "2"}, exact_ids=("1",))

    assert forward == reverse
    assert forward == guard._inventory_sha256(inventory)


@pytest.mark.parametrize(
    "rows,expected_adset_id",
    [
        (
            (
                {
                    "ad_id": "1",
                    "adset_id": "100",
                    "configured_status": "ACTIVE",
                },
            ),
            "100",
        ),
        ((_context("1"), _context("1")), "100"),
        ((_context("1"),), "999"),
    ],
    ids=("missing-field", "duplicate-ad", "wrong-adset"),
)
def test_inventory_state_sha256_rejects_invalid_rows(rows, expected_adset_id):
    with pytest.raises(ValueError):
        guard.inventory_state_sha256(
            rows,
            expected_adset_id=expected_adset_id,
        )


def test_locked_validation_uses_one_external_lock_and_allows_safe_pause(
    monkeypatch: pytest.MonkeyPatch,
    approval_lock_root,
):
    manifest = _manifest(sibling_active_ids=("2",))
    fetch = Mock(
        return_value={"100": _inventory(active_ids={"1", "2"}, exact_ids=("1",))}
    )
    monkeypatch.setattr(guard, "fetch_pause_inventory", fetch)

    with action_locks.adset_locks(("100",)):
        # Если helper попытается взять второй lock, тест немедленно упадёт.
        monkeypatch.setattr(
            action_locks,
            "adset_locks",
            Mock(side_effect=AssertionError("nested lock")),
        )
        result = guard._validate_pause_locked(manifest)

    assert result.allowed is True
    assert result.action_result is None
    assert result.check_reason is None
    assert result.active_other_ids == ("2",)
    fetch.assert_called_once_with(["1"])


def test_locked_validation_denies_last_effective_active_as_typed_result(
    monkeypatch: pytest.MonkeyPatch,
    approval_lock_root,
):
    manifest = _manifest(sibling_active_ids=())
    monkeypatch.setattr(
        guard,
        "fetch_pause_inventory",
        Mock(return_value={"100": _inventory(active_ids={"1"}, exact_ids=("1",))}),
    )

    with action_locks.adset_locks(("100",)):
        result = guard._validate_pause_locked(manifest)

    assert result.allowed is False
    assert result.action_result is ActionResult.FAILED
    assert result.check_reason is not None
    assert result.check_reason.code == "LAST_EFFECTIVE_ACTIVE"
    assert result.check_reason.blocking is True
    assert result.check_reason.source is SourceSystem.FACEBOOK


def test_locked_validation_accepts_only_exact_active_replacement(
    monkeypatch: pytest.MonkeyPatch,
    approval_lock_root,
):
    manifest = _manifest(sibling_active_ids=("2",), replacement_ad_id="2")
    monkeypatch.setattr(
        guard,
        "fetch_pause_inventory",
        Mock(
            return_value={
                "100": _inventory(active_ids={"1", "2"}, exact_ids=("1", "2"))
            }
        ),
    )

    with action_locks.adset_locks(("100",)):
        result = guard._validate_pause_locked(manifest)

    assert result.allowed is True
    assert result.active_other_ids == ("2",)


def test_locked_validation_denies_replacement_or_inventory_drift(
    monkeypatch: pytest.MonkeyPatch,
    approval_lock_root,
):
    manifest = _manifest(sibling_active_ids=("2",), replacement_ad_id="2")
    inventory = _inventory(active_ids={"1", "2"}, exact_ids=("1", "2"))
    inventory["candidate_context"]["2"] = _context(
        "2", configured_status="PAUSED", effective_status="PAUSED"
    )
    monkeypatch.setattr(
        guard,
        "fetch_pause_inventory",
        Mock(return_value={"100": inventory}),
    )

    with action_locks.adset_locks(("100",)):
        result = guard._validate_pause_locked(manifest)

    assert result.action_result is ActionResult.FAILED
    assert result.check_reason is not None
    assert result.check_reason.code == "INVENTORY_CHANGED"


def test_pause_guard_contains_no_direct_telegram_business_denial():
    source = inspect.getsource(guard)

    assert "send_critical_alert" not in source
    assert "send_telegram" not in source
