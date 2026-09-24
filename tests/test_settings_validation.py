"""
Юнит-тесты чистого валидатора web/settings_validation.py::validate_settings_update.
Дополняют (НЕ заменяют) tests/test_settings_autopilot.py — там интеграционные
тесты через HTTP-ручку POST /api/settings, здесь — прямые вызовы чистой функции
без FastAPI TestClient/БД (см. docs/specs/ARCH-phase6-engineering.md, T-SETTINGS-1).
"""

import pytest
from fastapi import HTTPException

from web.settings_validation import validate_settings_update


def test_empty_body_returns_current_unchanged():
    """Пустое тело {} — валидатор возвращает current без изменений."""
    current = {"auto_apply": True, "autopilot": {"enabled": False}}
    result = validate_settings_update({}, current)
    assert result == current


def test_auto_apply_toggle():
    """auto_apply приводится к bool и записывается поверх current."""
    result = validate_settings_update({"auto_apply": 1}, {"auto_apply": False})
    assert result["auto_apply"] is True


def test_autopilot_happy_path_merges_over_existing():
    """autopilot.enabled=True мержится поверх текущего autopilot-блока,
    непереданные ключи (mode) сохраняются."""
    current = {"autopilot": {"mode": "dry_run"}}
    result = validate_settings_update({"autopilot": {"enabled": True}}, current)
    assert result["autopilot"]["enabled"] is True
    assert result["autopilot"]["mode"] == "dry_run"


def test_autopilot_not_dict_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": "not-a-dict"}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "autopilot должен быть объектом"


def test_autopilot_unknown_field_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"foo": 1}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Неизвестные поля autopilot: ['foo']"


def test_autopilot_mode_invalid_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"mode": "turbo"}}, {})
    assert exc_info.value.status_code == 400
    assert "mode должен быть одним из" in exc_info.value.detail


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_launch_checker_accepts_exact_modes(mode):
    result = validate_settings_update(
        {"autopilot": {"launch_checker": {"mode": mode}}},
        {},
    )

    assert result["autopilot"]["launch_checker"] == {"mode": mode}


@pytest.mark.parametrize("value", [None, "observe", [], True])
def test_launch_checker_requires_object(value):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update(
            {"autopilot": {"launch_checker": value}},
            {},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "autopilot.launch_checker должен быть объектом"


@pytest.mark.parametrize("mode", ["active", "dry_run", "OBSERVE", "", 1, None])
def test_launch_checker_rejects_unknown_mode(mode):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update(
            {"autopilot": {"launch_checker": {"mode": mode}}},
            {},
        )

    assert exc_info.value.status_code == 400
    assert "'observe' или 'enforce'" in exc_info.value.detail


def test_launch_checker_rejects_unknown_nested_key():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update(
            {"autopilot": {"launch_checker": {"enabled": True}}},
            {},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == (
        "Неизвестные поля autopilot.launch_checker: ['enabled']"
    )


def test_launch_checker_partial_update_deep_merges_existing_mode():
    current = {"autopilot": {"launch_checker": {"mode": "enforce"}}}

    result = validate_settings_update(
        {"autopilot": {"launch_checker": {}}},
        current,
    )

    assert result["autopilot"]["launch_checker"] == {"mode": "enforce"}


@pytest.mark.parametrize(
    "field,value,expected_detail",
    [
        ("max_pauses_per_run", 99, "max_pauses_per_run должен быть от 1 до 10"),
        ("max_pauses_per_day", 0, "max_pauses_per_day должен быть от 1 до 50"),
        ("min_hours_between_runs", 25, "min_hours_between_runs должен быть от 1 до 24"),
        ("min_days_protect", 31, "min_days_protect должен быть от 0 до 30"),
        ("max_launches_per_day", 11, "max_launches_per_day должен быть от 1 до 10"),
        ("max_scales_per_run", 0, "max_scales_per_run должен быть от 1 до 10"),
        ("max_adset_daily_budget", 2001, "max_adset_daily_budget должен быть от 1 до 2000"),
        ("max_total_daily_budget", 50001, "max_total_daily_budget должен быть от 1 до 50000"),
    ],
)
def test_autopilot_int_field_out_of_range_raises_400(field, value, expected_detail):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {field: value}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == expected_detail


def test_autopilot_int_field_wrong_type_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"max_pauses_per_run": "abc"}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "max_pauses_per_run должен быть int"


def test_max_adset_budget_mult_out_of_range_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"max_adset_budget_mult": 10.0}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "max_adset_budget_mult должен быть от 1.0 до 5.0"


def test_max_budget_increase_pct_out_of_range_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"max_budget_increase_pct": 101}}, {})
    assert exc_info.value.status_code == 400
    assert "1..100" in exc_info.value.detail


def test_cleaner_block_merges_over_existing():
    """cleaner мержится поверх существующего autopilot.cleaner — непереданные
    под-ключи не теряются."""
    current = {"autopilot": {"cleaner": {"enabled": True, "stale_days": 30}}}
    result = validate_settings_update(
        {"autopilot": {"cleaner": {"stale_days": 60}}}, current
    )
    assert result["autopilot"]["cleaner"]["enabled"] is True
    assert result["autopilot"]["cleaner"]["stale_days"] == 60


def test_cleaner_unknown_field_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"cleaner": {"foo": 1}}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Неизвестные поля autopilot.cleaner: ['foo']"


@pytest.mark.parametrize("stale_days", [14, 400])
def test_cleaner_stale_days_out_of_range_raises_400(stale_days):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update(
            {"autopilot": {"cleaner": {"stale_days": stale_days}}}, {}
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "stale_days должен быть от 15 до 365"


@pytest.mark.parametrize(
    "field",
    ["enabled", "dry_run", "proactive_enabled", "allow_irreversible_delete"],
)
@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
def test_cleaner_safety_bool_requires_literal_json_boolean(field, value):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"cleaner": {field: value}}}, {})

    assert exc_info.value.status_code == 400
    assert "JSON boolean" in exc_info.value.detail


def test_cleaner_new_fields_deep_merge_without_enabling_flags():
    current = {
        "autopilot": {
            "cleaner": {
                "enabled": False,
                "dry_run": True,
                "allow_irreversible_delete": False,
                "stale_days": 30,
            }
        }
    }
    result = validate_settings_update(
        {
            "autopilot": {
                "cleaner": {
                    "adset_threshold": 45,
                    "target_free": 5,
                    "critical_free_slots": 2,
                    "hard_reserve_slots": 1,
                    "max_manifest_candidates_per_adset": 10,
                    "max_deletes_per_workflow": 2,
                    "alert_dedup_hours": 6,
                    "managed_account_kinds": ["offline"],
                }
            }
        },
        current,
    )

    cleaner = result["autopilot"]["cleaner"]
    assert cleaner["enabled"] is False
    assert cleaner["dry_run"] is True
    assert cleaner["allow_irreversible_delete"] is False
    assert cleaner["max_deletes_per_workflow"] == 2
    assert "max_deletes_per_run" not in cleaner


def test_cleaner_legacy_max_deletes_per_run_is_rejected():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update(
            {"autopilot": {"cleaner": {"max_deletes_per_run": 1}}},
            {},
        )

    assert exc_info.value.status_code == 400
    assert "max_deletes_per_run" in exc_info.value.detail


@pytest.mark.parametrize(
    "update",
    [
        {"target_free": 4},
        {"adset_threshold": 44},
        {"target_free": 1, "adset_threshold": 49, "critical_free_slots": 2},
    ],
)
def test_cleaner_threshold_target_and_critical_invariants(update):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"cleaner": update}}, {})

    assert exc_info.value.status_code == 400


@pytest.mark.parametrize(
    "field,value",
    [
        ("managed_account_kinds", "offline"),
        ("managed_account_kinds", []),
        ("managed_account_kinds", ["offline", "offline"]),
        ("managed_account_kinds", ["offline", "other"]),
    ],
)
def test_cleaner_account_allowlist_is_exact(field, value):
    with pytest.raises(HTTPException):
        validate_settings_update({"autopilot": {"cleaner": {field: value}}}, {})


def test_replacement_partial_update_deep_merges_and_stays_disabled():
    current = {
        "autopilot": {
            "replacement": {
                "enabled": False,
                "verify_interval_minutes": 30,
                "max_pending_hours": 48,
            }
        }
    }
    result = validate_settings_update(
        {"autopilot": {"replacement": {"max_pending_hours": 72}}},
        current,
    )

    assert result["autopilot"]["replacement"] == {
        "enabled": False,
        "verify_interval_minutes": 30,
        "max_pending_hours": 72,
    }


@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
def test_replacement_enabled_requires_literal_json_boolean(value):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update(
            {"autopilot": {"replacement": {"enabled": value}}}, {}
        )

    assert exc_info.value.status_code == 400
    assert "JSON boolean" in exc_info.value.detail


def test_replacement_unknown_key_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update(
            {"autopilot": {"replacement": {"unknown": True}}}, {}
        )

    assert exc_info.value.status_code == 400
    assert "Неизвестные поля autopilot.replacement" in exc_info.value.detail


@pytest.mark.parametrize(
    "block,field,value",
    [
        ("cleaner", "stale_days", True),
        ("cleaner", "max_deletes_per_workflow", 6),
        ("cleaner", "hard_reserve_slots", 0),
        ("cleaner", "alert_dedup_hours", float("nan")),
        ("replacement", "verify_interval_minutes", "30"),
        ("replacement", "max_pending_hours", 169),
    ],
)
def test_new_nested_integer_contract_is_strict(block, field, value):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update(
            {"autopilot": {block: {field: value}}},
            {},
        )

    assert exc_info.value.status_code == 400


def test_recovery_partial_update_deep_merges_safe_defaults():
    result = validate_settings_update(
        {"autopilot": {"recovery": {"max_cards_per_day": 2}}},
        {},
    )

    assert result["autopilot"]["recovery"] == {
        "enabled": False,
        "since": "2026-07-01T00:00:00+05:00",
        "max_cards_per_day": 2,
        "managed_account_kinds": ["offline", "online"],
    }


@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
def test_recovery_enabled_requires_literal_json_boolean(value):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update(
            {"autopilot": {"recovery": {"enabled": value}}},
            {},
        )

    assert exc_info.value.status_code == 400


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_cards_per_day", True),
        ("max_cards_per_day", 0),
        ("max_cards_per_day", 11),
        ("max_cards_per_day", float("nan")),
        ("managed_account_kinds", "offline"),
        ("managed_account_kinds", []),
        ("managed_account_kinds", ["online", "online"]),
        ("managed_account_kinds", ["unknown"]),
        ("since", "2026-07-01T00:00:00"),
        ("since", "2026-06-30T23:59:59+05:00"),
        ("since", "not-a-date"),
    ],
)
def test_recovery_rejects_invalid_types_ranges_and_cutoff(field, value):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update(
            {"autopilot": {"recovery": {field: value}}},
            {},
        )

    assert exc_info.value.status_code == 400


def test_recovery_unknown_key_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update(
            {"autopilot": {"recovery": {"apply": True}}},
            {},
        )

    assert exc_info.value.status_code == 400
    assert "Неизвестные поля autopilot.recovery" in exc_info.value.detail


@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
def test_kill_switch_requires_literal_json_boolean(value):
    with pytest.raises(HTTPException):
        validate_settings_update({"autopilot": {"kill_switch": value}}, {})


def test_guardian_block_merges_and_validates_floats():
    current = {"autopilot": {"guardian": {"early_min_spend": 5.0}}}
    result = validate_settings_update(
        {"autopilot": {"guardian": {"wnc_min_leads": 10}}}, current
    )
    assert result["autopilot"]["guardian"]["early_min_spend"] == 5.0
    assert result["autopilot"]["guardian"]["wnc_min_leads"] == 10


def test_guardian_negative_float_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"guardian": {"early_min_spend": -1}}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "early_min_spend должен быть >= 0"


def test_guardian_unknown_field_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"guardian": {"bar": 1}}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Неизвестные поля guardian: ['bar']"


def test_hypothesist_block_merges_and_validates():
    current = {"autopilot": {"hypothesist": {"ttl_days": 90}}}
    result = validate_settings_update(
        {"autopilot": {"hypothesist": {"min_age_days": 3}}}, current
    )
    assert result["autopilot"]["hypothesist"]["ttl_days"] == 90
    assert result["autopilot"]["hypothesist"]["min_age_days"] == 3


def test_hypothesist_unknown_field_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"hypothesist": {"baz": 1}}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Неизвестные поля autopilot.hypothesist: ['baz']"


def test_brief_generator_happy_path():
    result = validate_settings_update({"brief_generator": {"enabled": True}}, {})
    assert result["brief_generator"]["enabled"] is True


def test_brief_generator_not_dict_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"brief_generator": "nope"}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "brief_generator должен быть объектом"


def test_brief_generator_unknown_field_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"brief_generator": {"foo": 1}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Неизвестные поля brief_generator: ['foo']"


def test_plan_sheet_id_stripped_as_string():
    result = validate_settings_update(
        {"autopilot": {"plan_sheet_id": "  abc123  "}}, {}
    )
    assert result["autopilot"]["plan_sheet_id"] == "abc123"


def test_cdp_block_valid():
    """autopilot.cdp.enabled=true валидируется и сохраняется как bool."""
    result = validate_settings_update({"autopilot": {"cdp": {"enabled": True}}}, {})
    assert result["autopilot"]["cdp"]["enabled"] is True


def test_cdp_block_not_dict_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"cdp": "x"}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "autopilot.cdp должен быть объектом"


def test_cdp_unknown_key_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"cdp": {"foo": 1}}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Неизвестные поля autopilot.cdp: ['foo']"


def test_cdp_merges_over_existing():
    """cdp мержится поверх существующего autopilot.cdp — непереданные
    под-ключи не теряются."""
    current = {"autopilot": {"cdp": {"enabled": True}}}
    result = validate_settings_update({"autopilot": {"cdp": {}}}, current)
    assert result["autopilot"]["cdp"]["enabled"] is True


def test_cdp_doubt_alerts_valid():
    """autopilot.cdp.doubt_alerts=false валидируется и сохраняется как bool
    (протокол сомнений, ARCH-cdp-seasonal-pacing §4/T3)."""
    result = validate_settings_update({"autopilot": {"cdp": {"doubt_alerts": False}}}, {})
    assert result["autopilot"]["cdp"]["doubt_alerts"] is False


def test_cdp_doubt_alerts_merges_over_existing():
    """doubt_alerts мержится поверх существующего cdp-блока — enabled
    из current не теряется, доступны оба ключа одновременно."""
    current = {"autopilot": {"cdp": {"enabled": True, "doubt_alerts": True}}}
    result = validate_settings_update({"autopilot": {"cdp": {"doubt_alerts": False}}}, current)
    assert result["autopilot"]["cdp"]["enabled"] is True


def test_cdp_pace_engine_valid():
    """autopilot.cdp.pace_engine=false валидируется и сохраняется как bool
    (движок budget-context как источник темпа план-гейта, Шаг A.3,
    ARCH-cdp-budget-context §12/T2)."""
    result = validate_settings_update({"autopilot": {"cdp": {"pace_engine": False}}}, {})
    assert result["autopilot"]["cdp"]["pace_engine"] is False


def test_cdp_pace_engine_merges_over_existing():
    """pace_engine мержится поверх существующего cdp-блока — enabled и
    doubt_alerts из current не теряются, все три ключа доступны одновременно."""
    current = {"autopilot": {"cdp": {"enabled": True, "doubt_alerts": True}}}
    result = validate_settings_update({"autopilot": {"cdp": {"pace_engine": False}}}, current)
    assert result["autopilot"]["cdp"]["enabled"] is True
    assert result["autopilot"]["cdp"]["doubt_alerts"] is True
    assert result["autopilot"]["cdp"]["pace_engine"] is False


def test_cdp_unknown_key_400_with_pace_engine_allowed():
    """Неизвестный ключ по-прежнему 400, даже когда pace_engine уже разрешён
    (регрессия: расширение _ALLOWED_CDP_KEYS не открывает валидацию всем полям)."""
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"cdp": {"pace_engine": True, "bar": 1}}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Неизвестные поля autopilot.cdp: ['bar']"


# --- Удержание рангового правила (hold, ARCH-rank-pause-hold §6/§9) ---


def test_hold_enabled_valid():
    """autopilot.hold_enabled приводится к bool."""
    result = validate_settings_update({"autopilot": {"hold_enabled": True}}, {})
    assert result["autopilot"]["hold_enabled"] is True


def test_hold_romi_ratio_valid_saved():
    """hold_romi_ratio=0.8 — валидный float, сохраняется как есть (сценарий §9)."""
    result = validate_settings_update({"autopilot": {"hold_romi_ratio": 0.8}}, {})
    assert result["autopilot"]["hold_romi_ratio"] == 0.8


@pytest.mark.parametrize(
    "field,value,expected_detail",
    [
        ("hold_romi_target", 501, "hold_romi_target должен быть от 50 до 500"),
        ("hold_romi_target", 49, "hold_romi_target должен быть от 50 до 500"),
        ("hold_romi_ratio", 1.6, "hold_romi_ratio должен быть от 0.1 до 1.5"),
        ("hold_romi_ratio", 0.05, "hold_romi_ratio должен быть от 0.1 до 1.5"),
        ("hold_spend_max", 5001, "hold_spend_max должен быть от 1 до 5000"),
        ("hold_spend_max", 0, "hold_spend_max должен быть от 1 до 5000"),
        ("hold_min_qual_pct", 101, "hold_min_qual_pct должен быть от 0 до 100"),
        ("hold_min_qual_pct", -1, "hold_min_qual_pct должен быть от 0 до 100"),
        ("hold_days", 50, "hold_days должен быть от 1 до 30"),
        ("hold_days", 0, "hold_days должен быть от 1 до 30"),
    ],
)
def test_hold_field_out_of_range_raises_400(field, value, expected_detail):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {field: value}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == expected_detail


def test_hold_days_wrong_type_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"hold_days": "abc"}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "hold_days должен быть int"


def test_hold_romi_ratio_wrong_type_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"hold_romi_ratio": "abc"}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "hold_romi_ratio должен быть числом"


def test_hold_fields_merge_over_existing():
    """hold_* мержится поверх текущего autopilot-блока, непереданные ключи сохраняются."""
    current = {"autopilot": {"hold_enabled": False, "hold_days": 7}}
    result = validate_settings_update({"autopilot": {"hold_romi_target": 250}}, current)
    assert result["autopilot"]["hold_enabled"] is False
    assert result["autopilot"]["hold_days"] == 7
    assert result["autopilot"]["hold_romi_target"] == 250


# ---------------------------------------------------------------------------
# autopilot.cdp.payments_source (ARCH-cdp-payments, Шаг B) — источник факта
# оплаты по объявлению: amo|erp|shadow.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payments_source", ["amo", "erp", "shadow"])
def test_cdp_payments_source_valid(payments_source):
    """Все три допустимых значения payments_source сохраняются как есть."""
    result = validate_settings_update(
        {"autopilot": {"cdp": {"payments_source": payments_source}}}, {}
    )
    assert result["autopilot"]["cdp"]["payments_source"] == payments_source


def test_cdp_payments_source_invalid_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"cdp": {"payments_source": "xxx"}}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "autopilot.cdp.payments_source ∈ {amo,erp,shadow}"


def test_cdp_payments_source_default_shadow():
    """Дефолт payments_source="shadow" зафиксирован в AUTOPILOT_DEFAULTS (когда
    ключ не передан в settings.json — конфиг берётся из дефолтов, не из валидатора)."""
    from services.autopilot import AUTOPILOT_DEFAULTS

    assert AUTOPILOT_DEFAULTS["cdp"]["payments_source"] == "shadow"


def test_cdp_payments_source_merges_over_existing():
    """payments_source мержится поверх существующего cdp-блока — enabled сохраняется."""
    current = {"autopilot": {"cdp": {"enabled": True}}}
    result = validate_settings_update(
        {"autopilot": {"cdp": {"payments_source": "erp"}}}, current
    )
    assert result["autopilot"]["cdp"]["enabled"] is True
    assert result["autopilot"]["cdp"]["payments_source"] == "erp"


# ---------------------------------------------------------------------------
# Страж трат адсетов (spend_guard) и онлайн-отчёт (online_report)
# ---------------------------------------------------------------------------

def test_spend_guard_happy_merges_over_existing():
    """spend_guard: overspend_mult мержится поверх, existing enabled сохраняется."""
    current = {"autopilot": {"spend_guard": {"enabled": True}}}
    result = validate_settings_update(
        {"autopilot": {"spend_guard": {"overspend_mult": 1.4}}}, current
    )
    assert result["autopilot"]["spend_guard"]["enabled"] is True
    assert result["autopilot"]["spend_guard"]["overspend_mult"] == 1.4


def test_spend_guard_not_dict_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"spend_guard": "x"}}, {})
    assert exc_info.value.detail == "autopilot.spend_guard должен быть объектом"


def test_spend_guard_unknown_field_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"spend_guard": {"foo": 1}}}, {})
    assert exc_info.value.detail == "Неизвестные поля autopilot.spend_guard: ['foo']"


@pytest.mark.parametrize("bad", [0.5, 5.5])
def test_spend_guard_overspend_mult_out_of_range_400(bad):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"spend_guard": {"overspend_mult": bad}}}, {})
    assert exc_info.value.detail == "overspend_mult должен быть от 1.0 до 5.0"


def test_spend_guard_overspend_mult_not_number_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"spend_guard": {"overspend_mult": "x"}}}, {})
    assert exc_info.value.detail == "overspend_mult должен быть числом"


def test_spend_guard_defaults():
    from services.autopilot import AUTOPILOT_DEFAULTS

    assert AUTOPILOT_DEFAULTS["spend_guard"] == {"enabled": True, "overspend_mult": 1.25}


def test_online_report_happy_merges():
    current = {"autopilot": {"online_report": {"enabled": True}}}
    result = validate_settings_update(
        {"autopilot": {"online_report": {"enabled": False}}}, current
    )
    assert result["autopilot"]["online_report"]["enabled"] is False


def test_online_report_unknown_field_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"online_report": {"foo": 1}}}, {})
    assert exc_info.value.detail == "Неизвестные поля autopilot.online_report: ['foo']"


def test_online_report_not_dict_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"online_report": 5}}, {})
    assert exc_info.value.detail == "autopilot.online_report должен быть объектом"


def test_online_report_defaults():
    from services.autopilot import AUTOPILOT_DEFAULTS

    assert AUTOPILOT_DEFAULTS["online_report"] == {"enabled": True}


# ---------------------------------------------------------------------------
# Budget Scaler v2 (autopilot.scaler_v2) — официальный settings contract
# (по итогам ревью). Ключи фактически читаются в services/budget_scaler.py:
# waster_min_spend_usd, require_fresh_7d, engine_selfcheck_enabled,
# sanity_cap_enabled, sanity_cap_ratio, selfcheck_max_dev.
# ---------------------------------------------------------------------------


def test_scaler_v2_defaults_contract():
    """Дефолты scaler_v2 в AUTOPILOT_DEFAULTS — единственный источник правды,
    зеркалят fail-safe .get()-дефолты budget_scaler.py. Safety-гейты ВКЛючены,
    require_fresh_7d выключен (active-мутации не включаются автоматически)."""
    from services.autopilot import AUTOPILOT_DEFAULTS

    assert AUTOPILOT_DEFAULTS["scaler_v2"] == {
        "waster_min_spend_usd": 10.0,
        "require_fresh_7d": False,
        "engine_selfcheck_enabled": True,
        "sanity_cap_enabled": True,
        "sanity_cap_ratio": 0.7,
        "selfcheck_max_dev": 0.25,
    }


def test_scaler_v2_defaults_match_code_reads():
    """Дефолты в AUTOPILOT_DEFAULTS совпадают с fail-safe .get()-дефолтами,
    которые budget_scaler.py использует при отсутствии блока (единый контракт)."""
    from services.autopilot import AUTOPILOT_DEFAULTS
    from services.budget_scaler import _waster_min_spend_usd, _require_fresh_7d

    d = AUTOPILOT_DEFAULTS["scaler_v2"]
    # порог значимости и 7d-гейт читаются через хелперы — их дефолт при пустом cfg
    assert _waster_min_spend_usd(None) == d["waster_min_spend_usd"]
    assert _require_fresh_7d(None) is d["require_fresh_7d"]


def test_scaler_v2_happy_path_all_keys():
    """Все шесть ключей валидируются и сохраняются с правильными типами."""
    result = validate_settings_update(
        {
            "autopilot": {
                "scaler_v2": {
                    "waster_min_spend_usd": 25.0,
                    "require_fresh_7d": True,
                    "engine_selfcheck_enabled": False,
                    "sanity_cap_enabled": False,
                    "sanity_cap_ratio": 0.5,
                    "selfcheck_max_dev": 0.3,
                }
            }
        },
        {},
    )
    v2 = result["autopilot"]["scaler_v2"]
    assert v2["waster_min_spend_usd"] == 25.0
    assert v2["require_fresh_7d"] is True
    assert v2["engine_selfcheck_enabled"] is False
    assert v2["sanity_cap_enabled"] is False
    assert v2["sanity_cap_ratio"] == 0.5
    assert v2["selfcheck_max_dev"] == 0.3


def test_scaler_v2_not_dict_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"scaler_v2": "x"}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "autopilot.scaler_v2 должен быть объектом"


def test_scaler_v2_unknown_field_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"scaler_v2": {"foo": 1}}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Неизвестные поля autopilot.scaler_v2: ['foo']"


def test_scaler_v2_partial_deep_merge_preserves_siblings():
    """Частичный апдейт scaler_v2 мержится поверх существующего блока —
    непереданные под-ключи не теряются (deep merge)."""
    current = {
        "autopilot": {
            "scaler_v2": {
                "waster_min_spend_usd": 15.0,
                "engine_selfcheck_enabled": True,
                "sanity_cap_ratio": 0.7,
            }
        }
    }
    result = validate_settings_update(
        {"autopilot": {"scaler_v2": {"sanity_cap_ratio": 0.9}}}, current
    )
    v2 = result["autopilot"]["scaler_v2"]
    assert v2["sanity_cap_ratio"] == 0.9          # обновлён
    assert v2["waster_min_spend_usd"] == 15.0     # сохранён
    assert v2["engine_selfcheck_enabled"] is True  # сохранён


def test_scaler_v2_update_preserves_sibling_blocks():
    """Апдейт scaler_v2 не стирает соседние блоки autopilot (cdp/cleaner/guardian
    и др.) — top-level deep merge."""
    current = {
        "autopilot": {
            "cdp": {"enabled": True, "payments_source": "shadow"},
            "cleaner": {"enabled": True, "stale_days": 30},
            "guardian": {"enabled": False, "early_min_spend": 15.0},
            "spend_guard": {"enabled": True, "overspend_mult": 1.25},
            "scaler_v2": {"waster_min_spend_usd": 10.0},
        }
    }
    result = validate_settings_update(
        {"autopilot": {"scaler_v2": {"require_fresh_7d": True}}}, current
    )
    ap = result["autopilot"]
    # scaler_v2 обновлён и смержен
    assert ap["scaler_v2"]["require_fresh_7d"] is True
    assert ap["scaler_v2"]["waster_min_spend_usd"] == 10.0
    # соседи не тронуты
    assert ap["cdp"] == {"enabled": True, "payments_source": "shadow"}
    assert ap["cleaner"] == {"enabled": True, "stale_days": 30}
    assert ap["guardian"] == {"enabled": False, "early_min_spend": 15.0}
    assert ap["spend_guard"] == {"enabled": True, "overspend_mult": 1.25}


@pytest.mark.parametrize(
    "field,value,expected_detail",
    [
        ("waster_min_spend_usd", 1000.01, "waster_min_spend_usd должен быть от 0 до 1000"),
        ("waster_min_spend_usd", -0.01, "waster_min_spend_usd должен быть от 0 до 1000"),
        ("sanity_cap_ratio", 1.01, "sanity_cap_ratio должен быть от 0.1 до 1.0"),
        ("sanity_cap_ratio", 0.09, "sanity_cap_ratio должен быть от 0.1 до 1.0"),
        ("selfcheck_max_dev", 0.91, "selfcheck_max_dev должен быть от 0.05 до 0.9"),
        ("selfcheck_max_dev", 0.04, "selfcheck_max_dev должен быть от 0.05 до 0.9"),
    ],
)
def test_scaler_v2_float_out_of_range_raises_400(field, value, expected_detail):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"scaler_v2": {field: value}}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == expected_detail


@pytest.mark.parametrize("field", ["waster_min_spend_usd", "sanity_cap_ratio", "selfcheck_max_dev"])
def test_scaler_v2_float_boundaries_accepted(field):
    """Граничные значения (включительно) принимаются."""
    bounds = {
        "waster_min_spend_usd": (0, 1000),
        "sanity_cap_ratio": (0.1, 1.0),
        "selfcheck_max_dev": (0.05, 0.9),
    }
    lo, hi = bounds[field]
    for val in (lo, hi):
        result = validate_settings_update(
            {"autopilot": {"scaler_v2": {field: val}}}, {}
        )
        assert result["autopilot"]["scaler_v2"][field] == float(val)


@pytest.mark.parametrize("field", ["waster_min_spend_usd", "sanity_cap_ratio", "selfcheck_max_dev"])
def test_scaler_v2_float_wrong_type_raises_400(field):
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"scaler_v2": {field: "abc"}}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == f"{field} должен быть числом"


@pytest.mark.parametrize("field", ["waster_min_spend_usd", "sanity_cap_ratio", "selfcheck_max_dev"])
def test_scaler_v2_float_bool_rejected(field):
    """Булево НЕ принимается как число (True не должен молча стать 1.0)."""
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"scaler_v2": {field: True}}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == f"{field} должен быть числом"


@pytest.mark.parametrize("field", ["waster_min_spend_usd", "sanity_cap_ratio", "selfcheck_max_dev"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_scaler_v2_float_nan_inf_rejected(field, bad):
    """NaN/Infinity/-Infinity отклоняются как неконечные числа."""
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"scaler_v2": {field: bad}}}, {})
    assert exc_info.value.status_code == 400
    assert "конечным числом" in exc_info.value.detail


@pytest.mark.parametrize("field", ["require_fresh_7d", "engine_selfcheck_enabled", "sanity_cap_enabled"])
def test_scaler_v2_bool_string_false_is_false(field):
    """Строка "false" → False (НЕ наивный bool("false")==True)."""
    result = validate_settings_update(
        {"autopilot": {"scaler_v2": {field: "false"}}}, {}
    )
    assert result["autopilot"]["scaler_v2"][field] is False


@pytest.mark.parametrize("field", ["require_fresh_7d", "engine_selfcheck_enabled", "sanity_cap_enabled"])
def test_scaler_v2_bool_string_true_is_true(field):
    result = validate_settings_update(
        {"autopilot": {"scaler_v2": {field: "true"}}}, {}
    )
    assert result["autopilot"]["scaler_v2"][field] is True


@pytest.mark.parametrize("field", ["require_fresh_7d", "engine_selfcheck_enabled", "sanity_cap_enabled"])
def test_scaler_v2_bool_garbage_raises_400(field):
    """Неоднозначное значение булева → 400 (не молчаливая коэрция)."""
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update({"autopilot": {"scaler_v2": {field: "maybe"}}}, {})
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == f"autopilot.scaler_v2.{field} должен быть булевым (true/false)"


# ---------------------------------------------------------------------------
# Money-safety: никакой config-флаг НЕ отключает строгое confirmed_waster-вето.
# ---------------------------------------------------------------------------


def test_scaler_v2_no_veto_disable_flag_accepted():
    """Правдоподобные «обходные» ключи (выключить вето) → 400 как неизвестные.
    В allowlist нет и не может быть флага отключения вето."""
    for bypass_key in ("strict_waster_veto", "disable_waster_veto", "waster_veto_enabled", "veto"):
        with pytest.raises(HTTPException) as exc_info:
            validate_settings_update(
                {"autopilot": {"scaler_v2": {bypass_key: False}}}, {}
            )
        assert exc_info.value.status_code == 400
        assert "Неизвестные поля autopilot.scaler_v2" in exc_info.value.detail


def test_confirmed_waster_veto_reads_only_threshold_not_toggle():
    """_adset_waster_veto читает из cfg_v2 ТОЛЬКО порог значимости (waster_min_spend_usd),
    а не флаг вкл/выкл. Даже при МАКСИМАЛЬНО разрешённом пороге ($1000) реальный
    слив ($1500, оплат 0, сверено) блокирует весь адсет — вето не обходится настройками."""
    from services.budget_scaler import _adset_waster_veto

    # confirmed_waster: сверено (outcomes_matched_at не None) И оплат 0, расход выше порога
    waster = {"outcomes_matched_at": "2026-07-10T00:00:00", "payments": 0, "spend": 1500.0}
    winner = {"outcomes_matched_at": "2026-07-10T00:00:00", "payments": 5, "spend": 200.0}

    # cfg с максимально допустимым порогом — вето всё равно срабатывает
    veto, reason = _adset_waster_veto([winner, waster], use_erp_payments=False, cfg_v2={"waster_min_spend_usd": 1000})
    assert veto is True
    assert reason is not None

    # cfg вообще без ключей (пустой блок) — дефолтный порог, вето срабатывает
    veto2, _ = _adset_waster_veto([winner, waster], use_erp_payments=False, cfg_v2={})
    assert veto2 is True


def test_waster_min_spend_usd_capped_prevents_veto_bypass():
    """Порог значимости слива ограничен сверху ($1000): нельзя задать
    гигантский порог, который сделал бы любой слив «незначимым» и обошёл вето."""
    with pytest.raises(HTTPException) as exc_info:
        validate_settings_update(
            {"autopilot": {"scaler_v2": {"waster_min_spend_usd": 1_000_000}}}, {}
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "waster_min_spend_usd должен быть от 0 до 1000"


def test_waster_min_spend_usd_runtime_reader_clamps_over_cap():
    """Прямая правка settings.json обходит settings-API (0..1000). Runtime-ридер
    _waster_min_spend_usd обязан сам клампить сверху к $1000 — иначе гигантский
    порог из файла сделал бы любой слив «незначимым» и обошёл вето."""
    from services.budget_scaler import _waster_min_spend_usd

    # Значение выше потолка (как из руками правленого settings.json) → клампится к 1000
    assert _waster_min_spend_usd({"waster_min_spend_usd": 999_999}) == 1000.0
    assert _waster_min_spend_usd({"waster_min_spend_usd": 1000.01}) == 1000.0
    # Ровно потолок — проходит как есть
    assert _waster_min_spend_usd({"waster_min_spend_usd": 1000}) == 1000.0
    # В допустимом диапазоне — значение не трогаем
    assert _waster_min_spend_usd({"waster_min_spend_usd": 42.5}) == 42.5
