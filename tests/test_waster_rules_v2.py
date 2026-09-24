"""Юниты правил v2 «подтверждённый слив» — пороги из ретро-симуляции."""

from datetime import date, timedelta

from services.waster_rules_v2 import (
    MATURITY_DAYS,
    R1_MIN_MATURE_LEADS,
    dead_silence_verdict,
    mature_zero_verdict,
)

СЕГОДНЯ = date(2026, 8, 13)
СТАРТ = СЕГОДНЯ - timedelta(days=20)


def _лиды(n, days_ago, qual=False):
    d = СЕГОДНЯ - timedelta(days=days_ago)
    return [(d, qual)] * n


# --------------------------- R1 «зрелый ноль» ------------------------------

def test_r1_fires_on_mature_zero():
    v = mature_zero_verdict(_лиды(15, 5), 200.0, СТАРТ, СЕГОДНЯ)
    assert v.is_waster and v.rule == "R1_MATURE_ZERO"


def test_r1_immature_leads_do_not_count():
    """Лиды младше 3 дней не в знаменателе: 36% их квалов ещё дозревают."""
    v = mature_zero_verdict(_лиды(15, 1), 200.0, СТАРТ, СЕГОДНЯ)
    assert not v.is_waster and "зрелых лидов 0" in v.detail


def test_r1_single_mature_qual_saves_the_ad():
    события = _лиды(14, 5) + _лиды(1, 5, qual=True)
    assert not mature_zero_verdict(события, 200.0, СТАРТ, СЕГОДНЯ).is_waster


def test_r1_fresh_qual_means_alive():
    """Реклама ожила: свежий квал в незрелой зоне блокирует срез."""
    события = _лиды(15, 5) + _лиды(1, 1, qual=True)
    v = mature_zero_verdict(события, 200.0, СТАРТ, СЕГОДНЯ)
    assert not v.is_waster and "ожила" in v.detail


def test_r1_needs_spend_threshold():
    assert not mature_zero_verdict(_лиды(15, 5), 149.0, СТАРТ, СЕГОДНЯ).is_waster


def test_r1_protects_young_ads():
    молодой_старт = СЕГОДНЯ - timedelta(days=4)
    assert not mature_zero_verdict(_лиды(15, 4), 200.0, молодой_старт, СЕГОДНЯ).is_waster


def test_r1_needs_enough_mature_leads():
    v = mature_zero_verdict(_лиды(R1_MIN_MATURE_LEADS - 1, 5), 200.0, СТАРТ, СЕГОДНЯ)
    assert not v.is_waster


# ------------------------- R2 «мёртвая тишина» -----------------------------

def _расход(окно_дней=7, в_день=10.0):
    return {СЕГОДНЯ - timedelta(days=i): в_день for i in range(окно_дней + 3)}


def test_r2_fires_on_dead_silence():
    v = dead_silence_verdict(_расход(), [], СТАРТ, СЕГОДНЯ)
    assert v.is_waster and v.rule == "R2_DEAD_SILENCE"


def test_r2_any_lead_in_window_saves_the_ad():
    лид_вчера = [СЕГОДНЯ - timedelta(days=1)]
    assert not dead_silence_verdict(_расход(), лид_вчера, СТАРТ, СЕГОДНЯ).is_waster


def test_r2_old_leads_do_not_save():
    """Лид девятидневной давности не оправдывает неделю тишины."""
    старый = [СЕГОДНЯ - timedelta(days=9)]
    assert dead_silence_verdict(_расход(), старый, СТАРТ, СЕГОДНЯ).is_waster


def test_r2_needs_window_spend():
    капля = {СЕГОДНЯ - timedelta(days=i): 5.0 for i in range(8)}
    assert not dead_silence_verdict(капля, [], СТАРТ, СЕГОДНЯ).is_waster


def test_r2_protects_young_ads():
    """Возраст <10 дней: «тишина» юной рекламы — это обучение, не смерть.

    Симуляция это измерила: без возрастного гейта вариант «пустышка на
    5-й день» давал большую долю ложных сработок.
    """
    молодой_старт = СЕГОДНЯ - timedelta(days=9)
    assert not dead_silence_verdict(_расход(), [], молодой_старт, СЕГОДНЯ).is_waster


def test_maturity_boundary_is_three_full_days():
    """Лид ровно трёхдневной давности уже зрелый (92% судьбы известно)."""
    граница = _лиды(R1_MIN_MATURE_LEADS, MATURITY_DAYS)
    assert mature_zero_verdict(граница, 200.0, СТАРТ, СЕГОДНЯ).is_waster
