import glob
import logging
import os
import re
import shutil
import tempfile
import gdown

logger = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi"}


def extract_file_id(drive_url: str) -> str | None:
    """Извлекает file ID из любого формата Google Drive ссылки."""
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
        r"/folders/([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, drive_url)
        if match:
            return match.group(1)
    return None


def _download_file_direct(file_id: str, output: str) -> None:
    """Скачивает файл Drive напрямую через drive.usercontent.google.com.

    Фолбэк, когда gdown бьётся о «Cannot retrieve the public link … many
    accesses»: этот endpoint отдаёт публичные файлы и после того, как
    uc?id закрыт интерстициалом (rate limit от многократных скачиваний).
    """
    import requests

    resp = requests.get(
        "https://drive.usercontent.google.com/download",
        params={"id": file_id, "export": "download", "confirm": "t"},
        stream=True,
        timeout=300,
    )
    try:
        resp.raise_for_status()
        if "text/html" in resp.headers.get("content-type", "").lower():
            raise FileNotFoundError(
                f"Drive не отдал файл {file_id}: HTML вместо содержимого "
                "(нет доступа или файл удалён)"
            )
        with open(output, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    finally:
        resp.close()


def _download_folder_with_fallback(file_id: str, folder: str) -> None:
    """gdown.download_folder, при его падении — прямое скачивание каждого файла.

    Листинг папки (skip_download=True) у gdown живёт отдельно от скачивания
    и рейт-лимитом не задет — берём из него id и имена файлов.
    """
    try:
        gdown.download_folder(id=file_id, output=folder, quiet=False)
        return
    except Exception as exc:  # noqa: BLE001 — любой сбой gdown → фолбэк
        logger.warning(
            "gdown.download_folder упал (%s: %s) — качаю файлы напрямую",
            type(exc).__name__, exc,
        )
    entries = gdown.download_folder(
        id=file_id, output=folder, skip_download=True, quiet=True
    )
    if not entries:
        raise FileNotFoundError(f"Не удалось получить список файлов папки Drive {file_id}")
    for entry in entries:
        os.makedirs(os.path.dirname(entry.local_path) or ".", exist_ok=True)
        _download_file_direct(entry.id, entry.local_path)


def _detect_file_type(path: str) -> str:
    """Определяет тип файла по расширению: 'video', 'image' или 'unknown'."""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return "unknown"


def download_media(drive_url: str) -> dict:
    """Скачивает медиа с Drive, автоматически определяет формат.

    Возвращает:
        {"type": "video"|"image"|"carousel", "paths": [str, ...]}

    Правила выбора типа для папки:
    - Несколько картинок + ключевое слово 'carousel' в названии папки/имени файла → карусель
    - Несколько картинок без метки карусели → каждая = отдельное объявление (type='image', paths=[все])
    - Одна картинка → одно объявление
    - Видео (1+) → каждое = отдельное объявление (type='video', paths=[все])
    """
    file_id = extract_file_id(drive_url)
    if not file_id:
        raise ValueError(f"Не удалось извлечь file ID из: {drive_url}")

    # Папка Drive → может быть карусель или одиночный файл
    if "/folders/" in drive_url:
        folder = tempfile.mkdtemp(prefix="acme_")
        try:
            _download_folder_with_fallback(file_id, folder)

            # Логируем всё что скачалось (рекурсивно)
            all_files = []
            for root, _dirs, files in os.walk(folder):
                for f in files:
                    all_files.append(os.path.join(root, f))
            logger.info("Drive папка скачана в %s, файлов: %d: %s", folder, len(all_files), all_files)

            # Собираем медиа-файлы рекурсивно, case-insensitive
            images = sorted(
                f for f in all_files
                if os.path.splitext(f)[1].lower() in IMAGE_EXTS
            )
            videos = sorted(
                f for f in all_files
                if os.path.splitext(f)[1].lower() in VIDEO_EXTS
            )

            # Fallback: определяем тип по magic bytes если расширения нет
            if not images and not videos:
                for fpath in all_files:
                    ftype = _detect_downloaded_type(fpath)
                    if ftype == "image":
                        images.append(fpath)
                    elif ftype == "video":
                        videos.append(fpath)
                logger.info("Fallback по magic bytes: images=%d, videos=%d", len(images), len(videos))

            # Карусель — только если явно указано в имени папки или файлов
            is_carousel = "carousel" in drive_url.lower() or any(
                "carousel" in os.path.basename(f).lower() or "карусель" in os.path.basename(f).lower()
                for f in all_files
            )

            if videos:
                # Каждое видео = отдельное объявление (раньше брали только [0])
                return {"type": "video", "paths": videos}

            if images:
                # Карусель имеет приоритет (явная метка) — пары не собираем
                if is_carousel and len(images) >= 2:
                    return {"type": "carousel", "paths": images}

                # Распознаём пары лента+сторис (N.png + N.1.png)
                pairs, singles = detect_placement_pairs(images)
                if pairs:
                    # Есть хотя бы одна пара → новый тип. Одиночки запустятся обычными ads.
                    return {"type": "placement_pairs", "paths": pairs, "singles": singles}

                # Пар нет → прежнее поведение (каждая картинка = отдельное объявление)
                return {"type": "image", "paths": images}

            raise FileNotFoundError(
                f"Нет медиа-файлов в папке Drive: {drive_url}. "
                f"Скачано файлов: {len(all_files)}, содержимое: {[os.path.basename(f) for f in all_files]}"
            )
        except Exception:
            # При ошибке убираем скачанную папку — иначе мусор копится в /tmp
            shutil.rmtree(folder, ignore_errors=True)
            raise

    # Одиночный файл
    tmp = tempfile.NamedTemporaryFile(suffix=".tmp", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        gdown.download(id=file_id, output=tmp_path, quiet=False)
    except Exception as exc:  # noqa: BLE001 — любой сбой gdown → фолбэк
        logger.warning(
            "gdown.download упал (%s: %s) — качаю напрямую",
            type(exc).__name__, exc,
        )
        _download_file_direct(file_id, tmp_path)

    # Определяем тип по реальному содержимому (gdown не сохраняет расширение)
    # Пробуем определить по magic bytes
    file_type = _detect_downloaded_type(tmp_path)

    if file_type == "video":
        final_path = tmp_path + ".mp4"
        os.rename(tmp_path, final_path)
        return {"type": "video", "paths": [final_path]}
    elif file_type == "image":
        final_path = tmp_path + ".jpg"
        os.rename(tmp_path, final_path)
        return {"type": "image", "paths": [final_path]}

    # Fallback: считаем видео (как было раньше)
    final_path = tmp_path + ".mp4"
    os.rename(tmp_path, final_path)
    return {"type": "video", "paths": [final_path]}


def _detect_downloaded_type(path: str) -> str:
    """Определяет тип скачанного файла по magic bytes."""
    with open(path, "rb") as f:
        header = f.read(12)

    # JPEG: FF D8 FF
    if header[:3] == b"\xff\xd8\xff":
        return "image"
    # PNG: 89 50 4E 47
    if header[:4] == b"\x89PNG":
        return "image"
    # WEBP: RIFF....WEBP
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image"
    # MP4: ....ftyp (offset 4)
    if header[4:8] == b"ftyp":
        return "video"
    # MOV
    if header[4:8] in (b"moov", b"mdat", b"wide", b"free"):
        return "video"

    return "unknown"


# Паттерны для определения feed и story картинок по имени файла (stem)
_FEED_RE = re.compile(r"^(\d+)$")
_STORY_RE = re.compile(r"^(\d+)\.1$")


def detect_placement_pairs(image_paths: list[str]) -> tuple[list[dict], list[dict]]:
    """Группирует картинки в пары лента+сторис по соглашению об именах.

    Соглашение:
      N.<ext>    — feed-картинка (лента, 1:1 / 4:5). Пример: 1.png, 2.jpg
      N.1.<ext>  — story-картинка (сторис/reels, 9:16). Пример: 1.1.png, 2.1.jpg

    Алгоритм:
      1. Для каждого пути берём basename без расширения (stem).
      2. Если stem матчит ^(\\d+)\\.1$  → это story-картинка группы \\1.
         Если stem матчит ^(\\d+)$      → это feed-картинка группы \\1.
         Иначе — картинка вне соглашения (например "winner_v2") → одиночка.
      3. Группа считается ПАРОЙ только если есть И feed (N), И story (N.1).
      4. Если у группы есть N, но нет N.1 → feed-картинка идёт в singles.
      5. Если у группы есть N.1, но нет N → story-картинка идёт в singles
         (фоллбэк: запускаем как обычную картинку, без падения).
      6. Картинки вне соглашения об именах → singles.

    Edge-cases:
      - N.2 (stem "N.2") не матчит ни один паттерн → идёт в singles с label "N.2".
      - Только N.1 без N → single с label "N.1".
      - winner.png → single с label "winner".
      - Пустой список → ([], []).

    Возвращает (pairs, singles):
      pairs   = [{"label": str, "feed": str, "story": str}, ...]  отсортировано по label (числовое, по возрастанию)
      singles = [{"label": str, "path": str}, ...]                отсортировано по path

    label для пары = номер группы как строка ("1", "2", ...).
    label для одиночки = stem файла без расширения.
    """

    # feed_map: label -> путь к feed-картинке
    feed_map: dict[str, str] = {}
    # story_map: label -> путь к story-картинке
    story_map: dict[str, str] = {}
    # singles_raw: пути к картинкам вне соглашения
    singles_raw: list[str] = []

    for path in image_paths:
        basename = os.path.basename(path)
        stem, _ext = os.path.splitext(basename)

        feed_match = _FEED_RE.match(stem)
        story_match = _STORY_RE.match(stem)

        if feed_match:
            label = feed_match.group(1)
            feed_map[label] = path
        elif story_match:
            label = story_match.group(1)
            story_map[label] = path
        else:
            # Вне соглашения (winner.png, N.2.png и т.д.) → одиночка
            singles_raw.append(path)

    # Находим группы с обеими картинками → пары
    # Группы только с feed или только с story → singles
    pair_labels = set(feed_map.keys()) & set(story_map.keys())
    only_feed_labels = set(feed_map.keys()) - pair_labels
    only_story_labels = set(story_map.keys()) - pair_labels

    # Собираем пары, сортируем по числовому значению label
    pairs: list[dict] = []
    for label in sorted(pair_labels, key=lambda x: int(x)):
        pairs.append({
            "label": label,
            "feed": feed_map[label],
            "story": story_map[label],
        })

    # Собираем одиночки: feed без пары, story без пары, вне соглашения
    singles_list: list[dict] = []
    for label in only_feed_labels:
        singles_list.append({"label": label, "path": feed_map[label]})
    for label in only_story_labels:
        # label для сторис без пары = "N.1" (stem файла)
        stem_label = label + ".1"
        singles_list.append({"label": stem_label, "path": story_map[label]})
    for path in singles_raw:
        basename = os.path.basename(path)
        stem, _ext = os.path.splitext(basename)
        singles_list.append({"label": stem, "path": path})

    # Сортируем одиночки по пути
    singles_list.sort(key=lambda x: x["path"])

    return pairs, singles_list


def detect_media_hint(drive_url: str | None) -> str | None:
    """Определяет предположительный тип медиа по URL (без скачивания).
    Используется для бейджей в UI."""
    if not drive_url:
        return None
    if "/folders/" in drive_url:
        return "media"  # Папка — точный тип узнаем при скачивании
    # Ищем расширение в URL
    url_lower = drive_url.lower()
    for ext in VIDEO_EXTS:
        if ext in url_lower:
            return "video"
    for ext in IMAGE_EXTS:
        if ext in url_lower:
            return "image"
    return "media"  # Есть ссылка, но тип неизвестен


def download_video(drive_url: str, output_path: str | None = None) -> str:
    """Обратная совместимость: скачивает видео (обёртка над download_media)."""
    media = download_media(drive_url)
    return media["paths"][0]
