"""
Тесты для новых правил Стража в services/decision_policy.py (ARCH-phase1-guardian, G1):
early_waster (ранний слив день 1-3) и wasted_no_crm (дыра CRM-сверки).

См. docs/specs/ARCH-phase1-guardian.md §6.2 и §9 (таблица сценариев).
Дополнительно проверяем, что существующее правило confirmed_waster не сломано.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.decision_policy import score_and_decide


# ---------------------------------------------------------------------------
# Вспомогательная функция создания объявления (аналог _make_ad из test_decision_policy.py,
# расширена полями day_since_launch/outcomes_matched_at/payments для сценариев Стража).
# ---------------------------------------------------------------------------

def _make_ad(
    ad_id: str = "ad_1",
    ad_name: str = "CityA | Петров / Тест",
    adset_id: str = "adset_1",
    city: str = "CityA",
    adset_type: str = "L2",
    spend: float = 30.0,
    leads: int = 5,
    cpl: float = 6.0,
    ctr: float = 1.5,
    hook_rate: float = 30.0,
    qual_pct: float | None = None,
    romi: float | None = None,
    days_running: int = 5,
    day_since_launch: int | None = None,
    outcomes_matched_at: str | None = None,
    payments: int | None = None,
    impressions: int = 10000,
) -> dict:
    ad = {
        "ad_id": ad_id,
        "ad_name": ad_name,
        "adset_id": adset_id,
        "city": city,
        "adset_type": adset_type,
        "spend": spend,
        "leads": leads,
        "cpl": cpl,
        "ctr": ctr,
        "hook_rate": hook_rate,
        "qual_pct": qual_pct,
        "romi": romi,
        "days_running": days_running,
        "impressions": impressions,
        "video_views_3s": 0,
        "video_p25": 0,
        "video_p50": 0,
        "video_p75": 0,
        "video_p100": 0,
        "outcomes_matched_at": outcomes_matched_at,
        "payments": payments,
    }
    if day_since_launch is not None:
        ad["day_since_launch"] = day_since_launch
    return ad


def _result_for(results: list[dict], ad_id: str) -> dict:
    return next(r for r in results if r["ad_id"] == ad_id)


# ---------------------------------------------------------------------------
# early_waster — День 1 (zero-lead)
# ---------------------------------------------------------------------------

def test_early_waster_day1_zero_lead_diagnostic_when_dry_run_off():
    """day=1, spend=30, leads=0 → флаг и диагностика, но не PAUSE."""
    ad = _make_ad(
        ad_id="d1_zero",
        adset_id="adset_a",
        day_since_launch=1,
        days_running=1,
        spend=30.0,
        leads=0,
        qual_pct=None,
    )
    # Второй ад в ТОМ ЖЕ adset с нормальными показателями — чтобы guardrail
    # «последняя реклама в adset» не отменил PAUSE слива (иначе он остался бы
    # единственным в adset_a и guardrail вернул бы его в KEEP).
    sibling = _make_ad(
        ad_id="sibling",
        adset_id="adset_a",
        day_since_launch=5,
        days_running=5,
        leads=8,
        qual_pct=25.0,
        romi=120.0,
    )

    results = score_and_decide([ad, sibling], thresholds={"early_dry_run": False})
    r = _result_for(results, "d1_zero")

    assert r["is_early_waster"] is True, r
    assert r["action"] != "PAUSE", f"action={r['action']}, reasons={r['reasons']}"
    reasons_text = " ".join(r["reasons"]).lower()
    assert "diagnostic" in reasons_text, r["reasons"]
    assert "ранний слив" in reasons_text, r["reasons"]


def test_early_waster_day1_dry_run_marks_but_not_pauses():
    """тот же кейс, early_dry_run=True (дефолт) → action!=PAUSE, флаг True, reasons содержит dry_run."""
    ad = _make_ad(
        ad_id="d1_zero_dry",
        adset_id="adset_a",
        day_since_launch=1,
        days_running=1,
        spend=30.0,
        leads=0,
        qual_pct=None,
    )
    filler = _make_ad(ad_id="filler", adset_id="adset_b", day_since_launch=5, days_running=5)

    results = score_and_decide([ad, filler])  # дефолты: early_dry_run=True
    r = _result_for(results, "d1_zero_dry")

    assert r["is_early_waster"] is True, r
    assert r["action"] != "PAUSE", f"dry_run не должен паузить: {r}"
    reasons_text = " ".join(r["reasons"]).lower()
    assert "dry_run" in reasons_text, r["reasons"]
    assert "ранний слив" in reasons_text, r["reasons"]


def test_early_waster_day1_small_spend_not_flagged():
    """day=1, spend=10 (< early_min_spend=15) → is_early_waster=False."""
    ad = _make_ad(
        ad_id="small_spend",
        day_since_launch=1,
        days_running=1,
        spend=10.0,
        leads=0,
    )
    results = score_and_decide([ad], thresholds={"early_dry_run": False})
    r = _result_for(results, "small_spend")

    assert r["is_early_waster"] is False, r


def test_early_waster_day1_has_lead_not_flagged():
    """day=1, spend=40, leads=2 (есть лид) → is_early_waster=False."""
    ad = _make_ad(
        ad_id="has_lead",
        day_since_launch=1,
        days_running=1,
        spend=40.0,
        leads=2,
    )
    results = score_and_decide([ad], thresholds={"early_dry_run": False})
    r = _result_for(results, "has_lead")

    assert r["is_early_waster"] is False, r


def test_early_waster_learning_phase_not_flagged():
    """day=0, days_running=0 (моложе early_min_age_hours) → is_early_waster=False, несмотря на аномалию."""
    ad = _make_ad(
        ad_id="learning_phase",
        day_since_launch=0,
        days_running=0,
        spend=30.0,
        leads=0,
    )
    results = score_and_decide([ad], thresholds={"early_dry_run": False})
    r = _result_for(results, "learning_phase")

    assert r["is_early_waster"] is False, r
    assert r["action"] != "PAUSE" or "ранний слив" not in " ".join(r["reasons"]).lower()


# ---------------------------------------------------------------------------
# early_waster — День 2-3 (CPL x медианы города)
# ---------------------------------------------------------------------------

def test_early_waster_day23_expensive_cpl_diagnostic_when_dry_run_off():
    """day=2, cpl выше медианы ×3 даёт флаг, но не PAUSE."""
    ad = _make_ad(
        ad_id="d2_expensive",
        adset_id="adset_c",
        city="CityA",
        day_since_launch=2,
        days_running=2,
        spend=25.0,
        leads=1,
        cpl=20.0,
    )
    # Второй ад в ТОМ ЖЕ adset (нормальный) — чтобы guardrail не вернул слив в KEEP,
    # а peer1/peer2 в других adset формируют медиану CPL города ~4.
    sibling = _make_ad(
        ad_id="sibling",
        adset_id="adset_c",
        city="CityA",
        day_since_launch=5,
        days_running=5,
        leads=8,
        qual_pct=25.0,
        romi=120.0,
    )
    peer1 = _make_ad(ad_id="peer1", adset_id="adset_d", city="CityA", cpl=4.0, day_since_launch=10, days_running=10)
    peer2 = _make_ad(ad_id="peer2", adset_id="adset_e", city="CityA", cpl=4.2, day_since_launch=10, days_running=10)

    results = score_and_decide([ad, sibling, peer1, peer2], thresholds={"early_dry_run": False})
    r = _result_for(results, "d2_expensive")

    assert r["is_early_waster"] is True, r
    assert r["action"] != "PAUSE", f"action={r['action']}, reasons={r['reasons']}"
    assert "diagnostic" in " ".join(r["reasons"]).lower(), r["reasons"]


def test_early_waster_day23_normal_cpl_not_flagged():
    """day=2, cpl=5, медиана 4 (порог ×3=12) → cpl не превышает порог → is_early_waster=False."""
    ad = _make_ad(
        ad_id="d2_normal",
        city="CityA",
        day_since_launch=2,
        days_running=2,
        spend=25.0,
        leads=5,
        cpl=5.0,
    )
    peer1 = _make_ad(ad_id="peer1", city="CityA", cpl=4.0, day_since_launch=10, days_running=10)
    peer2 = _make_ad(ad_id="peer2", city="CityA", cpl=4.2, day_since_launch=10, days_running=10)

    results = score_and_decide([ad, peer1, peer2], thresholds={"early_dry_run": False})
    r = _result_for(results, "d2_normal")

    assert r["is_early_waster"] is False, r


def test_early_waster_day23_no_city_median_not_flagged():
    """Единственное объявление города (нет других cpl>0 для сравнения) → нет медианы → is_early_waster=False."""
    ad = _make_ad(
        ad_id="lonely_city",
        city="CityX",  # город, для которого больше нет объявлений с cpl>0
        day_since_launch=2,
        days_running=2,
        spend=25.0,
        leads=1,
        cpl=50.0,  # даже очень дорогой CPL — сравнивать не с чем
    )
    results = score_and_decide([ad], thresholds={"early_dry_run": False})
    r = _result_for(results, "lonely_city")

    assert r["is_early_waster"] is False, r


# ---------------------------------------------------------------------------
# early_waster — День 2-3, quality-override (дорогой лид, который
# платит/квалится — не слив)
# ---------------------------------------------------------------------------

def test_early_waster_day23_expensive_cpl_with_payments_not_flagged():
    """Дорогой CPL (как в базовом сценарии), но payments=1 (реальные деньги) → override, is_early_waster=False."""
    ad = _make_ad(
        ad_id="d2_paid",
        adset_id="adset_c",
        city="CityA",
        day_since_launch=2,
        days_running=2,
        spend=25.0,
        leads=1,
        cpl=20.0,
        payments=1,
    )
    sibling = _make_ad(
        ad_id="sibling",
        adset_id="adset_c",
        city="CityA",
        day_since_launch=5,
        days_running=5,
        leads=8,
        qual_pct=25.0,
        romi=120.0,
    )
    peer1 = _make_ad(ad_id="peer1", adset_id="adset_d", city="CityA", cpl=4.0, day_since_launch=10, days_running=10)
    peer2 = _make_ad(ad_id="peer2", adset_id="adset_e", city="CityA", cpl=4.2, day_since_launch=10, days_running=10)

    results = score_and_decide([ad, sibling, peer1, peer2], thresholds={"early_dry_run": False})
    r = _result_for(results, "d2_paid")

    assert r["is_early_waster"] is False, r


def test_early_waster_day23_expensive_cpl_with_good_qual_not_flagged():
    """Дорогой CPL, но qual_pct=20 (>= порога 15) → override, is_early_waster=False."""
    ad = _make_ad(
        ad_id="d2_qual_good",
        adset_id="adset_c",
        city="CityA",
        day_since_launch=2,
        days_running=2,
        spend=25.0,
        leads=1,
        cpl=20.0,
        qual_pct=20.0,
    )
    sibling = _make_ad(
        ad_id="sibling",
        adset_id="adset_c",
        city="CityA",
        day_since_launch=5,
        days_running=5,
        leads=8,
        qual_pct=25.0,
        romi=120.0,
    )
    peer1 = _make_ad(ad_id="peer1", adset_id="adset_d", city="CityA", cpl=4.0, day_since_launch=10, days_running=10)
    peer2 = _make_ad(ad_id="peer2", adset_id="adset_e", city="CityA", cpl=4.2, day_since_launch=10, days_running=10)

    results = score_and_decide([ad, sibling, peer1, peer2], thresholds={"early_dry_run": False})
    r = _result_for(results, "d2_qual_good")

    assert r["is_early_waster"] is False, r


def test_early_waster_day23_expensive_cpl_with_low_qual_still_flagged():
    """Дорогой CPL, qual_pct=5 (< порога 15) → override не применяется, is_early_waster=True (как раньше)."""
    ad = _make_ad(
        ad_id="d2_qual_low",
        adset_id="adset_c",
        city="CityA",
        day_since_launch=2,
        days_running=2,
        spend=25.0,
        leads=1,
        cpl=20.0,
        qual_pct=5.0,
    )
    sibling = _make_ad(
        ad_id="sibling",
        adset_id="adset_c",
        city="CityA",
        day_since_launch=5,
        days_running=5,
        leads=8,
        qual_pct=25.0,
        romi=120.0,
    )
    peer1 = _make_ad(ad_id="peer1", adset_id="adset_d", city="CityA", cpl=4.0, day_since_launch=10, days_running=10)
    peer2 = _make_ad(ad_id="peer2", adset_id="adset_e", city="CityA", cpl=4.2, day_since_launch=10, days_running=10)

    results = score_and_decide([ad, sibling, peer1, peer2], thresholds={"early_dry_run": False})
    r = _result_for(results, "d2_qual_low")

    assert r["is_early_waster"] is True, r
    assert r["action"] != "PAUSE", f"action={r['action']}, reasons={r['reasons']}"
    assert "diagnostic" in " ".join(r["reasons"]).lower(), r["reasons"]


def test_early_waster_day23_expensive_cpl_with_qual_none_still_flagged():
    """Дорогой CPL, qual_pct=None (нет данных) → override не применяется, is_early_waster=True (как раньше)."""
    ad = _make_ad(
        ad_id="d2_qual_none",
        adset_id="adset_c",
        city="CityA",
        day_since_launch=2,
        days_running=2,
        spend=25.0,
        leads=1,
        cpl=20.0,
        qual_pct=None,
    )
    sibling = _make_ad(
        ad_id="sibling",
        adset_id="adset_c",
        city="CityA",
        day_since_launch=5,
        days_running=5,
        leads=8,
        qual_pct=25.0,
        romi=120.0,
    )
    peer1 = _make_ad(ad_id="peer1", adset_id="adset_d", city="CityA", cpl=4.0, day_since_launch=10, days_running=10)
    peer2 = _make_ad(ad_id="peer2", adset_id="adset_e", city="CityA", cpl=4.2, day_since_launch=10, days_running=10)

    results = score_and_decide([ad, sibling, peer1, peer2], thresholds={"early_dry_run": False})
    r = _result_for(results, "d2_qual_none")

    assert r["is_early_waster"] is True, r
    assert r["action"] != "PAUSE", f"action={r['action']}, reasons={r['reasons']}"
    assert "diagnostic" in " ".join(r["reasons"]).lower(), r["reasons"]


# ---------------------------------------------------------------------------
# wasted_no_crm
# ---------------------------------------------------------------------------

def test_wasted_no_crm_is_diagnostic_when_dry_run_off():
    """wasted_no_crm остаётся флагом и диагностикой даже при dry_run=False."""
    ad = _make_ad(
        ad_id="wnc_trigger",
        adset_id="adset_f",
        spend=200.0,
        leads=15,
        qual_pct=None,
        days_running=4,
        outcomes_matched_at=None,
    )
    # Второй ад в ТОМ ЖЕ adset (нормальный) — чтобы guardrail не вернул слив в KEEP.
    sibling = _make_ad(
        ad_id="sibling",
        adset_id="adset_f",
        days_running=5,
        leads=8,
        qual_pct=25.0,
        romi=120.0,
    )

    results = score_and_decide([ad, sibling], thresholds={"wnc_dry_run": False})
    r = _result_for(results, "wnc_trigger")

    assert r["is_wasted_no_crm"] is True, r
    assert r["action"] != "PAUSE", f"action={r['action']}, reasons={r['reasons']}"
    reasons_text = " ".join(r["reasons"]).lower()
    assert "diagnostic" in reasons_text, r["reasons"]
    assert "wasted_no_crm" in reasons_text or "0 матчей amo" in reasons_text or "сломана связка crm" in reasons_text, r["reasons"]


def test_wasted_no_crm_dry_run_marks_but_not_pauses():
    """тот же кейс, wnc_dry_run=True (дефолт) → action!=PAUSE, флаг True, reasons содержит dry_run."""
    ad = _make_ad(
        ad_id="wnc_dry",
        spend=200.0,
        leads=15,
        qual_pct=None,
        days_running=4,
        outcomes_matched_at=None,
    )
    results = score_and_decide([ad])  # дефолты: wnc_dry_run=True
    r = _result_for(results, "wnc_dry")

    assert r["is_wasted_no_crm"] is True, r
    assert r["action"] != "PAUSE", f"dry_run не должен паузить: {r}"
    reasons_text = " ".join(r["reasons"]).lower()
    assert "dry_run" in reasons_text, r["reasons"]
    assert "wasted_no_crm" in reasons_text, r["reasons"]


def test_wasted_no_crm_with_sync_not_flagged():
    """outcomes_matched_at задан (сверка проходила) → is_wasted_no_crm=False, даже при остальных условиях."""
    ad = _make_ad(
        ad_id="wnc_synced",
        spend=200.0,
        leads=15,
        qual_pct=None,
        days_running=4,
        outcomes_matched_at="2026-07-01T00:00:00",
    )
    results = score_and_decide([ad], thresholds={"wnc_dry_run": False})
    r = _result_for(results, "wnc_synced")

    assert r["is_wasted_no_crm"] is False, r


def test_wasted_no_crm_too_few_days_not_flagged():
    """days_running=2 (< wnc_min_days=3) → is_wasted_no_crm=False."""
    ad = _make_ad(
        ad_id="wnc_few_days",
        spend=200.0,
        leads=15,
        qual_pct=None,
        days_running=2,
        outcomes_matched_at=None,
    )
    results = score_and_decide([ad], thresholds={"wnc_dry_run": False})
    r = _result_for(results, "wnc_few_days")

    assert r["is_wasted_no_crm"] is False, r


def test_wasted_no_crm_too_few_leads_not_flagged():
    """leads=5 (< wnc_min_leads=10) → is_wasted_no_crm=False."""
    ad = _make_ad(
        ad_id="wnc_few_leads",
        spend=200.0,
        leads=5,
        qual_pct=None,
        days_running=4,
        outcomes_matched_at=None,
    )
    results = score_and_decide([ad], thresholds={"wnc_dry_run": False})
    r = _result_for(results, "wnc_few_leads")

    assert r["is_wasted_no_crm"] is False, r


# ---------------------------------------------------------------------------
# Флаги присутствуют ВСЕГДА (даже когда False) — контракт результата
# ---------------------------------------------------------------------------

def test_result_always_contains_guardian_flags():
    """Каждый результат score_and_decide содержит is_early_waster и is_wasted_no_crm (bool)."""
    ad = _make_ad(ad_id="plain", days_running=10, day_since_launch=10)
    results = score_and_decide([ad])
    r = results[0]

    assert "is_early_waster" in r
    assert "is_wasted_no_crm" in r
    assert isinstance(r["is_early_waster"], bool)
    assert isinstance(r["is_wasted_no_crm"], bool)
    assert "is_zero_leads_after_3d" in r
    assert isinstance(r["is_zero_leads_after_3d"], bool)


# ---------------------------------------------------------------------------
# Безусловный hard-stop: 3+ полных дня и свежий lifetime leads == 0
# ---------------------------------------------------------------------------

def _healthy_zero_rule_sibling() -> dict:
    """Сосед сохраняет adset непустым и не влияет на проверяемый hard-stop."""
    return _make_ad(
        ad_id="healthy_sibling",
        adset_id="zero_rule_adset",
        leads=8,
        qual_pct=30.0,
        romi=150.0,
        days_running=20,  # >= 14 дн.: гейт цикла оплаты тир A
        day_since_launch=20,
    )


def _score_zero_rule_ad(**overrides) -> dict:
    ad = _make_ad(
        ad_id="zero_candidate",
        adset_id="zero_rule_adset",
        leads=0,
        spend=0.0,
        cpl=0.0,
        days_running=3,
        day_since_launch=3,
    )
    ad.update(overrides)
    return _result_for(
        score_and_decide([ad, _healthy_zero_rule_sibling()]),
        "zero_candidate",
    )


def test_zero_leads_after_3d_day2_not_triggered():
    result = _score_zero_rule_ad(days_running=2, day_since_launch=2)

    assert result["is_zero_leads_after_3d"] is False
    assert not any("0 lifetime-лидов" in reason for reason in result["reasons"])


@pytest.mark.parametrize("age_days", [3, 4])
def test_zero_leads_after_3d_boundary_pauses(age_days):
    result = _score_zero_rule_ad(
        days_running=age_days,
        day_since_launch=age_days,
    )

    assert result["is_zero_leads_after_3d"] is True
    assert result["action"] == "PAUSE"
    assert result["reasons"][-1] == (
        "PAUSE: 3+ полных дня с запуска и 0 lifetime-лидов "
        f"(возраст {age_days} дн.)"
    )
    assert result["business_reason"] == (
        f"за {age_days} полных дн. с запуска не получено ни одного лида"
    )


def test_zero_leads_after_3d_one_lead_not_triggered():
    result = _score_zero_rule_ad(leads=1)

    assert result["is_zero_leads_after_3d"] is False
    assert not any("0 lifetime-лидов" in reason for reason in result["reasons"])


def test_zero_leads_after_3d_unknown_age_fail_closed():
    ad = _make_ad(ad_id="unknown_age", adset_id="zero_rule_adset", leads=0)
    ad.pop("days_running")

    result = _result_for(
        score_and_decide([ad, _healthy_zero_rule_sibling()]),
        "unknown_age",
    )

    assert result["is_zero_leads_after_3d"] is False


@pytest.mark.parametrize("day_since_launch,days_running", [(2, 4), (4, 2)])
def test_zero_leads_after_3d_conflicting_age_uses_minimum(
    day_since_launch,
    days_running,
):
    result = _score_zero_rule_ad(
        day_since_launch=day_since_launch,
        days_running=days_running,
    )

    assert result["is_zero_leads_after_3d"] is False


def test_zero_leads_after_3d_has_no_spend_or_quality_override():
    result = _score_zero_rule_ad(
        spend=0,
        cpl=0,
        qual_pct=100,
        payments=9,
        romi=9999,
        outcomes_matched_at="2026-07-01T00:00:00",
    )

    assert result["is_zero_leads_after_3d"] is True
    assert result["action"] == "PAUSE"


@pytest.mark.parametrize(
    "invalid_leads",
    [
        False,
        True,
        -1,
        -0.5,
        0.9,
        float("nan"),
        float("inf"),
        "0.9",
        "bad",
        "9" * 5000,
        None,
    ],
)
def test_zero_leads_strict_parser_rejects_invalid_without_exception(invalid_leads):
    result = _score_zero_rule_ad(leads=invalid_leads)

    assert result["is_zero_leads_after_3d"] is False
    assert not any("0 lifetime-лидов" in reason for reason in result["reasons"])


@pytest.mark.parametrize("exact_zero", [0, 0.0, "0", "+0"])
def test_zero_leads_strict_parser_accepts_lossless_zero(exact_zero):
    result = _score_zero_rule_ad(leads=exact_zero)

    assert result["is_zero_leads_after_3d"] is True
    assert result["action"] == "PAUSE"


@pytest.mark.parametrize(
    "invalid_age",
    [False, -1, 2.5, float("nan"), float("inf"), "2.5", "bad", "9" * 5000],
)
def test_zero_leads_invalid_ages_fail_closed_without_exception(invalid_age):
    result = _score_zero_rule_ad(
        day_since_launch=invalid_age,
        days_running=None,
    )

    assert result["is_zero_leads_after_3d"] is False


@pytest.mark.parametrize(
    "invalid_age",
    [False, -1, 2.5, float("nan"), float("inf"), "2.5", "bad", "9" * 5000],
)
def test_zero_leads_both_ages_invalid_fail_closed(invalid_age):
    """Оба поля возраста malformed одновременно — правило не срабатывает и не падает."""
    result = _score_zero_rule_ad(
        day_since_launch=invalid_age,
        days_running=invalid_age,
    )

    assert result["is_zero_leads_after_3d"] is False


def test_zero_leads_guardrail_preserves_flag_and_hard_stop_reason():
    ad = _make_ad(
        ad_id="lone_zero",
        adset_id="lone_adset",
        leads=0,
        days_running=3,
        day_since_launch=3,
    )

    result = score_and_decide([ad])[0]

    assert result["action"] == "KEEP"
    assert result["is_zero_leads_after_3d"] is True
    assert any("0 lifetime-лидов" in reason for reason in result["reasons"])
    assert any("последняя реклама" in reason for reason in result["reasons"])


@pytest.mark.parametrize("conflict", ["veto", "scale", "tier_b"])
def test_zero_leads_rule_wins_reason_over_other_actions(conflict):
    overrides = {}
    if conflict == "veto":
        overrides["ad_name"] = "Бонус — конфликт"
    elif conflict == "scale":
        overrides.update(early_leads=2, ctr=99.0, ad_name="Отзыв цена")
    else:
        overrides.update(
            spend=400.0,
            qual_pct=0.0,
            payments=0,
            outcomes_matched_at="2026-07-01T00:00:00",
        )

    result = _score_zero_rule_ad(**overrides)

    assert result["action"] == "PAUSE"
    assert result["is_zero_leads_after_3d"] is True
    assert result["business_reason"] == (
        "за 3 полных дн. с запуска не получено ни одного лида"
    )
    assert any("0 lifetime-лидов" in reason for reason in result["reasons"])
    if conflict == "tier_b":
        assert result["is_confirmed_waster"] is False
        assert result["is_tier_b_diagnostic"] is True


# ---------------------------------------------------------------------------
# Существующий confirmed_waster не сломан новыми правилами
# ---------------------------------------------------------------------------

def test_existing_confirmed_waster_still_works_with_guardian_rules():
    """Типичный слив тира A (дорогой, низкий квал, 0 оплат, много лидов) по-прежнему даёт
    PAUSE + is_confirmed_waster=True, даже когда объявление также могло бы попасть под
    guardian-правила (проверка отсутствия конфликта)."""
    ad = _make_ad(
        ad_id="tier_a_example",
        adset_id="adset_tier_a",
        spend=500.0,
        leads=50,
        qual_pct=10.0,
        payments=0,
        outcomes_matched_at="2026-06-01T00:00:00",
        # >= 14 дн.: гейт цикла оплаты тир A — моложе «оплат 0» не слив
        days_running=20,
        day_since_launch=20,
    )
    sibling = _make_ad(
        ad_id="tier_a_companion",
        adset_id="adset_tier_a",
        spend=30.0,
        leads=8,
        qual_pct=25.0,
        payments=1,
        outcomes_matched_at="2026-06-01T00:00:00",
        days_running=10,
        day_since_launch=10,
    )
    results = score_and_decide([ad, sibling])
    r = _result_for(results, "tier_a_example")

    assert r["action"] == "PAUSE"
    assert r.get("is_confirmed_waster") is True
    reasons_text = " ".join(r["reasons"]).lower()
    assert "тир a" in reasons_text, r["reasons"]
    # День 10 — вне окна early (day<=3), поэтому early_waster не должен сработать
    assert r["is_early_waster"] is False, r
