"""
Тесты: Сценарист v2 кладёт продукт в очередь ТЗ (pending_briefs) при генерации
(ARCH-product-tags.md + ARCH-brief-approval-flow.md).

Механизм (services/brief_generator.py):
  _classify_topic_product(topic, card_name, scenario) — приоритет источников:
    1) target_product победителя-референса (teardown-темы, topic["reference"])
    2) keyword-эвристика (services.product_tags.classify_product)
    3) LLM-добор (services.product_tags._llm_classify_product) — только если
       keyword дал ОБЩАЯ (эмоциональные PRODA-заходы без явных слов, см. §5 спеки)
  generate_and_push_briefs передаёт результат в add_pending(..., product=...) —
  метка попадёт на карточку Trello ТОЛЬКО после одобрения владельцем
  (telegram_bot._execute_approve_brief создаёт карточку с product=), здесь на
  этапе генерации проверяем только содержимое очереди.

Мокаем границы: LLM (product_tags._llm_classify_product), Telegram
(send_with_buttons), topic_selector/scenario_validator/fact_sheet — как в
tests/test_brief_generator.py. PENDING_FILE изолирован на tmp_path.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from services.scenario_validator import CritiqueResult, ValidationResult  # noqa: E402


def _make_topic(angle: str = "Страх ошибки", city: str = "CityA",
                 ad_format: str = "video_speaker", reference: dict | None = None) -> dict:
    """Тестовая тема v2 (см. tests/test_brief_generator.py::_make_topic)."""
    return {
        "source": "coverage" if reference is None else "teardown",
        "city": city,
        "segment": "общий",
        "voice": "brand",
        "ad_format": ad_format,
        "reference": reference,
        "variable_to_vary": "город",
        "angle": angle,
        "priority": "HIGH",
        "rationale": "тест",
    }


def _fake_fact_sheet() -> dict:
    return {
        "products": {"proda": {}, "prodb": {}},
        "offer": ["Бесплатная консультация"],
        "promo_claims": ["Рассрочка банка-партнёра на 12 месяцев"],
        "social_proof_claims": [],
        "testimonials": [],
        "testimonial_mode_enabled": False,
        "incompatibility_rules": [],
        "deadlines": [],
        "allowed_numbers": [],
        "cities": ["CityA", "CityB", "CityC"],
    }


# ---------------------------------------------------------------------------
# _classify_topic_product — приоритет источников
# ---------------------------------------------------------------------------

def test_classify_topic_product_uses_reference_target_product():
    """target_product победителя-референса (teardown-тема) — источник №1,
    LLM-добор НЕ вызывается."""
    from services.brief_generator import _classify_topic_product

    topic = _make_topic(angle="Вариация хука", reference={"name": "x", "target_product": "PRODA"})

    with patch("services.product_tags._llm_classify_product") as mock_llm:
        product = _classify_topic_product(topic, "CityA / Вариация хука", "Сценарий про PRODA.")

    assert product == "PRODA"
    assert not mock_llm.called


def test_classify_topic_product_keyword_no_llm():
    """Явный keyword в названии/угле (PRODB) — keyword-эвристика ловит,
    LLM-добор НЕ вызывается (дешёвый шаг приоритетнее)."""
    from services.brief_generator import _classify_topic_product

    topic = _make_topic(angle="Тема А / пакет 6")

    with patch("services.product_tags._llm_classify_product") as mock_llm:
        product = _classify_topic_product(topic, "CityB / Тема А / пакет 6", "Сценарий про результат.")

    assert product == "PRODB"
    assert not mock_llm.called


def test_classify_topic_product_llm_fallback_for_emotional_angle():
    """Эмоциональный заход про PRODA без явных keywords («Критика рынка») —
    keyword даёт ОБЩАЯ, зовётся LLM-добор (мок -> PRODA), НЕ остаётся ОБЩАЯ.
    Это ТОТ ЖЕ случай, что описан в §5 спеки для бэкфилла — только для НОВОЙ
    карточки, создаваемой Сценаристом v2."""
    from services.brief_generator import _classify_topic_product

    topic = _make_topic(angle="Критика рынка")

    with patch("services.product_tags._llm_classify_product", return_value="PRODA") as mock_llm:
        product = _classify_topic_product(topic, "CityA / Критика рынка", "Олег разбирает рынок.")

    assert product == "PRODA"
    mock_llm.assert_called_once()


def test_classify_topic_product_fail_safe_on_exception():
    """classify_product упал (неожиданная ошибка) — _classify_topic_product
    ловит исключение и возвращает ОБЩАЯ, НЕ бросает (карточка не должна
    блокироваться из-за сбоя классификации)."""
    from services.brief_generator import _classify_topic_product

    topic = _make_topic(angle="Страх ошибки")

    with patch("services.product_tags.classify_product", side_effect=RuntimeError("бум")):
        product = _classify_topic_product(topic, "CityA / Страх ошибки", "Сценарий.")

    assert product == "ОБЩАЯ"


def test_classify_topic_product_default_general_without_signals():
    """Нет ни target_product, ни keyword-совпадений, LLM (замокан) тоже отдаёт
    ОБЩАЯ — итог ОБЩАЯ."""
    from services.brief_generator import _classify_topic_product

    topic = _make_topic(angle="Бесплатная консультация")

    with patch("services.product_tags._llm_classify_product", return_value="ОБЩАЯ") as mock_llm:
        product = _classify_topic_product(topic, "CityA / Консультация", "Приходите на консультацию.")

    assert product == "ОБЩАЯ"
    mock_llm.assert_called_once()


# ---------------------------------------------------------------------------
# generate_and_push_briefs — очередь pending_briefs получает product= (метка
# попадёт на карточку Trello только после одобрения владельцем)
# ---------------------------------------------------------------------------

def _isolate_queue(monkeypatch, tmp_path):
    """Изолирует state-файл генератора и очередь pending_briefs на tmp_path."""
    import services.brief_generator as bg_module
    import services.pending_briefs as pb_module
    monkeypatch.setattr(bg_module, "STATE_FILE", tmp_path / "brief_gen_state.json")
    monkeypatch.setattr(pb_module, "PENDING_FILE", tmp_path / "pending_briefs.json")


@patch("services.brief_generator.send_telegram")
@patch("services.brief_generator.send_with_buttons")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_passes_product_to_queue(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_buttons, mock_telegram,
    tmp_path, monkeypatch,
):
    """Запись в очереди получает product= (keyword PRODB по углу «пакет 6»),
    без отдельного вызова на добавление метки — метка появится на карточке
    Trello только после одобрения (см. tests/test_telegram_brief_approval.py)."""
    import services.brief_generator as bg_module
    import services.pending_briefs as pb_module
    _isolate_queue(monkeypatch, tmp_path)

    mock_fact_sheet.return_value = _fake_fact_sheet()
    mock_topics.return_value = [_make_topic(angle="Тема А / пакет 6", city="CityB")]
    mock_scenario.return_value = "Сценарий: живой текст про выход на новый уровень."
    mock_validate.return_value = ValidationResult(passed=True, violations=[])
    mock_critique.return_value = CritiqueResult(verdict="PASS", reason="дуга есть")
    mock_buttons.return_value = True

    result = bg_module.generate_and_push_briefs(max_briefs=1)

    assert result["queued"] == 1
    briefs = pb_module._load()["briefs"]
    assert len(briefs) == 1
    assert briefs[0]["product"] == "PRODB"
    assert mock_buttons.called


@patch("services.brief_generator.send_telegram")
@patch("services.brief_generator.send_with_buttons")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_uses_reference_target_product_for_teardown_topic(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_buttons, mock_telegram,
    tmp_path, monkeypatch,
):
    """Teardown-тема с reference["target_product"]="PRODA" -> запись в очереди
    получает product="PRODA" даже если название/сценарий не содержат keyword."""
    import services.brief_generator as bg_module
    import services.pending_briefs as pb_module
    _isolate_queue(monkeypatch, tmp_path)

    topic = _make_topic(
        angle="Вариация хука", city="CityC",
        reference={"name": "CityC | Критика рынка", "cpl": 3.0, "leads": 10,
                   "payments": 4, "target_product": "PRODA"},
    )
    mock_fact_sheet.return_value = _fake_fact_sheet()
    mock_topics.return_value = [topic]
    mock_scenario.return_value = "Сценарий без явных ключевых слов продукта."
    mock_validate.return_value = ValidationResult(passed=True, violations=[])
    mock_critique.return_value = CritiqueResult(verdict="PASS", reason="дуга есть")
    mock_buttons.return_value = True

    result = bg_module.generate_and_push_briefs(max_briefs=1)

    assert result["queued"] == 1
    briefs = pb_module._load()["briefs"]
    assert briefs[0]["product"] == "PRODA"


@patch("services.brief_generator.send_telegram")
@patch("services.brief_generator.send_with_buttons")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_blocked_topic_does_not_classify_product(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_buttons, mock_telegram,
    tmp_path, monkeypatch,
):
    """Тема заблокирована validate_scenario — в очередь ничего не кладём
    (продукт для неё не считается, send_with_buttons не вызывается)."""
    import services.brief_generator as bg_module
    import services.pending_briefs as pb_module
    _isolate_queue(monkeypatch, tmp_path)

    mock_fact_sheet.return_value = _fake_fact_sheet()
    mock_topics.return_value = [_make_topic(angle="Тема А / пакет 6")]
    mock_scenario.return_value = "Сценарий с нарушением."
    mock_validate.return_value = ValidationResult(passed=False, violations=["нарушение"])

    result = bg_module.generate_and_push_briefs(max_briefs=1)

    assert result["blocked"] == 1
    assert len(pb_module._load()["briefs"]) == 0
    assert not mock_buttons.called
