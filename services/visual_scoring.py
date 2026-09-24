"""Визуальная оценка креативов через Gemini Vision.

6 критериев + бинарные признаки. Результат — dict пригодный для JSON.
Используется analyze_image() из integrations/vision.py.
"""

import json
import logging
import re
from pathlib import Path

import requests

from integrations.vision import analyze_image

logger = logging.getLogger(__name__)

# Таймаут при скачивании картинки по URL
_DOWNLOAD_TIMEOUT_SEC = 15

# Значения по умолчанию при ошибке парсинга или недоступности
_DEFAULTS = {
    "hook_strength": 3,
    "visual_quality": 3,
    "has_cta": False,
    "has_subtitles": False,
    "has_person": False,
    "text_to_image_ratio": 50,
    "first_frame_type": "other",
    "emotion": "neutral",
    "hook_description": "",
    "summary": "",
}

# Промпт для Gemini Vision — строгая оценка по 6 критериям
_SCORING_PROMPT = """Ты — профессиональный perform-маркетолог Meta Ads с 10-летним опытом.
Оцени это рекламное изображение по 6 критериям. Будь строгим, не давай средних оценок.

Верни ТОЛЬКО валидный JSON (без обёрток, без комментариев) в таком формате:

{
  "hook_strength": <int 1-5>,
  "visual_quality": <int 1-5>,
  "has_cta": <bool>,
  "has_subtitles": <bool>,
  "has_person": <bool>,
  "text_to_image_ratio": <int 0-100>,
  "first_frame_type": "<тип>",
  "emotion": "<эмоция>",
  "hook_description": "<строка до 80 символов>",
  "summary": "<1 предложение объясняющее оценку>"
}

Расшифровка полей:
- hook_strength 1-5: Сила первого впечатления — останавливает скролл? 5=да, 1=пройдут мимо
- visual_quality 1-5: Композиция, читаемость, разрешение
- has_cta: Видна кнопка/призыв к действию
- has_subtitles: Есть субтитры/ключевой текст
- has_person: На картинке человек крупным планом
- text_to_image_ratio 0-100: % площади занятой текстом
- first_frame_type: person_talking | text_screen | product | lifestyle | other
- emotion: positive | energetic | neutral | negative
- hook_description: Что именно зрит первым (до 80 символов)
- summary: 1 предложение объясняющее оценку

Если не уверен — ставь нейтральные значения (3, false, 50, "neutral")."""


def _compute_score(parsed: dict) -> int:
    """Вычисляет визуальный итог 0-40 по взвешенным критериям.

    hook_strength и visual_quality имеют вес ×1.6 (макс 8 каждый).
    Бинарные признаки дают по 4 балла.
    text_to_image_ratio: ≤25% → 4, ≤40% → 2, иначе 0.
    """
    score = 0.0
    score += parsed.get("hook_strength", 0) * 1.6   # max 8
    score += parsed.get("visual_quality", 0) * 1.6  # max 8

    if parsed.get("has_cta"):
        score += 4
    if parsed.get("has_subtitles"):
        score += 4
    if parsed.get("has_person"):
        score += 4

    ratio = parsed.get("text_to_image_ratio", 100)
    if 0 <= ratio <= 25:
        score += 4
    elif ratio <= 40:
        score += 2

    return round(score)


def _parse_gemini_response(raw_text: str) -> dict | None:
    """Пытается распарсить JSON из ответа Gemini.

    Gemini иногда оборачивает ответ в ```json ... ```.
    Если прямой парсинг не удался — ищет фрагмент {...} через regex.
    """
    text = raw_text.strip()

    # Убираем markdown-обёртку
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    # Прямая попытка распарсить
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback: ищем первый {...} блок в ответе
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def _build_error_result(error_message: str) -> dict:
    """Возвращает словарь с дефолтными значениями и описанием ошибки."""
    result = dict(_DEFAULTS)
    result["_score"] = 0
    result["_max_score"] = 40
    result["_error"] = error_message
    return result


def score_visual_from_image(image_url_or_path: str) -> dict:
    """Оценивает рекламное изображение визуально через Gemini Vision.

    Принимает URL (https://...) или локальный путь к .jpg/.png.
    Никогда не бросает исключение — при любой ошибке возвращает dict с _error.

    Returns:
        dict с ключами:
          hook_strength, visual_quality, has_cta, has_subtitles, has_person,
          text_to_image_ratio, first_frame_type, emotion,
          hook_description, summary,
          _score (0-40), _max_score (40), _error (None или строка)
    """
    # --- Шаг 1: получаем байты изображения ---
    image_bytes: bytes | None = None

    if image_url_or_path.startswith("http://") or image_url_or_path.startswith("https://"):
        # Скачиваем по URL
        try:
            resp = requests.get(
                image_url_or_path,
                timeout=_DOWNLOAD_TIMEOUT_SEC,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code != 200:
                logger.warning("Не удалось скачать картинку: HTTP %d, URL=%s", resp.status_code, image_url_or_path)
                return _build_error_result(f"image_unavailable:http_{resp.status_code}")
            image_bytes = resp.content
        except requests.exceptions.Timeout:
            logger.warning("Timeout при скачивании картинки: %s", image_url_or_path)
            return _build_error_result("image_unavailable:timeout")
        except requests.exceptions.RequestException as exc:
            logger.warning("Ошибка при скачивании картинки: %s — %s", image_url_or_path, exc)
            return _build_error_result(f"image_unavailable:{type(exc).__name__}")
    else:
        # Читаем локальный файл
        file_path = Path(image_url_or_path)
        if not file_path.exists():
            logger.warning("Файл не найден: %s", image_url_or_path)
            return _build_error_result("image_unavailable:file_not_found")
        try:
            image_bytes = file_path.read_bytes()
        except OSError as exc:
            logger.warning("Ошибка чтения файла %s: %s", image_url_or_path, exc)
            return _build_error_result(f"image_unavailable:{exc}")

    # --- Шаг 2: отправляем в Gemini Vision ---
    try:
        gemini_response = analyze_image(image_bytes, _SCORING_PROMPT)
        raw_text = gemini_response.get("text", "")
    except RuntimeError as exc:
        # GEMINI_API_KEY не задан
        logger.error("Gemini Vision недоступен: %s", exc)
        return _build_error_result(f"gemini_unavailable:{exc}")
    except Exception as exc:
        logger.error("Ошибка вызова Gemini Vision: %s", exc)
        return _build_error_result(f"gemini_error:{type(exc).__name__}:{exc}")

    # --- Шаг 3: парсим JSON из ответа ---
    parsed = _parse_gemini_response(raw_text)
    if parsed is None:
        logger.warning("Не удалось распарсить ответ Gemini: %s", raw_text[:200])
        return _build_error_result("parse_failed")

    # --- Шаг 4: нормализуем и вычисляем скор ---
    result: dict = {}

    # Числовые поля — приводим к int с ограничением диапазона
    result["hook_strength"] = max(1, min(5, int(parsed.get("hook_strength", _DEFAULTS["hook_strength"]))))
    result["visual_quality"] = max(1, min(5, int(parsed.get("visual_quality", _DEFAULTS["visual_quality"]))))
    result["text_to_image_ratio"] = max(0, min(100, int(parsed.get("text_to_image_ratio", _DEFAULTS["text_to_image_ratio"]))))

    # Булевы поля
    result["has_cta"] = bool(parsed.get("has_cta", _DEFAULTS["has_cta"]))
    result["has_subtitles"] = bool(parsed.get("has_subtitles", _DEFAULTS["has_subtitles"]))
    result["has_person"] = bool(parsed.get("has_person", _DEFAULTS["has_person"]))

    # Строковые поля с дефолтами
    valid_frame_types = {"person_talking", "text_screen", "product", "lifestyle", "other"}
    first_frame_type = str(parsed.get("first_frame_type", "other"))
    result["first_frame_type"] = first_frame_type if first_frame_type in valid_frame_types else "other"

    valid_emotions = {"positive", "energetic", "neutral", "negative"}
    emotion = str(parsed.get("emotion", "neutral"))
    result["emotion"] = emotion if emotion in valid_emotions else "neutral"

    result["hook_description"] = str(parsed.get("hook_description", ""))[:80]
    result["summary"] = str(parsed.get("summary", ""))

    # Производные поля
    result["_score"] = _compute_score(result)
    result["_max_score"] = 40
    result["_error"] = None

    return result


def score_visual_from_kb_record(ad: dict) -> dict:
    """Оценивает визуально одну запись из creative_kb.

    Логика получения изображения:
    1. Если vision_tags уже содержит _score — возвращает кешированный результат (без Gemini)
    2. Берёт thumbnail_url из записи (поле может присутствовать если ad пришёл из analytics_cache)
    3. Если thumbnail_url нет — возвращает ошибку no_image_url

    Для видео-объявлений thumbnail_url — это превью первого кадра.
    Для объявлений без изображения возвращает no_image_for_video_yet.

    Args:
        ad: dict из creative_kb или analytics_cache (ключи: ad_id, vision_tags, thumbnail_url и др.)

    Returns:
        dict с теми же ключами что и score_visual_from_image()
    """
    ad_id = ad.get("ad_id", "unknown")

    # --- Шаг 1: проверяем кеш в vision_tags ---
    vision_tags = ad.get("vision_tags")
    if vision_tags:
        # vision_tags может быть JSON-строкой или уже dict
        if isinstance(vision_tags, str):
            try:
                vision_tags = json.loads(vision_tags)
            except json.JSONDecodeError:
                vision_tags = None

        if isinstance(vision_tags, dict) and vision_tags.get("_score") is not None:
            logger.debug("ad_id=%s: визуальный скор уже в кеше, Gemini не вызываем", ad_id)
            return vision_tags

    # --- Шаг 2: ищем URL изображения ---
    # Приоритет: image_url (HD из FB API, хранится в creative_kb после enrichment)
    # > thumbnail_url (есть в analytics_cache.json и fb_common.py)
    image_url = ad.get("image_url") or ad.get("thumbnail_url") or ""

    if not image_url:
        logger.info("ad_id=%s: нет thumbnail_url или image_url для скоринга", ad_id)
        return _build_error_result("no_image_url")

    # Для видео-объявлений без превью (video_id есть, thumbnail_url пустой)
    content_type = ad.get("content_type", "video")
    if content_type == "video" and not image_url:
        return _build_error_result("no_image_for_video_yet")

    # --- Шаг 3: скорим изображение ---
    logger.info("ad_id=%s: запускаем визуальный скоринг по URL=%s", ad_id, image_url[:60])
    return score_visual_from_image(image_url)
