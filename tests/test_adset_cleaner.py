"""Safety-тесты zero-spend cleaner.

Все Facebook-вызовы замоканы. Тесты не используют production DB и не выполняют
необратимых операций за пределами явно проверяемого mock-вызова.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from services.adset_cleaner import (
    CLEANER_DEFAULTS,
    CleanerEvidence,
    _ad_age_days,
    _validate_capacity,
    build_cleanup_candidate_record,
    fetch_live_zero_spend_evidence,
    get_cleaner_config,
    is_safe_zero_candidate,
    load_local_zero_evidence,
    run_cleaner,
    select_safe_candidates,
)


def _created_days_ago(days: int) -> str:
    created = datetime.now(timezone.utc) - timedelta(days=days, minutes=1)
    return created.strftime("%Y-%m-%dT%H:%M:%S+0000")


def _ad(ad_id: str, days: int = 31, status: str = "PAUSED") -> dict:
    return {
        "id": ad_id,
        "name": f"Ad {ad_id}",
        "status": status,
        "created_time": _created_days_ago(days),
    }


def _capacity(
    *,
    adset_id: str = "123",
    available: int = 0,
    ad_count: int = 50,
    stale_ads: list[dict] | None = None,
    effective_active_ids: list[str] | None = None,
) -> dict:
    stale = stale_ads or []
    stale_ids = [str(ad["id"]) for ad in stale]
    if effective_active_ids is None:
        effective_active_ids = ["active-guard"] if ad_count > 0 else []
    ad_ids = list(dict.fromkeys([*stale_ids, *effective_active_ids]))
    while len(ad_ids) < ad_count:
        ad_ids.append(f"inventory-{len(ad_ids)}")
    return {
        "adset_id": adset_id,
        "name": f"Adset {adset_id}",
        "ad_count": ad_count,
        "max_ads": 50,
        "available": available,
        "stale_ads": stale,
        "ad_ids": ad_ids,
        "effective_active_ids": effective_active_ids,
        "effective_active_count": len(effective_active_ids),
        "inventory_complete": True,
        "cleanup_guard_evidence": object(),
    }


def _cfg(**overrides) -> dict:
    config = {**CLEANER_DEFAULTS, "kill_switch": False}
    config.update(overrides)
    return config


def _live(
    ad_id: str,
    *,
    adset_id: str = "123",
    status: str = "PAUSED",
    effective_status: str = "PAUSED",
    age_days: int = 31,
    spend: float = 0.0,
    impressions: int = 0,
    clicks: int = 0,
) -> CleanerEvidence:
    return CleanerEvidence(
        ad_id=ad_id,
        adset_id=adset_id,
        status=status,
        effective_status=effective_status,
        age_days=age_days,
        lifetime_spend=spend,
        lifetime_impressions=impressions,
        lifetime_clicks=clicks,
        checked_at="2026-07-20T10:00:00+00:00",
    )


def _local(**overrides) -> dict:
    evidence = {
        "kb_found": True,
        "local_spend_usd": 0.0,
        "any_positive_delivery": False,
        "any_positive_outcome": False,
        "complete": True,
        "error": None,
    }
    evidence.update(overrides)
    return evidence




class FakeResponse:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _create_local_db(path, *, kb: dict | None = None, omit: str | None = None) -> None:
    kb = kb or {}
    conn = sqlite3.connect(path)
    try:
        if omit != "creative_kb":
            conn.execute(
                """
                CREATE TABLE creative_kb (
                    ad_id TEXT, spend REAL, impressions INTEGER, clicks INTEGER,
                    payments INTEGER, romi REAL, revenue REAL,
                    payments_erp INTEGER, revenue_erp_lcy REAL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO creative_kb VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ad-1",
                    kb.get("spend", 0),
                    kb.get("impressions", 0),
                    kb.get("clicks", 0),
                    kb.get("payments"),
                    kb.get("romi"),
                    kb.get("revenue"),
                    kb.get("payments_erp"),
                    kb.get("revenue_erp_lcy"),
                ),
            )
        if omit != "ad_daily_metrics":
            conn.execute(
                "CREATE TABLE ad_daily_metrics ("
                "ad_id TEXT, spend REAL, impressions INTEGER, clicks INTEGER, "
                "lead_semantics_version INTEGER, lead_parse_status TEXT)"
            )
            conn.execute("INSERT INTO ad_daily_metrics VALUES ('ad-1', 0, 0, 0, 2, 'ok')")
        if omit != "ad_hourly_metrics":
            conn.execute(
                "CREATE TABLE ad_hourly_metrics ("
                "ad_id TEXT, spend REAL, impressions INTEGER, clicks INTEGER, "
                "lead_semantics_version INTEGER, lead_parse_status TEXT)"
            )
            conn.execute("INSERT INTO ad_hourly_metrics VALUES ('ad-1', 0, 0, 0, 2, 'ok')")
        if omit != "decisions":
            conn.execute("CREATE TABLE decisions (ad_id TEXT, spend REAL, romi REAL)")
            conn.execute("INSERT INTO decisions VALUES ('ad-1', 0, 0)")
        conn.commit()
    finally:
        conn.close()


def test_cleaner_defaults_are_non_destructive_and_15_days():
    assert CLEANER_DEFAULTS["enabled"] is False
    assert CLEANER_DEFAULTS["dry_run"] is True
    assert CLEANER_DEFAULTS["allow_irreversible_delete"] is False
    assert CLEANER_DEFAULTS["stale_days"] == 15
    assert CLEANER_DEFAULTS["max_deletes_per_workflow"] == 2


@pytest.mark.parametrize(
    "mutation",
    [
        lambda cap: cap.pop("available"),
        lambda cap: cap.update(ad_count=-1),
        lambda cap: cap.update(ad_count="50"),
        lambda cap: cap.update(max_ads=49),
        lambda cap: cap.update(available=1),
        lambda cap: cap.update(stale_ads="not-a-list"),
        lambda cap: cap.update(adset_id="other"),
        lambda cap: cap.update(inventory_complete=False),
        lambda cap: cap.update(effective_active_count=0),
    ],
)
def test_capacity_requires_exact_complete_consistent_snapshot(mutation):
    capacity = _capacity()
    mutation(capacity)
    with pytest.raises(ValueError):
        _validate_capacity(capacity, "123")


def test_partial_nested_config_keeps_deep_safe_defaults():
    with patch(
        "services.autopilot.get_autopilot_config",
        return_value={"kill_switch": False, "cleaner": {"enabled": True}},
    ):
        config = get_cleaner_config()

    assert config["enabled"] is True
    assert config["stale_days"] == 15
    assert config["dry_run"] is True
    assert config["allow_irreversible_delete"] is False
    assert config["adset_threshold"] == 45
    assert config["target_free"] == 5
    assert "max_deletes_per_run" not in config
    assert config["max_deletes_per_workflow"] == 2


def test_ad_age_uses_full_days():
    assert _ad_age_days(_created_days_ago(30)) == 30


@pytest.mark.parametrize(
    ("age_days", "expected_state", "expected_reason"),
    [
        pytest.param(14, "SKIPPED", "recent_ad", id="14-days-skipped"),
        pytest.param(
            15,
            "ELIGIBLE",
            "exact_zero_evidence",
            id="15-days-eligible",
        ),
        pytest.param(
            16,
            "ELIGIBLE",
            "exact_zero_evidence",
            id="16-days-eligible",
        ),
    ],
)
def test_candidate_age_boundary_is_inclusive_15_days(
    age_days, expected_state, expected_reason
):
    """Возраст 15 полных дней включается, 14 — ещё нет."""
    local_zero = {
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
    references = {
        "complete": True,
        "replacement_reference": False,
        "prior_delete_claim": False,
        "error": None,
    }
    with patch(
        "services.adset_cleaner.fetch_live_zero_spend_evidence_with_reason",
        return_value=(_live("ad-boundary", age_days=age_days), "ok"),
    ), patch(
        "services.adset_cleaner.load_local_zero_evidence", return_value=local_zero
    ), patch(
        "services.adset_cleaner.load_candidate_reference_evidence",
        return_value=references,
    ):
        candidate = build_cleanup_candidate_record(
            run_id="run-boundary",
            account_kind="offline",
            adset={
                "adset_id": "123",
                "name": "Adset 123",
                "available": 2,
                "inventory_complete": True,
                "effective_active_count": 1,
            },
            ad={
                "id": "ad-boundary",
                "name": "Boundary ad",
                "adset_id": "123",
                "status": "PAUSED",
                "effective_status": "PAUSED",
            },
            ordinal=0,
            stale_days=15,
        )

    assert candidate["state"] == expected_state
    assert candidate["reason"] == expected_reason


def test_live_evidence_requires_non_empty_maximum_insights():
    exact = FakeResponse(
        200,
        {
            "id": "ad-1",
            "adset_id": "123",
            "status": "PAUSED",
            "effective_status": "ADSET_PAUSED",
            "created_time": _created_days_ago(31),
        },
    )
    insights = FakeResponse(
        200,
        {"data": [{"spend": "0", "impressions": "0", "clicks": "0"}]},
    )
    with patch("agent.fb_common._throttled_get", side_effect=[exact, insights]) as get, patch(
        "services.fb_token_provider.get_fb_token", return_value="test-token"
    ):
        evidence = fetch_live_zero_spend_evidence("ad-1", "123")

    assert evidence is not None
    assert evidence.lifetime_spend == 0.0
    assert evidence.effective_status == "ADSET_PAUSED"
    assert get.call_args_list[1].kwargs["params"]["date_preset"] == "maximum"


@pytest.mark.parametrize(
    "insights",
    [
        FakeResponse(200, {"data": []}),
        FakeResponse(200, {"data": [{"spend": "bad", "impressions": "0", "clicks": "0"}]}),
        FakeResponse(500, {"error": "down"}),
        FakeResponse(200, {"data": [{"spend": "0", "impressions": "0", "clicks": "0"}, {}]}),
        FakeResponse(
            200,
            {
                "data": [{"spend": "0", "impressions": "0", "clicks": "0"}],
                "paging": {"next": "https://graph.facebook.com/next"},
            },
        ),
        FakeResponse(
            200,
            {
                "data": [{"spend": "0", "impressions": "0", "clicks": "0"}],
                "paging": "not-an-object",
            },
        ),
    ],
)
def test_live_evidence_fails_closed_on_empty_error_unparseable_or_ambiguous(insights):
    exact = FakeResponse(
        200,
        {
            "id": "ad-1",
            "adset_id": "123",
            "status": "PAUSED",
            "effective_status": "PAUSED",
            "created_time": _created_days_ago(31),
        },
    )
    with patch("agent.fb_common._throttled_get", side_effect=[exact, insights]), patch(
        "services.fb_token_provider.get_fb_token", return_value="test-token"
    ):
        assert fetch_live_zero_spend_evidence("ad-1", "123") is None


def test_local_zero_evidence_accepts_null_outcomes_when_delivery_is_confirmed_zero(tmp_path):
    db_path = tmp_path / "cleaner.db"
    _create_local_db(db_path)
    with patch("services.adset_cleaner._DECISIONS_DB_PATH", db_path):
        evidence = load_local_zero_evidence("ad-1")

    assert evidence["complete"] is True
    assert evidence["kb_found"] is True
    assert evidence["any_positive_delivery"] is False
    assert evidence["any_positive_outcome"] is False


def test_local_zero_evidence_legacy_metric_row_fails_closed(tmp_path):
    db_path = tmp_path / "legacy-cleaner.db"
    _create_local_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE ad_daily_metrics SET lead_semantics_version = 1, "
            "lead_parse_status = 'legacy' WHERE ad_id = 'ad-1'"
        )
        conn.commit()
    finally:
        conn.close()

    with patch("services.adset_cleaner._DECISIONS_DB_PATH", db_path):
        evidence = load_local_zero_evidence("ad-1")

    safe, _ = is_safe_zero_candidate({"id": "ad-1"}, _live("ad-1"), evidence)
    assert evidence["complete"] is False
    assert evidence["error"] == "untrusted_lead_semantics"
    assert safe is False


@pytest.mark.parametrize("omit", ["creative_kb", "ad_daily_metrics", "ad_hourly_metrics", "decisions"])
def test_local_zero_evidence_missing_required_table_fails_closed(tmp_path, omit):
    db_path = tmp_path / "cleaner.db"
    _create_local_db(db_path, omit=omit)
    with patch("services.adset_cleaner._DECISIONS_DB_PATH", db_path):
        evidence = load_local_zero_evidence("ad-1")

    safe, _ = is_safe_zero_candidate({"id": "ad-1"}, _live("ad-1"), evidence)
    assert evidence["complete"] is False
    assert safe is False
    assert "missing_tables" in (evidence["error"] or "")


def test_local_zero_evidence_null_kb_spend_fails_closed(tmp_path):
    db_path = tmp_path / "cleaner.db"
    _create_local_db(db_path, kb={"spend": None})
    with patch("services.adset_cleaner._DECISIONS_DB_PATH", db_path):
        evidence = load_local_zero_evidence("ad-1")

    assert evidence["complete"] is False


def test_local_zero_evidence_allows_empty_optional_sources_with_kb_and_live_zero(tmp_path):
    db_path = tmp_path / "empty.db"
    _create_local_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM ad_daily_metrics")
        conn.execute("DELETE FROM ad_hourly_metrics")
        conn.execute("DELETE FROM decisions")
        conn.commit()
    finally:
        conn.close()

    with patch("services.adset_cleaner._DECISIONS_DB_PATH", db_path):
        evidence = load_local_zero_evidence("ad-1")

    safe, reason = is_safe_zero_candidate({"id": "ad-1"}, _live("ad-1"), evidence)

    assert evidence["complete"] is True
    assert evidence["local_spend_usd"] == 0.0
    assert safe is True
    assert reason == "confirmed_zero_spend"


def test_local_zero_evidence_allows_null_decision_spend_and_romi(tmp_path):
    db_path = tmp_path / "null-decisions.db"
    _create_local_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE decisions SET spend = ?, romi = ? WHERE ad_id = ?",
            (None, None, "ad-1"),
        )
        conn.commit()
    finally:
        conn.close()

    with patch("services.adset_cleaner._DECISIONS_DB_PATH", db_path):
        evidence = load_local_zero_evidence("ad-1")

    assert evidence["complete"] is True
    assert evidence["any_positive_delivery"] is False
    assert evidence["any_positive_outcome"] is False


@pytest.mark.parametrize(
    "table,column,bad_value",
    [
        ("creative_kb", "spend", "text"),
        ("creative_kb", "impressions", None),
        ("ad_daily_metrics", "spend", "text"),
        ("ad_daily_metrics", "clicks", -1),
        ("ad_hourly_metrics", "impressions", None),
        ("ad_hourly_metrics", "spend", -0.1),
        ("decisions", "spend", "text"),
        ("decisions", "spend", -0.1),
        ("decisions", "romi", "text"),
        ("decisions", "romi", -1),
    ],
)
def test_local_zero_evidence_bad_raw_value_fails_closed(tmp_path, table, column, bad_value):
    db_path = tmp_path / "bad.db"
    _create_local_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        update_sql = {
            ("creative_kb", "spend"): "UPDATE creative_kb SET spend = ? WHERE ad_id = ?",
            ("creative_kb", "impressions"): "UPDATE creative_kb SET impressions = ? WHERE ad_id = ?",
            ("ad_daily_metrics", "spend"): "UPDATE ad_daily_metrics SET spend = ? WHERE ad_id = ?",
            ("ad_daily_metrics", "clicks"): "UPDATE ad_daily_metrics SET clicks = ? WHERE ad_id = ?",
            ("ad_hourly_metrics", "impressions"): "UPDATE ad_hourly_metrics SET impressions = ? WHERE ad_id = ?",
            ("ad_hourly_metrics", "spend"): "UPDATE ad_hourly_metrics SET spend = ? WHERE ad_id = ?",
            ("decisions", "spend"): "UPDATE decisions SET spend = ? WHERE ad_id = ?",
            ("decisions", "romi"): "UPDATE decisions SET romi = ? WHERE ad_id = ?",
        }[(table, column)]
        conn.execute(update_sql, (bad_value, "ad-1"))
        conn.commit()
    finally:
        conn.close()

    with patch("services.adset_cleaner._DECISIONS_DB_PATH", db_path):
        evidence = load_local_zero_evidence("ad-1")

    safe, _ = is_safe_zero_candidate({"id": "ad-1"}, _live("ad-1"), evidence)
    assert evidence["complete"] is False
    assert safe is False


@pytest.mark.parametrize(
    "column",
    ["spend", "romi"],
)
def test_local_positive_decision_value_is_veto(tmp_path, column):
    db_path = tmp_path / f"positive-decision-{column}.db"
    _create_local_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        update_sql = {
            "spend": "UPDATE decisions SET spend = ? WHERE ad_id = ?",
            "romi": "UPDATE decisions SET romi = ? WHERE ad_id = ?",
        }[column]
        conn.execute(
            update_sql,
            (1, "ad-1"),
        )
        conn.commit()
    finally:
        conn.close()

    with patch("services.adset_cleaner._DECISIONS_DB_PATH", db_path):
        evidence = load_local_zero_evidence("ad-1")

    safe, _ = is_safe_zero_candidate({"id": "ad-1"}, _live("ad-1"), evidence)
    assert evidence["complete"] is True
    assert evidence["any_positive_delivery"] or evidence["any_positive_outcome"]
    assert safe is False


@pytest.mark.parametrize(
    "kb_field",
    ["spend", "impressions", "clicks", "payments", "romi", "revenue", "payments_erp", "revenue_erp_lcy"],
)
def test_local_positive_delivery_or_outcome_is_veto(tmp_path, kb_field):
    db_path = tmp_path / f"{kb_field}.db"
    _create_local_db(db_path, kb={kb_field: 1})
    with patch("services.adset_cleaner._DECISIONS_DB_PATH", db_path):
        evidence = load_local_zero_evidence("ad-1")

    assert evidence["any_positive_delivery"] or evidence["any_positive_outcome"]


@pytest.mark.parametrize(
    "effective_status,expected",
    [
        ("PAUSED", True),
        ("ADSET_PAUSED", True),
        ("CAMPAIGN_PAUSED", True),
        ("ACTIVE", False),
        ("PENDING_REVIEW", False),
        ("IN_PROCESS", False),
        ("PREAPPROVED", False),
        ("WITH_ISSUES", False),
        ("DISAPPROVED", False),
        ("UNKNOWN", False),
    ],
)
def test_only_safe_paused_effective_status_variants_are_eligible(effective_status, expected):
    safe, _ = is_safe_zero_candidate(
        {"id": "ad-1"},
        _live("ad-1", effective_status=effective_status),
        _local(),
    )
    assert safe is expected


def test_live_or_local_positive_spend_is_veto():
    live_safe, live_reason = is_safe_zero_candidate(
        {"id": "ad-1"}, _live("ad-1", spend=0.01), _local()
    )
    local_safe, local_reason = is_safe_zero_candidate(
        {"id": "ad-1"}, _live("ad-1"), _local(any_positive_delivery=True)
    )

    assert live_safe is False and live_reason == "positive_lifetime_spend"
    assert local_safe is False and local_reason == "positive_local_delivery"


def test_select_safe_candidates_refills_after_unsafe_rows():
    ads = [_ad(f"ad-{number}") for number in range(1, 6)]
    capacity = _capacity(stale_ads=ads)
    live_by_id = {
        "ad-1": None,
        "ad-2": _live("ad-2", spend=1.0),
        "ad-3": _live("ad-3"),
        "ad-4": _live("ad-4"),
        "ad-5": _live("ad-5"),
    }
    local_by_id = {
        "ad-2": _local(),
        "ad-3": _local(any_positive_outcome=True),
        "ad-4": _local(),
        "ad-5": _local(),
    }
    with patch(
        "services.adset_cleaner.fetch_live_zero_spend_evidence",
        side_effect=lambda ad_id, _adset_id: live_by_id[ad_id],
    ), patch(
        "services.adset_cleaner.load_local_zero_evidence",
        side_effect=lambda ad_id: local_by_id[ad_id],
    ):
        selected, skipped = select_safe_candidates(capacity, 2, _cfg())

    assert [row["ad_id"] for row in selected] == ["ad-4", "ad-5"]
    assert [row["ad_id"] for row in skipped] == ["ad-1", "ad-2", "ad-3"]


def test_14_day_ad_is_skipped_even_with_one_day_override():
    capacity = _capacity(stale_ads=[_ad("ad-14", days=14)])
    with patch("services.adset_cleaner.fetch_live_zero_spend_evidence") as live:
        selected, skipped = select_safe_candidates(capacity, 1, _cfg(stale_days=1))

    assert selected == []
    assert skipped[0]["reason"] == "recent_ad"
    live.assert_not_called()


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("enabled", 1),
        ("dry_run", "false"),
        ("allow_irreversible_delete", 1),
        ("kill_switch", "false"),
    ],
)
def test_safety_flags_require_literal_json_booleans(field, bad_value):
    config = _cfg(enabled=True)
    config[field] = bad_value
    with patch("services.adset_cleaner.get_cleaner_config", return_value=config), patch(
        "services.adset_cleaner.cleanup_stale_ads"
    ) as delete:
        result = run_cleaner(mode="active", workflow_id="wf-1")

    assert result["ran"] is False
    assert result["skipped_reason"] == "invalid_config"
    assert any("must_be_literal_bool" in error for error in result["errors"])
    delete.assert_not_called()


@pytest.mark.parametrize("stale_days", [1, 14])
def test_active_entry_rejects_stale_days_below_15_without_facebook(stale_days):
    config = _cfg(
        enabled=True,
        dry_run=False,
        allow_irreversible_delete=True,
        replacement_enabled=True,
        stale_days=stale_days,
    )
    with patch(
        "services.adset_cleaner.get_cleaner_config", return_value=config
    ), patch("services.adset_cleaner.get_adset_capacity") as capacity, patch(
        "services.adset_cleaner.cleanup_stale_ads"
    ) as delete:
        result = run_cleaner(mode="active", workflow_id="wf-1")

    assert result["ran"] is False
    assert result["skipped_reason"] == "stale_days_below_15"
    capacity.assert_not_called()
    delete.assert_not_called()


@pytest.mark.parametrize(
    "config_override,reason",
    [
        ({"enabled": False}, "cleaner_disabled"),
        ({"enabled": True, "kill_switch": True}, "kill_switch"),
        ({"enabled": True, "replacement_enabled": False}, "replacement_disabled"),
    ],
)
def test_active_master_gates_stop_before_provider_calls(config_override, reason):
    config = _cfg(
        dry_run=False,
        allow_irreversible_delete=True,
        replacement_enabled=True,
    )
    config.update(config_override)
    with patch(
        "services.adset_cleaner.get_cleaner_config", return_value=config
    ), patch("services.adset_cleaner.get_workflow") as workflow, patch(
        "services.adset_cleaner.cleanup_stale_ads"
    ) as delete:
        result = run_cleaner(mode="active", workflow_id="wf-1")

    assert result["ran"] is False
    assert result["skipped_reason"] == reason
    workflow.assert_not_called()
    delete.assert_not_called()
