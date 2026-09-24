"""
Pattern Miner — детерминированные агрегации срезов creative_kb → learnings.

Агрегирует creative_kb по типам срезов (hook_type, city, adset_type и комбинации),
находит значимые отклонения от базовой линии и записывает уроки в таблицу learnings
с source='pattern_miner'. Ручные уроки (source='manual') не трогаются.

Шкала confidence строго по §5.4 спеки:
  confirmed  — n_ads >= 20 AND total_spend >= 400
  probable   — n_ads >= 10 AND total_spend >= 150
  hypothesis — иначе (прошёл базовый порог n_ads>=5 AND spend>=50)
"""

import json
import logging
import sqlite3
from typing import Optional

import config

logger = logging.getLogger(__name__)

# --- Константы модуля ---

# Минимальные пороги среза (§5.4)
MIN_ADS_PER_SLICE = 5       # минимум объявлений в срезе
MIN_SPEND_PER_SLICE = 50.0  # минимум суммарного spend ($) в срезе

# Минимальный lift для значимости: лучше базы на 30%+ ИЛИ хуже на 30%+
LIFT_POSITIVE_THRESHOLD = 1.3
LIFT_NEGATIVE_THRESHOLD = 0.7

# Максимальное количество ad_id в evidence (первые N из group_concat)
MAX_EVIDENCE_ADS = 20

# Whitelist измерений для GROUP BY (SQL-безопасность: никакого f-string из ввода)
ALLOWED_DIMENSIONS = {"hook_type", "city", "adset_type"}

# Флаг включения LLM-формулировки (опционально, graceful)
MINER_USE_LLM = False  # включить через env в будущем если нужно


def _get_connection() -> sqlite3.Connection:
    """Подключение к БД через creative_intelligence.DB_PATH."""
    from services.creative_intelligence import DB_PATH
    if DB_PATH is None:
        raise RuntimeError("KB не инициализирована. Вызовите init_kb() при старте.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _confidence(n_ads: int, total_spend: float) -> str:
    """Рассчитывает confidence по объёму данных (формула §5.4).

    Шкала: hypothesis/probable/confirmed.
    Пороги монотонные — чем больше данных, тем выше уверенность.
    """
    if n_ads >= 20 and total_spend >= 400:
        return "confirmed"
    if n_ads >= 10 and total_spend >= 150:
        return "probable"
    # Базовый порог n_ads>=5 AND spend>=50 даёт минимум hypothesis
    return "hypothesis"


def _format_statement(slice_data: dict, lift: float, baseline: dict) -> str:
    """Детерминированный шаблон текста урока (русский).

    Формат по примерам из §5.5 спеки.
    """
    key: dict = slice_data["key"]
    n_ads: int = slice_data["n_ads"]
    total_spend: float = slice_data["total_spend"]
    avg_qual_pct: float = slice_data["avg_qual_pct"] or 0.0
    avg_cpl: float = slice_data["avg_cpl"] or 0.0
    baseline_qual: float = baseline.get("avg_qual_pct") or 0.0
    baseline_cpl: float = baseline.get("avg_cpl") or 0.0

    # Строим человекочитаемое описание среза
    parts = []
    if "hook_type" in key and key["hook_type"]:
        parts.append(f"хук «{key['hook_type']}»")
    if "city" in key and key["city"]:
        parts.append(f"в {key['city']}")
    if "adset_type" in key and key["adset_type"]:
        parts.append(f"адсеты {key['adset_type']}")

    slice_desc = " ".join(parts) if parts else "срез"

    # Определяем направление эффекта
    if lift >= LIFT_POSITIVE_THRESHOLD:
        # Лучше базы: акцент на качестве (qual_pct)
        qual_pct_str = f"{avg_qual_pct:.0f}%"
        base_qual_str = f"{baseline_qual:.0f}%"
        lift_str = f"{lift:.1f}x"
        statement = (
            f"Срез «{slice_desc}» даёт квалификацию {lift_str} выше базы "
            f"({qual_pct_str} против {base_qual_str}). "
            f"Срез: {n_ads} объявлений, расход ${total_spend:.0f}."
        )
    else:
        # Хуже базы или CPL-ориентированный
        if avg_cpl > 0 and baseline_cpl > 0:
            cpl_diff_pct = abs(avg_cpl - baseline_cpl) / baseline_cpl * 100
            if avg_cpl < baseline_cpl:
                # CPL ниже = лучше
                statement = (
                    f"Срез «{slice_desc}» показывает CPL на {cpl_diff_pct:.0f}% ниже базы "
                    f"(${avg_cpl:.1f} против ${baseline_cpl:.1f}). "
                    f"Срез: {n_ads} объявлений, расход ${total_spend:.0f}."
                )
            else:
                qual_pct_str = f"{avg_qual_pct:.0f}%"
                base_qual_str = f"{baseline_qual:.0f}%"
                statement = (
                    f"Срез «{slice_desc}» квалифицируется хуже базы "
                    f"({qual_pct_str} против {base_qual_str}). "
                    f"Срез: {n_ads} объявлений, расход ${total_spend:.0f}."
                )
        else:
            qual_pct_str = f"{avg_qual_pct:.0f}%"
            base_qual_str = f"{baseline_qual:.0f}%"
            statement = (
                f"Срез «{slice_desc}» квалифицируется хуже базы "
                f"({qual_pct_str} против {base_qual_str}). "
                f"Срез: {n_ads} объявлений, расход ${total_spend:.0f}."
            )

    return statement


def _compute_baseline() -> dict:
    """Средневзвешенный qual_pct и cpl по всем объявлениям is_full_cabinet=1 и spend>0.

    Returns:
        {"avg_qual_pct": float, "avg_cpl": float, "n_ads": int, "total_spend": float}
    Если данных нет — все значения 0.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                AVG(qual_pct)   AS avg_qual_pct,
                AVG(cpl)        AS avg_cpl,
                COUNT(*)        AS n_ads,
                SUM(spend)      AS total_spend
            FROM creative_kb
            WHERE is_full_cabinet = 1 AND spend > 0
            """
        ).fetchone()
        if not row or row["n_ads"] == 0:
            return {"avg_qual_pct": 0.0, "avg_cpl": 0.0, "n_ads": 0, "total_spend": 0.0}
        return {
            "avg_qual_pct": float(row["avg_qual_pct"] or 0.0),
            "avg_cpl":      float(row["avg_cpl"] or 0.0),
            "n_ads":        int(row["n_ads"]),
            "total_spend":  float(row["total_spend"] or 0.0),
        }
    finally:
        conn.close()


def _aggregate_slice(conn: sqlite3.Connection, dimensions: list[str]) -> list[dict]:
    """Агрегирует creative_kb по указанным dimensions.

    ВАЖНО (SQL-безопасность): dimensions берутся ТОЛЬКО из ALLOWED_DIMENSIONS —
    никакого f-string из пользовательского ввода.

    Фильтрует строки: is_full_cabinet=1 AND spend>0 AND каждый dim IS NOT NULL AND != ''.

    Returns:
        list[dict] с полями: key(dict), n_ads, total_spend, avg_qual_pct, avg_cpl, evidence_ad_ids
    """
    # Валидируем dimensions по whitelist
    for dim in dimensions:
        if dim not in ALLOWED_DIMENSIONS:
            raise ValueError(f"Недопустимое измерение: {dim}. Разрешены: {ALLOWED_DIMENSIONS}")

    # Строим SQL из whitelisted колонок
    dims_select = ", ".join(dimensions)
    dims_group = ", ".join(dimensions)

    # Условия фильтрации по не-пустым dim-значениям
    not_null_conditions = " AND ".join(
        f"({d} IS NOT NULL AND {d} != '')" for d in dimensions
    )

    sql = f"""
        SELECT
            {dims_select},
            COUNT(*)          AS n_ads,
            SUM(spend)        AS total_spend,
            AVG(qual_pct)     AS avg_qual_pct,
            AVG(cpl)          AS avg_cpl,
            group_concat(ad_id) AS evidence
        FROM creative_kb
        WHERE is_full_cabinet = 1
          AND spend > 0
          AND {not_null_conditions}
        GROUP BY {dims_group}
    """

    rows = conn.execute(sql).fetchall()
    result = []
    for row in rows:
        # Собираем key из dimension-значений
        key = {dim: row[dim] for dim in dimensions}

        # evidence_ad_ids — первые MAX_EVIDENCE_ADS из group_concat
        evidence_str: Optional[str] = row["evidence"]
        if evidence_str:
            evidence_ad_ids = evidence_str.split(",")[:MAX_EVIDENCE_ADS]
        else:
            evidence_ad_ids = []

        result.append({
            "key":            key,
            "n_ads":          int(row["n_ads"]),
            "total_spend":    float(row["total_spend"] or 0.0),
            "avg_qual_pct":   float(row["avg_qual_pct"] or 0.0),
            "avg_cpl":        float(row["avg_cpl"] or 0.0),
            "evidence_ad_ids": evidence_ad_ids,
        })
    return result


def mine_patterns() -> dict:
    """Детерминированный pattern-miner: агрегации по срезам → learnings.

    Алгоритм:
      1. Вычисляем baseline (средневзвешенные метрики всего кабинета).
      2. Для каждого типа среза (hook_type, city, adset_type, hook×city, hook×adset_type)
         агрегируем и фильтруем по порогам.
      3. Для каждого прошедшего среза считаем lift и confidence.
         Берём только значимые: lift >= 1.3 (лучше) или <= 0.7 (хуже).
      4. Удаляем старые source='pattern_miner' + INSERT новых — в одной транзакции.

    Returns:
        {"learnings_written": N, "slices_evaluated": M, "baseline": {...}}
    """
    baseline = _compute_baseline()

    # Нет данных в baseline — не можем считать lift, возвращаем пустой результат
    if baseline["avg_qual_pct"] == 0 and baseline["n_ads"] == 0:
        logger.info("pattern_miner: baseline пустой, уроки не записаны")
        return {"learnings_written": 0, "slices_evaluated": 0, "baseline": baseline}

    # Типы срезов: одиночные + комбинированные
    slice_types = [
        ["hook_type"],
        ["city"],
        ["adset_type"],
        ["hook_type", "city"],
        ["hook_type", "adset_type"],
    ]

    # Собираем все уроки для записи
    new_learnings: list[dict] = []
    slices_evaluated = 0

    conn = _get_connection()
    try:
        for dimensions in slice_types:
            slices = _aggregate_slice(conn, dimensions)
            for sl in slices:
                # Фильтр по базовому порогу
                if sl["n_ads"] < MIN_ADS_PER_SLICE or sl["total_spend"] < MIN_SPEND_PER_SLICE:
                    continue

                slices_evaluated += 1

                # Считаем lift по qual_pct
                if baseline["avg_qual_pct"] > 0:
                    lift = sl["avg_qual_pct"] / baseline["avg_qual_pct"]
                else:
                    # Если baseline qual = 0 — используем lift по CPL (меньше = лучше)
                    if baseline["avg_cpl"] > 0 and sl["avg_cpl"] > 0:
                        # CPL ниже базы на 30%+ тоже значимо
                        lift = baseline["avg_cpl"] / sl["avg_cpl"]
                    else:
                        continue  # нет данных для сравнения

                # Берём только значимые срезы
                if not (lift >= LIFT_POSITIVE_THRESHOLD or lift <= LIFT_NEGATIVE_THRESHOLD):
                    continue

                confidence = _confidence(sl["n_ads"], sl["total_spend"])
                statement = _format_statement(sl, lift, baseline)

                # Строим теги из dimension-значений
                tag_parts = ["pattern_miner"]
                for dim, val in sl["key"].items():
                    if val:
                        # Краткий формат: hook:authority_expert, city:CityB
                        dim_short = {"hook_type": "hook", "adset_type": "adset_type"}.get(dim, dim)
                        tag_parts.append(f"{dim_short}:{val}")
                tags = ",".join(tag_parts)

                new_learnings.append({
                    "statement":      statement,
                    "evidence_ad_ids": json.dumps(sl["evidence_ad_ids"], ensure_ascii=False),
                    "confidence":      confidence,
                    "tags":            tags,
                })

        # Опциональная LLM-формулировка (graceful, один батч на все уроки)
        if new_learnings and MINER_USE_LLM and config.ANTHROPIC_API_KEY:
            new_learnings = _llm_reformat_batch(new_learnings)

        # Атомарная транзакция: DELETE старых + INSERT новых
        with conn:
            # Ручные уроки (source='manual') НЕ трогаем
            conn.execute("DELETE FROM learnings WHERE source = 'pattern_miner'")
            for learning in new_learnings:
                conn.execute(
                    """
                    INSERT INTO learnings (statement, evidence_ad_ids, confidence, source, tags)
                    VALUES (?, ?, ?, 'pattern_miner', ?)
                    """,
                    (
                        learning["statement"],
                        learning["evidence_ad_ids"],
                        learning["confidence"],
                        learning["tags"],
                    ),
                )

    finally:
        conn.close()

    logger.info(
        "pattern_miner: записано %d уроков, оценено %d срезов",
        len(new_learnings),
        slices_evaluated,
    )

    return {
        "learnings_written": len(new_learnings),
        "slices_evaluated":  slices_evaluated,
        "baseline":          baseline,
    }


def _llm_reformat_batch(learnings: list[dict]) -> list[dict]:
    """Опциональная LLM-формулировка всех уроков одним батчем (graceful).

    Если Claude недоступен или вернул ошибку — возвращаем исходные шаблонные тексты.
    """
    try:
        import anthropic
        from services import llm_logger

        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        # Формируем JSON-список statements для переформулировки
        statements_input = [
            {"index": i, "statement": l["statement"]}
            for i, l in enumerate(learnings)
        ]

        prompt = (
            "Перефразируй каждый рекламный инсайт в таблице более живым языком (русский). "
            "Сохрани все числа. Верни JSON-массив объектов [{index, statement}].\n\n"
            f"Инсайты:\n{json.dumps(statements_input, ensure_ascii=False, indent=2)}"
        )

        import time
        start = time.monotonic()
        response = client.messages.create(
            model=config.CLAUDE_HAIKU_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        # Логируем вызов LLM
        usage = response.usage
        llm_logger.log_llm_call(
            model=config.CLAUDE_HAIKU_MODEL,
            purpose="format_learnings",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=latency_ms,
        )

        # Парсим ответ
        raw = response.content[0].text if response.content else ""
        # Ищем JSON-массив в ответе
        start_idx = raw.find("[")
        end_idx = raw.rfind("]") + 1
        if start_idx >= 0 and end_idx > start_idx:
            reformatted = json.loads(raw[start_idx:end_idx])
            for item in reformatted:
                idx = item.get("index")
                stmt = item.get("statement", "").strip()
                if isinstance(idx, int) and stmt and 0 <= idx < len(learnings):
                    learnings[idx]["statement"] = stmt

    except Exception as exc:  # noqa: BLE001
        # Graceful degradation: любая ошибка → шаблонный текст
        logger.warning("pattern_miner LLM-формулировка не удалась: %s", exc)

    return learnings
