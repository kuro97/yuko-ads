"""
Текстовый скоринг рекламных объявлений через Gemini.
Задача 2+3 Sprint 2.1: score_text (4 критерия) + check_customer_first (Customer-First фреймворк).
Имена customer_* — исторические, сохранены для совместимости с рубрикой и БД.
"""

import json
import os
import re

import google.generativeai as genai

# ── Конфигурация Gemini ─────────────────────────────────────────────────────

_GEMINI_MODEL = "gemini-2.5-flash-lite"
_GEMINI_CONFIGURED = False


def _ensure_configured() -> None:
    """Ленивая инициализация Gemini API — один раз за процесс."""
    global _GEMINI_CONFIGURED
    if _GEMINI_CONFIGURED:
        return
    # config.py уже сделал load_dotenv() при импорте, env переменные доступны
    from config import GEMINI_API_KEY
    api_key = GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY не задан")
    genai.configure(api_key=api_key)
    _GEMINI_CONFIGURED = True


def _call_gemini(prompt: str) -> str:
    """Единая обёртка вызова Gemini. Возвращает raw текст ответа.

    Поднимает исключение при проблемах с API или отсутствии ключа.
    Использует response_mime_type=application/json чтобы Gemini вернул чистый JSON.
    """
    _ensure_configured()
    model = genai.GenerativeModel(_GEMINI_MODEL)
    resp = model.generate_content(
        prompt,
        generation_config={
            "response_mime_type": "application/json",
            # gemini-2.5-flash — reasoning-модель, токенов нужно больше чем кажется
            "max_output_tokens": 8192,
        },
    )
    return resp.text


# ── Промпты ────────────────────────────────────────────────────────────────

_SCORE_TEXT_PROMPT = """Ты — эксперт по перформанс-маркетингу для сервисных компаний.
Оцени рекламный текст по 4 критериям. Будь строгим — середину шкалы (3) ставь как можно реже.

ЗАГОЛОВОК: {headline}
ТЕКСТ: {body}

Верни ТОЛЬКО валидный JSON без обёрток:
{{
  "offer_clarity": <int 1-5>,
  "emotional_resonance": <int 1-5>,
  "social_proof": <int 1-5>,
  "outcome_specificity": <int 1-5>,
  "summary": "<одно предложение>"
}}"""

_CUSTOMER_FIRST_PROMPT = """Ты — эксперт по рекламе сервисных компаний.

Из практики: лучшие объявления сервисов используют "Customer-First" фреймворк:
1. Голос реального клиента (не только обещания компании)
2. Конкретный результат клиента (решил задачу, сэкономил, получил нужный результат)
3. Ответ в первые 3 секунды на вопрос "Что изменится для клиента после покупки?"

Поле customer_voice = есть голос реального клиента.

ЗАГОЛОВОК: {headline}
ТЕКСТ: {body}

Проверь по 3 пунктам. Верни ТОЛЬКО валидный JSON без обёрток:
{{
  "customer_voice": <bool>,
  "customer_voice_quote": "<цитата если есть, иначе пусто>",
  "concrete_outcome": <bool>,
  "outcome_quote": "<цитата если есть, иначе пусто>",
  "life_after_answered": <bool>,
  "recommendation": "<1-2 предложения что добавить если каких-то пунктов нет>"
}}"""


# ── Внутренний хелпер ───────────────────────────────────────────────────────


def _parse_json(raw: str) -> dict:
    """Парсит JSON из ответа Gemini. При неудаче пробует regex-извлечение.

    Возвращает пустой dict если не удалось ничего извлечь.
    """
    # Прямой парсинг
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Попытка извлечь {...} через regex (на случай если Gemini обернул в markdown)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return {}


# ── Публичные функции ───────────────────────────────────────────────────────


def score_text(ad_body: str, ad_headline: str = "") -> dict:
    """Текстовый скоринг объявления по 4 критериям (один вызов Gemini).

    Возвращает dict со скорами offer_clarity, emotional_resonance,
    social_proof, outcome_specificity (каждая 1-5), summary,
    а также _score (4..20), _max_score=20, _error.
    """
    # Дефолт для error-кейсов
    defaults = {
        "offer_clarity": 0,
        "emotional_resonance": 0,
        "social_proof": 0,
        "outcome_specificity": 0,
        "summary": "",
        "_score": 0,
        "_max_score": 20,
        "_error": None,
    }

    if not ad_body or not ad_body.strip():
        return {**defaults, "_error": "empty_text"}

    prompt = _SCORE_TEXT_PROMPT.format(
        headline=ad_headline.strip(),
        body=ad_body.strip(),
    )

    try:
        raw = _call_gemini(prompt)
    except Exception as exc:
        return {**defaults, "_error": str(exc)}

    parsed = _parse_json(raw)
    if not parsed:
        return {**defaults, "_error": "parse_failed"}

    # Читаем скоры с защитой от неожиданных типов
    def _int(val: object, fallback: int = 0) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return fallback

    offer_clarity = _int(parsed.get("offer_clarity"))
    emotional_resonance = _int(parsed.get("emotional_resonance"))
    social_proof = _int(parsed.get("social_proof"))
    outcome_specificity = _int(parsed.get("outcome_specificity"))
    total = offer_clarity + emotional_resonance + social_proof + outcome_specificity

    return {
        "offer_clarity": offer_clarity,
        "emotional_resonance": emotional_resonance,
        "social_proof": social_proof,
        "outcome_specificity": outcome_specificity,
        "summary": str(parsed.get("summary", "")),
        "_score": total,
        "_max_score": 20,
        "_error": None,
    }


def check_customer_first(ad_body: str, ad_headline: str = "") -> dict:
    """Customer-First проверка объявления (один вызов Gemini).

    Возвращает dict с customer_voice, concrete_outcome, life_after_answered (bool),
    цитатами, recommendation, _score (0..25), _max_score=25, _error.
    """
    defaults = {
        "customer_voice": False,
        "customer_voice_quote": "",
        "concrete_outcome": False,
        "outcome_quote": "",
        "life_after_answered": False,
        "recommendation": "",
        "_score": 0,
        "_max_score": 25,
        "_error": None,
    }

    if not ad_body or not ad_body.strip():
        return {**defaults, "_error": "empty_text"}

    prompt = _CUSTOMER_FIRST_PROMPT.format(
        headline=ad_headline.strip(),
        body=ad_body.strip(),
    )

    try:
        raw = _call_gemini(prompt)
    except Exception as exc:
        return {**defaults, "_error": str(exc)}

    parsed = _parse_json(raw)
    if not parsed:
        return {**defaults, "_error": "parse_failed"}

    def _bool(val: object) -> bool:
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)

    customer_voice = _bool(parsed.get("customer_voice", False))
    concrete_outcome = _bool(parsed.get("concrete_outcome", False))
    life_after_answered = _bool(parsed.get("life_after_answered", False))

    # _score: каждый bool = 25/3 ≈ 8.33 очка, итого int (0, 8, 17 или 25)
    passed = sum([customer_voice, concrete_outcome, life_after_answered])
    score = round(passed * 25 / 3)

    return {
        "customer_voice": customer_voice,
        "customer_voice_quote": str(parsed.get("customer_voice_quote", "")),
        "concrete_outcome": concrete_outcome,
        "outcome_quote": str(parsed.get("outcome_quote", "")),
        "life_after_answered": life_after_answered,
        "recommendation": str(parsed.get("recommendation", "")),
        "_score": score,
        "_max_score": 25,
        "_error": None,
    }
