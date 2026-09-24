"""
AMO Outcomes — привязка исходов AMO CRM к объявлениям creative_kb.

Алгоритм:
1. Строим fb_lookup ПРЯМЫМ dict comprehension из creative_kb (ad_name_lower → ad_id).
   НЕ используем amo._build_fb_lookup (она ждёт FB-объявления с ключом 'id').
2. Качаем лиды AMO порциями (окнами по batch_days дней, от months_back назад до сегодня)
   через integrations.amo.get_leads_window.
3. match_leads_to_ads + накопление метрик по всем окнам.
4. calc_ad_metrics (spend берём из ad_daily_metrics за тот же период, что revenue).
   Fallback на creative_kb.spend если нет дневных метрик за окно.
5. UPDATE creative_kb SET qual_leads/payments/revenue/romi/qual_pct/cpql/outcomes_matched_at
   только для совпавших ad_id.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from services.creative_intelligence import _get_connection
from integrations.amo import get_leads_window, match_leads_to_ads, calc_ad_metrics

logger = logging.getLogger(__name__)


def _build_windows(months_back: int, batch_days: int) -> list[tuple[int, int]]:
    """Строит список временных окон [(from_ts, to_ts), ...] от самого старого к свежему.

    months_back: сколько месяцев назад начинать (≈30.44 дней на месяц).
    batch_days: ширина каждого окна в днях.
    """
    now = datetime.now(tz=timezone.utc)
    # Начало диапазона — months_back месяцев назад
    start = now - timedelta(days=int(months_back * 30.44))

    windows: list[tuple[int, int]] = []
    cursor = start
    while cursor < now:
        window_end = min(cursor + timedelta(days=batch_days), now)
        windows.append((int(cursor.timestamp()), int(window_end.timestamp())))
        cursor = window_end

    return windows


def attach_amo_outcomes_increment(window_index: int, batch_days: int = 30) -> dict:
    """ОДНО окно AMO (helper внутри attach_amo_outcomes).

    window_index=0 — самое свежее окно (последние batch_days дней),
    1 — предыдущее, и т.д.

    Returns: {"window_index": int, "from": iso, "to": iso, "leads": N, "matched": M}.
    """
    now = datetime.now(tz=timezone.utc)
    # window_index=0 → последнее окно
    to_ts = int((now - timedelta(days=window_index * batch_days)).timestamp())
    from_ts = int((now - timedelta(days=(window_index + 1) * batch_days)).timestamp())

    leads = get_leads_window(from_ts, to_ts)

    # Строим fb_lookup из creative_kb для матчинга
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT ad_id, ad_name, spend FROM creative_kb WHERE ad_name != ''"
        ).fetchall()
    finally:
        conn.close()

    fb_lookup: dict[str, str] = {}
    known_ad_ids: set[str] = set()
    for r in rows:
        key = (r["ad_name"] or "").strip().lower()
        if key and key not in fb_lookup:
            fb_lookup[key] = r["ad_id"]
        known_ad_ids.add(r["ad_id"])

    matched = match_leads_to_ads(leads, fb_lookup, known_ad_ids)

    return {
        "window_index": window_index,
        "from": datetime.fromtimestamp(from_ts, tz=timezone.utc).isoformat(),
        "to": datetime.fromtimestamp(to_ts, tz=timezone.utc).isoformat(),
        "leads": len(leads),
        "matched": len(matched),
    }


def refresh_amo_payments_7d(now=None) -> dict:
    """ПОЛНЫЙ refresh source-specific AMO-оплат РОВНО за 7 календарных дней (Wave 3A).

    Отличие от attach_amo_outcomes (long-window ≈2 мес): узкое честное окно ровно
    7 суток (TZ CityA) и ТРАНЗАКЦИОННАЯ полная запись по ВСЕМ известным
    объявлениям с признаком полноты. Пишет колонки миграции 015:
    payments_amo_7d/revenue_amo_7d/amo_7d_window_from/amo_7d_window_to/
    amo_7d_synced_at/amo_7d_complete. НЕ трогает long-window payments/revenue/
    outcomes_matched_at (устраняет расхождение «текст 7 дней ≠ факт 2 мес», не
    переопределяя молча исторические поля).

    Окно — ровно 7 суток [window_from 00:00, window_to 00:00) CityA (см.
    services.cdp_payments.seven_day_window_local): нижняя граница включительно,
    верхняя (сегодня 00:00) — исключительно. get_leads_window фильтрует created_at
    ВКЛючительно с обеих сторон, поэтому верхняя граница передаётся как
    (window_to 00:00 − 1 сек) — лид ровно на верхней границе НЕ входит.

    Семантика полноты (fail-closed):
      - get_leads_window бросает (сеть/пагинация/timeout/парсинг) → НЕ помечаем
        complete, НЕ трогаем прошлый валидный снимок, synced_at не двигаем.
      - Успех: транзакционно для ВСЕХ известных ad_id — payments_amo_7d = число
        оплат за окно (0 если оплат нет = подтверждённый ноль), revenue_amo_7d,
        границы окна, synced_at, complete=1. Ошибка транзакции → rollback (нет
        смеси старых/новых строк), НЕ complete.

    Returns:
        {"window_from": iso, "window_to": iso, "ads_updated": int,
         "payments_total": int, "revenue_total_lcy": float,
         "complete": bool, "error": str | None}.
    """
    from services.cdp_payments import seven_day_window_local

    win = seven_day_window_local(now)
    synced_at = datetime.now(tz=timezone.utc).isoformat()
    result: dict = {
        "window_from": win["window_from"],
        "window_to": win["window_to"],
        "ads_updated": 0,
        "payments_total": 0,
        "revenue_total_lcy": 0.0,
        "complete": False,
        "error": None,
    }

    # Все известные объявления + fb_lookup (ad_name_lower → ad_id) для матчинга.
    try:
        conn = _get_connection()
    except RuntimeError as exc:
        logger.warning("refresh_amo_payments_7d: KB не инициализирована — %s", exc)
        result["error"] = str(exc)
        return result
    try:
        rows = conn.execute(
            "SELECT ad_id, ad_name FROM creative_kb WHERE ad_id != ''"
        ).fetchall()
    finally:
        conn.close()

    known_ad_ids: set[str] = set()
    all_ad_ids: list[str] = []
    fb_lookup: dict[str, str] = {}
    for r in rows:
        ad_id = r["ad_id"]
        known_ad_ids.add(ad_id)
        all_ad_ids.append(ad_id)
        key = (r["ad_name"] or "").strip().lower()
        if key and key not in fb_lookup:
            fb_lookup[key] = ad_id

    if not all_ad_ids:
        result["error"] = "нет известных объявлений в creative_kb"
        return result

    # ЕДИНСТВЕННОЕ окно 7 дней — get_leads_window сам пагинирует до конца; любое
    # исключение (частичный ответ/пагинация/timeout) → fail-closed, снимок цел.
    try:
        leads = get_leads_window(win["amo_from_ts"], win["amo_to_ts"])
    except Exception as exc:
        logger.warning(
            "refresh_amo_payments_7d: AMO недоступна для окна %s–%s — %s (fail-closed)",
            win["window_from"], win["window_to"], exc,
        )
        result["error"] = f"AMO недоступна: {exc}"
        return result

    matched = match_leads_to_ads(leads, fb_lookup, known_ad_ids)
    # payments/revenue не зависят от spend (см. integrations.amo.calc_ad_metrics) —
    # для честного 7d-среза расход не нужен, передаём пустой ad_spends.
    metrics = calc_ad_metrics(matched, {})

    # Транзакционная ПОЛНАЯ запись всем известным объявлениям (0 по умолчанию).
    try:
        conn = _get_connection()
    except RuntimeError as exc:
        logger.warning("refresh_amo_payments_7d: KB недоступна при записи — %s", exc)
        result["error"] = str(exc)
        return result

    updated = 0
    payments_total = 0
    revenue_total = 0.0
    try:
        conn.execute("BEGIN")
        for ad_id in all_ad_ids:
            m = metrics.get(ad_id) or {}
            p = int(m.get("payments", 0) or 0)
            rev = float(m.get("revenue", 0.0) or 0.0)
            conn.execute(
                """
                UPDATE creative_kb
                SET
                    payments_amo_7d    = ?,
                    revenue_amo_7d     = ?,
                    amo_7d_window_from = ?,
                    amo_7d_window_to   = ?,
                    amo_7d_synced_at   = ?,
                    amo_7d_complete    = 1
                WHERE ad_id = ?
                """,
                (p, rev, win["window_from"], win["window_to"], synced_at, ad_id),
            )
            updated += 1
            payments_total += p
            revenue_total += rev
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("refresh_amo_payments_7d: ошибка записи, откат — %s", exc)
        result["error"] = f"Ошибка записи в KB (откат): {exc}"
        return result
    finally:
        conn.close()

    result["ads_updated"] = updated
    result["payments_total"] = payments_total
    result["revenue_total_lcy"] = revenue_total
    result["complete"] = True
    return result


def attach_amo_outcomes(months_back: int = 12, batch_days: int = 30) -> dict:
    """Привязывает исходы AMO к объявлениям creative_kb по точному ad_name.

    Качает лиды AMO порциями (окнами по batch_days дней, от months_back назад до сегодня),
    матчит по точному fb_ad_name → ad_name, переиспользуя integrations.amo.match_leads_to_ads
    + calc_ad_metrics. Обновляет creative_kb.

    ВАЖНО (FB DEV + AMO лимиты): spend берём ИЗ creative_kb (колонка spend, уже залита
    бэкфиллом), FB API не дёргаем.

    ЗАФИКСИРОВАНО (конструирование lookup): lookup строим ПРЯМЫМ dict comprehension из
    creative_kb, БЕЗ приватной amo._build_fb_lookup (она ждёт FB-объявления с ключом 'id').

    Returns: {"windows": K, "leads_fetched": N, "ads_matched": M, "ads_updated": M}.
    Не падает если AMO недоступна — логирует warning, возвращает {"error": "...", ...}.
    """
    # Шаг 1: строим fb_lookup и ad_spends
    try:
        conn = _get_connection()
    except RuntimeError as exc:
        logger.warning("attach_amo_outcomes: KB не инициализирована — %s", exc)
        return {"error": str(exc), "ads_updated": 0}

    try:
        rows = conn.execute(
            "SELECT ad_id, ad_name, spend FROM creative_kb WHERE ad_name != ''"
        ).fetchall()
    finally:
        conn.close()

    # {ad_name_lower: ad_id} — первый матч побеждает (как в amo.py)
    fb_lookup: dict[str, str] = {}
    # known_ad_ids: множество ad_id из KB — для приоритетного матча по fb_ad_id
    known_ad_ids: set[str] = set()
    # kb_spends: {ad_id: lifetime_spend} — fallback если нет дневных метрик
    kb_spends: dict[str, float] = {}

    for r in rows:
        key = (r["ad_name"] or "").strip().lower()
        if key and key not in fb_lookup:
            fb_lookup[key] = r["ad_id"]
        known_ad_ids.add(r["ad_id"])
        kb_spends[r["ad_id"]] = r["spend"] or 0

    # Windowed spend: берём расход за тот же период, что revenue (months_back).
    # Дата начала окна совпадает с самым ранним окном запроса к AMO.
    window_start_date = (date.today() - timedelta(days=int(months_back * 30))).isoformat()

    try:
        conn = _get_connection()
        try:
            daily_rows = conn.execute(
                """
                SELECT ad_id, SUM(spend) AS windowed_spend
                FROM ad_daily_metrics
                WHERE date >= ?
                GROUP BY ad_id
                """,
                (window_start_date,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(
            "attach_amo_outcomes: не удалось получить windowed spend из ad_daily_metrics — %s."
            " Используем lifetime spend из creative_kb.", exc
        )
        daily_rows = []

    # Собираем windowed spend; для отсутствующих — fallback на lifetime из KB
    ad_spends: dict[str, float] = {}
    windowed_ad_ids: set[str] = set()
    for dr in daily_rows:
        ad_spends[dr["ad_id"]] = dr["windowed_spend"] or 0
        windowed_ad_ids.add(dr["ad_id"])

    # Fallback: объявления без дневных метрик за окно → lifetime spend из KB
    fallback_count = 0
    for ad_id, lifetime_spend in kb_spends.items():
        if ad_id not in windowed_ad_ids:
            ad_spends[ad_id] = lifetime_spend
            fallback_count += 1

    if fallback_count:
        logger.warning(
            "attach_amo_outcomes: для %d объявлений нет дневных метрик за окно %s — "
            "используется lifetime spend (fallback).",
            fallback_count,
            window_start_date,
        )

    # Шаг 2: скачиваем лиды окнами, ДЕДУПЛИЦИРУЕМ по lead_id и матчим ОДИН раз.
    #
    # ФИКС ДВОЙНОГО СЧЁТА: окна batch_days смежные, а filter[created_at][from/to]
    # в AMO включает обе границы — лид на стыке окон приходит ДВАЖДЫ. Раньше
    # каждое окно матчилось отдельно и revenue суммировался по окнам
    # (acc["revenue"] += ...), из-за чего один платёж засчитывался повторно и
    # завышал revenue/romi во всей системе. Теперь: собираем все лиды, оставляем
    # по одному на каждый lead_id, и считаем метрики ровно один раз.
    windows = _build_windows(months_back, batch_days)
    deduped_leads: dict[int, dict] = {}  # lead_id -> lead (последнее вхождение)
    total_leads_fetched = 0

    for from_ts, to_ts in windows:
        try:
            leads = get_leads_window(from_ts, to_ts)
        except Exception as exc:
            logger.warning(
                "attach_amo_outcomes: AMO недоступна для окна %s–%s — %s",
                from_ts, to_ts, exc,
            )
            return {"error": f"AMO недоступна: {exc}", "ads_updated": 0, "leads_fetched": 0}

        total_leads_fetched += len(leads)
        for lead in leads:
            lid = lead.get("id")
            if lid is None:
                continue
            # Один lead_id = один платёж: повторы из соседних окон не накапливаем
            deduped_leads[lid] = lead

    # match_leads_to_ads вызывается ОДИН раз на дедуп-списке — каждый лид учтён однократно
    # known_ad_ids передаётся для приоритетного матча по fb_ad_id (устраняет коллизию имён)
    matched_accumulated = match_leads_to_ads(list(deduped_leads.values()), fb_lookup, known_ad_ids)

    # Шаг 3: считаем финальные метрики через calc_ad_metrics
    if not matched_accumulated:
        return {
            "windows": len(windows),
            "leads_fetched": total_leads_fetched,
            "ads_matched": 0,
            "ads_updated": 0,
        }

    metrics = calc_ad_metrics(matched_accumulated, ad_spends)

    # Шаг 4: UPDATE creative_kb только для совпавших ad_id
    updated_count = 0
    try:
        conn = _get_connection()
        try:
            for ad_id, m in metrics.items():
                conn.execute(
                    """
                    UPDATE creative_kb
                    SET
                        qual_leads          = ?,
                        payments            = ?,
                        revenue             = ?,
                        romi                = ?,
                        qual_pct            = ?,
                        cpql                = ?,
                        outcomes_matched_at = datetime('now')
                    WHERE ad_id = ?
                    """,
                    (
                        m["qual_leads"],
                        m["payments"],
                        m["revenue"],
                        m["romi"],
                        m["qual_pct"],
                        m["cpql"],
                        ad_id,
                    ),
                )
                updated_count += 1
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("attach_amo_outcomes: ошибка при записи в KB — %s", exc)
        return {"error": f"Ошибка записи в KB: {exc}", "ads_updated": 0}

    return {
        "windows": len(windows),
        "leads_fetched": total_leads_fetched,
        "ads_matched": len(matched_accumulated),
        "ads_updated": updated_count,
    }
