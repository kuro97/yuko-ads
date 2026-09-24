"""
Тесты services/creative_briefs.py — формат ТЗ (без бюрократии) и промпт LLM.

Проверяем:
- format_brief_for_trello: тело карточки = только сценарий + короткая
  служебная строка, никакой таблицы полей (Аудитория/Тон/Эмоция/Приоритет).
- format_brief_for_trello с пустым сценарием — падает (карточку нельзя создавать).
- build_llm_prompt: содержит оба few-shot примера и данные брифа.
- analyze_winners: фильтр качества (payments/qual_pct) — не пропускает
  подтверждённые сливы в winners только по дешёвому CPL.
- build_creative_briefs: угол-слив (пример "Карусель") не предлагается
  на масштабирование.
- select_winner_teardowns (T3, ARCH-phase3-scenarist): teardown победителей
  ТОЛЬКО с payments>0, variable_to_vary (город/хук), ad_format, сортировка
  по payments, max_n, пустой список без победителей.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта (как в остальных тестах пакета)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from services.creative_briefs import (  # noqa: E402
    analyze_winners,
    build_creative_briefs,
    build_llm_prompt,
    format_brief_for_trello,
    select_winner_teardowns,
)


def _make_brief(angle: str = "Страх ошибки", city: str = "CityA") -> dict:
    """Тестовый бриф — как возвращает build_creative_briefs."""
    return {
        "type": "expand",
        "priority": "HIGH",
        "hypothesis": f"Запустить '{angle}' в {city}",
        "rationale": "CPL $4, 10 лидов. Тема работает.",
        "audience": f"Потенциальные клиенты в {city}",
        "message": "Тестовое сообщение",
        "hook_direction": "Тестовый хук",
        "angle": angle,
        "emotion": "Страх → облегчение",
        "tone": "Авторитетный",
        "format": "Reels 9:16",
        "references": [{"name": "Тест | Страх ошибки", "cpl": 3.5, "leads": 8}],
        "city": city,
        "topic": angle,
    }


# ---------------------------------------------------------------------------
# format_brief_for_trello
# ---------------------------------------------------------------------------

def test_format_brief_no_bureaucracy_fields():
    """Тело карточки НЕ содержит бюрократических полей — только сценарий."""
    scenario = (
        "Сценарий: Полгода я говорил себе «подумаю на выходных».\n\n"
        "Прошло три месяца — и дела больше не висят на мне одном."
    )
    brief = _make_brief()

    card = format_brief_for_trello(brief, scenario)

    forbidden = ["Аудитория:", "Тон:", "Эмоция:", "Приоритет:", "Формат:", "Хук (0-3", "Обоснование:", "Тип:"]
    for word in forbidden:
        assert word not in card["desc"], f"Найдена бюрократия: {word!r} в desc"


def test_format_brief_contains_scenario_text():
    """desc содержит сам текст сценария целиком."""
    scenario = "Сценарий: Уникальная история про бесплатную консультацию и личный кабинет."
    brief = _make_brief()

    card = format_brief_for_trello(brief, scenario)

    assert scenario in card["desc"]


def test_format_brief_has_tracking_line():
    """Внизу карточки — короткая служебная строка курсивом с CPL/референсом."""
    scenario = "Сценарий: тестовый текст."
    brief = _make_brief()

    card = format_brief_for_trello(brief, scenario)

    assert "*Авто-ТЗ" in card["desc"]
    assert "---" in card["desc"]
    # Служебная строка — после сценария, а не перед ним
    assert card["desc"].index(scenario) < card["desc"].index("*Авто-ТЗ")


def test_format_brief_name_no_auto_prefix():
    """Название карточки НЕ содержит технического префикса [ТЗ-авто]."""
    scenario = "Сценарий: тестовый текст."
    brief = _make_brief(angle="Страх ошибки", city="CityA")

    card = format_brief_for_trello(brief, scenario)

    assert "[ТЗ-авто]" not in card["name"]
    assert card["name"]  # не пустое


def test_format_brief_empty_scenario_raises():
    """Пустой сценарий — карточку создавать нельзя, функция должна упасть."""
    brief = _make_brief()

    with pytest.raises(ValueError):
        format_brief_for_trello(brief, "")

    with pytest.raises(ValueError):
        format_brief_for_trello(brief, "   ")


# ---------------------------------------------------------------------------
# build_llm_prompt
# ---------------------------------------------------------------------------

def test_build_llm_prompt_contains_both_examples():
    """Промпт содержит оба few-shot примера дословно (ключевые фрагменты)."""
    brief = _make_brief()
    prompt = build_llm_prompt(brief)

    # Фрагмент из примера 1 (спикер-монолог)
    assert "Менеджер Антон / Непонятная цена / Всё на одной странице" in prompt
    assert "личный менеджер" in prompt

    # Фрагмент из примера 2 (сторителлинг)
    assert "Дмитрий / Полгода «подумаю» / Одна смета вместо десяти звонков" in prompt
    assert "личный кабинет" in prompt
    assert "Дорого было полгода откладывать" in prompt


def test_build_llm_prompt_contains_brief_data():
    """Промпт содержит данные конкретного брифа (гипотеза, аудитория, город, угол)."""
    brief = _make_brief(angle="Страх ошибки", city="CityB")
    prompt = build_llm_prompt(brief)

    assert brief["hypothesis"] in prompt
    assert brief["audience"] in prompt
    assert "Страх ошибки" in prompt
    assert "CityB" in prompt


def test_build_llm_prompt_no_bureaucratic_output_format():
    """Промпт НЕ просит бюрократический формат ответа (ХУК/ПРОБЛЕМА/РЕШЕНИЕ/CTA таблицей)
    и НЕ просит 3 варианта — только один цельный сценарий."""
    brief = _make_brief()
    prompt = build_llm_prompt(brief)

    assert "3 ВАРИАНТА" not in prompt
    assert "ХУК (0-3 сек):" not in prompt
    assert "один цельный сценарий" in prompt.lower() or "один сценарий" in prompt.lower()


def test_build_llm_prompt_forbids_field_headers_in_output():
    """Промпт явно запрещает бюрократические заголовки в ответе LLM."""
    brief = _make_brief()
    prompt = build_llm_prompt(brief)

    assert "Аудитория:" in prompt and "запрещен" in prompt.lower()


# ---------------------------------------------------------------------------
# analyze_winners — фильтр качества (баг: генератор выбирал победителей
# только по CPL, игнорируя квал%/оплаты — пример "Карусель":
# дешёвый CPL, но квал около нуля и ни одной оплаты)
# ---------------------------------------------------------------------------

def _make_ad(name="Тест | Угол / V1", spend=100.0, leads=20, cpl=5.0,
             qual_pct=None, payments=None, **extra) -> dict:
    """Тестовое объявление в формате, который отдаёт _get_fresh_ads (creative_kb)."""
    ad = {
        "name": name,
        "spend": spend,
        "leads": leads,
        "cpl": cpl,
        "hook_rate": 0.3,
        "hold_rate": 0.15,
        "frequency": 1.5,
        "days_running": 10,
        "qual_pct": qual_pct,
        "payments": payments,
    }
    ad.update(extra)
    return ad


def test_analyze_winners_excludes_confirmed_waster_zero_payments_zero_qual():
    """Дешёвый CPL, но payments=0 и qual_pct=0 (подтверждённый слив) — НЕ в winners.

    Пример "Карусель": CPL дешёвый, но ни одного квала/оплаты.
    """
    ads = [
        _make_ad(name="CityB | Карусель / Пример", spend=500.0,
                 leads=200, cpl=2.5, qual_pct=0.0, payments=0),
        # Второе объявление — чтобы был ненулевой median_cpl и список with_leads
        _make_ad(name="CityC | Результат / V1", spend=100.0, leads=20, cpl=5.0,
                 qual_pct=15.0, payments=3),
    ]

    analysis = analyze_winners(ads)
    winner_names = [w["name"] for w in analysis["winners"]]

    assert "CityB | Карусель / Пример" not in winner_names


def test_analyze_winners_includes_ad_with_low_cpl_and_payments():
    """Дешёвый CPL и payments>0 — попадает в winners (как раньше)."""
    ads = [
        _make_ad(name="CityA | Страх ошибки / V1", spend=50.0, leads=15,
                 cpl=3.3, qual_pct=20.0, payments=2),
        _make_ad(name="CityC | Результат / V1", spend=100.0, leads=20, cpl=5.0,
                 qual_pct=15.0, payments=3),
    ]

    analysis = analyze_winners(ads)
    winner_names = [w["name"] for w in analysis["winners"]]

    assert "CityA | Страх ошибки / V1" in winner_names


def test_analyze_winners_includes_ad_with_no_qual_data_but_enough_leads():
    """Дешёвый CPL, qual_pct=None (нет данных), leads>=3 — попадает в winners.

    Отсутствие данных о квале не должно автоматически блокировать —
    только явный сигнал слива (payments=0 И qual_pct<10%) блокирует.
    """
    ads = [
        _make_ad(name="CityA | PRODA запуск / V1", spend=40.0, leads=10,
                 cpl=4.0, qual_pct=None, payments=None),
        _make_ad(name="CityC | Результат / V1", spend=100.0, leads=20, cpl=5.0,
                 qual_pct=15.0, payments=3),
    ]

    analysis = analyze_winners(ads)
    winner_names = [w["name"] for w in analysis["winners"]]

    assert "CityA | PRODA запуск / V1" in winner_names


def test_analyze_winners_excludes_no_qual_data_and_too_few_leads():
    """qual_pct=None и leads<3 — минимального сигнала недостаточно, не победитель."""
    ads = [
        _make_ad(name="CityA | Консультация / V1", spend=10.0, leads=2,
                 cpl=5.0, qual_pct=None, payments=None),
        _make_ad(name="CityC | Результат / V1", spend=100.0, leads=20, cpl=5.0,
                 qual_pct=15.0, payments=3),
    ]

    analysis = analyze_winners(ads)
    winner_names = [w["name"] for w in analysis["winners"]]

    assert "CityA | Консультация / V1" not in winner_names


def test_analyze_winners_by_angle_has_avg_qual_pct_and_total_payments():
    """by_angle агрегирует avg_qual_pct и total_payments, пропуская None."""
    # Имена городов не должны матчить TOPIC_PATTERNS (substring-поиск в
    # detect_angle) — берём нейтральные CityA/CityB
    ads = [
        _make_ad(name="CityA | Карусель / V1", cpl=3.6, leads=50, qual_pct=0.0, payments=0),
        _make_ad(name="CityB | Карусель / V1", cpl=3.6, leads=50, qual_pct=2.0, payments=0),
    ]

    analysis = analyze_winners(ads)
    data = analysis["by_angle"]["Карусель"]

    assert data["avg_qual_pct"] == pytest.approx(1.0, abs=0.01)
    assert data["total_payments"] == 0


def test_analyze_winners_by_angle_qual_pct_none_when_no_data():
    """Если ни у одного объявления угла нет qual_pct — avg_qual_pct is None."""
    ads = [
        _make_ad(name="CityA | Отзыв клиентки / V1", cpl=5.0, leads=10,
                 qual_pct=None, payments=None),
    ]

    analysis = analyze_winners(ads)
    data = analysis["by_angle"]["Отзыв клиента"]

    assert data["avg_qual_pct"] is None


# ---------------------------------------------------------------------------
# build_creative_briefs — угол-слив не предлагается на масштабирование
# ---------------------------------------------------------------------------

def _make_carousel_waster_ads() -> list[dict]:
    """Пример: 5 объявлений "Карусель" в разных городах, дешёвый CPL,
    но payments=0 и qual_pct<10% у всех — подтверждённый слив
    (confirmed_waster)."""
    # 5 разных городов — имена городов не должны матчить TOPIC_PATTERNS
    # (substring-поиск в detect_angle) раньше "карусель"
    cities = ["CityA", "CityB", "CityC", "CityD", "CityE"]
    return [
        _make_ad(
            name=f"{city} | Карусель / Пример",
            spend=500.0, leads=140, cpl=3.6 + i * 0.05,
            qual_pct=1.0 + i, payments=0,
        )
        for i, city in enumerate(cities)
    ]


def test_build_creative_briefs_does_not_propose_dead_angle():
    """Угол "Карусель" — дёшево (CPL~3.6-3.8), но payments=0 и qual<10% везде.
    build_creative_briefs НЕ должен предлагать его ни на expand, ни на scale."""
    ads = _make_carousel_waster_ads()

    analysis = analyze_winners(ads)
    briefs = build_creative_briefs(analysis, insights=[])

    angles_in_briefs = {b["angle"] for b in briefs}
    assert "Карусель" not in angles_in_briefs


def test_build_creative_briefs_still_proposes_good_angle():
    """Позитивный путь: угол с хорошим квалом/оплатами всё ещё предлагается."""
    cities = ["CityA", "CityB", "CityC", "CityD", "CityE"]
    ads = [
        _make_ad(
            name=f"{city} | Страх ошибки / V1",
            spend=50.0, leads=15, cpl=3.3,
            qual_pct=25.0, payments=3,
        )
        for city in cities
    ]

    analysis = analyze_winners(ads)
    briefs = build_creative_briefs(analysis, insights=[])

    angles_in_briefs = {b["angle"] for b in briefs}
    assert "Страх ошибки" in angles_in_briefs


# ---------------------------------------------------------------------------
# select_winner_teardowns — teardown победителей по факту продаж (T3)
# ---------------------------------------------------------------------------

def test_select_winner_teardowns_only_includes_ads_with_payments():
    """Teardown строится ТОЛЬКО из победителей с payments>0 — реклама с
    дешёвым CPL, но без единой оплаты (даже пройдя quality gate по лидам),
    в teardown попадать не должна: это разбор того, что реально продаёт."""
    ads = [
        _make_ad(name="CityA | Страх ошибки / V1", spend=50.0, leads=15,
                 cpl=3.3, qual_pct=20.0, payments=2),
        _make_ad(name="CityB | PRODA запуск / V1", spend=40.0, leads=10,
                 cpl=5.0, qual_pct=None, payments=None),
        _make_ad(name="CityD | Результат / V1", spend=100.0, leads=20, cpl=3.3,
                 qual_pct=15.0, payments=3),
    ]

    analysis = analyze_winners(ads)
    teardowns = select_winner_teardowns(analysis, max_n=5)

    ref_names = {t["reference"]["name"] for t in teardowns}
    assert "CityA | Страх ошибки / V1" in ref_names
    assert "CityD | Результат / V1" in ref_names
    # Без оплат (payments=None) — не в teardown, даже если прошёл quality gate winners
    assert "CityB | PRODA запуск / V1" not in ref_names
    for t in teardowns:
        assert t["reference"]["payments"] > 0


def test_select_winner_teardowns_excludes_confirmed_waster():
    """Пример "Карусель" (payments=0, qual_pct<10%) — не победитель вообще,
    поэтому и не может попасть в teardown (double-check через analyze_winners)."""
    ads = [
        _make_ad(name="CityB | Карусель / Пример", spend=500.0,
                 leads=200, cpl=2.5, qual_pct=0.0, payments=0),
        _make_ad(name="CityC | Результат / V1", spend=100.0, leads=20, cpl=5.0,
                 qual_pct=15.0, payments=3),
    ]

    analysis = analyze_winners(ads)
    teardowns = select_winner_teardowns(analysis, max_n=5)

    ref_names = {t["reference"]["name"] for t in teardowns}
    assert "CityB | Карусель / Пример" not in ref_names


def test_select_winner_teardowns_variable_to_vary_is_filled():
    """variable_to_vary всегда заполнен одним из допустимых значений."""
    ads = [
        _make_ad(name="CityA | Страх ошибки / V1", spend=50.0, leads=15,
                 cpl=3.3, qual_pct=20.0, payments=2),
        _make_ad(name="CityC | Результат / V1", spend=100.0, leads=20, cpl=5.0,
                 qual_pct=15.0, payments=3),
    ]

    analysis = analyze_winners(ads)
    teardowns = select_winner_teardowns(analysis, max_n=5)

    assert teardowns  # непустой результат для этого набора данных
    for t in teardowns:
        assert t["variable_to_vary"] in ("город", "хук")


def test_select_winner_teardowns_vary_city_when_angle_everywhere():
    """Если угол победителя уже есть во всех городах — варьируем город
    (в тексте спеки: "город если угол уже везде, иначе хук")."""
    ads = [
        _make_ad(name="CityA | Страх ошибки / V1", spend=50.0, leads=15,
                 cpl=3.3, qual_pct=20.0, payments=2),
        _make_ad(name="CityB | Страх ошибки / V1", spend=50.0, leads=15,
                 cpl=3.4, qual_pct=20.0, payments=1),
    ]

    analysis = analyze_winners(ads)
    teardowns = select_winner_teardowns(analysis, max_n=5)

    assert teardowns
    for t in teardowns:
        assert t["variable_to_vary"] == "город"


def test_select_winner_teardowns_sorted_by_payments_desc():
    """Сильнейшие по продажам победители — первыми в списке."""
    ads = [
        _make_ad(name="CityA | Страх ошибки / V1", spend=50.0, leads=15,
                 cpl=3.3, qual_pct=20.0, payments=1),
        _make_ad(name="CityC | Результат / V1", spend=100.0, leads=20, cpl=5.0,
                 qual_pct=15.0, payments=5),
    ]

    analysis = analyze_winners(ads)
    teardowns = select_winner_teardowns(analysis, max_n=5)

    payments_seq = [t["payments"] for t in teardowns]
    assert payments_seq == sorted(payments_seq, reverse=True)


def test_select_winner_teardowns_respects_max_n():
    """max_n ограничивает размер результата."""
    cities = ["CityA", "CityB", "CityC", "CityD", "CityE"]
    ads = [
        _make_ad(name=f"{city} | Страх ошибки / V1", spend=50.0, leads=15,
                 cpl=3.3, qual_pct=20.0, payments=i + 1)
        for i, city in enumerate(cities)
    ]

    analysis = analyze_winners(ads)
    teardowns = select_winner_teardowns(analysis, max_n=2)

    assert len(teardowns) <= 2


def test_select_winner_teardowns_empty_when_no_winners():
    """Нет победителей вообще — пустой список, не падает."""
    analysis = {"winners": [], "losers": [], "by_angle": {}, "by_city": {}}

    teardowns = select_winner_teardowns(analysis, max_n=5)

    assert teardowns == []


# ---------------------------------------------------------------------------
# target_product прокидывается через analyze_winners -> select_winner_teardowns
# (ARCH-product-tags.md): Сценарист v2 (brief_generator) использует его как
# источник продукта для Trello-метки новой карточки ТЗ.
# ---------------------------------------------------------------------------

def test_analyze_winners_propagates_target_product():
    """target_product входного ad попадает в ad_info winners без изменений."""
    ads = [
        _make_ad(name="CityA | Страх ошибки / V1", spend=50.0, leads=15,
                 cpl=3.3, qual_pct=20.0, payments=2, target_product="PRODB"),
    ]

    analysis = analyze_winners(ads)

    assert analysis["winners"][0]["target_product"] == "PRODB"


def test_analyze_winners_target_product_none_when_missing():
    """Нет target_product во входном ad (старые вызовы/тесты) — None, не падает,
    ключ ad_info["target_product"] всё равно присутствует (.get безопасен)."""
    ads = [
        _make_ad(name="CityA | Страх ошибки / V1", spend=50.0, leads=15,
                 cpl=3.3, qual_pct=20.0, payments=2),
    ]

    analysis = analyze_winners(ads)

    assert analysis["winners"][0]["target_product"] is None


def test_select_winner_teardowns_reference_includes_target_product():
    """reference победителя несёт target_product — Сценарист v2 знает продукт
    победителя без keyword-гадания (см. brief_generator._classify_topic_product)."""
    ads = [
        _make_ad(name="CityB | Критика рынка / V1", spend=80.0, leads=12,
                 cpl=4.0, qual_pct=20.0, payments=3, target_product="PRODA"),
    ]

    analysis = analyze_winners(ads)
    teardowns = select_winner_teardowns(analysis, max_n=5)

    assert teardowns[0]["reference"]["target_product"] == "PRODA"


def test_select_winner_teardowns_ad_format_detected():
    """ad_format заполнен (fallback video_speaker, пока scenario_formats
    T2 не смёржен — модуль creative_briefs.py не должен падать без него)."""
    ads = [
        _make_ad(name="CityA | Спикер / Пример / V1", spend=50.0,
                 leads=15, cpl=3.3, qual_pct=20.0, payments=2),
    ]

    analysis = analyze_winners(ads)
    teardowns = select_winner_teardowns(analysis, max_n=5)

    assert teardowns
    assert teardowns[0]["ad_format"]
