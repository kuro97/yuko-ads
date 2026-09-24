"""Тесты Decision Tree в analyzer.py — самая важная бизнес-логика."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.analyzer import apply_decision_tree, get_statuses_age_seconds
from agent.fb_common import _classify_objective


# --- Тесты get_statuses_age_seconds ---

def test_statuses_age_empty_cache():
    """Пустой кеш (ts=0) → None."""
    import agent.analyzer as _mod
    original_ts = _mod._status_cache["ts"]
    try:
        _mod._status_cache["ts"] = 0.0
        assert get_statuses_age_seconds() is None
    finally:
        _mod._status_cache["ts"] = original_ts


def test_statuses_age_returns_positive():
    """ts задан → возраст > 0."""
    import time
    import agent.analyzer as _mod
    original_ts = _mod._status_cache["ts"]
    try:
        _mod._status_cache["ts"] = time.time() - 300  # 5 минут назад
        age = get_statuses_age_seconds()
        assert age is not None
        assert age > 0
    finally:
        _mod._status_cache["ts"] = original_ts


# --- Правило 0: ROMI >= 200% → ОСТАВИТЬ (абсолютный приоритет) ---

def test_rule0_romi_overrides_cpl():
    """ROMI >= 200% — ОСТАВИТЬ, даже если CPL >= $30 и 5+ лидов."""
    ad = _make_ad(cpl=50, leads=10, romi=250)
    result = apply_decision_tree(ad)
    assert result["action"] == "ОСТАВИТЬ"


def test_rule0_romi_overrides_no_leads():
    """ROMI >= 200% — ОСТАВИТЬ, даже если 0 лидов и >7 дней."""
    ad = _make_ad(days_running=10, leads=0, cpl=0, romi=300)
    result = apply_decision_tree(ad)
    assert result["action"] == "ОСТАВИТЬ"


def _make_ad(**overrides) -> dict:
    """Создаёт объявление с дефолтными значениями.
    spend=20 — значимый расход (>= $15), чтобы правило 0.5 не блокировало тесты.
    """
    ad = {
        "days_running": 5,
        "leads": 3,
        "cpl": 15.0,
        "spend": 20.0,
        "qual_pct": None,
        "romi": None,
        "payments": None,
    }
    ad.update(overrides)
    return ad


# --- Правило 1: >7 дней, 0 лидов → ОТКЛЮЧИТЬ ---

def test_rule1_old_ad_no_leads():
    """Правило 1: >7 дней и 0 лидов — ОТКЛЮЧИТЬ."""
    ad = _make_ad(days_running=10, leads=0, cpl=0)
    result = apply_decision_tree(ad)
    assert result["action"] == "ОТКЛЮЧИТЬ"


def test_rule1_boundary_exactly_7_days():
    """Граница: ровно 7 дней, 0 лидов — НЕ отключать (условие >7, не >=7)."""
    ad = _make_ad(days_running=7, leads=0, cpl=0)
    result = apply_decision_tree(ad)
    assert result["action"] != "ОТКЛЮЧИТЬ"


# --- Правило 2: CPL >= $30 AND leads >= 5 → ОТКЛЮЧИТЬ ---

def test_rule2_expensive_with_enough_leads():
    """Правило 2: CPL >= $30 и leads >= 5 — ОТКЛЮЧИТЬ."""
    ad = _make_ad(cpl=35, leads=6)
    result = apply_decision_tree(ad)
    assert result["action"] == "ОТКЛЮЧИТЬ"


def test_rule2_cheap_with_enough_leads():
    """CPL $25 и 6 лидов — НЕ правило 2 (CPL < 30)."""
    ad = _make_ad(cpl=25, leads=6)
    result = apply_decision_tree(ad)
    assert result["action"] != "ОТКЛЮЧИТЬ" or "CPL" not in result.get("reason", "")


def test_rule2_boundary_exactly_30_cpl_5_leads():
    """Граница: CPL ровно $30, ровно 5 лидов — ОТКЛЮЧИТЬ (>= 30 и >= 5)."""
    ad = _make_ad(cpl=30, leads=5)
    result = apply_decision_tree(ad)
    assert result["action"] == "ОТКЛЮЧИТЬ"


# --- Правило 3: leads < 10 ---

@pytest.mark.parametrize("leads", [1, 4])
def test_rule3_high_cpl_below_min_leads_waits(leads):
    """CPL $30+ не отключает рекламу до 5 лидов."""
    ad = _make_ad(leads=leads, cpl=32)
    result = apply_decision_tree(ad)
    assert result == {
        "action": "ЖДАТЬ",
        "reason": "Нет данных по квалам — нужен ввод из AMO",
    }


def test_rule3_old_bad_qual_no_payments():
    """>7 дн, квал < 20%, 0 оплат — ОТКЛЮЧИТЬ."""
    ad = _make_ad(days_running=8, leads=5, cpl=15, qual_pct=10, payments=0)
    result = apply_decision_tree(ad)
    assert result["action"] == "ОТКЛЮЧИТЬ"


@pytest.mark.parametrize("leads", [1, 4])
def test_rule3_raw_quality_below_min_sample_is_diagnostic_only(leads):
    """Сырой квал/ROMI не отключает рекламу до min_leads_to_judge."""
    ad = _make_ad(
        days_running=8,
        leads=leads,
        cpl=15,
        qual_pct=0,
        romi=0,
        payments=0,
    )
    result = apply_decision_tree(ad)
    assert result == {"action": "ЖДАТЬ", "reason": "Копим данные"}


def test_rule3_raw_quality_uses_configured_min_sample_boundary():
    """Пользовательский min_leads_to_judge применяется на точной границе."""
    thresholds = {"min_leads_to_judge": 7}
    immature = _make_ad(
        days_running=8,
        leads=6,
        cpl=15,
        qual_pct=0,
        romi=0,
        payments=0,
    )
    mature = {**immature, "leads": 7}

    assert apply_decision_tree(immature, thresholds)["action"] == "ЖДАТЬ"
    assert apply_decision_tree(mature, thresholds)["action"] == "ОТКЛЮЧИТЬ"


def test_rule3_good_qual_no_payments():
    """Квал >= 20%, нет оплат — ЖДАТЬ."""
    ad = _make_ad(leads=5, cpl=15, qual_pct=25, payments=0)
    result = apply_decision_tree(ad)
    assert result["action"] == "ЖДАТЬ"


def test_rule3_no_qual_data():
    """Нет данных по квалам — ЖДАТЬ (нужен ввод из AMO)."""
    ad = _make_ad(leads=5, cpl=15, qual_pct=None)
    result = apply_decision_tree(ad)
    assert result["action"] == "ЖДАТЬ"


# --- Правило 4: leads >= 10 ---

def test_rule4_high_romi():
    """ROMI >= 200% — ОСТАВИТЬ."""
    ad = _make_ad(leads=15, cpl=10, romi=250)
    result = apply_decision_tree(ad)
    assert result["action"] == "ОСТАВИТЬ"


def test_rule4_low_romi():
    """ROMI < 200% — ОТКЛЮЧИТЬ."""
    ad = _make_ad(leads=15, cpl=10, romi=150)
    result = apply_decision_tree(ad)
    assert result["action"] == "ОТКЛЮЧИТЬ"


def test_rule4_good_qual_no_payments():
    """Квал >= 20%, нет оплат, leads >= 10 — ЖДАТЬ."""
    ad = _make_ad(leads=15, cpl=10, qual_pct=25, payments=0)
    result = apply_decision_tree(ad)
    assert result["action"] == "ЖДАТЬ"


def test_rule4_low_qual():
    """Квал < 20%, leads >= 10 — ОТКЛЮЧИТЬ."""
    ad = _make_ad(leads=15, cpl=10, qual_pct=15)
    result = apply_decision_tree(ad)
    assert result["action"] == "ОТКЛЮЧИТЬ"


def test_rule4_no_data():
    """Нет данных по ROMI/квалам, leads >= 10 — ЖДАТЬ."""
    ad = _make_ad(leads=15, cpl=10, qual_pct=None, romi=None)
    result = apply_decision_tree(ad)
    assert result["action"] == "ЖДАТЬ"


# --- Тесты кастомных порогов (thresholds) ---

def test_custom_max_cpl_raises_threshold():
    """cpl=35, leads=6 без ROMI → дефолт ОТКЛЮЧИТЬ, max_cpl=50 → НЕ ОТКЛЮЧИТЬ."""
    ad = _make_ad(cpl=35, leads=6, romi=None)

    # С дефолтными порогами (max_cpl=30) — должен ОТКЛЮЧИТЬ
    result_default = apply_decision_tree(ad)
    assert result_default["action"] == "ОТКЛЮЧИТЬ", (
        f"Ожидали ОТКЛЮЧИТЬ по умолчанию, получили {result_default}"
    )

    # С кастомным max_cpl=50 — CPL=35 не дотягивает до порога, ОТКЛЮЧИТЬ не должен
    result_custom = apply_decision_tree(ad, thresholds={"max_cpl": 50})
    assert result_custom["action"] != "ОТКЛЮЧИТЬ", (
        f"Ожидали НЕ ОТКЛЮЧИТЬ при max_cpl=50, получили {result_custom}"
    )


def test_custom_min_romi_keeps_ad():
    """thresholds={'min_romi': 100}, romi=150 → ОСТАВИТЬ (150 >= 100)."""
    ad = _make_ad(leads=15, cpl=10, romi=150)

    # С дефолтными порогами (min_romi=200) — ROMI 150% недостаточно → ОТКЛЮЧИТЬ
    result_default = apply_decision_tree(ad)
    assert result_default["action"] == "ОТКЛЮЧИТЬ", (
        f"Ожидали ОТКЛЮЧИТЬ по умолчанию (romi=150 < 200), получили {result_default}"
    )

    # С кастомным min_romi=100 — ROMI 150% >= 100% → ОСТАВИТЬ
    result_custom = apply_decision_tree(ad, thresholds={"min_romi": 100})
    assert result_custom["action"] == "ОСТАВИТЬ", (
        f"Ожидали ОСТАВИТЬ при min_romi=100, получили {result_custom}"
    )


# ---------------------------------------------------------------------------
# Тест: light=True не роняет прогон при отсутствии adset_id/adset/campaign
# ---------------------------------------------------------------------------

def test_get_ads_with_metrics_light_no_adset_fields():
    """get_ads_with_metrics(light=True) не падает KeyError когда в ответе get_all_ads
    отсутствуют поля adset_id/adset/campaign (типичный light-режим без extra_fields)."""

    # Два объявления — только базовые поля (как возвращает _collect_ads при parse_extra=False)
    fake_ads = {
        "ad_111": {
            "name": "Объявление 1",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "created_time": "2026-01-01T00:00:00+0000",
            "city": "CityA",
            "adset_type": "leads",
            # adset_id, campaign_name, adset_name, ad_objective, creative_id,
            # thumbnail_url — намеренно отсутствуют (light-режим)
        },
        "ad_222": {
            "name": "Объявление 2",
            "status": "PAUSED",
            "effective_status": "PAUSED",
            "created_time": "2026-02-01T00:00:00+0000",
            "city": "CityB",
            "adset_type": "traffic",
        },
    }

    # Метрики по обоим объявлениям
    fake_insights = {
        "ad_111": {"spend": 150.0, "leads": 5, "cpl": 30.0, "ctr": 2.5, "cpm": 80.0, "impressions": 1000, "clicks": 25},
        "ad_222": {"spend": 80.0,  "leads": 2, "cpl": 40.0, "ctr": 1.0, "cpm": 60.0, "impressions": 500,  "clicks": 5},
    }

    with patch("agent.analyzer.build_adset_map", return_value={}), \
         patch("agent.analyzer.get_all_ads", return_value=fake_ads), \
         patch("agent.analyzer._get_account_insights", return_value=fake_insights):

        from agent.analyzer import get_ads_with_metrics
        # Должно выполниться без KeyError
        result = get_ads_with_metrics("2026-01-01", "2026-01-31", light=True)

    assert len(result) == 2, f"Ожидали 2 объявления, получили {len(result)}"

    ad1 = next(a for a in result if a["id"] == "ad_111")
    ad2 = next(a for a in result if a["id"] == "ad_222")

    # adset_id должен быть None (не KeyError), spend должен подтянуться
    assert ad1["adset_id"] is None
    assert ad1["spend"] == 150.0
    assert ad1["days_running"] >= 0

    assert ad2["adset_id"] is None
    assert ad2["spend"] == 80.0


# ---------------------------------------------------------------------------
# Тесты классификации ad_objective (_classify_objective)
# ---------------------------------------------------------------------------

def test_classify_objective_quality_lead_on_ad():
    """QUALITY_LEAD + ON_AD (новый тип FB лид-форм) → leadform."""
    assert _classify_objective("QUALITY_LEAD", "ON_AD") == "leadform"


def test_classify_objective_on_ad_wins_over_site_goal():
    """destination_type=ON_AD приоритетнее goal — даже LINK_CLICKS → leadform."""
    assert _classify_objective("LINK_CLICKS", "ON_AD") == "leadform"


def test_classify_objective_lead_generation_no_dest():
    """LEAD_GENERATION без destination_type → leadform (старый тип форм)."""
    assert _classify_objective("LEAD_GENERATION", "") == "leadform"


def test_classify_objective_link_clicks_website():
    """LINK_CLICKS + WEBSITE → site."""
    assert _classify_objective("LINK_CLICKS", "WEBSITE") == "site"


def test_classify_objective_offsite_conversions_website():
    """OFFSITE_CONVERSIONS + WEBSITE → site."""
    assert _classify_objective("OFFSITE_CONVERSIONS", "WEBSITE") == "site"


def test_classify_objective_empty_goal_empty_dest():
    """Нет данных → пустая строка."""
    assert _classify_objective("", "") == ""
