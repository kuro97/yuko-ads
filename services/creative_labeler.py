"""
Инкрементальная LLM-разметка текстов креативов по таксономии.

Размечает объявления по трём осям:
  - hook_type (тип хука) — из таблицы hook_types
  - angle (угол подачи) — из таблицы angles
  - offer_type (тип оффера) — из таблицы offer_types

Работает только с неразмеченными записями (labeled_at IS NULL AND ad_body != '').
Каждый Gemini-вызов обрабатывает батч из BATCH_LABEL объявлений.
Стоимость каждого вызова логируется в llm_calls.
"""

import json
import logging
import re
import sqlite3
import time
logger = logging.getLogger(__name__)

# Количество объявлений в одном промпте к Gemini (экономия вызовов)
BATCH_LABEL = 10

# Slug для «не определено» — пишется в поля slug, но *_id остаётся NULL
_UNKNOWN_SLUG = "unknown"


def _get_connection() -> sqlite3.Connection:
    """Возвращает соединение к БД через creative_intelligence.DB_PATH."""
    from services.creative_intelligence import DB_PATH

    if DB_PATH is None:
        raise RuntimeError("KB не инициализирована. Вызовите init_kb() при старте.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _load_taxonomy() -> dict:
    """Возвращает {'hook': {slug: id}, 'angle': {slug: id}, 'offer': {slug: id}}
    из таблиц hook_types / angles / offer_types.

    Если таблицы пусты или не существуют — возвращает пустые словари
    (разметка продолжится, но *_id будет NULL для всех slug'ов).
    """
    conn = _get_connection()
    try:
        taxonomy: dict[str, dict[str, int]] = {
            "hook": {},
            "angle": {},
            "offer": {},
        }

        # hook_types
        try:
            rows = conn.execute("SELECT id, slug FROM hook_types").fetchall()
            taxonomy["hook"] = {row["slug"]: row["id"] for row in rows}
        except sqlite3.OperationalError:
            logger.warning("Таблица hook_types не найдена — hook_type_id будет NULL")

        # angles
        try:
            rows = conn.execute("SELECT id, slug FROM angles").fetchall()
            taxonomy["angle"] = {row["slug"]: row["id"] for row in rows}
        except sqlite3.OperationalError:
            logger.warning("Таблица angles не найдена — angle_id будет NULL")

        # offer_types
        try:
            rows = conn.execute("SELECT id, slug FROM offer_types").fetchall()
            taxonomy["offer"] = {row["slug"]: row["id"] for row in rows}
        except sqlite3.OperationalError:
            logger.warning("Таблица offer_types не найдена — offer_type_id будет NULL")

        return taxonomy
    finally:
        conn.close()


def _build_prompt(items: list[dict], taxonomy: dict) -> str:
    """Строит промпт для Gemini с батчем текстов.

    items: [{ad_id, ad_body, ad_headline}]
    taxonomy: {'hook': {slug: id}, 'angle': {...}, 'offer': {...}}
    """
    hook_slugs = sorted(taxonomy["hook"].keys()) or [_UNKNOWN_SLUG]
    angle_slugs = sorted(taxonomy["angle"].keys()) or [_UNKNOWN_SLUG]
    offer_slugs = sorted(taxonomy["offer"].keys()) or [_UNKNOWN_SLUG]

    # Формируем список объявлений для промпта
    ads_block_parts = []
    for item in items:
        headline = (item.get("ad_headline") or "").strip()
        body = (item.get("ad_body") or "").strip()
        ad_id = item["ad_id"]
        ads_block_parts.append(
            f'  {{"ad_id": "{ad_id}", "headline": {json.dumps(headline, ensure_ascii=False)}, '
            f'"body": {json.dumps(body[:500], ensure_ascii=False)}}}'
        )
    ads_block = "[\n" + ",\n".join(ads_block_parts) + "\n]"

    prompt = (
        "Ты — эксперт по рекламе сервиса ACME (продуктовые линейки PRODA и PRODB).\n"
        "Разметь каждое объявление по трём таксономиям.\n\n"
        f"ДОПУСТИМЫЕ hook_slug: {json.dumps(hook_slugs, ensure_ascii=False)}\n"
        f"ДОПУСТИМЫЕ angle_slug: {json.dumps(angle_slugs, ensure_ascii=False)}\n"
        f"ДОПУСТИМЫЕ offer_slug: {json.dumps(offer_slugs, ensure_ascii=False)}\n\n"
        "ПРАВИЛА:\n"
        '- Используй ТОЛЬКО slug\'i из списков выше или "unknown" если не подходит ни один.\n'
        "- Выбирай наиболее подходящий slug. Если сомневаешься — выбери ближайший.\n\n"
        f"ОБЪЯВЛЕНИЯ:\n{ads_block}\n\n"
        "Верни ТОЛЬКО валидный JSON-массив без обёрток:\n"
        '[\n'
        '  {"ad_id": "...", "hook_slug": "...", "angle_slug": "...", "offer_slug": "..."},\n'
        '  ...\n'
        ']\n'
        "Количество объектов должно совпадать с количеством входящих объявлений."
    )
    return prompt


def _call_gemini(prompt: str, model: str) -> tuple[str, int, int]:
    """Вызывает Gemini и возвращает (raw_text, input_tokens, output_tokens).

    Поднимает исключение при проблемах с API.
    """
    import google.generativeai as genai
    import config

    api_key = config.GEMINI_API_KEY
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY не задан")

    genai.configure(api_key=api_key)
    gemini_model = genai.GenerativeModel(model)

    resp = gemini_model.generate_content(
        prompt,
        generation_config={
            "response_mime_type": "application/json",
            "max_output_tokens": 2048,
        },
    )

    raw_text = resp.text or ""

    # Пробуем достать usage_metadata (не все версии SDK его возвращают)
    input_tokens = 0
    output_tokens = 0
    try:
        usage = resp.usage_metadata
        if usage:
            input_tokens = getattr(usage, "prompt_token_count", 0) or 0
            output_tokens = getattr(usage, "candidates_token_count", 0) or 0
    except Exception:
        pass

    return raw_text, input_tokens, output_tokens


def _parse_gemini_response(raw: str) -> list[dict]:
    """Парсит JSON-массив из ответа Gemini.

    При невалидном JSON возвращает пустой список.
    """
    # Прямой парсинг
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # Попытка извлечь [...] через regex (если Gemini обернул в markdown)
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    return []


def _label_batch(
    items: list[dict],
    taxonomy: dict,
    model: str,
) -> tuple[list[dict], int, int]:
    """Один Gemini-вызов: размечает батч текстов.

    items: [{ad_id, ad_body, ad_headline}]
    model: название Gemini-модели

    Возвращает (labeled_list, input_tokens, output_tokens).
    labeled_list: [{ad_id, hook_slug, angle_slug, offer_slug}]
    При ошибке парсинга — возвращает пустой список, tokens=0.

    log_llm_call с purpose='label_creative' вызывается ВНУТРИ этой функции.
    """
    from services.llm_logger import log_llm_call

    prompt = _build_prompt(items, taxonomy)
    start_ms = int(time.time() * 1000)

    try:
        raw, input_tokens, output_tokens = _call_gemini(prompt, model)
    except Exception as exc:
        latency_ms = int(time.time() * 1000) - start_ms
        log_llm_call(
            model=model,
            purpose="label_creative",
            input_tokens=0,
            output_tokens=0,
            latency_ms=latency_ms,
            error=str(exc),
        )
        logger.warning("Gemini-вызов для разметки упал: %s", exc)
        return [], 0, 0

    latency_ms = int(time.time() * 1000) - start_ms

    # Логируем вызов
    log_llm_call(
        model=model,
        purpose="label_creative",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
    )

    # Парсим ответ
    labeled = _parse_gemini_response(raw)
    return labeled, input_tokens, output_tokens


def _apply_labels(
    conn: sqlite3.Connection,
    ad_id: str,
    hook_slug: str | None,
    angle_slug: str | None,
    offer_slug: str | None,
    taxonomy: dict,
) -> None:
    """Обновляет одну запись в creative_kb разметкой.

    Slug записывается в текстовые поля hook_type/angle/offer_type.
    Если slug есть в таксономии — заполняется *_id, иначе NULL.
    labeled_at ставится всегда (чтобы не зациклиться при повторных прогонах).
    """
    hook_type_id = taxonomy["hook"].get(hook_slug) if hook_slug else None
    angle_id = taxonomy["angle"].get(angle_slug) if angle_slug else None
    offer_type_id = taxonomy["offer"].get(offer_slug) if offer_slug else None

    conn.execute(
        """UPDATE creative_kb
           SET hook_type = ?,
               angle = ?,
               offer_type = ?,
               hook_type_id = ?,
               angle_id = ?,
               offer_type_id = ?,
               labeled_at = datetime('now'),
               label_source = 'gemini'
           WHERE ad_id = ?""",
        (
            hook_slug,
            angle_slug,
            offer_slug,
            hook_type_id,
            angle_id,
            offer_type_id,
            ad_id,
        ),
    )


def label_unlabeled(limit: int = 20, model: str | None = None) -> dict:
    """Инкрементальная разметка текстов креативов по таксономии.

    Размечает ТОЛЬКО объявления где labeled_at IS NULL И ad_body != ''.
    Использует Gemini (config.GEMINI_AD_MODEL) — дёшево, текст-only.
    Каждый вызов LLM логируется через llm_logger.log_llm_call(purpose='label_creative').

    Алгоритм:
      1. Проверяем наличие GEMINI_API_KEY — если нет, возвращаем disabled=True.
      2. Выбираем до LIMIT записей без разметки.
      3. Грузим таксономию slug→id.
      4. Батчами по BATCH_LABEL вызываем Gemini.
      5. Обновляем creative_kb: hook_type / angle / offer_type (slug) + *_id + labeled_at.

    Args:
        limit: максимум объявлений за один вызов (дефолт 20)
        model: Gemini-модель; если None — берётся из config.GEMINI_AD_MODEL

    Returns:
        dict с ключами:
            labeled: int — успешно разметил
            skipped: int — объявлений без ad_body (не выбирались)
            errors: int — батчи где Gemini вернул невалидный JSON
            llm_calls: int — количество вызовов LLM
            disabled: bool — True если GEMINI_API_KEY не задан
    """
    import config

    # Проверка ключа до любых операций с БД
    if not config.GEMINI_API_KEY:
        return {"labeled": 0, "skipped": 0, "errors": 0, "llm_calls": 0, "disabled": True}

    # Модель по умолчанию из конфига
    if model is None:
        model = config.GEMINI_AD_MODEL

    # Подсчёт объявлений без body (информационно для skipped)
    conn = _get_connection()
    try:
        skipped_row = conn.execute(
            "SELECT COUNT(*) FROM creative_kb WHERE labeled_at IS NULL AND (ad_body IS NULL OR ad_body = '')"
        ).fetchone()
        skipped = skipped_row[0] if skipped_row else 0

        # Выбираем только объявления с текстом
        rows = conn.execute(
            """SELECT ad_id, ad_body, ad_headline
               FROM creative_kb
               WHERE labeled_at IS NULL AND ad_body != ''
               LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"labeled": 0, "skipped": skipped, "errors": 0, "llm_calls": 0, "disabled": False}

    # Загружаем таксономию один раз
    taxonomy = _load_taxonomy()

    items = [
        {
            "ad_id": row["ad_id"],
            "ad_body": row["ad_body"] or "",
            "ad_headline": row["ad_headline"] or "",
        }
        for row in rows
    ]

    labeled_count = 0
    error_count = 0
    llm_calls_count = 0

    # Разбиваем на батчи
    for batch_start in range(0, len(items), BATCH_LABEL):
        batch = items[batch_start : batch_start + BATCH_LABEL]
        batch_ids = {item["ad_id"] for item in batch}

        labeled_list, _inp_tok, _out_tok = _label_batch(batch, taxonomy, model)
        llm_calls_count += 1

        if not labeled_list:
            # Невалидный JSON или ошибка вызова — помечаем labeled_at, но без slug
            error_count += len(batch)
            conn = _get_connection()
            try:
                for item in batch:
                    _apply_labels(conn, item["ad_id"], None, None, None, taxonomy)
                conn.commit()
            finally:
                conn.close()
            continue

        # Применяем разметку для объявлений из ответа
        responded_ids: set[str] = set()
        conn = _get_connection()
        try:
            for label_item in labeled_list:
                ad_id = str(label_item.get("ad_id", ""))
                if not ad_id or ad_id not in batch_ids:
                    # Gemini вернул лишний или несуществующий id — пропускаем
                    continue
                hook_slug = label_item.get("hook_slug") or None
                angle_slug = label_item.get("angle_slug") or None
                offer_slug = label_item.get("offer_slug") or None

                _apply_labels(conn, ad_id, hook_slug, angle_slug, offer_slug, taxonomy)
                responded_ids.add(ad_id)
                labeled_count += 1

            # Для объявлений из батча которые Gemini не включил в ответ
            # — ставим labeled_at без slug (чтобы не зависнуть)
            missing_ids = batch_ids - responded_ids
            for missing_id in missing_ids:
                _apply_labels(conn, missing_id, None, None, None, taxonomy)
                error_count += 1

            conn.commit()
        finally:
            conn.close()

    return {
        "labeled": labeled_count,
        "skipped": skipped,
        "errors": error_count,
        "llm_calls": llm_calls_count,
        "disabled": False,
    }
