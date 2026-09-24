"""Контрактные тесты чистой kill-policy."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from services.kill_policy import (
    Decision,
    EvidenceStatus,
    KillCandidate,
    KillPolicyConfigError,
    RolloutMode,
    ZeroLeadPolicy,
    evaluate_kill_policy,
    parse_kill_candidate,
    parse_kill_policy_config,
)


def _candidate(**overrides: object) -> KillCandidate:
    raw: dict[str, object] = {
        "segment": "CityA|L2",
        "lifetime_evidence": EvidenceStatus.COMPLETE.value,
        "qualification_evidence": EvidenceStatus.COMPLETE.value,
        "age_days": 3,
        "lifetime_leads": 0,
        "lifetime_spend": "150",
        "mature_leads": 5,
        "qualified_mature_leads": 0,
        "maturity_profile": "d3",
        "maturity_event_at": "2026-07-22T00:00:00Z",
    }
    raw.update(overrides)
    return parse_kill_candidate(raw)


def _spend_config(mode: str = "shadow", **overrides: object):
    raw: dict[str, object] = {
        "zero_lead_policy": "spend_cpl",
        "contour_a_rollout": mode,
        "spend_cpl_multiplier": "3",
        "targets": [
            {
                "segment": "CityA|L2",
                "target_cpl": "10",
                "planned_qualification_rate": "0.2",
            }
        ],
    }
    raw.update(overrides)
    return parse_kill_policy_config(raw)


def _quality_config(mode: str = "shadow", **overrides: object):
    raw: dict[str, object] = {
        "quality_rollout": mode,
        "quality_min_mature_leads": 5,
        "approved_maturity_profiles": ["d3"],
        "targets": [
            {
                "segment": "CityA|L2",
                "target_cpl": "10",
                "planned_qualification_rate": "0.2",
            }
        ],
    }
    raw.update(overrides)
    return parse_kill_policy_config(raw)


def test_safe_defaults_are_shadow_and_non_mutating():
    config = parse_kill_policy_config({})
    plan = evaluate_kill_policy(config, _candidate())

    assert config.zero_lead_policy is ZeroLeadPolicy.AGE_3D
    assert config.contour_a_rollout is RolloutMode.SHADOW
    assert config.quality_rollout is RolloutMode.OFF
    assert plan.zero_lead.decision is Decision.PAUSE
    assert plan.zero_lead.mutation_allowed is False
    assert plan.decision is Decision.WAIT
    assert plan.mutation_allowed is False


def test_age3_active_requires_explicit_rollout():
    config = parse_kill_policy_config({"contour_a_rollout": "active"})
    plan = evaluate_kill_policy(config, _candidate())

    assert plan.zero_lead.mode is RolloutMode.ACTIVE
    assert plan.zero_lead.decision is Decision.PAUSE
    assert plan.decision is Decision.PAUSE
    assert plan.mutation_allowed is True


@pytest.mark.parametrize(
    ("candidate_overrides", "reason"),
    [
        ({"age_days": 2}, "age_below_3d"),
        ({"age_days": None}, "insufficient_age"),
        ({"lifetime_leads": None}, "insufficient_lifetime_leads"),
        ({"lifetime_leads": 1}, "lifetime_leads_present"),
        ({"lifetime_evidence": "incomplete"}, "insufficient_lifetime_evidence"),
        ({"lifetime_evidence": "unknown"}, "insufficient_lifetime_evidence"),
    ],
)
def test_age3_fail_closed_boundaries(candidate_overrides, reason):
    plan = evaluate_kill_policy(
        parse_kill_policy_config({}),
        _candidate(**candidate_overrides),
    )

    assert plan.zero_lead.mutation_allowed is False
    assert plan.zero_lead.reason == reason


def test_contour_a_off_disables_selected_age_policy():
    plan = evaluate_kill_policy(
        parse_kill_policy_config({"contour_a_rollout": "off"}),
        _candidate(),
    )
    assert plan.zero_lead.reason == "contour_off"
    assert plan.zero_lead.decision is Decision.WAIT


def test_selected_zero_lead_policy_is_exclusive():
    plan = evaluate_kill_policy(
        _spend_config(),
        _candidate(age_days=30, lifetime_spend="29.99"),
    )
    assert plan.zero_lead.contour == "zero_lead_spend_cpl"
    assert plan.zero_lead.decision is Decision.WAIT
    assert plan.zero_lead.threshold == Decimal("30")


@pytest.mark.parametrize(
    ("candidate_overrides", "reason"),
    [
        ({"lifetime_evidence": "unknown"}, "insufficient_lifetime_evidence"),
        ({"lifetime_leads": None}, "insufficient_lifetime_leads"),
        ({"lifetime_leads": 1}, "lifetime_leads_present"),
        ({"lifetime_spend": None}, "insufficient_spend"),
    ],
)
def test_spend_policy_requires_lifetime_evidence(candidate_overrides, reason):
    plan = evaluate_kill_policy(_spend_config(), _candidate(**candidate_overrides))
    assert plan.zero_lead.reason == reason
    assert plan.zero_lead.mutation_allowed is False


def test_spend_policy_exact_threshold_shadow_then_active():
    shadow = evaluate_kill_policy(_spend_config(), _candidate(lifetime_spend="30"))
    active = evaluate_kill_policy(
        _spend_config("active"),
        _candidate(lifetime_spend="30"),
    )

    assert shadow.zero_lead.decision is Decision.PAUSE
    assert shadow.zero_lead.threshold == Decimal("30")
    assert shadow.decision is Decision.WAIT
    assert shadow.mutation_allowed is False
    assert active.decision is Decision.PAUSE
    assert active.mutation_allowed is True


def test_spend_policy_missing_config_waits_in_shadow():
    config = parse_kill_policy_config(
        {"zero_lead_policy": "spend_cpl", "contour_a_rollout": "shadow"}
    )
    plan = evaluate_kill_policy(config, _candidate())
    assert plan.zero_lead.reason == "insufficient_multiplier"


def test_quality_off_never_evaluates_candidate():
    plan = evaluate_kill_policy(parse_kill_policy_config({}), _candidate())
    assert plan.quality.reason == "contour_off"


@pytest.mark.parametrize("status", ["incomplete", "unknown"])
def test_quality_requires_complete_qualification_evidence(status):
    plan = evaluate_kill_policy(
        _quality_config(),
        _candidate(qualification_evidence=status),
    )
    assert plan.quality.reason == "insufficient_qualification_evidence"
    assert plan.quality.mutation_allowed is False


def test_quality_does_not_infer_from_lifetime_evidence():
    plan = evaluate_kill_policy(
        _quality_config(),
        _candidate(lifetime_evidence="unknown"),
    )
    assert plan.zero_lead.reason == "insufficient_lifetime_evidence"
    assert plan.quality.decision is Decision.PAUSE
    assert plan.quality.reason == "quality_spend_threshold_reached_with_zero_qualifications"


@pytest.mark.parametrize(
    ("config_overrides", "candidate_overrides", "reason"),
    [
        ({"targets": []}, {}, "insufficient_exact_target"),
        ({"approved_maturity_profiles": []}, {}, "insufficient_approved_profiles"),
        ({}, {"maturity_profile": None}, "insufficient_maturity_profile"),
        ({}, {"maturity_profile": "d7"}, "unapproved_maturity_profile"),
        ({}, {"maturity_event_at": None}, "insufficient_maturity_event"),
        ({"quality_min_mature_leads": None}, {}, "insufficient_min_sample"),
        ({}, {"mature_leads": None}, "insufficient_mature_sample"),
        ({}, {"mature_leads": 4}, "mature_sample_below_minimum"),
        ({}, {"qualified_mature_leads": None}, "insufficient_qualified_mature_leads"),
        (
            {"targets": [{"segment": "CityA|L2", "planned_qualification_rate": "0.2"}]},
            {},
            "insufficient_target_cpl",
        ),
        (
            {"targets": [{"segment": "CityA|L2", "target_cpl": "10"}]},
            {},
            "insufficient_qualification_plan",
        ),
        ({}, {"lifetime_spend": None}, "insufficient_spend"),
    ],
)
def test_quality_fail_closed_requirements(config_overrides, candidate_overrides, reason):
    plan = evaluate_kill_policy(
        _quality_config(**config_overrides),
        _candidate(**candidate_overrides),
    )
    assert plan.quality.reason == reason
    assert plan.quality.mutation_allowed is False


def test_quality_formula_uses_decimal_cpq_and_exact_boundary():
    below = evaluate_kill_policy(
        _quality_config(),
        _candidate(lifetime_spend="149.99"),
    )
    exact = evaluate_kill_policy(
        _quality_config(),
        _candidate(lifetime_spend="150"),
    )

    assert below.quality.decision is Decision.WAIT
    assert below.quality.threshold == Decimal("150")
    assert exact.quality.decision is Decision.PAUSE
    assert exact.quality.threshold == Decimal("150")
    assert exact.decision is Decision.WAIT
    assert exact.mutation_allowed is False


def test_quality_nonzero_mature_qualifications_never_pause():
    plan = evaluate_kill_policy(
        _quality_config("active"),
        _candidate(lifetime_spend="999999", qualified_mature_leads=1),
    )
    assert plan.quality.decision is Decision.KEEP
    assert plan.quality.reason == "qualified_mature_leads_present"
    assert plan.quality.mutation_allowed is False


def test_quality_active_requires_explicit_safe_config():
    plan = evaluate_kill_policy(_quality_config("active"), _candidate())
    assert plan.quality.decision is Decision.PAUSE
    assert plan.decision is Decision.PAUSE
    assert plan.mutation_allowed is True


def test_strict_parsers_preserve_decimal_values():
    config = _quality_config()
    candidate = _candidate(lifetime_spend="150.25")
    assert config.targets[0].target_cpl == Decimal("10")
    assert config.targets[0].planned_qualification_rate == Decimal("0.2")
    assert candidate.lifetime_spend == Decimal("150.25")


@pytest.mark.parametrize(
    "raw",
    [
        {"mystery": 1},
        {"zero_lead_policy": "age_or_spend"},
        {"contour_a_rollout": "enabled"},
        {"spend_cpl_multiplier": True},
        {"quality_min_mature_leads": "1.5"},
        {"approved_maturity_profiles": "d3"},
        {"approved_maturity_profiles": ["d3", "d3"]},
        {"targets": "not-a-list"},
        {"targets": [{"segment": "A", "target_cpl": 10, "mystery": 1}]},
        {
            "targets": [
                {"segment": "A", "target_cpl": 10},
                {"segment": "A", "planned_qualification_rate": "0.2"},
            ]
        },
        {"targets": [{"segment": "A", "planned_qualification_rate": "0"}]},
        {"targets": [{"segment": "A", "planned_qualification_rate": "1.01"}]},
        {"targets": [{"segment": "A", "target_cpl": "NaN"}]},
    ],
)
def test_config_parser_rejects_unsafe_values(raw):
    with pytest.raises(KillPolicyConfigError):
        parse_kill_policy_config(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"zero_lead_policy": "spend_cpl", "contour_a_rollout": "active"},
        {
            "zero_lead_policy": "spend_cpl",
            "contour_a_rollout": "active",
            "spend_cpl_multiplier": 3,
        },
        {"quality_rollout": "active"},
        {
            "quality_rollout": "active",
            "quality_min_mature_leads": 5,
            "approved_maturity_profiles": ["d3"],
            "targets": [{"segment": "A", "target_cpl": 10}],
        },
    ],
)
def test_active_config_rejects_missing_safety_prerequisites(raw):
    with pytest.raises(KillPolicyConfigError):
        parse_kill_policy_config(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"segment": "A", "qualification_evidence": "complete"},
        {"segment": "A", "lifetime_evidence": "complete"},
        {
            "segment": "A",
            "lifetime_evidence": "complete",
            "qualification_evidence": "complete",
            "age_days": True,
        },
        {
            "segment": "A",
            "lifetime_evidence": "complete",
            "qualification_evidence": "complete",
            "lifetime_spend": "NaN",
        },
        {
            "segment": "A",
            "lifetime_evidence": "complete",
            "qualification_evidence": "complete",
            "lifetime_leads": -1,
        },
        {
            "segment": "A",
            "lifetime_evidence": "complete",
            "qualification_evidence": "complete",
            "qual_pct": 0,
        },
        {
            "segment": "A",
            "lifetime_evidence": "complete",
            "qualification_evidence": "complete",
            "outcomes_matched_at": "2026-07-22",
        },
        {
            "segment": "A",
            "lifetime_evidence": "complete",
            "qualification_evidence": "complete",
            "maturity_event_at": "not-a-timestamp",
        },
        {
            "segment": "A",
            "lifetime_evidence": "complete",
            "qualification_evidence": "complete",
            "maturity_event_at": "2026-07-22T00:00:00",
        },
        {
            "segment": "A",
            "lifetime_evidence": "complete",
            "qualification_evidence": "complete",
            "mature_leads": 1,
            "qualified_mature_leads": 2,
        },
        {
            "segment": "A",
            "lifetime_evidence": "complete",
            "qualification_evidence": "missing",
        },
    ],
)
def test_candidate_parser_rejects_missing_guessed_or_unsafe_evidence(raw):
    with pytest.raises(KillPolicyConfigError):
        parse_kill_candidate(raw)


def test_policy_module_has_no_io_imports():
    source_path = Path(__file__).parents[1] / "services" / "kill_policy.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden_roots = {
        "aiohttp",
        "httpx",
        "os",
        "pathlib",
        "requests",
        "socket",
        "sqlite3",
        "sqlalchemy",
        "psycopg2",
        "subprocess",
        "urllib",
    }
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imports.isdisjoint(forbidden_roots)
    direct_calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert direct_calls.isdisjoint({"open", "input"})
