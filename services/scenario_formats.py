"""
Форматные спецификации сценариста v2.

Структура рекламного ТЗ детерминирована форматом (референсом), а не выдумывается
LLM на ходу. FORMAT_SPECS описывает 4 формата (video_speaker/video_ugc/carousel/static):
структуру по таймкодам/слайдам, обязательные и запрещённые маркеры.

Используется в двух местах:
- scenario_prompt.build_scenario_prompt -> format_instructions(ad_format) диктует структуру промпту
- scenario_validator.validate_scenario -> validate_format_structure(scenario, ad_format) проверяет маркеры
"""

# Форматные спецификации: структура ТЗ по формату + маркеры для валидатора.
# required_markers — хотя бы намёк на структуру формата должен присутствовать в сценарии.
# forbidden_markers — маркеры "чужого" формата, которые сигналят о мисматче.
FORMAT_SPECS = {
    "video_speaker": {
        "label": "Видео: спикер-монолог",
        "structure": "Хук (0-3с) → тело монолога (3-5 абзацев) → мягкий CTA",
        "required_markers": ["(Хук):"],
        "forbidden_markers": ["Слайд", "Кадр", "overlay"],
    },
    "video_ugc": {
        "label": "Видео: UGC/сторителлинг",
        "structure": "Хук (0-3с) → проблема → неудачные попытки → ACME → инсайт → CTA с реальным дедлайном",
        "required_markers": ["Сценарий:"],
        "forbidden_markers": ["Слайд"],
    },
    "carousel": {
        "label": "Карусель",
        "structure": "Слайд 1 (хук) → слайды 2-N (аргументы) → финальный слайд (CTA)",
        "required_markers": ["Слайд 1", "Слайд 2"],
        "forbidden_markers": ["(Хук):", "0-3с"],
    },
    "static": {
        "label": "Статика",
        "structure": "Визуал (описание) → overlay-текст → подпись (caption) с CTA",
        "required_markers": ["Визуал:", "Текст на макете:", "Подпись:"],
        "forbidden_markers": ["Слайд 2", "0-3с"],
    },
}

# Формат по умолчанию, если референс не распознан ни по одному ключевому слову.
DEFAULT_FORMAT = "video_speaker"

# Ключевые слова в имени референса → формат. Порядок важен: проверяем по очереди,
# первое совпадение побеждает (video_ugc и video_speaker могут пересекаться по "видео").
_REFERENCE_KEYWORDS = [
    ("video_ugc", ("ugc", "сторител", "отзыв", "клиент", "покупател")),
    ("video_speaker", ("спикер", "монолог")),
    ("carousel", ("карусель", "слайд")),
    ("static", ("статик", "баннер", "макет")),
]


def format_instructions(ad_format: str) -> str:
    """
    Возвращает текстовый блок инструкций по структуре формата для промпта.
    Неизвестный формат → инструкции по DEFAULT_FORMAT (fail-safe, а не падение).
    """
    spec = FORMAT_SPECS.get(ad_format, FORMAT_SPECS[DEFAULT_FORMAT])
    forbidden = ", ".join(spec["forbidden_markers"])
    required = ", ".join(spec["required_markers"])
    return (
        f"Формат: {spec['label']}.\n"
        f"Структура (строго): {spec['structure']}.\n"
        f"Обязательно используй маркеры разметки: {required}.\n"
        f"Запрещено использовать маркеры чужих форматов: {forbidden}."
    )


def detect_format_from_reference(ref_name: str) -> str:
    """
    Определяет ad_format по ключевым словам в имени референса (креатива-победителя).
    Пример: "CityA | Спикер / Пример | CR001" -> "video_speaker".
    Нет референса или ни одно ключевое слово не совпало -> DEFAULT_FORMAT.
    """
    if not ref_name:
        return DEFAULT_FORMAT

    name_lower = ref_name.lower()
    for ad_format, keywords in _REFERENCE_KEYWORDS:
        if any(keyword in name_lower for keyword in keywords):
            return ad_format
    return DEFAULT_FORMAT


def validate_format_structure(scenario: str, ad_format: str) -> list[str]:
    """
    Проверяет соответствие структуры сценария заявленному формату.
    Возвращает список человекочитаемых нарушений (RU); пустой список = структура ок.

    Ловит два типа мисматча:
    - отсутствует хотя бы один required_marker (сценарий не размечен под формат)
    - присутствует forbidden_marker (сценарий размечен под ДРУГОЙ формат)
    """
    violations: list[str] = []
    spec = FORMAT_SPECS.get(ad_format)
    if spec is None:
        violations.append(f"Неизвестный формат '{ad_format}'")
        return violations

    if not scenario or not scenario.strip():
        violations.append("Пустой сценарий")
        return violations

    for marker in spec["required_markers"]:
        if marker not in scenario:
            violations.append(
                f"Формат '{ad_format}' требует маркер '{marker}', в сценарии не найден"
            )

    for marker in spec["forbidden_markers"]:
        if marker in scenario:
            violations.append(
                f"Формат '{ad_format}' запрещает маркер '{marker}' (структура другого формата)"
            )

    return violations
