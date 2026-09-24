"""
Страж просроченных офферов — ночной крон.

Сценарии, из-за которых он появился (пример):
- объявление с «до 15 июня» в имени продолжает тратить бюджет весь следующий месяц
  (оффер давно истёк, реклама крутится);
- дата «до 30 июня» видна только в картинке креатива (в имени даты нет).

Что делает крон (read-only, ничего сам не паузит):
1. Обходит эффективно-активные объявления кабинета (account-level листинг /ads,
   тот же паттерн, что hourly_collector._fetch_recent_ads_from_fb, но без фильтра
   «младше 48ч» и с потолком страниц — тянем ВСЕ активные).
2. Ищет истёкшие даты оффера:
   (а) в ИМЕНИ объявления — «до DD месяц» (месяцы L1 + L2), «DD.MM», «скидка до …»;
   (б) в текстах креатива (body/title/object_story_spec) — тянем инлайн одним
       запросом (field-expansion creative{...} = батч-GET).
3. Если дата истекла И объявление активно И расход > 0 за вчера (ad_daily_metrics) →
   Telegram-алерт списком с кнопками «⏸ Остановить» (callback pause:<ad_id> —
   существующий whitelisted-обработчик в telegram_bot: PAUSED, обратимо, паузит
   человек кнопкой; сам крон в dry-run, только алерт).
4. Видео без извлекаемого текста и без даты в имени — слепая зона: НЕ трогаем,
   в отчёт честной строкой «непроверяемых: N».

Мастер-ключ settings.autopilot.expired_offer_guard.enabled (дефолт включён).
Всё fail-safe: FB/БД-ошибки НЕ роняют крон — возвращаем отчёт с error.
"""

import html
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Локальное время (UTC+5 по умолчанию, настраивается) — единое со всеми модулями проекта
_TZ_LOCAL = timezone(timedelta(hours=5))

# State-файл дедупликации алертов по ad_id (паттерн cron_watchdog_state.json)
_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "expired_offer_guard_state.json"

# Потолок страниц пагинации /ads — не крутим бесконечно, если кабинет огромный
_FB_MAX_PAGES = 20
# Объявлений на страницу
_FB_PAGE_LIMIT = 100

# Максимум объявлений в одном Telegram-алерте (у каждого — своя кнопка «Остановить»)
_MAX_ALERT_ADS = 20

# Дата без явного года считается истёкшей, только если она в прошлом НЕ более чем на
# столько дней. Иначе «до 15 января», увиденное в декабре, ложно бьётся как истёкшее,
# хотя рекламодатель имел в виду январь СЛЕДУЮЩЕГО года.
_MAX_PAST_DAYS = 180


# ---------------------------------------------------------------------------
# Словарь месяцев (L1 + L2) и разбор дат
# ---------------------------------------------------------------------------

# Русские месяцы — стемы (ловят склонения: «июнь/июня/июне»). «ма» отдельно ниже.
_MONTHS_RU: dict[str, int] = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "июн": 6, "июл": 7,
    "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
# Май — короткий стем «ма», ловим отдельным точным набором форм, чтобы «ма» не
# цеплялся как префикс к любому слову на «ма…».
_MONTHS_RU_MAY = {"май", "мая", "мае", "маю", "маем"}

# Месяцы второго языка (L2) — точные словоформы. По умолчанию заглушка на
# английском (как и тексты L2 в config.AD_BODY): замените словоформами своего
# второго языка. Сравнение ТОЧНОЕ, без префикса: «mar» не должен ловить «marketing».
# Для языков с падежными окончаниями перечислите нужные формы явно.
_MONTHS_L2: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# «DD <слово>» — день + словесный месяц (L1/L2). Слово берём целиком (\w+ по
# юникоду), месяц резолвим отдельно через _resolve_month.
_WORD_DATE_RE = re.compile(r"(\d{1,2})\s+([^\W\d_]+)", re.IGNORECASE | re.UNICODE)

# «DD.MM» или «DD.MM.YY(YY)» — числовая дата (в имени/тексте почти всегда дедлайн).
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b")


def _resolve_month(word: str) -> int | None:
    """Слово-месяц (рус. склонение или словоформа L2) → номер месяца 1-12. Иначе None."""
    w = word.lower().strip()
    if not w:
        return None
    # L2 — точное совпадение словоформы (см. _MONTHS_L2)
    if w in _MONTHS_L2:
        return _MONTHS_L2[w]
    # Рус. май — отдельный набор точных форм («ма» слишком короткий для префикса)
    if w in _MONTHS_RU_MAY:
        return 5
    # Рус. — по стему-префиксу (склонения июнь/июня/июне)
    for stem, num in _MONTHS_RU.items():
        if w.startswith(stem):
            return num
    return None


def _iter_date_candidates(text: str):
    """Генератор кортежей (day, month, year|None) всех дат в тексте (словесных и
    числовых). Невалидные (месяц/день вне диапазона) не отдаёт. year — 4-значный
    (2-значный разворачиваем в 20YY), None если год не указан."""
    if not text:
        return

    # Словесные даты: «15 июня», «30 june»
    for match in _WORD_DATE_RE.finditer(text):
        day = int(match.group(1))
        month = _resolve_month(match.group(2))
        if month is None or not (1 <= day <= 31):
            continue
        yield (day, month, None)

    # Числовые даты: «15.06», «15.06.26»
    for match in _NUMERIC_DATE_RE.finditer(text):
        day = int(match.group(1))
        month = int(match.group(2))
        if not (1 <= day <= 31 and 1 <= month <= 12):
            continue
        year_raw = match.group(3)
        year: int | None = None
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
        yield (day, month, year)


def _contains_date(text: str) -> bool:
    """True, если в тексте есть хоть одна распознаваемая дата (истёкшая или нет).

    Нужно для «слепой зоны»: объявление БЕЗ текста И БЕЗ даты в имени непроверяемо.
    """
    for _ in _iter_date_candidates(text):
        return True
    return False


def _find_expired_deadline(text: str, today: date) -> date | None:
    """Возвращает самую раннюю ИСТЁКШУЮ дату оффера в тексте, либо None.

    - Дата с явным годом: истёкшая, если < today.
    - Дата без года: берём текущий год; истёкшая, только если в прошлом не более
      чем на _MAX_PAST_DAYS дней (иначе это «в следующем году», не флажим).
    """
    expired: list[date] = []
    for day, month, year in _iter_date_candidates(text):
        if year is not None:
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            if d < today:
                expired.append(d)
            continue
        # Без года — текущий год + окно «прошлого»
        try:
            d = date(today.year, month, day)
        except ValueError:
            continue
        delta = (today - d).days
        if 0 < delta <= _MAX_PAST_DAYS:
            expired.append(d)

    return min(expired) if expired else None


# ---------------------------------------------------------------------------
# FB: листинг активных объявлений + извлечение текста креатива
# ---------------------------------------------------------------------------

def _fetch_active_ads() -> list[dict]:
    """Тянет эффективно-активные объявления кабинета (account-level /ads).

    Тот же паттерн листинга, что hourly_collector._fetch_recent_ads_from_fb
    (agent.fb_common примитивы, after-курсор, потолок страниц), но БЕЗ фильтра
    «младше 48ч» — берём все активные. Инлайн тянем creative{body,title,
    object_story_spec} (field-expansion = батч-GET текстов за один запрос).

    Returns: список сырых dict объявлений из data[].
    Бросает FBApiError при не-200 — верхний уровень ловит и уходит в fail-safe.
    """
    from agent.fb_common import API, FBApiError, _throttled_get
    from services.fb_token_provider import get_fb_account_id, get_fb_token

    account_id = get_fb_account_id()
    token = get_fb_token()

    ads: list[dict] = []
    cursor: str | None = None

    for _page in range(_FB_MAX_PAGES):
        params: dict = {
            "access_token": token,
            "fields": "id,name,effective_status,creative{id,body,title,object_story_spec}",
            "filtering": json.dumps(
                [{"field": "effective_status", "operator": "IN", "value": ["ACTIVE"]}]
            ),
            "limit": _FB_PAGE_LIMIT,
        }
        if cursor:
            params["after"] = cursor

        resp = _throttled_get(f"{API}/act_{account_id}/ads", params=params)
        if resp.status_code != 200:
            raise FBApiError(
                f"FB /ads ошибка: {resp.status_code} {resp.text[:200]}", resp.status_code
            )

        data = resp.json()
        for ad in data.get("data", []):
            if isinstance(ad, dict) and ad.get("id"):
                ads.append(ad)

        paging = data.get("paging", {})
        cursor = paging.get("cursors", {}).get("after")
        if "next" not in paging or not cursor:
            break

    return ads


def _extract_creative_text(creative: object) -> str:
    """Собирает весь читаемый текст креатива: body/title + object_story_spec
    (message/name/description/caption по link_data/video_data/photo_data +
    child_attachments карусели). Возвращает склеенную строку (может быть пустой)."""
    if not isinstance(creative, dict):
        return ""

    parts: list[str] = []
    for key in ("body", "title", "name"):
        val = creative.get(key)
        if val:
            parts.append(str(val))

    oss = creative.get("object_story_spec")
    if isinstance(oss, dict):
        for spec_key in ("link_data", "video_data", "photo_data", "template_data"):
            spec = oss.get(spec_key)
            if not isinstance(spec, dict):
                continue
            for k in ("message", "name", "description", "caption", "title"):
                val = spec.get(k)
                if val:
                    parts.append(str(val))
            for child in spec.get("child_attachments") or []:
                if isinstance(child, dict):
                    for k in ("name", "description"):
                        val = child.get(k)
                        if val:
                            parts.append(str(val))

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Расход за вчера (ad_daily_metrics)
# ---------------------------------------------------------------------------

def _spent_yesterday(ad_ids: list[str], yesterday_iso: str) -> dict[str, float]:
    """Для переданных ad_id возвращает {ad_id: spend} только по тем, у кого
    spend > 0 за дату yesterday_iso (ad_daily_metrics). Параметризованный запрос."""
    if not ad_ids:
        return {}

    from services.creative_intelligence import _get_connection

    conn = _get_connection()
    try:
        placeholders = ",".join("?" for _ in ad_ids)
        rows = conn.execute(
            f"SELECT ad_id, spend FROM ad_daily_metrics "
            f"WHERE date = ? AND spend > 0 AND ad_id IN ({placeholders})",
            (yesterday_iso, *ad_ids),
        ).fetchall()
        return {str(r["ad_id"]): float(r["spend"] or 0) for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Дедуп алертов по ad_id
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    """Читает state-файл дедупа. Битый/нет файла → {'alerts': {}}."""
    if not _STATE_FILE.exists():
        return {"alerts": {}}
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("alerts"), dict):
            return {"alerts": {}}
        return data
    except Exception as e:
        logger.warning("expired_offer_guard: не удалось прочитать state: %s", e)
        return {"alerts": {}}


def _save_state(state: dict) -> None:
    """Атомарно сохраняет state (tmp + rename). Не бросает."""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception as e:
        logger.warning("expired_offer_guard: не удалось сохранить state: %s", e)


def _is_deduped(ad_id: str, state: dict, now: datetime, dedup_hours: float) -> bool:
    """True, если по ad_id уже слали алерт в течение dedup_hours часов."""
    last_str = state.get("alerts", {}).get(ad_id)
    if not last_str:
        return False
    try:
        last = datetime.fromisoformat(last_str)
        if last.tzinfo is None:
            last = last.replace(tzinfo=_TZ_LOCAL)
        return (now - last).total_seconds() < dedup_hours * 3600
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Оркестратор
# ---------------------------------------------------------------------------

def _format_deadline(d: date) -> str:
    """Дата истёкшего оффера в человекочитаемом виде (DD.MM.YYYY)."""
    return d.strftime("%d.%m.%Y")


def run_expired_offer_guard(now: datetime | None = None) -> dict:
    """Главная функция стража: активные объявления → истёкшие офферы → расход>0
    вчера → Telegram-алерт с кнопками «⏸ Остановить». Dry-run: сам не паузит.

    Returns:
        {
            "enabled": bool,          # мастер-ключ включён
            "checked": int,           # сколько активных объявлений обошли
            "flagged": int,           # у скольких найдена истёкшая дата
            "wasters": int,           # из них с расходом>0 вчера (в алерте)
            "alerted": int,           # реально попало в алерт (после дедупа)
            "unverifiable": int,      # слепая зона (видео без текста и даты в имени)
            "skipped_reason": str|None,
            "error": str|None,
        }
    """
    if now is None:
        now = datetime.now(_TZ_LOCAL)

    result: dict = {
        "enabled": True, "checked": 0, "flagged": 0, "wasters": 0,
        "alerted": 0, "unverifiable": 0, "skipped_reason": None, "error": None,
    }

    # --- Мастер-ключ ---
    try:
        from services.autopilot import get_autopilot_config
        cfg = (get_autopilot_config() or {}).get("expired_offer_guard", {}) or {}
    except Exception as exc:
        logger.warning("expired_offer_guard: не удалось прочитать настройки — %s", exc)
        cfg = {}

    if not cfg.get("enabled", True):
        result["enabled"] = False
        result["skipped_reason"] = "disabled"
        logger.info("expired_offer_guard: мастер-ключ выключен — пропуск")
        return result

    dedup_hours = float(cfg.get("dedup_hours", 12))
    today = now.date()
    yesterday_iso = (today - timedelta(days=1)).isoformat()

    # --- Активные объявления из FB (fail-safe) ---
    try:
        ads = _fetch_active_ads()
    except Exception as exc:
        logger.warning("expired_offer_guard: FB недоступен (%s) — прогон без результата",
                       type(exc).__name__)
        result["error"] = f"FB недоступен: {type(exc).__name__}"
        return result

    result["checked"] = len(ads)

    # --- Ищем истёкшие даты + считаем непроверяемых ---
    flagged: list[dict] = []
    unverifiable = 0
    for ad in ads:
        ad_id = str(ad.get("id") or "")
        if not ad_id:
            continue
        name = ad.get("name") or ""
        creative_text = _extract_creative_text(ad.get("creative"))

        # (а) дата в имени → (б) дата в тексте креатива
        deadline = _find_expired_deadline(name, today)
        if deadline is None and creative_text:
            deadline = _find_expired_deadline(creative_text, today)

        if deadline is not None:
            flagged.append({"ad_id": ad_id, "name": name, "deadline": deadline})
            continue

        # Слепая зона: нет извлекаемого текста И нет даты в имени → непроверяемо
        if not creative_text.strip() and not _contains_date(name):
            unverifiable += 1

    result["flagged"] = len(flagged)
    result["unverifiable"] = unverifiable

    # --- Оставляем только тех, кто реально жёг бюджет вчера ---
    try:
        spent = _spent_yesterday([f["ad_id"] for f in flagged], yesterday_iso)
    except Exception as exc:
        logger.warning("expired_offer_guard: запрос расхода упал (%s) — считаем что расхода нет",
                       type(exc).__name__)
        spent = {}

    wasters = [{**f, "spend": spent[f["ad_id"]]} for f in flagged if f["ad_id"] in spent]
    wasters.sort(key=lambda w: w["spend"], reverse=True)
    result["wasters"] = len(wasters)

    if not wasters:
        logger.info(
            "expired_offer_guard: активных %d, с истёкшей датой %d, из них жёгших вчера 0, "
            "непроверяемых %d — алерт не нужен",
            len(ads), len(flagged), unverifiable,
        )
        return result

    # --- Дедуп по ad_id ---
    state = _load_state()
    fresh = [w for w in wasters if not _is_deduped(w["ad_id"], state, now, dedup_hours)]
    if not fresh:
        logger.info("expired_offer_guard: все %d просроченных задедуплены — алерт не шлём", len(wasters))
        return result

    # --- Строим и шлём алерт с кнопками «⏸ Остановить» ---
    shown = fresh[:_MAX_ALERT_ADS]
    lines = ["🗓 <b>Просроченные офферы жгут бюджет</b>", ""]
    for w in shown:
        safe_name = html.escape(w["name"][:80] or w["ad_id"])
        lines.append(
            f"• {safe_name}\n   оффер истёк {_format_deadline(w['deadline'])}, "
            f"вчера потрачено ${w['spend']:.2f}"
        )
    if len(fresh) > _MAX_ALERT_ADS:
        lines.append(f"\n…и ещё {len(fresh) - _MAX_ALERT_ADS} (показаны топ по расходу)")
    lines.append(f"\nНепроверяемых (видео без текста и без даты в имени): {unverifiable}")
    text = "\n".join(lines)

    # По кнопке на объявление: pause:<ad_id> — существующий обработчик telegram_bot
    # (PAUSED, обратимо). Сам крон не паузит — dry-run, решает человек.
    buttons = [[("⏸ Остановить", f"pause:{w['ad_id']}")] for w in shown]

    try:
        from services.telegram_bot import send_with_buttons
        sent = send_with_buttons(text, buttons)
    except Exception as exc:
        logger.warning("expired_offer_guard: не удалось отправить алерт — %s", type(exc).__name__)
        sent = False

    if sent:
        alerts = state.setdefault("alerts", {})
        for w in shown:
            alerts[w["ad_id"]] = now.isoformat()
        # Чистим протухшие записи (старше 30 дней), чтобы файл не пух
        cutoff = now - timedelta(days=30)
        for aid in list(alerts.keys()):
            try:
                if datetime.fromisoformat(alerts[aid]).replace(tzinfo=_TZ_LOCAL) < cutoff:
                    del alerts[aid]
            except Exception:
                continue
        _save_state(state)
        result["alerted"] = len(shown)
        logger.warning(
            "expired_offer_guard: алерт отправлен — %d просроченных офферов жгут бюджет "
            "(непроверяемых %d)", len(shown), unverifiable,
        )
    else:
        logger.warning("expired_offer_guard: Telegram не отправил алерт — повторим на след. прогоне")

    return result
