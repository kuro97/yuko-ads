"""Страж трат адсетов — утренний контур (расширение идеи «нулевой доставки»).

Роль: НАБЛЮДАТЕЛЬ. Ничего не паузит и не меняет бюджеты — только читает у FB
дневной бюджет/статус адсета и вчерашний spend и шлёт ОДИН сводный Telegram-алерт
списком проблемных адсетов.

Что ловит по каждому эффективно-активному leadgen-адсету с daily_budget>0:
1. вчера потрачено $0 при живом бюджете — адсет «не тратит» (кейс, когда FB
   перестал откручивать бюджет и слив был не в перерасходе, а в простое);
2. вчера потрачено > бюджет × overspend_mult — адсет «перетрачивает».

Источник вчерашнего расхода — ЖИВОЙ GET /{adset_id}/insights за вчерашнюю дату,
а НЕ локальная ad_daily_metrics: там метрики ad-уровня и лаг из-за таймзоны
кабинета (днём кабинет в LA), поэтому дневная сумма адсета там неточна. insights
за конкретную дату отдаёт готовый суммарный расход адсета по таймзоне кабинета.

Антиспам: state-файл, не чаще одного алерта в день на адсет. Ошибки FB не роняют
крон — run_adset_spend_guard никогда не бросает исключение (вызывающий крон
получит ok=False и залогирует).

См. autopilot.spend_guard {enabled, overspend_mult} и валидацию в
web/settings_validation.py (паттерн guardian).
"""

import html
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from services import state_store
from services.formatting import fmt_money, truncate_at_word_boundary
from services.notifications import send_telegram

logger = logging.getLogger(__name__)

# Локальный часовой пояс (UTC+5 по умолчанию, единый со всеми модулями проекта)
_TZ_LOCAL = timezone(timedelta(hours=5))

# State-файл антиспама: {"sent": {adset_id: "YYYY-MM-DD"}}
_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "adset_spend_guard_state.json"

# Обрезка имени адсета в Telegram по границе слова
_NAME_MAX = 42

# Дефолтный множитель перерасхода (вчера потрачено > бюджет × mult → перетрата)
_OVERSPEND_MULT_DEFAULT = 1.25

# Дефолты настроек стража (мержатся поверх autopilot.spend_guard)
SPEND_GUARD_DEFAULTS: dict = {
    "enabled": True,
    "overspend_mult": _OVERSPEND_MULT_DEFAULT,
}


def get_spend_guard_config() -> dict:
    """Читает autopilot.spend_guard из settings.json, мержит поверх дефолтов.

    get_autopilot_config делает ПЛОСКИЙ мерж — если в settings.json задан
    spend_guard без overspend_mult, дефолтный overspend_mult потерялся бы, поэтому
    здесь повторно домешиваем SPEND_GUARD_DEFAULTS (тот же приём, что get_guardian_config).
    """
    from services.autopilot import get_autopilot_config

    cfg = get_autopilot_config().get("spend_guard") or {}
    return {**SPEND_GUARD_DEFAULTS, **cfg}


# ---------------------------------------------------------------------------
# FB (живые GET). Вынесены в отдельные функции — их же мокают тесты.
# ---------------------------------------------------------------------------

def _discover_leadgen_adset_ids() -> list[str]:
    """Список id эффективно-активных leadgen-адсетов из автообнаружения (без old)."""
    from agent.adset_discovery import discover_adsets

    leadgen = (discover_adsets() or {}).get("leadgen") or {}
    seen: set[str] = set()
    ids: list[str] = []
    for _city, types in leadgen.items():
        for _atype, aid in (types or {}).items():
            if not aid:
                continue
            aid = str(aid)
            if aid not in seen:
                seen.add(aid)
                ids.append(aid)
    return ids


def _fetch_adset_budgets(adset_ids: list[str]) -> dict:
    """Живой GET daily_budget+effective_status+name по adset_id (батч по 50).

    Бюджет FB отдаёт в ЦЕНТАХ — делим на 100. При не-200 бросаем RuntimeError:
    без бюджетов оценивать нечего, вызывающий run поймает и вернёт ok=False.

    Returns:
        {adset_id: {"daily_budget_usd": float, "effective_status": str, "name": str}}
    """
    if not adset_ids:
        return {}

    from agent.fb_common import API, _throttled_get
    from services.fb_token_provider import get_fb_token

    result: dict = {}
    for i in range(0, len(adset_ids), 50):
        chunk = adset_ids[i: i + 50]
        resp = _throttled_get(
            f"{API}",
            params={
                "access_token": get_fb_token(),
                "ids": ",".join(chunk),
                "fields": "id,name,daily_budget,effective_status",
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"FB adset GET {resp.status_code}: {resp.text[:200]}")
        for aid, info in (resp.json() or {}).items():
            budget_cents = int(info.get("daily_budget") or 0)
            result[aid] = {
                "daily_budget_usd": budget_cents / 100.0,
                "effective_status": info.get("effective_status", "UNKNOWN"),
                "name": info.get("name", ""),
            }
    return result


def _fetch_yesterday_spend(adset_id: str, day_iso: str) -> float | None:
    """Живой GET /{adset_id}/insights за день day_iso. spend USD или None при ошибке.

    Пустой ответ FB (нет открутки) → 0.0 — это ровно кейс «адсет не тратит».
    """
    from agent.fb_common import API, _throttled_get
    from services.fb_token_provider import get_fb_token

    try:
        resp = _throttled_get(
            f"{API}/{adset_id}/insights",
            params={
                "access_token": get_fb_token(),
                "fields": "spend",
                "time_range": json.dumps({"since": day_iso, "until": day_iso}),
            },
        )
        if resp.status_code != 200:
            logger.warning(
                "adset_spend_guard: insights %s вернул %s", adset_id, resp.status_code
            )
            return None
        rows = (resp.json() or {}).get("data", [])
        return sum(float(r.get("spend") or 0) for r in rows)
    except Exception as exc:
        logger.warning(
            "adset_spend_guard: insights %s ошибка — %s", adset_id, type(exc).__name__
        )
        return None


# ---------------------------------------------------------------------------
# State (атомарный JSON через общий state_store)
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    """Загружает state антиспама; чинит битую/legacy структуру."""
    state = state_store.load_json_state(_STATE_FILE)
    if not isinstance(state.get("sent"), dict):
        state["sent"] = {}
    return state


def _save_state(state: dict) -> None:
    """Сохраняет state; ошибку не бросает наружу (never-throw контур)."""
    try:
        state_store.save_json_state(_STATE_FILE, state)
    except Exception as exc:
        logger.error("adset_spend_guard: не удалось сохранить state — %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Форматирование и отправка
# ---------------------------------------------------------------------------

def _format_summary(target: date, findings: list[tuple]) -> str:
    """Один сводный алерт списком. findings: (adset_id, kind, name, budget, spend, pct)."""
    lines = [f"<b>Страж трат адсетов</b> · вчера {target.strftime('%d.%m')}", ""]
    for _aid, kind, name, budget, spend, pct in findings:
        safe = html.escape(truncate_at_word_boundary(name or _aid, _NAME_MAX), quote=False)
        if kind == "zero":
            lines.append(
                f"⚠️ Адсет не тратит: {safe} — бюджет {fmt_money(budget)}/день, вчера $0"
            )
        else:
            lines.append(
                f"⚠️ Адсет перетрачивает: {safe} — бюджет {fmt_money(budget)}, "
                f"вчера потрачено {fmt_money(spend)} (+{pct}%)"
            )
    return "\n".join(lines)


def _send(text: str) -> bool:
    """Отправка в основной бот; никогда не бросает."""
    try:
        return bool(send_telegram(text, channel="ads"))
    except Exception as exc:
        logger.warning("adset_spend_guard: Telegram exception — %s", type(exc).__name__)
        return False


def _normalize_moment(now: datetime | None) -> datetime:
    """Приводит now к Etc/GMT-5; naive трактует как UTC."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_TZ_LOCAL)


# ---------------------------------------------------------------------------
# Основной прогон (never-throw)
# ---------------------------------------------------------------------------

def run_adset_spend_guard(now: datetime | None = None) -> dict:
    """Проверяет вчерашний расход leadgen-адсетов и шлёт сводный алерт.

    Never-throw: любая ошибка (в т.ч. FB) → ok=False, status="error", без исключения.

    Returns:
        {"target_date", "checked", "not_spending", "overspending",
         "alerts_sent", "alerts_skipped", "ok", "status"}
    """
    moment = _normalize_moment(now)
    target = moment.date() - timedelta(days=1)
    result: dict = {
        "target_date": target.isoformat(),
        "checked": 0,
        "not_spending": 0,
        "overspending": 0,
        "alerts_sent": 0,
        "alerts_skipped": 0,
        "ok": True,
        "status": "no_findings",
    }

    try:
        cfg = get_spend_guard_config()
        if not cfg.get("enabled", True):
            result["status"] = "disabled"
            return result

        try:
            overspend_mult = float(cfg.get("overspend_mult", _OVERSPEND_MULT_DEFAULT))
        except (TypeError, ValueError):
            overspend_mult = _OVERSPEND_MULT_DEFAULT
        if overspend_mult <= 0:
            overspend_mult = _OVERSPEND_MULT_DEFAULT

        adset_ids = _discover_leadgen_adset_ids()
        if not adset_ids:
            result["status"] = "no_adsets"
            return result

        budgets = _fetch_adset_budgets(adset_ids)  # может бросить → except ниже

        day_iso = target.isoformat()
        findings: list[tuple] = []
        for aid in adset_ids:
            info = budgets.get(aid)
            if not info:
                continue
            if info.get("effective_status") != "ACTIVE":
                continue
            budget = float(info.get("daily_budget_usd") or 0.0)
            if budget <= 0:
                continue

            result["checked"] += 1
            spend = _fetch_yesterday_spend(aid, day_iso)
            if spend is None:
                continue  # не смогли оценить этот адсет — молча пропускаем

            name = info.get("name") or aid
            if spend == 0:
                findings.append((aid, "zero", name, budget, 0.0, None))
            elif spend > budget * overspend_mult:
                pct = round((spend / budget - 1) * 100)
                findings.append((aid, "over", name, budget, spend, pct))

        result["not_spending"] = sum(1 for f in findings if f[1] == "zero")
        result["overspending"] = sum(1 for f in findings if f[1] == "over")

        if not findings:
            result["status"] = "no_findings"
            return result

        # Антиспам: не чаще одного алерта в день на адсет
        state = _load_state()
        today_iso = moment.date().isoformat()
        sent_map = state.get("sent", {})
        fresh = [f for f in findings if sent_map.get(f[0]) != today_iso]
        result["alerts_skipped"] = len(findings) - len(fresh)
        if not fresh:
            result["status"] = "deduped"
            return result

        if not _send(_format_summary(target, fresh)):
            result["ok"] = False
            result["status"] = "send_failed"
            return result

        # Помечаем отправленные только после успешной отправки
        for f in fresh:
            state.setdefault("sent", {})[f[0]] = today_iso
        _save_state(state)
        result["alerts_sent"] = 1
        result["status"] = "sent"
        return result
    except Exception as exc:
        logger.warning("adset_spend_guard: ошибка прогона — %s", type(exc).__name__)
        result["ok"] = False
        result["status"] = "error"
        return result
