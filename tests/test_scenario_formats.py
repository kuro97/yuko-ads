"""
Тесты services/scenario_formats.py — детекция формата по референсу,
инструкции формата в промпт, проверка структуры сценария под формат.

Чистые функции, без сети/БД.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта (паттерн из test_coverage_monitor.py)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

import services.scenario_formats as sf


# ---------------------------------------------------------------------------
# detect_format_from_reference — все 4 формата + дефолт
# ---------------------------------------------------------------------------

def test_detect_format_from_reference_video_speaker():
    """Референс со словом «Спикер» → video_speaker."""
    assert sf.detect_format_from_reference("CityA | Спикер / Пример | CR001") == "video_speaker"


def test_detect_format_from_reference_video_ugc():
    """Референс со словом «клиентка»/«отзыв» → video_ugc."""
    assert sf.detect_format_from_reference("CityB | UGC клиентка отзыв | CR002") == "video_ugc"


def test_detect_format_from_reference_video_ugc_persona_without_ugc_word():
    """Персона клиента в имени референса («Покупатель рассказывает») без слова UGC → video_ugc."""
    assert sf.detect_format_from_reference("CityB | Покупатель рассказывает | CR006") == "video_ugc"


def test_detect_format_from_reference_carousel():
    """Референс со словом «Карусель»/«Слайд» → carousel."""
    assert sf.detect_format_from_reference("CityC | Карусель 3 услуги | CR003") == "carousel"


def test_detect_format_from_reference_static():
    """Референс со словом «Статика»/«Баннер» → static."""
    assert sf.detect_format_from_reference("CityD | Статика баннер акция | CR004") == "static"


def test_detect_format_from_reference_default_on_unknown():
    """Референс без ключевых слов → DEFAULT_FORMAT (video_speaker)."""
    assert sf.detect_format_from_reference("CityE | Что-то непонятное | CR005") == sf.DEFAULT_FORMAT


def test_detect_format_from_reference_default_on_empty():
    """Пустой/None референс → DEFAULT_FORMAT, без падения."""
    assert sf.detect_format_from_reference("") == sf.DEFAULT_FORMAT
    assert sf.detect_format_from_reference(None) == sf.DEFAULT_FORMAT


# ---------------------------------------------------------------------------
# format_instructions — не падает ни на одном формате
# ---------------------------------------------------------------------------

def test_format_instructions_all_known_formats_return_nonempty_text():
    """format_instructions для каждого известного формата возвращает непустую строку."""
    for ad_format in sf.FORMAT_SPECS:
        text = sf.format_instructions(ad_format)
        assert isinstance(text, str)
        assert len(text) > 0


def test_format_instructions_unknown_format_falls_back_to_default():
    """Неизвестный формат не роняет format_instructions — используется DEFAULT_FORMAT."""
    text = sf.format_instructions("несуществующий_формат")
    default_text = sf.format_instructions(sf.DEFAULT_FORMAT)
    assert text == default_text


# ---------------------------------------------------------------------------
# validate_format_structure — happy path для всех 4 форматов
# ---------------------------------------------------------------------------

def test_validate_format_structure_video_speaker_happy():
    """Корректный video_speaker-сценарий (есть required, нет forbidden) → без нарушений."""
    scenario = "(Хук): Сезон короткий — решать надо сейчас.\nТело монолога про ACME.\nCTA: запишись на консультацию."
    violations = sf.validate_format_structure(scenario, "video_speaker")
    assert violations == []


def test_validate_format_structure_video_ugc_happy():
    """Корректный video_ugc-сценарий → без нарушений."""
    scenario = "Сценарий: клиентка рассказывает свою историю.\nПроблема → ACME → инсайт → CTA."
    violations = sf.validate_format_structure(scenario, "video_ugc")
    assert violations == []


def test_validate_format_structure_carousel_happy():
    """Корректная карусель (Слайд 1 + Слайд 2) → без нарушений."""
    scenario = "Слайд 1: хук про рассрочку.\nСлайд 2: аргумент про 3 услуги.\nСлайд 3: CTA."
    violations = sf.validate_format_structure(scenario, "carousel")
    assert violations == []


def test_validate_format_structure_static_happy():
    """Корректная статика (Визуал/Текст на макете/Подпись) → без нарушений."""
    scenario = "Визуал: фото клиентов.\nТекст на макете: Рассрочка банка-партнёра на 12 месяцев.\nПодпись: Запишись сейчас."
    violations = sf.validate_format_structure(scenario, "static")
    assert violations == []


# ---------------------------------------------------------------------------
# validate_format_structure — required_markers отсутствуют
# ---------------------------------------------------------------------------

def test_validate_format_structure_missing_required_marker_caught():
    """video_speaker без «(Хук):» → нарушение (отсутствует required marker)."""
    scenario = "Просто текст без разметки хука. CTA: запишись."
    violations = sf.validate_format_structure(scenario, "video_speaker")
    assert len(violations) >= 1
    assert any("(Хук):" in v for v in violations)


def test_validate_format_structure_carousel_missing_required_marker_caught():
    """Карусель без «Слайд 2» → нарушение (только один слайд размечен)."""
    scenario = "Слайд 1: хук.\nДальше текст без разметки следующих слайдов."
    violations = sf.validate_format_structure(scenario, "carousel")
    assert any("Слайд 2" in v for v in violations)


# ---------------------------------------------------------------------------
# validate_format_structure — forbidden_markers присутствуют (мисматч формата)
# ---------------------------------------------------------------------------

def test_validate_format_structure_forbidden_marker_caught():
    """carousel-формат с текстом video_speaker («(Хук):», «0-3с») → мисматч формата."""
    scenario = "(Хук): 0-3с интро.\nСлайд 1: хук.\nСлайд 2: аргумент."
    violations = sf.validate_format_structure(scenario, "carousel")
    assert len(violations) >= 1
    assert any("запрещает маркер" in v for v in violations)


def test_validate_format_structure_video_speaker_forbidden_slide_marker_caught():
    """video_speaker-сценарий со словом «Слайд» → мисматч (структура карусели/статики)."""
    scenario = "(Хук): начало.\nСлайд 2: аргумент про рассрочку банка."
    violations = sf.validate_format_structure(scenario, "video_speaker")
    assert any("Слайд" in v and "запрещает" in v for v in violations)


# ---------------------------------------------------------------------------
# validate_format_structure — пустой сценарий
# ---------------------------------------------------------------------------

def test_validate_format_structure_empty_scenario_caught():
    """Пустой сценарий → нарушение «Пустой сценарий», без падения."""
    violations = sf.validate_format_structure("", "video_speaker")
    assert violations == ["Пустой сценарий"]


def test_validate_format_structure_whitespace_only_scenario_caught():
    """Сценарий из одних пробелов трактуется как пустой."""
    violations = sf.validate_format_structure("   \n  \t", "static")
    assert violations == ["Пустой сценарий"]


def test_validate_format_structure_unknown_format_caught():
    """Неизвестный ad_format → отдельное нарушение (не required/forbidden проверка)."""
    violations = sf.validate_format_structure("любой текст", "неизвестный_формат")
    assert len(violations) == 1
    assert "Неизвестный формат" in violations[0]


# ---------------------------------------------------------------------------
# FORMAT_SPECS — покрывает все 4 формата с нужными ключами
# ---------------------------------------------------------------------------

def test_format_specs_covers_four_formats_with_required_keys():
    """FORMAT_SPECS содержит ровно 4 формата, у каждого есть label/structure/required/forbidden."""
    expected_formats = {"video_speaker", "video_ugc", "carousel", "static"}
    assert set(sf.FORMAT_SPECS.keys()) == expected_formats

    for ad_format, spec in sf.FORMAT_SPECS.items():
        assert "label" in spec
        assert "structure" in spec
        assert isinstance(spec["required_markers"], list) and len(spec["required_markers"]) >= 1
        assert isinstance(spec["forbidden_markers"], list) and len(spec["forbidden_markers"]) >= 1
