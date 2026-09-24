"""Освобождение слотов в боевых адсетах: архивация давно паузнутых объявлений.

Facebook держит лимит 50 объявлений на адсет, и паузнутое объявление занимает
слот наравне с живым. Когда слоты кончаются, новым креативам физически некуда
ложиться — конвейер запуска встаёт молча, а выглядит это как «реклама не
запускается». Заметную часть слотов боевого адсета обычно держат давно
паузнутые объявления.

Чистильщик архивирует ровно то, что давно стоит на паузе. Архив ОБРАТИМ:
объявление возвращается сменой статуса, статистика и история сохраняются, а в
аналитике проекта архивные и так считаются выбывшими
(``integrations.facebook._RETIRED_AD_EFFECTIVE_STATUSES``), поэтому цифры
отчётов не поедут.

Чего здесь нет и быть не может:
  * DELETE — он необратим; удаление живёт в отдельном fail-closed контуре
    (``services/adset_cleaner.py``) со своим тройным подтверждением;
  * любых действий с ACTIVE — работающую рекламу чистильщик не трогает никогда;
  * работы по адсетам, которых нет в боевом перечне: старые и чужие адсеты
    слотов конвейеру не занимают, лезть в них незачем.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# Часовой пояс CityA (UTC+5) — вся периодика проекта считается в нём.
_TZ_LOCAL = timezone(timedelta(hours=5))

# Лимит Facebook на объявления в адсете. Держим свою копию, чтобы отчёт о
# свободных слотах не зависел от импорта тяжёлого модуля.
MAX_ADS_PER_ADSET = 50

# Машинное имя правила — попадает в лог и в отчёт владельцу.
CLEANER_RULE = "SLOT_CLEANER_ARCHIVE_STALE_PAUSED"

SLOT_CLEANER_DEFAULTS: dict[str, dict] = {
    "slot_cleaner": {
        # Мастер-ключ. ДЕФОЛТ FALSE: после деплоя поведение не меняется, пока
        # владелец не включит чистку в настройках (без деплоя).
        "enabled": False,
        # Раз во сколько суток чистим. Решение владельца — раз в 3 дня.
        "every_days": 3,
        # Сколько полных суток объявление обязано простоять на паузе, прежде
        # чем считаться мусором. Две недели — запас на «паузнул, передумал».
        "min_paused_days": 14,
        # Потолок на прогон: защита от лавины, если критерий вдруг зацепит
        # больше ожидаемого. Остаток уйдёт в следующий прогон через 3 дня.
        "max_archive_per_run": 150,
    },
}


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

def slot_cleaner_config(cfg: Mapping[str, object] | None = None) -> dict:
    """Блок autopilot.slot_cleaner с fail-safe чтением.

    Любая ошибка чтения настроек даёт пустой блок, то есть выключенную чистку:
    молчаливая деградация здесь идёт в безопасную сторону.
    """
    if cfg is None:
        try:
            from services.autopilot import get_autopilot_config

            cfg = get_autopilot_config() or {}
        except Exception as exc:  # noqa: BLE001 — настройки не роняют прогон
            logger.warning("slot_cleaner: настройки недоступны — %s", exc)
            cfg = {}
    block = (cfg or {}).get("slot_cleaner")
    return block if isinstance(block, dict) else {}


def is_slot_cleaner_enabled(cfg: Mapping[str, object] | None = None) -> bool:
    """Мастер-ключ, fail-CLOSED.

    Проверка ``is True``, а не ``.get(..., False)``: это мутация рекламного
    кабинета. Строка "true", 1, "yes" из правленого руками settings.json чистку
    НЕ включают — только настоящий JSON-boolean, который кладёт туда валидатор.
    """
    return slot_cleaner_config(cfg).get("enabled") is True


def safe_error(value: object) -> str:
    """Единая граница: наружу не выходит сырой exception с секретом.

    Текст ошибки requests содержит URL запроса, а в нём живёт access_token.
    Этот текст идёт и в лог, и в отчёт владельцу в Telegram, поэтому чистим
    его тем же санитайзером, что и durable-записи чистильщика удаления.
    """
    from services.cleanup_repository import sanitize_text

    return sanitize_text(value)


def _positive_int(config: Mapping[str, object], key: str) -> int:
    """Целое из настроек с откатом на дефолт при любом мусоре.

    bool отсекаем отдельно: True прошёл бы как 1 и молча превратил бы, скажем,
    min_paused_days в «сутки», то есть ослабил бы критерий.
    """
    default = int(SLOT_CLEANER_DEFAULTS["slot_cleaner"][key])
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def cleanup_every_days(cfg: Mapping[str, object] | None = None) -> int:
    """Раз во сколько суток чистить. Мусор в настройках даёт дефолт."""
    return _positive_int(slot_cleaner_config(cfg), "every_days")


# ---------------------------------------------------------------------------
# Перечень боевых адсетов
# ---------------------------------------------------------------------------

def _discover_adsets() -> dict:
    """Обёртка для мокирования в тестах."""
    from agent.adset_discovery import discover_adsets

    return discover_adsets()


def live_adset_ids() -> dict[str, str]:
    """{adset_id: человеческое имя} — только боевые адсеты конвейера.

    Берём ровно то, во что конвейер кладёт новые объявления: leadgen L2/L1 по
    городам и mql-адсеты. Ключ "old" из discover_adsets намеренно НЕ берём:
    старые адсеты слотов конвейеру не занимают.

    Fail-closed: если перечень пришёл не в ожидаемой форме (частичный отказ
    кабинета, битый кеш), возвращаем пусто — лучше не почистить, чем почистить
    не там.
    """
    try:
        discovered = _discover_adsets()
    except Exception as exc:  # noqa: BLE001 — сбой перечня не роняет крон
        logger.warning("slot_cleaner: перечень адсетов недоступен — %s", exc)
        return {}
    if not isinstance(discovered, dict):
        logger.warning("slot_cleaner: перечень адсетов не словарь — чистка пропущена")
        return {}

    result: dict[str, str] = {}

    leadgen = discovered.get("leadgen")
    if isinstance(leadgen, dict):
        for city, slots in leadgen.items():
            if not isinstance(slots, dict):
                continue
            for kind, adset_id in slots.items():
                if isinstance(adset_id, str) and adset_id.isdigit():
                    result[adset_id] = f"{city} · {kind}"

    mql = discovered.get("mql")
    if isinstance(mql, dict):
        for city, adset_id in mql.items():
            if isinstance(adset_id, str) and adset_id.isdigit():
                result.setdefault(adset_id, f"{city} · MQL")

    return result


# ---------------------------------------------------------------------------
# Отбор кандидатов
# ---------------------------------------------------------------------------

def _fetch_ads(adset_id: str) -> list[dict]:
    """Объявления адсета с полями статуса и времени изменения."""
    from agent.fb_common import API, _throttled_get
    from integrations.facebook import get_fb_token

    ads: list[dict] = []
    url = f"{API}/{adset_id}/ads"
    params: dict[str, Any] = {
        "fields": "id,name,status,effective_status,updated_time",
        "limit": 200,
        "access_token": get_fb_token(),
    }
    # Страницы ограничены: у адсета не может быть больше 50 живых объявлений,
    # но архивные тоже приходят, поэтому запас есть — а бесконечного цикла нет.
    for _ in range(10):
        response = _throttled_get(url, params=params)
        response.raise_for_status()
        payload = response.json()
        page = payload.get("data")
        if not isinstance(page, list):
            break
        ads.extend(item for item in page if isinstance(item, dict))
        next_url = (payload.get("paging") or {}).get("next")
        if not next_url:
            break
        url, params = next_url, {}
    return ads


def _paused_days(ad: Mapping[str, object], now: datetime) -> float | None:
    """Сколько полных суток объявление не трогали. None — время нечитаемо.

    updated_time — момент последнего изменения объявления, и для паузнутого это
    и есть момент паузы (после паузы его никто не редактирует). Нечитаемое
    время означает «не знаем возраст», и такой кандидат отсеивается.
    """
    raw = ad.get("updated_time")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        updated = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (now - updated).total_seconds() / 86400.0


def select_candidates(
    ads: list[dict],
    *,
    min_paused_days: int,
    now: datetime,
) -> list[dict]:
    """Объявления, которые можно архивировать. Порядок — самые старые первыми.

    Три условия, каждое обязательное:
      * собственный статус ровно PAUSED — ACTIVE не трогаем никогда, а уже
        архивные и удалённые трогать незачем;
      * effective_status тоже PAUSED — если объявление стоит из-за паузы
        адсета или кампании (ADSET_PAUSED / CAMPAIGN_PAUSED), то само по себе
        оно живое, и владелец ждёт его возврата вместе с адсетом;
      * с последнего изменения прошло min_paused_days полных суток.
    """
    candidates = []
    for ad in ads:
        if ad.get("status") != "PAUSED":
            continue
        if ad.get("effective_status") != "PAUSED":
            continue
        ad_id = ad.get("id")
        if not isinstance(ad_id, str) or not ad_id.isdigit():
            continue
        age = _paused_days(ad, now)
        if age is None or age < min_paused_days:
            continue
        candidates.append({
            "ad_id": ad_id,
            "name": str(ad.get("name") or ""),
            "paused_days": age,
        })
    candidates.sort(key=lambda item: item["paused_days"], reverse=True)
    return candidates


def _occupied_slots(ads: list[dict]) -> int:
    """Сколько слотов адсета занято: всё, кроме архивных и удалённых."""
    return sum(
        1 for ad in ads
        if ad.get("status") not in ("ARCHIVED", "DELETED")
    )


# ---------------------------------------------------------------------------
# Архивация
# ---------------------------------------------------------------------------

def _archive_ad(ad_id: str) -> None:
    """Переводит объявление в ARCHIVED. Ошибку пробрасывает наверх."""
    from agent.fb_common import API, _throttled_post
    from integrations.facebook import get_fb_token

    response = _throttled_post(
        f"{API}/{ad_id}",
        data={"status": "ARCHIVED", "access_token": get_fb_token()},
    )
    response.raise_for_status()


def run_slot_cleanup(
    *,
    dry_run: bool = True,
    now: datetime | None = None,
    cfg: Mapping[str, object] | None = None,
) -> dict:
    """Прогон чистки слотов.

    dry_run=True (ДЕФОЛТ) — только считает и возвращает план, ничего не меняет.
    Боевой прогон дополнительно требует включённого мастер-ключа: вызов с
    dry_run=False при выключенной чистке ничего не архивирует.

    Returns:
        {
            "ran": bool,                  # дошли ли до отбора кандидатов
            "dry_run": bool,
            "skipped_reason": str | None,
            "adsets": [                   # по одному блоку на адсет
                {"adset_id", "name", "occupied_before", "candidates",
                 "archived", "errors"}
            ],
            "archived_total": int,
            "candidates_total": int,
            "errors": [str],
        }
    """
    moment = now or datetime.now(_TZ_LOCAL)
    config = slot_cleaner_config(cfg)
    min_paused_days = _positive_int(config, "min_paused_days")
    max_per_run = _positive_int(config, "max_archive_per_run")

    result: dict[str, Any] = {
        "ran": False,
        "dry_run": dry_run,
        "skipped_reason": None,
        "adsets": [],
        "archived_total": 0,
        "candidates_total": 0,
        "errors": [],
    }

    # Боевой прогон без мастер-ключа деградирует в план, а не в отказ: владелец
    # всё равно увидит, что чистильщик собирался сделать.
    if not dry_run and not is_slot_cleaner_enabled(cfg):
        dry_run = True
        result["dry_run"] = True
        result["skipped_reason"] = "slot_cleaner.enabled=false — только план"

    adsets = live_adset_ids()
    if not adsets:
        result["skipped_reason"] = result["skipped_reason"] or (
            "боевые адсеты не определены — чистка пропущена"
        )
        return result

    result["ran"] = True
    budget_left = max_per_run

    for adset_id, adset_name in adsets.items():
        block: dict[str, Any] = {
            "adset_id": adset_id,
            "name": adset_name,
            "occupied_before": None,
            "candidates": 0,
            "archived": 0,
            "errors": [],
        }
        try:
            ads = _fetch_ads(adset_id)
        except Exception as exc:  # noqa: BLE001 — один адсет не роняет прогон
            message = f"{adset_name}: перечень объявлений недоступен — {safe_error(exc)}"
            logger.warning("slot_cleaner: %s", message)
            block["errors"].append(message)
            result["errors"].append(message)
            result["adsets"].append(block)
            continue

        block["occupied_before"] = _occupied_slots(ads)
        candidates = select_candidates(
            ads, min_paused_days=min_paused_days, now=moment
        )
        block["candidates"] = len(candidates)
        result["candidates_total"] += len(candidates)

        if dry_run:
            result["adsets"].append(block)
            continue

        for candidate in candidates:
            if budget_left <= 0:
                break
            try:
                _archive_ad(candidate["ad_id"])
            except Exception as exc:  # noqa: BLE001 — одно объявление не роняет прогон
                message = (
                    f"{adset_name}: {candidate['ad_id']} не архивируется — "
                    f"{safe_error(exc)}"
                )
                logger.warning("slot_cleaner: %s", message)
                block["errors"].append(message)
                result["errors"].append(message)
                continue
            block["archived"] += 1
            budget_left -= 1

        result["archived_total"] += block["archived"]
        result["adsets"].append(block)

    logger.info(
        "slot_cleaner: dry_run=%s адсетов=%d кандидатов=%d архивировано=%d ошибок=%d",
        dry_run,
        len(result["adsets"]),
        result["candidates_total"],
        result["archived_total"],
        len(result["errors"]),
    )
    return result


# ---------------------------------------------------------------------------
# Отчёт владельцу
# ---------------------------------------------------------------------------

def format_report(result: Mapping[str, Any]) -> str:
    """Человеческий отчёт о прогоне: где сколько слотов освободилось."""
    if not result.get("ran"):
        reason = result.get("skipped_reason") or "нечего чистить"
        return f"🧹 Чистка слотов не выполнена: {reason}"

    dry_run = bool(result.get("dry_run"))
    head = "🧹 Чистка слотов — план" if dry_run else "🧹 Чистка слотов выполнена"
    lines = [head, ""]

    blocks = [
        block for block in result.get("adsets", [])
        if block.get("candidates") or block.get("errors")
    ]
    blocks.sort(key=lambda item: item.get("candidates", 0), reverse=True)

    for block in blocks:
        occupied = block.get("occupied_before")
        count = block.get("archived") if not dry_run else block.get("candidates")
        if occupied is None:
            lines.append(f"• {block['name']}: {count}")
            continue
        freed = block.get("archived", 0) if not dry_run else block.get("candidates", 0)
        after = MAX_ADS_PER_ADSET - occupied + freed
        verb = "освобождено" if not dry_run else "можно освободить"
        lines.append(
            f"• {block['name']}: {verb} {count} — "
            f"свободно станет {after} из {MAX_ADS_PER_ADSET}"
        )

    if not blocks:
        lines.append("Нечего архивировать — во всех боевых адсетах чисто.")

    if dry_run and result.get("skipped_reason"):
        lines.append("")
        lines.append(f"Причина: {result['skipped_reason']}")

    errors = result.get("errors") or []
    if errors:
        lines.append("")
        lines.append(f"⚠️ Ошибок: {len(errors)}")
        for message in errors[:3]:
            lines.append(f"   {message}")

    return "\n".join(lines)
