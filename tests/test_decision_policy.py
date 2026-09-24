"""
Тесты для services/decision_policy.py.
Проверяют: SCALE-скоринг, PAUSE-правила, VETO, guardrail последнего в adset,
видео-сигнал широкого входа.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.decision_policy import score_and_decide


# ---------------------------------------------------------------------------
# Вспомогательная функция создания объявления
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
    days_running: int = 20,  # зрелый возраст: тир A требует >= 14 дн. (гейт цикла оплаты 02.09)
    # Ранние данные — опционально
    early_leads: int | None = None,
    # Видео-поля
    impressions: int = 10000,
    video_views_3s: int = 0,
    video_p25: int = 0,
    video_p50: int = 0,
    video_p75: int = 0,
    video_p100: int = 0,
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
        "video_views_3s": video_views_3s,
        "video_p25": video_p25,
        "video_p50": video_p50,
        "video_p75": video_p75,
        "video_p100": video_p100,
    }
    if early_leads is not None:
        ad["early_leads"] = early_leads
    return ad


# ---------------------------------------------------------------------------
# Тест 1: SCALE — ранний лид + высокий hook + тема-отзыв
# ---------------------------------------------------------------------------

def test_scale_early_lead_high_hook_emotion_theme():
    """Объявление с ранним лидом + высоким hook_rate + темой «отзыв» → SCALE, score>=5, reasons перечислены."""
    # «Хороший» ад: ранний лид, hook выше медианы, тема «отзыв»
    good = _make_ad(
        ad_id="good",
        ad_name="CityA | Тест / отзыв клиента",
        early_leads=2,          # +3: есть лид за 3 дня
        hook_rate=45.0,         # выше медианы группы
        ctr=2.5,                # выше медианы группы
        cpl=8.0,                # выше медианы → +1
    )
    # «Слабый» ад в той же группе для формирования медиан
    weak = _make_ad(
        ad_id="weak",
        ad_name="CityA | Тест / предложение prodb",
        early_leads=0,
        hook_rate=15.0,         # ниже медианы
        ctr=0.8,                # ниже медианы
        cpl=3.0,                # ниже медианы
    )
    results = score_and_decide([good, weak])
    good_result = next(r for r in results if r["ad_id"] == "good")

    assert good_result["action"] == "SCALE", (
        f"Ожидали SCALE, получили {good_result['action']}. Score={good_result['score']}, reasons={good_result['reasons']}"
    )
    assert good_result["score"] >= 5, (
        f"Score должен быть >=5, получили {good_result['score']}. Reasons: {good_result['reasons']}"
    )
    # Проверяем, что причины содержательные
    all_reasons = " ".join(good_result["reasons"])
    assert "лид" in all_reasons.lower(), f"Причины должны упоминать лид: {good_result['reasons']}"
    assert len(good_result["reasons"]) >= 2, f"Должно быть 2+ причин: {good_result['reasons']}"


# ---------------------------------------------------------------------------
# Тест 2: qual_pct = 0 без зрелой выборки — только диагностика
# ---------------------------------------------------------------------------

def test_leads_with_zero_qual_are_diagnostic_only_even_after_matching():
    """qual_pct=0 и outcomes_matched_at не доказывают зрелость лидов."""
    ads = []
    for ad_id in ("zero_qual_1", "zero_qual_2"):
        ad = _make_ad(
            ad_id=ad_id,
            adset_id="adset_test",
            ad_name="CityA | Тест / предложение",
            leads=10,
            qual_pct=0.0,
            spend=50.0,
            days_running=10,
        )
        ad["payments"] = 0
        ad["outcomes_matched_at"] = "2026-07-01T00:00:00"
        ads.append(ad)

    results = score_and_decide(ads)

    assert all(result["action"] != "PAUSE" for result in results), results
    assert all(result["is_confirmed_waster"] is False for result in results), results
    assert all(
        "diagnostic" in " ".join(result["reasons"]).lower()
        and "qual_pct=0" in " ".join(result["reasons"])
        for result in results
    ), results


def test_tier_b_low_qual_is_diagnostic_only():
    """Tier B при spend>$150 не меняет action и confirmed-waster флаг."""
    ads = []
    for ad_id in ("tier_b_1", "tier_b_2"):
        ad = _make_ad(
            ad_id=ad_id,
            adset_id="adset_test",
            leads=10,
            qual_pct=5.0,
            spend=200.0,
            days_running=10,
        )
        ad["payments"] = 0
        ad["outcomes_matched_at"] = "2026-07-01T00:00:00"
        ads.append(ad)

    results = score_and_decide(ads)

    assert all(result["action"] != "PAUSE" for result in results), results
    assert all(result["is_confirmed_waster"] is False for result in results), results
    assert all(result["is_tier_b_diagnostic"] is True for result in results), results


# ---------------------------------------------------------------------------
# Тест 3: Тема «бонус» — veto, не SCALE
# ---------------------------------------------------------------------------

def test_bonus_theme_veto_no_scale():
    """Тема «бонус» блокирует только SCALE, но не даёт PAUSE."""
    bonus_ad = _make_ad(
        ad_id="bonus",
        ad_name="CityA | Бонус / PRODA бесплатно",
        early_leads=3,          # дало бы +3
        hook_rate=50.0,         # дало бы +2
        ctr=3.0,
        cpl=8.0,
    )
    weak = _make_ad(
        ad_id="weak",
        ad_name="Другой / предложение",
        hook_rate=50.0,
        ctr=3.0,
        cpl=8.0,
    )
    results = score_and_decide([bonus_ad, weak])
    bonus_result = next(r for r in results if r["ad_id"] == "bonus")

    assert bonus_result["action"] == "KEEP", bonus_result
    # Проверяем, что score=0 (вето блокирует начисление баллов) или причина упоминает бонус
    reasons_text = " ".join(bonus_result["reasons"])
    assert "бонус" in reasons_text.lower() or "veto" in reasons_text.lower() or "вето" in reasons_text.lower(), (
        f"Причина должна упоминать вето по «бонус»: {bonus_result['reasons']}"
    )
    assert bonus_result["score"] == 0, (
        f"При вето score должен быть 0, получили {bonus_result['score']}"
    )


def test_bonus_theme_does_not_override_age3_pause():
    """Независимое age3-правило сохраняет PAUSE для темы «бонус»."""
    bonus_ad = _make_ad(
        ad_id="bonus_age3",
        ad_name="Бонус — нулевой лид",
        adset_id="bonus_age3_set",
        leads=0,
        days_running=3,
    )
    replacement = _make_ad(
        ad_id="bonus_age3_replacement",
        adset_id="bonus_age3_set",
        leads=5,
        qual_pct=25.0,
    )

    result = next(
        item for item in score_and_decide([bonus_ad, replacement])
        if item["ad_id"] == "bonus_age3"
    )

    assert result["action"] == "PAUSE"
    assert result["is_zero_leads_after_3d"] is True
    assert any("0 lifetime-лидов" in reason for reason in result["reasons"])


def test_bonus_theme_does_not_override_tier_a_pause():
    """Независимый tier A сохраняет PAUSE для темы «бонус»."""
    bonus_ad = _make_ad(
        ad_id="bonus_tier_a",
        ad_name="Бонус — много лидов без оплат",
        adset_id="bonus_tier_a_set",
        spend=400.0,
        leads=20,
        qual_pct=13.0,
    )
    bonus_ad.update(payments=0, outcomes_matched_at="2026-07-01T00:00:00")
    replacement = _make_ad(
        ad_id="bonus_tier_a_replacement",
        adset_id="bonus_tier_a_set",
        leads=5,
        qual_pct=25.0,
    )

    result = next(
        item for item in score_and_decide([bonus_ad, replacement])
        if item["ad_id"] == "bonus_tier_a"
    )

    assert result["action"] == "PAUSE"
    assert result["is_confirmed_waster"] is True
    assert any("тир A" in reason for reason in result["reasons"])


# ---------------------------------------------------------------------------
# Тест 4: Guardrail — если все в adset PAUSE, один остаётся KEEP
# ---------------------------------------------------------------------------

def test_guardrail_last_in_adset_becomes_keep():
    """Если все объявления в одном adset получили PAUSE → одно остаётся KEEP."""
    # Два объявления tier A: оба имеют независимую причину PAUSE.
    ad1 = _make_ad(
        ad_id="ad_1",
        adset_id="adset_X",
        ad_name="CityA / предложение 1",
        leads=20,
        qual_pct=13.0,
        spend=400.0,
    )
    ad2 = _make_ad(
        ad_id="ad_2",
        adset_id="adset_X",
        ad_name="CityA / предложение 2",
        leads=20,
        qual_pct=13.0,
        spend=400.0,
    )
    for ad in (ad1, ad2):
        ad.update(payments=0, outcomes_matched_at="2026-07-01T00:00:00")
    results = score_and_decide([ad1, ad2])

    # Оба должны были стать PAUSE, но guardrail должен сохранить одного
    actions = [r["action"] for r in results]
    assert "KEEP" in actions, (
        f"Guardrail должен сохранить хотя бы одно KEEP в adset, но все: {actions}. "
        f"Results: {results}"
    )
    keep_count = actions.count("KEEP")
    assert keep_count >= 1, f"Должен быть хотя бы 1 KEEP, получили: {actions}"

    # Проверяем, что причина guardrail содержит нужное сообщение
    keep_results = [r for r in results if r["action"] == "KEEP"]
    guardrail_reason_found = any(
        "адсет" in " ".join(r["reasons"]).lower() or "guardrail" in " ".join(r["reasons"]).lower()
        for r in keep_results
    )
    assert guardrail_reason_found, (
        f"KEEP от guardrail должен иметь соответствующую причину: {keep_results}"
    )


# ---------------------------------------------------------------------------
# Тест 5: Видео-сигнал «широкий вход + неглубокий хвост» → +2
# ---------------------------------------------------------------------------

def test_video_wide_entry_shallow_tail_gets_bonus():
    """Видео с высоким video_p25/impressions И низким p100/p25 → +2. Видео с полным досмотром — нет."""
    # imp=10000, p25=4000 (40%), p100=400 (completion=10%) — широкий вход, малый хвост
    video_wide = _make_ad(
        ad_id="video_wide",
        ad_name="CityA / тест видео широкий",
        impressions=10000,
        video_p25=4000,   # 40% досмотрели 25% — высоко
        video_p100=400,   # completion ratio = 400/4000 = 10% — низко
        video_views_3s=5000,
    )
    # imp=10000, p25=1000 (10%), p100=800 (completion=80%) — узкий вход, глубокий досмотр
    video_deep = _make_ad(
        ad_id="video_deep",
        ad_name="CityA / тест видео глубокий",
        impressions=10000,
        video_p25=1000,   # 10% досмотрели 25% — ниже медианы
        video_p100=800,   # completion ratio = 800/1000 = 80% — высокий
        video_views_3s=2000,
    )
    results = score_and_decide([video_wide, video_deep])

    wide_result = next(r for r in results if r["ad_id"] == "video_wide")
    deep_result = next(r for r in results if r["ad_id"] == "video_deep")

    # Широкое видео должно получить бонус
    wide_reasons = " ".join(wide_result["reasons"])
    assert "широкий вход" in wide_reasons.lower() or "+2: видео" in wide_reasons.lower(), (
        f"Широкое видео должно получить +2 видео-бонус. Reasons: {wide_result['reasons']}"
    )

    # Глубокое видео — НЕ должно получить этот бонус
    deep_reasons = " ".join(deep_result["reasons"])
    assert "+2: видео" not in deep_reasons.lower() or "широкий вход" not in deep_reasons.lower(), (
        f"Глубокое видео не должно получить бонус широкого входа. Reasons: {deep_result['reasons']}"
    )

    # Score широкого видео должен быть выше на 2
    assert wide_result["score"] >= deep_result["score"] + 2, (
        f"wide score={wide_result['score']} должен быть >= deep score={deep_result['score']} + 2"
    )


# ---------------------------------------------------------------------------
# Дополнительный тест: базовый контракт функции
# ---------------------------------------------------------------------------

def test_return_structure():
    """score_and_decide возвращает список dict с нужными ключами."""
    ad = _make_ad(ad_id="test_structure")
    results = score_and_decide([ad])
    assert len(results) == 1
    r = results[0]
    assert "ad_id" in r
    assert "ad_name" in r
    assert "adset_id" in r
    assert "action" in r
    assert "score" in r
    assert "reasons" in r
    assert "is_tier_b_diagnostic" in r
    assert isinstance(r["is_tier_b_diagnostic"], bool)
    assert isinstance(r["reasons"], list)
    assert r["action"] in ("SCALE", "KEEP", "PAUSE")


def test_empty_list():
    """score_and_decide([]) → пустой список без ошибок."""
    assert score_and_decide([]) == []


def test_guardrail_different_adsets_independent():
    """Guardrail применяется независимо по каждому adset_id."""
    # adset_A: два tier A объявления → одно KEEP по guardrail.
    ad_a1 = _make_ad(ad_id="a1", adset_id="A", leads=20, qual_pct=13.0, spend=400.0)
    ad_a2 = _make_ad(ad_id="a2", adset_id="A", leads=20, qual_pct=13.0, spend=400.0)
    for ad in (ad_a1, ad_a2):
        ad.update(payments=0, outcomes_matched_at="2026-07-01T00:00:00")
    # adset_B: одно объявление с хорошим qual → не трогается guardrail
    ad_b1 = _make_ad(ad_id="b1", adset_id="B", leads=10, qual_pct=30.0, romi=150.0)

    results = score_and_decide([ad_a1, ad_a2, ad_b1])
    result_map = {r["ad_id"]: r for r in results}

    # В adset A: хотя бы одно KEEP (guardrail)
    a_actions = [result_map["a1"]["action"], result_map["a2"]["action"]]
    assert "KEEP" in a_actions, f"В adset A должен быть KEEP от guardrail: {a_actions}"

    # b1 с хорошим qual/romi → KEEP сам по себе
    assert result_map["b1"]["action"] in ("KEEP", "SCALE"), (
        f"b1 с хорошим qual должен быть KEEP/SCALE: {result_map['b1']}"
    )


# ---------------------------------------------------------------------------
# Тесты confirmed_waster (блокер 1): правило абсолютной паузы
# ---------------------------------------------------------------------------

def _make_waster_ad(
    ad_id: str = "waster_1",
    adset_id: str = "adset_w",
    spend: float = 400.0,
    payments: int = 0,
    qual_pct: float = 13.0,
    leads: int = 20,
    outcomes_matched_at: str | None = "2026-06-01T00:00:00",
) -> dict:
    """Объявление — подтверждённый слив tier A.

    B1: подтверждённый слив требует ФАКТА прошедшей сверки с AMO
    (outcomes_matched_at IS NOT NULL). По умолчанию сверка "прошла" —
    хелпер моделирует именно позитивный сценарий "слив подтверждён".
    """
    ad = _make_ad(
        ad_id=ad_id,
        adset_id=adset_id,
        spend=spend,
        leads=leads,
        qual_pct=qual_pct,
    )
    ad["payments"] = payments
    ad["outcomes_matched_at"] = outcomes_matched_at
    return ad


def test_confirmed_waster_gets_pause():
    """Tier A со сверкой и payments=0 даёт PAUSE и confirmed-waster."""
    waster = _make_waster_ad()
    # ACTIVE-замена в том же adset позволяет оставить решение PAUSE.
    other = _make_ad(ad_id="other", adset_id="adset_w", leads=8, qual_pct=25.0, romi=120.0)
    other["payments"] = 3

    results = score_and_decide([waster, other])
    result_map = {r["ad_id"]: r for r in results}

    waster_result = result_map["waster_1"]
    assert waster_result["action"] == "PAUSE", (
        f"Confirmed waster должен получить PAUSE, got {waster_result['action']}. "
        f"Reasons: {waster_result['reasons']}"
    )
    assert waster_result.get("is_confirmed_waster") is True, (
        f"Флаг is_confirmed_waster должен быть True: {waster_result}"
    )
    reasons_text = " ".join(waster_result["reasons"]).lower()
    assert "слив" in reasons_text or "waster" in reasons_text, (
        f"Причина должна упоминать слив: {waster_result['reasons']}"
    )


def test_payments_none_not_paused():
    """payments=None (нет данных) → НЕ confirmed_waster, не паузим по этому правилу."""
    ad = _make_ad(ad_id="no_data", adset_id="adset_nd", spend=300.0, leads=10, qual_pct=5.0)
    ad["payments"] = None  # нет данных — не знаем сколько оплат

    results = score_and_decide([ad])
    r = results[0]

    assert r.get("is_confirmed_waster") is False, (
        f"payments=None → is_confirmed_waster должен быть False: {r}"
    )
    # Может быть PAUSE по другим причинам (например портфельный аутсайдер), но не по confirmed_waster
    # Главное: флаг False
    reasons_text = " ".join(r.get("reasons", [])).lower()
    assert "слив" not in reasons_text, (
        f"При payments=None не должна быть причина «слив»: {r['reasons']}"
    )


def test_qual_pct_none_not_paused_as_waster():
    """qual_pct=None (нет данных) → НЕ confirmed_waster."""
    ad = _make_ad(ad_id="no_qual", adset_id="adset_nq", spend=300.0, leads=0, qual_pct=None)
    ad["payments"] = 0

    results = score_and_decide([ad])
    r = results[0]

    assert r.get("is_confirmed_waster") is False, (
        f"qual_pct=None → is_confirmed_waster должен быть False: {r}"
    )


def test_good_ad_with_payments_not_paused_as_waster():
    """payments>0 → НЕ confirmed_waster, даже если qual_pct низкий."""
    ad = _make_ad(ad_id="good_pay", adset_id="adset_gp", spend=300.0, leads=10, qual_pct=5.0)
    ad["payments"] = 2  # есть хоть одна оплата → не слив

    results = score_and_decide([ad])
    r = results[0]

    assert r.get("is_confirmed_waster") is False, (
        f"payments=2 → is_confirmed_waster должен быть False: {r}"
    )


def test_waster_spend_below_threshold_not_paused():
    """spend=$300 на границе не входит в tier A (условие строго >)."""
    ad = _make_waster_ad(spend=300.0)

    results = score_and_decide([ad])
    r = results[0]

    assert r.get("is_confirmed_waster") is False, (
        f"spend=300 на границе tier A не должен давать confirmed-waster: {r}"
    )


def test_guardrail_protects_lone_confirmed_waster():
    """Последний confirmed_waster остаётся ACTIVE до появления ACTIVE-замены."""
    waster = _make_waster_ad(ad_id="lone_waster", adset_id="adset_lone")

    results = score_and_decide([waster])
    r = results[0]

    assert r["action"] == "KEEP", (
        f"Единственный confirmed_waster должен ждать ACTIVE-замену: "
        f"action={r['action']}, reasons={r['reasons']}"
    )
    assert r.get("is_confirmed_waster") is True
    assert "active-замену" in " ".join(r["reasons"]).lower()


def test_guardrail_keeps_one_in_adset_with_waster():
    """В смешанном adset guardrail оставляет ровно одно объявление ACTIVE."""
    waster = _make_waster_ad(ad_id="waster", adset_id="adset_mix")
    # Обычное плохое объявление (qual=0 → PAUSE по старому правилу) — НЕ слив
    regular_bad = _make_ad(
        ad_id="regular_bad", adset_id="adset_mix",
        leads=5, qual_pct=0.0, spend=50.0,
    )
    regular_bad["payments"] = None  # нет данных по оплатам — не confirmed_waster

    results = score_and_decide([waster, regular_bad])
    result_map = {r["ad_id"]: r for r in results}

    assert result_map["waster"].get("is_confirmed_waster") is True
    assert sorted(r["action"] for r in result_map.values()) == ["KEEP", "PAUSE"]


def test_guardrail_all_confirmed_wasters_keeps_one():
    """Три confirmed_waster одного adset: максимум два получают PAUSE."""
    results = score_and_decide([
        _make_waster_ad(ad_id=f"waster_{index}", adset_id="adset_all_wasters", spend=400 + index)
        for index in range(3)
    ])

    assert sum(result["action"] == "PAUSE" for result in results) == 2
    assert sum(result["action"] == "KEEP" for result in results) == 1


# ---------------------------------------------------------------------------
# Тесты двухтирной логики confirmed_waster (Тир A и Тир B)
# ---------------------------------------------------------------------------

def _make_ad_with_payments(
    ad_id: str,
    adset_id: str = "adset_t",
    spend: float = 500.0,
    leads: int = 50,
    qual_pct: float | None = 10.0,
    payments: int | None = 0,
    outcomes_matched_at: str | None = "2026-06-01T00:00:00",
) -> dict:
    """Объявление с явным полем payments для тестов тиров.

    B1: outcomes_matched_at по умолчанию задан (сверка прошла) — эти тесты
    проверяют работу тир-логики ПРИ подтверждённой сверке, семантику
    "нет данных" отдельно проверяют test_payments_none_not_waster_tier_a
    и test_tier_no_outcomes_match_not_waster.
    """
    ad = _make_ad(ad_id=ad_id, adset_id=adset_id, spend=spend, leads=leads, qual_pct=qual_pct)
    ad["payments"] = payments
    ad["outcomes_matched_at"] = outcomes_matched_at
    # Гейт цикла оплаты: тир A требует возраст >= 14 дней —
    # моложе оплаты физически не дозрели (у _make_ad дефолт 5 дней). Тесты
    # тиров проверяют логику ПРИ зрелом возрасте; сам гейт отдельно проверяет
    # test_tier_a_respects_payment_maturity (test_autopause_blindspots).
    ad["days_running"] = 20
    return ad


def test_tier_a_typical_example():
    """$500 / квал 10% / 0 оплат / 50 лид → слив тир A (синтетический пример).

    spend=500 > 300, leads=50 >= 15, qual_pct=10 < 25 → тир A → PAUSE.
    """
    ad = _make_ad_with_payments(ad_id="tier_a_example", spend=500.0, leads=50, qual_pct=10.0, payments=0)
    replacement = _make_ad_with_payments(
        ad_id="tier_a_example_replacement", spend=40.0, leads=10,
        qual_pct=30.0, payments=2,
    )
    results = score_and_decide([ad, replacement])
    r = next(result for result in results if result["ad_id"] == "tier_a_example")

    assert r["action"] == "PAUSE", (
        f"$500/10%/0опл/50лид должен быть PAUSE (тир A): action={r['action']}, reasons={r['reasons']}"
    )
    assert r.get("is_confirmed_waster") is True
    reasons_text = " ".join(r["reasons"]).lower()
    assert "тир a" in reasons_text, f"Причина должна упоминать тир A: {r['reasons']}"


def test_tier_a_spend_and_leads_boundaries_unchanged():
    """Tier A требует spend>$300 и leads>=15; ровно 15 лидов уже достаточно."""
    cases = [
        (300.0, 15, False),
        (300.01, 14, False),
        (300.01, 15, True),
    ]

    for spend, leads, expected_confirmed in cases:
        ad = _make_ad_with_payments(
            ad_id=f"tier_a_{spend}_{leads}",
            spend=spend,
            leads=leads,
            qual_pct=13.0,
            payments=0,
        )
        result = score_and_decide([ad])[0]
        assert result["is_confirmed_waster"] is expected_confirmed, result


def test_tier_a_high_qual_not_waster():
    """$500 / квал 30% / 0 оплат → НЕ слив (qual >= 25% — лаг оплаты вероятен).

    spend=500 > 300, leads=20 >= 15, НО qual_pct=30 >= 25 → тир A НЕ срабатывает.
    spend=500 > 150, но qual_pct=30 >= 10 → тир B тоже не срабатывает.
    Итог: is_confirmed_waster=False.
    """
    ad = _make_ad_with_payments(ad_id="high_qual", spend=500.0, leads=20, qual_pct=30.0, payments=0)
    results = score_and_decide([ad])
    r = results[0]

    assert r.get("is_confirmed_waster") is False, (
        f"qual_pct=30% (>= 25%) → is_confirmed_waster должен быть False (лаг оплат): {r}"
    )
    reasons_text = " ".join(r.get("reasons", [])).lower()
    assert "слив" not in reasons_text, (
        f"При qual_pct=30% не должна быть причина «слив»: {r['reasons']}"
    )


def test_tier_b_low_qual_mid_spend():
    """$250 / квал 5% / 0 оплат / 30 лид → диагностика tier B.

    spend=250 > 150, qual_pct=5 < 10, но даже outcomes_matched_at
    подтверждает только сверку, а не зрелость когорты.
    (тир A не срабатывает: spend=250 < 300)
    """
    ads = [
        _make_ad_with_payments(
            ad_id=f"tier_b_{index}", spend=250.0, leads=30,
            qual_pct=5.0, payments=0,
        )
        for index in range(2)
    ]
    results = score_and_decide(ads)

    assert all(result["action"] != "PAUSE" for result in results), results
    assert all(result["is_confirmed_waster"] is False for result in results), results
    assert all(result["is_tier_b_diagnostic"] is True for result in results), results
    assert all("тир b" in " ".join(result["reasons"]).lower() for result in results)


def test_tier_b_spend_and_qual_boundaries_are_diagnostic_only():
    """Tier B требует spend>$150 и qual<10%; границы не включаются."""
    cases = [
        (150.0, 9.99, False),
        (150.01, 10.0, False),
        (150.01, 9.99, True),
    ]

    for spend, qual_pct, expected_diagnostic in cases:
        ad = _make_ad_with_payments(
            ad_id=f"tier_b_{spend}_{qual_pct}",
            spend=spend,
            leads=10,
            qual_pct=qual_pct,
            payments=0,
        )
        result = score_and_decide([ad])[0]
        assert result["is_tier_b_diagnostic"] is expected_diagnostic, result
        assert result["is_confirmed_waster"] is False, result


def test_tier_a_not_enough_leads_and_qual_above_b():
    """$250 / квал 13% / 0 оплат / 8 лид → НЕ слив.

    spend=250 < 300 → тир A НЕ (мало spend).
    qual_pct=13 >= 10 → тир B НЕ (квал выше порога).
    Итог: is_confirmed_waster=False.
    """
    ad = _make_ad_with_payments(ad_id="borderline", spend=250.0, leads=8, qual_pct=13.0, payments=0)
    results = score_and_decide([ad])
    r = results[0]

    assert r.get("is_confirmed_waster") is False, (
        f"$250/13%/8лид — не должен быть confirmed_waster: {r}"
    )


def test_payments_positive_not_waster_tier_a():
    """payments=2 → НЕ слив, даже если тир A условия выполнены."""
    ad = _make_ad_with_payments(ad_id="has_payments", spend=500.0, leads=30, qual_pct=10.0, payments=2)
    results = score_and_decide([ad])
    r = results[0]

    assert r.get("is_confirmed_waster") is False, (
        f"payments=2 → is_confirmed_waster должен быть False (есть оплаты): {r}"
    )


def test_payments_none_not_waster_tier_a():
    """payments=None → НЕ слив (нет данных по оплатам), даже при большом расходе."""
    ad = _make_ad_with_payments(ad_id="no_pay_data", spend=500.0, leads=30, qual_pct=10.0, payments=None)
    results = score_and_decide([ad])
    r = results[0]

    assert r.get("is_confirmed_waster") is False, (
        f"payments=None → is_confirmed_waster должен быть False (нет данных): {r}"
    )


def test_tier_a_qual_none_is_waster():
    """qual_pct=None при тире A: None трактуется как 'неизвестно' → тир A срабатывает.

    spend=400 > 300, leads=20 >= 15, qual_pct=None (None < 25 по условию тира A) → слив.
    Логика: нет данных по квалу + большой расход + много лидов + 0 оплат → режем.
    """
    ad = _make_ad_with_payments(ad_id="no_qual_tier_a", spend=400.0, leads=20, qual_pct=None, payments=0)
    replacement = _make_ad_with_payments(
        ad_id="no_qual_replacement", spend=40.0, leads=10,
        qual_pct=30.0, payments=2,
    )
    results = score_and_decide([ad, replacement])
    r = next(result for result in results if result["ad_id"] == "no_qual_tier_a")

    assert r.get("is_confirmed_waster") is True, (
        f"spend=400/leads=20/qual=None/0опл → тир A, is_confirmed_waster=True: {r}"
    )
    assert r["action"] == "PAUSE"


def test_emotion_theme_installment_markers():
    """Рассрочка в названии («рассрочк…» или «на 12 месяцев») — тема оффера,
    сигнал в пользу SCALE; нейтральное название темой не считается."""
    from services.decision_policy import _has_emotion_theme

    assert _has_emotion_theme("CityA | Спикер / Рассрочка банка") is True
    assert _has_emotion_theme("CityA | Спикер / Оплата на 12 месяцев") is True
    assert _has_emotion_theme("CityA | Петров / Тест") is False
