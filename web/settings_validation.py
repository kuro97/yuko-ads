"""
Валидатор тела POST /api/settings.
Вынесено из web/app.py механически, без изменения логики и текстов ошибок
(см. docs/specs/ARCH-phase6-engineering.md, T-SETTINGS-1).

Чистая функция: принимает тело запроса (data) и текущие настройки (current —
результат load_settings()), возвращает НОВЫЙ settings-dict для сохранения.
Ручка web/app.py::update_settings вызывает validate_settings_update, затем
save_settings и возвращает результат — сама логики валидации не содержит.
"""

import math
from datetime import datetime

from fastapi import HTTPException


_ALLOWED_CLEANER_KEYS = {
    "enabled",
    "dry_run",
    "proactive_enabled",
    "stale_days",
    "adset_threshold",
    "target_free",
    "critical_free_slots",
    "hard_reserve_slots",
    "allow_irreversible_delete",
    "max_manifest_candidates_per_adset",
    "max_deletes_per_workflow",
    "alert_dedup_hours",
    "managed_account_kinds",
}
_ALLOWED_REPLACEMENT_KEYS = {
    "enabled",
    "verify_interval_minutes",
    "max_pending_hours",
}
_ALLOWED_RECOVERY_KEYS = {
    "enabled",
    "since",
    "max_cards_per_day",
    "managed_account_kinds",
}
_ALLOWED_APPROVAL_KEYS = {"digest_hour"}
# Автономные действия бота: ровно один класс (пауза подтверждённого слива)
# плюс предохранитель от аномалии в данных. Расширение набора — отдельное
# решение владельца, а не правка списка.
_ALLOWED_AUTONOMOUS_KEYS = {"pause_confirmed_wasters", "pause_all_candidates", "anomaly_guard"}
_ALLOWED_ACCOUNT_KINDS = {"offline", "online"}
_RECOVERY_CUTOFF = datetime.fromisoformat("2026-07-01T00:00:00+05:00")


def _require_json_bool(value: object, field_name: str) -> bool:
    """Новые safety-флаги принимают только настоящий JSON boolean."""
    if type(value) is not bool:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} должен быть JSON boolean",
        )
    return value


def _require_bounded_int(
    value: object,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int:
        raise HTTPException(status_code=400, detail=f"{field_name} должен быть int")
    if not minimum <= value <= maximum:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} должен быть от {minimum} до {maximum}",
        )
    return value


def _require_account_kinds(value: object, field_name: str) -> list[str]:
    """Проверяет exact JSON list без строковой/tuple-коэрции."""
    if type(value) is not list or not value:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} должен быть непустым JSON list",
        )
    if any(type(kind) is not str or kind not in _ALLOWED_ACCOUNT_KINDS for kind in value):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} содержит неизвестный account kind",
        )
    if len(value) != len(set(value)):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} не должен содержать дубликаты",
        )
    return list(value)


def _validate_approval_update(value: object, current: dict) -> dict:
    """Блок approval: пока единственное поле — час дневного дайджеста."""
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="approval должен быть объектом")
    unknown = set(value) - _ALLOWED_APPROVAL_KEYS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестные поля approval: {sorted(unknown)}",
        )
    validated = dict(current)
    if "digest_hour" in value:
        validated["digest_hour"] = _require_bounded_int(
            value["digest_hour"], "digest_hour", 0, 23
        )
    return validated


def _nested_defaults(block_name: str) -> dict:
    """Берёт nested defaults из единственного runtime-источника правды."""
    from services.autopilot import AUTOPILOT_DEFAULTS

    return dict(AUTOPILOT_DEFAULTS[block_name])


def _validate_cleaner_update(value: object, current: dict) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="autopilot.cleaner должен быть объектом")
    unknown = set(value) - _ALLOWED_CLEANER_KEYS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестные поля autopilot.cleaner: {sorted(unknown)}",
        )

    validated: dict = {}
    for field_name in (
        "enabled",
        "dry_run",
        "proactive_enabled",
        "allow_irreversible_delete",
    ):
        if field_name in value:
            validated[field_name] = _require_json_bool(
                value[field_name], f"autopilot.cleaner.{field_name}"
            )
    ranges = {
        "stale_days": (15, 365),
        "adset_threshold": (40, 50),
        "target_free": (1, 10),
        "critical_free_slots": (0, 10),
        "hard_reserve_slots": (1, 5),
        "max_manifest_candidates_per_adset": (1, 10),
        "max_deletes_per_workflow": (1, 5),
        "alert_dedup_hours": (1, 24),
    }
    for field_name, (minimum, maximum) in ranges.items():
        if field_name in value:
            validated[field_name] = _require_bounded_int(
                value[field_name], field_name, minimum, maximum
            )
    if "managed_account_kinds" in value:
        validated["managed_account_kinds"] = _require_account_kinds(
            value["managed_account_kinds"],
            "autopilot.cleaner.managed_account_kinds",
        )

    current_nested = current if isinstance(current, dict) else {}
    known_current = {
        key: item for key, item in current_nested.items() if key in _ALLOWED_CLEANER_KEYS
    }
    merged = {**_nested_defaults("cleaner"), **known_current, **validated}

    critical = _require_bounded_int(
        merged["critical_free_slots"],
        "critical_free_slots",
        0,
        merged["target_free"],
    )
    merged["critical_free_slots"] = critical
    if merged["adset_threshold"] != 50 - merged["target_free"]:
        raise HTTPException(
            status_code=400,
            detail="adset_threshold должен равняться 50 - target_free",
        )
    return merged


def _validate_replacement_update(value: object, current: dict) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="autopilot.replacement должен быть объектом")
    unknown = set(value) - _ALLOWED_REPLACEMENT_KEYS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестные поля autopilot.replacement: {sorted(unknown)}",
        )

    validated: dict = {}
    if "enabled" in value:
        validated["enabled"] = _require_json_bool(
            value["enabled"], "autopilot.replacement.enabled"
        )
    for field_name, minimum, maximum in (
        ("verify_interval_minutes", 5, 60),
        ("max_pending_hours", 1, 168),
    ):
        if field_name in value:
            validated[field_name] = _require_bounded_int(
                value[field_name], field_name, minimum, maximum
            )
    current_nested = current if isinstance(current, dict) else {}
    known_current = {
        key: item for key, item in current_nested.items() if key in _ALLOWED_REPLACEMENT_KEYS
    }
    return {**_nested_defaults("replacement"), **known_current, **validated}


def _validate_recovery_update(value: object, current: dict) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="autopilot.recovery должен быть объектом")
    unknown = set(value) - _ALLOWED_RECOVERY_KEYS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестные поля autopilot.recovery: {sorted(unknown)}",
        )

    validated: dict = {}
    if "enabled" in value:
        validated["enabled"] = _require_json_bool(
            value["enabled"], "autopilot.recovery.enabled"
        )
    if "max_cards_per_day" in value:
        validated["max_cards_per_day"] = _require_bounded_int(
            value["max_cards_per_day"], "max_cards_per_day", 1, 10
        )
    if "managed_account_kinds" in value:
        validated["managed_account_kinds"] = _require_account_kinds(
            value["managed_account_kinds"],
            "autopilot.recovery.managed_account_kinds",
        )
    if "since" in value:
        since = value["since"]
        if type(since) is not str:
            raise HTTPException(status_code=400, detail="autopilot.recovery.since должен быть ISO datetime")
        try:
            parsed_since = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="autopilot.recovery.since должен быть ISO datetime",
            ) from None
        if parsed_since.tzinfo is None or parsed_since.utcoffset() is None:
            raise HTTPException(
                status_code=400,
                detail="autopilot.recovery.since должен содержать timezone",
            )
        if parsed_since < _RECOVERY_CUTOFF:
            raise HTTPException(
                status_code=400,
                detail="autopilot.recovery.since не может быть раньше 2026-07-01T00:00:00+05:00",
            )
        validated["since"] = since

    current_nested = current if isinstance(current, dict) else {}
    known_current = {
        key: item for key, item in current_nested.items() if key in _ALLOWED_RECOVERY_KEYS
    }
    return {**_nested_defaults("recovery"), **known_current, **validated}


def _validate_autonomous_update(value: object, current: dict) -> dict:
    """autopilot.autonomous — мастер-ключ автономных пауз и предохранитель.

    Оба флага принимаются ТОЛЬКО как настоящий JSON-boolean (_require_json_bool):
    строка "false" не должна молча стать True и включить боту право выключать
    рекламу. Неизвестное поле — 400, а не молчаливое сохранение: правленый
    руками settings.json деградирует в безопасную сторону уже в рантайме
    (services/autonomous_pause.py читает мастер-ключ через ``is True``).
    """
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=400, detail="autopilot.autonomous должен быть объектом"
        )
    unknown = set(value) - _ALLOWED_AUTONOMOUS_KEYS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестные поля autopilot.autonomous: {sorted(unknown)}",
        )

    validated: dict = {}
    for key in ("pause_confirmed_wasters", "pause_all_candidates", "anomaly_guard"):
        if key in value:
            validated[key] = _require_json_bool(
                value[key], f"autopilot.autonomous.{key}"
            )

    current_nested = current if isinstance(current, dict) else {}
    known_current = {
        key: item
        for key, item in current_nested.items()
        if key in _ALLOWED_AUTONOMOUS_KEYS
    }
    return {**_nested_defaults("autonomous"), **known_current, **validated}


_ALLOWED_EARLY_KILL_KEYS = frozenset({
    "mode", "max_age_days", "max_per_day", "floor_usd", "cap_usd",
    "cpl_window_days", "cpl_min_leads", "multiplier_no_lead", "multiplier_one_lead",
    "account_multipliers",
    # правило B «зрелый ноль» и страж загрузки ОП (волна 2a)
    "b_enabled", "b_min_mature_leads", "b_maturity_hours", "b_min_spend_usd", "b_cpl_multiplier",
    "b_small_max_leads", "b_small_cpl_multiplier", "op_guard_enabled",
    # волна 2b: режим правила B и маркеры исключения
    "b_mode", "b_exclude_name_markers",
    # лестница «заявки есть, квалов мало»
    "b_ladder", "b_ladder_accounts", "b_tail_min_leads", "b_tail_norm_share",
    "b_qual_norm_fallback_pct", "b_qual_norm_min_leads",
    # цена квала от средней по кабинету
    "c_price_mode", "c_cpq_norm_mult", "c_price_min_spend_mult", "c_price_min_mature",
    "c_cpq_norm_min_quals", "c_cpq_norm_fallback_usd", "c_cpq_plank_floor_usd",
    # волна 2c: правило C (качество на объёме) и правило S (голодные), суточный слот
    "c_mode", "c_min_mature_leads", "c_max_qual_pct", "c_cpq_multiplier", "c_cpq_fallback_usd",
    "s_mode", "s_min_age_days", "s_max_spend_usd", "s_adset_min_active", "s_max_per_day",
    "daily_hour",
})
# Все правила принимают active: бой идёт через штатный конвейер предложений.
_EARLY_KILL_MODES = ("off", "shadow", "active")
_EARLY_KILL_MODE_KEYS = ("mode", "b_mode", "c_mode", "s_mode")
_EARLY_KILL_BOOL_KEYS = ("b_enabled", "op_guard_enabled")
_EARLY_KILL_INT_BOUNDS = {
    "max_age_days": (1, 30),
    "max_per_day": (1, 50),
    "cpl_window_days": (3, 60),
    "cpl_min_leads": (1, 1000),
    "b_min_mature_leads": (1, 100),
    "b_maturity_hours": (24, 168),
    "b_small_max_leads": (1, 10),
    "b_tail_min_leads": (10, 500),
    "b_qual_norm_min_leads": (20, 5000),
    "c_min_mature_leads": (5, 500),
    "c_price_min_mature": (1, 100),
    "c_cpq_norm_min_quals": (3, 1000),
    "s_min_age_days": (1, 30),
    "s_adset_min_active": (1, 50),
    "s_max_per_day": (1, 200),
    "daily_hour": (0, 23),
}
_EARLY_KILL_FLOAT_BOUNDS = {
    "floor_usd": (0.0, 1000.0),
    "cap_usd": (0.0, 5000.0),
    "multiplier_no_lead": (0.5, 20.0),
    "multiplier_one_lead": (0.5, 30.0),
    "b_min_spend_usd": (0.0, 1000.0),
    "b_cpl_multiplier": (0.5, 20.0),
    "b_small_cpl_multiplier": (0.5, 30.0),
    "b_qual_norm_fallback_pct": (1.0, 60.0),
    "c_max_qual_pct": (0.0, 100.0),
    "c_cpq_multiplier": (1.0, 20.0),
    "c_cpq_norm_mult": (0.5, 5.0),
    "c_price_min_spend_mult": (1.0, 10.0),
    "c_cpq_norm_fallback_usd": (10.0, 1000.0),
    "c_cpq_plank_floor_usd": (10.0, 1000.0),
    "s_max_spend_usd": (0.0, 500.0),
}


def _parse_ladder(raw: object, label: str) -> list:
    """Ступени лестницы: список пар [зрелых ≥ n, квалов ≤ k], 1–10 ступеней, 0 ≤ k < n ≤ 500."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 10:
        raise HTTPException(status_code=400, detail=f"autopilot.early_kill.{label} должен быть списком из 1–10 ступеней")
    steps = []
    for item in raw:
        ok = (
            isinstance(item, list) and len(item) == 2
            and all(isinstance(x, int) and not isinstance(x, bool) for x in item)
            and 1 <= item[0] <= 500 and 0 <= item[1] < item[0]
        )
        if not ok:
            raise HTTPException(
                status_code=400,
                detail=f"autopilot.early_kill.{label}: ступень — пара [заявок, квалов], 0 ≤ квалов < заявок ≤ 500",
            )
        steps.append([item[0], item[1]])
    return sorted(steps)


def _validate_early_kill_update(value: object, current: dict) -> dict:
    """autopilot.early_kill — ранний стоп «расход ≥ N цен лида без заявки».

    mode принимается только из _EARLY_KILL_MODES (active появится в волне 2
    вместе с исполнением). Числа — в разумных границах, множители при одной
    заявке не ниже множителя без заявки. account_multipliers — объект
    {"<account_id>": {"no_lead": x, "one_lead": y}}; неизвестное поле — 400.
    """
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="autopilot.early_kill должен быть объектом")
    unknown = set(value) - _ALLOWED_EARLY_KILL_KEYS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестные поля autopilot.early_kill: {sorted(unknown)}",
        )

    validated: dict = {}
    for mode_key in _EARLY_KILL_MODE_KEYS:
        if mode_key in value:
            mode = value[mode_key]
            if mode not in _EARLY_KILL_MODES:
                raise HTTPException(
                    status_code=400,
                    detail=f"autopilot.early_kill.{mode_key} должен быть одним из {list(_EARLY_KILL_MODES)}",
                )
            validated[mode_key] = mode
    if "c_cpq_fallback_usd" in value:
        raw_map = value["c_cpq_fallback_usd"]
        if not isinstance(raw_map, dict):
            raise HTTPException(status_code=400, detail="autopilot.early_kill.c_cpq_fallback_usd должен быть объектом")
        parsed_map: dict = {}
        for account, amount in raw_map.items():
            if not isinstance(account, str) or not account.strip():
                raise HTTPException(status_code=400, detail="c_cpq_fallback_usd: ключ должен быть id кабинета")
            parsed = _coerce_finite_float(amount, f"c_cpq_fallback_usd[{account}]")
            if not 1.0 <= parsed <= 5000.0:
                raise HTTPException(status_code=400, detail=f"c_cpq_fallback_usd[{account}] должен быть от 1 до 5000")
            parsed_map[account.replace("act_", "")] = parsed
        validated["c_cpq_fallback_usd"] = parsed_map
    if "c_price_mode" in value:
        if value["c_price_mode"] not in ("median", "account_norm"):
            raise HTTPException(status_code=400, detail="autopilot.early_kill.c_price_mode должен быть median или account_norm")
        validated["c_price_mode"] = value["c_price_mode"]
    if "b_ladder" in value:
        validated["b_ladder"] = None if value["b_ladder"] is None else _parse_ladder(value["b_ladder"], "b_ladder")
    if "b_ladder_accounts" in value:
        raw_ladders = value["b_ladder_accounts"]
        if not isinstance(raw_ladders, dict):
            raise HTTPException(status_code=400, detail="autopilot.early_kill.b_ladder_accounts должен быть объектом")
        parsed_ladders: dict = {}
        for account, steps in raw_ladders.items():
            if not isinstance(account, str) or not account.strip():
                raise HTTPException(status_code=400, detail="b_ladder_accounts: ключ должен быть id кабинета")
            parsed_ladders[account.replace("act_", "")] = _parse_ladder(steps, f"b_ladder_accounts[{account}]")
        validated["b_ladder_accounts"] = parsed_ladders
    if "b_tail_norm_share" in value:
        if value["b_tail_norm_share"] is None:
            validated["b_tail_norm_share"] = None
        else:
            share = _coerce_finite_float(value["b_tail_norm_share"], "autopilot.early_kill.b_tail_norm_share")
            if not 0.1 <= share <= 1.5:
                raise HTTPException(status_code=400, detail="autopilot.early_kill.b_tail_norm_share должен быть от 0.1 до 1.5")
            validated["b_tail_norm_share"] = share
    if "b_exclude_name_markers" in value:
        markers = value["b_exclude_name_markers"]
        if not isinstance(markers, list) or not all(isinstance(m, str) and m.strip() for m in markers):
            raise HTTPException(
                status_code=400,
                detail="autopilot.early_kill.b_exclude_name_markers должен быть списком непустых строк",
            )
        validated["b_exclude_name_markers"] = [m.strip().lower() for m in markers]
    for key in _EARLY_KILL_BOOL_KEYS:
        if key in value:
            validated[key] = _require_json_bool(value[key], f"autopilot.early_kill.{key}")
    for key, (low, high) in _EARLY_KILL_INT_BOUNDS.items():
        if key in value:
            raw = value[key]
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise HTTPException(status_code=400, detail=f"autopilot.early_kill.{key} должен быть int")
            if not low <= raw <= high:
                raise HTTPException(
                    status_code=400,
                    detail=f"autopilot.early_kill.{key} должен быть от {low} до {high}",
                )
            validated[key] = raw
    for key, (low, high) in _EARLY_KILL_FLOAT_BOUNDS.items():
        if key in value:
            parsed = _coerce_finite_float(value[key], f"autopilot.early_kill.{key}")
            if not low <= parsed <= high:
                raise HTTPException(
                    status_code=400,
                    detail=f"autopilot.early_kill.{key} должен быть от {low} до {high}",
                )
            validated[key] = parsed
    if "account_multipliers" in value:
        raw_map = value["account_multipliers"]
        if not isinstance(raw_map, dict):
            raise HTTPException(
                status_code=400, detail="autopilot.early_kill.account_multipliers должен быть объектом"
            )
        parsed_map: dict = {}
        for account, block in raw_map.items():
            if not isinstance(account, str) or not account.strip():
                raise HTTPException(status_code=400, detail="account_multipliers: ключ должен быть id кабинета")
            if not isinstance(block, dict) or set(block) - {"no_lead", "one_lead"}:
                raise HTTPException(
                    status_code=400,
                    detail=f"account_multipliers[{account}] допускает только no_lead и one_lead",
                )
            entry: dict = {}
            for sub_key, (low, high) in (("no_lead", (0.5, 20.0)), ("one_lead", (0.5, 30.0))):
                if sub_key in block:
                    parsed = _coerce_finite_float(block[sub_key], f"account_multipliers[{account}].{sub_key}")
                    if not low <= parsed <= high:
                        raise HTTPException(
                            status_code=400,
                            detail=f"account_multipliers[{account}].{sub_key} должен быть от {low} до {high}",
                        )
                    entry[sub_key] = parsed
            parsed_map[account.replace("act_", "")] = entry
        validated["account_multipliers"] = parsed_map

    current_nested = current if isinstance(current, dict) else {}
    known_current = {
        key: item for key, item in current_nested.items() if key in _ALLOWED_EARLY_KILL_KEYS
    }
    merged = {**_nested_defaults("early_kill"), **known_current, **validated}
    if float(merged["cap_usd"]) < float(merged["floor_usd"]):
        raise HTTPException(status_code=400, detail="autopilot.early_kill.cap_usd не может быть ниже floor_usd")
    if float(merged["multiplier_one_lead"]) < float(merged["multiplier_no_lead"]):
        raise HTTPException(
            status_code=400,
            detail="autopilot.early_kill.multiplier_one_lead не может быть ниже multiplier_no_lead",
        )
    return merged


def _coerce_finite_float(value, field_name: str) -> float:
    """Парсит число в float; отклоняет NaN/Infinity/-Infinity и нечисловые значения.

    Булевы НЕ принимаем как число (True/False не должны молча стать 1.0/0.0 —
    это скрытая коэрция типа). При ошибке кидает HTTPException(400).
    """
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field_name} должен быть числом")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field_name} должен быть числом")
    if not math.isfinite(v):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} должен быть конечным числом (не NaN/Infinity)",
        )
    return v


def _coerce_strict_bool(value, field_name: str) -> bool:
    """Строгая коэрция булева. НЕ наивный bool("false") (который вернул бы True
    для любой непустой строки). Принимает только Python bool, целые 0/1 и явные
    строковые формы (true/false/yes/no/on/off/1/0). Иначе — HTTPException(400).
    """
    if isinstance(value, bool):
        return value
    # bool — подкласс int, поэтому реальные bool уже отсеяны выше
    if isinstance(value, int) and value in (0, 1):
        return value == 1
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    raise HTTPException(status_code=400, detail=f"{field_name} должен быть булевым (true/false)")


def validate_settings_update(data: dict, current: dict) -> dict:
    """Валидирует тело POST /api/settings и возвращает новый settings-dict
    (мерж поверх current). При невалидных данных кидает HTTPException(400, detail=...)
    с тем же текстом, что и раньше — на них завязаны тесты test_settings_autopilot.py."""
    settings = current
    if "auto_apply" in data:
        settings["auto_apply"] = bool(data["auto_apply"])

    # Блок "approval" — контур персонального одобрения владельцем.
    if "approval" in data:
        settings["approval"] = _validate_approval_update(
            data["approval"],
            settings.get("approval") if isinstance(settings.get("approval"), dict) else {},
        )

    # Поддержка блока "autopilot" с валидацией полей
    if "autopilot" in data:
        ap_input = data["autopilot"]
        if not isinstance(ap_input, dict):
            raise HTTPException(status_code=400, detail="autopilot должен быть объектом")

        # Допустимые поля и их типы/ограничения
        _ALLOWED_AP_KEYS = {
            "enabled", "kill_switch", "mode", "max_pauses_per_run",
            "max_pauses_per_day",
            "min_hours_between_runs", "min_days_protect",
            "launch_enabled", "max_launches_per_day",
            "launch_checker",  # server-owned checker: observe | enforce
            # Budget Scaler — масштабирование бюджетов победителей
            "scale_enabled", "max_budget_increase_pct", "max_adset_budget_mult",
            "max_adset_daily_budget", "max_total_daily_budget", "max_scales_per_run",
            # Budget Scaler v2 — вложенный блок флагов доп. предохранителей (Wave 3B)
            "scaler_v2",
            # Plan-based gate — ID месячного Google-листа (вкладка GENERAL)
            "plan_sheet_id",
            # Google Ads spend snapshot — ID таблицы дневного снимка расхода
            "google_spend_sheet_id",
            "cleaner",   # вложенный блок настроек чистильщика адсетов
            "replacement",  # workflow: новая ACTIVE до PAUSE старой
            "recovery",  # read-only audit и отдельный ручной recovery rollout
            "guardian",  # НОВЫЙ: вложенный блок настроек Стража (early_waster/wasted_no_crm)
            "early_kill",  # ранний стоп «расход ≥ N цен лида без заявки» (services/early_kill.py)
            "spend_guard",    # Страж трат адсетов (нулевой расход / перерасход)
            "online_report",  # Ежедневный онлайн-отчёт из CDP
            "hypothesist",  # Аналитик-Гипотезник (Фаза 4): журнал/вердикт/влияние/отчёт
            "cdp",       # CDP Acme — источник юнитки для Бюджет-пилота (ДРР+план-гейт)
            # Удержание рангового правила (ARCH-rank-pause-hold, §6): не паузить
            # «почти-прибыльную» рекламу сразу, а держать hold_days и смотреть на оплаты
            "hold_enabled", "hold_romi_target", "hold_romi_ratio",
            "hold_spend_max", "hold_min_qual_pct", "hold_days",
            # Удержание по живым встречам AMO (ARCH-hold-meetings, фаза 2): порог
            # meetings_scheduled+meetings_held, при котором тоже считаем «есть потенциал»
            "hold_min_meetings",
            # Тренд недельных когорт (волна 3): off | shadow | active.
            # Дефолт shadow — тренд только пишет в отчёты, исходов не меняет.
            "trend",
            # Автономные действия бота: единственный автономный класс —
            # пауза подтверждённого слива.
            "autonomous",
        }
        _VALID_MODES = ("dry_run", "active")

        # Неизвестные поля → 400
        unknown = set(ap_input.keys()) - _ALLOWED_AP_KEYS
        if unknown:
            raise HTTPException(status_code=400, detail=f"Неизвестные поля autopilot: {sorted(unknown)}")

        # Валидация типов
        validated_ap: dict = {}
        if "enabled" in ap_input:
            validated_ap["enabled"] = bool(ap_input["enabled"])
        if "mode" in ap_input:
            if ap_input["mode"] not in _VALID_MODES:
                raise HTTPException(status_code=400, detail=f"mode должен быть одним из {_VALID_MODES}")
            validated_ap["mode"] = ap_input["mode"]
        if "max_pauses_per_run" in ap_input:
            try:
                v = int(ap_input["max_pauses_per_run"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="max_pauses_per_run должен быть int")
            if not 1 <= v <= 10:
                raise HTTPException(status_code=400, detail="max_pauses_per_run должен быть от 1 до 10")
            validated_ap["max_pauses_per_run"] = v
        if "max_pauses_per_day" in ap_input:
            try:
                v = int(ap_input["max_pauses_per_day"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="max_pauses_per_day должен быть int")
            if not 1 <= v <= 50:
                raise HTTPException(status_code=400, detail="max_pauses_per_day должен быть от 1 до 50")
            validated_ap["max_pauses_per_day"] = v
        if "min_hours_between_runs" in ap_input:
            try:
                v = int(ap_input["min_hours_between_runs"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="min_hours_between_runs должен быть int")
            if not 1 <= v <= 24:
                raise HTTPException(status_code=400, detail="min_hours_between_runs должен быть от 1 до 24")
            validated_ap["min_hours_between_runs"] = v
        if "kill_switch" in ap_input:
            validated_ap["kill_switch"] = _require_json_bool(
                ap_input["kill_switch"], "autopilot.kill_switch"
            )
        if "min_days_protect" in ap_input:
            try:
                v = int(ap_input["min_days_protect"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="min_days_protect должен быть int")
            if not 0 <= v <= 30:
                raise HTTPException(status_code=400, detail="min_days_protect должен быть от 0 до 30")
            validated_ap["min_days_protect"] = v
        # Удержание рангового правила (hold) — «почти-прибыльную» рекламу не
        # паузим сразу, а держим hold_days и смотрим на оплаты (ARCH-rank-pause-hold §6)
        if "hold_enabled" in ap_input:
            validated_ap["hold_enabled"] = bool(ap_input["hold_enabled"])
        if "hold_romi_target" in ap_input:
            try:
                v = int(ap_input["hold_romi_target"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="hold_romi_target должен быть int")
            if not 50 <= v <= 500:
                raise HTTPException(status_code=400, detail="hold_romi_target должен быть от 50 до 500")
            validated_ap["hold_romi_target"] = v
        if "hold_romi_ratio" in ap_input:
            try:
                v = float(ap_input["hold_romi_ratio"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="hold_romi_ratio должен быть числом")
            if not 0.1 <= v <= 1.5:
                raise HTTPException(status_code=400, detail="hold_romi_ratio должен быть от 0.1 до 1.5")
            validated_ap["hold_romi_ratio"] = v
        if "hold_spend_max" in ap_input:
            try:
                v = int(ap_input["hold_spend_max"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="hold_spend_max должен быть int")
            if not 1 <= v <= 5000:
                raise HTTPException(status_code=400, detail="hold_spend_max должен быть от 1 до 5000")
            validated_ap["hold_spend_max"] = v
        if "hold_min_qual_pct" in ap_input:
            try:
                v = float(ap_input["hold_min_qual_pct"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="hold_min_qual_pct должен быть числом")
            if not 0 <= v <= 100:
                raise HTTPException(status_code=400, detail="hold_min_qual_pct должен быть от 0 до 100")
            validated_ap["hold_min_qual_pct"] = v
        if "hold_days" in ap_input:
            try:
                v = int(ap_input["hold_days"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="hold_days должен быть int")
            if not 1 <= v <= 30:
                raise HTTPException(status_code=400, detail="hold_days должен быть от 1 до 30")
            validated_ap["hold_days"] = v
        # Порог «потенциала» по встречам AMO (ARCH-hold-meetings §6): scheduled+held
        # >= этого значения тоже считается достаточным основанием для удержания
        if "hold_min_meetings" in ap_input:
            try:
                v = int(ap_input["hold_min_meetings"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="hold_min_meetings должен быть int")
            if not 1 <= v <= 20:
                raise HTTPException(status_code=400, detail="hold_min_meetings должен быть от 1 до 20")
            validated_ap["hold_min_meetings"] = v
        # Авто-запуск: включить/выключить и лимит запусков в день
        if "launch_enabled" in ap_input:
            validated_ap["launch_enabled"] = bool(ap_input["launch_enabled"])
        if "max_launches_per_day" in ap_input:
            try:
                v = int(ap_input["max_launches_per_day"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="max_launches_per_day должен быть int")
            if not 1 <= v <= 10:
                raise HTTPException(status_code=400, detail="max_launches_per_day должен быть от 1 до 10")
            validated_ap["max_launches_per_day"] = v
        if "launch_checker" in ap_input:
            checker_input = ap_input["launch_checker"]
            if not isinstance(checker_input, dict):
                raise HTTPException(
                    status_code=400,
                    detail="autopilot.launch_checker должен быть объектом",
                )
            unknown_checker = set(checker_input) - {"mode"}
            if unknown_checker:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Неизвестные поля autopilot.launch_checker: "
                        f"{sorted(unknown_checker)}"
                    ),
                )
            validated_checker: dict = {}
            if "mode" in checker_input:
                checker_mode = checker_input["mode"]
                if checker_mode not in ("observe", "enforce"):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "autopilot.launch_checker.mode должен быть "
                            "'observe' или 'enforce'"
                        ),
                    )
                validated_checker["mode"] = checker_mode
            existing_checker_raw = (
                (settings.get("autopilot") or {}).get("launch_checker") or {}
            )
            existing_checker = (
                existing_checker_raw
                if isinstance(existing_checker_raw, dict)
                else {}
            )
            validated_ap["launch_checker"] = {
                **existing_checker,
                **validated_checker,
            }
        # Тренд недельных когорт (волна 3, services/trend_gate.py): единственный
        # переключатель денежного поведения тренда. Шаблон — launch_checker выше:
        # вложенный объект с одним полем mode, неизвестные поля → 400, значение
        # из фиксированного набора, мерж поверх существующего блока.
        if "trend" in ap_input:
            trend_input = ap_input["trend"]
            if not isinstance(trend_input, dict):
                raise HTTPException(
                    status_code=400,
                    detail="autopilot.trend должен быть объектом",
                )
            unknown_trend = set(trend_input) - {"mode"}
            if unknown_trend:
                raise HTTPException(
                    status_code=400,
                    detail=f"Неизвестные поля autopilot.trend: {sorted(unknown_trend)}",
                )
            validated_trend: dict = {}
            if "mode" in trend_input:
                trend_mode = trend_input["mode"]
                if trend_mode not in ("off", "shadow", "active"):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "autopilot.trend.mode должен быть "
                            "'off', 'shadow' или 'active'"
                        ),
                    )
                validated_trend["mode"] = trend_mode
            existing_trend_raw = (settings.get("autopilot") or {}).get("trend") or {}
            existing_trend = (
                existing_trend_raw if isinstance(existing_trend_raw, dict) else {}
            )
            validated_ap["trend"] = {**existing_trend, **validated_trend}
        # Budget Scaler — мастер-ключ и лимиты масштабирования бюджета
        if "scale_enabled" in ap_input:
            validated_ap["scale_enabled"] = bool(ap_input["scale_enabled"])
        # Семантика ключа теперь «% роста бюджета В СУТКИ на адсет» (дневной кап
        # per-adset считает services/budget_daily_cap.py), а не «% за прогон».
        # Диапазон 1..100 оставлен как верхняя граница валидации (сама логика
        # дневного капа не даёт превысить фактический дневной лимит).
        if "max_budget_increase_pct" in ap_input:
            try:
                v = int(ap_input["max_budget_increase_pct"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="max_budget_increase_pct должен быть int")
            if not 1 <= v <= 100:
                raise HTTPException(
                    status_code=400,
                    detail="max_budget_increase_pct должен быть числом 1..100 (% роста в сутки на адсет)",
                )
            validated_ap["max_budget_increase_pct"] = v
        if "max_adset_budget_mult" in ap_input:
            try:
                v = float(ap_input["max_adset_budget_mult"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="max_adset_budget_mult должен быть числом")
            if not 1.0 <= v <= 5.0:
                raise HTTPException(status_code=400, detail="max_adset_budget_mult должен быть от 1.0 до 5.0")
            validated_ap["max_adset_budget_mult"] = v
        if "max_adset_daily_budget" in ap_input:
            try:
                v = int(ap_input["max_adset_daily_budget"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="max_adset_daily_budget должен быть int")
            if not 1 <= v <= 2000:
                raise HTTPException(status_code=400, detail="max_adset_daily_budget должен быть от 1 до 2000")
            validated_ap["max_adset_daily_budget"] = v
        if "max_total_daily_budget" in ap_input:
            try:
                v = int(ap_input["max_total_daily_budget"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="max_total_daily_budget должен быть int")
            if not 1 <= v <= 50000:
                raise HTTPException(status_code=400, detail="max_total_daily_budget должен быть от 1 до 50000")
            validated_ap["max_total_daily_budget"] = v
        if "max_scales_per_run" in ap_input:
            try:
                v = int(ap_input["max_scales_per_run"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="max_scales_per_run должен быть int")
            if not 1 <= v <= 10:
                raise HTTPException(status_code=400, detail="max_scales_per_run должен быть от 1 до 10")
            validated_ap["max_scales_per_run"] = v
        # Plan-based gate — Google Spreadsheet ID для вкладки GENERAL
        if "plan_sheet_id" in ap_input:
            v = str(ap_input["plan_sheet_id"]).strip()
            validated_ap["plan_sheet_id"] = v
        # Google Ads spend snapshot — ID таблицы дневного снимка расходов
        if "google_spend_sheet_id" in ap_input:
            v = str(ap_input["google_spend_sheet_id"]).strip()
            validated_ap["google_spend_sheet_id"] = v

        # Adset Cleaner — вложенный блок настроек ночной чистки адсетов
        if "cleaner" in ap_input:
            existing_cleaner = (settings.get("autopilot") or {}).get("cleaner") or {}
            validated_ap["cleaner"] = _validate_cleaner_update(
                ap_input["cleaner"], existing_cleaner
            )

        if "replacement" in ap_input:
            existing_replacement = (
                (settings.get("autopilot") or {}).get("replacement") or {}
            )
            validated_ap["replacement"] = _validate_replacement_update(
                ap_input["replacement"], existing_replacement
            )

        if "recovery" in ap_input:
            existing_recovery = (
                (settings.get("autopilot") or {}).get("recovery") or {}
            )
            validated_ap["recovery"] = _validate_recovery_update(
                ap_input["recovery"], existing_recovery
            )

        if "autonomous" in ap_input:
            existing_autonomous = (
                (settings.get("autopilot") or {}).get("autonomous") or {}
            )
            validated_ap["autonomous"] = _validate_autonomous_update(
                ap_input["autonomous"], existing_autonomous
            )

        if "early_kill" in ap_input:
            existing_early_kill = (
                (settings.get("autopilot") or {}).get("early_kill") or {}
            )
            validated_ap["early_kill"] = _validate_early_kill_update(
                ap_input["early_kill"], existing_early_kill
            )

        # Guardian — вложенный блок настроек Стража (early_waster/wasted_no_crm, §6.1 спеки)
        if "guardian" in ap_input:
            gd_input = ap_input["guardian"]
            if not isinstance(gd_input, dict):
                raise HTTPException(status_code=400, detail="autopilot.guardian должен быть объектом")
            _ALLOWED_GUARDIAN_KEYS = {
                "enabled", "early_dry_run", "early_min_age_hours", "early_min_spend",
                "early_day1_zero_spend", "early_cpl_mult", "early_day23_min_spend",
                "early_qual_override_pct",
                "wnc_dry_run", "wnc_min_spend", "wnc_min_leads", "wnc_min_days",
            }
            unknown_gd = set(gd_input.keys()) - _ALLOWED_GUARDIAN_KEYS
            if unknown_gd:
                raise HTTPException(status_code=400, detail=f"Неизвестные поля guardian: {sorted(unknown_gd)}")
            validated_gd: dict = {}
            if "enabled" in gd_input:
                validated_gd["enabled"] = bool(gd_input["enabled"])
            if "early_dry_run" in gd_input:
                validated_gd["early_dry_run"] = bool(gd_input["early_dry_run"])
            if "wnc_dry_run" in gd_input:
                validated_gd["wnc_dry_run"] = bool(gd_input["wnc_dry_run"])
            if "early_min_age_hours" in gd_input:
                try:
                    v = int(gd_input["early_min_age_hours"])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="early_min_age_hours должен быть int")
                if not 1 <= v <= 168:
                    raise HTTPException(status_code=400, detail="early_min_age_hours должен быть от 1 до 168")
                validated_gd["early_min_age_hours"] = v
            if "wnc_min_leads" in gd_input:
                try:
                    v = int(gd_input["wnc_min_leads"])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="wnc_min_leads должен быть int")
                if not 1 <= v <= 1000:
                    raise HTTPException(status_code=400, detail="wnc_min_leads должен быть от 1 до 1000")
                validated_gd["wnc_min_leads"] = v
            if "wnc_min_days" in gd_input:
                try:
                    v = int(gd_input["wnc_min_days"])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="wnc_min_days должен быть int")
                if not 1 <= v <= 30:
                    raise HTTPException(status_code=400, detail="wnc_min_days должен быть от 1 до 30")
                validated_gd["wnc_min_days"] = v
            # Float-пороги (>= 0) — early_min_spend, early_day1_zero_spend, early_cpl_mult,
            # early_day23_min_spend, early_qual_override_pct, wnc_min_spend
            for float_key in (
                "early_min_spend", "early_day1_zero_spend", "early_cpl_mult",
                "early_day23_min_spend", "early_qual_override_pct", "wnc_min_spend",
            ):
                if float_key in gd_input:
                    try:
                        v = float(gd_input[float_key])
                    except (TypeError, ValueError):
                        raise HTTPException(status_code=400, detail=f"{float_key} должен быть числом")
                    if v < 0:
                        raise HTTPException(status_code=400, detail=f"{float_key} должен быть >= 0")
                    validated_gd[float_key] = v
            # Мержим вложенный блок поверх существующего guardian-блока — не теряем
            # непереданные под-ключи (аналогично cleaner)
            existing_guardian = (settings.get("autopilot") or {}).get("guardian") or {}
            validated_ap["guardian"] = {**existing_guardian, **validated_gd}

        # Страж трат адсетов — вложенный блок (enabled + overspend_mult).
        if "spend_guard" in ap_input:
            sg_input = ap_input["spend_guard"]
            if not isinstance(sg_input, dict):
                raise HTTPException(status_code=400, detail="autopilot.spend_guard должен быть объектом")
            _ALLOWED_SG_KEYS = {"enabled", "overspend_mult"}
            unknown_sg = set(sg_input.keys()) - _ALLOWED_SG_KEYS
            if unknown_sg:
                raise HTTPException(status_code=400, detail=f"Неизвестные поля autopilot.spend_guard: {sorted(unknown_sg)}")
            validated_sg: dict = {}
            if "enabled" in sg_input:
                validated_sg["enabled"] = bool(sg_input["enabled"])
            if "overspend_mult" in sg_input:
                try:
                    v = float(sg_input["overspend_mult"])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="overspend_mult должен быть числом")
                if not 1.0 <= v <= 5.0:
                    raise HTTPException(status_code=400, detail="overspend_mult должен быть от 1.0 до 5.0")
                validated_sg["overspend_mult"] = v
            existing_sg = (settings.get("autopilot") or {}).get("spend_guard") or {}
            validated_ap["spend_guard"] = {**existing_sg, **validated_sg}

        # Ежедневный онлайн-отчёт из CDP — вложенный блок (только enabled).
        if "online_report" in ap_input:
            or_input = ap_input["online_report"]
            if not isinstance(or_input, dict):
                raise HTTPException(status_code=400, detail="autopilot.online_report должен быть объектом")
            _ALLOWED_OR_KEYS = {"enabled"}
            unknown_or = set(or_input.keys()) - _ALLOWED_OR_KEYS
            if unknown_or:
                raise HTTPException(status_code=400, detail=f"Неизвестные поля autopilot.online_report: {sorted(unknown_or)}")
            validated_or: dict = {}
            if "enabled" in or_input:
                validated_or["enabled"] = bool(or_input["enabled"])
            existing_or = (settings.get("autopilot") or {}).get("online_report") or {}
            validated_ap["online_report"] = {**existing_or, **validated_or}

        # Hypothesist — вложенный блок настроек Аналитика-Гипотезника (Фаза 4,
        # см. docs/specs/ARCH-phase4-hypothesist.md §10 T2). Безопасный контур:
        # только запись уроков и мягкая перестановка приоритетов, без денег.
        if "hypothesist" in ap_input:
            hp_input = ap_input["hypothesist"]
            if not isinstance(hp_input, dict):
                raise HTTPException(status_code=400, detail="autopilot.hypothesist должен быть объектом")
            _ALLOWED_HYPOTHESIST_KEYS = {
                "enabled", "min_age_days", "max_age_days", "min_spend",
                "segment_qual_min", "ttl_days", "dead_min_refuted",
            }
            unknown_hp = set(hp_input.keys()) - _ALLOWED_HYPOTHESIST_KEYS
            if unknown_hp:
                raise HTTPException(status_code=400, detail=f"Неизвестные поля autopilot.hypothesist: {sorted(unknown_hp)}")
            validated_hp: dict = {}
            if "enabled" in hp_input:
                validated_hp["enabled"] = bool(hp_input["enabled"])
            if "min_age_days" in hp_input:
                try:
                    v = int(hp_input["min_age_days"])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="min_age_days должен быть int")
                if not 1 <= v <= 30:
                    raise HTTPException(status_code=400, detail="min_age_days должен быть от 1 до 30")
                validated_hp["min_age_days"] = v
            if "max_age_days" in hp_input:
                try:
                    v = int(hp_input["max_age_days"])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="max_age_days должен быть int")
                if not 1 <= v <= 60:
                    raise HTTPException(status_code=400, detail="max_age_days должен быть от 1 до 60")
                validated_hp["max_age_days"] = v
            if "dead_min_refuted" in hp_input:
                try:
                    v = int(hp_input["dead_min_refuted"])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="dead_min_refuted должен быть int")
                if not 1 <= v <= 20:
                    raise HTTPException(status_code=400, detail="dead_min_refuted должен быть от 1 до 20")
                validated_hp["dead_min_refuted"] = v
            if "ttl_days" in hp_input:
                try:
                    v = int(hp_input["ttl_days"])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="ttl_days должен быть int")
                if not 1 <= v <= 365:
                    raise HTTPException(status_code=400, detail="ttl_days должен быть от 1 до 365")
                validated_hp["ttl_days"] = v
            # Float-пороги (>= 0) — min_spend (расход $), segment_qual_min (% квала)
            for float_key in ("min_spend", "segment_qual_min"):
                if float_key in hp_input:
                    try:
                        v = float(hp_input[float_key])
                    except (TypeError, ValueError):
                        raise HTTPException(status_code=400, detail=f"{float_key} должен быть числом")
                    if v < 0:
                        raise HTTPException(status_code=400, detail=f"{float_key} должен быть >= 0")
                    validated_hp[float_key] = v
            # Мержим вложенный блок поверх существующего hypothesist-блока — не теряем
            # непереданные под-ключи (аналогично cleaner/guardian)
            existing_hypothesist = (settings.get("autopilot") or {}).get("hypothesist") or {}
            validated_ap["hypothesist"] = {**existing_hypothesist, **validated_hp}

        # CDP Acme — источник юнитки (ДРР+план-гейт) вместо Google-листа.
        # Ключи: enabled (bool, дефолт true — CDP основной источник),
        # doubt_alerts (bool, дефолт true — протокол сомнений, Шаг A.2),
        # pace_engine (bool, дефолт true — движок
        # budget-context как основной источник темпа план-гейта, Шаг A.3),
        # payments_source (Шаг B, ARCH-cdp-payments) — источник факта оплаты:
        # "amo" (только AMO-статусы) | "erp" (боевой, max с ERP) | "shadow"
        # (дефолт, решения по AMO + ERP считается для сверки).
        if "cdp" in ap_input:
            cdp_input = ap_input["cdp"]
            if not isinstance(cdp_input, dict):
                raise HTTPException(status_code=400, detail="autopilot.cdp должен быть объектом")
            _ALLOWED_CDP_KEYS = {"enabled", "payments_source", "pace_engine", "doubt_alerts"}
            unknown_cdp = set(cdp_input.keys()) - _ALLOWED_CDP_KEYS
            if unknown_cdp:
                raise HTTPException(status_code=400, detail=f"Неизвестные поля autopilot.cdp: {sorted(unknown_cdp)}")
            validated_cdp: dict = {}
            if "enabled" in cdp_input:
                validated_cdp["enabled"] = bool(cdp_input["enabled"])
            if "payments_source" in cdp_input:
                payments_source = cdp_input["payments_source"]
                if payments_source not in ("amo", "erp", "shadow"):
                    raise HTTPException(
                        status_code=400,
                        detail="autopilot.cdp.payments_source ∈ {amo,erp,shadow}",
                    )
                validated_cdp["payments_source"] = payments_source
            if "doubt_alerts" in cdp_input:
                validated_cdp["doubt_alerts"] = bool(cdp_input["doubt_alerts"])
            if "pace_engine" in cdp_input:
                validated_cdp["pace_engine"] = bool(cdp_input["pace_engine"])
            existing_cdp = (settings.get("autopilot") or {}).get("cdp") or {}
            validated_ap["cdp"] = {**existing_cdp, **validated_cdp}

        # -------------------------------------------------------------------
        # Budget Scaler v2 — официальный settings-contract флагов доп.
        # предохранителей Budget Scaler v2 (Wave 3B).
        # Ключи фактически читаются в services/budget_scaler.py (grep cfg_v2.get):
        #   waster_min_spend_usd     — порог значимости слива, $ (_waster_min_spend_usd)
        #   require_fresh_7d         — честный 7d-гейт active-подъёма (_require_fresh_7d)
        #   engine_selfcheck_enabled — самопроверка прогноза CDP (Этап 4)
        #   sanity_cap_enabled       — стоп-кран сезонной нормы (Этап 4)
        #   sanity_cap_ratio         — порог стоп-крана (доля сезонной нормы)
        #   selfcheck_max_dev        — макс. относительное отставание факта до недоверия
        #
        # КРИТИЧНО (money-safety, Wave 1): НИ ОДИН ключ НЕ отключает строгое
        # confirmed_waster-вето. Из настроек вето читает ТОЛЬКО waster_min_spend_usd —
        # порог значимости САМОГО слива, жёстко ограниченный сверху (0..1000):
        # завышенный порог не должен превратиться в скрытый обход вето. Флага
        # «выключить вето» здесь нет и быть не может — неизвестный ключ → 400.
        # Булевы — строгая коэрция (НЕ наивный bool("false")); числа — finite,
        # NaN/Infinity отклоняются.
        if "scaler_v2" in ap_input:
            v2_input = ap_input["scaler_v2"]
            if not isinstance(v2_input, dict):
                raise HTTPException(status_code=400, detail="autopilot.scaler_v2 должен быть объектом")
            _ALLOWED_V2_KEYS = {
                "waster_min_spend_usd", "require_fresh_7d",
                "engine_selfcheck_enabled", "sanity_cap_enabled",
                "sanity_cap_ratio", "selfcheck_max_dev",
            }
            unknown_v2 = set(v2_input.keys()) - _ALLOWED_V2_KEYS
            if unknown_v2:
                raise HTTPException(
                    status_code=400,
                    detail=f"Неизвестные поля autopilot.scaler_v2: {sorted(unknown_v2)}",
                )
            validated_v2: dict = {}
            # Булевы — строгая коэрция (НЕ наивный bool)
            for bool_key in ("require_fresh_7d", "engine_selfcheck_enabled", "sanity_cap_enabled"):
                if bool_key in v2_input:
                    validated_v2[bool_key] = _coerce_strict_bool(
                        v2_input[bool_key], f"autopilot.scaler_v2.{bool_key}"
                    )
            # waster_min_spend_usd — $, finite, 0..1000. Верхняя граница — money-safety:
            # завышенный порог не должен обойти строгое confirmed_waster-вето.
            if "waster_min_spend_usd" in v2_input:
                v = _coerce_finite_float(v2_input["waster_min_spend_usd"], "waster_min_spend_usd")
                if not 0 <= v <= 1000:
                    raise HTTPException(status_code=400, detail="waster_min_spend_usd должен быть от 0 до 1000")
                validated_v2["waster_min_spend_usd"] = v
            # sanity_cap_ratio — доля сезонной нормы (0.1..1.0)
            if "sanity_cap_ratio" in v2_input:
                v = _coerce_finite_float(v2_input["sanity_cap_ratio"], "sanity_cap_ratio")
                if not 0.1 <= v <= 1.0:
                    raise HTTPException(status_code=400, detail="sanity_cap_ratio должен быть от 0.1 до 1.0")
                validated_v2["sanity_cap_ratio"] = v
            # selfcheck_max_dev — макс. относительное отставание факта (0.05..0.9)
            if "selfcheck_max_dev" in v2_input:
                v = _coerce_finite_float(v2_input["selfcheck_max_dev"], "selfcheck_max_dev")
                if not 0.05 <= v <= 0.9:
                    raise HTTPException(status_code=400, detail="selfcheck_max_dev должен быть от 0.05 до 0.9")
                validated_v2["selfcheck_max_dev"] = v
            # Мержим вложенный блок поверх существующего scaler_v2 — не теряем
            # непереданные под-ключи (аналогично cleaner/guardian/cdp)
            existing_v2 = (settings.get("autopilot") or {}).get("scaler_v2") or {}
            validated_ap["scaler_v2"] = {**existing_v2, **validated_v2}

        # Мержим поверх существующего блока
        existing_ap = settings.get("autopilot") or {}
        settings["autopilot"] = {**existing_ap, **validated_ap}

    # Поддержка блока "brief_generator" — мастер-выключатель авто-генератора ТЗ
    # (дефолт OFF, крон _cron_brief_generator читает этот флаг перед запуском)
    if "brief_generator" in data:
        bg_input = data["brief_generator"]
        if not isinstance(bg_input, dict):
            raise HTTPException(status_code=400, detail="brief_generator должен быть объектом")

        _ALLOWED_BG_KEYS = {"enabled"}

        unknown_bg = set(bg_input.keys()) - _ALLOWED_BG_KEYS
        if unknown_bg:
            raise HTTPException(
                status_code=400,
                detail=f"Неизвестные поля brief_generator: {sorted(unknown_bg)}",
            )

        validated_bg: dict = {}
        if "enabled" in bg_input:
            validated_bg["enabled"] = bool(bg_input["enabled"])

        existing_bg = settings.get("brief_generator") or {}
        settings["brief_generator"] = {**existing_bg, **validated_bg}

    return settings
