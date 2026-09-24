"""
Тесты services/budget_scaler.py — масштабирование бюджетов победителей.

Все внешние зависимости замокированы:
- _fetch_ads_from_local_db (локальная БД)
- score_and_decide (decision_policy)
- _fetch_candidate_fb_info (FB API — adset_id по ad_id)
- _fetch_all_account_adset_budgets (FB API — ВСЕ адсеты аккаунта для total)
- _fetch_adset_budgets (FB API — бюджеты конкретных адсетов, только для missing)
- set_adset_budget (legacy-заглушка прямой мутации — проверяем что НЕ вызывается)
- propose_scale (producer-граница: active-режим только СОЗДАЁТ proposal владельцу)
- send_telegram, send_critical_alert (уведомления)
- get_scale_config (конфиг)
- _is_in_cooldown, _record_scaled_at (state-файл кулдауна)

Approval-first: прямого подъёма бюджета из скейлера больше НЕ существует.
В active-режиме прогон создаёт owner-proposal (result["proposals"]), а
result["scaled"] остаётся пустым всегда — реальная мутация FB происходит только
в execution boundary после одобрения владельцем в Telegram. Поэтому тесты
active-режима проверяют пару инвариантов: (а) прямой FB-мутации не было,
(б) создан корректный proposal либо действие честно заблокировано гейтом.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pytest

from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import (
    install_provider_mutation_guard,
    install_proposal_recorder,
    proposal_outcome,
    proposal_outcomes,
)

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импортов проекта
import sys as _sys
if "google.genai" not in _sys.modules:
    _sys.modules["google.genai"] = MagicMock()
if "google" not in _sys.modules:
    _sys.modules["google"] = MagicMock()

_TZ = timezone(timedelta(hours=5))


@pytest.fixture(autouse=True)
def isolated_daily_cap_state(tmp_path, monkeypatch):
    """Перенаправляет state-файл дневного капа во временную папку для всех тестов.

    Без этого тесты писали бы в реальный data/budget_daily_cap_state.json проекта
    и могли бы влиять друг на друга (общий diск между тестами одного прогона).
    """
    import services.budget_daily_cap as cap_module
    state_file = tmp_path / "budget_daily_cap_state.json"
    monkeypatch.setattr(cap_module, "_CAP_STATE_FILE", state_file)


# ---------------------------------------------------------------------------
# Вспомогательные данные
# ---------------------------------------------------------------------------

def _make_local_ad(
    ad_id: str = "ad1",
    ad_name: str = "Победитель",
    qual_pct: float = 20.0,
    romi: float = 150.0,
    spend: float = 80.0,
    leads: int = 8,
    payments: int = 3,
    days_running: int = 10,
    outcomes_matched_at: str | None = None,
) -> dict:
    """Создаёт объявление из локальной БД с метриками победителя по продажам.

    Дефолт payments=3 (>0) — по новой семантике Фазы 2 кандидат отбирается
    по факту продаж (см. services.budget_scaler._select_sales_candidates),
    а не по qual_pct/romi (они остаются как доп. сигнал сортировки).
    """
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "city": "CityA",
        "adset_type": "L2",
        "adset_id": None,
        "spend": spend,
        "leads": leads,
        "qual_pct": qual_pct,
        "romi": romi,
        "cpl": spend / leads if leads else 0,
        "ctr": 2.0,
        "hook_rate": 30.0,
        "impressions": 5000,
        "video_p25": 0,
        "video_p100": 0,
        "video_views_3s": 0,
        "payments": payments,
        "outcomes_matched_at": outcomes_matched_at,
        "days_running": days_running,
        "effective_status": "ACTIVE",
        "recommendation": "ЖДАТЬ",
        "reason": "",
    }


def _make_scale_decision(ad_id: str = "ad1", ad_name: str = "Победитель") -> dict:
    """Возвращает решение SCALE от decision_policy."""
    return {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "adset_id": "",
        "action": "SCALE",
        "score": 7,
        "reasons": ["+3: лид в первые 3 дня", "+2: hook выше медианы", "+2: ctr выше медианы"],
    }


def _base_cfg() -> dict:
    """Базовый конфиг с включённым автопилотом (scale_enabled=False по умолчанию).

    D4 (fail-closed): без plan_sheet_id план-гейт теперь блокирует любой подъём —
    поэтому тесты, проверяющие логику ПОСЛЕ гейта (потолки/dry_run/cap), должны
    сами замокать валидный план через константу _VALID_PLAN (см. ниже), иначе
    они тестируют не то, что задумано (упрутся в fail-closed раньше своей проверки).
    """
    return {
        "enabled": True,
        "kill_switch": False,
        "scale_enabled": False,
        "max_budget_increase_pct": 15,  # дефолт Фазы 2: % роста В СУТКИ на адсет
        "max_adset_budget_mult": 2.0,
        "max_adset_daily_budget": 300,
        "max_total_daily_budget": 4000,
        "max_scales_per_run": 2,
        "plan_sheet_id": "test_sheet_id",
    }


# D4: план с большим запасом и ДРР в норме — план-гейт пропускает поток дальше,
# чтобы тест мог проверить логику ПОСЛЕ гейта (потолки, cap, dry_run).
# Используется в тестах, которые до fail-closed (D4) не задавали plan_sheet_id.
_VALID_PLAN = {
    "week_label": "неделя 1 (2026-07-01–07)",
    "budget_usd": 100_000.0,       # запас заведомо большой — гейт не блокирует
    "revenue_plan_lcy": 10_000_000.0,
    "unit_target": 0.99,           # ДРР-таргет заведомо высокий — не мешает
}


def _adset_info(budget_usd: float = 100.0, status: str = "ACTIVE") -> dict:
    """Создаёт ответ FB по адсету."""
    return {
        "daily_budget_usd": budget_usd,
        "effective_status": status,
        "name": "Тестовый адсет",
    }


def _no_cooldown():
    """Возвращает (False, None) — кулдаун не активен."""
    return (False, None)


# ---------------------------------------------------------------------------
# Тест 1: dry_run НЕ вызывает set_adset_budget
# ---------------------------------------------------------------------------

def test_dry_run_does_not_call_set_adset_budget():
    """В режиме dry_run set_adset_budget не вызывается никогда."""
    local_ads = [_make_local_ad("ad1")]
    decisions = [_make_scale_decision("ad1")]
    all_budgets = {"adset1": _adset_info(100.0)}

    cfg = _base_cfg()

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.plan_reader.read_general_plan", return_value=_VALID_PLAN), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=200_000.0), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler.set_adset_budget") as mock_set, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    # Ключевая проверка: set_adset_budget не вызывался
    mock_set.assert_not_called()
    assert result["ran"] is True
    assert result["mode"] == "dry_run"
    assert len(result["recommendations"]) >= 1
    assert result["scaled"] == []


# ---------------------------------------------------------------------------
# Тест 2: scale_enabled=False → не меняет бюджет даже при mode="active"
# ---------------------------------------------------------------------------

def test_scale_disabled_does_not_mutate_budget():
    """При scale_enabled=False mode=active → переходит в dry_run, set_adset_budget не вызывается."""
    local_ads = [_make_local_ad("ad1")]
    decisions = [_make_scale_decision("ad1")]
    all_budgets = {"adset1": _adset_info(100.0)}

    cfg = {**_base_cfg(), "scale_enabled": False}

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler.set_adset_budget") as mock_set, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="active")

    mock_set.assert_not_called()
    # mode принудительно переведён в dry_run
    assert result["mode"] == "dry_run"
    assert result["scaled"] == []


# ---------------------------------------------------------------------------
# Тест 3: повышение режется max_budget_increase_pct
# ---------------------------------------------------------------------------

def test_budget_increase_capped_by_max_pct():
    """Новый бюджет = current + effective_pct% от start_budget (§10.1 спеки Фазы 2)."""
    from services.budget_scaler import _calc_new_budget

    current = 100.0
    # start_budget=current (первый подъём за день), effective_pct=20%
    # Потолок 200 (mult=2.0 от start) и абсолютный 300
    new = _calc_new_budget(
        current_budget_usd=current,
        start_budget_usd=current,
        effective_pct=20,
        max_mult=2.0,
        max_abs_usd=300,
    )
    # 100 + 100×0.20 = 120
    assert new == pytest.approx(120.0)


def test_budget_increase_capped_at_low_pct():
    """При effective_pct=10 на $200 бюджете (start=current) — +$20."""
    from services.budget_scaler import _calc_new_budget

    new = _calc_new_budget(200.0, start_budget_usd=200.0, effective_pct=10, max_mult=2.0, max_abs_usd=300)
    assert new == pytest.approx(220.0)


def test_budget_increase_second_raise_same_day_adds_from_start():
    """Инвариант §10.1: второй подъём за день считает % ОТ START, не от current.

    start=$100. Подъём1: +10% → current=$110. Подъём2: effective=5% от
    start=$100=+$5 → $115 (НЕ +5% от current=$110=$115.5, и НЕ абсолютный
    таргет start×1.05=$105 с последующим max($105,$110)=$110).
    """
    from services.budget_scaler import _calc_new_budget

    new = _calc_new_budget(
        current_budget_usd=110.0, start_budget_usd=100.0, effective_pct=5,
        max_mult=2.0, max_abs_usd=300,
    )
    assert new == pytest.approx(115.0)


# ---------------------------------------------------------------------------
# Тест 4: общий потолок учитывает ВСЕ активные адсеты аккаунта
# ---------------------------------------------------------------------------

def test_total_budget_cap_uses_all_account_adsets():
    """Общий потолок считается по ВСЕМ активным адсетам аккаунта, а не только кандидатам.

    Аккаунт тратит $3950 на других адсетах (не кандидатах).
    Кандидат: $100, потолок $4000.
    Повышение 20% = +$20 → $3950+$100+$20=$4070 > $4000 → блокируется.
    """
    local_ads = [_make_local_ad("ad1")]
    decisions = [_make_scale_decision("ad1")]

    cfg = {
        **_base_cfg(),
        "max_total_daily_budget": 4000,
        "max_adset_daily_budget": 500,  # abs-потолок не мешает
    }

    # Аккаунт: кандидат на $100 + другие на $3950 = total $4050 > $4000
    # Но даже current_total=$4050 уже > $4000 → любое повышение заблокируется.
    # Используем: кандидат $100, остальные $3850 → total $3950.
    # Повышение +$20 → $3970, всё ещё < $4000 → прошло бы по старой логике.
    # Правильно: total должен включать $3850 других → $3850+$100+$20=$3970...
    # Скорректируем: остальные $3950, кандидат $60 → total $4010 > $4000 → блок.
    all_account_adsets = {
        "other1": {"daily_budget_usd": 2000.0, "effective_status": "ACTIVE", "name": "Другой 1"},
        "other2": {"daily_budget_usd": 1950.0, "effective_status": "ACTIVE", "name": "Другой 2"},
        "adset1": {"daily_budget_usd": 60.0, "effective_status": "ACTIVE", "name": "Кандидат"},
    }
    # total = 2000+1950+60 = $4010, потолок $4000 → уже превышен.
    # Повышение $60→$72 (+$12): $4010+$12=$4022 > $4000 → блокируется.

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.plan_reader.read_general_plan", return_value=_VALID_PLAN), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=200_000.0), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets",
               return_value=all_account_adsets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler.set_adset_budget") as mock_set, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    mock_set.assert_not_called()
    # Рекомендаций нет — общий потолок заблокировал (учитывая весь аккаунт)
    assert result["recommendations"] == []
    assert any("потолок" in e for e in result["errors"])


def test_total_budget_cap_blocks_scaling():
    """Простой случай: один адсет, повышение пробивает max_total_daily_budget."""
    local_ads = [_make_local_ad("ad1")]
    decisions = [_make_scale_decision("ad1")]

    # Текущий суммарный бюджет: $90, потолок $100.
    # Повышение 20% → $108, delta=$18 → 90+18=108 > 100 → блокируется.
    # max_adset_daily_budget=200 чтобы abs-потолок не срабатывал раньше.
    cfg = {**_base_cfg(), "max_total_daily_budget": 100, "max_adset_daily_budget": 200}
    all_budgets = {"adset1": {"daily_budget_usd": 90.0, "effective_status": "ACTIVE", "name": "Большой"}}

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.plan_reader.read_general_plan", return_value=_VALID_PLAN), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=200_000.0), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler.set_adset_budget") as mock_set, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    mock_set.assert_not_called()
    assert result["recommendations"] == []
    assert any("потолок" in e for e in result["errors"])


def test_total_budget_cumulative_two_raises(monkeypatch):
    """Накопительный учёт: два повышения суммарно не должны пробить потолок.

    Потолок $220. Два кандидата по $100 → каждый +$20.
    Первое повышение: $200+$20=$220 ≤ $220 → OK.
    Второе повышение: $220+$20=$240 > $220 → заблокировано.
    Итого: только 1 из 2 кандидатов доходит до предложения владельцу.

    Approval-first: «прошло» теперь = создан ровно один proposal; сам бюджет FB
    не меняется до одобрения, поэтому проверяем ещё и что прямой мутации не было.
    """
    local_ads = [
        _make_local_ad("ad1", "Победитель 1"),
        _make_local_ad("ad2", "Победитель 2"),
    ]
    decisions = [
        _make_scale_decision("ad1", "Победитель 1"),
        _make_scale_decision("ad2", "Победитель 2"),
    ]

    cfg = {
        **_base_cfg(),
        "scale_enabled": True,
        "max_total_daily_budget": 220,
        "max_adset_daily_budget": 200,   # abs не мешает
        "max_scales_per_run": 2,
    }
    # Оба кандидата по $100. total текущий = $100+$100 = $200.
    all_account_adsets = {
        "adset1": {"daily_budget_usd": 100.0, "effective_status": "ACTIVE", "name": "Адсет 1"},
        "adset2": {"daily_budget_usd": 100.0, "effective_status": "ACTIVE", "name": "Адсет 2"},
    }

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.plan_reader.read_general_plan", return_value=_VALID_PLAN), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=200_000.0), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value={
             "ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"},
             "ad2": {"adset_id": "adset2", "effective_status": "ACTIVE"},
         }), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets",
               return_value=all_account_adsets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch(
             "services.action_producer_gateway.propose_scale",
             side_effect=proposal_outcomes("prop-1", "prop-2"),
         ) as mock_propose, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        guard = install_provider_mutation_guard(monkeypatch)
        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="active", max_scales=2)

    # Только 1 из 2 прошло (накопительный потолок заблокировал второе)
    assert len(result["recommendations"]) == 1
    assert mock_propose.call_count == 1
    assert result["proposals"] == ["prop-1"]
    # Ничего не поднято прямо сейчас — только предложено владельцу.
    assert result["scaled"] == []
    guard.assert_untouched()
    # Второе — в ошибках
    assert any("потолок" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Тест 5: адсет уже на потолке (max_adset_budget_mult) — не растёт
# ---------------------------------------------------------------------------

def test_adset_at_mult_cap_skipped():
    """Если адсет уже на max_adset_budget_mult × начальный бюджет — не повышаем."""
    from services.budget_scaler import _calc_new_budget

    # current=200, mult=2.0 → потолок 400, abs=300 → фактический потолок 300
    # Если current=300 (уже на абсолютном потолке) → new=300=current → нет роста
    new = _calc_new_budget(300.0, start_budget_usd=300.0, effective_pct=20, max_mult=2.0, max_abs_usd=300)
    assert new == pytest.approx(300.0)  # нет роста — уже на потолке


def test_adset_at_mult_cap_in_flow():
    """В полном прогоне: адсет с бюджетом = abs_cap не попадает в recommendations."""
    local_ads = [_make_local_ad("ad1")]
    decisions = [_make_scale_decision("ad1")]

    # Адсет уже на абсолютном потолке $300
    cfg = {**_base_cfg(), "max_adset_daily_budget": 300}
    all_budgets = {"adset1": {"daily_budget_usd": 300.0, "effective_status": "ACTIVE", "name": "Потолочный"}}

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler.set_adset_budget") as mock_set, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    mock_set.assert_not_called()
    assert result["recommendations"] == []


# ---------------------------------------------------------------------------
# Тест 6: только победители масштабируются (слабый qual/romi не масштабируется)
# ---------------------------------------------------------------------------

def test_weak_ad_not_scaled():
    """Объявление БЕЗ оплат (payments None/0) не попадает в sales-кандидаты.

    Фаза 2 (§10.2 спеки): критерий отбора теперь ФАКТ ПРОДАЖ (payments>0),
    а не qual_pct/romi/spend/leads (старая _is_winner-логика удалена).
    """
    from services.budget_scaler import _select_sales_candidates

    # payments=None — нет сверки, не считается оплатой
    no_payments_data = {"ad_id": "a1", "payments": None, "qual_pct": 20.0, "romi": 100.0}
    assert _select_sales_candidates([no_payments_data]) == []

    # payments=0 — сверено, оплат нет
    zero_payments = {"ad_id": "a2", "payments": 0, "qual_pct": 20.0, "romi": 100.0}
    assert _select_sales_candidates([zero_payments]) == []


def test_winner_passes_all_criteria():
    """Объявление с payments>0 признаётся sales-кандидатом (§10.2 спеки)."""
    from services.budget_scaler import _select_sales_candidates

    winner = {"ad_id": "a1", "payments": 3, "qual_pct": 20.0, "romi": 150.0}
    result = _select_sales_candidates([winner])
    assert len(result) == 1
    assert result[0]["ad_id"] == "a1"


def test_confirmed_waster_detection():
    """_is_confirmed_waster: сверено (outcomes_matched_at!=None) И payments==0."""
    from services.budget_scaler import _is_confirmed_waster

    waster = {"outcomes_matched_at": "2026-07-01T10:00:00", "payments": 0}
    assert _is_confirmed_waster(waster) is True

    # Не сверено — не считается сливом (недостаточно данных)
    not_matched = {"outcomes_matched_at": None, "payments": 0}
    assert _is_confirmed_waster(not_matched) is False

    # Сверено, но есть оплаты — не слив
    has_payments = {"outcomes_matched_at": "2026-07-01T10:00:00", "payments": 2}
    assert _is_confirmed_waster(has_payments) is False


def test_sales_candidates_sort_order():
    """Сортировка кандидатов: payments↓, затем qual_pct↓, затем romi↓ (§10.2)."""
    from services.budget_scaler import _select_sales_candidates

    ads = [
        {"ad_id": "low", "payments": 2, "qual_pct": 20.0, "romi": 50.0},
        {"ad_id": "top", "payments": 5, "qual_pct": 10.0, "romi": 10.0},
        {"ad_id": "mid", "payments": 2, "qual_pct": 30.0, "romi": 40.0},
    ]
    result = _select_sales_candidates(ads)
    assert [r["ad_id"] for r in result] == ["top", "mid", "low"]


# ---------------------------------------------------------------------------
# Тест 7: cap соблюдён — max_scales_per_run ограничивает число адсетов
# ---------------------------------------------------------------------------

def test_max_scales_per_run_cap(monkeypatch):
    """max_scales_per_run=1: предложение уходит по 1 адсету, второй срезан капом.

    Кап режет ЧИСЛО действий за прогон — при approval-first это число созданных
    proposal'ов (мутаций FB в прогоне нет вообще).
    """
    local_ads = [
        _make_local_ad("ad1", "Победитель 1"),
        _make_local_ad("ad2", "Победитель 2"),
    ]
    decisions = [
        _make_scale_decision("ad1", "Победитель 1"),
        _make_scale_decision("ad2", "Победитель 2"),
    ]

    cfg = {**_base_cfg(), "scale_enabled": True, "max_scales_per_run": 1}
    all_budgets = {
        "adset1": _adset_info(100.0),
        "adset2": _adset_info(120.0),
    }

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.plan_reader.read_general_plan", return_value=_VALID_PLAN), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=200_000.0), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info", return_value={
             "ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"},
             "ad2": {"adset_id": "adset2", "effective_status": "ACTIVE"},
         }), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch(
             "services.action_producer_gateway.propose_scale",
             return_value=proposal_outcome("prop-1"),
         ) as mock_propose, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        guard = install_provider_mutation_guard(monkeypatch)
        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="active", max_scales=1)

    # Ровно одно предложение владельцу (cap=1), FB не тронут
    assert mock_propose.call_count == 1
    assert result["proposals"] == ["prop-1"]
    assert result["scaled"] == []
    guard.assert_untouched()
    assert len(result["winners"]) == 1  # срезано до cap


# ---------------------------------------------------------------------------
# Тест 8: disabled → ran=False
# ---------------------------------------------------------------------------

def test_disabled_returns_ran_false():
    """Если enabled=False — budget scaler не запускается."""
    cfg = {**_base_cfg(), "enabled": False}

    with patch("services.budget_scaler.get_scale_config", return_value=cfg):
        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is False
    assert result["skipped_reason"] == "disabled"


# ---------------------------------------------------------------------------
# Тест 9: kill_switch → ran=False
# ---------------------------------------------------------------------------

def test_kill_switch_returns_ran_false():
    """Если kill_switch=True — budget scaler не запускается."""
    cfg = {**_base_cfg(), "kill_switch": True}

    with patch("services.budget_scaler.get_scale_config", return_value=cfg):
        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is False
    assert result["skipped_reason"] == "kill_switch"


# ---------------------------------------------------------------------------
# Тест 10: active + scale_enabled=True → создаёт SCALE-proposal владельцу
# ---------------------------------------------------------------------------

def test_active_with_scale_enabled_creates_scale_proposal(monkeypatch, tmp_path):
    """При scale_enabled=True и mode=active подъём УХОДИТ В ПРЕДЛОЖЕНИЕ, не в FB.

    Тут работает НАСТОЯЩИЙ propose_scale (recorder подменяет только запись
    proposal в БД), поэтому тест проверяет реальную форму предложения:
    kind=SCALE, target=adset1, action_kind=SET_ADSET_BUDGET и точный intent
    ($100 → $115). Раньше этот тест доказывал факт прямой мутации бюджета —
    такой функциональности больше нет, её место занял owner-proposal.
    """
    local_ads = [_make_local_ad("ad1")]
    decisions = [_make_scale_decision("ad1")]
    all_budgets = {"adset1": _adset_info(100.0)}

    cfg = {**_base_cfg(), "scale_enabled": True}

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.plan_reader.read_general_plan", return_value=_VALID_PLAN), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=200_000.0), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         patch("agent.repositories.decisions_repo.save_decision"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        recorded = install_proposal_recorder(monkeypatch, tmp_path)
        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="active")

    # (а) прямой мутации FB и обхода approval не было
    recorded.assert_no_direct_provider_mutation()
    # (б) создан ровно один корректный SCALE-proposal на нужный адсет
    plan = recorded.assert_proposed(
        "adset1",
        kind=ProposalKind.SCALE,
        origin=ProposalOrigin.AUTOPILOT,
        action_kind="SET_ADSET_BUDGET",
    )
    payload = plan.targets[0].intended_payload
    # Новый бюджет: 100 + 100×15% = 115 (дефолт max_budget_increase_pct=15, §10.1)
    assert float(payload["expected_current_budget_usd"]) == pytest.approx(100.0)
    assert float(payload["target_budget_usd"]) == pytest.approx(115.0)
    assert payload["candidate_ad_id"] == "ad1"
    assert len(result["proposals"]) == 1
    assert result["scaled"] == []
    assert result["mode"] == "active"


# ---------------------------------------------------------------------------
# Тест 10b: сбой подсчёта суммарного бюджета (None) → fail-closed
# ---------------------------------------------------------------------------

def test_total_budget_uncounted_skips_all_raises():
    """FB отдал не-200 в ходе пагинации (_fetch_all_account_adset_budgets → None).

    Fail-closed (по итогам ревью): даже с готовым победителем и
    scale_enabled=True бюджеты НЕ поднимаются (нельзя доверять сумме → нельзя
    проверить общий потолок), в отчёт уходит честная строка про непосчитанный
    суммарный бюджет.
    """
    local_ads = [_make_local_ad("ad1")]
    decisions = [_make_scale_decision("ad1")]

    cfg = {**_base_cfg(), "scale_enabled": True}

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.plan_reader.read_general_plan", return_value=_VALID_PLAN), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=200_000.0), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=None), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler._record_scaled_at"), \
         patch("services.budget_scaler.set_adset_budget", return_value=True) as mock_set, \
         patch("services.notifications.send_telegram") as mock_tg, \
         patch("services.notifications.send_critical_alert") as mock_alert:

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="active")

    # Ноль подъёмов: денежная мутация не вызывалась вообще
    mock_set.assert_not_called()
    assert result["scaled"] == []
    assert result["recommendations"] == []
    assert result["ran"] is True
    assert result["skipped_reason"] == "total_budget_uncounted"
    # Это мягкий сигнал, а не падение крона — критический алерт НЕ шлём
    mock_alert.assert_not_called()
    # Честная строка в отчёте
    report_texts = " || ".join(str(c.args[0]) for c in mock_tg.call_args_list if c.args)
    assert "не удалось посчитать текущий суммарный бюджет" in report_texts


# ---------------------------------------------------------------------------
# Тест 11: адсет PAUSED не масштабируется
# ---------------------------------------------------------------------------

def test_paused_adset_not_scaled():
    """Адсет со статусом PAUSED не получает повышение бюджета."""
    local_ads = [_make_local_ad("ad1")]
    decisions = [_make_scale_decision("ad1")]

    cfg = {**_base_cfg(), "scale_enabled": True}
    all_budgets = {"adset1": {"daily_budget_usd": 100.0, "effective_status": "PAUSED", "name": "Паузный"}}

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler.set_adset_budget") as mock_set, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="active")

    mock_set.assert_not_called()
    assert result["recommendations"] == []
    assert result["scaled"] == []


# ---------------------------------------------------------------------------
# Тест 12: _calc_new_budget никогда не возвращает меньше current
# ---------------------------------------------------------------------------

def test_calc_new_budget_never_decreases():
    """_calc_new_budget никогда не снижает бюджет."""
    from services.budget_scaler import _calc_new_budget

    # При любых разумных входных данных new >= current (start=current — первый подъём за день)
    for current in [10.0, 50.0, 100.0, 200.0, 299.0, 300.0]:
        new = _calc_new_budget(
            current, start_budget_usd=current, effective_pct=20, max_mult=2.0, max_abs_usd=300,
        )
        assert new >= current, f"current={current} → new={new} < current"


# ---------------------------------------------------------------------------
# Тест 13: кулдаун < 4ч блокирует active-повышение
# ---------------------------------------------------------------------------

def test_cooldown_blocks_active_scaling():
    """Если с последнего реального повышения прошло < 4ч — active блокируется."""
    cfg = {**_base_cfg(), "scale_enabled": True}
    cooldown_reason = "cooldown: последнее повышение 1.5ч назад, ещё 150 мин"

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.budget_scaler._is_in_cooldown", return_value=(True, cooldown_reason)):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="active")

    assert result["ran"] is False
    assert result["skipped_reason"] == cooldown_reason


def test_cooldown_does_not_block_dry_run():
    """Кулдаун не влияет на dry_run — рекомендации доступны всегда."""
    local_ads = [_make_local_ad("ad1")]
    decisions = [_make_scale_decision("ad1")]
    all_budgets = {"adset1": _adset_info(100.0)}
    cfg = _base_cfg()

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler.set_adset_budget") as mock_set, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):
        # Намеренно НЕ мокаем _is_in_cooldown — в dry_run он не вызывается

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is True
    assert result["mode"] == "dry_run"
    mock_set.assert_not_called()


def test_cooldown_logic_direct():
    """_is_in_cooldown: < 4ч → True, > 4ч → False, None → False."""
    import services.budget_scaler as bs_module

    now = datetime.now(_TZ)

    # Случай 1: повышение 1 час назад → кулдаун активен
    last_1h = (now - timedelta(hours=1)).isoformat()
    with patch.object(bs_module, "_load_scale_state", return_value={"last_scaled_at": last_1h}):
        in_cd, reason = bs_module._is_in_cooldown()
    assert in_cd is True
    assert reason is not None
    assert "cooldown" in reason

    # Случай 2: повышение 5 часов назад → кулдаун прошёл
    last_5h = (now - timedelta(hours=5)).isoformat()
    with patch.object(bs_module, "_load_scale_state", return_value={"last_scaled_at": last_5h}):
        in_cd, reason = bs_module._is_in_cooldown()
    assert in_cd is False
    assert reason is None

    # Случай 3: нет last_scaled_at → кулдаун не активен
    with patch.object(bs_module, "_load_scale_state", return_value={"last_scaled_at": None}):
        in_cd, reason = bs_module._is_in_cooldown()
    assert in_cd is False


# ---------------------------------------------------------------------------
# Тест 14: CAMPAIGN_PAUSED адсет не масштабируется (ФИКС 3)
# ---------------------------------------------------------------------------

def test_campaign_paused_adset_not_scaled():
    """Адсет со статусом CAMPAIGN_PAUSED не масштабируется.

    До фикса 3 проверялось только == 'PAUSED'; CAMPAIGN_PAUSED проходил.
    После фикса: любой статус != 'ACTIVE' блокирует повышение.
    """
    local_ads = [_make_local_ad("ad1")]
    decisions = [_make_scale_decision("ad1")]

    cfg = {**_base_cfg(), "scale_enabled": True}
    # Адсет имеет статус CAMPAIGN_PAUSED (кампания на паузе, адсет технически активен)
    all_budgets = {
        "adset1": {"daily_budget_usd": 100.0, "effective_status": "CAMPAIGN_PAUSED", "name": "Кампания на паузе"}
    }

    with patch("services.budget_scaler.get_scale_config", return_value=cfg), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=local_ads), \
         patch("services.decision_policy.score_and_decide", return_value=decisions), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler._is_in_cooldown", return_value=(False, None)), \
         patch("services.budget_scaler.set_adset_budget") as mock_set, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="active")

    # CAMPAIGN_PAUSED — нельзя масштабировать
    mock_set.assert_not_called()
    assert result["recommendations"] == []
    assert result["scaled"] == []
