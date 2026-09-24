"""
Юнит-тесты портфельной логики apply_portfolio_decisions (agent/analyzer.py).
Все тесты работают с чистыми dict-объявлениями — никакого FB API.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.analyzer import apply_portfolio_decisions, _portfolio_group_key, _portfolio_score


# ---------------------------------------------------------------------------
# Вспомогательная функция создания объявления
# ---------------------------------------------------------------------------

def _make_ad(
    ad_id: str = "ad_1",
    city: str = "CityA",
    adset_type: str = "L2",
    ad_objective: str = "leadform",
    effective_status: str = "ACTIVE",
    spend: float = 30.0,
    leads: int = 10,
    cpl: float = 3.0,
    ctr: float = 2.0,
    qual_pct: float | None = None,
    romi: float | None = None,
    payments: int | None = None,
    recommendation: str = "ОТКЛЮЧИТЬ",
    reason: str = "тестовая причина",
) -> dict:
    return {
        "id": ad_id,
        "name": ad_id,
        "effective_status": effective_status,
        "city": city,
        "adset_type": adset_type,
        "ad_objective": ad_objective,
        "spend": spend,
        "leads": leads,
        "cpl": cpl,
        "ctr": ctr,
        "qual_pct": qual_pct,
        "romi": romi,
        "payments": payments,
        "recommendation": recommendation,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Тест: пустой список
# ---------------------------------------------------------------------------

def test_empty_list():
    """apply_portfolio_decisions([]) → возвращает [] без ошибок."""
    result = apply_portfolio_decisions([])
    assert result == []


# ---------------------------------------------------------------------------
# Тест: группа из 1 активного объявления — НИКОГДА ОТКЛЮЧИТЬ
# ---------------------------------------------------------------------------

def test_single_ad_never_disabled():
    """Группа из 1 ACTIVE с absolute=ОТКЛЮЧИТЬ → recommendation != ОТКЛЮЧИТЬ, reason содержит «Последнее в группе»."""
    ad = _make_ad(ad_id="solo", recommendation="ОТКЛЮЧИТЬ", reason="CPL слишком высокий")
    result = apply_portfolio_decisions([ad])
    assert len(result) == 1
    assert result[0]["recommendation"] != "ОТКЛЮЧИТЬ"
    assert "Последнее в группе" in result[0]["reason"]


def test_single_ad_keep_preserved():
    """Группа из 1 ACTIVE с absolute=ОСТАВИТЬ → остаётся ОСТАВИТЬ (не меняем)."""
    ad = _make_ad(ad_id="solo", recommendation="ОСТАВИТЬ", reason="ROMI отлично")
    result = apply_portfolio_decisions([ad])
    assert result[0]["recommendation"] == "ОСТАВИТЬ"


# ---------------------------------------------------------------------------
# Тест: квал 17% не-худший не отключается
# ---------------------------------------------------------------------------

def test_qual_17_not_worst_kept():
    """2 ad: A квал17% (absolute ОТКЛЮЧИТЬ), B квал12% → A не ОТКЛЮЧИТЬ (не худший по квалу)."""
    # A — absolute ОТКЛЮЧИТЬ, квал 17%
    ad_a = _make_ad(
        ad_id="A", qual_pct=17.0, romi=None,
        spend=30.0, leads=10, cpl=5.0, ctr=2.0,
        recommendation="ОТКЛЮЧИТЬ", reason="Квал 17%, ROMI низкий",
    )
    # B — absolute ОТКЛЮЧИТЬ, квал 12% (хуже)
    ad_b = _make_ad(
        ad_id="B", qual_pct=12.0, romi=None,
        spend=30.0, leads=10, cpl=8.0, ctr=1.0,
        recommendation="ОТКЛЮЧИТЬ", reason="Квал 12%, ROMI низкий",
    )
    result = apply_portfolio_decisions([ad_a, ad_b])
    ad_a_out = next(a for a in result if a["id"] == "A")
    # A не должна быть ОТКЛЮЧИТЬ — она не худшая (B хуже по комбинации qual+cpl+ctr)
    assert ad_a_out["recommendation"] != "ОТКЛЮЧИТЬ", (
        f"A не должна быть ОТКЛЮЧИТЬ, но получили: {ad_a_out['recommendation']} / {ad_a_out['reason']}"
    )


# ---------------------------------------------------------------------------
# Тест: нижний отключается когда есть явно лучший
# ---------------------------------------------------------------------------

def test_worst_disabled_when_better_exists():
    """3 ad с явным разрывом score, нижний spend≥15 leads≥5 → нижний = ОТКЛЮЧИТЬ."""
    # best_ad — лучший (высокий ROMI, высокий квал, низкий CPL)
    best_ad = _make_ad(
        ad_id="best", romi=300.0, qual_pct=50.0, cpl=2.0, ctr=5.0,
        spend=50.0, leads=20, recommendation="ОСТАВИТЬ", reason="ROMI 300%",
    )
    # mid_ad — средний
    mid_ad = _make_ad(
        ad_id="mid", romi=150.0, qual_pct=25.0, cpl=5.0, ctr=2.5,
        spend=30.0, leads=10, recommendation="ЖДАТЬ", reason="Квал ок",
    )
    # worst_ad — худший (низкий ROMI, низкий квал, высокий CPL), явный разрыв
    worst_ad = _make_ad(
        ad_id="worst", romi=10.0, qual_pct=5.0, cpl=25.0, ctr=0.5,
        spend=25.0, leads=8, recommendation="ОТКЛЮЧИТЬ", reason="Квал 5%, ROMI низкий",
    )
    result = apply_portfolio_decisions([best_ad, mid_ad, worst_ad])
    worst_out = next(a for a in result if a["id"] == "worst")
    # best сохранит ОСТАВИТЬ (absolute не понижаем), худший должен быть ОТКЛЮЧИТЬ
    best_out = next(a for a in result if a["id"] == "best")
    assert best_out["recommendation"] == "ОСТАВИТЬ"
    assert worst_out["recommendation"] == "ОТКЛЮЧИТЬ", (
        f"worst должен быть ОТКЛЮЧИТЬ, но: {worst_out['recommendation']} / {worst_out['reason']}"
    )


# ---------------------------------------------------------------------------
# Тест: мало расхода — не отключаем
# ---------------------------------------------------------------------------

def test_low_spend_not_disabled():
    """Кандидат worst, spend=$5 → ЖДАТЬ, reason «Мало данных»."""
    best_ad = _make_ad(
        ad_id="best", romi=300.0, qual_pct=50.0, cpl=2.0, ctr=5.0,
        spend=50.0, leads=20, recommendation="ОСТАВИТЬ", reason="ROMI 300%",
    )
    worst_ad = _make_ad(
        ad_id="worst", romi=10.0, qual_pct=5.0, cpl=25.0, ctr=0.5,
        spend=5.0,  # мало расхода
        leads=8, recommendation="ОТКЛЮЧИТЬ", reason="Квал 5%",
    )
    result = apply_portfolio_decisions([best_ad, worst_ad])
    worst_out = next(a for a in result if a["id"] == "worst")
    assert worst_out["recommendation"] == "ЖДАТЬ"
    assert "Мало данных" in worst_out["reason"]


# ---------------------------------------------------------------------------
# Тест: мало лидов — не отключаем
# ---------------------------------------------------------------------------

def test_low_leads_not_disabled():
    """Кандидат worst, leads=2 → ЖДАТЬ, reason «Мало данных»."""
    best_ad = _make_ad(
        ad_id="best", romi=300.0, qual_pct=50.0, cpl=2.0, ctr=5.0,
        spend=50.0, leads=20, recommendation="ОСТАВИТЬ", reason="ROMI 300%",
    )
    worst_ad = _make_ad(
        ad_id="worst", romi=10.0, qual_pct=5.0, cpl=25.0, ctr=0.5,
        spend=30.0, leads=2,  # мало лидов (< 5)
        recommendation="ОТКЛЮЧИТЬ", reason="Квал 5%",
    )
    result = apply_portfolio_decisions([best_ad, worst_ad])
    worst_out = next(a for a in result if a["id"] == "worst")
    assert worst_out["recommendation"] == "ЖДАТЬ"
    assert "Мало данных" in worst_out["reason"]


def test_raw_quality_portfolio_pause_starts_at_min_sample_boundary():
    """Portfolio не отключает по сырым qual/ROMI до 5 лидов, но видит границу 5."""
    def evaluate(leads: int) -> dict:
        best_ad = _make_ad(
            ad_id="best",
            romi=300.0,
            qual_pct=50.0,
            cpl=2.0,
            ctr=5.0,
            spend=50.0,
            leads=20,
            recommendation="ОСТАВИТЬ",
            reason="ROMI 300%",
        )
        candidate = _make_ad(
            ad_id="candidate",
            romi=0.0,
            qual_pct=0.0,
            cpl=15.0,
            ctr=0.5,
            spend=30.0,
            leads=leads,
            recommendation="ЖДАТЬ",
            reason="Сырой квал/ROMI",
        )
        result = apply_portfolio_decisions(
            [best_ad, candidate],
            thresholds={"portfolio_min_score_gap": 0.1},
        )
        return next(ad for ad in result if ad["id"] == "candidate")

    immature = evaluate(4)
    boundary = evaluate(5)

    assert immature["recommendation"] == "ЖДАТЬ"
    assert immature["reason"] == "Мало данных для портфельной оценки"
    assert boundary["recommendation"] == "ОТКЛЮЧИТЬ"


# ---------------------------------------------------------------------------
# Тест: все объявления одинаковые — никто не отключается
# ---------------------------------------------------------------------------

def test_all_equal_no_disable():
    """3 ad одинаковые метрики → никто не ОТКЛЮЧИТЬ (разрыв score=0 < gap)."""
    ads = [
        _make_ad(
            ad_id=f"ad_{i}", romi=100.0, qual_pct=20.0, cpl=10.0, ctr=2.0,
            spend=30.0, leads=10, recommendation="ОТКЛЮЧИТЬ", reason="ROMI низкий",
        )
        for i in range(3)
    ]
    result = apply_portfolio_decisions(ads)
    disabled = [a for a in result if a["recommendation"] == "ОТКЛЮЧИТЬ"]
    # При одинаковых метриках разрыв score=0 < min_score_gap=0.15 → никто не отключается
    assert len(disabled) == 0, f"Ожидали 0 ОТКЛЮЧИТЬ, получили {len(disabled)}: {disabled}"


# ---------------------------------------------------------------------------
# Тест: absolute ОСТАВИТЬ не понижается
# ---------------------------------------------------------------------------

def test_absolute_keep_preserved():
    """ad ROMI 300% (absolute ОСТАВИТЬ), worst rank → остаётся ОСТАВИТЬ."""
    # Этот ad имеет высокий ROMI — absolute дал ОСТАВИТЬ
    rich_ad = _make_ad(
        ad_id="rich", romi=300.0, qual_pct=50.0, cpl=2.0, ctr=5.0,
        spend=50.0, leads=20, recommendation="ОСТАВИТЬ", reason="ROMI 300%",
    )
    # Добавим ещё двух с явно лучшими метриками (но не ОСТАВИТЬ у absolute)
    # Они будут ранжированы выше, а rich — может оказаться нижним по score
    # (например если у них лучше всё кроме ROMI который у всех None)
    # Но у rich есть ROMI, а у других нет — значит разные шкалы AMO/no_amo
    other1 = _make_ad(
        ad_id="other1", romi=None, qual_pct=None, cpl=1.0, ctr=10.0,
        spend=30.0, leads=10, recommendation="ЖДАТЬ", reason="Нет AMO",
    )
    other2 = _make_ad(
        ad_id="other2", romi=None, qual_pct=None, cpl=1.5, ctr=9.0,
        spend=30.0, leads=10, recommendation="ЖДАТЬ", reason="Нет AMO",
    )
    result = apply_portfolio_decisions([rich_ad, other1, other2])
    rich_out = next(a for a in result if a["id"] == "rich")
    assert rich_out["recommendation"] == "ОСТАВИТЬ", (
        f"absolute ОСТАВИТЬ должно оставаться ОСТАВИТЬ, но: {rich_out['recommendation']}"
    )


# ---------------------------------------------------------------------------
# Тест: лучший в группе с absolute ОТКЛЮЧИТЬ → ОСТАВИТЬ
# ---------------------------------------------------------------------------

def test_best_in_group_not_disabled():
    """rank1 с absolute ОТКЛЮЧИТЬ → ОСТАВИТЬ «Лучшее в группе»."""
    best = _make_ad(
        ad_id="best", romi=200.0, qual_pct=30.0, cpl=3.0, ctr=4.0,
        spend=50.0, leads=15, recommendation="ОТКЛЮЧИТЬ", reason="Квал 30%, ROMI низкий",
    )
    worst = _make_ad(
        ad_id="worst", romi=10.0, qual_pct=5.0, cpl=25.0, ctr=0.5,
        spend=30.0, leads=8, recommendation="ОТКЛЮЧИТЬ", reason="Квал 5%",
    )
    result = apply_portfolio_decisions([best, worst])
    best_out = next(a for a in result if a["id"] == "best")
    assert best_out["recommendation"] == "ОСТАВИТЬ"
    assert "Лучшее в группе" in best_out["reason"]


# ---------------------------------------------------------------------------
# Тест: группа без AMO — fallback ранжирование по CPL/CTR
# ---------------------------------------------------------------------------

def test_no_amo_fallback_ranking():
    """Группа без AMO, разные CPL/CTR → нижний по CPL/CTR = ОТКЛЮЧИТЬ."""
    good = _make_ad(
        ad_id="good", romi=None, qual_pct=None, cpl=2.0, ctr=5.0,
        spend=30.0, leads=10, recommendation="ЖДАТЬ", reason="Нет AMO",
    )
    bad = _make_ad(
        ad_id="bad", romi=None, qual_pct=None, cpl=20.0, ctr=0.5,
        spend=30.0, leads=10, recommendation="ОТКЛЮЧИТЬ", reason="CPL высокий",
    )
    # Используем явно маленький порог gap, чтобы разрыв сработал
    thresholds = {"portfolio_min_score_gap": 0.1}
    result = apply_portfolio_decisions([good, bad], thresholds=thresholds)
    bad_out = next(a for a in result if a["id"] == "bad")
    good_out = next(a for a in result if a["id"] == "good")
    # good — лучший (rank 1), absolute был ЖДАТЬ → портфель не понижает (не ОТКЛЮЧИТЬ)
    assert good_out["recommendation"] != "ОТКЛЮЧИТЬ"
    # bad — худший с явным разрывом → ОТКЛЮЧИТЬ
    assert bad_out["recommendation"] == "ОТКЛЮЧИТЬ"


# ---------------------------------------------------------------------------
# Тест: AMO и без-AMO в одной группе — не сравниваем
# ---------------------------------------------------------------------------

def test_mixed_amo_no_amo_no_cross_compare():
    """1 ad с AMO + 1 без AMO — каждый без аналога в своей шкале → не ОТКЛЮЧИТЬ."""
    with_amo = _make_ad(
        ad_id="amo", romi=100.0, qual_pct=15.0, cpl=10.0, ctr=2.0,
        spend=30.0, leads=10, recommendation="ОТКЛЮЧИТЬ", reason="ROMI низкий",
    )
    without_amo = _make_ad(
        ad_id="no_amo", romi=None, qual_pct=None, cpl=10.0, ctr=2.0,
        spend=30.0, leads=10, recommendation="ОТКЛЮЧИТЬ", reason="CPL высокий",
    )
    result = apply_portfolio_decisions([with_amo, without_amo])
    amo_out = next(a for a in result if a["id"] == "amo")
    no_amo_out = next(a for a in result if a["id"] == "no_amo")
    # Каждый — одиночка в своей шкале, нет аналога для сравнения → не ОТКЛЮЧИТЬ
    assert amo_out["recommendation"] != "ОТКЛЮЧИТЬ", (
        f"ad с AMO без аналога не должен быть ОТКЛЮЧИТЬ: {amo_out}"
    )
    assert no_amo_out["recommendation"] != "ОТКЛЮЧИТЬ", (
        f"ad без AMO без аналога не должен быть ОТКЛЮЧИТЬ: {no_amo_out}"
    )


# ---------------------------------------------------------------------------
# Тест: не-активные не трогаются
# ---------------------------------------------------------------------------

def test_inactive_untouched():
    """ad PAUSED с recommendation=ВЕРНУТЬ — не изменяется портфелем."""
    paused = _make_ad(
        ad_id="paused", effective_status="PAUSED",
        recommendation="ВЕРНУТЬ", reason="На паузе",
    )
    result = apply_portfolio_decisions([paused])
    assert result[0]["recommendation"] == "ВЕРНУТЬ"
    assert result[0]["reason"] == "На паузе"


# ---------------------------------------------------------------------------
# Тест: ключ группы
# ---------------------------------------------------------------------------

def test_group_key():
    """_portfolio_group_key возвращает (city, adset_type, ad_objective)."""
    ad = _make_ad(city="CityA", adset_type="L2", ad_objective="leadform")
    key = _portfolio_group_key(ad)
    assert key == ("CityA", "L2", "leadform")


def test_group_key_empty_fields():
    """Пустые поля → ('', '', '')."""
    ad = {"city": "", "adset_type": None, "ad_objective": ""}
    key = _portfolio_group_key(ad)
    assert key == ("", "", "")


# ---------------------------------------------------------------------------
# Тест: min-max нормализация при одинаковых значениях → 0.5 (без деления на 0)
# ---------------------------------------------------------------------------

def test_score_minmax_equal_neutral():
    """В группе все cpl равны → norm = 0.5 (нет деления на 0)."""
    ads = [
        _make_ad(
            ad_id=f"ad_{i}", romi=None, qual_pct=None, cpl=10.0, ctr=2.0,
            spend=30.0, leads=10, recommendation="ОТКЛЮЧИТЬ", reason="тест",
        )
        for i in range(3)
    ]
    # Не должно бросать исключений
    result = apply_portfolio_decisions(ads)
    # При одинаковых метриках никто не отключается (разрыв = 0)
    assert all(a["recommendation"] != "ОТКЛЮЧИТЬ" for a in result)
