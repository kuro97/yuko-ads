"""
Лёгкий ежедневный обновитель расхода и статусов активных объявлений.

Проблема: creative_backfill обходит весь кабинет (~1900 реклам) медленно
и из-за рейт-лимитов отстаёт на 10-12 дней. Итог — spend=0 у активных
объявлений → romi считается некорректно. И ещё: effective_status устаревает —
удалённые/паузнутые объявления числятся ACTIVE в базе неделями, что ломает
боевой цикл пауз бота и SQL-аудиты.

Решение: ежедневно (крон 12:xx по локальному времени):
1. Тянуть lifetime-spend для ACTIVE объявлений (~176 штук).
2. Обновлять effective_status для тех же ACTIVE объявлений из живого FB —
   если объявление пропало из выдачи (удалено) → пометить DELETED.

Переиспользует:
- _fetch_lifetime_insights из creative_backfill (spend)
- _fetch_statuses_now из agent.analyzer (статусы) — тот же
  лёгкий запрос без insights, без нового FB-клиента.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from services.creative_intelligence import _get_connection
from services.creative_backfill import _fetch_lifetime_insights

logger = logging.getLogger(__name__)

# Путь к state-файлу дедупликации крона
_STATE_PATH = Path(__file__).parent.parent / "data" / "spend_refresh_state.json"


def _load_state() -> dict:
    """Загружает состояние крона (last_run_date)."""
    try:
        if _STATE_PATH.exists():
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("spend_refresh: не удалось прочитать state — %s", exc)
    return {}


def _save_state(state: dict) -> None:
    """Сохраняет состояние крона."""
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("spend_refresh: не удалось сохранить state — %s", exc)


def _refresh_spend_only(active_ad_ids: list[str]) -> dict:
    """Общий сбор spend для переданных ACTIVE ad_id (без статусов).

    Вынесено из refresh_active_ads_spend, чтобы переиспользовать в лёгком режиме
    (refresh_active_ads_spend_light) для учащённых циклов Стража — без похода
    за статусами всего кабинета (это отдельный дорогой FB-запрос с пагинацией).

    Алгоритм:
    1. Тянет lifetime-метрики через _fetch_lifetime_insights (батчи по 50,
       date_preset=maximum) — переиспользует existing функцию, новый клиент не нужен.
    2. Обновляет creative_kb.spend + leads + impressions + synced_at для этих ad_id
       через UPDATE (НЕ через UPSERT — не трогаем остальные поля записи).

    Returns: {"updated": N, "fetched": M, "active_count": K}
    Не падает — при ошибках логирует, возвращает то что успело обновиться.
    """
    active_count = len(active_ad_ids)

    if not active_ad_ids:
        logger.info("_refresh_spend_only: нет активных объявлений в KB — пропускаем")
        return {"updated": 0, "fetched": 0, "active_count": 0}

    logger.info("_refresh_spend_only: запрашиваем spend для %d активных объявлений", active_count)

    # Тянем lifetime-метрики через уже существующую функцию (батчами по 50).
    # Мультикабинетно: act-scoped insights не отдаёт строки чужого
    # кабинета — spend объявлений cabinet_b замирал бы на значении вставки.
    # Объявление отвечает только из своего кабинета, merge конфликтов не даёт;
    # отказ кабинета безопасен — его строки просто не обновятся (UPDATE only).
    metrics_by_id: dict[str, dict] = {}
    from services.fb_token_provider import fb_account, offline_account_context

    try:
        from services.launch_routing import accounts_to_scan

        _accounts = accounts_to_scan() or None
    except Exception as exc:  # noqa: BLE001 — деградация до старого охвата
        logger.warning("_refresh_spend_only: карта роутинга недоступна — %s", exc)
        _accounts = None
    if not _accounts:
        from services.fb_token_provider import get_fb_account_id

        _accounts = (str(get_fb_account_id()).replace("act_", ""),)
    for _account_id in _accounts:
        try:
            _ctx = offline_account_context(_account_id)
        except Exception as exc:  # noqa: BLE001 — незарегистрированный кабинет
            logger.warning("_refresh_spend_only: кабинет %s отвергнут — %s", _account_id, exc)
            continue
        with fb_account(_ctx):
            metrics_by_id.update(_fetch_lifetime_insights(active_ad_ids))

    fetched = len(metrics_by_id)
    logger.info("_refresh_spend_only: FB вернул данные для %d из %d объявлений", fetched, active_count)

    if not metrics_by_id:
        logger.warning("_refresh_spend_only: FB вернул пустые метрики — spend не обновлён")
        return {"updated": 0, "fetched": 0, "active_count": active_count}

    # Обновляем только поля spend/leads/impressions/synced_at — не трогаем классификацию,
    # labeling, amo-исходы и прочие поля которые обновляет backfill полностью
    updated = 0
    try:
        conn = _get_connection()
        try:
            for ad_id, metrics in metrics_by_id.items():
                spend = float(metrics.get("spend") or 0)
                leads = int(metrics.get("leads") or 0)
                impressions = int(metrics.get("impressions") or 0)
                conn.execute(
                    """
                    UPDATE creative_kb
                    SET spend = ?, leads = ?, impressions = ?, synced_at = datetime('now')
                    WHERE ad_id = ?
                    """,
                    (spend, leads, impressions, ad_id),
                )
                updated += conn.execute(
                    "SELECT changes() AS n"
                ).fetchone()["n"]
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.error("_refresh_spend_only: ошибка при UPDATE в KB — %s", exc)
        raise

    logger.info(
        "_refresh_spend_only: обновлено %d строк (из %d с данными FB)",
        updated,
        fetched,
    )

    return {"updated": updated, "fetched": fetched, "active_count": active_count}


def _read_active_ad_ids() -> list[str]:
    """Читает ad_id всех ACTIVE объявлений из creative_kb."""
    try:
        conn = _get_connection()
        try:
            rows = conn.execute(
                "SELECT ad_id FROM creative_kb WHERE effective_status = 'ACTIVE'"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.error("_read_active_ad_ids: не удалось прочитать KB — %s", exc)
        raise
    return [r["ad_id"] for r in rows]


def refresh_active_ads_spend() -> dict:
    """Обновляет lifetime spend + effective_status активных объявлений из живого FB.

    Полный рефреш (боевой крон 12:xx по локальному времени): spend + статусы всего кабинета.
    Для лёгкого цикла (каждые 2ч) используйте refresh_active_ads_spend_light —
    она НЕ тянет статусы (экономит FB-запросы).

    Returns: {"updated": N, "fetched": M, "active_count": K,
              "status_updated": N, "status_demoted": M}
    Не падает — при ошибках логирует, возвращает то что успело обновиться.
    """
    active_ad_ids = _read_active_ad_ids()

    spend_result = _refresh_spend_only(active_ad_ids)

    if not active_ad_ids:
        return spend_result

    # Обновляем effective_status сразу после spend — пока уже взяли ACTIVE из KB
    status_result = refresh_active_ads_status(active_ad_ids)

    return {
        **spend_result,
        "status_updated": status_result["status_updated"],
        "status_demoted": status_result["status_demoted"],
    }


def refresh_active_ads_spend_light() -> dict:
    """Лёгкий рефреш: только lifetime-spend ACTIVE, БЕЗ обновления статусов.

    Идентичен refresh_active_ads_spend, но НЕ вызывает refresh_active_ads_status
    (не тянет статусы всего кабинета — экономит FB-запросы для 2-часового цикла
    Стража, см. docs/specs/ARCH-phase1-guardian.md §6.3, §8.1).

    Returns: {"updated": N, "fetched": M, "active_count": K}  (без status_* полей)
    Не падает — при ошибках логирует, возвращает то что успело обновиться.
    """
    active_ad_ids = _read_active_ad_ids()
    return _refresh_spend_only(active_ad_ids)


def refresh_active_ads_status(active_ad_ids: list[str] | None = None) -> dict:
    """Обновляет effective_status для ACTIVE объявлений из живого FB.

    Переиспользует _fetch_statuses_now из agent.analyzer — лёгкий запрос
    (только id + effective_status, без insights).

    Алгоритм:
    - Если active_ad_ids не передан — читает ACTIVE из KB (для самостоятельного вызова).
    - Тянет свежие статусы через _fetch_statuses_now (все не-DELETED объявления FB).
    - Для каждого ACTIVE из KB:
        * Есть в FB → обновляем effective_status (может стать PAUSED/ADSET_PAUSED/etc).
        * Нет в FB (удалено) → ставим DELETED.
    - Не трогает spend, leads, impressions и другие поля.

    Returns: {"status_updated": N, "status_demoted": M, "active_count": K}
    Некритичная операция: при ошибке FB логирует и возвращает нули (не падает).
    """
    # Если ad_id не переданы — читаем из KB (вызов без контекста spend-рефреша)
    if active_ad_ids is None:
        try:
            conn = _get_connection()
            try:
                rows = conn.execute(
                    "SELECT ad_id FROM creative_kb WHERE effective_status = 'ACTIVE'"
                ).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            logger.error("refresh_active_ads_status: не удалось прочитать KB — %s", exc)
            raise
        active_ad_ids = [r["ad_id"] for r in rows]

    active_count = len(active_ad_ids)
    if not active_ad_ids:
        logger.info("refresh_active_ads_status: нет ACTIVE объявлений — пропускаем")
        return {"status_updated": 0, "status_demoted": 0, "active_count": 0}

    # Тянем свежие статусы через существующий механизм из analyzer.
    # Импорт здесь (не на уровне модуля) чтобы избежать циклического импорта:
    # analyzer → fb_common → (services не импортирует), но spend_refresh → analyzer
    # при старте модуля может вызвать проблемы если analyzer не до конца загружен.
    try:
        from agent.analyzer import _fetch_statuses_now
        fresh_statuses = _fetch_statuses_now()
    except Exception as exc:
        # Некритично — статусы не обновились, но spend уже сохранён
        logger.warning("refresh_active_ads_status: не удалось получить статусы от FB — %s", exc)
        return {"status_updated": 0, "status_demoted": 0, "active_count": active_count}

    if not fresh_statuses:
        logger.warning("refresh_active_ads_status: FB вернул пустые статусы — пропускаем")
        return {"status_updated": 0, "status_demoted": 0, "active_count": active_count}

    logger.info(
        "refresh_active_ads_status: FB вернул %d статусов, проверяем %d ACTIVE",
        len(fresh_statuses), active_count,
    )

    # Обновляем effective_status только для тех кто изменился (не трогаем лишние строки)
    status_updated = 0  # статус поменялся (ACTIVE → что-то другое или ACTIVE → ACTIVE)
    status_demoted = 0  # объявления которые выбыли из ACTIVE (включая DELETED)
    try:
        conn = _get_connection()
        try:
            for ad_id in active_ad_ids:
                new_status = fresh_statuses.get(ad_id)
                if new_status is None:
                    # Объявление пропало из выдачи FB → удалено/архивировано
                    new_status = "DELETED"

                if new_status != "ACTIVE":
                    # Статус изменился — обновляем (ACTIVE → PAUSED/DELETED/etc)
                    conn.execute(
                        "UPDATE creative_kb SET effective_status = ? WHERE ad_id = ? AND effective_status = 'ACTIVE'",
                        (new_status, ad_id),
                    )
                    changed = conn.execute("SELECT changes() AS n").fetchone()["n"]
                    if changed:
                        status_updated += 1
                        status_demoted += 1
                        logger.info(
                            "refresh_active_ads_status: %s ACTIVE → %s", ad_id, new_status
                        )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.error("refresh_active_ads_status: ошибка при UPDATE effective_status — %s", exc)
        raise

    logger.info(
        "refresh_active_ads_status: статус обновлён у %d объявлений (%d выбыло из ACTIVE)",
        status_updated, status_demoted,
    )
    return {"status_updated": status_updated, "status_demoted": status_demoted, "active_count": active_count}
