"""
Pattern Engine — предикторы успеха объявлений по РАННИМ метрикам (день 1-7).

Анти-утечка: успех — таргет (lifetime AMO-исходы из creative_kb),
фичи-предикторы — ТОЛЬКО ad_daily_metrics с day_since_launch BETWEEN 1 AND 7.

Результат: уроки в таблицу learnings (source='pattern_engine',
confidence hypothesis/probable/confirmed) + структурированный отчёт.
"""

import json
import logging
import statistics
import sqlite3

from services.creative_intelligence import _get_connection

logger = logging.getLogger(__name__)

# --- Пороги определения успеха (§6 / Группа D ресёрча) ---
MIN_QUAL_LEADS = 1          # минимум квал-лидов чтобы объявление вообще участвовало
MIN_SPEND_FOR_EVAL = 15.0   # минимум lifetime spend ($) — иначе данные случайны
MIN_SUCCESS_ADS = 5         # минимум успешных объявлений для выводов (иначе insufficient)
MIN_FAIL_ADS = 5            # минимум неуспешных объявлений
MIN_DAY17_ROWS = 1          # минимум дневных строк day1-7 у объявления чтобы учесть фичи

# --- Метрики-кандидаты в предикторы (ранние, day1-7) ---
# (имя метрики, направление "higher_better" | "lower_better", априорный порог-ориентир из ресёрча)
EARLY_METRICS = [
    ("hook_rate",     "higher_better", 25.0),   # % — стоп-сигнал <20, solid 25-35
    ("hold_rate",     "higher_better", 40.0),   # % — слабый <30, средний 40-50
    ("ctr",           "higher_better", 1.2),    # % — стоп <0.8, норма 1.2-1.9 (lead-objective)
    ("cpm",           "lower_better",  None),   # $ — относительный, абсолютного порога нет
    ("cpl",           "lower_better",  None),   # $ — день1-7 завышен на 30-50% (learning phase)
    ("lead_velocity", "higher_better", None),   # лидов/день среднее за day1-7
]

# Минимальный значимый lift предиктора: медиана успешных лучше неуспешных в N раз
LIFT_THRESHOLD = 1.2

# Максимум ad_id в evidence урока
MAX_EVIDENCE_ADS = 20


def _classify_success() -> dict[str, bool]:
    """Возвращает {ad_id: is_success} по объявлениям creative_kb.

    Кандидаты: is_full_cabinet=1 AND spend >= MIN_SPEND_FOR_EVAL.
    Медианы по кандидатам: median_romi (по тем у кого romi не NULL),
    median_cpl (по spend>0).
    Успех (is_success=True), если ВЫПОЛНЕНО ОБА:
        qual_leads >= MIN_QUAL_LEADS
        AND (romi >= median_romi ИЛИ (romi is NULL/0 AND cpl <= median_cpl AND cpl>0))
    Иначе неуспех. Объявления без spend/квалов в кандидаты не попадают вовсе.
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT ad_id, spend, qual_leads, romi, cpl
            FROM creative_kb
            WHERE is_full_cabinet = 1
              AND spend >= ?
            """,
            (MIN_SPEND_FOR_EVAL,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {}

    # Собираем медианы
    romi_values = [r["romi"] for r in rows if r["romi"] is not None and float(r["romi"] or 0) > 0]
    cpl_values = [r["cpl"] for r in rows if r["cpl"] is not None and float(r["cpl"] or 0) > 0]

    median_romi = statistics.median(romi_values) if romi_values else None
    median_cpl = statistics.median(cpl_values) if cpl_values else None

    result: dict[str, bool] = {}
    for row in rows:
        ad_id = str(row["ad_id"])
        qual_leads = int(row["qual_leads"] or 0)
        romi = row["romi"]
        cpl = float(row["cpl"] or 0)

        # Первый фильтр: есть квал-лиды
        if qual_leads < MIN_QUAL_LEADS:
            result[ad_id] = False
            continue

        romi_val = float(romi) if romi is not None else 0.0
        has_romi = romi_val > 0

        # Второй фильтр: ROMI выше медианы ИЛИ CPL ниже медианы
        if has_romi and median_romi is not None and romi_val >= median_romi:
            result[ad_id] = True
        elif (not has_romi or median_romi is None) and median_cpl is not None and cpl > 0 and cpl <= median_cpl:
            result[ad_id] = True
        else:
            result[ad_id] = False

    return result


def _fetch_early_features(ad_ids: list[str]) -> dict[str, dict]:
    """Для каждого ad_id агрегирует РАННИЕ метрики day1-7 из ad_daily_metrics.

    SQL: WHERE day_since_launch BETWEEN 1 AND 7 AND ad_id IN (...).
    На объявление считает средневзвешенные/медианные ранние метрики:
        hook_rate (AVG по дням где не NULL),
        hold_rate (AVG где не NULL),
        ctr (AVG где impressions>0),
        cpm (SUM(spend)/SUM(impressions)*1000, если impressions>0),
        cpl (SUM(spend)/SUM(leads), если leads>0, иначе None),
        lead_velocity (SUM(leads) / число_дней_с_данными),
        days_with_data (COUNT дней day1-7).
    Returns: {ad_id: {hook_rate, hold_rate, ctr, cpm, cpl, lead_velocity, days_with_data}}.
    Объявления без строк day1-7 в результат не попадают.

    Батчинг: SQLite ограничен 999 переменными на запрос.
    Разбиваем ad_ids на батчи по BATCH_SIZE и сливаем результаты.
    Батчи не пересекаются по ad_id, поэтому достаточно dict.update().
    """
    if not ad_ids:
        return {}

    # SQLite лимит 999 переменных; оставляем запас (1 переменная — MIN_DAY17_ROWS)
    BATCH_SIZE = 900

    result: dict[str, dict] = {}

    conn = _get_connection()
    try:
        # Разбиваем список на батчи и выполняем запрос для каждого
        for batch_start in range(0, len(ad_ids), BATCH_SIZE):
            batch = ad_ids[batch_start : batch_start + BATCH_SIZE]

            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"""
                SELECT
                    ad_id,
                    AVG(CASE WHEN hook_rate IS NOT NULL THEN hook_rate END) AS avg_hook_rate,
                    AVG(CASE WHEN hold_rate IS NOT NULL THEN hold_rate END) AS avg_hold_rate,
                    AVG(CASE WHEN impressions > 0 THEN ctr END)             AS avg_ctr,
                    SUM(spend)                                               AS total_spend,
                    SUM(impressions)                                         AS total_impressions,
                    SUM(leads)                                               AS total_leads,
                    COUNT(*)                                                 AS days_with_data
                FROM ad_daily_metrics
                WHERE day_since_launch BETWEEN 1 AND 7
                  AND lead_semantics_version = 2
                  AND lead_parse_status IN ('ok', 'component_mismatch')
                  AND ad_id IN ({placeholders})
                GROUP BY ad_id
                HAVING COUNT(*) >= ?
                """,
                (*batch, MIN_DAY17_ROWS),
            ).fetchall()

            for row in rows:
                ad_id = str(row["ad_id"])
                total_spend = float(row["total_spend"] or 0)
                total_impressions = int(row["total_impressions"] or 0)
                total_leads = int(row["total_leads"] or 0)
                days_with_data = int(row["days_with_data"] or 1)

                cpm = (total_spend / total_impressions * 1000) if total_impressions > 0 else None
                cpl = (total_spend / total_leads) if total_leads > 0 else None
                lead_velocity = total_leads / days_with_data

                result[ad_id] = {
                    "hook_rate": row["avg_hook_rate"],
                    "hold_rate": row["avg_hold_rate"],
                    "ctr": row["avg_ctr"],
                    "cpm": cpm,
                    "cpl": cpl,
                    "lead_velocity": lead_velocity,
                    "days_with_data": days_with_data,
                }
    finally:
        conn.close()

    return result


def _median(values: list[float]) -> float | None:
    """Медиана непустого списка (None отфильтрованы вызывающим). None если список пуст."""
    if not values:
        return None
    return statistics.median(values)


def _build_predictors(
    success_feats: list[dict],
    fail_feats: list[dict],
) -> list[dict]:
    """Сравнивает распределения ранних метрик у успешных vs неуспешных.

    Для каждой метрики из EARLY_METRICS:
      - собираем не-None значения у успешных и неуспешных,
      - считаем медианы s_med, f_med,
      - lift = s_med/f_med (higher_better) или f_med/s_med (lower_better),
      - значимо если lift >= LIFT_THRESHOLD,
      - threshold предиктора = середина между s_med и f_med (округлённая),
        но не ниже априорного ориентира из EARLY_METRICS если он задан и higher_better.
    Returns: list[dict] предикторов:
        {metric, direction, threshold, lift, success_median, fail_median,
         n_success, n_fail, prior_benchmark}.
    Только значимые (lift>=LIFT_THRESHOLD) и где обе медианы не None.
    """
    predictors = []

    for metric_name, direction, prior_benchmark in EARLY_METRICS:
        # Собираем не-None значения
        s_values = [f[metric_name] for f in success_feats if f.get(metric_name) is not None]
        f_values = [f[metric_name] for f in fail_feats if f.get(metric_name) is not None]

        s_med = _median(s_values)
        f_med = _median(f_values)

        # Не хватает данных для сравнения
        if s_med is None or f_med is None:
            continue

        # Защита от деления на ноль
        if f_med == 0:
            if direction == "lower_better":
                continue  # у неуспешных 0 — невозможно посчитать lift
            # У lower_better: f_med=0, нет смысла; у higher_better — бесконечный lift, пропускаем
            continue

        # Расчёт lift
        if direction == "higher_better":
            # Успешные выше → lift > 1 значит лучше
            if s_med == 0:
                continue
            lift = round(s_med / f_med, 3)
        else:
            # lower_better: у успешных меньше → lift > 1 значит лучше
            if s_med == 0:
                continue
            lift = round(f_med / s_med, 3)

        # Фильтрация по значимости
        if lift < LIFT_THRESHOLD:
            continue

        # Порог предиктора: середина между медианами
        midpoint = round((s_med + f_med) / 2, 3)

        # Для higher_better: не ниже априорного ориентира из ресёрча
        if direction == "higher_better" and prior_benchmark is not None:
            threshold = round(max(midpoint, prior_benchmark), 3)
        else:
            threshold = midpoint

        predictors.append({
            "metric": metric_name,
            "direction": direction,
            "threshold": threshold,
            "lift": lift,
            "success_median": round(s_med, 3),
            "fail_median": round(f_med, 3),
            "n_success": len(s_values),
            "n_fail": len(f_values),
            "prior_benchmark": prior_benchmark,
        })

    return predictors


def _confidence(n_success: int, n_fail: int) -> str:
    """Шкала как в pattern_miner (НЕ low/medium/high):
        confirmed  — n_success >= 15 AND n_fail >= 15
        probable   — n_success >= 8  AND n_fail >= 8
        hypothesis — иначе.
    """
    if n_success >= 15 and n_fail >= 15:
        return "confirmed"
    if n_success >= 8 and n_fail >= 8:
        return "probable"
    return "hypothesis"


def _format_statement(predictor: dict) -> str:
    """Детерминированный русский текст урока. Пример:
    "Ранний предиктор: hook_rate >= 30% в дни 1-7 связан с успехом
     (медиана успешных 34% против 19% у неуспешных, lift 1.8x).
     Выборка: 12 успешных / 20 неуспешных."
    """
    metric = predictor["metric"]
    direction = predictor["direction"]
    threshold = predictor["threshold"]
    lift = predictor["lift"]
    s_med = predictor["success_median"]
    f_med = predictor["fail_median"]
    n_s = predictor["n_success"]
    n_f = predictor["n_fail"]

    # Определяем знак и единицу для читаемости
    if metric in ("hook_rate", "hold_rate", "ctr"):
        unit = "%"
    elif metric in ("cpm", "cpl"):
        unit = "$"
    else:
        unit = ""

    if direction == "higher_better":
        sign = ">="
    else:
        sign = "<="

    return (
        f"Ранний предиктор: {metric} {sign} {threshold}{unit} в дни 1-7 связан с успехом "
        f"(медиана успешных {s_med}{unit} против {f_med}{unit} у неуспешных, lift {lift}x). "
        f"Выборка: {n_s} успешных / {n_f} неуспешных."
    )


def run_pattern_engine() -> dict:
    """Главная функция: считает предикторы успеха и пишет уроки.

    1. success_map = _classify_success().
    2. success_ids = [id для is_success], fail_ids = [id для not is_success].
    3. data_sufficiency: если len(success_ids) < MIN_SUCCESS_ADS или
       len(fail_ids) < MIN_FAIL_ADS -> "insufficient" -> вернуть пустой результат,
       уроки НЕ перезаписывать.
    4. early = _fetch_early_features(success_ids + fail_ids).
       success_feats = [early[id] для id in success_ids если id in early];
       fail_feats аналогично.
    5. Если success_feats или fail_feats пусты -> "insufficient" -> пустой результат.
    6. predictors = _build_predictors(success_feats, fail_feats).
    7. confidence = _confidence(len(success_feats), len(fail_feats)).
    8. Транзакция: DELETE FROM learnings WHERE source='pattern_engine';
       INSERT по одному уроку на каждый предиктор (statement, evidence_ad_ids=JSON success_ids[:20],
       confidence, source='pattern_engine', tags="pattern_engine,metric:<name>").
    9. Returns: {"predictors": [...], "learnings_written": N, "n_success", "n_fail",
                 "data_sufficiency": "ok"|"insufficient"}.
    НИКОГДА не бросает наружу programmatic ошибки наверх в крон (крон сам ловит),
    но при RuntimeError "KB не инициализирована" пробрасывает (эндпоинт ловит -> 503).
    """
    # _get_connection() бросит RuntimeError если KB не инициализирована — пробрасываем
    success_map = _classify_success()

    success_ids = [ad_id for ad_id, is_s in success_map.items() if is_s]
    fail_ids = [ad_id for ad_id, is_s in success_map.items() if not is_s]

    # Проверяем достаточность данных
    if len(success_ids) < MIN_SUCCESS_ADS or len(fail_ids) < MIN_FAIL_ADS:
        logger.info(
            "run_pattern_engine: insufficient data — success=%d fail=%d (min %d/%d)",
            len(success_ids), len(fail_ids), MIN_SUCCESS_ADS, MIN_FAIL_ADS,
        )
        return {
            "predictors": [],
            "learnings_written": 0,
            "n_success": len(success_ids),
            "n_fail": len(fail_ids),
            "data_sufficiency": "insufficient",
        }

    # Получаем ранние фичи (только day1-7, анти-утечка)
    all_ids = success_ids + fail_ids
    early = _fetch_early_features(all_ids)

    success_feats = [early[ad_id] for ad_id in success_ids if ad_id in early]
    fail_feats = [early[ad_id] for ad_id in fail_ids if ad_id in early]

    if not success_feats or not fail_feats:
        logger.info(
            "run_pattern_engine: нет ранних фич — success_feats=%d fail_feats=%d",
            len(success_feats), len(fail_feats),
        )
        return {
            "predictors": [],
            "learnings_written": 0,
            "n_success": len(success_ids),
            "n_fail": len(fail_ids),
            "data_sufficiency": "insufficient",
        }

    predictors = _build_predictors(success_feats, fail_feats)
    conf = _confidence(len(success_feats), len(fail_feats))

    # Evidence — первые MAX_EVIDENCE_ADS успешных ad_id
    evidence_ad_ids = success_ids[:MAX_EVIDENCE_ADS]

    # Транзакция: удаляем старые pattern_engine уроки и пишем новые
    conn = _get_connection()
    try:
        # Удаляем только pattern_engine уроки (manual и pattern_miner не трогаем)
        conn.execute("DELETE FROM learnings WHERE source = 'pattern_engine'")

        learnings_written = 0
        for pred in predictors:
            metric_name = pred["metric"]
            statement = _format_statement(pred)
            tags = f"pattern_engine,metric:{metric_name}"

            conn.execute(
                """
                INSERT INTO learnings
                    (statement, evidence_ad_ids, confidence, source, tags, created_at)
                VALUES (?, ?, ?, 'pattern_engine', ?, datetime('now'))
                """,
                (
                    statement,
                    json.dumps(evidence_ad_ids, ensure_ascii=False),
                    conf,
                    tags,
                ),
            )
            learnings_written += 1

        conn.commit()
    finally:
        conn.close()

    logger.info(
        "run_pattern_engine: predictors=%d learnings_written=%d success=%d fail=%d",
        len(predictors), learnings_written, len(success_feats), len(fail_feats),
    )

    return {
        "predictors": predictors,
        "learnings_written": learnings_written,
        "n_success": len(success_feats),
        "n_fail": len(fail_feats),
        "data_sufficiency": "ok",
    }


def get_patterns_summary() -> dict:
    """Сводка для GET /api/brain/patterns. Читает последние уроки source='pattern_engine'
    + пересчитывает быстрый snapshot data_sufficiency по counts.

    Returns: {"predictors": [...], "data_sufficiency": str, "n_success": int, "n_fail": int,
              "generated_at": str|None}.
    predictors восстанавливаются из learnings (statement + tags + confidence) —
    НЕ пересчитываем заново (дёшево, без FB). Если уроков нет — predictors=[].
    """
    conn = _get_connection()
    try:
        # Читаем уроки pattern_engine
        rows = conn.execute(
            """
            SELECT statement, tags, confidence, evidence_ad_ids, created_at
            FROM learnings
            WHERE source = 'pattern_engine'
            ORDER BY created_at DESC
            """,
        ).fetchall()

        # Быстрый пересчёт counts для data_sufficiency (только SQLite, без FB)
        success_map = _classify_success()
    finally:
        conn.close()

    success_ids = [ad_id for ad_id, is_s in success_map.items() if is_s]
    fail_ids = [ad_id for ad_id, is_s in success_map.items() if not is_s]
    n_success = len(success_ids)
    n_fail = len(fail_ids)

    data_sufficiency = "ok" if (n_success >= MIN_SUCCESS_ADS and n_fail >= MIN_FAIL_ADS) else "insufficient"

    # Восстанавливаем предикторы из learnings
    predictors = []
    generated_at = None
    for row in rows:
        tags = row["tags"] or ""
        # Извлекаем имя метрики из тега "pattern_engine,metric:<name>"
        metric_name = None
        for tag in tags.split(","):
            if tag.startswith("metric:"):
                metric_name = tag[len("metric:"):]
                break

        if generated_at is None:
            generated_at = row["created_at"]

        predictors.append({
            "metric": metric_name,
            "statement": row["statement"],
            "confidence": row["confidence"],
            "tags": tags,
        })

    return {
        "predictors": predictors,
        "data_sufficiency": data_sufficiency,
        "n_success": n_success,
        "n_fail": n_fail,
        "generated_at": generated_at,
        "learnings_written": 0,  # GET не пишет
    }
