"""
Журнал гипотез Фазы 4 (Аналитик-Гипотезник).

При каждом успешном авто-запуске карточки фиксируем ИЗМЕРИМУЮ гипотезу:
угол/город/формат/сегмент + порог (CPL/qual_pct/payments), взятый из данных
на момент запуска. Через 7-14 дней hypothesis_verdict.py сверяет её с фактом
и выносит вердикт, записывая урок в learnings.

Одна карточка может запуститься в несколько городов — на каждый город своя
гипотеза (у каждого города свой порог CPL/qual). Идемпотентность: если для
данных ad_ids уже есть открытая гипотеза, повторно не пишем (защита от
дублей при повторном чтении лога).

См. docs/specs/ARCH-phase4-hypothesist.md §6.1, §8.
"""

import json
import logging
import re
import statistics
import sqlite3
from datetime import datetime, timedelta

from services.creative_intelligence import _get_connection

logger = logging.getLogger(__name__)

# Минимум строк creative_kb для медианы CPL города (иначе данных мало —
# считаем город "новым" и падаем на порог qual_pct сегмента)
_MIN_ROWS_FOR_MEDIAN = 5

# Минимальный расход объявления, чтобы участвовать в медиане CPL города
# (копеечные тестовые прогоны искажают медиану)
_MIN_SPEND_FOR_MEDIAN = 15.0

# Порог квала для нового города/сегмента без медианы CPL (SEGMENT_QUAL_MIN,
# §8 спеки) — ориентир из guardian.early_qual_override_pct=15 минус запас.
SEGMENT_QUAL_MIN = 12.0

# Допуск на вариацию CPL референса teardown (30% — реклама-вариация может
# быть чуть дороже референса и всё равно считаться подтверждением)
_TEARDOWN_CPL_TOLERANCE = 1.3

def _local_naive(moment: datetime) -> datetime:
    """Приводит момент к naive-локальному виду — конвенции хранения таблицы.

    В hypotheses колонки created_at/verdict_at пишутся как naive-локальные
    строки '%Y-%m-%d %H:%M:%S' (record_hypothesis:220), и обратно читаются
    naive-датами через strptime. Любой aware-момент от вызывающего (крон
    web/app.py._cron_hypothesis_verdict передаёт aware-время в локальной зоне бизнеса)
    иначе роняет весь проход вердикта на первом же вычитании:
    «can't subtract offset-naive and offset-aware datetimes». Здесь aware сначала переводится в локальную зону процесса,
    а потом теряет tzinfo — тот же момент времени, что и datetime.now().

    Именно приведение к naive, а не перевод хранения на UTC-aware: уже
    записанные строки naive-локальные, и молчаливая смена конвенции сдвинула
    бы возраст всех открытых гипотез на величину смещения зоны сервера.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


# Регэкспы для разбора строк лога launch_single (agent/launcher.py L140/142)
_RE_LOG_SINGLE = re.compile(r"^✅\s*(?P<city>[^:]+):\s*(?P<ad_id>\S+)\s*$")
_RE_LOG_MULTI = re.compile(
    r"^✅\s*(?P<city>[^:]+):\s*\d+\s*объявлени[йя]?\s*—\s*(?P<ids>.+)$"
)


def extract_ad_ids_from_log(log: list[str]) -> dict[str, list[str]]:
    """Достаёт ad_id по городам из status['log'] launch_single.

    Формат строк лога (agent/launcher.py L140/142):
      "✅ {city}: {ad_id}"                          — одно объявление
      "✅ {city}: {N} объявлений — id1, id2, id3"   — несколько
    Возвращает {city: [ad_id, ...]}. Города без объявлений не включаются.
    Строки с ❌ и прочий мусор (не совпавший с форматом) игнорируются.
    """
    result: dict[str, list[str]] = {}
    for line in log:
        if not isinstance(line, str) or "✅" not in line:
            continue

        multi_match = _RE_LOG_MULTI.match(line.strip())
        if multi_match:
            city = multi_match.group("city").strip()
            ad_ids = [ad_id.strip() for ad_id in multi_match.group("ids").split(",") if ad_id.strip()]
            if city and ad_ids:
                result[city] = ad_ids
            continue

        single_match = _RE_LOG_SINGLE.match(line.strip())
        if single_match:
            city = single_match.group("city").strip()
            ad_id = single_match.group("ad_id").strip()
            if city and ad_id:
                result[city] = [ad_id]

    return result


def _median_cpl_for_city(city: str) -> float | None:
    """Медиана CPL по объявлениям города из creative_kb.

    SQL (параметризованный): WHERE city = ? AND cpl > 0 AND spend >= 15
    AND is_full_cabinet = 1. Возвращает медиану или None если данных <5 строк.
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT cpl FROM creative_kb
            WHERE city = ? AND cpl > 0 AND spend >= ? AND is_full_cabinet = 1
            """,
            (city, _MIN_SPEND_FOR_MEDIAN),
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < _MIN_ROWS_FOR_MEDIAN:
        return None

    cpl_values = [float(row["cpl"]) for row in rows]
    return statistics.median(cpl_values)


def build_expectation(
    city: str, segment: str, ad_format: str, source: str, reference: dict | None
) -> dict:
    """Строит измеримое ожидание из данных (формула §8, детерминирована).

    Приоритет источника метрики:
      1. source == "teardown" (есть reference) — «не хуже референса».
      2. иначе медиана CPL города, если данных достаточно.
      3. иначе (новый город/сегмент) — порог qual_pct сегмента.

    Возвращает dict {"metric", "op", "threshold", "basis"}.
    """
    if source == "teardown" and reference:
        ref_payments = reference.get("payments") or 0
        if ref_payments >= 1:
            return {
                "metric": "payments",
                "op": ">=",
                "threshold": 1,
                "basis": f"референс дал {ref_payments} оплат, ждём хотя бы 1",
            }
        ref_cpl = float(reference.get("cpl") or 0)
        threshold = round(ref_cpl * _TEARDOWN_CPL_TOLERANCE, 2)
        return {
            "metric": "cpl",
            "op": "<=",
            "threshold": threshold,
            "basis": f"CPL референса ${ref_cpl} ×1.3",
        }

    median_cpl = _median_cpl_for_city(city)
    if median_cpl is not None:
        return {
            "metric": "cpl",
            "op": "<=",
            "threshold": round(median_cpl, 2),
            "basis": f"медиана CPL {city} ${round(median_cpl, 2)}",
        }

    return {
        "metric": "qual_pct",
        "op": ">=",
        "threshold": SEGMENT_QUAL_MIN,
        "basis": f"порог квала сегмента {segment}",
    }


def _derive_from_card(card_name: str, campaign_type: str, cities: list[str]) -> dict:
    """Фолбэк-производные поля гипотезы, когда topic отсутствует (§8: topic is None).

    angle — первые 40 символов card_name; ad_format пустой; source='manual';
    segment по метке PRODA/PRODB в имени карточки или 'общий'.
    """
    name_lower = card_name.lower()
    if "prodb" in name_lower:
        segment = "PRODB"
    elif "proda" in name_lower:
        segment = "PRODA"
    else:
        segment = "общий"

    return {
        "angle": card_name[:40],
        "ad_format": "",
        "segment": segment,
        "source": "manual",
        "reference": None,
    }


def _hypothesis_exists_for_ad_ids(conn: sqlite3.Connection, ad_ids: list[str]) -> bool:
    """Проверяет, есть ли уже открытая гипотеза, покрывающая эти ad_ids.

    Идемпотентность: сравниваем JSON-строку ad_ids как множество, поэтому
    достаточно найти пересечение хотя бы по одному ad_id среди open-гипотез.
    """
    if not ad_ids:
        return False

    rows = conn.execute(
        "SELECT ad_ids FROM hypotheses WHERE status = 'open'"
    ).fetchall()

    wanted = set(ad_ids)
    for row in rows:
        try:
            existing_ids = set(json.loads(row["ad_ids"]) or [])
        except (json.JSONDecodeError, TypeError):
            continue
        if wanted & existing_ids:
            return True
    return False


def record_hypothesis(
    card_name: str,
    campaign_type: str,
    ad_ids_by_city: dict[str, list[str]],
    topic: dict | None,
    now: datetime | None = None,
) -> list[int]:
    """Записывает гипотезу(-ы) при запуске карточки. Возвращает id созданных строк.

    Одна карточка может запуститься в несколько городов -> одна гипотеза на
    город (у каждого свой порог CPL/qual). angle/ad_format/source/reference
    берём из topic если он есть, иначе derive из card_name/campaign_type.
    Идемпотентность: если для этих ad_ids уже есть открытая гипотеза — не
    дублируем.
    """
    if not ad_ids_by_city:
        return []

    now = _local_naive(now or datetime.now())
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")

    if topic:
        base_angle = topic.get("angle", "") or card_name[:40]
        base_ad_format = topic.get("ad_format", "") or ""
        base_segment = topic.get("segment", "общий") or "общий"
        base_source = topic.get("source", "manual") or "manual"
        base_reference = topic.get("reference")
    else:
        derived = _derive_from_card(card_name, campaign_type, list(ad_ids_by_city.keys()))
        base_angle = derived["angle"]
        base_ad_format = derived["ad_format"]
        base_segment = derived["segment"]
        base_source = derived["source"]
        base_reference = derived["reference"]

    reference_ad_id = None
    if base_reference:
        reference_ad_id = base_reference.get("ad_id")

    created_ids: list[int] = []
    conn = _get_connection()
    try:
        for city, ad_ids in ad_ids_by_city.items():
            if not ad_ids:
                continue
            if _hypothesis_exists_for_ad_ids(conn, ad_ids):
                logger.info(
                    "hypothesis_journal: гипотеза для ad_ids=%s (город %s) уже существует — пропуск",
                    ad_ids, city,
                )
                continue

            expectation = build_expectation(
                city=city,
                segment=base_segment,
                ad_format=base_ad_format,
                source=base_source,
                reference=base_reference,
            )

            cursor = conn.execute(
                """
                INSERT INTO hypotheses
                    (angle, city, ad_format, segment, source, card_name,
                     ad_ids, reference_ad_id, expectation_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    base_angle,
                    city,
                    base_ad_format,
                    base_segment,
                    base_source,
                    card_name,
                    json.dumps(ad_ids, ensure_ascii=False),
                    reference_ad_id,
                    json.dumps(expectation, ensure_ascii=False),
                    created_at,
                ),
            )
            created_ids.append(cursor.lastrowid)

        conn.commit()
    finally:
        conn.close()

    return created_ids


def _row_to_dict(row: sqlite3.Row, now: datetime) -> dict:
    """Преобразует строку hypotheses в dict контракта (ad_ids/expectation распакованы)."""
    created_at_raw = row["created_at"]
    # now может прийти aware (крон вердикта передаёт локальное время), а
    # created_at в таблице — naive-локальная строка: без приведения вычитание
    # ниже падает TypeError и роняет весь проход вердикта.
    now = _local_naive(now)
    try:
        created_dt = datetime.strptime(created_at_raw, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        created_dt = now
    age_days = (now - created_dt).days

    try:
        ad_ids = json.loads(row["ad_ids"]) or []
    except (json.JSONDecodeError, TypeError):
        ad_ids = []

    try:
        expectation = json.loads(row["expectation_json"]) or {}
    except (json.JSONDecodeError, TypeError):
        expectation = {}

    return {
        "id": row["id"],
        "angle": row["angle"],
        "city": row["city"],
        "ad_format": row["ad_format"],
        "segment": row["segment"],
        "source": row["source"],
        "card_name": row["card_name"],
        "ad_ids": ad_ids,
        "reference_ad_id": row["reference_ad_id"],
        "expectation": expectation,
        "status": row["status"],
        "lesson": row["lesson"],
        "created_at": created_at_raw,
        "age_days": age_days,
    }


def get_open_hypotheses(
    min_age_days: int, max_age_days: int, now: datetime | None = None
) -> list[dict]:
    """Открытые гипотезы возрастом [min_age_days, max_age_days] дней.

    SQL: WHERE status='open' AND created_at BETWEEN (now-max) AND (now-min).
    Каждый dict: {id, angle, city, ad_format, segment, source, card_name,
                  ad_ids(list), reference_ad_id, expectation(dict), created_at, age_days}.
    """
    now = _local_naive(now or datetime.now())
    lower_bound = now.replace(microsecond=0) - timedelta(days=max_age_days)
    upper_bound = now.replace(microsecond=0) - timedelta(days=min_age_days)

    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM hypotheses
            WHERE status = 'open'
              AND created_at >= ?
              AND created_at <= ?
            ORDER BY created_at ASC
            """,
            (lower_bound.strftime("%Y-%m-%d %H:%M:%S"), upper_bound.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()
    finally:
        conn.close()

    return [_row_to_dict(row, now) for row in rows]


def get_hypotheses_by_status(status: str, since_iso: str | None = None) -> list[dict]:
    """Гипотезы с заданным статусом, опционально созданные/закрытые после since_iso.

    Фильтрует по verdict_at (для закрытых статусов) или created_at (для 'open'),
    в зависимости от того, что осмысленно для статуса.
    """
    conn = _get_connection()
    try:
        if status == "open":
            query = "SELECT * FROM hypotheses WHERE status = ?"
            params: tuple = (status,)
            if since_iso:
                query += " AND created_at >= ?"
                params = (status, since_iso)
        else:
            query = "SELECT * FROM hypotheses WHERE status = ?"
            params = (status,)
            if since_iso:
                query += " AND verdict_at >= ?"
                params = (status, since_iso)
        query += " ORDER BY created_at ASC"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    now = datetime.now()
    return [_row_to_dict(row, now) for row in rows]
