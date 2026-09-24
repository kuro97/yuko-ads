"""Интеграция с Gemini Vision API для анализа видео-креативов."""

import json
import logging
import os

logger = logging.getLogger(__name__)

# Модель для vision-задач
VISION_MODEL = "gemini-2.5-flash-lite"


def analyze_image(image_data: bytes, prompt: str) -> dict:
    """Анализирует изображение через Gemini Vision API.

    Args:
        image_data: байты изображения (jpg/png)
        prompt: промпт для анализа

    Returns:
        dict с текстом ответа

    Raises:
        RuntimeError: если GEMINI_API_KEY не задан
    """
    from config import GEMINI_API_KEY
    api_key = GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY не задан")

    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(VISION_MODEL)

    image_part = {"mime_type": "image/jpeg", "data": image_data}
    response = model.generate_content([prompt, image_part])
    return {"text": response.text}


def analyze_video_frames(frames: list[bytes], ad_name: str = "") -> dict:
    """Анализирует ключевые кадры видео-креатива.

    Отправляет все кадры в один запрос с структурированным промптом.

    Returns:
        dict: first_frame_type, has_person, has_subtitles, emotion,
              text_overlay, hook_description, summary
    """
    from config import GEMINI_API_KEY
    api_key = GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY не задан")

    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(VISION_MODEL)

    prompt = f"""Ты — маркетинговый аналитик. Проанализируй ключевые кадры рекламного видео.
{"Название: " + ad_name if ad_name else ""}

Кадры идут в порядке: начало (1 с), середина, конец видео.

Ответь СТРОГО в формате JSON (без markdown-блоков):
{{
    "first_frame_type": "person_talking" | "text_screen" | "product" | "lifestyle" | "other",
    "has_person": true/false,
    "has_subtitles": true/false,
    "emotion": "positive" | "neutral" | "negative" | "energetic",
    "text_overlay": "текст если есть или null",
    "hook_description": "краткое описание первого кадра (1-2 предложения)",
    "summary": "общее описание креатива (2-3 предложения)"
}}"""

    # Собираем контент: промпт + все кадры
    content_parts = [prompt]
    for frame_data in frames:
        content_parts.append({"mime_type": "image/jpeg", "data": frame_data})

    response = model.generate_content(content_parts)

    # Парсим JSON из ответа
    text = response.text.strip()
    # Убираем markdown-блоки если Gemini вернул ```json ... ```
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]
        text = text.strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Gemini вернул невалидный JSON: %s", text[:200])
        result = {
            "first_frame_type": "other",
            "has_person": False,
            "has_subtitles": False,
            "emotion": "neutral",
            "text_overlay": None,
            "hook_description": text[:200],
            "summary": "Не удалось распарсить ответ Gemini",
        }

    return result
