"""Почасовой read-only сторож рекламы.

Расход проверяет единый CDP-контур по завершённому дню. Этот модуль отдельно
следит только за проблемными статусами объявлений и отправляет их в health-бот.
FB insights, локальный analytics cache и управление рекламой здесь не используются.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict
from zoneinfo import ZoneInfo

from services.cdp_spend_alerts import SpendRunStatus

logger = logging.getLogger(__name__)

_TZ_LOCAL = ZoneInfo("Etc/GMT-5")
_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "ads_watchdog_state.json"
DEDUP_HOURS = 6


class AdsHealthResult(TypedDict):
    """Результат проверки только системных статусов объявлений."""

    alerts: list[str]
    ok: bool


class WatchdogRunResult(TypedDict):
    """Объединённый результат CDP spend и status-контуров."""

    alerts_sent: int
    alerts_skipped: int
    warnings_sent: int
    spend_status: SpendRunStatus
    ok: bool


def _load_state() -> dict:
    """Загружает state дедупликации системных status-алертов."""
    if not _STATE_FILE.exists():
        return {"alerts": {}}
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        if "alerts" not in data:
            data["alerts"] = {}
        return data
    except Exception as exc:
        logger.warning("ads_watchdog: не удалось прочитать state: %s", exc)
        return {"alerts": {}}


def _save_state(state: dict) -> None:
    """Атомарно сохраняет state системных status-алертов."""
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception as exc:
        logger.error("ads_watchdog: не удалось сохранить state: %s", exc)


def _is_deduped(alert_key: str, state: dict, now: datetime) -> bool:
    """True, если status-алерт уже отправлен в течение DEDUP_HOURS."""
    last_sent_str = state.get("alerts", {}).get(alert_key)
    if not last_sent_str:
        return False
    try:
        last_sent = datetime.fromisoformat(last_sent_str)
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=_TZ_LOCAL)
        return (now - last_sent).total_seconds() < DEDUP_HOURS * 3600
    except Exception:
        return False


def _mark_sent(alert_key: str, state: dict, now: datetime) -> None:
    """Помечает системный status-алерт отправленным."""
    state.setdefault("alerts", {})[alert_key] = now.isoformat()


def _check_problem_statuses() -> list[str]:
    """Проверяет DISAPPROVED / WITH_ISSUES в локальной creative_kb."""
    alerts: list[str] = []
    try:
        import sqlite3

        from services.creative_intelligence import DB_PATH

        if DB_PATH is None:
            return alerts

        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(
                """
                SELECT effective_status, COUNT(*) as cnt
                FROM creative_kb
                WHERE effective_status IN ('DISAPPROVED', 'WITH_ISSUES')
                  AND (archived = 0 OR archived IS NULL)
                GROUP BY effective_status
                """,
            ).fetchall()
        finally:
            conn.close()

        for status, count in rows:
            if count > 0:
                alerts.append(
                    f"Обнаружено {count} объявлений со статусом {status} — требует проверки"
                )
                logger.warning(
                    "ads_watchdog: %d объявлений со статусом %s",
                    count,
                    status,
                )
    except Exception as exc:
        logger.debug("ads_watchdog: не удалось проверить статусы через KB: %s", exc)

    return alerts


def check_ads_health() -> AdsHealthResult:
    """Проверяет только системные статусы; spend обслуживает CDP-контур."""
    alerts = _check_problem_statuses()
    return {"alerts": alerts, "ok": len(alerts) == 0}


def _local_now(now: datetime | None) -> datetime:
    """Нормализует время для status-дедупа; naive datetime считается UTC."""
    moment = now or datetime.now(_TZ_LOCAL)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_TZ_LOCAL)


def run_watchdog(now: datetime | None = None) -> WatchdogRunResult:
    """Объединяет CDP spend-алерты и health-алерты проблемных статусов.

    Старый вызов без аргументов совместим. Контуры независимы: сбой одного не
    отменяет второй. Функция только читает данные и отправляет уведомления.
    """
    from services.cdp_spend_alerts import run_cdp_spend_alerts

    try:
        spend_result = run_cdp_spend_alerts(now)
    except Exception as exc:
        # CDP runner сам never-throw, но защитный барьер сохраняет watchdog живым.
        logger.error("ads_watchdog: CDP spend-контур неожиданно упал: %s", exc)
        spend_result = {
            "target_date": None,
            "alerts_sent": 0,
            "alerts_skipped": 0,
            "warnings_sent": 0,
            "segments_resolved": [],
            "ok": False,
            "status": "degraded",
        }

    status_now = _local_now(now)
    state = _load_state()
    state_changed = False
    status_result = check_ads_health()
    status_alerts_sent = 0
    status_alerts_skipped = 0

    for alert_text in status_result["alerts"]:
        if _is_deduped(alert_text, state, status_now):
            status_alerts_skipped += 1
            logger.debug("ads_watchdog: status-алерт дедуплицирован: %s", alert_text[:60])
            continue

        try:
            from services.notifications import send_telegram

            sent = send_telegram(
                f"⚠️ <b>Сторож рекламы</b>\n{alert_text}",
                channel="health",
            )
            if sent:
                _mark_sent(alert_text, state, status_now)
                state_changed = True
                status_alerts_sent += 1
                logger.info("ads_watchdog: status-алерт отправлен: %s", alert_text[:80])
            else:
                status_alerts_skipped += 1
                logger.warning(
                    "ads_watchdog: Telegram не отправил status-алерт: %s",
                    alert_text[:60],
                )
        except Exception as exc:
            status_alerts_skipped += 1
            logger.warning("ads_watchdog: ошибка отправки status-алерта: %s", exc)

    if state_changed:
        _save_state(state)

    return {
        "alerts_sent": spend_result["alerts_sent"] + status_alerts_sent,
        "alerts_skipped": spend_result["alerts_skipped"] + status_alerts_skipped,
        "warnings_sent": spend_result["warnings_sent"],
        "spend_status": spend_result["status"],
        "ok": spend_result["ok"] and status_result["ok"],
    }
