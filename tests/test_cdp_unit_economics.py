"""
Тесты юнитки CDP Acme в services/budget_scaler.py:
- compute_cdp_unit_economics(now) — формула ДРР (сумма/сумма), план-темп, fail-closed
- гейт-каскад в _run_scaling_inner (§8 спеки, 6 комбинаций fail-closed таблицы)
- shadow-сверка (drr_cdp/drr_sheet/drr_divergence_pp) в результате run_budget_scaling

Задача T5 волны 3 спеки ARCH-cdp-unit-economics.md.

Моки — на границе: services.cdp_client.get_daily_report/get_plan_fact_summary
(клиент CDP) и листовой путь (services.plan_reader.read_general_plan +
get_fb_week_spend/get_google_week_spend/get_amo_week_revenue), по образцу
tests/test_budget_pilot_integration.py (_ScalingMocks, _frozen_now).

Комментарии на русском.
"""

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импортов проекта (как в других тестах бюджет-пилота)
import sys as _sys
if "google.genai" not in _sys.modules:
    _sys.modules["google.genai"] = MagicMock()
if "google" not in _sys.modules:
    _sys.modules["google"] = MagicMock()

from services.budget_scaler import compute_cdp_unit_economics, compute_drr_window
from services.cdp_client import CdpError

_TZ = timezone(timedelta(hours=5))


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
    прогон этого файла срабатывал триггерами сомнений и писал в
    рабочий data/doubt_log.json — та же изоляция, что уже есть выше для
    budget_daily_cap/scaler state."""
    import services.doubt_log as doubt_log_module
    log_file = tmp_path / "doubt_log.json"
    monkeypatch.setattr(doubt_log_module, "_DOUBT_LOG_FILE", log_file)


def _frozen_now(now: datetime):
    """Патчит services.budget_scaler.datetime так, чтобы .now() возвращал фиксированный now,
    а конструктор datetime(...) продолжал работать как обычно (нужен коду внутри модуля).
    """
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = now
    return patch("services.budget_scaler.datetime", mock_dt)


# ===========================================================================
# compute_cdp_unit_economics — формула ДРР, план-темп, fail-closed (юнит)
# ===========================================================================


def _daily_items():
    """2 города × 2 дня — числовой пример из §10.2 (свой, формула зафиксирована спекой).

    CityA 07-01: spend=100 rate=95  revenue=200_000
    CityA 07-02: spend=50  rate=100 revenue=100_000
    CityB 07-01: spend=30  rate=95  revenue=60_000
    CityB 07-02: spend=20  rate=100 revenue=40_000

    spend_lcy = 100*95 + 50*100 + 30*95 + 20*100 = 19_350
    revenue_lcy = 200_000+100_000+60_000+40_000 = 400_000
    drr = 19_350 / 400_000 = 0.048375 (готовые drr_new внутри items — ЛОЖНЫЕ,
    намеренно НЕ совпадают со средним, чтобы тест ловил использование drr_new).
    """
    return [
        {"report_date": "2026-07-01", "city": "CityA", "usd_rate": 95.0,
         "ad_spend": 100.0, "revenue_new": 200_000.0, "drr_new": 999.0},
        {"report_date": "2026-07-02", "city": "CityA", "usd_rate": 100.0,
         "ad_spend": 50.0, "revenue_new": 100_000.0, "drr_new": 999.0},
        {"report_date": "2026-07-01", "city": "CityB", "usd_rate": 95.0,
         "ad_spend": 30.0, "revenue_new": 60_000.0, "drr_new": 999.0},
        {"report_date": "2026-07-02", "city": "CityB", "usd_rate": 100.0,
         "ad_spend": 20.0, "revenue_new": 40_000.0, "drr_new": 999.0},
    ]


_NOW = datetime(2026, 7, 8, 10, 0, tzinfo=_TZ)  # среда, окно ДРР = 2026-06-28..2026-07-04


def _plan_fact_summary(time_pct=0.5, plan_rev=1_000_000.0, fact_rev=600_000.0, cities_override=None):
    if cities_override is not None:
        cities = cities_override
    else:
        cities = [
            {"city": "CityA", "plan": {"revenue_new": plan_rev * 0.6}, "fact": {"revenue_new": fact_rev * 0.6}},
            {"city": "CityB", "plan": {"revenue_new": plan_rev * 0.4}, "fact": {"revenue_new": fact_rev * 0.4}},
        ]
    return {"month": "2026-07-01", "time_pct": time_pct, "days_in_month": 31, "days_elapsed": 15, "cities": cities}


def test_drr_aggregates_sum_not_average():
    """ДРР = Σ(spend·rate)/Σ(revenue) по числовому примеру — НЕ среднее drr_new."""
    with patch("services.cdp_client.get_daily_report", return_value=_daily_items()) as mock_daily, \
         patch("services.cdp_client.get_plan_fact_summary", return_value=_plan_fact_summary()):
        result = compute_cdp_unit_economics(_NOW)

    assert result is not None
    assert result["drr"] == pytest.approx(19_350.0 / 400_000.0)
    assert result["drr"] != pytest.approx(999.0 / 100.0)  # не использовали "среднее" готовых drr_new
    assert result["spend_usd"] == pytest.approx(200.0)  # 100+50+30+20
    assert result["revenue_lcy"] == pytest.approx(400_000.0)

    # Проверяем, что get_daily_report вызван именно с границами compute_drr_window(now)
    window_start, window_end, _ = compute_drr_window(_NOW)
    mock_daily.assert_called_once_with(window_start.date(), window_end.date())


def test_drr_zero_revenue_returns_none_drr():
    """Вся revenue_new=0 в окне — drr=None (ДРР неизвестен), но result не None."""
    items = [
        {"report_date": "2026-07-01", "city": "CityA", "usd_rate": 95.0,
         "ad_spend": 100.0, "revenue_new": 0.0, "drr_new": 0.0},
    ]
    with patch("services.cdp_client.get_daily_report", return_value=items), \
         patch("services.cdp_client.get_plan_fact_summary", return_value=_plan_fact_summary()):
        result = compute_cdp_unit_economics(_NOW)

    assert result is not None
    assert result["drr"] is None
    assert result["revenue_lcy"] == pytest.approx(0.0)


def test_drr_positive_revenue_zero_spend_returns_zero_drr():
    """revenue>0, spend=0 — drr=0.0 (валидно, не None, не блок)."""
    items = [
        {"report_date": "2026-07-01", "city": "CityA", "usd_rate": 95.0,
         "ad_spend": 0.0, "revenue_new": 100_000.0, "drr_new": 0.0},
    ]
    with patch("services.cdp_client.get_daily_report", return_value=items), \
         patch("services.cdp_client.get_plan_fact_summary", return_value=_plan_fact_summary()):
        result = compute_cdp_unit_economics(_NOW)

    assert result is not None
    assert result["drr"] == pytest.approx(0.0)


# ===========================================================================
# Сезонный план-гейт (ARCH-cdp-seasonal-pacing §6.2): fact_share >= expected_share*1.0
# пропускает, иначе блокирует. Формула заменена по спеке ARCH-cdp-seasonal-pacing
# (была fact_pace >= time_pct*0.9) — используем ЛОКАЛЬНЫЙ now внутри теста
# (НЕ переопределяем модульную _NOW=2026-07-08, она нужна другим 26 тестам файла
# для ДРР-окна/каскада). now=2026-07-15 -> expected_share=0.33 (PACING_CURVE[15]).
# ===========================================================================


def test_plan_pace_ok():
    """fact_share >= expected_share*1.0 -> plan_ok=True, plan_reason=None."""
    now = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ)  # expected_share = PACING_CURVE[15] = 0.33
    # plan_rev=1_000_000, fact_rev=350_000 -> fact_share=0.35 >= 0.33
    with patch("services.cdp_client.get_daily_report", return_value=_daily_items()), \
         patch("services.cdp_client.get_plan_fact_summary",
               return_value=_plan_fact_summary(time_pct=0.5, plan_rev=1_000_000.0, fact_rev=350_000.0)):
        result = compute_cdp_unit_economics(now)

    assert result is not None
    assert result["fact_share"] == pytest.approx(0.35)
    assert result["expected_share"] == pytest.approx(0.33)
    assert result["fact_pace"] == pytest.approx(0.35)  # обратная совместимость = fact_share
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


def test_plan_pace_behind_blocks():
    """fact_share < expected_share*1.0 -> plan_ok=False, plan_reason заполнен ('сезонно')."""
    now = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ)  # expected_share = PACING_CURVE[15] = 0.33
    # plan_rev=1_000_000, fact_rev=200_000 -> fact_share=0.20 < 0.33
    with patch("services.cdp_client.get_daily_report", return_value=_daily_items()), \
         patch("services.cdp_client.get_plan_fact_summary",
               return_value=_plan_fact_summary(time_pct=0.5, plan_rev=1_000_000.0, fact_rev=200_000.0)):
        result = compute_cdp_unit_economics(now)

    assert result is not None
    assert result["fact_share"] == pytest.approx(0.20)
    assert result["expected_share"] == pytest.approx(0.33)
    assert result["plan_ok"] is False
    assert result["plan_reason"] is not None
    assert "сезонно" in result["plan_reason"]


def test_plan_empty_cities_plan_ok():
    """cities=[] -> нет планового знаменателя -> plan_ok=True, plan_reason=None (не блокируем)."""
    with patch("services.cdp_client.get_daily_report", return_value=_daily_items()), \
         patch("services.cdp_client.get_plan_fact_summary",
               return_value=_plan_fact_summary(cities_override=[])):
        result = compute_cdp_unit_economics(_NOW)

    assert result is not None
    assert result["fact_pace"] is None
    assert result["fact_share"] is None
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


def test_plan_time_pct_normalized_from_percent():
    """time_pct=50.0 (проценты) -> нормализуется в 0.5 (доля), но НЕ влияет на решение гейта
    (решение — сезонная формула fact_share/expected_share)."""
    now = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ)  # expected_share = 0.33
    # fact_rev=350_000 -> fact_share=0.35 >= 0.33 (гейт прошёл бы и без учёта time_pct)
    with patch("services.cdp_client.get_daily_report", return_value=_daily_items()), \
         patch("services.cdp_client.get_plan_fact_summary",
               return_value=_plan_fact_summary(time_pct=50.0, plan_rev=1_000_000.0, fact_rev=350_000.0)):
        result = compute_cdp_unit_economics(now)

    assert result is not None
    assert result["time_pct"] == pytest.approx(0.5)  # нормализация сохранена (справочно)
    assert result["plan_gate_mode"] == "seasonal"
    assert result["plan_ok"] is True  # 0.35 >= 0.33 (сезонный гейт, time_pct не участвует)


def test_cdp_error_returns_none():
    """Клиент бросает CdpError (например daily-report) -> compute_cdp_unit_economics is None."""
    with patch("services.cdp_client.get_daily_report", side_effect=CdpError("CDP недоступен")), \
         patch("services.cdp_client.get_plan_fact_summary", return_value=_plan_fact_summary()):
        result = compute_cdp_unit_economics(_NOW)

    assert result is None


def test_cdp_error_from_plan_fact_summary_returns_none():
    """CdpError именно из get_plan_fact_summary (после успешного daily-report) -> None."""
    with patch("services.cdp_client.get_daily_report", return_value=_daily_items()), \
         patch("services.cdp_client.get_plan_fact_summary", side_effect=CdpError("нет плана")):
        result = compute_cdp_unit_economics(_NOW)

    assert result is None


# ===========================================================================
# Интеграция каскада в _run_scaling_inner / run_budget_scaling
# (мокаем на границе: cdp_client.* и plan_reader.read_general_plan + week-функции)
# ===========================================================================


def _make_local_ad(
    ad_id: str = "ad1",
    ad_name: str = "Победитель",
    payments: int = 3,
    days_running: int = 10,
) -> dict:
    return {
        "ad_id": ad_id, "ad_name": ad_name, "city": "CityA", "adset_type": "L2",
        "adset_id": None, "spend": 80.0, "leads": 8, "qual_pct": 20.0, "romi": 150.0,
        "cpl": 10.0, "ctr": 2.0, "hook_rate": 30.0, "impressions": 5000,
        "video_p25": 0, "video_p100": 0, "video_views_3s": 0,
        "payments": payments, "outcomes_matched_at": None, "days_running": days_running,
        "effective_status": "ACTIVE", "recommendation": "ЖДАТЬ", "reason": "",
    }


def _adset_info(budget_usd: float = 100.0, status: str = "ACTIVE", name: str = "Тестовый адсет") -> dict:
    return {"daily_budget_usd": budget_usd, "effective_status": status, "name": name}


def _base_cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "kill_switch": False,
        "scale_enabled": False,
        "max_budget_increase_pct": 15,
        "max_adset_budget_mult": 2.0,
        "max_adset_daily_budget": 300,
        "max_total_daily_budget": 4000,
        "max_scales_per_run": 2,
        "plan_sheet_id": "test_sheet_id",
        "cdp": {"enabled": False},
    }
    cfg.update(overrides)
    return cfg


# Лист с большим запасом и мягкой юниткой — гейт пропускает поток дальше.
_VALID_PLAN = {
    "week_label": "неделя 1 (2026-07-01–07)",
    "budget_usd": 100_000.0,
    "revenue_plan_lcy": 10_000_000.0,
    "unit_target": 0.99,
}


class _CascadeMocks:
    """Полный набор моков одного прогона run_budget_scaling для тестов каскада CDP/лист.

    cdp_ok=True/False управляет тем, бросают ли cdp_client.get_daily_report/
    get_plan_fact_summary исключение CdpError (эмулирует §8 «CDP err»).
    sheet_ok=True/False управляет тем, доступен ли листовой путь (read_general_plan).
    """

    def __init__(
        self,
        cfg: dict,
        cdp_ok: bool | None = True,
        cdp_daily_items: list[dict] | None = None,
        cdp_plan_fact: dict | None = None,
        sheet_ok: bool = True,
        sheet_plan: dict | None = _VALID_PLAN,
        sheet_revenue_lcy: float = 200_000.0,
        sheet_fb_week_spend: float = 0.0,
        local_ads: list[dict] | None = None,
        all_budgets: dict | None = None,
    ):
        if local_ads is None:
            local_ads = [_make_local_ad("ad1")]
        if all_budgets is None:
            all_budgets = {"adset1": _adset_info(100.0)}
        if cdp_daily_items is None:
            cdp_daily_items = _daily_items()
        if cdp_plan_fact is None:
            # По умолчанию — план-темп проходит с большим запасом (time_pct мал)
            cdp_plan_fact = _plan_fact_summary(time_pct=0.1, plan_rev=1_000_000.0, fact_rev=200_000.0)

        fb_ad_info = {ad["ad_id"]: {"adset_id": "adset1", "effective_status": "ACTIVE"} for ad in local_ads}

        def _fetch_candidate_fb_info_side_effect(ad_ids: list[str]) -> dict:
            return {aid: info for aid, info in fb_ad_info.items() if aid in ad_ids}

        self._stack = ExitStack()
        self._specs: dict[str, tuple] = {
            "get_scale_config": ("services.budget_scaler.get_scale_config", {"return_value": cfg}),
            "fetch_ads": ("services.shadow_report._fetch_ads_from_local_db", {"return_value": local_ads}),
            "load_settings": ("agent.scheduler.load_settings", {"return_value": {"thresholds": {}}}),
            "fetch_candidate_fb_info": (
                "services.autopilot._fetch_candidate_fb_info",
                {"side_effect": _fetch_candidate_fb_info_side_effect},
            ),
            "fetch_all_budgets": ("services.budget_scaler._fetch_all_account_adset_budgets", {"return_value": all_budgets}),
            "fetch_adset_budgets": ("services.budget_scaler._fetch_adset_budgets", {"return_value": {}}),
            "set_adset_budget": ("services.budget_scaler.set_adset_budget", {"return_value": True}),
            "send_telegram": ("services.notifications.send_telegram", {}),
            "send_critical_alert": ("services.notifications.send_critical_alert", {}),
            "save_decision": ("agent.repositories.decisions_repo.save_decision", {}),
            "send_with_buttons": ("services.telegram_bot.send_with_buttons", {"return_value": True}),
        }

        # --- CDP client (граница) ---
        if cdp_ok is True:
            self._specs["cdp_get_daily_report"] = (
                "services.cdp_client.get_daily_report", {"return_value": cdp_daily_items},
            )
            self._specs["cdp_get_plan_fact_summary"] = (
                "services.cdp_client.get_plan_fact_summary", {"return_value": cdp_plan_fact},
            )
        elif cdp_ok is False:
            self._specs["cdp_get_daily_report"] = (
                "services.cdp_client.get_daily_report", {"side_effect": CdpError("CDP недоступен")},
            )
            self._specs["cdp_get_plan_fact_summary"] = (
                "services.cdp_client.get_plan_fact_summary", {"return_value": cdp_plan_fact},
            )
        # cdp_ok=None — не мокаем клиент вообще (используется, когда cdp.enabled=false
        # и CDP всё равно считается для shadow — в данном наборе тестов cdp_ok всегда bool)

        # --- Листовой путь (граница) ---
        if sheet_ok:
            self._specs.update({
                "read_general_plan": ("services.plan_reader.read_general_plan", {"return_value": sheet_plan}),
                "get_usd_to_lcy": ("services.exchange_rate.get_usd_to_lcy", {"return_value": 100.0}),
                "get_fb_week_spend": ("services.budget_scaler.get_fb_week_spend", {"return_value": sheet_fb_week_spend}),
                "get_google_week_spend": ("services.budget_scaler.get_google_week_spend", {"return_value": 0.0}),
                "get_amo_week_revenue": ("services.budget_scaler.get_amo_week_revenue", {"return_value": sheet_revenue_lcy}),
            })
        else:
            # Лист недоступен: read_general_plan возвращает None (как при ошибке чтения)
            self._specs["read_general_plan"] = ("services.plan_reader.read_general_plan", {"return_value": None})

        self.mocks: dict[str, MagicMock] = {}

    def __enter__(self) -> "_CascadeMocks":
        for name, (target, kwargs) in self._specs.items():
            self.mocks[name] = self._stack.enter_context(patch(target, **kwargs))
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._stack.__exit__(exc_type, exc, tb)

    def __getitem__(self, name: str) -> MagicMock:
        return self.mocks[name]


# ---------------------------------------------------------------------------
# 6 комбинаций fail-closed таблицы §8
# ---------------------------------------------------------------------------


def test_cascade_disabled_uses_sheet():
    """cdp.enabled=false -> unit_source='sheet' (старый путь решает)."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": False})
    with _CascadeMocks(cfg, cdp_ok=True, sheet_ok=True) as _, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "sheet"


def test_cascade_cdp_enabled_ok_uses_cdp():
    """cdp.enabled=true, CDP ok -> unit_source='cdp' (ДРР+план из CDP)."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True})
    with _CascadeMocks(cfg, cdp_ok=True, sheet_ok=True) as _, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "cdp"


def test_cascade_cdp_enabled_err_falls_back_to_sheet():
    """cdp.enabled=true, CDP err, лист ok -> unit_source='sheet' (fallback), прогон не упал."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True})
    with _CascadeMocks(cfg, cdp_ok=False, sheet_ok=True) as _, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "sheet"
    assert result["ran"] is True
    assert result["drr_cdp"] is None


def test_cascade_both_unavailable_fail_closed():
    """cdp.enabled=true, CDP err, лист тоже нет -> unit_source='none', блок fail-closed."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True})
    with _CascadeMocks(cfg, cdp_ok=False, sheet_ok=False) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "none"
    assert result["scaled"] == []
    assert "fail-closed" in (result["skipped_reason"] or "")
    m["set_adset_budget"].assert_not_called()


def test_cascade_disabled_cdp_err_still_uses_sheet():
    """cdp.enabled=false, CDP тоже err (неважно, shadow не решает), лист ok -> unit_source='sheet'."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": False})
    with _CascadeMocks(cfg, cdp_ok=False, sheet_ok=True) as _, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "sheet"
    assert result["drr_cdp"] is None  # CDP упал -> shadow тоже None
    assert result["ran"] is True


def test_cascade_disabled_no_sheet_fail_closed():
    """cdp.enabled=false, CDP ok (но не решает), лист нет -> unit_source='none', блок fail-closed."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": False})
    with _CascadeMocks(cfg, cdp_ok=True, sheet_ok=False) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "none"
    assert result["scaled"] == []
    assert "fail-closed" in (result["skipped_reason"] or "")
    m["set_adset_budget"].assert_not_called()


# ---------------------------------------------------------------------------
# CdpError никогда не роняет прогон
# ---------------------------------------------------------------------------


def test_cdp_error_never_crashes_run():
    """get_daily_report кидает исключение -> run завершился (ran=True), unit_source='sheet',
    поля телеметрии присутствуют (не всплыло исключение наружу)."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True})
    with _CascadeMocks(cfg, cdp_ok=False, sheet_ok=True) as _, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["ran"] is True
    assert result["unit_source"] == "sheet"
    for key in ("unit_source", "drr_cdp", "drr_sheet", "drr_divergence_pp"):
        assert key in result


def test_cdp_error_never_crashes_run_even_unexpected_exception():
    """compute_cdp_unit_economics падает НЕ CdpError-исключением (баг где-то внутри) —
    доп. страховка try/except в _run_scaling_inner всё равно не роняет прогон."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True})
    with _CascadeMocks(cfg, cdp_ok=True, sheet_ok=True) as _, \
         patch("services.budget_scaler.compute_cdp_unit_economics", side_effect=RuntimeError("boom")), \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["ran"] is True
    assert result["unit_source"] == "sheet"  # cdp_ue=None -> fallback на лист


# ---------------------------------------------------------------------------
# Shadow при enabled=false
# ---------------------------------------------------------------------------


def test_shadow_divergence_computed_when_disabled():
    """cdp.enabled=false -> решает лист, но drr_cdp посчитан и drr_divergence_pp =
    abs(cdp-sheet)*100 (точное число)."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": False})
    sheet_revenue_lcy = 400_000.0
    sheet_fb_week_spend = 200.0  # с курсом 100 -> spend_lcy=20000; drr_sheet = 20000/400000=0.05
    with _CascadeMocks(
        cfg, cdp_ok=True, sheet_ok=True,
        sheet_revenue_lcy=sheet_revenue_lcy, sheet_fb_week_spend=sheet_fb_week_spend,
    ) as _, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "sheet"
    expected_drr_cdp = 19_350.0 / 400_000.0  # из _daily_items()
    expected_drr_sheet = (200.0 * 100.0) / 400_000.0
    assert result["drr_cdp"] == pytest.approx(expected_drr_cdp)
    assert result["drr_sheet"] == pytest.approx(expected_drr_sheet)
    assert result["drr_divergence_pp"] == pytest.approx(
        abs(expected_drr_cdp - expected_drr_sheet) * 100.0
    )


def test_shadow_drr_cdp_present_even_when_disabled_and_no_scaling_gate_blocks():
    """Регрессия §8: при disabled shadow всё равно считает CDP, даже если решение
    принял лист и результат — блок по юнитке листа."""
    from services.budget_scaler import run_budget_scaling

    tight_plan = {**_VALID_PLAN, "unit_target": 0.001}  # почти любой факт превысит план
    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": False})
    with _CascadeMocks(
        cfg, cdp_ok=True, sheet_ok=True, sheet_plan=tight_plan,
        sheet_revenue_lcy=200_000.0, sheet_fb_week_spend=500.0,
    ) as _, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "sheet"
    assert "юнитка" in (result["skipped_reason"] or "")
    assert result["drr_cdp"] is not None  # shadow посчитан несмотря на блок листа


# ---------------------------------------------------------------------------
# Телеметрия в ранних ветках (kill_switch/disabled)
# ---------------------------------------------------------------------------


def test_telemetry_present_when_autopilot_disabled():
    """cfg['enabled']=False (ранний return до расчёта юнитки) -> unit_source='none',
    все 4 ключа телеметрии присутствуют."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(enabled=False)
    with patch("services.budget_scaler.get_scale_config", return_value=cfg):
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is False
    assert result["skipped_reason"] == "disabled"
    assert result["unit_source"] == "none"
    assert result["drr_cdp"] is None
    assert result["drr_sheet"] is None
    assert result["drr_divergence_pp"] is None


def test_telemetry_present_when_kill_switch():
    """cfg['kill_switch']=True (ранний return) -> unit_source='none', ключи присутствуют."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(kill_switch=True)
    with patch("services.budget_scaler.get_scale_config", return_value=cfg):
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is False
    assert result["skipped_reason"] == "kill_switch"
    assert result["unit_source"] == "none"
    for key in ("drr_cdp", "drr_sheet", "drr_divergence_pp"):
        assert result[key] is None


# ---------------------------------------------------------------------------
# Окно: compute_cdp_unit_economics вызывает get_daily_report с границами
# compute_drr_window(now) (frozen now) — уже частично проверено в юнит-тесте выше,
# здесь — сквозная проверка через полный прогон run_budget_scaling.
# ---------------------------------------------------------------------------


def test_window_boundaries_match_compute_drr_window_in_full_run():
    """Полный прогон с cdp.enabled=true — get_daily_report вызван ровно с границами
    compute_drr_window(_NOW), а не каким-то другим окном."""
    from services.budget_scaler import run_budget_scaling

    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True})
    with _CascadeMocks(cfg, cdp_ok=True, sheet_ok=True) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        run_budget_scaling(mode="active")

    window_start, window_end, _ = compute_drr_window(_NOW)
    m["cdp_get_daily_report"].assert_called_once_with(window_start.date(), window_end.date())


# ---------------------------------------------------------------------------
# drr > unit_target блокирует при cdp-источнике
# ---------------------------------------------------------------------------


def test_cdp_drr_exceeds_target_blocks():
    """cdp.enabled=true, drr(cdp)>unit_target -> skipped_reason содержит 'юнитка', подъёмов 0."""
    from services.budget_scaler import run_budget_scaling

    # unit_target листа = 0.01 (1%), а drr_cdp из _daily_items() = 0.048375 (4.8%) > target
    tight_plan = {**_VALID_PLAN, "unit_target": 0.01}
    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True})
    with _CascadeMocks(cfg, cdp_ok=True, sheet_ok=True, sheet_plan=tight_plan) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(_NOW):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "cdp"
    assert result["scaled"] == []
    assert "юнитка" in (result["skipped_reason"] or "")
    m["set_adset_budget"].assert_not_called()


def test_cdp_plan_pace_behind_blocks_in_full_run():
    """cdp.enabled=true, ДРР ок но сезонный план-гейт CDP отстаёт -> блок с plan_reason CDP.

    Формула заменена по спеке ARCH-cdp-seasonal-pacing (была fact_pace>=time_pct*0.9).
    Используем ЛОКАЛЬНЫЙ now=2026-07-15 (среда) внутри теста — НЕ переопределяем
    модульную _NOW=2026-07-08, она нужна другим тестам файла для ДРР-окна/каскада.
    expected_share = PACING_CURVE[15] = 0.33.
    """
    from services.budget_scaler import run_budget_scaling

    now_mid_month = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ)
    # plan_rev=1_000_000, fact_rev=100_000 -> fact_share=0.10 < expected_share=0.33
    behind_plan_fact = _plan_fact_summary(time_pct=0.9, plan_rev=1_000_000.0, fact_rev=100_000.0)
    cfg = _base_cfg(scale_enabled=True, cdp={"enabled": True})
    with _CascadeMocks(cfg, cdp_ok=True, sheet_ok=True, cdp_plan_fact=behind_plan_fact) as m, \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         _frozen_now(now_mid_month):
        result = run_budget_scaling(mode="active")

    assert result["unit_source"] == "cdp"
    assert result["scaled"] == []
    assert "сезонно" in (result["skipped_reason"] or "")
    m["set_adset_budget"].assert_not_called()


# ---------------------------------------------------------------------------
# Аварийная ветка верхнего try/except в run_budget_scaling (§6.3): даже при
# необработанном исключении внутри _run_scaling_inner телеметрия обязана
# присутствовать во всех 4 ключах (регрессия ревью: эта ветка была без них).
# ---------------------------------------------------------------------------


def test_telemetry_present_when_top_level_exception_caught():
    """_run_scaling_inner бросает неожиданное исключение -> внешний try/except
    run_budget_scaling ловит его, ran=False, но все 4 ключа телеметрии на месте
    (unit_source='none', drr_*=None) — единообразно с остальными ранними return."""
    from services.budget_scaler import run_budget_scaling

    with patch("services.budget_scaler._run_scaling_inner", side_effect=RuntimeError("boom")), \
         patch("services.notifications.send_critical_alert"):
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is False
    assert result["skipped_reason"] == "error: boom"
    assert result["unit_source"] == "none"
    assert result["drr_cdp"] is None
    assert result["drr_sheet"] is None
    assert result["drr_divergence_pp"] is None


# ===========================================================================
# Регрессия: CDP может отдать город с явным "plan": null
# (не отсутствие ключа, а null-значение) — c.get("plan", {}) НЕ спасает, т.к.
# дефолт .get() срабатывает только при ОТСУТСТВИИ ключа, а не при null.
# Ожидание: город с plan=null просто пропускается из суммы (вклад 0), CDP-путь
# не падает целиком. Аналогично для fact=null.
# ===========================================================================


def test_plan_null_city_among_normal_excluded_from_sum():
    """Город с "plan": null среди нормальных городов -> сумма считается по
    остальным (вклад null-города в plan_rev = 0), не падает."""
    cities = [
        {"city": "CityA", "plan": {"revenue_new": 600_000.0}, "fact": {"revenue_new": 300_000.0}},
        {"city": "CityB", "plan": None, "fact": {"revenue_new": 100_000.0}},  # кейс с null
    ]
    with patch("services.cdp_client.get_daily_report", return_value=_daily_items()), \
         patch("services.cdp_client.get_plan_fact_summary",
               return_value=_plan_fact_summary(cities_override=cities)):
        result = compute_cdp_unit_economics(_NOW)

    assert result is not None
    # plan_rev = 600_000 (CityB с null не даёт вклада), fact_rev = 300_000+100_000=400_000
    assert result["fact_share"] == pytest.approx(400_000.0 / 600_000.0)


def test_fact_null_city_among_normal_excluded_from_sum():
    """Город с "fact": null среди нормальных -> вклад в fact_rev = 0, не падает."""
    cities = [
        {"city": "CityA", "plan": {"revenue_new": 600_000.0}, "fact": {"revenue_new": 300_000.0}},
        {"city": "CityB", "plan": {"revenue_new": 400_000.0}, "fact": None},  # кейс с null
    ]
    with patch("services.cdp_client.get_daily_report", return_value=_daily_items()), \
         patch("services.cdp_client.get_plan_fact_summary",
               return_value=_plan_fact_summary(cities_override=cities)):
        result = compute_cdp_unit_economics(_NOW)

    assert result is not None
    # plan_rev = 600_000+400_000=1_000_000, fact_rev = 300_000 (CityB с null не даёт вклада)
    assert result["fact_share"] == pytest.approx(300_000.0 / 1_000_000.0)


def test_all_cities_plan_null_fact_share_none_plan_ok_true():
    """ВСЕ города "plan": null -> plan_rev=0 -> fact_share=None -> plan_ok=True
    (существующая ветка «нет планового знаменателя — не блокируем»)."""
    cities = [
        {"city": "CityA", "plan": None, "fact": {"revenue_new": 300_000.0}},
        {"city": "CityB", "plan": None, "fact": {"revenue_new": 100_000.0}},
    ]
    with patch("services.cdp_client.get_daily_report", return_value=_daily_items()), \
         patch("services.cdp_client.get_plan_fact_summary",
               return_value=_plan_fact_summary(cities_override=cities)):
        result = compute_cdp_unit_economics(_NOW)

    assert result is not None
    assert result["fact_share"] is None
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


def test_metrics_null_city_in_cities_does_not_crash():
    """Город с ключами "plan"/"fact" вообще отсутствующими (не null, а нет
    ключа) — старое поведение .get(key, {}) тоже должно продолжать работать
    (регрессия: фикс не должен сломать штатный случай "нет ключа")."""
    cities = [
        {"city": "CityA", "plan": {"revenue_new": 600_000.0}, "fact": {"revenue_new": 300_000.0}},
        {"city": "CityJ"},  # нет ни plan, ни fact вообще
    ]
    with patch("services.cdp_client.get_daily_report", return_value=_daily_items()), \
         patch("services.cdp_client.get_plan_fact_summary",
               return_value=_plan_fact_summary(cities_override=cities)):
        result = compute_cdp_unit_economics(_NOW)

    assert result is not None
    assert result["fact_share"] == pytest.approx(300_000.0 / 600_000.0)


def test_cities_none_treated_as_empty():
    """summary["cities"] сам по себе null (не просто пустой список) -> не падает,
    ведёт себя как cities=[] (plan_ok=True, fact_share=None)."""
    summary = _plan_fact_summary()
    summary["cities"] = None
    with patch("services.cdp_client.get_daily_report", return_value=_daily_items()), \
         patch("services.cdp_client.get_plan_fact_summary", return_value=summary):
        result = compute_cdp_unit_economics(_NOW)

    assert result is not None
    assert result["fact_share"] is None
    assert result["plan_ok"] is True
