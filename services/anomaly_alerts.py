"""
Алерты аномалий — два health-детектора, только чтение, без мутаций.

1. CPL-спайк по городу: CPL дня города > cpl_mult × медианы 7 дней (ad_daily_metrics
   JOIN creative_kb для city — у daily нет колонки city). Нужно ≥3 валидных дня
   истории города (день валиден если leads > 0), порог по расходу дня (cpl_min_spend) —
   не алертим на копеечных городах.
2. Серия FB-ошибок: fb_error_count_last_hour() >= fb_error_burst_threshold за час —
   возможен сбой токена/лимита. Счётчик — agent/fb_common.py (единая точка всех
   FB-вызовов через _do_throttled_request).

Расход здесь больше не оценивается: low/high/no-spend обслуживает единый
CDP completed-day контур services.cdp_spend_alerts.

Дедуп 6ч (dedup_hours из конфига) через data/anomaly_alerts_state.json — тот же
паттерн, что ads_watchdog_state.json/cron_watchdog_state.json. Ключ дедупа — текст
БЕЗ плавающих чисел (напр. "cpl_spike:CityA"), чтобы дрожание цифр не сбрасывало cooldown.

CPL и FB-ошибки шлются в Telegram channel="health".
run_anomaly_alerts() — публичная точка входа, never-throw.
"""

import json
import logging
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

# Локальный часовой пояс (UTC+5 по умолчанию, единый со всеми остальными модулями проекта)
_TZ_LOCAL = timezone(timedelta(hours=5))

# State-файл дедупликации алертов (паттерн ads_watchdog_state.json)
_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "anomaly_alerts_state.json"

# Значения по умолчанию — используются как fallback если ключ отсутствует в конфиге
# (get_autopilot_config делает shallow merge поверх AUTOPILOT_DEFAULTS, поэтому при
# частичном override settings.json блок anomaly_alerts может не содержать всех ключей).
_DEFAULT_CPL_MULT = 3.0
_DEFAULT_CPL_MIN_SPEND = 20.0
_DEFAULT_FB_ERROR_BURST_THRESHOLD = 5
_DEFAULT_DEDUP_HOURS = 6

# Минимум валидных дней истории города для расчёта медианы CPL (день валиден при leads>0)
_MIN_VALID_DAYS = 3


def _get_config() -> dict:
    """Читает блок anomaly_alerts из get_autopilot_config() с безопасными дефолтами."""
    from services.autopilot import get_autopilot_config

    cfg = get_autopilot_config().get("anomaly_alerts") or {}
    return {
        "enabled": cfg.get("enabled", True),
        "cpl_mult": float(cfg.get("cpl_mult", _DEFAULT_CPL_MULT)),
        "cpl_min_spend": float(cfg.get("cpl_min_spend", _DEFAULT_CPL_MIN_SPEND)),
        "fb_error_burst_threshold": int(
            cfg.get("fb_error_burst_threshold", _DEFAULT_FB_ERROR_BURST_THRESHOLD)
        ),
        "dedup_hours": float(cfg.get("dedup_hours", _DEFAULT_DEDUP_HOURS)),
    }


# ---------------------------------------------------------------------------
# State (дедуп) — паттерн ads_watchdog._load_state/_save_state/_is_deduped
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    """Загружает state-файл дедупликации алертов.

    Структура: {"alerts": {"ключ-без-чисел": "ISO-дата последней отправки"}}
    """
    if not _STATE_FILE.exists():
        return {"alerts": {}}
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        if "alerts" not in data:
            data["alerts"] = {}
        return data
    except Exception as e:
        logger.warning("anomaly_alerts: не удалось прочитать state: %s", e)
        return {"alerts": {}}


def _save_state(state: dict) -> None:
    """Атомарно сохраняет state-файл (tmp+rename)."""
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception as e:
        logger.error("anomaly_alerts: не удалось сохранить state: %s", e)


def _is_deduped(alert_key: str, state: dict, now: datetime, dedup_hours: float) -> bool:
    """True если алерт с этим ключом уже отправлен в течение dedup_hours часов."""
    last_sent_str = state.get("alerts", {}).get(alert_key)
    if not last_sent_str:
        return False
    try:
        last_sent = datetime.fromisoformat(last_sent_str)
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=_TZ_LOCAL)
        return (now - last_sent).total_seconds() < dedup_hours * 3600
    except Exception:
        return False


def _mark_sent(alert_key: str, state: dict, now: datetime) -> None:
    """Помечает алерт как отправленный (обновляет state in-place)."""
    state.setdefault("alerts", {})[alert_key] = now.isoformat()


# ---------------------------------------------------------------------------
# Детектор 1: CPL-спайк по городу
# ---------------------------------------------------------------------------

def detect_cpl_spike_by_city(now: datetime) -> list[str]:
    """CPL вчерашнего дня по городу > cpl_mult × медианы CPL за 7 дней до вчера.

    Медиана считается по дням с leads > 0 (иначе CPL неопределён), нужно
    минимум _MIN_VALID_DAYS валидных дней истории города. Алертим только
    если вчерашний spend города >= cpl_min_spend (отсекаем копеечные города).

    БД недоступна/пуста → [] (fail-quiet, лог debug).
    """
    try:
        from services.creative_intelligence import DB_PATH

        if not DB_PATH:
            logger.debug("anomaly_alerts: DB_PATH не инициализирован — CPL-детектор пропущен")
            return []

        cfg = _get_config()
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        week_ago = (now - timedelta(days=8)).strftime("%Y-%m-%d")   # вчера-7
        history_to = (now - timedelta(days=2)).strftime("%Y-%m-%d")  # вчера-1

        conn = sqlite3.connect(DB_PATH)
        try:
            # Вчерашний spend/leads по городу
            yesterday_rows = conn.execute(
                """
                SELECT k.city AS city,
                       SUM(m.spend) AS spend,
                       SUM(m.leads) AS leads
                FROM ad_daily_metrics m
                JOIN creative_kb k ON k.ad_id = m.ad_id
                WHERE m.date = ?
                  AND m.lead_semantics_version = 2
                  AND m.lead_parse_status IN ('ok', 'component_mismatch')
                  AND k.city IS NOT NULL AND k.city <> ''
                GROUP BY k.city
                """,
                (yesterday,),
            ).fetchall()

            # История 7 дней ДО вчера, по дням и городам
            history_rows = conn.execute(
                """
                SELECT m.date AS date,
                       k.city AS city,
                       SUM(m.spend) AS spend,
                       SUM(m.leads) AS leads
                FROM ad_daily_metrics m
                JOIN creative_kb k ON k.ad_id = m.ad_id
                WHERE m.date >= ? AND m.date <= ?
                  AND m.lead_semantics_version = 2
                  AND m.lead_parse_status IN ('ok', 'component_mismatch')
                  AND k.city IS NOT NULL AND k.city <> ''
                GROUP BY m.date, k.city
                """,
                (week_ago, history_to),
            ).fetchall()
        finally:
            conn.close()

        # Собираем дневные CPL истории по городу, отбрасывая дни с leads==0
        history_cpl_by_city: dict[str, list[float]] = {}
        for date_, city, spend, leads in history_rows:
            leads = leads or 0
            spend = spend or 0.0
            if leads <= 0:
                continue
            history_cpl_by_city.setdefault(city, []).append(spend / leads)

        alerts: list[str] = []
        for city, spend, leads in yesterday_rows:
            leads = leads or 0
            spend = spend or 0.0
            if leads <= 0:
                # CPL неопределён без лидов — нечего сравнивать
                continue
            if spend < cfg["cpl_min_spend"]:
                continue

            history_cpls = history_cpl_by_city.get(city, [])
            if len(history_cpls) < _MIN_VALID_DAYS:
                continue

            median_cpl = statistics.median(history_cpls)
            if median_cpl <= 0:
                continue

            cpl_yesterday = spend / leads
            if cpl_yesterday > cfg["cpl_mult"] * median_cpl:
                ratio = cpl_yesterday / median_cpl
                alerts.append(
                    f"CPL {city} {ratio:.1f}× медианы (вчера ¤{cpl_yesterday:.0f} "
                    f"vs норма ¤{median_cpl:.0f}, spend ${spend:.0f})"
                )
                logger.warning(
                    "anomaly_alerts: CPL-спайк %s — вчера %.0f, медиана %.0f (×%.1f)",
                    city, cpl_yesterday, median_cpl, ratio,
                )

        return alerts

    except Exception as e:
        logger.debug("anomaly_alerts: detect_cpl_spike_by_city ошибка: %s", e)
        return []


# ---------------------------------------------------------------------------
# Детектор 2: серия FB-ошибок за час
# ---------------------------------------------------------------------------

def detect_fb_error_burst(now: datetime) -> list[str]:
    """Серия FB-ошибок >= fb_error_burst_threshold за последний час → алерт.

    Читает счётчик из agent.fb_common.fb_error_count_last_hour() (кольцевой буфер
    таймстампов не-2xx ответов/устойчивого rate-limit). Счётчик in-memory —
    при рестарте процесса обнуляется (это ОК, нет истории ошибок = нет алерта).
    """
    try:
        from agent.fb_common import fb_error_count_last_hour

        cfg = _get_config()
        count = fb_error_count_last_hour(now.timestamp())

        if count >= cfg["fb_error_burst_threshold"]:
            logger.warning("anomaly_alerts: серия FB-ошибок — %d за час", count)
            return [
                f"Серия FB-ошибок: {count} за час — возможен сбой токена/лимита"
            ]

        return []

    except Exception as e:
        logger.debug("anomaly_alerts: detect_fb_error_burst ошибка: %s", e)
        return []


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

class AnomalyRunResult(TypedDict):
    """Счётчики отправки health-аномалий."""

    alerts_sent: int
    alerts_skipped: int


def run_anomaly_alerts(now: datetime | None = None) -> AnomalyRunResult:
    """Запускает CPL и FB-error детекторы и шлёт их в health-бот.

    Read-only: никаких мутаций FB/AMO/бюджетов. Never-throw — любая внутренняя
    ошибка логируется, функция всегда возвращает валидный dict.

    Returns:
        {"alerts_sent": int, "alerts_skipped": int}
    """
    if now is None:
        now = datetime.now(_TZ_LOCAL)

    try:
        cfg = _get_config()
    except Exception as e:
        logger.error("anomaly_alerts: не удалось прочитать конфиг: %s", e)
        return {"alerts_sent": 0, "alerts_skipped": 0}

    if not cfg.get("enabled", True):
        return {"alerts_sent": 0, "alerts_skipped": 0}

    alerts_sent = 0
    alerts_skipped = 0

    try:
        state = _load_state()
        state_changed = False

        # Собираем алерты вместе с дедуп-ключами (текст БЕЗ плавающих чисел)
        items: list[tuple[str, str]] = []  # (dedup_key, текст)

        for text in detect_cpl_spike_by_city(now):
            # Ключ = город без чисел. Извлекаем город из начала строки "CPL <city> ..."
            city = text.split(" ", 2)[1] if len(text.split(" ", 2)) > 1 else "unknown"
            items.append((f"cpl_spike:{city}", text))

        for text in detect_fb_error_burst(now):
            items.append(("fb_error_burst", text))

        for dedup_key, text in items:
            if _is_deduped(dedup_key, state, now, cfg["dedup_hours"]):
                alerts_skipped += 1
                logger.debug("anomaly_alerts: алерт дедуплицирован: %s", dedup_key)
                continue

            try:
                from services.notifications import send_telegram
                sent = send_telegram(
                    f"⚠️ <b>Аномалия</b>\n{text}",
                    channel="health",
                )
                if sent:
                    _mark_sent(dedup_key, state, now)
                    state_changed = True
                    alerts_sent += 1
                    logger.info("anomaly_alerts: алерт отправлен: %s", text[:80])
                else:
                    alerts_skipped += 1
                    logger.warning("anomaly_alerts: Telegram не отправил алерт: %s", dedup_key)
            except Exception as e:
                alerts_skipped += 1
                logger.warning("anomaly_alerts: ошибка отправки алерта %s: %s", dedup_key, e)

        if state_changed:
            _save_state(state)

    except Exception as e:
        logger.error("anomaly_alerts: run_anomaly_alerts упал: %s", e)

    return {"alerts_sent": alerts_sent, "alerts_skipped": alerts_skipped}
