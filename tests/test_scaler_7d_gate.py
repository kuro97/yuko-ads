"""
Тесты честного 7d-гейта скейлера (Wave 3A) — services.budget_scaler.run_budget_scaling
с флагом autopilot.scaler_v2.require_fresh_7d.

Инвариант (fail-closed): при require_fresh_7d=True active-подъём разрешён ТОЛЬКО
если source-specific честное 7d-окно оплат подтверждено (свежий полный refresh).
Stale/partial/unknown → никакого active raise (FB не трогаем). Дефолт False —
поведение по умолчанию не меняется. Гейт — предохранитель ПОВЕРХ инвариантов
Фазы 2 (строгое вето слива остаётся строгим и не ослабляется этим гейтом).

Approval-first: «подъём прошёл» здесь означает «создано предложение владельцу»
(propose_scale вызван, result["proposals"] непустой). Сам бюджет FB меняется
только в execution boundary после одобрения, поэтому во всех тестах файла
автоюз-гард доказывает вторую половину инварианта — прямой FB-мутации не было
ни в разрешающей, ни в блокирующей ветке.

Внешние границы (FB/Telegram/decisions/plan) мокаются. Реальной сети нет.
"""

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tests.gateway_test_helpers import (
    install_provider_mutation_guard,
    proposal_outcome,
)

sys.path.insert(0, str(Path(__file__).parent.parent))

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from services import cdp_payments as cp

_VALID_PLAN = {
    "week_label": "неделя 1 (2026-07-01–07)",
    "budget_usd": 100_000.0,
    "revenue_plan_lcy": 10_000_000.0,
    "unit_target": 0.99,
}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    import services.budget_daily_cap as cap_module
    import services.budget_scaler as scaler_module
    monkeypatch.setattr(cap_module, "_CAP_STATE_FILE", tmp_path / "cap.json")
    monkeypatch.setattr(scaler_module, "_SCALE_STATE_FILE", tmp_path / "scale.json")


@pytest.fixture(autouse=True)
def no_direct_fb_mutation(monkeypatch):
    """Любая прямая мутация FB из скейлера = провал теста (approval-first).

    Гард стоит на КАЖДОМ тесте файла, а не только на блокирующих: если гейт
    когда-нибудь снова начнёт «сам поднимать» бюджет вместо предложения,
    упадёт и разрешающий сценарий тоже."""
    return install_provider_mutation_guard(monkeypatch)


def _fresh_7d_fields():
    """Свежий полный AMO 7d-снимок относительно ТЕКУЩЕГО окна (реальное now)."""
    win = cp.seven_day_window_local()
    return {
        "payments_amo_7d": 3, "revenue_amo_7d": 100.0,
        "amo_7d_window_from": win["window_from"], "amo_7d_window_to": win["window_to"],
        "amo_7d_synced_at": "2026-07-17T00:00:00", "amo_7d_complete": 1,
    }


def _stale_7d_fields():
    """Полный, но УСТАРЕВШИЙ снимок (окно не совпадает с текущим)."""
    return {
        "payments_amo_7d": 3, "revenue_amo_7d": 100.0,
        "amo_7d_window_from": "2000-01-01", "amo_7d_window_to": "2000-01-08",
        "amo_7d_synced_at": "2000-01-08T00:00:00", "amo_7d_complete": 1,
    }


def _make_local_ad(ad_id="ad1", payments=3, days_running=10, seven_d=None):
    ad = {
        "ad_id": ad_id, "ad_name": "Победитель", "city": "CityA", "adset_type": "L2",
        "adset_id": None, "spend": 80.0, "leads": 8, "qual_pct": 20.0, "romi": 150.0,
        "cpl": 10.0, "ctr": 2.0, "hook_rate": 30.0, "impressions": 5000,
        "video_p25": 0, "video_p100": 0, "video_views_3s": 0,
        "payments": payments, "outcomes_matched_at": None, "days_running": days_running,
        "effective_status": "ACTIVE", "recommendation": "ЖДАТЬ", "reason": "",
        # 7d-поля по умолчанию отсутствуют (None) — «нет подтверждённых данных».
        "payments_amo_7d": None, "amo_7d_complete": None,
        "payments_erp_7d": None, "erp_7d_complete": None,
    }
    if seven_d:
        ad.update(seven_d)
    return ad


def _adset_info(budget_usd=100.0, status="ACTIVE", name="Адсет"):
    return {"daily_budget_usd": budget_usd, "effective_status": status, "name": name}


def _base_cfg(**overrides):
    cfg = {
        "enabled": True, "kill_switch": False, "scale_enabled": False,
        "max_budget_increase_pct": 15, "max_adset_budget_mult": 2.0,
        "max_adset_daily_budget": 300, "max_total_daily_budget": 4000,
        "max_scales_per_run": 2, "plan_sheet_id": "test_sheet_id",
    }
    cfg.update(overrides)
    return cfg


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
            # Producer-граница: боевой код импортирует именно propose_scale —
            # патч legacy-алиаса execute_scale ничего бы не перехватил.
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


def _run(cfg, local_ads, mode="active"):
    from services.budget_scaler import run_budget_scaling
    all_budgets = {"adset1": _adset_info(100.0)}
    with _ScalingMocks(local_ads, all_budgets, cfg) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"):
        result = run_budget_scaling(mode=mode)
    return result, m


# ===========================================================================
# Дефолт (флаг выключен) — поведение не меняется
# ===========================================================================

def test_default_flag_off_active_raise_proceeds_without_7d_data():
    """require_fresh_7d не задан (дефолт False): без 7d-данных подъём доходит до
    предложения владельцу как раньше (обратная совместимость гейта)."""
    cfg = _base_cfg(scale_enabled=True)
    result, m = _run(cfg, [_make_local_ad("ad1")])

    m["propose_scale"].assert_called_once()
    assert result["proposals"] == ["prop-1"]
    # Ничего не поднято прямо сейчас: подъём произойдёт после одобрения.
    assert result["scaled"] == []


# ===========================================================================
# Флаг включён — fail-closed без подтверждённого 7d-окна
# ===========================================================================

def test_flag_on_no_7d_data_blocks_active_raise():
    """require_fresh_7d=True, 7d-данных нет (None) → active-подъём заблокирован,
    set_adset_budget НЕ вызван, skipped_reason='stale_7d_window'."""
    cfg = _base_cfg(scale_enabled=True, scaler_v2={"require_fresh_7d": True})
    result, m = _run(cfg, [_make_local_ad("ad1")])

    m["propose_scale"].assert_not_called()
    assert result["scaled"] == []
    assert result["skipped_reason"] == "stale_7d_window"
    assert result["ran"] is True


def test_flag_on_stale_window_blocks_active_raise():
    """require_fresh_7d=True, снимок полный но окно устаревшее → active заблокирован."""
    cfg = _base_cfg(scale_enabled=True, scaler_v2={"require_fresh_7d": True})
    result, m = _run(cfg, [_make_local_ad("ad1", seven_d=_stale_7d_fields())])

    m["propose_scale"].assert_not_called()
    assert result["skipped_reason"] == "stale_7d_window"


def test_flag_on_fresh_7d_allows_active_raise():
    """require_fresh_7d=True, свежий полный AMO 7d-снимок текущего окна →
    гейт пропускает, предложение владельцу создано."""
    cfg = _base_cfg(scale_enabled=True, scaler_v2={"require_fresh_7d": True})
    result, m = _run(cfg, [_make_local_ad("ad1", seven_d=_fresh_7d_fields())])

    m["propose_scale"].assert_called_once()
    assert result["proposals"] == ["prop-1"]
    assert result["scaled"] == []


def test_flag_on_dry_run_not_blocked_by_7d_gate():
    """Гейт — только для active. dry_run без 7d-данных всё равно даёт рекомендации
    (без реальных мутаций FB)."""
    cfg = _base_cfg(scale_enabled=False, scaler_v2={"require_fresh_7d": True})
    result, m = _run(cfg, [_make_local_ad("ad1")], mode="dry_run")

    m["propose_scale"].assert_not_called()
    assert len(result["recommendations"]) == 1
    assert result["skipped_reason"] != "stale_7d_window"


def test_flag_on_fresh_7d_still_respects_waster_veto():
    """Гейт НЕ ослабляет строгое вето слива: адсет со значимым confirmed_waster
    исключён даже при свежем 7d-окне и включённом флаге."""
    cfg = _base_cfg(scale_enabled=True, scaler_v2={"require_fresh_7d": True})
    local_ads = [
        _make_local_ad("ad_win", payments=3, seven_d=_fresh_7d_fields()),
        # значимый слив: сверено, оплат 0, расход $80 ≥ порога → вето адсета
        {**_make_local_ad("ad_waste", payments=0, seven_d=_fresh_7d_fields()),
         "outcomes_matched_at": "2026-07-01T10:00:00", "spend": 80.0},
    ]
    fb_ad_info = {
        "ad_win": {"adset_id": "adset1", "effective_status": "ACTIVE"},
        "ad_waste": {"adset_id": "adset1", "effective_status": "ACTIVE"},
    }
    from services.budget_scaler import run_budget_scaling
    all_budgets = {"adset1": _adset_info(100.0)}
    with _ScalingMocks(local_ads, all_budgets, cfg, fb_ad_info=fb_ad_info) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"):
        result = run_budget_scaling(mode="active")

    m["propose_scale"].assert_not_called()
    assert result["scaled"] == []
