"""
Рубрика скоринга креативов: итоговый балл 0-100 из трёх источников.

Источники:
  - visual: score_visual_from_image/score_visual_from_kb_record → _score 0-40
  - text: score_text → _score 0-20
  - customer: check_customer_first → _score 0-25
  + bonus до 15 баллов за комбинации признаков

Итого max = 40 + 20 + 25 + 15 = 100
"""

import json
import logging

logger = logging.getLogger(__name__)

# Пороги для грейдов
_GRADE_EXCELLENT = 80
_GRADE_GOOD = 60
_GRADE_MEDIOCRE = 40

# Максимальные баллы по каждому источнику
_MAX_VISUAL = 40
_MAX_TEXT = 20
_MAX_CUSTOMER = 25
_MAX_BONUS = 15


def grade_from_score(score: int) -> str:
    """Грейд по итоговому баллу.

    Возвращает: excellent (80-100), good (60-79), mediocre (40-59), poor (0-39).
    """
    if score >= _GRADE_EXCELLENT:
        return "excellent"
    if score >= _GRADE_GOOD:
        return "good"
    if score >= _GRADE_MEDIOCRE:
        return "mediocre"
    return "poor"


def _compute_bonuses(visual: dict, text: dict, customer: dict) -> tuple[int, list[str]]:
    """Вычисляет бонусные баллы (до 15) за комбинации признаков.

    Условия:
    - +5 если есть субтитры (has_subtitles) И outcome_specificity >= 4
    - +5 если есть человек в кадре (has_person) И customer_voice
    - +5 если есть CTA (has_cta) И offer_clarity >= 4

    Бонус не начисляется если у источника есть _error.

    Returns:
        (сумма_бонусов, список_активных_бонусов)
    """
    bonus = 0
    active: list[str] = []

    # Бонус 1: субтитры + конкретность результата
    if (
        not visual.get("_error")
        and not text.get("_error")
        and visual.get("has_subtitles")
        and text.get("outcome_specificity", 0) >= 4
    ):
        bonus += 5
        active.append("subtitles+outcome")

    # Бонус 2: человек в кадре + голос клиента
    if (
        not visual.get("_error")
        and not customer.get("_error")
        and visual.get("has_person")
        and customer.get("customer_voice")
    ):
        bonus += 5
        active.append("person+customer")

    # Бонус 3: CTA + чёткий оффер
    if (
        not visual.get("_error")
        and not text.get("_error")
        and visual.get("has_cta")
        and text.get("offer_clarity", 0) >= 4
    ):
        bonus += 5
        active.append("cta+offer")

    return bonus, active


def compute_total_score(visual: dict, text: dict, customer: dict) -> dict:
    """Вычисляет итоговый скор 0-100 из трёх источников + бонусы.

    Формула:
        total = visual._score (0-40)
              + text._score (0-20)
              + customer._score (0-25)
              + bonus (0-15)
        max = 100

    Edge cases:
    - Если у источника есть _error — его _score трактуется как 0,
      бонусы связанные с ним тоже не начисляются.
    - Итог зажимается в диапазон 0-100.

    Args:
        visual: результат score_visual_from_image() или score_visual_from_kb_record()
        text: результат score_text()
        customer: результат check_customer_first()

    Returns:
        {
            "total_score": int (0-100),
            "grade": str ("excellent"/"good"/"mediocre"/"poor"),
            "visual_score": int (0-40),
            "text_score": int (0-20),
            "customer_score": int (0-25),
            "bonus_score": int (0-15),
            "breakdown": {
                "visual": dict,
                "text": dict,
                "customer": dict,
                "bonuses": list[str]
            }
        }
    """
    # Берём _score из каждого источника, при ошибке — 0
    visual_score = int(visual.get("_score", 0)) if not visual.get("_error") else 0
    text_score = int(text.get("_score", 0)) if not text.get("_error") else 0
    customer_score = int(customer.get("_score", 0)) if not customer.get("_error") else 0

    # Зажимаем в пределах допустимых максимумов
    visual_score = max(0, min(_MAX_VISUAL, visual_score))
    text_score = max(0, min(_MAX_TEXT, text_score))
    customer_score = max(0, min(_MAX_CUSTOMER, customer_score))

    # Бонусные баллы
    bonus_score, active_bonuses = _compute_bonuses(visual, text, customer)
    bonus_score = max(0, min(_MAX_BONUS, bonus_score))

    total = visual_score + text_score + customer_score + bonus_score
    total = max(0, min(100, total))

    grade = grade_from_score(total)

    return {
        "total_score": total,
        "grade": grade,
        "visual_score": visual_score,
        "text_score": text_score,
        "customer_score": customer_score,
        "bonus_score": bonus_score,
        "breakdown": {
            "visual": visual,
            "text": text,
            "customer": customer,
            "bonuses": active_bonuses,
        },
    }
