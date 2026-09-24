"""Тесты валидатора autopilot.early_kill (web/settings_validation.py)."""

import pytest
from fastapi import HTTPException

from web.settings_validation import validate_settings_update


def _update(body, current=None):
    return validate_settings_update({"autopilot": {"early_kill": body}}, current or {})


def test_shadow_mode_and_numbers_accepted_with_defaults_filled():
    result = _update({"mode": "shadow", "max_per_day": 5, "floor_usd": 10})
    block = result["autopilot"]["early_kill"]
    assert block["mode"] == "shadow" and block["max_per_day"] == 5 and block["floor_usd"] == 10.0
    assert block["cap_usd"] == 80.0 and block["multiplier_no_lead"] == 3.0  # дефолты дозаполнены


def test_every_rule_mode_accepts_active_and_rejects_garbage():
    """Волна 2c: у каждого правила свой режим, все принимают off|shadow|active."""
    block = _update({"mode": "active", "b_mode": "active", "c_mode": "shadow", "s_mode": "off"})["autopilot"]["early_kill"]
    assert (block["mode"], block["b_mode"], block["c_mode"], block["s_mode"]) == ("active", "active", "shadow", "off")
    for key in ("mode", "b_mode", "c_mode", "s_mode"):
        with pytest.raises(HTTPException) as exc:
            _update({key: "boom"})
        assert exc.value.status_code == 400 and key in exc.value.detail


def test_rule_c_and_s_keys_validated():
    block = _update({"c_min_mature_leads": 40, "c_max_qual_pct": 8, "c_cpq_multiplier": 2.5,
                     "c_cpq_fallback_usd": {"act_1": 60}, "s_min_age_days": 5, "s_max_spend_usd": 10,
                     "s_adset_min_active": 20, "s_max_per_day": 40, "daily_hour": 9})["autopilot"]["early_kill"]
    assert block["c_min_mature_leads"] == 40 and block["c_max_qual_pct"] == 8.0 and block["c_cpq_multiplier"] == 2.5
    assert block["c_cpq_fallback_usd"] == {"1": 60.0}
    assert block["s_min_age_days"] == 5 and block["s_max_spend_usd"] == 10.0 and block["daily_hour"] == 9
    for bad in ({"c_min_mature_leads": 4}, {"daily_hour": 24}, {"c_cpq_fallback_usd": {"1": 0}},
                {"c_cpq_fallback_usd": "x"}, {"s_max_per_day": 0}, {"c_cpq_multiplier": 0.5}):
        with pytest.raises(HTTPException):
            _update(bad)


def test_exclusion_markers_validated_and_normalized():
    result = _update({"b_exclude_name_markers": [" Intl ", "тест"]})
    assert result["autopilot"]["early_kill"]["b_exclude_name_markers"] == ["intl", "тест"]
    for bad in ("intl", ["", "x"], [1]):
        with pytest.raises(HTTPException):
            _update({"b_exclude_name_markers": bad})


def test_unknown_field_rejected():
    with pytest.raises(HTTPException) as exc:
        _update({"foo": 1})
    assert exc.value.status_code == 400
    assert exc.value.detail == "Неизвестные поля autopilot.early_kill: ['foo']"


@pytest.mark.parametrize("body", [
    {"max_per_day": 0}, {"max_per_day": "5"}, {"max_per_day": True},
    {"max_age_days": 31}, {"multiplier_no_lead": 0.1}, {"floor_usd": -1},
    {"cpl_min_leads": 0},
])
def test_out_of_bounds_rejected(body):
    with pytest.raises(HTTPException) as exc:
        _update(body)
    assert exc.value.status_code == 400


def test_one_lead_multiplier_below_no_lead_rejected():
    with pytest.raises(HTTPException) as exc:
        _update({"multiplier_no_lead": 5.0, "multiplier_one_lead": 4.0})
    assert "multiplier_one_lead" in exc.value.detail


def test_cap_below_floor_rejected():
    with pytest.raises(HTTPException):
        _update({"floor_usd": 50.0, "cap_usd": 20.0})


def test_account_multipliers_validated_and_normalized():
    result = _update({"account_multipliers": {"act_152882611033373": {"no_lead": 4.0, "one_lead": 6.0}}})
    assert result["autopilot"]["early_kill"]["account_multipliers"] == {
        "152882611033373": {"no_lead": 4.0, "one_lead": 6.0}
    }
    with pytest.raises(HTTPException):
        _update({"account_multipliers": {"111": {"weird": 1}}})
    with pytest.raises(HTTPException):
        _update({"account_multipliers": "no"})


def test_rule_b_keys_accepted_with_bounds_and_json_bools():
    result = _update({"b_enabled": False, "op_guard_enabled": True, "b_min_mature_leads": 4,
                      "b_min_spend_usd": 30, "b_cpl_multiplier": 2.5, "b_small_max_leads": 1,
                      "b_small_cpl_multiplier": 4})
    block = result["autopilot"]["early_kill"]
    assert block["b_enabled"] is False and block["op_guard_enabled"] is True
    assert block["b_min_mature_leads"] == 4 and block["b_min_spend_usd"] == 30.0
    assert block["b_cpl_multiplier"] == 2.5 and block["b_small_max_leads"] == 1
    assert block["b_small_cpl_multiplier"] == 4.0
    for bad in ({"b_enabled": "false"}, {"b_min_mature_leads": 0}, {"b_small_max_leads": 11},
                {"b_cpl_multiplier": 0.1}, {"b_min_spend_usd": -5}):
        with pytest.raises(HTTPException):
            _update(bad)


def test_b_maturity_hours_bounds():
    assert _update({"b_maturity_hours": 48})["autopilot"]["early_kill"]["b_maturity_hours"] == 48
    for bad in ({"b_maturity_hours": 12}, {"b_maturity_hours": 200}, {"b_maturity_hours": "48"}):
        with pytest.raises(HTTPException):
            _update(bad)


def test_ladder_keys_validated():
    block = _update({"b_ladder": [[10, 1], [8, 0]], "b_ladder_accounts": {"act_1": [[8, 0], [12, 1]]},
                     "b_tail_min_leads": 25, "b_tail_norm_share": 0.75, "b_qual_norm_fallback_pct": 16})["autopilot"]["early_kill"]
    assert block["b_ladder"] == [[8, 0], [10, 1]] and block["b_ladder_accounts"] == {"1": [[8, 0], [12, 1]]}
    assert block["b_tail_norm_share"] == 0.75 and block["b_qual_norm_fallback_pct"] == 16.0
    assert _update({"b_ladder": None, "b_tail_norm_share": None})["autopilot"]["early_kill"]["b_ladder"] is None
    for bad in ({"b_ladder": []}, {"b_ladder": [[8, 8]]}, {"b_ladder": [[8, -1]]}, {"b_ladder": [[8]]},
                {"b_ladder": [[True, 0]]}, {"b_ladder": "x"}, {"b_ladder_accounts": {"1": [[0, 0]]}},
                {"b_ladder_accounts": []}, {"b_tail_norm_share": 5}, {"b_tail_min_leads": 3}):
        with pytest.raises(HTTPException):
            _update(bad)


def test_price_norm_keys_validated():
    block = _update({"c_price_mode": "account_norm", "c_cpq_norm_mult": 1.2, "c_price_min_spend_mult": 2,
                     "c_price_min_mature": 5, "c_cpq_norm_fallback_usd": 100})["autopilot"]["early_kill"]
    assert block["c_price_mode"] == "account_norm" and block["c_cpq_norm_mult"] == 1.2 and block["c_price_min_mature"] == 5
    for bad in ({"c_price_mode": "x"}, {"c_cpq_norm_mult": 0.1}, {"c_price_min_mature": 0}, {"c_cpq_norm_fallback_usd": 1}):
        with pytest.raises(HTTPException):
            _update(bad)


def test_merge_keeps_existing_values():
    current = {"autopilot": {"early_kill": {"mode": "off", "max_per_day": 3, "legacy": 1}}}
    result = _update({"floor_usd": 12}, current)
    block = result["autopilot"]["early_kill"]
    assert block["mode"] == "off" and block["max_per_day"] == 3 and block["floor_usd"] == 12.0
    assert "legacy" not in block
