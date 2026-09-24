"""Offline tests daily planner и fail-closed workflow slot preflight."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import nullcontext
from datetime import date
from unittest.mock import patch

import pytest

from integrations.facebook import CleanupDeleteReconcileRequired
from services import adset_cleaner
from services import creative_intelligence as ci
from services import proactive_adset_cleaner as proactive
from services.cleanup_authorization import consume_delete_authorization
from services.cleanup_repository import finish_cleanup_delete
from services.replacement_workflow import enqueue_replacement, link_replacement_launch


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    ci.DB_PATH = None
    db_path = tmp_path / "cleaner.db"
    ci.init_kb(str(db_path))
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id TEXT NOT NULL,
                spend REAL,
                romi REAL,
                leads INTEGER
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    with patch.object(adset_cleaner, "_DECISIONS_DB_PATH", db_path):
        yield db_path
    ci.DB_PATH = None


def _config(**overrides) -> proactive.ProactiveCleanerConfig:
    values = {
        "proactive_enabled": True,
        "stale_days": 15,
        "target_free": 5,
        "critical_free_slots": 2,
        "max_manifest_candidates_per_adset": 10,
        "alert_dedup_hours": 6,
        "managed_account_kinds": ("offline",),
    }
    values.update(overrides)
    return proactive.ProactiveCleanerConfig(**values)


def _managed(
    adset_id: str = "adset-1",
    *,
    used: int = 39,
    ads: tuple[dict, ...] = (),
    source: str = "fb_api",
    effective_status: str = "ACTIVE",
    inventory_complete: bool = True,
    active_count: int | None = 1,
) -> proactive.ManagedAdset:
    return proactive.ManagedAdset(
        account_kind="offline",
        account_id="account-1",
        source=source,
        adset_id=adset_id,
        adset_name=f"Adset {adset_id}",
        effective_status=effective_status,
        inventory_complete=inventory_complete,
        used=used if inventory_complete else None,
        available=50 - used if inventory_complete else None,
        active_count=active_count,
        ads=ads,
    )


def _candidate(
    ad_id: str,
    *,
    ordinal: int,
    capacity_before: int,
    state: str = "ELIGIBLE",
    reason: str = "exact_zero_evidence",
) -> dict:
    return {
        "account_kind": "offline",
        "adset_id": "adset-1",
        "adset_name": "Adset adset-1",
        "ad_id": ad_id,
        "ad_name": f"Ad {ad_id}",
        "ordinal": ordinal,
        "state": state,
        "reason": reason,
        "configured_status": "PAUSED",
        "effective_status": "PAUSED",
        "age_days": 45,
        "lifetime_spend_usd": 0.0,
        "lifetime_impressions": 0,
        "lifetime_clicks": 0,
        "local_spend_usd": 0.0,
        "local_impressions": 0,
        "local_clicks": 0,
        "local_leads": 0,
        "local_payments": 0,
        "evidence": {
            "exact": True,
            "inventory_complete": True,
            "lifetime_row_count": 1,
            "local_evidence_complete": True,
            "kb_present": True,
        },
        "capacity_before": capacity_before,
    }


def _capacity(*, available: int, stale_ids: tuple[str, ...]) -> dict:
    ad_count = 50 - available
    ad_ids = [*stale_ids, "active-1"]
    while len(ad_ids) < ad_count:
        ad_ids.append(f"inventory-{len(ad_ids)}")
    return {
        "adset_id": "adset-1",
        "name": "Adset adset-1",
        "ad_count": ad_count,
        "max_ads": 50,
        "available": available,
        "stale_ads": [
            {
                "id": ad_id,
                "name": f"Ad {ad_id}",
                "adset_id": "adset-1",
                "status": "PAUSED",
                "effective_status": "PAUSED",
                "created_time": f"2026-05-{index + 1:02d}T00:00:00+0000",
            }
            for index, ad_id in enumerate(stale_ids)
        ],
        "ad_ids": ad_ids,
        "effective_active_ids": ["active-1"],
        "effective_active_count": 1,
        "inventory_complete": True,
    }


def test_proactive_api_has_no_active_mode_and_disabled_has_no_external_calls():
    with patch(
        "services.proactive_adset_cleaner.get_proactive_cleaner_config",
        return_value=_config(proactive_enabled=False),
    ), patch("services.proactive_adset_cleaner.acquire_cleanup_run") as acquire, patch(
        "services.proactive_adset_cleaner.discover_managed_adsets"
    ) as discover:
        result = proactive.run_proactive_cleaner(date(2026, 7, 21))

    assert result["ran"] is False
    assert result["effective_mode"] == "dry_run"
    acquire.assert_not_called()
    discover.assert_not_called()
    with pytest.raises(TypeError):
        proactive.run_proactive_cleaner(mode="active")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "cleaner_config",
    [
        {"proactive_enabled": "true"},
        {"proactive_enabled": 1},
        {"managed_account_kinds": []},
        {"managed_account_kinds": ["offline", "offline"]},
        {"managed_account_kinds": ["bot"]},
        {"stale_days": 14},
        {"target_free": 5, "adset_threshold": 44},
    ],
)
def test_config_is_strict_and_fail_closed(cleaner_config):
    with patch(
        "services.autopilot.get_autopilot_config",
        return_value={"cleaner": cleaner_config},
    ):
        with pytest.raises(ValueError):
            proactive.get_proactive_cleaner_config()


def test_discovery_requires_direct_fb_source_before_inventory():
    with patch(
        "services.fb_token_provider.get_fb_account_id", return_value="account-1"
    ), patch(
        "services.proactive_adset_cleaner.discover_adsets",
        return_value={"source": "fallback", "leadgen": {}, "mql": {}},
    ) as discover, patch(
        "services.proactive_adset_cleaner._load_managed_adset"
    ) as load:
        managed, errors = proactive.discover_managed_adsets(_config())

    assert managed == []
    assert errors and "direct_fb_discovery_required" in errors[0]
    discover.assert_called_once_with(force_refresh=True)
    load.assert_not_called()


def test_manifest_sorts_candidates_and_caps_safe_rows_at_ten():
    ads = tuple(
        {
            "id": f"ad-{index:02d}",
            "name": f"Ad {index}",
            "adset_id": "adset-1",
            "status": "PAUSED",
            "effective_status": "PAUSED",
            "created_time": f"2026-05-{12 - index:02d}T00:00:00+0000",
        }
        for index in range(12)
    )

    def candidate_factory(**kwargs):
        return _candidate(
            kwargs["ad"]["id"],
            ordinal=kwargs["ordinal"],
            capacity_before=2,
        )

    with patch(
        "services.proactive_adset_cleaner.build_cleanup_candidate_record",
        side_effect=candidate_factory,
    ):
        manifest = proactive.build_proactive_manifest(
            "run-1",
            [_managed(used=48, ads=ads)],
            _config(),
        )

    eligible = [row for row in manifest.candidates if row["state"] == "ELIGIBLE"]
    overflow = [
        row for row in manifest.candidates if row["reason"] == "manifest_candidate_limit"
    ]
    assert len(eligible) == 10
    assert len(overflow) == 2
    assert [row["ordinal"] for row in manifest.candidates] == list(range(12))
    assert manifest.pressure[0].safe_candidate_count == 12


@pytest.mark.parametrize(
    "used,severity,reason",
    [
        (44, "ok", None),
        (45, "warning", "safe_capacity_deficit"),
        (48, "critical", "critical_capacity"),
        (50, "critical", "adset_full"),
    ],
)
def test_pressure_thresholds(used, severity, reason):
    manifest = proactive.build_proactive_manifest(
        "run-1",
        [_managed(used=used)],
        _config(),
    )
    assert manifest.pressure[0].severity == severity
    assert manifest.pressure[0].reason == reason


@pytest.mark.parametrize(
    "local,reference,expected_reason",
    [
        (
            {
                "kb_found": True,
                "local_spend_usd": 0.0,
                "local_impressions": 0,
                "local_clicks": 0,
                "local_leads": 0,
                "local_payments": None,
                "any_positive_delivery": False,
                "any_positive_outcome": False,
                "complete": True,
                "error": None,
            },
            {"complete": True, "replacement_reference": False, "prior_delete_claim": False, "error": None},
            "local_metric_unknown",
        ),
        (
            {
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
            },
            {"complete": True, "replacement_reference": True, "prior_delete_claim": False, "error": None},
            "replacement_reference",
        ),
        (
            {
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
            },
            {"complete": True, "replacement_reference": False, "prior_delete_claim": True, "error": None},
            "prior_delete_claim",
        ),
    ],
)
def test_candidate_requires_exact_local_zero_and_no_references(local, reference, expected_reason):
    live = adset_cleaner.CleanerEvidence(
        ad_id="ad-1",
        adset_id="adset-1",
        status="PAUSED",
        effective_status="PAUSED",
        age_days=45,
        lifetime_spend=0.0,
        lifetime_impressions=0,
        lifetime_clicks=0,
        checked_at="2026-07-21T00:00:00+00:00",
    )
    with patch(
        "services.adset_cleaner.fetch_live_zero_spend_evidence_with_reason",
        return_value=(live, "ok"),
    ), patch(
        "services.adset_cleaner.load_local_zero_evidence", return_value=local
    ), patch(
        "services.adset_cleaner.load_candidate_reference_evidence",
        return_value=reference,
    ):
        record = adset_cleaner.build_cleanup_candidate_record(
            run_id="run-1",
            account_kind="offline",
            adset={
                "adset_id": "adset-1",
                "name": "Adset",
                "available": 2,
                "inventory_complete": True,
                "effective_active_count": 1,
            },
            ad={
                "id": "ad-1",
                "name": "Ad",
                "adset_id": "adset-1",
                "status": "PAUSED",
                "effective_status": "PAUSED",
            },
            ordinal=0,
            stale_days=30,
        )

    assert record["state"] == "SKIPPED"
    assert record["reason"] == expected_reason


def test_daily_run_is_durable_idempotent_and_never_creates_claim(isolated_db):
    healthy = [_managed(f"adset-{index}", used=39) for index in range(11)]
    with patch(
        "services.proactive_adset_cleaner.get_proactive_cleaner_config",
        return_value=_config(),
    ), patch(
        "services.proactive_adset_cleaner.discover_managed_adsets",
        return_value=(healthy, []),
    ) as discover, patch(
        "services.proactive_adset_cleaner.send_telegram"
    ) as telegram:
        first = proactive.run_proactive_cleaner(date(2026, 7, 21))
        second = proactive.run_proactive_cleaner(date(2026, 7, 21))

    assert first["phase"] == "COMPLETED"
    assert first["would_delete"] == []
    assert first["counters"]["deleted"] == 0
    assert second["ran"] is False
    assert second["warnings"] == ["already_completed"]
    assert discover.call_count == 1
    telegram.assert_not_called()
    conn = sqlite3.connect(isolated_db)
    try:
        runs = conn.execute(
            "SELECT run_kind, requested_mode, effective_mode FROM ad_cleanup_runs"
        ).fetchall()
        claims = conn.execute("SELECT COUNT(*) FROM ad_cleanup_delete_claims").fetchone()[0]
    finally:
        conn.close()
    assert runs == [("PROACTIVE_DAILY", "dry_run", "dry_run")]
    assert claims == 0


def test_concurrent_daily_call_does_not_start_second_scan():
    entered = threading.Event()
    release = threading.Event()
    results: list[dict] = []

    def slow_discovery(_config):
        entered.set()
        assert release.wait(timeout=5)
        return ([_managed()], [])

    with patch(
        "services.proactive_adset_cleaner.get_proactive_cleaner_config",
        return_value=_config(),
    ), patch(
        "services.proactive_adset_cleaner.discover_managed_adsets",
        side_effect=slow_discovery,
    ) as discover:
        thread = threading.Thread(
            target=lambda: results.append(
                proactive.run_proactive_cleaner(date(2026, 7, 22))
            )
        )
        thread.start()
        assert entered.wait(timeout=5)
        second = proactive.run_proactive_cleaner(date(2026, 7, 22))
        release.set()
        thread.join(timeout=5)

    assert second["ran"] is False
    assert second["warnings"] == ["already_running"]
    assert discover.call_count == 1
    assert results[0]["phase"] == "COMPLETED"


def test_pressure_alert_is_one_summary_for_whole_run():
    pressured = [_managed(f"adset-{index}", used=48) for index in range(3)]
    with patch(
        "services.proactive_adset_cleaner.get_proactive_cleaner_config",
        return_value=_config(),
    ), patch(
        "services.proactive_adset_cleaner.discover_managed_adsets",
        return_value=(pressured, []),
    ), patch(
        "services.proactive_adset_cleaner.send_telegram", return_value=True
    ) as telegram:
        result = proactive.run_proactive_cleaner(date(2026, 7, 23))

    assert result["phase"] == "COMPLETED_WITH_WARNINGS"
    telegram.assert_called_once()
    assert all(adset.adset_id in telegram.call_args.args[0] for adset in pressured)


def test_workflow_exact_deficit_runs_two_durable_single_ad_deletes(
    isolated_db,
):
    workflow_id = enqueue_replacement(
        "old-active",
        "Старое объявление",
        "adset-1",
        "CityA",
        "L1",
    )
    link_replacement_launch(
        workflow_id,
        "attempt-1",
        "card-1",
        "Карточка",
        "CityA",
        "offline",
        "account-1",
        "adset-1",
        ["CityA | Новый креатив"],
        1,
        "manifest-sha256",
    )
    config = {
        **adset_cleaner.CLEANER_DEFAULTS,
        "enabled": True,
        "dry_run": False,
        "allow_irreversible_delete": True,
        "kill_switch": False,
        "replacement_enabled": True,
    }

    def candidate_factory(**kwargs):
        return _candidate(
            kwargs["ad"]["id"],
            ordinal=kwargs["ordinal"],
            capacity_before=kwargs["adset"]["available"],
        )

    available_by_ad = {"safe-1": 0, "safe-2": 1}

    def delete_success(stale_ads, count, *, guard_evidence, authorization):
        assert count == 1
        assert guard_evidence is not None
        ad_id = stale_ads[0]["id"]
        capacity_before = available_by_ad[ad_id]
        consume_delete_authorization(
            authorization,
            claim_id=authorization.claim_id,
            workflow_id=authorization.workflow_id,
            adset_id=authorization.adset_id,
            ad_id=authorization.ad_id,
            live_capacity_before=capacity_before,
        )
        finish_cleanup_delete(
            authorization.claim_id,
            "DELETED",
            capacity_after=capacity_before + 1,
            evidence={"mocked_facebook_boundary": True},
            error=None,
        )
        return [ad_id]

    outer_capacities = [
        _capacity(available=0, stale_ids=("safe-1", "safe-2")),
        _capacity(available=0, stale_ids=("safe-1", "safe-2")),
        _capacity(available=1, stale_ids=("safe-2",)),
        _capacity(available=2, stale_ids=()),
    ]
    guarded_capacities = [
        {
            **_capacity(available=0, stale_ids=("safe-1", "safe-2")),
            "cleanup_guard_evidence": object(),
        },
        {
            **_capacity(available=1, stale_ids=("safe-2",)),
            "cleanup_guard_evidence": object(),
        },
    ]

    with patch(
        "services.adset_cleaner.get_cleaner_config", return_value=config
    ), patch(
        "services.adset_cleaner.adset_mutation_lock", return_value=nullcontext()
    ), patch(
        "services.fb_token_provider.get_fb_account_id", return_value="account-1"
    ), patch(
        "services.adset_cleaner.get_adset_capacity",
        side_effect=outer_capacities,
    ), patch(
        "services.adset_cleaner.build_cleanup_candidate_record",
        side_effect=candidate_factory,
    ), patch(
        "services.adset_cleaner.get_cleanup_capacity",
        side_effect=guarded_capacities,
    ) as guarded, patch(
        "services.adset_cleaner.cleanup_stale_ads",
        side_effect=delete_success,
    ) as delete:
        result = adset_cleaner.run_cleaner(mode="active", workflow_id=workflow_id)

    assert result["deficit_before"] == 2
    assert result["deficit_after"] == 0
    assert [row["ad_id"] for row in result["would_delete"]] == ["safe-1", "safe-2"]
    assert result["deleted"] == ["safe-1", "safe-2"]
    assert len(result["claim_ids"]) == 2
    assert result["phase"] == "COMPLETED"
    assert result["action"] == "SLOTS_RELEASED"
    assert result["errors"] == []
    assert [call.kwargs["candidate_id"] for call in guarded.call_args_list] == [
        "safe-1",
        "safe-2",
    ]
    assert delete.call_count == 2
    conn = sqlite3.connect(isolated_db)
    try:
        claims = conn.execute(
            "SELECT ad_id, state, capacity_before, capacity_after "
            "FROM ad_cleanup_delete_claims ORDER BY claimed_at, claim_id"
        ).fetchall()
        workflow = conn.execute(
            "SELECT phase, last_error FROM ad_replacement_workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        run = conn.execute(
            "SELECT phase, evidence_json FROM ad_cleanup_runs WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
    finally:
        conn.close()
    assert claims == [("safe-1", "DELETED", 0, 1), ("safe-2", "DELETED", 1, 2)]
    assert workflow == ("WAITING_CARD", None)
    assert run[0] == "COMPLETED"
    assert json.loads(run[1])["preflight_covers_deficit"] is True


def test_workflow_waits_for_safe_candidates_without_blocking_or_claiming(isolated_db):
    workflow_id = enqueue_replacement(
        "old-active",
        "Старое объявление",
        "adset-1",
        "CityA",
        "L1",
    )
    link_replacement_launch(
        workflow_id,
        "attempt-waiting",
        "card-waiting",
        "Карточка",
        "CityA",
        "offline",
        "account-1",
        "adset-1",
        ["CityA | Новый креатив"],
        1,
        "manifest-sha256",
    )
    config = {
        **adset_cleaner.CLEANER_DEFAULTS,
        "enabled": True,
        "dry_run": False,
        "allow_irreversible_delete": True,
        "kill_switch": False,
        "replacement_enabled": True,
    }
    capacity = _capacity(available=0, stale_ids=("safe-1", "unsafe-2"))

    def candidate_factory(**kwargs):
        ad_id = kwargs["ad"]["id"]
        if ad_id == "safe-1":
            return _candidate(ad_id, ordinal=kwargs["ordinal"], capacity_before=0)
        return _candidate(
            ad_id,
            ordinal=kwargs["ordinal"],
            capacity_before=0,
            state="SKIPPED",
            reason="local_nonzero",
        )

    with patch(
        "services.adset_cleaner.get_cleaner_config", return_value=config
    ), patch(
        "services.adset_cleaner.adset_mutation_lock", return_value=nullcontext()
    ), patch(
        "services.fb_token_provider.get_fb_account_id", return_value="account-1"
    ), patch(
        "services.adset_cleaner.get_adset_capacity", return_value=capacity
    ), patch(
        "services.adset_cleaner.build_cleanup_candidate_record",
        side_effect=candidate_factory,
    ), patch(
        "services.adset_cleaner.claim_replacement_slot_delete"
    ) as claim, patch(
        "services.adset_cleaner.cleanup_stale_ads"
    ) as delete:
        result = adset_cleaner.run_cleaner(mode="active", workflow_id=workflow_id)

    assert result["action"] == "WAITING_SAFE_CANDIDATES"
    assert result["phase"] == "COMPLETED_WITH_WARNINGS"
    assert result["deficit_after"] == 2
    assert result["claim_ids"] == []
    claim.assert_not_called()
    delete.assert_not_called()
    conn = sqlite3.connect(isolated_db)
    try:
        workflow_phase = conn.execute(
            "SELECT phase FROM ad_replacement_workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()[0]
        claim_count = conn.execute(
            "SELECT COUNT(*) FROM ad_cleanup_delete_claims"
        ).fetchone()[0]
    finally:
        conn.close()
    assert workflow_phase == "WAITING_SLOT"
    assert claim_count == 0


def test_workflow_entry_rejects_dry_run_without_reading_facebook():
    with patch("services.adset_cleaner.get_cleaner_config") as config, patch(
        "services.adset_cleaner.get_adset_capacity"
    ) as capacity:
        result = adset_cleaner.run_cleaner(mode="dry_run", workflow_id="workflow-1")

    assert result["ran"] is False
    assert result["skipped_reason"] == "workflow_active_only"
    config.assert_not_called()
    capacity.assert_not_called()


def test_workflow_stops_after_ambiguous_delete_without_second_claim(isolated_db):
    workflow_id = enqueue_replacement(
        "old-active",
        "Старое объявление",
        "adset-1",
        "CityA",
        "L1",
    )
    link_replacement_launch(
        workflow_id,
        "attempt-ambiguous",
        "card-ambiguous",
        "Карточка",
        "CityA",
        "offline",
        "account-1",
        "adset-1",
        ["CityA | Новый креатив"],
        1,
        "manifest-sha256",
    )
    config = {
        **adset_cleaner.CLEANER_DEFAULTS,
        "enabled": True,
        "dry_run": False,
        "allow_irreversible_delete": True,
        "kill_switch": False,
        "replacement_enabled": True,
    }

    def candidate_factory(**kwargs):
        return _candidate(
            kwargs["ad"]["id"],
            ordinal=kwargs["ordinal"],
            capacity_before=kwargs["adset"]["available"],
        )

    def ambiguous_delete(stale_ads, count, *, guard_evidence, authorization):
        assert count == 1
        assert guard_evidence is not None
        consume_delete_authorization(
            authorization,
            claim_id=authorization.claim_id,
            workflow_id=authorization.workflow_id,
            adset_id=authorization.adset_id,
            ad_id=authorization.ad_id,
            live_capacity_before=0,
        )
        finish_cleanup_delete(
            authorization.claim_id,
            "RECONCILE_REQUIRED",
            capacity_after=None,
            evidence={"mocked_ambiguous_delete": stale_ads[0]["id"]},
            error="cleanup_delete_transport_ambiguous",
        )
        raise CleanupDeleteReconcileRequired("cleanup_delete_reconcile_required")

    initial = _capacity(available=0, stale_ids=("safe-1", "safe-2"))
    guarded = {**initial, "cleanup_guard_evidence": object()}
    with patch(
        "services.adset_cleaner.get_cleaner_config", return_value=config
    ), patch(
        "services.adset_cleaner.adset_mutation_lock", return_value=nullcontext()
    ), patch(
        "services.fb_token_provider.get_fb_account_id", return_value="account-1"
    ), patch(
        "services.adset_cleaner.get_adset_capacity", return_value=initial
    ), patch(
        "services.adset_cleaner.build_cleanup_candidate_record",
        side_effect=candidate_factory,
    ), patch(
        "services.adset_cleaner.get_cleanup_capacity", return_value=guarded
    ), patch(
        "services.adset_cleaner.cleanup_stale_ads", side_effect=ambiguous_delete
    ) as delete:
        result = adset_cleaner.run_cleaner(mode="active", workflow_id=workflow_id)

    assert result["phase"] == "BLOCKED"
    assert result["deleted"] == []
    assert len(result["claim_ids"]) == 1
    assert result["errors"] == ["cleanup_delete_reconcile_required"]
    delete.assert_called_once()
    conn = sqlite3.connect(isolated_db)
    try:
        claims = conn.execute(
            "SELECT ad_id, state FROM ad_cleanup_delete_claims ORDER BY claimed_at"
        ).fetchall()
        run_phase = conn.execute(
            "SELECT phase FROM ad_cleanup_runs WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert claims == [("safe-1", "RECONCILE_REQUIRED")]
    assert run_phase == "BLOCKED"


def test_workflow_preflight_fault_finishes_run_blocked(isolated_db):
    workflow_id = enqueue_replacement(
        "old-active",
        "Старое объявление",
        "adset-1",
        "CityA",
        "L1",
    )
    link_replacement_launch(
        workflow_id,
        "attempt-fault",
        "card-fault",
        "Карточка",
        "CityA",
        "offline",
        "account-1",
        "adset-1",
        ["CityA | Новый креатив"],
        1,
        "manifest-sha256",
    )
    config = {
        **adset_cleaner.CLEANER_DEFAULTS,
        "enabled": True,
        "dry_run": False,
        "allow_irreversible_delete": True,
        "kill_switch": False,
        "replacement_enabled": True,
    }
    with patch(
        "services.adset_cleaner.get_cleaner_config", return_value=config
    ), patch(
        "services.adset_cleaner.adset_mutation_lock", return_value=nullcontext()
    ), patch(
        "services.fb_token_provider.get_fb_account_id", return_value="account-1"
    ), patch(
        "services.adset_cleaner.get_adset_capacity",
        return_value=_capacity(available=0, stale_ids=("safe-1", "safe-2")),
    ), patch(
        "services.adset_cleaner.build_cleanup_candidate_record",
        return_value=_candidate("safe-1", ordinal=0, capacity_before=0),
    ), patch(
        "services.adset_cleaner.upsert_cleanup_candidate",
        side_effect=RuntimeError("injected persistence failure"),
    ):
        result = adset_cleaner.run_cleaner(mode="active", workflow_id=workflow_id)

    assert result["phase"] == "BLOCKED"
    assert any("workflow_slot_plan_failed" in error for error in result["errors"])
    conn = sqlite3.connect(isolated_db)
    try:
        run_phase = conn.execute(
            "SELECT phase FROM ad_cleanup_runs WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()[0]
        claim_count = conn.execute(
            "SELECT COUNT(*) FROM ad_cleanup_delete_claims"
        ).fetchone()[0]
    finally:
        conn.close()
    assert run_phase == "BLOCKED"
    assert claim_count == 0
