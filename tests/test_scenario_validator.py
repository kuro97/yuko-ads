"""
Тесты services/scenario_validator.py — блок-проверки Сценариста v2 (ARCH-phase3-scenarist.md, T9).

Два независимых гейта:
1. validate_scenario — детерминированные regex/правила, БЕЗ сети. Обязаны ловиться
   все 5 типов нарушений: пакет↔продукт, число без источника, смешение голосов,
   мисматч формата, выдуманный дедлайн. Чистый сценарий должен проходить (PASS).
2. self_critique — второй LLM-вызов (мокаем anthropic.Anthropic), fail-closed:
   нет ключа/ошибка LLM/невалидный вердикт → BLOCK.

Fact Sheet берём реальный — собранный из docs/product/fact-sheet-acme.md
(services.fact_sheet.build_fact_sheet_json, мимо кэша data/fact_sheet.json: локальный
кэш инвалидируется только по mtime .md и может отставать от кода парсера), поэтому
тесты валидатора проверяют контракт на реальных разрешённых фактах, а не на
придуманной фикстуре. Исключение — тест testimonial-гейта «режим выключен»:
он строит синтетическую копию реального fact_sheet с override testimonial_mode_enabled=False,
т.к. §3 в .md из репо заполнен отзывами.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.fact_sheet import build_fact_sheet_json
from services.scenario_validator import (
    CritiqueResult,
    ValidationResult,
    self_critique,
    validate_scenario,
)


@pytest.fixture(scope="module")
def fact_sheet() -> dict:
    """Реальный Fact Sheet, собранный из docs/product/fact-sheet-acme.md."""
    return build_fact_sheet_json()


def _brand_topic(ad_format: str = "video_speaker") -> dict:
    """Тема с голосом brand — дефолтный (разрешённый) голос."""
    return {
        "source": "coverage",
        "city": "CityA",
        "segment": "PRODA",
        "voice": "brand",
        "ad_format": ad_format,
        "reference": None,
        "variable_to_vary": "город",
        "angle": "Рассрочка банка-партнёра на 12 месяцев",
        "priority": "HIGH",
        "rationale": "тест",
    }


# ---------------------------------------------------------------------------
# 1. Блок: несовместимость пакет↔продукт
# ---------------------------------------------------------------------------

def test_validate_scenario_package_product_mismatch_blocked(fact_sheet):
    """Сценарий одновременно упоминает 'пакет 2' (есть только у PRODA) и 'PRODB' (только пакет 6) — блок."""
    scenario = (
        "(Хук): Вам хватит пакета 2?\n\n"
        "Мы в ACME подключаем к PRODB уже с него — приходите на консультацию.\n\n"
        "Записывайтесь на бесплатную консультацию уже сегодня."
    )
    topic = _brand_topic("video_speaker")

    result = validate_scenario(scenario, topic, fact_sheet)

    assert isinstance(result, ValidationResult)
    assert result.passed is False
    assert any("пакет" in v.lower() and "продукт" in v.lower() for v in result.violations) or any(
        "пакет 2" in v and "PRODB" in v for v in result.violations
    )


def test_validate_scenario_package_marker_matches_word_forms_only_exact_number(fact_sheet):
    """Маркер «пакет 6» ловит словоформы («пакета 6»), но не другое число («пакета 60»):
    правило «пакет 6 + PRODA» срабатывает только на реальный пакет 6."""
    topic = _brand_topic("video_speaker")
    blocked = (
        "(Хук): Для клиентов пакета 6 действует весенняя акция PRODA?\n\n"
        "Запишитесь на бесплатную консультацию."
    )
    not_blocked = (
        "(Хук): Для клиентов пакета 60 действует весенняя акция PRODA?\n\n"
        "Запишитесь на бесплатную консультацию."
    )

    blocked_result = validate_scenario(blocked, topic, fact_sheet)
    passed_result = validate_scenario(not_blocked, topic, fact_sheet)

    assert any("пакет 6" in v and "PRODA" in v for v in blocked_result.violations)
    assert not any("пакет" in v.lower() and "продукт" in v.lower() for v in passed_result.violations)


# ---------------------------------------------------------------------------
# 2. Блок: число без источника (бизнес-статистика не из allowed_numbers)
# ---------------------------------------------------------------------------

def test_validate_scenario_number_without_source_blocked(fact_sheet):
    """'28 из 40 клиентов улучшили результат' — числа 28 и 40 не входят в allowed_numbers Fact Sheet."""
    scenario = (
        "(Хук): Как улучшить результат за месяц?\n\n"
        "28 из 40 наших клиентов улучшили результат уже после первого месяца.\n\n"
        "Запишитесь на бесплатную консультацию."
    )
    topic = _brand_topic("video_speaker")

    result = validate_scenario(scenario, topic, fact_sheet)

    assert result.passed is False
    assert any("число" in v.lower() for v in result.violations)


# ---------------------------------------------------------------------------
# 3. Блок: смешение голосов brand + testimonial
# ---------------------------------------------------------------------------

def test_validate_scenario_voice_mixing_blocked(fact_sheet):
    """Маркеры brand ('мы в ACME') и testimonial ('я записалась') одновременно."""
    scenario = (
        "(Хук): История одной клиентки.\n\n"
        "Мы в ACME всегда готовы помочь — я записалась на консультацию "
        "и сразу почувствовала разницу.\n\n"
        "Запишитесь на бесплатную консультацию."
    )
    topic = _brand_topic("video_speaker")

    result = validate_scenario(scenario, topic, fact_sheet)

    assert result.passed is False
    assert any("смешение голосов" in v.lower() for v in result.violations)


# ---------------------------------------------------------------------------
# 4. Блок: мисматч структуры формата
# ---------------------------------------------------------------------------

def test_validate_scenario_format_mismatch_blocked(fact_sheet):
    """ad_format=carousel, но текст размечен под video_speaker: есть '(Хук):',
    нет обязательных 'Слайд 1'/'Слайд 2' — мисматч формата."""
    scenario = (
        "(Хук): Подключаем PRODA с пакета 2.\n\n"
        "Пакет 4 даёт 4 услуги в одном месте — начать с него проще.\n\n"
        "Запишитесь на бесплатную консультацию."
    )
    topic = _brand_topic("carousel")

    result = validate_scenario(scenario, topic, fact_sheet)

    assert result.passed is False
    assert any("формат" in v.lower() for v in result.violations)


# ---------------------------------------------------------------------------
# 5. Блок: выдуманный дедлайн
# ---------------------------------------------------------------------------

def test_validate_scenario_fake_deadline_blocked(fact_sheet):
    """'успей до 15 августа' — такой даты нет в fact_sheet['deadlines']."""
    scenario = (
        "(Хук): Время уходит.\n\n"
        "Успей подключиться до 15 августа — потом мест не будет.\n\n"
        "Запишитесь на бесплатную консультацию."
    )
    topic = _brand_topic("video_speaker")

    result = validate_scenario(scenario, topic, fact_sheet)

    assert result.passed is False
    assert any("дедлайн" in v.lower() for v in result.violations)


# ---------------------------------------------------------------------------
# Чистый сценарий — проходит все проверки
# ---------------------------------------------------------------------------

def test_validate_scenario_clean_brand_scenario_passes(fact_sheet):
    """Корректный video_speaker про рассрочку на 12 месяцев без нарушений — passed=True, violations=[]."""
    scenario = (
        "(Хук): Хотите подключить PRODA без удара по бюджету?\n\n"
        "В ACME работает рассрочка банка-партнёра — 12 месяцев без переплат.\n\n"
        "Один менеджер ведёт не больше 10 клиентов и сопровождает каждый этап.\n\n"
        "Сезон короткий — решение лучше принять уже сейчас.\n\n"
        "Запишитесь на бесплатную консультацию."
    )
    topic = _brand_topic("video_speaker")

    result = validate_scenario(scenario, topic, fact_sheet)

    assert result.passed is True
    assert result.violations == []


# ---------------------------------------------------------------------------
# Edge: пустой сценарий
# ---------------------------------------------------------------------------

def test_validate_scenario_empty_scenario_blocked(fact_sheet):
    """Пустой сценарий — блок с понятной причиной, не падает с исключением."""
    topic = _brand_topic("video_speaker")

    result = validate_scenario("", topic, fact_sheet)

    assert result.passed is False
    assert result.violations == ["Пустой сценарий"]


# ---------------------------------------------------------------------------
# Edge: реальный дедлайн из Fact Sheet НЕ блокируется
# ---------------------------------------------------------------------------

def test_validate_scenario_real_deadline_not_blocked(fact_sheet):
    """'до 15 апреля' — реальный дедлайн весенней акции PRODA (2026-04-15), не блокируется."""
    scenario = (
        "(Хук): Весенняя акция на PRODA идёт ограниченный срок.\n\n"
        "Зафиксировать цену пакета можно до 15 апреля — успейте подключиться заранее.\n\n"
        "Запишитесь на бесплатную консультацию."
    )
    topic = _brand_topic("video_speaker")

    result = validate_scenario(scenario, topic, fact_sheet)

    deadline_violations = [v for v in result.violations if "дедлайн" in v.lower()]
    assert deadline_violations == []


# ---------------------------------------------------------------------------
# Edge: абстрактная срочность НЕ блокируется
# ---------------------------------------------------------------------------

def test_validate_scenario_abstract_urgency_not_blocked(fact_sheet):
    """'сезон короткий' — абстрактная срочность без конкретной даты, не блокируется."""
    scenario = (
        "(Хук): Времени меньше, чем кажется.\n\n"
        "Сезон короткий — начинать нужно уже сейчас.\n\n"
        "Запишитесь на бесплатную консультацию."
    )
    topic = _brand_topic("video_speaker")

    result = validate_scenario(scenario, topic, fact_sheet)

    assert result.passed is True
    assert result.violations == []


# ---------------------------------------------------------------------------
# Edge: разрешённые числа из Fact Sheet проходят
# ---------------------------------------------------------------------------

def test_validate_scenario_allowed_numbers_not_blocked(fact_sheet):
    """'не больше 10 клиентов' на менеджера (§2 оффера) и '4 из 6' (пакет 4 из 6 услуг ACME, §1) —
    числа есть в allowed_numbers, не блокируются."""
    scenario = (
        "(Хук): Индивидуальный подход без очереди.\n\n"
        "В ACME один менеджер ведёт не больше 10 клиентов — и успевает уделить внимание каждому.\n\n"
        "Пакет 4 — это 4 из 6 услуг ACME в одном месте, и менеджер разбирает с клиентом каждый этап.\n\n"
        "Запишитесь на бесплатную консультацию."
    )
    topic = _brand_topic("video_speaker")

    result = validate_scenario(scenario, topic, fact_sheet)

    number_violations = [v for v in result.violations if "число" in v.lower()]
    assert number_violations == []


# ---------------------------------------------------------------------------
# Edge: год (2026) не блокируется как число без источника
# ---------------------------------------------------------------------------

def test_validate_scenario_year_number_not_blocked(fact_sheet):
    """4-значный год (2026) — календарная привязка, не бизнес-статистика, не блокируется."""
    scenario = (
        "(Хук): Сезон 2026 уже начался.\n\n"
        "В 2026 году ACME снова запускает весеннюю акцию на PRODA.\n\n"
        "Запишитесь на бесплатную консультацию."
    )
    topic = _brand_topic("video_speaker")

    result = validate_scenario(scenario, topic, fact_sheet)

    number_violations = [v for v in result.violations if "число" in v.lower()]
    assert number_violations == []


# ---------------------------------------------------------------------------
# Edge: testimonial-голос заблокирован пока testimonial_mode_enabled=False
# ---------------------------------------------------------------------------

def test_validate_scenario_testimonial_voice_blocked_when_mode_disabled(fact_sheet):
    """topic.voice='testimonial' при testimonial_mode_enabled=False — блок независимо от
    текста сценария. Реальный Fact Sheet теперь имеет §3 заполненным (mode=True), поэтому
    режим 'выключен' проверяем на синтетической копии fact_sheet с override (без обращения
    к .md из репо), чтобы сценарий «нет отзывов → блок» остался покрытым."""
    disabled_fact_sheet = {**fact_sheet, "testimonial_mode_enabled": False, "testimonials": []}

    scenario = (
        "Сценарий: Я записалась на бесплатную консультацию в ACME.\n\n"
        "Запишитесь на бесплатную консультацию."
    )
    topic = _brand_topic("video_ugc")
    topic["voice"] = "testimonial"

    result = validate_scenario(scenario, topic, disabled_fact_sheet)

    assert result.passed is False
    assert any("testimonial" in v.lower() for v in result.violations)


# ---------------------------------------------------------------------------
# Edge: testimonial-голос ПРОХОДИТ гейт, когда testimonial_mode_enabled=True
# ---------------------------------------------------------------------------

def test_validate_scenario_testimonial_voice_passes_when_mode_enabled(fact_sheet):
    """topic.voice='testimonial' на Fact Sheet из репо (§3 заполнен образцами отзывов,
    testimonial_mode_enabled=True) — testimonial-гейт не блокирует
    (нарушений 'testimonial' в violations нет)."""
    assert fact_sheet["testimonial_mode_enabled"] is True

    scenario = (
        "Сценарий: Я записалась на бесплатную консультацию в ACME.\n\n"
        "Запишитесь на бесплатную консультацию."
    )
    topic = _brand_topic("video_ugc")
    topic["voice"] = "testimonial"

    result = validate_scenario(scenario, topic, fact_sheet)

    testimonial_violations = [v for v in result.violations if "testimonial" in v.lower()]
    assert testimonial_violations == [], \
        "Гейт testimonial не должен блокировать, когда режим включён"


# ---------------------------------------------------------------------------
# self_critique — fail-closed без ключа
# ---------------------------------------------------------------------------

def test_self_critique_no_api_key_blocks(monkeypatch):
    """ANTHROPIC_API_KEY не задан — verdict='BLOCK' (fail-closed), LLM не вызывается."""
    import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        result = self_critique("Валидный сценарий про рассрочку на 12 месяцев.", _brand_topic())

    assert isinstance(result, CritiqueResult)
    assert result.verdict == "BLOCK"
    assert not mock_anthropic_cls.called


# ---------------------------------------------------------------------------
# self_critique — fail-closed при исключении LLM
# ---------------------------------------------------------------------------

def test_self_critique_llm_exception_blocks(monkeypatch):
    """Claude API бросает исключение (таймаут/сеть) — verdict='BLOCK' (fail-closed),
    исключение наружу не пробрасывается."""
    import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("Claude API таймаут")
        mock_anthropic_cls.return_value = mock_client

        result = self_critique("Валидный сценарий про рассрочку на 12 месяцев.", _brand_topic())

    assert result.verdict == "BLOCK"
    assert "Ошибка LLM" in result.reason


# ---------------------------------------------------------------------------
# self_critique — валидный PASS-ответ LLM
# ---------------------------------------------------------------------------

def test_self_critique_valid_pass_response(monkeypatch):
    """LLM вернул 'PASS: дуга есть' — verdict='PASS', llm_logger.log_llm_call вызван
    с purpose='scenario_critique'."""
    import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    fake_response = MagicMock()
    fake_text_block = MagicMock()
    fake_text_block.type = "text"
    fake_text_block.text = "PASS: дуга есть, хук цепляет, формат соблюдён."
    fake_response.content = [fake_text_block]
    fake_response.usage = MagicMock(input_tokens=120, output_tokens=15)

    with patch("anthropic.Anthropic") as mock_anthropic_cls, \
         patch("services.llm_logger.log_llm_call") as mock_log_call:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_response
        mock_anthropic_cls.return_value = mock_client

        result = self_critique("Валидный сценарий про рассрочку на 12 месяцев.", _brand_topic())

    assert result.verdict == "PASS"
    assert "дуга есть" in result.reason
    assert mock_client.messages.create.called
    # thinking должен быть явно отключён — самокритика это классификация, не рассуждение
    _, call_kwargs = mock_client.messages.create.call_args
    assert call_kwargs.get("thinking") == {"type": "disabled"}

    assert mock_log_call.called
    _, log_kwargs = mock_log_call.call_args
    assert log_kwargs.get("purpose") == "scenario_critique"


# ---------------------------------------------------------------------------
# self_critique — невалидный вердикт LLM тоже блокируется (fail-closed)
# ---------------------------------------------------------------------------

def test_self_critique_invalid_verdict_blocks(monkeypatch):
    """LLM вернул текст без PASS/BLOCK в начале строки — verdict='BLOCK' (fail-closed)."""
    import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    fake_response = MagicMock()
    fake_text_block = MagicMock()
    fake_text_block.type = "text"
    fake_text_block.text = "Кажется, сценарий неплохой, но не уверен."
    fake_response.content = [fake_text_block]
    fake_response.usage = MagicMock(input_tokens=100, output_tokens=10)

    with patch("anthropic.Anthropic") as mock_anthropic_cls, \
         patch("services.llm_logger.log_llm_call"):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_response
        mock_anthropic_cls.return_value = mock_client

        result = self_critique("Валидный сценарий про рассрочку на 12 месяцев.", _brand_topic())

    assert result.verdict == "BLOCK"
