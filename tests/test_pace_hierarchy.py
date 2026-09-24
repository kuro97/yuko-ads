"""
Интеграционные тесты иерархии источников темпа (pace_source) в run_budget_scaling
(Шаг A.3 плана-гейта v3): engine -> curve -> sheet -> none.

Задача T6 волны 3 спеки docs/specs/ARCH-cdp-budget-context.md (§9, tests/test_pace_hierarchy.py).

Тесты СКВОЗНЫЕ — через run_budget_scaling, границы мокаются:
- services.cdp_client.get_budget_context (движок budget-context)
- services.cdp_client.get_daily_report / get_plan_fact_summary (сезонная кривая A.2,
  через _CascadeMocks — паттерн из tests/test_cdp_unit_economics.py)
- services.plan_reader.read_general_plan + week-функции (лист)

Комментарии на русском.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импортов проекта (как в других тестах бюджет-пилота)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from services.budget_scaler import run_budget_scaling
from services.cdp_client import CdpError

# Переиспользуем инфраструктуру теста T3 (сезонная кривая / каскад CDP-лист):
# _CascadeMocks, _frozen_now, _base_cfg, _VALID_PLAN, _daily_items, _plan_fact_summary.
from tests.test_cdp_unit_economics import (
    _CascadeMocks,
    _frozen_now,
    _base_cfg,
    _VALID_PLAN,
    _plan_fact_summary,
)

_TZ = timezone(timedelta(hours=5))

# now выбран так, чтобы data_as_of (2026-07-03T00:00:00Z) был свежим (8ч < 36ч
# порога _ENGINE_STALE_HOURS) — как рекомендовано §9 спеки.
_NOW = datetime(2026, 7, 3, 13, 0, tzinfo=_TZ)


@pytest.fixture(autouse=True)
def isolated_daily_cap_state(tmp_path, monkeypatch):
    """Перенаправляет state-файл дневного капа во временную папку (изоляция между тестами)."""
    import services.budget_daily_cap as cap_module
    state_file = tmp_path / "budget_daily_cap_state.json"
    monkeypatch.setattr(cap_module, "_CAP_STATE_FILE", state_file)


@pytest.fixture(autouse=True)
def isolated_scale_cooldown_state(tmp_path, monkeypatch):
    """Перенаправляет state-файл кулдауна scaler'а во временную папку (изоляция между тестами)."""
    import services.budget_scaler as scaler_module
    state_file = tmp_path / "budget_scaler_state.json"
    monkeypatch.setattr(scaler_module, "_SCALE_STATE_FILE", state_file)


@pytest.fixture(autouse=True)
def isolated_doubt_log(tmp_path, monkeypatch):
    """Перенаправляет журнал сомнений (data/doubt_log.json) во временную папку.

    ДОБАВЛЕНО вместе с пунктом 1 пакета мелочей (журнал сомнений в «Итоге дня»):
    прогон этого файла реально срабатывал триггерами сомнений и писал в
    продовый data/doubt_log.json — та же изоляция, что уже есть выше для
    budget_daily_cap/scaler state."""
    import services.doubt_log as doubt_log_module
    log_file = tmp_path / "doubt_log.json"
    monkeypatch.setattr(doubt_log_module, "_DOUBT_LOG_FILE", log_file)


@pytest.fixture(autouse=True)
def clear_cdp_cache():
    """Чистит TTL-кеш cdp_client между тестами (иначе get_budget_context может
    вернуть закешированное значение прошлого теста)."""
    import services.cdp_client as cdp_client_module
    cdp_client_module._cache.clear()
    yield
    cdp_client_module._cache.clear()


# ===========================================================================
# Helpers — конструкторы payload budget-context (§6.1 спеки)
# ===========================================================================


def _metric(
    plan=100_000_000.0,
    fact_mtd=6_000_000.0,
    expected_by_today=10_000_000.0,
    pace_vs_expected=0.6,
    forecast_eom=120_000_000.0,
    status="ahead",
    is_cost_metric=False,
) -> dict:
    return {
        "plan": plan,
        "fact_mtd": fact_mtd,
        "expected_by_today": expected_by_today,
        "pace_vs_expected": pace_vs_expected,
        "forecast_eom": forecast_eom,
        "lo": None,
        "hi": None,
        "status": status,
        "is_cost_metric": is_cost_metric,
    }


def _budget_context(
    is_cold_start: bool = False,
    data_as_of: str = "2026-07-03T00:00:00Z",
    revenue_metric: dict | None = None,
    spend_metric: dict | None = None,
    revenue_wape: float = 0.10,
    new_sales_wape: float = 0.10,
) -> dict:
    """Пример из §6.1 спеки по умолчанию: revenue ahead, ad_spend no_plan."""
    if revenue_metric is None:
        revenue_metric = _metric()
    if spend_metric is None:
        spend_metric = _metric(
            plan=None, fact_mtd=1000.0, expected_by_today=None,
            pace_vs_expected=None, forecast_eom=None, status="no_plan",
            is_cost_metric=True,
        )
    return {
        "data_as_of": data_as_of,
        "month": "2026-07",
        "is_cold_start": is_cold_start,
        "month_progress": {"day": 3, "days_in_month": 31, "expected_share": 0.0882},
        "engine_accuracy": [
            {"metric": "revenue_new", "target_month": "2026-06", "wape": revenue_wape},
            {"metric": "new_sales", "target_month": "2026-06", "wape": new_sales_wape},
        ],
        "cities": [
            {
                "city": "_total",
                "metrics": {
                    "revenue_new": revenue_metric,
                    "ad_spend": spend_metric,
                },
            },
        ],
        "next_months": [],
        "semantics": {},
    }


# CDP-плановая ветка (сезонная кривая) с большим запасом — по умолчанию проходит
# ДРР-гейт и сезонный план-гейт, чтобы движок был единственной точкой блокировки.
_LOOSE_CDP_PLAN_FACT = _plan_fact_summary(time_pct=0.1, plan_rev=1_000_000.0, fact_rev=200_000.0)


def _run(cfg_overrides=None, engine_ctx=None, engine_side_effect=None, cascade_kwargs=None, mode="active"):
    """Единая точка запуска: собирает cfg с cdp.enabled=true (если не переопределено),
    мокает get_budget_context и прогоняет run_budget_scaling через _CascadeMocks."""
    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True, "pace_engine": True, "doubt_alerts": True})
    if cfg_overrides:
        cfg.update(cfg_overrides)

    kwargs = dict(cdp_ok=True, sheet_ok=True, cdp_plan_fact=_LOOSE_CDP_PLAN_FACT)
    if cascade_kwargs:
        kwargs.update(cascade_kwargs)

    budget_ctx_patch_kwargs = {}
    if engine_side_effect is not None:
        budget_ctx_patch_kwargs["side_effect"] = engine_side_effect
    else:
        budget_ctx_patch_kwargs["return_value"] = engine_ctx if engine_ctx is not None else _budget_context()

    with _CascadeMocks(cfg, **kwargs) as m, \
         patch("services.cdp_client.get_budget_context", **budget_ctx_patch_kwargs) as mock_engine, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode=mode)
    return result, m, mock_engine


# ===========================================================================
# 1. pace_engine=true + движок доверенный -> pace_source="engine", телеметрия заполнена
# ===========================================================================


def test_pace_source_engine_when_trusted():
    """Движок доверен и вернул валидный payload -> pace_source="engine",
    forecast_eom/pace_vs_expected/engine_wape заполнены (не None)."""
    result, _, mock_engine = _run()

    assert result["pace_source"] == "engine"
    assert result["forecast_eom"] == pytest.approx(120_000_000.0)
    assert result["pace_vs_expected"] == pytest.approx(0.6)
    assert result["engine_wape"] == pytest.approx(0.10)
    mock_engine.assert_called()


# ===========================================================================
# 2. pace_engine=false -> движок НЕ дёргается, pace_source="curve"
# ===========================================================================


def test_pace_engine_flag_off_skips_engine():
    """cdp.pace_engine=false -> get_budget_context вообще не вызван, pace_source="curve"."""
    result, _, mock_engine = _run(cfg_overrides={"cdp": {"enabled": True, "pace_engine": False}})

    assert result["pace_source"] == "curve"
    mock_engine.assert_not_called()
    assert result["forecast_eom"] is None
    assert result["pace_vs_expected"] is None
    assert result["engine_wape"] is None


# ===========================================================================
# 3. pace_engine=true, движок недоверенный (cold_start) -> pace_source="curve",
#    сомнение-триггер (д) в telegram-моке
# ===========================================================================


def test_pace_source_curve_when_engine_distrusted():
    """cold_start=true -> compute_engine_pace вернул None -> pace_source="curve",
    прогон не упал."""
    result, _, mock_engine = _run(engine_ctx=_budget_context(is_cold_start=True))

    assert result["pace_source"] == "curve"
    assert result["ran"] is True
    mock_engine.assert_called()


def test_doubt_trigger_engine_distrust():
    """Движок cold_start, doubt_alerts=true -> триггер (д) содержит "сезонной кривой".
    РЕДИЗАЙН: отдельное сообщение "есть сомнение"
    больше не отправляется — текст триггера приклеен к ЕДИНСТВЕННОМУ сообщению о
    результате прогона (send_telegram ИЛИ send_with_buttons, смотря что реально
    ушло), поэтому проверяем оба канала и маркер "Почему сомневаюсь"."""
    result, m, _ = _run(
        engine_ctx=_budget_context(is_cold_start=True),
        cfg_overrides={"cdp": {"enabled": True, "pace_engine": True, "doubt_alerts": True}},
    )

    assert result["pace_source"] == "curve"
    all_texts = [
        (c.args[0] if c.args else c.kwargs.get("text", ""))
        for c in m["send_telegram"].call_args_list + m["send_with_buttons"].call_args_list
    ]
    doubt_calls = [t for t in all_texts if "Почему сомневаюсь" in t]
    assert doubt_calls, "ожидалась секция «Почему сомневаюсь» в отправленном сообщении"
    assert "сезонной кривой" in doubt_calls[0]


# ===========================================================================
# 4. CDP целиком лёг (daily-report CdpError) -> pace_source="sheet", движок не важен
# ===========================================================================


def test_pace_source_sheet_when_cdp_disabled():
    """cdp.enabled=false -> unit_source="sheet", pace_source="sheet" (движок не дёргается
    к листовому пути — §6.7)."""
    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": False})
    with _CascadeMocks(cfg, cdp_ok=True, sheet_ok=True) as _, \
         patch("services.cdp_client.get_budget_context", return_value=_budget_context()) as mock_engine, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "sheet"
    assert result["pace_source"] == "sheet"
    mock_engine.assert_not_called()


def test_pace_source_curve_when_engine_error():
    """cdp.enabled=true, get_budget_context бросает CdpError -> compute_engine_pace
    ловит и возвращает None -> pace_source="curve", ran корректен (прогон не упал)."""
    result, _, mock_engine = _run(engine_side_effect=CdpError("budget-context недоступен"))

    assert result["pace_source"] == "curve"
    assert result["ran"] is True
    mock_engine.assert_called()


# ===========================================================================
# 5. Всё лежит -> pace_source="none", масштабирования нет (fail-closed)
# ===========================================================================


def test_pace_source_none_when_everything_down():
    """cdp.enabled=true, CDP-кривая недоступна (daily-report err), лист тоже
    недоступен -> unit_source="none", pace_source="none", fail-closed блок,
    масштабирования нет."""
    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True, "pace_engine": True})
    with _CascadeMocks(cfg, cdp_ok=False, sheet_ok=False) as m, \
         patch("services.cdp_client.get_budget_context", side_effect=CdpError("недоступен")), \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "none"
    assert result["pace_source"] == "none"
    assert result["scaled"] == []
    assert "fail-closed" in (result["skipped_reason"] or "")
    m["set_adset_budget"].assert_not_called()


# ===========================================================================
# 6. Приоритет блокировок: ДРР-провал блокирует ДО тормоза перерасхода и до
#    темпа (ДРР-гейт независим)
# ===========================================================================


def test_ddr_gate_still_independent():
    """Движок ahead (темп бы прошёл), но cdp_ue.drr > unit_target -> блок
    "юнитка превышена" (ДРР-гейт главнее темпа движка)."""
    # unit_target листа = 0.01 (1%), а drr_cdp из _daily_items() = 0.04835 (4.8%) > target
    tight_plan = {**_VALID_PLAN, "unit_target": 0.01}
    result, m, mock_engine = _run(
        engine_ctx=_budget_context(),  # ahead, доверенный
        cascade_kwargs={"sheet_plan": tight_plan},
    )

    assert result["unit_source"] == "cdp"
    assert result["pace_source"] == "engine"  # движок доверенный и посчитан
    assert result["scaled"] == []
    assert "юнитка" in (result["skipped_reason"] or "")
    m["set_adset_budget"].assert_not_called()


# ===========================================================================
# 7. Телеметрия pace_source присутствует в ранних ветках (kill_switch -> "none")
# ===========================================================================


def test_telemetry_none_in_early_returns():
    """cfg['kill_switch']=True (ранний return до расчёта юнитки) -> pace_source="none",
    engine-поля телеметрии None, get_budget_context не вызывался (гейт отработал раньше)."""
    cfg = _base_cfg(kill_switch=True, cdp={"enabled": True, "pace_engine": True})
    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.cdp_client.get_budget_context") as mock_engine:
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is False
    assert result["skipped_reason"] == "kill_switch"
    assert result["pace_source"] == "none"
    assert result["forecast_eom"] is None
    assert result["pace_vs_expected"] is None
    assert result["engine_wape"] is None
    mock_engine.assert_not_called()


def test_telemetry_none_in_disabled_early_return():
    """cfg['enabled']=False (ранний return) -> pace_source="none", все engine-поля None."""
    cfg = _base_cfg(enabled=False)
    with patch("services.budget_scaler.get_scale_config", return_value=cfg):
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is False
    assert result["pace_source"] == "none"
    assert result["forecast_eom"] is None
    assert result["pace_vs_expected"] is None
    assert result["engine_wape"] is None


def test_telemetry_engine_in_result():
    """Движок доверенный -> forecast_eom/pace_vs_expected/engine_wape не None в result
    (дубль сценария 1, но проверяет отдельно от pace_source — контракт §6.8)."""
    result, _, _ = _run()

    for key in ("forecast_eom", "pace_vs_expected", "engine_wape"):
        assert result[key] is not None, f"{key} не должен быть None при доверенном движке"


def test_existing_a2_telemetry_preserved():
    """A.2-ключи телеметрии (unit_source/plan_gate_mode/drr_cdp/expected_share/fact_share)
    присутствуют в результате наравне с новыми A.3-ключами."""
    result, _, _ = _run()

    for key in ("unit_source", "drr_cdp", "drr_sheet", "drr_divergence_pp",
                "plan_gate_mode", "expected_share", "fact_share"):
        assert key in result
    for key in ("pace_source", "forecast_eom", "pace_vs_expected", "engine_wape"):
        assert key in result


# ===========================================================================
# 8. Порядок: тормоз перерасхода блокирует при revenue ahead (сквозной)
# ===========================================================================


def test_overspend_brake_blocks_even_if_revenue_ahead():
    """revenue ahead (план-темп бы прошёл), но ad_spend.status="behind" (plan!=null)
    -> блок с "перерасход" в skipped_reason, движок доверенный (pace_source="engine")."""
    spend_behind = _metric(
        plan=50_000.0, fact_mtd=60_000.0, expected_by_today=45_000.0,
        pace_vs_expected=1.3, forecast_eom=70_000.0, status="behind",
        is_cost_metric=True,
    )
    result, m, _ = _run(engine_ctx=_budget_context(spend_metric=spend_behind))

    assert result["unit_source"] == "cdp"
    assert result["pace_source"] == "engine"
    assert result["scaled"] == []
    assert "перерасход" in (result["skipped_reason"] or "")
    m["set_adset_budget"].assert_not_called()


def test_overspend_no_plan_does_not_block():
    """ad_spend.status="no_plan" (plan=null, пример по умолчанию) -> тормоз
    перерасхода НЕ блокирует; движок доверенный, revenue ahead -> план-гейт проходит."""
    result, m, _ = _run()  # дефолтный _budget_context(): ad_spend status="no_plan"

    assert result["pace_source"] == "engine"
    assert "перерасход" not in (result["skipped_reason"] or "")
    # план-гейт по темпу тоже не заблокировал (ahead) — прогон должен либо
    # смасштабировать, либо упереться в другой гейт (headroom/candidates), но
    # НЕ в plan_gate по темпу/перерасходу движка.
    assert not (result["skipped_reason"] or "").startswith("plan_gate:") or (
        "перерасход" not in (result["skipped_reason"] or "")
        and "прогноз" not in (result["skipped_reason"] or "")
    )


# ===========================================================================
# Дополнительно: план-гейт движка блокирует / разрешает масштабирование
# (не входит в явные 8 пронумерованных сценариев, но требуется §9 таблицей
# tests/test_pace_hierarchy.py — test_engine_plan_gate_blocks_scaling /
# test_engine_live_case_allows_scaling)
# ===========================================================================


def test_engine_plan_gate_blocks_scaling():
    """Движок доверенный, revenue status=behind + forecast miss + pace low ->
    skipped_reason содержит "прогноз", подъёмов 0."""
    revenue_behind = _metric(
        plan=100_000_000.0, fact_mtd=6_000_000.0, expected_by_today=10_000_000.0,
        pace_vs_expected=0.5, forecast_eom=70_000_000.0, status="behind",
    )
    result, m, _ = _run(engine_ctx=_budget_context(revenue_metric=revenue_behind))

    assert result["pace_source"] == "engine"
    assert result["scaled"] == []
    assert "прогноз" in (result["skipped_reason"] or "")
    m["set_adset_budget"].assert_not_called()


def test_engine_live_case_allows_scaling():
    """Движок доверенный, пример по умолчанию (ahead) -> план-гейт по темпу НЕ блокирует
    (другие гейты могут блокировать, но не по темпу/перерасходу движка)."""
    result, _, _ = _run()  # пример по умолчанию: revenue ahead, ad_spend no_plan

    assert result["pace_source"] == "engine"
    reason = result["skipped_reason"] or ""
    assert "прогноз" not in reason
    assert "перерасход" not in reason


# ===========================================================================
# Триггер (е): выручка отстаёт при спенде в норме — не блокирует, только сообщение
# ===========================================================================


def test_doubt_trigger_revenue_behind_spend_ok():
    """revenue behind, spend on_track, движок доверенный -> триггер (е) содержит
    "проверь каналы", решение НЕ меняется автоматическим перекладыванием (только
    текст). РЕДИЗАЙН: отдельное сообщение "есть
    сомнение" больше не отправляется — текст триггера приклеен к ЕДИНСТВЕННОМУ
    сообщению о результате прогона (send_telegram ИЛИ send_with_buttons)."""
    revenue_behind_ok = _metric(
        plan=100_000_000.0, fact_mtd=6_000_000.0, expected_by_today=10_000_000.0,
        pace_vs_expected=0.95, forecast_eom=100_000_000.0, status="behind",
    )
    spend_on_track = _metric(
        plan=50_000.0, fact_mtd=20_000.0, expected_by_today=20_000.0,
        pace_vs_expected=1.0, forecast_eom=48_000.0, status="on_track",
        is_cost_metric=True,
    )
    result, m, _ = _run(engine_ctx=_budget_context(
        revenue_metric=revenue_behind_ok, spend_metric=spend_on_track,
    ))

    assert result["pace_source"] == "engine"
    all_texts = [
        (c.args[0] if c.args else c.kwargs.get("text", ""))
        for c in m["send_telegram"].call_args_list + m["send_with_buttons"].call_args_list
    ]
    doubt_calls = [t for t in all_texts if "Почему сомневаюсь" in t]
    assert doubt_calls, "ожидалась секция «Почему сомневаюсь» в отправленном сообщении"
    assert "проверь каналы" in doubt_calls[0]


def test_doubt_non_blocking_still_holds():
    """Триггеры (д)/(е) есть, но doubt_alerts=false -> результат прогона (кроме
    факта отправки в Telegram) идентичен прогону с doubt_alerts=true — протокол
    сомнений НЕ влияет на решение."""
    engine_ctx = _budget_context(is_cold_start=True)  # триггер (д)

    # mode="dry_run": не мутирует дневной кап/кулдаун между двумя вызовами внутри
    # одного теста (active-режим списывает дневной лимит адсета и второй прогон
    # видел бы состояние первого — не то, что здесь проверяется).
    result_on, _, _ = _run(
        engine_ctx=engine_ctx,
        cfg_overrides={"cdp": {"enabled": True, "pace_engine": True, "doubt_alerts": True}},
        mode="dry_run",
    )
    result_off, _, _ = _run(
        engine_ctx=engine_ctx,
        cfg_overrides={"cdp": {"enabled": True, "pace_engine": True, "doubt_alerts": False}},
        mode="dry_run",
    )

    # Сравниваем решающие поля — они не должны зависеть от doubt_alerts.
    decision_keys = (
        "ran", "skipped_reason", "unit_source", "pace_source",
        "scaled", "winners", "drr_cdp", "drr_sheet",
    )
    for key in decision_keys:
        assert result_on[key] == result_off[key], f"поле {key} отличается при doubt_alerts on/off"
