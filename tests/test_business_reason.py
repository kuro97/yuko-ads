"""
Тесты services/decision_policy.py::score_and_decide — новый ключ business_reason.

Часть редизайна Live-отчёта (ARCH-live-report-redesign): бизнес-причина (квалы, оплаты, деньги) должна собираться В МОМЕНТ
принятия решения, а не парситься из сырых reasons постфактум.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.decision_policy import score_and_decide, _cpl_vs_median_phrase


# ---------------------------------------------------------------------------
# Вспомогательная функция создания объявления (аналог test_decision_policy.py)
# ---------------------------------------------------------------------------

def _make_ad(
    ad_id: str = "ad_1",
    ad_name: str = "CityA | Тест / обзор",
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
    payments: int | None = None,
    outcomes_matched_at: str | None = None,
    days_running: int = 20,  # зрелый возраст: тир A требует >= 14 дн. (гейт цикла оплаты)
    early_leads: int | None = None,
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
        "payments": payments,
        "outcomes_matched_at": outcomes_matched_at,
        "days_running": days_running,
        "impressions": 10000,
        "video_views_3s": 0,
        "video_p25": 0,
        "video_p50": 0,
        "video_p75": 0,
        "video_p100": 0,
    }
    if early_leads is not None:
        ad["early_leads"] = early_leads
    return ad


# ---------------------------------------------------------------------------
# business_reason: ключ всегда присутствует
# ---------------------------------------------------------------------------

def test_business_reason_key_present():
    """Любой ад — ключ business_reason есть в результате, тип str."""
    ad = _make_ad()
    results = score_and_decide([ad])

    assert "business_reason" in results[0]
    assert isinstance(results[0]["business_reason"], str)


# ---------------------------------------------------------------------------
# qual_pct=0: диагностика без PAUSE/business_reason
# ---------------------------------------------------------------------------

def test_zero_qual_keeps_diagnostic_reason_without_pause_business_reason():
    """leads=8 и qual_pct=0 остаются диагностикой, а не бизнес-причиной PAUSE."""
    ad_bad = _make_ad(
        ad_id="zero_qual",
        adset_id="adset_test",
        leads=8,
        qual_pct=0.0,
        spend=72.03,
        cpl=9.0,  # без ярко выраженной разницы с медианой (одинокий в своей группе)
    )
    # Идентичный peer убирает независимую портфельную причину PAUSE.
    ad_peer = _make_ad(
        ad_id="zero_qual_peer",
        adset_id="adset_test",
        leads=8,
        qual_pct=0.0,
        spend=72.03,
        cpl=9.0,
    )
    results = score_and_decide([ad_bad, ad_peer])
    bad_result = next(r for r in results if r["ad_id"] == "zero_qual")

    assert bad_result["action"] != "PAUSE", bad_result
    assert bad_result["business_reason"] == ""
    diagnostic = " ".join(bad_result["reasons"]).lower()
    assert "diagnostic" in diagnostic
    assert "8 лидов" in diagnostic
    assert "qual_pct=0" in diagnostic


# ---------------------------------------------------------------------------
# business_reason: сравнение CPL с медианой пиринговой группы
# ---------------------------------------------------------------------------

def test_business_reason_cpl_vs_median():
    """CPL-сравнение остаётся в diagnostic reason, а не PAUSE business_reason."""
    expensive = _make_ad(
        ad_id="expensive",
        adset_id="adset_1",
        city="CityB",
        adset_type="L2",
        leads=8,
        qual_pct=0.0,
        cpl=8.2,
        spend=60.0,
    )
    peer1 = _make_ad(
        ad_id="peer1",
        adset_id="adset_2",
        city="CityB",
        adset_type="L2",
        leads=0,
        qual_pct=None,
        cpl=4.2,
        spend=10.0,
    )
    peer2 = _make_ad(
        ad_id="peer2",
        adset_id="adset_3",
        city="CityB",
        adset_type="L2",
        leads=0,
        qual_pct=None,
        cpl=4.2,
        spend=10.0,
    )
    results = score_and_decide([expensive, peer1, peer2])
    result = next(r for r in results if r["ad_id"] == "expensive")

    assert result["action"] != "PAUSE", result
    assert result["business_reason"] == ""
    diagnostic = " ".join(result["reasons"])
    assert "дороже медианы группы" in diagnostic
    assert "$8" in diagnostic
    assert "$4" in diagnostic


def test_cpl_vs_median_phrase_helper_no_data():
    """_cpl_vs_median_phrase: cpl/cpl_med None или 0 → пустая строка."""
    assert _cpl_vs_median_phrase(None, 4.2) == ""
    assert _cpl_vs_median_phrase(8.2, None) == ""
    assert _cpl_vs_median_phrase(0, 4.2) == ""
    assert _cpl_vs_median_phrase(8.2, 0) == ""


def test_cpl_vs_median_phrase_helper_small_diff_ignored():
    """Разница <30% — не пишем сравнение (незначительно)."""
    assert _cpl_vs_median_phrase(5.0, 4.2) == ""  # 5.0 < 1.3*4.2


# ---------------------------------------------------------------------------
# business_reason: подтверждённый слив
# ---------------------------------------------------------------------------

def test_business_reason_confirmed_waster():
    """spend=350, leads=20, qual_pct=5, payments=0 (сверка прошла) → упоминает
    «оплат 0» и расход через fmt_money."""
    waster = _make_ad(
        ad_id="waster",
        adset_id="adset_w",
        spend=350.0,
        leads=20,
        qual_pct=5.0,
        payments=0,
        outcomes_matched_at="2026-06-01T00:00:00",
    )
    other = _make_ad(ad_id="other", adset_id="adset_other", leads=8, qual_pct=25.0, romi=120.0)
    results = score_and_decide([waster, other])
    result = next(r for r in results if r["ad_id"] == "waster")

    assert result["is_confirmed_waster"] is True
    assert "оплат 0" in result["business_reason"], result["business_reason"]
    assert "$350" in result["business_reason"], result["business_reason"]


# ---------------------------------------------------------------------------
# business_reason: пусто для сильного SCALE-кандидата
# ---------------------------------------------------------------------------

def test_business_reason_empty_for_scale():
    """Сильный SCALE-кандидат (score>=5) → business_reason == ''."""
    good = _make_ad(
        ad_id="good",
        ad_name="CityA | Тест / отзыв клиента",
        early_leads=2,
        hook_rate=45.0,
        ctr=2.5,
        cpl=8.0,
    )
    weak = _make_ad(
        ad_id="weak",
        ad_name="CityA | Тест / обзор prodb",
        early_leads=0,
        hook_rate=15.0,
        ctr=0.8,
        cpl=3.0,
    )
    results = score_and_decide([good, weak])
    good_result = next(r for r in results if r["ad_id"] == "good")

    assert good_result["action"] == "SCALE", good_result
    assert good_result["business_reason"] == ""
