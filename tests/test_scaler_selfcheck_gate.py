"""
Интеграционные тесты fail-closed гейта самопроверки движка (Wave 3C) —
services.budget_scaler.run_budget_scaling с реальным services.engine_selfcheck.

Инвариант (fail-closed): когда гейт активен (cdp.enabled=true И
scaler_v2.engine_selfcheck_enabled=true, дефолт true), active-подъём разрешён
ТОЛЬКО при валидной самопроверке (status="ok": валидная история 6-9 дней +
отклонение факта в норме). ЛЮБОЙ иной исход самопроверки (недоступность CDP,
неполный ответ, ошибка сохранения, битый state, прогрев, ошибка сверки/сезонной
кривой, сильное отставание/недоверие) → active-подъём заблокирован, FB не трогаем.

Гейт — предохранитель ПОВЕРХ инвариантов Фазы 2 (строгое вето слива) и Wave 3A
(честное 7d-окно): он их не ослабляет. Валидная самопроверка не меняет прочие
plan/unit/money-гейты.

Совместимость: cdp.enabled=false ИЛИ engine_selfcheck_enabled=false → гейт
неактивен, поведение как раньше.

Approval-first: «подъём разрешён» = создано предложение владельцу (propose_scale
вызван, result["proposals"] непустой), а не мутация FB — она происходит только
в execution boundary после одобрения. Поэтому гейт проверяется парой инвариантов:
предложение создано/не создано И прямой FB-мутации не было ни в одной ветке
(автоюз-гард на каждом тесте файла).

Внешние границы (FB/Telegram/decisions/plan/CDP) мокаются. Реальной сети нет.
Состояние самопроверки/капа/кулдауна изолировано в tmp_path.
"""

import json
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

import services.engine_selfcheck as esc
from tests.gateway_test_helpers import (
    install_provider_mutation_guard,
    proposal_outcome,
)

_TZ = timezone(timedelta(hours=5))
# День 16 — сезонная доля > 0 (pacing_curve[16]=0.36); «неделю назад» 09 (0.15).
_NOW = datetime(2026, 7, 16, 13, 0, tzinfo=_TZ)

_VALID_PLAN = {
    "week_label": "неделя 3 (2026-07-15–21)",
    "budget_usd": 100_000.0,
    "revenue_plan_lcy": 10_000_000.0,
    "unit_target": 0.99,  # мягкая юнитка — план-гейт пропускает
}

# CDP-ответ budget-context для record_snapshot (валидный полный агрегат).
def _ctx(forecast_eom=18_000_000.0, fact_mtd=6_000_000.0, month="2026-07"):
    return {
        "month": month,
        "cities": [
            {"city": "_total", "metrics": {
                "revenue_new": {"forecast_eom": forecast_eom, "fact_mtd": fact_mtd},
            }},
        ],
    }


# --- Сиды истории самопроверки (относительно _NOW=2026-07-16) ---

def _ok_history():
    """Валидная история: отклонение факта в норме → status='ok'."""
    return [
        {"date": "2026-07-09", "month": "2026-07", "forecast_eom": 20_000_000.0, "fact_mtd": 4_000_000.0},
        {"date": "2026-07-16", "month": "2026-07", "forecast_eom": 18_000_000.0, "fact_mtd": 6_800_000.0},
    ]


def _distrust_history():
    """Сильное отставание факта → status='distrust' (недоверие движку)."""
    return [
        {"date": "2026-07-09", "month": "2026-07", "forecast_eom": 20_000_000.0, "fact_mtd": 4_000_000.0},
        {"date": "2026-07-16", "month": "2026-07", "forecast_eom": 18_000_000.0, "fact_mtd": 5_400_000.0},
    ]


def _today_only_history():
    """Только сегодняшний валидный снапшот, истории 6-9 дней нет → status='warming'."""
    return [
        {"date": "2026-07-16", "month": "2026-07", "forecast_eom": 18_000_000.0, "fact_mtd": 6_800_000.0},
    ]


def _make_local_ad(ad_id="ad1", payments=3, days_running=10, spend=80.0, outcomes_matched_at=None):
    return {
        "ad_id": ad_id, "ad_name": "Победитель", "city": "CityA", "adset_type": "L2",
        "adset_id": None, "spend": spend, "leads": 8, "qual_pct": 20.0, "romi": 150.0,
        "cpl": 10.0, "ctr": 2.0, "hook_rate": 30.0, "impressions": 5000,
        "video_p25": 0, "video_p100": 0, "video_views_3s": 0,
        "payments": payments, "outcomes_matched_at": outcomes_matched_at,
        "days_running": days_running, "effective_status": "ACTIVE",
        "recommendation": "ЖДАТЬ", "reason": "",
    }


def _adset_info(budget_usd=100.0, status="ACTIVE", name="Адсет"):
    return {"daily_budget_usd": budget_usd, "effective_status": status, "name": name}


def _base_cfg(**overrides):
    cfg = {
        "enabled": True, "kill_switch": False, "scale_enabled": True,
        "max_budget_increase_pct": 15, "max_adset_budget_mult": 2.0,
        "max_adset_daily_budget": 300, "max_total_daily_budget": 4000,
        "max_scales_per_run": 2, "plan_sheet_id": "test_sheet_id",
        # Гейт самопроверки активен: CDP включён, движок темпа выключен (чтобы
        # get_budget_context дёргал ТОЛЬКО record_snapshot — изоляция мока).
        "cdp": {"enabled": True, "pace_engine": False, "payments_source": "shadow"},
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    import services.budget_daily_cap as cap_module
    import services.budget_scaler as scaler_module
    monkeypatch.setattr(cap_module, "_CAP_STATE_FILE", tmp_path / "cap.json")
    monkeypatch.setattr(scaler_module, "_SCALE_STATE_FILE", tmp_path / "scale.json")
    monkeypatch.setattr(esc, "_SELFCHECK_STATE_FILE", tmp_path / "selfcheck.json")


@pytest.fixture(autouse=True)
def no_direct_fb_mutation(monkeypatch):
    """Прямая мутация FB из скейлера запрещена в ЛЮБОМ исходе самопроверки.

    Гард на каждом тесте: и когда гейт блокирует (FB трогать нельзя), и когда
    пропускает (подъём обязан уйти предложением, а не мутацией)."""
    return install_provider_mutation_guard(monkeypatch)


def _seed(snapshots):
    esc._SELFCHECK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    esc._SELFCHECK_STATE_FILE.write_text(json.dumps({"snapshots": snapshots}), encoding="utf-8")


class _ScalingMocks:
    def __init__(self, local_ads, all_budgets, cfg, fb_ad_info=None):
        if fb_ad_info is None:
            fb_ad_info = {ad["ad_id"]: {"adset_id": "adset1", "effective_status": "ACTIVE"} for ad in local_ads}

        def _fetch_fb_info(ad_ids):
            return {aid: info for aid, info in fb_ad_info.items() if aid in ad_ids}

        self._stack = ExitStack()
        self._specs = {
            "get_scale_config": ("services.budget_scaler.get_scale_config", {"return_value": cfg}),
            "fetch_ads": ("services.shadow_report._fetch_ads_from_local_db", {"return_value": local_ads}),
            "load_settings": ("agent.scheduler.load_settings", {"return_value": {"thresholds": {}}}),
            "fetch_candidate_fb_info": ("services.autopilot._fetch_candidate_fb_info", {"side_effect": _fetch_fb_info}),
            "fetch_all_budgets": ("services.budget_scaler._fetch_all_account_adset_budgets", {"return_value": all_budgets}),
            "fetch_adset_budgets": ("services.budget_scaler._fetch_adset_budgets", {"return_value": {}}),
            # Producer-граница: боевой код импортирует propose_scale локально,
            # поэтому патч legacy-алиаса execute_scale ничего не перехватывал бы
            # (мок молчал, а прогон уходил в настоящего producer'а).
            "propose_scale": (
                "services.action_producer_gateway.propose_scale",
                {"return_value": proposal_outcome("prop-1")},
            ),
            "send_telegram": ("services.notifications.send_telegram", {}),
            "send_critical_alert": ("services.notifications.send_critical_alert", {}),
            "save_decision": ("agent.repositories.decisions_repo.save_decision", {}),
            "send_with_buttons": ("services.telegram_bot.send_with_buttons", {"return_value": True}),
            "read_general_plan": ("services.plan_reader.read_general_plan", {"return_value": _VALID_PLAN}),
            "get_usd_to_lcy": ("services.exchange_rate.get_usd_to_lcy", {"return_value": 100.0}),
            "get_fb_week_spend": ("services.budget_scaler.get_fb_week_spend", {"return_value": 0.0}),
            "get_google_week_spend": ("services.budget_scaler.get_google_week_spend", {"return_value": 0.0}),
            "get_amo_week_revenue": ("services.budget_scaler.get_amo_week_revenue", {"return_value": 200_000.0}),
        }
        self.mocks = {}

    def __enter__(self):
        for name, (target, kwargs) in self._specs.items():
            self.mocks[name] = self._stack.enter_context(patch(target, **kwargs))
        return self

    def __exit__(self, *a):
        return self._stack.__exit__(*a)

    def __getitem__(self, name):
        return self.mocks[name]


def _frozen_now(now=_NOW):
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = now
    return patch("services.budget_scaler.datetime", mock_dt)


_SENTINEL = object()


def _run(cfg, local_ads, mode="active", ctx_kwargs=None, ctx_side_effect=None,
         ctx_raw=_SENTINEL, save_side_effect=None, extra_patches=None, fb_ad_info=None):
    """Прогоняет run_budget_scaling с изоляцией состояния и мокнутым CDP-контекстом.

    Приоритет мока get_budget_context: ctx_side_effect (напр. CdpError) → ctx_raw
    (произвольный сырой ответ, в т.ч. неполный) → _ctx(**ctx_kwargs) (валидный полный).
    """
    from services.budget_scaler import run_budget_scaling
    all_budgets = {"adset1": _adset_info(100.0)}
    if ctx_side_effect is not None:
        ctx_patch = {"side_effect": ctx_side_effect}
    elif ctx_raw is not _SENTINEL:
        ctx_patch = {"return_value": ctx_raw}
    else:
        ctx_patch = {"return_value": _ctx(**(ctx_kwargs or {}))}
    with ExitStack() as stack:
        m = stack.enter_context(_ScalingMocks(local_ads, all_budgets, cfg, fb_ad_info=fb_ad_info))
        stack.enter_context(patch("services.cdp_client.get_budget_context", **ctx_patch))
        stack.enter_context(patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)))
        stack.enter_context(patch("services.budget_scaler._record_scaled_at"))
        stack.enter_context(_frozen_now())
        if save_side_effect is not None:
            stack.enter_context(patch.object(esc, "_save_state", side_effect=save_side_effect))
        for p in (extra_patches or []):
            stack.enter_context(p)
        result = run_budget_scaling(mode=mode)
    return result, m


# ===========================================================================
# Happy path: валидная самопроверка → гейт проходит
# ===========================================================================

def test_valid_history_ok_allows_active_raise():
    """Валидная история + отклонение в норме → self-check пропускает, подъём
    доходит до предложения владельцу (сам FB при этом не тронут)."""
    _seed(_ok_history())
    result, m = _run(_base_cfg(), [_make_local_ad("ad1")])

    m["propose_scale"].assert_called_once()
    assert result["proposals"] == ["prop-1"]
    assert result["scaled"] == []
    assert result["selfcheck_status"] == "ok"


def test_valid_ok_does_not_override_waster_veto():
    """Валидная самопроверка НЕ ослабляет строгое вето слива: адсет со значимым
    confirmed_waster исключён даже при status='ok'."""
    _seed(_ok_history())
    local_ads = [
        _make_local_ad("ad_win", payments=3),
        {**_make_local_ad("ad_waste", payments=0, spend=80.0),
         "outcomes_matched_at": "2026-07-01T10:00:00"},
    ]
    fb_ad_info = {
        "ad_win": {"adset_id": "adset1", "effective_status": "ACTIVE"},
        "ad_waste": {"adset_id": "adset1", "effective_status": "ACTIVE"},
    }
    result, m = _run(_base_cfg(), local_ads, fb_ad_info=fb_ad_info)

    m["propose_scale"].assert_not_called()
    assert result["scaled"] == []


# ===========================================================================
# Fail-closed: каждый режим сбоя самопроверки блокирует active-подъём
# ===========================================================================

def test_warming_no_history_blocks_active_raise():
    """Недостаточно истории 6-9 дней (только сегодня) → warming → подъём заблокирован."""
    _seed(_today_only_history())
    result, m = _run(_base_cfg(), [_make_local_ad("ad1")])

    m["propose_scale"].assert_not_called()
    assert result["scaled"] == []
    assert result["skipped_reason"] == "selfcheck_warming"
    assert result["ran"] is True


def test_fetch_error_blocks_active_raise():
    """CDP лёг (нет свежего снапшота) → unavailable → подъём заблокирован."""
    # Истории нет и снапшот не записать (CdpError) → unavailable.
    result, m = _run(_base_cfg(), [_make_local_ad("ad1")], ctx_side_effect=esc.CdpError("down"))

    m["propose_scale"].assert_not_called()
    assert result["scaled"] == []
    assert result["skipped_reason"] == "selfcheck_unavailable"


def test_save_failure_blocks_active_raise():
    """Ошибка сохранения снапшота → блок, propose_scale НЕ вызван."""
    result, m = _run(
        _base_cfg(), [_make_local_ad("ad1")],
        save_side_effect=OSError("disk full"),
    )

    m["propose_scale"].assert_not_called()
    assert result["scaled"] == []
    assert result["skipped_reason"] == "selfcheck_unavailable"


def test_corrupt_state_blocks_active_raise():
    """Битый state НЕ трактуется как чистый новый → invalid → подъём заблокирован."""
    esc._SELFCHECK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    esc._SELFCHECK_STATE_FILE.write_text("{ битый json", encoding="utf-8")
    result, m = _run(_base_cfg(), [_make_local_ad("ad1")])

    m["propose_scale"].assert_not_called()
    assert result["scaled"] == []
    assert result["skipped_reason"] == "selfcheck_invalid"


def test_partial_cdp_response_blocks_active_raise():
    """Неполный ответ CDP (нет revenue_new._total) → invalid снапшот → блок."""
    result, m = _run(
        _base_cfg(), [_make_local_ad("ad1")],
        ctx_raw={"month": "2026-07", "cities": []},  # нет revenue_new._total
    )

    m["propose_scale"].assert_not_called()
    assert result["scaled"] == []
    assert result["skipped_reason"] == "selfcheck_invalid"


def test_pacing_error_blocks_active_raise():
    """Ошибка сезонной кривой во время сверки → unavailable → блок."""
    _seed(_ok_history())
    result, m = _run(
        _base_cfg(), [_make_local_ad("ad1")],
        extra_patches=[patch("services.pacing_curve.expected_cumulative_share",
                             side_effect=RuntimeError("boom"))],
    )

    m["propose_scale"].assert_not_called()
    assert result["scaled"] == []
    assert result["skipped_reason"] == "selfcheck_unavailable"


def test_strong_lag_distrust_blocks_and_engine_off():
    """Сильное отставание → engine path off (pace_source не engine) И подъём не проходит."""
    _seed(_distrust_history())
    result, m = _run(_base_cfg(), [_make_local_ad("ad1")])

    m["propose_scale"].assert_not_called()
    assert result["scaled"] == []
    assert result["skipped_reason"] == "selfcheck_distrust"
    # Недоверие гасит движок: источник темпа НЕ "engine".
    assert result["pace_source"] != "engine"


# ===========================================================================
# Dry-run и совместимость
# ===========================================================================

def test_dry_run_not_blocked_by_selfcheck_gate():
    """Гейт — только для active. dry_run без валидной истории всё равно даёт
    рекомендации без реальных мутаций FB."""
    cfg = _base_cfg(scale_enabled=False)  # active→dry_run
    _seed(_today_only_history())  # warming
    result, m = _run(cfg, [_make_local_ad("ad1")], mode="dry_run")

    m["propose_scale"].assert_not_called()
    assert len(result["recommendations"]) == 1
    assert result["skipped_reason"] != "selfcheck_warming"


def test_gate_inactive_when_selfcheck_disabled_raise_proceeds():
    """engine_selfcheck_enabled=false → гейт неактивен, предложение создаётся
    даже без истории самопроверки (обратная совместимость)."""
    cfg = _base_cfg(scaler_v2={"engine_selfcheck_enabled": False})
    result, m = _run(cfg, [_make_local_ad("ad1")], ctx_side_effect=esc.CdpError("down"))

    m["propose_scale"].assert_called_once()
    assert result["proposals"] == ["prop-1"]
    assert result["scaled"] == []


def test_gate_inactive_when_cdp_disabled_raise_proceeds():
    """cdp.enabled=false → гейт самопроверки неактивен, предложение создаётся
    без истории самопроверки."""
    cfg = _base_cfg(cdp={"enabled": False})
    result, m = _run(cfg, [_make_local_ad("ad1")], ctx_side_effect=esc.CdpError("down"))

    m["propose_scale"].assert_called_once()
    assert result["proposals"] == ["prop-1"]
    assert result["scaled"] == []
