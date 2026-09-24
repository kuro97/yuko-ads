"""
Creative Intelligence — Knowledge Base: единая таблица creative_kb.

Фаза 1: синхронизация данных из 3 источников:
  - data/learner_results.json — FB метрики + классификация
  - data/amo_data.json       — AMO метрики (обогащение)
  - data/vision_analysis.json — Gemini vision tags

Фаза 2.1: скоринг через Gemini (visual + text + customer) + рубрика 0-100.
"""

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.database_migrations import (
    apply_runtime_migrations,
    verify_runtime_schema,
)

logger = logging.getLogger(__name__)

# Путь к БД (тот же файл что и agent/database.py — data/decisions.db)
_DATA_DIR = Path(__file__).parent.parent / "data"
DB_PATH: str | None = None


def _get_connection() -> sqlite3.Connection:
    """Возвращает соединение к текущей БД."""
    if DB_PATH is None:
        raise RuntimeError("KB не инициализирована. Вызовите init_kb() при старте.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _apply_scoring_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 005 идемпотентно.

    SQLite не поддерживает IF NOT EXISTS для ALTER TABLE,
    поэтому ловим OperationalError 'duplicate column' и продолжаем.
    """
    migration_path = Path(__file__).parent.parent / "migrations" / "005_scoring_columns.sql"
    migration_sql = migration_path.read_text(encoding="utf-8")

    for stmt in migration_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            # Колонка уже существует — игнорируем
            if "duplicate column" in str(exc).lower():
                continue
            raise
    conn.commit()


def _apply_image_url_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 006 идемпотентно.

    Добавляет колонку image_url и индекс. При повторном запуске
    ловим 'duplicate column' и продолжаем.
    """
    migration_path = Path(__file__).parent.parent / "migrations" / "006_image_url.sql"
    migration_sql = migration_path.read_text(encoding="utf-8")

    for stmt in migration_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            # Колонка или индекс уже существуют — игнорируем
            if "duplicate column" in str(exc).lower() or "already exists" in str(exc).lower():
                continue
            raise
    conn.commit()


def _apply_v2_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 007 идемпотентно.

    Создаёт новые таблицы (IF NOT EXISTS) и добавляет колонки в creative_kb.
    ALTER TABLE ловим duplicate column и продолжаем.
    Если файл миграции ещё не создан — пропускаем.
    """
    migration_path = Path(__file__).parent.parent / "migrations" / "007_ad_agent_v2.sql"
    if not migration_path.exists():
        return

    migration_sql = migration_path.read_text(encoding="utf-8")

    # Разбиваем на отдельные statement'ы по ";"
    for stmt in migration_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            # Колонка уже существует — OK
            if "duplicate column" in msg:
                continue
            # Таблица уже существует — OK (CREATE IF NOT EXISTS не должна бросать, но на всякий)
            if "already exists" in msg:
                continue
            raise
    conn.commit()


def _apply_daily_metrics_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 009 (ad_daily_metrics) идемпотентно.

    CREATE TABLE / INDEX IF NOT EXISTS безопасны. Если файла нет — пропускаем.
    """
    migration_path = Path(__file__).parent.parent / "migrations" / "009_ad_daily_metrics.sql"
    if not migration_path.exists():
        return
    migration_sql = migration_path.read_text(encoding="utf-8")
    for stmt in migration_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            raise
    conn.commit()


def _apply_brain_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 008 идемпотентно (Creative Brain).

    Добавляет колонки в creative_kb: is_full_cabinet, effective_status,
    outcomes_matched_at, labeled_at, label_source, hook_type_id, angle_id,
    offer_type_id. Создаёт таблицу backfill_state и индексы.
    ALTER TABLE ловим 'duplicate column', CREATE ... IF NOT EXISTS безопасен.
    Если файл миграции ещё не создан — пропускаем.
    """
    migration_path = Path(__file__).parent.parent / "migrations" / "008_creative_brain.sql"
    if not migration_path.exists():
        return

    migration_sql = migration_path.read_text(encoding="utf-8")

    # Разбиваем на отдельные statement'ы по ";"
    for stmt in migration_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            # Колонка уже существует — OK (ALTER TABLE не поддерживает IF NOT EXISTS)
            if "duplicate column" in msg:
                continue
            # Таблица или индекс уже существуют — OK (CREATE IF NOT EXISTS страхует, но на всякий)
            if "already exists" in msg:
                continue
            raise
    conn.commit()


def _apply_null_payments_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 010 идемпотентно (B1: честная семантика оплат).

    UPDATE-миграция: payments/qual_leads/revenue = 0 у несверённых с AMO строк
    (outcomes_matched_at IS NULL) обнуляются в NULL — «нет данных» вместо
    фейкового «точно 0». Повторный запуск не находит строк (WHERE payments = 0 /
    qual_leads = 0 / revenue = 0 больше не совпадает после первого прогона).
    Если файл миграции ещё не создан — пропускаем.
    """
    migration_path = Path(__file__).parent.parent / "migrations" / "010_null_unconfirmed_payments.sql"
    if not migration_path.exists():
        return
    migration_sql = migration_path.read_text(encoding="utf-8")
    for stmt in migration_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg or "no such column" in msg:
                continue
            raise
    conn.commit()


def _apply_hypotheses_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 011 (hypotheses) идемпотентно.

    CREATE TABLE / INDEX IF NOT EXISTS безопасны. Если файла нет — пропускаем.
    """
    migration_path = Path(__file__).parent.parent / "migrations" / "011_hypotheses.sql"
    if not migration_path.exists():
        return
    migration_sql = migration_path.read_text(encoding="utf-8")
    for stmt in migration_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            raise
    conn.commit()


def _apply_cdp_payments_erp_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 012 идемпотентно (Шаг B: реальные платежи ERP из CDP).

    Добавляет колонки payments_erp/revenue_erp_lcy/payments_erp_synced_at в creative_kb.
    ADD COLUMN — ловим 'duplicate column' при повторном запуске. Файла нет → skip.
    """
    migration_path = Path(__file__).parent.parent / "migrations" / "012_cdp_payments_erp.sql"
    if not migration_path.exists():
        return
    migration_sql = migration_path.read_text(encoding="utf-8")
    for stmt in migration_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            raise
    conn.commit()


def _apply_hourly_metrics_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 013 (ad_hourly_metrics) идемпотентно.

    CREATE TABLE / INDEX IF NOT EXISTS безопасны. Если файла нет — пропускаем.
    """
    migration_path = Path(__file__).parent.parent / "migrations" / "013_ad_hourly_metrics.sql"
    if not migration_path.exists():
        return
    migration_sql = migration_path.read_text(encoding="utf-8")
    for stmt in migration_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            raise
    conn.commit()


_EARLY_KILL_MIGRATIONS = (
    "029_early_kill_evaluations.sql",   # журнал оценок (волна 1)
    "030_early_kill_state.sql",         # состояние по объявлению + правило B (волна 2a)
    "031_early_kill_action.sql",        # действие по решению + proposal_id (волна 2b, бой A)
    "032_early_kill_window.sql",        # окно 14 дней в кэше состояния (волна 2c, правило C)
)


def _apply_early_kill_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграции раннего стопа (029, 030) идемпотентно, по порядку.

    services/early_kill.py. CREATE TABLE / INDEX IF NOT EXISTS безопасны,
    повторный ALTER TABLE ADD COLUMN даёт «duplicate column» и пропускается.
    Если файла нет — пропускаем.
    """
    for name in _EARLY_KILL_MIGRATIONS:
        migration_path = Path(__file__).parent.parent / "migrations" / name
        if not migration_path.exists():
            continue
        migration_sql = migration_path.read_text(encoding="utf-8")
        for stmt in migration_sql.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "duplicate column" in msg or "already exists" in msg:
                    continue
                raise
        conn.commit()


def _apply_target_product_norm_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 014 идемпотентно (нормализация старых кодов target_product).

    Только UPDATE-стейтменты — санитайзер уже записанных вручную старых кодов
    (PRODC/PRODD/PRODE → PRODA, START/BASIC → СТАРТ, GENERAL/COMMON → ОБЩАЯ) в канон
    продукта. ОСНОВНОЙ бэкфилл NULL-значений делает backfill_target_product()
    (зависит от Python-классификатора, чистым SQL не делается). Если колонки
    target_product ещё нет — ловим 'no such column' и пропускаем (как в
    _apply_null_payments_migration). Если файла миграции нет — пропускаем.
    """
    migration_path = Path(__file__).parent.parent / "migrations" / "014_backfill_target_product.sql"
    if not migration_path.exists():
        return
    migration_sql = migration_path.read_text(encoding="utf-8")
    for stmt in migration_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg or "no such column" in msg:
                continue
            raise
    conn.commit()


def _apply_payments_7d_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 015 идемпотентно (Wave 3A: честные 7d source-specific оплаты).

    Добавляет source-specific колонки payments_amo_7d/revenue_amo_7d/amo_7d_* и
    payments_erp_7d/revenue_erp_7d/erp_7d_* в creative_kb. Эти колонки НЕ переопределяют
    исторические lifetime/long-window payments/payments_erp — отдельный узкий срез
    ровно за 7 календарных дней (см. заголовок migrations/015_payments_7d.sql).
    ADD COLUMN — ловим 'duplicate column' при повторном запуске. Файла нет → skip.
    """
    migration_path = Path(__file__).parent.parent / "migrations" / "015_payments_7d.sql"
    if not migration_path.exists():
        return
    migration_sql = migration_path.read_text(encoding="utf-8")
    for stmt in migration_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            raise
    conn.commit()


def _apply_replacement_workflow_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 016: workflow замены и cleanup audit.

    DDL использует только CREATE TABLE/INDEX IF NOT EXISTS, поэтому повторный
    запуск безопасен. Если файл миграции ещё не создан — пропускаем.
    """
    migration_path = (
        Path(__file__).parent.parent
        / "migrations"
        / "016_replacement_and_cleanup_audit.sql"
    )
    if not migration_path.exists():
        return
    migration_sql = migration_path.read_text(encoding="utf-8")
    conn.executescript(migration_sql)
    conn.commit()


def _apply_proactive_cleaner_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 017: durable cleaner runs/claims и recovery.

    SQL-файл сам открывает ``BEGIN IMMEDIATE`` и завершает ``COMMIT``. Это
    важно для атомарной замены unique-индекса миграции 016: промежуточного
    состояния без once-only индекса не видно другим SQLite-соединениям.
    Все таблицы/индексы используют ``IF NOT EXISTS``, а старый индекс удаляется
    через ``DROP INDEX IF EXISTS``, поэтому повторное применение безопасно.
    """
    migration_path = (
        Path(__file__).parent.parent
        / "migrations"
        / "017_proactive_cleaner_and_launch_recovery.sql"
    )
    if not migration_path.exists():
        return
    migration_sql = migration_path.read_text(encoding="utf-8")
    conn.executescript(migration_sql)
    conn.commit()


def _apply_meta_lead_semantics_migration(conn: sqlite3.Connection) -> None:
    """Применяет миграцию 018: provenance семантики Meta lead actions."""
    migration_path = (
        Path(__file__).parent.parent
        / "migrations"
        / "018_meta_lead_semantics.sql"
    )
    if not migration_path.exists():
        return
    migration_sql = migration_path.read_text(encoding="utf-8")
    for stmt in migration_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "duplicate column" in str(exc).lower():
                continue
            raise
    conn.commit()


def _apply_launch_checker_migration(conn: sqlite3.Connection) -> None:
    """Совместимый вход в единый runner миграций 019–025."""

    conn.commit()
    database_row = next(
        (
            row
            for row in conn.execute("PRAGMA database_list")
            if str(row[1]) == "main"
        ),
        None,
    )
    db_path = "" if database_row is None else str(database_row[2])
    if not db_path:
        raise RuntimeError(
            "Runtime-миграции требуют файловую SQLite БД, не :memory:"
        )
    apply_runtime_migrations(db_path)
    verify_runtime_schema(db_path)


def _apply_asset_recovery_gateway_migration(conn: sqlite3.Connection) -> None:
    """Совместимый вход 020; фактически проверяет весь runtime-набор."""

    _apply_launch_checker_migration(conn)


def init_kb(db_path: str | None = None) -> str:
    """Создаёт таблицу creative_kb (идемпотентно). Возвращает путь к БД."""
    global DB_PATH

    if db_path is None:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        db_path = str(_DATA_DIR / "decisions.db")

    DB_PATH = db_path

    # Миграция 004: основная схема
    migration_path = Path(__file__).parent.parent / "migrations" / "004_creative_kb.sql"
    ddl = migration_path.read_text(encoding="utf-8")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(ddl)
        conn.commit()
        # Миграция 005: колонки скоринга (идемпотентно)
        _apply_scoring_migration(conn)
        # Миграция 006: колонка image_url (идемпотентно)
        _apply_image_url_migration(conn)
        # Миграция 007: AD Agent V2 (идемпотентно)
        _apply_v2_migration(conn)
        # Миграция 008: Creative Brain — полный кабинет + AMO-исходы + разметка (идемпотентно)
        _apply_brain_migration(conn)
        # Миграция 009: дневные срезы метрик (идемпотентно)
        _apply_daily_metrics_migration(conn)
        # Миграция 010: несверённые payments/qual_leads/revenue → NULL (идемпотентно, B1)
        _apply_null_payments_migration(conn)
        # Миграция 011: журнал гипотез Фазы 4 (идемпотентно)
        _apply_hypotheses_migration(conn)
        # Миграция 012: колонки ERP-оплат (payments_erp/revenue_erp_lcy/synced_at)
        _apply_cdp_payments_erp_migration(conn)
        # Миграция 013: почасовые метрики первых 48ч (датасет для раннего прогноза v3)
        _apply_hourly_metrics_migration(conn)
        # Миграция 029: журнал оценок раннего стопа (services/early_kill.py)
        _apply_early_kill_migration(conn)
        # Миграция 014: нормализация старых кодов target_product под канон продукта
        _apply_target_product_norm_migration(conn)
        # Миграция 015: source-specific оплаты ровно за 7 дней (честное окно, Wave 3A)
        _apply_payments_7d_migration(conn)
        # Миграция 016: durable replacement workflow + append-only cleanup audit
        _apply_replacement_workflow_migration(conn)
        # Миграция 017: proactive dry-run, workflow-only claims и recovery
        _apply_proactive_cleaner_migration(conn)
        # Миграция 018: версия и статус семантики Meta lead actions
        _apply_meta_lead_semantics_migration(conn)
        # Миграция 019: checker authorizations, all-target reservations и audit
        _apply_launch_checker_migration(conn)
    finally:
        conn.close()

    logger.info("creative_kb инициализирована: %s", DB_PATH)
    return DB_PATH


# Алиас для обратной совместимости с импортами из web/app.py и тестами
init_creative_kb = init_kb


def _extract_body_and_image(creative: dict) -> tuple[str, str | None]:
    """Достаёт body и image_url из creative с fallback.

    Приоритет body: creative.body > video_data.message > link_data.message
    Приоритет image: creative.image_url > video_data.image_url > link_data.picture > thumbnail_url
    """
    if not creative:
        return "", None

    body = creative.get("body", "")
    image_url = creative.get("image_url")
    thumbnail_url = creative.get("thumbnail_url")

    spec = creative.get("object_story_spec") or {}
    video_data = spec.get("video_data") or {}
    link_data = spec.get("link_data") or {}

    # body fallback по приоритету
    if not body:
        body = video_data.get("message") or link_data.get("message") or ""

    # image fallback по приоритету
    if not image_url:
        image_url = video_data.get("image_url") or link_data.get("picture") or thumbnail_url

    return body, image_url


def _fetch_creative_fields(ad_ids: list[str]) -> dict[str, dict]:
    """Тянет ad_body + image_url для списка ad_ids через FB API.

    Делает batch-запросы по 50 IDs (ограничение FB API).
    При ошибке батча — пропускает его, логирует и продолжает.

    Returns: {ad_id: {"ad_body": str, "image_url": str | None}}
    """
    from services.fb_token_provider import get_fb_token
    from agent.fb_common import session

    if not ad_ids:
        return {}

    token = get_fb_token()
    result: dict[str, dict] = {}

    # FB API ограничение — batch до 50 IDs за раз
    BATCH = 50
    for i in range(0, len(ad_ids), BATCH):
        batch = ad_ids[i : i + BATCH]
        params = {
            "ids": ",".join(batch),
            "fields": (
                "creative{body,image_url,thumbnail_url,"
                "object_story_spec{video_data{message,image_url},"
                "link_data{message,picture}}}"
            ),
            "access_token": token,
        }
        try:
            r = session.get(
                "https://graph.facebook.com/v21.0/",
                params=params,
                timeout=30,
            )
            if not r.ok:
                logger.warning(
                    "FB API batch creative fetch failed (batch %d): %s",
                    i // BATCH,
                    r.text[:200],
                )
                continue
            data = r.json()
            for ad_id, ad_data in data.items():
                # FB возвращает ключ "error" при общей ошибке — пропускаем
                if ad_id == "error":
                    logger.warning("FB API вернул ошибку в batch: %s", ad_data)
                    continue
                # Отдельный ad может вернуть {"error": ...} внутри dict
                if isinstance(ad_data, dict) and "error" in ad_data:
                    logger.debug("Пропускаем удалённый/недоступный ad_id=%s", ad_id)
                    continue
                body, img = _extract_body_and_image(ad_data.get("creative") or {})
                result[ad_id] = {"ad_body": body or "", "image_url": img}
        except Exception as e:
            logger.warning("FB API batch error (batch %d): %s", i // BATCH, e)

    logger.info(
        "_fetch_creative_fields: запрошено %d, получено %d",
        len(ad_ids),
        len(result),
    )
    return result


def sync_knowledge_base(auto_score: bool = False, enrich_creatives: bool = True) -> dict:
    """Синхронизирует creative_kb из 3 источников через UPSERT.

    Источники:
      - data/learner_results.json  → FB метрики + классификация (основной)
      - data/amo_data.json         → AMO метрики (обогащение по ad_id)
      - data/vision_analysis.json  → Gemini vision tags (обогащение по ad_id)

    После основного UPSERT (если enrich_creatives=True) дозагружает ad_body и image_url
    через FB API для всех записей где они пустые.

    Args:
        auto_score: если True — после синка запускает score_unscored_creatives()
        enrich_creatives: если True — дозагружает ad_body + image_url из FB API

    Returns:
        {"synced": N, "new": M, "updated": K, "sources": {...}, "enriched": E}
        Если auto_score=True: добавляется ключ "scoring": {"scored": N, "errors": M, "skipped": K}
    """
    learner_path = _DATA_DIR / "learner_results.json"
    amo_path = _DATA_DIR / "amo_data.json"
    vision_path = _DATA_DIR / "vision_analysis.json"

    # --- Загрузка learner_results.json (обязательный источник) ---
    if not learner_path.exists():
        logger.warning("learner_results.json не найден: %s", learner_path)
        return {"synced": 0, "new": 0, "updated": 0, "sources": {}}

    try:
        learner_data = json.loads(learner_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Ошибка чтения learner_results.json: %s", exc)
        return {"synced": 0, "new": 0, "updated": 0, "sources": {}}

    creative_table: list[dict] = learner_data.get("creative_table", [])
    if not creative_table:
        logger.warning("creative_table пустой в learner_results.json")
        return {"synced": 0, "new": 0, "updated": 0, "sources": {}}

    # --- Загрузка amo_data.json (необязательный) ---
    amo_by_id: dict[str, dict] = {}
    amo_count = 0
    if amo_path.exists():
        try:
            raw_amo = json.loads(amo_path.read_text(encoding="utf-8"))
            if isinstance(raw_amo, dict):
                amo_by_id = raw_amo
                amo_count = len(amo_by_id)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Ошибка чтения amo_data.json: %s", exc)
    else:
        logger.warning("amo_data.json не найден — AMO метрики будут NULL")

    # --- Загрузка vision_analysis.json (необязательный) ---
    vision_by_id: dict[str, dict] = {}
    vision_count = 0
    if vision_path.exists():
        try:
            raw_vision = json.loads(vision_path.read_text(encoding="utf-8"))
            if isinstance(raw_vision, dict):
                vision_by_id = raw_vision
                vision_count = len(vision_by_id)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Ошибка чтения vision_analysis.json: %s", exc)
    else:
        logger.warning("vision_analysis.json не найден — vision_tags будут NULL")

    # --- UPSERT в creative_kb ---
    conn = _get_connection()
    new_count = 0
    updated_count = 0

    try:
        for row in creative_table:
            ad_id = str(row.get("ad_id", "")).strip()
            if not ad_id:
                continue

            # Проверяем: новая запись или обновление
            existing = conn.execute(
                "SELECT days_running, created_at FROM creative_kb WHERE ad_id = ?", (ad_id,)
            ).fetchone()

            if existing:
                updated_count += 1
            else:
                new_count += 1

            # AMO метрики: сначала из creative_table (уже содержат AMO поля),
            # потом обогащаем из amo_data.json если там другие данные
            amo_row = amo_by_id.get(ad_id, {})

            # B1: «нет данных» ≠ «0». Для несверённых записей payments/revenue/qual_leads
            # остаются NULL (default не подставляем) — реальные значения приходят только
            # из amo_data.json (отматченные с AMO лиды). ВАЖНО: эти значения используются
            # ТОЛЬКО в INSERT-части (новая строка) — при UPDATE (ON CONFLICT) AMO-поля
            # существующей записи НЕ трогаем (их пишет только attach_amo_outcomes), см. ниже.
            qual_pct = _coalesce(amo_row.get("qual_pct"), row.get("qual_pct"))
            romi = _coalesce(amo_row.get("romi"), row.get("romi"))
            payments = _coalesce(amo_row.get("payments"), row.get("payments"))
            cpql = _coalesce(amo_row.get("cpql"), row.get("cpql"))
            revenue = _coalesce(amo_row.get("revenue"), row.get("revenue"))
            qual_leads = _coalesce(amo_row.get("qual_leads"), row.get("qual_leads"))

            # Vision tags: сериализуем в JSON строку
            vision_dict = vision_by_id.get(ad_id)
            vision_tags_str = json.dumps(vision_dict, ensure_ascii=False) if vision_dict else None

            # days_running: вычисляем из created_at если есть, иначе берём существующее из БД.
            # row.get("days_running") из learner_results.json обычно равен 0 или отсутствует —
            # без пересчёта это затёрло бы корректное значение, записанное creative_backfill
            # (C1: days_running всё равно в SET ON CONFLICT, т.к. sync его реально обновляет —
            # но берём уже пересчитанное значение, а не сырой 0 из learner). Сначала вычисляем
            # из created_at, если не вышло — сохраняем существующее из БД.
            _days_running_in_row = row.get("days_running") or 0
            if _days_running_in_row > 0:
                # Источник (learner/backfill) уже посчитал — доверяем
                _sync_days_running = _days_running_in_row
            else:
                # Пытаемся пересчитать из created_at
                _created_at_str = row.get("created_at") or (existing[1] if existing else None)
                _sync_days_running = 0
                if _created_at_str:
                    try:
                        import re as _re
                        from datetime import datetime as _dt, timezone as _tz
                        _fixed = _re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', _created_at_str)
                        _created_dt = _dt.fromisoformat(_fixed)
                        _sync_days_running = (_dt.now(_tz.utc) - _created_dt).days
                    except Exception:
                        # Не смогли распарсить — берём из существующей записи в БД
                        _sync_days_running = int(existing[0] or 0) if existing else 0

            # C1: INSERT ... ON CONFLICT DO UPDATE вместо INSERT OR REPLACE.
            # REPLACE полностью пересоздавал строку и стирал ~28 колонок, которые
            # sync не приносит: разметку Gemini (labeled_at/hook_type_id/angle_id/
            # offer_type_id/label_source), скоринг, AMO-исходы (qual_pct/romi/payments/
            # cpql/revenue/qual_leads/outcomes_matched_at), business_class, created_at,
            # ad_body/image_url (дозагружаются отдельным enrichment-шагом ниже).
            # ON CONFLICT обновляет ТОЛЬКО поля, которые реально приходят из FB-источника
            # (learner_results.json): метрики + creative_class + vision_tags (COALESCE —
            # не затираем существующий vision, если синк его не принёс) + days_running.
            # AMO-поля/разметка/created_at/outcomes_matched_at в SET сознательно ОТСУТСТВУЮТ —
            # их пишут только attach_amo_outcomes / score_single_creative / creative_backfill.
            # Для НОВОЙ строки (INSERT-часть) AMO-поля из values нужны — если amo_data.json
            # уже содержит отматченные значения на момент первой вставки, они не теряются.
            conn.execute(
                """
                INSERT INTO creative_kb (
                    ad_id, ad_name, city, adset_type, status,
                    effective_status,
                    spend, leads, cpl, ctr, cpm,
                    impressions, clicks, frequency,
                    hook_rate, hold_rate,
                    video_views_3s, thruplay,
                    video_p25, video_p50, video_p75, video_p100,
                    qual_pct, romi, payments, cpql, revenue, qual_leads,
                    creative_class, business_class,
                    vision_tags,
                    days_running,
                    synced_at
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?,
                    ?,
                    ?,
                    datetime('now')
                )
                ON CONFLICT(ad_id) DO UPDATE SET
                    ad_name        = excluded.ad_name,
                    city           = excluded.city,
                    adset_type     = excluded.adset_type,
                    status         = excluded.status,
                    -- effective_status: без него новая cabinet_b-строка невидима автопилоту
                    -- (_fetch_ads_from_local_db фильтрует по этой колонке); пустое
                    -- значение из learner не затирает свежее от creative_backfill.
                    effective_status = COALESCE(
                        NULLIF(excluded.effective_status, ''), creative_kb.effective_status
                    ),
                    spend          = excluded.spend,
                    leads          = excluded.leads,
                    cpl            = excluded.cpl,
                    ctr            = excluded.ctr,
                    cpm            = excluded.cpm,
                    impressions    = excluded.impressions,
                    clicks         = excluded.clicks,
                    frequency      = excluded.frequency,
                    hook_rate      = excluded.hook_rate,
                    hold_rate      = excluded.hold_rate,
                    video_views_3s = excluded.video_views_3s,
                    thruplay       = excluded.thruplay,
                    video_p25      = excluded.video_p25,
                    video_p50      = excluded.video_p50,
                    video_p75      = excluded.video_p75,
                    video_p100     = excluded.video_p100,
                    creative_class = excluded.creative_class,
                    vision_tags    = COALESCE(excluded.vision_tags, creative_kb.vision_tags),
                    days_running   = excluded.days_running,
                    synced_at      = excluded.synced_at
                """,
                (
                    ad_id,
                    row.get("ad_name", ""),
                    row.get("city", ""),
                    row.get("adset_type", ""),
                    row.get("status", ""),
                    row.get("effective_status", ""),
                    # FB метрики
                    row.get("spend", 0),
                    row.get("leads", 0),
                    row.get("cpl", 0),
                    row.get("ctr", 0),
                    row.get("cpm", 0),
                    # impressions и clicks могут отсутствовать в learner_results
                    row.get("impressions", 0),
                    row.get("clicks", 0),
                    row.get("frequency", 0),
                    row.get("hook_rate", 0),
                    row.get("hold_rate", 0),
                    row.get("video_views_3s", 0),
                    row.get("thruplay", 0),
                    row.get("video_p25", 0),
                    row.get("video_p50", 0),
                    row.get("video_p75", 0),
                    row.get("video_p100", 0),
                    # AMO метрики (используются ТОЛЬКО при INSERT новой строки — см. комментарий выше)
                    qual_pct,
                    romi,
                    payments,
                    cpql,
                    revenue,
                    qual_leads,
                    # Классификация (business_class — только для INSERT, при UPDATE не трогаем)
                    row.get("creative_class", ""),
                    row.get("business_class", ""),
                    # Vision
                    vision_tags_str,
                    # Мета (days_running вычислен выше из created_at или взят из БД)
                    _sync_days_running,
                ),
            )

        conn.commit()
    finally:
        conn.close()

    synced_total = new_count + updated_count
    logger.info(
        "sync_knowledge_base завершён: всего=%d, новых=%d, обновлено=%d",
        synced_total, new_count, updated_count,
    )

    # --- Обогащение: дозагружаем ad_body и image_url из FB API ---
    enriched_count = 0
    if enrich_creatives:
        enrich_conn = _get_connection()
        try:
            # Берём ad_id где нет body или image_url
            empty_rows = enrich_conn.execute(
                "SELECT ad_id FROM creative_kb WHERE (ad_body IS NULL OR ad_body = '') OR image_url IS NULL"
            ).fetchall()
            empty_ids = [r["ad_id"] for r in empty_rows]

            if empty_ids:
                logger.info("Enriching %d креативов из FB API (ad_body + image_url)", len(empty_ids))
                fb_data = _fetch_creative_fields(empty_ids)

                for ad_id, fields in fb_data.items():
                    enrich_conn.execute(
                        "UPDATE creative_kb SET ad_body = ?, image_url = ? WHERE ad_id = ?",
                        (fields["ad_body"], fields["image_url"], ad_id),
                    )
                    enriched_count += 1

                enrich_conn.commit()
                logger.info("Enrichment завершён: обновлено %d записей", enriched_count)
            else:
                logger.info("Enrichment: все креативы уже имеют ad_body и image_url")
        finally:
            enrich_conn.close()

    result: dict = {
        "synced": synced_total,
        "new": new_count,
        "updated": updated_count,
        "sources": {
            "learner": len(creative_table),
            "amo": amo_count,
            "vision": vision_count,
        },
        "enriched": enriched_count,
    }

    # Автоскоринг после синка
    if auto_score:
        logger.info("auto_score=True, запускаем score_unscored_creatives()")
        scoring = score_unscored_creatives(limit=500, parallel=5)
        result["scoring"] = scoring

    return result


# --- Скоринг креативов ---

# Пул потоков для параллельного скоринга (переиспользуется между вызовами)
_scoring_pool = ThreadPoolExecutor(max_workers=5)


def score_single_creative(ad: dict, force: bool = False) -> dict:
    """Скорит один креатив через Gemini (visual + text + customer) и сохраняет в creative_kb.

    Args:
        ad: запись из creative_kb (dict с ad_id, ad_body, ad_headline, vision_tags, etc.)
        force: если True — пересчитывать даже если scored_at уже есть

    Returns:
        результат compute_total_score + ad_id. Никогда не бросает исключение.
    """
    # Импорты здесь чтобы избежать циклических зависимостей при старте модуля
    from services.visual_scoring import score_visual_from_kb_record
    from services.text_scoring import score_text, check_customer_first
    from services.scoring_rubric import compute_total_score

    ad_id = ad.get("ad_id", "")

    # Если уже скорен и не форс — возвращаем кешированный результат
    if not force and ad.get("scored_at"):
        logger.debug("ad_id=%s: скор уже есть (scored_at=%s), пропускаем", ad_id, ad.get("scored_at"))
        return {
            "ad_id": ad_id,
            "total_score": ad.get("total_score"),
            "grade": ad.get("score_grade"),
            "visual_score": ad.get("visual_score"),
            "text_score": ad.get("text_score"),
            "customer_score": ad.get("customer_score"),
            "skipped": True,
        }

    try:
        # Визуальный скоринг (через thumbnail_url или кеш vision_tags)
        visual = score_visual_from_kb_record(ad)

        # Текстовый скоринг
        ad_body = ad.get("ad_body") or ""
        ad_headline = ad.get("ad_headline") or ""
        text = score_text(ad_body, ad_headline)

        # Customer-First проверка
        customer = check_customer_first(ad_body, ad_headline)

        # Итоговый скор по рубрике
        rubric = compute_total_score(visual, text, customer)
        rubric["ad_id"] = ad_id

        # Сохраняем в БД
        conn = _get_connection()
        try:
            conn.execute(
                """
                UPDATE creative_kb
                SET total_score = ?,
                    visual_score = ?,
                    text_score = ?,
                    customer_score = ?,
                    score_grade = ?,
                    score_breakdown = ?,
                    scored_at = datetime('now')
                WHERE ad_id = ?
                """,
                (
                    rubric["total_score"],
                    rubric["visual_score"],
                    rubric["text_score"],
                    rubric["customer_score"],
                    rubric["grade"],
                    json.dumps(rubric["breakdown"], ensure_ascii=False),
                    ad_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        logger.info(
            "ad_id=%s: скор %d (%s) — visual=%d text=%d customer=%d bonus=%d",
            ad_id,
            rubric["total_score"],
            rubric["grade"],
            rubric["visual_score"],
            rubric["text_score"],
            rubric["customer_score"],
            rubric["bonus_score"],
        )
        return rubric

    except Exception as exc:
        logger.warning("Скоринг %s (%s) не удался: %s", ad_id, ad.get("ad_name", ""), exc)
        # Записываем ошибку в breakdown, total_score=0, grade='poor'
        error_breakdown = json.dumps({"_error": str(exc)}, ensure_ascii=False)
        try:
            conn = _get_connection()
            try:
                conn.execute(
                    """
                    UPDATE creative_kb
                    SET total_score = 0,
                        visual_score = 0,
                        text_score = 0,
                        customer_score = 0,
                        score_grade = 'poor',
                        score_breakdown = ?,
                        scored_at = datetime('now')
                    WHERE ad_id = ?
                    """,
                    (error_breakdown, ad_id),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as db_exc:
            logger.error("Не удалось сохранить ошибку скоринга в БД для %s: %s", ad_id, db_exc)

        return {
            "ad_id": ad_id,
            "total_score": 0,
            "grade": "poor",
            "visual_score": 0,
            "text_score": 0,
            "customer_score": 0,
            "bonus_score": 0,
            "_error": str(exc),
        }


def score_unscored_creatives(limit: int = 50, parallel: int = 5) -> dict:
    """Скорит все ещё не оценённые или устаревшие креативы из KB.

    Берёт записи где scored_at IS NULL или synced_at > scored_at.
    Запускает score_single_creative в ThreadPoolExecutor.

    Args:
        limit: максимальное количество креативов для скоринга
        parallel: количество параллельных потоков (max_workers)

    Returns:
        {"scored": N, "errors": M, "skipped": K}
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM creative_kb
            WHERE scored_at IS NULL OR synced_at > scored_at
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        ads = [dict(r) for r in rows]
    finally:
        conn.close()

    if not ads:
        logger.info("score_unscored_creatives: нет креативов для скоринга")
        return {"scored": 0, "errors": 0, "skipped": 0}

    logger.info("score_unscored_creatives: запускаем скоринг %d креативов (parallel=%d)", len(ads), parallel)

    scored_count = 0
    error_count = 0
    skipped_count = 0

    # Используем локальный пул с нужным числом воркеров
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(score_single_creative, ad): ad for ad in ads}

        for future in futures:
            ad = futures[future]
            try:
                result = future.result()
                if result.get("skipped"):
                    skipped_count += 1
                elif result.get("_error"):
                    error_count += 1
                else:
                    scored_count += 1
            except Exception as exc:
                # Этого не должно случиться — score_single_creative не бросает
                logger.error("Неожиданная ошибка в score_single_creative(%s): %s", ad.get("ad_id"), exc)
                error_count += 1

    logger.info(
        "score_unscored_creatives завершён: scored=%d, errors=%d, skipped=%d",
        scored_count, error_count, skipped_count,
    )
    return {"scored": scored_count, "errors": error_count, "skipped": skipped_count}


# Разрешённые поля для сортировки — защита от SQL injection
ALLOWED_SORT_FIELDS = {"spend", "total_score", "cpl", "romi", "hook_rate", "leads", "ctr", "scored_at"}
ALLOWED_SORT_ORDERS = {"asc", "desc"}


def get_kb_creatives(
    creative_class: str | None = None,
    city: str | None = None,
    grade: str | None = None,
    score_min: int | None = None,
    score_max: int | None = None,
    sort_by: str = "spend",
    sort_order: str = "desc",
    limit: int = 100,
) -> list[dict]:
    """Возвращает креативы из KB с опциональной фильтрацией и сортировкой.

    Args:
        creative_class: фильтр по классу (Winner/Clickbait/Hidden Gem/Dead)
        city: фильтр по городу
        grade: фильтр по грейду скора (excellent/good/mediocre/poor)
        score_min: минимальный total_score (включительно)
        score_max: максимальный total_score (включительно)
        sort_by: поле сортировки из ALLOWED_SORT_FIELDS, дефолт "spend"
        sort_order: направление сортировки "asc"/"desc", дефолт "desc"
        limit: максимум записей (принудительно не больше 500)

    Returns:
        Список словарей с данными креативов.
    """
    limit = min(limit, 500)

    # Защита от SQL injection через whitelist
    if sort_by not in ALLOWED_SORT_FIELDS:
        sort_by = "spend"
    if sort_order not in ALLOWED_SORT_ORDERS:
        sort_order = "desc"

    conditions: list[str] = []
    params: list = []

    if creative_class:
        conditions.append("creative_class = ?")
        params.append(creative_class)
    if city:
        conditions.append("city = ?")
        params.append(city)
    if grade:
        conditions.append("score_grade = ?")
        params.append(grade)
    if score_min is not None:
        conditions.append("total_score >= ?")
        params.append(score_min)
    if score_max is not None:
        conditions.append("total_score <= ?")
        params.append(score_max)

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    # NULLS LAST: строка "column IS NULL" возвращает 0 или 1,
    # ORDER BY ... IS NULL ASC ставит NULL'ы в конец при любом sort_order
    order_clause = f"{sort_by} IS NULL, {sort_by} {sort_order}"

    params.append(limit)

    conn = _get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM creative_kb {where} ORDER BY {order_clause} LIMIT ?",
            params,
        ).fetchall()

        result = []
        for r in rows:
            item = dict(r)
            # Десериализуем vision_tags из JSON строки
            if item.get("vision_tags"):
                try:
                    item["vision_tags"] = json.loads(item["vision_tags"])
                except (json.JSONDecodeError, ValueError):
                    item["vision_tags"] = None
            result.append(item)

        return result
    finally:
        conn.close()


# --- Диагностика ---

# Порядок сортировки уровней: сначала самые критичные проблемы
LEVEL_ORDER = ["fatigue", "hook", "hold", "ctr", "cvr", "insufficient_data", "healthy"]


def diagnose_creative(ad: dict) -> dict:
    """Каскадная диагностика рекламного объявления.

    Первый matching уровень побеждает — порядок проверок важен.

    Args:
        ad: словарь с метриками объявления (из creative_kb или learner_results)

    Returns:
        {
            "level": str,         # hook | hold | ctr | cvr | fatigue | healthy | insufficient_data
            "message": str,       # человеческое описание с цифрами
            "recommendation": str, # конкретное действие
            "metrics": dict       # подмножество метрик
        }
    """
    hook_rate = ad.get("hook_rate", 0) or 0
    hold_rate = ad.get("hold_rate", 0) or 0
    ctr = ad.get("ctr", 0) or 0
    qual_pct = ad.get("qual_pct")  # может быть None
    frequency = ad.get("frequency", 0) or 0
    impressions = ad.get("impressions", 0) or 0
    spend = float(ad.get("spend", 0) or 0)

    # Подмножество метрик для ответа
    metrics = {
        "hook_rate": hook_rate,
        "hold_rate": hold_rate,
        "ctr": ctr,
        "qual_pct": qual_pct,
        "frequency": frequency,
        "impressions": impressions,
        "spend": spend,
        "leads": ad.get("leads", 0) or 0,
        "cpl": ad.get("cpl", 0) or 0,
    }

    # 0. Минимум данных для диагностики.
    # Проверяем по spend (>= $5) — impressions может отсутствовать в JSON-источниках,
    # spend — самый надёжный сигнал что реклама реально крутилась.
    # Запасной вариант: если spend пуст, но impressions >= 500 — тоже считаем достаточно.
    if spend < 5 and impressions < 500:
        return {
            "level": "insufficient_data",
            "message": f"Недостаточно данных (расход ${spend:.2f}, показов {impressions})",
            "recommendation": "Дай рекламе открутить хотя бы $15 — сейчас цифры случайны и решение преждевременно.",
            "metrics": metrics,
        }

    # 1. HOOK: первый кадр не цепляет
    if hook_rate < 25:
        return {
            "level": "hook",
            "message": f"Слабый хук ({hook_rate}% < 25%) — первый кадр не цепляет",
            "recommendation": (
                "Тестируй новый первый кадр: лицо человека крупным планом + эмоция в первую секунду, "
                "или провокационный вопрос (например: лицо эксперта, вопрос клиенту в первую секунду)."
            ),
            "metrics": metrics,
        }

    # 2. HOLD: хук работает, но body не держит внимание
    if hold_rate < 40:
        return {
            "level": "hold",
            "message": f"Хук цепляет ({hook_rate}%), но body не держит ({hold_rate}% < 40%)",
            "recommendation": (
                "Ускорь темп: смена кадра каждые 2-3 сек, добавь субтитры, сократи до 15-20 сек. "
                "Перенеси главный бенефит в первую треть видео."
            ),
            "metrics": metrics,
        }

    # 3. CTR: смотрят, но не кликают
    if ctr < 1.0:
        return {
            "level": "ctr",
            "message": f"Держит внимание (Hook {hook_rate}%, Hold {hold_rate}%), но не кликают (CTR {ctr}% < 1%)",
            "recommendation": (
                "Проблема в CTA или оффере. Конкретизируй: 'Бесплатная консультация' вместо 'Узнать больше', "
                "добавь срочность ('до 30 апреля'), сделай ярче CTA-кнопку."
            ),
            "metrics": metrics,
        }

    # 4. CVR / Квалификация: кликают, но плохо квалифицируются
    if qual_pct is not None and qual_pct < 15:
        return {
            "level": "cvr",
            "message": f"Кликают, но квалифицируются плохо (квал {qual_pct}% < 15%)",
            "recommendation": (
                "Кликают не та аудитория или лид-форма привлекает 'халявщиков'. "
                "Проверь: соответствует ли таргетинг офферу, добавь квалифицирующий вопрос в форму, "
                "убери слово 'бесплатно' если используешь."
            ),
            "metrics": metrics,
        }

    # 5. FATIGUE: высокая частота + падающий CTR
    if frequency > 3 and ctr < 1.5:
        return {
            "level": "fatigue",
            "message": f"Креатив выгорел (частота {frequency:.1f}, CTR упал до {ctr}%)",
            "recommendation": (
                "Ротируй: создай 2-3 вариации с тем же месседжем но другим визуалом. "
                "Сохрани текст, смени видео/картинку."
            ),
            "metrics": metrics,
        }

    # Здоровый креатив
    return {
        "level": "healthy",
        "message": f"Креатив работает хорошо (Hook {hook_rate}%, Hold {hold_rate}%, CTR {ctr}%)",
        "recommendation": (
            "Масштабируй: увеличь бюджет на 20-30%, "
            "создай Lookalike на основе конверсий этого объявления."
        ),
        "metrics": metrics,
    }


def diagnose_all(only_active: bool = True) -> dict:
    """Диагностика всех объявлений из Knowledge Base.

    Читает записи из creative_kb, запускает каскадную диагностику,
    сохраняет результат обратно в БД (diagnosis_level, diagnosis_message, diagnosed_at).

    Args:
        only_active: если True — диагностируем только ACTIVE объявления

    Returns:
        {
            "total": int,
            "by_level": {"hook": 5, "hold": 3, ..., "healthy": 10},
            "diagnoses": [
                {"ad_id": ..., "ad_name": ..., "level": ..., "message": ...,
                 "recommendation": ..., "metrics": {...}},
                ...
            ]
        }
    """
    from datetime import datetime

    conn = _get_connection()
    try:
        # Выбираем объявления (с фильтром по статусу или все)
        if only_active:
            rows = conn.execute(
                "SELECT * FROM creative_kb WHERE status = 'ACTIVE'"
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM creative_kb").fetchall()

        diagnoses: list[dict] = []
        now_iso = datetime.now().isoformat()

        for row in rows:
            ad = dict(row)
            # Десериализуем vision_tags если есть (не нужны для диагностики, но сохраняем структуру)
            diagnosis = diagnose_creative(ad)

            entry = {
                "ad_id": ad["ad_id"],
                "ad_name": ad.get("ad_name", ""),
                "level": diagnosis["level"],
                "message": diagnosis["message"],
                "recommendation": diagnosis["recommendation"],
                "metrics": diagnosis["metrics"],
            }
            diagnoses.append(entry)

            # UPDATE: сохраняем диагноз в БД
            conn.execute(
                """
                UPDATE creative_kb
                SET diagnosis_level = ?,
                    diagnosis_message = ?,
                    diagnosed_at = ?
                WHERE ad_id = ?
                """,
                (diagnosis["level"], diagnosis["message"], now_iso, ad["ad_id"]),
            )

        conn.commit()

    finally:
        conn.close()

    # Сортировка по приоритету: fatigue и hook выходят первыми
    level_priority = {level: idx for idx, level in enumerate(LEVEL_ORDER)}
    diagnoses.sort(key=lambda d: level_priority.get(d["level"], 99))

    # Подсчёт статистики по уровням
    by_level: dict[str, int] = {}
    for d in diagnoses:
        lv = d["level"]
        by_level[lv] = by_level.get(lv, 0) + 1

    logger.info(
        "diagnose_all завершён: всего=%d, by_level=%s", len(diagnoses), by_level
    )

    return {
        "total": len(diagnoses),
        "by_level": by_level,
        "diagnoses": diagnoses,
    }


# --- Вспомогательные функции ---

def _coalesce(*values, default=None):
    """Возвращает первое не-None значение из списка, иначе default."""
    for v in values:
        if v is not None:
            return v
    return default


# --- Похожие winners через cosine similarity по vision_tags ---

# Признаки для бинарного вектора: (ключ в vision_tags, значение)
VISION_FEATURES: list[tuple[str, object]] = [
    ("first_frame_type", "person_talking"),
    ("first_frame_type", "text_screen"),
    ("first_frame_type", "product"),
    ("first_frame_type", "lifestyle"),
    ("has_person", True),
    ("has_subtitles", True),
    ("emotion", "positive"),
    ("emotion", "energetic"),
    ("emotion", "neutral"),
]


def _tags_to_vector(tags: dict | None) -> list[float]:
    """Бинарный вектор по VISION_FEATURES. Если tags=None — нулевой вектор."""
    if not tags:
        return [0.0] * len(VISION_FEATURES)
    vector = []
    for key, expected_value in VISION_FEATURES:
        actual = tags.get(key)
        vector.append(1.0 if actual == expected_value else 0.0)
    return vector


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Cosine similarity без numpy. Для нулевых векторов возвращает 0."""
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = sum(a * a for a in v1) ** 0.5
    norm2 = sum(b * b for b in v2) ** 0.5
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


def _describe_difference(target_tags: dict | None, winner_tags: dict | None) -> str:
    """Что есть у winner и нет у target. Используется для объяснения разницы.

    Если у обоих vision_tags=None — возвращает 'Нет vision-данных для сравнения'.
    """
    if not winner_tags:
        return "Нет vision-данных для сравнения"

    # Человекочитаемые названия признаков
    feature_labels = {
        ("first_frame_type", "person_talking"): "лицо спикера в первом кадре",
        ("first_frame_type", "text_screen"): "текст/надпись в первом кадре",
        ("first_frame_type", "product"): "продукт в первом кадре",
        ("first_frame_type", "lifestyle"): "lifestyle-кадр в начале",
        ("has_person", True): "человек в кадре",
        ("has_subtitles", True): "субтитры",
        ("emotion", "positive"): "позитивная эмоция",
        ("emotion", "energetic"): "энергетика/динамика",
        ("emotion", "neutral"): "нейтральная подача",
    }

    winner_has: list[str] = []
    for key, val in VISION_FEATURES:
        if winner_tags.get(key) == val:
            # У target этого нет — значит это отличие
            if not target_tags or target_tags.get(key) != val:
                label = feature_labels.get((key, val))
                if label:
                    winner_has.append(label)

    if not winner_has:
        return "Похожий визуальный стиль"

    return "У winner: " + " + ".join(winner_has)


def get_few_shot_examples(
    city: str | None = None,
    product: str | None = None,
    language: str | None = None,
    winner_limit: int = 5,
    loser_limit: int = 3,
) -> tuple[list[dict], list[dict]]:
    """Подтягивает few-shot примеры из creative_kb для копирайтера.

    Winners: top-K по spend DESC, creative_class='Winner', ad_body непустой.
    Losers: top-N по CPL DESC, creative_class IN ('Dead', 'Clickbait'), ad_body непустой.

    Фильтры city, product (target_product), language (primary_language) — опциональные.
    Если после фильтрации < 2 winners — убираем фильтры и берём без них.

    Returns:
        (winners: list[dict], losers: list[dict])
        Каждый dict содержит: ad_id, ad_name, ad_body, city, hook_rate, hold_rate,
                              cpl, spend, leads, creative_class, hook_type, angle
    """
    # Поля которые возвращаем — достаточно для few-shot контекста
    SELECT_FIELDS = (
        "ad_id, ad_name, ad_body, city, hook_rate, hold_rate, "
        "cpl, spend, leads, creative_class, hook_type, angle"
    )

    def _fetch_winners(conn: sqlite3.Connection, use_filters: bool) -> list[dict]:
        """Выбирает winners с опциональными фильтрами."""
        conditions = [
            "creative_class = 'Winner'",
            "ad_body IS NOT NULL",
            "ad_body != ''",
        ]
        params: list = []

        if use_filters:
            if city:
                conditions.append("city = ?")
                params.append(city)
            if product:
                conditions.append("target_product = ?")
                params.append(product)
            if language:
                conditions.append("primary_language = ?")
                params.append(language)

        where = "WHERE " + " AND ".join(conditions)
        params.append(winner_limit)

        rows = conn.execute(
            f"SELECT {SELECT_FIELDS} FROM creative_kb {where} ORDER BY spend DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def _fetch_losers(conn: sqlite3.Connection) -> list[dict]:
        """Выбирает losers (Dead/Clickbait) с наибольшим CPL."""
        rows = conn.execute(
            f"""
            SELECT {SELECT_FIELDS} FROM creative_kb
            WHERE creative_class IN ('Dead', 'Clickbait')
              AND ad_body IS NOT NULL
              AND ad_body != ''
            ORDER BY cpl DESC
            LIMIT ?
            """,
            (loser_limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    conn = _get_connection()
    try:
        # Сначала пробуем с фильтрами
        winners = _fetch_winners(conn, use_filters=True)

        # Fallback: если < 2 winners после фильтрации — берём без фильтров
        if len(winners) < 2:
            logger.info(
                "get_few_shot_examples: после фильтров city=%s product=%s lang=%s только %d winners, "
                "убираем фильтры",
                city, product, language, len(winners),
            )
            winners = _fetch_winners(conn, use_filters=False)

        losers = _fetch_losers(conn)
    finally:
        conn.close()

    logger.info(
        "get_few_shot_examples: winners=%d, losers=%d (city=%s, product=%s, lang=%s)",
        len(winners), len(losers), city, product, language,
    )
    return winners, losers


def find_similar_winners(ad_id: str, top_n: int = 3) -> list[dict]:
    """Находит топ-N winners похожих на данный ad по vision_tags + city + adset_type.

    Алгоритм:
    1. Загружает целевой ad из creative_kb (если нет → ValueError)
    2. Загружает всех winners (creative_class='Winner') из KB
    3. Приоритет группировки: same_city > same_adset_type > any winner
    4. Если у целевого и winner есть vision_tags — считаем cosine similarity
    5. Если vision_tags нет — fallback: ранжируем winners по spend
    6. Сортируем: similarity DESC → spend DESC внутри группы
    7. Возвращаем топ-N

    Returns: список dict, каждый:
        {
            "ad_id": str,
            "ad_name": str,
            "city": str,
            "adset_type": str,
            "creative_class": str,
            "similarity": float | None,  # 0..1, None если нет vision_tags
            "spend": float,
            "cpl": float,
            "romi": float | None,
            "what_differs": str,  # что у winner есть, чего нет у target
        }
    """
    conn = _get_connection()
    try:
        # 1. Загружаем целевой ad
        row = conn.execute(
            "SELECT * FROM creative_kb WHERE ad_id = ?", (ad_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Объявление не найдено в KB: {ad_id}")
        target = dict(row)

        # 2. Загружаем всех winners
        winner_rows = conn.execute(
            "SELECT * FROM creative_kb WHERE creative_class = 'Winner'"
        ).fetchall()
    finally:
        conn.close()

    if not winner_rows:
        return []

    # Десериализуем vision_tags целевого ad
    target_tags: dict | None = None
    if target.get("vision_tags"):
        try:
            target_tags = json.loads(target["vision_tags"])
        except (json.JSONDecodeError, ValueError):
            target_tags = None

    target_city = target.get("city", "")
    target_adset_type = target.get("adset_type", "")
    target_vector = _tags_to_vector(target_tags)
    # Проверяем: все ли компоненты целевого вектора нулевые (нет vision_tags)
    has_target_vision = any(v != 0.0 for v in target_vector)

    # 3. Строим список winners с метриками схожести
    scored: list[dict] = []
    for wrow in winner_rows:
        winner = dict(wrow)
        w_id = winner["ad_id"]

        # Пропускаем сам целевой ad если он случайно winner
        if w_id == ad_id:
            continue

        # Десериализуем vision_tags winner'а
        w_tags: dict | None = None
        if winner.get("vision_tags"):
            try:
                w_tags = json.loads(winner["vision_tags"])
            except (json.JSONDecodeError, ValueError):
                w_tags = None

        # Считаем similarity только если у обоих есть vision_tags
        similarity: float | None = None
        if has_target_vision and w_tags:
            w_vector = _tags_to_vector(w_tags)
            similarity = round(_cosine_similarity(target_vector, w_vector), 4)

        # Приоритет группировки: 0 = same city, 1 = same type, 2 = any
        w_city = winner.get("city", "")
        w_adset_type = winner.get("adset_type", "")
        if w_city == target_city:
            group_priority = 0
        elif w_adset_type == target_adset_type:
            group_priority = 1
        else:
            group_priority = 2

        spend = float(winner.get("spend") or 0)

        scored.append({
            "ad_id": w_id,
            "ad_name": winner.get("ad_name", ""),
            "city": w_city,
            "adset_type": w_adset_type,
            "creative_class": winner.get("creative_class", "Winner"),
            "similarity": similarity,
            "spend": spend,
            "cpl": float(winner.get("cpl") or 0),
            "romi": winner.get("romi"),
            "what_differs": _describe_difference(target_tags, w_tags),
            # служебные поля для сортировки — удалим после
            "_group": group_priority,
        })

    # 4. Сортировка:
    #    - сначала по группе (same_city > same_type > any)
    #    - внутри группы: если есть similarity — по similarity DESC; иначе по spend DESC
    def _sort_key(item: dict) -> tuple:
        sim = item["similarity"]
        # Для сортировки similarity: None → -1 (хуже любого реального)
        sim_sort = sim if sim is not None else -1.0
        return (item["_group"], -sim_sort, -item["spend"])

    scored.sort(key=_sort_key)

    # Убираем служебное поле и берём top_n
    result = []
    for item in scored[:top_n]:
        item.pop("_group", None)
        result.append(item)

    return result


# "Живая" когорта для LLM-добора — только объявления, которые реально крутятся
# или недавно крутились (status — конфигурируемый владельцем статус,
# effective_status — вычисляемый FB статус; то же поле status использует
# services/ad_renamer.py::plan_renames и services/product_report.py::
# build_product_breakdown). Архив/DELETED (тысячи строк накопленной истории
# аккаунта) LLM НЕ добирает — несоразмерно дорого для мёртвых объявлений,
# отчёты по ним не строятся.
_LLM_LIVE_STATUSES = ("ACTIVE", "PAUSED")


def _is_llm_live_cohort(status: str | None, effective_status: str | None) -> bool:
    """Живая когорта для LLM-добора: сконфигурированный status ACTIVE/PAUSED
    ИЛИ вычисляемый FB effective_status == ACTIVE (ловит случай, когда status
    в KB устарел/пуст, но объявление по факту активно)."""
    return (status or "") in _LLM_LIVE_STATUSES or (effective_status or "") == "ACTIVE"


def backfill_target_product() -> dict:
    """Двухступенчатый бэкфилл creative_kb.target_product для строк с NULL/пустым значением.

    Этап 1 (keyword, быстрый и бесплатный): classify_product(ad_name, ad_body).
    Применяется ко ВСЕМ строкам с NULL/'' target_product (весь архив — дёшево,
    без сети). Если результат НЕ "ОБЩАЯ" — очевидный случай, пишем канон без LLM.

    Этап 2 (LLM-добор, ПЛАТНЫЙ вызов Anthropic): только для строк, где этап 1
    дал "ОБЩАЯ" И объявление входит в "живую" когорту (_is_llm_live_cohort —
    status ACTIVE/PAUSED или effective_status ACTIVE). Без этого этапа
    "эмоциональные" PRODA-креативы без буквальных keywords («Критика рынка»,
    «История Олега» и т.п.) навсегда падают в "ОБЩАЯ" (см. docs/specs/
    ARCH-product-tags.md §5). Архивные/DELETED строки с keyword-ОБЩАЯ LLM
    НЕ добирает — остаются "ОБЩАЯ" от keyword-этапа (несоразмерная стоимость
    LLM на весь исторический архив аккаунта, отчёты по ним не строятся).

    Разовый по своей природе: после первого прогона строк с NULL/'' не остаётся,
    следующие вызовы (крон T6) находят 0 строк и LLM не зовут.

    UPDATE параметризованный и точечный — трогает только target_product,
    остальные колонки (spend/leads/творческая классификация и т.д.) не задеваем.

    Returns: {"total": N, "by_keyword": K, "by_llm": M, "llm_scope_rows": L,
              "by_product": {"PRODA": X, ...}}
    llm_scope_rows — верхняя граница "живой" когорты среди NULL-кандидатов
    (для контроля стоимости LLM ДО прогона, логируется сразу при старте);
    by_llm — фактическое число вызовов LLM (≤ llm_scope_rows, т.к. часть
    живых строк ловит keyword и без LLM).
    """
    from services.product_tags import classify_product, _llm_classify_product, VALID_PRODUCTS
    from services.llm_credit_guard import CreditBalanceError

    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT ad_id, ad_name, ad_body, status, effective_status FROM creative_kb "
            "WHERE target_product IS NULL OR target_product = ''"
        ).fetchall()
        candidates = [dict(r) for r in rows]
    finally:
        conn.close()

    total = len(candidates)
    if total == 0:
        logger.info("backfill_target_product: нет строк с NULL/пустым target_product — бэкфилл не нужен")
        return {
            "total": 0, "by_keyword": 0, "by_llm": 0, "llm_scope_rows": 0,
            "by_product": {p: 0 for p in VALID_PRODUCTS},
        }

    # Верхняя граница стоимости LLM: сколько NULL-кандидатов вообще входят
    # в живую когорту (до keyword-фильтрации) — логируем ДО запуска цикла,
    # чтобы аномальный масштаб (архив вместо live) был виден сразу в логе.
    llm_scope_rows = sum(
        1 for row in candidates if _is_llm_live_cohort(row.get("status"), row.get("effective_status"))
    )
    logger.info(
        "backfill_target_product: найдено %d строк для бэкфилла target_product, "
        "живая когорта (верхняя граница LLM-добора) = %d",
        total, llm_scope_rows,
    )

    by_keyword = 0
    by_llm = 0
    archived_general_skipped = 0
    credit_aborted = False
    by_product: dict[str, int] = {p: 0 for p in VALID_PRODUCTS}

    conn = _get_connection()
    try:
        for idx, row in enumerate(candidates, start=1):
            ad_id = row["ad_id"]
            ad_name = row.get("ad_name") or ""
            ad_body = row.get("ad_body") or ""

            # Этап 1: keyword-эвристика (чистая функция, LLM не зовёт)
            product = classify_product(ad_name, ad_body)
            if product != "ОБЩАЯ":
                by_keyword += 1
            elif _is_llm_live_cohort(row.get("status"), row.get("effective_status")):
                # Этап 2: keyword ничего не поймал, но объявление живое —
                # обязательный LLM-добор, иначе эмоциональные PRODA-заходы
                # без keywords теряются навсегда
                try:
                    product = _llm_classify_product(ad_name, ad_body)
                except CreditBalanceError:
                    # Кончились кредиты Anthropic — алерт уже отправлен стражем.
                    # Прерываем цикл СРАЗУ: иначе каждая оставшаяся строка сожжёт
                    # ещё один заведомо провальный LLM-вызов.
                    # Текущую строку не пишем — остаётся NULL, доберётся при пополнении.
                    logger.error(
                        "backfill_target_product: кредиты Anthropic кончились — прерываю бэкфилл на %d/%d",
                        idx, total,
                    )
                    credit_aborted = True
                    break
                by_llm += 1
            else:
                # Архив/DELETED с keyword-ОБЩАЯ — LLM НЕ зовём, дорого и
                # бессмысленно для мёртвых объявлений; остаётся "ОБЩАЯ"
                archived_general_skipped += 1

            by_product[product] = by_product.get(product, 0) + 1

            conn.execute(
                "UPDATE creative_kb SET target_product = ? WHERE ad_id = ?",
                (product, ad_id),
            )
            conn.commit()

            if idx % 25 == 0 or idx == total:
                logger.info(
                    "backfill_target_product: прогресс %d/%d (by_keyword=%d, by_llm=%d, archived_skipped=%d)",
                    idx, total, by_keyword, by_llm, archived_general_skipped,
                )
    finally:
        conn.close()

    logger.info(
        "backfill_target_product завершён: total=%d, by_keyword=%d, by_llm=%d, "
        "llm_scope_rows=%d, archived_general_skipped=%d, credit_aborted=%s, by_product=%s",
        total, by_keyword, by_llm, llm_scope_rows, archived_general_skipped, credit_aborted, by_product,
    )
    return {
        "total": total, "by_keyword": by_keyword, "by_llm": by_llm,
        "llm_scope_rows": llm_scope_rows, "credit_aborted": credit_aborted,
        "by_product": by_product,
    }
