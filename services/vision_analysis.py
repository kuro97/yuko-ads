"""Сервис визуального анализа рекламных креативов через Gemini Vision."""

import json
import logging
import subprocess
import tempfile
import threading
from pathlib import Path

from integrations.vision import analyze_video_frames

logger = logging.getLogger(__name__)

# Путь к хранилищу результатов анализа
DATA_DIR = Path(__file__).parent.parent / "data"
ANALYSIS_FILE = DATA_DIR / "vision_analysis.json"

_lock = threading.Lock()


def _load_analyses() -> dict:
    """Загружает все анализы из файла."""
    if not ANALYSIS_FILE.exists():
        return {}
    with open(ANALYSIS_FILE) as f:
        return json.load(f)


def _save_analyses(data: dict) -> None:
    """Сохраняет анализы в файл."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(ANALYSIS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_analysis(ad_id: str) -> dict | None:
    """Получить результат анализа по ad_id."""
    with _lock:
        analyses = _load_analyses()
    return analyses.get(str(ad_id))


def get_all_analyses() -> dict:
    """Получить все анализы."""
    with _lock:
        return _load_analyses()


def extract_frames_from_video(video_url: str) -> list[bytes]:
    """Извлекает 3 ключевых кадра из видео: 1с, 3с, середина.

    Использует ffmpeg для извлечения кадров.

    Args:
        video_url: URL видео (HTTP/HTTPS)

    Returns:
        list[bytes]: список байтов jpg-изображений
    """
    frames = []
    timestamps = ["00:00:01", "00:00:03"]

    # Получаем длительность
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(result.stdout.strip()) if result.stdout.strip() else 10.0
    except (subprocess.TimeoutExpired, ValueError):
        duration = 10.0

    # Добавляем середину
    mid = int(duration / 2)
    if mid < 60:
        timestamps.append(f"00:00:{mid:02d}")
    else:
        timestamps.append(f"00:{mid // 60:02d}:{mid % 60:02d}")

    for ts in timestamps:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            subprocess.run(
                [
                    "ffmpeg", "-ss", ts, "-i", video_url,
                    "-vframes", "1", "-q:v", "2", tmp_path, "-y",
                ],
                capture_output=True, timeout=30, check=True,
            )
            frame_data = Path(tmp_path).read_bytes()
            if len(frame_data) > 100:  # Минимальный размер валидного jpg
                frames.append(frame_data)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            logger.warning("Не удалось извлечь кадр %s из %s", ts, video_url)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    return frames


def get_video_url_from_fb(ad_id: str) -> str | None:
    """Получает URL видео объявления из Facebook API."""
    import os

    import requests

    from services.fb_token_provider import get_fb_token

    token = get_fb_token()
    if not token:
        return None

    # Получаем creative ID объявления
    resp = requests.get(
        f"https://graph.facebook.com/v21.0/{ad_id}",
        params={
            "access_token": token,
            "fields": "creative{video_id,object_story_spec}",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return None

    data = resp.json()
    creative = data.get("creative", {})
    video_id = creative.get("video_id")

    if not video_id:
        return None

    # Получаем URL видео
    resp = requests.get(
        f"https://graph.facebook.com/v21.0/{video_id}",
        params={"access_token": token, "fields": "source"},
        timeout=15,
    )
    if resp.status_code != 200:
        return None

    return resp.json().get("source")


def analyze_creative(ad_id: str, ad_name: str = "", video_url: str = None) -> dict:
    """Анализирует креатив: извлекает кадры, отправляет в Gemini Vision.

    Args:
        ad_id: ID объявления в Facebook
        ad_name: название объявления (для контекста)
        video_url: URL видео (если не указан — получаем из FB API)

    Returns:
        dict: результат анализа

    Raises:
        ValueError: если видео не найдено
        RuntimeError: если анализ не удался
    """
    # Проверяем кэш
    cached = get_analysis(str(ad_id))
    if cached:
        return cached

    # Получаем URL видео
    if not video_url:
        video_url = get_video_url_from_fb(str(ad_id))

    if not video_url:
        raise ValueError(f"Видео не найдено для объявления {ad_id}")

    # Извлекаем кадры
    frames = extract_frames_from_video(video_url)
    if not frames:
        raise RuntimeError(f"Не удалось извлечь кадры из видео {ad_id}")

    # Анализируем через Gemini
    analysis = analyze_video_frames(frames, ad_name=ad_name)
    analysis["ad_id"] = str(ad_id)
    analysis["ad_name"] = ad_name
    analysis["frames_count"] = len(frames)

    # Сохраняем в кэш
    with _lock:
        all_analyses = _load_analyses()
        all_analyses[str(ad_id)] = analysis
        _save_analyses(all_analyses)

    return analysis


def get_vision_patterns() -> dict:
    """Находит паттерны в проанализированных креативах.

    Группирует по first_frame_type и ищет корреляции с CPL.

    Returns:
        dict: total_analyzed, patterns
    """
    analyses = get_all_analyses()
    if not analyses:
        return {"total_analyzed": 0, "patterns": []}

    # Загружаем данные из creative_table для CPL
    learner_file = DATA_DIR / "learner_results.json"
    cpl_map = {}
    if learner_file.exists():
        with open(learner_file) as f:
            data = json.load(f)
        for entry in data.get("creative_table", []):
            cpl_map[str(entry["ad_id"])] = entry.get("cpl", 0)

    # Группируем по типу первого кадра
    type_groups: dict[str, dict] = {}
    for ad_id, analysis in analyses.items():
        frame_type = analysis.get("first_frame_type", "other")
        if frame_type not in type_groups:
            type_groups[frame_type] = {"count": 0, "cpls": [], "has_person": 0}
        type_groups[frame_type]["count"] += 1
        cpl = cpl_map.get(ad_id, 0)
        if cpl > 0:
            type_groups[frame_type]["cpls"].append(cpl)
        if analysis.get("has_person"):
            type_groups[frame_type]["has_person"] += 1

    patterns = []
    for frame_type, group in sorted(
        type_groups.items(), key=lambda x: x[1]["count"], reverse=True
    ):
        avg_cpl = sum(group["cpls"]) / len(group["cpls"]) if group["cpls"] else 0
        person_pct = (
            (group["has_person"] / group["count"] * 100) if group["count"] else 0
        )
        patterns.append({
            "type": frame_type,
            "count": group["count"],
            "avg_cpl": round(avg_cpl, 2),
            "person_pct": round(person_pct, 1),
            "description": _type_description(frame_type),
        })

    return {"total_analyzed": len(analyses), "patterns": patterns}


def _type_description(frame_type: str) -> str:
    """Описание типа первого кадра."""
    descriptions = {
        "person_talking": "Человек говорит в камеру",
        "text_screen": "Текстовый экран / заголовок",
        "product": "Показ продукта / услуги",
        "lifestyle": "Лайфстайл / ситуация из жизни",
        "other": "Другое",
    }
    return descriptions.get(frame_type, frame_type)
