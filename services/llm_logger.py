"""Логирование LLM-вызовов и управление версиями промптов.

Используем тот же SQLite файл что и creative_intelligence.py (DB_PATH).
Таблицы llm_calls и prompt_versions создаются миграцией 007.
"""

import logging
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Тарифы Claude (USD за 1M tokens, май 2026)
MODEL_PRICING = {
    "claude-sonnet-4-20250514": {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-3-5-haiku-20241022": {
        "input": 0.80,
        "output": 4.0,
        "cache_write": 1.0,
        "cache_read": 0.08,
    },
    "claude-opus-4-6": {
        "input": 15.0,
        "output": 75.0,
        "cache_write": 18.75,
        "cache_read": 1.50,
    },
    # Актуальные модели (заменили claude-3-5-haiku-20241022, снятую с производства 2026-02-19)
    "claude-sonnet-5": {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-haiku-4-5": {
        "input": 1.0,
        "output": 5.0,
        "cache_write": 1.25,
        "cache_read": 0.10,
    },
}

# Модель по умолчанию для расчёта стоимости если модель не найдена в таблице
_DEFAULT_PRICING_MODEL = "claude-sonnet-4-20250514"


def _get_connection() -> sqlite3.Connection:
    """Возвращает соединение к БД через creative_intelligence.DB_PATH."""
    from services.creative_intelligence import DB_PATH

    if DB_PATH is None:
        raise RuntimeError("KB не инициализирована. Вызовите init_kb() при старте.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Считает стоимость вызова в USD.

    Цены Claude Sonnet (май 2026):
        input: $3 / 1M tokens
        output: $15 / 1M tokens
        cache write: $3.75 / 1M tokens
        cache read: $0.30 / 1M tokens

    Args:
        model: название модели Claude
        input_tokens: входящие токены
        output_tokens: исходящие токены
        cache_creation_tokens: токены при записи в кэш промпта
        cache_read_tokens: токены при чтении из кэша промпта

    Returns:
        float: стоимость в USD, округлённая до 6 знаков
    """
    # Берём тариф для указанной модели, при отсутствии — дефолтный
    pricing = MODEL_PRICING.get(model, MODEL_PRICING[_DEFAULT_PRICING_MODEL])

    cost = (
        input_tokens * pricing["input"] / 1_000_000
        + output_tokens * pricing["output"] / 1_000_000
        + cache_creation_tokens * pricing["cache_write"] / 1_000_000
        + cache_read_tokens * pricing["cache_read"] / 1_000_000
    )
    return round(cost, 6)


def log_llm_call(
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    total_cost_usd: float = 0.0,
    prompt_version_id: int | None = None,
    error: str | None = None,
) -> int:
    """Логирует один LLM-вызов в таблицу llm_calls.

    Стоимость пересчитывается на основе токенов; параметр total_cost_usd
    используется как переопределение только если передан ненулевой.

    Args:
        model: название модели (например, "claude-sonnet-4-20250514")
        purpose: назначение вызова ("generate_ad_batch", "analyze_pair", etc.)
        input_tokens: входящие токены
        output_tokens: исходящие токены
        latency_ms: задержка в миллисекундах
        cache_creation_tokens: токены при записи в кэш
        cache_read_tokens: токены при чтении из кэша
        total_cost_usd: итоговая стоимость (0.0 = рассчитать автоматически)
        prompt_version_id: FK на prompt_versions.id (опционально)
        error: текст ошибки если вызов упал (опционально)

    Returns:
        int: ID созданной записи в llm_calls

    Raises:
        RuntimeError: если БД не инициализирована
    """
    # Пересчитываем стоимость если не передана явно
    if total_cost_usd == 0.0:
        total_cost_usd = compute_cost(
            model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens
        )

    conn = _get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO llm_calls
               (model, purpose, input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens, total_cost_usd,
                latency_ms, prompt_version_id, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                model,
                purpose,
                input_tokens,
                output_tokens,
                cache_creation_tokens,
                cache_read_tokens,
                total_cost_usd,
                latency_ms,
                prompt_version_id,
                error,
            ),
        )
        conn.commit()
        call_id = cur.lastrowid
        logger.info(
            "LLM call logged: id=%d, model=%s, cost=$%.4f, latency=%dms",
            call_id,
            model,
            total_cost_usd,
            latency_ms,
        )
        return call_id
    finally:
        conn.close()


def get_llm_stats(days: int = 30) -> dict:
    """Агрегированная статистика LLM-вызовов за последние N дней.

    Args:
        days: период в днях (по умолчанию 30)

    Returns:
        dict с ключами:
            total_calls: int — всего вызовов
            total_input_tokens: int — суммарно входящих токенов
            total_output_tokens: int — суммарно исходящих токенов
            total_cost_usd: float — суммарная стоимость
            avg_latency_ms: int — средняя задержка
            by_model: dict — {model: {calls, cost, tokens}}
            by_purpose: dict — {purpose: {calls, cost, tokens}}

    Raises:
        RuntimeError: если БД не инициализирована
    """
    # Нижняя граница периода
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    conn = _get_connection()
    try:
        # Общая статистика за период
        summary = conn.execute(
            """SELECT
                   COUNT(*) AS total_calls,
                   COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS total_output_tokens,
                   COALESCE(SUM(total_cost_usd), 0.0) AS total_cost_usd,
                   COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
               FROM llm_calls
               WHERE created_at >= ?""",
            (since,),
        ).fetchone()

        # Разбивка по модели
        model_rows = conn.execute(
            """SELECT
                   model,
                   COUNT(*) AS calls,
                   COALESCE(SUM(total_cost_usd), 0.0) AS cost,
                   COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
               FROM llm_calls
               WHERE created_at >= ?
               GROUP BY model""",
            (since,),
        ).fetchall()

        # Разбивка по назначению
        purpose_rows = conn.execute(
            """SELECT
                   purpose,
                   COUNT(*) AS calls,
                   COALESCE(SUM(total_cost_usd), 0.0) AS cost,
                   COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
               FROM llm_calls
               WHERE created_at >= ?
               GROUP BY purpose""",
            (since,),
        ).fetchall()

    finally:
        conn.close()

    by_model = {
        row["model"]: {
            "calls": row["calls"],
            "cost": round(row["cost"], 6),
            "tokens": row["tokens"],
        }
        for row in model_rows
    }

    by_purpose = {
        row["purpose"]: {
            "calls": row["calls"],
            "cost": round(row["cost"], 6),
            "tokens": row["tokens"],
        }
        for row in purpose_rows
    }

    return {
        "total_calls": summary["total_calls"],
        "total_input_tokens": summary["total_input_tokens"],
        "total_output_tokens": summary["total_output_tokens"],
        "total_cost_usd": round(summary["total_cost_usd"], 6),
        "avg_latency_ms": int(summary["avg_latency_ms"]),
        "by_model": by_model,
        "by_purpose": by_purpose,
    }


def save_prompt_version(name: str, content: str, model: str = "") -> int:
    """Сохраняет новую версию промпта. Деактивирует предыдущие с тем же name.

    Args:
        name: уникальное имя промпта ("copywriter_v2_system", etc.)
        content: полный текст промпта
        model: для какой модели предназначен промпт (опционально)

    Returns:
        int: ID созданной записи в prompt_versions

    Raises:
        RuntimeError: если БД не инициализирована
    """
    conn = _get_connection()
    try:
        # Деактивируем все предыдущие версии с тем же именем
        conn.execute(
            "UPDATE prompt_versions SET is_active = 0 WHERE name = ? AND is_active = 1",
            (name,),
        )
        # Вставляем новую активную версию
        cur = conn.execute(
            "INSERT INTO prompt_versions (name, content, model, is_active) VALUES (?, ?, ?, 1)",
            (name, content, model),
        )
        conn.commit()
        version_id = cur.lastrowid
        logger.info("Сохранена версия промпта: name=%s, id=%d", name, version_id)
        return version_id
    finally:
        conn.close()


def get_active_prompt(name: str) -> dict | None:
    """Возвращает активную версию промпта по имени.

    Args:
        name: уникальное имя промпта

    Returns:
        dict с полями {id, name, content, model, is_active, created_at}
        или None если активная версия не найдена

    Raises:
        RuntimeError: если БД не инициализирована
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            """SELECT id, name, content, model, is_active, created_at
               FROM prompt_versions
               WHERE name = ? AND is_active = 1
               ORDER BY id DESC
               LIMIT 1""",
            (name,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
