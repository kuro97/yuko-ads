"""
Analyzer — ежедневный анализ объявлений по Decision Tree.

Алгоритм:
1. Тянет метрики из FB API (расход, лиды, CPL, CTR, CPM) за 7 дней
2. Сопоставляет с данными из AMO (квал%, ROMI, оплаты) — ручной ввод через UI
3. Применяет Decision Tree → ОТКЛЮЧИТЬ / ЖДАТЬ / ОСТАВИТЬ
4. Возвращает список рекомендаций (действия только после подтверждения)
"""

import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.fb_token_provider import get_fb_token, get_fb_account_id
from services.meta_lead_actions import parse_meta_lead_actions
from agent.fb_common import build_adset_map, get_all_ads, API, FBApiError, _throttled_get

import time as _time

logger = logging.getLogger(__name__)

# Кеш свежих статусов объявлений (отдельно от тяжёлого analytics-кеша).
# Статус — лёгкий запрос без insights, обновляем чаще (TTL 120с), чтобы
# приостановленные/удалённые объявления отображались правильно даже если
# метрики берутся из старого кеша.
_status_cache = {"data": {}, "ts": 0.0}
_STATUS_TTL = 120.0

# Кеш HD-превью: creative_id → url. Превью у креатива не меняется,
# поэтому держим постоянно И сохраняем на диск — переживает рестарт pm2.
# Экономит десятки FB-запросов при каждой загрузке аналитики/обзора.
_THUMB_CACHE_PATH = Path(__file__).parent.parent / "data" / "thumb_cache.json"


def _load_thumb_cache() -> dict:
    """Грузит кеш превью с диска при старте. При ошибке — пустой кеш."""
    try:
        if _THUMB_CACHE_PATH.exists():
            with open(_THUMB_CACHE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                logger.info("thumb_cache: загружено %d превью с диска", len(data))
                return data
    except Exception as e:
        logger.warning("thumb_cache: не удалось загрузить — %s", e)
    return {}


_thumb_cache: dict = _load_thumb_cache()


def _save_thumb_cache() -> None:
    """Атомарно сохраняет кеш превью на диск (tmp + replace)."""
    try:
        _THUMB_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _THUMB_CACHE_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_thumb_cache, f, ensure_ascii=False)
        tmp.replace(_THUMB_CACHE_PATH)
    except Exception as e:
        logger.warning("thumb_cache: не удалось сохранить — %s", e)


def _status_accounts() -> tuple[str, ...]:
    """Кабинеты статус-скана: все оффлайн из карты роутинга.

    Потребитель spend_refresh трактует «нет в выдаче = DELETED»: однокабинетный
    скан помечал бы DELETED все живые объявления второго кабинета (cabinet_b).
    Фолбэк при недоступной карте — дефолтный кабинет.
    """
    try:
        from services.launch_routing import accounts_to_scan

        accounts = accounts_to_scan()
        if accounts:
            return accounts
    except Exception as exc:  # noqa: BLE001 — деградация до старого охвата
        logger.warning("_status_accounts: карта роутинга недоступна — %s", exc)
    return (str(get_fb_account_id()).replace("act_", ""),)


def _fetch_account_statuses() -> dict | None:
    """Статусы ТЕКУЩЕГО (thread-контекст) кабинета. None = сбор неполный.

    Частичная выдача (обрыв пагинации, не-200) возвращает None, а не кусок:
    неполный скан у потребителя превращается в ложные DELETED.
    """
    statuses: dict[str, str] = {}
    url = f"{API}/act_{get_fb_account_id()}/ads"
    params = {
        "access_token": get_fb_token(),
        "fields": "id,effective_status",
        "effective_status": json.dumps([
            "ACTIVE", "PAUSED", "ADSET_PAUSED", "CAMPAIGN_PAUSED",
            "DISAPPROVED", "WITH_ISSUES", "PENDING_REVIEW",
        ]),
        "limit": 500,
    }
    resp = _throttled_get(url, params=params)
    if resp.status_code != 200:
        return None  # rate limit / ошибка
    data = resp.json()
    for a in data.get("data", []):
        statuses[a["id"]] = a.get("effective_status", "ACTIVE")
    pages = 0
    while data.get("paging", {}).get("next") and pages < 10:
        resp = _throttled_get(data["paging"]["next"])
        if resp.status_code != 200:
            return None  # обрыв пагинации = неполный скан
        data = resp.json()
        for a in data.get("data", []):
            statuses[a["id"]] = a.get("effective_status", "ACTIVE")
        pages += 1
    return statuses


def _fetch_statuses_now() -> dict:
    """Реальный (блокирующий) запрос статусов к FB. Обновляет кеш при успехе.
    Лёгкий запрос (без insights). Сбрасывает флаг refreshing в любом случае.

    Мультикабинетно: статусы сливаются по всем оффлайн-кабинетам карты
    роутинга. Отказ ЛЮБОГО кабинета = отдаём старый кеш целиком (fail-closed:
    неполный скан хуже устаревшего — он фабрикует DELETED).
    """
    try:
        from services.fb_token_provider import fb_account, offline_account_context

        merged: dict[str, str] = {}
        for account_id in _status_accounts():
            try:
                account_ctx = offline_account_context(account_id)
            except Exception as exc:  # noqa: BLE001 — незарегистрированный кабинет
                logger.warning(
                    "_fetch_statuses_now: кабинет %s отвергнут (%s) — отдаём кеш",
                    account_id,
                    exc,
                )
                return _status_cache["data"]
            with fb_account(account_ctx):
                got = _fetch_account_statuses()
            if got is None:
                logger.warning(
                    "_fetch_statuses_now: кабинет %s не отдал полный скан — отдаём кеш",
                    account_id,
                )
                return _status_cache["data"]
            merged.update(got)
        if merged:
            _status_cache["data"] = merged
            _status_cache["ts"] = _time.time()
        return merged or _status_cache["data"]
    except Exception as exc:
        logger.warning("get_fresh_ad_statuses failed: %s", exc)
        return _status_cache["data"]
    finally:
        _status_cache["refreshing"] = False


def get_statuses_age_seconds() -> float | None:
    """Возраст кеша статусов в секундах (now - _status_cache['ts']).
    Возвращает None если кеш пустой или ts == 0 (никогда не обновлялся)."""
    ts = _status_cache.get("ts", 0.0)
    if not ts:
        return None
    return _time.time() - ts


def get_fresh_ad_statuses(force: bool = False) -> dict:
    """{ad_id: effective_status}. Stale-while-revalidate, чтобы НЕ блокировать
    запрос аналитики на медленном (5500 объявлений) обновлении статусов:
      • свежий кеш (<120с) → сразу;
      • устаревший, но есть старые → отдаём старые + обновляем в ФОНЕ;
      • данных нет вообще (или force) → блокирующий запрос."""
    import threading
    now = _time.time()
    if not force and _status_cache["data"] and (now - _status_cache["ts"]) < _STATUS_TTL:
        return _status_cache["data"]
    # Есть старые данные → не блокируем: фоновое обновление + отдаём старое сразу
    if not force and _status_cache["data"]:
        if not _status_cache.get("refreshing"):
            _status_cache["refreshing"] = True
            threading.Thread(target=_fetch_statuses_now, daemon=True).start()
        return _status_cache["data"]
    # Первый раз (нет данных) или принудительно — блокирующий запрос
    return _fetch_statuses_now()


def refresh_statuses_in_place(ads: list[dict]) -> None:
    """Перезаписывает effective_status в списке ads актуальными данными FB.
    Объявление, которого нет в свежем списке (удалено/архивировано) —
    помечается effective_status='DELETED'."""
    fresh = get_fresh_ad_statuses()
    if not fresh:
        return  # не смогли получить — оставляем как было
    for ad in ads:
        real = fresh.get(ad.get("id"))
        if real:
            ad["effective_status"] = real
            ad["status"] = real
            ad["is_paused"] = (real != "ACTIVE")
        else:
            # Не вернулось из FB → удалено/архивировано
            ad["effective_status"] = "DELETED"
            ad["status"] = "DELETED"
            ad["is_paused"] = True


def get_ads_with_metrics(date_from: str = None, date_to: str = None, light: bool = False) -> list[dict]:
    """
    Получает все объявления с метриками за период.
    date_from, date_to: строки YYYY-MM-DD. По умолчанию — последние 7 дней.
    light=True: только базовые поля + spend/leads (без креативов и HD-превью).
        Используется для предыдущего периода в обзоре, где нужны лишь дельты —
        экономит десятки FB-запросов и не упирается в rate limit.
    """
    if not date_to:
        date_to = datetime.now().strftime("%Y-%m-%d")
    if not date_from:
        date_from = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    adset_map = build_adset_map()

    # В light-режиме не тянем тяжёлые поля креатива — только base_fields.
    # adset{optimization_goal,destination_type} — чтобы отличать лид-формы от трафика на сайт.
    # destination_type=ON_AD означает FB Instant Form (форма прямо в объявлении).
    extra = "" if light else "campaign{name},adset{name,optimization_goal,destination_type},creative{id,thumbnail_url,image_url,object_story_spec{video_data{image_url},link_data{picture,child_attachments{picture}}}}"

    # Параллельно: объявления + метрики (2 запроса одновременно, ~700мс вместо ~1400мс)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ads_future = pool.submit(get_all_ads, adset_map, extra_fields=extra)
        insights_future = pool.submit(_get_account_insights, date_from, date_to)
        ads_by_id = ads_future.result()
        insights = insights_future.result()

    # 3. Объединяем
    all_ads = []
    for ad_id, ad in ads_by_id.items():
        metrics = insights.get(ad_id, {})
        created = datetime.fromisoformat(ad["created_time"].replace("+0000", "+00:00"))
        days_running = (datetime.now(created.tzinfo) - created).days

        all_ads.append({
            "id": ad_id,
            "name": ad["name"],
            "status": ad["status"],
            "effective_status": ad.get("effective_status", ad["status"]),
            "created_time": ad["created_time"],
            "days_running": days_running,
            "city": ad["city"],
            "adset_type": ad["adset_type"],
            # В light-режиме adset_id не тянется из FB — используем .get()
            "adset_id": ad.get("adset_id"),
            "campaign_name": ad.get("campaign_name", ""),
            "adset_name": ad.get("adset_name", ""),
            # leadform — лид-форма внутри FB; site — ведёт на сайт (трафик/конверсии)
            "ad_objective": ad.get("ad_objective", ""),
            "creative_id": ad.get("creative_id", ""),
            "thumbnail_url": ad.get("thumbnail_url", ""),
            # FB метрики
            "spend": metrics.get("spend", 0),
            "leads": metrics.get("leads", 0),
            "cpl": metrics.get("cpl", 0),
            "ctr": metrics.get("ctr", 0),
            "cpm": metrics.get("cpm", 0),
            "impressions": metrics.get("impressions", 0),
            "clicks": metrics.get("clicks", 0),
            # AMO данные (заполняются позже)
            "qual_pct": None,
            "romi": None,
            "payments": None,
        })

    # Дозагрузка HD превью для объявлений с маленьким thumbnail
    # (в light-режиме креативов нет — пропускаем)
    if not light:
        _upgrade_thumbnails(all_ads)

    return all_ads


def _upgrade_thumbnails(ads: list[dict]) -> None:
    """Batch-запрос за HD превью для ВСЕХ реклам с creative_id.
    facebook.com/ads/image = HD (1080px), оставляем.
    Остальные (scontent, external) — пробуем заменить на HD."""
    # Сначала пробуем из памяти — превью не меняется, повторно не тянем
    cached_hits = 0
    pending = {}
    for a in ads:
        cid = a.get("creative_id")
        if not cid or "facebook.com/ads/image" in a.get("thumbnail_url", ""):
            continue
        if cid in _thumb_cache:
            a["thumbnail_url"] = _thumb_cache[cid]
            cached_hits += 1
        else:
            pending[cid] = a

    need_upgrade = pending
    if not need_upgrade:
        if cached_hits:
            logger.info("Thumbnails: %d из кеша, дозагрузка не нужна", cached_hits)
        return

    logger.info("Upgrading %d thumbnails to HD (%d взято из кеша)...", len(need_upgrade), cached_hits)
    from services.fb_token_provider import get_fb_token
    cids = list(need_upgrade.keys())
    upgraded = 0
    for i in range(0, len(cids), 50):
        batch = cids[i:i+50]
        try:
            resp = _throttled_get("https://graph.facebook.com/v21.0/", params={
                "access_token": get_fb_token(),
                "ids": ",".join(batch),
                "fields": "object_story_spec{video_data{image_url},link_data{picture,child_attachments{picture}}}",
            })
            if not resp.ok:
                logger.warning("HD batch %d failed: %s", i, resp.status_code)
                continue
            for cid, data in resp.json().items():
                spec = data.get("object_story_spec") or {}
                vd = (spec.get("video_data") or {}).get("image_url")
                ld = (spec.get("link_data") or {}).get("picture")
                children = (spec.get("link_data") or {}).get("child_attachments", [])
                child = children[0].get("picture") if children else None
                hd = vd or ld or child
                if hd and cid in need_upgrade:
                    need_upgrade[cid]["thumbnail_url"] = hd
                    _thumb_cache[cid] = hd  # запоминаем — больше не тянем
                    upgraded += 1
        except Exception as e:
            logger.warning("HD thumbnail batch failed: %s", e)
    logger.info("Upgraded %d/%d thumbnails to HD", upgraded, len(need_upgrade))
    if upgraded:
        _save_thumb_cache()  # переживёт рестарт — больше не тянем


# Кеш кампаний кабинета: {account_id: {"data": [...], "ts": float}}
# TTL 1 час — кампании создаются редко, ежечасное обновление достаточно.
_campaigns_cache: dict = {}
_CAMPAIGNS_TTL = 3600.0


def _is_reduce_data_error(resp) -> bool:
    """Проверяет, является ли ответ ошибкой FB 'reduce the amount of data' (код 1)."""
    if resp.status_code == 500:
        try:
            err = (resp.json().get("error") or {})
            if err.get("code") == 1 and "reduce" in (err.get("message") or "").lower():
                return True
        except Exception:
            pass
    # FB иногда шлёт через 400 с кодом 1
    if resp.status_code in (400, 500):
        try:
            err = (resp.json().get("error") or {})
            code = err.get("code", 0)
            msg = (err.get("message") or "").lower()
            return code == 1 and "reduce" in msg
        except Exception:
            pass
    return False


def _get_account_campaigns() -> list[dict]:
    """Возвращает список кампаний кабинета {id, name}.
    Кешируется в памяти на 1 час — кампании редко меняются.
    """
    account_id = get_fb_account_id()
    now = _time.time()
    cached = _campaigns_cache.get(account_id)
    if cached and (now - cached["ts"]) < _CAMPAIGNS_TTL:
        return cached["data"]

    campaigns = []
    resp = _throttled_get(
        f"{API}/act_{account_id}/campaigns",
        params={
            "access_token": get_fb_token(),
            "fields": "id,name",
            "limit": 200,
        },
    )
    if resp.status_code != 200:
        raise FBApiError(f"FB API campaigns ошибка: {resp.status_code} {resp.text[:200]}", resp.status_code)

    data = resp.json()
    campaigns.extend(data.get("data", []))
    # Пагинация кампаний
    while data.get("paging", {}).get("next"):
        resp = _throttled_get(data["paging"]["next"])
        if resp.status_code != 200:
            break
        data = resp.json()
        campaigns.extend(data.get("data", []))

    _campaigns_cache[account_id] = {"data": campaigns, "ts": now}
    return campaigns


def _fetch_insights_paginated(params: dict) -> dict:
    """Выполняет insights-запрос с пагинацией, возвращает {ad_id: metrics}.
    При ошибке пагинации — возвращает уже собранные данные (частичный результат).
    """
    insights = {}
    resp = _throttled_get(
        f"{API}/act_{get_fb_account_id()}/insights",
        params=params,
    )
    if resp.status_code != 200:
        raise FBApiError(f"FB API insights ошибка: {resp.status_code} {resp.text[:200]}", resp.status_code)

    data = resp.json()
    for row in data.get("data", []):
        insights[row["ad_id"]] = _parse_insight_row(row)

    # Пагинация (при ошибке — возвращаем что уже получили)
    while "paging" in data and "next" in data.get("paging", {}):
        resp = _throttled_get(data["paging"]["next"])
        if resp.status_code != 200:
            logger.warning("FB API insights пагинация %d — возвращаем %d записей", resp.status_code, len(insights))
            break
        data = resp.json()
        for row in data.get("data", []):
            insights[row["ad_id"]] = _parse_insight_row(row)

    return insights


def _get_account_insights(date_from: str, date_to: str) -> dict:
    """Метрики всех объявлений за период.

    Стратегия деградации:
    1. Попытка 1 — полный account-level запрос (быстрый путь).
    2. Если FB вернул 'reduce the amount of data' (code 1) — переходим на
       чанки по кампаниям: для каждой кампании отдельный insights-запрос
       с filtering по campaign.id. Результаты сливаются в один словарь.
    3. Если отдельный чанк упал — пропускаем с logger.warning (лучше
       90% данных, чем 0%).
    """
    base_params = {
        "access_token": get_fb_token(),
        "level": "ad",
        "fields": "ad_id,spend,impressions,clicks,ctr,cpm,actions",
        "time_range": json.dumps({"since": date_from, "until": date_to}),
        "limit": 200,
    }

    # Попытка 1: полный запрос
    resp = _throttled_get(
        f"{API}/act_{get_fb_account_id()}/insights",
        params=base_params,
    )

    if resp.status_code == 200:
        # Успех — обрабатываем с пагинацией
        insights = {}
        data = resp.json()
        for row in data.get("data", []):
            insights[row["ad_id"]] = _parse_insight_row(row)
        while "paging" in data and "next" in data.get("paging", {}):
            resp = _throttled_get(data["paging"]["next"])
            if resp.status_code != 200:
                logger.warning("FB API insights пагинация %d — возвращаем %d записей", resp.status_code, len(insights))
                break
            data = resp.json()
            for row in data.get("data", []):
                insights[row["ad_id"]] = _parse_insight_row(row)
        return insights

    if not _is_reduce_data_error(resp):
        # Другая ошибка — бросаем исключение как раньше
        raise FBApiError(f"FB API insights ошибка: {resp.status_code} {resp.text[:200]}", resp.status_code)

    # Попытка 2: чанки по кампаниям
    campaigns = _get_account_campaigns()
    logger.warning(
        "insights full-запрос отбит FB (reduce data) — переходим на чанки по кампаниям: %d кампаний",
        len(campaigns),
    )

    all_insights: dict = {}
    for campaign in campaigns:
        campaign_id = campaign["id"]
        chunk_params = {
            **base_params,
            "filtering": json.dumps([
                {"field": "campaign.id", "operator": "IN", "value": [campaign_id]}
            ]),
        }
        try:
            chunk_insights = _fetch_insights_paginated(chunk_params)
            all_insights.update(chunk_insights)
        except FBApiError as exc:
            logger.warning(
                "insights чанк кампании %s упал (%s) — пропускаем, продолжаем",
                campaign_id, exc,
            )

    return all_insights


def _parse_insight_row(row: dict) -> dict:
    """Парсит одну строку инсайтов из FB API."""
    spend = float(row.get("spend", 0))
    impressions = int(row.get("impressions", 0))
    clicks = int(row.get("clicks", 0))
    ctr = float(row.get("ctr", 0))
    cpm = float(row.get("cpm", 0))

    lead_result = parse_meta_lead_actions(row.get("actions"))
    if lead_result.canonical_total is None:
        raise ValueError(f"invalid Meta lead actions: {','.join(lead_result.problems)}")
    if lead_result.problems:
        logger.warning("Meta lead components расходятся: %s", ",".join(lead_result.problems))
    leads = lead_result.canonical_total

    cpl = spend / leads if leads > 0 else 0

    return {
        "spend": round(spend, 2),
        "leads": leads,
        "cpl": round(cpl, 2),
        "ctr": round(ctr, 2),
        "cpm": round(cpm, 2),
        "impressions": impressions,
        "clicks": clicks,
    }


def get_daily_insights(ad_ids: list[str], date_from: str, date_to: str) -> dict:
    """Daily breakdown метрик по объявлениям для графиков.

    Возвращает: {ad_id: [{date, spend, leads, cpl}, ...], ...}
    """
    if not ad_ids:
        return {}

    result = {}
    resp = _throttled_get(
        f"{API}/act_{get_fb_account_id()}/insights",
        params={
            "access_token": get_fb_token(),
            "level": "ad",
            "time_increment": 1,
            "fields": "ad_id,date_start,spend,actions",
            "time_range": json.dumps({"since": date_from, "until": date_to}),
            "filtering": json.dumps([{"field": "ad.id", "operator": "IN", "value": ad_ids}]),
            "limit": 200,
        },
    )
    if resp.status_code != 200:
        raise FBApiError(f"FB API daily insights ошибка: {resp.status_code} {resp.text[:200]}", resp.status_code)

    data = resp.json()
    _process_daily_rows(data.get("data", []), result)

    # Пагинация (при ошибке — возвращаем что есть)
    while "paging" in data and "next" in data.get("paging", {}):
        resp = _throttled_get(data["paging"]["next"])
        if resp.status_code != 200:
            logger.warning("FB API daily пагинация %d — возвращаем что есть", resp.status_code)
            break
        data = resp.json()
        _process_daily_rows(data.get("data", []), result)

    # Сортируем по дате
    for ad_id in result:
        result[ad_id].sort(key=lambda x: x["date"])

    return result


def _process_daily_rows(rows: list, result: dict):
    """Обрабатывает строки daily insights и добавляет в result."""
    for row in rows:
        ad_id = row["ad_id"]
        parsed = _parse_insight_row(row)
        if ad_id not in result:
            result[ad_id] = []
        result[ad_id].append({
            "date": row["date_start"],
            "spend": parsed["spend"],
            "leads": parsed["leads"],
            "cpl": parsed["cpl"],
        })


# Пороги по умолчанию для Decision Tree
DEFAULT_THRESHOLDS = {
    "max_cpl": 30,
    "min_leads_to_judge": 5,
    "min_days_no_leads": 7,
    "min_qual_pct": 20,
    "min_romi": 200,
    "min_leads_for_stats": 10,
}


def apply_decision_tree(ad: dict, thresholds: dict | None = None) -> dict:
    """
    Применяет Decision Tree к одному объявлению.
    Возвращает рекомендацию с причиной.

    thresholds — опциональный словарь переопределений порогов.
    Незаданные ключи берутся из DEFAULT_THRESHOLDS.

    Decision Tree:
    0. ROMI >= min_romi% → ОСТАВИТЬ (абсолютный приоритет, какие бы цифры не были)
    1. >min_days_no_leads дней, 0 лидов → ОТКЛЮЧИТЬ
    2. CPL >= max_cpl AND leads >= min_leads_to_judge → ОТКЛЮЧИТЬ
    3. Leads < min_leads_for_stats:
       - Высокий CPL сам по себе не отключает до min_leads_to_judge
       - При leads >= min_leads_to_judge: >min_days_no_leads дней,
         0 оплат, квал < min_qual_pct% → ОТКЛЮЧИТЬ
       - Квал >= min_qual_pct%, нет оплат → ЖДАТЬ
       - Иначе → ЖДАТЬ (копим данные)
    4. Leads >= min_leads_for_stats:
       - ROMI >= min_romi% → ОСТАВИТЬ
       - Квал >= min_qual_pct%, нет оплат → ЖДАТЬ
       - ROMI < min_romi% → ОТКЛЮЧИТЬ
       - Квал < min_qual_pct% AND ROMI < min_romi% → ОТКЛЮЧИТЬ
    """
    # Сливаем дефолтные пороги с кастомными (кастомные приоритетнее)
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    days = ad["days_running"]
    leads = ad["leads"]
    cpl = ad["cpl"]
    spend = float(ad.get("spend", 0) or 0)
    qual = ad.get("qual_pct")
    romi = ad.get("romi")
    payments = ad.get("payments")

    # Правило 0: ROMI >= min_romi% — всегда оставляем (абсолютный приоритет)
    if romi is not None and romi >= t["min_romi"]:
        return {"action": "ОСТАВИТЬ", "reason": f"ROMI {romi}% — отлично"}

    # Правило 0.5: недостаточно данных для вердикта (расход < $15)
    # Meta могла не открутить бюджет из-за оптимизации, узкой аудитории, rate limits
    # $15 — константа, намеренно не вынесена в пороги
    if spend < 15:
        return {"action": "ЖДАТЬ", "reason": f"Мало данных (расход ${spend:.2f} < $15)"}

    # Правило 1: >min_days_no_leads дней, 0 лидов, расход >= $15 (значимый).
    # thresholds.zero_leads_rule_enabled=false отдаёт этот класс раннему стопу по
    # расходу (services/early_kill.py).
    if days > t["min_days_no_leads"] and leads == 0 and t.get("zero_leads_rule_enabled", True) is not False:
        return {"action": "ОТКЛЮЧИТЬ", "reason": f"Работает {days} дней, 0 лидов, расход ${spend:.0f}"}

    # Правило 2: CPL >= max_cpl и есть хотя бы min_leads_to_judge лидов
    if cpl >= t["max_cpl"] and leads >= t["min_leads_to_judge"]:
        return {"action": "ОТКЛЮЧИТЬ", "reason": f"CPL ${cpl} >= ${t['max_cpl']}, {leads} лидов"}

    # Правило 3: Мало лидов (< min_leads_for_stats)
    if leads < t["min_leads_for_stats"]:
        if leads >= t["min_leads_to_judge"] and cpl >= t["max_cpl"]:
            return {"action": "ОТКЛЮЧИТЬ", "reason": f"Все лиды по ${cpl}+"}

        if (
            leads >= t["min_leads_to_judge"]
            and days > t["min_days_no_leads"]
            and payments == 0
            and qual is not None
            and qual < t["min_qual_pct"]
        ):
            return {"action": "ОТКЛЮЧИТЬ", "reason": f"Работает {days} дн, квал {qual}%, 0 оплат"}

        if qual is not None and qual >= t["min_qual_pct"] and (payments is None or payments == 0):
            return {"action": "ЖДАТЬ", "reason": f"Квал {qual}%, ждём оплат"}

        if qual is None:
            return {"action": "ЖДАТЬ", "reason": "Нет данных по квалам — нужен ввод из AMO"}

        return {"action": "ЖДАТЬ", "reason": "Копим данные"}

    # Правило 4: Достаточно лидов (>= min_leads_for_stats)
    if romi is not None and romi >= t["min_romi"]:
        return {"action": "ОСТАВИТЬ", "reason": f"ROMI {romi}% — отлично"}

    if qual is not None and qual >= t["min_qual_pct"] and (payments is None or payments == 0):
        return {"action": "ЖДАТЬ", "reason": f"Квал {qual}%, ждём оплат"}

    if romi is not None and romi < t["min_romi"]:
        return {"action": "ОТКЛЮЧИТЬ", "reason": f"ROMI {romi}% < {t['min_romi']}%"}

    if qual is not None and qual < t["min_qual_pct"]:
        return {"action": "ОТКЛЮЧИТЬ", "reason": f"Квал {qual}%, ROMI низкий"}

    return {"action": "ЖДАТЬ", "reason": "Нет данных по ROMI/квалам — нужен ввод"}


def analyze_all(date_from: str = None, date_to: str = None, thresholds: dict = None, include_paused: bool = False) -> list[dict]:
    """
    Полный анализ: получаем метрики + применяем Decision Tree.
    Возвращает список объявлений с рекомендациями.
    """
    ads = get_ads_with_metrics(date_from, date_to)
    results = []

    for ad in ads:
        eff_status = ad.get("effective_status", ad["status"])
        if eff_status != "ACTIVE":
            # Показываем PAUSED если запрошены отключённые
            if include_paused and eff_status == "PAUSED":
                ad["recommendation"] = "ВЕРНУТЬ"
                ad["reason"] = "На паузе — проверьте ROMI"
                ad["is_paused"] = True
                results.append(ad)
            continue
        decision = apply_decision_tree(ad, thresholds)
        ad["recommendation"] = decision["action"]
        ad["reason"] = decision["reason"]
        results.append(ad)

    return results


def _read_ad_status(ad_id: str) -> str | None:
    """Читает актуальный status объявления из FB (одиночный GET).
    Возвращает 'ACTIVE'/'PAUSED'/... или None при ошибке."""
    try:
        resp = _throttled_get(
            f"{API}/{ad_id}",
            params={"access_token": get_fb_token(), "fields": "status"},
        )
        if resp.status_code == 200:
            return resp.json().get("status")
        logger.error("_read_ad_status %s: FB %s — %s", ad_id, resp.status_code, resp.text[:300])
    except Exception as exc:
        logger.error("_read_ad_status %s: исключение — %s", ad_id, exc)
    return None


def save_decision(ad_id: str, ad_name: str, action: str, reason: str,
                  confirmed_by: str = "user", effect_id: str | None = None,
                  projection_kind: str | None = None,
                  projection_payload: dict | None = None) -> bool:
    """Gateway-effect пишет в SQLite UoW; legacy запись остаётся JSON."""
    if effect_id is not None:
        from agent.database import save_decision as save_gateway_decision

        return save_gateway_decision(
            ad_id,
            ad_name,
            action,
            reason,
            confirmed_by,
            effect_id=effect_id,
            projection_kind=projection_kind,
            projection_payload=projection_payload,
        )
    journal_path = Path(__file__).parent.parent / "data" / "auto_actions.json"
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    entries = []
    if journal_path.exists():
        entries = json.loads(journal_path.read_text(encoding="utf-8"))

    if effect_id is not None and any(entry.get("effect_id") == effect_id for entry in entries):
        return False

    entries.insert(0, {
        "timestamp": datetime.now().isoformat(),
        "ad_id": ad_id,
        "ad_name": ad_name,
        "action": action,
        "reason": reason,
        "confirmed_by": confirmed_by,
        "effect_id": effect_id,
    })

    entries = entries[:500]
    journal_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Портфельный слой — apply_portfolio_decisions
# ---------------------------------------------------------------------------

# Веса составной оценки портфеля. Сумма не обязана быть 1 — score относительный внутри группы.
PORTFOLIO_WEIGHTS = {
    "romi": 0.45,   # ROMI важнее всего (деньги)
    "qual": 0.30,   # квал% — качество лида
    "cpl": 0.25,    # CPL — стоимость (инвертируется: дешевле = лучше)
}

# Дополнительные пороги портфельного слоя (мержатся поверх DEFAULT_THRESHOLDS внутри функции)
PORTFOLIO_DEFAULTS = {
    "portfolio_min_spend": 15.0,      # минимум данных: расход $, ниже — не отключаем
    "portfolio_min_score_gap": 0.15,  # разрыв score между лучшим и кандидатом
}


def _portfolio_group_key(ad: dict) -> tuple:
    """Ключ группы: (город, adset_type L2/L1, ad_objective). Пустые поля → '' (отдельная группа)."""
    return (
        ad.get("city") or "",
        ad.get("adset_type") or "",
        ad.get("ad_objective") or "",
    )


def _portfolio_score(ad: dict, group_stats: dict) -> tuple:
    """Составная относительная оценка объявления внутри группы.

    Возвращает (score, has_amo):
      score   — float, чем БОЛЬШЕ тем ЛУЧШЕ объявление;
      has_amo — есть ли у объявления AMO-данные (romi/qual_pct не None).

    Метрики нормируются относительно мин/макс В ГРУППЕ (min-max нормализация в [0,1]),
    поэтому абсолютные пороги settings тут НЕ участвуют — оценка чисто относительная.
    Для объявлений БЕЗ AMO (romi=None, qual_pct=None) — fallback: score только по
    CPL (инверт) и CTR; такие объявления оцениваются в отдельной шкале и НЕ сравниваются
    напрямую с теми, у кого есть AMO (см. правила в apply_portfolio_decisions).
    """
    romi = ad.get("romi")
    qual_pct = ad.get("qual_pct")
    cpl = float(ad.get("cpl") or 0)
    ctr = float(ad.get("ctr") or 0)

    has_amo = (romi is not None) or (qual_pct is not None)

    def _norm(val, min_v, max_v) -> float:
        """Min-max нормализация; если диапазон нулевой — нейтрально 0.5."""
        if max_v == min_v:
            return 0.5
        return (val - min_v) / (max_v - min_v)

    if has_amo:
        stats = group_stats.get("amo", {})
        # Нормализуем ROMI (None заменяем нейтральным 0.5)
        if romi is not None:
            romi_norm = _norm(romi, stats.get("romi_min", romi), stats.get("romi_max", romi))
        else:
            romi_norm = 0.5
        # Нормализуем qual_pct (None заменяем нейтральным 0.5)
        if qual_pct is not None:
            qual_norm = _norm(qual_pct, stats.get("qual_min", qual_pct), stats.get("qual_max", qual_pct))
        else:
            qual_norm = 0.5
        # CPL инвертируем (меньше = лучше)
        cpl_norm = 1 - _norm(cpl, stats.get("cpl_min", cpl), stats.get("cpl_max", cpl))

        score = (
            PORTFOLIO_WEIGHTS["romi"] * romi_norm
            + PORTFOLIO_WEIGHTS["qual"] * qual_norm
            + PORTFOLIO_WEIGHTS["cpl"] * cpl_norm
        )
    else:
        # Fallback без AMO: CPL инверт + CTR
        stats = group_stats.get("no_amo", {})
        cpl_norm = 1 - _norm(cpl, stats.get("cpl_min", cpl), stats.get("cpl_max", cpl))
        ctr_norm = _norm(ctr, stats.get("ctr_min", ctr), stats.get("ctr_max", ctr))
        score = 0.6 * cpl_norm + 0.4 * ctr_norm

    return (score, has_amo)


def _compute_group_stats(ads: list) -> dict:
    """Считает min/max по каждой метрике для двух подгрупп (AMO и без AMO).

    Возвращает:
    {
        "amo":    {"romi_min": .., "romi_max": .., "qual_min": .., "qual_max": .., "cpl_min": .., "cpl_max": ..},
        "no_amo": {"cpl_min": ..,  "cpl_max": ..,  "ctr_min": ..,  "ctr_max": ..},
    }
    """
    amo_ads = [a for a in ads if (a.get("romi") is not None) or (a.get("qual_pct") is not None)]
    no_amo_ads = [a for a in ads if (a.get("romi") is None) and (a.get("qual_pct") is None)]

    def _minmax(vals):
        """Возвращает (min, max) для непустого списка или (0, 0)."""
        filtered = [v for v in vals if v is not None]
        if not filtered:
            return (0.0, 0.0)
        return (min(filtered), max(filtered))

    stats: dict = {"amo": {}, "no_amo": {}}

    if amo_ads:
        romi_min, romi_max = _minmax([a.get("romi") for a in amo_ads])
        qual_min, qual_max = _minmax([a.get("qual_pct") for a in amo_ads])
        cpl_min, cpl_max = _minmax([float(a.get("cpl") or 0) for a in amo_ads])
        stats["amo"] = {
            "romi_min": romi_min, "romi_max": romi_max,
            "qual_min": qual_min, "qual_max": qual_max,
            "cpl_min": cpl_min,   "cpl_max": cpl_max,
        }

    if no_amo_ads:
        cpl_min, cpl_max = _minmax([float(a.get("cpl") or 0) for a in no_amo_ads])
        ctr_min, ctr_max = _minmax([float(a.get("ctr") or 0) for a in no_amo_ads])
        stats["no_amo"] = {
            "cpl_min": cpl_min, "cpl_max": cpl_max,
            "ctr_min": ctr_min, "ctr_max": ctr_max,
        }

    return stats


def apply_portfolio_decisions(ads: list, thresholds: dict | None = None) -> list:
    """Портфельный слой поверх apply_decision_tree.

    Группирует активные объявления по (city, adset_type, ad_objective), ранжирует
    внутри группы по _portfolio_score и переписывает recommendation+reason по
    железным правилам. Возвращает ТОТ ЖЕ список (мутирует элементы).

    Не-активные (effective_status != 'ACTIVE') не трогает — у них recommendation
    уже выставлен вызывающим кодом (ВЕРНУТЬ и т.п.).

    thresholds — словарь переопределений; незаданные ключи берутся из
    DEFAULT_THRESHOLDS + PORTFOLIO_DEFAULTS.
    """
    if not ads:
        return ads

    # Сливаем пороги: дефолты → портфельные → пользовательские
    t = {**DEFAULT_THRESHOLDS, **PORTFOLIO_DEFAULTS, **(thresholds or {})}

    min_spend = float(t.get("portfolio_min_spend", 15.0))
    min_score_gap = float(t.get("portfolio_min_score_gap", 0.15))
    min_leads = int(t.get("min_leads_to_judge", 5))

    # Отбираем только активные
    active = [a for a in ads if a.get("effective_status") == "ACTIVE"]

    if not active:
        return ads

    # Группировка по (city, adset_type, ad_objective)
    groups: dict = {}
    for ad in active:
        key = _portfolio_group_key(ad)
        groups.setdefault(key, []).append(ad)

    for key, group_ads in groups.items():
        city = key[0]
        adset_type = key[1]
        n = len(group_ads)

        # Считаем group_stats по ДВУМ подгруппам (AMO / без AMO)
        group_stats = _compute_group_stats(group_ads)

        # Считаем score для каждого объявления
        scored = []
        for ad in group_ads:
            score, has_amo = _portfolio_score(ad, group_stats)
            scored.append((ad, score, has_amo))

        # Группа из 1 активного — никогда ОТКЛЮЧИТЬ
        if n == 1:
            ad, score, has_amo = scored[0]
            if ad.get("recommendation") == "ОТКЛЮЧИТЬ":
                ad["recommendation"] = "ОСТАВИТЬ"
                ad["reason"] = "Последнее в группе — держим"
            continue

        # Ранжируем по убыванию score (лучшие первыми) ВНУТРИ каждой шкалы
        # Шкалы: AMO и без AMO — не смешиваем при сравнении
        amo_scored = sorted(
            [(ad, sc, ha) for ad, sc, ha in scored if ha],
            key=lambda x: x[1], reverse=True,
        )
        no_amo_scored = sorted(
            [(ad, sc, ha) for ad, sc, ha in scored if not ha],
            key=lambda x: x[1], reverse=True,
        )

        # Присваиваем rank в своей шкале и определяем best_score
        # Rank 1 = лучший
        def _assign_ranks(ranked_list):
            """Возвращает список (ad, rank, score, best_score)."""
            result = []
            n_sub = len(ranked_list)
            best_score = ranked_list[0][1] if ranked_list else 0.0
            for rank_idx, (ad, sc, _) in enumerate(ranked_list, start=1):
                result.append((ad, rank_idx, sc, best_score, n_sub))
            return result

        amo_ranked = _assign_ranks(amo_scored) if amo_scored else []
        no_amo_ranked = _assign_ranks(no_amo_scored) if no_amo_scored else []

        # Применяем железные правила для каждого объявления
        all_ranked = amo_ranked + no_amo_ranked

        for ad, rank, score, best_score, n_sub in all_ranked:
            absolute_rec = ad.get("recommendation", "ЖДАТЬ")

            # Правило: absolute ОСТАВИТЬ — не понижаем
            if absolute_rec == "ОСТАВИТЬ":
                continue

            spend = float(ad.get("spend") or 0)
            leads = int(ad.get("leads") or 0)

            # Лучший в своей шкале (rank 1)
            if rank == 1:
                if absolute_rec == "ОТКЛЮЧИТЬ":
                    # Нельзя отключать лучшего в группе
                    ad["recommendation"] = "ОСТАВИТЬ"
                    ad["reason"] = f"Лучшее в группе ({city}/{adset_type})"
                # Иначе оставляем absolute (ЖДАТЬ и т.п.)
                continue

            # Мало данных — не отключаем
            if spend < min_spend or leads < min_leads:
                ad["recommendation"] = "ЖДАТЬ"
                ad["reason"] = "Мало данных для портфельной оценки"
                continue

            # Нет аналога в своей шкале (одиночка в подгруппе)
            if n_sub == 1:
                # Нет с кем сравнивать — не отключаем
                ad["recommendation"] = "ЖДАТЬ"
                ad["reason"] = "Нет сопоставимого аналога — копим данные"
                continue

            # Нижняя половина ранга (rank >= ceil(n_sub/2))
            in_bottom = rank >= math.ceil(n_sub / 2)

            # Проверяем, есть ли явно лучший аналог (разрыв >= gap)
            score_gap = best_score - score
            has_better = score_gap >= min_score_gap

            if in_bottom and has_better:
                # Все условия выполнены — ОТКЛЮЧИТЬ
                if absolute_rec == "ОТКЛЮЧИТЬ":
                    # Оставляем ОТКЛЮЧИТЬ, но уточняем reason
                    ad["reason"] = f"В нижней части ранга, есть лучше — {ad['reason']}"
                else:
                    # absolute был ЖДАТЬ — тоже можно отключить
                    ad["recommendation"] = "ОТКЛЮЧИТЬ"
                    if (ad.get("romi") is not None) or (ad.get("qual_pct") is not None):
                        ad["reason"] = f"В нижней части ранга, есть лучше — {ad.get('reason', '')}"
                    else:
                        ad["reason"] = "Слабее аналогов по CPL/CTR, есть лучше"
            elif score_gap < min_score_gap and in_bottom:
                # Все примерно равны — не отключаем
                ad["recommendation"] = "ЖДАТЬ"
                ad["reason"] = "Все в группе примерно равны — копим данные"
            else:
                # Не в нижней половине — не трогаем (absolute остаётся, но если был ОТКЛЮЧИТЬ — смягчаем)
                if absolute_rec == "ОТКЛЮЧИТЬ":
                    ad["recommendation"] = "ЖДАТЬ"
                    ad["reason"] = "Не худшее в группе — копим данные"

    return ads
