"""
Резюмируемый бэкфилл всего рекламного кабинета → creative_kb.

Скачивает ВСЕ объявления кабинета (все статусы включая ARCHIVED/DELETED)
с lifetime-метриками и creative-полями. Курсор хранится в таблице
backfill_state (ключ 'cabinet_backfill').

При FBApiError курсор НЕ двигается — следующий запуск продолжит с того же места.

Обработка ошибок FB:
- code=100 subcode=1815001 "Deleted not supported" → автодеградация: повторяем
  без DELETED в статусах (DEV-кабинет не поддерживает запрос удалённых).
- code=1 "reduce data" → уменьшаем page_size (25→10→5) и повторяем ту же страницу.
- настоящий rate-limit (429/4/17/32/80003/80004) → rate_limited=True, курсор не двигаем.
- прочие ошибки → логируем с кодом, возвращаем error=..., rate_limited=False.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from agent.fb_common import (
    API,
    FBApiError,
    _throttled_get,
    build_adset_map,
)
from agent.learner import classify_business, classify_creative, parse_video_insight_row
from services.creative_intelligence import _get_connection
from services.fb_token_provider import get_fb_account_id, get_fb_token

logger = logging.getLogger(__name__)

# Статусы для полного кабинета (включая архив/удалённые)
ACCOUNT_EFFECTIVE_STATUSES = [
    "ACTIVE",
    "PAUSED",
    "DELETED",
    "ARCHIVED",
    "ADSET_PAUSED",
    "CAMPAIGN_PAUSED",
    "PENDING_REVIEW",
    "DISAPPROVED",
    "PREAPPROVED",
    "PENDING_BILLING_INFO",
    "IN_PROCESS",
    "WITH_ISSUES",
]

# Статусы без DELETED — фолбэк для DEV-кабинетов где DELETED не поддерживается
ACCOUNT_EFFECTIVE_STATUSES_NO_DELETED = [
    s for s in ACCOUNT_EFFECTIVE_STATUSES if s != "DELETED"
]

# ---------------------------------------------------------------------------
# Детекторы ошибок FB API
# ---------------------------------------------------------------------------


def _is_true_rate_limit_error(exc: FBApiError) -> bool:
    """Настоящий rate-limit: 429 или FB code 4/17/32/80003/80004.
    Код 1 (reduce data) и 100 (invalid param) — НЕ rate-limit.
    Аналог из metrics_backfill._is_true_rate_limit_error.
    """
    return exc.status_code in (429, 4, 17, 32, 80003, 80004)


def _is_reduce_data_exc(exc: FBApiError) -> bool:
    """FB 'Please reduce the amount of data' — status_code=1 (code из тела ответа)."""
    return exc.status_code == 1


def _is_deleted_not_supported(exc: FBApiError) -> bool:
    """FB code=100, subcode=1815001 — 'Deleted objects not supported'.
    DEV-кабинеты не поддерживают effective_status=DELETED в /ads.
    Текст проверяем по subcode чтобы не зависеть от локализации сообщения.
    """
    if exc.status_code != 400:
        return False
    msg = str(exc)
    return "1815001" in msg


# Ступени уменьшения page_size при reduce-data ошибке
_PAGE_SIZE_STEPS = [25, 10, 5]


# Дефолтное состояние бэкфилла
_DEFAULT_STATE = {
    "cursor": None,
    "fetched_total": 0,
    "upserted_total": 0,
    "done": False,
    "last_run_at": None,
    "rate_limited_at": None,
}

# Ключ в таблице backfill_state
_STATE_KEY = "cabinet_backfill"


# ---------------------------------------------------------------------------
# Управление состоянием
# ---------------------------------------------------------------------------


def get_backfill_state() -> dict:
    """Читает состояние бэкфилла из таблицы backfill_state (key='cabinet_backfill').

    Если строки нет — возвращает дефолт:
        {"cursor": None, "fetched_total": 0, "upserted_total": 0,
         "done": False, "last_run_at": None, "rate_limited_at": None}.
    cursor — строка after-пагинации FB Graph API (paging.cursors.after) или None для старта.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM backfill_state WHERE key = ?",
            (_STATE_KEY,),
        ).fetchone()
        if row is None:
            return dict(_DEFAULT_STATE)
        try:
            state = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            logger.warning("Некорректный JSON в backfill_state[%s], сбрасываем", _STATE_KEY)
            return dict(_DEFAULT_STATE)
        # Заполняем отсутствующие ключи дефолтами
        for k, v in _DEFAULT_STATE.items():
            state.setdefault(k, v)
        return state
    finally:
        conn.close()


def save_backfill_state(state: dict) -> None:
    """UPSERT состояния в backfill_state (key='cabinet_backfill', value=JSON-строка)."""
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO backfill_state (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (_STATE_KEY, json.dumps(state, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def reset_backfill_state() -> None:
    """Сбрасывает состояние (cursor=None, done=False, *_total=0). Для ручного перезапуска."""
    save_backfill_state(dict(_DEFAULT_STATE))
    logger.info("backfill_state сброшен")


# ---------------------------------------------------------------------------
# Вспомогательное состояние (крон-гейты)
# ---------------------------------------------------------------------------


def _get_state_by_key(key: str) -> dict:
    """Читает произвольный ключ из backfill_state (для гейтинга кронов)."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM backfill_state WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return {}
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return {}
    finally:
        conn.close()


def _save_state_by_key(key: str, value: dict) -> None:
    """Сохраняет произвольный ключ в backfill_state."""
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO backfill_state (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, json.dumps(value, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Скачивание объявлений
# ---------------------------------------------------------------------------


def _fetch_ads_page(
    cursor: str | None,
    page_size: int,
    statuses: list[str] | None = None,
) -> tuple[list[dict], str | None]:
    """Тянет ОДНУ страницу всех объявлений кабинета через account-level /ads.

    Использует agent.fb_common._throttled_get (троттлинг 5с + retry rate-limit).
    effective_status: по умолчанию ACCOUNT_EFFECTIVE_STATUSES (включая DELETED).
    Если передать statuses=ACCOUNT_EFFECTIVE_STATUSES_NO_DELETED — работает без DELETED
    (фолбэк для DEV-кабинетов где code=100 subcode=1815001).
    fields: id,name,status,effective_status,created_time,adset_id,campaign{name},
            adset{name,optimization_goal}.
    page_size: limit FB (рекомендуется 25 в DEV-режиме).

    Returns: (ads, next_cursor)
        ads — список dict как из FB (сырые поля);
        next_cursor — paging.cursors.after или None если страниц больше нет.
    Бросает FBApiError при rate-limit/ошибке (НЕ глушит — ловит верхний уровень).
    Тело ошибки содержит FB subcode для детекторов выше (_is_deleted_not_supported и др.).
    """
    token = get_fb_token()
    account_id = get_fb_account_id()

    # Список статусов: дефолт включает DELETED, фолбэк — без него
    effective_statuses = statuses if statuses is not None else ACCOUNT_EFFECTIVE_STATUSES

    params: dict = {
        "access_token": token,
        "fields": (
            "id,name,status,effective_status,created_time,adset_id,"
            "campaign{name},adset{name,optimization_goal}"
        ),
        "effective_status": json.dumps(effective_statuses),
        "limit": page_size,
    }
    # FB after-курсор пагинации
    if cursor:
        params["after"] = cursor

    resp = _throttled_get(
        f"{API}/act_{account_id}/ads",
        params=params,
    )

    if resp.status_code != 200:
        raise FBApiError(
            f"FB /ads ошибка: {resp.status_code} {resp.text[:300]}",
            resp.status_code,
        )

    data = resp.json()
    ads = data.get("data", [])

    # Извлекаем after-курсор из paging.cursors
    paging = data.get("paging", {})
    next_cursor: str | None = paging.get("cursors", {}).get("after")

    # Если нет next-ссылки — страниц больше нет (курсор тоже не нужен)
    if "next" not in paging:
        next_cursor = None

    return ads, next_cursor


# ---------------------------------------------------------------------------
# Lifetime-метрики
# ---------------------------------------------------------------------------


def _fetch_lifetime_insights(ad_ids: list[str]) -> dict[str, dict]:
    """Lifetime-метрики для списка ad_id (date_preset=maximum, level=ad).

    Батчами по 50 ad_id. Парсит spend/impressions/clicks/ctr/cpm/frequency +
    video-метрики через agent.learner.parse_video_insight_row.

    Returns: {ad_id: {spend, leads, cpl, ctr, cpm, impressions, clicks, frequency,
                       video_views_3s, thruplay, video_p25, video_p50, video_p75,
                       video_p100, hook_rate, hold_rate}}
    Пустой dict если ad_ids пуст. НЕ бросает — при ошибке батча логирует и пропускает.
    """
    if not ad_ids:
        return {}

    token = get_fb_token()
    account_id = get_fb_account_id()
    result: dict[str, dict] = {}

    BATCH = 50
    for i in range(0, len(ad_ids), BATCH):
        batch = ad_ids[i : i + BATCH]
        try:
            resp = _throttled_get(
                f"{API}/act_{account_id}/insights",
                params={
                    "access_token": token,
                    "level": "ad",
                    "date_preset": "maximum",
                    "fields": ",".join([
                        "ad_id",
                        "spend",
                        "impressions",
                        "clicks",
                        "ctr",
                        "cpm",
                        "frequency",
                        "actions",
                        "video_thruplay_watched_actions",
                        "video_p25_watched_actions",
                        "video_p50_watched_actions",
                        "video_p75_watched_actions",
                        "video_p100_watched_actions",
                    ]),
                    # Фильтруем только нужные ad_id в батче
                    "filtering": json.dumps([
                        {"field": "ad.id", "operator": "IN", "value": batch}
                    ]),
                    "limit": len(batch),
                },
            )
            if resp.status_code != 200:
                logger.warning(
                    "_fetch_lifetime_insights батч %d: FB API %d — %s",
                    i // BATCH,
                    resp.status_code,
                    resp.text[:200],
                )
                continue

            data = resp.json()
            for row in data.get("data", []):
                ad_id = row.get("ad_id")
                if not ad_id:
                    continue
                metrics = parse_video_insight_row(row)
                # Рассчитываем hook_rate и hold_rate из распарсенных метрик
                impressions = metrics.get("impressions", 0)
                video_views_3s = metrics.get("video_views_3s", 0)
                thruplay = metrics.get("thruplay", 0)
                hook_rate = round(video_views_3s / impressions * 100, 1) if impressions > 0 else 0
                hold_rate = round(thruplay / video_views_3s * 100, 1) if video_views_3s > 0 else 0
                metrics["hook_rate"] = hook_rate
                metrics["hold_rate"] = hold_rate
                result[ad_id] = metrics

        except Exception as exc:
            logger.warning(
                "_fetch_lifetime_insights батч %d: ошибка — %s",
                i // BATCH,
                exc,
            )

    return result


# ---------------------------------------------------------------------------
# Creative-поля
# ---------------------------------------------------------------------------


def _fetch_creative_fields_batch(ad_ids: list[str]) -> dict[str, dict]:
    """ad_body + ad_headline + thumbnail_url + content_type для списка ad_id.

    Переиспользует логику из services.creative_intelligence._fetch_creative_fields,
    дополняя результат полем content_type (video/image/carousel).

    Returns: {ad_id: {"ad_body": str, "ad_headline": str, "image_url": str|None,
                      "content_type": "video"|"image"|"carousel"}}
    """
    from services.creative_intelligence import _extract_body_and_image

    if not ad_ids:
        return {}

    token = get_fb_token()
    result: dict[str, dict] = {}

    BATCH = 50
    for i in range(0, len(ad_ids), BATCH):
        batch = ad_ids[i : i + BATCH]
        params = {
            "ids": ",".join(batch),
            "fields": (
                "creative{body,image_url,thumbnail_url,"
                "object_story_spec{video_data{message,image_url},"
                "link_data{message,picture,child_attachments}}}"
            ),
            "access_token": token,
        }
        try:
            resp = _throttled_get(
                f"{API}/",
                params=params,
            )
            if not resp.ok:
                logger.warning(
                    "_fetch_creative_fields_batch батч %d: %s",
                    i // BATCH,
                    resp.text[:200],
                )
                continue

            data = resp.json()
            for ad_id, ad_data in data.items():
                if ad_id == "error":
                    logger.warning("FB API вернул ошибку batch: %s", ad_data)
                    continue
                if isinstance(ad_data, dict) and "error" in ad_data:
                    logger.debug("Пропускаем недоступный ad_id=%s", ad_id)
                    continue

                creative = ad_data.get("creative") or {}
                body, img = _extract_body_and_image(creative)

                # Определяем content_type по структуре creative
                spec = creative.get("object_story_spec") or {}
                link_data = spec.get("link_data") or {}
                if spec.get("video_data"):
                    content_type = "video"
                elif link_data.get("child_attachments"):
                    content_type = "carousel"
                else:
                    content_type = "image"

                # Headline из body если отдельного нет (FB не всегда отдаёт)
                headline = creative.get("name", "")

                result[ad_id] = {
                    "ad_body": body or "",
                    "ad_headline": headline or "",
                    "image_url": img,
                    "content_type": content_type,
                }
        except Exception as exc:
            logger.warning(
                "_fetch_creative_fields_batch батч %d: ошибка — %s",
                i // BATCH,
                exc,
            )

    return result


# ---------------------------------------------------------------------------
# Классификация и UPSERT
# ---------------------------------------------------------------------------


def _classify(ad: dict) -> tuple[str, str]:
    """Возвращает (creative_class, business_class) по тем же правилам что learner.

    creative_class: agent.learner.classify_creative(hook_rate, hold_rate).
    business_class: agent.learner.classify_business(ad).
    """
    hook_rate = float(ad.get("hook_rate") or 0)
    hold_rate = float(ad.get("hold_rate") or 0)
    creative_class = classify_creative(hook_rate, hold_rate)
    business_class = classify_business(ad)
    return creative_class, business_class


def _upsert_ad(
    conn,
    ad: dict,
    metrics: dict,
    creative: dict,
    adset_map: dict,
) -> bool:
    """ON CONFLICT(ad_id) DO UPDATE одной записи в creative_kb с is_full_cabinet=1.

    ВАЖНО: используем ON CONFLICT(ad_id) DO UPDATE SET (НЕ INSERT OR REPLACE) —
    REPLACE затрёт строку целиком вместе с разметкой и AMO-исходами.
    В SET перечисляем ТОЛЬКО поля бэкфилла.
    НЕ включаем labeled_at/label_source/hook_type_id/angle_id/offer_type_id/
    outcomes_matched_at/qual_*/romi/payments/revenue/cpql/vision_tags/total_score.

    city/adset_type: берём из adset_map (build_adset_map). Если adset_id неизвестен —
    city='', adset_type='' (объявление из чужого/старого адсета — всё равно сохраняем).

    business_class: бэкфилл всегда классифицирует без AMO-данных (romi/qual_pct/
    payments=None) → classify_business отдаёт заглушку «Нет данных». CASE в SET-блоке
    защищает уже осмысленную разметку (напр. «Прибыльный», выставленную синком/лейблингом
    из AMO) от затирания этой заглушкой при обновлении: если excluded.business_class ==
    'Нет данных' — сохраняем прежнее значение creative_kb.business_class, иначе пишем новое.
    На INSERT (новая строка) CASE не мешает — creative_kb.business_class ещё не существует,
    пишется excluded.business_class как есть.

    Returns: True если новая запись (INSERT), False если обновление (UPDATE).
    """
    ad_id = str(ad.get("id", "")).strip()
    if not ad_id:
        return False

    # Проверяем: новая запись или обновление
    existing = conn.execute(
        "SELECT 1 FROM creative_kb WHERE ad_id = ?",
        (ad_id,),
    ).fetchone()
    is_new = existing is None

    # city/adset_type из карты адсетов
    adset_id = ad.get("adset_id", "")
    if adset_id in adset_map:
        city, adset_type = adset_map[adset_id]
    else:
        city, adset_type = "", ""

    # Дата создания
    created_at = ad.get("created_time", "")

    # days_running из created_time
    days_running = 0
    if created_at:
        try:
            created_dt = datetime.fromisoformat(created_at.replace("+0000", "+00:00"))
            days_running = (datetime.now(timezone.utc) - created_dt).days
        except (ValueError, TypeError):
            pass

    # Метрики из insights (дефолт 0 если DELETED без данных)
    spend = float(metrics.get("spend") or 0)
    leads = int(metrics.get("leads") or 0)
    cpl = float(metrics.get("cpl") or 0)
    ctr = float(metrics.get("ctr") or 0)
    cpm = float(metrics.get("cpm") or 0)
    impressions = int(metrics.get("impressions") or 0)
    clicks = int(metrics.get("clicks") or 0)
    frequency = float(metrics.get("frequency") or 0)
    hook_rate = float(metrics.get("hook_rate") or 0)
    hold_rate = float(metrics.get("hold_rate") or 0)
    video_views_3s = int(metrics.get("video_views_3s") or 0)
    thruplay = int(metrics.get("thruplay") or 0)
    video_p25 = int(metrics.get("video_p25") or 0)
    video_p50 = int(metrics.get("video_p50") or 0)
    video_p75 = int(metrics.get("video_p75") or 0)
    video_p100 = int(metrics.get("video_p100") or 0)

    # Классификация (используем метрики из инсайтов)
    ad_with_metrics = {
        "hook_rate": hook_rate,
        "hold_rate": hold_rate,
        "romi": None,
        "qual_pct": None,
        "payments": None,
    }
    creative_class, business_class = _classify(ad_with_metrics)

    # Creative поля
    ad_body = creative.get("ad_body", "")
    ad_headline = creative.get("ad_headline", "")
    image_url = creative.get("image_url")
    content_type = creative.get("content_type", "video")

    conn.execute(
        """
        INSERT INTO creative_kb (
            ad_id, ad_name, status, effective_status,
            city, adset_type,
            spend, leads, cpl, ctr, cpm,
            impressions, clicks, frequency,
            hook_rate, hold_rate,
            video_views_3s, thruplay,
            video_p25, video_p50, video_p75, video_p100,
            ad_body, ad_headline, image_url, content_type,
            creative_class, business_class,
            days_running, created_at,
            is_full_cabinet, synced_at
        ) VALUES (
            ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?,
            ?, ?,
            1, datetime('now')
        )
        ON CONFLICT(ad_id) DO UPDATE SET
            ad_name        = excluded.ad_name,
            status         = excluded.status,
            effective_status = excluded.effective_status,
            city           = excluded.city,
            adset_type     = excluded.adset_type,
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
            ad_body        = excluded.ad_body,
            ad_headline    = excluded.ad_headline,
            image_url      = excluded.image_url,
            content_type   = excluded.content_type,
            creative_class = excluded.creative_class,
            business_class = CASE
                WHEN excluded.business_class = 'Нет данных' THEN creative_kb.business_class
                ELSE excluded.business_class
            END,
            days_running   = excluded.days_running,
            created_at     = excluded.created_at,
            is_full_cabinet = 1,
            synced_at      = excluded.synced_at
        """,
        (
            ad_id,
            ad.get("name", ""),
            ad.get("status", ""),
            ad.get("effective_status", ""),
            city,
            adset_type,
            spend, leads, cpl, ctr, cpm,
            impressions, clicks, frequency,
            hook_rate, hold_rate,
            video_views_3s, thruplay,
            video_p25, video_p50, video_p75, video_p100,
            ad_body, ad_headline, image_url, content_type,
            creative_class, business_class,
            days_running, created_at,
        ),
    )

    return is_new


# ---------------------------------------------------------------------------
# Главный инкремент
# ---------------------------------------------------------------------------


def sync_backfill_increment(max_ads: int = 25) -> dict:
    """ОДИН инкремент бэкфилла (для крона и ручного эндпоинта).

    Алгоритм:
      1. state = get_backfill_state(); если state["done"] → return done no-op.
      2. _fetch_ads_page с умной обработкой ошибок:
         - code=100 subcode=1815001 (DELETED not supported) → повтор без DELETED.
         - code=1 (reduce-data) → уменьшаем page_size (25→10→5) и повторяем.
         - настоящий rate-limit (429/4/17/32/80003/80004) → rate_limited=True, стоп.
         - прочая ошибка → логируем с кодом, возвращаем error=..., rate_limited=False.
      3. Если ads пуст И next_cursor is None → done=True.
      4. Метрики + creative поля (обе функции глушат ошибки).
      5. UPSERT каждого объявления.
      6. Обновляем и сохраняем state.
      7. Возвращаем результат.

    НИКОГДА не бросает наружу (кроме программных ошибок) — rate-limit это штатная ситуация.
    """
    state = get_backfill_state()

    # Бэкфилл уже завершён — no-op
    if state.get("done"):
        return {
            "done": True,
            "fetched": 0,
            "upserted": 0,
            "rate_limited": False,
            "cursor_after": state.get("cursor"),
        }

    # Ограничиваем размер страницы — DEV-режим не любит большие батчи
    page_size = min(max_ads, 25)

    # --- Скачиваем страницу объявлений (с обработкой FB ошибок) ---
    ads: list[dict] = []
    next_cursor: str | None = None

    # Текущий список статусов — может деградировать до _NO_DELETED при ошибке 1815001
    current_statuses: list[str] | None = None  # None = дефолт (с DELETED)

    fetch_ok = False
    for attempt_page_size in _PAGE_SIZE_STEPS:
        # Используем запрошенный page_size только на первой попытке;
        # при reduce-data деградируем по ступеням _PAGE_SIZE_STEPS
        actual_page_size = min(page_size, attempt_page_size)

        try:
            ads, next_cursor = _fetch_ads_page(
                state["cursor"], actual_page_size, statuses=current_statuses
            )
            fetch_ok = True
            break  # успех
        except FBApiError as exc:
            if _is_deleted_not_supported(exc):
                # DEV-кабинет не поддерживает DELETED: повторяем без него (один раз)
                if current_statuses is None:
                    logger.warning(
                        "sync_backfill_increment: FB не поддерживает DELETED "
                        "(code=100 subcode=1815001) — повторяем без DELETED"
                    )
                    current_statuses = ACCOUNT_EFFECTIVE_STATUSES_NO_DELETED
                    # Сбрасываем счётчик page_size — повторяем с тем же page_size
                    # но уже с другим списком статусов
                    try:
                        ads, next_cursor = _fetch_ads_page(
                            state["cursor"], actual_page_size, statuses=current_statuses
                        )
                        fetch_ok = True
                    except FBApiError as exc2:
                        exc = exc2  # будем обрабатывать exc2 ниже
                    if fetch_ok:
                        break

            if _is_true_rate_limit_error(exc):
                # Настоящий rate-limit — курсор не двигаем, вернём rate_limited=True
                logger.warning("sync_backfill_increment: настоящий rate-limit — %s", exc)
                state["rate_limited_at"] = datetime.now(timezone.utc).isoformat()
                save_backfill_state(state)
                return {
                    "rate_limited": True,
                    "fetched": 0,
                    "upserted": 0,
                    "cursor_after": state.get("cursor"),
                    "done": False,
                }

            if _is_reduce_data_exc(exc):
                # Reduce-data — пробуем следующую ступень page_size
                if attempt_page_size <= _PAGE_SIZE_STEPS[-1]:
                    # Уже минимальная ступень — не можем уменьшить дальше
                    logger.error(
                        "sync_backfill_increment: reduce-data при минимальном "
                        "page_size=%d — отдаём ошибку без rate_limited",
                        attempt_page_size,
                    )
                    return {
                        "rate_limited": False,
                        "error": f"reduce-data при page_size={attempt_page_size}",
                        "fetched": 0,
                        "upserted": 0,
                        "cursor_after": state.get("cursor"),
                        "done": False,
                    }
                logger.warning(
                    "sync_backfill_increment: reduce-data (page_size=%d) → "
                    "пробуем следующую ступень",
                    attempt_page_size,
                )
                continue  # следующая ступень _PAGE_SIZE_STEPS

            # Прочая ошибка (напр. транзиентный code 2, code 100 другой subcode) —
            # НЕ rate-limit, курсор не двигаем, но и rate_limited=False
            err_short = str(exc)[:120]
            logger.error(
                "sync_backfill_increment: неожиданная FB ошибка (status_code=%s) — %s",
                exc.status_code,
                err_short,
            )
            return {
                "rate_limited": False,
                "error": f"FB error {exc.status_code}: {err_short}",
                "fetched": 0,
                "upserted": 0,
                "cursor_after": state.get("cursor"),
                "done": False,
            }

    if not fetch_ok:
        # Все ступени исчерпаны (reduce-data на всех) — не должно случиться,
        # но защищаемся от логической ошибки
        return {
            "rate_limited": False,
            "error": "все ступени page_size исчерпаны при reduce-data",
            "fetched": 0,
            "upserted": 0,
            "cursor_after": state.get("cursor"),
            "done": False,
        }

    # --- Проверяем завершение ---
    if not ads and next_cursor is None:
        state["done"] = True
        save_backfill_state(state)
        return {
            "done": True,
            "fetched": 0,
            "upserted": 0,
            "rate_limited": False,
            "cursor_after": state.get("cursor"),
        }

    # --- Lifetime-метрики (глушим ошибки батчей) ---
    ad_ids = [a["id"] for a in ads if a.get("id")]
    metrics = _fetch_lifetime_insights(ad_ids)

    # --- Creative поля (глушим ошибки батчей) ---
    creatives = _fetch_creative_fields_batch(ad_ids)

    # --- Карта адсетов ---
    try:
        adset_map = build_adset_map()
    except Exception as exc:
        logger.warning("build_adset_map ошибка: %s — city/adset_type будут пустыми", exc)
        adset_map = {}

    # --- UPSERT каждого объявления ---
    conn = _get_connection()
    new_count = 0
    try:
        for ad in ads:
            ad_id = str(ad.get("id", "")).strip()
            if not ad_id:
                continue
            ad_metrics = metrics.get(ad_id, {})
            ad_creative = creatives.get(ad_id, {})
            is_new = _upsert_ad(conn, ad, ad_metrics, ad_creative, adset_map)
            if is_new:
                new_count += 1
        conn.commit()
    finally:
        conn.close()

    # --- Обновляем state ---
    state["fetched_total"] = state.get("fetched_total", 0) + len(ads)
    state["upserted_total"] = state.get("upserted_total", 0) + new_count
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    state["rate_limited_at"] = None  # успешный прогон — сбрасываем

    if next_cursor is None:
        # Страниц больше нет — бэкфилл завершён
        state["done"] = True
        state["cursor"] = None
    else:
        state["cursor"] = next_cursor

    save_backfill_state(state)

    logger.info(
        "sync_backfill_increment: fetched=%d, new=%d, cursor=%s",
        len(ads),
        new_count,
        next_cursor,
    )

    return {
        "fetched": len(ads),
        "upserted": new_count,
        "rate_limited": False,
        "cursor_after": next_cursor,
        "done": state["done"],
    }


# ---------------------------------------------------------------------------
# Ежедневный досинк недавних объявлений (закрывает дыру created_at)
# ---------------------------------------------------------------------------

# Защитный лимит страниц: если FB проигнорит серверный created_time-фильтр —
# не крутим пагинацию по всему кабинету (обычно свежих объявлений за 7д мало).
_RECENT_ADS_MAX_PAGES = 20


def _fetch_recent_ads(days: int = 7, page_size: int = 25) -> list[dict]:
    """Тянет объявления кабинета с created_time >= now-days (account-level /ads).

    Серверный фильтр created_time > (now-days) unix-ts — оптимизация; границу
    ВСЕГДА перепроверяем локально (на случай если FB отдаст лишнее). Страничный
    обход через after-курсор (как _fetch_ads_page), стоп при конце страниц или
    потолке _RECENT_ADS_MAX_PAGES.

    fields — те же, что у _fetch_ads_page (id,name,status,effective_status,
    created_time,adset_id,campaign{name},adset{name,optimization_goal}), чтобы
    _upsert_ad получил полный набор полей.

    Returns: список сырых ad-dict (моложе days). Бросает FBApiError при не-200 —
    ловит sync_recent_ads.
    """
    token = get_fb_token()
    account_id = get_fb_account_id()

    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = int(cutoff_dt.timestamp())

    ads: list[dict] = []
    cursor: str | None = None

    for _page in range(_RECENT_ADS_MAX_PAGES):
        params: dict = {
            "access_token": token,
            "fields": (
                "id,name,status,effective_status,created_time,adset_id,"
                "campaign{name},adset{name,optimization_goal}"
            ),
            "filtering": json.dumps(
                [{"field": "created_time", "operator": "GREATER_THAN", "value": cutoff_ts}]
            ),
            "limit": page_size,
        }
        if cursor:
            params["after"] = cursor

        resp = _throttled_get(f"{API}/act_{account_id}/ads", params=params)
        if resp.status_code != 200:
            raise FBApiError(
                f"FB /ads ошибка: {resp.status_code} {resp.text[:300]}", resp.status_code
            )

        data = resp.json()
        for ad in data.get("data", []):
            created_time = ad.get("created_time")
            if not created_time:
                continue
            try:
                created_dt = datetime.fromisoformat(created_time.replace("+0000", "+00:00"))
            except (ValueError, AttributeError):
                continue
            # Локальная перепроверка «моложе days» (FB мог проигнорить фильтр)
            if created_dt >= cutoff_dt:
                ads.append(ad)

        paging = data.get("paging", {})
        cursor = paging.get("cursors", {}).get("after")
        if "next" not in paging or not cursor:
            break

    return ads


def sync_recent_ads(days: int = 7) -> dict:
    """Досинк недавних объявлений (created_time >= now-days) в creative_kb С created_at.

    Зачем: регулярный синк (creative_intelligence) created_at НЕ пишет
    (creative_intelligence.py:574-580), а разовый бэкфилл (sync_backfill_increment)
    давно done → новые ad_id попадали в картотику без created_at, и почасовой
    сборщик (hourly_collector) не видел их кандидатами. Здесь тянем свежие
    объявления и UPSERT-им через _upsert_ad — он пишет created_at из created_time,
    ON CONFLICT(ad_id) DO UPDATE идемпотентен (разметку/AMO-исходы не трогает).

    НЕ трогает backfill_state('cabinet_backfill') и done-флаг разового бэкфилла —
    это независимый ежедневный проход.

    Fail-safe: rate-limit → rate_limited=True (крон повторит), прочие ошибки →
    error=..., наружу НЕ бросает.

    Returns dict: {"fetched": int, "upserted": int (только новые строки),
                   "rate_limited": bool, "error": str | None}
    """
    result = {"fetched": 0, "upserted": 0, "rate_limited": False, "error": None}

    # Карта адсетов — один раз, ВНЕ account-контекста (discovery мультикабинетный)
    try:
        adset_map = build_adset_map()
    except Exception as exc:
        logger.warning("sync_recent_ads: build_adset_map ошибка: %s — city/adset_type пустыми", exc)
        adset_map = {}

    # Мультикабинетно: без обхода всех оффлайн-кабинетов карты
    # роутинга новые объявления cabinet_b не попадали в creative_kb вовсе —
    # автопилот не видел их сливы. Метрики/креативы тянутся в контексте
    # кабинета этих объявлений; отказ одного кабинета не рушит остальные.
    from services.fb_token_provider import fb_account, offline_account_context

    try:
        from services.launch_routing import accounts_to_scan

        accounts = accounts_to_scan() or None
    except Exception as exc:  # noqa: BLE001 — деградация до старого охвата
        logger.warning("sync_recent_ads: карта роутинга недоступна — %s", exc)
        accounts = None
    if not accounts:
        accounts = (str(get_fb_account_id()).replace("act_", ""),)

    for account_id in accounts:
        try:
            account_ctx = offline_account_context(account_id)
        except Exception as exc:  # noqa: BLE001 — незарегистрированный кабинет
            logger.warning("sync_recent_ads: кабинет %s отвергнут — %s", account_id, exc)
            continue
        with fb_account(account_ctx):
            # --- Скачиваем свежие объявления кабинета (с обработкой FB ошибок) ---
            try:
                ads = _fetch_recent_ads(days=days)
            except FBApiError as exc:
                if _is_true_rate_limit_error(exc):
                    logger.warning("sync_recent_ads: act_%s rate-limit — %s", account_id, exc)
                    result["rate_limited"] = True
                    continue
                logger.error(
                    "sync_recent_ads: act_%s FB ошибка (status_code=%s) — %s",
                    account_id, exc.status_code, exc,
                )
                result["error"] = f"FB error {exc.status_code}"
                continue
            except Exception as exc:
                logger.error("sync_recent_ads: act_%s неожиданная ошибка — %s", account_id, exc)
                result["error"] = str(exc)
                continue

            if not ads:
                continue

            # --- Метрики + creative-поля (в контексте кабинета; глушат ошибки) ---
            ad_ids = [a["id"] for a in ads if a.get("id")]
            metrics = _fetch_lifetime_insights(ad_ids)
            creatives = _fetch_creative_fields_batch(ad_ids)

        # --- UPSERT каждого объявления (created_at пишется внутри _upsert_ad) ---
        conn = _get_connection()
        new_count = 0
        try:
            for ad in ads:
                ad_id = str(ad.get("id", "")).strip()
                if not ad_id:
                    continue
                is_new = _upsert_ad(
                    conn, ad, metrics.get(ad_id, {}), creatives.get(ad_id, {}), adset_map
                )
                if is_new:
                    new_count += 1
            conn.commit()
        finally:
            conn.close()

        result["fetched"] += len(ads)
        result["upserted"] += new_count

    logger.info(
        "sync_recent_ads: fetched=%d new=%d (days=%d, кабинетов=%d)",
        result["fetched"], result["upserted"], days, len(accounts),
    )
    return result
